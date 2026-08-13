"""Pixel Starships game formulas — pure evaluation, no HTTP.

All formulas are sourced from the official Pixel Starships wiki:
    https://pixelstarships.fandom.com/wiki/Formulas

These are pure helper functions. They take numbers and return numbers.
They never touch the network, never read game state, and never mutate anything.

Formula #2 (Crew Stat Buff) is implemented in ``sdk/crew_leveling.py`` as
``compute_final_stat``, ``compute_final_ability``, and ``compute_final_stamina``
— it is intentionally NOT duplicated here.
"""

from __future__ import annotations

import math
from typing import Literal


# ===========================================================================
# 1. Room Reload  (Wiki: "Room Reload")
# ===========================================================================

def room_reload_boosted(base_reload: float, sum_crew_stat: float) -> float:
    """Compute the boosted reload time of a room after crew stats.

    Formula (wiki):
        Boosted = Base / ((100 + SumCrewStat) / 100)

    Higher crew stat → faster reload (lower number).

    Args:
        base_reload: The room's base reload time in seconds.
        sum_crew_stat: Sum of the relevant crew stat (e.g., Weapon for
            weapon rooms, Engine for engine rooms, Science for shield rooms).

    Returns:
        Boosted reload time in seconds (lower = faster).

    Raises:
        ValueError: If base_reload is negative.
    """
    if base_reload < 0:
        raise ValueError("base_reload must be non-negative")
    return base_reload / ((100.0 + sum_crew_stat) / 100.0)


def room_reload_powered(boosted_reload: float, max_power: float,
                        current_power: float) -> float:
    """Compute the actual reload time adjusted for power supply.

    Formula (wiki):
        Powered = Boosted × (MaxPower / CurrentPower)

    If the room is under-powered (current < max), reload takes longer.
    If the room is over-powered (current > max), reload is faster.

    Args:
        boosted_reload: Reload after crew stat boost (from room_reload_boosted).
        max_power: The room's max power requirement.
        current_power: The current power supplied to the room.

    Returns:
        Actual reload time in seconds.

    Raises:
        ValueError: If current_power is zero (would divide by zero).
    """
    if current_power == 0:
        raise ValueError("current_power must be non-zero (room has no power)")
    if current_power < 0 or max_power < 0:
        raise ValueError("power values must be non-negative")
    return boosted_reload * (max_power / current_power)


def room_reload(base_reload: float, sum_crew_stat: float,
                max_power: float, current_power: float) -> float:
    """Convenience: compute full room reload (boosted + powered) in one call."""
    boosted = room_reload_boosted(base_reload, sum_crew_stat)
    return room_reload_powered(boosted, max_power, current_power)


# ===========================================================================
# 3. Escape  (Wiki: "Escape")
# ===========================================================================

def escape_chance(mod: float, pilot_stat: float) -> float:
    """Compute the probability of successfully escaping from battle.

    Formula (wiki):
        Chance = Mod × (100 + PilotStat) / 100

    Args:
        mod: Base escape modifier from the room (e.g., 0.5 for 50%).
        pilot_stat: The pilot's relevant crew stat (Ability).

    Returns:
        Escape probability (0.0 to ~1.0+, may exceed 1.0 if over-capped).
    """
    return mod * (100.0 + pilot_stat) / 100.0


def escape_rate(player_escape: float, enemy_escape: float) -> float:
    """Compute the comparative escape rate vs an enemy.

    Formula (wiki):
        Rate = Player / (Player + Enemy) × 100

    Rounded down to 5% increments (per wiki: "rounds 5 down").

    Args:
        player_escape: The player's escape stat value.
        enemy_escape: The enemy's escape stat value.

    Returns:
        Escape rate as a percentage (0-100).

    Raises:
        ValueError: If both values are zero (indeterminate).
    """
    if player_escape + enemy_escape == 0:
        raise ValueError("player + enemy escape cannot both be zero")
    rate = player_escape / (player_escape + enemy_escape) * 100.0
    # Wiki says "rounds 5 down" — round to nearest 5%
    return math.floor(rate / 5) * 5


# ===========================================================================
# 4. Dodge  (Wiki: "Dodge") — post-July 2023 formula
# ===========================================================================

