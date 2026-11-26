"""Tests for the crew-training simulator benchmark (scripts/training_benchmark.py).

Stdlib unittest, no network: every episode runs in the offline simulator (sdk.simulation.game). Benchmark mechanics
are tested with scripted stub planners on tiny 1-day scenarios; the real planner appears only where a test needs it
(catalog coverage, the weakened-planner and determinism tests). The checked-in baseline gate is never run here: CI
runs `make training-benchmark`.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import sdk.crew_leveling as crew_leveling
from scripts import mutation
from scripts import training_benchmark as bench
from sdk.training import build_context
from sdk.training.parse import parse_consumable_designs
from sdk.training.planner import pick_design
from sdk.training.strategy import DEFAULT_ROLES, DEFAULT_STRATEGY, RARITY_STARS, STAT_CODES

T0 = datetime(2027, 7, 5, tzinfo=timezone.utc)


def hours(value: float) -> datetime:
    return T0 + timedelta(hours=value)


ALL_RESEARCH = ["Fitness", "Fitness Lv2", "Fitness Lv3", "Education", "Education Lv2", "Education Lv3"]


def mini(**overrides) -> dict:
    """A valid 1-day scenario: GYM 7001, research Fitness, one captain Admiral Serena at 0 TP."""
    data = {
        "schema_version": 1,
        "name": "mini",
        "description": "One untrained captain in a GYM.",
        "tags": ["mini"],
        "seed": 7,
        "start": "2027-07-05T00:00:00+00:00",
        "days": 1,
        "tick_minutes": 60,
        "rooms": [{"id": 7001, "design": "GYM"}],
        "research": ["Fitness"],
        "inventory": {},
        "crew": [{"id": 101, "name": "Admiral Serena", "design": "captain", "room": 7001}],
        "faults": [],
    }
    data.update(overrides)
    return data


def captain(**changes) -> dict:
    return dict(mini()["crew"][0], **changes)


def observe_context(data: dict):
    scenario = bench.validate_scenario(data)
    world = bench.build_world(scenario)
    env = bench.SimEnvironment(world, scenario, scenario.seed)
    return world, env, build_context(now=env.now(), **env.observe())


class PureHelperTests(unittest.TestCase):
    def test_percent_is_exact_ceiling(self):
        self.assertEqual(bench.percent(7, 100), 7)
        self.assertEqual(bench.percent(55, 100), 55)
        self.assertEqual(bench.percent(1, 3), 34)
        self.assertEqual(bench.percent(0, 50), 0)
        self.assertEqual(bench.percent(90, 100), 90)

    def test_oracle_band_edges(self):
        cases = {
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
            120: None,
        }
        for pct, expected in cases.items():
            with self.subTest(pct=pct):
                self.assertEqual(bench.oracle_band(pct), expected)

    def test_oracle_cap_cases(self):
        cases = [
            ("Unique", "Huntress", 100, 70),
            ("Epic", "Mistycball", 100, 100),
            ("Epic", "Huge Hellaluya", 100, 100),
            ("Epic", "Delish", 100, 80),
            ("Epic", "Mistycball", 90, 90),
            ("Hero", "Cyber Duck", 120, 90),
            ("Special", "Huntress", 120, 100),
            ("Special", "Admiral Serena", 200, 200),
            ("Special", "Admiral Serena", 199, 100),
            ("Legendary", "King Dong", 150, 110),
            ("Common", "Visiting Trader", 60, 60),
            ("Elite", "Cyber Duck", 100, 100),
            ("Standard", "Nobody", 50, 50),
            ("Unique", "Huntress", 60, 60),
        ]
        for rarity, name, capacity, expected in cases:
            with self.subTest(rarity=rarity, name=name, capacity=capacity):
                self.assertEqual(bench.oracle_cap(rarity, name, capacity), expected)

    def test_consumable_allowed_windows(self):
        cases = [
            ("Common", 0, 0, True),
            ("Common", 29, 0, True),
            ("Common", 30, 0, False),
            ("Common", 29, 1, False),
            ("Hero", 90, 100, True),
            ("Hero", 95, 100, True),
            ("Hero", 89, 100, False),
            ("Hero", 90, 99, False),
            ("Elite", 0, 0, False),
        ]
        for tier, pct, fatigue, expected in cases:
            with self.subTest(tier=tier, pct=pct, fatigue=fatigue):
                self.assertIs(bench.consumable_allowed(tier, pct, fatigue), expected)

    def test_gate_met_and_research_levels(self):
        completed = [
            {"@ResearchDesignId": "2010", "@ResearchState": "Completed"},
            {"@ResearchDesignId": "2011", "@ResearchState": "Completed"},
            {"@ResearchDesignId": "2012", "@ResearchState": "Researching"},
            {"@ResearchDesignId": "9999", "@ResearchState": "Completed"},
        ]
        self.assertEqual(bench.research_levels(completed), {"Fitness": 2})
        self.assertEqual(bench.research_levels([]), {})
        self.assertTrue(bench.gate_met(("primary", 1), {"Fitness": 1}))
        self.assertFalse(bench.gate_met(("primary", 2), {"Fitness": 1}))
        self.assertTrue(bench.gate_met(("primary", 2), {"Education": 2}))
        self.assertFalse(bench.gate_met(("primary", 1), {}))
        self.assertTrue(bench.gate_met(("secondary", 1), {}))
        self.assertFalse(bench.gate_met(("secondary", 2), {"Fitness": 1}))
        for band in bench.ORACLE_GATES:
            with self.subTest(band=band):
                self.assertTrue(bench.gate_met(band, {"Education": 3}))

    def test_idle_seconds_exact(self):
        window = (hours(0), hours(10))
        sessions = [(hours(1), hours(3)), (hours(3), hours(4)), (hours(6), hours(12))]
        self.assertEqual(bench.idle_seconds(window, sessions), 3 * 3600)
        self.assertEqual(bench.idle_seconds(window, []), 10 * 3600)
        self.assertEqual(bench.idle_seconds((hours(5), hours(5)), sessions), 0)
        overlapping = [(hours(1), hours(5)), (hours(2), hours(3)), (hours(4), hours(6))]
        self.assertEqual(bench.idle_seconds(window, overlapping), 5 * 3600)
        before = [(hours(-3), hours(-1)), (hours(-1), hours(2))]
        self.assertEqual(bench.idle_seconds(window, before), 8 * 3600)

    def test_progress_auc(self):
        headroom = {1: 10, 2: 0}
        initial = {1: 0, 2: 5}
        totals = [{1: 2, 2: 7}, {1: 6, 2: 9}, {1: 12, 2: 9}]
        self.assertEqual(bench.progress_auc(headroom, initial, totals), 0.6)
        self.assertEqual(bench.progress_auc({1: 4}, {1: 2}, [{1: 1}, {1: 4}]), 0.25)
        self.assertEqual(bench.progress_auc({1: 0}, {1: 0}, [{1: 3}]), 0.0)

    def test_focus_ratio(self):
        roles = {1: "weapons", 2: None, 3: "defenders"}
        gains = {1: {"Weapon": 6, "Pilot": 2}, 2: {"Attack": 10}}
        self.assertEqual(bench.focus_ratio(roles, gains), 0.75)
        self.assertEqual(bench.focus_ratio(roles, {}), 0.0)
        self.assertEqual(bench.focus_ratio({3: "defenders"}, {3: {"Attack": 3, "Hp": 1}}), 1.0)
        self.assertEqual(bench.focus_ratio({3: "rushers"}, {3: {"Hp": 4}}), 0.0)

    def test_score_formula(self):
        base = bench.Metrics(progress=0.5, utilisation=0.8, focus=1.0)
        self.assertEqual(base.reliability, 1.0)
        self.assertEqual(bench.score_scenario(base), 62.5)
        refused = bench.Metrics(progress=0.5, utilisation=0.8, focus=1.0, accepted_actions=3, refused_actions=1)
        self.assertEqual(refused.reliability, 0.75)
        self.assertEqual(bench.score_scenario(refused), 46.875)
        for kind in ("cap_violations", "consumable_violations", "research_gate_violations"):
            with self.subTest(kind=kind):
                self.assertEqual(bench.score_scenario(bench.Metrics(progress=0.5, utilisation=0.8, **{kind: 1})), 0.0)
        self.assertEqual(bench.score_scenario(None), 0.0)
        thirds = bench.Metrics(progress=1 / 3, utilisation=1 / 3, focus=1 / 3)
        self.assertEqual(bench.score_scenario(thirds), 33.333333)
        weights = bench.Metrics(progress=0.0, utilisation=1.0, focus=0.0)
        self.assertEqual(bench.score_scenario(weights), 25.0)
        self.assertEqual(bench.score_scenario(bench.Metrics(progress=0.0, utilisation=0.0, focus=1.0)), 10.0)
        self.assertEqual(bench.score_scenario(bench.Metrics(progress=1.0, utilisation=0.0, focus=0.0)), 65.0)

    def test_composite_mean_rounded(self):
        self.assertEqual(bench.composite([10.0, 20.0, 40.0]), 23.333333)
        self.assertEqual(bench.composite([50.0]), 50.0)
        self.assertEqual(bench.composite([]), 0.0)


class CatalogWorldScenarioTests(unittest.TestCase):
    def test_catalog_parses_for_build_context_and_simulator(self):
        rooms = [
            {"id": 7001, "design": "GYM"},
            {"id": 7002, "design": "Academy"},
            {"id": 7003, "design": "Galaxy Gym"},
            {"id": 7004, "design": "Lunar College"},
            {"id": 7005, "design": "Bridge"},
        ]
        world, env, ctx = observe_context(mini(rooms=rooms, research=["Fitness"]))
        self.assertEqual(len(ctx.designs), 21)
        expected_rooms = {7001: "GYM", 7002: "Academy", 7003: "Galaxy Gym", 7004: "Lunar College", 7005: "Bridge"}
        self.assertEqual(dict(ctx.rooms), expected_rooms)
        self.assertEqual(ctx.research_levels["Fitness"], 1)
        self.assertNotIn("Education", ctx.research_levels)
        for role in DEFAULT_ROLES:
            for slot in (role.primary, role.secondary):
                for name in slot.trainings:
                    with self.subTest(role=role.name, training=name):
                        self.assertIsNotNone(pick_design(name, slot.room_type, ctx.designs))
        by_id = {design.id: design for design in ctx.designs}
        self.assertEqual(by_id[3011].name, "Read Expert Weapon Theory")
        self.assertEqual((by_id[3011].tier, by_id[3011].stat), (1, "WeaponImprovement"))
        self.assertEqual(by_id[3011].required_room_type, "academy")
        self.assertIsNone(by_id[3011].required_research)
        self.assertEqual(by_id[3112].required_research, "Fitness Lv2")
        self.assertEqual(by_id[3023].required_research, "Education Lv3")
        self.assertEqual(by_id[3133].duration, timedelta(hours=6))
        (crew,) = ctx.crew
        observed = (crew.name, crew.rarity, crew.training_capacity, crew.room_name)
        self.assertEqual(observed, ("Admiral Serena", "Special", 200, "GYM"))
        self.assertEqual(world.crew[101].cap, 200)
        self.assertTrue(world.crew[101].eligible)
        self.assertEqual(world.crew[101].headroom, 200)
        self.assertEqual(env.call("addTraining", ("3111", "101")).status, "ok")
        self.assertEqual(env.call("addTraining", ("3011", "101")).status, "refused")

    def test_world_consumable_mapping_self_check(self):
        world, _, ctx = observe_context(mini(inventory={"Common ATK Consumable": 2, "Hero HP Consumable": 1}))
        item_designs = world.sim_scenario["training"]["item_designs"]
        self.assertEqual(dict(parse_consumable_designs({"ItemDesign": item_designs})), bench.CATALOG_CONSUMABLES)
        self.assertEqual(dict(ctx.items), {5001: 2, 5016: 1})
        self.assertEqual(len(bench.CATALOG_CONSUMABLES), 12)
        renamed = dict(bench.CATALOG_ITEMS)
        renamed["Common Gizmo"] = renamed.pop("Common ATK Consumable")
        with mock.patch.object(bench, "CATALOG_ITEMS", renamed):
            with self.assertRaisesRegex(bench.WorldError, "consumable"):
                bench.build_world(bench.validate_scenario(mini()))

    def test_validate_scenario_rejects_each_bad_field(self):
        def without(key):
            data = mini()
            del data[key]
            return data

        session = {"design": "Nap", "ends_after_minutes": 5}
        cases = {
            "unknown key": (mini(bogus=1), "bogus"),
            "missing seed": (without("seed"), "seed"),
            "days 0": (mini(days=0), "days"),
            "days 31": (mini(days=31), "days"),
            "tick 7": (mini(tick_minutes=7), "tick_minutes"),
            "tick 480": (mini(tick_minutes=480), "tick_minutes"),
            "bad name": (mini(name="Mini Scenario"), "name"),
            "empty description": (mini(description=" "), "description"),
            "duplicate tags": (mini(tags=["a", "a"]), "tags"),
            "negative seed": (mini(seed=-1), "seed"),
            "recovery 0": (mini(fatigue_recovery_seconds=0), "fatigue_recovery_seconds"),
            "unknown room design": (mini(rooms=[{"id": 7001, "design": "Kitchen"}]), "rooms"),
            "duplicate room id": (
                mini(rooms=[{"id": 7001, "design": "GYM"}, {"id": 7001, "design": "Bridge"}]),
                "rooms",
            ),
            "crew room undeclared": (mini(crew=[captain(room=7999)]), "room"),
            "unknown research": (mini(research=["Fitness Lv9"]), "research"),
            "unknown item": (mini(inventory={"Mystery Box": 1}), "inventory"),
            "negative quantity": (mini(inventory={"Common ATK Consumable": -1}), "inventory"),
            "bad fault kind": (mini(faults=[{"kind": "concurrent-spend", "method": "addTraining"}]), "kind"),
            "bad fault method": (mini(faults=[{"kind": "reject", "method": "collectAllResources"}]), "method"),
            "effect on reject": (mini(faults=[{"kind": "reject", "method": "addTraining", "effect": False}]), "effect"),
            "unknown training design": (
                mini(faults=[{"kind": "contradicted-effect", "method": "addTraining", "training_design": "Nap"}]),
                "training_design",
            ),
            "contradicted without design": (
                mini(faults=[{"kind": "contradicted-effect", "method": "addTraining"}]),
                "training_design",
            ),
            "stats over capacity": (mini(crew=[captain(design="common", stats={"Attack": 61})]), "stats"),
            "unknown stat": (mini(crew=[captain(stats={"Luck": 1})]), "stats"),
            "duplicate crew id": (mini(crew=[captain(), captain(name="Huntress")]), "id"),
            "unknown crew design": (mini(crew=[captain(design="mythic")]), "design"),
            "fatigue 101": (mini(crew=[captain(fatigue=101)]), "fatigue"),
            "unknown crew key": (mini(crew=[captain(mood="calm")]), "mood"),
            "unknown session design": (mini(crew=[captain(training=session)]), "training"),
            "empty crew": (mini(crew=[]), "crew"),
            "naive start": (mini(start="2027-07-05T00:00:00"), "start"),
            "bad start": (mini(start="soon"), "start"),
            "schema 2": (mini(schema_version=2), "schema_version"),
        }
        for name, (data, needle) in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(bench.ScenarioError) as raised:
                    bench.validate_scenario(data, source="case.json")
                self.assertIn(needle, str(raised.exception))
                self.assertIn("case.json", str(raised.exception))
        with self.assertRaises(bench.ScenarioError) as raised:
            bench.validate_scenario(mini(days=0, seed="x"))
        self.assertEqual(len(raised.exception.problems), 2)
        loaded = {"design": "Bench Press", "ends_after_minutes": 30}
        scenario = bench.validate_scenario(mini(crew=[captain(stats={"Attack": 5}, fatigue=3, training=loaded)]))
        self.assertEqual(scenario.tick_minutes, 60)
        self.assertEqual(scenario.fatigue_recovery_seconds, 360)
        self.assertEqual(scenario.start, T0)
        self.assertEqual(scenario.crew[0].training, ("Bench Press", 30))
        self.assertEqual(scenario.crew[0].fatigue, 3)
        self.assertEqual(dict(scenario.crew[0].stats), {"Attack": 5})
        self.assertEqual(bench.validate_scenario(without("tick_minutes")).tick_minutes, 60)

    def test_build_world_requires_headroom(self):
        with self.assertRaisesRegex(bench.WorldError, "headroom"):
            bench.build_world(bench.validate_scenario(mini(crew=[captain(stats={"Attack": 200})])))
        nobody = mini(crew=[{"id": 101, "name": "Visiting Trader", "design": "common", "room": 7001}])
        with self.assertRaisesRegex(bench.WorldError, "headroom"):
            bench.build_world(bench.validate_scenario(nobody))
        world = bench.build_world(bench.validate_scenario(mini(crew=[captain(stats={"Attack": 150})])))
        self.assertEqual(world.crew[101].headroom, 50)
        self.assertEqual(world.horizon_end, T0 + timedelta(days=1))

    def test_oracle_tables_agree_with_policy_sources(self):
        self.assertEqual(bench.ORACLE_TP_CAPS, dict(crew_leveling.TP_CAPS_BY_RARITY))
        self.assertEqual(bench.ORACLE_TP_CAPS, dict(DEFAULT_STRATEGY.tp_caps))
        exceptional = {(4, name): crew_leveling.EXCEPTIONAL_4STAR_TP for name in crew_leveling.EXCEPTIONAL_4STAR_NAMES}
        self.assertEqual(bench.ORACLE_EXCEPTIONAL_CAPS, exceptional)
        self.assertEqual(bench.ORACLE_EXCEPTIONAL_CAPS, dict(DEFAULT_STRATEGY.exceptional_tp_caps))
        self.assertEqual(bench.ORACLE_CAPTAIN_CAPS, {6: crew_leveling.CAPTAIN_TP})
        self.assertEqual(bench.ORACLE_CAPTAIN_CAPS, dict(DEFAULT_STRATEGY.captain_tp_caps))
        self.assertEqual(bench.ORACLE_RARITY_STARS, dict(RARITY_STARS))
        self.assertEqual(bench.ORACLE_RARITY_STARS, dict(DEFAULT_STRATEGY.rarity_stars))
        bands = tuple((band.low, band.high, band.slot, band.tier) for band in DEFAULT_STRATEGY.bands)
        self.assertEqual(bench.ORACLE_BANDS, bands)
        self.assertEqual(bench.ORACLE_COMPLETE_PERCENT, DEFAULT_STRATEGY.complete_percent)
        self.assertEqual(bench.ORACLE_GATES, dict(DEFAULT_STRATEGY.research_gates))
        rules = {
            rule.tier: (rule.min_percent, rule.max_percent, rule.min_fatigue, rule.max_fatigue)
            for rule in DEFAULT_STRATEGY.consumable_rules
        }
        self.assertEqual(bench.ORACLE_CONSUMABLE_RULES, rules)
        roles = {name: role.name for role in DEFAULT_ROLES for name in role.characters}
        self.assertEqual(bench.ORACLE_ROLES, roles)
        training_rooms = {name: kind for name, (_, kind, _) in bench.CATALOG_ROOMS.items() if kind is not None}
        self.assertEqual(dict(DEFAULT_STRATEGY.room_types), training_rooms)
        for role in DEFAULT_ROLES:
            for index, slot in enumerate((role.primary, role.secondary)):
                stats = set()
                for name in slot.trainings:
                    _, stat, room, _ = bench.CATALOG_TRAININGS[name]
                    stats.add(stat)
                    self.assertEqual(bench.CATALOG_ROOMS[room][1], slot.room_type, name)
                self.assertEqual(len(stats), 1, (role.name, slot))
                if role.characters:
                    self.assertEqual(bench.ORACLE_FOCUS[role.name][index], stats.pop())
        self.assertEqual(set(bench.ORACLE_FOCUS), {role.name for role in DEFAULT_ROLES if role.characters})
        codes = set(STAT_CODES.values())
        item_ids = {entry[0] for entry in bench.CATALOG_ITEMS.values()}
        for (code, tier), item_id in bench.CATALOG_CONSUMABLES.items():
            self.assertIn(code, codes)
            self.assertIn(tier, bench.ORACLE_CONSUMABLE_RULES)
            self.assertIn(item_id, item_ids)
        levels = {(family, level) for family in ("Fitness", "Education") for level in (1, 2, 3)}
        self.assertEqual(set(bench.CATALOG_RESEARCH_LEVELS.values()), levels)


FOUR_CREW = [
    {"id": 1, "name": "Admiral Serena", "design": "captain", "room": 7001},
    {"id": 2, "name": "King Dong", "design": "legendary", "room": 7001},
    {"id": 3, "name": "Huntress", "design": "unique", "room": 7001},
    {"id": 4, "name": "Visiting Trader", "design": "common", "room": 7001},
]


class FakeEnvironment:
    """Scripted outcomes over payloads observed once from a real world; records every call."""

    def __init__(self, outcomes=None, fail_observe=False, inventory=None):
        _, env, _ = observe_context(mini(crew=FOUR_CREW, inventory=inventory or {"Common ATK Consumable": 3}))
        self.payloads = env.observe()
        record = dict(env.game.training.record("1", env.now()), **{"@AttackImprovement": "9"})
        self.finish = {"TrainingService": {"FinishTraining": {"Character": record}}}
        self.outcomes = dict(outcomes or {})
        self.fail_observe = fail_observe
        self.calls = []

    def now(self):
        return T0

    def observe(self):
        if self.fail_observe:
            raise bench.ObservationError("injected read failure")
        return self.payloads

    def call(self, method, args):
        self.calls.append((method, args))
        return bench.CallOutcome(self.outcomes.get(method, "ok"))

    def finish_response(self):
        return self.finish


def scripted(*decisions):
    calls = []

    def planner(ctx, strategy):
        calls.append(ctx)
        return tuple(decisions)

    planner.calls = calls
    return planner


def replanner_returning(*decisions):
    calls = []
    queue = list(decisions)

    def replanner(ctx, crew, strategy, items):
        calls.append((crew, dict(items)))
        return (queue.pop(0),)

    replanner.calls = calls
    return replanner


class EnvironmentTickTests(unittest.TestCase):
    def test_sim_environment_call_outcomes(self):
        faults = [
            {"kind": "reject", "method": "addTraining", "call": 1},
            {"kind": "raise", "method": "addTraining", "call": 2},
            {"kind": "ambiguous-response", "method": "finishTraining", "call": 1},
            {"kind": "reject", "method": "activateItem3", "call": 1},
        ]
        _, env, _ = observe_context(mini(faults=faults, inventory={"Common ATK Consumable": 2}))
        rejected = env.call("addTraining", ("3111", "101"))
        self.assertEqual(rejected, bench.CallOutcome("refused", "", ("day 0 addTraining #1: reject",)))
        raised = env.call("addTraining", ("3111", "101"))
        self.assertEqual((raised.status, raised.faults), ("raised", ("day 0 addTraining #2: raise",)))
        self.assertEqual(env.call("addTraining", ("3111", "101")), bench.CallOutcome("refused"))
        env.advance(timedelta(hours=1))
        ambiguous = env.call("finishTraining", ("101",))
        self.assertEqual(ambiguous.status, "ambiguous")
        self.assertEqual(env.call("addTraining", ("3111", "101")), bench.CallOutcome("ok"))
        self.assertEqual(env.call("useConsumable", ("5001", "101")).status, "refused")
        self.assertEqual(env.call("useConsumable", ("5001", "101")), bench.CallOutcome("ok"))
        self.assertEqual(env.call("useConsumable", ("5099", "101")), bench.CallOutcome("refused"))
        with self.assertRaisesRegex(bench.BenchmarkError, "collectAllResources"):
            env.call("collectAllResources", ())
        self.assertEqual(len(env.fired()), 4)
        # A rejected call never reaches the state machine: the log starts with the raised (effective) start.
        log = env.training_log()
        self.assertEqual([(entry["op"], entry["code"]) for entry in log][:4],
                         [("start", "ok"), ("start", "busy"), ("finish", "ok"), ("start", "ok")])

    def test_run_tick_maps_decisions_to_catalog_calls(self):
        env = FakeEnvironment()
        planner = scripted(
            bench.FinishTraining(1, "ended"),
            bench.StartTraining(1, 3111, "advisory"),
            bench.StartTraining(2, 3112, "start"),
            bench.UseConsumable(3, 5001, "use"),
            bench.Skip(4, "unknown_role", "no role"),
        )
        replanner = replanner_returning(bench.StartTraining(1, 3113, "replanned"))
        recorder = bench.Recorder()
        bench.run_tick(env, planner=planner, replanner=replanner, strategy=bench.DEFAULT_STRATEGY, recorder=recorder)
        expected = [
            ("finishTraining", ("1",)),
            ("addTraining", ("3113", "1")),
            ("addTraining", ("3112", "2")),
            ("useConsumable", ("5001", "3")),
        ]
        self.assertEqual(env.calls, expected)
        self.assertEqual(recorder.skip_reasons, {"unknown_role": 1})
        self.assertEqual(recorder.actions, {"addTraining": 2, "finishTraining": 1, "useConsumable": 1})
        self.assertEqual((recorder.accepted, recorder.refused, recorder.dropped, recorder.replans), (4, 0, 0, 1))
        self.assertEqual(recorder.consumables_used, 1)
        (rebuilt, items) = replanner.calls[0]
        self.assertEqual((rebuilt.character_id, rebuilt.stats["AttackImprovement"]), (1, 9))
        self.assertEqual(items, {5001: 2})
        self.assertEqual(len(planner.calls), 1)
        kinds = [entry["decision"] for entry in recorder.trace]
        self.assertEqual(kinds, ["finish", "start", "start", "consume", "skip"])

    def test_run_tick_drops_advisory_when_finish_not_ok(self):
        for status in ("refused", "raised", "ambiguous"):
            with self.subTest(status=status):
                env = FakeEnvironment(outcomes={"finishTraining": status})
                planner = scripted(
                    bench.FinishTraining(1, "ended"),
                    bench.StartTraining(1, 3111, "advisory"),
                    bench.StartTraining(2, 3112, "start"),
                )
                replanner = replanner_returning(bench.StartTraining(1, 3113, "replanned"))
                recorder = bench.Recorder()
                bench.run_tick(env, planner=planner, replanner=replanner, strategy=None, recorder=recorder)
                self.assertEqual(env.calls, [("finishTraining", ("1",)), ("addTraining", ("3112", "2"))])
                self.assertEqual(replanner.calls, [])
                self.assertEqual(recorder.dropped, 1)
                self.assertEqual(recorder.skip_reasons, {})
                self.assertEqual(recorder.outcomes[status], 1)
        recorder = bench.Recorder()
        env = FakeEnvironment(outcomes={"finishTraining": "refused", "addTraining": "refused"})
        bench.run_tick(env, planner=scripted(bench.StartTraining(2, 3112, "start")), replanner=None,
                       strategy=None, recorder=recorder)
        self.assertEqual((recorder.accepted, recorder.refused), (0, 1))

    def test_replan_stock_excludes_finished_advisory_consumables(self):
        env = FakeEnvironment(inventory={"Common ATK Consumable": 3})
        planner = scripted(
            bench.FinishTraining(1, "ended"),
            bench.UseConsumable(1, 5001, "advisory"),
            bench.UseConsumable(2, 5001, "use"),
            bench.FinishTraining(3, "ended"),
            bench.UseConsumable(3, 5001, "advisory"),
        )
        replanner = replanner_returning(bench.UseConsumable(1, 5001, "replanned"), bench.Skip(3, "complete", "done"))
        recorder = bench.Recorder()
        bench.run_tick(env, planner=planner, replanner=replanner, strategy=None, recorder=recorder)
        self.assertEqual([items for _, items in replanner.calls], [{5001: 2}, {5001: 1}])
        self.assertEqual(recorder.skip_reasons, {"complete": 1})
        self.assertEqual(
            env.calls,
            [
                ("finishTraining", ("1",)),
                ("useConsumable", ("5001", "1")),
                ("useConsumable", ("5001", "2")),
                ("finishTraining", ("3",)),
            ],
        )

    def test_observation_failure_skips_tick(self):
        env = FakeEnvironment(fail_observe=True)
        planner = scripted(bench.StartTraining(2, 3112, "start"))
        recorder = bench.Recorder()
        bench.run_tick(env, planner=planner, replanner=None, strategy=None, recorder=recorder)
        self.assertEqual(planner.calls, [])
        self.assertEqual(env.calls, [])
        self.assertEqual(recorder.observe_failures, 1)
        self.assertEqual(recorder.ticks, 1)


def wrap(planner, transform):
    """A planner whose Skips go through ``transform`` (a Skip -> Decision function)."""

    def wrapped(ctx, strategy):
        return tuple(transform(d) if isinstance(d, bench.Skip) else d for d in planner(ctx, strategy))

    return wrapped


def run_one(data: dict, **kwargs):
    suite = bench.run_suite([bench.validate_scenario(data)], **kwargs)
    (result,) = suite.scenarios
    return suite, result


def baseline_for(suite, **scores) -> dict:
    return {
        "composite": suite.composite,
        "scenarios": {
            result.name: {"score": scores.get(result.name, result.score), "trace_digest": result.trace_digest}
            for result in suite.scenarios
        },
    }


class EpisodeOracleTests(unittest.TestCase):
    def test_tiny_episode_metrics_exact(self):
        episode = bench.run_episode(bench.validate_scenario(mini()))
        metrics = bench.compute_metrics(episode)
        self.assertEqual(metrics.actions, {"addTraining": 24, "finishTraining": 23, "useConsumable": 0})
        self.assertEqual((metrics.accepted_actions, metrics.refused_actions), (47, 0))
        self.assertEqual(metrics.idle_crew_hours, 0.0)
        self.assertEqual(metrics.eligible_crew_hours, 24.0)
        self.assertEqual(metrics.utilisation, 1.0)
        gained = sum(sum(entry["gain"].values()) for entry in episode.log if entry["accepted"])
        self.assertGreater(gained, 23)
        self.assertEqual(metrics.tp_gained, gained)
        self.assertEqual(metrics.progress, gained / 200)
        self.assertEqual(metrics.tp_per_crew_day, float(gained))
        self.assertEqual(metrics.focus, 1.0)
        self.assertEqual(metrics.skip_reasons, {})
        self.assertEqual((metrics.eligible_crew, metrics.headroom), (1, 200))
        self.assertEqual(metrics.cap_overshoot_tp, 0)
        self.assertEqual(metrics.zero_gain_sessions, 0)
        score = round(100 * (0.65 * gained / 200 + 0.25 + 0.10), 6)
        self.assertEqual(bench.score_scenario(metrics), score)
        self.assertEqual(episode.final[101], gained)
        self.assertEqual(len(episode.recorder.trace), 47)

    def test_idle_hours_count_uncollected_and_move_time(self):
        # A weapons crew at 71% in an Academy reaches 72% after one session and then waits ("move to gym").
        crew = [{"id": 201, "name": "Delish", "design": "elite", "room": 7002, "stats": {"Weapon": 71}}]
        data = mini(rooms=[{"id": 7002, "design": "Academy"}], research=ALL_RESEARCH, crew=crew)
        metrics = bench.compute_metrics(bench.run_episode(bench.validate_scenario(data)))
        self.assertGreaterEqual(metrics.skip_moves, 1)
        self.assertEqual(metrics.skip_reasons["not_in_training_room"], metrics.skip_moves)
        self.assertGreater(metrics.idle_crew_hours, 0.0)
        self.assertLess(metrics.utilisation, 1.0)
        complete = mini(crew=[captain(stats={"Attack": 180})])
        done = bench.compute_metrics(bench.run_episode(bench.validate_scenario(complete)))
        self.assertEqual((done.eligible_crew_hours, done.idle_crew_hours, done.utilisation), (0.0, 0.0, 1.0))

    def test_injected_cap_violation_fails_gate(self):
        crew = [{"id": 301, "name": "Huntress", "design": "unique", "room": 7001, "stats": {"Attack": 70}}, captain()]
        data = mini(crew=crew)
        clean, clean_result = run_one(data)
        self.assertEqual(clean_result.metrics.cap_violations, 0)
        self.assertEqual(clean_result.metrics.skip_reasons["tp_cap_reached"], 24)
        forced = wrap(bench.plan_training, lambda d: bench.StartTraining(d.character_id, 3111, "forced")
                      if d.reason_code == "tp_cap_reached" else d)
        suite, result = run_one(data, planner=forced)
        self.assertGreaterEqual(result.metrics.cap_violations, 1)
        self.assertGreater(result.metrics.cap_overshoot_tp, 0)
        self.assertEqual(result.score, 0.0)
        self.assertEqual(result.metrics.violations[0]["kind"], "cap")
        self.assertEqual(result.metrics.violations[0]["character_id"], 301)
        verdict = bench.gate(suite, baseline_for(clean), deterministic=True)
        self.assertFalse(verdict.passed)
        self.assertIn("cap_violation:mini", verdict.reasons)
        self.assertTrue(bench.gate(clean, baseline_for(clean), deterministic=True).passed)

    def test_injected_consumable_violation_detected(self):
        crew = [captain(), {"id": 401, "name": "Visiting Trader", "design": "common", "room": 7001}]
        data = mini(crew=crew, inventory={"Common WPN Consumable": 5})
        clean, _ = run_one(data)
        use = wrap(bench.plan_training, lambda d: bench.UseConsumable(d.character_id, 5002, "forced"))
        suite, result = run_one(data, planner=use)
        self.assertGreaterEqual(result.metrics.consumable_violations, 1)
        self.assertEqual(result.metrics.cap_violations, 0)
        self.assertIn("has no role", result.metrics.violations[0]["detail"])
        self.assertEqual(result.score, 0.0)
        verdict = bench.gate(suite, baseline_for(clean), deterministic=True)
        self.assertIn("consumable_violation:mini", verdict.reasons)
        late = wrap(bench.plan_training, lambda d: d)
        self.assertEqual(run_one(data, planner=late)[1].metrics.consumable_violations, 0)

    def test_consumable_oracle_rules(self):
        recorder = bench.Recorder()
        recorder.begin_tick(T0)
        _, _, ctx = observe_context(mini(crew=FOUR_CREW, rooms=[{"id": 7001, "design": "GYM"}]))
        crew = {member.character_id: member for member in ctx.crew}
        ok = bench.CallOutcome("ok")
        recorder.executed(bench.UseConsumable(1, 5001, "fine"), crew[1], ok)
        self.assertEqual(recorder.consumable_violations, 0)
        recorder.executed(bench.UseConsumable(1, 5011, "hero too early"), crew[1], ok)
        recorder.executed(bench.UseConsumable(1, 4242, "not an item"), crew[1], ok)
        recorder.executed(bench.UseConsumable(4, 5001, "no role"), crew[4], ok)
        self.assertEqual(recorder.consumable_violations, 3)
        details = [entry["detail"] for entry in recorder.violations]
        self.assertIn("Hero rule does not allow 0% with fatigue 0", details[0])
        self.assertIn("item 4242 is not a catalog consumable", details[1])
        self.assertIn("'Visiting Trader' has no role", details[2])
        self.assertEqual(recorder.consumables_used, 4)

    def test_injected_research_gate_violation_detected(self):
        gates = dict(bench.DEFAULT_STRATEGY.research_gates)
        gates[("primary", 1)] = ()
        loose = bench.replace(bench.DEFAULT_STRATEGY, research_gates=gates)
        data = mini(research=[])
        _, clean = run_one(data)
        self.assertEqual(clean.metrics.research_gate_violations, 0)
        self.assertEqual(clean.metrics.skip_reasons, {"research_missing": 24})
        suite, result = run_one(data, strategy=loose)
        self.assertGreaterEqual(result.metrics.research_gate_violations, 1)
        self.assertEqual(result.metrics.violations[0]["kind"], "research-gate")
        self.assertEqual(result.score, 0.0)
        verdict = bench.gate(suite, baseline_for(suite), deterministic=True)
        self.assertIn("research_gate_violation:mini", verdict.reasons)

    def test_fault_counters(self):
        cases = {
            "reject": ({"kind": "reject", "method": "addTraining", "call": 1}, "fault_rejected"),
            "contradicted": (
                {"kind": "contradicted-effect", "method": "addTraining", "call": 1, "training_design": "Kickbox"},
                "contradicted",
            ),
            "raise": ({"kind": "raise", "method": "addTraining", "call": 2}, "raised"),
            "ambiguous": ({"kind": "ambiguous-response", "method": "finishTraining", "call": 1}, "ambiguous"),
            "snapshot": ({"kind": "snapshot-failure", "method": "snapshot", "call": 3}, "observe_failures"),
        }
        counters = ("fault_rejected", "contradicted", "raised", "ambiguous", "observe_failures")
        for name, (fault, counter) in cases.items():
            with self.subTest(fault=name):
                _, result = run_one(mini(faults=[fault]))
                metrics = result.metrics
                self.assertEqual(getattr(metrics, counter), 1)
                for other in counters:
                    if other != counter:
                        self.assertEqual(getattr(metrics, other), 0, other)
                self.assertEqual(metrics.refused_actions, 0)
                self.assertEqual(metrics.reliability, 1.0)
        _, rejected = run_one(mini(faults=[cases["reject"][0]]))
        # Tick 0's start is rejected and retried at tick 1, so one session (and one finish) is lost.
        self.assertEqual(rejected.metrics.actions, {"addTraining": 24, "finishTraining": 22, "useConsumable": 0})
        self.assertEqual(rejected.metrics.accepted_actions, 45)  # 46 calls, the rejected one is not accepted
        self.assertEqual(rejected.metrics.refusal_codes, {})

    def test_determinism_and_nondeterminism_detection(self):
        scenarios = [bench.validate_scenario(mini())]
        deterministic, suite = bench.check_determinism(scenarios)
        self.assertTrue(deterministic)
        self.assertEqual(len(suite.scenarios), 1)
        state = {"calls": 0}

        def drifting(ctx, strategy):
            state["calls"] += 1
            plan = bench.plan_training(ctx, strategy)
            if state["calls"] > 24:
                return tuple(bench.Skip(d.character_id, "complete", "drift") for d in plan
                             if not isinstance(d, bench.FinishTraining))
            return plan

        deterministic, suite = bench.check_determinism(scenarios, planner=drifting)
        self.assertFalse(deterministic)
        verdict = bench.gate(suite, baseline_for(suite), deterministic=False)
        self.assertEqual(verdict.reasons, ("nondeterministic",))
        self.assertFalse(verdict.passed)

    def test_seed_override_changes_trace(self):
        scenario = [bench.validate_scenario(mini())]
        first = bench.run_suite(scenario).scenarios[0]
        again = bench.run_suite(scenario, seed=7).scenarios[0]
        other = bench.run_suite(scenario, seed=8).scenarios[0]
        self.assertEqual(first.trace_digest, again.trace_digest)
        self.assertNotEqual(first.trace_digest, other.trace_digest)
        self.assertRegex(first.trace_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual((first.seed, other.seed), (7, 8))

    def test_planner_crash_is_a_scenario_error(self):
        def broken(ctx, strategy):
            raise RuntimeError("planner exploded")

        suite, result = run_one(mini(), planner=broken)
        self.assertEqual(result.error, "RuntimeError: planner exploded")
        self.assertIsNone(result.metrics)
        self.assertEqual((result.score, suite.composite), (0.0, 0.0))
        self.assertIn("scenario_error:mini", bench.gate(suite, baseline_for(suite), deterministic=True).reasons)


def write_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


class SuiteFilesTests(unittest.TestCase):
    def test_load_suite_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            write_json(os.path.join(directory, "mini.json"), mini())
            write_json(os.path.join(directory, "other.json"), mini(name="other", seed=8))
            loaded = bench.load_suite(directory)
            self.assertEqual([scenario.name for scenario in loaded], ["mini", "other"])
            self.assertEqual([s.name for s in bench.load_suite(directory, names=["other"])], ["other"])
            with self.assertRaisesRegex(bench.ScenarioError, "unknown scenario.*nope"):
                bench.load_suite(directory, names=["nope"])
            with self.assertRaises(bench.ScenarioError) as raised:
                bench.load_suite(directory, strict=True)
            message = str(raised.exception)
            self.assertIn(f"days < {bench.MIN_SUITE_DAYS}: mini, other", message)
            self.assertIn("missing required tags: consumables", message)
            write_json(os.path.join(directory, "renamed.json"), mini(name="mini", seed=9))
            with self.assertRaisesRegex(bench.ScenarioError, "file stem 'renamed'"):
                bench.load_suite(directory)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(bench.ScenarioError, "no scenario files"):
                bench.load_suite(directory)
            path = os.path.join(directory, "broken.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            with self.assertRaisesRegex(bench.ScenarioError, "broken.json: cannot read"):
                bench.load_scenario(path)

    def test_checked_in_suite_rules(self):
        suite = bench.load_suite()
        self.assertGreaterEqual(len(suite), 10)
        names = [scenario.name for scenario in suite]
        self.assertEqual(names, sorted(names))
        stems = sorted(name[:-5] for name in os.listdir(bench.SCENARIO_DIR) if name.endswith(".json"))
        self.assertEqual(names, stems)
        self.assertTrue(all(scenario.days >= bench.MIN_SUITE_DAYS for scenario in suite))
        self.assertLessEqual(bench.REQUIRED_TAGS, {tag for scenario in suite for tag in scenario.tags})
        kinds = {fault["kind"] for scenario in suite for fault in scenario.faults}
        self.assertEqual(kinds, set(bench.ALLOWED_FAULTS))
        roles = {name for role in DEFAULT_ROLES for name in role.characters}
        unknown = {crew.name for scenario in suite for crew in scenario.crew} - roles
        self.assertEqual(unknown, {"Visiting Trader"})  # the one intentionally role-less crew
        for scenario in suite:
            with self.subTest(scenario=scenario.name):
                world = bench.build_world(scenario)
                self.assertGreater(sum(meta.headroom for meta in world.crew.values()), 0)

    def test_weakened_planner_and_strategy_score_lower(self):
        suite = {scenario.name: scenario for scenario in bench.load_suite()}

        def score(name, **kwargs):
            return bench.run_suite([suite[name]], days=2, **kwargs).composite

        def lazy(decision, ctx_now):
            if isinstance(decision, bench.StartTraining) and ctx_now.hour % 2:
                return bench.Skip(decision.character_id, "training_in_progress", "lazy: odd hour")
            return decision

        def lazy_planner(ctx, strategy):
            return tuple(lazy(d, ctx.now) for d in bench.plan_training(ctx, strategy))

        def lazy_replanner(ctx, crew, strategy, items):
            return tuple(lazy(d, ctx.now) for d in bench.replan_after_finish(ctx, crew, strategy, items))

        fresh = score("fresh-crew")
        self.assertGreater(fresh, 0.0)
        self.assertLess(score("fresh-crew", planner=lazy_planner, replanner=lazy_replanner), fresh)
        narrow = bench.replace(bench.default_strategy(), room_types={"GYM": "gym", "Academy": "academy"})
        self.assertLess(score("mixed-rooms", strategy=narrow), score("mixed-rooms"))
        self.assertLess(score("missing-research", strategy=bench.DEFAULT_STRATEGY), score("missing-research"))

        def noop(ctx, strategy):
            return tuple(bench.Skip(crew.character_id, "complete", "no-op") for crew in ctx.crew)

        noop_suite = bench.run_suite(list(suite.values()), days=2, planner=noop)
        self.assertEqual({result.name: result.score for result in noop_suite.scenarios}, dict.fromkeys(suite, 0.0))

    def test_every_checked_in_fault_fires(self):
        # A fault scheduled after the crew stop calling never fires and silently drops its coverage.
        for scenario in bench.load_suite():
            if not scenario.faults:
                continue
            with self.subTest(scenario=scenario.name):
                episode = bench.run_episode(scenario)
                self.assertEqual(len(episode.fired), len(scenario.faults), episode.fired)
                fired_kinds = sorted(entry.rsplit(": ", 1)[1] for entry in episode.fired)
                self.assertEqual(fired_kinds, sorted(fault["kind"] for fault in scenario.faults))


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def synthetic_suite(scores: dict, digests: dict | None = None, **metric_changes):
    results = tuple(
        bench.ScenarioResult(name, 1, bench.Metrics(**metric_changes), score, (digests or {}).get(name, DIGEST_A))
        for name, score in scores.items()
    )
    return bench.SuiteResult(results, bench.composite(list(scores.values())))


def synthetic_baseline(scores: dict, **changes) -> dict:
    data = {
        "schema_version": 1,
        "benchmark": "crew-training",
        "composite": bench.composite(list(scores.values())),
        "scenarios": {name: {"score": score, "trace_digest": DIGEST_A} for name, score in scores.items()},
        "lowered": [],
        "qa_eval": {"min_detection_rate": 0.9, "equivalent": []},
    }
    data.update(changes)
    return data


class GateBaselineTests(unittest.TestCase):
    def test_gate_rules(self):
        baseline = synthetic_baseline({"a": 50.0, "b": 70.0})
        self.assertEqual(bench.gate(synthetic_suite({"a": 50.0, "b": 70.0}), baseline, deterministic=True),
                         bench.GateResult(True, (), ()))
        lower = synthetic_suite({"a": 50.0, "b": 70.0})
        lower = bench.SuiteResult(lower.scenarios, baseline["composite"] - 0.000001)
        self.assertEqual(bench.gate(lower, baseline, deterministic=True).reasons, ("composite_below_baseline",))
        shifted = bench.gate(synthetic_suite({"a": 49.0, "b": 71.0}), baseline, deterministic=True)
        self.assertEqual(shifted.reasons, ("scenario_below_baseline:a",))
        self.assertFalse(shifted.passed)
        mismatch = bench.gate(synthetic_suite({"a": 50.0, "b": 70.0, "c": 99.0}), baseline, deterministic=True)
        self.assertEqual(mismatch.reasons, ("baseline_mismatch",))
        subset = bench.gate(synthetic_suite({"b": 70.0}), baseline, deterministic=True, subset=True)
        self.assertEqual(subset, bench.GateResult(True, (), ()))
        subset_lower = bench.gate(synthetic_suite({"b": 69.0}), baseline, deterministic=True, subset=True)
        self.assertEqual(subset_lower.reasons, ("scenario_below_baseline:b",))
        drifted = bench.gate(synthetic_suite({"a": 50.0, "b": 70.0}, {"b": DIGEST_B}), baseline, deterministic=True)
        self.assertEqual(drifted, bench.GateResult(True, (), ("b",)))
        missing = bench.gate(synthetic_suite({"a": 50.0}), None, deterministic=False)
        self.assertEqual(missing, bench.GateResult(False, ("nondeterministic", "baseline_missing"), ()))
        higher = bench.gate(synthetic_suite({"a": 60.0, "b": 70.0}), baseline, deterministic=True)
        self.assertTrue(higher.passed)

    def test_update_baseline_only_goes_up(self):
        scores = {"a": 50.0, "b": 70.0}
        new = bench.update_baseline(synthetic_suite(scores), None, deterministic=True, allow_lower=None)
        self.assertEqual(new, synthetic_baseline(scores))
        bench.validate_baseline(new)
        old = synthetic_baseline(scores, qa_eval={"min_detection_rate": 0.95, "equivalent": [
            {"id": "sdk/training/planner.py:1:2:constant:0", "original": "1", "replacement": "2", "reason": "dead"}]})
        higher = bench.update_baseline(synthetic_suite({"a": 55.0, "b": 70.0}), old, deterministic=True,
                                       allow_lower=None)
        self.assertEqual((higher["composite"], higher["scenarios"]["a"]["score"]), (62.5, 55.0))
        self.assertEqual(higher["qa_eval"], old["qa_eval"])
        refreshed = bench.update_baseline(synthetic_suite(scores, {"a": DIGEST_B}), old, deterministic=True,
                                          allow_lower=None)
        self.assertEqual(refreshed["scenarios"]["a"]["trace_digest"], DIGEST_B)
        added = bench.update_baseline(synthetic_suite({"a": 50.0, "b": 70.0, "c": 80.0}), old, deterministic=True,
                                      allow_lower=None)
        self.assertEqual(sorted(added["scenarios"]), ["a", "b", "c"])
        for name, suite in {
            "lower": synthetic_suite({"a": 49.0, "b": 70.0}),
            "one lower, composite up": synthetic_suite({"a": 49.0, "b": 90.0}),
            "removed": synthetic_suite({"a": 60.0}),
        }.items():
            with self.subTest(case=name):
                with self.assertRaisesRegex(bench.BaselineRefused, "--allow-lower"):
                    bench.update_baseline(suite, old, deterministic=True, allow_lower=None)
                with self.assertRaisesRegex(bench.BaselineRefused, "reason"):
                    bench.update_baseline(suite, old, deterministic=True, allow_lower="  ")
        lowered = bench.update_baseline(synthetic_suite({"a": 49.0, "b": 70.0}), old, deterministic=True,
                                        allow_lower=" new scenario weights ")
        self.assertEqual(lowered["lowered"], [{"from": 60.0, "to": 59.5, "reason": "new scenario weights"}])
        self.assertEqual(lowered["composite"], 59.5)
        refusals = {
            "nondeterministic": (synthetic_suite(scores), {"deterministic": False}),
            "violation": (synthetic_suite(scores, cap_violations=1), {}),
            "error": (bench.SuiteResult((bench.ScenarioResult("a", 1, None, 0.0, "", "Boom"),), 0.0), {}),
        }
        for name, (suite, options) in refusals.items():
            with self.subTest(refusal=name):
                with self.assertRaisesRegex(bench.BaselineRefused, name):
                    bench.update_baseline(suite, old, deterministic=options.get("deterministic", True),
                                          allow_lower="anyway")

    def test_validate_baseline_schema(self):
        good = synthetic_baseline({"a": 50.0})
        self.assertEqual(bench.validate_baseline(good), good)
        bad_cases = {
            "unknown key": dict(good, extra=1),
            "score 101": dict(good, scenarios={"a": {"score": 101, "trace_digest": DIGEST_A}}),
            "bool score": dict(good, scenarios={"a": {"score": True, "trace_digest": DIGEST_A}}),
            "bad digest": dict(good, scenarios={"a": {"score": 5, "trace_digest": "md5:abc"}}),
            "scenario key": dict(good, scenarios={"a": {"score": 5, "trace_digest": DIGEST_A, "seed": 1}}),
            "missing composite": {key: value for key, value in good.items() if key != "composite"},
            "composite -1": dict(good, composite=-1),
            "schema 2": dict(good, schema_version=2),
            "benchmark name": dict(good, benchmark="programbench"),
            "blank lowered reason": dict(good, lowered=[{"from": 2.0, "to": 1.0, "reason": " "}]),
            "rate 0": dict(good, qa_eval={"min_detection_rate": 0, "equivalent": []}),
            "rate 1.5": dict(good, qa_eval={"min_detection_rate": 1.5, "equivalent": []}),
            "blank equivalent reason": dict(good, qa_eval={"min_detection_rate": 0.9, "equivalent": [
                {"id": "sdk/training/planner.py:1:2:constant:0", "original": "1", "replacement": "2", "reason": ""}]}),
            "bad equivalent id": dict(good, qa_eval={"min_detection_rate": 0.9, "equivalent": [
                {"id": "planner", "original": "1", "replacement": "2", "reason": "x"}]}),
            "not an object": [],
        }
        for name, data in bad_cases.items():
            with self.subTest(case=name):
                with self.assertRaises(bench.BaselineError):
                    bench.validate_baseline(data)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "baseline.json")
            self.assertIsNone(bench.load_baseline(path))
            write_json(path, good)
            self.assertEqual(bench.load_baseline(path), good)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{")
            with self.assertRaisesRegex(bench.BaselineError, "baseline.json"):
                bench.load_baseline(path)

    def test_report_json_and_markdown(self):
        suite = synthetic_suite({"zeta": 40.0, "alpha": 60.125}, skip_reasons={"tp_cap_reached": 3})
        report = bench.report_dict(suite)
        text = bench.to_json(report)
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(json.loads(text), report)
        self.assertLess(text.index('"benchmark"'), text.index('"composite"'))
        self.assertLess(text.index('"alpha"'), text.index('"zeta"'))
        self.assertEqual(report["scenarios"]["alpha"]["metrics"]["skip_reasons"], {"tp_cap_reached": 3})
        verdict = bench.GateResult(False, ("scenario_below_baseline:zeta",), ("alpha",))
        markdown = bench.render_markdown(report, verdict)
        lines = markdown.splitlines()
        self.assertTrue(any(line.startswith("| Scenario | Score |") for line in lines))
        self.assertIn("Composite: **50.06**", markdown)
        self.assertIn("Gate: **FAIL** (scenario_below_baseline:zeta)", markdown)
        self.assertTrue(any(line.startswith("| alpha | 60.12") for line in lines))
        self.assertTrue(any(line.startswith("| zeta | tp_cap_reached 3") for line in lines))
        self.assertIn("Trace drift (behaviour changed, scores held): alpha", markdown)
        passing = bench.render_markdown(report, bench.GateResult(True, (), ()))
        self.assertIn("Gate: **PASS**", passing)
        self.assertNotIn("Trace drift", passing)
        self.assertIn("Gate: not run", bench.render_markdown(report, None))
        errored = bench.report_dict(bench.SuiteResult((bench.ScenarioResult("a", 1, None, 0.0, "", "Boom"),), 0.0))
        self.assertIn("| a | 0.00 | error: Boom |", bench.render_markdown(errored, None))


class MainTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.scenarios = os.path.join(self.directory.name, "scenarios")
        os.mkdir(self.scenarios)
        write_json(os.path.join(self.scenarios, "mini.json"), mini())
        self.baseline = os.path.join(self.directory.name, "baseline.json")

    def main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = bench.main(["--scenarios-dir", self.scenarios, "--baseline", self.baseline, *argv])
        return code, out.getvalue()

    def test_main_exit_codes(self):
        self.assertEqual(self.main()[0], bench.EXIT_ERROR)  # gate mode, no baseline
        code, out = self.main("--update-baseline", "--quiet")
        self.assertEqual((code, out), (bench.EXIT_OK, ""))
        with open(self.baseline, encoding="utf-8") as handle:
            written = json.load(handle)
        self.assertEqual(sorted(written["scenarios"]), ["mini"])
        code, out = self.main()
        self.assertEqual(code, bench.EXIT_OK)
        self.assertIn("Gate: **PASS**", out)
        report_path = os.path.join(self.directory.name, "report.json")
        summary = os.path.join(self.directory.name, "summary.md")
        with open(summary, "w", encoding="utf-8") as handle:
            handle.write("before\n")
        self.assertEqual(self.main("--json", report_path, "--summary", summary, "--quiet")[0], bench.EXIT_OK)
        with open(report_path, encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual(report["composite"], written["composite"])
        self.assertEqual(report["gate"], {"passed": True, "reasons": [], "drift": []})
        with open(summary, encoding="utf-8") as handle:
            summary_text = handle.read()
        self.assertTrue(summary_text.startswith("before\n# Crew-training benchmark"))
        code, out = self.main("--json", "-")
        self.assertEqual(json.loads(out)["gate"]["passed"], True)
        higher = dict(written, composite=written["composite"] + 1)
        write_json(self.baseline, higher)
        code, out = self.main("--json", "-")
        self.assertEqual(code, bench.EXIT_GATE)
        self.assertEqual(json.loads(out)["gate"]["reasons"], ["composite_below_baseline"])
        self.assertEqual(self.main("--quick", "--quiet")[0], bench.EXIT_OK)  # report only, never gated
        self.assertEqual(self.main("--seed", "3", "--quiet")[0], bench.EXIT_OK)
        self.assertEqual(self.main("--update-baseline", "--quiet")[0], bench.EXIT_GATE)  # would lower it
        self.assertEqual(self.main("--update-baseline", "--allow-lower", "reset", "--quiet")[0], bench.EXIT_OK)
        with open(self.baseline, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["lowered"][0]["reason"], "reset")
        self.assertEqual(self.main("--allow-lower", "x")[0], bench.EXIT_ERROR)
        self.assertEqual(self.main("--update-baseline", "--quick")[0], bench.EXIT_ERROR)
        self.assertEqual(self.main("--scenario", "nope")[0], bench.EXIT_ERROR)
        self.assertEqual(self.main("--scenario", "mini", "--quiet")[0], bench.EXIT_OK)
        write_json(os.path.join(self.scenarios, "broken.json"), mini(name="broken", days=0))
        self.assertEqual(self.main()[0], bench.EXIT_ERROR)

    def test_markdown_file_output(self):
        self.main("--update-baseline", "--quiet")
        path = os.path.join(self.directory.name, "report.md")
        self.assertEqual(self.main("--markdown", path, "--quiet")[0], bench.EXIT_OK)
        with open(path, encoding="utf-8") as handle:
            self.assertIn("| mini |", handle.read())


class CheckedInBaselineTests(unittest.TestCase):
    def test_checked_in_baseline_is_valid(self):
        baseline = bench.load_baseline(bench.BASELINE_PATH)
        self.assertIsNotNone(baseline)
        self.assertEqual(bench.validate_baseline(baseline), baseline)
        self.assertEqual(sorted(baseline["scenarios"]), [scenario.name for scenario in bench.load_suite()])
        self.assertEqual(baseline["qa_eval"]["min_detection_rate"], 0.9)
        scores = [entry["score"] for entry in baseline["scenarios"].values()]
        self.assertEqual(baseline["composite"], bench.composite(scores))


TOY_SOURCE = "LIMIT = 3\n\n\ndef over(value):\n    return value > LIMIT\n"


class FakeProcess:
    pid = 424242

    def __init__(self, returncode=0, stdout="", hang=False):
        self.returncode = returncode
        self.stdout_text = stdout
        self.hang = hang
        self.timeouts = []

    def communicate(self, timeout=None):
        self.timeouts.append(timeout)
        if self.hang and len(self.timeouts) == 1:
            raise subprocess.TimeoutExpired("bench", timeout)
        return self.stdout_text, "stderr tail"


class QaEvalTests(unittest.TestCase):
    def test_qa_classification_and_rates(self):
        detected = bench.BenchOutcome("detected", ("scenario_below_baseline:fresh-crew",), ())
        passing = bench.BenchOutcome("pass")
        drifting = bench.BenchOutcome("pass", (), ("long-run",))
        cases = [
            ("killed", "failed", detected, "both"),
            ("killed", "timeout", passing, "tests-only"),
            ("killed", "passed", detected, "benchmark-only"),
            ("killed", "passed", bench.BenchOutcome("crash"), "benchmark-only"),
            ("timeout", "passed", bench.BenchOutcome("timeout"), "benchmark-only"),
            ("survived", "passed", drifting, "drift-only"),
            ("survived", "passed", passing, "neither"),
            ("survived", None, None, "neither"),
            ("equivalent", None, None, "equivalent"),
            ("incompetent", None, None, "incompetent"),
        ]
        for status, tests_status, outcome, expected in cases:
            with self.subTest(expected=expected, status=status):
                self.assertEqual(bench.classify_mutant(status, tests_status, outcome), expected)
        rows = [
            {"id": f"sdk/x.py:{index}:0:constant:0", "original": "1", "replacement": "2", "class": klass,
             "reasons": ["r"] if klass == "benchmark-only" else [], "drift": ["d"] if klass == "drift-only" else []}
            for index, klass in enumerate(
                ["both", "tests-only", "benchmark-only", "drift-only", "neither", "neither", "equivalent",
                 "incompetent", "both", "tests-only"], start=1)
        ]
        summary = bench.summarize(rows, {"sdk/x.py:6:0:constant:0": "dead store"})
        self.assertEqual(summary["counts"], {"both": 2, "tests-only": 2, "benchmark-only": 1, "drift-only": 1,
                                             "neither": 2, "equivalent": 1, "incompetent": 1})
        self.assertEqual((summary["total"], summary["eligible"]), (10, 8))
        self.assertEqual(summary["detection_rate"], 0.625)
        self.assertEqual(summary["observable_rate"], 0.75)
        self.assertEqual((summary["tests_rate"], summary["benchmark_rate"]), (0.5, 0.375))
        self.assertEqual(summary["benchmark_only"], [{"id": "sdk/x.py:3:0:constant:0", "change": "1 -> 2",
                                                      "reasons": ["r"]}])
        self.assertEqual([entry["note"] for entry in summary["neither"]], ["unexplained - investigate", "dead store"])
        self.assertEqual(summary["drift_only"][0]["drift"], ["d"])
        empty = bench.summarize([], {})
        self.assertEqual((empty["detection_rate"], empty["eligible"]), (1.0, 0))

    def test_qa_runner_records_channels_by_source_digest(self):
        with tempfile.TemporaryDirectory() as workspace:
            os.makedirs(os.path.join(workspace, "sdk"))
            scenario_dir = os.path.join(workspace, "automation", "benchmark", "training", "scenarios")
            os.makedirs(scenario_dir)
            for name in ("a.json", "b.json", "notes.txt"):
                write_json(os.path.join(scenario_dir, name), {})
            path = os.path.join(workspace, "sdk", "x.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(TOY_SOURCE)
            digest = hashlib.sha256(TOY_SOURCE.encode("utf-8")).hexdigest()
            seen = []

            def tests_runner(status):
                def run(ws, tests, timeout):
                    seen.append(("tests", tests, timeout))
                    return mutation.RunResult(status, 0.5, "tests output", 7)
                return run

            def bench_runner(status):
                def run(ws, timeout):
                    seen.append(("bench", str(ws), timeout))
                    return bench.BenchOutcome(status, ("why",) if status != "pass" else (), ())
                return run

            matrix = [("passed", "pass", "passed"), ("failed", "pass", "failed"), ("passed", "detected", "failed"),
                      ("timeout", "pass", "failed"), ("passed", "crash", "failed")]
            for tests_status, bench_status, expected in matrix:
                with self.subTest(tests=tests_status, bench=bench_status):
                    seen.clear()
                    runner = bench.QaRunner("sdk/x.py", ("tests", "benchmark"), tests_runner(tests_status),
                                            bench_runner(bench_status))
                    result = runner(workspace, ("tests.a",), 60.0)
                    self.assertEqual((result.status, result.tests_run), (expected, 7))
                    self.assertEqual(seen, [("tests", ("tests.a",), 30.0), ("bench", workspace, 30.0)])
                    (key,) = runner.outcomes
                    self.assertEqual(key, digest)
                    self.assertEqual(runner.outcomes[key][0], tests_status)
                    self.assertEqual(runner.outcomes[key][1].status, bench_status)
            only_bench = bench.QaRunner("sdk/x.py", ("benchmark",), tests_runner("failed"), bench_runner("pass"))
            seen.clear()
            result = only_bench(workspace, ("tests.a",), 60.0)
            self.assertEqual((result.status, result.tests_run), ("passed", 2))
            self.assertEqual(seen, [("bench", workspace, 60.0)])
            self.assertIsNone(only_bench.outcomes[digest][0])

    def test_bench_subprocess_classifies(self):
        report = {"gate": {"passed": False, "reasons": ["cap_violation:near-cap-crew"], "drift": ["fresh-crew"]}}
        cases = [
            (FakeProcess(0, json.dumps({"gate": {"passed": True, "reasons": [], "drift": ["x"]}})),
             bench.BenchOutcome("pass", (), ("x",))),
            (FakeProcess(1, json.dumps(report)),
             bench.BenchOutcome("detected", ("cap_violation:near-cap-crew",), ("fresh-crew",))),
            (FakeProcess(1, "Traceback: not json"), bench.BenchOutcome("crash", ("exit 1: stderr tail",), ())),
            (FakeProcess(2, ""), bench.BenchOutcome("crash", ("exit 2: stderr tail",), ())),
            (FakeProcess(0, "garbage"), bench.BenchOutcome("pass", (), ())),
        ]
        for process, expected in cases:
            with self.subTest(expected=expected):
                commands, killed = [], []

                def popen(command, **options):
                    commands.append((command, options))
                    return process

                with tempfile.TemporaryDirectory() as workspace:
                    runner = bench.BenchSubprocessRunner(popen=popen, kill=killed.append)
                    self.assertEqual(runner(workspace, 12.5), expected)
                    (command, options), = commands
                    self.assertEqual(command[1:], [os.path.join(workspace, "scripts", "training_benchmark.py"),
                                                   "--gate", "--json", "-", "--quiet"])
                    self.assertEqual(options["cwd"], workspace)
                    self.assertEqual(options["env"]["PYTHONPATH"], workspace)
                    self.assertEqual(process.timeouts, [12.5])
                    self.assertEqual(killed, [])
        hung = FakeProcess(hang=True)
        killed = []
        with tempfile.TemporaryDirectory() as workspace:
            runner = bench.BenchSubprocessRunner(popen=lambda command, **options: hung, kill=killed.append)
            self.assertEqual(runner(workspace, 3.0), bench.BenchOutcome("timeout"))
        self.assertEqual(killed, [hung])
        self.assertEqual(hung.timeouts[0], 3.0)

    def test_qa_eval_wires_run_mutants(self):
        calls = []

        def fake_run_mutants(target, tests, **kwargs):
            calls.append((target, tests, kwargs))
            generated = kwargs["generated"]
            results = tuple(mutation.MutantResult(m, "survived", 0.0, "") for m in generated[:3])
            return mutation.Report(target, tuple(tests), results, 1.0, kwargs["timeout"])

        with tempfile.TemporaryDirectory() as directory:
            baseline_path = os.path.join(directory, "baseline.json")
            planner_source = mutation.read_source(Path(bench.ROOT) / "sdk" / "training" / "planner.py")
            first = mutation.generate_mutants("sdk/training/planner.py", planner_source)[0]
            equivalent = {"id": first.id, "original": first.original, "replacement": first.replacement,
                          "reason": "proved equivalent"}
            stale = {"id": "sdk/training/strategy.py:1:0:constant:0", "original": "x", "replacement": "y",
                     "reason": "gone"}
            write_json(baseline_path, synthetic_baseline({"a": 1.0}, qa_eval={"min_detection_rate": 0.9,
                                                                                "equivalent": [equivalent, stale]}))
            with mock.patch.object(mutation, "run_mutants", fake_run_mutants):
                result = bench.qa_eval(jobs=3, timeout=40.0, baseline_path=baseline_path)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = bench.main(["--qa-eval", "--baseline", baseline_path, "--jobs", "2", "--json", "-"])
        self.assertEqual([call[0] for call in calls[:2]], list(bench.QA_TARGETS))
        target, tests, kwargs = calls[0]
        self.assertEqual(tests, bench.QA_TEST_MODULES)
        self.assertEqual(kwargs["copy_paths"], bench.QA_COPY_PATHS)
        self.assertEqual(kwargs["equivalent"], [first.id])
        self.assertEqual(calls[1][2]["equivalent"], [])
        self.assertEqual((kwargs["jobs"], kwargs["timeout"]), (3, 40.0))
        self.assertIsInstance(kwargs["test_runner"], bench.QaRunner)
        self.assertEqual(result["overall"]["counts"]["neither"], 6)
        self.assertEqual(result["overall"]["detection_rate"], 0.0)
        self.assertEqual(result["stale_equivalents"], [stale["id"]])
        self.assertFalse(result["passed"])
        self.assertEqual(code, bench.EXIT_GATE)
        self.assertEqual(json.loads(out.getvalue())["overall"]["eligible"], 6)
        self.assertEqual(calls[2][2]["jobs"], 2)
        markdown = bench.render_qa_markdown(result)
        self.assertIn("| sdk/training/planner.py | 3 |", markdown)
        self.assertIn("unexplained - investigate", markdown)
        self.assertIn(stale["id"], markdown)

    def test_stale_equivalents(self):
        generated = mutation.generate_mutants("toy.py", TOY_SOURCE)
        first = generated[0]
        good = {"id": first.id, "original": first.original, "replacement": first.replacement, "reason": "r"}
        moved = dict(good, id=first.id.replace(":5:", ":9:", 1).replace(f":{first.line}:", ":99:", 1))
        changed = dict(good, replacement=first.replacement + " ")
        self.assertEqual(bench.stale_equivalents([good, moved, changed], generated), [moved["id"], changed["id"]])
        self.assertEqual(bench.stale_equivalents([], generated), [])

    def test_mutation_config_copy_covers_benchmark(self):
        path = os.path.join(bench.ROOT, "automation", "mutation", "config.json")
        if not os.path.exists(path):  # a mutant workspace copies only the config's copy paths, not the config
            self.skipTest("no automation/mutation/config.json in this tree (mutation workspace)")
        config = mutation.load_config(path)
        for path in ("scripts/mutation.py", "scripts/training_benchmark.py", "automation/benchmark/training"):
            self.assertIn(path, config.copy)
        self.assertLessEqual(set(config.copy), set(bench.QA_COPY_PATHS))
        mutation.validate_run("scripts/training_benchmark.py", ("tests.test_training_benchmark",),
                              Path(bench.ROOT), config.copy)


class ReviewRegressionTests(unittest.TestCase):
    """Regression coverage for defects found in the Unit 11 code review."""

    def test_read_fault_observation_does_not_move_the_clock(self):
        faults = [{"kind": "snapshot-failure", "method": "snapshot", "day": 0, "call": 2}]
        scenario = bench.validate_scenario(mini(faults=faults))
        world = bench.build_world(scenario)
        self.assertTrue(world.read_faults)
        env = bench.SimEnvironment(world, scenario, scenario.seed)
        before = env.now()
        env.observe()  # snapshot read 1: succeeds
        self.assertEqual(env.now(), before)
        with self.assertRaises(bench.ObservationError):
            env.observe()  # snapshot read 2: the injected failure
        self.assertEqual(env.now(), before)
        env.observe()
        self.assertEqual(env.now(), before)
        # Over a whole episode, the read-fault scenario keeps whole-hour windows like a fault-free one.
        metrics = bench.compute_metrics(bench.run_episode(scenario))
        self.assertEqual(metrics.eligible_crew_hours, 24.0)

    def test_ambiguous_consumable_reply_is_not_accepted(self):
        for effect in (False, True):
            with self.subTest(effect=effect):
                faults = [{"kind": "ambiguous-response", "method": "activateItem3", "call": 1, "effect": effect}]
                _, env, _ = observe_context(mini(faults=faults, inventory={"Common ATK Consumable": 2}))
                outcome = env.call("useConsumable", ("5001", "101"))
                self.assertEqual(outcome.status, "ambiguous")
                self.assertEqual(outcome.faults, ("day 0 activateItem3 #1: ambiguous-response",))
                recorder = bench.Recorder()
                recorder.now = T0
                crew = observe_context(mini())[2].crew[0]
                recorder.executed(bench.UseConsumable(101, 5001, "use"), crew, outcome)
                self.assertEqual((recorder.accepted, recorder.consumables_used), (0, 0))
                self.assertEqual(recorder.outcomes["ambiguous"], 1)

    def test_validate_scenario_reports_unhashable_values(self):
        cases = {
            "room design list": (mini(rooms=[{"id": 7001, "design": ["GYM"]}]), "rooms[0].design"),
            "crew room list": (mini(crew=[captain(room=[7001])]), "crew[0].room"),
            "crew design list": (mini(crew=[captain(design=["captain"])]), "crew[0].design"),
            "session design object": (
                mini(crew=[captain(training={"design": {}, "ends_after_minutes": 5})]), "crew[0].training"
            ),
            "fault design list": (
                mini(faults=[{"kind": "contradicted-effect", "method": "addTraining", "training_design": []}]),
                "training_design",
            ),
            "fault kind list": (mini(faults=[{"kind": ["reject"], "method": "addTraining"}]), "kind"),
            "research nested list": (mini(research=[["Fitness"]]), "research"),
        }
        for name, (data, needle) in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(bench.ScenarioError) as raised:
                    bench.validate_scenario(data)
                self.assertIn(needle, str(raised.exception))

    def test_fault_day_beyond_the_scenario_is_rejected(self):
        late = {"kind": "reject", "method": "addTraining", "day": 1, "call": 1}
        with self.assertRaises(bench.ScenarioError) as raised:
            bench.validate_scenario(mini(days=1, faults=[late]))
        self.assertIn("faults[0].day", str(raised.exception))
        self.assertEqual(bench.validate_scenario(mini(days=2, faults=[late])).faults, (late,))
        with self.assertRaises(bench.ScenarioError) as raised:
            bench.validate_scenario(mini(days=0, faults=[late]))
        self.assertEqual(len(raised.exception.problems), 1)  # only days: no fault-day noise from bad days

    def test_gate_subset_needs_baseline_entries_and_errors_are_not_drift(self):
        baseline = synthetic_baseline({"a": 50.0, "b": 70.0})
        unknown = bench.gate(synthetic_suite({"c": 99.0}), baseline, deterministic=True, subset=True)
        self.assertFalse(unknown.passed)
        self.assertEqual(unknown.reasons, ("baseline_mismatch",))
        errored = bench.SuiteResult((bench.ScenarioResult("a", 1, None, 0.0, "", "Boom"),
                                     bench.ScenarioResult("b", 1, bench.Metrics(), 70.0, DIGEST_A)), 35.0)
        verdict = bench.gate(errored, baseline, deterministic=True)
        self.assertIn("scenario_error:a", verdict.reasons)
        self.assertEqual(verdict.drift, ())

    def test_update_baseline_accepts_a_new_lower_scenario(self):
        old = synthetic_baseline({"a": 50.0, "b": 70.0})
        added = bench.update_baseline(synthetic_suite({"a": 50.0, "b": 70.0, "c": 20.0}), old, deterministic=True,
                                      allow_lower=None)
        self.assertEqual(added["composite"], 46.666667)
        self.assertEqual(added["lowered"], [])
        with self.assertRaisesRegex(bench.BaselineRefused, "b 70.0 -> 69.0"):
            bench.update_baseline(synthetic_suite({"a": 50.0, "b": 69.0, "c": 99.0}), old, deterministic=True,
                                  allow_lower=None)

    def test_cap_overshoot_ignores_crew_that_started_above_cap(self):
        huntress = {"id": 3, "name": "Huntress", "design": "unique", "room": 7001, "stats": {"Attack": 80}}
        scenario = bench.validate_scenario(mini(crew=[captain(), huntress]))
        world = bench.build_world(scenario)
        self.assertLess(world.crew[3].cap, 80)
        episode = bench.run_episode(scenario)
        self.assertEqual(episode.final[3], 80)
        self.assertEqual(bench.compute_metrics(episode).cap_overshoot_tp, 0)
        crew = dict(world.crew)
        crew[3] = bench.replace(crew[3], initial_total=60)  # had it started below cap, the 20 TP over cap counts
        self.assertEqual(bench.compute_metrics(bench.replace(episode, world=bench.replace(world, crew=crew)))
                         .cap_overshoot_tp, 10)

    def test_bench_subprocess_kills_child_on_interrupt_and_after_kill_all(self):
        class Interrupted(FakeProcess):
            def communicate(self, timeout=None):
                raise KeyboardInterrupt

        killed = []
        process = Interrupted()
        with tempfile.TemporaryDirectory() as workspace:
            runner = bench.BenchSubprocessRunner(popen=lambda command, **options: process, kill=killed.append)
            with self.assertRaises(KeyboardInterrupt):
                runner(workspace, 5.0)
            self.assertEqual(killed, [process])
            self.assertEqual(runner._live, set())
            killed.clear()
            late = FakeProcess(0, json.dumps({"gate": {"passed": True, "reasons": [], "drift": []}}))
            runner = bench.BenchSubprocessRunner(popen=lambda command, **options: late, kill=killed.append)
            runner.kill_all()
            runner(workspace, 5.0)
            self.assertEqual(killed, [late])

    def test_qa_eval_matches_outcomes_on_a_crlf_target(self):
        source = TOY_SOURCE.replace("\n", "\r\n")
        rel = "sdk/toy.py"

        def fake_run_mutants(target, tests, **kwargs):
            raw = mutation.read_source(Path(kwargs["repo_root"]) / target)
            results = []
            with tempfile.TemporaryDirectory() as workspace:
                os.makedirs(os.path.join(workspace, "sdk"))
                for mutant in kwargs["generated"]:
                    with open(os.path.join(workspace, target), "wb") as handle:
                        handle.write(mutation.apply_mutant(raw, mutant).encode("utf-8"))
                    run = kwargs["test_runner"](workspace, tuple(tests), 10.0)
                    results.append(mutation.MutantResult(mutant, "killed" if run.status == "failed" else "survived",
                                                         0.0, ""))
            return mutation.Report(target, tuple(tests), tuple(results), 1.0, 10.0)

        with tempfile.TemporaryDirectory() as repo:
            os.makedirs(os.path.join(repo, "sdk"))
            with open(os.path.join(repo, rel), "wb") as handle:
                handle.write(source.encode("utf-8"))
            with mock.patch.object(mutation, "run_mutants", fake_run_mutants):
                result = bench.qa_eval(
                    targets=(rel,), repo_root=repo, baseline_path=os.path.join(repo, "none.json"),
                    tests_runner=lambda ws, tests, timeout: mutation.RunResult("passed", 0.0, "", 1),
                    bench_runner=lambda ws, timeout: bench.BenchOutcome("detected", ("caught",)),
                )
        self.assertGreater(result["overall"]["eligible"], 0)
        self.assertEqual(result["overall"]["counts"]["benchmark-only"], result["overall"]["eligible"])
        self.assertEqual(result["overall"]["detection_rate"], 1.0)

    def test_usage_rejects_flags_that_would_be_ignored(self):
        parser = bench.build_parser()
        for argv in (
            ["--gate", "--quick"],
            ["--gate", "--seed", "5"],
            ["--gate", "--update-baseline"],
            ["--qa-eval", "--scenario", "mini"],
            ["--qa-eval", "--quick"],
            ["--qa-eval", "--seed", "1"],
            ["--qa-eval", "--update-baseline"],
            ["--qa-eval", "--gate"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(bench._usage_error(parser.parse_args(argv)))
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(bench.main(argv), bench.EXIT_ERROR)
        for argv in ([], ["--gate"], ["--quick"], ["--seed", "3"], ["--gate", "--scenario", "x"], ["--qa-eval"]):
            with self.subTest(ok=argv):
                self.assertIsNone(bench._usage_error(parser.parse_args(argv)))


if __name__ == "__main__":
    unittest.main()
