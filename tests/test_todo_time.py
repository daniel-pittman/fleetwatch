"""The Your to-dos tab grouped by time: buckets, kind sub-groups, folded counts, the G cycle and its memory."""
import asyncio
import json
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import standin  # noqa: E402
from test_v6 import Env, boot, run  # noqa: E402
from fleetwatch import data, views  # noqa: E402


def at(text):
    """Epoch of a local (Mountain) wall-clock time."""
    return datetime.fromisoformat(text).replace(tzinfo=data.LOCAL_TZ).timestamp()


def item(what, due=None, kind="todo", yours=True, has_time=False, session="Sess"):
    d = None
    if due:
        d = datetime.fromisoformat(due).replace(tzinfo=data.LOCAL_TZ)
        has_time = has_time or "T" in due
    return {"what": what, "due": d, "has_time": has_time, "kind": kind, "yours": yours, "session": session}


def view_of(now, *items):
    return {"timeline": data.build_timeline(list(items), now)}


def slot(now_text, due_text, red=False):
    row = {"due": datetime.fromisoformat(due_text).replace(tzinfo=data.LOCAL_TZ), "red": red}
    return views.time_slot(row, at(now_text))


class SlotTests(unittest.TestCase):
    def test_today_runs_from_local_midnight_to_local_midnight(self):
        self.assertEqual(slot("2026-10-18T00:00:01", "2026-10-18"), "today")
        self.assertEqual(slot("2026-10-18T23:59:59", "2026-10-18T23:59:59"), "today")
        self.assertEqual(slot("2026-10-18T23:59:59", "2026-10-19"), "tomorrow")
        self.assertEqual(slot("2026-10-18T00:00:01", "2026-10-17T23:59:59", red=True), "overdue")

    def test_tomorrow_crosses_a_month_end(self):
        self.assertEqual(slot("2026-10-31T10:00:00", "2026-11-01"), "tomorrow")
        self.assertEqual(slot("2026-12-31T23:30:00", "2027-01-01"), "tomorrow")
        self.assertEqual(slot("2028-02-28T08:00:00", "2028-02-29"), "tomorrow")      # leap day

    def test_the_seven_day_edge(self):
        self.assertEqual(slot("2026-10-18T14:00:00", "2026-10-20"), "d2")
        self.assertEqual(slot("2026-10-18T14:00:00", "2026-10-25"), "d7")
        self.assertEqual(slot("2026-10-18T14:00:00", "2026-10-26"), "month")
        self.assertEqual(slot("2026-10-18T14:00:00", "2026-10-31"), "month")
        self.assertEqual(slot("2026-10-18T14:00:00", "2026-11-01"), "later")
        self.assertEqual(slot("2026-10-30T14:00:00", "2026-11-06"), "d7")          # the edge spans the month end
        self.assertEqual(slot("2026-10-30T14:00:00", "2026-11-08"), "later")      # next month is never "this month"
        self.assertEqual(slot("2026-10-18T14:00:00", "2027-10-30"), "later")      # same month, next year

    def test_overdue_is_the_red_flag_even_for_a_date_today(self):
        self.assertEqual(slot("2026-10-18T14:00:00", "2026-10-18T09:00:00", red=True), "overdue")


class StructureTests(unittest.TestCase):
    NOW = at("2026-10-18T14:00:00")        # a Sunday

    def nodes(self, *items, sort="date"):
        return views.todo_nodes(view_of(self.NOW, *items), self.NOW, sort, None, mode="time")

    def sample(self):
        return self.nodes(
            item("Later thing", "2026-12-01", "milestone"),
            item("Month thing", "2026-10-28", "deadline"),
            item("Friday call", "2026-10-23T09:00:00", "meeting"),
            item("Friday deadline", "2026-10-23", "deadline"),
            item("Friday todo", "2026-10-23", "todo"),
            item("Friday todo two", "2026-10-23", "todo"),
            item("Tomorrow deploy", "2026-10-19", "deploy"),
            item("Today todo", "2026-10-18", "todo"),
            item("Missed", "2026-10-10", "deadline"),
            item("No date", None, "todo"),
            item("Not mine", "2026-10-21", "milestone", yours=False))

    def test_top_level_groups_run_overdue_to_undated_then_also_noted(self):
        keys = [n.key for n in self.sample()]
        self.assertEqual(keys, ["grp:1:t:overdue", "grp:1:t:today", "grp:1:t:tomorrow", "grp:1:t:d5",
                                "grp:1:t:month", "grp:1:t:later", "grp:1:t:undated", "upg:other"])

    def test_empty_groups_are_left_out(self):
        keys = [n.key for n in self.nodes(item("Only one", "2026-10-19", "todo"))]
        self.assertEqual(keys, ["grp:1:t:tomorrow"])

    def test_day_heads_name_the_day(self):
        heads = {n.key: n.head.plain for n in self.sample()}
        self.assertEqual(heads["grp:1:t:overdue"], "Overdue · 1")
        self.assertEqual(heads["grp:1:t:today"], "Today · Sun Oct 18 · 1")
        self.assertEqual(heads["grp:1:t:tomorrow"], "Tomorrow · Mon Oct 19 · 1")
        self.assertEqual(heads["grp:1:t:d5"], "Fri Oct 23 · 4")
        self.assertEqual(heads["grp:1:t:month"], "Later this month · 1")
        self.assertEqual(heads["grp:1:t:later"], "Later · 1")
        self.assertEqual(heads["grp:1:t:undated"], "Undated · 1")

    def test_kind_subgroups_have_counts_and_run_in_kind_order(self):
        friday = next(n for n in self.sample() if n.key == "grp:1:t:d5")
        self.assertEqual([k.key for k in friday.children],
                         ["grp:1:t:d5:deadline", "grp:1:t:d5:meeting", "grp:1:t:d5:todo"])
        self.assertEqual([k.head.plain for k in friday.children],
                         ["deadline · 1", "meeting · 1", "todo · 2"])
        todo = friday.children[2]
        self.assertEqual([c.key.split(":", 1)[0] for c in todo.children], ["up", "up"])
        self.assertIn("Friday todo", todo.children[0].head.plain)

    def test_a_folded_day_row_shows_its_counts_by_kind_busiest_first(self):
        friday = next(n for n in self.sample() if n.key == "grp:1:t:d5")
        self.assertEqual(friday.head.plain, "Fri Oct 23 · 4")
        self.assertEqual(friday.head_shut.plain, "Fri Oct 23 · 4 · 2 todo · 1 deadline · 1 meeting")

    def test_undated_items_are_a_flat_group_and_also_noted_is_untouched(self):
        nodes = self.sample()
        undated = next(n for n in nodes if n.key == "grp:1:t:undated")
        self.assertEqual([c.key.split(":", 1)[0] for c in undated.children], ["todo"])
        other = nodes[-1]
        self.assertEqual(other.head.plain, "Also noted · 1")

    def test_rows_inside_a_kind_follow_the_chosen_sort(self):
        nodes = self.nodes(item("B thing", "2026-10-23", session="Zed"), item("A thing", "2026-10-23", session="Amy"),
                           sort="session")
        kids = nodes[0].children[0].children
        self.assertIn("A thing", kids[0].head.plain)

    def test_default_mode_is_unchanged_without_the_time_flag(self):
        flat = views.todo_nodes(view_of(self.NOW, item("X", "2026-10-19")), self.NOW)
        self.assertEqual([n.key.split(":", 1)[0] for n in flat], ["up"])

    def test_the_modes_are_time_folder_none(self):
        self.assertEqual(views.TODO_GROUPS, ("time", "folder", "none"))


