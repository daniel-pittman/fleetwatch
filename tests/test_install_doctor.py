"""fleetwatch install, uninstall and doctor over the dependency inventory, the retirement of older jobs, and the
index failure-streak alert. launchd is tests/fake_launchd.py and systemd a recording runner; every folder
is a temporary one."""
import contextlib
import io
import json
import os
import plistlib
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fleetwatch import dependencies as deps  # noqa: E402
from fleetwatch import index_health, install_cli, notify, schedule, services, views  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
LEGACY_INDEX, LEGACY_ARCHIVE = "local.search-indexes.refresh", "local.claude-session-archive"
OURS = ("local.fleetwatch.index-refresh", "local.fleetwatch.watch-run", "local.fleetwatch.notify",
        "local.fleetwatch.archive")
MUTATING = ("bootstrap", "bootout", "kickstart")


def sid(n):
    return "aa11bb22-0000-4000-8000-%012d" % n


class Home:
    """A throwaway home: LaunchAgents, systemd, data, state, config, projects and archive folders, and a fake
    launchd whose state lives in a JSON file."""

    def __init__(self, test):
        d = tempfile.TemporaryDirectory()
        test.addCleanup(d.cleanup)
        self.root = Path(d.name).resolve()
        self.agents = self.root / "LaunchAgents"
        self.agents.mkdir()
        self.state_file = self.root / "launchd.json"
        self.log = self.root / "launchctl.log"
        self.projects = self.root / "projects"
        self.archive = self.root / "archive"
        self.env = {
            "FLEETWATCH_LAUNCH_AGENTS_DIR": str(self.agents),
            "FLEETWATCH_SYSTEMD_USER_DIR": str(self.root / "systemd"),
            "XDG_DATA_HOME": str(self.root / "data"), "XDG_STATE_HOME": str(self.root / "state"),
            "FLEETWATCH_CONFIG_DIR": str(self.root / "config"),
            "FLEETWATCH_PROJECTS_DIR": str(self.projects), "CLAUDE_SESSION_ARCHIVE_DEST": str(self.archive),
            "FLEETWATCH_ARCHIVE_DIR": str(self.archive / "projects"),
        }
        p = mock.patch.dict(os.environ, self.env)
        p.start()
        test.addCleanup(p.stop)
        os.environ.pop("FLEETWATCH_SYSTEMCTL", None)
        plat = mock.patch.object(sys, "platform", "darwin")
        plat.start()
        test.addCleanup(plat.stop)
        os.environ["FLEETWATCH_LAUNCHCTL"] = shlex.join([sys.executable, str(REPO / "tests" / "fake_launchd.py")])
        os.environ["FAKE_LAUNCHCTL_STATE"] = str(self.state_file)
        os.environ["FAKE_LAUNCHCTL_LOG"] = str(self.log)
        self.write_launchd({})

    @property
    def state(self):
        return self.root / "state" / "fleetwatch"

    def write_launchd(self, loaded, running=(), fail=()):
        self.state_file.write_text(json.dumps({"loaded": loaded, "running": list(running), "fail": list(fail)}))

    def launchd(self):
        return json.loads(self.state_file.read_text())

    def manager(self):
        return services.Manager("darwin")

    def calls(self, mutating_only=True):
        if not self.log.exists():
            return []
        # The domain is this user's (schedule.gui_domain()); the expected lines spell it gui/501.
        lines = [x.replace(schedule.gui_domain(), "gui/501") for x in self.log.read_text().splitlines()]
        return [x for x in lines if x.split()[0] in MUTATING] if mutating_only else lines

    def legacy(self, label, loaded=True, args=None):
        path = self.agents / (label + ".plist")
        path.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": args or ["/bin/sh", "old.sh"]}))
        if loaded:
            st = self.launchd()
            st["loaded"][label] = str(path)
            self.state_file.write_text(json.dumps(st))
        return path

    def transcript(self, rel, archived=False, age=0):
        src = self.projects / rel
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text('{"type":"user"}\n')
        if age:
            t = time.time() - age
            os.utime(src, (t, t))
        if archived:
            dst = self.archive / "projects" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text())
        return src

    def archive_run(self, ago=60, ok=True, error=None, started_ago=None):
        path = self.state / "archive" / "last_run.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        path.write_text(json.dumps({"started": now - (started_ago or ago + 5), "ended": now - ago, "ok": ok,
                                    "copied": 1, "transcripts_live": 1, "transcripts_archived": 1, "error": error}))

    def job_logs(self, ago=60):
        self.state.mkdir(parents=True, exist_ok=True)
        t = time.time() - ago
        for name in ("index-refresh", "watch-run"):
            p = self.state / (name + ".log")
            p.write_text("=== run started\n")
            os.utime(p, (t, t))


