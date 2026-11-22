"""`fleetwatch search` and `fleetwatch index`: the calling session's index, the trust guard,
add, migrate, verify and schedule, on synthetic registries and indexes (TF-IDF backend, no
model downloads)."""
import contextlib
import hashlib
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import yaml

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")

from fleetwatch import index_cli, index_migrate, indexes, schedule, search_cmd, settings
from fleetwatch.memory import refresh
from fleetwatch.search import engine

REPO = Path(__file__).resolve().parent.parent
SID = "aa11bb22-0000-4000-8000-000000000001"
SID2 = "cc33dd44-0000-4000-8000-000000000002"

TAGS = {"description": "a test session", "areas": {"a": "area a"}, "kinds": {"k": "kind k"}}
PACK = textwrap.dedent("""\
    id_patterns: ['\\b9\\d{8}\\b']
    anon_ids: [{pattern: 'ANON-\\d+', pairing: 'for (\\w+)'}]
    """)
SIM = {"backend": "tfidf", "model": "none", "max_features": 5000, "window_chars": 1200,
       "window_overlap": 300, "rrf_k": 60}


def old_config(name, entry, root, drop=()):
    """What an old index's hand-copied search_config.yaml looks like: the generated config with
    the TF-IDF backend and a few hand edits."""
    cfg = indexes.session_config(name, entry, refresh.SETTINGS)
    cfg["corpus_root"] = str(root)
    cfg["similarity"] = dict(SIM)
    cfg["kind_rules"] = cfg["kind_rules"][:2]
    cfg["labels"] = dict(cfg["labels"], description="Search the demo.")
    for k in drop:
        cfg.pop(k)
    return yaml.safe_dump(cfg, sort_keys=False)


PROBES = {"probes": [{"query": "lantern battery", "expect": {"path_contains": "Saa11bb-EX-0001"}}]}


PROBES_TEMPLATE_EMPTY = "# probes for eval\nprobes: []\n"


def tmp(test):
    d = tempfile.TemporaryDirectory()
    test.addCleanup(d.cleanup)
    return Path(d.name).resolve()


def use_layout(test, cfg, data_dir, projects=None, packs=("education",), protected=("ferpa",)):
    """Point the refresh module at a synthetic config and data folder for one test."""
    raw = {"paths": {"data_dir": str(data_dir), "projects_dir": str(projects or cfg / "projects")},
           "scanner": {"rule_packs": list(packs)}, "groups": {"protected": list(protected)}}
    old = refresh.SETTINGS
    test.addCleanup(refresh.configure, old)
    refresh.configure(settings.indexer(raw=raw, cfg_dir=cfg))


def write_registry(root, groups=None, entries=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / "registry.yaml").write_text(yaml.safe_dump({
        "defaults": {"tag": True, "tag_budget_per_run": 0},
        "groups": groups or {"oss": {"readers": ["oss", "research"]},
                             "research": {"readers": ["research"]},
                             "ferpa": {"readers": []}, "global": {"readers": ["*"]}}}))
    (root / "registry.d").mkdir(exist_ok=True)
    for name, e in (entries or {}).items():
        (root / "registry.d" / (name + ".yaml")).write_text(yaml.safe_dump({"indexes": {name: e}}))


def make_old_index(root, name, group="research", owner="Demo Session", drop=()):
    """An index in the old single-folder layout, built with the TF-IDF backend; returns its
    registry entry."""
    d = root / name
    day = d / "corpus" / "session" / "2026-04" / "20260401"
    day.mkdir(parents=True)
    (day / "Saa11bb-EX-0001.owner.md").write_text(
        "# Saa11bb-EX-0001\n\nWhy is the gate lantern dim? The lantern battery is flat; swap "
        "the cell.\n\nTags: area:a kind:k\n")
    (day / "Saa11bb-EX-0002.owner.md").write_text(
        "# Saa11bb-EX-0002\n\nThe seed tray holds twelve cells and sits by the greenhouse door.\n\n"
        "Tags: area:a kind:k\n")
    (d / "state" / "sessions").mkdir(parents=True)
    (d / "state" / "sessions" / "x.jsonl").write_text("{}\n{}\n")
    (d / "state" / "tags.jsonl").write_text("{}\n{}\n")
    (d / "scan_allowlist.yaml").write_text("version: 1\n")
    (d / "tags.yaml").write_text(yaml.safe_dump(TAGS))
    (d / "probes.yaml").write_text(yaml.safe_dump(PROBES))
    entry = {"group": group, "owner_session": owner,
             "transcripts": {"project_dir": "~/nowhere", "sessions": "all"},
             "privacy": {"allowlist": "%s/scan_allowlist.yaml" % name}, "tags": "%s/tags.yaml" % name}
    (d / "search_config.yaml").write_text(old_config(name, entry, d, drop))
    (d / "search.py").write_text("print('wrapper')\n")
    engine.build(engine.Index(d))
    return entry


