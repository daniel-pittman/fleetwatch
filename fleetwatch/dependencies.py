"""Everything fleetwatch relies on outside its own code: its scheduled jobs, the folders it reads and writes,
and the programs it calls. `fleetwatch install`, `uninstall` and `doctor` work from this list, and
docs/dependencies.md is printed from it (`fleetwatch doctor --inventory`); a test keeps the two in step.

Each entry has a name, a kind ("job", "folder" or "tool"), what it is for, an install step, a check, and
the labels of any older scheduled job it replaces. A check returns Check rows with a status of "ok",
"warn" (worth knowing, nothing is broken) or "fail" (doctor exits 1). Nothing here prints index content
or transcript text: an index's last error is reduced to its first line.
"""
import io
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import archive, data, notify_schedule, schedule, services, settings

ARCHIVE = schedule.ARCHIVE


def archive_root():
    """The transcript archive (it holds projects/): archive.archive_root()."""
    return Path(archive.archive_root())


STALE_FACTOR = 2
MAX_ERROR = 160
OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass(frozen=True)
class Check:
    item: str
    check: str
    status: str             # OK, WARN or FAIL
    detail: str = ""
    fix: str = ""           # the command or step that repairs it, "" when there is none

    def as_dict(self):
        return {"item": self.item, "check": self.check, "status": self.status, "detail": self.detail,
                "fix": self.fix}


@dataclass
class Context:
    manager: services.Manager
    out: object = None
    dry_run: bool = False
    now: float = None

    def say(self, text):
        print(text, file=self.out or sys.stdout)

    def time(self):
        return time.time() if self.now is None else self.now


def _home(path):
    """A path as the docs and messages show it, with the home folder as ~."""
    home = os.path.expanduser("~")
    text = str(path)
    return "~" + text[len(home):] if home and (text == home or text.startswith(home + os.sep)) else text


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _first_line(text, limit=MAX_ERROR):
    line = next((x for x in str(text or "").splitlines() if x.strip()), "")
    return data.clean(line).strip()[:limit]


# ---------------------------------------------------------------- folders

def projects_dir():
    return Path(data.default_paths()["projects"])


@dataclass(frozen=True)
class Folder:
    name: str
    purpose: str
    shown: str              # how the docs name it
    path: object            # a function returning the Path
    create: bool = True     # install and doctor --fix may create it
    kind: str = "folder"
    legacy: tuple = ()

    def install(self, ctx):
        p = self.path()
        if p.is_dir() or not self.create:
            return True
        if ctx.dry_run:
            ctx.say("  would create %s" % _home(p))
            return True
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            ctx.say("  could not create %s: %s" % (_home(p), _first_line(e)))
            return False
        ctx.say("  created %s" % _home(p))
        return True

    def check(self, ctx):
        p = self.path()
        if not p.is_dir():
            if self.create:
                return [Check(self.name, "exists", FAIL, "%s is missing" % _home(p), "fleetwatch doctor --fix")]
            return [Check(self.name, "exists", WARN, "%s is missing" % _home(p))]
        if self.create and not os.access(p, os.W_OK):
            return [Check(self.name, "writable", FAIL, "%s cannot be written" % _home(p))]
        return [Check(self.name, "exists", OK, _home(p))]


# ---------------------------------------------------------------- tools

@dataclass(frozen=True)
class Tool:
    name: str
    purpose: str
    without: str            # what still works when it is missing
    platforms: tuple = ()   # () is every platform
    kind: str = "tool"
    legacy: tuple = ()

    def applies(self, platform=None):
        return not self.platforms or (platform or sys.platform) in self.platforms

    def install(self, ctx):
        return True         # fleetwatch never installs another program

    def check(self, ctx):
        if not self.applies(ctx.manager.platform):
            return []
        found = shutil.which(self.name)
        if found:
            return [Check(self.name, "on the PATH", OK, _home(found))]
        return [Check(self.name, "on the PATH", WARN, "not found; %s" % self.without)]


# ---------------------------------------------------------------- scheduled jobs

