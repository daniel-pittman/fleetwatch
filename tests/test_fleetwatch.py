import argparse
import builtins
import json
import os

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")  # tests never move a real home folder
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import standin  # noqa: E402,F401  (pins the time zone before fleetwatch loads)
from fleetwatch import data, iterm, render_text  # noqa: E402

NOW = time.time()
DEAD_PID = 2 ** 22 + 12345


def ms(offset_s=0):
    return int((NOW + offset_s) * 1000)


class Env:
    def __init__(self, agents=None, agents_cmd=None):
        self.tmp = tempfile.TemporaryDirectory()
        r = self.tmp.name
        self.dirs = {k: os.path.join(r, k) for k in ("sessions", "jobs", "status")}
        for d in self.dirs.values():
            os.makedirs(d)
        self.agents_file = os.path.join(r, "agents.json")
        with open(self.agents_file, "w") as fh:
            json.dump(agents if agents is not None else [], fh)
        cmd = agents_cmd or "%s -c \"import sys;sys.stdout.write(open(sys.argv[1]).read())\" %s" % (
            sys.executable, self.agents_file)
        self.paths = dict(self.dirs, command=cmd)
        self.ppids = {}

    def reg(self, name, sid, pid=None, status="idle", age=0, **extra):
        d = {"name": name, "sessionId": sid, "pid": pid if pid is not None else os.getpid(),
             "kind": "interactive", "cwd": "/x", "status": status,
             "statusUpdatedAt": ms(-age), "updatedAt": ms(-age), "startedAt": ms(-age)}
        d.update(extra)
        with open(os.path.join(self.dirs["sessions"], "%s.json" % sid), "w") as fh:
            json.dump(d, fh)

    def status(self, fname, **d):
        with open(os.path.join(self.dirs["status"], fname), "w") as fh:
            if isinstance(d.get("raw"), str):
                fh.write(d["raw"])
            else:
                json.dump(d, fh)

    def run(self, window=24 * 3600, show_all=False):
        warnings = []
        recs = data.build_records(self.paths, NOW, warnings, ppids=self.ppids)
        sections, stale = data.classify(recs, NOW, window, show_all)
        return sections, stale, warnings

    def names(self, sections, key):
        return [r["name"] for r in sections[key]]


def iso(offset_s):
    return data.datetime.fromtimestamp(NOW + offset_s, data.LOCAL_TZ).isoformat()


