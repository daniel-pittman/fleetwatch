"""The indexer's settings: defaults that name nobody, strict loading, the owner's canonical
kind, rule packs, the protected-group guard and the zone that lays out day folders. All data
is synthetic."""
import json
import os

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")  # tests never move a real home folder
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from fleetwatch import settings
from fleetwatch.memory import doc_corpus, privacy_scan, refresh, render, session_parse, session_tagger
from fleetwatch.search import engine

REPO = Path(__file__).resolve().parent.parent
TAGS = {"description": "A synthetic session.", "areas": {"garden": "garden work"},
        "kinds": {"decision": "a decision"}}

PRIVATE_PACK = textwrap.dedent(r"""
    name: synthetic-school
    id_patterns: ['\bQX-\d{6}\b']
    anon_ids:
      - pattern: '\bL\d{2}\b'
        pairing: '\bL\d{2}\b\s*(?:is|=)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)'
    private_paths: ['sealed-records/']
    institution_words: [zorbmoot]
    column_denylist: [learner code]
    """)


def tmpdir(test):
    t = tempfile.TemporaryDirectory()
    test.addCleanup(t.cleanup)
    return Path(t.name)


class Defaults(unittest.TestCase):
    def test_an_empty_config_folder_gives_generic_defaults(self):
        cfg = tmpdir(self)
        env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        os.environ["XDG_DATA_HOME"] = str(cfg / "data")
        os.environ.pop("FLEETWATCH_PROJECTS_DIR", None)
        s = settings.indexer(cfg_dir=cfg)
        self.assertEqual((s.owner_kind, s.owner_name, s.owner_display), ("owner", "", "User"))
        self.assertEqual(s.index_root, cfg / "data" / "fleetwatch" / "indexes")
        self.assertEqual(s.projects_dir, Path.home() / ".claude" / "projects")
        self.assertEqual(s.build_python, sys.executable)
        self.assertEqual(s.heartbeat_patterns, settings.DEFAULT_HEARTBEAT_PATTERNS)
        self.assertEqual(s.automated_prefixes, settings.DEFAULT_AUTOMATED_PREFIXES)
        self.assertEqual(s.protected_groups, frozenset({"ferpa"}))
        self.assertEqual(s.group_guess, ())
        self.assertEqual(s.rule_packs, ("education",))
        self.assertIsNone(s.column_denylist)
        self.assertIsNone(s.default_guidance)
        self.assertEqual(s.tz, settings.system_tz())

    def test_default_rules_name_nobody(self):
        r = session_parse.TranscriptRules()
        self.assertEqual((r.owner_kind, r.owner_name, r.owner_label), ("owner", "User", "User"))
        rs = privacy_scan.RuleSet()
        self.assertEqual(rs.names, ["core", "education"])
        self.assertEqual(rs.id_res, [])
        self.assertEqual(rs.anon_res, [])


class Loading(unittest.TestCase):
    def write(self, text, files=None):
        cfg = tmpdir(self)
        (cfg / "settings.toml").write_text(textwrap.dedent(text))
        for name, body in (files or {}).items():
            (cfg / name).write_text(body)
        return cfg

    def test_every_key_is_read_and_files_resolve_in_the_config_folder(self):
        cfg = self.write("""
            timezone = "Asia/Tokyo"
            owner_name = "Sam"
            [paths]
            index_root = "/srv/indexes"
            projects_dir = "/srv/projects"
            build_python = "/opt/py/bin/python"
            [transcripts]
            owner_kind = "sam"
            heartbeat_patterns = ["^TICK"]
            automated_prefixes = ["Probe:"]
            [groups]
            protected = ["sealed", "ferpa"]
            guess = {"-work-" = "research", "-home-" = "personal"}
            [scanner]
            rule_packs = ["education", "school-pack.yaml"]
            [docs]
            column_denylist = ["email", "badge"]
            [tagger]
            default_guidance_file = "guide.txt"
            """, {"school-pack.yaml": PRIVATE_PACK})
        (cfg / "guide.txt").write_text("summary: one line.\n")
        s = settings.indexer(cfg_dir=cfg)
        self.assertEqual(s.tz, ZoneInfo("Asia/Tokyo"))
        self.assertEqual((s.owner_kind, s.owner_display), ("sam", "Sam"))
        self.assertEqual((s.index_root, s.projects_dir, s.build_python),
                         (Path("/srv/indexes"), Path("/srv/projects"), "/opt/py/bin/python"))
        self.assertEqual(s.heartbeat_patterns, ("^TICK",))
        self.assertEqual(s.automated_prefixes, ("Probe:",))
        self.assertEqual(s.protected_groups, frozenset({"sealed", "ferpa"}))
        self.assertEqual(s.group_guess, (("-work-", "research"), ("-home-", "personal")))
        self.assertEqual(s.rule_packs, ("education", str(cfg / "school-pack.yaml")))
        self.assertEqual(s.column_denylist, ("email", "badge"))
        self.assertEqual(s.default_guidance, "summary: one line.\n")

    def test_invalid_values_stop_the_indexer(self):
        bad = ['timezone = "Mars/Olympus"', 'owner_name = 3', 'nonsense = [',
               '[transcripts]\nowner_kind = "heartbeat"', '[transcripts]\nowner_kind = "Has Space"',
               '[transcripts]\nheartbeat_patterns = ["("]', '[scanner]\nrule_packs = "education"',
               '[groups]\nguess = ["x"]', '[tagger]\ndefault_guidance = "a"\ndefault_guidance_file = "b"',
               '[tagger]\ndefault_guidance_file = "missing.txt"', 'paths = 1']
        for text in bad:
            with self.subTest(text=text), self.assertRaises(settings.SettingsError):
                settings.indexer(cfg_dir=self.write(text))

    def test_the_board_reader_still_ignores_what_it_cannot_read(self):
        cfg = self.write('timezone = "Mars/Olympus"')
        self.assertEqual(settings.local_tz(settings.load(cfg / "settings.toml")), settings.system_tz())

    def test_refresh_reports_a_bad_settings_file_and_stops(self):
        cfg = self.write("nonsense = [")
        env = dict(os.environ, FLEETWATCH_CONFIG_DIR=str(cfg), PYTHONPATH=str(REPO))
        p = subprocess.run([sys.executable, "-c", "from fleetwatch.memory import refresh; refresh.main(['status'])"],
                           capture_output=True, text=True, env=env, cwd=str(cfg))
        self.assertEqual(p.returncode, 1)
        self.assertIn("settings:", p.stderr)


