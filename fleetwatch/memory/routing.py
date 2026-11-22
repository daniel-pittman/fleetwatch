"""Provider-agnostic routing of external agent runs (Codex, Antigravity, etc.) into Claude Code
session indexes.

A transcript source participates in routing by exposing:
  - `provider`: short provider label (for example "codex")
  - `run_meta(path)`: returns (working_folder, start_time) for one run file
  - optional `default_path`: default directory holding its run files

The first resolved placement persists. A later refresh moves it only when the folder rule
identifies a more protected index. Launches match their provider, prefer the longest containing
folder, and send conflicting trust groups to the protected catch-all.
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
CACHE_VERSION = 2
GIT_TIMEOUT_S = 5


class RoutingError(ValueError):
    """A routing configuration problem (for example a catch-all index outside a protected group)."""


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
    unreadable: dict = field(default_factory=dict)      # provider -> set of unreadable session ids

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
        if is_same_or_parent(pf, str(Path.home())):
            continue
        if is_same_or_parent(pf, candidate_norm):
            best = max(best, len(pf))
    if slug_names:
        for base in dict.fromkeys(x for x in (candidate_norm, candidate_abs) if x):
            p = Path(base)
            for anc in (p, *p.parents):
                anc_str = str(anc).rstrip(os.sep) or os.sep
                if is_same_or_parent(anc_str, str(Path.home())):
                    continue
                if _slugs_for(anc_str) & slug_names:
                    best = max(best, len(anc_str))
                    break
    return best


def validate_catch_all(reg, settings):
    """Return the configured catch-all index name (or None). Raises RoutingError when settings
    name an index that is missing, plain, or not in a protected group."""
    name = getattr(settings, "catch_all_index", None)
    if not name:
        return None
    active = reg.get("indexes") or {}
    if name not in active:
        raise RoutingError("routing.catch_all index %r is not in the registry" % name)
    entry = active[name]
    if entry.get("kind") == "plain":
        raise RoutingError("routing.catch_all index %r cannot be a plain index" % name)
    t = entry.get("transcripts")
    if isinstance(t, dict) and t.get("source", "claude") != "claude":
        raise RoutingError("routing.catch_all index %r cannot be a provider-specific index" % name)
    group = entry.get("group")
    if not fleetwatch_settings.is_protected(group, settings.protected_groups):
        raise RoutingError(
            "routing.catch_all index %r is in group %r, which is not a protected group "
            "(protected: %s)" % (name, group, ", ".join(sorted(settings.protected_groups)) or "none"))
    return name


def resolve_run(run, reg, ledger, index_folders, catch_all=None, git_lookup=None,
                protected_groups=None):
    """Resolve a run while preserving the folder owner's protected group."""
    protected_groups = (fleetwatch_settings.DEFAULT_PROTECTED_GROUPS if protected_groups is None
                        else protected_groups)
    folder_target = None
    # The folder owner is needed before launch selection to enforce the protection floor.
    if run.folder:
        norm_run = norm_folder(run.folder)
        abs_run = _abs_folder(run.folder)
        candidates = [(norm_run, abs_run)]
        main = git_lookup(run.folder) if git_lookup is not None else git_main_checkout(run.folder)
        if main and main != norm_run:
            candidates.append((main, main))
        hits = []
        for idx_name, (real_folders, slug_names) in index_folders.items():
            best = max(_folder_match_len(cn, ca, real_folders, slug_names)
                       for cn, ca in candidates)
            if best >= 0:
                hits.append((best, idx_name))
        if hits:
            top = max(ml for ml, _ in hits)
            winners = sorted({ix for ml, ix in hits if ml == top})
            if len(winners) == 1:
                folder_target = winners[0]
    if folder_target and fleetwatch_settings.is_protected(
            reg["indexes"][folder_target].get("group"), protected_groups):
        return Placement(run, folder_target, REASON_FOLDER)

    if run.folder and run.start_time is not None:
        matches = []
        for e in ledger:
            tool = str(e.get("tool") or "")
            tool_matches = (tool == "multi-review" and run.provider in ("codex", "antigravity")
                            or run.provider == "codex" and tool.startswith("codex")
                            or run.provider == "antigravity" and tool.startswith(("agy", "antigravity")))
            if not tool_matches:
                continue
            cwd = e.get("cwd")
            if not cwd or is_same_or_parent(cwd, str(Path.home())):
                continue
            session_cwd = e.get("session_cwd")
            if session_cwd and norm_folder(cwd) != norm_folder(session_cwd) and \
                    is_same_or_parent(cwd, session_cwd):
                continue
            if (e.get("ts_epoch") is None or
                    not 0 <= run.start_time - e["ts_epoch"] <= LAUNCH_WINDOW_S or
                    not is_same_or_parent(cwd, run.folder)):
                continue
            target = launcher_index(reg, e.get("session_name"), e.get("session_id"))
            if target is not None:
                own_folders = index_folders.get(target, ([], set()))[0]
                if any(norm_folder(cwd) != norm_folder(own) and
                       is_same_or_parent(cwd, own) for own in own_folders):
                    continue
                matches.append((e, target))
        if matches:
            groups = {reg["indexes"][target].get("group") for _e, target in matches}
            if len(groups) > 1:
                return Placement(run, catch_all, REASON_CATCH_ALL if catch_all else REASON_UNPLACED)
            chosen, target = max(matches, key=lambda pair: (len(norm_folder(pair[0]["cwd"])),
                                                        pair[0]["ts_epoch"], pair[0].get("line_no", 0)))
            return Placement(run, target, REASON_LAUNCH)
    if folder_target:
        return Placement(run, folder_target, REASON_FOLDER)

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
    _save_private_json(Path(path), {"version": CACHE_VERSION, "files": files})


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


