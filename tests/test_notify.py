"""fleetwatch notify: transitions, content, rate limits, quiet hours, delivery argv safety, the loop and install."""
import contextlib
import io
import json
import os
import plistlib
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import standin  # noqa: E402
from fleetwatch import notify, notify_schedule, settings  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc).timestamp()
SID = "aa11bb22-0000-4000-8000-000000000001"
EVIL = '"; do shell script "touch /tmp/x'


class Recording:
    name = "recording"

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)
        return True


def item(kind, name, key=None, text="", sid="", variant=""):
    return notify.Item(kind, key or (name, text), name, sid, text, variant)


def make(**over):
    cfg = dict(enabled=True, kinds=notify.KINDS, show_text=False, quiet_hours=None, poll_seconds=20)
    cfg.update(over)
    backend = Recording()
    return notify.Notifier(notify.NotifySettings(**cfg), backend, tz=timezone.utc), backend


def seeded(**over):
    n, b = make(**over)
    n.tick([], T0 - 3600)
    return n, b


def texts(backend):
    return [m.text for m in backend.sent]


class Transitions(unittest.TestCase):
    def test_first_tick_seeds_silently(self):
        n, b = make()
        out = n.tick([item("stuck", "Billing API"), item("question", "Checkout Flow", text="q")], T0)
        self.assertEqual(out, [])
        self.assertEqual(b.sent, [])
        n.tick([item("stuck", "Billing API"), item("question", "Checkout Flow", text="q")], T0 + 20)
        self.assertEqual(b.sent, [])

    def test_new_stuck_session_notifies_once(self):
        n, b = seeded()
        n.tick([item("stuck", "Billing API")], T0)
        n.tick([item("stuck", "Billing API")], T0 + 20)
        self.assertEqual(texts(b), ["Billing API is stuck on a prompt"])

    def test_stuck_again_after_clearing_notifies_after_the_window(self):
        n, b = seeded()
        n.tick([item("stuck", "A")], T0)
        n.tick([], T0 + 60)
        n.tick([item("stuck", "A")], T0 + 120)
        self.assertEqual(len(b.sent), 1)          # inside 10 minutes: held
        n.tick([item("stuck", "A")], T0 + 601)
        self.assertEqual(len(b.sent), 2)

    def test_new_question_is_keyed_by_text_hash(self):
        n, b = seeded()
        n.tick([item("question", "Checkout Flow", text="Ship it?")], T0)
        n.tick([item("question", "Checkout Flow", text="Ship it?")], T0 + 20)
        self.assertEqual(texts(b), ["Checkout Flow has a new question"])
        n.tick([item("question", "Checkout Flow", text="Ship it?"),
                item("question", "Checkout Flow", text="Drop the old sheet?")], T0 + 700)
        self.assertEqual(texts(b)[1:], ["Checkout Flow has a new question"])

    def test_two_new_questions_in_one_session_coalesce(self):
        n, b = seeded()
        n.tick([item("question", "Checkout Flow", text="a"), item("question", "Checkout Flow", text="b")], T0)
        self.assertEqual(texts(b), ["Checkout Flow has 2 new questions"])

    def test_due_today_once_per_item_per_day(self):
        n, b = seeded()
        today = item("due", "Newsletter", key=("today", "h1", "2026-11-12"), variant="today")
        n.tick([today], T0)
        n.tick([today], T0 + 20)
        self.assertEqual(texts(b), ["Newsletter has an item due today"])
        tomorrow = item("due", "Newsletter", key=("today", "h1", "2026-11-13"), variant="today")
        n.tick([tomorrow], T0 + 86400)
        self.assertEqual(len(b.sent), 2)

    def test_overdue_once(self):
        n, b = seeded()
        over = item("due", "Release Notes", key=("overdue", "h2"), variant="overdue")
        n.tick([over], T0)
        n.tick([over], T0 + 3600)
        n.tick([over], T0 + 86400)
        self.assertEqual(texts(b), ["Release Notes has an overdue item"])

    def test_index_problem_and_watch_flag(self):
        n, b = seeded()
        n.tick([item("index", "release-history", text="last refresh failed (build)"),
                item("watch", "Search Tuning", text="CI red")], T0)
        self.assertEqual(texts(b), ["Index release-history has a problem",
                                    "Search Tuning has a watch flag"])

    def test_disabled_kind_is_marked_seen_so_enabling_it_later_does_not_flood(self):
        n, b = seeded(kinds=("stuck",))
        n.tick([item("question", "A", text="q")], T0)
        self.assertEqual(b.sent, [])
        n.settings = notify.NotifySettings(True, notify.KINDS, False, None, 20)
        n.tick([item("question", "A", text="q")], T0 + 20)
        self.assertEqual(b.sent, [])

    def test_disabled_sends_nothing_and_stays_quiet_after_enabling(self):
        n, b = seeded(enabled=False)
        n.tick([item("stuck", "A")], T0)
        n.settings = notify.NotifySettings(True, notify.KINDS, False, None, 20)
        n.tick([item("stuck", "A")], T0 + 20)
        self.assertEqual(b.sent, [])

    def test_state_survives_a_restart(self):
        n, b = make()
        n.tick([item("stuck", "A")], T0)                       # seeds
        saved = json.loads(json.dumps(n.state))
        n2, b2 = make()
        n2.state = saved
        n2.tick([item("stuck", "A"), item("stuck", "B")], T0 + 60)
        self.assertEqual(texts(b2), ["B is stuck on a prompt"])