class FleetTests(unittest.TestCase):
    def test_join_registry_agents_and_status_file(self):
        e = Env(agents=[{"name": "Alpha", "sessionId": "s1", "kind": "interactive",
                         "pid": os.getpid(), "status": "idle"}])
        e.reg("Alpha", "s1", status="busy", age=30)
        e.status("a.json", session="Alpha", updated=iso(-60), working_on="the thing",
                 questions=["q1", "q2"], remind=["r1"])
        sections, _, warnings = e.run()
        self.assertEqual(e.names(sections, "working"), ["Alpha"])
        self.assertEqual(e.names(sections, "questions"), ["Alpha"])
        self.assertEqual(sections["working"][0]["status"]["working_on"], "the thing")
        self.assertEqual(warnings, [])
        text = render_text.render(sections, [], NOW, False, [])
        self.assertIn("1. q1\n", text + "\n")
        self.assertIn("2. q2\n", text + "\n")
        self.assertIn("r1", text)

    def test_dead_pid_blocked_is_hidden_as_stale(self):
        e = Env(agents=[{"name": "Ghost", "id": "j1", "kind": "background", "sessionId": "g1",
                         "state": "blocked", "startedAt": ms(-90 * 86400)}])
        os.makedirs(os.path.join(e.dirs["jobs"], "j1"))
        with open(os.path.join(e.dirs["jobs"], "j1", "state.json"), "w") as fh:
            json.dump({"state": "blocked", "updatedAt": iso(-90 * 86400)}, fh)
        sections, stale, _ = e.run()
        self.assertEqual(sections["needs"], [])
        self.assertEqual([r["name"] for r in stale], ["Ghost"])
        text = render_text.render(sections, stale, NOW, False, [])
        self.assertIn("1 stale record hidden", text)
        self.assertIn("fleetwatch --all", text)
        s_all, _, _ = e.run(show_all=True)
        self.assertEqual(e.names(s_all, "stale"), ["Ghost"])

    def test_live_blocked_needs_you(self):
        e = Env()
        e.reg("Waiting", "w1", status="waiting", age=300)
        e.reg("Stuck", "w2", status="blocked", age=60)
        sections, _, _ = e.run()
        self.assertEqual(sorted(e.names(sections, "needs")), ["Stuck", "Waiting"])

    def test_window_flag(self):
        e = Env()
        e.reg("Old", "o1", pid=DEAD_PID, status="idle", age=3 * 3600)
        s, stale, _ = e.run(window=data.parse_window("6h"))
        self.assertEqual(e.names(s, "idle"), ["Old"])
        s, stale, _ = e.run(window=data.parse_window("1h"))
        self.assertEqual(s["idle"], [])
        self.assertEqual(len(stale), 1)
        self.assertEqual(data.parse_window("3d"), 3 * 86400)
        self.assertEqual(data.parse_window("90m"), 5400)

    def test_busy_with_dead_pid_needs_recent_activity(self):
        e = Env()
        e.reg("Recent", "r1", pid=DEAD_PID, status="busy", age=5 * 60)
        e.reg("Old", "r2", pid=DEAD_PID, status="busy", age=3600)
        s, _, _ = e.run()
        self.assertEqual(e.names(s, "working"), ["Recent"])
        self.assertNotIn("Old", e.names(s, "working"))

    def test_gone_quiet_arithmetic(self):
        e = Env()
        e.reg("Edge", "q1")
        e.reg("Late", "q2")
        e.reg("NoCad", "q3")
        e.status("1.json", session="Edge", updated=iso(-14 * 60), cadence_min=10)   # 1.4x: ok
        e.status("2.json", session="Late", updated=iso(-16 * 60), cadence_min=10)   # 1.6x: quiet
        e.status("3.json", session="NoCad", updated=iso(-5 * 3600))
        s, _, _ = e.run()
        self.assertEqual(e.names(s, "quiet"), ["Late"])

    def test_malformed_status_file_is_a_warning(self):
        e = Env()
        e.reg("Fine", "f1")
        e.status("bad.json", raw="{not json")
        e.status("nosession.json", updated=iso(0))
        s, _, warnings = e.run()
        self.assertEqual(len(warnings), 2)
        self.assertTrue(all("does not parse" in w for w in warnings))
        self.assertEqual(e.names(s, "idle"), ["Fine"])

    def test_agents_command_failure_falls_back_to_registry(self):
        for cmd in ("%s -c \"import sys;sys.exit(3)\"" % sys.executable,
                    "%s -c \"print('{}')\"" % sys.executable,
                    "%s -c \"print('[{\\\"x\\\":1}]')\"" % sys.executable,
                    "/nonexistent/command --json"):
            e = Env(agents_cmd=cmd)
            e.reg("Solo", "s1", status="busy")
            s, _, warnings = e.run()
            self.assertEqual(len(warnings), 1, cmd)
            self.assertEqual(e.names(s, "working"), ["Solo"])

    def test_key_files_are_never_opened(self):
        e = Env()
        e.reg("Alpha", "s1")
        with open(os.path.join(e.dirs["sessions"], "s1.abcdef.key"), "w") as fh:
            fh.write("SECRET")
        opened = []
        real_open = builtins.open

        def spy(path, *a, **k):
            opened.append(str(path))
            return real_open(path, *a, **k)

        with mock.patch("builtins.open", spy):
            e.run()
        self.assertTrue(any(p.endswith("s1.json") for p in opened))
        self.assertFalse([p for p in opened if p.endswith(".key")])

    def test_job_record_fields_do_not_leak(self):
        e = Env(agents=[{"name": "Job", "id": "j9", "kind": "background", "sessionId": "j9s",
                         "state": "busy"}])
        os.makedirs(os.path.join(e.dirs["jobs"], "j9"))
        with open(os.path.join(e.dirs["jobs"], "j9", "state.json"), "w") as fh:
            json.dump({"state": "busy", "updatedAt": iso(-60), "providerEnv": "LEAKME_ENV",
                       "tokens": "LEAKME_TOK", "detail": "LEAKME_DETAIL", "intent": "LEAKME_INT"}, fh)
        s, stale, w = e.run()
        self.assertEqual(e.names(s, "working"), ["Job"])
        blob = render_text.render(s, stale, NOW, False, w) + json.dumps(render_text.to_json(s, stale, NOW, 1, w))
        self.assertNotIn("LEAKME", blob)
        rec = s["working"][0]
        self.assertNotIn("providerEnv", rec)

    def test_terminal_escapes_are_stripped(self):
        evil = "\x1b[2J\x1b]0;x\x07"
        e = Env(agents=[{"name": "Bad" + evil, "id": "j7", "kind": "background",
                         "sessionId": "b7", "state": "blocked" + evil}])
        e.reg("Alpha" + evil, "s1", status="busy")
        e.status("a.json", session="Alpha" + evil, updated=iso(-60), persona="P" + evil,
                 working_on="w" + evil, questions=["q" + evil], remind=["r" + evil],
                 cadence_min=1)
        e.status("bad.json", raw="{" + evil)
        s, stale, w = e.run(show_all=True)
        text = render_text.render(s, stale, NOW, False, w)
        for bad in ("\x1b", "\x07", "\x9b"):
            self.assertNotIn(bad, text)
        self.assertNotIn("\x1b[2J", text)
        self.assertNotIn("\x1b]0;x\x07", text)
        self.assertIn("Alpha", text)

    def test_json_and_color(self):
        e = Env()
        e.reg("Alpha", "s1", status="busy")
        s, stale, w = e.run()
        data = render_text.to_json(s, stale, NOW, 86400, w)
        self.assertEqual(data["counts"]["working"], 1)
        self.assertNotIn("\033", render_text.render(s, stale, NOW, False, w))
        self.assertIn("\033", render_text.render(s, stale, NOW, True, w))
        for line in render_text.render(s, stale, NOW, False, w).splitlines():
            self.assertLessEqual(len(line), 100)


def render_env(e, width=100, utf8=True, show_all=False, color=False, **kw):
    s, stale, w = e.run(show_all=show_all)
    return render_text.render(s, stale, NOW, color, w, width=width, utf8=utf8, show_all=show_all, **kw)


LONG_Q = ("Should the coordinator see the nominee list before or after the faculty member confirms "
          "the ranked program preferences for the section")


