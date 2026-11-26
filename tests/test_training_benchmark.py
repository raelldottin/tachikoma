"""Tests for the crew-training simulator benchmark (scripts/training_benchmark.py).

Stdlib unittest, no network: every episode runs in the offline simulator (sdk.simulation.game). The oracles and the
metrics are pure functions, tested on hand-built journals and logs; the recording client and the contract executor
are tested against the simulator and a scripted fake client; whole episodes pin exact, seed-determined values. The
policy-literal pins live in tests/test_training_benchmark_policy.py, which is deliberately not a qa-eval channel.
The checked-in baseline gate is never run here: CI runs `make training-benchmark`.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scripts import mutation
from scripts import training_benchmark as bench
from sdk.simulation.game import SimulatedFaultError
from sdk.training import build_context
from sdk.training.model import (
    SKIP_FATIGUED,
    CrewState,
    FinishTraining,
    Skip,
    StartTraining,
    TrainingContext,
    TrainingDesign,
    UseConsumable,
)
from sdk.training.parse import parse_consumable_designs, parse_crew
from sdk.training.planner import (
    percent,
    pick_design,
    plan_training,
    remaining_stock,
    replan_after_finish,
    target_stat,
    training_state,
)
from sdk.training.strategy import (
    DEFAULT_ROLES,
    DEFAULT_STRATEGY,
    RARITY_STARS,
    ConsumableRule,
    TierBand,
    with_consumables,
)

T0 = datetime(2027, 7, 5, tzinfo=timezone.utc)
AT0 = "2027-07-05T00:00:00"
ALL_RESEARCH = ["Fitness", "Fitness Lv2", "Fitness Lv3", "Education", "Education Lv2", "Education Lv3"]


def hours(value: float) -> datetime:
    return T0 + timedelta(hours=value)


def stamp(value: float) -> str:
    return bench.format_time(hours(value))


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


def member(cid: int, name: str, design: str, room: int = 7001, **extra) -> dict:
    return {"id": cid, "name": name, "design": design, "room": room, **extra}


ROOMS = [{"id": 7001, "design": "GYM"}, {"id": 7002, "design": "Academy"}, {"id": 7005, "design": "Bridge"}]


def world_for(data: dict):
    scenario = bench.validate_scenario(data)
    return scenario, bench.build_world(scenario)


def client_for(data: dict, seed: int | None = None):
    """A real recording client over the simulator: (world, simulation, client, journal)."""
    scenario, world = world_for(data)
    simulation = bench.Simulation(world, scenario.seed if seed is None else seed)
    journal: list = []
    return world, simulation, bench.RecordingClient(simulation, journal.append), journal


def episode_for(data: dict, **kwargs):
    return bench.run_episode(bench.validate_scenario(data), **kwargs)


def metrics_for(data: dict, **kwargs) -> bench.Metrics:
    return bench.compute_metrics(episode_for(data, **kwargs))


def run_one(data: dict, **kwargs):
    suite = bench.run_suite([bench.validate_scenario(data)], seeds=1, **kwargs)
    (result,) = suite.scenarios
    return suite, result


def transformed(transform):
    """A contract tick whose planner and replanner pass every decision through ``transform(decision, crew)``."""

    def planner(ctx, strategy):
        crew = {member.character_id: member for member in ctx.crew}
        return tuple(transform(d, crew[d.character_id]) for d in plan_training(ctx, strategy))

    def replanner(ctx, crew, strategy, items):
        return tuple(transform(d, crew) for d in replan_after_finish(ctx, crew, strategy, items))

    return bench.make_contract_executor(planner, replanner)


@functools.lru_cache(maxsize=None)
def checked_in_suite():
    return bench.load_suite()


@functools.lru_cache(maxsize=None)
def default_run():
    """The unmodified planner over the checked-in suite at each scenario's own seed."""
    return bench.run_suite(checked_in_suite(), seeds=1)


def scenario_named(name: str):
    return next(scenario for scenario in checked_in_suite() if scenario.name == name)


# -- pure helpers -------------------------------------------------------------------------------------------------


class PureHelperTests(unittest.TestCase):
    def test_percent_is_exact_ceiling(self):
        self.assertEqual(bench.percent(7, 100), 7)
        self.assertEqual(bench.percent(55, 100), 55)
        self.assertEqual(bench.percent(1, 3), 34)
        self.assertEqual(bench.percent(0, 50), 0)
        self.assertEqual(bench.percent(90, 100), 90)

    def test_first_total_is_the_smallest_total_at_a_percent(self):
        for capacity in (50, 60, 100, 120, 150, 200):
            for pct in range(0, 101):
                with self.subTest(capacity=capacity, pct=pct):
                    total = bench.first_total(pct, capacity)
                    self.assertGreaterEqual(bench.percent(total, capacity), pct)
                    if total > 0:
                        self.assertLess(bench.percent(total - 1, capacity), pct)
        exact = {(90, 200): 179, (90, 120): 107, (90, 150): 134, (90, 100): 90, (72, 100): 72, (0, 100): 0,
                 (1, 100): 1, (-5, 100): 0, (90, 60): 54, (0, 150): 0, (0, 200): 0}
        for (pct, capacity), expected in exact.items():
            self.assertEqual(bench.first_total(pct, capacity), expected, (pct, capacity))

    def test_policy_target(self):
        cases = [
            ("defenders", "gym", 0, 200, 200, 179),  # captain: complete percent first
            ("defenders", "gym", 0, 100, 70, 70),  # cap first
            ("weapons", "academy", 0, 100, 80, 72),  # the 72% move to the gym
            ("weapons", "academy", 80, 100, 100, 74),  # already in a gym band: below its total, no headroom
            ("weapons", "gym", 0, 100, 100, 0),  # must move to the academy from the start
            ("weapons", "gym", 75, 100, 100, 90),  # secondary slot in the gym: trains to complete
            ("engines", "academy", 0, 100, 70, 0),
            ("engines", "gym", 0, 120, 90, 86),  # first total at 72% of 120
            ("rushers", "gym", 0, 100, 100, 90),
            (None, "gym", 0, 100, 70, 70),
            ("defenders", None, 0, 200, 200, 179),
        ]
        for role, room, total, capacity, cap, expected in cases:
            with self.subTest(role=role, room=room, total=total):
                self.assertEqual(bench.policy_target(role, room, total, capacity, cap), expected)

    def test_oracle_band_edges_and_labels(self):
        cases = {0: ("primary", 1), 50: ("primary", 1), 51: ("primary", 2), 64: ("primary", 2), 65: ("primary", 3),
                 71: ("primary", 3), 72: ("secondary", 1), 73: ("secondary", 1), 74: ("secondary", 2),
                 84: ("secondary", 2), 85: ("secondary", 3), 89: ("secondary", 3), 90: None, 120: None}
        for pct, expected in cases.items():
            with self.subTest(pct=pct):
                self.assertEqual(bench.oracle_band(pct), expected)
        self.assertEqual([bench.band_label(pct) for pct in (0, 64, 72, 89, 90)],
                         ["primary-1", "primary-2", "secondary-1", "secondary-3", "complete"])

    def test_oracle_cap_cases(self):
        cases = [
            ("Unique", "Huntress", 100, 70), ("Epic", "Mistycball", 100, 100), ("Epic", "Huge Hellaluya", 100, 100),
            ("Epic", "Delish", 100, 80), ("Epic", "Mistycball", 90, 90), ("Hero", "Cyber Duck", 120, 90),
            ("Special", "Huntress", 120, 100), ("Special", "Admiral Serena", 200, 200),
            ("Special", "Admiral Serena", 199, 100), ("Legendary", "King Dong", 150, 110),
            ("Common", "Visiting Trader", 60, 60), ("Elite", "Cyber Duck", 100, 100), ("Standard", "Nobody", 50, 50),
            ("Unique", "Huntress", 60, 60),
        ]
        for rarity, name, capacity, expected in cases:
            with self.subTest(rarity=rarity, name=name, capacity=capacity):
                self.assertEqual(bench.oracle_cap(rarity, name, capacity), expected)

    def test_consumable_allowed_windows(self):
        cases = [("Common", 0, 0, True), ("Common", 29, 0, True), ("Common", 30, 0, False), ("Common", 29, 1, False),
                 ("Hero", 90, 100, True), ("Hero", 95, 100, True), ("Hero", 89, 100, False),
                 ("Hero", 90, 99, False), ("Elite", 0, 0, False)]
        for tier, pct, fatigue, expected in cases:
            with self.subTest(tier=tier, pct=pct, fatigue=fatigue):
                self.assertIs(bench.consumable_allowed(tier, pct, fatigue), expected)

    def test_research_levels_and_gates(self):
        self.assertEqual(bench.research_levels([2010, 2011, 2022, 9999]), {"Fitness": 2, "Education": 3})
        self.assertEqual(bench.research_levels([2011, 2010]), {"Fitness": 2})
        self.assertEqual(bench.research_levels([]), {})
        self.assertTrue(bench.gate_met(("primary", 1), {"Fitness": 1}))
        self.assertFalse(bench.gate_met(("primary", 2), {"Fitness": 1}))
        self.assertTrue(bench.gate_met(("primary", 2), {"Education": 2}))
        self.assertFalse(bench.gate_met(("primary", 1), {}))
        self.assertTrue(bench.gate_met(("secondary", 1), {}))
        self.assertFalse(bench.gate_met(("secondary", 2), {"Fitness": 1}))

    def test_design_research_and_role_training(self):
        self.assertEqual([bench.design_research(design) for design in (3011, 3112, 3023, 3133, 9999)],
                         [None, 2011, 2022, 2012, None])
        self.assertEqual(bench.role_training("weapons", ("primary", 2)), ("academy", 3012))
        self.assertEqual(bench.role_training("engines", ("secondary", 3)), ("academy", 3033))
        self.assertEqual(bench.role_training("defenders", ("secondary", 1)), ("gym", 3131))
        self.assertEqual(bench.role_training("rushers", ("primary", 3)), ("gym", 3123))
        self.assertEqual(bench.role_training(None, ("primary", 1)), (None, None))
        self.assertEqual(bench.role_training("weapons", None), (None, None))
        self.assertEqual(bench.role_training("pilots", ("primary", 1)), (None, None))

    def test_to_int(self):
        cases = [("12", 12), (7, 7), (True, 0), ("x", 0), ("-3", 0), (None, 0), (2.0, 0), ("", 0)]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(bench._to_int(value), expected)

    def test_covered_seconds_exact(self):
        window = (hours(0), hours(10))
        sessions = [(hours(1), hours(3)), (hours(3), hours(4)), (hours(6), hours(12))]
        self.assertEqual(bench.covered_seconds(window, sessions), 7 * 3600)
        self.assertEqual(bench.covered_seconds(window, []), 0)
        self.assertEqual(bench.covered_seconds((hours(5), hours(5)), sessions), 0)
        overlapping = [(hours(1), hours(5)), (hours(2), hours(3)), (hours(4), hours(6))]
        self.assertEqual(bench.covered_seconds(window, overlapping), 5 * 3600)
        before = [(hours(-3), hours(-1)), (hours(-1), hours(2))]
        self.assertEqual(bench.covered_seconds(window, before), 2 * 3600)

    def test_can_gain(self):
        self.assertTrue(bench.can_gain(3011, 66, 0, 100))  # 3 * 34 // 100 = 1
        self.assertFalse(bench.can_gain(3011, 67, 0, 100))  # 3 * 33 // 100 = 0
        self.assertTrue(bench.can_gain(3113, 94, 0, 100))  # 18 * 6 // 100 = 1
        self.assertFalse(bench.can_gain(3113, 95, 0, 100))
        self.assertFalse(bench.can_gain(3113, 0, 100, 100))  # at capacity
        self.assertTrue(bench.can_gain(3113, 0, 99, 100))
        self.assertFalse(bench.can_gain(4242, 0, 0, 100))

    def test_score_formula(self):
        base = bench.Metrics(progress=0.5, utilisation=0.8)
        self.assertEqual(base.reliability, 1.0)
        self.assertEqual(bench.score_scenario(base), 57.5)
        refused = bench.Metrics(progress=0.5, utilisation=0.8, accepted_actions=3, refused_actions=1)
        self.assertEqual(refused.reliability, 0.75)
        self.assertEqual(bench.score_scenario(refused), 43.125)
        no_window = bench.Metrics(progress=0.5, utilisation=None)
        self.assertEqual(bench.score_scenario(no_window), 50.0)  # U left out, weights renormalised to P
        self.assertEqual(bench.score_scenario(bench.Metrics(progress=0.0, utilisation=1.0)), 25.0)
        self.assertEqual(bench.score_scenario(bench.Metrics(progress=1.0, utilisation=0.0)), 75.0)
        self.assertEqual(bench.score_scenario(bench.Metrics(progress=1 / 3, utilisation=1 / 3)), 33.333333)
        for kind in bench.GATED_VIOLATIONS:
            with self.subTest(kind=kind):
                violating = bench.Metrics(progress=0.5, utilisation=0.8, violations={kind: 1})
                self.assertEqual(violating.hard_violations, 1)
                self.assertEqual(bench.score_scenario(violating), 0.0)
        ungated = bench.Metrics(progress=0.5, utilisation=0.8, violations={"not-an-oracle": 4})
        self.assertEqual((ungated.hard_violations, bench.score_scenario(ungated)), (0, 57.5))
        self.assertEqual(bench.score_scenario(None), 0.0)
        self.assertEqual(bench.SCORE_WEIGHTS, {"progress": 0.75, "utilisation": 0.25})

    def test_composite_mean_rounded(self):
        self.assertEqual(bench.composite([10.0, 20.0, 40.0]), 23.333333)
        self.assertEqual(bench.composite([50.0]), 50.0)
        self.assertEqual(bench.composite([]), 0.0)


# -- oracles over hand-built journals -----------------------------------------------------------------------------


def before(total=0, fatigue=0, room=7001, design=0, end=""):
    return bench.Before(total, fatigue, room, design, end)


def call(method, args, cid, prior, *, at=AT0, outcome="ok", faults=(), seq=1):
    return bench.Call(seq, 1, at, method, tuple(args), str(cid), prior, outcome, tuple(faults), ())


ORACLE_CREW = [
    member(101, "Admiral Serena", "captain"),
    member(102, "Huntress", "unique"),
    member(103, "Delish", "epic"),
    member(104, "Visiting Trader", "common"),
    member(105, "Delish", "epic", room=7005),
]


def kinds(data, *calls):
    _, world = world_for(data)
    return [found.kind for found in bench.journal_violations(world, calls)]


def oracle_world(research=("Fitness",)):
    return mini(rooms=ROOMS, crew=ORACLE_CREW, research=list(research))


