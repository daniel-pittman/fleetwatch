"""--once and --json output stay byte-identical to v4 on the fixed stand-in fleetwatch."""
import json
import os

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")  # tests never move a real home folder
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import standin  # noqa: E402
from fleetwatch import render_text  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(__file__), "golden")


def golden(name):
    with open(os.path.join(GOLDEN, name)) as fh:
        return fh.read()


class GoldenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = standin.build(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def sections(self, show_all):
        return standin.collect_view(self.paths, show_all=show_all)

    def test_text_output_is_identical(self):
        for show_all, tag in ((False, "def"), (True, "all")):
            sections, stale, warnings = self.sections(show_all)
            for width in (60, 80, 120):
                for utf8, u in ((True, "u"), (False, "a")):
                    text = render_text.render(sections, stale, standin.NOW, False, warnings, width=width,
                                              utf8=utf8, show_all=show_all)
                    self.assertEqual(text, golden("once_w%d_%s_%s.txt" % (width, u, tag)),
                                     "width %d utf8 %s all %s" % (width, utf8, show_all))
            color = render_text.render(sections, stale, standin.NOW, True, warnings, width=100,
                                       utf8=True, show_all=show_all)
            self.assertEqual(color, golden("once_color_%s.txt" % tag))

    def test_json_output_is_identical(self):
        for show_all, tag in ((False, "def"), (True, "all")):
            sections, stale, warnings = self.sections(show_all)
            text = json.dumps(render_text.to_json(sections, stale, standin.NOW, 24 * 3600, warnings, 21),
                              indent=2)
            self.assertEqual(text, golden("json_%s.json" % tag))

    def test_cli_once_and_json_run_without_textual(self):
        """--once and --json are plain text paths: no escape codes, valid JSON."""
        import subprocess
        env = dict(os.environ, FLEETWATCH_AGENTS_CMD=self.paths["command"], FLEETWATCH_PS_CMD="true",
                   FLEETWATCH_SESSIONS_DIR=self.paths["sessions"], FLEETWATCH_JOBS_DIR=self.paths["jobs"],
                   FLEETWATCH_STATUS_DIR=self.paths["status"], COLUMNS="100")
        root = os.path.join(os.path.dirname(__file__), "..")
        out = subprocess.run([sys.executable, "-m", "fleetwatch", "--once"], env=env, cwd=root,
                             capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("\x1b", out.stdout)
        js = subprocess.run([sys.executable, "-m", "fleetwatch", "--json"], env=env, cwd=root,
                            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(js.returncode, 0, js.stderr)
        self.assertIn("counts", json.loads(js.stdout))


if __name__ == "__main__":
    unittest.main()
