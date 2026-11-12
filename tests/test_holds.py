"""Snooze and done for to-dos: local state only, hidden from rows and counts, back on a date or a change."""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta

_CONFIG = tempfile.TemporaryDirectory()
os.environ["FLEETWATCH_CONFIG_DIR"] = _CONFIG.name
os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from test_fleetwatch import NOW, dstr, up_env  # noqa: E402
from test_board_polish import BoardCase, walk  # noqa: E402
from fleetwatch import app as fapp, config, data, holds, views  # noqa: E402

WHAT = "Review the quick start rewrite"


def view_with(e, hold=None):
    sections, stale, _w = e.run()
    return data.build_view(sections, NOW, 21, stale, hold)


class HoldsUnitTests(unittest.TestCase):
    def test_snooze_hides_until_its_date_then_returns(self):
        h = holds.Holds()
        h.snooze(["a"], NOW + 3600)
        self.assertEqual(h.status("a", NOW)[0], "snoozed")
        self.assertIsNone(h.status("a", NOW + 3601))

    def test_done_stays_done(self):
        h = holds.Holds()
        h.done(["a"])
        self.assertEqual(h.status("a", NOW + 10 ** 7), ("done", None))

    def test_prune_drops_expired_snoozes_and_holds_on_items_no_longer_reported(self):
        h = holds.Holds()
        h.snooze(["old"], NOW - 5)
        h.snooze(["later"], NOW + 5000)
        h.done(["gone"])
        h.done(["kept"])
        self.assertTrue(h.prune({"later", "kept"}, NOW))
        self.assertEqual(sorted(h.items), ["kept", "later"])
        self.assertFalse(h.prune({"later", "kept"}, NOW))

    def test_the_dict_round_trips_and_ignores_junk(self):
        raw = {"a": {"until": 5.0}, "b": {"done": True}, "c": "x", 3: {"done": True}, "d": {"until": True}}
        self.assertEqual(holds.Holds(raw).as_dict(), {"a": {"until": 5.0}, "b": {"done": True}})

    def test_tomorrow_and_next_monday(self):
        wed = datetime(2026, 11, 11, 15, 0, tzinfo=data.LOCAL_TZ).timestamp()
        self.assertEqual(holds.tomorrow(wed).isoformat(), "2026-11-12")
        self.assertEqual(holds.next_monday(wed).isoformat(), "2026-11-16")
        mon = datetime(2026, 11, 16, 9, 0, tzinfo=data.LOCAL_TZ).timestamp()
        self.assertEqual(holds.next_monday(mon).isoformat(), "2026-11-23")

    def test_a_typed_date_must_be_a_later_day(self):
        self.assertEqual(holds.parse_date("2099-01-02", NOW).isoformat(), "2099-01-02")
        for bad in ("soon", "2001-01-01", dstr(0), ""):
            with self.assertRaises(ValueError):
                holds.parse_date(bad, NOW)

    def test_state_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            h = holds.Holds()
            h.snooze(["s:1"], NOW + 99)
            h.done(["s:2"])
            state, _ = config.load_state(path)
            state["holds"] = h.as_dict()
            self.assertTrue(config.save_state(state, path))
            again, warn = config.load_state(path)
            self.assertIsNone(warn)
            self.assertEqual(holds.Holds(again["holds"]).as_dict(), h.as_dict())


