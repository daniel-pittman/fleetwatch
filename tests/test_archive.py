"""fleetwatch archive and fleetwatch restore: copying without deleting or overwriting, the lock, rsync and the
built-in copy, adopting the legacy archiver, status, and restoring archive-only transcripts."""
import contextlib
import fcntl
import io
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fleetwatch import __main__ as cli, archive, data, restore, resume, schedule, settings  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = shlex.join([sys.executable, os.path.join(HERE, "fake_launchctl.py")])
HOUR = 3600


def sid():
    return str(uuid.uuid4())


class Home(unittest.TestCase):
    """A temp home: a projects folder, an archive, fleetwatch's config and state, LaunchAgents and a
    fake launchctl. Nothing here reaches the real home folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.claude = self.mk("home/.claude")
        self.projects = self.mk("home/.claude/projects")
        self.dest = os.path.join(self.root, "archive")
        self.agents = self.mk("LaunchAgents")
        self.loaded = os.path.join(self.root, "launchd")
        env = {"HOME": os.path.join(self.root, "home"), "FLEETWATCH_CONFIG_DIR": self.mk("cfg"),
               "XDG_STATE_HOME": self.mk("state"), "FLEETWATCH_PROJECTS_DIR": self.projects,
               "CLAUDE_SESSION_ARCHIVE_DEST": self.dest, "FLEETWATCH_LAUNCH_AGENTS_DIR": self.agents,
               "FLEETWATCH_SYSTEMD_USER_DIR": self.mk("systemd"), "FLEETWATCH_LAUNCHCTL": FAKE,
               "FAKE_LAUNCHCTL_DIR": self.loaded}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        for k in ("FLEETWATCH_ARCHIVE_DIR", "FLEETWATCH_RSYNC", "FAKE_LAUNCHCTL_FAIL"):
            os.environ.pop(k, None)
        self.paths = data.default_paths()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def mk(self, rel):
        path = os.path.join(self.root, rel)
        os.makedirs(path, exist_ok=True)
        return path

    def write(self, path, text, mtime=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        mtime = mtime or time.time() - HOUR
        os.utime(path, (mtime, mtime))
        return path

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def transcript(self, root, slug="-proj", session=None, title=None, cwd=None, mtime=None, extra=""):
        session = session or sid()
        lines = [{"type": "user", "cwd": cwd or self.root, "entrypoint": "cli", "sessionId": session}]
        if title:
            lines.append({"type": "custom-title", "customTitle": title, "sessionId": session})
        body = "".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines) + extra
        return session, self.write(os.path.join(root, slug, session + ".jsonl"), body, mtime)

    def live(self, **kw):
        return self.transcript(self.projects, **kw)

    def kept(self, **kw):
        return self.transcript(os.path.join(self.dest, "projects"), **kw)

    def quiet(self, fn, *a, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fn(*a, **kw)
        return code, out.getvalue()


class RunCases:
    """Archive-run behavior, run once with the built-in copy and once with rsync."""
    METHOD = None

    def run_archive(self):
        return archive.run(self.paths, self.METHOD, io.StringIO())

    def a(self, *parts):
        return os.path.join(self.dest, "projects", *parts)

    def test_copies_transcripts_side_files_and_history_and_nothing_else(self):
        s, path = self.live(title="One")
        self.write(os.path.join(self.projects, "-proj", s, "subagents", "agent-a.jsonl"), "sub\n")
        self.write(os.path.join(self.projects, "-proj", s, "tool-results", "r1.txt"), "result\n")
        self.write(os.path.join(self.projects, "-proj", "agent-old.jsonl"), "older layout\n")
        self.write(os.path.join(self.projects, "-proj", s + ".jsonl.tmp.123"), "half\n")
        self.write(os.path.join(self.projects, "-proj", "memory", "MEMORY.md"), "not a transcript\n")
        self.write(os.path.join(self.claude, "history.jsonl"), "h1\n")
        record = self.run_archive()
        self.assertTrue(record["ok"], record)
        self.assertEqual(record["method"], self.METHOD)
        self.assertEqual(self.read(self.a("-proj", s + ".jsonl")), self.read(path))
        self.assertEqual(int(os.path.getmtime(self.a("-proj", s + ".jsonl"))), int(os.path.getmtime(path)))
        self.assertEqual(self.read(self.a("-proj", s, "subagents", "agent-a.jsonl")), "sub\n")
        self.assertEqual(self.read(self.a("-proj", s, "tool-results", "r1.txt")), "result\n")
        self.assertTrue(os.path.exists(self.a("-proj", "agent-old.jsonl")))
        self.assertFalse(os.path.exists(self.a("-proj", s + ".jsonl.tmp.123")))
        self.assertFalse(os.path.exists(self.a("-proj", "memory")))
        self.assertEqual(self.read(os.path.join(self.dest, "history.jsonl")), "h1\n")
        self.assertEqual(record["copied"], 5)
        self.assertEqual((record["transcripts_live"], record["transcripts_archived"]), (1, 1))

    def test_a_deleted_transcript_stays_in_the_archive(self):
        s, path = self.live()
        other, other_path = self.live()
        side = self.write(os.path.join(self.projects, "-proj", s, "tool-results", "r.txt"), "r\n")
        self.run_archive()
        os.unlink(path)
        shutil.rmtree(os.path.dirname(side))
        # A changed file in the same folder, so the second run copies something there.
        self.write(other_path, self.read(other_path) + "more\n", time.time())
        record = self.run_archive()
        self.assertTrue(record["ok"], record)
        self.assertEqual(record["copied"], 1)
        self.assertTrue(os.path.exists(self.a("-proj", s + ".jsonl")))
        self.assertTrue(os.path.exists(self.a("-proj", s, "tool-results", "r.txt")))
        self.assertEqual((record["transcripts_live"], record["transcripts_archived"]), (1, 2))

    def test_an_older_or_smaller_file_never_replaces_the_archive_copy(self):
        now = time.time()
        s, path = self.live(mtime=now - 2 * HOUR, extra="grown\n" * 50)
        self.run_archive()
        kept = self.read(self.a("-proj", s + ".jsonl"))
        # Smaller and newer: refused.
        self.write(path, "cut\n", now - HOUR)
        self.run_archive()
        self.assertEqual(self.read(self.a("-proj", s + ".jsonl")), kept)
        # Larger but older than the archive copy: refused.
        self.write(path, kept + "more\n", now - 3 * HOUR)
        self.run_archive()
        self.assertEqual(self.read(self.a("-proj", s + ".jsonl")), kept)
        # Larger and newer: copied.
        self.write(path, kept + "more\n", now - 60)
        record = self.run_archive()
        self.assertEqual(self.read(self.a("-proj", s + ".jsonl")), kept + "more\n")
        self.assertEqual(record["copied"], 1)

    def test_an_unchanged_tree_copies_nothing(self):
        s, _ = self.live(mtime=time.time() - HOUR + 0.75)
        self.write(os.path.join(self.projects, "-proj", s, "tool-results", "r.txt"), "r\n", time.time() - 0.25)
        self.write(os.path.join(self.claude, "history.jsonl"), "h\n")
        self.assertEqual(self.run_archive()["copied"], 3)
        self.assertEqual(self.run_archive()["copied"], 0)

    def test_the_record_and_the_log_line(self):
        self.live()
        record = self.run_archive()
        on_disk = json.loads(self.read(str(archive.state_path())))
        self.assertEqual(on_disk, record)
        for key in archive.STATE_KEYS:
            self.assertIn(key, on_disk)
        self.assertIsNone(on_disk["error"])
        self.assertLessEqual(on_disk["started"], on_disk["ended"])
        log = self.read(str(archive.log_path()))
        self.assertIn("start: ", log)
        self.assertIn("done: copied 1, 1 transcripts live, 1 archived", log)


class PythonCopy(RunCases, Home):
    METHOD = "python"


class RsyncCases(RunCases):
    METHOD = "rsync"
    RSYNC = None

    def setUp(self):
        super().setUp()
        if not self.RSYNC:
            self.skipTest("no such rsync here")
        os.environ["FLEETWATCH_RSYNC"] = self.RSYNC


class RsyncCopy(RsyncCases, Home):
    RSYNC = shutil.which("rsync")


class SystemRsyncCopy(RsyncCases, Home):
    """macOS's /usr/bin/rsync is openrsync, which sets whole-second times."""
    RSYNC = "/usr/bin/rsync" if os.access("/usr/bin/rsync", os.X_OK) and \
        os.path.realpath(shutil.which("rsync") or "") != "/usr/bin/rsync" else None


