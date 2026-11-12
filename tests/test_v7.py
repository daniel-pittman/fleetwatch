import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import standin
from fleetwatch import config, data, views, write_status
from fleetwatch.render_text import snapshot

NOW = standin.NOW


def write(path, obj):
    with open(path, "w") as fh:
        json.dump(obj, fh)


def read(path):
    with open(path) as fh:
        return json.load(fh)


class Tree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = standin.build(self.tmp.name)
        for f in os.listdir(self.paths["status"]):
            os.unlink(os.path.join(self.paths["status"], f))
        for f in os.listdir(self.paths["sessions"]):
            os.unlink(os.path.join(self.paths["sessions"], f))

    def reg(self, name, sid, pid=standin.PARENT_PID, since=-3600, extra=None):
        d = {"name": name, "sessionId": sid, "pid": pid, "kind": "interactive", "cwd": "/x",
             "status": "idle", "statusUpdatedAt": standin.ms(-60), "updatedAt": standin.ms(-60),
             "startedAt": standin.ms(-9999)}
        if since is not None:
            d["nameSince"] = standin.ms(since)
        d.update(extra or {})
        fname = sid if pid == standin.PARENT_PID else str(pid)   # the real registry is <pid>.json
        write(os.path.join(self.paths["sessions"], "%s.json" % fname), d)

    def status(self, fname, **d):
        write(os.path.join(self.paths["status"], fname), d)

    def view(self):
        sections, _, warnings = standin.collect_view(self.paths)
        return data.build_view(sections, NOW), warnings


class RenameJoin(Tree):
    def test_rename_shows_one_row_with_note(self):
        self.reg("Docs Site", "sid-1", since=-3600)
        self.status("projects-8d.json", session="projects-8d", sessionId="sid-1",
                    updated=standin.iso(-300), working_on="packing")
        view, _ = self.view()
        self.assertEqual([x["name"] for x in view["rows"]], ["Docs Site"])
        self.assertEqual(view["rows"][0]["renamed_from"], "projects-8d")
        self.assertEqual(view["rows"][0]["working_on"], "packing")
        self.assertEqual(view["orphans"], [])
        node = views.session_node(view["rows"][0], NOW)
        self.assertIn("renamed from projects-8d", " ".join(t.plain for _, t in node.body))

    def test_note_expires_after_a_day(self):
        self.reg("Docs Site", "sid-1", since=-25 * 3600)
        self.status("old.json", session="old", sessionId="sid-1", updated=standin.iso(-300))
        view, _ = self.view()
        self.assertIsNone(view["rows"][0]["renamed_from"])

    def test_same_session_id_files_newest_wins(self):
        self.reg("New", "sid-1")
        self.status("a.json", session="A", sessionId="sid-1", updated=standin.iso(-900), working_on="older")
        self.status("b.json", session="B", sessionId="sid-1", updated=standin.iso(-100), working_on="newer")
        view, _ = self.view()
        self.assertEqual(len(view["rows"]) + len(view["orphans"]), 1)
        self.assertEqual(view["rows"][0]["working_on"], "newer")

    def test_orphan_session_id_follows_stale_rules(self):
        self.reg("Live", "sid-1")
        self.status("gone.json", session="Gone", sessionId="sid-zzz", updated=standin.iso(-5 * 86400))
        view, _ = self.view()
        sections, stale, _ = standin.collect_view(self.paths)
        names = [r["name"] for k in sections for r in sections[k]] + [r["name"] for r in stale]
        self.assertEqual(names, ["Live", "Gone"])
        self.assertTrue([r for r in stale if r["name"] == "Gone"][0]["file_only"])
        self.assertEqual([x["name"] for x in view["rows"]], ["Live"])   # never a row by itself
        self.assertEqual(view["orphans"], [])

    def test_name_join_still_works_without_session_id(self):
        self.reg("Plain", "sid-1")
        self.status("plain.json", session="Plain", updated=standin.iso(-60), working_on="x")
        view, _ = self.view()
        self.assertEqual(view["rows"][0]["working_on"], "x")
        self.assertIsNone(view["rows"][0]["renamed_from"])

    def test_old_name_reused_by_other_session_does_not_steal_file(self):
        self.reg("A", "sid-2")            # a different session now holds the old name
        self.reg("B", "sid-1")            # the file's session, renamed A -> B
        self.status("a.json", session="A", sessionId="sid-1", updated=standin.iso(-60), working_on="mine")
        view, _ = self.view()
        by = {x["name"]: x for x in view["rows"]}
        self.assertEqual(by["B"]["working_on"], "mine")
        self.assertEqual(by["A"]["working_on"], "")

    def test_text_lines_note_rename(self):
        from fleetwatch.render_text import nothing_lines
        self.reg("Docs Site", "sid-1", extra={"status": "busy"})
        self.status("p.json", session="p", sessionId="sid-1", updated=standin.iso(-60), working_on="w")
        view, _ = self.view()
        text = " ".join(seg for ln in nothing_lines(view, [], 120, True, False) for seg, _ in ln)
        self.assertIn("renamed from p", text)


