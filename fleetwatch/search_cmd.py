"""`fleetwatch search`: query a session or plain index.

    fleetwatch search "question" [--similar] [--provider claude|codex|antigravity] [--kind K]
                                 [--tag T] [--level D] [--around N] [--limit N] [--json] [--index NAME]
    fleetwatch search show ID [ID ...] [--around N] [--full]
    fleetwatch search eval | stats [--index NAME]

Without --index the calling session's own index is searched (found from process ancestry, as
`write-status` finds the session). From inside a session, another index is read only when the
trust guard allows it (indexes.read_refusal). Output is the index CLI's own, flag for flag.
Repeat --provider to match exchanges from any of the named providers.
"""
import sys

from . import indexes
from .indexes import IndexesError

USAGE = __doc__


def take_index(argv):
    """(index name or None, argv without --index)."""
    out, name, i = [], None, 0
    while i < len(argv):
        a = argv[i]
        if a == "--index":
            if i + 1 >= len(argv):
                raise IndexesError("--index needs a name")
            name, i = argv[i + 1], i + 2
        elif a.startswith("--index="):
            name, i = a.split("=", 1)[1], i + 1
        else:
            out.append(a)
            i += 1
    return name, out


def run_index(loc, argv, config=None):
    """Run the index CLI for a located index with `argv`; raises SystemExit like the CLI."""
    from .memory import session_search
    from .search import cli
    config = config if config is not None else indexes.search_config(loc)
    if loc.is_session:
        session_search.run(loc.cfg_dir, loc.data_dir, argv=argv, config=config)
    else:
        cli.set_env_defaults()
        cli.main(loc.cfg_dir, None, argv, config=config)


def main(argv):
    argv = list(argv)
    if not argv or argv in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    try:
        name, rest = take_index(argv)
        reg = indexes.load_registry()
        session = indexes.calling_session()
        if name is None:
            name = indexes.own_index(reg, session)
        if name not in reg["indexes"]:
            raise IndexesError("no index %r in the registry (see `fleetwatch index list`)" % name)
        why = indexes.read_refusal(reg, session, name)
        if why:
            print("fleetwatch search: " + why, file=sys.stderr)
            return 3
        loc = indexes.locate(reg, name)
        from .memory import session_search
        session_search.arm_memory_cap()
        run_index(loc, rest)
        return 0
    except IndexesError as e:
        print("fleetwatch search: %s" % e, file=sys.stderr)
        return 2
