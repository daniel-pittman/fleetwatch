"""Two scrubber fixes, each with its regression test.

1. A keep literal exempts a secret only when it covers the whole secret match: a literal that
   is a prefix or fragment of a token used to mask that part and leave the rest unredacted.
2. A document the scanner drops (an anonymous id paired with a name) is never written to
   the corpus; the manifest and the run report say why, as for a dropped exchange.

All secrets and names here are fabricated. Scanners run with person_names: false where the
test does not need spaCy.
"""
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

from fleetwatch.memory import doc_corpus, privacy_scan

TOKEN = "sk-ant-abcdefghijKLMNOPQRSTUVWX0123456789zz"


def scanner(tmp, body):
    p = Path(tmp) / ("allow-%s.yaml" % hashlib.sha1(body.encode()).hexdigest()[:8])
    p.write_text(body)
    return privacy_scan.Scanner(p)


class KeepLiteralsAndSecrets(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = t.name

    def test_partial_literal_does_not_exempt_a_secret(self):
        # The literal is word-bounded, so it can match a prefix that ends at a hyphen.
        sc = scanner(self.tmp, "version: 1\nperson_names: false\nkeep: ['sk-ant']\n")
        out, cats, _ = sc.scan_text("deploy key %s in the env" % TOKEN)
        self.assertNotIn("abcdefghijKLMNOPQRSTUVWX0123456789zz", out)
        self.assertIn("[secret]", out)
        self.assertEqual(cats.get("secret"), 1)

    def test_literal_inside_a_query_secret_does_not_exempt_it(self):
        sc = scanner(self.tmp, "version: 1\nperson_names: false\nkeep: ['Zq9']\n")
        url = "https://files.example.org/x?sig=AbCdEf0123-Zq9-GhIjKl4567MnOpQr89"
        out, _, _ = sc.scan_text("link " + url)
        self.assertIn("sig=[secret]", out)
        self.assertNotIn("Zq9", out)
        self.assertNotIn("GhIjKl4567", out)

    def test_literal_covering_the_whole_secret_keeps_it(self):
        sc = scanner(self.tmp, "version: 1\nperson_names: false\nkeep: ['%s']\n" % TOKEN)
        out, cats, _ = sc.scan_text("documented test key %s here" % TOKEN)
        self.assertIn(TOKEN, out)
        self.assertEqual(cats.get("kept-secret"), 1)
        self.assertNotIn("secret", cats)

    def test_literal_away_from_secrets_scans_as_before(self):
        # A keep literal that touches no secret changes nothing but its own span.
        text = ("Call 303-555-0142 about Room 21n. Token %s leaked; mail jdoe@example.org, "
                "student id 7731904." % TOKEN)
        plain = scanner(self.tmp, "version: 1\nperson_names: false\n")
        kept = scanner(self.tmp, "version: 1\nperson_names: false\nkeep: ['303-555-0142']\n")
        out_plain, _, _ = plain.scan_text(text)
        out_kept, cats, _ = kept.scan_text(text)
        self.assertEqual(out_kept, out_plain.replace("[phone]", "303-555-0142"))
        self.assertEqual(cats.get("kept-phone"), 1)

    def test_version_moves_only_for_indexes_with_keep_literals(self):
        # The version is the rules version, the lists and the rule-pack fingerprint; keep
        # literals add the whole-secret rule.
        fp = b"|rules:" + privacy_scan.RuleSet().fingerprint.encode()
        body = "version: 1\nemails: ['*@example.edu']\n"
        sc = scanner(self.tmp, body)
        old = hashlib.sha1(("%d|" % privacy_scan.SCAN_RULES_VERSION).encode() + body.encode()
                           + b"|" + b"" + fp).hexdigest()[:12]
        self.assertEqual(sc.version, old)
        body = "version: 1\nkeep: ['Level 6']\n"
        sc = scanner(self.tmp, body)
        old = hashlib.sha1(("%d|" % privacy_scan.SCAN_RULES_VERSION).encode() + body.encode()
                           + b"|" + b"" + fp).hexdigest()[:12]
        self.assertNotEqual(sc.version, old)

# Synthetic names in the shapes real names take: an inner apostrophe (straight and curly), an
# inner capital, a middle initial, an accented first letter, four tokens.
NAME_SHAPES = ["Sean O'Vashtel", "Sean O\u2019Vashtel", "Jane McTavorin", "LaQuennid Brimwell",
               "Mary J. Brimwell", "\u00d3scar Quellwyn", "Juan Carlos Vashtel Brimwell",
               "Marcus D'Quellano"]


class NameShapes(unittest.TestCase):
    """A PERSON span spaCy found is redacted whatever its shape. The filter that keeps
    name-shaped spans used to drop inner apostrophes and capitals, initials, accented
    capitals and four-token names, so those names reached the corpus whole."""

    def test_the_name_filter_keeps_every_real_name_shape(self):
        for n in NAME_SHAPES:
            self.assertTrue(privacy_scan.NAMEISH_RE.match(n), n)
        for n in ["Dr. McTavorin", "Dr. O\u2019Vashtel", "Prof. LaQuennid Brimwell"]:
            self.assertTrue(privacy_scan.TITLE_NAME_RE.search(n), n)
        self.assertFalse(privacy_scan.TITLE_NAME_RE.search("Dr. API"))

    def test_acronyms_and_lowercase_words_are_not_names(self):
        for x in ["NASA", "USA FBI", "J. K.", "the budget", "Claude API", "Jordan JSON", "OpenAPI", "SQLite"]:
            self.assertFalse(privacy_scan.NAMEISH_RE.match(x), x)

    def test_a_titled_name_before_a_possessive_apostrophe_is_matched_whole(self):
        for text, want in (("Dr. Brimwell' lab", " Brimwell"),
                           ("Dr. Anna Quellwyn\u2019 grant", " Anna Quellwyn"),
                           ("Dr. Smith-Quellwyn reviewed it", " Smith-Quellwyn")):
            m = privacy_scan.TITLE_NAME_RE.search(text)
            self.assertIsNotNone(m, text)
            self.assertEqual(m.group(2), want, text)

    def test_a_name_takes_the_name_parts_hyphened_on_after_it(self):
        rx = re.compile(privacy_scan.name_rx("Smith", joined=True))
        self.assertEqual(rx.search("Smith-Quellwyn reviewed it").group(0), "Smith-Quellwyn")
        # An all-capital part is not a name part: acronyms would be redacted with it.
        self.assertEqual(rx.search("Smith-API notes").group(0), "Smith")
        for n in ["Claude API", "Jordan JSON", "NASA", "MSU", "J. BRIMWELL"]:
            self.assertFalse(privacy_scan.NAMEISH_RE.match(n), n)

    def test_possessive_and_hyphened_names_are_redacted_in_text(self):
        try:
            privacy_scan.nlp()
        except Exception as e:      # the memory extra is not installed
            self.skipTest("spaCy unavailable: %s" % type(e).__name__)
        with tempfile.TemporaryDirectory() as t:
            sc = scanner(t, "version: 1\n")
            for text, leaks in (("Notes from Dr. Brimwell' lab came back.", ["Brimwell"]),
                                ("The grant went to Dr. Anna Quellwyn' group.", ["Anna", "Quellwyn"]),
                                ("Dr. Smith-Quellwyn reviewed the rubric.", ["Smith", "Quellwyn"]),
                                ("Sean O'Vashtel's notes are attached.", ["Sean", "Vashtel"])):
                with self.subTest(text=text):
                    if text.startswith("Sean"):
                        spans = [e.text for e in privacy_scan.nlp()(text).ents if e.label_ == "PERSON"]
                        if not any(leaks[-1] in x for x in spans):
                            self.skipTest("spaCy does not tag the name in %r" % text)
                    out, _, _ = sc.scan_text(text)
                    self.assertEqual([w for w in leaks if w in out], [], out)
            out, _, _ = sc.scan_text("NASA and MSU signed the agreement.")
            self.assertIn("NASA", out)
            self.assertIn("MSU", out)

    def test_a_long_lowercase_run_after_a_title_scans_in_linear_time(self):
        import time
        text = ("Dr. A" + "a" * 5000 + "1 ") * 4
        t0 = time.time()
        privacy_scan.TITLE_NAME_RE.sub("x", text)
        self.assertLess(time.time() - t0, 1.0)

    def test_a_name_spacy_found_leaves_no_token_in_the_text(self):
        try:
            privacy_scan.nlp()
        except Exception as e:      # the memory extra is not installed
            self.skipTest("spaCy unavailable: %s" % type(e).__name__)
        with tempfile.TemporaryDirectory() as t:
            sc = scanner(t, "version: 1\n")
            for n in NAME_SHAPES:
                text = "Yesterday I met with %s about the grant budget." % n
                with self.subTest(name=n):
                    spans = [e.text for e in privacy_scan.nlp()(text).ents if e.label_ == "PERSON"]
                    if n not in spans:
                        self.skipTest("spaCy does not tag %r on this model version" % n)
                    out, cats, _ = sc.scan_text(text)
                    left = [w for w in re.split(r"[ .'\u2019-]", n) if len(w) >= 3 and w in out]
                    self.assertEqual(left, [], out)
                    self.assertTrue(cats.get("person-name"))


# Synthetic names with a lowercase particle between capitalized parts, a glued al- prefix, and
# five tokens: spaCy tags each whole, and each used to fail the name filter and reach the corpus.
PARTICLE_NAMES = ["Maarten van der Quellwyn", "Ludovica di Vashtel", "Omar bin Quellano",
                  "Leila al-Brimwell", "Juan Carlos Vashtel Brimwell Ortez"]


def spacy_or_skip(test):
    try:
        privacy_scan.nlp()
    except Exception as e:      # the memory extra is not installed
        test.skipTest("spaCy unavailable: %s" % type(e).__name__)


class ParticleAndLongNames(unittest.TestCase):
    """A name with a closed-set lowercase particle (van, di, bin, al-) or up to six tokens is
    a name. A particle is never a name on its own: alone, first or last it is left in the
    text, and it is never redacted elsewhere as a component of a name."""

    def test_the_name_filter_keeps_particle_and_long_names(self):
        for n in PARTICLE_NAMES + ["Ana de la Vashtel", "Kees ter Quellwyn-Ortez", "Ana B. van Ortez",
                                   "Ana Bea Cora Dina Eva Fay"]:
            self.assertTrue(privacy_scan.NAMEISH_RE.match(n), n)

    def test_particles_alone_acronyms_and_institutions_are_not_names(self):
        for x in ["van", "de la", "van der", "al-Brimwell", "van Quellwyn", "Maarten van",
                  "J. van K.", "NASA", "USA de FBI", "Claude API", "Department of Computer Science",
                  "University of Colorado Denver",
                  "Ana Bea Cora Dina Eva Fay Gia", "Ana de la Bea Cora Dina Eva"]:   # seven tokens
            self.assertFalse(privacy_scan.NAMEISH_RE.match(x), x)

    def test_a_titled_particle_name_is_matched_whole(self):
        for text, want in (("Dr. Maarten van der Quellwyn said", " Maarten van der Quellwyn"),
                           ("Prof. Leila al-Brimwell' lab", " Leila al-Brimwell"),
                           ("Dr. Omar bin Quellano.", " Omar bin Quellano"),
                           ("Dr. Quellwyn van the end", " Quellwyn")):
            m = privacy_scan.TITLE_NAME_RE.search(text)
            self.assertIsNotNone(m, text)
            self.assertEqual(m.group(2), want, text)

    def test_the_hyphen_extension_takes_a_glued_particle_and_name(self):
        rx = re.compile(privacy_scan.name_rx("Vashtel", joined=True))
        self.assertEqual(rx.search("Vashtel-al-Brimwell wrote").group(0), "Vashtel-al-Brimwell")
        self.assertEqual(rx.search("Vashtel-de notes").group(0), "Vashtel")

    def test_particle_and_long_names_are_redacted_in_text(self):
        spacy_or_skip(self)
        with tempfile.TemporaryDirectory() as t:
            sc = scanner(t, "version: 1\n")
            for n in PARTICLE_NAMES:
                text = "Yesterday I met with %s about the grant budget." % n
                with self.subTest(name=n):
                    spans = [e.text for e in privacy_scan.nlp()(text).ents if e.label_ == "PERSON"]
                    if n not in spans:
                        self.skipTest("spaCy does not tag %r on this model version" % n)
                    out, cats, _ = sc.scan_text(text)
                    left = [w for w in re.split(r"[ -]", n) if re.search(r"\b%s\b" % w, out)]
                    self.assertEqual(left, [], out)
                    self.assertTrue(cats.get("person-name"))

    def test_a_long_particle_run_scans_in_linear_time(self):
        import time
        t0 = time.time()
        privacy_scan.TITLE_NAME_RE.sub("x", ("Dr. Ana " + "van " * 20000 + "x") * 2)
        privacy_scan.NAMEISH_RE.match("Ana " + "de " * 20000 + "x")
        self.assertLess(time.time() - t0, 1.0)

    def test_a_name_part_is_redacted_elsewhere_but_a_particle_is_not(self):
        spacy_or_skip(self)
        with tempfile.TemporaryDirectory() as t:
            sc = scanner(t, "version: 1\n")
            for n, part in (("Maarten van der Quellwyn", "Quellwyn"),
                            ("Leila al-Brimwell", "Brimwell")):
                text = ("Yesterday I met with %s about the grant budget. cc: %s. The van der "
                        "Waals term and the de la mode clause stay." % (n, part))
                with self.subTest(name=n):
                    spans = [e.text for e in privacy_scan.nlp()(text).ents if e.label_ == "PERSON"]
                    if spans != [n]:
                        self.skipTest("spaCy tags %r, not only %r, on this model version" % (spans, n))
                    out, _, _ = sc.scan_text(text)
                    self.assertNotIn(part, out)
                    for kept in ("The van der Waals", "de la mode"):
                        self.assertIn(kept, out)

    def test_institutions_and_acronyms_keep_their_behavior(self):
        spacy_or_skip(self)
        with tempfile.TemporaryDirectory() as t:
            sc = scanner(t, "version: 1\n")
            for text in ("The Claude API and NASA both appear in the Department of Computer "
                         "Science notes.",
                         "University of Colorado Denver hosted the van and de la meeting."):
                out, _, _ = sc.scan_text(text)
                self.assertEqual(out, text)


class CapitalizedParticleWords(unittest.TestCase):
    """A capitalized word that is also a particle ("Ben", "Della") is a given name: only a
    lowercase particle between two name tokens is a particle."""

    def test_the_given_name_is_redacted_where_it_appears_alone(self):
        spacy_or_skip(self)
        with tempfile.TemporaryDirectory() as t:
            sc = scanner(t, "version: 1\n")
            text = "Yesterday I met with Ben Quellano about the grant budget. see draft-Ben.docx"
            spans = [e.text for e in privacy_scan.nlp()(text).ents if e.label_ == "PERSON"]
            if spans != ["Ben Quellano"]:
                self.skipTest("spaCy tags %r, not only the full name, on this model version" % spans)
            out, _, _ = sc.scan_text(text)
            self.assertIsNone(re.search(r"\bBen\b|Quellano", out), out)

    def test_the_given_name_beside_an_institution_word_is_not_institutional(self):
        with tempfile.TemporaryDirectory() as t:
            sc = scanner(t, "version: 1\nperson_names: false\ninstitution_words: [brimwell]\n")
            self.assertFalse(sc.institutional("Della Brimwell"))
            self.assertTrue(sc.institutional("Senate de la Faculty"))

    def test_the_given_name_beside_an_institution_word_is_redacted_in_text(self):
        spacy_or_skip(self)
        with tempfile.TemporaryDirectory() as t:
            sc = scanner(t, "version: 1\ninstitution_words: [quellano]\n")
            text = "Della Quellano asked about the late policy."
            spans = [e.text for e in privacy_scan.nlp()(text).ents if e.label_ == "PERSON"]
            if "Della Quellano" not in spans:
                self.skipTest("spaCy does not tag the name on this model version")
            out, _, _ = sc.scan_text(text)
            self.assertNotIn("Della", out)

    def test_the_given_name_is_not_allowlisted_by_its_surname(self):
        with tempfile.TemporaryDirectory() as t:
            sc = scanner(t, "version: 1\nperson_names: false\nnames: [quellano]\n")
            self.assertFalse(sc.name_allowed("Ben Quellano"))
            self.assertTrue(sc.name_allowed("Quellano"))
        spacy_or_skip(self)
        with tempfile.TemporaryDirectory() as t:
            sc = scanner(t, "version: 1\nnames: [quellano]\n")
            text = "Yesterday I met with Ben Quellano about the grant budget."
            spans = [e.text for e in privacy_scan.nlp()(text).ents if e.label_ == "PERSON"]
            if "Ben Quellano" not in spans:
                self.skipTest("spaCy does not tag the name on this model version")
            out, _, _ = sc.scan_text(text)
            self.assertIsNone(re.search(r"\bBen\b", out), out)


class PairingWithAnyRedactedName(unittest.TestCase):
    """An anonymous id beside a name this scan redacted is a pairing, whichever rule redacted
    the name: a full spaCy span, the denylist, or one part of a full name found elsewhere."""

    PAD = " The rubric had four criteria and the late policy applied to every section this term."

    def scanner(self, root, deny=(), allow="version: 1\n"):
        (root / "allow.yaml").write_text(allow)
        (root / "deny.txt").write_text("".join(privacy_scan.token_hash(n) + "\n" for n in deny))
        (root / "anon.yaml").write_text(
            "anon_ids:\n  - pattern: '\\bL\\d{2}\\b'\n"
            "    pairing: '\\bL\\d{2}\\b\\s*(?:is|=)\\s+([A-Z][a-z]+(?:\\s+[A-Z][a-z]+)?)'\n")
        return privacy_scan.Scanner(root / "allow.yaml", root / "deny.txt",
                                    rules=privacy_scan.RuleSet([str(root / "anon.yaml")]))

    def test_an_id_beside_a_denylisted_name_is_dropped(self):
        try:
            privacy_scan.nlp()
        except Exception as e:      # the memory extra is not installed
            self.skipTest("spaCy unavailable: %s" % type(e).__name__)
        with tempfile.TemporaryDirectory() as t:
            sc = self.scanner(Path(t), deny=["Torvane"])
            out, _, dropped = sc.scan_text("L12 = Torvane (late)")
            self.assertNotIn("Torvane", out)
            self.assertEqual(dropped, "anon-id-pairing")

    def test_an_id_beside_one_part_of_a_name_found_far_away_is_dropped(self):
        try:
            privacy_scan.nlp()
        except Exception as e:      # the memory extra is not installed
            self.skipTest("spaCy unavailable: %s" % type(e).__name__)
        with tempfile.TemporaryDirectory() as t:
            sc = self.scanner(Path(t))
            text = "Jordan Thistlewaite asked about the extension." + self.PAD + " Later: L07 = Jordan."
            out, _, dropped = sc.scan_text(text)
            if "Thistlewaite" in out:
                self.skipTest("spaCy did not tag the synthetic name on this model version")
            self.assertEqual(dropped, "anon-id-pairing")

    def test_a_placeholder_already_in_the_text_is_not_a_pairing(self):
        with tempfile.TemporaryDirectory() as t:
            sc = self.scanner(Path(t), deny=["Torvane"], allow="version: 1\nperson_names: true\n")
            out, _, dropped = sc.scan_text("L12 = [name] (late)." + self.PAD * 2 + " Torvane wrote the rubric.")
            self.assertIsNone(dropped, out)

    def test_an_id_far_from_every_redacted_name_is_kept(self):
        with tempfile.TemporaryDirectory() as t:
            sc = self.scanner(Path(t), deny=["Torvane"])
            out, _, dropped = sc.scan_text("Torvane wrote the rubric." + self.PAD * 2 + " L12 passed.")
            self.assertIsNone(dropped, out)
            self.assertIn("[name] wrote", out)


class TopicsScrubbedBeforeNormalizing(unittest.TestCase):
    """The tagger's topics reach scrub_tag as the model wrote them. Lowercasing and stripping
    punctuation first turned an email, an id or a key into a shape no rule matches."""

    # The key is assembled here so the tree's own leak check does not read it as a real one.
    KEY = "AK" + "IA" + "QWERTYUIOPASDFGH"
    ANSWER = json.dumps({"result": json.dumps([{
        "id": "S1-EX-0001", "area": None, "kinds": [],
        "summary": "Regrade for QX-123456 approved.",
        "topics": ["QX-123456 regrade", "qvex@example.org", KEY, "Rubric Update"]}]),
        "modelUsage": {}, "total_cost_usd": 0})

    def test_a_private_string_in_a_topic_is_scrubbed_then_normalized(self):
        from unittest import mock

        from fleetwatch.memory import session_tagger as st
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "allow.yaml").write_text("version: 1\nperson_names: false\n")
            (d / "pack.yaml").write_text("id_patterns: ['\\bQX-\\d{6}\\b']\n")
            sc = privacy_scan.Scanner(d / "allow.yaml",
                                      rules=privacy_scan.RuleSet(["education", str(d / "pack.yaml")]))
            tc = st.TagConfig({"description": "x", "areas": {"a": "a"}, "kinds": {"k": "k"}})
            rec = dict(id="S1-EX-0001", sender_kind="user", sender_name="me", user_text="[id] regrade",
                       assistant=["done"], tools=[], ts="2026-11-01T00:00:00Z", hash="h")
            cache = {}
            done = mock.Mock(stdout=self.ANSWER, returncode=0)
            with mock.patch.object(st.subprocess, "run", return_value=done):
                st.tag_records([rec], cache, tc, d / "tags.jsonl", d / "usage.jsonl", 10, t, sc)
            topics = cache["S1-EX-0001"]["topics"]
            joined = " ".join(topics)
            for bad in ("qx-123456", "123456", "qvex", "akia"):
                self.assertNotIn(bad, joined)
            self.assertIn("rubric update", topics)
            self.assertEqual(topics, [x.lower() for x in topics])
            self.assertNotIn("qx-123456", (d / "tags.jsonl").read_text().lower())


