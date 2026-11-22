"""Claude Code transcript parser with per-file cursors, shared by every session index.

One corpus record per EXCHANGE: one incoming
message (the user, a heartbeat cron prompt, a subagent or teammate hand-back, another
session) plus every assistant TEXT block until the next incoming message, with the names of
the tools used in between. Tool inputs and results, thinking blocks, system reminders, hook
output, skill bodies and compaction summaries stay out. The one tool_result kept is the
user's own answer to an AskUserQuestion or plan approval, which is the user's words.

Sources: every *.jsonl directly in the project folder (never subagents/), minus automated
one-shot runs. Each file is streamed line by line, never loaded whole.

Cursors: state/sessions/<sid>.jsonl holds the scrubbed records, state/sessions/<sid>.cursor.json
holds the byte size and counters. A grown file is reopened at the start of its last exchange,
which may have been open at the last run.

Who the owner is, which prompts are heartbeats, which runs are automated and the local zone
come from TranscriptRules (built from settings.toml by the refresh driver); the defaults
name nobody.
"""
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from fleetwatch import settings as fleetwatch_settings

AUTOMATED_EXACT = ("ok",)
SKIP_AUTOMATED_PREFIX = "automated-prefix"
SKIP_AUTOMATED_EXACT = "automated-exact"
SKIP_REASONS = (SKIP_AUTOMATED_PREFIX, SKIP_AUTOMATED_EXACT)

SYSREM_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
HOOK_BLOCK_RE = re.compile(r"<user-prompt-submit-hook>.*?</user-prompt-submit-hook>", re.S)
HOOK_LINE_RE = re.compile(r"^\s*(PreToolUse|PostToolUse|UserPromptSubmit|SessionStart|Stop|"
                          r"SubagentStop|Notification)\b.*\bhook\b.*$", re.M | re.I)
SKIP_PREFIXES = ("<local-command-caveat>", "<command-name>", "<local-command-stdout>",
                 "<local-command-stderr>", "<bash-input>", "<bash-stdout>", "[Image: ",
                 "Base directory for this skill", "<command-message>",
                 "<artifact-content-authored-by-others/>",
                 "This session is being continued from a previous conversation")
OTHER_PREFIXES = ("Your claude.ai usage limit has reset", "Continue from where you left off")
ATTR_RE = re.compile(r'(\w[\w-]*)="([^"]*)"')
ANSWER_HEADS = ("The user answered:", "Your questions have been answered:",
                "User has approved your plan", "The user approved")
# Byte markers that make a tool_result line worth parsing as an AskUserQuestion answer; every
# other tool_result line is tool output and is passed over without decoding its (huge) JSON.
ANSWER_MARKERS = tuple(h.encode() for h in ANSWER_HEADS) + (b"Questions asked:",)
TAIL_NOISE_RE = re.compile(r"\s*(You can now continue with these answers in mind\.|Read the answers "
                           r"carefully.*)$", re.S)


class TranscriptRules:
    """The parse settings that are not per index: the owner's canonical kind and display
    name, the heartbeat patterns, the automated-run prefixes and the local zone (day folders
    and displayed times). Stored records carry the kind and name, so changing them takes a
    --full refresh, as does changing the patterns or the zone."""

    def __init__(self, owner_kind=fleetwatch_settings.DEFAULT_OWNER_KIND, owner_name="",
                 heartbeat_patterns=fleetwatch_settings.DEFAULT_HEARTBEAT_PATTERNS,
                 automated_prefixes=fleetwatch_settings.DEFAULT_AUTOMATED_PREFIXES, tz=None):
        self.owner_kind = owner_kind
        self.owner_name = owner_name or "User"
        self.owner_label = "%s (user)" % owner_name if owner_name else "User"
        self.heartbeat_re = re.compile("|".join(heartbeat_patterns), re.M)
        self.automated_prefixes = tuple(automated_prefixes)
        self.tz = tz or fleetwatch_settings.system_tz()

    @classmethod
    def from_settings(cls, s):
        """From a fleetwatch.settings.IndexerSettings."""
        return cls(s.owner_kind, s.owner_name, s.heartbeat_patterns, s.automated_prefixes, s.tz)


