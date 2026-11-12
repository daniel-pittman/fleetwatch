"""Search output never passes a control sequence from an indexed document to the terminal."""
import contextlib
import io
import json
import unittest

from fleetwatch.memory import session_search
from fleetwatch.search import cli

OSC52 = "\x1b]52;c;cHduZWQ=\x07"
CSI = "\x1b[2J\x9b31m\r"


def row(text):
    return {"level": None, "region": "body", "area": None, "heading_path": "Notes " + CSI,
            "heading": "", "file_title": "", "kind": "doc", "path": "docs/n/a" + OSC52 + ".md",
            "text": "the lantern " + OSC52 + text, "tags": "t " + CSI, "also_in": None}


class TerminalSafe(unittest.TestCase):
    def assertSafe(self, out):
        for ch in ("\x1b", "\x07", "\x9b", "\r"):
            self.assertNotIn(ch, out)
        self.assertIn("\\x1b", out)

    def test_search_results_escape_controls(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.print_results([(row(" battery"), 0.5)], "lantern", False)
        self.assertSafe(buf.getvalue())
        self.assertIn("lantern", buf.getvalue())

    def test_json_output_is_already_escaped(self):
        self.assertNotIn("\x1b", json.dumps({"t": OSC52}))

    def test_an_exchange_escapes_controls_and_keeps_its_lines(self):
        meta = {"id": "Saa11bb-EX-0001", "summary": "s " + OSC52, "tags": ["topic:x" + CSI],
                "incoming": "line one " + OSC52 + "\nline two", "reply": "ok " + CSI}
        for full in (False, True):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                session_search.print_exchange(meta, full)
            self.assertSafe(buf.getvalue())
        self.assertIn("    line two", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
