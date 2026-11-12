"""Board layout and folding: the radar overlay, the state on Needs you, fold-all and the Resume detail."""
import asyncio
import os
import sys
import unittest

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fleetwatch import app as fapp, data, demo, views  # noqa: E402


def walk(nodes):
    for n in nodes:
        yield n
        yield from walk(n.children)


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        self.tmp, self.paths = demo.prepare()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        self.tmp.cleanup()

    def board(self, size, script):
        """Run the demo board at `size`, wait for the first scan, then await script(app, pilot)."""
        async def go():
            app = fapp.FleetwatchApp(
                lambda: fapp.gather(self.paths, data.parse_window("24h"), data.DEFAULT_HORIZON_DAYS),
                3600, paths=self.paths)
            async with app.run_test(size=size) as pilot:
                await pilot.pause(1.5)
                return await script(app, pilot)
        return asyncio.run(go())

    @staticmethod
    def expandable(lst):
        return [n for n in walk(lst.nodes) if n.expandable]

    # ---- the radar overlay

    def test_content_starts_directly_under_the_status_line(self):
        async def script(app, pilot):
            status = app.query_one("#status").region
            return status.bottom, app.query_one("#sort0").region.y, app.query_one("#tab0").region.y
        status_bottom, sort_y, list_y = self.board((120, 40), script)
        self.assertEqual(sort_y, status_bottom)
        self.assertEqual(list_y, status_bottom + 1)

    def test_radar_sits_bottom_right_above_the_hint_line_and_tab_bar(self):
        async def script(app, pilot):
            r = app.query_one("#radar")
            return (r.region, r.display, app.query_one("#footer").region.y, app.query_one("#tab0").region.y,
                    app.size)
        region, shown, footer_y, list_y, size = self.board((120, 40), script)
        self.assertTrue(shown)
        self.assertEqual((region.width, region.height), (9, 5))
        self.assertLessEqual(region.bottom, footer_y)
        self.assertGreater(region.y, list_y)
        self.assertGreaterEqual(region.right, size.width - 3)
        self.assertLessEqual(region.right, size.width)

    def test_radar_does_not_shift_the_content_when_it_appears(self):
        async def script(app, pilot):
            return app.query_one("#tab0").region
        tall = self.board((120, 40), script)
        small = self.board((60, 20), script)
        self.assertEqual(tall.y, small.y)

    def test_radar_is_hidden_when_there_is_no_room(self):
        async def script(app, pilot):
            return app.query_one("#radar").display
        self.assertFalse(self.board((60, 20), script))
        self.assertFalse(self.board((120, 30), script))

    # ---- state on Needs you

    def test_needs_you_rows_show_the_session_state(self):
        async def script(app, pilot):
            return app.lists[0].visible_text()
        text = self.board((120, 40), script)
        self.assertIn("Checkout Flow (Sage)  idle", text)
        self.assertIn("Billing API (Robin)  stuck on a prompt", text)

    def test_needs_you_state_covers_working_and_quiet(self):
        async def script(app, pilot):
            fy = app.snapshot.view["for_you"]
            fy[1]["working"], fy[1]["quiet"] = True, None
            fy[2]["working"], fy[2]["quiet"] = False, 30
            app.rebuild()
            await pilot.pause(0.2)
            return fy[1]["name"], fy[2]["name"], app.lists[0].visible_text()
        first, second, text = self.board((120, 40), script)
        line = {ln.split("  ")[0].lstrip("▸▾ ").split(" (")[0]: ln for ln in text.split("\n")}
        self.assertIn("  working", line[first])
        self.assertIn("  quiet, expected every 30 min", line[second])

    # ---- fold everything

    def test_z_toggles_every_row_and_group_on_every_tab(self):
        async def script(app, pilot):
            seen = {}
            for tab in range(4):
                await pilot.press(str(tab + 1))
                await pilot.pause(0.2)
                lst = app.lists[tab]
                steps = []
                for _ in range(3):
                    await pilot.press("z")
                    await pilot.pause(0.2)
                    nodes = self.expandable(lst)
                    steps.append([lst.is_open(n) for n in nodes])
                seen[tab] = steps
            return seen
        seen = self.board((120, 40), script)
        for tab, (first, second, third) in seen.items():
            self.assertTrue(first and all(first), "tab %d opens everything first" % tab)
            self.assertFalse(any(second), "tab %d then closes everything" % tab)
            self.assertTrue(all(third), "tab %d and opens it again" % tab)

    def test_the_fold_all_key_is_in_the_hint_line_and_the_help(self):
        async def script(app, pilot):
            return app.footer_plain
        self.assertIn("z fold all", self.board((140, 40), script))
        self.assertTrue(any("  z " in line and "fold" in line for line in fapp.HELP_LINES))

    # ---- Resume detail

    def test_resume_was_line_folds_with_its_row(self):
        async def script(app, pilot):
            await pilot.press("4")
            await pilot.pause(0.3)
            lst = app.lists[views.RESUME_TAB]
            closed = lst.visible_text()
            await pilot.press("space")
            await pilot.pause(0.2)
            one = lst.visible_text()
            await pilot.press("space")
            await pilot.pause(0.2)
            back = lst.visible_text()
            await pilot.press("z")
            await pilot.pause(0.2)
            return closed, one, back, lst.visible_text()
        closed, one, back, everything = self.board((120, 40), script)
        self.assertNotIn("was:", closed)
        self.assertIn("waiting on the new price list from finance", closed)     # the parked note stays in view
        self.assertEqual(one.count("was:"), 1)
        self.assertEqual(back, closed)
        self.assertEqual(everything.count("was:"), 2)


if __name__ == "__main__":
    unittest.main()
