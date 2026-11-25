"""fleetwatch data: collection, rollup, timeline buckets and text cleaning. No UI."""
import argparse
import glob
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import date as date_cls, datetime, time as time_cls

from .settings import LOCAL_TZ

WIDTH = 100
DEFAULT_INTERVAL_S = 5.0
DEFAULT_HORIZON_DAYS = 21
KINDS = {"checkin", "deadline", "deploy", "meeting", "milestone", "todo"}
HELPER_NAME = re.compile(r"^(wt-|run-|t\d+-)|^[0-9a-f]{32}(-[0-9a-z]{2})?$")
BUSY_RECENT_S = 15 * 60
NEEDS_STATES = {"blocked", "waiting"}
WORKING_STATES = {"busy", "shell"}
HOME = os.path.expanduser("~")


def default_paths():
    return {
        "command": os.environ.get("FLEETWATCH_AGENTS_CMD", "claude agents --json"),
        "sessions": os.environ.get("FLEETWATCH_SESSIONS_DIR", os.path.join(HOME, ".claude/sessions")),
        "jobs": os.environ.get("FLEETWATCH_JOBS_DIR", os.path.join(HOME, ".claude/jobs")),
        "status": os.environ.get("FLEETWATCH_STATUS_DIR", os.path.join(HOME, ".claude/state/fleetwatch")),
        "ps": os.environ.get("FLEETWATCH_PS_CMD", "ps -axo pid=,ppid="),
        "projects": os.environ.get("FLEETWATCH_PROJECTS_DIR", os.path.join(HOME, ".claude/projects")),
        "archive": os.environ.get("FLEETWATCH_ARCHIVE_DIR", os.path.join(
            HOME, ".local/share/claude-session-archive/archive/projects")),
    }


def to_epoch(v):
    """Epoch seconds from epoch ms/s numbers or ISO-8601 strings; None if unusable."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v / 1000.0 if v > 1e11 else float(v)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_agents(command, warnings):
    """Input 1. Returns a list of dicts, or None (with one warning) on any failure."""
    try:
        out = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise ValueError("exit %d" % out.returncode)
        data = json.loads(out.stdout)
        if not isinstance(data, list) or not all(isinstance(x, dict) and "name" in x for x in data):
            raise ValueError("unexpected shape")
        return data
    except Exception as exc:
        warnings.append("warning: `%s` unusable (%s); using the session registry alone"
                        % (command, type(exc).__name__ if not str(exc) else exc))
        return None


def read_registry(folder):
    rows = []
    for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
        try:
            d = load_json(path)
        except Exception:
            continue
        if isinstance(d, dict):
            rows.append({k: d.get(k) for k in (
                "name", "pid", "kind", "cwd", "status", "statusUpdatedAt",
                "updatedAt", "startedAt", "sessionId", "nameSince")})
    return rows


def read_job(folder, job_id):
    if not job_id or not re.fullmatch(r"[A-Za-z0-9_.-]+", str(job_id)):
        return None
    try:
        d = load_json(os.path.join(folder, str(job_id), "state.json"))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    return {"state": d.get("state"), "updatedAt": d.get("updatedAt")}


def read_status_files(folder, warnings):
    """Parsed status files as a list, one per distinct session.

    Files that share a sessionId are one session's leftovers (a rename moves the file):
    only the newest `updated` is kept. Files without a sessionId stay keyed by name.
    """
    best = {}
    # Only the folder's own files: `fleetwatch prune` parks finished sessions in .pruned/, which
    # must never be read back as live status.
    for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
        if not os.path.isfile(path):
            continue
        base = os.path.basename(path)
        try:
            d = load_json(path)
            if not isinstance(d, dict) or not isinstance(d.get("session"), str):
                raise ValueError("no session name")
            if to_epoch(d.get("updated")) is None:
                raise ValueError("no usable updated time")
        except Exception as exc:
            warnings.append("warning: status file %s does not parse (%s)" % (base, exc))
            continue
        sid = d.get("sessionId")
        key = ("id", sid) if isinstance(sid, str) and sid else ("name", d["session"])
        if key not in best or to_epoch(d["updated"]) >= to_epoch(best[key]["updated"]):
            best[key] = d
    return list(best.values())


def parse_ps(text):
    """pid -> ppid from `ps -axo pid=,ppid=` output."""
    table = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = int(parts[1])
    return table


def read_ps(command, warnings, timeout=10):
    try:
        out = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            raise ValueError("exit %d" % out.returncode)
        return parse_ps(out.stdout)
    except Exception:
        warnings.append("warning: process table unreadable; helper sessions are not rolled up")
        return {}


def split_ps_command(text):
    """Separate ps's executable path from argv, including paths containing spaces."""
    columns = re.split(r"\s{2,}", text, maxsplit=1)
    if len(columns) == 2:
        return columns[0], columns[1]
    for gap in re.finditer(r"\s+", text):
        command, args = text[:gap.start()], text[gap.end():]
        if args.startswith(command) and (len(args) == len(command) or
                                         args[len(command)].isspace()):
            return command, args
    match = re.match(r"(.+?/(?:node|codex|agy|antigravity|claude))\s+"
                     r"((?:node|codex|agy|antigravity|claude)(?:\s|$).*)", text, re.I)
    if match:
        return match.group(1), match.group(2)
    return text, None