class Content(unittest.TestCase):
    def test_text_is_hidden_by_default(self):
        n, b = seeded()
        n.tick([item("question", "Billing API", text="Should refunds need a second approver?")], T0)
        for m in b.sent:
            self.assertNotIn("refunds", m.text + m.title)

    def test_show_text_adds_the_question(self):
        n, b = seeded(show_text=True)
        n.tick([item("question", "Billing API", text="Should refunds need a second approver?")], T0)
        self.assertIn("Should refunds need a second approver?", b.sent[0].text)
        self.assertTrue(b.sent[0].text.startswith("Billing API has a new question"))

    def test_default_settings_hide_text(self):
        self.assertFalse(notify.parse_settings({}).show_text)

    def test_click_targets_the_session_by_id_else_by_name(self):
        n, b = seeded()
        n.tick([item("stuck", "A", sid=SID), item("stuck", "-weird name")], T0)
        self.assertEqual(b.sent[0].click[-2:], ["--session-id", SID])
        self.assertEqual(b.sent[1].click[-2:], ["--", "-weird name"])
        self.assertEqual(b.sent[0].click[:4], [sys.executable, "-P", "-m", "fleetwatch"])
        self.assertEqual(b.sent[0].click[4], "jump")


class Limits(unittest.TestCase):
    def test_one_per_session_and_kind_per_ten_minutes(self):
        n, b = seeded()
        n.tick([item("question", "A", text="1")], T0)
        n.tick([item("question", "A", text="1"), item("question", "A", text="2")], T0 + 120)
        self.assertEqual(len(b.sent), 1)
        n.tick([item("question", "A", text="1"), item("question", "A", text="2")], T0 + 601)
        self.assertEqual(len(b.sent), 2)

    def test_a_different_kind_or_session_is_not_limited(self):
        n, b = seeded()
        n.tick([item("question", "A", text="1"), item("stuck", "A"), item("stuck", "B")], T0)
        self.assertEqual(len(b.sent), 3)

    def test_overflow_becomes_one_summary(self):
        n, b = seeded()
        n.tick([item("stuck", "S%d" % i) for i in range(8)], T0)
        self.assertEqual(len(b.sent), 5)
        self.assertEqual(texts(b)[:4], ["S%d is stuck on a prompt" % i for i in range(4)])
        self.assertEqual(texts(b)[4], "4 more sessions need you")
        self.assertIsNone(b.sent[4].click)
        n.tick([item("stuck", "S%d" % i) for i in range(8)], T0 + 20)
        self.assertEqual(len(b.sent), 5)          # the summarised ones are not announced again

    def test_a_full_minute_holds_new_items_until_it_clears(self):
        n, b = seeded()
        n.tick([item("stuck", "S%d" % i) for i in range(5)], T0)
        self.assertEqual(len(b.sent), 5)
        n.tick([item("stuck", "S%d" % i) for i in range(5)] + [item("stuck", "Late")], T0 + 30)
        self.assertEqual(len(b.sent), 5)
        n.tick([item("stuck", "S%d" % i) for i in range(5)] + [item("stuck", "Late")], T0 + 61)
        self.assertEqual(texts(b)[5], "Late is stuck on a prompt")

    def test_stuck_comes_before_other_kinds(self):
        n, b = seeded()
        n.tick([item("watch", "W"), item("question", "Q", text="x"), item("stuck", "S")], T0)
        self.assertEqual(texts(b)[0], "S is stuck on a prompt")