class ViewTests(unittest.TestCase):
    ITEMS = [{"what": "late", "due": dstr(-2), "yours": True}, {"what": "now", "due": dstr(0), "yours": True},
             {"what": "undated", "yours": True}, {"what": "later", "due": dstr(5), "yours": True}]

    def keys(self, view):
        return {r["what"]: r["keys"] for r in data.timeline_rows(view["timeline"])}

    def test_held_items_leave_the_rows_and_every_count(self):
        e = up_env(self.ITEMS)
        full = view_with(e)
        keys = self.keys(full)
        h = holds.Holds()
        h.done(keys["late"])
        h.snooze(keys["undated"], NOW + 86400)
        view = view_with(e, h)
        self.assertEqual(view["task_counts"], {"overdue": 0, "today": 1, "undated": 0})
        self.assertEqual({t["what"] for t in view["for_you"][0]["tasks"]}, {"now"})
        self.assertEqual({r["what"] for r in view["held"]}, {"late", "undated"})
        self.assertEqual(views.tab_counts(view, [])[1], 2)
        self.assertEqual(view["item_keys"], full["item_keys"])

    def test_an_expired_snooze_is_visible_again(self):
        e = up_env(self.ITEMS)
        keys = self.keys(view_with(e))
        h = holds.Holds()
        h.snooze(keys["late"], NOW - 1)
        self.assertEqual(view_with(e, h)["task_counts"]["overdue"], 1)

    def test_an_edited_item_is_a_new_item(self):
        e = up_env(self.ITEMS)
        h = holds.Holds()
        h.done(self.keys(view_with(e))["late"])
        edited = up_env([dict(self.ITEMS[0], due=dstr(-1))] + self.ITEMS[1:])
        self.assertEqual(view_with(edited, h)["task_counts"]["overdue"], 1)

    def test_the_key_is_the_session_id_plus_a_hash_and_does_not_hold_the_text(self):
        key = self.keys(view_with(up_env(self.ITEMS)))["late"][0]
        sid, digest = key.split(":")
        self.assertEqual(sid, "s1")
        self.assertRegex(digest, r"^[0-9a-f]{12}$")
        self.assertNotIn("late", key)


