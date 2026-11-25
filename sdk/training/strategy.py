"""Training strategy as frozen data (no callables): roles, room types, tier bands, research gates, consumables.

This preserves the original ``Client.manageTraining`` strategy with its defects fixed:

- Roles name room *types* ("academy"/"gym"), not rooms. A room's type comes from an exact (stripped,
  case-sensitive) name lookup: Academy/Lunar College -> academy, GYM/Galaxy Gym -> gym. That removes the old
  substring matching ("Galaxy Gym Annex" used to count) and the "Galexy Gym" typo in the defenders' rooms.
- "Huge Hellaloya" is spelled "Huge Hellaluya", matching ``sdk.crew_leveling.EXCEPTIONAL_4STAR_NAMES``.
- Rushers and defenders use gym for both slots, so they keep training in place across the 71%/72% boundary.
- Research gates: the original passed 101/201/203 as *minimum research levels* to a lookup that could never match.
  They read as tier labels, so the hundreds digit is taken as the required level (an inference): primary T1 needs
  Fitness or Education >= 1, primary/secondary T2 >= 2, primary/secondary T3 >= 3, secondary T1 is ungated.
  The original also computed a "202" gate that nothing used; it is dropped.
- Rarity -> stars for ``crew_leveling.get_tp_cap``: Common 1 ... Legendary 7. Unknown rarities (e.g. "Standard")
  map to 0 and only the design's training capacity applies. A "Special" crew whose design capacity reaches
  ``crew_leveling.CAPTAIN_TP`` is treated as a captain (200 TP); other "Special" crew use the standard 6-star cap.
- Consumables are a fallback only. They are considered when nothing is startable for a crew that has a known role,
  sits in a training room and is under its TP cap; an out-of-stock item falls through to the Skip that would
  otherwise apply. ``DEFAULT_STRATEGY.consumables`` is empty until ``with_consumables`` attaches the item ids
  found by ``sdk.training.parse.parse_consumable_designs``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

SLOT_PRIMARY = "primary"
SLOT_SECONDARY = "secondary"
ROOM_ACADEMY = "academy"
ROOM_GYM = "gym"
TIER_GREY = "Common"
TIER_HERO = "Hero"

STAT_CODES: Mapping[str, str] = MappingProxyType(
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


def _frozen(mapping: Mapping) -> Mapping:
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True)
class RoleSlot:
    room_type: str
    trainings: tuple[str, str, str]


@dataclass(frozen=True)
class Role:
    name: str
    characters: frozenset[str]
    primary: RoleSlot
    secondary: RoleSlot

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

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", tuple(self.roles))
        object.__setattr__(self, "room_types", _frozen(self.room_types))
        object.__setattr__(self, "bands", tuple(self.bands))
        object.__setattr__(self, "research_gates", _frozen(self.research_gates))
        object.__setattr__(self, "rarity_stars", _frozen(self.rarity_stars))
        object.__setattr__(self, "consumable_rules", tuple(self.consumable_rules))
        object.__setattr__(self, "consumables", _frozen(self.consumables))
        self._validate_bands()
        self._validate_roles()
        self._validate_band_tiers()
        self._validate_consumables()

    def _validate_band_tiers(self) -> None:
        """Every band must name a known slot and a tier that indexes each role's training list for that slot."""
        for band in self.bands:
            if band.slot not in (SLOT_PRIMARY, SLOT_SECONDARY):
                raise ValueError(f"band names unknown slot {band.slot!r}")
            for role in self.roles:
                trainings = role.slot(band.slot).trainings
                if not 1 <= band.tier <= len(trainings):
                    raise ValueError(f"band {band.slot} T{band.tier} has no training in role {role.name!r}")

    def _validate_consumables(self) -> None:
        stat_codes = set(STAT_CODES.values())
        for stat_code, tier in self.consumables:
            if stat_code not in stat_codes or tier not in self.rarity_stars:
                raise ValueError(f"invalid consumable key: {(stat_code, tier)!r}")

    def _validate_roles(self) -> None:
        owner: dict[str, str] = {}
        room_types = set(self.room_types.values())
        for role in self.roles:
            for slot in (role.primary, role.secondary):
                if slot.room_type not in room_types:
                    raise ValueError(f"role {role.name!r} names unknown room type {slot.room_type!r}")
                if len(slot.trainings) != 3:
                    raise ValueError(f"role {role.name!r} must name exactly 3 trainings per slot: {slot.trainings!r}")
            for character in role.characters:
                if character in owner:
                    raise ValueError(f"{character!r} is in roles {owner[character]!r} and {role.name!r}")
                owner[character] = role.name

    def _validate_bands(self) -> None:
        ordered = sorted(self.bands, key=lambda band: band.low)
        expected_low = 0
        for band in ordered:
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


RARITY_STARS: Mapping[str, int] = MappingProxyType(
    dict(Common=1, Elite=2, Unique=3, Epic=4, Hero=5, Special=6, Legendary=7)
)


def _gate(level: int) -> tuple[tuple[str, int], ...]:
    return (("Fitness", level), ("Education", level))


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
        (SLOT_PRIMARY, 1): _gate(1),
        (SLOT_PRIMARY, 2): _gate(2),
        (SLOT_PRIMARY, 3): _gate(3),
        (SLOT_SECONDARY, 1): (),
        (SLOT_SECONDARY, 2): _gate(2),
        (SLOT_SECONDARY, 3): _gate(3),
    },
    max_training_fatigue=100,
    rarity_stars=RARITY_STARS,
    consumable_rules=(
        ConsumableRule(TIER_GREY, min_percent=0, max_percent=29, min_fatigue=0, max_fatigue=0),
        ConsumableRule(TIER_HERO, min_percent=90, max_percent=None, min_fatigue=100, max_fatigue=None),
    ),
    consumables={},
)


def with_consumables(strategy: TrainingStrategy, consumables: Mapping[tuple[str, str], int]) -> TrainingStrategy:
    """A copy of ``strategy`` using ``consumables`` ((stat_code, tier) -> item design id); validation re-runs."""
    return replace(strategy, consumables=consumables)
