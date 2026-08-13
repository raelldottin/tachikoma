from __future__ import annotations

"""Crew leveling logic — pure evaluation, no HTTP."""

import math
from dataclasses import dataclass
from enum import Enum
from datetime import datetime, timezone
from typing import Optional


class UpgradeBlockReason(Enum):
    """Reason a character cannot be upgraded."""
    MAX_LEVEL = "max_level"
    INSUFFICIENT_XP = "insufficient_xp"
    INSUFFICIENT_GAS = "insufficient_gas"
    NOT_AVAILABLE = "not_available"
    ELIGIBLE = "eligible"


@dataclass(frozen=True)
class UpgradeDecision:
    """Result of evaluating whether a character can be upgraded."""
    character_id: str
    character_name: str
    current_level: int
    next_level: int
    xp_available: int
    xp_required: int
    gas_available: int
    gas_required: int
    reason: UpgradeBlockReason
    is_legendary: bool

    @property
    def is_eligible(self) -> bool:
        return self.reason == UpgradeBlockReason.ELIGIBLE


# XP required for NEXT level (level -> XP needed for that level-up)
# Index = current level (1-based). Level 1 means "XP needed to go from 1 to 2".
STANDARD_XP_REQUIRED = {
    1: 90,
    2: 270,
    3: 450,
    4: 630,
    5: 810,
    6: 1020,
    7: 1230,
    8: 1440,
    9: 1650,
    10: 1860,
    11: 2130,
    12: 2400,
    13: 2670,
    14: 2940,
    15: 3210,
    16: 3540,
    17: 3870,
    18: 4200,
    19: 4530,
    20: 4860,
    21: 5220,
    22: 5580,
    23: 5940,
    24: 6300,
    25: 6660,
    26: 7050,
    27: 7440,
    28: 7830,
    29: 8220,
    30: 8610,
    31: 9030,
    32: 9450,
    33: 9870,
    34: 10290,
    35: 10710,
    36: 11160,
    37: 11610,
    38: 12060,
    39: 12510,
    # Level 40 is max - no upgrade possible
}

# Gas required for NEXT level (level -> gas needed for that level-up)
STANDARD_GAS_REQUIRED = {
    1: 0,
    2: 0,
    3: 17,
    4: 33,
    5: 65,
    6: 130,
    7: 325,
    8: 650,
    9: 1300,
    9: 3200,  # Note: level 9 appears twice in original, keeping last
    10: 6500,
    11: 9700,
    12: 13000,
    13: 19500,
    14: 26000,
    15: 35700,
    16: 43800,
    17: 52000,
    17: 61700,  # Note: level 17 appears twice, keeping last
    18: 71500,
    19: 84500,
    20: 104000,
    21: 117000,
    22: 130000,
    23: 156000,
    24: 175000,
    25: 201000,
    26: 227000,
    27: 253000,
    28: 279000,
    29: 312000,
    30: 351000,
    31: 383000,
    32: 422000,
    33: 468000,
    34: 507000,
    35: 552000,
    36: 604000,
    37: 650000,
    38: 715000,
    39: 0,  # Max level - no upgrade
}

# Legendary multiplier (3x for XP, separate gas table for legendary)
LEGENDARY_XP_MULTIPLIER = 3


# Legendary gas costs (from original implementation)
LEGENDARY_GAS_REQUIRED = {
    1: 0,
    2: 130000,
    3: 162500,
    4: 195000,
    5: 227500,
    5: 260000,  # Note: duplicate level 5 in original, keeping last
    6: 292500,
    7: 325000,
    8: 357500,
    9: 390000,
    10: 422500,
    11: 455000,
    11: 487500,  # Note: duplicate level 11, keeping last
    12: 520000,
    13: 552500,
    13: 585000,  # Note: duplicate level 13, keeping last
    14: 617500,
    15: 650000,
    16: 682500,
    17: 715000,
    17: 747500,  # Note: duplicate level 17, keeping last
    18: 780000,
    19: 812500,
    19: 845000,  # Note: duplicate level 19, keeping last
    20: 877500,
    21: 910000,
    21: 942000,  # Note: duplicate level 21, keeping last
    22: 975000,
    23: 1007500,
    24: 1040000,
    24: 1072500,  # Note: duplicate level 24, keeping last
    25: 1105000,
    26: 1137500,
    26: 1170000,  # Note: duplicate level 26, keeping last
    27: 1202500,
    28: 1235000,
    29: 1267500,
    30: 1300000,
    31: 1332500,
    31: 1365000,  # Note: duplicate level 31, keeping last
    32: 0,  # Max level
}


MAX_CHARACTER_LEVEL = 40


# =============================================================================
# Crew stat and training formulas (from Crew Planning and Training Guide by
# Raisinbunhk / Cinnamoroll). These are pure helper functions — no HTTP.
# =============================================================================

