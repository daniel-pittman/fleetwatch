"""fleetwatch write-status refuses upcoming items a reader would misread."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import io  # noqa: E402

from test_v7 import Tree  # noqa: E402
from fleetwatch import write_status  # noqa: E402
from fleetwatch.write_status import StatusError, validate  # noqa: E402


def up(*items):
    return {"upcoming": list(items)}


class StrictUpcomingTests(unittest.TestCase):
    def test_his_item_without_a_date_is_refused_with_the_checkin_hint(self):
        with self.assertRaises(StatusError) as cm:
            validate(up({"what": "Call ITS", "kind": "todo", "yours": True}))
        self.assertIn("needs a due date", str(cm.exception))
        self.assertIn('"kind": "checkin"', str(cm.exception))

    def test_date_under_another_name_is_refused_with_did_you_mean(self):
        for key in ("when", "date", "by", "deadline"):
            with self.assertRaises(StatusError) as cm:
                validate(up({"what": "Call ITS", key: "2026-11-12", "yours": True}))
            self.assertIn("unknown key %r" % key, str(cm.exception), key)
            self.assertIn("did you mean due?", str(cm.exception), key)

    def test_other_unknown_key_is_refused_without_the_due_hint(self):
        with self.assertRaises(StatusError) as cm:
            validate(up({"what": "A", "due": "2026-11-12", "yours": True, "note": "x"}))
        self.assertIn("unknown key 'note'", str(cm.exception))
        self.assertNotIn("did you mean", str(cm.exception))

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(StatusError) as cm:
            validate(up({"what": "A", "due": "2026-11-12", "kind": "task", "yours": True}))
        self.assertIn("kind 'task' is not one of", str(cm.exception))

    def test_good_items_still_pass(self):
        out = validate(up({"what": "Tag", "due": "2026-11-17", "kind": "deadline", "yours": True},
                          {"what": "Check ITS", "due": "2026-11-12", "kind": "checkin", "yours": True},
                          {"what": "Go-live", "due": "2026-11-17", "kind": "deploy"},
                          {"what": "Not his, no date", "kind": "milestone"}))
        self.assertEqual([i["what"] for i in out["upcoming"]],
                         ["Tag", "Check ITS", "Go-live", "Not his, no date"])
        self.assertEqual(out["upcoming"][1]["kind"], "checkin")


class RefusalReachesTheCaller(Tree):
    def run_ws(self, argv, payload):
        out, err = io.StringIO(), io.StringIO()
        code = write_status.run(argv, io.StringIO(payload), out, err, paths=self.paths,
                                ppids={500: 400, 400: 300, 300: 1}, start_pid=500, now=None)
        return code, out.getvalue(), err.getvalue()

    def test_refused_write_prints_the_rule_and_writes_nothing(self):
        self.reg("My Session", "sid-9", pid=300)
        code, out, err = self.run_ws([], '{"upcoming": [{"what": "A", "when": "2026-11-12", "yours": true}]}')
        self.assertNotEqual(code, 0)
        self.assertIn("did you mean due?", err)
        self.assertEqual(os.listdir(self.paths["status"]), [])

    def test_cadence_without_working_on_notes_but_still_writes(self):
        self.reg("My Session", "sid-9", pid=300)
        code, _out, err = self.run_ws([], '{"cadence_min": 30}')
        self.assertEqual(code, 0)
        self.assertIn("a looping session should write working_on every firing", err)
        self.assertEqual(len(os.listdir(self.paths["status"])), 1)
        code, _out, err = self.run_ws([], '{"cadence_min": 30, "working_on": "x"}')
        self.assertEqual(code, 0)
        self.assertNotIn("looping", err)


class HelperSessionsAreRefused(Tree):
    """Helper sessions (worktree lanes, headless runs, unnamed sessions) do not write status."""

    def run_ws(self, argv, payload='{"working_on": "x"}'):
        out, err = io.StringIO(), io.StringIO()
        code = write_status.run(argv, io.StringIO(payload), out, err, paths=self.paths,
                                ppids={500: 400, 400: 300, 300: 1}, start_pid=500, now=None)
        return code, out.getvalue(), err.getvalue()

    def refused(self, name, extra=None, sid="sid-h"):
        self.reg(name, sid, pid=300, extra=extra)
        code, out, err = self.run_ws([])
        self.assertEqual(code, 3, err)
        self.assertEqual(os.listdir(self.paths["status"]), [])
        return err

    def test_a_helper_name_is_refused_with_the_rule(self):
        for name in ("wt-board-polish", "run-17", "t3-lane", "0123456789abcdef0123456789abcdef"):
            err = self.refused(name)
            self.assertIn("helper sessions don't write status; the session that started you does", err, name)

    def test_a_headless_sdk_run_is_refused(self):
        err = self.refused("Billing API", extra={"entrypoint": "sdk-cli"})
        self.assertIn("helper sessions don't write status", err)

    def test_a_session_nobody_named_is_refused_and_told_how_to_name_it(self):
        err = self.refused("quick-fox", extra={"nameSource": "derived"})
        self.assertIn("name this session with /rename to report status", err)

    def test_force_writes_anyway(self):
        self.reg("wt-board-polish", "sid-h", pid=300)
        code, out, err = self.run_ws(["--force"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(os.listdir(self.paths["status"])), 1)

    def test_a_named_interactive_session_still_writes(self):
        self.reg("Billing API", "sid-n", pid=300, extra={"nameSource": "user", "entrypoint": "cli"})
        code, out, err = self.run_ws([])
        self.assertEqual(code, 0, err)

    def test_print_path_is_not_refused(self):
        self.reg("wt-x", "sid-h", pid=300)
        self.assertEqual(self.run_ws(["--print-path"])[0], 0)

    def test_dry_run_is_refused_for_a_helper(self):
        self.reg("wt-x", "sid-h", pid=300)
        self.assertEqual(self.run_ws(["--dry-run"])[0], 3)


if __name__ == "__main__":
    unittest.main()
