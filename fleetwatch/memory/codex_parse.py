"""Codex CLI session logs as a transcript source: the same exchange records session_parse
emits, so scrubbing, tagging, rendering and search treat both providers alike.

Codex writes one rollout per session under <root>/YYYY/MM/DD/: rollout-<local time>-<id>.jsonl
while the session runs, and rollout-<local time>-<id>.jsonl.zst once it is compressed.
Each line is {"timestamp", "type", "payload"}. The first line is a session_meta record that
names the session id and the working folder. Conversation text sits in response_item records
whose payload is a message (role user, assistant or developer) or an agent_message from a
sub-agent; function_call and custom_tool_call records name the tools used.

One record per EXCHANGE, as in session_parse: one incoming message plus every assistant text
message until the next incoming message, with the names of the tools used in between. Tool
inputs and outputs, reasoning, developer messages, event_msg records and the context Codex
injects as user-role messages (AGENTS.md, environment context, plugin lists) stay out.

Plain files keep a byte cursor and are read incrementally, as Claude transcripts are. A
compressed file never changes, so it is streamed through `zstd -dc` once and its cursor says
so. A session that exists both plain and compressed is indexed once, from the plain file.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import session_parse
from .session_parse import DEFAULT_OPTS, DEFAULT_RULES, index_kind

PROVIDER = "codex"
ROLLOUT_RE = re.compile(r"rollout-(?P<ts>\d{4}-\d{2}-\d{2}T[\d-]+)-(?P<sid>[0-9a-f]{8}-[0-9a-f]{4}-"
                        r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl(?P<zst>\.zst)?")

# User-role messages Codex writes itself: project instructions, the environment block, the
# plugin list, shell and sub-agent notices, and the wrappers around attached images. Anything
# else in a user message is the person's own text. A harness block not listed here is kept as
# text and goes through the same scrubbing as any prompt.
INJECTED_PREFIXES = ("<environment_context", "<recommended_plugins", "<user_instructions",
                     "<user_shell_command", "<subagent_notification", "<turn_aborted",
                     "<apps_instructions", "<skill", "<image", "</image",
                     "# AGENTS.md instructions")
TOOL_RECORDS = ("function_call", "custom_tool_call")
# Tool output and reasoning records are large; their lines are passed over undecoded.
OUTPUT_MARKERS = (b'"type":"function_call_output"', b'"type":"custom_tool_call_output"',
                  b'"type":"reasoning"')
HEAD = 400


class ZstdError(RuntimeError):
    """zstd is missing, or a compressed rollout could not be decompressed."""


def zstd_path():
    path = shutil.which("zstd")
    if not path:
        raise ZstdError("zstd is not installed, and the Codex sessions folder holds .jsonl.zst "
                        "files that need it (macOS: brew install zstd)")
    return path


def short_home(path):
    """`path` with the home folder written as ~, so the stored folder names no account."""
    home = str(Path.home())
    path = " ".join(str(path or "").split())
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def text_blocks(content, kind):
    return [b.get("text", "") for b in content or []
            if isinstance(b, dict) and b.get("type") == kind and b.get("text", "").strip()]


def user_text(content):
    """The person's own text in a user message: the input_text blocks that are not injected
    context, joined; "" when the message is all injected context."""
    keep = [t for t in text_blocks(content, "input_text")
            if not t.lstrip().startswith(INJECTED_PREFIXES)]
    return "\n\n".join(t.strip() for t in keep).strip()


class Rollout:
    """A rollout file opened for reading lines, plain or through zstd."""

    def __init__(self, path):
        self.path, self.zst = Path(path), str(path).endswith(".zst")
        self.proc = None

    def __enter__(self):
        if self.zst:
            self.proc = subprocess.Popen([zstd_path(), "-dc", "--", str(self.path)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.fh = self.proc.stdout
        else:
            self.fh = open(self.path, "rb")
        return self

    def __exit__(self, exc_type, *_):
        self.fh.close()
        if self.proc:
            if exc_type:
                self.proc.kill()
            err = self.proc.stderr.read().decode("utf-8", "replace").strip()
            self.proc.stderr.close()
            code = self.proc.wait()
            if code and not exc_type:
                raise ZstdError("zstd could not read %s: %s" % (self.path.name, err or code))


def read_meta(path):
    """The session_meta payload of a rollout (its first line); {} when it is not one."""
    with Rollout(path) as r:
        line = r.fh.readline()
    try:
        d = json.loads(line)
    except ValueError:
        return {}
    return (d.get("payload") or {}) if d.get("type") == "session_meta" else {}


def parse(path, offset, line_no, next_n, opts=None, rules=DEFAULT_RULES, cwd=""):
    """Stream one rollout from a byte offset. Returns (records, cursor); the last record is
    still open (it may receive more assistant text on the next refresh). A compressed
    rollout is complete, so its last line counts even without a newline; a plain one stops
    before a line still being written. `cwd` is the working folder recorded on every record;
    when empty it comes from the file's session_meta line."""
    opts = opts or DEFAULT_OPTS
    records, cur = [], None
    complete = str(path).endswith(".zst")
    if not cwd and offset:
        cwd = short_home(read_meta(path).get("cwd"))
    owner = index_kind(opts, rules.owner_kind, rules)
    subagent = index_kind(opts, "subagent", rules)

    def start(kind, canon, name, body, ts, pos, ln):
        nonlocal next_n
        rec = dict(n=next_n, ex="EX", start_offset=pos, start_line=ln, end_line=ln,
                   sender_kind=kind, sender_canon=canon, sender_name=name, ts=ts, user_text=body,
                   assistant=[], tools={}, interrupted=False, seen=[], provider=PROVIDER)
        if cwd:
            rec["cwd"] = cwd
        next_n += 1
        return rec

    with Rollout(path) as f:
        if offset:
            if f.zst:
                raise ValueError("a compressed rollout is always read from the start")
            f.fh.seek(offset)
        pos = offset
        while True:
            line = f.fh.readline()
            if not line or (not line.endswith(b"\n") and not complete):
                break
            at, pos = pos, pos + len(line)
            line_no += 1
            head = line[:HEAD]
            if b'"response_item"' in head:
                if any(m in head for m in OUTPUT_MARKERS):
                    if cur:
                        cur["end_line"] = line_no
                    continue
            elif not (b'"session_meta"' in head or (b'"event_msg"' in head
                                                   and b'"turn_aborted"' in line)):
                if cur:
                    cur["end_line"] = line_no
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            p = d.get("payload")
            if not isinstance(p, dict):
                continue
            ts = d.get("timestamp")
            typ, ptype = d.get("type"), p.get("type")
            prev_end = cur["end_line"] if cur is not None else None
            if cur is not None:
                cur["end_line"] = line_no
            if typ == "session_meta":
                cwd = cwd or short_home(p.get("cwd"))
            elif typ == "event_msg":
                if ptype == "turn_aborted" and cur is not None:
                    cur["interrupted"] = True
            elif typ == "response_item" and ptype == "message":
                role = p.get("role")
                if role == "user":
                    body = user_text(p.get("content"))
                    if body:
                        if cur is not None:
                            cur["end_line"] = prev_end
                            records.append(cur)
                        cur = start(owner, rules.owner_kind, rules.owner_name, body,
                                    ts or "1970-01-01T00:00:00Z", at, line_no)
                elif role == "assistant" and cur is not None:
                    cur["assistant"] += [t.strip() for t in text_blocks(p.get("content"),
                                                                        "output_text")]
            elif typ == "response_item" and ptype == "agent_message":
                body = "\n\n".join(t.strip() for t in text_blocks(p.get("content"), "input_text"))
                if body:
                    if cur is not None:
                        cur["end_line"] = prev_end
                        records.append(cur)
                    cur = start(subagent, "subagent", str(p.get("author") or "agent")[:80], body,
                                ts or "1970-01-01T00:00:00Z", at, line_no)
            elif typ == "response_item" and ptype in TOOL_RECORDS and cur is not None:
                name = str(p.get("name") or "?")
                cur["tools"][name] = cur["tools"].get(name, 0) + 1
    if cur is not None:
        records.append(cur)
    return records, dict(next_n=next_n, next_aq=1, cron_seen={}, size=os.path.getsize(path),
                         source="zst" if complete else "plain")


