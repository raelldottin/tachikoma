"""Tests for run.py --progression-dry-run with a fake client. No network traffic.

Exercises the flags --progression-dry-run, --account-alias and --progression-journal.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sdk.journal import read_events

REPO_ROOT = Path(os.path.abspath(__file__)).parents[1]
INVENTORY = json.loads((REPO_ROOT / "docs/baseline/capability-inventory.json").read_text(encoding="utf-8"))
MUTATING = {entry["name"] for entry in INVENTORY["client_methods"] if entry["kind"] == "mutating"}
SESSION = {entry["name"] for entry in INVENTORY["client_methods"] if entry["kind"] == "session"}


class FakeClient:
    """Logs in and serves read-only payloads; any mutating call fails the test."""

    def __init__(self, *args, **kwargs):
        self.calls = []

    def __getattr__(self, name):
        if name in MUTATING:
            raise AssertionError(f"dry run called mutating Client.{name}")
        raise AttributeError(name)

    def login(self, *args, **kwargs):
        self.calls.append("login")
        return True

    def listRoomDesigns2(self):
        self.calls.append("listRoomDesigns2")
        self.roomDesigns = {"RoomDesigns": {"RoomDesign": [
            {"@RoomDesignId": "100", "@RoomName": "Engine"},
            {"@RoomDesignId": "101", "@RoomName": "Engine II", "@UpgradeFromRoomDesignId": "100",
             "@PriceString": "mineral:300"},
        ]}}

    def listAllResearchDesigns2(self):
        self.calls.append("listAllResearchDesigns2")
        self.allResearchDesigns = {"ResearchDesigns": {"ResearchDesign": [
            {"@ResearchDesignId": "510", "@RootResearchDesignId": "510", "@GasCost": "50"},
        ]}}

    def getShipByUserId(self):
        self.calls.append("getShipByUserId")
        self.shipByUserId = {"Ship": {"@ShipLevel": "5", "Rooms": {"Room": [
            {"@RoomId": "r1", "@RoomDesignId": "100", "@RoomStatus": "Normal"}]}, "Researches": {"Research": {"@ResearchDesignId": "400", "@ResearchState": "Completed"}}}}
        return True

    def listAllCharactersOfUser(self):
        self.calls.append("listAllCharactersOfUser")
        self.allCharactersOfUser = {"Characters": {"Character": [{"@CharacterId": "c1", "@Level": "3"}]}}
        return True

    def listItemsOfAShip(self):
        self.calls.append("listItemsOfAShip")
        self.itemsOfAShip = {"Items": {"Item": [
            {"@ItemDesignId": "1", "@ItemType": "Mineral", "@Quantity": "1000"},
            {"@ItemDesignId": "2", "@ItemType": "Gas", "@Quantity": "1000"},
        ]}}
        return True


class ProgressionDryRunTests(unittest.TestCase):
    def run_main(self, *extra):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        journal = Path(self.tmp.name) / "progression.jsonl"
        client = FakeClient()
        argv = ["run.py", "--progression-dry-run", "--account-alias", "alpha", "--progression-journal", str(journal),
                *extra]
        output = io.StringIO()
        with patch.object(sys, "argv", argv), patch("run.Device"), patch("run.Client", return_value=client), \
                redirect_stdout(output), self.assertRaises(SystemExit) as exit_info:
            from run import main
            main()
        return exit_info.exception.code, client, journal, output.getvalue()

    def test_dry_run_plans_journals_and_exits_without_mutating(self):
        code, client, journal, output = self.run_main()
        self.assertEqual(code, 0)
        non_read = [name for name in client.calls if name in MUTATING or (name in SESSION and name != "login")]
        self.assertEqual(non_read, [])
        events = read_events(journal)
        self.assertEqual([event.action_id for event in events],
                         ["add_research", "upgrade_room", "collect_all_resources", "collect_daily_reward"])
        self.assertTrue(all(event.dry_run and not event.attempted for event in events))
        self.assertTrue(all(event.account_alias == "alpha" for event in events))
        self.assertIn("planned actions: 4", output)
        self.assertIn("add_training: crew training is not modelled by baseline-v1", output)

    def test_daily_automation_does_not_run_after_dry_run(self):
        _, client, _, _ = self.run_main()
        # Stops right after planning: no heartbeat, battle, collection or upgrade loop.
        self.assertEqual(client.calls[0], "login")
        self.assertEqual(set(client.calls[1:]), {
            "listRoomDesigns2", "listAllResearchDesigns2", "getShipByUserId",
            "listAllCharactersOfUser", "listItemsOfAShip",
        })

    def test_invalid_alias_is_refused(self):
        from sdk.state import SnapshotInputError

        with self.assertRaises(SnapshotInputError):
            self.run_main("--account-alias", "Captain Name")

    def test_default_journal_path_is_gitignored(self):
        import run

        self.assertTrue(run.PROGRESSION_JOURNAL_DEFAULT.endswith(".log"))
        self.assertIn("*.log", (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").split())


if __name__ == "__main__":
    unittest.main()
