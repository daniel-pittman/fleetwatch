"""fleetwatch tab models: the tabs as trees of folding nodes. Rich Text only, never markup."""
from dataclasses import dataclass, field
from datetime import datetime

from rich.text import Text

from .config import OTHER
from .data import (MARKERS_UTF8, LOCAL_TZ, ago, build_view, clean, date_label, day, dedup_key,
                   due_label, has_overdue, items_of, plural, short_name, task_label, timeline_rows)
from .index_health import problem_rows
from .resume import home_folder, left_off, short_folder, tags

RED = "bold red"
DIM = "dim"
TAB_TITLES = ("Needs you", "Your to-dos", "Sessions", "Resume", "Indexes")
SHORT_TITLES = ("Needs", "To-dos", "Sessions", "Resume", "Indexes")
TINY_TITLES = ("Need", "Todo", "All", "Resume", "Idx")   # the labels must still fit 60 columns
EMPTY = ("Nothing waiting on you", "No to-dos for you", "No sessions found",
         "No custom-named session is parked or stopped", "No indexes are registered")
TODO_TAB = 1
SESSIONS_TAB = 2
RESUME_TAB = 3
INDEXES_TAB = 4

# Per tab: (id, label shown as "sorted by: <label>"). The first entry is the default.
SORTS = (
    (("oldest", "oldest question"), ("count", "most questions"),
     ("recent", "most recently updated"), ("name", "name")),
    (("date", "date"), ("overdue", "most overdue first"), ("session", "session")),
    (("recent", "last activity"), ("name", "name")),
    (("recent", "last active"), ("name", "name")),
    (("name", "name"), ("problems", "problems first")),
)


def sort_id(tab, wanted):
    """The sort id to use for a tab: the wanted one when it exists there, else the default."""
    ids = [i for i, _ in SORTS[tab]]
    return wanted if wanted in ids else ids[0]


def next_sort(tab, current):
    ids = [i for i, _ in SORTS[tab]]
    return ids[(ids.index(sort_id(tab, current)) + 1) % len(ids)]


def sort_label(tab, current):
    return dict(SORTS[tab])[sort_id(tab, current)]


class Grouping:
    """Which group each top-level session is in, and the order groups are shown."""

    def __init__(self, by_name, order):
        self.by_name, self.order = dict(by_name), list(order)

    def of(self, name):
        return self.by_name.get(name, OTHER)

    def present(self, names):
        seen = {self.of(n) for n in names}
        return [g for g in self.order if g in seen] + sorted(seen - set(self.order))


@dataclass
class Node:
    key: str
    head: Text
    body: list = field(default_factory=list)       # [(label Text, text Text)] shown when open
    children: list = field(default_factory=list)   # nested Nodes shown when open
    start_open: bool = False
    session: str = None                            # cleaned name of the owning top-level session
    helper: str = None                             # a helper session this row is about; its own tab first
    sessions: list = field(default_factory=list)   # every session holding a merged item (a picker when > 1)
    section: bool = False                          # a header: Enter folds it, nothing to jump to
    sort: tuple = ()                               # coarse rank that stabilise() keeps rows within
    hang: int = 0                                  # continuation lines of the head indent this much more
    resume: str = None                             # a Resume row: the sessionId it would bring back
    index: str = None                              # an Indexes row: the index name

    @property
    def expandable(self):
        return bool(self.body or self.children)


def _row_text(parts):
    t = Text(no_wrap=False)
    for s, style in parts:
        if s:
            t.append(s, style=style or "")
    return t


def _due(row, now):
    return due_label(row, now) if row["due"] else "no date"


def _marker(row):
    key = "yours" if row["yours"] else row["kind"]
    return MARKERS_UTF8[key][0]


def _oldest_question(x):
    """Age stamp of the session's oldest unanswered question: its since, else the file's updated time."""
    fallback = x["updated"] or 0
    return min([since if since is not None else fallback for _t, since, _tag in x["qs"]] or [fallback])


