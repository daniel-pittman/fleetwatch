#!/usr/bin/env python3
"""Refresh every index in the registry (registry.yaml in the index root, settings.toml
[paths] index_root): transcripts and reference documents, scrubbed, tagged and rebuilt only
when something changed.

    refresh_all.py                        refresh every registered index (what launchd runs)
    refresh_all.py --index NAME           refresh one index
    refresh_all.py index-now NAME         same as --index NAME
    refresh_all.py index-now NAME PATH    reconvert one just-filed document, then rebuild
                                          (a plain index reruns its refresh commands)
    refresh_all.py discover               list project folders not in the registry
                                          (nothing is ever indexed from this)
    refresh_all.py status                 last run per index, entries, tags, usage, staleness
    refresh_all.py retag NAME [--all]     re-tag an index without reparsing: entries whose prompt
                                          or taxonomy hash changed (--all: every entry), then
                                          rebuild
    refresh_all.py triage NAME            list person-name candidates not on the allowlist
                                          (counts only; writes no corpus or state)
    refresh_all.py forget NAME SESSION_ID [--force]
                                          remove one session's records, tags and corpus
                                          files from an index, then rebuild

Options for a refresh: --full (reparse everything), --no-tag, --tag-budget N, --build (force
the engine build).

Needs spaCy (the `memory` extra). The engine build runs `fleetwatch index build NAME` (or an
index folder's own search.py) under settings.toml [paths] build_python (default: this Python). Paths, the owner's identity, the
zone, the heartbeat patterns, the rule packs and the protected groups come from settings.toml
(fleetwatch.settings.indexer); an invalid settings.toml stops the run before any index is touched.
Each index has an atomic mkdir lock (broken when older than 60 minutes); one index failing
never stops the others. Only one whole-registry run goes at a time (run_lock), and builds of
one index file wait for each other (the engine's build_lock). The process exits if its
resident memory passes 4 GB.
"""
import argparse
import contextlib
import fcntl
import glob as globmod
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml

from fleetwatch import migrate, settings as fleetwatch_settings
from fleetwatch.providers import format_counts

from . import antigravity_parse, codex_parse, doc_corpus, routing, session_parse, session_tagger
from .privacy_scan import RuleSet, Scanner, protected_ready

LOCK_STALE_S = 3600
# run_one and run_plain return True, False, or SKIPPED when another process holds the index's
# lock. SKIPPED is truthy, so a caller that only asks whether the index failed reads a skip as
# no failure; one that counts successes or failure streaks must test for it.
SKIPPED = "skipped"
BUILTIN_DEFAULTS = dict(schedule_minutes=30, tag=True, tag_budget_per_run=400)
MEM_CAP = 4 << 30


def configure(s=None):
    """Point this module at indexer settings (a fleetwatch.settings.IndexerSettings; default:
    settings.toml). Sets ROOT (each index's data folder), CONFIG_ROOT (the registry and each
    index's config; the same folder as ROOT in a one-root layout), REGISTRY, REGISTRY_DIR, FAILURES_LOG, PROJECTS, BUILD_PY,
    SETTINGS, PARSE_RULES, RULESET and COLUMN_DENYLIST. Raises SettingsError or ValueError
    (a rule pack that cannot be loaded)."""
    global SETTINGS, ROOT, CONFIG_ROOT, REGISTRY, REGISTRY_DIR, FAILURES_LOG, PROJECTS, BUILD_PY
    global PARSE_RULES, RULESET, COLUMN_DENYLIST
    s = s or fleetwatch_settings.indexer()
    rules = RuleSet(s.rule_packs)
    SETTINGS, RULESET = s, rules
    ROOT = s.index_root
    CONFIG_ROOT = s.config_root or ROOT
    REGISTRY, REGISTRY_DIR = CONFIG_ROOT / "registry.yaml", CONFIG_ROOT / "registry.d"
    FAILURES_LOG = ROOT / "logs" / "failures.log"
    PROJECTS, BUILD_PY = s.projects_dir, s.build_python
    PARSE_RULES = session_parse.TranscriptRules.from_settings(s)
    COLUMN_DENYLIST = list(s.column_denylist) if s.column_denylist is not None else list(
        dict.fromkeys(doc_corpus.DEFAULT_OPTS["column_denylist"] + rules.column_denylist))


# Importing never fails, but nothing scrubs under fallback rules: with a bad settings.toml or
# rule pack, main() reports it and stops, and Index refuses to start.
# Importing never moves a folder: the one-time folder migration runs only from a command-line
# entry point (fleetwatch's main, or this file run as a script), so a test or a library caller
# that imports this module never touches a real home.
SETTINGS_ERROR = None


def load_settings():
    """configure() from settings.toml; a bad file sets SETTINGS_ERROR and leaves fallback rules
    in place, which main() and Index refuse to run under."""
    global SETTINGS_ERROR
    SETTINGS_ERROR = None
    try:
        configure()
    except ValueError as e:
        SETTINGS_ERROR = e
        configure(fleetwatch_settings.indexer(raw={}))


load_settings()


def protection_problems(group, scanner, settings=None, doc_options=None):
    """Why an index in `group` may not refresh with this scanner, as a list of messages
    (empty when it may). A protected group (settings.toml [groups] protected) needs every
    redaction on: no keep_patterns, person names and anonymous-id pairing on, rule packs that
    supply both institution id patterns and anonymous-id rules (so a pack that went missing
    can never leave its ids in the corpus), and, when the entry's `doc_options` sets its own
    column_denylist, every column the composed list drops. Its `doc_options` may not set a
    column_keep: a keep word re-admits every denied header that also holds it."""
    settings = settings or SETTINGS
    if not fleetwatch_settings.is_protected(group, settings.protected_groups):
        return []
    out = []
    if not scanner.person_names:
        out.append("person_names: false is not allowed for a %s index" % group)
    if scanner.keep_res:
        out.append("keep_patterns is not allowed for a %s index; use keep: literals" % group)
    if scanner.rules.anon_res and not scanner.pairing_on:
        out.append("anon_id_pairing: false is not allowed for a %s index" % group)
    ok, reason = protected_ready(scanner.rules)
    if not ok:
        out.append("a %s index: %s" % (group, reason))
    cols = (doc_options or {}).get("column_denylist")
    if cols is not None:
        missing = sorted(set(doc_corpus.column_words(COLUMN_DENYLIST)) - set(doc_corpus.column_words(cols)))
        if missing:
            out.append("doc_options.column_denylist for a %s index must keep every column of the "
                       "composed list; it leaves out %d (add them, or drop the override)"
                       % (group, len(missing)))
    if doc_corpus.column_words((doc_options or {}).get("column_keep")):
        out.append("doc_options.column_keep is not allowed for a %s index; it re-admits denied "
                   "columns" % group)
    return out


def check_protected(name, group, scanner, settings=None, doc_options=None):
    """Raise ValueError naming every protection_problems() entry for index `name`."""
    problems = protection_problems(group, scanner, settings, doc_options)
    if problems:
        raise ValueError("%s: %s" % (name, "; ".join(problems)))


TRANSCRIPT_SOURCES = {"claude": session_parse.ClaudeSource, "codex": codex_parse.CodexSource,
                      "antigravity": antigravity_parse.AntigravitySource}
# Sources whose `transcripts:` block names one folder in `path` instead of `project_dir`.
PATH_SOURCES = ("codex", "antigravity")


def source_name(t):
    """The `source` of a `transcripts:` block: `claude` (the default; the block names
    `project_dir`), or `codex` or `antigravity` (the block names `path`, the folder that
    holds every session of that tool)."""
    name = t.get("source", "claude")
    if name not in TRANSCRIPT_SOURCES:
        raise ValueError("transcripts.source must be one of %s, not %r"
                         % (", ".join(TRANSCRIPT_SOURCES), name))
    return name


