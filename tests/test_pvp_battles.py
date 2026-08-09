import unittest
from unittest.mock import patch, MagicMock
from sdk.client import Client
from sdk.device import Device

class TestPvPBattles(unittest.TestCase):
    def setUp(self):
        self.device = Device(language="en")
        self.device.device_key = "6AD42828-7D06-534D-A461-49658461A614"
        self.client = Client(device=self.device, settings={
            "allow_email_password_login": True,
            "checksum_key": "5343",
            "savy_checksum": "Savvy!s0d@"
        })
        self.client.accessToken = "466f7d82-0bd8-48d1-90f6-2466c3e873b0"
        self.client.user = MagicMock()
        self.client.user.isAuthorized = True
        self.client.info = {"@Name": "TestPlayer", "Hp": "40"}

    @patch('sdk.client.Client.request')
    @patch('time.sleep', return_value=None)
    def test_battle_flow(self, mock_sleep, mock_request):
        create_xml = """<BattleService><CreateStarBattle5 ChanceToFindStarBattles="0"><Battle BattleId="4028975" AttackingShipId="6917397" DefendingShipId="5790873" RandomSeed="867" AttackingShipXml="" /></CreateStarBattle5></BattleService>"""
        verify_xml = """<BattleService><VerifyBattle2><Battle BattleId="4028975" AttackingShipId="6917397" DefendingShipId="5790873" RandomSeed="867" AttackingShipXml="" /></VerifyBattle2></BattleService>"""
        finalise_xml = """<BattleService><FinaliseBattle15><Ship ShipId="6917397" ShipDesignId="233" Hp="24.31" ShipStatus="Attacking" /></FinaliseBattle15></BattleService>"""

        def mock_request_side_effect(url, method, **kwargs):
            mock_resp = MagicMock()
            mock_resp.text = ""
            if "CreateStarBattle5" in url:
                mock_resp.content = create_xml.encode('utf-8')
            elif "VerifyBattle2" in url:
                mock_resp.content = verify_xml.encode('utf-8')
            elif "FinaliseBattle15" in url:
                mock_resp.content = finalise_xml.encode('utf-8')
            return mock_resp

        mock_request.side_effect = mock_request_side_effect

        # Test Create
        result = self.client.createStarBattle5(100000)
        self.assertTrue(result)

        # Test Verify
        result = self.client.verifyBattle2("4028975", 1, 2400, "result_string", 100)
        self.assertTrue(result)
        self.assertEqual(self.client.verifyBattle2Result["BattleService"]["VerifyBattle2"]["Battle"]["@BattleId"], "4028975")

        # Test Finalise
        finalised = self.client.finaliseBattle15("4028975", 1, 2400, "result_string", 100, "0.999.59")
        self.assertTrue(finalised)
