"""`fleetwatch archive run | install | uninstall | status`: copy Claude Code transcripts into an archive that
Claude Code's retention sweep cannot reach.

The sweep deletes transcripts on its own schedule, so the archive never mirrors a deletion: nothing here
removes a file from the archive, and a file in the archive is never replaced by an older or smaller one.
What is copied: each project folder's top-level `*.jsonl` (the main transcripts), every file under a
session's own folder beside it (`<sessionId>/subagents/`, `<sessionId>/tool-results/`), and
`history.jsonl` from the folder above the projects folder. Names with `.tmp.` in them are Claude Code's
half-written files and are skipped.

The archive's projects folder is the one `fleetwatch resume` reads (data.default_paths()["archive"]), and
history.jsonl goes beside it. One run at a time holds an flock in fleetwatch's state folder; a second run
exits at once. Each finished run writes <state>/archive/last_run.json and appends a line to
<state>/archive/archive.log.
"""
import argparse
import errno
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import config, data, schedule, settings

LEGACY_LABEL = "local.claude-session-archive"
JOB = schedule.ARCHIVE
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MAIN_TRANSCRIPT = re.compile(SESSION_ID.pattern + r"\.jsonl")
# A last run older than this many intervals is stale.
STALE_FACTOR = 2
RSYNC_TIMEOUT_S = 3 * 3600
# rsync exits 24 when source files vanished during the copy, which live sessions do; not a failure.
RSYNC_VANISHED = 24
STATE_KEYS = ("started", "ended", "ok", "copied", "transcripts_live", "transcripts_archived", "error")


# ---------------------------------------------------------------- where things are

def state_dir():
    return Path(settings.state_dir()) / "archive"


def state_path():
    """The last run's record: {started, ended, ok, copied, transcripts_live, transcripts_archived, error}."""
    return state_dir() / "last_run.json"


def log_path():
    return state_dir() / "archive.log"


def lock_path():
    return state_dir() / "run.lock"


def archive_root():
    return settings.archive_root()


def roots(paths=None):
    """(projects folder, archive projects folder, history source, history destination)."""
    paths = paths or data.default_paths()
    src, dest = paths["projects"], paths["archive"]
    return (src, dest, os.path.join(os.path.dirname(os.path.normpath(src)), "history.jsonl"),
            os.path.join(os.path.dirname(os.path.normpath(dest)), "history.jsonl"))


# ---------------------------------------------------------------- what to copy

def _skipped(name):
    return ".tmp." in name


def _regular(entry):
    try:
        return entry.is_file(follow_symlinks=False)
    except OSError:
        return False


def _folder(entry):
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def transcript_files(root):
    """Paths relative to root of every file the archive keeps from a projects folder."""
    try:
        projects = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return
    for project in projects:
        if not _folder(project):
            continue
        try:
            inside = sorted(os.scandir(project.path), key=lambda e: e.name)
        except OSError:
            continue
        for f in inside:
            if _skipped(f.name):
                continue
            if f.name.endswith(".jsonl") and _regular(f):
                yield os.path.join(project.name, f.name)
            elif SESSION_ID.fullmatch(f.name) and _folder(f):
                for top, dirs, files in os.walk(f.path):
                    dirs[:] = sorted(d for d in dirs if not _skipped(d)
                                     and not os.path.islink(os.path.join(top, d)))
                    for name in sorted(files):
                        full = os.path.join(top, name)
                        if not _skipped(name) and os.path.isfile(full) and not os.path.islink(full):
                            yield os.path.relpath(full, root)


def wanted(src_stat, dest_path):
    """True when src should replace or create dest: dest is missing, or src is neither older nor smaller
    than dest and differs from it in size or time."""
    try:
        d = os.stat(dest_path)
    except FileNotFoundError:
        return True
    if src_stat.st_size < d.st_size or src_stat.st_mtime < d.st_mtime:
        return False
    return src_stat.st_size != d.st_size or src_stat.st_mtime != d.st_mtime


def plan(src_root, dest_root, rels):
    """The relative paths among rels that need copying from src_root to dest_root."""
    out = []
    for rel in rels:
        try:
            st = os.stat(os.path.join(src_root, rel), follow_symlinks=False)
        except OSError:
            continue
        if wanted(st, os.path.join(dest_root, rel)):
            out.append(rel)
    return out