class ViewTests(unittest.TestCase):
    def test_session_appears_once(self):
        e = Env()
        e.reg("Alpha", "s1", status="blocked", age=400)
        e.status("a.json", session="Alpha", updated=iso(-60), working_on="WORKINGON",
                 questions=["q1"])
        e.reg("Beta", "s2", status="busy")
        e.status("b.json", session="Beta", updated=iso(-60), working_on="BETAWORK")
        text = render_env(e)
        self.assertEqual(text.count("Alpha"), 1)
        self.assertEqual(text.count("Beta"), 1)
        self.assertNotIn("WORKINGON", text)
        self.assertIn("stuck on a prompt for 7 min", text)

    def test_order_stuck_then_questions_then_oldest(self):
        e = Env()
        e.reg("ManyQ", "s1")
        e.status("1.json", session="ManyQ", updated=iso(-60), questions=["a", "b", "c"])
        e.reg("OneOld", "s2")
        e.status("2.json", session="OneOld", updated=iso(-3600), questions=["a"])
        e.reg("OneNew", "s3")
        e.status("3.json", session="OneNew", updated=iso(-60), questions=["a"])
        e.reg("Stuck", "s4", status="waiting", age=100)
        e.status("4.json", session="Stuck", updated=iso(-60), questions=[])
        e.reg("RemindOnly", "s5")
        e.status("5.json", session="RemindOnly", updated=iso(-60), remind=["x"])
        text = render_env(e)
        order = [text.index(n) for n in ("Stuck", "ManyQ", "OneOld", "OneNew", "RemindOnly")]
        self.assertEqual(order, sorted(order))

    def test_question_wrapping_hanging_indent(self):
        for width in (60, 100):
            e = Env()
            e.reg("Alpha", "s1")
            e.status("a.json", session="Alpha", updated=iso(-60), questions=[LONG_Q, "short one"])
            lines = render_env(e, width=width).splitlines()
            self.assertTrue(all(len(ln) <= width for ln in lines), width)
            qi = [i for i, ln in enumerate(lines) if "Should the coordinator" in ln][0]
            self.assertTrue(lines[qi].startswith("   1. "))
            cont = lines[qi + 1]
            self.assertTrue(cont.startswith(" " * 6) and cont.strip(), (width, cont))
            numbered = [ln for ln in lines if ln.lstrip()[:2] in ("1.", "2.")]
            self.assertEqual(len(numbered), 2)
            self.assertTrue(lines[qi + 2].startswith("   2. short one") if width == 100
                            else any(ln.startswith("   2. short one") for ln in lines))

    def test_reminder_markers(self):
        e = Env()
        e.reg("Alpha", "s1")
        e.status("a.json", session="Alpha", updated=iso(-60), remind=["call ITS about the key"])
        self.assertIn("NO DATE   Alpha  ✎ call ITS", render_env(e, utf8=True))
        self.assertIn("Needs a date: call ITS about the key", render_env(e, utf8=True))
        ascii_text = render_env(e, utf8=False)
        self.assertIn("NO DATE   Alpha  + call ITS", ascii_text)
        ascii_text.encode("ascii")

    def test_helper_detection(self):
        e = Env()
        for i, name in enumerate(["wt-t30-pa-d2", "run-68145-0c00b5a6b832-28", "t30-pa",
                                  "ff61" + "0" * 28 + "-90", "Real Name", "billing-api-aa"]):
            e.reg(name, "s%d" % i)
        e.status("r.json", session="wt-has-status", updated=iso(-60))
        e.reg("wt-has-status", "sx")
        s, stale, w = e.run()
        recs = {r["name"]: r for sec in ("idle",) for r in s[sec]}
        gen = {n: data.looks_generated(r) for n, r in recs.items()}
        self.assertEqual([n for n, g in gen.items() if g].sort(), None)
        self.assertEqual(sorted(n for n, g in gen.items() if g),
                         sorted(["wt-t30-pa-d2", "run-68145-0c00b5a6b832-28", "t30-pa",
                                 "ff61" + "0" * 28 + "-90"]))
        text = render_text.render(s, stale, NOW, False, w)
        self.assertIn("+4 unattached helper sessions", text)
        self.assertNotIn("wt-t30-pa-d2", text)
        self.assertIn("wt-t30-pa-d2", render_text.render(s, stale, NOW, False, w, show_all=True))

    def test_narrow_terminal_drops_working_on(self):
        e = Env()
        e.reg("Alpha", "s1", status="busy")
        e.status("a.json", session="Alpha", updated=iso(-60), working_on="the long thing")
        self.assertIn("the long thing", render_env(e, width=100))
        narrow = render_env(e, width=70)
        self.assertNotIn("the long thing", narrow)
        self.assertIn("Alpha", narrow)

    def test_per_question_since(self):
        e = Env()
        e.reg("Alpha", "s1")
        e.status("a.json", session="Alpha", updated=iso(-60),
                 questions=[{"q": "dated one", "since": iso(-2 * 3600)}, "plain one",
                            {"q": "bad stamp", "since": "nonsense"}, {"q": "  "}])
        text = render_env(e)
        self.assertIn("1. dated one · 2.0h", text)
        self.assertIn("2. plain one\n", text + "\n")
        self.assertIn("3. bad stamp\n", text + "\n")
        self.assertNotIn("4.", text)


    def test_stale_hidden_footer_and_all(self):
        e = Env()
        e.reg("Old", "o1", pid=DEAD_PID, age=3 * 86400)
        text = render_env(e)
        self.assertIn("1 stale record hidden", text)
        self.assertIn("Old", render_env(e, show_all=True))


