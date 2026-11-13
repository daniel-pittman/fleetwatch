"""`/` finds a row on the current tab and Enter does what Enter does there; Ctrl-K stays the live-session finder."""
import asyncio
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fleetwatch import app as fapp, data, demo, views  # noqa: E402


class TabFind(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        self.tmp, self.paths = demo.prepare()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        self.tmp.cleanup()

    def board(self, script, size=(120, 40)):
        async def go():
            app = fapp.FleetwatchApp(
                lambda: fapp.gather(self.paths, data.parse_window("24h"), data.DEFAULT_HORIZON_DAYS),
                3600, paths=self.paths)
            async with app.run_test(size=size) as pilot:
                await pilot.pause(1.5)
                return await script(app, pilot)
        return asyncio.run(go())

    @staticmethod
    async def search(app, pilot, tab, query):
        await pilot.press(tab, "slash")
        await pilot.pause(0.2)
        for ch in query:
            await pilot.press("space" if ch == " " else ch)
        await pilot.pause(0.2)
        return app.screen

    def results(self, tab, query):
        async def script(app, pilot):
            screen = await self.search(app, pilot, tab, query)
            return type(screen).__name__, [e["name"] for e in screen.shown]
        return self.board(script)

    # ---- what each tab searches

    def test_needs_finds_a_session_row(self):
        kind, names = self.results("1", "billing")
        self.assertEqual(kind, "TabFindScreen")
        self.assertTrue(names[0].startswith("Billing API"))

    def test_todos_match_the_text_and_the_session_name(self):
        kind, names = self.results("2", "pricing question")
        self.assertEqual(kind, "TabFindScreen")
        self.assertEqual(len(names), 1)
        self.assertIn("Answer the pilot customer's pricing question", names[0])
        _k, by_session = self.results("2", "newsletter")
        self.assertGreaterEqual(len(by_session), 3)                  # Newsletter's own items
        self.assertTrue(all("ewsletter" in n for n in by_session[:3]))

    def test_todos_inside_closed_groups_are_found(self):
        async def script(app, pilot):
            await pilot.press("2", "z", "z")                             # fold every group
            self.assertNotIn("Renew the supplier", app.current.visible_text())
            screen = await self.search(app, pilot, "2", "supplier api key")
            return [e["name"] for e in screen.shown]
        names = self.board(script)
        self.assertEqual(len(names), 1)

    def test_an_empty_result_names_the_tab_kind(self):
        async def script(app, pilot):
            out = {}
            for tab in ("1", "2", "3", "5"):
                screen = await self.search(app, pilot, tab, "zzqqxx")
                out[tab] = str(screen.query_one("#results").get_option_at_index(0).prompt)
                await pilot.press("escape")
                await pilot.pause(0.2)
            return out
        self.assertEqual(self.board(script), {"1": "no matching row", "2": "no matching to-do",
                                              "3": "no matching session", "5": "no matching index"})

    def test_sessions_finds_an_active_session(self):
        kind, names = self.results("3", "docs")
        self.assertEqual(kind, "TabFindScreen")
        self.assertTrue(any(n.startswith("Docs Site") for n in names))

    def test_resume_matches_name_and_folder(self):
        kind, by_name = self.results("4", "pricing")
        self.assertEqual(kind, "ResumeFindScreen")
        self.assertEqual(by_name, ["Pricing Page"])
        _k, by_folder = self.results("4", "site/status")
        self.assertEqual(by_folder, ["Status Page"])

    def test_indexes_match_name_group_and_owner_only(self):
        _k, by_name = self.results("5", "support")
        self.assertEqual(by_name, ["support-faq"])
        _k, by_group = self.results("5", "ferpa")
        self.assertEqual(by_group, ["advising-notes"])
        _k, by_owner = self.results("5", "pricing page")
        self.assertEqual(by_owner, ["release-history"])
        _k, by_content = self.results("5", "lantern")
        self.assertEqual(by_content, [])

        async def script(app, pilot):
            await self.search(app, pilot, "5", "")
            return app.index_find_entries()
        for e in self.board(script):
            self.assertEqual(set(e), {"key", "name", "persona", "status", "updated", "group", "fields"})

    def test_ranking_is_the_finders_ranking(self):
        # a name prefix outranks a word-start match
        _k, names = self.results("5", "s")
        self.assertEqual(names[0], "site-docs")           # both are prefixes; the tie goes to the name
        self.assertIn("advising-notes", names)            # a subsequence match still lists, last

    # ---- Enter does what Enter does on that row

    def test_enter_on_a_needs_or_sessions_or_todo_result_is_enter_on_that_row(self):
        for tab, query, key in (("1", "billing", "ses:"), ("3", "docs", "ses:"), ("2", "pricing question", "up:")):
            async def script(app, pilot, tab=tab, query=query):
                seen = []
                with mock.patch.object(app, "action_enter", lambda: seen.append((app.active, app.current.cursor))):
                    await self.search(app, pilot, tab, query)
                    await pilot.press("enter")
                    await pilot.pause(0.3)
                self.assertNotIsInstance(app.screen, fapp.FindScreen)
                return seen
            seen = self.board(script)
            self.assertEqual(len(seen), 1, tab)
            self.assertEqual(seen[0][0], int(tab) - 1)
            self.assertIsNotNone(seen[0][1], tab)

    def test_enter_on_a_session_result_runs_the_real_jump_path(self):
        async def script(app, pilot):
            seen = []
            with mock.patch.object(app, "action_jump", lambda: seen.append(app.current.current().session)):
                await self.search(app, pilot, "3", "billing")
                await pilot.press("enter")
                await pilot.pause(0.3)
            return seen
        self.assertEqual(self.board(script), ["Billing API"])

    def test_enter_on_an_index_result_goes_to_its_owner(self):
        async def script(app, pilot):
            seen = []
            with mock.patch.object(app, "go_to_index_owner", lambda name: seen.append(name)):
                await self.search(app, pilot, "5", "release")
                await pilot.press("enter")
                await pilot.pause(0.3)
            return seen
        self.assertEqual(self.board(script), ["release-history"])

    def test_enter_on_a_resume_result_resumes_here(self):
        async def script(app, pilot):
            seen = []
            with mock.patch.object(app, "start_resume", lambda sid, here: seen.append((sid, here))):
                await self.search(app, pilot, "4", "pricing")
                await pilot.press("enter")
                await pilot.pause(0.3)
            return seen
        self.assertEqual(self.board(script), [(demo.RESUME_SIDS[0], True)])

    def test_escape_closes_without_acting(self):
        async def script(app, pilot):
            seen = []
            with mock.patch.object(app, "action_enter", lambda: seen.append(1)):
                await self.search(app, pilot, "2", "pricing")
                await pilot.press("escape")
                await pilot.pause(0.2)
            return seen, isinstance(app.screen, fapp.FindScreen)
        self.assertEqual(self.board(script), ([], False))

    # ---- Ctrl-K

    def test_ctrl_k_is_still_the_live_session_finder_on_every_tab(self):
        async def script(app, pilot):
            out = []
            for tab in "12345":
                await pilot.press(tab, "ctrl+k")
                await pilot.pause(0.2)
                out.append((type(app.screen) is fapp.FindScreen,
                            sorted(e["name"] for e in app.screen.shown)[:2]))
                await pilot.press("escape")
                await pilot.pause(0.2)
            return out
        out = self.board(script)
        self.assertTrue(all(kind for kind, _ in out))
        self.assertEqual(len({tuple(names) for _, names in out}), 1)
        self.assertEqual(out[0][1], ["Billing API", "Checkout Flow"])

    def test_help_and_hints_name_both_keys(self):
        text = "\n".join(fapp.HELP_LINES)
        self.assertIn("find a row on this tab", text)
        self.assertIn("Ctrl-K", text)


if __name__ == "__main__":
    unittest.main()