class QuietHours(unittest.TestCase):
    def setUp(self):
        self.quiet = notify.parse_quiet("22:00-07:00")

    def test_window_wraps_midnight(self):
        def at(h, m=0):
            return datetime(2026, 11, 12, h, m, tzinfo=timezone.utc).timestamp()
        for h, expect in ((23, True), (3, True), (6, True), (7, False), (12, False), (21, False), (22, True)):
            self.assertEqual(notify.in_quiet(self.quiet, at(h), timezone.utc), expect, h)
        day = notify.parse_quiet("09:00-17:00")
        self.assertTrue(notify.in_quiet(day, at(12), timezone.utc))
        self.assertFalse(notify.in_quiet(day, at(8), timezone.utc))
        self.assertFalse(notify.in_quiet(None, at(12), timezone.utc))

    def test_held_during_quiet_then_one_summary(self):
        n, b = make(quiet_hours=self.quiet)
        late = datetime(2026, 11, 12, 22, 30, tzinfo=timezone.utc).timestamp()
        n.tick([], late - 3600 * 2)
        n.tick([item("stuck", "A"), item("question", "B", text="q")], late)
        n.tick([item("stuck", "A"), item("question", "B", text="q"), item("stuck", "C")], late + 3600)
        self.assertEqual(b.sent, [])
        morning = datetime(2026, 11, 13, 7, 1, tzinfo=timezone.utc).timestamp()
        n.tick([item("stuck", "A"), item("question", "B", text="q"), item("stuck", "C")], morning)
        self.assertEqual(texts(b), ["3 sessions need you"])
        n.tick([item("stuck", "A"), item("question", "B", text="q"), item("stuck", "C")], morning + 20)
        self.assertEqual(len(b.sent), 1)

    def test_nothing_held_means_no_summary(self):
        n, b = make(quiet_hours=self.quiet)
        n.tick([], datetime(2026, 11, 12, 22, 30, tzinfo=timezone.utc).timestamp())
        n.tick([], datetime(2026, 11, 13, 7, 30, tzinfo=timezone.utc).timestamp())
        self.assertEqual(b.sent, [])


class Settings(unittest.TestCase):
    def test_defaults(self):
        s = notify.parse_settings({})
        self.assertEqual((s.enabled, s.kinds, s.show_text, s.quiet_hours, s.poll_seconds),
                         (True, notify.KINDS, False, None, 20))
        self.assertEqual(notify.KINDS, ("stuck", "question", "due", "index", "watch"))

    def test_values(self):
        s = notify.parse_settings({"notify": {"enabled": False, "kinds": ["stuck"], "show_text": True,
                                              "quiet_hours": "22:00-07:00", "poll_seconds": 5}})
        self.assertEqual((s.enabled, s.kinds, s.show_text, s.poll_seconds), (False, ("stuck",), True, 5))
        self.assertEqual(s.quiet_hours, (22 * 60, 7 * 60))

    def test_invalid_values_name_the_key(self):
        bad = ({"enabled": "yes"}, {"kinds": ["stuck", "nope"]}, {"kinds": "stuck"}, {"show_text": 1},
               {"quiet_hours": "late"}, {"quiet_hours": "25:00-07:00"}, {"quiet_hours": "08:00-08:00"},
               {"poll_seconds": 0}, {"poll_seconds": "20"}, {"poll_seconds": True}, {"surprise": 1})
        for table in bad:
            with self.assertRaises(settings.SettingsError, msg=table) as cm:
                notify.parse_settings({"notify": table})
            self.assertIn("notify", str(cm.exception))
        with self.assertRaises(settings.SettingsError):
            notify.parse_settings({"notify": "on"})

    def test_empty_quiet_hours_means_none(self):
        self.assertIsNone(notify.parse_settings({"notify": {"quiet_hours": ""}}).quiet_hours)


