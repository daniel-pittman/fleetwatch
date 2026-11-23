"""Type a message into a running session's Claude Code prompt from the board.

The text reaches Claude as input typed at its own terminal, so it carries the owner's authority; a
message between sessions never does. That makes the target the whole safety question: text typed into
a shell runs as a command, text typed into an editor runs as editor commands, and text typed into a
permission prompt answers it. Every check here runs at send time against fresh reads (the tty's
processes, the registry, `claude agents`), never against the last board refresh, and a check that cannot
tell refuses. fleetwatch never stores or logs the message.
"""
import os
import shlex
import subprocess
import time
import unicodedata

from . import data, iterm

MAX_LEN = 2000
CLAUDE_COMM = "claude"

# Registry states in which Claude Code itself is reading its prompt. `shell` is Claude with a background
# shell running: that shell is in its own session and process group, off the terminal's foreground.
# Anything else, a missing or unknown state included, is refused.
SAFE_STATES = {"idle", "busy", "shell"}

# Programs that read the terminal when Claude starts them in its own foreground group (Ctrl-G opens
# $EDITOR there). Helpers such as MCP servers, caffeinate or a Bash tool's `bash -c` do not read it.
INTERACTIVE = {"vi", "vim", "nvim", "nano", "emacs", "micro", "hx", "kak", "less", "more", "most", "man"}

# The pause between typing the text and sending the return: Claude Code reads one `write text` as a
# paste, and a return that arrives before the paste ends is taken into the box instead of submitting it.
SETTLE_S = 0.2

TYPED = "typed but not submitted"
ONE_LINE = "one line only: no newline, tab or other control character"

# Cc covers newline, tab, escape (which could end a bracketed paste) and DEL; Zl and Zp are the Unicode
# line and paragraph separators, which break a line the way a newline does.
_BREAKS = {"Cc", "Zl", "Zp"}


def check_text(text):
    """None when text may be sent, else the reason it may not."""
    if has_break(text):
        return ONE_LINE
    if not text.strip():
        return "type a message first"
    if len(text) > MAX_LEN:
        return "too long: %d of %d characters" % (len(text), MAX_LEN)
    return None


def has_break(text):
    return any(unicodedata.category(c) in _BREAKS for c in text)


def parse_terminal(text):
    """What `ps -o pid=,pgid=,tpgid=,stat=,comm= -t TTY` says about a terminal: {"pids": every pid on it,
    "fg": the foreground group or None}. The group is {"leader", "stat", "command", "members"}, members
    being (pid, command) for the group's other processes; None when the terminal has no foreground group
    or no process leads it. A command may hold spaces."""
    rows = []
    for line in text.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3], parts[4].strip()))
    out = {"pids": {r[0] for r in rows}, "fg": None}
    for pid, _pgid, tpgid, stat, command in rows:
        if pid == tpgid != 0:
            members = [(p, c) for p, g, _t, _s, c in rows if g == tpgid and p != pid]
            out["fg"] = {"leader": pid, "stat": stat, "command": command, "members": members}
            break
    return out


