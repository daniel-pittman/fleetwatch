"""Caller classification and the index-content guard with synthetic process ancestry."""
import io
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from fleetwatch import index_cli, indexes, search_cmd
from fleetwatch.memory import routing
from fleetwatch.memory import refresh


REG = {"indexes": {"private": {"group": "private", "owner_session": "Owner",
                              "owner_session_id": "owner-id"},
                   "public": {"group": "public", "owner_session": "Other"}},
       "groups": {"private": {"readers": ["*"]}, "public": {"readers": ["*"]}}}
PROTECTED = frozenset(("private",))


class Tree(dict):
    def __init__(self, parents, names=None):
        super().__init__(parents)
        self.names = names or {}

    def process_name(self, pid):
        return self.names.get(pid)


class Terminal:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


class CallerGuardTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.paths = {"sessions": folder.name}

    def caller(self, tree, env=None, tty=True):
        return indexes.classify_caller(self.paths, tree, 600, env or {}, Terminal(tty))

    def refusal(self, caller, target):
        return indexes.read_refusal(REG, caller, target, PROTECTED)

    def test_codex_app_server_is_untrusted_even_with_a_tty(self):
        caller = self.caller(Tree({600: 500, 500: 1}, {500: "/opt/bin/codex"}))
        self.assertEqual(caller.kind, "untrusted")
        self.assertIn("codex process", self.refusal(caller, "private"))
        self.assertIn("not identified as a Claude session or the owner",
                      self.refusal(caller, "private"))
        self.assertIsNone(self.refusal(caller, "public"))
        detached = self.caller(Tree({600: 500, 500: 1}, {500: "/opt/bin/codex"}),
                               {"CODEX_SANDBOX": "1"}, tty=False)
        self.assertIn("codex process", self.refusal(detached, "private"))

    def test_agy_is_untrusted(self):
        caller = self.caller(Tree({600: 500, 500: 1}, {500: "/opt/bin/agy"}))
        self.assertIn("agy process", self.refusal(caller, "private"))
        self.assertIsNone(self.refusal(caller, "public"))

    def test_non_tty_and_codex_environment_are_untrusted(self):
        tree = Tree({600: 1})
        caller = self.caller(tree, tty=False)
        self.assertIn("no controlling terminal", self.refusal(caller, "private"))
        for marker in indexes.CODEX_ENV_MARKERS:
            caller = self.caller(tree, {marker: "1"})
            self.assertIn(marker, self.refusal(caller, "private"))
            self.assertIsNone(self.refusal(caller, "public"))

    def test_interactive_owner_and_recognized_session(self):
        tree = Tree({600: 1})
        self.assertIsNone(self.refusal(self.caller(tree), "private"))
        session = indexes.Caller("session", ("Owner", "owner-id"))
        self.assertIsNone(self.refusal(session, "private"))
        other = indexes.Caller("session", ("Other", "other-id"))
        self.assertIn("protected group", self.refusal(other, "private"))
        self.assertIsNone(self.refusal(other, "public"))

    def test_agent_inside_session_is_delegated_and_cannot_read_protected_group(self):
        (Path(self.paths["sessions"]) / "500.json").write_text(json.dumps(
            {"name": "Owner", "sessionId": "owner-id"}))
        caller = self.caller(Tree({600: 550, 550: 500, 500: 1},
                                  {550: "/opt/bin/codex", 500: "/opt/bin/claude"}),
                             {"CODEX_SANDBOX": "1"}, tty=False)
        self.assertEqual(caller.kind, "delegated")
        self.assertEqual(caller.session, ("Owner", "owner-id"))
        self.assertIn("agent started by session Owner", self.refusal(caller, "private"))
        self.assertIsNone(self.refusal(caller, "public"))
        invited = {**REG, "indexes": {**REG["indexes"],
                   "private": {**REG["indexes"]["private"], "extra_readers": ["owner-id"]}}}
        self.assertIn("agent started by session Owner",
                      indexes.read_refusal(invited, caller, "private", PROTECTED))

    def test_claude_process_with_session_file_keeps_direct_session_rights(self):
        (Path(self.paths["sessions"]) / "500.json").write_text(json.dumps(
            {"name": "Owner", "sessionId": "owner-id"}))
        caller = self.caller(Tree({600: 500, 500: 1}, {500: "/opt/bin/claude"}))
        self.assertEqual(caller, indexes.Caller("session", ("Owner", "owner-id"), None))
        self.assertIsNone(self.refusal(caller, "private"))
        node = Tree({600: 500, 500: 1}, {500: "/usr/bin/node"})
        node.process_args = lambda pid: "node /opt/claude/cli.js" if pid == 500 else None
        self.assertEqual(self.caller(node).kind, "session")

    def test_versioned_claude_and_hosted_claude_never_downgrade(self):
        (Path(self.paths["sessions"]) / "600.json").write_text(json.dumps(
            {"name": "Owner", "sessionId": "owner-id"}))
        for command, args in (("/opt/claude-v2.1.0", None),
                              ("/usr/bin/node", "node claude.js codex agy"),
                              ("/usr/bin/electron", "electron claude --codex")):
            with self.subTest(command=command, args=args):
                tree = Tree({600: 1}, {600: command})
                tree.process_args = lambda pid: args
                caller = self.caller(tree, {"CODEX_THREAD_ID": "x"}, tty=False)
                self.assertEqual(caller.kind, "session")

    def test_node_companion_under_claude_plugins_is_delegated(self):
        (Path(self.paths["sessions"]) / "500.json").write_text(json.dumps(
            {"name": "Owner", "sessionId": "owner-id"}))
        tree = Tree({600: 550, 550: 500, 500: 1},
                    {550: "/usr/bin/node", 500: "/opt/bin/claude"})
        tree.process_args = lambda pid: (
            "node /home/alex/.claude/plugins/codex-companion.mjs task" if pid == 550 else None)
        caller = self.caller(tree)
        self.assertEqual(caller.kind, "delegated")
        self.assertIn("JS agent process", caller.sign)

    def test_agent_ancestor_above_matched_session_is_delegated(self):
        (Path(self.paths["sessions"]) / "550.json").write_text(json.dumps(
            {"name": "Owner", "sessionId": "owner-id"}))
        tree = Tree({600: 550, 550: 500, 500: 1},
                    {550: "/tmp/claude", 500: "/opt/bin/codex"})
        caller = self.caller(tree)
        self.assertEqual(caller.kind, "delegated")
        self.assertEqual(caller.session, ("Owner", "owner-id"))

    def test_planted_session_file_for_shell_pid_does_not_raise_trust(self):
        (Path(self.paths["sessions"]) / "600.json").write_text(json.dumps(
            {"name": "Owner", "sessionId": "owner-id"}))
        tree = Tree({600: 1}, {600: "/bin/bash"})
        tree.process_args = lambda pid: "bash -c 'echo claude'" if pid == 600 else None
        with mock.patch.dict(os.environ, {"FLEETWATCH_SESSIONS_DIR": self.paths["sessions"]}):
            caller = indexes.classify_caller(ppids=tree, start_pid=600, environ={}, stdin=Terminal(False))
        self.assertEqual(caller.kind, "untrusted")
        self.assertIn("not identified", self.refusal(caller, "private"))

    def test_agent_executables_with_spaces_and_node_wrappers(self):
        for command in ("/Library/Application Support/codex", "/Applications/Antigravity.app/antigravity"):
            with self.subTest(command=command):
                caller = self.caller(Tree({600: 500, 500: 1}, {500: command}))
                self.assertEqual(caller.kind, "untrusted")
        for agent in ("codex", "agy", "codex-companion", "agy-companion"):
            with self.subTest(agent=agent):
                tree = Tree({600: 500, 500: 1}, {500: "/usr/bin/node"})
                tree.process_args = lambda pid: "node /opt/bin/%s server" % agent if pid == 500 else None
                self.assertEqual(self.caller(tree).kind, "untrusted")
        for host in ("bun", "deno", "nodejs"):
            tree = Tree({600: 500, 500: 1}, {500: "/usr/bin/" + host})
            tree.process_args = lambda pid: host + " /opt/codex-companion.mjs task"
            self.assertEqual(self.caller(tree).kind, "untrusted")

    def test_search_and_index_cli_refuse_untrusted_read(self):
        caller = indexes.Caller("untrusted", sign="agy process in the ancestry")
        with mock.patch.object(indexes, "classify_caller", return_value=caller), \
                mock.patch.object(indexes, "load_registry", return_value=REG), \
                mock.patch.object(indexes, "locate") as locate, \
                mock.patch.object(indexes, "refresh") as indexer:
            indexer.return_value.SETTINGS.protected_groups = PROTECTED
            indexer.return_value.index_name_problem.return_value = None
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(search_cmd.main(["--index", "private", "query"]), 3)
                self.assertIn("agy process", err.getvalue())
            with self.assertRaises(index_cli.Refused):
                index_cli._guard(REG, "private")
            self.assertFalse(locate.called)

    def test_index_status_hides_untrusted_indexes(self):
        caller = indexes.Caller("untrusted", sign="stdin has no controlling terminal")
        with mock.patch.object(indexes, "classify_caller", return_value=caller), \
                mock.patch.object(indexes, "refresh") as indexer:
            indexer.return_value.SETTINGS.protected_groups = PROTECTED
            indexer.return_value.index_name_problem.return_value = None
            index_cli.cmd_status(REG, [])
            shown = indexer.return_value.cmd_status.call_args.args[0]["indexes"]
            self.assertEqual(set(shown), {"public"})

    def test_direct_refresh_status_hides_protected_indexes(self):
        caller = indexes.Caller("untrusted", sign="stdin has no controlling terminal")
        with mock.patch.object(indexes, "classify_caller", return_value=caller), \
                mock.patch.object(refresh, "load_registry", return_value=REG), \
                mock.patch.object(refresh, "SETTINGS") as settings, \
                mock.patch.object(refresh, "resolve_routing") as resolve, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            settings.protected_groups = PROTECTED
            resolve.return_value = routing.RoutingPlan()
            refresh._main(["status"])
            self.assertIn("== public", output.getvalue())
            self.assertNotIn("private", output.getvalue())
            self.assertIs(resolve.call_args.args[0], REG)

    def test_restricted_status_hides_routing_error_index_name(self):
        visible = dict(REG, indexes={"public": REG["indexes"]["public"]})
        with mock.patch.object(refresh, "resolve_routing", side_effect=ValueError("private missing")), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            refresh.cmd_status(visible, full_reg=REG)
        self.assertIn("agent routing unavailable", output.getvalue())
        self.assertNotIn("private", output.getvalue())

    def test_delegated_caller_cannot_refresh_or_build_protected_index(self):
        callers = (indexes.Caller("delegated", ("Owner", "owner-id"), "codex process"),
                   indexes.Caller("untrusted", sign="agy process in the ancestry"))
        for caller in callers:
            with self.subTest(caller=caller.kind), \
                    mock.patch.object(indexes, "classify_caller", return_value=caller), \
                    mock.patch.object(indexes, "refresh") as indexer:
                indexer.return_value.SETTINGS.protected_groups = PROTECTED
                indexer.return_value.index_name_problem.return_value = None
                with self.assertRaises(index_cli.Refused):
                    index_cli.cmd_refresh(REG, ["private"])
                with self.assertRaises(index_cli.Refused):
                    index_cli.cmd_build(REG, ["private"])
                indexer.return_value.main.assert_not_called()

    def test_direct_refresh_all_refuses_agent_but_allows_scheduler(self):
        with mock.patch.object(refresh, "load_registry", return_value=REG), \
                mock.patch.object(refresh, "SETTINGS") as settings, \
                mock.patch.object(refresh, "resolve_routing", side_effect=RuntimeError("reached routing")):
            settings.protected_groups = PROTECTED
            with mock.patch.object(indexes, "classify_caller", return_value=indexes.Caller(
                    "delegated", ("Owner", "owner-id"), "codex process")):
                with self.assertRaises(SystemExit) as cm:
                    refresh._main([])
                self.assertEqual(cm.exception.code, 3)
            with mock.patch.object(indexes, "classify_caller", return_value=indexes.Caller(
                    "untrusted", sign="stdin has no controlling terminal")):
                with self.assertRaisesRegex(RuntimeError, "reached routing"):
                    refresh._main([])

    def test_forget_routed_run_in_catch_all(self):
        reg = dict(REG, defaults={})
        with mock.patch.object(refresh, "SETTINGS") as settings, \
                mock.patch.object(refresh, "resolve_routing") as resolve, \
                mock.patch.object(refresh, "Index") as index, \
                mock.patch.object(refresh, "Lock") as lock:
            settings.catch_all_index = "private"
            resolve.return_value = routing.RoutingPlan(all_present={"codex": set()})
            lock.return_value.__enter__.return_value = object()
            index.return_value.forget.return_value = {"stored_files": 1}
            got = refresh.forget_session(reg, "private", "run-id")
            self.assertEqual(got["stored_files"], 1)
            index.return_value.forget.assert_called_once_with("run-id")

    def test_index_add_and_force_migrate_refuse_untrusted_and_delegated(self):
        for caller in (indexes.Caller("untrusted", sign="no terminal"),
                       indexes.Caller("delegated", ("Owner", "owner-id"), "codex process")):
            with self.subTest(caller=caller.kind), \
                    mock.patch.object(indexes, "classify_caller", return_value=caller):
                with mock.patch.object(indexes, "refresh", side_effect=AssertionError("unguarded")):
                    with self.assertRaises(index_cli.Refused):
                        index_cli.cmd_add(REG, ["--source", "codex", "--group", "public"])
                with mock.patch.object(index_cli, "_guard", return_value="public"), \
                        mock.patch("fleetwatch.index_migrate.migrate", side_effect=AssertionError("migrated")):
                    with self.assertRaises(index_cli.Refused):
                        index_cli.cmd_migrate(REG, ["public", "--from", "/tmp/old", "--force"])

    def test_direct_refresh_guards_triage_and_forget_but_all_refresh_stays_available(self):
        caller = indexes.Caller("untrusted", sign="stdin has no controlling terminal")
        with mock.patch.object(indexes, "classify_caller", return_value=caller), \
                mock.patch.object(refresh, "SETTINGS") as settings:
            settings.protected_groups = PROTECTED
            self.assertIn("not identified", refresh.owner_refusal(REG, "private"))
            with self.assertRaises(SystemExit) as cm:
                refresh.guard(REG, "private")
            self.assertEqual(cm.exception.code, 3)
            # The scheduled command does not call either guard before its writer.
            with mock.patch.object(index_cli, "_own_or", side_effect=AssertionError("guarded")), \
                    mock.patch.object(refresh, "main") as driver:
                index_cli.cmd_refresh(REG, ["--all"])
            driver.assert_called_once_with([])

    def test_build_writer_can_run_without_a_terminal(self):
        caller = indexes.Caller("untrusted", sign="stdin has no controlling terminal")
        with mock.patch.object(indexes, "classify_caller", return_value=caller), \
                mock.patch.object(indexes, "refresh") as indexer, \
                mock.patch.object(indexes, "locate") as locate, \
                mock.patch.object(indexes, "search_config", return_value={}), \
                mock.patch("fleetwatch.search.engine.Index"), \
                mock.patch("fleetwatch.search.cli.cmd_build") as build:
            indexer.return_value.index_name_problem.return_value = None
            indexer.return_value.SETTINGS.protected_groups = PROTECTED
            self.assertEqual(index_cli.cmd_build(REG, ["private"]), 0)
            self.assertTrue(locate.called)
            self.assertTrue(build.called)


if __name__ == "__main__":
    unittest.main()
