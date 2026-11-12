"""Shared privacy scrubber for session and document corpora.

Everything is scrubbed before any text is written to corpus/ or state/, and before any text is
sent to the tagger. The allowlist and denylist come from the index; the rules come from the
core set plus the rule packs settings.toml enables.

Replacements: secrets -> [secret], emails not on the allowlist -> [email], phone numbers ->
[phone], numbers beside an id word ("user", "login", plus each pack's words) and each pack's
id patterns -> [id], keyed identifiers (uid, oid, tenant, sub) -> [id], person names spaCy finds
that are not allowlisted -> [name]. Categories and counts are recorded, never values.

Needs spaCy with en_core_web_sm (the `memory` extra). spaCy loads lazily, so a run that has
nothing to scan never pays for it.

Allowlist file (YAML):
    version: 1
    anon_id_pairing: false     # turn off the packs' anonymous-id pairing check for this index
    emails: [addr, "*suffix", "prefix*"]
    staff:  [lowercase name tokens never redacted and never counted as pairing]
    names:  [lowercase names/tokens that are not people]
    keep:   [literal strings, matched case-insensitively, left alone by every rule; a secret
             rule's match is left alone only when one literal covers all of it]
    keep_patterns: [regexes; a span they cover is left alone by every rule except the secret rules]
Denylist file: one sha256 of a lowercased name token per line (names never stored on disk).

Rule pack (built in: core, always on, and education; or a YAML file of the same keys, all
optional):
    id_context_words: [words; a 5+ digit number within 30 characters after one is an id]
    id_patterns: [regexes; every match becomes [id], in order, before the context rule]
    anon_ids: [{pattern: regex of an anonymous id, pairing: regex whose group 1 is the name
               paired with one}]   an exchange or document pairing one with a name is dropped
    private_paths: [regex fragments, case-insensitive; a mention is flagged, not redacted]
    institution_words: [lowercase words never redacted as part of a name]
    column_denylist: [spreadsheet headers whose columns document conversion drops]
"""
import hashlib
import json
import re
from pathlib import Path

import yaml

SCAN_RULES_VERSION = 17

EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# A local part whose domain was cut off ("jdoe@" in a table cell) still carries a surname.
# Runs after EMAIL_RE; a following domain-like token (react@18.3.1, git@github.com) is left
# to EMAIL_RE or kept.
PARTIAL_EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z][A-Za-z0-9._%+-]{1,}@(?![A-Za-z0-9-]+\.)(?![\w\[])")
# One name token: a capital (accented ones too), then lowercase runs, each of which may open
# with one capital or an apostrophe, so O'Brien, O\u2019Brien, McDonald, DeShawn, D'Angelo
# and \u00d3scar all match. Two capitals in a row never do: spaCy tags product names and
# acronyms ("Claude API", "OpenAPI") as people often enough that they would be redacted
# everywhere.
NAME_UPPER = "A-Z\u00C0-\u00D6\u00D8-\u00DE"
NAME_LOWER = "a-z\u00DF-\u00F6\u00F8-\u017F"
# Every part after the first starts with a capital or an apostrophe and every lowercase run is
# whole, so a long run cannot backtrack exponentially (TITLE_NAME_RE runs over all text).
NAME_TOKEN = r"[{u}](?:[{lo}]+|(?=['\u2019]))(?:['\u2019][{u}]?[{lo}]+|[{u}][{lo}]+)*".format(
    u=NAME_UPPER, lo=NAME_LOWER)
# A closed set of lowercase name particles ("Maarten van der Quellwyn", "Omar bin Quellano").
# A particle is part of a name only between two name tokens, never first, last or alone, and
# is never redacted on its own, so "van", "de la" and "e" in prose stay. "al-" and "el-" also
# glue onto the following token ("Leila al-Brimwell").
NAME_PARTICLES = frozenset("""
van der den de del della di da dos das du la le von zu bin binti ibn ben bint al el y e ter
ten op
""".split())
_PARTICLE = "(?:%s)" % "|".join(sorted(NAME_PARTICLES, key=len, reverse=True))
NAME_GLUED = r"(?:(?:al|el)-)?"
# Between two name tokens joined by a space: any spaced particles, then a glued prefix.
NAME_GAP = r"(?:%s )*%s" % (_PARTICLE, NAME_GLUED)
NAME_WORD = r"{t}(?:-{g}{t})*".format(t=NAME_TOKEN, g=NAME_GLUED)


def name_parts(toks):
    """`toks` without its particles. A token is a particle only when it is written lowercase,
    is in NAME_PARTICLES, and is neither first nor last: a capitalized "Ben", "Della" or "Van"
    is a given name, and must be redacted and checked against the allowlist like any other."""
    return [t for i, t in enumerate(toks)
            if not (0 < i < len(toks) - 1 and t in NAME_PARTICLES)]
# A name ends before anything but a word character or an apostrophe that starts one, so a
# possessive apostrophe ("Dr. Jones' lab") ends the name instead of hiding it.
NAME_END = r"(?!\w|['\u2019]\w)"
# A title followed by one or two capitalized words, with particles between them, is a person
# even when spaCy does not tag it ("Dean Marigold Thistlewaite" quoted inside a sentence). The
# title stays; the words that are neither institutional nor allowlisted become [name].
TITLE_NAME_RE = re.compile(
    r"\b(Dean|Provost|Chancellor|President|Professor|Prof\.|Dr\.|Dr|Mr\.|Mrs\.|Ms\.|Mx\.)"
    r"( %s(?: %s%s)?)%s" % (NAME_WORD, NAME_GAP, NAME_WORD, NAME_END))
# North American phone numbers with separators: 303-555-1234, (303) 555-1234, 303.555.1234,
# +1 303 555 1234. A bare 10-digit run is left alone (run ids and counts look the same).
PHONE_RE = re.compile(
    r"(?<![\w.+-])(?:\+?1[ .-]?)?(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-]\d{4}(?![\w-])")
NUM_RE = re.compile(r"(?<![\w#./:-])\d{5,}(?![\w-])")
ID_EXEMPT_RE = re.compile(r"\b(assignment|assignments|course|courses|quiz|quizzes|module|page|"
                          r"file|files|rubric|outcome|section|term|account|item|folder|tool|"
                          r"task|issue|ticket|pr|req|commit|pid|line|lines|port|group|"
                          r"category|discussion|announcement|attachment|submission|token|"
                          r"tokens|bytes|chars|characters|ms|seconds|cost|run|job|build)\b", re.I)

SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"(?i)\b(?:bearer|authorization:?\s*bearer)\s+[A-Za-z0-9._~+/-]{24,}=*"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|passwd|password|token|access[_-]?key)\b\s*[:=]\s*[\"']?"
               r"[A-Za-z0-9+/_=-]{20,}"),
    # credentials inside a connection URL: scheme://user:pass@host
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]+@"),
    # secret-sharing links carry the decryption key in the URL
    re.compile(r"(?i)\bhttps?://(?:share\.1password\.com|send\.bitwarden\.com|onetimesecret\.com/secret|"
               r"privnote\.com)/\S+"),
    # AWS secret access key beside its label
    re.compile(r"(?i)\baws_secret_access_key\b\s*[:=]\s*[\"']?[A-Za-z0-9+/]{30,}"),
]
B64_RE = re.compile(r"(?<![\w/+=.-])[A-Za-z0-9+/]{40,}={0,2}(?![\w/+=-])")
# URL-safe base64 runs (hyphen and underscore), as in signed links and encoded ids.
# A run directly followed by a file extension is a long file name, not a token.
FILE_EXT = r"(?:pdf|docx?|xlsx?|pptx?|csv|tsv|txt|md|json|ya?ml|html?|png|jpe?g|gif|svg|zip|py|js|jsx|ts|tsx|log)"
B64URL_RE = re.compile(r"(?<![\w/+=.-])[A-Za-z0-9_-]{40,}(?![\w/+=-])(?!\.%s\b)" % FILE_EXT)
# A query or fragment parameter value that is long enough to be a token or signature.
QUERY_SECRET_RE = re.compile(r"(?<=[?&;#])([A-Za-z0-9_.-]{1,24}=)[A-Za-z0-9+/_%.~=-]{24,}")
# Identifiers keyed by a label that names a person's account: Firebase uid, Entra oid/sub/tid.
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
KEYED_ID_RE = re.compile(
    r"(?i)(\b(?:uid|firebase[_ ]?uid|local[_ ]?id|object[_ ]?id|oid|tid|sub|upn|user[_ ]?id)\b"
    r"[\"']?\s*[:=]\s*[\"']?)(?:%s|[A-Za-z0-9]{24,36})(?![A-Za-z0-9-])" % UUID)


def looks_secret_b64(s):
    if re.fullmatch(r"[0-9a-fA-F]+", s):
        return False                      # git sha / hex digest
    if not (re.search(r"[a-z]", s) and re.search(r"[A-Z]", s) and re.search(r"\d", s)):
        return False
    digits = sum(c.isdigit() for c in s)
    return len(set(s)) >= 20 and digits >= 4 and digits / len(s) >= 0.10


