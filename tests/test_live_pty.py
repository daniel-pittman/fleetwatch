"""The live app in a real pseudo-terminal: it starts, quits on q and Ctrl-C, and restores the terminal;
SIGTERM and SIGHUP end it, and so does its terminal going away."""
import os

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")  # tests never move a real home folder
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, ROOT)
import standin  # noqa: E402
from fleetwatch import hangup  # noqa: E402


class LivePtyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        paths = standin.build(self.tmp.name)
        self.env = dict(os.environ, FLEETWATCH_AGENTS_CMD=paths["command"], FLEETWATCH_PS_CMD="true",
                        FLEETWATCH_SESSIONS_DIR=paths["sessions"], FLEETWATCH_JOBS_DIR=paths["jobs"],
                        FLEETWATCH_STATUS_DIR=paths["status"], FLEETWATCH_OSASCRIPT="false", FLEETWATCH_TTY_CMD="true",
                        FLEETWATCH_PROJECTS_DIR=paths["projects"],
                        FLEETWATCH_ARCHIVE_DIR=os.path.join(self.tmp.name, "archive"),
                        TERM="xterm-256color", COLUMNS="100", LINES="30", PYTHONIOENCODING="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def drive(self, keys=None, sig=None, delay=4.0):
        import termios
        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)
        proc = subprocess.Popen([sys.executable, "-m", "fleetwatch", "--interval", "1"], env=self.env, cwd=ROOT,
                                stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        buf, sent_at, sent = b"", time.time() + delay, False
        while time.time() < sent_at + 20 and proc.poll() is None:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    buf += os.read(master, 65536)
                except OSError:
                    break
            if not sent and time.time() >= sent_at:
                if sig:
                    proc.send_signal(sig)
                else:
                    os.write(master, keys)
                sent = True
        code = proc.wait(timeout=15)
        try:
            while select.select([master], [], [], 0.3)[0]:
                buf += os.read(master, 65536)
        except OSError:
            pass
        after = termios.tcgetattr(slave)
        os.close(master)
        os.close(slave)
        before[3] &= ~0x20000000     # PENDIN is set by the kernel on any mode change
        after[3] &= ~0x20000000
        return code, buf.decode("utf-8", "replace"), before, after

    def check(self, code, out, before, after):
        self.assertEqual(code, 0, out[-300:])
        self.assertIn("\x1b[?1049h", out)
        self.assertIn("Stuck Session", out)
        self.assertIn("\x1b[?1049l", out)
        self.assertLess(out.rindex("\x1b[?25l") if "\x1b[?25l" in out else 0, out.rindex("\x1b[?25h"))
        self.assertEqual(before, after)

    def test_q_quits_and_restores(self):
        self.check(*self.drive(keys=b"q"))

    def test_ctrl_c_quits_and_restores(self):
        self.check(*self.drive(keys=b"\x03"))

    def test_sigterm_restores(self):
        self.check(*self.drive(sig=signal.SIGTERM))

    def test_sighup_restores(self):
        self.check(*self.drive(sig=signal.SIGHUP))

    def start_and_hang_up(self, leader):
        """Start the board on a fresh pty, wait until it has drawn, then close every descriptor this
        process holds on the pty. As the session leader with the pty as its controlling terminal
        (`leader`), the board is sent SIGHUP; otherwise nothing signals it and only the terminal
        itself says it has gone. Returns (process, seconds from the close to its exit or None)."""
        import fcntl
        import termios
        master, slave = pty.openpty()

        def own_terminal():
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        proc = subprocess.Popen([sys.executable, "-m", "fleetwatch", "--interval", "1"], env=self.env, cwd=ROOT,
                                stdin=slave, stdout=slave, stderr=slave, close_fds=True, start_new_session=True,
                                preexec_fn=own_terminal if leader else None)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        os.close(slave)
        buf, until = b"", time.time() + 20
        while b"Stuck Session" not in buf and time.time() < until and proc.poll() is None:
            if select.select([master], [], [], 0.2)[0]:
                buf += os.read(master, 65536)
        self.assertIn(b"Stuck Session", buf)
        os.close(master)
        closed = time.time()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            return proc, None
        return proc, time.time() - closed

    def test_closing_its_terminal_ends_the_board(self):
        _proc, took = self.start_and_hang_up(leader=False)
        self.assertIsNotNone(took, "the board kept running after its terminal closed")
        self.assertLess(took, 5)

    def test_a_hangup_ends_the_board_that_leads_the_terminal(self):
        _proc, took = self.start_and_hang_up(leader=True)
        self.assertIsNotNone(took, "the board kept running after its terminal hung up")
        self.assertLess(took, 5)


WEDGED = """
import os, queue, signal, sys
sys.path.insert(0, sys.argv[1])
from fleetwatch import hangup
guard = hangup.Guard(lambda: None, os.open(os.devnull, os.O_WRONLY), deadline_s=1.0)
guard.install()
full = queue.Queue(1)
full.put(None)
print("wedged", flush=True)
full.put(None)
"""


class WedgedLoopTests(unittest.TestCase):
    """The state #35 found: the board's thread blocked on Textual's full output queue, where an
    asyncio signal handler never runs. SIGTERM and SIGHUP still end the process."""

    def wedge_and_signal(self, sig):
        proc = subprocess.Popen([sys.executable, "-c", WEDGED, ROOT], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        self.assertEqual(proc.stdout.readline().strip(), "wedged")
        sent = time.time()
        proc.send_signal(sig)
        code = proc.wait(timeout=6)
        proc.stdout.close()
        return code, time.time() - sent

    def test_sigterm_ends_a_wedged_board(self):
        code, took = self.wedge_and_signal(signal.SIGTERM)
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertLess(took, 4)

    def test_sighup_ends_a_wedged_board(self):
        code, took = self.wedge_and_signal(signal.SIGHUP)
        self.assertEqual(code, 128 + signal.SIGHUP)
        self.assertLess(took, 4)


class ProbeTests(unittest.TestCase):
    def test_a_live_terminal_is_not_gone_and_a_closed_one_is(self):
        master, slave = pty.openpty()
        try:
            self.assertFalse(hangup.terminal_gone(slave))
            os.close(master)
            master = None
            self.assertTrue(hangup.terminal_gone(slave))
        finally:
            os.close(slave)
            if master is not None:
                os.close(master)

    def test_the_probe_ends_the_run_once_and_stop_restores_the_handlers(self):
        calls, exits = [], []
        before = signal.getsignal(signal.SIGTERM)
        master, slave = pty.openpty()
        self.addCleanup(os.close, slave)
        guard = hangup.Guard(lambda: calls.append(1), slave, deadline_s=0.3, probe_s=0.05, hard_exit=exits.append)
        guard.install()
        try:
            self.assertIsNot(signal.getsignal(signal.SIGTERM), before)
            time.sleep(0.2)
            self.assertEqual(calls, [])
            os.close(master)
            until = time.time() + 3
            while not exits and time.time() < until:
                time.sleep(0.05)
            self.assertEqual(calls, [1])
            self.assertEqual(exits, [128 + signal.SIGHUP])
            guard.end(1)
            time.sleep(0.5)
            self.assertEqual((calls, exits), ([1], [128 + signal.SIGHUP]))
        finally:
            guard.stop()
        self.assertIs(signal.getsignal(signal.SIGTERM), before)


if __name__ == "__main__":
    unittest.main()
