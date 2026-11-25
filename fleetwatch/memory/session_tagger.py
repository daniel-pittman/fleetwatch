"""Haiku tagger for session exchanges, driven by a per-index tags.yaml.

Tags are cached by a content hash
(state/tags.jsonl), so only a new or changed exchange is tagged. A run never tags more than
its budget, newest first; the rest stay untagged (and searchable) until a later run. Every
run appends its summed modelUsage to state/usage.jsonl.

Only scrubbed text is ever sent: records are scanned before they reach this module.

tags.yaml:
    description: one paragraph telling the tagger what the session is
    model: haiku
    batch_size: 25
    workers: 3
    topic_max: 5
    areas:  {name: "what it covers", ...}     exactly one per exchange
    kinds:  {name: "what it is", ...}         one to three
    kind_notes: "free text appended after the kinds list"
    facets: {area: area, kind: kind, topic: topic}   tag prefixes in the corpus (default shown)
    summary_guidance: "text appended to the default summary and topics guidance"
    prompt: "replaces the default summary and topics guidance (the JSON format and the
             taxonomy lists are always generated from this file); the default is
             DEFAULT_GUIDANCE unless settings.toml [tagger] names another"
    rules:                                    regex tags added without the model
      - {facet: ticket, pattern: "(?<![\\w&])#(\\d{3,5})\\b", prefix: "#"}
    A rule may also set `case: upper|lower` and `split: <regex>` (cuts a captured value into
    several), and may feed several facets from different capture groups:
      - {pattern: "\\bt(\\d+)-([a-z])\\b", outputs: [{facet: tranche, group: 1},
                                                      {facet: lane, group: 2, case: upper}]}
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .render import render_exchange
from .session_parse import DEFAULT_RULES, jsonl_lines

# Retention a tagger run passes to Claude Code, far past any transcript's useful life.
TAGGER_RETENTION_DAYS = 36500

TOKEN_KEYS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens",
              "webSearchRequests", "costUSD")


DEFAULT_GUIDANCE = (
    "summary: one sentence, at most 25 words, that names the specific thing: the artifact, "
    "file, course item, ticket, command or error involved, its cause when one is stated, and "
    "the outcome or decision. Use the exchange's own nouns (file names, ticket numbers, "
    "feature names, counts). Never describe the conversational move: do not write \"asks\", "
    "\"discusses\", \"the user wants\", \"requests\" or \"reports on\". Write what was "
    "found, decided or done, for example \"CSV import rejected over two extra header "
    "columns; trimmed to the three required columns\".\n\n"
    "topics: up to {topic_max} short lowercase phrases naming the concrete subjects, each "
    "specific enough to find this exchange by (for example \"invoice csv import\", "
    "\"token refresh race\", \"ticket 641\"), not general themes such as \"testing\".")

FACET_KEYS = ("area", "kind", "topic")


def compile_rule(r):
    """A rule becomes (regex, outputs); an output is (facet, group, prefix, case, split).
    `group` None means every capture group (the whole match when there are none). A rule
    without `outputs` is one output built from its own facet, prefix, case and split."""
    outs = r.get("outputs") or [dict(facet=r["facet"])]
    res = []
    for o in outs:
        case = o.get("case", r.get("case"))
        if case not in (None, "upper", "lower"):
            raise ValueError("tags.yaml rule case must be upper or lower: %r" % (case,))
        split = o.get("split", r.get("split"))
        res.append((o["facet"], o.get("group"), o.get("prefix", r.get("prefix", "")), case,
                    re.compile(split) if split else None))
    return re.compile(r["pattern"]), res


# How the prompt names the session for each source other than Claude Code. The prompt text is
# the tag-cache version (TagConfig.version), so a Claude index's prompt must stay byte-identical:
# any change to it re-tags every exchange of every Claude index.
PROVIDER_SESSIONS = {"codex": "a Codex CLI session", "antigravity": "an Antigravity CLI conversation"}


class TagConfig:
    def __init__(self, spec, default_guidance=None, source="claude"):
        """`spec` is a parsed tags.yaml; `default_guidance` replaces DEFAULT_GUIDANCE when the
        spec has no `prompt` (settings.toml [tagger]). The guidance text is part of the
        system prompt, so it is part of `version`. `source` is the index's transcript source;
        the prompt names it unless it is claude."""
        if source != "claude" and source not in PROVIDER_SESSIONS:
            raise ValueError("unknown transcript source %r" % (source,))
        self.source = source
        self.description = spec["description"].strip()
        self.model = spec.get("model", "haiku")
        self.batch_size = int(spec.get("batch_size", 25))
        self.workers = int(spec.get("workers", 3))
        self.topic_max = int(spec.get("topic_max", 5))
        self.areas = dict(spec["areas"])
        self.kinds = dict(spec["kinds"])
        self.kind_notes = (spec.get("kind_notes") or "").strip()
        self.facets = {k: k for k in FACET_KEYS}
        for k, v in (spec.get("facets") or {}).items():
            if k not in FACET_KEYS or not re.fullmatch(r"[a-z][a-z0-9_-]*", str(v)):
                raise ValueError("tags.yaml facets: unknown key or bad name %r: %r" % (k, v))
            self.facets[k] = str(v)
        guidance = (spec.get("prompt") or default_guidance or DEFAULT_GUIDANCE).strip()
        extra = (spec.get("summary_guidance") or "").strip()
        self.guidance = (guidance + ("\n\n" + extra if extra else "")).replace(
            "{topic_max}", str(self.topic_max))
        self.rules = [compile_rule(r) for r in spec.get("rules") or []]
        self.system = self._system()
        # Keys the tag cache: a change to the prompt or the taxonomy re-tags. The facet names
        # are part of it because they appear in the prompt.
        self.version = hashlib.sha1(self.system.encode()).hexdigest()[:10]

    def _system(self):
        areas = "\n".join("- %s: %s" % kv for kv in self.areas.items())
        kinds = "\n".join("- %s: %s" % kv for kv in self.kinds.items())
        text = (
            "You tag exchanges from the transcript of a Claude Code session. %s\n\n"
            "Each exchange is one incoming message (from the user, a heartbeat cron prompt, a "
            "subagent or teammate hand-back, or a peer Claude session) plus the session's text "
            "replies and the names of tools it used.\n\n"
            "Return ONLY a JSON array, one object per input exchange, in input order, no prose, "
            "no code fences. Every input id must appear exactly once:\n"
            '[{"id": "Sa77396-EX-0001", "area": "<one %s>", "kinds": ["<%s>", ...], '
            '"topics": ["<short phrase>", ...], "summary": "<one sentence, at most 25 words>"}]\n\n'
            "area (exactly one %s, the main activity):\n%s\n\n"
            "kinds (one to three %s):\n%s\n%s\n\n%s\n"
            % (self.description, self.facets["area"], self.facets["kind"], self.facets["area"],
               areas, self.facets["kind"], kinds, self.kind_notes, self.guidance))
        if self.source == "claude":
            return text
        return text.replace("of a Claude Code session.", "of %s." % PROVIDER_SESSIONS[self.source],
                            1).replace("or a peer Claude session)", "or a peer session)", 1)


def tag_hash(r):
    blob = json.dumps([r["sender_kind"], r["user_text"], r["assistant"], r["tools"]], sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def trim(t, head, tail):
    return t if len(t) <= head + tail else t[:head] + "\n[...]\n" + t[-tail:]


class Usage:
    """Sums modelUsage over every claude call in a run, failed calls included."""

    def __init__(self):
        self.lock = threading.Lock()
        self.models = {}
        self.calls = 0
        self.cost = 0.0

    def totals(self):
        """Input, cache-read and cache-creation tokens are separate counts: the CLI reports
        inputTokens as only the uncached part of the prompt."""
        t = {k: sum(m[k] for m in self.models.values()) for k in TOKEN_KEYS}
        return dict(input_tokens=t["inputTokens"], cache_read_tokens=t["cacheReadInputTokens"],
                    cache_creation_tokens=t["cacheCreationInputTokens"],
                    output_tokens=t["outputTokens"], cost_usd=round(self.cost, 4))

    def add(self, j):
        with self.lock:
            self.calls += 1
            self.cost += float(j.get("total_cost_usd") or 0)
            mu = j.get("modelUsage") or {}
            if not mu and j.get("usage"):       # older CLI output: no per-model breakdown
                u = j["usage"]
                mu = {"unknown": dict(
                    inputTokens=u.get("input_tokens"), outputTokens=u.get("output_tokens"),
                    cacheReadInputTokens=u.get("cache_read_input_tokens"),
                    cacheCreationInputTokens=u.get("cache_creation_input_tokens"))}
            for model, u in mu.items():
                acc = self.models.setdefault(model, {k: 0 for k in TOKEN_KEYS})
                for k in TOKEN_KEYS:
                    acc[k] += u.get(k) or 0


def tag_batch(batch, tc, usage, cwd):
    """Returns {id: tag} for the ids the model answered. An id the model omitted is simply
    absent; the caller counts and retries it."""
    items = [{"id": r["id"], "sender": "%s (%s)" % (r["sender_kind"], r["sender_name"]),
              "incoming": trim(r["user_text"], 1400, 400),
              "reply": trim("\n\n".join(r["assistant"]), 1000, 800),
              "tools": ", ".join(sorted(r["tools"]))} for r in batch]
    # --setting-sources "" keeps the user's hooks and settings out of the tagger, but it also
    # hides cleanupPeriodDays: Claude Code's startup cleanup then falls back to its 30-day
    # default and deletes transcripts in every project (anthropics/claude-code#45903). The
    # explicit --settings value keeps any cleanup this run starts from deleting them.
    cmd = ["claude", "-p", "--model", tc.model, "--tools", "", "--strict-mcp-config",
           "--setting-sources", "", "--settings", json.dumps({"cleanupPeriodDays": TAGGER_RETENTION_DAYS}),
           "--disable-slash-commands", "--no-session-persistence",
           "--system-prompt", tc.system, "--output-format", "json"]
    p = subprocess.run(cmd, input=json.dumps(items, ensure_ascii=False), capture_output=True,
                       text=True, cwd=cwd, timeout=600)
    j = json.loads(p.stdout)
    usage.add(j)
    used = sorted((j.get("modelUsage") or {}).keys())
    resolved = used[0] if len(used) == 1 else (",".join(used) or tc.model)
    out = j["result"].strip()
    out = re.sub(r"^```(?:json)?\s*|\s*```$", "", out)
    arr = json.loads(out[out.index("["):out.rindex("]") + 1])
    ids = {r["id"] for r in batch}
    got = {}
    for o in arr:
        if not isinstance(o, dict) or o.get("id") not in ids:
            continue
        area = o.get("area") if o.get("area") in tc.areas else None
        kinds = [k for k in (o.get("kinds") or []) if k in tc.kinds][:3]
        # Topics stay as the model wrote them until scrub_tag has run (tag_records): lowercased
        # and stripped of punctuation, an email, id or key no longer matches any scan rule.
        topics = [str(t).strip()[:120] for t in (o.get("topics") or [])][:tc.topic_max]
        got[o["id"]] = dict(area=area, kinds=kinds, topics=[t for t in topics if t],
                            summary=str(o.get("summary") or "")[:300], model=resolved)
    return got


def normalize_topics(topics):
    """Lowercase phrases of letters, digits and a little punctuation, as facet tags use."""
    out = [re.sub(r"[^a-z0-9 #./+-]", "", str(t).lower()).strip()[:60] for t in topics or []]
    return [t for t in out if t]


def load_tags(path):
    cache = {}
    if path.exists():
        for line in jsonl_lines(path.read_text()):
            rec = json.loads(line)
            cache[rec["id"]] = rec
    return cache


def compact_tags(path, cache):
    """Keep only the latest tag record per id, so a superseded summary never lingers."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        for rec in cache.values():
            fh.write(json.dumps(rec) + "\n")
    os.replace(tmp, path)