# Words spaCy often tags as PERSON inside institutional phrases ("Faculty Senate Secretary",
# "Bylaws Art"). A candidate made only of these and allowlisted words is not a person, and
# these words are never redacted on their own as a component of a longer name. Packs add
# words; an index can add its own under `institution_words:` in its scan allowlist.
CORE_INSTITUTION_WORDS = """
academic academy accreditation ad affairs agenda amendment annual appendix approval art article
articles assembly assessment assistant associate attendance board budget bylaws calendar campus
caucus center centre chair chancellor charter clause college commission committee constitution
council course courses curriculum dean department departments director division draft
election emeritus executive faculty fall finance form general governance graduate guideline
guidelines handbook hoc honors institute item items liaison manual meeting member members
minutes motion office officer officers ombuds parliamentarian plan policies policy president
presiding procedure procedures professor program programs provost quorum rank record regent
regents report resolution review rules school section secretary senate senator senators
service services session spring staff standing state statement student students studies
subcommittee summer task force term treasurer trustee trustees undergraduate university
verify vice vote
adjourn adjournment agenda alternate amend ballot bylaw caucus chairperson conduct consent
convener delegate delegates designee ex floor meetings nominate nomination nominations officio
order orders parliamentary preside presides pro recess recording second serve serves
tempore
""".split()

EDUCATION_INSTITUTION_WORDS = """
adjunct admissions advising advisor advisors aid alumni bursar catalog catalogue
certificate classroom clinical coordinator credit credits degree degrees diploma
enrollment extension fellowship financial freedom grade grades honorary instructor instructors
lecturer lecturers library major majors minor minors online prerequisite registrar research
revised schedule scholarship semester senior statute statutes syllabus teaching tenure
transcript tuition vitae workload
""".split()

PACK_KEYS = ("id_context_words", "id_patterns", "anon_ids", "private_paths", "institution_words",
             "column_denylist")

BUILTIN_PACKS = {
    "core": dict(id_context_words=["user", "users", "login"],
                 institution_words=CORE_INSTITUTION_WORDS),
    # Student-record context: an LMS or SIS number beside "student", "sis" or "canvas" is an
    # id; academic vocabulary is not a name; a roster mention is flagged; student and SIS id
    # columns are dropped. Institution id formats and anonymous ids need a pack file.
    "education": dict(id_context_words=["student", "students", "sis", "canvas"],
                      institution_words=EDUCATION_INSTITUTION_WORDS,
                      private_paths=["roster"], column_denylist=["student id", "sis id"]),
}
DEFAULT_PACKS = ("education",)


