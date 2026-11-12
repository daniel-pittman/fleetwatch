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

    def file(self, fname, name, sid=None, age=10 * DAY, title=None, transcript=True, **extra):
        """A status file. With a sessionId it also gets a transcript (untitled unless `title` is given),
        so the session classifies; pass transcript=False for a session whose transcript is gone."""
        d = dict(session=name, updated=standin.iso(-age), **extra)
        if sid:
            d["sessionId"] = sid
        with open(os.path.join(self.status, fname), "w") as fh:
            json.dump(d, fh)
        if sid and transcript:
            self.transcript(sid, age, title)

    def transcript(self, sid, age, title=None, root=None):
        folder = os.path.join(root or self.paths["projects"], "-some-project")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, sid + ".jsonl")
        with open(path, "w") as fh:
            fh.write("{}\n")
            if title:
                fh.write(json.dumps({"type": "custom-title", "customTitle": title, "sessionId": sid},
                                    separators=(",", ":")) + "\n")
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
        self.reg("wt-live-two", "l2", pid=standin.PARENT_PID)
        self.file("live2.json", "wt-live-two")
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
        self.file("anon.json", "wt-anon-one")
        prune.apply(self.plan(), standin.NOW)
        self.reg("wt-anon-one", "a9", pid=standin.PARENT_PID)
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


class NamedTests(PruneWorld):
    """A session the user named (a custom-title record in its transcript) is never a default candidate."""

    def test_a_named_session_is_kept_by_default_and_counted(self):
        self.file("named.json", "Pricing Page", "n1", title="Pricing Page")
        self.file("plain.json", "Scratch Run", "p1")
        plan = self.plan()
        self.assertEqual(self.names(plan.candidates), ["Scratch Run"])
        self.assertEqual(self.names(plan.kept_named), ["Pricing Page"])
        moved, _skipped = prune.apply(plan, standin.NOW)
        self.assertEqual(moved, ["Scratch Run"])
        self.assertIn("named.json", self.folder())

    def test_the_newest_title_in_the_transcript_decides(self):
        self.file("n.json", "Renamed", "n2", title="Old name")
        path = os.path.join(self.paths["projects"], "-some-project", "n2.jsonl")
        with open(path, "a") as fh:
            fh.write(json.dumps({"type": "custom-title", "customTitle": "New name"}, separators=(",", ":")) + "\n")
        os.utime(path, (standin.NOW - 10 * DAY, standin.NOW - 10 * DAY))
        self.assertEqual(self.names(self.plan().kept_named), ["Renamed"])

    def test_include_named_makes_them_candidates_and_marks_them(self):
        self.file("named.json", "Pricing Page", "n1", title="Pricing Page")
        self.file("plain.json", "Scratch Run", "p1")
        plan = prune.make_plan(self.paths, standin.NOW, self.records(), self.settings, include_named=True)
        self.assertEqual(self.names(plan.candidates), ["Pricing Page", "Scratch Run"])
        self.assertEqual(plan.kept_named, [])
        named = {c["name"]: c["named"] for c in plan.candidates}
        self.assertEqual(named, {"Pricing Page": True, "Scratch Run": False})
        moved, _skipped = prune.apply(plan, standin.NOW)
        self.assertEqual(sorted(moved), ["Pricing Page", "Scratch Run"])

    def test_include_flips_a_plan_without_reading_anything_again(self):
        self.file("named.json", "Pricing Page", "n1", title="Pricing Page")
        plan = self.plan()
        both = plan.include(True)
        self.assertEqual(self.names(both.candidates), ["Pricing Page"])
        self.assertTrue(both.include_named)
        self.assertEqual(self.names(both.include(False).kept_named), ["Pricing Page"])
        self.assertEqual(self.names(plan.candidates), [])          # the original is untouched

    def test_a_transcript_without_a_title_is_unnamed_whatever_the_status_name(self):
        self.file("a.json", "Looks Human", "a1")
        self.assertEqual(self.names(self.plan().candidates), ["Looks Human"])

    def test_the_archive_is_searched_for_the_transcript(self):
        self.file("a.json", "Archived Name", "ar1", transcript=False)
        self.paths["archive"] = os.path.join(self.tmp.name, "archive")
        self.transcript("ar1", 10 * DAY, "Archived Name", root=self.paths["archive"])
        self.assertEqual(self.names(self.plan().kept_named), ["Archived Name"])

    def test_no_transcript_falls_back_to_the_generated_name_pattern(self):
        self.file("g.json", "wt-fix-login", "g1", transcript=False)
        self.file("g2.json", "a1b2c3d4e5f60718293a4b5c6d7e8f90", "g2", transcript=False)
        self.assertEqual(self.names(self.plan().candidates),
                         ["a1b2c3d4e5f60718293a4b5c6d7e8f90", "wt-fix-login"])

    def test_no_transcript_and_a_human_name_cannot_be_told_and_is_never_pruned(self):
        self.file("h.json", "Pricing Page", "h1", transcript=False)
        self.file("h2.json", "Nameless Id", transcript=False)           # no sessionId at all
        for include in (False, True):
            plan = prune.make_plan(self.paths, standin.NOW, self.records(), self.settings, include_named=include)
            self.assertEqual(plan.candidates, [])
            self.assertEqual(self.names(plan.kept_unknown), ["Nameless Id", "Pricing Page"])
        moved, _s = prune.apply(plan, standin.NOW)
        self.assertEqual(moved, [])
        self.assertTrue({"h.json", "h2.json"} <= set(self.folder()))

    def test_an_unreadable_transcript_cannot_be_told_either(self):
        self.file("u.json", "Scratch Run", "u1")
        with mock.patch("fleetwatch.resume.last_title", side_effect=OSError("denied")):
            plan = self.plan()
        self.assertEqual(plan.candidates, [])
        self.assertEqual(self.names(plan.kept_unknown), ["Scratch Run"])

    def test_open_items_still_win_over_the_named_heading(self):
        self.file("n.json", "Pricing Page", "n1", title="Pricing Page", questions=["Which?"])
        plan = self.plan()
        self.assertEqual(self.names(plan.kept_open), ["Pricing Page"])
        self.assertEqual(plan.kept_named, [])

    def test_a_session_named_after_the_listing_is_not_moved(self):
        self.file("s.json", "Scratch Run", "x1")
        plan = self.plan()
        self.transcript("x1", 10 * DAY, "Now named")
        moved, skipped = prune.apply(plan, standin.NOW)
        self.assertEqual(moved, [])
        self.assertEqual(skipped, [("Scratch Run", "no longer a candidate")])
        self.assertIn("s.json", self.folder())


