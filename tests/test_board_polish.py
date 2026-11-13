"""Board polish: question previews, header counts, spelled-out overdue, Resume details, short next steps."""
import asyncio
import os
import re
import sys
import tempfile
import time
import unittest

_CONFIG = tempfile.TemporaryDirectory()
os.environ["FLEETWATCH_CONFIG_DIR"] = _CONFIG.name
os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from test_fleetwatch import NOW, dstr, iso, up_env  # noqa: E402
from fleetwatch import app as fapp, data, demo, index_health, views  # noqa: E402


def view_of(e):
    sections, stale, _w = e.run()
    return data.build_view(sections, NOW, 21, stale)


def walk(nodes):
    for n in nodes:
        yield n
        yield from walk(n.children)


class QuestionPreviewTests(unittest.TestCase):
    def node(self, qs, **extra):
        e = up_env([], questions=qs, **extra)
        return views.needs_nodes(view_of(e), NOW)[0]

    def test_a_folded_row_previews_the_oldest_question_and_its_age(self):
        n = self.node([{"q": "Newer question about the flag", "since": iso(-600)},
                       {"q": "Should refunds over the limit need a second approver?", "since": iso(-35 * 60)}])
        shut = n.head_shut.plain
        self.assertIn("Should refunds over the limit need a second approver? · 35m", shut)
        self.assertNotIn("Newer question", shut)

    def test_a_long_question_is_cut_near_sixty_characters(self):
        n = self.node([{"q": "Is it fine to drop support for the old payment sheet in this release or must we wait "
                             "for the partner migration to finish first?", "since": iso(-3600)}])
        preview = [ln for ln in n.head_shut.plain.split("\n") if "…" in ln][0]
        text = preview.strip().split(" · ")[0]
        self.assertLessEqual(len(text), 61)
        self.assertTrue(text.endswith("…"))
        self.assertTrue(text.startswith("Is it fine to drop support"))

    def test_an_unstamped_question_uses_the_status_age(self):
        n = self.node(["Which branch should the fix land on?"])
        self.assertRegex(n.head_shut.plain, r"Which branch should the fix land on\? · \d+s")

    def test_the_open_row_keeps_its_own_head_and_lists_every_question(self):
        n = self.node(["first", "second"])
        self.assertNotIn("first", n.head.plain)
        self.assertEqual([c.head.plain for c in n.children if ":q:" in c.key][0][:3], "1. ")

    def test_no_questions_no_preview(self):
        e = up_env([{"what": "A", "yours": True}])
        n = views.needs_nodes(view_of(e), NOW)[0]
        self.assertNotIn("\n", (n.head_shut or n.head).plain)


class OverdueInWordsTests(unittest.TestCase):
    def head(self, items):
        n = views.needs_nodes(view_of(up_env(items)), NOW)[0]
        return n.head.plain

    def test_overdue_is_spelled_out(self):
        head = self.head([{"what": "A", "due": dstr(-2), "yours": True}])
        self.assertIn("1 overdue", head)
        self.assertNotIn("1 task", head)

    def test_overdue_then_the_other_tasks(self):
        head = self.head([{"what": "A", "due": dstr(-2), "yours": True}, {"what": "B", "yours": True},
                          {"what": "C", "yours": True}])
        self.assertIn("1 overdue · 2 tasks", head)

    def test_due_today_is_spelled_out(self):
        head = self.head([{"what": "A", "due": dstr(0), "yours": True}])
        self.assertIn("1 due today", head)

    def test_only_undated_tasks_keep_the_task_count(self):
        head = self.head([{"what": "A", "yours": True}, {"what": "B", "yours": True}])
        self.assertIn("2 tasks", head)
        self.assertNotIn("overdue", head)

    def test_overdue_is_red_and_the_word_is_there_without_color(self):
        n = views.needs_nodes(view_of(up_env([{"what": "A", "due": dstr(-2), "yours": True}])), NOW)[0]
        spans = [(n.head.plain[s.start:s.end], str(s.style)) for s in n.head.spans]
        self.assertIn(("1 overdue", views.RED), spans)


