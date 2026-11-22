"""fleetwatch resume and fleetwatch park: the transcript index, the gate, the command line and the Resume tab."""
import asyncio
import json
import os

os.environ.setdefault("FLEETWATCH_NO_MIGRATE", "1")  # tests never move a real home folder
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import test_app  # noqa: E402  (sets FLEETWATCH_CONFIG_DIR before fleetwatch reads it)
from test_app import TTY, boot  # noqa: E402
import standin  # noqa: E402
from fleetwatch import app as fapp, config, data, iterm, park, prune, resume, views, write_status  # noqa: E402
from textual.widgets import Input  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
NASTY = "it's $(touch pwned) | a & b"


def sid():
    return str(uuid.uuid4())


class Tree(unittest.TestCase):
    """Fixture folders for transcripts, the archive, status files and fleetwatch's own config."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.paths = standin.build(self.root)
        for d in ("sessions", "status"):
            for f in os.listdir(self.paths[d]):
                os.remove(os.path.join(self.paths[d], f))
        self.paths["archive"] = os.path.join(self.root, "archive")
        os.makedirs(self.paths["archive"])
        self.cfg = os.path.join(self.root, "cfg")
        self.env = mock.patch.dict(os.environ, {"FLEETWATCH_CONFIG_DIR": self.cfg})
        self.env.start()
        self.folder = self.mkdir("work")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def mkdir(self, name):
        path = os.path.join(self.root, name)
        os.makedirs(path, exist_ok=True)
        return path

    def transcript(self, title=None, cwd=None, entry="cli", session=None, root=None, slug="-proj",
                   mtime=None, later=(), side=False):
        session = session or sid()
        folder = os.path.join(root or self.paths["projects"], slug)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, session + ".jsonl")
        lines = [{"type": "queue-operation", "sessionId": session},
                 {"type": "user", "cwd": cwd or self.folder, "entrypoint": entry, "isSidechain": side,
                  "sessionId": session}]
        if title:
            lines.append({"type": "custom-title", "customTitle": title, "sessionId": session})
        lines += list(later)
        with open(path, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line, separators=(",", ":")) + "\n")
        # Quiet for an hour unless a test says otherwise: a transcript written in the last minute is refused.
        mtime = mtime or time.time() - 3600
        os.utime(path, (mtime, mtime))
        return session, path

    def reg(self, name, session, pid=standin.PARENT_PID, cwd="/x"):
        d = {"name": name, "sessionId": session, "pid": pid, "kind": "interactive", "cwd": cwd,
             "status": "idle", "statusUpdatedAt": standin.ms(-60), "updatedAt": standin.ms(-60)}
        with open(os.path.join(self.paths["sessions"], "%s.json" % session), "w") as fh:
            json.dump(d, fh)

    def agents(self, rows):
        with open(os.path.join(self.root, "agents.json"), "w") as fh:
            json.dump(rows, fh)

    def entries(self, held=None):
        return resume.mark_held(resume.load(self.paths).entries(), held or {})

    def names(self):
        return [e["name"] for e in self.entries()]


class ScanTests(Tree):
    def test_only_custom_named_main_sessions_show(self):
        self.transcript("Grant Proposal")
        self.transcript(None)                                              # unnamed
        self.transcript("Headless Helper", entry="sdk-cli")                # -p run
        self.transcript("Python Helper", entry="sdk-py")
        self.transcript("Side Chain", side=True)
        self.transcript("Lane", cwd="/repo/.claude/worktrees/lane")        # worktree lanes
        self.transcript("Lane Two", cwd="/git/fleetwatch-worktrees/resume")
        self.transcript("Lane Three", cwd="/code/work/wt-t26-p")
        self.transcript("wt-lane-4")                                       # helper-style names
        self.transcript("0123456789abcdef0123456789abcdef")
        folder = os.path.join(self.paths["projects"], "-proj")
        with open(os.path.join(folder, "agent-e4ad0592.jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "custom-title", "customTitle": "Old Subagent"}) + "\n")
        self.assertEqual(self.names(), ["Grant Proposal"])

    def test_first_cwd_and_last_title(self):
        moved = {"type": "user", "cwd": "/somewhere/else", "entrypoint": "cli"}
        renamed = {"type": "custom-title", "customTitle": "New Name"}
        self.transcript("Old Name", later=[moved, renamed])
        e = self.entries()[0]
        self.assertEqual((e["name"], e["cwd"]), ("New Name", self.folder))

    def test_a_title_mentioned_in_content_is_not_a_title(self):
        quoted = {"type": "user", "message": 'the text says "type":"custom-title","customTitle":"Fake"'}
        nested = {"type": "tool", "result": {"type": "custom-title", "customTitle": "Nested"}}
        self.transcript("Real Name", later=[quoted, nested])
        self.assertEqual(self.names(), ["Real Name"])
        self.transcript(None, later=[quoted, nested], slug="-other")
        self.assertEqual(self.names(), ["Real Name"])

    def test_title_far_from_the_end_and_across_a_chunk_boundary(self):
        filler = {"type": "user", "x": "y" * 4096}
        session, path = self.transcript("Far Back", later=[filler] * 700)   # about 2.9 MB of tail
        self.assertGreater(os.path.getsize(path), 2 * resume.CHUNK)
        self.assertEqual(resume.last_title(path, 0, os.path.getsize(path)), "Far Back")
        # Walk a title line across the position a chunk boundary falls on.
        for pad in range(0, 200, 7):
            p = os.path.join(self.root, "edge.jsonl")
            title = b'{"type":"custom-title","customTitle":"Edge"}\n'
            with open(p, "wb") as fh:
                fh.write(b'{"pad":"' + b"a" * (resume.CHUNK - 40 - pad) + b'"}\n' + title
                         + b'{"tail":"' + b"b" * (resume.CHUNK - 8) + b'"}\n')
            self.assertEqual(resume.last_title(p, 0, os.path.getsize(p)), "Edge", pad)

    def test_unchanged_files_are_not_opened_and_added_bytes_are_all_that_is_read(self):
        session, path = self.transcript("First")
        helper, helper_path = self.transcript("Helper", entry="sdk-cli", slug="-h")
        resume.scan(self.paths)
        real_open = open
        opened = []

        def spy(file, *a, **k):
            opened.append(str(file))
            return real_open(file, *a, **k)
        with mock.patch("builtins.open", spy):
            resume.scan(self.paths)
        self.assertNotIn(path, opened)
        self.assertNotIn(helper_path, opened)
        with open(path, "a") as fh:
            fh.write('{"type":"custom-title","customTitle":"Second"}\n')
        with open(helper_path, "a") as fh:
            fh.write("{}\n")
        cached = resume._load_cache(config.resume_cache_path())[path]["size"]
        calls = []
        real_last = resume.last_title
        with mock.patch.object(resume, "read_head", side_effect=AssertionError("head re-read")), \
                mock.patch.object(resume, "last_title",
                                  lambda p, a, b: calls.append((p, a)) or real_last(p, a, b)):
            got = resume.scan(self.paths)
        self.assertEqual(got[session]["title"], "Second")
        self.assertEqual(calls, [(path, cached)])

    def test_a_corrupt_cache_is_rebuilt(self):
        self.transcript("Named")
        os.makedirs(self.cfg, exist_ok=True)
        with open(config.resume_cache_path(), "w") as fh:
            fh.write("{not json")
        self.assertEqual(self.names(), ["Named"])
        self.assertEqual(resume._load_cache(config.resume_cache_path()).__class__, dict)
        self.assertTrue(resume._load_cache(config.resume_cache_path()))

    def test_a_wrong_shaped_cache_entry_is_a_miss(self):
        session, path = self.transcript("Named")
        os.makedirs(self.cfg, exist_ok=True)
        good = {"size": 1, "mtime": 1.0, "cwd": "/x", "title": "t"}
        files = {"a": "text", "b": dict(good, size="big"), "c": dict(good, title=7), "d": dict(good, cwd=[]),
                 "e": [1], path: "text", "ok": good}
        with open(config.resume_cache_path(), "w") as fh:
            json.dump({"version": resume.CACHE_VERSION, "files": files}, fh)
        self.assertEqual(list(resume._load_cache(config.resume_cache_path())), ["ok"])
        self.assertEqual(self.names(), ["Named"])

    def test_a_title_is_cleaned_of_control_characters(self):
        line = json.dumps({"type": "custom-title", "customTitle": "Red \x1b[31mAlert\x07\x1b]0;x\x07"})
        self.assertNotIn("\x1b", resume._title_in(line))
        self.assertNotIn("\x07", resume._title_in(line))
        self.assertTrue(resume._title_in(line).startswith("Red "))

    def test_same_name_shows_both_newest_first(self):
        other = self.mkdir("other")
        a, _ = self.transcript("Same Name", mtime=1000000)
        b, _ = self.transcript("Same Name", cwd=other, mtime=2000000, slug="-b")
        rows = self.entries()
        self.assertEqual([e["sid"] for e in rows], [b, a])
        self.assertEqual([e["cwd"] for e in rows], [other, self.folder])
        self.assertEqual(len(resume.match("same name", rows)), 2)

    def test_archive_only_appears_and_live_copy_wins(self):
        gone, _ = self.transcript("Swept Away", root=self.paths["archive"])
        both, _ = self.transcript("In Both", slug="-b")
        self.transcript("In Both Old Title", session=both, root=self.paths["archive"], slug="-b")
        rows = {e["name"]: e for e in self.entries()}
        self.assertTrue(rows["Swept Away"]["archived"])
        self.assertFalse(rows["In Both"]["archived"])
        self.assertNotIn("In Both Old Title", rows)


class MatchTests(unittest.TestCase):
    def rows(self, *names):
        return [{"name": n, "persona": "Robin" if n.startswith("Billing") else None, "sid": "abcdef12-%d" % i}
                for i, n in enumerate(names)]

    def names(self, q, rows):
        return [e["name"] for e in resume.match(q, rows)]

    def test_bands(self):
        rows = self.rows("Roadmap Planning Development", "Roadmap Writing", "Billing API Development",
                         "Docs Site", "Migrant Study")
        self.assertEqual(self.names("road", rows), ["Roadmap Planning Development", "Roadmap Writing"])
        self.assertEqual(self.names("ba", rows), ["Billing API Development"])
        self.assertEqual(self.names("robin", rows), ["Billing API Development"])
        self.assertEqual(self.names("igran", rows), ["Migrant Study"])
        self.assertEqual(self.names("dcs", rows), ["Docs Site"])
        self.assertEqual(self.names("qqq", rows), [])

    def test_exact_name_wins_and_id_prefix_works(self):
        rows = self.rows("Grant", "Roadmap Writing")
        self.assertEqual(self.names("GRANT", rows), ["Grant"])
        self.assertEqual(self.names("abcdef12-1", rows), ["Roadmap Writing"])


class GateTests(Tree):
    def one(self, name):
        return next(e for e in self.entries() if e["name"] == name)

    def test_live_session_is_refused(self):
        s, _ = self.transcript("Running")
        self.reg("Running", s)
        with self.assertRaises(resume.Blocked) as ctx:
            resume.prepare(self.one("Running"), self.paths)
        self.assertEqual(ctx.exception.kind, "live")
        self.assertIn("still running", str(ctx.exception))

    def test_dead_registry_entry_is_not_live(self):
        s, _ = self.transcript("Exited")
        self.reg("Exited", s, pid=standin.DEAD_PID)
        self.assertEqual(resume.prepare(self.one("Exited"), self.paths)["sid"], s)

    def test_background_session_with_no_pid_is_refused(self):
        s, _ = self.transcript("In Background")
        self.agents([{"id": s[:8], "name": "In Background", "sessionId": s, "kind": "background",
                      "state": "blocked", "cwd": self.folder}])
        with self.assertRaises(resume.Blocked) as ctx:
            resume.prepare(self.one("In Background"), self.paths)
        self.assertEqual(ctx.exception.kind, "background")
        self.assertIn("claude attach %s" % s[:8], str(ctx.exception))
        self.assertIn("moving ~/.claude/jobs/%s aside" % s[:8], str(ctx.exception))
        self.assertNotIn("claude rm", str(ctx.exception))

    def background(self, name):
        s, _ = self.transcript(name)
        self.agents([{"id": s[:8], "name": name, "sessionId": s, "kind": "background", "state": "blocked",
                      "cwd": self.folder}])
        return s

    def test_a_stale_registry_file_does_not_hide_a_background_hold(self):
        s = self.background("Hidden Hold")
        self.reg("Hidden Hold", s, pid=standin.DEAD_PID)
        with self.assertRaises(resume.Blocked) as ctx:
            resume.prepare(self.one("Hidden Hold"), self.paths)
        self.assertEqual(ctx.exception.kind, "background")
        held = resume.held_map(data.build_records(self.paths, time.time(), []))
        self.assertIn(s, held)

    def test_unreadable_sources_refuse_instead_of_failing_open(self):
        s, _ = self.transcript("Maybe Running")
        e = self.one("Maybe Running")
        with mock.patch.dict(self.paths, {"command": "false"}), self.assertRaises(resume.Blocked) as ctx:
            resume.prepare(e, self.paths)
        self.assertEqual(ctx.exception.kind, "unknown")
        self.assertIn("can't tell whether Maybe Running is running", str(ctx.exception))
        with mock.patch.dict(self.paths, {"sessions": os.path.join(self.root, "no-such")}), \
                self.assertRaises(resume.Blocked) as ctx:
            resume.prepare(e, self.paths)
        self.assertEqual(ctx.exception.kind, "unknown")
        self.assertEqual(resume.prepare(e, self.paths)["sid"], s)

    def test_a_transcript_written_in_the_last_minute_is_refused(self):
        self.transcript("Just Written", mtime=time.time() - 10)
        self.transcript("Written Earlier", mtime=time.time() - 120, slug="-b")
        with self.assertRaises(resume.Blocked) as ctx:
            resume.prepare(self.one("Just Written"), self.paths)
        self.assertEqual(ctx.exception.kind, "recent")
        self.assertIn("Try again in a minute", str(ctx.exception))
        self.assertTrue(resume.prepare(self.one("Written Earlier"), self.paths))

    def test_enter_in_the_tui_repeats_the_gate_before_the_exec(self):
        import io
        from fleetwatch import __main__ as entry_point
        self.background("Went Background")
        e = self.one("Went Background")

        class Tty(io.StringIO):
            def isatty(self):
                return True
        with mock.patch.object(data, "default_paths", return_value=self.paths), \
                mock.patch.object(sys, "stdout", Tty()), mock.patch.object(sys, "stdin", Tty()), \
                mock.patch.object(fapp, "run", return_value=e), \
                mock.patch.object(resume, "exec_here") as ex:
            rc = entry_point.main([])
        self.assertEqual(rc, 3)
        ex.assert_not_called()

    def test_folder_gone_is_refused(self):
        self.transcript("Lost Folder", cwd=os.path.join(self.root, "removed-folder"))
        e = self.one("Lost Folder")
        self.assertEqual(resume.tags(e), ["folder gone"])
        with self.assertRaises(resume.Blocked) as ctx:
            resume.prepare(e, self.paths)
        self.assertEqual(ctx.exception.kind, "gone")
        self.assertIn("removed-folder", str(ctx.exception))

    def test_folder_with_control_characters_or_relative_is_refused(self):
        for label, cwd in (("Newline", os.path.join(self.root, "a\nb")),
                           ("Escape", os.path.join(self.root, "a\x1b]0;x\x07b")),
                           ("Relative", "relative/folder")):
            if os.path.isabs(cwd):
                os.makedirs(cwd)
            self.transcript(label, cwd=cwd)
            e = dict(self.one(label), cwd=cwd)
            with self.assertRaises(resume.Blocked) as ctx:
                resume.prepare(e, self.paths)
            self.assertEqual(ctx.exception.kind, "unsafe", label)
            self.assertNotIn("\x1b", str(ctx.exception))
            self.assertNotIn("\n", str(ctx.exception))

    def test_parked_note_edited_by_hand_is_cleaned_on_load(self):
        s, _ = self.transcript("Noted")
        e = resume.build_entries({s: {"cwd": self.root, "mtime": 1, "path": "x", "folder_ok": True}},
                                 {s: {"name": "Noted", "note": "wait\x1b]0;x\x07 for it"}}, ({}, {}))
        self.assertEqual(e[0]["note"], "wait]0;x for it")

    def test_parking_shows_a_session_from_a_removed_worktree(self):
        s, _ = self.transcript("Lane Work", cwd=os.path.join(self.root, "repo-worktrees", "gone"))
        self.assertEqual(self.names(), [])
        park.park(s, "Lane Work", "keep this one")
        e = self.one("Lane Work")
        self.assertEqual(resume.tags(e), ["folder gone"])
        with self.assertRaises(resume.Blocked) as ctx:
            resume.prepare(e, self.paths)
        self.assertEqual(ctx.exception.kind, "gone")

    def test_archive_copy_comes_back_and_stays_in_the_archive(self):
        s, src = self.transcript("Swept Away", root=self.paths["archive"], slug="-swept", mtime=1500000)
        side = os.path.join(self.paths["archive"], "-swept", s, "tool-results")
        os.makedirs(side)
        with open(os.path.join(side, "x.txt"), "w") as fh:
            fh.write("kept")
        ready = resume.prepare(self.one("Swept Away"), self.paths)
        dest = os.path.join(self.paths["projects"], "-swept", s + ".jsonl")
        self.assertEqual(ready["path"], dest)
        self.assertFalse(ready["archived"])
        self.assertTrue(os.path.exists(src))
        self.assertEqual(os.path.getmtime(dest), 1500000)
        self.assertTrue(os.path.exists(os.path.join(self.paths["projects"], "-swept", s, "tool-results", "x.txt")))
        self.assertEqual(sorted(os.listdir(os.path.join(self.paths["projects"], "-swept"))), sorted([s + ".jsonl", s]))
        self.assertFalse(self.one("Swept Away")["archived"])

    def test_restore_never_overwrites(self):
        s, src = self.transcript("Swept", root=self.paths["archive"], slug="-swept")
        dest_dir = os.path.join(self.paths["projects"], "-swept")
        os.makedirs(dest_dir)
        dest = os.path.join(dest_dir, s + ".jsonl")
        with open(dest, "w") as fh:
            fh.write("newer\n")
        resume.restore_from_archive({"sid": s, "path": src}, self.paths)
        with open(dest) as fh:
            self.assertEqual(fh.read(), "newer\n")

    def test_parked_session_with_no_transcript_shows_and_is_refused(self):
        s = sid()
        park.park(s, "Lost One", "waiting")
        e = self.one("Lost One")
        self.assertTrue(e["parked"])
        self.assertEqual(resume.tags(e), ["no transcript"])
        with self.assertRaises(resume.Blocked) as ctx:
            resume.prepare(e, self.paths)
        self.assertEqual(ctx.exception.kind, "missing")


class HostileDataTests(Tree):
    """Text from transcripts and parked.json is data: it must not reach a terminal or a shell as control."""

    BAD_FOLDERS = ("bad\nfolder", "bad\rfolder", "bad\tfolder", "bad\x1b[31mfolder", "bad\x03folder",
                   "bad\x15folder", "bad\x9bfolder", "bad\x7ffolder")

    def one(self, name):
        return next(e for e in self.entries() if e["name"] == name)

    def test_folder_with_a_control_character_is_refused_even_though_it_exists(self):
        for i, folder in enumerate(self.BAD_FOLDERS):
            cwd = self.mkdir(folder)
            self.assertTrue(os.path.isdir(cwd))
            self.transcript("Bad %d" % i, cwd=cwd, slug="-bad%d" % i)
            e = self.one("Bad %d" % i)
            self.assertEqual(resume.tags(e), ["folder unusable"], repr(folder))
            with self.assertRaises(resume.Blocked) as ctx:
                resume.prepare(e, self.paths)
            self.assertEqual(ctx.exception.kind, "unsafe", repr(folder))
            self.assertNotRegex(str(ctx.exception), "[\x00-\x1f\x7f-\x9f]")

    def test_relative_folder_is_refused_even_though_it_exists(self):
        rel = os.path.relpath(self.folder)
        self.assertTrue(os.path.isdir(rel))
        self.transcript("Relative", cwd=rel)
        e = self.one("Relative")
        self.assertEqual(resume.tags(e), ["folder unusable"])
        with self.assertRaises(resume.Blocked) as ctx:
            resume.prepare(e, self.paths)
        self.assertEqual(ctx.exception.kind, "unsafe")

    def test_an_ordinary_odd_folder_is_still_fine(self):
        cwd = self.mkdir(NASTY)
        self.transcript("Odd But Fine", cwd=cwd)
        self.assertEqual(resume.prepare(self.one("Odd But Fine"), self.paths)["cwd"], cwd)

    def write_parked(self, sessions):
        os.makedirs(self.cfg, exist_ok=True)
        with open(config.parked_path(), "w") as fh:
            json.dump({"sessions": sessions}, fh)

    def test_parked_file_text_is_cleaned(self):
        s, _ = self.transcript(None)
        self.write_parked({s: {"name": "Na\x1b]0;evil\x07me", "note": "wait\x1b[2Jing\non\x9bit", "parked_at": "soon"}})
        e = self.one("Na]0;evilme")
        self.assertEqual(e["note"], "wait[2Jing onit")
        self.assertIsNone(e["parked_at"])
        text = resume.listing([e], standin.NOW).replace("\n", " ") + " ".join(resume.notes_for(e))
        self.assertNotRegex(text, "[\x00-\x1f\x7f-\x9f]")

    def test_parked_keys_must_be_session_ids(self):
        s = sid()
        self.write_parked({s: {"name": "Real", "note": "x"}, "--dangerously-skip-permissions": {"name": "Flag"},
                           "../../etc/passwd": {"name": "Path"}, s + "\n": {"name": "Trailing"}})
        self.assertEqual(self.names(), ["Real"])

    def test_session_ids_are_uuid_shaped_before_claude_sees_one(self):
        folder = os.path.join(self.paths["projects"], "-proj")
        os.makedirs(folder)
        good = sid()
        for name in ("--flag.jsonl", "not-a-uuid.jsonl", good.upper() + ".jsonl", good + ".jsonl\n",
                     good + "x.jsonl"):
            with open(os.path.join(folder, name), "w") as fh:
                fh.write(json.dumps({"cwd": self.folder, "entrypoint": "cli"}) + "\n")
                fh.write('{"type":"custom-title","customTitle":"Not A Session"}\n')
        self.assertEqual(self.names(), [])
        e = {"sid": "--flag", "name": "Hand Built", "path": "/x", "cwd": self.folder, "archived": False}
        with self.assertRaises(resume.Blocked) as ctx:
            resume.prepare(e, self.paths)
        self.assertEqual(ctx.exception.kind, "missing")


class ShellTests(Tree):
    def test_nasty_folder_survives_the_shell_and_never_enters_the_script(self):
        folder = self.mkdir(NASTY)
        out = os.path.join(self.root, "seen.txt")
        fake = os.path.join(self.root, "fake-claude")
        with open(fake, "w") as fh:
            fh.write('#!/bin/sh\npwd -P > "%s"\necho "$@" >> "%s"\n' % (out, out))
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        entry = {"sid": sid(), "cwd": folder}
        with mock.patch.dict(os.environ, {"FLEETWATCH_CLAUDE_CMD": fake}):
            line = resume.shell_line(entry)
        subprocess.run(["bash", "-c", line], check=True, cwd=self.root)
        with open(out) as fh:
            self.assertEqual(fh.read().splitlines(), [folder, "--resume " + entry["sid"]])
        self.assertFalse(os.path.exists(os.path.join(self.root, "pwned")))
        self.assertFalse(os.path.exists(os.path.join(folder, "pwned")))
        self.assertEqual(shlex.split(line)[1], folder)

    def test_a_new_window_runs_fleetwatch_by_id_with_no_folder_or_name_in_the_command(self):
        entry = {"sid": sid(), "cwd": self.mkdir(NASTY), "name": "It's $(a) name", "parked": False, "persona": None}
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            command = resume.window_command(entry)
        self.assertEqual(shlex.split(command)[:2], ["/bin/bash", "-ilc"])
        self.assertIn("fleetwatch resume %s --here" % entry["sid"], shlex.split(command)[2])
        self.assertNotIn("touch pwned", command)
        self.assertNotIn("name", command)
        seen = []
        with mock.patch.object(iterm, "osascript", lambda script, args=(): seen.append((script, args)) or (True, "42\n")):
            self.assertEqual(iterm.open_window(command), (True, "42"))
        self.assertNotIn(entry["sid"], seen[0][0])
        self.assertEqual(seen[0][1], [command])
        self.assertNotIn("write text", seen[0][0])

    def test_window_command_runs_in_a_real_shell_and_a_refusal_waits_for_a_key(self):
        out = os.path.join(self.root, "seen.txt")
        fake = os.path.join(self.root, "fake-fleetwatch")
        with open(fake, "w") as fh:
            fh.write('#!/bin/sh\necho "$@" > "%s"\nexit 1\n' % out)
        os.chmod(fake, 0o755)
        entry = {"sid": sid()}
        with mock.patch.dict(os.environ, {"SHELL": "/bin/bash", "FLEETWATCH_SELF_CMD": fake}):
            argv = shlex.split(resume.window_command(entry))
        # -i and -l would read this machine's shell startup files, which a test has no business doing.
        r = subprocess.run(["/bin/bash", "-c", argv[2]], input="\n", capture_output=True, text=True,
                           cwd=self.root, env=dict(os.environ, HOME=self.root))
        with open(out) as fh:
            self.assertEqual(fh.read().strip(), "resume %s --here" % entry["sid"])
        self.assertIn("Press Enter to close", r.stdout)

    def test_short_folder_keeps_the_telling_end(self):
        home = data.HOME
        od = home + "/Library/CloudStorage/OneDrive-Example(Corp)/Research/Latent"
        self.assertEqual(resume.short_folder(od), "OneDrive/Research/Latent")
        gd = home + "/Library/CloudStorage/GoogleDrive-a@b.com/My Drive/Teaching"
        self.assertEqual(resume.short_folder(gd), "Drive/Teaching")
        self.assertEqual(resume.short_folder(home + "/Library/CloudStorage/Dropbox/Apps"), "Dropbox/Apps")
        self.assertEqual(resume.short_folder(home + "/git/x"), "~/git/x")
        long = "/a" * 60 + "/tail"
        self.assertTrue(resume.short_folder(long).endswith("/tail"))
        self.assertEqual(len(resume.short_folder(long)), 64)


class ParkTests(Tree):
    def test_round_trip_and_replacing_a_note(self):
        s = sid()
        self.assertEqual(park.park(s, "Name", " grant\nsubmitted \x07"), "grant submitted")
        self.assertEqual(park.load()[s]["note"], "grant submitted")
        park.park(s, "Name", "award notice")
        self.assertEqual(park.load()[s]["note"], "award notice")
        self.assertEqual(park.unpark(s)["note"], "award notice")
        self.assertIsNone(park.unpark(s))
        self.assertEqual(park.load(), {})

    def test_write_status_and_prune_leave_parked_state_alone(self):
        s, _ = self.transcript("Parked Session")
        park.park(s, "Parked Session", "waiting")
        before = open(config.parked_path()).read()
        doc = write_status.build_document({"working_on": "x"}, "Parked Session", s)
        write_status.write_document(self.paths["status"], doc)
        settings = dict(config.default_prune(), quiet_s=0)
        records = data.build_records(self.paths, standin.NOW + 10 ** 9, [])
        plan = prune.make_plan(self.paths, standin.NOW + 10 ** 9, records, settings)
        prune.apply(plan, standin.NOW + 10 ** 9)
        self.assertEqual(open(config.parked_path()).read(), before)
        self.assertNotIn("parked.json", os.listdir(self.paths["status"]))

    def test_corrupt_file_is_never_replaced(self):
        os.makedirs(self.cfg, exist_ok=True)
        with open(config.parked_path(), "w") as fh:
            fh.write("{broken")
        with self.assertRaises(park.ParkedError):
            park.park(sid(), "Name", "note")
        with open(config.parked_path()) as fh:
            self.assertEqual(fh.read(), "{broken")

    def test_concurrent_parks_lose_nothing(self):
        ids = [sid() for _ in range(40)]
        threads = [threading.Thread(target=park.park, args=(s, "N", "note")) for s in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(set(park.load()), set(ids))

    def test_an_out_of_range_parked_at_is_ignored(self):
        s, _ = self.transcript("Odd Date")
        for bad in (1e30, float("nan"), -1e30, "soon", True):
            parked = {s: {"name": "Odd Date", "note": "n", "parked_at": bad}}
            entry = resume.build_entries(resume.scan(self.paths), parked, ({}, {}))[0]
            self.assertIsNone(entry["parked_at"], bad)
            resume.listing([entry], time.time())

    def test_the_chooser_survives_a_superscript_digit(self):
        out = __import__("io").StringIO()
        entries = [dict(name="A", persona=None, last=1.0, cwd="/x", parked=False, held=None, path="p",
                        archived=False, folder_ok=True)]
        self.assertIsNone(resume.choose(entries, out, now=2.0, ask=lambda _: "\u00b2"))
        self.assertIn("no such row", out.getvalue())

    def test_parked_first_with_notes(self):
        a, _ = self.transcript("Newest", mtime=3000000)
        b, _ = self.transcript("Oldest", mtime=1000000, slug="-b")
        park.park(b, "Oldest", "grant submitted")
        rows = self.entries()
        self.assertEqual([e["name"] for e in rows], ["Oldest", "Newest"])
        self.assertIn("grant submitted", resume.listing(rows, standin.NOW))

    def test_a_parked_unnamed_session_shows(self):
        s, _ = self.transcript(None)
        park.park(s, "derived-name-4e", "later")
        self.assertEqual(self.names(), ["derived-name-4e"])


class StandIns(Tree):
    """A stand-in claude and osascript that record how they were called."""

    def setUp(self):
        super().setUp()
        self.out = os.path.join(self.root, "standin-claude.txt")
        self.fake = os.path.join(self.root, "standin-claude")
        with open(self.fake, "w") as fh:
            fh.write('#!/bin/sh\n{ pwd -P; echo "$@"; echo "$$"; } > "%s"\n' % self.out)
        os.chmod(self.fake, 0o755)
        self.extra = {}          # environment a test overrides for one fleetwatch call
        self.osa_log = os.path.join(self.root, "osa.txt")
        self.osa = os.path.join(self.root, "standin-osa")
        with open(self.osa, "w") as fh:
            fh.write('#!/bin/sh\ncat > /dev/null\nprintf "%%s\\n" "$@" > "%s"\necho 777\n' % self.osa_log)
        os.chmod(self.osa, 0o755)

    def fleetwatch(self, *args, stdin=None):
        p = self.paths
        env = dict(os.environ, FLEETWATCH_AGENTS_CMD=p["command"], FLEETWATCH_PS_CMD="true", FLEETWATCH_SESSIONS_DIR=p["sessions"],
                   FLEETWATCH_JOBS_DIR=p["jobs"], FLEETWATCH_STATUS_DIR=p["status"], FLEETWATCH_PROJECTS_DIR=p["projects"],
                   FLEETWATCH_ARCHIVE_DIR=p["archive"], FLEETWATCH_CONFIG_DIR=self.cfg, FLEETWATCH_CLAUDE_CMD=self.fake,
                   FLEETWATCH_OSASCRIPT=self.osa, FLEETWATCH_TTY_CMD="true", PYTHONIOENCODING="utf-8")
        env.update(self.extra)
        return subprocess.run([sys.executable, "-m", "fleetwatch", *args], cwd=ROOT, env=env, capture_output=True,
                              text=True, input=stdin or "", timeout=60)


class CommandLine(StandIns):
    def test_no_name_lists_newest_first_with_folder_and_date(self):
        self.transcript("Older One", mtime=1000000)
        self.transcript("Newer One", mtime=1700000000, slug="-b")
        r = self.fleetwatch("resume")
        self.assertEqual(r.returncode, 0)
        self.assertLess(r.stdout.index("Newer One"), r.stdout.index("Older One"))
        self.assertIn("Nov 14 2023", r.stdout)
        self.assertIn("work", r.stdout)
        self.assertFalse(os.path.exists(self.osa_log))

    def test_no_name_leaves_out_running_sessions(self):
        s, _ = self.transcript("Running Now")
        self.reg("Running Now", s)
        self.transcript("Stopped", slug="-b")
        r = self.fleetwatch("resume")
        self.assertIn("Stopped", r.stdout)
        self.assertNotIn("Running Now", r.stdout)

    def test_one_match_opens_a_new_window_in_its_folder(self):
        folder = self.mkdir(NASTY)
        s, _ = self.transcript("Quoted Folder", cwd=folder)
        r = self.fleetwatch("resume", "quoted")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("window 777", r.stdout)
        with open(self.osa_log) as fh:
            argv = fh.read().splitlines()
        self.assertEqual(argv[0], "-")
        self.assertIn("fleetwatch resume %s --here" % s, argv[1])
        self.assertNotIn("pwned", argv[1])

    def test_several_matches_without_a_terminal_list_and_stop(self):
        self.transcript("Same Name", mtime=1000000)
        self.transcript("Same Name", cwd=self.mkdir("other"), mtime=2000000, slug="-b")
        r = self.fleetwatch("resume", "same")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout.count("Same Name"), 2)
        self.assertIn("other", r.stdout)
        self.assertFalse(os.path.exists(self.osa_log))

    def test_refusals(self):
        s, _ = self.transcript("Running Now")
        self.reg("Running Now", s)
        r = self.fleetwatch("resume", "running")
        self.assertEqual(r.returncode, 3)
        self.assertIn("still running", r.stdout)
        self.transcript("Lost Folder", cwd=os.path.join(self.root, "gone"), slug="-b")
        r = self.fleetwatch("resume", "lost")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no longer exists", r.stdout)
        # The live refusal may ask iTerm2 for its tabs, but no window was opened and claude never ran.
        if os.path.exists(self.osa_log):
            self.assertNotIn("--resume", open(self.osa_log).read())
        self.assertFalse(os.path.exists(self.out))
        r = self.fleetwatch("resume", "zzzzzz")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no custom-named session matches", r.stderr)

    def test_here_execs_in_the_folder_with_persona_note_and_unparks(self):
        folder = self.mkdir(NASTY)
        s, _ = self.transcript("Persona Session", cwd=folder)
        os.makedirs(os.path.join(self.paths["status"], ".pruned"))
        with open(os.path.join(self.paths["status"], ".pruned", "x.json"), "w") as fh:
            json.dump({"session": "Persona Session", "sessionId": s, "persona": "Robin",
                       "updated": standin.iso(0)}, fh)
        self.assertEqual(self.fleetwatch("park", s[:13], "--note", "grant submitted").returncode, 0)
        r = self.fleetwatch("resume", "persona", "--here")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("It was parked: grant submitted.", r.stdout)
        self.assertIn("Robin ran on a scheduled loop", r.stdout)
        with open(self.out) as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines[:2], [folder, "--resume " + s])
        self.assertEqual(json.load(open(config.parked_path())), {"sessions": {}})

    def test_a_new_window_leaves_parking_to_the_fleetwatch_that_execs(self):
        s, _ = self.transcript(None)                     # listed only because it is parked
        self.assertEqual(self.fleetwatch("park", s, "--note", "later").returncode, 1)  # not named: no match
        park.park(s, "derived-name", "later")
        self.assertEqual(self.fleetwatch("resume", s).returncode, 0)
        self.assertIn(s, park.load())
        r = self.fleetwatch("resume", s, "--here")            # what the new window runs
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(s, park.load())
        with open(self.out) as fh:
            self.assertEqual(fh.read().splitlines()[1], "--resume " + s)

    def test_bad_folder_never_reaches_a_fallback_or_a_terminal_raw(self):
        cwd = self.mkdir("evil\x1b[31m\nfolder")
        s, _ = self.transcript("Evil Folder", cwd=cwd)
        for args, env in ((("resume", s, "--here"), {"FLEETWATCH_CLAUDE_CMD": "no-such-claude-here"}),
                          (("resume", s), {"FLEETWATCH_OSASCRIPT": "false"}),
                          (("resume",), {})):
            self.extra = env
            r = self.fleetwatch(*args)
            self.assertNotIn("\x1b", r.stdout + r.stderr, args)
            if args != ("resume",):
                self.assertEqual(r.returncode, 1, args)
                self.assertIn("can't be used", r.stdout, args)
                self.assertNotIn("run this yourself", r.stdout.lower(), args)
        self.assertFalse(os.path.exists(self.out))
        if os.path.exists(self.osa_log):
            self.assertNotIn(s, open(self.osa_log).read())

    def test_fallbacks_still_help_for_a_good_folder(self):
        folder = self.mkdir(NASTY)
        s, _ = self.transcript("Good Folder", cwd=folder)
        self.extra = {"FLEETWATCH_OSASCRIPT": "false"}
        r = self.fleetwatch("resume", s)
        self.assertEqual(r.returncode, 1)
        self.assertIn("could not reach iTerm2", r.stdout)
        self.assertIn(shlex.quote(folder), r.stdout)
        self.extra = {"FLEETWATCH_CLAUDE_CMD": "no-such-claude-here"}
        r = self.fleetwatch("resume", s, "--here")
        self.assertEqual(r.returncode, 1)
        self.assertIn("is not on PATH", r.stdout)
        self.assertIn(shlex.quote(folder), r.stdout)

    def test_park_finds_the_calling_session_and_names_work(self):
        s, _ = self.transcript("Self Parker")
        with open(os.path.join(self.paths["sessions"], "%d.json" % os.getpid()), "w") as fh:
            json.dump({"name": "Self Parker", "sessionId": s, "pid": os.getpid()}, fh)
        with mock.patch.dict(os.environ, {"FLEETWATCH_PS_CMD": "ps -axo pid=,ppid="}):
            p = self.paths
            env = dict(os.environ, FLEETWATCH_SESSIONS_DIR=p["sessions"], FLEETWATCH_CONFIG_DIR=self.cfg,
                       FLEETWATCH_PROJECTS_DIR=p["projects"], FLEETWATCH_STATUS_DIR=p["status"])
            r = subprocess.run([sys.executable, "-m", "fleetwatch", "park", "--note", "waiting on it"],
                               cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("parked Self Parker: waiting on it", r.stdout)
        self.assertEqual(park.load()[s]["note"], "waiting on it")
        r = self.fleetwatch("unpark", "self parker")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(s, park.load())

    def test_park_outside_a_session_says_how(self):
        r = self.fleetwatch("park", "--note", "x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("name one", r.stderr)
        self.assertEqual(self.fleetwatch("park", "nothing-like-this", "--note", "x").returncode, 1)

    def test_name_with_several_matches_does_not_park_without_a_choice(self):
        self.transcript("Same Name")
        self.transcript("Same Name", slug="-b")
        self.assertEqual(self.fleetwatch("park", "same", "--note", "x").returncode, 1)
        self.assertEqual(park.load(), {})


class AppWorld(Tree):
    """The app over fixture transcripts. Held sessions come through the snapshot, as in gather()."""

    def snapshot(self):
        recs = data.build_records(self.paths, standin.NOW, [], ppids=standin.PPIDS)
        sections, stale = data.classify(recs, standin.NOW, 24 * 3600, True)
        jump = {"ttys": {standin.PARENT_PID: TTY}, "iterm": {TTY}, "err": None}
        groups, bad = config.load_groups()
        return fapp.Snapshot(sections, stale, standin.NOW, [], 21, jump, groups, bad, None, resume.held_map(recs))

    def app(self):
        return fapp.FleetwatchApp(self.snapshot, interval=3600, paths=self.paths)

    def drive(self, body):
        async def go():
            app = self.app()
            async with app.run_test(size=(120, 30)) as pilot:
                await boot(app, pilot)
                for _ in range(100):
                    if app.resume_data is not None:
                        break
                    await pilot.pause(0.05)
                await pilot.press("4")
                await pilot.pause(0.1)
                await body(app, pilot)
            return app
        return asyncio.run(go())

    def rows(self, app):
        return app.lists[views.RESUME_TAB].visible_text()

    async def settle(self, pilot, check):
        for _ in range(100):
            if check():
                return
            await pilot.pause(0.05)
        self.fail("condition never held")


class AppTests(AppWorld):
    def test_tab_shows_parked_first_with_notes_and_hides_running(self):
        a, _ = self.transcript("Plain Stopped", mtime=3000000)
        b, _ = self.transcript("Parked Earlier", mtime=1000000, slug="-b")
        c, _ = self.transcript("Live Now", slug="-c")
        self.reg("Live Now", c)
        self.transcript("Headless", entry="sdk-cli", slug="-d")
        park.park(b, "Parked Earlier", "grant submitted")

        async def body(app, pilot):
            text = self.rows(app)
            self.assertLess(text.index("Parked Earlier"), text.index("Plain Stopped"))
            self.assertIn("grant submitted", text)
            self.assertNotIn("Live Now", text)
            self.assertNotIn("Headless", text)
            self.assertIn("4 Resume · 2", app.query_one(fapp.TabBar).plain)
            # The cursor lands on a session, not the Parked header.
            self.assertTrue(app.current.current().resume)
            self.assertIn("resume here", app.footer_plain)
            self.assertIn("o new window", app.footer_plain)
        self.drive(body)

    def test_enter_exits_with_the_entry_to_exec(self):
        s, _ = self.transcript("Come Back")

        async def body(app, pilot):
            await pilot.press("enter")
            await self.settle(pilot, lambda: app._exit)
            self.assertEqual(app.return_value["sid"], s)
        self.drive(body)

    def test_o_opens_a_window_once_and_stays(self):
        s, _ = self.transcript("Open Me")
        calls = []

        def fake(script, args=()):
            calls.append(list(args))
            return True, "9\n"

        async def body(app, pilot):
            await pilot.press("o")
            await self.settle(pilot, lambda: calls and "opened Open Me" in (app.status_text or ""))
            await pilot.press("o")
            await pilot.pause(0.2)
            self.assertEqual(len(calls), 1)
            self.assertIn("just opened", app.status_text)
            self.assertFalse(app._exit)
        with mock.patch.object(iterm, "osascript", fake):
            self.drive(body)
        self.assertIn("resume %s --here" % s, calls[0][0])

    def test_iterm_failure_is_said_and_keeps_it_parked(self):
        s, _ = self.transcript("Open Me")
        park.park(s, "Open Me", "note")

        async def body(app, pilot):
            await pilot.press("o")
            await self.settle(pilot, lambda: "could not reach iTerm2" in (app.status_text or ""))
        with mock.patch.object(iterm, "osascript", lambda script, args=(): (False, "no iTerm")):
            self.drive(body)
        self.assertIn(s, park.load())

    def test_hotkey_window_never_becomes_the_session(self):
        self.transcript("From Hotkey")
        calls = []

        async def body(app, pilot):
            self.assertIn("⏎ new window", app.footer_plain)
            self.assertNotIn("resume here", app.footer_plain)
            await pilot.press("enter")
            await self.settle(pilot, lambda: calls)
            await pilot.pause(0.1)
            self.assertFalse(app._exit)
        with mock.patch.dict(os.environ, {"ITERM_PROFILE": "fleetwatch"}), \
                mock.patch.object(iterm, "osascript", lambda s, args=(): calls.append(args) or (True, "1")):
            self.drive(body)

    def test_find_box_filters_and_resumes(self):
        self.transcript("Grant Proposal", slug="-a", mtime=3000000)
        want, _ = self.transcript("Docs Site", slug="-b", mtime=1000000)

        async def body(app, pilot):
            await pilot.press("slash")
            await self.settle(pilot, lambda: isinstance(app.screen, fapp.ResumeFindScreen))
            await self.settle(pilot, lambda: len(app.screen.shown) == 2)
            self.assertEqual(app.screen.shown[0]["name"], "Grant Proposal")   # newest first, before typing
            for ch in "doc":
                await pilot.press(ch)
            # Folders match by prefix, word start or substring only, so scattered letters in the
            # temp folder's path do not list Grant Proposal.
            await self.settle(pilot, lambda: [e["name"] for e in app.screen.shown] == ["Docs Site"])
            await pilot.press("enter")
            await self.settle(pilot, lambda: app._exit)
            self.assertEqual(app.return_value["sid"], want)
        self.drive(body)

    def test_find_box_enter_never_also_jumps(self):
        self.transcript("Lost Folder", cwd=os.path.join(self.root, "gone"))
        jumps = []

        async def body(app, pilot):
            app.find_jump = jumps.append
            await pilot.press("slash")
            await pilot.pause(0.1)
            await pilot.press("enter")
            await self.settle(pilot, lambda: "no longer exists" in (app.status_text or ""))
            self.assertEqual(jumps, [])
            self.assertIsInstance(app.screen, fapp.ResumeFindScreen)
        self.drive(body)

    def test_find_rows_do_not_wrap(self):
        long_folder = self.mkdir("a-very-long-folder-name/" * 3 + "tail")
        self.transcript("A Rather Long Session Name For The Box", cwd=long_folder)

        async def body(app, pilot):
            await pilot.press("slash")
            await pilot.pause(0.1)
            e = app.screen.shown[0]
            self.assertLessEqual(len(e["name"]) + len(e["group"]) + len(e["status"]) + 4, 68)
        self.drive(body)

    def test_find_box_ctrl_o_opens_a_window_and_closes(self):
        self.transcript("Docs Site")
        calls = []

        async def body(app, pilot):
            await pilot.press("slash")
            await pilot.pause(0.1)
            await pilot.press("ctrl+o")
            await self.settle(pilot, lambda: calls)
            await pilot.pause(0.2)
            self.assertNotIsInstance(app.screen, fapp.ResumeFindScreen)
            self.assertFalse(app._exit)
        with mock.patch.object(iterm, "osascript", lambda s, args=(): calls.append(args) or (True, "1")):
            self.drive(body)

    def test_find_on_other_tabs_is_still_the_jump_box(self):
        self.transcript("Named")

        async def body(app, pilot):
            await pilot.press("1")
            await pilot.press("slash")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, fapp.FindScreen)
            self.assertNotIsInstance(app.screen, fapp.ResumeFindScreen)
        self.drive(body)

    def test_p_parks_with_a_note_and_u_unparks(self):
        s, _ = self.transcript("Park Me")

        async def body(app, pilot):
            await pilot.press("p")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, fapp.ParkScreen)
            for ch in "later":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause(0.1)
            self.assertEqual(park.load()[s]["note"], "later")
            self.assertIn("later", self.rows(app))
            self.assertIn("Parked · 1", self.rows(app))
            # Parking again edits the note that is there.
            await pilot.press("p")
            await pilot.pause(0.1)
            self.assertEqual(app.screen.query_one("#note", Input).value, "later")
            await pilot.press("escape")
            await pilot.pause(0.1)
            self.assertEqual(park.load()[s]["note"], "later")
            await pilot.press("u")
            await pilot.pause(0.1)
            self.assertNotIn(s, park.load())
            self.assertNotIn("Parked", self.rows(app))
        self.drive(body)

    def test_park_from_the_sessions_tab(self):
        s, _ = self.transcript("Live Session")
        self.reg("Live Session", s)

        async def body(app, pilot):
            await pilot.press("3")
            await pilot.pause(0.1)
            app.current.move_to("ses:Live Session")
            await pilot.press("p")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, fapp.ParkScreen)
            await pilot.press("x", "enter")
            await pilot.pause(0.1)
            self.assertEqual(park.load()[s]["note"], "x")
            # Parked and running: it shows on the Resume tab, tagged, and Enter refuses.
            await pilot.press("4")
            await pilot.pause(0.1)
            self.assertIn("running", self.rows(app))
        self.drive(body)

    def test_running_parked_row_refuses_and_offers_the_jump(self):
        s, _ = self.transcript("Live Parked")
        self.reg("Live Parked", s)
        park.park(s, "Live Parked", "note")
        calls = []

        async def body(app, pilot):
            await pilot.press("enter")
            await self.settle(pilot, lambda: isinstance(app.screen, fapp.StillRunningScreen))
            self.assertFalse(app._exit)
            await pilot.press("enter")
            await self.settle(pilot, lambda: calls)
            self.assertEqual(calls[0], [TTY])
            await pilot.pause(0.2)
            self.assertFalse(app._exit)
        with mock.patch.object(iterm, "osascript", lambda s, args=(): calls.append(list(args)) or (True, "ok")):
            self.drive(body)

    def test_session_that_went_live_after_the_list_was_drawn_is_refused(self):
        s, _ = self.transcript("Started Since")

        async def body(app, pilot):
            self.assertIn("Started Since", self.rows(app))
            self.reg("Started Since", s)
            await pilot.press("enter")
            await self.settle(pilot, lambda: isinstance(app.screen, fapp.StillRunningScreen))
            self.assertFalse(app._exit)
        with mock.patch.object(iterm, "jump_context", lambda: {"ttys": {}, "iterm": set(), "err": None}):
            self.drive(body)

    def test_folder_gone_is_said_on_the_row_and_on_enter(self):
        self.transcript("Lost Folder", cwd=os.path.join(self.root, "gone"))

        async def body(app, pilot):
            self.assertIn("folder gone", self.rows(app))
            await pilot.press("enter")
            await self.settle(pilot, lambda: "no longer exists" in (app.status_text or ""))
            self.assertFalse(app._exit)
        self.drive(body)

    def test_unusable_folder_is_said_on_the_row_and_refused_on_enter(self):
        self.transcript("Evil Folder", cwd=self.mkdir("evil\x1b[31mfolder"))

        async def body(app, pilot):
            self.assertIn("folder unusable", self.rows(app))
            self.assertNotIn("\x1b", self.rows(app))
            await pilot.press("enter")
            await self.settle(pilot, lambda: "can't be used" in (app.status_text or ""))
            self.assertNotIn("\x1b", app.status_text)
            self.assertFalse(app._exit)
        self.drive(body)

    def test_same_name_rows_are_told_apart(self):
        self.transcript("Same Name", mtime=1000000)
        self.transcript("Same Name", cwd=self.mkdir("elsewhere"), mtime=2000000, slug="-b")

        async def body(app, pilot):
            text = self.rows(app)
            self.assertEqual(text.count("Same Name"), 2)
            self.assertIn("elsewhere", text)
            self.assertIn("work", text)
        self.drive(body)

    def test_sort_and_groups_cover_the_tab(self):
        self.transcript("Beta", mtime=3000000)
        self.transcript("Alpha", mtime=1000000, slug="-b")

        async def body(app, pilot):
            text = self.rows(app)
            self.assertLess(text.index("Beta"), text.index("Alpha"))
            await pilot.press("s")
            await pilot.pause(0.1)
            text = self.rows(app)
            self.assertLess(text.index("Alpha"), text.index("Beta"))
            await pilot.press("G")
            await pilot.pause(0.1)
            self.assertIn("Other · 2", self.rows(app))
        self.drive(body)

    def test_row_opens_to_the_full_folder_and_i_adds_the_id_and_transcript(self):
        s, _ = self.transcript("Details")

        async def body(app, pilot):
            await pilot.press("space")
            await pilot.pause(0.1)
            text = self.rows(app)
            self.assertIn(self.folder, text.replace("\n", ""))
            self.assertNotIn(s, text)               # the session ID is in the details view, not the row
            await pilot.press("i")
            await pilot.pause(0.2)
            shown = str(app.screen.query_one("#detailtext").render())
            self.assertIn(s, shown)
            self.assertIn(self.folder, shown.replace("\n", ""))
        self.drive(body)

    def test_narrow_tab_bar_keeps_all_four(self):
        self.transcript("Named")

        async def go():
            app = self.app()
            async with app.run_test(size=(60, 20)) as pilot:
                await boot(app, pilot)
                plain = app.query_one(fapp.TabBar).plain
                self.assertLessEqual(len(plain), 58)
                self.assertIn("4 ", plain)
                await pilot.press("4")
                self.assertLessEqual(len(app.footer_plain), 58)
                self.assertIn("/ find", app.footer_plain)
        asyncio.run(go())

    def test_eighty_columns_keeps_the_real_tab_names(self):
        self.transcript("Named")

        async def go():
            app = self.app()
            async with app.run_test(size=(80, 24)) as pilot:
                await boot(app, pilot)
                bar = app.query_one(fapp.TabBar)
                bar.update_tabs([14, 22, 39, 20, 4], 0)
                self.assertIn("3 Sessions", bar.plain)
                self.assertLessEqual(len(bar.plain), 78)
        asyncio.run(go())

    def test_a_slow_scan_does_not_hold_up_the_list_or_refresh(self):
        self.transcript("Named")
        release = threading.Event()
        real = resume.load

        def slow(paths):
            release.wait(10)
            return real(paths)

        async def go():
            app = self.app()
            async with app.run_test(size=(100, 30)) as pilot:
                await boot(app, pilot)
                self.assertGreaterEqual(app.refreshes, 1)
                await pilot.press("4")
                await pilot.pause(0.1)
                self.assertIn("Loading", self.rows(app))
                before = app.refreshes
                app.refresh_now()
                for _ in range(100):
                    await pilot.pause(0.05)
                    if app.refreshes > before:
                        break
                self.assertGreater(app.refreshes, before)
                self.assertIsNone(app.resume_data)
                release.set()
                await self.settle(pilot, lambda: app.resume_data is not None)
                self.assertIn("Named", self.rows(app))
        with mock.patch.object(resume, "load", slow):
            asyncio.run(go())

    def test_a_corrupt_parked_file_still_shows_sessions_and_says_so(self):
        self.transcript("Still Listed")
        os.makedirs(self.cfg, exist_ok=True)
        with open(config.parked_path(), "w") as fh:
            fh.write("{broken")

        async def body(app, pilot):
            self.assertIn("Still Listed", self.rows(app))
            self.assertIn("parked.json", app.status_text or "")
        self.drive(body)


class ExecInATerminal(StandIns):
    """The whole path in a real pseudo-terminal: keys, exit, terminal restored, exec."""

    def drive(self, keys, delay=3.0):
        import pty
        import select
        import termios
        import time
        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)
        p = self.paths
        env = dict(os.environ, FLEETWATCH_AGENTS_CMD=p["command"], FLEETWATCH_PS_CMD="true", FLEETWATCH_SESSIONS_DIR=p["sessions"],
                   FLEETWATCH_JOBS_DIR=p["jobs"], FLEETWATCH_STATUS_DIR=p["status"], FLEETWATCH_PROJECTS_DIR=p["projects"],
                   FLEETWATCH_ARCHIVE_DIR=p["archive"], FLEETWATCH_CONFIG_DIR=self.cfg, FLEETWATCH_CLAUDE_CMD=self.fake,
                   FLEETWATCH_OSASCRIPT="false", FLEETWATCH_TTY_CMD="true", TERM="xterm-256color", COLUMNS="100",
                   LINES="30", PYTHONIOENCODING="utf-8", ITERM_PROFILE="Default")
        proc = subprocess.Popen([sys.executable, "-m", "fleetwatch", "--interval", "1"], env=env, cwd=ROOT,
                                stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        buf, start, sent, end = b"", time.time(), 0, time.time() + 25
        while time.time() < end and proc.poll() is None:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    buf += os.read(master, 65536)
                except OSError:
                    break
            if sent < len(keys) and time.time() - start >= delay + sent * 0.6:
                os.write(master, keys[sent])
                sent += 1
        code = proc.wait(timeout=15)
        try:
            while select.select([master], [], [], 0.3)[0]:
                buf += os.read(master, 65536)
        except OSError:
            pass
        after = termios.tcgetattr(slave)
        os.close(master)
        os.close(slave)
        before[3] &= ~0x20000000
        after[3] &= ~0x20000000
        return proc.pid, code, buf.decode("utf-8", "replace"), before, after

    def test_enter_on_the_tab_becomes_the_session(self):
        folder = self.mkdir(NASTY)
        s, _ = self.transcript("Come Back Here", cwd=folder)
        pid, code, out, before, after = self.drive([b"4", b"\r"])
        self.assertEqual(code, 0, out[-400:])
        self.assertIn("\x1b[?1049l", out)
        self.assertLess(out.rindex("\x1b[?1049l"), out.rindex("Resuming Come Back Here"))
        self.assertEqual(before, after)
        with open(self.out) as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines, [folder, "--resume " + s, str(pid)])     # same pid: an exec, no wrapper

    def test_find_box_path(self):
        self.transcript("Some Other", slug="-a")
        s, _ = self.transcript("Docs Site", slug="-b")
        pid, code, out, before, after = self.drive([b"4", b"/", b"d", b"o", b"c", b"\r"])
        self.assertEqual(code, 0, out[-400:])
        with open(self.out) as fh:
            self.assertEqual(fh.read().splitlines()[1], "--resume " + s)


if __name__ == "__main__":
    unittest.main()


class LeftOffTests(Tree):
    """A stopped session's row says where it left off, from its last status file."""
    NOW = standin.NOW

    def status(self, fname, folder=None, **d):
        folder = folder or self.paths["status"]
        os.makedirs(folder, exist_ok=True)
        d.setdefault("updated", "2026-10-16T14:00:00-06:00")
        with open(os.path.join(folder, fname), "w") as fh:
            json.dump(d, fh)

    def one(self, name):
        return next(e for e in self.entries() if e["name"] == name)

    def line(self, name):
        return resume.left_off(self.one(name), self.NOW)

    def test_line_from_working_on_next_questions_and_age(self):
        s, _ = self.transcript("Lines")
        self.status("a.json", session="Lines", sessionId=s, working_on="v0.5.1 waiting on #972",
                    questions=["a", "b"],
                    upcoming=[{"what": "later", "due": "2026-11-20", "yours": True},
                              {"what": "start #975", "due": "2026-11-10", "yours": True},
                              {"what": "not mine", "due": "2026-10-01", "yours": False}])
        self.assertEqual(self.line("Lines"), "was: v0.5.1 waiting on #972 · next: Nov 10 start #975"
                                             " · 2 questions · status 2 d ago")

    def test_undated_item_and_no_questions(self):
        s, _ = self.transcript("Undated")
        self.status("u.json", session="Undated", sessionId=s, working_on="idle",
                    upcoming=[{"what": "call Sam", "yours": True}])
        self.assertEqual(self.line("Undated"), "was: idle · next: (no date) call Sam · status 2 d ago")

    def test_dated_item_beats_undated(self):
        s, _ = self.transcript("Both")
        self.status("b.json", session="Both", sessionId=s, upcoming=[
            {"what": "undated", "yours": True}, {"what": "dated", "due": "2026-12-01", "yours": True}])
        self.assertIn("next: Dec 1 dated", self.line("Both"))

    def test_lookup_by_session_id_before_name_and_in_pruned(self):
        s, _ = self.transcript("Lookup")
        self.status("by_name.json", session="Lookup", working_on="by name only")
        self.assertIn("was: by name only", self.line("Lookup"))
        self.status("by_id.json", folder=prune.pruned_dir(self.paths["status"]), session="Lookup",
                    sessionId=s, working_on="by id, pruned")
        self.assertIn("was: by id, pruned", self.line("Lookup"))

    def test_a_file_for_another_session_id_is_not_used_by_name(self):
        s, _ = self.transcript("Other")
        self.status("o.json", session="Other", sessionId=sid(), working_on="someone else")
        self.assertIsNone(self.one("Other")["left"])

    def test_no_status_file_shows_nothing(self):
        s, _ = self.transcript("Bare")
        park.park(s, "Bare", "n")
        e = self.one("Bare")
        self.assertIsNone(resume.left_off(e, self.NOW))
        self.assertEqual(resume.listing([e], self.NOW).count("\n"), 1)
        self.assertEqual(resume.notes_for(e), ["It was parked: n."])
        self.assertNotIn("was:", views.resume_node(e, self.NOW).head.plain)

    def test_parked_shows_note_then_left_off_in_listing_notes_and_row(self):
        s, _ = self.transcript("Parked One")
        self.status("p.json", session="Parked One", sessionId=s, working_on="doing x")
        park.park(s, "Parked One", "waiting on award")
        e = self.one("Parked One")
        lines = resume.listing([e], self.NOW).split("\n")
        self.assertIn("parked", lines[1])
        self.assertIn("was: doing x", lines[2])
        notes = resume.notes_for(e, self.NOW)
        self.assertEqual(notes[0], "It was parked: waiting on award.")
        self.assertTrue(notes[1].startswith("Where it left off: was: doing x"))
        node = views.resume_node(e, self.NOW)
        self.assertIn("waiting on award", node.head.plain)
        self.assertNotIn("was: doing x", node.head.plain)
        self.assertEqual(node.body[0][1].plain.split(" · ")[0], "was: doing x")

    def test_unparked_row_shows_the_line_under_the_name(self):
        s, _ = self.transcript("Plain")
        self.status("q.json", session="Plain", sessionId=s, working_on="doing y")
        node = views.resume_node(self.one("Plain"), self.NOW)
        self.assertNotIn("was:", node.head.plain)
        self.assertEqual(node.body[0][1].plain.split(" · ")[0], "was: doing y")

    def test_control_characters_are_cleaned(self):
        s, _ = self.transcript("Dirty")
        self.status("d.json", session="Dirty", sessionId=s, working_on="a\x1b]0;x\x07b\nc",
                    questions=["q\x1b[2J"], upcoming=[{"what": "t\x1bz", "yours": True}])
        line = self.line("Dirty")
        self.assertNotIn("\x1b", line)
        self.assertNotIn("\x07", line)
        self.assertNotIn("\n", line)
        self.assertIn("was: a]0;xb c", line)
