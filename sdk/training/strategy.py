"""Training strategy as frozen data (no callables): roles, room types, tier bands, research gates, consumables.

This preserves the original ``Client.manageTraining`` strategy with its defects fixed:

- Roles name room *types* ("academy"/"gym"), not rooms. A room's type comes from an exact (stripped,
  case-sensitive) name lookup: Academy/Lunar College -> academy, GYM/Galaxy Gym -> gym. That removes the old
  substring matching ("Galaxy Gym Annex" used to count) and the "Galexy Gym" typo in the defenders' rooms.
- "Huge Hellaloya" is spelled "Huge Hellaluya", matching ``sdk.crew_leveling.EXCEPTIONAL_4STAR_NAMES``.
- Rushers and defenders use gym for both slots, so they keep training in place across the 71%/72% boundary.
- Research gates keep the original's courses: primary T1 needs Fitness or Education course 101, primary/secondary
  T2 course 201, primary/secondary T3 course 203; secondary T1 is ungated. A line's level is its highest completed
  course, read from live names such as "Fitness 203" (``sdk.training.parse.research_course``); the original's
  lookup could never match them. The original also computed a "202" gate that nothing used; it is dropped.
- TP caps are strategy data. ``rarity_stars`` maps rarity -> stars (Common 1 ... Legendary 7); ``tp_caps`` maps
  stars -> cap, ``exceptional_tp_caps`` maps (stars, exact name) -> cap and ``captain_tp_caps`` maps stars -> the
  captain cap. ``DEFAULT_STRATEGY`` holds frozen copies of ``sdk.crew_leveling``'s tables (TP_CAPS_BY_RARITY,
  EXCEPTIONAL_4STAR_NAMES/TP at 4 stars, CAPTAIN_TP at 6 stars), which stay the canonical source of the defaults
  (``crew_leveling.get_tp_cap``). Unknown rarities (e.g. "Standard") map to 0 stars and only the design's training
  capacity applies. A crew whose stars have a captain cap and whose design capacity reaches it is treated as a
  captain (a 6-star "Special" with capacity 200 gets 200 TP); other crew use the standard cap for their stars.
- Consumables are a fallback only. They are considered when nothing is startable for a crew that has a known role,
  sits in a training room and is under its TP cap; an out-of-stock item falls through to the Skip that would
  otherwise apply. ``DEFAULT_STRATEGY.consumables`` is empty until ``with_consumables`` attaches the item ids
  found by ``sdk.training.parse.parse_consumable_designs``. An item boosts the stat of the design the crew would
  train in its current band, else the crew's highest positive stat (``sdk.training.planner.target_stat``).
- Research gate values are tuples of (exact research name, minimum level >= 0) alternatives; () means ungated.
- A role slot lists its trainings by tier (tier 1 first); it may list more than the bands use, but every band's tier
  must index each role's training list for that slot (the single rule).

Deliberate changes from Client.manageTraining (details in ``sdk.training.planner``'s docstring): finished iff
training_end <= now (no per-tier elapsed-time gates); new fatigue gate; captain = capacity reaches the captain cap;
FinishTraining whenever a design id is set and end <= now; exact room-name matching; design_missing also covers zero
capacity; parse keeps its own envelope walker (no ``sdk.client`` import).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from sdk.crew_leveling import CAPTAIN_TP, EXCEPTIONAL_4STAR_NAMES, EXCEPTIONAL_4STAR_TP, TP_CAPS_BY_RARITY
from sdk.training.model import frozen_mapping

SLOT_PRIMARY = "primary"
SLOT_SECONDARY = "secondary"
ROOM_ACADEMY = "academy"
ROOM_GYM = "gym"
TIER_GREY = "Common"
TIER_HERO = "Hero"

STAT_CODES: Mapping[str, str] = frozen_mapping(
    dict(
        WeaponImprovement="WPN",
        ScienceImprovement="SCI",
        EngineImprovement="ENG",
        PilotImprovement="PLT",
        HpImprovement="HP",
        AttackImprovement="ATK",
        AbilityImprovement="ABL",
        StaminaImprovement="STA",
        RepairImprovement="RPR",
    )
)


@dataclass(frozen=True)
class RoleSlot:
    """A room type and the training names by tier, tier 1 first (any sequence; stored as a tuple).

    The list may be longer than the bands use; ``TrainingStrategy`` checks that every band's tier indexes it.
    """

    room_type: str
    trainings: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "trainings", tuple(self.trainings))


@dataclass(frozen=True)
class Role:
    """Exact character names (any iterable; stored as a frozenset) and the room/trainings for each slot."""

    name: str
    characters: frozenset[str]
    primary: RoleSlot
    secondary: RoleSlot

    def __post_init__(self) -> None:
        object.__setattr__(self, "characters", frozenset(self.characters))

    def slot(self, slot: str) -> RoleSlot:
        if slot == SLOT_PRIMARY:
            return self.primary
        if slot == SLOT_SECONDARY:
            return self.secondary
        raise ValueError(f"unknown slot: {slot!r}")


@dataclass(frozen=True)
class TierBand:
    slot: str
    tier: int
    low: int
    high: int


@dataclass(frozen=True)
class ConsumableRule:
    tier: str
    min_percent: int
    max_percent: int | None
    min_fatigue: int
    max_fatigue: int | None


@dataclass(frozen=True)
class TrainingStrategy:
    roles: tuple[Role, ...]
    room_types: Mapping[str, str]
    bands: tuple[TierBand, ...]
    complete_percent: int
    research_gates: Mapping[tuple[str, int], tuple[tuple[str, int], ...]]
    max_training_fatigue: int
    rarity_stars: Mapping[str, int]
    consumable_rules: tuple[ConsumableRule, ...]
    consumables: Mapping[tuple[str, str], int]
    tp_caps: Mapping[int, int]
    exceptional_tp_caps: Mapping[tuple[int, str], int]
    captain_tp_caps: Mapping[int, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", tuple(self.roles))
        object.__setattr__(self, "room_types", frozen_mapping(self.room_types))
        object.__setattr__(self, "bands", tuple(sorted(self.bands, key=lambda band: band.low)))
        object.__setattr__(self, "research_gates", frozen_mapping(self.research_gates))
        object.__setattr__(self, "rarity_stars", frozen_mapping(self.rarity_stars))
        object.__setattr__(self, "consumable_rules", tuple(self.consumable_rules))
        object.__setattr__(self, "consumables", frozen_mapping(self.consumables))
        object.__setattr__(self, "tp_caps", frozen_mapping(self.tp_caps))
        object.__setattr__(self, "exceptional_tp_caps", frozen_mapping(self.exceptional_tp_caps))
        object.__setattr__(self, "captain_tp_caps", frozen_mapping(self.captain_tp_caps))
        self._validate_bands()
        self._validate_research_gates()
        self._validate_roles()
        self._validate_band_tiers()
        self._validate_consumables()
        self._validate_tp_caps()

    def _validate_band_tiers(self) -> None:
        """Every band must name a known slot and a tier that indexes each role's training list for that slot."""
        for band in self.bands:
            if band.slot not in (SLOT_PRIMARY, SLOT_SECONDARY):
                raise ValueError(f"band names unknown slot {band.slot!r}")
            for role in self.roles:
                trainings = role.slot(band.slot).trainings
                if not 1 <= band.tier <= len(trainings):
                    raise ValueError(f"band {band.slot} T{band.tier} has no training in role {role.name!r}")

    def _validate_research_gates(self) -> None:
        """Each gate is a tuple of (exact research name, level >= 0) pairs; () is ungated."""
        for key, alternatives in self.research_gates.items():
            if not isinstance(alternatives, tuple) or not all(_gate_pair(pair) for pair in alternatives):
                raise ValueError(f"research gate {key!r} needs a tuple of (name, level >= 0) pairs: {alternatives!r}")

    def _validate_consumables(self) -> None:
        """Consumable keys name a stat code and a known tier; item ids are positive ints; rules can fire."""
        stat_codes = set(STAT_CODES.values())
        for (stat_code, tier), item_design_id in self.consumables.items():
            if stat_code not in stat_codes or tier not in self.rarity_stars:
                raise ValueError(f"invalid consumable key: {(stat_code, tier)!r}")
            if not _positive_int(item_design_id):
                raise ValueError(f"consumable {(stat_code, tier)!r} needs a positive int item id: {item_design_id!r}")
        for rule in self.consumable_rules:
            if rule.tier not in self.rarity_stars:
                raise ValueError(f"consumable rule names unknown tier {rule.tier!r}")
            for low, high, label in (
                (rule.min_percent, rule.max_percent, "percent"),
                (rule.min_fatigue, rule.max_fatigue, "fatigue"),
            ):
                if high is not None and low > high:
                    raise ValueError(f"consumable rule {rule.tier!r} has an empty {label} window: {low}..{high}")

    def _validate_tp_caps(self) -> None:
        """Caps are positive ints keyed by star count ints; exceptional caps by (stars, exact name)."""
        for label, table in (("tp_caps", self.tp_caps), ("captain_tp_caps", self.captain_tp_caps)):
            for stars, cap in table.items():
                if not _positive_int(stars) or not _positive_int(cap):
                    raise ValueError(f"{label} needs positive int stars -> cap, got {stars!r}: {cap!r}")
        for key, cap in self.exceptional_tp_caps.items():
            valid_key = isinstance(key, tuple) and len(key) == 2 and _positive_int(key[0]) and isinstance(key[1], str)
            if not valid_key or not _positive_int(cap):
                raise ValueError(f"exceptional_tp_caps needs (stars, name) -> positive int cap, got {key!r}: {cap!r}")

    def _validate_roles(self) -> None:
        owner: dict[str, str] = {}
        room_types = set(self.room_types.values())
        for role in self.roles:
            for slot in (role.primary, role.secondary):
                if slot.room_type not in room_types:
                    raise ValueError(f"role {role.name!r} names unknown room type {slot.room_type!r}")
            for character in role.characters:
                if character in owner:
                    raise ValueError(f"{character!r} is in roles {owner[character]!r} and {role.name!r}")
                owner[character] = role.name

    def _validate_bands(self) -> None:
        expected_low = 0
        for band in self.bands:
            if band.low != expected_low or band.high < band.low:
                raise ValueError(f"tier bands must tile 0..{self.complete_percent - 1} without gaps: {band}")
            expected_low = band.high + 1
        if expected_low != self.complete_percent:
            raise ValueError(f"tier bands must end at complete_percent - 1 ({self.complete_percent - 1})")
        for band in self.bands:
            if (band.slot, band.tier) not in self.research_gates:
                raise ValueError(f"band {band.slot} T{band.tier} has no research gate entry (use () for ungated)")
        if self.max_training_fatigue <= 0:
            raise ValueError("max_training_fatigue must be positive")


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _gate_pair(pair: object) -> bool:
    if not isinstance(pair, tuple) or len(pair) != 2:
        return False
    name, level = pair
    return isinstance(name, str) and isinstance(level, int) and not isinstance(level, bool) and level >= 0