def tree_digest(root):
    h = hashlib.sha256()
    for p in sorted(Path(root).rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def capture(fn, *a, **kw):
    out, err = io.StringIO(), io.StringIO()
    code = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = fn(*a, **kw)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------- settings

class ConfigRoot(unittest.TestCase):
    def test_default_splits_config_from_data(self):
        base = tmp(self)
        s = settings.indexer(raw={"paths": {"data_dir": str(base / "d")}}, cfg_dir=base / "c")
        self.assertEqual(s.config_root, base / "c" / "indexes")
        self.assertEqual(s.index_root, base / "d" / "indexes")

    def test_an_explicit_index_root_alone_is_one_root(self):
        base = tmp(self)
        s = settings.indexer(raw={"paths": {"index_root": str(base / "one")}}, cfg_dir=base / "c")
        self.assertEqual(s.config_root, base / "one")

    def test_both_can_be_set(self):
        base = tmp(self)
        s = settings.indexer(raw={"paths": {"index_root": str(base / "a"),
                                            "index_config_root": str(base / "b")}}, cfg_dir=base / "c")
        self.assertEqual((s.index_root, s.config_root), (base / "a", base / "b"))


# -------------------------------------------------------------------- session and guard

REG = {
    "groups": {"oss": {"readers": ["oss", "research"]}, "research": {"readers": ["research"]},
               "ferpa": {"readers": []}, "global": {"readers": ["*"]}},
    "indexes": {
        "mine-session": {"group": "oss", "owner_session": "Mine", "owner_session_id": SID},
        "other-oss": {"group": "oss", "owner_session": "Other"},
        "lab": {"group": "research", "owner_session": "Lab"},
        "student": {"group": "ferpa", "owner_session": "Student"},
        "open": {"group": "global", "owner_session": "Open"},
        "invite": {"group": "research", "owner_session": "Inv", "extra_readers": ["Mine"]},
        "invite-id": {"group": "research", "owner_session": "Inv2", "extra_readers": [SID]},
        "ghost": {"group": "undeclared", "owner_session": "Ghost"},
    }}


class TrustGuard(unittest.TestCase):
    def refusal(self, target, session=("Mine", SID)):
        return indexes.read_refusal(REG, session, target)

    def test_outside_a_session_everything_is_allowed(self):
        for t in REG["indexes"]:
            self.assertIsNone(self.refusal(t, None))

    def test_own_index_and_a_group_that_lists_the_callers_group(self):
        self.assertIsNone(self.refusal("mine-session"))
        self.assertIsNone(self.refusal("other-oss"))

    def test_a_group_that_does_not_list_the_callers_group_is_refused_with_the_rule(self):
        why = self.refusal("lab")
        self.assertIn("refused", why)
        self.assertIn("group 'research' allows readers research", why)
        self.assertIn("your own index is in group oss", why)
        self.assertIn("extra_readers", why)

    def test_ferpa_with_no_readers_refuses_everyone_but_its_owner(self):
        self.assertIn("protected group", self.refusal("student"))
        self.assertIsNone(indexes.read_refusal(REG, ("Student", "zz"), "student"))

    def test_a_protected_group_ignores_its_readers_list(self):
        for readers in (["*"], ["ferpa"], ["oss"]):
            reg = dict(REG, groups=dict(REG["groups"], ferpa={"readers": readers}),
                       indexes=dict(REG["indexes"], **{
                           "mine-ferpa": {"group": "ferpa", "owner_session": "Mine2"},
                           "student-ok": {"group": "ferpa", "owner_session": "Student2",
                                          "extra_readers": ["Mine"]}}))
            why = indexes.read_refusal(reg, ("Mine", SID), "student")
            self.assertIn("protected group", why)
            # another ferpa index's session is not a reader either
            self.assertIn("protected group", indexes.read_refusal(reg, ("Mine2", "zz"), "student"))
            self.assertIsNone(indexes.read_refusal(reg, ("Student", "zz"), "student"))
            self.assertIsNone(indexes.read_refusal(reg, ("Mine", SID), "student-ok"))

    def test_protected_groups_come_from_settings_and_match_without_case(self):
        reg = dict(REG, groups=dict(REG["groups"], FERPA={"readers": ["*"]}),
                   indexes={"s": {"group": "FERPA", "owner_session": "S"}})
        self.assertIn("protected group", indexes.read_refusal(reg, ("Mine", SID), "s"))
        self.assertIsNone(indexes.read_refusal(reg, ("Mine", SID), "s", protected=frozenset()))

    def test_wildcard_readers_allow_everyone(self):
        self.assertIsNone(self.refusal("open"))
        self.assertIsNone(self.refusal("open", ("Nobody", "zz")))

    def test_extra_readers_by_name_or_id(self):
        self.assertIsNone(self.refusal("invite"))
        self.assertIsNone(self.refusal("invite-id"))
        self.assertIn("refused", self.refusal("invite", ("Someone", "zz")))

    def test_an_undeclared_group_has_no_readers(self):
        self.assertIn("not declared", self.refusal("ghost"))

    def test_a_session_with_no_index_reads_only_wildcard_or_invited(self):
        self.assertIn("no index of its own", self.refusal("other-oss", ("Nobody", "zz")))

    def test_own_index_prefers_the_session_id_and_reports_none_or_several(self):
        self.assertEqual(indexes.own_index(REG, ("Renamed", SID)), "mine-session")
        self.assertEqual(indexes.own_index(REG, ("Lab", "q")), "lab")
        with self.assertRaises(indexes.IndexesError):
            indexes.own_index(REG, ("Nobody", "q"))
        with self.assertRaises(indexes.IndexesError):
            indexes.own_index(REG, None)
        two = {"indexes": {"a": {"owner_session": "S"}, "b": {"owner_session": "S"}}}
        with self.assertRaises(indexes.IndexesError):
            indexes.own_index(two, ("S", "q"))


class CallingSession(unittest.TestCase):
    def test_found_from_ancestry_and_none_in_a_plain_terminal(self):
        base = tmp(self)
        (base / "500.json").write_text(json.dumps({"name": "Mine", "sessionId": SID}))
        paths = {"sessions": str(base)}
        self.assertEqual(indexes.calling_session(paths, {600: 500, 500: 1}, 600), ("Mine", SID))
        self.assertIsNone(indexes.calling_session(paths, {600: 1}, 600))

    def test_an_unreadable_process_table_is_an_error_not_a_pass(self):
        class Broken(dict):
            unreadable = True
        with self.assertRaises(indexes.IndexesError):
            indexes.calling_session({"sessions": str(tmp(self))}, Broken(), 600)


# --------------------------------------------------------------------- search, end to end

class SearchCommand(unittest.TestCase):
    def setUp(self):
        base = tmp(self)
        self.cfg, self.data = base / "config", base / "data"
        use_layout(self, self.cfg, self.data)
        self.old = base / "old"
        write_registry(self.old, entries={
            "demo-session": make_old_index(self.old, "demo-session", group="research"),
            "mine-session": make_old_index(self.old, "mine-session", group="oss", owner="Mine")})

    def migrate_both(self):
        for n in ("demo-session", "mine-session"):
            code, out, err = capture(index_migrate.migrate, n, str(self.old))
            self.assertEqual(code, 0, err)

    def search(self, *argv, session=None):
        with mock.patch.object(indexes, "calling_session", return_value=session):
            return capture(search_cmd.main, list(argv))

    def test_output_equals_the_old_cli_and_json_matches(self):
        self.migrate_both()
        from fleetwatch.memory import session_search
        for extra in ([], ["--json"], ["--similar", "--no-rerank"]):
            _, old_out, _ = capture(session_search.run, self.old / "demo-session",
                                    self.old / "demo-session",
                                    argv=["lantern battery", "--limit", "5"] + extra)
            code, new_out, err = self.search("--index", "demo-session", "lantern battery",
                                             "--limit", "5", *extra)
            self.assertEqual(code, 0, err)
            self.assertIn("lantern", new_out.lower())
            if "--json" in extra:     # paths name the folder each corpus lives in
                strip = lambda t: [(r["heading"], r["score"]) for r in json.loads(t)]
                self.assertEqual(strip(new_out), strip(old_out))
            else:
                self.assertEqual(new_out, old_out)

    def test_an_unknown_index_is_named_as_missing(self):
        self.migrate_both()
        code, out, err = self.search("--index", "nope-session", "lantern", session=("Mine", SID))
        self.assertEqual(code, 2)
        self.assertIn("no index 'nope-session'", err)
        self.assertNotIn("None", err)

    def test_eval_and_verify_on_a_new_index_say_there_are_no_probes_yet(self):
        self.migrate_both()
        loc = indexes.locate(indexes.load_registry(), "demo-session")
        (loc.cfg_dir / "probes.yaml").write_text(PROBES_TEMPLATE_EMPTY)
        code, out, err = self.search("--index", "demo-session", "eval")
        self.assertEqual(code, 0, err)
        self.assertIn("No probes yet", out)
        buf = io.StringIO()
        bad = index_migrate.verify("demo-session", out=buf)
        self.assertIn("eval: no probes yet", buf.getvalue())
        self.assertEqual(bad, 0, buf.getvalue())

    def test_no_index_means_the_calling_sessions_own(self):
        self.migrate_both()
        code, out, err = self.search("lantern battery", session=("Mine", SID))
        self.assertEqual(code, 0, err)
        self.assertIn("EX-0001", out)

    def test_inside_a_session_a_cross_group_read_is_refused_and_an_in_group_one_allowed(self):
        self.migrate_both()
        code, out, err = self.search("--index", "demo-session", "lantern", session=("Mine", SID))
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("group 'research' allows readers research", err)
        # demo-session (research) reading an oss index is refused too: oss lists research, not the reverse
        code, out, err = self.search("--index", "mine-session", "lantern", session=("Demo Session", "zz"))
        self.assertEqual(code, 0, err)
        # and a plain terminal reads anything
        code, out, err = self.search("--index", "demo-session", "lantern")
        self.assertEqual(code, 0, err)

    def test_no_session_and_no_index_is_a_clear_error(self):
        self.migrate_both()
        code, out, err = self.search("lantern")
        self.assertEqual(code, 2)
        self.assertIn("--index NAME", err)

    def test_unknown_index(self):
        self.migrate_both()
        code, out, err = self.search("--index", "nope", "x")
        self.assertEqual(code, 2)
        self.assertIn("no index 'nope'", err)

    def test_show_stats_and_eval_pass_through(self):
        self.migrate_both()
        code, out, err = self.search("--index", "demo-session", "stats")
        self.assertEqual(code, 0, err)
        self.assertIn("Chunks:", out)
        code, out, err = self.search("--index", "demo-session", "eval")
        self.assertIn("MRR", out)


class ProbeExpectKeys(unittest.TestCase):
    """An expect key the engine does not check is an error at eval load, so a probe can never
    match the first result by default."""

    def setUp(self):
        base = tmp(self)
        use_layout(self, base / "config", base / "data")
        make_old_index(base, "demo-session", "oss", "Mine")
        self.ix = engine.Index(base / "demo-session")
        self.probes = base / "demo-session" / "probes.yaml"

    def probe(self, expect):
        self.probes.write_text(yaml.safe_dump({"probes": [{"query": "lantern battery", "expect": expect}]}))
        return engine.evaluate(self.ix)

    def test_a_nonexistent_exchange_scores_zero(self):
        self.assertEqual(self.probe({"path_contains": "Saa11bb-EX-0001"})["mrr"], 1.0)
        ev = self.probe({"path_contains": "S000000-EX-9999"})
        self.assertEqual((ev["mrr"], ev["found"]), (0.0, 0))

    def test_unknown_or_empty_expect_is_refused(self):
        for expect in ({"path": "S000000-EX-9999"}, {}, {"path_contains": "x", "pth": "y"}):
            with self.assertRaises(engine.ProbesInvalid, msg=expect):
                self.probe(expect)

    def test_the_add_template_names_a_supported_key(self):
        sample = [ln.lstrip("# ") for ln in index_cli.PROBES_TEMPLATE.splitlines() if "expect:" in ln]
        spec = yaml.safe_load("probes:\n  - query: q\n    " + sample[-1])
        engine.check_probes(spec["probes"])


# ------------------------------------------------------------------------------ migrate

class Migrate(unittest.TestCase):
    def setUp(self):
        base = tmp(self)
        self.cfg, self.data, self.old = base / "config", base / "data", base / "old"
        use_layout(self, self.cfg, self.data)
        write_registry(self.old, entries={"demo-session": make_old_index(self.old, "demo-session")})
        self.before = tree_digest(self.old)

    def run_migrate(self, *extra):
        return capture(index_migrate.migrate, "demo-session", str(self.old), *extra)

    def test_copies_into_the_two_homes_and_never_touches_the_old_tree(self):
        code, out, err = self.run_migrate()
        self.assertEqual(code, 0, err)
        croot, droot = self.cfg / "indexes", self.data / "indexes"
        for f in ("tags.yaml", "probes.yaml", "scan_allowlist.yaml"):
            self.assertTrue((croot / "demo-session" / f).exists(), f)
        for item in ("corpus", "index", "state"):
            self.assertTrue((droot / "demo-session" / item).is_dir(), item)
        self.assertFalse((croot / "demo-session" / "search.py").exists())
        self.assertFalse((croot / "demo-session" / "corpus").exists())
        self.assertTrue((croot / "registry.d" / "demo-session.yaml").exists())
        self.assertEqual(yaml.safe_load((croot / "registry.yaml").read_text())["groups"]["research"],
                         {"readers": ["research"]})
        self.assertEqual(tree_digest(self.old), self.before)
        self.assertNotIn("lantern", out)

    def test_the_migrated_data_folders_are_owner_only(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        self.assertEqual(self.run_migrate()[0], 0)
        for d in (self.data / "indexes", self.data / "indexes" / "demo-session"):
            self.assertEqual(d.stat().st_mode & 0o777, 0o700, d)

    def test_corpus_tree_is_byte_identical_and_mtimes_kept(self):
        self.run_migrate()
        new = self.data / "indexes" / "demo-session"
        old = self.old / "demo-session"
        self.assertEqual(index_migrate.corpus_digest(old), index_migrate.corpus_digest(new))
        f = "corpus/session/2026-04/20260401/Saa11bb-EX-0001.owner.md"
        self.assertAlmostEqual((old / f).stat().st_mtime, (new / f).stat().st_mtime, places=2)

    def test_search_config_is_generated_and_overrides_make_it_equal_the_old_one(self):
        code, out, err = self.run_migrate()
        self.assertIn("generated from the registry entry; 3 key(s) overridden", out)
        reg = indexes.load_registry()
        loc = indexes.locate(reg, "demo-session")
        self.assertFalse((loc.cfg_dir / "search_config.yaml").exists())
        mine = indexes.search_config(loc)
        old = yaml.safe_load((self.old / "demo-session" / "search_config.yaml").read_text())
        strip = lambda c: {k: v for k, v in c.items() if k not in ("corpus_root", "index_path")}
        self.assertEqual(strip(mine), strip(old))
        self.assertEqual(mine["corpus_root"], str(loc.data_dir))

    def test_refuses_to_overwrite_unless_forced_and_force_is_idempotent(self):
        self.assertEqual(self.run_migrate()[0], 0)
        with self.assertRaises(indexes.IndexesError):
            index_migrate.migrate("demo-session", str(self.old))
        a = tree_digest(self.cfg / "indexes"), tree_digest(self.data / "indexes")
        self.assertEqual(self.run_migrate(True)[0], 0)
        self.assertEqual((tree_digest(self.cfg / "indexes"), tree_digest(self.data / "indexes")), a)
        self.assertEqual(tree_digest(self.old), self.before)

    def test_a_key_the_generator_adds_and_the_old_config_lacks_keeps_a_copy_of_the_old_config(self):
        entry = yaml.safe_load((self.old / "registry.d" / "demo-session.yaml").read_text())["indexes"]["demo-session"]
        (self.old / "demo-session" / "search_config.yaml").write_text(
            old_config("demo-session", entry, self.old / "demo-session", drop=("generic_folders",)))
        code, out, err = self.run_migrate()
        self.assertEqual(code, 0, err)
        self.assertIn("copied from the old config (the generator adds 1 key(s) it lacks)", out)
        loc = indexes.locate(indexes.load_registry(), "demo-session")
        self.assertEqual(indexes.search_config(loc)["corpus_root"], str(loc.data_dir))

    def set_old_index_path(self, value):
        f = self.old / "demo-session" / "search_config.yaml"
        cfg = yaml.safe_load(f.read_text())
        cfg["index_path"] = value
        f.write_text(yaml.safe_dump(cfg, sort_keys=False))

    def overrides(self):
        f = self.cfg / "indexes" / "demo-session" / indexes.OVERRIDES
        return yaml.safe_load(f.read_text()) if f.exists() else {}

    def test_an_absolute_index_path_into_the_old_folder_moves_with_the_data(self):
        self.set_old_index_path("%s/demo-session/index/demo-session.sqlite" % self.old)
        code, out, err = self.run_migrate()
        self.assertEqual(code, 0, err)
        self.assertNotIn("index_path", self.overrides())
        loc = indexes.locate(indexes.load_registry(), "demo-session")
        ix = engine.Index(loc.cfg_dir, indexes.search_config(loc))
        self.assertEqual(ix.cfg["_index_path"], loc.data_dir / "index" / "demo-session.sqlite")

    def test_an_absolute_index_path_with_its_own_file_name_is_rewritten(self):
        self.set_old_index_path("%s/demo-session/index/older.sqlite" % self.old)
        code, out, err = self.run_migrate()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.overrides()["index_path"],
                         str(self.data / "indexes" / "demo-session" / "index" / "older.sqlite"))

    def test_from_the_new_homes_is_refused(self):
        with self.assertRaises(indexes.IndexesError):
            index_migrate.migrate("demo-session", str(self.data / "indexes"))

    def test_unknown_index_and_undeclared_group(self):
        with self.assertRaises(indexes.IndexesError):
            index_migrate.migrate("nope", str(self.old))
        (self.cfg / "indexes").mkdir(parents=True)
        (self.cfg / "indexes" / "registry.yaml").write_text("defaults: {}\ngroups: {oss: {readers: [oss]}}\n")
        with self.assertRaises(indexes.IndexesError):
            index_migrate.migrate("demo-session", str(self.old))
        self.assertFalse((self.data / "indexes" / "demo-session").exists())

    def test_paths_naming_the_old_folder_are_rewritten(self):
        e = yaml.safe_load((self.old / "registry.d" / "demo-session.yaml").read_text())
        e["indexes"]["demo-session"]["docs"] = [{"label": "x", "path": "~/docs"}]
        e["indexes"]["demo-session"]["note"] = "%s/demo-session/state/last_run.json" % self.old
        e["indexes"]["demo-session"]["tool"] = "%s/demo-session/tool.sh" % self.old
        e["indexes"]["demo-session"]["elsewhere"] = "%s/other/x" % self.old
        (self.old / "registry.d" / "demo-session.yaml").write_text(yaml.safe_dump(e))
        code, out, err = self.run_migrate()
        self.assertEqual(code, 0, err)
        entry = yaml.safe_load((self.cfg / "indexes" / "registry.d" / "demo-session.yaml").read_text())["indexes"]["demo-session"]
        self.assertEqual(entry["note"], str(self.data / "indexes" / "demo-session" / "state" / "last_run.json"))
        self.assertEqual(entry["tool"], str(self.cfg / "indexes" / "demo-session" / "tool.sh"))
        self.assertEqual(entry["elsewhere"], "%s/other/x" % self.old)
        self.assertIn("paths rewritten: 2; references to other old-root folders left as they were: 1", out)

    def test_a_plain_index_keeps_its_own_search_config_with_paths_rewritten(self):
        d = self.old / "lib"
        (d / "corpus").mkdir(parents=True)
        (d / "corpus" / "a.md").write_text("# A\n\nlantern notes here for the library.\n")
        plain = yaml.safe_load(old_config("lib", {"transcripts": {}}, d))
        plain["roots"] = [{"name": "docs", "path": "corpus", "extensions": [".md"], "default_kind": "doc"}]
        (d / "search_config.yaml").write_text(yaml.safe_dump(plain))
        write_registry(self.old, entries={"lib": {"group": "research", "kind": "plain",
                                                  "sources": ["~/x"], "index_file": "%s/lib/index/lib.sqlite" % self.old,
                                                  "refresh": [["%s/lib/build.sh" % self.old]]}})
        code, out, err = capture(index_migrate.migrate, "lib", str(self.old))
        self.assertEqual(code, 0, err)
        home = self.data / "indexes" / "lib"
        cfg = yaml.safe_load((home / "search_config.yaml").read_text())
        self.assertEqual(cfg["corpus_root"], str(home))
        self.assertTrue((home / "corpus" / "a.md").exists())
        self.assertFalse((self.cfg / "indexes" / "lib").exists())
        entry = yaml.safe_load((self.cfg / "indexes" / "registry.d" / "lib.yaml").read_text())["indexes"]["lib"]
        self.assertEqual(entry["index_file"], str(home / "index" / "lib.sqlite"))
        self.assertEqual(entry["refresh"], [[str(home / "build.sh")]])


# ------------------------------------------------------------------------ plain indexes

PREPARE = """\
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus"
CORPUS.mkdir(exist_ok=True)
for p in sorted((HERE / "raw").glob("*.md")):
    (CORPUS / p.name).write_text(p.read_text())
print("prepared", len(list(CORPUS.glob("*.md"))))
"""

OLD_ENGINE_IMPORT = """\
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
from catalog_corpus import write_catalog_corpus  # noqa: E402
"""


def make_old_plain(root, name="lib", group="research", extra_sources=()):
    """A plain index in the old single-folder layout: a prepare script that reads raw/ beside
    itself and writes corpus/ beside itself, a search.py wrapper, its own search_config.yaml,
    probes, and an engine-built index. Returns its registry entry."""
    d = root / name
    (d / "raw").mkdir(parents=True)
    (d / "raw" / "a.md").write_text("# A\n\nThe gate lantern battery is flat; swap the cell.\n")
    (d / "prepare_corpus.py").write_text(PREPARE)
    (d / "search.py").write_text("print('wrapper')\n")
    cfg = yaml.safe_load(old_config(name, {"transcripts": {}}, d))
    cfg["roots"] = [{"name": "docs", "path": "corpus", "extensions": [".md"], "default_kind": "doc"}]
    cfg["kind_rules"] = []
    cfg["index_path"] = "index/%s.sqlite" % name
    (d / "search_config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    (d / "probes.yaml").write_text(yaml.safe_dump(
        {"probes": [{"query": "lantern battery", "expect": {"path_contains": "a.md"}}]}))
    subprocess.run([sys.executable, str(d / "prepare_corpus.py")], check=True, capture_output=True)
    engine.build(engine.Index(d))
    (d / "state").mkdir()
    (d / "state" / "last_run.json").write_text("{}")
    return {"kind": "plain", "group": group, "index_file": "%s/%s/index/%s.sqlite" % (root, name, name),
            "sources": ["%s/%s/raw" % (root, name)] + list(extra_sources),
            "refresh": [["~/.venvs/old/bin/python", "-I", "%s/%s/prepare_corpus.py" % (root, name)],
                        ["~/.venvs/old/bin/python", "%s/%s/search.py" % (root, name), "build"]]}


