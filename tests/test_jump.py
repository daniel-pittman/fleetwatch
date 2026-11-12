"""`fleetwatch jump`: raise a session's iTerm2 tab by name or session ID."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import standin  # noqa: E402
from fleetwatch import jump  # noqa: E402


class Jump(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = standin.build(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"FLEETWATCH_AGENTS_CMD": self.paths["command"],
                                                "FLEETWATCH_SESSIONS_DIR": self.paths["sessions"],
                                                "FLEETWATCH_JOBS_DIR": self.paths["jobs"],
                                                "FLEETWATCH_STATUS_DIR": self.paths["status"],
                                                "FLEETWATCH_PS_CMD": "true"})
        self.env.start()
        self.ctx = {"ttys": {1: "/dev/ttys004"}, "iterm": {"/dev/ttys004"}, "err": None}

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_jump(self, argv, ctx=None, jumped=(True, "")):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("fleetwatch.iterm.jump_context", return_value=ctx or self.ctx), \
                mock.patch("fleetwatch.iterm.jump_to_tty", return_value=jumped) as jt:
            rc = jump.main(argv, out, err)
        return rc, out.getvalue(), err.getvalue(), jt

    def test_jumps_by_name(self):
        rc, out, err, jt = self.run_jump(["Stuck Session"])
        self.assertEqual(rc, 0, err)
        jt.assert_called_once_with("/dev/ttys004")
        self.assertIn("Stuck Session", out)

    def test_name_match_ignores_case(self):
        rc, out, err, jt = self.run_jump(["stuck session"])
        self.assertEqual(rc, 0, err)
        jt.assert_called_once()

    def test_jumps_by_session_id(self):
        sid = "aa11bb22-0000-4000-8000-000000000001"
        with open(os.path.join(self.paths["sessions"], sid + ".json"), "w") as fh:
            json.dump({"name": "Twin", "sessionId": sid, "pid": 1, "kind": "interactive", "cwd": "/x",
                       "status": "idle", "statusUpdatedAt": standin.ms(), "updatedAt": standin.ms(),
                       "startedAt": standin.ms()}, fh)
        rc, out, err, jt = self.run_jump(["--session-id", sid])
        self.assertEqual(rc, 0, err)
        jt.assert_called_once_with("/dev/ttys004")

    def test_an_unknown_session_id(self):
        rc, out, err, jt = self.run_jump(["--session-id", "deadbeef-0000"])
        self.assertEqual(rc, 1)
        jt.assert_not_called()

    def test_a_name_starting_with_a_dash_after_double_dash(self):
        rc, out, err, jt = self.run_jump(["--", "-nothing"])
        self.assertEqual(rc, 1)
        self.assertIn("no running session", err)

    def test_unknown_name(self):
        rc, out, err, jt = self.run_jump(["Nobody"])
        self.assertEqual(rc, 1)
        self.assertIn("no running session named Nobody", err)
        jt.assert_not_called()

    def test_a_session_that_is_not_running(self):
        rc, out, err, jt = self.run_jump(["Worker"])
        self.assertEqual(rc, 1)
        jt.assert_not_called()

    def test_iterm_unreachable(self):
        rc, out, err, jt = self.run_jump(["Stuck Session"], ctx={"ttys": {}, "iterm": None, "err": "not running"})
        self.assertEqual(rc, 1)
        self.assertIn("iTerm2", err)
        jt.assert_not_called()

    def test_not_in_an_iterm_tab(self):
        rc, out, err, jt = self.run_jump(["Stuck Session"], ctx={"ttys": {1: "/dev/ttys004"}, "iterm": set(), "err": None})
        self.assertEqual(rc, 1)
        self.assertIn("not in an iTerm2 tab", err)

    def test_the_jump_itself_failing(self):
        rc, out, err, jt = self.run_jump(["Stuck Session"], jumped=(False, "no iTerm2 session on that terminal"))
        self.assertEqual(rc, 1)
        self.assertIn("no iTerm2 session", err)

    def test_needs_a_name_or_id(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                jump.main([], io.StringIO(), io.StringIO())
        self.assertEqual(cm.exception.code, 2)

    def test_bad_session_id_is_refused(self):
        rc, out, err, jt = self.run_jump(["--session-id", "x; rm -rf"])
        self.assertEqual(rc, 2)
        jt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
