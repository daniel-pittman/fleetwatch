"""Tell a session: the checks that stand between a typed message and a terminal, and the board's `t`."""
import asyncio
import json
import os
import shlex
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import standin  # noqa: E402
from test_app import TTY, boot  # noqa: E402
from test_resume import AppWorld, sid  # noqa: E402
from waiting import settle  # noqa: E402
from fleetwatch import data, iterm, send  # noqa: E402
from textual import events  # noqa: E402
from textual.widgets import Input, Static  # noqa: E402

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_send_tools.py")
PID = 4242
SID = "aa11bb22-0000-4000-8000-0000000000aa"
HOSTILE = 'say "hi" \\ back; do shell script "touch /tmp/never"\'; end tell -- `x` $(y)'


def py(*args):
    return " ".join(shlex.quote(a) for a in (sys.executable, FAKE) + args)


class Rig:
    """The fake commands and the files they read. Tests change the files, then call deliver or press t."""

    def start(self, root, pid, tty_path, status="idle", fg_row=None):
        self.root = root
        self.sessions = os.path.join(root, "sessions")
        os.makedirs(self.sessions, exist_ok=True)
        self.ttys, self.fg = os.path.join(root, "ttys.txt"), os.path.join(root, "fg.txt")
        self.log, self.fail = os.path.join(root, "osascript.log"), os.path.join(root, "fail.txt")
        self.tty_path, self.pid = tty_path, pid
        self.registry(status=status)
        self.map_tty(pid, tty_path)
        self.foreground(fg_row or "%d %d %d S+ claude" % (pid, pid, pid))
        open(self.fail, "w").close()
        self.env = mock.patch.dict(os.environ, {
            "FLEETWATCH_OSASCRIPT": py("osascript", self.log, self.fail, "--"),
            "FLEETWATCH_TTY_CMD": py("show", self.ttys),
            "FLEETWATCH_FG_CMD": py("show", self.fg)})
        self.env.start()

    def stop(self):
        self.env.stop()

    def registry(self, status="idle", pid=None, session=SID, name="Alpha"):
        with open(os.path.join(self.sessions, "r.json"), "w") as fh:
            json.dump({"name": name, "sessionId": session, "pid": pid or self.pid, "kind": "interactive",
                       "cwd": "/x", "status": status, "statusUpdatedAt": 1, "updatedAt": 1}, fh)

    def map_tty(self, pid, tty):
        with open(self.ttys, "w") as fh:
            fh.write("%d %s\n" % (pid, tty[len("/dev/"):]))

    def foreground(self, row):
        with open(self.fg, "w") as fh:
            fh.write(row + "\n")

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]


