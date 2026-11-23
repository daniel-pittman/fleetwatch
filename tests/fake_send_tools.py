"""Stand-ins for the commands `send` runs: osascript, the terminal read and `claude agents`.

    fake_send_tools.py osascript LOG FAILFILE THENFILE -- ...
        append {"argv": [...], "script": stdin} to LOG; when FAILFILE holds text, print it to stderr and
        exit 1. Afterwards, when THENFILE exists, write each {path: text} it holds and remove it: the
        world changing while the text is typed, seen by the checks before the next call.
    fake_send_tools.py show FILE [-t TTY]
        print FILE. With -t, only when TTY is the terminal name held in FILE.tty; any other exits 1, as
        ps does for a terminal with nothing on it.

osascript is called as `osascript - ARGS...`, so the recorded argv starts with "-".
"""
import json
import os
import sys


def show(argv):
    path, rest = argv[0], argv[1:]
    if rest:
        if len(rest) != 2 or rest[0] != "-t":
            sys.stderr.write("unexpected arguments: %r\n" % rest)
            return 2
        with open(path + ".tty", encoding="utf-8") as fh:
            if rest[1] != fh.read().strip():
                return 1
    sys.stdout.write(open(path, encoding="utf-8").read())
    return 0


def osascript(argv):
    log, fail, then = argv[0], argv[1], argv[2]
    args = argv[argv.index("--") + 1:]
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": args, "script": sys.stdin.read()}) + "\n")
    text = open(fail, encoding="utf-8").read() if os.path.exists(fail) else ""
    if os.path.exists(then):
        with open(then, encoding="utf-8") as fh:
            changes = json.load(fh)
        os.remove(then)
        for path, body in changes.items():
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
    if text.strip():
        sys.stderr.write(text)
        return 1
    sys.stdout.write("ok\n")
    return 0


def main(argv):
    return show(argv[1:]) if argv[0] == "show" else osascript(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
