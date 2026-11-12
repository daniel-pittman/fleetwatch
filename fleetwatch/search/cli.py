"""Command line for one search index: what an index directory's search.py runs.

    python search.py build                     rebuild the index
    python search.py "query"                   keyword search (FTS5 BM25, typo-corrected)
    python search.py "query" --similar         add semantic ranking and reranking
    python search.py stats | eval              what is indexed; probe-set accuracy

Query syntax is SQLite FTS5: bare words are ANDed, "quoted words" is a phrase,
and NOT / OR work as operators.

The printed output is a contract other sessions read, so its format matches the engine's
original CLI byte for byte. Library errors (SearchError) are printed and exit 1.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from . import engine
from .errors import NoProbesYet, SearchError

# The --help epilog when a wrapper passes no docstring of its own; kept word for word from
# the engine's original module docstring so help output is unchanged.
EPILOG = """Shared hybrid search engine for local text indexes (one engine, one config per index).

Each index directory holds a thin search.py that calls main(index_dir, doc). The index
directory supplies search_config.yaml, and optionally synonyms.yaml and probes.yaml.

    python search.py build                     rebuild the index
    python search.py "query"                   keyword search (FTS5 BM25, typo-corrected)
    python search.py "query" --similar         add semantic ranking and reranking
    python search.py stats | eval              what is indexed; probe-set accuracy

Query syntax is SQLite FTS5: bare words are ANDed, "quoted words" is a phrase,
and NOT / OR work as operators.
"""


def set_env_defaults():
    """Quiet tokenizers, for this process and its children. Whether models load offline is
    decided per index once its models are known (engine.prepare_hub)."""
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def print_notes(notes, as_json):
    """Side remarks go to stderr. In --json mode the typo note has no header to live in, so
    it is printed; the any-word note is left out of --json, as it always was."""
    for kind, text in notes:
        if kind == "any-word" and as_json:
            continue
        if kind == "typos" and not as_json:
            continue
        print(text, file=sys.stderr)


CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def terminal_safe(text):
    """Indexed text bound for a terminal. A document can hold escape sequences (ESC, CSI,
    OSC, including the OSC 52 clipboard write) that would act on the terminal, so every
    control character except newline and tab is shown as a \\xNN escape instead. JSON
    output needs none of this: json.dumps escapes control characters itself."""
    return CONTROL_RE.sub(lambda m: "\\x%02x" % ord(m.group(0)), str(text))


def tprint(*parts, **kw):
    print(*(terminal_safe(p) for p in parts), **kw)


def print_results(results, query, as_json, tag="rrf", header=None):
    if as_json:
        print(json.dumps(engine.result_dicts(results), indent=2))
        return
    if not results:
        print("No matches.")
        return
    if header:
        tprint(header + "\n")
    for i, (r, score) in enumerate(results, start=1):
        loc = "L%s" % r["level"] if r["level"] is not None else (r["region"] or "-")
        area = r["area"] or "-"
        head = r["heading_path"] or r["heading"] or r["file_title"]
        tprint("%2d. [%s %-7.4f] %-6s area %-4s %-8s %s"
               % (i, tag, score, loc, area, r["kind"], Path(r["path"]).name))
        tprint("    %s" % head)
        tprint("    %s" % engine.snippet(r["text"], query))
        if r.get("tags"):
            tprint("    tags: %s" % r["tags"].strip())
        if r.get("also_in"):
            others = json.loads(r["also_in"])
            tprint("    same text also in: %s"
                   % ", ".join(str(Path(Path(o).parent.name) / Path(o).name) for o in others[:3])
                   + (" and %d more" % (len(others) - 3) if len(others) > 3 else ""))
        print()
    print("%d result%s" % (len(results), "" if len(results) == 1 else "s"))


def cmd_build(ix, args):
    rep = engine.build(ix, progress=print)
    print("Indexed %d chunks in %.1fs" % (rep["chunks"], rep["elapsed"]))
    print("Vectors: %d windows, %d chunks needed more than one" % (rep["windows"], rep["multi"]))
    print("Backend: %s" % rep["backend"])
    print("Deduped: %d chunks dropped as copies of a higher priority source" % rep["deduped"])
    print("Index:   %s (%.1f MB)" % (rep["index_path"], rep["size_mb"]))
    print("\nBy source root:")
    for name, n in rep["per_root"].items():
        print("  %-18s %5d" % (name, n))
    print("\nBy kind:")
    for k, n in sorted(rep["by_kind"].items(), key=lambda kv: -kv[1]):
        print("  %-18s %5d" % (k, n))
    if rep["skipped_roots"]:
        print("\nRoots not present yet (they will be picked up once created):")
        for s in rep["skipped_roots"]:
            print("  %s" % s)


def cmd_search(ix, args):
    res = engine.search(ix, args.query, options_from_args(args))
    print_notes(res.notes, args.json)
    print_results(res.results, res.effective_query, args.json, tag=res.tag, header=res.header)


def cmd_stats(ix, args):
    cfg = ix.cfg
    con = ix.connect()
    get_meta = engine.get_meta
    total = con.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"]
    size = cfg["_index_path"].stat().st_size / 1e6
    print("Index:   %s" % cfg["_index_path"])
    print("Built:   %s" % get_meta(con, "built", "unknown"))
    print("Backend: %s" % get_meta(con, "backend", "unknown"))
    print("Model:   %s" % get_meta(con, "model", "n/a (TF-IDF)"))
    print("Rerank:  %s" % (cfg["similarity"].get("reranker") or "off"))
    print("Deduped: %s duplicate chunks dropped at build time"
          % get_meta(con, "deduped", "unknown"))
    try:
        nwords = con.execute("SELECT COUNT(*) n FROM words").fetchone()["n"]
        print("Typos:   corrected against %d corpus words (--exact to disable)" % nwords)
    except sqlite3.OperationalError:
        print("Typos:   correction off; this index predates it (python search.py build)")
    print("Chunks:  %d  (%.1f MB on disk)\n" % (total, size))
    for label, sql in (
            ("By source root", "SELECT root k, COUNT(*) n FROM chunks GROUP BY root ORDER BY n DESC"),
            ("By kind", "SELECT kind k, COUNT(*) n FROM chunks GROUP BY kind ORDER BY n DESC"),
            ("By %s" % cfg["_label_level"], "SELECT COALESCE(CAST(level AS TEXT), '(none)') k, COUNT(*) n "
                                 "FROM chunks GROUP BY level ORDER BY level"),
            ("By region", "SELECT COALESCE(region, '(none)') k, COUNT(*) n FROM chunks "
                          "GROUP BY region ORDER BY n DESC LIMIT 15")):
        print("%s:" % label)
        for r in con.execute(sql):
            print("  %-22s %5d" % (r["k"], r["n"]))
        print()
    # Tag counts are per document (distinct path), since every chunk of a file carries its tags.
    try:
        counts = {}
        for r in con.execute("SELECT DISTINCT path, tags FROM chunks WHERE tags IS NOT NULL"):
            for t in r["tags"].split():
                counts[t] = counts.get(t, 0) + 1
    except sqlite3.OperationalError:
        counts = {}
    if counts:
        facets = {}
        for t, n in counts.items():
            facets.setdefault(t.split(":", 1)[0] if ":" in t else "(none)", []).append((t, n))
        for facet, items in sorted(facets.items()):
            print("Tags, %s (documents):" % facet)
            for t, n in sorted(items, key=lambda kv: -kv[1])[:20]:
                print("  %-40s %5d" % (t, n))
            print()
    con.close()


def cmd_eval(ix, args):
    """Run the fixed probe set and report rank of the expected answer plus MRR. A new
    index's empty probe list is not a failure: there is nothing to score yet."""
    try:
        ev = engine.evaluate(ix, options_from_args(args), typos=args.typos)
    except NoProbesYet as e:
        print(e)
        return
    print("Evaluating %d probes: %s\n" % (ev["probes"], ev["label"]))
    if ev["typos"]:
        print("Typo mode: each probe misspelled by dropping a middle letter of its two longest words.\n")
    print_notes(ev["notes"], False)
    rows = ev["rows"]
    width = max(len(r[0]) for r in rows)
    print("%-*s  %6s  %8s" % (width, "PROBE", "RANK", "SECONDS"))
    print("%s  %s  %s" % ("-" * width, "-" * 6, "-" * 8))
    for q, rank, elapsed in rows:
        print("%-*s  %6s  %8.2f" % (width, q, rank if rank else "miss", elapsed))
    print("\nMRR %.3f over %d probes, %d found, %.1fs total, %.2fs per query"
          % (ev["mrr"], ev["probes"], ev["found"], ev["total_s"], ev["total_s"] / ev["probes"]))


