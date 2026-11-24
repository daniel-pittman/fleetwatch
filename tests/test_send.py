"""Tell a session: the checks that stand between a typed message and a terminal, and the board's `t`."""
import asyncio
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
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
from fleetwatch import app as fapp, data, iterm, park, send, views  # noqa: E402
from textual import events  # noqa: E402
from textual.widgets import Input, Static  # noqa: E402

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_send_tools.py")
PID = 4242
SID = "aa11bb22-0000-4000-8000-0000000000aa"
OTHER_SID = "bb22cc33-0000-4000-8000-0000000000bb"
HOSTILE = 'say "hi" \\ back; do shell script "touch /tmp/never"\'; end tell -- `x` $(y)'


def py(*args):
    return " ".join(shlex.quote(a) for a in (sys.executable, FAKE) + args)


# Claude Code's executable is named for its version, so its ucomm is a version and only its argv says claude.
CLAUDE_UCOMM = "2.1.330"


def row(pid, pgid, tpgid, stat, args, ucomm=None):
    """One `ps -o pid=,pgid=,tpgid=,stat=,ucomm=,args=` row; ucomm defaults to the argv's program."""
    if ucomm is None:
        ucomm = os.path.basename(args.split()[0]).lstrip("-")
    return "%d %d %d %s %s %s" % (pid, pgid, tpgid, stat, ucomm, args)


def claude_rows(pid, *members):
    """`ps -t` rows for a terminal whose foreground group Claude (pid) leads, with members in it. A member
    is its argv, or (ucomm, argv) when the executable's own name differs from the argv's."""
    rows = [row(pid, pid, pid, "S+", "claude --resume x", CLAUDE_UCOMM)]
    for i, m in enumerate(members):
        ucomm, args = m if isinstance(m, tuple) else (None, m)
        rows.append(row(pid + 1 + i, pid, pid, "S+", args, ucomm))
    return "\n".join(rows)


