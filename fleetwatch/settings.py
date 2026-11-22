"""fleetwatch's optional settings.toml, read from fleetwatch's config folder (FLEETWATCH_CONFIG_DIR, else
~/.config/fleetwatch). Every key is optional and the defaults carry nothing about any one person.

Board keys: `timezone` (an IANA name; default is the system's local zone) and `owner_name` (how
fleetwatch names you in prose; default empty). For the board a missing file is the defaults and an
unreadable or invalid value is ignored, because the board must still draw.

The search indexer reads the same file through `indexer()`, strictly: an invalid value stops
it, because a guess could scrub less than you asked for or rename every stored exchange.

    timezone = "Europe/Lisbon"        # day folders and displayed times
    owner_name = "Sam"                # sender label in transcripts: "Sam (user)"

    [paths]
    data_dir = "~/.local/share/fleetwatch" # default $XDG_DATA_HOME/fleetwatch
    index_root = "..."                # default <data_dir>/indexes: one folder per index (corpus,
                                      # index, state), the logs, and the legacy registry location
    index_config_root = "..."         # registry.yaml, registry.d/ and each index's tags, probes,
                                      # allowlist and scripts; default <config>/indexes, or
                                      # index_root when only index_root is set (one root for both)
    projects_dir = "~/.claude/projects"
    build_python = "..."              # runs each index's search.py build; default this Python

    [transcripts]
    owner_kind = "owner"              # canonical sender kind of your own messages
    heartbeat_patterns = ["..."]      # regexes (multiline) that mark a scheduled prompt
    automated_prefixes = ["..."]      # a transcript whose first prompt starts so is skipped

    [groups]
    protected = ["ferpa"]             # groups that refuse keep_patterns and person_names: false
    guess = {"-work-" = "research"}   # project-folder fragment -> group, for `discover`

    [scanner]
    rule_packs = ["education"]        # added to the core rules; a name ending .yaml is a
                                      # pack file, relative to the config folder

    [docs]
    column_denylist = ["..."]         # replaces the composed default (core plus packs)

    [tagger]
    default_guidance_file = "x.txt"   # replaces the built-in summary guidance (or
                                      # default_guidance = "text"); relative to the config folder

    [routing]
    catch_all = "unrouted-agents"     # optional index for unrouted agent runs (must be in a
                                      # protected group); with none set, unrouted runs are skipped
    sources = { codex = "~/.codex/sessions" }  # optional provider -> sessions folder(s)

Changing owner_kind, owner_name, timezone, heartbeat_patterns or automated_prefixes does not
reparse stored transcripts by itself: refresh an index with --full to apply them to it.

This module imports nothing else from fleetwatch: data.py imports LOCAL_TZ, a zone that
resolves on first use.
"""
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone, tzinfo

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


