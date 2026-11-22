"""Synthetic tests for provider-agnostic agent routing: resolver branches (a) launch ledger,
(b) project folder plus fake-git worktree resolution, (c) protected catch-all or unplaced,
settings validation, and plugging in another provider source via run_meta()."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")

from fleetwatch import launch, settings
from fleetwatch.memory import codex_parse, routing

SID_A = "aa11bb22-0000-4000-8000-000000000001"
SID_B = "cc33dd44-0000-4000-8000-000000000002"
SID_C = "ee55ff66-0000-4000-8000-000000000003"
CODEX_1 = "01a1fbfa-efba-7173-bb5c-6fc0b903b19a"
CODEX_2 = "01a1fbfb-0000-7000-8000-00000000abcd"
CODEX_3 = "01a1fbfc-0000-7000-8000-00000000ef01"


def tmp(test):
    d = tempfile.TemporaryDirectory()
    test.addCleanup(d.cleanup)
    return Path(d.name).resolve()


def write_claude_transcript(pdir, sid, cwd=None, text="Hello"):
    pdir.mkdir(parents=True, exist_ok=True)
    rec = {"type": "user", "message": {"role": "user", "content": text},
           "timestamp": "2026-04-01T10:00:00Z"}
    if cwd is not None:
        rec["cwd"] = str(cwd)
    p = pdir / (sid + ".jsonl")
    p.write_text(json.dumps(rec) + "\n")
    return p


def write_codex_rollout(root, sid, cwd, ts="2026-04-01T10:05:00.000Z", prompt="Run check",
                        reply="Done."):
    day = root / "2026" / "04" / "01"
    day.mkdir(parents=True, exist_ok=True)
    ts_name = ts[:19].replace(":", "-")
    p = day / ("rollout-%s-%s.jsonl" % (ts_name, sid))
    lines = [
        json.dumps({"timestamp": ts, "type": "session_meta",
                    "payload": {"id": sid, "timestamp": ts, "cwd": str(cwd)}}),
        json.dumps({"timestamp": ts, "type": "response_item",
                    "payload": {"type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": prompt}]}}),
        json.dumps({"timestamp": ts, "type": "response_item",
                    "payload": {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": reply}]}}),
    ]
    p.write_text("\n".join(lines) + "\n")
    old = 1_700_000_000
    os.utime(p, (old, old))
    return p


def make_fake_git(folder, mapping, log_path=None):
    """Executable stand-in for `git -C <dir> rev-parse --git-common-dir`."""
    script = folder / "fake-git"
    body = [
        "#!/bin/sh",
        'dir=""',
        'if [ "$1" = "-C" ]; then dir="$2"; shift 2; fi',
    ]
    if log_path is not None:
        body.append('echo "$dir" >> %s' % json.dumps(str(log_path)))
    body.append('case "$dir" in')
    for src, target in mapping.items():
        body.append('  %s) echo %s; exit 0 ;;' % (json.dumps(str(src)), json.dumps(str(target))))
    body.append('  *) exit 128 ;;')
    body.append('esac')
    script.write_text("\n".join(body) + "\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return [str(script)]


class RoutingSettings(unittest.TestCase):
    def test_routing_table_parses_and_validates(self):
        base = tmp(self)
        s = settings.indexer(raw={
            "routing": {
                "catch_all": "vault",
                "sources": {"codex": str(base / "cx"), "antigravity": [str(base / "ag1"), str(base / "ag2")]},
            }
        }, cfg_dir=base)
        self.assertEqual(s.catch_all_index, "vault")
        self.assertEqual(s.source_dirs("codex"), [base / "cx"])
        self.assertEqual(s.source_dirs("antigravity"), [base / "ag1", base / "ag2"])
        self.assertEqual(s.source_dirs("other", "~/default-other"), [Path.home() / "default-other"])

    def test_empty_source_list_disables_a_provider(self):
        base = tmp(self)
        s = settings.indexer(raw={"routing": {"sources": {"codex": []}}}, cfg_dir=base)
        self.assertEqual(s.source_dirs("codex", "~/.codex/sessions"), [])

    def test_invalid_routing_settings_fail_strictly(self):
        base = tmp(self)
        bad_cases = [
            {"routing": {"unknown_key": "x"}},
            {"routing": {"catch_all": ""}},
            {"routing": {"catch_all": 123}},
            {"routing": {"sources": "not-a-table"}},
            {"routing": {"sources": {"codex": ""}}},
            {"routing": {"sources": {"codex": [123]}}},
        ]
        for raw in bad_cases:
            with self.subTest(raw=raw), self.assertRaises(settings.SettingsError):
                settings.indexer(raw=raw, cfg_dir=base)


class CodexRunMeta(unittest.TestCase):
    def test_reads_cwd_and_timestamp_from_session_meta(self):
        base = tmp(self)
        p = write_codex_rollout(base / "codex", CODEX_1, "/Users/alex/repo/alpha",
                                ts="2026-04-01T10:05:00.000Z")
        cwd, ts = codex_parse.CodexSource().run_meta(p)
        self.assertEqual((cwd, ts), ("/Users/alex/repo/alpha", "2026-04-01T10:05:00.000Z"))

    def test_malformed_or_missing_file_returns_nones(self):
        base = tmp(self)
        bad = base / "bad.jsonl"
        bad.write_text("{not json\n")
        src = codex_parse.CodexSource()
        self.assertEqual(src.run_meta(bad), (None, None))
        self.assertEqual(src.run_meta(base / "missing.jsonl"), (None, None))


class ResolverBranches(unittest.TestCase):
    def setUp(self):
        self.base = tmp(self)
        self.repo_a = self.base / "repos" / "alpha"
        self.repo_b = self.base / "repos" / "beta"
        self.repo_sub = self.repo_a / "nested"
        for d in (self.repo_a, self.repo_b, self.repo_sub):
            d.mkdir(parents=True)
        self.proj_a = self.base / "projects" / "-alpha"
        self.proj_b = self.base / "projects" / "-beta"
        write_claude_transcript(self.proj_a, SID_A, cwd=self.repo_a)
        write_claude_transcript(self.proj_b, SID_B, cwd=self.repo_b)
        self.codex_dir = self.base / "codex"
        self.codex_dir.mkdir()
        self.ledger = self.base / "launches.jsonl"
        self.reg = {
            "groups": {
                "work": {"readers": ["work"]},
                "oss": {"readers": ["oss"]},
                "ferpa": {"readers": []},
            },
            "indexes": {
                "alpha-session": {
                    "group": "work",
                    "owner_session": "Alpha",
                    "owner_session_id": SID_A,
                    "transcripts": {"project_dir": [str(self.proj_a)], "sessions": [SID_A]},
                },
                "beta-session": {
                    "group": "oss",
                    "owner_session": "Beta",
                    "owner_session_id": SID_B,
                    "transcripts": {"project_dir": [str(self.proj_b)], "sessions": [SID_B]},
                },
                "vault-session": {
                    "group": "ferpa",
                    "owner_session": "Vault",
                    "owner_session_id": SID_C,
                },
            },
        }

    def _settings(self, catch_all=None, extra_sources=None):
        sources = {"codex": str(self.codex_dir)}
        if extra_sources:
            sources.update(extra_sources)
        rt = {"sources": sources}
        if catch_all is not None:
            rt["catch_all"] = catch_all
        return settings.indexer(raw={"routing": rt}, cfg_dir=self.base / "cfg")

    def _append_launch(self, sid, cwd, ts, tool="codex", name=None):
        entry = {"session_id": sid, "tool": tool, "cwd": str(cwd), "ts": ts}
        if name:
            entry["session_name"] = name
        launch.append_entry(entry, str(self.ledger))

    def test_branch_a_launch_matches_same_or_parent_folder_and_newest_wins(self):
        # Beta launches in Alpha's repo 3 minutes before the run; launch beats folder fallback.
        self._append_launch(SID_A, self.repo_a, "2026-04-01T10:00:00Z")
        self._append_launch(SID_B, self.repo_a, "2026-04-01T10:02:00Z")
        write_codex_rollout(self.codex_dir, CODEX_1, self.repo_sub, ts="2026-04-01T10:05:00Z")
        plan = routing.resolve_all(self.reg, self._settings(), {"codex": codex_parse.CodexSource},
                                   ledger_path=str(self.ledger))
        self.assertEqual(len(plan.placements), 1)
        self.assertEqual((plan.placements[0].index, plan.placements[0].reason),
                         ("beta-session", routing.REASON_LAUNCH))
        self.assertEqual(plan.counts_for("beta-session"), {"launch": 1, "folder": 0, "catch_all": 0})
        self.assertEqual(len(plan.unplaced), 0)

    def test_branch_a_time_window_boundaries_and_sibling_folder_isolation(self):
        sibling = self.base / "repos" / "alpha-other"
        sibling.mkdir()
        # Sibling folder shares prefix string with repo_a but is not a child.
        self._append_launch(SID_B, self.repo_a, "2026-04-01T10:04:00Z")
        write_codex_rollout(self.codex_dir, CODEX_1, sibling, ts="2026-04-01T10:05:00Z")
        # Launch 601 seconds before run is outside the 10-minute window.
        self._append_launch(SID_B, self.base / "elsewhere", "2026-04-01T09:54:59Z")
        write_codex_rollout(self.codex_dir, CODEX_2, self.base / "elsewhere", ts="2026-04-01T10:05:00Z")
        # Launch 1 second after run start does not match.
        self._append_launch(SID_B, self.base / "after", "2026-04-01T10:05:01Z")
        write_codex_rollout(self.codex_dir, CODEX_3, self.base / "after", ts="2026-04-01T10:05:00Z")
        plan = routing.resolve_all(self.reg, self._settings(), {"codex": codex_parse.CodexSource},
                                   ledger_path=str(self.ledger))
        self.assertEqual(len(plan.unplaced), 3)

        # Exactly 600 seconds before run start DOES match.
        exact_dir = self.base / "exact600"
        self._append_launch(SID_B, exact_dir, "2026-04-01T09:55:00Z")
        write_codex_rollout(self.codex_dir, "01a1fbfd-0000-7000-8000-000000001234", exact_dir,
                            ts="2026-04-01T10:05:00Z")
        plan2 = routing.resolve_all(self.reg, self._settings(), {"codex": codex_parse.CodexSource},
                                    ledger_path=str(self.ledger))
        placed = {p.run.session_id: (p.index, p.reason) for p in plan2.placements}
        self.assertEqual(placed["01a1fbfd-0000-7000-8000-000000001234"],
                         ("beta-session", routing.REASON_LAUNCH))

    def test_branch_b_project_folder_and_worktree_via_fake_git(self):
        wt_live = self.base / "worktrees" / "wt-alpha"
        wt_live.mkdir(parents=True)
        wt_gone = self.base / "worktrees" / "wt-deleted"   # does not exist on disk
        git_log = self.base / "git.log"
        fake_git = make_fake_git(self.base, {
            wt_live: self.repo_a / ".git",
            wt_gone: self.repo_a / ".git",
        }, log_path=git_log)

        write_codex_rollout(self.codex_dir, CODEX_1, self.repo_sub, ts="2026-04-01T11:00:00Z")
        write_codex_rollout(self.codex_dir, CODEX_2, wt_live, ts="2026-04-01T11:05:00Z")
        write_codex_rollout(self.codex_dir, CODEX_3, wt_gone, ts="2026-04-01T11:10:00Z")

        plan = routing.resolve_all(self.reg, self._settings(), {"codex": codex_parse.CodexSource},
                                   ledger_path=str(self.ledger), git_cmd=fake_git)
        by_sid = {p.run.session_id: (p.index, p.reason) for p in plan.placements}
        self.assertEqual(by_sid[CODEX_1], ("alpha-session", routing.REASON_FOLDER))
        self.assertEqual(by_sid[CODEX_2], ("alpha-session", routing.REASON_FOLDER))
        # Deleted worktree must not call git and must remain unplaced.
        self.assertEqual(by_sid[CODEX_3], (None, routing.REASON_UNPLACED))
        logged = git_log.read_text().splitlines()
        self.assertIn(str(wt_live), logged)
        self.assertNotIn(str(wt_gone), logged)
        self.assertEqual(plan.counts_for("alpha-session"), {"launch": 0, "folder": 2, "catch_all": 0})
        self.assertEqual(len(plan.unplaced), 1)

    def test_branch_b_longest_project_folder_wins_and_exact_tie_falls_through(self):
        proj_sub = self.base / "projects" / "-alpha-nested"
        sid_sub = "11223344-0000-4000-8000-000000000009"
        write_claude_transcript(proj_sub, sid_sub, cwd=self.repo_sub)
        reg = dict(self.reg, indexes=dict(self.reg["indexes"], **{
            "nested-session": {
                "group": "work",
                "owner_session": "Nested",
                "owner_session_id": sid_sub,
                "transcripts": {"project_dir": [str(proj_sub)], "sessions": [sid_sub]},
            }
        }))
        write_codex_rollout(self.codex_dir, CODEX_1, self.repo_sub / "pkg", ts="2026-04-01T11:00:00Z")
        plan = routing.resolve_all(reg, self._settings(), {"codex": codex_parse.CodexSource},
                                   ledger_path=str(self.ledger))
        self.assertEqual((plan.placements[0].index, plan.placements[0].reason),
                         ("nested-session", routing.REASON_FOLDER))

        # Two distinct indexes owning the exact same folder are ambiguous -> catch-all / unplaced.
        proj_tie = self.base / "projects" / "-beta-2"
        write_claude_transcript(proj_tie, "99887766-0000-4000-8000-000000000009", cwd=self.repo_b)
        reg_tie = dict(self.reg, indexes=dict(self.reg["indexes"], **{
            "beta2-session": {
                "group": "work",
                "owner_session": "Beta2",
                "owner_session_id": "99887766-0000-4000-8000-000000000009",
                "transcripts": {"project_dir": [str(proj_tie)], "sessions": ["99887766-0000-4000-8000-000000000009"]},
            }
        }))
        write_codex_rollout(self.codex_dir, CODEX_2, self.repo_b, ts="2026-04-01T11:05:00Z")
        plan_tie = routing.resolve_all(reg_tie, self._settings(catch_all="vault-session"),
                                       {"codex": codex_parse.CodexSource},
                                       ledger_path=str(self.ledger))
        by_sid = {p.run.session_id: (p.index, p.reason) for p in plan_tie.placements}
        self.assertEqual(by_sid[CODEX_2], ("vault-session", routing.REASON_CATCH_ALL))

    def test_branch_c_catch_all_requires_protected_group_and_receives_unmatched(self):
        write_codex_rollout(self.codex_dir, CODEX_1, self.base / "unowned", ts="2026-04-01T12:00:00Z")
        # Protected catch-all works.
        plan = routing.resolve_all(self.reg, self._settings(catch_all="vault-session"),
                                   {"codex": codex_parse.CodexSource},
                                   ledger_path=str(self.ledger))
        self.assertEqual((plan.placements[0].index, plan.placements[0].reason),
                         ("vault-session", routing.REASON_CATCH_ALL))
        self.assertEqual(plan.counts_for("vault-session"), {"launch": 0, "folder": 0, "catch_all": 1})
        self.assertEqual(len(plan.unplaced), 0)

        # Non-protected catch-all is refused.
        with self.assertRaisesRegex(ValueError, "not a protected group"):
            routing.resolve_all(self.reg, self._settings(catch_all="beta-session"),
                                {"codex": codex_parse.CodexSource},
                                ledger_path=str(self.ledger))

        # Missing catch-all index is refused.
        with self.assertRaisesRegex(ValueError, "not in the registry"):
            routing.resolve_all(self.reg, self._settings(catch_all="missing-session"),
                                {"codex": codex_parse.CodexSource},
                                ledger_path=str(self.ledger))

    def test_provider_agnostic_source_plugs_in_via_run_meta(self):
        agy_dir = self.base / "agy-runs"
        agy_dir.mkdir()
        run_file = agy_dir / "conv-1234567890.json"
        run_file.write_text(json.dumps({
            "cwd": str(self.repo_b),
            "started": "2026-04-01T10:06:00Z",
        }))

        class FakeAntigravitySource:
            provider = "antigravity"
            default_path = str(agy_dir)

            def list(self, dirs, *_):
                items = []
                for d in dirs:
                    for p in sorted(Path(d).glob("*.json")):
                        items.append((p.name, p.stem, p))
                return items, {}

            def run_meta(self, path):
                doc = json.loads(Path(path).read_text())
                return doc["cwd"], doc["started"]

        self._append_launch(SID_A, self.repo_b, "2026-04-01T10:05:00Z", tool="agy")
        s = self._settings(extra_sources={"antigravity": str(agy_dir)})
        plan = routing.resolve_all(self.reg, s, {
            "codex": codex_parse.CodexSource,
            "antigravity": FakeAntigravitySource,
        }, ledger_path=str(self.ledger))
        self.assertEqual(len(plan.placements), 1)
        p = plan.placements[0]
        self.assertEqual((p.run.provider, p.index, p.reason),
                         ("antigravity", "alpha-session", routing.REASON_LAUNCH))

    def test_standalone_provider_index_claims_its_runs_so_none_double_index(self):
        write_codex_rollout(self.codex_dir, CODEX_1, self.repo_a, ts="2026-04-01T10:05:00Z")
        reg = dict(self.reg, indexes=dict(self.reg["indexes"], **{
            "codex-all": {
                "group": "work",
                "transcripts": {"source": "codex", "path": str(self.codex_dir)},
            }
        }))
        plan = routing.resolve_all(reg, self._settings(), {"codex": codex_parse.CodexSource},
                                   ledger_path=str(self.ledger))
        self.assertEqual(plan.placements, [])


if __name__ == "__main__":
    unittest.main()
