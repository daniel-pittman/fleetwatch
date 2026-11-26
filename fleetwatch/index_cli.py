"""`fleetwatch index ...`: manage the session and document indexes.

    list | status [NAME]
    refresh [NAME | --all] [--full] [--no-tag] [--tag-budget N] [--build]
    file PATH [--index NAME]          index a just-filed document now
    build NAME                        rebuild the search engine's index only
    retag NAME [--all] | discover | triage NAME
    forget NAME SESSION_ID [--force]  remove one session from an index (records, tags, corpus)
    add [--session NAME] --group G [--docs PATH ... --include GLOB ...]
    docs add NAME --docs PATH [--include GLOB ...] [--label L]   add a docs folder to your index
    add --source codex|antigravity --group G [--path DIR] [--name INDEX]
    migrate NAME --from OLD_ROOT [--force]
    verify NAME [--baseline FILE] [--compare-old OLD_ROOT]
    schedule install | remove | status

The refresh, file, retag, discover, triage, forget and status commands are thin wrappers over
fleetwatch.memory.refresh. With no NAME, refresh and file act on the calling session's own
index (found from process ancestry, as `write-status` finds the session). Commands that read a
named index run the same trust guard as `search` first and exit 3 when it refuses.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml

from . import data, indexes
from .indexes import IndexesError, tilde

USAGE = __doc__

LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]*")

TAGS_TEMPLATE = """\
# Taxonomy for the tagger: what a session is about, and which areas and kinds an exchange has.
description: "a Claude Code working session"
areas:
  build: writing or changing code
  design: planning, architecture and decisions
  review: reviewing, testing and verifying
  ops: running, deploying and maintaining
  other: anything else
kinds:
  decision: a choice was made, with its reason
  instruction: the owner told the session what to do
  question: a question was asked or left open
  status: progress or a result was reported
  finding: something was discovered or diagnosed