class Delivery(unittest.TestCase):
    def runner(self):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, "", "")
        return calls, run

    def test_osascript_gets_the_text_only_as_argv(self):
        calls, run = self.runner()
        ok = notify.Osascript(run).send(notify.Message("fleetwatch", EVIL + " is stuck on a prompt"))
        self.assertTrue(ok)
        argv, kw = calls[0]
        self.assertIsInstance(argv, list)
        self.assertFalse(kw.get("shell"))
        self.assertEqual(argv[0], "osascript")
        scripts = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        self.assertIn("on run argv", scripts[0])
        for s in scripts:
            self.assertNotIn("touch", s)
            self.assertNotIn(EVIL, s)
        self.assertEqual(argv[-2:], ["fleetwatch", EVIL + " is stuck on a prompt"])
        self.assertEqual(sum(EVIL in a for a in argv), 1)

    def test_terminal_notifier_argv_and_click(self):
        calls, run = self.runner()
        click = [sys.executable, "-P", "-m", "fleetwatch", "jump", "--", "x; rm -rf ~"]
        notify.TerminalNotifier("/bin/terminal-notifier", run).send(
            notify.Message("fleetwatch", "A is stuck on a prompt", click))
        argv, kw = calls[0]
        self.assertFalse(kw.get("shell"))
        self.assertEqual(argv[0], "/bin/terminal-notifier")
        self.assertEqual(argv[argv.index("-message") + 1], "A is stuck on a prompt")
        execute = argv[argv.index("-execute") + 1]
        self.assertEqual(shlex.split(execute), click)

    def test_terminal_notifier_without_click_has_no_execute(self):
        calls, run = self.runner()
        notify.TerminalNotifier("/bin/tn", run).send(notify.Message("fleetwatch", "4 more sessions need you"))
        self.assertNotIn("-execute", calls[0][0])

    def test_leading_dash_cannot_become_an_option(self):
        calls, run = self.runner()
        notify.TerminalNotifier("tn", run).send(notify.Message("fleetwatch", "-open evil is stuck on a prompt"))
        argv = calls[0][0]
        self.assertFalse(argv[argv.index("-message") + 1].startswith("-"))
        calls.clear()
        notify.NotifySend("notify-send", run).send(notify.Message("-t", "-h evil"))
        argv = calls[0][0]
        self.assertEqual(argv[-3:-2], ["--"])

    def test_notify_send_argv(self):
        calls, run = self.runner()
        notify.NotifySend("notify-send", run).send(notify.Message("fleetwatch", EVIL))
        argv, kw = calls[0]
        self.assertEqual(argv[0], "notify-send")
        self.assertIn(EVIL, argv)
        self.assertFalse(kw.get("shell"))

    def test_print_line(self):
        out = io.StringIO()
        notify.PrintLine(out).send(notify.Message("fleetwatch", "A is stuck on a prompt"))
        self.assertEqual(out.getvalue(), "fleetwatch: A is stuck on a prompt\n")

    def test_backend_reports_failure(self):
        def bad(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, "", "no")
        self.assertFalse(notify.Osascript(bad).send(notify.Message("t", "m")))

        def boom(argv, **kw):
            raise OSError("gone")
        self.assertFalse(notify.Osascript(boom).send(notify.Message("t", "m")))

    def test_text_is_cleaned_and_capped(self):
        calls, run = self.runner()
        notify.Osascript(run).send(notify.Message("fleetwatch", "A\x00\x1b[31m " + "x" * 500))
        msg = calls[0][0][-1]
        self.assertNotIn("\x1b", msg)
        self.assertNotIn("\x00", msg)
        self.assertLessEqual(len(msg), 200)

    def test_pick_backend(self):
        have = {"terminal-notifier": "/opt/tn", "notify-send": "/usr/bin/notify-send"}
        pick = lambda plat, avail: notify.pick_backend(plat, lambda x: have.get(x) if x in avail else None, None)  # noqa: E731
        self.assertIsInstance(pick("darwin", ("terminal-notifier",)), notify.TerminalNotifier)
        self.assertIsInstance(pick("darwin", ()), notify.Osascript)
        self.assertIsInstance(pick("linux", ("notify-send",)), notify.NotifySend)
        self.assertIsInstance(pick("linux", ()), notify.PrintLine)
        self.assertIsInstance(pick("freebsd13", ("notify-send",)), notify.NotifySend)
        self.assertIsInstance(pick("win32", ()), notify.PrintLine)


