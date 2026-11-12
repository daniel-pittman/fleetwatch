"""fleetwatch text output: the --once screen and the --json snapshot."""
import json
import shutil
import sys
from datetime import datetime, timezone

from .views import needs_parts
from .data import (BUCKET_LABELS, BUCKET_ORDER, DEFAULT_HORIZON_DAYS, MARKERS_ASCII, MARKERS_UTF8,
                   LOCAL_TZ, WIDTH, ago, build_view, clean, collect, day, due_label, human, is_utf8,
                   plural, short_name, task_label, timeline_rows, trunc)

def to_json(sections, stale, now, window_s, warnings, horizon_days=DEFAULT_HORIZON_DAYS, groups=None):
    def rec(r):
        sf = r["status"] or {}
        parent = r.get("parent")
        out = {"name": r["name"], "kind": r["kind"], "state": r["state"], "live": r["live"],
               "last_activity": datetime.fromtimestamp(r["last"], timezone.utc).isoformat() if r["last"] else None,
               "persona": sf.get("persona"), "working_on": sf.get("working_on"),
               "parent": parent["name"] if parent else None,
               "questions": sf.get("questions") or [], "remind": sf.get("remind") or []}
        if groups is not None:
            out["group"] = groups.of(clean(r["name"]), r.get("cwd"), r.get("sessionId"))
        return out
    view = build_view(sections, now, horizon_days, stale)
    upcoming = [{"what": r["what"], "due": r["due"].isoformat() if r["due"] else None,
                 "has_time": r["has_time"], "kind": r["kind"], "yours": r["yours"],
                 "sessions": [clean(n) for n in r["sessions"]], "bucket": r["bucket"],
                 "overdue": r["red"]} for r in timeline_rows(view["timeline"])]
    return {
        "generated": datetime.fromtimestamp(now, LOCAL_TZ).isoformat(),
        "window_seconds": window_s,
        "counts": {k: len(sections[k]) for k in ("needs", "questions", "quiet", "working")},
        "sections": {k: [rec(r) for r in v] for k, v in sections.items()},
        "upcoming": upcoming,
        "stale_hidden": len(stale) if not sections["stale"] else 0,
        "warnings": warnings + view["up_warnings"],
    }


def marker_for(row, utf8):
    key = "yours" if row["yours"] else row["kind"]
    return MARKERS_UTF8[key] if utf8 else (MARKERS_ASCII[key], 1)


def row_sessions(row, width):
    names = "+".join(short_name(n) for n in row["sessions"])
    cap = 14 if width < 80 else (22 if len(row["sessions"]) == 1 else 34)
    return trunc(names, cap, True)


def coming_up_lines(tl, now, width, utf8):
    """{section: [segment lines]} for the COMING UP block, without its rule."""
    dot = " · " if utf8 else " | "
    out = {}
    dated = [r for k in BUCKET_ORDER[:4] for r in tl["buckets"][k]]
    datew = max([len(due_label(r, now)) for r in dated] or [6])
    lab = 11

    def row_line(first, bname, row):
        mk, cells = marker_for(row, utf8)
        used = lab + datew + 1 + 2 + 1
        nm = row_sessions(row, width)
        room = width - used - 1
        if room - len(nm) - 2 < 10:
            nm = trunc(nm, room - 12, True) if room - 12 >= 4 else ""
        avail = room - (len(nm) + 2 if nm else 0)
        what = trunc(row["what"], max(avail, 1), utf8)
        gap = max(width - used - len(what) - len(nm) - 1, 2)
        red = "1;31" if row["red"] else None
        head = " %-9s " % BUCKET_LABELS[bname] if first else " " * lab
        line = [(head, ("1;31" if bname == "overdue" else "1") if first else None),
                (due_label(row, now).ljust(datew) + " ", red),
                (mk + " " * (2 - cells) + " ", red), (what, red)]
        if nm:
            line += [(" " * gap, None), (nm, "2")]
        return line

    for bname in BUCKET_ORDER[:4]:
        lines = [row_line(i == 0, bname, row) for i, row in enumerate(tl["buckets"][bname])]
        if bname == "later" and tl["beyond"]:
            first = tl["beyond"][0]
            horizon = tl["horizon"]
            text = "%d more%s, first %s (%s)" % (
                len(tl["beyond"]), "" if horizon <= DEFAULT_HORIZON_DAYS else " beyond %dd" % horizon,
                first["due"].astimezone(LOCAL_TZ).strftime("%b %-d"), first["what"])
            if horizon < 90:
                text += dot + "fleetwatch --horizon 90d"
            head = " %-9s " % "LATER" if not lines else " " * lab
            wrapped = wrap_words(tokens(text, "2"), width, head, lab, " " * lab)
            if not tl["buckets"]["later"]:
                wrapped[0][0] = (head, "1")
            lines += wrapped
        if lines:
            out[bname] = lines
    nd = tl["buckets"]["no_date"]
    if nd:
        groups = {}
        for row in nd:
            groups.setdefault(tuple(short_name(n) for n in row["sessions"]), []).append(row)
        cap = 14 if width < 80 else 22
        names = {k: trunc("+".join(k), cap, True) for k in groups}
        namew = max(len(v) for v in names.values())
        lines = []
        for key, rows in groups.items():
            for i, row in enumerate(rows):
                head = " %-9s " % "NO DATE" if not lines else " " * lab
                nm = names[key].ljust(namew) if i == 0 else " " * namew
                plen = len(head) + namew + 2
                wrapped = wrap_words([(marker_for(row, utf8)[0], None)] + tokens(row["what"]),
                                     width, "", plen, " " * plen)
                wrapped[0][0:1] = [(head, "1" if not lines else None), (nm, "2"), ("  ", None)]
                lines += wrapped
        out["no_date"] = lines
    return out