"""

CODEX_TAGS_TEMPLATE = TAGS_TEMPLATE.replace(
    'description: "a Claude Code working session"',
    'description: "a Codex CLI working session (the transcripts are Codex rollouts, not '
    'Claude Code)"')
ANTIGRAVITY_TAGS_TEMPLATE = TAGS_TEMPLATE.replace(
    'description: "a Claude Code working session"',
    'description: "an Antigravity CLI working session (the transcripts are Antigravity '
    'conversations, not Claude Code)"')

# The sources one index covers whole: the tool keeps every session in one folder, so the
# index has no owner session. source -> (label, default folder, default index name, tags).
FOLDER_SOURCES = {
    "codex": ("Codex", "~/.codex/sessions", "codex-sessions", CODEX_TAGS_TEMPLATE),
    "antigravity": ("Antigravity", "~/.gemini/antigravity-cli/conversations",
                    "antigravity-sessions", ANTIGRAVITY_TAGS_TEMPLATE),
}

PROBES_TEMPLATE = """\
# Fixed questions with the document or exchange that should answer each, for `fleetwatch
# search eval`. Add a probe whenever a real question returns the wrong thing. expect takes
# path_contains, heading_contains, level and area; a result matches when all the given ones do.
#
# probes:
#   - query: "what did we decide about the retry policy"
#     expect: {path_contains: "S1a2b3c-EX-0042"}
probes: []
"""

ALLOWLIST_TEMPLATE = """\
# Scrub allowlist: tokens the privacy scan must leave alone. Values are lowercase.
version: 1
emails: []        # addresses, "*suffix" or "prefix*" that may stay
staff: []         # name tokens of people who are not private individuals
names: []         # tokens that look like names but are not people
"""


def _fail(msg, code=2):
    print("fleetwatch index: %s" % msg, file=sys.stderr)
    return code


class Refused(IndexesError):
    """The trust guard refused the calling session this index (exit 3, as `search` does)."""


def _require_direct_caller(verb):
    caller = indexes.classify_caller()
    if caller.kind in ("untrusted", "delegated"):
        raise Refused("refused: index %s requires a Claude session or the owner at a terminal (%s)"
                      % (verb, caller.sign))


def _guard(reg, name, caller=None):
    """Raise Refused unless the caller may read index `name`. Triage prints raw names."""
    why = indexes.refresh().index_name_problem(name)
    if why:
        raise IndexesError(why)
    if name in reg.get("indexes", {}):
        why = indexes.read_refusal(reg, caller if caller is not None else indexes.classify_caller(), name)
        if why:
            raise Refused(why)
    return name


def _own_or(name, reg):
    if name:
        return _guard(reg, name)
    caller = indexes.classify_caller()
    return _guard(reg, indexes.own_index(reg, caller.session), caller)


def cmd_list(reg, args):
    r = indexes.refresh()
    print("%-28s %-20s %-8s %s" % ("INDEX", "GROUP", "KIND", "LAST RUN"))
    for name, e in sorted(reg["indexes"].items()):
        try:
            info = json.loads((r.ROOT / name / "state" / "last_run.json").read_text())
            last = ("ok " if info.get("ok") else "FAILED ") + str(info.get("finished", "?"))
        except (OSError, ValueError):
            last = "never"
        print("%-28s %-20s %-8s %s" % (name, e.get("group", "?"), e.get("kind", "session"), last))
    return 0


def cmd_status(reg, args):
    r = indexes.refresh()
    full_reg = reg
    name = args[0] if args else None
    if name:
        _guard(reg, name)
        if name not in reg["indexes"]:
            raise IndexesError("no index %r in the registry (see `fleetwatch index list`)" % name)
        reg = dict(reg, indexes={name: reg["indexes"][name]})
    else:
        # Show only indexes the caller may read.
        caller = indexes.classify_caller()
        shown = {n: e for n, e in reg["indexes"].items()
                 if indexes.read_refusal(reg, caller, n) is None}
        hidden = len(reg["indexes"]) - len(shown)
        reg = dict(reg, indexes=shown)
        if hidden:
            print("(%d index%s this session may not read not shown)" % (hidden, "" if hidden == 1 else "es"))
    r.cmd_status(reg, full_reg=full_reg)
    return 0


def cmd_refresh(reg, args):
    ap = argparse.ArgumentParser(prog="fleetwatch index refresh")
    ap.add_argument("name", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--no-tag", action="store_true")
    ap.add_argument("--tag-budget", type=int)
    ap.add_argument("--build", action="store_true")
    a = ap.parse_args(args)
    if a.all and a.name:
        return _fail("give an index name or --all, not both")
    argv = []
    if not a.all:
        argv += ["--index", _own_or(a.name, reg)]
    for flag, on in (("--full", a.full), ("--no-tag", a.no_tag), ("--build", a.build)):
        if on:
            argv.append(flag)
    if a.tag_budget is not None:
        argv += ["--tag-budget", str(a.tag_budget)]
    indexes.refresh().main(argv)


def cmd_file(reg, args):
    ap = argparse.ArgumentParser(prog="fleetwatch index file")
    ap.add_argument("path")
    ap.add_argument("--index")
    a = ap.parse_args(args)
    indexes.refresh().main(["index-now", _own_or(a.index, reg), os.path.abspath(a.path)])


def cmd_build(reg, args):
    if len(args) != 1:
        return _fail("usage: fleetwatch index build NAME")
    from .search import cli, engine
    os.umask(indexes.refresh().PRIVATE_UMASK)
    # A refresh launches this writer without a terminal. It emits build counts, not index
    # content. Recognized Claude sessions still use their existing named-index guard.
    caller = indexes.classify_caller()
    name = args[0]
    protected = indexes.refresh().SETTINGS.protected_groups
    target_group = (reg.get("indexes", {}).get(name) or {}).get("group")
    if caller.kind != "untrusted" or (indexes.is_agent_caller(caller) and
                                      indexes.is_protected(target_group, protected)):
        name = _guard(reg, name, caller)
    else:
        why = indexes.refresh().index_name_problem(name)
        if why:
            raise IndexesError(why)
    loc = indexes.locate(reg, name)
    ix = engine.Index(loc.cfg_dir, indexes.search_config(loc))
    cli.set_env_defaults()
    cli.cmd_build(ix, None)
    return 0


def cmd_add(reg, args):
    _require_direct_caller("add")
    ap = argparse.ArgumentParser(prog="fleetwatch index add")
    ap.add_argument("--session", help="session name (default: the calling session)")
    ap.add_argument("--session-id", help="session id when --session names none that is running")
    ap.add_argument("--name", help="index name (default: <session>-session, codex-sessions or "
                                   "antigravity-sessions)")
    ap.add_argument("--group", required=True)
    ap.add_argument("--source", choices=("claude",) + tuple(FOLDER_SOURCES), default="claude",
                    help="codex or antigravity: index that CLI's sessions folder; the index has "
                         "no owner session and one index covers every project, so choose its "
                         "group for the most private project in it")
    ap.add_argument("--path", help="the sessions folder of --source codex (default "
                                   "~/.codex/sessions) or antigravity (default "
                                   "~/.gemini/antigravity-cli/conversations)")
    ap.add_argument("--docs", action="append",
                    help="a folder of reference documents to index (repeatable: one docs entry each)")
    ap.add_argument("--include", action="append",
                    help="glob under every --docs folder (repeatable; default **/*.md)")
    ap.add_argument("--label", action="append",
                    help="label of a --docs folder (default docs for one folder, else the folder's "
                         "name); give one per --docs, in order")
    a = ap.parse_args(args)
    r = indexes.refresh()
    paths = data.default_paths()
    whole = FOLDER_SOURCES.get(a.source)
    if a.path and not whole:
        return _fail("--path names a sessions folder; use it with --source codex or antigravity")
    if whole:
        label, default_path, default_name, tags_template = whole
        if a.session or a.session_id:
            return _fail("--session and --session-id name a Claude session; a %s index has "
                         "no owner session (drop them with --source %s)" % (label, a.source))
        sname = sid = None
        folder = Path(os.path.abspath(os.path.expanduser(a.path or default_path)))
        if not folder.is_dir():
            return _fail("the %s sessions folder %s does not exist" % (label, tilde(folder)))
        name = a.name or default_name
    else:
        if a.session_id:
            sname, sid = a.session or a.session_id, a.session_id
        elif a.session:
            sname, sid = a.session, _session_id_by_name(paths, a.session)
        else:
            found = indexes.calling_session(paths)
            if found is None:
                return _fail("not inside a Claude session; pass --session NAME")
            sname, sid = found
        folders = indexes.session_folders(r.PROJECTS, sid)
        if not folders:
            return _fail("no transcript of session %s under %s" % (sid, r.PROJECTS))
        name = a.name or indexes.slug(sname) + "-session"
    if r.index_name_problem(name):
        return _fail(r.index_name_problem(name))
    owned = [] if whole else indexes.owned_by(reg, sname, sid)
    # Names compare without case: on a case-insensitive file system two names that differ
    # only in case share one config folder, one data folder and one registry.d file.
    rdir = Path(r.REGISTRY_DIR)
    files = {f.name[:-len(".yaml")].casefold() for f in rdir.glob("*.yaml")} if rdir.is_dir() else set()
    if indexes.names_clash(reg, name) or name.casefold() in files:
        return _fail("index %r already exists; nothing was changed" % name)
    if owned:
        return _fail("session %r already owns %s; nothing was changed" % (sname, ", ".join(owned)))
    groups = reg.get("groups") or {}
    if groups and a.group not in groups:
        return _fail("group %r is not declared in the registry (have: %s)"
                     % (a.group, ", ".join(sorted(groups))))
    docs = _docs_entries(a.docs or [], a.include, a.label)
    if whole:
        entry = {"group": a.group,
                 "transcripts": {"source": a.source, "path": tilde(folder)}}
    else:
        entry = {"group": a.group, "owner_session": sname, "owner_session_id": sid,
                 "transcripts": {"project_dir": [tilde(f) for f in folders], "sessions": [sid],
                                 "exclude_automated": True}}
    if docs:
        entry["docs"] = docs
    entry["privacy"] = {"allowlist": "%s/scan_allowlist.yaml" % name}
    entry["tags"] = "%s/tags.yaml" % name
    cfg = Path(r.CONFIG_ROOT) / name
    cfg.mkdir(parents=True, exist_ok=True)
    allow = cfg / "scan_allowlist.yaml"
    allow.write_text(ALLOWLIST_TEMPLATE)
    from .memory.privacy_scan import RuleSet, Scanner
    problems = r.protection_problems(a.group, Scanner(allow, None, RuleSet(r.SETTINGS.rule_packs)))
    if problems:
        allow.unlink()
        cfg.rmdir()
        return _fail("a %s index cannot start from the template: %s" % (a.group, "; ".join(problems)))
    (cfg / "tags.yaml").write_text(tags_template if whole else TAGS_TEMPLATE)
    (cfg / "probes.yaml").write_text(PROBES_TEMPLATE)
    if not r.REGISTRY.exists():
        # A protected group starts with no readers: the guard ignores readers for it, and a
        # list naming the group would read as if every index in it could read the others.
        readers = [] if indexes.is_protected(a.group, r.SETTINGS.protected_groups) else [a.group]
        r.REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        r.REGISTRY.write_text(yaml.safe_dump({"defaults": dict(r.BUILTIN_DEFAULTS),
                                              "groups": {a.group: {"readers": readers}}},
                                             sort_keys=False))
    entry_file = Path(r.REGISTRY_DIR) / (name + ".yaml")
    entry_file.parent.mkdir(parents=True, exist_ok=True)
    with open(entry_file, "x") as fh:
        fh.write("# Registry entry for the %s.\n"
                 % ("%s sessions index" % label if whole else "session %s" % sname))
        fh.write(yaml.safe_dump({"indexes": {name: entry}}, sort_keys=False))
    if whole:
        print("added index %s (group %s) over the %s sessions in %s"
              % (name, a.group, label, tilde(folder)))
    else:
        print("added index %s (group %s) for session %s" % (name, a.group, sname))
        print("transcript folders: %d" % len(folders))
    print("registry entry: %s" % tilde(entry_file))
    print("config folder: %s (tags.yaml, probes.yaml, scan_allowlist.yaml are templates to edit)"
          % tilde(cfg))
    _print_docs_report(docs)
    print("next: fleetwatch index refresh %s" % name)
    return 0


def _docs_entries(paths, include, labels, single_label="docs"):
    """The registry `docs:` entries for `--docs` folders, each refused by docs_path_problem
    before anything is written. One folder is labeled `docs` unless told otherwise; several are
    labeled by folder name (`single_label=None` labels one by folder name too), and every label
    and folder must be distinct."""
    include = list(include or ["**/*.md"])
    labels = list(labels or [])
    for g in include:
        if not g or os.path.isabs(os.path.expanduser(g)):
            raise IndexesError("--include glob %r is not allowed: a glob is relative to its --docs "
                               "folder and not empty" % g)
    if labels and len(labels) != len(paths):
        raise IndexesError("give one --label per --docs folder, or none (got %d labels for %d folders)"
                           % (len(labels), len(paths)))
    out = []
    for i, raw in enumerate(paths):
        folder = os.path.abspath(os.path.expanduser(raw))
        for g in include:
            # Python 3.13 and later accept "**x" and read it as "*", where 3.11 and 3.12 raise,
            # so a "**" that is not a whole path part is refused here on every version.
            if any("**" in part and part != "**" for part in g.split("/")):
                raise IndexesError("--include glob %r is not valid ('**' must be a whole path "
                                   "part, as in '**/*.md')" % g)
            try:
                next(iter(Path(folder).glob(g)), None)
            except (ValueError, NotImplementedError) as e:
                raise IndexesError("--include glob %r is not valid (%s)" % (g, e))
        why = indexes.docs_path_problem(folder, include)
        if why:
            raise Refused(why)
        if labels:
            label = labels[i]
        elif single_label and len(paths) == 1:
            label = single_label
        else:
            label = re.sub(r"[^A-Za-z0-9._ -]+", "-", Path(folder).name).lstrip("._ -") or "docs"
        if not LABEL_RE.fullmatch(label) or ".." in label:
            raise IndexesError("docs label %r is not allowed: a label becomes a folder name in the "
                               "corpus, so use letters, digits, '.', '_', '-' and spaces, starting "
                               "with a letter or digit, with no '..' or separator" % label)
        out.append({"label": label, "path": tilde(folder), "include": list(include)})
    seen_paths = [os.path.realpath(os.path.expanduser(d["path"])) for d in out]
    # Labels are compared without case: each is a corpus folder, one folder on macOS.
    if len(set(seen_paths)) != len(seen_paths) or len({d["label"].casefold() for d in out}) != len(out):
        raise IndexesError("every --docs folder needs its own path and its own --label")
    return out


def _print_docs_report(entries):
    """After a docs entry is written: the glob warning, and how many documents it selects."""
    for d in entries:
        warn = indexes.include_warning(os.path.expanduser(d["path"]), d["include"])
        if warn:
            print(warn)
        matched, total = indexes.match_counts(d)
        print("docs %s: %d docs matched of %d files" % (d["label"], matched, total))


def _entry_file(name):
    """The one registry.d file that defines index `name`. Refuses an entry that lives in
    registry.yaml or in two files, since there is then no single file this session owns."""
    rdir = Path(indexes.refresh().REGISTRY_DIR)
    hits = []
    for f in sorted(rdir.glob("*.yaml")) if rdir.is_dir() else []:
        try:
            part = yaml.safe_load(f.read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        if name in (part.get("indexes") or {}):
            hits.append(f)
    if len(hits) != 1:
        raise IndexesError("the registry entry for %s is not in exactly one registry.d file, so "
                           "nothing was changed; edit its docs list by hand" % name)
    return hits[0]


def _lone_entry(text, name, fname):
    """The parsed registry.d file when it defines index `name` and nothing else, with no YAML
    anchors or aliases; refuses otherwise. A docs change then cannot reach another index's
    entry, through a shared mapping or a second key."""
    try:
        part = yaml.safe_load(text) or {}
        tokens = list(yaml.scan(text))
    except yaml.YAMLError as e:
        raise IndexesError("%s does not parse (%s); nothing was changed" % (fname, e))
    if any(isinstance(t, (yaml.AnchorToken, yaml.AliasToken)) for t in tokens):
        raise IndexesError("%s uses YAML anchors or aliases, so nothing was changed; edit its "
                           "docs list by hand" % fname)
    ents = part.get("indexes") if isinstance(part, dict) else None
    if not isinstance(ents, dict) or list(part) != ["indexes"] or list(ents) != [name]:
        raise IndexesError("%s holds more than the entry for %s, so nothing was changed; edit its "
                           "docs list by hand" % (fname, name))
    if not isinstance(ents[name], dict):
        raise IndexesError("index %s is no longer in %s; nothing was changed" % (name, fname))
    return part


def _last_line(node):
    """The 0-based line after which text can follow `node`: the line its last scalar ends on."""
    while isinstance(node, (yaml.MappingNode, yaml.SequenceNode)) and node.value:
        last = node.value[-1]
        node = last[1] if isinstance(node, yaml.MappingNode) else last
    end = node.end_mark
    return end.line if end.column > 0 else end.line - 1


def _docs_text(text, name, docs):
    """`text` with `docs` appended to the docs list of index `name`, by inserting lines only, so
    every comment and line of the file stays as written and in place. Returns None when the
    entry is not in the block layout this can extend."""
    root = yaml.compose(text)
    ents = next(v for k, v in root.value if k.value == "indexes")
    entry = next(v for k, v in ents.value if k.value == name)
    if not isinstance(entry, yaml.MappingNode) or entry.flow_style or not entry.value:
        return None
    seq = next((v for k, v in entry.value if k.value == "docs"), None)
    if seq is None:
        col = entry.value[0][0].start_mark.column
        block = yaml.safe_dump({"docs": docs}, sort_keys=False, default_flow_style=False)
        after = _last_line(entry)
    elif isinstance(seq, yaml.SequenceNode) and not seq.flow_style and seq.value:
        col = seq.start_mark.column
        block = yaml.safe_dump(docs, sort_keys=False, default_flow_style=False)
        after = _last_line(seq)
    else:
        return None
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    add = ["%s%s\n" % (" " * col, ln) if ln else "\n" for ln in block.splitlines()]
    return "".join(lines[:after + 1] + add + lines[after + 1:])


def _lock(folder):
    """An exclusive lock on folder/.docs-add.lock, opened without following a link and never
    truncated, so a lock file linked to another file cannot change that file."""
    import fcntl
    import stat
    path = folder / ".docs-add.lock"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise IndexesError("the lock file %s could not be opened safely (%s); nothing was changed"
                           % (tilde(path), e.strerror))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise IndexesError("the lock file %s is not a regular file; nothing was changed" % tilde(path))
    lock = os.fdopen(fd, "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    return lock


def _append_docs(name, docs):
    """Append docs entries to index `name` in its registry.d file, under a lock and through a
    temp file. The file must hold only that entry; the new entries are inserted as lines, so
    every comment stays where it was. A docs entry already there for the same folder or label
    (labels compared without case) refuses the whole change."""
    import copy
    from . import config
    entry_file = _entry_file(name)
    with _lock(entry_file.parent):
        text = entry_file.read_text()
        part = _lone_entry(text, name, entry_file.name)
        entry = part["indexes"][name]
        have = entry.get("docs") or []
        if not isinstance(have, list) or not all(isinstance(d, dict) for d in have):
            raise IndexesError("the docs list of %s in %s is not a list of entries; edit it by hand"
                               % (name, entry_file.name))
        real = {os.path.realpath(os.path.expanduser(str(d.get("path")))) for d in have}
        labels = {str(d.get("label") or Path(os.path.expanduser(str(d.get("path")))).name).casefold()
                  for d in have}
        for d in docs:
            if os.path.realpath(os.path.expanduser(d["path"])) in real:
                raise IndexesError("index %s already has a docs entry for %s; nothing was changed"
                                   % (name, d["path"]))
            if d["label"].casefold() in labels:
                raise IndexesError("index %s already has a docs entry labeled %r; pass --label "
                                   "to name this one differently. Nothing was changed"
                                   % (name, d["label"]))
        want = copy.deepcopy(part)
        want["indexes"][name]["docs"] = list(have) + docs
        new = _docs_text(text, name, docs)
        if new is None or yaml.safe_load(new) != want:
            raise IndexesError("the entry for %s in %s is not in a layout this command can extend "
                               "line by line, so nothing was changed; edit its docs list by hand"
                               % (name, entry_file.name))
        config.atomic_write(str(entry_file), new)
    return entry_file


def cmd_docs(reg, args):
    """`index docs add NAME --docs PATH`: a docs entry for an index the caller owns. Applies the
    refusals `index add` applies (direct caller, the trust guard, the never-index folders, a
    protected group's scanner rules) and the ownership rule `forget` applies."""
    if not args or args[0] != "add":
        return _fail("usage: fleetwatch index docs add NAME --docs PATH [--include GLOB ...] "
                     "[--label L]")
    _require_direct_caller("docs add")
    ap = argparse.ArgumentParser(prog="fleetwatch index docs add")
    ap.add_argument("name")
    ap.add_argument("--docs", action="append", required=True, help="a folder of documents")
    ap.add_argument("--include", action="append",
                    help="glob under the folder (repeatable; default **/*.md)")
    ap.add_argument("--label", action="append", help="label of the folder (default: its name)")
    a = ap.parse_args(args[1:])
    r = indexes.refresh()
    name = a.name
    why = r.index_name_problem(name)
    if why:
        raise IndexesError(why)
    if name not in reg["indexes"]:
        raise IndexesError("no index %r in the registry (see `fleetwatch index list`)" % name)
    _guard(reg, name)
    why = r.owner_refusal(reg, name, "add docs to")
    if why:
        raise Refused(why)
    entry = reg["indexes"][name]
    if entry.get("kind") == "plain" or not (entry.get("privacy") or {}).get("allowlist"):
        raise IndexesError("index %s has no docs entries to extend (it is a plain index or has no "
                           "privacy block)" % name)
    group = entry.get("group")
    if group not in (reg.get("groups") or {}):
        raise IndexesError("index %s is in group %r, which is not declared in the registry, so its "
                           "protection cannot be checked; fix the group first" % (name, group))
    if indexes.is_protected(group, r.SETTINGS.protected_groups):
        from .memory.privacy_scan import RuleSet, Scanner
        try:
            deny = entry["privacy"].get("denylist")
            scanner = Scanner(Path(r.CONFIG_ROOT) / entry["privacy"]["allowlist"],
                              Path(r.CONFIG_ROOT) / deny if deny else None,
                              RuleSet(r.SETTINGS.rule_packs))
            problems = r.protection_problems(group, scanner, doc_options=entry.get("doc_options"))
        except Exception as e:
            problems = ["its scanner could not be loaded (%s)" % e]
        if problems:
            raise IndexesError("a %s index cannot take a docs entry: %s" % (group, "; ".join(problems)))
    docs = _docs_entries(a.docs, a.include, a.label, single_label=None)
    for d in docs:
        if not os.path.isdir(os.path.expanduser(d["path"])):
            raise IndexesError("the docs folder %s does not exist" % d["path"])
    entry_file = _append_docs(name, docs)
    for d in docs:
        print("added docs entry %s (%s) to index %s" % (d["label"], d["path"], name))
    print("registry entry: %s" % tilde(entry_file))
    _print_docs_report(docs)
    print("next: fleetwatch index refresh %s" % name)
    return 0


def _session_id_by_name(paths, name):
    """sessionId of the newest session registry file with this name."""
    best, sid = -1, None
    for f in Path(paths["sessions"]).glob("*.json"):
        try:
            d = data.load_json(str(f))
            if isinstance(d, dict) and d.get("name") == name and d.get("sessionId"):
                if f.stat().st_mtime > best:
                    best, sid = f.stat().st_mtime, d["sessionId"]
        except (OSError, ValueError):
            continue
    if sid is None:
        raise IndexesError("no running session named %r; run this from inside it, or pass "
                           "--session-id" % name)
    return sid


def cmd_migrate(reg, args):
    ap = argparse.ArgumentParser(prog="fleetwatch index migrate")
    ap.add_argument("name")
    ap.add_argument("--from", dest="old", required=True, metavar="OLD_ROOT")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(args)
    if a.force:
        _require_direct_caller("migrate --force")
    # The live registry's entry first: an index already here is overwritten only by a
    # session the guard lets read it (--force included). The old entry is checked too.
    _guard(reg, a.name)
    old = Path(os.path.expanduser(a.old))
    if (old / "registry.yaml").exists() or (old / "registry.d").is_dir():
        _guard(indexes.refresh().load_registry(old / "registry.yaml", old / "registry.d"), a.name)
    from . import index_migrate
    return index_migrate.migrate(a.name, a.old, a.force)


def cmd_verify(reg, args):
    ap = argparse.ArgumentParser(prog="fleetwatch index verify")
    ap.add_argument("name")
    ap.add_argument("--baseline")
    ap.add_argument("--compare-old", metavar="OLD_ROOT")
    a = ap.parse_args(args)
    _guard(reg, a.name)
    from . import index_migrate
    return 1 if index_migrate.verify(a.name, a.baseline, a.compare_old) else 0


def main(argv):
    argv = list(argv)
    verb, rest = (argv[0], argv[1:]) if argv else ("", [])
    if verb in ("", "-h", "--help"):
        print(USAGE)
        return 0
    if verb == "schedule":
        from . import schedule
        return schedule.main(rest)
    try:
        if verb in ("retag", "triage", "forget"):
            if not rest or rest[0].startswith("-") or (verb == "forget" and len(
                    [a for a in rest if a != "--force"]) != 2):
                return _fail("usage: fleetwatch index %s NAME%s" % (verb, {
                    "retag": " [--all]", "forget": " SESSION_ID [--force]"}.get(verb, "")))
            reg = indexes.load_registry()
            _guard(reg, rest[0])
            if verb == "forget" and rest[0] in reg["indexes"]:
                why = indexes.refresh().owner_refusal(reg, rest[0])
                if why:
                    raise Refused(why)
        if verb in ("retag", "discover", "triage", "forget"):
            indexes.refresh().main([verb] + rest)
            return 0
        table = {"list": cmd_list, "status": cmd_status, "refresh": cmd_refresh, "file": cmd_file,
                 "build": cmd_build, "add": cmd_add, "docs": cmd_docs, "migrate": cmd_migrate, "verify": cmd_verify}
        if verb not in table:
            return _fail("unknown command %r\n%s" % (verb, USAGE))
        return table[verb](indexes.load_registry(), rest) or 0
    except Refused as e:
        return _fail(str(e), 3)
    except IndexesError as e:
        return _fail(str(e))