def fleet(root, sessions=(), status=()):
    """A registry and status folder: sessions are (name, sid, state); status is {file: doc}."""
    paths = {k: os.path.join(root, k) for k in ("sessions", "jobs", "status", "projects")}
    for p in paths.values():
        os.makedirs(p, exist_ok=True)
    for name, sid, state in sessions:
        with open(os.path.join(paths["sessions"], sid + ".json"), "w") as fh:
            json.dump({"name": name, "sessionId": sid, "pid": 1, "kind": "interactive", "cwd": "/x",
                       "status": state, "statusUpdatedAt": int(time.time() * 1000),
                       "updatedAt": int(time.time() * 1000), "startedAt": int(time.time() * 1000)}, fh)
    for fname, doc in dict(status).items():
        with open(os.path.join(paths["status"], fname), "w") as fh:
            json.dump(doc, fh)
    paths["command"] = "true"
    paths["ps"] = "true"
    return paths


class Observe(unittest.TestCase):
    def test_standin_fleet(self):
        with tempfile.TemporaryDirectory() as root:
            paths = standin.build(root)
            with mock.patch.dict(os.environ, {"FLEETWATCH_DEMO": ""}), \
                    mock.patch.object(notify, "index_items", return_value=[]), \
                    mock.patch.object(notify, "watch_items", return_value=[]):
                items = notify.observe(paths, standin.NOW, ppids=standin.PPIDS)
        by = {}
        for i in items:
            by.setdefault(i.kind, []).append(i)
        self.assertEqual([i.name for i in by["stuck"]], ["Stuck Session"])
        self.assertTrue(by["stuck"][0].sid)
        qs = {i.text for i in by["question"]}
        self.assertIn("Which branch should the fix land on?", qs)
        self.assertIn("Approve the schema change?", qs)
        over = [i for i in by["due"] if i.variant == "overdue"]
        self.assertEqual([i.name for i in over], ["Stuck Session"])
        self.assertNotIn("Overdue thing", repr([i.key for i in over]))      # no text in keys

    def test_due_today_item_has_a_dated_key_and_no_text(self):
        today = datetime.fromtimestamp(time.time(), settings.LOCAL_TZ).date().isoformat()
        with tempfile.TemporaryDirectory() as root:
            paths = fleet(root, [("Newsletter", SID, "idle")], {"n.json": {
                "session": "Newsletter", "sessionId": SID, "updated": datetime.now(timezone.utc).isoformat(),
                "upcoming": [{"what": "Approve the draft", "due": today, "kind": "deadline", "yours": True},
                             {"what": "Not mine", "due": today, "kind": "deadline"}]}})
            with mock.patch.object(notify, "index_items", return_value=[]), \
                    mock.patch.object(notify, "watch_items", return_value=[]):
                items = notify.observe(paths, time.time(), ppids={})
        due = [i for i in items if i.kind == "due"]
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].variant, "today")
        self.assertEqual(due[0].name, "Newsletter")
        self.assertIn(today, due[0].key)
        self.assertNotIn("Approve", repr(due[0].key))

    def test_watch_hook_is_empty_when_the_module_is_absent(self):
        with mock.patch.dict(sys.modules, {"fleetwatch.watches": None}):
            self.assertEqual(notify.watch_items({}, T0), [])

    def test_watch_hook_reads_flags(self):
        fake = mock.Mock()
        fake.flags.return_value = [("k1", "Search Tuning", "CI red")]
        with mock.patch.dict(sys.modules, {"fleetwatch.watches": fake}):
            with mock.patch("fleetwatch.watches", fake, create=True):
                items = notify.watch_items({}, T0)
        self.assertEqual([(i.kind, i.name, i.text) for i in items], [("watch", "Search Tuning", "CI red")])

    def test_watch_hook_survives_a_broken_module(self):
        fake = mock.Mock()
        fake.flags.side_effect = RuntimeError("x")
        with mock.patch.dict(sys.modules, {"fleetwatch.watches": fake}), mock.patch("fleetwatch.watches", fake, create=True):
            self.assertEqual(notify.watch_items({}, T0), [])

    def test_index_problems_become_items(self):
        from fleetwatch.index_health import Health, Result
        rows = [Health("good", "team", "plain", "ok", finished=T0, stale_after=3600),
                Health("bad", "team", "session", "failed", "build", finished=T0, stale_after=3600)]
        with mock.patch("fleetwatch.index_health.load", return_value=Result(rows)):
            items = notify.index_items(T0)
        self.assertEqual([(i.kind, i.name) for i in items], [("index", "bad")])
        self.assertIn("failed", items[0].text)


