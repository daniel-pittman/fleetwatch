"""The repository is public: no tracked file may carry personal or secret material.

Set FLEETWATCH_LEAK_TERMS to a text file of private terms (one per line) to add
your own names and paths to the check without the list entering the repository.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import leak_check  # noqa: E402


def _in_git_checkout():
    if not shutil.which("git"):
        return False
    probe = subprocess.run(["git", "-C", ROOT, "rev-parse", "--is-inside-work-tree"], capture_output=True)
    return probe.returncode == 0


class TreeIsClean(unittest.TestCase):
    @unittest.skipUnless(_in_git_checkout(), "needs a git checkout")
    def test_tracked_files_hold_no_personal_or_secret_material(self):
        found = leak_check.scan_tree(ROOT, leak_check.load_terms())
        lines = ["%s:%s %s" % (path, n, cat) for path, hits in sorted(found.items()) for cat, n, _ in hits]
        self.assertEqual(lines, [], "leak check hits:\n" + "\n".join(lines))


class DetectorCatchesEachCategory(unittest.TestCase):
    """The samples are assembled from parts so this file does not trip its own check."""

    def cats(self, text, terms=()):
        return {c for c, _, _ in leak_check.scan_text(text, terms)}

    def test_home_paths(self):
        self.assertIn("home path", self.cats("/" + "Users/" + "realperson/notes"))
        self.assertIn("home path", self.cats("cd /" + "home/" + "realperson"))
        self.assertEqual(self.cats("/" + "Users/" + "alex/project and /" + "home/" + "user/x"), set())

    def test_project_slug_form_of_a_home_path(self):
        self.assertIn("home path", self.cats("projects/-" + "Users-" + "realperson-git-repo"))
        self.assertIn("home path", self.cats("-" + "home-" + "realperson-src"))
        self.assertEqual(self.cats("-" + "Users-" + "alex-git and -" + "home-" + "user-x and re-Users-x"), set())

    def test_email_addresses(self):
        self.assertIn("email", self.cats("write to " + "pat" + "@" + "university.edu"))
        self.assertEqual(self.cats("pat" + "@" + "example.com, noreply" + "@" + "anthropic.com"), set())
        self.assertEqual(self.cats("react" + "@" + "18.3.1"), set())
        # a company domain is not safe as a whole, only its noreply address
        self.assertIn("email", self.cats("pat" + "@" + "anthropic.com"))

    def test_cloud_drive_fragments(self):
        self.assertIn("cloud drive path", self.cats("~/Library/" + "Cloud" + "Storage/x"))
        self.assertIn("cloud drive path", self.cats("Google " + "Drive/notes"))

    def test_secret_shapes(self):
        self.assertIn("private key block", self.cats("-----BEGIN " + "RSA PRIVATE KEY-----"))
        self.assertIn("GitHub token", self.cats("ghp" + "_" + "a" * 36))
        self.assertIn("AWS access key id", self.cats("AKIA" + "A" * 16))
        self.assertIn("Slack token", self.cats("xox" + "b-" + "1234567890-abcdef"))

    def test_private_terms_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "terms.txt")
            with open(path, "w") as fh:
                fh.write("# comment\nSecretName\n\n")
            terms = leak_check.load_terms(path)
        self.assertEqual(terms, ["secretname"])
        self.assertIn("private term", self.cats("the secretNAME was here", terms))
        self.assertEqual(self.cats("nothing to see", terms), set())

    def test_allowlist_is_scoped_to_path_and_excerpt(self):
        self.assertTrue(leak_check.allowed("fleetwatch/resume.py", "cloud drive path", "One" + "Drive"))
        self.assertFalse(leak_check.allowed("fleetwatch/app.py", "cloud drive path", "One" + "Drive"))
        self.assertFalse(leak_check.allowed("fleetwatch/memory/privacy_scan.py", "email", "other" + "@" + "x.org"))



@unittest.skipUnless(shutil.which("git"), "needs git")
class HistoryIsScanned(unittest.TestCase):
    """--history finds what the commits being pushed add, even when a later commit removes it,
    and the author and committer addresses."""

    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.repo = t.name
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="Dev", GIT_COMMITTER_NAME="Dev",
                        GIT_AUTHOR_EMAIL="dev" + "@" + "example.com",
                        GIT_COMMITTER_EMAIL="dev" + "@" + "example.com")
        self.git("init", "-q", "-b", "main")

    def git(self, *args, env=None):
        return subprocess.run(["git", "-C", self.repo] + list(args), check=True, capture_output=True,
                              text=True, env=env or self.env).stdout.strip()

    def commit(self, name, text, env=None):
        with open(os.path.join(self.repo, name), "w") as fh:
            fh.write(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change " + name, env=env)
        return self.git("rev-parse", "HEAD")

    def test_a_line_added_then_deleted_is_found(self):
        self.commit("a.txt", "clean\n")
        bad = self.commit("a.txt", "clean\nsee /" + "Users/" + "realperson/notes\n")
        self.commit("a.txt", "clean\n")
        self.assertEqual(leak_check.scan_tree(self.repo), {})
        found = leak_check.scan_history(self.repo, "HEAD")
        self.assertEqual(list(found), ["commit %s a.txt" % bad[:12]])
        self.assertEqual(found["commit %s a.txt" % bad[:12]][0][:2], ("home path", 2))
        # a range that leaves the commit out does not report it
        self.assertEqual(leak_check.scan_history(self.repo, bad + "..HEAD"), {})

    def test_author_and_committer_addresses(self):
        self.commit("a.txt", "x\n")
        env = dict(self.env, GIT_AUTHOR_EMAIL="pat" + "@" + "university.edu",
                   GIT_COMMITTER_EMAIL="noreply" + "@" + "anthropic.com")
        sha = self.commit("b.txt", "y\n", env=env)
        found = leak_check.scan_history(self.repo, "HEAD")
        self.assertEqual([c for c, _, _ in found["commit " + sha[:12]]], ["author email"])

    def test_a_private_term_in_a_path_is_found(self):
        sha = self.commit("secretname-notes.txt", "x\n")
        found = leak_check.scan_history(self.repo, "HEAD", ["secretname"])
        self.assertIn("commit %s secretname-notes.txt" % sha[:12], found)


if __name__ == "__main__":
    unittest.main()