class Deliver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rig = Rig()
        self.rig.start(self.tmp.name, PID, "/dev/ttys007")
        self.addCleanup(self.rig.stop)
        self.addCleanup(self.tmp.cleanup)

    def go(self, text="please move that to-do to tomorrow"):
        return send.deliver("Alpha", PID, SID, "/dev/ttys007", text, self.rig.sessions)

    def refused(self, text="hello", containing=None):
        ok, reason = self.go(text)
        self.assertFalse(ok, reason)
        self.assertEqual(self.rig.calls(), [], "nothing may reach osascript")
        if containing:
            self.assertIn(containing, reason)
        return reason

    def test_a_clean_send_calls_osascript_once_with_the_tty_and_text(self):
        ok, reason = self.go("Reply with the single word pong.")
        self.assertTrue(ok, reason)
        calls = self.rig.calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["argv"], ["-", "/dev/ttys007", "Reply with the single word pong."])
        self.assertEqual(calls[0]["script"], iterm.ITERM_SEND_SCRIPT)

    def test_hostile_text_reaches_osascript_only_as_an_argument(self):
        ok, reason = self.go(HOSTILE)
        self.assertTrue(ok, reason)
        (call,) = self.rig.calls()
        self.assertEqual(call["argv"], ["-", "/dev/ttys007", HOSTILE])
        self.assertEqual(call["script"], iterm.ITERM_SEND_SCRIPT)
        self.assertNotIn("touch /tmp/never", call["script"])

    def test_the_script_is_constant_and_does_not_steal_focus(self):
        script = iterm.ITERM_SEND_SCRIPT
        self.assertNotIn("%", script)
        for word in ("activate", "select ", "create window", "create tab", "launch"):
            self.assertNotIn(word, script)
        self.assertIn("write text msg newline no", script)
        self.assertIn("(ASCII character 13) newline no", script)

    def test_a_session_at_a_prompt_is_refused_whichever_word_the_registry_uses(self):
        for status in ("blocked", "waiting"):
            self.rig.registry(status=status)
            self.refused(containing="is at a prompt; answer it in its tab")

    def test_working_and_idle_sessions_are_sent_to(self):
        for status in ("idle", "busy", "shell"):
            self.rig.registry(status=status)
            self.assertTrue(self.go()[0], status)

    def test_a_terminal_running_something_else_is_refused_and_named(self):
        for row, shown in (("4242 4242 4242 S+ /bin/zsh", "zsh"), ("4242 4242 4242 S+ -zsh", "zsh"),
                           ("4242 4242 4242 S+ less", "less"), ("4242 4242 4242 S+ vim", "vim"),
                           ("4242 4242 4242 S+ /usr/bin/ssh", "ssh"),
                           ("4242 4242 4242 S+ /opt/My Tools/claude-helper", "claude-helper")):
            self.rig.foreground(row)
            reason = self.refused(containing="not Claude Code")
            self.assertIn("running " + shown, reason)

    def test_a_foreground_group_led_by_another_claude_is_refused(self):
        self.rig.foreground("9999 9999 9999 S+ claude")
        self.refused(containing="another Claude Code")

    def test_a_claude_path_is_accepted(self):
        self.rig.foreground("4242 4242 4242 Ss+ /home/someone/.local/bin/claude")
        self.assertTrue(self.go()[0])

    def test_no_foreground_group_is_refused(self):
        self.rig.foreground("4242 4242 0 S 4242")
        self.refused(containing="nothing is running in the foreground")

    def test_a_terminal_that_changed_hands_is_refused(self):
        self.rig.map_tty(PID, "/dev/ttys009")
        self.refused(containing="no longer on that terminal")

    def test_a_session_that_left_the_terminal_is_refused(self):
        self.rig.map_tty(PID + 1, "/dev/ttys007")
        self.refused(containing="no longer on that terminal")

    def test_a_registry_row_for_another_session_is_refused(self):
        self.rig.registry(session="bb22cc33-0000-4000-8000-0000000000bb")
        self.refused(containing="no longer registered")

    def test_a_missing_registry_row_is_refused(self):
        os.remove(os.path.join(self.rig.sessions, "r.json"))
        self.refused(containing="no longer registered")

    def test_iterm_not_running_is_refused_with_its_reason(self):
        with open(self.rig.fail, "w") as fh:
            fh.write("0:20: execution error: iTerm2 is not running (-2700)\n")
        ok, reason = self.go()
        self.assertFalse(ok)
        self.assertIn("iTerm2 is not running", reason)

    def test_no_iterm_session_on_the_terminal_is_refused_with_its_reason(self):
        with open(self.rig.fail, "w") as fh:
            fh.write("0:20: execution error: no iTerm2 session on that terminal (-2700)\n")
        ok, reason = self.go()
        self.assertFalse(ok)
        self.assertIn("no iTerm2 session", reason)

    def test_a_tty_that_is_not_a_terminal_device_never_reaches_osascript(self):
        ok, reason = iterm.send_to_tty("/dev/ttys1; rm -rf", "hi")
        self.assertFalse(ok)
        self.assertEqual(self.rig.calls(), [])

    def test_text_that_may_not_be_sent(self):
        cases = {"": "type a message first", "   ": "type a message first",
                 "two\nlines": "one line only", "a\rb": "one line only", "a\tb": "one line only",
                 "esc\x1b[201~": "one line only", "del\x7f": "one line only", "next\x85line": "one line only",
                 "sep x": "one line only", "para x": "one line only",
                 "x" * (send.MAX_LEN + 1): "too long"}
        for text, why in cases.items():
            with self.subTest(text=repr(text[:12])):
                self.refused(text, containing=why)

    def test_the_limit_itself_is_allowed(self):
        self.assertTrue(self.go("x" * send.MAX_LEN)[0])

    def test_accented_and_wide_text_is_allowed(self):
        self.assertTrue(self.go("déjà vu 日本語 🙂")[0])
        self.assertEqual(self.rig.calls()[0]["argv"][2], "déjà vu 日本語 🙂")