def _needs_key(sort, x):
    """Sort key of a Needs row. A session stuck on a prompt leads (it blocks work), then one
    with an overdue item of yours, under every sort."""
    over = 0 if has_overdue(x) else 1
    stuck = 0 if (x["stuck"] is not None or x["helper_stuck"]) else 1
    upd = x["updated"] or 0
    if sort == "count":
        return (stuck, over, -len(x["qs"]))
    if sort == "recent":
        return (stuck, over, -upd)
    if sort == "name":
        return (stuck, over, x["name"].casefold())
    return (stuck, over, _oldest_question(x))


def needs_nodes(view, now, sort="oldest"):
    nodes = []
    for x in sorted(view["for_you"], key=lambda x: (_needs_key(sort, x), x["updated"] or 0)):
        n_q = len(x["qs"])
        base = "needs:" + x["name"]
        head = [(x["name"], "bold")]
        if x["persona"]:
            head.append((" (%s)" % x["persona"], ""))
        head.append(("  ", ""))
        bits = []
        if x.get("gone"):
            bits.append(("not running", DIM))
        elif x["stuck"] is None:
            bits.append(session_state(x))      # a stuck session says so below, with its age
        if x["stuck"] is not None:
            bits.append(("stuck on a prompt %s" % ago(x["stuck"]), RED))
        elif x["helper_stuck"]:
            bits.append(("a helper is stuck %s" % ago(x["helper_stuck"][0][1]), RED))
        if n_q:
            bits.append((plural(n_q, "question"), ""))
        n_task = len(x["tasks"])
        if n_task:
            bits.append((plural(n_task, "task"), RED if has_overdue(x) else ""))
        if x["updated"] is not None:
            bits.append(("updated %s" % ago(now - x["updated"]), DIM))
        for i, (s, style) in enumerate(bits):
            if i:
                head.append((" · ", DIM))
            head.append((s, style))
        kids = []
        for c, secs in x["helper_stuck"]:
            cname = clean(c["name"])
            kids.append(Node("%s:h:%s" % (base, cname), _row_text(
                [("! ", RED), ("helper %s stuck on a prompt %s" % (cname, ago(secs)), RED)]),
                session=x["name"], helper=cname, hang=2))
        for i, t in enumerate(x["tasks"], 1):
            label, red = task_label(t)
            kid = _row_text([(label + ": ", RED if red else DIM if t["kind"] == "undated" else ""),
                             (t["what"], RED if red else DIM if t["kind"] == "undated" else "")])
            kids.append(Node("%s:t:%d:%s" % (base, i, t["what"]), kid, session=x["name"], hang=2))
        width = len(str(n_q))
        for i, (text, since, tag) in enumerate(x["qs"], 1):
            lead = "%*d. " % (width, i)
            t = Text(lead + text)
            if since is not None:
                t.append(" · %s" % ago(now - since), style=DIM)
            if tag:
                t.append(" [%s]" % tag, style=DIM)
            kids.append(Node("%s:q:%d" % (base, i), t, session=x["name"], helper=tag, hang=len(lead)))
        nodes.append(Node(base, _row_text(head), children=kids, session=x["name"],
                          sort=_needs_key(sort, x)))
    return nodes


def stabilise(nodes, previous):
    """Within equal rank, keep rows in the order the viewer saw them; new rows go after, oldest first.

    The Needs tab sorts ties by last update, which changes every few seconds for a busy session and
    would shuffle rows under the cursor on every refresh.
    """
    rank = {k: i for i, k in enumerate(previous)}
    order = {id(n): i for i, n in enumerate(nodes)}
    return sorted(nodes, key=lambda n: (n.sort, n.key not in rank,
                                        rank.get(n.key, 0) if n.key in rank else order[id(n)]))


def _item_node(prefix, row, now, datew):
    """One dated or undated item row: date, marker, what, session names."""
    names = "+".join(short_name(n) for n in row["sessions"])
    red = RED if row["red"] else ""
    lead = _due(row, now).ljust(datew) + " " + _marker(row) + " "
    parts = [(lead, red or DIM if not row["due"] else red)]
    if row["red"]:
        parts.append(("overdue ", RED))
    parts += [(row["what"], red), ("  " + names, DIM)]
    return Node("%s:%s:%s" % (prefix, row["bucket"], "|".join(map(str, _dedup(row)))), _row_text(parts),
                session=row["sessions"][0], sessions=list(row["sessions"]), hang=len(lead))


