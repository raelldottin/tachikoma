from __future__ import annotations

"""Tests for crew leveling logic."""

import unittest
from datetime import datetime, timezone, timedelta

from sdk.crew_leveling import (
    UpgradeBlockReason,
    UpgradeDecision,
    parse_server_datetime,
    get_xp_required,
    get_gas_required,
    evaluate_upgrade,
    plan_upgrades,
    MAX_CHARACTER_LEVEL,
    STANDARD_XP_REQUIRED,
    STANDARD_GAS_REQUIRED,
    LEGENDARY_GAS_REQUIRED,
    LEGENDARY_XP_MULTIPLIER,
)


class TestParseServerDatetime(unittest.TestCase):
    """Tests for parse_server_datetime function."""

    def test_valid_datetime(self):
        """Test parsing valid server datetime."""
        result = parse_server_datetime("2026-08-02T17:30:00")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2026)
        self.assertEqual(result.month, 8)
        self.assertEqual(result.day, 2)
        self.assertEqual(result.hour, 17)
        self.assertEqual(result.minute, 30)
        self.assertEqual(result.tzinfo, timezone.utc)

    def test_none_input(self):
        """Test parsing None returns None."""
        self.assertIsNone(parse_server_datetime(None))

    def test_empty_string(self):
        """Test parsing empty string returns None."""
        self.assertIsNone(parse_server_datetime(""))

    def test_invalid_format(self):
        """Test parsing invalid format returns None."""
        self.assertIsNone(parse_server_datetime("invalid"))


class TestCostTables(unittest.TestCase):
    """Tests for cost table access functions."""

    def test_get_xp_required_standard(self):
        """Test standard XP lookup."""
        self.assertEqual(get_xp_required(1, False), 90)
        self.assertEqual(get_xp_required(2, False), 270)
        self.assertEqual(get_xp_required(10, False), 1860)
        self.assertEqual(get_xp_required(39, False), 12510)

    def test_get_xp_required_legendary(self):
        """Test legendary XP multiplier."""
        self.assertEqual(get_xp_required(1, True), 90 * 3)
        self.assertEqual(get_xp_required(2, True), 270 * 3)
        self.assertEqual(get_xp_required(10, True), 1860 * 3)

    def test_get_xp_required_max_level(self):
        """Test max level returns 0."""
        self.assertEqual(get_xp_required(40, False), 0)
        self.assertEqual(get_xp_required(40, True), 0)
        self.assertEqual(get_xp_required(50, False), 0)

    def test_get_gas_required_standard(self):
        """Test standard gas lookup."""
        self.assertEqual(get_gas_required(1, False), 0)
        self.assertEqual(get_gas_required(2, False), 0)
        self.assertEqual(get_gas_required(3, False), 17)
        self.assertEqual(get_gas_required(4, False), 33)
        self.assertEqual(get_gas_required(5, False), 65)

    def test_get_gas_required_legendary(self):
        """Test legendary gas lookup."""
        self.assertEqual(get_gas_required(1, True), 0)
        self.assertEqual(get_gas_required(2, True), 130000)
        self.assertEqual(get_gas_required(3, True), 162500)

    def test_get_gas_required_max_level(self):
        """Test max level returns 0 gas."""
        self.assertEqual(get_gas_required(40, False), 0)
        self.assertEqual(get_gas_required(40, True), 0)