# ---------------------------------------------------------------- copying

def rsync_command():
    """The rsync to use, or None for the pure-Python copy. FLEETWATCH_RSYNC names one; set to "" it
    turns rsync off."""
    if "FLEETWATCH_RSYNC" in os.environ:
        cmd = os.environ["FLEETWATCH_RSYNC"].strip()
        return shlex.split(cmd) if cmd else None
    found = shutil.which("rsync")
    return [found] if found else None


def copy_python(src_root, dest_root, rels):
    """Copy each relative path through a temp file and a rename, so the archive never holds half a file.
    The check against the archive copy is repeated after the temp file is complete, so a file that
    shrank or a newer copy written meanwhile never replaces what the archive has. Returns the count."""
    copied = 0
    for rel in rels:
        src, dest = os.path.join(src_root, rel), os.path.join(dest_root, rel)
        folder = os.path.dirname(dest)
        os.makedirs(folder, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".fleetwatch-archive-", dir=folder)
        os.close(fd)
        try:
            try:
                shutil.copy2(src, temp)
            except FileNotFoundError:
                continue
            if wanted(os.stat(temp), dest):
                os.replace(temp, dest)
                copied += 1
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    return copied


def copy_rsync(rsync, src_root, dest_root, rels):
    """rsync the listed files. The flags are the old archiver's: -a and no --delete of any kind. It
    drops --partial, which leaves a cut-off transfer in place of the archive's whole copy. --update
    keeps a newer archive copy; the never-smaller rule is plan()'s, and holds because a transcript only
    grows between plan() and the copy. Returns the count, or raises OSError on a failed rsync."""
    if not rels:
        return 0
    os.makedirs(dest_root, exist_ok=True)
    fd, listing = tempfile.mkstemp(prefix="fleetwatch-archive-", suffix=".list", dir=str(state_dir()))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"\0".join(os.fsencode(r) for r in rels) + b"\0")
        argv = rsync + ["-a", "--update", "--exclude=*.tmp.*", "--from0", "--files-from=" + listing,
                        src_root.rstrip("/") + "/", dest_root.rstrip("/") + "/"]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=RSYNC_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise OSError("rsync took longer than %d s" % RSYNC_TIMEOUT_S) from None
        if proc.returncode not in (0, RSYNC_VANISHED):
            first = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise OSError("rsync exited %d: %s" % (proc.returncode, first[0] if first else "no output"))
    finally:
        os.unlink(listing)
    return len(rels)


# ---------------------------------------------------------------- counting

def main_transcripts(root):
    """{relative path: stat} for each main transcript (<project>/<sessionId>.jsonl) under root."""
    out = {}
    try:
        projects = list(os.scandir(root))
    except OSError:
        return out
    for project in projects:
        if not _folder(project):
            continue
        try:
            for f in os.scandir(project.path):
                if MAIN_TRANSCRIPT.fullmatch(f.name) and _regular(f):
                    out[os.path.join(project.name, f.name)] = f.stat(follow_symlinks=False)
        except OSError:
            continue
    return out


def coverage(paths=None, since=None):
    """How well the archive covers the projects folder. `missing` counts live main transcripts with no
    archive copy; `missing_before` counts those whose last write was before `since` (a run's start),
    which that run should have copied; `behind` counts archive copies smaller than the live file."""
    src, dest, _, _ = roots(paths)
    live, kept = main_transcripts(src), main_transcripts(dest)
    missing = [rel for rel in live if rel not in kept]
    return {"live": len(live), "archived": len(kept), "missing": len(missing),
            "missing_before": None if since is None else sum(1 for rel in missing if live[rel].st_mtime < since),
            "behind": sum(1 for rel, st in live.items() if rel in kept and kept[rel].st_size < st.st_size)}


# ---------------------------------------------------------------- one run

class Busy(Exception):
    """Another archive run holds the lock."""


class _Lock:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self.fh.close()
            if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise Busy() from None
            raise
        return self

    def __exit__(self, *exc):
        self.fh.close()


def _stamp(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t))