def is_stale(r, cache, tc, force=False):
    rec = cache.get(r["id"])
    if not rec or rec.get("hash") != r["hash"]:
        return True
    # A record written before prompt versions were stored (no "pv") counts as current, so
    # adopting this code never re-tags an index by surprise; `retag NAME --all` forces it.
    return force or (rec.get("pv") not in (None, tc.version))


def tag_records(records, cache, tc, tags_path, usage_path, budget, cwd, scanner=None,
                force=False, tagger=None, beat=lambda: None, tz=None):
    """Tag stale records, newest first, at most `budget` of them. Returns a stats dict with
    stale == tagged + failed + deferred; `attempted` is the entries sent this run."""
    t0 = time.time()
    tagger = tagger or tag_batch
    stale = [r for r in records if is_stale(r, cache, tc, force) and not r.get("open")
             and not r.get("withheld")]
    stale.sort(key=lambda r: r["ts"], reverse=True)
    todo = stale[:max(budget, 0)]
    stats = dict(stale=len(stale), attempted=len(todo), tagged=0, failed=0,
                 deferred=len(stale) - len(todo))
    if not todo:
        return stats
    usage = Usage()
    batches = [todo[i:i + tc.batch_size] for i in range(0, len(todo), tc.batch_size)]
    lock = threading.Lock()

    def retry(batch, depth, why):
        """Split a failed or partly answered batch and try again; a batch that cannot be
        split further, or has been retried three times, is counted as failed."""
        if depth < 3 and len(batch) > 1:
            mid = (len(batch) + 1) // 2
            run(batch[:mid], depth + 1)
            run(batch[mid:], depth + 1)
        elif depth < 3:
            run(batch, depth + 1)
        else:
            sys.stderr.write("tagging failed for %s..%s (%d entries): %s\n"
                             % (batch[0]["id"], batch[-1]["id"], len(batch), why))
            with lock:
                stats["failed"] += len(batch)

    def run(batch, depth=0):
        beat()
        try:
            got = tagger(batch, tc, usage, cwd)
        except Exception as e:      # malformed JSON or a CLI failure
            retry(batch, depth, type(e).__name__)
            return
        with lock:
            with open(tags_path, "a") as fh:
                for r in batch:
                    if r["id"] in got:
                        rec = dict(got[r["id"]], id=r["id"], hash=r["hash"], pv=tc.version)
                        if scanner:
                            scanner.scrub_tag(rec)
                        rec["topics"] = normalize_topics(rec.get("topics"))
                        cache[r["id"]] = rec
                        fh.write(json.dumps(rec) + "\n")
                        stats["tagged"] += 1
            print("  tagged %d / %d" % (stats["tagged"], len(todo)), flush=True)
        beat()
        missing = [r for r in batch if r["id"] not in got]
        if missing:                 # the model left ids out of its JSON
            retry(missing, depth, "ids missing from the model's answer")

    with ThreadPoolExecutor(max_workers=tc.workers) as ex:
        list(ex.map(run, batches))
    if stats["tagged"] + stats["failed"] + stats["deferred"] != stats["stale"]:
        sys.stderr.write("WARNING: tag accounting mismatch: stale %d, tagged %d, failed %d, "
                         "deferred %d\n" % (stats["stale"], stats["tagged"], stats["failed"],
                                           stats["deferred"]))
    stats["wall_s"] = round(time.time() - t0, 1)
    stats["calls"] = usage.calls
    stats["usage"] = usage.totals()
    stats["modelUsage"] = usage.models
    stats["total_cost_usd"] = round(usage.cost, 4)
    with open(usage_path, "a") as fh:
        fh.write(json.dumps(dict(at=datetime.now(tz or DEFAULT_RULES.tz).isoformat(timespec="seconds"),
                                 **stats)) + "\n")
    return stats