class FlaggingScanner:
    """Drops any document containing PAIRME, the way the real scanner flags an anonymous id
    paired with a name."""
    version = "flag-v1"

    def scan_groups(self, groups, triage=None):
        out = []
        for g in groups:
            drop = "anon-id-pairing" if any("PAIRME" in f for f in g) else None
            cats = {"anon-id-pairing": 1} if drop else {}
            out.append((list(g), cats, drop))
        return out


class MissingListFiles(unittest.TestCase):
    """A configured allowlist or denylist that is missing stops the scanner instead of
    scanning with an empty list."""

    def test_a_missing_denylist_or_allowlist_is_an_error(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "allow.yaml").write_text("version: 1\n")
            with self.assertRaises(ValueError) as cm:
                privacy_scan.Scanner(root / "allow.yaml", root / "deny.txt")
            self.assertIn("denylist", str(cm.exception))
            with self.assertRaises(ValueError) as cm:
                privacy_scan.Scanner(root / "missing.yaml")
            self.assertIn("allowlist", str(cm.exception))
            (root / "deny.txt").write_text(privacy_scan.token_hash("Zorvath") + "\n")
            sc = privacy_scan.Scanner(root / "allow.yaml", root / "deny.txt")
            self.assertNotIn("Zorvath", sc.scan_text("Met Zorvath today.")[0])


