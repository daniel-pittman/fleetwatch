"""The Codex rollout source on fabricated rollouts: exchange pairing, injected context,
tool names, incremental reads of a growing plain file, compressed files read once, a session
in both forms counted once, malformed lines, and the registry wiring."""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from fleetwatch import index_cli
from fleetwatch.memory import codex_parse, refresh, render, session_parse as sp
from fleetwatch.memory.privacy_scan import RuleSet, Scanner

SID = "01a1fbfa-efba-7173-bb5c-6fc0b903b19a"
SID2 = "01a1fbfb-0000-7000-8000-00000000abcd"
HAVE_ZSTD = shutil.which("zstd") is not None
HOME = str(Path.home())
SLACK = "xo" + "xb-123456789012-abcdefABCDEF"      # a made-up token shape


def rec(typ, payload, ts):
    return json.dumps(dict(timestamp=ts, ordinal=0, type=typ, payload=payload),
                      separators=(",", ":")) + "\n"


def meta(sid=SID, cwd=HOME + "/git/demo/widgets", ts="2026-03-01T10:00:00.000Z"):
    return rec("session_meta", dict(id=sid, session_id=sid, timestamp=ts, cwd=cwd,
                                    originator="codex_cli_rs", cli_version="0.1.0"), ts)


def msg(role, text, ts, kind=None):
    kind = kind or ("output_text" if role == "assistant" else "input_text")
    return rec("response_item", dict(type="message", role=role,
                                     content=[dict(type=kind, text=text)]), ts)


def call(name, ts, custom=False):
    return rec("response_item", dict(type="custom_tool_call" if custom else "function_call",
                                     name=name, call_id="c1", arguments="{}", input="{}"), ts)


def output(ts, text="tool output that is never indexed", custom=False):
    return rec("response_item", dict(type="custom_tool_call_output" if custom
                                     else "function_call_output", call_id="c1", output=text), ts)


def reasoning(ts):
    return rec("response_item", dict(type="reasoning", summary=[], encrypted_content="reasoning-secret"), ts)


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


PACK = """name: synthetic
anon_ids:
  - pattern: '\\bL\\d{2}\\b'
    pairing: '\\bL\\d{2}\\b\\s*(?:is|=)\\s+([A-Z][a-z]+)'
"""


def real_scanner(test, deny=(), allow="version: 1\n"):
    """A real Scanner (spaCy names, the synthetic anonymous-id pack) with a denylist of `deny`."""
    import hashlib
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    d = Path(tmp.name)
    (d / "allow.yaml").write_text(allow)
    (d / "pack.yaml").write_text(PACK)
    (d / "deny.txt").write_text("".join(hashlib.sha256(w.encode()).hexdigest() + "\n" for w in deny))
    return Scanner(d / "allow.yaml", d / "deny.txt", RuleSet([str(d / "pack.yaml")]))


def session_lines(sid=SID, ts="2026-03-01T10:00:00.000Z"):
    return [meta(sid, ts=ts),
            msg("developer", "developer instructions never indexed", ts),
            msg("user", "# AGENTS.md instructions for /x\n\n<INSTRUCTIONS>never indexed</INSTRUCTIONS>", ts),
            msg("user", "<environment_context>\n  <cwd>/x</cwd>\n</environment_context>", ts),
            msg("user", "Why does the widget count drift?", "2026-03-01T10:00:01.000Z"),
            reasoning("2026-03-01T10:00:02.000Z"),
            call("shell_command", "2026-03-01T10:00:03.000Z"),
            output("2026-03-01T10:00:04.000Z"),
            call("exec", "2026-03-01T10:00:05.000Z", custom=True),
            output("2026-03-01T10:00:06.000Z", custom=True),
            call("shell_command", "2026-03-01T10:00:07.000Z"),
            msg("assistant", "Checking the counter.", "2026-03-01T10:00:08.000Z"),
            msg("assistant", "The counter resets on reload.", "2026-03-01T10:00:09.000Z"),
            msg("user", "Fix it", "2026-03-01T10:05:00.000Z"),
            msg("assistant", "Fixed.", "2026-03-01T10:05:30.000Z")]