def _file_sig(path):
    st = path.stat()
    return [st.st_size, st.st_mtime_ns]


def discover_runs(sources, settings, cache_path=None, claimed_fn=None):
    """Discover all agent runs across provider `sources` ({provider_name: source_cls}).

    Returns (runs, all_present_by_provider, unreadable_by_provider). Metadata is cached by file signature when
    `cache_path` is given. Sources with sidecar files can include them via file_sig(path).
    """
    old_cache = _load_meta_cache(cache_path)
    new_cache = {}
    runs = []
    all_present = {}
    unreadable = {}
    for provider, src_cls in sorted(sources.items()):
        dirs = [d for d in settings.source_dirs(provider, getattr(src_cls, "default_path", None))
                if d.is_dir()]
        if not dirs:
            all_present[provider] = set()
            unreadable[provider] = set()
            continue
        src = src_cls()
        file_sig = getattr(src, "file_sig", _file_sig)
        keep, _skipped = src.list(dirs, None, (), "all", False)
        present = set()
        failed = set()
        for _sort_key, sid, path in keep:
            present.add(sid)
            if claimed_fn is not None and claimed_fn(provider, sid, path):
                continue
            key = str(path)
            try:
                sig = file_sig(path)
            except OSError:
                failed.add(sid)
                continue
            cached = old_cache.get(key)
            if isinstance(cached, dict) and cached.get("sig") == sig:
                folder = cached.get("folder")
                start_epoch = cached.get("start_time")
            else:
                try:
                    folder, raw_ts = src.run_meta(path)
                except Exception:
                    failed.add(sid)
                    continue
                start_epoch = _to_epoch(raw_ts) if raw_ts is not None else None
            if folder is None or start_epoch is None:
                failed.add(sid)
                continue
            new_cache[key] = {"sig": sig, "folder": folder, "start_time": start_epoch}
            runs.append(AgentRun(provider=provider, session_id=sid, path=path,
                                 folder=folder, start_time=start_epoch))
        all_present[provider] = present
        unreadable[provider] = failed
    if cache_path and new_cache != old_cache:
        _save_meta_cache(cache_path, new_cache)
    return runs, all_present, unreadable


def resolve_all(reg, settings, sources, ledger=None, ledger_path=None, git_cmd=None,
                cache_path=None, project_dirs_fn=None, runs=None, all_present=None,
                placement_path=None):
    """Discover and resolve all agent runs against `reg` and return a RoutingPlan."""
    catch_all = validate_catch_all(reg, settings)
    if ledger is None:
        ledger = launch.read_ledger(ledger_path)
    unreadable = {}
    if runs is None:
        claimed_fn = _claimed_by_standalone(reg, sources)
        runs, all_present, unreadable = discover_runs(sources, settings, cache_path=cache_path,
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

    plan = RoutingPlan(all_present=all_present, unreadable=unreadable)
    placements_file = Path(placement_path) if placement_path else None
    try:
        stored = json.loads(placements_file.read_text()) if placements_file else {}
    except (OSError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    updated = dict(stored)
    for run in runs:
        if run.folder is None or run.start_time is None:
            continue
        placement = resolve_run(run, reg, ledger, index_folders, catch_all=catch_all,
                                git_lookup=git_lookup, protected_groups=settings.protected_groups)
        key = "%s:%s" % (run.provider, run.session_id)
        old = stored.get(key)
        if isinstance(old, dict) and old.get("index") in reg["indexes"]:
            old_group = reg["indexes"][old["index"]].get("group")
            new_group = (reg["indexes"][placement.index].get("group") if placement.index else None)
            if not (placement.reason == REASON_FOLDER and
                    fleetwatch_settings.is_protected(new_group, settings.protected_groups)
                    and not fleetwatch_settings.is_protected(old_group, settings.protected_groups)):
                placement = Placement(run, old["index"], old.get("reason", REASON_FOLDER))
        if placement.index:
            updated[key] = {"index": placement.index, "reason": placement.reason}
        plan.placements.append(placement)
        if placement.index is None:
            plan.unplaced.append(placement)
        else:
            plan.by_index.setdefault(placement.index, []).append(placement)
            c = plan.counts.setdefault(placement.index, {
                REASON_LAUNCH: 0, REASON_FOLDER: 0, REASON_CATCH_ALL: 0,
            })
            c[placement.reason] = c.get(placement.reason, 0) + 1
    if placements_file and updated != stored:
        _save_placements(placements_file, updated)
    return plan


def _save_placements(path, placements):
    _save_private_json(path, placements)


def _save_private_json(path, value):
    try:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(path.parent, 0o700)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out)
        os.replace(tmp, path)
    except OSError:
        pass
