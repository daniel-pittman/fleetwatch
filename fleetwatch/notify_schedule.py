"""`fleetwatch notify schedule install|remove|status`: keep `fleetwatch notify` running.

macOS: a launchd agent, local.fleetwatch.notify, in ~/Library/LaunchAgents, restarted when it
crashes. Elsewhere: a systemd user service. install writes the file and prints the start
command; it never starts anything. Logs go to the fleetwatch state folder.
"""
import os
import plistlib
import shlex
import sys
from pathlib import Path

from . import schedule, settings

LABEL = "local.fleetwatch.notify"
SERVICE = "fleetwatch-notify.service"


def command():
    return [sys.executable, "-P", "-m", "fleetwatch", "notify"]


def plist_dict(log_dir):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")}
    for key in ("FLEETWATCH_CONFIG_DIR", "XDG_DATA_HOME", "XDG_STATE_HOME", "FLEETWATCH_PROJECTS_DIR", "PYTHONPATH"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return {
        "Label": LABEL,
        "ProgramArguments": command(),
        # Restart after a crash or a failed start, not after a clean stop (a bootout or kill).
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "Nice": 5,
        "StandardOutPath": str(Path(log_dir) / "notify.log"),
        "StandardErrorPath": str(Path(log_dir) / "notify.err"),
        "EnvironmentVariables": env,
    }


def _plist_path():
    return schedule.launch_agents_dir() / (LABEL + ".plist")


def _unit_path():
    return schedule.systemd_dir() / SERVICE


def install(out=None):
    out = out or sys.stdout
    log_dir = Path(settings.state_dir())
    if sys.platform == "darwin":
        log_dir.mkdir(parents=True, exist_ok=True)
        path = _plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(plist_dict(log_dir)))
        print("wrote %s (not loaded)" % path, file=out)
        print("to start it: launchctl bootstrap gui/$(id -u) %s" % shlex.quote(str(path)), file=out)
        return 0
    path = _unit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[Unit]\nDescription=fleetwatch desktop notifications\n\n[Service]\nExecStart=%s\n"
            "Restart=on-failure\nRestartSec=30\nNice=5\n\n[Install]\nWantedBy=default.target\n"
            % " ".join(shlex.quote(a) for a in command()))
    except OSError as e:
        print("could not write a systemd service (%s)" % e, file=out)
        return 1
    print("wrote %s (not enabled)" % path, file=out)
    print("to start it: systemctl --user enable --now %s" % SERVICE, file=out)
    return 0


def remove(out=None):
    out = out or sys.stdout
    path = _plist_path() if sys.platform == "darwin" else _unit_path()
    if path.exists():
        path.unlink()
        print("removed %s" % path, file=out)
    else:
        print("not installed: %s" % path, file=out)
    if sys.platform == "darwin":
        print("if it was loaded: launchctl bootout gui/$(id -u)/%s" % LABEL, file=out)
    else:
        print("if it was enabled: systemctl --user disable --now %s" % SERVICE, file=out)
    return 0


def status(out=None):
    out = out or sys.stdout
    path = _plist_path() if sys.platform == "darwin" else _unit_path()
    print("%s: %s" % (path, "installed" if path.exists() else "not installed"), file=out)
    if sys.platform == "darwin":
        print("check whether it is loaded: launchctl print gui/$(id -u)/%s" % LABEL, file=out)
    else:
        print("check whether it is running: systemctl --user status %s" % SERVICE, file=out)
    return 0


def main(argv, out=None):
    verb = argv[0] if argv else ""
    if verb == "install":
        return install(out)
    if verb == "remove":
        return remove(out)
    if verb == "status":
        return status(out)
    print("usage: fleetwatch notify schedule install|remove|status", file=sys.stderr)
    return 2