class Rig:
    """The fake commands and the files they read. Tests change the files, then call deliver or press t."""

    def start(self, root, pid, tty_path, status="idle", fg_row=None):
        self.root = root
        self.sessions = os.path.join(root, "sessions")
        os.makedirs(self.sessions, exist_ok=True)
        self.fg = os.path.join(root, "fg.txt")
        self.log, self.fail = os.path.join(root, "osascript.log"), os.path.join(root, "fail.txt")
        self.then = os.path.join(root, "then.json")
        self.tty_path, self.pid = tty_path, pid
        self.agents_ran = os.path.join(root, "agents-ran")
        # `claude agents` is never read at send time; this one leaves a mark if it is.
        self.paths = {"sessions": self.sessions, "command": py("osascript", self.agents_ran, self.fail, self.then, "--")}
        self.registry(status=status)
        self.foreground(fg_row or claude_rows(pid))
        open(self.fail, "w").close()
        self.env = mock.patch.dict(os.environ, {
            "FLEETWATCH_OSASCRIPT": py("osascript", self.log, self.fail, self.then, "--"),
            "FLEETWATCH_FG_CMD": py("show", self.fg, "-t")})
        self.env.start()
        self.settle = mock.patch.object(send, "SETTLE_S", 0)
        self.settle.start()

    def stop(self):
        self.settle.stop()
        self.env.stop()

    def registry_row(self, status="idle", pid=None, session=SID, name="Alpha"):
        return json.dumps({"name": name, "sessionId": session, "pid": pid or self.pid, "kind": "interactive",
                           "cwd": "/x", "status": status, "statusUpdatedAt": 1, "updatedAt": 1})

    def registry(self, fname="r.json", **row):
        with open(os.path.join(self.sessions, fname), "w") as fh:
            fh.write(self.registry_row(**row))

    def foreground(self, rows, tty=None):
        with open(self.fg, "w") as fh:
            fh.write(rows + "\n")
        with open(self.fg + ".tty", "w") as fh:
            fh.write((tty or self.tty_path)[len("/dev/"):])

    def after_first_call(self, changes):
        """Files to rewrite once the first osascript call (the typing) has run."""
        with open(self.then, "w") as fh:
            json.dump(changes, fh)

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

    def go(self, text="please move that to-do to tomorrow", sid_=SID, tty="/dev/ttys007"):
        return send.deliver("Alpha", PID, sid_, tty, text, self.rig.paths)

    def sent(self, text="please move that to-do to tomorrow", **kw):
        ok, reason = self.go(text, **kw)
        self.assertTrue(ok, reason)
        calls = self.rig.calls()
        self.assertEqual(len(calls), 2, calls)
        os.remove(self.rig.log)
        return calls

    def refused(self, text="hello", containing=None, **kw):
        ok, reason = self.go(text, **kw)
        self.assertFalse(ok, reason)
        self.assertEqual(self.rig.calls(), [], "nothing may reach osascript")
        if containing:
            self.assertIn(containing, reason)
        return reason

    def test_a_clean_send_types_the_text_then_sends_the_return(self):
        typed, submitted = self.sent("Reply with the single word pong.")
        self.assertEqual(typed["argv"], ["-", "/dev/ttys007", "Reply with the single word pong."])
        self.assertEqual(typed["script"], iterm.ITERM_TYPE_SCRIPT)
        self.assertEqual(submitted["argv"], ["-", "/dev/ttys007"])
        self.assertEqual(submitted["script"], iterm.ITERM_SUBMIT_SCRIPT)

    def test_hostile_text_reaches_osascript_only_as_an_argument(self):
        typed, submitted = self.sent(HOSTILE)
        self.assertEqual(typed["argv"], ["-", "/dev/ttys007", HOSTILE])
        for call in (typed, submitted):
            self.assertIn(call["script"], iterm.SEND_SCRIPTS)
            self.assertNotIn("touch /tmp/never", call["script"])
        self.assertNotIn(HOSTILE, submitted["argv"])

    def test_the_scripts_are_constant_and_do_not_steal_focus(self):
        self.assertEqual(len(iterm.SEND_SCRIPTS), 2)
        for script in iterm.SEND_SCRIPTS:
            with self.subTest(script=script[-120:]):
                self.assertNotIn("%", script)
                for word in ("activate", "select ", "create window", "create tab", "launch"):
                    self.assertNotIn(word, script)
                self.assertIn("newline no", script)
                self.assertEqual(script.count("tty of sessions of tabs of windows"), 1, "one bulk read")
                self.assertIn("if (tty of found) is not target then error", script)
        self.assertIn("write text (item 2 of argv) newline no", iterm.ITERM_TYPE_SCRIPT)
        self.assertIn("(ASCII character 13) newline no", iterm.ITERM_SUBMIT_SCRIPT)
        self.assertNotIn("item 2 of argv", iterm.ITERM_SUBMIT_SCRIPT)

    def test_a_session_at_a_prompt_is_refused_whichever_word_the_registry_uses(self):
        for status in ("blocked", "waiting"):
            self.rig.registry(status=status)
            self.refused(containing="is at a prompt; answer it in its tab")

    def test_working_and_idle_sessions_are_sent_to(self):
        for status in ("idle", "busy", "shell"):
            self.rig.registry(status=status)
            self.sent()

    def test_a_state_that_is_not_known_to_be_safe_is_refused(self):
        for status in ("compacting", "unknown"):
            with self.subTest(status=status):
                self.rig.registry(status=status)
                self.refused(containing="cannot tell whether Alpha is at a prompt (state %s)" % status)

    def test_an_empty_registry_status_is_refused_without_asking_claude_agents(self):
        for status in (None, ""):
            with self.subTest(status=status):
                self.rig.registry(status=status)
                self.assertEqual(self.refused(), "cannot tell whether Alpha is at a prompt: its status is "
                                                 "unknown; check its tab")
        self.assertFalse(os.path.exists(self.rig.agents_ran), "claude agents was run")

    def test_two_rows_on_one_pid_without_ids_take_the_last_and_any_prompt_refuses(self):
        self.rig.registry("a.json", status="idle", session=None)
        self.rig.registry("b.json", status="blocked", session=None)
        os.remove(os.path.join(self.rig.sessions, "r.json"))
        self.refused(containing="is at a prompt", sid_=None)
        self.rig.registry("b.json", status="busy", session=None)
        self.sent(sid_=None)

    def test_the_last_row_carrying_the_id_decides_the_state(self):
        os.remove(os.path.join(self.rig.sessions, "r.json"))
        self.rig.registry("a.json", status="idle")
        self.rig.registry("b.json", status=None)
        self.refused(containing="cannot tell whether Alpha is at a prompt")

    def test_another_sessions_row_on_the_same_pid_at_a_prompt_refuses(self):
        self.rig.registry("z.json", status="waiting", session=OTHER_SID)
        self.refused(containing="is at a prompt")

    def test_a_terminal_running_something_else_is_refused_and_named(self):
        for args, shown in (("/bin/zsh", "zsh"), ("-zsh", "zsh"), ("less", "less"), ("vim", "vim"),
                            ("/usr/bin/ssh host", "ssh"), ("/opt/tools/claude-helper --x", "claude-helper"),
                            ("/opt/My Tools/claude", "My")):
            self.rig.foreground(row(4242, 4242, 4242, "S+", args, CLAUDE_UCOMM))
            reason = self.refused(containing="not Claude Code")
            self.assertIn("running " + shown, reason)

    def test_anything_in_the_group_that_is_not_a_known_helper_is_refused_and_named(self):
        for member, shown in (("vim", "vim"), ("/usr/bin/nvim", "nvim"), ("nvim --embed", "nvim"),
                              ("/usr/bin/less -R", "less"), ("man ls", "man"), ("nano", "nano"),
                              ("/bin/ed", "ed"), ("joe x.txt", "joe"), ("pico", "pico"), (("vim", "view"), "vim"),
                              ("vimdiff a b", "vimdiff"), ("emacsclient -t", "emacsclient"), ("ssh host", "ssh"),
                              ("fzf", "fzf"), (("Electron", "/opt/My Editor/editor --wait"), "Electron")):
            with self.subTest(member=member):
                self.rig.foreground(claude_rows(PID, "caffeinate -i -t 300", member))
                self.assertEqual(self.refused(containing="finish there first"),
                                 "%s is running in Alpha; finish there first" % shown)

    def test_an_editor_nobody_listed_is_refused_with_no_editor_variable_set(self):
        self.rig.foreground(claude_rows(PID, "/opt/bin/myedit --wait"))
        env = {k: v for k, v in os.environ.items() if k not in ("EDITOR", "VISUAL")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.refused(containing="myedit is running in Alpha")

    def test_a_shell_without_a_command_string_or_a_login_shell_is_refused(self):
        for member, shown in (("-bash", "bash"), ("/bin/zsh", "zsh"), ("bash -i -c x", "bash"),
                              ("-bash -i -c claude", "bash"), ("-zsh -c x", "zsh"), ("bash script.sh -c", "bash"),
                              ("bash -o pipefail -c x", "bash"), ("/bin/zsh -ic x", "zsh")):
            with self.subTest(member=member):
                self.rig.foreground(claude_rows(PID, member))
                self.assertEqual(self.refused(), "%s is running in Alpha; finish there first" % shown)

    def test_the_helpers_seen_in_live_sessions_do_not_refuse(self):
        self.rig.foreground(claude_rows(
            PID, "caffeinate -i -t 300", ("python3.11", "/opt/tools/venv/bin/python -m server"),
            ("python3.14", "/opt/cache/bin/python"), ("python", "python mcp"),
            ("node", "npm exec @scope/mcp@latest"), ("node", "node /opt/x/index.js --stdio"),
            ("slack-mcp-server", "/opt/x/bin/slack-mcp-server-darwin-arm64 --transport stdio"),
            ("uv", "/opt/homebrew/bin/uv tool uvx x"), ("dart", "/opt/dart-sdk/bin/dart mcp-server"),
            "/bin/bash -c ls", "bash -lc 'x y'", "/bin/zsh -c -l 'eval x'", "sh -ec x", "bash --norc -c x"))
        self.sent()

    def test_an_editor_outside_the_foreground_group_does_not_refuse(self):
        self.rig.foreground(claude_rows(PID) + "\n" + row(5000, 5000, PID, "Ss", "vim"))
        self.sent()

    def test_a_line_that_does_not_parse_is_refused(self):
        self.rig.foreground(claude_rows(PID) + "\n5000 5000 %d Ss" % PID)
        self.refused(containing="cannot read what is running on Alpha's terminal")

    def test_a_stopped_or_exited_claude_is_refused(self):
        for stat, why in (("T+", "is stopped"), ("Z+", "has exited")):
            self.rig.foreground(row(4242, 4242, 4242, stat, "claude", CLAUDE_UCOMM))
            self.refused(containing="Alpha's Claude Code " + why)

    def test_a_foreground_group_led_by_another_claude_is_refused(self):
        self.rig.foreground(row(4242, 4242, 9999, "S", "claude") + "\n" + row(9999, 9999, 9999, "S+", "claude"))
        self.refused(containing="another Claude Code")

    def test_a_claude_path_is_accepted(self):
        self.rig.foreground(row(4242, 4242, 4242, "Ss+", "/home/someone/.local/bin/claude --resume x", CLAUDE_UCOMM))
        self.sent()

    def test_no_foreground_group_is_refused(self):
        self.rig.foreground(row(4242, 4242, 0, "S", "claude"))
        self.refused(containing="nothing is running in the foreground")

    def test_a_session_that_left_the_terminal_is_refused(self):
        self.rig.foreground(row(5555, 5555, 5555, "S+", "claude"))
        self.refused(containing="no longer on that terminal")

    def test_the_terminal_read_names_the_tty(self):
        self.rig.foreground(claude_rows(PID), tty="/dev/ttys009")
        self.refused(containing="no longer on that terminal")

    def test_a_registry_row_for_another_session_is_refused(self):
        self.rig.registry(session=OTHER_SID)
        self.refused(containing="no longer registered")

    def test_a_missing_registry_row_is_refused(self):
        os.remove(os.path.join(self.rig.sessions, "r.json"))
        self.refused(containing="no longer registered")

    def test_the_terminal_is_checked_before_the_prompt_state(self):
        self.rig.foreground(row(4242, 4242, 4242, "S+", "/bin/zsh"))
        self.rig.registry(status="blocked")
        self.refused(containing="running zsh")

    def test_a_prompt_that_opens_while_the_text_is_typed_stops_the_return(self):
        self.rig.after_first_call({os.path.join(self.rig.sessions, "r.json"): self.rig.registry_row(status="waiting")})
        ok, reason = self.go("yes")
        self.assertFalse(ok)
        self.assertEqual(reason, "typed but not submitted: Alpha is at a prompt; answer it in its tab; check its tab")
        self.assertEqual([c["script"] for c in self.rig.calls()], [iterm.ITERM_TYPE_SCRIPT])

    def test_an_editor_that_opens_while_the_text_is_typed_stops_the_return(self):
        self.rig.after_first_call({self.rig.fg: claude_rows(PID, "vim") + "\n"})
        ok, reason = self.go("yes")
        self.assertFalse(ok)
        self.assertTrue(reason.startswith(send.TYPED + ": vim is running in Alpha"), reason)
        self.assertEqual(len(self.rig.calls()), 1)

    def test_a_failed_return_says_the_text_was_typed(self):
        self.rig.after_first_call({self.rig.fail: "0:20: execution error: no iTerm2 session on that terminal (-2700)\n"})
        ok, reason = self.go("yes")
        self.assertFalse(ok)
        self.assertEqual(reason, "typed but not submitted: no iTerm2 session on that terminal (-2700); check its tab")
        self.assertEqual(len(self.rig.calls()), 2)

    def test_iterm_not_running_is_refused_with_its_reason(self):
        with open(self.rig.fail, "w") as fh:
            fh.write("0:20: execution error: iTerm2 is not running (-2700)\n")
        ok, reason = self.go()
        self.assertFalse(ok)
        self.assertEqual(reason, "iTerm2 is not running (-2700)")
        self.assertEqual(len(self.rig.calls()), 1, "no return after a failed typing")

    def test_a_timeout_while_typing_says_the_text_may_be_typed(self):
        with mock.patch.object(iterm, "type_into_tty", return_value=(False, iterm.TIMED_OUT)), \
                mock.patch.object(iterm, "submit_tty") as submit:
            ok, reason = self.go("yes")
        self.assertFalse(ok)
        self.assertEqual(reason, "typed but not submitted: timed out talking to iTerm2; check its tab")
        submit.assert_not_called()

    def test_a_timeout_never_quotes_the_command_line(self):
        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 10)
        with mock.patch.object(iterm.subprocess, "run", slow):
            ok, reason = iterm.type_into_tty("/dev/ttys007", "the secret plan")
        self.assertFalse(ok)
        self.assertEqual(reason, "timed out talking to iTerm2")

    def test_a_tty_that_is_not_a_terminal_device_never_reaches_osascript(self):
        for call in (lambda: iterm.type_into_tty("/dev/ttys1; rm -rf", "hi"),
                     lambda: iterm.submit_tty("/dev/ttys1; rm -rf"),
                     lambda: self.go(tty="/dev/ttys1; rm -rf")):
            ok, reason = call()
            self.assertFalse(ok)
            self.assertEqual(reason, "not a terminal device")
        self.assertEqual(self.rig.calls(), [])

    def test_text_that_may_not_be_sent(self):
        cases = {"": "type a message first", "   ": "type a message first", "  \t ": "one line only",
                 "two\nlines": "one line only", "a\rb": "one line only", "a\tb": "one line only",
                 "esc\x1b[201~": "one line only", "del\x7f": "one line only", "next\x85line": "one line only",
                 "sep x": "one line only", "para x": "one line only",
                 "x" * (send.MAX_LEN + 1): "too long"}
        for text, why in cases.items():
            with self.subTest(text=repr(text[:12])):
                self.refused(text, containing=why)

    def test_the_limit_itself_is_allowed(self):
        self.sent("x" * send.MAX_LEN)

    def test_accented_and_wide_text_is_allowed(self):
        self.assertEqual(self.sent("déjà vu 日本語 🙂")[0]["argv"][2], "déjà vu 日本語 🙂")