# ---------------------------------------------------------------- facets and rendering

def facet_tags(r, t, tc):
    tags = []
    if t.get("area"):
        tags.append(tc.facets["area"] + ":" + t["area"])
    tags += [tc.facets["kind"] + ":" + k for k in t.get("kinds", [])]
    tags += [tc.facets["topic"] + ":" + re.sub(r"\s+", "-", x) for x in t.get("topics", [])]
    blob = r["user_text"] + "\n" + "\n".join(r["assistant"])
    for rx, outs in tc.rules:
        matches = list(rx.finditer(blob))
        for facet, group, prefix, case, split in outs:
            vals = set()
            for m in matches:
                gs = (m.group(group),) if group is not None else (m.groups() or (m.group(0),))
                for g in gs:
                    for v in (split.split(g) if split and g else (g,)):
                        v = (v or "").strip()
                        if v:
                            vals.add(v.upper() if case == "upper" else v.lower() if case == "lower" else v)
            tags += ["%s:%s%s" % (facet, prefix, v) for v in sorted(vals, key=lambda s: (len(s), s))]
    tags.append("sender:" + r["sender_kind"])
    # Two rules can yield the same facet value (a lane named in a list and in an
    # agent name), so the list is deduped across rules, keeping first order.
    return list(dict.fromkeys(tags))