class Foreground(unittest.TestCase):
    PS_CLAUDE = """ 37482 46022 46022 S+   caffeinate
 45743 45743 46022 Ss   /opt/homebrew/bin/bash
 46022 46022 46022 R+   claude
 46119 46022 46022 S+   /opt/tools/venv/bin/python
"""
    PS_SHELL_AFTER_CLAUDE = """ 45743 45743 45743 Ss+  /opt/homebrew/bin/bash
"""
    PS_SPACES = " 700 700 700 S+ /Applications/My App.app/Contents/MacOS/claude\n"

    def test_the_leader_of_the_foreground_group_is_the_foreground_process(self):
        self.assertEqual(send.parse_foreground(self.PS_CLAUDE), (46022, "claude"))

    def test_a_shell_at_its_prompt(self):
        self.assertEqual(send.parse_foreground(self.PS_SHELL_AFTER_CLAUDE), (45743, "/opt/homebrew/bin/bash"))

    def test_a_command_with_spaces_stays_whole(self):
        self.assertEqual(send.parse_foreground(self.PS_SPACES)[1], "/Applications/My App.app/Contents/MacOS/claude")

    def test_nothing_in_the_foreground(self):
        self.assertIsNone(send.parse_foreground(""))
        self.assertIsNone(send.parse_foreground(" 12 12 0 Ss bash\n"))
        self.assertIsNone(send.parse_foreground("garbage\n"))


