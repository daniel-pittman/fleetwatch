"""Index health for the board's Indexes tab: numbers and statuses only, read-only.

The board runs in a plain terminal, which the trust guard treats as the owner, so what keeps it safe
is what this module returns: an index's name and group (registry metadata), its kind, times, counts
and a status from a fixed vocabulary. Never corpus text, probe text, tags, paths or the text of an
error. Nothing here writes to an index or starts a refresh. The protection check builds a Scanner,
so it runs once per index and is cached until the allowlist, the denylist or the registry changes.
"""
import contextlib
import io
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import data, schedule

NO_SCHEDULE_STALE_S = 2 * 86400
STALE_FACTOR = 2

# The only words a failure or a refusal is ever shown as.
FAIL_CATEGORIES = (("build", "build"), ("lock", "locked"), ("timed out", "timeout"), ("timeout", "timeout"),
                   ("memory", "memory"), ("allowlist", "privacy settings"), ("denylist", "privacy settings"),
                   ("person_names", "privacy settings"), ("protected", "privacy settings"),
                   ("rule pack", "privacy settings"), ("settings", "settings"))
FAIL_OTHER = "error"
REFUSED_SETTINGS, REFUSED_SCANNER = "settings", "scanner"
MISSING_EXTRA = "Install fleetwatch[memory] to see index health here."
NO_INDEXES = "No indexes are registered"


@dataclass(frozen=True)
class Health:
    name: str
    group: str
    kind: str                      # "session" or "plain"
    state: str                     # "ok", "failed" or "never"
    reason: str = ""               # a FAIL_CATEGORIES word when failed, else ""
    finished: float = None         # epoch seconds of the last refresh
    stale_after: float = None      # seconds without a successful refresh that count as stale
    protection: str = None         # None (not protected), "ok", "refused" or "plain" (no scanner applies)
    refusal: str = ""              # REFUSED_SETTINGS or REFUSED_SCANNER when refused
    exchanges: int = None
    files: int = None
    scheduled: bool = False
    owner: str = ""                # the registry's owner_session name ("" when none is recorded)
    owner_id: str = ""             # the registry's owner_session_id
    refresh_state: str = ""        # "refreshing" (its lock is fresh) or "waiting" (stale, a run is in progress)
    covered: float = None          # epoch seconds the last completed `refresh --all` run ended, when it finished this index

    def age(self, now):
        """Seconds since the index was last known current: its own last refresh, or the end of the last
        completed `refresh --all` run that finished it, whichever is later."""
        if self.finished is None:
            return None
        return max(now - max(self.finished, self.covered or 0), 0)

    def is_stale(self, now):
        age = self.age(now)
        return self.state == "ok" and age is not None and self.stale_after is not None and age > self.stale_after

    def problems(self, now):
        """Short words for what is wrong, empty when the index is healthy. A never-run index is new, not broken."""
        out = []
        if self.state == "failed" and self.refresh_state != "refreshing":
            out.append("last refresh failed (%s)" % (self.reason or FAIL_OTHER))
        if self.protection == "refused":
            out.append("protection check refused (%s)" % (self.refusal or REFUSED_SETTINGS))
        if self.is_stale(now) and not self.refresh_state:
            out.append("stale")
        return out

    def next_steps(self, now):
        """One plain sentence per problem saying what to do. Commands and setting names only."""
        out = []
        for problem in self.problems(now):
            if problem.startswith("last refresh failed"):
                out.append("details: fleetwatch index status %s, and the failures log (logs/failures.log in the "
                           "index data folder)" % self.name)
            elif problem.startswith("protection"):
                what = ("the privacy scanner could not be built from the settings"
                        if self.refusal == REFUSED_SCANNER else
                        "the allowlist or denylist settings do not meet what a protected group requires")
                out.append("%s; fleetwatch index verify %s names the setting to change" % (what, self.name))
            else:
                if not self.scheduled:
                    out.append("the scheduled refresh is not installed; run fleetwatch index schedule install")
                else:
                    out.append("last successful refresh %s ago; check fleetwatch index schedule status"
                               % data.ago(self.age(now)))
        if self.state == "never" and not self.refresh_state:
            out.append("not refreshed yet; run fleetwatch index refresh %s" % self.name)
        return out

    def next_commands(self, now):
        """The command to run for each problem, in the order next_steps explains them. The board shows
        these alone on one line, and y copies the first."""
        out = []
        for problem in self.problems(now):
            if problem.startswith("last refresh failed"):
                out.append("fleetwatch index status %s" % self.name)
            elif problem.startswith("protection"):
                out.append("fleetwatch index verify %s" % self.name)
            elif not self.scheduled:
                out.append("fleetwatch index schedule install")
            else:
                out.append("fleetwatch index schedule status")
        if self.state == "never" and not self.refresh_state:
            out.append("fleetwatch index refresh %s" % self.name)
        return out