DEFAULT_RULES = TranscriptRules()


def text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text", ""))
    return "\n".join(out)


def clean(text):
    text = SYSREM_RE.sub("", text)
    text = HOOK_BLOCK_RE.sub("", text)
    text = HOOK_LINE_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def tag_body(text, tag):
    m = re.search(r"<%s\b([^>]*)>(.*?)(?:</%s>|\Z)" % (tag, tag), text, re.S)
    if not m:
        return None, None
    return dict(ATTR_RE.findall(m.group(1))), m.group(2).strip()


DEFAULT_OPTS = dict(queued_as_human=True, kinds={}, peer_session_kinds={}, bare_exchange_ids=False)


def parse_opts(spec):
    """Per-index parse options from the registry's `transcripts:` block (all optional):
    queued_as_human (default true): a message typed while the session was busy arrives with a
    non-human origin; it is the human sender unless this is false.
    kinds: canonical kind (the owner's kind, heartbeat, subagent, peer-session, other) -> this
    index's own name for it; the key `owner` also names the owner's kind. peer_session_kinds: substring of the peer's session name (case-insensitive)
    -> kind name, checked before kinds["peer-session"]. bare_exchange_ids: EX-NNNN instead of
    S<sid6>-EX-NNNN, for an index that holds one session. withhold_patterns: regexes
    (case-insensitive); an exchange whose prompt or reply matches any of them keeps its id but
    none of its text, for material that must never be copied out of the transcript. The key is
    set only when the list is non-empty, so indexes without it keep their parse signature and
    are not reparsed."""
    o = dict(DEFAULT_OPTS)
    for k, v in (spec or {}).items():
        if k in o:
            o[k] = v
    o["kinds"] = dict(o["kinds"] or {})
    o["peer_session_kinds"] = {str(k).lower(): v for k, v in (o["peer_session_kinds"] or {}).items()}
    withhold = [str(p) for p in ((spec or {}).get("withhold_patterns") or [])]
    if withhold:
        o["withhold_patterns"] = withhold
    return o


WITHHELD_TEXT = "[Exchange withheld by index policy]"


def withhold_matching(recs, patterns):
    """Blank every record whose prompt, reply or working folder matches a pattern, before any
    scan, tag or write sees the text. Returns the number withheld."""
    if not patterns:
        return 0
    rx = re.compile("|".join("(?:%s)" % p for p in patterns), re.I)
    n = 0
    for r in recs:
        if (rx.search(r.get("user_text") or "") or rx.search(r.get("cwd") or "")
                or any(rx.search(a or "") for a in r.get("assistant") or [])):
            r["user_text"], r["assistant"], r["tools"] = WITHHELD_TEXT, [], {}
            r.pop("cwd", None)
            r["withheld"] = True
            n += 1
    return n


def opts_sig(opts):
    return hashlib.sha1(json.dumps(opts, sort_keys=True).encode()).hexdigest()[:10]


def exchange_id(sid, ex, n, opts, stem=None):
    """EX-NNNN with bare ids, else S<stem>-EX-NNNN; the stem is the session id's first six
    characters unless its source gave a longer one (id_stem)."""
    if opts["bare_exchange_ids"]:
        return "%s-%04d" % (ex, n)
    return "S%s-%s-%04d" % (sid[:6] if stem is None else stem, ex, n)


def index_kind(opts, canon, rules=DEFAULT_RULES):
    """The index's own name for canonical kind `canon` (opts["kinds"])."""
    kinds = opts["kinds"]
    if canon == rules.owner_kind:
        return kinds.get(canon) or kinds.get("owner") or canon
    return kinds.get(canon, canon)