def read_terminal(tty):
    """parse_terminal for a tty, or None when it cannot be read."""
    cmd = shlex.split(os.environ.get("FLEETWATCH_FG_CMD", "ps -o pid=,pgid=,tpgid=,stat=,comm= -t"))
    try:
        out = subprocess.run(cmd + [tty[len("/dev/"):]], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    return parse_terminal(out.stdout) if out.returncode == 0 else None


def _names(command):
    """The program names a ps command field may stand for: its basename, and the basename of its first
    word when ps shows arguments after the program."""
    command = command.strip()
    names = {os.path.basename(command)}
    if command.split():
        names.add(os.path.basename(command.split()[0]))
    return {n for n in names if n}


def _editors():
    names = set(INTERACTIVE)
    for var in ("VISUAL", "EDITOR"):
        try:
            words = shlex.split(os.environ.get(var) or "")
        except ValueError:
            continue
        if words:
            names.add(os.path.basename(words[0]))
    return names


def interactive_member(members):
    """The shown name of the first group member that reads the terminal (an editor, a pager, a login
    shell), else None."""
    editors = _editors()
    for _pid, command in members:
        bare = command.strip()
        if bare.startswith("-"):
            return data.clean(bare.lstrip("-").split()[0]) if bare.lstrip("-").split() else "a shell"
        hit = _names(bare) & editors
        if hit:
            return data.clean(sorted(hit)[0])
    return None


def terminal_problem(name, pid, tty):
    """None when the session's own Claude Code holds the terminal and nothing it started reads it."""
    term = read_terminal(tty)
    if term is None or pid not in term["pids"]:
        return "%s is no longer on that terminal" % name
    fg = term["fg"]
    if fg is None:
        return "nothing is running in the foreground of %s's terminal" % name
    if os.path.basename(fg["command"]) != CLAUDE_COMM:
        shown = data.clean(os.path.basename(fg["command"])).lstrip("-")
        return "%s's terminal is running %s, not Claude Code" % (name, shown or "something else")
    if fg["leader"] != pid:
        return "%s's terminal is running another Claude Code, not this session" % name
    if "T" in fg["stat"]:
        return "%s's Claude Code is stopped" % name
    if "Z" in fg["stat"]:
        return "%s's Claude Code has exited" % name
    program = interactive_member(fg["members"])
    if program:
        return "%s is in %s; finish there first" % (name, program)
    return None


def _agent_state(paths, pid, sid):
    """The `claude agents` state of the session, the last matching row winning as in data.build_records."""
    state = None
    for a in data.read_agents(paths["command"], []) or []:
        if (a.get("sessionId") == sid) if sid else (not a.get("sessionId") and a.get("pid") == pid):
            state = a.get("state") or a.get("status")
    return state


def fresh_state(paths, pid, sid):
    """The session's prompt state now, or None when no registry row is the session's.

    The precedence is data.build_records': the registry status of the session's row (the last one when
    several carry its ID), else its `claude agents` state, else "unknown". Another registry row on the
    same pid that is at a prompt wins, since its prompt would be on the same terminal."""
    rows = data.read_registry(paths["sessions"])
    mine = [r for r in rows if ((r["sessionId"] == sid) if sid else (not r["sessionId"] and r["pid"] == pid))]
    if not mine or mine[-1]["pid"] != pid:
        return None
    for r in rows:
        if r["pid"] == pid and r["status"] in data.NEEDS_STATES:
            return r["status"]
    return mine[-1]["status"] or _agent_state(paths, pid, sid) or "unknown"


def state_problem(name, paths, pid, sid):
    state = fresh_state(paths, pid, sid)
    if state is None:
        return "%s is no longer registered as running" % name
    if state in data.NEEDS_STATES:
        return "%s is at a prompt; answer it in its tab" % name
    if state not in SAFE_STATES:
        return "cannot tell whether %s is at a prompt (state %s); check its tab" % (name, data.clean(state)[:20])
    return None


def deliver(name, pid, sid, tty, text, paths):
    """(ok, reason). Types text into the session's tab and then sends the return, each only when the
    session's own Claude Code holds that terminal and is not at a prompt. The prompt state is read last,
    just before each write. When the check between the two writes fails, the text stays typed in the box
    and the reason starts with TYPED."""
    name = data.clean(name)
    problem = check_text(text)
    if problem:
        return False, problem
    if not iterm.valid_tty(tty):
        return False, "not a terminal device"
    problem = terminal_problem(name, pid, tty) or state_problem(name, paths, pid, sid)
    if problem:
        return False, problem
    ok, reason = iterm.type_into_tty(tty, text)
    if not ok:
        return False, reason
    time.sleep(SETTLE_S)
    problem = terminal_problem(name, pid, tty) or state_problem(name, paths, pid, sid)
    if not problem:
        ok, reason = iterm.submit_tty(tty)
        if ok:
            return True, ""
        problem = reason
    return False, "%s: %s; check its tab" % (TYPED, problem)