class Codex(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "sessions"
        self.state = Path(tmp.name) / "state"
        self.day = self.root / "2026" / "03" / "01"
        self.day.mkdir(parents=True)
        self.state.mkdir()

    def plain(self, sid=SID, ts="2026-03-01T10-00-00"):
        return self.day / ("rollout-%s-%s.jsonl" % (ts, sid))

    def write(self, lines, path=None, mode="w"):
        path = path or self.plain()
        with open(path, mode) as f:
            f.writelines(lines)
        return path

    def compress(self, path):
        subprocess.run(["zstd", "-q", "--rm", "-f", str(path)], check=True)
        return Path(str(path) + ".zst")

    def age(self, p):
        old = time.time() - 3600
        os.utime(p, (old, old))

    def load(self, scanner=None, source=None, **kw):
        source = source or codex_parse.CodexSource()
        recs, skipped, st = sp.load_transcripts(self.root, self.state, scanner or FakeScanner(),
                                                source=source, **kw)
        return recs, st

    def test_user_and_assistant_pair_into_exchanges(self):
        p = self.write(session_lines())
        self.age(p)
        recs, st = self.load()
        self.assertEqual([r["user_text"] for r in recs], ["Why does the widget count drift?", "Fix it"])
        self.assertEqual(recs[0]["assistant"], ["Checking the counter.", "The counter resets on reload."])
        self.assertEqual(recs[1]["assistant"], ["Fixed."])
        self.assertEqual([r["id"] for r in recs], ["Sc0b903b19a-EX-0001", "Sc0b903b19a-EX-0002"])
        self.assertEqual(recs[0]["session"], SID)
        self.assertEqual((recs[0]["ts"], recs[0]["start_line"], recs[0]["end_line"]),
                         ("2026-03-01T10:00:01.000Z", 5, 12 + 1))
        self.assertEqual(recs[0]["sender_canon"], "owner")
        self.assertEqual((recs[0]["provider"], recs[0]["cwd"]), ("codex", "~/git/demo/widgets"))
        self.assertEqual(st["files_read"], {"plain": 1, "zst": 0})

    def test_injected_context_and_developer_messages_are_dropped(self):
        p = self.write(session_lines())
        self.age(p)
        recs, _ = self.load()
        blob = json.dumps(recs)
        for leaked in ("AGENTS.md", "environment_context", "developer instructions", "never indexed"):
            self.assertNotIn(leaked, blob)

    def test_a_message_mixing_injected_blocks_and_text_keeps_the_text(self):
        content = [dict(type="input_text", text="<environment_context>\n</environment_context>"),
                   dict(type="input_text", text="Real question"),
                   dict(type="input_image", image_url="data:image/png;base64,AAAA")]
        p = self.write([meta(), rec("response_item", dict(type="message", role="user", content=content),
                                    "2026-03-01T10:00:01.000Z")])
        self.age(p)
        recs, _ = self.load()
        self.assertEqual([r["user_text"] for r in recs], ["Real question"])

    def test_tool_names_are_kept_and_tool_output_is_dropped(self):
        p = self.write(session_lines())
        self.age(p)
        recs, _ = self.load()
        self.assertEqual(recs[0]["tools"], {"shell_command": 2, "exec": 1})
        blob = json.dumps(recs)
        self.assertNotIn("tool output", blob)
        self.assertNotIn("reasoning-secret", blob)

    def test_an_aborted_turn_marks_the_exchange_interrupted(self):
        p = self.write([meta(), msg("user", "Run the long job", "2026-03-01T10:00:01.000Z"),
                        rec("event_msg", dict(type="turn_aborted", reason="interrupted"),
                            "2026-03-01T10:00:02.000Z")])
        self.age(p)
        recs, _ = self.load()
        self.assertTrue(recs[0]["interrupted"])

    def test_a_sub_agent_message_is_a_subagent_hand_back(self):
        content = [dict(type="input_text", text="Message Type: MESSAGE\nreview done")]
        p = self.write([meta(), msg("user", "Review it", "2026-03-01T10:00:01.000Z"),
                        rec("response_item", dict(type="agent_message", author="/root/reviewer",
                                                  recipient="/root", content=content),
                            "2026-03-01T10:00:09.000Z")])
        self.age(p)
        recs, _ = self.load()
        self.assertEqual([(r["sender_canon"], r["sender_name"]) for r in recs],
                         [("owner", "User"), ("subagent", "/root/reviewer")])

    def test_a_malformed_line_is_skipped_without_failing_the_file(self):
        lines = session_lines()
        lines.insert(6, "{this is not json\n")
        lines.insert(7, '{"type":"response_item","payload":"not an object"}\n')
        p = self.write(lines)
        self.age(p)
        recs, st = self.load()
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["assistant"][-1], "The counter resets on reload.")
        self.assertEqual(st["files_failed"], [])

    def test_a_growing_plain_file_is_read_incrementally(self):
        p = self.write(session_lines()[:12])
        scanner = FakeScanner()
        recs, st = self.load(scanner)
        self.assertEqual((len(recs), st["new"], st["files_read"]["plain"]), (1, 1, 1))
        self.assertTrue(recs[0].get("open"))

        # Unchanged: the file is not opened and nothing is scanned.
        before = scanner.scanned
        recs, st = self.load(scanner)
        self.assertEqual((st["new"], scanner.scanned - before, st["files_read"]["plain"]), (0, 0, 0))

        # Grown: only the open exchange onward is read, and ids do not move.
        self.write(session_lines()[12:], mode="a")
        self.age(p)
        recs, st = self.load(scanner)
        self.assertEqual(st["files_read"]["plain"], 1)
        self.assertEqual([r["id"] for r in recs], ["Sc0b903b19a-EX-0001", "Sc0b903b19a-EX-0002"])
        self.assertEqual(recs[0]["assistant"], ["Checking the counter.", "The counter resets on reload."])
        self.assertEqual(recs[1]["user_text"], "Fix it")
        stored = sp.read_records(self.state / "sessions" / (SID + ".jsonl"))
        self.assertEqual(len(stored), 2)
        self.assertEqual(stored[1]["cwd"], "~/git/demo/widgets")

    def test_a_half_written_line_waits(self):
        p = self.write(session_lines()[:12])
        with open(p, "a") as f:
            f.write(msg("user", "Partial", "2026-03-01T10:01:00.000Z")[:-12])
        recs, _ = self.load()
        self.assertEqual([r["user_text"] for r in recs], ["Why does the widget count drift?"])

    def test_a_session_without_exchanges_is_not_reread(self):
        self.write([meta(), msg("user", "<environment_context>\n</environment_context>",
                                "2026-03-01T10:00:01.000Z")])
        recs, st = self.load()
        self.assertEqual((recs, st["files_read"]["plain"]), ([], 1))
        recs, st = self.load()
        self.assertEqual(st["files_read"]["plain"], 0)

    def test_a_scan_version_change_reparses(self):
        self.age(self.write(session_lines()))
        self.load(FakeScanner("v1"))
        scanner = FakeScanner("v2")
        _, st = self.load(scanner)
        self.assertEqual((st["new"], scanner.scanned), (2, 2))

    @unittest.skipUnless(HAVE_ZSTD, "the zstd CLI is not installed")
    def test_a_compressed_file_is_read_once(self):
        z = self.compress(self.write(session_lines()))
        self.age(z)
        recs, st = self.load()
        self.assertEqual([r["user_text"] for r in recs], ["Why does the widget count drift?", "Fix it"])
        self.assertEqual(recs[0]["tools"], {"shell_command": 2, "exec": 1})
        self.assertEqual(recs[0]["cwd"], "~/git/demo/widgets")
        self.assertEqual(st["files_read"], {"plain": 0, "zst": 1})
        recs, st = self.load()
        self.assertEqual((len(recs), st["new"], st["files_read"]), (2, 0, {"plain": 0, "zst": 0}))

    @unittest.skipUnless(HAVE_ZSTD, "the zstd CLI is not installed")
    def test_a_session_in_both_forms_counts_once(self):
        plain = self.write(session_lines())
        copy = self.root / "2026" / "03" / "02"
        copy.mkdir()
        both = copy / plain.name
        shutil.copy(plain, both)
        self.compress(both)
        self.age(plain)
        recs, st = self.load()
        self.assertEqual((len(recs), st["sessions"], st["files_read"]), (2, 1, {"plain": 1, "zst": 0}))

    @unittest.skipUnless(HAVE_ZSTD, "the zstd CLI is not installed")
    def test_a_plain_file_compressed_later_is_read_again_from_the_start(self):
        p = self.write(session_lines())
        self.age(p)
        self.load()
        z = self.compress(p)
        self.age(z)
        recs, st = self.load()
        self.assertEqual((len(recs), st["files_read"]), (2, {"plain": 0, "zst": 1}))
        self.assertEqual([r["id"] for r in recs], ["Sc0b903b19a-EX-0001", "Sc0b903b19a-EX-0002"])

    @unittest.skipUnless(HAVE_ZSTD, "the zstd CLI is not installed")
    def test_a_truncated_compressed_file_is_reported_and_retried(self):
        z = self.compress(self.write(session_lines()))
        self.age(z)
        good = z.read_bytes()
        z.write_bytes(good[:len(good) // 2])
        other = self.write(session_lines(SID2), self.plain(SID2, "2026-03-01T11-00-00"))
        self.age(other)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            recs, st = self.load()
        self.assertIn("premature end", err.getvalue())
        self.assertEqual([r["session"] for r in recs], [SID2, SID2])
        self.assertEqual(st["files_failed"], [z.name])
        z.write_bytes(good)
        recs, st = self.load()
        self.assertEqual((len(recs), st["files_failed"]), (4, []))

    def test_a_missing_zstd_fails_with_a_clear_message(self):
        z = self.day / ("rollout-2026-03-01T10-00-00-%s.jsonl.zst" % SID)
        z.write_bytes(b"not read")
        real = codex_parse.shutil.which
        codex_parse.shutil.which = lambda name: None
        self.addCleanup(setattr, codex_parse.shutil, "which", real)
        with self.assertRaisesRegex(codex_parse.ZstdError, "zstd is not installed"):
            self.load()

    def test_sessions_list_limits_the_index(self):
        self.age(self.write(session_lines()))
        self.age(self.write(session_lines(SID2), self.plain(SID2, "2026-03-01T11-00-00")))
        recs, st = self.load(sessions=[SID2])
        self.assertEqual({r["session"] for r in recs}, {SID2})

    def test_exchange_ids_differ_for_sessions_started_together(self):
        a = "01a1fbfa-0000-7000-8000-0000000aaaaa"
        b = "01a1fbfa-0000-7000-8000-0000000bbbbb"
        self.age(self.write(session_lines(a), self.plain(a, "2026-03-01T10-00-00")))
        self.age(self.write(session_lines(b), self.plain(b, "2026-03-01T10-00-01")))
        recs, _ = self.load()
        self.assertEqual(len({r["id"] for r in recs}), 4)

    def test_withheld_exchange_loses_its_folder(self):
        self.age(self.write(session_lines()))
        recs, _ = self.load(opts=sp.parse_opts({"withhold_patterns": ["counter"]}))
        self.assertTrue(recs[0]["withheld"])
        self.assertNotIn("cwd", recs[0])
        self.assertEqual(recs[1]["cwd"], "~/git/demo/widgets")

    def test_the_folder_renders_and_reads_back(self):
        self.age(self.write(session_lines()))
        recs, _ = self.load()
        text = render.render_exchange(recs[0], None, recs[1]["id"], {}, [])
        self.assertIn("Source: codex | Folder: ~/git/demo/widgets\n", text)
        self.assertRegex(text.splitlines()[0], r"^# Sc0b903b19a-EX-0001 .* owner ~/git/demo/widgets$")
        meta_ = render.parse_exchange(text, recs[0]["id"])
        self.assertEqual((meta_["provider"], meta_["folder"]), ("codex", "~/git/demo/widgets"))
        self.assertEqual(meta_["session"], SID)
        claude = dict(recs[0], provider=None)
        self.assertNotIn("Source: ", render.render_exchange(claude, None, None, {}, []))


    # ---------------------------------------------------------------- fix round

    def test_exchange_ids_carry_ten_session_characters(self):
        a = "01a1fbfa-0000-7000-8000-123456abcdef"
        b = "01a1fbfb-1111-7111-8111-654321abcdef"       # same last six characters as `a`
        self.age(self.write(session_lines(a), self.plain(a, "2026-03-01T10-00-00")))
        self.age(self.write(session_lines(b), self.plain(b, "2026-03-01T10-00-01")))
        recs, _ = self.load()
        ids = [r["id"] for r in recs]
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual(sorted(ids)[0], "S3456abcdef-EX-0001")
        self.assertEqual(sp.ClaudeSource().id_stem("a1b2c3d4-0000"), "a1b2c3")
        self.assertEqual(sp.exchange_id("abcdef123", "EX", 7, sp.DEFAULT_OPTS), "Sabcdef-EX-0007")

    def test_the_search_id_patterns_accept_the_longer_stem(self):
        from fleetwatch import indexes
        from fleetwatch.memory import session_search
        cfg = indexes.session_config("x", {"group": "research", "owner_session": "X"}, refresh.SETTINGS)
        for ex_id in ("S3456abcdef-EX-0001", "Sa1b2c3-EX-0001", "Sa1b2c3-AQ-0012"):
            self.assertTrue(session_search.ID_RE.match(ex_id), ex_id)
            self.assertTrue(re.search(cfg["exact_heading_pattern"], "# %s owner" % ex_id), ex_id)

    def test_a_plain_file_replaced_by_a_damaged_compressed_one_keeps_its_records(self):
        p = self.write(session_lines())
        self.age(p)
        recs, _ = self.load()
        stored = self.state / "sessions" / (SID + ".jsonl")
        cursor = self.state / "sessions" / (SID + ".cursor.json")
        before = (stored.read_text(), cursor.read_text())
        z = self.compress(p)
        good = z.read_bytes()
        z.write_bytes(good[:len(good) // 2])
        self.age(z)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            recs, st = self.load()
        self.assertEqual([r["user_text"] for r in recs], ["Why does the widget count drift?", "Fix it"])
        self.assertEqual(st["files_failed"], [z.name])
        self.assertEqual((stored.read_text(), cursor.read_text()), before)
        z.write_bytes(good)
        recs, st = self.load()
        self.assertEqual((len(recs), st["files_failed"]), (2, []))

    def test_a_final_line_without_a_newline_is_read_when_it_is_complete(self):
        lines = session_lines()
        lines[-1] = lines[-1].rstrip("\n")
        p = self.write(lines)
        self.age(p)
        recs, _ = self.load()
        self.assertEqual(recs[1]["assistant"], ["Fixed."])
        self.write(["\n"], mode="a")
        self.age(p)
        recs, st = self.load()
        self.assertEqual((len(recs), recs[1]["assistant"]), (2, ["Fixed."]))

    def test_a_cut_off_final_line_is_read_once_it_is_finished(self):
        p = self.write(session_lines()[:12])
        with open(p, "a") as f:
            f.write(msg("assistant", "Late answer", "2026-03-01T10:00:10.000Z")[:-20])
        self.age(p)
        recs, _ = self.load()
        self.assertEqual(recs[0]["assistant"], ["Checking the counter."])
        with open(p, "a") as f:
            f.write(msg("assistant", "Late answer", "2026-03-01T10:00:10.000Z")[-20:])
        self.age(p)
        recs, _ = self.load()
        self.assertEqual(recs[0]["assistant"][-1], "Late answer")

    def test_an_append_between_end_of_file_and_the_size_check_is_not_lost(self):
        p = self.write(session_lines()[:12])
        self.age(p)
        late = msg("assistant", "Late answer.", "2026-03-01T10:00:10.000Z")
        real = codex_parse.Rollout.__exit__
        fired = []

        def exit_and_append(inner, *a):
            real(inner, *a)                      # the read has reached end of file and closed
            if not fired and inner.path == p:
                fired.append(1)
                with open(p, "a") as f:
                    f.write(late)

        codex_parse.Rollout.__exit__ = exit_and_append
        try:
            self.load()
        finally:
            codex_parse.Rollout.__exit__ = real
        self.assertTrue(fired)
        recs, _ = self.load()
        self.assertEqual(recs[0]["assistant"][-1], "Late answer.")

    def test_malformed_records_are_skipped_and_counted(self):
        bad = ['["response_item"]\n', '"just a string"\n', "42\n", "\n",
               rec("response_item", dict(type="message", role="assistant",
                                         content=[dict(type="output_text", text=None)]),
                   "2026-03-01T10:00:08.500Z"),
               rec("response_item", dict(type="message", role="assistant", content=5),
                   "2026-03-01T10:00:08.600Z"),
               rec("response_item", dict(type="message", role="assistant",
                                         content=[None, 7, dict(type="output_text", text=3)]),
                   "2026-03-01T10:00:08.700Z"),
               rec("response_item", dict(type="message", role="user",
                                         content=[dict(type="input_text", text="Bad stamp")]),
                   "not a time"),
               rec("response_item", dict(type="message", role="user",
                                         content=[dict(type="input_text", text="Numeric stamp")]),
                   12345),
               rec("response_item", dict(type="function_call", name=None), "2026-03-01T10:00:08.800Z")]
        lines = session_lines()
        lines[12:12] = bad
        p = self.write(lines)
        self.age(p)
        other = self.write(session_lines(SID2), self.plain(SID2, "2026-03-01T11-00-00"))
        self.age(other)
        recs, st = self.load()
        mine = [r for r in recs if r["session"] == SID]
        self.assertEqual([r["user_text"] for r in mine], ["Why does the widget count drift?", "Fix it"])
        self.assertEqual(len([r for r in recs if r["session"] == SID2]), 2)
        self.assertEqual(st["files_failed"], [])
        self.assertEqual(st["lines_skipped"], 3)
        for r in recs:                                 # every record renders
            render.render_exchange(r, None, None, {}, [])

    def test_a_file_that_raises_does_not_stop_the_others(self):
        p = self.write(session_lines())
        self.age(p)
        other = self.write(session_lines(SID2), self.plain(SID2, "2026-03-01T11-00-00"))
        self.age(other)
        real = codex_parse.parse

        def boom(path, *a, **k):
            if SID in str(path):
                raise RuntimeError("unexpected")
            return real(path, *a, **k)

        codex_parse.parse = boom
        self.addCleanup(setattr, codex_parse, "parse", real)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            recs, st = self.load()
        self.assertEqual({r["session"] for r in recs}, {SID2})
        self.assertEqual(st["files_failed"], [p.name])

    @unittest.skipUnless(HAVE_ZSTD, "the zstd CLI is not installed")
    def test_a_file_compressed_during_the_refresh_is_left_for_the_next_one(self):
        p = self.write(session_lines())
        self.age(p)
        self.load()

        class Racing(codex_parse.CodexSource):
            def list(inner, *a):
                found = super().list(*a)
                test.age(test.compress(p))             # Codex compresses it after the listing
                return found

        test = self
        recs, st = self.load(source=Racing())
        self.assertEqual([r["user_text"] for r in recs], ["Why does the widget count drift?", "Fix it"])
        recs, st = self.load()
        self.assertEqual((len(recs), st["files_read"]), (2, {"plain": 0, "zst": 1}))

    def test_anon_id_pairing_with_the_folder_survives_a_growing_file(self):
        scanner = real_scanner(self, deny=["torvane"])
        lines = [meta(cwd=HOME + "/work/torvane"),
                 msg("user", "Check L12 now", "2026-03-01T10:00:01.000Z"),
                 msg("assistant", "Done.", "2026-03-01T10:00:02.000Z")]
        p = self.write(lines)
        self.age(p)
        recs, _ = self.load(scanner)
        self.assertEqual(recs[0].get("dropped"), "anon-id-pairing")
        self.write([msg("assistant", "More.", "2026-03-01T10:00:03.000Z"),
                    msg("user", "Next L12", "2026-03-01T10:01:00.000Z")], mode="a")
        self.age(p)
        recs, _ = self.load(scanner)
        stored = sp.read_records(self.state / "sessions" / (SID + ".jsonl"))
        self.assertEqual([r.get("dropped") for r in stored], ["anon-id-pairing"] * 2)
        self.assertNotIn("torvane", (self.state / "sessions" / (SID + ".jsonl")).read_text().lower())

    def test_withhold_patterns_also_match_the_folder(self):
        self.age(self.write(session_lines()))
        recs, _ = self.load(opts=sp.parse_opts({"withhold_patterns": ["demo/widgets"]}))
        self.assertEqual([r.get("withheld") for r in recs], [True, True])
        self.assertTrue(all("cwd" not in r for r in recs))

    @unittest.skipUnless(HAVE_ZSTD, "the zstd CLI is not installed")
    def test_the_session_meta_of_a_large_compressed_file_reads_cleanly(self):
        filler = [msg("assistant", "x" * 900, "2026-03-01T10:00:%02d.000Z" % (i % 60))
                  for i in range(4000)]
        z = self.compress(self.write([meta()] + filler))
        self.assertEqual(codex_parse.read_meta(z)["cwd"], HOME + "/git/demo/widgets")
        self.assertGreater(z.stat().st_size, 0)
        big = self.write([meta()] + [msg("assistant", os.urandom(400).hex(), "2026-03-01T10:00:01.000Z")
                                     for i in range(4000)], self.plain(SID2, "2026-03-01T11-00-00"))
        z = self.compress(big)
        self.assertGreater(z.stat().st_size, 1 << 20)
        self.assertEqual(codex_parse.read_meta(z)["id"], SID)


class Scrubbing(unittest.TestCase):
    """The folder goes through the same scan as the prompt: nothing bypasses it."""

    def scanner(self):
        return real_scanner(self)

    def record(self, **tools):
        return dict(user_text="Why does the build fail?", assistant=["Run it again."],
                    sender_name="User", tools=dict(tools))

    def test_tool_names_are_scrubbed_like_any_other_text(self):
        leak = "mcp__leak_zed.person@example.org-" + SLACK
        r = self.record(**{leak: 1, "shell_command": 2, "bad\nname": 1, "other\nname": 1})
        r["tools"]["tab\tname"] = 1
        self.scanner().scan_records([r])
        blob = json.dumps(r)
        self.assertNotIn("example.org", blob)
        self.assertNotIn(SLACK[:20], blob)
        self.assertEqual(r["tools"]["shell_command"], 2)
        self.assertTrue(all("\n" not in k and "\t" not in k for k in r["tools"]), r["tools"])
        self.assertIn("bad name", r["tools"])
        self.assertGreaterEqual(r["scan"].get("email", 0), 1)

    def test_names_that_scrub_to_the_same_text_add_their_counts(self):
        a = "mcp__x-" + SLACK
        b = "mcp__x-" + SLACK.replace("1234", "9876")
        r = self.record(**{a: 2, b: 3})
        self.scanner().scan_records([r])
        self.assertEqual(sum(r["tools"].values()), 5)
        self.assertEqual(len(r["tools"]), 1)

    def test_ordinary_tool_names_come_out_unchanged_and_the_record_is_byte_identical(self):
        names = ("Bash", "Read", "Edit", "Task", "WebFetch", "shell_command", "exec", "apply_patch",
                 "mcp__plugin_figma_figma__authenticate", "mcp__zenhub__create_issue",
                 "mcp__slack-msu__conversations_history", "functions.exec_command")
        scanner = self.scanner()
        a = self.record(**{n: i + 1 for i, n in enumerate(names)})
        b = self.record(**{n: i + 1 for i, n in enumerate(names)})
        c = self.record()                              # the same record with no tools to scan
        scanner.scan_records([a, c])
        self.assertEqual(list(a["tools"].items()), list(b["tools"].items()))
        self.assertEqual(json.dumps({k: v for k, v in a.items() if k != "tools"}),
                         json.dumps({k: v for k, v in c.items() if k != "tools"}))
        self.assertEqual(a["scan"], c["scan"])

    def test_a_claude_record_gets_the_same_treatment(self):
        r = self.record(**{"mcp__a_jdoe@example.org": 1, "Bash": 1})
        r.pop("provider", None)
        self.scanner().scan_records([r])
        self.assertNotIn("jdoe", json.dumps(r))
        self.assertEqual(r["tools"]["Bash"], 1)

    def test_the_real_scanner_scrubs_the_folder_and_the_text(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        allow = Path(tmp.name) / "allow.yaml"
        allow.write_text("version: 1\nemails: []\nstaff: []\nnames: []\n")
        scanner = Scanner(allow, None, RuleSet([]))
        r = dict(user_text="Mail me at someone@example.org", assistant=["done"],
                 sender_name="User", cwd="~/work/mail-someone@example.org/app")
        scanner.scan_records([r])
        self.assertNotIn("someone@example.org", json.dumps(r))
        self.assertEqual(r["assistant"], ["done"])


class Registry(unittest.TestCase):
    def test_source_defaults_to_claude_and_codex_names_a_path(self):
        self.assertIsInstance(refresh.transcript_source({"project_dir": "~/x"}),
                              sp.ClaudeSource)
        t = {"source": "codex", "path": "~/.codex/sessions"}
        self.assertIsInstance(refresh.transcript_source(t), codex_parse.CodexSource)
        self.assertEqual(refresh.project_dirs(t), [Path.home() / ".codex" / "sessions"])

    def test_bad_registry_entries_fail_clearly(self):
        with self.assertRaisesRegex(ValueError, "transcripts.source must be one of claude, codex"):
            refresh.transcript_source({"source": "gemini"})
        with self.assertRaisesRegex(ValueError, "transcripts.path is required"):
            refresh.project_dirs({"source": "codex"})
        with self.assertRaisesRegex(ValueError, "transcripts.source must be one of claude, codex"):
            refresh.project_dirs({"source": "gemini", "path": "~/x"})

    def test_the_codex_tags_template_names_codex(self):
        self.assertIn("Codex CLI", index_cli.CODEX_TAGS_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