class GroupKeyTests(Env):
    """G on the To-dos tab cycles time, folder and none; elsewhere it still toggles the folder grouping."""

    def todo_keys(self, app):
        return [k for k in app.lists[views.TODO_TAB]._order if k.startswith("grp:1:")]

    def test_time_grouping_is_the_default_and_g_cycles_on_this_tab_only(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(120, 40)) as pilot:
                await boot(app, pilot)
                self.assertEqual(app.todo_group, "time")
                await pilot.press("2")
                self.assertTrue(any(":t:" in k for k in self.todo_keys(app)))
                self.assertIn("grouped by day", app.sortline_plain[1])
                self.assertFalse(app.grouped)
                await pilot.press("G")
                self.assertEqual(app.todo_group, "folder")
                self.assertFalse(app.grouped)                       # the other tabs did not change
                self.assertFalse(any(":t:" in k for k in self.todo_keys(app)))
                self.assertTrue(self.todo_keys(app))
                self.assertIn("grouped by project folder", app.sortline_plain[1])
                await pilot.press("G")
                self.assertEqual(app.todo_group, "none")
                self.assertEqual(self.todo_keys(app), [])
                self.assertNotIn("grouped", app.sortline_plain[1])
                await pilot.press("G")
                self.assertEqual(app.todo_group, "time")
        run(go())

    def test_g_on_another_tab_still_toggles_folder_grouping_and_leaves_the_to_dos_alone(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(120, 40)) as pilot:
                await boot(app, pilot)
                await pilot.press("1", "G")
                self.assertTrue(app.grouped)
                self.assertEqual(app.todo_group, "time")
                self.assertIn("grp:0:", " ".join(app.lists[0]._order))
                self.assertTrue(any(":t:" in k for k in self.todo_keys(app)))
        run(go())

    def test_the_choice_survives_a_restart(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(120, 40)) as pilot:
                await boot(app, pilot)
                await pilot.press("2", "G")
            with open(self.state) as fh:
                self.assertEqual(json.load(fh)["todo_group"], "folder")
            app2 = self.app()
            async with app2.run_test(size=(120, 40)) as pilot:
                await boot(app2, pilot)
                self.assertEqual(app2.todo_group, "folder")
        run(go())

    def test_a_missing_or_bad_saved_choice_means_time(self):
        from fleetwatch import config
        for raw in ({}, {"todo_group": "sideways"}, {"todo_group": 3}):
            with open(self.state, "w") as fh:
                json.dump(dict(raw, version=config.STATE_VERSION), fh)
            self.assertEqual(config.load_state(self.state)[0]["todo_group"], "time")

    def test_a_folded_day_row_reads_at_a_glance_and_z_folds_all(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(120, 40)) as pilot:
                await boot(app, pilot)
                await pilot.press("2")
                lst = app.lists[views.TODO_TAB]
                await pilot.press("z")                              # nothing closed yet: this opens all
                await pilot.press("z")                              # now closes every group
                text = lst.visible_text()
                self.assertIn("▸ Overdue", text)
                self.assertRegex(text, r"▸ .* · \d+ · \d+ \w+")
                self.assertNotIn("Pens down", text)
        run(go())


if __name__ == "__main__":
    unittest.main()