class PlainIndex(unittest.TestCase):
    """A plain index migrates into one self-contained folder in the data home, and refresh,
    build, search and verify all find it there."""

    def setUp(self):
        base = tmp(self)
        self.cfg, self.old = base / "config", base / "old"
        self.xdg = base / "xdg"
        self.data = self.xdg / "fleetwatch"
        use_layout(self, self.cfg, self.data)
        sessions = base / "no-sessions"
        sessions.mkdir()
        # The build step runs `python -m fleetwatch` in a child process: point it at the same
        # homes, this checkout, and an empty session table (a plain terminal, owner mode).
        env = {"FLEETWATCH_CONFIG_DIR": str(self.cfg), "XDG_DATA_HOME": str(self.xdg),
               "FLEETWATCH_SESSIONS_DIR": str(sessions), "FLEETWATCH_NO_MIGRATE": "1",
               "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", "")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.entry = make_old_plain(self.old, extra_sources=["~/elsewhere/*.md"])
        write_registry(self.old, entries={"lib": self.entry})
        self.home = self.data / "indexes" / "lib"
        self.before = tree_digest(self.old)

    def migrate(self, *extra):
        code, out, err = capture(index_migrate.migrate, "lib", str(self.old), *extra)
        self.assertEqual(code, 0, err)
        return out

    def new_entry(self):
        return yaml.safe_load((self.cfg / "indexes" / "registry.d" / "lib.yaml").read_text())["indexes"]["lib"]

    def test_everything_lands_in_one_folder_in_the_data_home(self):
        out = self.migrate()
        for f in ("prepare_corpus.py", "search_config.yaml", "probes.yaml", "raw/a.md", "corpus/a.md",
                  "index/lib.sqlite", "state/last_run.json"):
            self.assertTrue((self.home / f).exists(), f)
        self.assertFalse((self.home / "search.py").exists())
        self.assertFalse((self.cfg / "indexes" / "lib").exists())
        self.assertIn("index folder:", out)
        loc = indexes.locate(indexes.load_registry(), "lib")
        self.assertEqual((loc.cfg_dir, loc.data_dir), (self.home, self.home))
        self.assertEqual(indexes.search_config(loc)["corpus_root"], str(self.home))
        self.assertEqual(tree_digest(self.old), self.before)

    def test_refresh_commands_run_under_the_python_token_and_build_through_fleetwatch(self):
        out = self.migrate()
        e = self.new_entry()
        self.assertEqual(e["refresh"], [
            ["{python}", "-I", str(self.home / "prepare_corpus.py")],
            ["{python}", "-P", "-m", "fleetwatch", "index", "build", "lib"]])
        self.assertEqual(e["index_file"], str(self.home / "index" / "lib.sqlite"))
        self.assertEqual(e["sources"], [str(self.home / "raw"), "~/elsewhere/*.md"])
        self.assertIn("refresh commands: 2 of 2 run under {python}; 1 build step(s) now "
                      "`fleetwatch index build lib`", out)

    def test_interpreter_names_flags_and_a_relative_wrapper(self):
        rw = index_migrate.Rewriter(self.old, "lib", self.home, self.home)
        cmds, n_py, n_build, warns = index_migrate.plain_refresh([
            ["/usr/bin/python3", "search.py", "build", "--force"],
            ["python3.12", "-X", "utf8", "%s/lib/x.py" % self.old],
            ["bash", "%s/lib/x.sh" % self.old],
            ["~/.venvs/old/bin/python", "%s/lib/search.py" % self.old, "stats"]], "lib", rw)
        self.assertEqual(cmds[0], ["{python}", "-P", "-m", "fleetwatch", "index", "build", "lib", "--force"])
        self.assertEqual(cmds[1], ["{python}", "-X", "utf8", str(self.home / "x.py")])
        self.assertEqual(cmds[2], ["bash", str(self.home / "x.sh")])
        self.assertEqual((n_py, n_build), (3, 1))
        self.assertEqual(len(warns), 1)
        self.assertIn("refresh command 4", warns[0])

    def test_the_token_resolves_to_the_running_interpreter(self):
        self.assertEqual(refresh.plain_argv(["{python}", "-I", "~/x.py", "{python}x"]),
                         [sys.executable, "-I", os.path.expanduser("~/x.py"), "{python}x"])

    def test_refresh_runs_in_the_folder_and_writes_the_corpus_there(self):
        self.migrate()
        (self.home / "raw" / "b.md").write_text("# B\n\nThe seed tray sits by the greenhouse door.\n")
        code, out, err = capture(refresh.run_plain, "lib", indexes.load_registry()["indexes"]["lib"],
                                 mock.Mock(build=True, full=False))
        self.assertTrue(code, out + err)
        self.assertIn("plain: rebuilt", out)
        self.assertTrue((self.home / "corpus" / "b.md").exists())
        self.assertFalse((self.cfg / "indexes" / "lib").exists())
        self.assertTrue(json.loads((self.home / "state" / "last_run.json").read_text())["ok"])
        loc = indexes.locate(indexes.load_registry(), "lib")
        code, out, err = capture(search_cmd.run_index, loc, ["greenhouse", "--json"])
        self.assertIn("b.md", out, err)

    def test_build_eval_and_verify_find_the_folder(self):
        self.migrate()
        reg = indexes.load_registry()
        code, out, err = capture(index_cli.cmd_build, reg, ["lib"])
        self.assertEqual(code, 0, err)
        buf = io.StringIO()
        bad = index_migrate.verify("lib", compare_old=str(self.old), out=buf)
        out = buf.getvalue()
        self.assertEqual(bad, 0, out)
        self.assertIn("eval MRR: 1.000", out)
        self.assertIn("old corpus checksum equal: ok", out)
        self.assertIn("effective search config equal to the old one, paths aside: ok", out)
        self.assertNotIn("lantern", out)

    def test_a_script_importing_the_old_engine_folder_is_warned_about_by_line(self):
        (self.old / "lib" / "helper.py").write_text(OLD_ENGINE_IMPORT)
        (self.old / "lib" / "other.py").write_text(
            "import sys\nsys.path.insert(0, %r)\n" % str(self.old / "engine"))
        out = self.migrate()
        self.assertIn("helper.py line 3 refers to the old engine folder", out)
        self.assertIn("other.py line 2 refers to the old engine folder", out)
        self.assertNotIn("prepare_corpus.py line", out)
        self.assertNotIn("catalog_corpus", out)

    def test_force_replaces_a_split_copy_left_by_an_earlier_migrate(self):
        stale = self.cfg / "indexes" / "lib"
        stale.mkdir(parents=True)
        (stale / "search_config.yaml").write_text("corpus_root: x\n")
        with self.assertRaises(indexes.IndexesError):
            index_migrate.migrate("lib", str(self.old))
        self.migrate(True)
        self.assertFalse(stale.exists())

    def test_a_split_plain_index_is_still_found_by_its_config_home(self):
        split = self.cfg / "indexes" / "lib"
        split.mkdir(parents=True)
        (split / "search_config.yaml").write_text("corpus_root: x\n")
        write_registry(self.cfg / "indexes", entries={"lib": self.entry})
        loc = indexes.locate(indexes.load_registry(), "lib")
        self.assertEqual((loc.cfg_dir, loc.data_dir), (split, self.home))
        self.assertEqual(refresh.plain_dir("lib"), split)