# struct kinfo_proc on 64-bit macOS: 648 bytes, kp_eproc.e_ppid at byte 560 (the same layout psutil
# reads). CTL_KERN=1, KERN_PROC=14, KERN_PROC_PID=1.
_KINFO_SIZE, _E_PPID = 648, 560
_libc = None


def kernel_ppid(pid):
    """Parent of pid straight from the kernel, or None. No fork: on a loaded machine `ps -axo` has
    taken over 120 s, while this takes microseconds. sysctl on macOS, /proc on Linux."""
    global _libc
    try:
        if sys.platform == "darwin":
            import ctypes
            import ctypes.util
            if _libc is None:
                _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            mib = (ctypes.c_int * 4)(1, 14, 1, int(pid))
            buf = ctypes.create_string_buffer(_KINFO_SIZE)
            size = ctypes.c_size_t(_KINFO_SIZE)
            if _libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value != _KINFO_SIZE:
                return None
            return int.from_bytes(buf.raw[_E_PPID:_E_PPID + 4], sys.byteorder, signed=True)
        with open("/proc/%d/stat" % int(pid)) as fh:
            # the command name in parentheses may hold spaces; the ppid is the second field after it
            return int(fh.read().rsplit(")", 1)[1].split()[1])
    except Exception:
        return None


class Ancestry:
    """pid -> ppid for walking up from one process. The general ancestry lookup asks the kernel
    per pid and falls back to a cached parent table. Caller classification reads names and
    arguments in separate process snapshots keyed by pid."""

    def __init__(self, command=None, timeout=60):
        self.command = command or "ps -axo pid=,ppid="
        self.forced = "FLEETWATCH_PS_CMD" in os.environ
        self.timeout, self.table, self.warnings = timeout, None, []
        self._processes = None
        self.process_unreadable = False

    def _snapshot(self):
        """Read structured names and full arguments by pid."""
        if self._processes is None:
            self._processes = {}
            try:
                names = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="],
                                       capture_output=True, text=True, timeout=5)
                args = subprocess.run(["ps", "-axww", "-o", "pid=,args="],
                                      capture_output=True, text=True, timeout=5)
                if names.returncode != 0 or args.returncode != 0:
                    raise OSError("ps failed")
                by_pid = {}
                for line in args.stdout.splitlines():
                    fields = line.strip().split(None, 1)
                    if fields and fields[0].isdigit():
                        by_pid[int(fields[0])] = fields[1] if len(fields) > 1 else ""
                for line in names.stdout.splitlines():
                    fields = line.strip().split(None, 2)
                    if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
                        continue
                    pid = int(fields[0])
                    self._processes[pid] = (int(fields[1]), fields[2], by_pid.get(pid, ""))
                if not self._processes:
                    raise OSError("empty process table")
            except (OSError, subprocess.TimeoutExpired):
                self.process_unreadable = True
        return self._processes

    def _table(self):
        if self.table is None:
            self.table = read_ps(self.command, self.warnings, self.timeout)
        return self.table

    def get(self, pid, default=None):
        if self._processes is not None:
            row = self._snapshot().get(pid)
            return row[0] if row is not None else default
        p = None if self.forced else kernel_ppid(pid)
        if p is None:
            p = self._table().get(pid)
        return default if p is None else p

    def process_name(self, pid):
        """Executable path from the cached process snapshot."""
        row = self._snapshot().get(pid)
        if row is None:
            self.process_unreadable = True
        return row[1] if row is not None else None

    def process_args(self, pid):
        row = self._snapshot().get(pid)
        if row is None or not row[2]:
            self.process_unreadable = True
        return row[2] if row is not None else None

    @property
    def unreadable(self):
        return self.table == {} and bool(self.warnings)


