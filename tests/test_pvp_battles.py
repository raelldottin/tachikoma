import unittest
from unittest.mock import patch, MagicMock
from sdk.client import Client
from sdk.device import Device

class TestPvPBattles(unittest.TestCase):
    def setUp(self):
        self.device = Device(language="en")
        self.device.device_key = "6AD42828-7D06-534D-A461-49658461A614"
        self.client = Client(device=self.device, settings={"allow_email_password_login": True})
        self.client.accessToken = "466f7d82-0bd8-48d1-90f6-2466c3e873b0"
        self.client.user = MagicMock()
        self.client.user.isAuthorized = True
        self.client.info = {"@Name": "TestPlayer", "Hp": "40"}

    @patch('sdk.client.Client.request')
    @patch('time.sleep', return_value=None)
    def test_battle_flow(self, mock_sleep, mock_request):
        create_xml = """<BattleService><CreateBattle ChanceToFindStarBattles="0"><Battle BattleId="4028975" AttackingShipId="6917397" DefendingShipId="5790873" RandomSeed="867" AttackingShipXml="" /></CreateBattle></BattleService>"""
        accept_xml = """<BattleService><AcceptBattle><Battle BattleId="4028975" AttackingShipId="6917397" DefendingShipId="5790873" RandomSeed="867" AttackingShipXml="" /></AcceptBattle></BattleService>"""
        finalise_xml = """<BattleService><FinaliseBattle><Ship ShipId="6917397" ShipDesignId="233" Hp="24.31" ShipStatus="Attacking" /></FinaliseBattle></BattleService>"""

        def mock_request_side_effect(url, method, **kwargs):
            mock_resp = MagicMock()
            mock_resp.text = ""
            if "CreateBattle9" in url:
                mock_resp.content = create_xml.encode('utf-8')
            elif "AcceptBattle5" in url:
                mock_resp.content = accept_xml.encode('utf-8')
            elif "FinaliseBattle15" in url:
                mock_resp.content = finalise_xml.encode('utf-8')
            return mock_resp

        mock_request.side_effect = mock_request_side_effect

        # Test Create
        battle = self.client.createBattle()
        self.assertIsNotNone(battle)
        self.assertEqual(battle["@BattleId"], "4028975")

        # Test Accept
        accepted = self.client.acceptBattle("4028975")
        self.assertTrue(accepted)

        # Test Finalise
        finalised = self.client.finaliseBattle("4028975", 1, 2400, 24.31)
        self.assertTrue(finalised)