# ------------------------------------------------------------------------------- verify

class Verify(unittest.TestCase):
    def setUp(self):
        base = tmp(self)
        self.cfg, self.data, self.old = base / "config", base / "data", base / "old"
        use_layout(self, self.cfg, self.data)
        write_registry(self.old, entries={"demo-session": make_old_index(self.old, "demo-session")})
        capture(index_migrate.migrate, "demo-session", str(self.old))
        self.base = base

    def verify(self, **kw):
        out = io.StringIO()
        bad = index_migrate.verify("demo-session", out=out, **kw)
        return bad, out.getvalue()

    def test_numbers_only_and_ok_against_the_old_index(self):
        bad, out = self.verify(compare_old=str(self.old))
        self.assertEqual(bad, 0, out)
        self.assertIn("protection: ok", out)
        self.assertIn("eval MRR: 1.000", out)
        self.assertIn("eval found/probes: 1/1", out)
        self.assertIn("exchanges: 2", out)
        self.assertIn("corpus files: 2", out)
        self.assertIn("old corpus checksum equal: ok", out)
        self.assertIn("effective search config equal to the old one, paths aside: ok", out)
        self.assertIn("allowlist anon_id_pairing false: no", out)
        self.assertIn("registry doc_options.column_denylist override: no", out)
        self.assertTrue(out.rstrip().endswith("result: OK"))
        for secret in ("lantern", "battery", "Saa11bb", "swap the cell"):
            self.assertNotIn(secret, out)

    def test_a_broken_old_allowlist_is_reported_without_its_text(self):
        (self.old / "demo-session" / "scan_allowlist.yaml").write_text(
            "version: 1\nstaff: [Zorvath Staffperson\n  names: {\n")
        bad, out = self.verify(compare_old=str(self.old))
        self.assertGreater(bad, 0)
        self.assertIn("old scanner and tags loaded: MISMATCH", out)
        self.assertNotIn("Zorvath", out)
        self.assertNotIn("Staffperson", out)

    def test_compare_old_sets_an_absolute_index_path_aside(self):
        f = self.old / "demo-session" / "search_config.yaml"
        cfg = yaml.safe_load(f.read_text())
        cfg["index_path"] = "%s/demo-session/index/demo-session.sqlite" % self.old
        f.write_text(yaml.safe_dump(cfg, sort_keys=False))
        bad, out = self.verify(compare_old=str(self.old))
        self.assertIn("effective search config equal to the old one, paths aside: ok", out)
        cfg["index_path"] = "index/another.sqlite"
        f.write_text(yaml.safe_dump(cfg, sort_keys=False))
        bad, out = self.verify(compare_old=str(self.old))
        self.assertIn("effective search config equal to the old one, paths aside: MISMATCH", out)

    def test_baseline_flags_a_different_mrr_and_a_changed_corpus(self):
        _, ok = self.verify()
        sha = next(l.split(": ")[1] for l in ok.splitlines() if l.startswith("corpus checksum"))
        good = self.base / "good.json"
        good.write_text(json.dumps({"demo-session": {"mrr": 1.0, "found": 1, "probes": 1,
                                                     "corpus_files": 2, "corpus_sha": sha,
                                                     "exchanges": 2, "tags": 2}}))
        bad, out = self.verify(baseline=str(good))
        self.assertEqual(bad, 0, out)
        worse = self.base / "worse.json"
        worse.write_text(json.dumps({"demo-session": {"mrr": 0.65, "corpus_sha": "0" * 16}}))
        bad, out = self.verify(baseline=str(worse))
        self.assertEqual(bad, 2, out)
        self.assertIn("baseline MRR: MISMATCH (0.65 vs 1.000)", out)
        self.assertIn("baseline corpus_sha: MISMATCH", out)
        missing = self.base / "missing.json"
        missing.write_text("{}")
        self.assertEqual(self.verify(baseline=str(missing))[0], 1)

    def test_a_changed_corpus_against_the_old_index_is_a_mismatch(self):
        f = self.data / "indexes" / "demo-session" / "corpus" / "session" / "2026-04" / "20260401"
        (f / "Saa11bb-EX-0002.owner.md").write_text("# changed\n\nnew text for this exchange here.\n")
        bad, out = self.verify(compare_old=str(self.old))
        self.assertGreaterEqual(bad, 1)
        self.assertIn("old corpus checksum equal: MISMATCH", out)

    def test_a_protected_index_without_id_rules_is_refused(self):
        entry_file = self.cfg / "indexes" / "registry.d" / "demo-session.yaml"
        e = yaml.safe_load(entry_file.read_text())
        e["indexes"]["demo-session"]["group"] = "ferpa"
        entry_file.write_text(yaml.safe_dump(e))
        bad, out = self.verify()
        self.assertIn("protection: REFUSED", out)
        self.assertGreaterEqual(bad, 1)

    def test_the_two_flags_the_security_review_asked_for_are_booleans(self):
        (self.cfg / "indexes" / "demo-session" / "scan_allowlist.yaml").write_text(
            "version: 1\nanon_id_pairing: false\nkeep: [alpha-literal]\n")
        entry_file = self.cfg / "indexes" / "registry.d" / "demo-session.yaml"
        e = yaml.safe_load(entry_file.read_text())
        e["indexes"]["demo-session"]["doc_options"] = {"column_denylist": ["secretcol"]}
        entry_file.write_text(yaml.safe_dump(e))
        _, out = self.verify()
        self.assertIn("allowlist anon_id_pairing false: yes", out)
        self.assertIn("allowlist keep literals: yes", out)
        self.assertIn("registry doc_options.column_denylist override: yes", out)
        self.assertNotIn("alpha-literal", out)
        self.assertNotIn("secretcol", out)

    def test_a_stored_parse_signature_from_other_rules_is_a_mismatch(self):
        d = self.data / "indexes" / "demo-session" / "state" / "sessions"
        (d / "x.cursor.json").write_text(json.dumps({"scan_version": "stale|x"}))
        bad, out = self.verify()
        self.assertIn("stored parse signature equal: 0 of 1 cursors", out)
        self.assertGreaterEqual(bad, 1)