def attach_parents(records, ppids):
    """Set r["parent"] to the top-most session that is a process ancestor of r.

    Only sessions whose registry pid is alive take part. r["children"] on the
    parent holds every descendant (children and grandchildren) flat.
    """
    pidmap = {}
    for r in records:
        r["parent"], r["children"] = None, []
        if r["pid_alive"]:
            pidmap.setdefault(r["pid"], r)
    for r in records:
        if not r["pid_alive"]:
            continue
        seen, top, p = {r["pid"]}, None, ppids.get(r["pid"])
        while p and p not in seen and len(seen) < 512:
            seen.add(p)
            cand = pidmap.get(p)
            if cand is not None and cand is not r:
                top = cand
            p = ppids.get(p)
        r["parent"] = top
    for r in records:
        if r["parent"] is not None:
            r["parent"]["children"].append(r)


def _file_only(r):
    """Mark a record that exists only because a status file names it: never a row."""
    r["file_only"] = True
    return r


def ident(r):
    """A record's stable identity: ("s", its session ID), or ("n", its name) when it has no ID.

    build_records keeps one record per session ID and one per name among those without an ID, so no two
    records share an identity. Board row keys are built from it, never from a display name."""
    if r.get("ident"):
        return r["ident"]
    return ("s", r["sessionId"]) if r.get("sessionId") else ("n", r["name"])


def build_records(paths, now, warnings, ppids=None, failed=None):
    """failed, when given, collects the sources that could not be read ("agents")."""
    agents = read_agents(paths["command"], warnings)
    if agents is None and failed is not None:
        failed.append("agents")
    registry = read_registry(paths["sessions"])
    status_files = read_status_files(paths["status"], warnings)

    by_key = {}

    def rec_for(name, sid):
        key = sid or ("name:%s" % name)
        if key not in by_key:
            by_key[key] = {"name": name, "sessionId": sid, "kind": None, "state": None,
                           "state_since": None, "pid": None, "pid_alive": False,
                           "activity": [], "job_id": None, "agent_state": None, "status": None,
                           "cwd": None, "agent_kind": None}
        return by_key[key]

    for a in agents or []:
        r = rec_for(a.get("name"), a.get("sessionId"))
        r["kind"] = r["kind"] or a.get("kind")
        r["agent_state"] = a.get("state") or a.get("status")
        if a.get("kind") == "background":
            r["agent_kind"] = "background"
        if a.get("kind") == "background" or not r["job_id"]:
            r["job_id"] = a.get("id")
        if isinstance(a.get("cwd"), str):
            r["cwd"] = a["cwd"]
        if isinstance(a.get("pid"), int):
            r["pid"] = a["pid"]

    for g in registry:
        r = rec_for(g.get("name"), g.get("sessionId"))
        r["name"] = r["name"] or g.get("name")
        r["kind"] = g.get("kind") or r["kind"]
        if isinstance(g.get("cwd"), str):
            r["cwd"] = g["cwd"]
        if isinstance(g.get("pid"), int):
            r["pid"] = g["pid"]
        r["registry_state"] = g.get("status")
        r["name_since"] = to_epoch(g.get("nameSince"))
        r["state_since"] = to_epoch(g.get("statusUpdatedAt"))
        r["activity"] += [to_epoch(g.get("statusUpdatedAt")), to_epoch(g.get("updatedAt"))]

    for r in by_key.values():
        r["pid_alive"] = pid_alive(r["pid"])
        job = None
        if r["kind"] == "background" and not r["pid_alive"]:
            job = read_job(paths["jobs"], r["job_id"])
        if job:
            r["job_state"] = job["state"]
            r["activity"].append(to_epoch(job["updatedAt"]))
            if r["state_since"] is None:
                r["state_since"] = to_epoch(job["updatedAt"])

    for sf in status_files:
        name, sid = sf["session"], sf.get("sessionId")
        sid = sid if isinstance(sid, str) and sid else None
        by_id = by_key.get(sid) if sid else None
        by_name = [r for r in by_key.values() if r["name"] == name
                   and not (sid and r["sessionId"] and r["sessionId"] != sid)]
        if by_id is not None and (by_id["pid_alive"] or not any(r["pid_alive"] for r in by_name)):
            matches = [by_id]
        else:
            matches = by_name or ([by_id] if by_id is not None else [_file_only(rec_for(name, None))])
        for r in matches:
            r["status"] = sf
            r["activity"].append(to_epoch(sf.get("updated")))
            if r["name"] != name and r.get("name_since"):
                r["renamed_from"] = (name, r["name_since"])

    records = []
    for r in by_key.values():
        if not isinstance(r["name"], str) or not r["name"]:
            continue
        state = (r.get("registry_state") if r["pid_alive"] else None) \
            or r.get("job_state") or r["agent_state"] or r.get("registry_state") or "unknown"
        acts = [x for x in r["activity"] if x]
        last = max(acts) if acts else None
        recent = last is not None and now - last <= BUSY_RECENT_S
        live = r["pid_alive"] or (state == "busy" and recent)
        r["state"] = str(state)
        r["live"] = live
        r["last"] = last
        if r["state_since"] is None:
            r["state_since"] = last
        records.append(r)
    attach_parents(records, read_ps(paths.get("ps") or "ps -axo pid=,ppid=", warnings) if ppids is None else ppids)
    return records


