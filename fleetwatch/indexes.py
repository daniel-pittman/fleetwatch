"""Index layout, the calling session's own index, search configs generated from registry
entries, and the trust guard.

Layout: a session index has a config folder (`<config root>/<name>/`: tags.yaml,
probes.yaml, synonyms.yaml, the allowlist, search_overrides.yaml) and a data folder
(`<index root>/<name>/`: corpus/, index/, state/, scan-report.md). The two are one folder in
a one-root layout (settings.toml [paths] index_root alone), which is how a search.py-per-index
tree looks. A plain index (`kind: plain`) is always one folder, `<index root>/<name>/`: its
own scripts write the corpus beside themselves, so its scripts, search_config.yaml, probes and
data stay together (refresh.plain_dir).

The trust guard is a guard against mistakes, not a security boundary: the files are readable
by anyone who can read the disk.
"""
import copy
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import data, write_status
from .settings import is_protected

DATA_ITEMS = ("corpus", "index", "state", "scan-report.md", "logs")
OVERRIDES = "search_overrides.yaml"
OWNER_KINDS_FIXED = ("heartbeat", "subagent", "peer-session", "other")


class IndexesError(Exception):
    """A user-facing failure: the message says what to do."""


def refresh():
    """The refresh module, imported on first use; its settings are the indexer's."""
    from .memory import refresh as mod
    if mod.SETTINGS_ERROR is not None:
        raise IndexesError("settings: %s" % mod.SETTINGS_ERROR)
    return mod


def load_registry():
    r = refresh()
    reg = r.load_registry()
    reg.setdefault("groups", {})
    return reg


# ------------------------------------------------------------------ the calling session

def calling_session(paths=None, ppids=None, start_pid=None):
    """(name, sessionId) of the Claude session this process runs inside, or None in a plain
    terminal. Found from process ancestry exactly as `write-status` finds it."""
    paths = paths or data.default_paths()
    ppids = ppids if ppids is not None else data.Ancestry(paths.get("ps"))
    try:
        return write_status.find_session(paths["sessions"], ppids,
                                         os.getpid() if start_pid is None else start_pid)
    except write_status.StatusError as e:
        if getattr(ppids, "unreadable", False):
            raise IndexesError(str(e))
        return None


def listed_session_ids(entry):
    """The session ids an entry names explicitly in `transcripts.sessions`. Ownership is never
    inferred from transcript files on disk: anyone who can write a file into a project folder
    could otherwise make themselves an owner."""
    sessions = (entry.get("transcripts") or {}).get("sessions")
    return {str(s) for s in sessions} if isinstance(sessions, list) else set()


def all_entries(reg):
    """Every registry entry by name, including those load_registry() will not act on
    (reg["invalid"]): ownership, the trust guard and duplicate checks see them all."""
    return {**(reg.get("invalid") or {}), **reg["indexes"]}


def names_clash(reg, name):
    """Registered names equal to `name` without regard to case (one folder on a
    case-insensitive file system)."""
    return sorted(n for n in all_entries(reg) if str(n).casefold() == str(name).casefold())


def owned_by(reg, name, sid):
    """Registry index names owned by a session, by the first rule that finds any:
    1. `owner_session_id` equals the session id (written by `index add`, and recorded for
       migrated indexes; stable across renames);
    2. the id is in the entry's explicit `transcripts.sessions` list;
    3. `owner_session` equals the session name, only for an entry that records neither an
       `owner_session_id` nor a `transcripts.sessions` list (session names are not unique,
       so ids an entry records are never overridden by a name; this rule breaks on a
       rename).
    Entries the registry will not act on are included (all_entries)."""
    ents = all_entries(reg)
    by_id = sorted(n for n, e in ents.items() if sid and e.get("owner_session_id") == sid)
    if by_id:
        return by_id
    listed = sorted(n for n, e in ents.items() if sid and sid in listed_session_ids(e))
    if listed:
        return listed
    return sorted(n for n, e in ents.items()
                  if name and not e.get("owner_session_id") and not listed_session_ids(e)
                  and e.get("owner_session") == name)