class Terminal(unittest.TestCase):
    PS_CLAUDE = """ 37482 46022 46022 S+   caffeinate       caffeinate -i -t 300
 45743 45743 46022 Ss   bash             /opt/homebrew/bin/bash -c sleep 9
 46022 46022 46022 R+   2.1.330          claude --resume x
 46119 46022 46022 S+   python3.11       /opt/tools/venv/bin/python -m server
 46120 46022 46022 S+   vim              vim /tmp/claude-prompt.md
"""
    PS_SHELL_AFTER_CLAUDE = """ 45743 45743 45743 Ss+  bash             -bash
 46022 46022 45743 T    2.1.330          claude
"""
    PS_SPACES = " 700 700 700 S+ claude /Applications/My App.app/Contents/MacOS/claude --x\n"

    def test_the_leader_and_the_other_members_of_the_foreground_group(self):
        term = send.parse_terminal(self.PS_CLAUDE)
        self.assertEqual(term["pids"], {37482, 45743, 46022, 46119, 46120})
        self.assertFalse(term["bad"])
        fg = term["fg"]
        self.assertEqual((fg["leader"], fg["stat"], fg["command"]), (46022, "R+", "claude --resume x"))
        self.assertEqual(fg["members"], [(37482, "caffeinate", "caffeinate -i -t 300"),
                                         (46119, "python3.11", "/opt/tools/venv/bin/python -m server"),
                                         (46120, "vim", "vim /tmp/claude-prompt.md")])
        self.assertEqual(send.foreign_member(fg["members"]), "vim")

    def test_a_shell_at_its_prompt(self):
        fg = send.parse_terminal(self.PS_SHELL_AFTER_CLAUDE)["fg"]
        self.assertEqual((fg["leader"], fg["command"], fg["members"]), (45743, "-bash", []))

    def test_a_command_with_spaces_stays_whole(self):
        fg = send.parse_terminal(self.PS_SPACES)["fg"]
        self.assertEqual(fg["command"], "/Applications/My App.app/Contents/MacOS/claude --x")

    def test_nothing_in_the_foreground(self):
        self.assertIsNone(send.parse_terminal("")["fg"])
        self.assertIsNone(send.parse_terminal(" 12 12 0 Ss bash -bash\n")["fg"])
        self.assertIsNone(send.parse_terminal("garbage\n")["fg"])
        self.assertTrue(send.parse_terminal("garbage\n")["bad"])
        self.assertEqual(send.parse_terminal(" 12 12 0 Ss bash -bash\n")["pids"], {12})


