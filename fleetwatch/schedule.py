"""`fleetwatch index schedule` and `fleetwatch watch schedule`: install|remove|status for fleetwatch's two
background jobs. The index job runs `fleetwatch index refresh --all` every 30 minutes; the watch job runs
`fleetwatch watch run` every 15 minutes. Both run niced. The index job is also a macOS background job with
low-priority I/O; the watch job is not, because macOS throttles a background job's network calls and every
watch lookup is one.

macOS: a launchd agent in ~/Library/LaunchAgents (local.fleetwatch.index-refresh, local.fleetwatch.watch-run).
install writes the plist and prints the launchctl command; it never loads it. Elsewhere: a systemd user timer
pair when ~/.config/systemd/user can be written, and the equivalent cron line is printed. Logs go to the
fleetwatch state folder.
"""
import os
import plistlib
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from . import settings


@dataclass(frozen=True)
class Job:
    label: str          # the launchd label
    unit: str           # the systemd unit name and the log file name
    args: tuple         # after `python -m fleetwatch`
    interval_s: int
    what: str           # how the timer describes itself
    usage: str
    background: bool = True   # macOS ProcessType Background and low-priority I/O; idle I/O under systemd


INDEX = Job("local.fleetwatch.index-refresh", "fleetwatch-index-refresh", ("index", "refresh", "--all"), 1800,
            "fleetwatch index refresh", "fleetwatch index schedule")
WATCH = Job("local.fleetwatch.watch-run", "fleetwatch-watch-run", ("watch", "run"), 900,
            "fleetwatch watch run", "fleetwatch watch schedule", background=False)
LABEL = INDEX.label
INTERVAL_S = INDEX.interval_s
# Carried into the job's environment when set at install time: where fleetwatch's folders are, and for
# the watch job, which gh to run and where its login lives.
PASSED = ("FLEETWATCH_CONFIG_DIR", "XDG_DATA_HOME", "XDG_STATE_HOME", "FLEETWATCH_PROJECTS_DIR", "PYTHONPATH",
          "FLEETWATCH_STATUS_DIR", "FLEETWATCH_SESSIONS_DIR", "FLEETWATCH_GH_CMD", "GH_CONFIG_DIR")


def launch_agents_dir(environ=None):
    environ = os.environ if environ is None else environ
    return Path(environ.get("FLEETWATCH_LAUNCH_AGENTS_DIR")
                or os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents"))


def systemd_dir(environ=None):
    environ = os.environ if environ is None else environ
    return Path(environ.get("FLEETWATCH_SYSTEMD_USER_DIR")
                or os.path.join(os.path.expanduser("~"), ".config", "systemd", "user"))


def command(job=INDEX):
    """The job's command line: this Python running the package, so no launcher is needed."""
    return [sys.executable, "-P", "-m", "fleetwatch", *job.args]


def _passed_env():
    """The PASSED variables that are set, in PASSED order."""
    return [(k, os.environ[k]) for k in PASSED if os.environ.get(k)]


def _systemd_env_line(key, value):
    """One Environment= line. systemd takes the whole assignment in double quotes, with backslash and
    double quote escaped, and expands % specifiers, so a literal % is doubled."""
    escaped = (key + "=" + value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return 'Environment="%s"\n' % escaped


def _log_name(job):
    return job.unit[len("fleetwatch-"):]


def plist_dict(log_dir, job=INDEX):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")}
    env.update(_passed_env())
    throttle = {"ProcessType": "Background", "LowPriorityIO": True} if job.background else {
        "ProcessType": "Standard"}
    return {
        "Label": job.label,
        "ProgramArguments": command(job),
        "StartInterval": job.interval_s,
        "RunAtLoad": False,
        **throttle,
        "Nice": 10,
        "StandardOutPath": str(Path(log_dir) / (_log_name(job) + ".log")),
        "StandardErrorPath": str(Path(log_dir) / (_log_name(job) + ".err")),
        "EnvironmentVariables": env,
    }


def _plist_path(job=INDEX):
    return launch_agents_dir() / (job.label + ".plist")


def _units(job):
    return [systemd_dir() / (job.unit + ".service"), systemd_dir() / (job.unit + ".timer")]


def install(out=None, job=INDEX):
    out = out or sys.stdout
    log_dir = Path(settings.state_dir())
    if sys.platform == "darwin":
        log_dir.mkdir(parents=True, exist_ok=True)
        path = _plist_path(job)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(plist_dict(log_dir, job)))
        print("wrote %s (not loaded)" % path, file=out)
        print("to start it: launchctl bootstrap gui/$(id -u) %s" % shlex.quote(str(path)), file=out)
        return 0
    cmd = " ".join(shlex.quote(a) for a in command(job))
    environment = _passed_env()
    # cron treats an unescaped % in a command line as a newline.
    cron_env = "".join("%s=%s " % (k, shlex.quote(v).replace("%", "\\%")) for k, v in environment)
    minutes = job.interval_s // 60
    service, timer = _units(job)
    try:
        service.parent.mkdir(parents=True, exist_ok=True)
        service.write_text(
            "[Unit]\nDescription=%s\n\n[Service]\nType=oneshot\n"
            "Nice=10\n%s%sExecStart=%s\n"
            % (job.what, "IOSchedulingClass=idle\n" if job.background else "",
               "".join(_systemd_env_line(k, v) for k, v in environment), cmd))
        timer.write_text(
            "[Unit]\nDescription=%s every %d minutes\n\n[Timer]\n"
            "OnBootSec=5min\nOnUnitActiveSec=%dmin\n\n[Install]\nWantedBy=timers.target\n"
            % (job.what, minutes, minutes))
        print("wrote the timer in %s (not enabled)" % service.parent, file=out)
        print("to start it: systemctl --user enable --now %s" % timer.name, file=out)
    except OSError as e:
        print("could not write a systemd timer (%s)" % e, file=out)
    print("or use cron: */%d * * * * %snice %s >> %s 2>&1"
          % (minutes, cron_env, cmd, shlex.quote(str(log_dir / (_log_name(job) + ".log")))), file=out)
    return 0


def remove(out=None, job=INDEX):
    out = out or sys.stdout
    targets = [_plist_path(job)] if sys.platform == "darwin" else _units(job)
    for p in targets:
        if p.exists():
            p.unlink()
            print("removed %s" % p, file=out)
        else:
            print("not installed: %s" % p, file=out)
    if sys.platform == "darwin":
        print("if it was loaded: launchctl bootout gui/$(id -u)/%s" % job.label, file=out)
    else:
        print("if it was enabled: systemctl --user disable --now %s.timer" % job.unit, file=out)
    return 0


def status(out=None, job=INDEX):
    out = out or sys.stdout
    if sys.platform == "darwin":
        p = _plist_path(job)
        print("%s: %s" % (p, "installed" if p.exists() else "not installed"), file=out)
        print("check whether it is loaded: launchctl print gui/$(id -u)/%s" % job.label, file=out)
    else:
        for p in _units(job):
            print("%s: %s" % (p, "installed" if p.exists() else "not installed"), file=out)
    return 0


def main(argv, out=None, job=INDEX):
    verb = argv[0] if argv else ""
    if verb == "install":
        return install(out, job)
    if verb == "remove":
        return remove(out, job)
    if verb == "status":
        return status(out, job)
    print("usage: %s install|remove|status" % job.usage, file=sys.stderr)
    return 2
