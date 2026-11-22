"""`fleetwatch notify`: a separate, opt-in process that raises a desktop notification when something
new needs you. The board never notifies, so several open boards never duplicate.

It reads the records the board reads (the registry, status files, index health, watch flags) every
poll and compares them with what it saw before. A notification fires on these transitions only:

    stuck     a session becomes stuck on a permission prompt
    question  a session has a new question (keyed by session and a hash of its text)
    due       one of your dated items is due today (once per item per day) or overdue (once)
    index     an index problem appears
    watch     a watch flag appears

Nothing already there when the first run starts is announced: that run records what it sees
silently. The record of what was seen is kept in the state folder (names and hashes, no text).

Content defaults to the session name and the kind only, because a banner can appear on a shared
screen. Question and item text is added only with `show_text = true`. Every string reaches the
delivery tool as one argv element, never through a shell and never inside script source, because
session names and questions are untrusted.

Settings, in settings.toml:

    [notify]
    enabled = true                    # false: keep watching, send nothing
    kinds = ["stuck", "question", "due", "index", "watch"]
    show_text = false                 # true: add the question or item text to the banner
    quiet_hours = "22:00-07:00"       # local time; "" (the default) means none
    poll_seconds = 20

Limits: one notification per session per kind per 10 minutes, at most 5 a minute overall (the rest
become one summary), and quiet hours hold everything and send one summary when they end.
"""
import argparse
import hashlib
import importlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from . import config, data, settings

KINDS = ("stuck", "question", "due", "index", "watch")
TITLE = "fleetwatch"
SESSION_LIMIT_S = 600
MINUTE_LIMIT = 5
MAX_TEXT = 200
STATE_VERSION = 1
HHMM = re.compile(r"(\d{2}):(\d{2})")


# ---------------------------------------------------------------- settings

@dataclass(frozen=True)
class NotifySettings:
    enabled: bool
    kinds: tuple
    show_text: bool
    quiet_hours: tuple      # (start_minute, end_minute) of the local day, or None
    poll_seconds: float


def parse_quiet(text):
    """(start, end) minutes after midnight from "22:00-07:00"; None for an empty string."""
    if text is None or not str(text).strip():
        return None
    parts = str(text).strip().split("-")
    if len(parts) != 2:
        raise settings.SettingsError('settings.toml: notify.quiet_hours must look like "22:00-07:00"')
    out = []
    for part in parts:
        m = HHMM.fullmatch(part.strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise settings.SettingsError('settings.toml: notify.quiet_hours must look like "22:00-07:00"')
        out.append(int(m.group(1)) * 60 + int(m.group(2)))
    if out[0] == out[1]:
        raise settings.SettingsError("settings.toml: notify.quiet_hours start and end must differ")
    return tuple(out)


def parse_settings(raw=None):
    """The [notify] table, checked. Raises SettingsError naming the key."""
    raw = settings.load_strict() if raw is None else raw
    t = raw.get("notify", {})
    if not isinstance(t, dict):
        raise settings.SettingsError("settings.toml: [notify] must be a table")
    unknown = sorted(set(t) - {"enabled", "kinds", "show_text", "quiet_hours", "poll_seconds"})
    if unknown:
        raise settings.SettingsError("settings.toml: notify.%s is not a setting" % unknown[0])
    for key in ("enabled", "show_text"):
        if key in t and not isinstance(t[key], bool):
            raise settings.SettingsError("settings.toml: notify.%s must be true or false" % key)
    kinds = t.get("kinds", list(KINDS))
    if not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds):
        raise settings.SettingsError("settings.toml: notify.kinds must be a list of kinds")
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        raise settings.SettingsError("settings.toml: notify.kinds %r is not one of %s" % (bad[0], ", ".join(KINDS)))
    poll = t.get("poll_seconds", 20)
    if isinstance(poll, bool) or not isinstance(poll, (int, float)) or poll < 1:
        raise settings.SettingsError("settings.toml: notify.poll_seconds must be a number of at least 1")
    quiet = t.get("quiet_hours", "")
    if not isinstance(quiet, str):
        raise settings.SettingsError('settings.toml: notify.quiet_hours must be a string like "22:00-07:00"')
    return NotifySettings(t.get("enabled", True), tuple(k for k in KINDS if k in kinds),
                          t.get("show_text", False), parse_quiet(quiet), float(poll))