def _datew(rows, now):
    return max([len(_due(r, now)) for r in rows] or [6])


def _dedup(row):
    return dedup_key(row)


def _when(row):
    return row["due"].timestamp() if row["due"] else float("inf")


def _item_key(sort):
    if sort == "session":
        return lambda r: (short_name(r["sessions"][0]).casefold(), _when(r), r["what"].lower())
    if sort == "overdue":
        # overdue first, the oldest due date at the top; then undated; then what is still ahead
        return lambda r: (not r["red"], r["due"] is not None and not r["red"], _when(r), r["what"].lower())
    return lambda r: (not r["red"], _when(r), r["what"].lower())


def todo_rows(view, sort="date"):
    """(your undated items, your dated items, every other item).

    Undated: `upcoming` entries without a date and the old `remind` strings, by session then text.
    Dated: in the chosen sort. The undated block stays above the dated one under every sort."""
    rows = timeline_rows(view["timeline"])
    undated = sorted((r for r in rows if r["yours"] and not r["due"]),
                     key=lambda r: (short_name(r["sessions"][0]).casefold(), r["what"].lower()))
    dated = sorted((r for r in rows if r["yours"] and r["due"]), key=_item_key(sort))
    return undated, dated, sorted((r for r in rows if not r["yours"]), key=_item_key("date"))


def _also_noted(other, now, datew, suffix=""):
    head = [("Also noted", "bold"), (" · %d" % len(other), DIM)]
    node = Node("upg:other" + suffix, _row_text(head), section=True)
    node.children = [_item_node("up", r, now, datew) for r in other]
    return node


def todo_nodes(view, now, sort="date", grouping=None):
    undated, dated, other = todo_rows(view, sort)
    datew = _datew(undated + dated + other, now)

    def block(und, dat, oth, suffix=""):
        out = [_item_node("todo", r, now, datew) for r in und]
        out += [_item_node("up", r, now, datew) for r in dat]
        if oth:
            out.append(_also_noted(oth, now, datew, suffix))
        return out

    if grouping is None:
        return block(undated, dated, other)
    groups = grouping.present([r["sessions"][0] for r in undated + dated + other])
    out = []
    for g in groups:
        def mine(rows):
            return [r for r in rows if grouping.of(r["sessions"][0]) == g]
        u, d, o = mine(undated), mine(dated), mine(other)
        out.append(_group_node(TODO_TAB, g, len(u) + len(d) + len(o), block(u, d, o, ":" + g)))
    return out


def _group_node(tab, group, count, kids):
    head = _row_text([(group, "bold"), (" · %d" % count, DIM)])
    return Node("grp:%d:%s" % (tab, group), head, children=kids, start_open=True, section=True)


def _grouped(tab, nodes, grouping):
    """Wrap session-owned nodes in one folding section per group, in group order."""
    if grouping is None:
        return nodes
    out = []
    for g in grouping.present([n.session for n in nodes]):
        kids = [n for n in nodes if grouping.of(n.session) == g]
        out.append(_group_node(tab, g, len(kids), kids))
    return out


def session_state(x):
    if x["stuck"] is not None:
        return "stuck on a prompt", RED
    if x["quiet"]:
        return "quiet, expected every %g min" % x["quiet"], "yellow"
    if x["working"]:
        return "working", "green"
    return "idle" if x["r"]["live"] else "idle (exited)", ""


