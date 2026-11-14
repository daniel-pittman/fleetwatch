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
RUN_BUDGET_S = 600         # a run stops starting lookups after this; the next run takes the rest first
UNREACHABLE_AFTER = 3      # this many lookups in a row failing to connect ends the run: GitHub is unreachable
PER_PAGE = 100
MAX_PAGES = 3              # up to 300 items per listing per run
MAX_PRS = 30               # per-PR lookups stop after the 30 newest open PRs
ACTIVE_FACTOR = 1.5        # a session is active while its status is newer than 1.5 x its cadence
DEFAULT_CADENCE_MIN = 30
LOOKBACK_S = 14 * 86400    # external_activity never looks further back than this
STALE_RUN_S = 3600         # four missed 15-minute runs: the board says the watches are not running
# gh's own words for a request that never reached GitHub; matched, never stored
NETWORK = re.compile(r"connection reset|connection refused|i/o timeout|TLS handshake|no such host|"
                     r"network is unreachable|dial tcp|unexpected EOF|context deadline exceeded", re.I)
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

    @property
    def scope(self):
        """"gh": gh itself cannot run, so no lookup can; "retry": the request did not get an answer (no
        connection, a timeout, a GitHub-side failure), so the next run tries again; "watch": GitHub answered
        and refused or garbled this lookup."""
        r = self.reason
        if r in ("gh not found", "gh not logged in", "HTTP 401"):
            return "gh"
        if r in ("timed out", "network error", "HTTP 429") or r.startswith(("HTTP 5", "gh exited")):
            return "retry"
        return "watch"


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
            if NETWORK.search(p.stderr or ""):
                raise GhError("network error")
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
    ws.pop("retrying", None)
    ws.pop("failing_since", None)
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
    """(Result, None) or (None, GhError) for one watch."""
    last = data.to_epoch(ws.get("last_checked")) or 0
    # the session covered its own repos until it last wrote its status
    since = max(last, data.to_epoch(sf.get("updated")) or 0, now - LOOKBACK_S)
    try:
        return CHECKS[w["kind"]](gh, w["repo"], w, Context(now, since, team)), None
    except GhError as e:
        return None, e


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


def run_path():
    return os.path.join(state_dir(), "run.json")


def load_run():
    """The last run's record, or None before the first run."""
    try:
        doc = data.load_json(run_path())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _save_run(doc):
    config.atomic_write(run_path(), json.dumps(doc, indent=1, sort_keys=True) + "\n")


def _say(out, text):
    # the scheduled run's output is a file; flush each line so the log shows a run that is still going
    print(text, file=out, flush=True)