# Training Point (TP) caps by crew rarity (stars).
# Source: Crew Planning and Training Guide, Section 5.
TP_CAPS_BY_RARITY = {
    3: 70,   # 3* crew
    4: 80,   # 4* crew (exceptions below)
    5: 90,   # 5* crew
    6: 100,  # 6* crew (captains: 200)
    7: 110,  # 7* crew
}

# 4* crew with exceptional 100 TP (instead of standard 80).
EXCEPTIONAL_4STAR_TP = 100
EXCEPTIONAL_4STAR_NAMES = frozenset({"Mistycball", "Huge Hellaluya"})

# 6* captains have 200 TP (instead of standard 100).
CAPTAIN_TP = 200


def get_tp_cap(rarity: int, character_name: str = "", is_captain: bool = False) -> int:
    """Return the training-point cap for a crew member.

    Args:
        rarity: Crew star rating (3-7).
        character_name: Crew name — used to detect exceptional 4* crew.
        is_captain: Whether this crew is a captain (6* captains get 200 TP).

    Returns:
        Maximum training points for this crew.
    """
    if is_captain and rarity == 6:
        return CAPTAIN_TP
    if rarity == 4 and character_name in EXCEPTIONAL_4STAR_NAMES:
        return EXCEPTIONAL_4STAR_TP
    return TP_CAPS_BY_RARITY.get(rarity, 0)


def get_crew_level_cap(ship_level: int) -> int:
    """Return the crew level cap for a given ship level.

    Formula: level_cap = ship_level × 4, max 40.
    (Source: Crew Planning and Training Guide, Section 3.)
    """
    return min(ship_level * 4, MAX_CHARACTER_LEVEL)


def compute_final_stat(base_stat: float, training_points: int, equipment_bonus: float) -> float:
    """Compute a crew member's final stat value.

    Formula: final_stat = base_stat × (1 + TP/100) + equipment_bonus
    (Source: Crew Planning and Training Guide, Section 5.)

    HP is rounded UP to the nearest whole number (use math.ceil).
    Attack/Repairs are rounded UP to one decimal place.
    """
    return base_stat * (1 + training_points / 100) + equipment_bonus


def compute_final_ability(base_ability: float, training_points: int, equipment_bonus_pct: float) -> float:
    """Compute a crew member's final ability value.

    Formula: final_ability = base_ability × (1 + TP/100) × (1 + equipment_bonus%)
    (Source: Crew Planning and Training Guide, Section 5.)

    Both training points and equipment provide percentage boosts to ability.
    """
    return base_ability * (1 + training_points / 100) * (1 + equipment_bonus_pct / 100)


def compute_final_stamina(training_points: int, equipment_bonus: float) -> float:
    """Compute a crew member's final stamina value.

    Formula: final_stamina = TP + equipment_bonus
    (Source: Crew Planning and Training Guide, Section 5.)

    Stamina is unique — both TP and equipment add directly (not multiplicative).
    """
    return training_points + equipment_bonus


def parse_server_datetime(value: str) -> Optional[datetime]:
    """Parse server datetime string as UTC."""
    if not value:
        return None
    try:
        # Server format: "YYYY-MM-DDTHH:MM:SS"
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
        return parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def get_xp_required(level: int, is_legendary: bool) -> int:
    """Get XP required to upgrade from given level to next level."""
    if level >= MAX_CHARACTER_LEVEL:
        return 0
    xp = STANDARD_XP_REQUIRED.get(level, 0)
    if is_legendary:
        xp *= LEGENDARY_XP_MULTIPLIER
    return xp


def get_gas_required(level: int, is_legendary: bool) -> int:
    """Get gas required to upgrade from given level to next level."""
    if level >= MAX_CHARACTER_LEVEL:
        return 0
    if is_legendary:
        return LEGENDARY_GAS_REQUIRED.get(level, 0)
    return STANDARD_GAS_REQUIRED.get(level, 0)