def in_quiet(quiet, now, tz=None):
    """True when `now` falls inside the quiet window, which may wrap midnight."""
    if not quiet:
        return False
    local = datetime.fromtimestamp(now, tz or settings.LOCAL_TZ)
    minute = local.hour * 60 + local.minute
    start, end = quiet
    return start <= minute < end if start < end else (minute >= start or minute < end)


# ---------------------------------------------------------------- what is there

@dataclass(frozen=True)
class Item:
    """One thing that needs you. `key` is its identity and holds no text; `text` is shown only with show_text."""
    kind: str
    key: tuple
    name: str               # the session, or the index, it concerns
    sid: str = ""           # the session ID, for the click
    text: str = ""
    variant: str = ""       # a due item's "today" or "overdue"; "run" on a watch item about the runs


def _gkind(item):
    """The announcement group of an item: a due item is grouped by whether it is due today or overdue."""
    return "due:" + item.variant if item.kind == "due" and item.variant else item.kind


def _hash(text):
    return hashlib.sha1(str(text).encode("utf-8", "replace")).hexdigest()[:12]


def index_items(now):
    """One item per index with a problem; nothing when the index extra is missing or the registry cannot be read."""
    try:
        from . import index_health
        res = index_health.load()
        return [Item("index", ("index", r.name, ", ".join(r.problems(now))), r.name,
                     text=", ".join(r.problems(now))) for r in index_health.problem_rows(res.rows, now)]
    except Exception:
        return []


def watch_items(paths, now):
    """Watch flags, through lane `watches`' module when it exists: `flags(paths, now)` returns
    (key, session name, text) tuples. Nothing when the module is absent or fails."""
    try:
        # import_module reads sys.modules, so a module that is absent (or masked there) reads
        # as absent even after an earlier import bound it on the package.
        watches = importlib.import_module(__package__ + ".watches")
        flags = list(watches.flags(paths, now))
    except Exception:
        return []
    # an item with no owner is about the watch runs themselves (watches.RUN_KEY)
    return [Item("watch", ("watch", _hash(k)), data.clean(n) or "Watches", text=data.clean(t),
                 variant="" if n else "run") for k, n, t in flags]


def observe(paths, now, ppids=None):
    """Every item that needs you right now. Read-only: it never moves or writes a status file."""
    warnings = []
    records = data.build_records(paths, now, warnings, ppids=ppids)
    sections, stale = data.classify(records, now, data.parse_window("24h"), True)
    view = data.build_view(sections, now, data.DEFAULT_HORIZON_DAYS, stale)
    items, due_seen = [], set()
    today = datetime.fromtimestamp(now, settings.LOCAL_TZ).date().isoformat()
    for x in view["for_you"]:
        name, sid = x["name"], x["r"].get("sessionId") or ""
        if x["stuck"] is not None or x["helper_stuck"]:
            items.append(Item("stuck", ("stuck", sid or name), name, sid, "stuck on a prompt"))
        for text, _since, _tag in x["qs"]:
            items.append(Item("question", ("question", sid or name, _hash(text)), name, sid, text))
        for t in x["tasks"]:
            if t["kind"] not in ("today", "overdue"):
                continue
            key = (t["kind"], _hash(t["what"]), t["due"].isoformat() if t["due"] else "")
            if t["kind"] == "today":
                key += (today,)
            if key in due_seen:
                continue
            due_seen.add(key)
            items.append(Item("due", key, name, sid, t["what"], t["kind"]))
    return items + index_items(now) + watch_items(paths, now)


# ---------------------------------------------------------------- delivery

def _plain(text, limit=MAX_TEXT):
    """Untrusted text made safe to hand over: control characters out, capped, and never starting with
    a dash, which a delivery tool could read as one of its own options."""
    return data.clean(text).strip().lstrip("-").lstrip()[:limit]


