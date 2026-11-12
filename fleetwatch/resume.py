"""`fleetwatch resume`: find a custom-named main session by name and bring it back in its own folder.

Transcripts are only read here. The one write under the projects folder is copying back a transcript
that survives only in the archive. Everything in this module runs off the UI thread, because a first
scan reads whole files.
"""
import argparse
import json
import math
import os
import re
import shlex
import shutil
import signal
import sys
import time

from . import config, data, find, iterm, park, prune

SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SESSION_FILE = re.compile(SESSION_ID.pattern + r"\.jsonl")
WORKTREE = re.compile(r"/(\.claude/)?worktrees?/|-worktrees?(/|$)|/wt-[^/]*(/|$)")
SID_QUERY = re.compile(r"^[0-9a-f-]{8,36}$")
TITLE_MARK = b'"type":"custom-title"'
CHUNK = 1 << 20
HEAD_LINES = 2000
CACHE_VERSION = 1
# A transcript written this recently may belong to a process fleetwatch cannot see yet.
IN_USE_S = 60
MAX_EPOCH = 4e10          # year 3000; parked.json is hand-editable and date_label must not overflow
LOOP_NOTE = "%s ran on a scheduled loop. A cron does not survive a session ending, so start its loop " \
            "again once it is back."
# Claude Code's -p and SDK runs record this entrypoint. Nearly every transcript is one, and none is a
# session someone would want back by name, so these are never read past their first records.
HELPER_ENTRYPOINT = "sdk-"


# ---------------------------------------------------------------- reading one transcript

def read_head(path):
    """(first cwd, entrypoint, sidechain). The first cwd is the launch folder: later records follow
    the session when its working folder moves or when it is resumed somewhere else."""
    with open(path, "rb") as fh:
        for i, line in enumerate(fh):
            if i >= HEAD_LINES:
                break
            if b'"cwd"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and isinstance(d.get("cwd"), str):
                return d["cwd"], d.get("entrypoint"), bool(d.get("isSidechain"))
    return None, None, False


def _title_in(line):
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if isinstance(d, dict) and d.get("type") == "custom-title" and isinstance(d.get("customTitle"), str):
        return data.clean(d["customTitle"]).strip() or None
    return None


def last_title(path, start, end):
    """The newest custom title in bytes [start, end), searching backward from the end.

    Claude Code repeats the title through a named transcript, so a named file stops within a chunk or
    two; only a file with no title is read in full. The mark can sit inside a nested object, so each
    hit is parsed and must be a whole top-level record.
    """
    with open(path, "rb") as fh:
        pos, carry = end, b""
        while pos > start:
            at = max(start, pos - CHUNK)
            fh.seek(at)
            buf = fh.read(pos - at) + carry
            if at > start:
                cut = buf.find(b"\n")
                carry, buf = (buf, b"") if cut < 0 else (buf[:cut], buf[cut + 1:])
            hit = buf.rfind(TITLE_MARK)
            while hit >= 0:
                head = buf.rfind(b"\n", 0, hit) + 1
                tail = buf.find(b"\n", hit)
                title = _title_in(buf[head:tail if tail >= 0 else len(buf)])
                if title:
                    return title
                hit = buf.rfind(TITLE_MARK, 0, head)
            pos = at
    return None


def read_transcript(path, size, mtime, prev):
    """What the index keeps for one transcript, reading as little as the cache allows."""
    if prev and prev.get("size") == size and prev.get("mtime") == mtime:
        return prev
    if prev and prev.get("helper"):
        return dict(prev, size=size, mtime=mtime)
    if prev and prev.get("cwd") and prev.get("size", size + 1) <= size:
        # Transcripts only grow, so only what was added can hold a newer title.
        return dict(prev, size=size, mtime=mtime, title=last_title(path, prev["size"], size) or prev.get("title"))
    cwd, entry, side = read_head(path)
    if side or (isinstance(entry, str) and entry.startswith(HELPER_ENTRYPOINT)):
        return {"size": size, "mtime": mtime, "helper": True}
    return {"size": size, "mtime": mtime, "cwd": cwd, "title": last_title(path, 0, size)}


# ---------------------------------------------------------------- the index

def _transcripts(root):
    """(path, sessionId, size, mtime) for each session file one level under root."""
    try:
        folders = list(os.scandir(root))
    except OSError:
        return
    for folder in folders:
        try:
            if not folder.is_dir():
                continue
            with os.scandir(folder.path) as it:
                for f in it:
                    if SESSION_FILE.fullmatch(f.name):
                        st = f.stat()
                        yield f.path, f.name[:-6], st.st_size, st.st_mtime
        except OSError:
            continue


