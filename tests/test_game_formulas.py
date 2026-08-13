"""Tests for game formulas — pure functions, no HTTP.

All formulas sourced from https://pixelstarships.fandom.com/wiki/Formulas
"""

import math
import unittest

from sdk.game_formulas import (
    # 1. Room Reload
    room_reload_boosted,
    room_reload_powered,
    room_reload,
    # 3. Escape
    escape_chance,
    escape_rate,
    # 4. Dodge
    dodge_evasion,
    DODGE_CAP_PERCENT,
    DODGE_CAP_COMBINED,
    # 5. Damage Reduction
    damage_reduction,
    effective_damage,
    # 6. Fire Damage
    fire_damage_reduced,
    fire_crew_damage,
    fire_ap_damage,
    # 7. Crew Stat by Level
    crew_stat_at_level,
    # 8. Gas Draw Price
    gas_draw_price,
    GAS_DRAW_BASE,
    GAS_DRAW_CAP,
    # 9. Trophy
    trophy_gain,
    TROPHY_BASE,
    TROPHY_MIN,
    TROPHY_MAX,
)


class TestRoomReload(unittest.TestCase):
    """Formula 1: Room Reload."""

    def test_boosted_no_crew_stat(self):
        """Zero crew stat → boosted = base (no change)."""
        self.assertAlmostEqual(room_reload_boosted(10.0, 0), 10.0)

    def test_boosted_with_crew_stat(self):
        """100 crew stat halves the reload time."""
        # 10 / ((100+100)/100) = 10/2 = 5
        self.assertAlmostEqual(room_reload_boosted(10.0, 100), 5.0)

    def test_boosted_high_stat(self):
        """300 crew stat → 10/4 = 2.5s reload."""
        self.assertAlmostEqual(room_reload_boosted(10.0, 300), 2.5)

    def test_boosted_zero_base(self):
        """Zero base reload stays zero."""
        self.assertAlmostEqual(room_reload_boosted(0, 100), 0)

    def test_boosted_negative_base_raises(self):
        with self.assertRaises(ValueError):
            room_reload_boosted(-1, 100)

    def test_powered_equal_power(self):
        """Power at 100% → powered = boosted (no change)."""
        self.assertAlmostEqual(room_reload_powered(5.0, 100, 100), 5.0)

    def test_powered_under_powered(self):
        """50% power → reload takes 2× longer."""
        self.assertAlmostEqual(room_reload_powered(5.0, 100, 50), 10.0)

    def test_powered_over_powered(self):
        """200% power → reload is 2× faster."""
        self.assertAlmostEqual(room_reload_powered(5.0, 100, 200), 2.5)

    def test_powered_zero_current_raises(self):
        with self.assertRaises(ValueError):
            room_reload_powered(5.0, 100, 0)

    def test_full_room_reload(self):
        """Combined boosted + powered calculation."""
        # Base 20, crew stat 100 → boosted = 10
        # Max power 100, current 50 → powered = 10 × 2 = 20
        self.assertAlmostEqual(room_reload(20.0, 100, 100, 50), 20.0)


class TestEscape(unittest.TestCase):
    """Formula 3: Escape."""

    def test_escape_chance_no_pilot(self):
        """Zero pilot stat → chance = mod (unchanged)."""
        self.assertAlmostEqual(escape_chance(0.5, 0), 0.5)

    def test_escape_chance_with_pilot(self):
        """100 pilot stat → chance = mod × 2."""
        self.assertAlmostEqual(escape_chance(0.5, 100), 1.0)

    def test_escape_chance_negative_pilot(self):
        """-50 pilot → chance = mod × 0.5."""
        self.assertAlmostEqual(escape_chance(0.5, -50), 0.25)

    def test_escape_rate_equal(self):
        """Equal escape stats → 50% rate."""
        self.assertAlmostEqual(escape_rate(100, 100), 50)

    def test_escape_rate_player_dominant(self):
        """Player 3× enemy → 75% rate."""
        self.assertAlmostEqual(escape_rate(300, 100), 75)

    def test_escape_rate_enemy_dominant(self):
        """Enemy 3× player → 25% rate."""
        self.assertAlmostEqual(escape_rate(100, 300), 25)

    def test_escape_rate_rounds_to_5(self):
        """Rate rounds down to nearest 5%."""
        # 100/230 → 43.48% → rounds to 40%
        self.assertAlmostEqual(escape_rate(100, 130), 40)

    def test_escape_rate_both_zero_raises(self):
        with self.assertRaises(ValueError):
            escape_rate(0, 0)