@dataclass(frozen=True)
class Message:
    title: str
    text: str
    click: list = None      # an argv list that raises the session; None for a summary


def _run(runner, argv):
    try:
        return runner(argv, capture_output=True, text=True, timeout=15).returncode == 0
    except Exception:
        return False


OSASCRIPT = ("on run argv", "display notification (item 2 of argv) with title (item 1 of argv)", "end run")


class Osascript:
    """macOS without terminal-notifier: no click action."""
    name = "osascript"

    def __init__(self, runner=subprocess.run):
        self.runner = runner

    def send(self, msg):
        argv = ["osascript"]
        for line in OSASCRIPT:
            argv += ["-e", line]
        return _run(self.runner, argv + [_plain(msg.title), _plain(msg.text)])


class TerminalNotifier:
    """macOS with terminal-notifier: a click runs the jump command."""
    name = "terminal-notifier"

    def __init__(self, exe="terminal-notifier", runner=subprocess.run):
        self.exe, self.runner = exe, runner

    def send(self, msg):
        argv = [self.exe, "-title", _plain(msg.title), "-message", _plain(msg.text), "-group", TITLE]
        if msg.click:
            argv += ["-execute", shlex.join(msg.click)]
        return _run(self.runner, argv)


class NotifySend:
    name = "notify-send"

    def __init__(self, exe="notify-send", runner=subprocess.run):
        self.exe, self.runner = exe, runner

    def send(self, msg):
        return _run(self.runner, [self.exe, "--app-name", TITLE, "--", _plain(msg.title), _plain(msg.text)])


class PrintLine:
    """No notification tool: one line to the output."""
    name = "print"

    def __init__(self, out=None):
        self.out = out

    def send(self, msg):
        print("%s: %s" % (msg.title, msg.text), file=self.out or sys.stdout, flush=True)
        return True


def pick_backend(platform=None, which=shutil.which, runner=subprocess.run, out=None, environ=None):
    """The delivery tool for this machine. FLEETWATCH_NOTIFY_BACKEND=print forces one line of output."""
    platform = platform or sys.platform
    if (os.environ if environ is None else environ).get("FLEETWATCH_NOTIFY_BACKEND") == "print":
        return PrintLine(out)
    if platform == "darwin":
        tn = which("terminal-notifier")
        return TerminalNotifier(tn, runner) if tn else Osascript(runner)
    if platform.startswith(("linux", "freebsd", "openbsd", "netbsd")):
        ns = which("notify-send")
        if ns:
            return NotifySend(ns, runner)
    return PrintLine(out)


# ---------------------------------------------------------------- the transitions

def click_command(item):
    """The argv that raises the item's session, or None when it has none (an index, the watch runs)."""
    base = [sys.executable, "-P", "-m", "fleetwatch", "jump"]
    if item.sid:
        return base + ["--session-id", item.sid]
    if item.kind in ("index",) or (item.kind == "watch" and item.variant == "run"):
        return None
    return base + ["--", item.name]


def _plural(n, one, many):
    return "%d %s" % (n, one if n == 1 else many)


