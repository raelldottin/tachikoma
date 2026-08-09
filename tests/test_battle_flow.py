#!/usr/bin/env python3
"""Unit test suite for ship battle end-to-end flow.

Tests use synthetic fixtures and mocked traffic exclusively.
No real credentials or live Pixel Starships network calls are performed.
"""
import unittest
from unittest.mock import MagicMock, patch
import xmltodict

from sdk.client import Client
from sdk.device import Device
from sdk.security import checksum_finalise_battle15


class TestBattleFlow(unittest.TestCase):
    """Test the end-to-end battle flow with mocked responses."""

    def setUp(self):
        self.device = Device(language="en")
        self.device.key = "00000000-0000-0000-0000-000000000000"
        self.device.refreshToken = "test-refresh-token"
        self.client = Client(
            device=self.device,
            settings={
                "checksum_key": "5343",
                "savy_checksum": "Savvy!s0d@",
            }
        )
        self.client.accessToken = "test-access-token"
        self.client.info = {"@Name": "TestCaptain", "@Id": "123"}
        self.client.user = MagicMock(id="123")

    # 1. FinaliseBattle15 Checksum Formula Tests

    def test_finalise_battle15_checksum_formula(self):
        """Verify FinaliseBattle15 checksum formula against known synthetic vector."""
        # Test vector from static analysis
        battle_id = "battle123"
        client_outcome_type = 1
        client_end_frame = 100
        client_result_string = "test_result"
        attacking_ship_hp = 50000
        client_version = "0.999.59"
        access_token = "test-access-token"
        checksum_key = "5343"
        savy_checksum = "Savvy!s0d@"

        actual = checksum_finalise_battle15(
            battle_id=battle_id,
            client_outcome_type=client_outcome_type,
            client_end_frame=client_end_frame,
            client_result_string=client_result_string,
            attacking_ship_hp=attacking_ship_hp,
            client_version=client_version,
            access_token=access_token,
            checksum_key=checksum_key,
            savy_checksum=savy_checksum,
        )

        # Verify it produces a valid 32-char hex digest
        self.assertEqual(len(actual), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in actual))

    def test_finalise_battle15_checksum_deterministic(self):
        """Verify checksum is deterministic for same inputs."""
        kwargs = {
            "battle_id": "battle123",
            "client_outcome_type": 1,
            "client_end_frame": 100,
            "client_result_string": "test_result",
            "attacking_ship_hp": 50000,
            "client_version": "0.999.59",
            "access_token": "test-access-token",
            "checksum_key": "5343",
            "savy_checksum": "Savvy!s0d@",
        }

        result1 = checksum_finalise_battle15(**kwargs)
        result2 = checksum_finalise_battle15(**kwargs)
        self.assertEqual(result1, result2)

    def test_finalise_battle15_checksum_different_inputs(self):
        """Verify checksum changes with different inputs."""
        base_kwargs = {
            "battle_id": "battle123",
            "client_outcome_type": 1,
            "client_end_frame": 100,
            "client_result_string": "test_result",
            "attacking_ship_hp": 50000,
            "client_version": "0.999.59",
            "access_token": "test-access-token",
            "checksum_key": "5343",
            "savy_checksum": "Savvy!s0d@",
        }

        result1 = checksum_finalise_battle15(**base_kwargs)
        result2 = checksum_finalise_battle15(**{**base_kwargs, "battle_id": "battle456"})
        self.assertNotEqual(result1, result2)

    # 2. createStarBattle5 Tests

    @patch.object(Client, "request")
    def test_create_star_battle5_success(self, mock_request):
        """createStarBattle5 returns True and stores battleId on success."""
        xml_response = (
            b'<BattleService><CreateStarBattle5 battleId="battle-12345" '
            b'errorCode="0" /></BattleService>'
        )
        mock_response = MagicMock()
        mock_response.content = xml_response
        mock_response.text = xml_response.decode("utf-8")
        mock_request.return_value = mock_response

        result = self.client.createStarBattle5(clientHp=100000)

        self.assertTrue(result)
        self.assertEqual(self.client.lastBattleId, "battle-12345")
        mock_request.assert_called_once()

    @patch.object(Client, "request")
    def test_create_star_battle5_error_message(self, mock_request):
        """createStarBattle5 returns False when errorMessage in response."""
        xml_response = b'<BattleService><CreateStarBattle5 errorMessage="Insufficient HP" /></BattleService>'
        mock_response = MagicMock()
        mock_response.content = xml_response
        mock_response.text = xml_response.decode("utf-8")
        mock_request.return_value = mock_response

        result = self.client.createStarBattle5(clientHp=100000)

        self.assertFalse(result)

    @patch.object(Client, "request")
    def test_create_star_battle5_no_response(self, mock_request):
        """createStarBattle5 returns False when no response."""
        mock_request.return_value = None

        result = self.client.createStarBattle5(clientHp=100000)

        self.assertFalse(result)

    def test_create_star_battle5_missing_config_raises(self):
        """createStarBattle5 raises ConfigurationError when checksum config missing."""
        client = Client(device=self.device, settings={})  # No checksum_key/savy_checksum
        client.accessToken = "test-access-token"

        with self.assertRaises(Exception) as cm:
            client.createStarBattle5(clientHp=100000)

        self.assertIn("checksum_key and savy_checksum", str(cm.exception))

    # 3. verifyBattle2 Tests

    @patch.object(Client, "request")
    def test_verify_battle2_success(self, mock_request):
        """verifyBattle2 returns True on successful verification."""
        xml_response = b'<BattleService><VerifyBattle2 errorCode="0" /></BattleService>'
        mock_response = MagicMock()
        mock_response.content = xml_response
        mock_response.text = xml_response.decode("utf-8")
        mock_request.return_value = mock_response

        result = self.client.verifyBattle2(
            battleId="battle-12345",
            clientOutcomeType=1,
            clientEndFrame=100,
            clientResultString="test_result",
            attackingShipHp=50000,
        )

        self.assertTrue(result)
        mock_request.assert_called_once()

    @patch.object(Client, "request")
    def test_verify_battle2_error_message(self, mock_request):
        """verifyBattle2 returns False when errorMessage in response."""
        xml_response = b'<BattleService><VerifyBattle2 errorMessage="Invalid battle" /></BattleService>'
        mock_response = MagicMock()
        mock_response.content = xml_response
        mock_response.text = xml_response.decode("utf-8")
        mock_request.return_value = mock_response

        result = self.client.verifyBattle2(
            battleId="battle-12345",
            clientOutcomeType=1,
            clientEndFrame=100,
            clientResultString="test_result",
            attackingShipHp=50000,
        )

        self.assertFalse(result)

    def test_verify_battle2_missing_config_raises(self):
        """verifyBattle2 raises ConfigurationError when checksum config missing."""
        client = Client(device=self.device, settings={})
        client.accessToken = "test-access-token"

        with self.assertRaises(Exception) as cm:
            client.verifyBattle2(
                battleId="battle-12345",
                clientOutcomeType=1,
                clientEndFrame=100,
                clientResultString="test_result",
                attackingShipHp=50000,
            )

        self.assertIn("checksum_key and savy_checksum", str(cm.exception))

    # 4. finaliseBattle15 Tests

    @patch.object(Client, "request")
    def test_finalise_battle15_success(self, mock_request):
        """finaliseBattle15 returns True on successful finalisation."""
        xml_response = b'<BattleService><FinaliseBattle15 errorCode="0" /></BattleService>'
        mock_response = MagicMock()
        mock_response.content = xml_response
        mock_response.text = xml_response.decode("utf-8")
        mock_request.return_value = mock_response

        result = self.client.finaliseBattle15(
            battleId="battle-12345",
            clientOutcomeType=1,
            clientEndFrame=100,
            clientResultString="test_result",
            attackingShipHp=50000,
            clientVersion="0.999.59",
        )

        self.assertTrue(result)
        mock_request.assert_called_once()

    @patch.object(Client, "request")
    def test_finalise_battle15_error_message(self, mock_request):
        """finaliseBattle15 returns False when errorMessage in response."""
        xml_response = b'<BattleService><FinaliseBattle15 errorMessage="Battle already finalised" /></BattleService>'
        mock_response = MagicMock()
        mock_response.content = xml_response
        mock_response.text = xml_response.decode("utf-8")
        mock_request.return_value = mock_response

        result = self.client.finaliseBattle15(
            battleId="battle-12345",
            clientOutcomeType=1,
            clientEndFrame=100,
            clientResultString="test_result",
            attackingShipHp=50000,
            clientVersion="0.999.59",
        )

        self.assertFalse(result)

    def test_finalise_battle15_missing_config_raises(self):
        """finaliseBattle15 raises ConfigurationError when checksum config missing."""
        client = Client(device=self.device, settings={})
        client.accessToken = "test-access-token"

        with self.assertRaises(Exception) as cm:
            client.finaliseBattle15(
                battleId="battle-12345",
                clientOutcomeType=1,
                clientEndFrame=100,
                clientResultString="test_result",
                attackingShipHp=50000,
                clientVersion="0.999.59",
            )

        self.assertIn("checksum_key and savy_checksum", str(cm.exception))

    def test_finalise_battle15_url_structure(self):
        """Verify finaliseBattle15 constructs correct URL with all parameters."""
        with patch.object(self.client, "request") as mock_request:
            xml_response = b'<BattleService><FinaliseBattle15 errorCode="0" /></BattleService>'
            mock_response = MagicMock()
            mock_response.content = xml_response
            mock_response.text = xml_response.decode("utf-8")
            mock_request.return_value = mock_response

            self.client.finaliseBattle15(
                battleId="battle-12345",
                clientOutcomeType=1,
                clientEndFrame=100,
                clientResultString="special chars & test",
                attackingShipHp=50000,
                clientVersion="0.999.59",
            )

            # Verify URL was constructed with all parameters
            call_args = mock_request.call_args
            url = call_args[0][0]
            self.assertIn("FinaliseBattle15", url)
            self.assertIn("battleId=battle-12345", url)
            self.assertIn("clientOutcomeType=1", url)
            self.assertIn("clientEndFrame=100", url)
            self.assertIn("attackingShipHp=50000", url)
            self.assertIn("clientVersion=0.999.59", url)
            self.assertIn("accessToken=test-access-token", url)
            self.assertIn("checksum=", url)

    # 5. runBattleEndToEnd Integration Test

    def test_run_battle_end_to_end_flow(self):
        """Test complete end-to-end battle flow with mocked responses."""
        # Mock the three battle steps in sequence
        with patch.object(self.client, "getShipHpFraction", return_value=1.0), \
             patch.object(self.client, "createStarBattle5") as mock_create, \
             patch.object(self.client, "verifyBattle2") as mock_verify, \
             patch.object(self.client, "finaliseBattle15") as mock_finalise:

            # Step 1: createStarBattle5 returns True and sets lastBattleId
            def create_side_effect(clientHp):
                self.client.lastBattleId = "battle-12345"
                return True
            mock_create.side_effect = create_side_effect

            # Step 2: verifyBattle2 returns True
            mock_verify.return_value = True

            # Step 3: finaliseBattle15 returns True
            mock_finalise.return_value = True

            result = self.client.runBattleEndToEnd(clientHp=100000)

            self.assertTrue(result)
            mock_create.assert_called_once_with(clientHp=100000)
            mock_verify.assert_called_once()
            mock_finalise.assert_called_once()

            # Verify verifyBattle2 called with victory parameters
            verify_call = mock_verify.call_args
            self.assertEqual(verify_call.kwargs["clientOutcomeType"], 1)
            self.assertEqual(verify_call.kwargs["clientEndFrame"], 100)

            # Verify finaliseBattle15 called with victory parameters
            finalise_call = mock_finalise.call_args
            self.assertEqual(finalise_call.kwargs["clientOutcomeType"], 1)
            self.assertEqual(finalise_call.kwargs["clientEndFrame"], 100)

    def test_run_battle_end_to_end_create_fails(self):
        """runBattleEndToEnd returns False if createStarBattle5 fails."""
        with patch.object(self.client, "getShipHpFraction", return_value=1.0), \
             patch.object(self.client, "createStarBattle5") as mock_create:
            mock_create.return_value = False

            result = self.client.runBattleEndToEnd(clientHp=100000)

            self.assertFalse(result)
            mock_create.assert_called_once()

    def test_run_battle_end_to_end_no_battle_id(self):
        """runBattleEndToEnd returns False if no battleId returned."""
        with patch.object(self.client, "getShipHpFraction", return_value=1.0), \
             patch.object(self.client, "createStarBattle5") as mock_create:
            mock_create.return_value = True
            # Don't set lastBattleId

            result = self.client.runBattleEndToEnd(clientHp=100000)

            self.assertFalse(result)

    def test_run_battle_end_to_end_verify_fails(self):
        """runBattleEndToEnd returns False if verifyBattle2 fails."""
        with patch.object(self.client, "getShipHpFraction", return_value=1.0), \
             patch.object(self.client, "createStarBattle5") as mock_create, \
             patch.object(self.client, "verifyBattle2") as mock_verify:

            def create_side_effect(clientHp):
                self.client.lastBattleId = "battle-12345"
                return True
            mock_create.side_effect = create_side_effect
            mock_verify.return_value = False

            result = self.client.runBattleEndToEnd(clientHp=100000)

            self.assertFalse(result)
            mock_create.assert_called_once()
            mock_verify.assert_called_once()

    def test_run_battle_end_to_end_finalise_fails(self):
        """runBattleEndToEnd returns False if finaliseBattle15 fails."""
        with patch.object(self.client, "getShipHpFraction", return_value=1.0), \
             patch.object(self.client, "createStarBattle5") as mock_create, \
             patch.object(self.client, "verifyBattle2") as mock_verify, \
             patch.object(self.client, "finaliseBattle15") as mock_finalise:

            def create_side_effect(clientHp):
                self.client.lastBattleId = "battle-12345"
                return True
            mock_create.side_effect = create_side_effect
            mock_verify.return_value = True
            mock_finalise.return_value = False

            result = self.client.runBattleEndToEnd(clientHp=100000)

            self.assertFalse(result)
            mock_create.assert_called_once()
            mock_verify.assert_called_once()
            mock_finalise.assert_called_once()

    # 6. Ship HP Pre-flight Gate Tests

    def test_run_battle_aborts_when_hp_unknown(self):
        """runBattleEndToEnd returns False when ship HP is unavailable (-1)."""
        with patch.object(self.client, "getShipHpFraction", return_value=-1.0), \
             patch.object(self.client, "createStarBattle5") as mock_create:
            result = self.client.runBattleEndToEnd(clientHp=100000)
            self.assertFalse(result)
            mock_create.assert_not_called()

    def test_run_battle_aborts_when_hp_below_full(self):
        """runBattleEndToEnd returns False when ship HP < 100%."""
        with patch.object(self.client, "getShipHpFraction", return_value=0.5), \
             patch.object(self.client, "createStarBattle5") as mock_create:
            result = self.client.runBattleEndToEnd(clientHp=100000)
            self.assertFalse(result)
            mock_create.assert_not_called()

    def test_run_battle_proceeds_when_hp_full(self):
        """runBattleEndToEnd proceeds to createStarBattle5 when HP is 100%."""
        with patch.object(self.client, "getShipHpFraction", return_value=1.0), \
             patch.object(self.client, "createStarBattle5") as mock_create:
            mock_create.return_value = False  # fail fast
            result = self.client.runBattleEndToEnd(clientHp=100000)
            mock_create.assert_called_once()

    def test_get_ship_hp_fraction_from_ship_attrs(self):
        """getShipHpFraction reads @Hp/@MaxHp from ship-level attributes."""
        self.client.shipByUserId = {
            "ShipService": {
                "GetShipByUserId": {
                    "Ship": {
                        "@Hp": "1000",
                        "@MaxHp": "1000",
                        "Rooms": {"Room": []},
                        "Researches": {"Research": []},
                    }
                }
            }
        }
        self.assertEqual(self.client.getShipHpFraction(), 1.0)

    def test_get_ship_hp_fraction_partial(self):
        """getShipHpFraction returns 0.5 when HP is half of max."""
        self.client.shipByUserId = {
            "ShipService": {
                "GetShipByUserId": {
                    "Ship": {
                        "@Hp": "500",
                        "@MaxHp": "1000",
                        "Rooms": {"Room": []},
                        "Researches": {"Research": []},
                    }
                }
            }
        }
        self.assertEqual(self.client.getShipHpFraction(), 0.5)

    def test_get_ship_hp_fraction_fallback_to_rooms(self):
        """getShipHpFraction falls back to summing room HP when ship attrs absent."""
        self.client.shipByUserId = {
            "ShipService": {
                "GetShipByUserId": {
                    "Ship": {
                        "Rooms": {
                            "Room": [
                                {"@Hp": "300", "@MaxHp": "300"},
                                {"@Hp": "700", "@MaxHp": "700"},
                            ]
                        },
                        "Researches": {"Research": []},
                    }
                }
            }
        }
        self.assertEqual(self.client.getShipHpFraction(), 1.0)

    def test_get_ship_hp_fraction_unknown_returns_negative(self):
        """getShipHpFraction returns -1.0 when ship data is unavailable."""
        self.client.shipByUserId = None
        with patch.object(self.client, "getShipByUserId", return_value=False):
            self.assertEqual(self.client.getShipHpFraction(), -1.0)


if __name__ == "__main__":
    unittest.main()