def _load_cache(path):
    try:
        doc = data.load_json(path)
    except (OSError, ValueError):
        return {}
    if isinstance(doc, dict) and doc.get("version") == CACHE_VERSION and isinstance(doc.get("files"), dict):
        return {k: v for k, v in doc["files"].items() if _cache_entry_ok(v)}
    return {}


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _cache_entry_ok(v):
    """A hand-edited entry of the wrong shape is a miss: the file is read again."""
    return (isinstance(v, dict) and _number(v.get("size")) and _number(v.get("mtime"))
            and all(isinstance(v.get(k), (str, type(None))) for k in ("cwd", "title")))


def scan(paths, cache_path=None):
    """{sessionId: {path, archived, mtime, cwd, title}} for every main session, helpers left out.

    The archive mirrors the projects folder, so a transcript counts from it only when the projects
    folder no longer has that session. The cache is keyed by file; an unchanged file costs one stat.
    """
    cache_path = cache_path or config.resume_cache_path()
    old = _load_cache(cache_path)
    new, sessions = {}, {}
    for root, archived in ((paths["projects"], False), (paths.get("archive"), True)):
        if not root:
            continue
        for path, sid, size, mtime in _transcripts(root):
            if archived and sid in sessions:
                continue
            try:
                info = read_transcript(path, size, mtime, old.get(path))
            except OSError:
                continue
            new[path] = info
            if info.get("helper") or (sid in sessions and sessions[sid]["mtime"] >= mtime):
                continue
            sessions[sid] = dict(info, path=path, archived=archived)
    for s in sessions.values():
        s["folder_ok"] = bool(s.get("cwd")) and os.path.isdir(s["cwd"])
    if new != old:
        try:
            config.atomic_write(cache_path, json.dumps({"version": CACHE_VERSION, "files": new}))
        except OSError:
            pass
    return sessions


def _newer(table, key, d):
    cur = table.get(key)
    if cur is None or data.to_epoch(d.get("updated")) >= data.to_epoch(cur.get("updated")):
        table[key] = d


def read_personas(paths):
    """(persona by sessionId, persona by name, status by sessionId, status by name) from status files,
    parked ones included, since a finished session's file has usually been pruned. A name counts only
    for a file with no sessionId. Of several files for one key the newest `updated` wins."""
    by_id, by_name, file_id, file_name = {}, {}, {}, {}
    status = paths.get("status")
    if status:
        for _path, d in prune.read_folder(status) + prune.read_pruned(status):
            sid = prune.sid_of(d)
            _newer(file_id, sid, d) if sid else _newer(file_name, d["session"], d)
            if d.get("persona"):
                if sid:
                    by_id[sid] = data.clean(d["persona"])
                else:
                    by_name[d["session"]] = data.clean(d["persona"])
    return by_id, by_name, file_id, file_name


def left_of(d):
    """What a status file says about where its session left off: working_on, the first of your
    upcoming items (earliest dated, else an undated one), the question count and when it was written."""
    if not d:
        return None
    mine = [i for i in data.session_upcoming(d, "", []) if i["yours"]]
    dated = sorted((i for i in mine if i["due"]), key=lambda i: i["due"])
    nxt = (dated or [i for i in mine if not i["due"]] or [None])[0]
    return {"was": data.clean(d.get("working_on") or "").strip(), "updated": data.to_epoch(d.get("updated")),
            "questions": len(data.items_of(d.get("questions"))),
            "next": None if nxt is None else (nxt["due"].strftime("%b %-d") if nxt["due"] else "(no date)",
                                              nxt["what"])}


def left_off(e, now):
    """The one line a stopped session's row shows, or None without a status file."""
    left = e.get("left")
    if not left:
        return None
    parts = []
    if left["was"]:
        parts.append("was: " + data.trunc(left["was"], 90, True))
    if left["next"]:
        parts.append("next: %s %s" % (left["next"][0], data.trunc(left["next"][1], 50, True)))
    if left["questions"]:
        parts.append(data.plural(left["questions"], "question"))
    if left["updated"]:
        parts.append("status %s ago" % data.human(now - left["updated"]))
    return " · ".join(parts) or None


class Loaded:
    """One scan: main sessions and personas, plus the parked table, which changes on its own."""

    def __init__(self, sessions, personas, parked):
        self.sessions, self.personas, self.parked = sessions, personas, parked

    def entries(self):
        return build_entries(self.sessions, self.parked, self.personas)