class Notifier:
    """Compares each poll with what it has seen and sends what is new, within the limits.

    `state` (a JSON-able dict) is everything that must survive a restart: `seen` (the keys of
    what is present and already announced or skipped), `last` (when each session and kind was
    last announced), `sent` (send times in the last minute) and `held` (something was held back
    by quiet hours). An item that is present and not in `seen` is waiting to be announced; it is
    announced as soon as the limits allow, and dropped if it goes away first.
    """

    def __init__(self, cfg, backend, tz=None, state=None):
        self.settings, self.backend, self.tz = cfg, backend, tz
        self.state = state if state is not None else {}

    # -- state
    def _seen(self):
        return {tuple(k) for k in self.state.get("seen", [])}

    def _fid(self, item):
        return (item.kind,) + tuple(item.key)

    def tick(self, items, now):
        """Process one poll; returns the Messages sent."""
        st = self.state
        here = {self._fid(i): i for i in items}
        seen = {k for k in self._seen() if k in here}          # an item that went away may be announced again
        if not st.get("version"):
            st["version"] = STATE_VERSION                         # the first poll: record it, announce nothing
            seen |= set(here)
            self._store(seen)
            return []
        cfg = self.settings
        fresh = [i for k, i in here.items() if k not in seen]
        seen |= {self._fid(i) for i in fresh if i.kind not in cfg.kinds}    # a kind turned off is skipped, not saved up
        fresh = [i for i in fresh if i.kind in cfg.kinds]
        if not cfg.enabled:
            seen |= {self._fid(i) for i in fresh}
            self._store(seen)
            return []
        out = []
        last = {tuple(k.split("\x1f")): v for k, v in st.get("last", {}).items()}
        sent = [t for t in st.get("sent", []) if now - t < 60]
        groups = {}
        for i in fresh:
            groups.setdefault((i.name, _gkind(i)), []).append(i)
        groups = {k: v for k, v in groups.items() if now - last.get(k, -1e12) >= SESSION_LIMIT_S}
        order = sorted(groups.items(), key=lambda kv: KINDS.index(kv[0][1].split(":")[0]))
        if in_quiet(cfg.quiet_hours, now, self.tz):
            if order:
                st["held"] = True
            self._store(seen, last, sent)
            return []
        room = MINUTE_LIMIT - len(sent)
        if st.get("held") and order:
            if room >= 1:
                out.append(self._summary(order, False))
                self._mark(order, seen, last, now)
                st["held"] = False
                order, room = [], room - 1
        elif st.get("held"):
            st["held"] = False
        if order and room >= 1:
            if len(order) <= room:
                individual, rest = order, []
            else:
                individual, rest = order[:room - 1], order[room - 1:]
            for (_name, _kind), members in individual:
                out.append(self._single(members))
            if rest:
                out.append(self._summary(rest, bool(individual)))
            self._mark(order, seen, last, now)
        for m in out:
            self.backend.send(m)
            sent.append(now)
        self._store(seen, last, sent)
        return out

    def _mark(self, groups, seen, last, now):
        for (name, kind), members in groups:
            last[(name, kind)] = now
            seen |= {self._fid(i) for i in members}

    def _store(self, seen, last=None, sent=None):
        st = self.state
        st["seen"] = sorted(list(k) for k in seen)
        if last is not None:
            st["last"] = {"\x1f".join(k): v for k, v in last.items()}
        if sent is not None:
            st["sent"] = sent

    def _single(self, members):
        first = members[0]
        n = len(members)
        name = first.name
        if first.kind == "stuck":
            text = "%s is stuck on a prompt" % name
        elif first.kind == "question":
            text = "%s has a new question" % name if n == 1 else "%s has %d new questions" % (name, n)
        elif first.kind == "due":
            overdue = first.variant == "overdue"
            text = ("%s has an overdue item" if n == 1 else "%s has overdue items") % name if overdue \
                else ("%s has an item due today" if n == 1 else "%s has items due today") % name
        elif first.kind == "index":
            text = "Index %s has a problem" % name
        elif first.variant == "run":
            text = "Watches are not running"
        else:
            text = "%s has a watch flag" % name
        if self.settings.show_text:
            detail = "; ".join(i.text for i in members if i.text and first.kind != "stuck")
            if detail:
                text += ": " + detail
        return Message(TITLE, text, click_command(first))

    def _summary(self, groups, more):
        indexes = {name for (name, kind), _m in groups if kind == "index"}
        sessions = {name for (name, kind), _m in groups if kind != "index"}
        total = len(indexes) + len(sessions)
        lead = "%d %s" % (total, "more " if more else "")
        if not sessions:
            return Message(TITLE, "%s%s you" % (lead, "index needs" if total == 1 else "indexes need"))
        if not indexes:
            return Message(TITLE, "%s%s you" % (lead, "session needs" if total == 1 else "sessions need"))
        return Message(TITLE, "%sneed you (%d session%s, %d index%s)" % (
            lead, len(sessions), "" if len(sessions) == 1 else "s",
            len(indexes), "" if len(indexes) == 1 else "es"))


