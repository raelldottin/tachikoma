from __future__ import annotations

"""Tests for ship layout analysis — pure functions, no HTTP."""

import unittest
from unittest.mock import MagicMock, patch

from sdk.ship_layout import (
    parse_rooms,
    analyze_layout,
    format_analysis_report,
    classify_room,
    RoomInfo,
    LayoutAnalysis,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

def _make_design(design_id, name="Room", width=1, height=1, hp=100, power=0, pop=0):
    return {
        "@RoomDesignId": str(design_id),
        "@RoomName": name,
        "@ColumnWidth": str(width),
        "@RowHeight": str(height),
        "@RoomHp": str(hp),
        "@PowerGenerated": str(power),
        "@MaxPopulation": str(pop),
    }


def _make_room(room_id, design_id, row, col, status="normal"):
    return {
        "@RoomId": str(room_id),
        "@RoomDesignId": str(design_id),
        "@Row": str(row),
        "@Column": str(col),
        "@RoomStatus": status,
        "@UpgradeRoomDesignId": "0",
    }


def _make_ship(rooms_list):
    """Create a ship data dict with the given rooms."""
    return {
        "Rooms": {"Room": rooms_list},
    }


# Standard test designs
TEST_DESIGNS = [
    _make_design("256", "Heavy Armor", 1, 1, hp=200),       # armor
    _make_design("100", "Laser Cannon", 2, 1, hp=50),        # weapon
    _make_design("200", "Shield Generator", 2, 1, hp=80),     # shield
    _make_design("300", "Reactor", 2, 2, hp=100, power=20),  # reactor
    _make_design("400", "Medbay", 2, 2, hp=60),               # repair
    _make_design("500", "Crew Quarters", 2, 2, pop=3),       # bedroom
    _make_design("600", "Lift", 1, 1),                        # corridor
    _make_design("700", "Storage", 1, 1),                     # storage
    _make_design("800", "Gym", 2, 2),                         # training
    _make_design("900", "Lab", 2, 2),                         # lab
]


class TestClassifyRoom(unittest.TestCase):
    """Tests for room classification."""

    def test_armor_by_design_id(self):
        """Known armor design IDs are classified as armor."""
        d = _make_design("256", "Some Random Name")
        self.assertEqual(classify_room(d), "armor")

    def test_weapon_by_name(self):
        d = _make_design("999", "Plasma Laser MK2")
        self.assertEqual(classify_room(d), "weapon")

    def test_reactor_by_name(self):
        d = _make_design("999", "Fusion Reactor")
        self.assertEqual(classify_room(d), "reactor")

    def test_repair_by_name(self):
        d = _make_design("999", "Medbay")
        self.assertEqual(classify_room(d), "repair")

    def test_shield_by_name(self):
        d = _make_design("999", "Shield Generator")
        self.assertEqual(classify_room(d), "shield")

    def test_bedroom_by_population(self):
        d = _make_design("999", "Crew Quarters", pop=3)
        self.assertEqual(classify_room(d), "bedroom")

    def test_security_by_name(self):
        d = _make_design("999", "Security Station")
        self.assertEqual(classify_room(d), "security")

    def test_training_by_name(self):
        d = _make_design("999", "Training Gym")
        self.assertEqual(classify_room(d), "training")

    def test_lab_by_name(self):
        d = _make_design("999", "Research Lab")
        self.assertEqual(classify_room(d), "lab")

    def test_other_for_unknown(self):
        d = _make_design("999", "Mystery Room")
        self.assertEqual(classify_room(d), "other")


class TestParseRooms(unittest.TestCase):
    """Tests for parse_rooms."""

    def test_parse_basic_rooms(self):
        """parse_rooms correctly extracts room data."""
        ship = _make_ship([
            _make_room("1", "256", 10, 20),   # armor
            _make_room("2", "100", 11, 20),   # weapon
            _make_room("3", "300", 12, 22),   # reactor
        ])
        rooms = parse_rooms(ship, TEST_DESIGNS)
        self.assertEqual(len(rooms), 3)
        self.assertEqual(rooms[0].category, "armor")
        self.assertEqual(rooms[1].category, "weapon")
        self.assertEqual(rooms[2].category, "reactor")
        self.assertEqual(rooms[0].row, 10)
        self.assertEqual(rooms[0].column, 20)

    def test_parse_single_room_dict(self):
        """parse_rooms handles a single room (dict, not list)."""
        ship = {"Rooms": {"Room": _make_room("1", "256", 10, 20)}}
        rooms = parse_rooms(ship, TEST_DESIGNS)
        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0].category, "armor")

    def test_parse_room_with_unknown_design(self):
        """parse_rooms handles rooms with unknown design IDs."""
        ship = _make_ship([_make_room("1", "99999", 10, 20)])
        rooms = parse_rooms(ship, TEST_DESIGNS)
        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0].category, "other")

    def test_parse_extracts_design_attributes(self):
        """parse_rooms extracts width, height, hp, power from design."""
        ship = _make_ship([_make_room("1", "300", 10, 20)])  # reactor 2×2, power=20
        rooms = parse_rooms(ship, TEST_DESIGNS)
        self.assertEqual(rooms[0].width, 2)
        self.assertEqual(rooms[0].height, 2)
        self.assertEqual(rooms[0].power, 20)


