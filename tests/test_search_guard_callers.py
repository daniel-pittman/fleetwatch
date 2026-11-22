"""Caller classification and the index-content guard with synthetic process ancestry."""
import io
import tempfile
import unittest
from unittest import mock

from fleetwatch import index_cli, indexes, search_cmd
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
        caller = self.caller(Tree({600: 500, 500: 1}, {500: "codex app-server"}))
        self.assertEqual(caller.kind, "untrusted")
        self.assertIn("codex process", self.refusal(caller, "private"))
        self.assertIn("not identified as a Claude session or the owner",
                      self.refusal(caller, "private"))
        self.assertIsNone(self.refusal(caller, "public"))
        detached = self.caller(Tree({600: 500, 500: 1}, {500: "codex app-server"}),
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

    def test_recognized_session_takes_precedence_over_agent_sign(self):
        import json
        from pathlib import Path
        (Path(self.paths["sessions"]) / "500.json").write_text(json.dumps(
            {"name": "Owner", "sessionId": "owner-id"}))
        caller = self.caller(Tree({600: 500, 500: 1}, {500: "codex app-server"}),
                             {"CODEX_SANDBOX": "1"}, tty=False)
        self.assertEqual(caller, indexes.Caller("session", ("Owner", "owner-id")))
        self.assertIsNone(self.refusal(caller, "private"))

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
            index_cli.cmd_status(REG, [])
            shown = indexer.return_value.cmd_status.call_args.args[0]["indexes"]
            self.assertEqual(set(shown), {"public"})

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
            self.assertEqual(index_cli.cmd_build(REG, ["private"]), 0)
            self.assertTrue(locate.called)
            self.assertTrue(build.called)


if __name__ == "__main__":
    unittest.main()
