#!/usr/bin/env python3
"""Planted-control self-check for the shared scrubber and the parser options.

Run: python tests/indexer_checks/scrub_controls.py (or through python -m unittest discover -s tests).
Exits 1 on any failure. Prints only PASS/FAIL per control, never a scanned value other than
the planted strings below (synthetic).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

from fleetwatch.memory import privacy_scan, session_parse  # noqa: E402

REDACT = [
    ("Canvas user 4417203", "4417203"),
    ("student ID 7731904", "7731904"),
    ("user id 5520117", "5520117"),
    ("SIS id 8834102", "8834102"),
    ("the Canvas id 6623391", "6623391"),
    ("the number is QX-123456 here", "QX-123456"),
    ("the number is qx 123456 here", "qx 123456"),
    ("write to jane.doe@somewhere-else.org today", "jane.doe@somewhere-else.org"),
]
KEEP = [("see PR #712", "712"), ("assignment 4417", "4417"), ("run 38963861270 passed", "38963861270"),
        ("ticket 56789", "56789")]
PATHS = ["sealed/L07.md", "roster.csv", "roster-exports/", "vault-ledger.yaml", "vault_ledger",
         "vault ledger"]
# A synthetic private pack: an id format, anonymous ids and folder names of a made-up school.
PRIVATE_PACK = r"""
id_patterns: ['(?i)\bqx[ -]?\d{6}\b']
anon_ids:
  - pattern: '\bL\d{2}\b'
    pairing: '\bL\d{2}\b\s*(?:is|=|:)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)'