def transcript_source(t):
    """The source object for a `transcripts:` block."""
    return TRANSCRIPT_SOURCES[source_name(t)]()


def agent_sources():
    """Provider-agnostic map of routable external agent sources ({provider_name: source_cls}).
    Any source registered in TRANSCRIPT_SOURCES that implements run_meta() participates."""
    return {name: cls for name, cls in TRANSCRIPT_SOURCES.items()
            if cls is not session_parse.ClaudeSource and callable(getattr(cls, "run_meta", None))}


def routing_cache_path():
    # Leading dot keeps the cache out of logs/ and guarantees it can never collide with an index name.
    return ROOT / ".routing-cache.json"


def resolve_routing(reg, ledger_path=None, git_cmd=None):
    return routing.resolve_all(
        reg, SETTINGS, agent_sources(), ledger_path=ledger_path,
        git_cmd=git_cmd, cache_path=routing_cache_path(), project_dirs_fn=project_dirs,
        placement_path=Path(fleetwatch_settings.state_dir()) / "placements.json")


def _merge_session_records(groups_of_records):
    """Merge records from Claude and routed agent sources while keeping each session's exchanges
    contiguous in chronological order by session start time."""
    by_session = {}
    order = []
    for recs in groups_of_records:
        for r in recs:
            key = (r.get("provider") or "", r["session"])
            if key not in by_session:
                by_session[key] = []
                order.append(key)
            by_session[key].append(r)
    by_key = sorted(order, key=lambda k: (by_session[k][0].get("ts") or "", k[1], k[0]))
    out = []
    for k in by_key:
        out.extend(by_session[k])
    return out


def load_routed_runs(runs, provider, src_cls, provider_state, scanner, opts, rules,
                     full=False, triage=None, on_missing="hold", all_present_sids=None,
                     unreadable_sids=None):
    """Parse and scrub routed agent runs assigned to one index for one provider.

    Constraint: a previously stored run that still exists on disk but resolved to a different
    index (or unplaced) is always removed here, so no run stays indexed in two indexes.
    """
    if on_missing not in session_parse.ON_MISSING:
        raise ValueError("transcripts.on_missing must be one of %s, not %r"
                         % (", ".join(session_parse.ON_MISSING), on_missing))
    sess = provider_state / "sessions"
    if not runs and not sess.is_dir():
        return [], dict(sessions=0, new=0, files_reparsed=0, removed=0, held=0)
    sess.mkdir(parents=True, exist_ok=True)
    source = src_cls()
    sig = "%s|%s" % (scanner.version, session_parse.opts_sig(opts))
    keep = sorted(((r.start_time or 0.0, r.session_id, r.path) for r in runs),
                  key=lambda item: (item[0], item[1]))
    keep_sids = {sid for _, sid, _ in keep}
    present_sids = set(all_present_sids or ()) | keep_sids
    unreadable_sids = set(unreadable_sids or ())

    held, removed = [], set()
    for p in sorted(sess.glob("*.jsonl")) + sorted(sess.glob("*.cursor.json")):
        sid = p.name.split(".")[0]
        if sid in keep_sids:
            continue
        moved_elsewhere = sid in present_sids and sid not in unreadable_sids
        if sid in unreadable_sids or (not moved_elsewhere and on_missing == "hold"):
            if p.name == sid + ".jsonl" and triage is None:
                recs = session_parse.hold_records(sess, sid, scanner, sig, full, opts)
                if recs:
                    held.append((recs[0]["ts"], sid, recs))
            continue
        removed.add(sid)
        if triage is None:
            p.unlink()

    allrecs, n_new, n_files = [], 0, 0
    for _ts0, sid, path in sorted(keep + held, key=lambda k: (str(k[0]), k[1])):
        sp = sess / (sid + ".jsonl")
        curp = sess / (sid + ".cursor.json")
        if isinstance(path, list):
            allrecs += session_parse.drop_replays(path)
            continue
        old, cursor = [], None
        if sp.exists() and curp.exists() and not full and triage is None:
            cursor = json.loads(curp.read_text())
            if cursor.get("scan_version") != sig:
                cursor = None
            else:
                old = session_parse.read_records(sp)
        recs, cur, old = source.parse(path, cursor, old, opts, rules)
        for r in recs:
            r.pop("seen", None)
            r["session"] = sid
            r["id"] = session_parse.exchange_id(sid, r.get("ex", "EX"), r["n"], opts,
                                                source.id_stem(sid))
            r.setdefault("provider", provider)
        session_parse.withhold_matching(recs, opts.get("withhold_patterns"))
        if recs:
            n_files += 1
            scanner.scan_records(recs, triage)
        n_new += len(recs)
        merged = old + recs
        if triage is None and (recs or (getattr(source, "keep_empty", False)
                                        and cur is not None and cur is not cursor)):
            tmp = sp.with_suffix(".tmp")
            with open(tmp, "w") as fh:
                for r in merged:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(tmp, sp)
            cur["scan_version"] = sig
            curp.write_text(json.dumps(cur))
        part = session_parse.drop_replays(merged)
        try:
            recent = path.stat().st_mtime > time.time() - 900
        except OSError:
            recent = False
        if part and recent:
            part[-1]["open"] = True
        allrecs += part
    return allrecs, dict(sessions=len(keep), new=n_new, files_reparsed=n_files,
                         removed=len(removed), held=len(held), **source.stats())


def project_dirs(t):
    """An index's transcript folders. For a Claude index `project_dir` is one folder or a
    list of them; a list covers a session whose working directory moved, which starts a new
    project folder. A Codex or Antigravity index names its sessions folder in `path`."""
    name = source_name(t)
    if name in PATH_SOURCES:
        if not t.get("path"):
            raise ValueError("transcripts.path is required when transcripts.source is %s" % name)
        return [Path(os.path.expanduser(t["path"]))]
    pd = t["project_dir"]
    return [Path(os.path.expanduser(p)) for p in (pd if isinstance(pd, list) else [pd])]


def arm_memory_cap(limit=MEM_CAP):
    """macOS refuses RLIMIT_AS, so a watchdog enforces the cap on peak resident size."""
    scale = 1 if sys.platform == "darwin" else 1024

    def watch():
        while True:
            if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale > limit:
                sys.stderr.write("memory cap of %d GB reached; exiting\n" % (limit >> 30))
                os._exit(70)
            time.sleep(0.5)
    threading.Thread(target=watch, daemon=True).start()


def load_registry(registry=None, registry_dir=None):
    """registry.yaml holds defaults, groups and older entries. Each file in registry.d/ holds
    one session's own entries under `indexes:`, so sessions joining at the same time never
    edit the same file.

    While a session moves its entry out of registry.yaml, both copies exist for a moment: the
    registry.d copy wins and the inline one is ignored with a warning. A name defined in two
    registry.d files is ambiguous, so that one index is skipped with an error and every other
    index still refreshes; one session's mistake never stops the job for everyone.

    `registry` and `registry_dir` default to REGISTRY and REGISTRY_DIR."""
    registry = REGISTRY if registry is None else Path(registry)
    registry_dir = REGISTRY_DIR if registry_dir is None else Path(registry_dir)
    reg = (yaml.safe_load(registry.read_text()) or {}) if registry.exists() else {}
    reg.setdefault("defaults", dict(BUILTIN_DEFAULTS))
    inline = dict(reg.get("indexes") or {})
    dropins, owners, ambiguous = {}, {}, set()
    if registry_dir.is_dir():
        for f in sorted(registry_dir.glob("*.yaml")):
            try:
                part = yaml.safe_load(f.read_text()) or {}
            except yaml.YAMLError as e:
                print("registry.d/%s does not parse, skipped: %s" % (f.name, e), file=sys.stderr)
                continue
            for name, entry in (part.get("indexes") or {}).items():
                if name in dropins:
                    ambiguous.add(name)
                    print("index %r is defined in both registry.d/%s and registry.d/%s; skipped"
                          % (name, owners[name], f.name), file=sys.stderr)
                    continue
                dropins[name], owners[name] = entry, f.name
    for name in sorted(set(inline) & set(dropins)):
        print("index %r is in registry.yaml and registry.d/%s; using registry.d (remove the "
              "inline copy)" % (name, owners[name]), file=sys.stderr)
    merged = {**inline, **dropins}
    for name in ambiguous:
        merged.pop(name, None)
    # An entry with a name no index may have is never acted on, but it stays in
    # reg["invalid"]: it still names an owner, a group and a name that ownership, the trust
    # guard and the duplicate checks must see.
    invalid = {}
    for name in sorted(merged, key=str):
        why = index_name_problem(name)
        if why:
            print("%s; that index is skipped" % why, file=sys.stderr)
            invalid[name] = merged.pop(name)
    reg["indexes"], reg["invalid"] = merged, invalid
    return reg