def session_node(x, now, prefix="ses:"):
    r = x["r"]
    state, style = session_state(x)
    head = [(x["name"], "bold")]
    if x["persona"]:
        head.append((" (%s)" % x["persona"], ""))
    head += [("  " + state, style)]
    kids = r["children"]
    if kids:
        head += [(" · ", DIM), (plural(len(kids), "helper"), "")]
    if x["updated"] is not None:
        head += [(" · ", DIM), ("updated %s" % ago(now - x["updated"]), DIM)]
    body = []
    if x["working_on"]:
        body.append((Text("  "), Text(x["working_on"])))
    if x.get("renamed_from"):
        body.append((Text("  "), Text("renamed from %s" % x["renamed_from"], style=DIM)))
    key = prefix + x["name"]
    children = []
    for c in kids:
        cname = clean(c["name"])
        t = Text("↳ ", style=DIM)
        t.append("%s  %s" % (cname, clean(c["state"])))
        children.append(Node("%s:k:%s" % (key, cname), t, session=x["name"], helper=cname, hang=2))
    return Node(key, _row_text(head), body, children, session=x["name"])


def _session_key(sort):
    if sort == "name":
        return lambda x: x["name"].casefold()
    return lambda x: -(x["updated"] or 0)


def sessions_nodes(view, stale, now, sort="recent", grouping=None, ready=0):
    rows = sorted(view["rows"], key=_session_key(sort))
    out = []
    if grouping is None:
        groups = {"working": [], "quiet": [], "idle": []}
        for x in rows:
            groups["quiet" if x["quiet"] else "working" if x["working"] else "idle"].append(x)
        for key, title in (("working", "Working"), ("quiet", "Gone quiet"), ("idle", "Idle")):
            if groups[key]:
                node = Node("sesg:" + key, _row_text([(title, "bold"), (" · %d" % len(groups[key]), DIM)]),
                            start_open=True, section=True)
                node.children = [session_node(x, now) for x in groups[key]]
                out.append(node)
    else:
        for g in grouping.present([x["name"] for x in rows]):
            kids = [session_node(x, now) for x in rows if grouping.of(x["name"]) == g]
            out.append(_group_node(SESSIONS_TAB, g, len(kids), kids))
    if view["orphans"]:
        node = Node("sesg:orphans", _row_text([("Unattached helpers", "bold"),
                                               (" · %d" % len(view["orphans"]), DIM)]), section=True)
        node.children = [Node("orph:" + clean(r["name"]), _row_text(
            [(clean(r["name"]), ""), ("  " + clean(r["state"]), DIM)]), helper=clean(r["name"]))
            for r in view["orphans"]]
        out.append(node)
    if stale:
        label = [("Stale", "bold"), (" · %d" % len(stale), DIM)]
        if ready:
            label.append((" · %d ready to prune (P)" % ready, DIM))
        node = Node("sesg:stale", _row_text(label), section=True)
        node.children = [Node("stale:" + clean(r["name"]), _row_text(
            [(clean(r["name"]), ""), ("  %s, last active %s" % ("status file only" if r.get("file_only") else clean(r["state"]), day(r["last"])), DIM)]),
            helper=clean(r["name"]))
            for r in stale]
        out.append(node)
    return out


def resume_node(e, now):
    """One Resume row. A parked note is part of the head, so it shows without opening the row."""
    t = _row_text([(e["name"], "bold"), (" (%s)" % e["persona"] if e["persona"] else "", ""),
                   ("  " + date_label(e["last"], now), DIM), ("  " + short_folder(e["cwd"]), DIM)])
    for tag in tags(e):
        t.append("  ")
        t.append(tag, style="green" if tag == "running" else DIM if tag == "archived" else "yellow")
    if e["parked"]:
        t.append("\nparked %s" % date_label(e["parked_at"], now), style=DIM)
        if e["note"]:
            t.append(": " + e["note"])
    line = left_off(e, now)
    body = [(Text("  "), Text(line, style=DIM))] if line else []
    body += [(Text("  "), Text(clean(home_folder(e["cwd"]) if e["cwd"] else "folder unknown"))),
             (Text("  "), Text("session " + e["sid"], style=DIM))]
    if e["persona"]:
        body.append((Text("  "), Text("%s's scheduled loop does not come back with it" % e["persona"], style=DIM)))
    return Node("res:" + e["sid"], t, body, resume=e["sid"], hang=2)


