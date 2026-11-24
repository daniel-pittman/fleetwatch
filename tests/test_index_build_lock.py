"""Overlapping index builds must not corrupt each other: each build writes its own temp file, a
plain index build takes the per-index lock, and only one `refresh --all` runs at a time."""
import argparse
import contextlib
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")

from fleetwatch.memory import refresh  # noqa: E402
from fleetwatch.search import engine  # noqa: E402
from test_search_library import GARDEN, make_index  # noqa: E402


class TempNames(unittest.TestCase):
    def test_two_builds_of_one_index_keep_separate_temp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_index(tmp, "garden", GARDEN)
            ix = engine.Index(d)
            barrier = threading.Barrier(2, timeout=30)
            real_connect = engine.sqlite3.connect
            opened, errors = [], []
            lock = threading.Lock()
            ids = {}

            def connect(path, *a, **k):
                con = real_connect(path, *a, **k)
                with lock:
                    opened.append(Path(path))
                barrier.wait()
                with lock:
                    alive = [p.exists() for p in opened]
                if not all(alive):
                    errors.append("a temp file vanished: %s" % opened)
                return con

            def pid():
                with lock:
                    return ids.setdefault(threading.get_ident(), 1000 + len(ids))

            def build():
                try:
                    ix.build()
                except Exception as e:
                    errors.append(repr(e))

            with mock.patch.object(engine.sqlite3, "connect", connect), \
                    mock.patch.object(engine.os, "getpid", pid):
                ts = [threading.Thread(target=build) for _ in range(2)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join(60)
            self.assertEqual(errors, [])
            self.assertEqual(len(set(opened)), 2)
            self.assertEqual(sorted(p.name for p in (d / "index").iterdir()), ["garden.sqlite"])
            res = engine.Index(d).search("tomatoes")
            self.assertTrue(res.results[0][0]["path"].endswith("tomatoes.md"))

    def test_a_failed_build_removes_only_its_own_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = make_index(tmp, "garden", GARDEN)
            other = d / "index" / "garden.sqlite.tmp.424242"
            other.parent.mkdir(parents=True)
            other.write_text("another build's file")
            with mock.patch.object(engine, "build_tfidf", side_effect=RuntimeError("boom")), \
                    self.assertRaises(RuntimeError):
                engine.Index(d).build()
            self.assertTrue(other.exists())
            self.assertEqual([p.name for p in (d / "index").iterdir()], [other.name])


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
        with mock.patch.object(refresh.subprocess, "run") as run, contextlib.redirect_stdout(out):
            self.assertTrue(refresh.run_plain("plain", self.entry, self.opts))
        run.assert_not_called()
        self.assertIn("[plain] refresh already running; skipped", out.getvalue())
        self.assertFalse((self.state / "last_run.json").exists())
        self.assertFalse((self.state / "last_error.log").exists())
        self.assertTrue((self.state / "refresh.lock.d").exists())

    def test_a_plain_build_takes_and_releases_the_lock(self):
        seen = []

        def run(argv, **k):
            seen.append((self.state / "refresh.lock.d").exists())
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(refresh.subprocess, "run", run), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(refresh.run_plain("plain", self.entry, self.opts))
        self.assertEqual(seen, [True])
        self.assertFalse((self.state / "refresh.lock.d").exists())

    def test_a_stale_plain_lock_does_not_block(self):
        self.hold(self.state / "refresh.lock.d", age=refresh.LOCK_STALE_S + 60)
        with mock.patch.object(refresh.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(refresh.run_plain("plain", self.entry, self.opts))
        run.assert_called_once()

    def test_index_now_on_a_locked_plain_index_exits_with_the_retry_message(self):
        self.hold(self.state / "refresh.lock.d")
        reg = {"defaults": {}, "indexes": {"plain": self.entry}}
        with mock.patch.object(refresh, "load_registry", return_value=reg), \
                mock.patch.object(refresh.subprocess, "run") as run, \
                self.assertRaises(SystemExit) as cm:
            refresh._main(["index-now", "plain", str(self.root / "src" / "a.md")])
        run.assert_not_called()
        self.assertIn("refresh already running; try again shortly", str(cm.exception.code))

    def run_all(self):
        reg = {"defaults": {}, "indexes": {"one": {}}}
        called = []
        out = io.StringIO()
        with mock.patch.object(refresh, "load_registry", return_value=reg), \
                mock.patch.object(refresh, "FAILURES_LOG", self.root / "logs" / "f.log"), \
                mock.patch.object(refresh, "run_one", lambda *a: called.append(a[1]) or True), \
                mock.patch.object(refresh, "record_streak"), mock.patch.object(refresh, "resolve_routing"), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            refresh._main([])
        return cm.exception.code, called, out.getvalue()

    def test_a_second_run_while_one_is_live_builds_nothing(self):
        self.hold(self.root / "logs" / "refresh.lock.d")
        code, called, out = self.run_all()
        self.assertEqual((code, called), (0, []))
        self.assertIn("already in progress", out)
        self.assertFalse((self.root / "logs" / "refresh_run.json").exists())

    def test_a_stale_run_lock_does_not_block_and_is_released(self):
        self.hold(self.root / "logs" / "refresh.lock.d", age=refresh.LOCK_STALE_S + 60)
        code, called, _ = self.run_all()
        self.assertEqual((code, called), (0, ["one"]))
        self.assertFalse((self.root / "logs" / "refresh.lock.d").exists())


if __name__ == "__main__":
    unittest.main()