def classify_incoming(d, text, opts=None, rules=DEFAULT_RULES):
    """Return (sender_kind, sender_name, body, canonical_kind) for an incoming message, or None
    to skip. sender_kind is the index's own name (opts["kinds"]); the canonical kind is kept so
    rendering and filtering do not depend on the names an index chose."""
    opts = opts or DEFAULT_OPTS
    r = _classify(d, text, opts, rules)
    if r is None or r[0] == "interrupt":
        return r
    kind, name, body = r
    if kind.startswith("peer:"):
        return (kind[5:], name, body, "peer-session")
    return (index_kind(opts, kind, rules), name, body, kind)


def _classify(d, text, opts, rules):
    beat = rules.heartbeat_re
    t = text.lstrip()
    if not t:
        return None
    if t.startswith(SKIP_PREFIXES):
        return None
    if t.startswith("[Request interrupted"):
        return ("interrupt", None, None)
    if t.startswith(OTHER_PREFIXES):
        return ("other", "system", t)
    if "<task-notification>" in t[:200]:
        _, inner = tag_body(t, "task-notification")
        inner = inner or t
        summary = (tag_body(inner, "summary")[1] or "").strip()
        result = (tag_body(inner, "result")[1] or "").strip()
        status = (tag_body(inner, "status")[1] or "").strip()
        body = "\n\n".join(x for x in (
            ("Status: %s" % status) if status else "", summary, result) if x)
        return ("subagent", summary[:80] or "task-notification", body or inner)
    for tag in ("cross-session-message", "teammate-message", "agent-message"):
        if "<%s" % tag in t[:600]:
            attrs, body = tag_body(t, tag)
            attrs = attrs or {}
            if tag == "cross-session-message":
                name = attrs.get("from-name") or "another session"
                for frag, pk in opts["peer_session_kinds"].items():
                    if frag in name.lower():
                        return ("peer:" + pk, name, body or t)
                return ("peer-session", name, body or t)
            name = attrs.get("teammate_id") or attrs.get("from") or "agent"
            if attrs.get("summary"):
                body = "Summary: %s\n\n%s" % (attrs["summary"], body)
            return ("subagent", name, body or t)
    if d.get("scheduledTaskId") or ("queuePriority" in d and d.get("isMeta")) \
            or (d.get("isMeta") and beat.search(t[:400])):
        return ("heartbeat", "cron", t)
    if d.get("isMeta"):
        if beat.search(t[:200]):
            return ("heartbeat", "cron", t)
        return ("other", "meta", t)
    origin = d.get("origin")
    if origin and "human" not in str(origin):
        if beat.search(t[:400]):
            return ("heartbeat", "cron", t)
        if opts["queued_as_human"]:
            return (rules.owner_kind, rules.owner_name, t)
        return ("other", "queued", t)
    return (rules.owner_kind, rules.owner_name, t)


def answer_text(blocks):
    """The user's answer to an AskUserQuestion (or an ExitPlanMode approval), delivered as a
    tool_result: the questions, the option picked and any text typed. A rejected question set
    that lists 'Questions asked' is kept too, because declining to answer is itself a message.
    Any other tool result is tool output and stays out."""
    for b in blocks:
        if not (isinstance(b, dict) and b.get("type") == "tool_result"):
            continue
        c = b.get("content")
        t = c if isinstance(c, str) else text_of(c)
        t = (t or "").strip()
        if t.startswith(ANSWER_HEADS):
            return "[User's answer to AskUserQuestion] " + TAIL_NOISE_RE.sub("", t).strip()
        if "Questions asked:" in t and "doesn't want to proceed" in t:
            m = re.search(r"Questions asked:.*", t, re.S)
            return ("[User declined to answer AskUserQuestion and wants to clarify] "
                    + (m.group(0) if m else t)).strip()
    return None