class RollupTests(unittest.TestCase):
    def setUp(self):
        self.P, self.Q, self.G = os.getpid(), os.getppid(), 1
        if len({self.P, self.Q, self.G}) < 3:
            self.skipTest("need three distinct live pids")

    def env(self):
        e = Env()
        e.ppids = {self.G: self.Q, self.Q: self.P}
        e.reg("Parent", "p", pid=self.P)
        e.reg("wt-child", "c", pid=self.Q, status="busy")
        e.reg("run-grand-ab", "g", pid=self.G, status="busy")
        return e

    def test_child_and_grandchild_roll_up(self):
        e = self.env()
        s, stale, w = e.run()
        by = {r["name"]: r for r in s["idle"] + s["working"]}
        self.assertIs(by["wt-child"]["parent"], by["Parent"])
        self.assertIs(by["run-grand-ab"]["parent"], by["Parent"])
        text = render_text.render(s, stale, NOW, False, w)
        self.assertNotIn("wt-child", text)
        self.assertNotIn("run-grand-ab", text)
        self.assertIn("1 working", text)
        s_all = render_text.render(s, stale, NOW, False, w, show_all=True)
        self.assertIn("↳ wt-child", s_all)
        self.assertIn("↳ run-grand-ab", s_all)

    def test_busy_children_make_parent_working_and_show_count(self):
        e = self.env()
        e.status("p.json", session="Parent", updated=iso(-60), questions=["q"])
        text = render_env(e)
        self.assertIn("working · 2 helpers · updated", text)

    def test_stuck_child_puts_parent_in_for_you(self):
        e = self.env()
        e.reg("wt-child", "c", pid=self.Q, status="blocked", age=360)
        text = render_env(e)
        self.assertIn("FOR YOU", text)
        self.assertIn("a helper is stuck on a prompt: wt-child for 6 min", text)
        self.assertNotIn("stuck on a prompt for", text.split("a helper is")[0])

    def test_child_status_file_adds_tagged_items(self):
        e = self.env()
        e.status("c.json", session="wt-child", updated=iso(-60), questions=["child q"], remind=["child r"])
        text = render_env(e)
        self.assertIn("1. child q [wt-child]", text)
        self.assertIn("NO DATE   Parent  ✎ child r", text)
        self.assertEqual(len([ln for ln in text.splitlines() if ln.startswith(" Parent")]), 1)

    def test_child_upcoming_rolls_up_and_dedups(self):
        e = self.env()
        e.status("p.json", session="Parent", updated=iso(-60),
                 upcoming=[{"what": "Go live", "due": dstr(5), "kind": "deploy"}])
        e.status("c.json", session="wt-child", updated=iso(-60),
                 upcoming=[{"what": "go live", "due": dstr(5)}, {"what": "Child only", "due": dstr(6)}])
        text = render_env(e, width=120)
        self.assertEqual(text.count("Go live"), 1)
        self.assertIn("Child only", text)
        self.assertNotIn("wt-child", text.split("COMING UP")[1])
        self.assertTrue([ln for ln in text.splitlines() if "Child only" in ln][0].endswith("Parent"))

    def test_orphan_is_counted_not_listed(self):
        e = Env()
        e.reg("wt-orphan", "o", pid=self.P)
        e.reg("Solo", "s", pid=self.Q)
        text = render_env(e)
        self.assertNotIn("wt-orphan", text)
        self.assertIn("+1 unattached helper session ", text + " ")
        self.assertIn("wt-orphan", render_env(e, show_all=True))

    def test_pid_that_died_between_reads(self):
        e = Env()
        e.ppids = {self.Q: DEAD_PID, DEAD_PID: self.P}
        e.reg("Parent", "p", pid=DEAD_PID, status="busy", age=60)
        e.reg("wt-late", "c", pid=self.Q, status="busy")
        s, _, _ = e.run()
        self.assertTrue(all(r["parent"] is None for sec in s.values() for r in sec))
        # a live pid the process table never listed has no ancestry either
        e.ppids = {}
        s, _, _ = e.run()
        self.assertTrue(all(r["parent"] is None for sec in s.values() for r in sec))

    def test_ancestry_cycle_terminates(self):
        e = Env()
        e.ppids = {self.Q: self.G, self.G: self.Q}
        e.reg("A", "a", pid=self.Q)
        e.reg("B", "b", pid=self.G)
        e.run()

    def test_parse_ps(self):
        self.assertEqual(data.parse_ps("  1     0\n 331 39229\nbad line\n x 2\n"), {1: 0, 331: 39229})


