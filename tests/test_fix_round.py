"""Regression tests for agent placement and launch trust constraints."""
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fleetwatch import index_migrate, indexes, launch
from fleetwatch.memory import codex_parse, routing, session_tagger
from fleetwatch.search import engine


class RoutingRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = str(self.root / "work")
        self.protected = str(self.root / "work" / "private")
        self.reg = {"indexes": {
            "work": {"group": "work", "owner_session_id": "work-id"},
            "private": {"group": "ferpa", "owner_session_id": "private-id"},
        }}
        self.folders = {"work": ([self.work], set()),
                        "private": ([self.protected], set())}
        self.run = routing.AgentRun("codex", "run-id", self.root / "run", self.protected, 1000)

    def entry(self, sid, folder, ts=900, tool="codex"):
        return {"session_id": sid, "cwd": folder, "ts_epoch": ts, "tool": tool}

    def place(self, ledger, folder=None):
        run = self.run if folder is None else routing.AgentRun(
            "codex", "run-id", self.root / "run", folder, 1000)
        return routing.resolve_run(run, self.reg, ledger, self.folders,
                                   catch_all="private", git_lookup=lambda _: None)

    def test_specific_launch_wins_and_protection_does_not_decrease(self):
        got = self.place([self.entry("private-id", self.protected, 900),
                          self.entry("work-id", self.work, 950)])
        self.assertEqual(got.index, "private")

    def test_older_specific_launch_beats_newer_parent_in_same_group(self):
        sub = str(self.root / "work" / "sub")
        self.reg["indexes"]["sub"] = {"group": "work", "owner_session_id": "sub-id"}
        self.folders["sub"] = ([sub], set())
        got = self.place([self.entry("sub-id", sub, 900),
                          self.entry("work-id", self.work, 950)], str(Path(sub) / "child"))
        self.assertEqual((got.index, got.reason), ("sub", routing.REASON_LAUNCH))
        tie = self.place([self.entry("sub-id", sub, 900),
                          self.entry("work-id", sub, 950)], str(Path(sub) / "child"))
        self.assertEqual((tie.index, tie.reason), ("work", routing.REASON_LAUNCH))

    def test_parent_launch_cannot_override_protected_folder(self):
        self.assertEqual(self.place([self.entry("work-id", self.work)]).index, "private")

    def test_home_folder_index_does_not_absorb_runs(self):
        self.reg["indexes"]["home"] = {"group": "work", "owner_session_id": "home-id"}
        self.folders["home"] = ([str(self.root)], set())
        with mock.patch("pathlib.Path.home", return_value=self.root):
            got = self.place([], str(self.root / "unknown"))
            self.assertEqual(got.index, "private")
            self.assertEqual(got.reason, routing.REASON_CATCH_ALL)

    def test_ambiguous_launcher_groups_go_to_catch_all(self):
        folder = str(self.root / "unowned")
        got = self.place([self.entry("work-id", folder, 900),
                          self.entry("private-id", folder, 910)], folder)
        self.assertEqual((got.index, got.reason), ("private", routing.REASON_CATCH_ALL))
        protected = self.place([self.entry("work-id", self.protected, 900),
                                self.entry("private-id", self.protected, 910)])
        self.assertEqual((protected.index, protected.reason), ("private", routing.REASON_FOLDER))

    def test_tool_must_match_provider(self):
        folder = str(self.root / "unowned")
        got = self.place([self.entry("work-id", folder, tool="agy")], folder)
        self.assertEqual((got.index, got.reason), ("private", routing.REASON_CATCH_ALL))

    def test_read_rejects_ancestor_of_session_cwd(self):
        entry = self.entry("work-id", self.work)
        entry["session_cwd"] = self.protected
        got = self.place([entry], str(self.root / "work" / "other"))
        self.assertEqual((got.index, got.reason), ("work", routing.REASON_FOLDER))

    def test_multi_review_places_runs_beneath_work_root(self):
        root = str(self.root / "review" / "work" / "worktrees")
        got = self.place([self.entry("work-id", root, tool="multi-review")],
                         str(Path(root) / "wt-1"))
        self.assertEqual((got.index, got.reason), ("work", routing.REASON_LAUNCH))

    def test_stored_placement_moves_only_to_protected_folder(self):
        for name, folder in (("work", self.work), ("private", self.protected)):
            self.reg["indexes"][name]["transcripts"] = {"project_dir": folder}
        settings = mock.Mock(catch_all_index=None, protected_groups=frozenset({"ferpa"}))
        mapping = {"work": ([self.work], set()), "private": ([self.protected], set())}
        path = self.root / "placements.json"
        with mock.patch.object(routing, "index_project_folders",
                               side_effect=lambda entry, **_: mapping[
                                   "private" if entry["group"] == "ferpa" else "work"]):
            def place(folder):
                run = routing.AgentRun("codex", "run-id", self.root / "run", folder, 1000)
                return routing.resolve_all(self.reg, settings, {}, ledger=[], runs=[run],
                                           placement_path=path).placements[0]
            self.assertEqual(place(self.work).index, "work")
            self.assertEqual(place(self.protected).index, "private")
            self.assertEqual(place(self.work).index, "private")


