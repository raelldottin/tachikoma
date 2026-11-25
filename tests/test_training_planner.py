"""Tests for the pure crew-training planner (sdk.training) — no network, no client."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import math
import os
import random
import typing
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from unittest import mock

import sdk.crew_leveling as crew_leveling
from sdk.crew_leveling import CAPTAIN_TP, EXCEPTIONAL_4STAR_NAMES, EXCEPTIONAL_4STAR_TP, TP_CAPS_BY_RARITY, get_tp_cap
from sdk.training import planner as planner_module
from sdk.training.model import (
    CANONICAL_STATS,
    SKIP_CODES,
    CrewState,
    FinishTraining,
    Skip,
    StartTraining,
    TrainingContext,
    TrainingDesign,
    UseConsumable,
)
from sdk.training.planner import (
    band_for,
    design_research_ok,
    effective_cap,
    highest_stat,
    percent,
    pick_consumable,
    pick_design,
    plan_training,
    replan_after_finish,
    research_ok,
    role_for,
    room_type_for,
)
from sdk.training.strategy import (
    DEFAULT_STRATEGY,
    STAT_CODES,
    ConsumableRule,
    Role,
    RoleSlot,
    TierBand,
    with_consumables,
)

PACKAGE_DIR = Path(os.path.abspath(__file__)).parents[1] / "sdk" / "training"
UTC = timezone.utc
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def mk_crew(
    cid: int = 1,
    name: str = "Delish",
    *,
    total: int = 0,
    capacity: int = 100,
    rarity: str = "Legendary",
    room_name: str | None = "Academy",
    room_id: int | None = None,
    fatigue: int = 0,
    training_design_id: int | None = None,
    training_end: datetime | None = None,
    stat: str = "HpImprovement",
    stats: dict[str, int] | None = None,
) -> CrewState:
    """A test crew whose ``total`` training points all sit in ``stat``; pass ``stats`` instead for a mix."""
    if stats is not None and (total or stat != "HpImprovement"):
        raise ValueError("mk_crew: pass either total/stat or stats, not both")
    return CrewState(
        character_id=cid,
        name=name,
        design_id=500 + cid,
        rarity=rarity,
        level=10,
        room_id=room_id,
        room_name=room_name,
        stats={stat: total} if stats is None else stats,
        training_capacity=capacity,
        fatigue=fatigue,
        training_design_id=training_design_id,
        training_end=training_end,
    )


class TestModel(unittest.TestCase):
    def test_skip_codes_closed_enum(self) -> None:
        self.assertEqual(
            SKIP_CODES,
            frozenset(
                {
                    "not_in_training_room",
                    "training_in_progress",
                    "research_missing",
                    "tp_cap_reached",
                    "design_missing",
                    "unknown_role",
                    "fatigued",
                    "complete",
                }
            ),
        )
        self.assertIsInstance(SKIP_CODES, frozenset)

    def test_skip_rejects_unknown_code(self) -> None:
        with self.assertRaises(ValueError):
            Skip(1, "bogus", "")
        self.assertEqual(Skip(1, "complete", "x").reason_code, "complete")

    def test_crew_state_rejects_at_prefixed_stat_keys(self) -> None:
        # Regression: the original read '@HpImprovement' and looked it up without '@'.
        # The dedicated '@' message is pinned so this check stays observable on its own: the generic unknown-key
        # check would also reject these keys, but without telling the caller to strip the XML prefix.
        for bad in ({"@HpImprovement": 1}, {"HpImprovement": 1, "@WeaponImprovement": 2}, {"@Weapon": 1}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, r"must not carry the XML '@' prefix"):
                mk_crew(stats=bad)
        self.assertEqual(mk_crew(stats={"HpImprovement": 1}).stats["HpImprovement"], 1)

    def test_crew_state_rejects_non_canonical_stat_keys(self) -> None:
        # Regression: a mis-keyed stat ("Weapon") was accepted and then ignored by the TP total, so the crew read 0%.
        for bad in ({"Weapon": 5}, {"weaponimprovement": 5}, {"HpImprovement": 1, "Hp": 2}, {"": 1}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, r"must be among CANONICAL_STATS"):
                mk_crew(stats=bad)
        every = dict.fromkeys(CANONICAL_STATS, 1)
        self.assertEqual(dict(mk_crew(stats=every).stats), every)

    def test_mk_crew_rejects_total_together_with_stats(self) -> None:
        with self.assertRaises(ValueError):
            mk_crew(total=5, stats={"HpImprovement": 5})
        with self.assertRaises(ValueError):
            mk_crew(stat="WeaponImprovement", stats={"WeaponImprovement": 5})
        self.assertEqual(dict(mk_crew(total=5, stat="WeaponImprovement").stats), {"WeaponImprovement": 5})

    def test_crew_state_rejects_naive_training_end(self) -> None:
        with self.assertRaises(ValueError):
            mk_crew(training_design_id=1, training_end=datetime(2026, 1, 1, 12, 0))
        self.assertEqual(mk_crew(training_design_id=1, training_end=NOW).training_end, NOW)

    def test_context_rejects_naive_now(self) -> None:
        with self.assertRaises(ValueError):
            TrainingContext(datetime(2026, 1, 1), (), (), {}, {}, {})
        self.assertEqual(TrainingContext(NOW, (), (), {}, {}, {}).now, NOW)

    def test_rejects_tzinfo_whose_utcoffset_is_none(self) -> None:
        # A tzinfo can be attached yet report no offset; such a datetime is still naive for comparisons.
        class NoOffset(tzinfo):
            def utcoffset(self, dt: datetime | None) -> timedelta | None:
                return None

            def dst(self, dt: datetime | None) -> timedelta | None:
                return None

            def tzname(self, dt: datetime | None) -> str | None:
                return None

        floating = datetime(2026, 1, 1, 12, 0, tzinfo=NoOffset())
        self.assertIsNotNone(floating.tzinfo)
        with self.assertRaisesRegex(ValueError, "training_end must be a timezone-aware datetime"):
            mk_crew(training_design_id=1, training_end=floating)
        with self.assertRaisesRegex(ValueError, "now must be a timezone-aware datetime"):
            TrainingContext(floating, (), (), {}, {}, {})

    def test_context_freezes_inputs(self) -> None:
        stats = {"HpImprovement": 3}
        crew = mk_crew(stats=stats)
        rooms = {1: "Academy"}
        levels = {"Fitness": 1}
        items = {9: 2}
        ctx = TrainingContext(NOW, [crew], [], rooms, levels, items)  # type: ignore[arg-type]
        stats["HpImprovement"] = 99
        rooms[1] = "Bridge"
        levels["Fitness"] = 0
        items[9] = 0
        self.assertIsInstance(ctx.crew, tuple)
        self.assertIsInstance(ctx.designs, tuple)
        self.assertEqual(ctx.crew[0].stats["HpImprovement"], 3)
        self.assertEqual(ctx.rooms[1], "Academy")
        self.assertEqual(ctx.research_levels["Fitness"], 1)
        self.assertEqual(ctx.items[9], 2)
        for mapping in (ctx.rooms, ctx.research_levels, ctx.items, ctx.crew[0].stats):
            with self.assertRaises(TypeError):
                mapping[1] = 0  # type: ignore[index]

    def test_decisions_and_designs_are_frozen(self) -> None:
        objects = (
            FinishTraining(1, "r"),
            StartTraining(1, 2, "r"),
            UseConsumable(1, 2, "r"),
            Skip(1, "complete", "d"),
            TrainingDesign(1, "Bench Press", 1, None, timedelta(hours=1), None, None),
            mk_crew(),
            TrainingContext(NOW, (), (), {}, {}, {}),
        )
        for obj in objects:
            with self.subTest(type(obj).__name__), self.assertRaises(dataclasses.FrozenInstanceError):
                obj.character_id = 5  # type: ignore[misc,union-attr]


class TestPercent(unittest.TestCase):
    def test_percent_zero(self) -> None:
        self.assertEqual(percent(0, 100), 0)

    def test_percent_exact_integer_ceiling(self) -> None:
        cases = (((1, 100), 1), ((50, 100), 50), ((100, 200), 50), ((101, 200), 51), ((1, 3), 34), ((2, 3), 67))
        cases += (((143, 200), 72),)
        for (total, capacity), expected in cases:
            with self.subTest(total=total, capacity=capacity):
                self.assertEqual(percent(total, capacity), expected)

    def test_percent_float_ceil_regression(self) -> None:
        # The original computed math.ceil(total / capacity * 100); float error rounds these up by one.
        for total, capacity, expected in ((7, 100, 7), (14, 100, 14), (28, 100, 28), (55, 100, 55), (56, 100, 56)):
            with self.subTest(total=total, capacity=capacity):
                self.assertEqual(percent(total, capacity), expected)
                self.assertNotEqual(math.ceil(total / capacity * 100), expected)  # proves the row is a defect case
        self.assertEqual(percent(11, 20), 55)
        self.assertNotEqual(math.ceil(11 / 20 * 100), 55)

    def test_percent_rejects_nonpositive_capacity(self) -> None:
        for capacity in (0, -1):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                percent(10, capacity)


class TestBands(unittest.TestCase):
    def test_band_for_table(self) -> None:
        cases = {
            -1: ("primary", 1),
            0: ("primary", 1),
            50: ("primary", 1),
            51: ("primary", 2),
            64: ("primary", 2),
            65: ("primary", 3),
            71: ("primary", 3),
            72: ("secondary", 1),
            73: ("secondary", 1),
            74: ("secondary", 2),
            84: ("secondary", 2),
            85: ("secondary", 3),
            89: ("secondary", 3),
            90: None,
            150: None,
        }
        for pct, expected in cases.items():
            with self.subTest(pct=pct):
                band = band_for(pct, DEFAULT_STRATEGY)
                self.assertEqual(None if band is None else (band.slot, band.tier), expected)

    def test_default_bands_contiguous_0_to_complete(self) -> None:
        ordered = sorted(DEFAULT_STRATEGY.bands, key=lambda band: band.low)
        self.assertEqual(ordered[0].low, 0)
        self.assertEqual(ordered[-1].high, DEFAULT_STRATEGY.complete_percent - 1)
        for before, after in zip(ordered, ordered[1:]):
            self.assertEqual(after.low, before.high + 1)
        self.assertEqual(DEFAULT_STRATEGY.complete_percent, 90)

    def test_strategy_rejects_gapped_or_overlapping_bands(self) -> None:
        bands = list(DEFAULT_STRATEGY.bands)
        gapped = [*bands[:1], TierBand("primary", 2, 52, 64), *bands[2:]]
        overlapping = [*bands[:1], TierBand("primary", 2, 50, 64), *bands[2:]]
        short = bands[:-1]
        inverted = [*bands[:1], TierBand("primary", 2, 64, 51), *bands[2:]]
        for label, candidate in (("gap", gapped), ("overlap", overlapping), ("short", short), ("inverted", inverted)):
            with self.subTest(label), self.assertRaises(ValueError):
                dataclasses.replace(DEFAULT_STRATEGY, bands=tuple(candidate))

    def test_strategy_rejects_band_tier_or_slot_outside_role_slots(self) -> None:
        # Regression: an unchecked tier picked trainings[-1] (tier 0) or raised IndexError (tier 4) inside the
        # supposedly total planner; an unknown slot raised from Role.slot at plan time.
        bands = list(DEFAULT_STRATEGY.bands)
        gates = dict(DEFAULT_STRATEGY.research_gates)
        for label, bad in (
            ("tier0", TierBand("primary", 0, 0, 50)),
            ("tier4", TierBand("primary", 4, 0, 50)),
            ("slot", TierBand("tertiary", 1, 0, 50)),
        ):
            with self.subTest(label), self.assertRaises(ValueError):
                dataclasses.replace(
                    DEFAULT_STRATEGY,
                    bands=(bad, *bands[1:]),
                    research_gates={**gates, (bad.slot, bad.tier): ()},
                )
        short_slot = RoleSlot("academy", ("A", "B"))
        short_role = Role("short", frozenset({"Shorty"}), short_slot, RoleSlot("gym", ("C", "D", "E")))
        with self.subTest("two-training slot"), self.assertRaises(ValueError):
            dataclasses.replace(DEFAULT_STRATEGY, roles=(*DEFAULT_STRATEGY.roles, short_role))

    def test_strategy_sorts_bands_once(self) -> None:
        shuffled = dataclasses.replace(DEFAULT_STRATEGY, bands=tuple(reversed(DEFAULT_STRATEGY.bands)))
        self.assertEqual([band.low for band in shuffled.bands], [0, 51, 65, 72, 74, 85])
        self.assertEqual(shuffled, DEFAULT_STRATEGY)
        for pct, expected in ((0, ("primary", 1)), (72, ("secondary", 1)), (89, ("secondary", 3))):
            with self.subTest(pct=pct):
                found = band_for(pct, shuffled)
                self.assertEqual((found.slot, found.tier) if found else None, expected)

    def test_role_and_slot_normalise_their_sequences(self) -> None:
        # Regression: a set of characters made the Role unhashable and a list of trainings stayed mutable.
        slot = RoleSlot("gym", ["A", "B", "C"])  # type: ignore[arg-type]
        role = Role("x", {"Zed"}, slot, RoleSlot("gym", ("D", "E", "F")))  # type: ignore[arg-type]
        self.assertEqual(slot.trainings, ("A", "B", "C"))
        self.assertIsInstance(slot.trainings, tuple)
        self.assertEqual(role.characters, frozenset({"Zed"}))
        self.assertIsInstance(role.characters, frozenset)
        same = Role("x", frozenset({"Zed"}), RoleSlot("gym", ("A", "B", "C")), role.secondary)
        self.assertEqual(hash(role), hash(same))
        self.assertEqual(typing.get_type_hints(RoleSlot)["trainings"], tuple[str, ...])


class TestRooms(unittest.TestCase):
    def test_room_type_none_empty(self) -> None:
        for name in (None, "", "  "):
            with self.subTest(name=name):
                self.assertIsNone(room_type_for(name, DEFAULT_STRATEGY))

    def test_room_type_canonical_names(self) -> None:
        cases = {
            "Academy": "academy",
            "Lunar College": "academy",
            "GYM": "gym",
            "Galaxy Gym": "gym",
            " Galaxy Gym ": "gym",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(room_type_for(name, DEFAULT_STRATEGY), expected)

    def test_room_type_substring_false_positives(self) -> None:
        # Regression: the original matched room names by case-sensitive substring.
        names = ("Galaxy Gym Annex", "Big GYM", "gym", "Gym", "academy", "Galexy Gym", "Lunar College 2", "Bridge")
        for name in names:
            with self.subTest(name=name):
                self.assertIsNone(room_type_for(name, DEFAULT_STRATEGY))


ROLE_CHARACTERS = {
    "weapons": ("Galactic Succubus", "Galactic Snow Maiden", "Delish"),
    "shields": ("Mistycball", "C.P.U.", "r2e"),
    "engines": ("The Conjoint Archon", "Galactic Sprite"),
    "rushers": ("Huge Hellaluya", "Cyber Duck"),
    "defenders": (
        "Admiral Serena",
        "Ancestral Spirit",
        "Green Ranger - Oliver",
        "Huntress",
        "Turkey Hero",
        "1st engineer Tully",
        "King Dong",
    ),
    "pilots": (),
}
GYM_STRENGTH = ("Bench Press", "Muscle Beach", "Olympic Weightlifting")
# role -> (primary room type, primary trainings, secondary room type, secondary trainings), as literals.
EXPECTED_ROLE_TABLE = {
    "weapons": ("academy", ("Read Expert Weapon Theory", "Weapons Summit", "Weapons PHD"), "gym", GYM_STRENGTH),
    "shields": ("academy", ("Big Book of Science", "Scientific Summit", "Science PHD"), "gym", GYM_STRENGTH),
    "engines": (
        "gym",
        GYM_STRENGTH,
        "academy",
        ("Study Expert Engineering Manual", "Engineering Summit", "Engineering PHD"),
    ),
    "rushers": ("gym", ("Steam Yoga", "Crew vs Wild", "Space Marine"), "gym", GYM_STRENGTH),
    "defenders": ("gym", GYM_STRENGTH, "gym", ("Kickbox", "BBJ", "Shaolin Tradition")),
    "pilots": ("academy", ("Read Expert Pilot Handbook", "Pilot Summit", "Pilot Expert"), "gym", GYM_STRENGTH),
}


class TestRoles(unittest.TestCase):
    def test_role_for_known_names(self) -> None:
        for role_name, characters in ROLE_CHARACTERS.items():
            for character in characters:
                with self.subTest(character=character):
                    role = role_for(character, DEFAULT_STRATEGY)
                    self.assertIsNotNone(role)
                    assert role is not None
                    self.assertEqual(role.name, role_name)
        rusher = role_for("Huge Hellaluya", DEFAULT_STRATEGY)
        self.assertEqual(rusher.name if rusher else None, "rushers")
        # Spelling regression: the original table had this typo.
        self.assertIsNone(role_for("Huge Hellaloya", DEFAULT_STRATEGY))

    def test_role_for_is_exact_case_and_whitespace_sensitive(self) -> None:
        # Role names match the server's character names exactly; a case or whitespace variant is another name.
        for variant in ("huge hellaluya", "HUGE HELLALUYA", "Huge hellaluya", "delish", "DELISH", " Delish", "Delish "):
            with self.subTest(variant=variant):
                self.assertIsNone(role_for(variant, DEFAULT_STRATEGY))
        self.assertEqual(summary(plan_one(mk_crew(name="huge hellaluya"))), [("skip", 1, "unknown_role")])

    def test_role_for_unknown_and_pilots_empty(self) -> None:
        self.assertIsNone(role_for("", DEFAULT_STRATEGY))
        self.assertIsNone(role_for("Nobody", DEFAULT_STRATEGY))
        pilots = [role for role in DEFAULT_STRATEGY.roles if role.name == "pilots"]
        self.assertEqual(len(pilots), 1)
        self.assertEqual(pilots[0].characters, frozenset())

    def test_strategy_rejects_character_in_two_roles(self) -> None:
        # Regression: the original role loop had no break, so the last matching role silently won.
        clash = Role("clash", frozenset({"Delish"}), RoleSlot("gym", ("A", "B", "C")), RoleSlot("gym", ("D", "E", "F")))
        with self.assertRaises(ValueError):
            dataclasses.replace(DEFAULT_STRATEGY, roles=(*DEFAULT_STRATEGY.roles, clash))

    def test_default_role_table_golden(self) -> None:
        actual = {
            role.name: (
                role.primary.room_type,
                role.primary.trainings,
                role.secondary.room_type,
                role.secondary.trainings,
            )
            for role in DEFAULT_STRATEGY.roles
        }
        self.assertEqual(actual, EXPECTED_ROLE_TABLE)
        self.assertEqual(
            {role.name: role.characters for role in DEFAULT_STRATEGY.roles},
            {name: frozenset(chars) for name, chars in ROLE_CHARACTERS.items()},
        )


class TestCaps(unittest.TestCase):
    def test_effective_cap_rarity_without_table_uses_capacity(self) -> None:
        for rarity in ("Standard", "", "Common", "Elite"):
            with self.subTest(rarity=rarity):
                self.assertEqual(effective_cap(mk_crew(rarity=rarity, capacity=100), DEFAULT_STRATEGY), 100)
                self.assertEqual(effective_cap(mk_crew(rarity=rarity, capacity=250), DEFAULT_STRATEGY), 250)

    def test_effective_cap_unknown_rarity_ignores_one_star_cap(self) -> None:
        # Unknown rarity means 0 stars (capacity only), never the lowest known tier. The default tables have no
        # 1-star entry, so only a strategy with one can tell 0 stars from 1 star.
        custom = dataclasses.replace(DEFAULT_STRATEGY, tp_caps={1: 10}, exceptional_tp_caps={}, captain_tp_caps={})
        self.assertEqual(effective_cap(mk_crew(rarity="Common", capacity=100), custom), 10)
        for rarity in ("", "Mythic", "common"):
            with self.subTest(rarity=rarity):
                self.assertEqual(effective_cap(mk_crew(rarity=rarity, capacity=100), custom), 100)
        self.assertEqual(
            summary(plan_one(mk_crew(rarity="Mythic", total=20), strategy=custom)),
            [("start", 1, "Read Expert Weapon Theory")],
        )
        self.assertEqual(
            summary(plan_one(mk_crew(rarity="Common", total=20), strategy=custom)),
            [("skip", 1, "tp_cap_reached")],
        )

    def test_effective_cap_limited_by_tp_table(self) -> None:
        cases = (
            ("Delish", "Unique", 100, 70),
            ("Delish", "Epic", 100, 80),
            ("Mistycball", "Epic", 100, 100),
            ("Huge Hellaluya", "Epic", 100, 100),
            ("Delish", "Hero", 100, 90),
            ("Delish", "Special", 100, 100),
            ("Delish", "Legendary", 120, 110),
            ("Delish", "Legendary", 60, 60),
        )
        for name, rarity, capacity, expected in cases:
            with self.subTest(name=name, rarity=rarity, capacity=capacity):
                crew = mk_crew(name=name, rarity=rarity, capacity=capacity)
                self.assertEqual(effective_cap(crew, DEFAULT_STRATEGY), expected)

    def test_effective_cap_special_captain_capacity_not_clamped(self) -> None:
        # Regression: a 6-star captain (design capacity 200) was clamped to the standard 6-star cap of 100, so it
        # stopped at 50% and never reached its secondary training.
        self.assertEqual(effective_cap(mk_crew(rarity="Special", capacity=200), DEFAULT_STRATEGY), 200)
        self.assertEqual(effective_cap(mk_crew(rarity="Special", capacity=150), DEFAULT_STRATEGY), 100)
        decisions = plan_one(mk_crew(rarity="Special", capacity=200, total=120, room_name="Academy"))
        self.assertEqual(summary(decisions), [("start", 1, "Weapons Summit")])

    def test_default_tp_tables_are_crew_leveling_copies(self) -> None:
        # crew_leveling stays the canonical source of the default caps; the strategy holds frozen copies of them.
        self.assertEqual(dict(DEFAULT_STRATEGY.tp_caps), TP_CAPS_BY_RARITY)
        expected_exceptional = {(4, name): EXCEPTIONAL_4STAR_TP for name in EXCEPTIONAL_4STAR_NAMES}
        self.assertEqual(dict(DEFAULT_STRATEGY.exceptional_tp_caps), expected_exceptional)
        self.assertEqual(dict(DEFAULT_STRATEGY.captain_tp_caps), {6: CAPTAIN_TP})
        # With a capacity that never binds, the strategy-driven cap is exactly get_tp_cap's table value.
        for rarity, stars in DEFAULT_STRATEGY.rarity_stars.items():
            for name in ("Delish", "Mistycball", "Huge Hellaluya"):
                with self.subTest(rarity=rarity, name=name):
                    crew = mk_crew(name=name, rarity=rarity, capacity=1000)
                    table = get_tp_cap(stars, name, is_captain=stars == 6)
                    self.assertEqual(effective_cap(crew, DEFAULT_STRATEGY), table or 1000)

    def test_effective_cap_reads_only_the_strategy(self) -> None:
        # Regression: the cap came from crew_leveling's module-level dicts, which a strategy could not override.
        custom = dataclasses.replace(
            DEFAULT_STRATEGY,
            tp_caps={**DEFAULT_STRATEGY.tp_caps, 7: 50},
            exceptional_tp_caps={(7, "Delish"): 60},
            captain_tp_caps={},
        )
        self.assertEqual(effective_cap(mk_crew(name="Delish", rarity="Legendary", capacity=120), custom), 60)
        self.assertEqual(effective_cap(mk_crew(name="Cyber Duck", rarity="Legendary", capacity=120), custom), 50)
        self.assertEqual(effective_cap(mk_crew(name="Mistycball", rarity="Epic", capacity=120), custom), 80)
        self.assertEqual(effective_cap(mk_crew(rarity="Special", capacity=200), custom), 100)
        legendary = mk_crew(rarity="Legendary", capacity=120)
        with mock.patch.dict(crew_leveling.TP_CAPS_BY_RARITY, {7: 1}):
            self.assertEqual(effective_cap(legendary, DEFAULT_STRATEGY), 110)


def band(slot: str, tier: int) -> TierBand:
    return next(b for b in DEFAULT_STRATEGY.bands if (b.slot, b.tier) == (slot, tier))


class TestResearch(unittest.TestCase):
    def test_research_ok_secondary_t1_ungated(self) -> None:
        self.assertTrue(research_ok(band("secondary", 1), {}, DEFAULT_STRATEGY))

    def test_research_gate_table(self) -> None:
        required = {("primary", 1): 1, ("primary", 2): 2, ("primary", 3): 3, ("secondary", 2): 2, ("secondary", 3): 3}
        for (slot, tier), level in required.items():
            with self.subTest(slot=slot, tier=tier):
                self.assertFalse(research_ok(band(slot, tier), {"Fitness": level - 1}, DEFAULT_STRATEGY))
                both_short = {"Fitness": level - 1, "Education": level - 1}
                self.assertFalse(research_ok(band(slot, tier), both_short, DEFAULT_STRATEGY))
                self.assertTrue(research_ok(band(slot, tier), {"Fitness": level}, DEFAULT_STRATEGY))
                self.assertTrue(research_ok(band(slot, tier), {"Education": level}, DEFAULT_STRATEGY))

    def test_research_exact_key_regression(self) -> None:
        # Regression: the original matched research by name prefix (startswith of the first word).
        near_misses = {"Fitness Lv3": 3, "fitness": 3, "FitnessX": 3, "Fit": 3}
        self.assertFalse(research_ok(band("primary", 1), near_misses, DEFAULT_STRATEGY))
        self.assertTrue(research_ok(band("primary", 1), {"Education": 1}, DEFAULT_STRATEGY))


def mk_design(
    design_id: int,
    name: str,
    *,
    tier: int = 1,
    required_research: str | None = None,
    required_room_type: str | None = None,
) -> TrainingDesign:
    return TrainingDesign(design_id, name, tier, None, timedelta(hours=1), required_research, required_room_type)


class TestDesigns(unittest.TestCase):
    def test_design_research_ok(self) -> None:
        self.assertTrue(design_research_ok(mk_design(1, "Bench Press"), {}))
        gated = mk_design(1, "Bench Press", required_research="Advanced Training")
        self.assertFalse(design_research_ok(gated, {}))
        self.assertFalse(design_research_ok(gated, {"Advanced Training": 0}))
        self.assertTrue(design_research_ok(gated, {"Advanced Training": 1}))
        self.assertTrue(design_research_ok(gated, {"Advanced Training": 4}))

    def test_pick_design_absent(self) -> None:
        self.assertIsNone(pick_design("Bench Press", "gym", ()))
        self.assertIsNone(pick_design("Bench Press", "gym", (mk_design(1, "Muscle Beach"),)))

    def test_pick_design_exact_name_only(self) -> None:
        designs = (mk_design(1, "Bench Press 2"), mk_design(2, "bench press"), mk_design(3, " Bench Press"))
        self.assertIsNone(pick_design("Bench Press", "gym", designs))

    def test_pick_design_lowest_id_any_order(self) -> None:
        # Regression: the original kept overwriting the id, so the last duplicate won.
        designs = (mk_design(7, "Bench Press"), mk_design(3, "Bench Press"), mk_design(5, "Bench Press"))
        for ordering in (designs, tuple(reversed(designs))):
            with self.subTest(order=[d.id for d in ordering]):
                picked = pick_design("Bench Press", "gym", ordering)
                self.assertEqual(picked.id if picked else None, 3)

    def test_pick_design_required_room_type(self) -> None:
        academy_only = mk_design(1, "Bench Press", required_room_type="academy")
        any_room = mk_design(2, "Bench Press")
        gym_only = mk_design(3, "Bench Press", required_room_type="gym")
        self.assertIsNone(pick_design("Bench Press", "gym", (academy_only,)))
        picked = pick_design("Bench Press", "gym", (academy_only, any_room))
        self.assertEqual(picked.id if picked else None, 2)
        picked = pick_design("Bench Press", "gym", (academy_only, gym_only))
        self.assertEqual(picked.id if picked else None, 3)


class TestStats(unittest.TestCase):
    def test_highest_stat_ties_canonical(self) -> None:
        self.assertEqual(highest_stat({}), "HpImprovement")
        self.assertEqual(highest_stat({"StaminaImprovement": 0}), "HpImprovement")
        self.assertEqual(highest_stat({"RepairImprovement": 4, "WeaponImprovement": 4}), "RepairImprovement")
        self.assertEqual(highest_stat({"WeaponImprovement": 4, "RepairImprovement": 4}), "RepairImprovement")
        self.assertEqual(highest_stat({"HpImprovement": 1, "StaminaImprovement": 9}), "StaminaImprovement")

    def test_stat_codes_table_repair_is_rpr(self) -> None:
        # Regression: the original mapped RepairImprovement to STA and keyed stats with '@'.
        expected = {
            "WeaponImprovement": "WPN",
            "ScienceImprovement": "SCI",
            "EngineImprovement": "ENG",
            "PilotImprovement": "PLT",
            "HpImprovement": "HP",
            "AttackImprovement": "ATK",
            "AbilityImprovement": "ABL",
            "StaminaImprovement": "STA",
            "RepairImprovement": "RPR",
        }
        self.assertEqual(dict(STAT_CODES), expected)
        self.assertFalse(any(key.startswith("@") for key in STAT_CODES))
        self.assertEqual(set(STAT_CODES), set(CANONICAL_STATS))


def mk_ctx(*crew: CrewState, designs=(), rooms=None, research=None, items=None, now: datetime = NOW) -> TrainingContext:
    return TrainingContext(now, tuple(crew), tuple(designs), rooms or {}, research or {}, items or {})


class TestPickConsumable(unittest.TestCase):
    def test_pick_consumable_stock(self) -> None:
        crew = mk_crew(stats={"WeaponImprovement": 10}, fatigue=0)
        configured = dataclasses.replace(DEFAULT_STRATEGY, consumables={("WPN", "Common"): 9001})
        self.assertIsNone(pick_consumable(crew, 10, {9001: 5}, DEFAULT_STRATEGY))
        self.assertIsNone(pick_consumable(crew, 10, {9001: 0}, configured))
        self.assertIsNone(pick_consumable(crew, 10, {}, configured))
        decision = pick_consumable(crew, 10, {9001: 1}, configured)
        self.assertIsInstance(decision, UseConsumable)
        assert decision is not None
        self.assertEqual((decision.character_id, decision.item_design_id), (1, 9001))


TRAINING_NAMES = tuple(
    sorted(
        {name for role in DEFAULT_STRATEGY.roles for slot in (role.primary, role.secondary) for name in slot.trainings}
    )
)
DESIGN_IDS = {name: 100 + index for index, name in enumerate(TRAINING_NAMES)}
DESIGN_NAMES = {design_id: name for name, design_id in DESIGN_IDS.items()}
ALL_DESIGNS = tuple(mk_design(design_id, name) for name, design_id in DESIGN_IDS.items())
FULL_RESEARCH = {"Fitness": 3}


def summary(decisions) -> list[tuple]:
    rows: list[tuple] = []
    for decision in decisions:
        if isinstance(decision, FinishTraining):
            rows.append(("finish", decision.character_id))
        elif isinstance(decision, StartTraining):
            rows.append(("start", decision.character_id, DESIGN_NAMES.get(decision.training_design_id)))
        elif isinstance(decision, UseConsumable):
            rows.append(("use", decision.character_id, decision.item_design_id))
        else:
            rows.append(("skip", decision.character_id, decision.reason_code))
    return rows


def plan_one(crew: CrewState, *, strategy=DEFAULT_STRATEGY, designs=ALL_DESIGNS, research=None, **ctx_kwargs):
    ctx = mk_ctx(crew, designs=designs, research=FULL_RESEARCH if research is None else research, **ctx_kwargs)
    return plan_training(ctx, strategy)


class TestPlannerBasics(unittest.TestCase):
    def test_empty_crew_empty_tuple(self) -> None:
        self.assertEqual(plan_training(mk_ctx(designs=ALL_DESIGNS)), ())

    def test_single_weapons_crew_starts_primary_t1(self) -> None:
        crew = mk_crew(name="Delish", room_name="Academy", total=0)
        decisions = plan_one(crew, research={"Fitness": 1})
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].__class__, StartTraining)
        self.assertEqual(summary(decisions), [("start", 1, "Read Expert Weapon Theory")])
        self.assertEqual(decisions[0].training_design_id, DESIGN_IDS["Read Expert Weapon Theory"])


class TestSkipCodes(unittest.TestCase):
    def assertSkip(self, decisions, code: str, detail: str | None = None) -> Skip:
        self.assertEqual(summary(decisions), [("skip", decisions[0].character_id, code)])
        skip = decisions[0]
        assert isinstance(skip, Skip)
        if detail is not None:
            self.assertEqual(skip.detail, detail)
        return skip

    def test_skip_unknown_role(self) -> None:
        self.assertSkip(plan_one(mk_crew(name="Nobody")), "unknown_role")

    def test_skip_not_in_training_room_no_room(self) -> None:
        self.assertSkip(plan_one(mk_crew(room_name=None, room_id=None)), "not_in_training_room")
        self.assertSkip(plan_one(mk_crew(room_name="Bridge")), "not_in_training_room")

    def test_skip_design_missing_no_capacity(self) -> None:
        for capacity in (0, -5):
            with self.subTest(capacity=capacity):
                self.assertSkip(plan_one(mk_crew(capacity=capacity)), "design_missing", "no training capacity")

    def test_skip_tp_cap_reached(self) -> None:
        # Unique: TP table cap 70 at capacity 100.
        detail = "70 TP >= cap 70 TP (70% of capacity 100)"
        self.assertSkip(plan_one(mk_crew(rarity="Unique", total=70)), "tp_cap_reached", detail)
        self.assertSkip(plan_one(mk_crew(rarity="Unique", total=75)), "tp_cap_reached")
        self.assertSkip(plan_one(mk_crew(rarity="Legendary", capacity=60, total=60)), "tp_cap_reached")

    def test_skip_tp_cap_detail_uses_point_units(self) -> None:
        # Regression: the detail compared a percent with a TP cap ("35% >= cap 70"). It now states points vs points.
        skip = self.assertSkip(plan_one(mk_crew(rarity="Unique", capacity=200, total=70)), "tp_cap_reached")
        self.assertEqual(skip.detail, "70 TP >= cap 70 TP (35% of capacity 200)")
        self.assertNotIn("35% >= cap", skip.detail)

    def test_skip_training_in_progress(self) -> None:
        crew = mk_crew(training_design_id=DESIGN_IDS["Weapons Summit"], training_end=NOW + timedelta(seconds=1))
        self.assertSkip(plan_one(crew), "training_in_progress")

    def test_skip_complete(self) -> None:
        self.assertSkip(plan_one(mk_crew(rarity="Legendary", capacity=100, total=90, room_name="GYM")), "complete")
        self.assertSkip(plan_one(mk_crew(rarity="Legendary", capacity=100, total=95, room_name="Academy")), "complete")

    def test_skip_not_in_training_room_wrong_slot(self) -> None:
        self.assertSkip(plan_one(mk_crew(total=72, room_name="Academy")), "not_in_training_room", "move to gym")
        # Formerly silent: a primary-band crew sitting in its secondary room.
        self.assertSkip(plan_one(mk_crew(total=40, room_name="GYM")), "not_in_training_room", "move to academy")

    def test_skip_research_missing(self) -> None:
        self.assertSkip(plan_one(mk_crew(total=0), research={}), "research_missing")
        self.assertSkip(plan_one(mk_crew(total=60), research={"Fitness": 1}), "research_missing")

    def test_skip_fatigued(self) -> None:
        for fatigue in (100, 150):
            with self.subTest(fatigue=fatigue):
                self.assertSkip(plan_one(mk_crew(fatigue=fatigue)), "fatigued")
        self.assertEqual(summary(plan_one(mk_crew(fatigue=99))), [("start", 1, "Read Expert Weapon Theory")])

    def test_skip_fatigued_honours_strategy_limit(self) -> None:
        # The fatigue limit is strategy data, not a literal 100: a lower limit must move the boundary with it.
        strategy = dataclasses.replace(DEFAULT_STRATEGY, max_training_fatigue=50)
        self.assertEqual(
            summary(plan_one(mk_crew(fatigue=49), strategy=strategy)), [("start", 1, "Read Expert Weapon Theory")]
        )
        for fatigue in (50, 99):
            with self.subTest(fatigue=fatigue):
                self.assertSkip(plan_one(mk_crew(fatigue=fatigue), strategy=strategy), "fatigued", f"fatigue {fatigue} >= 50")

    def test_skip_design_missing_no_design(self) -> None:
        # Regression: the original sent AddTraining with trainingDesignId=None when no design matched.
        designs = tuple(d for d in ALL_DESIGNS if d.name != "Read Expert Weapon Theory")
        decisions = plan_one(mk_crew(), designs=designs)
        skip = self.assertSkip(decisions, "design_missing")
        self.assertIn("Read Expert Weapon Theory", skip.detail)
        self.assertFalse(any(isinstance(d, StartTraining) for d in decisions))
        self.assertSkip(plan_one(mk_crew(), designs=()), "design_missing")


BOUNDARY_PERCENTS = (0, 50, 51, 64, 65, 71, 72, 73, 74, 84, 85, 89, 90)
ROOM_NAME_FOR_TYPE = {"academy": "Academy", "gym": "GYM"}


def role_named(name: str) -> Role:
    return next(role for role in DEFAULT_STRATEGY.roles if role.name == name)


EXPECTED_BANDS = {
    0: ("primary", 1),
    50: ("primary", 1),
    51: ("primary", 2),
    64: ("primary", 2),
    65: ("primary", 3),
    71: ("primary", 3),
    72: ("secondary", 1),
    73: ("secondary", 1),
    74: ("secondary", 2),
    84: ("secondary", 2),
    85: ("secondary", 3),
    89: ("secondary", 3),
    90: None,
}


def expected_band(pct: int) -> tuple[str, int] | None:
    return EXPECTED_BANDS[pct]


class TestBoundaries(unittest.TestCase):
    def test_tier_boundaries_right_room(self) -> None:
        for role_name, character in (("weapons", "Delish"), ("engines", "Galactic Sprite"), ("rushers", "Cyber Duck")):
            role = role_named(role_name)
            for pct in BOUNDARY_PERCENTS:
                with self.subTest(role=role_name, pct=pct):
                    found = expected_band(pct)
                    if found is None:
                        crew = mk_crew(name=character, total=pct, room_name="GYM")
                        self.assertEqual(summary(plan_one(crew)), [("skip", 1, "complete")])
                        continue
                    slot = role.slot(found[0])
                    crew = mk_crew(name=character, total=pct, room_name=ROOM_NAME_FOR_TYPE[slot.room_type])
                    expected = [("start", 1, slot.trainings[found[1] - 1])]
                    self.assertEqual(summary(plan_one(crew)), expected)

    def test_tier_boundaries_wrong_room(self) -> None:
        primary = [pct for pct in BOUNDARY_PERCENTS if pct <= 71]
        secondary = [pct for pct in BOUNDARY_PERCENTS if 72 <= pct <= 89]
        cases = (
            ("Delish", primary, "GYM", "move to academy"),
            ("Delish", secondary, "Academy", "move to gym"),
            ("Galactic Sprite", primary, "Academy", "move to gym"),
            ("Galactic Sprite", secondary, "GYM", "move to academy"),
            ("Cyber Duck", primary + secondary, "Academy", "move to gym"),
            ("Cyber Duck", primary + secondary, "Lunar College", "move to gym"),
        )
        for character, percents, room_name, detail in cases:
            for pct in percents:
                with self.subTest(character=character, pct=pct, room=room_name):
                    decisions = plan_one(mk_crew(name=character, total=pct, room_name=room_name))
                    self.assertEqual(summary(decisions), [("skip", 1, "not_in_training_room")])
                    self.assertEqual(decisions[0].detail, detail)
        for room_name in ("GYM", "Galaxy Gym"):
            for pct in primary + secondary:
                with self.subTest(character="Cyber Duck", pct=pct, room=room_name):
                    decisions = plan_one(mk_crew(name="Cyber Duck", total=pct, room_name=room_name))
                    self.assertIsInstance(decisions[0], StartTraining)

    def test_ceiling_crosses_band_edge(self) -> None:
        cases = ((100, "Steam Yoga"), (101, "Crew vs Wild"), (142, "Space Marine"), (143, "Bench Press"))
        for total, expected in cases:
            with self.subTest(total=total):
                crew = mk_crew(name="Cyber Duck", rarity="Standard", capacity=200, total=total, room_name="GYM")
                self.assertEqual(summary(plan_one(crew)), [("start", 1, expected)])

    def test_research_gate_boundaries_per_band(self) -> None:
        cases = ((0, 1), (51, 2), (65, 3), (74, 2), (85, 3))
        for pct, level in cases:
            for research in ("Fitness", "Education"):
                with self.subTest(pct=pct, research=research):
                    crew = mk_crew(name="Cyber Duck", rarity="Standard", total=pct, room_name="GYM")
                    below = summary(plan_one(crew, research={research: level - 1}))
                    self.assertEqual(below, [("skip", 1, "research_missing")])
                    self.assertEqual(summary(plan_one(crew, research={research: level}))[0][0], "start")
        for pct in (72, 73):
            with self.subTest(pct=pct, research="none"):
                crew = mk_crew(name="Cyber Duck", rarity="Standard", total=pct, room_name="GYM")
                self.assertEqual(summary(plan_one(crew, research={})), [("start", 1, "Bench Press")])

    def test_float_ceil_regression_observable(self) -> None:
        crew = mk_crew(name="Cyber Duck", rarity="Standard", capacity=100, total=55, room_name="GYM")
        decisions = plan_one(crew)
        self.assertEqual(summary(decisions), [("start", 1, "Crew vs Wild")])
        self.assertIn("at 55%", decisions[0].reason)
        self.assertNotIn("56%", decisions[0].reason)

    def test_tp_cap_boundary(self) -> None:
        unique_69 = mk_crew(name="Delish", rarity="Unique", total=69, room_name="Academy")
        self.assertEqual(summary(plan_one(unique_69)), [("start", 1, "Weapons PHD")])
        unique_70 = mk_crew(name="Delish", rarity="Unique", total=70, room_name="Academy")
        self.assertEqual(summary(plan_one(unique_70)), [("skip", 1, "tp_cap_reached")])
        mistycball = mk_crew(name="Mistycball", rarity="Epic", total=85, room_name="GYM")
        self.assertEqual(summary(plan_one(mistycball)), [("start", 1, "Olympic Weightlifting")])


class TestTimeGate(unittest.TestCase):
    def test_finished_emits_finish_then_start(self) -> None:
        # Regression: the original treated the end date as a start date and idled for another full duration.
        crew = mk_crew(training_design_id=DESIGN_IDS["Weapons PHD"], training_end=NOW - timedelta(minutes=30))
        self.assertEqual(summary(plan_one(crew)), [("finish", 1), ("start", 1, "Read Expert Weapon Theory")])

    def test_end_equal_now_is_finished(self) -> None:
        crew = mk_crew(training_design_id=DESIGN_IDS["Weapons PHD"], training_end=NOW)
        self.assertEqual(summary(plan_one(crew)), [("finish", 1), ("start", 1, "Read Expert Weapon Theory")])

    def test_end_future_in_progress_no_finish(self) -> None:
        crew = mk_crew(training_design_id=DESIGN_IDS["Weapons PHD"], training_end=NOW + timedelta(hours=3))
        self.assertEqual(summary(plan_one(crew)), [("skip", 1, "training_in_progress")])

    def test_design_id_without_end_is_idle(self) -> None:
        crew = mk_crew(training_design_id=DESIGN_IDS["Weapons PHD"], training_end=None)
        self.assertEqual(summary(plan_one(crew)), [("start", 1, "Read Expert Weapon Theory")])

    def test_end_without_design_id_is_idle(self) -> None:
        for design_id in (None, 0):
            with self.subTest(design_id=design_id):
                crew = mk_crew(training_design_id=design_id, training_end=NOW - timedelta(minutes=5))
                self.assertEqual(summary(plan_one(crew)), [("start", 1, "Read Expert Weapon Theory")])

    def test_replan_after_finish_uses_rebuilt_stats_and_never_refinishes(self) -> None:
        # The primary that follows a FinishTraining is advisory (pre-finish stats); replan_after_finish replaces it.
        weapon_t1 = DESIGN_IDS["Read Expert Weapon Theory"]
        ended = NOW - timedelta(minutes=1)
        before = mk_crew(1, total=50, training_design_id=weapon_t1, training_end=ended)
        other = mk_crew(2, "Cyber Duck", room_name="GYM")
        ctx = mk_ctx(before, other, designs=ALL_DESIGNS, research=FULL_RESEARCH)
        self.assertEqual(
            summary(plan_training(ctx))[:2], [("finish", 1), ("start", 1, "Read Expert Weapon Theory")]
        )
        # The rebuilt crew may still carry the finished training's id and end date; neither may trigger a re-finish.
        rebuilt = mk_crew(1, total=51, training_design_id=weapon_t1, training_end=ended)
        self.assertEqual(summary(replan_after_finish(ctx, rebuilt)), [("start", 1, "Weapons Summit")])
        self.assertEqual(summary(replan_after_finish(ctx, rebuilt, DEFAULT_STRATEGY)), [("start", 1, "Weapons Summit")])
        grey = CONSUMABLE_IDS[("WPN", "Common")]
        starved = mk_ctx(before, designs=ALL_DESIGNS, research={}, items={grey: 1})
        rebuilt_low = mk_crew(1, total=20, stat="WeaponImprovement", training_design_id=weapon_t1, training_end=ended)
        self.assertEqual(summary(replan_after_finish(starved, rebuilt_low, CONSUMABLE_STRATEGY)), [("use", 1, grey)])

    def test_finish_precedes_any_primary(self) -> None:
        crew = mk_crew(name="Nobody", training_design_id=7, training_end=NOW - timedelta(seconds=1))
        self.assertEqual(summary(plan_one(crew)), [("finish", 1), ("skip", 1, "unknown_role")])
        crew = mk_crew(room_name=None, training_design_id=7, training_end=NOW - timedelta(seconds=1))
        self.assertEqual(summary(plan_one(crew)), [("finish", 1), ("skip", 1, "not_in_training_room")])


class TestDesignRoomLeakage(unittest.TestCase):
    def test_planner_duplicate_designs_lowest_id(self) -> None:
        extra = (mk_design(5, "Read Expert Weapon Theory"), mk_design(900, "Read Expert Weapon Theory"))
        for designs in (ALL_DESIGNS + extra, extra + ALL_DESIGNS):
            decisions = plan_one(mk_crew(), designs=designs)
            self.assertEqual(decisions[0].training_design_id, 5)

    def test_design_required_room_type_mismatch_design_missing(self) -> None:
        designs = (mk_design(5, "Read Expert Weapon Theory", required_room_type="gym"),)
        self.assertEqual(summary(plan_one(mk_crew(), designs=designs)), [("skip", 1, "design_missing")])
        designs = (mk_design(5, "Read Expert Weapon Theory", required_room_type="academy"),)
        self.assertEqual(plan_one(mk_crew(), designs=designs)[0].training_design_id, 5)

    def test_design_required_research_missing(self) -> None:
        designs = (mk_design(5, "Read Expert Weapon Theory", required_research="Advanced Training"),)
        decisions = plan_one(mk_crew(), designs=designs, research={"Fitness": 3})
        self.assertEqual(summary(decisions), [("skip", 1, "research_missing")])
        decisions = plan_one(mk_crew(), designs=designs, research={"Fitness": 3, "Advanced Training": 1})
        self.assertEqual(decisions[0].training_design_id, 5)

    def test_no_cross_crew_room_leakage(self) -> None:
        # Regression: the original kept self.roomName from the previous crew member when one had no room.
        crew_a = mk_crew(1, "Cyber Duck", room_name="Galaxy Gym", room_id=11)
        crew_b = mk_crew(2, "Cyber Duck", room_name=None, room_id=None)
        crew_c = mk_crew(3, "Cyber Duck", room_name=None, room_id=99)
        ctx = mk_ctx(crew_a, crew_b, crew_c, designs=ALL_DESIGNS, rooms={11: "Galaxy Gym"}, research=FULL_RESEARCH)
        self.assertEqual(
            summary(plan_training(ctx)),
            [("start", 1, "Steam Yoga"), ("skip", 2, "not_in_training_room"), ("skip", 3, "not_in_training_room")],
        )

    def test_room_name_falls_back_to_ctx_rooms(self) -> None:
        crew = mk_crew(1, "Cyber Duck", room_name=None, room_id=11)
        self.assertEqual(summary(plan_one(crew, rooms={11: "Galaxy Gym"})), [("start", 1, "Steam Yoga")])
        named = mk_crew(1, "Cyber Duck", room_name="Bridge", room_id=11)
        self.assertEqual(summary(plan_one(named, rooms={11: "Galaxy Gym"})), [("skip", 1, "not_in_training_room")])

    def test_galaxy_gym_exact_planner(self) -> None:
        annex = mk_crew(name="Cyber Duck", room_name="Galaxy Gym Annex")
        self.assertEqual(summary(plan_one(annex)), [("skip", 1, "not_in_training_room")])
        exact = mk_crew(name="Cyber Duck", room_name="Galaxy Gym")
        self.assertEqual(summary(plan_one(exact)), [("start", 1, "Steam Yoga")])

    def test_huge_hellaluya_planner(self) -> None:
        correct = mk_crew(name="Huge Hellaluya", rarity="Epic", room_name="GYM", total=85)
        self.assertEqual(summary(plan_one(correct)), [("start", 1, "Olympic Weightlifting")])
        typo = mk_crew(name="Huge Hellaloya", rarity="Epic", room_name="GYM")
        self.assertEqual(summary(plan_one(typo)), [("skip", 1, "unknown_role")])


CONSUMABLE_IDS = {
    (code, tier): 9000 + index
    for index, (code, tier) in enumerate(
        (code, tier) for tier in ("Common", "Hero") for code in sorted(set(STAT_CODES.values()))
    )
}
CONSUMABLE_STRATEGY = dataclasses.replace(DEFAULT_STRATEGY, consumables=CONSUMABLE_IDS)
ALL_STOCKED = {item_id: 3 for item_id in CONSUMABLE_IDS.values()}


def plan_cons(crew: CrewState, *, items=None, research=None, strategy=CONSUMABLE_STRATEGY, **kwargs):
    return plan_one(crew, strategy=strategy, items=ALL_STOCKED if items is None else items, research=research, **kwargs)


class TestConsumables(unittest.TestCase):
    def test_default_strategy_never_consumes(self) -> None:
        crews = (
            mk_crew(total=20, fatigue=0),
            mk_crew(total=95, fatigue=120),
            mk_crew(total=10, training_design_id=1, training_end=NOW + timedelta(hours=1)),
        )
        for crew in crews:
            for research in ({}, FULL_RESEARCH):
                decisions = plan_cons(crew, strategy=DEFAULT_STRATEGY, research=research)
                self.assertFalse(any(isinstance(d, UseConsumable) for d in decisions))

    def test_grey_when_research_missing(self) -> None:
        crew = mk_crew(total=20, fatigue=0, stat="WeaponImprovement")
        decisions = plan_cons(crew, research={})
        self.assertEqual(summary(decisions), [("use", 1, CONSUMABLE_IDS[("WPN", "Common")])])

    def test_grey_never_replaces_startable(self) -> None:
        crew = mk_crew(total=20, fatigue=0, stat="WeaponImprovement")
        self.assertEqual(summary(plan_cons(crew, research=FULL_RESEARCH)), [("start", 1, "Read Expert Weapon Theory")])

    def test_grey_boundaries(self) -> None:
        grey = CONSUMABLE_IDS[("WPN", "Common")]
        cases = ((29, 0, [("use", 1, grey)]), (30, 0, [("skip", 1, "research_missing")]))
        cases += ((20, 0, [("use", 1, grey)]), (20, 1, [("skip", 1, "research_missing")]))
        for total, fatigue, expected in cases:
            with self.subTest(total=total, fatigue=fatigue):
                crew = mk_crew(total=total, fatigue=fatigue, stat="WeaponImprovement")
                self.assertEqual(summary(plan_cons(crew, research={})), expected)

    def test_hero_boundaries(self) -> None:
        hero = CONSUMABLE_IDS[("WPN", "Hero")]
        cases = (
            (90, 100, [("use", 1, hero)]),
            (95, 150, [("use", 1, hero)]),
            (89, 100, [("skip", 1, "fatigued")]),
            (90, 99, [("skip", 1, "complete")]),
        )
        for total, fatigue, expected in cases:
            with self.subTest(total=total, fatigue=fatigue):
                crew = mk_crew(total=total, fatigue=fatigue, room_name="GYM", stat="WeaponImprovement")
                self.assertEqual(summary(plan_cons(crew, research=FULL_RESEARCH)), expected)

    def test_consumable_out_of_stock_falls_back_to_skip(self) -> None:
        empty = {item_id: 0 for item_id in CONSUMABLE_IDS.values()}
        in_progress = {"training_design_id": 1, "training_end": NOW + timedelta(hours=1)}
        cases = (
            (mk_crew(total=20, stat="WeaponImprovement"), {}, "research_missing"),
            (
                mk_crew(total=95, fatigue=100, room_name="GYM", stat="WeaponImprovement"),
                FULL_RESEARCH,
                "complete",
            ),
            (mk_crew(total=20, stat="WeaponImprovement", **in_progress), FULL_RESEARCH, "training_in_progress"),
        )
        for crew, research, code in cases:
            for items in (empty, {}):
                with self.subTest(code=code, items=bool(items)):
                    self.assertEqual(summary(plan_cons(crew, items=items, research=research)), [("skip", 1, code)])

    def test_consumable_targets_highest_stat_rpr(self) -> None:
        # Regression: the original took the first non-zero stat and mapped Repair to STA.
        stats = {"HpImprovement": 2, "RepairImprovement": 9, "StaminaImprovement": 5}
        crew = mk_crew(stats=stats)
        self.assertEqual(summary(plan_cons(crew, research={})), [("use", 1, CONSUMABLE_IDS[("RPR", "Common")])])

    def test_grey_during_in_progress(self) -> None:
        crew = mk_crew(
            total=10,
            stat="PilotImprovement",
            training_design_id=1,
            training_end=NOW + timedelta(hours=1),
        )
        self.assertEqual(
            summary(plan_cons(crew, research=FULL_RESEARCH)), [("use", 1, CONSUMABLE_IDS[("PLT", "Common")])]
        )

    def test_grey_during_in_progress_in_other_slot_room(self) -> None:
        # Precedence: in-progress (step 5, with fallback) is decided before the slot-room check (step 7).
        running = {"training_design_id": 1, "training_end": NOW + timedelta(hours=1)}
        crew = mk_crew(name="Galactic Sprite", total=0, room_name="Academy", **running)
        self.assertEqual(summary(plan_cons(crew)), [("use", 1, CONSUMABLE_IDS[("HP", "Common")])])

    def test_no_consumable_at_or_above_cap(self) -> None:
        for total in (70, 71):
            with self.subTest(total=total):
                crew = mk_crew(rarity="Unique", total=total, stat="WeaponImprovement", fatigue=100)
                self.assertEqual(summary(plan_cons(crew, research={})), [("skip", 1, "tp_cap_reached")])
        legendary = mk_crew(rarity="Legendary", capacity=95, total=95, fatigue=100, room_name="GYM")
        self.assertEqual(summary(plan_cons(legendary)), [("skip", 1, "tp_cap_reached")])

    def test_no_consumable_unknown_role_or_non_training_room(self) -> None:
        cases = (
            (mk_crew(name="Nobody", total=10), "unknown_role"),
            (mk_crew(total=10, room_name="Bridge"), "not_in_training_room"),
            (mk_crew(total=10, room_name=None), "not_in_training_room"),
            (mk_crew(total=95, fatigue=100, room_name="Bridge"), "not_in_training_room"),
        )
        for crew, code in cases:
            with self.subTest(code=code, room=crew.room_name):
                self.assertEqual(summary(plan_cons(crew, research={})), [("skip", 1, code)])

    def test_no_consumable_when_in_wrong_slot_room(self) -> None:
        # Wrong-slot crews should move rooms; consumables are not a substitute for moving.
        crew = mk_crew(total=20, room_name="GYM", stat="WeaponImprovement")
        self.assertEqual(summary(plan_cons(crew, research={})), [("skip", 1, "not_in_training_room")])

    def test_consumable_fallback_when_design_missing(self) -> None:
        crew = mk_crew(total=20, stat="WeaponImprovement")
        decisions = plan_cons(crew, research=FULL_RESEARCH, designs=())
        self.assertEqual(summary(decisions), [("use", 1, CONSUMABLE_IDS[("WPN", "Common")])])
        gated = (mk_design(5, "Read Expert Weapon Theory", required_research="Advanced Training"),)
        decisions = plan_cons(crew, research=FULL_RESEARCH, designs=gated)
        self.assertEqual(summary(decisions), [("use", 1, CONSUMABLE_IDS[("WPN", "Common")])])

    def test_consumable_stock_is_shared_across_crew_in_order(self) -> None:
        # Regression: each crew saw the full stock, so 3 crew and 2 items planned 3 uses the server would reject.
        grey = CONSUMABLE_IDS[("WPN", "Common")]
        crews = [mk_crew(cid, total=20, stat="WeaponImprovement") for cid in (1, 2, 3)]
        ctx = mk_ctx(*crews, designs=ALL_DESIGNS, research={}, items={grey: 2})
        expected = [("use", 1, grey), ("use", 2, grey), ("skip", 3, "research_missing")]
        self.assertEqual(summary(plan_training(ctx, CONSUMABLE_STRATEGY)), expected)
        self.assertEqual(dict(ctx.items), {grey: 2})
        self.assertEqual(plan_training(ctx, CONSUMABLE_STRATEGY), plan_training(ctx, CONSUMABLE_STRATEGY))

    def test_exhausted_first_rule_item_falls_through_to_next_rule(self) -> None:
        # Rules are tried in order; an item used up by an earlier crew lets a later matching rule fire.
        grey, hero = CONSUMABLE_IDS[("WPN", "Common")], CONSUMABLE_IDS[("WPN", "Hero")]
        rules = (
            ConsumableRule("Common", min_percent=0, max_percent=None, min_fatigue=0, max_fatigue=None),
            ConsumableRule("Hero", min_percent=0, max_percent=None, min_fatigue=0, max_fatigue=None),
        )
        strategy = dataclasses.replace(CONSUMABLE_STRATEGY, consumable_rules=rules)
        crews = [mk_crew(cid, total=20, stat="WeaponImprovement") for cid in (1, 2, 3)]
        ctx = mk_ctx(*crews, designs=ALL_DESIGNS, research={}, items={grey: 1, hero: 1})
        expected = [("use", 1, grey), ("use", 2, hero), ("skip", 3, "research_missing")]
        self.assertEqual(summary(plan_training(ctx, strategy)), expected)

    def test_fatigued_fallback_with_custom_rule(self) -> None:
        # Unreachable with the default rules (grey needs fatigue 0); rules are data, so the path stays.
        rule = ConsumableRule("Hero", min_percent=0, max_percent=None, min_fatigue=100, max_fatigue=None)
        strategy = dataclasses.replace(CONSUMABLE_STRATEGY, consumable_rules=(rule,))
        crew = mk_crew(total=40, fatigue=100, stat="ScienceImprovement")
        decisions = plan_cons(crew, strategy=strategy, research=FULL_RESEARCH)
        self.assertEqual(summary(decisions), [("use", 1, CONSUMABLE_IDS[("SCI", "Hero")])])


class TestStructure(unittest.TestCase):
    def test_order_follows_crew_and_one_primary_each(self) -> None:
        finished = {"training_design_id": 7, "training_end": NOW - timedelta(hours=1)}
        crew = (
            mk_crew(5, "Cyber Duck", room_name="GYM", **finished),
            mk_crew(2, "Nobody"),
            mk_crew(9, "Delish", total=72, room_name="Academy"),
            mk_crew(1, "Delish", total=95, room_name="GYM", **finished),
        )
        decisions = plan_training(mk_ctx(*crew, designs=ALL_DESIGNS, research=FULL_RESEARCH))
        self.assertEqual(
            summary(decisions),
            [
                ("finish", 5),
                ("start", 5, "Steam Yoga"),
                ("skip", 2, "unknown_role"),
                ("skip", 9, "not_in_training_room"),
                ("finish", 1),
                ("skip", 1, "complete"),
            ],
        )
        primaries = [d for d in decisions if not isinstance(d, FinishTraining)]
        self.assertEqual([d.character_id for d in primaries], [5, 2, 9, 1])

    def test_plan_deterministic(self) -> None:
        crew = (mk_crew(1, "Delish"), mk_crew(2, "Cyber Duck", room_name="GYM", total=80))
        ctx = mk_ctx(*crew, designs=ALL_DESIGNS, research=FULL_RESEARCH, rooms={1: "GYM"}, items={1: 1})
        first = plan_training(ctx)
        self.assertEqual(plan_training(ctx), first)
        self.assertIsInstance(first, tuple)

    def test_with_consumables_returns_new_strategy(self) -> None:
        configured = with_consumables(DEFAULT_STRATEGY, {("WPN", "Common"): 42})
        self.assertIsNot(configured, DEFAULT_STRATEGY)
        self.assertEqual(dict(configured.consumables), {("WPN", "Common"): 42})
        self.assertEqual(dict(DEFAULT_STRATEGY.consumables), {})
        self.assertEqual(configured.roles, DEFAULT_STRATEGY.roles)
        for bad in ({("XYZ", "Common"): 1}, {("Weapon", "Hero"): 1}, {("@WPN", "Hero"): 1}, {("WPN", "Mythic"): 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                with_consumables(DEFAULT_STRATEGY, bad)

    def test_strategy_validation_rules(self) -> None:
        lab = Role("lab", frozenset({"Zed"}), RoleSlot("lab", ("A", "B", "C")), RoleSlot("gym", ("D", "E", "F")))
        gates = dict(DEFAULT_STRATEGY.research_gates)
        del gates[("secondary", 1)]
        invalid = {
            "unknown room type in role": {"roles": (*DEFAULT_STRATEGY.roles, lab)},
            "band without research gate": {"research_gates": gates},
            "non-positive fatigue limit": {"max_training_fatigue": 0},
            # Regression: a mistyped tier ("Grey") or an inverted window built fine and then never fired.
            "consumable rule with unknown tier": {"consumable_rules": (ConsumableRule("Grey", 0, 29, 0, 0),)},
            "consumable rule with inverted percent": {"consumable_rules": (ConsumableRule("Common", 30, 29, 0, 0),)},
            "consumable rule with inverted fatigue": {"consumable_rules": (ConsumableRule("Common", 0, 29, 5, 4),)},
            "consumable item id zero": {"consumables": {("WPN", "Common"): 0}},
            "consumable item id negative": {"consumables": {("WPN", "Common"): -3}},
            "consumable item id bool": {"consumables": {("WPN", "Common"): True}},
            "consumable item id string": {"consumables": {("WPN", "Common"): "9001"}},
            "non-positive tp cap": {"tp_caps": {3: 0}},
            "tp cap keyed by rarity name": {"tp_caps": {"Unique": 70}},
            "exceptional cap not keyed by (stars, name)": {"exceptional_tp_caps": {"Mistycball": 100}},
            "non-positive exceptional cap": {"exceptional_tp_caps": {(4, "Mistycball"): 0}},
            "non-positive captain cap": {"captain_tp_caps": {6: -1}},
        }
        for label, changes in invalid.items():
            with self.subTest(label), self.assertRaises(ValueError):
                dataclasses.replace(DEFAULT_STRATEGY, **changes)

    def test_strategy_mappings_read_only(self) -> None:
        strategy = with_consumables(DEFAULT_STRATEGY, {("WPN", "Common"): 1})
        mappings = (
            strategy.room_types,
            strategy.research_gates,
            strategy.rarity_stars,
            strategy.consumables,
            strategy.tp_caps,
            strategy.exceptional_tp_caps,
            strategy.captain_tp_caps,
            STAT_CODES,
        )
        for mapping in mappings:
            with self.subTest(type=type(mapping).__name__), self.assertRaises(TypeError):
                mapping["x"] = 1  # type: ignore[index]
        self.assertIsInstance(strategy.roles, tuple)
        self.assertIsInstance(strategy.bands, tuple)
        self.assertIsInstance(strategy.consumable_rules, tuple)
        for role in strategy.roles:
            self.assertIsInstance(role.characters, frozenset)
            self.assertIsInstance(role.primary.trainings, tuple)

    def test_training_package_imports_allowlist(self) -> None:
        allowed = {"__future__", "dataclasses", "datetime", "types", "typing", "collections.abc", "math", "re"}
        # crew_leveling only seeds DEFAULT_STRATEGY's TP tables: strategy.py may copy its constants, and no module
        # may call into it at plan time (its tables are mutable module globals).
        leveling_constants = {"CAPTAIN_TP", "EXCEPTIONAL_4STAR_NAMES", "EXCEPTIONAL_4STAR_TP", "TP_CAPS_BY_RARITY"}
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module == "sdk.crew_leveling":
                    with self.subTest(file=path.name, crew_leveling=True):
                        self.assertEqual(path.name, "strategy.py")
                        self.assertLessEqual({alias.name for alias in node.names}, leveling_constants)
        sources = sorted(PACKAGE_DIR.glob("*.py"))
        names = {path.name for path in sources}
        self.assertEqual(names, {"__init__.py", "model.py", "strategy.py", "parse.py", "planner.py"})
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    with self.subTest(file=path.name, call=called):
                        self.assertNotIn(called, {"now", "utcnow", "today", "print", "open", "input", "time"})
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    self.assertEqual(node.level, 0, f"{path.name}: use absolute imports")
                    modules = [node.module or ""]
                else:
                    continue
                for module in modules:
                    with self.subTest(file=path.name, module=module):
                        in_package = module.startswith("sdk.training.")
                        leveling = module == "sdk.crew_leveling" and path.name == "strategy.py"
                        self.assertTrue(module in allowed or in_package or leveling, module)

    def test_planner_helpers_are_public_and_take_the_strategy_explicitly(self) -> None:
        expected = {
            "band_for",
            "design_research_ok",
            "effective_cap",
            "highest_stat",
            "percent",
            "pick_consumable",
            "pick_design",
            "plan_training",
            "replan_after_finish",
            "research_ok",
            "resolve_room_name",
            "role_for",
            "room_type_for",
            "total_points",
            "training_state",
        }
        self.assertEqual(set(planner_module.__all__), expected)
        # Regression: helpers defaulted to DEFAULT_STRATEGY, so a caller planning with another strategy could
        # silently look bands, rooms or caps up in the default one.
        for name in ("band_for", "effective_cap", "pick_consumable", "research_ok", "role_for", "room_type_for"):
            with self.subTest(helper=name):
                parameter = inspect.signature(getattr(planner_module, name)).parameters["strategy"]
                self.assertIs(parameter.default, inspect.Parameter.empty)
        for name in ("plan_training", "replan_after_finish"):
            with self.subTest(entry_point=name):
                parameter = inspect.signature(getattr(planner_module, name)).parameters["strategy"]
                self.assertIs(parameter.default, DEFAULT_STRATEGY)

    def test_single_frozen_mapping_helper(self) -> None:
        # Regression: model.py and strategy.py each defined an identical MappingProxyType(dict(...)) helper.
        builders = []
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.FunctionDef) and any(
                    isinstance(inner, ast.Return)
                    and isinstance(inner.value, ast.Call)
                    and getattr(inner.value.func, "id", "") == "MappingProxyType"
                    and len(inner.value.args) == 1
                    and isinstance(inner.value.args[0], ast.Call)
                    and getattr(inner.value.args[0].func, "id", "") == "dict"
                    for inner in ast.walk(node)
                ):
                    builders.append((path.name, node.name))
        self.assertEqual(builders, [("model.py", "frozen_mapping")])

    def test_package_exports_public_api(self) -> None:
        import sdk.training as package

        required = {
            "plan_training",
            "build_context",
            "with_consumables",
            "parse_consumable_designs",
            "classify_payload",
            "DEFAULT_STRATEGY",
            "TrainingStrategy",
            "TrainingContext",
            "CrewState",
            "TrainingDesign",
            "Decision",
            "FinishTraining",
            "StartTraining",
            "UseConsumable",
            "Skip",
            "SKIP_CODES",
            "replan_after_finish",
            "classify_payloads",
        }
        self.assertIsInstance(package.__all__, tuple)
        self.assertLessEqual(required, set(package.__all__))
        self.assertEqual(len(package.__all__), len(set(package.__all__)))
        for name in package.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(package, name))

    def test_no_module_level_mutable_containers(self) -> None:
        mutable_nodes = (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)
        mutable_calls = {"list", "dict", "set", "bytearray"}
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                    value = node.value
                    is_call = isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                    with self.subTest(file=path.name, line=node.lineno):
                        self.assertNotIsInstance(value, mutable_nodes)
                        self.assertFalse(is_call and value.func.id in mutable_calls)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Property suite: generator, an independent oracle, and the seeds.
# The oracle re-derives each crew's expected decision from test-local literals. Of the planner's helpers it reuses
# only effective_cap (pinned above by literal tables), so a regression in percent, bands, rooms, roles, training
# state, design choice, research gates, Skip precedence or consumable choice cannot hide in planner and oracle alike.
# Module-level functions (no TestCase) so other test modules may import them.
# ---------------------------------------------------------------------------

# (low %, high %, slot, tier), ascending; percents below 0 clamp to the first row.
ORACLE_BANDS = (
    (0, 50, "primary", 1),
    (51, 64, "primary", 2),
    (65, 71, "primary", 3),
    (72, 73, "secondary", 1),
    (74, 84, "secondary", 2),
    (85, 89, "secondary", 3),
)
ORACLE_COMPLETE_PERCENT = 90
ORACLE_ROOM_TYPES = {"Academy": "academy", "Lunar College": "academy", "GYM": "gym", "Galaxy Gym": "gym"}
# (slot, tier) -> Fitness or Education level the band needs; 0 = ungated.
ORACLE_RESEARCH_GATES = {
    ("primary", 1): 1,
    ("primary", 2): 2,
    ("primary", 3): 3,
    ("secondary", 1): 0,
    ("secondary", 2): 2,
    ("secondary", 3): 3,
}
ORACLE_FATIGUE_LIMIT = 100
ORACLE_STAT_ORDER = (
    "HpImprovement",
    "PilotImprovement",
    "RepairImprovement",
    "WeaponImprovement",
    "ScienceImprovement",
    "EngineImprovement",
    "AttackImprovement",
    "AbilityImprovement",
    "StaminaImprovement",
)
ORACLE_STAT_CODES = {
    "HpImprovement": "HP",
    "PilotImprovement": "PLT",
    "RepairImprovement": "RPR",
    "WeaponImprovement": "WPN",
    "ScienceImprovement": "SCI",
    "EngineImprovement": "ENG",
    "AttackImprovement": "ATK",
    "AbilityImprovement": "ABL",
    "StaminaImprovement": "STA",
}
# (tier, min %, max %, min fatigue, max fatigue), tried in order; None = unbounded.
ORACLE_CONSUMABLE_RULES = (("Common", 0, 29, 0, 0), ("Hero", 90, None, 100, None))
ORACLE_ROLE_OF = {character: role for role, characters in ROLE_CHARACTERS.items() for character in characters}

# The primary decision's precedence as a literal list: (outcome, consumable fallback allowed, predicate on facts).
# The first row whose predicate holds decides. Later predicates may assume earlier ones were false.
ORACLE_PRECEDENCE = (
    ("unknown_role", False, lambda facts: facts["role"] is None),
    ("not_in_training_room", False, lambda facts: facts["room_type"] is None),
    ("design_missing", False, lambda facts: facts["capacity"] <= 0),
    ("tp_cap_reached", False, lambda facts: facts["total"] >= facts["cap"]),
    ("training_in_progress", True, lambda facts: facts["in_progress"]),
    ("complete", True, lambda facts: facts["band"] is None),
    ("not_in_training_room", False, lambda facts: facts["slot_room"] != facts["room_type"]),
    ("research_missing", True, lambda facts: not facts["gate_met"]),
    ("fatigued", True, lambda facts: facts["fatigue"] >= ORACLE_FATIGUE_LIMIT),
    ("design_missing", True, lambda facts: facts["design"] is None),
    ("research_missing", True, lambda facts: not facts["design_research_met"]),
    ("start", False, lambda facts: True),
)


def _require_oracle_strategy(strategy) -> None:
    """The oracle's literals describe DEFAULT_STRATEGY; only the consumable item ids may differ."""
    if dataclasses.replace(strategy, consumables={}) != DEFAULT_STRATEGY:
        raise ValueError("the oracle only models DEFAULT_STRATEGY (plus consumable item ids)")


