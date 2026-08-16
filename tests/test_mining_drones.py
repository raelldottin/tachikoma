"""Tests for the mining drone find-travel-collect flow.

All Pixel Starships network traffic is mocked. No real credentials or live calls.
"""
import datetime
import unittest
from unittest.mock import MagicMock, patch

from sdk.client import Client
from sdk.device import Device


class TestMiningDroneFlow(unittest.TestCase):
    """Tests for findMiningDrones, goToStarSystem, speedUpTravelling,
    waitForArrival, and collectMiningDronesWithTravel."""

    def setUp(self):
        """Create a mocked client with synthetic settings."""
        self.device = Device(language="en")
        self.device.key = "00000000-0000-0000-0000-000000000000"
        self.device.refreshToken = "test-refresh-token"
        self.client = Client(device=self.device)
        self.client.accessToken = "test-access-token"
        self.client.info = {"@Name": "TestCaptain", "@Id": "123"}
        self.client.user = MagicMock(id="123")
        self.client.settings = {
            "checksum_key": "5343",
            "savy_checksum": "Savvy!s0d@",
        }
        self.client.dronesCollected = {}
        self.client.baseUrl = "https://api.pixelstarships.com"

    # ── findMiningDrones ──────────────────────────────────────────────

    def test_find_mining_drones_finds_mining_markers(self):
        """findMiningDrones returns only uncollected Mining markers."""
        xml = (
            b'<GalaxyService><ListStarSystemMarkersAndUserMarkers>'
            b'<StarSystemMarkers>'
            b'<StarSystemMarker StarSystemMarkerId="95140453" StarSystemId="24" '
            b'MarkerType="Mining" IsCollected="false" MissionDesignId="307" '
            b'MissionEventId="1697" RewardString="item:781x1|item:105x1" '
            b'IsRepeatable="true" />'
            b'<StarSystemMarker StarSystemMarkerId="9927041" StarSystemId="39" '
            b'MarkerType="MissionObjective" IsCollected="false" />'
            b'<StarSystemMarker StarSystemMarkerId="95372006" StarSystemId="47" '
            b'MarkerType="NPCShip" IsCollected="false" Title="Void Asteroid" />'
            b'</StarSystemMarkers>'
            b'</ListStarSystemMarkersAndUserMarkers></GalaxyService>'
        )
        self.client.request = MagicMock()
        mock_r = MagicMock()
        mock_r.content = xml
        self.client.request.return_value = mock_r

        # Patch listStarSystemMarkersAndUserMarkers to set the attribute
        import xmltodict

        def fake_list(self_inner):
            self.client.starSystemMarkersAndUserMarkers = xmltodict.parse(
                xml, xml_attribs=True
            )

        with patch.object(Client, "listStarSystemMarkersAndUserMarkers", fake_list):
            drones = self.client.findMiningDrones()

        self.assertEqual(len(drones), 1)
        self.assertEqual(drones[0]["markerId"], "95140453")
        self.assertEqual(drones[0]["starSystemId"], "24")
        self.assertEqual(drones[0]["missionDesignId"], "307")
        self.assertEqual(drones[0]["missionEventId"], "1697")
        self.assertTrue(drones[0]["isRepeatable"])

    def test_find_mining_drones_skips_collected(self):
        """findMiningDrones skips markers with IsCollected=true."""
        xml = (
            b'<GalaxyService><ListStarSystemMarkersAndUserMarkers>'
            b'<StarSystemMarkers>'
            b'<StarSystemMarker StarSystemMarkerId="100" StarSystemId="5" '
            b'MarkerType="Mining" IsCollected="true" />'
            b'<StarSystemMarker StarSystemMarkerId="200" StarSystemId="6" '
            b'MarkerType="Mining" IsCollected="false" />'
            b'</StarSystemMarkers>'
            b'</ListStarSystemMarkersAndUserMarkers></GalaxyService>'
        )
        import xmltodict

        def fake_list(self_inner):
            self.client.starSystemMarkersAndUserMarkers = xmltodict.parse(
                xml, xml_attribs=True
            )

        with patch.object(Client, "listStarSystemMarkersAndUserMarkers", fake_list):
            drones = self.client.findMiningDrones()

        self.assertEqual(len(drones), 1)
        self.assertEqual(drones[0]["markerId"], "200")

    def test_find_mining_drones_empty_when_no_mining(self):
        """findMiningDrones returns empty list when no Mining markers exist."""
        xml = (
            b'<GalaxyService><ListStarSystemMarkersAndUserMarkers>'
            b'<StarSystemMarkers>'
            b'<StarSystemMarker StarSystemMarkerId="1" StarSystemId="1" '
            b'MarkerType="MerchantShip" IsCollected="false" />'
            b'</StarSystemMarkers>'
            b'</ListStarSystemMarkersAndUserMarkers></GalaxyService>'
        )
        import xmltodict

        def fake_list(self_inner):
            self.client.starSystemMarkersAndUserMarkers = xmltodict.parse(
                xml, xml_attribs=True
            )

        with patch.object(Client, "listStarSystemMarkersAndUserMarkers", fake_list):
            drones = self.client.findMiningDrones()

        self.assertEqual(drones, [])

    def test_find_mining_drones_no_access_token(self):
        """findMiningDrones returns empty list when not logged in."""
        self.client.accessToken = None
        drones = self.client.findMiningDrones()
        self.assertEqual(drones, [])

    def test_find_mining_drones_handles_single_marker(self):
        """findMiningDrones handles a single StarSystemMarker (dict not list)."""
        xml = (
            b'<GalaxyService><ListStarSystemMarkersAndUserMarkers>'
            b'<StarSystemMarkers>'
            b'<StarSystemMarker StarSystemMarkerId="500" StarSystemId="7" '
            b'MarkerType="Mining" IsCollected="false" />'
            b'</StarSystemMarkers>'
            b'</ListStarSystemMarkersAndUserMarkers></GalaxyService>'
        )
        import xmltodict

        def fake_list(self_inner):
            self.client.starSystemMarkersAndUserMarkers = xmltodict.parse(
                xml, xml_attribs=True
            )

        with patch.object(Client, "listStarSystemMarkersAndUserMarkers", fake_list):
            drones = self.client.findMiningDrones()

        self.assertEqual(len(drones), 1)
        self.assertEqual(drones[0]["markerId"], "500")

    # ── goToStarSystem ─────────────────────────────────────────────────

    def test_go_to_star_system_returns_arrival_date(self):
        """goToStarSystem parses StarSystemArrivalDate from the response."""
        arrival = "2026-08-16T07:34:26"
        xml = (
            f'<GalaxyService><GoTo><Ship ShipId="6957420" '
            f'StarSystemId="24" FromStarSystemId="42" '
            f'NextStarSystemId="24" StarSystemArrivalDate="{arrival}" />'
            f'</GoTo></GalaxyService>'
        ).encode()

        mock_r = MagicMock()
        mock_r.content = xml
        mock_r.text = xml.decode()
        self.client.request = MagicMock(return_value=mock_r)

        result = self.client.goToStarSystem("24")
        self.assertEqual(result, arrival)

    def test_go_to_star_system_returns_none_on_error(self):
        """goToStarSystem returns None when server returns errorMessage."""
        mock_r = MagicMock()
        mock_r.text = '<GalaxyService><GoTo errorMessage="Failed to authorize access token." /></GalaxyService>'
        self.client.request = MagicMock(return_value=mock_r)

        result = self.client.goToStarSystem("24")
        self.assertIsNone(result)

    def test_go_to_star_system_no_checksum_config(self):
        """goToStarSystem returns None when checksum_key is not configured."""
        self.client.settings = {"checksum_key": "", "savy_checksum": ""}
        result = self.client.goToStarSystem("24")
        self.assertIsNone(result)

    # ── speedUpTravelling ──────────────────────────────────────────────

    def test_speed_up_travelling_success(self):
        """speedUpTravelling returns True when server says Success."""
        mock_r = MagicMock()
        mock_r.text = '<GalaxyService><SpeedUpTravelling>Success</SpeedUpTravelling></GalaxyService>'
        self.client.request = MagicMock(return_value=mock_r)

        result = self.client.speedUpTravelling()
        self.assertTrue(result)

    def test_speed_up_travelling_failure(self):
        """speedUpTravelling returns False on errorMessage."""
        mock_r = MagicMock()
        mock_r.text = '<GalaxyService><SpeedUpTravelling errorMessage="An error occurred." /></GalaxyService>'
        self.client.request = MagicMock(return_value=mock_r)

        result = self.client.speedUpTravelling()
        self.assertFalse(result)

    def test_speed_up_travelling_no_checksum_config(self):
        """speedUpTravelling returns False when checksum_key not configured."""
        self.client.settings = {"checksum_key": "", "savy_checksum": ""}
        result = self.client.speedUpTravelling()
        self.assertFalse(result)

    # ── waitForArrival ─────────────────────────────────────────────────

    def test_wait_for_arrival_already_arrived(self):
        """waitForArrival returns True immediately if arrival time is in the past."""
        past = "2020-01-01T00:00:00"
        result = self.client.waitForArrival(past)
        self.assertTrue(result)

    def test_wait_for_arrival_speedup_under_5min(self):
        """waitForArrival calls speedUpTravelling when travel time < 5 minutes."""
        now = datetime.datetime.now()
        arrival = (now + datetime.timedelta(seconds=60)).isoformat(timespec="seconds")

        with patch.object(Client, "speedUpTravelling", return_value=True) as mock_su:
            result = self.client.waitForArrival(arrival)

        self.assertTrue(result)
        mock_su.assert_called_once()

    def test_wait_for_arrival_timeout(self):
        """waitForArrival returns False after max_wait seconds if not arrived."""
        now = datetime.datetime.now()
        arrival = (now + datetime.timedelta(hours=1)).isoformat(timespec="seconds")

        with patch.object(Client, "speedUpTravelling", return_value=False):
            with patch("time.sleep"):  # prevent real sleeping
                result = self.client.waitForArrival(arrival, max_wait=2, poll_interval=1)

        self.assertFalse(result)

    def test_wait_for_arrival_invalid_date(self):
        """waitForArrival returns False for unparseable arrival_date."""
        result = self.client.waitForArrival("not-a-date")
        self.assertFalse(result)

    def test_wait_for_arrival_empty_date(self):
        """waitForArrival returns False for empty arrival_date."""
        result = self.client.waitForArrival("")
        self.assertFalse(result)

    # ── collectMiningDronesWithTravel ───────────────────────────────────

    def test_collect_no_drones_found(self):
        """collectMiningDronesWithTravel returns 0 when no drones found."""
        with patch.object(Client, "findMiningDrones", return_value=[]):
            result = self.client.collectMiningDronesWithTravel()
        self.assertEqual(result, 0)

    def test_collect_no_access_token(self):
        """collectMiningDronesWithTravel returns 0 when not logged in."""
        self.client.accessToken = None
        result = self.client.collectMiningDronesWithTravel()
        self.assertEqual(result, 0)

    def test_collect_drone_same_star_system(self):
        """collectMiningDronesWithTravel collects without travel when already at the drone's star system."""
        drones = [
            {
                "markerId": "500",
                "starSystemId": "24",
                "missionDesignId": "307",
                "missionEventId": "1697",
                "rewardString": "item:781x1",
                "isRepeatable": True,
            }
        ]
        ship_xml = (
            b'<ShipService><GetShipByUserId><Ship StarSystemId="24" /></GetShipByUserId></ShipService>'
        )
        mock_ship_r = MagicMock()
        mock_ship_r.content = ship_xml

        with patch.object(Client, "findMiningDrones", return_value=drones):
            with patch.object(Client, "getShipByUserId", return_value=True):
                self.client.shipByUserId = __import__("xmltodict").parse(ship_xml, xml_attribs=True)
                with patch.object(Client, "goToStarSystem") as mock_goto:
                    with patch.object(Client, "collectMiningDrone", return_value=True) as mock_collect:
                        result = self.client.collectMiningDronesWithTravel()

        self.assertEqual(result, 1)
        mock_goto.assert_not_called()  # no travel needed
        mock_collect.assert_called_once_with("500")

    def test_collect_drone_with_travel_and_speedup(self):
        """collectMiningDronesWithTravel travels, speeds up, then collects."""
        drones = [
            {
                "markerId": "500",
                "starSystemId": "99",
                "missionDesignId": "307",
                "missionEventId": "1697",
                "rewardString": "item:781x1",
                "isRepeatable": True,
            }
        ]
        ship_xml = (
            b'<ShipService><GetShipByUserId><Ship StarSystemId="24" /></GetShipByUserId></ShipService>'
        )
        self.client.shipByUserId = __import__("xmltodict").parse(ship_xml, xml_attribs=True)

        # Arrival in 30 seconds (under 5 minutes, trigger speedUp)
        now = datetime.datetime.now()
        arrival = (now + datetime.timedelta(seconds=30)).isoformat(timespec="seconds")

        with patch.object(Client, "findMiningDrones", return_value=drones):
            with patch.object(Client, "getShipByUserId"):
                with patch.object(Client, "goToStarSystem", return_value=arrival):
                    with patch.object(Client, "speedUpTravelling", return_value=True):
                        with patch.object(Client, "collectMiningDrone", return_value=True) as mock_collect:
                            result = self.client.collectMiningDronesWithTravel()

        self.assertEqual(result, 1)
        mock_collect.assert_called_once_with("500")

    def test_collect_drone_goto_failure_skips(self):
        """collectMiningDronesWithTravel skips a drone when GoTo fails."""
        drones = [
            {
                "markerId": "500",
                "starSystemId": "99",
                "missionDesignId": "307",
                "missionEventId": "1697",
                "rewardString": "",
                "isRepeatable": False,
            }
        ]
        ship_xml = (
            b'<ShipService><GetShipByUserId><Ship StarSystemId="24" /></GetShipByUserId></ShipService>'
        )
        self.client.shipByUserId = __import__("xmltodict").parse(ship_xml, xml_attribs=True)

        with patch.object(Client, "findMiningDrones", return_value=drones):
            with patch.object(Client, "getShipByUserId"):
                with patch.object(Client, "goToStarSystem", return_value=None):
                    with patch.object(Client, "collectMiningDrone") as mock_collect:
                        result = self.client.collectMiningDronesWithTravel()

        self.assertEqual(result, 0)
        mock_collect.assert_not_called()

    def test_collect_skips_already_collected(self):
        """collectMiningDronesWithTravel skips drones in dronesCollected."""
        drones = [
            {
                "markerId": "500",
                "starSystemId": "24",
                "missionDesignId": "307",
                "missionEventId": "1697",
                "rewardString": "",
                "isRepeatable": False,
            }
        ]
        self.client.dronesCollected = {"500": 1}
        ship_xml = (
            b'<ShipService><GetShipByUserId><Ship StarSystemId="24" /></GetShipByUserId></ShipService>'
        )
        self.client.shipByUserId = __import__("xmltodict").parse(ship_xml, xml_attribs=True)

        with patch.object(Client, "findMiningDrones", return_value=drones):
            with patch.object(Client, "getShipByUserId"):
                with patch.object(Client, "collectMiningDrone") as mock_collect:
                    result = self.client.collectMiningDronesWithTravel()

        self.assertEqual(result, 0)
        mock_collect.assert_not_called()

    def test_collect_multiple_drones(self):
        """collectMiningDronesWithTravel handles multiple drones across star systems."""
        drones = [
            {
                "markerId": "100",
                "starSystemId": "5",
                "missionDesignId": "1",
                "missionEventId": "1",
                "rewardString": "",
                "isRepeatable": False,
            },
            {
                "markerId": "200",
                "starSystemId": "6",
                "missionDesignId": "2",
                "missionEventId": "2",
                "rewardString": "",
                "isRepeatable": False,
            },
        ]
        ship_xml = (
            b'<ShipService><GetShipByUserId><Ship StarSystemId="5" /></GetShipByUserId></ShipService>'
        )
        self.client.shipByUserId = __import__("xmltodict").parse(ship_xml, xml_attribs=True)

        past_arrival = "2020-01-01T00:00:00"  # already arrived

        with patch.object(Client, "findMiningDrones", return_value=drones):
            with patch.object(Client, "getShipByUserId"):
                with patch.object(Client, "goToStarSystem", return_value=past_arrival):
                    with patch.object(Client, "waitForArrival", return_value=True):
                        with patch.object(Client, "collectMiningDrone", return_value=True) as mock_collect:
                            result = self.client.collectMiningDronesWithTravel()

        self.assertEqual(result, 2)
        self.assertEqual(mock_collect.call_count, 2)


if __name__ == "__main__":
    unittest.main()
