"""The service manager behind `fleetwatch install`, `uninstall` and `doctor`: launchd on macOS, systemd user
units elsewhere.

launchd is reached only through schedule.py's shared helpers (schedule.launchctl, is_loaded, load,
retire_legacy), which run FLEETWATCH_LAUNCHCTL when it is set. systemd runs `systemctl --user`, or
FLEETWATCH_SYSTEMCTL, through the Manager's runner. When FLEETWATCH_LAUNCH_AGENTS_DIR (or
FLEETWATCH_SYSTEMD_USER_DIR) points the files at another folder and no replacement command is set, the
Manager runs neither: it would load a trial file into the live session. Each step is reported as not run.
"""
import io
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from types import SimpleNamespace

from . import schedule

TIMEOUT_S = 30


@dataclass(frozen=True)
class Outcome:
    ok: bool
    detail: str = ""        # the first line of what the command printed when it failed
    ran: bool = True        # False: the step was only described (a trial folder, or no service manager)


def _first(text, fallback):
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    return lines[0] if lines else fallback


class Manager:
    """One service manager. `loaded(label)` is True, False, or None when it cannot be told."""

    def __init__(self, platform=None, runner=subprocess.run, environ=None):
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
        self.available = self.live and (bool(override) or shutil.which(self.argv[0]) is not None)

    @property
    def name(self):
        return "launchd" if self.darwin else "systemd"

    def why_not(self):
        return "a trial folder is set" if not self.live else "%s is not on the PATH" % self.argv[0]

    def _call(self, args):
        """A CompletedProcess for one launchctl or systemctl call, or None when it may not run."""
        if not self.available:
            return None
        if self.darwin:
            return schedule.launchctl(*args)
        try:
            return self.runner(self.argv + list(args), capture_output=True, text=True, timeout=TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as e:
            return subprocess.CompletedProcess(args, 127, "", str(e))

    def _outcome(self, args):
        p = self._call(args)
        if p is None:
            return Outcome(False, "not run (%s)" % self.why_not(), ran=False)
        return Outcome(p.returncode == 0, "" if p.returncode == 0 else
                       _first((p.stderr or "") + "\n" + (p.stdout or ""), "exit %d" % p.returncode))

    def _service(self, label):
        return "%s/%s" % (schedule.gui_domain(), label)

    # ---- launchd: a label and its plist; systemd: the unit that is enabled (a timer, or the service itself)

    def loaded(self, label):
        if not self.available:
            return None
        if self.darwin:
            return schedule.is_loaded(label)
        return self._call(["is-active", label]).returncode == 0

    def running(self, label):
        """Whether a kept-alive job has a live process: launchd's `state = running`, systemd's active."""
        p = self._call(["print", self._service(label)] if self.darwin else ["is-active", label])
        if p is None:
            return None
        if not self.darwin:
            return p.returncode == 0
        return p.returncode == 0 and any(line.strip() == "state = running" for line in (p.stdout or "").splitlines())

    def load(self, label):
        """Start an installed job, replacing a loaded copy so a rewritten file takes effect."""
        if not self.available:
            return Outcome(False, "not run (%s)" % self.why_not(), ran=False)
        if self.darwin:
            # schedule.load finds the plist from the label alone.
            buf = io.StringIO()
            ok = schedule.load(buf, SimpleNamespace(label=label))
            return Outcome(ok, "" if ok else _first(buf.getvalue().split(": ", 1)[-1], "it did not load"))
        out = self._outcome(["daemon-reload"])
        if not out.ok:
            return out
        if self.loaded(label):
            return self._outcome(["restart", label])
        return self._outcome(["enable", "--now", label])

    def restart(self, label):
        """Restart a kept-alive job that is loaded but not running."""
        return self._outcome(["kickstart", "-k", self._service(label)] if self.darwin else ["restart", label])

    def unload(self, label):
        if self.darwin:
            out = self._outcome(["bootout", self._service(label)])
            return out if out.ok or self.loaded(label) is not False else Outcome(True)
        return self._outcome(["disable", "--now", label])

    def retire(self, label, out, dry_run=False):
        """Retire an older launchd job through schedule.retire_legacy (unloaded, plist kept in
        schedule.legacy_backup_dir()). Returns (ok, actions); nothing runs when the Manager may not."""
        if not self.darwin:
            return True, []
        if not self.available:
            return False, []
        return schedule.retire_legacy(label, out, dry_run)

    def loaded_labels(self):
        """Every label launchd has loaded in this session (from `launchctl list`), or None when unknown."""
        if not self.darwin:
            return None
        p = self._call(["list"])
        if p is None or p.returncode != 0:
            return None
        labels = set()
        for line in (p.stdout or "").splitlines()[1:]:
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