def classify(records, now, window_s, show_all):
    shown, stale = [], []
    for r in records:
        old = r["last"] is None or now - r["last"] > window_s
        (stale if (r.get("file_only") or (not r["live"] and old)) else shown).append(r)
    sections = {"needs": [], "questions": [], "quiet": [], "working": [], "idle": [], "stale": []}
    used = set()
    for r in shown:
        sf = r["status"] or {}
        if r["live"] and r["state"] in NEEDS_STATES:
            sections["needs"].append(r)
            used.add(id(r))
        has_q = isinstance(sf.get("questions"), list) and sf["questions"]
        if has_q:
            sections["questions"].append(r)
            used.add(id(r))
        cad = sf.get("cadence_min")
        upd = to_epoch(sf.get("updated"))
        if isinstance(cad, (int, float)) and not isinstance(cad, bool) and cad > 0 \
                and upd is not None and now - upd > 1.5 * cad * 60:
            sections["quiet"].append(r)
            used.add(id(r))
        if r["live"] and r["state"] in WORKING_STATES:
            sections["working"].append(r)
            used.add(id(r))
    sections["idle"] = [r for r in shown if id(r) not in used]
    stale.sort(key=lambda r: r["last"] or 0)
    if show_all:
        sections["stale"] = stale
    return sections, stale


def parse_horizon(text):
    m = re.fullmatch(r"(\d+)d", text.strip())
    if not m:
        raise argparse.ArgumentTypeError("horizon looks like 30d or 90d")
    return max(int(m.group(1)), DEFAULT_HORIZON_DAYS)


def parse_window(text):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([mhd])", text.strip())
    if not m:
        raise argparse.ArgumentTypeError("window looks like 90m, 6h or 3d")
    return float(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]