def all_nodes(nodes):
    for n in nodes:
        yield n
        yield from all_nodes(n.children)


class Board(AppWorld):
    """`t` on the board, with the same fake commands under it."""

    TTY2, TTY3 = "/dev/ttys098", "/dev/ttys097"

    def setUp(self):
        super().setUp()
        self.extra_ttys = {}
        self.rig = Rig()
        self.rig.start(self.root, standin.PARENT_PID, TTY)
        os.remove(os.path.join(self.rig.sessions, "r.json"))     # the board's own registry row stands in
        self.s = sid()
        self.reg("Live One", self.s, pid=standin.PARENT_PID, cwd=self.folder)
        self.due = (data.datetime.fromtimestamp(standin.NOW, data.LOCAL_TZ).date() - timedelta(days=1)).isoformat()
        self.status_file("live.json", "Live One", self.s)
        self.addCleanup(self.rig.stop)

    def status_file(self, fname, name, session, what="send the form"):
        with open(os.path.join(self.paths["status"], fname), "w") as fh:
            json.dump({"session": name, "sessionId": session, "updated": standin.iso(-60),
                       "upcoming": [{"what": what, "due": self.due, "yours": True}]}, fh)

    def snapshot(self):
        snap = super().snapshot()
        snap.jump["ttys"].update(self.extra_ttys)
        snap.jump["iterm"] |= set(self.extra_ttys.values())
        return snap

    def reg_status(self, status, session=None):
        path = os.path.join(self.paths["sessions"], "%s.json" % (session or self.s))
        with open(path) as fh:
            row = json.load(fh)
        row["status"] = status
        with open(path, "w") as fh:
            json.dump(row, fh)

    def drive_tab(self, key, body):
        async def go():
            app = self.app()
            async with app.run_test(size=(120, 30)) as pilot:
                await boot(app, pilot)
                await settle(pilot, lambda: app.resume_data is not None, "the resume data to load")
                await pilot.press(key)
                await settle(pilot, lambda: app.active == int(key) - 1, "tab " + key)
                await body(app, pilot)
        asyncio.run(go())

    def drive_needs(self, body):
        self.drive_tab("1", body)

    def screen_name(self, app):
        return type(app.screen).__name__

    def box_texts(self, app):
        return [str(w.render()) for w in app.screen.query(Static)]

    def problem(self, app):
        return str(app.screen.query_one("#problem").render())

    async def open_box(self, app, pilot, node=None):
        if node is None:
            await pilot.press("t")
            await settle(pilot, lambda: self.screen_name(app) == "SendScreen", "the send box")
            return
        with mock.patch.object(app.current, "current", return_value=node):
            await pilot.press("t")
            await settle(pilot, lambda: self.screen_name(app) == "SendScreen", "the send box")

    async def send_text(self, app, pilot, text):
        await pilot.press(*text)
        await pilot.press("enter")

    def test_t_types_a_message_and_enter_sends_it(self):
        async def body(app, pilot):
            await self.open_box(app, pilot)
            title = self.box_texts(app)
            self.assertIn("Tell Live One", title[0])
            self.assertIn(self.folder.split(os.sep)[-1], title[1])
            await self.send_text(app, pilot, "hello there")
            await settle(pilot, lambda: "sent to Live One" in (app.status_text or ""), "the footer")
            self.assertNotEqual(self.screen_name(app), "SendScreen")
        self.drive_needs(body)
        typed, submitted = self.rig.calls()
        self.assertEqual(typed["argv"], ["-", TTY, "hello there"])
        self.assertEqual(submitted["argv"], ["-", TTY])

    def test_esc_cancels_and_sends_nothing(self):
        async def body(app, pilot):
            await self.open_box(app, pilot)
            await pilot.press(*"never sent")
            await pilot.press("escape")
            await settle(pilot, lambda: self.screen_name(app) != "SendScreen")
            self.assertNotIn("sent to", app.status_text or "")
        self.drive_needs(body)
        self.assertEqual(self.rig.calls(), [])

    def test_an_empty_message_is_refused_in_the_box(self):
        async def body(app, pilot):
            await self.open_box(app, pilot)
            await pilot.press("enter")
            await settle(pilot, lambda: "type a message first" in self.problem(app))
            self.assertEqual(self.screen_name(app), "SendScreen")
        self.drive_needs(body)
        self.assertEqual(self.rig.calls(), [])

    def test_a_refusal_shows_in_the_box_and_keeps_the_text(self):
        self.rig.foreground(row(standin.PARENT_PID, standin.PARENT_PID, standin.PARENT_PID, "S+", "/bin/zsh"))

        async def body(app, pilot):
            await self.open_box(app, pilot)
            await self.send_text(app, pilot, "rm everything")
            await settle(pilot, lambda: "running zsh, not Claude Code" in self.problem(app))
            self.assertEqual(self.screen_name(app), "SendScreen")
            self.assertEqual(app.screen.query_one("#message", Input).value, "rm everything")
            self.assertNotIn("sent to", app.status_text or "")
        self.drive_needs(body)
        self.assertEqual(self.rig.calls(), [])

    def test_a_session_that_went_to_a_prompt_after_the_last_refresh_is_refused(self):
        async def body(app, pilot):
            await self.open_box(app, pilot)
            self.reg_status("blocked")     # the board's snapshot still says idle
            await self.send_text(app, pilot, "yes")
            await settle(pilot, lambda: "is at a prompt; answer it in its tab" in self.problem(app))
        self.drive_needs(body)
        self.assertEqual(self.rig.calls(), [])

    def test_typed_but_not_submitted_clears_the_box_and_says_so(self):
        path = os.path.join(self.paths["sessions"], "%s.json" % self.s)
        with open(path) as fh:
            row = json.load(fh)
        self.rig.after_first_call({path: json.dumps(dict(row, status="waiting"))})

        async def body(app, pilot):
            await self.open_box(app, pilot)
            await self.send_text(app, pilot, "yes")
            await settle(pilot, lambda: "typed but not submitted" in self.problem(app))
            self.assertIn("check its tab", self.problem(app))
            self.assertEqual(app.screen.query_one("#message", Input).value, "")
            self.assertFalse(app.screen.sending)
        self.drive_needs(body)
        self.assertEqual(len(self.rig.calls()), 1)

    def test_a_pasted_line_break_is_refused_not_trimmed(self):
        async def body(app, pilot):
            await self.open_box(app, pilot)
            box = app.screen.query_one("#message", Input)
            await pilot.press(*"typed")
            box.post_message(events.Paste("  \n"))
            await settle(pilot, lambda: "one line only" in self.problem(app))
            self.assertEqual(box.value, "typed")
            box.post_message(events.Paste(" one clean line"))
            await settle(pilot, lambda: box.value == "typed one clean line", "the clean paste")
        self.drive_needs(body)

    def test_esc_and_a_second_enter_wait_while_a_send_is_in_flight(self):
        release, calls = threading.Event(), []

        def slow(name, pid, sid_, tty, text, paths):
            calls.append(text)
            release.wait(10)
            return True, ""

        async def body(app, pilot):
            await self.open_box(app, pilot)
            await self.send_text(app, pilot, "once")
            await settle(pilot, lambda: calls and "sending" in self.problem(app), "the send to start")
            await pilot.press("escape")
            await pilot.press("enter")
            await pilot.pause(0.2)   # asserts a non-event: nothing signals that the box ignored the keys
            self.assertEqual(self.screen_name(app), "SendScreen")
            self.assertEqual(calls, ["once"])
            release.set()
            await settle(pilot, lambda: "sent to Live One" in (app.status_text or ""), "the footer")
        with mock.patch.object(send, "deliver", slow):
            self.drive_needs(body)

    def test_an_error_while_sending_unlocks_the_box_without_quoting_the_text(self):
        def broken(*args):
            raise RuntimeError("osascript - /dev/ttys099 the secret plan")

        async def body(app, pilot):
            await self.open_box(app, pilot)
            await self.send_text(app, pilot, "the secret plan")
            await settle(pilot, lambda: "could not send" in self.problem(app))
            self.assertNotIn("secret", self.problem(app))
            self.assertFalse(app.screen.sending)
            await pilot.press("escape")
            await settle(pilot, lambda: self.screen_name(app) != "SendScreen", "Esc to close the box")
        with mock.patch.object(send, "deliver", broken):
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
        self.status_file("live.json", "Live One", s)

        async def body(app, pilot):
            await pilot.press("t")
            await settle(pilot, lambda: self.screen_name(app) == "ReopenScreen", "the reopen offer")
        self.drive_needs(body)
        self.assertEqual(self.rig.calls(), [])

    def test_t_on_a_session_outside_iterm_says_so(self):
        async def body(app, pilot):
            app.snapshot.jump["iterm"] = set()
            await pilot.press("t")
            await settle(pilot, lambda: "not in an iTerm2 tab" in (app.status_text or ""))
            self.assertNotEqual(self.screen_name(app), "SendScreen")
        self.drive_needs(body)
        self.assertEqual(self.rig.calls(), [])

    def test_help_and_binding_list_the_key(self):
        self.assertTrue(any(keys == "t" for keys, _ in fapp.HELP_KEYS))
        self.assertTrue(any(b.key == "t" and b.action == "tell" for b in fapp.FleetwatchApp.BINDINGS))

    # Same-name sessions, pickers, helpers and the Resume tab.

    def add_second(self, name, cwd_name, what="send the form"):
        """A second live session on the test runner's parent process, in its own iTerm2 tab."""
        other = sid()
        folder = self.mkdir(cwd_name)
        self.reg(name, other, pid=os.getppid(), cwd=folder)
        self.status_file("second.json", name, other, what)
        self.extra_ttys[os.getppid()] = self.TTY2
        return other, folder

    def aim_rig_at(self, pid, tty):
        self.rig.pid, self.rig.tty_path = pid, tty
        self.rig.foreground(claude_rows(pid), tty=tty)

    SHARED = "two running sessions are named Live One; rename one first"

    async def refused_on_every_row(self, app, pilot, pick, expect):
        """Put the real cursor on every row pick() accepts, by key and then with the arrow keys, press t
        and expect the refusal in the footer. Nothing mocks the cursor."""
        keys = [k for k in app.current._order if pick(app.current._by_key[k])]
        self.assertTrue(keys, "no row to try")

        async def press_t(where):
            app.status_text = ""
            await pilot.press("t")
            await settle(pilot, lambda: expect in (app.status_text or ""), "the refusal on " + where)
            self.assertNotEqual(self.screen_name(app), "SendScreen")
        for key in keys:
            app.current.move_to(key)
            await press_t(key)
        app.current.move_to(app.current._order[0])
        tried = 0
        for _ in range(len(app.current._order)):
            node = app.current.current()
            if node is not None and pick(node):
                await press_t("the arrow keys at " + node.key)
                tried += 1
            await pilot.press("down")
        self.assertGreater(tried, 0)
        return keys

    def test_same_name_sessions_are_refused_on_the_sessions_tab(self):
        self.status_file("live.json", "Live One", self.s, "first item")
        self.add_second("Live One", "elsewhere", "second item")
        self.aim_rig_at(os.getppid(), self.TTY2)

        async def body(app, pilot):
            await self.refused_on_every_row(app, pilot, lambda n: n.session == "Live One", self.SHARED)
        self.drive_tab("3", body)
        self.assertEqual(self.rig.calls(), [])

    def test_same_name_sessions_are_refused_on_the_needs_tab_rows_and_tasks(self):
        self.status_file("live.json", "Live One", self.s, "first item")
        self.add_second("Live One", "elsewhere", "second item")
        self.aim_rig_at(os.getppid(), self.TTY2)

        async def body(app, pilot):
            app.current.set_all(True)
            keys = await self.refused_on_every_row(app, pilot, lambda n: n.session == "Live One", self.SHARED)
            self.assertTrue(any(":t:" in k for k in keys), keys)
        self.drive_needs(body)
        self.assertEqual(self.rig.calls(), [])

    def test_a_shared_name_is_refused_even_with_the_session_id(self):
        other, _ = self.add_second("Live One", "elsewhere")

        async def body(app, pilot):
            for sid_ in (None, self.s, other):
                tty, reason, _n, _r = app.snapshot.target_record("Live One", sid=sid_, unique=True)
                self.assertIsNone(tty)
                self.assertEqual(reason, self.SHARED)
            self.assertIsNotNone(app.snapshot.target_record("Live One")[0], "the jump is unchanged")
        self.drive_needs(body)

    def test_a_name_alone_that_finds_a_session_with_an_id_is_refused(self):
        async def body(app, pilot):
            tty, reason, _n, _r = app.snapshot.target_record("Live One", unique=True)
            self.assertIsNone(tty)
            self.assertIn("cannot tell which session Live One is", reason)
            self.assertEqual(app.snapshot.target_record("Live One", sid=self.s, unique=True)[0], TTY)
        self.drive_needs(body)

    def test_an_item_two_same_name_sessions_share_is_refused_after_the_pick(self):
        self.add_second("Live One", "elsewhere")
        self.aim_rig_at(os.getppid(), self.TTY2)

        async def body(app, pilot):
            await pilot.press("2")
            await settle(pilot, lambda: app.active == 1, "the to-dos tab")
            item = next(n for n in all_nodes(app.current.nodes) if n.todo and len(n.owners) == 2)
            app.current.move_to(item.key)
            await pilot.press("t")
            await settle(pilot, lambda: self.screen_name(app) == "SessionPicker", "the picker")
            self.assertTrue(any("elsewhere" in x for x in app.screen.names), app.screen.names)
            await pilot.press("enter")
            await settle(pilot, lambda: self.SHARED in (app.status_text or ""), "the refusal")
            self.assertNotEqual(self.screen_name(app), "SendScreen")
        self.drive_needs(body)
        self.assertEqual(self.rig.calls(), [])

    def test_an_item_one_of_two_same_name_sessions_holds_is_refused(self):
        self.status_file("live.json", "Live One", self.s, "only the first has this")
        self.add_second("Live One", "elsewhere", "only the second has this")

        async def body(app, pilot):
            await pilot.press("2")
            await settle(pilot, lambda: app.active == 1, "the to-dos tab")
            keys = [n.key for n in all_nodes(app.current.nodes) if n.todo and len(n.owners) == 1]
            self.assertEqual(len(keys), 2)
            for key in keys:
                app.status_text = ""
                app.current.move_to(key)
                await pilot.press("t")
                await settle(pilot, lambda: self.SHARED in (app.status_text or ""), "the refusal")
        self.drive_needs(body)
        self.assertEqual(self.rig.calls(), [])

    def test_an_item_two_sessions_share_asks_which_one(self):
        self.add_second("Other One", "elsewhere")
        self.aim_rig_at(os.getppid(), self.TTY2)

        async def body(app, pilot):
            await pilot.press("2")
            await settle(pilot, lambda: app.active == 1, "the to-dos tab")
            item = next(n for n in all_nodes(app.current.nodes) if n.todo and len(n.owners) == 2)
            app.current.move_to(item.key)
            await pilot.press("t")
            await settle(pilot, lambda: self.screen_name(app) == "SessionPicker", "the picker")
            self.assertEqual(sorted(app.screen.names), ["Live One", "Other One"])
            await pilot.press(*(["down"] if app.screen.names[1] == "Other One" else []), "enter")
            await settle(pilot, lambda: self.screen_name(app) == "SendScreen", "the send box")
            self.assertIn("Tell Other One", self.box_texts(app)[0])
            await self.send_text(app, pilot, "picked")
            await settle(pilot, lambda: "sent to Other One" in (app.status_text or ""), "the footer")
        self.drive_needs(body)
        self.assertEqual(self.rig.calls()[0]["argv"], ["-", self.TTY2, "picked"])

    def add_helper(self, own_tab):
        h = sid()
        self.reg("wt-send-helper", h, pid=standin.CHILD_PID, cwd=self.folder)
        if own_tab:
            self.extra_ttys[standin.CHILD_PID] = self.TTY3
        return h

    async def tell_helper(self, app, pilot):
        app.current.set_open("ses:Live One", True)
        await settle(pilot, lambda: "ses:Live One:k:wt-send-helper" in app.current._order, "the helper row")
        app.current.move_to("ses:Live One:k:wt-send-helper")
        await self.open_box(app, pilot)

    def test_a_helper_row_tells_the_helper_in_its_own_tab(self):
        self.add_helper(own_tab=True)
        self.aim_rig_at(standin.CHILD_PID, self.TTY3)

        async def body(app, pilot):
            await self.tell_helper(app, pilot)
            self.assertIn("Tell wt-send-helper", self.box_texts(app)[0])
            await self.send_text(app, pilot, "helper")
            await settle(pilot, lambda: "sent to wt-send-helper" in (app.status_text or ""), "the footer")
        self.drive_tab("3", body)
        self.assertEqual(self.rig.calls()[0]["argv"], ["-", self.TTY3, "helper"])

    def test_a_helper_without_a_tab_falls_back_to_its_parent(self):
        self.add_helper(own_tab=False)

        async def body(app, pilot):
            await self.tell_helper(app, pilot)
            self.assertIn("Tell Live One", self.box_texts(app)[0])
            await self.send_text(app, pilot, "parent")
            await settle(pilot, lambda: "sent to Live One" in (app.status_text or ""), "the footer")
        self.drive_tab("3", body)
        self.assertEqual(self.rig.calls()[0]["argv"], ["-", TTY, "parent"])

    def test_a_helper_name_two_live_helpers_share_is_refused_and_never_falls_back(self):
        self.add_helper(own_tab=True)
        self.reg("wt-send-helper", sid(), pid=os.getppid(), cwd=self.folder)   # an unattached one
        shared = "two running helpers are named wt-send-helper; rename one first"

        async def body(app, pilot):
            for session in (None, "Live One"):
                tty, reason, _n, _r = app.snapshot.target_record(session, "wt-send-helper", self.s, unique=True)
                self.assertIsNone(tty)
                self.assertEqual(reason, shared)
            self.assertEqual(app.snapshot.target_record(None, "wt-send-helper")[0], self.TTY3, "the jump is unchanged")
            app.current.set_open("ses:Live One", True)
            await settle(pilot, lambda: "ses:Live One:k:wt-send-helper" in app.current._order, "the helper row")
            app.current.move_to("ses:Live One:k:wt-send-helper")
            await pilot.press("t")
            await settle(pilot, lambda: shared in (app.status_text or ""), "the refusal")
            self.assertNotEqual(self.screen_name(app), "SendScreen")
        self.drive_tab("3", body)
        self.assertEqual(self.rig.calls(), [])

    def test_a_helper_that_is_not_known_does_not_fall_back_to_its_parent(self):
        async def body(app, pilot):
            tty, reason, _n, _r = app.snapshot.target_record("Live One", "wt-gone", self.s, unique=True)
            self.assertIsNone(tty)
            self.assertEqual(reason, "wt-gone is not a live session")
            self.assertEqual(app.snapshot.target_record("Live One", "wt-gone")[0], TTY, "the jump is unchanged")
        self.drive_needs(body)

    def test_a_stale_row_is_refused_as_not_running_even_when_a_live_helper_has_its_name(self):
        self.add_helper(own_tab=True)
        self.aim_rig_at(standin.CHILD_PID, self.TTY3)
        with open(os.path.join(self.paths["status"], "stale.json"), "w") as fh:
            json.dump({"session": "wt-send-helper", "sessionId": sid(), "updated": standin.iso(-90 * 86400)}, fh)

        async def body(app, pilot):
            app.current.set_open("sesg:stale", True)
            await settle(pilot, lambda: "stale:wt-send-helper" in app.current._order, "the stale row")
            app.current.move_to("stale:wt-send-helper")
            await pilot.press("t")
            await settle(pilot, lambda: "wt-send-helper is not running" in (app.status_text or ""), "the refusal")
            self.assertNotEqual(self.screen_name(app), "SendScreen")
        self.drive_tab("3", body)
        self.assertEqual(self.rig.calls(), [])

    def test_a_running_row_on_the_resume_tab_tells_that_session_by_its_id(self):
        os.remove(os.path.join(self.paths["status"], "live.json"))
        os.remove(os.path.join(self.paths["sessions"], "%s.json" % self.s))
        s, _ = self.transcript("Live Parked")
        self.reg("Live Parked", s, pid=standin.PARENT_PID, cwd=self.folder)
        park.park(s, "Live Parked", "note")

        async def body(app, pilot):
            await settle(pilot, lambda: app.current.current() is not None and app.current.current().resume == s,
                         "the parked row")
            await self.open_box(app, pilot)
            self.assertIn("Tell Live Parked", self.box_texts(app)[0])
            await self.send_text(app, pilot, "from resume")
            await settle(pilot, lambda: "sent to Live Parked" in (app.status_text or ""), "the footer")
        self.drive_tab("4", body)
        self.assertEqual(self.rig.calls()[0]["argv"], ["-", TTY, "from resume"])

    def test_a_resume_row_whose_name_two_running_sessions_share_is_refused(self):
        os.remove(os.path.join(self.paths["status"], "live.json"))
        os.remove(os.path.join(self.paths["sessions"], "%s.json" % self.s))
        s, _ = self.transcript("Live Parked")
        self.reg("Live Parked", s, pid=standin.PARENT_PID, cwd=self.folder)
        park.park(s, "Live Parked", "note")
        self.add_second("Live Parked", "elsewhere")

        async def body(app, pilot):
            await settle(pilot, lambda: app.current.current() is not None and app.current.current().resume == s,
                         "the parked row")
            await pilot.press("t")
            await settle(pilot, lambda: "two running sessions are named Live Parked; rename one first"
                         in (app.status_text or ""), "the refusal")
            self.assertNotEqual(self.screen_name(app), "SendScreen")
        self.drive_tab("4", body)
        self.assertEqual(self.rig.calls(), [])


if __name__ == "__main__":
    unittest.main()
