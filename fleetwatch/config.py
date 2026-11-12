"""fleetwatch's own files under ~/.config/fleetwatch: groups.toml (what the user edits) and state.json (what the app remembers)."""
import json
import os
import re
import tempfile
import tomllib

from .settings import config_dir
from .data import clean, parse_window

OTHER = "Other"
SORT_TABS = ("0", "1", "2", "3", "4")
STATE_VERSION = 2
TODO_GROUP = "time"                         # the To-dos tab groups by when it is due until told otherwise
TODO_GROUPS = ("time", "folder", "none")
# Tab index in a version-1 state file (five tabs) to its index now; the old to-dos tab (2) is gone.
_OLD_TAB = {"0": "0", "1": "1", "3": "2", "4": "3"}




def groups_path():
    return os.path.join(config_dir(), "groups.toml")


def state_path():
    return os.path.join(config_dir(), "state.json")


def prune_path():
    return os.path.join(config_dir(), "prune.toml")


def parked_path():
    return os.path.join(config_dir(), "parked.json")


def resume_cache_path():
    return os.path.join(config_dir(), "resume-cache.json")


def atomic_write(path, text):
    """Write text to path through a temp file in the same folder, so a reader never sees half a file."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".fleetwatch-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, (os.stat(path).st_mode & 0o777) if os.path.exists(path) else 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- groups.toml

DEFAULT_GROUPS_TOML = '''# fleetwatch groups. A session belongs to the group with the longest path below that its
# working folder (cwd) sits inside. [overrides] pins one session, by name, to a group
# and beats the paths. A session that matches nothing is shown under "Other".
# Press m on a session in fleetwatch to write an override here. These groups are examples: edit them.

[[group]]
name = "Work"
paths = ["~/code/work"]

[[group]]
name = "Personal"
paths = ["~/code/personal", "~/Documents"]

[overrides]
'''


def _norm(path):
    p = os.path.expanduser(str(path)).rstrip("/")
    return p or "/"


class Groups:
    """Path rules plus per-session overrides. Matching is by whole path components."""

    def __init__(self, rules=(), overrides=None, override_ids=None):
        self.rules = [(name, [_norm(p) for p in paths]) for name, paths in rules]
        self.overrides = dict(overrides or {})
        self.override_ids = dict(override_ids or {})

    def of(self, name, cwd, session_id=None):
        """Group name for a session: its override by sessionId, then by name, else the longest
        matching path, else Other. The sessionId entry is what survives a rename."""
        if session_id in self.override_ids:
            return self.override_ids[session_id]
        return self.overrides[name] if name in self.overrides else self.by_path(cwd)

    def by_path(self, cwd):
        """The group the paths alone give a working folder."""
        best, best_len = OTHER, -1
        if isinstance(cwd, str) and cwd:
            here = _norm(cwd)
            for group, paths in self.rules:
                for p in paths:
                    if (here == p or here.startswith(p.rstrip("/") + "/")) and len(p) > best_len:
                        best, best_len = group, len(p)
        return best

    def names(self):
        """Every known group in display order: the rules, then override-only groups, then Other."""
        out = []
        for g in [n for n, _ in self.rules] + list(self.overrides.values()) + list(self.override_ids.values()):
            if g not in out and g != OTHER:
                out.append(g)
        return out + [OTHER]


def parse_groups(text):
    """Groups from groups.toml text; ValueError (or TOMLDecodeError) when it is malformed."""
    doc = tomllib.loads(text)
    rules = []
    raw = doc.get("group", [])
    if not isinstance(raw, list):
        raise ValueError("group must be a list of [[group]] tables")
    for g in raw:
        if not isinstance(g, dict) or not isinstance(g.get("name"), str) or not g["name"].strip():
            raise ValueError("every [[group]] needs a name")
        paths = g.get("paths", [])
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise ValueError("paths of %s must be a list of strings" % clean(g["name"]))
        rules.append((clean(g["name"]).strip(), paths))
    ov = doc.get("overrides", {})
    if not isinstance(ov, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in ov.items()):
        raise ValueError("[overrides] must map session names to group names")
    ids = doc.get("override_ids", {})
    if not isinstance(ids, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in ids.items()):
        raise ValueError("[override_ids] must map session IDs to group names")
    return Groups(rules, {clean(k): clean(v).strip() for k, v in ov.items() if clean(v).strip()},
                  {clean(k): clean(v).strip() for k, v in ids.items() if clean(v).strip()})


def load_groups(path=None, create=True):
    """(Groups, warning or None). Creates the default file when absent. Never raises:
    a bad file yields an empty Groups, so every session shows under Other, plus a warning."""
    path = path or groups_path()
    if not os.path.exists(path):
        if create:
            try:
                atomic_write(path, DEFAULT_GROUPS_TOML)
            except OSError as exc:
                return parse_groups(DEFAULT_GROUPS_TOML), "could not create groups.toml: %s" % clean(exc)[:60]
        return parse_groups(DEFAULT_GROUPS_TOML), None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return parse_groups(fh.read()), None
    except Exception as exc:
        return Groups(), "groups.toml is not valid (%s); every session shows under %s" % (
            clean(exc)[:80] or type(exc).__name__, OTHER)


_ANY_HEADER = re.compile(r"^\s*\[")


def _header_re(table):
    return re.compile(r"^\s*\[%s\]\s*(#.*)?$" % re.escape(table))


def _toml_string(value):
    return json.dumps(value, ensure_ascii=False)


def _key_of(line):
    """The key a one-line `"name" = "group"` entry defines, or None."""
    try:
        doc = tomllib.loads(line)
    except tomllib.TOMLDecodeError:
        return None
    return next(iter(doc), None) if len(doc) == 1 else None


def _edit_table(text, table, key, group):
    """text with `key = group` set in [table] (appended, or replaced in place); group None removes it."""
    entry = "%s = %s\n" % (_toml_string(key), _toml_string(group))
    lines = text.splitlines(keepends=True)
    head = next((i for i, ln in enumerate(lines) if _header_re(table).match(ln)), None)
    if group is None:
        if head is not None:
            end = next((i for i in range(head + 1, len(lines)) if _ANY_HEADER.match(lines[i])), len(lines))
            lines = [ln for i, ln in enumerate(lines) if not (head < i < end and _key_of(ln) == key)]
        return "".join(lines)
    if head is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += ["\n", "[%s]\n" % table, entry]
    else:
        end = next((i for i in range(head + 1, len(lines)) if _ANY_HEADER.match(lines[i])), len(lines))
        at = next((i for i in range(head + 1, end) if _key_of(lines[i]) == key), None)
        if at is not None:
            lines[at] = entry
        else:
            at = end
            while at > head + 1 and not lines[at - 1].strip():
                at -= 1
            if at > 0 and not lines[at - 1].endswith("\n"):
                lines[at - 1] += "\n"
            lines.insert(at, entry)
    return "".join(lines)


def set_override(path, name, group, natural=None, session_id=None):
    """Pin session `name` to `group` in the [overrides] table by a targeted edit.

    With `session_id`, the same pin is written under [override_ids], which is matched first so
    the override follows the session through a rename. Comments, order and the rest of the file
    stay as they are. When `group` is what the paths already give (`natural`), the entries are
    removed instead, so a move back leaves no residue. Raises ValueError when the file or the
    result would not parse, leaving the file untouched.
    """
    name, group = clean(name).strip(), clean(group).strip()
    if not name or not group:
        raise ValueError("a session name and a group name are both needed")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    parse_groups(text)
    remove = natural is not None and group == natural
    pins = [("overrides", name)] + ([("override_ids", clean(session_id).strip())] if session_id else [])
    new = text
    for table, key in pins:
        new = _edit_table(new, table, key, None if remove else group)
    check = parse_groups(new)
    for table, key in pins:
        got = getattr(check, table).get(key)
        if got != (None if remove else group):
            raise ValueError("the override could not be %s" % ("removed" if remove else "saved in [%s]" % table))
    if new != text:
        atomic_write(path, new)


# ---------------------------------------------------------------- state.json

def default_state():
    return {"version": STATE_VERSION, "sort": {}, "groups": False, "folds": {}, "todo_group": TODO_GROUP}


def _migrate(sort, folds):
    """Re-key a version-1 state (tabs Needs, Coming up, To-dos, Sessions, Resume) onto the four tabs now."""
    new_sort = {_OLD_TAB[k]: v for k, v in sort.items() if k in _OLD_TAB}
    new_folds = {}
    for k, v in folds.items():
        parts = k.split(":", 2)
        if len(parts) == 3 and parts[0] == "grp" and parts[1] in _OLD_TAB:
            new_folds["grp:%s:%s" % (_OLD_TAB[parts[1]], parts[2])] = v
        elif not k.startswith("grp:"):
            new_folds[k] = v
    return new_sort, new_folds


def load_state(path=None):
    """(state, warning or None). A missing file is the defaults; a corrupt one is the defaults plus a warning."""
    path = path or state_path()
    if not os.path.exists(path):
        return default_state(), None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        sort = raw.get("sort", {})
        folds = raw.get("folds", {})
        groups = raw.get("groups", False)
        if not isinstance(sort, dict) or not isinstance(folds, dict) or not isinstance(groups, bool):
            raise ValueError("wrong shape")
        sort = {k: v for k, v in sort.items() if isinstance(v, str)}
        folds = {k: v for k, v in folds.items() if isinstance(k, str) and isinstance(v, bool)}
        if raw.get("version") != STATE_VERSION:
            sort, folds = _migrate(sort, folds)
        return {"version": STATE_VERSION, "sort": {k: v for k, v in sort.items() if k in SORT_TABS},
                "groups": groups, "folds": folds,
                "todo_group": raw.get("todo_group") if raw.get("todo_group") in TODO_GROUPS else TODO_GROUP}, None
    except Exception as exc:
        return default_state(), "state.json is not valid (%s); using the defaults" % (
            clean(exc)[:60] or type(exc).__name__)


def save_state(state, path=None):
    """True on success. A failed write must never take the app down."""
    try:
        atomic_write(path or state_path(), json.dumps(state, indent=2, sort_keys=True) + "\n")
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- prune.toml

# Sessions whose status files are never pruned; empty until prune.toml sets `keep`.
DEFAULT_KEEP = ()
DEFAULT_QUIET_S = 24 * 3600


def default_prune():
    return {"keep": list(DEFAULT_KEEP), "quiet_s": float(DEFAULT_QUIET_S)}


def load_prune(path=None):
    """(settings, warning or None). prune.toml is optional: `keep = [names]`, `quiet = "24h"`.

    A missing file is the defaults; a bad one is the defaults plus a warning.
    """
    path = path or prune_path()
    settings = default_prune()
    if not os.path.exists(path):
        return settings, None
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        keep = raw.get("keep", settings["keep"])
        if not isinstance(keep, list) or not all(isinstance(k, str) for k in keep):
            raise ValueError("keep must be a list of session names")
        settings["keep"] = keep
        if "quiet" in raw:
            if not isinstance(raw["quiet"], str):
                raise ValueError('quiet looks like "24h"')
            settings["quiet_s"] = parse_window(raw["quiet"])
        return settings, None
    except Exception as exc:
        return default_prune(), "prune.toml is not valid (%s); using the defaults" % (
            clean(exc)[:60] or type(exc).__name__)