def wrap_words(words, width, first, first_cells, sub):
    """Greedy wrap of (word, code) tokens into segment lines with a hanging indent."""
    lines, cur, used = [], [(first, None)], first_cells
    empty = True
    for w, c in words:
        if len(w) + (0 if empty else 1) > width - used:
            if not empty:
                lines.append(cur)
                cur, used, empty = [(sub, None)], len(sub), True
            room = max(width - used, 8)
            while len(w) > room:
                cur.append((w[:room], c))
                lines.append(cur)
                cur, used, w = [(sub, None)], len(sub), w[room:]
                room = max(width - used, 8)
        if not empty:
            cur.append((" ", None))
            used += 1
        cur.append((w, c))
        used += len(w)
        empty = False
    lines.append(cur)
    return lines


def tokens(text, code=None):
    return [(w, code) for w in text.split()]


def seg_len(line):
    return sum(len(t) for t, _ in line)


def rule(title, width, utf8, code):
    ch = "━" if utf8 else "="
    lead = "%s %s " % (ch * 3, title)
    return [(lead + ch * max(width - len(lead), 3), code)]


def for_you_lines(x, now, width, utf8):
    dot = " · " if utf8 else " | "
    lines = []
    right = []
    if x.get("gone"):
        right.append(("not running", "2"))
    if x["stuck"] is not None:
        right.append(("stuck on a prompt for %s" % human(x["stuck"]), "1;31"))
    elif x["working"]:
        right.append(("working", "32"))
    if x["busy_helpers"]:
        right.append((plural(x["busy_helpers"], "helper"), "36"))
    if x["quiet"]:
        right.append(("quiet: expected every %g min" % x["quiet"], "33"))
    if x["updated"] is not None:
        right.append(("updated %s ago" % human(now - x["updated"]), "2"))
    rsegs = []
    for i, (t, c) in enumerate(right):
        if i:
            rsegs.append((dot, "2"))
        rsegs.append((t, c))
    name = x["name"] + (" (%s)" % x["persona"] if x["persona"] else "")
    lead, tag = "", ""
    rlen = seg_len(rsegs)
    name = trunc(name, max(width - rlen - 3 - len(lead) - len(tag), 10), utf8)
    gap = max(width - 1 - len(lead) - len(name) - len(tag) - rlen, 2)
    lines.append([(" ", None), (lead, "1;36"), (name, "1"), (tag, "2")] + [(" " * gap, None)] + rsegs)
    for c, secs in x["helper_stuck"]:
        lines += wrap_words(tokens("a helper is stuck on a prompt: %s for %s" % (clean(c["name"]), human(secs)), "31"),
                            width, "   ! ", 5, " " * 5)
    for t in x["tasks"]:
        text, red = task_label(t)
        code = "31" if red else "2" if t["kind"] == "undated" else None
        lines += wrap_words(tokens("%s: %s" % (text, t["what"]), code), width, "   ", 3, " " * 5)
    n = len(x["qs"])
    numw = len(str(n))
    for i, (text, since, tag) in enumerate(x["qs"], 1):
        label = "   %*d. " % (numw, i)
        words = tokens(text)
        if since is not None:
            words += [("·" if utf8 else "|", "2"), (ago(now - since), "2")]
        if tag:
            words += tokens("[%s]" % tag, "2")
        lines += wrap_words(words, width, label, len(label), " " * len(label))
    return lines


