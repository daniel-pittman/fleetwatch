"""`fleetwatch jump NAME` (or `--session-id ID`): raise a running session's iTerm2 tab.

The board's Enter key does the same through the same iTerm2 helpers; this is the command-line
form, and the target of a notification click. The name and ID are only compared against the
registry, and the tty reaches AppleScript as argv (see iterm.py).
"""
import argparse
import re
import sys
import time

from . import data, iterm

SID_RE = re.compile(r"[0-9a-f-]{8,36}")


def find(records, name=None, sid=None):
    """(record, None) for the one running top-level session that matches, else (None, reason)."""
    live = [r for r in records if r.get("parent") is None and r["pid_alive"]]
    if sid:
        hits = [r for r in live if r.get("sessionId") == sid]
        label = "session ID %s" % sid
    else:
        hits = [r for r in live if r["name"] == name]
        if not hits:
            hits = [r for r in live if str(r["name"]).casefold() == name.casefold()]
        label = "named %s" % name
    if not hits:
        return None, "no running session %s" % label
    if len(hits) > 1:
        return None, "%d running sessions %s; use --session-id" % (len(hits), label)
    return hits[0], None


def tty_of(rec, ctx):
    """(tty, None) when the record has an iTerm2 tab, else (None, reason)."""
    if ctx["iterm"] is None:
        return None, "could not reach iTerm2: %s" % (ctx.get("err") or "no answer")
    tty = ctx["ttys"].get(rec["pid"])
    if tty not in ctx["iterm"]:
        return None, "%s is not in an iTerm2 tab" % data.clean(rec["name"])
    return tty, None


def main(argv, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    p = argparse.ArgumentParser(prog="fleetwatch jump", description="Raise a running session's iTerm2 tab.")
    p.add_argument("name", nargs="?", help="the session's name (a leading - needs `--` before it)")
    p.add_argument("--session-id", metavar="ID", help="the session ID, for a name that repeats")
    args = p.parse_args(argv)
    if bool(args.name) == bool(args.session_id):
        p.error("give a session name or --session-id, not both and not neither")
    if args.session_id and not SID_RE.fullmatch(args.session_id):
        print("not a session ID: %s" % data.clean(args.session_id), file=err)
        return 2
    paths = data.default_paths()
    records = data.build_records(paths, time.time(), [])
    rec, reason = find(records, args.name, args.session_id)
    if rec is None:
        print(data.clean(reason), file=err)
        return 1
    tty, reason = tty_of(rec, iterm.jump_context())
    if tty is None:
        print(reason, file=err)
        return 1
    ok, reason = iterm.jump_to_tty(tty)
    if not ok:
        print("could not reach iTerm2: %s" % reason, file=err)
        return 1
    print("jumped to %s" % data.clean(rec["name"]), file=out)
    return 0
