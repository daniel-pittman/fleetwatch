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
        if name == "alpha":
            info["exchanges_by_provider"] = {"claude": 7, "codex": 3, "antigravity": 2, "CANARY-TAG": 12}
            info["agent_runs"] = {"launch": 2, "folder": 1, "catch_all": 3, "CANARY-TAG": 12}
        if error:
            info["error"] = error
        (state / "last_run.json").write_text(json.dumps(info))
        corpus = self.root / name / "corpus" / "session"
        corpus.mkdir(parents=True)
        for i in range(3):
            (corpus / ("CANARY-CORPUS-TEXT-%d.md" % i)).write_text("CANARY-CORPUS-TEXT body")
        (self.root / name / "probes.yaml").write_text("- CANARY-PROBE-TEXT")
        self.reg["indexes"][name] = {"group": group, "tags": "CANARY-TAG.yaml", "owner_session": "builder-" + name, "owner_session_id": "sid-" + name,
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
        self.assertEqual(rows["alpha"].exchanges_by_provider, {"claude": 7, "codex": 3, "antigravity": 2})
        self.assertEqual(rows["alpha"].agent_runs, {"launch": 2, "folder": 1, "catch_all": 3})

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


class BoardCase(unittest.TestCase):
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
                async with asyncio.timeout(5):
                    while app.snapshot is None or app.resume_data is None:
                        await pilot.pause()
                return await script(app, pilot)
        if loader is None:
            return asyncio.run(go())
        with mock.patch.object(index_health, "load", loader):
            return asyncio.run(go())


class TabTests(BoardCase):
    def test_expanded_index_shows_provider_and_placement_counts(self):
        tree = CanaryTree()
        self.addCleanup(tree.close)
        real = index_health.load

        def loader():
            with mock.patch.dict(os.environ, FLEETWATCH_DEMO=""):
                return real(tree.reg, tree.root, False, index_health.Cache())

        async def script(app, pilot):
            await pilot.press("5", "z")
            return app.lists[views.INDEXES_TAB].visible_text()

        text = self.board((140, 40), script, loader)
        self.assertIn("exchanges by provider: claude 7, codex 3, antigravity 2", text)
        self.assertIn("agent runs: 2 by launch, 1 by folder, 3 by catch-all", text)
        self.assertNotIn("CANARY-TAG", text)

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
        self.assertIn("5 Indexes · 6", bar)

    def test_needs_you_lists_each_problem_index_with_the_reason(self):
        async def script(app, pilot):
            return app.lists[0].visible_text(), app.tab_counts()[0]
        text, count = self.board((140, 40), script)
        self.assertIn("Indexes · 3", text)
        self.assertIn("release-history  last refresh failed (build)", text)
        self.assertIn("advising-notes  protection check refused (settings)", text)
        self.assertIn("site-docs  stale", text)
        self.assertNotIn("storefront-sessions", text)
        self.assertEqual(count, 4 + 3 + 2)              # four sessions need you, three indexes, two watch rows

    def test_y_copies_the_next_command_for_a_problem_and_a_search_for_a_healthy_index(self):
        async def script(app, pilot):
            copied = []
            app.copy_to_clipboard = copied.append
            await pilot.press("5")
            lst = app.lists[views.INDEXES_TAB]
            await pilot.press("y")                      # on a group header: says what y does, copies nothing
            lst.move_to("idx:storefront-sessions")
            await pilot.press("y")
            lst.move_to("idx:release-history")
            await pilot.press("y")
            await pilot.pause(0.2)
            return copied, app.status_text, app.footer_plain
        copied, status, footer = self.board((140, 40), script)
        self.assertEqual(copied, ['fleetwatch search --index storefront-sessions "',
                                  "fleetwatch index status release-history"])
        self.assertIn("copied", status)
        self.assertIn("y copy", footer)
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
        rows = demo.index_rows()
        self.assertEqual({h.refresh_state for h in rows}, {"", "refreshing", "waiting"})
        self.assertTrue(any(not h.owner for h in rows))


def health(**kw):
    base = dict(name="ix", group="team", kind="session", state="ok", finished=time.time() - 60, stale_after=3600)
    base.update(kw)
    return index_health.Health(**base)


class OwnerTests(unittest.TestCase):
    """Who owns an index, what to do about a problem, and a running refresh is not a problem."""

    def test_next_step_for_each_problem_kind(self):
        now = time.time()
        failed = " ".join(health(name="a", state="failed", reason="build").next_steps(now))
        self.assertIn("fleetwatch index status a", failed)
        self.assertIn("failures.log", failed)
        refused = " ".join(health(name="b", protection="refused", refusal="settings").next_steps(now))
        self.assertIn("allowlist", refused)
        self.assertIn("fleetwatch index verify b", refused)
        old = now - 5 * 3600
        off = " ".join(health(name="c", finished=old, scheduled=False).next_steps(now))
        self.assertIn("scheduled refresh is not installed", off)
        self.assertIn("fleetwatch index schedule install", off)
        on = " ".join(health(name="d", finished=old, scheduled=True).next_steps(now))
        self.assertIn("last successful refresh 5.0h ago", on)
        self.assertIn("fleetwatch index schedule status", on)
        never = " ".join(health(name="e", state="never", finished=None).next_steps(now))
        self.assertIn("fleetwatch index refresh e", never)

    def test_a_running_refresh_is_not_a_problem(self):
        now = time.time()
        old = now - 5 * 3600
        stale = health(finished=old)
        self.assertTrue(stale.problems(now))
        running = health(finished=old, refresh_state="refreshing")
        self.assertEqual(running.problems(now), [])
        self.assertEqual(health(finished=old, refresh_state="waiting").problems(now), [])
        self.assertEqual(health(state="failed", reason="build", refresh_state="refreshing").problems(now), [])
        self.assertTrue(health(protection="refused", refresh_state="refreshing").problems(now))
        self.assertEqual(index_health.problem_rows([running], now), [])

    def test_the_lock_files_decide_refreshing_and_waiting(self):
        tree = CanaryTree()
        self.addCleanup(tree.close)
        os.environ.pop("FLEETWATCH_DEMO", None)
        rows = {h.name: h for h in tree.load(scheduled=True).rows}
        self.assertEqual({h.refresh_state for h in rows.values()}, {""})
        lock = tree.root / "alpha" / "state" / "refresh.lock.d"
        lock.mkdir()
        rows = {h.name: h for h in tree.load(scheduled=True).rows}
        self.assertEqual(rows["alpha"].refresh_state, "refreshing")
        self.assertEqual(rows["gamma"].refresh_state, "waiting")       # stale, not reached yet
        self.assertEqual(rows["beta"].refresh_state, "")                # fresh enough: not waiting
        old = time.time() - 2 * 3600
        os.utime(lock, (old, old))                                      # a dead run's lock
        rows = {h.name: h for h in tree.load(scheduled=True).rows}
        self.assertEqual({h.refresh_state for h in rows.values()}, {""})

    def test_a_plain_index_in_a_protected_group_is_not_refused(self):
        tree = CanaryTree()
        self.addCleanup(tree.close)
        os.environ.pop("FLEETWATCH_DEMO", None)
        tree.reg["indexes"]["delta"]["kind"] = "plain"
        result = tree.load()
        rows = {h.name: h for h in result.rows}
        self.assertEqual(rows["delta"].protection, "plain")
        self.assertEqual(rows["delta"].problems(time.time()), [])
        self.assertNotIn("delta", [h.name for h in index_health.problem_rows(result.rows)])
        text = " ".join(str(n.head) for n in views.index_nodes(result.rows, time.time())[0].children)
        self.assertIn("its own scripts build the corpus", text)

    def test_the_registry_owner_is_read(self):
        tree = CanaryTree()
        self.addCleanup(tree.close)
        os.environ.pop("FLEETWATCH_DEMO", None)
        tree.reg["indexes"]["delta"].pop("owner_session")
        tree.reg["indexes"]["delta"].pop("owner_session_id")
        rows = {h.name: h for h in tree.load().rows}
        self.assertEqual((rows["alpha"].owner, rows["alpha"].owner_id), ("builder-alpha", "sid-alpha"))
        self.assertEqual((rows["delta"].owner, rows["delta"].owner_id), ("", ""))


class OwnerBoardTests(BoardCase):
    """Owner text and Enter on the Indexes tab, in the demo."""

    def test_rows_say_who_owns_them_and_the_next_step(self):
        async def script(app, pilot):
            await pilot.press("5", "z")
            await pilot.pause(0.3)
            tab = app.lists[views.INDEXES_TAB].visible_text()
            await pilot.press("1")
            await pilot.pause(0.3)
            return tab, app.lists[0].visible_text()
        tab, needs = self.board((140, 60), script)
        for want in ("owner: Billing API", "owner: Pricing Page", "no owner session", "refreshing now",
                     "waiting for the running refresh", "fleetwatch index status release-history",
                     "fleetwatch index schedule status", "fleetwatch index verify advising-notes"):
            self.assertIn(want, tab)
        self.assertIn("owner: Pricing Page", needs)
        self.assertIn("fleetwatch index status release-history", needs)
        self.assertNotIn("refreshing now", needs)

    def run_enter(self, key, demo_mode=False, jump=True):
        calls = {"jump": [], "resume": []}

        async def script(app, pilot):
            if not demo_mode:
                os.environ.pop("FLEETWATCH_DEMO", None)
            if jump:
                ttys = {r["pid"]: "ttyX" for r in app.snapshot.held.values()}
                app.snapshot.jump = {"ttys": ttys, "iterm": {"ttyX"}, "err": None}
            app._do_jump = lambda name, tty, close_find=False: calls["jump"].append((name, tty))
            app.start_resume = lambda sid, here: calls["resume"].append(sid)
            await pilot.press("5", "z")
            app.lists[views.INDEXES_TAB].move_to(key)
            await pilot.press("enter")
            await pilot.pause(0.4)
            return app.status_text
        status = self.board((140, 60), script)
        return calls, status

    def test_enter_on_a_running_owner_jumps_to_its_tab(self):
        calls, _ = self.run_enter("idx:storefront-sessions")
        self.assertEqual(len(calls["jump"]), 1)
        self.assertEqual((calls["jump"][0][0], calls["resume"]), ("Billing API", []))

    def test_enter_on_a_stopped_owner_resumes_it(self):
        calls, _ = self.run_enter("idx:release-history")
        self.assertEqual(calls["resume"], [demo.RESUME_SIDS[0]])
        self.assertEqual(calls["jump"], [])

    def test_enter_with_no_owner_names_the_command(self):
        calls, status = self.run_enter("idx:advising-notes")
        self.assertEqual((calls["jump"], calls["resume"]), ([], []))
        self.assertIn("no owner session", status)
        self.assertIn("fleetwatch index verify advising-notes", status)

    def test_enter_on_a_stale_ownerless_index_names_refresh(self):
        calls, status = self.run_enter("idx:nightly-notes")
        self.assertIn("fleetwatch index refresh nightly-notes", status)

    def test_enter_in_the_demo_only_says_what_it_would_do(self):
        calls, status = self.run_enter("idx:release-history", demo_mode=True)
        self.assertEqual((calls["jump"], calls["resume"]), ([], []))
        self.assertIn("demo", status)
        self.assertIn("resume Pricing Page", status)
        calls, status = self.run_enter("idx:storefront-sessions", demo_mode=True)
        self.assertEqual((calls["jump"], calls["resume"]), ([], []))
        self.assertIn("jump to Billing API", status)

    def test_enter_on_a_needs_you_index_row_also_goes_to_the_owner(self):
        calls = {"resume": []}

        async def script(app, pilot):
            os.environ.pop("FLEETWATCH_DEMO", None)
            app.start_resume = lambda sid, here: calls["resume"].append(sid)
            await pilot.press("1")
            app.lists[0].move_to("needs-idx:release-history")
            await pilot.press("enter")
            await pilot.pause(0.3)
        self.board((140, 60), script)
        self.assertEqual(calls["resume"], [demo.RESUME_SIDS[0]])

    def test_a_cursor_on_the_section_header_still_folds(self):
        async def script(app, pilot):
            await pilot.press("1")
            app.lists[0].move_to("needs-idx")
            before = app.lists[0].is_open(app.lists[0].current())
            await pilot.press("enter")
            return before, app.lists[0].is_open(app.lists[0].current())
        before, after = self.board((140, 60), script)
        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