class TestDodge(unittest.TestCase):
    """Formula 4: Dodge (post-July 2023)."""

    def test_zero_dodge(self):
        """Zero combined dodge → 0% evasion."""
        self.assertAlmostEqual(dodge_evasion(0), 0.0)

    def test_negative_dodge(self):
        """Negative dodge → 0% (clamped)."""
        self.assertAlmostEqual(dodge_evasion(-10), 0.0)

    def test_low_dodge(self):
        """10 combined → ~6.78% evasion."""
        # 100 * (1 - exp(10/100 * ln(0.2))) = 100 * (1 - 0.2^0.1)
        expected = 100 * (1 - 0.2 ** 0.1)
        self.assertAlmostEqual(dodge_evasion(10), expected, places=2)

    def test_mid_dodge(self):
        """50 combined → ~52.86% evasion."""
        expected = 100 * (1 - 0.2 ** 0.5)
        self.assertAlmostEqual(dodge_evasion(50), expected, places=2)

    def test_cap_dodge(self):
        """100.28+ combined → 80% evasion (capped)."""
        self.assertAlmostEqual(dodge_evasion(100.28), DODGE_CAP_PERCENT)
        self.assertAlmostEqual(dodge_evasion(200), DODGE_CAP_PERCENT)
        self.assertAlmostEqual(dodge_evasion(1000), DODGE_CAP_PERCENT)

    def test_dodge_monotonic_increase(self):
        """Evasion increases monotonically with dodge stat."""
        prev = 0
        for d in range(0, 101):
            val = dodge_evasion(d)
            self.assertGreaterEqual(val, prev)
            prev = val


class TestDamageReduction(unittest.TestCase):
    """Formula 5: Damage Reduction."""

    def test_zero_armor(self):
        """Zero armor → 0% damage reduction."""
        self.assertAlmostEqual(damage_reduction(0), 0.0)

    def test_negative_armor_raises(self):
        with self.assertRaises(ValueError):
            damage_reduction(-1)

    def test_low_armor(self):
        """100 armor → 50% reduction."""
        # 100 * (1 - 100/200) = 100 * 0.5 = 50
        self.assertAlmostEqual(damage_reduction(100), 50.0)

    def test_high_armor(self):
        """900 armor → 90% reduction."""
        # 100 * (1 - 100/1000) = 100 * 0.9 = 90
        self.assertAlmostEqual(damage_reduction(900), 90.0)

    def test_diminishing_returns(self):
        """Each 100 armor adds less reduction than the previous 100."""
        r1 = damage_reduction(100)
        r2 = damage_reduction(200) - r1
        r3 = damage_reduction(300) - damage_reduction(200)
        self.assertGreater(r1, r2)
        self.assertGreater(r2, r3)

    def test_effective_damage_zero_armor(self):
        """Zero armor → full damage."""
        self.assertAlmostEqual(effective_damage(100, 0), 100)

    def test_effective_damage_with_armor(self):
        """100 armor → 50% damage gets through."""
        self.assertAlmostEqual(effective_damage(100, 100), 50)

    def test_effective_damage_complement(self):
        """effective_damage + reduced = base (conservation)."""
        base = 200.0
        armor = 300.0
        reduced = base * (damage_reduction(armor) / 100)
        self.assertAlmostEqual(effective_damage(base, armor) + reduced, base)


class TestFireDamage(unittest.TestCase):
    """Formula 6: Fire Damage."""

    def test_reduced_no_sprinkler(self):
        """Zero sprinkler → no reduction (base damage)."""
        self.assertAlmostEqual(fire_damage_reduced(100, 0), 100)

    def test_reduced_with_sprinkler(self):
        """100 sprinkler stat → 50% damage (halved)."""
        # 100 / (1 + 100/100) = 100/2 = 50
        self.assertAlmostEqual(fire_damage_reduced(100, 100), 50)

    def test_reduced_high_sprinkler(self):
        """300 sprinkler stat → 25% damage."""
        # 100 / (1 + 300/100) = 100/4 = 25
        self.assertAlmostEqual(fire_damage_reduced(100, 300), 25)

    def test_crew_damage_no_resistance(self):
        """Zero fire resistance → full crew damage."""
        # 200/200 × 100/100 = 1.0
        self.assertAlmostEqual(fire_crew_damage(200, 0), 1.0)

    def test_crew_damage_full_resistance(self):
        """100 fire resistance → zero crew damage."""
        self.assertAlmostEqual(fire_crew_damage(200, 100), 0.0)

    def test_crew_damage_partial_resistance(self):
        """50 fire resistance → 50% crew damage."""
        # 200/200 × 50/100 = 0.5
        self.assertAlmostEqual(fire_crew_damage(200, 50), 0.5)

    def test_ap_damage(self):
        """AP damage = duration / 500."""
        self.assertAlmostEqual(fire_ap_damage(100), 0.2)
        self.assertAlmostEqual(fire_ap_damage(500), 1.0)
        self.assertAlmostEqual(fire_ap_damage(1000), 2.0)

    def test_negative_duration_raises(self):
        with self.assertRaises(ValueError):
            fire_crew_damage(-1, 0)
        with self.assertRaises(ValueError):
            fire_ap_damage(-1)


