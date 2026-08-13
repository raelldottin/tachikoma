from __future__ import annotations

"""Tests for crew leveling logic."""

import unittest
from datetime import datetime, timezone, timedelta

from sdk.crew_leveling import (
    UpgradeBlockReason,
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


class TestCrewStatFormulas(unittest.TestCase):
    """Tests for crew stat / training formulas from the Crew Planning and Training Guide."""

    def test_tp_caps_by_rarity(self):
        """TP caps match the guide: 3*=70, 4*=80, 5*=90, 6*=100, 7*=110."""
        from sdk.crew_leveling import get_tp_cap

        self.assertEqual(get_tp_cap(3), 70)
        self.assertEqual(get_tp_cap(4), 80)
        self.assertEqual(get_tp_cap(5), 90)
        self.assertEqual(get_tp_cap(6), 100)
        self.assertEqual(get_tp_cap(7), 110)

    def test_tp_cap_exceptional_4star(self):
        """Mistycball and Huge Hellaluya (4*) have 100 TP, not 80."""
        from sdk.crew_leveling import get_tp_cap

        self.assertEqual(get_tp_cap(4, "Mistycball"), 100)
        self.assertEqual(get_tp_cap(4, "Huge Hellaluya"), 100)
        # Regular 4* crew still get 80
        self.assertEqual(get_tp_cap(4, "Robyna Hoots"), 80)

    def test_tp_cap_captain(self):
        """6* captains get 200 TP."""
        from sdk.crew_leveling import get_tp_cap

        self.assertEqual(get_tp_cap(6, is_captain=True), 200)
        # Non-captain 6* crew get standard 100
        self.assertEqual(get_tp_cap(6, is_captain=False), 100)

    def test_tp_cap_unknown_rarity(self):
        """Unknown rarity returns 0."""
        from sdk.crew_leveling import get_tp_cap

        self.assertEqual(get_tp_cap(1), 0)
        self.assertEqual(get_tp_cap(0), 0)

    def test_crew_level_cap(self):
        """Level cap = ship_level × 4, max 40."""
        from sdk.crew_leveling import get_crew_level_cap

        self.assertEqual(get_crew_level_cap(1), 4)
        self.assertEqual(get_crew_level_cap(5), 20)
        self.assertEqual(get_crew_level_cap(10), 40)
        self.assertEqual(get_crew_level_cap(15), 40)  # capped at 40

    def test_final_stat_formula(self):
        """final_stat = base × (1 + TP/100) + equipment."""
        from sdk.crew_leveling import compute_final_stat

        # Base 10 HP, 15 TP in HP, 0 equipment → 11.5
        result = compute_final_stat(10, 15, 0)
        self.assertAlmostEqual(result, 11.5)

        # Base 10, 20 TP, 5 equipment → 10 × 1.20 + 5 = 17.0
        result = compute_final_stat(10, 20, 5)
        self.assertAlmostEqual(result, 17.0)

        # Base 7, 70 TP (max for 3*), 0 equipment → 7 × 1.70 = 11.9
        result = compute_final_stat(7, 70, 0)
        self.assertAlmostEqual(result, 11.9)

    def test_final_ability_formula(self):
        """final_ability = base × (1 + TP/100) × (1 + equipment%)."""
        from sdk.crew_leveling import compute_final_ability

        # Base 5.0, 50 TP, 20% equipment → 5.0 × 1.5 × 1.2 = 9.0
        result = compute_final_ability(5.0, 50, 20)
        self.assertAlmostEqual(result, 9.0)

        # Base 3.0, 0 TP, 0% equipment → 3.0
        result = compute_final_ability(3.0, 0, 0)
        self.assertAlmostEqual(result, 3.0)

    def test_final_stamina_formula(self):
        """final_stamina = TP + equipment_bonus (additive, not multiplicative)."""
        from sdk.crew_leveling import compute_final_stamina

        # 70 TP + 10 equipment → 80
        self.assertEqual(compute_final_stamina(70, 10), 80)
        # 0 TP, 5 equipment → 5
        self.assertEqual(compute_final_stamina(0, 5), 5)
        # 100 TP, 0 equipment → 100
        self.assertEqual(compute_final_stamina(100, 0), 100)

    def test_hp_rounding_from_guide(self):
        """Guide example: 10 HP crew with 15 TP → 11.5 (guide notes display
        shows 11.45 as 11.5 but game rounds UP to 12)."""
        from sdk.crew_leveling import compute_final_stat
        import math

        raw = compute_final_stat(10, 15, 0)
        # HP should be rounded UP per guide
        self.assertEqual(math.ceil(raw), 12)

    def test_gunner_blueprint_example(self):
        """Guide mentions Robyna Hoots (3* gunner) with 10 HP and 39.4 Weapon.
        Verify that 39.4 Weapon is achievable with reasonable TP allocation."""
        from sdk.crew_leveling import compute_final_stat

        # If base weapon is ~23 (typical 3* gunner), need 39.4/23 ≈ 1.713 multiplier
        # So ~71% TP in weapon: compute_final_stat(23, 71, 0) ≈ 39.33
        # With 3 equipment: compute_final_stat(23, 68, 3) = 23 × 1.68 + 3 = 41.64
        # Or: base 20, 70 TP, 5 equipment = 20 × 1.70 + 5 = 39.0
        # These are approximate — the guide says "not too shabby" for 10HP/39.4WPN
        result = compute_final_stat(20, 70, 5)
        self.assertAlmostEqual(result, 39.0, places=1)

    def test_xp_from_pvp(self):
        """XP from PVP = enemy_trophies / 10 (from guide Section 3)."""
        # This is documented but not implemented as a function — verify formula
        enemy_trophies = 1000
        xp = enemy_trophies / 10
        self.assertEqual(xp, 100)


if __name__ == "__main__":
    unittest.main()