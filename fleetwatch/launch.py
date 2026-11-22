"""`fleetwatch launch record`: record Codex and Antigravity launches from a Claude Code PreToolUse hook.

Reads a PreToolUse JSON payload on stdin (session_id, cwd, tool_input.command). When the command
starts a recognized Codex or Antigravity job, appends one JSON line to launches.jsonl in
fleetwatch's state folder: session_id, session_name (when status files supply it), tool, cwd
(--worktree when given, else cwd, resolved to an absolute path) and an ISO timestamp.

Constraint: `fleetwatch launch record` must always exit 0 and write nothing to stdout, so a hook
invocation can never block or alter the tool call, even on malformed or unreadable input.
"""
import glob
import json
import os
import re
import shlex
import shutil
import sys
from datetime import datetime

from . import data, settings

LEDGER_NAME = "launches.jsonl"
MAX_INPUT = 256 * 1024

CODEX_COMPANION_SUBS = frozenset({"task", "review", "adversarial-review"})
AGY_COMPANION_SUBS = frozenset({"task", "review", "plan", "ask"})
SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
JS_RUNTIMES = frozenset({"node", "nodejs", "bun", "deno"})
SEPARATORS = frozenset({";", "&&", "||", "|", "&", "\n"})
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Value-taking flags that may appear before a subcommand on `codex` or companion scripts.
CODEX_VAL_FLAGS = frozenset({
    "-m", "--model", "-p", "--profile", "-c", "--config", "-C", "--cd",
    "-s", "--sandbox", "-a", "--ask-for-approval", "--worktree",
})
COMPANION_VAL_FLAGS = frozenset({"--worktree", "--model", "--base", "--cwd", "-C"})
NODE_VAL_FLAGS = frozenset({"-r", "--require", "--import", "--loader", "--conditions", "--env-file"})
NODE_INLINE_FLAGS = frozenset({"-e", "--eval", "-p", "--print"})
ENV_VAL_FLAGS = frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"})
TIMEOUT_VAL_FLAGS = frozenset({"-k", "--kill-after", "-s", "--signal"})
HELP_FLAGS = frozenset({"-h", "--help", "-V", "--version"})


def ledger_path(state_dir=None):
    """Path to the launch ledger in fleetwatch's state folder."""
    if state_dir is None:
        override = os.environ.get("FLEETWATCH_LAUNCH_LEDGER")
        if override:
            return os.path.expanduser(override)
        state_dir = settings.state_dir()
    return os.path.join(state_dir, LEDGER_NAME)


