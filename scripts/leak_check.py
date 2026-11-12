#!/usr/bin/env python3
"""Fail when tracked files (or commit messages) carry personal or secret material.

Categories: home-directory paths, email addresses, cloud-drive path fragments,
private-key and token shapes, and an optional private term list.

The private list is a text file named by FLEETWATCH_LEAK_TERMS: one term per line,
matched case-insensitively as a substring, `#` starts a comment. It lives outside
the repository so personal terms never enter it.

Usage:
    leak_check.py                     scan every tracked file
    leak_check.py --messages RANGE    scan commit messages in a git revision range
    leak_check.py --history RANGE     scan the lines each commit in RANGE adds, the paths it
                                      touches, and its author and committer addresses
"""
import os
import re
import subprocess
import sys

ENV_TERMS = "FLEETWATCH_LEAK_TERMS"

# Placeholder user names that may appear in a path.
FICTIONAL_USERS = {
    "alex", "example", "jane", "me", "name", "someone", "sam", "user", "username", "you",
    "yourname", "your-name", "dev", "demo", "test", "runner", "x", "u", "xyz",
}
# Domains and suffixes that cannot reach a real person.
# A company domain is not safe as a whole: its people have addresses there too. The
# co-author trailer's noreply address passes through SAFE_EMAIL_LOCALPARTS.
SAFE_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com",
                      "noreply.github.com")
SAFE_EMAIL_SUFFIXES = (".invalid", ".test", ".example", ".localhost")
SAFE_EMAIL_LOCALPARTS = ("noreply", "no-reply")

