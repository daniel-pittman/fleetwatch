"""Built-in synthetic fleet for `fleetwatch --demo`: screenshots, documentation and a first look.

Everything lives in one temporary folder: the session registry, status files, transcripts, groups and
state. Nothing reads or writes the user's own folders, and every name here is fictional.
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

from . import data

STUCK_SID = "d3a00001-0000-4000-8000-000000000001"
RESUME_SIDS = ("d3a00002-0000-4000-8000-000000000002", "d3a00003-0000-4000-8000-000000000003")
# The demo never starts a real session: Enter on a Resume row runs this in place of `claude`.
RESUME_COMMAND = "echo demo, nothing resumed:"
# The sessions' folders live under this folder of the temp root, which the board shows as ~, so the
# Resume tab reads ~/code/storefront/pricing and never the temporary path.
HOME = "home"
STATE = "state"          # the demo's XDG_STATE_HOME: its watch state lives here
GROUPS_TOML = '''[[group]]
name = "Storefront"
paths = ["{home}/code/storefront"]

[[group]]
name = "Docs and Site"
paths = ["{home}/code/site"]
'''
# (name, folder below code/, state, minutes since last activity)
SESSIONS = (
    ("Billing API", "storefront/billing", "waiting", 7),
    ("Checkout Flow", "storefront/checkout", "idle", 35),
    ("Search Tuning", "storefront/search", "busy", 2),
    ("Inventory Sync", "storefront/inventory", "idle", 95),
    ("Docs Site", "site/docs", "busy", 4),
    ("Release Notes", "site/notes", "idle", 50),
    ("Newsletter", "site/newsletter", "idle", 20),
    ("wt-billing-lint", "storefront/billing", "busy", 1),
)


def _today():
    return datetime.fromtimestamp(time.time(), data.LOCAL_TZ).date()


def _write(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def build(root, now=None, parent_pid=None, helper_pid=None):
    """Write the demo tree under root and return the paths dict `default_paths()` would give.

    Every demo session reports pid 1, which always answers a liveness check. The helper needs a live pid
    of its own whose parent is the first session, so the process table below says so.
    """
    now = time.time() if now is None else now
    parent_pid = parent_pid or os.getpid()
    helper_pid = helper_pid or os.getppid()
    if helper_pid in (1, parent_pid):
        helper_pid = None
    today = _today()
    day = lambda n: (today + timedelta(days=n)).isoformat()      # noqa: E731
    stamp = lambda age_s: datetime.fromtimestamp(now - age_s, data.LOCAL_TZ).isoformat()   # noqa: E731
    ms = lambda age_s: int((now - age_s) * 1000)                  # noqa: E731
    sessions, status, projects = (os.path.join(root, d) for d in ("sessions", "status", "projects"))
    home = os.path.join(root, HOME)
    for i, (name, folder, state, mins) in enumerate(SESSIONS):
        cwd = os.path.join(home, "code", folder)
        os.makedirs(cwd, exist_ok=True)
        sid = STUCK_SID if i == 0 else "d3a1%04d-0000-4000-8000-0000000000%02d" % (i, i)
        _write(os.path.join(sessions, sid + ".json"), {
            "name": name, "sessionId": sid, "pid": parent_pid if i == 0 else (helper_pid or 1) if i == len(SESSIONS) - 1 else 1, "kind": "interactive", "cwd": cwd, "status": state,
            "statusUpdatedAt": ms(mins * 60), "updatedAt": ms(mins * 60), "startedAt": ms(3 * 3600)})

    def put(fname, **doc):
        _write(os.path.join(status, fname), doc)

    put("billing.json", session="Billing API", persona="Robin", updated=stamp(7 * 60),
        working_on="adding retry to the invoice job",
        questions=["Should refunds over the limit need a second approver?"])
    put("checkout.json", session="Checkout Flow", persona="Sage", updated=stamp(35 * 60),
        questions=[{"q": "Ship the new address form behind a flag or straight to everyone?", "since": stamp(3 * 3600)},
                   "Is it fine to drop support for the old payment sheet this release?"],
        upcoming=[{"what": "Checkout load test", "due": day(4), "kind": "milestone"}])
    put("search.json", session="Search Tuning", persona="Rowan", updated=stamp(2 * 60),
        working_on="comparing ranking models on the replay set",
        upcoming=[{"what": "Index rebuild", "due": day(2), "kind": "deploy"}])
    put("inventory.json", session="Inventory Sync", updated=stamp(95 * 60), cadence_min=30,
        working_on="waiting on the nightly import",
        upcoming=[{"what": "Review supplier feed changes", "due": day(9), "kind": "deadline", "yours": True}])
    put("docs.json", session="Docs Site", persona="Piper", updated=stamp(4 * 60),
        working_on="rewriting the quick start",
        upcoming=[{"what": "Docs freeze", "due": day(6), "kind": "milestone"}])
    put("notes.json", session="Release Notes", persona="Piper", updated=stamp(50 * 60),
        upcoming=[{"what": "Send the 2.4 release notes to the partner list", "due": day(-3),
                   "kind": "deadline", "yours": True}])
    put("newsletter.json", session="Newsletter", updated=stamp(20 * 60),
        upcoming=[{"what": "Approve the autumn newsletter draft", "due": day(0), "kind": "deadline", "yours": True},
                  {"what": "Team sync", "due": day(1), "kind": "meeting"}])
    put("lint.json", session="wt-billing-lint", updated=stamp(60), questions=["Lint rule needs a decision"])
    for sid, title, folder, hours in ((RESUME_SIDS[0], "Pricing Page", "storefront/pricing", 20),
                                      (RESUME_SIDS[1], "Status Page", "site/status", 52)):
        cwd = os.path.join(home, "code", folder)
        os.makedirs(cwd, exist_ok=True)
        path = os.path.join(projects, "-demo-" + folder.replace("/", "-"), sid + ".jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for rec in ({"type": "user", "cwd": cwd, "entrypoint": "cli", "sessionId": sid},
                        {"type": "custom-title", "customTitle": title, "sessionId": sid}):
                fh.write(json.dumps(rec, separators=(",", ":")) + "\n")   # the title scan matches compact JSON
        t = now - hours * 3600
        os.utime(path, (t, t))
    # The stopped sessions' last status files, already pruned as a finished session's usually is,
    # give each Resume row its "was: ... next: ..." line.
    pruned = os.path.join(status, ".pruned")
    _write(os.path.join(pruned, "pricing-page.json"), {
        "session": "Pricing Page", "sessionId": RESUME_SIDS[0], "updated": stamp(20 * 3600),
        "working_on": "new annual plans on the pricing page",
        "upcoming": [{"what": "Check the price list again", "due": day(3), "kind": "checkin", "yours": True}]})
    _write(os.path.join(pruned, "status-page.json"), {
        "session": "Status Page", "sessionId": RESUME_SIDS[1], "updated": stamp(52 * 3600),
        "working_on": "moving the status page to the new host", "questions": ["Keep the old URL as a redirect?"]})
    os.makedirs(os.path.join(root, "jobs"), exist_ok=True)
    config = os.path.join(root, "config")
    os.makedirs(config, exist_ok=True)
    _write(os.path.join(config, "parked.json"), {"sessions": {RESUME_SIDS[0]: {
        "name": "Pricing Page", "note": "waiting on the new price list from finance", "parked_at": now - 19 * 3600}}})
    with open(os.path.join(config, "groups.toml"), "w", encoding="utf-8") as fh:
        fh.write(GROUPS_TOML.format(home=home))
    write_watches(os.path.join(root, STATE, "fleetwatch", "watches"), now)
    return {"command": "%s -c \"print('[]')\"" % sys.executable, "sessions": sessions,
            "jobs": os.path.join(root, "jobs"), "status": status, "projects": projects,
            "ps": "echo %d %d" % (helper_pid or 1, parent_pid), "archive": os.path.join(root, "archive")}


def write_watches(folder, now):
    """Two stopped sessions' watch state: one with flags, one whose lookup GitHub refused. The board reads
    these files as it reads real ones; nothing calls GitHub."""
    checked = datetime.fromtimestamp(now - 4 * 60, data.LOCAL_TZ).isoformat(timespec="seconds")

    def watch(kind, repo, flags=(), error=None):
        return {"%s:%s" % (kind, repo): {"kind": kind, "repo": repo, "last_checked": checked, "error": error,
                                         "available": True, "seen": [f["key"] for f in flags], "flags": list(flags)}}
    store = "example-shop/storefront"
    pricing = dict(watch("external_activity", store, [{"what": "issue", "ref": 41, "key": "issue:41"},
                                                       {"what": "issue", "ref": 44, "key": "issue:44"}]),
                   **watch("pr_checks_failed", store, [{"what": "failed_check", "ref": 38, "key": "check:38@demo"}]))
    status = watch("pr_checks_failed", "example-shop/status-site", error="lookup failed (HTTP 403)")
    for sid, name, state, watches in ((RESUME_SIDS[0], "Pricing Page", "parked", pricing),
                                      (RESUME_SIDS[1], "Status Page", "stopped", status)):
        _write(os.path.join(folder, sid + ".json"), {"version": 1, "sessionId": sid, "session": name,
                                                     "owner_state": state, "checked": checked, "watches": watches})


def index_rows(now=None):
    """Six synthetic indexes: each state the Indexes tab shows and each kind of owner. Nothing here is read
    from disk. Owners are demo sessions: Billing API runs, Pricing Page and Status Page can be resumed."""
    from .index_health import Health
    now = time.time() if now is None else now
    two_scheduled = 2 * 1800
    return [
        Health("storefront-sessions", "team", "session", "ok", finished=now - 14 * 60, stale_after=two_scheduled,
               exchanges=1842, files=96, scheduled=True, owner="Billing API"),
        Health("site-docs", "team", "plain", "ok", finished=now - 5 * 3600, stale_after=two_scheduled,
               files=212, scheduled=True, owner="Docs Site"),
        Health("nightly-notes", "team", "plain", "ok", finished=now - 3 * 3600, stale_after=two_scheduled,
               files=58, scheduled=True, refresh_state="waiting"),
        Health("support-faq", "team", "session", "ok", finished=now - 2 * 3600, stale_after=two_scheduled,
               exchanges=310, files=27, scheduled=True, owner="Status Page", refresh_state="refreshing"),
        Health("release-history", "archive", "session", "failed", "build", finished=now - 40 * 60,
               stale_after=two_scheduled, exchanges=530, files=41, scheduled=True, owner="Pricing Page"),
        Health("advising-notes", "ferpa", "session", "ok", finished=now - 20 * 60, stale_after=two_scheduled,
               protection="refused", refusal="settings", exchanges=77, files=12, scheduled=True),
    ]


def environ(root, paths):
    """The variables that keep every other fleetwatch module inside the demo folder."""
    return {"FLEETWATCH_CONFIG_DIR": os.path.join(root, "config"), "FLEETWATCH_PS_CMD": paths["ps"],
            "FLEETWATCH_TTY_CMD": "true", "FLEETWATCH_OSASCRIPT": "false",
            "FLEETWATCH_CLAUDE_CMD": RESUME_COMMAND, "FLEETWATCH_SELF_CMD": "true",
            "FLEETWATCH_AGENTS_CMD": paths["command"], "FLEETWATCH_SESSIONS_DIR": paths["sessions"],
            "FLEETWATCH_JOBS_DIR": paths["jobs"], "FLEETWATCH_STATUS_DIR": paths["status"],
            "FLEETWATCH_PROJECTS_DIR": paths["projects"], "FLEETWATCH_ARCHIVE_DIR": paths["archive"],
            "FLEETWATCH_DISPLAY_HOME": os.path.join(root, HOME), "FLEETWATCH_NO_MIGRATE": "1",
            "FLEETWATCH_DEMO": "1", "XDG_STATE_HOME": os.path.join(root, STATE)}


def prepare():
    """(TemporaryDirectory, paths). The environment is pointed at the temp folder; keep the directory
    object alive for as long as the demo runs."""
    tmp = tempfile.TemporaryDirectory(prefix="fwdemo-")
    root = tmp.name
    paths = build(root)
    os.environ.update(environ(root, paths))
    return tmp, paths
