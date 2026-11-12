"""The radar mark, the synthetic demo fleet and the iTerm overlay installer."""
import asyncio
import contextlib
import io
import json
import os

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import standin  # noqa: E402,F401  (pins the time zone and config folder before fleetwatch loads)

from fleetwatch import app as fapp, data, demo, overlay, radar  # noqa: E402
from fleetwatch.__main__ import main  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
BRAILLE_ALERT = ' ⢀⠤⠒⣶⠒⠤⡀\n⡰⠁⣶⠖⣿⠲⠛⠈⢆\n⡇ ⡇ ⠿ ⢸ ⢸\n⠱⣠⡝⠦⠤⠴⠏⢀⠎\n ⠈⠒⠤⠤⠤⠒⠁'
ASCII_MARK = ' ...|...\n... | o..\n..  +  ..\n...   ...\n .......'


def colors(cells):
    return {col for line in cells for _, col in line if col}


class RadarTests(unittest.TestCase):
    def test_golden_alert_and_calm_share_a_shape(self):
        self.assertEqual(radar.plain(radar.rows(True, 0)), BRAILLE_ALERT)
        self.assertEqual(radar.plain(radar.rows(False, 0)), BRAILLE_ALERT)

    def test_one_blip_is_amber_only_in_the_alert_state(self):
        alert, calm = radar.rows(True, 0), radar.rows(False, 0)
        amber = [(r, c) for r, line in enumerate(alert) for c, (_, col) in enumerate(line) if col == radar.AMBER]
        self.assertEqual(amber, [(1, 6)])
        self.assertNotIn(radar.AMBER, colors(calm))
        self.assertEqual(colors(calm), {radar.TEAL, radar.RING})

    def test_size_and_sweep_steps(self):
        for step in range(radar.STEPS):
            cells = radar.rows(True, step)
            self.assertEqual((len(cells), {len(line) for line in cells}), (5, {9}))
        shapes = {radar.plain(radar.rows(True, s)) for s in range(radar.STEPS)}
        self.assertEqual(len(shapes), radar.STEPS)
        self.assertEqual(radar.plain(radar.rows(True, radar.STEPS)), radar.plain(radar.rows(True, 0)))

    def test_ascii_fallback_is_plain_ascii(self):
        for alert in (True, False):
            for step in range(radar.STEPS):
                text = radar.plain(radar.mark(alert, step, utf8=False))
                self.assertTrue(text.isascii(), text)
        self.assertEqual(radar.plain(radar.mark(True, 0, utf8=False)), ASCII_MARK)
        self.assertEqual([col for line in radar.mark(True, 0, False) for ch, col in line if col == radar.AMBER], [radar.AMBER])

    def test_single_glyph_fallback(self):
        self.assertEqual(radar.glyph(True), ("◉", radar.AMBER))
        self.assertEqual(radar.glyph(False), ("◉", radar.TEAL))
        self.assertEqual(radar.glyph(True, utf8=False), ("*", radar.AMBER))


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        self.tmp, self.paths = demo.prepare()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        self.tmp.cleanup()

    def snapshot(self, as_json=False):
        from fleetwatch.render_text import snapshot
        return snapshot(data.parse_window("24h"), False, data.DEFAULT_HORIZON_DAYS, as_json, self.paths, False)

    def test_everything_lives_in_the_temp_folder(self):
        root = os.path.realpath(self.tmp.name)
        for key in ("sessions", "jobs", "status", "projects", "archive"):
            self.assertTrue(os.path.realpath(self.paths[key]).startswith(root), key)
        for var in ("FLEETWATCH_CONFIG_DIR", "FLEETWATCH_STATUS_DIR", "FLEETWATCH_SESSIONS_DIR", "FLEETWATCH_PROJECTS_DIR"):
            self.assertTrue(os.path.realpath(os.environ[var]).startswith(root), var)

    def test_renders_the_planned_scenario(self):
        out = self.snapshot()
        for want in ("Billing API (Robin)", "stuck on a prompt", "Overdue (", "Due today:", "Docs Site",
                     "Checkout Flow", "1 helper"):
            self.assertIn(want, out)
        self.assertIn("2 questions" if "2 questions" in out else "1 stuck", out)
        view = json.loads(self.snapshot(as_json=True))
        self.assertGreaterEqual(len(json.dumps(view)), 100)

    def test_two_resume_rows_and_two_groups(self):
        from fleetwatch import config, resume
        self.assertEqual({e["name"] for e in resume.load(self.paths).entries()}, {"Pricing Page", "Status Page"})
        groups, warning = config.load_groups()
        self.assertIsNone(warning)
        self.assertEqual([n for n in groups.names() if n != config.OTHER], ["Storefront", "Docs and Site"])

    def test_resume_rows_show_home_folders_not_the_temp_path(self):
        from fleetwatch import resume, views
        now = time.time()
        entries = sorted(resume.load(self.paths).entries(), key=lambda e: e["name"])
        nodes = [views.resume_node(e, now) for e in entries]
        heads = [n.head.plain for n in nodes]
        self.assertIn("~/code/storefront/pricing", heads[0])
        self.assertIn("~/code/site/status", heads[1])
        self.assertIn("waiting on the new price list from finance", heads[0])
        shown = "\n".join(heads + [t.plain for n in nodes for _, t in n.body])
        self.assertIn("was: moving the status page to the new host", shown)
        self.assertNotIn(self.tmp.name, shown)
        self.assertNotIn(os.path.realpath(self.tmp.name), shown)
        self.assertTrue(all(e["folder_ok"] for e in entries), "the demo folders exist, so resume is offered")

    def test_names_are_fictional(self):
        text = self.snapshot() + json.dumps(sorted(os.listdir(self.paths["status"])))
        self.assertNotIn(os.path.expanduser("~"), text)

    def test_demo_once_through_main_ignores_other_dirs(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["--demo", "--once", "--status-dir", "/nonexistent/real"])
        self.assertEqual(code, 0)
        self.assertIn("Billing API (Robin)", buf.getvalue())


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "DynamicProfiles")
        patch = mock.patch.dict(os.environ, {"FLEETWATCH_ITERM_PROFILES_DIR": self.dir, "SHELL": "/bin/zsh"})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.target = os.path.join(self.dir, "fleetwatch.json")
        self.lines = []

    def run_cmd(self, fn):
        with mock.patch.object(overlay, "is_macos", return_value=True):
            return fn(self.lines.append)

    def load(self, name="fleetwatch.json"):
        with open(os.path.join(self.dir, name)) as fh:
            return json.load(fh)

    def write(self, name, doc):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, name), "w") as fh:
            json.dump(doc, fh)

    def test_fresh_install_writes_a_valid_profile(self):
        self.assertEqual(self.run_cmd(overlay.install), 0)
        prof = self.load()["Profiles"][0]
        self.assertEqual((prof["Guid"], prof["Name"], prof["Has Hotkey"]), (overlay.GUID, "fleetwatch", True))
        self.assertTrue(prof["Command"].startswith("/bin/zsh -ilc "))
        self.assertIn("fleetwatch", prof["Command"])
        self.assertNotIn("/opt/homebrew/bin/bash", prof["Command"])

    def test_command_uses_the_login_shell_and_the_running_entry(self):
        cmd = overlay.command("/bin/fish", ["/opt/x y/fleetwatch"])
        import shlex
        shell, flag, inner = shlex.split(cmd)
        self.assertEqual((shell, flag, shlex.split(inner)), ("/bin/fish", "-ilc", ["/opt/x y/fleetwatch"]))

    def test_repo_profile_matches_the_installed_one_but_for_the_command(self):
        with open(os.path.join(ROOT, "iterm", "fleetwatch.json")) as fh:
            sample = json.load(fh)["Profiles"][0]
        built = overlay.profile("x")
        sample.pop("Command"), built.pop("Command")
        self.assertEqual(sample, built)

    def test_reinstall_replaces(self):
        self.run_cmd(overlay.install)
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            self.run_cmd(overlay.install)
        profs = self.load()["Profiles"]
        self.assertEqual(len(profs), 1)
        self.assertTrue(profs[0]["Command"].startswith("/bin/bash -ilc "))

    def test_predecessor_with_the_same_guid_is_removed(self):
        self.write("fleet.json", {"Profiles": [{"Name": "fleet", "Guid": overlay.GUID}]})
        self.run_cmd(overlay.install)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "fleet.json")))
        self.assertTrue(os.path.exists(self.target))

    def test_predecessor_keeps_other_profiles_and_unrelated_files_are_untouched(self):
        other = {"Profiles": [{"Name": "mine", "Guid": "AAAA"}]}
        self.write("fleet.json", {"Profiles": [{"Name": "fleet", "Guid": overlay.GUID}, other["Profiles"][0]]})
        self.write("mine.json", other)
        before = open(os.path.join(self.dir, "mine.json")).read()
        self.run_cmd(overlay.install)
        self.assertEqual(self.load("fleet.json"), other)
        self.assertEqual(open(os.path.join(self.dir, "mine.json")).read(), before)

    def test_a_predecessor_with_another_guid_is_left_alone(self):
        self.write("fleet.json", {"Profiles": [{"Name": "fleet", "Guid": "BBBB"}]})
        self.run_cmd(overlay.install)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "fleet.json")))

    def test_a_foreign_file_at_the_target_is_not_overwritten(self):
        self.write("fleetwatch.json", {"Profiles": [{"Name": "other", "Guid": "CCCC"}]})
        self.assertEqual(self.run_cmd(overlay.install), 1)
        self.assertEqual(self.load()["Profiles"][0]["Guid"], "CCCC")

    def test_status_and_uninstall(self):
        self.run_cmd(overlay.status)
        self.assertTrue(any("not installed" in x for x in self.lines))
        self.run_cmd(overlay.install)
        self.lines.clear()
        self.run_cmd(overlay.status)
        self.assertTrue(any("Hotkey profile: installed" in x for x in self.lines))
        self.assertTrue(any(x.startswith("Command: /bin/zsh -ilc") for x in self.lines))
        self.assertTrue(any(x.startswith("iTerm2:") for x in self.lines))
        self.run_cmd(overlay.uninstall)
        self.assertFalse(os.path.exists(self.target))

    def test_not_macos_prints_and_exits_zero_without_writing(self):
        lines = []
        with mock.patch.object(overlay, "is_macos", return_value=False):
            for fn in (overlay.install, overlay.status, overlay.uninstall):
                self.assertEqual(fn(lines.append), 0)
        self.assertEqual(lines, [overlay.NOT_MAC] * 3)
        self.assertFalse(os.path.exists(self.dir))

    def test_cli_route_and_usage(self):
        self.assertEqual(main(["iterm", "bogus"]), 2)


class RadarInAppTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        self.tmp, self.paths = demo.prepare()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        self.tmp.cleanup()

    def screen(self, size, quiet=False):
        async def go():
            app = fapp.FleetwatchApp(lambda: fapp.gather(self.paths, data.parse_window("24h"), data.DEFAULT_HORIZON_DAYS),
                                     3600, paths=self.paths)
            async with app.run_test(size=size) as pilot:
                await pilot.pause(1.5)
                rendered = app.query_one("#radar").render()
                spans = [str(sp.style).lower() for sp in rendered.spans]
                return rendered.plain, spans, app.refreshes
        return asyncio.run(go())

    def test_tall_terminal_shows_the_mark_with_amber_blip(self):
        text, spans, step = self.screen((110, 40))
        self.assertEqual([x.rstrip() for x in text.split("\n")], radar.plain(radar.rows(True, step)).split("\n"))
        self.assertTrue(any("245,185,80" in sp for sp in spans), spans)

    def test_short_terminal_hides_the_mark(self):
        async def go():
            app = fapp.FleetwatchApp(lambda: fapp.gather(self.paths, data.parse_window("24h"), data.DEFAULT_HORIZON_DAYS),
                                     3600, paths=self.paths)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(1.5)
                return app.query_one("#radar").display
        self.assertFalse(asyncio.run(go()))


class DemoPtyTests(unittest.TestCase):
    def test_live_demo_renders_the_radar_and_quits(self):
        import fcntl
        import struct
        import termios
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 110, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", PYTHONIOENCODING="utf-8", LANG="en_US.UTF-8")
        proc = subprocess.Popen([sys.executable, "-m", "fleetwatch", "--demo", "--interval", "1"], env=env, cwd=ROOT,
                                stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        buf, quit_at, sent = b"", time.time() + 4, False
        while time.time() < quit_at + 15 and proc.poll() is None:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    buf += os.read(master, 65536)
                except OSError:
                    break
            if not sent and time.time() >= quit_at:
                os.write(master, b"q")
                sent = True
        code = proc.wait(timeout=15)
        os.close(master), os.close(slave)
        out = buf.decode("utf-8", "replace")
        self.assertEqual(code, 0, out[-300:])
        self.assertIn("Billing API", out)
        self.assertGreater(sum(1 for ch in out if "⠀" <= ch <= "⣿"), 10)


if __name__ == "__main__":
    unittest.main()