class BoardHoldTests(BoardCase):
    def status_hash(self):
        out = {}
        for f in sorted(os.listdir(self.paths["status"])):
            p = os.path.join(self.paths["status"], f)
            if os.path.isfile(p):
                out[f] = hashlib.sha1(open(p, "rb").read()).hexdigest()
        return out

    @staticmethod
    def todo(app, what=WHAT):
        return [n for n in walk(app.lists[views.TODO_TAB].nodes) if n.todo and n.todo[1] == what][0]

    def test_x_marks_done_hides_the_row_and_the_counts_follow(self):
        before = self.status_hash()

        async def script(app, pilot):
            await pilot.press("2")
            n_before = app.tab_counts()[1]
            over_before = app.snapshot.header.plain
            app.lists[1].move_to(self.todo(app).key)
            await pilot.press("x")
            await pilot.pause(0.3)
            return (app.lists[1].visible_text(), app.tab_counts()[1], n_before, app.footer_plain,
                    app.state["holds"], app.status_text, over_before)
        text, after, before_n, footer, saved, status, _hdr = self.board((140, 50), script)
        self.assertNotIn(WHAT, text)
        self.assertEqual(after, before_n - 1)
        self.assertIn("0 snoozed · 1 done (u shows them)", footer)
        self.assertEqual(len(saved), 1)
        self.assertIn("marked done", status)
        self.assertEqual(self.status_hash(), before)         # no status file was touched

    def test_u_shows_the_hidden_item_dimmed_and_x_puts_it_back(self):
        async def script(app, pilot):
            await pilot.press("2")
            app.lists[1].move_to(self.todo(app).key)
            await pilot.press("x")
            await pilot.pause(0.2)
            await pilot.press("u")
            await pilot.pause(0.2)
            shown = app.lists[1].visible_text()
            footer = app.footer_plain
            held = [n for n in walk(app.lists[1].nodes) if n.held]
            app.lists[1].move_to(held[0].key)
            await pilot.press("x")
            await pilot.pause(0.2)
            return shown, footer, app.lists[1].visible_text(), app.state["holds"], app.status_text
        shown, footer, after, saved, status = self.board((140, 50), script)
        self.assertIn("Snoozed and done · 1", shown)
        self.assertIn(WHAT, shown)
        self.assertIn("· done", shown)
        self.assertIn("u hides them", footer)
        self.assertIn(WHAT, after)
        self.assertNotIn("Snoozed and done", after)
        self.assertEqual(saved, {})
        self.assertIn("back on your list", status)

    def test_s_offers_tomorrow_next_week_and_a_typed_date(self):
        async def script(app, pilot):
            await pilot.press("2")
            app.lists[1].move_to(self.todo(app).key)
            await pilot.press("S")
            await pilot.pause(0.3)
            screen = app.screen
            labels = [str(o.prompt) for o in screen.query_one("#choices").options]
            await pilot.press("enter")                      # tomorrow
            await pilot.pause(0.3)
            return labels, app.footer_plain, app.state["holds"], app.status_text
        labels, footer, saved, status = self.board((140, 50), script)
        self.assertTrue(labels[0].startswith("tomorrow"))
        self.assertTrue(labels[1].startswith("next week"))
        self.assertEqual(labels[2], "a date…")
        self.assertIn("1 snoozed · 0 done", footer)
        (until,) = [v["until"] for v in saved.values()]
        self.assertEqual(until, holds.day_start(holds.tomorrow(time.time())))
        self.assertIn("snoozed until", status)

    def test_a_typed_date_is_checked_and_used(self):
        async def script(app, pilot):
            await pilot.press("2")
            app.lists[1].move_to(self.todo(app).key)
            await pilot.press("S")
            await pilot.pause(0.2)
            await pilot.press("down", "down", "enter")      # "a date…"
            await pilot.pause(0.2)
            for ch in "yesterday":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause(0.2)
            problem = str(app.screen.query_one("#problem").render())
            still_open = isinstance(app.screen, fapp.SnoozeScreen)
            box = app.screen.query_one("#when")
            box.value = (datetime.now() + timedelta(days=30)).date().isoformat()
            await pilot.press("enter")
            await pilot.pause(0.3)
            return problem, still_open, app.state["holds"]
        problem, still_open, saved = self.board((140, 50), script)
        self.assertIn("YYYY-MM-DD", problem)
        self.assertTrue(still_open)
        self.assertEqual(len(saved), 1)

    def test_a_hold_survives_a_restart(self):
        async def first(app, pilot):
            await pilot.press("2")
            app.lists[1].move_to(self.todo(app).key)
            await pilot.press("x")
            await pilot.pause(0.2)
            return True
        self.board((140, 50), first)

        async def second(app, pilot):
            await pilot.press("2")
            return app.lists[1].visible_text(), app.footer_plain
        text, footer = self.board((140, 50), second)
        self.assertNotIn(WHAT, text)
        self.assertIn("1 done", footer)

    def test_a_snooze_comes_back_on_its_date(self):
        async def first(app, pilot):
            await pilot.press("2")
            node = self.todo(app)
            app.holds.snooze(node.todo[0], time.time() + 3600)
            app._holds_changed("test")
            await pilot.pause(0.1)
            return WHAT in app.lists[1].visible_text()
        self.assertFalse(self.board((140, 50), first))
        state = json.load(open(os.path.join(os.environ["FLEETWATCH_CONFIG_DIR"], "state.json")))
        (key,) = state["holds"]
        state["holds"][key] = {"until": time.time() - 5}
        json.dump(state, open(os.path.join(os.environ["FLEETWATCH_CONFIG_DIR"], "state.json"), "w"))

        async def second(app, pilot):
            await pilot.press("2")
            return app.lists[1].visible_text(), app.state["holds"]
        text, saved = self.board((140, 50), second)
        self.assertIn(WHAT, text)
        self.assertEqual(saved, {})                          # the expired snooze is forgotten

    def test_a_done_item_returns_when_the_session_changes_it(self):
        async def script(app, pilot):
            await pilot.press("2")
            app.lists[1].move_to(self.todo(app).key)
            await pilot.press("x")
            await pilot.pause(0.2)
            gone = WHAT not in app.lists[1].visible_text()
            path = os.path.join(self.paths["status"], "docs.json")
            doc = json.load(open(path))
            for it in doc["upcoming"]:
                if it["what"] == WHAT:
                    it["what"] = WHAT + " again"
            json.dump(doc, open(path, "w"))
            app.refresh_now()
            await pilot.pause(1.5)
            return gone, app.lists[1].visible_text(), app.state["holds"]
        gone, text, saved = self.board((140, 50), script)
        self.assertTrue(gone)
        self.assertIn(WHAT + " again", text)
        self.assertEqual(saved, {})

    def test_x_and_s_on_a_needs_task_row(self):
        async def script(app, pilot):
            await pilot.press("e")
            tasks = [n for n in walk(app.lists[0].nodes) if n.todo]
            first = tasks[0]
            app.lists[0].move_to(first.key)
            await pilot.press("x")
            await pilot.pause(0.3)
            return len(tasks), len([n for n in walk(app.lists[0].nodes) if n.todo]), first.todo[1]
        n_before, n_after, what = self.board((140, 50), script)
        self.assertEqual(n_after, n_before - 1)

    def test_x_off_a_to_do_says_where_it_works(self):
        async def script(app, pilot):
            await pilot.press("3", "x")
            await pilot.pause(0.1)
            return app.status_text
        self.assertIn("highlight one on Your to-dos", self.board((140, 40), script))