class RunEdges(Home):
    def test_rsync_is_used_when_present_and_can_be_turned_off(self):
        os.environ["FLEETWATCH_RSYNC"] = "/opt/rsync-stand-in --flag"
        self.assertEqual(archive.rsync_command(), ["/opt/rsync-stand-in", "--flag"])
        os.environ["FLEETWATCH_RSYNC"] = ""
        self.assertIsNone(archive.rsync_command())
        self.live()
        self.assertEqual(archive.run(self.paths, None, io.StringIO())["method"], "python")

    def test_rsync_is_never_asked_to_delete_or_to_write_in_place(self):
        self.live()
        os.environ["FLEETWATCH_RSYNC"] = "rsync"
        with mock.patch.object(archive.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertTrue(archive.run(self.paths, None, io.StringIO())["ok"])
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["rsync", "-a", "--update"])
        for flag in argv:
            self.assertFalse(flag.startswith(("--del", "--remove", "--partial", "--inplace", "--append", "-r")),
                             flag)

    def test_a_failed_rsync_is_a_failed_run_and_exits_1(self):
        self.live()
        os.environ["FLEETWATCH_RSYNC"] = shutil.which("false")
        code, out = self.quiet(archive.main, ["run"])
        self.assertEqual(code, 1)
        record = archive.load_state()
        self.assertFalse(record["ok"])
        self.assertIn("rsync exited 1", record["error"])
        self.assertIn("failed: rsync exited 1", self.read(str(archive.log_path())))

    def test_rsync_exit_24_is_vanished_files_not_a_failure(self):
        self.live()
        stand_in = self.write(os.path.join(self.root, "rsync24"), "#!/bin/sh\nexit 24\n")
        os.chmod(stand_in, 0o755)
        os.environ["FLEETWATCH_RSYNC"] = stand_in
        self.assertTrue(archive.run(self.paths, None, io.StringIO())["ok"])

    def test_a_missing_projects_folder_is_a_failed_run(self):
        shutil.rmtree(self.projects)
        record = archive.run(self.paths, "python", io.StringIO())
        self.assertFalse(record["ok"])
        self.assertIn("does not exist", record["error"])

    def test_a_second_run_exits_while_the_lock_is_held(self):
        self.live()
        os.makedirs(archive.state_dir(), exist_ok=True)
        with open(archive.lock_path(), "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            out = io.StringIO()
            self.assertIsNone(archive.run(self.paths, "python", out))
            code, _ = self.quiet(archive.main, ["run", "--python"])
        self.assertEqual(code, 0)
        self.assertIn("in progress", out.getvalue())
        self.assertFalse(os.path.exists(archive.state_path()))
        self.assertFalse(os.path.exists(os.path.join(self.dest, "projects", "-proj")))
        self.assertIn("another run holds", self.read(str(archive.log_path())))
        self.assertTrue(archive.run(self.paths, "python", io.StringIO())["ok"])

    def test_a_shrunk_file_during_the_copy_never_replaces_the_archive_copy(self):
        s, path = self.live(extra="x" * 500)
        archive.run(self.paths, "python", io.StringIO())
        kept = self.read(os.path.join(self.dest, "projects", "-proj", s + ".jsonl"))
        self.write(path, kept + "more\n", time.time())
        real = shutil.copy2

        def shrinking(src, dst):
            real(src, dst)
            with open(dst, "w") as fh:
                fh.write("cut\n")
            return dst

        with mock.patch.object(archive.shutil, "copy2", shrinking):
            archive.run(self.paths, "python", io.StringIO())
        self.assertEqual(self.read(os.path.join(self.dest, "projects", "-proj", s + ".jsonl")), kept)
        self.assertEqual([n for n in os.listdir(os.path.join(self.dest, "projects", "-proj")) if n.startswith(".")],
                         [])


class Destination(Home):
    def test_the_env_then_the_setting_then_the_default(self):
        self.assertEqual(settings.archive_root(), self.dest)
        self.assertEqual(data.default_paths()["archive"], os.path.join(self.dest, "projects"))
        del os.environ["CLAUDE_SESSION_ARCHIVE_DEST"]
        with open(os.path.join(os.environ["FLEETWATCH_CONFIG_DIR"], "settings.toml"), "w") as fh:
            fh.write('[archive]\ndest = "%s"\n' % os.path.join(self.root, "elsewhere"))
        self.assertEqual(data.default_paths()["archive"], os.path.join(self.root, "elsewhere", "projects"))
        os.unlink(os.path.join(os.environ["FLEETWATCH_CONFIG_DIR"], "settings.toml"))
        self.assertTrue(settings.archive_root().endswith(
            os.path.join(".local", "share", "claude-session-archive", "archive")))
        os.environ["FLEETWATCH_ARCHIVE_DIR"] = os.path.join(self.root, "explicit")
        self.assertEqual(data.default_paths()["archive"], os.path.join(self.root, "explicit"))

    def test_an_existing_archive_is_adopted_in_place(self):
        s, old = self.kept(title="Kept From Before", mtime=time.time() - 5 * HOUR)
        before = self.read(old)
        self.live()
        archive.run(self.paths, "python", io.StringIO())
        self.assertEqual(self.read(old), before)
        self.assertEqual(len(os.listdir(os.path.join(self.dest, "projects", "-proj"))), 2)

    def test_the_job_carries_the_archive_location(self):
        plist = schedule.plist_dict(self.root, schedule.ARCHIVE)
        self.assertEqual(plist["Label"], "local.fleetwatch.archive")
        self.assertEqual(plist["StartInterval"], 3600)
        self.assertEqual(plist["ProgramArguments"][-3:], ["fleetwatch", "archive", "run"])
        self.assertEqual(plist["EnvironmentVariables"]["CLAUDE_SESSION_ARCHIVE_DEST"], self.dest)
        self.assertEqual(plist["ProcessType"], "Background")


class Status(Home):
    def test_never_run(self):
        self.live()
        result = archive.check(self.paths)
        self.assertFalse(result["ok"])
        self.assertEqual(result["problems"], ["the archive job has never run"])
        self.assertEqual(result["status"]["coverage"]["missing"], 1)

    def test_clean_after_a_run_and_json_status(self):
        self.live()
        archive.run(self.paths, "python", io.StringIO())
        self.assertEqual(archive.check(self.paths), {"ok": True, "problems": [], "status": mock.ANY})
        with mock.patch.object(archive.sys, "platform", "linux"):
            code, out = self.quiet(archive.main, ["status", "--json"])
        doc = json.loads(out)
        self.assertEqual(code, 0)
        for key in archive.STATE_KEYS + ("age_s", "coverage", "archive"):
            self.assertIn(key, doc)
        self.assertEqual(doc["coverage"], {"live": 1, "archived": 1, "missing": 0, "missing_before": 0,
                                           "behind": 0})

    def test_a_transcript_the_last_run_missed_a_failed_run_and_a_stale_run(self):
        archive.run(self.paths, "python", io.StringIO())
        self.live(mtime=time.time() - 5 * HOUR)
        self.live(mtime=time.time() + 5)  # newer than the run: not yet expected in the archive
        problems = archive.check(self.paths)["problems"]
        self.assertEqual(problems, ["1 live transcript with no archive copy"])
        record = archive.load_state()
        record.update(ok=False, error="rsync exited 23: some files\nmore", ended=time.time() - 3 * HOUR)
        with open(archive.state_path(), "w") as fh:
            json.dump(record, fh)
        problems = archive.check(self.paths)["problems"]
        self.assertIn("the last archive run failed: rsync exited 23: some files", problems)
        self.assertIn("the last archive run was 3 h ago (it runs every 60 min)", problems)

    def test_the_text_status(self):
        self.live()
        archive.run(self.paths, "python", io.StringIO())
        open(os.path.join(self.mk("launchd"), archive.LEGACY_LABEL), "w").close()
        with mock.patch.object(archive.sys, "platform", "darwin"):
            code, out = self.quiet(archive.main, ["status"])
        self.assertEqual(code, 0)
        self.assertIn("last run: ", out)
        self.assertIn("coverage: 0 of 1 live transcripts have no archive copy", out)
        self.assertIn("job local.fleetwatch.archive: not loaded", out)
        self.assertIn("legacy archiver local.claude-session-archive is still loaded", out)


class Install(Home):
    """Install and uninstall on macOS with a fake launchctl, and the legacy archiver's retirement."""

    LEGACY = {"Label": archive.LEGACY_LABEL, "ProgramArguments": ["/bin/bash", "archive.sh"], "StartInterval": 3600}

    def setUp(self):
        super().setUp()
        self.platform = mock.patch.object(sys, "platform", "darwin")
        self.platform.start()

    def tearDown(self):
        self.platform.stop()
        super().tearDown()

    def legacy(self, loaded=True):
        path = os.path.join(self.agents, archive.LEGACY_LABEL + ".plist")
        with open(path, "wb") as fh:
            plistlib.dump(self.LEGACY, fh)
        if loaded:
            os.makedirs(self.loaded, exist_ok=True)
            open(os.path.join(self.loaded, archive.LEGACY_LABEL), "w").close()
        return path

    def is_loaded(self, label):
        return os.path.exists(os.path.join(self.loaded, label))

    def backups(self):
        folder = schedule.legacy_backup_dir()
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []

    def test_install_loads_the_job_then_retires_the_legacy_archiver_keeping_its_plist(self):
        path = self.legacy()
        code, out = self.quiet(archive.main, ["install"])
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.agents, "local.fleetwatch.archive.plist")))
        self.assertTrue(self.is_loaded("local.fleetwatch.archive"))
        self.assertFalse(self.is_loaded(archive.LEGACY_LABEL))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.backups(), [archive.LEGACY_LABEL + ".plist"])
        with open(schedule.legacy_backup_dir() / (archive.LEGACY_LABEL + ".plist"), "rb") as fh:
            self.assertEqual(plistlib.load(fh), self.LEGACY)
        self.assertIn("loaded local.fleetwatch.archive", out)
        self.assertIn("unloaded the legacy agent local.claude-session-archive", out)
        self.assertIn("kept as a backup", out)
        self.assertNotIn("not loaded", out)
        calls = self.read(os.path.join(self.loaded, "calls.log")).splitlines()
        first_bootout = next(i for i, c in enumerate(calls) if c.startswith("bootout") and archive.LEGACY_LABEL in c)
        self.assertLess(next(i for i, c in enumerate(calls) if c.startswith("bootstrap")), first_bootout)

    def test_a_second_install_keeps_both_backups_and_reloads_the_job(self):
        self.legacy()
        self.quiet(archive.main, ["install"])
        self.legacy()
        code, out = self.quiet(archive.main, ["install"])
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.backups()), 2)
        self.assertTrue(self.is_loaded("local.fleetwatch.archive"))

    def test_a_failed_load_leaves_the_legacy_archiver_running(self):
        path = self.legacy()
        os.environ["FAKE_LAUNCHCTL_FAIL"] = "bootstrap"
        code, out = self.quiet(archive.main, ["install"])
        self.assertEqual(code, 1)
        self.assertTrue(self.is_loaded(archive.LEGACY_LABEL))
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.backups(), [])
        self.assertIn("could not load local.fleetwatch.archive", out)
        self.assertIn("left as it was", out)

    def test_a_failed_unload_keeps_the_legacy_plist_in_place(self):
        path = self.legacy()
        schedule.install(io.StringIO(), schedule.ARCHIVE)
        os.environ["FAKE_LAUNCHCTL_FAIL"] = "bootout"
        ok, actions = schedule.retire_legacy(archive.LEGACY_LABEL, io.StringIO())
        self.assertFalse(ok)
        self.assertEqual(actions, [])
        self.assertTrue(os.path.exists(path))

    def test_an_unloaded_legacy_plist_is_still_moved_aside(self):
        path = self.legacy(loaded=False)
        code, out = self.quiet(archive.main, ["install"])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.backups(), [archive.LEGACY_LABEL + ".plist"])

    def test_no_legacy_archiver(self):
        code, out = self.quiet(archive.main, ["install"])
        self.assertEqual(code, 0)
        self.assertIn("no legacy archiver (local.claude-session-archive) to retire", out)

    def test_a_dry_run_changes_nothing(self):
        path = self.legacy()
        out = io.StringIO()
        ok, actions = schedule.retire_legacy(archive.LEGACY_LABEL, out, dry_run=True)
        self.assertTrue(ok)
        self.assertEqual(len(actions), 2)
        self.assertIn("would unload the legacy agent", out.getvalue())
        self.assertTrue(self.is_loaded(archive.LEGACY_LABEL))
        self.assertTrue(os.path.exists(path))

    def test_uninstall_unloads_and_removes_the_job_and_keeps_the_archive(self):
        self.live()
        archive.run(self.paths, "python", io.StringIO())
        self.quiet(archive.main, ["install"])
        code, out = self.quiet(archive.main, ["uninstall"])
        self.assertEqual(code, 0, out)
        self.assertFalse(self.is_loaded("local.fleetwatch.archive"))
        self.assertFalse(os.path.exists(os.path.join(self.agents, "local.fleetwatch.archive.plist")))
        self.assertEqual(len(os.listdir(os.path.join(self.dest, "projects", "-proj"))), 1)
        self.assertIn("keeps its copies", out)
        code, out = self.quiet(archive.main, ["uninstall"])
        self.assertEqual(code, 0)
        self.assertIn("not installed", out)

    def test_a_trial_folder_without_a_stand_in_never_runs_the_real_launchctl(self):
        path = self.legacy()
        del os.environ["FLEETWATCH_LAUNCHCTL"]
        with mock.patch.object(schedule.subprocess, "run") as run:
            code, out = self.quiet(archive.main, ["install"])
            self.assertFalse(schedule.is_loaded("local.fleetwatch.archive"))
            ok, actions = schedule.retire_legacy(archive.LEGACY_LABEL, io.StringIO(), dry_run=True)
            self.assertEqual(self.quiet(archive.main, ["uninstall"])[0], 0)
        run.assert_not_called()
        self.assertEqual(code, 1)
        self.assertIn("not run (a trial folder is set", out)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.backups(), [])

    def test_linux_writes_a_timer_and_never_calls_launchctl(self):
        self.platform.stop()
        with mock.patch.object(sys, "platform", "linux"):
            code, out = self.quiet(archive.main, ["install"])
            self.assertEqual(code, 0)
            timer = os.path.join(os.environ["FLEETWATCH_SYSTEMD_USER_DIR"], "fleetwatch-archive.timer")
            self.assertIn("OnUnitActiveSec=60min", self.read(timer))
            self.assertIn("systemctl --user enable", out)
            code, out = self.quiet(archive.main, ["uninstall"])
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(timer))
        self.platform.start()
        self.assertFalse(os.path.exists(os.path.join(self.loaded, "calls.log")))