def iso_pair(ts, tz=None):
    """(UTC stamp, local time with zone abbreviation) for a transcript timestamp; `tz`
    defaults to DEFAULT_RULES.tz."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    local = dt.astimezone(tz or DEFAULT_RULES.tz)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), local.strftime("%Y-%m-%d %H:%M %Z")


def first_user_text(path):
    """First real user prompt of a transcript, reading only until it appears."""
    with open(path, "rb") as f:
        for line in f:
            if b'"type":"user"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "user":
                c = d.get("message", {}).get("content")
                if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                               for b in c):
                    continue
                t = clean(text_of(c)).lstrip()
                if t and not t.startswith(SKIP_PREFIXES):
                    return t
    return ""


def first_ts(path):
    with open(path, "rb") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("timestamp"):
                return d["timestamp"]
    return "9999"


def parse(path, offset, line_no, next_n, cron_seen, next_aq=1, opts=None, rules=DEFAULT_RULES):
    """Stream one transcript from a byte offset. Returns (records, cursor); the last record
    is still open (it may receive more assistant text on the next refresh)."""
    opts = opts or DEFAULT_OPTS
    records, cur = [], None

    def start(kind, name, body, ts, pos, ln, aq=False, canon=None):
        nonlocal next_n, next_aq
        rec = dict(n=next_aq if aq else next_n, ex="AQ" if aq else "EX", start_offset=pos,
                   start_line=ln, end_line=ln, sender_kind=kind, sender_canon=canon or kind, sender_name=name, ts=ts,
                   user_text=body, assistant=[], tools={}, interrupted=False, seen=[])
        if aq:
            next_aq += 1
        else:
            next_n += 1
        return rec

    with open(path, "rb") as f:
        f.seek(offset)
        while True:
            pos = f.tell()
            line = f.readline()
            if not line or not line.endswith(b"\n"):
                break      # end, or a line still being written: stop before it
            line_no += 1
            if b'"type":"tool_result"' in line and not any(m in line for m in ANSWER_MARKERS):
                if cur:
                    cur["end_line"] = line_no
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("isSidechain"):
                continue
            typ = d.get("type")
            incoming = None
            if typ == "user":
                c = d.get("message", {}).get("content")
                if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                               for b in c):
                    ans = answer_text(c)
                    if ans is None:
                        if cur:
                            cur["end_line"] = line_no
                        continue
                    if cur is not None:
                        records.append(cur)
                    cur = start(index_kind(opts, rules.owner_kind, rules), "AskUserQuestion", ans,
                                d.get("timestamp"), pos, line_no, aq=True, canon=rules.owner_kind)
                    continue
                if d.get("isCompactSummary"):
                    continue
                incoming = (d, clean(text_of(c)), d.get("timestamp"))
            elif typ == "attachment" and (d.get("attachment") or {}).get("type") == "queued_command":
                a = d["attachment"]
                meta = dict(a)
                incoming = (meta, clean(text_of(a.get("prompt"))), a.get("timestamp") or d.get("timestamp"))
            elif typ == "assistant" and cur is not None:
                msg = d.get("message", {})
                mid = msg.get("id", "")
                for b in msg.get("content") or []:
                    bt = b.get("type")
                    if bt == "text" and b.get("text", "").strip():
                        key = hashlib.sha1((mid + b["text"]).encode()).hexdigest()[:16]
                        if key not in cur["seen"]:
                            cur["seen"].append(key)
                            cur["assistant"].append(clean(b["text"]))
                    elif bt == "tool_use":
                        nm = b.get("name", "?")
                        cur["tools"][nm] = cur["tools"].get(nm, 0) + 1
                cur["end_line"] = line_no
                continue
            else:
                if cur is not None:
                    cur["end_line"] = line_no
                continue
            if incoming is None:
                continue
            meta, text, ts = incoming
            cls = classify_incoming(meta, text, opts, rules)
            if cls is None:
                continue
            kind, name, body = cls[:3]
            canon = cls[3] if len(cls) > 3 else kind
            if kind == "interrupt":
                if cur is not None:
                    cur["interrupted"] = True
                continue
            if canon == "heartbeat":
                h = hashlib.sha1(re.sub(r"\s+", " ", body).encode()).hexdigest()[:16]
                if h in cron_seen and cron_seen[h] != next_n:
                    first = body.strip().splitlines()[0][:200]
                    body = "[Heartbeat prompt identical to %s] %s" % (cron_seen[h], first)
                else:
                    cron_seen[h] = next_n
            if cur is not None:
                records.append(cur)
            cur = start(kind, name, body, ts or (cur or {}).get("ts") or "1970-01-01T00:00:00Z",
                        pos, line_no, canon=canon)
    if cur is not None:
        records.append(cur)
    return records, dict(next_n=next_n, next_aq=next_aq, cron_seen=cron_seen,
                         size=os.path.getsize(path))


def drop_replays(recs):
    """A resumed session replays earlier history: the same incoming message appears again with
    the same timestamp. Keep the first copy; fold any assistant text only the replay carries
    into it. The session file on disk stays raw, so ids never depend on this pass."""
    first, out = {}, []
    for r in recs:
        key = (r["sender_kind"], r["ts"], hashlib.sha1(
            re.sub(r"\s+", " ", r["user_text"]).encode()).hexdigest()[:16])
        if key in first:
            orig = first[key]
            for a in r["assistant"]:
                if a not in orig["assistant"]:
                    orig["assistant"].append(a)
            for k, v in r["tools"].items():
                orig["tools"][k] = max(orig["tools"].get(k, 0), v)
            continue
        first[key] = r
        out.append(r)
    return out


def session_list(project_dir, state_dir, automated_prefixes=None,
                 sessions="all", exclude_automated=True):
    """Conversation transcripts directly in project_dir, ordered by first timestamp, plus the
    skipped set (automated one-shot runs, cached by file size). project_dir may be a list:
    a session whose working directory moved has transcripts in several project folders.
    Session ids are UUIDs, so files from different folders never share a cursor.
    `automated_prefixes` defaults to DEFAULT_RULES.automated_prefixes."""
    if automated_prefixes is None:
        automated_prefixes = DEFAULT_RULES.automated_prefixes
    skip_path = state_dir / "skipped-sessions.json"
    skipped = json.loads(skip_path.read_text()) if skip_path.exists() else {}
    # The skip list lives in state/, which holds scrubbed text only, so a reason is a fixed
    # code and never prompt text; an entry with any other reason is rewritten to a code.
    for entry in skipped.values():
        if entry.get("reason") not in SKIP_REASONS:
            entry["reason"] = SKIP_AUTOMATED_PREFIX
    keep = []
    dirs = project_dir if isinstance(project_dir, (list, tuple)) else [project_dir]
    for p in sorted(f for d in dirs for f in Path(d).glob("*.jsonl")):
        sid = p.stem
        if sessions != "all" and sid not in sessions:
            continue
        size = p.stat().st_size
        if exclude_automated:
            if sid in skipped and skipped[sid]["size"] == size:
                continue
            ft = first_user_text(p)
            if ft.startswith(tuple(automated_prefixes)):
                skipped[sid] = dict(size=size, reason=SKIP_AUTOMATED_PREFIX)
                continue
            if ft.strip() in AUTOMATED_EXACT:
                skipped[sid] = dict(size=size, reason=SKIP_AUTOMATED_EXACT)
                continue
        keep.append((first_ts(p), sid, p))
    keep.sort()
    skip_path.write_text(json.dumps(skipped, indent=1))
    return keep, skipped


def read_records(sp):
    return [json.loads(x) for x in sp.read_text().splitlines() if x.strip()]


ON_MISSING = ("hold", "remove")


def hold_records(sess, sid, scanner, sig, full, opts):
    """The stored records of a session whose transcript is gone, scrubbed under the current
    rules. When the stored signature is not `sig` (other scan rules or parse options) or
    `full` is set, the stored text is scanned again and rewritten with `sig`; records
    scrubbed under other rules are never returned as they are. The raw transcript is gone,
    so parse options cannot be reapplied; only the scrub is."""
    sp, curp = sess / (sid + ".jsonl"), sess / (sid + ".cursor.json")
    recs = read_records(sp)
    if not recs:
        return recs
    try:
        cursor = json.loads(curp.read_text()) if curp.exists() else {}
    except ValueError:
        cursor = {}
    if cursor.get("scan_version") == sig and not full:
        return recs
    # A drop is never undone here: the text that justified it (an anonymous id beside a
    # name) was redacted when it was stored, so a second scan cannot see it again.
    dropped = {i: r["dropped"] for i, r in enumerate(recs) if r.get("dropped")}
    withhold_matching(recs, opts.get("withhold_patterns"))
    scanner.scan_records(recs)
    for i, why in dropped.items():
        recs[i].setdefault("dropped", why)
    tmp = sp.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, sp)
    cursor["scan_version"] = sig
    curp.write_text(json.dumps(cursor))
    return recs


class ClaudeSource:
    """The transcript source for Claude Code project folders (the default). A source supplies
    list() (the sessions in the folders), parse() (what a refresh reads of one session file),
    present(), newest_mtime(), id_stem() and stats(); see codex_parse.CodexSource for the
    other one.
    keep_empty: store a cursor for a session that has no exchange yet, so it is not reread."""

    keep_empty = False

    def stats(self):
        return {}

    def id_stem(self, sid):
        """The part of a session id that exchange ids are built from (its first six characters)."""
        return sid[:6]

    def list(self, project_dir, state_dir, automated_prefixes, sessions, exclude_automated):
        return session_list(project_dir, state_dir, automated_prefixes, sessions, exclude_automated)

    def present(self, dirs, sid):
        return [str(d) for d in dirs if (d / (sid + ".jsonl")).exists()]

    def newest_mtime(self, dirs):
        return max((f.stat().st_mtime for d in dirs for f in d.glob("*.jsonl")), default=0)

    def parse(self, path, cursor, old, opts, rules):
        """(new records, cursor, old records kept) for one session file, given its stored
        cursor and records: nothing when the file has not grown, the open last exchange
        onward when it has, everything otherwise."""
        if cursor and cursor["size"] == path.stat().st_size and old:
            return [], cursor, old
        if old and cursor:
            tail = old.pop()
            nx = 1 + max([r["n"] for r in old if r.get("ex", "EX") == "EX"] or [0])
            na = 1 + max([r["n"] for r in old if r.get("ex") == "AQ"] or [0])
            recs, cur = parse(path, tail["start_offset"], tail["start_line"] - 1, nx,
                              dict(cursor["cron_seen"]), na, opts, rules)
            return recs, cur, old
        recs, cur = parse(path, 0, 0, 1, {}, 1, opts, rules)
        return recs, cur, []


def load_transcripts(project_dir, state_dir, scanner, full=False, triage=None,
                     automated_prefixes=None, sessions="all",
                     exclude_automated=True, opts=None, rules=DEFAULT_RULES, on_missing="hold",
                     source=None):
    """Parse every grown transcript from its cursor, scrub the new records, persist them, and
    return (records, skipped, stats). Records come back in session order with replays dropped.
    A change of scanner.version reparses everything, because the stored records were scrubbed
    under the old rules. `source` supplies the sessions and the per-file reading
    (ClaudeSource by default)."""
    opts = opts or DEFAULT_OPTS
    source = source or ClaudeSource()
    sig = "%s|%s" % (scanner.version, opts_sig(opts))
    sess = state_dir / "sessions"
    sess.mkdir(parents=True, exist_ok=True)
    if automated_prefixes is None:
        automated_prefixes = rules.automated_prefixes
    keep, skipped = source.list(project_dir, state_dir, automated_prefixes, sessions,
                                exclude_automated)
    sids = {sid for _, sid, _ in keep}
    # A stored session whose transcript is in no registered folder is held from its stored
    # records by default: the index keeps history that transcript retention deletes. With
    # on_missing "remove" it leaves the index instead, unless a registered folder is itself
    # missing (archived, moved, not mounted), when deleted and unreachable cannot be told
    # apart. A session the `sessions` list or the automated-run skip excludes always leaves.
    if on_missing not in ON_MISSING:
        raise ValueError("transcripts.on_missing must be one of %s, not %r"
                         % (", ".join(ON_MISSING), on_missing))
    dirs = project_dir if isinstance(project_dir, (list, tuple)) else [project_dir]
    all_dirs_present = all(Path(d).is_dir() for d in dirs)
    held, removed = [], set()
    for p in sorted(sess.glob("*.jsonl")) + sorted(sess.glob("*.cursor.json")):
        sid = p.name.split(".")[0]
        if sid in sids:
            continue
        excluded = (sessions != "all" and sid not in sessions) or sid in skipped
        if not excluded and not (on_missing == "remove" and all_dirs_present):
            if p.name == sid + ".jsonl" and triage is None:
                recs = hold_records(sess, sid, scanner, sig, full, opts)
                if recs:
                    held.append((recs[0]["ts"], sid, recs))
            continue
        removed.add(sid)
        if triage is None:
            p.unlink()          # a session that became an automated run, left the list, or vanished
    allrecs, n_new, n_files = [], 0, 0
    for _ts0, sid, path in sorted(keep + held, key=lambda k: (k[0], k[1])):
        sp = sess / (sid + ".jsonl")
        curp = sess / (sid + ".cursor.json")
        if isinstance(path, list):
            allrecs += drop_replays(path)
            continue
        old, cursor = [], None
        if sp.exists() and curp.exists() and not full and triage is None:
            cursor = json.loads(curp.read_text())
            if cursor.get("scan_version") != sig:
                cursor = None
            else:
                old = read_records(sp)
        recs, cur, old = source.parse(path, cursor, old, opts, rules)
        for r in recs:
            r.pop("seen", None)
            r["session"] = sid
            r["id"] = exchange_id(sid, r.get("ex", "EX"), r["n"], opts, source.id_stem(sid))
        withhold_matching(recs, opts.get("withhold_patterns"))
        if recs:
            n_files += 1
            scanner.scan_records(recs, triage)
        n_new += len(recs)
        merged = old + recs
        if triage is None and (recs or (source.keep_empty and cur is not None
                                        and cur is not cursor)):
            tmp = sp.with_suffix(".tmp")
            with open(tmp, "w") as fh:
                for r in merged:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(tmp, sp)
            cur["scan_version"] = sig
            curp.write_text(json.dumps(cur))
        # a session file touched in the last 15 minutes still has an open final exchange
        part = drop_replays(merged)
        try:
            recent = path.stat().st_mtime > time.time() - 900
        except OSError:
            recent = False          # the file moved away since it was listed
        if part and recent:
            part[-1]["open"] = True
        allrecs += part
    # heartbeat pointers store ids as numbers in cron_seen; render them as exchange ids
    for r in allrecs:
        if r.get("sender_canon", r["sender_kind"]) == "heartbeat" \
                and r["user_text"].startswith("[Heartbeat prompt identical to "):
            m = re.match(r"\[Heartbeat prompt identical to (\d+)\]", r["user_text"])
            if m:
                r["user_text"] = r["user_text"].replace(
                    "identical to %s]" % m.group(1),
                    "identical to %s]" % exchange_id(r["session"], "EX", int(m.group(1)), opts,
                                                     source.id_stem(r["session"])), 1)
    return allrecs, skipped, dict(sessions=len(keep), new=n_new, files_reparsed=n_files,
                                  removed=len(removed), held=len(held), **source.stats())