class OwnerKind(unittest.TestCase):
    QUEUED = {"origin": {"kind": "queued"}}

    def test_the_owner_kind_and_name_come_from_the_rules(self):
        rules = session_parse.TranscriptRules(owner_kind="sam", owner_name="Sam")
        opts = session_parse.parse_opts({})
        self.assertEqual(session_parse.classify_incoming({}, "hello", opts, rules),
                         ("sam", "Sam", "hello", "sam"))
        default = session_parse.classify_incoming({}, "hello", opts)
        self.assertEqual(default, ("owner", "User", "hello", "owner"))

    def test_the_kinds_map_renames_the_owner_by_its_kind_or_by_owner(self):
        rules = session_parse.TranscriptRules(owner_kind="sam", owner_name="Sam")
        for spec, want in [({"kinds": {"sam": "lead"}}, "lead"), ({"kinds": {"owner": "lead"}}, "lead"),
                           ({"kinds": {"heartbeat": "cron"}}, "sam")]:
            o = session_parse.parse_opts(spec)
            r = session_parse.classify_incoming(dict(self.QUEUED), "do it", o, rules)
            self.assertEqual((r[0], r[3]), (want, "sam"), spec)
        r = session_parse.classify_incoming({}, "x", session_parse.parse_opts({"kinds": {"owner": "me"}}))
        self.assertEqual((r[0], r[3]), ("me", "owner"))

    def test_identity_is_not_part_of_the_parse_signature(self):
        # Stored cursors keep their signature when an index adopts these settings; a change
        # of identity takes a --full refresh (settings module docstring).
        o = session_parse.parse_opts({})
        self.assertEqual(json.loads(json.dumps(o, sort_keys=True)),
                         dict(queued_as_human=True, kinds={}, peer_session_kinds={}, bare_exchange_ids=False))

    def test_a_named_owner_renders_its_label_tags_and_file_name(self):
        rules = session_parse.TranscriptRules(owner_kind="sam", owner_name="Sam",
                                              tz=ZoneInfo("UTC"))
        tc = session_tagger.TagConfig(TAGS)
        rec = dict(n=1, ex="EX", start_offset=0, start_line=1, end_line=2, sender_kind="sam",
                   sender_canon="sam", sender_name="Sam", ts="2026-05-01T12:00:00Z",
                   user_text="plant the beans", assistant=["Planted."], tools={}, interrupted=False,
                   session="0f1e2d3c-4b5a-6978-8091-a2b3c4d5e6f7", id="S0f1e2d-EX-0001")
        out = tmpdir(self)
        session_tagger.write_corpus([rec], {}, tc, out, rules)
        f = out / "2026-05" / "20260501" / "S0f1e2d-EX-0001.sam.md"
        text = f.read_text()
        self.assertIn("| from Sam (user)", text)
        self.assertIn("Tags: sender:sam", text)
        self.assertIn("## S0f1e2d-EX-0001 Sam (user)", text)
        # The tag cache keys on the index's kind name and the text, never on the label.
        self.assertEqual(session_tagger.tag_hash(rec), session_tagger.tag_hash(dict(rec, sender_name="X")))

    def test_unnamed_owner_reads_user(self):
        r = dict(sender_kind="owner", sender_canon="owner", sender_name="User")
        self.assertEqual(render.sender_label(r), "User")


