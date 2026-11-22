"""Tests for `fleetwatch launch record`: command matching, ledger output, --worktree resolution,
status-file session name lookup, and silence plus exit 0 on bad input."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")

from fleetwatch import launch

REPO = Path(__file__).resolve().parent.parent
SID = "aa11bb22-0000-4000-8000-000000000001"
FIXED_NOW = datetime(2026, 4, 10, 14, 30, 0, tzinfo=timezone.utc)


class CommandMatching(unittest.TestCase):
    def test_recognized_commands_match(self):
        cases = [
            ('codex exec "fix the counter"', "codex", None),
            ('/opt/bin/codex -m o3 exec --full-auto "fix"', "codex", None),
            ('FOO=1 codex exec --worktree /Users/alex/wt "fix"', "codex", "/Users/alex/wt"),
            ('node /Users/alex/scripts/codex-companion.mjs task "build it"', "codex-companion", None),
            ('node "$PLUGIN/scripts/codex-companion.mjs" review --base main', "codex-companion", None),
            ('node codex-companion.mjs adversarial-review --worktree=../wt-1', "codex-companion", "../wt-1"),
            ('/Users/alex/bin/codex-companion.mjs task "direct"', "codex-companion", None),
            ('bash scripts/codex-task-in-worktree.sh --worktree /Users/alex/wt-a "go"', "codex-task-in-worktree", "/Users/alex/wt-a"),
            ('./codex-task-in-worktree.sh "go"', "codex-task-in-worktree", None),
            ('sh /Users/alex/bin/multi-review.sh --worktree /Users/alex/wt-b', "multi-review", "/Users/alex/wt-b"),
            ('multi-review.sh', "multi-review", None),
            ('agy "summarize this repo"', "agy", None),
            ('/usr/local/bin/agy --worktree /Users/alex/wt-c "run"', "agy", "/Users/alex/wt-c"),
            ('node /Users/alex/agy-companion.mjs task "do x"', "agy-companion", None),
            ('node agy-companion.mjs review --base main', "agy-companion", None),
            ('node agy-companion.mjs plan "design y"', "agy-companion", None),
            ('node agy-companion.mjs ask "why?"', "agy-companion", None),
            ('agy-companion.mjs task --worktree /Users/alex/wt-d "direct"', "agy-companion", "/Users/alex/wt-d"),
            ('cd /Users/alex/repo && codex exec "chained"', "codex", None),
            ('env BAR=2 timeout 300 codex exec "wrapped"', "codex", None),
            ('bash -c \'codex exec "nested"\'', "codex", None),
        ]
        for cmd, want_tool, want_wt in cases:
            with self.subTest(cmd=cmd):
                self.assertEqual(launch.parse_command(cmd), (want_tool, want_wt))

    def test_multiline_and_heredoc_commands_match(self):
        cmd = "cd /Users/alex/repo\ncodex exec - <<'EOF'\nKeep going after the first line\nEOF\n"
        self.assertEqual(launch.parse_command(cmd), ("codex", None))

    def test_non_launch_commands_do_not_match(self):
        non_matches = [
            "git status",
            'echo "codex exec"',
            'grep "codex-companion.mjs task" README.md',
            "cat scripts/codex-task-in-worktree.sh",
            'git commit -m "support agy and codex exec"',
            "codex --version",
            "codex login",
            "codex --help exec",
            "node codex-companion.mjs status",
            "node codex-companion.mjs result job-1",
            "node agy-companion.mjs status",
            "node agy-companion.mjs cancel job-1",
            'node -e "console.log(\'agy-companion.mjs task\')"',
            "",
            "   ",
        ]
        for cmd in non_matches:
            with self.subTest(cmd=cmd):
                self.assertIsNone(launch.parse_command(cmd))


class RecordLedger(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.base = Path(t.name).resolve()
        self.state = self.base / "state"
        self.ledger = self.state / "fleetwatch" / "launches.jsonl"
        self.status = self.base / "status"
        self.status.mkdir(parents=True)

    def _run(self, raw_stdin, extra=()):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--ledger", str(self.ledger), "--status-dir", str(self.status), *extra]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("fleetwatch.indexes.calling_session", return_value=("Billing API", SID)):
            code = launch.record_main(argv, stdin=io.StringIO(raw_stdin), now=FIXED_NOW)
        return code, out.getvalue(), err.getvalue()

    def test_records_match_with_status_session_name_and_resolved_worktree(self):
        (self.status / "billing-api.json").write_text(json.dumps({
            "session": "Billing API",
            "sessionId": SID,
            "updated": "2026-04-10T14:25:00Z",
        }))
        payload = {
            "session_id": SID,
            "cwd": "/Users/alex/code/billing",
            "tool_name": "Bash",
            "tool_input": {"command": 'node codex-companion.mjs task --worktree ../wt-billing "fix"'},
        }
        code, out, err = self._run(json.dumps(payload))
        self.assertEqual((code, out, err), (0, "", ""))
        entries = launch.read_ledger(str(self.ledger))
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["session_id"], SID)
        self.assertEqual(e["session_name"], "Billing API")
        self.assertEqual(e["tool"], "codex-companion")
        self.assertEqual(e["cwd"], "/Users/alex/code/wt-billing")
        self.assertEqual(e["ts"], "2026-04-10T14:30:00+00:00")
        self.assertEqual(self.ledger.stat().st_mode & 0o077, 0)

    def test_falls_back_to_cwd_and_omits_unknown_session_name(self):
        payload = {
            "session_id": SID,
            "cwd": "/Users/alex/code/widgets/.",
            "tool_input": {"command": 'agy "review module"'},
        }
        code, out, err = self._run(json.dumps(payload))
        self.assertEqual((code, out, err), (0, "", ""))
        raw_line = json.loads(self.ledger.read_text().strip())
        self.assertEqual(raw_line["cwd"], "/Users/alex/code/widgets")
        self.assertEqual(raw_line["tool"], "agy")
        self.assertNotIn("session_name", raw_line)

    def test_non_matching_command_writes_nothing(self):
        payload = {
            "session_id": SID,
            "cwd": "/Users/alex/code/widgets",
            "tool_input": {"command": "pytest -q"},
        }
        code, out, err = self._run(json.dumps(payload))
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertFalse(self.ledger.exists())

    def test_non_bash_tool_is_ignored(self):
        payload = {
            "session_id": SID,
            "cwd": "/Users/alex/code/widgets",
            "tool_name": "Write",
            "tool_input": {"command": 'codex exec "hi"'},
        }
        code, out, err = self._run(json.dumps(payload))
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertFalse(self.ledger.exists())

    def test_bad_inputs_always_exit_zero_and_print_nothing(self):
        bad_inputs = [
            "",
            "{not valid json",
            "[]",
            '"a string"',
            "42",
            json.dumps({}),
            json.dumps({"session_id": "", "cwd": "/x", "tool_input": {"command": "agy"}}),
            json.dumps({"session_id": SID, "cwd": "", "tool_input": {"command": "agy"}}),
            json.dumps({"session_id": SID, "cwd": "/x", "tool_input": "not a dict"}),
            json.dumps({"session_id": SID, "cwd": "/x", "tool_input": {"command": 123}}),
            "x" * (launch.MAX_INPUT + 100),
        ]
        for raw in bad_inputs:
            with self.subTest(raw=raw[:40]):
                code, out, err = self._run(raw)
                self.assertEqual(code, 0)
                self.assertEqual(out, "")
                self.assertEqual(err, "")
        self.assertFalse(self.ledger.exists())

    def test_unwritable_ledger_still_exits_zero_and_prints_nothing(self):
        blocker = self.base / "not-a-dir"
        blocker.write_text("file")
        payload = json.dumps({
            "session_id": SID,
            "cwd": "/Users/alex/code/widgets",
            "tool_input": {"command": 'codex exec "hi"'},
        })
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = launch.record_main(["--ledger", str(blocker / "sub" / "launches.jsonl")],
                                      stdin=io.StringIO(payload), now=FIXED_NOW)
        self.assertEqual((code, out.getvalue(), err.getvalue()), (0, "", ""))

    def test_cli_subprocess_without_session_drops_entry(self):
        env = dict(os.environ,
                   FLEETWATCH_NO_MIGRATE="1",
                   XDG_STATE_HOME=str(self.state),
                   FLEETWATCH_STATUS_DIR=str(self.status),
                   FLEETWATCH_CONFIG_DIR=str(self.base / "cfg"),
                   PYTHONPATH=str(REPO))
        payload = json.dumps({
            "session_id": SID,
            "cwd": "/Users/alex/code/widgets",
            "tool_input": {"command": 'codex exec "check tests"'},
        })
        p = subprocess.run([sys.executable, "-m", "fleetwatch", "launch", "record"],
                           input=payload, capture_output=True, text=True, env=env, cwd=str(self.base))
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))
        entries = launch.read_ledger(str(self.ledger))
        self.assertEqual(len(entries), 0)

        bad = subprocess.run([sys.executable, "-m", "fleetwatch", "launch", "record"],
                             input="{broken", capture_output=True, text=True, env=env, cwd=str(self.base))
        self.assertEqual((bad.returncode, bad.stdout, bad.stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
