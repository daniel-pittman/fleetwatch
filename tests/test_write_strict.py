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


if __name__ == "__main__":
    unittest.main()
