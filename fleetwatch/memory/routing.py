"""Provider-agnostic routing of external agent runs (Codex, Antigravity, etc.) into Claude Code
session indexes.

A transcript source participates in routing by exposing:
  - `provider`: short provider label (for example "codex")
  - `run_meta(path)`: returns (working_folder, start_time) for one run file
  - optional `default_path`: default directory holding its run files

Resolution order for each run (provider, folder, start_time):
  (a) the newest launch-ledger entry with the same folder or a parent folder whose timestamp is
      within 10 minutes before the run's start: that launcher session's index;
  (b) else the unique registered session index whose project folders contain the run's folder,
      following a git worktree to its main checkout via `git rev-parse --git-common-dir` when
      the folder still exists;
  (c) else the catch-all index named in settings.toml [routing] catch_all, which must belong to
      a protected group; with no catch-all configured, the run is left unplaced and counted.
"""
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fleetwatch import data, indexes, launch, resume, settings as fleetwatch_settings

LAUNCH_WINDOW_S = 600
REASON_LAUNCH = "launch"
REASON_FOLDER = "folder"
REASON_CATCH_ALL = "catch_all"
REASON_UNPLACED = "unplaced"
CACHE_VERSION = 1
GIT_TIMEOUT_S = 5


@dataclass(frozen=True)
class AgentRun:
    provider: str
    session_id: str
    path: Path
    folder: str = None
    start_time: float = None        # epoch seconds


@dataclass(frozen=True)
class Placement:
    run: AgentRun
    index: str = None               # target index name, or None when unplaced
    reason: str = REASON_UNPLACED


@dataclass
class RoutingPlan:
    placements: list = field(default_factory=list)
    by_index: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    unplaced: list = field(default_factory=list)
    all_present: dict = field(default_factory=dict)     # provider -> set of discovered session ids

    def counts_for(self, index_name):
        c = self.counts.get(index_name) or {}
        return {
            REASON_LAUNCH: c.get(REASON_LAUNCH, 0),
            REASON_FOLDER: c.get(REASON_FOLDER, 0),
            REASON_CATCH_ALL: c.get(REASON_CATCH_ALL, 0),
        }

    def runs_for(self, index_name):
        """{provider: [AgentRun, ...]} resolved to `index_name`."""
        out = {}
        for p in self.by_index.get(index_name, ()):
            out.setdefault(p.run.provider, []).append(p.run)
        return out


def norm_folder(path):
    """Canonical absolute path with symlinks resolved and no trailing slash (except root)."""
    if not path or not isinstance(path, str):
        return ""
    s = path.strip()
    if not s:
        return ""
    p = os.path.realpath(os.path.abspath(os.path.expanduser(s)))
    return p.rstrip(os.sep) or os.sep


def _abs_folder(path):
    """Absolute path without resolving symlinks (for slug matching against unresolved paths)."""
    if not path or not isinstance(path, str):
        return ""
    s = path.strip()
    if not s:
        return ""
    p = os.path.abspath(os.path.expanduser(s))
    return p.rstrip(os.sep) or os.sep


def is_same_or_parent(parent, child):
    """True when `parent` equals `child` or is an ancestor directory of `child`."""
    p, c = norm_folder(parent), norm_folder(child)
    if not p or not c:
        return False
    if c == p:
        return True
    prefix = os.sep if p == os.sep else p + os.sep
    return c.startswith(prefix)


def _to_epoch(val):
    if isinstance(val, datetime):
        return val.timestamp()
    return data.to_epoch(val)


def _git_argv(git_cmd=None):
    if git_cmd is not None:
        return shlex.split(git_cmd) if isinstance(git_cmd, str) else list(git_cmd)
    env = os.environ.get("FLEETWATCH_GIT_CMD")
    if env:
        return shlex.split(env)
    return ["git"]