INDEX_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
# Names of what fleetwatch keeps beside the index folders: the registry and logs under the
# roots, and its own files when a root is set to the config or data folder itself. An index
# of one of these names would have that file or folder as its own, and `migrate --force`
# replaces an index's own folder.
RESERVED_INDEX_NAMES = frozenset({
    "registry.yaml", "registry.d", "logs", "indexes", "settings.toml", "groups.toml",
    "state.json", "prune.toml", "parked.json", "resume-cache.json", "probes.yaml",
    "search_config.yaml"})


def index_name_problem(name):
    """Why `name` cannot name an index, or None. An index name becomes a folder under the
    config and data roots and a registry.d file name, so it is a single path component:
    lowercase letters, digits, '.', '-' and '_', starting with a letter or digit, never '..',
    and never one of RESERVED_INDEX_NAMES."""
    name = str(name)
    if not INDEX_NAME_RE.fullmatch(name) or ".." in name:
        return ("index name %r is not valid: use lowercase letters, digits, '.', '-' and '_', "
                "starting with a letter or digit, without '..'" % name)
    if name in RESERVED_INDEX_NAMES:
        return ("index name %r is not valid: it is reserved for fleetwatch's own files beside "
                "the index folders" % name)
    return None


def now_iso():
    return datetime.now(PARSE_RULES.tz).isoformat(timespec="seconds")


# A refresh run holds its run lock for its whole life, and the kernel frees that lock only when
# the run ends, so a build that never returns would stop every later scheduled run. Each build
# command is therefore bounded; on timeout its whole process group is killed, so a grandchild
# (the engine build a refresh command starts) cannot keep the index's writer lock either.
BUILD_TIMEOUT_S = 1800


def run_bounded(argv, cwd, timeout=None):
    """subprocess.run(argv, capture_output=True, text=True) in its own session, killed with its
    whole process group after `timeout` seconds (BUILD_TIMEOUT_S by default). A timeout raises
    RuntimeError naming the command, which fails that index only."""
    timeout = BUILD_TIMEOUT_S if timeout is None else timeout
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise RuntimeError("timed out after %ds: %s" % (timeout, " ".join(str(a) for a in argv[1:])))
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


class Lock:
    def __init__(self, state):
        self.path = state / "refresh.lock.d"
        self.held = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.mkdir()
        except FileExistsError:
            if time.time() - self.path.stat().st_mtime > LOCK_STALE_S:
                shutil.rmtree(self.path, ignore_errors=True)
                self.path.mkdir()
            else:
                return None
        self.held = True
        return self

    def touch(self):
        """Refresh the lock's age; a live run calls this between phases and tag batches so
        the 60-minute rule only ever breaks a lock whose process is gone."""
        if self.held:
            try:
                os.utime(self.path)
            except OSError:
                pass

    def __exit__(self, *exc):
        if self.held:
            shutil.rmtree(self.path, ignore_errors=True)


