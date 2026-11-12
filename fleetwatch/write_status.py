"""`fleetwatch write-status`: a session writes its own status file, keyed by its stable sessionId."""
import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime

from . import data, watches
from .config import atomic_write

FIELDS = ("persona", "cadence_min", "working_on", "questions", "upcoming", "remind", "watches")
MAX_INPUT = 256 * 1024
HELPER_ENTRYPOINT = "sdk-"   # Claude Code records this for -p and SDK runs (resume.HELPER_ENTRYPOINT)


class StatusError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "session"


def find_session(sessions_dir, ppids, start_pid):
    """(name, sessionId) of the first process at or above start_pid that has a registry file."""
    rec = find_record(sessions_dir, ppids, start_pid)
    return data.clean(rec["name"]).strip(), rec["sessionId"]


def find_record(sessions_dir, ppids, start_pid):
    """The registry record (a dict) of the first process at or above start_pid that names a session."""
    seen, pid = set(), start_pid
    while isinstance(pid, int) and pid > 1 and pid not in seen and len(seen) < 512:
        seen.add(pid)
        path = os.path.join(sessions_dir, "%d.json" % pid)
        if os.path.isfile(path):
            try:
                d = data.load_json(path)
            except Exception:
                d = None
            if isinstance(d, dict) and isinstance(d.get("name"), str) and d["name"].strip() \
                    and isinstance(d.get("sessionId"), str) and d["sessionId"]:
                return d
        pid = ppids.get(pid)
    if getattr(ppids, "unreadable", False):
        raise StatusError("could not read the process table (ps failed or timed out; the machine may "
                          "be busy); try again", 2)
    raise StatusError("no Claude session found above this process; run it from inside a session", 2)


def helper_reason(rec):
    """Why this registry record is a helper session (a message naming the fix), or None.

    Helpers are worktree lanes and run ids (data.HELPER_NAME), headless `-p` and SDK runs, and a session
    whose name Claude Code derived itself. The session that started a helper reports for it."""
    name = data.clean(rec["name"]).strip()
    if data.HELPER_NAME.search(name) or str(rec.get("entrypoint") or "").startswith(HELPER_ENTRYPOINT):
        return "helper sessions don't write status; the session that started you does"
    source = rec.get("nameSource")
    if isinstance(source, str) and source and source != "user":
        return "name this session with /rename to report status (only named sessions write status)"
    return None


def _text(value, field):
    if not isinstance(value, str):
        raise StatusError("%s must be a string" % field)
    return data.clean(value).strip()


def _when(value, field):
    if data.to_epoch(value) is None or not isinstance(value, str):
        raise StatusError("%s must be an ISO 8601 time" % field)
    return value


UPCOMING_KEYS = ("what", "due", "kind", "yours")
# Keys sessions reach for when they mean `due`; naming the fix teaches the session at once
DUE_SYNONYMS = ("when", "date", "by", "deadline", "due_date", "on")


def _check_upcoming_shape(x):
    """Refuse an upcoming item a reader would misread: an unknown key (it would be dropped, so
    a date under the wrong name is lost), an unknown kind, or one of the owner's items with no date.
    Every item of theirs carries a date."""
    if not isinstance(x, dict):
        return  # parse_upcoming_item names this one
    for key in sorted(set(x) - set(UPCOMING_KEYS)):
        hint = "; did you mean due?" if key in DUE_SYNONYMS else ""
        raise StatusError("bad upcoming item: unknown key %r (allowed: %s)%s"
                          % (data.clean(key), ", ".join(UPCOMING_KEYS), hint))
    kind = x.get("kind")
    if kind is not None and kind not in data.KINDS:
        raise StatusError("bad upcoming item: kind %r is not one of %s"
                          % (data.clean(str(kind)), ", ".join(sorted(data.KINDS))))
    if x.get("yours") is True and x.get("due") is None:
        what = data.clean(x.get("what")) if isinstance(x.get("what"), str) else "?"
        raise StatusError("bad upcoming item %r: an item of yours needs a due date. If the real "
                          "date is unknown, use \"kind\": \"checkin\" with the day you should look "
                          "at it again" % what)