@dataclass(frozen=True)
class Job:
    """A scheduled job. `files` are the files install writes (a plist, or systemd units), `unit` is the one
    systemd enables, and `write` writes them. A job with no interval is kept running instead."""
    name: str
    purpose: str
    label: str
    unit: str
    interval_s: int         # None: kept running (restarted when it stops)
    command: str            # what it runs, for the docs
    own_install: str        # the job's own install command
    write: object
    files: object
    expected_plist: object  # the plist bytes install would write (macOS), for a dry run
    last_run: object        # a function returning epoch seconds of the last run, or None
    legacy: tuple = ()
    kind: str = "job"
    # The job's own install and uninstall (each takes `out`, returns an exit code) for a live launchd. Its
    # install loads the job and retires the older jobs it replaces itself, only once the job is loaded.
    own_live_install: object = None
    own_live_uninstall: object = None
    checks_last_run: bool = True    # False: its own check reports how long ago it ran

    def live_own(self, manager):
        return manager.darwin and manager.available and self.own_live_install is not None

    def manager_label(self, manager):
        return self.label if manager.darwin else self.unit

    def _files(self, manager):
        return self.files(manager.darwin)

    def installed(self, manager):
        return all(p.exists() for p in self._files(manager))

    # -- install

    def install(self, ctx):
        m = ctx.manager
        paths = self._files(m)
        before = {p: _read(p) for p in paths}
        label = self.manager_label(m)
        if ctx.dry_run:
            if m.darwin:
                same = before[paths[0]] == self.expected_plist()
                ctx.say("  %s %s" % ("unchanged:" if same else "would write", _home(paths[0])))
            else:
                ctx.say("  would write %s" % ", ".join(_home(p) for p in paths))
                same = False
            loaded = m.loaded(label)
            ctx.say("  " + ("would load %s" % label if loaded is False else
                            "%s is loaded%s" % (label, "" if same else "; would reload it") if loaded else
                            "would load %s (%s cannot be asked whether it is loaded)" % (label, m.name)))
            return True
        if self.live_own(m):
            return self._own_install(ctx, paths, before)
        buf = io.StringIO()
        self.write(buf)
        missing = [p for p in paths if not p.exists()]
        if missing:
            ctx.say("  could not write %s" % ", ".join(_home(p) for p in missing))
            for line in buf.getvalue().splitlines():
                if line.startswith(("could not", "or use cron")):
                    ctx.say("    " + line)
            return False
        changed = any(_read(p) != before[p] for p in paths)
        ctx.say("  %s %s" % ("wrote" if changed else "unchanged:", ", ".join(_home(p) for p in paths)))
        loaded = m.loaded(label)
        if loaded is True and not changed:
            ctx.say("  %s is loaded" % label)
            return True
        if loaded is None:
            ctx.say("  not started: %s cannot be asked whether it is loaded (%s)" % (m.name, m.why_not()))
            for line in buf.getvalue().splitlines():
                if line.startswith(("to start it", "or use cron")):
                    ctx.say("    " + line)
            return True
        res = m.load(label)
        if res.ok:
            ctx.say("  %s %s" % ("reloaded" if loaded else "loaded", label))
            return True
        ctx.say("  could not start %s: %s" % (label, res.detail))
        return False

    def _own_install(self, ctx, paths, before):
        """The job's own install, run only when there is something for it to do, so running install again
        reloads nothing."""
        m = ctx.manager
        current = before[paths[0]] == self.expected_plist()
        if current and m.loaded(self.label) and not any(legacy_state(x, m) != (False, None) for x in self.legacy):
            ctx.say("  unchanged: %s" % _home(paths[0]))
            ctx.say("  %s is loaded" % self.label)
            return True
        buf = io.StringIO()
        code = self.own_live_install(buf)
        _relay(ctx, buf)
        return code == 0

    def restart(self, ctx):
        label = self.manager_label(ctx.manager)
        res = ctx.manager.restart(label)
        ctx.say("  %s %s%s" % ("restarted" if res.ok else "could not restart", label, "" if res.ok else ": " + res.detail))
        return res.ok

    def uninstall(self, ctx):
        m = ctx.manager
        label = self.manager_label(m)
        if self.live_own(m) and self.own_live_uninstall is not None and not ctx.dry_run:
            buf = io.StringIO()
            code = self.own_live_uninstall(buf)
            _relay(ctx, buf)
            return code == 0
        ok = True
        loaded = m.loaded(label)
        if loaded:
            if ctx.dry_run:
                ctx.say("  would unload %s" % label)
            else:
                res = m.unload(label)
                if res.ok:
                    ctx.say("  unloaded %s" % label)
                else:
                    # A loaded job whose file is gone can no longer be reloaded or inspected from it,
                    # so its file stays until it unloads.
                    ctx.say("  could not unload %s: %s" % (label, res.detail))
                    for p in self._files(m):
                        if p.exists():
                            ctx.say("  kept %s while %s is loaded" % (_home(p), label))
                    return False
        elif loaded is None and self.installed(m):
            ctx.say("  %s cannot be asked whether %s is loaded (%s); if it is: %s"
                    % (m.name, label, m.why_not(), _unload_hint(m, label)))
        for p in self._files(m):
            if not p.exists():
                continue
            if ctx.dry_run:
                ctx.say("  would remove %s" % _home(p))
                continue
            try:
                p.unlink()
                ctx.say("  removed %s" % _home(p))
            except OSError as e:
                ok = False
                ctx.say("  could not remove %s: %s" % (_home(p), _first_line(e)))
        return ok

    # -- check

    def check(self, ctx):
        m = ctx.manager
        label = self.manager_label(m)
        out = []
        if not self.installed(m):
            out.append(Check(self.name, "installed", FAIL, "not installed", "fleetwatch install"))
        else:
            out.append(Check(self.name, "installed", OK, _home(self._files(m)[0])))
            loaded = m.loaded(label)
            if loaded is None:
                out.append(Check(self.name, "loaded", WARN, "%s cannot be asked (%s)" % (m.name, m.why_not())))
            elif not loaded:
                out.append(Check(self.name, "loaded", FAIL, "installed but not loaded", "fleetwatch install"))
            elif self.interval_s is None:
                running = m.running(label)
                out.append(Check(self.name, "running", OK if running else FAIL,
                                 "running" if running else "loaded but not running",
                                 "" if running else "fleetwatch doctor --fix (restarts it)"))
            else:
                out.append(Check(self.name, "loaded", OK, label))
            if self.interval_s is not None and self.checks_last_run:
                out.append(self._recent(ctx))
        if m.darwin:
            out += [legacy_check(self.name, old, ctx) for old in self.legacy]
        return out

    def _recent(self, ctx):
        now = ctx.time()
        last = self.last_run()
        every = "every %s" % data.ago(self.interval_s)
        limit = STALE_FACTOR * self.interval_s
        if last is None:
            since = _mtime(self._files(ctx.manager)[0])
            if since is not None and now - since < limit:
                return Check(self.name, "last run", OK, "no run yet; installed %s ago, runs %s"
                             % (data.ago(now - since), every))
            return Check(self.name, "last run", FAIL, "no run recorded (runs %s)" % every,
                         "see its log in %s" % _home(settings.state_dir()))
        age = max(now - last, 0)
        if age > limit:
            return Check(self.name, "last run", FAIL, "last ran %s ago (runs %s)" % (data.ago(age), every),
                         "see its log in %s" % _home(settings.state_dir()))
        return Check(self.name, "last run", OK, "%s ago" % data.ago(age))