def load(paths, cache_path=None, parked_path=None):
    """A fresh Loaded. A corrupt parked file raises park.ParkedError."""
    return Loaded(scan(paths, cache_path), read_personas(paths), park.load(parked_path))


def build_entries(sessions, parked, personas):
    """Rows to offer: custom-named main sessions, plus anything parked by hand. Parked first, then
    newest first. Worktree lanes and helper-style names are left out even when they carry a title."""
    by_id, by_name, file_id, file_name = tuple(personas) + ({}, {})[len(personas) - 2:]
    # parked.json can be edited by hand, so nothing from it reaches a terminal or claude unchecked.
    parked = {sid: p for sid, p in parked.items() if SESSION_ID.fullmatch(sid)}
    out = []

    def entry(sid, name, s):
        p = parked.get(sid) or {}
        when = p.get("parked_at")
        return {"sid": sid, "name": name, "cwd": s.get("cwd"), "last": s.get("mtime"),
                "path": s.get("path"), "archived": bool(s.get("archived")),
                "folder_ok": bool(s.get("folder_ok")),
                "persona": by_id.get(sid) or by_name.get(name),
                "left": left_of(file_id.get(sid) or file_name.get(name)),
                "parked": sid in parked, "note": data.clean(p.get("note") or ""),
                "parked_at": when if _number(when) and 0 < when < MAX_EPOCH else None,
                "held": None, "job": None}

    for sid, s in sessions.items():
        title, cwd, p = s.get("title"), s.get("cwd") or "", parked.get(sid)
        named = title and cwd and not data.HELPER_NAME.search(title) and not WORKTREE.search(cwd)
        if named or p:
            out.append(entry(sid, title or data.clean(p.get("name") or sid), s))
    for sid, p in parked.items():
        if sid not in sessions:
            out.append(entry(sid, data.clean(p.get("name") or sid), {}))
    out.sort(key=lambda e: (not e["parked"], -(e["last"] or 0), e["name"].casefold()))
    return out


def held_map(records):
    """{sessionId: record} for sessions a process or the background daemon still holds.

    Live is what the rest of fleetwatch counts as live. A background session that `claude agents` lists is
    held even with no pid.
    """
    return {r["sessionId"]: r for r in records if r["sessionId"]
            and (r["live"] or (r["agent_kind"] == "background" and r.get("job_id")))}


def mark_held(entries, held):
    """Copies of the entries with who holds each one, from held_map."""
    out = []
    for e in entries:
        r = held.get(e["sid"])
        how = None if r is None else "live" if r["live"] else "background"
        out.append(dict(e, held=how, job=(r or {}).get("job_id")))
    return out


def offered(entries):
    """What a list of things to resume shows: running sessions only when parked."""
    return [e for e in entries if e["parked"] or not e["held"]]


def match(query, entries):
    """Loose name match, best band only: an exact name, a session ID prefix, then word-start matches
    (prefix and initials count the same, so `grant` finds every Grant session), then substrings,
    then subsequences. A subsequence must not widen a list that has a real match in it."""
    q = query.strip()
    exact = [e for e in entries if e["name"].casefold() == q.casefold()]
    if exact:
        return exact
    if SID_QUERY.match(q):
        ids = [e for e in entries if e["sid"].startswith(q)]
        if ids:
            return ids
    scored = []
    for e in entries:
        s = find.score(q, [e["name"], e["persona"] or ""])
        if s is not None:
            scored.append((max(s[0], 1), e))
    if not scored:
        return []
    band = min(s for s, _ in scored)
    return [e for s, e in scored if s == band]


# ---------------------------------------------------------------- showing them

_CLOUD = re.compile(r"^~/Library/CloudStorage/(?:OneDrive-[^/]*|GoogleDrive-[^/]*/My Drive|(Dropbox))(?=/|$)")


def home_folder(cwd):
    """cwd with the home folder written as ~. FLEETWATCH_DISPLAY_HOME names the folder shown as ~
    instead: the demo sets it so its temporary folder reads as a home folder on screen."""
    home = os.environ.get("FLEETWATCH_DISPLAY_HOME") or data.HOME
    if cwd == home or cwd.startswith(home + "/"):
        return "~" + cwd[len(home):]
    return cwd


