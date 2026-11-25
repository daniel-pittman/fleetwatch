"""The tagger's claude -p call must carry its own cleanupPeriodDays.

It loads no setting sources, so without an explicit value Claude Code's startup cleanup
falls back to its 30-day default and deletes transcripts in every project.
"""
import json
import unittest
from unittest import mock

from fleetwatch.memory import session_tagger


class TaggerRetention(unittest.TestCase):
    def test_the_tagger_passes_a_long_cleanup_period_with_no_setting_sources(self):
        seen = {}

        def run(cmd, **k):
            seen["cmd"] = cmd
            raise RuntimeError("stop after capturing the command")
        tc = mock.Mock(model="haiku", system="sys")
        batch = [dict(id="x", sender_kind="human", sender_name="user", user_text="hi", assistant=["hello"], tools=set())]
        with mock.patch.object(session_tagger.subprocess, "run", run):
            with self.assertRaises(RuntimeError):
                session_tagger.tag_batch(batch, tc, mock.Mock(), "/")
        cmd = seen["cmd"]
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "")
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        self.assertGreaterEqual(settings["cleanupPeriodDays"], 3650)


if __name__ == "__main__":
    unittest.main()
