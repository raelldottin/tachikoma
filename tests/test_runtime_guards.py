#!/usr/bin/env python3
"""Deterministic unit test coverage for runtime response-shape guards,

SMTP pre-validation, exit semantics aggregation, and nonfatal gameplay execution.
"""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qsl, urlsplit
from xml.sax.saxutils import quoteattr

from sdk.client import ROOM_UPGRADE_BLOCKERS, SHIP_UPGRADE_REQUIRED, Client, _extract_collection
from sdk.device import Device
from tests.synthetic_fixtures import SYNTHETIC_ACCESS_TOKEN

# The client steps run.main() calls on a default run (no --run-battle, SMTP off), in the order it calls them.
RUN_MAIN_STEPS = (
    "heartbeat",
    "purchaseScorchedPodIfAffordable",
    "grabFlyingStarbux",
    "collectTaskReward",
    "getCrewInfo",
    "upgradeResearches",
    "upgradeRooms",
    "collectDailyReward",
    "collectAvailableMarkers",
    "collectMiningDronesWithTravel",
    "listActiveMarketplaceMessages",
    "getMessages",
    "infoBux",
    "manageTraining",
    "analyzeShipLayout",
    "getResourceTotals",
    "upgradeCharacters",
)
# Read from run.main(): the steps whose False return value fails the run (exit 1), and the steps whose exception
# fails it. Any other outcome is logged and the run carries on to exit 0; no outcome stops the later steps.
FATAL_ON_FALSE = frozenset(
    {
        "getCrewInfo",
        "upgradeResearches",
        "upgradeRooms",
        "collectDailyReward",
        "listActiveMarketplaceMessages",
        "getMessages",
        "manageTraining",
        "upgradeCharacters",
    }
)
FATAL_ON_RAISE = FATAL_ON_FALSE | {"purchaseScorchedPodIfAffordable", "collectTaskReward"}


def recording_client(outcomes=None, **attributes):
    """A logged-in fake client whose run.main() steps record their names in client.step_calls.

    outcomes maps a step name to its return value, or to an exception instance the step raises; every other
    step returns True. _automation_done is False so the once-per-account steps run, as on a real first pass.
    The fake is specced on Client, so run.main() calling a method Client lacks raises AttributeError.
    """
    outcomes = outcomes or {}
    client = MagicMock(
        spec=Client, _automation_done=False, freeStarbuxToday=10, freeStarbuxMax=10, info={"@Name": "TestCaptain"}
    )
    client.configure_mock(**attributes)
    client.login.return_value = True
    client.step_calls = []

    def step(name):
        def call(*args, **kwargs):
            client.step_calls.append(name)
            outcome = outcomes.get(name, True)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return MagicMock(side_effect=call)

    for name in RUN_MAIN_STEPS:
        setattr(client, name, step(name))
    return client


