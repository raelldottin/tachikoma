#!/usr/bin/env python3
"""Deterministic unit test coverage for runtime response-shape guards,

SMTP pre-validation, exit semantics aggregation, and nonfatal gameplay execution.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sdk.client import Client, _extract_collection
from sdk.device import Device


class TestExtractCollectionHelper(unittest.TestCase):
    """Test unit behavior of private helper _extract_collection."""

    def test_extract_from_top_level_list(self):
        data = {"RoomDesign": [{"@RoomDesignId": "1"}, {"@RoomDesignId": "2"}]}
        res = _extract_collection(data, "RoomDesign")
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["@RoomDesignId"], "1")

    def test_extract_from_top_level_dict(self):
        data = {"RoomDesign": {"@RoomDesignId": "1"}}
        res = _extract_collection(data, "RoomDesign")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["@RoomDesignId"], "1")

    def test_extract_from_nested_dict(self):
        data = {
            "RoomService": {
                "ListRoomDesigns": {
                    "RoomDesigns": {
                        "RoomDesign": [{"@RoomDesignId": "10"}]
                    }
                }
            }
        }
        res = _extract_collection(data, "RoomDesign")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["@RoomDesignId"], "10")

    def test_extract_missing_or_invalid(self):
        self.assertEqual(_extract_collection(None, "RoomDesign"), [])
        self.assertEqual(_extract_collection({}, "RoomDesign"), [])
        self.assertEqual(_extract_collection("not_a_dict", "RoomDesign"), [])
        self.assertEqual(_extract_collection({"errorMessage": "fail"}, "RoomDesign"), [])


class TestRoomDesignShapeGuards(unittest.TestCase):
    """Test room design response shape handling (R3)."""

    def setUp(self):
        self.device = MagicMock(spec=Device)  # type: ignore
        self.device.languageKey = "en"
        self.client = Client(device=self.device)

    def test_room_designs_missing(self):
        self.client.listRoomDesigns2 = MagicMock(return_value=True)  # type: ignore
        self.client.roomDesigns = None
        with patch("logging.info") as mock_info:
            res = self.client.upgradeRooms()
            self.assertFalse(res)
            mock_info.assert_any_call("Room design data unavailable; skipping room upgrades.")

    def test_room_designs_empty_collection(self):
        self.client.listRoomDesigns2 = MagicMock(return_value=True)  # type: ignore
        self.client.roomDesigns = {"RoomDesign": []}
        with patch("logging.info") as mock_info:
            res = self.client.upgradeRooms()
            self.assertTrue(res)
            mock_info.assert_any_call("Room design data unavailable; skipping room upgrades.")

    def test_room_designs_single_dict(self):
        self.client.listRoomDesigns2 = MagicMock(return_value=True)  # type: ignore
        self.client.roomDesigns = {
            "RoomDesign": {
                "@RoomDesignId": "101",
                "@RoomName": "Engine",
                "@UpgradeFromRoomDesignId": "100",
                "@PriceString": "mineral:100",
            }
        }
        self.client.listUpgradingRooms = MagicMock()  # type: ignore
        self.client.getShipByUserId = MagicMock()  # type: ignore
        self.client.shipByUserId = {
            "ShipService": {
                "GetShipByUserId": {
                    "Ship": {
                        "Rooms": {
                            "Room": [
                                {
                                    "@RoomId": "1",
                                    "@RoomStatus": "Normal",
                                    "@RoomDesignId": "100",
                                }
                            ]
                        }
                    }
                }
            }
        }
        self.client.mineralTotal = 500
        self.client.request = MagicMock(return_value=MagicMock(text="<UpgradeRoom2/>"))  # type: ignore
        self.client.collectAllResources = MagicMock()  # type: ignore

        res = self.client.upgradeRooms()
        self.assertTrue(res)

    def test_room_designs_list_of_dicts(self):
        self.client.listRoomDesigns2 = MagicMock(return_value=True)  # type: ignore
        self.client.roomDesigns = {
            "RoomDesign": [
                {
                    "@RoomDesignId": "101",
                    "@RoomName": "Engine",
                    "@UpgradeFromRoomDesignId": "100",
                    "@PriceString": "mineral:100",
                }
            ]
        }
        self.client.listUpgradingRooms = MagicMock()  # type: ignore
        self.client.getShipByUserId = MagicMock()  # type: ignore
        self.client.shipByUserId = {
            "ShipService": {
                "GetShipByUserId": {
                    "Ship": {
                        "Rooms": {
                            "Room": {
                                "@RoomId": "1",
                                "@RoomStatus": "Normal",
                                "@RoomDesignId": "100",
                            }
                        }
                    }
                }
            }
        }
        self.client.mineralTotal = 500
        self.client.request = MagicMock(return_value=MagicMock(text="<UpgradeRoom2/>"))  # type: ignore
        self.client.collectAllResources = MagicMock()  # type: ignore

        res = self.client.upgradeRooms()
        self.assertTrue(res)

    def test_room_designs_endpoint_error(self):
        self.client.listRoomDesigns2 = MagicMock(return_value=True)  # type: ignore
        self.client.roomDesigns = {"errorMessage": "Server error"}
        with patch("logging.info") as mock_info:
            res = self.client.upgradeRooms()
            self.assertFalse(res)
            mock_info.assert_any_call("Room design data unavailable; skipping room upgrades.")

    def test_room_designs_invalid_schema(self):
        self.client.listRoomDesigns2 = MagicMock(return_value=True)  # type: ignore
        self.client.roomDesigns = "invalid_string_response"
        with patch("logging.info") as mock_info:
            res = self.client.upgradeRooms()
            self.assertFalse(res)
            mock_info.assert_any_call("Room design data unavailable; skipping room upgrades.")

    def test_room_upgrade_exception_message(self):
        self.client.listRoomDesigns2 = MagicMock(side_effect=RuntimeError("unexpected crash"))  # type: ignore
        with patch("logging.exception") as mock_log_exc:
            res = self.client.upgradeRooms()
            self.assertFalse(res)
            mock_log_exc.assert_called_once_with("Unable to upgrade rooms.", exc_info=True)

    def test_list_upgrading_rooms_safe_with_single_dict_or_missing(self):
        self.client.getShipByUserId = MagicMock()  # type: ignore
        self.client.shipByUserId = {
            "ShipService": {
                "GetShipByUserId": {
                    "Ship": {
                        "Rooms": {
                            "Room": {"@RoomId": "1", "@RoomStatus": "Upgrading", "@RoomDesignId": "101"}
                        }
                    }
                }
            }
        }
        self.client.roomDesigns = {"RoomDesign": {"@RoomDesignId": "101", "@RoomName": "Shield"}}
        # Should not raise KeyError or TypeError
        self.client.listUpgradingRooms()


class TestResearchOutcomeClassification(unittest.TestCase):
    """Test research lab upgrade skip vs failure classification (R4)."""

    def setUp(self):
        self.device = MagicMock(spec=Device)  # type: ignore
        self.client = Client(device=self.device)
        self.client.accessToken = "synthetic_token"

    def test_add_research_lab_upgrade_required_logged_as_skip(self):
        mock_resp = MagicMock()  # type: ignore
        mock_resp.text = '<AddResearch errorMessage="Please upgrade your lab room."/>'
        self.client.request = MagicMock(return_value=mock_resp)  # type: ignore

        with patch("logging.info") as mock_info, patch("logging.error") as mock_error:
            res = self.client.addResearch("42")
            self.assertEqual(res, "LAB_UPGRADE_REQUIRED")
            mock_info.assert_called_once_with("Skipped research design 42: lab upgrade required.")
            mock_error.assert_not_called()

    def test_upgrade_researches_lab_upgrade_skip_continues(self):
        self.client.listAllResearches = MagicMock()  # type: ignore
        self.client.listAllResearchDesigns2 = MagicMock()  # type: ignore
        self.client.allResearches = {"Research": []}
        self.client.allResearchDesigns = {
            "ResearchDesign": [
                {
                    "@ResearchDesignId": "101",
                    "@RootResearchDesignId": "100",
                    "@GasCost": "50",
                    "@StarbuxCost": "0",
                    "@ResearchName": "Gas Crafting",
                },
                {
                    "@ResearchDesignId": "102",
                    "@RootResearchDesignId": "200",
                    "@GasCost": "50",
                    "@StarbuxCost": "0",
                    "@ResearchName": "Armor Crafting",
                },
            ]
        }
        self.client.collectAllResources = MagicMock()  # type: ignore
        self.client.gasTotal = 1000

        # First research returns lab upgrade required, second succeeds
        def mock_add_research(design_id):
            if design_id == "101":
                return "LAB_UPGRADE_REQUIRED"
            return True

        self.client.addResearch = MagicMock(side_effect=mock_add_research)  # type: ignore

        res = self.client.upgradeResearches()
        self.assertTrue(res)
        self.assertEqual(self.client.addResearch.call_count, 2)

    def test_add_research_unexpected_endpoint_error(self):
        mock_resp = MagicMock()  # type: ignore
        mock_resp.text = '<AddResearch errorMessage="Database connection failed"/>'
        self.client.request = MagicMock(return_value=mock_resp)  # type: ignore

        res = self.client.addResearch("42")
        self.assertFalse(res)

    def test_upgrade_researches_unexpected_failure_returns_false(self):
        self.client.listAllResearches = MagicMock()  # type: ignore
        self.client.listAllResearchDesigns2 = MagicMock()  # type: ignore
        self.client.allResearches = {"Research": []}
        self.client.allResearchDesigns = {
            "ResearchDesign": {
                "@ResearchDesignId": "101",
                "@RootResearchDesignId": "100",
                "@GasCost": "50",
                "@StarbuxCost": "0",
                "@ResearchName": "Gas Crafting",
            }
        }
        self.client.collectAllResources = MagicMock()  # type: ignore
        self.client.gasTotal = 1000
        self.client.addResearch = MagicMock(return_value=False)  # type: ignore

        res = self.client.upgradeResearches()
        self.assertFalse(res)


class TestTrainingShapeGuards(unittest.TestCase):
    """Test training data shape handling and outcome reporting (R5)."""

    def setUp(self):
        self.device = MagicMock(spec=Device)  # type: ignore
        self.client = Client(device=self.device)
        self.client.listAllCharactersOfUser = MagicMock(return_value=True)  # type: ignore
        self.client.allCharactersOfUser = {"CharacterService": {"ListAllCharactersOfUser": {"Characters": {"Character": []}}}}
        self.client.listAllCharacterDesigns2 = MagicMock(return_value=True)  # type: ignore
        self.client.allCharacterDesigns = {"CharacterService": {"ListAllCharacterDesigns": {"CharacterDesigns": {"CharacterDesign": []}}}}
        self.client.listRoomsViaAccessToken = MagicMock(return_value=True)  # type: ignore
        self.client.roomsViaAccessToken = {"RoomService": {"ListRoomsViaAccessToken": {"Rooms": {"Room": []}}}}
        self.client.listAllTrainingDesigns2 = MagicMock(return_value=True)  # type: ignore

    def test_training_designs_missing(self):
        self.client.trainingDesigns = {}

        with patch("logging.info") as mock_info:
            res = self.client.manageTraining()
            self.assertTrue(res)
            mock_info.assert_any_call("Training design data unavailable; skipping training.")

    def test_training_designs_single_dict(self):
        self.client.trainingDesigns = {
            "TrainingDesign": {
                "@TrainingDesignId": "1",
                "@TrainingName": "Read Expert Weapon Theory",
            }
        }

        res = self.client.manageTraining()
        self.assertTrue(res)

    def test_training_endpoint_error(self):
        self.client.trainingDesigns = {"errorMessage": "Internal server error"}

        with patch("logging.error") as mock_err:
            res = self.client.manageTraining()
            self.assertFalse(res)
            mock_err.assert_called_with("TrainingDesign data not available.")


class TestSMTPPreValidation(unittest.TestCase):
    """Test CLI SMTP pre-validation and exit codes before gameplay (R6)."""

    @patch("run.Client")
    @patch("run.Device")
    def test_smtp_disabled_when_no_flags(self, mock_device, mock_client):
        mock_cli_inst = MagicMock()  # type: ignore
        mock_cli_inst.login.return_value = True
        mock_cli_inst.freeStarbuxToday = 10
        mock_cli_inst.freeStarbuxMax = 10
        mock_cli_inst.upgradeResearches.return_value = True
        mock_cli_inst.upgradeRooms.return_value = True
        mock_cli_inst.manageTraining.return_value = True
        mock_client.return_value = mock_cli_inst

        test_args = ["run.py"]
        with patch.object(sys, "argv", test_args), patch("run.email_logfile") as mock_email:
            with self.assertRaises(SystemExit) as cm:
                from run import main
                main()
            self.assertEqual(cm.exception.code, 0)
            mock_email.assert_not_called()

    @patch("run.Client")
    @patch("run.Device")
    def test_smtp_partial_one_flag_exits_2_before_client_creation(self, mock_device, mock_client):
        partial_args_list = [
            ["run.py", "--smtp-email", "sender@example.com"],
            ["run.py", "--smtp-password-file", "/tmp/nonexistent.pwd"],
            ["run.py", "-r", "recv@example.com"],
        ]
        for test_args in partial_args_list:
            mock_device.reset_mock()
            mock_client.reset_mock()
            with patch.object(sys, "argv", test_args), patch("logging.error") as mock_log_err:
                with self.assertRaises(SystemExit) as cm:
                    from run import main
                    main()
                self.assertEqual(cm.exception.code, 2)
                mock_device.assert_not_called()
                mock_client.assert_not_called()
                mock_log_err.assert_called_with("Incomplete SMTP configuration; email delivery was not attempted.")

    @patch("run.Client")
    @patch("run.Device")
    def test_smtp_partial_two_flags_exits_2_before_client_creation(self, mock_device, mock_client):
        partial_args_list = [
            ["run.py", "--smtp-email", "s@e.com", "--smtp-password-file", "/tmp/pw.txt"],
            ["run.py", "--smtp-email", "s@e.com", "-r", "r@e.com"],
            ["run.py", "--smtp-password-file", "/tmp/pw.txt", "-r", "r@e.com"],
        ]
        for test_args in partial_args_list:
            mock_device.reset_mock()
            mock_client.reset_mock()
            with patch.object(sys, "argv", test_args):
                with self.assertRaises(SystemExit) as cm:
                    from run import main
                    main()
                self.assertEqual(cm.exception.code, 2)
                mock_device.assert_not_called()
                mock_client.assert_not_called()

    @patch("run.Client")
    @patch("run.Device")
    def test_smtp_missing_password_file_exits_2(self, mock_device, mock_client):
        test_args = [
            "run.py",
            "--smtp-email",
            "s@e.com",
            "--smtp-password-file",
            "/path/to/nonexistent/password.file",
            "-r",
            "r@e.com",
        ]
        with patch.object(sys, "argv", test_args):
            with self.assertRaises(SystemExit) as cm:
                from run import main
                main()
            self.assertEqual(cm.exception.code, 2)
            mock_device.assert_not_called()
            mock_client.assert_not_called()

    @patch("run.Client")
    @patch("run.Device")
    def test_smtp_empty_password_file_exits_2(self, mock_device, mock_client, tmp_path_factory=None):
        tmp_pwd_file = Path("tests_scratch_empty_pw.txt")
        tmp_pwd_file.write_text("   \n ")
        try:
            test_args = [
                "run.py",
                "--smtp-email",
                "s@e.com",
                "--smtp-password-file",
                str(tmp_pwd_file),
                "-r",
                "r@e.com",
            ]
            with patch.object(sys, "argv", test_args):
                with self.assertRaises(SystemExit) as cm:
                    from run import main
                    main()
                self.assertEqual(cm.exception.code, 2)
                mock_device.assert_not_called()
                mock_client.assert_not_called()
        finally:
            if tmp_pwd_file.exists():
                tmp_pwd_file.unlink()

    @patch("run.Client")
    @patch("run.Device")
    def test_smtp_valid_configuration_invokes_email_logfile_after_gameplay(self, mock_device, mock_client):
        tmp_pwd_file = Path("tests_scratch_valid_pw.txt")
        tmp_pwd_file.write_text("synthetic_secret_password\n")
        try:
            mock_cli_inst = MagicMock()  # type: ignore
            mock_cli_inst.login.return_value = True
            mock_cli_inst.freeStarbuxToday = 10
            mock_cli_inst.freeStarbuxMax = 10
            mock_cli_inst.upgradeResearches.return_value = True
            mock_cli_inst.upgradeRooms.return_value = True
            mock_cli_inst.manageTraining.return_value = True
            mock_client.return_value = mock_cli_inst

            test_args = [
                "run.py",
                "--smtp-email",
                "s@e.com",
                "--smtp-password-file",
                str(tmp_pwd_file),
                "-r",
                "r@e.com",
            ]
            with patch.object(sys, "argv", test_args), patch("run.email_logfile") as mock_email:
                with self.assertRaises(SystemExit) as cm:
                    from run import main
                    main()
                self.assertEqual(cm.exception.code, 0)
                mock_email.assert_called_once_with("tachikoma.log", mock_cli_inst, "s@e.com", "synthetic_secret_password", "r@e.com")
        finally:
            if tmp_pwd_file.exists():
                tmp_pwd_file.unlink()


class TestExitCodeAggregation(unittest.TestCase):
    """Test truthful exit code aggregation and nonfatal execution sequence (R7)."""

    @patch("run.Client")
    @patch("run.Device")
    def test_exit_0_on_success_and_skips(self, mock_device, mock_client):
        mock_cli_inst = MagicMock()  # type: ignore
        mock_cli_inst.login.return_value = True
        mock_cli_inst.freeStarbuxToday = 10
        mock_cli_inst.freeStarbuxMax = 10
        mock_cli_inst.upgradeResearches.return_value = True
        mock_cli_inst.upgradeRooms.return_value = True
        mock_cli_inst.manageTraining.return_value = True
        mock_client.return_value = mock_cli_inst

        with patch.object(sys, "argv", ["run.py"]):
            with self.assertRaises(SystemExit) as cm:
                from run import main
                main()
            self.assertEqual(cm.exception.code, 0)

    @patch("run.Client")
    @patch("run.Device")
    def test_exit_1_on_unexpected_room_failure_and_continues_training(self, mock_device, mock_client):
        mock_cli_inst = MagicMock()  # type: ignore
        mock_cli_inst.login.return_value = True
        mock_cli_inst.freeStarbuxToday = 10
        mock_cli_inst.freeStarbuxMax = 10
        mock_cli_inst.upgradeResearches.return_value = True
        mock_cli_inst.upgradeRooms.return_value = False  # Unexpected failure
        mock_cli_inst.manageTraining.return_value = True
        mock_client.return_value = mock_cli_inst

        with patch.object(sys, "argv", ["run.py"]):
            with self.assertRaises(SystemExit) as cm:
                from run import main
                main()
            self.assertEqual(cm.exception.code, 1)
            # Verify manageTraining was still called despite upgradeRooms failure
            mock_cli_inst.manageTraining.assert_called_once()

    @patch("run.Client")
    @patch("run.Device")
    def test_exit_1_on_unexpected_training_failure(self, mock_device, mock_client):
        mock_cli_inst = MagicMock()  # type: ignore
        mock_cli_inst.login.return_value = True
        mock_cli_inst.freeStarbuxToday = 10
        mock_cli_inst.freeStarbuxMax = 10
        mock_cli_inst.upgradeResearches.return_value = True
        mock_cli_inst.upgradeRooms.return_value = True
        mock_cli_inst.manageTraining.return_value = False  # Unexpected failure
        mock_client.return_value = mock_cli_inst

        with patch.object(sys, "argv", ["run.py"]):
            with self.assertRaises(SystemExit) as cm:
                from run import main
                main()
            self.assertEqual(cm.exception.code, 1)

    @patch("run.Client")
    @patch("run.Device")
    def test_independent_actions_continue_after_research_failure(self, mock_device, mock_client):
        mock_cli_inst = MagicMock()  # type: ignore
        mock_cli_inst.login.return_value = True
        mock_cli_inst.freeStarbuxToday = 10
        mock_cli_inst.freeStarbuxMax = 10
        mock_cli_inst.upgradeResearches.return_value = False  # Unexpected failure
        mock_cli_inst.upgradeRooms.return_value = True
        mock_cli_inst.manageTraining.return_value = True
        mock_client.return_value = mock_cli_inst

        with patch.object(sys, "argv", ["run.py"]):
            with self.assertRaises(SystemExit) as cm:
                from run import main
                main()
            self.assertEqual(cm.exception.code, 1)
            mock_cli_inst.upgradeRooms.assert_called_once()
            mock_cli_inst.manageTraining.assert_called_once()


if __name__ == "__main__":
    unittest.main()