class NamedCliTests(PruneWorld):
    run_cli = CliTests.run_cli

    def test_default_lists_named_under_its_own_heading_and_moves_nothing_named(self):
        self.file("named.json", "Pricing Page", "n1", title="Pricing Page")
        self.file("plain.json", "Scratch Run", "p1")
        self.file("lost.json", "Lost Name", "l1", transcript=False)
        code, out = self.run_cli("--yes")
        self.assertEqual(code, 0)
        self.assertIn("Ready to prune (1)", out)
        self.assertIn("Kept: named session (1)", out)
        self.assertIn("Kept: could not tell if named (1)", out)
        self.assertIn("--include-named", out)
        self.assertTrue({"lost.json", "named.json"} <= set(self.folder()))
        self.assertNotIn("plain.json", self.folder())

    def test_include_named_prunes_the_named_and_still_not_the_unknown(self):
        self.file("named.json", "Pricing Page", "n1", title="Pricing Page")
        self.file("lost.json", "Lost Name", "l1", transcript=False)
        code, out = self.run_cli("--include-named", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("Pricing Page", out)
        self.assertNotIn("Kept: named session", out)
        self.assertIn("lost.json", self.folder())
        self.assertNotIn("named.json", self.folder())
        code, out = self.run_cli("--restore", "Pricing Page")
        self.assertEqual(code, 0)
        self.assertIn("named.json", self.folder())


if __name__ == "__main__":
    unittest.main()
