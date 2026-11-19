"""An ended session on Needs you says so, and Enter offers to reopen it."""
import os
import sys
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from test_fleetwatch import dstr, iso, render_env, Env  # noqa: E402
from test_needs_tasks import NOW, view_of  # noqa: E402
from test_resume import AppWorld, sid  # noqa: E402
import standin  # noqa: E402
from fleetwatch import data, iterm, views  # noqa: E402

ITEMS = [{"what": "send the form", "due": dstr(-1), "yours": True}]


class TagTests(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.e.status("gone.json", session="Gone Session", updated=iso(-5 * 86400 * 4), upcoming=ITEMS)

    def test_tui_head_says_not_running(self):
        node = views.needs_nodes(view_of(self.e), NOW)[0]
        self.assertIn("not running", node.head.plain)

    def test_once_output_says_not_running(self):
        text = render_env(self.e)
        line = next(ln for ln in text.splitlines() if "Gone Session" in ln)
        self.assertIn("not running", line)

    def test_live_session_has_no_tag(self):
        e = Env()
        e.reg("Alpha", "s1")
        e.status("a.json", session="Alpha", updated=iso(-60), upcoming=ITEMS)
        self.assertNotIn("not running", views.needs_nodes(view_of(e), NOW)[0].head.plain)
        self.assertNotIn("not running", render_env(e))


class EnterTests(AppWorld):
    def gone(self, name="Gone Session", **kw):
        self.status("gone.json", session=name, updated=standin.iso(-5 * 86400 * 4), upcoming=self.items(), **kw)

    def items(self):
        d = data.datetime.fromtimestamp(standin.NOW, data.LOCAL_TZ).date()
        return [{"what": "send the form", "due": (d - timedelta(days=1)).isoformat(), "yours": True}]

    def status(self, fname, **d):
        import json
        with open(os.path.join(self.paths["status"], fname), "w") as fh:
            json.dump(d, fh)

    def drive_needs(self, body):
        async def go():
            from test_app import boot
            from fleetwatch import app as fapp
            app = self.app()
            async with app.run_test(size=(120, 30)) as pilot:
                await boot(app, pilot)
                for _ in range(100):
                    if app.resume_data is not None:
                        break
                    await pilot.pause(0.05)
                await pilot.press("1")
                await pilot.pause(0.1)
                await body(app, pilot)
        import asyncio
        asyncio.run(go())

    def test_enter_on_the_row_confirms_then_opens_once(self):
        s, _ = self.transcript("Gone Session")
        self.gone(sessionId=s)
        calls = []

        async def body(app, pilot):
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertEqual(type(app.screen).__name__, "ReopenScreen")
            self.assertEqual(calls, [])
            await pilot.press("enter")
            await self.settle(pilot, lambda: "opened Gone Session in a new window" in (app.status_text or ""))
        with mock.patch.object(iterm, "osascript", lambda script, args=(): calls.append(list(args)) or (True, "9\n")):
            self.drive_needs(body)
        self.assertEqual(len(calls), 1)
        self.assertIn("resume %s --here" % s, calls[0][0])

    def test_m_moves_a_session_that_is_not_running(self):
        # Its only row is on Needs you and it has no live record. The move updates
        # the pins a live move wrote (by name and by session id), the grouped board
        # shows it in the chosen group, and moving it back to Other removes both.
        from fleetwatch import config
        s, _ = self.transcript("Gone Session")
        self.gone(sessionId=s)
        path = config.groups_path()
        config.load_groups()                                   # writes the default file
        config.set_override(path, "Gone Session", "Work", session_id=s)   # a pin from when it ran

        async def body(app, pilot):
            await pilot.press("G")
            await pilot.pause(0.1)
            app.current.move_to("needs:Gone Session")
            self.assertNotIn("Gone Session", app.snapshot.records)
            await pilot.press("m")
            await pilot.pause(0.2)
            self.assertEqual(type(app.screen).__name__, "GroupPicker")
            picker = app.screen.query_one("#choices")
            picker.highlighted = app.screen.names.index("Personal")
            await pilot.press("enter")
            await self.settle(pilot, lambda: "moved Gone Session to Personal" in (app.status_text or ""))
            groups, _w = config.load_groups(create=False)
            self.assertEqual(groups.overrides.get("Gone Session"), "Personal")
            self.assertEqual(groups.override_ids.get(s), "Personal")
            await self.settle(pilot, lambda: app.snapshot.grouping.of("Gone Session") == "Personal")
            await self.settle(pilot, lambda: "needs:Gone Session" in app.current._order)
            order = app.current._order
            heads = [k for k in order[:order.index("needs:Gone Session")] if k.startswith("grp:")]
            self.assertTrue(heads and heads[-1].endswith(":Personal"), heads)
            app.current.move_to("needs:Gone Session")
            await pilot.press("m")
            await pilot.pause(0.2)
            picker = app.screen.query_one("#choices")
            picker.highlighted = app.screen.names.index("Other")
            await pilot.press("enter")
            await self.settle(pilot, lambda: "moved Gone Session to Other" in (app.status_text or ""))
            groups, _w = config.load_groups(create=False)
            self.assertNotIn("Gone Session", groups.overrides)
            self.assertNotIn(s, groups.override_ids)
        self.drive_needs(body)

    def test_escape_calls_nothing(self):
        s, _ = self.transcript("Gone Session")
        self.gone(sessionId=s)
        calls = []

        async def body(app, pilot):
            await pilot.press("enter")
            await pilot.pause(0.2)
            await pilot.press("escape")
            await pilot.pause(0.3)
            self.assertNotEqual(type(app.screen).__name__, "ReopenScreen")
        with mock.patch.object(iterm, "osascript", lambda script, args=(): calls.append(args) or (True, "9\n")):
            self.drive_needs(body)
        self.assertEqual(calls, [])

    def test_enter_on_a_task_row_does_the_same(self):
        s, _ = self.transcript("Gone Session")
        self.gone(sessionId=s)
        calls = []

        async def body(app, pilot):
            await pilot.press("right")
            await pilot.press("down")
            node = app.current.current()
            self.assertIn(":t:", node.key)
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertEqual(type(app.screen).__name__, "ReopenScreen")
            await pilot.press("y")
            await self.settle(pilot, lambda: calls)
        with mock.patch.object(iterm, "osascript", lambda script, args=(): calls.append(list(args)) or (True, "9\n")):
            self.drive_needs(body)
        self.assertEqual(len(calls), 1)

    def test_session_without_a_custom_name_gives_a_reason_and_no_dialog(self):
        self.gone(sessionId=sid())
        calls = []

        async def body(app, pilot):
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertNotEqual(type(app.screen).__name__, "ReopenScreen")
            self.assertIn("Gone Session has no custom name, so fleetwatch can't reopen it; prune it with P",
                          app.status_text)
        with mock.patch.object(iterm, "osascript", lambda script, args=(): calls.append(args) or (True, "9\n")):
            self.drive_needs(body)
        self.assertEqual(calls, [])

    def test_folder_gone_gives_a_reason_and_no_dialog(self):
        s, _ = self.transcript("Gone Session", cwd=os.path.join(self.root, "nowhere"))
        self.gone(sessionId=s)

        async def body(app, pilot):
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertNotEqual(type(app.screen).__name__, "ReopenScreen")
            self.assertIn("folder is gone", app.status_text)
        self.drive_needs(body)

    def test_enter_on_a_live_session_still_jumps(self):
        s = sid()
        self.reg("Live One", s, pid=standin.PARENT_PID)
        self.status("live.json", session="Live One", sessionId=s, updated=standin.iso(-60), upcoming=self.items())
        jumped = []

        async def body(app, pilot):
            await pilot.press("enter")
            await pilot.pause(0.3)
            self.assertNotEqual(type(app.screen).__name__, "ReopenScreen")
            self.assertTrue(app.jump_calls or jumped)
        with mock.patch.object(iterm, "jump_to_tty", lambda tty: jumped.append(tty) or (True, "")):
            self.drive_needs(body)


if __name__ == "__main__":
    unittest.main()