private_paths: ['sealed/', 'vault[-_ ]ledger']
"""

fails = 0


def check(ok, label):
    global fails
    print("%s  %s" % ("PASS" if ok else "FAIL", label))
    fails += 0 if ok else 1


tmp = Path(tempfile.mkdtemp())
(tmp / "allow.yaml").write_text("version: 1\nemails: ['*@example.edu']\n")
(tmp / "private.yaml").write_text(PRIVATE_PACK)
RULES = privacy_scan.RuleSet(["education", str(tmp / "private.yaml")])
sc = privacy_scan.Scanner(tmp / "allow.yaml", rules=RULES)
for text, val in REDACT:
    out, _c, _d = sc.scan_text(text)
    check(val not in out, "redacted: %s" % text.split(" here")[0].split("the number is ")[-1][:2] + "...")
for text, val in KEEP:
    out, _c, _d = sc.scan_text(text)
    check(val in out, "kept: %s" % text)
out, cats, dropped = sc.scan_text("L07 is Jordan Smith and L07 = Jordan")
check(dropped == "anon-id-pairing" or "[name]" in out, "anon-id pairing flagged or name redacted")
for p in PATHS:
    check(bool(RULES.private_path_re.search(p)), "private path flagged: %s" % p)

# denylist: apostrophe names, possessives, ordinary words (synthetic names only)
import hashlib
(tmp / "deny.txt").write_text("\n".join(
    hashlib.sha256(n.encode()).hexdigest() + "  synthetic"
    for n in ("o'zzbrien", "qqvexley")) + "\n")
scd = privacy_scan.Scanner(tmp / "allow.yaml", tmp / "deny.txt", RULES)
out, _c, _d = scd.scan_text("We met O'Zzbrien at noon and again later.")
check("Zzbrien" not in out and "[name]" in out, "denylisted apostrophe name redacted")
out, _c, _d = scd.scan_text("Qqvexley's report is late.")
check("Qqvexley" not in out and "[name]" in out, "possessive of denylisted name redacted")
out, _c, _d = scd.scan_text("Monday's meeting covers Thursday.")
check("Monday" in out and "Thursday" in out, "ordinary words kept with denylist active")

# denylist: lowercase uses and names inside file names (case-insensitive alphabetic runs)
out, _c, _d = scd.scan_text("let's start with qqvexley")
check("qqvexley" not in out and "[name]" in out, "lowercase denylisted name redacted")
out, _c, _d = scd.scan_text("see RTPQqvexleyLetterFinal.pdf attached")
check("qvexley" not in out.lower() and "Letter" in out and ".pdf" in out,
      "camel-case segment of a file name redacted")
out, _c, _d = scd.scan_text("file 06-DRAFT-Qqvexley-Letter.docx attached")
check("qvexley" not in out.lower() and "DRAFT" in out and "Letter.docx" in out,
      "hyphen-joined segment of a file name redacted")
out, _c, _d = scd.scan_text("notes_qqvexley2.txt and qqvexley.v3 kept apart")
check("qvexley" not in out.lower(), "underscore, digit and dot split segments redacted")
out, _c, _d = scd.scan_text("the quarterly review of the letter final draft is due Monday")
check(out == "the quarterly review of the letter final draft is due Monday",
      "ordinary lowercase words kept with denylist active")
out, _c, _d = scd.scan_text("Qqvexley said so")
check(out.count("[name]") == 1, "one span is not double-redacted")

# long file names and tokens
fname = "Synthetic-Long-File-Name-For-Testing-Purposes-Only-2026"
out, _c, _d = sc.scan_text("see %s.pdf for details" % fname)
check(fname + ".pdf" in out, "long file name before .pdf kept")
tokn = "Ab3dEf9hIj2kLm5nOp8qRs1tUv4wXy7zAb3dEf9hIj2k"
out, _c, _d = sc.scan_text("the key %s was pasted" % tokn)
check(tokn not in out, "real-looking token without an extension redacted")

# parser options
human = {"origin": {"kind": "queued"}}
for opts_spec, want_kind, label in [
        ({}, "owner", "queued message is the human by default"),
        ({"queued_as_human": False}, "other", "queued_as_human false keeps it non-human"),
        ({"kinds": {"owner": "instructor"}}, "instructor", "kinds map renames the human sender")]:
    o = session_parse.parse_opts(opts_spec)
    r = session_parse.classify_incoming(dict(human), "please do the thing", o)
    check(r[0] == want_kind, label)
o = session_parse.parse_opts({"kinds": {"peer-session": "other-session"},
                              "peer_session_kinds": {"build helper": "build-dev"}})
msg = '<cross-session-message from-name="%s">hi</cross-session-message>'
r1 = session_parse.classify_incoming({}, msg % "Build Helper", o)
r2 = session_parse.classify_incoming({}, msg % "Some Other", o)
check(r1[0] == "build-dev" and r1[3] == "peer-session", "peer name maps to build-dev")
check(r2[0] == "other-session", "unmapped peer takes kinds[peer-session]")
check(session_parse.classify_incoming({"scheduledTaskId": "x"}, "tick", o)[0] == "heartbeat",
      "heartbeat default name")
d = session_parse.parse_opts({})
check(session_parse.exchange_id("abcdef123", "EX", 7, d) == "Sabcdef-EX-0007", "default exchange id")
b = session_parse.parse_opts({"bare_exchange_ids": True})
check(session_parse.exchange_id("abcdef123", "EX", 7, b) == "EX-0007", "bare exchange id")
# person_names: false (fiction corpora): invented names survive, everything else still scrubs.
(tmp / "fiction.yaml").write_text("version: 1\nperson_names: false\nemails: ['*@example.edu']\n")
scf = privacy_scan.Scanner(tmp / "fiction.yaml", rules=RULES)
fiction = "Halaster Blackcloak told Skella Ironheart the gate opens at dusk."
out = scf.scan_groups([[fiction]])[0][0][0]
check("Halaster" in out and "Skella" in out, "person_names false keeps invented names")
out2 = scf.scan_groups([["contact planted.person@gmail.com and student ID 7731904"]])[0][0][0]
check("[email]" in out2 and "[id]" in out2, "person_names false still scrubs email and ids")
out3 = sc.scan_groups([[fiction]])[0][0][0]
check("[name]" in out3, "default still redacts person names")
# Governance text: institutional phrases spaCy tags as PERSON keep their words; a real
# (synthetic) person in the same text is still redacted.
gov = ("The Faculty Senate Secretary records the minutes. Per Bylaws Art. 4, the Presiding "
       "Officer may Verify Senate quorum. Professor Marigold Thistlewaite moved to amend the "
       "Faculty Handbook and the Curriculum Committee report.")
gout = sc.scan_groups([[gov]])[0][0][0]
for w in ["Faculty", "Senate", "Handbook", "Curriculum", "Presiding", "Bylaws"]:
    check(w in gout, "governance word kept: " + w)
check("Thistlewaite" not in gout, "a person named in governance text is still redacted")
(tmp / "inst.yaml").write_text("version: 1\ninstitution_words: [zorbmoot]\n")
sci = privacy_scan.Scanner(tmp / "inst.yaml", rules=RULES)
check("zorbmoot" in sci.inst and "senate" in sci.inst, "per-index institution_words extend the list")
# Phones and cut-off email local parts.
(tmp / "phones.yaml").write_text("version: 1\nphones: ['303-556-0000']\n")
scp = privacy_scan.Scanner(tmp / "phones.yaml", rules=RULES)
def scrub(sc_, t): return sc_.scan_groups([[t]])[0][0][0]
for t in ["call 303-555-0142 today", "call (720) 555-0199 today", "call 303.555.0123 today",
          "call +1 303 555 0177 today"]:
    check("[phone]" in scrub(scp, t), "phone redacted: " + t.split(" ", 1)[1].rsplit(" ", 1)[0])
check("303-556-0000" in scrub(scp, "office 303-556-0000"), "allowlisted office phone kept")
for t in ["date 2026-10-16", "run 38963861270", "version 1.2.3", "tickets 640-650", "count 3035550142"]:
    check("[phone]" not in scrub(scp, t), "not a phone: " + t)
check("[email]" in scrub(sc, "contact | qrandall@ | room"), "cut-off email local part redacted")
for t in ["react@18.3.1 is pinned", "ping @MegaEgg22 there", "the @types/node package"]:
    out = scrub(sc, t)
    check("react@18" in out or "@MegaEgg22" in out or "@types" in out, "kept: " + t)
# Parliamentary single words spaCy tags as PERSON on their own.
ronr = ("Article II - Conduct of Meetings. 11.1.4 Serve as final authority on the Bylaw process. "
        "The Presiding Officer Pro Tempore and the Convener may designate a Designee.")
rout = sc.scan_groups([[ronr]])[0][0][0]
for w in ["Conduct", "Serve", "Bylaw", "Tempore", "Convener", "Designee"]:
    check(w in rout, "parliamentary word kept: " + w)
# General academic vocabulary, and a mixed phrase keeps its institutional word.
acad = ("Academic Freedom applies to every Senior Lecturer. See the State Revised Statutes, "
        "the Curriculum Vitae template, Financial Aid and Advising. Dean Marigold Thistlewaite chairs it.")
aout = sc.scan_groups([[acad]])[0][0][0]
for w in ["Academic", "Senior Lecturer", "Statutes", "Vitae", "Financial Aid", "Advising", "Dean"]:
    check(w in aout, "academic word kept: " + w)
check("Thistlewaite" not in aout and "Marigold" not in aout, "a person in a mixed phrase is still redacted")
# Title backstop: a titled name spaCy may not tag is still redacted; institutional words stay.
tb = sc.scan_groups([['so "Dean Marigold Thistlewaite now becomes" a test; ask Dr. Quellwyn too']])[0][0][0]
check("Thistlewaite" not in tb and "Marigold" not in tb and "Quellwyn" not in tb, "titled names redacted without spaCy")
check("Dean" in tb and "Dr." in tb, "titles kept")
tk = sc.scan_groups([["the Dean of Students and Professor Emeritus status; President Senate Office"]])[0][0][0]
check("Students" in tk and "Emeritus" in tk and "Senate Office" in tk, "titles before institutional words are left alone")
tf = scf.scan_groups([["Professor Halaster Blackcloak lectures"]])[0][0][0]
check("Halaster" in tf, "title backstop off when person_names is false")

# keep / keep_patterns
(tmp / "keep.yaml").write_text(
    "version: 1\nkeep: ['303-555-0142', 'sk-keepme000000000000000000000000000000']\n"
    "keep_patterns: ['Level \\d+ - Room \\d+[a-z]', 'sk-patternonly[A-Za-z0-9]{24,}']\n")
kp = privacy_scan.Scanner(tmp / "keep.yaml", rules=RULES)
kr = lambda t: kp.scan_groups([[t]])[0]
plain = lambda t: sc.scan_groups([[t]])[0][0][0]
out, cats, _d = kr("call 303-555-0142 now")
out = out[0]
check("303-555-0142" in out and "[phone]" not in out, "keep literal that looks like a phone stays")
check("[phone]" in plain("call 303-555-0142 now"), "same phone without the keep is redacted")
check(cats.get("kept-phone") == 1 and "303-555" not in json.dumps(cats), "kept span counted by category, no value")
out, cats, _d = kr("the dungeon code Level 6 - Room 21n is sealed, student id 7731904")
out = out[0]
check("Level 6 - Room 21n" in out and "7731904" not in out, "keep_pattern match stays, other ids still redacted")
check(cats.get("kept-other") == 1, "keep_pattern kept span counted")
tok_pat = "sk-patternonly" + "A1b2C3d4E5f6G7h8I9j0K1l2M3"
tok_lit = "sk-keepme000000000000000000000000000000"
out = kr("key %s here" % tok_pat)[0][0]
check(tok_pat not in out and "[secret]" in out, "secret covered only by a keep_pattern is still redacted")
out = kr("key %s here" % tok_lit)[0][0]
check(tok_lit in out, "secret covered by an exact keep literal stays")
for t in ["call 303-555-0142 now", "Level 6 - Room 21n and 303-555-0199", "key %s here" % tok_lit]:
    o = kr(t)[0][0]
    check(privacy_scan.PH_OPEN not in o and privacy_scan.PH_CLOSE not in o and not privacy_scan.PH_RE.search(o),
          "no placeholder remains in output")
for bad in ["'a*'", "'.*'", "'[a-z ]+'", "'(?:)'"]:
    (tmp / "bad.yaml").write_text("keep_patterns: [%s]\n" % bad)
    try:
        privacy_scan.Scanner(tmp / "bad.yaml")
        check(False, "keep_pattern refused: " + bad)
    except ValueError:
        check(True, "keep_pattern refused: " + bad)
(tmp / "ok.yaml").write_text("keep: ['Level 6']\nkeep_patterns: ['Room \\d+n']\n")
check(privacy_scan.Scanner(tmp / "ok.yaml").version != privacy_scan.Scanner(tmp / "allow.yaml").version,
      "keep entries change the scanner version")
# ferpa guards in refresh_all: neither person_names: false nor keep_patterns is allowed.
from fleetwatch.memory import refresh as _ra  # noqa: E402
_tags = str(Path(__file__).resolve().parent.parent / "fixtures" / "tags.yaml")
def _refused(allow_text, group):
    f = tmp / ("g-%s.yaml" % abs(hash(allow_text + group)))
    f.write_text(allow_text)
    try:
        _ra.Index("t", {"group": group, "privacy": {"allowlist": str(f)}, "tags": _tags}, {})
        return False
    except ValueError:
        return True
check(_refused("version: 1\nkeep_patterns: ['Room \\\\d+']\n", "ferpa"), "ferpa refuses keep_patterns")
check(not _refused("version: 1\nkeep_patterns: ['Room \\\\d+']\n", "personal"), "personal allows keep_patterns")
check(_refused("version: 1\nperson_names: false\n", "ferpa"), "ferpa refuses person_names false")
sys.exit(1 if fails else 0)