def _read(path):
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _relay(ctx, buf):
    for line in buf.getvalue().splitlines():
        if line.strip():
            ctx.say("  " + line)


def _unload_hint(m, label):
    return ("launchctl bootout gui/$(id -u)/%s" % label) if m.darwin else ("systemctl --user disable --now %s" % label)


def _log_mtime(job):
    """When a job last wrote to its log in the state folder: each run of the index refresh and the watch
    run prints a line, so the newer of its .log and .err is when it last ran."""
    def last():
        name = job.unit[len("fleetwatch-"):]
        times = [t for t in (_mtime(Path(settings.state_dir()) / (name + ext)) for ext in (".log", ".err"))
                 if t is not None]
        return max(times) if times else None
    return last


def _schedule_job(job, name, purpose, legacy=(), last_run=None, **own):
    return Job(
        name=name, purpose=purpose, label=job.label, unit=job.unit + ".timer", interval_s=job.interval_s,
        command="fleetwatch " + " ".join(job.args), own_install=job.usage + " install",
        write=lambda out, job=job: schedule.install(out, job),
        files=lambda darwin, job=job: [schedule._plist_path(job)] if darwin else schedule._units(job),
        expected_plist=lambda job=job: _plist_bytes(schedule.plist_dict(settings.state_dir(), job)),
        last_run=last_run or _log_mtime(job), legacy=legacy, **own)


