"""`fleetwatch index schedule install|remove|status`: run `fleetwatch index refresh --all` every
30 minutes at low priority.

macOS: a launchd agent, local.fleetwatch.index-refresh, in ~/Library/LaunchAgents. install
writes the plist and prints the launchctl command; it never loads it. Elsewhere: a systemd
user timer pair when ~/.config/systemd/user can be written, and the equivalent cron line is
printed. Logs go to the fleetwatch state folder.
"""
import os
import plistlib
import shlex
import sys
from pathlib import Path

from . import settings

LABEL = "local.fleetwatch.index-refresh"
INTERVAL_S = 1800


def launch_agents_dir(environ=None):
    environ = os.environ if environ is None else environ
    return Path(environ.get("FLEETWATCH_LAUNCH_AGENTS_DIR")
                or os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents"))


def systemd_dir(environ=None):
    environ = os.environ if environ is None else environ
    return Path(environ.get("FLEETWATCH_SYSTEMD_USER_DIR")
                or os.path.join(os.path.expanduser("~"), ".config", "systemd", "user"))


def command():
    """The refresh command line: this Python running the package, so no launcher is needed."""
    return [sys.executable, "-P", "-m", "fleetwatch", "index", "refresh", "--all"]


def plist_dict(log_dir):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")}
    for key in ("FLEETWATCH_CONFIG_DIR", "XDG_DATA_HOME", "XDG_STATE_HOME", "FLEETWATCH_PROJECTS_DIR", "PYTHONPATH"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return {
        "Label": LABEL,
        "ProgramArguments": command(),
        "StartInterval": INTERVAL_S,
        "RunAtLoad": False,
        "ProcessType": "Background",
        "Nice": 10,
        "LowPriorityIO": True,
        "StandardOutPath": str(Path(log_dir) / "index-refresh.log"),
        "StandardErrorPath": str(Path(log_dir) / "index-refresh.err"),
        "EnvironmentVariables": env,
    }


def _plist_path():
    return launch_agents_dir() / (LABEL + ".plist")


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
    cmd = " ".join(shlex.quote(a) for a in command())
    d = systemd_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / "fleetwatch-index-refresh.service").write_text(
            "[Unit]\nDescription=fleetwatch index refresh\n\n[Service]\nType=oneshot\n"
            "Nice=10\nIOSchedulingClass=idle\nExecStart=%s\n" % cmd)
        (d / "fleetwatch-index-refresh.timer").write_text(
            "[Unit]\nDescription=fleetwatch index refresh every 30 minutes\n\n[Timer]\n"
            "OnBootSec=5min\nOnUnitActiveSec=30min\n\n[Install]\nWantedBy=timers.target\n")
        print("wrote the timer in %s (not enabled)" % d, file=out)
        print("to start it: systemctl --user enable --now fleetwatch-index-refresh.timer", file=out)
    except OSError as e:
        print("could not write a systemd timer (%s)" % e, file=out)
    print("or use cron: */30 * * * * nice %s >> %s 2>&1"
          % (cmd, shlex.quote(str(log_dir / "index-refresh.log"))), file=out)
    return 0


def remove(out=None):
    out = out or sys.stdout
    targets = ([_plist_path()] if sys.platform == "darwin"
               else [systemd_dir() / "fleetwatch-index-refresh.service",
                     systemd_dir() / "fleetwatch-index-refresh.timer"])
    for p in targets:
        if p.exists():
            p.unlink()
            print("removed %s" % p, file=out)
        else:
            print("not installed: %s" % p, file=out)
    if sys.platform == "darwin":
        print("if it was loaded: launchctl bootout gui/$(id -u)/%s" % LABEL, file=out)
    else:
        print("if it was enabled: systemctl --user disable --now fleetwatch-index-refresh.timer",
              file=out)
    return 0


def status(out=None):
    out = out or sys.stdout
    if sys.platform == "darwin":
        p = _plist_path()
        print("%s: %s" % (p, "installed" if p.exists() else "not installed"), file=out)
        print("check whether it is loaded: launchctl print gui/$(id -u)/%s" % LABEL, file=out)
    else:
        for n in ("fleetwatch-index-refresh.service", "fleetwatch-index-refresh.timer"):
            p = systemd_dir() / n
            print("%s: %s" % (p, "installed" if p.exists() else "not installed"), file=out)
    return 0


def main(argv, out=None):
    verb = argv[0] if argv else ""
    if verb == "install":
        return install(out)
    if verb == "remove":
        return remove(out)
    if verb == "status":
        return status(out)
    print("usage: fleetwatch index schedule install|remove|status", file=sys.stderr)
    return 2