class TestEvaluateUpgrade(unittest.TestCase):
    """Tests for evaluate_upgrade function."""

    def setUp(self):
        self.now = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
        self.base_character = {
            "@CharacterId": "12345",
            "@CharacterName": "Test Character",
            "@Level": "20",
            "@Xp": "5000",
            "@AvailableDate": "2026-08-01T12:00:00",
            "@RoomId": "1",
            "@CharacterDesignId": "DESIGN_1",
        }
        self.base_design = {
            "@CharacterDesignId": "DESIGN_1",
            "@Rarity": "Standard",
        }

    def test_eligible_standard_character(self):
        """Test standard character with sufficient XP and gas."""
        character = {**self.base_character, "@Level": "20", "@Xp": "10000"}
        design = {**self.base_design, "@Rarity": "Standard"}
        
        decision = evaluate_upgrade(
            character, design, gas_available=104000, now=datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
        )
        
        self.assertEqual(decision.reason, UpgradeBlockReason.ELIGIBLE)
        self.assertEqual(decision.character_id, "12345")
        self.assertEqual(decision.current_level, 20)
        self.assertEqual(decision.next_level, 21)
        self.assertFalse(decision.is_legendary)

    def test_eligible_legendary_character(self):
        """Test legendary character with sufficient XP and gas."""
        character = {**self.base_character, "@Level": "20", "@Xp": "100000"}
        design = {**self.base_design, "@Rarity": "Legendary"}
        
        # Legendary gas cost for level 20 is 877500
        decision = evaluate_upgrade(
            character, design, gas_available=877500, now=datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
        )
        
        self.assertEqual(decision.reason, UpgradeBlockReason.ELIGIBLE)
        self.assertTrue(decision.is_legendary)
        # Legendary XP is 3x
        self.assertEqual(decision.xp_required, 4860 * 3)
        # Legendary gas from table
        self.assertEqual(decision.gas_required, 877500)

    def test_max_level_blocked(self):
        """Test max level (40) cannot be upgraded."""
        character = {**self.base_character, "@Level": "40", "@Xp": "100000"}
        design = {**self.base_design, "@Rarity": "Standard"}
        
        decision = evaluate_upgrade(character, self.base_design, 100000, self.now)
        
        self.assertEqual(decision.reason, UpgradeBlockReason.MAX_LEVEL)
        self.assertEqual(decision.next_level, 40)

    def test_insufficient_xp_blocked(self):
        """Test insufficient XP blocks upgrade."""
        character = {**self.base_character, "@Level": "20", "@Xp": "100"}  # Too low
        design = {**self.base_design, "@Rarity": "Standard"}
        
        decision = evaluate_upgrade(character, design, 100000, self.now)
        
        self.assertEqual(decision.reason, UpgradeBlockReason.INSUFFICIENT_XP)
        self.assertLess(decision.xp_available, decision.xp_required)

    def test_insufficient_gas_blocked(self):
        """Test insufficient gas blocks upgrade."""
        character = {**self.base_character, "@Level": "20", "@Xp": "10000"}
        design = {**self.base_design, "@Rarity": "Standard"}
        
        decision = evaluate_upgrade(character, design, gas_available=100, now=self.now)
        
        self.assertEqual(decision.reason, UpgradeBlockReason.INSUFFICIENT_GAS)
        self.assertLess(decision.gas_available, decision.gas_required)

    def test_not_available_future_date_blocked(self):
        """Test future AvailableDate blocks upgrade."""
        future_date = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        character = {**self.base_character, "@Level": "20", "@Xp": "10000", "@AvailableDate": future_date}
        design = {**self.base_design, "@Rarity": "Standard"}
        
        decision = evaluate_upgrade(character, design, 100000, self.now)
        
        self.assertEqual(decision.reason, UpgradeBlockReason.NOT_AVAILABLE)

    def test_not_available_room_zero_blocked(self):
        """Test character not in a room (RoomId=0) cannot upgrade."""
        character = {**self.base_character, "@Level": "20", "@Xp": "10000", "@RoomId": "0"}
        design = {**self.base_design, "@Rarity": "Standard"}
        
        decision = evaluate_upgrade(character, design, 100000, self.now)
        
        self.assertEqual(decision.reason, UpgradeBlockReason.NOT_AVAILABLE)

    def test_legendary_xp_multiplier(self):
        """Test legendary XP is 3x standard."""
        character = {**self.base_character, "@Level": "10", "@Xp": "10000"}
        design_standard = {**self.base_design, "@Rarity": "Standard"}
        design_legendary = {**self.base_design, "@Rarity": "Legendary"}
        
        std_decision = evaluate_upgrade(character, design_standard, 100000, self.now)
        leg_decision = evaluate_upgrade(character, design_legendary, 100000, self.now)
        
        self.assertEqual(leg_decision.xp_required, std_decision.xp_required * 3)

    def test_decision_contains_all_fields(self):
        """Test decision contains all expected fields."""
        character = {**self.base_character, "@Level": "20", "@Xp": "10000"}
        design = {**self.base_design, "@Rarity": "Standard"}
        
        # Gas cost for level 20 is 104000
        decision = evaluate_upgrade(character, design, 104000, self.now)
        
        self.assertEqual(decision.character_id, "12345")
        self.assertEqual(decision.character_name, "Test Character")
        self.assertEqual(decision.current_level, 20)
        self.assertEqual(decision.next_level, 21)
        self.assertEqual(decision.xp_available, 10000)
        self.assertEqual(decision.xp_required, 4860)
        self.assertEqual(decision.gas_available, 104000)
        # Gas cost for level 20 from STANDARD_GAS_REQUIRED table
        self.assertEqual(decision.gas_required, 104000)
        self.assertEqual(decision.reason, UpgradeBlockReason.ELIGIBLE)
        self.assertFalse(decision.is_legendary)
        self.assertTrue(decision.is_eligible)