class ResumeDetailTests(unittest.TestCase):
    ENTRY = {"sid": "d3a00002-0000-4000-8000-000000000002", "name": "Pricing Page", "persona": None,
             "last": NOW - 3600, "cwd": "/home/x/code/pricing", "parked": False, "parked_at": None, "note": "",
             "path": "/home/x/.claude/projects/-p/d3a00002.jsonl", "archived": False, "held": None,
             "left": {"was": "annual plans", "next": None, "questions": 0, "updated": NOW - 3600}, "job": None, "folder_ok": True}

    def test_the_row_body_has_no_session_id_line(self):
        from fleetwatch import resume
        node = views.resume_node(dict(self.ENTRY), NOW)
        body = "\n".join(t.plain for _l, t in node.body)
        self.assertNotIn("session d3a00002", body)
        self.assertNotIn("d3a00002", body)

    def test_the_detail_text_carries_the_id_folder_and_transcript(self):
        text = views.resume_details(dict(self.ENTRY), NOW).plain
        self.assertIn("d3a00002-0000-4000-8000-000000000002", text)
        self.assertIn("/home/x/code/pricing", text)
        self.assertIn("/home/x/.claude/projects/-p/d3a00002.jsonl", text)
        self.assertIn("Pricing Page", text)


class ShortNextStepTests(unittest.TestCase):
    def test_a_problem_row_shows_one_command_and_says_y_copies(self):
        now = time.time()
        h = index_health.Health(name="release-history", group="team", kind="session", state="failed",
                                reason="build", finished=now - 600, stale_after=3600)
        node = views.index_node(h, now)
        lines = [t.plain for _l, t in node.body if t.plain.startswith("next:")]
        self.assertEqual(lines, ["next: fleetwatch index status release-history (y copies)"])
        self.assertLessEqual(len("  " + lines[0]), 100)

    def test_each_problem_has_its_command(self):
        now = time.time()
        old = now - 5 * 3600

        def h(**kw):
            base = dict(name="ix", group="team", kind="session", state="ok", finished=now - 60, stale_after=3600)
            base.update(kw)
            return index_health.Health(**base)
        self.assertEqual(h(state="failed", reason="build").next_commands(now), ["fleetwatch index status ix"])
        self.assertEqual(h(protection="refused", refusal="settings").next_commands(now),
                         ["fleetwatch index verify ix"])
        self.assertEqual(h(finished=old, scheduled=False).next_commands(now),
                         ["fleetwatch index schedule install"])
        self.assertEqual(h(finished=old, scheduled=True).next_commands(now),
                         ["fleetwatch index schedule status"])
        self.assertEqual(h(state="never", finished=None).next_commands(now), ["fleetwatch index refresh ix"])

    def test_the_needs_row_uses_the_short_line_too(self):
        now = time.time()
        h = index_health.Health(name="advising-notes", group="ferpa", kind="session", state="ok",
                                finished=now - 60, stale_after=3600, protection="refused", refusal="settings")
        group = views.index_problem_group([h], now)
        body = [t.plain for _l, t in group.children[0].body]
        self.assertIn("next: fleetwatch index verify advising-notes (y copies)", body)


class BoardCase(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        self.tmp, self.paths = demo.prepare()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.saved)))

    def board(self, size, script):
        async def go():
            app = fapp.FleetwatchApp(
                lambda: fapp.gather(self.paths, data.parse_window("24h"), data.DEFAULT_HORIZON_DAYS),
                3600, paths=self.paths)
            async with app.run_test(size=size) as pilot:
                await pilot.pause(1.5)
                return await script(app, pilot)
        return asyncio.run(go())


