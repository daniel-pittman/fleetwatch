"""An index is not stale just because one long scheduled `refresh --all` run finished it early; a
schedule that stopped still shows as stale."""
import json
import os
import sys
import time
import unittest
from unittest import mock

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fleetwatch import index_health  # noqa: E402
from test_indexes_tab import CanaryTree, iso  # noqa: E402

H = 3600


class RunMarkerCase(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        os.environ.pop("FLEETWATCH_DEMO", None)
        self.tree = CanaryTree()
        self.addCleanup(self.tree.close)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.saved)))

    def own(self, name, ago):
        path = self.tree.root / name / "state" / "last_run.json"
        info = json.loads(path.read_text())
        info["finished"] = iso(ago)
        path.write_text(json.dumps(info))

    def marker(self, started_ago, ended_ago, finished):
        now = time.time()
        logs = self.tree.root / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "refresh_run.json").write_text(json.dumps(dict(
            started=now - started_ago, ended=None if ended_ago is None else now - ended_ago,
            finished=list(finished))))

    def stale(self, name):
        rows = {h.name: h for h in self.tree.load(scheduled=True).rows}
        return "stale" in rows[name].problems(time.time())


class LongRun(RunMarkerCase):
    def test_an_index_finished_early_is_not_stale_while_the_run_is_going(self):
        self.own("alpha", 2.5 * H)
        self.marker(3 * H, None, ["alpha"])
        self.assertFalse(self.stale("alpha"))

    def test_nor_right_after_the_run_ends(self):
        self.own("alpha", 2 * H)
        self.marker(3 * H + 300, 300, ["alpha"])
        self.assertFalse(self.stale("alpha"))

    def test_an_index_the_run_skipped_is_stale_past_the_threshold(self):
        self.own("alpha", 2 * H)
        self.own("gamma", 3 * H)
        self.marker(3 * H + 300, 300, ["alpha"])
        self.assertTrue(self.stale("gamma"))

    def test_a_stopped_schedule_still_flags_the_index(self):
        self.own("alpha", 4 * H)
        self.marker(5 * H, 4 * H, ["alpha"])
        self.assertTrue(self.stale("alpha"))

    def test_a_dead_run_does_not_hide_a_stale_index_forever(self):
        self.own("gamma", 5 * H)
        self.marker(6 * H, None, [])
        path = self.tree.root / "logs" / "refresh_run.json"
        old = time.time() - 2 * H
        os.utime(path, (old, old))
        self.assertTrue(self.stale("gamma"))

    def test_a_failed_index_is_still_failed(self):
        self.marker(3 * H, None, ["alpha"])
        rows = {h.name: h for h in self.tree.load(scheduled=True).rows}
        self.assertTrue(any(p.startswith("last refresh failed") for p in rows["beta"].problems(time.time())))


class Marker(unittest.TestCase):
    def run_all(self, root, fail=()):
        from fleetwatch.memory import refresh
        reg = {"defaults": {}, "indexes": {"one": {}, "two": {}, "bad": {}}}
        seen = []

        def run_one(_reg, name, _opts):
            if name == "bad":
                return False
            state = root / name / "state"
            state.mkdir(parents=True, exist_ok=True)
            (state / "last_run.json").write_text(json.dumps(dict(ok=True, finished=refresh.now_iso())))
            path = root / "logs" / "refresh_run.json"
            seen.append(json.loads(path.read_text()))
            return True
        with mock.patch.object(refresh, "ROOT", root), mock.patch.object(refresh, "FAILURES_LOG", root / "logs" / "f.log"), \
                mock.patch.object(refresh, "load_registry", return_value=reg), \
                mock.patch.object(refresh, "run_one", run_one), \
                mock.patch.object(refresh, "record_streak"), mock.patch.object(refresh, "last_error", return_value="x"), \
                self.assertRaises(SystemExit):
            refresh._main([])
        return seen

    def test_the_marker_holds_only_times_and_names_and_is_replaced_atomically(self):
        import tempfile
        from pathlib import Path
        from fleetwatch.memory import refresh
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(os, "replace", wraps=os.replace) as replace:
                seen = self.run_all(root)
            final = json.loads((root / "logs" / "refresh_run.json").read_text())
            self.assertEqual(set(final), {"started", "ended", "finished"})
            self.assertEqual(final["finished"], ["one", "two"])
            self.assertIsInstance(final["started"], float)
            self.assertGreaterEqual(final["ended"], final["started"])
            self.assertIsNone(seen[0]["ended"])
            self.assertEqual(seen[1]["finished"], ["one"])
            self.assertEqual(os.listdir(root / "logs"), ["refresh_run.json"])
            self.assertGreaterEqual(replace.call_count, 5)
            self.assertTrue(hasattr(refresh, "write_run_marker"))

    def test_a_single_index_refresh_writes_no_marker(self):
        import tempfile
        from pathlib import Path
        from fleetwatch.memory import refresh
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reg = {"defaults": {}, "indexes": {"one": {}}}
            with mock.patch.object(refresh, "ROOT", root), mock.patch.object(refresh, "load_registry", return_value=reg), \
                    mock.patch.object(refresh, "run_one", return_value=True), mock.patch.object(refresh, "record_streak"), \
                    self.assertRaises(SystemExit):
                refresh._main(["--index", "one"])
            self.assertFalse((root / "logs" / "refresh_run.json").exists())


if __name__ == "__main__":
    unittest.main()
