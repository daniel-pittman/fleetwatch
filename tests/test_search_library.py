"""fleetwatch.search as a library: per-index state, errors instead of exits, the TF-IDF backend,
and a ranking smoke test on a tiny synthetic corpus. No network and no model download: every
index here is built with `similarity.backend: tfidf` and no reranker."""
import contextlib
import hashlib
import inspect
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from fleetwatch.search import cli, engine
from fleetwatch.search.errors import (EmbedderUnavailable, EmbeddingFailed, EmptyCorpus, IndexMissing, IndexStale,
                                 ProbesMissing, SearchError)

REPO = Path(__file__).resolve().parent.parent

CONFIG = textwrap.dedent("""\
    corpus_root: "{root}"
    index_path: "index/{name}.sqlite"
    level_folder_pattern: "^(\\\\d{{8}})()$"
    chunking: {{max_chars: 1500, min_chars: 25}}
    similarity:
      backend: tfidf
      model: none
      max_features: 5000
      window_chars: 1200
      window_overlap: 300
      rrf_k: 60
    dedupe: {{enabled: false}}
    tag_line_prefix: "Tags:"
    roots:
      - {{name: notes, path: corpus/notes, default_kind: note}}
    """)

# A fabricated corpus: each file is about one thing, so the expected top hits are plain.
GARDEN = {
    "tomatoes.md": "# Growing tomatoes\n\nTags: area:garden topic:tomato\n\n"
                   "Tomatoes need full sun, deep watering and a sturdy cage. Pinch the suckers "
                   "so the plant puts its energy into fruit. Tomato blight spreads in wet weather.",
    "compost.md": "# Compost heap\n\nTags: area:garden topic:soil\n\n"
                  "A compost heap wants green and brown layers, air and moisture. Turn the heap "
                  "every week; finished compost smells of earth and feeds the soil.",
    "lantern.md": "# Garden lantern\n\nTags: area:lighting\n\n"
                  "The lantern by the gate runs on a small solar panel. Replace the lantern "
                  "battery each spring. A lantern with a warm bulb draws fewer moths.",
    "irrigation.md": "# Drip irrigation\n\nTags: area:garden topic:water\n\n"
                     "Drip irrigation lines water the beds slowly at the roots. Flush the drip "
                     "lines before winter and check each emitter for clogs.",
    "bees.md": "# Bee hotel\n\nTags: area:wildlife\n\n"
               "Solitary bees nest in hollow stems. Hang the bee hotel facing south, out of "
               "the rain, and leave the stems in place over winter.",
}
HARBOR = {
    "ferry.md": "# Ferry timetable\n\nThe harbor ferry leaves every forty minutes. The ferry "
                "crossing takes twenty minutes in calm water and longer in a swell.",
    "lighthouse.md": "# Lighthouse keeper\n\nThe lighthouse keeper logs the weather at dawn. "
                     "The lighthouse lamp turns once every ten seconds.",
    "nets.md": "# Mending nets\n\nFishing nets are mended on the quay. A torn net loses the "
               "catch, so each net is checked after every trip.",
}


def make_index(base, name, files, synonyms=None, probes=None):
    d = Path(base) / name
    (d / "corpus" / "notes").mkdir(parents=True)
    for fn, text in files.items():
        (d / "corpus" / "notes" / fn).write_text(text)
    (d / "search_config.yaml").write_text(CONFIG.format(root=d, name=name))
    if synonyms:
        (d / "synonyms.yaml").write_text("groups:\n" + "".join(
            "  - [%s]\n" % ", ".join(g) for g in synonyms))
    if probes:
        (d / "probes.yaml").write_text(probes)
    return d


def top_files(res, n=3):
    return [Path(r["path"]).name for r, _ in res.results[:n]]


