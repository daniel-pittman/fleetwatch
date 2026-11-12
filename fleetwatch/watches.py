"""`fleetwatch watch`: standing GitHub checks a session declares in its status file, run while it is away.

A session lists watches in its status file (`fleetwatch write-status`, field `watches`), each one kind from a
fixed menu on one repo. `fleetwatch watch run` evaluates the watches of every session that is not active,
through read-only `gh api` calls, and keeps what it finds in one state file per session under fleetwatch's
state folder. The board reads those files and never calls GitHub. `fleetwatch watch report` and `ack` are
how the session picks the flags up when it is back.

Safety rule: the state holds item numbers, advisory ids, states, counts and times, never a title, body,
branch name, check name, login or message. That text is written by people outside the team and must not
reach the board, a prompt or a session.
"""
import argparse
import contextlib
import fcntl
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlencode

from . import config, data, settings

# kind -> the options it takes beyond kind and repo. New kinds are added here, in code, never by a session.
KINDS = {
    "external_activity": (),
    "pr_checks_failed": (),
    "pr_stale": ("days",),
    "security_advisory": (),
}
FORMAT = '{"kind": K, "repo": "owner/name"}'
EXAMPLE_REPO = "example-org/widget"
DEFAULT_DAYS = 3
MAX_DAYS = 60
MAX_WATCHES = 50
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
SID = re.compile(r"[A-Za-z0-9_-]{1,100}")
SHA = re.compile(r"[0-9a-f]{40}")
GHSA = re.compile(r"GHSA(-[23456789cfghjmpqrvwx]{4}){3}")
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
ISSUE_URL = re.compile(r"/(?:issues|pulls)/(\d+)$")

GH_TIMEOUT_S = 30
PER_PAGE = 100
MAX_PAGES = 3              # up to 300 items per listing per run
MAX_PRS = 30               # per-PR lookups stop after the 30 newest open PRs
ACTIVE_FACTOR = 1.5        # a session is active while its status is newer than 1.5 x its cadence
DEFAULT_CADENCE_MIN = 30
LOOKBACK_S = 14 * 86400    # external_activity never looks further back than this
STALE_RUN_S = 3600         # four missed 15-minute runs: the board says the watches are not running
MAX_SEEN = 2000
TEAM = {"admin", "maintain", "write"}
FAILED = {"failure", "cancelled", "timed_out", "startup_failure"}
# flag kind -> (one, many), in the order a row lists them
WHAT = {
    "issue": ("external issue", "external issues"),
    "pr": ("external PR", "external PRs"),
    "comment": ("external comment", "external comments"),
    "failed_check": ("failed check", "failed checks"),
    "stale_pr": ("stale PR", "stale PRs"),
    "dismissed": ("dismissed approval", "dismissed approvals"),
    "advisory": ("security report", "security reports"),
}
MAX_REFS = 5


class WatchError(ValueError):
    """A watches declaration that does not follow the format."""


# ---------------------------------------------------------------------------- the declaration


def valid_repo(repo):
    return isinstance(repo, str) and REPO.fullmatch(repo) is not None and repo.split("/")[1] not in (".", "..")


def validate(value):
    """The watches list as given, or WatchError with a message that teaches the format."""
    if not isinstance(value, list):
        raise WatchError("watches must be a list of %s objects" % FORMAT)
    if len(value) > MAX_WATCHES:
        raise WatchError("at most %d watches per session" % MAX_WATCHES)
    menu = ", ".join(KINDS)
    out, seen = [], set()
    for x in value:
        if not isinstance(x, dict):
            raise WatchError("bad watch: each entry is an object, %s" % FORMAT)
        if "kind" not in x:
            raise WatchError("bad watch: no kind; each entry is %s with kind one of %s" % (FORMAT, menu))
        kind = x["kind"]
        if not isinstance(kind, str) or kind not in KINDS:
            raise WatchError("bad watch: kind %r is not one of %s" % (data.clean(str(kind))[:40], menu))
        allowed = ("kind", "repo") + KINDS[kind]
        for key in sorted(set(x) - set(allowed), key=str):
            raise WatchError("bad watch: unknown key %r for %s (allowed: %s)"
                             % (data.clean(str(key))[:40], kind, ", ".join(allowed)))
        repo = x.get("repo")
        if not valid_repo(repo):
            raise WatchError("bad watch: repo %s must be owner/name, for example %s"
                             % (data.clean(repr(repo))[:60], EXAMPLE_REPO))
        if "days" in x:
            days = x["days"]
            if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
                raise WatchError("bad watch: days must be a whole number of working days from 1 to %d" % MAX_DAYS)
        ident = (kind, repo.lower())
        if ident in seen:
            raise WatchError("bad watch: %s on %s is listed twice" % ident)
        seen.add(ident)
        out.append(dict(x))
    return out