def log(line, t=None):
    """Append one line to the archive log; a log that cannot be written never fails a run."""
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(log_path(), "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (_stamp(t), line))
    except OSError:
        pass


def run(paths=None, method=None, out=None):
    """One archive run. method: "rsync", "python" or None (rsync when there is one). Returns the record
    written to last_run.json, or None when another run holds the lock."""
    out = out or sys.stdout
    src, dest, hist_src, hist_dest = roots(paths)
    os.makedirs(state_dir(), exist_ok=True)
    try:
        with _Lock(str(lock_path())):
            return _run_locked(src, dest, hist_src, hist_dest, method, out)
    except Busy:
        log("another run holds %s; exiting" % lock_path())
        print("another archive run is in progress; exiting", file=out)
        return None


def _run_locked(src, dest, hist_src, hist_dest, method, out):
    started = time.time()
    rsync = rsync_command() if method in (None, "rsync") else None
    record = {"started": started, "ended": None, "ok": False, "copied": 0, "transcripts_live": 0,
              "transcripts_archived": 0, "error": None, "method": "rsync" if rsync else "python",
              "dest": dest}
    log("start: %s -> %s (%s)" % (src, dest, record["method"]), started)
    try:
        if method == "rsync" and rsync is None:
            raise OSError("no rsync found (FLEETWATCH_RSYNC or PATH)")
        if not os.path.isdir(src):
            raise OSError("the projects folder %s does not exist" % src)
        os.makedirs(dest, exist_ok=True)
        copy = (lambda s, d, r: copy_rsync(rsync, s, d, r)) if rsync else copy_python
        record["copied"] = copy(src, dest, plan(src, dest, transcript_files(src)))
        hist_dir, hist_name = os.path.split(hist_src)
        if os.path.isfile(hist_src):
            record["copied"] += copy(hist_dir, os.path.dirname(hist_dest),
                                     plan(hist_dir, os.path.dirname(hist_dest), [hist_name]))
        record["ok"] = True
    except OSError as e:
        record["error"] = data.clean(str(e))[:300]
    record["transcripts_live"] = len(main_transcripts(src))
    record["transcripts_archived"] = len(main_transcripts(dest))
    record["ended"] = time.time()
    config.atomic_write(str(state_path()), json.dumps(record, indent=1, sort_keys=True))
    if record["ok"]:
        line = "done: copied %d, %d transcripts live, %d archived" % (
            record["copied"], record["transcripts_live"], record["transcripts_archived"])
    else:
        line = "failed: %s" % record["error"]
    log(line, record["ended"])
    print(line, file=out)
    return record


# ---------------------------------------------------------------- status and the doctor's check

def load_state():
    """The last run's record, or None when there is none or it cannot be read."""
    try:
        with open(state_path(), encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def status_data(paths=None, now=None):
    """The last run's fields (None when it never ran), its age, the coverage and where the archive is."""
    now = time.time() if now is None else now
    state = load_state()
    base = {k: (state or {}).get(k) for k in STATE_KEYS}
    started = data.to_epoch(base["started"]) if state else None
    ended = data.to_epoch(base["ended"]) if state else None
    dest = roots(paths)[1]
    return dict(base, age_s=None if ended is None else max(0.0, now - ended), archive=dest,
                interval_s=JOB.interval_s, coverage=coverage(paths, started))


def check(paths=None, now=None):
    """What `fleetwatch doctor` reports for the archive: {ok, problems, status}. A problem is a last run
    that never happened, failed or is older than STALE_FACTOR intervals, or a live transcript that the
    last run should have copied and the archive lacks."""
    st = status_data(paths, now)
    problems = []
    if st["ended"] is None:
        problems.append("the archive job has never run")
    else:
        if not st["ok"]:
            problems.append("the last archive run failed: %s" % (st["error"] or "unknown error").splitlines()[0])
        if st["age_s"] > STALE_FACTOR * JOB.interval_s:
            problems.append("the last archive run was %s ago (it runs every %s)" % (
                data.human(st["age_s"]), data.human(JOB.interval_s)))
    late = st["coverage"]["missing_before"]
    if late:
        problems.append("%s with no archive copy" % data.plural(late, "live transcript"))
    return {"ok": not problems, "problems": problems, "status": st}


def print_status(paths=None, out=None, now=None):
    out = out or sys.stdout
    now = time.time() if now is None else now
    st = status_data(paths, now)
    print("archive: %s" % st["archive"], file=out)
    if st["ended"] is None:
        print("last run: never", file=out)
    else:
        print("last run: %s ago, %s" % (data.human(st["age_s"]), "ok" if st["ok"] else "FAILED: %s" % st["error"]),
              file=out)
        print("  copied %d; %d transcripts live, %d archived" % (
            st["copied"] or 0, st["transcripts_live"] or 0, st["transcripts_archived"] or 0), file=out)
    cov = st["coverage"]
    print("coverage: %d of %d live transcripts have no archive copy%s" % (
        cov["missing"], cov["live"], "" if not cov["behind"] else "; %d archive copies are behind" % cov["behind"]),
        file=out)
    if sys.platform == "darwin":
        print("job %s: %s" % (JOB.label, "loaded" if schedule.is_loaded(JOB.label) else "not loaded"), file=out)
        if schedule.is_loaded(LEGACY_LABEL):
            print("the legacy archiver %s is still loaded; `fleetwatch archive install` retires it" % LEGACY_LABEL,
                  file=out)
    else:
        schedule.status(out, JOB)
    return 0


# ---------------------------------------------------------------- install and uninstall

def install(out=None):
    """Install the archive job and, on macOS, load it; then retire the legacy archiver. The legacy agent
    is unloaded only once the new job is loaded, so transcripts are never left without an archiver."""
    out = out or sys.stdout
    schedule.install(out, JOB, hint=sys.platform != "darwin")
    if sys.platform != "darwin":
        return 0
    if not schedule.load(out, JOB):
        print("the legacy archiver %s, if any, was left as it was" % LEGACY_LABEL, file=out)
        return 1
    ok, actions = schedule.retire_legacy(LEGACY_LABEL, out)
    if ok and not actions:
        print("no legacy archiver (%s) to retire" % LEGACY_LABEL, file=out)
    return 0 if ok else 1


def uninstall(out=None):
    """Unload and remove the archive job. The archive keeps every copy it has."""
    out = out or sys.stdout
    if sys.platform == "darwin" and schedule.is_loaded(JOB.label):
        proc = schedule.launchctl("bootout", "%s/%s" % (schedule.gui_domain(), JOB.label))
        if proc.returncode != 0 and schedule.is_loaded(JOB.label):
            print("could not unload %s; its plist was left in place" % JOB.label, file=out)
            return 1
        print("unloaded %s" % JOB.label, file=out)
    schedule.remove(out, JOB, hint=sys.platform != "darwin")
    print("transcripts are no longer archived; the archive at %s keeps its copies" % archive_root(), file=out)
    return 0


# ---------------------------------------------------------------- the command line

def build_parser():
    p = argparse.ArgumentParser(
        prog="fleetwatch archive",
        description="Copy Claude Code transcripts into an archive the retention sweep cannot reach. The "
                    "archive never mirrors a deletion and never takes an older or smaller copy.")
    sub = p.add_subparsers(dest="verb", required=True)
    r = sub.add_parser("run", help="copy new and changed transcripts now")
    r.add_argument("--python", action="store_true", help="use the built-in copy even when rsync is present")
    sub.add_parser("install", help="install and load the hourly job, retiring %s" % LEGACY_LABEL)
    sub.add_parser("uninstall", help="unload and remove the hourly job; the archive is kept")
    s = sub.add_parser("status", help="the last run, its age and the archive's coverage")
    s.add_argument("--json", action="store_true")
    for q in (r, s):
        q.add_argument("--projects-dir")
        q.add_argument("--archive-dir", help="the archive's projects folder")
    return p


def main(argv, out=None):
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    paths = data.default_paths()
    for key, val in (("projects", getattr(args, "projects_dir", None)),
                     ("archive", getattr(args, "archive_dir", None))):
        if val:
            paths[key] = os.path.abspath(os.path.expanduser(val))
    if args.verb == "run":
        record = run(paths, "python" if args.python else None, out)
        return 0 if record is None or record["ok"] else 1
    if args.verb == "install":
        return install(out)
    if args.verb == "uninstall":
        return uninstall(out)
    if args.json:
        print(json.dumps(status_data(paths), indent=1, sort_keys=True), file=out)
        return 0
    return print_status(paths, out)