class SnapshotAndLiveTests(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.e.reg("Alpha", "s1", status="busy")
        self.e.status("a.json", session="Alpha", updated=iso(-60), questions=["q1"])
        self.env = dict(os.environ, FLEETWATCH_AGENTS_CMD=self.e.paths["command"], FLEETWATCH_PS_CMD="true",
                        FLEETWATCH_SESSIONS_DIR=self.e.dirs["sessions"], FLEETWATCH_JOBS_DIR=self.e.dirs["jobs"],
                        FLEETWATCH_STATUS_DIR=self.e.dirs["status"], COLUMNS="120", PYTHONIOENCODING="utf-8",
                        FLEETWATCH_OSASCRIPT="false", FLEETWATCH_TTY_CMD="true")
        self.root = os.path.join(os.path.dirname(__file__), "..")

    def test_piped_snapshot_has_no_escape_codes(self):
        import subprocess
        out = subprocess.run([sys.executable, "-m", "fleetwatch"], env=self.env, capture_output=True,
                             text=True, timeout=60, stdin=subprocess.DEVNULL, cwd=self.root)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("\x1b", out.stdout)
        self.assertIn("1 question", out.stdout)
        self.assertNotIn("q: quit", out.stdout)






# ---------------------------------------------------------------- iTerm jump, hotkey, NO DATE

def view_for(names_pids, now=NOW):
    """Render inputs for live sessions at the given pids."""
    e = Env()
    for i, (name, pid) in enumerate(names_pids):
        e.reg(name, "id%d" % i, pid=pid, status="busy")
    sections, stale, _ = e.run()
    return e, sections, stale


class JumpTests(unittest.TestCase):
    def test_tty_validation(self):
        for good in ("/dev/ttys0", "/dev/ttys033"):
            self.assertTrue(iterm.valid_tty(good), good)
        for bad in ("ttys033", "/dev/ttys", "/dev/ttys03x", "/dev/tty", "/dev/ttys033\n", "/dev/ttys033; x",
                    '/dev/ttys1" & do shell script "x', "/dev/../ttys1", "", None, 5):
            self.assertFalse(iterm.valid_tty(bad), repr(bad))

    def test_tty_table_parse(self):
        self.assertEqual(iterm.parse_tty_table("12 ttys033\n13 ??\n14 ttys7\nx y\n15 tty"),
                         {12: "/dev/ttys033", 14: "/dev/ttys7"})

    def render(self, ttys, iterm, rows=(("Alpha", None), ("Beta", None)), width=100, **kw):
        e = Env()
        for i, (name, pid) in enumerate(rows):
            e.reg(name, "id%d" % i, pid=pid if pid is not None else os.getpid(), status="idle", age=i)
            e.status("%d.json" % i, session=name, updated=iso(-60), questions=["q"] if name == "Alpha" else [])
        sections, stale, warnings = e.run()
        jump = {"ttys": ttys, "iterm": iterm, "err": None, "letters": {}}
        text = render_text.render(sections, stale, NOW, False, warnings, width=width, jump=jump, **kw)
        return text, jump








    def test_jump_passes_tty_as_argv_only(self):
        calls = []

        def fake(argv, **kw):
            calls.append((argv, kw))
            return mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.dict(os.environ, {"FLEETWATCH_OSASCRIPT": "osascript"}), \
                mock.patch.object(iterm.subprocess, "run", fake):
            self.assertEqual(iterm.jump_to_tty("/dev/ttys033"), (True, ""))
        argv, kw = calls[0]
        self.assertEqual(argv, ["osascript", "-", "/dev/ttys033"])
        self.assertNotIn("ttys", kw["input"])
        self.assertNotIn("033", kw["input"])
        self.assertNotIn("ttys", iterm.ITERM_JUMP_SCRIPT + iterm.ITERM_LIST_SCRIPT)
        self.assertIn("item 1 of argv", kw["input"])

    def test_invalid_tty_never_reaches_osascript(self):
        with mock.patch.object(iterm.subprocess, "run") as run:
            ok, _ = iterm.jump_to_tty('/dev/ttys1" & do shell script "x')
        self.assertFalse(ok)
        run.assert_not_called()

    def test_osascript_failure_is_a_status_line(self):
        err = "43:80: execution error: Not authorized to send Apple events to iTerm2. (-1743)"
        with mock.patch.object(iterm.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="", stderr=err)):
            ok, reason = iterm.jump_to_tty("/dev/ttys1")
        self.assertFalse(ok)
        self.assertIn("Not authorized", reason)

    def test_osascript_missing_or_hung_does_not_raise(self):
        with mock.patch.object(iterm.subprocess, "run", side_effect=FileNotFoundError("osascript")):
            self.assertFalse(iterm.read_iterm()[0] is not None)
            self.assertFalse(iterm.jump_to_tty("/dev/ttys1")[0])
        with mock.patch.object(iterm.subprocess, "run",
                               side_effect=iterm.subprocess.TimeoutExpired("osascript", 10)):
            self.assertIsNone(iterm.read_iterm()[0])



class NoDateTests(unittest.TestCase):
    def lines(self, width):
        e = Env()
        e.reg("Billing API Development", "a", status="idle")
        e.reg("Docs Server Development", "b", status="idle")
        e.status("a.json", session="Billing API Development", updated=iso(-60),
                 remind=["Add ticket to trust mail (#643)"])
        e.status("b.json", session="Docs Server Development", updated=iso(-60),
                 remind=["return contract v2 to legal with review request",
                         "log revision-history appendix when v2 ships"])
        sections, stale, warnings = e.run()
        text = render_text.render(sections, stale, NOW, False, warnings, width=width)
        start = text.index(" NO DATE")
        return text[start:].split("\n")

    ROWS = [
        " NO DATE   Billing API  ✎ Add ticket to trust mail (#643)",
        "           Docs Server  ✎ log revision-history appendix when v2 ships",
        "                        ✎ return contract v2 to legal with review request"]

    def test_layout_at_120(self):
        self.assertEqual(self.lines(120), self.ROWS)

    def test_layout_at_80(self):
        got = self.lines(80)
        self.assertTrue(all(len(ln) <= 80 for ln in got), got)
        self.assertEqual(got, self.ROWS)

    def test_long_item_wraps_under_its_text(self):
        got = self.lines(60)
        self.assertTrue(all(len(ln) <= 60 for ln in got), got)
        self.assertEqual(sum(1 for ln in got if "✎" in ln), 3)
        col = got[1].index("✎")
        cont = [ln for ln in got if "✎" not in ln and "NO DATE" not in ln and not ln.startswith("━")]
        self.assertTrue(cont)
        self.assertTrue(all(ln[:col].strip() == "" for ln in cont), got)