class TestPlanUpgrades(unittest.TestCase):
    """Tests for plan_upgrades function."""

    def setUp(self):
        self.now = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
        self.base_character = {
            "@CharacterId": "12345",
            "@CharacterName": "Test Character",
            "@Level": "20",
            "@Xp": "10000",
            "@AvailableDate": "2026-08-01T12:00:00",
            "@RoomId": "1",
            "@CharacterDesignId": "DESIGN_1",
        }
        self.base_design = {
            "@CharacterDesignId": "DESIGN_1",
            "@Rarity": "Standard",
        }

    def test_plan_upgrades_single_eligible(self):
        """Test planning single eligible upgrade."""
        characters = [{**self.base_character, "@Level": "20", "@Xp": "10000"}]
        designs = [{**self.base_design, "@Rarity": "Standard", "@CharacterDesignId": "DESIGN_1"}]
        
        # Gas cost for level 20 is 104000
        eligible, remaining_gas = plan_upgrades(characters, designs, 104000, self.now)
        
        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0].reason, UpgradeBlockReason.ELIGIBLE)
        self.assertEqual(remaining_gas, 0)  # gas - cost

    def test_plan_upgrades_multiple_eligible(self):
        """Test planning multiple eligible upgrades."""
        characters = [
            {**self.base_character, "@CharacterId": "1", "@CharacterName": "Char1", "@Level": "20", "@Xp": "10000"},
            {**self.base_character, "@CharacterId": "2", "@CharacterName": "Char2", "@Level": "20", "@Xp": "10000"},
        ]
        designs = [
            {**self.base_design, "@CharacterDesignId": "DESIGN_1"},
            {**self.base_design, "@CharacterDesignId": "DESIGN_2"},
        ]
        
        # Each upgrade costs 104000 gas
        eligible, remaining_gas = plan_upgrades(characters, designs, 208000, self.now)
        
        self.assertEqual(len(eligible), 2)

    def test_plan_upgrades_max_upgrades_limit(self):
        """Test max_upgrades limit is respected."""
        characters = [
            {**self.base_character, "@CharacterId": str(i), "@CharacterName": f"Char{i}", "@Level": "20", "@Xp": "10000"}
            for i in range(10)
        ]
        designs = [
            {**self.base_design, "@CharacterDesignId": f"DESIGN_{i}"}
            for i in range(10)
        ]
        
        eligible, _ = plan_upgrades(characters, designs, 10000000, self.now, max_upgrades=3)
        
        self.assertEqual(len(eligible), 3)

    def test_plan_upgrades_gas_reservation(self):
        """Test gas is reserved for subsequent upgrades."""
        characters = [
            {**self.base_character, "@CharacterId": "1", "@CharacterName": "Char1", "@Level": "20", "@Xp": "10000"},
            {**self.base_character, "@CharacterId": "2", "@CharacterName": "Char2", "@Level": "20", "@Xp": "10000"},
        ]
        designs = [
            {**self.base_design, "@CharacterDesignId": "DESIGN_1"},
            {**self.base_design, "@CharacterDesignId": "DESIGN_2"},
        ]
        
        # Gas enough for only 1 upgrade (104000)
        eligible, remaining_gas = plan_upgrades(characters, designs, 104000 + 100, self.now)
        
        self.assertEqual(len(eligible), 1)
        self.assertEqual(remaining_gas, 100)

    def test_plan_updates_remaining_gas(self):
        """Test remaining gas is correctly calculated."""
        characters = [
            {**self.base_character, "@CharacterId": "1", "@CharacterName": "Char1", "@Level": "20", "@Xp": "10000"},
            {**self.base_character, "@CharacterId": "2", "@CharacterName": "Char2", "@Level": "20", "@Xp": "10000"},
        ]
        designs = [
            {**self.base_design, "@CharacterDesignId": "DESIGN_1"},
            {**self.base_design, "@CharacterDesignId": "DESIGN_2"},
        ]
        
        # Each upgrade costs 104000 gas
        eligible, remaining_gas = plan_upgrades(characters, designs, 208000, self.now)
        
        expected_remaining = 208000 - 2 * 104000
        self.assertEqual(remaining_gas, expected_remaining)

    def test_plan_upgrades_insufficient_gas_second(self):
        """Test second upgrade blocked by insufficient gas after first."""
        characters = [
            {**self.base_character, "@CharacterId": "1", "@CharacterName": "Char1", "@Level": "20", "@Xp": "10000"},
            {**self.base_character, "@CharacterId": "2", "@CharacterName": "Char2", "@Level": "20", "@Xp": "10000"},
        ]
        designs = [
            {**self.base_design, "@CharacterDesignId": "DESIGN_1"},
            {**self.base_design, "@CharacterDesignId": "DESIGN_2"},
        ]
        
        # Only enough for 1 upgrade (104000)
        eligible, _ = plan_upgrades(characters, designs, 150000, self.now)
        
        self.assertEqual(len(eligible), 1)