RARITY_STARS: Mapping[str, int] = frozen_mapping(
    dict(Common=1, Elite=2, Unique=3, Epic=4, Hero=5, Special=6, Legendary=7)
)


def _gate(course: int) -> tuple[tuple[str, int], ...]:
    return (("Fitness", course), ("Education", course))


_GYM_STRENGTH = ("Bench Press", "Muscle Beach", "Olympic Weightlifting")

DEFAULT_ROLES: tuple[Role, ...] = (
    Role(
        "weapons",
        frozenset({"Galactic Succubus", "Galactic Snow Maiden", "Delish"}),
        RoleSlot(ROOM_ACADEMY, ("Read Expert Weapon Theory", "Weapons Summit", "Weapons PHD")),
        RoleSlot(ROOM_GYM, _GYM_STRENGTH),
    ),
    Role(
        "shields",
        frozenset({"Mistycball", "C.P.U.", "r2e"}),
        RoleSlot(ROOM_ACADEMY, ("Big Book of Science", "Scientific Summit", "Science PHD")),
        RoleSlot(ROOM_GYM, _GYM_STRENGTH),
    ),
    Role(
        "engines",
        frozenset({"The Conjoint Archon", "Galactic Sprite"}),
        RoleSlot(ROOM_GYM, _GYM_STRENGTH),
        RoleSlot(ROOM_ACADEMY, ("Study Expert Engineering Manual", "Engineering Summit", "Engineering PHD")),
    ),
    Role(
        "rushers",
        frozenset({"Huge Hellaluya", "Cyber Duck"}),
        RoleSlot(ROOM_GYM, ("Steam Yoga", "Crew vs Wild", "Space Marine")),
        RoleSlot(ROOM_GYM, _GYM_STRENGTH),
    ),
    Role(
        "defenders",
        frozenset(
            {
                "Admiral Serena",
                "Ancestral Spirit",
                "Green Ranger - Oliver",
                "Huntress",
                "Turkey Hero",
                "1st engineer Tully",
                "King Dong",
            }
        ),
        RoleSlot(ROOM_GYM, _GYM_STRENGTH),
        RoleSlot(ROOM_GYM, ("Kickbox", "BBJ", "Shaolin Tradition")),
    ),
    Role(
        "pilots",
        frozenset(),
        RoleSlot(ROOM_ACADEMY, ("Read Expert Pilot Handbook", "Pilot Summit", "Pilot Expert")),
        RoleSlot(ROOM_GYM, _GYM_STRENGTH),
    ),
)

