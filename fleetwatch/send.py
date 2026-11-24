"""Type a message into a running session's Claude Code prompt from the board.

The text reaches Claude as input typed at its own terminal, so it carries the owner's authority; a
message between sessions never does. That makes the target the whole safety question: text typed into
a shell runs as a command, text typed into an editor runs as editor commands, and text typed into a
permission prompt answers it. Every check here runs at send time against fresh reads (the tty's
processes and the registry), never against the last board refresh, and a check that cannot tell refuses.
fleetwatch never stores or logs the message.
"""
import os
import re
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

# What may share Claude Code's foreground process group besides Claude itself. A name qualifies only when
# Claude Code runs it there as a helper that talks over pipes and never reads the terminal, and only when it
# was seen doing so in live sessions: caffeinate (keeps the machine awake), the runtimes MCP servers run
# on, and a server named for MCP. Names are the executable's own (ps `ucomm`), which a process title cannot
# change. Python is matched in any case and version: a framework build on macOS names its executable
# `Python`, other builds `python3.11`. Anything else in the group refuses, so an editor opened with Ctrl-G
# refuses whatever it is named, unless it runs on one of these runtimes (README, Limits).
HELPERS = {"caffeinate", "node", "uv", "dart"}
_HELPER_RE = re.compile(r"(?i:python[0-9.]*)|.*mcp.*")
# A shell is never a helper, whatever its arguments: nothing in an argv proves that a shell, or the
# command it runs, will not read the terminal. These names only choose the wording of the refusal.
SHELLS = {"bash", "zsh", "sh", "dash", "ksh", "fish", "csh", "tcsh"}

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
    """What `ps -o pid=,pgid=,tpgid=,stat=,ucomm=,args= -t TTY` says about a terminal: {"pids": every pid
    on it, "fg": the foreground group or None, "bad": a line that did not parse}. The group is {"leader",
    "stat", "command", "members"}: command is the leader's argv, members are (pid, ucomm, argv) for the
    group's other processes. fg is None when the terminal has no foreground group or no process leads it.
    The argv may hold spaces."""
    rows, bad = [], False
    for line in text.splitlines():
        parts = line.split(None, 5)
        if len(parts) == 6 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3], parts[4], parts[5].strip()))
        elif line.strip():
            bad = True
    out = {"pids": {r[0] for r in rows}, "fg": None, "bad": bad}
    for pid, _pgid, tpgid, stat, _ucomm, args in rows:
        if pid == tpgid != 0:
            members = [(p, u, a) for p, g, _t, _s, u, a in rows if g == tpgid and p != pid]
            out["fg"] = {"leader": pid, "stat": stat, "command": args, "members": members}
            break
    return out


def read_terminal(tty):
    """parse_terminal for a tty, or None when it cannot be read."""
    cmd = shlex.split(os.environ.get("FLEETWATCH_FG_CMD", "ps -o pid=,pgid=,tpgid=,stat=,ucomm=,args= -t"))
    try:
        out = subprocess.run(cmd + [tty[len("/dev/"):]], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    return parse_terminal(out.stdout) if out.returncode == 0 else None


def _program(args):
    """The program name an argv stands for: the basename of its first word, without a login shell's -."""
    words = args.split()
    return os.path.basename(words[0]).lstrip("-") if words else ""


def foreign_member(members):
    """The executable name of the first group member that is not a known helper, else None."""
    for _pid, ucomm, args in members:
        if ucomm in HELPERS or _HELPER_RE.fullmatch(ucomm):
            continue
        return data.clean(ucomm or _program(args)) or "a program"
    return None


def terminal_problem(name, pid, tty):
    """None when the session's own Claude Code holds the terminal and nothing it started reads it."""
    term = read_terminal(tty)
    if term is None or pid not in term["pids"]:
        return "%s is no longer on that terminal" % name
    if term["bad"]:
        return "cannot read what is running on %s's terminal" % name
    fg = term["fg"]
    if fg is None:
        return "nothing is running in the foreground of %s's terminal" % name
    if _program(fg["command"]) != CLAUDE_COMM:
        shown = data.clean(_program(fg["command"]))
        return "%s's terminal is running %s, not Claude Code" % (name, shown or "something else")
    if fg["leader"] != pid:
        return "%s's terminal is running another Claude Code, not this session" % name
    if "T" in fg["stat"]:
        return "%s's Claude Code is stopped" % name
    if "Z" in fg["stat"]:
        return "%s's Claude Code has exited" % name
    program = foreign_member(fg["members"])
    if program in SHELLS:
        return "%s is running a command; try again when it finishes" % name
    if program:
        return "%s is running in %s; finish there first" % (program, name)
    return None


def fresh_state(paths, pid, sid):
    """The session's prompt state now: its registry status, "" when that is empty, or None when no
    registry row is the session's.

    The row is data.build_records': the last one carrying the session's ID, else the last ID-less row on
    its pid. Another registry row on the same pid that is at a prompt wins, since its prompt would be on
    the same terminal. An empty status is not filled in from `claude agents`: that read is slow and older
    than the terminal checks it would follow."""
    rows = data.read_registry(paths["sessions"])
    mine = [r for r in rows if ((r["sessionId"] == sid) if sid else (not r["sessionId"] and r["pid"] == pid))]
    if not mine or mine[-1]["pid"] != pid:
        return None
    for r in rows:
        if r["pid"] == pid and r["status"] in data.NEEDS_STATES:
            return r["status"]
    return mine[-1]["status"] or ""


def state_problem(name, paths, pid, sid):
    state = fresh_state(paths, pid, sid)
    if state is None:
        return "%s is no longer registered as running" % name
    if not state:
        return "cannot tell whether %s is at a prompt: its status is unknown; check its tab" % name
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
        if reason == iterm.TIMED_OUT:
            # The text may have landed before the stall; another Enter would type it a second time.
            return False, "%s: %s; check its tab" % (TYPED, reason)
        return False, reason
    time.sleep(SETTLE_S)
    problem = terminal_problem(name, pid, tty) or state_problem(name, paths, pid, sid)
    if not problem:
        ok, reason = iterm.submit_tty(tty)
        if ok:
            return True, ""
        problem = reason
    return False, "%s: %s; check its tab" % (TYPED, problem)