class TestCrewStatByLevel(unittest.TestCase):
    """Formula 7: Crew Stat by Level."""

    def test_level_1(self):
        """Level 1 returns the level-1 value exactly."""
        self.assertAlmostEqual(crew_stat_at_level(1, 10, 100, "ease_out"), 10)

    def test_level_40(self):
        """Level 40 returns the max value exactly."""
        self.assertAlmostEqual(crew_stat_at_level(40, 10, 100, "ease_out"), 100)

    def test_level_20_ease_out(self):
        """Level 20 with ease_out (exponent 0.5) — faster early growth."""
        # progress = 19/39 ≈ 0.487
        # value = 10 + 90 × 0.487^0.5 ≈ 10 + 90 × 0.698 ≈ 72.8
        progress = 19 / 39
        expected = 10 + 90 * (progress ** 0.5)
        self.assertAlmostEqual(crew_stat_at_level(20, 10, 100, "ease_out"), expected)

    def test_level_20_linear(self):
        """Level 20 with linear (exponent 1.0) — constant growth."""
        progress = 19 / 39
        expected = 10 + 90 * progress
        self.assertAlmostEqual(crew_stat_at_level(20, 10, 100, "linear"), expected)

    def test_level_20_ease_in(self):
        """Level 20 with ease_in (exponent 2.0) — slower early growth."""
        progress = 19 / 39
        expected = 10 + 90 * (progress ** 2.0)
        self.assertAlmostEqual(crew_stat_at_level(20, 10, 100, "ease_in"), expected)

    def test_ease_out_grows_faster_than_linear(self):
        """Ease_out values > linear > ease_in for mid-levels."""
        v_out = crew_stat_at_level(20, 10, 100, "ease_out")
        v_lin = crew_stat_at_level(20, 10, 100, "linear")
        v_in = crew_stat_at_level(20, 10, 100, "ease_in")
        self.assertGreater(v_out, v_lin)
        self.assertGreater(v_lin, v_in)

    def test_out_of_range_level_raises(self):
        with self.assertRaises(ValueError):
            crew_stat_at_level(0, 10, 100)
        with self.assertRaises(ValueError):
            crew_stat_at_level(41, 10, 100)


class TestGasDrawPrice(unittest.TestCase):
    """Formula 8: Gas Draw Price."""

    def test_zero_crew(self):
        """Zero 3-5★ crew → base price 500."""
        self.assertAlmostEqual(gas_draw_price(0), 500.0)

    def test_one_crew(self):
        """1 crew → 500 × 1.5 = 750."""
        self.assertAlmostEqual(gas_draw_price(1), 750.0)

    def test_two_crew(self):
        """2 crew → 500 × 1.5² = 1125."""
        self.assertAlmostEqual(gas_draw_price(2), 1125.0)

    def test_ten_crew(self):
        """10 crew → 500 × 1.5^10 = 28,820."""
        expected = 500 * (1.5 ** 10)
        self.assertAlmostEqual(gas_draw_price(10), expected)

    def test_cap(self):
        """Price caps at 2,000,000 gas."""
        big = gas_draw_price(100)
        self.assertLessEqual(big, GAS_DRAW_CAP)
        self.assertAlmostEqual(big, GAS_DRAW_CAP)

    def test_dna_alternative(self):
        """DNA count is used when higher than crew count."""
        # 100 DNA → 100//100=1 → same as 1 crew = 750
        self.assertAlmostEqual(gas_draw_price(0, dna_count=100), 750.0)

    def test_max_of_crew_and_dna(self):
        """The higher of crew count and DNA/100 is used."""
        # 5 crew, 800 DNA → max(5, 8) = 8 → 500 × 1.5^8
        expected = 500 * (1.5 ** 8)
        self.assertAlmostEqual(gas_draw_price(5, dna_count=800), expected)


class TestTrophy(unittest.TestCase):
    """Formula 9: Trophy Gain/Loss."""

    def test_equal_trophies(self):
        """Equal trophies → 20 trophies (base)."""
        self.assertEqual(trophy_gain(1000, 1000), 20)

    def test_winner_has_more(self):
        """Winner has more trophies → fewer gained."""
        # 20 × (500/2000)^4 = 20 × 0.015625 = 0.3125 → rounds to 0 → capped to 1
        self.assertEqual(trophy_gain(500, 2000), 1)

    def test_winner_has_fewer(self):
        """Winner has fewer trophies → more gained."""
        # 20 × (2000/500)^4 = 20 × 256 = 5120 → capped to 40
        self.assertEqual(trophy_gain(2000, 500), 40)

    def test_min_cap(self):
        """Trophy gain never goes below 1."""
        # Very low ratio → would be near 0 → capped to 1
        self.assertEqual(trophy_gain(1, 10000), 1)

    def test_max_cap(self):
        """Trophy gain never exceeds 40."""
        self.assertEqual(trophy_gain(10000, 1), 40)

    def test_zero_winner_raises(self):
        with self.assertRaises(ValueError):
            trophy_gain(100, 0)

    def test_negative_trophies_raises(self):
        with self.assertRaises(ValueError):
            trophy_gain(-1, 100)
        with self.assertRaises(ValueError):
            trophy_gain(100, -1)

    def test_moderate_difference(self):
        """Moderate difference → moderate gain."""
        # 20 × (1500/2000)^4 = 20 × 0.3164 = 6.328 → rounds to 6
        self.assertEqual(trophy_gain(1500, 2000), 6)


if __name__ == "__main__":
    unittest.main()
