"""The Antigravity conversation source on databases built here with a small protobuf encoder:
exchange pairing, tool names without their arguments, the working folder, timestamps,
malformed steps, wrong-shape databases, a growing conversation, a sub-agent's conversation,
and opening that never writes beside the database. Also the wire-format decoder, the
registry wiring."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from fleetwatch import settings
from fleetwatch.memory import (antigravity_parse as ap, protowire as pw, refresh, render, routing,
                               session_parse as sp, session_search)

HOME = str(Path.home())
CID = "5a1c0de0-1111-4222-8333-444455556666"
CID2 = "5a1c0de0-9999-4aaa-8bbb-ccccddddeeee"   # shares its first eight characters with CID
T0 = 1772359200                                  # 2026-03-01 10:00:00 UTC
SCHEMA = """
CREATE TABLE `trajectory_meta` (`trajectory_id` text,`cascade_id` text,`trajectory_type` integer,
  `source` integer,PRIMARY KEY (`trajectory_id`));
CREATE TABLE `steps` (`idx` integer,`step_type` integer NOT NULL DEFAULT 0,`status` integer NOT
  NULL DEFAULT 0,`has_subtrajectory` numeric NOT NULL DEFAULT false,`metadata` blob,
  `error_details` blob,`permissions` blob,`task_details` blob,`render_info` blob,
  `step_payload` blob,`step_format` integer NOT NULL DEFAULT 0,PRIMARY KEY (`idx`));