class Board(AppWorld):
    """`t` on the board, with the same fake commands under it."""

    def setUp(self):
        super().setUp()
        self.rig = Rig()
        self.rig.start(self.root, standin.PARENT_PID, TTY)
        os.remove(os.path.join(self.rig.sessions, "r.json"))     # the board's own registry row stands in
        self.s = sid()
        self.reg("Live One", self.s, pid=standin.PARENT_PID, cwd=self.folder)
        d = data.datetime.fromtimestamp(standin.NOW, data.LOCAL_TZ).date()
        with open(os.path.join(self.paths["status"], "live.json"), "w") as fh:
            json.dump({"session": "Live One", "sessionId": self.s, "updated": standin.iso(-60),
                       "upcoming": [{"what": "send the form", "due": (d - timedelta(days=1)).isoformat(),
                                     "yours": True}]}, fh)
        self.addCleanup(self.rig.stop)

    def reg_status(self, status):
        path = os.path.join(self.paths["sessions"], "%s.json" % self.s)
        with open(path) as fh:
            row = json.load(fh)
        row["status"] = status
        with open(path, "w") as fh:
            json.dump(row, fh)

    def drive_needs(self, body):
        async def go():
            app = self.app()
            async with app.run_test(size=(120, 30)) as pilot:
                await boot(app, pilot)
                await settle(pilot, lambda: app.resume_data is not None, "the resume data to load")
                await pilot.press("1")
                await settle(pilot, lambda: app.active == 0, "the needs tab")
                await body(app, pilot)
        asyncio.run(go())

    def screen_name(self, app):
        return type(app.screen).__name__

    def test_t_types_a_message_and_enter_sends_it(self):
        async def body(app, pilot):
            await pilot.press("t")
            await settle(pilot, lambda: self.screen_name(app) == "SendScreen", "the send box")
            title = [str(w.render()) for w in app.screen.query(Static)]
            self.assertIn("Tell Live One", title[0])
            self.assertIn(self.folder.split(os.sep)[-1], title[1])
            await pilot.press(*"hello there")
            await pilot.press("enter")
            await settle(pilot, lambda: "sent to Live One" in (app.status_text or ""), "the footer")
            self.assertNotEqual(self.screen_name(app), "SendScreen")
        self.drive_needs(body)
        (call,) = self.rig.calls()
        self.assertEqual(call["argv"], ["-", TTY, "hello there"])

    def test_esc_cancels_and_sends_nothing(self):
        async def body(app, pilot):
            await pilot.press("t")
            await settle(pilot, lambda: self.screen_name(app) == "SendScreen")
            await pilot.press(*"never sent")
            await pilot.press("escape")
            await settle(pilot, lambda: self.screen_name(app) != "SendScreen")
            self.assertNotIn("sent to", app.status_text or "")
        self.drive_needs(body)
        self.assertEqual(self.rig.calls(), [])

    def test_an_empty_message_is_refused_in_the_box(self):
        async def body(app, pilot):
            await pilot.press("t")
            await settle(pilot, lambda: self.screen_name(app) == "SendScreen")
            await pilot.press("enter")
            await settle(pilot, lambda: "type a message first" in str(app.screen.query_one("#problem").render()))
            self.assertEqual(self.screen_name(app), "SendScreen")
        self.drive_needs(body)
        self.assertEqual(self.rig.calls(), [])

    def test_a_refusal_shows_in_the_box_and_keeps_the_text(self):
        self.rig.foreground("%d %d %d S+ /bin/zsh" % ((standin.PARENT_PID,) * 3))

        async def body(app, pilot):
            await pilot.press("t")
            await settle(pilot, lambda: self.screen_name(app) == "SendScreen")
            await pilot.press(*"rm everything")
            await pilot.press("enter")
            await settle(pilot, lambda: "running zsh, not Claude Code" in str(app.screen.query_one("#problem").render()))
            self.assertEqual(self.screen_name(app), "SendScreen")
            self.assertEqual(app.screen.query_one("#message", Input).value, "rm everything")
            self.assertNotIn("sent to", app.status_text or "")
        self.drive_needs(body)
        self.assertEqual(self.rig.calls(), [])

    def test_a_session_that_went_to_a_prompt_after_the_last_refresh_is_refused(self):
        async def body(app, pilot):
            await pilot.press("t")
            await settle(pilot, lambda: self.screen_name(app) == "SendScreen")
            self.reg_status("blocked")     # the board's snapshot still says idle
            await pilot.press(*"yes")
            await pilot.press("enter")
            await settle(pilot, lambda: "is at a prompt; answer it in its tab" in str(
                app.screen.query_one("#problem").render()))
        self.drive_needs(body)
        self.assertEqual(self.rig.calls(), [])

    def test_a_pasted_line_break_is_refused_not_trimmed(self):
        async def body(app, pilot):
            await pilot.press("t")
            await settle(pilot, lambda: self.screen_name(app) == "SendScreen")
            box = app.screen.query_one("#message", Input)
            box.post_message(events.Paste("first line\nsecond line"))
            await settle(pilot, lambda: "one line only" in str(app.screen.query_one("#problem").render()))
            self.assertEqual(box.value, "")
            box.post_message(events.Paste("one clean line"))
            await settle(pilot, lambda: box.value == "one clean line", "the clean paste")
        self.drive_needs(body)

    def test_the_demo_only_says_what_t_would_do(self):
        async def body(app, pilot):
            with mock.patch.dict(os.environ, {"FLEETWATCH_DEMO": "1"}):
                await pilot.press("t")
                await settle(pilot, lambda: "demo: t would send to Live One" in (app.status_text or ""))
            self.assertNotEqual(self.screen_name(app), "SendScreen")
        self.drive_needs(body)
        self.assertEqual(self.rig.calls(), [])

    def test_t_on_a_session_that_ended_offers_the_reopen_not_a_box(self):
        os.remove(os.path.join(self.paths["sessions"], "%s.json" % self.s))
        s, _ = self.transcript("Live One")
        with open(os.path.join(self.paths["status"], "live.json")) as fh:
            row = json.load(fh)
        row["sessionId"] = s
        with open(os.path.join(self.paths["status"], "live.json"), "w") as fh:
            json.dump(row, fh)

        async def body(app, pilot):
            await pilot.press("t")
            await settle(pilot, lambda: self.screen_name(app) == "ReopenScreen", "the reopen offer")
        self.drive_needs(body)
        self.assertEqual(self.rig.calls(), [])

    def test_t_on_a_session_outside_iterm_says_so(self):
        with open(self.rig.ttys, "w") as fh:
            fh.write("")

        async def body(app, pilot):
            app.snapshot.jump["iterm"] = set()
            await pilot.press("t")
            await settle(pilot, lambda: "not in an iTerm2 tab" in (app.status_text or ""))
            self.assertNotEqual(self.screen_name(app), "SendScreen")
        self.drive_needs(body)
        self.assertEqual(self.rig.calls(), [])

    def test_help_and_binding_list_the_key(self):
        from fleetwatch import app as fapp
        self.assertTrue(any(keys == "t" for keys, _ in fapp.HELP_KEYS))
        self.assertTrue(any(b.key == "t" and b.action == "tell" for b in fapp.FleetwatchApp.BINDINGS))


if __name__ == "__main__":
    unittest.main()
