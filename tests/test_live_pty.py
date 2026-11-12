"""The live app in a real pseudo-terminal: it starts, quits on q and Ctrl-C, and restores the terminal."""
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

sys.path.insert(0, os.path.dirname(__file__))
import standin  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


class LivePtyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        paths = standin.build(self.tmp.name)
        self.env = dict(os.environ, FLEETWATCH_AGENTS_CMD=paths["command"], FLEETWATCH_PS_CMD="true",
                        FLEETWATCH_SESSIONS_DIR=paths["sessions"], FLEETWATCH_JOBS_DIR=paths["jobs"],
                        FLEETWATCH_STATUS_DIR=paths["status"], FLEETWATCH_OSASCRIPT="false", FLEETWATCH_TTY_CMD="true",
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


if __name__ == "__main__":
    unittest.main()
