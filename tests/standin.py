"""A fixed stand-in fleetwatch: static inputs and a fixed clock, shared by the golden and app tests."""
import atexit
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

# Dates in the fixtures are Mountain time. fleetwatch reads its zone once at import, so pin it here,
# before any test module imports fleetwatch, and keep a stray settings.toml out of the run.
os.environ["TZ"] = "America/Denver"
time.tzset()
_CONFIG = tempfile.TemporaryDirectory()
atexit.register(_CONFIG.cleanup)
with open(os.path.join(_CONFIG.name, "settings.toml"), "w") as _fh:
    _fh.write('timezone = "America/Denver"\n')
os.environ["FLEETWATCH_CONFIG_DIR"] = _CONFIG.name

NOW = datetime.fromisoformat("2026-10-18T14:00:00-06:00").timestamp()
PARENT_PID = 1                 # launchd: always answers kill -0
CHILD_PID = os.getpid()
DEAD_PID = 2 ** 22 + 12345
PPIDS = {CHILD_PID: PARENT_PID}


def ms(offset_s=0):
    return int((NOW + offset_s) * 1000)


def iso(offset_s):
    return datetime.fromtimestamp(NOW + offset_s, timezone(timedelta(hours=-6))).isoformat()


def build(root, agents_file=None):
    """Write the stand-in tree under root; return the paths dict."""
    dirs = {k: os.path.join(root, k) for k in ("sessions", "jobs", "status", "projects")}
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)
    agents = os.path.join(root, "agents.json")
    with open(agents, "w") as fh:
        json.dump([], fh)

    def reg(name, sid, pid, status, age):
        d = {"name": name, "sessionId": sid, "pid": pid, "kind": "interactive", "cwd": "/x",
             "status": status, "statusUpdatedAt": ms(-age), "updatedAt": ms(-age),
             "startedAt": ms(-age)}
        with open(os.path.join(dirs["sessions"], sid + ".json"), "w") as fh:
            json.dump(d, fh)

    def status(fname, **d):
        with open(os.path.join(dirs["status"], fname), "w") as fh:
            json.dump(d, fh)

    reg("Stuck Session", "s1", PARENT_PID, "waiting", 6 * 60)
    reg("Asker Development", "s2", DEAD_PID, "idle", 40 * 60)
    reg("Worker", "s3", DEAD_PID, "busy", 5 * 60)
    reg("Quiet One", "s4", DEAD_PID, "idle", 2 * 3600)
    reg("Sleeper", "s5", DEAD_PID, "idle", 5 * 3600)
    reg("wt-helper-1", "s6", CHILD_PID, "busy", 60)
    reg("Ancient", "s7", DEAD_PID, "idle", 5 * 86400)
    status("stuck.json", session="Stuck Session", persona="Robin", updated=iso(-6 * 60),
           questions=["Which branch should the fix land on?"],
           upcoming=[{"what": "Overdue thing", "due": "2026-10-10", "kind": "deadline", "yours": True}])
    status("asker.json", session="Asker Development", persona="Piper", updated=iso(-40 * 60),
           questions=[{"q": "Approve the schema change?", "since": iso(-3 * 3600)},
                      "Is the staging deploy allowed this week or should it wait for the freeze to lift and the release train to pass?"],
           remind=["Email the registrar about the term dates"],
           upcoming=[{"what": "Pens down for LA applications", "due": "2026-10-22", "kind": "deadline", "yours": True},
                     {"what": "Dev deploy", "due": "2026-10-28", "kind": "deploy"},
                     {"what": "Spring launch", "due": "2027-03-01", "kind": "milestone"}])
    status("worker.json", session="Worker", persona="Rowan", updated=iso(-5 * 60), working_on="reviewing PR 720",
           upcoming=[{"what": "Dev deploy", "due": "2026-10-28", "kind": "deploy"},
                     {"what": "Team meeting", "due": "2026-10-19T10:00:00-06:00", "kind": "meeting"},
                     {"what": "Faculty interviews", "due": "2026-11-05", "kind": "milestone", "yours": True}])
    status("quiet.json", session="Quiet One", updated=iso(-2 * 3600), cadence_min=30,
           working_on="waiting on CI", upcoming=[{"what": "Write the ITS ticket", "yours": True}])
    status("helper.json", session="wt-helper-1", updated=iso(-60),
           questions=["Helper question that rolls up"])
    return dict(dirs, command="%s -c \"import sys;sys.stdout.write(open(sys.argv[1]).read())\" %s"
                % (sys.executable, agents), ps="true")


def collect_view(paths, window=24 * 3600, show_all=False):
    from fleetwatch import data
    warnings = []
    recs = data.build_records(paths, NOW, warnings, ppids=PPIDS)
    sections, stale = data.classify(recs, NOW, window, show_all)
    return sections, stale, warnings