def evaluate_upgrade(
    character: dict,
    character_design: dict,
    gas_available: int,
    now: datetime,
) -> UpgradeDecision:
    """
    Evaluate whether a character can be upgraded.
    
    Pure function — no HTTP, no mutations, no side effects.
    
    Args:
        character: Character dict from ListAllCharactersOfUser (with @CharacterId, @CharacterName, @Level, @Xp, @AvailableDate, @CharacterDesignId, @RoomId)
        character_design: CharacterDesign dict from ListAllCharacterDesigns2 (with @CharacterDesignId, @Rarity)
        gas_available: Current gas total on ship
        now: Current UTC time (timezone-aware)
    
    Returns:
        UpgradeDecision with eligibility and details
    """
    character_id = character.get("@CharacterId", "")
    character_name = character.get("@CharacterName", "Unknown")
    current_level = int(character.get("@Level", 0))
    xp_available = int(character.get("@Xp", 0))
    available_date_str = character.get("@AvailableDate", "")
    room_id = character.get("@RoomId", "0")
    design_id = character.get("@CharacterDesignId", "")
    
    rarity = character_design.get("@Rarity", "Standard")
    is_legendary = rarity == "Legendary"
    
    # Check max level
    if current_level >= MAX_CHARACTER_LEVEL:
        return UpgradeDecision(
            character_id=character_id,
            character_name=character_name,
            current_level=current_level,
            next_level=current_level,
            xp_available=xp_available,
            xp_required=0,
            gas_available=gas_available,
            gas_required=0,
            reason=UpgradeBlockReason.MAX_LEVEL,
            is_legendary=is_legendary,
        )
    
    # Check if assigned to a room (not in quarters)
    if room_id == "0":
        return UpgradeDecision(
            character_id=character_id,
            character_name=character_name,
            current_level=current_level,
            next_level=current_level + 1,
            xp_available=xp_available,
            xp_required=get_xp_required(current_level, is_legendary),
            gas_available=gas_available,
            gas_required=get_gas_required(current_level, is_legendary),
            reason=UpgradeBlockReason.NOT_AVAILABLE,
            is_legendary=is_legendary,
        )
    
    # Check XP
    xp_required = get_xp_required(current_level, is_legendary)
    if xp_available < xp_required:
        return UpgradeDecision(
            character_id=character_id,
            character_name=character_name,
            current_level=current_level,
            next_level=current_level + 1,
            xp_available=xp_available,
            xp_required=xp_required,
            gas_available=gas_available,
            gas_required=get_gas_required(current_level, is_legendary),
            reason=UpgradeBlockReason.INSUFFICIENT_XP,
            is_legendary=is_legendary,
        )
    
    # Check AvailableDate
    available_date = parse_server_datetime(available_date_str)
    if available_date and available_date > now:
        return UpgradeDecision(
            character_id=character_id,
            character_name=character_name,
            current_level=current_level,
            next_level=current_level + 1,
            xp_available=xp_available,
            xp_required=xp_required,
            gas_available=gas_available,
            gas_required=get_gas_required(current_level, is_legendary),
            reason=UpgradeBlockReason.NOT_AVAILABLE,
            is_legendary=is_legendary,
        )
    
    # Check gas
    gas_required = get_gas_required(current_level, is_legendary)
    if gas_available < gas_required:
        return UpgradeDecision(
            character_id=character_id,
            character_name=character_name,
            current_level=current_level,
            next_level=current_level + 1,
            xp_available=xp_available,
            xp_required=xp_required,
            gas_available=gas_available,
            gas_required=gas_required,
            reason=UpgradeBlockReason.INSUFFICIENT_GAS,
            is_legendary=is_legendary,
        )
    
    # All checks passed — eligible for upgrade
    return UpgradeDecision(
        character_id=character_id,
        character_name=character_name,
        current_level=current_level,
        next_level=current_level + 1,
        xp_available=xp_available,
        xp_required=xp_required,
        gas_available=gas_available,
        gas_required=gas_required,
        reason=UpgradeBlockReason.ELIGIBLE,
        is_legendary=is_legendary,
    )


def plan_upgrades(
    characters: list,
    character_designs: list,
    gas_total: int,
    now: Optional[datetime] = None,
    max_upgrades: int = 5,
) -> tuple[list[UpgradeDecision], int]:
    """
    Plan all upgrades before sending any requests.
    
    Args:
        characters: List of character dicts from ListAllCharactersOfUser
        character_designs: List of CharacterDesign dicts from ListAllCharacterDesigns2
        gas_total: Current gas total on ship
        now: Current UTC time (defaults to datetime.now(timezone.utc))
        max_upgrades: Maximum number of upgrades to plan
    
    Returns:
        Tuple of (eligible_decisions, remaining_gas_after_planned)
    """
    if now is None:
        now = datetime.now(timezone.utc)
    
    # Build design lookup
    designs_by_id = {
        design.get("@CharacterDesignId", ""): design
        for design in character_designs
        if design.get("@CharacterDesignId")
    }
    
    eligible = []
    remaining_gas = gas_total
    count = 0
    
    for character in characters:
        if count >= max_upgrades:
            break
        
        design_id = character.get("@CharacterDesignId", "")
        design = designs_by_id.get(design_id)
        if not design:
            continue
        
        decision = evaluate_upgrade(character, design, remaining_gas, now)
        
        if decision.reason == UpgradeBlockReason.ELIGIBLE:
            eligible.append(decision)
            remaining_gas -= decision.gas_required
            count += 1
    
    return eligible, remaining_gas