# Dodge cap: 80% evasion at 100.28% combined dodge stat
DODGE_CAP_PERCENT = 80.0
DODGE_CAP_COMBINED = 100.28

def dodge_evasion(combined_dodge: float) -> float:
    """Compute dodge evasion percentage from combined engine dodge stats.

    Formula (wiki, post-July 2023):
        Evasion = 100 × (1 - exp(Combined/100 × ln(0.2)))

    Capped at 80% evasion when combined dodge reaches 100.28%.

    Args:
        combined_dodge: Sum of all engine dodge stats on the ship.

    Returns:
        Evasion percentage (0.0 to 80.0).
    """
    if combined_dodge <= 0:
        return 0.0
    if combined_dodge >= DODGE_CAP_COMBINED:
        return DODGE_CAP_PERCENT
    evasion = 100.0 * (1.0 - math.exp(combined_dodge / 100.0 * math.log(0.2)))
    # Clamp to cap
    return min(max(evasion, 0.0), DODGE_CAP_PERCENT)


# ===========================================================================
# 5. Damage Reduction  (Wiki: "Damage Reduction / Armor")
# ===========================================================================

def damage_reduction(armor: float) -> float:
    """Compute the percentage of incoming damage reduced by armor.

    Formula (wiki):
        DamageReduction = 100 × (1 - 100 / (100 + Armor))

    Diminishing returns: each additional armor point provides less reduction.
    Crew damage ignores armor entirely.

    Args:
        armor: Total armor value (sum of all armor block HP on the ship).

    Returns:
        Damage reduction percentage (0.0 to <100.0).
    """
    if armor < 0:
        raise ValueError("armor must be non-negative")
    if armor == 0:
        return 0.0
    return 100.0 * (1.0 - 100.0 / (100.0 + armor))


def effective_damage(base_damage: float, armor: float) -> float:
    """Compute the effective ship damage after armor reduction.

    This is the complement of damage_reduction — the actual damage that
    gets through the armor.

    Args:
        base_damage: Incoming damage before armor.
        armor: Total armor value.

    Returns:
        Effective damage after armor reduction.
    """
    if armor < 0:
        raise ValueError("armor must be non-negative")
    if armor == 0:
        return base_damage
    return base_damage * (100.0 / (100.0 + armor))


# ===========================================================================
# 6. Fire Damage  (Wiki: "Fire Damage")
# ===========================================================================

def fire_damage_reduced(base_fire_damage: float, sprinkler_stat: float) -> float:
    """Compute fire damage after sprinkler reduction.

    Formula (wiki):
        Reduced = Base / (1 + Sprinkler/100)

    Args:
        base_fire_damage: Base fire damage per tick.
        sprinkler_stat: Sum of sprinkler crew stat.

    Returns:
        Reduced fire damage.
    """
    if sprinkler_stat < 0:
        raise ValueError("sprinkler_stat must be non-negative")
    return base_fire_damage / (1.0 + sprinkler_stat / 100.0)


def fire_crew_damage(duration: float, fire_resistance: float) -> float:
    """Compute crew damage from fire over a duration.

    Formula (wiki):
        CrewDmg = Duration/200 × (100 - FireRes)/100

    Args:
        duration: Fire duration in seconds.
        fire_resistance: Crew fire resistance percentage (0-100+).

    Returns:
        Total crew damage from the fire.
    """
    if duration < 0:
        raise ValueError("duration must be non-negative")
    return (duration / 200.0) * ((100.0 - fire_resistance) / 100.0)


def fire_ap_damage(duration: float) -> float:
    """Compute AP (ability point) damage from fire over a duration.

    Formula (wiki):
        APdmg = Duration / 500

    Args:
        duration: Fire duration in seconds.

    Returns:
        AP damage from the fire.
    """
    if duration < 0:
        raise ValueError("duration must be non-negative")
    return duration / 500.0


# ===========================================================================
# 7. Crew Stat by Level  (Wiki: "Crew Stat by Level")
# ===========================================================================