class WriteStatus(Tree):
    def run_ws(self, argv, payload, ppids=None, start=500):
        out, err = io.StringIO(), io.StringIO()
        code = write_status.run(argv, io.StringIO(payload), out, err, paths=self.paths,
                                ppids={500: 400, 400: 300, 300: 1} if ppids is None else ppids,
                                start_pid=start, now=None)
        return code, out.getvalue(), err.getvalue()

    def test_resolves_through_ppid_chain_and_writes(self):
        self.reg("My Session", "sid-9", pid=300)
        code, out, _ = self.run_ws([], '{"working_on": "hello", "questions": ["q1"]}')
        self.assertEqual(code, 0)
        target = os.path.join(self.paths["status"], "my-session.json")
        doc = read(target)
        self.assertEqual((doc["session"], doc["sessionId"], doc["working_on"], doc["questions"]),
                         ("My Session", "sid-9", "hello", ["q1"]))
        self.assertRegex(doc["updated"], r"T\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertNotIn("persona", doc)
        self.assertEqual([f for f in os.listdir(self.paths["status"]) if f.startswith(".")], [])

    def test_removes_only_same_session_id_files(self):
        self.reg("New Name", "sid-9", pid=300)
        s = self.paths["status"]
        write(os.path.join(s, "old-name.json"), {"session": "Old", "sessionId": "sid-9", "updated": standin.iso(-9)})
        write(os.path.join(s, "other.json"), {"session": "Other", "sessionId": "sid-8", "updated": standin.iso(-9)})
        write(os.path.join(s, "legacy.json"), {"session": "Legacy", "updated": standin.iso(-9)})
        with open(os.path.join(s, "broken.json"), "w") as fh:
            fh.write("{nope")
        code, _, _ = self.run_ws([], '{"working_on": "x"}')
        self.assertEqual(code, 0)
        self.assertEqual(sorted(os.listdir(s)), ["broken.json", "legacy.json", "new-name.json", "other.json"])

    def test_no_session_found_writes_nothing(self):
        self.reg("Unrelated", "sid-1", pid=777)
        code, out, err = self.run_ws([], '{"working_on": "x"}')
        self.assertNotEqual(code, 0)
        self.assertIn("no Claude session", err)
        self.assertEqual(os.listdir(self.paths["status"]), [])

    def test_bad_payloads_rejected(self):
        self.reg("My Session", "sid-9", pid=300)
        for bad in ("not json", "[1]", '{"nope": 1}', '{"cadence_min": true}', '{"cadence_min": -1}',
                    '{"questions": "x"}', '{"questions": [3]}', '{"upcoming": [{"due": "2026-10-28"}]}',
                    '{"working_on": 5}', '{"questions": [{"q": "a", "since": "yesterday"}]}'):
            code, _, err = self.run_ws([], bad)
            self.assertEqual(code, 1, bad)
            self.assertIn("fleetwatch write-status:", err)
        self.assertEqual(os.listdir(self.paths["status"]), [])

    def test_strings_are_cleaned(self):
        self.reg("My Session", "sid-9", pid=300)
        code, out, _ = self.run_ws(["--dry-run"], json.dumps({"working_on": "a\x1b[31mb\nc"}))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["working_on"], "a[31mb c")

    def test_dry_run_and_print_path_do_not_write(self):
        self.reg("My Session", "sid-9", pid=300)
        code, out, _ = self.run_ws(["--dry-run"], '{"persona": "Robin"}')
        self.assertEqual(json.loads(out)["persona"], "Robin")
        code, out, _ = self.run_ws(["--print-path"], "")
        self.assertEqual(out.strip(), os.path.join(self.paths["status"], "my-session.json"))
        self.assertEqual(os.listdir(self.paths["status"]), [])

    def test_upcoming_and_questions_round_trip(self):
        self.reg("My Session", "sid-9", pid=300)
        payload = {"cadence_min": 15, "questions": [{"q": "ok?", "since": standin.iso(-60)}],
                   "upcoming": [{"what": "File form", "due": "2026-10-20", "kind": "deadline", "yours": True}],
                   "remind": ["x"]}
        self.assertEqual(self.run_ws([], json.dumps(payload))[0], 0)
        doc = read(os.path.join(self.paths["status"], "my-session.json"))
        self.assertEqual(doc["upcoming"][0]["what"], "File form")
        self.assertEqual(doc["questions"][0]["q"], "ok?")
        # fleetwatch reads the file it wrote, joined by sessionId
        os.unlink(os.path.join(self.paths["sessions"], "300.json"))
        self.reg("My Session", "sid-9", pid=standin.PARENT_PID)
        view, warnings = self.view()
        self.assertEqual(warnings, [])
        self.assertEqual(len(view["rows"][0]["qs"]), 1)


class OverrideFollowsRename(unittest.TestCase):
    def test_override_ids_beat_name(self):
        g = config.Groups([("A", ["/a"])], {"Old Name": "Pinned"}, {"sid-1": "Pinned"})
        self.assertEqual(g.of("New Name", "/a/x", "sid-1"), "Pinned")
        self.assertEqual(g.of("New Name", "/a/x", "sid-2"), "A")
        self.assertEqual(g.of("Old Name", "/a/x"), "Pinned")

    def test_set_override_writes_both_and_removes_both(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "groups.toml")
            with open(path, "w") as fh:
                fh.write('[[group]]\nname = "A"\npaths = ["/a"]\n')
            config.set_override(path, "Old Name", "Pinned", "A", "sid-1")
            g, warn = config.load_groups(path)
            self.assertIsNone(warn)
            self.assertEqual(g.overrides, {"Old Name": "Pinned"})
            self.assertEqual(g.override_ids, {"sid-1": "Pinned"})
            self.assertEqual(g.of("Renamed", "/a", "sid-1"), "Pinned")
            config.set_override(path, "Old Name", "A", "A", "sid-1")
            g, _ = config.load_groups(path)
            self.assertEqual((g.overrides, g.override_ids), ({}, {}))

    def test_bad_override_ids_table_is_a_warning(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "groups.toml")
            with open(path, "w") as fh:
                fh.write('[override_ids]\nx = 3\n')
            _, warn = config.load_groups(path)
            self.assertIn("override_ids", warn)


if __name__ == "__main__":
    unittest.main()