class TestAnalyzeLayout(unittest.TestCase):
    """Tests for analyze_layout."""

    def test_empty_layout(self):
        """Empty rooms list returns empty analysis."""
        analysis = analyze_layout([])
        self.assertEqual(analysis.total_rooms, 0)
        self.assertTrue(len(analysis.recommendations) > 0)

    def test_perfect_layout_armor_around_reactor(self):
        """Reactor surrounded by armor has high armor_coverage."""
        rooms = [
            RoomInfo("1", "256", 10, 20, "normal", 1, 1, "armor", "Heavy Armor"),      # armor
            RoomInfo("2", "256", 10, 21, "normal", 1, 1, "armor", "Heavy Armor"),      # armor
            RoomInfo("3", "256", 11, 20, "normal", 1, 1, "armor", "Heavy Armor"),      # armor
            RoomInfo("4", "256", 11, 21, "normal", 1, 1, "armor", "Heavy Armor"),      # armor
            RoomInfo("5", "300", 12, 20, "normal", 2, 2, "reactor", "Reactor", power=20),  # reactor adjacent to armor below
        ]
        analysis = analyze_layout(rooms, ship_name="TestShip", ship_level=10)
        self.assertGreater(analysis.armor_coverage, 0)
        self.assertGreater(analysis.defense_score, 0)

    def test_exposed_reactor_no_armor(self):
        """Reactor with no adjacent armor is flagged as exposed."""
        rooms = [
            RoomInfo("1", "300", 10, 20, "normal", 2, 2, "reactor", "Reactor", power=20),
            RoomInfo("2", "100", 14, 20, "normal", 2, 1, "weapon", "Laser Cannon"),
        ]
        analysis = analyze_layout(rooms, ship_name="TestShip")
        self.assertEqual(analysis.armor_coverage, 0)
        self.assertTrue(any("Armor gap" in r for r in analysis.recommendations))
        self.assertTrue(len(analysis.critical_rooms) > 0)

    def test_no_repair_rooms(self):
        """Missing repair rooms generates recommendation."""
        rooms = [
            RoomInfo("1", "300", 10, 20, "normal", 2, 2, "reactor", "Reactor", power=20),
            RoomInfo("2", "256", 10, 22, "normal", 1, 1, "armor", "Heavy Armor"),
        ]
        analysis = analyze_layout(rooms)
        self.assertEqual(analysis.repair_proximity, 0)
        self.assertTrue(any("repair" in r.lower() for r in analysis.recommendations))

    def test_weapon_imbalance(self):
        """All weapons on one side triggers imbalance warning."""
        # ship grid: cols 10-30; all weapons at col 10 (left side)
        rooms = [
            RoomInfo("1", "100", 10, 10, "normal", 2, 1, "weapon", "Laser 1"),
            RoomInfo("2", "100", 12, 10, "normal", 2, 1, "weapon", "Laser 2"),
            RoomInfo("3", "100", 14, 10, "normal", 2, 1, "weapon", "Laser 3"),
            RoomInfo("4", "256", 16, 25, "normal", 1, 1, "armor", "Armor"),
        ]
        analysis = analyze_layout(rooms)
        self.assertTrue(any("Weapon imbalance" in r for r in analysis.recommendations))
        self.assertLess(analysis.weapon_coverage, 50)

    def test_no_weapons(self):
        """No weapon rooms generates recommendation."""
        rooms = [
            RoomInfo("1", "300", 10, 20, "normal", 2, 2, "reactor", "Reactor"),
            RoomInfo("2", "256", 10, 22, "normal", 1, 1, "armor", "Armor"),
        ]
        analysis = analyze_layout(rooms)
        self.assertTrue(any("weapon" in r.lower() for r in analysis.recommendations))

    def test_bedroom_capacity_warning(self):
        """Low bedroom capacity triggers crew count recommendation."""
        rooms = [
            RoomInfo("1", "500", 10, 20, "normal", 2, 2, "bedroom", "Quarters", capacity=3),
            RoomInfo("2", "256", 10, 22, "normal", 1, 1, "armor", "Armor"),
        ]
        analysis = analyze_layout(rooms)
        self.assertTrue(any("Bedroom capacity" in r or "crew" in r.lower() for r in analysis.recommendations))

    def test_rooms_under_construction(self):
        """Rooms under construction are flagged."""
        rooms = [
            RoomInfo("1", "300", 10, 20, "constructing", 2, 2, "reactor", "Reactor"),
        ]
        analysis = analyze_layout(rooms)
        self.assertTrue(any("construction" in r.lower() for r in analysis.recommendations))

    def test_pending_upgrades(self):
        """Rooms with pending upgrades are flagged."""
        rooms = [
            RoomInfo("1", "300", 10, 20, "normal", 2, 2, "reactor", "Reactor",
                     upgrade_id="999"),
        ]
        analysis = analyze_layout(rooms)
        self.assertTrue(any("upgrade" in r.lower() for r in analysis.recommendations))

    def test_defense_score_range(self):
        """Defense score is in 0-100 range."""
        rooms = [
            RoomInfo("1", "300", 10, 20, "normal", 2, 2, "reactor", "Reactor", power=20),
            RoomInfo("2", "256", 10, 22, "normal", 1, 1, "armor", "Armor"),
            RoomInfo("3", "100", 12, 25, "normal", 2, 1, "weapon", "Laser"),
            RoomInfo("4", "400", 14, 20, "normal", 2, 2, "repair", "Medbay"),
        ]
        analysis = analyze_layout(rooms)
        self.assertGreaterEqual(analysis.defense_score, 0)
        self.assertLessEqual(analysis.defense_score, 100)


