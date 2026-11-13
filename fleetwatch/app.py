"""fleetwatch live view: a Textual app with tabs along the bottom and folding sections."""
import asyncio
import os
import signal
import sys
import textwrap
import time
from datetime import datetime

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.geometry import Size
from textual.screen import ModalScreen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widgets import ContentSwitcher, Input, OptionList, Static
from textual.widgets.option_list import Option

from . import config, data, find, holds, index_health, iterm, park, prune, radar, resume, views, watches

STATUS_SECONDS = 10.0
RESCAN_SECONDS = 60.0
# Below this size the radar is hidden: it floats over the bottom-right corner of the lists, so it only
# appears where the rows to its left are not crowded.
RADAR_MIN_ROWS, RADAR_MIN_COLS = 32, 100
RADAR_BOTTOM_GAP = 2      # the hint line and the tab bar sit below it
TAB_OPEN_RESCAN_SECONDS = 15.0
# A window just opened takes a few seconds to show up as a live session. Until it can, this is what
# stops a second press from putting two processes on one transcript.
REOPEN_GUARD_SECONDS = 30.0
FIND_NOUNS = ("a row on Needs you", "a to-do", "a session", "a session to resume", "an index")
FIND_ROW_ROOM = 60                        # the find box's row width less its separators
CURSOR = "reverse"
OPEN, SHUT = "▾", "▸"

# (keys, what they do). The board wraps each description under its own column, so every line fits the
# terminal, and the help screen scrolls for the rest.
HELP_KEYS = [
    ("1 to 5, Tab, Shift-Tab", "switch tabs (or click one)"),
    ("up/down, j/k", "move the cursor; PgUp/PgDn and the wheel scroll"),
    ("Enter, g", "jump to the row's session in iTerm2 (helpers: their own tab, else the parent's); on a "
                 "section header, fold it; on a row tagged 'not running', offer to reopen it"),
    ("click", "select the row; click a tab to switch to it; click the ▸ or ▾ arrow to fold"),
    ("double click", "jump like Enter"),
    ("/", "find a row on this tab by part of its text; Enter does what Enter does on that row"),
    ("Ctrl-K", "find a live session from any tab, Enter jumps"),
    ("Space", "open or close the row (▸ closed, ▾ open)"),
    ("right arrow, left arrow", "open, close"),
    ("z", "fold all: open every row and group in this tab, or close them all"),
    ("e / c", "open all / close all rows in this tab"),
    ("y", "on the Indexes tab (5): copy the highlighted index's next command, or a search command for a "
          "healthy one"),
    ("s", "change the sort order of this tab (remembered)"),
    ("G", "group every tab by project folder, or ungroup; on To-dos, cycle by day, by project folder and "
          "no groups"),
    ("m", "move the highlighted session to another group"),
    ("S", "snooze the highlighted to-do (Your to-dos, or a task on Needs you): tomorrow, next week or a "
          "date; it comes back by itself"),
    ("x", "mark the highlighted to-do done; it stays hidden until the session reports it changed or gone"),
    ("u", "on To-dos or a to-do row: show or hide the snoozed and done items (S or x on one puts it back); "
          "elsewhere, unpark the highlighted session"),
    ("P", "prune unnamed finished sessions' status files (y moves them, n adds the named ones, Esc cancels)"),
    ("p", "park the highlighted session with a note"),
    ("i", "on the Resume tab: details of the row (session ID, full folder, transcript)"),
    ("r", "refresh now"),
    ("?", "show or hide this help"),
    ("q, Ctrl-C", "quit"),
]
HELP_NOTES = [
    "The Indexes tab (5) shows each index's health and its owner session: status and counts only, never what "
    "is in it. Enter on an index row jumps to its owner's iTerm tab, or resumes the owner when it is not "
    "running; with no owner it says which command to run. Space folds the row.",
    "A Watches section on Needs you lists what a stopped session's watches found on GitHub: counts and issue "
    "or PR numbers only. Enter goes to the owner session, which reads them with `fleetwatch watch report`.",
    "Snoozed and done to-dos are the board's own memory; it never edits a session's status file.",
]
HELP_RESUME = [
    ("Enter", "resume here: fleetwatch exits and this terminal becomes the session"),
    ("o", "resume in a new iTerm window (Ctrl-O in the find box)"),
    ("/", "find by part of a name or folder; Enter resumes"),
    ("i", "details: session ID, full folder, transcript"),
]
HELP_HOTKEY = [("Ctrl-Shift-F", "hides this window; q quits fleetwatch; a resume always opens a new window")]
HELP_COLUMN = 26
HELP_CLOSE = "Esc, q or ? closes this help. Up, down, PgUp, PgDn, Home and End scroll."