def options_from_args(args):
    return engine.SearchOptions(
        similar=args.similar, level=args.level, kind=args.kind, path=args.path, tag=args.tag,
        limit=args.limit, no_rerank=args.no_rerank, expand=args.expand,
        no_expand=args.no_expand, exact=args.exact)


def make_parser(ix, doc=None):
    cfg = ix.cfg
    desc = (cfg.get("labels") or {}).get("description") or "Search the local %s index." % ix.name
    p = argparse.ArgumentParser(
        description=desc,
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=doc or EPILOG)
    p.add_argument("query", nargs="?",
                   help='search text, or "build", "stats" or "eval"')
    p.add_argument("--similar", action="store_true",
                   help="blend semantic or TF-IDF ranking with keyword ranking")
    p.add_argument("--level", type=int, help=cfg["_label_level_help"])
    p.add_argument("--kind", help=cfg["_label_kinds"] or "chunk kind, from kind_rules in the config")
    p.add_argument("--path", help="substring the file path must contain")
    p.add_argument("--tag", action="append",
                   help="only documents carrying this tag (repeatable, all must match): "
                        "facet:value exact, bare value under any facet, trailing * for prefix")
    p.add_argument("--limit", type=int, default=10, help="maximum results (default 10)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--no-rerank", action="store_true",
                   help="skip the cross-encoder reranking stage")
    p.add_argument("--expand", action="store_true",
                   help="also search curated synonyms from synonyms.yaml, at lower weight")
    p.add_argument("--typos", action="store_true",
                   help="eval only: misspell every probe to measure typo tolerance")
    p.add_argument("--exact", action="store_true",
                   help="do not correct words that appear nowhere in the corpus")
    p.add_argument("--no-expand", action="store_true",
                   help="never expand, even if expand_by_default is set in the config")
    return p


def run(ix, args, parser):
    if args.query == "build":
        cmd_build(ix, args)
    elif args.query == "stats":
        cmd_stats(ix, args)
    elif args.query == "eval":
        cmd_eval(ix, args)
    elif not args.query:
        parser.print_help()
    else:
        cmd_search(ix, args)


def main(index_dir, doc=None, argv=None, config=None):
    """Entry point for an index directory's search.py wrapper. `argv` defaults to
    sys.argv[1:]. `config` is a raw search_config dict used instead of the directory's
    search_config.yaml (the directory still supplies probes.yaml and synonyms.yaml)."""
    set_env_defaults()
    try:
        ix = engine.Index(index_dir, config)
        parser = make_parser(ix, doc)
        run(ix, parser.parse_args(argv), parser)
    except SearchError as e:
        sys.exit(str(e))