DEFAULT_STRATEGY = TrainingStrategy(
    roles=DEFAULT_ROLES,
    room_types={"Academy": ROOM_ACADEMY, "Lunar College": ROOM_ACADEMY, "GYM": ROOM_GYM, "Galaxy Gym": ROOM_GYM},
    bands=(
        TierBand(SLOT_PRIMARY, 1, 0, 50),
        TierBand(SLOT_PRIMARY, 2, 51, 64),
        TierBand(SLOT_PRIMARY, 3, 65, 71),
        TierBand(SLOT_SECONDARY, 1, 72, 73),
        TierBand(SLOT_SECONDARY, 2, 74, 84),
        TierBand(SLOT_SECONDARY, 3, 85, 89),
    ),
    complete_percent=90,
    research_gates={
        (SLOT_PRIMARY, 1): _gate(101),
        (SLOT_PRIMARY, 2): _gate(201),
        (SLOT_PRIMARY, 3): _gate(203),
        (SLOT_SECONDARY, 1): (),
        (SLOT_SECONDARY, 2): _gate(201),
        (SLOT_SECONDARY, 3): _gate(203),
    },
    max_training_fatigue=100,
    rarity_stars=RARITY_STARS,
    consumable_rules=(
        ConsumableRule(TIER_GREY, min_percent=0, max_percent=29, min_fatigue=0, max_fatigue=0),
        ConsumableRule(TIER_HERO, min_percent=90, max_percent=None, min_fatigue=100, max_fatigue=None),
    ),
    consumables={},
    tp_caps=TP_CAPS_BY_RARITY,
    exceptional_tp_caps={(4, name): EXCEPTIONAL_4STAR_TP for name in sorted(EXCEPTIONAL_4STAR_NAMES)},
    captain_tp_caps={6: CAPTAIN_TP},
)


def with_consumables(strategy: TrainingStrategy, consumables: Mapping[tuple[str, str], int]) -> TrainingStrategy:
    """A copy of ``strategy`` using ``consumables`` ((stat_code, tier) -> item design id); validation re-runs."""
    return replace(strategy, consumables=consumables)
