"""A stand-in for launchctl's print, bootstrap and bootout. A label is loaded while a file of its name exists in
FAKE_LAUNCHCTL_DIR; every call is appended to calls.log there. FAKE_LAUNCHCTL_FAIL lists verbs that fail."""
import os
import plistlib
import sys

root = os.environ["FAKE_LAUNCHCTL_DIR"]
os.makedirs(root, exist_ok=True)
args = sys.argv[1:]
with open(os.path.join(root, "calls.log"), "a") as fh:
    fh.write(" ".join(args) + "\n")
verb = args[0] if args else ""
if verb in os.environ.get("FAKE_LAUNCHCTL_FAIL", "").split(","):
    print("%s failed: 5: Input/output error" % verb.capitalize(), file=sys.stderr)
    sys.exit(5)
if verb == "print":
    sys.exit(0 if os.path.exists(os.path.join(root, args[1].split("/")[-1])) else 113)
if verb == "bootstrap":
    with open(args[2], "rb") as fh:
        label = plistlib.load(fh)["Label"]
    if os.path.exists(os.path.join(root, label)):
        print("Bootstrap failed: 5: Input/output error", file=sys.stderr)
        sys.exit(5)
    open(os.path.join(root, label), "w").close()
    sys.exit(0)
if verb == "bootout":
    label = args[1].split("/")[-1]
    if not os.path.exists(os.path.join(root, label)):
        print("Boot-out failed: 3: No such process", file=sys.stderr)
        sys.exit(3)
    os.unlink(os.path.join(root, label))
    sys.exit(0)
sys.exit(64)