class SyntheticIndexes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.garden_dir = make_index(cls.tmp.name, "garden", GARDEN,
                                    synonyms=[["lantern", "lamp"]])
        cls.harbor_dir = make_index(cls.tmp.name, "harbor", HARBOR)
        cls.garden = engine.Index(cls.garden_dir)
        cls.harbor = engine.Index(cls.harbor_dir)
        cls.garden_report = cls.garden.build()
        cls.harbor.build()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_build_report_and_backend(self):
        rep = self.garden_report
        self.assertEqual(rep["backend"], "tfidf")
        self.assertEqual(rep["chunks"], 5)
        self.assertEqual(rep["per_root"], {"notes": 5})
        self.assertTrue(Path(rep["index_path"]).exists())

    def test_ranking_smoke_keyword(self):
        # Pinned top results, the same as the engine gave before it became a library. A
        # change here means ranking changed: confirm it is intended.
        self.assertEqual(top_files(self.garden.search("compost heap"), 1), ["compost.md"])
        self.assertEqual(top_files(self.garden.search("winter"), 2), ["bees.md", "irrigation.md"])
        res = self.garden.search("blight weather")
        self.assertEqual(res.tag, "bm25")
        self.assertEqual(top_files(res, 1), ["tomatoes.md"])

    def test_ranking_smoke_hybrid(self):
        res = self.garden.search("how do I water the beds before winter", similar=True)
        self.assertEqual(res.tag, "rrf ")
        self.assertEqual(top_files(res, 3), ["irrigation.md", "bees.md", "tomatoes.md"])
        self.assertEqual(res.header, "model none, no reranker")
        res = self.garden.search("solar battery for the gate light", similar=True)
        self.assertEqual(top_files(res, 1), ["lantern.md"])

    def test_filters(self):
        res = self.garden.search("winter", tag=["area:wildlife"])
        self.assertEqual(top_files(res), ["bees.md"])
        res = self.garden.search("winter", kind="!note")
        self.assertEqual(res.results, [])

    def test_two_indexes_keep_their_own_vocabulary(self):
        # Alternate between the indexes: each corrects against its own corpus words only.
        for _ in range(2):
            g = self.garden.search("lantren")
            self.assertEqual(top_files(g, 1), ["lantern.md"])
            self.assertIn("corrected: lantren -> lantern", g.header)
            h = self.harbor.search("lantren")
            self.assertEqual(h.results, [])
            self.assertIsNone(h.header)
            h = self.harbor.search("lighthuose")
            self.assertIn("corrected: lighthuose -> lighthouse", h.header)
            self.assertEqual(top_files(h, 1), ["lighthouse.md"])
            g = self.garden.search("lighthuose")
            self.assertEqual(g.results, [])
        self.assertIn("lantern", self.garden.words(self.garden.connect()))
        self.assertNotIn("lantern", self.harbor.words(self.harbor.connect()))

    def test_two_indexes_keep_their_own_synonyms(self):
        g = self.garden.search("lamp", similar=True, expand=True)
        self.assertIn("also searched: lantern", g.header)
        self.assertEqual(top_files(g, 1), ["lantern.md"])
        h = self.harbor.search("lamp", similar=True, expand=True)
        self.assertNotIn("also searched", h.header or "")
        self.assertEqual(top_files(h, 1), ["lighthouse.md"])
        self.assertEqual(self.harbor.synonyms(), {})

    def test_notes_are_returned_not_printed(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            res = self.garden.search("compost moths")
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        self.assertIn("any-word", [k for k, _ in res.notes])

    def test_eval(self):
        d = make_index(self.tmp.name, "garden-eval", GARDEN, probes=textwrap.dedent("""\
            probes:
              - {query: "compost heap layers", expect: {path_contains: compost}}
              - {query: "emitter clogs", expect: {path_contains: irrigation}}
            """))
        ix = engine.Index(d)
        ix.build()
        ev = ix.evaluate()
        self.assertEqual((ev["probes"], ev["found"], round(ev["mrr"], 3)), (2, 2, 1.0))
        with self.assertRaises(ProbesMissing):
            self.harbor.evaluate()

    def test_cli_output_and_json(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(self.garden_dir, None, ["compost heap", "--limit", "1"])
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith(" 1. [bm25 "), lines[0])
        self.assertTrue(lines[0].endswith("compost.md"), lines[0])
        self.assertEqual(lines[1], "    Compost heap")
        self.assertEqual(lines[-1], "1 result")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(self.garden_dir, None, ["compost heap", "--limit", "1", "--json"])
        import json
        rows = json.loads(out.getvalue())
        self.assertEqual([r["path"] for r in rows], ["corpus/notes/compost.md"])
        self.assertEqual(rows[0]["tags"], ["area:garden", "topic:soil"])


class Errors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_missing_index_raises(self):
        ix = engine.Index(make_index(self.tmp.name, "empty", HARBOR))
        with self.assertRaises(IndexMissing) as cm:
            ix.search("ferry")
        self.assertTrue(str(cm.exception).startswith("No index at "))

    def test_cli_exits_with_the_message(self):
        d = make_index(self.tmp.name, "empty", HARBOR)
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
            cli.main(d, None, ["ferry"])
        self.assertTrue(str(cm.exception.code).startswith("No index at "))

    def test_empty_corpus(self):
        ix = engine.Index(make_index(self.tmp.name, "nothing", {}))
        with self.assertRaises(EmptyCorpus):
            ix.build()

    def test_stale_index(self):
        ix = engine.Index(make_index(self.tmp.name, "stale", HARBOR))
        ix.build()
        con = engine.sqlite3.connect(ix.cfg["_index_path"])
        con.execute("UPDATE meta SET value='0' WHERE key='version'")
        con.commit()
        con.close()
        with self.assertRaises(IndexStale):
            ix.search("ferry")

    def test_no_silent_tfidf_fallback(self):
        d = make_index(self.tmp.name, "st", HARBOR)
        cfg = (d / "search_config.yaml").read_text().replace("backend: tfidf",
                                                              "backend: sentence-transformers")
        (d / "search_config.yaml").write_text(cfg)
        ix = engine.Index(d)
        with mock.patch.object(engine, "find_embedder", return_value=None):
            with self.assertRaises(EmbedderUnavailable):
                ix.build()
        self.assertFalse(ix.cfg["_index_path"].exists())
        self.assertFalse(ix.cfg["_index_path"].with_suffix(".sqlite.tmp").exists())

    def test_backend_defaults_to_sentence_transformers(self):
        d = make_index(self.tmp.name, "default", HARBOR)
        cfg = (d / "search_config.yaml").read_text().replace("  backend: tfidf\n", "")
        (d / "search_config.yaml").write_text(cfg)
        ix = engine.Index(d)
        self.assertEqual(ix.cfg["_backend"], "sentence-transformers")
        with mock.patch.object(engine, "find_embedder", return_value=None):
            with self.assertRaises(SearchError):
                ix.build()

    def test_library_has_no_exits(self):
        for mod in (engine,):
            src = inspect.getsource(mod)
            self.assertNotIn("sys.exit", src.replace('sys.argv[1], sys.argv[2]', ''))
            self.assertNotIn("os._exit", src)
            self.assertNotIn("print(", src)


class ImportHasNoSideEffects(unittest.TestCase):
    def test_env_untouched_at_import(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("HF_HUB_OFFLINE", "TOKENIZERS_PARALLELISM")}
        env["PYTHONPATH"] = str(REPO)
        code = ("import os, fleetwatch.search.engine, fleetwatch.search.cli, fleetwatch.memory.session_search; "
                "print(os.environ.get('HF_HUB_OFFLINE'), os.environ.get('TOKENIZERS_PARALLELISM'))")
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "None None")

    def test_cli_sets_env_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TOKENIZERS_PARALLELISM", None)
            cli.set_env_defaults()
            self.assertEqual(os.environ["TOKENIZERS_PARALLELISM"], "false")


