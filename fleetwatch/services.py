"""The service manager behind `fleetwatch install`, `uninstall` and `doctor`: launchd on macOS, systemd user
units elsewhere. Every call goes through a Manager, so tests and trial installs hand it a fake.

The command is `launchctl` (or `systemctl --user`), replaced by FLEETWATCH_LAUNCHCTL (or
FLEETWATCH_SYSTEMCTL), split like a shell command line. When FLEETWATCH_LAUNCH_AGENTS_DIR (or
FLEETWATCH_SYSTEMD_USER_DIR) points the files at another folder and no replacement command is set, the
manager never runs the real one: it would load a trial file into the live session. It reports each step as
"not run" instead.
"""
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass

from . import schedule

TIMEOUT_S = 30


@dataclass(frozen=True)
class Outcome:
    ok: bool
    detail: str = ""        # the first line of what the command printed when it failed
    ran: bool = True        # False: the step was only described (a dry run or a trial folder)


class Manager:
    """One service manager. `loaded(label)` is True, False, or None when it cannot be told."""

    def __init__(self, platform=None, runner=subprocess.run, environ=None, uid=None):
        self.platform = platform or sys.platform
        self.runner = runner
        env = os.environ if environ is None else environ
        self.darwin = self.platform == "darwin"
        if self.darwin:
            override, folder = env.get("FLEETWATCH_LAUNCHCTL"), env.get("FLEETWATCH_LAUNCH_AGENTS_DIR")
            default = ["launchctl"]
        else:
            override, folder = env.get("FLEETWATCH_SYSTEMCTL"), env.get("FLEETWATCH_SYSTEMD_USER_DIR")
            default = ["systemctl", "--user"]
        self.argv = shlex.split(override) if override else default
        self.live = bool(override) or not folder
        self.uid = os.getuid() if uid is None else uid
        self.domain = "gui/%d" % self.uid
        self.available = self.live and (bool(override) or shutil.which(self.argv[0]) is not None)

    @property
    def name(self):
        return "launchd" if self.darwin else "systemd"

    def _run(self, args):
        if not self.available:
            return Outcome(False, "not run (%s)" % ("a trial folder is set" if not self.live
                                                     else "%s is not on the PATH" % self.argv[0]), ran=False)
        try:
            p = self.runner(self.argv + list(args), capture_output=True, text=True, timeout=TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as e:
            return Outcome(False, str(e).splitlines()[0] if str(e) else type(e).__name__)
        text = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
        return Outcome(p.returncode == 0, "" if p.returncode == 0 else (text.splitlines() or ["exit %d" % p.returncode])[0],
                       ran=True)

    def _query(self, args):
        """(returncode, stdout) for a read-only query, or (None, "") when it cannot run."""
        if not self.available:
            return None, ""
        try:
            p = self.runner(self.argv + list(args), capture_output=True, text=True, timeout=TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            return None, ""
        return p.returncode, p.stdout or ""

    # ---- launchd: a label and a plist; systemd: the unit that is enabled (a timer, or the service itself)

    def loaded(self, label):
        rc, _out = self._query(["print", "%s/%s" % (self.domain, label)] if self.darwin else ["is-active", label])
        return None if rc is None else rc == 0

    def running(self, label):
        """Whether a kept-alive job has a live process: launchd's `state = running`, systemd's active."""
        rc, out = self._query(["print", "%s/%s" % (self.domain, label)] if self.darwin else ["is-active", label])
        if rc is None:
            return None
        if not self.darwin:
            return rc == 0
        return rc == 0 and any(line.strip() == "state = running" for line in out.splitlines())

    def load(self, label, path):
        if self.darwin:
            return self._run(["bootstrap", self.domain, str(path)])
        reload = self._run(["daemon-reload"])
        return reload if not reload.ok else self._run(["enable", "--now", label])

    def reload(self, label, path):
        """Pick up a rewritten file for a job that is already loaded."""
        if self.darwin:
            out = self._run(["bootout", "%s/%s" % (self.domain, label)])
            return out if not out.ok else self._run(["bootstrap", self.domain, str(path)])
        out = self._run(["daemon-reload"])
        return out if not out.ok else self._run(["restart", label])

    def restart(self, label):
        """Restart a kept-alive job that is loaded but not running."""
        if self.darwin:
            return self._run(["kickstart", "-k", "%s/%s" % (self.domain, label)])
        return self._run(["restart", label])

    def unload(self, label):
        if self.darwin:
            return self._run(["bootout", "%s/%s" % (self.domain, label)])
        return self._run(["disable", "--now", label])

    def loaded_labels(self):
        """Every label launchd has loaded in this session (from `launchctl list`), or None when unknown."""
        if not self.darwin:
            return None
        rc, out = self._query(["list"])
        if rc != 0:
            return None
        labels = set()
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3:
                labels.add(parts[-1])
        return labels


def plist_args(path):
    """A plist's ProgramArguments (or [Program]), [] when it cannot be read."""
    try:
        with open(path, "rb") as fh:
            info = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return []
    if not isinstance(info, dict):
        return []
    args = info.get("ProgramArguments") or ([info["Program"]] if info.get("Program") else [])
    return [str(a) for a in args] if isinstance(args, list) else []


def signature(args):
    """What a job runs, with the interpreter that runs it left out, so two copies of the same command compare
    equal: the arguments after `-m fleetwatch`, else the script's file name and what follows it."""
    args = list(args)
    for i in range(len(args) - 1):
        if args[i] == "-m" and args[i + 1] == "fleetwatch":
            return ("fleetwatch",) + tuple(args[i + 2:])
    for i, a in enumerate(args):
        if a.endswith((".py", ".sh")):
            return (os.path.basename(a),) + tuple(args[i + 1:])
    if args:
        return (os.path.basename(args[0]),) + tuple(args[1:])
    return ()


def agent_plists():
    """{label: plist path} for every plist in the LaunchAgents folder fleetwatch writes to."""
    folder = schedule.launch_agents_dir()
    out = {}
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        if name.endswith(".plist"):
            out[name[:-len(".plist")]] = folder / name
    return out