class TaggerGuidance(unittest.TestCase):
    def test_settings_guidance_replaces_the_default_and_moves_the_version(self):
        base = session_tagger.TagConfig(TAGS)
        other = session_tagger.TagConfig(TAGS, "summary: say what changed.")
        self.assertNotEqual(base.version, other.version)
        self.assertIn("say what changed", other.system)
        # Same text as the built-in default: same prompt, same version.
        self.assertEqual(session_tagger.TagConfig(TAGS, session_tagger.DEFAULT_GUIDANCE).version,
                         base.version)
        # A tags.yaml prompt still wins over both.
        mine = dict(TAGS, prompt="summary: mine.")
        self.assertEqual(session_tagger.TagConfig(mine, "summary: say what changed.").version,
                         session_tagger.TagConfig(mine).version)


class RulePacks(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir(self)
        (self.dir / "allow.yaml").write_text("version: 1\nperson_names: false\n")
        (self.dir / "school.yaml").write_text(PRIVATE_PACK)

    def scanner(self, packs):
        return privacy_scan.Scanner(self.dir / "allow.yaml", rules=privacy_scan.RuleSet(packs))

    def scrub(self, packs, text):
        return self.scanner(packs).scan_text(text)

    def test_core_scrubs_secrets_emails_phones_and_user_ids(self):
        out, cats, _ = self.scrub([], "user id 5520117, mail a.b@example.org, call 212-555-0142, "
                                      "key sk-ant-%s" % ("x" * 30))
        for v in ("5520117", "a.b@example.org", "212-555-0142", "sk-ant-"):
            self.assertNotIn(v, out)
        self.assertEqual(self.scanner([]).rules.names, ["core"])

    def test_education_adds_student_context_vocabulary_and_roster_paths(self):
        text = "student 7731904 is in roster.csv"
        core, ccats, _ = self.scrub([], text)
        edu, ecats, _ = self.scrub(["education"], text)
        self.assertIn("7731904", core)
        self.assertNotIn("7731904", edu)
        self.assertNotIn("private-path-mention", ccats)
        self.assertEqual(ecats.get("private-path-mention"), 1)
        self.assertNotIn("registrar", privacy_scan.RuleSet([]).institution_words)
        self.assertIn("registrar", privacy_scan.RuleSet(["education"]).institution_words)

    def test_a_pack_file_adds_ids_paths_words_and_columns(self):
        school = str(self.dir / "school.yaml")
        out, cats, _ = self.scrub(["education", school], "badge QX-123456 under sealed-records/a.md")
        self.assertNotIn("QX-123456", out)
        self.assertEqual(cats.get("private-path-mention"), 1)
        rs = privacy_scan.RuleSet(["education", school])
        self.assertIn("zorbmoot", rs.institution_words)
        self.assertEqual(rs.column_denylist, ["student id", "sis id", "learner code"])
        self.assertIn("QX-123456", self.scrub(["education"], "badge QX-123456")[0])

    def test_anonymous_id_pairing_needs_a_pack_and_the_allowlist_can_turn_it_off(self):
        school = str(self.dir / "school.yaml")
        (self.dir / "names.yaml").write_text("version: 1\n")
        sc = privacy_scan.Scanner(self.dir / "names.yaml", rules=privacy_scan.RuleSet([school]))
        self.assertTrue(sc.pairing_on)
        off = privacy_scan.Scanner(self.dir / "names.yaml", rules=privacy_scan.RuleSet([]))
        self.assertFalse(off.pairing_on)
        (self.dir / "nopair.yaml").write_text("version: 1\nanon_id_pairing: false\n")
        self.assertFalse(privacy_scan.Scanner(self.dir / "nopair.yaml",
                                              rules=privacy_scan.RuleSet([school])).pairing_on)

    def test_a_pack_that_cannot_load_is_an_error(self):
        (self.dir / "typo.yaml").write_text("id_paterns: ['x']\n")
        (self.dir / "badre.yaml").write_text("id_patterns: ['(']\n")
        for packs in (["missing-pack.yaml"], [str(self.dir / "nope.yaml")], [str(self.dir / "typo.yaml")],
                      [str(self.dir / "badre.yaml")], ["no-such-builtin"]):
            with self.subTest(packs=packs), self.assertRaises((ValueError, KeyError)):
                privacy_scan.RuleSet(packs)

    def test_a_bad_rule_is_refused_at_load_naming_the_pack_and_rule(self):
        (self.dir / "nogroup.yaml").write_text(
            "anon_ids:\n  - pattern: 'ANON-\\d+'\n    pairing: 'ANON-\\d+ is \\w+'\n")
        (self.dir / "badpath.yaml").write_text("private_paths: ['ok-dir/', 'bad[']\n")
        for pack, where in (("nogroup.yaml", "anon_ids[0].pairing"), ("badpath.yaml", "private_paths[1]")):
            with self.subTest(pack=pack), self.assertRaises(ValueError) as cm:
                privacy_scan.RuleSet([str(self.dir / pack)])
            self.assertIn(pack, str(cm.exception))
            self.assertIn(where, str(cm.exception))
        # a pairing rule that captures the name loads and scans
        (self.dir / "good.yaml").write_text(
            "anon_ids:\n  - pattern: 'ANON-\\d+'\n    pairing: 'ANON-\\d+ is (\\w+)'\n")
        sc = privacy_scan.Scanner(self.dir / "allow.yaml", rules=privacy_scan.RuleSet([str(self.dir / "good.yaml")]))
        sc.scan_text("ANON-12 is here")

    def test_the_rule_set_joins_the_version(self):
        v = {name: self.scanner(p).version for name, p in
             [("core", []), ("edu", ["education"]), ("school", ["education", str(self.dir / "school.yaml")])]}
        self.assertEqual(len(set(v.values())), 3)
        # Pack order does not matter for set-like keys, so reordering is not a rescan.
        a = privacy_scan.RuleSet(["education", str(self.dir / "school.yaml")]).fingerprint
        b = privacy_scan.RuleSet([str(self.dir / "school.yaml"), "education"]).fingerprint
        self.assertEqual(a, b)


class ProtectedGroups(unittest.TestCase):
    SCHOOL = {"scanner": {"rule_packs": ["education", "school.yaml"]}}

    def setUp(self):
        self.dir = tmpdir(self)
        self.saved = refresh.SETTINGS
        self.addCleanup(refresh.configure, self.saved)
        self.tags = str(REPO / "tests" / "fixtures" / "tags.yaml")
        (self.dir / "school.yaml").write_text(PRIVATE_PACK)
        (self.dir / "ok.yaml").write_text("version: 1\n")
        (self.dir / "kp.yaml").write_text("version: 1\nkeep_patterns: ['Room \\\\d+']\n")
        (self.dir / "pn.yaml").write_text("version: 1\nperson_names: false\n")

    def configure(self, **raw):
        refresh.configure(settings.indexer(raw=raw, cfg_dir=self.dir))

    def refusal(self, allow, group, **extra):
        """The refusal message, or None when the index may refresh."""
        try:
            refresh.Index("t", dict({"group": group, "privacy": {"allowlist": str(self.dir / allow)},
                                     "tags": self.tags}, **extra), {})
            return None
        except ValueError as e:
            return str(e)

    def test_a_respelt_protected_group_takes_the_same_scan_rules(self):
        self.configure()                         # no id rules: a ferpa index is refused
        for spelling in ("FERPA", " ferpa ", "Ferpa"):
            self.assertIn("id_patterns and anon_ids", self.refusal("ok.yaml", spelling) or "", spelling)
            self.assertIn("person_names", self.refusal("pn.yaml", spelling) or "", spelling)

    def test_anonymous_id_pairing_cannot_be_turned_off_for_a_protected_index(self):
        self.configure(**self.SCHOOL)
        (self.dir / "np.yaml").write_text("version: 1\nanon_id_pairing: false\n")
        self.assertIn("anon_id_pairing", self.refusal("np.yaml", "ferpa") or "")
        self.assertIsNone(self.refusal("np.yaml", "research"))

    def test_a_column_denylist_override_must_keep_the_composed_list(self):
        self.configure(**self.SCHOOL)
        short = {"doc_options": {"column_denylist": ["email"]}}
        self.assertIn("column_denylist", self.refusal("ok.yaml", "ferpa", **short) or "")
        self.assertIsNone(self.refusal("ok.yaml", "research", **short))
        full = {"doc_options": {"column_denylist": list(refresh.COLUMN_DENYLIST) + ["badge"]}}
        self.assertIsNone(self.refusal("ok.yaml", "ferpa", **full))

    def test_a_column_keep_cannot_readmit_a_denied_column_of_a_protected_index(self):
        # A keep word re-admits every denied header that also holds it ("Course Email" under
        # keep "course"), so a protected index may not set one; entries that normalize to
        # nothing are no keep at all.
        self.configure(**self.SCHOOL)
        for keep in (["Student_ID"], ["course"]):
            self.assertIn("column_keep", self.refusal("ok.yaml", "ferpa", doc_options={"column_keep": keep})
                          or "", keep)
            self.assertIsNone(self.refusal("ok.yaml", "research", doc_options={"column_keep": keep}))
        self.assertIsNone(self.refusal("ok.yaml", "ferpa", doc_options={"column_keep": [" ", "__"]}))

    def test_ferpa_is_protected_by_default(self):
        self.configure(**self.SCHOOL)
        self.assertIsNone(self.refusal("ok.yaml", "ferpa"))
        self.assertIn("keep_patterns", self.refusal("kp.yaml", "ferpa"))
        self.assertIn("person_names", self.refusal("pn.yaml", "ferpa"))
        self.assertIsNone(self.refusal("kp.yaml", "research"))

    def test_the_protected_list_is_a_setting(self):
        self.configure(groups={"protected": ["sealed"]}, **self.SCHOOL)
        self.assertIsNotNone(self.refusal("kp.yaml", "sealed"))
        self.assertIsNotNone(self.refusal("pn.yaml", "sealed"))
        self.assertIsNone(self.refusal("kp.yaml", "ferpa"))

    def test_a_protected_index_without_id_rules_refuses_and_names_the_setting(self):
        self.configure()                         # core + education: no id patterns, no anon ids
        msg = self.refusal("ok.yaml", "ferpa")
        self.assertIn("id_patterns and anon_ids", msg)
        self.assertIn("[scanner] rule_packs", msg)
        self.assertIsNone(self.refusal("ok.yaml", "research"))
        (self.dir / "ids-only.yaml").write_text("id_patterns: ['\\bQX-\\d{6}\\b']\n")
        self.configure(scanner={"rule_packs": ["education", "ids-only.yaml"]})
        msg = self.refusal("ok.yaml", "ferpa")
        self.assertIn("anon_ids", msg)
        self.assertNotIn("id_patterns", msg)
        self.configure(**self.SCHOOL)
        self.assertIsNone(self.refusal("ok.yaml", "ferpa"))

    def test_protected_ready_takes_the_rules_alone(self):
        ok, reason = privacy_scan.protected_ready(privacy_scan.RuleSet())
        self.assertFalse(ok)
        self.assertIn("[scanner] rule_packs", reason)
        ok, reason = privacy_scan.protected_ready(
            privacy_scan.RuleSet(["education", str(self.dir / "school.yaml")]))
        self.assertEqual((ok, reason), (True, ""))

    def test_the_check_is_a_function_for_verify(self):
        self.configure()
        sc = privacy_scan.Scanner(self.dir / "ok.yaml", rules=privacy_scan.RuleSet(["education"]))
        self.assertEqual(len(refresh.protection_problems("ferpa", sc)), 1)
        self.assertEqual(refresh.protection_problems("research", sc), [])
        school = privacy_scan.RuleSet(["education", str(self.dir / "school.yaml")])
        self.assertEqual(refresh.protection_problems(
            "ferpa", privacy_scan.Scanner(self.dir / "ok.yaml", rules=school)), [])


class PacksFailClosed(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir(self)
        self.addCleanup(refresh.configure, refresh.SETTINGS)
        self.addCleanup(setattr, refresh, "SETTINGS_ERROR", refresh.SETTINGS_ERROR)
        (self.dir / "school.yaml").write_text(PRIVATE_PACK)
        (self.dir / "ok.yaml").write_text("version: 1\n")
        self.entry = {"group": "research", "privacy": {"allowlist": str(self.dir / "ok.yaml")},
                      "tags": str(REPO / "tests" / "fixtures" / "tags.yaml")}
        self.raw = {"scanner": {"rule_packs": ["education", "school.yaml"]}}

    def test_a_missing_or_broken_pack_is_an_error_at_load(self):
        for body in (None, "id_patterns: [\n", "- a list\n"):
            with self.subTest(body=body):
                p = self.dir / "school.yaml"
                p.unlink(missing_ok=True)
                if body is not None:
                    p.write_text(body)
                with self.assertRaises(ValueError):
                    refresh.configure(settings.indexer(raw=self.raw, cfg_dir=self.dir))

    def test_a_pack_removed_after_import_stops_the_index(self):
        refresh.configure(settings.indexer(raw=self.raw, cfg_dir=self.dir))
        refresh.Index("t", self.entry, {})
        (self.dir / "school.yaml").unlink()
        with self.assertRaises(ValueError):
            refresh.Index("t", self.entry, {})

    def test_a_settings_error_stops_every_index(self):
        refresh.configure(settings.indexer(raw={}, cfg_dir=self.dir))
        refresh.SETTINGS_ERROR = ValueError("rule pack x cannot be read")
        with self.assertRaises(ValueError):
            refresh.Index("t", self.entry, {})


class ColumnsReachConversion(unittest.TestCase):
    """The composed column denylist reaches convert_docs through the refresh driver."""

    class PassScanner:
        version = "pass"

        def scan_groups(self, groups, triage=None):
            return [(list(g), {}, None) for g in groups]

    def convert(self, raw):
        d = tmpdir(self)
        self.addCleanup(refresh.configure, refresh.SETTINGS)
        # The synthetic school's id-number column is headed "900".
        (d / "school.yaml").write_text(PRIVATE_PACK.replace("[learner code]", "['900']"))
        refresh.configure(settings.indexer(raw=raw, cfg_dir=d))
        (d / "src").mkdir()
        (d / "src" / "marks.csv").write_text("Course,SIS ID,900,Score\nGARDEN-1,X1,Y2,88\n")
        ix = refresh.Index.__new__(refresh.Index)
        ix.entry = {}
        doc_corpus.convert_docs([{"path": str(d / "src"), "include": ["*.csv"], "label": "n"}],
                                d / "corpus", d / "m.json", self.PassScanner(), opts=ix.doc_options())
        return (d / "corpus" / "n" / "marks.csv.md").read_text()

    def test_default_packs_drop_sis_id(self):
        t = self.convert({})
        self.assertNotIn("SIS ID", t)
        self.assertIn("Score", t)
        self.assertIn("900", t)

    def test_a_private_pack_drops_its_columns(self):
        t = self.convert({"scanner": {"rule_packs": ["education", "school.yaml"]}})
        self.assertNotIn("SIS ID", t)
        self.assertNotIn("| 900", t)
        self.assertNotIn("Y2", t)
        self.assertIn("Score", t)


class Configure(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir(self)
        self.addCleanup(refresh.configure, refresh.SETTINGS)

    def test_columns_compose_from_packs_unless_settings_pin_them(self):
        (self.dir / "school.yaml").write_text(PRIVATE_PACK)
        refresh.configure(settings.indexer(raw={"scanner": {"rule_packs": ["education", "school.yaml"]}},
                                           cfg_dir=self.dir))
        self.assertEqual(refresh.COLUMN_DENYLIST,
                         doc_corpus.DEFAULT_OPTS["column_denylist"] + ["student id", "sis id", "learner code"])
        refresh.configure(settings.indexer(raw={"docs": {"column_denylist": ["badge", "email"]}},
                                           cfg_dir=self.dir))
        self.assertEqual(refresh.COLUMN_DENYLIST, ["badge", "email"])
        ix = refresh.Index.__new__(refresh.Index)
        ix.entry = {"doc_options": {"row_cap": 9}}
        self.assertEqual(ix.doc_options(), {"column_denylist": ["badge", "email"], "row_cap": 9})
        ix.entry = {"doc_options": {"column_denylist": ["own"]}}
        self.assertEqual(ix.doc_options()["column_denylist"], ["own"])

    def test_paths_and_zone_come_from_settings(self):
        refresh.configure(settings.indexer(raw={"timezone": "Pacific/Auckland",
                                                "paths": {"index_root": str(self.dir / "ix")}},
                                           cfg_dir=self.dir))
        self.assertEqual(refresh.ROOT, self.dir / "ix")
        self.assertEqual(refresh.REGISTRY, self.dir / "ix" / "registry.yaml")
        self.assertEqual(refresh.PARSE_RULES.tz, ZoneInfo("Pacific/Auckland"))
        self.assertTrue(refresh.now_iso().endswith(("+13:00", "+12:00")))


class Discover(unittest.TestCase):
    def test_group_guesses_come_from_settings(self):
        d = tmpdir(self)
        self.addCleanup(refresh.configure, refresh.SETTINGS)
        for slug in ("-home-user-work-alpha", "-home-user-play-beta"):
            (d / "projects" / slug).mkdir(parents=True)
            (d / "projects" / slug / "s.jsonl").write_text("{}\n")
        raw = {"paths": {"projects_dir": str(d / "projects")},
               "groups": {"guess": {"-work-": "research"}}}
        refresh.configure(settings.indexer(raw=raw, cfg_dir=d))
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            refresh.cmd_discover({"indexes": {}})
        rows = {ln.split()[-1]: ln.split()[2] for ln in buf.getvalue().splitlines() if ln.strip().endswith(("alpha", "beta"))}
        self.assertEqual(rows, {"-home-user-work-alpha": "research", "-home-user-play-beta": "unknown"})


class DayFolders(unittest.TestCase):
    def test_the_zone_decides_the_day_folder_and_the_local_time(self):
        rec = dict(n=1, ex="EX", start_offset=0, start_line=1, end_line=1, sender_kind="owner",
                   sender_canon="owner", sender_name="User", ts="2026-05-01T20:00:00Z",
                   user_text="late note", assistant=[], tools={}, interrupted=False,
                   session="0f1e2d3c-4b5a-6978-8091-a2b3c4d5e6f7", id="S0f1e2d-EX-0001")
        tc = session_tagger.TagConfig(TAGS)
        for zone, day, local in [("Asia/Tokyo", "20260502", "2026-05-02 05:00 JST"),
                                 ("America/Los_Angeles", "20260501", "2026-05-01 13:00 PDT")]:
            out = tmpdir(self)
            rules = session_parse.TranscriptRules(tz=ZoneInfo(zone))
            session_tagger.write_corpus([rec], {}, tc, out, rules)
            files = list(out.rglob("*.md"))
            self.assertEqual([f.parent.name for f in files], [day], zone)
            self.assertIn("| %s |" % local, files[0].read_text())


class YamlLibrary(unittest.TestCase):
    ITEMS = {"items": [
        {"title": "Lantern kit", "description": "Solar lantern parts.", "maker": "Acme",
         "size": {"min": 2, "max": 4}, "tags": ["light", "garden"]},
        {"title": "Seed tray", "description": "Twelve cells.", "size": {"min": 1, "max": 1}},
        {"title": "Mystery box"}]}

    def chunks(self, root_cfg):
        d = tmpdir(self)
        (d / "library.yaml").write_text(yaml.safe_dump(self.ITEMS))
        return engine.scan_yaml_library({}, dict(name="lib", **root_cfg), d / "library.yaml",
                                        Path("library.yaml"))

    def test_default_lines_are_title_description_and_tags(self):
        c = self.chunks({})
        self.assertEqual(c[0]["text"], "Lantern kit\nSolar lantern parts.\nTags: light, garden.")
        self.assertEqual(c[1]["text"], "Seed tray\nTwelve cells.")
        self.assertEqual((c[0]["heading"], c[0]["kind"], c[0]["level"]), ("Lantern kit", "catalog", None))

    def test_configured_lines_and_ranges(self):
        cfg = {"lines": [{"template": "Maker: {maker}. {size}."},
                         {"template": "Tags: {tags}.", "if": "tags"}],
               "ranges": {"size": {"one": "size {min}", "many": "sizes {min} to {max}",
                                   "none": "size not stated"}}}
        c = self.chunks(cfg)
        self.assertEqual(c[0]["text"], "Lantern kit\nSolar lantern parts.\nMaker: Acme. sizes 2 to 4.\n"
                                       "Tags: light, garden.")
        self.assertEqual(c[1]["text"], "Seed tray\nTwelve cells.\nMaker: None. size 1.")
        self.assertEqual(c[2]["text"], "Mystery box\nMaker: None. size not stated.")

    def test_the_older_root_key_is_still_accepted(self):
        cfg = engine.resolve_config({"dnd_root": "/tmp/x", "index_path": "i.sqlite"})
        self.assertEqual(cfg["_index_path"], Path("/tmp/x/i.sqlite"))


class TextutilPlatform(unittest.TestCase):
    def test_textutil_is_refused_off_macos(self):
        saved = doc_corpus.sys.platform
        self.addCleanup(setattr, doc_corpus.sys, "platform", saved)
        doc_corpus.sys.platform = "linux"
        with self.assertRaises(RuntimeError):
            doc_corpus.textutil_txt(Path("x.doc"), doc_corpus.Ctx())


class TaggerPrompt(unittest.TestCase):
    """The prompt text is the tag-cache version: a Claude index's must never change, or every
    Claude index re-tags."""

    def test_the_claude_prompt_hash_is_unchanged(self):
        self.assertEqual(session_tagger.TagConfig(TAGS).version, "ab5e0b2866")
        self.assertEqual(session_tagger.TagConfig(TAGS, "summary: say what changed.").version,
                         "0c0f96a194")
        self.assertEqual(session_tagger.TagConfig(TAGS, source="claude").version, "ab5e0b2866")

    def test_other_sources_name_their_provider(self):
        claude = session_tagger.TagConfig(TAGS).system
        for source, words in (("codex", "a Codex CLI session."),
                              ("antigravity", "an Antigravity CLI conversation.")):
            tc = session_tagger.TagConfig(TAGS, source=source)
            self.assertIn("You tag exchanges from the transcript of %s" % words, tc.system)
            self.assertNotIn("Claude", tc.system)
            self.assertNotEqual(tc.version, "ab5e0b2866")
            self.assertEqual(tc.system.replace(words, "a Claude Code session.").replace(
                "a peer session)", "a peer Claude session)"), claude)
        with self.assertRaises(ValueError):
            session_tagger.TagConfig(TAGS, source="gemini")


if __name__ == "__main__":
    unittest.main()


E2E_CONFIG = textwrap.dedent("""\
    corpus_root: "{root}"
    index_path: "index/demo-session.sqlite"
    level_folder_pattern: "^(\\\\d{{8}})()$"
    chunking: {{max_chars: 1500, min_chars: 25}}
    similarity: {{backend: tfidf, model: none, max_features: 5000, window_chars: 1200,
                  window_overlap: 300, rrf_k: 60}}
    dedupe: {{enabled: false}}
    tag_line_prefix: "Tags:"
    roots:
      - {{name: session, path: corpus/session, default_kind: exchange}}
    """)


class EndToEndWithNoSettings(unittest.TestCase):
    """A fabricated session index refreshed and searched with an empty config folder: every
    path, the owner, the zone and the rules are defaults."""

    def test_refresh_and_search(self):
        try:
            privacy_scan.nlp()
        except Exception as e:          # the memory extra is not installed
            self.skipTest("spaCy unavailable: %s" % type(e).__name__)
        base = tmpdir(self)
        cfg, data, proj = base / "config", base / "data", base / "projects" / "-demo"
        cfg.mkdir()
        proj.mkdir(parents=True)
        sid = "aa11bb22-0000-4000-8000-000000000001"

        def line(d):
            return json.dumps(d, separators=(",", ":")) + "\n"
        (proj / (sid + ".jsonl")).write_text(
            line(dict(type="user", message=dict(role="user", content="Why is the gate lantern dim?"),
                      timestamp="2026-04-01T16:00:00Z"))
            + line(dict(type="assistant", message=dict(id="m1", content=[
                dict(type="text", text="The lantern battery is flat; swap the cell.")]),
                timestamp="2026-04-01T16:00:05Z"))
            + line(dict(type="user", message=dict(role="user", content="HEARTBEAT: check the beds"),
                        timestamp="2026-04-01T17:00:00Z", isMeta=True)))
        old = 1_700_000_000
        os.utime(proj / (sid + ".jsonl"), (old, old))
        root = cfg / "indexes"                              # registry and per-index config
        ix = root / "demo-session"
        dix = data / "fleetwatch" / "indexes" / "demo-session"   # corpus, index, state
        ix.mkdir(parents=True)
        (root / "registry.yaml").write_text(textwrap.dedent("""\
            defaults: {tag: true, tag_budget_per_run: 0}
            indexes:
              demo-session:
                group: research
                transcripts: {project_dir: "%s", sessions: all}
                privacy: {allowlist: demo-session/scan_allowlist.yaml}
                tags: demo-session/tags.yaml
            """ % proj))
        (ix / "scan_allowlist.yaml").write_text("version: 1\n")
        (ix / "tags.yaml").write_text(yaml.safe_dump(TAGS))
        (ix / "search_config.yaml").write_text(E2E_CONFIG.format(root=dix))
        sessions = base / "sessions"
        sessions.mkdir()
        env = {k: v for k, v in os.environ.items() if k not in ("FLEETWATCH_PROJECTS_DIR", "TZ")}
        env.update(FLEETWATCH_CONFIG_DIR=str(cfg), XDG_DATA_HOME=str(data), TZ="UTC",
                   FLEETWATCH_SESSIONS_DIR=str(sessions), FLEETWATCH_NO_MIGRATE="1",
                   PYTHONPATH=str(REPO) + os.pathsep + env.get("PYTHONPATH", ""))
        run = lambda *argv: subprocess.run([sys.executable, *argv], capture_output=True, text=True,
                                           env=env, cwd=str(base), timeout=300)
        p = run("-c", "from fleetwatch.memory import refresh; refresh.main(['--index', 'demo-session', '--no-tag'])")
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr[-2000:])
        self.assertIn("2 exchanges (2 new)", p.stdout)
        files = sorted(f.relative_to(dix / "corpus" / "session") for f in (dix / "corpus" / "session").rglob("*.md"))
        self.assertEqual([str(f) for f in files],
                         ["2026-04/20260401/Saa11bb-EX-0001.owner.md",
                          "2026-04/20260401/Saa11bb-EX-0002.heartbeat.md"])
        text = (dix / "corpus" / "session" / files[0]).read_text()
        self.assertIn("| 2026-04-01 16:00 UTC | from User", text)
        self.assertTrue((dix / "index" / "demo-session.sqlite").exists())
        q = run("-m", "fleetwatch", "search", "--index", "demo-session", "lantern battery", "--json")
        self.assertEqual(q.returncode, 0, q.stderr[-2000:])
        hits = json.loads(q.stdout)
        self.assertTrue(hits and hits[0]["path"].endswith("Saa11bb-EX-0001.owner.md"), q.stdout[:500])
