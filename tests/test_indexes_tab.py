"""The Indexes tab: health only, read-only, and nothing from inside an index reaches the screen."""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fleetwatch import app as fapp, data, demo, index_health, views  # noqa: E402

CANARIES = ("CANARY-CORPUS-TEXT", "CANARY-PROBE-TEXT", "CANARY-TAG", "CANARY-ERROR-TEXT", "canary-secret-dir")


def iso(seconds_ago):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


class CanaryTree:
    """A registry and data folder whose every content field holds a canary string."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.reg = {"defaults": {"schedule_minutes": 30}, "indexes": {}}
        self.add("alpha", "team", ok=True, ago=600)
        self.add("beta", "team", ok=False, ago=900, error="engine build failed: /srv/canary-secret-dir CANARY-ERROR-TEXT")
        self.add("gamma", "team", ok=True, ago=3 * 86400)
        self.add("delta", "ferpa", ok=True, ago=60)

    def add(self, name, group, ok, ago, error=None):
        state = self.root / name / "state"
        state.mkdir(parents=True)
        info = {"ok": ok, "finished": iso(ago), "exchanges": 12, "tags": {"tagged": 1, "x": "CANARY-TAG"}}
        if error:
            info["error"] = error
        (state / "last_run.json").write_text(json.dumps(info))
        corpus = self.root / name / "corpus" / "session"
        corpus.mkdir(parents=True)
        for i in range(3):
            (corpus / ("CANARY-CORPUS-TEXT-%d.md" % i)).write_text("CANARY-CORPUS-TEXT body")
        (self.root / name / "probes.yaml").write_text("- CANARY-PROBE-TEXT")
        self.reg["indexes"][name] = {"group": group, "tags": "CANARY-TAG.yaml", "owner_session": "CANARY-TAG",
                                     "privacy": {"allowlist": "no-such-%s.yaml" % name}}

    def load(self, scheduled=False, cache=None):
        return index_health.load(self.reg, self.root, scheduled, cache or index_health.Cache())

    def close(self):
        self.tmp.cleanup()


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        os.environ.pop("FLEETWATCH_DEMO", None)
        self.t = CanaryTree()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        self.t.close()

    def rows(self, **kw):
        return {h.name: h for h in self.t.load(**kw).rows}

    def test_status_counts_and_vocabulary(self):
        rows = self.rows()
        self.assertEqual((rows["alpha"].state, rows["alpha"].exchanges, rows["alpha"].files), ("ok", 12, 3))
        self.assertEqual((rows["beta"].state, rows["beta"].reason), ("failed", "build"))
        self.assertEqual(rows["alpha"].kind, "session")

    def test_stale_is_twice_the_schedule_or_two_days_without_one(self):
        now = time.time()
        scheduled = self.rows(scheduled=True)
        self.assertEqual(scheduled["alpha"].stale_after, 3600)
        self.assertFalse(scheduled["alpha"].is_stale(now))
        self.assertTrue(scheduled["gamma"].is_stale(now))
        plain = self.rows(scheduled=False)
        self.assertEqual(plain["alpha"].stale_after, 2 * 86400)
        self.assertTrue(plain["gamma"].is_stale(now))                  # three days old
        self.assertFalse(plain["alpha"].is_stale(now))

    def test_protection_is_checked_for_protected_groups_only(self):
        rows = self.rows()
        self.assertIsNone(rows["alpha"].protection)
        self.assertEqual((rows["delta"].protection, rows["delta"].refusal), ("refused", "scanner"))

    def test_the_protection_check_is_cached_until_a_file_changes(self):
        from fleetwatch.memory import refresh
        cache = index_health.Cache()
        with mock.patch.object(refresh, "Scanner", side_effect=ValueError("no")) as scanner:
            self.t.load(cache=cache)
            self.t.load(cache=cache)
            self.assertEqual(scanner.call_count, 1)

    def test_loading_writes_nothing(self):
        def tree():
            return sorted((str(p), p.stat().st_mtime_ns) for p in self.t.root.rglob("*"))
        before = tree()
        self.t.load()
        self.assertEqual(tree(), before)

    def test_nothing_from_inside_an_index_is_in_a_row(self):
        text = repr(self.t.load().rows)
        for canary in CANARIES:
            self.assertNotIn(canary, text)

    def test_a_missing_memory_extra_gives_one_line(self):
        import fleetwatch.memory as memory
        saved = memory.__dict__.pop("refresh", None)
        try:
            with mock.patch.dict(sys.modules, {"fleetwatch.memory.refresh": None}):
                result = index_health.load()
        finally:
            if saved is not None:
                memory.refresh = saved
        self.assertEqual((result.rows, result.note), ([], index_health.MISSING_EXTRA))
        self.assertIn("fleetwatch[memory]", result.note)


class TabTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        self.tmp, self.paths = demo.prepare()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        self.tmp.cleanup()

    def board(self, size, script, loader=None):
        async def go():
            app = fapp.FleetwatchApp(
                lambda: fapp.gather(self.paths, data.parse_window("24h"), data.DEFAULT_HORIZON_DAYS),
                3600, paths=self.paths)
            async with app.run_test(size=size) as pilot:
                await pilot.pause(1.5)
                return await script(app, pilot)
        if loader is None:
            return asyncio.run(go())
        with mock.patch.object(index_health, "load", loader):
            return asyncio.run(go())

    def test_tab_five_lists_each_index_by_group_with_its_state_in_words(self):
        async def script(app, pilot):
            await pilot.press("5", "z")
            await pilot.pause(0.3)
            return app.lists[views.INDEXES_TAB].visible_text(), app.query_one(fapp.TabBar).plain
        text, bar = self.board((140, 40), script)
        for want in ("team", "archive", "ferpa", "storefront-sessions", "refreshed 14m ago", "1842 exchanges",
                     "96 files", "refresh FAILED (build)", "STALE", "protection REFUSED (settings)",
                     "group: ferpa", "plain"):
            self.assertIn(want, text)
        self.assertIn("5 Indexes · 4", bar)

    def test_needs_you_lists_each_problem_index_with_the_reason(self):
        async def script(app, pilot):
            return app.lists[0].visible_text(), app.tab_counts()[0]
        text, count = self.board((140, 40), script)
        self.assertIn("Indexes · 3", text)
        self.assertIn("release-history  last refresh failed (build)", text)
        self.assertIn("advising-notes  protection check refused (settings)", text)
        self.assertIn("site-docs  stale", text)
        self.assertNotIn("storefront-sessions", text)
        self.assertEqual(count, 4 + 3)                  # four sessions need you, three indexes

    def test_y_copies_a_search_command_for_the_highlighted_index(self):
        async def script(app, pilot):
            copied = []
            app.copy_to_clipboard = copied.append
            await pilot.press("5")
            lst = app.lists[views.INDEXES_TAB]
            await pilot.press("y")                      # on a group header: says what y does, copies nothing
            lst.move_to("idx:release-history")
            await pilot.press("y")
            await pilot.pause(0.2)
            return copied, app.status_text, app.footer_plain
        copied, status, footer = self.board((140, 40), script)
        self.assertEqual(copied, ['fleetwatch search --index release-history "'])
        self.assertIn("copied", status)
        self.assertIn("y copy search", footer)
        self.assertTrue(any("  y " in line and "search" in line for line in fapp.HELP_LINES))

    def test_missing_extra_shows_one_line(self):
        async def script(app, pilot):
            await pilot.press("5")
            await pilot.pause(0.2)
            return app.lists[views.INDEXES_TAB].visible_text(), app.lists[0].visible_text()
        loader = lambda: index_health.Result(note=index_health.MISSING_EXTRA)  # noqa: E731
        tab, needs = self.board((140, 40), script, loader)
        self.assertEqual(tab.strip(), index_health.MISSING_EXTRA)
        self.assertNotIn("Indexes", needs)

    def test_nothing_from_inside_an_index_reaches_any_screen(self):
        tree = CanaryTree()
        self.addCleanup(tree.close)
        real = index_health.load

        def loader():
            os.environ.pop("FLEETWATCH_DEMO", None)
            return real(tree.reg, tree.root, False, index_health.Cache())

        async def script(app, pilot):
            seen = []
            for key in "5", "1":
                await pilot.press(key, "z")
                await pilot.pause(0.3)
                seen.append(app.lists[app.active].visible_text())
            await pilot.press("5", "y")
            seen.append(app.status_text or "")
            seen.append(app.footer_plain)
            seen.append("\n".join(fapp.HELP_LINES))
            return "\n".join(seen), app.lists[views.INDEXES_TAB].visible_text()
        text, tab = self.board((140, 40), script, loader)
        self.assertIn("alpha", tab)
        self.assertIn("refresh FAILED (build)", tab)
        for canary in CANARIES:
            self.assertNotIn(canary, text)

    def test_the_demo_shows_every_state(self):
        states = {(h.state, h.protection) for h in demo.index_rows()}
        self.assertEqual(states, {("ok", None), ("failed", None), ("ok", "refused")})
        self.assertTrue(any(h.is_stale(time.time()) for h in demo.index_rows()))


if __name__ == "__main__":
    unittest.main()