class HotkeyProfileTests(unittest.TestCase):
    PROFILE = os.path.join(os.path.dirname(__file__), "..", "iterm", "fleetwatch.json")

    def test_profile_parses_and_runs_fleet(self):
        with open(self.PROFILE) as fh:
            data = json.load(fh)
        prof = data["Profiles"][0]
        self.assertEqual(prof["Name"], "fleetwatch")
        self.assertRegex(prof["Guid"], r"^[0-9A-F-]{36}$")
        self.assertIn("fleetwatch", prof["Command"])
        self.assertTrue(prof["Custom Command"] in ("Yes", "Custom Shell"))
        self.assertTrue(prof["Has Hotkey"])
        self.assertEqual(prof["HotKey Key Code"], 3)   # F
        self.assertEqual(prof["HotKey Modifier Flags"] & 393216, 393216)   # Control + Shift


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- coming up

def mtn(y, mo, d, h=12, mi=0):
    return data.datetime(y, mo, d, h, mi, tzinfo=data.LOCAL_TZ)


def item(what, due=None, kind="milestone", yours=False, session="S"):
    due_dt, has_time = (data.parse_due(due) if due else (None, False))
    return {"what": what, "due": due_dt, "has_time": has_time, "kind": kind, "yours": yours,
            "session": session}


def bucket_of(due, now_dt, **kw):
    tl = data.build_timeline([item("x", due, **kw)], now_dt.timestamp())
    for k, rows in tl["buckets"].items():
        if rows:
            return k
    return "later" if tl["beyond"] else None


def dstr(days):
    return (data.datetime.fromtimestamp(NOW, data.LOCAL_TZ).date() + timedelta(days=days)).isoformat()