# ---------------------------------------------------------------- state file and the loop

def state_path():
    return os.path.join(settings.state_dir(), "notify-state.json")


def load_state(path=None):
    try:
        with open(path or state_path(), encoding="utf-8") as fh:
            st = json.load(fh)
        return st if isinstance(st, dict) and st.get("version") == STATE_VERSION else {}
    except (OSError, ValueError):
        return {}


def save_state(state, path=None):
    path = path or state_path()
    config.atomic_write(path, json.dumps(state))
    os.chmod(path, 0o600)


def loop(notifier, observe_fn, load_fn, sleep, stop, save=None):
    """Poll, announce, sleep, until `stop` is set. The sleep is the only wait, so the loop cannot spin;
    an error in one poll is reported and the loop sleeps and tries again."""
    warned = set()

    def warn(text):
        if text not in warned:
            warned.add(text)
            print("fleetwatch notify: %s" % text, file=sys.stderr)
    while not stop.is_set():
        try:
            try:
                notifier.settings = load_fn()
            except settings.SettingsError as e:
                warn(str(e))
            notifier.tick(observe_fn(), time.time())
            if save:
                save(notifier.state)
        except (BrokenPipeError, ConnectionResetError):
            return 0
        except Exception as e:
            warn("%s: %s" % (type(e).__name__, data.clean(e)))
        sleep(notifier.settings.poll_seconds)
    return 0


def _stop_on_signals(stop):
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, lambda *_a: stop.set())


def demo(out):
    """Run once on the demo fleet from an empty record, printing what would be sent. Nothing is delivered."""
    from . import demo as demo_fleet
    tmp, paths = demo_fleet.prepare()
    try:
        cfg = parse_settings({})
        notifier = Notifier(cfg, PrintLine(out), state={"version": STATE_VERSION})
        sent = notifier.tick(observe(paths, time.time(), ppids=None), time.time())
        if not sent:
            print("nothing would be sent", file=out)
        return 0
    finally:
        tmp.cleanup()


def main(argv, out=None, runner=subprocess.run, which=shutil.which):
    out = out or sys.stdout
    if argv[:1] == ["schedule"]:
        from . import notify_schedule
        return notify_schedule.main(argv[1:])
    p = argparse.ArgumentParser(
        prog="fleetwatch notify", description="Raise a desktop notification when something new needs you.",
        epilog="commands: `fleetwatch notify schedule install|remove|status` runs it at login.")
    p.add_argument("--once", action="store_true", help="check once, announce what is new, and exit")
    p.add_argument("--demo", action="store_true", help="print what it would send for the demo fleet; sends nothing")
    p.add_argument("--test", action="store_true", help="send one test notification and report the result")
    args = p.parse_args(argv)
    if args.demo:
        return demo(out)
    try:
        cfg = parse_settings()
    except settings.SettingsError as e:
        print("fleetwatch notify: %s" % e, file=sys.stderr)
        return 2
    backend = pick_backend(runner=runner, which=which, out=out)
    if args.test:
        ok = backend.send(Message(TITLE, "Test notification from fleetwatch"))
        print("sent through %s" % backend.name if ok else "could not send through %s" % backend.name, file=out)
        return 0 if ok else 1
    paths = data.default_paths()
    notifier = Notifier(cfg, backend, state=load_state())
    first = not notifier.state.get("version")
    if args.once:
        notifier.tick(observe(paths, time.time()), time.time())
        save_state(notifier.state)
        if first:
            print("seeded: recorded what is there now, announced nothing", file=out)
        return 0
    stop = threading.Event()
    _stop_on_signals(stop)
    print("fleetwatch notify: watching every %gs through %s" % (cfg.poll_seconds, backend.name),
          file=sys.stderr, flush=True)
    return loop(notifier, lambda: observe(paths, time.time()), parse_settings, stop.wait, stop,
                save=save_state)