def run(fn, home, argv=(), **kw):
    out = io.StringIO()
    code = fn(list(argv), out, home.manager(), **kw)
    return code, out.getvalue()


def no_indexes():
    return mock.patch.object(index_health, "load", return_value=index_health.Result(note=index_health.NO_INDEXES))


# ---------------------------------------------------------------- install and uninstall

class Install(unittest.TestCase):
    def setUp(self):
        self.h = Home(self)

    def test_dry_run_changes_nothing(self):
        old = self.h.legacy(LEGACY_INDEX)
        before = self.h.launchd()
        code, out = run(install_cli.install_main, self.h, ["--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(self.h.calls(), [])
        self.assertEqual(self.h.launchd(), before)
        self.assertEqual(sorted(p.name for p in self.h.agents.iterdir()), [old.name])
        self.assertFalse((self.h.root / "state").exists())
        self.assertFalse((self.h.root / "data").exists())
        self.assertIn("would write", out)
        self.assertIn("would load local.fleetwatch.watch-run", out)
        self.assertIn("would unload the legacy agent %s" % LEGACY_INDEX, out)

    def test_install_writes_and_loads_every_job_and_creates_the_folders(self):
        code, out = run(install_cli.install_main, self.h)
        self.assertEqual(code, 0, out)
        for label in OURS:
            plist = plistlib.loads((self.h.agents / (label + ".plist")).read_bytes())
            self.assertEqual(plist["Label"], label)
        self.assertEqual(sorted(self.h.launchd()["loaded"]), sorted(OURS))
        archive = plistlib.loads((self.h.agents / "local.fleetwatch.archive.plist").read_bytes())
        self.assertEqual(archive["ProgramArguments"][-2:], ["archive", "run"])
        self.assertEqual(archive["StartInterval"], 3600)
        for folder in (self.h.root / "data" / "fleetwatch", self.h.state, self.h.archive):
            self.assertTrue(folder.is_dir(), folder)
        self.assertIn("next: fleetwatch doctor", out)

    def test_install_is_idempotent(self):
        run(install_cli.install_main, self.h)
        n = len(self.h.calls())
        code, out = run(install_cli.install_main, self.h)
        self.assertEqual(code, 0)
        self.assertEqual(self.h.calls()[n:], [])
        self.assertEqual(out.count("unchanged:"), len(OURS))
        self.assertIn("in place", out)

    def test_a_changed_job_is_reloaded(self):
        run(install_cli.install_main, self.h)
        path = self.h.agents / "local.fleetwatch.watch-run.plist"
        plist = plistlib.loads(path.read_bytes())
        plist["StartInterval"] = 5
        path.write_bytes(plistlib.dumps(plist))
        n = len(self.h.calls())
        code, out = run(install_cli.install_main, self.h)
        self.assertEqual(code, 0)
        self.assertEqual(self.h.calls()[n:], ["bootout gui/501/local.fleetwatch.watch-run",
                                              "bootstrap gui/501 %s" % path])
        self.assertIn("reloaded local.fleetwatch.watch-run", out)
        self.assertEqual(plistlib.loads(path.read_bytes())["StartInterval"], 900)

    def test_older_jobs_are_unloaded_and_their_plists_kept(self):
        index = self.h.legacy(LEGACY_INDEX)
        archive = self.h.legacy(LEGACY_ARCHIVE)
        body = index.read_bytes()
        code, out = run(install_cli.install_main, self.h)
        self.assertEqual(code, 0, out)
        loaded = self.h.launchd()["loaded"]
        self.assertNotIn(LEGACY_INDEX, loaded)
        self.assertNotIn(LEGACY_ARCHIVE, loaded)
        self.assertFalse(index.exists() or archive.exists())
        kept = self.h.state / "legacy"
        self.assertEqual((kept / (LEGACY_INDEX + ".plist")).read_bytes(), body)
        self.assertTrue((kept / (LEGACY_ARCHIVE + ".plist")).exists())
        self.assertIn("unloaded the legacy agent %s" % LEGACY_INDEX, out)
        self.assertIn("unloaded the legacy agent %s" % LEGACY_ARCHIVE, out)
        self.assertIn("(kept as a backup)", out)
        # each older job is unloaded after its replacement loads
        calls = self.h.calls()
        self.assertLess(calls.index("bootstrap gui/501 %s" % (self.h.agents / "local.fleetwatch.index-refresh.plist")),
                        calls.index("bootout gui/501/%s" % LEGACY_INDEX))

    def test_an_unloaded_older_plist_is_still_moved_aside(self):
        index = self.h.legacy(LEGACY_INDEX, loaded=False)
        run(install_cli.install_main, self.h)
        self.assertFalse(index.exists())
        self.assertTrue((self.h.state / "legacy" / (LEGACY_INDEX + ".plist")).exists())
        self.assertNotIn("bootout gui/501/%s" % LEGACY_INDEX, self.h.calls())

    def test_an_earlier_backup_is_never_replaced(self):
        kept = self.h.state / "legacy"
        kept.mkdir(parents=True)
        (kept / (LEGACY_INDEX + ".plist")).write_text("the first backup")
        self.h.legacy(LEGACY_INDEX)
        run(install_cli.install_main, self.h)
        self.assertEqual((kept / (LEGACY_INDEX + ".plist")).read_text(), "the first backup")
        self.assertEqual(len(list(kept.glob(LEGACY_INDEX + ".*.plist"))), 1)

    def test_a_failed_unload_leaves_the_older_plist_and_fails(self):
        index = self.h.legacy(LEGACY_INDEX)
        st = self.h.launchd()
        st["fail"] = [LEGACY_INDEX]
        self.h.state_file.write_text(json.dumps(st))
        code, out = run(install_cli.install_main, self.h)
        self.assertEqual(code, 1)
        self.assertTrue(index.exists())
        self.assertIn("could not unload the legacy agent", out)

    def test_an_older_job_stays_when_its_replacement_does_not_start(self):
        index = self.h.legacy(LEGACY_INDEX)
        st = self.h.launchd()
        st["fail"] = ["local.fleetwatch.index-refresh"]
        self.h.state_file.write_text(json.dumps(st))
        code, out = run(install_cli.install_main, self.h)
        self.assertEqual(code, 1)
        self.assertTrue(index.exists())
        self.assertIn(LEGACY_INDEX, self.h.launchd()["loaded"])
        self.assertIn("could not start local.fleetwatch.index-refresh", out)

    def test_the_legacy_archiver_stays_when_the_archive_job_does_not_load(self):
        old = self.h.legacy(LEGACY_ARCHIVE)
        st = self.h.launchd()
        st["fail"] = ["local.fleetwatch.archive"]
        self.h.state_file.write_text(json.dumps(st))
        code, out = run(install_cli.install_main, self.h)
        self.assertEqual(code, 1)
        self.assertTrue(old.exists())
        self.assertIn(LEGACY_ARCHIVE, self.h.launchd()["loaded"])
        self.assertNotIn("bootout gui/501/%s" % LEGACY_ARCHIVE, self.h.calls())

    def test_uninstall_unloads_and_removes_only_fleetwatchs_jobs(self):
        self.h.legacy(LEGACY_INDEX)
        other = self.h.agents / "com.example.other.plist"
        other.write_bytes(plistlib.dumps({"Label": "com.example.other", "ProgramArguments": ["/bin/true"]}))
        run(install_cli.install_main, self.h)
        code, out = run(install_cli.uninstall_main, self.h)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.h.launchd()["loaded"], {})
        self.assertEqual([p.name for p in self.h.agents.iterdir()], [other.name])
        self.assertTrue((self.h.state / "legacy" / (LEGACY_INDEX + ".plist")).exists())
        self.assertTrue(self.h.archive.is_dir())
        self.assertIn("are kept in", out)
        code, out = run(install_cli.uninstall_main, self.h)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("not installed"), len(OURS))

    def test_a_job_that_cannot_be_unloaded_keeps_its_plist_and_fails(self):
        run(install_cli.install_main, self.h)
        st = self.h.launchd()
        st["fail"] = ["local.fleetwatch.index-refresh", "local.fleetwatch.notify"]
        self.h.state_file.write_text(json.dumps(st))
        code, out = run(install_cli.uninstall_main, self.h)
        self.assertEqual(code, 1, out)
        self.assertEqual(sorted(p.name for p in self.h.agents.iterdir()),
                         ["local.fleetwatch.index-refresh.plist", "local.fleetwatch.notify.plist"])
        self.assertEqual(sorted(self.h.launchd()["loaded"]),
                         ["local.fleetwatch.index-refresh", "local.fleetwatch.notify"])
        self.assertIn("could not unload local.fleetwatch.index-refresh", out)
        self.assertIn("kept %s" % deps._home(self.h.agents / "local.fleetwatch.index-refresh.plist"), out)

    def test_uninstall_dry_run_changes_nothing(self):
        run(install_cli.install_main, self.h)
        n, before = len(self.h.calls()), sorted(p.name for p in self.h.agents.iterdir())
        code, out = run(install_cli.uninstall_main, self.h, ["--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(self.h.calls()[n:], [])
        self.assertEqual(sorted(p.name for p in self.h.agents.iterdir()), before)
        self.assertIn("would unload local.fleetwatch.notify", out)

    def test_a_trial_folder_never_runs_the_real_launchctl(self):
        m = services.Manager("darwin", environ={"FLEETWATCH_LAUNCH_AGENTS_DIR": str(self.h.agents)})
        self.h.legacy(LEGACY_INDEX)
        out = io.StringIO()
        with mock.patch.object(schedule, "launchctl", side_effect=AssertionError("ran launchctl")) as lc:
            self.assertEqual(install_cli.install_main([], out, m), 0)
        lc.assert_not_called()
        self.assertTrue((self.h.agents / (LEGACY_INDEX + ".plist")).exists())
        self.assertIn("not retired: %s (a trial folder is set)" % LEGACY_INDEX, out.getvalue())
        self.assertIn("not started: launchd cannot be asked whether it is loaded (a trial folder is set)", out.getvalue())
        self.assertIn("launchctl bootstrap", out.getvalue())
        self.assertTrue((self.h.agents / "local.fleetwatch.index-refresh.plist").exists())

    def test_linux_writes_units_and_enables_them_with_systemctl(self):
        calls = []

        def runner(argv, **_kw):
            calls.append(argv[2:])
            return subprocess.CompletedProcess(argv, 3 if argv[2] == "is-active" else 0, "", "")
        m = services.Manager("linux", runner, {"FLEETWATCH_SYSTEMCTL": "systemctl --user"}, )
        with mock.patch.object(sys, "platform", "linux"):
            out = io.StringIO()
            self.assertEqual(install_cli.install_main([], out, m), 0, out.getvalue())
            units = self.h.root / "systemd"
            for name in ("fleetwatch-index-refresh.timer", "fleetwatch-watch-run.timer", "fleetwatch-notify.service",
                         "fleetwatch-archive.timer", "fleetwatch-archive.service"):
                self.assertTrue((units / name).exists(), name)
            self.assertIn(["enable", "--now", "fleetwatch-archive.timer"], calls)
            self.assertIn(["enable", "--now", "fleetwatch-notify.service"], calls)
            self.assertFalse(any(c[0] in MUTATING for c in calls))
            calls.clear()
            runner2 = lambda argv, **_kw: (calls.append(argv[2:]), subprocess.CompletedProcess(argv, 0, "", ""))[1]
            m2 = services.Manager("linux", runner2, {"FLEETWATCH_SYSTEMCTL": "systemctl --user"}, )
            self.assertEqual(install_cli.uninstall_main([], io.StringIO(), m2), 0)
            self.assertIn(["disable", "--now", "fleetwatch-index-refresh.timer"], calls)
            self.assertFalse((units / "fleetwatch-index-refresh.timer").exists())

    def test_the_fake_launchctl_command_survives_paths_with_spaces(self):
        with mock.patch.object(sys, "executable", "/opt/my python/bin/python3"):
            Home(self)
        self.assertEqual(shlex.split(os.environ["FLEETWATCH_LAUNCHCTL"]),
                         ["/opt/my python/bin/python3", str(REPO / "tests" / "fake_launchd.py")])

    def test_the_command_line_reaches_install_uninstall_and_doctor(self):
        from fleetwatch import __main__ as cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["doctor", "--inventory"]), 0)
        self.assertTrue(out.getvalue().startswith("# What fleetwatch depends on"))
        self.assertIn("install [--dry-run]", cli.build_parser().format_help())


