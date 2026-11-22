"""Antigravity CLI conversations as a transcript source: the same exchange records
session_parse emits, so scrubbing, tagging, rendering and search treat every provider alike.

Antigravity keeps one SQLite database per conversation, <folder>/<uuid>.db. Its `steps` table
holds one row per step (idx, step_type, step_payload, ...), and the payload is Protocol
Buffers with no published schema. The field numbers below were mapped by hand against
MAPPED_VERSION and can move with any agy update, so every database is checked before it is
read (check_shape), and one that fails is skipped, counted and named in the refresh report
rather than indexed as garbage.

Step payload (every step type):
    1   the step type again (always equal to the step_type column; a mismatch means drift)
    5   step metadata: 5.1 the step's time {1: seconds, 2: nanos}; 5.4 the tool call a tool
        step ran {1: call id, 2: tool name, 3: JSON arguments}
Per step type:
    14  PROMPT: the user's prompt, text at 19.2
    15  TURN: one model turn; 20.1 its text answer (20.8 repeats it), 20.3 its reasoning,
        20.7 the tool calls it asked for
    101 SYSTEM: a message the harness delivers, kind at 114.3. "agent_message" is another
        agent's message (a sub-agent's prompt from its parent), text at 114.2.10.1; every
        other kind (background task notices, reminders) is harness output
    132 and the older 2, 5, 8, 9, 21: tool steps, named at 5.4.2. A job run with a result
        schema ends with a `finish` tool whose JSON arguments are the model's final answer
    17 errors, 23 compaction summaries, 90 ephemeral reminders, 98 empty markers: not text
trajectory_metadata_blob.data:
    1.1 (also 7) the workspace folder as a file:// URI; 2 the creation time; for a sub-agent's
    own conversation, 5 the parent conversation id and 8 its spec (8.2 the display name)

One record per EXCHANGE: one prompt plus every model text answer up to the next prompt, with
the names of the tools used in between. The arguments of `finish` are the reply of a job run
with a result schema, so they are kept as reply text, less the harness's own keys; every other
tool's arguments and all tool output, reasoning, harness notices and errors stay out. A sub-agent runs in its own database and is indexed as its own session,
its exchanges sent by the sub-agent kind under its display name. `start_line` and `end_line`
are step numbers (idx).

The databases are in WAL mode, and Antigravity's folder is never written to: a plain
read-only open would create the -wal and -shm files a WAL reader needs, or fail. A database
with no -wal file beside it is fully checkpointed and opened immutable; one with a -wal file
is being written, so it and its WAL are copied to a private temporary folder and the copy is
read. A conversation is read again when the size or modification time of either file
changes, from the step where its last exchange began.
"""
import contextlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import protowire as pw
from .codex_parse import short_home
from .session_parse import DEFAULT_OPTS, DEFAULT_RULES, index_kind

PROVIDER = "antigravity"
# The `agy --version` the field mapping in this module was checked against.
MAPPED_VERSION = "1.5.14"

PROMPT, TURN, SYSTEM = 14, 15, 101
TOOL_STEPS = (2, 5, 8, 9, 21, 132)
QUIET_STEPS = (17, 23, 90, 98)
KNOWN_STEPS = frozenset((PROMPT, TURN, SYSTEM) + TOOL_STEPS + QUIET_STEPS)
REQUIRED = {"steps": {"idx", "step_type", "step_payload"}, "trajectory_metadata_blob": {"data"}}
CONVERSATION_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.db")
MAX_SECONDS = 253402300799          # 9999-12-31: a larger value is not a time
FINISH = "finish"
FINISH_HARNESS_KEYS = ("toolAction", "toolSummary")


class ShapeError(ValueError):
    """The database is not a conversation in the mapped format; `reason` is a fixed code."""

    def __init__(self, reason, detail=""):
        super().__init__("%s%s" % (reason, ": " + detail if detail else ""))
        self.reason = reason


def file_sig(path):
    """What decides a reread: size and mtime of the database and of its -wal file, if any."""
    st = path.stat()
    sig = [st.st_size, st.st_mtime_ns]
    try:
        w = wal_path(path).stat()
        sig += [w.st_size, w.st_mtime_ns]
    except FileNotFoundError:
        pass
    return sig


def wal_path(path):
    return path.with_name(path.name + "-wal")


@contextlib.contextmanager
def open_conversation(path):
    """A read-only connection to one conversation that writes nothing beside it."""
    if wal_path(path).exists():
        with tempfile.TemporaryDirectory(prefix="fleetwatch-agy-") as tmp:
            copy = Path(tmp) / "conversation.db"
            # The database before its WAL: a checkpoint between the two copies leaves frames
            # in the WAL that replay onto the older copy, never older frames over newer pages.
            shutil.copyfile(path, copy)
            try:
                shutil.copyfile(wal_path(path), wal_path(copy))
            except FileNotFoundError:
                pass                # checkpointed and closed between the check and the copy
            conn = sqlite3.connect(str(copy))
            try:
                yield conn
            finally:
                conn.close()
    else:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            yield conn
        finally:
            conn.close()