class Index:
    def __init__(self, name, entry, defaults):
        self.name, self.entry, self.defaults = name, entry, defaults
        self.dir = ROOT / name              # data: corpus, index, state
        self.cdir = CONFIG_ROOT / name      # config: tags, probes, allowlist, scripts
        self.state = self.dir / "state"
        if SETTINGS_ERROR is not None:
            raise ValueError("settings: %s" % SETTINGS_ERROR)
        # The packs load again for every index, so a pack file removed or broken since import
        # stops this index instead of scrubbing without it.
        self.scanner = Scanner(CONFIG_ROOT / entry["privacy"]["allowlist"],
                               CONFIG_ROOT / entry["privacy"]["denylist"]
                               if entry["privacy"].get("denylist") else None,
                               RuleSet(SETTINGS.rule_packs))
        check_protected(name, entry.get("group"), self.scanner, doc_options=entry.get("doc_options"))
        self.tc = session_tagger.TagConfig(yaml.safe_load((CONFIG_ROOT / entry["tags"]).read_text()),
                                           SETTINGS.default_guidance,
                                           source_name(entry.get("transcripts") or {}))
        self.index_file = self.dir / "index" / ("%s.sqlite" % name)

    # ------------------------------------------------------------------ pieces

    def docs_specs(self):
        return self.entry.get("docs") or []

    def doc_options(self):
        """The index's doc_options over the column denylist the settings compose."""
        return dict(dict(column_denylist=COLUMN_DENYLIST), **(self.entry.get("doc_options") or {}))

    def transcripts(self, plan=None, **kw):
        t = self.entry.get("transcripts")
        if t:
            opts = session_parse.parse_opts(t)
            self.state.mkdir(parents=True, exist_ok=True)
            (self.state / "parse_options.json").write_text(json.dumps(opts, sort_keys=True))
            records, skipped, st = session_parse.load_transcripts(
                project_dirs(t), self.state, self.scanner,
                sessions=t.get("sessions", "all"),
                exclude_automated=t.get("exclude_automated", True), opts=opts, rules=PARSE_RULES,
                on_missing=t.get("on_missing", "hold"), source=transcript_source(t), **kw)
        else:
            records, skipped = [], {}
            st = dict(sessions=0, new=0, files_reparsed=0, removed=0, held=0)
        if self.entry.get("kind") != "plain" and (not t or source_name(t) == "claude"):
            if plan is None and (REGISTRY.exists() or REGISTRY_DIR.is_dir()):
                reg = load_registry()
                if self.name in reg["indexes"]:
                    plan = resolve_routing(reg)
            if plan is not None:
                agent_counts = plan.counts_for(self.name)
                runs_by_provider = plan.runs_for(self.name)
                agents_root = self.state / "agents"
                existing = ({p.name for p in agents_root.iterdir() if p.is_dir()}
                            if agents_root.is_dir() else set())
                routed_opts = dict(session_parse.parse_opts(t), bare_exchange_ids=False)
                on_missing = (t or {}).get("on_missing", "hold")
                sources = agent_sources()
                extra_groups = []
                for provider in sorted(set(runs_by_provider) | existing):
                    src_cls = sources.get(provider)
                    if src_cls is None:
                        continue
                    r_recs, rst = load_routed_runs(
                        runs_by_provider.get(provider, []), provider, src_cls,
                        agents_root / provider, self.scanner, routed_opts, PARSE_RULES,
                        full=kw.get("full", False), triage=kw.get("triage"),
                        on_missing=on_missing, all_present_sids=plan.all_present.get(provider),
                        unreadable_sids=plan.unreadable.get(provider))
                    if r_recs:
                        extra_groups.append(r_recs)
                    for key, value in rst.items():
                        if isinstance(value, dict):
                            counts = st.setdefault(key, {})
                            for item, count in value.items():
                                counts[item] = counts.get(item, 0) + count
                        elif isinstance(value, (int, float)):
                            st[key] = st.get(key, 0) + value
                        elif isinstance(value, list):
                            st.setdefault(key, []).extend(value)
                if extra_groups:
                    records = _merge_session_records([records] + extra_groups)
                st["agent_runs"] = agent_counts
            else:
                st.setdefault("agent_runs", {routing.REASON_LAUNCH: 0, routing.REASON_FOLDER: 0,
                                             routing.REASON_CATCH_ALL: 0})
        return records, skipped, st

    def build_marker(self):
        """Present from just before an engine build until it succeeds: a failed build leaves
        it, so the next refresh rebuilds even when the corpus has not changed since."""
        return self.state / "build-pending"

    def build_if(self, needed):
        """Build when `needed` or when an earlier build failed. Returns (built, seconds, line,
        retried) and raises what build() raises, leaving the marker for the next run."""
        marker = self.build_marker()
        retried = marker.exists()
        if not (needed or retried):
            return False, 0, "", False
        self.state.mkdir(parents=True, exist_ok=True)
        marker.write_text(now_iso() + "\n")
        build_s, line = self.build()
        marker.unlink()
        return True, build_s, line, retried

    def build(self):
        t = time.time()
        wrapper = self.dir / "search.py"
        # The build never runs from the caller's folder: -P keeps it off sys.path for the
        # module form (a script's own folder leads the path instead), and cwd is the index's
        # data folder, so a module file where refresh was started is never imported.
        argv = ([BUILD_PY, str(wrapper), "build"] if wrapper.exists()
                else [BUILD_PY, "-P", "-m", "fleetwatch", "index", "build", self.name])
        p = run_bounded(argv, self.dir)
        if p.returncode != 0:
            raise RuntimeError("engine build failed: " + p.stderr.strip()[-400:])
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("Indexed")), "")
        return round(time.time() - t, 1), line

    def write_report(self, records, skipped, doc_cats, docs_dropped=()):
        cats, per_cat = {}, {}
        for r in records:
            for c, n in (r.get("scan") or {}).items():
                cats[c] = cats.get(c, 0) + n
                per_cat.setdefault(c, []).append(r["id"])
        order = ("secret", "email", "numeric-id", "keyed-id", "person-name", "anon-id-pairing",
                 "private-path-mention")
        out = ["# Privacy scan report", "",
               "Generated %s by session-indexer over %d exchanges (scan version %s)."
               % (time.strftime("%Y-%m-%d %H:%M"), len(records), self.scanner.version),
               "Redacted values are never written here, only the category and the exchange id.",
               "`kept-*` rows count spans left alone by the allowlist `keep` and `keep_patterns` "
               "entries, by the category the span would have been redacted under; a large count "
               "means a keep entry is too broad.", "",
               "| Category | Redactions or flags | Exchanges | Documents |", "|---|---|---|---|"]
        kept_cats = sorted({c for c in list(cats) + list(doc_cats) if c.startswith("kept-")})
        for c in list(order) + kept_cats:
            out.append("| %s | %d | %d | %d |" % (c, cats.get(c, 0), len(per_cat.get(c, [])),
                                                  doc_cats.get(c, 0)))
        out += ["", "Dropped exchanges: %s." % (", ".join(
            "%s" % r["id"] for r in records if r.get("dropped")) or "none")]
        if docs_dropped:
            out.append("Dropped documents (never written to the corpus; reason in "
                       "state/docs-manifest.json): %d." % len(docs_dropped))
        out.append("")
        for c in list(order) + kept_cats:
            ids = per_cat.get(c, [])
            out += ["## %s" % c, ""]
            out += ([", ".join(ids[:400]) + (" ... (%d more)" % (len(ids) - 400) if len(ids) > 400 else "")]
                    if ids else ["None."])
            out.append("")
        out += ["## Sessions not indexed (automated one-shot runs)", "",
                "%d transcript files are CI security reviews, readiness probes or judge calls, "
                "not the session's conversation." % len(skipped), ""]
        (self.dir / "scan-report.md").write_text("\n".join(out) + "\n")

    # ------------------------------------------------------------------- refresh

    def refresh(self, opts, beat=lambda: None, plan=None):
        t0 = time.time()
        private_dirs(ROOT, self.dir)
        self.state.mkdir(parents=True, exist_ok=True)
        info = dict(index=self.name, started=now_iso())
        cfgd = self.defaults
        records, skipped, st = self.transcripts(full=opts.full, plan=plan)
        beat()
        t_parse = time.time()
        for r in records:
            r["hash"] = session_tagger.tag_hash(r)
        live = [r for r in records if not r.get("dropped")]
        provider = source_name(self.entry.get("transcripts") or {})
        exchanges_by_provider = Counter(r.get("provider") or provider for r in live)
        tags_path, usage_path = self.state / "tags.jsonl", self.state / "usage.jsonl"
        first_run = not tags_path.exists()
        cache = session_tagger.load_tags(tags_path)
        # A withheld exchange's cached summary and topics describe the text the policy hides,
        # so they are purged whether or not this run tags.
        withheld = [r["id"] for r in records if r.get("withheld") and r["id"] in cache]
        for k in withheld:
            del cache[k]
        rescrubbed = [rec for rec in cache.values() if rec.get("sv") != self.scanner.version]
        for rec in rescrubbed:      # tags written under older scan rules are scrubbed again
            self.scanner.scrub_tag(rec)
            rec["topics"] = session_tagger.normalize_topics(rec.get("topics"))
        tag_stats = dict(attempted=0, tagged=0, failed=0, deferred=0)
        do_tag = cfgd.get("tag", True) and not opts.no_tag and bool(self.entry.get("transcripts") or live)
        if do_tag:
            budget = opts.tag_budget if opts.tag_budget is not None else (
                self.entry.get("first_run_tag_budget", cfgd["tag_budget_per_run"]) if first_run
                else self.entry.get("tag_budget_per_run", cfgd["tag_budget_per_run"]))
            tag_stats = session_tagger.tag_records(live, cache, self.tc, tags_path, usage_path,
                                                   budget, str(self.dir), self.scanner,
                                                   force=opts.retag_all, beat=beat,
                                                   tz=PARSE_RULES.tz)
            if tag_stats["tagged"] or first_run or rescrubbed or withheld:
                session_tagger.compact_tags(tags_path, cache)
        elif withheld:
            session_tagger.compact_tags(tags_path, cache)
        beat()
        t_tag = time.time()

        corpus_session = self.dir / "corpus" / "session"
        sess_changed = 0
        n_live = len(live)
        # The file count catches exchanges of a session that left the index, or whose stored
        # records an earlier run dropped, whatever this run parsed. The rewrite keeps every
        # live and held session; only files no stored record backs are removed.
        stale = corpus_session.exists() and sum(1 for _ in corpus_session.rglob("*.md")) != len(live)
        if st["new"] or st.get("removed") or stale or tag_stats["tagged"] or rescrubbed \
                or withheld or not corpus_session.exists():
            n_live, sess_changed = session_tagger.write_corpus(live, cache, self.tc, corpus_session,
                                                               PARSE_RULES)
        beat()
        t_write = time.time()

        docs = dict(changed=[], removed=[], unchanged=0, errors=[], categories={},
                    by_type={}, skipped=[], dropped_columns={})
        if self.docs_specs():
            docs = doc_corpus.convert_docs(self.docs_specs(), self.dir / "corpus" / "docs",
                                           self.state / "docs-manifest.json", self.scanner,
                                           full=opts.full, opts=self.doc_options())
        beat()
        t_docs = time.time()
        if st["new"] or st.get("removed") or docs["changed"] or docs["removed"] or docs.get("dropped"):
            self.write_report(live, skipped, docs["categories"], docs.get("dropped", ()))

        changed = sess_changed + len(docs["changed"]) + len(docs["removed"])
        beat()
        has_corpus = n_live > 0 or bool(docs["changed"]) or docs["unchanged"] > 0
        if has_corpus:
            built, build_s, idx_line, retried = self.build_if(
                changed or opts.build or not self.index_file.exists())
        else:
            if self.index_file.exists():
                self.index_file.unlink()
            built, build_s, idx_line, retried = False, 0, "", False
        info.update(
            finished=now_iso(), wall_s=round(time.time() - t0, 1),
            parse_s=round(t_parse - t0, 1), tag_s=round(t_tag - t_parse, 1),
            write_s=round(t_write - t_tag, 1), docs_s=round(t_docs - t_write, 1),
            build_s=build_s, built=built, index_line=idx_line, build_retried=retried,
            sessions=st["sessions"], new_exchanges=st["new"], exchanges=n_live,
            exchanges_by_provider=dict(exchanges_by_provider),
            **{k: st[k] for k in ("files_read", "files_failed", "files_vanished", "lines_skipped",
                                  "zst_skipped", "conversations_read", "conversations_skipped",
                                  "conversations_vanished", "steps_skipped", "unknown_steps")
               if k in st},
            sessions_removed=st.get("removed", 0), sessions_held=st.get("held", 0),
            agent_runs=st.get("agent_runs", {routing.REASON_LAUNCH: 0, routing.REASON_FOLDER: 0,
                                             routing.REASON_CATCH_ALL: 0}),
            corpus_files_changed=sess_changed, tags=tag_stats, tags_cached=len(cache),
            docs_changed=len(docs["changed"]), docs_removed=len(docs["removed"]),
            docs_unchanged=docs["unchanged"], doc_errors=docs["errors"],
            doc_converted_by_type=docs["by_type"],
            doc_skipped=[list(x) for x in docs["skipped"]],
            doc_columns_dropped=docs["dropped_columns"],
            doc_recalculated=docs.get("recalculated", 0),
            doc_recalc_failed=docs.get("recalc_failed", 0), ok=True)
        (self.state / "last_run.json").write_text(json.dumps(info, indent=1))
        return info

    def forget(self, sid):
        """Remove one session from this index: its stored records and cursor, its tag
        records, its automated-run note, and every corpus file whose header names it; then
        rebuild. Returns counts only."""
        sess = self.state / "sessions"
        sess_dirs = [sess]
        agents_root = self.state / "agents"
        if agents_root.is_dir():
            sess_dirs += [p / "sessions" for p in sorted(agents_root.iterdir()) if (p / "sessions").is_dir()]
        ids = set()
        stored = 0
        for sd in sess_dirs:
            sp = sd / (sid + ".jsonl")
            if sp.exists():
                ids.update(r["id"] for r in session_parse.read_records(sp))
            for f in (sp, sd / (sid + ".cursor.json")):
                if f.exists():
                    f.unlink()
                    stored += 1
        skip_path = self.state / "skipped-sessions.json"
        if skip_path.exists():
            skipped = json.loads(skip_path.read_text())
            if skipped.pop(sid, None) is not None:
                skip_path.write_text(json.dumps(skipped, indent=1))
        tags_path = self.state / "tags.jsonl"
        cache = session_tagger.load_tags(tags_path)
        gone = [k for k in cache if k in ids]
        for k in gone:
            del cache[k]
        n_tags = len(gone)
        if n_tags:
            session_tagger.compact_tags(tags_path, cache)
        # Exchange ids may be bare (EX-0001) and shared across sessions, so a file is matched
        # by the Session: line its header carries, not by its name.
        header = re.compile(r"(?m)^Session: %s$" % re.escape(sid))
        n_files = 0
        corpus = self.dir / "corpus" / "session"
        for f in sorted(corpus.rglob("*.md")) if corpus.exists() else []:
            if header.search(f.read_text().partition("\n\n## ")[0]):
                f.unlink()
                n_files += 1
        built, build_s, _line, _retried = self.build_if(bool(n_files or ids))
        return dict(index=self.name, session=sid, stored_files=stored, exchanges=len(ids),
                    corpus_files=n_files, tags=n_tags, built=built, build_s=build_s)

    def index_now(self, path, opts):
        t0 = time.time()
        docs = doc_corpus.convert_docs(self.docs_specs(), self.dir / "corpus" / "docs",
                                       self.state / "docs-manifest.json", self.scanner, only=path,
                                       opts=self.doc_options())
        built, build_s, _line, _retried = self.build_if(bool(docs["changed"] or docs["removed"]))
        return dict(index=self.name, path=path, reconverted=docs["changed"],
                    unchanged=docs["unchanged"], errors=docs["errors"], built=built,
                    build_s=build_s, wall_s=round(time.time() - t0, 1))


