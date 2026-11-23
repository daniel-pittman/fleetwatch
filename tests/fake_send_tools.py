"""Stand-ins for the commands `send` runs: osascript, `ps -axo pid=,tty=` and the foreground read.

    fake_send_tools.py osascript LOG [FAILFILE] -- ...   append {"argv": [...], "script": stdin} to LOG;
                                                         when FAILFILE holds text, print it to stderr and exit 1
    fake_send_tools.py show FILE [...]                   print FILE (extra arguments are ignored)

osascript is called as `osascript - ARGS...`, so the recorded argv starts with "-".
"""
import json
import sys


def main(argv):
    kind = argv[0]
    if kind == "show":
        sys.stdout.write(open(argv[1], encoding="utf-8").read())
        return 0
    log, fail = argv[1], argv[2]
    args = argv[argv.index("--") + 1:]
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": args, "script": sys.stdin.read()}) + "\n")
    text = open(fail, encoding="utf-8").read() if fail != "-" else ""
    if text.strip():
        sys.stderr.write(text)
        return 1
    sys.stdout.write("ok\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