def git_main_checkout(folder, git_cmd=None):
    """Main checkout directory for a git repo or worktree, via `git rev-parse --git-common-dir`.

    Constraint: only runs git when `folder` exists on disk; returns None otherwise or on error.
    """
    if not folder or not isinstance(folder, str):
        return None
    expanded = os.path.expanduser(folder.strip())
    if not expanded or not os.path.isdir(expanded):
        return None
    argv = _git_argv(git_cmd) + ["-C", expanded, "rev-parse", "--git-common-dir"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    common = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(expanded, raw))
    if os.path.basename(common) == ".git":
        return norm_folder(os.path.dirname(common))
    return None


def _is_claude_session_entry(entry):
    if not isinstance(entry, dict) or entry.get("kind") == "plain":
        return False
    t = entry.get("transcripts")
    if not isinstance(t, dict):
        return False
    return t.get("source", "claude") == "claude" and bool(t.get("project_dir"))


def _is_routable_target(entry):
    """Any registered non-plain index that is not a standalone external-provider index."""
    if not isinstance(entry, dict) or entry.get("kind") == "plain":
        return False
    t = entry.get("transcripts")
    if isinstance(t, dict) and t.get("source", "claude") != "claude":
        return False
    return True


def launcher_index(reg, session_name, session_id):
    """The registered session index owned by a launching Claude session, or None."""
    active = reg.get("indexes") or {}
    found = [n for n in indexes.owned_by(reg, session_name, session_id)
             if n in active and _is_routable_target(active[n])]
    if not found:
        return None
    return found[0]


def _claude_project_dirs(t):
    pd = t.get("project_dir")
    if not pd:
        return []
    return [Path(os.path.expanduser(p)) for p in (pd if isinstance(pd, list) else [pd])]


def _cwds_in_project_dir(pdir, sessions):
    """Distinct normalized working folders recorded inside Claude transcripts under `pdir`."""
    if not pdir.is_dir():
        return []
    files = []
    if isinstance(sessions, list):
        files = [pdir / (str(s) + ".jsonl") for s in sessions if (pdir / (str(s) + ".jsonl")).is_file()]
    if not files:
        files = sorted(pdir.glob("*.jsonl"))
    cwds = []
    for f in files:
        try:
            cwd, _ep, _sc = resume.read_head(str(f))
        except OSError:
            continue
        if cwd:
            nf = norm_folder(cwd)
            if nf and nf not in cwds:
                cwds.append(nf)
    return cwds


def index_project_folders(entry, project_dirs_fn=None):
    """(real_folders, slug_names) for one Claude session index entry.

    Uses the exact `cwd` recorded inside each project folder's transcripts when available;
    falls back to the Claude project folder slug only for a project folder that holds no
    transcript with a `cwd` line.
    """
    if not _is_claude_session_entry(entry):
        return [], set()
    t = entry["transcripts"]
    dirs = (project_dirs_fn or _claude_project_dirs)(t)
    sessions = t.get("sessions", "all")
    real_folders = []
    slug_names = set()
    for d in dirs:
        found = _cwds_in_project_dir(d, sessions)
        if found:
            for nf in found:
                if nf not in real_folders:
                    real_folders.append(nf)
        else:
            slug_names.add(d.name)
    return real_folders, slug_names


def _slugs_for(path_str):
    return {re.sub(r"[^A-Za-z0-9-]", "-", path_str), path_str.replace("/", "-")}


def _folder_match_len(candidate_norm, candidate_abs, real_folders, slug_names):
    """Length of the longest project folder matching `candidate`, or -1 when none matches."""
    best = -1
    for pf in real_folders:
        if is_same_or_parent(pf, candidate_norm):
            best = max(best, len(pf))
    if slug_names:
        for base in dict.fromkeys(x for x in (candidate_norm, candidate_abs) if x):
            p = Path(base)
            for anc in (p, *p.parents):
                anc_str = str(anc).rstrip(os.sep) or os.sep
                if _slugs_for(anc_str) & slug_names:
                    best = max(best, len(anc_str))
                    break
    return best