class UnknownIndex(KeyError):
    def __str__(self):
        return self.args[0]


def selected(reg, name):
    idxs = reg["indexes"]
    if name and name not in idxs:
        raise UnknownIndex("no index %r in registry (have: %s)" % (name, ", ".join(idxs)))
    return [name] if name else list(idxs)


# A plain index has no transcripts, tagging or scrub: it is a hand-maintained
# document index (the methodology library, a knowledge base) with its own
# rebuild commands. The refresher runs those commands, in order, only when a
# source file is newer than the index file, and records the run like any other.
# Its scripts locate the corpus beside themselves, so the whole index (scripts,
# search_config.yaml, probes, corpus/, index/, state/) is one folder in the data home.
PYTHON_TOKEN = "{python}"


def plain_dir(name):
    """The folder of plain index `name`: ROOT/<name>. A plain index whose search_config.yaml
    is only in the config home (config and data split like a session index) is found there."""
    d, split = ROOT / name, CONFIG_ROOT / name
    if not (d / "search_config.yaml").exists() and (split / "search_config.yaml").exists():
        return split
    return d


def plain_argv(cmd):
    """One refresh command as run: the {python} token becomes the running interpreter, so
    the commands follow whichever venv runs the refresher; other arguments get ~ expanded."""
    return [sys.executable if a == PYTHON_TOKEN else os.path.expanduser(str(a)) for a in cmd]


def plain_source_files(entry):
    """Every file a plain index's sources name: a path, a glob (** allowed), or a
    directory walked recursively, skipping hidden entries and `skip_dirs`."""
    skip = set(entry.get("skip_dirs", []))
    for pattern in entry["sources"]:
        for m in globmod.glob(os.path.expanduser(pattern), recursive=True):
            m = Path(m)
            if m.is_file():
                yield m
            elif m.is_dir():
                for dirpath, dirnames, filenames in os.walk(m):
                    dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in skip]
                    for f in filenames:
                        if not f.startswith("."):
                            yield Path(dirpath) / f


def run_plain(name, entry, opts):
    state = ROOT / name / "state"
    state.mkdir(parents=True, exist_ok=True)
    with Lock(state) as lock:
        if lock is None:
            print("[%s] refresh already running; skipped" % name)
            return SKIPPED
        return _run_plain_locked(name, entry, opts, state, lock)


