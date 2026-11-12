"""All-capital surnames are names; acronyms are not.

A surname written in capitals ("Jean DUPONT", "DUPONT, Jean") is redacted when it sits beside a
mixed-case name, a title or a roster comma, is on the Census surname list, and is not a known
acronym. An acronym ("Claude API", "MSU ITS") is never redacted, even in a text that also holds
a redacted name. The surnames here are among the most common in the US Census list; the given
names are common too.
"""
import hashlib
import re
import tempfile
import time
import unittest
from pathlib import Path

from fleetwatch.memory import privacy_scan


def scanner(tmp, body="version: 1\n", deny=()):
    allow = Path(tmp) / "allow.yaml"
    allow.write_text(body)
    deny_path = None
    if deny:
        deny_path = Path(tmp) / "deny.txt"
        deny_path.write_text("".join(privacy_scan.token_hash(d) + "\n" for d in deny))
    return privacy_scan.Scanner(allow, deny_path)


def spacy_or_skip(test):
    try:
        privacy_scan.nlp()
    except Exception as e:      # the memory extra is not installed
        test.skipTest("spaCy unavailable: %s" % type(e).__name__)


def words(text):
    return set(re.findall(r"[A-Za-z]+", text))


class AllCapitalSurnamesAreRedacted(unittest.TestCase):
    CASES = (
        ("Yesterday I met with Jean DUPONT", ["Jean", "DUPONT"]),
        ("Dr. Smith-JONES approved", ["Smith", "JONES"]),
        ("DUPONT, Jean asked", ["DUPONT", "Jean"]),
        ("Roster: DUPONT, Jean; GARCIA, Maria", ["DUPONT", "Jean", "GARCIA", "Maria"]),
        ("GARCIA, Maria Elena signed the form.", ["GARCIA", "Maria", "Elena"]),
    )

    def test_each_shape_leaves_no_part_of_the_name(self):
        spacy_or_skip(self)
        with tempfile.TemporaryDirectory() as t:
            sc = scanner(t)
            for text, parts in self.CASES:
                with self.subTest(text=text):
                    out, cats, _ = sc.scan_text(text)
                    self.assertEqual(sorted(words(out) & set(parts)), [], out)
                    self.assertTrue(cats.get("person-name"), cats)


class AcronymsAreLeftAlone(unittest.TestCase):
    TEXTS = ("We call the Claude API from the server.",
             "Store it in AWS S3 for now.",
             "MSU ITS runs the servers.",
             "NASA, Boeing and others joined.",
             "The Claude API and NASA both appear in the notes.",
             "Dr. Smith API keys rotate monthly.")

    def test_acronym_texts_come_back_unchanged(self):
        spacy_or_skip(self)
        with tempfile.TemporaryDirectory() as t:
            sc = scanner(t)
            for text in self.TEXTS:
                with self.subTest(text=text):
                    out, _, _ = sc.scan_text(text)
                    # Only a mixed-case name beside a title may change; every acronym stays.
                    for acr in re.findall(r"\b[A-Z]{2,}\d*\b", text):
                        self.assertIn(acr, out, out)
                    if not text.startswith("Dr."):
                        self.assertEqual(out, text)

    def test_a_lone_acronym_beside_a_redacted_name_stays(self):
        spacy_or_skip(self)
        text = "Jean DUPONT emailed today. The API is down, and the API docs say why."
        with tempfile.TemporaryDirectory() as t:
            out, _, _ = scanner(t).scan_text(text)
        self.assertNotIn("DUPONT", out)
        self.assertNotIn("Jean", out)
        self.assertEqual(out.count("API"), 2, out)

    def test_an_all_capital_surname_is_not_spread_through_the_text(self):
        spacy_or_skip(self)
        text = "Yesterday I met with Maria WHITE about the budget. The WHITE paper is due."
        spans = [e.text for e in privacy_scan.nlp()(text).ents if e.label_ == "PERSON"]
        if "Maria WHITE" not in spans:
            self.skipTest("spaCy tags %r on this model version" % spans)
        with tempfile.TemporaryDirectory() as t:
            out, _, _ = scanner(t).scan_text(text)
        self.assertNotIn("Maria", out)
        self.assertIn("The WHITE paper", out)