def resume_nodes(entries, now, sort="recent", groups=None):
    """Parked sessions in a section of their own, first; then the rest, flat or by group.

    `groups` is the config.Groups: entries of one name can sit in different folders, so a group is
    worked out per entry rather than through a by-name Grouping.
    """
    key = (lambda e: e["name"].casefold()) if sort == "name" else (lambda e: -(e["last"] or 0))
    parked = sorted((e for e in entries if e["parked"]), key=key)
    rest = sorted((e for e in entries if not e["parked"]), key=key)
    out = []
    if parked:
        out.append(Node("resg:parked", _row_text([("Parked", "bold"), (" · %d" % len(parked), DIM)]),
                        children=[resume_node(e, now) for e in parked], start_open=True, section=True))
    if groups is None:
        return out + [resume_node(e, now) for e in rest]
    by_group = {}
    for e in rest:
        by_group.setdefault(groups.of(e["name"], e["cwd"], e["sid"]), []).append(e)
    order = [g for g in groups.names() if g in by_group] + sorted(set(by_group) - set(groups.names()))
    return out + [_group_node(RESUME_TAB, g, len(by_group[g]), [resume_node(e, now) for e in by_group[g]])
                  for g in order]


def _age_text(seconds):
    return ago(seconds) if seconds is not None else "never"


def owner_text(h, owners):
    """'owner: NAME (running)' or '(can resume)' from the owners map, 'no owner session' when none is recorded."""
    if not h.owner:
        return "no owner session"
    where = (owners or {}).get(h.name)
    return "owner: %s%s" % (h.owner, {"running": " (running)", "resume": " (can resume)"}.get(where, ""))


def index_node(h, now, owners=None):
    """One index row: status and numbers, in words as well as color. Nothing from inside the index."""
    problems = h.problems(now)
    head = [(h.name, "bold"), ("  " + h.kind, DIM)]
    if h.state == "never":
        head += [(" · ", DIM), ("never refreshed", DIM)]
    else:
        word = "refreshed %s ago" % _age_text(h.age(now)) if h.state == "ok" else "refresh FAILED (%s) %s ago" % (
            h.reason, _age_text(h.age(now)))
        head += [(" · ", DIM), (word, RED if h.state == "failed" and h.refresh_state != "refreshing" else "")]
    if h.refresh_state == "refreshing":
        head += [(" · ", DIM), ("refreshing now", "bold")]
    elif h.refresh_state == "waiting":
        head += [(" · ", DIM), ("waiting for the running refresh", DIM)]
    elif h.is_stale(now):
        head += [(" · ", DIM), ("STALE", RED)]
    if h.protection == "plain":
        head += [(" · ", DIM), ("plain index: its own scripts build the corpus", DIM)]
    elif h.protection:
        head += [(" · ", DIM), ("protection ok" if h.protection == "ok" else "protection REFUSED (%s)" % h.refusal,
                              "" if h.protection == "ok" else RED)]
    if h.exchanges is not None:
        head += [(" · ", DIM), (plural(h.exchanges, "exchange"), DIM)]
    if h.files is not None:
        head += [(" · ", DIM), (plural(h.files, "file"), DIM)]
    body = [(Text("  "), Text(owner_text(h, owners))),
            (Text("  "), Text("group: %s" % h.group)),
            (Text("  "), Text("refresh schedule: %s" % ("installed, stale after %s" % _age_text(h.stale_after)
                                                         if h.scheduled else "not installed, stale after 2 d")))]
    steps = h.next_steps(now)
    body = [(Text("  "), Text(p, style=RED)) for p in problems] + [
        (Text("  "), Text("next: " + t)) for t in steps] + body
    return Node("idx:" + h.name, _row_text(head), body, session=None, index=h.name, hang=2)


def index_nodes(rows, now, sort="name", owners=None):
    """One folding section per trust group; with the problems-first sort, rows that need a look lead."""
    def key(h):
        return (not h.problems(now), h.name.casefold()) if sort == "problems" else (h.name.casefold(),)
    out = []
    for g in sorted({h.group for h in rows}, key=str.casefold):
        kids = [index_node(h, now, owners) for h in sorted((h for h in rows if h.group == g), key=key)]
        out.append(_group_node(INDEXES_TAB, g, len(kids), kids))
    return out


