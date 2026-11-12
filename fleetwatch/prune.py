"""`fleetwatch prune`: park finished sessions' status files in .pruned/, and bring them back on return.

Nothing here deletes a status file. A file is a candidate only when its session is not live, has
been quiet past the window, holds nothing waiting on the user, and is not on the keep list.
"""
import argparse
import glob
import os
import re
import sys
import time

from . import config, data

PRUNED = ".pruned"
SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")
COLLIDED = re.compile(r"^(.*)\.\d{9,}\.json$")


def pruned_dir(status):
    return os.path.join(status, PRUNED)


def transcript_mtime(projects, sid):
    """Newest mtime of ~/.claude/projects/*/<sid>.jsonl, or None."""
    if not projects or not isinstance(sid, str) or not SAFE_ID.fullmatch(sid):
        return None
    best = None
    for path in glob.glob(os.path.join(glob.escape(projects), "*", sid + ".jsonl")):
        try:
            best = max(best or 0.0, os.path.getmtime(path))
        except OSError:
            continue
    return best


def read_folder(folder):
    """[(path, parsed status dict)] for each readable status file directly in folder."""
    out = []
    for path in sorted(glob.glob(os.path.join(glob.escape(folder), "*.json"))):
        if not os.path.isfile(path):
            continue
        try:
            d = data.load_json(path)
        except Exception:
            continue
        if isinstance(d, dict) and isinstance(d.get("session"), str) \
                and data.to_epoch(d.get("updated")) is not None:
            out.append((path, d))
    return out


def sid_of(d):
    sid = d.get("sessionId")
    return sid if isinstance(sid, str) and sid else None


def open_items(d):
    """(questions, upcoming + remind) counts: everything that may be waiting on the user."""
    ups = d.get("upcoming")
    return (len(data.items_of(d.get("questions"))),
            (len(ups) if isinstance(ups, list) else 0) + len(data.items_of(d.get("remind"))))


def kept(name, keep):
    return name.strip().casefold() in {k.strip().casefold() for k in keep}


def last_activity(d, projects):
    return max(x for x in (data.to_epoch(d.get("updated")),
                           transcript_mtime(projects, sid_of(d))) if x is not None)


class Plan:
    """What a prune would do right now: candidates, files kept for open items, counts left alone."""

    def __init__(self, paths, now, settings, candidates=(), kept_open=(), left=None):
        self.paths, self.now, self.settings = paths, now, settings
        self.candidates, self.kept_open = list(candidates), list(kept_open)
        self.left = dict(left or {"live": 0, "recent": 0, "keep": 0})


def make_plan(paths, now, records, settings):
    """Classify every status file. `records` come from data.build_records."""
    live_ids = {r["sessionId"] for r in records if r["live"] and r["sessionId"]}
    live_names = {r["name"] for r in records if r["live"]}
    quiet_s, keep = settings["quiet_s"], settings["keep"]
    plan = Plan(paths, now, settings)
    for path, d in read_folder(paths["status"]):
        name = d["session"]
        if sid_of(d) in live_ids or name in live_names:
            plan.left["live"] += 1
            continue
        if kept(name, keep):
            plan.left["keep"] += 1
            continue
        last = last_activity(d, paths.get("projects"))
        if now - last <= quiet_s:
            plan.left["recent"] += 1
            continue
        q, u = open_items(d)
        entry = {"name": data.clean(name), "path": path, "sessionId": sid_of(d), "last": last,
                 "questions": q, "upcoming": u,
                 "reason": "not live, quiet for %s" % data.human(now - last)}
        (plan.kept_open if q or u else plan.candidates).append(entry)
    for lst in (plan.candidates, plan.kept_open):
        lst.sort(key=lambda e: e["last"])
    return plan


def live_now(paths, now, sid, name):
    """Cheap fresh liveness for one session straight from the registry, right before a move."""
    for g in data.read_registry(paths["sessions"]):
        if (sid and g.get("sessionId") == sid) or g.get("name") == name:
            if data.pid_alive(g.get("pid")):
                return True
            acts = [data.to_epoch(g.get("statusUpdatedAt")), data.to_epoch(g.get("updatedAt"))]
            acts = [a for a in acts if a]
            if g.get("status") == "busy" and acts and now - max(acts) <= data.BUSY_RECENT_S:
                return True
    return False


def apply(plan, now=None):
    """Move the listed candidates that are still candidates. Returns (moved names, [(name, why skipped)]).

    The plan is re-derived from fresh records first, so a session that came back since the
    listing drops out, and only files the user was shown are moved. Each file is then checked
    once more, against the registry and its own contents, immediately before its move.
    """
    now = time.time() if now is None else now
    paths, settings = plan.paths, plan.settings
    warnings = []
    fresh = make_plan(paths, now, data.build_records(paths, now, warnings), settings)
    fresh_paths = {c["path"] for c in fresh.candidates}
    moved, skipped = [], []
    dest = pruned_dir(paths["status"])
    for c in plan.candidates:
        if c["path"] not in fresh_paths:
            skipped.append((c["name"], "no longer a candidate"))
            continue
        if live_now(paths, now, c["sessionId"], c["name"]):
            skipped.append((c["name"], "went live"))
            continue
        try:
            d = data.load_json(c["path"])
            if open_items(d) != (0, 0) or now - last_activity(d, paths.get("projects")) <= settings["quiet_s"]:
                raise ValueError("changed")
        except Exception:
            skipped.append((c["name"], "changed since the listing"))
            continue
        os.makedirs(dest, exist_ok=True)
        target = os.path.join(dest, os.path.basename(c["path"]))
        if os.path.exists(target):
            target = os.path.join(dest, "%s.%d.json" % (os.path.basename(c["path"])[:-5], int(now)))
        try:
            os.replace(c["path"], target)
        except OSError as exc:
            skipped.append((c["name"], "could not move (%s)" % type(exc).__name__))
            continue
        moved.append(c["name"])
    return moved, skipped