@dataclass
class Result:
    rows: list = field(default_factory=list)
    note: str = ""                 # one line shown in place of the rows


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def fail_category(error):
    text = str(error or "").lower()
    for word, category in FAIL_CATEGORIES:
        if word in text:
            return category
    return FAIL_OTHER


def scheduled_now():
    """Whether the refresh schedule's file is installed (it is not asked whether it is loaded)."""
    try:
        if sys.platform == "darwin":
            return schedule._plist_path().exists()
        return (schedule.systemd_dir() / "fleetwatch-index-refresh.timer").exists()
    except OSError:
        return False


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


class Cache:
    """Per-index results that are costly to compute: the protection check and the corpus file count."""

    def __init__(self):
        self.protection = {}
        self.files = {}

    def clear(self):
        self.protection.clear()
        self.files.clear()


CACHE = Cache()


def _registry_stamp(mod):
    stamp = [_mtime(mod.REGISTRY), _mtime(mod.REGISTRY_DIR)]
    try:
        stamp += [_mtime(os.path.join(mod.REGISTRY_DIR, f)) for f in sorted(os.listdir(mod.REGISTRY_DIR))]
    except OSError:
        pass
    return tuple(stamp)


def _protection(mod, name, entry, cache):
    """(None, "") when the group is not protected, else ("ok", "") or ("refused", category)."""
    group = entry.get("group")
    if not mod.fleetwatch_settings.is_protected(group, mod.SETTINGS.protected_groups):
        return None, ""
    if entry.get("kind") == "plain":
        return "plain", ""         # its own scripts build the corpus; there is no transcript scanner to check
    priv = entry.get("privacy") or {}
    root = mod.CONFIG_ROOT
    allow = root / str(priv.get("allowlist", ""))
    deny = root / str(priv["denylist"]) if priv.get("denylist") else None
    key = (group, _mtime(allow), _mtime(deny) if deny else None, _registry_stamp(mod),
           json.dumps(entry.get("doc_options"), sort_keys=True, default=str))
    hit = cache.protection.get(name)
    if hit and hit[0] == key:
        return hit[1]
    try:
        scanner = mod.Scanner(allow, deny, mod.RuleSet(mod.SETTINGS.rule_packs))
        problems = mod.protection_problems(group, scanner, doc_options=entry.get("doc_options"))
        verdict = ("refused", REFUSED_SETTINGS) if problems else ("ok", "")
    except Exception:
        verdict = ("refused", REFUSED_SCANNER)
    cache.protection[name] = (key, verdict)
    return verdict


def _corpus_files(root, name, stamp, cache):
    hit = cache.files.get(name)
    if hit and hit[0] == stamp:
        return hit[1]
    count = 0
    for _dir, _subs, files in os.walk(os.path.join(root, name, "corpus")):
        count += len(files)
    cache.files[name] = (stamp, count)
    return count


def _lock_age(root, name):
    """Seconds since the index's refresh lock was last touched, None when it holds none."""
    mtime = _mtime(os.path.join(root, name, "state", "refresh.lock.d"))
    return None if mtime is None else max(time.time() - mtime / 1e9, 0)


RUN_MARKER = os.path.join("logs", "refresh_run.json")


@dataclass(frozen=True)
class Run:
    """The last `refresh --all` run, from the marker the refresh writes: times and names only."""
    started: float
    ended: float = None            # None while the run is going (or when it died)
    finished: tuple = ()           # the index names the run finished
    live: bool = False             # still going: no end yet and the marker was written within the lock window