class Restore(Home):
    def setUp(self):
        super().setUp()
        self.paths["archive"] = os.path.join(self.dest, "projects")
        self.work = self.mk("work")

    def restore(self, *argv):
        out = io.StringIO()
        code = restore.main(list(argv), out)
        return code, out.getvalue()

    def back(self, slug, s):
        return os.path.join(self.projects, slug, s + ".jsonl")

    def test_by_name_with_side_files_and_the_archive_keeps_its_copy(self):
        s, src = self.kept(slug="-work-app", title="Billing Fix", cwd=self.work)
        self.write(os.path.join(self.dest, "projects", "-work-app", s, "subagents", "agent-1.jsonl"), "sub\n")
        code, out = self.restore("billing fix")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read(self.back("-work-app", s)), self.read(src))
        self.assertTrue(os.path.exists(os.path.join(self.projects, "-work-app", s, "subagents", "agent-1.jsonl")))
        self.assertTrue(os.path.exists(src))
        self.assertIn("restored Billing Fix to", out)
        self.assertIn("to resume it: cd %s && claude --resume %s" % (self.work, s), out)

    def test_an_unnamed_session_by_id_prefix(self):
        s, _ = self.kept(slug="-work-app", cwd=self.work)
        code, out = self.restore(s[:8])
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(self.back("-work-app", s)))
        self.assertIn("unnamed session %s" % s[:8], out)

    def test_by_project_folder_name(self):
        s, _ = self.kept(slug="-Users-x-git-tools-gadget", title="Something Else")
        self.kept(slug="-Users-x-git-other")
        code, out = self.restore("gadget")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(self.back("-Users-x-git-tools-gadget", s)))

    def test_an_ambiguous_match_lists_and_copies_nothing(self):
        a, _ = self.kept(slug="-one", title="Grant Report")
        b, _ = self.kept(slug="-two", title="Grant Budget")
        code, out = self.restore("grant")
        self.assertEqual(code, 2)
        self.assertIn("2 archived sessions match 'grant'", out)
        self.assertIn("Grant Report", out)
        self.assertIn("Grant Budget", out)
        self.assertFalse(os.path.exists(os.path.join(self.projects, "-one")))
        self.assertFalse(os.path.exists(os.path.join(self.projects, "-two")))

    def test_an_exact_name_beats_a_partial_one(self):
        a, _ = self.kept(slug="-one", title="Grant")
        self.kept(slug="-two", title="Grant Budget")
        self.assertEqual(self.restore("grant")[0], 0)
        self.assertTrue(os.path.exists(self.back("-one", a)))

    def test_side_files_with_no_main_transcript_are_reported_as_unrecoverable(self):
        lost = sid()
        self.write(os.path.join(self.projects, "-work-app", lost, "tool-results", "r.txt"), "r\n")
        self.write(os.path.join(self.dest, "projects", "-work-app", lost, "subagents", "a.jsonl"), "a\n")
        code, out = self.restore(lost[:8])
        self.assertEqual(code, 1)
        self.assertIn(restore.UNRECOVERABLE, out)
        self.assertIn("in the projects folder and the archive", out)
        code, out = self.restore("--list")
        self.assertEqual(code, 0)
        self.assertIn("cannot be restored (1):", out)
        self.assertIn(lost, out)
        code, out = self.restore("work-app")
        self.assertEqual(code, 1)
        self.assertIn(restore.UNRECOVERABLE, out)

    def test_side_files_beside_a_surviving_transcript_are_not_reported(self):
        s, _ = self.kept(slug="-p")
        self.write(os.path.join(self.projects, "-p", s, "tool-results", "r.txt"), "r\n")
        self.assertEqual(restore.orphans(self.paths), [])

    def test_never_overwrites_a_live_transcript_or_side_file(self):
        s, src = self.kept(slug="-p", title="Keep Mine")
        self.write(os.path.join(self.dest, "projects", "-p", s, "tool-results", "r.txt"), "archived\n")
        _, mine = self.live(slug="-p", session=s, title="Keep Mine", extra="live and newer\n")
        theirs = self.read(mine)
        side = self.write(os.path.join(self.projects, "-p", s, "tool-results", "r.txt"), "live side\n")
        dest, copied = resume.copy_back(src, s, self.paths)
        self.assertFalse(copied)
        self.assertEqual(dest, mine)
        self.assertEqual(self.read(mine), theirs)
        self.assertEqual(self.read(side), "live side\n")
        self.assertEqual(sorted(os.listdir(os.path.join(self.projects, "-p"))), sorted([s, s + ".jsonl"]))
        # Through the command, a session in the projects folder is not an archive-only one.
        code, out = self.restore("keep mine")
        self.assertEqual(code, 0)
        self.assertIn("already in the projects folder", out)
        self.assertEqual(self.read(mine), theirs)

    def test_list_shows_archive_only_sessions_with_title_folder_date_and_size(self):
        self.kept(slug="-p", title="Only Archived", cwd=self.work)
        s, _ = self.live(slug="-p", title="Still Live")
        self.kept(slug="-p", session=s, title="Still Live")
        code, out = self.restore("--list")
        self.assertEqual(code, 0)
        self.assertIn("in the archive only (1 session):", out)
        self.assertIn("Only Archived", out)
        self.assertNotIn("Still Live", out)
        self.assertIn(" B  ", out)
        self.assertIn(resume.short_folder(self.work), out)

    def test_no_match(self):
        self.kept(slug="-p", title="Something")
        code, out = self.restore("nothing like it")
        self.assertEqual(code, 1)
        self.assertIn("restore --list", out)

    def test_restore_never_starts_claude(self):
        self.kept(slug="-p", title="Quiet", cwd=self.work)
        with mock.patch.object(os, "execvp") as ex, mock.patch.object(resume, "open_new_window") as win:
            self.assertEqual(self.restore("quiet")[0], 0)
        ex.assert_not_called()
        win.assert_not_called()

    def test_resume_still_restores_through_the_same_path(self):
        s, src = self.kept(slug="-p", title="Back Again", cwd=self.work)
        entry = {"sid": s, "path": src, "archived": True}
        with mock.patch.object(resume, "copy_back", wraps=resume.copy_back) as shared:
            got = resume.restore_from_archive(entry, self.paths)
        shared.assert_called_once()
        self.assertEqual(got["path"], self.back("-p", s))
        self.assertFalse(got["archived"])


class CommandLine(Home):
    def test_the_subcommands_are_registered(self):
        self.live()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["archive", "run", "--python"]), 0)
            self.assertEqual(cli.main(["restore", "--list"]), 0)
        self.assertIn("done: copied 1", out.getvalue())
        p = subprocess.run([sys.executable, "-m", "fleetwatch", "--help"], capture_output=True, text=True,
                           cwd=os.path.join(HERE, ".."))
        self.assertIn("restore [NAME|ID]", p.stdout)
        self.assertIn("archive             run | install | uninstall | status", p.stdout)


if __name__ == "__main__":
    unittest.main()