def nothing_lines(view, stale, width, utf8, show_all):
    """Lines of the bottom group, without its rule."""
    lines = []
    lab = 11   # " working   "
    narrow = width < 80

    def labelled(label, code, x, extra, extra_code=None, namew=None):
        lead, tag = "", ""
        if x.get("renamed_from"):
            extra = (extra + " " if extra else "") + "(renamed from %s)" % x["renamed_from"]
        name = x["name"]
        room = width - lab - len(lead)
        if narrow or not extra:
            cell = trunc(name, max(room - len(tag), 1), utf8)
            return [(" %-9s " % label, code), (lead, "1;36"), (cell, None), (tag, "2")]
        namew = min(namew or len(name) + len(tag), 34, room)
        cell = trunc(name, max(namew - len(tag), 1), utf8)
        pad = max(namew - len(cell) - len(tag), 0)
        out = [(" %-9s " % label, code), (lead, "1;36"), (cell, None), (tag, "2"), (" " * pad, None)]
        rest = width - lab - len(lead) - namew - 2
        if rest >= 8:
            out += [("  ", None), (trunc(extra, rest, utf8), extra_code)]
        return out

    if view["working"]:
        namew = min(max(len(x["name"]) for x in view["working"]), 34)
        for i, x in enumerate(view["working"]):
            extra = x["working_on"]
            if x["busy_helpers"]:
                extra = (extra + " " if extra else "") + "(%s)" % plural(x["busy_helpers"], "helper")
            lines.append(labelled("working" if i == 0 else "", "32", x, extra, namew=namew))
    for i, x in enumerate(view["quiet"]):
        extra = "expected every %g min" % x["quiet"]
        lines.append(labelled("quiet" if i == 0 else "", "33", x, extra, "33"))
    if view["idle"]:
        words = []
        for i, x in enumerate(view["idle"]):
            lead, tag = "", ""
            nm = x["name"] + ("" if x["r"]["live"] else " (exited)")
            if x.get("renamed_from"):
                nm += " (renamed from %s)" % x["renamed_from"]
            parts = [(w, None) for w in nm.split(" ")]
            if lead.strip():
                parts.insert(0, (lead.strip(), "1;36"))
            if tag:
                parts += [(w, "2") for w in tag.split()]
            if i < len(view["idle"]) - 1:
                parts[-1] = (parts[-1][0] + ",", parts[-1][1])
            words += parts
        wrapped = wrap_words(words, width, " %-9s " % "idle", lab, " " * lab)
        for ln in wrapped:
            lines.append([(ln[0][0], "36" if ln[0][0].strip() else None)] + ln[1:])
    if show_all:
        kids = [x for x in view["rows"] if x["r"]["children"]]
        if kids or view["orphans"]:
            lines.append([("", None)])
            lines.append([(" helpers (--all)", "1")])
            for x in kids:
                lines.append([("  " + x["name"], None)])
                for c in x["r"]["children"]:
                    lines.append([("    ↳ " if utf8 else "    - ", "2"),
                                  (trunc(clean(c["name"]), width - 8, utf8), None),
                                  (" " + clean(c["state"]), "2")])
            if view["orphans"]:
                lines.append([("  unattached", None)])
                for r in view["orphans"]:
                    lines.append([("    ↳ " if utf8 else "    - ", "2"),
                                  (trunc(clean(r["name"]), width - 8, utf8), None),
                                  (" " + clean(r["state"]), "2")])
        if stale:
            lines.append([("", None)])
            lines.append([(" stale", "1;90")])
            for r in stale:
                lines.append([(trunc("  %s: %s, last active %s" % (
                    clean(r["name"]), "status file only" if r.get("file_only") else clean(r["state"]), day(r["last"])), width, utf8), None)])
    return lines