class TheNameFilter(unittest.TestCase):
    def test_an_all_capital_surname_beside_a_mixed_case_name_is_a_name(self):
        for n in ["Jean DUPONT", "Maria GARCIA-LOPEZ", "Jean-Paul DUPONT", "Mary J. SMITH",
                  "Sean O'BRIEN", "Ana de la CRUZ"]:
            self.assertTrue(privacy_scan.person_shaped(n), n)

    def test_acronyms_and_all_capital_spans_are_not_names(self):
        for x in ["Claude API", "Jordan JSON", "NASA", "USA FBI", "J. BRIMWELL", "GARCIA",
                  "DUPONT GARCIA", "Jean USA", "Jean IT", "Jean ZQXVW", "Jean DUPONT API",
                  "Jean D"]:
            self.assertFalse(privacy_scan.person_shaped(x), x)

    def test_the_stoplist_holds_every_acronym_the_rule_must_never_take(self):
        for acr in ("API AWS SSO ITS MSU NASA CS IT AI ML UI UX PR CI URL PDF SQL "
                    "USA CEO OS REST IP MAC RAM LAN ACS TA PI").split():
            self.assertIn(acr.lower(), privacy_scan.ACRONYM_STOPLIST, acr)
            self.assertFalse(privacy_scan.surname_ok(acr), acr)

    def test_the_census_list_loads_lazily_and_holds_common_surnames(self):
        privacy_scan._SURNAMES = None
        self.assertIsNone(privacy_scan._SURNAMES)
        self.assertTrue(privacy_scan.surname_ok("DUPONT"))
        self.assertIsNotNone(privacy_scan._SURNAMES)
        for s in ("smith", "garcia", "nguyen", "obrien"):
            self.assertIn(s, privacy_scan.surnames())
        self.assertGreater(len(privacy_scan.surnames()), 150_000)
        self.assertTrue(privacy_scan.surname_ok("O'BRIEN"))
        self.assertTrue(privacy_scan.surname_ok("M\u00dcLLER"))

    def test_a_title_takes_an_all_capital_surname_but_not_an_acronym(self):
        for text, want in (("Dr. Smith-JONES approved", " Smith-JONES"),
                           ("Prof. Ana GARCIA said", " Ana GARCIA")):
            m = privacy_scan.TITLE_NAME_RE.search(text)
            self.assertIsNotNone(m, text)
            self.assertEqual(m.group(2), want, text)
        self.assertFalse(privacy_scan.TITLE_NAME_RE.search("Dr. API"))
        self.assertFalse(privacy_scan.TITLE_NAME_RE.search("Dr. JONES"))


class DenylistInAnyCase(unittest.TestCase):
    def test_a_denylisted_surname_in_capitals_is_redacted(self):
        with tempfile.TemporaryDirectory() as t:
            sc = scanner(t, "version: 1\nperson_names: false\n", deny=["brimwell"])
            out, _, _ = sc.scan_text("We asked BRIMWELL about the API.")
        self.assertEqual(out, "We asked [name] about the API.")


class LinearTime(unittest.TestCase):
    def test_long_capital_runs_scan_in_linear_time(self):
        texts = [("Dr. Ab" + "-ABC" * 5000 + "x ") * 4,
                 ("Dr. Ab " + "A" * 20000 + "a ") * 2,
                 ("ABCDEF, Ab" + "Ab" * 5000 + "1 ") * 4,
                 ("A" * 20000 + ", " + "Ab " * 5000) * 2]
        t0 = time.time()
        for text in texts:
            privacy_scan.TITLE_NAME_RE.sub("x", text)
            privacy_scan.ROSTER_RE.sub("x", text)
        privacy_scan.person_shaped("Ab " + "ABC " * 20000 + "x")
        self.assertLess(time.time() - t0, 1.0)


class PackagedData(unittest.TestCase):
    def test_the_census_list_ships_as_package_data(self):
        import tomllib
        root = Path(__file__).resolve().parent.parent
        data = root / "fleetwatch" / "memory" / "data"
        self.assertTrue((data / "census_surnames_2010.txt.gz").is_file())
        self.assertTrue((data / "README.md").is_file())
        cfg = tomllib.loads((root / "pyproject.toml").read_text())
        self.assertIn("fleetwatch.memory.data", cfg["tool"]["setuptools"]["packages"])
        self.assertIn("*.gz", cfg["tool"]["setuptools"]["package-data"]["fleetwatch.memory.data"])


if __name__ == "__main__":
    unittest.main()