def iso(sec, nanos=0):
    return "%s.%03dZ" % (datetime.fromtimestamp(sec, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                         nanos // 10 ** 6)


def stamp(t):
    """The ISO form of a time message {1: seconds, 2: nanos}, or None when `t` is not one."""
    if not isinstance(t, memoryview):
        return None
    sec, nanos = pw.first(t, 1), pw.first(t, 2) or 0
    if not isinstance(sec, int) or not isinstance(nanos, int) or not 0 < sec <= MAX_SECONDS \
            or nanos >= 10 ** 9:
        return None
    return iso(sec, nanos)


def folder(blob):
    """The workspace folder named in the metadata blob, home written as ~; "" when none."""
    uri = pw.text(blob, 1, 1) or pw.text(blob, 7) or ""
    if uri.startswith("file://"):
        uri = unquote(urlparse(uri).path)
    return short_home(uri.rstrip("/") or uri) if uri else ""


def finish_text(args):
    """The reply a `finish` call carries: its JSON arguments without the harness's keys, as
    indented JSON; "" when they are not a JSON object."""
    try:
        obj = json.loads(args or "")
    except ValueError:
        return ""
    if not isinstance(obj, dict):
        return ""
    obj = {k: v for k, v in obj.items() if k not in FINISH_HARNESS_KEYS}
    return json.dumps(obj, indent=1, ensure_ascii=False) if obj else ""


def check_shape(conn):
    """The metadata blob of a conversation database, after checking its tables; raises
    ShapeError otherwise. The steps themselves are checked as they are read."""
    for table, cols in REQUIRED.items():
        have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        if not cols <= have:
            raise ShapeError("tables", "%s lacks %s" % (table, ", ".join(sorted(cols - have))))
    row = conn.execute("SELECT data FROM trajectory_metadata_blob").fetchone()
    if row is None or row[0] is None:
        raise ShapeError("metadata", "no metadata blob")
    try:
        pw.fields(row[0])
    except pw.DecodeError as e:
        raise ShapeError("metadata", str(e))
    return bytes(row[0])


def created(path):
    """The conversation's creation time (blob field 2) for ordering, falling back to the
    file's modification time when the blob cannot be read yet."""
    try:
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1",
                                                uri=True)) as conn:
            row = conn.execute("SELECT data FROM trajectory_metadata_blob").fetchone()
        ts = stamp(pw.first(row[0], 2))
        if ts:
            return ts
    except Exception:       # any failure only costs the ordering; parse() reports the file
        pass
    return iso(int(path.stat().st_mtime))


def parse(conn, start, next_n, opts=None, rules=DEFAULT_RULES, counts=None):
    """Read steps from idx `start` on. Returns (records, next_n); the last record is still
    open. A step whose payload does not decode, does not restate its type, or lacks the text
    or time its type carries is skipped and added to counts["steps_skipped"]; a step type not
    in the mapping is added to counts["unknown_steps"]."""
    opts = opts or DEFAULT_OPTS
    counts = counts if counts is not None else {}
    blob = check_shape(conn)
    cwd = folder(blob)
    sub_name = pw.text(blob, 8, 2) if pw.first(blob, 8) is not None else None
    owner = index_kind(opts, rules.owner_kind, rules)
    subagent = index_kind(opts, "subagent", rules)
    records, cur, valid, known, rows = [], None, 0, 0, 0

    def skip():
        counts["steps_skipped"] = counts.get("steps_skipped", 0) + 1

    def begin(kind, canon, name, body, ts, idx):
        nonlocal next_n
        rec = dict(n=next_n, ex="EX", start_offset=idx, start_line=idx, end_line=idx,
                   sender_kind=kind, sender_canon=canon, sender_name=name, ts=ts, user_text=body,
                   assistant=[], tools={}, interrupted=False, seen=[], provider=PROVIDER)
        if cwd:
            rec["cwd"] = cwd
        next_n += 1
        return rec

    for idx, step_type, payload in conn.execute(
            "SELECT idx, step_type, step_payload FROM steps WHERE idx >= ? ORDER BY idx", (start,)):
        rows += 1
        try:
            top = pw.fields(payload or b"")
        except (pw.DecodeError, TypeError):
            skip()
            continue
        if [v for f, wt, v in top if f == 1 and wt == pw.VARINT] != [step_type]:
            skip()
            continue
        valid += 1
        if step_type not in KNOWN_STEPS:
            unknown = counts.setdefault("unknown_steps", {})
            unknown[str(step_type)] = unknown.get(str(step_type), 0) + 1
            continue
        known += 1
        part = {f: v for f, wt, v in reversed(top) if wt == pw.LENGTH}
        meta = part.get(5)
        body, kind = None, None
        if step_type == PROMPT:
            body, kind = (pw.text(part.get(19, b""), 2) or "").strip(), "owner"
            if not body:
                skip()
                continue
        elif step_type == SYSTEM and pw.text(part.get(114, b""), 3) == "agent_message":
            body, kind = (pw.text(part.get(114, b""), 2, 10, 1) or "").strip(), "subagent"
            if not body:
                skip()
                continue
        if body:
            ts = stamp(pw.first(meta, 1)) if meta is not None else None
            if ts is None:
                skip()
                continue
            if cur is not None:
                records.append(cur)
            cur = (begin(owner, rules.owner_kind, rules.owner_name, body, ts, idx)
                   if kind == "owner" else
                   begin(subagent, "subagent", (sub_name or "agent")[:80], body, ts, idx))
            continue
        if cur is None:
            continue
        cur["end_line"] = idx
        if step_type == TURN:
            answer = (pw.text(part.get(20, b""), 1) or "").strip()
            if answer:
                cur["assistant"].append(answer)
        tool = pw.text(meta, 4, 2) if meta is not None else None
        if tool:
            cur["tools"][tool] = cur["tools"].get(tool, 0) + 1
        if tool == FINISH:
            answer = finish_text(pw.text(meta, 4, 3))
            if answer:
                cur["assistant"].append(answer)
    if rows and not valid:
        raise ShapeError("payload", "no step payload decodes")
    if valid and not known:
        raise ShapeError("step_types", "no step type is in the mapping for agy %s" % MAPPED_VERSION)
    if cur is not None:
        records.append(cur)
    return records, next_n