def group_lines(view, groups, width, utf8):
    """The BY GROUP block: each group's top-level sessions, in the live app's group order."""
    by = {}
    for x in view["rows"]:
        by.setdefault(groups.of(x["name"], x["r"]["cwd"], x["r"]["sessionId"]), []).append(x)
    lines = [rule("BY GROUP", width, utf8, "1;35")]
    for g in [n for n in groups.names() if n in by] + sorted(set(by) - set(groups.names())):
        lines.append([(" %s" % trunc(g, width - 8, utf8), "1"), (" · %d" % len(by[g]) if utf8 else " | %d" % len(by[g]), "2")])
        for x in sorted(by[g], key=lambda x: x["name"].casefold()):
            state = ("stuck on a prompt" if x["stuck"] is not None else "quiet" if x["quiet"]
                     else "working" if x["working"] else "idle")
            lines.append([(trunc("   " + x["name"], width - len(state) - 3, utf8), None), ("  " + state, "2")])
    return lines


def render(sections, stale, now, color, warnings, width=WIDTH, utf8=True, show_all=False,
           horizon_days=DEFAULT_HORIZON_DAYS, groups=None):
    width = max(int(width), 40)
    dot = " · " if utf8 else " | "

    def paint_line(line):
        if not color:
            return "".join(t for t, _ in line)
        return "".join("\033[%sm%s\033[0m" % (c, t) if c and t else t for t, c in line)

    view = build_view(sections, now, horizon_days, stale)
    warnings = list(warnings) + view["up_warnings"]
    tl = view["timeline"]
    for_you = view["for_you"]
    n_work = sum(1 for x in view["rows"] if x["working"])
    when = datetime.fromtimestamp(now, LOCAL_TZ).strftime("%a %b %-d %-I:%M %p %Z")
    title = " fleetwatch%s%s" % (dot, when)
    sep = ("·" if utf8 else "|", None)
    words = []
    parts = needs_parts(view, short=width < 80)
    if not parts:
        words += tokens("nothing for you")
    for i, (text, style) in enumerate(parts):
        if i:
            words.append(sep)
        words += tokens(text, "1;31" if style else None)
    tail = "%d working" % n_work
    if view["quiet"]:
        tail += dot + "%d quiet" % len(view["quiet"])
    tail += dot + "%d idle" % len(view["idle"])
    week = [r for r in tl["buckets"]["this_week"] if not r["past"]]
    if week:
        tail += dot + "%d coming up this week (%d yours)" % (len(week), sum(1 for r in week if r["yours"]))
    words += [sep] + tokens(tail)
    head = [[(trunc(title, width, utf8), "1")]]
    for ln in wrap_words(words, width, " ", 1, "   "):
        head.append(ln)
    for w in warnings:
        head.append([(trunc(" " + clean(w), width, utf8), "33")])

    fy = []
    if for_you:
        fy.append(rule("FOR YOU", width, utf8, "1;31"))
        for i, x in enumerate(for_you):
            if i:
                fy.append([("", None)])
            fy += for_you_lines(x, now, width, utf8)

    up = coming_up_lines(tl, now, width, utf8)
    up_rule = [rule("COMING UP", width, utf8, "1;36")] if up else []

    def up_block():
        return up_rule + [ln for k in BUCKET_ORDER for ln in up.get(k, [])]

    body = nothing_lines(view, stale if show_all else [], width, utf8, show_all)
    nothing = ([rule("NOTHING FOR YOU", width, utf8, "1;32")] + body) if body else []

    foot = []
    if view["orphans"] and not show_all:
        foot.append("+" + plural(len(view["orphans"]), "unattached helper session"))
    if stale and not show_all:
        foot.append("%s hidden" % plural(len(stale), "stale record"))
    footer = []
    if foot:
        text = " " + dot.join(foot) + dot + "fleetwatch --all shows them"
        footer = [[(trunc(text, width, utf8), "2")]]

    lines = head + fy + up_block() + nothing + footer
    if groups is not None:
        lines += group_lines(view, groups, width, utf8)
    return "\n".join(paint_line(ln) for ln in lines)


def snapshot(window_s, show_all, horizon_days, as_json, paths, with_groups=False):
    from .config import load_groups
    sections, stale, now, warnings = collect(window_s, show_all, paths)
    groups, bad = load_groups(create=False)
    if bad:
        warnings = warnings + ["warning: " + bad]
    if as_json:
        return json.dumps(to_json(sections, stale, now, window_s, warnings, horizon_days, groups), indent=2)
    cols = shutil.get_terminal_size((WIDTH, 24)).columns
    return render(sections, stale, now, sys.stdout.isatty(), warnings, width=cols,
                  utf8=is_utf8(sys.stdout), show_all=show_all, horizon_days=horizon_days,
                  groups=groups if with_groups else None)