class LibraryConfigDefaults(unittest.TestCase):
    def test_optional_keys_take_defaults_and_required_ones_say_what_is_missing(self):
        base = {"corpus_root": "/tmp/x", "index_path": "i.sqlite", "roots": [{"name": "a", "path": "."}],
                "similarity": {"backend": "tfidf"}}
        cfg = engine.resolve_config(dict(base))
        self.assertEqual(cfg["chunking"], engine.CHUNKING_DEFAULTS)
        self.assertEqual(cfg["similarity"]["rrf_k"], 60)
        self.assertEqual(engine.path_metadata(("2026", "x.md"), cfg, "x"), (None, "2026"))
        with self.assertRaises(engine.ConfigError) as cm:
            engine.resolve_config({k: v for k, v in base.items() if k != "index_path"})
        self.assertIn("index_path", str(cm.exception))
        self.assertEqual(engine.resolve_config(dict(base, similarity={}))["similarity"]["model"],
                         "sentence-transformers/all-MiniLM-L6-v2")


class FirstRunModels(unittest.TestCase):
    """Models load offline once they are cached; a fresh install may download them once."""
    CFG = {"_backend": "sentence-transformers",
           "similarity": {"model": "all-MiniLM-L6-v2", "reranker": "cross-encoder/ms-marco-MiniLM-L-6-v2"}}

    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.hf = Path(t.name)
        env = mock.patch.dict(os.environ, {"HF_HOME": str(self.hf)})
        env.start()
        self.addCleanup(env.stop)
        for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_CACHE"):
            os.environ.pop(k, None)
        getattr(engine, "_HUB_NOTICE", []).clear()

    def cache(self, mid):
        snap = self.hf / "hub" / ("models--" + mid.replace("/", "--")) / "snapshots" / "abc"
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}")

    def test_an_empty_cache_allows_the_download_once_and_says_so(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            engine.prepare_hub(self.CFG)
            engine.prepare_hub(self.CFG)
        self.assertNotIn("HF_HUB_OFFLINE", os.environ)
        self.assertEqual(err.getvalue().count("not downloaded yet"), 1)

    def test_cached_models_load_offline(self):
        self.cache("sentence-transformers/all-MiniLM-L6-v2")
        self.cache("cross-encoder/ms-marco-MiniLM-L-6-v2")
        engine.prepare_hub(self.CFG)
        self.assertEqual(os.environ.get("HF_HUB_OFFLINE"), "1")

    def test_one_missing_model_still_allows_the_download(self):
        self.cache("sentence-transformers/all-MiniLM-L6-v2")
        with mock.patch.object(sys, "stderr", io.StringIO()):
            engine.prepare_hub(self.CFG)
        self.assertNotIn("HF_HUB_OFFLINE", os.environ)

    def test_the_callers_setting_and_tfidf_are_left_alone(self):
        os.environ["HF_HUB_OFFLINE"] = "0"
        engine.prepare_hub(self.CFG)
        self.assertEqual(os.environ["HF_HUB_OFFLINE"], "0")
        del os.environ["HF_HUB_OFFLINE"]
        engine.prepare_hub(dict(self.CFG, _backend="tfidf"))
        self.assertNotIn("HF_HUB_OFFLINE", os.environ)

    def test_the_encoder_child_inherits_the_decision(self):
        if engine.np is None:
            self.skipTest("numpy unavailable")
        seen = {}

        def run(argv, **kw):
            seen.update(kw["env"])
            return subprocess.CompletedProcess(argv, 1, b"", b"no")
        with mock.patch.object(engine.subprocess, "run", run), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(EmbeddingFailed):
                engine.encode_texts(self.CFG, ["x"], ("subprocess", sys.executable))
        self.assertNotIn("HF_HUB_OFFLINE", seen)


class CorpusWalk(unittest.TestCase):
    """The engine indexes only the corpus: never a file under a hidden folder (.git, .trash,
    .claude), and never a file whose real path lies outside the corpus root."""

    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.base = Path(t.name)
        self.d = make_index(self.base, "walk", {"kept.md": GARDEN["tomatoes.md"]})
        self.notes = self.d / "corpus" / "notes"

    def files(self):
        chunks, _, _ = engine.collect_chunks(engine.load_config(self.d))
        return sorted({Path(c["path"]).name for c in chunks})

    def test_a_file_under_a_hidden_folder_is_skipped(self):
        for hidden in (".git", ".trash", ".claude/sub"):
            (self.notes / hidden).mkdir(parents=True)
            (self.notes / hidden / "inside.md").write_text(HARBOR["ferry.md"])
        self.assertEqual(self.files(), ["kept.md"])

    def test_a_link_to_a_file_outside_the_corpus_root_is_skipped(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "private.md").write_text(HARBOR["nets.md"])
        (self.notes / "linked.md").symlink_to(outside / "private.md")
        (self.notes / "same.md").symlink_to(self.notes / "kept.md")
        self.assertEqual(self.files(), ["kept.md", "same.md"])


class RankingConstants(unittest.TestCase):
    """Ranking constants are fixed by recorded MRR baselines; a change re-baselines every
    index. They may move, but not change value."""

    def test_values(self):
        self.assertEqual((engine.RARE_DF, engine.RARE_RATIO, engine.RARE_MIN_DF), (5, 50, 10))
        self.assertEqual(engine.INDEX_VERSION, "3")
        self.assertEqual(len(engine.STOPWORDS), 66)
        self.assertEqual(hashlib.sha1(" ".join(sorted(engine.STOPWORDS)).encode()).hexdigest()[:12],
                         STOPWORDS_SHA)
        self.assertIn("bm25(chunks_fts, 8.0, 4.0, 1.0)", inspect.getsource(engine.bm25_search))
        src = inspect.getsource(engine.run_query)
        self.assertIn('get("rerank_pool", 50) if want_rerank else max(limit * 3, 30)', src)
        self.assertIn("nearest_word(w, words, 1 if len(w) <= 6 else 2)",
                      inspect.getsource(engine.correct_typos))
        self.assertIn("if len(w) < 4 or w in STOPWORDS", inspect.getsource(engine.correct_typos))


STOPWORDS_SHA = "b031a6fbedcc"


if __name__ == "__main__":
    unittest.main()