def ago(seconds):
    """Short age for per-question stamps: 45s, 12m, 2.0h, 3d."""
    s = max(0, int(seconds))
    if s < 90:
        return "%ds" % s
    if s < 5400:
        return "%dm" % round(s / 60)
    if s < 172800:
        return "%.1fh" % (s / 3600)
    return "%dd" % (s // 86400)


def human(seconds):
    """Spoken age for headers: 40s, 4 min, 3 h, 2 d."""
    s = max(0, int(seconds))
    if s < 90:
        return "%ds" % s
    if s < 5400:
        return "%d min" % round(s / 60)
    if s < 172800:
        return "%d h" % round(s / 3600)
    return "%d d" % (s // 86400)


_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean(value):
    """Untrusted text for the terminal: tabs and newlines become spaces, other C0/C1 controls go."""
    return _CONTROL.sub("", str(value).replace("\t", " ").replace("\n", " "))


def day(ts):
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%b %-d") if ts else "unknown"


def date_label(ts, now):
    """A date for rows that can be months old: the year shows once it is not this year."""
    if not ts:
        return "unknown"
    when = datetime.fromtimestamp(ts, LOCAL_TZ)
    same = when.year == datetime.fromtimestamp(now, LOCAL_TZ).year
    return when.strftime("%b %-d" if same else "%b %-d %Y")


def plural(n, one, many=None):
    return "%d %s" % (n, one if n == 1 else (many or one + "s"))


def looks_generated(r):
    """An unnamed helper: a generated name and no status file of its own."""
    return not r["status"] and bool(HELPER_NAME.search(str(r["name"])))


# ---------------------------------------------------------------- the view

def items_of(value):
    """(text, since_epoch|None) for each entry; entries are strings or {"q"|text, "since"}."""
    out = []
    for x in value if isinstance(value, list) else []:
        since = None
        if isinstance(x, dict):
            since = to_epoch(x.get("since"))
            x = x.get("q")
        text = clean(x).strip() if x is not None else ""
        if text:
            out.append((text, since))
    return out


RENAME_NOTE_S = 24 * 3600


def renamed_from(r, now):
    """The old name of a session renamed within the last day, else None."""
    old, since = r.get("renamed_from") or (None, None)
    if old and since and 0 <= now - since <= RENAME_NOTE_S:
        return clean(old)
    return None


def make_row(r, now):
    sf = r["status"] or {}
    live_kids = [c for c in r["children"] if c["live"]]
    stuck = now - (r["state_since"] or now) if r["live"] and r["state"] in NEEDS_STATES else None
    helper_stuck = sorted(
        ((c, now - (c["state_since"] or now)) for c in live_kids if c["state"] in NEEDS_STATES),
        key=lambda t: -t[1])
    qs = [(t, s, None) for t, s in items_of(sf.get("questions"))]
    q_helpers = [None] * len(qs)
    for c in r["children"]:
        csf = c["status"] or {}
        tag = clean(c["name"])
        asked = [(t, s, tag) for t, s in items_of(csf.get("questions"))]
        qs += asked
        q_helpers += [c] * len(asked)
    busy_kids = [c for c in live_kids if c["state"] in WORKING_STATES]
    cad, upd = sf.get("cadence_min"), to_epoch(sf.get("updated"))
    quiet = None
    if isinstance(cad, (int, float)) and not isinstance(cad, bool) and cad > 0 \
            and upd is not None and now - upd > 1.5 * cad * 60:
        quiet = cad
    return {
        "r": r, "name": clean(r["name"]), "persona": clean(sf["persona"]) if sf.get("persona") else None,
        "stuck": stuck, "helper_stuck": helper_stuck, "qs": qs,
        "q_helpers": q_helpers,    # per question: the helper record that asked it, None for the session's own
        "working": bool((r["live"] and r["state"] in WORKING_STATES) or busy_kids),
        "busy_helpers": len(busy_kids), "quiet": quiet,
        "updated": upd if upd is not None else r["last"],
        "renamed_from": renamed_from(r, now),
        "working_on": clean(sf["working_on"]) if sf.get("working_on") else "",
        "tasks": [],
        "for_you": bool(stuck is not None or helper_stuck or qs),
    }


def build_view(sections, now, horizon_days=DEFAULT_HORIZON_DAYS, stale=(), holds=None):
    """The rollup every tab reads. `holds` (holds.Holds) hides snoozed and done items from the timeline,
    the tasks and every count; view["held"] lists their rows, and view["item_keys"] every key reported."""
    seen = {}
    for k in ("needs", "questions", "quiet", "working", "idle"):
        for r in sections[k]:
            seen.setdefault(id(r), r)
    rows, orphans = [], []
    for r in seen.values():
        if r.get("parent") is not None:
            continue
        (orphans if looks_generated(r) else rows).append(r)
    up_warnings, up_items = [], []

    def reported(r, label, only_yours):
        out = []
        for src in [r] + r["children"]:
            for i in session_upcoming(src["status"] or {}, label, up_warnings):
                if i["yours"] or not only_yours:
                    i["key"] = item_key(r.get("sessionId") or "name-" + label, i)
                    out.append(i)
        return out
    for r in rows:
        up_items += reported(r, clean(r["name"]), False)
    gone = [r for r in stale if r.get("parent") is None and not looks_generated(r)]
    for r in gone:
        up_items += reported(r, clean(r["name"]), True)
    item_keys = {i["key"] for i in up_items}
    held_items = [i for i in up_items if holds and holds.status(i["key"], now)] if holds else []
    held_ids = {id(i) for i in held_items}
    up_items = [i for i in up_items if id(i) not in held_ids]
    rows = [make_row(r, now) for r in rows]
    timeline = build_timeline(up_items, now, horizon_days)
    held = timeline_rows(build_timeline(held_items, now, horizon_days)) if held_items else []
    by_name = {}
    for x in rows:
        by_name.setdefault(x["name"], x)
    for r in gone:
        name = clean(r["name"])
        if name not in by_name:
            by_name[name] = gone_row(r, name, now)
    counts = attach_tasks(timeline, by_name, now)
    rows_set = {id(x) for x in rows}
    extra = [x for x in by_name.values() if id(x) not in rows_set and x["tasks"]]
    for x in extra:
        x["for_you"] = True
    for x in rows:
        if x["tasks"]:
            x["for_you"] = True
    for_you = sorted((x for x in rows + extra if x["for_you"]), key=lambda x: (
        0 if (x["stuck"] is not None or x["helper_stuck"]) else 1,
        0 if has_overdue(x) else 1, -len(x["qs"]), x["updated"] or 0))
    rest = [x for x in rows if not x["for_you"]]
    return {
        "rows": rows, "for_you": for_you, "orphans": orphans,
        "quiet": [x for x in rest if x["quiet"]],
        "working": [x for x in rest if not x["quiet"] and x["working"]],
        "idle": [x for x in rest if not x["quiet"] and not x["working"]],
        "timeline": timeline, "up_warnings": up_warnings, "task_counts": counts,
        "held": held, "item_keys": item_keys,
    }


TASK_KINDS = ("overdue", "today", "undated")


def gone_row(r, name, now):
    """A Needs-you row for a session with no live row, so its due items stay visible."""
    sf = r["status"] or {}
    upd = to_epoch(sf.get("updated"))
    # A file-only record carries no session id; the status file names it, and a
    # group pin written while the session ran is keyed by it.
    sid = r.get("sessionId") or (sf.get("sessionId") if isinstance(sf.get("sessionId"), str) else None)
    return {
        "r": {"name": r["name"], "status": None, "children": [], "live": False, "state": "gone",
              "cwd": r.get("cwd"), "sessionId": sid, "last": r.get("last"), "ident": ident(r)},
        "name": name, "persona": clean(sf["persona"]) if sf.get("persona") else None,
        "stuck": None, "helper_stuck": [], "qs": [], "q_helpers": [], "working": False, "busy_helpers": 0,
        "quiet": None, "updated": upd if upd is not None else r.get("last"), "renamed_from": None,
        "working_on": "", "tasks": [], "for_you": False, "gone": True,
    }


def task_kind(row, now):
    """Which Needs-you kind an item is: overdue, today, undated, else None. Only your items count."""
    if not row["yours"]:
        return None
    if row["due"] is None:
        return "undated"
    if row["red"]:
        return "overdue"
    if row["due"].astimezone(LOCAL_TZ).date() == datetime.fromtimestamp(now, LOCAL_TZ).date():
        return "today"
    return None


def attach_tasks(timeline, by_name, now):
    """Put each qualifying item on every session that holds it; returns {kind: unique item count}."""
    counts = dict.fromkeys(TASK_KINDS, 0)
    for row in timeline_rows(timeline):
        kind = task_kind(row, now)
        if kind is None:
            continue
        counts[kind] += 1
        for name in row["sessions"]:
            x = by_name.get(clean(name))
            if x is not None:
                x["tasks"].append({"kind": kind, "what": row["what"], "due": row["due"],
                                   "has_time": row["has_time"], "item": row["kind"],
                                   "keys": list(row.get("keys", ())), "id": dedup_key(row)})
    order = {k: i for i, k in enumerate(TASK_KINDS)}
    for x in by_name.values():
        x["tasks"].sort(key=lambda t: (order[t["kind"]], t["due"].timestamp() if t["due"] else 0,
                                       t["what"].lower()))
    return counts


def has_overdue(x):
    return any(t["kind"] == "overdue" for t in x["tasks"])


def task_label(t):
    """(label, is_red) for a Needs-you task row; the text follows after ': '."""
    checkin = t["item"] == "checkin"
    when = t["due"].astimezone(LOCAL_TZ) if t["due"] else None
    if t["kind"] == "overdue":
        return "%s (%s)" % ("Check-in overdue" if checkin else "Overdue", when.strftime("%b %-d")), True
    if t["kind"] == "today":
        base = "Check in today" if checkin else "Due today"
        return base + (when.strftime(" %-I:%M %p") if t["has_time"] else ""), False
    return "Needs a date", False


# ---------------------------------------------------------------- coming up

DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
SHORT_SUFFIX = re.compile(r"\s+(Development|Assistant|Planning)$")
MARKERS_UTF8 = {"yours": ("✎", 1), "checkin": ("•", 1), "deadline": ("⏰", 2), "deploy": ("▲", 1),
                "meeting": ("●", 1), "milestone": ("◆", 1), "todo": ("•", 1)}
MARKERS_ASCII = {"yours": "+", "checkin": "-", "deadline": "!", "deploy": "^", "meeting": "o",
                 "milestone": "*", "todo": "-"}
BUCKET_LABELS = {"overdue": "OVERDUE", "this_week": "THIS WEEK", "next_2_wk": "NEXT 2 WK",
                 "later": "LATER", "no_date": "NO DATE"}
BUCKET_ORDER = ("overdue", "this_week", "next_2_wk", "later", "no_date")


def short_name(name):
    """Session name without a trailing Development/Assistant/Planning, cut to about 22 characters."""
    return trunc(SHORT_SUFFIX.sub("", clean(name)).strip(), 22, True)


def parse_due(value):
    """(aware datetime in the local zone; a date-only value is that day's midnight there, has_time) from YYYY-MM-DD or an ISO date-time."""
    if not isinstance(value, str):
        raise ValueError("due is not a string")
    text = value.strip()
    if DATE_ONLY.fullmatch(text):
        d = date_cls.fromisoformat(text)
        return datetime.combine(d, time_cls(0), tzinfo=LOCAL_TZ), False
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ), True


def parse_upcoming_item(raw):
    """One normalized item from a status-file entry; ValueError when it is malformed."""
    if not isinstance(raw, dict):
        raise ValueError("not an object")
    what = clean(raw.get("what")).strip() if isinstance(raw.get("what"), str) else ""
    if not what:
        raise ValueError("no what")
    due, has_time = (None, False)
    if raw.get("due") is not None:
        due, has_time = parse_due(raw["due"])
    yours = raw.get("yours", False)
    if not isinstance(yours, bool):
        raise ValueError("yours is not a boolean")
    kind = raw.get("kind")
    kind = kind if isinstance(kind, str) and kind in KINDS else "milestone"
    return {"what": what, "due": due, "has_time": has_time, "kind": kind, "yours": yours}


def session_upcoming(sf, label, warnings):
    """Items one status file reports: `upcoming` entries plus the older `remind` strings."""
    out = []
    ups = sf.get("upcoming")
    if ups is not None and not isinstance(ups, list):
        warnings.append("warning: upcoming from %s is not a list; skipped" % clean(label))
        ups = []
    for raw in ups or []:
        try:
            item = parse_upcoming_item(raw)
        except (ValueError, TypeError, OverflowError) as exc:
            warnings.append("warning: skipped a malformed upcoming item from %s (%s)"
                            % (clean(label), clean(exc)))
            continue
        item["session"] = label
        out.append(item)
    for text, _since in items_of(sf.get("remind")):
        out.append({"what": text, "due": None, "has_time": False, "kind": "todo", "yours": True,
                    "session": label})
    return out


def dedup_key(it):
    due = it["due"].astimezone(LOCAL_TZ).date().isoformat() if it["due"] else None
    return due, " ".join(it["what"].lower().split())


def item_key(session_id, it):
    """The identity a snooze or done hold is kept under: the owning session plus a hash of the item's
    text, date and kind. Editing any of them makes a new item."""
    due = it["due"].isoformat() if it["due"] else ""
    raw = "\x1f".join((" ".join(it["what"].lower().split()), due, it["kind"]))
    return "%s:%s" % (session_id, hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12])


