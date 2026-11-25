"""Reloading a launchd job survives launchd's slow teardown.

bootout returns before launchd has finished unloading the job, and a bootstrap in that window fails
with "Input/output error" (5). load() waits for the job to read as unloaded and retries once.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fleetwatch import schedule


class Launchd:
    """A launchd whose teardown takes `settle` polls, and whose bootstrap fails while it is pending."""

    def __init__(self, settle):
        self.loaded, self.pending, self.settle, self.calls = True, 0, settle, []

    def __call__(self, *args):
        self.calls.append(args[0])
        verb = args[0]
        if verb == "print":
            if self.pending:
                self.pending -= 1
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 0 if self.loaded else 113, "", "")
        if verb == "bootout":
            self.loaded, self.pending = False, self.settle
            return subprocess.CompletedProcess(args, 0, "", "")
        if verb == "bootstrap":
            if self.pending:
                return subprocess.CompletedProcess(args, 5, "", "Bootstrap failed: 5: Input/output error")
            self.loaded = True
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(verb)


class Reload(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.plist = Path(d.name) / "x.plist"
        self.plist.write_text("<plist/>")
        p = mock.patch.object(schedule, "_plist_path", lambda job: self.plist)
        p.start()
        self.addCleanup(p.stop)
        g = mock.patch.object(schedule, "gui_domain", lambda: "gui/501")
        g.start()
        self.addCleanup(g.stop)
        t = mock.patch.object(schedule.time, "sleep", lambda s: None)
        t.start()
        self.addCleanup(t.stop)

    def test_a_reload_waits_for_the_teardown_before_bootstrap(self):
        fake = Launchd(settle=3)
        with mock.patch.object(schedule, "launchctl", fake):
            self.assertTrue(schedule.load(out=open("/dev/null", "w"), job=schedule.INDEX))
        self.assertEqual(fake.calls.count("bootstrap"), 1)

    def test_a_bootstrap_that_still_races_is_retried_once(self):
        fake = Launchd(settle=0)
        real_call = fake.__call__
        failed = []

        def flaky(*args):
            if args[0] == "bootstrap" and not failed:
                failed.append(1)
                return subprocess.CompletedProcess(args, 5, "", "Bootstrap failed: 5: Input/output error")
            return real_call(*args)
        with mock.patch.object(schedule, "launchctl", flaky):
            self.assertTrue(schedule.load(out=open("/dev/null", "w"), job=schedule.INDEX))
        self.assertEqual(fake.calls.count("bootstrap") + len(failed), 2)

    def test_a_bootstrap_that_keeps_failing_reports_it(self):
        def broken(*args):
            if args[0] == "print":
                return subprocess.CompletedProcess(args, 113, "", "")
            return subprocess.CompletedProcess(args, 5, "", "Bootstrap failed: 5: Input/output error")
        with mock.patch.object(schedule, "launchctl", broken):
            self.assertFalse(schedule.load(out=open("/dev/null", "w"), job=schedule.INDEX))


if __name__ == "__main__":
    unittest.main()
