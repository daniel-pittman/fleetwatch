"""`fleetwatch restore NAME|ID|FOLDER` and `fleetwatch restore --list`: copy a transcript that survives only in
the archive back into the projects folder, named or not, without starting it.

A query matches a session's custom name exactly, then a session ID prefix, then part of a name, then part
of its project folder's name; the first of those that matches anything decides. More than one match lists
them and stops. The copy goes through resume.copy_back, the path `fleetwatch resume` uses: the archive
keeps its copy and nothing in the projects folder is ever overwritten.

A session folder whose side files (subagents/, tool-results/) survive with no main transcript in either
place cannot be restored, and is always reported as such, never skipped.
"""
import argparse
import os
import sys
import time

from . import archive, data, resume

UNRECOVERABLE = "the main transcript was deleted before it was archived; only side files remain"


def size_label(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0


def _row(sid, s):
    path = s["path"]
    try:
        size = os.stat(path).st_size
    except OSError:
        size = s.get("size") or 0
    return {"sid": sid, "title": s.get("title"), "cwd": s.get("cwd"), "path": path,
            "slug": os.path.basename(os.path.dirname(path)), "mtime": s.get("mtime"), "size": size,
            "archived": bool(s.get("archived"))}


def rows(paths):
    """(archive-only sessions, sessions in the projects folder), each newest first. Helper runs (-p and
    SDK sessions, sidechains) are left out, as everywhere else."""
    sessions = resume.scan(paths)
    out = sorted((_row(sid, s) for sid, s in sessions.items()), key=lambda r: -(r["mtime"] or 0))
    return [r for r in out if r["archived"]], [r for r in out if not r["archived"]]


def orphans(paths):
    """Session folders, in the projects folder or the archive, whose main transcript is in neither."""
    projects, kept = paths["projects"], paths.get("archive")
    mains = set()
    for root in (projects, kept):
        if root:
            mains.update(os.path.basename(rel)[:-len(".jsonl")] for rel in archive.main_transcripts(root))
    found = {}
    for root, where in ((projects, "the projects folder"), (kept, "the archive")):
        if not root:
            continue
        try:
            folders = list(os.scandir(root))
        except OSError:
            continue
        for project in folders:
            try:
                if not project.is_dir(follow_symlinks=False):
                    continue
                inside = list(os.scandir(project.path))
            except OSError:
                continue
            for d in inside:
                if not (archive.SESSION_ID.fullmatch(d.name) and d.is_dir(follow_symlinks=False)) or d.name in mains:
                    continue
                r = found.setdefault((project.name, d.name), {
                    "sid": d.name, "slug": project.name, "title": None, "where": [], "files": 0, "size": 0,
                    "mtime": None})
                r["where"].append(where)
                if len(r["where"]) > 1:
                    continue
                for top, _dirs, files in os.walk(d.path):
                    for name in files:
                        try:
                            st = os.stat(os.path.join(top, name), follow_symlinks=False)
                        except OSError:
                            continue
                        r["files"] += 1
                        r["size"] += st.st_size
                        r["mtime"] = max(r["mtime"] or 0, st.st_mtime)
    return sorted(found.values(), key=lambda r: -(r["mtime"] or 0))


def match(query, candidates):
    """The first band with a hit: exact name, session ID prefix, part of a name, part of a folder name."""
    q = query.strip()
    f = q.casefold()
    bands = (lambda r: (r["title"] or "").casefold() == f,
             lambda r: bool(resume.SID_QUERY.match(f)) and r["sid"].startswith(f),
             lambda r: f in (r["title"] or "").casefold(),
             lambda r: f in r["slug"].casefold())
    for band in bands:
        hits = [r for r in candidates if band(r)]
        if hits:
            return hits
    return []


def name_of(r):
    return data.clean(r["title"]) if r["title"] else "unnamed session %s" % r["sid"][:8]


def listing(found, now):
    lines = []
    for r in found:
        lines.append("  %s  %s  %s  %s  %s" % (
            name_of(r), data.date_label(r["mtime"], now), size_label(r["size"]), r["sid"][:8],
            resume.short_folder(r["cwd"]) if r.get("cwd") else data.clean(r["slug"])))
    return "\n".join(lines)


def orphan_lines(found, now):
    return "\n".join("  %s in %s, %s, last written %s: %s (in %s)" % (
        r["sid"], data.clean(r["slug"]), data.plural(r["files"], "side file"), data.date_label(r["mtime"], now),
        UNRECOVERABLE, " and ".join(r["where"])) for r in found)


def restore(r, paths, out):
    """Copy one archive-only session back. Returns the exit code."""
    try:
        dest, copied = resume.copy_back(r["path"], r["sid"], paths)
    except OSError as e:
        print("could not copy %s back from the archive: %s" % (name_of(r), data.clean(e)[:200]), file=out)
        return 1
    if not copied:
        print("%s: the projects folder already has %s, so it was left as it is" % (name_of(r), dest), file=out)
        return 0
    print("restored %s to %s; the archive keeps its copy" % (name_of(r), dest), file=out)
    cwd = r.get("cwd")
    if cwd and resume.usable_folder(cwd) and os.path.isdir(cwd):
        print("to resume it: %s" % resume.shell_line({"cwd": cwd, "sid": r["sid"]}), file=out)
    elif cwd:
        print("its folder %s no longer exists" % data.clean(cwd), file=out)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="fleetwatch restore",
        description="Copy a transcript that survives only in the archive back into the projects folder, "
                    "without starting it. Matches a custom name, a session ID prefix, or part of a project "
                    "folder's name. No name, or --list, shows what can be restored.")
    p.add_argument("name", nargs="*", help="a session name, a session ID prefix, or part of a folder name")
    p.add_argument("--list", action="store_true", help="list archive-only sessions and stop")
    p.add_argument("--projects-dir")
    p.add_argument("--archive-dir")
    return p


def main(argv, out=None):
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    paths = data.default_paths()
    for key, val in (("projects", args.projects_dir), ("archive", args.archive_dir)):
        if val:
            paths[key] = os.path.abspath(os.path.expanduser(val))
    try:
        out.reconfigure(errors="replace")
    except Exception:  # pragma: no cover
        pass
    now = time.time()
    kept, live = rows(paths)
    lost = orphans(paths)
    query = " ".join(args.name).strip()
    if args.list or not query:
        if kept:
            print("in the archive only (%s):" % data.plural(len(kept), "session"), file=out)
            print(listing(kept, now), file=out)
        else:
            print("nothing to restore: every archived transcript is also in the projects folder", file=out)
        if lost:
            print("cannot be restored (%d):" % len(lost), file=out)
            print(orphan_lines(lost, now), file=out)
        return 0
    hits = match(query, kept)
    if len(hits) > 1:
        print("%d archived sessions match %r; give more of the name or the session ID:" % (len(hits), query),
              file=out)
        print(listing(hits, now), file=out)
        return 2
    if hits:
        return restore(hits[0], paths, out)
    here = match(query, live)
    if here:
        print("already in the projects folder, so there is nothing to restore:", file=out)
        print(listing(here, now), file=out)
        return 0
    gone = match(query, lost)
    if gone:
        print("cannot restore %r:" % query, file=out)
        print(orphan_lines(gone, now), file=out)
        return 1
    print("nothing in the archive matches %r. `fleetwatch restore --list` shows what can be restored." % query,
          file=out)
    return 1