CREATE TABLE `trajectory_metadata_blob` (`id` text DEFAULT "main",`data` blob,PRIMARY KEY (`id`));
CREATE TABLE `parent_references` (`idx` integer,`data` blob,PRIMARY KEY (`idx`));
"""


# ------------------------------------------------------------------ a tiny protobuf encoder

def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def num(field, n):
    return varint(field << 3) + varint(n)


def ln(field, data):
    data = data.encode() if isinstance(data, str) else data
    return varint(field << 3 | 2) + varint(len(data)) + data


def fixed(field, n, width):
    return varint(field << 3 | (1 if width == 8 else 5)) + n.to_bytes(width, "little")


def when(sec, nanos=0):
    return ln(1, num(1, sec) + num(2, nanos))


def step(step_type, sec, body=b"", meta_extra=b""):
    return num(1, step_type) + num(4, 3) + ln(5, when(sec, 250_000_000) + meta_extra) + body


def prompt(text, sec):
    return step(14, sec, ln(19, ln(2, text) + ln(3, ln(1, text))))


def turn(sec, answer=None, reasoning="private reasoning never indexed"):
    body = ln(3, reasoning) + ln(7, ln(1, "call_1") + ln(2, "read_file")
                                 + ln(3, '{"Path": "/argument/never/indexed"}'))
    if answer is not None:
        body = ln(1, answer) + body + ln(8, answer)
    return step(15, sec, ln(20, body))


def tool(name, sec, args='{"Path": "/argument/never/indexed"}', output="tool output never indexed",
         step_type=132):
    return step(step_type, sec, ln(140, ln(2, ln(1, output))),
                meta_extra=ln(4, ln(1, "call_1") + ln(2, name) + ln(3, args)))


def system(kind, text, sec):
    return step(101, sec, ln(114, ln(1, "[Message] content=" + text)
                             + ln(2, ln(1, "Message from Parent Agent") + ln(10, ln(1, text)))
                             + ln(3, kind)))


def error(sec):
    return step(17, sec, ln(24, ln(3, ln(1, "Individual quota reached."))))


def blob(cid=CID, folder=HOME + "/git/demo/widgets", parent=None, sub_name=None):
    uri = "file://" + folder.replace(" ", "%20") if folder else None
    out = (ln(1, ln(1, uri) + ln(3, "")) if uri else b"") + ln(2, num(1, T0)) + ln(3, "traj")
    if parent:
        out += ln(5, parent)
    out += ln(6, parent or cid)
    if uri:
        out += ln(7, uri)
    if sub_name:
        out += ln(8, ln(1, "research") + ln(2, sub_name) + ln(3, "the sub-agent's prompt"))
    return out + ln(18, "default-cli-project")


def conversation_steps():
    return [prompt("Why does the widget count drift?", T0 + 1),
            turn(T0 + 2),
            tool("read_file", T0 + 3),
            tool("run_command", T0 + 4, args='{"CommandLine": "cat /argument/never/indexed"}'),
            system("task_notification", "Background command finished: tool output never indexed",
                   T0 + 5),
            turn(T0 + 6, "The counter resets on reload."),
            error(T0 + 7),
            prompt("Fix it", T0 + 300),
            turn(T0 + 301, "Fixed.")]


class FakeScanner:
    """Stands in for privacy_scan.Scanner: no spaCy, records what it was asked to scan."""

    def __init__(self, version="v1"):
        self.version = version
        self.scanned = 0

    def scan_records(self, recs, triage=None):
        self.scanned += len(recs)
        for r in recs:
            r["scan"] = {}
        return recs


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "conversations"
        self.state = Path(tmp.name) / "state"
        self.root.mkdir()
        self.state.mkdir()

    def db(self, steps, cid=CID, meta=None, schema=SCHEMA, wal=True):
        """A conversation database in WAL mode, closed, as Antigravity leaves one."""
        path = self.root / (cid + ".db")
        conn = sqlite3.connect(str(path))
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(schema)
        if "trajectory_metadata_blob" in schema:
            conn.execute("INSERT INTO trajectory_metadata_blob (id, data) VALUES ('main', ?)",
                         (blob(cid) if meta is None else meta,))
        self.insert(conn, steps)
        self.close(conn, path)
        self.age(path)
        return path

    def close(self, conn, path):
        """Close as Antigravity does, leaving no -wal or -shm file. The system SQLite on macOS
        keeps both after a close (persistent WAL); after a full checkpoint they hold nothing
        the database lacks."""
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        for side in ("-wal", "-shm"):
            Path(str(path) + side).unlink(missing_ok=True)

    def insert(self, conn, steps, start=0):
        """Each step is a payload, its step_type column read from the payload (15 when it does
        not decode), or a (step_type, payload) pair."""
        for i, payload in enumerate(steps, start):
            if isinstance(payload, tuple):
                st, payload = payload
            else:
                st = pw.first(payload, 1)
            conn.execute("INSERT INTO steps (idx, step_type, step_payload) VALUES (?, ?, ?)",
                         (i, st if isinstance(st, int) else 15, payload))
        conn.commit()

    def age(self, p):
        old = time.time() - 3600
        os.utime(p, (old, old))

    def load(self, scanner=None, source=None, **kw):
        source = source or ap.AntigravitySource()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            recs, _skipped, st = sp.load_transcripts(self.root, self.state,
                                                     scanner or FakeScanner(), source=source, **kw)
        self.err = err.getvalue()
        return recs, st

    def listing(self):
        return sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in self.root.iterdir())


class Decoder(unittest.TestCase):
    def test_every_wire_type_decodes(self):
        buf = num(1, 300) + fixed(2, 7, 8) + ln(3, ln(1, "inner")) + fixed(4, 9, 4) + num(1, 2)
        fs = pw.fields(buf)
        self.assertEqual([(f, wt) for f, wt, _ in fs], [(1, 0), (2, 1), (3, 2), (4, 5), (1, 0)])
        self.assertEqual((fs[0][2], fs[1][2], fs[3][2]), (300, 7, 9))
        self.assertEqual(pw.text(buf, 3, 1), "inner")
        self.assertEqual(pw.every(buf, 1), [300, 2])
        self.assertIsNone(pw.first(buf, 9))
        self.assertEqual(pw.first(num(1, 2 ** 63), 1), 2 ** 63)

    def test_malformed_input_raises(self):
        for bad in (b"\x80", b"\x0a\x05ab", b"\x0b", b"\x00\x01", b"\x09\x01\x02",
                    b"\x08" + b"\xff" * 11):
            with self.assertRaises(pw.DecodeError, msg=bad):
                pw.fields(bad)

    def test_a_path_through_a_non_message_yields_nothing(self):
        buf = ln(1, b"\xff\xfe not a message")
        self.assertEqual(pw.every(buf, 1, 2), [])
        self.assertIsNone(pw.text(ln(1, b"\xff\xfe"), 1))


class Antigravity(Base):
    def test_prompts_and_text_answers_pair_into_exchanges(self):
        self.db(conversation_steps())
        recs, st = self.load()
        self.assertEqual([r["user_text"] for r in recs], ["Why does the widget count drift?", "Fix it"])
        self.assertEqual([r["assistant"] for r in recs], [["The counter resets on reload."], ["Fixed."]])
        self.assertEqual({r["provider"] for r in recs}, {"antigravity"})
        self.assertEqual({r["sender_kind"] for r in recs}, {"owner"})
        self.assertEqual([(r["start_line"], r["end_line"]) for r in recs], [(0, 6), (7, 8)])
        self.assertEqual(st["conversations_read"], 1)
        self.assertEqual(st["conversations_skipped"], [])

    def test_tool_names_are_kept_and_arguments_output_reasoning_and_notices_dropped(self):
        self.db(conversation_steps())
        recs, _ = self.load()
        self.assertEqual(recs[0]["tools"], {"read_file": 1, "run_command": 1})
        blob_ = json.dumps(recs)
        for hidden in ("never indexed", "private reasoning", "quota reached", "Background command"):
            self.assertNotIn(hidden, blob_)

    def test_older_tool_step_types_are_named_too(self):
        self.db([prompt("list it", T0 + 1), tool("list_dir", T0 + 2, step_type=9),
                 tool("view_file", T0 + 3, step_type=8), turn(T0 + 4, "Listed.")])
        recs, st = self.load()
        self.assertEqual(recs[0]["tools"], {"list_dir": 1, "view_file": 1})
        self.assertEqual(st["unknown_steps"], {})

    def test_a_finish_call_is_the_reply_without_the_harness_keys(self):
        args = json.dumps({"summary": "Two findings.", "findings": [{"claim": "off by one"}],
                           "toolAction": "Finishing", "toolSummary": "finish"})
        self.db([prompt("Review the diff", T0 + 1), turn(T0 + 2),
                 tool("finish", T0 + 3, args=args), tool("read_file", T0 + 4)])
        recs, _ = self.load()
        self.assertEqual(len(recs[0]["assistant"]), 1)
        reply = json.loads(recs[0]["assistant"][0])
        self.assertEqual(reply, {"summary": "Two findings.", "findings": [{"claim": "off by one"}]})
        self.assertEqual(recs[0]["tools"], {"finish": 1, "read_file": 1})
        self.assertNotIn("argument/never", json.dumps(recs))

    def test_the_folder_comes_from_the_metadata_blob(self):
        self.db(conversation_steps(), meta=blob(folder=HOME + "/git/demo/my widgets"))
        recs, _ = self.load()
        self.assertEqual({r["cwd"] for r in recs}, {"~/git/demo/my widgets"})
        self.db(conversation_steps(), cid=CID2, meta=blob(CID2, folder=None))
        recs, _ = self.load()
        self.assertEqual([("cwd" in r) for r in recs if r["session"] == CID2], [False, False])

    def test_timestamps_are_utc_with_milliseconds(self):
        self.db(conversation_steps())
        recs, _ = self.load()
        self.assertEqual([r["ts"] for r in recs],
                         ["2026-03-01T10:00:01.250Z", "2026-03-01T10:05:00.250Z"])
        self.assertEqual(sp.iso_pair(recs[0]["ts"])[0], "2026-03-01T10:00:01Z")

    def test_a_malformed_step_is_skipped_and_counted(self):
        steps = conversation_steps()
        steps[2] = b"\xff\xff\xff"                            # does not decode
        steps[3] = (132, num(1, 99) + ln(5, when(T0)))       # does not restate its type
        steps.insert(8, step(14, T0 + 299, ln(19, ln(2, ""))))  # a prompt with no text
        self.db(steps)
        recs, st = self.load()
        self.assertEqual(st["steps_skipped"], 3)
        self.assertEqual([r["user_text"] for r in recs], ["Why does the widget count drift?", "Fix it"])
        self.assertEqual(recs[0]["tools"], {})
        self.assertEqual(st["conversations_skipped"], [])

    def test_a_step_type_not_in_the_mapping_is_counted_not_fatal(self):
        steps = conversation_steps()
        steps.insert(2, num(1, 777) + num(4, 3) + ln(5, when(T0)))
        self.db(steps)
        recs, st = self.load()
        self.assertEqual(len(recs), 2)
        self.assertEqual(st["unknown_steps"], {"777": 1})

    def test_a_wrong_shape_database_is_skipped_counted_and_named(self):
        good = self.db(conversation_steps(), cid=CID2)
        bad = {"a0000000-0000-4000-8000-000000000001": "tables",
               "a0000000-0000-4000-8000-000000000002": "payload",
               "a0000000-0000-4000-8000-000000000003": "step_types",
               "a0000000-0000-4000-8000-000000000004": "unreadable",
               "a0000000-0000-4000-8000-000000000005": "metadata"}
        ids = list(bad)
        self.db([], cid=ids[0], schema="CREATE TABLE steps (idx integer, step_payload blob);")
        self.db([b"\xff\xff", b"\x0b"], cid=ids[1])
        self.db([num(1, 900) + ln(5, when(T0))], cid=ids[2])
        (self.root / (ids[3] + ".db")).write_bytes(b"not a database at all" * 100)
        self.db(conversation_steps(), cid=ids[4], meta=b"\x0a\x09short")
        recs, st = self.load()
        self.assertEqual({r["session"] for r in recs}, {CID2})
        self.assertEqual(sorted(st["conversations_skipped"]),
                         sorted([[i + ".db", why] for i, why in bad.items()]))
        self.assertEqual(st["conversations_read"], 1)
        for i in ids:
            self.assertIn(i, self.err)
        self.assertTrue(good.exists())
        # Still skipped on the next refresh, and still reported, until the file changes.
        recs, st = self.load()
        self.assertEqual(len(st["conversations_skipped"]), 5)

    def test_an_unchanged_conversation_is_not_read_again(self):
        self.db(conversation_steps())
        self.load()
        recs, st = self.load()
        self.assertEqual((st["conversations_read"], st["new"], len(recs)), (0, 0, 2))

    def test_a_conversation_without_exchanges_gets_a_cursor_and_is_not_reread(self):
        self.db([])
        recs, st = self.load()
        self.assertEqual((len(recs), st["conversations_read"]), (0, 1))
        self.assertTrue((self.state / "sessions" / (CID + ".cursor.json")).exists())
        recs, st = self.load()
        self.assertEqual(st["conversations_read"], 0)

    def test_a_growing_conversation_is_read_again_from_its_last_exchange(self):
        path = self.db(conversation_steps()[:6])
        recs, _ = self.load()
        self.assertEqual([r["assistant"] for r in recs], [["The counter resets on reload."]])
        first_ids = [r["id"] for r in recs]
        conn = sqlite3.connect(str(path))
        self.insert(conn, [turn(T0 + 7, "One more thing."), prompt("Fix it", T0 + 300),
                           turn(T0 + 301, "Fixed.")], start=6)
        self.close(conn, path)
        recs, st = self.load()
        self.assertEqual(st["conversations_read"], 1)
        self.assertEqual([r["id"] for r in recs][:1], first_ids)
        self.assertEqual([r["assistant"] for r in recs],
                         [["The counter resets on reload.", "One more thing."], ["Fixed."]])
        self.assertEqual(len({r["id"] for r in recs}), 2)
        recs, st = self.load()
        self.assertEqual((st["conversations_read"], len(recs)), (0, 2))

    def test_a_scan_version_change_reparses(self):
        self.db(conversation_steps())
        self.load()
        recs, st = self.load(scanner=FakeScanner("v2"))
        self.assertEqual((st["conversations_read"], st["new"]), (1, 2))

    def test_opening_a_closed_wal_database_writes_nothing_beside_it(self):
        self.db(conversation_steps())
        before = self.listing()
        self.assertEqual([n for n, _, _ in before], [CID + ".db"])
        self.load()
        self.assertEqual(self.listing(), before)

    def test_a_conversation_being_written_is_read_from_a_copy(self):
        path = self.db(conversation_steps()[:6])
        writer = sqlite3.connect(str(path))
        self.addCleanup(writer.close)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        self.insert(writer, [prompt("Fix it", T0 + 300), turn(T0 + 301, "Fixed.")], start=6)
        self.assertTrue(ap.wal_path(path).exists())
        before = self.listing()
        recs, st = self.load()
        self.assertEqual(self.listing(), before)
        self.assertEqual([r["user_text"] for r in recs][-1], "Fix it")
        self.assertEqual(st["conversations_read"], 1)
        # The WAL grows: a change to the -wal file alone triggers a reread.
        self.insert(writer, [turn(T0 + 302, "Checked again.")], start=8)
        recs, st = self.load()
        self.assertEqual(recs[-1]["assistant"], ["Fixed.", "Checked again."])

    def test_a_sub_agent_conversation_is_its_own_session_under_its_name(self):
        self.db(conversation_steps())
        self.db([system("agent_message", "Find the widget files", T0 + 10), turn(T0 + 11),
                 tool("view_file", T0 + 12), turn(T0 + 13, "They are in src/.")],
                cid=CID2, meta=blob(CID2, parent=CID, sub_name="File Finder"))
        recs, _ = self.load()
        sub = [r for r in recs if r["session"] == CID2]
        self.assertEqual(len(sub), 1)
        self.assertEqual((sub[0]["sender_canon"], sub[0]["sender_name"]), ("subagent", "File Finder"))
        self.assertEqual((sub[0]["user_text"], sub[0]["assistant"]),
                         ("Find the widget files", ["They are in src/."]))
        self.assertEqual(sub[0]["tools"], {"view_file": 1})

    def test_exchange_ids_carry_ten_session_characters(self):
        self.db(conversation_steps())
        self.db(conversation_steps(), cid=CID2)
        recs, _ = self.load()
        ids = sorted(r["id"] for r in recs)
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual(ids[0], "S5a1c0de011-EX-0001")
        self.assertTrue(all(session_search.ID_RE.match(i) for i in ids))

    def test_the_folder_renders_and_reads_back(self):
        self.db(conversation_steps())
        recs, _ = self.load()
        text = render.render_exchange(recs[0], None, recs[1]["id"], {}, [])
        self.assertIn("Source: antigravity | Folder: ~/git/demo/widgets\n", text)
        meta_ = render.parse_exchange(text, recs[0]["id"])
        self.assertEqual((meta_["provider"], meta_["folder"], meta_["session"]),
                         ("antigravity", "~/git/demo/widgets", CID))

    def test_withhold_patterns_also_match_the_folder(self):
        self.db(conversation_steps())
        recs, _ = self.load(opts=sp.parse_opts({"withhold_patterns": ["demo/widgets"]}))
        self.assertTrue(all(r["withheld"] for r in recs))
        self.assertFalse(any("cwd" in r for r in recs))

    def test_a_deleted_conversation_is_held_from_its_stored_records(self):
        path = self.db(conversation_steps())
        self.load()
        path.unlink()
        recs, st = self.load()
        self.assertEqual((len(recs), st["held"]), (2, 1))

    def test_other_files_in_the_folder_are_ignored(self):
        self.db(conversation_steps())
        (self.root / "notes.db").write_bytes(b"x")
        (self.root / (CID + ".db-shm")).write_bytes(b"")
        recs, st = self.load()
        self.assertEqual((st["sessions"], st["conversations_skipped"]), (1, []))

    def test_the_real_scanner_scrubs_the_reply(self):
        from fleetwatch.memory.privacy_scan import RuleSet, Scanner
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        allow = Path(tmp.name) / "allow.yaml"
        allow.write_text("version: 1\nemails: []\nstaff: []\nnames: []\n")
        self.db([prompt("Who owns it?", T0 + 1), turn(T0 + 2, "Mail someone@example.org")])
        recs, _ = self.load(scanner=Scanner(allow, None, RuleSet([])))
        self.assertNotIn("someone@example.org", json.dumps(recs))


class Routing(Base):
    def test_routes_conversations_by_launch_folder_and_catch_all(self):
        from tests.test_routing import SID_A, SID_B, write_claude_transcript

        base = self.root.parent.resolve()
        checkout = base / "repo with spaces"
        project = base / "claude"
        write_claude_transcript(project, SID_A, cwd=checkout)
        reg = {"indexes": {
            "workspace": {"group": "work", "transcripts": {"project_dir": str(project)}},
            "launcher": {"group": "work", "owner_session_id": SID_B},
            "vault": {"group": "ferpa"},
        }}
        cfg = settings.indexer(raw={"routing": {
            "sources": {"antigravity": str(self.root), "codex": []}, "catch_all": "vault",
        }}, cfg_dir=base)
        other = "5a1c0de0-0000-4000-8000-000000000001"
        self.db([prompt("Launch", T0 + 30)], meta=blob(folder=str(checkout)))
        self.db([prompt("Folder", T0 + 900)], cid=CID2, meta=blob(CID2, folder=str(checkout)))
        self.db([prompt("Catch-all", T0 + 30)], cid=other, meta=blob(other, folder=str(base / "other")))
        before = self.listing()
        # The blob's creation time precedes the launch. The first step follows it.
        ledger = [{"session_id": SID_B, "tool": "agy", "cwd": str(checkout), "ts_epoch": T0 + 15}]
        plan = routing.resolve_all(reg, cfg, refresh.agent_sources(), ledger=ledger)
        self.assertEqual({p.run.session_id: (p.run.provider, p.index, p.reason)
                          for p in plan.placements}, {
            CID: ("antigravity", "launcher", routing.REASON_LAUNCH),
            CID2: ("antigravity", "workspace", routing.REASON_FOLDER),
            other: ("antigravity", "vault", routing.REASON_CATCH_ALL),
        })
        self.assertEqual(self.listing(), before)

    def test_first_step_in_the_wal_invalidates_cached_metadata(self):
        path = self.db([], meta=blob(folder="/example/workspace"))
        cfg = settings.indexer(raw={"routing": {"sources": {"antigravity": str(self.root)}}},
                               cfg_dir=self.root)
        cache = self.state / "routing.json"
        sources = {"antigravity": ap.AntigravitySource}
        runs, _ = routing.discover_runs(sources, cfg, cache_path=cache)
        self.assertEqual(runs, [])
        with contextlib.closing(sqlite3.connect(str(path))) as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            self.insert(writer, [turn(T0 + 1), prompt("First prompt", T0 + 2)])
            self.assertTrue(ap.wal_path(path).exists())
            before = self.listing()
            runs, _ = routing.discover_runs(sources, cfg, cache_path=cache)
            self.assertEqual((runs[0].folder, runs[0].start_time), ("/example/workspace", T0 + 1.25))
            self.assertEqual(self.listing(), before)


class Registry(unittest.TestCase):
    def test_antigravity_names_a_path(self):
        t = {"source": "antigravity", "path": "~/.gemini/antigravity-cli/conversations"}
        self.assertIsInstance(refresh.transcript_source(t), ap.AntigravitySource)
        self.assertIs(refresh.agent_sources()["antigravity"], ap.AntigravitySource)
        self.assertEqual(ap.AntigravitySource.default_path, t["path"])
        self.assertEqual(refresh.project_dirs(t),
                         [Path.home() / ".gemini" / "antigravity-cli" / "conversations"])
        with self.assertRaisesRegex(ValueError, "transcripts.path is required when "
                                                "transcripts.source is antigravity"):
            refresh.project_dirs({"source": "antigravity"})

    def test_the_mapped_version_is_recorded(self):
        self.assertRegex(ap.MAPPED_VERSION, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
