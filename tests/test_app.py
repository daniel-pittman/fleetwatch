"""Textual pilot tests for the live app. Run with the fleetwatch venv's Python."""
import asyncio
import atexit
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

# Nothing in these tests may touch the real ~/.config/fleetwatch.
_CONFIG = tempfile.TemporaryDirectory()
os.environ["FLEETWATCH_CONFIG_DIR"] = _CONFIG.name
atexit.register(_CONFIG.cleanup)

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import standin  # noqa: E402
from fleetwatch import app as fapp, config, data, iterm, prune  # noqa: E402

TTY = "/dev/ttys099"


class World:
    """The stand-in fleetwatch on disk, editable between refreshes."""

    def __init__(self, bare=False):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = standin.build(self.tmp.name)
        if bare:
            for d in ("sessions", "status"):
                for f in os.listdir(self.paths[d]):
                    os.remove(os.path.join(self.paths[d], f))
        self.iterm = {TTY}
        self.ttys = {standin.PARENT_PID: TTY}

    def reg(self, name, sid, status="idle", age=60, pid=standin.DEAD_PID):
        d = {"name": name, "sessionId": sid, "pid": pid, "kind": "interactive", "cwd": "/x",
             "status": status, "statusUpdatedAt": standin.ms(-age), "updatedAt": standin.ms(-age),
             "startedAt": standin.ms(-age)}
        with open(os.path.join(self.paths["sessions"], sid + ".json"), "w") as fh:
            json.dump(d, fh)

    def status(self, fname, **d):
        with open(os.path.join(self.paths["status"], fname), "w") as fh:
            json.dump(d, fh)

    def drop(self, sid=None, fname=None):
        if sid:
            os.remove(os.path.join(self.paths["sessions"], sid + ".json"))
        if fname:
            os.remove(os.path.join(self.paths["status"], fname))

    def snapshot(self):
        sections, stale, warnings = standin.collect_view(self.paths, show_all=True)
        jump = {"ttys": dict(self.ttys), "iterm": None if self.iterm is None else set(self.iterm), "err": None}
        groups, bad = config.load_groups()
        recs = data.build_records(self.paths, standin.NOW, [], ppids=standin.PPIDS)
        plan = prune.make_plan(self.paths, standin.NOW, recs, config.default_prune())
        return fapp.Snapshot(sections, stale, standin.NOW, warnings, 21, jump, groups, bad, plan)

    def close(self):
        self.tmp.cleanup()


def run(coro):
    return asyncio.run(coro)


async def refresh(app, pilot):
    before = app.refreshes
    app.refresh_now()
    for _ in range(100):
        await pilot.pause(0.05)
        if app.refreshes > before:
            return
    raise AssertionError("refresh did not finish")


async def boot(app, pilot):
    for _ in range(100):
        await pilot.pause(0.05)
        if app.refreshes:
            break
    await pilot.pause(0.1)


class PruneModalTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.status("old.json", session="Old Done", sessionId="od1", updated=standin.iso(-9 * 86400))
        self.w.status("openq.json", session="Old Asker", sessionId="oa1", updated=standin.iso(-9 * 86400),
                      questions=["Still open?"])
        self.old = os.path.join(self.w.paths["status"], "old.json")

    def tearDown(self):
        self.w.close()

    def app(self):
        app = fapp.FleetwatchApp(self.w.snapshot, interval=3600)
        app.prune_clock = lambda: standin.NOW
        return app

    def test_p_lists_candidates_and_escape_cancels(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("P")
                await pilot.pause(0.2)
                self.assertIsInstance(app.screen, fapp.PruneScreen)
                text = "\n".join(str(w.render()) for w in app.screen.query("Static"))
                self.assertIn("Old Done", text)
                self.assertIn("Kept: has open items", text)
                self.assertIn("Old Asker", text)
                await pilot.press("escape")
                await pilot.pause(0.2)
                self.assertNotIsInstance(app.screen, fapp.PruneScreen)
                self.assertTrue(os.path.isfile(self.old))
        run(go())

    def test_y_prunes_and_stale_header_counts_ready_files(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("3")
                self.assertIn("1 ready to prune (P)", app.lists[2].visible_text())
                await pilot.press("P")
                await pilot.pause(0.2)
                await pilot.press("y")
                for _ in range(100):
                    await pilot.pause(0.05)
                    if not os.path.exists(self.old):
                        break
                self.assertFalse(os.path.exists(self.old))
                self.assertTrue(os.path.isfile(os.path.join(self.w.paths["status"], ".pruned", "old.json")))
                self.assertTrue(os.path.isfile(os.path.join(self.w.paths["status"], "openq.json")))
                await pilot.pause(0.2)
                self.assertIn("pruned 1", app.status_text or "")
        run(go())

    def test_help_lists_the_key(self):
        self.assertTrue(any(l.lstrip().startswith("P ") for l in fapp.HELP_LINES))


class AppTests(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def tearDown(self):
        self.w.close()

    def app(self):
        return fapp.FleetwatchApp(self.w.snapshot, interval=3600)

    def test_tab_switch_keys_tab_and_click(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                self.assertEqual(app.active, 0)
                for key, want in (("2", 1), ("3", 2), ("4", 3), ("1", 0)):
                    await pilot.press(key)
                    self.assertEqual(app.active, want)
                await pilot.press("tab")
                self.assertEqual(app.active, 1)
                await pilot.press("shift+tab", "shift+tab")
                self.assertEqual(app.active, 3)
                await pilot.press("tab")
                self.assertEqual(app.active, 0)
                tabs = app.query_one(fapp.TabBar)
                x = tabs._spans[2][0] + 2
                await pilot.click(fapp.TabBar, offset=(x, 0))
                self.assertEqual(app.active, 2)
        run(go())

    def test_tab_labels_carry_counts(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                plain = app.query_one(fapp.TabBar).plain
                snap = app.snapshot
                self.assertEqual(snap.counts[0], 3)   # stuck, asker, and Quiet One's undated task
                for i, (title, n) in enumerate(zip(("Needs you", "Your to-dos", "Sessions"),
                                                   snap.counts)):
                    self.assertIn("%d %s · %d" % (i + 1, title, n), plain)
                self.assertEqual(snap.counts[1], 5)   # his 3 dated and 2 undated items, not Also noted
        run(go())

    def test_narrow_terminal_keeps_tab_bar_whole(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(60, 20)) as pilot:
                await boot(app, pilot)
                plain = app.query_one(fapp.TabBar).plain
                self.assertLessEqual(len(plain), 58)
                for i in "1234":
                    self.assertIn(i + " ", plain)
        run(go())

    def test_a_header_too_long_for_the_width_ends_in_an_ellipsis(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 20)) as pilot:
                await boot(app, pilot)
                app.snapshot.header.append(" · " + "x" * 120)
                app.show_header()
                await pilot.pause()
                line = "".join(seg.text for seg in app.query_one("#top").render_line(0)).rstrip()
                self.assertLessEqual(len(line), 100)
                self.assertTrue(line.endswith("\u2026"), line)
        run(go())

    def test_stuck_row_sorts_first(self):
        # Asker has more questions than the stuck session; stuck still comes first.
        self.w.status("asker2.json", session="Asker Development", persona="Piper",
                      updated=standin.iso(-40 * 60),
                      questions=["a?", "b?", "c?", "d?", "e?"])
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                keys = app.lists[0]._order
                self.assertEqual(keys[0], "needs:Stuck Session")
                self.assertEqual(app.lists[0].cursor, "needs:Stuck Session")
                self.assertIn("stuck on a prompt 6m", app.lists[0].visible_text())
        run(go())

    def test_expand_collapse_and_all(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                lst = app.lists[0]
                self.assertNotIn("1. Which branch", lst.visible_text())
                await pilot.press("space")
                text = lst.visible_text()
                self.assertIn("▾ Stuck Session", text)
                self.assertIn("1. Which branch should the fix land on?", text)
                self.assertIn("2. Helper question that rolls up [wt-helper-1]", text)
                await pilot.press("left")
                self.assertNotIn("1. Which branch", lst.visible_text())
                await pilot.press("right")
                self.assertIn("1. Which branch", lst.visible_text())
                await pilot.press("left", "c")
                self.assertNotIn("1. Which branch", lst.visible_text())
                await pilot.press("e")
                text = lst.visible_text()
                self.assertIn("1. Which branch", text)
                self.assertIn("1. Approve the schema change? · 3.0h", text)
                self.assertIn("Needs a date: Email the registrar about the term dates", text)
                await pilot.press("c")
                self.assertNotIn("Approve the schema change", lst.visible_text())
        run(go())

    def test_todos_tab_orders_undated_then_dated_then_also_noted(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("2")
                lst = app.current
                text = lst.visible_text()
                for w in ("Email the registrar", "Write the ITS ticket", "Overdue thing",
                          "Pens down for LA applications", "Faculty interviews"):
                    self.assertIn(w, text)
                order = [text.index(w) for w in ("Email the registrar", "Write the ITS ticket", "Overdue thing",
                                                 "Pens down", "Faculty interviews", "Also noted")]
                self.assertEqual(order, sorted(order))
                keys = lst._order
                self.assertTrue(all(k.startswith("todo:") for k in keys[:2]))
                self.assertTrue(all(k.startswith("up:") for k in keys[2:5]))
                self.assertEqual(keys[5], "upg:other")
                self.assertIn("▸ Also noted · 3", text)
                self.assertNotIn("Dev deploy", text)
                lst.move_to("upg:other")
                await pilot.press("enter")
                text = lst.visible_text()
                self.assertIn("Dev deploy  Asker+Worker", text)
                self.assertIn("Spring launch", text)
                await refresh(app, pilot)
                self.assertIn("Spring launch", lst.visible_text())
                self.assertEqual(lst.cursor, "upg:other")
        run(go())

    def test_sessions_groups_and_helper_count(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("3")
                lst = app.current
                text = lst.visible_text()
                for head in ("▾ Working · 2", "▾ Gone quiet · 1", "▾ Idle · 2", "▸ Stale · 1"):
                    self.assertIn(head, text)
                self.assertIn("1 helper", text)
                lst.move_to("ses:Stuck Session")
                await pilot.press("space")
                self.assertIn("↳ wt-helper-1", lst.visible_text())
        run(go())

    def test_empty_states(self):
        w = World(bare=True)
        self.addCleanup(w.close)
        async def go():
            app = fapp.FleetwatchApp(w.snapshot, interval=3600)
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                for key, msg in (("1", "Nothing waiting on you"), ("2", "No to-dos for you"),
                                 ("3", "No sessions found")):
                    await pilot.press(key)
                    self.assertIn(msg, app.current.visible_text())
        run(go())

    def test_cursor_and_expansion_survive_a_refresh_that_adds_and_removes(self):
        self.w.reg("Newcomer", "n1", status="waiting", age=3000)
        self.w.status("n.json", session="Newcomer", updated=standin.iso(-3000), questions=["hello?"])
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                lst = app.lists[0]
                await pilot.press("down")
                self.assertEqual(lst.cursor, "needs:Asker Development")
                await pilot.press("space")
                await pilot.press("2")
                await pilot.press("down", "down")
                cursor2 = app.lists[1].cursor
                await pilot.press("1")
                # a new session appears above, another vanishes
                self.w.reg("Another", "n2", status="waiting", age=10)
                self.w.status("n2.json", session="Another", updated=standin.iso(-10), questions=["x?", "y?"])
                self.w.drop(sid="n1", fname="n.json")
                await refresh(app, pilot)
                self.assertEqual(app.active, 0)
                self.assertEqual(lst.cursor, "needs:Asker Development")
                self.assertIn("1. Approve the schema change?", lst.visible_text())
                self.assertIn("needs:Another", lst._order)
                self.assertNotIn("needs:Newcomer", lst._order)
                self.assertEqual(app.lists[1].cursor, cursor2)
        run(go())

    def test_tied_rows_keep_their_order_when_update_times_change(self):
        self.w.status("a1.json", session="Alpha", updated=standin.iso(-500), questions=["q?"])
        self.w.status("b1.json", session="Beta", updated=standin.iso(-100), questions=["q?"])
        self.w.reg("Alpha", "al", status="idle", age=500)
        self.w.reg("Beta", "be", status="idle", age=100)
        self.w.status("asker.json", session="Asker", persona="Piper", updated=standin.iso(-2400),
                      questions=["a?", "b?"])
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("s")      # most questions: Alpha and Beta tie on one question each
                self.assertEqual(app.sorts[0], "count")
                before = list(app.lists[0]._order)
                self.assertLess(before.index("needs:Alpha"), before.index("needs:Beta"))
                self.w.status("a1.json", session="Alpha", updated=standin.iso(-5), questions=["q?"])
                self.w.reg("Alpha", "al", status="idle", age=5)
                await refresh(app, pilot)
                self.assertEqual(app.lists[0]._order, before)
        run(go())

    def test_cursor_moves_to_neighbour_when_its_session_vanishes(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                lst = app.lists[0]
                await pilot.press("down")
                self.assertEqual(lst.cursor, "needs:Asker Development")
                self.w.drop(fname="asker.json")
                await refresh(app, pilot)
                self.assertEqual(lst.cursor, "needs:Quiet One")
                self.assertNotIn("needs:Asker Development", lst._order)
        run(go())

    def test_scroll_offset_and_screen_position_survive_a_refresh(self):
        for i in range(40):
            self.w.reg("Idle%02d" % i, "idle%d" % i, status="idle", age=600 + i)
        async def go():
            app = self.app()
            async with app.run_test(size=(80, 14)) as pilot:
                await boot(app, pilot)
                await pilot.press("3")
                lst = app.current
                await pilot.press("pagedown", "pagedown")
                await pilot.pause(0.1)
                scroll, cursor = lst.scroll_y, lst.cursor
                self.assertGreater(scroll, 0)
                row = lst._spans[cursor][0] - lst.scroll_y
                await refresh(app, pilot)
                self.assertEqual(lst.cursor, cursor)
                self.assertEqual(lst.scroll_y, scroll)
                # a session appears above the cursor; the cursor stays on the same screen row
                self.w.reg("Aaa newcomer", "new1", status="busy", age=1)
                await refresh(app, pilot)
                self.assertEqual(lst.cursor, cursor)
                self.assertEqual(lst._spans[lst.cursor][0] - lst.scroll_y, row)
        run(go())

    def test_status_expires_and_refresh_key(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                n = app.refreshes
                await pilot.press("r")
                for _ in range(60):
                    await pilot.pause(0.05)
                    if app.refreshes > n:
                        break
                self.assertGreater(app.refreshes, n)
        run(go())

    def test_help_opens_and_any_key_closes(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("question_mark")
                self.assertEqual(type(app.screen).__name__, "HelpScreen")
                await pilot.press("x")
                self.assertEqual(type(app.screen).__name__, "Screen")
                await pilot.press("q")
        run(go())

    def test_untrusted_text_is_cleaned_and_markup_is_literal(self):
        evil = "Evil\x1b]0;pwn\x07[red]x[/red]\x9b"
        self.w.reg(evil, "ev1", status="waiting", age=30)
        self.w.status("ev.json", session=evil, updated=standin.iso(-30),
                      questions=["do it\x1b[31m now [bold]ok[/bold]"])
        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("e")
                text = app.lists[0].visible_text()
                self.assertNotIn("\x1b", text)
                self.assertNotIn("\x07", text)
                self.assertIn("[red]x[/red]", text)
                self.assertIn("[bold]ok[/bold]", text)
        run(go())


class JumpAppTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.calls = []

        def fake_osascript(script, args=()):
            self.calls.append((script, list(args)))
            return True, "ok"
        self.patch = mock.patch.object(iterm, "osascript", fake_osascript)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.w.close()

    def wait_calls(self, pilot, n=1):
        async def inner():
            for _ in range(60):
                await pilot.pause(0.05)
                if len(self.calls) >= n:
                    break
            await pilot.pause(0.1)
        return inner()

    def test_g_jumps_with_the_right_tty_as_argv(self):
        async def go():
            app = fapp.FleetwatchApp(self.w.snapshot, interval=3600)
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("g")
                await self.wait_calls(pilot)
                self.assertEqual(len(self.calls), 1)
                script, args = self.calls[0]
                self.assertEqual(args, [TTY])
                self.assertNotIn(TTY, script)
                self.assertEqual(app.status_text, "jumped to Stuck Session")
        asyncio.run(go())

    def test_g_on_a_dated_todo_row_jumps_to_its_session(self):
        self.w.status("stuck2.json", session="Stuck Session", persona="Robin", updated=standin.iso(-6 * 60),
                      questions=["Which branch should the fix land on?"],
                      upcoming=[{"what": "Review the PR", "due": "2026-10-20", "kind": "meeting"}])
        async def go():
            app = fapp.FleetwatchApp(self.w.snapshot, interval=3600)
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("2")
                await pilot.press("e")
                lst = app.current
                key = next(k for k in lst._order if "Review the PR" in lst._by_key[k].head.plain)
                lst.move_to(key)
                await pilot.press("g")
                await self.wait_calls(pilot)
                self.assertEqual(self.calls[-1][1], [TTY])
        asyncio.run(go())

    def test_session_not_in_iterm_gets_a_toast_and_no_call(self):
        self.w.iterm = {"/dev/ttys500"}
        async def go():
            app = fapp.FleetwatchApp(self.w.snapshot, interval=3600)
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("g")
                await pilot.pause(0.2)
                self.assertEqual(self.calls, [])
                self.assertIn("not in an iTerm2 tab", app.status_text)
        asyncio.run(go())

    def test_exited_session_toast_and_unreachable_iterm_toast(self):
        async def go():
            app = fapp.FleetwatchApp(self.w.snapshot, interval=3600)
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                await pilot.press("down", "g")
                self.assertIn("is not running", app.status_text)
                self.w.iterm = None
                snap = self.w.snapshot()
                snap.jump = {"ttys": {}, "iterm": None, "err": "iTerm2 is not running"}
                app.apply(snap)
                await pilot.press("up", "g")
                await pilot.pause(0.1)
                self.assertEqual(app.status_text, "could not reach iTerm2: iTerm2 is not running")
                self.assertEqual(self.calls, [])
        asyncio.run(go())

    def test_osascript_failure_becomes_a_status_line(self):
        self.patch.stop()
        with mock.patch.object(iterm, "osascript", lambda s, a=(): (False, "Not authorized")):
            async def go():
                app = fapp.FleetwatchApp(self.w.snapshot, interval=3600)
                async with app.run_test(size=(100, 30)) as pilot:
                    await boot(app, pilot)
                    await pilot.press("g")
                    for _ in range(40):
                        await pilot.pause(0.05)
                    self.assertEqual(app.status_text, "could not reach iTerm2: Not authorized")
            asyncio.run(go())
        self.patch.start()


if __name__ == "__main__":
    unittest.main()