class RuleSet:
    """The core rules merged with the enabled packs. A pack is a built-in name or the path of
    a YAML pack file; a pack that cannot be loaded raises, because scrubbing without it would
    leave in what it was meant to take out."""

    def __init__(self, packs=DEFAULT_PACKS):
        specs = [("core", BUILTIN_PACKS["core"])]
        for p in packs:
            if p == "core":
                continue
            specs.append((p, BUILTIN_PACKS[p] if p in BUILTIN_PACKS else self._read(p)))
        self.names = [n for n, _ in specs]
        ctx, ids, anon, paths, inst, cols = [], [], [], [], set(), []
        compiled = {}       # pattern text -> compiled, each checked once with its pack and rule
        for name, spec in specs:
            unknown = set(spec) - set(PACK_KEYS) - {"name", "version"}
            if unknown:
                raise ValueError("rule pack %s: unknown keys %s" % (name, ", ".join(sorted(unknown))))
            ctx += [str(w).lower() for w in spec.get("id_context_words") or []]
            ids += [str(x) for x in spec.get("id_patterns") or []]
            for a in spec.get("anon_ids") or []:
                if not isinstance(a, dict) or not a.get("pattern"):
                    raise ValueError("rule pack %s: each anon_ids entry needs a pattern" % name)
                anon.append((str(a["pattern"]), str(a.get("pairing") or "")))
            paths += [str(x) for x in spec.get("private_paths") or []]
            inst |= {str(w).lower() for w in spec.get("institution_words") or []}
            cols += [str(c) for c in spec.get("column_denylist") or []]
            self._check(name, spec, compiled)
        try:
            self.id_res = [compiled[x] for x in ids]
            self.anon_res = [compiled[a] for a, _ in anon]
            self.pair_res = [compiled[b] for _, b in anon if b]
            self.private_path_re = re.compile("|".join(paths), re.I) if paths else None
        except re.error as e:
            raise ValueError("rule pack private_paths, combined: %s" % e)
        ctx = list(dict.fromkeys(ctx))
        self.id_context_re = re.compile(r"\b(%s)\b|\bids?\b" % "|".join(map(re.escape, ctx)), re.I)
        self.institution_words = frozenset(inst)
        self.column_denylist = list(dict.fromkeys(cols))
        self.fingerprint = hashlib.sha1(json.dumps(dict(
            ctx=sorted(ctx), ids=ids, anon=anon, paths=sorted(set(paths)),
            inst=sorted(inst)), sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _check(name, spec, compiled):
        """Compile every regex of one pack when it loads, so a bad rule is a configuration
        error naming the pack and the rule, never a failure halfway through a scan. A pairing
        rule must capture the name (group 1), which the scan compares with the names found."""
        rules = [("id_patterns[%d]" % i, str(x), 0) for i, x in enumerate(spec.get("id_patterns") or [])]
        for i, a in enumerate(spec.get("anon_ids") or []):
            rules.append(("anon_ids[%d].pattern" % i, str(a["pattern"]), 0))
            if a.get("pairing"):
                rules.append(("anon_ids[%d].pairing" % i, str(a["pairing"]), 1))
        rules += [("private_paths[%d]" % i, str(x), 0) for i, x in enumerate(spec.get("private_paths") or [])]
        for where, text, groups in rules:
            try:
                rx = re.compile(text, re.I) if where.startswith("private_paths") else re.compile(text)
            except re.error as e:
                raise ValueError("rule pack %s: %s %r is not a valid regex: %s" % (name, where, text, e))
            if rx.groups < groups:
                raise ValueError("rule pack %s: %s %r needs a capture group around the name"
                                 % (name, where, text))
            if not where.startswith("private_paths"):
                compiled.setdefault(text, rx)

    @staticmethod
    def _read(path):
        try:
            spec = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            raise ValueError("rule pack %s cannot be read: %s" % (path, e))
        if not isinstance(spec, dict):
            raise ValueError("rule pack %s is not a mapping" % path)
        return spec


def protected_ready(rules):
    """(ok, reason) for scrubbing an index in a protected group with `rules`: the packs must
    define both id patterns (an institution's student-id format) and anonymous-id rules,
    which core and the built-in packs never do, so a private pack that is not enabled can
    never leave its ids in such a corpus. `reason` names the settings key to fix."""
    missing = [k for k, have in (("id_patterns", rules.id_res), ("anon_ids", rules.anon_res)) if not have]
    if not missing:
        return True, ""
    return False, ("rule packs must define %s; add a pack file that does to settings.toml "
                   "[scanner] rule_packs (enabled now: %s)" % (" and ".join(missing), ", ".join(rules.names)))


# A spaCy PERSON span is kept as a name when it is name tokens or initials ("J.") joined by
# spaces or hyphens, with particles only between them, at most six tokens counting the
# particles, and at least one name token, so initials alone ("J. K.") and particles alone
# ("de la") are not. Real names come in every one of these shapes; a span this rejects
# reaches the corpus whole.
NAMEISH_RE = re.compile(
    r"^(?=(?:.*[ -])?[{u}][{lo}'\u2019])(?!(?:[^ -]*[ -]){{6}})"
    r"(?:{t}|[{u}]\.)(?:[ -]{g}(?:{t}|[{u}]\.))*$".format(
        t=NAME_TOKEN, u=NAME_UPPER, lo=NAME_LOWER, g=NAME_GAP))


# A name this scan redacts is written as NAME_MARK until the pairing check has run, so a
# "[name]" already in the text never counts as one. Private-use characters, never in prose.
NAME_MARK = "\ue000[name]\ue001"


def name_rx(n, joined=False):
    """A pattern for name `n` as a whole word. Lookarounds, not \\b, so a name that ends in an
    initial ("Mary J.") still matches before a space. `joined` also takes name tokens hyphened
    on after it, since spaCy often stops at the hyphen ("Smith" of "Smith-Jones")."""
    tail = "(?:-%s%s)*" % (NAME_GLUED, NAME_TOKEN) if joined else ""
    return r"(?<!\w)%s%s(?!\w)" % (re.escape(n), tail)


# No apostrophe in the token, so "Alyssa's" hashes as "alyssa".
CAP_TOKEN_RE = re.compile(r"\b[A-Z][A-Za-z\u00C0-\u017F-]+\b")
# Capitalized tokens with an inner apostrophe (O'Brien, O'Brien's), checked as written and with
# a trailing possessive stripped, since the denylist may hold either form.
APOS_TOKEN_RE = re.compile(r"\b[A-Z][A-Za-z\u00C0-\u017F-]*(?:['\u2019][A-Za-z\u00C0-\u017F-]+)+")
# Every alphabetic run, and within a run each camel-case segment, is checked against the denylist
# in any case, so lowercase uses and names inside file names ("RTPQqvexleyLetter.pdf",
# "06-DRAFT-Qqvexley-Letter.docx") are caught.
ALPHA_RUN_RE = re.compile(r"[A-Za-z\u00C0-\u017F]+")
CAMEL_SEG_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z\u00C0-\u017F]+|[A-Z\u00C0-\u00DE][a-z\u00C0-\u017F]+")
POSSESSIVE_RE = re.compile(r"['\u2019]s$", re.I)
LEAD_RE = re.compile(r"(?:\b[A-Z][a-z'\u00C0-\u017F]+\s+(?:[A-Z]\.\s+)?)$")
_NLP = None


def nlp():
    global _NLP
    if _NLP is None:
        import spacy
        _NLP = spacy.load("en_core_web_sm", disable=["parser", "lemmatizer", "attribute_ruler"])
        _NLP.max_length = 2_000_000
    return _NLP


def split_pieces(t, size=25_000):
    """Cut text into pieces of about `size` characters at line breaks (else spaces), so a
    name is never split across two spaCy documents."""
    out, k = [], 0
    while k < len(t):
        end = min(k + size, len(t))
        if end < len(t):
            cut = max(t.rfind("\n", k + size // 2, end), t.rfind(" ", k + size // 2, end))
            end = cut + 1 if cut > k else end
        out.append(t[k:end])
        k = end
    return out


def token_hash(tok):
    return hashlib.sha256(tok.lower().encode("utf-8")).hexdigest()


def email_allowed(addr, allow_emails):
    low = addr.lower()
    return any(low == a or (a.startswith("*") and low.endswith(a[1:])) or
               (a.endswith("*") and low.startswith(a[:-1])) for a in allow_emails)


# A kept span is swapped for a placeholder before the rules run and restored afterwards. The
# placeholder is lowercase letters between private-use characters: no digit, no @, nothing
# name-shaped, so no rule can match it.
PH_OPEN, PH_CLOSE = "\ue000", "\ue001"
PH_RE = re.compile("%s([a-z]+)%s" % (PH_OPEN, PH_CLOSE))
# Sample for the keep_patterns breadth check: prose, ids, an email, a phone, a path.
KEEP_SAMPLE = ("The committee met on Tuesday to review the budget for Room 21 and Level 6. "
               "Contact 303-555-0142 or jdoe@example.org about ticket 4417203, id 5520117. "
               "See docs/plan.md for the schedule; Alex and Jordan will present. Version 1.2.3.")


def ph_name(n):
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(97 + r) + s
    return PH_OPEN + "kq" + s + PH_CLOSE


# Marks keep-literal scans made under the whole-match rule for secrets (Scanner.version).
KEEP_SECRET_RULE = b"|keep-literal-whole-secret"


def secret_category(text, rules):
    """The category a span would have been redacted under, for the kept-span counts."""
    if any(rx.search(text) for rx in SECRET_PATTERNS) or QUERY_SECRET_RE.search(text) or \
            ((B64_RE.search(text) or B64URL_RE.search(text)) and looks_secret_b64(text.strip())):
        return "secret"
    if EMAIL_RE.search(text) or PARTIAL_EMAIL_RE.search(text):
        return "email"
    if PHONE_RE.search(text):
        return "phone"
    if KEYED_ID_RE.search(text):
        return "keyed-id"
    if any(rx.search(text) for rx in rules.id_res) or NUM_RE.search(text):
        return "numeric-id"
    if rules.private_path_re and rules.private_path_re.search(text):
        return "private-path"
    return "other"


class Scanner:
    def __init__(self, allow_path, deny_path=None, rules=None):
        """`rules` is a RuleSet (default: core plus DEFAULT_PACKS). A configured allowlist or
        denylist file that is missing is a ValueError, never an empty list: a moved or
        misspelt denylist would otherwise let every name it holds through."""
        self.rules = rules = rules if rules is not None else RuleSet()
        if not allow_path.exists():
            raise ValueError("allowlist file %s does not exist" % allow_path)
        if deny_path and not deny_path.exists():
            raise ValueError("denylist file %s does not exist" % deny_path)
        raw = allow_path.read_bytes()
        spec = yaml.safe_load(raw) or {}
        self.staff = {str(x).lower() for x in (spec.get("staff") or [])}
        self.names = {str(x).lower() for x in (spec.get("names") or [])} | self.staff
        self.emails = [str(x).lower() for x in (spec.get("emails") or [])]
        # Staff or office numbers that may stay, compared as digits only.
        self.phones = {re.sub(r"\D", "", str(x))[-10:] for x in (spec.get("phones") or [])}
        self.pairing_on = bool(spec.get("anon_id_pairing", True)) and bool(rules.anon_res)
        # A fiction corpus (a campaign's NPCs) is mostly invented names, so its index may turn
        # off spaCy PERSON redaction and the pairing check. Secrets, emails, ids and the
        # denylist still apply, and refresh_all refuses this setting for a ferpa index.
        self.person_names = bool(spec.get("person_names", True))
        self.keep_literals = [str(x) for x in (spec.get("keep") or []) if str(x)]
        self.keep_res = []
        for pat in (spec.get("keep_patterns") or []):
            try:
                rx = re.compile(str(pat))
            except re.error as e:
                raise ValueError("keep_patterns: invalid regex %r: %s" % (str(pat), e))
            if rx.search(""):
                raise ValueError("keep_patterns: %r matches the empty string" % str(pat))
            covered = sum(m.end() - m.start() for m in rx.finditer(KEEP_SAMPLE))
            if covered > len(KEEP_SAMPLE) / 2:
                raise ValueError("keep_patterns: %r is too broad (matches over half of a sample text)"
                                 % str(pat))
            self.keep_res.append(rx)
        self.keep_lit_re = None
        if self.keep_literals:
            alts = []
            for lit in sorted(set(self.keep_literals), key=len, reverse=True):
                alts.append((r"(?<!\w)" if re.match(r"\w", lit) else "") + re.escape(lit) +
                            (r"(?!\w)" if re.search(r"\w$", lit) else ""))
            self.keep_lit_re = re.compile("|".join(alts), re.I)
        self.inst = rules.institution_words | {str(x).lower() for x in (spec.get("institution_words") or [])}
        deny_raw = b""
        self.deny = set()
        if deny_path:
            deny_raw = deny_path.read_bytes()
            self.deny = {ln.split()[0] for ln in deny_raw.decode().splitlines()
                         if ln.strip() and not ln.startswith("#")}
        # A change to the rules, the allowlist or the denylist invalidates everything scanned
        # under the old ones; callers compare this to the version stored with their state.
        # KEEP_SECRET_RULE joins only when keep literals exist, because only those scans
        # changed when secrets stopped being exemptable piecemeal: every other index keeps
        # its version and is not rescanned. The rule packs join through their fingerprint.
        self.version = hashlib.sha1(
            ("%d|" % SCAN_RULES_VERSION).encode() + raw + b"|" + deny_raw
            + (KEEP_SECRET_RULE if self.keep_literals else b"")
            + b"|rules:" + rules.fingerprint.encode()).hexdigest()[:12]

    def mask_keep(self, fields, store, cats, literals=True, patterns=True, count=True):
        """Swap kept spans for placeholders; `store` maps placeholder -> original text."""
        rxs = ([self.keep_lit_re] if literals and self.keep_lit_re else []) + \
              (self.keep_res if patterns else [])
        if not rxs:
            return fields
        out = []
        for f in fields:
            for rx in rxs:
                def fn(m):
                    ph = ph_name(len(store))
                    store[ph] = m.group(0)
                    if count:
                        c = "kept-" + secret_category(m.group(0), self.rules)
                        cats[c] = cats.get(c, 0) + 1
                    return ph
                f = rx.sub(fn, f)
            out.append(f)
        return out

    @staticmethod
    def restore_keep(fields, store):
        return [PH_RE.sub(lambda m: store.get(m.group(0), ""), f) for f in fields]

    def name_allowed(self, cand):
        low = cand.lower()
        if low in self.names:
            return True
        parts = [p.lower() for p in name_parts([t for t in re.split(r"[ -]", cand) if t])]
        return bool(parts) and all(p in self.names for p in parts)

    def institutional(self, cand):
        """True for a candidate made only of institutional and allowlisted words (and the
        particles between them, which are never redacted on their own)."""
        parts = [p.lower() for p in name_parts([t for t in re.split(r"[ -]", cand) if t])]
        return bool(parts) and all(p in self.inst or p in self.names for p in parts)

    @staticmethod
    def person_candidates(texts):
        """Distinct name-shaped PERSON entities per text, from spaCy."""
        pieces, owners = [], []
        for i, t in enumerate(texts):
            for piece in split_pieces(t):
                pieces.append(piece)
                owners.append(i)
        found = [set() for _ in texts]
        if not pieces:
            return [[] for _ in texts]
        for i, doc in zip(owners, nlp().pipe(pieces, batch_size=8)):
            for e in doc.ents:
                if e.label_ != "PERSON":
                    continue
                x = re.sub(r"'s$|\u2019s$", "", e.text.strip().strip("'\u2019").strip())
                if len(x) >= 3 and NAMEISH_RE.match(x):
                    found[i].add(x)
        return [sorted(f) for f in found]

    def scan_groups(self, groups, triage=None, chunk=100):
        """groups: list of lists of strings (the fields of one record or one document).
        Returns a list of (fields, cats, dropped_reason_or_None). Values are redacted in
        the returned copies; categories hold counts only. spaCy sees `chunk` groups at a
        time, which keeps peak memory flat (one call over a whole corpus passed 4 GB)."""
        out = []
        for i in range(0, len(groups), chunk):
            out += self._scan_chunk(groups[i:i + chunk], triage)
        return out

    def literal_covers(self, m, f):
        """True when one keep literal spans the whole of match `m` in field `f`. A secret
        rule's match is exempt only then: a literal that covers part of a secret (a known
        prefix, a fragment) must not leave the rest of it in the corpus."""
        if self.keep_lit_re is None:
            return False
        return any(k.start() <= m.start() and m.end() <= k.end()
                   for k in self.keep_lit_re.finditer(f))

    def _scan_chunk(self, groups, triage):
        has_keep = bool(self.keep_lit_re or self.keep_res)
        texts = ["\n\n".join(self.mask_keep(g, {}, {}, count=False) if has_keep else g)
                 for g in groups]
        cands = self.person_candidates(texts) if self.person_names else [[] for _ in texts]
        out = []
        for fields, names in zip(groups, cands):
            cats, fields = {}, list(fields)
            store = {}

            def sub_all(rx, repl, cat, pred=None):
                nonlocal fields
                new = []
                for f in fields:
                    def fn(m, f=f):
                        if pred and not pred(m, f):
                            return m.group(0)
                        cats[cat] = cats.get(cat, 0) + 1
                        return repl(m) if callable(repl) else repl
                    new.append(rx.sub(fn, f))
                fields = new

            # The secret rules run first, on unmasked text, so a keep literal can only exempt a
            # secret it covers entirely; keep_patterns never exempt a secret.
            not_kept = lambda m, f: not self.literal_covers(m, f)
            for rx in SECRET_PATTERNS:
                sub_all(rx, "[secret]", "secret", pred=not_kept)
            sub_all(B64_RE, "[secret]", "secret",
                    pred=lambda m, f: looks_secret_b64(m.group(0)) and not_kept(m, f))
            sub_all(B64URL_RE, "[secret]", "secret",
                    pred=lambda m, f: looks_secret_b64(m.group(0)) and not_kept(m, f))
            sub_all(QUERY_SECRET_RE, lambda m: m.group(1) + "[secret]", "secret", pred=not_kept)
            if has_keep:
                fields = self.mask_keep(fields, store, cats)
            sub_all(EMAIL_RE, "[email]", "email",
                    pred=lambda m, f: not email_allowed(m.group(0), self.emails))
            sub_all(PARTIAL_EMAIL_RE, "[email]", "email")
            sub_all(PHONE_RE, "[phone]", "phone",
                    pred=lambda m, f: re.sub(r"\D", "", m.group(0))[-10:] not in self.phones)
            sub_all(KEYED_ID_RE, lambda m: m.group(1) + "[id]", "keyed-id")
            for rx in self.rules.id_res:
                sub_all(rx, "[id]", "numeric-id")

            def id_pred(m, f):
                window = f[max(0, m.start() - 30):m.start()]
                return bool(self.rules.id_context_re.search(window)) and not ID_EXEMPT_RE.search(window)
            sub_all(NUM_RE, "[id]", "numeric-id", pred=id_pred)

            hits = [n for n in names if not self.name_allowed(n) and not self.institutional(n)]
            if triage is not None:
                for n in hits:
                    triage[n] = triage.get(n, 0) + 1
            joined = "\n".join(fields)
            pairing = False
            if self.pairing_on and self.person_names:
                for n in hits:
                    for m in re.finditer(name_rx(n), joined):
                        near = joined[max(0, m.start() - 80):m.end() + 80]
                        if any(rx.search(near) for rx in self.rules.anon_res):
                            pairing = True
                for rx in self.rules.pair_res:
                    for m in rx.finditer(joined):
                        if m.group(1) in hits:
                            pairing = True
            # A phrase that mixes institutional words with a name ("Academic Freedom",
            # "Dean Marigold Thistlewaite") keeps its institutional words; only the rest is
            # redacted, so the surrounding governance or course text stays searchable.
            for n in sorted(hits, key=len, reverse=True):
                toks = re.split(r"([ -])", n)
                if any(t.lower() in self.inst for t in toks[::2]):
                    kept = "".join(t if (i % 2 or t.lower() in self.inst or t.lower() in self.names)
                                   else NAME_MARK for i, t in enumerate(toks))
                    # spaCy often stops a titled name one token short ("Dean Marigold" from
                    # "Dean Marigold Thistlewaite"), so a following capitalized word that is
                    # neither institutional nor allowlisted is redacted with it.
                    def mixed(m, kept=kept):
                        tail = m.group(1) or ""
                        word = tail.split()[-1] if tail else ""
                        if word and word.lower() not in self.inst and word.lower() not in self.names:
                            return kept + " " + NAME_MARK
                        return kept + tail
                    sub_all(re.compile(name_rx(n) + r"( %s%s%s)?" % (NAME_GAP, NAME_WORD, NAME_END)),
                            mixed, "person-name")
                else:
                    sub_all(re.compile(name_rx(n, joined=True)), NAME_MARK, "person-name")
            if self.person_names:
                def titled(m):
                    words = m.group(2).split()
                    out = [w if (w.lower() in self.inst or w.lower() in self.names) else NAME_MARK
                           for w in words]
                    if out == words:
                        return m.group(0)
                    cats["person-name"] = cats.get("person-name", 0) + 1
                    return m.group(1) + " " + " ".join(out)
                fields = [TITLE_NAME_RE.sub(titled, f) for f in fields]
            # Each part of a spaced name is redacted wherever it appears; a glued "al-" part is
            # also redacted without its prefix. A particle never is.
            comps = {c for n in hits if " " in n for p in name_parts(n.split())
                     for c in {p, re.sub(r"^(?:al|el)-", "", p)}
                     if len(c) >= 3 and c.lower() not in self.names and c.lower() not in self.inst}
            for c in sorted(comps, key=len, reverse=True):
                sub_all(re.compile(name_rx(c)), NAME_MARK, "person-name")
            if self.deny:
                new = []
                for f in fields:
                    spans, taken = [], []
                    for m in APOS_TOKEN_RE.finditer(f):
                        tok = m.group(0)
                        forms = {tok, POSSESSIVE_RE.sub("", tok)}
                        if any(token_hash(x) in self.deny for x in forms):
                            spans.append((m.start(), m.end()))
                            taken.append((m.start(), m.end()))
                    for m in CAP_TOKEN_RE.finditer(f):
                        if any(a <= m.start() < b for a, b in taken):
                            continue
                        if token_hash(m.group(0)) in self.deny:
                            lead = LEAD_RE.search(f[max(0, m.start() - 40):m.start()])
                            st = m.start() - (len(lead.group(0)) if lead else 0)
                            if lead and lead.group(0).split()[0].lower() in self.staff:
                                st = m.start()
                            spans.append((st, m.end()))
                    for m in ALPHA_RUN_RE.finditer(f):
                        segs = [(m.start(), m.end())] + [
                            (m.start() + g.start(), m.start() + g.end())
                            for g in CAMEL_SEG_RE.finditer(m.group(0))]
                        for a, b in segs:
                            if any(a < e and s_ < b for s_, e in spans):
                                continue
                            if token_hash(f[a:b]) in self.deny:
                                spans.append((a, b))
                    for st, en in sorted(spans, reverse=True):
                        f = f[:st] + NAME_MARK + f[en:]
                        cats["person-name"] = cats.get("person-name", 0) + 1
                    new.append(f)
                fields = new
            # An anonymous id beside any name this scan redacted is a pairing too: spaCy often
            # misses the name the denylist or a split component caught ("L12 = Torvane").
            if self.pairing_on and self.person_names and not pairing:
                joined = "\n".join(fields)
                for m in re.finditer(re.escape(NAME_MARK), joined):
                    near = joined[max(0, m.start() - 80):m.end() + 80]
                    if any(rx.search(near) for rx in self.rules.anon_res):
                        pairing = True
                        break
            fields = [f.replace(NAME_MARK, "[name]") for f in fields]
            dropped = None
            if pairing:
                cats["anon-id-pairing"] = cats.get("anon-id-pairing", 0) + 1
                dropped = "anon-id-pairing"
            if self.rules.private_path_re and self.rules.private_path_re.search("\n".join(fields)):
                cats["private-path-mention"] = 1
            if store:
                fields = self.restore_keep(fields, store)
            out.append((fields, cats, dropped))
        return out

    def scan_records(self, recs, triage=None):
        """Redact session records in place (user_text, assistant); set r['scan'], r['dropped']."""
        res = self.scan_groups([[r["user_text"], r.get("sender_name") or ""] + r["assistant"]
                                for r in recs], triage)
        for r, (fields, cats, dropped) in zip(recs, res):
            r["user_text"], r["assistant"] = fields[0], fields[2:]
            if r.get("sender_name"):
                r["sender_name"] = fields[1]
            r["scan"] = cats
            if dropped:
                r["dropped"] = dropped
        return recs

    def scrub_tag(self, rec):
        """Scrub the model-written fields of a tag record (summary, topics): the model may
        echo a name from its input or infer one."""
        fields = [rec.get("summary") or ""] + list(rec.get("topics") or [])
        (out, _c, _d), = self.scan_groups([fields])
        rec["summary"], rec["topics"] = out[0], [t for t in out[1:] if t]
        rec["sv"] = self.version
        return rec

    def scan_text(self, text):
        (fields, cats, dropped), = self.scan_groups([[text]])
        return fields[0], cats, dropped
