"""The generic base64url secret rule against dated document names.

A name made of word-like segments ("2026-07-01_Faculty-Employment-Handbook_AY2026-27") is long
and mixes case and digits like a token, but it is not one, in a file name or in body text. Real
tokens keep at least one long random segment and stay [secret].

Every token here is synthetic and built at run time from a seeded generator, so no key-shaped
literal sits in the tree.
"""
import random
import string
import tempfile
import unittest
from pathlib import Path

from fleetwatch.memory import doc_corpus, privacy_scan

HANDBOOK = "2026-07-01_Faculty-Employment-Handbook_AY2026-27"
DOC_NAMES = [
    HANDBOOK,
    "2024-07-01_Procedural-Calendar_AY2024-25",
    "CS-Department-Guidelines_2025-08-15",
    "2025-01-15_Graduate-Student-Handbook_v2_Q3-Revisions",
]

URLSAFE = string.ascii_letters + string.digits + "-_"
STD = string.ascii_letters + string.digits + "+/"


def random_run(rng, n, alphabet=string.ascii_letters + string.digits):
    """n characters from alphabet with a lower, an upper and a digit in them."""
    while True:
        s = "".join(rng.choice(alphabet) for _ in range(n))
        if any(c.islower() for c in s) and any(c.isupper() for c in s) and \
                sum(c.isdigit() for c in s) >= max(4, n // 8):
            return s


def token_shapes():
    rng = random.Random(20261112)
    digits = lambda n: "".join(rng.choice(string.digits) for _ in range(n))
    return {
        "github": "ghp" + "_" + random_run(rng, 36),
        "sk-key": "sk" + "-" + random_run(rng, 48),
        "slack": "xo" + "xb-" + digits(12) + "-" + digits(13) + "-" + random_run(rng, 24),
        "jwt-segment": "eyJ" + random_run(rng, 60, string.ascii_letters + string.digits + "_-"),
        "base64url-43": random_run(rng, 43, URLSAFE),
        "aws-secret": random_run(rng, 40, STD),
    }


def generic_secret(s):
    """The generic rule alone: a whole-string base64 or base64url run that looks random."""
    return bool(privacy_scan.B64_RE.fullmatch(s) or privacy_scan.B64URL_RE.fullmatch(s)) and \
        privacy_scan.looks_secret_b64(s)


def scanner(tmp):
    p = Path(tmp) / "allow.yaml"
    p.write_text("version: 1\nperson_names: false\n")
    return privacy_scan.Scanner(p)


class GenericRuleOnDocumentNames(unittest.TestCase):
    def test_dated_document_names_are_not_secret(self):
        for name in DOC_NAMES:
            with self.subTest(name=name):
                self.assertFalse(generic_secret(name))
                self.assertFalse(privacy_scan.looks_secret_b64(name))

    def test_token_shapes_are_still_secret_under_the_generic_rule(self):
        for kind, tok in token_shapes().items():
            with self.subTest(kind=kind):
                self.assertTrue(generic_secret(tok), kind)

    def test_a_name_with_one_random_segment_is_still_secret(self):
        rng = random.Random(7)
        tok = "2026-07-01_Faculty-" + random_run(rng, 28)
        self.assertTrue(generic_secret(tok))

    def test_document_names_in_body_text_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            sc = scanner(tmp)
            for name in DOC_NAMES:
                with self.subTest(name=name):
                    text = "See %s for the policy." % name
                    out, cats, _ = sc.scan_text(text)
                    self.assertEqual(out, text)
                    self.assertNotIn("secret", cats)

    def test_tokens_in_body_text_are_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            sc = scanner(tmp)
            for kind, tok in token_shapes().items():
                with self.subTest(kind=kind):
                    out, cats, _ = sc.scan_text("the key %s was rotated" % tok)
                    self.assertNotIn(tok, out)
                    self.assertIn("[secret]", out)
                    self.assertGreaterEqual(cats.get("secret", 0), 1)


class DocumentNamesInTheCorpus(unittest.TestCase):
    def test_a_handbook_named_file_keeps_its_name_in_the_corpus_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            (src / "Policies_AY2026-27").mkdir(parents=True)
            (src / "Policies_AY2026-27" / (HANDBOOK + ".txt")).write_text("Section one.\n")
            res = doc_corpus.convert_docs([{"path": str(src), "include": ["**/*"], "label": "n"}],
                                          root / "corpus", root / "state" / "m.json", scanner(root))
            out = root / "corpus" / "n" / "Policies_AY2026-27" / (HANDBOOK + ".txt.md")
            self.assertTrue(out.exists(), sorted(str(p) for p in (root / "corpus").rglob("*")))
            self.assertTrue(out.read_text().startswith("# %s\n\n" % HANDBOOK))
            self.assertNotIn("secret", res["categories"])


if __name__ == "__main__":
    unittest.main()
