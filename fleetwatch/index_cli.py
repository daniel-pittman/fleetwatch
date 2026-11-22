"""`fleetwatch index ...`: manage the session and document indexes.

    list | status [NAME]
    refresh [NAME | --all] [--full] [--no-tag] [--tag-budget N] [--build]
    file PATH [--index NAME]          index a just-filed document now
    build NAME                        rebuild the search engine's index only
    retag NAME [--all] | discover | triage NAME
    forget NAME SESSION_ID [--force]  remove one session from an index (records, tags, corpus)
    add [--session NAME] --group G [--docs PATH --include GLOB ...]
    migrate NAME --from OLD_ROOT [--force]
    verify NAME [--baseline FILE] [--compare-old OLD_ROOT]
    schedule install | remove | status

The refresh, file, retag, discover, triage, forget and status commands are thin wrappers over
fleetwatch.memory.refresh. With no NAME, refresh and file act on the calling session's own
index (found from process ancestry, as `write-status` finds the session). A command that names
an index runs the same trust guard as `search` first and exits 3 when it refuses.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from . import data, indexes
from .indexes import IndexesError, tilde

USAGE = __doc__

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


def _guard(reg, name):
    """Raise Refused unless the calling session may read index `name`. Every command that
    names an index runs this first: each one reads its content, and triage prints names."""
    why = indexes.refresh().index_name_problem(name)
    if why:
        raise IndexesError(why)
    if name in reg.get("indexes", {}):
        why = indexes.read_refusal(reg, indexes.calling_session(), name)
        if why:
            raise Refused(why)
    return name


def _own_or(name, reg):
    return _guard(reg, name) if name else indexes.own_index(reg, indexes.calling_session())


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
    name = args[0] if args else None
    if name:
        _guard(reg, name)
        if name not in reg["indexes"]:
            raise IndexesError("no index %r in the registry (see `fleetwatch index list`)" % name)
        reg = dict(reg, indexes={name: reg["indexes"][name]})
    else:
        # Every index the calling session may read; outside a session, every index.
        session = indexes.calling_session()
        shown = {n: e for n, e in reg["indexes"].items()
                 if indexes.read_refusal(reg, session, n) is None}
        hidden = len(reg["indexes"]) - len(shown)
        reg = dict(reg, indexes=shown)
        if hidden:
            print("(%d index%s this session may not read not shown)" % (hidden, "" if hidden == 1 else "es"))
    r.cmd_status(reg)
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
    loc = indexes.locate(reg, _guard(reg, args[0]))
    ix = engine.Index(loc.cfg_dir, indexes.search_config(loc))
    cli.set_env_defaults()
    cli.cmd_build(ix, None)
    return 0


def cmd_add(reg, args):
    ap = argparse.ArgumentParser(prog="fleetwatch index add")
    ap.add_argument("--session", help="session name (default: the calling session)")
    ap.add_argument("--session-id", help="session id when --session names none that is running")
    ap.add_argument("--name", help="index name (default: <session>-session, or codex-sessions)")
    ap.add_argument("--group", required=True)
    ap.add_argument("--source", choices=("claude", "codex"), default="claude",
                    help="codex: index the Codex CLI sessions folder; the index has no owner "
                         "session and one index covers every project, so choose its group "
                         "for the most private project in it")
    ap.add_argument("--path", help="the Codex sessions folder (default: ~/.codex/sessions)")
    ap.add_argument("--docs", help="a folder of reference documents to index")
    ap.add_argument("--include", action="append", help="glob under --docs (repeatable; default **/*.md)")
    ap.add_argument("--label", default="docs", help="label of the --docs folder")
    a = ap.parse_args(args)
    r = indexes.refresh()
    paths = data.default_paths()
    codex = a.source == "codex"
    if a.path and not codex:
        return _fail("--path names the Codex sessions folder; use it with --source codex")
    if codex:
        if a.session or a.session_id:
            return _fail("--session and --session-id name a Claude session; a Codex index has "
                         "no owner session (drop them with --source codex)")
        sname = sid = None
        folder = Path(os.path.abspath(os.path.expanduser(a.path or "~/.codex/sessions")))
        if not folder.is_dir():
            return _fail("the Codex sessions folder %s does not exist" % tilde(folder))
        name = a.name or "codex-sessions"
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
    owned = [] if codex else indexes.owned_by(reg, sname, sid)
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
    if codex:
        entry = {"group": a.group,
                 "transcripts": {"source": "codex", "path": tilde(folder)}}
    else:
        entry = {"group": a.group, "owner_session": sname, "owner_session_id": sid,
                 "transcripts": {"project_dir": [tilde(f) for f in folders], "sessions": [sid],
                                 "exclude_automated": True}}
    if a.docs:
        entry["docs"] = [{"label": a.label, "path": tilde(os.path.abspath(os.path.expanduser(a.docs))),
                          "include": a.include or ["**/*.md"]}]
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
    (cfg / "tags.yaml").write_text(CODEX_TAGS_TEMPLATE if codex else TAGS_TEMPLATE)
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
                 % ("Codex sessions index" if codex else "session %s" % sname))
        fh.write(yaml.safe_dump({"indexes": {name: entry}}, sort_keys=False))
    if codex:
        print("added index %s (group %s) over the Codex sessions in %s" % (name, a.group, tilde(folder)))
    else:
        print("added index %s (group %s) for session %s" % (name, a.group, sname))
        print("transcript folders: %d" % len(folders))
    print("registry entry: %s" % tilde(entry_file))
    print("config folder: %s (tags.yaml, probes.yaml, scan_allowlist.yaml are templates to edit)"
          % tilde(cfg))
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
                 "build": cmd_build, "add": cmd_add, "migrate": cmd_migrate, "verify": cmd_verify}
        if verb not in table:
            return _fail("unknown command %r\n%s" % (verb, USAGE))
        return table[verb](indexes.load_registry(), rest) or 0
    except Refused as e:
        return _fail(str(e), 3)
    except IndexesError as e:
        return _fail(str(e))
