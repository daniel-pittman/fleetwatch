"""Ending the board when it is told to stop or its terminal goes away.

Textual's input thread treats an empty read as piped input and selects again at once, so a
board whose terminal has closed spins it at full CPU (textual/drivers/linux_driver.py,
run_input_thread). Its output thread dies on the first failed write, and the next writes fill its
bounded queue (textual/drivers/_writer_thread.py, MAX_QUEUED_WRITES) until one blocks the event
loop's own thread. An asyncio signal handler only runs when that loop turns, so from then on
SIGTERM and SIGHUP do nothing. Two things here do not depend on the loop:

- SIGTERM and SIGHUP get plain `signal.signal` handlers. Python runs them on the main thread even
  while it waits on a lock, as a blocked `Queue.put` does.
- A daemon thread probes the board's terminal once a second with a zero-length write, which
  returns 0 on a live terminal and fails with EIO once the terminal has hung up (macOS; on
  Linux a hung-up tty's write is `hung_up_tty_write`, which returns -EIO). A board whose
  terminal closed without a SIGHUP reaching it, because it was not the terminal's session
  leader or foreground process, ends the same way.

Either one asks the app to exit and arms a deadline: if the process is still running
DEADLINE_S later (a wedged loop, or a worker thread the interpreter waits for at exit), it
calls os._exit with 128 plus the signal number.
"""
import errno
import os
import signal
import threading

DEADLINE_S = 3.0
PROBE_S = 1.0
SIGNALS = (signal.SIGTERM, signal.SIGHUP)
# A probe that fails with one of these says nothing about the terminal.
TRANSIENT = frozenset({errno.EINTR, errno.EAGAIN, errno.EWOULDBLOCK})


def terminal_gone(fd):
    """True when a zero-length write to `fd` fails: the terminal has hung up or been closed."""
    try:
        os.write(fd, b"")
    except OSError as exc:
        return exc.errno not in TRANSIENT
    return False


class Guard:
    """Installs the handlers and the probe for one board run. `request_exit` must be safe to call
    from any thread and must not block; the board passes one that schedules App.exit on its loop."""

    def __init__(self, request_exit, fd, deadline_s=DEADLINE_S, probe_s=PROBE_S):
        self.request_exit = request_exit
        self.fd = fd
        self.deadline_s = deadline_s
        self.probe_s = probe_s
        self.ending = False
        self.stopped = threading.Event()
        self.previous = {}

    def install(self):
        for sig in SIGNALS:
            self.previous[sig] = signal.signal(sig, self._on_signal)
        threading.Thread(target=self._probe, name="fleetwatch-hangup", daemon=True).start()

    def stop(self):
        """The board has returned: stop probing and restore the previous handlers. A deadline
        already armed stays armed, so an exit that began here still ends."""
        self.stopped.set()
        for sig, handler in self.previous.items():
            try:
                signal.signal(sig, handler)
            except (TypeError, ValueError):  # pragma: no cover
                pass
        self.previous.clear()

    def end(self, code):
        """Ask the board to exit, and exit with `code` if it has not done so in time."""
        if self.ending:
            return
        self.ending = True
        timer = threading.Timer(self.deadline_s, os._exit, (code,))
        timer.daemon = True
        timer.start()
        try:
            self.request_exit()
        except Exception:
            pass

    def _on_signal(self, signum, _frame):
        self.end(128 + signum)

    def _probe(self):
        while not self.stopped.wait(self.probe_s):
            if terminal_gone(self.fd):
                self.end(128 + signal.SIGHUP)
                return