def original_name(fname):
    m = COLLIDED.match(fname)
    return (m.group(1) + ".json") if m else fname


def read_pruned(status):
    """[(path, dict)] for the parked files."""
    return read_folder(pruned_dir(status))


def _newer_exists(status, d):
    """True when the folder already holds a file for this session at least as new as d."""
    sid, mine = sid_of(d), data.to_epoch(d["updated"])
    for _path, other in read_folder(status):
        same = (sid_of(other) == sid) if sid else (other["session"] == d["session"])
        if same and data.to_epoch(other["updated"]) >= mine:
            return True
    return False


def _put_back(status, path, d):
    """Move one parked file back unless a file for the session is as new. True when moved."""
    target = os.path.join(status, original_name(os.path.basename(path)))
    if _newer_exists(status, d):
        return False
    if os.path.exists(target):
        try:
            old = data.load_json(target)
            # A different session's file under the same name is never overwritten.
            if not isinstance(old, dict) or old.get("session") != d["session"] \
                    or sid_of(old) != sid_of(d):
                return False
        except Exception:
            return False
    os.replace(path, target)
    return True


def restore_returned(status, records):
    """Move back the parked file of every session that is live again; returns one note per file."""
    notes = []
    live = [r for r in records if r["live"]]
    for path, d in read_pruned(status):
        sid = sid_of(d)
        back = any(r["sessionId"] == sid for r in live) if sid \
            else any(r["name"] == d["session"] for r in live)
        if back and _put_back(status, path, d):
            notes.append("restored status file for %s (session is live again)" % data.clean(d["session"]))
    return notes


def restore_named(status, name):
    """Hand restore. Returns (message, ok)."""
    want = name.strip().casefold()
    found = [(p, d) for p, d in read_pruned(status)
             if d["session"].casefold() == want
             or os.path.basename(original_name(os.path.basename(p)))[:-5].casefold() == want]
    if not found:
        return "no pruned status file for %s" % name, False
    path, d = max(found, key=lambda x: data.to_epoch(x[1]["updated"]))
    if _put_back(status, path, d):
        return "restored %s" % data.clean(d["session"]), True
    return "%s already has a status file that is as new; the parked copy stays in %s" % (
        data.clean(d["session"]), PRUNED), False


# ---------------------------------------------------------------- the command line

def listing(plan, quiet_label):
    lines = ["Ready to prune (%d), not live and quiet for over %s:" % (len(plan.candidates), quiet_label)]
    for c in plan.candidates:
        lines.append("  %-42s last active %-7s %s" % (data.trunc(c["name"], 42, True), data.day(c["last"]),
                                                      c["reason"]))
    if not plan.candidates:
        lines.append("  none")
    if plan.kept_open:
        lines += ["", "Kept: has open items (%d), never pruned:" % len(plan.kept_open)]
        for c in plan.kept_open:
            lines.append("  %-42s last active %-7s %s" % (
                data.trunc(c["name"], 42, True), data.day(c["last"]),
                ", ".join(x for x in (data.plural(c["questions"], "question") if c["questions"] else "",
                                      data.plural(c["upcoming"], "upcoming item") if c["upcoming"] else "") if x)))
    left = plan.left
    lines += ["", "Left alone: %d live, %d recent, %d on the keep list" % (
        left["live"], left["recent"], left["keep"])]
    return "\n".join(lines)


def build_parser():
    p = argparse.ArgumentParser(prog="fleetwatch prune", description="Move finished sessions' status files "
                                "to .pruned/. A dry run unless --yes is given; nothing is ever deleted.")
    p.add_argument("--yes", action="store_true", help="move the candidates")
    p.add_argument("--restore", metavar="NAME", help="move one pruned file back")
    p.add_argument("--quiet", type=data.parse_window, metavar="WINDOW",
                   help="how long a session must be quiet, e.g. 24h or 3d (default from prune.toml, else 24h)")
    p.add_argument("--command")
    p.add_argument("--sessions-dir")
    p.add_argument("--jobs-dir")
    p.add_argument("--status-dir")
    return p


def main(argv):
    args = build_parser().parse_args(argv)
    paths = data.default_paths()
    for key, val in (("command", args.command), ("sessions", args.sessions_dir),
                     ("jobs", args.jobs_dir), ("status", args.status_dir)):
        if val:
            paths[key] = val
    if args.restore:
        msg, ok = restore_named(paths["status"], args.restore)
        print(msg)
        return 0 if ok else 1
    settings, warning = config.load_prune()
    if warning:
        print("warning: " + warning, file=sys.stderr)
    if args.quiet:
        settings["quiet_s"] = args.quiet
    now, warnings = time.time(), []
    records = data.build_records(paths, now, warnings)
    for w in warnings:
        print(w, file=sys.stderr)
    plan = make_plan(paths, now, records, settings)
    print(listing(plan, data.human(settings["quiet_s"])))
    if not args.yes:
        print("\nDry run: nothing moved. Add --yes to move the %d to %s/" % (
            len(plan.candidates), os.path.join(paths["status"], PRUNED)))
        return 0
    moved, skipped = apply(plan, now)
    print("\nMoved %d to %s/. `fleetwatch prune --restore NAME` brings one back." % (
        len(moved), os.path.join(paths["status"], PRUNED)))
    for name, why in skipped:
        print("  skipped %s: %s" % (name, why))
    return 0
