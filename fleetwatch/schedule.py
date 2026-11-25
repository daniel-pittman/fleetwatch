"""`fleetwatch index schedule`, `fleetwatch watch schedule` and `fleetwatch archive`: install|remove|status for
fleetwatch's timed background jobs. The index job runs `fleetwatch index refresh --all` every 30 minutes;
the watch job runs `fleetwatch watch run` every 15 minutes; the archive job runs `fleetwatch archive run`
every hour. All run niced. The index and archive jobs are also macOS background jobs with low-priority
I/O; the watch job is not, because macOS throttles a background job's network calls and every watch
lookup is one.

macOS: a launchd agent in ~/Library/LaunchAgents (local.fleetwatch.index-refresh, local.fleetwatch.watch-run,
local.fleetwatch.archive).
install writes the plist and prints the launchctl command; it never loads it. Elsewhere: a systemd user timer
pair when ~/.config/systemd/user can be written, and the equivalent cron line is printed. Logs go to the
fleetwatch state folder.
"""
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import time
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
ARCHIVE = Job("local.fleetwatch.archive", "fleetwatch-archive", ("archive", "run"), 3600,
              "fleetwatch archive run", "fleetwatch archive")
LABEL = INDEX.label
INTERVAL_S = INDEX.interval_s
# Carried into the job's environment when set at install time: where fleetwatch's folders are, for the
# watch job which gh to run and where its login lives, and for the archive job where the archive is.
PASSED = ("FLEETWATCH_CONFIG_DIR", "XDG_DATA_HOME", "XDG_STATE_HOME", "FLEETWATCH_PROJECTS_DIR", "PYTHONPATH",
          "FLEETWATCH_STATUS_DIR", "FLEETWATCH_SESSIONS_DIR", "FLEETWATCH_GH_CMD", "GH_CONFIG_DIR",
          "FLEETWATCH_ARCHIVE_DIR", "CLAUDE_SESSION_ARCHIVE_DEST")


def launch_agents_dir(environ=None):
    environ = os.environ if environ is None else environ
    return Path(environ.get("FLEETWATCH_LAUNCH_AGENTS_DIR")
                or os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents"))


def systemd_dir(environ=None):
    environ = os.environ if environ is None else environ
    return Path(environ.get("FLEETWATCH_SYSTEMD_USER_DIR")
                or os.path.join(os.path.expanduser("~"), ".config", "systemd", "user"))


# ---------------------------------------------------------------- launchd: loading and retiring jobs

LAUNCHCTL_TIMEOUT_S = 30


TRIAL_REFUSED = 126


def launchctl(*args):
    """Run launchctl (FLEETWATCH_LAUNCHCTL names a stand-in) and return the CompletedProcess. A launchctl
    that cannot be started reads as a failed call, exit 127. With a trial LaunchAgents folder
    (FLEETWATCH_LAUNCH_AGENTS_DIR) and no stand-in, the real launchctl is never run, exit 126: it would
    load a trial plist into the live login session or unload a real agent."""
    stand_in = os.environ.get("FLEETWATCH_LAUNCHCTL")
    if not stand_in and os.environ.get("FLEETWATCH_LAUNCH_AGENTS_DIR"):
        return subprocess.CompletedProcess(["launchctl"] + list(args), TRIAL_REFUSED, "",
                                           "not run (a trial folder is set: FLEETWATCH_LAUNCH_AGENTS_DIR)")
    argv = shlex.split(stand_in or "launchctl") + list(args)
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=LAUNCHCTL_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


def gui_domain():
    return "gui/%d" % os.getuid()


def is_loaded(label):
    """True when launchd has the agent loaded in this user's GUI domain."""
    return launchctl("print", "%s/%s" % (gui_domain(), label)).returncode == 0


def _first_line(proc):
    text = (proc.stderr or proc.stdout or "").strip()
    return text.splitlines()[0] if text else "exit %d" % proc.returncode


def load(out=None, job=INDEX):
    """Load the job's installed plist, replacing a loaded copy so a rewritten plist takes effect.
    Returns True when the job is loaded afterward. macOS only."""
    out = out or sys.stdout
    path = _plist_path(job)
    if is_loaded(job.label):
        launchctl("bootout", "%s/%s" % (gui_domain(), job.label))
    proc = launchctl("bootstrap", gui_domain(), str(path))
    if proc.returncode != 0 or not is_loaded(job.label):
        print("could not load %s: %s" % (job.label, _first_line(proc)), file=out)
        return False
    print("loaded %s" % job.label, file=out)
    return True


def legacy_backup_dir():
    """Where a retired legacy agent's plist is kept: fleetwatch's state folder, never deleted."""
    return Path(settings.state_dir()) / "legacy"


def _backup_path(label):
    """A backup name that is not taken, so an earlier backup is never overwritten."""
    folder = legacy_backup_dir()
    path = folder / (label + ".plist")
    if not path.exists():
        return path
    stamp = time.strftime("%Y%m%d-%H%M%S")
    n = 0
    while True:
        path = folder / ("%s.%s%s.plist" % (label, stamp, "" if n == 0 else "-%d" % n))
        if not path.exists():
            return path
        n += 1


def retire_legacy(label, out=None, dry_run=False):
    """Unload a legacy launchd agent and move its plist into legacy_backup_dir(), keeping it.

    Returns (ok, actions): the actions taken (or, with dry_run, that would be taken), each one line,
    and False when an unload failed. A plist whose agent could not be unloaded stays where it is, so
    nothing is left loaded without its file. Nothing happens off macOS, where there are no LaunchAgents.
    """
    out = out or sys.stdout
    if sys.platform != "darwin":
        return True, []
    plist = launch_agents_dir() / (label + ".plist")
    actions = []
    if is_loaded(label):
        actions.append("unload the legacy agent %s" % label)
        if not dry_run:
            proc = launchctl("bootout", "%s/%s" % (gui_domain(), label))
            if proc.returncode != 0 and is_loaded(label):
                print("could not unload the legacy agent %s: %s" % (label, _first_line(proc)), file=out)
                return False, actions[:-1]
            print("unloaded the legacy agent %s" % label, file=out)
    if plist.exists():
        backup = _backup_path(label)
        actions.append("move %s to %s" % (plist, backup))
        if not dry_run:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(plist), str(backup))
            print("moved its plist %s to %s (kept as a backup)" % (plist, backup), file=out)
    if dry_run:
        for a in actions:
            print("would " + a, file=out)
    return True, actions


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


def install(out=None, job=INDEX, hint=True):
    """Write the job's plist or systemd units; nothing is loaded. hint=False leaves out the line saying
    how to start it, for a caller that loads the job itself."""
    out = out or sys.stdout
    log_dir = Path(settings.state_dir())
    if sys.platform == "darwin":
        log_dir.mkdir(parents=True, exist_ok=True)
        path = _plist_path(job)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(plist_dict(log_dir, job)))
        print("wrote %s%s" % (path, " (not loaded)" if hint else ""), file=out)
        if hint:
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


def remove(out=None, job=INDEX, hint=True):
    out = out or sys.stdout
    targets = [_plist_path(job)] if sys.platform == "darwin" else _units(job)
    for p in targets:
        if p.exists():
            p.unlink()
            print("removed %s" % p, file=out)
        else:
            print("not installed: %s" % p, file=out)
    if hint and sys.platform == "darwin":
        print("if it was loaded: launchctl bootout gui/$(id -u)/%s" % job.label, file=out)
    elif hint:
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
