"""v6: Enter jumps, sorting, persistent groups. Run with the fleetwatch venv's Python."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import standin  # noqa: E402
from waiting import settle  # noqa: E402
import test_app  # noqa: E402,F401  (points FLEETWATCH_CONFIG_DIR at a temp folder before fleetwatch loads)
from test_app import World, boot, refresh  # noqa: E402
from fleetwatch import app as fapp, config, iterm, render_text  # noqa: E402

HOME = os.path.expanduser("~")
TTY, HELPER_TTY = "/dev/ttys099", "/dev/ttys098"


def run(coro):
    return asyncio.run(coro)


class Env(unittest.TestCase):
    """A stand-in fleetwatch, a private config folder and a stubbed osascript."""

    def setUp(self):
        self.cfg = tempfile.TemporaryDirectory()
        self.envpatch = mock.patch.dict(os.environ, {"FLEETWATCH_CONFIG_DIR": self.cfg.name})
        self.envpatch.start()
        self.w = World()
        self.w.ttys[standin.CHILD_PID] = HELPER_TTY
        self.w.iterm = {TTY, HELPER_TTY}
        self.calls = []

        def fake(script, args=()):
            self.calls.append((script, list(args)))
            return True, "ok"
        self.osa = mock.patch.object(iterm, "osascript", fake)
        self.osa.start()
        self.state = os.path.join(self.cfg.name, "state.json")
        self.groups = os.path.join(self.cfg.name, "groups.toml")

    def tearDown(self):
        self.osa.stop()
        self.envpatch.stop()
        self.w.close()
        self.cfg.cleanup()

    def app(self):
        return fapp.FleetwatchApp(self.w.snapshot, interval=3600, state_path=self.state)

    def setcwd(self, sid, cwd):
        path = os.path.join(self.w.paths["sessions"], sid + ".json")
        with open(path) as fh:
            d = json.load(fh)
        d["cwd"] = cwd
        with open(path, "w") as fh:
            json.dump(d, fh)

    async def jumps(self, app, pilot, n):
        await settle(pilot, lambda: len(self.calls) >= n and
                     (app.status_text or "").startswith("jumped"), "the jump status")
        await pilot.pause()

    async def enter_on(self, app, pilot, tab, key):
        await pilot.press(str(tab + 1), "e")
        app.current.move_to(key)
        before = len(self.calls)
        await pilot.press("enter")
        await self.jumps(app, pilot, before + 1)
        return self.calls[before][1] if len(self.calls) > before else None


class EnterTests(Env):
    def test_enter_explains_the_old_no_op(self):
        # Enter used to be bound to "open" only, so on an already open row, or a row with nothing
        # to open (a question or a date line), it did nothing at all. It now jumps.
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("space")
                await pilot.press("enter")
                await self.jumps(app, pilot, 1)
                self.assertEqual(self.calls[0][1], [TTY])
        run(go())

    def test_enter_from_each_row_type(self):
        self.w.status("stuck2.json", session="Stuck Session", persona="Robin", updated=standin.iso(-6 * 60),
                      questions=["Which branch should the fix land on?"],
                      upcoming=[{"what": "Mine for stuck", "due": "2026-10-21", "kind": "todo", "yours": True},
                                {"what": "Todo for stuck", "yours": True}])
        async def go():
            app = self.app()
            async with app.run_test(size=(120, 40)) as pilot:
                await boot(app, pilot)
                await pilot.press("e")
                # Needs you: the session row, its own question, a helper's question, a stuck-helper line
                needs = app.lists[0]
                self.assertEqual(await self.enter_on(app, pilot, 0, "needs:Stuck Session"), [TTY])
                self.assertEqual(await self.enter_on(app, pilot, 0, "needs:Stuck Session:q:1"), [TTY])
                # question 2 belongs to helper wt-helper-1, which has its own iTerm tab
                self.assertEqual(await self.enter_on(app, pilot, 0, "needs:Stuck Session:q:2"), [HELPER_TTY])
                # dated and undated to-do rows go to their session
                up = next(k for k in app.lists[1]._order if "Mine for stuck" in app.lists[1]._by_key[k].head.plain)
                self.assertEqual(await self.enter_on(app, pilot, 1, up), [TTY])
                todo = next(k for k in app.lists[1]._order if "Todo for stuck" in app.lists[1]._by_key[k].head.plain)
                self.assertEqual(await self.enter_on(app, pilot, 1, todo), [TTY])
                # Sessions tab: the session row, then its helper row (own tab)
                self.assertEqual(await self.enter_on(app, pilot, 2, "ses:Stuck Session"), [TTY])
                self.assertEqual(await self.enter_on(app, pilot, 2, "ses:Stuck Session:k:wt-helper-1"),
                                 [HELPER_TTY])
                for script, args in self.calls:
                    self.assertNotIn("/dev/ttys", script)
                self.assertGreater(len(needs._order), 3)
        run(go())

    def test_helper_not_in_iterm_falls_back_to_the_parent(self):
        self.w.iterm = {TTY}
        async def go():
            app = self.app()
            async with app.run_test(size=(120, 40)) as pilot:
                await boot(app, pilot)
                await pilot.press("e")
                self.assertEqual(await self.enter_on(app, pilot, 2, "ses:Stuck Session:k:wt-helper-1"), [TTY])
                self.assertEqual(app.status_text, "jumped to Stuck Session")
        run(go())

    def test_row_with_no_tab_shows_the_toast(self):
        self.w.iterm = {"/dev/ttys500"}
        async def go():
            app = self.app()
            async with app.run_test(size=(120, 40)) as pilot:
                await boot(app, pilot)
                await pilot.press("e")
                await pilot.press("enter")
                await settle(pilot, lambda: "not in an iTerm2 tab" in (app.status_text or ""), "the toast")
                self.assertEqual(self.calls, [])
                self.assertIn("not in an iTerm2 tab", app.status_text)
                app.current.move_to("needs:Stuck Session:q:1")
                await pilot.press("enter")
                self.assertIn("not in an iTerm2 tab", app.status_text)
        run(go())

    def test_enter_on_a_section_header_folds_it_and_g_still_jumps(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("3")
                lst = app.current
                lst.move_to("sesg:working")
                self.assertIn("ses:Stuck Session", lst._order)
                await pilot.press("enter")
                self.assertNotIn("ses:Stuck Session", lst._order)
                await pilot.press("enter")
                self.assertIn("ses:Stuck Session", lst._order)
                self.assertEqual(self.calls, [])
                await pilot.press("g")
                self.assertEqual(app.status_text, "no session is highlighted")
                lst.move_to("ses:Stuck Session")
                await pilot.press("g")
                await self.jumps(app, pilot, 1)
                self.assertEqual(self.calls[0][1], [TTY])
        run(go())

    def test_space_right_left_fold(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                lst = app.lists[0]
                self.assertNotIn("1. Which branch", lst.visible_text())
                await pilot.press("space")
                self.assertIn("▾ Stuck Session", lst.visible_text())
                self.assertIn("1. Which branch", lst.visible_text())
                await pilot.press("space")
                self.assertNotIn("1. Which branch", lst.visible_text())
                await pilot.press("right")
                self.assertIn("1. Which branch", lst.visible_text())
                await pilot.press("left")
                self.assertNotIn("1. Which branch", lst.visible_text())
                await pilot.press("e")
                self.assertIn("1. Which branch", lst.visible_text())
                await pilot.press("c")
                self.assertNotIn("1. Which branch", lst.visible_text())
                self.assertEqual(self.calls, [])
        run(go())


class SortTests(Env):
    def setUp(self):
        super().setUp()
        self.w.reg("Alpha", "al", status="idle", age=500)
        self.w.reg("Beta", "be", status="idle", age=100)
        self.w.status("a1.json", session="Alpha", updated=standin.iso(-500),
                      questions=[{"q": "q?", "since": standin.iso(-10 * 3600)}])
        self.w.status("b1.json", session="Beta", updated=standin.iso(-100), questions=["q?", "r?", "s?"])

    def order(self, app, tab, prefix):
        return [k[len(prefix):] for k in app.lists[tab]._order if k.startswith(prefix)]

    async def cycle(self, app, pilot, tab, want):
        await pilot.press(str(tab + 1))
        seen = []
        for _ in range(6):
            seen.append(app.sorts[tab])
            if app.sorts[tab] == want:
                return
            await pilot.press("s")
        raise AssertionError("sort %s never came up: %s" % (want, seen))

    def test_needs_orders(self):
        want = {
            "oldest": ["Stuck Session", "Alpha", "Asker Development", "Quiet One", "Beta"],
            "count": ["Stuck Session", "Beta", "Asker Development", "Alpha", "Quiet One"],
            "recent": ["Stuck Session", "Beta", "Alpha", "Asker Development", "Quiet One"],
            # Stuck Session owns the overdue item, so it leads even by name
            "name": ["Stuck Session", "Alpha", "Asker Development", "Beta", "Quiet One"],
        }
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                self.assertEqual(app.sorts[0], "oldest")
                self.assertEqual(app.sortline_plain[0][:27], "sorted by: oldest question ")
                for mode in ("oldest", "count", "recent", "name"):
                    await self.cycle(app, pilot, 0, mode)
                    self.assertEqual(self.order(app, 0, "needs:"), want[mode], mode)
                    self.assertIn("sorted by: " + dict(fapp.views.SORTS[0])[mode], app.sortline_plain[0])
                await pilot.press("s")
                self.assertEqual(app.sorts[0], "oldest")
        run(go())

    def test_todo_orders(self):
        def whats(app):
            out = []
            for line in app.lists[1].visible_text().split("\n"):
                for w in ("Overdue thing", "Pens down", "Faculty interviews", "Email the registrar",
                          "Write the ITS ticket", "Dev deploy", "Spring launch"):
                    if w in line:
                        out.append(w)
            return out
        # undated first (by session, then text), then dated in the sort; Also noted stays closed
        want = {
            "date": ["Email the registrar", "Write the ITS ticket", "Overdue thing", "Pens down", "Faculty interviews"],
            "overdue": ["Email the registrar", "Write the ITS ticket", "Overdue thing", "Pens down", "Faculty interviews"],
            "session": ["Email the registrar", "Write the ITS ticket", "Pens down", "Overdue thing", "Faculty interviews"],
        }
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("2", "G", "G")       # the flat list: time, folder, then no groups
                for mode, order in want.items():
                    await self.cycle(app, pilot, 1, mode)
                    self.assertEqual(whats(app), order, mode)
                    self.assertIn("sorted by: " + dict(fapp.views.SORTS[1])[mode], app.sortline_plain[1])
        run(go())

    def test_sessions_orders(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("3")
                self.assertEqual(app.sorts[2], "recent")
                keys = self.order(app, 2, "ses:")
                self.assertLess(keys.index("Worker"), keys.index("Stuck Session"))   # 5 min before 6 min
                await pilot.press("s")
                keys = self.order(app, 2, "ses:")
                self.assertEqual(app.sorts[2], "name")
                self.assertLess(keys.index("Stuck Session"), keys.index("Worker"))
                self.assertIn("sorted by: name", app.sortline_plain[2])
        run(go())

    def test_choice_is_remembered_per_tab_across_a_restart(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("s", "2", "s", "s", "3", "s")
            with open(self.state) as fh:
                saved = json.load(fh)
            self.assertEqual(saved["sort"], {"0": "count", "1": "session", "2": "name"})
            app2 = self.app()
            async with app2.run_test(size=(100, 30)) as pilot:
                await boot(app2, pilot)
                self.assertEqual(app2.sorts, ["count", "session", "name", "recent", "name"])
                self.assertIn("sorted by: session", app2.sortline_plain[1])
                self.assertEqual(self.order(app2, 0, "needs:")[:2], ["Stuck Session", "Beta"])
        run(go())

    def test_unknown_saved_sort_falls_back_to_the_default(self):
        with open(self.state, "w") as fh:
            json.dump({"version": 2, "sort": {"0": "bogus", "1": "text"}}, fh)
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                self.assertEqual(app.sorts, ["oldest", "date", "recent", "recent", "name"])
        run(go())


class GroupTests(Env):
    def setUp(self):
        super().setUp()
        self.setcwd("s1", HOME + "/code/work/billing-api")                  # Stuck Session: Work
        self.setcwd("s3", HOME + "/code/personal/side-project")             # Worker: Personal
        self.setcwd("s4", "/elsewhere/notes")                               # Quiet One: Other
        self.setcwd("s5", "/somewhere/else")                                # Sleeper: Other

    def test_g_toggles_group_headers_on_every_tab(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 40)) as pilot:
                await boot(app, pilot)
                for i in (0, 2, 3):
                    self.assertNotIn("grp:", " ".join(app.lists[i]._order))
                await pilot.press("G")
                self.assertTrue(app.grouped)
                self.assertIn("grouped by project folder", app.sortline_plain[0])
                self.assertIn("▾ Work · 1", app.lists[0].visible_text())
                self.assertIn("▾ Other · 2", app.lists[0].visible_text())     # Asker Development, Quiet One
                await pilot.press("3")
                text = app.current.visible_text()
                for head in ("▾ Work · 1", "▾ Personal · 1", "▾ Other ·"):
                    self.assertIn(head, text)
                self.assertLess(text.index("▾ Work"), text.index("▾ Personal"))
                self.assertLess(text.index("▾ Personal"), text.index("▾ Other"))
                self.assertIn("▸ Stale · 1", text)            # helpers and stale records stay ungrouped
                await pilot.press("2")
                self.assertNotIn("grp:1:Other", app.current._order)     # To-dos has its own G (by day)
                self.assertIn("grp:1:t:overdue", app.current._order)
                await pilot.press("1", "G")
                self.assertFalse(app.grouped)
                for i in (0, 2, 3):
                    self.assertNotIn("grp:", " ".join(app.lists[i]._order))
            with open(self.state) as fh:
                self.assertFalse(json.load(fh)["groups"])
        run(go())

    def test_grouping_and_group_folds_survive_a_restart(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 40)) as pilot:
                await boot(app, pilot)
                await pilot.press("G")
                app.lists[0].move_to("grp:0:Work")
                await pilot.press("space")
                self.assertNotIn("Stuck Session", app.lists[0].visible_text())
            with open(self.state) as fh:
                saved = json.load(fh)
            self.assertTrue(saved["groups"])
            self.assertEqual(saved["folds"], {"grp:0:Work": False})
            app2 = self.app()
            async with app2.run_test(size=(100, 40)) as pilot:
                await boot(app2, pilot)
                self.assertTrue(app2.grouped)
                text = app2.lists[0].visible_text()
                self.assertIn("▸ Work · 1", text)
                self.assertNotIn("Stuck Session", text)
                self.assertIn("▾ Other · 2", text)
        run(go())

    def test_m_writes_an_override_and_the_group_shows_after_a_refresh(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 40)) as pilot:
                await boot(app, pilot)
                await pilot.press("G", "3")
                app.current.move_to("ses:Sleeper")
                await pilot.press("m")
                self.assertEqual(type(app.screen).__name__, "GroupPicker")
                # keys typed in the picker belong to it, not to the tabs behind
                await pilot.press("tab")
                self.assertEqual(app.active, 2)
                picker = app.screen.query_one("#choices")
                picker.highlighted = picker.option_count - 3          # Personal (Other is Sleeper's natural group)
                chosen = app.screen.names[picker.highlighted]
                await pilot.press("enter")
                await settle(pilot, lambda: type(app.screen).__name__ == "Screen", "the picker to close")
                self.assertEqual(type(app.screen).__name__, "Screen")
                with open(self.groups) as fh:
                    self.assertIn('"Sleeper" = "%s"' % chosen, fh.read())
                await settle(pilot, lambda: "%s · " % chosen in app.current.visible_text()
                             and "Sleeper" in app.current.visible_text(), "the moved session to show")
                self.assertIn("moved Sleeper to %s" % chosen, app.status_text)
                self.assertEqual(app.snapshot.grouping.of("Sleeper"), chosen)
        run(go())

    def test_m_with_a_new_group_name(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 40)) as pilot:
                await boot(app, pilot)
                await pilot.press("G", "3")
                app.current.move_to("ses:Sleeper")
                await pilot.press("m")
                picker = app.screen.query_one("#choices")
                picker.highlighted = picker.option_count - 1          # New group...
                await pilot.press("enter")
                await pilot.press("Z", "e", "d", "space", "q", "g", "e")   # q, g, e type text, not commands
                await pilot.press("enter")
                await settle(pilot, lambda: "moved Sleeper to Zed qge" in (app.status_text or ""), "the move")
                await refresh(app, pilot)
                self.assertIn("Zed qge · 1", app.current.visible_text())
                self.assertEqual(config.load_groups(self.groups)[0].overrides["Sleeper"], "Zed qge")
            app2 = self.app()
            async with app2.run_test(size=(100, 40)) as pilot:
                await boot(app2, pilot)
                await pilot.press("3")
                self.assertIn("Zed qge · 1", app2.current.visible_text())
        run(go())

    def test_m_escape_changes_nothing_and_m_needs_a_session(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 40)) as pilot:
                await boot(app, pilot)
                await pilot.press("3")
                app.current.move_to("sesg:working")
                await pilot.press("m")
                self.assertEqual(app.status_text, "no session is highlighted")
                app.current.move_to("ses:Sleeper")
                before = open(self.groups).read()
                await pilot.press("m", "escape")
                self.assertEqual(type(app.screen).__name__, "Screen")
                self.assertEqual(open(self.groups).read(), before)
        run(go())

    def test_malformed_groups_file_never_crashes_and_everything_is_other(self):
        with open(self.groups, "w") as fh:
            fh.write('[[group]\nname = "broken\n')
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 40)) as pilot:
                await boot(app, pilot)
                self.assertIn("groups.toml is not valid", app.status_text)
                await pilot.press("G", "3")
                text = app.current.visible_text()
                self.assertIn("▾ Other · 5", text)
                self.assertNotIn("▾ Work", text)
                app.current.move_to("ses:Sleeper")
                await pilot.press("m", "enter")                       # pick the only group, Other
                await settle(pilot, lambda: "could not save the group" in (app.status_text or ""), "the failure message")
                self.assertIn("could not save the group", app.status_text)
                self.assertEqual(open(self.groups).read(), '[[group]\nname = "broken\n')
        run(go())

    def test_groups_file_with_the_wrong_shape_is_reported(self):
        for text in ('group = 3\n', '[[group]]\npaths = ["/a"]\n', '[[group]]\nname = "A"\npaths = "x"\n',
                     '[overrides]\nx = 3\n'):
            with open(self.groups, "w") as fh:
                fh.write(text)
            groups, warning = config.load_groups(self.groups)
            self.assertIn("not valid", warning, text)
            self.assertEqual(groups.names(), ["Other"])

    def test_malformed_state_file_falls_back_with_a_toast(self):
        for text in ("{not json", "[1, 2]", '{"sort": 3}', '{"groups": "yes"}'):
            with open(self.state, "w") as fh:
                fh.write(text)
            async def go():
                app = self.app()
                async with app.run_test(size=(100, 30)) as pilot:
                    await boot(app, pilot)
                    self.assertIn("state.json is not valid", app.status_text, text)
                    self.assertEqual(app.sorts, ["oldest", "date", "recent", "recent", "name"])
                    self.assertFalse(app.grouped)
                    await pilot.press("s")
                    with open(self.state) as fh:
                        self.assertEqual(json.load(fh)["sort"], {"0": "count"})
            run(go())

    def test_first_run_creates_groups_toml(self):
        self.assertFalse(os.path.exists(self.groups))
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
        run(go())
        self.assertTrue(os.path.exists(self.groups))
        self.assertEqual(config.load_groups(self.groups)[1], None)


class MatchingTests(unittest.TestCase):
    def groups(self):
        return config.Groups([("Broad", ["~/git"]), ("Narrow", ["~/git/research", "/opt/x/"]),
                              ("Lab", ["~/git/research/lab"])], {"Pinned": "Broad"})

    def test_longest_prefix_wins(self):
        g = self.groups()
        self.assertEqual(g.of("s", HOME + "/git/other"), "Broad")
        self.assertEqual(g.of("s", HOME + "/git/research/x"), "Narrow")
        self.assertEqual(g.of("s", HOME + "/git/research/lab/sub"), "Lab")
        self.assertEqual(g.of("s", HOME + "/git/research/lab"), "Lab")

    def test_tilde_and_trailing_slash(self):
        g = self.groups()
        self.assertEqual(g.of("s", HOME + "/git/research/"), "Narrow")
        self.assertEqual(g.of("s", "/opt/x"), "Narrow")
        self.assertEqual(g.of("s", "/opt/x/deep/er"), "Narrow")

    def test_matches_whole_path_components_only(self):
        g = self.groups()
        self.assertEqual(g.of("s", HOME + "/git/researchers"), "Broad")
        self.assertEqual(g.of("s", "/opt/xylophone"), "Other")

    def test_no_match_and_missing_cwd_are_other(self):
        g = self.groups()
        self.assertEqual(g.of("s", "/nowhere"), "Other")
        self.assertEqual(g.of("s", None), "Other")
        self.assertEqual(g.of("s", ""), "Other")

    def test_override_beats_the_paths(self):
        g = self.groups()
        self.assertEqual(g.of("Pinned", HOME + "/git/research/lab"), "Broad")
        self.assertEqual(g.of("Pinned", None), "Broad")

    def test_names_lists_rules_then_override_only_groups_then_other(self):
        g = config.Groups([("A", ["/a"])], {"x": "Zed", "y": "A", "z": "Other"})
        self.assertEqual(g.names(), ["A", "Zed", "Other"])

    def test_default_config_places_the_real_folders(self):
        g, warning = config.parse_groups(config.DEFAULT_GROUPS_TOML), None
        cases = {
            HOME + "/code/work/billing-api": "Work",
            HOME + "/code/personal/side-project": "Personal",
            HOME + "/Documents/notes": "Personal",
            HOME: "Other",
        }
        for cwd, want in cases.items():
            self.assertEqual(g.of("anon", cwd), want, cwd)


class OverrideEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "groups.toml")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text):
        with open(self.path, "w") as fh:
            fh.write(text)

    def read(self):
        with open(self.path) as fh:
            return fh.read()

    def test_replaces_an_existing_entry_and_keeps_everything_else(self):
        base = ('# my groups\n[[group]]\nname = "A"\npaths = ["/a"]  # keep me\n\n'
                '[overrides]\n# pinned by hand\n"One" = "A"\n"Two" = "A"\n')
        self.write(base)
        config.set_override(self.path, "One", "B")
        self.assertEqual(self.read(), base.replace('"One" = "A"', '"One" = "B"'))

    def test_adds_to_the_end_of_overrides_when_another_table_follows(self):
        self.write('[overrides]\n"One" = "A"\n\n[[group]]\nname = "A"\npaths = []\n')
        config.set_override(self.path, "Two", "B")
        self.assertEqual(self.read(), '[overrides]\n"One" = "A"\n"Two" = "B"\n\n[[group]]\nname = "A"\npaths = []\n')
        self.assertEqual(config.load_groups(self.path)[0].overrides, {"One": "A", "Two": "B"})

    def test_creates_the_table_when_missing_and_handles_a_missing_final_newline(self):
        self.write('[[group]]\nname = "A"\npaths = ["/a"]')
        config.set_override(self.path, "One", "A")
        self.assertEqual(self.read(), '[[group]]\nname = "A"\npaths = ["/a"]\n\n[overrides]\n"One" = "A"\n')

    def test_awkward_names_round_trip(self):
        self.write('[overrides]\n')
        names = ['He said "hi"', "back\\slash", "Ünïcode ☃", "a = b # c", "[brackets]"]
        for n in names:
            config.set_override(self.path, n, "G " + n)
        ov = config.load_groups(self.path)[0].overrides
        for n in names:
            self.assertEqual(ov[n], "G " + n)

    def test_moving_back_to_the_natural_group_removes_the_entry(self):
        base = '[[group]]\nname = "A"\npaths = ["/a"]\n\n[overrides]\n# keep\n"One" = "A"\n'
        self.write(base)
        config.set_override(self.path, "Two", "B", natural="A")
        self.assertEqual(self.read(), base + '"Two" = "B"\n')
        config.set_override(self.path, "Two", "A", natural="A")
        self.assertEqual(self.read(), base)
        config.set_override(self.path, "Two", "A", natural="A")        # already gone: no change
        self.assertEqual(self.read(), base)

    def test_a_malformed_file_is_left_untouched(self):
        self.write('[overrides\nbroken')
        with self.assertRaises(ValueError):
            config.set_override(self.path, "One", "A")
        self.assertEqual(self.read(), '[overrides\nbroken')

    def test_leaves_no_temp_files_behind(self):
        self.write('[overrides]\n')
        config.set_override(self.path, "One", "A")
        self.assertEqual(os.listdir(self.tmp.name), ["groups.toml"])

    def test_state_round_trip_and_atomic_save(self):
        path = os.path.join(self.tmp.name, "state.json")
        self.assertEqual(config.load_state(path), (config.default_state(), None))
        state = {"version": 2, "sort": {"0": "name"}, "groups": True, "folds": {"grp:0:A": False},
                 "todo_group": "folder", "holds": {"s1:abc": {"done": True}}}
        self.assertTrue(config.save_state(state, path))
        self.assertEqual(config.load_state(path), (state, None))
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["state.json"])
        self.assertFalse(config.save_state(state, os.path.join(path, "nope", "state.json")))


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = standin.build(self.tmp.name)
        self.groups = config.Groups([("Mine", ["/x"])])

    def tearDown(self):
        self.tmp.cleanup()

    def test_json_gains_a_group_per_session_only_when_groups_are_given(self):
        sections, stale, warnings = standin.collect_view(self.paths, show_all=True)
        plain = render_text.to_json(sections, stale, standin.NOW, 86400, warnings)
        self.assertNotIn("group", plain["sections"]["needs"][0])
        grouped = render_text.to_json(sections, stale, standin.NOW, 86400, warnings, groups=self.groups)
        rows = {r["name"]: r["group"] for sec in grouped["sections"].values() for r in sec}
        self.assertEqual(rows["Stuck Session"], "Mine")            # the stand-in registry cwd is /x
        self.assertEqual(rows["Asker Development"], "Mine")        # a registry session; a status file alone is never a row
        self.assertEqual(json.loads(json.dumps(plain)), plain)

    def test_once_text_gets_group_headers_only_with_groups(self):
        sections, stale, warnings = standin.collect_view(self.paths, show_all=False)
        args = (sections, stale, standin.NOW, False, warnings)
        plain = render_text.render(*args, width=80, utf8=True)
        self.assertNotIn("BY GROUP", plain)
        grouped = render_text.render(*args, width=80, utf8=True, groups=self.groups)
        self.assertTrue(grouped.startswith(plain))
        tail = grouped[len(plain):]
        self.assertIn("BY GROUP", tail)
        self.assertIn(" Mine · ", tail)
        self.assertNotIn(" Other · ", tail)    # every stand-in session has a registry cwd under /x


if __name__ == "__main__":
    unittest.main()


class FooterTests(Env):
    def test_core_keys_show_at_80_columns_and_m_only_on_a_session(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(80, 30)) as pilot:
                await boot(app, pilot)
                text = app.footer_plain
                self.assertLessEqual(len(text), 78)
                for hint in ("⏎ go to session", "␣ fold", "? help", "q quit"):
                    self.assertIn(hint, text)
                self.assertIn("m move", text)               # the Stuck Session row is selected
                await pilot.press("3")
                app.current.move_to("sesg:working")
                self.assertNotIn("m move", app.footer_plain)
                app.current.move_to("ses:Stuck Session")
                self.assertIn("m move", app.footer_plain)
        run(go())

    def test_narrow_widths_drop_the_least_important_and_keep_the_core(self):
        from fleetwatch.app import footer_text
        wide = footer_text(120, True).plain
        for hint in ("s sort", "G groups", "m move", "r refresh", "⌃⇧F hide"):
            self.assertIn(hint, wide)
        for width in range(47, 120):
            text = footer_text(width, True).plain
            self.assertLessEqual(len(text), width)
            for hint in ("⏎ go to session", "/ find", "␣ fold", "? help", "q quit"):
                self.assertIn(hint, text)
        self.assertNotIn("⌃⇧F hide", footer_text(78, True).plain)