def short_folder(cwd, width=64):
    """A folder for a row: cloud-storage prefixes folded and the tail kept, since the last folders
    are what tell two sessions of one name apart."""
    if not cwd:
        return "folder unknown"
    p = home_folder(cwd)
    m = _CLOUD.match(p)
    if m:
        label = "Dropbox" if m.group(1) else "OneDrive" if "OneDrive-" in m.group(0) else "Drive"
        p = label + p[m.end():]
    p = data.clean(p)
    return p if len(p) <= width else "…" + p[-(width - 1):]


def usable_folder(cwd):
    """True for an absolute path with no control characters. Anything else is refused rather than
    escaped: a folder name comes from a transcript, and it reaches terminals and shell lines. A clean
    that changes the path found a control character, newlines and tabs included."""
    return isinstance(cwd, str) and os.path.isabs(cwd) and cwd == data.clean(cwd)


def tags(e):
    out = []
    if e["held"] == "live":
        out.append("running")
    elif e["held"] == "background":
        out.append("background session")
    if not e["path"]:
        out.append("no transcript")
    elif e["cwd"] and not usable_folder(e["cwd"]):
        out.append("folder unusable")
    elif not e["folder_ok"]:
        out.append("folder gone")
    if e["archived"]:
        out.append("archived")
    return out


def listing(entries, now):
    """Numbered rows for a terminal: name, last-active date, folder, any parked note, then where it left off."""
    lines = []
    width = len(str(len(entries)))
    for i, e in enumerate(entries, 1):
        name = e["name"] + (" (%s)" % e["persona"] if e["persona"] else "")
        lines.append("%*d. %s  %s  %s%s" % (width, i, name, data.date_label(e["last"], now),
                                            short_folder(e["cwd"]),
                                            "".join("  [%s]" % t for t in tags(e))))
        if e["parked"]:
            lines.append("%*s  parked %s%s" % (width, "", data.date_label(e["parked_at"], now),
                                               ": " + e["note"] if e["note"] else ""))
        line = left_off(e, now)
        if line:
            lines.append("%*s  %s" % (width, "", line))
    return "\n".join(lines)


def choose(entries, out, now=None, ask=input):
    """Show numbered rows and ask for one. Without a terminal, list them and return None."""
    print(listing(entries, time.time() if now is None else now), file=out)
    if not (sys.stdin.isatty() and sys.stdout.isatty()) and ask is input:
        return None
    try:
        answer = ask("Which one? 1-%d, Enter cancels: " % len(entries)).strip()
    except (EOFError, KeyboardInterrupt):
        print(file=out)
        return None
    if answer.isascii() and answer.isdigit() and 1 <= int(answer) <= len(entries):
        return entries[int(answer) - 1]
    if answer:
        print("no such row", file=out)
    return None


# ---------------------------------------------------------------- bringing one back

class Blocked(Exception):
    """Why a session cannot be resumed now. kind: live, background, unknown, recent, unsafe, gone or missing."""

    def __init__(self, kind, message, record=None):
        super().__init__(message)
        self.kind, self.record = kind, record


def claude_argv(sid):
    return shlex.split(os.environ.get("FLEETWATCH_CLAUDE_CMD", "claude")) + ["--resume", sid]


def shell_line(entry):
    """A line to paste by hand when fleetwatch cannot start claude itself. shlex quotes the folder, which
    comes from data; the session name never enters it."""
    return "cd %s && %s" % (shlex.quote(entry["cwd"]), shlex.join(claude_argv(entry["sid"])))


def window_command(entry):
    """What a new window runs: fleetwatch again, by session ID, through an interactive login shell for PATH
    as the hotkey profile does. The ID is the only data in the string, and the second fleetwatch repeats
    the gate and does the chdir and exec. A refusal waits for a key, or the window would vanish with
    the reason in it."""
    inner = "%s resume %s --here || { echo Press Enter to close; read; }" % (
        os.environ.get("FLEETWATCH_SELF_CMD", "fleetwatch"), shlex.quote(entry["sid"]))
    return shlex.join([os.environ.get("SHELL") or "/bin/bash", "-ilc", inner])


def restore_from_archive(entry, paths):
    """Copy an archive-only transcript back so Claude Code can find it, with the folder of subagents
    and tool results beside it. The archive keeps its copy and nothing is ever overwritten."""
    src = entry["path"]
    slug = os.path.basename(os.path.dirname(src))
    folder = os.path.join(paths["projects"], slug)
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, entry["sid"] + ".jsonl")
    temp = "%s.fleetwatch-restore-%d" % (dest, os.getpid())
    shutil.copy2(src, temp)
    try:
        os.link(temp, dest)
    except FileExistsError:
        pass
    finally:
        os.unlink(temp)
    side_src, side_dest = src[:-len(".jsonl")], dest[:-len(".jsonl")]
    if os.path.isdir(side_src) and not os.path.exists(side_dest):
        shutil.copytree(side_src, side_dest)
    return dict(entry, path=dest, archived=False)