def render(r, prev_id, next_id, t, tc, rules=DEFAULT_RULES):
    """The corpus file text for one exchange; the format itself lives in render.py."""
    return render_exchange(r, prev_id, next_id, t, facet_tags(r, t, tc), rules)


def write_corpus(records, cache, tc, corpus_dir, rules=DEFAULT_RULES):
    """Write one file per exchange; returns (n_records, n_changed). Files for exchanges that
    no longer exist are removed. Only a file whose text changed is touched. Day folders are
    local dates in rules.tz, which --level and date probes depend on."""
    wanted, changed = set(), 0
    for i, r in enumerate(records):
        prev_id = records[i - 1]["id"] if i else None
        next_id = records[i + 1]["id"] if i + 1 < len(records) else None
        local = datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).astimezone(rules.tz)
        path = corpus_dir / local.strftime("%Y-%m") / local.strftime("%Y%m%d") / (
            "%s.%s.md" % (r["id"], r["sender_kind"]))
        wanted.add(path)
        # A tag renders only for the text it was made from: a withheld exchange, or one whose
        # text changed since it was tagged (stricter scan rules), shows no summary or topics.
        t = cache.get(r["id"]) or {}
        if r.get("withheld") or t.get("hash") != r.get("hash"):
            t = {}
        text = render(r, prev_id, next_id, t, tc, rules)
        if not path.exists() or path.read_text() != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            changed += 1
    if corpus_dir.exists():
        for p in corpus_dir.rglob("*.md"):
            if p not in wanted:
                p.unlink()
                changed += 1
    return len(records), changed