def _run_plain_locked(name, entry, opts, state, lock):
    index_file = Path(os.path.expanduser(entry["index_file"]))
    built_at = index_file.stat().st_mtime if index_file.exists() else 0
    newer = []
    for p in plain_source_files(entry):
        try:
            if p.stat().st_mtime > built_at:
                newer.append(p.name)
        except OSError:
            continue
    t0 = time.time()
    if not newer and not opts.build and not opts.full:
        print("[%s] plain: sources checked, none newer than the index; build skipped" % name)
        info = dict(index=name, kind="plain", finished=now_iso(), ok=True, built=False,
                    changed=[], wall_s=0.0)
    else:
        for cmd in entry["refresh"]:
            lock.touch()
            argv = plain_argv(cmd)
            r = run_bounded(argv, plain_dir(name))
            if r.returncode != 0:
                (state / "last_error.log").write_text(
                    "%s\n%s\nexit %d\n%s\n%s" % (now_iso(), " ".join(argv), r.returncode,
                                                 r.stdout[-4000:], r.stderr[-4000:]))
                (state / "last_run.json").write_text(json.dumps(dict(
                    index=name, kind="plain", finished=now_iso(), ok=False,
                    error="exit %d from %s" % (r.returncode, " ".join(argv[1:]))), indent=1))
                print("[%s] FAILED: %s exited %d (detail in %s)"
                      % (name, " ".join(argv[1:]), r.returncode, state / "last_error.log"))
                return False
        info = dict(index=name, kind="plain", finished=now_iso(), ok=True, built=True,
                    changed=sorted(set(newer)), wall_s=round(time.time() - t0, 1))
        print("[%s] plain: rebuilt in %.1fs; %d source(s) newer than the index: %s"
              % (name, info["wall_s"], len(info["changed"]), ", ".join(info["changed"][:8])))
    (state / "last_run.json").write_text(json.dumps(info, indent=1))
    return True


def run_one(reg, name, opts, plan=None):
    if plan is None:
        plan = getattr(opts, "routing_plan", None)
    if reg["indexes"][name].get("kind") == "plain":
        try:
            return run_plain(name, reg["indexes"][name], opts)
        except Exception as e:
            print("[%s] FAILED: %s: %s" % (name, type(e).__name__, e))
            return False
    try:
        ix = Index(name, reg["indexes"][name], reg["defaults"])
    except Exception as e:        # a bad registry entry fails this index only
        print("[%s] FAILED: %s: %s" % (name, type(e).__name__, e))
        return False
    with Lock(ix.state) as lock:
        if lock is None:
            print("[%s] refresh already running; skipping" % name)
            return SKIPPED
        try:
            info = ix.refresh(opts, lock.touch, plan=plan)
        except Exception as e:
            ix.state.mkdir(parents=True, exist_ok=True)
            (ix.state / "last_error.log").write_text("%s\n%s" % (now_iso(), traceback.format_exc()))
            (ix.state / "last_run.json").write_text(json.dumps(
                dict(index=name, finished=now_iso(), ok=False, error="%s: %s" % (type(e).__name__, e)),
                indent=1))
            print("[%s] FAILED: %s: %s (traceback in %s)" % (name, type(e).__name__, e,
                                                           ix.state / "last_error.log"))
            return False
    t = info["tags"]
    print("[%s] %d exchanges (%d new) over %d sessions; docs %d changed, %d removed, %d unchanged"
          % (name, info["exchanges"], info["new_exchanges"], info["sessions"],
             info["docs_changed"], info["docs_removed"], info["docs_unchanged"]))
    ar = info.get("agent_runs") or {}
    if any(ar.values()):
        print("[%s] agent runs: %d by launch, %d by folder, %d by catch-all"
              % (name, ar.get(routing.REASON_LAUNCH, 0), ar.get(routing.REASON_FOLDER, 0),
                 ar.get(routing.REASON_CATCH_ALL, 0)))
    if info.get("sessions_removed") or info.get("sessions_held"):
        print("[%s] sessions without a transcript: %d held from stored records, %d removed "
              "from the index" % (name, info.get("sessions_held", 0), info.get("sessions_removed", 0)))
    if info.get("doc_converted_by_type") or info.get("doc_skipped") or info.get("doc_errors"):
        print("[%s] docs converted by type %s; %d skipped, %d failed, %d columns dropped"
              % (name, json.dumps(info.get("doc_converted_by_type", {}), sort_keys=True),
                 len(info.get("doc_skipped", [])), len(info.get("doc_errors", [])),
                 sum((info.get("doc_columns_dropped") or {}).values())))
        for fn, why in info.get("doc_skipped", []):
            print("[%s]   skipped %s (%s)" % (name, fn, why))
    print("[%s] tags: %d attempted, %d tagged, %d failed, %d deferred (%d cached)"
          % (name, t["attempted"], t["tagged"], t["failed"], t["deferred"], info["tags_cached"]))
    if t.get("usage"):
        u = t["usage"]
        print("[%s] tag usage: %d calls, input %d + cache-read %d + cache-creation %d, output %d, $%.4f"
              % (name, t["calls"], u["input_tokens"], u["cache_read_tokens"],
                 u["cache_creation_tokens"], u["output_tokens"], u["cost_usd"]))
    if "files_read" in info:
        print("[%s] codex rollouts read: %d plain, %d compressed; %d unreadable, %d moved during "
              "the run; %d malformed lines skipped"
              % (name, info["files_read"]["plain"], info["files_read"]["zst"],
                 len(info["files_failed"]), len(info["files_vanished"]), info["lines_skipped"]))
        if info.get("zst_skipped"):
            print("[%s] codex: %d compressed rollouts skipped because zstd is not installed; "
                  "install it (macOS: brew install zstd) and refresh again"
                  % (name, info["zst_skipped"]))
    if "conversations_read" in info:
        skipped = info["conversations_skipped"]
        reasons = {}
        for _, why in skipped:
            reasons[why] = reasons.get(why, 0) + 1
        print("[%s] antigravity conversations read: %d; %d skipped%s; %d deleted during the run; "
              "%d malformed steps skipped"
              % (name, info["conversations_read"], len(skipped),
                 " (%s)" % ", ".join("%s %d" % kv for kv in sorted(reasons.items()))
                 if reasons else "", len(info["conversations_vanished"]), info["steps_skipped"]))
        for fn, why in skipped:
            print("[%s]   skipped %s (%s)" % (name, fn, why))
        if info.get("unknown_steps"):
            print("[%s] antigravity step types not in the mapping for agy %s, ignored: %s"
                  % (name, antigravity_parse.MAPPED_VERSION,
                     ", ".join("%s x%d" % kv for kv in sorted(info["unknown_steps"].items()))))
    if info.get("build_retried"):
        print("[%s] build: retried, because the previous build failed" % name)
    print("[%s] build: %s; total %ss (parse %ss, tag %ss, write %ss, docs %ss, build %ss)"
          % (name, "ran. " + info["index_line"] if info["built"] else "skipped, no corpus change",
             info["wall_s"], info["parse_s"], info["tag_s"], info["write_s"], info["docs_s"],
             info["build_s"]))
    return True


# ------------------------------------------------------------------------ commands

def cmd_discover(reg):
    registered = set()
    for e in reg["indexes"].values():
        t = e.get("transcripts")
        if t:
            registered.update(d.resolve() for d in project_dirs(t))
    rows = []
    for d in sorted(PROJECTS.iterdir()):
        if not d.is_dir() or d.resolve() in registered:
            continue
        top = list(d.glob("*.jsonl"))
        if not top:
            continue
        size = sum(f.stat().st_size for f in top)
        newest = max(f.stat().st_mtime for f in top)
        guess = next((g for pat, g in SETTINGS.group_guess if pat in d.name + "-"), "unknown")
        rows.append((size, newest, d.name, guess, len(top)))
    rows.sort(reverse=True)
    print("%d unregistered project folders with transcripts (never indexed; register by hand)\n"
          % len(rows))
    print("%9s  %-10s  %-20s  %5s  %s" % ("MB", "last used", "probable group", "files", "folder"))
    for size, newest, name, guess, n in rows[:40]:
        print("%9.1f  %-10s  %-20s  %5d  %s" % (size / 1e6, datetime.fromtimestamp(newest).strftime("%Y-%m-%d"),
                                              guess, n, name))
    if len(rows) > 40:
        print("... %d smaller folders not shown" % (len(rows) - 40))