class TimelineTests(unittest.TestCase):
    def test_bucket_boundaries(self):
        now = mtn(2026, 10, 18, 15)
        want = {"2026-10-18": "this_week", "2026-10-19": "this_week", "2026-10-25": "this_week",
                "2026-10-26": "next_2_wk", "2026-11-08": "next_2_wk", "2026-11-09": "later"}
        # day 0, day 1, day 7, day 8, day 21, day 22
        for due, bucket in want.items():
            self.assertEqual(bucket_of(due, now), bucket, due)
        tl = data.build_timeline([item("far", "2026-11-09")], now.timestamp())
        self.assertEqual([r["what"] for r in tl["buckets"]["later"]], [])
        self.assertEqual([r["what"] for r in tl["beyond"]], ["far"])

    def test_bucket_boundaries_across_dst(self):
        fall = mtn(2026, 10, 31, 12)   # clocks go back on Nov 1
        self.assertEqual(bucket_of("2026-11-07", fall), "this_week")
        self.assertEqual(bucket_of("2026-11-08", fall), "next_2_wk")
        self.assertEqual(bucket_of("2026-11-21", fall), "next_2_wk")
        self.assertEqual(bucket_of("2026-11-22", fall), "later")
        spring = mtn(2026, 3, 7, 12)   # clocks go forward on Mar 8
        self.assertEqual(bucket_of("2026-03-14", spring), "this_week")
        self.assertEqual(bucket_of("2026-03-15", spring), "next_2_wk")
        self.assertEqual(bucket_of("2026-03-28", spring), "next_2_wk")
        self.assertEqual(bucket_of("2026-03-29", spring), "later")

    def test_a_date_alone_is_local_and_an_offset_is_converted(self):
        d, t = data.parse_due("2026-10-21")
        self.assertEqual((d.isoformat(), t), ("2026-10-21T00:00:00-06:00", False))
        d, t = data.parse_due("2026-10-22T02:00:00+00:00")   # 8 PM the 21st in Mountain time
        self.assertEqual((d.date().isoformat(), t), ("2026-10-21", True))

    def test_overdue_rules(self):
        now = mtn(2026, 10, 18, 15)
        self.assertEqual(bucket_of("2026-10-16", now, yours=True), "overdue")
        self.assertEqual(bucket_of("2026-10-16", now, kind="deadline"), "overdue")
        tl = data.build_timeline([item("a", "2026-10-16", yours=True),
                                   item("b", "2026-10-16", kind="deadline"),
                                   item("c", "2026-10-17", kind="deploy"),
                                   item("d", "2026-10-16", kind="milestone")], now.timestamp())
        red = {r["what"]: r["red"] for r in tl["buckets"]["overdue"]}
        self.assertEqual(red, {"a": True, "b": True})
        self.assertEqual([r["what"] for r in tl["buckets"]["this_week"]], ["c"])   # recent past, not red
        self.assertFalse(tl["buckets"]["this_week"][0]["red"])
        self.assertIsNone(bucket_of("2026-10-16", now, kind="milestone"))   # over a day old: dropped
        # a time earlier today is past; the same date alone is not
        self.assertEqual(bucket_of("2026-10-18T09:00:00-06:00", now, yours=True), "overdue")
        self.assertEqual(bucket_of("2026-10-18", now, yours=True), "this_week")

    def test_undated_only_when_yours(self):
        now = mtn(2026, 10, 18, 15)
        tl = data.build_timeline([item("mine", yours=True), item("theirs")], now.timestamp())
        self.assertEqual([r["what"] for r in tl["buckets"]["no_date"]], ["mine"])
        self.assertEqual(len(data.timeline_rows(tl)), 1)

    def test_dedup_across_sessions(self):
        now = mtn(2026, 10, 18, 15)
        tl = data.build_timeline([
            item("LA go-live", "2026-10-28", session="BA"),
            item("la  GO-live", "2026-10-28", session="Robin", yours=True),
            item("LA go-live", "2026-10-29", session="Other"),
            item("todo", session="BA", yours=True), item("todo", session="Robin", yours=True)],
            now.timestamp())
        rows = tl["buckets"]["next_2_wk"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["sessions"], ["BA", "Robin"])
        self.assertTrue(rows[0]["yours"])
        self.assertEqual(tl["buckets"]["no_date"][0]["sessions"], ["BA", "Robin"])

    def test_malformed_items_warn_and_skip(self):
        w = []
        sf = {"upcoming": ["str", {"due": "2026-10-21"}, {"what": "bad date", "due": "next week"},
                           {"what": "bad yours", "yours": "yes"}, {"what": "ok", "due": "2026-10-21"},
                           {"what": "unknown kind", "kind": "party"}]}
        got = data.session_upcoming(sf, "Alpha", w)
        self.assertEqual([g["what"] for g in got], ["ok", "unknown kind"])
        self.assertEqual(got[1]["kind"], "milestone")
        self.assertEqual(len(w), 4)
        self.assertTrue(all("Alpha" in x for x in w))
        w = []
        self.assertEqual(data.session_upcoming({"upcoming": "oops"}, "Beta", w), [])
        self.assertEqual(len(w), 1)

    def test_remind_maps_to_yours_todo_without_date(self):
        got = data.session_upcoming({"remind": ["#643 ITS", {"q": "second", "since": None}]}, "A", [])
        self.assertEqual([(g["what"], g["kind"], g["yours"], g["due"]) for g in got],
                         [("#643 ITS", "todo", True, None), ("second", "todo", True, None)])

    def test_clean_on_what(self):
        evil = "\x1b[2J\x1b]0;x\x07"
        got = data.session_upcoming({"upcoming": [{"what": "go" + evil, "due": "2026-10-21"}]}, "A", [])
        self.assertNotIn("\x1b", got[0]["what"])
        self.assertNotIn("\x07", got[0]["what"])


def up_env(items, name="Alpha", **extra):
    e = Env()
    e.reg(name, "s1")
    e.status("a.json", session=name, updated=iso(-60), upcoming=items, **extra)
    return e