def own_index(reg, session):
    """The one index of the calling session. Raises IndexesError when there is none or several."""
    if session is None:
        raise IndexesError("not inside a Claude session, so there is no session index to "
                           "default to; name one with --index NAME (`fleetwatch index list`)")
    name, sid = session
    found = owned_by(reg, name, sid)
    bad = [n for n in found if n not in reg["indexes"]]
    found = [n for n in found if n in reg["indexes"]]
    if bad and not found:
        raise IndexesError(refresh().index_name_problem(bad[0]))
    if not found:
        raise IndexesError("no index is registered for the session %r; add one with "
                           "`fleetwatch index add --group G` or name one with --index NAME" % name)
    if len(found) > 1:
        # A session may also own plain document indexes; its default is its transcript index.
        sessions = [n for n in found if reg["indexes"][n].get("kind") != "plain"]
        if len(sessions) == 1:
            return sessions[0]
        raise IndexesError("session %r owns several indexes (%s); choose with --index NAME"
                           % (name, ", ".join(found)))
    return found[0]


# ------------------------------------------------------------------------ trust guard

def group_readers(reg, group):
    """The `readers` list of a registry group, or None when the group is not declared."""
    g = (reg.get("groups") or {}).get(group)
    if not isinstance(g, dict):
        return None
    return [str(x) for x in (g.get("readers") or [])]


def read_refusal(reg, session, target, protected=None):
    """Why the calling session may not read index `target`, or None when it may. A plain
    terminal (session None) is the owner at a shell and may read anything. An index in a
    protected group is readable only by its owner session and the sessions its entry names
    in extra_readers; the group's `readers` list never opens it. `protected` defaults to the
    indexer settings' protected groups."""
    if session is None:
        return None
    name, sid = session
    mine = owned_by(reg, name, sid)
    if target in mine:
        return None
    ents = all_entries(reg)
    entry = ents.get(target) or {}
    group = entry.get("group")
    readers = group_readers(reg, group)
    extra = [str(x) for x in (entry.get("extra_readers") or [])]
    if name in extra or (sid and sid in extra):
        return None
    if protected is None:
        protected = refresh().SETTINGS.protected_groups
    if is_protected(group, protected):
        return ("refused: index %r is in the protected group %r, which only its owner session "
                "and the sessions in its extra_readers may read; group readers never apply. To "
                "allow it, the owner adds your session to extra_readers in that index's registry "
                "entry." % (target, group))
    # A session's group comes from its transcript index only. Owning a plain document index
    # grants that index alone; counting its group would open every index in that group.
    caller_groups = {ents[n].get("group") for n in mine if ents[n].get("kind") != "plain"}
    if readers is not None and ("*" in readers or caller_groups & set(readers)):
        return None
    if readers is None:
        rule = "group %r is not declared under groups: in the registry, so it has no readers" % group
    else:
        rule = "group %r allows readers %s" % (group, ", ".join(readers) or "(none)")
    mine_txt = ("your own index is in group %s" % ", ".join(sorted(str(g) for g in caller_groups))
                if caller_groups else "your session has no index of its own")
    return ("refused: index %r is in group %r and %s; %s. To allow it, add your group to "
            "groups.%s.readers, or your session to extra_readers in that index's registry entry."
            % (target, group, rule, mine_txt, group))


# --------------------------------------------------------------- locating an index

@dataclass
class Located:
    name: str
    entry: dict
    cfg_dir: Path
    data_dir: Path

    @property
    def is_session(self):
        if self.is_plain:
            return False
        if self.entry.get("transcripts"):
            return True
        return self.name == getattr(refresh().SETTINGS, "catch_all_index", None)

    @property
    def is_plain(self):
        return self.entry.get("kind") == "plain"


def locate(reg, name):
    r = refresh()
    if name not in reg["indexes"]:
        raise IndexesError("no index %r in the registry (have: %s)"
                           % (name, ", ".join(sorted(reg["indexes"])) or "none"))
    entry = reg["indexes"][name]
    if entry.get("kind") == "plain":
        return Located(name, entry, r.plain_dir(name), r.ROOT / name)
    return Located(name, entry, r.CONFIG_ROOT / name, r.ROOT / name)