def watch_key(w):
    return "%s:%s" % (w["kind"], w["repo"])


# ---------------------------------------------------------------------------- state


def state_dir():
    return os.path.join(settings.state_dir(), "watches")


def state_path(sid):
    if not isinstance(sid, str) or not SID.fullmatch(sid):
        raise WatchError("not a usable session id")
    return os.path.join(state_dir(), sid + ".json")


def empty_state(sid):
    return {"version": 1, "sessionId": sid, "session": "", "owner_state": None, "checked": None, "watches": {}}


def load_state(sid):
    try:
        doc = data.load_json(state_path(sid))
    except (OSError, ValueError):
        return empty_state(sid)
    if not isinstance(doc, dict) or not isinstance(doc.get("watches"), dict):
        return empty_state(sid)
    return doc


def save_state(doc):
    config.atomic_write(state_path(doc["sessionId"]), json.dumps(doc, indent=1, sort_keys=True) + "\n")


@contextlib.contextmanager
def locked(name, block=True):
    """An exclusive lock on <state>/<name>.lock; yields False when block is False and another holds it."""
    os.makedirs(state_dir(), exist_ok=True)
    with open(os.path.join(state_dir(), name + ".lock"), "w", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True


def new_watch_state(w):
    return {"kind": w["kind"], "repo": w["repo"], "last_checked": None, "error": None, "available": True,
            "seen": [], "flags": []}


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def gh_time(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------- the hand-off


def live_sids(sessions_dir):
    """Session ids that have a registry file whose process is alive."""
    return {g["sessionId"] for g in data.read_registry(sessions_dir)
            if isinstance(g.get("sessionId"), str) and data.pid_alive(g.get("pid"))}


def owner_state(sf, live, parked, now):
    """"active" (the session keeps its own watches), else why fleetwatch covers them: parked, stopped or quiet."""
    sid = sf.get("sessionId")
    if sid in parked:
        return "parked"
    if sid not in live:
        return "stopped"
    cadence = sf.get("cadence_min")
    if isinstance(cadence, bool) or not isinstance(cadence, (int, float)) or cadence <= 0:
        cadence = DEFAULT_CADENCE_MIN
    updated = data.to_epoch(sf.get("updated"))
    if updated is not None and now - updated <= ACTIVE_FACTOR * cadence * 60:
        return "active"
    return "quiet"


# ---------------------------------------------------------------------------- gh


class GhError(Exception):
    """A failed lookup. reason is fixed wording (an HTTP code, "timed out"); never gh's own message."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Gh:
    """Read-only `gh api` GETs, memoised for one run so kinds that share a listing fetch it once."""

    def __init__(self, command=None):
        self.argv = shlex.split(command or os.environ.get("FLEETWATCH_GH_CMD") or "gh")
        self.cache = {}

    def get(self, path, **params):
        url = path + ("?" + urlencode(params, safe=":") if params else "")
        if url in self.cache:
            return self.cache[url]
        cmd = self.argv + ["api", "--method", "GET", "-H", "Accept: application/vnd.github+json", url]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=GH_TIMEOUT_S, stdin=subprocess.DEVNULL)
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            raise GhError("gh not found") from None
        except subprocess.TimeoutExpired:
            raise GhError("timed out") from None
        if p.returncode != 0:
            m = re.search(r"\(HTTP (\d{3})\)", p.stderr or "")
            if m:
                raise GhError("HTTP %s" % m.group(1))
            # gh exits 4 when it has no login for the host
            raise GhError("gh not logged in" if p.returncode == 4 else "gh exited %d" % p.returncode)
        try:
            body = json.loads(p.stdout)
        except ValueError:
            raise GhError("bad response") from None
        self.cache[url] = body
        return body

    def list(self, path, **params):
        out = []
        for page in range(1, MAX_PAGES + 1):
            body = self.get(path, per_page=PER_PAGE, page=page, **params)
            if not isinstance(body, list):
                raise GhError("bad response")
            out += body
            if len(body) < PER_PAGE:
                break
        return out


def _bad():
    return GhError("bad response")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _bad()
    return value


def _when(value):
    t = data.to_epoch(value) if isinstance(value, str) else None
    if t is None:
        raise _bad()
    return t


def _ref_of(item):
    """The issue or PR number a comment belongs to, from its API URL."""
    url = item.get("issue_url") or item.get("pull_request_url")
    m = ISSUE_URL.search(url) if isinstance(url, str) else None
    if m is None:
        raise _bad()
    return int(m.group(1))


def is_bot(u):
    """A bot account (dependabot, the workflow bots, review apps), or a deleted user with no account."""
    if not isinstance(u, dict) or not isinstance(u.get("login"), str):
        return True
    return u.get("type") == "Bot" or u["login"].endswith("[bot]")


def _dicts(items):
    if not all(isinstance(x, dict) for x in items):
        raise _bad()
    return items


# ---------------------------------------------------------------------------- the kinds


@dataclass
class Result:
    flags: list = field(default_factory=list)
    snapshot: bool = True      # flags is everything that holds now; else new events plus the refs to clear
    clear: set = field(default_factory=set)
    available: bool = True


@dataclass
class Context:
    now: float
    since: float
    team: dict                 # (repo, login) -> bool, shared by every watch in one run


def flag(what, ref, key):
    return {"what": what, "ref": ref, "key": key}


def on_team(gh, repo, login, ctx):
    """Whether login has write access to repo (admin, maintain or write); a failed lookup raises."""
    key = (repo.lower(), login)
    if key not in ctx.team:
        if not LOGIN.fullmatch(login):
            ctx.team[key] = False
        else:
            try:
                body = gh.get("repos/%s/collaborators/%s/permission" % (repo, login))
            except GhError as e:
                # GitHub does not know the login (deleted or renamed): it cannot be on the team. Raising
                # here would hold the window on this item at every run, so it is flagged as outside.
                if e.reason != "HTTP 404":
                    raise
                body = {}
            if not isinstance(body, dict):
                raise _bad()
            ctx.team[key] = body.get("permission") in TEAM or body.get("role_name") in TEAM
    return ctx.team[key]


def external_activity(gh, repo, opts, ctx):
    """Issues, PRs and comments opened since the last check by authors without write access."""
    since = gh_time(ctx.since)
    issues = _dicts(gh.list("repos/%s/issues" % repo, state="all", since=since, sort="updated", direction="desc"))
    # newest first: a listing stops after MAX_PAGES, and what it drops should be the oldest
    comments = _dicts(gh.list("repos/%s/issues/comments" % repo, since=since, sort="updated", direction="desc"))
    last_team, last_out, closed, found = {}, {}, set(), []
    events = [(it, _number(it.get("number")), "pr" if "pull_request" in it else "issue", None) for it in issues]
    events += [(c, _ref_of(c), "comment", _number(c.get("id"))) for c in comments]
    for item, n, what, cid in events:
        if what != "comment" and item.get("state") == "closed":
            closed.add(n)
        created = _when(item.get("created_at"))
        if created < ctx.since or is_bot(item.get("user")):
            continue
        if on_team(gh, repo, item["user"]["login"], ctx):
            last_team[n] = max(last_team.get(n, 0), created)
            continue
        last_out[n] = max(last_out.get(n, 0), created)
        found.append(flag(what, n, "comment:%d" % cid if cid else "%s:%d" % (what, n)))
    # An item is answered once it is closed or someone on the team spoke after the last outside word.
    clear = closed | {n for n, t in last_team.items() if t >= last_out.get(n, 0)}
    return Result([f for f in found if f["ref"] not in clear], snapshot=False, clear=clear)


def _repo_info(gh, repo):
    info = gh.get("repos/%s" % repo)
    if not isinstance(info, dict) or not isinstance(info.get("default_branch"), str):
        raise _bad()
    return info


def _open_pulls(gh, repo):
    return _dicts(gh.list("repos/%s/pulls" % repo, state="open"))[:MAX_PRS]


def _sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise _bad()
    return value


def _failed_runs(gh, repo, sha):
    body = gh.get("repos/%s/commits/%s/check-runs" % (repo, sha), per_page=PER_PAGE)
    runs = body.get("check_runs") if isinstance(body, dict) else None
    if not isinstance(runs, list):
        raise _bad()
    return sum(1 for r in runs if isinstance(r, dict) and r.get("conclusion") in FAILED)


def pr_checks_failed(gh, repo, opts, ctx):
    """A failed, cancelled or timed-out check run on an open PR's head or the default branch's latest commit."""
    branch = _repo_info(gh, repo)["default_branch"]
    found = []
    for p in _open_pulls(gh, repo):
        n, sha = _number(p.get("number")), _sha((p.get("head") or {}).get("sha"))
        if _failed_runs(gh, repo, sha):
            found.append(flag("failed_check", n, "check:%d@%s" % (n, sha)))
    head = gh.get("repos/%s/commits" % repo, sha=branch, per_page=1)
    if not isinstance(head, list) or not head or not isinstance(head[0], dict):
        raise _bad()
    sha = _sha(head[0].get("sha"))
    if _failed_runs(gh, repo, sha):
        found.append(flag("failed_check", None, "check:default@%s" % sha))
    return Result(found)


def working_days(start, end):
    """Monday-to-Friday days after start's date, up to and including end's date, in the local zone."""
    a = datetime.fromtimestamp(start, data.LOCAL_TZ).date()
    b = datetime.fromtimestamp(end, data.LOCAL_TZ).date()
    days = (b - a).days
    if days <= 0:
        return 0
    weeks, rest = divmod(days, 7)
    return weeks * 5 + sum(1 for i in range(1, rest + 1) if (a.weekday() + i) % 7 < 5)


def pr_stale(gh, repo, opts, ctx):
    """An open PR with no push, review or comment from a person for `days` working days, or a dismissed
    approval with no approval since. Drafts are never stale."""
    days = opts.get("days", DEFAULT_DAYS)
    # wide enough to hold `days` working days across weekends and a holiday or two
    since = gh_time(ctx.now - (2 * days + 4) * 86400)
    talk = {}
    for path in ("repos/%s/issues/comments", "repos/%s/pulls/comments"):
        for c in _dicts(gh.list(path % repo, since=since, sort="updated", direction="desc")):
            if not is_bot(c.get("user")):
                n = _ref_of(c)
                talk[n] = max(talk.get(n, 0), _when(c.get("created_at")))
    found = []
    for p in _open_pulls(gh, repo):
        n = _number(p.get("number"))
        reviews = _dicts(gh.list("repos/%s/pulls/%d/reviews" % (repo, n)))
        stamped = [(r, _when(r.get("submitted_at"))) for r in reviews if r.get("submitted_at")]
        approved = [t for r, t in stamped if r.get("state") == "APPROVED"]
        dismissed = [(t, _number(r.get("id"))) for r, t in stamped if r.get("state") == "DISMISSED"]
        if dismissed:
            t, rid = max(dismissed)
            if not any(a > t for a in approved):
                found.append(flag("dismissed", n, "dismissed:%d@%d" % (n, rid)))
        if p.get("draft"):
            continue
        times = [_when(p.get("created_at")), talk.get(n, 0)]
        times += [t for r, t in stamped if not is_bot(r.get("user"))]
        for c in _dicts(gh.list("repos/%s/pulls/%d/commits" % (repo, n))):
            when = ((c.get("commit") or {}).get("committer") or {}).get("date")
            if when:
                times.append(_when(when))
        last = max(times)
        if working_days(last, ctx.now) >= days:
            found.append(flag("stale_pr", n, "stale:%d@%d" % (n, int(last))))
    return Result(found)


def security_advisory(gh, repo, opts, ctx):
    """A repository security advisory in draft or a private vulnerability report waiting in triage. Only an
    admin of the repo can list those; for anyone else the watch is not available rather than quiet."""
    perms = _repo_info(gh, repo).get("permissions")
    if not isinstance(perms, dict) or perms.get("admin") is not True:
        return Result(available=False)
    try:
        # this listing pages by cursor, not by page number: one page of 100 is the budget
        body = gh.get("repos/%s/security-advisories" % repo, per_page=PER_PAGE)
    except GhError as e:
        if e.reason in ("HTTP 403", "HTTP 404"):
            return Result(available=False)
        raise
    if not isinstance(body, list):
        raise _bad()
    found = []
    for a in _dicts(body):
        ghsa = a.get("ghsa_id")
        if a.get("state") in ("triage", "draft") and isinstance(ghsa, str) and GHSA.fullmatch(ghsa):
            found.append(flag("advisory", ghsa, "advisory:" + ghsa))
    return Result(found)


CHECKS = {"external_activity": external_activity, "pr_checks_failed": pr_checks_failed,
          "pr_stale": pr_stale, "security_advisory": security_advisory}


# ---------------------------------------------------------------------------- a run


def merge(ws, result, now):
    """Fold a successful check into a watch's state: dedupe against what was already raised, clear what no
    longer holds."""
    ws.update(error=None, last_checked=iso(now))
    if not result.available:
        ws["available"] = False
        return 0
    ws["available"] = True
    old, seen = ws.get("flags") or [], ws.get("seen") or []
    if result.snapshot:
        holds = {f["key"] for f in result.flags}
        keep = [f for f in old if f["key"] in holds]
        # a condition that stopped holding is forgotten, so it is raised again if it comes back
        seen = [k for k in seen if k in holds]
    else:
        keep = [f for f in old if f["ref"] not in result.clear]
    known = set(seen) | {f["key"] for f in keep}
    new = []
    for f in result.flags:
        if f["key"] not in known:
            known.add(f["key"])
            new.append(f)
    ws["flags"] = keep + new
    ws["seen"] = (seen + [f["key"] for f in new])[-MAX_SEEN:]
    return len(new)


def evaluate(gh, w, ws, sf, now, team):
    """(Result, None) or (None, error text) for one watch."""
    last = data.to_epoch(ws.get("last_checked")) or 0
    # the session covered its own repos until it last wrote its status
    since = max(last, data.to_epoch(sf.get("updated")) or 0, now - LOOKBACK_S)
    try:
        return CHECKS[w["kind"]](gh, w["repo"], w, Context(now, since, team)), None
    except GhError as e:
        return None, "lookup failed (%s)" % e.reason


def _status_files(paths, out):
    warnings = []
    files = [sf for sf in data.read_status_files(paths["status"], warnings)
             if isinstance(sf.get("sessionId"), str) and SID.fullmatch(sf["sessionId"])]
    for w in warnings:
        print(w, file=out)
    return files


def _parked(parked_path, out):
    from . import park
    try:
        return set(park.load(parked_path))
    except park.ParkedError as e:
        print("warning: %s; parked sessions count as stopped" % e, file=out)
        return set()


def run(paths=None, now=None, out=None, parked_path=None, gh=None):
    """Evaluate the watches of every session that is not active. Prints one line per watch: counts only."""
    paths = paths or data.default_paths()
    out = out or sys.stdout
    with locked("run", block=False) as got:
        if not got:
            print("another watch run is in progress", file=out)
            return 0
        now = time.time() if now is None else now
        live, parked = live_sids(paths["sessions"]), _parked(parked_path, out)
        gh, team, declared = gh or Gh(), {}, set()
        for sf in _status_files(paths, out):
            sid, name = sf["sessionId"], data.clean(sf["session"]).strip()
            try:
                wl = validate(sf.get("watches") or [])
            except WatchError as e:
                print("%s: watches ignored, %s" % (name, e), file=out)
                wl = []
            if not wl:
                continue
            declared.add(sid)
            who = owner_state(sf, live, parked, now)
            before = load_state(sid)["watches"]
            results = {}
            if who == "active":
                print("%s: active, its watches are its own" % name, file=out)
            else:
                for w in wl:
                    results[watch_key(w)] = evaluate(gh, w, before.get(watch_key(w)) or {}, sf, now, team)
            # merged into a fresh read under the lock, so an ack made while the lookups ran is kept
            with locked(sid):
                doc = load_state(sid)
                doc.update(session=name, owner_state=who, checked=iso(now))
                doc["watches"] = {watch_key(w): doc["watches"].get(watch_key(w)) or new_watch_state(w) for w in wl}
                for key, (result, error) in results.items():
                    ws = doc["watches"][key]
                    if error:
                        ws["error"] = error
                        print("%s · %s · watch error: %s" % (name, key, error), file=out)
                    else:
                        n_new = merge(ws, result, now)
                        state = "not available" if not ws["available"] else "%d open, %d new" % (len(ws["flags"]), n_new)
                        print("%s · %s · %s" % (name, key, state), file=out)
                save_state(doc)
        # a session that no longer declares watches keeps no flags on the board
        for path in sorted(glob.glob(os.path.join(glob.escape(state_dir()), "*.json"))):
            sid = os.path.basename(path)[:-5]
            if sid not in declared and SID.fullmatch(sid):
                with locked(sid):
                    doc = load_state(sid)
                    if doc["watches"]:
                        doc.update(watches={}, checked=iso(now))
                        save_state(doc)
    return 0


# ---------------------------------------------------------------------------- reading the state


def _ref_text(ref):
    return "default branch" if ref is None else "#%d" % ref if isinstance(ref, int) else str(ref)


def summary(flags):
    """["2 external issues (#12, #15)", ...] in the fixed order of WHAT. Numbers and ids only."""
    out = []
    for what, (one, many) in WHAT.items():
        mine = [f for f in flags if f.get("what") == what]
        if not mine:
            continue
        refs = []
        for f in mine:
            if f.get("ref") not in refs:
                refs.append(f.get("ref"))
        refs.sort(key=lambda r: (r is not None, isinstance(r, str), r if r is not None else 0))
        shown = ", ".join(_ref_text(r) for r in refs[:MAX_REFS]) + (", …" if len(refs) > MAX_REFS else "")
        out.append("%d %s (%s)" % (len(mine), one if len(mine) == 1 else many, shown))
    return out


def _safe_flags(ws):
    """Only flags whose fields have the stored shape: a hand-edited file cannot put text on the board."""
    good = []
    for f in ws.get("flags") or []:
        if not isinstance(f, dict) or f.get("what") not in WHAT:
            continue
        ref = f.get("ref")
        if ref is None or (isinstance(ref, int) and not isinstance(ref, bool)) or (
                isinstance(ref, str) and GHSA.fullmatch(ref)):
            good.append(f)
    return good


ERROR_TEXT = re.compile(r"lookup failed \((HTTP \d{3}|timed out|gh not found|gh not logged in|bad response|"
                        r"gh exited \d+)\)")


@dataclass
class BoardRow:
    key: str
    sid: str
    owner: str
    owner_state: str
    repo: str = None           # None on a row about the run itself
    parts: list = field(default_factory=list)
    error: str = None
    kind: str = None
    checked: float = None


def board_rows(now=None):
    """The Watches section's rows from the state files: one per session and repo with something flagged, one
    per watch in error, and one per session whose watches have not run for an hour. Reads only."""
    now = time.time() if now is None else now
    rows = []
    for path in sorted(glob.glob(os.path.join(glob.escape(state_dir()), "*.json"))):
        try:
            doc = data.load_json(path)
        except (OSError, ValueError):
            continue
        sid = doc.get("sessionId") if isinstance(doc, dict) else None
        wmap = doc.get("watches") if isinstance(doc, dict) else None
        if not isinstance(sid, str) or not SID.fullmatch(sid) or not isinstance(wmap, dict) or not wmap:
            continue
        owner = data.clean(doc.get("session") or sid).strip()[:80]
        who = doc.get("owner_state") if doc.get("owner_state") in ("stopped", "quiet", "parked", "active") else "stopped"
        checked = data.to_epoch(doc.get("checked"))
        if checked is None or now - checked > STALE_RUN_S:
            age = "yet" if checked is None else "for %s" % data.human(now - checked)
            rows.append(BoardRow("watch-stale:" + sid, sid, owner, who, error="not checked %s; is the schedule "
                                 "running? see fleetwatch watch schedule status" % age, checked=checked))
        by_repo = {}
        for ws in wmap.values():
            if not isinstance(ws, dict) or ws.get("kind") not in KINDS or not valid_repo(ws.get("repo")):
                continue
            by_repo.setdefault(ws["repo"], []).append(ws)
        for repo in sorted(by_repo, key=str.casefold):
            flags = [f for ws in by_repo[repo] for f in _safe_flags(ws)]
            if flags:
                rows.append(BoardRow("watch:%s:%s" % (sid, repo), sid, owner, who, repo, summary(flags),
                                     checked=checked))
            for ws in sorted(by_repo[repo], key=lambda w: w["kind"]):
                err = ws.get("error")
                if err:
                    shown = err if isinstance(err, str) and ERROR_TEXT.fullmatch(err) else "lookup failed"
                    rows.append(BoardRow("watch-err:%s:%s:%s" % (sid, repo, ws["kind"]), sid, owner, who, repo,
                                         error=shown, kind=ws["kind"], checked=checked))
    return rows


def print_report(sid, out, now=None):
    """What `fleetwatch watch report` prints for one session: each watch, its flags, errors and last check."""
    now = time.time() if now is None else now
    doc = load_state(sid)
    if not doc["watches"]:
        print("no watch has run for this session yet; fleetwatch runs them while the session is away "
              "(fleetwatch watch schedule status)", file=out)
        return 0
    total = 0
    for key in sorted(doc["watches"], key=str.casefold):
        ws = doc["watches"][key]
        if not isinstance(ws, dict) or ws.get("kind") not in KINDS or not valid_repo(ws.get("repo")):
            continue
        flags = _safe_flags(ws)
        total += len(flags)
        checked = data.to_epoch(ws.get("last_checked"))
        bits = [ws["repo"], ws["kind"]]
        if ws.get("error"):
            err = ws["error"] if isinstance(ws["error"], str) and ERROR_TEXT.fullmatch(ws["error"]) else "lookup failed"
            bits.append("watch error: " + err)
        if ws.get("available") is False:
            bits.append("not available (needs admin on the repo)")
        bits += summary(flags) or ([] if ws.get("error") or ws.get("available") is False else ["nothing new"])
        bits.append("checked %s ago" % data.human(now - checked) if checked else "not checked yet")
        print(" · ".join(bits), file=out)
    if total:
        print("Clear them once handled: fleetwatch watch ack [REPO] [--kind KIND]", file=out)
    else:
        print("nothing flagged", file=out)
    return 0


def ack(sid, repo=None, kind=None):
    """Clear the session's flags, optionally for one repo and kind. They stay seen, so they do not return."""
    cleared = 0
    with locked(sid):
        doc = load_state(sid)
        for ws in doc["watches"].values():
            if repo and ws.get("repo", "").lower() != repo.lower():
                continue
            if kind and ws.get("kind") != kind:
                continue
            cleared += len(ws.get("flags") or [])
            ws["flags"] = []
        if cleared:
            save_state(doc)
    return cleared


# ---------------------------------------------------------------------------- the command line


def build_parser():
    p = argparse.ArgumentParser(
        prog="fleetwatch watch",
        description="Standing GitHub checks a session declares in its status file (`watches`), run while the "
                    "session is away. run: check every session that is not active. report: this session's "
                    "flags. ack: clear them. schedule: run every 15 minutes.")
    p.add_argument("verb", choices=("run", "report", "ack", "schedule"))
    p.add_argument("rest", nargs="*", help="ack: an optional owner/name; schedule: install, remove or status")
    p.add_argument("--kind", help="ack: only this kind")
    p.add_argument("--sessions-dir")
    p.add_argument("--status-dir")
    return p


def main(argv, out=None, err=None, ppids=None, start_pid=None):
    out, err = out or sys.stdout, err or sys.stderr
    if argv[:1] == ["schedule"]:
        from . import schedule
        return schedule.main(argv[1:], out, schedule.WATCH)
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    paths = dict(data.default_paths())
    for key, val in (("sessions", args.sessions_dir), ("status", args.status_dir)):
        if val:
            paths[key] = val
    if args.verb == "run":
        return run(paths, out=out)
    if args.kind is not None and args.kind not in KINDS:
        print("fleetwatch watch: --kind must be one of %s" % ", ".join(KINDS), file=err)
        return 2
    repo = args.rest[0] if args.rest else None
    if len(args.rest) > 1 or (repo is not None and not valid_repo(repo)):
        print("fleetwatch watch: give at most one repo, as owner/name", file=err)
        return 2
    from . import write_status
    try:
        _name, sid = write_status.find_session(paths["sessions"], data.Ancestry(paths.get("ps")) if ppids is None
                                               else ppids, os.getpid() if start_pid is None else start_pid)
    except write_status.StatusError as e:
        print("fleetwatch watch %s: %s" % (args.verb, e), file=err)
        return e.code
    if args.verb == "report":
        return print_report(sid, out)
    n = ack(sid, repo, args.kind)
    print("cleared %s" % data.plural(n, "flag"), file=out)
    return 0