def validate_catch_all(reg, settings):
    """Return the configured catch-all index name (or None). Raises ValueError when settings
    name an index that is missing, plain, or not in a protected group."""
    name = getattr(settings, "catch_all_index", None)
    if not name:
        return None
    active = reg.get("indexes") or {}
    if name not in active:
        raise ValueError("routing.catch_all index %r is not in the registry" % name)
    entry = active[name]
    if entry.get("kind") == "plain":
        raise ValueError("routing.catch_all index %r cannot be a plain index" % name)
    t = entry.get("transcripts")
    if isinstance(t, dict) and t.get("source", "claude") != "claude":
        raise ValueError("routing.catch_all index %r cannot be a provider-specific index" % name)
    group = entry.get("group")
    if not fleetwatch_settings.is_protected(group, settings.protected_groups):
        raise ValueError(
            "routing.catch_all index %r is in group %r, which is not a protected group "
            "(protected: %s)" % (name, group, ", ".join(sorted(settings.protected_groups)) or "none"))
    return name


def resolve_run(run, reg, ledger, index_folders, catch_all=None, git_lookup=None):
    """Resolve one AgentRun to a Placement following the (a) launch -> (b) folder -> (c) catch-all order."""
    # (a) Newest launch-ledger match with same or parent folder within 10 minutes before run start.
    if run.folder and run.start_time is not None:
        matches = [
            e for e in ledger
            if e.get("ts_epoch") is not None
            and 0 <= (run.start_time - e["ts_epoch"]) <= LAUNCH_WINDOW_S
            and is_same_or_parent(e["cwd"], run.folder)
        ]
        if matches:
            newest = max(matches, key=lambda e: (e["ts_epoch"], e.get("line_no", 0)))
            target = launcher_index(reg, newest.get("session_name"), newest.get("session_id"))
            if target is not None:
                return Placement(run, target, REASON_LAUNCH)

    # (b) Registered session index whose project folders contain the run's folder or git main checkout.
    if run.folder:
        norm_run = norm_folder(run.folder)
        abs_run = _abs_folder(run.folder)
        candidates = [(norm_run, abs_run)]
        main = git_lookup(run.folder) if git_lookup is not None else git_main_checkout(run.folder)
        if main and main != norm_run:
            candidates.append((main, main))
        hits = []
        for idx_name, (real_folders, slug_names) in index_folders.items():
            best = -1
            for cn, ca in candidates:
                best = max(best, _folder_match_len(cn, ca, real_folders, slug_names))
            if best >= 0:
                hits.append((best, idx_name))
        if hits:
            top = max(ml for ml, _ in hits)
            winners = sorted({ix for ml, ix in hits if ml == top})
            if len(winners) == 1:
                return Placement(run, winners[0], REASON_FOLDER)

    # (c) Catch-all index in a protected group, else unplaced.
    if catch_all:
        return Placement(run, catch_all, REASON_CATCH_ALL)
    return Placement(run, None, REASON_UNPLACED)


def _load_meta_cache(path):
    if not path:
        return {}
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(doc, dict) and doc.get("version") == CACHE_VERSION and isinstance(doc.get("files"), dict):
        return doc["files"]
    return {}


def _save_meta_cache(path, files):
    if not path:
        return
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"version": CACHE_VERSION, "files": files}), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def _claimed_by_standalone(reg, sources):
    """Build a predicate (provider, sid, path) -> bool for runs already owned by an explicit
    standalone provider index (such as `transcripts: {source: codex, path: ...}`), so no run
    is indexed into two indexes."""
    claims = []
    for _name, entry in (reg.get("indexes") or {}).items():
        if not isinstance(entry, dict) or entry.get("kind") == "plain":
            continue
        t = entry.get("transcripts")
        if not isinstance(t, dict):
            continue
        src = t.get("source", "claude")
        if src == "claude" or src not in sources:
            continue
        raw_path = t.get("path")
        if not raw_path:
            continue
        root = norm_folder(raw_path)
        sessions = t.get("sessions", "all")
        sess_set = {str(s) for s in sessions} if isinstance(sessions, list) else None
        claims.append((src, root, sess_set))

    def is_claimed(provider, sid, path):
        pnorm = norm_folder(str(path))
        for src, root, sess_set in claims:
            if src != provider:
                continue
            if not is_same_or_parent(root, pnorm):
                continue
            if sess_set is None or sid in sess_set:
                return True
        return False

    return is_claimed


