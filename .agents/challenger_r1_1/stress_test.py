#!/usr/bin/env python3
"""Comprehensive empirical stress-testing suite for Tachikoma Gauntlet Slice 2 (runtime-response-shape-guards).

Executed by challenger_r1_1 to stress-test requirements and edge cases:
1. Partial SMTP pre-validation exit code 2 before Device/Client construction.
2. Expected lab upgrade research rejection returns exit code 0 and logs INFO.
3. Unexpected endpoint errors return exit code 1.
4. Nonfatal runtime execution order in run.py.
5. Response shape normalization across all edge case schemas without tracebacks.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure repository root is in sys.path
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sdk.client import Client
from sdk.device import Device
import run


class EmpiricalSMTPValidation(unittest.TestCase):
    """Empirical verification of Requirement 6 (SMTP Pre-Validation)."""

    def setUp(self):
        self.tmp_dir = Path("/tmp/tachikoma_smtp_empirical")
        self.tmp_dir.mkdir(exist_ok=True)

    def tearDown(self):
        for f in self.tmp_dir.glob("*"):
            f.unlink()
        if self.tmp_dir.exists():
            self.tmp_dir.rmdir()

    @patch("run.Client")
    @patch("run.Device")
    @patch("getpass.getpass")
    def test_all_partial_smtp_arg_combinations_exit_2_immediately(self, mock_getpass, mock_device, mock_client):
        """Test all 1-field, 2-field, missing file, empty file, directory, and unreadable file combinations."""
        nonexistent_file = str(self.tmp_dir / "missing.txt")
        
        empty_file = self.tmp_dir / "empty.txt"
        empty_file.write_text("   \n \t ")
        
        dir_file = str(self.tmp_dir)

        partial_cases = [
            # 1 field
            ["run.py", "--smtp-email", "user@test.com"],
            ["run.py", "--smtp-password-file", str(empty_file)],
            ["run.py", "-r", "recv@test.com"],
            # 2 fields
            ["run.py", "--smtp-email", "user@test.com", "--smtp-password-file", str(empty_file)],
            ["run.py", "--smtp-email", "user@test.com", "-r", "recv@test.com"],
            ["run.py", "--smtp-password-file", str(empty_file), "-r", "recv@test.com"],
            # 3 fields but invalid file contents or path
            ["run.py", "--smtp-email", "u@t.com", "--smtp-password-file", nonexistent_file, "-r", "r@t.com"],
            ["run.py", "--smtp-email", "u@t.com", "--smtp-password-file", str(empty_file), "-r", "r@t.com"],
            ["run.py", "--smtp-email", "u@t.com", "--smtp-password-file", dir_file, "-r", "r@t.com"],
        ]

        for args in partial_cases:
            mock_device.reset_mock()
            mock_client.reset_mock()
            mock_getpass.reset_mock()

            with patch.object(sys, "argv", args), patch("logging.error") as mock_log_err:
                with self.assertRaises(SystemExit) as cm:
                    run.main()

                self.assertEqual(cm.exception.code, 2, f"Failed for args: {args}")
                mock_device.assert_not_called()
                mock_client.assert_not_called()
                mock_getpass.assert_not_called()
                mock_log_err.assert_called_with("Incomplete SMTP configuration; email delivery was not attempted.")

    @patch("run.Client")
    @patch("run.Device")
    def test_zero_smtp_args_disables_email_and_runs_gameplay(self, mock_device, mock_client):
        mock_cli = MagicMock()
        mock_cli.login.return_value = True
        mock_cli.freeStarbuxToday = 1
        mock_cli.freeStarbuxMax = 1
        mock_cli.upgradeResearches.return_value = True
        mock_cli.upgradeRooms.return_value = True
        mock_cli.manageTraining.return_value = True
        mock_client.return_value = mock_cli

        with patch.object(sys, "argv", ["run.py"]), patch("run.email_logfile") as mock_email:
            with self.assertRaises(SystemExit) as cm:
                run.main()

            self.assertEqual(cm.exception.code, 0)
            mock_device.assert_called_once()
            mock_client.assert_called_once()
            mock_email.assert_not_called()


class EmpiricalResearchClassification(unittest.TestCase):
    """Empirical verification of Requirement 4 (Research Outcome Classification)."""

    def setUp(self):
        self.device = MagicMock(spec=Device)
        self.client = Client(device=self.device)
        self.client.accessToken = "synth_access_token"
        self.client.info = {"@Name": "TestShip"}

    def test_lab_upgrade_rejection_logs_info_level(self):
        mock_resp = MagicMock()
        mock_resp.text = '<AddResearch errorMessage="Please upgrade your lab room."/>'
        self.client.request = MagicMock(return_value=mock_resp)

        with patch("logging.info") as mock_info, patch("logging.error") as mock_error:
            res = self.client.addResearch("123")
            self.assertEqual(res, "LAB_UPGRADE_REQUIRED")
            mock_info.assert_called_once_with("Skipped research design 123: lab upgrade required.")
            mock_error.assert_not_called()

    def test_upgrade_researches_returns_true_and_exit_0_on_all_lab_skips(self):
        self.client.listAllResearches = MagicMock()
        self.client.listAllResearchDesigns2 = MagicMock()
        self.client.allResearches = {"Research": []}
        self.client.allResearchDesigns = {
            "ResearchDesign": [
                {"@ResearchDesignId": "10", "@RootResearchDesignId": "1", "@GasCost": "50", "@StarbuxCost": "0", "@ResearchName": "R1"},
                {"@ResearchDesignId": "20", "@RootResearchDesignId": "2", "@GasCost": "50", "@StarbuxCost": "0", "@ResearchName": "R2"},
            ]
        }
        self.client.collectAllResources = MagicMock()
        self.client.gasTotal = 1000

        mock_resp = MagicMock()
        mock_resp.text = '<AddResearch errorMessage="Please upgrade your lab room."/>'
        self.client.request = MagicMock(return_value=mock_resp)

        with patch("logging.info") as mock_info, patch("logging.error") as mock_error:
            res = self.client.upgradeResearches()
            self.assertTrue(res)
            mock_error.assert_not_called()
            self.assertEqual(mock_info.call_count, 2)

    def test_unexpected_endpoint_error_during_research_returns_false(self):
        self.client.listAllResearches = MagicMock()
        self.client.listAllResearchDesigns2 = MagicMock()
        self.client.allResearches = {"Research": []}
        self.client.allResearchDesigns = {
            "ResearchDesign": {
                "@ResearchDesignId": "10", "@RootResearchDesignId": "1", "@GasCost": "50", "@StarbuxCost": "0", "@ResearchName": "R1"
            }
        }
        self.client.collectAllResources = MagicMock()
        self.client.gasTotal = 1000

        mock_resp = MagicMock()
        mock_resp.text = '<AddResearch errorMessage="Internal DB Error"/>'
        self.client.request = MagicMock(return_value=mock_resp)

        res = self.client.upgradeResearches()
        self.assertFalse(res)


class EmpiricalResponseShapeGuards(unittest.TestCase):
    """Empirical verification of Requirements 3 & 5 (Response-Shape Guards)."""

    def setUp(self):
        self.device = MagicMock(spec=Device)
        self.client = Client(device=self.device)
        self.client.info = {"@Name": "TestShip"}

    def test_room_designs_variations_no_traceback(self):
        shapes = [
            (None, False),
            ({}, True),
            ("corrupted_string", False),
            ({"errorMessage": "Service Unavailable"}, False),
            ({"RoomDesign": []}, True),
            ({"RoomDesign": {"@RoomDesignId": "101"}}, True),
            ({"RoomDesign": [{"@RoomDesignId": "101"}, {"@RoomDesignId": "102"}]}, True),
        ]

        for shape, expected_status in shapes:
            self.client.listRoomDesigns2 = MagicMock(return_value=True)
            self.client.roomDesigns = shape
            self.client.listUpgradingRooms = MagicMock()
            self.client.getShipByUserId = MagicMock()
            self.client.shipByUserId = {}

            res = self.client.upgradeRooms()
            self.assertEqual(res, expected_status, f"Failed for shape: {shape}")

    def test_room_upgrade_exception_logging_format(self):
        """Verify exception inside upgradeRooms logs 'Unable to upgrade rooms.'"""
        self.client.listRoomDesigns2 = MagicMock(side_effect=RuntimeError("Connection reset"))
        with patch("logging.exception") as mock_exc:
            res = self.client.upgradeRooms()
            self.assertFalse(res)
            mock_exc.assert_called_once_with("Unable to upgrade rooms.", exc_info=True)


if __name__ == "__main__":
    unittest.main()
