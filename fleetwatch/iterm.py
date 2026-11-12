"""fleetwatch iTerm2 tab map and jump. The tty reaches AppleScript as argv only."""
import os
import re
import shlex
import subprocess

from .data import clean, trunc

TTY_RE = re.compile(r"/dev/ttys[0-9]+")

# The tty arrives as argv item 1, never in the script text. `is running` is checked
# outside any `tell` so that a closed iTerm2 is reported rather than launched. The list
# script reads every tty in one bulk Apple event; a per-session loop takes seconds.
ITERM_LIST_SCRIPT = """on run argv
    if application "iTerm2" is not running then error "iTerm2 is not running"
    tell application "iTerm2" to set ttyLists to tty of sessions of tabs of windows
    set AppleScript's text item delimiters to linefeed
    set out to ""
    repeat with perWindow in ttyLists
        repeat with perTab in perWindow
            set out to out & (perTab as text) & linefeed
        end repeat
    end repeat
    return out
end run
"""

ITERM_JUMP_SCRIPT = """on run argv
    set target to item 1 of argv
    if application "iTerm2" is not running then error "iTerm2 is not running"
    tell application "iTerm2"
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    if (tty of s) is target then
                        select w
                        select t
                        select s
                        activate
                        return "ok"
                    end if
                end repeat
            end repeat
        end repeat
    end tell
    error "no iTerm2 session on that terminal"
end run
"""

# The command arrives as argv item 1, never in the script text. It runs as the window's process rather
# than through `write text`: typed text races the shell's startup and any key pressed as the window
# takes focus, so one stray character turned `cd ... && claude` into a command that does not exist.
ITERM_OPEN_SCRIPT = """on run argv
    tell application "iTerm2"
        activate
        set w to (create window with default profile command (item 1 of argv))
        return id of w
    end tell
end run
"""


def valid_tty(value):
    return isinstance(value, str) and bool(TTY_RE.fullmatch(value))


def parse_tty_table(text):
    """pid -> /dev/ttysNNN from `ps -axo pid=,tty=` output; sessions without a tty are left out."""
    table = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and valid_tty("/dev/" + parts[1]):
            table[int(parts[0])] = "/dev/" + parts[1]
    return table


def read_ttys():
    try:
        out = subprocess.run(shlex.split(os.environ.get("FLEETWATCH_TTY_CMD", "ps -axo pid=,tty=")),
                             capture_output=True, text=True, timeout=10)
        return parse_tty_table(out.stdout) if out.returncode == 0 else {}
    except Exception:
        return {}


def osascript(script, args=()):
    """(ok, output or short reason). The script goes in on stdin and arguments as argv."""
    argv = shlex.split(os.environ.get("FLEETWATCH_OSASCRIPT", "osascript")) + ["-"] + list(args)
    try:
        out = subprocess.run(argv, input=script, capture_output=True, text=True, timeout=10)
    except Exception as exc:
        return False, clean(str(exc))[:80] or "osascript failed"
    if out.returncode != 0:
        lines = [ln for ln in clean(out.stderr).split("\n") if ln.strip()]
        reason = lines[-1].strip() if lines else "exit %d" % out.returncode
        return False, trunc(re.sub(r"^\d+:\d+: (execution error: )?", "", reason), 90, True)
    return True, out.stdout


def read_iterm():
    """(set of iTerm2 session ttys or None, reason when None)."""
    ok, out = osascript(ITERM_LIST_SCRIPT)
    if not ok:
        return None, out
    return {ln.strip() for ln in out.splitlines() if valid_tty(ln.strip())}, None


def jump_to_tty(tty):
    """(ok, reason). Raises nothing; an invalid tty never reaches osascript."""
    if not valid_tty(tty):
        return False, "not a terminal device"
    ok, out = osascript(ITERM_JUMP_SCRIPT, [tty])
    return (True, "") if ok else (False, out)



def open_window(command):
    """(ok, window id or reason). Opens one new iTerm2 window running command; it closes when that ends."""
    ok, out = osascript(ITERM_OPEN_SCRIPT, [command])
    return ok, out.strip()


def jump_context():
    ttys = read_ttys()
    iterm, err = read_iterm()
    return {"ttys": ttys, "iterm": iterm, "err": err}
