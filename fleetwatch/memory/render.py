"""The rendered exchange: one Markdown file per exchange, written here and read back here.

The writer (render_exchange) and the reader (parse_exchange, parse_links) live side by
side because the file is three things at once: the text the search engine indexes (its
header lines are chunk text, so their wording changes BM25 and vector scores), the source
of `show` and `--around`, and the Tags: line that --tag filters on. Change the writer and
the reader together, and run the round-trip test.

    # <id> <local time> <sender kind>
    Exchange <id> | <utc> UTC | <local time> | from <who>
    Previous: <id|none> | Next: <id|none> | Source lines <a>-<b>
    Session: <session id>
    Tools used: <tool xN, ...|none>[ | interrupted by the user]
    Tagged by: <model|untagged>
    Summary: <summary|(untagged)>
    Tags: <facet:value ...>

    ## <id> <who>

    <incoming text, headings escaped>

    ## <id> session reply

    <reply text, headings escaped>
"""
import re

from .session_parse import DEFAULT_RULES, iso_pair

# Labels by canonical kind; the owner's label comes from TranscriptRules.
SENDER_LABEL = {"heartbeat": "Heartbeat cron prompt",
                "subagent": "Subagent or teammate hand-back", "peer-session": "Peer session",
                "other": "Other incoming"}


def escape_headings(text):
    """Markdown headings inside message text would split an exchange into foreign sections."""
    return re.sub(r"(?m)^(#{1,6})(\s)", r"\\\1\2", text)


def sender_labels(rules=DEFAULT_RULES):
    return dict(SENDER_LABEL, **{rules.owner_kind: rules.owner_label})


def sender_label(r, rules=DEFAULT_RULES):
    labels = sender_labels(rules)
    canon = r.get("sender_canon", r["sender_kind"])
    who = labels.get(canon) or labels.get(r["sender_kind"]) or r["sender_kind"]
    if canon in ("subagent", "peer-session", "other") and r["sender_name"]:
        who += ": " + r["sender_name"]
    return who


def render_exchange(r, prev_id, next_id, t, tags, rules=DEFAULT_RULES):
    """The file text for parser record `r`, its tag record `t` (may be empty) and its
    rendered `tags` (facet:value strings). `rules` gives the owner's label and the zone."""
    utc, local = iso_pair(r["ts"], rules.tz)
    tools = ", ".join("%s x%d" % (k, v) if v > 1 else k
                      for k, v in sorted(r["tools"].items(), key=lambda kv: -kv[1])) or "none"
    who = sender_label(r, rules)
    out = ["# %s %s %s" % (r["id"], local, r["sender_kind"]),
           "Exchange %s | %s UTC | %s | from %s" % (r["id"], utc, local, who),
           "Previous: %s | Next: %s | Source lines %d-%d"
           % (prev_id or "none", next_id or "none", r["start_line"], r["end_line"]),
           "Session: %s" % r["session"],
           "Tools used: %s%s" % (tools, " | interrupted by the user" if r["interrupted"] else ""),
           "Tagged by: %s" % (t.get("model") or "untagged"),
           "Summary: %s" % (t.get("summary") or "(untagged)"),
           "Tags: %s" % " ".join(tags),
           "",
           "## %s %s" % (r["id"], who),
           "",
           escape_headings(r["user_text"] or "(empty)"),
           ""]
    if r["assistant"]:
        out += ["## %s session reply" % r["id"], "",
                escape_headings("\n\n".join(r["assistant"])), ""]
    return "\n".join(out)


def parse_links(line):
    """(previous, next, source_lines) from a "Previous: ... | Next: ... | Source lines a-b"
    line."""
    parts = [x.strip() for x in line.split("|")]
    return (parts[0].split(": ", 1)[1], parts[1].split(": ", 1)[1],
            parts[2].replace("Source lines ", ""))


def parse_exchange(text, ex_id):
    """The header fields, incoming text and reply of a rendered exchange.

    Keys: id, utc, local (the local time as written), from, previous, next,
    source_lines, session, tagged_by, tools, summary, tags (list), incoming, reply. The
    texts come back as written, headings still escaped.
    """
    head, _, rest = text.partition("\n\n## ")
    meta = {"id": ex_id}
    for line in head.splitlines()[1:]:
        if line.startswith("Exchange "):
            parts = [x.strip() for x in line.split("|")]
            meta["utc"] = parts[1].replace(" UTC", "")
            meta["local"] = parts[2]
            meta["from"] = parts[3].replace("from ", "", 1)
        elif line.startswith("Previous: "):
            meta["previous"], meta["next"], meta["source_lines"] = parse_links(line)
        elif line.startswith("Session: "):
            meta["session"] = line[len("Session: "):]
        elif line.startswith("Tagged by: "):
            meta["tagged_by"] = line[len("Tagged by: "):]
        elif line.startswith("Tools used: "):
            meta["tools"] = line[len("Tools used: "):]
        elif line.startswith("Summary: "):
            meta["summary"] = line[len("Summary: "):]
        elif line.startswith("Tags: "):
            meta["tags"] = line[len("Tags: "):].split()
    body = "## " + rest if rest else ""
    incoming, _, reply = body.partition("\n## %s session reply" % ex_id)
    meta["incoming"] = re.sub(r"^## .*\n+", "", incoming).strip()
    meta["reply"] = reply.strip()
    return meta


# The Previous/Next line is within this many lines of the top of a rendered file.
HEADER_LINES = 6


def parse_header_links(lines):
    """{previous, next} from the first lines of a rendered exchange, or {} without them."""
    for line in lines:
        if line.startswith("Previous: "):
            prev, nxt, _ = parse_links(line)
            return {"previous": prev, "next": nxt}
    return {}