class Loop(unittest.TestCase):
    def test_sleeps_between_polls_and_never_spins(self):
        import threading
        stop = threading.Event()
        sleeps, ticks = [], []
        n, b = make(poll_seconds=7)

        def sleep(sec):
            sleeps.append(sec)
            if len(sleeps) == 3:
                stop.set()
        def observe():
            ticks.append(1)
            if len(ticks) >= 50:          # a loop that never sleeps fails here instead of hanging
                stop.set()
            return []
        notify.loop(n, observe, lambda: n.settings, sleep, stop)
        self.assertEqual(sleeps, [7, 7, 7])
        self.assertEqual(len(ticks), 3)

    def test_an_observation_error_still_sleeps(self):
        import threading
        stop = threading.Event()
        sleeps = []
        n, b = make()

        def sleep(sec):
            sleeps.append(sec)
            if len(sleeps) == 2:
                stop.set()

        def boom():
            raise RuntimeError("registry unreadable")
        with contextlib.redirect_stderr(io.StringIO()):
            notify.loop(n, boom, lambda: n.settings, sleep, stop)
        self.assertEqual(len(sleeps), 2)

    def test_settings_reload_each_poll_and_a_bad_edit_keeps_the_old(self):
        import threading
        stop = threading.Event()
        n, b = make()
        seq = [notify.NotifySettings(True, ("stuck",), False, None, 9), settings.SettingsError("bad")]
        loads = []

        def load():
            v = seq[len(loads)] if len(loads) < len(seq) else seq[0]
            loads.append(1)
            if isinstance(v, Exception):
                raise v
            return v

        def sleep(sec):
            if len(loads) >= 2:
                stop.set()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            notify.loop(n, lambda: [], load, sleep, stop)
        self.assertEqual(n.settings.kinds, ("stuck",))
        self.assertIn("bad", err.getvalue())

    def test_a_closed_output_ends_the_loop_cleanly(self):
        import threading
        stop = threading.Event()
        n, b = make()

        class Gone:
            def send(self, msg):
                raise BrokenPipeError()
        n.backend = Gone()
        n.tick([], T0 - 1)
        rc = notify.loop(n, lambda: [item("stuck", "A")], lambda: n.settings, lambda s: None, stop)
        self.assertEqual(rc, 0)


def run_cli(env, *argv, timeout=20):
    full = dict(os.environ)
    full.update(env)
    full["PYTHONPATH"] = str(REPO)
    full["FLEETWATCH_NOTIFY_BACKEND"] = "print"
    return subprocess.run([sys.executable, "-P", "-m", "fleetwatch", *argv], capture_output=True, text=True,
                          env=full, timeout=timeout, cwd=env.get("HOME"))