def _run_marker(root, lock_stale_s):
    path = os.path.join(root, RUN_MARKER)
    try:
        info = json.loads(Path(path).read_text(encoding="utf-8"))
        started = float(info["started"])
        ended = None if info.get("ended") is None else float(info["ended"])
        names = tuple(n for n in (info.get("finished") or ()) if isinstance(n, str))
        touched = os.stat(path).st_mtime
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return Run(started, ended, names, ended is None and time.time() - touched < lock_stale_s)


def _row(mod, name, entry, root, scheduled, interval_s, cache, run_active=False, run=None):
    protection, refusal = _protection(mod, name, entry, cache)
    path = os.path.join(root, name, "state", "last_run.json")
    state, reason, finished, exchanges = "never", "", None, None
    try:
        info = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(info, dict):
            raise ValueError(path)
        try:
            finished = datetime.fromisoformat(str(info.get("finished"))).timestamp()
        except ValueError:
            finished = None
        state = "ok" if info.get("ok") else "failed"
        reason = "" if state == "ok" else fail_category(info.get("error"))
        exchanges = _int(info.get("exchanges"))
    except (OSError, ValueError):
        pass
    files = _corpus_files(root, name, _mtime(path), cache) if state != "never" else None
    kind = "plain" if entry.get("kind") == "plain" else "session"
    lock = _lock_age(root, name)
    refresh_state = "refreshing" if lock is not None and lock < mod.LOCK_STALE_S else ""
    stale_after = interval_s * STALE_FACTOR if scheduled else NO_SCHEDULE_STALE_S
    # A run that finished this index vouches for it until it ended; a run that skipped it does not.
    covered = run.ended if run is not None and run.ended is not None and name in run.finished else None
    current = max(finished or 0, covered or 0) or None
    if not refresh_state and run_active and state != "never" and current is not None \
            and time.time() - current > stale_after:
        refresh_state = "waiting"
    return Health(data.clean(name), data.clean(entry.get("group") or "?"), kind, state, reason, finished,
                  stale_after, protection, refusal, exchanges, files, scheduled,
                  data.clean(str(entry.get("owner_session") or "")), data.clean(str(entry.get("owner_session_id") or "")),
                  refresh_state, covered)


def load(reg=None, root=None, scheduled=None, cache=None):
    """Every registered index's health, or a Result whose note says why there are none to show.

    `reg` and `root` default to the indexer's own registry and data folder. Reads only. What the
    registry loader prints about a broken entry is swallowed so it never lands on the board."""
    if os.environ.get("FLEETWATCH_DEMO"):
        from . import demo
        return Result(demo.index_rows())
    try:
        from .memory import refresh as mod
    except ImportError:
        return Result(note=MISSING_EXTRA)
    if mod.SETTINGS_ERROR is not None:
        return Result(note="The indexer settings do not load; run `fleetwatch index list` to see why.")
    cache = CACHE if cache is None else cache
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            reg = mod.load_registry() if reg is None else reg
    except Exception:
        return Result(note="The index registry could not be read.")
    root = mod.ROOT if root is None else root
    scheduled = scheduled_now() if scheduled is None else scheduled
    try:
        minutes = int((reg.get("defaults") or {}).get("schedule_minutes", 30))
    except (TypeError, ValueError):
        minutes = 30
    names = sorted((reg.get("indexes") or {}))
    # A scheduled `refresh --all` holds one index's lock at a time, so a fresh lock anywhere means a run is
    # in progress and an index it has not reached yet is waiting, not stale.
    ages = [_lock_age(str(root), n) for n in names]
    run = _run_marker(str(root), mod.LOCK_STALE_S)
    run_active = any(a is not None and a < mod.LOCK_STALE_S for a in ages) or (run is not None and run.live)
    rows = [_row(mod, name, entry, str(root), scheduled, max(minutes, 1) * 60, cache, run_active, run)
            for name, entry in sorted((reg.get("indexes") or {}).items())]
    return Result(rows, "" if rows else NO_INDEXES)


def problem_rows(rows, now=None):
    now = time.time() if now is None else now
    return [r for r in rows if r.problems(now)]