def _tokenize(text):
    """Split shell text into tokens while keeping quoted strings intact and preserving
    command separators including unquoted newlines."""
    separated = []
    quote = None
    escaped = False
    for ch in text:
        if escaped:
            separated.append(ch)
            escaped = False
        elif ch == "\\" and quote != "'":
            separated.append(ch)
            escaped = True
        elif ch in ("'", '"'):
            if quote is None:
                quote = ch
            elif quote == ch:
                quote = None
            separated.append(ch)
        elif ch == "\n" and quote is None:
            separated.append(" ; ")
        else:
            separated.append(ch)
    lex = shlex.shlex("".join(separated), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.whitespace = " \t\r\n"
    lex.commenters = ""
    return list(lex)


def _segments(command):
    """Split a shell command string into simple command token lists.

    Falls back to line-by-line tokenization when a multi-line body (such as a heredoc)
    contains unbalanced quotes.
    """
    try:
        tokens = _tokenize(command)
    except ValueError:
        segs = []
        for line in command.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                segs.extend(_split_tokens(_tokenize(line)))
            except ValueError:
                continue
        return segs
    return _split_tokens(tokens)


def _split_tokens(tokens):
    segs, cur = [], []
    for tok in tokens:
        if tok in SEPARATORS or (tok and all(c in ";|&\n" for c in tok)):
            if cur:
                segs.append(cur)
                cur = []
        else:
            cur.append(tok)
    if cur:
        segs.append(cur)
    return segs


def _strip_wrappers(tokens):
    """Drop leading shell variable assignments and benign command wrappers (env, nice, nohup,
    time, command, exec, timeout)."""
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if ASSIGN_RE.match(tok):
            i += 1
            continue
        base = os.path.basename(tok)
        if base == "env":
            i += 1
            while i < n:
                if tokens[i] == "--":
                    i += 1
                    break
                if tokens[i] in ENV_VAL_FLAGS:
                    i += 2
                    continue
                if tokens[i].startswith("-") or ASSIGN_RE.match(tokens[i]):
                    i += 1
                    continue
                break
            continue
        if base == "timeout":
            i += 1
            while i < n and tokens[i].startswith("-"):
                if tokens[i] == "--":
                    i += 1
                    break
                if tokens[i] in TIMEOUT_VAL_FLAGS:
                    i += 2
                else:
                    i += 1
            if i < n:
                i += 1  # duration argument
            continue
        if base == "nice":
            i += 1
            while i < n and tokens[i].startswith("-"):
                if tokens[i] in ("-n", "--adjustment"):
                    i += 2
                else:
                    i += 1
            continue
        if base in ("nohup", "time", "command", "exec"):
            i += 1
            if i < n and tokens[i] == "--":
                i += 1
            continue
        break
    return tokens[i:]


def _extract_worktree(args):
    """Value of `--worktree PATH` or `--worktree=PATH` in `args`, or None."""
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--worktree="):
            val = a[len("--worktree="):].strip()
            if val:
                return val
        elif a == "--worktree":
            if i + 1 < len(args) and args[i + 1] and not args[i + 1].startswith("-"):
                return args[i + 1]
        i += 1
    return None


def _first_positional(args, val_flags):
    """First non-flag argument in `args`, or None if a help/version flag precedes it."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return args[i + 1] if i + 1 < len(args) else None
        if a in HELP_FLAGS:
            return None
        if a.startswith("-"):
            if a in val_flags and "=" not in a:
                i += 2
            else:
                i += 1
            continue
        return a
    return None


def _match_segment(tokens):
    """(tool, worktree_arg) if one simple command starts a Codex or Antigravity job, else None."""
    tokens = _strip_wrappers(tokens)
    if not tokens:
        return None
    cmd, args = tokens[0], tokens[1:]
    base = os.path.basename(cmd)
    if any(a in HELP_FLAGS for a in args):
        return None

    def review_root(script, sargs):
        root = _option_value(sargs, "--worktree-root") or _extract_worktree(sargs)
        if root:
            return root
        located = script if os.path.dirname(script) else shutil.which(script)
        if located:
            return os.path.join(os.path.dirname(located), "work", "worktrees")
        return None

    if base in JS_RUNTIMES:
        i = 0
        while i < len(args):
            a = args[i]
            if a in NODE_INLINE_FLAGS or a in HELP_FLAGS:
                return None
            if a == "--":
                i += 1
                break
            if a.startswith("-"):
                i += 2 if (a in NODE_VAL_FLAGS and "=" not in a) else 1
                continue
            break
        if i >= len(args):
            return None
        script, sargs = os.path.basename(args[i]), args[i + 1:]
        if script == "codex-companion.mjs":
            sub = _first_positional(sargs, COMPANION_VAL_FLAGS)
            if sub in CODEX_COMPANION_SUBS:
                return ("codex-companion", _extract_worktree(sargs))
        elif script == "agy-companion.mjs":
            sub = _first_positional(sargs, COMPANION_VAL_FLAGS)
            if sub in AGY_COMPANION_SUBS:
                return ("agy-companion", _extract_worktree(sargs))
        return None

    if base in SHELLS:
        i = 0
        while i < len(args):
            a = args[i]
            if a == "-c" and i + 1 < len(args):
                return parse_command(args[i + 1])
            if a == "--":
                i += 1
                break
            if a.startswith("-"):
                i += 1
                continue
            break
        if i >= len(args):
            return None
        script, sargs = os.path.basename(args[i]), args[i + 1:]
        if script == "codex-task-in-worktree.sh":
            return ("codex-task-in-worktree", _extract_worktree(sargs))
        if script == "multi-review.sh":
            return ("multi-review", review_root(args[i], sargs))
        return None

    if base == "codex":
        sub = _first_positional(args, CODEX_VAL_FLAGS)
        if sub == "exec":
            return ("codex", _extract_worktree(args))
        return None
    if base == "codex-companion.mjs":
        sub = _first_positional(args, COMPANION_VAL_FLAGS)
        if sub in CODEX_COMPANION_SUBS:
            return ("codex-companion", _extract_worktree(args))
        return None
    if base == "agy-companion.mjs":
        sub = _first_positional(args, COMPANION_VAL_FLAGS)
        if sub in AGY_COMPANION_SUBS:
            return ("agy-companion", _extract_worktree(args))
        return None
    if base == "codex-task-in-worktree.sh":
        return ("codex-task-in-worktree", _extract_worktree(args))
    if base == "multi-review.sh":
        return ("multi-review", review_root(cmd, args))
    if base == "agy":
        return ("agy", _extract_worktree(args))
    return None


def parse_command(command):
    """(tool, worktree_arg) if `command` starts a Codex or Antigravity job, else None."""
    if not isinstance(command, str) or not command.strip():
        return None
    for seg in _segments(command):
        hit = _match_segment(seg)
        if hit is not None:
            return hit
    return None


def _option_value(args, flag):
    for i, arg in enumerate(args):
        if arg.startswith(flag + "="):
            return arg[len(flag) + 1:] or None
        if arg == flag and i + 1 < len(args):
            return args[i + 1]
    return None


def resolve_folder(worktree, cwd):
    """Working folder as an absolute path: `--worktree` when given (relative to `cwd`), else `cwd`."""
    cwd_clean = os.path.expanduser(cwd.strip()) if isinstance(cwd, str) and cwd.strip() else ""
    if isinstance(worktree, str) and worktree.strip():
        wt = os.path.expanduser(worktree.strip())
        if os.path.isabs(wt):
            return os.path.abspath(wt)
        if cwd_clean:
            return os.path.abspath(os.path.join(cwd_clean, wt))
        return os.path.abspath(wt)
    if cwd_clean:
        return os.path.abspath(cwd_clean)
    return None


def session_name_from_status(session_id, status_dir=None):
    """Session name from fleetwatch's status files for `session_id`, or None."""
    if not session_id:
        return None
    folder = status_dir or data.default_paths()["status"]
    best_ts, best_name = None, None
    try:
        paths = sorted(glob.glob(os.path.join(folder, "*.json")))
    except OSError:
        return None
    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            doc = data.load_json(path)
        except Exception:
            continue
        if not isinstance(doc, dict) or doc.get("sessionId") != session_id:
            continue
        name = doc.get("session")
        if not isinstance(name, str) or not name.strip():
            continue
        ts = data.to_epoch(doc.get("updated"))
        if best_name is None or (ts is not None and (best_ts is None or ts >= best_ts)):
            best_ts = ts
            best_name = data.clean(name).strip()
    return best_name or None


def append_entry(entry, path=None):
    """Append one JSON line to the ledger with owner-only file modes."""
    target = path or ledger_path()
    folder = os.path.dirname(target)
    if folder:
        os.makedirs(folder, mode=0o700, exist_ok=True)
    line = (json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def record_payload(payload, ledger=None, status_dir=None, now=None):
    """Process one PreToolUse hook payload; append and return the ledger entry on a match, else None."""
    if not isinstance(payload, dict):
        return None
    tool_name = payload.get("tool_name")
    if tool_name is not None and tool_name != "Bash":
        return None
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid.strip():
        return None
    from . import indexes
    session = indexes.calling_session()
    if session is None or session[1] != sid.strip():
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    hit = parse_command(command)
    if hit is None:
        return None
    tool, worktree = hit
    folder = resolve_folder(worktree, payload.get("cwd"))
    if not folder:
        return None
    from .memory.routing import is_same_or_parent
    home = os.path.expanduser("~")
    session_cwd = resolve_folder(None, payload.get("cwd"))
    if is_same_or_parent(folder, home) or (session_cwd and folder != session_cwd
                                         and is_same_or_parent(folder, session_cwd)):
        return None
    sid_clean = data.clean(sid).strip()
    sname = session_name_from_status(sid_clean, status_dir)
    ts = (now or datetime.now(settings.local_tz())).isoformat(timespec="seconds")
    entry = {"session_id": sid_clean, "tool": tool, "cwd": folder,
             "session_cwd": session_cwd, "ts": ts}
    if sname:
        entry["session_name"] = sname
    append_entry(entry, ledger)
    return entry


def read_ledger(path=None):
    """Read valid entries from the launch ledger. Malformed lines or a missing file are skipped."""
    target = path or ledger_path()
    if not os.path.isfile(target):
        return []
    entries = []
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                sid = d.get("session_id")
                cwd = d.get("cwd")
                ts_raw = d.get("ts")
                ts_epoch = data.to_epoch(ts_raw)
                if not isinstance(sid, str) or not sid.strip():
                    continue
                if not isinstance(cwd, str) or not cwd.strip():
                    continue
                if ts_epoch is None:
                    continue
                entries.append({
                    "session_id": sid.strip(),
                    "session_name": d["session_name"].strip()
                    if isinstance(d.get("session_name"), str) and d["session_name"].strip() else None,
                    "tool": str(d.get("tool") or ""),
                    "cwd": cwd.strip(),
                    "session_cwd": d.get("session_cwd") if isinstance(d.get("session_cwd"), str) else None,
                    "ts": ts_raw,
                    "ts_epoch": ts_epoch,
                    "line_no": line_no,
                })
    except OSError:
        return []
    return entries


def _parse_record_flags(rest):
    """Parse optional --ledger and --status-dir flags without ever exiting or printing."""
    ledger, status_dir = None, None
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--ledger" and i + 1 < len(rest):
            ledger = rest[i + 1]
            i += 2
        elif a.startswith("--ledger="):
            ledger = a[len("--ledger="):] or None
            i += 1
        elif a == "--status-dir" and i + 1 < len(rest):
            status_dir = rest[i + 1]
            i += 2
        elif a.startswith("--status-dir="):
            status_dir = a[len("--status-dir="):] or None
            i += 1
        else:
            i += 1
    return ledger, status_dir


def record_main(argv, stdin=None, now=None):
    """`fleetwatch launch record`: always exits 0 and never prints to stdout."""
    try:
        ledger, status_dir = _parse_record_flags(argv)
        stream = sys.stdin if stdin is None else stdin
        raw = stream.read(MAX_INPUT + 1)
        if not raw or len(raw) > MAX_INPUT:
            return 0
        payload = json.loads(raw)
        record_payload(payload, ledger=ledger, status_dir=status_dir, now=now)
    except BaseException:
        return 0
    return 0


def main(argv, stdin=None):
    argv = list(argv)
    if argv[:1] == ["record"]:
        return record_main(argv[1:], stdin=stdin)
    if argv[:1] in ([], ["-h"], ["--help"]):
        print("usage: fleetwatch launch record [--ledger PATH] [--status-dir PATH]\n\n"
              "Read a Claude Code PreToolUse hook payload on stdin and append matching\n"
              "Codex or Antigravity launches to the launch ledger. Always exits 0 and\n"
              "prints nothing to stdout.")
        return 0
    print("fleetwatch launch: unknown command %r (expected: record)" % argv[0], file=sys.stderr)
    return 0
