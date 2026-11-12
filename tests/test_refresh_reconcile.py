"""The refresh driver on a fabricated two-folder session index (no spaCy, no engine build): a
session whose transcript is gone is held from its stored records (re-scrubbed when the rules
change), on_missing: remove and `forget` take one out, and a failed build is retried on the
next run."""
import argparse
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")

from fleetwatch import settings
from fleetwatch.memory import refresh, session_tagger

SIDS = ("aa11bb22-0000-4000-8000-000000000001", "cc33dd44-0000-4000-8000-000000000002",
        "ee55ff66-0000-4000-8000-000000000003")
TAGS = {"description": "a test session", "areas": {"a": "area a"}, "kinds": {"k": "kind k"}}


def transcript(path, day, words, extra=""):
    def line(d):
        return json.dumps(d, separators=(",", ":")) + "\n"
    path.write_text(
        line(dict(type="user", message=dict(role="user", content="Question about the %s?%s" % (words, extra)),
                  timestamp="2026-04-%02dT16:00:00Z" % day))
        + line(dict(type="assistant", message=dict(id="m%d" % day, content=[
            dict(type="text", text="Answer about the %s." % words)]),
            timestamp="2026-04-%02dT16:00:05Z" % day)))
    old = 1_700_000_000
    os.utime(path, (old, old))


