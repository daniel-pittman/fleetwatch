"""Overlapping index builds must not corrupt each other or resurrect removed rows: builds of one
index serialize on a writer lock, each writes its own temp file, a plain index build takes the
per-index lock, and only one `refresh --all` runs at a time."""
import argparse
import contextlib
import fcntl
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")

from fleetwatch.memory import refresh  # noqa: E402
from fleetwatch.search import engine  # noqa: E402
from test_search_library import GARDEN, make_index  # noqa: E402


def sleeper(test):
    """A live process the test owns, for a temp file whose builder is still running."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    test.addCleanup(p.wait)
    test.addCleanup(p.kill)
    return p.pid


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def indexed_paths(d, name="garden"):
    con = sqlite3.connect(d / "index" / ("%s.sqlite" % name))
    try:
        return [Path(r[0]).name for r in con.execute("SELECT path FROM chunks")]
    finally:
        con.close()


class BuildLock(unittest.TestCase):
    def test_an_older_build_never_republishes_a_removed_document(self):
        # A build that collected its corpus before a forget removed a file must not publish
        # after the forget's rebuild: the final index would hold the forgotten rows again.
        with tempfile.TemporaryDirectory() as tmp:
            d = make_index(tmp, "garden", GARDEN)
            real_collect = engine.collect_chunks
            collected, release = threading.Event(), threading.Event()
            errors, ids = [], {}

            def collect(cfg):
                out = real_collect(cfg)
                if threading.current_thread().name == "older":
                    collected.set()
                    release.wait(30)
                return out

            def pid():
                return ids.setdefault(threading.current_thread().name, 900000 + len(ids))

            def build():
                try:
                    engine.Index(d).build()
                except Exception as e:
                    errors.append(repr(e))

            with mock.patch.object(engine, "collect_chunks", collect), \
                    mock.patch.object(engine.os, "getpid", pid):
                older = threading.Thread(target=build, name="older")
                older.start()
                self.assertTrue(collected.wait(30))
                (d / "corpus" / "notes" / "bees.md").unlink()
                newer = threading.Thread(target=build, name="newer")
                newer.start()
                newer.join(3)
                release.set()
                older.join(60)
                newer.join(60)
            self.assertEqual(errors, [])
            self.assertNotIn("bees.md", indexed_paths(d))
            self.assertIn("tomatoes.md", indexed_paths(d))

    def test_a_build_waits_a_bounded_time_for_the_lock_then_says_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_index(tmp, "garden", GARDEN)
            (d / "index").mkdir()
            with open(d / "index" / "garden.sqlite.lock", "a") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                with mock.patch.object(engine, "BUILD_LOCK_WAIT_S", 0.3), \
                        mock.patch.object(engine, "collect_chunks") as collect, \
                        self.assertRaises(engine.BuildBusy) as cm:
                    engine.Index(d).build()
            collect.assert_not_called()
            self.assertIn("another build", str(cm.exception))
            self.assertFalse((d / "index" / "garden.sqlite").exists())


class TempFiles(unittest.TestCase):
    def test_a_failed_build_removes_its_own_pid_named_temp_file_and_no_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_index(tmp, "garden", GARDEN)
            idx = d / "index"
            idx.mkdir()
            legacy = idx / "garden.sqlite.tmp"     # an older release's fixed name, maybe mid-build
            live = idx / ("garden.sqlite.tmp.%d" % sleeper(self))
            for p in (legacy, live):
                p.write_text("another build's file")
            real_connect, opened = engine.sqlite3.connect, []

            def connect(path, *a, **k):
                opened.append(Path(path))
                return real_connect(path, *a, **k)
            with mock.patch.object(engine.sqlite3, "connect", connect), \
                    mock.patch.object(engine, "build_tfidf", side_effect=RuntimeError("boom")), \
                    self.assertRaises(RuntimeError):
                engine.Index(d).build()
            self.assertEqual(opened, [idx / ("garden.sqlite.tmp.%d" % os.getpid())])
            self.assertFalse(opened[0].exists())
            self.assertTrue(legacy.exists())
            self.assertTrue(live.exists())

    def test_a_build_removes_temp_files_left_by_dead_builds_and_keeps_live_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_index(tmp, "garden", GARDEN)
            idx = d / "index"
            idx.mkdir()
            gone = dead_pid()
            dead = [idx / ("garden.sqlite.tmp.%d" % gone), idx / ("garden.sqlite.tmp.%d-journal" % gone)]
            kept = [idx / ("garden.sqlite.tmp.%d" % sleeper(self)), idx / "garden.sqlite.tmp",
                    idx / ("other.sqlite.tmp.%d" % gone)]
            for p in dead + kept:
                p.write_text("left behind")
            engine.Index(d).build()
            for p in dead:
                self.assertFalse(p.exists(), p.name)
            for p in kept:
                self.assertTrue(p.exists(), p.name)
            self.assertIn("tomatoes.md", indexed_paths(d))

    def test_a_failed_swap_removes_its_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_index(tmp, "garden", GARDEN)
            with mock.patch.object(engine.os, "replace", side_effect=OSError("disk full")), \
                    self.assertRaises(OSError):
                engine.Index(d).build()
            self.assertEqual([p.name for p in (d / "index").iterdir() if ".tmp" in p.name], [])


class Locks(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name).resolve()
        p = mock.patch.object(refresh, "ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)
        self.entry = {"kind": "plain", "sources": [str(self.root / "src")],
                      "index_file": str(self.root / "idx" / "x.sqlite"),
                      "refresh": [["{python}", "-c", "pass"]]}
        (self.root / "src").mkdir()
        (self.root / "src" / "a.md").write_text("a")
        self.opts = argparse.Namespace(full=False, build=True)
        self.state = self.root / "plain" / "state"

    def hold(self, path, age=0):
        path.mkdir(parents=True)
        if age:
            old = os.stat(path).st_mtime - age
            os.utime(path, (old, old))

    def test_a_plain_build_skips_when_the_lock_is_held(self):
        self.hold(self.state / "refresh.lock.d")
        out = io.StringIO()
        with mock.patch.object(refresh, "run_bounded") as run, contextlib.redirect_stdout(out):
            self.assertEqual(refresh.run_plain("plain", self.entry, self.opts), refresh.SKIPPED)
        run.assert_not_called()
        self.assertIn("[plain] refresh already running; skipped", out.getvalue())
        self.assertFalse((self.state / "last_run.json").exists())
        self.assertFalse((self.state / "last_error.log").exists())
        self.assertTrue((self.state / "refresh.lock.d").exists())

    def test_a_session_index_skipped_for_its_lock_returns_skipped(self):
        state = self.root / "sess" / "state"
        self.hold(state / "refresh.lock.d")
        reg = {"defaults": {}, "indexes": {"sess": {}}}
        with mock.patch.object(refresh, "Index", lambda *a: mock.Mock(state=state)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(refresh.run_one(reg, "sess", self.opts), refresh.SKIPPED)

    def test_a_plain_build_takes_and_releases_the_lock(self):
        seen = []

        def run(argv, *a, **k):
            seen.append((self.state / "refresh.lock.d").exists())
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(refresh, "run_bounded", run), contextlib.redirect_stdout(io.StringIO()):
            self.assertIs(refresh.run_plain("plain", self.entry, self.opts), True)
        self.assertEqual(seen, [True])
        self.assertFalse((self.state / "refresh.lock.d").exists())

    def test_a_plain_build_touches_its_lock_before_each_command(self):
        self.entry["refresh"] = [["{python}", "-c", "pass"], ["{python}", "-c", "pass"]]
        events = []

        def run(argv, *a, **k):
            events.append("run")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(refresh, "run_bounded", run), \
                mock.patch.object(refresh.Lock, "touch", lambda self: events.append("touch")), \
                contextlib.redirect_stdout(io.StringIO()):
            refresh.run_plain("plain", self.entry, self.opts)
        self.assertEqual(events, ["touch", "run", "touch", "run"])

    def test_a_stale_plain_lock_does_not_block(self):
        self.hold(self.state / "refresh.lock.d", age=refresh.LOCK_STALE_S + 60)
        with mock.patch.object(refresh, "run_bounded",
                               return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIs(refresh.run_plain("plain", self.entry, self.opts), True)
        run.assert_called_once()

    def hung_entry(self):
        """A plain index whose one refresh command sleeps far past a 1-second build timeout, after a
        run that succeeded."""
        self.entry["refresh"] = [["{python}", "-c", "import time; time.sleep(60)"]]
        self.state.mkdir(parents=True)
        (self.state / "last_run.json").write_text(json.dumps(dict(index="plain", ok=True)))
        return {"defaults": {}, "indexes": {"plain": self.entry}}

    def test_a_plain_build_that_times_out_is_recorded_as_failed(self):
        reg = self.hung_entry()
        with mock.patch.object(refresh, "BUILD_TIMEOUT_S", 1), contextlib.redirect_stdout(io.StringIO()):
            self.assertIs(refresh.run_one(reg, "plain", self.opts), False)
        info = json.loads((self.state / "last_run.json").read_text())
        self.assertIs(info["ok"], False)
        self.assertIn("timed out after 1s", info["error"])
        self.assertIn("timed out after 1s", (self.state / "last_error.log").read_text())
        self.assertFalse((self.state / "refresh.lock.d").exists())

    def test_a_plain_build_that_cannot_start_is_recorded_as_failed(self):
        self.entry["refresh"] = [[str(self.root / "no-such-program")]]
        reg = {"defaults": {}, "indexes": {"plain": self.entry}}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIs(refresh.run_one(reg, "plain", self.opts), False)
        info = json.loads((self.state / "last_run.json").read_text())
        self.assertIs(info["ok"], False)
        self.assertIn("no-such-program", info["error"])
        self.assertTrue((self.state / "last_error.log").exists())

    def test_a_timed_out_plain_build_counts_toward_its_failure_streak(self):
        from fleetwatch import index_health
        reg = self.hung_entry()
        log = self.root / "logs" / "f.log"
        with mock.patch.object(refresh, "BUILD_TIMEOUT_S", 1), \
                mock.patch.object(refresh, "load_registry", return_value=reg), \
                mock.patch.object(refresh, "FAILURES_LOG", log), \
                mock.patch.object(refresh, "resolve_routing"), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            refresh._main([])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("plain FAILED (1 in a row): timed out after 1s", log.read_text())
        info = json.loads((self.state / "last_run.json").read_text())
        h = index_health.Health(name="plain", group="g", kind="plain",
                                state="ok" if info["ok"] else "failed", streak=3)
        self.assertTrue(h.streak_problem(0))

    def test_a_session_index_build_that_times_out_is_recorded_as_failed(self):
        state = self.root / "sess" / "state"
        state.mkdir(parents=True)
        (state / "last_run.json").write_text(json.dumps(dict(index="sess", ok=True)))
        ix = refresh.Index.__new__(refresh.Index)
        ix.name, ix.dir, ix.state = "sess", self.root / "sess", state
        ix.refresh = lambda *a, **k: ix.build()
        (ix.dir / "search.py").write_text("import time; time.sleep(60)\n")
        reg = {"defaults": {}, "indexes": {"sess": {}}}
        with mock.patch.object(refresh, "Index", lambda *a: ix), \
                mock.patch.object(refresh, "BUILD_PY", sys.executable), \
                mock.patch.object(refresh, "BUILD_TIMEOUT_S", 1), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIs(refresh.run_one(reg, "sess", self.opts), False)
        info = json.loads((state / "last_run.json").read_text())
        self.assertIs(info["ok"], False)
        self.assertIn("timed out after 1s", info["error"])
        self.assertIn("timed out after 1s", (state / "last_error.log").read_text())

    def index_now(self):
        reg = {"defaults": {}, "indexes": {"plain": self.entry}}
        with mock.patch.object(refresh, "load_registry", return_value=reg), \
                mock.patch.object(refresh, "run_bounded") as run, \
                self.assertRaises(SystemExit) as cm:
            refresh._main(["index-now", "plain", str(self.root / "src" / "a.md")])
        run.assert_not_called()
        return cm.exception.code

    def test_index_now_on_a_locked_plain_index_exits_with_the_retry_message(self):
        self.hold(self.state / "refresh.lock.d")
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.index_now()
        self.assertIn("refresh already running; try again shortly", str(code))

    def test_index_now_reports_a_lock_taken_after_any_earlier_check(self):
        # Another process can take the lock after a separate "is it held" check passed; the
        # outcome must come from the lock attempt itself.
        self.hold(self.state / "refresh.lock.d")
        with mock.patch.object(refresh, "plain_lock_held", return_value=False, create=True), \
                contextlib.redirect_stdout(io.StringIO()):
            code = self.index_now()
        self.assertIn("refresh already running; try again shortly", str(code))

    def run_all(self, run_one=None, routing=None):
        reg = {"defaults": {}, "indexes": {"one": {}}}
        called = []
        out = io.StringIO()
        with mock.patch.object(refresh, "load_registry", return_value=reg), \
                mock.patch.object(refresh, "FAILURES_LOG", self.root / "logs" / "f.log"), \
                mock.patch.object(refresh, "run_one", run_one or (lambda *a: called.append(a[1]) or True)), \
                mock.patch.object(refresh, "record_streak"), \
                mock.patch.object(refresh, "resolve_routing", routing or mock.Mock()), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            refresh._main([])
        return cm.exception.code, called, out.getvalue()

    def nested_run(self, routing):
        """Run B starts while run A is inside one index, after every lock file under logs/ has
        aged past the staleness window (one index refresh that took over an hour)."""
        inner = []

        def run_one(reg, name, opts):
            if not inner:
                inner.append(None)
                for p in (self.root / "logs").rglob("*"):
                    old = os.stat(p).st_mtime - refresh.LOCK_STALE_S - 60
                    os.utime(p, (old, old))
                inner[0] = self.run_all(routing=routing)
            return True
        code, _, _ = self.run_all(run_one=run_one, routing=routing)
        return code, inner[0]

    def test_a_second_run_during_a_long_index_is_refused(self):
        code, (inner_code, inner_called, inner_out) = self.nested_run(mock.Mock())
        self.assertEqual(code, 0)
        self.assertEqual((inner_code, inner_called), (0, []))
        self.assertIn("already in progress", inner_out)

    def test_a_refused_run_resolves_no_routing(self):
        routing = mock.Mock()
        self.nested_run(routing)
        self.assertEqual(routing.call_count, 1)

    def test_a_run_lock_is_released_when_the_run_ends(self):
        self.assertEqual(self.run_all()[:2], (0, ["one"]))
        self.assertEqual(self.run_all()[:2], (0, ["one"]))

    def test_a_killed_run_leaves_no_lock_behind(self):
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time\nfrom pathlib import Path\nfrom fleetwatch.memory import refresh\n"
             "refresh.ROOT = Path(sys.argv[1])\nwith refresh.run_lock() as held:\n"
             "    print('held' if held else 'busy', flush=True)\n    time.sleep(60)\n",
             str(self.root)], stdout=subprocess.PIPE, text=True,
            env=dict(os.environ, FLEETWATCH_NO_MIGRATE="1",
                     PYTHONPATH=str(Path(refresh.__file__).resolve().parents[2])))
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), "held")
        code, called, out = self.run_all()
        self.assertEqual((code, called), (0, []))
        self.assertIn("already in progress", out)
        self.assertFalse((self.root / "logs" / "refresh_run.json").exists())
        child.kill()
        child.wait()
        child.stdout.close()
        self.assertEqual(self.run_all()[:2], (0, ["one"]))

    def test_a_skipped_index_keeps_its_failure_streak_and_is_not_counted_ok(self):
        self.hold(self.root / "one" / "state" / "refresh.lock.d")
        streak = self.root / "logs" / "streaks" / "one"
        streak.parent.mkdir(parents=True)
        streak.write_text("4")
        log = self.root / "logs" / "f.log"
        reg = {"defaults": {}, "indexes": {"one": self.entry}}
        out = io.StringIO()
        with mock.patch.object(refresh, "load_registry", return_value=reg), \
                mock.patch.object(refresh, "FAILURES_LOG", log), \
                mock.patch.object(refresh, "resolve_routing"), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            refresh._main([])
        self.assertFalse((self.root / "one" / "state" / "last_run.json").exists())
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(streak.read_text(), "4")
        self.assertFalse(log.exists() and "recovered" in log.read_text())
        self.assertIn("0 ok, 1 skipped, 0 failed", out.getvalue())
        marker = json.loads((self.root / "logs" / "refresh_run.json").read_text())
        self.assertEqual(marker["finished"], [])


class BoundedBuilds(unittest.TestCase):
    """A refresh run holds its run lock for life, so a build command that never returns must be cut off."""

    def test_a_hung_build_and_its_children_are_killed_and_reported(self):
        import subprocess, sys, tempfile, time as _t
        from fleetwatch.memory import refresh as rf
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "child.pid")
            grandchild = os.path.join(d, "grandchild.py")
            child = os.path.join(d, "child.py")
            with open(grandchild, "w") as f:
                f.write("import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
            # The child starts a grandchild that records its pid, then both sleep far past the timeout.
            with open(child, "w") as f:
                f.write("import subprocess, sys, time\n"
                        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\ntime.sleep(60)\n")
            t0 = _t.time()
            with self.assertRaises(RuntimeError) as cm:
                rf.run_bounded([sys.executable, child, grandchild, marker], d, timeout=2)
            self.assertLess(_t.time() - t0, 20)
            self.assertIn("timed out after 2s", str(cm.exception))
            for _ in range(40):  # the kernel reaps the group asynchronously
                try:
                    os.kill(int(Path(marker).read_text()), 0)
                except ProcessLookupError:
                    break
                _t.sleep(0.1)
            with open(marker) as f:
                gpid = int(f.read())
            with self.assertRaises(ProcessLookupError):
                os.kill(gpid, 0)

    def test_an_interrupted_build_takes_its_process_group_down(self):
        import sys, tempfile, time as _t
        from fleetwatch.memory import refresh as rf
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "child.pid")
            child = os.path.join(d, "child.py")
            with open(child, "w") as f:
                f.write("import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
            real = subprocess.Popen.communicate
            calls = []

            def communicate(self, *a, **k):
                if not calls:
                    calls.append(1)
                    for _ in range(100):
                        if os.path.exists(marker) and Path(marker).read_text():
                            break
                        _t.sleep(0.05)
                    raise KeyboardInterrupt
                return real(self, *a, **k)
            with mock.patch.object(subprocess.Popen, "communicate", communicate):
                with self.assertRaises(KeyboardInterrupt):
                    rf.run_bounded([sys.executable, child, marker], d, timeout=30)
            with open(marker) as f:
                pid = int(f.read())
            for _ in range(40):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                _t.sleep(0.1)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_a_build_that_finishes_returns_its_output(self):
        import sys, tempfile
        from fleetwatch.memory import refresh as rf
        with tempfile.TemporaryDirectory() as d:
            r = rf.run_bounded([sys.executable, "-c", "print('Indexed 3 chunks')"], d, timeout=30)
            self.assertEqual(r.returncode, 0)
            self.assertIn("Indexed 3 chunks", r.stdout)


if __name__ == "__main__":
    unittest.main()
