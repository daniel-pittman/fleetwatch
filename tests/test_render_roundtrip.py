"""The rendered exchange: what session_tagger writes, ExchangeStore reads back the same."""
import tempfile
import unittest
from pathlib import Path

from zoneinfo import ZoneInfo

from fleetwatch.memory import render, session_parse, session_search, session_tagger

TAGS = {"description": "A synthetic session.", "areas": {"garden": "garden work"},
        "kinds": {"decision": "a decision"},
        "rules": [{"facet": "ticket", "pattern": r"(?<![\w&])#(\d{3,5})\b", "prefix": "#"}]}


# A named owner and a fixed zone, so the header lines below do not depend on the machine.
RULES = session_parse.TranscriptRules(owner_name="Sam", tz=ZoneInfo("America/Chicago"))


def record(n, text, reply, ts, **kw):
    r = dict(n=n, ex="EX", start_offset=0, start_line=10 * n, end_line=10 * n + 7,
             sender_kind="owner", sender_canon="owner", sender_name="Sam", ts=ts,
             user_text=text, assistant=reply, tools={"Read": 3, "Edit": 1}, interrupted=False,
             session="0f1e2d3c-4b5a-6978-8091-a2b3c4d5e6f7", id="S0f1e2d-EX-%04d" % n)
    r.update(kw)
    return r


class RoundTrip(unittest.TestCase):
    def test_render_then_parse(self):
        r = record(7, "Fix ticket #4417 please.\n# not a heading\nsecond line",
                   ["Done.", "## Also not a heading"], "2026-03-04T05:06:07Z",
                   interrupted=True)
        t = {"model": "claude-haiku-x", "summary": "Fixed the gate lantern; ticket 4417 closed.",
             "area": "garden", "kinds": ["decision"], "topics": ["gate lantern"]}
        tags = ["area:garden", "kind:decision", "topic:gate-lantern", "ticket:#4417",
                "sender:owner"]
        text = render.render_exchange(r, "S0f1e2d-EX-0006", None, t, tags, RULES)
        m = render.parse_exchange(text, r["id"])
        self.assertEqual(m["id"], r["id"])
        self.assertEqual(m["utc"], "2026-03-04T05:06:07Z")
        self.assertEqual(m["local"], "2026-03-03 23:06 CST")
        self.assertEqual(m["from"], "Sam (user)")
        self.assertEqual((m["previous"], m["next"], m["source_lines"]),
                         ("S0f1e2d-EX-0006", "none", "70-77"))
        self.assertEqual(m["session"], r["session"])
        self.assertEqual(m["tools"], "Read x3, Edit | interrupted by the user")
        self.assertEqual(m["tagged_by"], "claude-haiku-x")
        self.assertEqual(m["summary"], t["summary"])
        self.assertEqual(m["tags"], tags)
        # Texts come back as written: headings escaped so they never split the file.
        self.assertEqual(m["incoming"], render.escape_headings(r["user_text"]))
        self.assertEqual(m["reply"], render.escape_headings("\n\n".join(r["assistant"])))
        self.assertEqual(render.parse_header_links(text.splitlines()[:render.HEADER_LINES]),
                         {"previous": "S0f1e2d-EX-0006", "next": "none"})

    def test_untagged_no_reply_and_named_sender(self):
        r = record(1, "", [], "2026-07-01T12:00:00Z", sender_kind="subagent",
                   sender_canon="subagent", sender_name="lane-a", tools={})
        text = render.render_exchange(r, None, "S0f1e2d-EX-0002", {}, ["sender:subagent"], RULES)
        m = render.parse_exchange(text, r["id"])
        self.assertEqual(m["from"], "Subagent or teammate hand-back: lane-a")
        self.assertEqual((m["tagged_by"], m["summary"], m["tools"]), ("untagged", "(untagged)", "none"))
        self.assertEqual((m["incoming"], m["reply"]), ("(empty)", ""))
        self.assertEqual(m["local"], "2026-07-01 07:00 CDT")

    def test_tagger_writes_what_the_store_reads(self):
        tc = session_tagger.TagConfig(TAGS)
        recs = [record(i, "Message %d about #%d" % (i, 1000 + i), ["Reply %d" % i],
                       "2026-05-0%dT15:00:00Z" % i) for i in range(1, 5)]
        cache = {recs[1]["id"]: {"model": "m", "summary": "s", "area": "garden",
                                 "kinds": ["decision"], "topics": []}}
        with tempfile.TemporaryDirectory() as d:
            here = Path(d)
            n, changed = session_tagger.write_corpus(recs, cache, tc, here / "corpus" / "session", RULES)
            self.assertEqual((n, changed), (4, 4))
            store = session_search.ExchangeStore(here)
            self.assertEqual(sorted(store.exchange_files()), [r["id"] for r in recs])
            m = store.read_exchange(recs[1]["id"])
            self.assertEqual(list(m)[:2], ["id", "path"])
            self.assertEqual(m["path"], "corpus/session/2026-05/20260502/%s.owner.md" % recs[1]["id"])
            self.assertEqual(m["tags"], ["area:garden", "kind:decision", "ticket:#1002",
                                         "sender:owner"])
            self.assertEqual(m["incoming"], "Message 2 about #1002")
            self.assertEqual(store.neighbours(recs[1]["id"], 1),
                             [recs[0]["id"], recs[1]["id"], recs[2]["id"]])
            self.assertEqual(store.neighbours(recs[0]["id"], 2),
                             [recs[0]["id"], recs[1]["id"], recs[2]["id"]])
            # show --json carries the local time under `local` and, for existing callers,
            # under its earlier name `mountain`, next to it and with the same value.
            keys = list(m)
            self.assertEqual(keys[keys.index("local") + 1], "mountain")
            self.assertEqual(m["mountain"], m["local"])
            import contextlib, io, json
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                session_search.cmd_show(store, [recs[1]["id"]], 0, False, True)
            shown = json.loads(buf.getvalue())[0]["exchanges"][0]
            self.assertEqual((shown["local"], shown["mountain"]), (m["local"], m["local"]))
            # Rewriting unchanged records touches nothing.
            self.assertEqual(session_tagger.write_corpus(recs, cache, tc,
                                                         here / "corpus" / "session", RULES), (4, 0))


if __name__ == "__main__":
    unittest.main()
