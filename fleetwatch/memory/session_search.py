"""Search for session indexes: the engine's search plus `show` (read exchanges by id with
their neighbours) and `--around N` / `--full` on search results.

Library: ExchangeStore reads rendered exchanges (format in render.py), and search_around
runs a query and returns each hit with its neighbouring exchanges. main() is the CLI an
index directory's search.py calls:

    from fleetwatch.memory import session_search
    session_search.main(__file__, __doc__)

Exchange files live at corpus/session/YYYY-MM/YYYYMMDD/<id>.<sender>.md.
"""
import json
import os
import re
import resource
import sys
import threading
import time
from pathlib import Path

from fleetwatch.providers import EXCHANGE_ID_RE as ID_RE
from fleetwatch.search import cli as search_cli
from fleetwatch.search import engine as search_engine
from fleetwatch.search.errors import SearchError

from .render import HEADER_LINES, parse_exchange, parse_header_links

MEM_CAP = 4 << 30


def arm_memory_cap(limit=MEM_CAP):
    """macOS refuses RLIMIT_AS, so a watchdog enforces the cap on peak resident size.
    It ends the whole process, so only a CLI entry point may arm it."""
    scale = 1 if sys.platform == "darwin" else 1024

    def watch():
        while True:
            if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale > limit:
                sys.stderr.write("memory cap of %d GB reached; exiting\n" % (limit >> 30))
                os._exit(70)
            time.sleep(0.5)
    threading.Thread(target=watch, daemon=True).start()


class ExchangeStore:
    """The rendered exchanges of one session index, looked up by exchange id."""

    def __init__(self, here):
        self.here = Path(here)
        self.corpus = self.here / "corpus" / "session"
        self._files = None

    def exchange_files(self):
        if self._files is None:
            self._files = {}
            for pat in ("S*-EX-*.md", "S*-AQ-*.md", "EX-*.md", "AQ-*.md"):
                self._files.update({p.name.split(".")[0]: p for p in self.corpus.rglob(pat)})
        return self._files

    def read_exchange(self, ex_id):
        p = self.exchange_files().get(ex_id)
        if not p:
            return None
        meta = parse_exchange(p.read_text(), ex_id)
        meta["path"] = str(p.relative_to(self.here))
        # The CLI's JSON lists path second, as it always has.
        out = {"id": meta.pop("id"), "path": meta.pop("path")}
        for k, v in meta.items():
            out[k] = v
            if k == "local":
                # The local time was once keyed `mountain`; existing callers of `show --json`
                # and `--around --json` still read that key, so it stays as an alias.
                out["mountain"] = v
        return out

    def read_header(self, ex_id):
        p = self.exchange_files().get(ex_id)
        if not p:
            return None
        with open(p, encoding="utf-8") as fh:
            return parse_header_links([line for _, line in zip(range(HEADER_LINES), fh)])

    def neighbours(self, ex_id, n):
        """Walk the previous/next chain in the file headers (ids are per session, so numeric
        arithmetic would cross sessions)."""
        if not ID_RE.match(ex_id) or ex_id not in self.exchange_files():
            return []
        before, cur = [], ex_id
        for _ in range(n):
            m = self.read_header(cur)
            if not m or m.get("previous", "none") == "none":
                break
            cur = m["previous"]
            before.append(cur)
        after, cur = [], ex_id
        for _ in range(n):
            m = self.read_header(cur)
            if not m or m.get("next", "none") == "none":
                break
            cur = m["next"]
            after.append(cur)
        return list(reversed(before)) + [ex_id] + after


def exchange_of(path):
    """The exchange id a corpus path belongs to, or None for a reference document."""
    ex = Path(path).name.split(".")[0]
    return ex if ID_RE.match(ex) else None


def search_around(ix, store, query, opts, around):
    """Search, then collapse hits to one per exchange (or per document section) with the
    exchange's `around` neighbours on each side. Returns (groups, notes); each group is
    dict(result=<result dict>, exchange=<id or None>, around=[exchange metas])."""
    res = search_engine.search(ix, query, opts)
    seen, groups = set(), []
    for r in search_engine.result_dicts(res.results):
        ex = exchange_of(r["path"])
        key = ex or r["path"] + "#" + str(r.get("heading", ""))
        if key in seen:
            continue
        seen.add(key)
        metas = [store.read_exchange(x) for x in store.neighbours(ex, around)] if ex else []
        groups.append(dict(result=r, exchange=ex, around=metas))
    return groups, res.notes


def heartbeat_kind(here):
    """The index's own name for heartbeat prompts (the registry's kinds map), written by the
    refresh driver to state/parse_options.json."""
    try:
        return json.loads((here / "state" / "parse_options.json").read_text())["kinds"].get(
            "heartbeat", "heartbeat")
    except (OSError, ValueError, KeyError):
        return "heartbeat"


# ------------------------------------------------------------------------------ CLI

def clip(text, width):
    flat = re.sub(r"\s+", " ", text or "").strip()
    return flat if len(flat) <= width else flat[:width] + "..."