def help_lines(width):
    """The help as plain lines no wider than `width`: each key's description wraps under its own column."""
    col = min(HELP_COLUMN, max(width // 3, 8))
    room = max(width - col - 2, 12)

    def entries(rows):
        out = []
        for keys, what in rows:
            parts = textwrap.wrap(what, room) or [""]
            lead = ("  " + keys).ljust(col + 2) if len(keys) <= col - 1 else "  " + keys + "\n" + " " * (col + 2)
            first = lead.split("\n")
            if len(first) == 2:
                out.append(first[0])
                lead = first[1]
            out.append(lead + parts[0])
            out += [" " * (col + 2) + p for p in parts[1:]]
        return out

    lines = ["fleetwatch keys", ""] + entries(HELP_KEYS) + [""]
    for note in HELP_NOTES:
        lines += textwrap.wrap(note, width) + [""]
    lines += ["On the Resume tab (4):"] + entries(HELP_RESUME) + ["", "In the fleetwatch hotkey window:"]
    lines += entries(HELP_HOTKEY) + [""] + textwrap.wrap(HELP_CLOSE, width)
    return lines


HELP_LINES = help_lines(120)


TODO_GROUP_NOTE = {
    "time": "  ·  grouped by day (G: project folder, then none)",
    "folder": "  ·  grouped by project folder (G: no groups, then by day)",
    "none": "",
}
HINTS = [("⏎", "go to session"), ("/", "find"), ("␣", "fold"), ("z", "fold all"), ("s", "sort"), ("G", "groups"), ("m", "move"),
         ("r", "refresh"), ("?", "help"), ("q", "quit"), ("⌃⇧F", "hide")]
CORE_HINTS = ("⏎", "␣", "?", "q")
DROP_ORDER = ("⌃⇧F", "r", "G", "m", "s", "z")     # least important first
RESUME_HINTS = [("⏎", "resume here"), ("o", "new window"), ("/", "find"), ("p", "park"), ("u", "unpark"),
                ("i", "details"), ("␣", "fold"), ("z", "fold all"), ("s", "sort"), ("?", "help"), ("q", "quit"),
                ("⌃⇧F", "hide")]
HOTKEY_RESUME_HINTS = [("⏎", "new window")] + RESUME_HINTS[2:]
RESUME_DROP = ("⌃⇧F", "s", "z", "␣", "u", "i", "p")
INDEX_HINTS = [("⏎", "go to owner"), ("y", "copy"), ("␣", "details"), ("z", "fold all"), ("s", "sort"), ("?", "help"),
               ("q", "quit"), ("⌃⇧F", "hide")]
INDEX_DROP = ("⌃⇧F", "s", "z", "␣")


def footer_text(width, show_move, hints=HINTS, drop_order=DROP_ORDER):
    """The key-hint line: whole hints only, least important dropped first, never wrapped."""
    keep = [h for h in hints if h[0] != "m" or show_move]
    drop = [k for k in drop_order]

    def render(hints):
        return "  ".join("%s %s" % h for h in hints)
    while len(render(keep)) > width and drop:
        gone = drop.pop(0)
        keep = [h for h in keep if h[0] != gone]
    return Text(render(keep), style="dim", no_wrap=True, overflow="ellipsis")


class Snapshot:
    """One collection: the view, counts, header, groups and the data the jump needs."""

    def __init__(self, sections, stale, now, warnings, horizon, jump=None, groups=None, cfg_warning=None,
                 prune_plan=None, held=None, indexes=None, watch_rows=None):
        self.now = now
        self.watches = list(watch_rows or [])
        self.indexes = indexes if indexes is not None else index_health.Result()
        self.held = held or {}
        self.prune_plan = prune_plan or prune.Plan({}, now, config.default_prune())
        self.stale = stale
        self.sections, self.horizon = sections, horizon
        self.base_warnings = list(warnings)
        self.jump = jump or {"ttys": {}, "iterm": None, "err": "not read"}
        self.groups = groups if groups is not None else config.Groups()
        self.cfg_warning = cfg_warning
        self.derive(None)

    def derive(self, holds):
        """Build the view, counts, header and lookups, hiding what `holds` (holds.Holds) snoozed or marked
        done. The board calls this again whenever a hold changes, so every number follows the rows."""
        now, stale = self.now, self.stale
        self.holds = holds
        self.view = data.build_view(self.sections, now, self.horizon, stale, holds)
        self.counts = views.tab_counts(self.view, stale)
        self.warnings = self.base_warnings + self.view["up_warnings"]
        self.records, self.helpers = {}, {}
        self.gone = {x["name"]: x["r"] for x in self.view["for_you"] if x.get("gone")}
        for x in self.view["rows"]:
            self.records.setdefault(x["name"], x["r"])
            for c in x["r"]["children"]:
                self.helpers.setdefault(data.clean(c["name"]), c)
        for r in list(self.view["orphans"]) + list(stale):
            self.helpers.setdefault(data.clean(r["name"]), r)
        self.grouping = views.Grouping(
            {x["name"]: self.groups.of(x["name"], x["r"]["cwd"], x["r"]["sessionId"]) for x in self.view["rows"]},
            self.groups.names())
        bad = len(index_health.problem_rows(self.indexes.rows, now))
        self.header = views.header_text(self.view, self.counts, now, self.warnings, False, bad, len(self.watches))
        self.header_mid = views.header_text(self.view, self.counts, now, self.warnings, False, bad,
                                            len(self.watches), stamp=False)
        self.header_short = views.header_text(self.view, self.counts, now, self.warnings, True, bad,
                                              len(self.watches))

    def _tty(self, rec, name):
        """(tty, None) when the record has an iTerm2 tab, else (None, reason)."""
        if rec is None:
            return None, "%s is not a live session" % name
        if not rec["pid_alive"]:
            return None, "%s is not running" % name
        if self.jump["iterm"] is None:
            return None, "could not reach iTerm2: %s" % (self.jump.get("err") or "no answer")
        tty = self.jump["ttys"].get(rec["pid"])
        if tty not in self.jump["iterm"]:
            return None, "%s is not in an iTerm2 tab" % name
        return tty, None

    def held_target(self, sid, name):
        """(tty, None) for a running session on the Resume tab, by ID since names can repeat."""
        return self._tty(self.held.get(sid), name)

    def target(self, session, helper=None):
        """(tty, None, name raised) or (None, reason, None). A helper's own tab comes first,
        then its parent session's."""
        if helper:
            tty, reason = self._tty(self.helpers.get(helper), helper)
            if tty is not None:
                return tty, None, helper
            if not session:
                return None, reason, None
        tty, reason = self._tty(self.records.get(session), session)
        return (tty, None, session) if tty is not None else (None, reason, None)


def gather(paths, window_s, horizon):
    """Collect everything one refresh needs; runs in a worker thread."""
    sections, stale, now, warnings, records = data.collect_full(window_s, True, paths)
    groups, cfg_warning = config.load_groups()
    settings, prune_warning = config.load_prune()
    warnings += [prune_warning] if prune_warning else []
    plan = prune.make_plan(paths, now, records, settings)
    return Snapshot(sections, stale, now, warnings, horizon, iterm.jump_context(), groups, cfg_warning, plan,
                    resume.held_map(records), index_health.load(), watches.board_rows(now))


class FoldList(ScrollView, can_focus=True):
    """A scrolling list of folding nodes with a cursor. State is keyed by node key, not by row."""

    DEFAULT_CSS = """
    FoldList { scrollbar-gutter: stable; scrollbar-size-vertical: 1; height: 1fr; }
    """

    def __init__(self, empty, **kw):
        super().__init__(**kw)
        self.empty = empty
        self.nodes = []
        self.overrides = {}        # key -> bool, the user's own open/closed choices
        self.cursor = None         # key of the highlighted node
        self.loaded = False
        self._order = []           # visible cursor keys, top to bottom
        self._by_key = {}
        self._spans = {}           # key -> (first line, last line)
        self._glyph_x = {}
        self._strips = []
        self._width = 0
        self.on_fold = None        # called after the user opens or closes something
        self.on_move = None        # called whenever the cursor or the rows change

    # ---- model

    def is_open(self, node):
        return self.overrides.get(node.key, node.start_open) if node.expandable else False

    def set_nodes(self, nodes):
        old_order, old_cursor = list(self._order), self.cursor
        anchor = None
        if old_cursor in self._spans:
            anchor = self._spans[old_cursor][0] - int(self.scroll_y)
        self.nodes, self.loaded = nodes, True
        self._layout()
        if old_cursor not in self._by_key:
            self.cursor = self._neighbour(old_order, old_cursor)
            self._layout()
            anchor = None
        if anchor is not None and self.cursor in self._spans:
            self.scroll_to(y=max(self._spans[self.cursor][0] - anchor, 0), animate=False, immediate=True)
        self._repaint()

    def _neighbour(self, old_order, old_cursor):
        if old_cursor in old_order:
            i = old_order.index(old_cursor)
            for k in old_order[i + 1:] + old_order[:i][::-1]:
                if k in self._by_key:
                    return k
        return self._order[0] if self._order else None

    def _layout(self):
        """Flatten the open nodes into lines at the current width."""
        width = max(self._width, 20)
        console = self.app.console
        self._order, self._by_key, self._spans, lines = [], {}, {}, []
        self._glyph_x = {}          # line index -> column of its fold arrow

        def wrap(label, text, indent, hang=0):
            pad = Text(" " * indent) + label
            room = max(width - pad.cell_len - hang, 8)
            out = []
            for i, part in enumerate(text.wrap(console, room, overflow="fold")):
                lead = pad if i == 0 else Text(" " * (pad.cell_len + hang))
                out.append(lead + part)
            return out or [pad]

        def walk(node, depth):
            self._by_key[node.key] = node
            glyph = (OPEN if self.is_open(node) else SHUT) if node.expandable else " "
            first = len(lines)
            head = node.head if self.is_open(node) or node.head_shut is None else node.head_shut
            if node.expandable:
                self._glyph_x[len(lines)] = depth * 2
            for ln in wrap(Text(glyph + " "), head, depth * 2, node.hang):
                lines.append((node.key, ln))
            self._order.append(node.key)
            if self.is_open(node):
                for label, text in node.body:
                    for ln in wrap(label, text, depth * 2 + 2):
                        lines.append((None, ln))
                for child in node.children:
                    walk(child, depth + 1)
            self._spans[node.key] = (first, first + sum(
                1 for k, _ in lines[first:] if k == node.key) - 1)

        for node in self.nodes:
            walk(node, 0)
        if not lines:
            lines.append((None, Text(self.empty if self.loaded else "Loading…", style="dim")))
        if self.cursor not in self._by_key:
            self.cursor = self._order[0] if self._order else None
        self._lines = lines
        self.virtual_size = Size(width, len(lines))

    def _repaint(self):
        strips = []
        width = max(self._width, 20)
        for key, line in self._lines:
            segs = list(line.render(self.app.console))
            strip = Strip(segs).adjust_cell_length(width)
            if key is not None and key == self.cursor:
                strip = strip.apply_style(self.app.console.get_style(CURSOR))
            strips.append(strip)
        self._strips = strips
        self.refresh()
        if self.on_move:
            self.on_move()

    def render_line(self, y):
        i = int(self.scroll_offset.y) + y
        if 0 <= i < len(self._strips):
            return self._strips[i].crop(int(self.scroll_offset.x), int(self.scroll_offset.x) + self.size.width)
        return Strip.blank(self.size.width)

    def on_resize(self, event):
        width = self.scrollable_content_region.width or event.size.width
        if width != self._width:
            self._width = width
            if self.nodes:
                self._layout()
                self._repaint()

    # ---- cursor and folding

    def _reveal(self):
        if self.cursor not in self._spans:
            return
        a, b = self._spans[self.cursor]
        top, height = int(self.scroll_y), max(self.size.height, 1)
        if a < top:
            self.scroll_to(y=a, animate=False, immediate=True)
        elif b >= top + height:
            self.scroll_to(y=b - height + 1, animate=False, immediate=True)

    def move_to(self, key):
        if key is None:
            return
        self.cursor = key
        self._layout()
        self._repaint()
        self._reveal()

    def move(self, delta):
        if not self._order:
            return
        i = self._order.index(self.cursor) if self.cursor in self._order else 0
        self.move_to(self._order[max(0, min(len(self._order) - 1, i + delta))])

    def page(self, direction):
        if not self._order:
            return
        height = max(self.size.height - 1, 1)
        top = max(0, int(self.scroll_y) + direction * height)
        self.scroll_to(y=top, animate=False, immediate=True)
        keys = [k for k in self._order if self._spans[k][0] >= top] or self._order[-1:]
        self.cursor = keys[0] if direction > 0 else (
            [k for k in self._order if self._spans[k][0] <= top + height][-1:] or keys)[-1]
        self._layout()
        self._repaint()

    def set_open(self, key, value):
        node = self._by_key.get(key)
        if node is not None and node.expandable:
            self.overrides[key] = value
            self._layout()
            self._repaint()
            self._reveal()
            if self.on_fold:
                self.on_fold()

    def set_all(self, value):
        def walk(nodes):
            for n in nodes:
                if n.expandable:
                    self.overrides[n.key] = value
                walk(n.children)
        walk(self.nodes)
        self._layout()
        self._repaint()
        self._reveal()
        if self.on_fold:
            self.on_fold()

    def toggle_all(self):
        """Open everything, or close everything once nothing is left closed."""
        def closed(nodes):
            return any((n.expandable and not self.is_open(n)) or closed(n.children) for n in nodes)
        self.set_all(closed(self.nodes))

    def toggle(self):
        node = self._by_key.get(self.cursor)
        if node is not None and node.expandable:
            self.set_open(self.cursor, not self.is_open(node))

    def current(self):
        return self._by_key.get(self.cursor)

    # A focused ScrollView binds the arrows, PgUp/PgDn, Home and End to scrolling and, once the rows
    # overflow the pane, handles them before the app's bindings run. Route them to the cursor so the
    # view only scrolls to keep the cursor visible.
    def action_scroll_up(self):
        self.move(-1)

    def action_scroll_down(self):
        self.move(1)

    def action_page_up(self):
        self.page(-1)

    def action_page_down(self):
        self.page(1)

    def action_scroll_home(self):
        self.move_to(self._order[0] if self._order else None)

    def action_scroll_end(self):
        self.move_to(self._order[-1] if self._order else None)

    def on_click(self, event: events.Click):
        """A click selects the row; a click on its fold arrow folds it; a double click does what Enter does
        (a section header folds, a session row jumps)."""
        i = int(self.scroll_offset.y) + event.y
        if not 0 <= i < len(self._lines):
            return
        key = self._lines[i][0]
        if key is None:
            return
        self.move_to(key)
        gx = self._glyph_x.get(i)
        if gx is not None and gx <= event.x + int(self.scroll_offset.x) <= gx + 1:
            self.toggle()
        elif event.chain >= 2:
            self.app.action_enter()

    def visible_text(self):
        """Plain text of every line, for tests and captures."""
        return "\n".join(line.plain.rstrip() for _, line in self._lines)


class TabBar(Static):
    DEFAULT_CSS = "TabBar { height: 1; }"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.counts = [0] * len(views.TAB_TITLES)
        self.compact = False
        self.plain = ""
        self.active = 0
        self._spans = []

    def update_tabs(self, counts, active, width=None):
        self.active, self.counts = active, list(counts)
        width = (width or self.size.width or 80) - 2
        for titles, self.compact in ((views.TAB_TITLES, False), (views.SHORT_TITLES, False),
                                     (views.SHORT_TITLES, True), (views.TINY_TITLES, True)):
            for gap in (4, 2):
                if self._fits(titles, gap, width):
                    return self._paint(titles, gap)
        self._paint(views.TINY_TITLES, 2)

    def _label(self, titles, i):
        return ("%d %s %d" if self.compact else "%d %s · %d") % (i + 1, titles[i], self.counts[i])

    def _fits(self, titles, gap, width):
        return sum(len(self._label(titles, i)) + gap for i in range(len(titles))) + 2 <= width

    def _paint(self, titles, gap):
        text = Text(no_wrap=True, overflow="ellipsis")
        self._spans = []
        pad = " " * (gap // 2 - 1)
        for i in range(len(titles)):
            label = self._label(titles, i)
            start = text.cell_len
            if i == self.active:
                text.append("[%s%s%s]" % (pad, label, pad), style="bold reverse")
            else:
                text.append(" %s%s%s " % (pad, label, pad), style="dim" if self.counts[i] == 0 else "")
            self._spans.append((start, text.cell_len))
        self.plain = text.plain
        self.update(text)

    def on_resize(self, event):
        self.update_tabs(self.counts, self.active, event.size.width)

    def on_click(self, event: events.Click):
        for i, (a, b) in enumerate(self._spans):
            if a <= event.x < b:
                self.app.show_tab(i)
                return


class HelpScreen(ModalScreen):
    """The key list, wrapped to the terminal and scrollable, so every line is reachable at 80x24."""

    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen #helpbox { width: 100%; max-width: 110; height: 100%; padding: 0 1; }
    HelpScreen #helptext { width: 100%; }
    """
    BINDINGS = [
        Binding("escape,q,question_mark", "close", show=False),
        Binding("down,j", "scroll(1)", show=False),
        Binding("up,k", "scroll(-1)", show=False),
        Binding("pagedown,space", "page(1)", show=False),
        Binding("pageup", "page(-1)", show=False),
        Binding("home", "edge(0)", show=False),
        Binding("end", "edge(1)", show=False),
    ]

    def __init__(self):
        super().__init__()
        self.lines = []

    def compose(self):
        with VerticalScroll(id="helpbox"):
            yield Static("", id="helptext")

    def on_mount(self):
        self.query_one("#helpbox").focus()
        self.fill()

    def on_resize(self, event):
        self.fill()

    def fill(self):
        width = max(self.size.width - 4, 20)
        self.lines = help_lines(min(width, 106))
        self.query_one("#helptext", Static).update(Text("\n".join(self.lines), no_wrap=True))

    def action_close(self):
        self.dismiss()

    def action_scroll(self, d):
        self.query_one("#helpbox").scroll_relative(y=d, animate=False)

    def action_page(self, d):
        box = self.query_one("#helpbox")
        box.scroll_relative(y=d * max(box.size.height - 1, 1), animate=False)

    def action_edge(self, which):
        box = self.query_one("#helpbox")
        box.scroll_to(y=box.max_scroll_y if which else 0, animate=False)

    def on_click(self, event):
        self.dismiss()


class DetailScreen(ModalScreen):
    """The lines a Resume row leaves out: session ID, full folder, transcript path."""

    DEFAULT_CSS = """
    DetailScreen { align: center middle; }
    DetailScreen #detail { width: 100%; max-width: 110; height: auto; max-height: 100%;
                           border: round $primary; padding: 0 1; background: $surface; }
    """
    BINDINGS = [Binding("escape,q,i,enter,space", "close", show=False)]

    def __init__(self, text):
        super().__init__()
        self.text = text

    def compose(self):
        with Vertical(id="detail"):
            yield Static(self.text, id="detailtext")
            yield Static(Text("Esc closes", style="dim"))

    def action_close(self):
        self.dismiss()


class SnoozeScreen(ModalScreen):
    """Pick when a to-do comes back: tomorrow, next Monday, or a typed date. Dismisses with the epoch of
    that day's start, or None."""

    DEFAULT_CSS = """
    SnoozeScreen { align: center middle; }
    SnoozeScreen #picker { width: 60; max-width: 95%; height: auto; max-height: 90%;
                           border: round $primary; padding: 0 1; background: $surface; }
    SnoozeScreen OptionList { height: auto; max-height: 8; border: none; padding: 0; }
    SnoozeScreen Input { margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, what, now):
        super().__init__()
        self.what, self.now = what, now
        self.options = (("tomorrow", holds.tomorrow(now)), ("next week", holds.next_monday(now)))

    def compose(self):
        with Vertical(id="picker"):
            yield Static(Text("Snooze: %s" % self.what, style="bold", no_wrap=True, overflow="ellipsis"))
            yield OptionList(*[Option(Text("%s (%s)" % (label, d.strftime("%a %b %-d"))), id="d:%d" % i)
                               for i, (label, d) in enumerate(self.options)],
                             Option(Text("a date…", style="italic"), id="typed"), id="choices")
            yield Input(placeholder="YYYY-MM-DD, then Enter", id="when")
            yield Static("", id="problem")

    def on_mount(self):
        self.query_one("#when", Input).display = False
        self.query_one("#choices", OptionList).focus()

    def on_option_list_option_selected(self, event):
        event.stop()
        if event.option.id == "typed":
            box = self.query_one("#when", Input)
            box.display = True
            box.focus()
        else:
            self.dismiss(holds.day_start(self.options[int(event.option.id.split(":")[1])][1]))

    def on_input_submitted(self, event):
        event.stop()
        try:
            day = holds.parse_date(event.value, self.now)
        except ValueError as exc:
            self.query_one("#problem", Static).update(Text(str(exc), style="bold"))
            return
        self.dismiss(holds.day_start(day))

    def action_cancel(self):
        self.dismiss(None)


class PruneScreen(ModalScreen):
    """The status files a prune would move aside, and the ones it keeps. Unnamed sessions only, until
    n includes the sessions the user named."""

    DEFAULT_CSS = """
    PruneScreen { align: center middle; }
    PruneScreen VerticalScroll { width: 90%; max-width: 110; height: auto; max-height: 85%;
        border: round $accent; padding: 0 1; background: $surface; }
    """
    BINDINGS = [
        Binding("y", "confirm", show=False),
        Binding("n", "toggle_named", show=False),
        Binding("escape", "cancel", show=False),
    ]

    def __init__(self, plan):
        super().__init__()
        self.base = plan
        self.plan = plan

    def body(self):
        plan = self.plan
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append("Prune status files · %d ready · %s\n" % (
            len(plan.candidates), "named sessions included" if plan.include_named else "unnamed sessions only"),
            style="bold")
        t.append("Moved to .pruned/, never deleted; a returning session gets its file back.\n", style="dim")
        for c in plan.candidates:
            t.append("\n  %s" % c["name"])
            t.append("  last active %s, %s" % (data.day(c["last"]), prune.why(c)), style="dim")
        if not plan.candidates:
            t.append("\n  nothing to prune")
        if plan.kept_open:
            t.append("\n\nKept: has open items (%d)" % len(plan.kept_open), style="bold")
            for c in plan.kept_open:
                t.append("\n  %s" % c["name"])
                t.append("  " + ", ".join(
                    x for x in (data.plural(c["questions"], "question") if c["questions"] else "",
                                data.plural(c["upcoming"], "upcoming item") if c["upcoming"] else "") if x),
                    style="dim")
        for entries, heading in ((plan.kept_named, "Kept: named session (%d)"),
                                 (plan.kept_unknown, "Kept: could not tell if named (%d)")):
            if entries:
                t.append("\n\n" + heading % len(entries), style="bold")
                for c in entries:
                    t.append("\n  %s" % c["name"])
                    t.append("  last active %s" % data.day(c["last"]), style="dim")
        keys = ["y prune these" if plan.candidates else "", "n %s named sessions" % (
            "leave out" if plan.include_named else "include"), "Esc cancel" if plan.candidates else "Esc closes"]
        t.append("\n\n" + ", ".join(k for k in keys if k), style="bold")
        return t

    def compose(self):
        with VerticalScroll():
            yield Static(self.body(), id="prune-body")

    def action_toggle_named(self):
        self.plan = self.plan.include(not self.plan.include_named)
        self.query_one("#prune-body", Static).update(self.body())

    def action_confirm(self):
        self.dismiss(self.plan if self.plan.candidates else None)

    def action_cancel(self):
        self.dismiss(None)


class FindScreen(ModalScreen):
    """Type-ahead jump: filter the live top-level sessions by name and persona."""

    DEFAULT_CSS = """
    FindScreen { align: center top; }
    FindScreen #finder { width: 72; max-width: 96%; height: auto; max-height: 80%; margin-top: 2;
                         border: round $primary; padding: 0 1; background: $surface; }
    FindScreen OptionList { height: auto; max-height: 14; border: none; padding: 0; }
    """
    BINDINGS = [
        Binding("escape", "cancel", show=False, priority=True),
        Binding("down,ctrl+n", "step(1)", show=False, priority=True),
        Binding("up,ctrl+p", "step(-1)", show=False, priority=True),
    ]

    PLACEHOLDER = "Jump to session: type part of a name or persona"

    def __init__(self, entries):
        super().__init__()
        self.entries = entries
        self.shown = []

    def compose(self):
        with Vertical(id="finder"):
            yield Input(placeholder=self.PLACEHOLDER, id="query")
            yield OptionList(id="results")

    def on_mount(self):
        self.query_one("#results", OptionList).can_focus = False
        self.query_one("#query", Input).focus()
        self.refill("")

    def refill(self, query):
        self.shown = find.rank(query, self.entries)
        lst = self.query_one("#results", OptionList)
        lst.clear_options()
        for e in self.shown:
            t = Text(no_wrap=True, overflow="ellipsis")
            t.append(e["name"], style="bold")
            if e["persona"]:
                t.append(" (%s)" % e["persona"])
            t.append("  " + e["group"], style="dim")
            t.append("  " + e["status"], style="italic")
            lst.add_option(Option(t))
        if self.shown:
            lst.highlighted = 0
        else:
            lst.add_option(Option(Text("no matching session", style="dim"), disabled=True))

    def on_input_changed(self, event):
        event.stop()
        self.refill(event.value)

    def on_input_submitted(self, event):
        event.stop()
        chosen = self.highlighted_entry()
        if chosen is not None:
            self.submit(chosen)

    def highlighted_entry(self):
        i = self.query_one("#results", OptionList).highlighted
        return self.shown[i] if self.shown and i is not None and i < len(self.shown) else None

    def submit(self, entry):
        self.app.find_jump(entry["name"])

    def action_step(self, d):
        lst = self.query_one("#results", OptionList)
        if self.shown:
            lst.highlighted = max(0, min(len(self.shown) - 1, (lst.highlighted or 0) + d))

    def action_cancel(self):
        self.dismiss()


class TabFindScreen(FindScreen):
    """The find box over one tab's rows. Enter closes it and does what Enter does on the chosen row."""

    def __init__(self, entries, noun):
        super().__init__(entries)
        self.PLACEHOLDER = "Find %s: type part of its text, name or group" % noun

    def submit(self, entry):
        self.dismiss(entry)


class ResumeFindScreen(FindScreen):
    """The find box over the Resume tab: Enter resumes here, Ctrl-O opens a new window."""

    BINDINGS = FindScreen.BINDINGS + [Binding("ctrl+o", "new_window", show=False, priority=True)]

    PLACEHOLDER = "Resume session: type part of a name, persona or folder"

    def submit(self, entry):
        self.app.start_resume(entry["sid"], True)

    def action_new_window(self):
        chosen = self.highlighted_entry()
        if chosen is not None:
            self.app.start_resume(chosen["sid"], False)


class ParkScreen(ModalScreen):
    """Ask what a session is waiting on, then park it."""

    DEFAULT_CSS = """
    ParkScreen { align: center middle; }
    ParkScreen #parker { width: 72; max-width: 96%; height: auto; border: round $primary;
                         padding: 0 1; background: $surface; }
    ParkScreen Input { margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", show=False, priority=True)]

    def __init__(self, session, note):
        super().__init__()
        self.session_name, self.note = session, note

    def compose(self):
        with Vertical(id="parker"):
            yield Static(Text("Park %s" % self.session_name, style="bold", no_wrap=True, overflow="ellipsis"))
            yield Input(value=self.note, placeholder="What is it waiting on?", id="note")
            yield Static(Text("Enter saves, Esc cancels", style="dim"))

    def on_mount(self):
        self.query_one("#note", Input).focus()

    def on_input_submitted(self, event):
        event.stop()
        self.dismiss(event.value)

    def action_cancel(self):
        self.dismiss(None)


class StillRunningScreen(ModalScreen):
    """A running session cannot be resumed; offer the jump instead."""

    DEFAULT_CSS = """
    StillRunningScreen { align: center middle; }
    StillRunningScreen #running { width: 72; max-width: 96%; height: auto; border: round $warning;
                                  padding: 0 1; background: $surface; }
    """
    BINDINGS = [Binding("enter,y", "confirm", show=False), Binding("escape,n", "cancel", show=False)]

    def __init__(self, message, can_jump):
        super().__init__()
        self.message, self.can_jump = message, can_jump

    def compose(self):
        with Vertical(id="running"):
            yield Static(Text(self.message))
            yield Static(Text("Enter jumps to its iTerm tab, Esc stays here" if self.can_jump
                              else "It is not in an iTerm2 tab fleetwatch can jump to. Esc closes", style="bold"))

    def action_confirm(self):
        if self.can_jump:
            self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)


class ReopenScreen(ModalScreen):
    """Confirm opening an ended session in a new window. Nothing starts without it."""

    DEFAULT_CSS = """
    ReopenScreen { align: center middle; }
    ReopenScreen #reopen { width: 72; max-width: 96%; height: auto; border: round $warning;
                           padding: 0 1; background: $surface; }
    """
    BINDINGS = [Binding("enter,y", "confirm", show=False), Binding("escape,n", "cancel", show=False)]

    def __init__(self, name):
        super().__init__()
        self.session_name = name

    def compose(self):
        with Vertical(id="reopen"):
            yield Static(Text("Reopen %s in a new window?" % self.session_name))
            yield Static(Text("Enter or y reopens it, Esc or n stays here", style="bold"))

    def action_confirm(self):
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)


class GroupPicker(ModalScreen):
    """Pick a group for one session, or type a new group name."""

    DEFAULT_CSS = """
    GroupPicker { align: center middle; }
    GroupPicker #picker { width: 48; max-width: 90%; height: auto; max-height: 90%;
                          border: round $primary; padding: 0 1; background: $surface; }
    GroupPicker OptionList { height: auto; max-height: 14; border: none; padding: 0; }
    GroupPicker Input { margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", show=False)]
    NEW = "new-group"

    def __init__(self, session, names, current):
        super().__init__()
        self.session_name, self.names, self.current = session, names, current

    def compose(self):
        with Vertical(id="picker"):
            yield Static(Text("Move %s to" % self.session_name, style="bold", no_wrap=True,
                              overflow="ellipsis"))
            options = [Option(Text(n + ("  (now)" if n == self.current else "")), id="g:%d" % i)
                       for i, n in enumerate(self.names)]
            yield OptionList(*options, Option(Text("New group…", style="italic"), id=self.NEW), id="choices")
            yield Input(placeholder="New group name, then Enter", id="newname")

    def on_mount(self):
        self.query_one("#newname", Input).display = False
        lst = self.query_one("#choices", OptionList)
        if self.current in self.names:
            lst.highlighted = self.names.index(self.current)
        lst.focus()

    def on_option_list_option_selected(self, event):
        event.stop()
        if event.option.id == self.NEW:
            box = self.query_one("#newname", Input)
            box.display = True
            box.focus()
        else:
            self.dismiss(self.names[int(event.option.id.split(":")[1])])

    def on_input_submitted(self, event):
        event.stop()
        name = data.clean(event.value).strip()
        if name:
            self.dismiss(name[:60])

    def action_cancel(self):
        self.dismiss(None)


class SessionPicker(ModalScreen):
    """Pick which of several sessions holding the same item to jump to."""

    DEFAULT_CSS = """
    SessionPicker { align: center middle; }
    SessionPicker #picker { width: 56; max-width: 90%; height: auto; max-height: 90%;
                            border: round $primary; padding: 0 1; background: $surface; }
    SessionPicker OptionList { height: auto; max-height: 14; border: none; padding: 0; }
    """
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, names):
        super().__init__()
        self.names = names

    def compose(self):
        with Vertical(id="picker"):
            yield Static(Text("Go to which session?", style="bold", no_wrap=True))
            yield OptionList(*[Option(Text(n, no_wrap=True, overflow="ellipsis"), id="s:%d" % i)
                               for i, n in enumerate(self.names)], id="choices")

    def on_mount(self):
        self.query_one("#choices", OptionList).focus()

    def on_option_list_option_selected(self, event):
        event.stop()
        self.dismiss(self.names[int(event.option.id.split(":")[1])])

    def action_cancel(self):
        self.dismiss(None)


