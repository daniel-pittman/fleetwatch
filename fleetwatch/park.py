"""`fleetwatch park`: mark a session finished for now, with a note saying what it waits on.

Parked state has its own file under the config folder. `fleetwatch write-status` and `fleetwatch prune` rewrite
and move files in the status folder, so a parked note kept there could be lost to either.
"""
import argparse
import contextlib
import fcntl
import json
import os
import sys
import time

from . import config, data, write_status

MAX_NOTE = 500


class ParkedError(Exception):
    """parked.json exists but cannot be read; it is left alone rather than replaced."""


def load(path=None):
    """{sessionId: {"name", "note", "parked_at"}}; ParkedError when the file does not parse."""
    path = path or config.parked_path()
    try:
        doc = data.load_json(path)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ParkedError("%s is not readable (%s); fix or move it" % (
            os.path.basename(path), data.clean(exc)[:60])) from None
    sessions = doc.get("sessions") if isinstance(doc, dict) else None
    if not isinstance(sessions, dict):
        raise ParkedError("%s has no sessions table; fix or move it" % os.path.basename(path))
    return {k: v for k, v in sessions.items() if isinstance(k, str) and isinstance(v, dict)}


@contextlib.contextmanager
def _locked(path):
    # Sessions park themselves independently of a running fleetwatch: without the lock, two writers that
    # read the same file would each drop the other's note.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "w", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def _save(path, sessions):
    config.atomic_write(path, json.dumps({"sessions": sessions}, indent=1, ensure_ascii=False) + "\n")


def park(sid, name, note, path=None, now=None):
    """Park a session, or replace the note of one already parked. Returns the cleaned note."""
    path = path or config.parked_path()
    note = data.clean(note or "").strip()[:MAX_NOTE]
    with _locked(path):
        sessions = load(path)
        sessions[sid] = {"name": data.clean(name).strip(), "note": note,
                         "parked_at": time.time() if now is None else now}
        _save(path, sessions)
    return note


def unpark(sid, path=None):
    """Remove a session from the parked file. Returns its record, or None when it was not parked."""
    path = path or config.parked_path()
    with _locked(path):
        sessions = load(path)
        gone = sessions.pop(sid, None)
        if gone is not None:
            _save(path, sessions)
    return gone


# ---------------------------------------------------------------- the command line

def _parser(prog, description, with_note):
    p = argparse.ArgumentParser(prog=prog, description=description)
    p.add_argument("name", nargs="*", help="part of a session name, or a session ID prefix; "
                                           "without one, the session this command runs inside")
    if with_note:
        p.add_argument("--note", default="", help="what the session is waiting on")
    p.add_argument("--sessions-dir")
    p.add_argument("--status-dir")
    p.add_argument("--projects-dir")
    p.add_argument("--archive-dir")
    return p


def _paths(args):
    paths = data.default_paths()
    for key, val in (("sessions", args.sessions_dir), ("status", args.status_dir),
                     ("projects", args.projects_dir), ("archive", args.archive_dir)):
        if val:
            paths[key] = val
    return paths


def _calling_session(paths):
    ppids = data.Ancestry(paths.get("ps"))
    name, sid = write_status.find_session(paths["sessions"], ppids, os.getpid())
    return sid, name


def _named_session(query, pool):
    """(sessionId, name) for a loose name; (None, None) when it finds no one session, after saying so."""
    from . import resume
    matches = resume.match(query, pool)
    if not matches:
        print("no session matches %r" % query, file=sys.stderr)
        return None, None
    if len(matches) > 1:
        chosen = resume.choose(matches, sys.stdout)
        if chosen is None:
            return None, None
        return chosen["sid"], chosen["name"]
    return matches[0]["sid"], matches[0]["name"]


def main(argv):
    args = _parser("fleetwatch park", "Mark a session finished for now, with a note saying what it waits on. "
                                 "Parked sessions list first on fleetwatch's Resume tab.", True).parse_args(argv)
    paths = _paths(args)
    try:
        if args.name:
            from . import resume
            entries = resume.load(paths).entries()
            sid, name = _named_session(" ".join(args.name), entries)
            if sid is None:
                return 1
        else:
            sid, name = _calling_session(paths)
        note = park(sid, name, args.note)
    except write_status.StatusError as exc:
        print("fleetwatch park: %s, or name one: fleetwatch park NAME --note \"...\"" % exc, file=sys.stderr)
        return exc.code
    except (ParkedError, OSError) as exc:
        print("fleetwatch park: %s" % data.clean(exc)[:160], file=sys.stderr)
        return 1
    print("parked %s%s" % (data.clean(name), ": " + note if note else ""))
    return 0


def unpark_main(argv):
    args = _parser("fleetwatch unpark", "Take a session off the parked list. Resuming a session does this "
                                   "by itself.", False).parse_args(argv)
    paths = _paths(args)
    try:
        if args.name:
            from . import resume
            parked = [e for e in resume.load(paths).entries() if e["parked"]]
            sid, name = _named_session(" ".join(args.name), parked)
            if sid is None:
                return 1
        else:
            sid, name = _calling_session(paths)
        gone = unpark(sid)
    except write_status.StatusError as exc:
        print("fleetwatch unpark: %s, or name one: fleetwatch unpark NAME" % exc, file=sys.stderr)
        return exc.code
    except (ParkedError, OSError) as exc:
        print("fleetwatch unpark: %s" % data.clean(exc)[:160], file=sys.stderr)
        return 1
    print(("unparked %s" if gone is not None else "%s was not parked") % data.clean(name))
    return 0