def config_dir():
    """fleetwatch's config folder: FLEETWATCH_CONFIG_DIR, else ~/.config/fleetwatch."""
    return os.environ.get("FLEETWATCH_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".config", "fleetwatch")


def settings_path():
    return os.path.join(config_dir(), "settings.toml")


def load(path=None):
    """The parsed settings.toml as a dict; {} when it is missing or not valid TOML."""
    try:
        with open(path or settings_path(), "rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _zone(name):
    if ZoneInfo is None or not isinstance(name, str) or not name.strip():
        return None
    try:
        return ZoneInfo(name.strip())
    except Exception:
        return None


def system_tz():
    """The machine's local zone with its DST rules when they can be found, else its current UTC offset."""
    zone = _zone(os.environ.get("TZ", "").lstrip(":"))
    if zone is None:
        try:
            target = os.path.realpath("/etc/localtime")
            if "zoneinfo/" in target:
                zone = _zone(target.split("zoneinfo/", 1)[1])
        except OSError:
            zone = None
    return zone or datetime.now().astimezone().tzinfo or timezone.utc


def local_tz(raw=None):
    """The zone dates are shown and date-only `due` values are read in: settings.toml `timezone`, else the system's."""
    raw = load() if raw is None else raw
    return _zone(raw.get("timezone")) or system_tz()


def owner_name(raw=None):
    """The owner's name from settings.toml, or "" when none is set."""
    raw = load() if raw is None else raw
    name = raw.get("owner_name")
    return name.strip() if isinstance(name, str) else ""


class LazyZone(tzinfo):
    """The local zone, read from settings.toml on first use rather than at import, so the
    folder migration and --demo's settings run before the zone is chosen. It stands in for
    the zone everywhere a tzinfo goes; the first call fixes it for the process."""

    def __init__(self, resolve=None):
        self._resolve = resolve or local_tz
        self._zone = None

    def zone(self):
        if self._zone is None:
            self._zone = self._resolve()
        return self._zone

    def utcoffset(self, dt):
        return self.zone().utcoffset(dt if dt is None else dt.replace(tzinfo=self.zone()))

    def dst(self, dt):
        return self.zone().dst(dt if dt is None else dt.replace(tzinfo=self.zone()))

    def tzname(self, dt):
        return self.zone().tzname(dt if dt is None else dt.replace(tzinfo=self.zone()))

    def fromutc(self, dt):
        # A zoneinfo zone refuses a datetime whose tzinfo is another object, so convert
        # under the real zone and hand back a datetime that carries this one.
        return self.zone().fromutc(dt.replace(tzinfo=self.zone())).replace(tzinfo=self)

    def __repr__(self):
        return "LazyZone(%r)" % (self._zone if self._zone is not None else "unresolved")


LOCAL_TZ: tzinfo = LazyZone()


# ------------------------------------------------------------------ indexer settings

class SettingsError(ValueError):
    """settings.toml holds something the indexer cannot use; the message names the key."""


# Claude Code's own scheduled prompts carry a scheduledTaskId the parser checks first; these
# catch a heartbeat prompt that arrives as ordinary text.
DEFAULT_HEARTBEAT_PATTERNS = ("HEARTBEAT", "^Heartbeat:")
# One-shot runs that are not a conversation: the security-review action and readiness probes.
DEFAULT_AUTOMATED_PREFIXES = ("Review this change for security vulnerabilities", "Reply with only")
DEFAULT_RULE_PACKS = ("education",)
DEFAULT_PROTECTED_GROUPS = ("ferpa",)


def is_protected(group, protected):
    """True when `group` is one of the protected groups (settings.toml [groups] protected),
    compared without case or surrounding space so a respelt group name is still protected.
    Every protected-group check goes through this, so the read guard and the scan rules
    never disagree about a group."""
    key = str(group or "").strip().casefold()
    return any(key == str(g).strip().casefold() for g in protected)
DEFAULT_OWNER_KIND = "owner"
# The other canonical sender kinds; the owner's kind may not take one of these names.
RESERVED_KINDS = frozenset(("heartbeat", "subagent", "peer-session", "other", "interrupt"))
KIND_RE = re.compile(r"[a-z][a-z0-9_-]*")


def load_strict(path=None):
    """The parsed settings.toml; {} when the file does not exist. Raises SettingsError when
    it exists but is not valid TOML."""
    path = path or settings_path()
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise SettingsError("%s: %s" % (path, e))


def state_dir():
    """fleetwatch's log folder: $XDG_STATE_HOME/fleetwatch, else ~/.local/state/fleetwatch."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "fleetwatch")


def data_dir():
    """fleetwatch's data folder: $XDG_DATA_HOME/fleetwatch, else ~/.local/share/fleetwatch."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "fleetwatch")


@dataclass(frozen=True)
class IndexerSettings:
    tz: tzinfo
    owner_name: str                 # "" when unset; transcripts then say "User"
    owner_kind: str
    data_dir: Path
    index_root: Path
    projects_dir: Path
    build_python: str
    heartbeat_patterns: tuple
    automated_prefixes: tuple
    protected_groups: frozenset
    group_guess: tuple              # ((fragment, group), ...) in file order
    rule_packs: tuple               # built-in names, or absolute paths of pack files
    column_denylist: tuple = None   # None: compose from the packs
    default_guidance: str = None    # None: the tagger's built-in guidance
    config_root: Path = None        # where registry.yaml, registry.d and per-index config live;
                                    # None: same as index_root
    catch_all_index: str = None     # optional index name for unrouted agent runs (protected group)
    routing_sources: tuple = None   # None: provider defaults; else ((provider, (Path, ...)), ...)

    @property
    def owner_display(self):
        return self.owner_name or "User"

    def source_dirs(self, provider, default_path=None):
        """Session directories to scan for agent `provider`."""
        if self.routing_sources is not None:
            by_name = dict(self.routing_sources)
            if provider in by_name:
                return list(by_name[provider])
        if default_path:
            return [Path(os.path.expanduser(default_path))]
        return []


def _table(raw, key):
    v = raw.get(key, {})
    if not isinstance(v, dict):
        raise SettingsError("settings.toml: [%s] must be a table" % key)
    return v


def _str(t, key, where, default=None):
    v = t.get(key, default)
    if v is not None and not (isinstance(v, str) and v.strip()):
        raise SettingsError("settings.toml: %s%s must be a non-empty string" % (where, key))
    return v.strip() if isinstance(v, str) else v


def _strs(t, key, where, default):
    v = t.get(key)
    if v is None:
        return tuple(default)
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise SettingsError("settings.toml: %s%s must be a list of non-empty strings" % (where, key))
    return tuple(v)


def _path(v, base=None):
    p = Path(os.path.expanduser(v))
    return p if p.is_absolute() or base is None else Path(base) / p


def indexer(raw=None, cfg_dir=None):
    """The indexer's settings, resolved and checked. `raw` is a parsed settings dict (default:
    settings.toml read strictly); relative file names resolve against `cfg_dir` (default: the
    config folder)."""
    cfg_dir = Path(cfg_dir or config_dir())
    raw = load_strict(cfg_dir / "settings.toml") if raw is None else raw
    if "timezone" in raw:
        tz = _zone(raw["timezone"])
        if tz is None:
            raise SettingsError("settings.toml: timezone %r is not a known IANA zone" % (raw["timezone"],))
    else:
        tz = system_tz()
    name = raw.get("owner_name", "")
    if not isinstance(name, str):
        raise SettingsError("settings.toml: owner_name must be a string")

    paths = _table(raw, "paths")
    ddir = _path(_str(paths, "data_dir", "paths.") or data_dir())
    root = _path(_str(paths, "index_root", "paths.") or str(ddir / "indexes"))
    projects = _path(_str(paths, "projects_dir", "paths.")
                     or os.environ.get("FLEETWATCH_PROJECTS_DIR") or "~/.claude/projects")
    cfg_root = _str(paths, "index_config_root", "paths.")
    if cfg_root:
        cfg_root = _path(cfg_root)
    elif "index_root" not in paths:
        cfg_root = cfg_dir / "indexes"
    else:
        cfg_root = root
    build_py = os.path.expanduser(_str(paths, "build_python", "paths.") or sys.executable)

    tr = _table(raw, "transcripts")
    kind = _str(tr, "owner_kind", "transcripts.", DEFAULT_OWNER_KIND)
    if not KIND_RE.fullmatch(kind) or kind in RESERVED_KINDS:
        raise SettingsError("settings.toml: transcripts.owner_kind %r must be lowercase letters, "
                            "digits, - or _, and not one of %s" % (kind, ", ".join(sorted(RESERVED_KINDS))))
    beats = _strs(tr, "heartbeat_patterns", "transcripts.", DEFAULT_HEARTBEAT_PATTERNS)
    for b in beats:
        try:
            re.compile(b)
        except re.error as e:
            raise SettingsError("settings.toml: transcripts.heartbeat_patterns %r: %s" % (b, e))
    prefixes = _strs(tr, "automated_prefixes", "transcripts.", DEFAULT_AUTOMATED_PREFIXES)

    gr = _table(raw, "groups")
    protected = frozenset(_strs(gr, "protected", "groups.", DEFAULT_PROTECTED_GROUPS))
    guess = gr.get("guess", {})
    if not isinstance(guess, dict) or not all(isinstance(v, str) for v in guess.values()):
        raise SettingsError("settings.toml: groups.guess must map folder fragments to group names")

    sc = _table(raw, "scanner")
    packs = []
    for p in _strs(sc, "rule_packs", "scanner.", DEFAULT_RULE_PACKS):
        packs.append(str(_path(p, cfg_dir)) if p.endswith((".yaml", ".yml")) else p)

    docs = _table(raw, "docs")
    cols = docs.get("column_denylist")
    if cols is not None:
        cols = _strs(docs, "column_denylist", "docs.", ())

    tg = _table(raw, "tagger")
    guidance = _str(tg, "default_guidance", "tagger.")
    gfile = _str(tg, "default_guidance_file", "tagger.")
    if guidance and gfile:
        raise SettingsError("settings.toml: set tagger.default_guidance or default_guidance_file, not both")
    if gfile:
        try:
            guidance = _path(gfile, cfg_dir).read_text(encoding="utf-8")
        except OSError as e:
            raise SettingsError("settings.toml: tagger.default_guidance_file: %s" % e)

    rt = _table(raw, "routing")
    unknown_rt = sorted(set(rt) - {"catch_all", "sources"})
    if unknown_rt:
        raise SettingsError("settings.toml: routing.%s is not a setting" % unknown_rt[0])
    catch_all = _str(rt, "catch_all", "routing.")
    rsources = None
    if "sources" in rt:
        src_tbl = rt["sources"]
        if not isinstance(src_tbl, dict):
            raise SettingsError("settings.toml: routing.sources must be a table mapping provider names to path(s)")
        parsed = []
        for prov, val in sorted(src_tbl.items()):
            if not isinstance(prov, str) or not prov.strip():
                raise SettingsError("settings.toml: routing.sources provider name must be a non-empty string")
            if isinstance(val, str):
                if not val.strip():
                    raise SettingsError("settings.toml: routing.sources.%s must be a non-empty path" % prov)
                parsed.append((prov.strip(), (_path(val.strip()),)))
            elif isinstance(val, list) and all(isinstance(x, str) and x.strip() for x in val):
                parsed.append((prov.strip(), tuple(_path(x.strip()) for x in val)))
            else:
                raise SettingsError("settings.toml: routing.sources.%s must be a path or list of paths" % prov)
        rsources = tuple(parsed)

    return IndexerSettings(
        tz=tz, owner_name=name.strip(), owner_kind=kind, data_dir=ddir, index_root=root,
        projects_dir=projects, build_python=build_py, heartbeat_patterns=beats,
        automated_prefixes=prefixes, protected_groups=protected, group_guess=tuple(guess.items()),
        rule_packs=tuple(packs), column_denylist=cols, default_guidance=guidance,
        config_root=cfg_root, catch_all_index=catch_all, routing_sources=rsources)