# --------------------------------------------------------------------------------- add

class Add(unittest.TestCase):
    def setUp(self):
        base = tmp(self)
        self.cfg, self.data, self.projects = base / "config", base / "data", base / "projects"
        self.sessions = base / "sessions"
        self.sessions.mkdir()
        for folder in ("-a-fleet", "-b-fleet-moved", "-c-other"):
            (self.projects / folder).mkdir(parents=True)
        for folder, sid in (("-a-fleet", SID), ("-b-fleet-moved", SID), ("-c-other", SID2)):
            (self.projects / folder / (sid + ".jsonl")).write_text("{}\n")
        (self.sessions / "700.json").write_text(json.dumps({"name": "Fleet Dev", "sessionId": SID}))
        use_layout(self, self.cfg, self.data, self.projects, packs=("education", self.write_pack()))
        os.environ["FLEETWATCH_SESSIONS_DIR"] = str(self.sessions)
        self.addCleanup(os.environ.pop, "FLEETWATCH_SESSIONS_DIR")
        os.environ["FLEETWATCH_PS_CMD"] = "true"
        self.addCleanup(os.environ.pop, "FLEETWATCH_PS_CMD")

    def write_pack(self):
        (self.cfg).mkdir(parents=True, exist_ok=True)
        (self.cfg / "pack.yaml").write_text(PACK)
        return "pack.yaml"

    def add(self, *argv):
        reg = indexes.load_registry()
        return capture(index_cli.cmd_add, reg, list(argv))

    def test_by_session_name_collects_every_folder_holding_its_transcripts(self):
        docs = self.cfg.parent / "docs"
        docs.mkdir()
        code, out, err = self.add("--session", "Fleet Dev", "--group", "oss", "--docs", str(docs),
                                  "--include", "*.md", "--include", "x/*.txt")
        self.assertEqual(code, 0, err)
        entry = yaml.safe_load((self.cfg / "indexes" / "registry.d" / "fleet-dev-session.yaml")
                               .read_text())["indexes"]["fleet-dev-session"]
        self.assertEqual(entry["owner_session"], "Fleet Dev")
        self.assertEqual(entry["owner_session_id"], SID)
        self.assertEqual(sorted(entry["transcripts"]["project_dir"]),
                         sorted([str(self.projects / "-a-fleet"), str(self.projects / "-b-fleet-moved")]))
        self.assertEqual(entry["transcripts"]["sessions"], [SID])
        self.assertEqual(entry["docs"][0]["include"], ["*.md", "x/*.txt"])
        self.assertEqual(entry["docs"][0]["path"], str(docs))
        cfg = self.cfg / "indexes" / "fleet-dev-session"
        for f in ("tags.yaml", "probes.yaml", "scan_allowlist.yaml"):
            self.assertTrue((cfg / f).exists())
        self.assertEqual(yaml.safe_load((self.cfg / "indexes" / "registry.yaml").read_text())["groups"],
                         {"oss": {"readers": ["oss"]}})
        # the entry loads and its templates are valid
        reg = indexes.load_registry()
        ix = refresh.Index("fleet-dev-session", reg["indexes"]["fleet-dev-session"], reg["defaults"])
        self.assertTrue(ix.tc.version)

    def test_the_calling_session_is_the_default(self):
        (self.sessions / "700.json").write_text(json.dumps({"name": "Fleet Dev", "sessionId": SID}))
        with mock.patch.object(indexes, "calling_session", return_value=("Fleet Dev", SID)):
            code, out, err = self.add("--group", "oss")
        self.assertEqual(code, 0, err)
        self.assertIn("added index fleet-dev-session", out)

    def test_outside_a_session_a_name_is_required(self):
        code, out, err = self.add("--group", "oss")
        self.assertEqual(code, 2)
        self.assertIn("--session NAME", err)

    def test_refuses_to_overwrite_and_to_register_a_second_index_for_one_session(self):
        self.assertEqual(self.add("--session", "Fleet Dev", "--group", "oss")[0], 0)
        before = tree_digest(self.cfg)
        code, out, err = self.add("--session", "Fleet Dev", "--group", "oss")
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        code, out, err = self.add("--session", "Fleet Dev", "--group", "oss", "--name", "another")
        self.assertEqual(code, 2)
        self.assertIn("already owns", err)
        self.assertEqual(tree_digest(self.cfg), before)

    def test_an_undeclared_group_and_an_unknown_session_are_refused(self):
        self.assertEqual(self.add("--session", "Fleet Dev", "--group", "oss")[0], 0)
        code, out, err = self.add("--session-id", SID2, "--name", "x", "--group", "nope")
        self.assertEqual(code, 2)
        self.assertIn("not declared", err)
        with self.assertRaises(indexes.IndexesError):
            self.add("--session", "No Such", "--group", "oss")

    def test_a_protected_group_starts_only_when_the_rules_allow_it(self):
        use_layout(self, self.cfg, self.data, self.projects, packs=("education",))
        code, out, err = self.add("--session", "Fleet Dev", "--group", "ferpa")
        self.assertEqual(code, 2)
        self.assertIn("ferpa", err)
        self.assertFalse((self.cfg / "indexes" / "fleet-dev-session").exists())

    def test_a_first_protected_index_writes_no_readers_and_a_second_cannot_read_it(self):
        code, out, err = self.add("--session", "Fleet Dev", "--group", "ferpa")
        self.assertEqual(code, 0, err)
        groups = yaml.safe_load((self.cfg / "indexes" / "registry.yaml").read_text())["groups"]
        self.assertEqual(groups, {"ferpa": {"readers": []}})
        code, out, err = self.add("--session-id", SID2, "--session", "Other Course", "--group", "ferpa")
        self.assertEqual(code, 0, err)
        reg = indexes.load_registry()
        self.assertIn("refused", indexes.read_refusal(reg, ("Other Course", SID2), "fleet-dev-session"))
        self.assertIsNone(indexes.read_refusal(reg, ("Fleet Dev", SID), "fleet-dev-session"))

    def test_a_name_differing_only_in_case_from_an_invalid_entry_is_refused(self):
        # An entry the registry cannot act on still holds its name, and on a case-insensitive
        # file system "Fleet-Dev-Session" and "fleet-dev-session" are one folder.
        write_registry(self.cfg / "indexes")
        reg_file = self.cfg / "indexes" / "registry.yaml"
        reg = yaml.safe_load(reg_file.read_text())
        reg["indexes"] = {"Fleet-Dev-Session": {"group": "ferpa", "owner_session": "Someone",
                                                "owner_session_id": SID2}}
        reg_file.write_text(yaml.safe_dump(reg))
        with contextlib.redirect_stderr(io.StringIO()):
            code, out, err = self.add("--session", "Fleet Dev", "--group", "oss")
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertFalse((self.cfg / "indexes" / "registry.d" / "fleet-dev-session.yaml").exists())

    def test_a_session_owning_an_invalid_entry_cannot_add_a_second(self):
        write_registry(self.cfg / "indexes", entries={
            "Old Name": {"group": "ferpa", "owner_session": "Fleet Dev", "owner_session_id": SID}})
        with contextlib.redirect_stderr(io.StringIO()):
            code, out, err = self.add("--session", "Fleet Dev", "--group", "oss")
        self.assertEqual(code, 2)
        self.assertIn("already owns", err)

    def test_a_session_with_no_transcript_is_refused(self):
        (self.sessions / "701.json").write_text(json.dumps({"name": "Ghost", "sessionId": "dd55-none"}))
        code, out, err = self.add("--session", "Ghost", "--group", "oss")
        self.assertEqual(code, 2)
        self.assertIn("no transcript", err)

    def test_a_codex_index_names_its_folder_and_has_no_owner_session(self):
        codex = self.cfg.parent / "codex-sessions"
        codex.mkdir()
        code, out, err = self.add("--source", "codex", "--path", str(codex), "--group", "research")
        self.assertEqual(code, 0, err)
        entry = yaml.safe_load((self.cfg / "indexes" / "registry.d" / "codex-sessions.yaml")
                               .read_text())["indexes"]["codex-sessions"]
        self.assertEqual(entry["transcripts"], {"source": "codex", "path": str(codex)})
        self.assertNotIn("owner_session", entry)
        self.assertIn("Codex CLI", (self.cfg / "indexes" / "codex-sessions" / "tags.yaml").read_text())
        reg = indexes.load_registry()
        ix = refresh.Index("codex-sessions", reg["indexes"]["codex-sessions"], reg["defaults"])
        self.assertTrue(ix.tc.version)
        code, out, err = self.add("--source", "codex", "--path", str(codex), "--group", "research")
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)

    def test_an_antigravity_index_names_its_folder_and_has_no_owner_session(self):
        convs = self.cfg.parent / "conversations"
        convs.mkdir()
        code, out, err = self.add("--source", "antigravity", "--path", str(convs), "--group",
                                  "research")
        self.assertEqual(code, 0, err)
        self.assertIn("Antigravity sessions", out)
        entry = yaml.safe_load((self.cfg / "indexes" / "registry.d" / "antigravity-sessions.yaml")
                               .read_text())["indexes"]["antigravity-sessions"]
        self.assertEqual(entry["transcripts"], {"source": "antigravity", "path": str(convs)})
        self.assertNotIn("owner_session", entry)
        self.assertIn("Antigravity CLI",
                      (self.cfg / "indexes" / "antigravity-sessions" / "tags.yaml").read_text())
        reg = indexes.load_registry()
        ix = refresh.Index("antigravity-sessions", reg["indexes"]["antigravity-sessions"],
                           reg["defaults"])
        self.assertTrue(ix.tc.version)
        code, out, err = self.add("--source", "antigravity", "--path", str(convs), "--group",
                                  "research", "--session", "Fleet Dev")
        self.assertEqual(code, 2)
        self.assertIn("Antigravity index has no owner session", err)

    def test_a_codex_index_needs_an_existing_folder_and_path_needs_codex(self):
        code, out, err = self.add("--source", "codex", "--path", str(self.cfg.parent / "none"),
                                  "--group", "research")
        self.assertEqual((code, "does not exist" in err), (2, True))
        code, out, err = self.add("--session", "Fleet Dev", "--path", "/x", "--group", "oss")
        self.assertEqual((code, "--source codex" in err), (2, True))

    def test_a_codex_index_refuses_session_flags(self):
        codex = self.cfg.parent / "codex-sessions"
        codex.mkdir()
        for flags in (("--session", "Fleet Dev"), ("--session-id", SID)):
            code, out, err = self.add("--source", "codex", "--path", str(codex), "--group", "research",
                                      *flags)
            self.assertEqual(code, 2, flags)
            self.assertIn("--session", err)
            self.assertIn("Codex", err)
        self.assertFalse((self.cfg / "indexes" / "registry.d" / "codex-sessions.yaml").exists())

    def test_a_relative_codex_path_is_stored_absolute(self):
        codex = self.cfg.parent / "rollouts"
        codex.mkdir()
        old = os.getcwd()
        os.chdir(self.cfg.parent)
        self.addCleanup(os.chdir, old)
        code, out, err = self.add("--source", "codex", "--path", "rollouts", "--group", "research")
        self.assertEqual(code, 0, err)
        entry = yaml.safe_load((self.cfg / "indexes" / "registry.d" / "codex-sessions.yaml")
                               .read_text())["indexes"]["codex-sessions"]
        self.assertEqual(entry["transcripts"]["path"], str(codex))