class AntigravitySource:
    """The transcript source for `transcripts: {source: antigravity, path: <folder>}`.
    One instance serves one refresh; its counters report what the refresh read."""

    keep_empty = True       # a conversation with no exchange still gets a cursor

    def __init__(self):
        self.read = 0
        self.skipped = []       # (file name, reason code)
        self.vanished = []
        self.counts = {}

    def id_stem(self, sid):
        """Conversation ids are random UUIDs: ten hex characters (40 bits) keep exchange ids
        apart across the thousands of conversations one index can hold, where six would
        clash; the first eight are the file name's own prefix."""
        return sid.replace("-", "")[:10]

    def stats(self):
        return dict(conversations_read=self.read,
                    conversations_skipped=[list(x) for x in self.skipped],
                    conversations_vanished=list(self.vanished),
                    steps_skipped=self.counts.get("steps_skipped", 0),
                    unknown_steps=dict(self.counts.get("unknown_steps", {})))

    def list(self, project_dir, state_dir, automated_prefixes, sessions, exclude_automated):
        """(keep, skipped): every conversation in the folders as (creation time, id, path),
        oldest first. Antigravity runs no automated one-shot sessions to skip."""
        keep = []
        for d in (project_dir if isinstance(project_dir, (list, tuple)) else [project_dir]):
            for p in sorted(Path(d).glob("*.db")):
                if not CONVERSATION_RE.fullmatch(p.name):
                    continue
                sid = p.name[:-3]
                if sessions != "all" and sid not in sessions:
                    continue
                try:
                    keep.append((created(p), sid, p))
                except FileNotFoundError:
                    continue        # deleted while listing
        return sorted(keep), {}

    def present(self, dirs, sid):
        return [str(d) for d in dirs if (d / (sid + ".db")).exists()]

    def newest_mtime(self, dirs):
        newest = 0
        for d in dirs:
            for f in d.glob("*.db*"):
                try:
                    newest = max(newest, f.stat().st_mtime)
                except OSError:
                    pass
        return newest

    def parse(self, path, cursor, old, opts, rules):
        """(new records, cursor, old records kept). Nothing is read when neither file changed.
        A database that fails the shape check or cannot be read is reported and keeps its
        stored cursor and records, so it is tried again on the next refresh."""
        stored = (cursor, old)
        try:
            sig = file_sig(path)
        except FileNotFoundError:
            self.vanished.append(path.name)
            return [], cursor, old
        if cursor and cursor.get("sig") == sig:
            return [], cursor, old
        old = list(old)
        if cursor and old:
            tail = old.pop()
            start, nx = tail["start_offset"], 1 + max([r["n"] for r in old] or [0])
        else:
            old, start, nx = [], 0, 1
        try:
            with open_conversation(path) as conn:
                recs, next_n = parse(conn, start, nx, opts, rules, self.counts)
        except FileNotFoundError:
            self.vanished.append(path.name)
            return [], stored[0], stored[1]
        except ShapeError as e:
            return self._skip(path, e.reason, e, stored)
        except (sqlite3.Error, OSError) as e:
            return self._skip(path, "unreadable", "%s: %s" % (type(e).__name__, e), stored)
        self.read += 1
        return recs, dict(next_n=next_n, next_aq=1, cron_seen={}, sig=sig), old

    def _skip(self, path, reason, detail, stored):
        self.skipped.append((path.name, reason))
        print("antigravity: skipped %s (%s): %s" % (path.name, reason, detail), file=sys.stderr)
        return [], stored[0], stored[1]

