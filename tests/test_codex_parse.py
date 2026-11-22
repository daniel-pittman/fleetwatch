"""The Codex rollout source on fabricated rollouts: exchange pairing, injected context,
tool names, incremental reads of a growing plain file, compressed files read once, a session
in both forms counted once, malformed lines, and the registry wiring."""
import contextlib
import io
import json
import os
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
        self.assertEqual([r["id"] for r in recs], ["S03b19a-EX-0001", "S03b19a-EX-0002"])
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
        self.assertEqual([r["id"] for r in recs], ["S03b19a-EX-0001", "S03b19a-EX-0002"])
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
        self.assertEqual([r["id"] for r in recs], ["S03b19a-EX-0001", "S03b19a-EX-0002"])

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
        self.assertRegex(text.splitlines()[0], r"^# S03b19a-EX-0001 .* owner ~/git/demo/widgets$")
        meta_ = render.parse_exchange(text, recs[0]["id"])
        self.assertEqual((meta_["provider"], meta_["folder"]), ("codex", "~/git/demo/widgets"))
        self.assertEqual(meta_["session"], SID)
        claude = dict(recs[0], provider=None)
        self.assertNotIn("Source: ", render.render_exchange(claude, None, None, {}, []))


class Scrubbing(unittest.TestCase):
    """The folder goes through the same scan as the prompt: nothing bypasses it."""

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

    def test_the_codex_tags_template_names_codex(self):
        self.assertIn("Codex CLI", index_cli.CODEX_TAGS_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
