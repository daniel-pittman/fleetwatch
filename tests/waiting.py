"""Condition waits for Textual UI tests.

A fixed `pilot.pause(n)` before an assertion races the app on a loaded
machine; every wait here polls the condition the assertion depends on.
"""
import asyncio
import time

POLL = 0.05
BOUND = 10.0


async def settle(pilot, check, what="the awaited condition", bound=BOUND):
    """Poll `check()` every POLL seconds until it is truthy; fail after `bound` seconds."""
    deadline = time.monotonic() + bound
    while True:
        if check():
            return
        if time.monotonic() > deadline:
            raise AssertionError("timed out after %.0fs waiting for %s" % (bound, what))
        await pilot.pause(POLL)


async def first_scan(app, pilot):
    """Wait for the board's first refresh and the Resume scan, then let queued messages run."""
    await settle(pilot, lambda: app.refreshes and app.resume_data is not None, "the first scan")
    await pilot.pause()