def build_timeline(items, now, horizon_days=DEFAULT_HORIZON_DAYS):
    """Merge duplicates and sort every item into its bucket."""
    now_dt = datetime.fromtimestamp(now, LOCAL_TZ)
    today = now_dt.date()
    merged = {}
    for it in items:
        key = dedup_key(it)
        if key not in merged:
            merged[key] = dict(it, sessions=[it["session"]], keys=[it["key"]] if it.get("key") else [])
            continue
        row = merged[key]
        if it.get("key") and it["key"] not in row["keys"]:
            row["keys"].append(it["key"])
        if it["session"] not in row["sessions"]:
            row["sessions"].append(it["session"])
        row["yours"] = row["yours"] or it["yours"]
    buckets = {k: [] for k in BUCKET_ORDER}
    beyond = []
    for row in merged.values():
        row.pop("session", None)
        due = row["due"]
        row["past"], row["red"], row["beyond"] = False, False, False
        if due is None:
            if row["yours"]:
                row["bucket"] = "no_date"
                buckets["no_date"].append(row)
            continue
        delta = (due.date() - today).days
        row["past"] = delta < 0 or (row["has_time"] and due < now_dt)
        if row["past"]:
            if row["yours"] or row["kind"] == "deadline":
                row["bucket"], row["red"] = "overdue", True
            elif delta >= -1:
                row["bucket"] = "this_week"
            else:
                continue
        elif delta <= 7:
            row["bucket"] = "this_week"
        elif delta <= 21:
            row["bucket"] = "next_2_wk"
        else:
            row["bucket"] = "later"
            row["beyond"] = delta > horizon_days
        (beyond if row["beyond"] else buckets[row["bucket"]]).append(row)

    def order(r):
        return (r["due"].timestamp() if r["due"] else float("inf"), r["what"].lower())
    for lst in list(buckets.values()) + [beyond]:
        lst.sort(key=order)
    return {"buckets": buckets, "beyond": beyond, "horizon": horizon_days}


