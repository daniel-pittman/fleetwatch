"""A stand-in for `launchctl`, keeping what is loaded in a JSON file: FAKE_LAUNCHCTL_STATE names it
({"loaded": {label: plist path}, "running": [label, ...]}), FAKE_LAUNCHCTL_LOG gets one line per call.

Supported: `print gui/UID/LABEL`, `bootstrap gui/UID PLIST` (a kept-alive job starts running), `bootout
gui/UID/LABEL`, `kickstart -k gui/UID/LABEL` (marks it running) and `list`. A label in the state's "fail"
list makes bootstrap and bootout of it fail the way launchctl does. It never touches a real launchd.
Run it as FLEETWATCH_LAUNCHCTL="python tests/fake_launchd.py".
"""
import json
import os
import plistlib
import sys


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save(path, state):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)


def call(argv, state_path, log_path=None):
    """(returncode, stdout, stderr) for one launchctl command line."""
    if log_path:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(" ".join(argv) + "\n")
    state = _load(state_path)
    loaded = state.setdefault("loaded", {})
    running = set(state.get("running", []))
    fail = set(state.get("fail", []))
    verb = argv[0] if argv else ""
    if verb == "print" and len(argv) == 2:
        label = argv[1].rsplit("/", 1)[-1]
        if label not in loaded:
            return 113, "", "Could not find service \"%s\" in domain for user gui\n" % label
        return 0, "%s = {\n\tstate = %s\n}\n" % (argv[1], "running" if label in running else "not running"), ""
    if verb == "bootstrap" and len(argv) == 3:
        try:
            with open(argv[2], "rb") as fh:
                info = plistlib.load(fh)
            label = info["Label"]
        except (OSError, KeyError, plistlib.InvalidFileException):
            return 5, "", "Bootstrap failed: 5: Input/output error\n"
        if label in fail:
            return 5, "", "Bootstrap failed: 5: Input/output error\n"
        if label in loaded:
            return 37, "", "Bootstrap failed: 37: Operation already in progress\n"
        loaded[label] = argv[2]
        if info.get("KeepAlive"):
            state["running"] = sorted(running | {label})
        _save(state_path, state)
        return 0, "", ""
    if verb == "bootout" and len(argv) == 2:
        label = argv[1].rsplit("/", 1)[-1]
        if label in fail:
            return 5, "", "Boot-out failed: 5: Input/output error\n"
        if label not in loaded:
            return 3, "", "Boot-out failed: 3: No such process\n"
        del loaded[label]
        state["running"] = sorted(running - {label})
        _save(state_path, state)
        return 0, "", ""
    if verb == "kickstart" and len(argv) == 3 and argv[1] == "-k":
        label = argv[2].rsplit("/", 1)[-1]
        if label not in loaded or label in fail:
            return 113, "", "Could not find service \"%s\" in domain for user gui\n" % label
        state["running"] = sorted(running | {label})
        _save(state_path, state)
        return 0, "", ""
    if verb == "list" and len(argv) == 1:
        rows = ["PID\tStatus\tLabel"] + ["%s\t0\t%s" % ("123" if lb in running else "-", lb) for lb in sorted(loaded)]
        return 0, "\n".join(rows) + "\n", ""
    return 64, "", "fake launchctl: unsupported: %s\n" % " ".join(argv)


if __name__ == "__main__":
    rc, out, err = call(sys.argv[1:], os.environ["FAKE_LAUNCHCTL_STATE"], os.environ.get("FAKE_LAUNCHCTL_LOG"))
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(rc)