class DroppedDocuments(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        root = Path(t.name)
        self.src = root / "notes"
        self.src.mkdir()
        self.corpus = root / "corpus" / "docs"
        self.manifest = root / "state" / "docs-manifest.json"
        self.specs = [{"path": str(self.src), "include": ["**/*.md"], "label": "notes"}]
        (self.src / "plain.md").write_text("# Plain\n\nNothing to see.\n")
        (self.src / "paired.md").write_text("# Paired\n\nPAIRME student A07 and a name.\n")

    def run_docs(self, **kw):
        return doc_corpus.convert_docs(self.specs, self.corpus, self.manifest, FlaggingScanner(), **kw)

    def test_dropped_document_is_not_written_and_is_recorded(self):
        res = self.run_docs()
        self.assertTrue((self.corpus / "notes" / "plain.md").exists())
        self.assertFalse((self.corpus / "notes" / "paired.md").exists())
        self.assertEqual(res["dropped"], ["notes/paired.md"])
        self.assertIn(("paired.md", "dropped: anon-id-pairing"), res["skipped"])
        self.assertEqual(res["categories"].get("anon-id-pairing"), 1)
        self.assertEqual(len(res["changed"]), 1)
        entry = json.loads(self.manifest.read_text())["files"][doc_corpus.manifest_key(self.src / "paired.md")]
        self.assertEqual((entry["out"], entry["skip"]), (None, "dropped: anon-id-pairing"))

        # Unchanged on the next run: still not written, still listed.
        res = self.run_docs()
        self.assertFalse((self.corpus / "notes" / "paired.md").exists())
        self.assertIn(("paired.md", "dropped: anon-id-pairing"), res["skipped"])
        self.assertEqual(res["changed"], [])

    def test_document_that_becomes_flagged_is_removed(self):
        self.run_docs()
        self.assertTrue((self.corpus / "notes" / "plain.md").exists())
        (self.src / "plain.md").write_text("# Plain\n\nNow PAIRME too.\n")
        res = self.run_docs(only=str(self.src / "plain.md"))
        self.assertFalse((self.corpus / "notes" / "plain.md").exists())
        self.assertEqual(res["dropped"], ["notes/plain.md"])
        self.assertTrue(res["removed"])

    def test_unselected_path_is_an_exception(self):
        with self.assertRaises(doc_corpus.NotSelected):
            self.run_docs(only=str(self.src.parent / "elsewhere.md"))


class HeadingFromFileStem(unittest.TestCase):
    """A document with no H1 is titled with its file stem; the stem is scanned like the body,
    and a stem with nothing to scrub gives the same bytes as before."""

    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.src = self.root / "src"
        self.src.mkdir()
        self.specs = [{"path": str(self.src), "include": ["**/*.md"], "label": "n"}]

    def convert(self, sc):
        return doc_corpus.convert_docs(self.specs, self.root / "corpus", self.root / "m.json", sc)

    def test_an_email_in_the_stem_is_redacted_in_the_heading(self):
        (self.src / "jdoe@example.org notes.md").write_text("Contact jdoe@example.org.\n")
        res = self.convert(scanner(self.root, "version: 1\nperson_names: false\n"))
        out = (self.root / "corpus" / "n" / "[email] notes.md").read_text()
        self.assertNotIn("jdoe@example.org", out)
        self.assertTrue(out.startswith("# [email] notes\n\n"), out)
        self.assertEqual(res["categories"].get("email"), 2)

    def test_a_clean_stem_and_an_existing_heading_are_unchanged(self):
        (self.src / "meeting-notes.md").write_text("Agenda and actions.\n")
        (self.src / "titled.md").write_text("# Own title\n\nBody.\n")
        self.convert(scanner(self.root, "version: 1\nperson_names: false\n"))
        self.assertEqual((self.root / "corpus" / "n" / "meeting-notes.md").read_text(),
                         "# meeting-notes\n\nAgenda and actions.\n")
        self.assertEqual((self.root / "corpus" / "n" / "titled.md").read_text(),
                         "# Own title\n\nBody.\n")

    def test_a_stem_the_scanner_drops_drops_the_document(self):
        (self.src / "PAIRME-list.md").write_text("Nothing in the body.\n")
        res = self.convert(FlaggingScanner())
        self.assertFalse((self.root / "corpus" / "n" / "PAIRME-list.md").exists())
        self.assertEqual(res["dropped"], ["n/PAIRME-list.md"])


def tree_text(root):
    """Every path under root and every file's text, for a no-leak assertion."""
    out = []
    for p in sorted(Path(root).rglob("*")):
        out.append(str(p.relative_to(root)))
        if p.is_file():
            out.append(p.read_text(errors="replace"))
    return "\n".join(out)


class FileAndFolderNames(unittest.TestCase):
    """File and folder names take the scan document text takes: the corpus path, the title,
    the manifest and the result carry only the scrubbed spelling."""

    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.src = self.root / "src"
        self.src.mkdir()
        self.specs = [{"path": str(self.src), "include": ["**/*"], "label": "n"}]
        self.sc = scanner(self.root, "version: 1\nperson_names: false\n")

    def convert(self, sc=None, **kw):
        return doc_corpus.convert_docs(self.specs, self.root / "corpus", self.root / "state" / "m.json",
                                       sc or self.sc, **kw)

    def test_a_folder_and_a_file_name_are_scrubbed_everywhere(self):
        (self.src / "jdoe@example.org").mkdir()
        (self.src / "jdoe@example.org" / "asmith@example.org plan.txt").write_text("Body.\n")
        (self.src / "jdoe@example.org" / "bwong@example.org notes.bin").write_text("x")
        res = self.convert()
        self.assertTrue((self.root / "corpus" / "n" / "[email]" / "[email] plan.txt.md").exists())
        everything = tree_text(self.root / "corpus") + tree_text(self.root / "state") + json.dumps(res)
        for leak in ("jdoe", "asmith", "bwong"):
            self.assertNotIn(leak, everything)
        self.assertIn(("[email] notes.bin", "unsupported type"), res["skipped"])
        self.assertEqual(res["changed"], ["n/[email]/[email] plan.txt.md"])

    def test_two_names_that_scrub_alike_get_distinct_paths(self):
        (self.src / "jdoe@example.org notes.md").write_text("One.\n")
        (self.src / "asmith@example.org notes.md").write_text("Two.\n")
        self.convert()
        names = sorted(p.name for p in (self.root / "corpus" / "n").iterdir())
        self.assertEqual(names, ["[email] notes (2).md", "[email] notes.md"])

    def test_a_clean_name_keeps_its_exact_spelling(self):
        (self.src / "Weekly_status.v2 notes.txt").write_text("Body.\n")
        self.convert()
        self.assertTrue((self.root / "corpus" / "n" / "Weekly_status.v2 notes.txt.md").exists())

    def test_a_manifest_keyed_by_path_is_rekeyed_and_the_old_file_swept(self):
        src = self.src / "jdoe@example.org notes.md"
        src.write_text("Body.\n")
        st = src.stat()
        old = self.root / "corpus" / "n" / "jdoe@example.org notes.md"
        old.parent.mkdir(parents=True)
        old.write_text("# jdoe@example.org notes\n\nBody.\n")
        (self.root / "state").mkdir()
        (self.root / "state" / "m.json").write_text(json.dumps(dict(
            _scan_version=self.sc.version, _conv=doc_corpus.options_key(dict(doc_corpus.DEFAULT_OPTS)),
            files={str(src): dict(mtime=st.st_mtime, size=st.st_size, sha1=doc_corpus.sha1_file(src),
                                  out="n/jdoe@example.org notes.md", scan={}, text_sha="x")})))
        self.convert()
        self.assertFalse(old.exists())
        self.assertTrue((self.root / "corpus" / "n" / "[email] notes.md").exists())
        self.assertNotIn("jdoe", tree_text(self.root / "corpus") + tree_text(self.root / "state"))

    def deny_scanner(self, *names, extra=""):
        (self.root / "deny.txt").write_text("".join(privacy_scan.token_hash(n) + "\n" for n in names))
        (self.root / "allow-d.yaml").write_text("version: 1\nperson_names: false\n" + extra)
        return privacy_scan.Scanner(self.root / "allow-d.yaml", self.root / "deny.txt")

    def test_a_name_after_the_last_dot_of_an_unknown_type_is_scrubbed(self):
        (self.src / "Report.Torvane").write_text("x")
        res = self.convert(self.deny_scanner("Torvane"))
        self.assertNotIn("Torvane", json.dumps(res) + tree_text(self.root / "state"))
        self.assertIn(("Report.[name]", "unsupported type"), res["skipped"])

    def test_a_one_file_run_applies_new_rules_to_that_file(self):
        (self.src / "plan.txt").write_text("Torvane will review the plan.\n")
        self.convert()
        self.assertIn("Torvane", tree_text(self.root / "corpus"))
        self.convert(self.deny_scanner("Torvane"), only=str(self.src / "plan.txt"))
        self.assertNotIn("Torvane", tree_text(self.root / "corpus"))

    def test_a_one_file_run_sweeps_the_files_old_spelling(self):
        (self.src / "Torvane plan.txt").write_text("Body.\n")
        self.convert()
        self.convert(self.deny_scanner("Torvane"), only=str(self.src / "Torvane plan.txt"))
        self.assertEqual(sorted(p.name for p in (self.root / "corpus" / "n").iterdir()),
                         ["[name] plan.txt.md"])

    def test_a_clean_name_never_shares_a_scrubbed_files_path(self):
        # Whichever order the hashed keys put them in, the clean name keeps its spelling and
        # the scrubbed one is numbered.
        for i in range(6):
            (self.src / ("[name] plan %d.md" % i)).write_text("Clean %d.\n" % i)
            (self.src / ("Torvane plan %d.md" % i)).write_text("Scrubbed %d.\n" % i)
        self.convert(self.deny_scanner("Torvane"))
        corpus = self.root / "corpus" / "n"
        for i in range(6):
            self.assertIn("Clean %d." % i, (corpus / ("[name] plan %d.md" % i)).read_text())
            self.assertIn("Scrubbed %d." % i, (corpus / ("[name] plan %d (2).md" % i)).read_text())

    def test_a_name_and_id_in_a_protected_style_folder_are_scrubbed(self):
        try:
            privacy_scan.nlp()
        except Exception as e:      # the memory extra is not installed
            self.skipTest("spaCy unavailable: %s" % type(e).__name__)
        (self.root / "pack.yaml").write_text(
            "id_patterns: ['\\b900\\d{6}\\b']\n"
            "anon_ids:\n  - pattern: '\\bL\\d{2}\\b'\n"
            "    pairing: '\\bL\\d{2}\\b\\s*(?:is|=)\\s+([A-Z][a-z]+(?:\\s+[A-Z][a-z]+)?)'\n")
        (self.root / "allow.yaml").write_text("version: 1\n")
        sc = privacy_scan.Scanner(self.root / "allow.yaml",
                                  rules=privacy_scan.RuleSet(["education", str(self.root / "pack.yaml")]))
        (self.src / "Advising").mkdir()
        (self.src / "Advising" / "Margaret Thistlewaite 900123456 probation.txt").write_text(
            "Course schedule and credits for next term.\n")
        res = self.convert(sc)
        everything = tree_text(self.root / "corpus") + tree_text(self.root / "state") + json.dumps(res)
        for leak in ("Thistlewaite", "Margaret", "900123456"):
            self.assertNotIn(leak, everything)
        self.assertEqual(len(res["changed"]), 1)
        self.assertTrue(res["changed"][0].startswith("n/Advising/"), res["changed"])


class SymlinksOutOfTheFolder(unittest.TestCase):
    def test_a_link_out_of_the_docs_folder_is_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "outside").mkdir()
            (root / "outside" / "private.md").write_text("Private notes from elsewhere.\n")
            (root / "src" / "inner").mkdir(parents=True)
            (root / "src" / "own.md").write_text("Own notes.\n")
            (root / "src" / "linked.md").symlink_to(root / "outside" / "private.md")
            (root / "src" / "inner" / "same.md").symlink_to(root / "src" / "own.md")
            (root / "src" / "dirlink").symlink_to(root / "outside")
            res = doc_corpus.convert_docs(
                [{"path": str(root / "src"), "include": ["**/*.md", "dirlink/*.md"], "label": "n"}],
                root / "corpus", root / "m.json", scanner(root, "version: 1\nperson_names: false\n"))
            written = sorted(str(p.relative_to(root / "corpus")) for p in (root / "corpus").rglob("*.md"))
            self.assertEqual(written, ["n/inner/same.md", "n/own.md"])
            self.assertEqual(sorted(n for n, why in res["skipped"] if why == "symlink outside the folder"),
                             ["linked.md", "private.md"])