def _oracle_facts(ctx: TrainingContext, strategy, crew: CrewState) -> dict:
    room_name = crew.room_name or (ctx.rooms.get(crew.room_id) if crew.room_id is not None else None)
    room_type = ORACLE_ROOM_TYPES.get(room_name.strip()) if room_name else None
    role = ORACLE_ROLE_OF.get(crew.name)
    total = sum(crew.stats.values())
    capacity = crew.training_capacity
    pct = -(-total * 100 // capacity) if capacity > 0 else None
    band = None
    if pct is not None and pct < ORACLE_COMPLETE_PERCENT:
        band = next((slot, tier) for _low, high, slot, tier in ORACLE_BANDS if pct <= high)
    slot_room = design = None
    gate_met = design_research_met = False
    if band is not None and role is not None:
        primary_room, primary_names, secondary_room, secondary_names = EXPECTED_ROLE_TABLE[role]
        slot_room, names = (primary_room, primary_names) if band[0] == "primary" else (secondary_room, secondary_names)
        name = names[band[1] - 1]
        level = ORACLE_RESEARCH_GATES[band]
        best_level = max(ctx.research_levels.get("Fitness", 0), ctx.research_levels.get("Education", 0))
        gate_met = level == 0 or best_level >= level
        for candidate in ctx.designs:
            if candidate.name == name and candidate.required_room_type in (None, room_type):
                if design is None or candidate.id < design.id:
                    design = candidate
        if design is not None:
            needed = design.required_research
            design_research_met = needed is None or ctx.research_levels.get(needed, 0) >= 1
    end = crew.training_end
    has_training = bool(crew.training_design_id) and end is not None
    return {
        "role": role,
        "room_type": room_type,
        "capacity": capacity,
        "total": total,
        "cap": effective_cap(crew, strategy),
        "pct": pct,
        "band": band,
        "slot_room": slot_room,
        "gate_met": gate_met,
        "fatigue": crew.fatigue,
        "design": design,
        "design_research_met": design_research_met,
        "in_progress": has_training and end > ctx.now,
        "finished": has_training and end <= ctx.now,
    }


def _oracle_consumable(strategy, crew: CrewState, pct: int, stock: dict[int, int]) -> int | None:
    """Item for the first rule whose window holds (pct, fatigue) and whose highest-stat item is still in stock."""
    best = ORACLE_STAT_ORDER[0]
    for stat in ORACLE_STAT_ORDER[1:]:
        if crew.stats.get(stat, 0) > crew.stats.get(best, 0):
            best = stat
    code = ORACLE_STAT_CODES[best]
    for tier, low, high, fatigue_low, fatigue_high in ORACLE_CONSUMABLE_RULES:
        in_percent = low <= pct and (high is None or pct <= high)
        in_fatigue = fatigue_low <= crew.fatigue and (fatigue_high is None or crew.fatigue <= fatigue_high)
        item = strategy.consumables.get((code, tier))
        if in_percent and in_fatigue and item is not None and stock.get(item, 0) > 0:
            return item
    return None


def oracle_primary(ctx: TrainingContext, strategy, crew: CrewState, stock: dict[int, int]) -> tuple:
    """The expected primary decision as ("start", design id) | ("use", item id) | ("skip", reason code)."""
    facts = _oracle_facts(ctx, strategy, crew)
    outcome, fallback = next((outcome, fallback) for outcome, fallback, holds in ORACLE_PRECEDENCE if holds(facts))
    if outcome == "start":
        return ("start", facts["design"].id)
    if fallback:
        item = _oracle_consumable(strategy, crew, facts["pct"], stock)
        if item is not None:
            return ("use", item)
    return ("skip", outcome)


def decision_shape(decision) -> tuple:
    if isinstance(decision, StartTraining):
        return ("start", decision.training_design_id)
    if isinstance(decision, UseConsumable):
        return ("use", decision.item_design_id)
    if isinstance(decision, Skip):
        return ("skip", decision.reason_code)
    return ("other", decision)


def plan_violations(ctx: TrainingContext, strategy, decisions) -> list[str]:
    """Check a plan against the oracle: structure, per-crew expected decision, and shared consumable stock."""
    _require_oracle_strategy(strategy)
    problems: list[str] = []
    decisions = list(decisions)
    stock = dict(ctx.items)
    position = 0
    for crew in ctx.crew:
        cid = crew.character_id
        finished = _oracle_facts(ctx, strategy, crew)["finished"]
        has_finish = (
            position < len(decisions)
            and isinstance(decisions[position], FinishTraining)
            and decisions[position].character_id == cid
        )
        if has_finish:
            position += 1
            if not finished:
                problems.append(f"{cid}: finish while training is not finished")
        elif finished:
            problems.append(f"{cid}: finished training not collected first")
        if position >= len(decisions):
            problems.append(f"{cid}: missing primary decision")
            continue
        primary = decisions[position]
        if isinstance(primary, FinishTraining) or primary.character_id != cid:
            problems.append(f"{cid}: expected a primary decision, found {primary!r}")
            continue
        position += 1
        expected = oracle_primary(ctx, strategy, crew, stock)
        actual = decision_shape(primary)
        if actual != expected:
            problems.append(f"{cid}: planned {actual}, oracle expects {expected}")
        if expected[0] == "use":
            stock[expected[1]] -= 1
    if position != len(decisions):
        problems.append(f"{len(decisions) - position} decisions after the last crew's primary")
    uses = Counter(decision.item_design_id for decision in decisions if isinstance(decision, UseConsumable))
    for item, count in sorted(uses.items()):
        if count > ctx.items.get(item, 0):
            problems.append(f"item {item}: {count} UseConsumable decisions but only {ctx.items.get(item, 0)} in stock")
    return problems


def _bad_plan_cases() -> list[tuple[str, TrainingContext, tuple]]:
    weapon_id = DESIGN_IDS["Read Expert Weapon Theory"]
    grey = CONSUMABLE_IDS[("WPN", "Common")]
    finished = {"training_design_id": 7, "training_end": NOW - timedelta(minutes=1)}
    running = {"training_design_id": 7, "training_end": NOW + timedelta(minutes=1)}
    idle = mk_crew(1)
    capped = mk_crew(1, rarity="Unique", total=70)
    done = mk_crew(1, **finished)
    busy = mk_crew(1, total=40, **running)
    busy_fatigued = mk_crew(1, total=40, fatigue=100, **running)
    capped_complete = mk_crew(1, rarity="Unique", total=95, room_name="GYM")
    no_research = mk_crew(1, total=10, stat="WeaponImprovement")
    no_research_2 = mk_crew(2, total=10, stat="WeaponImprovement")
    mixed = mk_crew(1, stats={"WeaponImprovement": 6, "HpImprovement": 4})
    second = mk_crew(2, "Cyber Duck", room_name="GYM")

    def ctx(*crew: CrewState, items=None, research=FULL_RESEARCH) -> TrainingContext:
        return mk_ctx(*crew, designs=ALL_DESIGNS, research=research, items=items)

    start = StartTraining(1, weapon_id, "r")
    stocked = {item_id: 3 for item_id in CONSUMABLE_IDS.values()}
    return [
        ("start at cap", ctx(capped), (start,)),
        ("finish after primary", ctx(done), (start, FinishTraining(1, "r"))),
        ("finish while end > now", ctx(busy), (FinishTraining(1, "r"), Skip(1, "training_in_progress", ""))),
        ("finished but not collected", ctx(done), (start,)),
        ("two primaries", ctx(idle), (start, Skip(1, "complete", ""))),
        ("zero primaries", ctx(idle), ()),
        ("unknown design id", ctx(idle), (StartTraining(1, 99999, "r"),)),
        ("start while in progress", ctx(busy), (StartTraining(1, DESIGN_IDS["Weapons Summit"], "r"),)),
        ("use with stock 0", ctx(no_research, items={grey: 0}, research={}), (UseConsumable(1, grey, "r"),)),
        ("use instead of startable", ctx(no_research, items={grey: 3}), (UseConsumable(1, grey, "r"),)),
        ("skip a startable crew", ctx(idle), (Skip(1, "fatigued", ""),)),
        ("wrong skip code", ctx(idle, research={}), (Skip(1, "tp_cap_reached", ""),)),
        ("fatigued outranks in progress", ctx(busy_fatigued), (Skip(1, "fatigued", ""),)),
        ("complete outranks tp cap", ctx(capped_complete), (Skip(1, "complete", ""),)),
        (
            "hero item outside its window",
            ctx(no_research, items=stocked, research={}),
            (UseConsumable(1, CONSUMABLE_IDS[("WPN", "Hero")], "r"),),
        ),
        (
            "item for a lower stat",
            ctx(mixed, items=stocked, research={}),
            (UseConsumable(1, CONSUMABLE_IDS[("HP", "Common")], "r"),),
        ),
        (
            "stock over-committed across crew",
            ctx(no_research, no_research_2, items={grey: 1}, research={}),
            (UseConsumable(1, grey, "r"), UseConsumable(2, grey, "r")),
        ),
        (
            "wrong crew order",
            ctx(idle, second),
            (StartTraining(2, DESIGN_IDS["Steam Yoga"], "r"), start),
        ),
    ]


BASE_NOW = datetime(2026, 1, 1, tzinfo=UTC)
EDGE_PERCENTS = (0, 29, 30, 50, 51, 64, 65, 68, 71, 72, 73, 74, 84, 85, 87, 89, 90, 95)
GEN_ROOMS = ("Academy", "Lunar College", "GYM", "Galaxy Gym", "Galaxy Gym Annex", "gym", "Bridge", None)
GEN_ROOMS_BY_TYPE = {"academy": ("Academy", "Lunar College"), "gym": ("GYM", "Galaxy Gym")}
GEN_CAPACITIES = (20, 100, 150, 200, 300)
GEN_RARITIES = (*DEFAULT_STRATEGY.rarity_stars, "Standard")
GEN_ROLE_NAMES = tuple(sorted(name for role in DEFAULT_STRATEGY.roles for name in role.characters))
GEN_OFFSETS = (timedelta(hours=-7), timedelta(hours=5, minutes=30), timedelta(hours=9))
PROPERTY_STRATEGY = with_consumables(DEFAULT_STRATEGY, CONSUMABLE_IDS)
TIER_OF_ITEM = {item_id: tier for (_code, tier), item_id in CONSUMABLE_IDS.items()}
SEEDS = range(500)


def make_designs(rng: random.Random, strategy=PROPERTY_STRATEGY) -> tuple[TrainingDesign, ...]:
    names = sorted(
        {name for role in strategy.roles for slot in (role.primary, role.secondary) for name in slot.trainings}
    )
    ids = iter(rng.sample(range(100, 2000), 2 * len(names)))
    designs: list[TrainingDesign] = []
    for name in names:
        if rng.random() >= 0.85:
            continue
        copies = 2 if rng.random() < 0.10 else 1
        for _ in range(copies):
            room = rng.choice(("academy", "gym")) if rng.random() < 0.10 else None
            research = "Advanced Training" if rng.random() < 0.3 else None
            designs.append(TrainingDesign(next(ids), name, 1, None, timedelta(hours=1), research, room))
    rng.shuffle(designs)
    return tuple(designs)


def _split_total(rng: random.Random, total: int) -> dict[str, int]:
    stats = dict.fromkeys(CANONICAL_STATS, 0)
    for _ in range(total):
        stats[rng.choice(CANONICAL_STATS)] += 1
    return stats


def make_crew(rng: random.Random, cid: int, strategy=PROPERTY_STRATEGY, now: datetime = BASE_NOW):
    """One random crew plus the ctx.rooms entries it needs (room id fallback)."""
    name = rng.choice(GEN_ROLE_NAMES) if rng.random() < 0.8 else f"Rando{cid}"
    capacity = 0 if rng.random() < 0.05 else rng.choice(GEN_CAPACITIES)
    target = rng.choice(EDGE_PERCENTS) if rng.random() < 0.5 else rng.randint(0, 110)
    total = max(0, target * max(capacity, 1) // 100 + rng.choice((-1, 0, 0, 1)))
    room = rng.choice(GEN_ROOMS)
    role = ORACLE_ROLE_OF.get(name)
    if role is not None and target < ORACLE_COMPLETE_PERCENT and rng.random() < 0.6:
        # Bias towards the room the target band trains in, so starts and consumables are common enough to check.
        slot = next(slot for _low, high, slot, _tier in ORACLE_BANDS if target <= high)
        room_type = EXPECTED_ROLE_TABLE[role][0 if slot == "primary" else 2]
        room = rng.choice(GEN_ROOMS_BY_TYPE[room_type])
    rooms: dict[int, str] = {}
    room_mode = rng.random()
    if room_mode < 0.6:
        room_id, room_name = None, room
    elif room_mode < 0.9:
        room_id, room_name = 1000 + cid, None
        if room is not None:
            rooms[room_id] = room
    else:
        room_id, room_name = 5000 + cid, None  # id with no ctx.rooms entry
    fatigue = rng.choice((0, 0, 0, rng.randint(1, 99), 99, 100, 150))
    mode = rng.choice(("idle", "running", "finished", "id_no_end", "end_no_id"))
    design_id = rng.choice((101, 150, 777))
    seconds = rng.choice((0, 1, rng.randint(2, 86400)))
    training = {
        "idle": (None, None),
        "running": (design_id, now + timedelta(seconds=max(seconds, 1))),
        "finished": (design_id, now - timedelta(seconds=seconds)),
        "id_no_end": (design_id, None),
        "end_no_id": (rng.choice((None, 0)), now - timedelta(seconds=seconds)),
    }[mode]
    end = training[1]
    if end is not None and rng.random() < 0.2:
        end = end.astimezone(timezone(rng.choice(GEN_OFFSETS)))  # same instant, non-UTC offset
    crew = CrewState(
        character_id=cid,
        name=name,
        design_id=cid + 500,
        rarity=rng.choice(GEN_RARITIES),
        level=rng.randint(1, 40),
        room_id=room_id,
        room_name=room_name,
        stats=_split_total(rng, total),
        training_capacity=capacity,
        fatigue=fatigue,
        training_design_id=training[0],
        training_end=end,
    )
    return crew, rooms


def make_context(rng: random.Random, strategy=PROPERTY_STRATEGY) -> TrainingContext:
    now = BASE_NOW + timedelta(seconds=rng.randrange(86400))
    designs = make_designs(rng, strategy)
    crew: list[CrewState] = []
    rooms: dict[int, str] = {}
    for cid in rng.sample(range(1, 10000), rng.randint(1, 8)):
        member, member_rooms = make_crew(rng, cid, strategy, now)
        crew.append(member)
        rooms.update(member_rooms)
    copy_mode = rng.random()
    if len(crew) > 1 and copy_mode < 0.15:
        crew[-1] = dataclasses.replace(crew[-1], character_id=crew[0].character_id)  # duplicate character id
    elif copy_mode > 0.75:
        # Contention: a consumable-ready first crew (training, fatigue 0, low percent) plus its twin under a fresh
        # id, so both want the same item and a stock of 1 cannot serve them both.
        ready = dataclasses.replace(
            crew[0],
            name=rng.choice(GEN_ROLE_NAMES),
            room_id=None,
            room_name=rng.choice(("Academy", "GYM")),
            rarity="Legendary",
            training_capacity=100,
            stats=_split_total(rng, rng.randint(0, 29)),
            fatigue=0,
            training_design_id=101,
            training_end=now + timedelta(hours=1),
        )
        crew[0] = ready
        crew.append(dataclasses.replace(ready, character_id=ready.character_id + 10000))
    research = {name: rng.randint(0, 3) for name in ("Fitness", "Education", "Advanced Training")}
    if rng.random() < 0.25:
        research = {}  # a new ship: every research gate is unmet
    items = {item_id: rng.choice((0, 1, 1, 3)) for item_id in sorted(strategy.consumables.values())}
    return TrainingContext(now, tuple(crew), designs, rooms, research, items)


def primaries(decisions) -> list:
    return [decision for decision in decisions if not isinstance(decision, FinishTraining)]


class TestPlannerProperties(unittest.TestCase):
    def test_violation_checker_catches_bad_plans(self) -> None:
        for label, ctx, bad_plan in _bad_plan_cases():
            with self.subTest(label):
                self.assertEqual(plan_violations(ctx, CONSUMABLE_STRATEGY, plan_training(ctx, CONSUMABLE_STRATEGY)), [])
                self.assertNotEqual(plan_violations(ctx, CONSUMABLE_STRATEGY, bad_plan), [])

    def test_violation_checker_flags_over_committed_stock_explicitly(self) -> None:
        grey = CONSUMABLE_IDS[("WPN", "Common")]
        crews = [mk_crew(cid, total=10, stat="WeaponImprovement") for cid in (1, 2)]
        ctx = mk_ctx(*crews, designs=ALL_DESIGNS, items={grey: 1})
        problems = plan_violations(ctx, CONSUMABLE_STRATEGY, (UseConsumable(1, grey, "r"), UseConsumable(2, grey, "r")))
        self.assertIn(f"item {grey}: 2 UseConsumable decisions but only 1 in stock", problems)

    def test_oracle_rejects_strategies_it_does_not_model(self) -> None:
        ctx = mk_ctx(mk_crew(), designs=ALL_DESIGNS)
        with self.assertRaises(ValueError):
            plan_violations(ctx, dataclasses.replace(DEFAULT_STRATEGY, max_training_fatigue=50), ())

    def test_generator_seed_deterministic(self) -> None:
        self.assertEqual(make_context(random.Random(7)), make_context(random.Random(7)))
        self.assertNotEqual(make_context(random.Random(7)), make_context(random.Random(8)))

    def test_invariants_hold_500_seeds(self) -> None:
        for seed in SEEDS:
            ctx = make_context(random.Random(seed))
            with self.subTest(seed=seed):
                self.assertEqual(plan_violations(ctx, PROPERTY_STRATEGY, plan_training(ctx, PROPERTY_STRATEGY)), [])

    def test_plan_composes_per_crew_when_stock_is_uncontended(self) -> None:
        # Consumable stock is shared in crew order, so planning crew one at a time only matches the whole plan when
        # no item can run out: every stocked item gets one unit per crew here. Contention has its own tests.
        for seed in SEEDS:
            ctx = make_context(random.Random(seed))
            plenty = {item: len(ctx.crew) if quantity else 0 for item, quantity in ctx.items.items()}
            ctx = dataclasses.replace(ctx, items=plenty)
            pieces: list = []
            for member in ctx.crew:
                pieces.extend(plan_training(dataclasses.replace(ctx, crew=(member,)), PROPERTY_STRATEGY))
            with self.subTest(seed=seed):
                self.assertEqual(plan_training(ctx, PROPERTY_STRATEGY), tuple(pieces))

    def test_plan_deterministic_over_seeds(self) -> None:
        for seed in SEEDS:
            ctx = make_context(random.Random(seed))
            first = plan_training(ctx, PROPERTY_STRATEGY)
            with self.subTest(seed=seed):
                self.assertEqual(plan_training(ctx, PROPERTY_STRATEGY), first)
                self.assertEqual(plan_training(make_context(random.Random(seed)), PROPERTY_STRATEGY), first)

    def test_generator_covers_everything(self) -> None:
        seen: Counter[str] = Counter()
        for seed in SEEDS:
            ctx = make_context(random.Random(seed))
            decisions = plan_training(ctx, PROPERTY_STRATEGY)
            without_items = primaries(plan_training(ctx, DEFAULT_STRATEGY))
            plenty = {item: len(ctx.crew) if quantity else 0 for item, quantity in ctx.items.items()}
            if plan_training(dataclasses.replace(ctx, items=plenty), PROPERTY_STRATEGY) != decisions:
                seen["contended stock changed the plan"] += 1
            ids = [member.character_id for member in ctx.crew]
            if len(ids) != len(set(ids)):
                seen["duplicate character id"] += 1
            for index, decision in enumerate(decisions):
                seen[type(decision).__name__] += 1
                if isinstance(decision, FinishTraining):
                    seen[f"finish then {type(decisions[index + 1]).__name__}"] += 1
            for member, primary, fallback in zip(ctx.crew, primaries(decisions), without_items):
                end = member.training_end
                if end is not None and end.utcoffset() != timedelta(0):
                    seen["non-UTC training_end"] += 1
                if isinstance(primary, Skip):
                    seen[primary.reason_code] += 1
                # Variants come from the consumable-free plan, so a Skip a consumable replaced still counts.
                if isinstance(fallback, Skip):
                    if fallback.reason_code == "not_in_training_room":
                        seen["move to" if fallback.detail.startswith("move to ") else "no training room"] += 1
                    if fallback.reason_code == "design_missing":
                        no_capacity = fallback.detail == "no training capacity"
                        seen["no capacity" if no_capacity else "no design"] += 1
                    if fallback.reason_code == "research_missing":
                        design_gate = "requires research" in fallback.detail
                        seen["design research" if design_gate else "band research"] += 1
                if isinstance(primary, StartTraining):
                    slot, tier = _oracle_facts(ctx, PROPERTY_STRATEGY, member)["band"]
                    seen[f"start {slot} T{tier}"] += 1
                if isinstance(primary, UseConsumable):
                    seen[f"{TIER_OF_ITEM[primary.item_design_id]} consumable"] += 1
                    seen[f"consumable replaced {fallback.reason_code}"] += 1
        expected = [*SKIP_CODES, "FinishTraining", "StartTraining", "UseConsumable", "Skip"]
        expected += [f"start {slot} T{tier}" for _low, _high, slot, tier in ORACLE_BANDS]
        expected += ["finish then StartTraining", "finish then Skip", "finish then UseConsumable"]
        expected += ["move to", "no training room", "no capacity", "no design", "design research", "band research"]
        expected += ["Common consumable", "Hero consumable"]
        expected += [
            f"consumable replaced {code}"
            for code in ("training_in_progress", "complete", "research_missing", "design_missing")
        ]
        expected += ["non-UTC training_end", "duplicate character id", "contended stock changed the plan"]
        for label in expected:
            with self.subTest(label=label):
                self.assertGreaterEqual(seen[label], 3, dict(seen))


if __name__ == "__main__":
    unittest.main()