# Easing types matching the wiki:
#   "ease_out" → exponent 0.5 (fast initial growth, decelerating)
#   "linear"   → exponent 1.0 (constant growth)
#   "ease_in"  → exponent 2.0 (slow initial growth, accelerating)
EaseType = Literal["ease_out", "linear", "ease_in"]
_EASE_EXPONENTS = {"ease_out": 0.5, "linear": 1.0, "ease_in": 2.0}


def crew_stat_at_level(level: int, level_1_value: float, max_value: float,
                       ease: EaseType = "ease_out") -> float:
    """Compute a crew stat value at a given level.

    Formula (wiki):
        Current = Lvl1 + (Max - Lvl1) × ((Level - 1) / 39) ^ Ease

    The stat interpolates from the level-1 value to the max (level-40) value
    using an easing curve. Different stats use different easing:
        - HP/Attack/Repair: ease_out (fast early growth)
        - Stamina: linear
        - Ability: ease_in (slow early growth)

    Args:
        level: Crew level (1-40).
        level_1_value: The stat value at level 1.
        max_value: The stat value at level 40 (max).
        ease: Easing type — "ease_out" (0.5), "linear" (1.0), or "ease_in" (2.0).

    Returns:
        The stat value at the given level.

    Raises:
        ValueError: If level is out of range (1-40) or ease is invalid.
    """
    if not 1 <= level <= 40:
        raise ValueError(f"level must be 1-40, got {level}")
    if ease not in _EASE_EXPONENTS:
        raise ValueError(f"ease must be one of {list(_EASE_EXPONENTS)}")
    if level == 1:
        return level_1_value
    if level == 40:
        return max_value
    exp = _EASE_EXPONENTS[ease]
    progress = (level - 1) / 39.0
    return level_1_value + (max_value - level_1_value) * (progress ** exp)


# ===========================================================================
# 8. Gas Draw Price  (Wiki: "Gas Draw Price")
# ===========================================================================

GAS_DRAW_BASE = 500.0
GAS_DRAW_CAP = 2_000_000.0

def gas_draw_price(count_3_to_5_star: int, dna_count: int = 0) -> float:
    """Compute the gas draw price for Crew/Item draws.

    Formula (wiki):
        Base = 500 × 1.5 ^ MAX(3-5★count, Floor(DNA/100))
        Capped at 2,000,000 gas.

    The draw price scales with how many 3-5★ crews you own (or DNA collected).
    More rare crews → higher draw price.

    Args:
        count_3_to_5_star: Number of 3-5★ crew owned.
        dna_count: Crew DNA count (alternative scaling, divided by 100).

    Returns:
        Gas price for the next draw, capped at 2,000,000.
    """
    if count_3_to_5_star < 0:
        raise ValueError("count_3_to_5_star must be non-negative")
    if dna_count < 0:
        raise ValueError("dna_count must be non-negative")
    exponent = max(count_3_to_5_star, dna_count // 100)
    price = GAS_DRAW_BASE * (1.5 ** exponent)
    return min(price, GAS_DRAW_CAP)


# ===========================================================================
# 9. Trophy Gain/Loss  (Wiki: "Trophy")
# ===========================================================================

TROPHY_BASE = 20
TROPHY_MIN = 1
TROPHY_MAX = 40

def trophy_gain(loser_trophies: int, winner_trophies: int) -> int:
    """Compute trophies gained by the winner of a PvP battle.

    Formula (wiki):
        Trophies = 20 × (LoserTrophies / WinnerTrophies) ^ 4

    Capped to range 1-40.

    If the winner has far more trophies than the loser, the gain is small.
    If the winner has fewer trophies than the loser, the gain is large.

    Args:
        loser_trophies: Trophies of the losing player.
        winner_trophies: Trophies of the winning player.

    Returns:
        Integer trophies gained (1-40).

    Raises:
        ValueError: If winner_trophies is zero (would divide by zero).
    """
    if winner_trophies == 0:
        raise ValueError("winner_trophies must be non-zero")
    if loser_trophies < 0 or winner_trophies < 0:
        raise ValueError("trophy counts must be non-negative")
    ratio = loser_trophies / winner_trophies
    raw = TROPHY_BASE * (ratio ** 4)
    return max(TROPHY_MIN, min(TROPHY_MAX, round(raw)))