def _plist_bytes(d):
    import plistlib
    return plistlib.dumps(d)


INDEX_LEGACY = ("local.search-indexes.refresh",)

JOBS = (
    _schedule_job(schedule.INDEX, "index refresh", "refreshes every registered session index",
                  legacy=INDEX_LEGACY),
    _schedule_job(schedule.WATCH, "watch run", "checks GitHub for stopped sessions' watches"),
    Job(name="notify", purpose="desktop notifications when something new needs you",
        label=notify_schedule.LABEL, unit=notify_schedule.SERVICE, interval_s=None,
        command="fleetwatch notify", own_install="fleetwatch notify schedule install",
        write=lambda out: notify_schedule.install(out),
        files=lambda darwin: [notify_schedule._plist_path()] if darwin else [notify_schedule._unit_path()],
        expected_plist=lambda: _plist_bytes(notify_schedule.plist_dict(settings.state_dir())),
        last_run=lambda: None),
    _schedule_job(ARCHIVE, "archive", "copies Claude Code transcripts into the archive before the retention "
                  "sweep removes them", legacy=(archive.LEGACY_LABEL,), last_run=lambda: None,
                  own_live_install=lambda out: archive.install(out),
                  own_live_uninstall=lambda out: archive.uninstall(out), checks_last_run=False),
)

FOLDERS = (
    Folder("data folder", "indexes and fleetwatch's own data", "$XDG_DATA_HOME/fleetwatch, else "
           "~/.local/share/fleetwatch", lambda: Path(settings.data_dir())),
    Folder("state folder", "job logs, notification state, the archive's last run, retired job backups",
           "$XDG_STATE_HOME/fleetwatch, else ~/.local/state/fleetwatch", lambda: Path(settings.state_dir())),
    Folder("session archive", "transcript copies that outlive Claude Code's retention sweep",
           "CLAUDE_SESSION_ARCHIVE_DEST, else settings.toml [archive] dest, else "
           "~/.local/share/claude-session-archive/archive", archive_root),
    Folder("Claude projects folder", "the transcripts fleetwatch reads (Claude Code writes them)",
           "FLEETWATCH_PROJECTS_DIR, else ~/.claude/projects", projects_dir, create=False),
)

TOOLS = (
    Tool("claude", "resuming a session; the session tagger during an index refresh",
         "Resume cannot reopen a session, and a refresh needs --no-tag"),
    Tool("gh", "watches (read-only GitHub calls with your login)", "watches report a lookup error"),
    Tool("rsync", "the archive copy", "the archive copies with Python instead"),
    Tool("osascript", "iTerm2 jumps and notification banners", "Enter cannot reach iTerm2 and no banner shows",
         platforms=("darwin",)),
    Tool("terminal-notifier", "a click on a notification raising its session",
         "banners still show through osascript; a click does nothing", platforms=("darwin",)),
    Tool("notify-send", "desktop notifications", "notifications print one line instead", platforms=("linux",)),
    Tool("pdftotext", "reading PDF files in reference folders", "those files are skipped"),
    Tool("pandoc", "reading Office files in reference folders", "those files are skipped"),
    Tool("soffice", "reading older Office files in reference folders", "those files are skipped"),
    Tool("tesseract", "reading scanned files in reference folders", "those files are skipped"),
)

INVENTORY = JOBS + FOLDERS + TOOLS


def legacy_labels():
    return tuple(label for job in JOBS for label in job.legacy)


# ---------------------------------------------------------------- legacy jobs