HOME_PATH = re.compile(r"(?<![\w.])/(?:Users|home)/([A-Za-z0-9._-]+)")
# Claude Code names a project folder after its path with "/" turned into "-":
# -Users-<name>-git-repo. The name is the segment after Users or home.
HOME_SLUG = re.compile(r"(?<![\w-])-(?:Users|home)-([A-Za-z0-9._]+)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
CLOUD_DRIVE = re.compile(
    r"CloudStorage|Mobile Documents|com~apple~CloudDocs|Google ?Drive|GoogleDrive|OneDrive|Dropbox"
    r"|iCloud ?Drive|/My Drive/", re.IGNORECASE)
SECRET_SHAPES = {
    "private key block": re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "Anthropic key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "AWS access key id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "generic bearer secret": re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*[\"'][A-Za-z0-9+/_-]{24,}[\"']"),
}


# Findings that are the point of the file: detectors name what they detect, and their
# tests plant fictional examples of it. Each entry is (path, category, excerpt); an
# excerpt of None allows the whole category in that file. Keep this list short.
ALLOWED = [
    ("fleetwatch/memory/doc_corpus.py", "cloud drive path", None),
    ("fleetwatch/resume.py", "cloud drive path", None),
    ("tests/test_resume.py", "cloud drive path", None),
    ("tests/test_resume.py", "email", "GoogleDrive-a@b.com"),
    ("fleetwatch/memory/privacy_scan.py", "email", "git@github.com"),
    ("tests/indexer_checks/scrub_controls.py", "email", "jane.doe@somewhere-else.org"),
    ("tests/indexer_checks/scrub_controls.py", "email", "planted.person@gmail.com"),
    ("tests/test_scrub_security.py", "Anthropic key", None),
    ("tests/test_scrub_security.py", "generic bearer secret", None),
]


# This file spells out the patterns and the allowlist, so it cannot scan itself.
SELF = "scripts/leak_check.py"


def allowed(path, category, excerpt):
    return any(path == p and category == c and (e is None or e == excerpt) for p, c, e in ALLOWED)


def load_terms(path=None):
    """Return the private terms from `path` (default: the FLEETWATCH_LEAK_TERMS file), lowercased."""
    path = path or os.environ.get(ENV_TERMS)
    if not path:
        return []
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        lines = [ln.split("#", 1)[0].strip() for ln in fh]
    return [ln.lower() for ln in lines if ln]


def scan_text(text, terms=()):
    """Return (category, line number, excerpt) for every hit in `text`."""
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for rx in (HOME_PATH, HOME_SLUG):
            for m in rx.finditer(line):
                if m.group(1).lower() not in FICTIONAL_USERS:
                    hits.append(("home path", n, m.group(0)))
        for m in EMAIL.finditer(line):
            domain = m.group(1).lower()
            local = m.group(0).split("@", 1)[0].lower()
            if (domain in SAFE_EMAIL_DOMAINS or domain.endswith(SAFE_EMAIL_SUFFIXES)
                    or local in SAFE_EMAIL_LOCALPARTS):
                continue
            # A pinned requirement such as `name==1.0@sha` is not an address.
            hits.append(("email", n, m.group(0)))
        for m in CLOUD_DRIVE.finditer(line):
            hits.append(("cloud drive path", n, m.group(0)))
        for label, rx in SECRET_SHAPES.items():
            if rx.search(line):
                hits.append((label, n, "<redacted>"))
        low = line.lower()
        for term in terms:
            if term in low:
                hits.append(("private term", n, "<redacted>"))
    return hits


def tracked_files(root):
    out = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def scan_tree(root, terms=()):
    """Return {path: [(category, line, excerpt), ...]} for every tracked text file."""
    found = {}
    for rel in tracked_files(root):
        if rel == SELF:
            continue
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except UnicodeDecodeError:
            continue  # binary asset
        hits = [h for h in scan_text(text, terms) if not allowed(rel, h[0], h[2])]
        # A path can carry a personal term too.
        hits += [(c, 0, e) for c, _, e in scan_text(rel, terms)]
        if hits:
            found[rel] = hits
    return found


def scan_messages(root, rev_range, terms=()):
    out = subprocess.run(["git", "-C", root, "log", "--format=%H%n%an <%ae>%n%B%x00", rev_range],
                         capture_output=True, check=True).stdout.decode("utf-8", "replace")
    found = {}
    for entry in out.split("\0"):
        entry = entry.strip("\n")
        if not entry:
            continue
        sha = entry.split("\n", 1)[0][:12]
        # The author line is the committer's own identity; scan the message body only.
        body = entry.split("\n", 2)[2] if entry.count("\n") >= 2 else ""
        hits = scan_text(body, terms)
        if hits:
            found["commit " + sha] = hits
    return found


def address_hits(label, address, terms=()):
    """Hits for one author or committer address: any address scan_text would flag, and
    any private term in it."""
    return [(label, 0, address if c == "email" else "<redacted>")
            for c, _, _ in scan_text(address, terms) if c in ("email", "private term")]


def scan_history(root, rev_range, terms=()):
    """Return {"commit <sha> <path>": hits} for the lines every commit in `rev_range` adds
    (so a line added and later deleted is still found), the paths it touches, and
    {"commit <sha>": hits} for its author and committer addresses. Message bodies are
    --messages' job."""
    out = subprocess.run(["git", "-C", root, "log", "-p", "--no-color", "--no-renames",
                          "--unified=0", "--no-ext-diff", "--format=%x00%H%n%ae%n%ce", rev_range],
                         capture_output=True, check=True).stdout.decode("utf-8", "replace")
    found = {}
    for entry in out.split("\0"):
        if not entry.strip():
            continue
        head, _, diff = entry.partition("\n\n")
        lines = head.split("\n")
        sha = lines[0][:12]
        ids = address_hits("author email", lines[1], terms) if len(lines) > 1 else []
        if len(lines) > 2 and lines[2] != lines[1]:
            ids += address_hits("committer email", lines[2], terms)
        if ids:
            found["commit " + sha] = ids
        path, n = None, 0
        for line in diff.split("\n"):
            if line.startswith("+++ "):
                path = line[6:] if line.startswith("+++ b/") else None
                n = 0
                if path and path != SELF:
                    hits = [(c, 0, e) for c, _, e in scan_text(path, terms)]
                    if hits:
                        found.setdefault("commit %s %s" % (sha, path), []).extend(hits)
                continue
            if line.startswith("@@"):
                m = re.match(r"@@ -\S+ \+(\d+)", line)
                n = int(m.group(1)) if m else 0
                continue
            if line.startswith("+") and path and path != SELF:
                hits = [(c, n, e) for c, _, e in scan_text(line[1:], terms) if not allowed(path, c, e)]
                if hits:
                    found.setdefault("commit %s %s" % (sha, path), []).extend(hits)
                n += 1
    return found


def report(found, show_excerpts=True):
    for path, hits in sorted(found.items()):
        for cat, n, excerpt in hits:
            where = "%s:%s" % (path, n) if n else path
            print("%s: %s%s" % (where, cat, " (%s)" % excerpt if show_excerpts and excerpt != "<redacted>" else ""))


def main(argv):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    terms = load_terms()
    if ENV_TERMS not in os.environ:
        print("note: %s is not set; the private term list was not applied" % ENV_TERMS, file=sys.stderr)
    if len(argv) == 3 and argv[1] == "--messages":
        found = scan_messages(root, argv[2], terms)
    elif len(argv) == 3 and argv[1] == "--history":
        found = scan_history(root, argv[2], terms)
    else:
        found = scan_tree(root, terms)
    report(found)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
