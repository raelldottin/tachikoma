"""Unit test suite covering Slice 3 end-to-end live fixes and exception vectors.

Tests use synthetic fixtures and mocked traffic exclusively.
No real credentials or live Pixel Starships network calls are performed.
"""
import unittest
from unittest.mock import MagicMock, patch
import os

from sdk.client import Client
from sdk.device import Device
import scripts.provision_account_secrets as provision_script
from tests.test_runtime_guards import RUN_MAIN_STEPS, recording_client, run_main


class TestE2ELiveFixes(unittest.TestCase):

    def setUp(self):
        self.device = Device(language="en")
        self.device.key = "00000000-0000-0000-0000-000000000000"
        self.device.refreshToken = "test-refresh-token"
        self.client = Client(device=self.device)
        self.client.accessToken = "test-access-token"
        self.client.info = {"@Name": "TestCaptain", "@Id": "123"}
        self.client.user = MagicMock(id="123")

    # 1. Login & Token Extraction Fixes

    def test_extract_access_token_with_error_code_present(self):
        """_extract_access_token extracts token even if errorCode attribute exists in XML."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '<UserLogin errorCode="400" accessToken="abcd-1234-efgh"/>'
        token = Client._extract_access_token(mock_response)
        self.assertEqual(token, "abcd-1234-efgh")

    def test_parse_user_login_data_root_user_login(self):
        """parseUserLoginData handles root <UserLogin> XML without <UserService> wrapper."""
        xml_content = (
            b'<UserLogin errorCode="400" errorMessage="test@example.com" UserId="9181430">'
            b'<User Id="9181430" Name=".ack" LastHeartBeatDate="2026-08-08T10:05:30" Credits="1000" DailyRewardStatus="0" FreeStarbuxReceivedToday="5"/>'
            b'</UserLogin>'
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = xml_content
        mock_response.text = xml_content.decode("utf-8")

        result = self.client.parseUserLoginData(mock_response)
        self.assertTrue(result)
        self.assertEqual(self.client.info["@Name"], ".ack")
        self.assertEqual(self.client.user.id, "9181430")
        self.assertEqual(self.client.credits, 1000)

    def test_parse_user_login_data_user_service_wrapper(self):
        """parseUserLoginData handles <UserService><UserLogin> root structure."""
        xml_content = (
            b'<UserService><UserLogin UserId="100">'
            b'<User Id="100" Name="CaptainService" LastHeartBeatDate="2026-08-08T10:00:00" Credits="500" DailyRewardStatus="1"/>'
            b'</UserLogin></UserService>'
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = xml_content
        mock_response.text = xml_content.decode("utf-8")

        result = self.client.parseUserLoginData(mock_response)
        self.assertTrue(result)
        self.assertEqual(self.client.info["@Name"], "CaptainService")

    # 2. Resource Collection Fixes

    def test_collect_all_resources_single_item_dict(self):
        """collectAllResources handles Item as a single dict."""
        xml = b'<RoomService><CollectResources><Items><Item Type="Mineral" Quantity="500"/></Items></CollectResources></RoomService>'
        with patch.object(self.client, "request") as mock_req:
            mock_req.return_value.content = xml
            mock_req.return_value.text = xml.decode("utf-8")
            res = self.client.collectAllResources()
            self.assertTrue(res)
            self.assertEqual(self.client.mineralTotal, "500")
            self.assertEqual(self.client.gasTotal, "0")

    def test_collect_all_resources_reversed_order_list(self):
        """collectAllResources identifies Gas and Mineral regardless of list order."""
        xml = (
            b'<RoomService><CollectResources><Items>'
            b'<Item Type="Gas" Quantity="300"/>'
            b'<Item Type="Mineral" Quantity="800"/>'
            b'</Items></CollectResources></RoomService>'
        )
        with patch.object(self.client, "request") as mock_req:
            mock_req.return_value.content = xml
            mock_req.return_value.text = xml.decode("utf-8")
            res = self.client.collectAllResources()
            self.assertTrue(res)
            self.assertEqual(self.client.mineralTotal, "800")
            self.assertEqual(self.client.gasTotal, "300")

    def test_collect_all_resources_empty_items(self):
        """collectAllResources handles empty Items gracefully."""
        xml = b'<RoomService><CollectResources><Items/></CollectResources></RoomService>'
        with patch.object(self.client, "request") as mock_req:
            mock_req.return_value.content = xml
            mock_req.return_value.text = xml.decode("utf-8")
            res = self.client.collectAllResources()
            self.assertTrue(res)
            self.assertEqual(self.client.mineralTotal, "0")
            self.assertEqual(self.client.gasTotal, "0")

    # 3. Message Handling Fixes

    def test_get_messages_malformed_activity_arg(self):
        """getMessages handles messages with missing or non-colon @ActivityArgument without crashing."""
        self.client.listSystemMessagesForUser3 = MagicMock(return_value=True)
        self.client.systemMessagesForUser = {
            "MessageService": {
                "ListSystemMessagesForUser": {
                    "Messages": {
                        "Message": [
                            {"@MessageId": "1", "@Message": "Hello", "@ActivityArgument": "None"},
                            {"@MessageId": "2", "@Message": "Notice", "@ActivityArgument": "nocolon"},
                            {"@MessageId": "3", "@Message": "Collect", "@ActivityArgument": "starbux:10"},
                        ]
                    }
                }
            }
        }
        self.client.actionMessage = MagicMock(return_value=True)
        self.client.collectReward2 = MagicMock(return_value=True)

        res = self.client.getMessages()
        self.assertTrue(res)
        self.client.actionMessage.assert_any_call("1")
        self.client.actionMessage.assert_any_call("2")
        self.client.collectReward2.assert_called_once_with("3")

    # 4. Task Operations Fixes

    def test_collect_task_reward_single_task_dict(self):
        """collectTaskReward handles single Task dictionary and single TaskDesign dictionary."""
        self.client.listTasksOfAUser = MagicMock()
        self.client.listAllTaskDesigns2 = MagicMock()
        self.client.tasksOfAUser = {
            "TaskService": {
                "ListTasksOfAUser": {
                    "Tasks": {
                        "Task": {
                            "@TaskDesignId": "5",
                            "@Collected": "false",
                            "@ProgressValue": "10",
                        }
                    }
                }
            }
        }
        self.client.allTaskDesigns = {
            "TaskService": {
                "ListAllTaskDesigns": {
                    "TaskDesigns": {
                        "TaskDesign": {
                            "@TaskDesignId": "5",
                            "@ObjectiveAmount": "10",
                            "@Name": "Win 10 Battles",
                        }
                    }
                }
            }
        }
        self.client.collectTaskCompletion = MagicMock(return_value=True)

        res = self.client.collectTaskReward()
        self.assertTrue(res)
        self.client.collectTaskCompletion.assert_called_once_with("5")

    # 5. Crew & Character Operations Fixes

    def test_upgrade_characters_single_dict_and_none(self):
        """upgradeCharacters handles single Character dict and single CharacterDesign dict safely."""
        self.client.listAllCharactersOfUser = MagicMock()
        self.client.listItemsOfAShip = MagicMock()
        self.client.listAllCharacterDesigns2 = MagicMock()
        self.client.allCharactersOfUser = {
            "CharacterService": {
                "ListAllCharactersOfUser": {
                    "Characters": {
                        "Character": {
                            "@CharacterId": "10",
                            "@CharacterDesignId": "1",
                            "@RoomId": "100",
                            "@Level": "1",
                            "@Xp": "0",
                            "@CharacterName": "Bob",
                            "@AvailableDate": "2000-01-01T00:00:00",
                        }
                    }
                }
            }
        }
        self.client.allCharacterDesigns = {
            "CharacterService": {
                "ListAllCharacterDesigns": {
                    "CharacterDesigns": {
                        "CharacterDesign": {
                            "@CharacterDesignId": "1",
                            "@Rarity": "Common",
                        }
                    }
                }
            }
        }

        res = self.client.upgradeCharacters()
        self.assertTrue(res)

    # 6. Marketplace Fixes

    def test_list_active_marketplace_messages_empty_and_dict(self):
        """listActiveMarketplaceMessages handles single Message dict and empty response."""
        xml = (
            b'<MessageService><ListActiveMarketplaceMessages>'
            b'<Messages><Message Message="Laser Cannon" ActivityArgument="starbux:50"/></Messages>'
            b'</ListActiveMarketplaceMessages></MessageService>'
        )
        with patch.object(self.client, "request") as mock_req:
            mock_req.return_value.content = xml
            mock_req.return_value.text = xml.decode("utf-8")
            res = self.client.listActiveMarketplaceMessages()
            self.assertTrue(res)

    # 7. Flying Starbux Fixes

    def test_grab_flying_starbux_invalid_xml(self):
        """grabFlyingStarbux handles malformed starbux response without raising exception."""
        self.client.freeStarbuxToday = 0
        self.client.freeStarbuxMax = 10
        self.client.freeStarbuxTodayTimestamp = 0
        self.client.AddStarbux2 = MagicMock()
        self.client.quickReload = MagicMock()
        self.client.starbux = {"UserService": {"AddStarbux": "InvalidNotDict"}}

        res = self.client.grabFlyingStarbux()
        self.assertFalse(res)
        self.client.quickReload.assert_called_once()

    # 8. Request Handling Fixes

    def test_request_handles_storage_full_gracefully(self):
        """request method logs 'storage is full' as a warning instead of an error."""
        xml = b'<CollectReward errorMessage="You cannot collect this reward as your storage is full."/>'
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = xml
        mock_response.text = xml.decode("utf-8")
        
        self.client.session = MagicMock()
        self.client.session.request.return_value = mock_response

        with self.assertLogs(level="WARNING") as logs:
            response = self.client.request("https://api.pixelstarships.com/MessageService/CollectReward2", "POST")

        self.assertIs(response, mock_response)
        self.assertEqual([record.levelname for record in logs.records], ["WARNING"])
        self.assertIn("storage is full", logs.output[0])

    # 9. Provisioning Script Fixes

    def test_provision_zero_accounts_safe_exit(self):
        """provision_account_secrets main exits 0 safely when no accounts are configured."""
        env = {
            f"PSS_ACCOUNT_{i}_{k}": ""
            for i in range(1, 6)
            for k in ("EMAIL", "PASSWORD", "REFRESH_TOKEN")
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as cm:
                provision_script.main()
            self.assertEqual(cm.exception.code, 0)

    def test_provision_partial_accounts_fast_exit_without_pss(self):
        """provision_account_secrets exits fast with code 1 when partial config is present."""
        env = {
            "PSS_ACCOUNT_1_EMAIL": "user@example.com",
            # PSS_ACCOUNT_1_PASSWORD missing
            "PSS_ACCOUNT_1_REFRESH_TOKEN": "token123",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("scripts.provision_account_secrets.provision_account") as mock_prov:
                with self.assertRaises(SystemExit) as cm:
                    provision_script.main()
                self.assertEqual(cm.exception.code, 1)
                mock_prov.assert_not_called()


class TestRunMainStatusAggregation(unittest.TestCase):
    """run.main() decides the exit status from each step's outcome and keeps running the steps after a failure."""

    def test_collect_task_reward_false_is_benign_and_exits_0(self):
        client = recording_client({"collectTaskReward": False})

        with self.assertLogs(level="INFO") as logs:
            code = run_main(client)

        self.assertEqual(code, 0)
        self.assertEqual(client.step_calls, list(RUN_MAIN_STEPS))
        self.assertIn("INFO:root:collectTaskReward returned False (expected if storage full)", logs.output)

    def test_get_crew_info_false_fails_run_but_later_steps_still_run(self):
        client = recording_client({"getCrewInfo": False})

        self.assertEqual(run_main(client), 1)
        self.assertEqual(client.step_calls, list(RUN_MAIN_STEPS))

    def test_research_room_and_daily_reward_failures_fail_run_but_later_steps_still_run(self):
        for step in ("upgradeResearches", "upgradeRooms", "collectDailyReward"):
            with self.subTest(step=step):
                client = recording_client({step: False})

                self.assertEqual(run_main(client), 1)
                self.assertEqual(client.step_calls, list(RUN_MAIN_STEPS))

    def test_manage_training_false_fails_run_and_later_steps_still_run(self):
        client = recording_client({"manageTraining": False})

        self.assertEqual(run_main(client), 1)
        self.assertEqual(client.step_calls, list(RUN_MAIN_STEPS))

    def test_manage_training_exception_is_logged_and_later_steps_still_run(self):
        client = recording_client({"manageTraining": RuntimeError("training exploded")})

        with self.assertLogs(level="ERROR") as logs:
            code = run_main(client)

        self.assertEqual(code, 1)
        self.assertEqual(client.step_calls, list(RUN_MAIN_STEPS))
        self.assertIn("ERROR:root:manageTraining failed: training exploded", logs.output)


if __name__ == "__main__":
    unittest.main()