class TestConstants(unittest.TestCase):
    """Test constant values are correct."""

    def test_max_character_level(self):
        self.assertEqual(MAX_CHARACTER_LEVEL, 40)

    def test_legendary_xp_multiplier(self):
        from sdk.crew_leveling import LEGENDARY_XP_MULTIPLIER
        self.assertEqual(LEGENDARY_XP_MULTIPLIER, 3)

    def test_standard_xp_has_expected_levels(self):
        self.assertIn(1, STANDARD_XP_REQUIRED)
        self.assertIn(39, STANDARD_XP_REQUIRED)
        self.assertNotIn(40, STANDARD_XP_REQUIRED)

    def test_standard_gas_has_expected_levels(self):
        self.assertIn(1, STANDARD_GAS_REQUIRED)
        self.assertIn(39, STANDARD_GAS_REQUIRED)

    def test_legendary_gas_has_expected_levels(self):
        self.assertIn(1, LEGENDARY_GAS_REQUIRED)
        self.assertIn(31, LEGENDARY_GAS_REQUIRED)


class TestParseServerDatetimeEdgeCases(unittest.TestCase):
    """Edge cases for datetime parsing."""

    def test_malformed_datetime(self):
        self.assertIsNone(parse_server_datetime("not-a-date"))
        self.assertIsNone(parse_server_datetime("2026/08/02"))
        self.assertIsNone(parse_server_datetime("2026-13-02T12:00:00"))

    def test_timezone_aware_utc(self):
        result = parse_server_datetime("2026-08-02T17:30:00")
        self.assertEqual(result.tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()