"""Index ownership comes only from what the registry records explicitly, never from files on disk."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fleetwatch import indexes  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"
INTRUDER = "00000000-0000-0000-0000-000000000000"
ADVISOR = "77777777-6666-5555-4444-333333333333"


class ExplicitOwnership(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proj = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.proj)
        open(os.path.join(self.proj, SID + ".jsonl"), "w").close()
        self.reg = {"indexes": {
            "billing-session": {"group": "research", "owner_session": "Billing API (old name)",
                                "owner_session_id": SID,
                                "transcripts": {"project_dir": [self.proj], "sessions": "all"}},
            "docs-session": {"group": "research", "owner_session": "Docs Site",
                             "transcripts": {"project_dir": "/nonexistent", "sessions": [OTHER]}},
            "handbook": {"kind": "plain", "group": "global"},
        }}

    def test_recorded_id_survives_a_rename(self):
        self.assertEqual(indexes.owned_by(self.reg, "Billing API", SID), ["billing-session"])
        self.assertEqual(indexes.own_index(self.reg, ("Billing API", SID)), "billing-session")

    def test_a_planted_transcript_file_grants_nothing(self):
        open(os.path.join(self.proj, INTRUDER + ".jsonl"), "w").close()
        self.assertEqual(indexes.owned_by(self.reg, "Intruder", INTRUDER), [])

    def test_transcript_files_alone_do_not_make_an_owner(self):
        del self.reg["indexes"]["billing-session"]["owner_session_id"]
        self.assertEqual(indexes.owned_by(self.reg, "Billing API", SID), [])

    def test_explicit_sessions_list_counts(self):
        self.assertEqual(indexes.owned_by(self.reg, "Renamed Docs", OTHER), ["docs-session"])

    def test_a_second_session_with_the_owners_name_owns_nothing_that_records_an_id(self):
        self.reg["indexes"]["advising-session"] = {"group": "ferpa", "owner_session": "advising",
                                                   "owner_session_id": SID}
        self.assertEqual(indexes.owned_by(self.reg, "advising", INTRUDER), [])
        self.assertIsNotNone(indexes.read_refusal(self.reg, ("advising", INTRUDER), "advising-session",
                                                  protected=["ferpa"]))
        self.assertIsNone(indexes.read_refusal(self.reg, ("advising", SID), "advising-session",
                                               protected=["ferpa"]))

    def test_a_same_named_session_not_in_the_sessions_list_owns_nothing(self):
        # An entry that lists its sessions names its owners by id; a session that only shares
        # the owner's name is not one of them.
        self.reg["indexes"]["advising-session"] = {"group": "ferpa", "owner_session": "advising",
                                                   "transcripts": {"sessions": [ADVISOR]}}
        self.assertEqual(indexes.owned_by(self.reg, "advising", INTRUDER), [])
        self.assertEqual(indexes.owned_by(self.reg, "advising", None), [])
        self.assertIsNotNone(indexes.read_refusal(self.reg, ("advising", INTRUDER), "advising-session",
                                                  protected=["ferpa"]))
        self.assertEqual(indexes.owned_by(self.reg, "advising", ADVISOR), ["advising-session"])

    def test_name_match_remains_the_fallback(self):
        # Only for an entry that records no owner id and no sessions list.
        self.reg["indexes"]["docs-session"]["transcripts"]["sessions"] = "all"
        self.assertEqual(indexes.owned_by(self.reg, "Docs Site", None), ["docs-session"])

    def test_owning_a_plain_index_does_not_join_its_group(self):
        self.reg["groups"] = {"research": {"readers": ["research"]}, "personal": {"readers": ["personal"]}}
        self.reg["indexes"]["handbook"] = {"kind": "plain", "group": "personal", "owner_session_id": SID}
        self.reg["indexes"]["diary-session"] = {"group": "personal", "owner_session": "Someone Else",
                                                "owner_session_id": OTHER}
        caller = ("Billing API", SID)
        self.assertIsNone(indexes.read_refusal(self.reg, caller, "handbook", protected=()))
        self.assertIsNotNone(indexes.read_refusal(self.reg, caller, "diary-session", protected=()))

    def test_own_index_prefers_the_session_index_over_an_owned_plain_one(self):
        self.reg["indexes"]["handbook"]["owner_session_id"] = SID
        self.assertEqual(sorted(indexes.owned_by(self.reg, "Billing API", SID)),
                         ["billing-session", "handbook"])
        self.assertEqual(indexes.own_index(self.reg, ("Billing API", SID)), "billing-session")


if __name__ == "__main__":
    unittest.main()