def validate(payload):
    """The cleaned fields from stdin; StatusError when the payload is malformed."""
    if not isinstance(payload, dict):
        raise StatusError("the payload must be a JSON object")
    extra = sorted(set(payload) - set(FIELDS))
    if extra:
        raise StatusError("unknown field(s): %s" % ", ".join(data.clean(k) for k in extra))
    out = {}
    for key, val in payload.items():
        if key in ("persona", "working_on"):
            out[key] = _text(val, key)
        elif key == "cadence_min":
            if isinstance(val, bool) or not isinstance(val, (int, float)) or val <= 0:
                raise StatusError("cadence_min must be a positive number")
            out[key] = val
        elif key == "remind":
            if not isinstance(val, list):
                raise StatusError("remind must be a list")
            out[key] = [_text(x, "remind entry") for x in val]
        elif key == "questions":
            if not isinstance(val, list):
                raise StatusError("questions must be a list")
            qs = []
            for x in val:
                if isinstance(x, dict):
                    if set(x) - {"q", "since"}:
                        raise StatusError("a question object takes only q and since")
                    q = {"q": _text(x.get("q"), "question")}
                    if x.get("since") is not None:
                        q["since"] = _when(x["since"], "since")
                    qs.append(q)
                else:
                    qs.append(_text(x, "question"))
            out[key] = qs
        elif key == "upcoming":
            if not isinstance(val, list):
                raise StatusError("upcoming must be a list")
            items = []
            for x in val:
                _check_upcoming_shape(x)
                try:
                    item = data.parse_upcoming_item(x)
                except (ValueError, TypeError) as exc:
                    raise StatusError("bad upcoming item (%s)" % data.clean(exc)) from None
                clean_item = {"what": item["what"], "kind": item["kind"], "yours": item["yours"]}
                if x.get("due") is not None:
                    clean_item["due"] = x["due"]
                items.append(clean_item)
            out[key] = items
        elif key == "watches":
            try:
                out[key] = watches.validate(val)
            except watches.WatchError as exc:
                raise StatusError(str(exc)) from None
    return out


def dropped_watches(target, doc):
    """How many watches the file at target declares that doc leaves out (each write replaces the file)."""
    if "watches" in doc:
        return 0
    try:
        old = data.load_json(target)
    except (OSError, ValueError):
        return 0
    return len(old["watches"]) if isinstance(old, dict) and isinstance(old.get("watches"), list) else 0


def build_document(fields, name, session_id, now=None):
    doc = {"session": name, "sessionId": session_id,
           "updated": (now or datetime.now().astimezone()).isoformat(timespec="seconds")}
    doc.update(fields)
    return doc


def write_document(folder, doc):
    """Write doc atomically as <slug>.json, then remove other files holding the same sessionId."""
    target = os.path.join(folder, slug(doc["session"]) + ".json")
    atomic_write(target, json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    removed = []
    for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
        if os.path.abspath(path) == os.path.abspath(target):
            continue
        try:
            other = data.load_json(path)
        except Exception:
            continue
        if isinstance(other, dict) and other.get("sessionId") == doc["sessionId"]:
            os.unlink(path)
            removed.append(path)
    return target, removed


def build_parser():
    p = argparse.ArgumentParser(
        prog="fleetwatch write-status",
        description="Write this session's status file from JSON on stdin (persona, cadence_min, "
                    "working_on, questions, upcoming, remind, watches). Absent fields are not written.")
    p.add_argument("--print-path", action="store_true", help="print the file it would write and exit")
    p.add_argument("--dry-run", action="store_true", help="print the JSON it would write and exit")
    p.add_argument("--force", action="store_true",
                   help="write even when this session looks like a helper (worktree lane, headless run, unnamed)")
    p.add_argument("--sessions-dir")
    p.add_argument("--status-dir")
    return p


def run(argv, stdin, out, err, paths=None, ppids=None, start_pid=None, now=None):
    args = build_parser().parse_args(argv)
    paths = dict(paths or data.default_paths())
    for key, val in (("sessions", args.sessions_dir), ("status", args.status_dir)):
        if val:
            paths[key] = val
    try:
        if ppids is None:
            ppids = data.Ancestry(paths.get("ps"))
        rec = find_record(paths["sessions"], ppids, os.getpid() if start_pid is None else start_pid)
        name, sid = data.clean(rec["name"]).strip(), rec["sessionId"]
        target = os.path.join(paths["status"], slug(name) + ".json")
        if args.print_path:
            print(target, file=out)
            return 0
        reason = None if args.force else helper_reason(rec)
        if reason:
            raise StatusError("%s (--force writes anyway)" % reason, 3)
        raw = stdin.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise StatusError("the payload is too large")
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise StatusError("stdin is not valid JSON (%s)" % data.clean(exc)[:60]) from None
        doc = build_document(validate(payload), name, sid, now)
        if args.dry_run:
            print(json.dumps(doc, indent=1, ensure_ascii=False), file=out)
            return 0
        dropped = dropped_watches(target, doc)
        target, _ = write_document(paths["status"], doc)
        print("wrote %s" % target, file=out)
        if dropped:
            print("fleetwatch write-status: note: this write drops the %s the last status declared; send "
                  "\"watches\" on every write to keep them" % data.plural(dropped, "watch", "watches"), file=err)
        return 0
    except StatusError as exc:
        print("fleetwatch write-status: %s" % exc, file=err)
        return exc.code
    except OSError as exc:
        print("fleetwatch write-status: %s" % data.clean(exc)[:100], file=err)
        return 1


def main(argv):
    return run(argv, sys.stdin, sys.stdout, sys.stderr)
