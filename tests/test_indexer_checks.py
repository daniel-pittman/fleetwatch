"""Runs the indexer check scripts in tests/indexer_checks as subprocesses.

Each script is a standalone check that exits 1 on failure and executes at import time, so
unittest discovery cannot import it; a subprocess keeps the script runnable on its own. Exit
77 means a program the check needs is missing, and is reported as a skip.
"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SKIP_EXIT = 77      # a check script's exit code for "a program it needs is missing"


class IndexerChecks(unittest.TestCase):
    def run_check(self, name):
        env = dict(os.environ, FLEETWATCH_NO_MIGRATE="1",
                   PYTHONPATH=REPO + os.pathsep + os.environ.get("PYTHONPATH", ""))
        p = subprocess.run([sys.executable, os.path.join(HERE, "indexer_checks", name + ".py")],
                           capture_output=True, text=True, env=env, cwd=REPO)
        if p.returncode == SKIP_EXIT:
            self.skipTest("; ".join(ln for ln in p.stdout.splitlines() if ln.startswith("skip:")))
        self.assertEqual(p.returncode, 0, "%s failed:\n%s\n%s" % (name, p.stdout[-3000:], p.stderr[-2000:]))

    def test_tagger(self):
        self.run_check("tagger")

    def test_scrub_controls(self):
        self.run_check("scrub_controls")

    def test_doc_formats(self):
        self.run_check("doc_formats")

    def test_doc_formats_skips_per_missing_program_unless_required(self):
        script = os.path.join(HERE, "indexer_checks", "doc_formats.py")
        with tempfile.TemporaryDirectory() as empty:
            env = dict(os.environ, PATH=empty, FLEETWATCH_NO_MIGRATE="1",
                       PYTHONPATH=REPO + os.pathsep + os.environ.get("PYTHONPATH", ""))
            env.pop("FLEETWATCH_REQUIRE_DOC_TOOLS", None)
            p = subprocess.run([sys.executable, script], capture_output=True, text=True, env=env, cwd=REPO)
            self.assertEqual(p.returncode, SKIP_EXIT, p.stdout + p.stderr)
            skips = [ln for ln in p.stdout.splitlines() if ln.startswith("skip:")]
            self.assertEqual([ln.split()[1] for ln in skips], ["pandoc", "soffice", "tesseract"])
            env["FLEETWATCH_REQUIRE_DOC_TOOLS"] = "1"
            p = subprocess.run([sys.executable, script], capture_output=True, text=True, env=env, cwd=REPO)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
