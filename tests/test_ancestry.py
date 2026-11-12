"""Finding the calling session must not depend on reading the whole process table in time."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fleetwatch import data, write_status  # noqa: E402
from fleetwatch.write_status import StatusError, find_session  # noqa: E402


@unittest.skipUnless(sys.platform == "darwin" or os.path.exists("/proc/self/stat"),
                     "needs sysctl or /proc")
class KernelPpidTests(unittest.TestCase):
    def test_matches_getppid_for_this_process(self):
        self.assertEqual(data.kernel_ppid(os.getpid()), os.getppid())

    def test_a_pid_that_does_not_exist_is_none(self):
        self.assertIsNone(data.kernel_ppid(2 ** 30))


class AncestryTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("FLEETWATCH_PS_CMD", None)

    def test_kernel_answer_never_runs_ps(self):
        with mock.patch.object(data, "kernel_ppid", return_value=7), \
                mock.patch.object(data, "read_ps", side_effect=AssertionError("ps ran")):
            self.assertEqual(data.Ancestry().get(100), 7)

    def test_falls_back_to_the_ps_table_with_a_long_timeout(self):
        calls = []

        def fake_ps(command, warnings, timeout=10):
            calls.append(timeout)
            return {100: 42}

        with mock.patch.object(data, "kernel_ppid", return_value=None), \
                mock.patch.object(data, "read_ps", side_effect=fake_ps):
            anc = data.Ancestry()
            self.assertEqual(anc.get(100), 42)
            self.assertEqual(anc.get(100), 42)
        self.assertEqual(calls, [60])          # read once, cached

    def test_forced_table_skips_the_kernel(self):
        os.environ["FLEETWATCH_PS_CMD"] = "ps -axo pid=,ppid="
        with mock.patch.object(data, "kernel_ppid", side_effect=AssertionError("kernel asked")), \
                mock.patch.object(data, "read_ps", return_value={100: 9}):
            self.assertEqual(data.Ancestry().get(100), 9)

    def test_unreadable_table_says_so_instead_of_no_session(self):
        def failing_ps(command, warnings, timeout=10):
            warnings.append("warning: process table unreadable")
            return {}

        with tempfile.TemporaryDirectory() as sessions, \
                mock.patch.object(data, "kernel_ppid", return_value=None), \
                mock.patch.object(data, "read_ps", side_effect=failing_ps):
            with self.assertRaises(StatusError) as cm:
                find_session(sessions, data.Ancestry(), 12345)
        self.assertIn("could not read the process table", str(cm.exception))

    def test_walk_reaches_a_session_through_the_kernel(self):
        tree = {300: 200, 200: 100, 100: 1}
        with tempfile.TemporaryDirectory() as sessions:
            with open(os.path.join(sessions, "100.json"), "w") as fh:
                fh.write('{"name": "Billing API", "sessionId": "abc"}')
            with mock.patch.object(data, "kernel_ppid", side_effect=tree.get), \
                    mock.patch.object(data, "read_ps", side_effect=AssertionError("ps ran")):
                self.assertEqual(find_session(sessions, data.Ancestry(), 300), ("Billing API", "abc"))


if __name__ == "__main__":
    unittest.main()