class Process(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        paths = fleet(os.path.join(root, "fleet"))
        self.env = {"HOME": root, "FLEETWATCH_NO_MIGRATE": "1", "FLEETWATCH_CONFIG_DIR": os.path.join(root, "cfg"),
                    "XDG_STATE_HOME": os.path.join(root, "state"), "XDG_DATA_HOME": os.path.join(root, "data"),
                    "FLEETWATCH_SESSIONS_DIR": paths["sessions"], "FLEETWATCH_JOBS_DIR": paths["jobs"],
                    "FLEETWATCH_STATUS_DIR": paths["status"], "FLEETWATCH_PROJECTS_DIR": paths["projects"],
                    "FLEETWATCH_AGENTS_CMD": "true", "FLEETWATCH_PS_CMD": "true"}
        os.makedirs(self.env["FLEETWATCH_CONFIG_DIR"])
        with open(os.path.join(self.env["FLEETWATCH_CONFIG_DIR"], "settings.toml"), "w") as fh:
            fh.write("[notify]\npoll_seconds = 1\n")

    def tearDown(self):
        self.tmp.cleanup()

    def start(self):
        env = dict(os.environ)
        env.update(self.env)
        env["PYTHONPATH"] = str(REPO)
        env["FLEETWATCH_NOTIFY_BACKEND"] = "print"
        return subprocess.Popen([sys.executable, "-P", "-m", "fleetwatch", "notify"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.tmp.name)

    def stop_with(self, sig):
        p = self.start()
        try:
            time.sleep(2.5)
            self.assertIsNone(p.poll(), p.stderr.read() if p.poll() is not None else "")
            os.kill(p.pid, sig)
            p.wait(timeout=10)
            return p.returncode
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stdout.close()
            p.stderr.close()

    def test_sigterm_exits_cleanly(self):
        self.assertEqual(self.stop_with(signal.SIGTERM), 0)

    def test_sighup_exits_cleanly(self):
        self.assertEqual(self.stop_with(signal.SIGHUP), 0)

    def test_the_loop_idles_instead_of_spinning(self):
        p = self.start()
        try:
            time.sleep(1.0)
            before = self.cpu(p.pid)
            time.sleep(4.0)
            after = self.cpu(p.pid)
            self.assertLess(after - before, 1.0, "used %.2fs of CPU in 4s of wall time" % (after - before))
        finally:
            p.terminate()
            p.wait(timeout=10)
            p.stdout.close()
            p.stderr.close()

    @staticmethod
    def cpu(pid):
        out = subprocess.run(["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        parts = out.replace(".", ":").split(":")
        nums = [int(x) for x in parts if x.isdigit()]
        h = m = s = 0
        frac = 0.0
        if len(nums) == 4:        # m:ss.cc
            m, s, frac = nums[0], nums[1], nums[2] / 100
        elif len(nums) == 3 and ":" in out and "." in out:
            m, s, frac = nums[0], nums[1], nums[2] / 100
        elif len(nums) == 3:
            h, m, s = nums
        return h * 3600 + m * 60 + s + frac

    def test_once_seeds_then_reports_only_what_is_new(self):
        first = run_cli(self.env, "notify", "--once")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("seeded", first.stdout)
        fleet(os.path.join(self.tmp.name, "fleet"), [("Billing API", SID, "waiting")])
        second = run_cli(self.env, "notify", "--once")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Billing API is stuck on a prompt", second.stdout)
        third = run_cli(self.env, "notify", "--once")
        self.assertNotIn("Billing API", third.stdout)

    def test_demo_prints_and_sends_nothing(self):
        env = dict(self.env, FLEETWATCH_OSASCRIPT="false")
        out = run_cli(env, "notify", "--demo")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Billing API is stuck on a prompt", out.stdout)
        self.assertIn("Checkout Flow has 2 new questions", out.stdout)
        self.assertNotIn("second approver", out.stdout)
        self.assertIn("more sessions need you", out.stdout)

    def test_test_flag_uses_the_picked_backend(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        buf = io.StringIO()
        with mock.patch.object(notify.sys, "platform", "darwin"):
            rc = notify.main(["--test"], out=buf, runner=run, which=lambda x: None)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][0], "osascript")
        self.assertIn("osascript", buf.getvalue())

        def fail(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, "", "denied")
        with mock.patch.object(notify.sys, "platform", "darwin"):
            self.assertEqual(notify.main(["--test"], out=io.StringIO(), runner=fail, which=lambda x: None), 1)

    def test_invalid_settings_stop_the_start(self):
        with open(os.path.join(self.env["FLEETWATCH_CONFIG_DIR"], "settings.toml"), "w") as fh:
            fh.write('[notify]\nkinds = ["nope"]\n')
        out = run_cli(self.env, "notify", "--once")
        self.assertEqual(out.returncode, 2)
        self.assertIn("notify.kinds", out.stderr)


class Schedule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "FLEETWATCH_LAUNCH_AGENTS_DIR": os.path.join(self.tmp.name, "LaunchAgents"),
            "FLEETWATCH_SYSTEMD_USER_DIR": os.path.join(self.tmp.name, "systemd"),
            "XDG_STATE_HOME": os.path.join(self.tmp.name, "state")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_plist_keeps_it_alive_and_runs_notify(self):
        d = notify_schedule.plist_dict(self.tmp.name)
        self.assertEqual(d["Label"], "local.fleetwatch.notify")
        self.assertEqual(d["ProgramArguments"], [sys.executable, "-P", "-m", "fleetwatch", "notify"])
        self.assertTrue(d["KeepAlive"])
        self.assertIn("ThrottleInterval", d)
        self.assertTrue(d["StandardErrorPath"].startswith(self.tmp.name))

    def test_job_carries_the_folder_overrides(self):
        over = {"FLEETWATCH_STATUS_DIR": "/x/status", "FLEETWATCH_SESSIONS_DIR": "/x/sessions"}
        with mock.patch.dict(os.environ, over):
            env = notify_schedule.plist_dict(self.tmp.name)["EnvironmentVariables"]
            for k, v in over.items():
                self.assertEqual(env[k], v)
            out = io.StringIO()
            with mock.patch.object(sys, "platform", "linux"):
                notify_schedule.main(["install"], out)
            unit = (Path(self.tmp.name) / "systemd" / "fleetwatch-notify.service").read_text()
            for k, v in over.items():
                self.assertIn("Environment=%s=%s" % (k, v), unit.replace('"', ""))

    def test_install_writes_the_plist_and_prints_the_start_command(self):
        out = io.StringIO()
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(notify_schedule.main(["install"], out), 0)
            path = Path(self.tmp.name) / "LaunchAgents" / "local.fleetwatch.notify.plist"
            self.assertEqual(plistlib.loads(path.read_bytes())["Label"], "local.fleetwatch.notify")
            self.assertIn("launchctl bootstrap", out.getvalue())
            self.assertIn("not loaded", out.getvalue())
            st = io.StringIO()
            notify_schedule.main(["status"], st)
            self.assertIn("installed", st.getvalue())
            rm = io.StringIO()
            notify_schedule.main(["remove"], rm)
            self.assertFalse(path.exists())
            self.assertIn("launchctl bootout", rm.getvalue())

    def test_install_writes_a_systemd_service(self):
        out = io.StringIO()
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(notify_schedule.main(["install"], out), 0)
            unit = (Path(self.tmp.name) / "systemd" / "fleetwatch-notify.service").read_text()
            self.assertIn("ExecStart=", unit)
            self.assertIn("fleetwatch notify", unit.replace("'", "").replace("-m fleetwatch", "fleetwatch"))
            self.assertIn("Restart=on-failure", unit)
            self.assertIn("WantedBy=default.target", unit)
            self.assertIn("systemctl --user enable --now fleetwatch-notify.service", out.getvalue())
            self.assertNotIn("started", out.getvalue())

    def test_usage(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(notify_schedule.main([]), 2)

    def test_schedule_never_starts_anything(self):
        with mock.patch.object(sys, "platform", "darwin"), mock.patch("subprocess.run") as run, \
                mock.patch("subprocess.Popen") as popen:
            notify_schedule.main(["install"], io.StringIO())
        run.assert_not_called()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