class TestFormatReport(unittest.TestCase):
    """Tests for format_analysis_report."""

    def test_report_contains_key_sections(self):
        """Report contains ship name, scores, and recommendations."""
        analysis = LayoutAnalysis(
            ship_name="TestShip",
            ship_level=10,
            ship_design_id="233",
            total_rooms=5,
            grid_rows=(8, 18),
            grid_cols=(14, 30),
            rooms_by_category={"armor": 3, "reactor": 1, "weapon": 1},
            armor_coverage=80.0,
            repair_proximity=60.0,
            weapon_coverage=90.0,
            power_balance=70.0,
            defense_score=75.0,
            recommendations=["Test recommendation"],
        )
        report = format_analysis_report(analysis)
        self.assertIn("TestShip", report)
        self.assertIn("Armor Coverage", report)
        self.assertIn("OVERALL DEFENSE", report)
        self.assertIn("Test recommendation", report)
        self.assertIn("armor: 3", report)


class TestClientAnalyzeShipLayout(unittest.TestCase):
    """Tests for Client.analyzeShipLayout integration."""

    def setUp(self):
        from sdk.client import Client
        from sdk.device import Device
        self.device = Device(language="en")
        self.settings = {"checksum_key": "5343", "savy_checksum": "Savvy!s0d@"}
        self.client = Client(device=self.device, settings=self.settings)
        self.client.accessToken = "test-token"
        self.client.info = {"@Name": "TestCaptain"}

    @patch("sdk.client.Client.getShipByUserId")
    @patch("sdk.client.Client.listRoomDesigns2")
    def test_analyze_with_mock_data(self, mock_list_designs, mock_get_ship):
        """analyzeShipLayout works with mocked ship data."""
        mock_get_ship.return_value = True
        mock_list_designs.return_value = True

        self.client.shipByUserId = {
            "ShipService": {"GetShipByUserId": {"Ship": {
                "@ShipName": "TestShip",
                "@ShipLevel": "10",
                "@ShipDesignId": "233",
                "Rooms": {"Room": [
                    _make_room("1", "256", 10, 20),
                    _make_room("2", "300", 12, 20),
                ]},
            }}}
        }
        self.client.roomDesigns = {"RoomDesigns": {"RoomDesign": TEST_DESIGNS}}

        result = self.client.analyzeShipLayout()
        self.assertTrue(result)

    @patch("sdk.client.Client.getShipByUserId")
    def test_analyze_no_ship_data(self, mock_get_ship):
        """analyzeShipLayout returns False if ship data unavailable."""
        mock_get_ship.return_value = False
        result = self.client.analyzeShipLayout()
        self.assertFalse(result)

    def test_analyze_missing_ship_data_no_method_call(self):
        """analyzeShipLayout returns False if shipByUserId already None."""
        # Don't mock getShipByUserId — just ensure it's not set
        self.client.shipByUserId = None
        # getShipByUserId will be called and return False (no mock)
        with patch("sdk.client.Client.getShipByUserId", return_value=False):
            result = self.client.analyzeShipLayout()
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