def legacy_plist(label):
    return schedule.launch_agents_dir() / (label + ".plist")


def backup_dir():
    return schedule.legacy_backup_dir()


def legacy_state(label, manager):
    """(loaded, plist path or None): whether an older job is loaded, and its plist when one is installed."""
    plist = legacy_plist(label)
    return manager.loaded(label), (plist if plist.exists() else None)


def retire(label, ctx):
    """Retire an older job through schedule.retire_legacy: unloaded, its plist moved to the backup folder in
    fleetwatch's state folder, never deleted. Returns False when a step failed or could not run."""
    m = ctx.manager
    if not m.darwin:
        return True
    if not m.available:
        if legacy_plist(label).exists():
            ctx.say("  not retired: %s (%s); its plist stays where it is" % (label, m.why_not()))
        return True
    buf = io.StringIO()
    ok, _actions = m.retire(label, buf, ctx.dry_run)
    _relay(ctx, buf)
    return ok


def legacy_check(item, label, ctx):
    loaded, plist = legacy_state(label, ctx.manager)
    if loaded:
        return Check(item, "no older job", FAIL, "the older job %s is still loaded and does the same work" % label,
                     "fleetwatch doctor --fix (unloads it and keeps its plist)")
    if plist is not None:
        return Check(item, "no older job", FAIL, "the older job %s is installed and loads at the next login" % label,
                     "fleetwatch doctor --fix (moves its plist to the backup folder)")
    return Check(item, "no older job", OK, "%s is retired" % label)


def duplicate_checks(ctx):
    """Two loaded launchd jobs that run the same command (fleetwatch's own, or any job in the LaunchAgents
    folder that runs fleetwatch) do the same work twice. Reported, never repaired: which to keep is yours."""
    m = ctx.manager
    if not m.darwin:
        return []
    loaded = m.loaded_labels()
    if loaded is None:
        return [Check("scheduled jobs", "no duplicates", WARN, "launchd cannot be asked (%s)" % m.why_not())]
    by_sig = {}
    for label, path in services.agent_plists().items():
        if label not in loaded:
            continue
        sig = services.signature(services.plist_args(path))
        if sig and sig[0] == "fleetwatch":
            by_sig.setdefault(sig, []).append(label)
    out = []
    for sig, labels in sorted(by_sig.items()):
        if len(labels) > 1:
            out.append(Check("scheduled jobs", "no duplicates", FAIL, "%s all run `%s`"
                             % (", ".join(sorted(labels)), " ".join(sig)), "unload all but one"))
    return out or [Check("scheduled jobs", "no duplicates", OK, "no two loaded jobs run the same command")]


# ---------------------------------------------------------------- the archive and the indexes

def archive_checks(ctx):
    """archive.check(): a run that never happened, failed or is stale, and live transcripts the last run
    should have copied. A job installed less than two intervals ago that has not run yet is not a problem."""
    res = archive.check(now=ctx.time())
    st = res.get("status") or {}
    problems = list(res.get("problems") or [])
    if st.get("ended") is None:
        since = _mtime(schedule._plist_path(ARCHIVE) if ctx.manager.darwin else schedule._units(ARCHIVE)[-1])
        if since is not None and ctx.time() - since < STALE_FACTOR * ARCHIVE.interval_s:
            return [Check("session archive", "last run", OK, "no run yet; installed %s ago, runs every %s"
                          % (data.ago(ctx.time() - since), data.ago(ARCHIVE.interval_s)))]
    if problems:
        return [Check("session archive", "archive check", FAIL, _first_line(p), "fleetwatch archive status")
                for p in problems]
    cov = st.get("coverage") or {}
    waiting = cov.get("missing") or 0
    return [Check("session archive", "archive check", OK, "last run %s ago; every transcript older than it is "
                  "archived%s" % (data.ago(st.get("age_s") or 0),
                                  "; %d newer waiting for the next run" % waiting if waiting else ""))]


