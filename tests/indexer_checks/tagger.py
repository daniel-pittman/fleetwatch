#!/usr/bin/env python3
"""Tagger accounting and configuration checks with a fake model (no claude call, no cost).

Run: python tests/indexer_checks/tagger.py (or through python -m unittest discover -s tests).
Exits 1 on any failure.
"""
import os
import sys
import tempfile
from pathlib import Path

from fleetwatch.memory import session_tagger as st  # noqa: E402

SPEC = dict(description="A test session.", areas={"a": "area a", "b": "area b"},
            kinds={"k": "kind k"})
fails = []


def check(name, ok):
    print("%s  %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        fails.append(name)


def recs(n):
    return [dict(id="EX-%04d" % i, hash="h%d" % i, ts="2026-10-%02dT00:00:00Z" % (1 + i % 28),
                 user_text="u", assistant=["a"], tools={}, sender_kind="owner",
                 sender_name="", open=False) for i in range(n)]


def fake(omit=lambda r, call: False, fail_calls=()):
    state = dict(calls=0)

    def tagger(batch, tc, usage, cwd):
        state["calls"] += 1
        if state["calls"] in fail_calls:
            raise ValueError("bad json")
        return {r["id"]: dict(area="a", kinds=["k"], topics=[], summary="s", model="m")
                for r in batch if not omit(r, state["calls"])}
    return tagger, state


def run(records, tagger, budget=1000, cache=None, tc=None, **kw):
    d = Path(tempfile.mkdtemp())
    cache = {} if cache is None else cache
    tc = tc or st.TagConfig(SPEC)
    s = st.tag_records(records, cache, tc, d / "tags.jsonl", d / "usage.jsonl", budget, str(d),
                       tagger=tagger, **kw)
    return s, cache, tc


def balanced(s):
    return s["tagged"] + s["failed"] + s["deferred"] == s["stale"] and \
        s["tagged"] + s["failed"] == s["attempted"]


# omitted ids that never come back are counted failed, not dropped
R = recs(30)
tagger, _ = fake(omit=lambda r, c: r["id"] in ("EX-0003", "EX-0017"))
s, cache, _ = run(R, tagger)
check("omitted ids counted failed", s["failed"] == 2 and s["tagged"] == 28 and balanced(s))

# omitted ids that come back on retry are tagged
tagger, _ = fake(omit=lambda r, c: r["id"] == "EX-0005" and c == 1)
s, cache, _ = run(R, tagger)
check("omitted id recovered by retry", s["failed"] == 0 and s["tagged"] == 30 and balanced(s))

# a thrown batch splits and every entry is accounted for
tagger, _ = fake(fail_calls=(1,))
s, _, _ = run(R, tagger)
check("failed batch split and retagged", s["failed"] == 0 and s["tagged"] == 30 and balanced(s))

# a model that answers nothing fails everything, counted
tagger, _ = fake(omit=lambda r, c: True)
s, _, _ = run(recs(7), tagger)
check("total omission counted failed", s["failed"] == 7 and s["tagged"] == 0 and balanced(s))

# budget defers the rest and the identity holds
tagger, _ = fake()
s, _, _ = run(R, tagger, budget=10)
check("deferred counted", s["deferred"] == 20 and s["tagged"] == 10 and balanced(s))

# the cache key includes the prompt and taxonomy
tagger, _ = fake()
s, cache, tc = run(R, tagger)
tagger, st2 = fake()
s2, _, _ = run(R, tagger, cache=cache, tc=tc)
check("unchanged prompt re-tags nothing", s2["stale"] == 0 and st2["calls"] == 0)
tc2 = st.TagConfig(dict(SPEC, summary_guidance="Name the file."))
tagger, st3 = fake()
s3, _, _ = run(R, tagger, cache=cache, tc=tc2)
check("changed guidance re-tags all", s3["stale"] == 30 and s3["tagged"] == 30)
tc3 = st.TagConfig(dict(SPEC, kinds={"k": "kind k", "j": "kind j"}))
check("changed taxonomy changes the version", tc3.version != tc.version)
legacy = {r["id"]: dict(id=r["id"], hash=r["hash"]) for r in R}
tagger, st4 = fake()
s4, _, _ = run(R, tagger, cache=legacy, tc=tc2)
check("record without a version is current", s4["stale"] == 0)
s5, _, _ = run(R, fake()[0], cache=legacy, tc=tc2, force=True)
check("force re-tags everything", s5["tagged"] == 30)

# config: facets, prompt, guidance
tcf = st.TagConfig(dict(SPEC, facets={"area": "phase"}))
t = dict(area="a", kinds=["k"], topics=["x y"], summary="s")
tags = st.facet_tags(dict(user_text="", assistant=[], sender_kind="owner"), t, tcf)
check("facet rename", "phase:a" in tags and "kind:k" in tags and "topic:x-y" in tags)
check("default facets", "area:a" in st.facet_tags(
    dict(user_text="", assistant=[], sender_kind="owner"), t, tc))
check("default prompt forbids conversational summaries", '"asks"' in tc.system)
tcp = st.TagConfig(dict(SPEC, prompt="Custom wording."))
check("prompt replaces default guidance", "Custom wording." in tcp.system and '"asks"' not in tcp.system)
check("guidance extends default", "Name the file." in tc2.system and '"asks"' in tc2.system)
try:
    st.TagConfig(dict(SPEC, facets={"bogus": "x"}))
    check("bad facet key rejected", False)
except ValueError:
    check("bad facet key rejected", True)

# regex rules: old format unchanged, case, multi-facet groups, split
def rule_tags(rules, text):
    tcr = st.TagConfig(dict(SPEC, rules=rules))
    return st.facet_tags(dict(user_text=text, assistant=[], sender_kind="owner"),
                         dict(area="a", kinds=[], topics=[]), tcr)


old = rule_tags([dict(facet="ticket", pattern=r"#(\d{3,5})\b", prefix="#")], "fix #123 and #4567")
check("old rule format unchanged", "ticket:#123" in old and "ticket:#4567" in old)
check("case upper", "lane:B" in rule_tags([dict(facet="lane", pattern=r"lane ([a-z])", case="upper")],
                                           "lane b"))
check("case lower", "lane:b" in rule_tags([dict(facet="lane", pattern=r"lane ([A-Z])", case="lower")],
                                          "lane B"))
multi = rule_tags([dict(pattern=r"\bt(\d+)-([a-z])\b", outputs=[
    dict(facet="tranche", group=1), dict(facet="lane", group=2, case="upper")])], "see t25-b now")
check("one rule feeds two facets", "tranche:25" in multi and "lane:B" in multi)
sp = rule_tags([dict(pattern=r"lanes ([A-Z](?:, [A-Z])*(?: and [A-Z])?)", facet="lane",
                     split=r"\s*(?:,|and)\s*")], "ran lanes A, B and C today")
check("split turns a list into values", all("lane:" + x in sp for x in "ABC") and "lane:A, B and C" not in sp)
try:
    rule_tags([dict(facet="x", pattern="a", case="title")], "a")
    check("bad case rejected", False)
except ValueError:
    check("bad case rejected", True)

# lock freshness (refresh_all)
import time  # noqa: E402
from fleetwatch.memory import refresh as ra  # noqa: E402
old_t = time.time() - ra.LOCK_STALE_S - 600
ld = Path(tempfile.mkdtemp())
with ra.Lock(ld) as held:
    os.utime(held.path, (old_t, old_t))
    with ra.Lock(ld) as second:
        check("stale lock from a dead run is broken", second is not None)
ld = Path(tempfile.mkdtemp())
with ra.Lock(ld) as held:
    os.utime(held.path, (old_t, old_t))
    held.touch()
    with ra.Lock(ld) as third:
        check("touched lock is not stale", third is None)
beats = []
tcb = st.TagConfig(SPEC)
run(recs(60), fake()[0], tc=tcb, beat=lambda: beats.append(1))
check("tagging beats between batches", len(beats) >= 3)

print("%d failed" % len(fails))
sys.exit(1 if fails else 0)