def print_exchange(meta, full, marker=" "):
    tprint = search_cli.tprint
    tprint("%s %s  %s  %s" % (marker, meta["id"], meta.get("local", "?"), meta.get("from", "?")))
    tprint("    summary: %s" % meta.get("summary", ""))
    tags = [t for t in meta.get("tags", []) if not t.startswith("sender:")]
    tprint("    tags: %s" % " ".join(tags))
    tprint("    tools: %s | session %s lines %s | tagged by %s"
           % (meta.get("tools", ""), meta.get("session", "")[:8], meta.get("source_lines", ""),
              meta.get("tagged_by", "?")))
    if meta.get("provider"):
        tprint("    source: %s%s" % (meta["provider"], " | folder: %s" % meta["folder"]
                                     if meta.get("folder") else ""))
    if full:
        tprint("\n    --- incoming ---\n" + "\n".join("    " + x for x in meta["incoming"].splitlines()))
        if meta["reply"]:
            tprint("\n    --- reply ---\n" + "\n".join("    " + x for x in meta["reply"].splitlines()))
    else:
        tprint("    in:    %s" % clip(meta["incoming"], 300))
        if meta["reply"]:
            tprint("    reply: %s" % clip(meta["reply"], 300))
    print()


def take_opt(argv, name, has_value):
    if name not in argv:
        return None
    i = argv.index(name)
    if has_value:
        val = argv[i + 1]
        del argv[i:i + 2]
        return val
    del argv[i]
    return True


def cmd_show(store, argv, around, full, as_json):
    ids = [a for a in argv if ID_RE.match(a)]
    if not ids:
        sys.exit("usage: search.py show Sa77396-EX-0123 [...] [--around N] [--full] [--json]")
    out = []
    for ex in ids:
        group = store.neighbours(ex, around) or [ex]
        metas = [store.read_exchange(x) for x in group]
        if as_json:
            out.append({"id": ex, "exchanges": [m for m in metas if m]})
            continue
        print("=" * 78)
        for m in metas:
            if m:
                print_exchange(m, full, ">" if m["id"] == ex else " ")
    if as_json:
        print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_search_around(ix, store, argv, around, full, as_json, doc):
    """Expand each hit's exchange to its neighbours. Hits in reference documents (no
    exchange id) are listed as plain results."""
    parser = search_cli.make_parser(ix, doc)
    args = parser.parse_args([a for a in argv if a != "--json"])
    if not args.query:
        parser.print_help()
        return
    groups, notes = search_around(ix, store, args.query, search_cli.options_from_args(args),
                                  around)
    search_cli.print_notes(notes, True)
    if as_json:
        print(json.dumps([dict(g["result"], exchange=g["exchange"], around=g["around"])
                          for g in groups], indent=2, ensure_ascii=False))
        return
    if not groups:
        print("No matches.")
        return
    for rank, g in enumerate(groups, start=1):
        r, ex = g["result"], g["exchange"]
        print("=" * 78)
        search_cli.tprint("#%d hit in %s%s (%s), score %s" % (
            rank, ex or r["path"], search_cli.provider_label(r), r["heading"], r["score"]))
        if ex:
            for m in g["around"]:
                if m:
                    print_exchange(m, full, ">" if m["id"] == ex else " ")
        else:
            search_cli.tprint("    " + clip(r.get("text", ""), 600))
    print("%d hits" % len(groups))


def main(wrapper_file, doc=None, argv=None):
    """Entry point for a session index's search.py. `argv` defaults to sys.argv[1:]."""
    arm_memory_cap()
    here = Path(wrapper_file).resolve().parent
    run(here, here, doc, argv)


def run(config_dir, data_dir, doc=None, argv=None, config=None):
    """Run the session-index CLI. `config_dir` holds probes.yaml, synonyms.yaml and (unless
    `config` supplies the raw dict) search_config.yaml; `data_dir` holds corpus/ and state/.
    They are one folder for an index with its own search.py."""
    search_cli.set_env_defaults()
    here, config_dir = Path(data_dir).resolve(), Path(config_dir).resolve()
    store = ExchangeStore(here)
    argv = list(sys.argv[1:] if argv is None else argv)
    around = take_opt(argv, "--around", True)
    full = bool(take_opt(argv, "--full", False))
    # Heartbeat prompts restate the same rules every firing and crowd out the exchange
    # where a rule was actually made, so a plain search leaves them out; --kind heartbeat
    # or --all-kinds brings them back. eval is not exempt: recorded session MRRs exclude
    # heartbeats.
    all_kinds = bool(take_opt(argv, "--all-kinds", False))
    commands = {"show", "stats", "build"}
    if (not all_kinds and argv and argv[0] not in commands
            and not any(a == "--kind" or a.startswith("--kind=") for a in argv)):
        argv += ["--kind", "!" + heartbeat_kind(here)]
    if argv and argv[0] == "show":
        as_json = bool(take_opt(argv, "--json", False))
        cmd_show(store, argv[1:], int(around or 0), full, as_json)
        return
    if around is not None or full:
        try:
            ix = search_engine.Index(config_dir, dict(config) if config is not None else None)
            cmd_search_around(ix, store, argv, int(around or 0), full, "--json" in argv, doc)
        except SearchError as e:
            sys.exit(str(e))
        return
    search_cli.main(config_dir, doc, argv, config=dict(config) if config is not None else None)
