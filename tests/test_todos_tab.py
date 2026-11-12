"""The merged Your to-dos tab: order, grouping, count and the saved-state migration."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import standin  # noqa: E402
from fleetwatch import config, data, views  # noqa: E402

UNDATED = ["Email the registrar", "Write the ITS ticket"]
DATED = {"date": ["Overdue thing", "Pens down", "Faculty interviews"],
         "overdue": ["Overdue thing", "Pens down", "Faculty interviews"],
         "session": ["Pens down", "Overdue thing", "Faculty interviews"]}   # Asker, Stuck Session, Worker


class World(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        paths = standin.build(self.tmp.name)
        sections, self.stale, _w = standin.collect_view(paths, show_all=True)
        self.view = data.build_view(sections, standin.NOW, 21)

    def whats(self, nodes):
        out = []
        for n in nodes:
            out.append(n.head.plain if n.section else n.key.split(":", 1)[0] + "|" + n.head.plain)
        return out


class OrderTests(World):
    def flat(self, sort, grouping=None):
        return views.todo_nodes(self.view, standin.NOW, sort, grouping)

    def test_undated_then_dated_then_also_noted_under_each_sort(self):
        for sort in ("date", "overdue", "session"):
            nodes = self.flat(sort)
            kinds = [n.key.split(":", 1)[0] for n in nodes]
            self.assertEqual(kinds, ["todo", "todo", "up", "up", "up", "upg"], sort)
            texts = [n.head.plain for n in nodes[:5]]
            for want, text in zip(UNDATED + DATED[sort], texts):
                self.assertIn(want, text, sort)
            self.assertEqual(nodes[5].head.plain, "Also noted · 3")

    def test_undated_rows_keep_the_date_column_so_dated_rows_line_up(self):
        nodes = self.flat("date")
        self.assertTrue(nodes[0].head.plain.startswith("no date"))
        marker = lambda n: len(n.head.plain.split(" ✎ ")[0])
        self.assertEqual(marker(nodes[0]), marker(nodes[2]))

    def test_grouping_repeats_the_order_inside_each_group(self):
        g = views.Grouping({"Asker Development": "A", "Worker": "B", "Quiet One": "B",
                            "Stuck Session": "A"}, ["A", "B"])
        nodes = self.flat("date", g)
        self.assertEqual([n.key for n in nodes], ["grp:1:A", "grp:1:B"])
        a, b = nodes
        self.assertEqual([k.key.split(":", 1)[0] for k in a.children], ["todo", "up", "up", "upg"])
        self.assertEqual([k.key.split(":", 1)[0] for k in b.children], ["todo", "up", "upg"])
        self.assertIn("Email the registrar", a.children[0].head.plain)
        self.assertIn("Write the ITS ticket", b.children[0].head.plain)
        self.assertIn("Faculty interviews", b.children[1].head.plain)
        self.assertTrue(a.children[-1].key.startswith("upg:other:A"))

    def test_count_is_undated_plus_dated_without_also_noted(self):
        self.assertEqual(views.tab_counts(self.view, self.stale)[1], 5)
        self.assertEqual(len(views.TAB_TITLES), 5)
        self.assertEqual(views.RESUME_TAB, 3)

    def test_build_tabs_has_three_node_lists(self):
        tabs, _seen = views.build_tabs(self.view, self.stale, standin.NOW)
        self.assertEqual(len(tabs), 3)
        self.assertEqual(tabs[1][0].key.split(":", 1)[0], "todo")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "state.json")

    def write(self, obj):
        with open(self.path, "w") as fh:
            json.dump(obj, fh)

    def test_old_five_tab_file_is_rekeyed(self):
        self.write({"sort": {"0": "count", "1": "session", "2": "text", "3": "name", "4": "name"},
                    "groups": True,
                    "folds": {"grp:0:A": False, "grp:1:B": False, "grp:2:C": False,
                              "grp:3:D": False, "grp:4:E": True}})
        state, warn = config.load_state(self.path)
        self.assertIsNone(warn)
        self.assertEqual(state["sort"], {"0": "count", "1": "session", "2": "name", "3": "name"})
        self.assertEqual(state["folds"], {"grp:0:A": False, "grp:1:B": False, "grp:2:D": False, "grp:3:E": True})
        self.assertEqual(state["version"], config.STATE_VERSION)
        self.assertTrue(state["groups"])

    def test_new_file_is_not_migrated_twice(self):
        self.write({"version": 2, "sort": {"1": "overdue", "2": "name", "3": "name"},
                    "groups": False, "folds": {"grp:3:E": False}})
        state, _w = config.load_state(self.path)
        self.assertEqual(state["sort"], {"1": "overdue", "2": "name", "3": "name"})
        self.assertEqual(state["folds"], {"grp:3:E": False})

    def test_saved_file_loads_back_unchanged(self):
        state, _w = config.load_state(self.path)
        state["sort"]["1"] = "session"
        self.assertTrue(config.save_state(state, self.path))
        self.assertEqual(config.load_state(self.path), (state, None))
        self.assertEqual(os.listdir(self.tmp.name), ["state.json"])


if __name__ == "__main__":
    unittest.main()
