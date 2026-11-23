"""Type a message into a running session's Claude Code prompt from the board.

The text reaches Claude as input typed at its own terminal, so it carries the owner's authority; a
message between sessions never does. That makes the target the whole safety question: text typed into
a shell runs as a command, and text typed into a permission prompt answers it. Every check here runs at
send time against fresh reads (the registry, the pid to tty map, the tty's foreground process), never
against the last board refresh. fleetwatch never stores or logs the message.
"""
import os
import shlex
import subprocess
import unicodedata

from . import data, iterm

MAX_LEN = 2000
CLAUDE_COMM = "claude"

# Cc covers newline, tab, escape (which could end a bracketed paste) and DEL; Zl and Zp are the Unicode
# line and paragraph separators, which break a line the way a newline does.
_BREAKS = {"Cc", "Zl", "Zp"}


def check_text(text):
    """None when text may be sent, else the reason it may not."""
    if not text.strip():
        return "type a message first"
    if has_break(text):
        return "one line only: no newline, tab or other control character"
    if len(text) > MAX_LEN:
        return "too long: %d of %d characters" % (len(text), MAX_LEN)
    return None


def has_break(text):
    return any(unicodedata.category(c) in _BREAKS for c in text)


def parse_foreground(text):
    """(pid, command) of a tty's foreground process group leader from `ps -o pid=,pgid=,tpgid=,stat=,comm=`
    rows, or None when the terminal has no foreground group. The command may hold spaces."""
    for line in text.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[0].isdigit() and parts[2].isdigit() and parts[0] == parts[2] != "0":
            return int(parts[0]), parts[4].strip()
    return None


def read_foreground(tty):
    """(pid, command) of what holds the terminal's foreground, or None when it cannot be read."""
    cmd = shlex.split(os.environ.get("FLEETWATCH_FG_CMD", "ps -o pid=,pgid=,tpgid=,stat=,comm= -t"))
    try:
        out = subprocess.run(cmd + [tty[len("/dev/"):]], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    return parse_foreground(out.stdout) if out.returncode == 0 else None


def fresh_status(sessions_dir, pid, sid):
    """The registry status of the live session with this pid (and session ID, when known), or None."""
    for row in data.read_registry(sessions_dir):
        if row["pid"] == pid and (not sid or row["sessionId"] == sid):
            return row["status"] or "unknown"
    return None


def deliver(name, pid, sid, tty, text, sessions_dir):
    """(ok, reason). Sends text and a return to the session's tab when, and only when, the session is
    still the one on that terminal, is not at a prompt, and Claude Code is what the terminal is running."""
    name = data.clean(name)
    problem = check_text(text)
    if problem:
        return False, problem
    status = fresh_status(sessions_dir, pid, sid)
    if status is None:
        return False, "%s is no longer registered as running" % name
    if status in data.NEEDS_STATES:
        return False, "%s is at a prompt; answer it in its tab" % name
    if iterm.read_ttys().get(pid) != tty:
        return False, "%s is no longer on that terminal" % name
    fg = read_foreground(tty)
    if fg is None:
        return False, "nothing is running in the foreground of %s's terminal" % name
    leader, command = fg
    if os.path.basename(command) != CLAUDE_COMM:
        shown = data.clean(os.path.basename(command)).lstrip("-")
        return False, "%s's terminal is running %s, not Claude Code" % (name, shown or "something else")
    if leader != pid:
        return False, "%s's terminal is running another Claude Code, not this session" % name
    return iterm.send_to_tty(tty, text)