def timeline_rows(tl):
    return [r for k in BUCKET_ORDER for r in tl["buckets"][k]] + tl["beyond"]


def due_label(row, now):
    local = row["due"].astimezone(LOCAL_TZ)
    delta = (local.date() - datetime.fromtimestamp(now, LOCAL_TZ).date()).days
    base = "today" if delta == 0 else "tomorrow" if delta == 1 else local.strftime("%b %-d")
    return base + (local.strftime(" %-I:%M %p") if row["has_time"] else "")



def trunc(text, n, utf8):
    if len(text) <= n:
        return text
    if n <= 1:
        return text[:max(n, 0)]
    return text[:n - 1] + "…" if utf8 else text[:max(n - 3, 0)] + "..."



def is_utf8(stream):
    return "utf" in (getattr(stream, "encoding", "") or "").lower()


def collect_full(window_s, show_all, paths):
    """collect() plus the records, for callers (the app) that also plan a prune."""
    from . import prune
    now = time.time()
    warnings = []
    records = build_records(paths, now, warnings)
    notes = prune.restore_returned(paths["status"], records)
    if notes:
        warnings = []
        records = build_records(paths, now, warnings)
        warnings += notes
    sections, stale = classify(records, now, window_s, show_all)
    return sections, stale, now, warnings, records


def collect(window_s, show_all, paths):
    return collect_full(window_s, show_all, paths)[:4]
