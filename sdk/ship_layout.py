"""Ship layout analysis — read-only evaluation, no HTTP, no mutations.

Evaluates a Pixel Starships ship layout from the ship data returned by
``getShipByUserId`` and the room designs from ``listAllDesigns4`` /
``listRoomDesigns2``.  The goal is to surface strategic weaknesses in the
current layout — uncovered rooms exposed to boarders, critical rooms far from
repairers, armor distribution gaps, etc. — so the human player can rearrange
rooms in the game client.

This module is pure-function: it takes already-fetched dicts and returns data
structures / strings.  It never touches the network.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Room-design helpers
# ---------------------------------------------------------------------------

# Known PSS room design categories (by RoomDesignId range or attribute):
#   - Armor blocks: 254, 255, 256, 265, 268 (high-count structural rooms)
#   - Weapons: rooms with @CanTargetShip or @SupportWeaponAttack
#   - Repair/Engineering: rooms with @CanRepair
#   - Reactors: rooms with @PowerGenerated
#   - Shield: rooms with @CanCharge
#   - Security/Boarding: rooms with @CanBoard or @CanAntiBoard
#   - Bedrooms/Quarters: rooms with @MaxPopulation or @CanHouseCharacter
#   - Lifts: rooms with @CanLift or elevator-type rooms
#   - Corridors: rooms with @CanWalk

# Common armor-block RoomDesignIds across many ship designs.
# These are 1×1 structural rooms that purely absorb damage.
_ARMOR_DESIGN_IDS = frozenset({
    "254", "255", "256",   # light/medium/heavy armor
    "265", "268",          # reinforced armor variants
    "257",                 # partial armor
})

# Reactor design IDs (reactors generate power — critical, should be protected)
_REACTOR_KEYWORDS = frozenset({"reactor", "generator", "power", "engine room"})

# Weapon design IDs / keywords
_WEAPON_KEYWORDS = frozenset({
    "weapon", "laser", "cannon", "missile", "plasma",
    "railgun", "photon", "burst", "ion", "minigun", "chaingun",
})

# Repair / engineering keywords
_REPAIR_KEYWORDS = frozenset({"repair", "engineering", "medbay", "medical"})

# Shield keywords
_SHIELD_KEYWORDS = frozenset({"shield", "deflector", "barrier"})

# Security / anti-boarder keywords
_SECURITY_KEYWORDS = frozenset({"security", "gas", "trap", "anti-boarder", "antiboard", "armory"})

# Corridor / lift keywords
_CORRIDOR_KEYWORDS = frozenset({"lift", "corridor", "hallway", "walkway", "passage"})


def _get_attr(design: dict, name: str, default=""):
    """Safely get an @-prefixed attribute from a dict (handles xmltodict)."""
    val = design.get(name, design.get("@" + name.lstrip("@"), default))
    return val if val is not None else default


def _keyword_match(name: str, keywords: frozenset) -> bool:
    """Match keywords as whole words, not substrings.

    e.g. 'ion' should NOT match 'security', but should match 'ion cannon'.
    """
    words = name.replace("-", " ").split()
    for kw in keywords:
        if kw in words:
            return True
    # Also check compound words (e.g., "anti-boarder" → "anti boarder")
    name_expanded = name.replace("-", " ")
    for kw in keywords:
        if f" {kw} " in f" {name_expanded} " or name_expanded.startswith(kw + " ") or name_expanded.endswith(" " + kw):
            return True
    return False


def _design_name(design: dict) -> str:
    return str(_get_attr(design, "@RoomName", "")).lower()


def classify_room(design: dict) -> str:
    """Classify a room by its design attributes.

    Returns one of:
        'armor', 'reactor', 'weapon', 'repair', 'shield',
        'security', 'training', 'lab', 'bedroom', 'corridor',
        'storage', 'other'
    """
    design_id = str(_get_attr(design, "@RoomDesignId", ""))
    name = _design_name(design)

    # Check by design ID first (most reliable for armor)
    if design_id in _ARMOR_DESIGN_IDS:
        return "armor"

    # Check by capacity — bedrooms house crew
    max_pop = _get_attr(design, "@MaxPopulation", "0")
    try:
        if int(max_pop) > 0:
            return "bedroom"
    except (ValueError, TypeError):
        pass

    # Check by keywords
    if _keyword_match(name, _WEAPON_KEYWORDS):
        return "weapon"
    if _keyword_match(name, _REPAIR_KEYWORDS):
        return "repair"
    if _keyword_match(name, _SHIELD_KEYWORDS):
        return "shield"
    if _keyword_match(name, _REACTOR_KEYWORDS):
        return "reactor"
    if _keyword_match(name, _SECURITY_KEYWORDS):
        return "security"
    if _keyword_match(name, _CORRIDOR_KEYWORDS):
        return "corridor"

    # Check by training/room attributes
    if _keyword_match(name, frozenset({"training", "gym", "academy"})):
        return "training"
    if _keyword_match(name, frozenset({"lab", "research"})):
        return "lab"
    if _keyword_match(name, frozenset({"storage", "mineral", "gas"})):
        return "storage"

    # Check if it's a 1×1 room with no special attributes — likely armor
    width = int(_get_attr(design, "@ColumnWidth", "1"))
    height = int(_get_attr(design, "@RowHeight", "1"))
    if width == 1 and height == 1 and design_id in _ARMOR_DESIGN_IDS:
        return "armor"

    return "other"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoomInfo:
    """A single room on the ship,parsed from GetShipByUserId."""
    room_id: str
    design_id: str
    row: int
    column: int
    status: str
    width: int          # from design
    height: int         # from design
    category: str       # classified category
    name: str           # design name
    hp: int = 0         # from design, if available
    power: int = 0      # from design, if available
    capacity: int = 0   # max population from design, if available
    upgrade_id: str = ""  # pending upgrade design ID


@dataclass
class LayoutAnalysis:
    """Result of analyzing a complete ship layout."""
    ship_name: str = ""
    ship_level: int = 0
    ship_design_id: str = ""
    grid_rows: tuple[int, int] = (0, 0)  # (min, max)
    grid_cols: tuple[int, int] = (0, 0)
    total_rooms: int = 0
    rooms_by_category: dict[str, int] = field(default_factory=dict)
    critical_rooms: list[RoomInfo] = field(default_factory=list)  # exposed critical rooms
    recommendations: list[str] = field(default_factory=list)
    defense_score: float = 0.0  # 0-100, higher = better
    armor_coverage: float = 0.0  # 0-100, percentage of critical rooms protected
    repair_proximity: float = 0.0  # 0-100, closeness of repairs to critical rooms
    weapon_coverage: float = 0.0  # 0-100, weapon distribution across ship
    power_balance: float = 0.0  # 0-100, power generated vs consumed


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def parse_rooms(ship_data: dict, room_designs: list[dict]) -> list[RoomInfo]:
    """Parse a ship's room data into RoomInfo objects.

    Args:
        ship_data: The parsed XML from GetShipByUserId
                    (e.g., self.shipByUserId["ShipService"]["GetShipByUserId"]["Ship"])
        room_designs: List of RoomDesign dicts from ListRoomDesigns2 or ListAllDesigns4

    Returns:
        List of RoomInfo, one per room on the ship.
    """
    # Build design lookup
    designs_by_id = {}
    for design in room_designs:
        did = _get_attr(design, "@RoomDesignId", "")
        if did:
            designs_by_id[did] = design

    # Extract rooms from ship data
    rooms = ship_data.get("Rooms", {}).get("Room", [])
    if isinstance(rooms, dict):
        rooms = [rooms]

    result = []
    for room in rooms:
        design_id = str(room.get("@RoomDesignId", ""))
        design = designs_by_id.get(design_id, {})

        width = int(_get_attr(design, "@ColumnWidth", "1"))
        height = int(_get_attr(design, "@RowHeight", "1"))
        hp = 0
        try:
            hp = int(_get_attr(design, "@RoomHp", "0"))
        except (ValueError, TypeError):
            pass
        power = 0
        try:
            power = int(_get_attr(design, "@PowerGenerated", "0"))
        except (ValueError, TypeError):
            pass
        capacity = 0
        try:
            capacity = int(_get_attr(design, "@MaxPopulation", "0"))
        except (ValueError, TypeError):
            pass

        category = classify_room(design) if design else "other"
        name = _get_attr(design, "@RoomName", f"DesignID:{design_id}")

        result.append(RoomInfo(
            room_id=str(room.get("@RoomId", "")),
            design_id=design_id,
            row=int(room.get("@Row", 0)),
            column=int(room.get("@Column", 0)),
            status=str(room.get("@RoomStatus", "")).lower(),
            width=width,
            height=height,
            category=category,
            name=name,
            hp=hp,
            power=power,
            capacity=capacity,
            upgrade_id=str(room.get("@UpgradeRoomDesignId", "")),
        ))

    return result


def _manhattan_distance(r1: RoomInfo, r2: RoomInfo) -> int:
    """Manhattan distance between room center points."""
    r1_r = r1.row + r1.height / 2
    r1_c = r1.column + r1.width / 2
    r2_r = r2.row + r2.height / 2
    r2_c = r2.column + r2.width / 2
    return int(abs(r1_r - r2_r) + abs(r1_c - r2_c))


def _rooms_overlap(a: RoomInfo, b: RoomInfo) -> bool:
    """Check if two rooms physically overlap on the grid."""
    a_rows = range(a.row, a.row + a.height)
    a_cols = range(a.column, a.column + a.width)
    b_rows = range(b.row, b.row + b.height)
    b_cols = range(b.column, b.column + b.width)
    return bool(set(a_rows) & set(b_rows) and set(a_cols) & set(b_cols))


def _is_adjacent(a: RoomInfo, b: RoomInfo) -> bool:
    """Check if two rooms are adjacent (touching, not overlapping)."""
    if _rooms_overlap(a, b):
        return False
    # Check if any tile of A is adjacent (1 step) to any tile of B
    a_tiles = {(r, c) for r in range(a.row, a.row + a.height) for c in range(a.column, a.column + a.width)}
    b_tiles = {(r, c) for r in range(b.row, b.row + b.height) for c in range(b.column, b.column + b.width)}
    for ar, ac in a_tiles:
        for dr, dc in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
            if (ar + dr, ac + dc) in b_tiles:
                return True
    return False


def analyze_layout(rooms: list[RoomInfo], ship_name: str = "", ship_level: int = 0,
                   ship_design_id: str = "") -> LayoutAnalysis:
    """Analyze a complete ship layout and provide strategic recommendations.

    This is a read-only analysis — it does not modify the ship. It evaluates:
    - Armor coverage around critical rooms (reactors, weapons, shields)
    - Repairer proximity to critical rooms
    - Weapon room distribution across the ship
    - Critical rooms exposed to boarder paths
    - Overall defense score

    Args:
        rooms: List of RoomInfo from parse_rooms()
        ship_name: Ship name for reporting
        ship_level: Ship level for context
        ship_design_id: Ship design ID for context

    Returns:
        LayoutAnalysis with scores and recommendations.
    """
    analysis = LayoutAnalysis(
        ship_name=ship_name,
        ship_level=ship_level,
        ship_design_id=ship_design_id,
        total_rooms=len(rooms),
    )

    if not rooms:
        analysis.recommendations.append("No rooms found in ship data.")
        return analysis

    # Grid bounds
    all_rows = [r.row for r in rooms]
    all_cols = [r.column for r in rooms]
    analysis.grid_rows = (min(all_rows), max(all_rows))
    analysis.grid_cols = (min(all_cols), max(all_cols))

    # Count by category
    from collections import Counter
    cat_counts = Counter(r.category for r in rooms)
    analysis.rooms_by_category = dict(cat_counts)

    # Identify critical rooms (reactors, weapons, shields, repair)
    critical_categories = {"reactor", "weapon", "shield", "repair"}
    critical_rooms = [r for r in rooms if r.category in critical_categories]
    armor_rooms = [r for r in rooms if r.category == "armor"]

    # ========================================
    # 1. Armor Coverage
    # ========================================
    protected_count = 0
    exposed_critical = []
    for crit in critical_rooms:
        is_protected = False
        for armor in armor_rooms:
            if _is_adjacent(crit, armor):
                is_protected = True
                break
        if is_protected:
            protected_count += 1
        else:
            exposed_critical.append(crit)

    if critical_rooms:
        analysis.armor_coverage = (protected_count / len(critical_rooms)) * 100
    else:
        analysis.armor_coverage = 100  # No critical rooms = nothing to protect

    analysis.critical_rooms = exposed_critical

    if exposed_critical:
        names = [r.name for r in exposed_critical[:5]]
        analysis.recommendations.append(
            f"Armor gap: {len(exposed_critical)} critical room(s) lack adjacent armor: "
            f"{', '.join(names)}. Place 1×1 armor blocks next to these rooms."
        )

    # ========================================
    # 2. Repair Proximity
    # ========================================
    repair_rooms = [r for r in rooms if r.category == "repair"]
    if repair_rooms and critical_rooms:
        total_dist = 0
        for crit in critical_rooms:
            min_dist = min(_manhattan_distance(crit, rep) for rep in repair_rooms)
            total_dist += min_dist
        avg_dist = total_dist / len(critical_rooms)
        # Score: closer = better. 0 dist = 100, 20+ dist = 0
        analysis.repair_proximity = max(0, 100 - (avg_dist / 20) * 100)

        if avg_dist > 10:
            analysis.recommendations.append(
                f"Repair proximity: average distance from repairers to critical rooms is "
                f"{avg_dist:.1f} tiles. Consider moving a repair room closer to your "
                f"reactors and weapons."
            )
    elif not repair_rooms:
        analysis.recommendations.append(
            "No repair rooms detected. Add at least one repair room (Medbay/Engineering) "
            "to keep crew healed during battle."
        )
        analysis.repair_proximity = 0

    # ========================================
    # 3. Weapon Distribution
    # ========================================
    weapon_rooms = [r for r in rooms if r.category == "weapon"]
    if weapon_rooms and all_cols:
        col_range = analysis.grid_cols[1] - analysis.grid_cols[0]
        if col_range > 0:
            # Distribute weapons across 4 quadrants of the ship
            mid_col = analysis.grid_cols[0] + col_range // 2
            left_weapons = sum(1 for w in weapon_rooms if w.column < mid_col)
            right_weapons = sum(1 for w in weapon_rooms if w.column >= mid_col)
            balance = min(left_weapons, right_weapons) / max(left_weapons, right_weapons) if max(left_weapons, right_weapons) > 0 else 1
            analysis.weapon_coverage = balance * 100

            if balance < 0.3:
                analysis.recommendations.append(
                    f"Weapon imbalance: {left_weapons} weapons on left, "
                    f"{right_weapons} on right. Distribute weapons more evenly "
                    f"to cover both sides of the ship."
                )
        else:
            analysis.weapon_coverage = 100
    elif not weapon_rooms:
        analysis.recommendations.append(
            "No weapon rooms detected. Ensure your ship has weapons installed."
        )
        analysis.weapon_coverage = 0

    # ========================================
    # 4. Power Balance
    # ========================================
    reactor_rooms = [r for r in rooms if r.category == "reactor"]
    total_power = sum(r.power for r in reactor_rooms)
    # Estimate power consumption: each non-armor room consumes some power
    consuming_rooms = [r for r in rooms if r.category not in ("armor", "corridor", "storage")]
    estimated_consumption = len(consuming_rooms) * 3  # rough estimate
    if total_power > 0:
        analysis.power_balance = min(100, (total_power / max(1, estimated_consumption)) * 50)
    elif reactor_rooms:
        analysis.power_balance = 50
    else:
        analysis.recommendations.append(
            "No reactor rooms detected. Reactors are essential for powering "
            "weapons and shields — ensure you have at least one."
        )
        analysis.power_balance = 0

    # ========================================
    # 5. Overall Defense Score
    # ========================================
    analysis.defense_score = (
        analysis.armor_coverage * 0.35 +
        analysis.repair_proximity * 0.25 +
        analysis.weapon_coverage * 0.20 +
        analysis.power_balance * 0.20
    )

    # ========================================
    # 6. Additional checks
    # ========================================
    # Check for rooms under construction
    constructing = [r for r in rooms if r.status == "constructing"]
    if constructing:
        analysis.recommendations.append(
            f"{len(constructing)} room(s) under construction. "
            f"Wait for completion before evaluating final layout."
        )

    # Check for pending upgrades
    upgrading = [r for r in rooms if r.upgrade_id and r.upgrade_id != "0"]
    if upgrading:
        analysis.recommendations.append(
            f"{len(upgrading)} room(s) have pending upgrades. "
            f"Complete upgrades to maximize room effectiveness."
        )

    # Check bedroom capacity
    bedrooms = [r for r in rooms if r.category == "bedroom"]
    total_capacity = sum(r.capacity for r in bedrooms)
    if total_capacity > 0 and total_capacity < 8:
        analysis.recommendations.append(
            f"Bedroom capacity is only {total_capacity} crew. "
            f"The Crew Guide recommends maximizing crew count (up to 24) "
            f"by buying every bux bedroom."
        )

    return analysis


def format_analysis_report(analysis: LayoutAnalysis) -> str:
    """Format a LayoutAnalysis into a human-readable report."""
    lines = []
    lines.append(f"=== Ship Layout Analysis: {analysis.ship_name} ===")
    lines.append(f"Ship Level: {analysis.ship_level} | Design: {analysis.ship_design_id}")
    lines.append(f"Grid: rows {analysis.grid_rows[0]}-{analysis.grid_rows[1]}, "
                 f"cols {analysis.grid_cols[0]}-{analysis.grid_cols[1]}")
    lines.append(f"Total Rooms: {analysis.total_rooms}")
    lines.append("")

    lines.append("--- Room Distribution ---")
    for cat, count in sorted(analysis.rooms_by_category.items(), key=lambda x: -x[1]):
        lines.append(f"  {cat}: {count}")
    lines.append("")

    lines.append("--- Defense Scores (0-100) ---")
    lines.append(f"  Armor Coverage:     {analysis.armor_coverage:.0f}/100")
    lines.append(f"  Repair Proximity:   {analysis.repair_proximity:.0f}/100")
    lines.append(f"  Weapon Coverage:    {analysis.weapon_coverage:.0f}/100")
    lines.append(f"  Power Balance:      {analysis.power_balance:.0f}/100")
    lines.append(f"  OVERALL DEFENSE:    {analysis.defense_score:.0f}/100")
    lines.append("")

    if analysis.critical_rooms:
        lines.append(f"--- Exposed Critical Rooms ({len(analysis.critical_rooms)}) ---")
        for r in analysis.critical_rooms[:10]:
            lines.append(f"  {r.name} (Row {r.row}, Col {r.column}) — no adjacent armor")
        if len(analysis.critical_rooms) > 10:
            lines.append(f"  ... and {len(analysis.critical_rooms) - 10} more")
        lines.append("")

    if analysis.recommendations:
        lines.append("--- Recommendations ---")
        for i, rec in enumerate(analysis.recommendations, 1):
            lines.append(f"  {i}. {rec}")
    else:
        lines.append("--- No recommendations: layout looks solid! ---")

    return "\n".join(lines)