class FleetwatchApp(App):
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { layout: vertical; layers: base over; }
    #head { height: 2; }
    #radar { layer: over; width: 9; height: 5; }
    #top { height: 1; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #status { height: 1; padding: 0 1; }
    ContentSwitcher { height: 1fr; }
    .pane { height: 1fr; }
    .sortline { height: 1; padding: 0 1; color: $text-muted; }
    TabBar { padding: 0 1; }
    #footer { height: 1; padding: 0 1; }
    """
    BINDINGS = [
        Binding("1", "tab(0)", "Needs you", show=False),
        Binding("2", "tab(1)", "To-dos", show=False),
        Binding("3", "tab(2)", "Sessions", show=False),
        Binding("4", "tab(3)", "Resume", show=False),
        Binding("5", "tab(4)", "Indexes", show=False),
        Binding("y", "copy_search", show=False),
        Binding("tab", "next_tab(1)", "Next tab", show=False, priority=True),
        Binding("shift+tab", "next_tab(-1)", "Previous tab", show=False, priority=True),
        Binding("up,k", "move(-1)", show=False),
        Binding("down,j", "move(1)", show=False),
        Binding("home", "edge(0)", show=False),
        Binding("end", "edge(-1)", show=False),
        Binding("pageup", "page(-1)", show=False),
        Binding("pagedown", "page(1)", show=False),
        Binding("enter", "enter", show=False),
        Binding("g", "jump", show=False),
        Binding("slash", "find", show=False),
        Binding("ctrl+k", "find_sessions", show=False),
        Binding("space", "toggle", show=False),
        Binding("right,l", "open", show=False),
        Binding("left,h", "close", show=False),
        Binding("z", "fold_all", show=False),
        Binding("e", "all(True)", show=False),
        Binding("c", "all(False)", show=False),
        Binding("s", "sort", show=False),
        Binding("G", "groups", show=False),
        Binding("m", "regroup", show=False),
        Binding("P", "prune", show=False),
        Binding("p", "park", show=False),
        Binding("u", "unpark", show=False),
        Binding("S", "snooze", show=False),
        Binding("x", "done", show=False),
        Binding("i", "details", show=False),
        Binding("o", "new_window", show=False),
        Binding("r", "refresh_now", show=False),
        Binding("question_mark", "help", show=False),
        Binding("q", "quit", show=False),
        Binding("ctrl+c", "quit_now", show=False, priority=True),
    ]

    def __init__(self, loader, interval=data.DEFAULT_INTERVAL_S, state_path=None, paths=None, **kw):
        if os.environ.get("NO_COLOR") == "":
            del os.environ["NO_COLOR"]           # no-color.org: an empty value means color on; Textual reads it as off
        super().__init__(ansi_color=True, **kw)
        self.loader = loader
        self.paths = paths                    # None: no transcript scan, and the Resume tab stays empty
        self.resume_data = None               # resume.Loaded from the latest scan
        self.resume_rows = {}                 # sessionId -> entry shown on the Resume tab
        self.scanning = False
        self.scanned_at = 0.0
        self.resuming = False
        self.index_owner_states = {}          # index name -> "running" or "resume", from index_owners()
        self.opened_at = {}                   # sessionId -> when a window was opened for it
        self.interval = interval
        self.snapshot = None
        self.active = 0
        self.status_text = None
        self.status_until = 0.0
        self.loading = False
        self.refreshes = 0
        self.utf8 = data.is_utf8(sys.stdout)      # read before Textual swaps stdout for its capture
        self.jump_calls = []
        self.prune_clock = time.time
        self.state_path = state_path or config.state_path()
        self.state, self._state_warning = config.load_state(self.state_path)
        self.sorts = [views.sort_id(i, self.state["sort"].get(str(i))) for i in range(len(views.TAB_TITLES))]
        self.grouped = self.state["groups"]
        self.todo_group = self.state["todo_group"]
        self.holds = holds.Holds(self.state.get("holds"))
        self.show_held = False
        self.needs_seen = []
        self.sortline_plain = []
        self.footer_plain = ""
        self._cfg_warned = None

    def compose(self) -> ComposeResult:
        with Vertical(id="head"):
            yield Static(Text("fleetwatch · loading…", no_wrap=True), id="top")
            yield Static("", id="status")
        yield Static("", id="radar")
        with ContentSwitcher(initial="pane0"):
            for i, empty in enumerate(views.EMPTY):
                with Vertical(id="pane%d" % i, classes="pane"):
                    yield Static("", id="sort%d" % i, classes="sortline")
                    yield FoldList(empty, id="tab%d" % i)
        yield Static("", id="footer")
        yield TabBar(id="tabs")

    def on_resize(self, event):
        self.update_footer(event.size.width)
        self.show_header()
        self.show_radar()

    def show_radar(self):
        """The radar mark, floating over the bottom-right corner above the hint line and the tab bar.

        It is decorative and takes no layout space; a terminal too small for it hides it. ASCII off UTF-8."""
        box = self.query_one("#radar", Static)
        fits = self.size.height >= RADAR_MIN_ROWS and self.size.width >= RADAR_MIN_COLS
        box.display = fits
        if not fits:
            return
        box.styles.offset = (self.size.width - radar.COLS - 1, self.size.height - radar.ROWS - RADAR_BOTTOM_GAP)
        alert = bool(self.snapshot and self.tab_counts()[0])
        cells = radar.mark(alert, self.refreshes, self.utf8)
        text = Text(no_wrap=True)
        for i, line in enumerate(cells):
            text.append("\n" if i else "")
            for ch, color in line:
                text.append(ch, style=color)
        self.query_one("#radar", Static).update(text)

    def update_footer(self, width=None):
        node = self.lists[self.active].current() if getattr(self, "lists", None) else None
        width = (width or self.size.width or 80) - 2
        if self.active == views.INDEXES_TAB:
            text = footer_text(width, False, INDEX_HINTS, INDEX_DROP)
        elif self.active == views.RESUME_TAB:
            text = footer_text(width, False, HOTKEY_RESUME_HINTS if resume.in_hotkey_window() else RESUME_HINTS,
                               RESUME_DROP)
        else:
            show_move = node is not None and node.session is not None
            note = self.held_note() if self.active in (0, views.TODO_TAB) else ""
            text = footer_text(width - len(note), show_move)
            if note and len(text.plain) + len(note) > width:
                note = self.held_note(short=True)
                text = footer_text(width - len(note), show_move)
            if note:
                text.append(note, style="dim")
        self.footer_plain = text.plain
        self.query_one("#footer", Static).update(text)

    def held_note(self, short=False):
        """'  ·  2 snoozed · 1 done (u shows them)', or '  ·  2z·1✓ u' when the line is narrow;
        empty when nothing is hidden."""
        snap = self.snapshot
        if snap is None:
            return ""
        snoozed, done = self.holds.counts(snap.view["held"], snap.now)
        if not snoozed and not done:
            return ""
        if short:
            return "  ·  %dz·%d✓ u" % (snoozed, done)
        return "  ·  %d snoozed · %d done (u %s them)" % (snoozed, done, "hides" if self.show_held else "shows")

    def on_mount(self):
        self.lists = [self.query_one("#tab%d" % i, FoldList) for i in range(len(views.TAB_TITLES))]
        for i, lst in enumerate(self.lists):
            lst.overrides.update({k: v for k, v in self.state["folds"].items()
                                  if k.startswith("grp:%d:" % i)})
            lst.on_fold = self.save_folds
            lst.on_move = self.update_footer
        self.update_sortlines()
        self.query_one(TabBar).update_tabs(self.tab_counts(), 0)
        self.lists[0].focus()
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGHUP):
                loop.add_signal_handler(sig, self.exit)
        except (NotImplementedError, RuntimeError, ValueError):  # pragma: no cover
            pass
        if self._state_warning:
            self.say(self._state_warning)
        self.refresh_now()
        self.set_interval(self.interval, self.refresh_now)
        self.set_interval(1.0, self._expire_status)
        self.set_interval(RESCAN_SECONDS, self.scan_resume)

    def check_action(self, action, parameters):
        """While a picker or the help panel is open, keys belong to it, not to the tabs behind."""
        if isinstance(self.screen, ModalScreen) and action != "quit_now":
            return False
        return True

    # ---- refresh

    def action_refresh_now(self):
        self.refresh_now()

    def refresh_now(self):
        if self.loading:
            return
        self.loading = True
        self.run_worker(self._load, thread=True, exclusive=False, group="load")

    def _load(self):
        try:
            snap = self.loader()
        except Exception as exc:
            self.call_from_thread(self._failed, data.clean(exc)[:80] or type(exc).__name__)
            return
        self.call_from_thread(self.apply, snap)

    def _failed(self, reason):
        self.loading = False
        self.say("refresh failed: %s" % reason)

    def show_header(self):
        snap = self.snapshot
        if snap is not None:
            # The widest form that fits: with the time, without it, then the short words.
            room = self.size.width - 2
            shown = next((h for h in (snap.header, snap.header_mid) if h.cell_len <= room), snap.header_short)
            self.query_one("#top", Static).update(shown)

    def apply(self, snap):
        self.loading = False
        self.snapshot = snap
        self.refreshes += 1
        snap.derive(self.holds)
        if self.holds.prune(snap.view["item_keys"], snap.now):
            self.state["holds"] = self.holds.as_dict()
            self._save_state()
            snap.derive(self.holds)
        self.rebuild()
        self.show_header()
        self.show_radar()
        self.query_one(TabBar).update_tabs(self.tab_counts(), self.active)
        # The first scan waits for the first snapshot, so reading transcripts never delays the first paint.
        if self.resume_data is None:
            self.scan_resume()
        for w in snap.warnings:
            if w.startswith("restored status file"):
                self.say(w)
        if snap.cfg_warning != self._cfg_warned:
            self._cfg_warned = snap.cfg_warning
            if snap.cfg_warning:
                self.say(snap.cfg_warning)

    def rebuild(self):
        """Lay the tabs out again from the current snapshot, sorts and grouping."""
        snap = self.snapshot
        if snap is None:
            return
        self.rebuild_resume(cascade=False)
        self.index_owner_states = owners = self.index_owners()
        tabs, self.needs_seen = views.build_tabs(
            snap.view, snap.stale, snap.now, self.sorts, snap.grouping if self.grouped else None,
            self.needs_seen, len(snap.prune_plan.candidates), snap.indexes.rows, owners, snap.watches, snap.held,
            todo_group=self.todo_group, folders=snap.grouping, holds=self.holds, show_held=self.show_held)
        for lst, nodes in zip(self.lists, tabs):
            lst.set_nodes(nodes)
        index_list = self.lists[views.INDEXES_TAB]
        index_list.empty = snap.indexes.note or views.EMPTY[views.INDEXES_TAB]
        index_list.set_nodes(views.index_nodes(snap.indexes.rows, snap.now, self.sorts[views.INDEXES_TAB], owners))

    def tab_counts(self):
        counts = list(self.snapshot.counts) if self.snapshot else [0] * views.RESUME_TAB
        if self.snapshot:
            counts[0] += len(index_health.problem_rows(self.snapshot.indexes.rows, self.snapshot.now))
            counts[0] += len(self.snapshot.watches)
        return counts + [len(self.resume_rows), len(self.snapshot.indexes.rows) if self.snapshot else 0]

    def index_owner(self, h):
        return self.owner_of(h.owner_id, h.owner)

    def owner_of(self, owner_id, owner):
        """(kind, sessionId, name) for an owner session: kind is "running" (held by a process or the daemon)
        or "resume" (offered on the Resume tab), else None. The session id wins over the name, and a name
        that matches several sessions matches none."""
        held = self.snapshot.held if self.snapshot else {}
        pools = (("running", held), ("resume", self.resume_rows))
        for kind, rows in pools:
            if owner_id and owner_id in rows:
                return kind, owner_id, data.clean(rows[owner_id]["name"])
        for kind, rows in pools:
            named = [sid for sid, r in rows.items() if owner and data.clean(r["name"]) == owner]
            if len(named) == 1:
                return kind, named[0], owner
        return None

    def index_owners(self):
        rows = self.snapshot.indexes.rows if self.snapshot else []
        return {h.name: found[0] for h in rows if (found := self.index_owner(h))}

    def rebuild_resume(self, cascade=True):
        """Lay out the Resume tab from the latest scan and who holds what right now."""
        lst = self.lists[views.RESUME_TAB]
        if self.resume_data is None:
            lst._layout()      # shows "Loading…" until the first scan lands
            lst._repaint()
            return
        snap = self.snapshot
        entries = resume.offered(resume.mark_held(self.resume_data.entries(), snap.held if snap else {}))
        self.resume_rows = {e["sid"]: e for e in entries}
        first = not lst.loaded
        lst.set_nodes(views.resume_nodes(entries, time.time(), self.sorts[views.RESUME_TAB],
                                         snap.groups if snap and self.grouped else None))
        if first:
            # Land on a session, so that the first Enter resumes rather than folds a section.
            for key in lst._order:
                if not lst._by_key[key].section:
                    lst.move_to(key)
                    break
        if cascade and self.index_owners() != self.index_owner_states:
            self.rebuild()

    def scan_resume(self):
        if self.paths is None or self.scanning:
            return
        self.scanning = True
        self.run_worker(self._scan_resume, thread=True, exclusive=False, group="resume-scan")

    def _scan_resume(self):
        warning = None
        try:
            loaded = resume.load(self.paths)
        except park.ParkedError as exc:
            warning = str(exc)
            loaded = resume.Loaded(resume.scan(self.paths), resume.read_personas(self.paths), {})
        except Exception as exc:
            self.call_from_thread(self._scan_failed, data.clean(exc)[:80] or type(exc).__name__)
            return
        self.call_from_thread(self._apply_resume, loaded, warning)

    def _scan_failed(self, reason):
        self.scanning = False
        self.say("could not read the transcripts: %s" % reason)

    def _apply_resume(self, loaded, warning):
        self.scanning = False
        self.scanned_at = time.monotonic()
        self.resume_data = loaded
        self.rebuild_resume()
        self.query_one(TabBar).update_tabs(self.tab_counts(), self.active)
        if warning:
            self.say(warning)

    def update_sortlines(self):
        self.sortline_plain = []
        for i in range(len(views.TAB_TITLES)):
            t = Text("sorted by: %s" % views.sort_label(i, self.sorts[i]), no_wrap=True, overflow="ellipsis")
            t.append("  (s changes)", style="dim")
            if i == views.TODO_TAB:
                t.append(TODO_GROUP_NOTE[self.todo_group], style="dim")
            elif self.grouped and i != views.INDEXES_TAB:
                t.append("  ·  grouped by project folder (G ungroups, m moves a session)", style="dim")
            self.sortline_plain.append(t.plain)
            self.query_one("#sort%d" % i, Static).update(t)

    # ---- remembered choices

    def _save_state(self):
        if not config.save_state(self.state, self.state_path):
            self.say("could not save %s" % os.path.basename(self.state_path))

    def save_folds(self):
        folds = {}
        for lst in self.lists:
            folds.update({k: v for k, v in lst.overrides.items() if k.startswith("grp:")})
        if folds != self.state["folds"]:
            self.state["folds"] = folds
            self._save_state()

    # ---- status line

    def say(self, text):
        self.status_text = data.clean(text)
        self.status_until = time.monotonic() + STATUS_SECONDS
        self.query_one("#status", Static).update(Text(self.status_text, style="bold", no_wrap=True,
                                                      overflow="ellipsis"))

    def _expire_status(self):
        if self.status_text and time.monotonic() >= self.status_until:
            self.status_text = None
            self.query_one("#status", Static).update("")

    # ---- actions

    @property
    def current(self):
        return self.lists[self.active]

    def show_tab(self, i):
        self.active = i % len(views.TAB_TITLES)
        self.query_one(ContentSwitcher).current = "pane%d" % self.active
        self.query_one(TabBar).update_tabs(self.tab_counts(), self.active)
        self.current.focus()
        self.update_footer()
        if self.active == views.RESUME_TAB and time.monotonic() - self.scanned_at > TAB_OPEN_RESCAN_SECONDS:
            self.scan_resume()

    def action_tab(self, i):
        self.show_tab(i)

    def action_next_tab(self, d):
        self.show_tab(self.active + d)

    def action_move(self, d):
        self.current.move(d)

    def action_edge(self, which):
        order = self.current._order
        if order:
            self.current.move_to(order[which])

    def action_page(self, d):
        self.current.page(d)

    def action_open(self):
        node = self.current.current()
        if node is not None and node.expandable:
            self.current.set_open(node.key, True)

    def action_close(self):
        node = self.current.current()
        if node is not None and node.expandable:
            self.current.set_open(node.key, False)

    def action_toggle(self):
        self.current.toggle()

    def action_copy_search(self):
        """On an Indexes row: put `fleetwatch search --index NAME "` on the clipboard, open for the query."""
        node = self.current.current() if self.active == views.INDEXES_TAB else None
        if node is None or not node.index:
            self.say("y copies a command for the highlighted row on the Indexes tab (5)")
            return
        h = next((r for r in self.snapshot.indexes.rows if r.name == node.index), None) if self.snapshot else None
        steps = h.next_commands(self.snapshot.now) if h is not None else []
        if steps:
            self.copy_to_clipboard(steps[0])
            self.say("copied: %s" % steps[0])
            return
        self.copy_to_clipboard('fleetwatch search --index %s "' % node.index)
        self.say("copied the search command for %s" % node.index)

    def action_fold_all(self):
        self.current.toggle_all()

    def action_all(self, value):
        self.current.set_all(value)

    def action_help(self):
        self.push_screen(HelpScreen())

    def action_sort(self):
        self.sorts[self.active] = views.next_sort(self.active, self.sorts[self.active])
        self.state["sort"][str(self.active)] = self.sorts[self.active]
        self._save_state()
        self.update_sortlines()
        self.rebuild()

    def action_groups(self):
        if self.active == views.TODO_TAB:
            groups = views.TODO_GROUPS
            self.todo_group = groups[(groups.index(self.todo_group) + 1) % len(groups)]
            self.state["todo_group"] = self.todo_group
            self._save_state()
            self.update_sortlines()
            self.rebuild()
            return
        self.grouped = not self.grouped
        self.state["groups"] = self.grouped
        self._save_state()
        self.update_sortlines()
        self.rebuild()

    def action_regroup(self):
        node = self.current.current()
        if node is None or node.session is None or self.snapshot is None:
            self.say("no session is highlighted")
            return
        snap, name = self.snapshot, node.session
        names = snap.groups.names()
        current = snap.grouping.of(name)

        def chosen(group):
            if group is None:
                return
            try:
                config.set_override(config.groups_path(), name, group,
                                    snap.groups.by_path(snap.records[name]["cwd"]),
                                    snap.records[name]["sessionId"])
            except (OSError, ValueError) as exc:
                self.say("could not save the group: %s" % (data.clean(exc)[:70] or type(exc).__name__))
                return
            self.say("moved %s to %s" % (name, group))
            self.refresh_now()
        self.push_screen(GroupPicker(name, names, current), chosen)

    def find_entries(self):
        snap = self.snapshot
        if snap is None:
            return []
        out = []
        for x in snap.view["rows"]:
            name = x["name"]
            tty, reason, _ = snap.target(name)
            if tty is None:
                status = "not in iTerm"
            elif x["qs"]:
                status = "%d question%s" % (len(x["qs"]), "" if len(x["qs"]) == 1 else "s")
            elif x["stuck"] is not None or x["helper_stuck"]:
                status = "needs you"
            elif x["working"]:
                status = "working"
            else:
                status = "idle"
            out.append({"name": name, "persona": x["persona"], "group": snap.grouping.of(name),
                        "status": status, "updated": x["updated"]})
        return out

    def action_find_sessions(self):
        """Ctrl-K: the live-session finder, from any tab."""
        if self.snapshot is None:
            self.say("still loading")
            return
        self.push_screen(FindScreen(self.find_entries()))

    def action_find(self):
        """/: find a row on the current tab."""
        if self.snapshot is None:
            self.say("still loading")
            return
        if self.active == views.RESUME_TAB:
            self.push_screen(ResumeFindScreen(self.resume_find_entries()))
            return
        entries = self.index_find_entries() if self.active == views.INDEXES_TAB else self.row_find_entries()
        self.push_screen(TabFindScreen(entries, FIND_NOUNS[self.active]),
                         lambda chosen: chosen and self.find_row(chosen))

    def row_find_entries(self):
        """Every row on the current tab that Enter acts on, headers left out, with the text it shows."""
        snap, out = self.snapshot, []

        def walk(nodes, depth):
            for n in nodes:
                if not n.section:
                    text = " ".join(n.head.plain.split())
                    names = [x for x in ([n.session] if n.session else []) + list(n.sessions) if x]
                    group = snap.grouping.of(n.session) if n.session else ""
                    # Ties go to the shallower row, so a session outranks its own question rows.
                    out.append({"key": n.key, "name": text, "persona": "", "status": "", "updated": -depth,
                                "group": group, "fields": [text] + names})
                walk(n.children, depth + 1)
        walk(self.current.nodes, 0)
        return out

    def index_find_entries(self):
        """Indexes match on the name, group and owner the row shows, never on what is inside the index."""
        out = []
        for h in self.snapshot.indexes.rows:
            out.append({"key": "idx:" + h.name, "name": h.name, "persona": "", "status": "", "updated": 0,
                        "group": "%s%s" % (h.group, "  owner: " + h.owner if h.owner else ""),
                        "fields": [h.name, h.group, h.owner]})
        return out

    def find_row(self, entry):
        """Put the cursor on the chosen row (opening what hides it) and press Enter on it."""
        lst = self.current

        def path(nodes, trail):
            for n in nodes:
                if n.key == entry["key"]:
                    return trail
                found = path(n.children, trail + [n.key])
                if found is not None:
                    return found
            return None
        trail = path(lst.nodes, [])
        if trail is None:
            self.say("that row is gone")
            return
        for key in trail:
            lst.set_open(key, True)
        lst.move_to(entry["key"])
        self.action_enter()

    def resume_find_entries(self):
        now, out = time.time(), []
        for e in self.resume_rows.values():
            status = " ".join((["parked"] if e["parked"] else []) + resume.tags(e))
            date = data.date_label(e["last"], now)
            # A row that wraps would hide the next result, so the folder takes only what is left.
            used = len(e["name"]) + len(date) + len(status) + (len(e["persona"]) + 3 if e["persona"] else 0)
            out.append({"sid": e["sid"], "name": e["name"], "persona": e["persona"], "updated": e["last"],
                        "fields": [e["name"], e["persona"] or "", resume.short_folder(e["cwd"], 200)],
                        "group": "%s  %s" % (date, resume.short_folder(e["cwd"], max(FIND_ROW_ROOM - used, 8))),
                        "status": status})
        return out

    def find_jump(self, name):
        tty, reason, shown = self.snapshot.target(name)
        if tty is None:
            self.say(reason)
            return
        self.run_worker(lambda: self._do_jump(shown, tty, True), thread=True, group="jump")

    def _close_find(self):
        if isinstance(self.screen, FindScreen):
            self.screen.dismiss()

    def action_jump(self):
        node = self.current.current()
        if node is not None and node.resume:
            self._jump_held(node.resume)
            return
        if (node is not None and self.snapshot is not None and node.session in self.snapshot.gone
                and node.helper is None and len(node.sessions) <= 1):
            self.offer_reopen(node.session)
            return
        if node is None or (node.session is None and node.helper is None):
            self.say("no session is highlighted")
            return
        if self.snapshot is None:
            return
        if len(node.sessions) > 1:
            self.push_screen(SessionPicker(node.sessions), self._picked)
            return
        tty, reason, name = self.snapshot.target(node.session, node.helper)
        if tty is None:
            self.say(reason)
            return
        self.run_worker(lambda: self._do_jump(name, tty), thread=True, group="jump")

    def offer_reopen(self, name):
        """Enter on a session that has ended: confirm, then open it as `o` does on the Resume tab."""
        rec = self.snapshot.gone[name]
        if self.resume_data is None:
            self.say("fleetwatch is still reading the Resume list; try again in a moment")
            return
        held = self.snapshot.held if self.snapshot else {}
        entries = resume.mark_held(self.resume_data.entries(), held)
        sid = rec.get("sessionId")
        entry = next((e for e in entries if sid and e["sid"] == sid), None)
        if entry is None:
            entry = next((e for e in entries if e["name"].casefold() == name.casefold()), None)
        if entry is None:
            self.say("%s has no custom name, so fleetwatch can't reopen it; prune it with P if it's finished" % name)
            return
        if entry["held"]:
            self.say("%s is still running, so fleetwatch won't reopen it" % name)
            return
        tags = resume.tags(entry)
        if "no transcript" in tags:
            self.say("%s has no transcript in the projects folder or the archive, so fleetwatch can't reopen it" % name)
            return
        if "folder gone" in tags or "folder unusable" in tags:
            self.say("%s: its folder is %s, so fleetwatch can't reopen it" % (
                name, "gone" if "folder gone" in tags else "unusable"))
            return
        sid = entry["sid"]

        def chosen(yes):
            if yes:
                self.resume_rows.setdefault(sid, entry)
                self.start_resume(sid, False)
        self.push_screen(ReopenScreen(name), chosen)

    def _picked(self, name):
        if name is None or self.snapshot is None:
            return
        tty, reason, shown = self.snapshot.target(name)
        if tty is None:
            self.say(reason)
            return
        self.run_worker(lambda: self._do_jump(shown, tty), thread=True, group="jump")

    def action_enter(self):
        node = self.current.current()
        if node is not None and node.index and not node.section:
            self.go_to_index_owner(node.index)
        elif node is not None and node.watch and not node.section:
            self.go_to_watch_owner(*node.watch)
        elif node is not None and node.section:
            self.current.toggle()
        elif node is not None and node.resume:
            self.start_resume(node.resume, True)
        else:
            self.action_jump()

    def go_to_index_owner(self, name):
        """Enter on an index row: jump to its owner's tab, or resume the owner, by the same paths a session
        row and a Resume row use. With nobody to go to, say which command to run."""
        h = next((r for r in self.snapshot.indexes.rows if r.name == name), None) if self.snapshot else None
        if h is None:
            return
        found = self.index_owner(h)
        if found is None:
            command = ("fleetwatch index verify %s" % name if h.protection == "refused"
                       else "fleetwatch index refresh %s" % name)
            who = "no owner session" if not h.owner else "owner %s is not running and has nothing to resume" % h.owner
            self.say("%s for %s; from a terminal, run: %s" % (who, name, command))
            return
        self.go_to_owner(found)

    def go_to_watch_owner(self, sid, name):
        """Enter on a Watches row: the owner session, by the path an index row's owner takes."""
        found = self.owner_of(sid, name)
        if found is None:
            self.say("%s is not running and has nothing to resume; another session can take its watches over "
                     "by declaring them" % name)
            return
        self.go_to_owner(found)

    def go_to_owner(self, found):
        """Jump to a running owner's tab, or resume a stopped one; in the demo, only say which."""
        kind, sid, owner = found
        if os.environ.get("FLEETWATCH_DEMO"):
            self.say("demo: Enter would %s %s" % ("jump to" if kind == "running" else "resume", owner))
        elif kind == "running":
            tty, reason = self.snapshot.held_target(sid, owner)
            if tty is None:
                self.say(reason)
            else:
                self.run_worker(lambda: self._do_jump(owner, tty, True), thread=True, group="jump")
        else:
            self.start_resume(sid, True)

    def _do_jump(self, name, tty, close_find=False):
        self.jump_calls.append(tty)
        ok, reason = iterm.jump_to_tty(tty)
        if ok and close_find:
            self.call_from_thread(self._close_find)
        self.call_from_thread(self.say, ("jumped to %s" % name) if ok
                              else "could not reach iTerm2: %s" % reason)

    # ---- resume and park

    def _jump_held(self, sid):
        e = self.resume_rows.get(sid)
        name = e["name"] if e else sid
        if self.snapshot is None or sid not in self.snapshot.held:
            self.say("%s is not running; Enter resumes it" % name)
            return
        tty, reason = self.snapshot.held_target(sid, name)
        if tty is None:
            self.say(reason)
            return
        self.run_worker(lambda: self._do_jump(name, tty, True), thread=True, group="jump")

    def action_new_window(self):
        node = self.current.current()
        if self.active != views.RESUME_TAB:
            self.say("o opens a session from the Resume tab (4)")
        elif node is None or not node.resume:
            self.say("no session is highlighted")
        else:
            self.start_resume(node.resume, False)

    def start_resume(self, sid, here):
        """Enter or o on a row. The gate and any archive copy run off the UI thread."""
        e = self.resume_rows.get(sid)
        if e is None or self.resuming:
            return
        here = here and not resume.in_hotkey_window()
        opened = self.opened_at.get(sid)
        if opened is not None and time.monotonic() - opened < REOPEN_GUARD_SECONDS:
            self.say("%s was just opened in a new window" % e["name"])
            return
        self.resuming = True
        self.run_worker(lambda: self._run_resume(e, here), thread=True, exclusive=False, group="resume-go")

    def _run_resume(self, entry, here):
        try:
            ready = resume.prepare(entry, self.paths)
        except resume.Blocked as blocked:
            self.call_from_thread(self._resume_blocked, entry, blocked)
            return
        except Exception as exc:
            self.call_from_thread(self._resume_failed, "could not resume: %s" % (
                data.clean(exc)[:80] or type(exc).__name__))
            return
        if here:
            self.call_from_thread(self.exit, ready)
            return
        ok, info = resume.open_new_window(ready)
        self.call_from_thread(self._window_opened, ready, ok, info)

    def _resume_failed(self, message):
        self.resuming = False
        self.say(message)

    def _resume_blocked(self, entry, blocked):
        self.resuming = False
        if blocked.kind != "live":
            self.say(str(blocked))
            return
        tty, _ = self.snapshot.held_target(entry["sid"], entry["name"]) if self.snapshot else (None, None)
        if tty is None and blocked.record is not None:
            # A session that started since the last refresh is not in the snapshot's map yet.
            ttys = iterm.jump_context()
            tty = ttys["ttys"].get(blocked.record.get("pid"))
            tty = tty if ttys["iterm"] and tty in ttys["iterm"] else None

        def chosen(jump):
            if jump:
                self._close_find()
                self.run_worker(lambda: self._do_jump(entry["name"], tty), thread=True, group="jump")
        self.push_screen(StillRunningScreen(str(blocked), tty is not None), chosen)

    def _window_opened(self, entry, ok, info):
        self.resuming = False
        if not ok:
            self.say("could not reach iTerm2: %s" % info)
            return
        self.opened_at[entry["sid"]] = time.monotonic()
        self._close_find()
        notes = resume.notes_for(entry)
        self.say("opened %s in a new window%s" % (entry["name"], ". " + notes[-1] if notes else ""))
        self._reload_parked()

    def _reload_parked(self):
        """Pick up a change to the parked file now, without waiting for the next scan."""
        if self.resume_data is None:
            return
        try:
            self.resume_data.parked = park.load()
        except park.ParkedError as exc:
            self.say(str(exc))
            return
        self.rebuild_resume()
        self.query_one(TabBar).update_tabs(self.tab_counts(), self.active)

    def _row_session(self):
        """(sessionId, name) of the highlighted row on any tab, else (None, None)."""
        node = self.current.current()
        if node is None:
            return None, None
        if node.resume:
            e = self.resume_rows.get(node.resume)
            return (node.resume, e["name"]) if e else (None, None)
        if node.session and self.snapshot is not None:
            rec = self.snapshot.records.get(node.session)
            if rec is not None and rec.get("sessionId"):
                return rec["sessionId"], node.session
        return None, None

    def action_park(self):
        sid, name = self._row_session()
        if sid is None:
            self.say("no session is highlighted")
            return
        try:
            note = data.clean(park.load().get(sid, {}).get("note") or "")
        except park.ParkedError as exc:
            self.say(str(exc))
            return

        def chosen(text):
            if text is None:
                return
            try:
                saved = park.park(sid, name, text)
            except (park.ParkedError, OSError) as exc:
                self.say("could not park: %s" % (data.clean(exc)[:80] or type(exc).__name__))
                return
            self.say("parked %s%s" % (name, ": " + saved if saved else ""))
            self._reload_parked()
        self.push_screen(ParkScreen(name, note), chosen)

    def action_unpark(self):
        node = self.current.current()
        if self.active == views.TODO_TAB or (node is not None and node.todo):
            self.action_toggle_held()
            return
        sid, name = self._row_session()
        if sid is None:
            self.say("no session is highlighted")
            return
        try:
            gone = park.unpark(sid)
        except (park.ParkedError, OSError) as exc:
            self.say("could not unpark: %s" % (data.clean(exc)[:80] or type(exc).__name__))
            return
        self.say(("unparked %s" if gone is not None else "%s is not parked") % name)
        self._reload_parked()

    # ---- snooze and done (the board's own memory; a status file is never edited)

    def action_toggle_held(self):
        """u on a to-do: show or hide the snoozed and done items."""
        self.show_held = not self.show_held
        self.rebuild()
        self.update_footer()
        if not self.snapshot or not self.snapshot.view["held"]:
            self.say("nothing is snoozed or done")

    def _holds_changed(self, message):
        self.state["holds"] = self.holds.as_dict()
        self._save_state()
        snap = self.snapshot
        snap.derive(self.holds)
        self.rebuild()
        self.show_header()
        self.show_radar()
        self.query_one(TabBar).update_tabs(self.tab_counts(), self.active)
        self.update_footer()
        self.say(message)

    def _todo_row(self, verb):
        node = self.current.current()
        if node is None or not node.todo or not node.todo[0]:
            self.say("%s works on a to-do: highlight one on Your to-dos or a task on Needs you" % verb)
            return None
        return node

    def action_snooze(self):
        node = self._todo_row("S")
        if node is None:
            return
        keys, what = node.todo
        if node.held:
            self.holds.clear(keys)
            self._holds_changed("back on your list: %s" % what)
            return

        def chosen(until):
            if until is None:
                return
            self.holds.snooze(keys, until)
            when = datetime.fromtimestamp(until, data.LOCAL_TZ).strftime("%a %b %-d")
            self._holds_changed("snoozed until %s: %s" % (when, what))
        self.push_screen(SnoozeScreen(what, time.time()), chosen)

    def action_done(self):
        node = self._todo_row("x")
        if node is None:
            return
        keys, what = node.todo
        if node.held:
            self.holds.clear(keys)
            self._holds_changed("back on your list: %s" % what)
            return
        self.holds.done(keys)
        self._holds_changed("marked done: %s (u shows it; it returns if the session changes it)" % what)

    def action_details(self):
        if self.active != views.RESUME_TAB:
            self.say("i shows the details of a row on the Resume tab (4)")
            return
        node = self.current.current()
        e = self.resume_rows.get(node.resume) if node is not None and node.resume else None
        if e is None:
            self.say("no session is highlighted")
            return
        self.push_screen(DetailScreen(views.resume_details(e, time.time())))

    def action_prune(self):
        if self.snapshot is None:
            self.say("still loading")
            return
        self.push_screen(PruneScreen(self.snapshot.prune_plan), lambda chosen: chosen and self._start_prune(chosen))

    def _start_prune(self, plan):
        self.say("pruning %d status files…" % len(plan.candidates))
        self.run_worker(lambda: self._run_prune(plan), thread=True, exclusive=False, group="prune")

    def _run_prune(self, plan):
        try:
            moved, skipped = prune.apply(plan, self.prune_clock())
        except Exception as exc:
            self.call_from_thread(self.say, "prune failed: %s" % (data.clean(exc)[:80] or type(exc).__name__))
            return
        self.call_from_thread(self._prune_done, moved, skipped)

    def _prune_done(self, moved, skipped):
        self.say("pruned %d status files%s" % (len(moved), ", skipped %d" % len(skipped) if skipped else ""))
        self.refresh_now()

    async def action_quit(self):
        self.exit()

    async def action_quit_now(self):
        self.exit()


def run(paths, window_s, horizon, interval):
    """Run the app. Returns the entry to resume in this terminal, once the terminal is restored."""
    app = FleetwatchApp(lambda: gather(paths, window_s, horizon), interval, paths=paths)
    return app.run()