# ----------------------------------------------------------------- guard on index commands

class IndexNames(unittest.TestCase):
    """An index name is one safe path component everywhere it is read or given."""

    def setUp(self):
        base = tmp(self)
        self.cfg, self.data, self.old = base / "config", base / "data", base / "old"
        use_layout(self, self.cfg, self.data)
        self.cfg.mkdir(parents=True, exist_ok=True)
        (self.cfg / "settings-canary.txt").write_text("keep me\n")

    def test_the_registry_skips_names_that_are_not_one_safe_component(self):
        write_registry(self.cfg / "indexes", entries={
            "..": {"group": "research"}, "Upper": {"group": "research"},
            "a..b": {"group": "research"}, "ok-one.v2": {"group": "research"}})
        with contextlib.redirect_stderr(io.StringIO()) as e:
            reg = indexes.load_registry()
        self.assertEqual(sorted(reg["indexes"]), ["ok-one.v2"])
        self.assertIn("not valid", e.getvalue())

    def test_migrate_dotdot_force_never_deletes_outside_the_index(self):
        write_registry(self.old, entries={"..": make_old_index(self.old, "demo-session")})
        write_registry(self.cfg / "indexes")
        code, out, err = capture(index_cli.main, ["migrate", "..", "--from", str(self.old), "--force"])
        self.assertNotEqual(code, 0)
        self.assertIn("not valid", err)
        with self.assertRaises(indexes.IndexesError):
            index_migrate.migrate("..", str(self.old), True)
        self.assertEqual((self.cfg / "settings-canary.txt").read_text(), "keep me\n")
        self.assertTrue((self.cfg / "indexes" / "registry.yaml").exists())

    def test_a_command_naming_a_path_is_refused(self):
        write_registry(self.cfg / "indexes")
        for argv in (["build", "../x"], ["verify", "a/b"], ["status", ".."]):
            code, out, err = capture(index_cli.main, argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("not valid", err, argv)

    def test_a_name_of_the_roots_own_files_is_reserved(self):
        for name in ("registry.d", "registry.yaml", "logs", "settings.toml", "indexes"):
            self.assertIn("reserved", refresh.index_name_problem(name) or "", name)
        self.assertIsNone(refresh.index_name_problem("logs-session"))

    def test_migrate_force_of_a_reserved_name_leaves_the_registry_alone(self):
        write_registry(self.old, entries={"registry.d": make_old_index(self.old, "registry.d")})
        write_registry(self.cfg / "indexes", entries={"lab": {"group": "research"}})
        code, out, err = capture(index_cli.main, ["migrate", "registry.d", "--from", str(self.old), "--force"])
        self.assertNotEqual(code, 0)
        self.assertIn("reserved", err)
        self.assertTrue((self.cfg / "indexes" / "registry.d" / "lab.yaml").exists())

    def test_migrate_refuses_a_name_that_differs_only_in_case_from_a_registered_one(self):
        write_registry(self.old, entries={"demo-session": make_old_index(self.old, "demo-session")})
        write_registry(self.cfg / "indexes")
        reg_file = self.cfg / "indexes" / "registry.yaml"
        reg = yaml.safe_load(reg_file.read_text())
        reg["indexes"] = {"Demo-Session": {"group": "ferpa", "owner_session_id": SID2}}
        reg_file.write_text(yaml.safe_dump(reg))
        code, out, err = capture(index_cli.main, ["migrate", "demo-session", "--from", str(self.old), "--force"])
        self.assertNotEqual(code, 0)
        self.assertIn("without case", err)
        self.assertFalse((self.data / "indexes" / "demo-session").exists())

    def test_an_invalid_entry_stays_visible_to_ownership_and_is_never_acted_on(self):
        write_registry(self.cfg / "indexes", entries={
            "Advising": {"group": "ferpa", "owner_session": "Advising", "owner_session_id": SID2}})
        with contextlib.redirect_stderr(io.StringIO()):
            reg = indexes.load_registry()
        self.assertNotIn("Advising", reg["indexes"])
        self.assertEqual(indexes.owned_by(reg, "Advising", SID2), ["Advising"])
        with self.assertRaises(indexes.IndexesError) as cm:
            indexes.own_index(reg, ("Advising", SID2))
        self.assertIn("not valid", str(cm.exception))
        self.assertIsNotNone(indexes.read_refusal(reg, ("Lab", SID), "Advising"))


class IndexCommandsGuard(unittest.TestCase):
    """Every `index` command that names an index runs the trust guard first; triage, which
    prints raw name candidates, refuses a protected index to another session."""

    def setUp(self):
        base = tmp(self)
        use_layout(self, base / "config", base / "data")
        write_registry(base / "config" / "indexes", entries={
            "student": {"group": "ferpa", "owner_session": "Grading", "owner_session_id": SID2},
            "lab": {"group": "research", "owner_session": "Lab", "owner_session_id": SID}})
        self.old = base / "old"
        write_registry(self.old, entries={
            "student": {"group": "ferpa", "owner_session": "Grading", "owner_session_id": SID2}})

    def run_as(self, session, argv):
        with mock.patch.object(refresh, "main") as drv, \
                mock.patch.object(refresh, "cmd_triage") as tri, \
                mock.patch.object(index_migrate, "verify", return_value=0) as ver, \
                mock.patch.object(index_migrate, "migrate", return_value=0) as mig, \
                mock.patch.object(indexes, "calling_session", return_value=session):
            code, out, err = capture(index_cli.main, argv)
        return code, err, drv.called or tri.called or ver.called or mig.called

    def test_every_command_naming_a_protected_index_refuses_another_session(self):
        for argv in (["triage", "student"], ["retag", "student", "--all"], ["status", "student"],
                     ["refresh", "student"], ["file", "/x/doc.md", "--index", "student"],
                     ["build", "student"], ["verify", "student"], ["forget", "student", SID2],
                     ["migrate", "student", "--from", str(self.old)]):
            code, err, ran = self.run_as(("Lab", SID), argv)
            self.assertEqual(code, 3, argv)
            self.assertIn("protected group", err, argv)
            self.assertFalse(ran, argv)

    def test_migrate_force_over_a_live_protected_index_is_refused(self):
        # the old registry's copy is an index the caller may read; the live one is not
        write_registry(self.old, entries={
            "student": {"group": "research", "owner_session": "Lab", "owner_session_id": SID}})
        for extra in ([], ["--force"]):
            code, err, ran = self.run_as(("Lab", SID), ["migrate", "student", "--from", str(self.old)] + extra)
            self.assertEqual(code, 3, extra)
            self.assertIn("protected group", err)
            self.assertFalse(ran, extra)

    def test_status_without_a_name_lists_only_readable_indexes(self):
        def status(session):
            with mock.patch.object(indexes, "calling_session", return_value=session):
                return capture(index_cli.main, ["status"])
        code, out, err = status(("Lab", SID))
        self.assertEqual(code, 0, err)
        self.assertIn("== lab (group research)", out)
        self.assertNotIn("student", out)
        self.assertIn("1 index this session may not read not shown", out)
        code, out, err = status(None)
        self.assertIn("== student (group ferpa)", out)
        self.assertIn("== lab (group research)", out)

    def test_forget_needs_the_owner_not_only_a_reader(self):
        root = refresh.CONFIG_ROOT
        write_registry(root, entries={
            "lab": {"group": "research", "owner_session": "Lab", "owner_session_id": SID},
            "peer": {"group": "research", "owner_session": "Peer", "owner_session_id": SID2}})
        reg = indexes.load_registry()
        self.assertIsNone(indexes.read_refusal(reg, ("Peer", SID2), "lab"))   # a reader
        code, err, ran = self.run_as(("Peer", SID2), ["forget", "lab", SID])
        self.assertEqual(code, 3)
        self.assertIn("not owned by this session", err)
        self.assertFalse(ran)
        for session in (("Lab", SID), None):
            code, err, ran = self.run_as(session, ["forget", "lab", SID])
            self.assertTrue(ran, (session, err))
        # the driver run as a script checks ownership too
        with mock.patch.object(refresh, "load_registry", return_value=reg), \
                mock.patch.object(refresh, "forget_session") as fs, \
                mock.patch.object(indexes, "calling_session", return_value=("Peer", SID2)):
            code, out, err = capture(refresh._main, ["forget", "lab", SID])
        self.assertEqual(code, 3)
        self.assertFalse(fs.called)

    def test_the_owner_and_a_plain_terminal_pass_the_guard(self):
        for session in (("Grading", SID2), None):
            code, err, ran = self.run_as(session, ["triage", "student"])
            self.assertTrue(ran, (session, err))

    def test_the_driver_guards_triage_when_run_on_its_own(self):
        reg = indexes.load_registry()
        with mock.patch.object(refresh, "load_registry", return_value=reg), \
                mock.patch.object(refresh, "cmd_triage") as tri, \
                mock.patch.object(indexes, "calling_session", return_value=("Lab", SID)):
            code, out, err = capture(refresh._main, ["triage", "student"])
        self.assertEqual(code, 3)
        self.assertFalse(tri.called)


# ----------------------------------------------------------------------- wrappers, dispatch

class Wrappers(unittest.TestCase):
    def setUp(self):
        base = tmp(self)
        use_layout(self, base / "config", base / "data")
        write_registry(base / "config" / "indexes", entries={
            "mine-session": {"group": "oss", "owner_session": "Mine", "owner_session_id": SID}})
        self.reg = indexes.load_registry()

    def test_refresh_file_retag_discover_triage_go_to_the_driver(self):
        calls = []
        with mock.patch.object(refresh, "main", side_effect=lambda a: calls.append(a)), \
                mock.patch.object(indexes, "calling_session", return_value=("Mine", SID)):
            index_cli.cmd_refresh(self.reg, ["--all", "--full", "--no-tag"])
            index_cli.cmd_refresh(self.reg, ["mine-session", "--build"])
            index_cli.cmd_refresh(self.reg, [])
            index_cli.cmd_file(self.reg, ["/x/doc.md"])
            index_cli.main(["retag", "mine-session", "--all"])
            index_cli.main(["discover"])
            index_cli.main(["triage", "mine-session"])
        self.assertEqual(calls, [
            ["--full", "--no-tag"],
            ["--index", "mine-session", "--build"],
            ["--index", "mine-session"],
            ["index-now", "mine-session", "/x/doc.md"],
            ["retag", "mine-session", "--all"], ["discover"], ["triage", "mine-session"]])

    def test_list_and_status(self):
        code, out, err = capture(index_cli.cmd_list, self.reg, [])
        self.assertIn("mine-session", out)
        self.assertIn("never", out)
        code, out, err = capture(index_cli.cmd_status, self.reg, ["mine-session"])
        self.assertIn("== mine-session (group oss)", out)
        with self.assertRaises(indexes.IndexesError):
            index_cli.cmd_status(self.reg, ["nope"])

    def test_the_subcommands_are_routed_from_the_program(self):
        env = dict(os.environ, FLEETWATCH_NO_MIGRATE="1", PYTHONPATH=str(REPO),
                   FLEETWATCH_CONFIG_DIR=str(tmp(self)), XDG_DATA_HOME=str(tmp(self)))
        for argv in (["search"], ["index", "--help"]):
            p = subprocess.run([sys.executable, "-m", "fleetwatch"] + argv, capture_output=True,
                               text=True, env=env, cwd=str(REPO), timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("fleetwatch", p.stdout)


# ---------------------------------------------------------------------------- schedule

class Schedule(unittest.TestCase):
    def setUp(self):
        base = tmp(self)
        self.dir = base / "LaunchAgents"
        self.state = base / "state"
        env = {"FLEETWATCH_LAUNCH_AGENTS_DIR": str(self.dir),
               "FLEETWATCH_SYSTEMD_USER_DIR": str(base / "systemd"), "XDG_STATE_HOME": str(self.state)}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)

    def run_schedule(self, verb, platform):
        out = io.StringIO()
        with mock.patch.object(schedule.sys, "platform", platform):
            code = schedule.main([verb], out)
        return code, out.getvalue()

    def test_install_writes_a_valid_plist_and_does_not_load_it(self):
        with mock.patch.object(subprocess, "run") as run:
            code, out = self.run_schedule("install", "darwin")
        run.assert_not_called()
        self.assertEqual(code, 0)
        path = self.dir / "local.fleetwatch.index-refresh.plist"
        plist = plistlib.loads(path.read_bytes())
        self.assertEqual(plist["Label"], "local.fleetwatch.index-refresh")
        self.assertEqual(plist["StartInterval"], 1800)
        self.assertEqual(plist["ProgramArguments"][-4:], ["fleetwatch", "index", "refresh", "--all"])
        self.assertEqual(plist["Nice"], 10)
        self.assertTrue(plist["StandardOutPath"].startswith(str(self.state / "fleetwatch")))
        self.assertIn("launchctl bootstrap", out)
        self.assertIn("not loaded", out)
        self.assertFalse((self.dir / "local.search-indexes.refresh.plist").exists())
        if shutil.which("plutil"):
            p = subprocess.run(["plutil", "-lint", str(path)], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_status_and_remove(self):
        self.assertIn("not installed", self.run_schedule("status", "darwin")[1])
        self.run_schedule("install", "darwin")
        self.assertIn(": installed", self.run_schedule("status", "darwin")[1])
        self.assertIn("removed", self.run_schedule("remove", "darwin")[1])
        self.assertFalse((self.dir / "local.fleetwatch.index-refresh.plist").exists())

    def test_linux_writes_a_systemd_timer_and_prints_a_cron_line(self):
        code, out = self.run_schedule("install", "linux")
        base = Path(os.environ["FLEETWATCH_SYSTEMD_USER_DIR"])
        self.assertIn("OnUnitActiveSec=30min", (base / "fleetwatch-index-refresh.timer").read_text())
        self.assertIn("ExecStart=", (base / "fleetwatch-index-refresh.service").read_text())
        self.assertIn("*/30 * * * *", out)
        self.assertIn("systemctl --user enable", out)

    def test_an_unknown_verb_is_a_usage_error(self):
        self.assertEqual(schedule.main(["nope"]), 2)


if __name__ == "__main__":
    unittest.main()