class LaunchRegressions(unittest.TestCase):
    def test_newline_is_command_separator_without_apostrophe(self):
        self.assertEqual(launch.parse_command("cd /tmp\ncodex exec hi"), ("codex", None))

    def test_help_and_version_do_not_record(self):
        for command in ("agy --version --worktree /", "codex exec --help --worktree /"):
            with self.subTest(command=command):
                self.assertIsNone(launch.parse_command(command))

    def test_typo_does_not_fail_hook(self):
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(launch.main(["recrod"]), 0)

    def test_rejects_parent_and_home_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = str(Path(tmp) / "repo")
            payload = {"session_id": "sid", "cwd": cwd,
                       "tool_input": {"command": "codex exec --worktree / hi"}}
            with mock.patch.object(indexes, "calling_session", return_value=("A", "sid")):
                self.assertIsNone(launch.record_payload(payload, ledger=str(Path(tmp) / "ledger")))

    def test_forged_session_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"session_id": "forged", "cwd": tmp,
                       "tool_input": {"command": "codex exec hi"}}
            with mock.patch.object(indexes, "calling_session", return_value=("A", "real")):
                self.assertIsNone(launch.record_payload(payload, ledger=str(Path(tmp) / "ledger")))

    def test_multi_review_uses_work_root(self):
        command = "/opt/tools/multi-review.sh --repo /tmp/project"
        self.assertEqual(launch.parse_command(command),
                         ("multi-review", "/opt/tools/work/worktrees"))


class MetadataRecovery(unittest.TestCase):
    def test_routing_state_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = os.umask(0o022)
            try:
                routing._save_meta_cache(root / "meta" / "cache.json", {"a": {"folder": "/x"}})
                routing._save_placements(root / "placement" / "placements.json", {"a": {"index": "x"}})
            finally:
                os.umask(old)
            for folder, name in (("meta", "cache.json"), ("placement", "placements.json")):
                self.assertEqual((root / folder).stat().st_mode & 0o077, 0)
                self.assertEqual((root / folder / name).stat().st_mode & 0o077, 0)

    @unittest.skipUnless(shutil.which("zstd"), "zstd executable required")
    def test_missing_zstd_is_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex = root / "codex" / "2026" / "04" / "01"
            codex.mkdir(parents=True)
            sid = "01a1fbfa-efba-7173-bb5c-6fc0b903b19a"
            path = codex / ("rollout-2026-04-01T10-00-00-%s.jsonl" % sid)
            path.write_text('{"type":"session_meta","timestamp":"2026-04-01T10:00:00Z",'
                            '"payload":{"id":"%s","cwd":"/tmp/project"}}\n' % sid)
            subprocess.run(["zstd", "-q", "--rm", str(path)], check=True)
            source = root / "codex"
            settings = mock.Mock(source_dirs=lambda provider, default=None: [source])
            cache = root / "cache.json"
            with mock.patch.object(codex_parse.shutil, "which", return_value=None):
                first, _ = routing.discover_runs({"codex": codex_parse.CodexSource},
                                                 settings, cache_path=cache)
            self.assertEqual(first, [])
            recovered, _ = routing.discover_runs({"codex": codex_parse.CodexSource},
                                                 settings, cache_path=cache)
            self.assertEqual(recovered[0].folder, "/tmp/project")


class SearchCompatibility(unittest.TestCase):
    def test_plain_search_does_not_prepare_provider_table(self):
        ix = mock.Mock()
        with mock.patch.object(engine, "run_query", return_value=([], None, None, "q")), \
                mock.patch.object(engine, "label_mixed_results"), \
                mock.patch.object(engine, "prepare_providers") as prepare:
            engine.search(ix, "q", engine.SearchOptions())
            prepare.assert_not_called()
            engine.search(ix, "q", engine.SearchOptions(provider=["codex"]))
            prepare.assert_called_once()

    def test_compare_old_uses_provider_tag_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "old"
            (old / "agent").mkdir(parents=True)
            (old / "allow.yaml").write_text("version: 1\n")
            spec = {"description": "x", "areas": {"a": "a"}, "kinds": {"k": "k"}}
            (old / "tags.yaml").write_text("description: x\nareas: {a: a}\nkinds: {k: k}\n")
            entry = {"privacy": {"allowlist": "allow.yaml"}, "tags": "tags.yaml",
                     "transcripts": {"source": "codex", "path": str(root / "sessions")}}
            indexer = mock.Mock()
            indexer.SETTINGS.rule_packs = []
            indexer.SETTINGS.default_guidance = None
            indexer.load_registry.return_value = {"indexes": {"agent": entry}}
            indexer.source_name = lambda t: t.get("source", "claude")
            flags = {}
            with mock.patch.object(indexes, "refresh", return_value=indexer), \
                    mock.patch.object(index_migrate, "corpus_digest", return_value=("sha", 0)), \
                    mock.patch.object(index_migrate, "counts", return_value=(0, 0)), \
                    mock.patch.object(index_migrate, "stored_versions", return_value=(0, 0, 0, 0)):
                index_migrate._compare_old(mock.Mock(), "agent", old, mock.Mock(version="scan"),
                    session_tagger.TagConfig(spec, source="codex"), "scan|opts",
                    ("sha", 0, 0, 0), None,
                    lambda label, passed, *details: flags.__setitem__(label, passed))
            self.assertTrue(flags["TagConfig.version equal to the old index's"])