def _local(ts):
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def run(paths=None, now=None, out=None, parked_path=None, gh=None, clock=time.time):
    """Evaluate the watches of every session that is not active, least recently checked first. Prints one
    line per watch: counts only. Stops starting lookups after RUN_BUDGET_S, at once when gh cannot run, and
    after UNREACHABLE_AFTER lookups in a row that got no answer; what it did not reach keeps its state and is
    taken first next time. The outcome goes in run.json, which the board reads."""
    paths = paths or data.default_paths()
    out = out or sys.stdout
    with locked("run", block=False) as got:
        if not got:
            _say(out, "another watch run is in progress")
            return 0
        t0 = clock()
        now = t0 if now is None else now
        prev = load_run() or {}
        record = {"version": 1, "started": iso(now), "ended": None, "last_ended": prev.get("ended") or
                  prev.get("last_ended"), "outcome": prev.get("outcome"), "reason": prev.get("reason"),
                  "since": prev.get("since")}
        _save_run(record)
        _say(out, "watch run started %s" % _local(now))
        live, parked = live_sids(paths["sessions"]), _parked(parked_path, out)
        gh, team, declared = gh or Gh(), {}, set()
        sessions = []
        for sf in _status_files(paths, out):
            try:
                wl = validate(sf.get("watches") or [])
            except WatchError as e:
                _say(out, "%s: watches ignored, %s" % (data.clean(sf["session"]).strip(), e))
                wl = []
            if wl:
                declared.add(sf["sessionId"])
                sessions.append((sf, wl, load_state(sf["sessionId"])))
        sessions.sort(key=lambda t: data.to_epoch(t[2].get("checked")) or 0)
        outcome, reason, misses, done, left = "ok", None, 0, 0, 0
        for sf, wl, before_doc in sessions:
            sid, name = sf["sessionId"], data.clean(sf["session"]).strip()
            who = owner_state(sf, live, parked, now)
            before = before_doc["watches"]
            results, complete = {}, True
            if who == "active":
                _say(out, "%s: active, its watches are its own" % name)
            else:
                for w in wl:
                    if outcome != "ok" or clock() - t0 > RUN_BUDGET_S:
                        if outcome == "ok":
                            outcome = "budget"
                        complete = False
                        left += 1
                        continue
                    result, err = evaluate(gh, w, before.get(watch_key(w)) or {}, sf, now, team)
                    results[watch_key(w)] = (result, err)
                    done += 1
                    if err is None or err.scope == "watch":
                        misses = 0
                    elif err.scope == "gh":
                        outcome, reason = "gh", err.reason
                    else:
                        misses += 1
                        if misses >= UNREACHABLE_AFTER:
                            outcome, reason = "unreachable", err.reason
            # merged into a fresh read under the lock, so an ack made while the lookups ran is kept
            with locked(sid):
                doc = load_state(sid)
                doc.update(session=name, owner_state=who)
                if complete:
                    doc["checked"] = iso(now)
                doc["watches"] = {watch_key(w): doc["watches"].get(watch_key(w)) or new_watch_state(w) for w in wl}
                for key, (result, err) in results.items():
                    ws = doc["watches"][key]
                    if err is None:
                        n_new = merge(ws, result, now)
                        state = "not available" if not ws["available"] else "%d open, %d new" % (len(ws["flags"]), n_new)
                        _say(out, "%s · %s · %s" % (name, key, state))
                    elif err.scope == "gh":
                        continue                    # the run's record says it, once
                    elif err.scope == "watch":
                        ws["error"] = "lookup failed (%s)" % err.reason
                        _say(out, "%s · %s · watch error: %s" % (name, key, ws["error"]))
                    else:
                        # no answer: the flags stay, and the board shows it only if it lasts (see board_rows)
                        ws["retrying"] = "lookup failed (%s)" % err.reason
                        ws.setdefault("failing_since", iso(now))
                        _say(out, "%s · %s · no answer, will retry: %s" % (name, key, ws["retrying"]))
                save_state(doc)
        # a session that no longer declares watches keeps no flags on the board
        for path in sorted(glob.glob(os.path.join(glob.escape(state_dir()), "*.json"))):
            sid = os.path.basename(path)[:-5]
            if sid not in declared and SID.fullmatch(sid) and path != run_path():
                with locked(sid):
                    doc = load_state(sid)
                    if doc["watches"]:
                        doc.update(watches={}, checked=iso(now))
                        save_state(doc)
        ended = now + (clock() - t0)
        problem = outcome in ("gh", "unreachable")
        same = problem and prev.get("outcome") == outcome and prev.get("since")
        record.update(ended=iso(ended), outcome=outcome, reason=reason, checked=done, left=left,
                      since=(prev["since"] if same else iso(now)) if problem else None)
        _save_run(record)
        tail = {"ok": "", "budget": "; stopped at the time limit, %d left for the next run" % left,
                "gh": "; stopped, gh cannot run (%s)" % reason,
                "unreachable": "; stopped, GitHub unreachable (%s), %d left" % (reason, left)}[outcome]
        _say(out, "watch run ended %s: %s checked%s" % (_local(ended), data.plural(done, "watch", "watches"), tail))
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


ERROR_TEXT = re.compile(r"lookup failed \((HTTP \d{3}|timed out|network error|gh not found|gh not logged in|"
                        r"bad response|gh exited \d+)\)")
REASON_TEXT = re.compile(r"HTTP \d{3}|timed out|network error|gh not found|gh not logged in|gh exited \d+")


@dataclass
class BoardRow:
    key: str
    sid: str
    owner: str
    owner_state: str
    repo: str = None           # None on a row about a session's run or the whole run
    parts: list = field(default_factory=list)
    error: str = None
    kind: str = None
    checked: float = None


def _read_states(now):
    """(sid, owner, owner state, checked, stale text or None, {repo: [watch state]}) per state file with
    watches, from the stored fields that pass their shape checks. Reads only."""
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
        stale = None
        if checked is None or now - checked > STALE_RUN_S:
            age = "yet" if checked is None else "for %s" % data.human(now - checked)
            stale = "not checked %s; is the schedule running? see fleetwatch watch schedule status" % age
        by_repo = {}
        for ws in wmap.values():
            if isinstance(ws, dict) and ws.get("kind") in KINDS and valid_repo(ws.get("repo")):
                by_repo.setdefault(ws["repo"], []).append(ws)
        yield sid, owner, who, checked, stale, {r: by_repo[r] for r in sorted(by_repo, key=str.casefold)}


def _error_of(ws):
    err = ws.get("error")
    if not err:
        return None
    if not (isinstance(err, str) and ERROR_TEXT.fullmatch(err)):
        return "lookup failed"
    # a lookup with no answer is kept under "retrying"; one under "error" is from an older run, not news
    return None if GhError(err[len("lookup failed ("):-1]).scope == "retry" else err


RUN_KEY = "watch-run"