def session_config(name, entry, settings):
    """The engine config of a session index, generated from its registry entry: sender kinds
    from the entry's `kinds` map and the owner kind, one folder kind per docs label."""
    t = entry.get("transcripts") or {}
    kinds = dict(t.get("kinds") or {})
    owner = kinds.get("owner") or kinds.get(settings.owner_kind) or settings.owner_kind
    names = [owner] + [kinds.get(k, k) for k in OWNER_KINDS_FIXED]
    labels = [str(d["label"]) for d in entry.get("docs") or [] if d.get("label")]
    rules = [{"field": "filename", "pattern": "\\.%s\\.md$" % k, "kind": k} for k in names]
    rules += [{"field": "folder", "pattern": "^%s$" % re.escape(lb), "kind": lb} for lb in labels]
    who = entry.get("owner_session") or name
    return {
        "corpus_root": "",
        "index_path": "index/%s.sqlite" % name,
        "chunking": {"max_chars": 1500, "min_chars": 25},
        "similarity": {
            "model": "sentence-transformers/all-MiniLM-L6-v2",
            "embedding_pythons": [settings.build_python],
            "window_chars": 1200, "window_overlap": 300, "max_features": 20000, "rrf_k": 60,
            "query_prefix": "", "reranker": "BAAI/bge-reranker-base", "rerank_pool": 50,
            "expansion_weight": 0.4, "expand_by_default": False},
        "roots": [
            {"name": "session", "path": "corpus/session", "extensions": [".md"],
             "default_kind": owner},
            {"name": "docs", "path": "corpus/docs", "extensions": [".md"], "default_kind": "doc"}],
        "dedupe": {"enabled": False},
        "tag_line_prefix": "Tags:",
        "kind_rules": rules,
        "level_folder_pattern": "^(\\d{8})()$",
        "exact_heading_pattern": "\\bS[0-9a-f]{6,12}-(?:EX|AQ)-\\d{4,}\\b",
        "labels": {
            "description": "Search the %s session: transcript exchanges and reference documents."
                           % who,
            "level": "day",
            "level_help": "local day as YYYYMMDD, for example 20261008 (transcript entries)",
            "kinds": "%s (who sent the incoming message)%s"
                     % (", ".join(names), "; %s (documents)" % ", ".join(labels) if labels else "")},
        "generic_folders": ["corpus", "session", "docs"],
    }


def load_overrides(cfg_dir):
    p = Path(cfg_dir) / OVERRIDES
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        raise IndexesError("%s must be a mapping of search_config keys" % p)
    return raw


def search_config(loc, settings=None):
    """The raw engine config for an index: its own search_config.yaml when the config folder
    has one (a plain index, or an old layout), else generated from the registry entry plus
    the folder's search_overrides.yaml (a top-level key there replaces the generated key)."""
    own = loc.cfg_dir / "search_config.yaml"
    if own.exists():
        return yaml.safe_load(own.read_text())
    if not loc.is_session:
        raise IndexesError("index %r has no search_config.yaml and is not a session index, so "
                           "no search config can be generated for it" % loc.name)
    settings = settings or refresh().SETTINGS
    cfg = session_config(loc.name, loc.entry, settings)
    cfg.update(copy.deepcopy(load_overrides(loc.cfg_dir)))
    cfg["corpus_root"] = str(loc.data_dir)
    return cfg


# ------------------------------------------------------------- transcript folders

def session_folders(projects_dir, sid):
    """Every project folder under `projects_dir` holding a transcript of session `sid`
    (a session whose working directory moved has one per directory)."""
    from . import resume
    return sorted({os.path.dirname(p) for p, s, _sz, _mt in resume._transcripts(str(projects_dir))
                   if s == sid})


def tilde(path):
    home = os.path.expanduser("~")
    path = str(path)
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "session"