def _index_error_line(root, name):
    try:
        info = json.loads(Path(root, name, "state", "last_run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return ""
    return _first_line(info.get("error")) if isinstance(info, dict) else ""


def index_checks(ctx):
    """One row per index whose failure streak is past the alert limits, with its last error's first line."""
    from . import index_health
    res = index_health.load()
    if res.note and not res.rows:
        status = OK if res.note.startswith(index_health.NO_INDEXES) else WARN
        return [Check("indexes", "failure streaks", status, res.note)]
    try:
        from .memory import refresh
        root = refresh.ROOT
    except ImportError:
        root = None
    now = ctx.time()
    out = []
    for h in res.rows:
        streak = h.streak_problem(now)
        if not streak:
            continue
        error = _index_error_line(root, h.name) if root is not None else ""
        out.append(Check("index " + h.name, "failure streak", FAIL, streak + (": " + error if error else ""),
                         "fleetwatch index status %s" % h.name))
    runs, hours = res.rows[0].streak_limits if res.rows else index_health.streak_limits()
    return out or [Check("indexes", "failure streaks", OK, "none at %d runs or %s" % (runs, data.ago(hours * 3600)))]


# ---------------------------------------------------------------- the docs page

def markdown():
    """docs/dependencies.md."""
    from . import index_health
    lines = ["# What fleetwatch depends on", "",
             "Generated from `fleetwatch/dependencies.py` by `fleetwatch doctor --inventory`; a test keeps them "
             "in step.", "",
             "`fleetwatch install` installs and starts every scheduled job and creates the folders, and retires "
             "the older jobs each one replaces once its replacement is loaded: it unloads them and moves their "
             "plist into `legacy/` in the state folder, never deleting it. `fleetwatch uninstall` stops and removes the jobs. "
             "`fleetwatch doctor` checks every row below and exits 1 when something is wrong; "
             "`fleetwatch doctor --fix` installs what is missing and retires older jobs, and reports the rest.",
             "", "## Scheduled jobs", "",
             "| Job | What it does | Runs | launchd label (macOS) | systemd unit | Its own install | Replaces |",
             "|---|---|---|---|---|---|---|"]
    for j in JOBS:
        runs = "kept running" if j.interval_s is None else "every %d minutes" % (j.interval_s // 60)
        lines.append("| %s | %s (`%s`) | %s | `%s` | `%s` | `%s` | %s |" % (
            j.name, j.purpose, j.command, runs, j.label, j.unit, j.own_install,
            ", ".join("`%s`" % x for x in j.legacy) or ""))
    lines += ["", "Doctor checks each job: installed, loaded (a kept-running job: running), the last run no older "
              "than %d times its interval, no older job it replaces still installed or loaded, and no two loaded "
              "jobs running the same fleetwatch command." % STALE_FACTOR, "",
              "## Folders", "", "| Folder | What it holds | Where | Created by install |", "|---|---|---|---|"]
    for f in FOLDERS:
        lines.append("| %s | %s | %s | %s |" % (f.name, f.purpose, f.shown, "yes" if f.create else "no (read only)"))
    lines += ["", "Doctor checks that each folder exists and that fleetwatch can write the ones it creates.", "",
              "## Programs", "", "All optional: a missing one is a warning, and doctor still exits 0.", "",
              "| Program | Used for | Without it | Platforms |", "|---|---|---|---|"]
    for t in TOOLS:
        lines.append("| `%s` | %s | %s | %s |" % (t.name, t.purpose, t.without,
                                                   ", ".join("macOS" if p == "darwin" else "Linux" for p in t.platforms)
                                                   or "all"))
    lines += ["", "## Other checks", "",
              "- **The archive:** `fleetwatch.archive.check()`: the last archive run (from `archive/last_run.json` "
              "in the state folder) never happened, failed or is older than twice its interval, or a live "
              "transcript last written before the last run started has no archive copy. A job installed less "
              "than two intervals ago that has not run yet is fine.",
              "- **Indexes:** an index whose scheduled refresh has failed %d runs in a row, or for %d hours, is "
              "named with its last error's first line. `[alerts] index_failure_runs` and `index_failure_hours` in "
              "settings.toml change the limits. The same streak shows on the board's Needs you tab and is sent "
              "once per streak by `fleetwatch notify`." % (index_health.STREAK_RUNS, index_health.STREAK_HOURS), ""]
    return "\n".join(lines)