class OracleTests(unittest.TestCase):
    def test_a_policy_start_breaks_nothing(self):
        self.assertEqual(kinds(oracle_world(), call("addTraining", ("3111", "101"), 101, before())), [])
        weapons = call("addTraining", ("3011", "103"), 103, before(room=7002))
        self.assertEqual(kinds(oracle_world(), weapons), [])

    def test_each_start_rule(self):
        cases = {
            "cap": (ALL_RESEARCH, ("3113", "102"), 102, before(total=70), ["cap"]),
            "complete": (ALL_RESEARCH, ("3111", "101"), 101, before(total=179), ["complete"]),
            "fatigue": (["Fitness"], ("3111", "101"), 101, before(fatigue=100), ["fatigue"]),
            "fatigue 99": (["Fitness"], ("3111", "101"), 101, before(fatigue=99), []),
            "gate": ([], ("3111", "101"), 101, before(), ["research-gate"]),
            "room": (["Fitness"], ("3011", "103"), 103, before(), ["room"]),
            "no training room": (["Fitness"], ("3011", "105"), 105, before(room=7005), ["room"]),
            "design": (["Fitness"], ("3011", "101"), 101, before(), ["design"]),
            "unknown design": (["Fitness"], ("9999", "101"), 101, before(), ["design"]),
            "no role": (["Fitness"], ("3111", "104"), 104, before(), ["design"]),
            "design research": (["Education", "Education Lv2"], ("3112", "101"), 101, before(total=110),
                                ["design-research"]),
            "design research met": (["Fitness", "Fitness Lv2"], ("3112", "101"), 101, before(total=110), []),
        }
        for name, (research, args, cid, prior, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(kinds(oracle_world(research), call("addTraining", args, cid, prior)), expected)

    def test_each_consumable_rule(self):
        running = {"design": 3111, "end": stamp(1)}
        cases = {
            "allowed in progress": (["Fitness"], "5001", 101, before(**running), []),
            "hero too early": (["Fitness"], "5011", 101, before(**running), ["consumable"]),
            "not an item": (["Fitness"], "4242", 101, before(**running), ["consumable"]),
            "no role": (["Fitness"], "5001", 104, before(), ["consumable"]),
            "bridge": (["Fitness"], "5002", 105, before(room=7005), ["consumable"]),
            "at cap": (["Fitness"], "5001", 102, before(total=70, **running), ["cap", "consumable"]),
            "startable": (["Fitness"], "5001", 101, before(), ["consumable-precedence"]),
            "should move": (["Fitness"], "5002", 103, before(), ["consumable-precedence"]),
            "finished, not collected": (["Fitness"], "5001", 101, before(design=3111, end=AT0),
                                        ["consumable-precedence"]),
            "gate unmet": ([], "5001", 101, before(), []),
            "complete": (["Fitness"], "5011", 101, before(total=180, fatigue=100), []),
            # design research missing is a fallback branch: only the Common rule (<= 29%) is broken, not precedence
            "design research missing": (["Education", "Education Lv2"], "5001", 101, before(total=110),
                                        ["consumable"]),
        }
        for name, (research, item, cid, prior, expected) in cases.items():
            with self.subTest(case=name):
                found = kinds(oracle_world(research), call("useConsumable", (item, str(cid)), cid, prior))
                self.assertEqual(found, expected)

    def test_violation_details_and_fields(self):
        _, world = world_for(oracle_world())
        calls = [
            call("useConsumable", ("5011", "101"), 101, before(design=3111, end=stamp(1)), seq=4),
            call("useConsumable", ("4242", "104"), 104, before(), seq=5, at=stamp(2)),
            call("useConsumable", ("5001", "101"), 101, before(), seq=6),
            call("useConsumable", ("5002", "103"), 103, before(), seq=7),
            call("addTraining", ("3011", "105"), 105, before(room=7005), seq=8),
            call("addTraining", ("3111", "102"), 102, before(total=70, fatigue=100), seq=9),
        ]
        found = bench.journal_violations(world, calls)
        self.assertEqual(found[0], bench.Violation("consumable", 4, AT0, 101,
                                                   "Hero rule does not allow 0% with fatigue 0"))
        self.assertEqual(found[1], bench.Violation("consumable", 5, stamp(2), 104,
                                                   "item 4242 is not a catalog consumable; no role"))
        self.assertEqual(found[2].detail, "a start was possible")
        self.assertEqual(found[3].detail, "the crew should move to academy")
        self.assertEqual(found[4].detail, "start in room type None for a academy slot")
        self.assertEqual([(v.kind, v.detail) for v in found[5:]], [
            ("cap", "start at 70 TP >= cap 70 TP"),
            ("fatigue", "start at fatigue 100 >= 100"),
            ("research-gate", "primary T3 gate unmet at 70%"),
            ("design", "design 3111 is not the role's training 3113"),
        ])
        complete = bench.journal_violations(world, [call("addTraining", ("3111", "101"), 101, before(total=180))])
        self.assertEqual(complete[0].detail, "start at 90% >= 90%")
        _, gated = world_for(oracle_world([]))
        gate = bench.journal_violations(gated, [call("addTraining", ("3111", "101"), 101, before())])
        self.assertEqual(gate[0].detail, "primary T1 gate unmet at 0%")
        _, split = world_for(oracle_world(["Education", "Education Lv2"]))
        research = bench.journal_violations(split, [call("addTraining", ("3112", "101"), 101, before(total=110))])
        self.assertEqual(research[0].detail, "design 3112 needs research 2011")

    def test_finishes_and_unknown_crew_are_never_judged(self):
        ignored = [
            call("finishTraining", ("102",), 102, before(total=70)),
            call("addTraining", ("3111", "999"), 999, None),
            call("addTraining", ("3111", "x"), "x", before(total=70)),
        ]
        self.assertEqual(kinds(oracle_world(), *ignored), [])

    def test_policy_view_branches(self):
        _, world = world_for(oracle_world(["Education", "Education Lv2"]))
        view = bench.policy_view(world, world.crew[101], before(total=110), AT0)
        self.assertEqual((view.pct, view.band, view.slot_room, view.expected_design), (55, ("primary", 2), "gym", 3112))
        self.assertEqual((view.gate_ok, view.design_research_ok, view.startable, view.consumable_branch),
                         (True, False, False, True))
        tired = bench.policy_view(world, world.crew[101], before(fatigue=100), AT0)
        self.assertEqual((tired.startable, tired.consumable_branch), (False, True))
        running = bench.policy_view(world, world.crew[101], before(design=3111, end=stamp(1)), AT0)
        self.assertEqual((running.in_progress, running.consumable_branch), (True, True))
        done = bench.policy_view(world, world.crew[101], before(design=3111, end=AT0), AT0)
        self.assertEqual((done.in_progress, done.startable), (False, True))
        moving = bench.policy_view(world, world.crew[103], before(), AT0)
        self.assertEqual((moving.placed, moving.startable, moving.consumable_branch), (True, False, False))
        capped = bench.policy_view(world, world.crew[102], before(total=70), AT0)
        self.assertEqual((capped.placed, capped.consumable_branch), (False, False))
        bridge = bench.policy_view(world, world.crew[105], before(room=7005), AT0)
        self.assertEqual((bridge.room_type, bridge.placed), (None, False))
        nobody = bench.policy_view(world, world.crew[104], before(), AT0)
        self.assertEqual((nobody.role, nobody.placed), (None, False))


# -- the recording client -----------------------------------------------------------------------------------------


class RecordingClientTests(unittest.TestCase):
    def test_training_calls_are_journaled_with_the_simulators_record(self):
        world, simulation, client, journal = client_for(
            mini(crew=[captain(fatigue=3, stats={"Attack": 5})], inventory={"Common ATK Consumable": 1}))
        client.tick = 4
        self.assertIs(client.addTraining("3111", "101"), True)
        self.assertEqual(journal[0], bench.Call(1, 4, AT0, "addTraining", ("3111", "101"), "101",
                                                before(5, 3, 7001, 0, ""), "ok", (), (1,)))
        self.assertIs(client.addTraining(3111, 101), False)  # busy: the simulator refuses, the args are kept as text
        self.assertEqual((journal[1].args, journal[1].outcome, journal[1].log_seqs), (("3111", "101"), "refused", (2,)))
        self.assertEqual(journal[1].before, before(5, 13, 7001, 3111, stamp(1)))
        self.assertIs(client.finishTraining("101"), False)  # early
        simulation.advance(timedelta(hours=1))
        self.assertIs(client.finishTraining("101"), True)
        self.assertEqual((journal[3].at, journal[3].outcome, journal[3].log_seqs), (stamp(1), "ok", (4,)))
        self.assertIs(client.useConsumable("5099", "101"), False)  # not in the inventory: no call, no log entry
        self.assertEqual((journal[4].outcome, journal[4].log_seqs), ("refused", ()))
        self.assertIs(client.addTraining("3111", "999"), False)
        self.assertEqual((journal[5].before, journal[5].character_id, journal[5].seq), (None, "999", 6))
        self.assertEqual(journal[0].to_dict()["args"], ["3111", "101"])
        self.assertEqual(journal[0].to_dict()["before"], {"total": 5, "fatigue": 3, "room_id": 7001,
                                                          "training_design_id": 0, "training_end": ""})
        self.assertEqual([entry["seq"] for entry in simulation.log()], [1, 2, 3, 4, 5])

    def test_outcomes_under_faults(self):
        faults = [
            {"kind": "reject", "method": "addTraining", "call": 1},
            {"kind": "raise", "method": "addTraining", "call": 2},
            {"kind": "ambiguous-response", "method": "finishTraining", "call": 1},
            {"kind": "ambiguous-response", "method": "activateItem3", "call": 1},
            {"kind": "contradicted-effect", "method": "addTraining", "call": 3, "training_design": "Kickbox"},
        ]
        _, simulation, client, journal = client_for(mini(faults=faults, inventory={"Common ATK Consumable": 2}))
        self.assertIs(client.addTraining("3111", "101"), False)
        self.assertEqual((journal[0].outcome, journal[0].faults, journal[0].log_seqs),
                         ("refused", ("day 0 addTraining #1: reject",), ()))
        with self.assertRaises(SimulatedFaultError):
            client.addTraining("3111", "101")
        self.assertEqual((journal[1].outcome, journal[1].log_seqs), ("raised", (1,)))  # the effect happened
        simulation.advance(timedelta(hours=1))
        self.assertIsNone(client.finishTraining("101"))
        self.assertEqual((journal[2].outcome, journal[2].log_seqs), ("ambiguous", (2,)))
        self.assertIs(client.useConsumable("5001", "101"), True)  # the Client answers True with no reply
        self.assertEqual(journal[3].outcome, "ambiguous")
        self.assertIs(client.addTraining("3111", "101"), True)
        self.assertEqual(journal[4].outcome, "ok")
        self.assertEqual(simulation.log()[-1]["design_id"], "3131")
        self.assertEqual(len(simulation.fired()), 5)

    def test_call_outcome(self):
        self.assertEqual(bench.call_outcome(True, ()), "ok")
        self.assertEqual(bench.call_outcome(False, ()), "refused")
        self.assertEqual(bench.call_outcome(None, ()), "ambiguous")
        self.assertEqual(bench.call_outcome(True, ("day 0 activateItem3 #1: ambiguous-response",)), "ambiguous")
        self.assertEqual(bench.call_outcome(1, ()), "refused")

    def test_reads_build_a_context(self):
        rooms = ROOMS + [{"id": 7003, "design": "Galaxy Gym"}, {"id": 7004, "design": "Lunar College"}]
        _, _, client, journal = client_for(mini(rooms=rooms, research=["Fitness", "Fitness Lv2"],
                                                inventory={"Common ATK Consumable": 2, "Hero HP Consumable": 1}))
        payloads = bench.read_payloads(client)
        self.assertEqual(sorted(payloads), sorted(keyword for _, _, keyword in bench.CLIENT_READS))
        ctx = build_context(now=T0, **payloads)
        self.assertEqual(len(ctx.designs), 21)
        self.assertEqual(dict(ctx.rooms), {7001: "GYM", 7002: "Academy", 7005: "Bridge", 7003: "Galaxy Gym",
                                           7004: "Lunar College"})
        self.assertEqual((ctx.research_levels["Fitness"], ctx.research_levels["Fitness Lv2"]), (2, 2))
        self.assertNotIn("Education", ctx.research_levels)
        self.assertEqual(dict(ctx.items), {5001: 2, 5016: 1})
        (crew,) = ctx.crew
        self.assertEqual((crew.name, crew.rarity, crew.training_capacity, crew.room_name),
                         ("Admiral Serena", "Special", 200, "GYM"))
        for role in DEFAULT_ROLES:
            for slot in (role.primary, role.secondary):
                for name in slot.trainings:
                    self.assertIsNotNone(pick_design(name, slot.room_type, ctx.designs), name)
        by_id = {design.id: design for design in ctx.designs}
        self.assertEqual((by_id[3011].tier, by_id[3011].stat, by_id[3011].required_room_type),
                         (1, "WeaponImprovement", "academy"))
        self.assertEqual((by_id[3011].required_research, by_id[3112].required_research), (None, "Fitness Lv2"))
        self.assertEqual(journal, [])  # reads are never journaled

    def test_synthesized_reads_use_the_clients_envelopes(self):
        _, _, client, _ = client_for(mini(research=["Fitness"]))
        for method in ("listRoomsViaAccessToken", "listRoomDesigns2", "listAllResearches", "listAllResearchDesigns2"):
            self.assertIs(getattr(client, method)(), True)
        self.assertEqual(client.roomsViaAccessToken, {"RoomService": {"ListRoomsViaAccessToken": {"Rooms": {"Room": [
            {"@RoomId": "7001", "@RoomDesignId": "1000", "@RoomStatus": "Normal"}]}}}})
        designs = client.roomDesigns["RoomService"]["ListRoomDesigns"]["RoomDesigns"]["RoomDesign"]
        self.assertEqual(designs, list(bench.room_design_records()))
        self.assertEqual(client.allResearches, {"ResearchService": {"ListAllResearches": {"Researches": {"Research": [
            {"@ResearchDesignId": "2010", "@ResearchState": "Completed"}]}}}})
        research = client.allResearchDesigns["ResearchService"]["ListAllResearchDesigns"]["ResearchDesigns"]
        self.assertEqual(research["ResearchDesign"], list(bench.research_design_records()))

    def test_a_failed_account_read_skips_payloads_and_keeps_the_clock(self):
        faults = [{"kind": "snapshot-failure", "method": "snapshot", "day": 0, "call": 2}]
        _, simulation, client, _ = client_for(mini(faults=faults))
        self.assertIsNotNone(bench.read_payloads(client))
        self.assertEqual(simulation.now, T0)
        self.assertIs(client.listAllCharactersOfUser(), False)
        self.assertEqual(simulation.now, T0)
        self.assertIsNotNone(bench.read_payloads(client))
        self.assertEqual(simulation.fired(), ("day 0 snapshot #2: snapshot-failure",))

    def test_read_payloads_fails_closed(self):
        class Raising:
            def listAllCharactersOfUser(self):
                raise ConnectionError("down")

        class Refusing:
            def listAllCharactersOfUser(self):
                return None

        self.assertIsNone(bench.read_payloads(Raising()))
        self.assertIsNone(bench.read_payloads(Refusing()))

    def test_only_reads_and_training_calls_are_reachable(self):
        _, simulation, client, _ = client_for(mini())
        for name in ("addResearch", "game", "training", "collectAllResources", "_journal"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(AttributeError, "not a training call or read"):
                    getattr(client, name)
        with self.assertRaises(AttributeError):
            client.trainingFinish  # no finish yet
        client.addTraining("3111", "101")
        simulation.advance(timedelta(hours=1))
        client.finishTraining("101")
        self.assertEqual(client.trainingFinish["TrainingService"]["FinishTraining"]["Character"]["@CharacterId"], "101")
        self.assertIs(client.listItemsOfAShip(), True)
        self.assertIn("ItemService", client.itemsOfAShip)


# -- the contract executor ----------------------------------------------------------------------------------------

FOUR_CREW = [
    member(1, "Admiral Serena", "captain"),
    member(2, "King Dong", "legendary"),
    member(3, "Huntress", "unique"),
    member(4, "Visiting Trader", "common"),
]
RESPONSE_OF = {attribute: keyword for _, attribute, keyword in bench.CLIENT_READS}
READS = {method for method, _, _ in bench.CLIENT_READS}


class FakeClient:
    """Scripted training results over payloads read once from a real world; records every training call."""

    def __init__(self, results=None, fail_read=None, inventory=None, finish_id="1"):
        _, simulation, client, _ = client_for(mini(crew=FOUR_CREW, inventory=inventory or {"Common ATK Consumable": 3}))
        self.payloads = bench.read_payloads(client)
        record = dict(simulation.crew_record(finish_id), **{"@AttackImprovement": "9"})
        self.trainingFinish = {"TrainingService": {"FinishTraining": {"Character": record}}}
        self.results = dict(results or {})
        self.fail_read = fail_read
        self.calls = []

    def __getattr__(self, name):
        if name in READS:
            return lambda: name != self.fail_read
        if name in RESPONSE_OF:
            return self.payloads[RESPONSE_OF[name]]
        raise AttributeError(name)

    def _call(self, method, *args):
        self.calls.append((method, args))
        result = self.results.get(method, True)
        if isinstance(result, Exception):
            raise result
        return result

    def addTraining(self, *args):
        return self._call("addTraining", *args)

    def finishTraining(self, *args):
        return self._call("finishTraining", *args)

    def useConsumable(self, *args):
        return self._call("useConsumable", *args)


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


class ContractExecutorTests(unittest.TestCase):
    def test_decisions_map_to_catalog_calls_and_replans(self):
        client = FakeClient()
        planner = scripted(
            FinishTraining(1, "ended"),
            StartTraining(1, 3111, "advisory"),
            StartTraining(2, 3112, "start"),
            UseConsumable(3, 5001, "use"),
            Skip(4, "unknown_role", "no role"),
            Skip(3, "not_in_training_room", "move to gym"),
        )
        replanner = replanner_returning(StartTraining(1, 3113, "replanned"))
        report = bench.make_contract_executor(planner, replanner)(client, T0)
        self.assertEqual(client.calls, [
            ("finishTraining", ("1",)),
            ("addTraining", ("3113", "1")),
            ("addTraining", ("3112", "2")),
            ("useConsumable", ("5001", "3")),
        ])
        self.assertEqual(report, bench.TickReport((("unknown_role", "no role"), ("not_in_training_room", "move to gym")),
                                                  0, 1))
        (rebuilt, items) = replanner.calls[0]
        self.assertEqual((rebuilt.character_id, rebuilt.stats["AttackImprovement"]), (1, 9))
        self.assertEqual(items, {5001: 2})
        self.assertEqual(planner.calls[0].now, T0)
        self.assertEqual(len(planner.calls), 1)

    def test_the_advisory_is_dropped_unless_the_finish_succeeded(self):
        for result in (False, None, ConnectionError("lost")):
            with self.subTest(result=result):
                client = FakeClient(results={"finishTraining": result})
                planner = scripted(FinishTraining(1, "ended"), StartTraining(1, 3111, "advisory"),
                                   StartTraining(2, 3112, "start"))
                replanner = replanner_returning(StartTraining(1, 3113, "replanned"))
                report = bench.make_contract_executor(planner, replanner)(client, T0)
                self.assertEqual(client.calls, [("finishTraining", ("1",)), ("addTraining", ("3112", "2"))])
                self.assertEqual(replanner.calls, [])
                self.assertEqual(report, bench.TickReport((), 1, 0))

    def test_a_finish_response_for_another_crew_drops_the_advisory(self):
        client = FakeClient(finish_id="2")
        planner = scripted(FinishTraining(1, "ended"), StartTraining(1, 3111, "advisory"))
        replanner = replanner_returning(StartTraining(1, 3113, "replanned"))
        report = bench.make_contract_executor(planner, replanner)(client, T0)
        self.assertEqual((client.calls, report.dropped, replanner.calls), ([("finishTraining", ("1",))], 1, []))

    def test_replan_stock_excludes_finished_advisory_consumables(self):
        client = FakeClient(inventory={"Common ATK Consumable": 3})
        planner = scripted(
            FinishTraining(1, "ended"),
            UseConsumable(1, 5001, "advisory"),
            UseConsumable(2, 5001, "use"),
            FinishTraining(3, "ended"),
            UseConsumable(3, 5001, "advisory"),
        )
        replanner = replanner_returning(UseConsumable(1, 5001, "replanned"), Skip(3, "complete", "done"))
        client.trainingFinish = {"TrainingService": {"FinishTraining": {"Character": dict(
            client.trainingFinish["TrainingService"]["FinishTraining"]["Character"])}}}
        records = {cid: dict(client.trainingFinish["TrainingService"]["FinishTraining"]["Character"],
                             **{"@CharacterId": str(cid)}) for cid in (1, 3)}
        original = client.finishTraining

        def finish(cid):
            client.trainingFinish = {"TrainingService": {"FinishTraining": {"Character": records[int(cid)]}}}
            return original(cid)

        client.finishTraining = finish
        report = bench.make_contract_executor(planner, replanner)(client, T0)
        self.assertEqual([items for _, items in replanner.calls], [{5001: 2}, {5001: 1}])
        self.assertEqual(report, bench.TickReport((("complete", "done"),), 0, 2))
        self.assertEqual(client.calls, [("finishTraining", ("1",)), ("useConsumable", ("5001", "1")),
                                        ("useConsumable", ("5001", "2")), ("finishTraining", ("3",))])

    def test_a_failed_read_skips_the_tick(self):
        for method in ("listAllCharactersOfUser", "listAllResearchDesigns2"):
            with self.subTest(method=method):
                client = FakeClient(fail_read=method)
                planner = scripted(StartTraining(2, 3112, "start"))
                report = bench.make_contract_executor(planner)(client, T0)
                self.assertEqual((planner.calls, client.calls), ([], []))
                self.assertEqual(report, bench.TickReport(observe_failed=True))

    def test_execute_reports_the_executors_view(self):
        cases = [(True, "ok"), (False, "refused"), (None, "ambiguous"), (ConnectionError("x"), "raised"), (0, "refused")]
        for result, expected in cases:
            with self.subTest(result=result):
                client = FakeClient(results={"addTraining": result})
                self.assertEqual(bench.execute(client, StartTraining(2, 3112, "start")), expected)
        with self.assertRaisesRegex(bench.BenchmarkError, "never executed"):
            bench.execute(FakeClient(), Skip(2, "complete", "done"))
        client = FakeClient(results={"addTraining": ValueError("bug")})
        with self.assertRaises(ValueError):  # only connection errors are an outcome; a bug propagates
            bench.execute(client, StartTraining(2, 3112, "start"))

    def test_report_dict(self):
        report = bench.TickReport((("fatigued", "fatigue 100 >= 100"),), 2, 3, True)
        self.assertEqual(report.to_dict(), {"skips": [["fatigued", "fatigue 100 >= 100"]], "dropped": 2,
                                            "replans": 3, "observe_failed": True})


# -- whole episodes: exact, seed-determined values ----------------------------------------------------------------


def progress_by_hand(world, log) -> float:
    """P computed independently: sum over accepted gains of clamped TP x seconds left, over headroom x horizon."""
    reached, area = {}, 0
    for entry in log:
        if entry["accepted"] and entry["op"] in ("finish", "consume"):
            meta = world.crew[int(entry["character_id"])]
            now = min(meta.headroom, max(0, entry["total"] - meta.initial_total))
            seconds = (world.horizon_end - bench.parse_time(entry["at"])).total_seconds()
            area += (now - reached.get(meta.character_id, 0)) * seconds
            reached[meta.character_id] = now
    span = (world.horizon_end - world.start).total_seconds()
    return area / (sum(meta.headroom for meta in world.crew.values()) * span)


class EpisodeMetricsTests(unittest.TestCase):
    def test_mini_episode_exact(self):
        episode = episode_for(mini())
        metrics = bench.compute_metrics(episode)
        self.assertEqual(metrics.actions, {"addTraining": 24, "finishTraining": 23, "useConsumable": 0})
        self.assertEqual(metrics.outcomes, {"ok": 47})
        self.assertEqual((metrics.accepted_actions, metrics.refused_actions), (47, 0))
        self.assertEqual((metrics.eligible_crew_hours, metrics.productive_crew_hours, metrics.idle_crew_hours),
                         (24.0, 24.0, 0.0))
        self.assertEqual(metrics.utilisation, 1.0)
        self.assertEqual(metrics.tp_gained, 41)
        self.assertEqual((metrics.eligible_crew, metrics.headroom, metrics.tp_per_crew_day), (1, 179, 41.0))
        self.assertEqual(metrics.progress, progress_by_hand(episode.world, episode.log))
        self.assertEqual(metrics.progress, 0.12523277467411545)
        self.assertEqual(bench.score_scenario(metrics), 34.392458)
        self.assertEqual(metrics.starts_by_band, {"primary-1": 24})
        self.assertEqual(metrics.sessions_by_room, {"GYM": 24})
        self.assertEqual((metrics.zero_gain_sessions, metrics.crew_completed, metrics.cap_overshoot_tp), (0, 0, 0))
        self.assertEqual(metrics.violations, dict.fromkeys(bench.VIOLATION_KINDS, 0))
        self.assertEqual(len(episode.journal), 47)
        self.assertEqual(len(episode.reports), 24)
        self.assertEqual([call.tick for call in episode.journal[:3]], [1, 2, 2])

    def test_a_loaded_session_is_busy_until_it_ends(self):
        loaded = {"design": "Bench Press", "ends_after_minutes": 150}
        metrics = metrics_for(mini(crew=[captain(training=loaded)]))
        self.assertEqual(metrics.skip_reasons, {"training_in_progress": 3})
        self.assertEqual((metrics.eligible_crew_hours, metrics.productive_crew_hours, metrics.idle_crew_hours),
                         (24.0, 23.5, 0.5))
        self.assertEqual(metrics.loaded_sessions_collected, 1)
        self.assertEqual(metrics.actions, {"addTraining": 21, "finishTraining": 21, "useConsumable": 0})

    def test_the_window_ends_at_the_room_limit(self):
        crew = [member(201, "Delish", "elite", room=7002, stats={"Weapon": 65})]
        data = mini(rooms=[{"id": 7002, "design": "Academy"}], research=ALL_RESEARCH, crew=crew)
        metrics = metrics_for(data)
        self.assertEqual((metrics.headroom, metrics.progress, metrics.utilisation), (7, 0.75, 1.0))
        self.assertEqual((metrics.eligible_crew_hours, metrics.productive_crew_hours), (6.0, 6.0))
        self.assertEqual((metrics.skip_moves, metrics.skip_reasons["not_in_training_room"]), (18, 18))
        self.assertEqual((metrics.crew_completed, metrics.cap_overshoot_tp), (1, 0))
        self.assertEqual(bench.score_scenario(metrics), 81.25)

    def test_one_point_of_headroom(self):
        metrics = metrics_for(mini(crew=[captain(stats={"Attack": 178})], research=ALL_RESEARCH))
        self.assertEqual((metrics.headroom, metrics.crew_completed, metrics.tp_gained), (1, 1, 16))
        self.assertEqual((metrics.eligible_crew_hours, metrics.productive_crew_hours, metrics.idle_crew_hours),
                         (6.0, 6.0, 0.0))
        self.assertEqual((metrics.progress, metrics.utilisation), (0.75, 1.0))

    def test_zero_gain_sessions_are_wasted_time(self):
        metrics = metrics_for(mini(fatigue_recovery_seconds=7200, crew=[captain(fatigue=100)]))
        self.assertEqual((metrics.zero_gain_sessions, metrics.productive_crew_hours, metrics.utilisation),
                         (11, 0.0, 0.0))
        self.assertEqual(metrics.skip_reasons, {"fatigued": 13})
        self.assertEqual((metrics.wasted_actions, metrics.tp_gained, bench.score_scenario(metrics)), (11, 0, 0.0))

    def test_genuine_refusals_count_against_reliability(self):
        wrong = transformed(lambda d, crew: StartTraining(d.character_id, 3011, "academy design in a gym")
                            if isinstance(d, StartTraining) else d)
        metrics = metrics_for(mini(), tick=wrong)
        self.assertEqual((metrics.accepted_actions, metrics.refused_actions, metrics.reliability), (0, 24, 0.0))
        self.assertEqual(metrics.refusal_codes, {"wrong-room": 24})
        self.assertEqual(metrics.outcomes, {"refused": 24})
        self.assertEqual(metrics.violations["design"], 24)
        self.assertEqual(metrics.wasted_actions, 24)

    def test_fault_counters(self):
        cases = {
            "reject": ({"kind": "reject", "method": "addTraining", "call": 1}, "fault_rejected"),
            "contradicted": ({"kind": "contradicted-effect", "method": "addTraining", "call": 1,
                              "training_design": "Kickbox"}, "contradicted"),
            "raise": ({"kind": "raise", "method": "addTraining", "call": 2}, "raised"),
            "ambiguous": ({"kind": "ambiguous-response", "method": "finishTraining", "call": 1}, "ambiguous"),
            "snapshot": ({"kind": "snapshot-failure", "method": "snapshot", "call": 3}, "read_faults"),
        }
        counters = ("fault_rejected", "contradicted", "raised", "ambiguous", "read_faults")
        for name, (fault, counter) in cases.items():
            with self.subTest(fault=name):
                metrics = metrics_for(mini(faults=[fault]))
                self.assertEqual(getattr(metrics, counter), 1)
                for other in counters:
                    if other != counter:
                        self.assertEqual(getattr(metrics, other), 0, other)
                self.assertEqual((metrics.refused_actions, metrics.reliability), (0, 1.0))
        rejected = metrics_for(mini(faults=[cases["reject"][0]]))
        # Tick 1's start is rejected and retried at tick 2, so one session (and one finish) is lost.
        self.assertEqual(rejected.actions, {"addTraining": 24, "finishTraining": 22, "useConsumable": 0})
        self.assertEqual((rejected.accepted_actions, rejected.outcomes), (45, {"ok": 45, "refused": 1}))
        self.assertEqual((rejected.refusal_codes, rejected.idle_crew_hours), ({}, 1.0))
        snapshot = metrics_for(mini(faults=[cases["snapshot"][0]]))
        self.assertEqual((snapshot.observe_failures, snapshot.eligible_crew_hours), (1, 24.0))
        raised = metrics_for(mini(faults=[{"kind": "raise", "method": "finishTraining", "call": 1, "effect": False}]))
        self.assertEqual((raised.raised, raised.dropped), (1, 1))

    def test_cap_overshoot_counts_only_what_was_gained_above_the_cap(self):
        crew = [captain(), member(3, "Huntress", "unique", stats={"Attack": 69}),
                member(4, "Huntress", "unique", stats={"Attack": 80})]  # started above her cap: gains nothing
        episode = episode_for(mini(crew=crew, research=ALL_RESEARCH))
        self.assertEqual((episode.world.crew[3].cap, episode.world.crew[4].headroom), (70, 0))
        gains = bench.gains_by_crew(episode.log)
        self.assertEqual((gains[3], gains.get(4, 0)), (15, 0))  # one primary T3 session from 69 TP
        self.assertEqual(bench.compute_metrics(episode).cap_overshoot_tp, 69 + 15 - 70)

    def test_metrics_to_dict(self):
        metrics = bench.Metrics(progress=1 / 3, utilisation=None, skip_reasons={"b": 1, "a": 2},
                                violation_samples=({"kind": "cap"},), accepted_actions=1, refused_actions=2)
        data = metrics.to_dict()
        self.assertEqual((data["progress"], data["utilisation"], data["reliability"]), (0.333333, None, 0.333333))
        self.assertEqual(list(data["skip_reasons"]), ["a", "b"])
        self.assertEqual(data["violation_samples"], [{"kind": "cap"}])
        self.assertEqual(bench.Metrics(utilisation=2 / 3).to_dict()["utilisation"], 0.666667)
        self.assertEqual(set(data), set(bench.Metrics.__dataclass_fields__) | {"reliability"})


class PureMetricTests(unittest.TestCase):
    """The metric functions on hand-built worlds, logs and journals."""

    def setUp(self):
        crew = [member(1, "Admiral Serena", "captain"), member(2, "King Dong", "legendary", stats={"Attack": 100}),
                member(3, "Visiting Trader", "common")]
        _, self.world = world_for(mini(crew=crew, days=2))

    def entry(self, seq, op, cid, at, *, accepted=True, total=0, gain=0, end=None, design="3111", requested=None):
        return {"seq": seq, "at": stamp(at), "op": op, "character_id": str(cid), "accepted": accepted,
                "total": total, "gain": {"Attack": gain} if gain else {}, "end": None if end is None else stamp(end),
                "design_id": design, "requested_design_id": requested or design, "room_id": "7001",
                "code": "ok" if accepted else "early"}

    def test_progress_area_counts_the_time_left(self):
        self.assertEqual((self.world.crew[1].headroom, self.world.crew[2].headroom, self.world.crew[3].headroom),
                         (179, 10, 0))
        log = [
            self.entry(1, "finish", 1, 12, total=50, gain=50),
            self.entry(2, "finish", 2, 24, total=104, gain=4),
            self.entry(3, "consume", 2, 36, total=130, gain=26),  # clamped at 10 headroom
            self.entry(4, "finish", 3, 1, total=20, gain=20),  # no headroom: never counts
            self.entry(5, "finish", 1, 47, accepted=False, total=50),
            self.entry(6, "start", 1, 47, total=50),
        ]
        expected = (50 * 36 + 4 * 24 + 6 * 12) / (189 * 48)
        self.assertEqual(bench.progress_area(self.world, log), expected)
        self.assertEqual(bench.progress_area(self.world, []), 0.0)

    def test_sessions_pair_starts_and_finishes(self):
        fatigued = before(fatigue=95)
        journal = [bench.Call(1, 1, stamp(0), "addTraining", ("3113", "1"), "1", before(), "ok", (), (1,)),
                   bench.Call(2, 1, stamp(0), "addTraining", ("3113", "2"), "2", fatigued, "ok", (), (2,)),
                   bench.Call(3, 9, stamp(8), "addTraining", ("3111", "1"), "1", before(), "ok", (), (5,)),
                   bench.Call(4, 9, stamp(42), "addTraining", ("3113", "3"), "3", before(), "ok", (), (7,)),
                   bench.Call(5, 9, stamp(47), "addTraining", ("3111", "1"), "1", before(), "ok", (), (8,))]
        log = [
            self.entry(1, "start", 1, 0, end=6, design="3113"),
            self.entry(2, "start", 2, 0, end=6, design="3113"),
            self.entry(3, "finish", 1, 7, total=12, gain=12),
            self.entry(4, "consume", 1, 7, total=14, gain=2),
            self.entry(5, "start", 1, 8, end=9, design="3111"),
            self.entry(6, "finish", 3, 8, accepted=False),
            self.entry(7, "start", 3, 42, end=48, design="3113"),
            self.entry(8, "start", 1, 47, end=48, design="3111"),
        ]
        sessions = bench.episode_sessions(self.world, log, journal)
        self.assertEqual(sessions, (
            bench.Session(1, hours(0), hours(6), 12, True),
            bench.Session(1, hours(47), hours(48), None, True),  # running at the horizon: it may still gain
            bench.Session(2, hours(0), hours(6), None, False),  # started at fatigue 95: 18 * 5 // 100 = 0
            bench.Session(3, hours(42), hours(48), None, True),
        ))
        self.assertEqual([bench.productive(session) for session in sessions], [True, True, False, True])
        # the 8:00 session ended uncollected before the horizon and was replaced: it never appears as a session
        uncollected = bench.episode_sessions(self.world, log[:5], journal)
        self.assertEqual(uncollected[1], bench.Session(1, hours(8), hours(9), None, False))
        self.assertFalse(bench.productive(bench.Session(1, hours(0), hours(1), 0, True)))
        running = [self.entry(1, "start", 1, 0, end=6, design="3113"), self.entry(2, "consume", 1, 2, total=2, gain=2),
                   self.entry(3, "finish", 1, 6, total=14, gain=12)]  # an item use never closes a session
        self.assertEqual(bench.episode_sessions(self.world, running, journal[:1]),
                         (bench.Session(1, hours(0), hours(6), 12, True),))
        orphan = [self.entry(1, "start", 1, 0, end=1)]  # a start the journal never saw
        self.assertEqual(bench.episode_sessions(self.world, orphan, ())[0].can_gain, False)

    def test_loaded_sessions_open_at_the_start(self):
        loaded = {"design": "Bench Press", "ends_after_minutes": 90}
        crew = [captain(training=loaded), member(2, "King Dong", "legendary", fatigue=95,
                                                 training={"design": "Bench Press", "ends_after_minutes": 60})]
        _, world = world_for(mini(crew=crew))
        self.assertEqual((world.crew[101].loaded_until, world.crew[101].loaded_can_gain), (hours(1.5), True))
        self.assertEqual(world.crew[2].loaded_can_gain, False)
        log = [self.entry(1, "finish", 101, 2, total=2, gain=2)]
        sessions = bench.episode_sessions(world, log, ())
        self.assertEqual(sessions, (bench.Session(2, hours(0), hours(1), None, False, True),
                                    bench.Session(101, hours(0), hours(1.5), 2, True, True)))

    def test_utilisation_counts_productive_time_in_each_window(self):
        sessions = (
            bench.Session(1, hours(0), hours(6), 12, True),
            bench.Session(1, hours(6), hours(12), 0, True),  # zero gain: wasted, not busy
            bench.Session(2, hours(0), hours(30), None, True),  # clipped to its window
            bench.Session(3, hours(0), hours(10), 5, True),  # no headroom: no window at all
        )
        log = [self.entry(1, "finish", 2, 20, total=110, gain=10)]  # crew 2 reaches its target at hour 20
        window, busy = bench.utilisation_seconds(self.world, log, sessions)
        self.assertEqual((window, busy), ((48 + 20) * 3600, (6 + 20) * 3600))
        fewer = bench.utilisation_seconds(self.world, log, sessions[:1] + sessions[2:])
        self.assertEqual(fewer, (window, busy))  # dropping a wasted session never lowers the score
        self.assertEqual(bench.done_at(self.world.crew[2], log, hours(48)), hours(20))
        self.assertEqual(bench.done_at(self.world.crew[1], log, hours(48)), hours(48))
        early = [self.entry(1, "start", 2, 1, total=110, end=2), self.entry(2, "finish", 2, 3, accepted=False,
                                                                           total=110)]
        self.assertEqual(bench.done_at(self.world.crew[2], early, hours(48)), hours(48))

    def test_gains_and_counts(self):
        log = [self.entry(1, "finish", 1, 1, total=3, gain=3), self.entry(2, "consume", 1, 2, total=5, gain=2),
               self.entry(3, "finish", 2, 2, accepted=False, total=100),
               self.entry(4, "start", 2, 3, end=4, design="3131", requested="3111")]
        self.assertEqual(bench.gains_by_crew(log), {1: 5, 2: 0})
        counts = bench.log_counts(self.world, log, ("day 0 addTraining #1: reject", "day 0 snapshot #2: "
                                                    "snapshot-failure", "day 0 finishTraining #1: reject"))
        self.assertEqual(counts, {"refusal_codes": {"early": 1}, "contradicted": 1, "sessions_by_room": {"GYM": 1},
                                  "fault_rejected": 2, "read_faults": 1})

    def test_journal_counts(self):
        reject = ("day 0 addTraining #1: reject",)
        journal = [
            bench.Call(1, 1, AT0, "addTraining", ("3111", "1"), "1", before(), "ok", (), (1,)),
            bench.Call(2, 1, AT0, "addTraining", ("3113", "2"), "2", before(total=100), "ok", (), (2,)),
            bench.Call(3, 1, AT0, "addTraining", ("3111", "1"), "1", before(), "refused", reject, ()),
            bench.Call(4, 1, AT0, "addTraining", ("3111", "1"), "1", before(), "refused", (), ()),
            bench.Call(5, 1, AT0, "useConsumable", ("5011", "1"), "1", before(), "ok", (), ()),
            bench.Call(6, 1, AT0, "useConsumable", ("4242", "1"), "1", before(), "ok", (), ()),
            bench.Call(7, 1, AT0, "finishTraining", ("1",), "1", before(), "raised", (), ()),
            bench.Call(8, 1, AT0, "finishTraining", ("1",), "1", before(), "ambiguous", (), ()),
            bench.Call(9, 1, AT0, "addTraining", ("3111", "9"), "9", None, "ok", (), ()),
        ]
        self.assertEqual(bench.journal_counts(self.world, journal), {
            "accepted_actions": 5,
            "refused_actions": 1,
            "actions": {"addTraining": 5, "finishTraining": 2, "useConsumable": 2},
            "outcomes": {"ambiguous": 1, "ok": 5, "raised": 1, "refused": 2},
            "raised": 1,
            "ambiguous": 1,
            "starts_by_band": {"primary-1": 1, "primary-3": 1},
            "consumables_by_tier": {"Hero": 1, "other": 1},
            "consumables_used": 2,
        })

    def test_tick_diagnostics(self):
        reports = [bench.TickReport((("not_in_training_room", "move to gym"), ("not_in_training_room", "room x")),
                                    1, 2),
                   bench.TickReport(observe_failed=True), bench.TickReport((("fatigued", "f"),), 0, 1)]
        self.assertEqual(bench.tick_diagnostics(reports), {
            "skip_reasons": {"fatigued": 1, "not_in_training_room": 2}, "skip_moves": 1, "dropped": 1,
            "replans": 3, "observe_failures": 1})


# -- the swap point: any tick is measured the same way ------------------------------------------------------------


def hand_written_tick(client, now):
    """A Unit 9 stand-in: reads and calls the client directly (no catalog, no report), same contract."""
    names = ("allCharactersOfUser", "allCharacterDesigns", "trainingDesigns", "itemsOfAShip", "roomsViaAccessToken",
             "roomDesigns", "allResearches", "allResearchDesigns")
    reads = ("listAllCharactersOfUser", "listAllCharacterDesigns2", "listAllTrainingDesigns2", "listItemsOfAShip",
             "listRoomsViaAccessToken", "listRoomDesigns2", "listAllResearches", "listAllResearchDesigns2")
    if not all(getattr(client, read)() for read in reads):
        return None
    raw = dict(zip(("characters_raw", "character_designs_raw", "training_designs_raw", "items_raw", "rooms_raw",
                    "room_designs_raw", "researches_raw", "research_designs_raw"),
                   (getattr(client, name) for name in names)))
    ctx = build_context(now=now, **raw)
    strategy = bench.default_strategy()
    plan = plan_training(ctx, strategy)
    finished = {d.character_id for d in plan if isinstance(d, FinishTraining)}
    stock = remaining_stock(ctx.items, plan, exclude_character_ids=finished)
    collected = set()
    for decision in plan:
        cid = decision.character_id
        if isinstance(decision, FinishTraining):
            try:
                if client.finishTraining(str(cid)) is True:
                    collected.add(cid)
            except ConnectionError:
                pass
            continue
        if cid in finished:
            if cid not in collected:
                continue
            crew = parse_crew(client.trainingFinish, raw["character_designs_raw"], raw["rooms_raw"],
                                    raw["room_designs_raw"])[0]
            (decision,) = replan_after_finish(ctx, crew, strategy, stock)
            stock = remaining_stock(stock, (decision,))
        try:
            if isinstance(decision, StartTraining):
                client.addTraining(str(decision.training_design_id), str(cid))
            elif isinstance(decision, UseConsumable):
                client.useConsumable(str(decision.item_design_id), str(cid))
        except ConnectionError:
            pass
    return None


SWAP_WORLD = mini(
    days=2,
    research=["Fitness", "Fitness Lv2"],
    inventory={"Common ATK Consumable": 2},
    crew=[captain(), member(2, "King Dong", "legendary", stats={"Attack": 60}), member(3, "Huntress", "unique")],
    faults=[{"kind": "raise", "method": "finishTraining", "day": 0, "call": 2, "effect": False},
            {"kind": "ambiguous-response", "method": "addTraining", "day": 1, "call": 3}],
)


class SwapPointTests(unittest.TestCase):
    def test_a_hand_written_tick_is_measured_identically(self):
        contract = episode_for(SWAP_WORLD)
        hand = episode_for(SWAP_WORLD, tick=hand_written_tick)
        self.assertEqual(hand.journal, contract.journal)
        self.assertEqual(hand.log, contract.log)
        ours, theirs = bench.compute_metrics(contract), bench.compute_metrics(hand)
        diagnostics = ("skip_reasons", "skip_moves", "dropped", "replans", "observe_failures")
        self.assertEqual(replace(theirs, **{name: getattr(ours, name) for name in diagnostics}), ours)
        self.assertEqual(bench.score_scenario(theirs), bench.score_scenario(ours))
        self.assertGreater(ours.tp_gained, 0)
        self.assertEqual((ours.raised, ours.ambiguous), (1, 1))

    def test_a_tick_cannot_hide_a_violation(self):
        def careless(client, now):  # never reports anything: the journal still sees every call
            client.addTraining("3113", "3")  # Huntress at her cap
            client.useConsumable("5001", "101")  # the captain could start instead
            client.addTraining("3011", "101")  # an academy design for a gym crew

        crew = [captain(), member(3, "Huntress", "unique", stats={"Attack": 70})]
        data = mini(crew=crew, research=ALL_RESEARCH, inventory={"Common ATK Consumable": 30})
        suite, result = run_one(data, tick=careless)
        metrics = result.runs[0].metrics
        found = {kind: count for kind, count in metrics.violations.items() if count}
        self.assertEqual(found, {"cap": 24, "consumable-precedence": 24, "design": 24})
        self.assertEqual(metrics.refusal_codes, {"busy": 23, "wrong-room": 24})
        self.assertEqual((metrics.consumables_used, metrics.outcomes), (24, {"ok": 25, "refused": 47}))
        self.assertEqual(result.score, 0.0)
        verdict = bench.gate(suite, synthetic_baseline({"mini": 0.0}), deterministic=True)
        self.assertIn("cap_violation:mini", verdict.reasons)
        self.assertIn("consumable_precedence_violation:mini", verdict.reasons)


# -- test-of-tests: the score moves the right way -----------------------------------------------------------------


def resting_tick():
    """Rests crew at fatigue 90 or more instead of starting (the default planner trains up to 99)."""
    return transformed(lambda d, crew: Skip(d.character_id, "fatigued", "resting")
                       if isinstance(d, StartTraining) and crew.fatigue >= 90 else d)


class MonotonicityTests(unittest.TestCase):
    def test_resting_fatigued_crew_never_scores_lower(self):
        scenario = scenario_named("fatigued-crew")
        default = bench.run_suite([scenario]).scenarios[0]
        rested = bench.run_suite([scenario], tick=resting_tick()).scenarios[0]
        self.assertGreaterEqual(rested.score, default.score)
        wasted = [run.metrics.zero_gain_sessions for run in default.runs]
        self.assertEqual(wasted, [216, 216, 216])
        self.assertLess(max(run.metrics.zero_gain_sessions for run in rested.runs), 30)

    def test_resting_one_crew_scores_at_least_as_high_every_seed(self):
        data = mini(days=3, fatigue_recovery_seconds=7200, research=ALL_RESEARCH, crew=[captain(stats={"Attack": 130})])
        for seed in (7, 8, 9):
            with self.subTest(seed=seed):
                default = metrics_for(data, seed=seed)
                rested = metrics_for(data, seed=seed, tick=resting_tick())
                self.assertGreaterEqual(bench.score_scenario(rested), bench.score_scenario(default))
                self.assertLess(rested.zero_gain_sessions, default.zero_gain_sessions)

    def test_a_wrong_design_is_refused_and_scores_lower(self):
        scenario = scenario_named("fresh-crew")
        baseline = default_run().scenarios[[s.name for s in checked_in_suite()].index("fresh-crew")]
        for name, design in (("academy design in a gym", 3011), ("unknown design", 9999)):
            with self.subTest(case=name):
                swap = transformed(lambda d, crew, design=design: StartTraining(d.character_id, design, "wrong")
                                   if isinstance(d, StartTraining) and crew.room_name == "GYM" else d)
                result = bench.run_suite([scenario], tick=swap, seeds=1).scenarios[0]
                metrics = result.runs[0].metrics
                self.assertLess(metrics.reliability, 1.0)
                code = "wrong-room" if design == 3011 else "unknown-design"
                self.assertGreater(metrics.refusal_codes[code], 0)
                self.assertGreater(metrics.violations["design"], 0)
                self.assertLess(result.score, baseline.score)

    def test_weakened_planners_score_lower_suite_wide(self):
        suite = checked_in_suite()
        default = default_run()

        def lazy(decision, crew):  # a start only on even hours
            if isinstance(decision, StartTraining) and decision.character_id % 2 == 0:
                return Skip(decision.character_id, "training_in_progress", "lazy")
            return decision

        weak = bench.run_suite(suite, tick=transformed(lazy), seeds=1)
        self.assertLess(weak.composite, default.composite)
        noop = bench.run_suite(suite, tick=lambda client, now: None, seeds=1)
        self.assertEqual({result.name: result.score for result in noop.scenarios},
                         dict.fromkeys((s.name for s in suite), 0.0))

    def test_strategy_changes_score_lower(self):
        narrow = replace(bench.default_strategy(), room_types={"GYM": "gym", "Academy": "academy"})

        def score(name, **kwargs):
            return bench.run_suite([scenario_named(name)], seeds=1, **kwargs).composite

        self.assertLess(score("mixed-rooms", tick=bench.make_contract_executor(strategy=narrow)), score("mixed-rooms"))
        bare = bench.make_contract_executor(strategy=DEFAULT_STRATEGY)  # no consumable ids attached
        self.assertLess(score("missing-research", tick=bare), score("missing-research"))

    def test_each_policy_breach_is_caught_by_its_oracle(self):
        def mutant(decide):
            return transformed(lambda d, crew: decide(d, crew) or d)

        cases = {
            "fatigue": ("fatigued-crew", lambda d, crew: StartTraining(crew.character_id, 3111, "tired")
                        if isinstance(d, Skip) and d.reason_code == "fatigued" and crew.name == "King Dong" else None),
            "complete": ("near-cap-crew", lambda d, crew: StartTraining(crew.character_id, 3133, "more")
                         if isinstance(d, Skip) and d.reason_code == "complete" else None),
            "design-research": ("fitness-only", lambda d, crew: StartTraining(crew.character_id, 3012, "ungated")
                                if isinstance(d, Skip) and d.reason_code == "research_missing"
                                and crew.name == "Galactic Succubus" else None),
            "consumable-precedence": ("missing-research", lambda d, crew: UseConsumable(crew.character_id, 5001,
                                                                                        "instead")
                                      if isinstance(d, StartTraining) else None),
            "room": ("mixed-rooms", lambda d, crew: StartTraining(crew.character_id, 3111, "no move")
                     if isinstance(d, Skip) and d.detail == "move to gym" else None),
        }
        for kind, (name, decide) in cases.items():
            with self.subTest(oracle=kind):
                result = bench.run_suite([scenario_named(name)], tick=mutant(decide), seeds=1).scenarios[0]
                self.assertGreater(result.violations()[kind], 0)
                self.assertEqual(result.score, 0.0)


# -- seeds, determinism and the suite -----------------------------------------------------------------------------


class SuiteSeedTests(unittest.TestCase):
    def test_a_scenario_is_the_mean_of_its_seeds(self):
        suite = bench.run_suite([bench.validate_scenario(mini())])
        (result,) = suite.scenarios
        self.assertEqual([run.seed for run in result.runs], [7, 8, 9])
        scores = [run.score for run in result.runs]
        self.assertEqual(scores[:2], [34.392458, 34.130587])
        self.assertEqual(result.score, bench.composite(scores))
        self.assertEqual(result.spread, round(max(scores) - min(scores), 6))
        self.assertEqual(suite.composite, result.score)
        joined = ",".join(run.trace_digest for run in result.runs)
        self.assertEqual(result.trace_digest, "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest())
        other = bench.run_suite([bench.validate_scenario(mini())], base_seed=8, seeds=2).scenarios[0]
        self.assertEqual([run.seed for run in other.runs], [8, 9])
        self.assertEqual(other.runs[0], result.runs[1])
        self.assertEqual(bench.seeds_for(bench.validate_scenario(mini()), 100), (100, 101, 102))
        self.assertEqual(bench.SEED_COUNT, 3)

    def test_trace_digest_moves_with_behaviour(self):
        first = episode_for(mini())
        self.assertEqual(bench.trace_digest(first), bench.trace_digest(episode_for(mini())))
        self.assertNotEqual(bench.trace_digest(first), bench.trace_digest(episode_for(mini(), seed=8)))
        skipped = replace(first, reports=first.reports[:-1] + (bench.TickReport((("complete", "x"),)),))
        self.assertNotEqual(bench.trace_digest(first), bench.trace_digest(skipped))
        # The digest is baseline.json's contract: pinned so its format cannot change unnoticed.
        self.assertEqual(bench.trace_digest(first),
                         "sha256:f89f30168a5e3ce2bcb63819fa060bdec10618eb5d69c39f8611ccbd65655b7d")

    def test_a_crash_in_one_seed_is_a_scenario_error(self):
        runs = {"count": 0}
        contract = bench.make_contract_executor()

        def flaky(client, now):
            if now == T0:
                runs["count"] += 1
            if runs["count"] == 2:
                raise RuntimeError("planner exploded")
            return contract(client, now)

        suite = bench.run_suite([bench.validate_scenario(mini())], tick=flaky)
        (result,) = suite.scenarios
        self.assertEqual([run.error for run in result.runs], [None, "RuntimeError: planner exploded", None])
        self.assertEqual(result.error, "seed 8: RuntimeError: planner exploded")
        self.assertEqual((result.runs[1].score, result.runs[1].metrics, result.runs[1].trace_digest), (0.0, None, ""))
        self.assertEqual(result.score, bench.composite([result.runs[0].score, 0.0, result.runs[2].score]))
        verdict = bench.gate(suite, synthetic_baseline({"mini": 0.0}), deterministic=True)
        self.assertIn("scenario_error:mini", verdict.reasons)

    def test_an_input_problem_propagates(self):
        nobody = mini(crew=[member(101, "Visiting Trader", "common")])
        with self.assertRaisesRegex(bench.WorldError, "headroom"):
            bench.run_suite([bench.validate_scenario(nobody)])

    def test_determinism_and_nondeterminism_detection(self):
        scenarios = [bench.validate_scenario(mini())]
        deterministic, suite = bench.check_determinism(scenarios, seeds=1)
        self.assertTrue(deterministic)
        self.assertEqual(len(suite.scenarios), 1)
        state = {"ticks": 0}
        contract = bench.make_contract_executor()

        def drifting(client, now):
            state["ticks"] += 1
            return None if state["ticks"] > 24 else contract(client, now)

        deterministic, suite = bench.check_determinism(scenarios, tick=drifting, seeds=1)
        self.assertFalse(deterministic)
        verdict = bench.gate(suite, synthetic_baseline({"mini": 0.0}), deterministic=False)
        self.assertEqual(verdict.reasons, ("nondeterministic",))


def write_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


class ScenarioWorldTests(unittest.TestCase):
    def test_world_facts(self):
        crew = [captain(stats={"Attack": 150}), member(2, "Delish", "epic", room=7002, stats={"Weapon": 10}),
                member(3, "Visiting Trader", "common"), member(4, "Delish", "epic", room=0),
                member(5, "Galactic Sprite", "hero", fatigue=4, training={"design": "Bench Press",
                                                                          "ends_after_minutes": 30})]
        _, world = world_for(mini(rooms=ROOMS, research=["Fitness", "Education Lv2"], crew=crew))
        meta = world.crew
        self.assertEqual((meta[101].cap, meta[101].target, meta[101].headroom, meta[101].eligible), (200, 179, 29, True))
        self.assertEqual((meta[2].cap, meta[2].target, meta[2].headroom, meta[2].room_type), (80, 72, 62, "academy"))
        self.assertEqual((meta[3].role, meta[3].eligible, meta[3].headroom, meta[3].target), (None, False, 0, 54))
        self.assertEqual((meta[4].room_type, meta[4].eligible, meta[4].headroom), (None, False, 0))
        self.assertEqual((meta[5].loaded_until, meta[5].loaded_can_gain, meta[5].target), (hours(0.5), True, 86))
        self.assertEqual(dict(world.rooms), {7001: "GYM", 7002: "Academy", 7005: "Bridge"})
        self.assertEqual(world.research, frozenset({2010, 2021}))
        self.assertEqual(dict(world.levels), {"Fitness": 1, "Education": 2})
        self.assertEqual((world.start, world.horizon_end), (T0, hours(24)))
        records = world.sim_scenario["accounts"]["bench"]["crew"]
        self.assertEqual((records[4]["@TrainingDesignId"], records[4]["@TrainingEndDate"]), ("3111", stamp(0.5)))

    def test_simulator_records_are_exact(self):
        _, world = world_for(mini(rooms=ROOMS, inventory={"Hero HP Consumable": 2, "Common ATK Consumable": 1}))
        account = world.sim_scenario["accounts"]["bench"]
        self.assertEqual(account["rooms"], [
            {"@RoomId": "7001", "@RoomDesignId": "1000", "@RoomStatus": "Normal"},
            {"@RoomId": "7002", "@RoomDesignId": "1100", "@RoomStatus": "Normal"},
            {"@RoomId": "7005", "@RoomDesignId": "1200", "@RoomStatus": "Normal"},
        ])
        self.assertEqual(account["inventory"], [{"@ItemId": "6001", "@ItemDesignId": "5001", "@Quantity": "1"},
                                                {"@ItemId": "6016", "@ItemDesignId": "5016", "@Quantity": "2"}])
        self.assertEqual(account["research"], [{"@ResearchDesignId": "2010", "@ResearchState": "Completed"}])
        training = world.sim_scenario["training"]
        self.assertEqual(training["character_designs"][0], {"@CharacterDesignId": "4001", "@CharacterDesignName":
                                                            "Common", "@Rarity": "Common", "@TrainingCapacity": "60"})
        self.assertEqual(training["item_designs"][0], {
            "@ItemDesignId": "5001", "@ItemName": "Common ATK Consumable", "@ItemSubType": "Consumable",
            "@Rarity": "Common", "@ConsumableStat": "Attack", "@ConsumableGain": "2", "@ConsumableFatigue": "0",
            "@FatigueRecovery": "0"})
        self.assertEqual(world.research_designs[1], {"@ResearchDesignId": "2011", "@ResearchName": "Fitness Lv2",
                                                     "@RootResearchDesignId": "2010"})
        self.assertEqual((training["fatigue_max"], training["fatigue_recovery_seconds"]), (100, 360))

    def test_world_consumable_mapping_self_check(self):
        _, world = world_for(mini(inventory={"Common ATK Consumable": 2, "Hero HP Consumable": 1}))
        item_designs = world.sim_scenario["training"]["item_designs"]
        self.assertEqual(dict(parse_consumable_designs({"ItemDesign": item_designs})), bench.CATALOG_CONSUMABLES)
        renamed = dict(bench.CATALOG_ITEMS)
        renamed["Common Gizmo"] = renamed.pop("Common ATK Consumable")
        with mock.patch.object(bench, "CATALOG_ITEMS", renamed):
            with self.assertRaisesRegex(bench.WorldError, "consumable"):
                bench.build_world(bench.validate_scenario(mini()))

    def test_build_world_requires_reachable_headroom(self):
        for crew in ([captain(stats={"Attack": 179})], [member(101, "Visiting Trader", "common")],
                     [member(101, "Delish", "epic")]):  # Delish in a gym must move to the academy first
            with self.subTest(crew=crew[0]["name"]):
                with self.assertRaisesRegex(bench.WorldError, "policy-reachable headroom"):
                    bench.build_world(bench.validate_scenario(mini(crew=crew)))
        self.assertEqual(world_for(mini(crew=[captain(stats={"Attack": 178})]))[1].crew[101].headroom, 1)

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
            "duplicate room id": (mini(rooms=[{"id": 7001, "design": "GYM"}, {"id": 7001, "design": "Bridge"}]),
                                  "rooms"),
            "room id 0": (mini(rooms=[{"id": 0, "design": "GYM"}]), "rooms[0].id"),
            "room extra key": (mini(rooms=[{"id": 7001, "design": "GYM", "x": 1}]), "rooms[0]"),
            "no rooms": (mini(rooms=[]), "rooms"),
            "crew room undeclared": (mini(crew=[captain(room=7999)]), "room"),
            "unknown research": (mini(research=["Fitness Lv9"]), "research"),
            "unknown item": (mini(inventory={"Mystery Box": 1}), "inventory"),
            "negative quantity": (mini(inventory={"Common ATK Consumable": -1}), "inventory"),
            "inventory list": (mini(inventory=[]), "inventory"),
            "bad fault kind": (mini(faults=[{"kind": "concurrent-spend", "method": "addTraining"}]), "kind"),
            "bad fault method": (mini(faults=[{"kind": "reject", "method": "collectAllResources"}]), "method"),
            "effect on reject": (mini(faults=[{"kind": "reject", "method": "addTraining", "effect": False}]), "effect"),
            "unknown fault key": (mini(faults=[{"kind": "reject", "method": "addTraining", "when": 1}]), "when"),
            "fault not an object": (mini(faults=["reject"]), "faults[0]"),
            "faults not a list": (mini(faults={}), "faults"),
            "simulator rejects fault": (mini(faults=[{"kind": "reject", "method": "addTraining", "probability": 2}]),
                                        "faults[0]"),
            "design on reject": (mini(faults=[{"kind": "reject", "method": "addTraining", "training_design": "BBJ"}]),
                                 "training_design"),
            "unknown training design": (mini(faults=[{"kind": "contradicted-effect", "method": "addTraining",
                                                      "training_design": "Nap"}]), "training_design"),
            "contradicted without design": (mini(faults=[{"kind": "contradicted-effect", "method": "addTraining"}]),
                                            "training_design"),
            "stats over capacity": (mini(crew=[captain(design="common", stats={"Attack": 61})]), "stats"),
            "unknown stat": (mini(crew=[captain(stats={"Luck": 1})]), "stats"),
            "negative stat": (mini(crew=[captain(stats={"Attack": -1})]), "stats"),
            "stats list": (mini(crew=[captain(stats=[])]), "stats"),
            "duplicate crew id": (mini(crew=[captain(), captain(name="Huntress")]), "id"),
            "crew id 0": (mini(crew=[captain(id=0)]), "crew[0].id"),
            "blank crew name": (mini(crew=[captain(name=" ")]), "crew[0].name"),
            "unknown crew design": (mini(crew=[captain(design="mythic")]), "design"),
            "fatigue 101": (mini(crew=[captain(fatigue=101)]), "fatigue"),
            "level 0": (mini(crew=[captain(level=0)]), "level"),
            "unknown crew key": (mini(crew=[captain(mood="calm")]), "mood"),
            "crew missing room": (mini(crew=[{"id": 1, "name": "Huntress", "design": "unique"}]), "missing room"),
            "crew not an object": (mini(crew=["Huntress"]), "crew[0]"),
            "unknown session design": (mini(crew=[captain(training=session)]), "training"),
            "session ends now": (mini(crew=[captain(training={"design": "BBJ", "ends_after_minutes": 0})]),
                                 "training"),
            "empty crew": (mini(crew=[]), "crew"),
            "naive start": (mini(start="2027-07-05T00:00:00"), "start"),
            "bad start": (mini(start="soon"), "start"),
            "start number": (mini(start=5), "start"),
            "schema 2": (mini(schema_version=2), "schema_version"),
            "not an object": ([], "JSON object"),
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
        scenario = bench.validate_scenario(mini(crew=[captain(stats={"Attack": 5}, fatigue=3, level=4,
                                                              training=loaded)], fatigue_recovery_seconds=90))
        self.assertEqual((scenario.tick_minutes, scenario.fatigue_recovery_seconds, scenario.start), (60, 90, T0))
        self.assertEqual(scenario.crew[0], bench.CrewSpec(101, "Admiral Serena", "captain", 7001, 3, 4,
                                                          {"Attack": 5}, ("Bench Press", 30)))
        defaults = bench.validate_scenario(without("tick_minutes"))
        self.assertEqual((defaults.tick_minutes, defaults.fatigue_recovery_seconds), (60, 360))
        self.assertEqual(defaults.crew[0], bench.CrewSpec(101, "Admiral Serena", "captain", 7001))
        self.assertEqual(bench.validate_scenario(mini(tick_minutes=30)).tick_minutes, 30)
        self.assertEqual(bench.validate_scenario(mini(crew=[captain(room=0)], rooms=ROOMS)).crew[0].room, 0)

    def test_validate_scenario_reports_unhashable_values(self):
        cases = {
            "room design list": (mini(rooms=[{"id": 7001, "design": ["GYM"]}]), "rooms[0].design"),
            "crew room list": (mini(crew=[captain(room=[7001])]), "crew[0].room"),
            "crew design list": (mini(crew=[captain(design=["captain"])]), "crew[0].design"),
            "session design object": (mini(crew=[captain(training={"design": {}, "ends_after_minutes": 5})]),
                                      "crew[0].training"),
            "fault design list": (mini(faults=[{"kind": "contradicted-effect", "method": "addTraining",
                                                "training_design": []}]), "training_design"),
            "fault kind list": (mini(faults=[{"kind": ["reject"], "method": "addTraining"}]), "kind"),
            "research nested list": (mini(research=[["Fitness"]]), "research"),
        }
        for name, (data, needle) in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(bench.ScenarioError) as raised:
                    bench.validate_scenario(data)
                self.assertIn(needle, str(raised.exception))

    def test_validate_scenario_reports_exactly_each_problem(self):
        research = "research: must be unique names among " + ", ".join(bench.CATALOG_RESEARCH)
        cases = {
            "unknown fault key": (mini(faults=[{"kind": "reject", "method": "addTraining", "when": 1}]),
                                  ["faults[0]: unknown keys when"]),
            "effect on reject": (mini(faults=[{"kind": "reject", "method": "addTraining", "effect": True}]),
                                 ["faults[0].effect: only raise and ambiguous-response faults take an effect"]),
            "design on reject": (mini(faults=[{"kind": "reject", "method": "addTraining", "training_design": "BBJ"}]),
                                 ["faults[0].training_design: only contradicted-effect faults take a training "
                                  "design"]),
            "faults text": (mini(faults="ab"), ["faults: must be a list"]),
            "crew text": (mini(crew="ab"), ["crew: must be a non-empty list"]),
            "rooms text": (mini(rooms="ab", crew=[captain(room=0)]), ["rooms: must be a non-empty list"]),
            "unknown stat text points": (mini(crew=[captain(stats={"Luck": "a"})]), ["crew[0].stats: unknown stats Luck"]),
            "stat text points": (mini(crew=[captain(stats={"Attack": "a"})]),
                                 ["crew[0].stats: points must be non-negative integers"]),
            "session extra key": (mini(crew=[captain(training={"design": "BBJ", "ends_after_minutes": 5, "x": 1})]),
                                  ["crew[0].training: needs a catalog training design and ends_after_minutes >= 1"]),
            "session list": (mini(crew=[captain(training=["BBJ", 5])]),
                             ["crew[0].training: needs a catalog training design and ends_after_minutes >= 1"]),
            "research text": (mini(research="Fitness"), [research]),
            "research duplicate": (mini(research=["Fitness", "Fitness"]), [research]),
            "inventory text": (mini(inventory="x"), ["inventory: must map catalog item names to non-negative "
                                                     "quantities"]),
            "crew unknown and missing": (mini(crew=[{"id": 1, "mood": 2}]),
                                         ["crew[0]: unknown keys mood", "crew[0]: missing name, design, room"]),
            "tags text": (mini(tags="abc"), ["tags: must be a list of unique strings"]),
            "tags numbers": (mini(tags=[1]), ["tags: must be a list of unique strings"]),
            "name number": (mini(name=5), ["name: 5 must match ^[a-z0-9-]+$"]),
            "schema text": (mini(schema_version="1"), ["schema_version: must be 1, not '1'"]),
            "schema bool": (mini(schema_version=True), ["schema_version: must be 1, not True"]),
            "recovery bool": (mini(fatigue_recovery_seconds=True), ["fatigue_recovery_seconds: must be a positive "
                                                                    "integer, not True"]),
            "tick 23": (mini(tick_minutes=25), ["tick_minutes: must be an integer 5..240 dividing 1440, not 25"]),
            "unknown top key": (mini(zzz=1, aaa=2), ["unknown keys: aaa, zzz"]),
        }
        for name, (data, expected) in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(bench.ScenarioError) as raised:
                    bench.validate_scenario(data, source="case.json")
                self.assertEqual(raised.exception.problems, expected)
                self.assertEqual(raised.exception.source, "case.json")
        with self.assertRaises(bench.ScenarioError) as raised:
            bench.validate_scenario([])
        self.assertEqual((raised.exception.source, str(raised.exception)), ("<scenario>",
                                                                           "<scenario>: scenario: must be a JSON object"))
        self.assertEqual(bench.validate_scenario(mini(tags=[])).tags, ())
        self.assertEqual(bench.validate_scenario(mini(start="2027-07-05T02:00:00+02:00")).start, T0)

    def test_fault_day_beyond_the_scenario_is_rejected(self):
        late = {"kind": "reject", "method": "addTraining", "day": 1, "call": 1}
        with self.assertRaises(bench.ScenarioError) as raised:
            bench.validate_scenario(mini(days=1, faults=[late]))
        self.assertIn("faults[0].day: 1 is past the last day (0)", str(raised.exception))
        self.assertEqual(bench.validate_scenario(mini(days=2, faults=[late])).faults, (late,))
        with self.assertRaises(bench.ScenarioError) as raised:
            bench.validate_scenario(mini(days=0, faults=[late]))
        self.assertEqual(len(raised.exception.problems), 1)  # only days: no fault-day noise from bad days

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
            with self.assertRaises(bench.ScenarioError) as raised:
                bench.load_suite(directory, names=["mini"], strict=True)
            self.assertNotIn("missing required tags", str(raised.exception))
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


class CheckedInSuiteTests(unittest.TestCase):
    def test_checked_in_suite_rules(self):
        suite = checked_in_suite()
        self.assertEqual(len(suite), 15)
        names = [scenario.name for scenario in suite]
        stems = sorted(name[:-5] for name in os.listdir(bench.SCENARIO_DIR) if name.endswith(".json"))
        self.assertEqual(names, stems)
        self.assertTrue(all(scenario.days >= bench.MIN_SUITE_DAYS for scenario in suite))
        self.assertLessEqual(bench.REQUIRED_TAGS, {tag for scenario in suite for tag in scenario.tags})
        kinds = {fault["kind"] for scenario in suite for fault in scenario.faults}
        self.assertEqual(kinds, set(bench.ALLOWED_FAULTS))
        roles = {name for role in DEFAULT_ROLES for name in role.characters}
        unknown = {crew.name for scenario in suite for crew in scenario.crew} - roles
        self.assertEqual(unknown, {"Visiting Trader"})  # the one intentionally role-less crew
        loaded = [crew.id for scenario in suite for crew in scenario.crew if crew.training is not None]
        self.assertEqual(loaded, [801, 802, 803, 804])

    def test_every_tag_shows_its_evidence(self):
        for scenario, result in zip(checked_in_suite(), default_run().scenarios):
            with self.subTest(scenario=scenario.name):
                self.assertEqual(bench.tag_evidence(scenario, result.runs[0].metrics), [])
        used = {tag for scenario in checked_in_suite() for tag in scenario.tags}
        self.assertEqual(used, set(bench.TAG_EVIDENCE) | set(bench.FAULT_TAG_COUNTERS))

    def test_tag_evidence_reports_each_unmet_claim(self):
        scenario = bench.validate_scenario(mini(tags=["fatigued", "fault-reject", "mystery"], days=2, faults=[
            {"kind": "reject", "method": "addTraining", "call": 1},
            {"kind": "reject", "method": "finishTraining", "day": 1, "call": 1}]))
        metrics = bench.Metrics(skip_reasons={"fatigued": 30}, zero_gain_sessions=0, fault_rejected=1,
                                consumables_by_tier={"Hero": 2})
        self.assertEqual(bench.tag_evidence(scenario, metrics), [
            "fatigued: zero_gain_sessions is 0 < 1",
            "fault-reject: fault_rejected is 1, not 2",
            "mystery: no evidence rule",
        ])
        self.assertEqual(bench.metric_value(metrics, "skip_reasons.fatigued"), 30)
        self.assertEqual(bench.metric_value(metrics, "skip_reasons.complete"), 0)
        self.assertEqual(bench.metric_value(metrics, "fault_rejected"), 1)

    def test_the_unmodified_planner_trips_no_oracle(self):
        for result in default_run().scenarios:
            with self.subTest(scenario=result.name):
                self.assertEqual(result.violations(bench.VIOLATION_KINDS), dict.fromkeys(bench.VIOLATION_KINDS, 0))
                self.assertIsNone(result.error)

    def test_every_checked_in_fault_fires(self):
        # A fault scheduled after the crew stop calling never fires and silently drops its coverage.
        for scenario in checked_in_suite():
            if not scenario.faults:
                continue
            with self.subTest(scenario=scenario.name):
                episode = bench.run_episode(scenario)
                fired_kinds = sorted(entry.rsplit(": ", 1)[1] for entry in episode.fired)
                self.assertEqual(fired_kinds, sorted(fault["kind"] for fault in scenario.faults))


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def synthetic_result(name: str, score: float, digest: str = DIGEST_A, error: str | None = None, **metric_changes):
    """A one-seed ScenarioResult whose trace digest is ``digest`` (the combined digest is patched in)."""
    metrics = None if error else bench.Metrics(**metric_changes)
    result = bench.ScenarioResult(name, (bench.SeedRun(1, metrics, score, "", error),))
    return SyntheticResult(result, digest)


class SyntheticResult(bench.ScenarioResult):
    def __init__(self, result, digest):
        object.__setattr__(self, "name", result.name)
        object.__setattr__(self, "runs", result.runs)
        object.__setattr__(self, "_digest", digest)

    @property
    def trace_digest(self):
        return self._digest


def synthetic_suite(scores: dict, digests: dict | None = None, **metric_changes):
    return bench.SuiteResult(tuple(synthetic_result(name, score, (digests or {}).get(name, DIGEST_A),
                                                    **metric_changes) for name, score in scores.items()))


def synthetic_baseline(scores: dict, **changes) -> dict:
    data = {
        "schema_version": 2,
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
        lower = bench.gate(synthetic_suite({"a": 50.0, "b": 70.0}), dict(baseline, composite=60.000001),
                           deterministic=True)
        self.assertEqual(lower.reasons, ("composite_below_baseline",))
        shifted = bench.gate(synthetic_suite({"a": 49.999999, "b": 71.0}), baseline, deterministic=True)
        self.assertEqual(shifted.reasons, ("scenario_below_baseline:a",))  # exact: no tolerance
        mismatch = bench.gate(synthetic_suite({"a": 50.0, "b": 70.0, "c": 99.0}), baseline, deterministic=True)
        self.assertEqual(mismatch.reasons, ("baseline_mismatch",))
        subset = bench.gate(synthetic_suite({"b": 70.0}), baseline, deterministic=True, subset=True)
        self.assertEqual(subset, bench.GateResult(True, (), ()))
        subset_lower = bench.gate(synthetic_suite({"b": 69.0}), baseline, deterministic=True, subset=True)
        self.assertEqual(subset_lower.reasons, ("scenario_below_baseline:b",))
        unknown = bench.gate(synthetic_suite({"c": 99.0}), baseline, deterministic=True, subset=True)
        self.assertEqual(unknown.reasons, ("baseline_mismatch",))
        drifted = bench.gate(synthetic_suite({"a": 50.0, "b": 70.0}, {"b": DIGEST_B}), baseline, deterministic=True)
        self.assertEqual(drifted, bench.GateResult(True, (), ("b",)))
        missing = bench.gate(synthetic_suite({"a": 50.0}), None, deterministic=False)
        self.assertEqual(missing, bench.GateResult(False, ("nondeterministic", "baseline_missing"), ()))
        self.assertTrue(bench.gate(synthetic_suite({"a": 60.0, "b": 70.0}), baseline, deterministic=True).passed)
        for kind in bench.GATED_VIOLATIONS:
            with self.subTest(kind=kind):
                violating = synthetic_suite({"a": 50.0, "b": 70.0}, violations={kind: 2})
                reasons = bench.gate(violating, baseline, deterministic=True).reasons
                self.assertEqual(reasons, (f"{kind.replace('-', '_')}_violation:a",
                                           f"{kind.replace('-', '_')}_violation:b"))
        errored = bench.SuiteResult((synthetic_result("a", 0.0, error="Boom"), synthetic_result("b", 70.0)))
        verdict = bench.gate(errored, baseline, deterministic=True)
        self.assertEqual(verdict.reasons, ("composite_below_baseline", "scenario_error:a",
                                           "scenario_below_baseline:a"))
        self.assertEqual(verdict.drift, ())

    def test_update_baseline_only_goes_up(self):
        scores = {"a": 50.0, "b": 70.0}
        new = bench.update_baseline(synthetic_suite(scores), None, deterministic=True, allow_lower=None)
        self.assertEqual(new, synthetic_baseline(scores))
        bench.validate_baseline(new)
        old = synthetic_baseline(scores, qa_eval={"min_detection_rate": 0.95, "equivalent": [
            {"id": "sdk/training/planner.py:1:2:constant:0", "original": "1", "replacement": "2", "reason": "dead"}]},
            lowered=[{"from": 70.0, "to": 60.0, "reason": "earlier"}])
        higher = bench.update_baseline(synthetic_suite({"a": 55.0, "b": 70.0}), old, deterministic=True,
                                       allow_lower=None)
        self.assertEqual((higher["composite"], higher["scenarios"]["a"]["score"]), (62.5, 55.0))
        self.assertEqual((higher["qa_eval"], higher["lowered"]), (old["qa_eval"], old["lowered"]))
        refreshed = bench.update_baseline(synthetic_suite(scores, {"a": DIGEST_B}), old, deterministic=True,
                                          allow_lower=None)
        self.assertEqual(refreshed["scenarios"]["a"]["trace_digest"], DIGEST_B)
        added = bench.update_baseline(synthetic_suite({"a": 50.0, "b": 70.0, "c": 20.0}), old, deterministic=True,
                                      allow_lower=None)
        self.assertEqual((sorted(added["scenarios"]), added["composite"], added["lowered"]),
                         (["a", "b", "c"], 46.666667, old["lowered"]))
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
        with self.assertRaisesRegex(bench.BaselineRefused, r"would lower the baseline \(composite 60.0 -> 59.5; "
                                                           r"a 50.0 -> 49.0\)"):
            bench.update_baseline(synthetic_suite({"a": 49.0, "b": 70.0}), old, deterministic=True, allow_lower=None)
        with self.assertRaisesRegex(bench.BaselineRefused, r"\(a removed\)"):
            bench.update_baseline(synthetic_suite({"b": 70.0, "c": 99.0}), old, deterministic=True, allow_lower=None)
        lowered = bench.update_baseline(synthetic_suite({"a": 49.0, "b": 70.0}), old, deterministic=True,
                                        allow_lower=" new scenario weights ")
        self.assertEqual(lowered["lowered"], [{"from": 70.0, "to": 60.0, "reason": "earlier"},
                                              {"from": 60.0, "to": 59.5, "reason": "new scenario weights"}])
        self.assertEqual(lowered["composite"], 59.5)
        refusals = {
            "nondeterministic": (synthetic_suite(scores), {"deterministic": False}),
            "hard violation in a, b": (synthetic_suite(scores, violations={"cap": 1}), {}),
            "scenario error in a": (bench.SuiteResult((synthetic_result("a", 0.0, error="Boom"),)), {}),
        }
        for name, (suite, options) in refusals.items():
            with self.subTest(refusal=name):
                with self.assertRaisesRegex(bench.BaselineRefused, name):
                    bench.update_baseline(suite, old, deterministic=options.get("deterministic", True),
                                          allow_lower="anyway")

    def test_validate_baseline_schema(self):
        good = synthetic_baseline({"a": 50.0})
        self.assertEqual(bench.validate_baseline(good), good)
        equivalent = {"id": "sdk/training/planner.py:1:2:constant:0", "original": "1", "replacement": "2",
                      "reason": "x"}
        self.assertEqual(bench.validate_baseline(dict(good, qa_eval={"min_detection_rate": 1, "equivalent": [
            equivalent]})), dict(good, qa_eval={"min_detection_rate": 1, "equivalent": [equivalent]}))
        bad_cases = {
            "unknown key": dict(good, extra=1),
            "score 101": dict(good, scenarios={"a": {"score": 101, "trace_digest": DIGEST_A}}),
            "score -1": dict(good, scenarios={"a": {"score": -1, "trace_digest": DIGEST_A}}),
            "bool score": dict(good, scenarios={"a": {"score": True, "trace_digest": DIGEST_A}}),
            "bad digest": dict(good, scenarios={"a": {"score": 5, "trace_digest": "md5:abc"}}),
            "scenario key": dict(good, scenarios={"a": {"score": 5, "trace_digest": DIGEST_A, "seed": 1}}),
            "scenarios list": dict(good, scenarios=[]),
            "missing composite": {key: value for key, value in good.items() if key != "composite"},
            "composite -1": dict(good, composite=-1),
            "composite 101": dict(good, composite=101),
            "schema 1": dict(good, schema_version=1),
            "schema true": dict(good, schema_version=True),
            "benchmark name": dict(good, benchmark="programbench"),
            "lowered not a list": dict(good, lowered={}),
            "blank lowered reason": dict(good, lowered=[{"from": 2.0, "to": 1.0, "reason": " "}]),
            "lowered text": dict(good, lowered=[{"from": "2", "to": 1.0, "reason": "r"}]),
            "lowered key": dict(good, lowered=[{"from": 2.0, "to": 1.0, "reason": "r", "x": 1}]),
            "rate 0": dict(good, qa_eval={"min_detection_rate": 0, "equivalent": []}),
            "rate 1.5": dict(good, qa_eval={"min_detection_rate": 1.5, "equivalent": []}),
            "qa key": dict(good, qa_eval={"min_detection_rate": 0.9}),
            "equivalent not a list": dict(good, qa_eval={"min_detection_rate": 0.9, "equivalent": {}}),
            "blank equivalent reason": dict(good, qa_eval={"min_detection_rate": 0.9, "equivalent": [
                dict(equivalent, reason="")]}),
            "bad equivalent id": dict(good, qa_eval={"min_detection_rate": 0.9, "equivalent": [
                dict(equivalent, id="planner")]}),
            "equivalent text types": dict(good, qa_eval={"min_detection_rate": 0.9, "equivalent": [
                dict(equivalent, original=1)]}),
            "equivalent keys": dict(good, qa_eval={"min_detection_rate": 0.9, "equivalent": [{"id": "x"}]}),
            "not an object": [],
        }
        for name, data in bad_cases.items():
            with self.subTest(case=name):
                with self.assertRaises(bench.BaselineError):
                    bench.validate_baseline(data)
        with self.assertRaises(bench.BaselineError) as raised:
            bench.validate_baseline(dict(good, schema_version=1, composite=-1))
        self.assertEqual(str(raised.exception), "schema_version: must be 2; composite: must be a number in [0, 100]")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "baseline.json")
            self.assertIsNone(bench.load_baseline(path))
            write_json(path, good)
            self.assertEqual(bench.load_baseline(path), good)
            write_json(path, dict(good, schema_version=1))
            with self.assertRaisesRegex(bench.BaselineError, "baseline.json: schema_version"):
                bench.load_baseline(path)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{")
            with self.assertRaisesRegex(bench.BaselineError, "baseline.json: cannot read"):
                bench.load_baseline(path)

    def test_report_json_and_markdown(self):
        suite = bench.run_suite([bench.validate_scenario(mini())], seeds=2)
        report = bench.report_dict(suite)
        text = bench.to_json(report)
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(json.loads(text), report)
        self.assertEqual((report["schema_version"], report["formula"]), (2, bench.SCORE_FORMULA))
        entry = report["scenarios"]["mini"]
        self.assertEqual([run["seed"] for run in entry["seeds"]], [7, 8])
        self.assertEqual(entry["spread"], 0.261871)
        self.assertEqual(entry["seeds"][0]["metrics"]["tp_gained"], 41)
        verdict = bench.GateResult(False, ("scenario_below_baseline:mini",), ("mini",))
        markdown = bench.render_markdown(report, verdict)
        self.assertIn("Composite: **34.26** over 1 scenarios x 3 seeds", markdown)
        self.assertIn("Gate: **FAIL** (scenario_below_baseline:mini)", markdown)
        self.assertIn("| mini | 34.26 | 0.26 | 34.39 / 34.13 | 0.123 | 1.000 | 1.000 | 88 | 0 | 0 | 0 | 0 |", markdown)
        self.assertIn("| mini | none | 0 | 0 |", markdown)
        self.assertIn("trace drift (behaviour changed, scores held; never a failure)", markdown)
        self.assertIn(": mini", markdown)
        passing = bench.render_markdown(report, bench.GateResult(True, (), ()))
        self.assertIn("Gate: **PASS**", passing)
        self.assertNotIn("trace drift", passing)
        self.assertIn("Gate: not run", bench.render_markdown(report, None))
        errored = bench.report_dict(bench.SuiteResult((synthetic_result("a", 0.0, error="Boom"),)))
        self.assertIn("| a | 0.00 | error: seed 1: Boom |", bench.render_markdown(errored, None))
        windowless = {"composite": 1.0, "formula": "f", "scenarios": {"w": {"score": 1.0, "spread": 0.0, "error": None,
                      "seeds": [{"score": 1.0, "metrics": bench.Metrics(skip_reasons={"complete": 2},
                                                                        violations={"cap": 1}).to_dict()}]}}}
        self.assertIn("| w | 1.00 | 0.00 | 1.00 | 0.000 | n/a | 1.000 | 0 | 0 | 0 | 1 | 0 |",
                      bench.render_markdown(windowless, None))
        self.assertIn("| w | complete 2 | 0 | 0 |", bench.render_markdown(windowless, None))


class MainTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.scenarios = os.path.join(self.directory.name, "scenarios")
        os.mkdir(self.scenarios)
        write_json(os.path.join(self.scenarios, "mini.json"), mini())
        self.baseline = os.path.join(self.directory.name, "baseline.json")

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bench.main(["--scenarios-dir", self.scenarios, "--baseline", self.baseline, *argv])
        self.stderr = err.getvalue()
        return code, out.getvalue()

    def test_main_exit_codes(self):
        self.assertEqual(self.main()[0], bench.EXIT_ERROR)  # gate mode, no baseline
        code, out = self.main("--update-baseline", "--quiet")
        self.assertEqual((code, out), (bench.EXIT_OK, ""))
        with open(self.baseline, encoding="utf-8") as handle:
            written = json.load(handle)
        self.assertEqual((sorted(written["scenarios"]), written["schema_version"]), (["mini"], 2))
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
            self.assertTrue(handle.read().startswith("before\n# Crew-training benchmark"))
        code, out = self.main("--json", "-")
        self.assertEqual((code, json.loads(out)["gate"]["passed"]), (bench.EXIT_OK, True))
        write_json(self.baseline, dict(written, composite=written["composite"] + 1))
        code, out = self.main("--json", "-")
        self.assertEqual(code, bench.EXIT_GATE)
        self.assertEqual(json.loads(out)["gate"]["reasons"], ["composite_below_baseline"])
        self.assertEqual(self.main("--update-baseline", "--quiet")[0], bench.EXIT_GATE)  # would lower it
        self.assertIn("training_benchmark: refused: would lower the baseline", self.stderr)
        self.assertEqual(self.main("--update-baseline", "--allow-lower", "reset", "--quiet")[0], bench.EXIT_OK)
        with open(self.baseline, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["lowered"][0]["reason"], "reset")
        self.assertEqual(self.main("--allow-lower", "x")[0], bench.EXIT_ERROR)
        self.assertEqual(self.stderr, "training_benchmark: --allow-lower needs --update-baseline\n")
        self.assertEqual(self.main("--update-baseline", "--quick")[0], bench.EXIT_ERROR)
        self.assertEqual(self.main("--scenario", "nope")[0], bench.EXIT_ERROR)
        self.assertIn("unknown scenario(s): nope", self.stderr)
        self.assertEqual(self.main("--scenario", "mini", "--quiet")[0], bench.EXIT_OK)
        write_json(os.path.join(self.scenarios, "broken.json"), mini(name="broken", days=0))
        self.assertEqual(self.main()[0], bench.EXIT_ERROR)

    def test_quick_and_seed_runs_are_report_only(self):
        code, out = self.main("--quick", "--json", "-")
        report = json.loads(out)
        self.assertEqual(code, bench.EXIT_OK)
        self.assertNotIn("gate", report)
        self.assertEqual(report["scenarios"]["mini"]["seeds"][0]["metrics"]["eligible_crew_hours"], 48.0)
        code, out = self.main("--seed", "3", "--json", "-")
        self.assertEqual(code, bench.EXIT_OK)
        report = json.loads(out)
        self.assertNotIn("gate", report)
        self.assertEqual([run["seed"] for run in report["scenarios"]["mini"]["seeds"]], [3, 4, 5])
        code, out = self.main("--quick")
        self.assertIn("Gate: not run (report only)", out)

    def test_markdown_file_output(self):
        self.main("--update-baseline", "--quiet")
        path = os.path.join(self.directory.name, "report.md")
        self.assertEqual(self.main("--markdown", path, "--quiet")[0], bench.EXIT_OK)
        with open(path, encoding="utf-8") as handle:
            self.assertIn("| mini |", handle.read())

    def test_usage_rejects_flags_that_would_be_ignored(self):
        parser = bench.build_parser()
        for argv in (["--gate", "--quick"], ["--gate", "--seed", "5"], ["--gate", "--update-baseline"],
                     ["--qa-eval", "--scenario", "mini"], ["--qa-eval", "--quick"], ["--qa-eval", "--seed", "1"],
                     ["--qa-eval", "--update-baseline"], ["--qa-eval", "--gate"], ["--update-baseline", "--seed", "1"],
                     ["--update-baseline", "--scenario", "x"]):
            with self.subTest(argv=argv):
                self.assertIsNotNone(bench._usage_error(parser.parse_args(argv)))
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(bench.main(argv), bench.EXIT_ERROR)
        for argv in ([], ["--gate"], ["--quick"], ["--seed", "3"], ["--gate", "--scenario", "x"], ["--qa-eval"],
                     ["--update-baseline", "--allow-lower", "r"]):
            with self.subTest(ok=argv):
                self.assertIsNone(bench._usage_error(parser.parse_args(argv)))
        defaults = parser.parse_args([])
        self.assertEqual((defaults.timeout, defaults.channels, defaults.baseline, defaults.scenarios_dir),
                         (300.0, "tests,benchmark", bench.BASELINE_PATH, bench.SCENARIO_DIR))


class CheckedInBaselineTests(unittest.TestCase):
    def test_checked_in_baseline_is_valid(self):
        baseline = bench.load_baseline(bench.BASELINE_PATH)
        self.assertIsNotNone(baseline)
        self.assertEqual(baseline["schema_version"], 2)
        self.assertEqual(sorted(baseline["scenarios"]), [scenario.name for scenario in checked_in_suite()])
        self.assertEqual(baseline["qa_eval"]["min_detection_rate"], 0.9)
        scores = [entry["score"] for entry in baseline["scenarios"].values()]
        self.assertEqual(baseline["composite"], bench.composite(scores))


# -- qa-eval ------------------------------------------------------------------------------------------------------

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


def always_imports(workspace, timeout):
    return True


class QaEvalTests(unittest.TestCase):
    def test_qa_classification(self):
        detected = bench.BenchOutcome("detected", ("scenario_below_baseline:fresh-crew",), ())
        passing = bench.BenchOutcome("pass")
        drifting = bench.BenchOutcome("pass", (), ("long-run",))
        cases = [
            ("killed", bench.QaOutcome("failed", detected), "both"),
            ("killed", bench.QaOutcome("timeout", passing), "tests-only"),
            ("killed", bench.QaOutcome("passed", detected), "benchmark-only"),
            ("killed", bench.QaOutcome("passed", bench.BenchOutcome("crash")), "benchmark-only"),
            ("timeout", bench.QaOutcome("passed", bench.BenchOutcome("timeout")), "benchmark-only"),
            ("killed", bench.QaOutcome(invalid=True), "invalid-strategy"),
            ("survived", bench.QaOutcome("passed", drifting), "drift-only"),
            ("survived", bench.QaOutcome("passed", passing), "neither"),
            ("survived", bench.QaOutcome(), "neither"),
            ("equivalent", bench.QaOutcome(invalid=True), "equivalent"),
            ("incompetent", bench.QaOutcome(), "incompetent"),
        ]
        for status, outcome, expected in cases:
            with self.subTest(expected=expected, status=status):
                self.assertEqual(bench.classify_mutant(status, outcome), expected)

    def test_rates_lead_with_each_channel(self):
        classes = ["both", "tests-only", "benchmark-only", "drift-only", "neither", "neither", "equivalent",
                   "incompetent", "both", "tests-only", "invalid-strategy", "invalid-strategy"]
        rows = [
            {"id": f"sdk/x.py:{index}:0:constant:0", "original": "1", "replacement": "2", "class": klass,
             "reasons": ["r"] if klass == "benchmark-only" else [], "drift": ["d"] if klass == "drift-only" else []}
            for index, klass in enumerate(classes, start=1)
        ]
        summary = bench.summarize(rows, {"sdk/x.py:6:0:constant:0": "dead store"})
        self.assertEqual(summary["counts"], {"both": 2, "tests-only": 2, "benchmark-only": 1,
                                             "invalid-strategy": 2, "drift-only": 1, "neither": 2,
                                             "equivalent": 1, "incompetent": 1})
        self.assertEqual((summary["total"], summary["eligible"], summary["importable"]), (12, 10, 8))
        self.assertEqual((summary["benchmark_rate"], summary["tests_rate"]), (0.375, 0.5))
        self.assertEqual(summary["detection_rate"], 0.7)  # (5 caught + 2 invalid) / 10
        self.assertEqual(summary["detection_rate_importable"], 0.625)  # 5 / 8
        self.assertEqual(summary["observable_rate"], 0.8)
        self.assertEqual(summary["benchmark_only"], [{"id": "sdk/x.py:3:0:constant:0", "change": "1 -> 2",
                                                      "reasons": ["r"]}])
        self.assertEqual([entry["note"] for entry in summary["neither"]], ["unexplained - investigate", "dead store"])
        self.assertEqual(summary["drift_only"], [{"id": "sdk/x.py:4:0:constant:0", "change": "1 -> 2",
                                                  "drift": ["d"]}])
        self.assertEqual(summary["invalid_strategy"], ["sdk/x.py:11:0:constant:0", "sdk/x.py:12:0:constant:0"])
        empty = bench.summarize([], {})
        self.assertEqual((empty["detection_rate"], empty["eligible"], empty["benchmark_rate"]), (1.0, 0, 1.0))

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
                                            bench_runner(bench_status), always_imports)
                    result = runner(workspace, ("tests.a",), 60.0)
                    self.assertEqual((result.status, result.tests_run), (expected, 7))
                    self.assertEqual(seen, [("tests", ("tests.a",), 30.0), ("bench", workspace, 30.0)])
                    self.assertEqual(list(runner.outcomes), [digest])
                    self.assertEqual(runner.outcomes[digest].tests, tests_status)
                    self.assertEqual(runner.outcomes[digest].bench.status, bench_status)
                    self.assertIn(f"benchmark: {bench_status}", result.output)
            only_bench = bench.QaRunner("sdk/x.py", ("benchmark",), tests_runner("failed"), bench_runner("pass"),
                                        always_imports)
            seen.clear()
            result = only_bench(workspace, ("tests.a",), 60.0)
            self.assertEqual((result.status, result.tests_run), ("passed", 2))
            self.assertEqual(seen, [("bench", workspace, 60.0)])
            self.assertIsNone(only_bench.outcomes[digest].tests)
            probes = []
            invalid = bench.QaRunner("sdk/x.py", ("tests", "benchmark"), tests_runner("passed"), bench_runner("pass"),
                                     lambda ws, timeout: probes.append((ws, timeout)) and False)
            seen.clear()
            result = invalid(workspace, ("tests.a",), 60.0)
            self.assertEqual((result.status, result.tests_run, result.output), ("failed", 1, "import sdk.training failed"))
            self.assertEqual((seen, probes), ([], [(workspace, 60.0)]))  # no channel runs on an import crash
            self.assertEqual(invalid.outcomes[digest], bench.QaOutcome(invalid=True))

    def test_import_probe_runs_the_import_in_the_workspace(self):
        with tempfile.TemporaryDirectory() as workspace:
            package = os.path.join(workspace, "sdk", "training")
            os.makedirs(package)
            Path(workspace, "sdk", "__init__.py").write_text("", encoding="utf-8")
            init = Path(package, "__init__.py")
            init.write_text("VALUE = 1\n", encoding="utf-8")
            self.assertIs(bench.import_probe(workspace, 30.0), True)
            init.write_text("raise ValueError('tier bands must tile')\n", encoding="utf-8")
            self.assertIs(bench.import_probe(workspace, 30.0), False)
            self.assertTrue(os.path.isdir(os.path.join(workspace, mutation.CHILD_TMP)))

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
            (FakeProcess(2, json.dumps(report)), bench.BenchOutcome("crash", ("exit 2: stderr tail",), ())),
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
                    self.assertEqual((options["stdin"], options["stdout"]), (subprocess.DEVNULL, subprocess.PIPE))
                    self.assertTrue(options["start_new_session"])
                    self.assertEqual(process.timeouts, [12.5])
                    self.assertEqual(killed, [])
        hung = FakeProcess(hang=True)
        killed = []
        with tempfile.TemporaryDirectory() as workspace:
            runner = bench.BenchSubprocessRunner(popen=lambda command, **options: hung, kill=killed.append)
            self.assertEqual(runner(workspace, 3.0), bench.BenchOutcome("timeout"))
        self.assertEqual(killed, [hung])
        self.assertEqual(hung.timeouts[0], 3.0)

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

    def test_qa_runner_kill_all_reaches_both_runners(self):
        killed = []

        class Killable:
            def __init__(self, name):
                self.name = name

            def kill_all(self):
                killed.append(self.name)

        bench.QaRunner("sdk/x.py", ("tests",), Killable("tests"), Killable("bench"), always_imports).kill_all()
        self.assertEqual(killed, ["tests", "bench"])
        bench.QaRunner("sdk/x.py", ("tests",), lambda: None, object(), always_imports).kill_all()  # no kill_all: fine

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
                    code = bench.main(["--qa-eval", "--baseline", baseline_path, "--jobs", "2", "--json", "-",
                                       "--qa-target", "sdk/training/planner.py", "--timeout", "9"])
        self.assertEqual([call[0] for call in calls[:2]], list(bench.QA_TARGETS))
        target, tests, kwargs = calls[0]
        self.assertEqual(tests, bench.QA_TEST_MODULES)
        self.assertNotIn("tests.test_training_benchmark_policy", tests)
        self.assertEqual(kwargs["copy_paths"], bench.QA_COPY_PATHS)
        self.assertEqual(kwargs["equivalent"], [first.id])
        self.assertEqual(calls[1][2]["equivalent"], [])
        self.assertEqual((kwargs["jobs"], kwargs["timeout"], kwargs["only"]), (3, 40.0, None))
        self.assertIsInstance(kwargs["test_runner"], bench.QaRunner)
        self.assertIs(kwargs["test_runner"].probe, bench.import_probe)
        self.assertEqual(result["overall"]["counts"]["neither"], 6)
        self.assertEqual(result["overall"]["detection_rate"], 0.0)
        self.assertEqual(result["stale_equivalents"], [stale["id"]])
        self.assertEqual((result["passed"], result["min_detection_rate"], result["channels"]),
                         (False, 0.9, ["tests", "benchmark"]))
        self.assertEqual(code, bench.EXIT_GATE)
        self.assertEqual(json.loads(out.getvalue())["overall"]["eligible"], 3)
        self.assertEqual((calls[2][0], calls[2][2]["jobs"], calls[2][2]["timeout"]),
                         ("sdk/training/planner.py", 2, 9.0))
        markdown = bench.render_qa_markdown(result)
        self.assertIn("| sdk/training/planner.py | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 3 | 0.000 | 0.000 |", markdown)
        self.assertIn("Benchmark channel: **0.000**; tests channel: **0.000** (of 6 importable mutants", markdown)
        self.assertIn("Detection: **0.000** with invalid-strategy counted as detected (gated, minimum 0.9)",
                      markdown)
        self.assertIn("unexplained - investigate", markdown)
        self.assertIn(stale["id"], markdown)

    def test_qa_eval_only_and_bad_channels(self):
        calls = []

        def fake_run_mutants(target, tests, **kwargs):
            calls.append((target, kwargs["only"]))
            return mutation.Report(target, tuple(tests), (), 1.0, kwargs["timeout"])

        planner_id = "sdk/training/planner.py:374:13:compare:0"
        with mock.patch.object(mutation, "run_mutants", fake_run_mutants):
            bench.qa_eval(only=[planner_id], baseline_path=os.path.join(bench.ROOT, "missing.json"))
        self.assertEqual(calls, [("sdk/training/planner.py", [planner_id])])
        for channels in ("", "tests,bogus", " , "):
            with self.subTest(channels=channels):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(bench.main(["--qa-eval", "--channels", channels]), bench.EXIT_ERROR)
                self.assertIn("--channels must name tests and/or benchmark", err.getvalue())
        with mock.patch.object(bench, "qa_eval", side_effect=bench.BaselineError("bad baseline")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(bench.main(["--qa-eval", "--quiet"]), bench.EXIT_ERROR)
            self.assertEqual(err.getvalue(), "training_benchmark: qa-eval: bad baseline\n")
        passing = {"targets": {}, "overall": bench.summarize([], {}), "channels": ["benchmark"],
                   "min_detection_rate": 0.9, "stale_equivalents": [], "mutants": [], "passed": True}
        with mock.patch.object(bench, "qa_eval", return_value=passing) as fake:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(bench.main(["--qa-eval", "--channels", "benchmark", "--quiet"]), bench.EXIT_OK)
        self.assertEqual(fake.call_args.kwargs["channels"], ("benchmark",))
        self.assertIsNone(fake.call_args.kwargs["progress"])

    def test_qa_rows_classify_by_mutated_source(self):
        source = TOY_SOURCE
        generated = mutation.generate_mutants("sdk/x.py", source)
        runner = bench.QaRunner("sdk/x.py", ("tests",), None, None, always_imports)
        key = hashlib.sha256(mutation.apply_mutant(source, generated[0]).encode("utf-8")).hexdigest()
        runner.outcomes[key] = bench.QaOutcome("failed", bench.BenchOutcome("detected", ("why",), ("d",)))
        results = (mutation.MutantResult(generated[0], "killed", 0.0, ""),
                   mutation.MutantResult(generated[1], "equivalent", 0.0, ""),
                   mutation.MutantResult(generated[2], "survived", 0.0, ""))
        report = mutation.Report("sdk/x.py", ("tests.a",), results, 1.0, 10.0)
        rows = bench._qa_rows(report, runner, source)
        self.assertEqual(rows[0], {"id": generated[0].id, "original": generated[0].original,
                                   "replacement": generated[0].replacement, "status": "killed", "tests": "failed",
                                   "benchmark": "detected", "reasons": ["why"], "drift": ["d"], "class": "both"})
        self.assertEqual((rows[1]["class"], rows[1]["tests"]), ("equivalent", None))
        self.assertEqual((rows[2]["class"], rows[2]["benchmark"], rows[2]["reasons"]), ("neither", None, []))

    def test_stale_equivalents(self):
        generated = mutation.generate_mutants("toy.py", TOY_SOURCE)
        first = generated[0]
        good = {"id": first.id, "original": first.original, "replacement": first.replacement, "reason": "r"}
        moved = dict(good, id=first.id.replace(f":{first.line}:", ":99:", 1))
        changed = dict(good, replacement=first.replacement + " ")
        self.assertEqual(bench.stale_equivalents([good, moved, changed], generated), [moved["id"], changed["id"]])
        self.assertEqual(bench.stale_equivalents([], generated), [])

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
                    probe=always_imports,
                )
        self.assertGreater(result["overall"]["eligible"], 0)
        self.assertEqual(result["overall"]["counts"]["benchmark-only"], result["overall"]["eligible"])
        self.assertEqual(result["overall"]["detection_rate"], 1.0)

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


# -- planner and strategy contracts found by --qa-eval -----------------------------------------------------------

WEAPON_T1 = TrainingDesign(
    id=9101,
    name="Read Expert Weapon Theory",
    tier=1,
    stat="WeaponImprovement",
    duration=timedelta(hours=1),
    required_research=None,
    required_room_type="academy",
)
WPN_COMMON_ITEM = 9501


def academy_crew(**changes) -> CrewState:
    """Delish (a weapons role) at 0 TP in the Academy with an ordinary capacity, idle and rested."""
    fields = dict(
        character_id=301,
        name="Delish",
        design_id=1,
        rarity="Common",
        level=1,
        room_id=None,
        room_name="Academy",
        stats={},
        training_capacity=100,
        fatigue=0,
        training_design_id=None,
        training_end=None,
    )
    fields.update(changes)
    return CrewState(**fields)


def academy_context(crew: CrewState, research=None, items=None) -> TrainingContext:
    return TrainingContext(
        now=T0,
        crew=(crew,),
        designs=(WEAPON_T1,),
        rooms={},
        research_levels={"Fitness": 1} if research is None else research,
        items={} if items is None else items,
    )


def strategy_with(**changes):
    return replace(DEFAULT_STRATEGY, **changes)


class PlannerContractTests(unittest.TestCase):
    """Public planner and strategy contracts that no other test pinned.

    Found by `scripts/training_benchmark.py --qa-eval` (round 1 "neither" mutants of sdk/training/planner.py and
    sdk/training/strategy.py). They assert return values, decisions, raised errors with their diagnostic messages
    and immutability, not strategy literals (tests/test_training_benchmark_policy.py owns those). Candidates to move
    into tests/test_training_planner.py.
    """

    def test_training_state_names_every_state(self):
        self.assertEqual(training_state(academy_crew(), T0), "idle")
        self.assertEqual(training_state(academy_crew(training_design_id=WEAPON_T1.id), T0), "unknown")
        running = academy_crew(training_design_id=WEAPON_T1.id, training_end=hours(1))
        self.assertEqual(training_state(running, T0), "in_progress")
        self.assertEqual(training_state(running, hours(1)), "finished")

    def test_percent_accepts_a_capacity_of_one(self):
        self.assertEqual(percent(0, 1), 0)
        self.assertEqual(percent(1, 1), 100)
        with self.assertRaisesRegex(ValueError, "training capacity must be positive, got 0"):
            percent(1, 0)

    def test_capacity_one_crew_starts_its_first_training(self):
        crew = academy_crew(training_capacity=1)
        self.assertEqual(
            plan_training(academy_context(crew)),
            (StartTraining(301, WEAPON_T1.id, "primary T1 Read Expert Weapon Theory at 0% in academy"),),
        )

    def test_capacity_one_crew_boosts_its_band_design_stat(self):
        crew = academy_crew(training_capacity=1)
        self.assertEqual(target_stat(crew, (WEAPON_T1,), DEFAULT_STRATEGY), "WeaponImprovement")
        strategy = with_consumables(DEFAULT_STRATEGY, {("WPN", "Common"): WPN_COMMON_ITEM})
        ctx = academy_context(crew, research={}, items={WPN_COMMON_ITEM: 1})
        self.assertEqual(
            plan_training(ctx, strategy),
            (UseConsumable(301, WPN_COMMON_ITEM, "Common WPN consumable at 0% with fatigue 0"),),
        )

    def test_fatigue_limit_of_one_is_valid_and_enforced(self):
        strategy = strategy_with(max_training_fatigue=1)
        (rested,) = plan_training(academy_context(academy_crew(fatigue=0)), strategy)
        self.assertIsInstance(rested, StartTraining)
        self.assertEqual(
            plan_training(academy_context(academy_crew(fatigue=1)), strategy),
            (Skip(301, SKIP_FATIGUED, "fatigue 1 >= 1"),),
        )
        with self.assertRaisesRegex(ValueError, "^max_training_fatigue must be positive$"):
            strategy_with(max_training_fatigue=0)

    def test_strategy_parts_are_frozen_and_hashable(self):
        band = DEFAULT_STRATEGY.bands[0]
        rule = DEFAULT_STRATEGY.consumable_rules[0]
        for instance, field in ((band, "high"), (rule, "max_percent"), (DEFAULT_STRATEGY, "complete_percent")):
            with self.subTest(type(instance).__name__), self.assertRaises(FrozenInstanceError):
                setattr(instance, field, 50)
        self.assertEqual({band: 1}[TierBand(band.slot, band.tier, band.low, band.high)], 1)
        self.assertEqual(len({rule, replace(rule)}), 1)

    def test_strategy_copies_mutable_inputs(self):
        roles = list(DEFAULT_ROLES)
        rules = list(DEFAULT_STRATEGY.consumable_rules)
        stars = dict(RARITY_STARS)
        strategy = strategy_with(roles=roles, consumable_rules=rules, rarity_stars=stars)
        self.assertEqual(type(strategy.roles), tuple)
        self.assertEqual(type(strategy.consumable_rules), tuple)
        roles.clear()
        rules.clear()
        stars["Common"] = 99
        self.assertEqual(strategy.roles, DEFAULT_ROLES)
        self.assertEqual(strategy.consumable_rules, DEFAULT_STRATEGY.consumable_rules)
        self.assertEqual(strategy.rarity_stars["Common"], RARITY_STARS["Common"])
        with self.assertRaises(TypeError):
            strategy.rarity_stars["Common"] = 99  # type: ignore[index]
        (decision,) = plan_training(academy_context(academy_crew()), strategy)
        self.assertIsInstance(decision, StartTraining)

    def test_role_slot_rejects_an_unknown_slot(self):
        with self.assertRaisesRegex(ValueError, r"^unknown slot: 'tertiary'$"):
            DEFAULT_ROLES[0].slot("tertiary")

    def test_band_with_an_unknown_slot_is_rejected(self):
        bands = tuple(
            replace(band, slot="tertiary") if (band.slot, band.tier) == ("secondary", 3) else band
            for band in DEFAULT_STRATEGY.bands
        )
        gates = {**DEFAULT_STRATEGY.research_gates, ("tertiary", 3): ()}
        for roles in (DEFAULT_ROLES, ()):
            with self.subTest(roles=len(roles)), self.assertRaisesRegex(
                ValueError, r"^band names unknown slot 'tertiary'$"
            ):
                strategy_with(roles=roles, bands=bands, research_gates=gates)

    def test_exceptional_cap_keys_must_be_positive_stars_and_a_name(self):
        for key in ((0, "Mistycball"), (4, 7), (4,), ("4", "Mistycball"), (4, "Mistycball", "extra")):
            with self.subTest(key=key), self.assertRaisesRegex(
                ValueError, r"^exceptional_tp_caps needs \(stars, name\) -> positive int cap"
            ):
                strategy_with(exceptional_tp_caps={key: 100})

    def test_cap_table_errors_name_the_table(self):
        with self.assertRaisesRegex(ValueError, r"^tp_caps needs positive int stars -> cap, got 0: 5$"):
            strategy_with(tp_caps={0: 5})
        with self.assertRaisesRegex(ValueError, r"^captain_tp_caps needs positive int stars -> cap, got 6: 0$"):
            strategy_with(captain_tp_caps={6: 0})

    def test_consumable_window_errors_name_the_window(self):
        cases = (
            (r"percent window: 30\.\.29", ConsumableRule("Common", 30, 29, 0, 0)),
            (r"fatigue window: 5\.\.4", ConsumableRule("Common", 0, 29, 5, 4)),
        )
        for window, rule in cases:
            with self.subTest(window), self.assertRaisesRegex(
                ValueError, rf"^consumable rule 'Common' has an empty {window}$"
            ):
                strategy_with(consumable_rules=(rule,))


if __name__ == "__main__":
    unittest.main()