class ComingUpScreenTests(unittest.TestCase):
    def items(self):
        return [
            {"what": "Symposium registration", "due": dstr(-2), "kind": "deadline", "yours": True},
            {"what": "Past milestone", "due": dstr(-3), "kind": "milestone"},
            {"what": "Feedback window opens", "due": dstr(3), "kind": "milestone"},
            {"what": "LA go-live (v0.3.0 to production)", "due": dstr(10), "kind": "deploy"},
            {"what": "Summer Learning Series proposal", "due": dstr(50), "kind": "deadline", "yours": True},
            {"what": "Add ticket to trust our mail", "kind": "todo", "yours": True},
        ]

    def test_sections_between_for_you_and_nothing(self):
        e = up_env(self.items(), name="Billing API Development", questions=["q"])
        e.reg("Other", "s2")
        text = render_env(e, width=120)
        for needle in ("FOR YOU", "COMING UP", "NOTHING FOR YOU"):
            self.assertIn(needle, text)
        self.assertLess(text.index("FOR YOU"), text.index("COMING UP"))
        self.assertLess(text.index("COMING UP"), text.index("NOTHING FOR YOU"))
        self.assertIn("OVERDUE", text)
        self.assertIn("THIS WEEK", text)
        self.assertIn("NEXT 2 WK", text)
        self.assertIn("1 more, first ", text)
        self.assertIn("fleetwatch --horizon 90d", text)
        self.assertIn("NO DATE   Billing API  ✎ Add ticket to trust our mail", text)
        self.assertNotIn("Past milestone", text)
        self.assertNotIn("Development)", text)
        self.assertTrue(all(len(ln) <= 120 for ln in text.splitlines()))
        ascii_text = render_env(e, width=120, utf8=False)
        ascii_text.encode("ascii")

    def test_rows_have_marker_date_and_right_aligned_session(self):
        e = up_env(self.items(), name="Billing API Development")
        lines = render_env(e, width=120).splitlines()
        row = [ln for ln in lines if "Feedback window opens" in ln][0]
        self.assertIn("◆", row)
        self.assertTrue(row.endswith("Billing API"))
        self.assertEqual(len(row), 120 - 1)
        dead = [ln for ln in lines if "LA go-live" in ln][0]
        self.assertIn("▲", dead)
        self.assertIn("✎", [ln for ln in lines if "Symposium" in ln][-1])   # yours wins over kind
        e = up_env([{"what": "Dead", "due": dstr(3), "kind": "deadline"}])
        self.assertIn("⏰", render_env(e))

    def test_today_tomorrow_and_time(self):
        e = up_env([{"what": "A", "due": dstr(0)}, {"what": "B", "due": dstr(1)},
                    {"what": "C", "due": dstr(0) + "T15:00:00-06:00", "yours": True}])
        text = render_env(e)
        self.assertRegex(text, r"today\s+\S\s+A")
        self.assertRegex(text, r"tomorrow\s+\S\s+B")
        self.assertRegex(text, r"(today|Oct \d+|[A-Z][a-z]{2} \d+) \d+:\d\d [AP]M")

    def test_overdue_colour_only_for_yours_or_deadline(self):
        e = up_env([{"what": "Mine", "due": dstr(-2), "kind": "milestone", "yours": True},
                    {"what": "Dead", "due": dstr(-2), "kind": "deadline"},
                    {"what": "Recent", "due": dstr(-1), "kind": "deploy"}])
        lines = render_env(e, color=True).splitlines()
        def red(w):
            line = [ln for ln in lines if w in ln][-1]
            return any("\033[1;31m" in seg and w in seg for seg in line.split("\033[0m"))
        self.assertTrue(red("Mine"))
        self.assertTrue(red("Dead"))
        self.assertFalse(red("Recent"))

    def test_header_counts(self):
        e = up_env(self.items())
        text = render_env(e)
        self.assertIn("1 coming up this week (0 yours)", text)
        self.assertIn("1 overdue", text)
        colored = render_env(e, color=True)
        self.assertIn("\033[1;31m1\033[0m \033[1;31moverdue\033[0m", colored)
        quiet = render_env(up_env([{"what": "x", "due": dstr(30)}]))
        self.assertNotIn("overdue", quiet)
        self.assertNotIn("coming up this week", quiet)

    def test_reminder_only_session_is_on_for_you_as_needs_a_date(self):
        e = Env()
        e.reg("Alpha", "s1")
        e.status("a.json", session="Alpha", updated=iso(-60), remind=["call ITS"])
        text = render_env(e)
        self.assertIn("━━━ FOR YOU", text)
        self.assertIn("Needs a date: call ITS", text)
        self.assertIn("1 needs a date", text)
        self.assertNotIn("nothing for you", text)

    def test_horizon_widens_rows(self):
        e = up_env(self.items())
        s, stale, w = e.run()
        base = render_text.render(s, stale, NOW, False, w, width=120)
        wide = render_text.render(s, stale, NOW, False, w, width=120, horizon_days=90)
        self.assertIn("1 more, first", base)
        self.assertNotIn("1 more", wide)
        self.assertRegex(wide, r"LATER\s+\S+ \d+\s+\S\s+Summer Learning Series proposal")
        self.assertEqual(data.parse_horizon("30d"), 30)
        self.assertEqual(data.parse_horizon("7d"), 21)
        with self.assertRaises(argparse.ArgumentTypeError):
            data.parse_horizon("soon")

    def test_horizon_beyond_line(self):
        e = up_env([{"what": "Far away", "due": dstr(100)}, {"what": "Mid", "due": dstr(40)}])
        s, stale, w = e.run()
        text = render_text.render(s, stale, NOW, False, w, width=120, horizon_days=30)
        self.assertIn("2 more beyond 30d, first ", text)
        self.assertIn("fleetwatch --horizon 90d", text)
        text = render_text.render(s, stale, NOW, False, w, width=120, horizon_days=90)
        self.assertIn("1 more beyond 90d", text)
        self.assertNotIn("fleetwatch --horizon 90d", text)

    def test_narrow_terminal(self):
        e = up_env(self.items(), name="A Very Long Session Name Development", questions=["q"])
        for width in (40, 60, 79):
            text = render_env(e, width=width)
            lines = text.splitlines()
            self.assertTrue(all(len(ln) <= width for ln in lines), (width, [ln for ln in lines if len(ln) > width]))
            self.assertIn("THIS WEEK", text)
            self.assertIn("OVERDUE", text)


    def test_json_upcoming(self):
        e = up_env(self.items())
        s, stale, w = e.run()
        data = render_text.to_json(s, stale, NOW, 86400, w)
        got = {r["what"]: r for r in data["upcoming"]}
        self.assertEqual(got["Symposium registration"]["bucket"], "overdue")
        self.assertTrue(got["Symposium registration"]["overdue"])
        self.assertEqual(got["Feedback window opens"]["bucket"], "this_week")
        self.assertEqual(got["Summer Learning Series proposal"]["bucket"], "later")
        self.assertEqual(got["Add ticket to trust our mail"]["bucket"], "no_date")
        self.assertEqual(got["Add ticket to trust our mail"]["sessions"], ["Alpha"])
        self.assertNotIn("Past milestone", got)
        json.dumps(data)

    def test_malformed_item_warns_without_crashing(self):
        e = up_env([{"what": "ok", "due": dstr(2)}, {"due": "x"}, 5])
        text = render_env(e)
        self.assertIn("ok", text)
        self.assertEqual(text.count("warning: skipped a malformed upcoming item from Alpha"), 2)