def cmd_status(reg, full_reg=None):
    plan, routing_err = None, None
    try:
        plan = resolve_routing(full_reg if full_reg is not None else reg)
    except ValueError as exc:
        routing_err = str(exc)
    for name, e in reg["indexes"].items():
        d = ROOT / name
        lr = d / "state" / "last_run.json"
        print("== %s (group %s)" % (name, e["group"]))
        if (d / "state" / "build-pending").exists():
            print("   build pending: the last engine build did not finish; the next refresh rebuilds")
        is_plain = e.get("kind") == "plain"
        if not lr.exists():
            print("   never run")
            if not is_plain and plan is not None:
                ar = plan.counts_for(name)
                print("   agent runs: %d by launch, %d by folder, %d by catch-all"
                      % (ar[routing.REASON_LAUNCH], ar[routing.REASON_FOLDER], ar[routing.REASON_CATCH_ALL]))
            continue
        j = json.loads(lr.read_text())
        if not j.get("ok"):
            print("   last run FAILED at %s: %s" % (j.get("finished"), j.get("error")))
            continue
        if j.get("kind") == "plain":
            print("   plain index, last check %s, %s" % (
                j["finished"], "rebuilt (%s)" % ", ".join(j["changed"]) if j["built"] else "no change"))
            continue
        fin = datetime.fromisoformat(j["finished"])
        age = (datetime.now(fin.tzinfo) - fin).total_seconds() / 60
        t = j["tags"]
        print("   last run %s (%.0f min ago), %ss, build %s"
              % (j["finished"], age, j["wall_s"], "ran %ss" % j["build_s"] if j["built"] else "skipped"))
        print("   %d exchanges (%d new last run), docs: %d changed / %d removed / %d unchanged"
              % (j["exchanges"], j["new_exchanges"], j["docs_changed"], j["docs_removed"], j["docs_unchanged"]))
        if isinstance(j.get("exchanges_by_provider"), dict):
            print("   exchanges by provider: " + format_counts(j["exchanges_by_provider"]))
        ar = j.get("agent_runs") if isinstance(j.get("agent_runs"), dict) else (
            plan.counts_for(name) if plan is not None else None)
        if ar is not None:
            print("   agent runs: %d by launch, %d by folder, %d by catch-all"
                  % (ar.get(routing.REASON_LAUNCH, 0), ar.get(routing.REASON_FOLDER, 0),
                     ar.get(routing.REASON_CATCH_ALL, 0)))
        print("   tags last run: %d tagged, %d failed, %d deferred; %d cached"
              % (t["tagged"], t["failed"], t["deferred"], j["tags_cached"]))
        up = d / "state" / "usage.jsonl"
        if up.exists():
            tot, calls, cost = {}, 0, 0.0
            for ln in session_parse.jsonl_lines(up.read_text()):
                u = json.loads(ln)
                calls += u.get("calls", 0)
                cost += u.get("total_cost_usd", 0)
                for m, v in (u.get("modelUsage") or {}).items():
                    a = tot.setdefault(m, dict(input=0, cache_read=0, cache_creation=0, output=0))
                    a["input"] += v.get("inputTokens", 0)
                    a["cache_read"] += v.get("cacheReadInputTokens", 0)
                    a["cache_creation"] += v.get("cacheCreationInputTokens", 0)
                    a["output"] += v.get("outputTokens", 0)
            print("   usage to date: %d calls, $%.2f, %s" % (calls, cost, json.dumps(tot)))
        td = e.get("transcripts")
        if td:
            newest = transcript_source(td).newest_mtime(project_dirs(td))
            lag = newest - fin.timestamp()
            print("   staleness: transcript %s the last run by %.0f min"
                  % ("is newer than" if lag > 0 else "is older than", abs(lag) / 60))
    if routing_err:
        if full_reg is not None and set(reg["indexes"]) != set(full_reg["indexes"]):
            print("agent routing unavailable")
        else:
            print("agent routing error: %s" % routing_err)
    elif plan is not None:
        print("unplaced agent runs: %d" % len(plan.unplaced))


def cmd_triage(reg, name):
    ix = Index(name, reg["indexes"][name], reg["defaults"])
    tri = {}
    _, _, st = ix.transcripts(triage=tri)
    texts = []
    for spec in ix.docs_specs():
        ctx = doc_corpus.Ctx(ix.doc_options(), ix.state)
        for p, _label, _rel in doc_corpus.discover(spec, ctx.opts):
            try:
                texts.append([doc_corpus.to_markdown(p, ctx)])
            except Exception:
                pass
    if texts:
        ix.scanner.scan_groups(texts, tri)
    print("%d exchanges scanned over %d sessions; %d docs; name candidates not allowlisted "
          "(count of exchanges or documents):" % (st["new"], st["sessions"], len(texts)))
    for n, c in sorted(tri.items(), key=lambda kv: (-kv[1], kv[0])):
        print("  %5d  %s" % (c, n))


def guard(reg, name):
    """Exit 3 unless the calling session may read index `name`. triage prints raw name
    candidates, so it runs the trust guard even when this module is run as a script rather
    than through `fleetwatch index`."""
    from fleetwatch import indexes
    why = indexes.read_refusal(dict(reg, groups=reg.get("groups") or {}),
                               indexes.classify_caller(), name, SETTINGS.protected_groups)
    if why:
        print(why, file=sys.stderr)
        sys.exit(3)


def owner_refusal(reg, name):
    """Why the calling session may not change index `name` destructively, or None. Reading
    is not enough: only the owner at an interactive terminal or the session that
    owns the index may."""
    from fleetwatch import indexes
    caller = indexes.classify_caller()
    if caller.kind == "owner" or (caller.kind == "session" and
                                   name in indexes.owned_by(reg, *caller.session)):
        return None
    if caller.kind in ("untrusted", "delegated"):
        return ("refused: index %r cannot be changed because the caller was not identified "
                "as a Claude session or the owner (%s)" % (name, caller.sign))
    return ("refused: index %r is not owned by this session; only its owner session, or the "
            "owner at a terminal, may remove sessions from it" % name)


def owner_guard(reg, name):
    """Exit 3 unless owner_refusal allows the calling session to change index `name`."""
    why = owner_refusal(reg, name)
    if why:
        print(why, file=sys.stderr)
        sys.exit(3)


SESSION_ID_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z-]*")


def forget_session(reg, name, sid, force=False):
    """Remove session `sid` from index `name` (Index.forget). Refuses while the session's
    transcript is still in a registered folder, since the next refresh would parse it back,
    unless `force`."""
    entry = reg["indexes"][name]
    t = entry.get("transcripts")
    if not t and name != SETTINGS.catch_all_index:
        sys.exit("[%s] has no transcripts, so there is no session to forget" % name)
    if not SESSION_ID_RE.fullmatch(sid):
        sys.exit("not a session id: %r" % sid)
    present = transcript_source(t).present(project_dirs(t), sid) if t else False
    if not present:
        plan = resolve_routing(reg)
        present = any(sid in ids for ids in plan.all_present.values())
    if present and not force:
        sys.exit("[%s] the transcript of session %s is still in a registered folder, so the next "
                 "refresh would index it again; remove it first, or pass --force" % (name, sid))
    ix = Index(name, entry, reg["defaults"])
    with Lock(ix.state) as lock:
        if lock is None:
            sys.exit("[%s] refresh already running; try again shortly" % name)
        res = ix.forget(sid)
    res["transcript_still_present"] = bool(present)
    return res


PRIVATE_UMASK = 0o077


def private_dirs(*dirs):
    """Index data (scrubbed corpora, the search index, state) is for its owner alone: each
    folder is created, or tightened, to 0700. A folder owned by someone else is left as it is."""
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass


def main(argv=None):
    """CLI entry point. Library failures that today's commands report as a message and exit
    1 (an unknown index name, a path no docs entry selects) are caught here. Everything it
    writes, and every child it starts, gets owner-only modes (PRIVATE_UMASK)."""
    os.umask(PRIVATE_UMASK)
    arm_memory_cap()
    if SETTINGS_ERROR is not None:
        sys.exit("settings: %s" % SETTINGS_ERROR)
    try:
        return _main(sys.argv[1:] if argv is None else list(argv))
    except (UnknownIndex, doc_corpus.NotSelected, routing.RoutingError) as e:
        sys.exit(str(e))


def _main(argv):
    reg = load_registry()
    if argv and argv[0] == "discover":
        return cmd_discover(reg)
    if argv and argv[0] == "status":
        from fleetwatch import indexes
        caller = indexes.classify_caller()
        visible = {name: entry for name, entry in reg["indexes"].items()
                   if indexes.read_refusal(reg, caller, name, SETTINGS.protected_groups) is None}
        return cmd_status(dict(reg, indexes=visible), full_reg=reg)
    if argv and argv[0] == "triage":
        if len(argv) < 2:
            sys.exit("usage: refresh_all.py triage NAME")
        selected(reg, argv[1])
        guard(reg, argv[1])
        return cmd_triage(reg, argv[1])
    if argv and argv[0] == "forget":
        rest = [a for a in argv[1:] if a != "--force"]
        if len(rest) != 2:
            sys.exit("usage: refresh_all.py forget NAME SESSION_ID [--force]")
        selected(reg, rest[0])
        owner_guard(reg, rest[0])
        print(json.dumps(forget_session(reg, rest[0], rest[1], "--force" in argv), indent=1))
        return
    if argv and argv[0] == "retag":
        if len(argv) < 2:
            sys.exit("usage: refresh_all.py retag NAME [--all]")
        name = argv[1]
        selected(reg, name)
        o = argparse.Namespace(index=name, full=False, no_tag=False, build=True,
                               tag_budget=10 ** 9, retag_all="--all" in argv[2:])
        sys.exit(0 if run_one(reg, name, o) else 1)
    if argv and argv[0] == "index-now":
        if len(argv) < 2:
            sys.exit("usage: refresh_all.py index-now NAME [PATH]")
        name = argv[1]
        selected(reg, name)
        if len(argv) == 2:
            argv = ["--index", name]
        elif reg["indexes"][name].get("kind") == "plain":
            # A plain index has no per-document conversion and no privacy block; its
            # refresh commands rebuild the whole corpus, so PATH only names what changed.
            o = argparse.Namespace(full=False, build=True)
            res = run_plain(name, reg["indexes"][name], o)
            if res == SKIPPED:
                sys.exit("[%s] refresh already running; try again shortly" % name)
            sys.exit(0 if res else 1)
        else:
            ix = Index(name, reg["indexes"][name], reg["defaults"])
            with Lock(ix.state) as lock:
                if lock is None:
                    sys.exit("[%s] refresh already running; try again shortly" % name)
                print(json.dumps(ix.index_now(argv[2], None), indent=1))
            return
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--no-tag", action="store_true")
    ap.add_argument("--tag-budget", type=int)
    ap.add_argument("--build", action="store_true")
    opts = ap.parse_args(argv)
    opts.retag_all = False
    names = selected(reg, opts.index)
    from fleetwatch import indexes
    caller = indexes.classify_caller()
    if indexes.is_agent_caller(caller) and any(fleetwatch_settings.is_protected(reg["indexes"][name].get("group"),
                                                        SETTINGS.protected_groups) for name in names):
        sys.exit(3)
    if opts.index:
        opts.routing_plan = resolve_routing(reg)
        return _run_all(reg, names, opts)
    with run_lock() as held:
        if not held:
            print("a refresh run is already in progress; nothing started")
            sys.exit(0)
        # Routing can write sticky session placements, so it runs under the run lock.
        opts.routing_plan = resolve_routing(reg)
        _run_all(reg, names, opts)


@contextlib.contextmanager
def run_lock():
    """One whole-registry run at a time: an flock on logs/refresh-run.lock, held for the run's
    lifetime. The kernel drops it when the process exits, however it exits (SIGTERM, the memory
    cap's os._exit, SIGKILL), so it needs no staleness rule and a run of any length keeps it.
    open() makes the descriptor non-inheritable, so a child build never holds it past the run."""
    path = ROOT / "logs" / "refresh-run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def _run_all(reg, names, opts):
    print("=== run started %s (%d indexes)" % (now_iso(), len(names)), flush=True)
    failed, skipped = [], []
    t0, done = time.time(), []
    mark = not opts.index
    if mark:
        write_run_marker(t0, None, done)
    for name in names:
        res = run_one(reg, name, opts)
        if res == SKIPPED:
            skipped.append(name)
        elif res:
            record_streak(name, None)
            if refreshed_since(name, t0):
                done.append(name)
        else:
            failed.append(name)
            record_streak(name, last_error(name))
        if mark:
            write_run_marker(t0, None, done)
    if mark:
        write_run_marker(t0, time.time(), done)
    print("=== run finished %s: %d ok, %d skipped, %d failed%s" % (
        now_iso(), len(names) - len(failed) - len(skipped), len(skipped), len(failed),
        " (%s)" % ", ".join(failed) if failed else ""), flush=True)
    sys.exit(0 if not failed else 1)


def refreshed_since(name, t0):
    """Whether the index's last run succeeded at or after t0 (a refresh skipped for a held lock did not)."""
    try:
        info = json.loads((ROOT / name / "state" / "last_run.json").read_text())
        return bool(info.get("ok")) and datetime.fromisoformat(str(info["finished"])).timestamp() >= int(t0)
    except (OSError, ValueError, KeyError, AttributeError):
        return False


def write_run_marker(started, ended, finished):
    """logs/refresh_run.json: when the last `refresh --all` run started and ended (None while it runs)
    and the index names it finished. Times and names only. The board reads it to tell a long run
    in progress from a stopped schedule. Written atomically, so a reader never sees half a file."""
    path = ROOT / "logs" / "refresh_run.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(dict(started=started, ended=ended, finished=list(finished)), indent=1))
        os.replace(tmp, path)
    except OSError:
        pass


# Failures are appended to logs/failures.log, one line each with the index's
# consecutive-failure count, so a broken index is visible without reading every
# run's output (FAILURES_LOG, set by configure). No notification: the owner checks the log.


def last_error(name):
    try:
        info = json.loads((ROOT / name / "state" / "last_run.json").read_text())
        return info.get("error") or "failed"
    except Exception:
        return "failed (no last_run.json)"


def record_streak(name, error):
    """logs/streaks/NAME holds "RUNS SINCE": the failed runs in a row and the epoch second the streak began.
    fleetwatch.index_health.read_streak reads it (a file holding RUNS alone has no start)."""
    streak_file = ROOT / "logs" / "streaks" / name
    try:
        parts = streak_file.read_text().split() if streak_file.exists() else []
        streak = int(parts[0]) if parts else 0
        since = int(float(parts[1])) if len(parts) > 1 else None
    except ValueError:
        streak, since = 0, None
    if error is None:
        if streak:
            with open(FAILURES_LOG, "a") as f:
                f.write("%s %s recovered after %d failed run(s)\n" % (now_iso(), name, streak))
            streak_file.unlink()
        return
    if not streak:
        since = int(time.time())
    streak += 1
    streak_file.parent.mkdir(parents=True, exist_ok=True)
    streak_file.write_text(str(streak) if since is None else "%d %d" % (streak, since))
    with open(FAILURES_LOG, "a") as f:
        f.write("%s %s FAILED (%d in a row): %s\n" % (now_iso(), name, streak, error))


if __name__ == "__main__":
    if migrate.migrate_defaults():
        load_settings()     # the settings folder may have just moved under the new name
    main()