class HeaderCountsAgreeWithRowsTests(BoardCase):
    def numbers(self, header):
        return {word: int(n) for n, word in re.findall(
            r"(\d+) (working|quiet|idle|not running|index problems?|watch flags?|questions?|stuck|overdue|"
            r"due today|need a date|needs a date)", header)}

    def test_the_header_numbers_equal_the_rows(self):
        async def script(app, pilot):
            return app.snapshot.header.plain, app.snapshot.counts, app.lists
        header, _counts, lists = self.board((200, 50), script)
        got = self.numbers(header)
        needs, sessions = lists[0].nodes, lists[2].nodes
        flat = list(walk(needs))
        by_state = {n.key.split(":")[1]: len(n.children) for n in sessions if n.key.startswith("sesg:")
                    and n.key.split(":")[1] in ("working", "quiet", "idle")}
        self.assertEqual(got["working"], by_state.get("working", 0))
        self.assertEqual(got.get("quiet", 0), by_state.get("quiet", 0))
        self.assertEqual(got["idle"], by_state.get("idle", 0))
        self.assertEqual(got.get("question", got.get("questions", 0)),
                         sum(1 for n in flat if ":q:" in n.key))
        idx = [n for n in needs if n.key == "needs-idx"]
        self.assertEqual(got.get("index problems", got.get("index problem", 0)),
                         len(idx[0].children) if idx else 0)
        wat = [n for n in needs if n.key == "needs-watch"]
        self.assertEqual(got.get("watch flags", got.get("watch flag", 0)),
                         len(wat[0].children) if wat else 0)

    def test_idle_in_the_header_matches_the_sessions_tab_even_when_idle_sessions_need_you(self):
        async def script(app, pilot):
            return app.snapshot.header.plain, app.lists[2].nodes
        header, sessions = self.board((200, 50), script)
        idle = [n for n in sessions if n.key == "sesg:idle"][0]
        self.assertGreater(len(idle.children), 0)
        self.assertIn("%d idle" % len(idle.children), header)

    def test_the_header_states_are_the_sessions_tab_total(self):
        async def script(app, pilot):
            return app.snapshot.header.plain, app.snapshot.view
        header, view = self.board((200, 50), script)
        got = self.numbers(header)
        self.assertEqual(got["working"] + got.get("quiet", 0) + got["idle"], len(view["rows"]))


class HelpAt80ColumnsTests(BoardCase):
    KEYS = ("1 to 5, Tab, Shift-Tab", "Enter, g", "click", "double click", "/", "Ctrl-K", "Space", "z", "e / c", "y", "s", "G", "m",
            "S", "x", "u", "P", "p", "i", "r", "?", "q, Ctrl-C", "right arrow, left arrow")

    def test_every_line_fits_each_width(self):
        for width in (40, 60, 80, 120):
            lines = fapp.help_lines(width)
            self.assertLessEqual(max(len(ln) for ln in lines), width, width)

    def test_every_key_is_listed_at_80_columns(self):
        text = "\n".join(fapp.help_lines(76))
        for key in self.KEYS:
            self.assertRegex(text, r"(?m)^  %s( |$)" % re.escape(key), key)

    def test_help_at_80x24_scrolls_to_the_last_line_without_sideways_overflow(self):
        async def script(app, pilot):
            await pilot.press("question_mark")
            await pilot.pause(0.3)
            screen = app.screen
            box = screen.query_one("#helpbox")
            top = (box.scroll_y, box.max_scroll_y, box.virtual_size.width, box.size.width, box.max_scroll_x)
            seen = [list(screen.lines)]
            await pilot.press("end")
            await pilot.pause(0.2)
            bottom = box.scroll_y
            await pilot.press("home")
            await pilot.pause(0.2)
            home = box.scroll_y
            await pilot.press("pagedown")
            await pilot.pause(0.2)
            paged = box.scroll_y
            await pilot.press("escape")
            await pilot.pause(0.2)
            return top, seen[0], bottom, home, paged, type(app.screen).__name__
        top, lines, bottom, home, paged, after = self.board((80, 24), script)
        scroll_y, max_y, vwidth, width, max_x = top
        self.assertEqual(scroll_y, 0)
        self.assertGreater(max_y, 0)                  # more help than one screen, so it scrolls
        self.assertEqual(max_x, 0)                    # and never sideways
        self.assertLessEqual(max(len(ln) for ln in lines), 78)
        self.assertEqual(bottom, max_y)
        self.assertEqual(home, 0)
        self.assertGreater(paged, 0)
        self.assertNotEqual(after, "HelpScreen")

    def test_question_mark_closes_it_too(self):
        async def script(app, pilot):
            await pilot.press("question_mark")
            await pilot.pause(0.2)
            await pilot.press("question_mark")
            await pilot.pause(0.2)
            return type(app.screen).__name__
        self.assertNotEqual(self.board((80, 24), script), "HelpScreen")