# ---------------------------------------------------------------- doctor

class Doctor(unittest.TestCase):
    def setUp(self):
        self.h = Home(self)
        p = no_indexes()
        p.start()
        self.addCleanup(p.stop)

    def healthy(self):
        run(install_cli.install_main, self.h)
        st = self.h.launchd()
        st["running"] = ["local.fleetwatch.notify"]
        self.h.state_file.write_text(json.dumps(st))
        self.h.job_logs()
        self.h.transcript("-work-a/%s.jsonl" % sid(1), archived=True, age=7200)
        self.h.archive_run()

    def doctor(self, *argv):
        code, out = run(install_cli.doctor_main, self.h, argv)
        return code, out

    def rows(self):
        code, out = self.doctor("--json")
        d = json.loads(out)
        self.assertEqual(d["ok"], code == 0)
        return code, {(c["item"], c["check"]): c for c in d["checks"]}

    def failing(self):
        code, rows = self.rows()
        return code, {k: v for k, v in rows.items() if v["status"] == "fail"}

    def test_a_healthy_install_is_clean(self):
        self.healthy()
        code, bad = self.failing()
        self.assertEqual((code, bad), (0, {}))
        code, out = self.doctor()
        self.assertEqual(code, 0)
        self.assertIn("no problems", out)

    def test_a_missing_tool_is_a_warning_not_a_failure(self):
        self.healthy()
        with mock.patch.object(deps.shutil, "which", return_value=None):
            code, rows = self.rows()
        self.assertEqual(code, 0)
        self.assertEqual(rows[("gh", "on the PATH")]["status"], "warn")
        self.assertNotIn(("notify-send", "on the PATH"), rows)    # a Linux program is not checked on macOS

    def test_nothing_installed(self):
        code, bad = self.failing()
        self.assertEqual(code, 1)
        for name in ("index refresh", "watch run", "notify", "archive"):
            self.assertEqual(bad[(name, "installed")]["fix"], "fleetwatch install")
        self.assertIn(("state folder", "exists"), bad)

    def test_installed_but_not_loaded(self):
        self.healthy()
        st = self.h.launchd()
        del st["loaded"]["local.fleetwatch.watch-run"]
        self.h.state_file.write_text(json.dumps(st))
        code, bad = self.failing()
        self.assertEqual(code, 1)
        self.assertEqual(list(bad), [("watch run", "loaded")])

    def test_notify_loaded_but_not_running(self):
        self.healthy()
        st = self.h.launchd()
        st["running"] = []
        self.h.state_file.write_text(json.dumps(st))
        self.assertEqual(list(self.failing()[1]), [("notify", "running")])

    def test_a_stale_last_run(self):
        self.healthy()
        self.h.job_logs(ago=3 * 3600)
        bad = self.failing()[1]
        self.assertEqual(sorted(bad), [("index refresh", "last run"), ("watch run", "last run")])
        self.assertIn("last ran 3.0h ago (runs every 30m)", bad[("index refresh", "last run")]["detail"])

    def test_no_run_yet_is_fine_just_after_install_but_not_later(self):
        run(install_cli.install_main, self.h)
        rows = self.rows()[1]
        self.assertEqual(rows[("index refresh", "last run")]["status"], "ok")
        old = time.time() - 3 * 3600
        for p in self.h.agents.iterdir():
            os.utime(p, (old, old))
        self.assertEqual(self.rows()[1][("index refresh", "last run")]["status"], "fail")

    def test_on_linux_a_job_that_ran_is_seen_through_the_log_its_unit_names(self):
        # systemd writes a run's output where the unit's StandardOutput= and StandardError= say; doctor
        # reads <state>/<name>.log and .err, so a healthy Linux machine passes only if the two agree.
        runner = lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, "", "")
        m = services.Manager("linux", runner, {"FLEETWATCH_SYSTEMCTL": "systemctl --user"})
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(install_cli.install_main([], io.StringIO(), m), 0)
            units = self.h.root / "systemd"
            old = time.time() - 3 * 3600
            for p in units.iterdir():
                os.utime(p, (old, old))
            for unit, stream in (("fleetwatch-index-refresh", "StandardOutput"),
                                 ("fleetwatch-watch-run", "StandardError")):
                lines = (units / (unit + ".service")).read_text().splitlines()
                target = {k: v for k, _, v in (ln.partition("=") for ln in lines)}.get(stream, "")
                self.assertTrue(target.startswith("append:"), (unit, stream, lines))
                with open(target[len("append:"):], "a") as f:      # what systemd does on a run
                    f.write("=== run started\n")
            self.h.transcript("-work-a/%s.jsonl" % sid(1), archived=True, age=7200)
            self.h.archive_run()
            out = io.StringIO()
            install_cli.doctor_main(["--json"], out, m)
        rows = {(c["item"], c["check"]): c for c in json.loads(out.getvalue())["checks"]}
        for name in ("index refresh", "watch run"):
            self.assertEqual(rows[(name, "last run")]["status"], "ok", rows[(name, "last run")])

    def test_linux_units_append_to_the_state_folder_logs(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(
                self.h.root / "state 100%")}):
            schedule.install(io.StringIO(), schedule.INDEX)
            service = (self.h.root / "systemd" / "fleetwatch-index-refresh.service").read_text()
            log_dir = Path(schedule.settings.state_dir())
        self.assertTrue(log_dir.is_dir())
        self.assertIn("StandardOutput=append:%s\n" % str(log_dir / "index-refresh.log").replace("%", "%%"), service)
        self.assertIn("StandardError=append:%s\n" % str(log_dir / "index-refresh.err").replace("%", "%%"), service)
        self.assertLess(service.index("StandardOutput="), service.index("ExecStart="))

    def test_an_older_job_still_loaded_or_installed(self):
        self.healthy()
        self.h.legacy(LEGACY_INDEX)
        self.h.legacy(LEGACY_ARCHIVE, loaded=False)
        bad = self.failing()[1]
        self.assertIn("still loaded", bad[("index refresh", "no older job")]["detail"])
        self.assertIn("loads at the next login", bad[("archive", "no older job")]["detail"])

    def test_two_loaded_jobs_running_the_same_command(self):
        self.healthy()
        plist = plistlib.loads((self.h.agents / "local.fleetwatch.index-refresh.plist").read_bytes())
        plist["Label"] = "com.example.copy"
        plist["ProgramArguments"][0] = "/some/other/python"
        path = self.h.agents / "com.example.copy.plist"
        path.write_bytes(plistlib.dumps(plist))
        st = self.h.launchd()
        st["loaded"]["com.example.copy"] = str(path)
        self.h.state_file.write_text(json.dumps(st))
        bad = self.failing()[1]
        self.assertIn("com.example.copy, local.fleetwatch.index-refresh all run `fleetwatch index refresh --all`",
                      bad[("scheduled jobs", "no duplicates")]["detail"])

    def test_the_archive_last_run_failed(self):
        self.healthy()
        self.h.archive_run(ok=False, error="rsync exited 23\nsecond line with detail")
        bad = self.failing()[1]
        detail = bad[("session archive", "archive check")]["detail"]
        self.assertEqual(detail, "the last archive run failed: rsync exited 23")

    def test_archive_coverage(self):
        self.healthy()
        side = self.h.projects / "-work-a" / sid(1) / "subagents" / "agent-x.jsonl"
        side.parent.mkdir(parents=True)
        side.write_text("{}")                                      # a side file is not a main transcript
        self.h.transcript("-work-b/%s.jsonl" % sid(2))                    # written after the last run started: waits
        code, rows = self.rows()
        self.assertEqual(code, 0)
        self.assertIn("1 newer waiting", rows[("session archive", "archive check")]["detail"])
        self.h.transcript("-work-b/%s.jsonl" % sid(3), age=3600)          # older than the last run, yet not archived
        bad = self.failing()[1]
        self.assertEqual(bad[("session archive", "archive check")]["detail"],
                         "1 live transcript with no archive copy")

    def test_a_shorter_archive_copy_that_differs_is_a_warning(self):
        self.healthy()
        grown = self.h.transcript("-work-c/%s.jsonl" % sid(4), archived=True, age=7200)
        grown.write_text(grown.read_text() + '{"type":"assistant"}\n')        # behind, a prefix: the next run copies it
        code, rows = self.rows()
        self.assertEqual(code, 0)
        self.assertEqual(rows[("session archive", "archive check")]["status"], "ok")
        self.assertNotIn(("session archive", "archive copies"), rows)
        cut = self.h.archive / "projects" / "-work-a" / ("%s.jsonl" % sid(1))
        cut.write_text("{}\n")                                               # shorter, and not the live file's start
        code, rows = self.rows()
        self.assertEqual(code, 0)
        row = rows[("session archive", "archive copies")]
        self.assertEqual(row["status"], "warn")
        self.assertIn("1 archive copy is shorter than its live transcript and differs from its start", row["detail"])

    def test_the_archive_job_running_late(self):
        self.healthy()
        self.h.archive_run(ago=3 * 3600)
        bad = self.failing()[1]
        self.assertEqual(list(bad), [("session archive", "archive check")])
        self.assertIn("the last archive run was 3", bad[("session archive", "archive check")]["detail"])

    def test_a_missing_folder(self):
        self.healthy()
        os.rmdir(self.h.root / "data" / "fleetwatch")
        self.assertEqual(list(self.failing()[1]), [("data folder", "exists")])

    def test_fix_makes_only_the_safe_repairs(self):
        self.healthy()
        st = self.h.launchd()
        del st["loaded"]["local.fleetwatch.watch-run"]
        st["running"] = []
        self.h.state_file.write_text(json.dumps(st))
        (self.h.agents / "local.fleetwatch.archive.plist").unlink()
        legacy = self.h.legacy(LEGACY_INDEX)
        os.rmdir(self.h.root / "data" / "fleetwatch")
        self.h.job_logs(ago=3 * 3600)                             # stale: reported, not repaired
        self.h.transcript("-work-b/%s.jsonl" % sid(3), age=3600)          # uncovered: reported, not repaired
        code, out = self.doctor("--fix")
        self.assertEqual(code, 1)
        self.assertIn("fixed: data folder, %s, watch run, notify, archive" % LEGACY_INDEX, out)
        self.assertFalse(legacy.exists())
        self.assertTrue((self.h.state / "legacy" / (LEGACY_INDEX + ".plist")).exists())
        bad = self.failing()[1]
        self.assertEqual(sorted(bad), [("index refresh", "last run"), ("session archive", "archive check"),
                                       ("watch run", "last run")])
        # only the archive, whose plist went missing while it stayed loaded, is reloaded
        self.assertEqual([c for c in self.h.calls() if c.startswith("bootout gui/501/local.fleetwatch")],
                         ["bootout gui/501/local.fleetwatch.archive"])

    def test_fix_leaves_a_duplicate_alone(self):
        self.healthy()
        plist = plistlib.loads((self.h.agents / "local.fleetwatch.watch-run.plist").read_bytes())
        plist["Label"] = "com.example.copy"
        path = self.h.agents / "com.example.copy.plist"
        path.write_bytes(plistlib.dumps(plist))
        st = self.h.launchd()
        st["loaded"]["com.example.copy"] = str(path)
        self.h.state_file.write_text(json.dumps(st))
        n = len(self.h.calls())
        code, out = self.doctor("--fix")
        self.assertEqual(code, 1)
        self.assertIn("fixed: nothing", out)
        self.assertEqual(self.h.calls()[n:], [])
        self.assertTrue(path.exists())

    def test_fix_with_json_keeps_stdout_json(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code, out = self.doctor("--fix", "--json")
        d = json.loads(out)
        self.assertIn("watch run", d["fixed"])
        self.assertIn("loaded local.fleetwatch.watch-run", stderr.getvalue())


# ---------------------------------------------------------------- index failure streaks

class Streaks(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        self.now = time.time()

    def index(self, name, ok=False, error="engine build failed: exit 3\nTraceback line 2", streak=None):
        state = self.root / name / "state"
        state.mkdir(parents=True)
        from datetime import datetime, timezone
        info = {"ok": ok, "finished": datetime.fromtimestamp(self.now - 60, timezone.utc).isoformat()}
        if error and not ok:
            info["error"] = error
        (state / "last_run.json").write_text(json.dumps(info))
        if streak is not None:
            f = self.root / "logs" / "streaks" / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(streak)

    def load(self, names):
        reg = {"defaults": {"schedule_minutes": 30}, "indexes": {n: {"group": "team"} for n in names}}
        return index_health.load(reg, self.root, True, index_health.Cache())

    def test_streak_file_formats(self):
        self.index("a", streak="4")
        self.index("b", streak="2 1700000000")
        self.index("c", streak="junk")
        self.assertEqual(index_health.read_streak(self.root, "a"), (4, None))
        self.assertEqual(index_health.read_streak(self.root, "b"), (2, 1700000000.0))
        self.assertEqual(index_health.read_streak(self.root, "c"), (0, None))
        self.assertEqual(index_health.read_streak(self.root, "none"), (0, None))

    def test_the_limits(self):
        h = index_health.Health("x", "team", "session", "failed", "build", finished=self.now)
        cases = [(2, self.now - 60, ""), (3, self.now - 60, "failed 3 runs in a row over 60s"),
                 (1, self.now - 6 * 3600, "failed 1 run in a row over 6.0h"), (2, None, "")]
        for runs, since, want in cases:
            got = index_health.Health(**{**h.__dict__, "streak": runs, "streak_since": since}).streak_problem(self.now)
            self.assertEqual(got, want, (runs, since))
        ok = index_health.Health(**{**h.__dict__, "state": "ok", "streak": 9})
        self.assertEqual(ok.streak_problem(self.now), "")         # a manual refresh that worked ends the alert

    def test_settings_change_the_limits_and_a_bad_value_keeps_the_default(self):
        self.assertEqual(index_health.streak_limits({"alerts": {"index_failure_runs": 5, "index_failure_hours": 1}}),
                         (5, 1.0))
        self.assertEqual(index_health.streak_limits({"alerts": {"index_failure_runs": 0, "index_failure_hours": "x"}}),
                         (3, 6.0))
        self.assertEqual(index_health.streak_limits({"alerts": "nope"}), (3, 6.0))

    def test_record_streak_keeps_when_the_streak_began(self):
        from fleetwatch.memory import refresh
        log = self.root / "logs" / "failures.log"
        log.parent.mkdir(parents=True)
        with mock.patch.object(refresh, "ROOT", self.root), mock.patch.object(refresh, "FAILURES_LOG", log):
            refresh.record_streak("a", "boom")
            runs, since = index_health.read_streak(self.root, "a")
            self.assertEqual(runs, 1)
            self.assertAlmostEqual(since, time.time(), delta=5)
            os.utime(self.root / "logs" / "streaks" / "a", (0, 0))
            refresh.record_streak("a", "boom")
            self.assertEqual(index_health.read_streak(self.root, "a"), (2, since))
            refresh.record_streak("a", None)
            self.assertEqual(index_health.read_streak(self.root, "a"), (0, None))
            (self.root / "logs" / "streaks" / "b").write_text("4")    # a file from before streaks kept a start
            refresh.record_streak("b", "boom")
            self.assertEqual((self.root / "logs" / "streaks" / "b").read_text(), "5")
        self.assertIn("recovered after 2 failed run(s)", log.read_text())

    def test_a_streak_shows_on_needs_you(self):
        self.index("alpha", streak="4 %d" % (self.now - 3 * 3600))
        self.index("beta", streak="1 %d" % self.now)
        rows = self.load(["alpha", "beta"]).rows
        group = views.index_problem_group(rows, self.now)
        heads = {k.index: k.head.plain for k in group.children}
        self.assertIn("failed 4 runs in a row over 3.0h", heads["alpha"])
        self.assertNotIn("in a row", heads["beta"])
        alpha = next(r for r in rows if r.name == "alpha")
        self.assertEqual(alpha.next_commands(self.now), ["fleetwatch index status alpha", "fleetwatch doctor"])
        self.assertNotIn("Traceback", " ".join(heads.values()))

    def test_doctor_names_the_streak_with_the_first_error_line_only(self):
        from fleetwatch.memory import refresh
        self.index("alpha", streak="4 %d" % (self.now - 3 * 3600))
        self.index("beta", streak="1 %d" % self.now)
        res = self.load(["alpha", "beta"])
        ctx = deps.Context(services.Manager("darwin", mock.Mock(), {"FLEETWATCH_LAUNCH_AGENTS_DIR": "/nowhere"}),
                           io.StringIO(), now=self.now)
        with mock.patch.object(index_health, "load", return_value=res), mock.patch.object(refresh, "ROOT", self.root):
            rows = deps.index_checks(ctx)
        self.assertEqual([(r.item, r.status) for r in rows], [("index alpha", "fail")])
        self.assertEqual(rows[0].detail, "failed 4 runs in a row over 3.0h: engine build failed: exit 3")
        with mock.patch.object(index_health, "load", return_value=self.load(["beta"])):
            self.assertEqual([r.status for r in deps.index_checks(ctx)], ["ok"])

    def test_notify_sends_a_streak_once(self):
        def items(streak):
            f = self.root / "logs" / "streaks" / "alpha"
            if streak is None:
                f.unlink(missing_ok=True)
            else:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(streak)
            with mock.patch.object(index_health, "load", return_value=self.load(["alpha"])):
                return notify.index_items(self.now)
        self.index("alpha")
        cfg = notify.NotifySettings(True, notify.KINDS, False, None, 20)
        sent = []
        backend = type("B", (), {"send": lambda _s, m: sent.append(m) or True})()
        n = notify.Notifier(cfg, backend)
        t = self.now
        n.tick(items("1 %d" % t), t)                                  # the first poll records silently
        self.assertEqual(sent, [])
        out = n.tick(items("3 %d" % t), t + 1800)
        self.assertEqual([m.text for m in out], ["Index alpha has failed 3 runs in a row"])
        for runs in (4, 5, 6):                                        # the same streak runs on: nothing new
            self.assertEqual(n.tick(items("%d %d" % (runs, t)), t + 1800 * runs), [])
        self.assertEqual(n.tick(items(None), t + 1800 * 7), [])        # it ended
        again = n.tick(items("3 %d" % (t + 1800 * 8)), t + 1800 * 10)  # a new streak is sent again
        self.assertEqual(len(again), 1)
        self.assertIn("3 runs in a row", again[0].text)
        self.assertIsNone(again[0].click)


# ---------------------------------------------------------------- the inventory and its page

class Inventory(unittest.TestCase):
    def test_every_entry_has_what_the_inventory_promises(self):
        for e in deps.INVENTORY:
            self.assertIn(e.kind, ("job", "folder", "tool"), e)
            self.assertTrue(e.name and e.purpose, e)
            self.assertTrue(callable(e.install) and callable(e.check), e)
        legacy = {label: j.name for j in deps.JOBS for label in j.legacy}
        self.assertEqual(legacy, {LEGACY_INDEX: "index refresh", LEGACY_ARCHIVE: "archive"})
        self.assertEqual({j.label for j in deps.JOBS}, set(OURS))

    def test_the_archive_job_follows_the_contract(self):
        self.assertEqual((deps.ARCHIVE.label, deps.ARCHIVE.args, deps.ARCHIVE.interval_s),
                         ("local.fleetwatch.archive", ("archive", "run"), 3600))

    def test_docs_page_is_in_step(self):
        page = (REPO / "docs" / "dependencies.md").read_text(encoding="utf-8")
        self.assertEqual(page, deps.markdown(),
                         "regenerate it: python -m fleetwatch doctor --inventory > docs/dependencies.md")

    def test_signature_ignores_the_interpreter(self):
        a = services.signature(["/usr/bin/python3", "-P", "-m", "fleetwatch", "index", "refresh", "--all"])
        b = services.signature(["/opt/venv/bin/python", "-m", "fleetwatch", "index", "refresh", "--all"])
        self.assertEqual(a, b)
        self.assertEqual(services.signature(["/usr/bin/python3", "/x/refresh_all.py"]), ("refresh_all.py",))


if __name__ == "__main__":
    unittest.main()
