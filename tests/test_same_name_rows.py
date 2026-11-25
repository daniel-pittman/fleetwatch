"""Two running sessions with one name: each has its own row, the cursor reaches each, and Enter, p and
the cursor's place across a refresh all follow the row's own session ID."""
import asyncio
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import rowkeys  # noqa: E402
import standin  # noqa: E402
from waiting import settle  # noqa: E402
from test_v6 import Env  # noqa: E402
from test_app import boot, refresh  # noqa: E402
from fleetwatch import app as fapp, park  # noqa: E402

NAME = "Twin Session"
FIRST, SECOND = "twin-a", "twin-b"
TTY_A, TTY_B = "/dev/ttys201", "/dev/ttys202"


def run(coro):
    return asyncio.run(coro)


def lit_keys(lst):
    """The keys of the rows painted in reverse video, the cursor's highlight."""
    return {k for (k, _line), strip in zip(lst._lines, lst._strips)
            if any(seg.style and seg.style.reverse for seg in strip)}


class SameName(Env):
    def setUp(self):
        super().setUp()
        # Two live processes this test owns stand in for the two sessions' Claude processes.
        self.procs = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"]) for _ in range(2)]
        for p in self.procs:
            self.addCleanup(lambda p=p: (p.kill(), p.wait()))
        pid_a, pid_b = (p.pid for p in self.procs)
        self.w.reg(NAME, FIRST, status="idle", age=120, pid=pid_a)
        self.w.reg(NAME, SECOND, status="idle", age=60, pid=pid_b)
        self.w.status("twin-a.json", session=NAME, sessionId=FIRST, updated=standin.iso(-120),
                      questions=["Ship the first one?"])
        self.w.status("twin-b.json", session=NAME, sessionId=SECOND, updated=standin.iso(-60),
                      questions=["Ship the second one?"])
        self.w.ttys.update({pid_a: TTY_A, pid_b: TTY_B})
        self.w.iterm |= {TTY_A, TTY_B}

    async def walk_to(self, pilot, lst, key):
        """Move the real cursor with the arrow keys until it rests on `key`."""
        await pilot.press("home")
        for _ in range(len(lst._order)):
            if lst.cursor == key:
                break
            await pilot.press("down")
        self.assertEqual(lst.cursor, key)
        await pilot.pause()
        self.assertEqual(lit_keys(lst), {key})

    def test_each_row_has_its_own_key_and_the_cursor_reaches_each(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(120, 40)) as pilot:
                await boot(app, pilot)
                for tab, keys in (("1", (rowkeys.needs(FIRST), rowkeys.needs(SECOND))),
                                  ("3", (rowkeys.ses(FIRST), rowkeys.ses(SECOND)))):
                    await pilot.press(tab)
                    lst = app.current
                    for key in keys:
                        self.assertEqual(lst._order.count(key), 1, key)
                        self.assertEqual(lst._by_key[key].session, NAME)
                    for key in keys + keys[::-1]:
                        await self.walk_to(pilot, lst, key)
        run(go())

    def test_enter_on_each_jumps_to_that_session(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(120, 40)) as pilot:
                await boot(app, pilot)
                for tab, keys in (("1", (rowkeys.needs(SECOND), rowkeys.needs(FIRST))),
                                  ("3", (rowkeys.ses(SECOND), rowkeys.ses(FIRST)))):
                    await pilot.press(tab)
                    for key, tty in zip(keys, (TTY_B, TTY_A)):
                        await self.walk_to(pilot, app.current, key)
                        before = len(self.calls)
                        await pilot.press("enter")
                        await self.jumps(app, pilot, before + 1)
                        self.assertEqual(self.calls[before][1], [tty], key)
        run(go())

    def test_park_and_unpark_act_on_the_highlighted_session(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(120, 40)) as pilot:
                await boot(app, pilot)
                for tab, key in (("1", rowkeys.needs(SECOND)), ("3", rowkeys.ses(SECOND))):
                    await pilot.press(tab)
                    await self.walk_to(pilot, app.current, key)
                    await pilot.press("p")
                    await settle(pilot, lambda: isinstance(app.screen, fapp.ParkScreen), "the park dialog")
                    await pilot.press("enter")
                    await settle(pilot, lambda: SECOND in park.load(), "the park")
                    self.assertEqual(set(park.load()), {SECOND})
                    await self.walk_to(pilot, app.current, key)
                    await pilot.press("u")
                    await settle(pilot, lambda: SECOND not in park.load(), "the unpark")
                    self.assertEqual(park.load(), {})
        run(go())

    def test_the_cursor_stays_on_its_row_across_a_refresh(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(120, 40)) as pilot:
                await boot(app, pilot)
                for tab, key, other in (("1", rowkeys.needs(FIRST), rowkeys.needs(SECOND)),
                                        ("3", rowkeys.ses(FIRST), rowkeys.ses(SECOND))):
                    await pilot.press(tab)
                    lst = app.current
                    await self.walk_to(pilot, lst, key)
                    # The other twin changes: a new question and a fresh update stamp.
                    self.w.status("twin-b.json", session=NAME, sessionId=SECOND, updated=standin.iso(-5),
                                  questions=["Ship the second one?", "And tag it?"])
                    await refresh(app, pilot)
                    self.assertEqual(lst.cursor, key)
                    self.assertIn(other, lst._order)
                    await pilot.pause()
                    self.assertEqual(lit_keys(lst), {key})
                    self.w.status("twin-b.json", session=NAME, sessionId=SECOND, updated=standin.iso(-60),
                                  questions=["Ship the second one?"])
                    await refresh(app, pilot)
        run(go())


if __name__ == "__main__":
    unittest.main()
