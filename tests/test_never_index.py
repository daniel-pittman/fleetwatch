"""Documents under a never-index folder are skipped on every refresh, and a docs label cannot
leave the corpus folder."""
import contextlib
import io
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fleetwatch.memory import doc_corpus, privacy_scan, refresh


class PassScanner:
    version = "pass"

    def __init__(self, private_path_re=None):
        self.rules = SimpleNamespace(private_path_re=private_path_re)

    def scan_groups(self, groups, triage=None):
        return [(list(g), {}, None) for g in groups]


def write(path, text="# Note\n\nA note about lanterns.\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class NeverIndexFilesBelowTheRoot(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.src = self.root / "src"
        self.corpus = self.root / "corpus"
        self.manifest = self.root / "state" / "m.json"
        write(self.src / "notes" / "a.md")
        write(self.src / "grades" / "b.md")
        write(self.src / "roster-exports" / "c.md")
        write(self.src / "notes" / "Instructor_Only" / "d.md")
        write(self.src / "notes" / "grading-rubric.md")

    def convert(self, scanner=None, label="docs"):
        return doc_corpus.convert_docs([{"path": str(self.src), "include": ["**/*.md"], "label": label}],
                                       self.corpus, self.manifest, scanner or PassScanner())

    def corpus_files(self):
        return sorted(str(p.relative_to(self.corpus)) for p in self.corpus.rglob("*.md"))

    def test_only_files_outside_never_index_folders_are_indexed(self):
        res = self.convert()
        self.assertEqual(self.corpus_files(), ["docs/notes/a.md", "docs/notes/grading-rubric.md"])
        self.assertEqual(res["never_index"], 3)

    def test_a_rule_pack_private_path_applies_below_the_root(self):
        write(self.src / "secretcourse-2026" / "e.md")
        res = self.convert(PassScanner(re.compile("secretcourse", re.I)))
        self.assertNotIn("docs/secretcourse-2026/e.md", self.corpus_files())
        self.assertEqual(res["never_index"], 4)

    def test_a_rule_pack_private_path_still_applies_to_a_file_name(self):
        write(self.src / "notes" / "secretcourse-plan.md")
        self.convert(PassScanner(re.compile("secretcourse", re.I)))
        self.assertNotIn("docs/notes/secretcourse-plan.md", self.corpus_files())

    def test_never_index_words_match_compound_folder_names_by_word_start(self):
        for bad in ("FinalGrades", "grades2026", "gradesheets"):
            write(self.src / bad / "x.md")
        for ok in ("upgrade-notes", "graduate"):
            write(self.src / ok / "y.md")
        self.convert()
        self.assertEqual(self.corpus_files(), ["docs/graduate/y.md", "docs/notes/a.md",
                                               "docs/notes/grading-rubric.md",
                                               "docs/upgrade-notes/y.md"])
        for name in ("FinalGrades", "grades2026", "gradesheets", "FinalGrades2026", "XMLRoster"):
            self.assertIsNotNone(doc_corpus.folder_word(name), name)
        for name in ("upgrade-notes", "graduate", "Notes", "2026"):
            self.assertIsNone(doc_corpus.folder_word(name), name)

    def test_a_file_named_for_a_record_is_skipped_by_its_first_word_only(self):
        skipped = ("roster.md", "Gradebook2026.md", "grades-fall.md", "identity-docs.md",
                   "InstructorNotes.md")
        kept = ("grading-rubric.md", "feedback-on-grades.md", "upgrade-notes.md", "grading-workflow.md")
        for name in skipped + kept:
            write(self.src / "notes" / name)
        res = self.convert()
        files = self.corpus_files()
        for name in skipped:
            self.assertNotIn("docs/notes/" + name, files)
        for name in kept:
            self.assertIn("docs/notes/" + name, files)
        self.assertEqual(res["never_index"], 3 + len(skipped))
        for name in skipped:
            self.assertIsNotNone(doc_corpus.file_word(name), name)
        for name in kept + ("notes.md", "2026-roster.md"):
            self.assertIsNone(doc_corpus.file_word(name), name)

    def test_a_private_path_rule_with_a_separator_applies_below_and_at_the_root(self):
        rx = re.compile("blind/|exports/restricted/", re.I)
        write(self.src / "blind" / "e.md")
        write(self.src / "exports" / "restricted" / "f.md")
        write(self.src / "exports" / "open" / "g.md")
        (self.src / "innocent.md").symlink_to(self.src / "blind" / "e.md")
        self.convert(PassScanner(rx))
        self.assertEqual(self.corpus_files(), ["docs/exports/open/g.md", "docs/notes/a.md",
                                               "docs/notes/grading-rubric.md"])
        for root in (self.src / "blind", self.src / "exports" / "restricted"):
            with self.subTest(root=root):
                found = list(doc_corpus.discover({"path": str(root), "label": "x"}, private_path_re=rx))
                self.assertEqual(found, [])

    def test_a_zero_width_rule_match_counts_as_a_match(self):
        rx = re.compile("(?=secretcourse)", re.I)
        write(self.src / "secretcourse" / "e.md")
        self.convert(PassScanner(rx))
        self.assertNotIn("docs/secretcourse/e.md", self.corpus_files())
        self.assertTrue(doc_corpus.never_index_hit(["secretcourse"], rx, is_file=False))

    def test_a_never_index_root_or_a_root_link_into_one_indexes_nothing(self):
        link = self.root / "current-notes"
        link.symlink_to(self.src / "grades")
        for root in (self.src / "grades", link):
            with self.subTest(root=root):
                never = []
                found = list(doc_corpus.discover({"path": str(root), "label": "x"}, never=never))
                self.assertEqual(found, [])
                self.assertEqual(len(never), 1)

    def test_a_root_listed_under_a_never_index_name_indexes_nothing(self):
        link = self.root / "grades-now"
        link.symlink_to(self.src / "notes")
        self.assertEqual(list(doc_corpus.discover({"path": str(link), "label": "x"})), [])

    def test_an_existing_copy_under_a_never_index_root_is_removed(self):
        spec = [{"path": str(self.src / "grades"), "include": ["**/*.md"], "label": "g"}]
        with mock.patch.object(doc_corpus, "NEVER_INDEX_WORDS", frozenset()):
            doc_corpus.convert_docs(spec, self.corpus, self.manifest, PassScanner())
        self.assertEqual(self.corpus_files(), ["g/b.md"])
        res = doc_corpus.convert_docs(spec, self.corpus, self.manifest, PassScanner())
        self.assertEqual(self.corpus_files(), [])
        self.assertIn("g/b.md", res["removed"])

    def test_an_existing_corpus_copy_is_removed_on_the_next_refresh(self):
        with mock.patch.object(doc_corpus, "NEVER_INDEX_WORDS", frozenset()):
            self.convert()
        self.assertIn("docs/grades/b.md", self.corpus_files())
        res = self.convert()
        self.assertEqual(self.corpus_files(), ["docs/notes/a.md", "docs/notes/grading-rubric.md"])
        self.assertIn("docs/grades/b.md", res["removed"])

    def test_a_symlink_into_a_never_index_folder_is_not_indexed_through_its_name(self):
        (self.src / "innocent.md").symlink_to(self.src / "grades" / "b.md")
        self.convert()
        self.assertNotIn("docs/innocent.md", self.corpus_files())

    def test_the_refresh_report_counts_them_once(self):
        info = dict(doc_never_index=4, doc_skipped=[["one.md", "unsupported type"]])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            refresh.print_docs_detail("x", info)
        self.assertEqual(out.getvalue().count("skipped 4 files by the never-index rule (folder or file name)"), 1)

    def test_one_skipped_file_is_one_file(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            refresh.print_docs_detail("x", dict(doc_never_index=1))
        self.assertIn("skipped 1 file by the never-index rule (folder or file name)", out.getvalue())


class LabelsStayInsideTheCorpus(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.src = self.root / "src"
        write(self.src / "a.md")
        self.corpus = self.root / "corpus" / "docs"

    def convert(self, label):
        return doc_corpus.convert_docs([{"path": str(self.src), "include": ["*.md"], "label": label}],
                                       self.corpus, self.root / "state" / "m.json", PassScanner())

    def written(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*.md")
                      if self.src not in p.parents)

    def test_a_traversing_label_writes_nothing_outside_the_corpus(self):
        for label in ("../x", "../../x", "/tmp/claude-label-escape", "a/b", "..", "."):
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    self.convert(label)
                self.assertEqual(self.written(), [])
                self.assertFalse((self.root / "x").exists())

    def test_a_plain_label_still_converts(self):
        self.convert("notes")
        self.assertEqual(self.written(), ["corpus/docs/notes/a.md"])

    def test_a_destination_computed_outside_the_corpus_is_never_written(self):
        real = doc_corpus.corpus_names

        def escaping(scanner, found):
            return {k: dict(v, out=Path("../escape.md")) for k, v in real(scanner, found).items()}

        with mock.patch.object(doc_corpus, "corpus_names", escaping):
            with self.assertRaises(ValueError):
                self.convert("notes")
        self.assertFalse((self.root / "corpus" / "escape.md").exists())
        self.assertEqual(self.written(), [])

    def test_a_linked_corpus_folder_is_neither_written_nor_swept(self):
        peer = self.root / "peer" / "corpus" / "docs"
        write(peer / "notes" / "theirs.md")
        for link_at, target in ((self.root / "a" / "corpus" / "docs", peer),
                                (self.root / "b" / "corpus", peer.parent)):
            with self.subTest(link=link_at):
                link_at.parent.mkdir(parents=True, exist_ok=True)
                link_at.symlink_to(target)
                corpus = link_at if link_at.name == "docs" else link_at / "docs"
                kw = {} if link_at.name == "docs" else {"root": link_at.parent}
                for include in ("*.md", "*.none"):      # a run that writes nothing still sweeps
                    with self.assertRaises(ValueError):
                        doc_corpus.convert_docs([{"path": str(self.src), "include": [include],
                                                  "label": "mine"}],
                                                corpus, self.root / "state" / "m.json", PassScanner(), **kw)
                self.assertEqual(sorted(p.name for p in peer.rglob("*.md")), ["theirs.md"])

    def test_a_linked_folder_inside_the_corpus_is_refused_before_resolving(self):
        (self.corpus / "elsewhere").mkdir(parents=True)
        (self.corpus / "notes").symlink_to(self.corpus / "elsewhere")
        with self.assertRaises(ValueError):
            doc_corpus.safe_corpus_path(self.corpus, "notes/a.md")
        with self.assertRaises(ValueError):
            self.convert("notes")
        self.assertEqual(list((self.corpus / "elsewhere").iterdir()), [])

    def test_a_destination_that_resolves_outside_the_corpus_is_refused(self):
        with self.assertRaises(ValueError):
            doc_corpus.safe_corpus_path(self.corpus, "../escape.md")
        with self.assertRaises(ValueError):
            doc_corpus.safe_corpus_path(self.corpus, "/etc/escape.md")
        (self.corpus / "link").parent.mkdir(parents=True, exist_ok=True)
        (self.corpus / "link").symlink_to(self.root)
        with self.assertRaises(ValueError):
            doc_corpus.safe_corpus_path(self.corpus, "link/escape.md")
        self.assertEqual(doc_corpus.safe_corpus_path(self.corpus, "ok/a.md"),
                         self.corpus.resolve() / "ok" / "a.md")


if __name__ == "__main__":
    unittest.main()