class RealScannerPairing(unittest.TestCase):
    """The same path end to end with the real scanner and spaCy, when spaCy is installed."""

    def test_pairing_document_is_dropped(self):
        try:
            privacy_scan.nlp()
        except Exception as e:      # the memory extra is not installed
            self.skipTest("spaCy unavailable: %s" % type(e).__name__)
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "src").mkdir()
            (root / "src" / "roster-note.md").write_text(
                "# Grading note\n\nL07 is Jordan Smith, and L07 = Jordan in the sheet.\n")
            (root / "allow.yaml").write_text("version: 1\n")
            # Anonymous ids come from a pack; this synthetic one names "L" plus two digits.
            (root / "anon.yaml").write_text(
                "anon_ids:\n  - pattern: '\\bL\\d{2}\\b'\n"
                "    pairing: '\\bL\\d{2}\\b\\s*(?:is|=)\\s+([A-Z][a-z]+(?:\\s+[A-Z][a-z]+)?)'\n")
            sc = privacy_scan.Scanner(root / "allow.yaml",
                                      rules=privacy_scan.RuleSet([str(root / "anon.yaml")]))
            res = doc_corpus.convert_docs([{"path": str(root / "src"), "label": "n"}],
                                          root / "corpus", root / "m.json", sc)
            if not res["dropped"]:
                self.skipTest("spaCy did not tag the synthetic name on this model version")
            self.assertFalse((root / "corpus" / "n" / "roster-note.md").exists())


if __name__ == "__main__":
    unittest.main()
