"""JSONL is split on newline only. json.dumps(ensure_ascii=False) leaves U+2028, U+2029 and U+0085
raw inside a string, and str.splitlines() would cut such a record in half."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")

from fleetwatch import index_migrate  # noqa: E402
from fleetwatch.memory import refresh, session_parse, session_tagger  # noqa: E402

ODD = "a b c\u0085d\x0be\x0cf\x1cg"


def jsonl(*recs):
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs) + "\n"


class Separators(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.dir = Path(t.name)

    def test_the_fixture_really_splits_under_splitlines(self):
        self.assertGreater(len(jsonl({"t": ODD}).splitlines()), 2)

    def test_read_records(self):
        p = self.dir / "s.jsonl"
        p.write_text(jsonl({"id": 1, "t": ODD}, {"id": 2, "t": ODD}), encoding="utf-8")
        self.assertEqual(session_parse.read_records(p), [{"id": 1, "t": ODD}, {"id": 2, "t": ODD}])

    def test_load_tags(self):
        p = self.dir / "tags.jsonl"
        p.write_text(jsonl({"id": "x", "t": ODD}), encoding="utf-8")
        self.assertEqual(session_tagger.load_tags(p), {"x": {"id": "x", "t": ODD}})

    def test_stored_versions_counts_a_tag_record_holding_the_separators(self):
        state = self.dir / "state"
        state.mkdir()
        (state / "tags.jsonl").write_text(jsonl({"id": "x", "t": ODD, "sv": 1, "pv": 1}), encoding="utf-8")
        self.assertEqual(index_migrate.stored_versions(self.dir, "s", 1, 1)[2:], (1, 1))

    def test_status_reads_a_usage_record_holding_the_separators(self):
        root = self.dir / "data"
        state = root / "demo" / "state"
        state.mkdir(parents=True)
        (state / "usage.jsonl").write_text(jsonl({"calls": 2, "total_cost_usd": 0.5, "note": ODD}),
                                           encoding="utf-8")
        (state / "last_run.json").write_text(json.dumps(dict(
            ok=True, finished="2026-01-01T00:00:00+00:00", wall_s=1, build_s=1, built=True,
            tags={"tagged": 0, "failed": 0, "deferred": 0}, tags_cached=0, exchanges=1,
            new_exchanges=0, docs_changed=0, docs_removed=0, docs_unchanged=0, agent_runs={})))
        reg = {"defaults": {}, "indexes": {"demo": {"group": "g"}}}
        out = io.StringIO()
        with mock.patch.object(refresh, "ROOT", root), mock.patch.object(refresh, "resolve_routing"), \
                contextlib.redirect_stdout(out):
            refresh.cmd_status(reg)
        self.assertIn("usage to date: 2 calls, $0.50", out.getvalue())


if __name__ == "__main__":
    unittest.main()