def index_problem_group(rows, now, owners=None):
    """The Needs you section for indexes that failed, are refused or are stale; None when all are fine."""
    bad = problem_rows(rows, now)
    if not bad:
        return None
    kids = []
    for h in bad:
        t = Text(h.name, style="bold")
        t.append("  " + "; ".join(h.problems(now)), style=RED)
        body = [(Text("  "), Text("next: " + step)) for step in h.next_steps(now)]
        body.append((Text("  "), Text(owner_text(h, owners))))
        kids.append(Node("needs-idx:" + h.name, t, body, index=h.name, hang=2, start_open=True))
    head = _row_text([("Indexes", "bold"), (" · %d" % len(kids), DIM)])
    return Node("needs-idx", head, children=kids, start_open=True, section=True)


def tab_counts(view, stale):
    undated, dated, _other = todo_rows(view)
    return [len(view["for_you"]), len(undated) + len(dated),
            len(view["rows"]) + len(view["orphans"]) + len(stale)]


def build_tabs(view, stale, now, sorts=(None,) * 3, grouping=None, seen=(), ready=0, indexes=(), owners=None):
    """(node tree of each tab, Needs row order).

    Each tab is in its chosen sort order, optionally wrapped in group sections. `seen` is the
    Needs row order the viewer last saw; rows that tie keep it (see stabilise).
    """
    s = [sort_id(i, sorts[i]) for i in range(3)]
    needs = stabilise(needs_nodes(view, now, s[0]), seen)
    needs_tab = list(_grouped(0, needs, grouping))
    bad = index_problem_group(indexes, now, owners)
    if bad is not None:
        needs_tab.append(bad)
    return [needs_tab,
            todo_nodes(view, now, s[1], grouping),
            sessions_nodes(view, stale, now, s[2], grouping, ready)], [n.key for n in needs]


def needs_parts(view, short=False):
    """[(text, style)] for what is waiting on you: questions, stuck sessions, overdue, today, undated."""
    fy = view["for_you"]
    n_q = sum(len(x["qs"]) for x in fy)
    n_stuck = sum(1 for x in fy if x["stuck"] is not None or x["helper_stuck"])
    tc = view["task_counts"]
    n_over = tc["overdue"]
    words = ("question", "stuck", "overdue", "due today", "need a date")
    if short:
        words = ("q", "stuck", "overdue", "today", "undated")
    out = []
    for n, word, style in ((n_q, words[0], ""), (n_stuck, words[1], RED), (n_over, words[2], RED),
                           (tc["today"], words[3], ""), (tc["undated"], words[4], "")):
        if not n:
            continue
        if word == "question":
            word = "question" if n == 1 else "questions"
        elif word == "need a date" and n == 1:
            word = "needs a date"
        out.append(("%d %s" % (n, word), style))
    return out


def header_text(view, counts, now, warnings, short=False):
    when = datetime.fromtimestamp(now, LOCAL_TZ).strftime("%a %b %-d %-I:%M %p %Z")
    rows = view["rows"]
    n_work = sum(1 for x in rows if x["working"])
    parts = needs_parts(view, short)
    bits = ["fleetwatch"] if short else ["fleetwatch", when]
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(" · ".join(bits), style="")
    t.stylize("bold", 0, len("fleetwatch"))
    t.append(" · ")
    if not parts:
        t.append("nothing needs you")
    for i, (text, style) in enumerate(parts):
        if i:
            t.append(" · ")
        t.append(text, style=style)
    rest = ["%d working" % n_work]
    if view["quiet"]:
        rest.append("%d quiet" % len(view["quiet"]))
    rest.append("%d idle" % len(view["idle"]))
    t.append(" · " + " · ".join(rest))
    uniq = len({clean(w) for w in warnings})
    if uniq:
        t.append(" · ")
        t.append(plural(uniq, "warning"), style="yellow")
    return t