def discover_runs(sources, settings, cache_path=None, claimed_fn=None):
    """Discover all agent runs across provider `sources` ({provider_name: source_cls}).

    Returns (runs, all_present_by_provider). Metadata is cached by (size, mtime_ns) when
    `cache_path` is given so unchanged rollouts are not reopened on every refresh.
    """
    old_cache = _load_meta_cache(cache_path)
    new_cache = {}
    runs = []
    all_present = {}
    for provider, src_cls in sorted(sources.items()):
        dirs = [d for d in settings.source_dirs(provider, getattr(src_cls, "default_path", None))
                if d.is_dir()]
        if not dirs:
            all_present[provider] = set()
            continue
        src = src_cls()
        keep, _skipped = src.list(dirs, None, (), "all", False)
        present = set()
        for _sort_key, sid, path in keep:
            present.add(sid)
            if claimed_fn is not None and claimed_fn(provider, sid, path):
                continue
            key = str(path)
            try:
                st = path.stat()
                sig = (st.st_size, st.st_mtime_ns)
            except OSError:
                continue
            cached = old_cache.get(key)
            if (isinstance(cached, dict) and cached.get("size") == sig[0]
                    and cached.get("mtime_ns") == sig[1]):
                folder = cached.get("folder")
                start_epoch = cached.get("start_time")
            else:
                folder, raw_ts = src.run_meta(path)
                start_epoch = _to_epoch(raw_ts) if raw_ts is not None else None
            new_cache[key] = {"size": sig[0], "mtime_ns": sig[1],
                              "folder": folder, "start_time": start_epoch}
            runs.append(AgentRun(provider=provider, session_id=sid, path=path,
                                 folder=folder, start_time=start_epoch))
        all_present[provider] = present
    if cache_path and new_cache != old_cache:
        _save_meta_cache(cache_path, new_cache)
    return runs, all_present


def resolve_all(reg, settings, sources, ledger=None, ledger_path=None, git_cmd=None,
                cache_path=None, project_dirs_fn=None, runs=None, all_present=None):
    """Discover and resolve all agent runs against `reg` and return a RoutingPlan."""
    catch_all = validate_catch_all(reg, settings)
    if ledger is None:
        ledger = launch.read_ledger(ledger_path)
    if runs is None:
        claimed_fn = _claimed_by_standalone(reg, sources)
        runs, all_present = discover_runs(sources, settings, cache_path=cache_path,
                                          claimed_fn=claimed_fn)
    elif all_present is None:
        all_present = {}
        for r in runs:
            all_present.setdefault(r.provider, set()).add(r.session_id)

    index_folders = {}
    for name, entry in (reg.get("indexes") or {}).items():
        if _is_claude_session_entry(entry):
            index_folders[name] = index_project_folders(entry, project_dirs_fn=project_dirs_fn)

    git_cache = {}

    def git_lookup(folder):
        if folder not in git_cache:
            git_cache[folder] = git_main_checkout(folder, git_cmd=git_cmd)
        return git_cache[folder]

    plan = RoutingPlan(all_present=all_present)
    for run in runs:
        placement = resolve_run(run, reg, ledger, index_folders, catch_all=catch_all,
                                git_lookup=git_lookup)
        plan.placements.append(placement)
        if placement.index is None:
            plan.unplaced.append(placement)
        else:
            plan.by_index.setdefault(placement.index, []).append(placement)
            c = plan.counts.setdefault(placement.index, {
                REASON_LAUNCH: 0, REASON_FOLDER: 0, REASON_CATCH_ALL: 0,
            })
            c[placement.reason] = c.get(placement.reason, 0) + 1
    return plan