def run_problem(now):
    """(text, since) when the watch runs as a whole are failing: gh cannot run, GitHub did not answer, or no
    run has finished for STALE_RUN_S. None when they are fine or there is no run record yet. One problem
    is one row, never a row per watch or per session."""
    rec = load_run()
    if rec is None:
        return None
    reason = rec.get("reason") if isinstance(rec.get("reason"), str) and REASON_TEXT.fullmatch(rec["reason"]) \
        else "no answer"
    since = data.to_epoch(rec.get("since"))
    if rec.get("outcome") == "gh":
        return "watches cannot run: %s; check gh auth status" % reason, since
    if rec.get("outcome") == "unreachable":
        at = " for %s" % data.human(now - since) if since is not None else ""
        return "watches could not reach GitHub (%s)%s; retrying every 15 minutes" % (reason, at), since
    ended = data.to_epoch(rec.get("ended") or rec.get("last_ended"))
    started = data.to_epoch(rec.get("started"))
    last = ended if ended is not None else started
    if last is not None and now - last > STALE_RUN_S:
        return ("not checked for %s; is the schedule running? see fleetwatch watch schedule status"
                % data.human(now - last)), last
    return None


def _lasting_retry(ws, now):
    """A watch whose lookups have had no answer for STALE_RUN_S while the other lookups worked."""
    since = data.to_epoch(ws.get("failing_since"))
    if since is None or now - since < STALE_RUN_S:
        return None
    err = ws.get("retrying")
    return err if isinstance(err, str) and ERROR_TEXT.fullmatch(err) else "lookup failed"


def _problems(now):
    """(run problem or None, [(sid, owner, who, checked, stale, by_repo)]) with the per-session stale text
    dropped while the whole run has a problem, since that one row already says it."""
    problem = run_problem(now)
    states = list(_read_states(now))
    if problem:
        states = [(sid, owner, who, checked, None, by_repo) for sid, owner, who, checked, _s, by_repo in states]
    return problem, states


def board_rows(now=None):
    """The Watches section's rows from the state files: one for a problem with the runs themselves, one per
    session and repo with something flagged, one per watch GitHub refused, one per watch with no answer for
    an hour while the rest worked, and one per session whose watches have not run for an hour. Reads only."""
    now = time.time() if now is None else now
    problem, states = _problems(now)
    rows = []
    if problem:
        rows.append(BoardRow(RUN_KEY, "", "Watch runs", "run", error=problem[0], checked=None))
    for sid, owner, who, checked, stale, by_repo in states:
        if stale:
            rows.append(BoardRow("watch-stale:" + sid, sid, owner, who, error=stale, checked=checked))
        for repo, ws_list in by_repo.items():
            flagged = [f for ws in ws_list for f in _safe_flags(ws)]
            if flagged:
                rows.append(BoardRow("watch:%s:%s" % (sid, repo), sid, owner, who, repo, summary(flagged),
                                     checked=checked))
            for ws in sorted(ws_list, key=lambda w: w["kind"]):
                err = _error_of(ws) or (None if problem else _lasting_retry(ws, now))
                if err:
                    rows.append(BoardRow("watch-err:%s:%s:%s" % (sid, repo, ws["kind"]), sid, owner, who, repo,
                                         error=err, kind=ws["kind"], checked=checked))
    return rows


def flags(paths=None, now=None):
    """(key, owner name, text) for the desktop notifier: one per owner, repo and kind of flag, one per watch
    in error, one per session whose watches have stalled, and one for a problem with the runs themselves
    (owner "", key RUN_KEY...) once GitHub has been unreachable for STALE_RUN_S, at once when gh cannot run.
    The key holds the session id, repo, kind and the numbers, so a new number is a new key and an unchanged
    flag keeps its key. The text is counts and numbers only. paths is accepted for the notifier's
    interface; the state folder comes from settings."""
    now = time.time() if now is None else now
    problem, states = _problems(now)
    out = []
    if problem:
        text, since = problem
        if not text.startswith("watches could not reach") or (since is not None and now - since >= STALE_RUN_S):
            out.append(("%s:%s" % (RUN_KEY, text.split(" (")[0].split(";")[0]), "", text))
    for sid, owner, _who, _checked, stale, by_repo in states:
        if stale:
            out.append(("watch-stale:%s" % sid, owner, "watch error: " + stale))
        for repo, ws_list in by_repo.items():
            short = repo.split("/", 1)[1]
            flagged = [f for ws in ws_list for f in _safe_flags(ws)]
            for what in WHAT:
                mine = [f for f in flagged if f["what"] == what]
                if mine:
                    refs = sorted({_ref_text(f["ref"]) for f in mine})
                    out.append(("watch:%s:%s:%s:%s" % (sid, repo, what, ",".join(refs)), owner,
                                "%s: %s" % (short, summary(mine)[0])))
            for ws in sorted(ws_list, key=lambda w: w["kind"]):
                err = _error_of(ws) or (None if problem else _lasting_retry(ws, now))
                if err:
                    out.append(("watch-err:%s:%s:%s" % (sid, repo, ws["kind"]), owner,
                                "%s: watch error: %s" % (short, err)))
    return out


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
