"""Tests for the jump-to-session search box."""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import test_app  # noqa: E402  (sets FLEETWATCH_CONFIG_DIR before fleetwatch reads it)
from test_app import TTY, World, boot  # noqa: E402
from fleetwatch import app as fapp, find, iterm  # noqa: E402
from textual.widgets import Input, OptionList  # noqa: E402

ENTRIES = [
    {"name": "Billing API Development", "persona": "Robin", "updated": 5},
    {"name": "Docs Site", "persona": None, "updated": 9},
    {"name": "Spherical Heap Xylophone", "persona": "Piper", "updated": 7},
    {"name": "Robinch Lab", "persona": None, "updated": 1},
]


def names(q):
    return [e["name"] for e in find.rank(q, ENTRIES)]


class RankTests(unittest.TestCase):
    def test_an_exact_field_does_not_match_by_scattered_letters(self):
        path = "/home/alex/downloads/old-archive-budget"         # d, o, c appear in order, but never together
        exact = {"name": "Grant Proposal", "persona": None, "updated": 1, "fields": ["Grant Proposal", find.exact(path)]}
        plain = dict(exact, fields=["Grant Proposal", path])
        self.assertEqual(find.rank("doc", [exact]), [])
        self.assertEqual(find.rank("doc", [plain]), [plain])      # a plain field still takes them
        self.assertEqual(find.rank("gnt", [exact]), [exact])      # the name still takes subsequences
        self.assertEqual(find.rank("budget", [exact]), [exact])   # a substring of the folder still matches

    def test_initials_and_prefix(self):
        self.assertEqual(names("ba")[0], "Billing API Development")
        self.assertEqual(names("doc"), ["Docs Site"])

    def test_prefix_before_subsequence(self):
        self.assertEqual(names("sph")[0], "Spherical Heap Xylophone")
        es = [{"name": "Zebra Alpha Q", "persona": None, "updated": 9}, {"name": "Alphabet", "persona": None, "updated": 1}]
        self.assertEqual([e["name"] for e in find.rank("alp", es)], ["Alphabet", "Zebra Alpha Q"])

    def test_subsequence_matches_last(self):
        self.assertEqual(names("dcs"), ["Docs Site"])
        self.assertEqual(names("qqq"), [])

    def test_persona_matches(self):
        self.assertIn("Billing API Development", names("robin"))
        self.assertEqual(names("piper"), ["Spherical Heap Xylophone"])

    def test_empty_query_most_recent_first(self):
        self.assertEqual(names("")[0], "Docs Site")
        self.assertEqual(names("")[-1], "Robinch Lab")

    def test_tiers_order(self):
        es = [{"name": "xabc", "persona": None, "updated": 9}, {"name": "abcx", "persona": None, "updated": 1},
              {"name": "a b c", "persona": None, "updated": 5}, {"name": "zaxbxc", "persona": None, "updated": 9}]
        got = [e["name"] for e in find.rank("abc", es)]
        self.assertEqual(got, ["abcx", "a b c", "xabc", "zaxbxc"])


class FindAppTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.calls = []

        def fake(script, args=()):
            self.calls.append((script, list(args)))
            return True, "ok"
        self.patch = mock.patch.object(iterm, "osascript", fake)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.w.close()

    def test_open_and_close_keys(self):
        async def go():
            app = fapp.FleetwatchApp(self.w.snapshot, interval=3600)
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                for key in ("slash", "ctrl+k"):
                    await pilot.press(key)
                    await pilot.pause(0.1)
                    self.assertIsInstance(app.screen, fapp.FindScreen)
                    await pilot.press("escape")
                    await pilot.pause(0.1)
                    self.assertNotIsInstance(app.screen, fapp.FindScreen)
                await pilot.press("3")
                await pilot.press("ctrl+k")
                await pilot.pause(0.1)
                self.assertIsInstance(app.screen, fapp.FindScreen)
        asyncio.run(go())

    def test_filter_persona_and_enter_jumps_with_tty(self):
        async def go():
            app = fapp.FleetwatchApp(self.w.snapshot, interval=3600)
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("ctrl+k")
                await pilot.pause(0.1)
                for ch in "robin":
                    await pilot.press(ch)
                await pilot.pause(0.1)
                self.assertEqual([e["name"] for e in app.screen.shown], ["Stuck Session"])
                self.assertEqual(app.screen.shown[0]["status"], "2 questions")
                await pilot.press("enter")
                for _ in range(60):
                    await pilot.pause(0.05)
                    if self.calls:
                        break
                await pilot.pause(0.2)
                self.assertEqual(self.calls[0][1], [TTY])
                self.assertEqual(app.status_text, "jumped to Stuck Session")
                self.assertNotIsInstance(app.screen, fapp.FindScreen)
        asyncio.run(go())

    def test_not_in_iterm_stays_open_with_toast(self):
        async def go():
            app = fapp.FleetwatchApp(self.w.snapshot, interval=3600)
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("ctrl+k")
                await pilot.pause(0.1)
                for ch in "piper":
                    await pilot.press(ch)
                await pilot.pause(0.1)
                self.assertEqual(app.screen.shown[0]["status"], "not in iTerm")
                await pilot.press("enter")
                await pilot.pause(0.2)
                self.assertEqual(self.calls, [])
                self.assertIn("not running", app.status_text)
                self.assertIsInstance(app.screen, fapp.FindScreen)
        asyncio.run(go())

    def test_empty_query_lists_recent_first_and_arrows_move(self):
        async def go():
            app = fapp.FleetwatchApp(self.w.snapshot, interval=3600)
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("ctrl+k")
                await pilot.pause(0.1)
                shown = [e["name"] for e in app.screen.shown]
                self.assertGreater(len(shown), 1)
                self.assertEqual(shown[0], "Worker")   # updated 5 minutes ago, newest
                lst = app.screen.query_one("#results", OptionList)
                self.assertEqual(lst.highlighted, 0)
                await pilot.press("down")
                self.assertEqual(lst.highlighted, 1)
                await pilot.press("ctrl+n")
                self.assertEqual(lst.highlighted, 2)
                await pilot.press("ctrl+p")
                await pilot.press("up")
                self.assertEqual(lst.highlighted, 0)
        asyncio.run(go())

    def test_footer_keeps_find_at_80_columns(self):
        self.assertIn("/ find", fapp.footer_text(80, True).plain)
        narrow = fapp.footer_text(40, True).plain
        self.assertIn("/ find", narrow)
        self.assertNotIn("s sort", narrow)


if __name__ == "__main__":
    unittest.main()