class Reconcile(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        base = Path(t.name).resolve()
        cfg, data = base / "config", base / "data"
        self.a, self.b = base / "projects" / "-a", base / "projects" / "-b"
        self.a.mkdir(parents=True)
        self.b.mkdir(parents=True)
        old = refresh.SETTINGS
        self.addCleanup(refresh.configure, old)
        refresh.configure(settings.indexer(raw={"paths": {"data_dir": str(data),
                                                          "projects_dir": str(base / "projects")}},
                                           cfg_dir=cfg))
        c = refresh.CONFIG_ROOT / "demo"
        c.mkdir(parents=True)
        (c / "allow.yaml").write_text("version: 1\nperson_names: false\n")
        (c / "tags.yaml").write_text(yaml.safe_dump(TAGS))
        self.entry = {"group": "research",
                      "transcripts": {"project_dir": [str(self.a), str(self.b)], "sessions": "all",
                                      "exclude_automated": False},
                      "privacy": {"allowlist": "demo/allow.yaml"}, "tags": "demo/tags.yaml"}
        transcript(self.a / (SIDS[0] + ".jsonl"), 1, "lantern")
        transcript(self.b / (SIDS[1] + ".jsonl"), 2, "seed tray")
        self.builds = []

    def refresh(self, fail=False):
        def build(ix):
            self.builds.append(ix.name)
            if fail:
                raise RuntimeError("engine build failed: boom")
            ix.index_file.parent.mkdir(parents=True, exist_ok=True)
            ix.index_file.write_text("db")
            return 0.1, "Indexed"
        ix = refresh.Index("demo", self.entry, {"tag": False, "tag_budget_per_run": 0})
        opts = argparse.Namespace(full=False, no_tag=True, tag_budget=None, build=False, retag_all=False)
        with mock.patch.object(refresh.Index, "build", build):
            return ix, ix.refresh(opts)

    def corpus(self, ix):
        return sorted(p.name for p in (ix.dir / "corpus" / "session").rglob("*.md"))

    def test_a_deleted_transcript_is_held_by_default(self):
        ix, info = self.refresh()
        before = self.corpus(ix)
        self.assertEqual(len(before), 2)
        self.builds.clear()
        (self.b / (SIDS[1] + ".jsonl")).unlink()
        ix, info = self.refresh()
        self.assertEqual(self.corpus(ix), before)
        self.assertEqual((info["sessions_held"], info["sessions_removed"]), (1, 0))
        self.assertEqual(self.builds, [])
        self.assertTrue((ix.state / "sessions" / (SIDS[1] + ".jsonl")).exists())
        # a growing session rewrites the corpus and keeps the held one
        transcript(self.a / (SIDS[2] + ".jsonl"), 3, "gate hinge")
        ix, info = self.refresh()
        self.assertEqual(len(self.corpus(ix)), 3)
        self.assertIn("Scc33dd-EX-0001.owner.md", self.corpus(ix))

    def test_on_missing_remove_takes_a_deleted_transcript_out_and_rebuilds(self):
        self.entry["transcripts"]["on_missing"] = "remove"
        ix, info = self.refresh()
        self.builds.clear()
        ix, info = self.refresh()
        self.assertEqual(self.builds, [])               # nothing changed: no build
        (self.b / (SIDS[1] + ".jsonl")).unlink()
        ix, info = self.refresh()
        self.assertEqual(self.corpus(ix), ["Saa11bb-EX-0001.owner.md"])
        self.assertEqual((info["sessions_removed"], info["built"]), (1, True))
        self.assertFalse((ix.state / "sessions" / (SIDS[1] + ".jsonl")).exists())

    def test_an_unknown_on_missing_value_is_an_error(self):
        self.entry["transcripts"]["on_missing"] = "delete"
        with self.assertRaises(ValueError):
            self.refresh()

    def test_a_held_session_is_rescrubbed_when_the_rules_change(self):
        allow = refresh.CONFIG_ROOT / "demo" / "allow.yaml"
        allow.write_text("version: 1\nperson_names: false\nemails: [jdoe@example.org]\n")
        transcript(self.b / (SIDS[1] + ".jsonl"), 2, "seed tray", " Mail jdoe@example.org.")
        ix, _ = self.refresh()
        stored = ix.state / "sessions" / (SIDS[1] + ".jsonl")
        self.assertIn("jdoe@example.org", stored.read_text())
        (self.b / (SIDS[1] + ".jsonl")).unlink()
        allow.write_text("version: 1\nperson_names: false\n")      # a new scanner version
        ix, info = self.refresh()
        self.assertEqual(info["sessions_held"], 1)
        self.assertNotIn("jdoe@example.org", stored.read_text())
        corpus = "".join(p.read_text() for p in (ix.dir / "corpus" / "session").rglob("*.md"))
        self.assertNotIn("jdoe@example.org", corpus)
        self.assertIn("[email]", corpus)
        cursor = json.loads((ix.state / "sessions" / (SIDS[1] + ".cursor.json")).read_text())
        self.assertTrue(cursor["scan_version"].startswith(ix.scanner.version))

    def test_a_corpus_an_earlier_run_left_stale_is_reconciled(self):
        ix, _ = self.refresh()
        (self.b / (SIDS[1] + ".jsonl")).unlink()
        for p in (ix.state / "sessions").glob(SIDS[1] + "*"):
            p.unlink()                                  # what the earlier code left behind
        ix, info = self.refresh()
        self.assertEqual(self.corpus(ix), ["Saa11bb-EX-0001.owner.md"])
        self.assertTrue(info["built"])

    def test_a_dropped_held_record_stays_dropped_after_a_rule_change(self):
        ix, _ = self.refresh()
        stored = ix.state / "sessions" / (SIDS[1] + ".jsonl")
        recs = [json.loads(x) for x in stored.read_text().splitlines()]
        recs[0]["dropped"] = "anon-id-pairing"        # as the scan that stored it decided
        stored.write_text("".join(json.dumps(r) + "\n" for r in recs))
        (self.b / (SIDS[1] + ".jsonl")).unlink()
        allow = refresh.CONFIG_ROOT / "demo" / "allow.yaml"
        allow.write_text("version: 1\nperson_names: false\nemails: [x@example.org]\n")  # new version
        ix, info = self.refresh()
        self.assertEqual(info["sessions_held"], 1)
        again = [json.loads(x) for x in stored.read_text().splitlines()]
        self.assertEqual(again[0].get("dropped"), "anon-id-pairing")
        cursor = json.loads((ix.state / "sessions" / (SIDS[1] + ".cursor.json")).read_text())
        self.assertTrue(cursor["scan_version"].startswith(ix.scanner.version))   # it was re-scrubbed
        self.assertEqual(self.corpus(ix), ["Saa11bb-EX-0001.owner.md"])

    def test_a_missing_registered_folder_holds_even_with_on_missing_remove(self):
        self.entry["transcripts"]["on_missing"] = "remove"
        ix, _ = self.refresh()
        before = self.corpus(ix)
        shutil.move(str(self.a), str(self.a.parent / "archived"))
        self.builds.clear()
        ix, info = self.refresh()
        self.assertEqual(self.corpus(ix), before)
        self.assertEqual((info["sessions_held"], info["sessions_removed"]), (1, 0))
        self.assertEqual(self.builds, [])

    def test_stale_count_healing_keeps_held_sessions(self):
        ix, _ = self.refresh()
        (self.a / (SIDS[0] + ".jsonl")).unlink()          # held from here on
        stray = ix.dir / "corpus" / "session" / "2026-04" / "20260401" / "Sffffff-EX-0001.owner.md"
        stray.write_text("# stray\n")                     # a file no stored record backs
        ix, info = self.refresh()
        self.assertEqual(self.corpus(ix), ["Saa11bb-EX-0001.owner.md", "Scc33dd-EX-0001.owner.md"])
        self.assertTrue(info["built"])

    def test_a_session_dropped_from_the_list_leaves_even_with_a_folder_missing(self):
        ix, _ = self.refresh()
        shutil.rmtree(self.a)
        self.entry["transcripts"]["sessions"] = [SIDS[1]]
        ix, info = self.refresh()
        self.assertEqual(self.corpus(ix), ["Scc33dd-EX-0001.owner.md"])
        self.assertEqual(info["sessions_removed"], 1)


class Withheld(unittest.TestCase):
    """A withhold pattern added after an exchange was tagged purges its cached summary and
    topics; the corpus never shows them, with tagging off."""
    setUp = Reconcile.setUp
    refresh = Reconcile.refresh

    def test_a_withheld_exchange_loses_its_cached_tags(self):
        ix = refresh.Index("demo", self.entry, {})
        records, _, _ = ix.transcripts(full=True)
        rec = next(r for r in records if r["id"] == "Saa11bb-EX-0001")
        tags = ix.state / "tags.jsonl"
        tags.parent.mkdir(parents=True, exist_ok=True)
        tags.write_text(json.dumps({"id": "Saa11bb-EX-0001", "summary": "CONFIDENTIAL-HARVEST summary",
                                    "topics": ["harvest-secret"], "hash": session_tagger.tag_hash(rec),
                                    "sv": ix.scanner.version}) + "\n")
        ix, _ = self.refresh()
        text = "".join(p.read_text() for p in (ix.dir / "corpus" / "session").rglob("*.md"))
        self.assertIn("CONFIDENTIAL-HARVEST", text, "the cached summary renders before the policy")
        self.entry["transcripts"]["withhold_patterns"] = ["lantern"]
        ix, _ = self.refresh()
        text = "".join(p.read_text() for p in (ix.dir / "corpus" / "session").rglob("*.md"))
        self.assertIn("[Exchange withheld by index policy]", text)
        for leak in ("CONFIDENTIAL-HARVEST", "harvest-secret", "lantern"):
            self.assertNotIn(leak, text)
            self.assertNotIn(leak, tags.read_text())


class StaleTags(unittest.TestCase):
    """A cached tag describes the text it was made from. When the exchange's text has changed
    since (stricter scan rules redacted more of it) and it is not retagged this run, the old
    summary and topics are not rendered."""

    def test_a_tag_whose_hash_differs_from_the_record_is_not_rendered(self):
        from fleetwatch.memory import session_tagger as st
        tc = st.TagConfig(TAGS)
        r = dict(id="Saa11bb-EX-0001", ts="2026-04-01T16:00:00Z", sender_kind="user", sender_name="me",
                 user_text="Regrade request for [id].", assistant=["Approved."], tools={},
                 interrupted=False, session="aa11bb22", start_line=1, end_line=2)
        r["hash"] = st.tag_hash(r)
        stale = {"id": r["id"], "hash": "0" * 16, "summary": "Regrade for QX-424242 approved.",
                 "topics": ["qx-424242 regrade"]}
        with tempfile.TemporaryDirectory() as t:
            corpus = Path(t) / "corpus"
            st.write_corpus([r], {r["id"]: stale}, tc, corpus)
            text = "".join(p.read_text() for p in corpus.rglob("*.md"))
            self.assertNotIn("424242", text)
            self.assertIn("Regrade request for [id].", text)
            st.write_corpus([r], {r["id"]: dict(stale, hash=r["hash"])}, tc, corpus)
            text = "".join(p.read_text() for p in corpus.rglob("*.md"))
            self.assertIn("qx-424242", text, "a current tag still renders")


class PrivateModes(unittest.TestCase):
    """Index data is created owner-only, whatever umask the caller had."""
    setUp = Reconcile.setUp

    def test_refresh_creates_owner_only_folders_and_files(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        refresh.REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
        (refresh.REGISTRY_DIR / "demo.yaml").write_text(yaml.safe_dump({"indexes": {"demo": self.entry}}))
        refresh.ROOT.mkdir(parents=True, exist_ok=True)
        os.chmod(refresh.ROOT, 0o755)

        def build(ix):
            ix.index_file.parent.mkdir(parents=True, exist_ok=True)
            ix.index_file.write_text("db")
            return 0.1, "Indexed"
        with mock.patch.object(refresh.Index, "build", build), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            refresh.main(["--index", "demo", "--no-tag"])
        self.assertEqual(cm.exception.code, 0)
        ix = refresh.ROOT / "demo"
        self.assertEqual(refresh.ROOT.stat().st_mode & 0o777, 0o700)
        self.assertEqual(ix.stat().st_mode & 0o777, 0o700)
        files = [p for p in ix.rglob("*") if p.is_file()]
        self.assertTrue(files)
        for p in files:
            self.assertEqual(p.stat().st_mode & 0o077, 0, p.relative_to(ix))
        for p in ix.rglob("*"):
            if p.is_dir():
                self.assertEqual(p.stat().st_mode & 0o077, 0, p.relative_to(ix))


class Forget(unittest.TestCase):
    setUp = Reconcile.setUp
    refresh = Reconcile.refresh
    corpus = Reconcile.corpus

    def forget(self, sid, force=False):
        reg = {"indexes": {"demo": self.entry}, "defaults": {"tag": False, "tag_budget_per_run": 0}}
        with mock.patch.object(refresh.Index, "build", lambda ix: (self.builds.append(ix.name), (0.1, "x"))[1]):
            return refresh.forget_session(reg, "demo", sid, force)

    def test_forget_removes_records_tags_and_corpus_then_rebuilds(self):
        ix, _ = self.refresh()
        tags = ix.state / "tags.jsonl"
        tags.write_text(json.dumps({"id": "Scc33dd-EX-0001", "summary": "s"}) + "\n"
                        + json.dumps({"id": "Saa11bb-EX-0001", "summary": "t"}) + "\n")
        (self.b / (SIDS[1] + ".jsonl")).unlink()
        self.builds.clear()
        res = self.forget(SIDS[1])
        self.assertEqual((res["exchanges"], res["corpus_files"], res["tags"], res["built"]), (1, 1, 1, True))
        self.assertEqual(self.corpus(ix), ["Saa11bb-EX-0001.owner.md"])
        self.assertFalse(list((ix.state / "sessions").glob(SIDS[1] + "*")))
        self.assertNotIn("Scc33dd", tags.read_text())
        self.assertEqual(self.builds, ["demo"])
        # it stays gone: nothing is held on the next refresh
        ix, info = self.refresh()
        self.assertEqual((info["sessions_held"], self.corpus(ix)), (0, ["Saa11bb-EX-0001.owner.md"]))

    def test_forget_refuses_while_the_transcript_is_registered_unless_forced(self):
        ix, _ = self.refresh()
        with self.assertRaises(SystemExit):
            self.forget(SIDS[1])
        self.assertEqual(len(self.corpus(ix)), 2)
        res = self.forget(SIDS[1], force=True)
        self.assertTrue(res["transcript_still_present"])
        self.assertEqual(self.corpus(ix), ["Saa11bb-EX-0001.owner.md"])

    def test_forget_refuses_a_path_as_a_session_id(self):
        self.refresh()
        with self.assertRaises(SystemExit):
            self.forget("../x")


class FailedBuild(unittest.TestCase):
    setUp = Reconcile.setUp
    refresh = Reconcile.refresh

    def test_a_failed_build_is_retried_on_the_next_unchanged_run(self):
        with self.assertRaises(RuntimeError):
            self.refresh(fail=True)
        ix = refresh.Index("demo", self.entry, {"tag": False, "tag_budget_per_run": 0})
        self.assertTrue(ix.build_marker().exists())
        self.builds.clear()
        ix, info = self.refresh()                       # no corpus change since the failure
        self.assertEqual(self.builds, ["demo"])
        self.assertTrue(info["built"] and info["build_retried"])
        self.assertFalse(ix.build_marker().exists())
        self.builds.clear()
        ix, info = self.refresh()
        self.assertEqual(self.builds, [])

    def test_status_reports_a_pending_build(self):
        with self.assertRaises(RuntimeError):
            self.refresh(fail=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            refresh.cmd_status({"indexes": {"demo": self.entry}})
        self.assertIn("build pending", out.getvalue())


if __name__ == "__main__":
    unittest.main()
