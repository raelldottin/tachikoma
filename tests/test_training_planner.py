from __future__ import annotations

"""Tests for the pure crew-training planner (sdk.training) — no network, no client."""

import ast
import dataclasses
import math
import os
import random
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sdk.crew_leveling import CAPTAIN_TP, get_tp_cap
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
    TRAINING_FINISHED,
    TRAINING_IN_PROGRESS,
    band_for,
    design_research_ok,
    effective_cap,
    highest_stat,
    percent,
    pick_consumable,
    pick_design,
    plan_training,
    research_ok,
    resolve_room_name,
    role_for,
    room_type_for,
    total_points,
    training_state,
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
    stats: dict[str, int] | None = None,
) -> CrewState:
    return CrewState(
        character_id=cid,
        name=name,
        design_id=500 + cid,
        rarity=rarity,
        level=10,
        room_id=room_id,
        room_name=room_name,
        stats={"HpImprovement": total} if stats is None else stats,
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
        with self.assertRaises(ValueError):
            mk_crew(stats={"@HpImprovement": 1})
        self.assertEqual(mk_crew(stats={"HpImprovement": 1}).stats["HpImprovement"], 1)

    def test_crew_state_rejects_naive_training_end(self) -> None:
        with self.assertRaises(ValueError):
            mk_crew(training_design_id=1, training_end=datetime(2026, 1, 1, 12, 0))
        self.assertEqual(mk_crew(training_design_id=1, training_end=NOW).training_end, NOW)

    def test_context_rejects_naive_now(self) -> None:
        with self.assertRaises(ValueError):
            TrainingContext(datetime(2026, 1, 1), (), (), {}, {}, {})
        self.assertEqual(TrainingContext(NOW, (), (), {}, {}, {}).now, NOW)

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
                band = band_for(pct)
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
        short_role = Role("short", frozenset({"Shorty"}), RoleSlot("academy", ("A", "B")), RoleSlot("gym", ("C", "D", "E")))
        with self.subTest("two-training slot"), self.assertRaises(ValueError):
            dataclasses.replace(DEFAULT_STRATEGY, roles=(*DEFAULT_STRATEGY.roles, short_role))


class TestRooms(unittest.TestCase):
    def test_room_type_none_empty(self) -> None:
        for name in (None, "", "  "):
            with self.subTest(name=name):
                self.assertIsNone(room_type_for(name))

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
                self.assertEqual(room_type_for(name), expected)

    def test_room_type_substring_false_positives(self) -> None:
        # Regression: the original matched room names by case-sensitive substring.
        names = ("Galaxy Gym Annex", "Big GYM", "gym", "Gym", "academy", "Galexy Gym", "Lunar College 2", "Bridge")
        for name in names:
            with self.subTest(name=name):
                self.assertIsNone(room_type_for(name))


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


class TestRoles(unittest.TestCase):
    def test_role_for_known_names(self) -> None:
        for role_name, characters in ROLE_CHARACTERS.items():
            for character in characters:
                with self.subTest(character=character):
                    role = role_for(character)
                    self.assertIsNotNone(role)
                    assert role is not None
                    self.assertEqual(role.name, role_name)
        rusher = role_for("Huge Hellaluya")
        self.assertEqual(rusher.name if rusher else None, "rushers")
        self.assertIsNone(role_for("Huge Hellaloya"))  # spelling regression: the original table had this typo

    def test_role_for_unknown_and_pilots_empty(self) -> None:
        self.assertIsNone(role_for(""))
        self.assertIsNone(role_for("Nobody"))
        pilots = [role for role in DEFAULT_STRATEGY.roles if role.name == "pilots"]
        self.assertEqual(len(pilots), 1)
        self.assertEqual(pilots[0].characters, frozenset())

    def test_strategy_rejects_character_in_two_roles(self) -> None:
        # Regression: the original role loop had no break, so the last matching role silently won.
        clash = Role("clash", frozenset({"Delish"}), RoleSlot("gym", ("A", "B", "C")), RoleSlot("gym", ("D", "E", "F")))
        with self.assertRaises(ValueError):
            dataclasses.replace(DEFAULT_STRATEGY, roles=(*DEFAULT_STRATEGY.roles, clash))

    def test_default_role_table_golden(self) -> None:
        gym = ("Bench Press", "Muscle Beach", "Olympic Weightlifting")
        expected = {
            "weapons": ("academy", ("Read Expert Weapon Theory", "Weapons Summit", "Weapons PHD"), "gym", gym),
            "shields": ("academy", ("Big Book of Science", "Scientific Summit", "Science PHD"), "gym", gym),
            "engines": (
                "gym",
                gym,
                "academy",
                ("Study Expert Engineering Manual", "Engineering Summit", "Engineering PHD"),
            ),
            "rushers": ("gym", ("Steam Yoga", "Crew vs Wild", "Space Marine"), "gym", gym),
            "defenders": ("gym", gym, "gym", ("Kickbox", "BBJ", "Shaolin Tradition")),
            "pilots": ("academy", ("Read Expert Pilot Handbook", "Pilot Summit", "Pilot Expert"), "gym", gym),
        }
        actual = {
            role.name: (
                role.primary.room_type,
                role.primary.trainings,
                role.secondary.room_type,
                role.secondary.trainings,
            )
            for role in DEFAULT_STRATEGY.roles
        }
        self.assertEqual(actual, expected)
        self.assertEqual(
            {role.name: role.characters for role in DEFAULT_STRATEGY.roles},
            {name: frozenset(chars) for name, chars in ROLE_CHARACTERS.items()},
        )


class TestCaps(unittest.TestCase):
    def test_effective_cap_rarity_without_table_uses_capacity(self) -> None:
        for rarity in ("Standard", "", "Common", "Elite"):
            with self.subTest(rarity=rarity):
                self.assertEqual(effective_cap(mk_crew(rarity=rarity, capacity=100)), 100)
                self.assertEqual(effective_cap(mk_crew(rarity=rarity, capacity=250)), 250)

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
                self.assertEqual(effective_cap(mk_crew(name=name, rarity=rarity, capacity=capacity)), expected)

    def test_effective_cap_special_captain_capacity_not_clamped(self) -> None:
        # Regression: a 6-star captain (design capacity 200) was clamped to the standard 6-star cap of 100, so it
        # stopped at 50% and never reached its secondary training.
        self.assertEqual(effective_cap(mk_crew(rarity="Special", capacity=200)), 200)
        self.assertEqual(effective_cap(mk_crew(rarity="Special", capacity=150)), 100)
        decisions = plan_one(mk_crew(rarity="Special", capacity=200, total=120, room_name="Academy"))
        self.assertEqual(summary(decisions), [("start", 1, "Weapons Summit")])

    def test_effective_cap_delegates_to_get_tp_cap(self) -> None:
        names = ("Delish", "Mistycball", "Huge Hellaluya")
        for rarity, stars in DEFAULT_STRATEGY.rarity_stars.items():
            for name in names:
                for capacity in (50, 100, 300):
                    with self.subTest(rarity=rarity, name=name, capacity=capacity):
                        table = get_tp_cap(stars, name, is_captain=stars == 6 and capacity >= CAPTAIN_TP)
                        expected = min(capacity, table) if table > 0 else capacity
                        crew = mk_crew(name=name, rarity=rarity, capacity=capacity)
                        self.assertEqual(effective_cap(crew), expected)


def band(slot: str, tier: int) -> TierBand:
    return next(b for b in DEFAULT_STRATEGY.bands if (b.slot, b.tier) == (slot, tier))


class TestResearch(unittest.TestCase):
    def test_research_ok_secondary_t1_ungated(self) -> None:
        self.assertTrue(research_ok(band("secondary", 1), {}))

    def test_research_gate_table(self) -> None:
        required = {("primary", 1): 1, ("primary", 2): 2, ("primary", 3): 3, ("secondary", 2): 2, ("secondary", 3): 3}
        for (slot, tier), level in required.items():
            with self.subTest(slot=slot, tier=tier):
                self.assertFalse(research_ok(band(slot, tier), {"Fitness": level - 1}))
                self.assertFalse(research_ok(band(slot, tier), {"Fitness": level - 1, "Education": level - 1}))
                self.assertTrue(research_ok(band(slot, tier), {"Fitness": level}))
                self.assertTrue(research_ok(band(slot, tier), {"Education": level}))

    def test_research_exact_key_regression(self) -> None:
        # Regression: the original matched research by name prefix (startswith of the first word).
        self.assertFalse(research_ok(band("primary", 1), {"Fitness Lv3": 3, "fitness": 3, "FitnessX": 3, "Fit": 3}))
        self.assertTrue(research_ok(band("primary", 1), {"Education": 1}))


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
        self.assertIsNone(pick_consumable(crew, 10, mk_ctx(crew, items={9001: 5}), DEFAULT_STRATEGY))
        self.assertIsNone(pick_consumable(crew, 10, mk_ctx(crew, items={9001: 0}), configured))
        self.assertIsNone(pick_consumable(crew, 10, mk_ctx(crew, items={}), configured))
        decision = pick_consumable(crew, 10, mk_ctx(crew, items={9001: 1}), configured)
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
        self.assertSkip(plan_one(mk_crew(rarity="Unique", total=70)), "tp_cap_reached", "70 TP >= cap 70 TP (70% of capacity 100)")
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
        crew = mk_crew(total=20, fatigue=0, stats={"WeaponImprovement": 20})
        decisions = plan_cons(crew, research={})
        self.assertEqual(summary(decisions), [("use", 1, CONSUMABLE_IDS[("WPN", "Common")])])

    def test_grey_never_replaces_startable(self) -> None:
        crew = mk_crew(total=20, fatigue=0, stats={"WeaponImprovement": 20})
        self.assertEqual(summary(plan_cons(crew, research=FULL_RESEARCH)), [("start", 1, "Read Expert Weapon Theory")])

    def test_grey_boundaries(self) -> None:
        grey = CONSUMABLE_IDS[("WPN", "Common")]
        cases = ((29, 0, [("use", 1, grey)]), (30, 0, [("skip", 1, "research_missing")]))
        cases += ((20, 0, [("use", 1, grey)]), (20, 1, [("skip", 1, "research_missing")]))
        for total, fatigue, expected in cases:
            with self.subTest(total=total, fatigue=fatigue):
                crew = mk_crew(total=total, fatigue=fatigue, stats={"WeaponImprovement": total})
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
                crew = mk_crew(total=total, fatigue=fatigue, room_name="GYM", stats={"WeaponImprovement": total})
                self.assertEqual(summary(plan_cons(crew, research=FULL_RESEARCH)), expected)

    def test_consumable_out_of_stock_falls_back_to_skip(self) -> None:
        empty = {item_id: 0 for item_id in CONSUMABLE_IDS.values()}
        in_progress = {"training_design_id": 1, "training_end": NOW + timedelta(hours=1)}
        cases = (
            (mk_crew(total=20, stats={"WeaponImprovement": 20}), {}, "research_missing"),
            (
                mk_crew(total=95, fatigue=100, room_name="GYM", stats={"WeaponImprovement": 95}),
                FULL_RESEARCH,
                "complete",
            ),
            (mk_crew(total=20, stats={"WeaponImprovement": 20}, **in_progress), FULL_RESEARCH, "training_in_progress"),
        )
        for crew, research, code in cases:
            for items in (empty, {}):
                with self.subTest(code=code, items=bool(items)):
                    self.assertEqual(summary(plan_cons(crew, items=items, research=research)), [("skip", 1, code)])

    def test_consumable_targets_highest_stat_rpr(self) -> None:
        # Regression: the original took the first non-zero stat and mapped Repair to STA.
        stats = {"HpImprovement": 2, "RepairImprovement": 9, "StaminaImprovement": 5}
        crew = mk_crew(total=16, stats=stats)
        self.assertEqual(summary(plan_cons(crew, research={})), [("use", 1, CONSUMABLE_IDS[("RPR", "Common")])])

    def test_grey_during_in_progress(self) -> None:
        crew = mk_crew(
            total=10,
            stats={"PilotImprovement": 10},
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
                crew = mk_crew(rarity="Unique", total=total, stats={"WeaponImprovement": total}, fatigue=100)
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
        crew = mk_crew(total=20, room_name="GYM", stats={"WeaponImprovement": 20})
        self.assertEqual(summary(plan_cons(crew, research={})), [("skip", 1, "not_in_training_room")])

    def test_consumable_fallback_when_design_missing(self) -> None:
        crew = mk_crew(total=20, stats={"WeaponImprovement": 20})
        decisions = plan_cons(crew, research=FULL_RESEARCH, designs=())
        self.assertEqual(summary(decisions), [("use", 1, CONSUMABLE_IDS[("WPN", "Common")])])
        gated = (mk_design(5, "Read Expert Weapon Theory", required_research="Advanced Training"),)
        decisions = plan_cons(crew, research=FULL_RESEARCH, designs=gated)
        self.assertEqual(summary(decisions), [("use", 1, CONSUMABLE_IDS[("WPN", "Common")])])

    def test_fatigued_fallback_with_custom_rule(self) -> None:
        # Unreachable with the default rules (grey needs fatigue 0); rules are data, so the path stays.
        rule = ConsumableRule("Hero", min_percent=0, max_percent=None, min_fatigue=100, max_fatigue=None)
        strategy = dataclasses.replace(CONSUMABLE_STRATEGY, consumable_rules=(rule,))
        crew = mk_crew(total=40, fatigue=100, stats={"ScienceImprovement": 40})
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

    def test_plan_deterministic_and_ctx_unchanged(self) -> None:
        crew = (mk_crew(1, "Delish"), mk_crew(2, "Cyber Duck", room_name="GYM", total=80))
        ctx = mk_ctx(*crew, designs=ALL_DESIGNS, research=FULL_RESEARCH, rooms={1: "GYM"}, items={1: 1})
        snapshot = repr(ctx)
        first = plan_training(ctx)
        second = plan_training(ctx)
        self.assertEqual(first, second)
        self.assertIsInstance(first, tuple)
        self.assertEqual(repr(ctx), snapshot)

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
        allowed |= {"sdk.crew_leveling"}
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
                        self.assertTrue(module in allowed or module.startswith("sdk.training."), module)

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
# Property suite: generator, independent invariant checker, and the seeds.
# Module-level functions (no TestCase) so other test modules may import them.
# ---------------------------------------------------------------------------


def _startable_design(ctx: TrainingContext, strategy, crew: CrewState) -> TrainingDesign | None:
    """Independent re-derivation (public helpers only) of the design the crew could start now, if any."""
    role = role_for(crew.name, strategy)
    room_type = room_type_for(resolve_room_name(crew, ctx), strategy)
    if role is None or room_type is None or crew.training_capacity <= 0:
        return None
    total = total_points(crew.stats)
    if total >= effective_cap(crew, strategy) or training_state(crew, ctx.now) == TRAINING_IN_PROGRESS:
        return None
    if crew.fatigue >= strategy.max_training_fatigue:
        return None
    found = band_for(percent(total, crew.training_capacity), strategy)
    if found is None:
        return None
    slot = role.slot(found.slot)
    if slot.room_type != room_type or not research_ok(found, ctx.research_levels, strategy):
        return None
    design = pick_design(slot.trainings[found.tier - 1], room_type, ctx.designs)
    if design is None or not design_research_ok(design, ctx.research_levels):
        return None
    return design


def _skip_consistent(ctx: TrainingContext, strategy, crew: CrewState, code: str) -> bool:
    role = role_for(crew.name, strategy)
    room_type = room_type_for(resolve_room_name(crew, ctx), strategy)
    total = total_points(crew.stats)
    pct = percent(total, crew.training_capacity) if crew.training_capacity > 0 else None
    found = band_for(pct, strategy) if pct is not None else None
    slot = role.slot(found.slot) if role is not None and found is not None else None
    design = (
        pick_design(slot.trainings[found.tier - 1], room_type, ctx.designs) if slot and found and room_type else None
    )
    if code == "unknown_role":
        return role is None
    if code == "not_in_training_room":
        return room_type is None or (slot is not None and slot.room_type != room_type)
    if code == "design_missing":
        return crew.training_capacity <= 0 or (slot is not None and design is None)
    if code == "tp_cap_reached":
        return crew.training_capacity > 0 and total >= effective_cap(crew, strategy)
    if code == "training_in_progress":
        return training_state(crew, ctx.now) == TRAINING_IN_PROGRESS
    if code == "complete":
        return pct is not None and pct >= strategy.complete_percent
    if code == "research_missing":
        return found is not None and (
            not research_ok(found, ctx.research_levels, strategy)
            or (design is not None and not design_research_ok(design, ctx.research_levels))
        )
    if code == "fatigued":
        return crew.fatigue >= strategy.max_training_fatigue
    return False


def _primary_violations(ctx: TrainingContext, strategy, crew: CrewState, decision) -> list[str]:
    cid = crew.character_id
    problems: list[str] = []
    role = role_for(crew.name, strategy)
    room_type = room_type_for(resolve_room_name(crew, ctx), strategy)
    total = total_points(crew.stats)
    eligible = role is not None and room_type is not None and crew.training_capacity > 0
    eligible = eligible and total < effective_cap(crew, strategy)
    startable = _startable_design(ctx, strategy, crew)
    designs_by_id = {design.id: design for design in ctx.designs}
    if isinstance(decision, StartTraining):
        design = designs_by_id.get(decision.training_design_id)
        if design is None:
            problems.append(f"{cid}: start with design id {decision.training_design_id} not in ctx")
        if not eligible:
            problems.append(f"{cid}: start for an ineligible crew (role/room/capacity/cap)")
        if training_state(crew, ctx.now) == TRAINING_IN_PROGRESS:
            problems.append(f"{cid}: start while training in progress")
        if crew.fatigue >= strategy.max_training_fatigue:
            problems.append(f"{cid}: start while fatigued")
        if startable is None or design is None or design.id != startable.id:
            problems.append(f"{cid}: start does not match the startable design {startable}")
    elif isinstance(decision, UseConsumable):
        if not eligible:
            problems.append(f"{cid}: consumable for an ineligible crew")
        if ctx.items.get(decision.item_design_id, 0) <= 0:
            problems.append(f"{cid}: consumable {decision.item_design_id} out of stock")
        if decision.item_design_id not in set(strategy.consumables.values()):
            problems.append(f"{cid}: consumable {decision.item_design_id} not configured")
        if startable is not None:
            problems.append(f"{cid}: consumable used instead of a startable training")
        pct = percent(total, crew.training_capacity) if crew.training_capacity > 0 else 0
        found = band_for(pct, strategy)
        # Moving rooms only matters once the crew is free; a running training may take a consumable anywhere.
        free = training_state(crew, ctx.now) != TRAINING_IN_PROGRESS
        if free and role is not None and found is not None and role.slot(found.slot).room_type != room_type:
            problems.append(f"{cid}: consumable used instead of moving rooms")
    elif isinstance(decision, Skip):
        if startable is not None:
            problems.append(f"{cid}: skipped ({decision.reason_code}) a startable training")
        if not _skip_consistent(ctx, strategy, crew, decision.reason_code):
            problems.append(f"{cid}: skip code {decision.reason_code} inconsistent with crew state")
    else:
        problems.append(f"{cid}: unexpected primary decision {decision!r}")
    return problems


def plan_violations(ctx: TrainingContext, strategy, decisions) -> list[str]:
    """Check plan invariants independently of the planner's control flow."""
    problems: list[str] = []
    remaining = list(decisions)
    position = 0
    for crew in ctx.crew:
        cid = crew.character_id
        finished = training_state(crew, ctx.now) == TRAINING_FINISHED
        has_finish = (
            position < len(remaining)
            and isinstance(remaining[position], FinishTraining)
            and remaining[position].character_id == cid
        )
        if has_finish:
            position += 1
            if not finished:
                problems.append(f"{cid}: finish while training is not finished")
        elif finished:
            problems.append(f"{cid}: finished training not collected first")
        if position >= len(remaining):
            problems.append(f"{cid}: missing primary decision")
            continue
        primary = remaining[position]
        if isinstance(primary, FinishTraining) or primary.character_id != cid:
            problems.append(f"{cid}: expected a primary decision, found {primary!r}")
            continue
        position += 1
        problems.extend(_primary_violations(ctx, strategy, crew, primary))
    if position != len(remaining):
        problems.append(f"{len(remaining) - position} decisions after the last crew's primary")
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
    no_research = mk_crew(1, total=10, stats={"WeaponImprovement": 10})
    second = mk_crew(2, "Cyber Duck", room_name="GYM")

    def ctx(*crew: CrewState, items=None, research=FULL_RESEARCH) -> TrainingContext:
        return mk_ctx(*crew, designs=ALL_DESIGNS, research=research, items=items)

    start = StartTraining(1, weapon_id, "r")
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
        (
            "wrong crew order",
            ctx(idle, second),
            (StartTraining(2, DESIGN_IDS["Steam Yoga"], "r"), start),
        ),
    ]


BASE_NOW = datetime(2026, 1, 1, tzinfo=UTC)
EDGE_PERCENTS = (0, 29, 30, 50, 51, 64, 65, 71, 72, 73, 74, 84, 85, 89, 90, 95)
GEN_ROOMS = ("Academy", "Lunar College", "GYM", "Galaxy Gym", "Galaxy Gym Annex", "gym", "Bridge", None)
GEN_CAPACITIES = (20, 100, 150, 200, 300)
GEN_RARITIES = (*DEFAULT_STRATEGY.rarity_stars, "Standard")
GEN_ROLE_NAMES = tuple(sorted(name for role in DEFAULT_STRATEGY.roles for name in role.characters))
PROPERTY_STRATEGY = with_consumables(DEFAULT_STRATEGY, CONSUMABLE_IDS)
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
            research = "Advanced Training" if rng.random() < 0.10 else None
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
    room = rng.choice(GEN_ROOMS)
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
    capacity = 0 if rng.random() < 0.05 else rng.choice(GEN_CAPACITIES)
    target = rng.choice(EDGE_PERCENTS) if rng.random() < 0.5 else rng.randint(0, 110)
    total = max(0, target * max(capacity, 1) // 100 + rng.choice((-1, 0, 0, 1)))
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
        training_end=training[1],
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
    research = {name: rng.randint(0, 3) for name in ("Fitness", "Education", "Advanced Training")}
    items = {item_id: rng.choice((0, 0, 1, 3)) for item_id in sorted(strategy.consumables.values())}
    return TrainingContext(now, tuple(crew), designs, rooms, research, items)


class TestPlannerProperties(unittest.TestCase):
    def test_violation_checker_catches_bad_plans(self) -> None:
        for label, ctx, bad_plan in _bad_plan_cases():
            with self.subTest(label):
                self.assertEqual(plan_violations(ctx, CONSUMABLE_STRATEGY, plan_training(ctx, CONSUMABLE_STRATEGY)), [])
                self.assertNotEqual(plan_violations(ctx, CONSUMABLE_STRATEGY, bad_plan), [])

    def test_generator_seed_deterministic(self) -> None:
        self.assertEqual(make_context(random.Random(7)), make_context(random.Random(7)))
        self.assertNotEqual(make_context(random.Random(7)), make_context(random.Random(8)))

    def test_invariants_hold_500_seeds(self) -> None:
        for seed in SEEDS:
            ctx = make_context(random.Random(seed))
            with self.subTest(seed=seed):
                self.assertEqual(plan_violations(ctx, PROPERTY_STRATEGY, plan_training(ctx, PROPERTY_STRATEGY)), [])

    def test_plan_composes_per_crew(self) -> None:
        for seed in SEEDS:
            ctx = make_context(random.Random(seed))
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
        skip_codes: Counter[str] = Counter()
        decision_types: Counter[str] = Counter()
        start_bands: Counter[tuple[str, int]] = Counter()
        finish_pairs: Counter[str] = Counter()
        for seed in SEEDS:
            ctx = make_context(random.Random(seed))
            decisions = plan_training(ctx, PROPERTY_STRATEGY)
            crew_by_id = {member.character_id: member for member in ctx.crew}
            for index, decision in enumerate(decisions):
                decision_types[type(decision).__name__] += 1
                if isinstance(decision, Skip):
                    skip_codes[decision.reason_code] += 1
                if isinstance(decision, StartTraining):
                    member = crew_by_id[decision.character_id]
                    found = band_for(percent(total_points(member.stats), member.training_capacity))
                    assert found is not None
                    start_bands[(found.slot, found.tier)] += 1
                if isinstance(decision, FinishTraining):
                    finish_pairs[type(decisions[index + 1]).__name__] += 1
        for code in SKIP_CODES:
            with self.subTest(code=code):
                self.assertGreaterEqual(skip_codes[code], 5, dict(skip_codes))
        self.assertEqual(set(decision_types), {"FinishTraining", "StartTraining", "UseConsumable", "Skip"})
        self.assertEqual(set(start_bands), {(band.slot, band.tier) for band in DEFAULT_STRATEGY.bands})
        self.assertGreater(finish_pairs["StartTraining"], 0)
        self.assertGreater(finish_pairs["Skip"], 0)


if __name__ == "__main__":
    unittest.main()
