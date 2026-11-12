"""Needs you also holds his overdue, due-today and undated items."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from test_fleetwatch import NOW, Env, dstr, iso, render_env, up_env  # noqa: E402
from fleetwatch import data, views  # noqa: E402


def view_of(e):
    sections, stale, _w = e.run()
    return data.build_view(sections, NOW, 21, stale)


def names(view):
    return [x["name"] for x in view["for_you"]]


class OnNeedsTests(unittest.TestCase):
    def test_each_kind_puts_a_session_on_needs(self):
        for label, item in (("overdue", {"what": "A", "due": dstr(-2), "yours": True}),
                            ("today", {"what": "A", "due": dstr(0), "yours": True}),
                            ("undated", {"what": "A", "yours": True})):
            view = view_of(up_env([item]))
            self.assertEqual(names(view), ["Alpha"], label)
            self.assertEqual([t["kind"] for t in view["for_you"][0]["tasks"]], [label])

    def test_tomorrow_and_not_yours_do_not(self):
        view = view_of(up_env([{"what": "A", "due": dstr(1), "yours": True},
                               {"what": "B", "due": dstr(-2), "kind": "deadline"},
                               {"what": "C", "due": dstr(0)},
                               {"what": "D"}]))
        self.assertEqual(names(view), [])

    def test_timed_item_today_is_overdue_once_the_time_has_passed(self):
        from datetime import datetime
        now = datetime.fromtimestamp(NOW, data.LOCAL_TZ)
        past = now.replace(hour=0, minute=0, second=1).isoformat()
        future = now.replace(hour=23, minute=59, second=0).isoformat()
        view = view_of(up_env([{"what": "P", "due": past, "yours": True},
                               {"what": "F", "due": future, "yours": True}]))
        kinds = {t["what"]: t["kind"] for t in view["for_you"][0]["tasks"]}
        self.assertEqual(kinds["F"], "today")
        if now.hour or now.minute:
            self.assertEqual(kinds["P"], "overdue")

    def test_remind_strings_count_as_undated(self):
        e = up_env([])
        e.status("a.json", session="Alpha", updated=iso(-60), remind=["call ITS"])
        self.assertEqual(names(view_of(e)), ["Alpha"])


class RowTests(unittest.TestCase):
    ITEMS = [{"what": "zeta", "yours": True},
             {"what": "today B", "due": dstr(0), "yours": True},
             {"what": "late newer", "due": dstr(-1), "yours": True},
             {"what": "late older", "due": dstr(-3), "yours": True},
             {"what": "alpha", "yours": True},
             {"what": "look again", "due": dstr(-2), "kind": "checkin", "yours": True},
             {"what": "look today", "due": dstr(0), "kind": "checkin", "yours": True}]

    def test_labels_and_order(self):
        from datetime import timedelta
        today = data.datetime.fromtimestamp(NOW, data.LOCAL_TZ).date()
        d = lambda n: (today + timedelta(days=n)).strftime("%b %-d")
        text = render_env(up_env(self.ITEMS))
        fy = text[text.index("FOR YOU"):text.index("COMING UP")].splitlines()[1:]
        got = [ln.strip() for ln in fy if ":" in ln]
        # overdue first (oldest due first), then due today, then undated by text
        self.assertEqual(got, [
            "Overdue (%s): late older" % d(-3),
            "Check-in overdue (%s): look again" % d(-2),
            "Overdue (%s): late newer" % d(-1),
            "Check in today: look today",
            "Due today: today B",
            "Needs a date: alpha",
            "Needs a date: zeta"])

    def test_due_today_shows_its_time(self):
        # The clock is pinned to noon so the 11:00 PM item is still ahead at any wall time.
        from datetime import datetime
        from unittest import mock
        import test_fleetwatch
        noon = datetime.fromtimestamp(NOW, data.LOCAL_TZ).replace(hour=12, minute=0, second=0)
        late = noon.replace(hour=23).isoformat()
        with mock.patch.object(test_fleetwatch, "NOW", noon.timestamp()):
            text = render_env(up_env([{"what": "ship", "due": late, "yours": True}]))
        self.assertIn("Due today 11:00 PM: ship", text)

    def test_tui_rows_precede_questions_and_jump_to_the_session(self):
        view = view_of(up_env([{"what": "A", "due": dstr(-2), "yours": True}], questions=["q?"]))
        node = views.needs_nodes(view, NOW)[0]
        plain = [c.head.plain for c in node.children]
        self.assertTrue(plain[0].startswith("Overdue ("), plain)
        self.assertTrue(plain[1].startswith("1. q?"), plain)
        self.assertEqual(node.children[0].session, "Alpha")


class SortTests(unittest.TestCase):
    def test_overdue_leads_under_every_sort(self):
        e = Env()
        e.reg("Alpha", "s1")
        e.reg("Zed", "s2")
        e.status("a.json", session="Alpha", updated=iso(-60), questions=["q1", "q2", "q3"])
        e.status("z.json", session="Zed", updated=iso(-600),
                 upcoming=[{"what": "late", "due": dstr(-1), "yours": True}])
        view = view_of(e)
        for sort in ("oldest", "count", "recent", "name"):
            self.assertEqual([n.session for n in views.needs_nodes(view, NOW, sort)], ["Zed", "Alpha"], sort)
        self.assertEqual(names(view), ["Zed", "Alpha"])

    def test_stuck_leads_overdue_under_every_sort(self):
        e = Env()
        e.reg("Alpha", "s1")
        e.status("a.json", session="Alpha", updated=iso(-60),
                 upcoming=[{"what": "late", "due": dstr(-1), "yours": True}])
        e.reg("Zed", "s2", status="blocked", age=100)
        e.status("z.json", session="Zed", updated=iso(-60), questions=[])
        view = view_of(e)
        for sort in ("oldest", "count", "recent", "name"):
            self.assertEqual([n.session for n in views.needs_nodes(view, NOW, sort)], ["Zed", "Alpha"], sort)
        self.assertEqual(names(view), ["Zed", "Alpha"])
        self.assertLess(render_env(e).index("Zed"), render_env(e).index("Alpha"))

    def test_stabilise_keeps_the_seen_order_for_ties(self):
        e = Env()
        for n in ("Aa", "Bb"):
            e.reg(n, n)
            e.status(n + ".json", session=n, updated=iso(-60), questions=["q"])
        nodes = views.needs_nodes(view_of(e), NOW, "name")
        again = views.stabilise(nodes, ["needs:Bb", "needs:Aa"])
        self.assertEqual([n.session for n in again], ["Aa", "Bb"])   # name sort ranks Aa first
        nodes = views.needs_nodes(view_of(e), NOW, "oldest")
        order = [n.key for n in views.stabilise(nodes, ["needs:Bb", "needs:Aa"])]
        self.assertEqual(order, ["needs:Bb", "needs:Aa"])


class HeaderTests(unittest.TestCase):
    ITEMS = [{"what": "late", "due": dstr(-2), "yours": True},
             {"what": "now", "due": dstr(0), "yours": True},
             {"what": "n1", "yours": True}, {"what": "n2", "yours": True}, {"what": "n3", "yours": True}]

    def parts(self, e, short=False):
        return [t for t, _s in views.needs_parts(view_of(e), short)]

    def test_counts_full_and_short(self):
        e = up_env(self.ITEMS, questions=["a", "b"])
        self.assertEqual(self.parts(e), ["2 questions", "1 overdue", "1 due today", "3 need a date"])
        self.assertEqual(self.parts(e, True), ["2 q", "1 overdue", "1 today", "3 undated"])
        plain = views.header_text(view_of(e), [1, 0, 0], NOW, []).plain
        self.assertIn("2 questions · 1 overdue · 1 due today · 3 need a date", plain)
        self.assertNotIn("need you", plain)
        self.assertEqual(plain.count("overdue"), 1)

    def test_overdue_count_is_his_items_only(self):
        e = up_env(self.ITEMS + [{"what": "theirs", "due": dstr(-2), "kind": "deadline"}])
        self.assertIn("1 overdue", self.parts(e))

    def test_short_header_fits_sixty_columns_up_to_the_counts(self):
        e = up_env(self.ITEMS, questions=["a", "b"])
        plain = views.header_text(view_of(e), [1, 0, 0], NOW, [], short=True).plain
        self.assertLessEqual(len(plain.split(" · 0 working")[0]), 60)

    def test_nothing_needs_you(self):
        plain = views.header_text(view_of(up_env([])), [0, 0, 0], NOW, []).plain
        self.assertIn("nothing needs you", plain)

    def test_text_header_at_full_and_narrow_width(self):
        e = up_env(self.ITEMS, questions=["a", "b"])
        self.assertIn("2 questions · 1 overdue · 1 due today · 3 need a date", render_env(e, width=140))
        narrow = render_env(e, width=60)
        self.assertIn("2 q", narrow)
        self.assertIn("3 undated", narrow)
        self.assertTrue(all(len(ln) <= 60 for ln in narrow.splitlines()))


class GoneSessionTests(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.e.status("gone.json", session="Gone Session", updated=iso(-5 * 86400 * 4),
                      upcoming=[{"what": "send the form", "due": dstr(-1), "yours": True},
                                {"what": "not his", "due": dstr(-1), "kind": "deadline"}])

    def test_item_of_a_session_with_no_row_still_shows(self):
        view = view_of(self.e)
        self.assertEqual(names(view), ["Gone Session"])
        self.assertEqual([t["what"] for t in view["for_you"][0]["tasks"]], ["send the form"])
        self.assertEqual(view["rows"], [])

    def test_once_output_lists_it(self):
        text = render_env(self.e)
        self.assertIn("FOR YOU", text)
        self.assertIn("Gone Session", text)
        self.assertIn("send the form", text)

    def test_tui_row_builds(self):
        node = views.needs_nodes(view_of(self.e), NOW)[0]
        self.assertTrue(node.children[0].head.plain.startswith("Overdue ("))


class CheckinKindTests(unittest.TestCase):
    def test_checkin_is_a_known_kind_and_behaves_like_todo(self):
        self.assertIn("checkin", data.KINDS)
        e = up_env([{"what": "look", "due": dstr(5), "kind": "checkin", "yours": True}])
        view = view_of(e)
        self.assertEqual([r["kind"] for r in data.timeline_rows(view["timeline"])], ["checkin"])
        todo, dated, _o = views.todo_rows(view)
        self.assertEqual([r["what"] for r in dated], ["look"])
        self.assertEqual(names(view), [])


if __name__ == "__main__":
    unittest.main()