class CodexSource:
    """The transcript source for `transcripts: {source: codex, path: <sessions folder>}`.
    One instance serves one refresh; its counters report what the refresh read."""

    keep_empty = True       # a session with no exchange still gets a cursor, so it is not reread

    def __init__(self):
        self.read = {"plain": 0, "zst": 0}
        self.failed = []

    def id_stem(self, sid):
        """Codex session ids are UUIDv7: the first characters are a timestamp, so sessions
        started within hours of each other share them. The random tail tells them apart."""
        return sid[-6:]

    def stats(self):
        return dict(files_read=dict(self.read), files_failed=list(self.failed))

    def list(self, project_dir, state_dir, automated_prefixes, sessions, exclude_automated):
        """(keep, skipped): every session in the folders as (sort key, session id, path),
        oldest first. A session in both forms is listed once, as its plain file. Codex runs
        no automated one-shot sessions of its own to skip, so nothing is skipped."""
        found = {}
        for d in (project_dir if isinstance(project_dir, (list, tuple)) else [project_dir]):
            for p in sorted(Path(d).rglob("rollout-*.jsonl*")):
                m = ROLLOUT_RE.fullmatch(p.name)
                if not m:
                    continue
                sid = m.group("sid")
                if sessions != "all" and sid not in sessions:
                    continue
                if sid not in found or (found[sid][2].name.endswith(".zst") and not m.group("zst")):
                    found[sid] = (m.group("ts"), sid, p)
        return sorted(found.values()), {}

    def present(self, dirs, sid):
        return [str(d) for d in dirs if any(d.rglob("rollout-*-%s.jsonl*" % sid))]

    def newest_mtime(self, dirs):
        return max((f.stat().st_mtime for d in dirs for f in d.rglob("rollout-*.jsonl*")),
                   default=0)

    def parse(self, path, cursor, old, opts, rules):
        """(new records, cursor, old records kept). A cursor from the other form of the file
        is not trusted: the session is read again from the start. A compressed file already
        read is not opened. A file that cannot be read is reported and left for the next
        refresh, with its stored records untouched."""
        kind = "zst" if path.name.endswith(".zst") else "plain"
        if cursor and cursor.get("source") != kind:
            cursor, old = None, []
        try:
            if cursor and (kind == "zst" or cursor["size"] == path.stat().st_size):
                return [], cursor, old
            if cursor and old:
                tail = old.pop()
                nx = 1 + max([r["n"] for r in old] or [0])
                recs, cur = parse(path, tail["start_offset"], tail["start_line"] - 1, nx, opts,
                                  rules, tail.get("cwd") or "")
            else:
                old = []
                recs, cur = parse(path, 0, 0, 1, opts, rules)
        except ZstdError as e:
            if "not installed" in str(e):
                raise
            self.failed.append(path.name)
            print("codex: skipped %s: %s" % (path.name, e), file=sys.stderr)
            return [], cursor, old
        self.read[kind] += 1
        return recs, cur, old
