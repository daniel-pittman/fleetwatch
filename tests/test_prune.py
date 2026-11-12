"""fleetwatch prune: candidates, the move, restore on return, and the command line."""
import atexit
import contextlib
import io
import json
import os

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")  # tests never move a real home folder
import sys
import tempfile
import unittest
from unittest import mock

_CONFIG = tempfile.TemporaryDirectory()
os.environ["FLEETWATCH_CONFIG_DIR"] = _CONFIG.name
atexit.register(_CONFIG.cleanup)

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import standin  # noqa: E402
from fleetwatch import config, data, prune  # noqa: E402

DAY = 86400


class PruneWorld(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = standin.build(self.tmp.name)
        self.status = self.paths["status"]
        self.settings = config.default_prune()

    def tearDown(self):
        self.tmp.cleanup()

    def reg(self, name, sid, pid=standin.DEAD_PID, status="idle", age=10 * DAY):
        d = {"name": name, "sessionId": sid, "pid": pid, "kind": "interactive", "cwd": "/x",
             "status": status, "statusUpdatedAt": standin.ms(-age), "updatedAt": standin.ms(-age),
             "startedAt": standin.ms(-age)}
        with open(os.path.join(self.paths["sessions"], sid + ".json"), "w") as fh:
            json.dump(d, fh)

    def file(self, fname, name, sid=None, age=10 * DAY, **extra):
        d = dict(session=name, updated=standin.iso(-age), **extra)
        if sid:
            d["sessionId"] = sid
        with open(os.path.join(self.status, fname), "w") as fh:
            json.dump(d, fh)

    def transcript(self, sid, age):
        folder = os.path.join(self.paths["projects"], "-some-project")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, sid + ".jsonl")
        with open(path, "w") as fh:
            fh.write("{}\n")
        os.utime(path, (standin.NOW - age, standin.NOW - age))

    def records(self):
        return data.build_records(self.paths, standin.NOW, [], ppids=standin.PPIDS)

    def plan(self):
        return prune.make_plan(self.paths, standin.NOW, self.records(), self.settings)

    def names(self, entries):
        return sorted(e["name"] for e in entries)

    def folder(self):
        return sorted(f for f in os.listdir(self.status) if f.endswith(".json"))


class CandidateTests(PruneWorld):
    def test_finished_quiet_session_is_a_candidate(self):
        self.file("done.json", "Done One", "d1")
        plan = self.plan()
        self.assertEqual(self.names(plan.candidates), ["Done One"])
        self.assertIn("quiet for 10 d", plan.candidates[0]["reason"])

    def test_live_session_is_never_a_candidate(self):
        self.reg("Live One", "l1", pid=standin.PARENT_PID)
        self.file("live.json", "Live One", "l1")
        self.assertEqual(self.plan().candidates, [])

    def test_live_by_name_when_the_file_has_no_session_id(self):
        self.reg("Live Two", "l2", pid=standin.PARENT_PID)
        self.file("live2.json", "Live Two")
        self.assertEqual(self.plan().candidates, [])

    def test_questions_upcoming_and_remind_keep_a_file(self):
        self.file("q.json", "Has Question", "q1", questions=["Which branch?"])
        self.file("u.json", "Has Upcoming", "u1", upcoming=[{"what": "Deploy", "due": "2026-12-01"}])
        self.file("r.json", "Has Remind", "r1", remind=["Email the registrar"])
        plan = self.plan()
        self.assertEqual(plan.candidates, [])
        self.assertEqual(self.names(plan.kept_open), ["Has Question", "Has Remind", "Has Upcoming"])

    def test_empty_lists_do_not_keep_a_file(self):
        self.file("e.json", "Empty Lists", "e1", questions=[], upcoming=[], remind=[])
        self.assertEqual(self.names(self.plan().candidates), ["Empty Lists"])

    def test_keep_list_names_are_never_candidates(self):
        self.settings = dict(self.settings, keep=["Side Project", "Docs Site"])
        self.file("a.json", "Side Project", "k1")
        self.file("b.json", "docs site", "k2")
        self.file("c.json", "Billing API", "k3")
        plan = self.plan()
        self.assertEqual(self.names(plan.candidates), ["Billing API"])
        self.assertEqual(plan.left["keep"], 2)

    def test_recent_status_update_keeps_a_file(self):
        self.file("n.json", "Fresh", "n1", age=3600)
        plan = self.plan()
        self.assertEqual(plan.candidates, [])
        self.assertGreaterEqual(plan.left["recent"], 1)

    def test_recent_transcript_keeps_a_file(self):
        self.file("t.json", "Transcript Fresh", "t1")
        self.transcript("t1", 3600)
        self.assertEqual(self.plan().candidates, [])
        self.transcript("t1", 5 * DAY)
        self.assertEqual(self.names(self.plan().candidates), ["Transcript Fresh"])

    def test_window_is_configurable(self):
        self.file("w.json", "Three Days", "w1", age=3 * DAY)
        self.assertEqual(self.names(self.plan().candidates), ["Three Days"])
        self.settings["quiet_s"] = 7 * DAY
        self.assertEqual(self.plan().candidates, [])

    def test_prune_toml_sets_keep_and_window(self):
        path = os.path.join(_CONFIG.name, "prune.toml")
        with open(path, "w") as fh:
            fh.write('keep = ["Mine"]\nquiet = "3d"\n')
        try:
            settings, warning = config.load_prune()
            self.assertIsNone(warning)
            self.assertEqual(settings["keep"], ["Mine"])
            self.assertEqual(settings["quiet_s"], 3 * DAY)
            with open(path, "w") as fh:
                fh.write("keep = 5\n")
            settings, warning = config.load_prune()
            self.assertIn("not valid", warning)
            self.assertEqual(settings, config.default_prune())
        finally:
            os.remove(path)