class MouseTests(BoardCase):
    def rows(self, app):
        lst = app.lists[0]
        return lst, [lst._lines[i][0] for i in range(len(lst._lines)) if lst._lines[i][0]]

    def test_a_single_click_selects_and_does_not_fold(self):
        async def script(app, pilot):
            lst = app.lists[0]
            keys = [k for k in lst._order]
            second = keys[1]
            y = lst._spans[second][0]
            was_open = lst.is_open(lst._by_key[second])
            await pilot.click("#tab0", offset=(20, y))
            await pilot.pause(0.2)
            return lst.cursor, second, was_open, lst.is_open(lst._by_key[second])
        cursor, second, was_open, now_open = self.board((140, 40), script)
        self.assertEqual(cursor, second)
        self.assertEqual(was_open, now_open)

    def test_a_click_on_the_arrow_folds_and_unfolds(self):
        async def script(app, pilot):
            lst = app.lists[0]
            key = next(k for k in lst._order if lst._by_key[k].expandable and not lst._by_key[k].section)
            y = lst._spans[key][0]
            before = lst.is_open(lst._by_key[key])
            await pilot.click("#tab0", offset=(0, y))
            await pilot.pause(0.2)
            mid = lst.is_open(lst._by_key[key])
            y = lst._spans[key][0]
            await pilot.click("#tab0", offset=(1, y))
            await pilot.pause(0.2)
            return before, mid, lst.is_open(lst._by_key[key])
        before, mid, end = self.board((140, 40), script)
        self.assertNotEqual(before, mid)
        self.assertEqual(before, end)

    def test_a_double_click_does_what_enter_does(self):
        async def script(app, pilot):
            lst = app.lists[0]
            calls = []
            app.action_enter = lambda: calls.append(lst.cursor)
            key = [k for k in lst._order if not lst._by_key[k].section][0]
            y = lst._spans[key][0]
            await pilot.click("#tab0", offset=(20, y), times=2)
            await pilot.pause(0.2)
            return calls, key, lst.is_open(lst._by_key[key])
        calls, key, opened = self.board((140, 40), script)
        self.assertEqual(calls, [key])
        self.assertFalse(opened)                      # the double click did not open the row first

    def test_a_double_click_on_a_section_header_folds_it(self):
        async def script(app, pilot):
            await pilot.press("2")
            lst = app.lists[1]
            key = next(k for k in lst._order if lst._by_key[k].section)
            y = lst._spans[key][0]
            before = lst.is_open(lst._by_key[key])
            await pilot.click("#tab1", offset=(20, y), times=2)
            await pilot.pause(0.2)
            return before, lst.is_open(lst._by_key[key])
        before, after = self.board((140, 40), script)
        self.assertNotEqual(before, after)

    def test_a_click_on_a_tab_switches_to_it(self):
        async def script(app, pilot):
            bar = app.query_one(fapp.TabBar)
            a, b = bar._spans[2]
            await pilot.click("#tabs", offset=((a + b) // 2 + 1, 0))
            await pilot.pause(0.2)
            return app.active
        self.assertEqual(self.board((140, 40), script), 2)


class NoColorTests(BoardCase):
    def test_the_board_strips_color_and_keeps_the_words(self):
        os.environ["NO_COLOR"] = "1"

        async def script(app, pilot):
            from rich.color import Color
            from textual.color import Color as TC
            colors = set()
            for strip in app.screen._compositor.render_strips():
                for f in app._filters:
                    strip = strip.apply_filter(f, TC.parse("black"))
                for seg in strip:
                    if seg.style:
                        colors |= {seg.style.color, seg.style.bgcolor}
            return app.no_color, colors - {None, Color.default()}, app.lists[0].visible_text()
        no_color, colors, needs = self.board((140, 40), script)
        self.assertTrue(no_color)
        self.assertEqual(colors, set())
        self.assertIn("stuck on a prompt", needs)
        self.assertIn("overdue", needs)

    def test_an_empty_value_means_color_stays_on(self):
        os.environ["NO_COLOR"] = ""

        async def script(app, pilot):
            return app.no_color, "NO_COLOR" in os.environ
        self.assertEqual(self.board((100, 30), script), (False, False))

    def test_once_output_drops_ansi_color_under_no_color(self):
        import io
        from unittest import mock
        from fleetwatch import render_text
        for value, want in (("1", False), (None, True), ("", True)):
            env = {"NO_COLOR": value} if value is not None else {}
            out = io.StringIO()
            out.isatty = lambda: True
            with mock.patch.dict(os.environ, env), mock.patch.object(sys, "stdout", out):
                if value is None:
                    os.environ.pop("NO_COLOR", None)
                text = render_text.snapshot(data.parse_window("24h"), False, 21, False, self.paths)
            self.assertEqual("\033[" in text, want, repr(value))

    def test_meaning_is_in_words_not_only_color(self):
        async def script(app, pilot):
            self.index_nodes = list(app.lists[4].nodes)
            return {i: lst.visible_text() for i, lst in enumerate(app.lists)}
        text = self.board((200, 60), script)
        index_rows = " ".join(n.head.plain for n in walk(self.index_nodes))
        for word in ("refresh FAILED", "protection REFUSED", "STALE"):
            self.assertIn(word, index_rows)
        self.assertIn("stuck on a prompt", text[0])
        self.assertIn("overdue", text[0].lower())
        for word in ("last refresh failed", "protection check refused", "stale"):
            self.assertIn(word, text[0])


class ResumeDetailsKeyTests(BoardCase):
    def test_i_shows_the_session_id_folder_and_transcript(self):
        async def script(app, pilot):
            await pilot.press("4")
            await pilot.pause(0.5)
            rows = app.lists[3].visible_text()
            await pilot.press("i")
            await pilot.pause(0.3)
            shown = type(app.screen).__name__, str(app.screen.query_one("#detailtext").render())
            footer = app.footer_plain
            await pilot.press("escape")
            await pilot.pause(0.2)
            return rows, shown, footer, type(app.screen).__name__
        rows, (screen, text), footer, after = self.board((140, 40), script)
        self.assertNotIn("session d3a0", rows)
        self.assertEqual(screen, "DetailScreen")
        self.assertRegex(text, r"session ID +d3a0000[23]-")
        self.assertIn("transcript", text)
        self.assertIn(".jsonl", text)
        self.assertIn("i details", footer)
        self.assertNotEqual(after, "DetailScreen")

    def test_the_opened_row_keeps_was_and_the_folder(self):
        async def script(app, pilot):
            await pilot.press("4")
            await pilot.pause(0.5)
            await pilot.press("e")
            await pilot.pause(0.2)
            return app.lists[3].visible_text()
        text = self.board((140, 60), script)
        self.assertIn("was: ", text)
        self.assertIn("~/code/", text)


class YCopiesTheNextCommandTests(BoardCase):
    def copied(self, key):
        async def script(app, pilot):
            got = []
            app.copy_to_clipboard = got.append
            await pilot.press("5")
            app.lists[4].move_to(key)
            await pilot.press("y")
            await pilot.pause(0.2)
            return got
        return self.board((140, 40), script)

    def test_a_problem_row_copies_its_command(self):
        self.assertEqual(self.copied("idx:release-history"), ["fleetwatch index status release-history"])

    def test_a_healthy_row_copies_a_search(self):
        self.assertEqual(self.copied("idx:storefront-sessions"), ['fleetwatch search --index storefront-sessions "'])


class HeaderFitsTests(BoardCase):
    def shown(self, width):
        async def script(app, pilot):
            return "".join(seg.text for seg in app.query_one("#top").render_line(0)).rstrip()
        return self.board((width, 30), script)

    def test_the_states_stay_visible_at_120_and_200_columns(self):
        for width in (120, 200):
            line = self.shown(width)
            self.assertRegex(line, r"\d+ working", width)
            self.assertRegex(line, r"\d+ idle", width)
            self.assertLessEqual(len(line), width)

    def test_at_80_columns_the_needs_counts_and_index_and_watch_counts_lead(self):
        line = self.shown(80)
        self.assertIn("3 idx", line)
        self.assertLessEqual(len(line), 80)