def run_main(client, argv=("run.py",)):
    """Run run.main() against client with a mocked Device and return its exit code."""
    import run

    with patch("run.Client", return_value=client), patch("run.Device"), patch.object(sys, "argv", list(argv)):
        try:
            run.main()
        except SystemExit as exit_request:
            return exit_request.code
    raise AssertionError("run.main() returned without calling sys.exit")


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
        upgrading_shield = {
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
        shield_design = {"RoomDesign": {"@RoomDesignId": "101", "@RoomName": "Shield"}}
        self.client.info = {"@Name": "TestCaptain"}
        self.client.getShipByUserId = MagicMock()  # type: ignore

        # A single Room record and a single RoomDesign record (dicts, not lists) are still matched and reported.
        self.client.shipByUserId = upgrading_shield
        self.client.roomDesigns = shield_design
        with self.assertLogs(level="INFO") as logs:
            self.client.listUpgradingRooms()
        self.assertEqual(logs.output, ["INFO:root:[TestCaptain] Shield is currently being upgraded."])

        # Missing ship data, room designs or rooms: nothing is reported and nothing raises.
        missing = {
            "ship": (None, shield_design),
            "room designs": (upgrading_shield, None),
            "rooms": ({"ShipService": {"GetShipByUserId": {"Ship": {}}}}, shield_design),
        }
        for case, (ship, designs) in missing.items():
            with self.subTest(missing=case):
                self.client.shipByUserId = ship
                self.client.roomDesigns = designs
                with self.assertNoLogs(level="INFO"):
                    self.client.listUpgradingRooms()


class TestShipReadEmptyContainers(unittest.TestCase):
    """Regression: a new ship's empty <Researches/> or <Rooms/> crashed getShipByUserId (found in the G1 capture run)."""

    def setUp(self):
        self.device = MagicMock(spec=Device)  # type: ignore
        self.device.languageKey = "en"
        self.client = Client(device=self.device)
        self.client.accessToken = "token"
        self.client.user = MagicMock(id="1")

    def read(self, ship_xml):
        body = ("<ShipService><GetShipByUserId>" + ship_xml + "</GetShipByUserId></ShipService>").encode()
        response = MagicMock(content=body, text=body.decode())
        with patch.object(self.client, "request", return_value=response):
            return self.client.getShipByUserId()

    def test_empty_research_and_rooms_are_empty_lists(self):
        self.assertTrue(self.read('<Ship ShipId="9"><Rooms/><Researches/></Ship>'))
        self.assertEqual(self.client.rooms, [])
        self.assertEqual(self.client.researches, [])

    def test_missing_containers_are_empty_lists(self):
        self.assertTrue(self.read('<Ship ShipId="9"></Ship>'))
        self.assertEqual((self.client.rooms, self.client.researches), ([], []))

    def test_present_records_keep_their_shape(self):
        self.assertTrue(self.read('<Ship ShipId="9"><Rooms><Room RoomId="1"/></Rooms>'
                                  '<Researches><Research ResearchDesignId="5"/><Research ResearchDesignId="6"/>'
                                  '</Researches></Ship>'))
        self.assertEqual(self.client.rooms, {"@RoomId": "1"})  # a single room stays a dict, as before
        self.assertEqual([r["@ResearchDesignId"] for r in self.client.researches], ["5", "6"])


class TestTargetedRoomUpgrade(unittest.TestCase):
    """Regression: upgradeRooms delegates each UpgradeRoom2 request to upgradeRoom unchanged."""

    def setUp(self):
        self.device = MagicMock(spec=Device)  # type: ignore
        self.device.languageKey = "en"
        self.client = Client(device=self.device)
        self.client.accessToken = "00000000-1111-2222-3333-444444444444"
        self.client.info = {"@Name": ""}
        self.client.listRoomDesigns2 = MagicMock(return_value=True)  # type: ignore
        self.client.roomDesigns = {
            "RoomDesign": [
                {"@RoomDesignId": "100", "@RoomName": "Engine"},
                {"@RoomDesignId": "101", "@RoomName": "Engine II", "@UpgradeFromRoomDesignId": "100",
                 "@PriceString": "mineral:100"},
                {"@RoomDesignId": "200", "@RoomName": "Shield"},
                {"@RoomDesignId": "201", "@RoomName": "Shield II", "@UpgradeFromRoomDesignId": "200",
                 "@PriceString": "gas:900"},
            ]
        }
        self.client.listUpgradingRooms = MagicMock()  # type: ignore
        self.client.getShipByUserId = MagicMock()  # type: ignore
        self.client.shipByUserId = {"Ship": {"Rooms": {"Room": [
            {"@RoomId": "1", "@RoomStatus": "Normal", "@RoomDesignId": "100"},
            {"@RoomId": "2", "@RoomStatus": "Normal", "@RoomDesignId": "200"},
            {"@RoomId": "3", "@RoomStatus": "Upgrading", "@RoomDesignId": "100"},
        ]}}}
        self.client.mineralTotal = 500
        self.client.gasTotal = 500
        self.client.collectAllResources = MagicMock()  # type: ignore

    def test_upgrade_room_issues_one_upgraderoom2_request(self):
        self.client.request = MagicMock(return_value="raw")  # type: ignore
        self.assertEqual(self.client.upgradeRoom("7", "71"), "raw")
        url, method = self.client.request.call_args.args
        self.assertEqual(method, "POST")
        self.assertIn("RoomService/UpgradeRoom2?roomId=7&upgradeRoomDesignId=71&", url)

    def test_upgrade_ship_issues_one_upgradeship_request(self):
        self.client.request = MagicMock(return_value="raw")  # type: ignore
        self.assertEqual(self.client.upgradeShip("2"), "raw")
        url, method = self.client.request.call_args.args
        self.assertEqual(method, "POST")
        self.assertIn("ShipService/UpgradeShip?shipDesignId=2&", url)

    def test_upgrade_rooms_selection_is_unchanged(self):
        self.client.request = MagicMock(return_value=MagicMock(text="<UpgradeRoom2/>"))  # type: ignore
        with patch.object(self.client, "upgradeRoom", wraps=self.client.upgradeRoom) as targeted:
            self.assertTrue(self.client.upgradeRooms())
        # Room 2 is unaffordable (gas 900 > 500) and room 3 is already upgrading.
        targeted.assert_called_once_with("1", "101")
        self.assertEqual(self.client.request.call_count, 1)
        self.client.collectAllResources.assert_called_once()

    def test_a_rejected_upgrade_is_not_resent(self):
        # The ship never shows the upgrade (an unrecognised refusal): the loop must still end, asking once per room.
        self.client.gasTotal = 5000
        self.client.request = MagicMock(return_value=MagicMock(text='<UpgradeRoom2 errorMessage="Ship level too low."/>'))  # type: ignore
        with patch.object(self.client, "upgradeRoom", wraps=self.client.upgradeRoom) as targeted:
            self.assertTrue(self.client.upgradeRooms())
        self.assertEqual(sorted(call.args for call in targeted.call_args_list), [("1", "101"), ("2", "201")])
        self.assertEqual(self.client.listUpgradingRooms.call_count, 2)

    def test_a_collection_can_fund_another_upgrade(self):
        self.client.request = MagicMock(return_value=MagicMock(text="<UpgradeRoom2/>"))  # type: ignore
        self.client.collectAllResources = MagicMock(side_effect=lambda: setattr(self.client, "gasTotal", 1000))  # type: ignore
        with patch.object(self.client, "upgradeRoom", wraps=self.client.upgradeRoom) as targeted:
            self.assertTrue(self.client.upgradeRooms())
        self.assertEqual([call.args for call in targeted.call_args_list], [("1", "101"), ("2", "201")])

    def test_upgrade_rooms_stops_at_a_blocker(self):
        for blocker in ROOM_UPGRADE_BLOCKERS:
            with self.subTest(blocker=blocker):
                self.setUp()
                self.client.gasTotal = 5000
                self.client.request = MagicMock(return_value=MagicMock(text=blocker))  # type: ignore
                with patch.object(self.client, "upgradeRoom", wraps=self.client.upgradeRoom) as targeted:
                    self.assertTrue(self.client.upgradeRooms())
                targeted.assert_called_once_with("1", "101")
                self.assertTrue(self.client.max_room_upgrades)
                self.client.collectAllResources.assert_not_called()

    def test_a_ship_upgrade_that_does_not_help_still_ends_the_loop(self):
        # The hull upgrade reports success but the room is still refused: one hull upgrade per run, then stop.
        self.client.request = MagicMock(return_value=MagicMock(text=SHIP_UPGRADE_REQUIRED))  # type: ignore
        self.client._tryUpgradeShip = MagicMock(return_value=True)  # type: ignore
        with patch.object(self.client, "upgradeRoom", wraps=self.client.upgradeRoom) as targeted:
            self.assertTrue(self.client.upgradeRooms())
        self.assertEqual([call.args for call in targeted.call_args_list], [("1", "101"), ("1", "101")])
        self.client._tryUpgradeShip.assert_called_once()
        self.assertTrue(self.client.max_room_upgrades)

    def test_upgrade_rooms_triggers_ship_upgrade_when_minerals_sufficient(self):
        self.client.gasTotal = 5000
        self.client.mineralTotal = 5000  # Enough for ship upgrade
        # Set up room designs with ship upgrade cost
        self.client.roomDesigns = {
            "RoomDesign": [
                {"@RoomDesignId": "100", "@RoomName": "Engine"},
                {"@RoomDesignId": "101", "@RoomName": "Engine II", "@UpgradeFromRoomDesignId": "100",
                 "@PriceString": "mineral:100"},
            ]
        }
        # Set up ship designs with next hull level
        self.client.shipDesigns = {
            "ShipDesign": [
                {"@ShipDesignId": "1", "@ShipLevel": "1", "@MineralCost": "0"},
                {"@ShipDesignId": "2", "@ShipLevel": "2", "@MineralCost": "1000"},
            ]
        }
        # Initial ship data (level 1)
        ship_data_first = {
            "ShipService": {
                "GetShipByUserId": {
                    "Ship": {
                        "@ShipId": "SHIP-A",
                        "@ShipDesignId": "1",
                        "@ShipLevel": "1",
                        "@MineralCost": "0",
                        "Rooms": {"Room": [{"@RoomId": "1", "@RoomDesignId": "100", "@RoomStatus": "Normal"}]}
                    }
                }
            }
        }
        # Ship data after ship upgrade but before room upgrade (room still Normal, ship level 2)
        ship_data_after_ship_upgrade = {
            "ShipService": {
                "GetShipByUserId": {
                    "Ship": {
                        "@ShipId": "SHIP-A",
                        "@ShipDesignId": "2",
                        "@ShipLevel": "2",
                        "@MineralCost": "1000",
                        "Rooms": {"Room": [{"@RoomId": "1", "@RoomDesignId": "100", "@RoomStatus": "Normal"}]}
                    }
                }
            }
        }
        # Ship data after room upgrade (room now Upgrading)
        ship_data_after_room_upgrade = {
            "ShipService": {
                "GetShipByUserId": {
                    "Ship": {
                        "@ShipId": "SHIP-A",
                        "@ShipDesignId": "2",
                        "@ShipLevel": "2",
                        "@MineralCost": "1000",
                        "Rooms": {"Room": [{"@RoomId": "1", "@RoomDesignId": "101", "@RoomStatus": "Upgrading"}]}
                    }
                }
            }
        }
        call_count = 0

        def mock_get_ship():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                self.client.shipByUserId = ship_data_first
            elif call_count == 2:
                self.client.shipByUserId = ship_data_after_ship_upgrade
            else:
                self.client.shipByUserId = ship_data_after_room_upgrade

        self.client.getShipByUserId = mock_get_ship
        self.client.listAllDesigns4 = MagicMock(return_value=True)  # type: ignore
        # Mock _tryUpgradeShip to return True (successful ship upgrade)
        self.client._tryUpgradeShip = MagicMock(return_value=True)  # type: ignore
        # Mock upgradeRoom to return ship upgrade required on first call, success on second
        upgrade_room_mock = MagicMock(side_effect=[
            MagicMock(text="You need to upgrade your ship first."),
            MagicMock(text="<RoomService><UpgradeRoom2><Room @RoomId=\"1\" @RoomDesignId=\"101\" @RoomStatus=\"Upgrading\"/></UpgradeRoom2></RoomService>"),
        ])
        self.client.upgradeRoom = upgrade_room_mock  # type: ignore
        self.assertTrue(self.client.upgradeRooms())
        # upgradeRoom should be called twice: once fails with ship upgrade required, once succeeds after ship upgrade
        self.assertEqual(upgrade_room_mock.call_count, 2)
        upgrade_room_mock.assert_any_call("1", "101")
        self.client._tryUpgradeShip.assert_called_once()


def _ship_reply(design_id="20", level="2", upgrade_design_id="0"):
    """A GetShipByUserId reply in the live shape (see tests/fixtures/live_sanitized/rooms/buy_room2.json)."""
    body = (
        '<ShipService><GetShipByUserId><Ship ShipId="SHIP-A" ShipDesignId="{}" ShipLevel="{}" '
        'UpgradeShipDesignId="{}" UpgradeStartDate="2026-10-19T19:15:13" OriginalRaceId="2">'
        '<Rooms><Room RoomId="1" RoomDesignId="100" RoomStatus="Normal"/></Rooms></Ship>'
        "</GetShipByUserId></ShipService>"
    ).format(design_id, level, upgrade_design_id).encode()
    return MagicMock(content=body, text=body.decode(), status_code=200)


class TestShipUpgrade(unittest.TestCase):
    """Regression: the hull upgrade picks the ship's race, and only a ship read counts it as done."""

    # Two races, one hull design per race per level; the ship is the race-2 level-2 hull (design 20).
    SHIP_DESIGNS = {"ShipDesign": [
        {"@ShipDesignId": "10", "@ShipLevel": "1", "@RaceId": "2", "@MineralCost": "0"},
        {"@ShipDesignId": "20", "@ShipLevel": "2", "@RaceId": "2", "@MineralCost": "1000"},
        {"@ShipDesignId": "31", "@ShipLevel": "3", "@RaceId": "1", "@MineralCost": "4000"},
        {"@ShipDesignId": "32", "@ShipLevel": "3", "@RaceId": "2", "@MineralCost": "5000"},
        {"@ShipDesignId": "41", "@ShipLevel": "4", "@RaceId": "2", "@MineralCost": "9000"},
    ]}

    def setUp(self):
        self.device = MagicMock(spec=Device)  # type: ignore
        self.device.languageKey = "en"
        self.client = Client(device=self.device)
        self.client.accessToken = "00000000-1111-2222-3333-444444444444"
        self.client.user = MagicMock(id="1")
        self.client.info = {"@Name": ""}
        self.client.shipDesigns = {"ShipDesign": [dict(d) for d in self.SHIP_DESIGNS["ShipDesign"]]}
        self.client.listAllDesigns4 = MagicMock(return_value=True)  # type: ignore
        self.client.mineralTotal = 6000
        self.client.shipByUserId = None
        self.sent = []
        self.upgrade_reply = MagicMock(content=b"<ShipService><UpgradeShip/></ShipService>",
                                       text="<ShipService><UpgradeShip/></ShipService>", status_code=200)
        self.ship_after = _ship_reply()

        def fake_request(url, method, data=None):
            self.sent.append((method, url))
            if "GetShipByUserId" in url:
                ship = _ship_reply() if not any("UpgradeShip?" in u for _, u in self.sent) else self.ship_after
                return ship
            if "UpgradeShip?" in url:
                return self.upgrade_reply
            raise AssertionError(f"unexpected request {url.split('?')[0]}")

        self.client.request = MagicMock(side_effect=fake_request)  # type: ignore

    def upgrade_requests(self):
        return [(method, url) for method, url in self.sent if "UpgradeShip?" in url]

    # nextHullDesign

    def test_next_hull_is_the_one_of_the_ships_race(self):
        self.client.getShipByUserId()
        self.assertEqual(self.client.nextHullDesign()["@ShipDesignId"], "32")  # not 31, the first level-3 record

    def test_a_single_design_record_is_read_without_error(self):
        self.client.getShipByUserId()
        self.client.shipDesigns = {"ShipDesign": {"@ShipDesignId": "20", "@ShipLevel": "2", "@RaceId": "2"}}
        self.assertIsNone(self.client.nextHullDesign())  # no level-3 design at all

    def test_ambiguous_next_hull_is_not_picked(self):
        self.client.getShipByUserId()
        self.client.shipDesigns["ShipDesign"].append(
            {"@ShipDesignId": "33", "@ShipLevel": "3", "@RaceId": "2", "@MineralCost": "5000"})
        with self.assertLogs(level="INFO") as logs:
            self.assertIsNone(self.client.nextHullDesign())
        self.assertIn("2 level 3 hull designs for race 2", "\n".join(logs.output))

    def test_unknown_race_is_not_guessed(self):
        self.client.getShipByUserId()
        del self.client.shipDesigns["ShipDesign"][1]["@RaceId"]
        with self.assertLogs(level="INFO") as logs:
            self.assertIsNone(self.client.nextHullDesign())
        self.assertIn("race of the ship's design 20 is unknown", "\n".join(logs.output))

    def test_missing_current_design_is_not_guessed(self):
        self.client.getShipByUserId()
        del self.client.shipDesigns["ShipDesign"][1]
        self.assertIsNone(self.client.nextHullDesign())

    def test_no_ship_no_pick(self):
        self.assertIsNone(self.client.nextHullDesign())

    def test_ship_level_that_disagrees_with_its_design_is_not_guessed(self):
        self.client.shipByUserId = {"ShipService": {"GetShipByUserId": {"Ship": {
            "@ShipDesignId": "20", "@ShipLevel": "3", "@UpgradeShipDesignId": "0"}}}}
        self.assertIsNone(self.client.nextHullDesign())  # not design 41, a two-level jump

    def test_other_ship_types_of_the_same_race_and_level_are_ignored(self):
        self.client.getShipByUserId()
        for design in self.client.shipDesigns["ShipDesign"]:
            design["@ShipType"] = "Player"
        self.client.shipDesigns["ShipDesign"].append(
            {"@ShipDesignId": "90", "@ShipLevel": "3", "@RaceId": "2", "@ShipType": "Ai", "@MineralCost": "0"})
        self.assertEqual(self.client.nextHullDesign()["@ShipDesignId"], "32")

    # upgradeShip

    def test_upgrade_ship_is_a_post_with_design_and_client_time(self):
        self.assertIs(self.client.upgradeShip("32"), self.upgrade_reply)
        (method, url), = self.sent
        self.assertEqual(method, "POST")
        path, _, query = url.partition("?")
        self.assertTrue(path.endswith("/ShipService/UpgradeShip"))
        params = dict(part.split("=", 1) for part in query.split("&"))
        self.assertEqual(params["shipDesignId"], "32")
        self.assertRegex(params["clientDateTime"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertIn("accessToken", params)

    def test_upgrade_ship_keeps_a_sanitized_reply_summary(self):
        body = b'<ShipService><UpgradeShip errorMessage="Not enough minerals."/></ShipService>'
        self.upgrade_reply = MagicMock(content=body, text=body.decode(), status_code=200)
        self.client.upgradeShip("32")
        summary = self.client.upgradeShipReply
        self.assertEqual(summary["http_status"], 200)
        self.assertEqual(summary["elements"][:2], ["ShipService", "UpgradeShip"])
        self.assertEqual(summary["messages"], [{"attribute": "errorMessage", "text": "Not enough minerals."}])
        self.assertNotIn("success_shape", summary)

    # _tryUpgradeShip

    def test_upgrade_started_counts(self):
        self.ship_after = _ship_reply(upgrade_design_id="32")
        self.assertTrue(self.client._tryUpgradeShip())
        self.assertEqual(len(self.upgrade_requests()), 1)
        self.assertIn("shipDesignId=32&", self.upgrade_requests()[0][1])

    def test_instant_upgrade_counts(self):
        self.ship_after = _ship_reply(design_id="32", level="3")
        self.assertTrue(self.client._tryUpgradeShip())

    def test_success_looking_reply_with_unchanged_ship_does_not_count(self):
        # The #275 defect: any reply without errorMessage counted as an upgrade.
        with self.assertLogs(level="WARNING") as logs:
            self.assertFalse(self.client._tryUpgradeShip())
        self.assertEqual(len(self.upgrade_requests()), 1)
        self.assertIn("Ship not upgraded to hull design 32", "\n".join(logs.output))
        self.assertIn("'elements': ['ShipService', 'UpgradeShip']", "\n".join(logs.output))
        self.assertNotIn(self.client.accessToken, "\n".join(logs.output))  # the reply summary, never the URL
        self.assertEqual(self.sent[-1][1].split("?")[0].rsplit("/", 1)[-1], "GetShipByUserId")

    def test_an_upgrade_to_another_design_does_not_count(self):
        self.ship_after = _ship_reply(upgrade_design_id="31")
        self.assertFalse(self.client._tryUpgradeShip())

    def test_unreadable_ship_after_request_does_not_count(self):
        self.ship_after = MagicMock(content=b"<ShipService/>", text="<ShipService/>", status_code=200)
        self.assertFalse(self.client._tryUpgradeShip())
        self.assertEqual(len(self.upgrade_requests()), 1)

    def test_insufficient_minerals_sends_nothing(self):
        self.client.mineralTotal = 4999
        self.assertFalse(self.client._tryUpgradeShip())
        self.assertEqual(self.upgrade_requests(), [])

    def test_wrong_race_cost_is_not_used(self):
        # Enough for race 1's level-3 hull (4000) but not race 2's (5000): nothing is sent.
        self.client.mineralTotal = 4500
        self.assertFalse(self.client._tryUpgradeShip())
        self.assertEqual(self.upgrade_requests(), [])

    def test_ambiguous_or_unknown_race_sends_nothing(self):
        del self.client.shipDesigns["ShipDesign"][1]["@RaceId"]
        self.assertFalse(self.client._tryUpgradeShip())
        self.assertEqual(self.upgrade_requests(), [])

    def test_designs_without_ship_designs_are_reloaded(self):
        self.client.shipDesigns = {"errorMessage": "stale"}
        self.assertFalse(self.client._tryUpgradeShip())
        self.client.listAllDesigns4.assert_called_once()
        self.assertEqual(self.upgrade_requests(), [])

    def test_unparseable_cost_sends_nothing(self):
        for cost in ("-1", "", "5,000"):
            with self.subTest(cost=cost):
                self.sent.clear()
                self.client.shipDesigns["ShipDesign"][3]["@MineralCost"] = cost
                self.assertFalse(self.client._tryUpgradeShip())
                self.assertEqual(self.upgrade_requests(), [])

    def test_hull_upgrade_already_under_way_sends_nothing(self):
        self.client.shipByUserId = {"ShipService": {"GetShipByUserId": {"Ship": {
            "@ShipDesignId": "20", "@ShipLevel": "2", "@UpgradeShipDesignId": "32"}}}}
        self.assertFalse(self.client._tryUpgradeShip())
        self.assertEqual(self.upgrade_requests(), [])

    # upgradeRooms with the real _tryUpgradeShip

    def room_setup(self):
        self.client.roomDesigns = {"RoomDesign": [
            {"@RoomDesignId": "100", "@RoomName": "Engine"},
            {"@RoomDesignId": "101", "@RoomName": "Engine II", "@UpgradeFromRoomDesignId": "100",
             "@PriceString": "mineral:100"},
        ]}
        self.client.collectAllResources = MagicMock()  # type: ignore
        self.client.upgradeRoom = MagicMock(return_value=MagicMock(text=SHIP_UPGRADE_REQUIRED))  # type: ignore

    def test_upgrade_rooms_stops_when_the_hull_upgrade_is_not_confirmed(self):
        self.room_setup()
        self.assertTrue(self.client.upgradeRooms())
        self.assertEqual(len(self.upgrade_requests()), 1)
        self.client.upgradeRoom.assert_called_once_with("1", "101")  # no rescan
        self.assertTrue(self.client.max_room_upgrades)

    def test_upgrade_rooms_rescans_once_after_a_confirmed_hull_upgrade(self):
        self.room_setup()
        self.ship_after = _ship_reply(upgrade_design_id="32")
        self.assertTrue(self.client.upgradeRooms())
        self.assertEqual(len(self.upgrade_requests()), 1)  # one hull attempt per run
        self.assertEqual(self.client.upgradeRoom.call_count, 2)
        self.assertTrue(self.client.max_room_upgrades)


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


# The endpoints manageTraining reads today through Client.getLatestVersion3, listAllCharactersOfUser,
# listAllCharacterDesigns2, listRoomsViaAccessToken, listAllTrainingDesigns2 and listAllResearches, and the two it
# posts to. TestTrainingShapeGuards exercises those methods through these endpoints, never by name.
SETTINGS = "/SettingService/GetLatestVersion3"
CHARACTERS = "/CharacterService/ListAllCharactersOfUser"
CHARACTER_DESIGNS = "/CharacterService/ListAllCharacterDesigns2"
ROOMS = "/RoomService/ListRoomsViaAccessToken"
TRAINING_DESIGNS = "/TrainingService/ListAllTrainingDesigns2"
RESEARCHES = "/ResearchService/ListAllResearches"
RESEARCH_DESIGNS = "/ResearchService/ListAllResearchDesigns2"
FINISH_TRAINING = "/TrainingService/FinishTraining"
ADD_TRAINING = "/TrainingService/AddTraining"


def _elements(tag, records):
    """<tag .../> elements for records whose keys are xmltodict attribute names, such as "@CharacterId"."""
    return "".join(
        f"<{tag} " + " ".join(f"{key[1:]}={quoteattr(value)}" for key, value in record.items()) + "/>"
        for record in records
    )


def _reply(service, method, collection, tag, records):
    """A <service><method><collection>records</collection></method></service> list reply."""
    return f"<{service}><{method}><{collection}>{_elements(tag, records)}</{collection}></{method}></{service}>"


def _error_reply(path):
    """The errorMessage reply a list endpoint sends: its service and method elements carry the message."""
    service, method = path.strip("/").split("/")
    method = "GetLatestSetting" if path == SETTINGS else method.rstrip("0123456789")
    return f'<{service}><{method} errorMessage="Internal server error"/></{service}>'


class TrainingRequestRecorder:
    """Stands in for Client.request: answers each endpoint from replies (path -> XML body) and records every
    request, so a test checks the POSTs that were sent, whatever reads came before them."""

    FINISH_REPLY = (
        '<TrainingService><FinishTraining><Character HpImprovement="1" PilotImprovement="1" RepairImprovement="1" '
        'WeaponImprovement="1" ScienceImprovement="1" EngineImprovement="1" AttackImprovement="1" '
        'AbilityImprovement="1" StaminaImprovement="1" Fatigue="12"/></FinishTraining></TrainingService>'
    )
    FINISH_ERROR_REPLY = '<TrainingService><FinishTraining errorMessage="Training is not finished."/></TrainingService>'
    ADD_REPLY = "<TrainingService><AddTraining/></TrainingService>"

    def __init__(self, replies):
        self.replies = {FINISH_TRAINING: self.FINISH_REPLY, ADD_TRAINING: self.ADD_REPLY, **replies}
        self.calls = []

    def __call__(self, url, method, data=None):
        parts = urlsplit(url)
        self.calls.append((method, parts.path, dict(parse_qsl(parts.query))))
        text = self.replies.get(parts.path, "<Unexpected/>")
        return SimpleNamespace(text=text, content=text.encode("utf-8"), status_code=200)

    def posts(self):
        """Every POST sent, as (path, query), with the clock-dependent trainingStartDate left out."""
        return [
            (path, {key: value for key, value in params.items() if key != "trainingStartDate"})
            for method, path, params in self.calls
            if method == "POST"
        ]


def _crew(character_id, name, room_id, end="", fatigue="10", **improvements):
    """One ListAllCharactersOfUser Character record; improvements are stat names without the Improvement suffix."""
    record = {
        "@CharacterId": character_id,
        "@CharacterName": name,
        "@CharacterDesignId": "1",
        "@RoomId": room_id,
        "@TrainingEndDate": end,
        "@Fatigue": fatigue,
    }
    for stat in ("Hp", "Pilot", "Repair", "Weapon", "Science", "Engine", "Attack", "Ability", "Stamina"):
        record[f"@{stat}Improvement"] = str(improvements.get(stat, 0))
    return record


def _characters_reply(characters):
    return _reply("CharacterService", "ListAllCharactersOfUser", "Characters", "Character", characters)


# Fitness course -> research design id. Per-account ListAllResearches records carry only the design id and state;
# the course name ("Fitness 203") lives on the ListAllResearchDesigns2 design, so the client joins the two.
FITNESS_DESIGNS = {"101": "7", "201": "8", "202": "9", "203": "10"}


def _research_reply(*completed_courses):
    """ListAllResearches with the given Fitness courses completed; no courses means no training research."""
    research = [
        {"@ResearchId": f"70{index}", "@ResearchDesignId": FITNESS_DESIGNS[course], "@ResearchState": "Completed"}
        for index, course in enumerate(completed_courses)
    ]
    return _reply("ResearchService", "ListAllResearches", "Researches", "Research", research)


def _research_designs_reply():
    designs = [
        {"@ResearchDesignId": design_id, "@ResearchName": f"Fitness {course}", "@RootResearchDesignId": "7"}
        for course, design_id in FITNESS_DESIGNS.items()
    ]
    return _reply("ResearchService", "ListAllResearchDesigns", "ResearchDesigns", "ResearchDesign", designs)


def _training_designs_reply(designs):
    return _reply("TrainingService", "ListAllTrainingDesigns", "TrainingDesigns", "TrainingDesign", designs)


class TestTrainingShapeGuards(unittest.TestCase):
    """CHARACTERIZATION of Client.manageTraining: pins the POSTs it sends today (FinishTraining then AddTraining
    per crew member, or none) and its return value, for game data served through Client.request.

    TrainingRequestRecorder is the only seam: it answers the list endpoints with XML built from ROSTER and the
    tests assert only on POSTs, so Wave 2 may change how and when the data is read. Wave 2 rewrites
    manageTraining and may deliberately update the expected POSTs and the KNOWN DEFECT cases below.

    roomDesigns is pre-seeded as run.main() leaves it: upgradeRooms, an earlier step, loads it through
    RoomService/ListRoomDesigns2. manageTraining's own fallback is the deprecated DesignService/ListAllDesigns4,
    which needs 30+ design sections and also replaces trainingDesigns, so these tests do not exercise it.

    Every crew member has a training capacity of 100, so a stat total is also the training percentage.
    """

    ROSTER = (
        # Weapons role in its primary room at 40%: green primary training, gated on Fitness/Education 101.
        _crew("101", "Delish", "500", Weapon=40),
        # Engines role in its secondary room at 72%, cooldown long over: green secondary training, no research gate.
        _crew("102", "Galactic Sprite", "500", end="2020-01-01T00:00:00", Engine=72),
        # Weapons role still training: no request.
        _crew("103", "Galactic Succubus", "500", end="2999-01-01T00:00:00", Weapon=40),
        # Not in a training room: skipped.
        _crew("104", "Mistycball", "600", Science=40),
        # In a training room but in no role: no request.
        _crew("105", "Random Recruit", "700", Hp=40),
        # Weapons role past 71% in its primary room: told to move to the gym, no request.
        _crew("106", "Galactic Snow Maiden", "500", Weapon=80),
        # Engines role in its secondary room at 72% but still training: the secondary T1 cooldown holds, no request.
        _crew("107", "The Conjoint Archon", "500", end="2999-01-01T00:00:00", Engine=72),
        # Shields role past 71% but not in a training room: skipped, so neither told to move nor sent a request.
        _crew("108", "C.P.U.", "600", Science=80),
    )
    TRAINING = (
        {"@TrainingDesignId": "11", "@TrainingName": "Read Expert Weapon Theory"},
        {"@TrainingDesignId": "12", "@TrainingName": "Weapons Summit"},
        {"@TrainingDesignId": "21", "@TrainingName": "Study Expert Engineering Manual"},
        {"@TrainingDesignId": "31", "@TrainingName": "Bench Press"},
    )

    def setUp(self):
        self.recorder = TrainingRequestRecorder(
            {
                SETTINGS: (
                    '<SettingService><GetLatestSetting><Setting CharacterDesignVersion="7" '
                    'TrainingDesignVersion="3" ResearchDesignVersion="5"/></GetLatestSetting></SettingService>'
                ),
                CHARACTERS: _characters_reply(self.ROSTER),
                CHARACTER_DESIGNS: _reply(
                    "CharacterService",
                    "ListAllCharacterDesigns",
                    "CharacterDesigns",
                    "CharacterDesign",
                    [
                        {"@CharacterDesignId": "1", "@TrainingCapacity": "100", "@SpecialAbilityType": "DamageToCurrentEnemy"},
                        {"@CharacterDesignId": "2", "@TrainingCapacity": "200", "@SpecialAbilityType": "HealRoomHp"},
                    ],
                ),
                ROOMS: _reply(
                    "RoomService",
                    "ListRoomsViaAccessToken",
                    "Rooms",
                    "Room",
                    [
                        {"@RoomId": "500", "@RoomDesignId": "50"},
                        {"@RoomId": "600", "@RoomDesignId": "60"},
                        {"@RoomId": "700", "@RoomDesignId": "70"},
                    ],
                ),
                TRAINING_DESIGNS: _training_designs_reply(self.TRAINING),
                RESEARCHES: _research_reply("203"),
                RESEARCH_DESIGNS: _research_designs_reply(),
            }
        )
        self.client = self.new_client()

    def new_client(self):
        device = MagicMock(spec=Device)
        device.languageKey = "en"
        client = Client(device=device)
        client.accessToken = SYNTHETIC_ACCESS_TOKEN
        client.info = {"@Name": "TestCaptain"}
        client.roomDesigns = {
            "RoomService": {
                "ListRoomDesigns": {
                    "RoomDesigns": {
                        "RoomDesign": [
                            {"@RoomDesignId": "50", "@RoomName": "Academy"},
                            {"@RoomDesignId": "60", "@RoomName": "Bridge"},
                            {"@RoomDesignId": "70", "@RoomName": "GYM"},
                        ]
                    }
                }
            }
        }
        client.request = self.recorder
        return client

    def training_posts(self, character_id, training_design_id):
        token = SYNTHETIC_ACCESS_TOKEN
        return [
            (FINISH_TRAINING, {"characterId": character_id, "accessToken": token}),
            (ADD_TRAINING, {"trainingDesignId": training_design_id, "characterId": character_id, "accessToken": token}),
        ]

    def test_eligible_crew_get_finish_then_add_training_posts(self):
        self.assertIs(self.client.manageTraining(), True)

        self.assertEqual(self.recorder.posts(), [*self.training_posts("101", "11"), *self.training_posts("102", "21")])
        start_dates = [params["trainingStartDate"] for _, path, params in self.recorder.calls if path == ADD_TRAINING]
        self.assertEqual(len(start_dates), 2)
        for start_date in start_dates:
            self.assertRegex(start_date, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")

    def test_without_training_research_only_ungated_secondary_training_is_sent(self):
        self.recorder.replies[RESEARCHES] = _research_reply()

        self.assertIs(self.client.manageTraining(), True)

        self.assertEqual(self.recorder.posts(), self.training_posts("102", "21"))

    def test_over_trained_crew_in_primary_room_is_told_to_move(self):
        with self.assertLogs(level="ERROR") as logs:
            self.assertIs(self.client.manageTraining(), True)

        move_messages = [message for message in logs.output if "Move Galactic Snow Maiden" in message]
        self.assertEqual(len(move_messages), 1)
        self.assertIn("to the GYM or Galaxy Gym", move_messages[0])
        self.assertNotIn("106", [params.get("characterId") for _, params in self.recorder.posts()])

    def test_secondary_room_crew_still_training_get_no_request(self):
        self.assertIs(self.client.manageTraining(), True)

        posted = [params.get("characterId") for _, params in self.recorder.posts()]
        self.assertNotIn("107", posted)
        self.assertEqual(posted, ["101", "101", "102", "102"])

    def test_crew_outside_training_rooms_are_neither_moved_nor_trained(self):
        with self.assertLogs(level="ERROR") as logs:
            self.assertIs(self.client.manageTraining(), True)

        move_messages = [message for message in logs.output if "] Move " in message]
        self.assertEqual(len(move_messages), 1)
        self.assertIn("Move Galactic Snow Maiden", move_messages[0])
        for name in ("Mistycball", "C.P.U."):
            self.assertFalse([message for message in logs.output if name in message], name)
        posted = [params.get("characterId") for _, params in self.recorder.posts()]
        self.assertNotIn("104", posted)
        self.assertNotIn("108", posted)

    def test_training_designs_missing_skips_without_posting(self):
        self.recorder.replies[TRAINING_DESIGNS] = _training_designs_reply([])

        with self.assertLogs(level="INFO") as logs:
            self.assertIs(self.client.manageTraining(), True)

        self.assertIn("INFO:root:Training design data unavailable; skipping training.", logs.output)
        self.assertEqual(self.recorder.posts(), [])

    def test_single_character_and_training_design_records_are_used(self):
        # One record parses to a dict rather than a list of dicts.
        self.recorder.replies[CHARACTERS] = _characters_reply([self.ROSTER[0]])
        self.recorder.replies[TRAINING_DESIGNS] = _training_designs_reply([self.TRAINING[0]])

        self.assertIs(self.client.manageTraining(), True)

        self.assertEqual(self.recorder.posts(), self.training_posts("101", "11"))

    def test_unreadable_training_data_returns_false_without_posting(self):
        # A reply without the endpoint's service element, such as a gateway error page.
        foreign = '<ServerError errorMessage="Service unavailable"/>'
        # The pinned "avaialble" typos come from Client; Wave 2 may fix them.
        cases = (
            (TRAINING_DESIGNS, _error_reply(TRAINING_DESIGNS), ["ERROR:root:TrainingDesign data not available."]),
            (SETTINGS, _error_reply(SETTINGS), ["ERROR:root:AllCharacterDesigns data not avaialble."]),
            (
                CHARACTERS,
                foreign,
                [
                    "ERROR:root:Failed to get list of characters on the ship.",
                    "ERROR:root:allCharactersOfUser data not avaialble.",
                ],
            ),
            (ROOMS, foreign, ["ERROR:root:RoomService data not available."]),
        )
        for path, reply, expected in cases:
            with self.subTest(error_at=path):
                self.setUp()
                self.recorder.replies[path] = reply

                with self.assertLogs(level="ERROR") as logs:
                    self.assertIs(self.client.manageTraining(), False)

                self.assertEqual(logs.output, expected)
                self.assertEqual(self.recorder.posts(), [])

    def test_character_list_error_is_not_read_as_an_empty_roster(self):
        # Regression (fixed in #279): the errorMessage reply still carries <CharacterService>; it used to be accepted
        # as a roster with no crew, so the run counted training as done. listAllCharactersOfUser now refuses it.
        self.recorder.replies[CHARACTERS] = _error_reply(CHARACTERS)

        self.assertIs(self.client.manageTraining(), False)

        self.assertEqual(self.recorder.posts(), [])

    def test_character_design_list_error_returns_false_before_any_post(self):
        # Regression (fixed in #276): listAllCharacterDesigns2 goes through _fetchDesignList, so an errorMessage reply
        # is refused instead of being read as data and raising on the missing records.
        self.recorder.replies[CHARACTER_DESIGNS] = _error_reply(CHARACTER_DESIGNS)

        with self.assertLogs(level="ERROR") as logs:
            self.assertIs(self.client.manageTraining(), False)

        self.assertEqual(logs.output, ["ERROR:root:AllCharacterDesigns data not avaialble."])
        self.assertEqual(self.recorder.posts(), [])

    def test_room_list_error_raises_before_any_post(self):
        # KNOWN DEFECT: the errorMessage reply is accepted as data, then reading the missing records raises
        # (run.main() logs "manageTraining failed" and exits 1). Wave 2 may return False here instead.
        self.recorder.replies[ROOMS] = _error_reply(ROOMS)

        with self.assertRaises((KeyError, AttributeError)):
            self.client.manageTraining()

        self.assertEqual(self.recorder.posts(), [])

    def test_failed_finish_training_still_adds_training(self):
        self.recorder.replies[CHARACTERS] = _characters_reply([self.ROSTER[0]])
        self.recorder.replies[FINISH_TRAINING] = TrainingRequestRecorder.FINISH_ERROR_REPLY

        self.assertIs(self.client.manageTraining(), True)

        self.assertEqual(self.recorder.posts(), self.training_posts("101", "11"))


class TestSMTPPreValidation(unittest.TestCase):
    """Test CLI SMTP pre-validation and exit codes before gameplay (R6)."""

    def test_smtp_disabled_when_no_flags(self):
        client = recording_client()

        with patch("run.email_logfile") as mock_email:
            code = run_main(client)

        self.assertEqual(code, 0)
        self.assertEqual(client.step_calls, list(RUN_MAIN_STEPS))
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

    def test_smtp_valid_configuration_invokes_email_logfile_after_gameplay(self):
        tmp_pwd_file = Path("tests_scratch_valid_pw.txt")
        tmp_pwd_file.write_text("synthetic_secret_password\n")
        try:
            client = recording_client()

            def email_logfile(*args):
                client.step_calls.append("email_logfile")
                return True

            test_args = [
                "run.py",
                "--smtp-email",
                "s@e.com",
                "--smtp-password-file",
                str(tmp_pwd_file),
                "-r",
                "r@e.com",
            ]
            with patch("run.email_logfile", side_effect=email_logfile) as mock_email:
                code = run_main(client, test_args)

            self.assertEqual(code, 0)
            # The log is e-mailed once, after every gameplay step has run.
            self.assertEqual(client.step_calls, [*RUN_MAIN_STEPS, "email_logfile"])
            mock_email.assert_called_once_with(
                "tachikoma.log", client, "s@e.com", "synthetic_secret_password", "r@e.com"
            )
        finally:
            if tmp_pwd_file.exists():
                tmp_pwd_file.unlink()


class TestRunMainHarness(unittest.TestCase):
    """recording_client stays in step with Client, so a renamed or removed client method fails the run.main() tests."""

    def test_every_run_main_step_is_a_client_method(self):
        for name in RUN_MAIN_STEPS:
            with self.subTest(step=name):
                self.assertTrue(callable(getattr(Client, name, None)), f"Client has no method {name}")

    def test_the_fake_client_rejects_methods_client_does_not_have(self):
        client = recording_client()

        with self.assertRaises(AttributeError):
            client.collectEverythingAtOnce()


class TestExitCodeAggregation(unittest.TestCase):
    """Test truthful exit code aggregation and nonfatal execution sequence (R7)."""

    def test_exit_code_contract_for_every_step_outcome(self):
        # redact_secrets scrubs a token in its accessToken=<value> form (a bare UUID is not redacted).
        secret = f"accessToken={SYNTHETIC_ACCESS_TOKEN}"
        for name in RUN_MAIN_STEPS:
            for outcome, fatal in ((False, FATAL_ON_FALSE), (RuntimeError(f"boom {secret}"), FATAL_ON_RAISE)):
                with self.subTest(step=name, outcome="raises" if isinstance(outcome, RuntimeError) else "False"):
                    client = recording_client({name: outcome})

                    with self.assertLogs(level="DEBUG") as logs:
                        code = run_main(client)

                    self.assertEqual(code, 1 if name in fatal else 0)
                    self.assertEqual(client.step_calls, list(RUN_MAIN_STEPS))
                    output = "\n".join(logs.output)
                    if isinstance(outcome, RuntimeError):
                        self.assertIn(f":root:{name} failed: boom accessToken=***REDACTED***", output)
                    self.assertNotIn(SYNTHETIC_ACCESS_TOKEN, output)

    def test_failed_login_exits_1_before_any_gameplay_step(self):
        client = recording_client()
        client.login.return_value = False

        with self.assertLogs(level="WARNING") as logs:
            code = run_main(client)

        self.assertEqual(code, 1)
        self.assertEqual(client.step_calls, [])
        self.assertIn("WARNING:root:[authenticate] failed to login", logs.output)

    def test_email_logfile_exception_fails_the_run_after_every_step(self):
        with TemporaryDirectory() as directory:
            password_file = Path(directory) / "smtp_password.txt"
            password_file.write_text("synthetic_secret_password\n")
            client = recording_client()
            argv = ["run.py", "--smtp-email", "s@e.com", "--smtp-password-file", str(password_file), "-r", "r@e.com"]

            with (
                patch("run.email_logfile", side_effect=RuntimeError("smtp down")),
                self.assertLogs(level="ERROR") as logs,
            ):
                code = run_main(client, argv)

        self.assertEqual(code, 1)
        self.assertEqual(client.step_calls, list(RUN_MAIN_STEPS))
        self.assertIn("email_logfile raised exception: smtp down", "\n".join(logs.output))

    def test_exit_0_on_success_and_skips(self):
        client = recording_client()

        self.assertEqual(run_main(client), 0)
        self.assertEqual(client.step_calls, list(RUN_MAIN_STEPS))
        self.assertIs(client._automation_done, True)

    def test_exit_1_on_unexpected_room_failure_and_continues_training(self):
        client = recording_client({"upgradeRooms": False})

        self.assertEqual(run_main(client), 1)
        self.assertEqual(client.step_calls, list(RUN_MAIN_STEPS))

    def test_exit_1_on_unexpected_training_failure(self):
        client = recording_client({"manageTraining": False})

        self.assertEqual(run_main(client), 1)
        self.assertEqual(client.step_calls, list(RUN_MAIN_STEPS))

    def test_independent_actions_continue_after_research_failure(self):
        client = recording_client({"upgradeResearches": False})

        self.assertEqual(run_main(client), 1)
        self.assertEqual(client.step_calls, list(RUN_MAIN_STEPS))

    def test_automation_already_done_runs_only_per_pass_steps(self):
        client = recording_client(_automation_done=True)

        self.assertEqual(run_main(client), 0)
        self.assertEqual(
            client.step_calls,
            [
                "heartbeat",
                "purchaseScorchedPodIfAffordable",
                "grabFlyingStarbux",
                "collectTaskReward",
                "getResourceTotals",
                "upgradeCharacters",
            ],
        )

    def test_collect_task_reward_skipped_below_daily_cap(self):
        client = recording_client(freeStarbuxToday=3)

        self.assertEqual(run_main(client), 0)
        self.assertEqual(client.step_calls, [name for name in RUN_MAIN_STEPS if name != "collectTaskReward"])

    def test_collect_task_reward_exception_fails_run(self):
        client = recording_client({"collectTaskReward": RuntimeError("boom")})

        with self.assertLogs(level="ERROR") as logs:
            code = run_main(client)

        self.assertEqual(code, 1)
        self.assertIn("ERROR:root:collectTaskReward failed: boom", logs.output)
        self.assertEqual(client.step_calls, list(RUN_MAIN_STEPS))

    def test_analyze_ship_layout_exception_is_nonfatal(self):
        client = recording_client({"analyzeShipLayout": RuntimeError("boom")})

        with self.assertLogs(level="ERROR") as logs:
            code = run_main(client)

        self.assertEqual(code, 0)
        self.assertIn("ERROR:root:analyzeShipLayout failed: boom", logs.output)
        self.assertEqual(client.step_calls, list(RUN_MAIN_STEPS))


if __name__ == "__main__":
    unittest.main()
