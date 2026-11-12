import argparse
import sys

from . import data, migrate


def build_parser():
    p = argparse.ArgumentParser(
        prog="fleetwatch", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="One screen of every Claude session. Live by default on a terminal;\n"
                    "--once prints one snapshot.",
        epilog="commands (run `fleetwatch COMMAND --help` for each):\n"
               "  resume [NAME]       resume a named session in its folder\n"
               "  park [NAME]         mark a session finished for now, with a note\n"
               "  unpark [NAME]       take a session off the parked list\n"
               "  prune               move finished sessions' status files to .pruned/\n"
               "  write-status        write this session's status file from JSON on stdin\n"
               "  jump NAME           raise a running session's iTerm2 tab\n"
               "  notify              desktop notifications when something new needs you\n"
               "  watch               run | report | ack | schedule: GitHub watches for stopped sessions\n"
               "  iterm               install | status | uninstall the hotkey window\n"
               "  search, index       session memory (need the search and memory extras)")
    p.add_argument("--window", type=data.parse_window, default=data.parse_window("24h"),
                   help="activity window, e.g. 90m, 6h, 3d (default 24h)")
    p.add_argument("--horizon", type=data.parse_horizon, default=data.DEFAULT_HORIZON_DAYS,
                   help="COMING UP window, e.g. 30d or 90d (default %dd)" % data.DEFAULT_HORIZON_DAYS)
    p.add_argument("--all", action="store_true", help="include stale records, helpers and orphans")
    p.add_argument("--json", action="store_true", help="emit one JSON snapshot")
    p.add_argument("--once", action="store_true", help="print one snapshot and exit")
    p.add_argument("--groups", action="store_true", help="with --once, add a BY GROUP block")
    p.add_argument("--interval", type=float, default=data.DEFAULT_INTERVAL_S, metavar="SECONDS",
                   help="live refresh period (default %gs)" % data.DEFAULT_INTERVAL_S)
    p.add_argument("--watch", nargs="?", const=-1, type=float, metavar="SECONDS",
                   help="alias for live mode; optional refresh period")
    p.add_argument("--command", help="override the `claude agents --json` command")
    p.add_argument("--sessions-dir")
    p.add_argument("--jobs-dir")
    p.add_argument("--status-dir")
    p.add_argument("--demo", action="store_true",
                   help="run on built-in synthetic data (fictional sessions in a temp folder)")
    return p


def _indexes(verb, rest):
    """The search and index subcommands (the `search` and `memory` extras)."""
    try:
        if verb == "search":
            from . import search_cmd
            return search_cmd.main(rest)
        from . import index_cli
        return index_cli.main(rest)
    except ImportError as e:
        print("fleetwatch %s needs the optional dependencies (%s); install fleetwatch[search,memory]"
              % (verb, e), file=sys.stderr)
        return 2


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--demo" not in argv:
        migrate.migrate_defaults()
    if argv[:1] == ["iterm"]:
        from . import overlay
        return overlay.main(argv[1:])
    if argv[:1] == ["write-status"]:
        from . import write_status
        return write_status.main(argv[1:])
    if argv[:1] == ["watch"]:
        from . import watches
        return watches.main(argv[1:])
    if argv[:1] == ["prune"]:
        from . import prune
        return prune.main(argv[1:])
    if argv[:1] == ["resume"]:
        from . import resume
        return resume.main(argv[1:])
    if argv[:1] == ["notify"]:
        from . import notify
        return notify.main(argv[1:])
    if argv[:1] == ["jump"]:
        from . import jump
        return jump.main(argv[1:])
    if argv[:1] == ["park"]:
        from . import park
        return park.main(argv[1:])
    if argv[:1] == ["unpark"]:
        from . import park
        return park.unpark_main(argv[1:])
    if argv[:1] in (["search"], ["index"]):
        return _indexes(argv[0], argv[1:])
    args = build_parser().parse_args(argv)
    if args.watch is not None and args.watch > 0:
        args.interval = args.watch
    args.interval = max(args.interval, 1.0)
    demo_dir = None
    if args.demo:
        from . import demo
        demo_dir, paths = demo.prepare()
    else:
        paths = data.default_paths()
    for key, val in (("command", args.command), ("sessions", args.sessions_dir),
                     ("jobs", args.jobs_dir), ("status", args.status_dir)):
        if val and not args.demo:
            paths[key] = val
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # pragma: no cover
        pass
    interactive = sys.stdout.isatty() and sys.stdin.isatty()
    if args.once or args.json or not interactive:
        from .render_text import snapshot
        print(snapshot(args.window, args.all, args.horizon, args.json, paths, args.groups))
        return 0
    from .app import run
    chosen = run(paths, args.window, args.horizon, args.interval)
    if chosen is not None:
        from . import resume
        # The gate repeats here: a session can have started since Enter was pressed.
        return resume.go(chosen, paths, True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
