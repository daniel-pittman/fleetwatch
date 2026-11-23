"""`fleetwatch iterm install | status | uninstall`: the iTerm2 hotkey overlay as an installed Dynamic Profile."""
import json
import os
import shlex
import shutil
import sys

from . import config

GUID = "6F1EE7A0-5B1D-4C8E-9A52-0F1E7D3C4B01"
FILE = "fleetwatch.json"
# The file an earlier release wrote; it carries the same GUID, so a reinstall must not leave both.
PREDECESSOR = "fleet.json"
NAME = "fleetwatch"
ITERM_APPS = ("/Applications/iTerm.app", "~/Applications/iTerm.app")
NOT_MAC = "The fleetwatch overlay needs iTerm2 on macOS; nothing to do on this system."


def profiles_dir():
    return os.environ.get("FLEETWATCH_ITERM_PROFILES_DIR") or os.path.join(
        os.path.expanduser("~"), "Library", "Application Support", "iTerm2", "DynamicProfiles")


def is_macos():
    return sys.platform == "darwin"


def iterm_installed():
    return any(os.path.isdir(os.path.expanduser(p)) for p in ITERM_APPS)


def self_argv():
    """The command that runs this fleetwatch: the installed entry point by absolute path, else the module."""
    entry = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if os.path.basename(entry) == "fleetwatch" and os.access(entry, os.X_OK):
        return [entry]
    found = shutil.which("fleetwatch")
    # -P: the working directory never shadows a module (see bin/fleetwatch).
    return [os.path.abspath(found)] if found else [sys.executable, "-P", "-m", "fleetwatch"]


def command(shell=None, argv=None):
    """The user's login shell running fleetwatch interactively, so it reads the PATH the user's rc files set."""
    shell = shell or os.environ.get("SHELL") or "/bin/zsh"
    return shlex.join([shell, "-ilc", shlex.join(argv or self_argv())])


def profile(cmd):
    return {
        "Name": NAME, "Guid": GUID, "Tags": ["fleetwatch"], "Rewritable": True,
        "Dynamic Profile Parent Name": "Default", "Custom Command": "Yes", "Command": cmd,
        "Window Type": 2, "Screen": -2, "Space": -1, "Columns": 120, "Rows": 40,
        "Has Hotkey": True, "HotKey Key Code": 3, "HotKey Modifier Flags": 393216,
        "HotKey Characters": "\u0006", "HotKey Characters Ignoring Modifiers": "F",
        "HotKey Window AutoHides": True, "HotKey Window Animates": False,
        "HotKey Window Dock Click Action": 1,
        # An empty trigger list replaces the triggers the profile would inherit from Default.
        # iTerm2's Claude Code integration adds triggers there that show its agent toolbar
        # by the session's running program; fleetwatch's refresh starts short helper
        # processes, so the toolbar opened and closed every few seconds over the board.
        "Triggers": [],
    }


def _read(path):
    """The Profiles list of a profile file, or None when it is missing or not a profile file."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    profs = doc.get("Profiles") if isinstance(doc, dict) else None
    return profs if isinstance(profs, list) else None


def _ours(profs):
    return any(isinstance(p, dict) and p.get("Guid") == GUID for p in profs or [])


def _drop_ours(path):
    """Remove our profile from path: the file when nothing else is in it. True when something was removed."""
    profs = _read(path)
    if not _ours(profs):
        return False
    rest = [p for p in profs if not (isinstance(p, dict) and p.get("Guid") == GUID)]
    if rest:
        config.atomic_write(path, json.dumps({"Profiles": rest}, indent=2) + "\n")
    else:
        os.unlink(path)
    return True


def install(out=print):
    if not is_macos():
        out(NOT_MAC)
        return 0
    folder = profiles_dir()
    target = os.path.join(folder, FILE)
    existing = _read(target)
    if os.path.exists(target) and not _ours(existing):
        out("%s exists and is not the fleetwatch profile; leaving it alone." % target)
        return 1
    cmd = command()
    config.atomic_write(target, json.dumps({"Profiles": [profile(cmd)]}, indent=2) + "\n")
    if _drop_ours(os.path.join(folder, PREDECESSOR)):
        out("Removed the older %s profile (same GUID)." % PREDECESSOR)
    out("Installed the fleetwatch hotkey profile: %s" % target)
    out("Command: %s" % cmd)
    out("Press Control-Shift-F from anywhere; iTerm2 loads the profile within a second or two.")
    return 0


def uninstall(out=print):
    if not is_macos():
        out(NOT_MAC)
        return 0
    folder = profiles_dir()
    gone = [f for f in (FILE, PREDECESSOR) if _drop_ours(os.path.join(folder, f))]
    out("Removed %s." % ", ".join(gone) if gone else "The fleetwatch hotkey profile is not installed.")
    return 0


def status(out=print):
    if not is_macos():
        out(NOT_MAC)
        return 0
    target = os.path.join(profiles_dir(), FILE)
    profs = _read(target)
    ours = [p for p in profs or [] if isinstance(p, dict) and p.get("Guid") == GUID]
    out("iTerm2: %s" % ("installed" if iterm_installed() else "not found"))
    if ours:
        out("Hotkey profile: installed (%s)" % target)
        out("Command: %s" % ours[0].get("Command", ""))
    else:
        out("Hotkey profile: not installed (run: fleetwatch iterm install)")
    return 0


def main(argv):
    actions = {"install": install, "status": status, "uninstall": uninstall}
    if len(argv) != 1 or argv[0] not in actions:
        print("usage: fleetwatch iterm install | status | uninstall", file=sys.stderr)
        return 2
    return actions[argv[0]]()