class MoveTests(PruneWorld):
    def test_prune_moves_to_pruned_and_status_reads_ignore_them(self):
        self.file("done.json", "Done One", "d1")
        self.file("keepme.json", "Has Question", "q1", questions=["Which?"])
        moved, skipped = prune.apply(self.plan(), standin.NOW)
        self.assertEqual((moved, skipped), (["Done One"], []))
        self.assertNotIn("done.json", self.folder())
        self.assertTrue(os.path.isfile(os.path.join(self.status, ".pruned", "done.json")))
        self.assertIn("keepme.json", self.folder())
        names = [d["session"] for d in data.read_status_files(self.status, [])]
        self.assertNotIn("Done One", names)
        self.assertIn("Has Question", names)

    def test_nothing_is_deleted_when_a_parked_name_repeats(self):
        self.file("done.json", "Done One", "d1")
        prune.apply(self.plan(), standin.NOW)
        self.file("done.json", "Done One", "d1", age=9 * DAY)
        prune.apply(self.plan(), standin.NOW)
        parked = os.listdir(os.path.join(self.status, ".pruned"))
        self.assertEqual(len(parked), 2)

    def test_session_that_goes_live_after_the_listing_is_not_moved(self):
        self.file("done.json", "Done One", "d1")
        plan = self.plan()
        self.reg("Done One", "d1", pid=standin.PARENT_PID)
        moved, skipped = prune.apply(plan, standin.NOW)
        self.assertEqual(moved, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("done.json", self.folder())

    def test_final_registry_check_catches_a_session_the_fresh_plan_missed(self):
        self.file("done.json", "Done One", "d1")
        plan = self.plan()
        self.reg("Done One", "d1", pid=standin.PARENT_PID)
        with mock.patch.object(prune, "make_plan", return_value=plan):
            moved, skipped = prune.apply(plan, standin.NOW)
        self.assertEqual(moved, [])
        self.assertEqual(skipped, [("Done One", "went live")])
        self.assertIn("done.json", self.folder())

    def test_a_file_that_gained_a_question_after_the_listing_is_not_moved(self):
        self.file("done.json", "Done One", "d1")
        plan = self.plan()
        self.file("done.json", "Done One", "d1", questions=["New question"])
        with mock.patch.object(prune, "make_plan", return_value=plan):
            moved, skipped = prune.apply(plan, standin.NOW)
        self.assertEqual(moved, [])
        self.assertIn("done.json", self.folder())


class RestoreTests(PruneWorld):
    def park(self):
        self.file("done.json", "Done One", "d1")
        prune.apply(self.plan(), standin.NOW)
        self.assertNotIn("done.json", self.folder())

    def test_a_returning_session_gets_its_file_back(self):
        self.park()
        self.assertEqual(prune.restore_returned(self.status, self.records()), [])
        self.reg("Done One", "d1", pid=standin.PARENT_PID)
        notes = prune.restore_returned(self.status, self.records())
        self.assertEqual(len(notes), 1)
        self.assertIn("Done One", notes[0])
        self.assertIn("done.json", self.folder())
        self.assertEqual(os.listdir(os.path.join(self.status, ".pruned")), [])

    def test_a_newer_file_is_not_overwritten(self):
        self.park()
        self.file("done-new.json", "Done One", "d1", age=60, working_on="back again")
        self.reg("Done One", "d1", pid=standin.PARENT_PID)
        self.assertEqual(prune.restore_returned(self.status, self.records()), [])
        self.assertEqual(self.folder().count("done.json"), 0)
        self.assertTrue(os.path.isfile(os.path.join(self.status, ".pruned", "done.json")))
        with open(os.path.join(self.status, "done-new.json")) as fh:
            self.assertEqual(json.load(fh)["working_on"], "back again")

    def test_a_file_without_session_id_returns_by_name(self):
        self.file("anon.json", "Anon One")
        prune.apply(self.plan(), standin.NOW)
        self.reg("Anon One", "a9", pid=standin.PARENT_PID)
        self.assertEqual(len(prune.restore_returned(self.status, self.records())), 1)
        self.assertIn("anon.json", self.folder())

    def test_a_different_sessions_file_under_the_same_name_is_not_clobbered(self):
        self.park()
        self.file("done.json", "Someone Else", "z9", age=20 * DAY)
        self.reg("Done One", "d1", pid=standin.PARENT_PID)
        self.assertEqual(prune.restore_returned(self.status, self.records()), [])
        with open(os.path.join(self.status, "done.json")) as fh:
            self.assertEqual(json.load(fh)["session"], "Someone Else")

    def test_collect_restores_and_notes_it(self):
        self.park()
        self.reg("Done One", "d1", pid=standin.PARENT_PID)
        with mock.patch.object(data.time, "time", return_value=standin.NOW):
            sections, stale, now, warnings, records = data.collect_full(24 * 3600, True, self.paths)
        self.assertTrue(any(w.startswith("restored status file for Done One") for w in warnings))
        rec = [r for r in records if r["name"] == "Done One"][0]
        self.assertEqual(rec["status"]["session"], "Done One")

    def test_hand_restore(self):
        self.park()
        msg, ok = prune.restore_named(self.status, "done one")
        self.assertTrue(ok, msg)
        self.assertIn("done.json", self.folder())
        msg, ok = prune.restore_named(self.status, "nobody")
        self.assertFalse(ok)


class CliTests(PruneWorld):
    def run_cli(self, *args):
        env = {"FLEETWATCH_PROJECTS_DIR": self.paths["projects"], "FLEETWATCH_PS_CMD": "true"}
        argv = list(args) + ["--status-dir", self.status, "--sessions-dir", self.paths["sessions"],
                             "--jobs-dir", self.paths["jobs"], "--command", self.paths["command"]]
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            code = prune.main(argv)
        return code, out.getvalue()

    def test_dry_run_lists_and_changes_nothing(self):
        self.file("done.json", "Done One", "d1")
        self.file("q.json", "Has Question", "q1", questions=["Which?"])
        before = sorted(os.listdir(self.status))
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("Done One", out)
        self.assertIn("Kept: has open items", out)
        self.assertIn("Dry run: nothing moved", out)
        self.assertEqual(sorted(os.listdir(self.status)), before)
        self.assertFalse(os.path.exists(os.path.join(self.status, ".pruned")))

    def test_yes_moves_and_restore_brings_back(self):
        self.file("done.json", "Done One", "d1")
        self.file("q.json", "Has Question", "q1", questions=["Which?"])
        code, out = self.run_cli("--yes")
        self.assertEqual(code, 0)
        self.assertIn("Moved", out)
        self.assertNotIn("done.json", self.folder())
        self.assertIn("q.json", self.folder())
        code, out = self.run_cli("--restore", "Done One")
        self.assertEqual(code, 0)
        self.assertIn("done.json", self.folder())

    def test_dispatch_through_the_main_entry(self):
        from fleetwatch import __main__ as entry
        with mock.patch.object(prune, "main", return_value=7) as m:
            self.assertEqual(entry.main(["prune", "--yes"]), 7)
        m.assert_called_once_with(["--yes"])


if __name__ == "__main__":
    unittest.main()
