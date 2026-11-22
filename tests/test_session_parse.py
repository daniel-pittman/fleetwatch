"""The Claude Code transcript parser on fabricated JSONL: exchanges, cursors and resuming
from an open exchange, replay dropping, automated-session skipping, withholding."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from fleetwatch.memory import session_parse as sp

SID = "aa11bb22-0000-4000-8000-000000000001"
SID2 = "cc33dd44-0000-4000-8000-000000000002"


def line(d):
    # Claude Code writes compact JSON; the parser's byte prefilters depend on it.
    return json.dumps(d, separators=(",", ":")) + "\n"


def user(text, ts, **kw):
    return line(dict(type="user", message=dict(role="user", content=text), timestamp=ts, **kw))


def asst(text, ts, mid, tools=()):
    content = [dict(type="text", text=text)] if text else []
    content += [dict(type="tool_use", name=n, input={}) for n in tools]
    return line(dict(type="assistant", message=dict(id=mid, content=content), timestamp=ts))


def tool_result(ts, text="tool output that is never indexed"):
    return line(dict(type="user", message=dict(content=[dict(type="tool_result", content=text)]),
                     timestamp=ts))


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


class Transcripts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.proj = Path(tmp.name) / "project"
        self.state = Path(tmp.name) / "state"
        self.proj.mkdir()
        self.state.mkdir()
        self.path = self.proj / (SID + ".jsonl")

    def write(self, *lines, name=SID, mode="w"):
        p = self.proj / (name + ".jsonl")
        with open(p, mode) as f:
            f.writelines(lines)
        return p

    def age(self, p):
        old = time.time() - 3600
        os.utime(p, (old, old))

    def load(self, scanner=None, **kw):
        return sp.load_transcripts(self.proj, self.state, scanner or FakeScanner(), **kw)

    def test_exchanges_tools_and_ids(self):
        self.write(user("First question about the gate lantern", "2026-01-02T10:00:00Z"),
                   asst("Looking.", "2026-01-02T10:00:05Z", "m1", tools=["Read"]),
                   tool_result("2026-01-02T10:00:06Z"),
                   asst("The battery is flat.", "2026-01-02T10:00:09Z", "m2", tools=["Read", "Bash"]),
                   user("Second: replace it", "2026-01-02T10:05:00Z"),
                   asst("Replaced.", "2026-01-02T10:05:30Z", "m3"))
        self.age(self.path)
        recs, skipped, st = self.load()
        self.assertEqual([r["id"] for r in recs], ["Saa11bb-EX-0001", "Saa11bb-EX-0002"])
        self.assertEqual(recs[0]["assistant"], ["Looking.", "The battery is flat."])
        self.assertEqual(recs[0]["tools"], {"Read": 2, "Bash": 1})
        self.assertEqual((recs[0]["start_line"], recs[0]["end_line"]), (1, 4))
        self.assertNotIn("tool output", json.dumps(recs))
        self.assertEqual(recs[0]["sender_canon"], "owner")
        self.assertFalse(any(r.get("open") for r in recs))
        self.assertEqual(st, dict(sessions=1, new=2, files_reparsed=1, removed=0, held=0))

    def test_cursor_resumes_from_the_open_exchange(self):
        self.write(user("Plan the irrigation", "2026-02-01T09:00:00Z"),
                   asst("Plan drafted.", "2026-02-01T09:00:10Z", "m1"),
                   user("Now order the parts", "2026-02-01T09:10:00Z"),
                   asst("Ordering.", "2026-02-01T09:10:05Z", "m2"))
        scanner = FakeScanner()
        recs, _, st = self.load(scanner)
        self.assertEqual(st["new"], 2)
        self.assertTrue(recs[-1].get("open"), "a file touched in the last 15 minutes is open")
        cursor = json.loads((self.state / "sessions" / (SID + ".cursor.json")).read_text())
        self.assertEqual(cursor["size"], self.path.stat().st_size)
        self.assertEqual(cursor["next_n"], 3)

        # Unchanged file: nothing parsed or scanned again.
        before = scanner.scanned
        recs, _, st = self.load(scanner)
        self.assertEqual((st["new"], scanner.scanned - before, len(recs)), (0, 0, 2))

        # The open exchange grows and a new one starts: only the tail is reparsed, and the
        # exchange ids do not move.
        self.write(asst("Parts ordered, arriving Friday.", "2026-02-01T09:12:00Z", "m3"),
                   user("Thanks", "2026-02-01T09:20:00Z"),
                   asst("Noted.", "2026-02-01T09:20:02Z", "m4"), mode="a")
        self.age(self.path)
        recs, _, st = self.load(scanner)
        self.assertEqual(st["new"], 2)
        self.assertEqual([r["id"] for r in recs],
                         ["Saa11bb-EX-0001", "Saa11bb-EX-0002", "Saa11bb-EX-0003"])
        self.assertEqual(recs[1]["assistant"], ["Ordering.", "Parts ordered, arriving Friday."])
        self.assertEqual(recs[1]["start_line"], 3)
        self.assertFalse(recs[-1].get("open"))
        stored = sp.read_records(self.state / "sessions" / (SID + ".jsonl"))
        self.assertEqual(len(stored), 3)

    def test_half_written_line_waits(self):
        self.write(user("Check the bees", "2026-02-02T08:00:00Z"),
                   asst("Checked.", "2026-02-02T08:00:03Z", "m1"))
        with open(self.path, "a") as f:
            f.write(user("Partial", "2026-02-02T08:01:00Z")[:-10])
        recs, _, _ = self.load()
        self.assertEqual([r["user_text"] for r in recs], ["Check the bees"])

    def test_scanner_version_change_reparses(self):
        self.write(user("Mulch the beds", "2026-02-03T08:00:00Z"),
                   asst("Mulched.", "2026-02-03T08:00:03Z", "m1"))
        self.load(FakeScanner("v1"))
        scanner = FakeScanner("v2")
        _, _, st = self.load(scanner)
        self.assertEqual((st["new"], scanner.scanned), (1, 1))

    def test_replay_of_a_resumed_session_is_dropped(self):
        self.write(user("Prune the roses", "2026-03-01T07:00:00Z"),
                   asst("Pruned.", "2026-03-01T07:00:04Z", "m1"),
                   # A resumed session replays the earlier prompt with its original timestamp.
                   user("Prune the roses", "2026-03-01T07:00:00Z"),
                   asst("Pruned.", "2026-03-01T07:00:04Z", "m1"),
                   asst("Also swept the cuttings.", "2026-03-01T08:00:00Z", "m9"),
                   user("Water them", "2026-03-01T08:05:00Z"))
        self.age(self.path)
        recs, _, st = self.load()
        self.assertEqual(st["new"], 3, "the replay keeps its own id in state")
        self.assertEqual([r["user_text"] for r in recs], ["Prune the roses", "Water them"])
        self.assertEqual(recs[0]["assistant"], ["Pruned.", "Also swept the cuttings."])
        self.assertEqual(recs[1]["id"], "Saa11bb-EX-0003")

    def test_automated_sessions_are_skipped_and_cached(self):
        self.write(user("Real work on the compost heap", "2026-03-02T07:00:00Z"),
                   asst("On it.", "2026-03-02T07:00:01Z", "m1"))
        self.write(user("Reply with only the word OK", "2026-03-02T07:01:00Z"),
                   asst("OK", "2026-03-02T07:01:01Z", "m2"), name=SID2)
        recs, skipped, st = self.load()
        self.assertEqual(st["sessions"], 1)
        self.assertEqual({r["session"] for r in recs}, {SID})
        self.assertEqual(list(skipped), [SID2])
        cached = json.loads((self.state / "skipped-sessions.json").read_text())
        self.assertEqual(cached[SID2]["size"], (self.proj / (SID2 + ".jsonl")).stat().st_size)
        self.assertEqual(cached[SID2]["reason"], sp.SKIP_AUTOMATED_PREFIX)
        _, _, st = self.load(exclude_automated=False)
        self.assertEqual(st["sessions"], 2)

    def test_a_skipped_session_stores_a_reason_code_never_its_prompt(self):
        self.write(user("Real work on the compost heap", "2026-03-02T07:00:00Z"),
                   asst("On it.", "2026-03-02T07:00:01Z", "m1"))
        self.write(user("Reply with only jdoe@example.org", "2026-03-02T07:01:00Z"),
                   asst("OK", "2026-03-02T07:01:01Z", "m2"), name=SID2)
        self.load()
        text = (self.state / "skipped-sessions.json").read_text()
        self.assertNotIn("jdoe", text)
        self.assertEqual(json.loads(text)[SID2]["reason"], sp.SKIP_AUTOMATED_PREFIX)

    def test_a_stored_prompt_reason_is_rewritten_to_a_code(self):
        self.write(user("Reply with only the word OK", "2026-03-02T07:01:00Z"),
                   asst("OK", "2026-03-02T07:01:01Z", "m2"), name=SID2)
        size = (self.proj / (SID2 + ".jsonl")).stat().st_size
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "skipped-sessions.json").write_text(json.dumps(
            {SID2: {"size": size, "reason": "Reply with only jdoe@example.org"}}))
        self.load()
        cached = json.loads((self.state / "skipped-sessions.json").read_text())
        self.assertEqual(cached[SID2], {"size": size, "reason": sp.SKIP_AUTOMATED_PREFIX})

    def test_withhold_patterns_blank_the_exchange(self):
        self.write(user("Notes on the PRIVATE-HARVEST plan", "2026-03-03T07:00:00Z"),
                   asst("Here is the plan.", "2026-03-03T07:00:01Z", "m1", tools=["Read"]),
                   user("Unrelated: rake leaves", "2026-03-03T07:05:00Z"),
                   asst("Raking.", "2026-03-03T07:05:01Z", "m2"))
        self.age(self.path)
        opts = sp.parse_opts({"withhold_patterns": ["private-harvest"]})
        recs, _, _ = self.load(opts=opts)
        self.assertEqual(recs[0]["user_text"], sp.WITHHELD_TEXT)
        self.assertEqual((recs[0]["assistant"], recs[0]["tools"], recs[0]["withheld"]), ([], {}, True))
        self.assertEqual(recs[1]["user_text"], "Unrelated: rake leaves")
        self.assertNotIn("PRIVATE-HARVEST", (self.state / "sessions" / (SID + ".jsonl")).read_text())
        # Without the pattern the signature differs, so the state is reparsed, not reused.
        self.assertNotEqual(sp.opts_sig(opts), sp.opts_sig(sp.parse_opts({})))

    def test_a_record_without_a_folder_is_not_withheld_by_an_empty_match(self):
        self.write(user("Rake the leaves", "2026-03-03T07:00:00Z"),
                   asst("Raking.", "2026-03-03T07:00:01Z", "m1"))
        self.age(self.path)
        recs, _, _ = self.load(opts=sp.parse_opts({"withhold_patterns": ["^$"]}))
        self.assertEqual(recs[0]["user_text"], "Rake the leaves")
        self.assertNotIn("withheld", recs[0])

    def test_heartbeats_interrupts_and_kinds(self):
        beat = "HEARTBEAT: check every lane and resume what stalled."
        self.write(user(beat, "2026-03-04T07:00:00Z", isMeta=True),
                   asst("All lanes fine.", "2026-03-04T07:00:01Z", "m1"),
                   user(beat, "2026-03-04T07:30:00Z", isMeta=True),
                   asst("Still fine.", "2026-03-04T07:30:01Z", "m2"),
                   user("Stop that", "2026-03-04T07:31:00Z"),
                   user("[Request interrupted by user]", "2026-03-04T07:31:05Z"),
                   user('<cross-session-message from-name="peer-one">hello</cross-session-message>',
                        "2026-03-04T07:40:00Z"))
        self.age(self.path)
        opts = sp.parse_opts({"kinds": {"heartbeat": "cron"}})
        recs, _, _ = self.load(opts=opts)
        self.assertEqual([r["sender_kind"] for r in recs], ["cron", "cron", "owner", "peer-session"])
        self.assertEqual([r["sender_canon"] for r in recs],
                         ["heartbeat", "heartbeat", "owner", "peer-session"])
        self.assertTrue(recs[1]["user_text"].startswith(
            "[Heartbeat prompt identical to Saa11bb-EX-0001] HEARTBEAT"), recs[1]["user_text"])
        self.assertTrue(recs[2]["interrupted"])
        self.assertEqual((recs[3]["sender_name"], recs[3]["user_text"]), ("peer-one", "hello"))


if __name__ == "__main__":
    unittest.main()