def prepare(entry, paths):
    """The gate every resume passes, right before starting claude. Returns the entry to start.

    Liveness is read fresh here, not from the list on screen: two processes on one transcript corrupt
    it, and a session can have started since the list was drawn. exec_here, open_new_window and the
    shell_line fallbacks must only ever see an entry this returned, because this is what keeps a
    folder's control characters away from a terminal.
    """
    name = entry["name"]
    failed = []
    records = data.build_records(paths, time.time(), [], failed=failed)
    # Without both sources a running session looks stopped, so nothing is started.
    if failed or not os.path.isdir(paths["sessions"]):
        raise Blocked("unknown", "fleetwatch can't tell whether %s is running (%s could not be read), so "
                                 "claude was not started." % (
                                     name, "`claude agents`" if failed else "the sessions folder"))
    record = held_map(records).get(entry["sid"])
    if record is not None:
        if record["live"]:
            raise Blocked("live", "%s is still running. A second process on one transcript would "
                                  "corrupt it." % name, record)
        short = record.get("job_id") or entry["sid"]
        raise Blocked("background", "%s is a background session. Use `claude attach %s`. If it is a "
                                    "leftover record with nothing running, moving ~/.claude/jobs/%s "
                                    "aside clears it." % (name, short, short), record)
    if not entry["path"] or not SESSION_ID.fullmatch(entry["sid"]):
        raise Blocked("missing", "%s has no transcript in the projects folder or the archive." % name)
    try:
        age = time.time() - os.stat(entry["path"]).st_mtime
    except OSError:
        age = None
    if age is not None and age < IN_USE_S:
        raise Blocked("recent", "%s looks in use: its transcript was written in the last minute. Try again "
                                "in a minute." % name)
    # A control byte would be acted on by a terminal when the fallback prints the line to paste, and a
    # relative path would land wherever fleetwatch happened to run, so neither is ever started.
    cwd = entry["cwd"]
    if cwd and not usable_folder(cwd):
        raise Blocked("unsafe", "%s: its recorded folder can't be used, so claude was not started. It was %s" % (
            name, data.clean(cwd)))
    if not entry["cwd"] or not os.path.isdir(entry["cwd"]):
        raise Blocked("gone", "%s: its folder no longer exists, so claude was not started. It was %s" % (
            name, data.clean(entry["cwd"] or "unknown")))
    if entry["archived"]:
        try:
            return restore_from_archive(entry, paths)
        except OSError as exc:
            raise Blocked("missing", "could not copy %s back from the archive: %s" % (
                name, data.clean(exc)[:100])) from None
    return entry


def notes_for(entry, now=None):
    """Lines worth saying as a session comes back."""
    out = []
    if entry["parked"]:
        out.append("It was parked%s." % (": " + entry["note"] if entry["note"] else ""))
    line = left_off(entry, time.time() if now is None else now)
    if line:
        out.append("Where it left off: " + line)
    if entry["persona"]:
        out.append(LOOP_NOTE % entry["persona"])
    return out


def _unpark_quietly(entry):
    # The session is back, so "finished for now" no longer holds. Its note has already been shown.
    if entry["parked"]:
        try:
            park.unpark(entry["sid"])
        except (park.ParkedError, OSError):
            pass


def open_new_window(entry):
    """(ok, window id or reason). The fleetwatch in the new window unparks at its exec: this one must not,
    or that one could no longer find a session that is listed only because it is parked."""
    return iterm.open_window(window_command(entry))


def in_hotkey_window():
    """True inside fleetwatch's own iTerm2 hotkey profile, which must stay fleetwatch: a resume there goes to a
    new window instead of turning the floating window into a session."""
    return os.environ.get("ITERM_PROFILE") == "fleetwatch"


