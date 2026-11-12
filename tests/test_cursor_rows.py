"""Your to-dos rows are cursor rows: highlight, Enter, picker, refresh, folds."""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import standin  # noqa: E402
import test_app  # noqa: E402,F401  (sets FLEETWATCH_CONFIG_DIR before fleetwatch reads it)
from test_app import TTY, World, boot, refresh  # noqa: E402
from fleetwatch import app as fapp, iterm  # noqa: E402

SHARED = {"what": "Pens down for LA applications", "due": "2026-10-22", "kind": "deadline", "yours": True}


def highlighted(lst):
    """Plain text of the lines painted in reverse video."""
    return [line.plain.strip() for (k, line), strip in zip(lst._lines, lst._strips)
            if any(seg.style and seg.style.reverse for seg in strip)]


class CursorRows(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.calls = []
        self.patch = mock.patch.object(iterm, "osascript", lambda s, a=(): (self.calls.append(list(a)), (True, "ok"))[1])
        self.patch.start()
        # one item shared by two sessions, one of them the iTerm session
        self.stuck_items([])

    def tearDown(self):
        self.patch.stop()
        self.w.close()

    def stuck_items(self, extra):
        """One status file per session: Stuck Session holds the shared item, a solo one and `extra`."""
        solo = {"what": "Solo task", "due": "2026-10-21", "yours": True}
        todos = [{"what": "Solo todo", "yours": True}] + [{"what": "Filler todo %d" % i, "yours": True} for i in range(4)]
        self.w.status("stuck2.json", session="Stuck Session", persona="Robin", updated=standin.iso(-6 * 60),
                      questions=["Which branch should the fix land on?"], upcoming=[SHARED, solo] + todos + extra)

    def app(self):
        return fapp.FleetwatchApp(self.w.snapshot, interval=3600)

    def settle(self, pilot, n=1):
        async def inner():
            for _ in range(60):
                await pilot.pause(0.05)
                if len(self.calls) >= n:
                    break
            await pilot.pause(0.1)
        return inner()

    def test_highlight_moves_item_by_item_even_when_rows_overflow(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 9)) as pilot:   # short pane: the view overflows
                await boot(app, pilot)
                for tab in ("2",):
                    await pilot.press(tab)
                    lst = app.current
                    self.assertGreater(lst.virtual_size.height, lst.size.height)
                    start = lst._order.index(lst.cursor)
                    for step in range(1, len(lst._order) - start):
                        await pilot.press("down")
                        self.assertEqual(lst.cursor, lst._order[start + step])
                        self.assertEqual(len(highlighted(lst)) >= 1, True)
                    self.assertEqual(lst.cursor, lst._order[-1])
                    await pilot.press("k")
                    self.assertEqual(lst.cursor, lst._order[-2])
                    await pilot.press("home")
                    self.assertEqual(lst.cursor, lst._order[0])
                    await pilot.press("pagedown")
                    self.assertNotEqual(lst.cursor, lst._order[0])
                    await pilot.press("end")
                    self.assertEqual(lst.cursor, lst._order[-1])
                    self.assertIn(lst._by_key[lst.cursor].head.plain.strip(), highlighted(lst)[0])
        asyncio.run(go())

    def test_enter_jumps_to_the_owning_session_tty(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("2", "e")
                lst = app.current
                lst.move_to(next(k for k in lst._order if "Write the ITS ticket" in lst._by_key[k].head.plain))
                self.calls.clear()
                await pilot.press("enter")
                await pilot.pause(0.2)
                self.assertEqual(self.calls, [])   # Quiet One is not in iTerm: toast, no call
                self.assertIn("is not", app.status_text)
                lst = app.current
                lst.move_to(next(k for k in lst._order if "Pens down" in lst._by_key[k].head.plain))
                await pilot.press("enter")
                await pilot.pause(0.1)
                self.assertIsInstance(app.screen, fapp.SessionPicker)
                await pilot.press("escape")
                self.assertEqual(self.calls, [])
        asyncio.run(go())

    def test_single_session_item_jumps_without_a_picker(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("2")
                lst = app.current
                lst.move_to(next(k for k in lst._order if "Solo todo" in lst._by_key[k].head.plain))
                await pilot.press("enter")
                await self.settle(pilot)
                self.assertEqual(self.calls, [[TTY]])
        asyncio.run(go())

    def test_double_click_jumps_like_enter_and_single_click_only_selects(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("2")
                lst = app.current
                key = next(k for k in lst._order if "Solo todo" in lst._by_key[k].head.plain)
                row = next(i for i, (k, _) in enumerate(lst._lines) if k == key) - int(lst.scroll_offset.y)
                await pilot.click(lst, offset=(4, row))
                await pilot.pause(0.3)
                self.assertEqual(lst.cursor, key)
                self.assertEqual(self.calls, [])   # a single click selects and never jumps
                await pilot.double_click(lst, offset=(4, row))
                await self.settle(pilot)
                self.assertEqual(self.calls, [[TTY]])
        asyncio.run(go())

    def test_double_click_on_a_section_leaves_it_as_the_first_click_set_it(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("3")
                lst = app.current
                key = next((k for k in lst._order if lst._by_key[k].section and lst._by_key[k].expandable), None)
                self.assertIsNotNone(key)
                before = lst.is_open(lst._by_key[key])
                row = next(i for i, (k, _) in enumerate(lst._lines) if k == key) - int(lst.scroll_offset.y)
                await pilot.double_click(lst, offset=(4, row))
                await pilot.pause(0.3)
                self.assertEqual(lst.is_open(lst._by_key[key]), not before)
                self.assertEqual(self.calls, [])
        asyncio.run(go())

    def test_multi_session_item_opens_a_picker_then_jumps(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("2")
                lst = app.current
                node = lst._by_key[next(k for k in lst._order if "Pens down" in lst._by_key[k].head.plain)]
                self.assertEqual(sorted(node.sessions), ["Asker Development", "Stuck Session"])
                lst.move_to(node.key)
                await pilot.press("g")
                await pilot.pause(0.1)
                self.assertIsInstance(app.screen, fapp.SessionPicker)
                picker = app.screen
                opts = picker.query_one("#choices")
                opts.highlighted = picker.names.index("Stuck Session")
                await pilot.press("enter")
                await self.settle(pilot)
                self.assertEqual(self.calls, [[TTY]])
                self.assertEqual(app.status_text, "jumped to Stuck Session")
                # a session outside iTerm gets the usual toast
                self.calls.clear()
                await pilot.press("enter")
                await pilot.pause(0.1)
                picker = app.screen
                picker.query_one("#choices").highlighted = picker.names.index("Asker Development")
                await pilot.press("enter")
                await pilot.pause(0.2)
                self.assertEqual(self.calls, [])
                self.assertIn("is not running", app.status_text)
        asyncio.run(go())

    def test_cursor_survives_a_refresh_that_adds_an_item_above(self):
        async def go():
            app = self.app()
            added = []
            async with app.run_test(size=(100, 9)) as pilot:
                await boot(app, pilot)
                for tab in ("2",):
                    await pilot.press(tab)
                    lst = app.current
                    await pilot.press("down", "down")
                    key = lst.cursor
                    self.assertTrue(key.startswith(("up:", "todo:")))
                    before = lst._order.index(key)
                    item = {"what": "Added above %s" % tab, "yours": True}
                    added.append(item)
                    self.stuck_items(added)
                    await refresh(app, pilot)
                    self.assertEqual(lst.cursor, key)
                    self.assertEqual(lst._order.index(key), before + 1)
                    self.assertTrue(any("Added above" in lst._by_key[k].head.plain for k in lst._order[:before + 1]))
        asyncio.run(go())

    def test_group_header_is_a_row_that_folds(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("G")
                for tab in ("2",):
                    await pilot.press(tab)
                    lst = app.current
                    await pilot.press("home")
                    head = lst._order[0]
                    self.assertTrue(head.startswith("grp:"))
                    self.assertEqual(lst.cursor, head)
                    n = len(lst._order)
                    await pilot.press("space")
                    self.assertLess(len(lst._order), n)
                    await pilot.press("right")
                    self.assertEqual(len(lst._order), n)
                    await pilot.press("left")
                    self.assertLess(len(lst._order), n)
                    await pilot.press("enter")           # Enter on a header folds, never jumps
                    self.assertEqual(len(lst._order), n)
                    self.assertEqual(self.calls, [])
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