def exec_here(entry, out=None):
    """Become the session: chdir, then exec, so nothing is left wrapping it. Returns only on failure."""
    out = out or sys.stdout
    argv = claude_argv(entry["sid"])
    if shutil.which(argv[0]) is None:
        print("%s is not on PATH; run this yourself:\n  %s" % (argv[0], shell_line(entry)), file=out)
        return 1
    print("Resuming %s in %s" % (data.clean(entry["name"]), data.clean(entry["cwd"])), file=out)
    for line in notes_for(entry):
        print(line, file=out)
    out.flush()
    try:
        os.chdir(entry["cwd"])
    except OSError as exc:
        print("could not enter the folder: %s" % data.clean(exc)[:100], file=out)
        return 1
    _unpark_quietly(entry)
    # Python starts with these ignored, and an ignored signal stays ignored across exec.
    for name in ("SIGPIPE", "SIGXFSZ"):
        signal.signal(getattr(signal, name), signal.SIG_DFL)
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        print("could not start %s: %s" % (argv[0], data.clean(exc)[:100]), file=out)
        return 1


def _offer_jump(blocked, out, ask=input):
    """Offer the iTerm jump for a running session. The exit code says it was not resumed."""
    ctx = iterm.jump_context()
    tty = ctx["ttys"].get((blocked.record or {}).get("pid"))
    if ctx["iterm"] is None or tty not in ctx["iterm"]:
        print("It is not in an iTerm2 tab fleetwatch can jump to.", file=out)
        return 3
    if not (sys.stdin.isatty() and sys.stdout.isatty()) and ask is input:
        return 3
    try:
        answer = ask("Jump to its iTerm tab? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(file=out)
        return 3
    if answer in ("", "y", "yes"):
        ok, reason = iterm.jump_to_tty(tty)
        print("jumped" if ok else "could not reach iTerm2: %s" % reason, file=out)
    return 3


def go(entry, paths, here, out=None):
    """Run one resume from the command line. Returns the exit code."""
    out = out or sys.stdout
    try:
        entry = prepare(entry, paths)
    except Blocked as b:
        print(b, file=out)
        if b.kind == "live":
            return _offer_jump(b, out)
        return 3 if b.kind == "background" else 1
    if here:
        return exec_here(entry, out)
    ok, info = open_new_window(entry)
    if not ok:
        print("could not reach iTerm2: %s\nRun this yourself:\n  %s" % (info, shell_line(entry)), file=out)
        return 1
    print("Opened %s in a new iTerm window (window %s), in %s" % (
        data.clean(entry["name"]), data.clean(info), data.clean(entry["cwd"])), file=out)
    for line in notes_for(entry):
        print(line, file=out)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="fleetwatch resume",
        description="Find a custom-named session by name and resume it in its original folder, in a new "
                    "iTerm window. No name lists what can be resumed, newest first.")
    p.add_argument("name", nargs="*", help="part of a session name, or a session ID prefix")
    p.add_argument("--here", action="store_true", help="resume in this terminal instead of a new window")
    p.add_argument("--command", help="override the `claude agents --json` command")
    p.add_argument("--sessions-dir")
    p.add_argument("--jobs-dir")
    p.add_argument("--status-dir")
    p.add_argument("--projects-dir")
    p.add_argument("--archive-dir")
    return p


def main(argv):
    args = build_parser().parse_args(argv)
    paths = data.default_paths()
    for key, val in (("command", args.command), ("sessions", args.sessions_dir), ("jobs", args.jobs_dir),
                     ("status", args.status_dir), ("projects", args.projects_dir),
                     ("archive", args.archive_dir)):
        if val:
            paths[key] = val
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # pragma: no cover
        pass
    try:
        loaded = load(paths)
    except park.ParkedError as exc:
        print("warning: %s" % exc, file=sys.stderr)
        loaded = Loaded(scan(paths), read_personas(paths), {})
    warnings = []
    now = time.time()
    entries = mark_held(loaded.entries(), held_map(data.build_records(paths, now, warnings)))
    for w in warnings:
        print(w, file=sys.stderr)
    query = " ".join(args.name).strip()
    if not query:
        pool = offered(entries)
        if not pool:
            print("nothing to resume: no custom-named session is parked or stopped")
            return 0
        chosen = choose(pool, sys.stdout, now)
        return go(chosen, paths, args.here) if chosen else 0
    matches = match(query, entries)
    if not matches:
        print("no custom-named session matches %r. `fleetwatch resume` lists them; sessions you never named "
              "are not listed." % query, file=sys.stderr)
        return 1
    if len(matches) > 1:
        chosen = choose(matches, sys.stdout, now)
        if chosen is None:
            return 2 if not sys.stdin.isatty() else 0
        return go(chosen, paths, args.here)
    return go(matches[0], paths, args.here)
