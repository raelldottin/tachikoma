"""Tests for training-v2's pure crew core (SPEC-14 step 3) and the read-only ``run.py --crew-plan`` preview.

- ``sdk.training.strategy``: the crew policy data (roles, room type, status, research state, fatigue, Prestige targets).
- ``sdk.training.planner``: role assignment, crew and course eligibility, course choice and the crew plan.
- ``sdk.training.prestige``: Prestige targets, ingredient eligibility and recipe matching.
- ``run.py --crew-plan`` against FakePss: the exact lines, read-only, and parity with ``--fleet-dry-run``.

Every negative case has a paired control (the canonical input that does plan), so nothing passes because nothing is
planned. No network: every Pixel Starships request goes to FakePss. This module never imports
scripts/training_workflow_benchmark.py (the mutation sandbox does not copy it).
"""

import contextlib
import dataclasses
import datetime as dt
import importlib
import io
import json
import os
import pkgutil
import random
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import sdk

from sdk.training.model import CANONICAL_STATS, CourseObservation, CoursePrice, PrestigeRecipe, TrainingSession
from sdk.training.planner import (
    CrewDecision,
    CrewObservation,
    CrewPlan,
    RoleAssignment,
    RoomObservation,
    TrainingInputs,
    assign_roles,
    choose_course,
    course_blocker,
    crew_blocker,
    plan_crew,
)
from sdk.training.prestige import (
    PrestigeCandidate,
    PrestigeRoute,
    ingredient_eligible,
    match_recipe,
    prestige_routes,
    prestige_targets,
)
from sdk.training.strategy import (
    CREW_SKIP_CODES,
    DEFAULT_CREW_POLICY,
    DEFAULT_ROLES,
    DEFAULT_STRATEGY,
    ROLE_REPAIR,
    ROLE_SHIELDS,
    ROLE_WEAPONS,
    CrewPolicy,
)

ROOT = Path(os.path.abspath(__file__)).parent.parent
VOCABULARY = json.loads((ROOT / "tests/fixtures/training/workflow_benchmark.json").read_text(encoding="utf-8"))[
    "vocabulary"]
END = datetime(2026, 11, 26, 2, 0, tzinfo=timezone.utc)
IDLE = TrainingSession(None, None, "none")


def full_stats(**values):
    """All nine canonical stats, 0 unless given (``Repair=30`` sets RepairImprovement)."""
    return {stat: values.get(stat.removesuffix("Improvement"), 0) for stat in CANONICAL_STATS}


def member(cid=10001, *, design_id=30001, fatigue=0, room_id="11", session=IDLE, stats="full", **values):
    """A crew observation: every stat observed (0 unless given) unless ``stats`` is None or a mapping."""
    observed = full_stats(**values) if stats == "full" else stats
    return CrewObservation(cid, design_id, observed, fatigue, room_id, session)


def eligible(**changes):
    """The canonical repair crew: id 10001, design 30001, Repair 30, idle, rested, in the Academy (room "11")."""
    return member(**{"Repair": 30, **changes})


def academy(**changes):
    values = {"room_id": "11", "design_name": "Academy", "room_type": "Training", "level": 2, "capacity": 2,
              "status": "Normal", "running": 0, **changes}
    return RoomObservation(**values)


def bridge():
    return RoomObservation("12", "Bridge", "Bridge", 1, 0, "Normal", 0)


def course(design_id=40001, *, name="Study Expert Engineering Manual", stat="RepairImprovement", room="Academy",
           level=2, research=60001, price=CoursePrice("ok", "mineral", 500), rank=1):
    """Engineering Manual: Repair, a level 2 Academy, research 60001 completed, 500 minerals."""
    return CourseObservation(design_id, name, rank, stat, room, level, research, price)


def inputs(crew=None, **changes):
    values = {"crew": (eligible(),) if crew is None else tuple(crew), "rooms": {"11": academy(), "12": bridge()},
              "courses": (course(),), "research": {60001: "Completed"}, "capacities": {30001: 100},
              "funds": {"mineral": 1000, "gas": 0}, "spend_caps": {"mineral": None, "gas": None},
              "action_slots": None, **changes}
    return TrainingInputs(**values)


def roles_of(assignment):
    return dict(assignment.roles)


class CrewPolicyDataTests(unittest.TestCase):
    # Verifies: S14-R1
    def test_default_policy_pins_roles_room_type_status_research_fatigue(self):
        policy = DEFAULT_CREW_POLICY
        self.assertEqual(policy.roles, (("repair", "RepairImprovement"), ("shields", "ScienceImprovement"),
                                        ("weapons", "WeaponImprovement")))
        self.assertEqual((ROLE_REPAIR, ROLE_SHIELDS, ROLE_WEAPONS), ("repair", "shields", "weapons"))
        self.assertEqual(policy.training_room_type, "Training")
        self.assertEqual(policy.ready_status, "Normal")
        self.assertEqual(policy.completed_research, "Completed")
        self.assertEqual(policy.max_fatigue, 100)
        self.assertEqual(policy.max_fatigue, DEFAULT_STRATEGY.max_training_fatigue)
        # The benchmark vocabulary's role -> stat table (shields are Science).
        self.assertEqual({word: stat.removesuffix("Improvement") for word, stat in policy.roles},
                         {word: stat.removesuffix("Improvement") for word, stat in VOCABULARY["roles"].items()})

    def test_prestige_targets_are_the_original_named_characters(self):
        expected = set().union(*(role.characters for role in DEFAULT_ROLES))
        self.assertEqual(DEFAULT_CREW_POLICY.prestige_targets, frozenset(expected))
        self.assertIn("Galactic Succubus", DEFAULT_CREW_POLICY.prestige_targets)
        self.assertIsInstance(DEFAULT_CREW_POLICY.prestige_targets, frozenset)

    def test_fields_are_normalised(self):
        policy = CrewPolicy([["a", "HpImprovement"]], "T", "N", "C", 1, ["x", "x"])
        self.assertEqual(policy.roles, (("a", "HpImprovement"),))
        self.assertIsInstance(policy.roles[0], tuple)
        self.assertEqual(policy.prestige_targets, frozenset({"x"}))
        self.assertIsInstance(policy.prestige_targets, frozenset)

    def test_invalid_policy_is_refused(self):
        valid = {"roles": (("a", "HpImprovement"), ("b", "PilotImprovement")), "training_room_type": "T",
                 "ready_status": "N", "completed_research": "C", "max_fatigue": 1, "prestige_targets": ()}
        CrewPolicy(**valid)  # control
        for change, message in (
            ({"roles": (("a", "HpImprovement"), ("a", "PilotImprovement"))}, "distinct"),
            ({"roles": (("", "HpImprovement"),)}, "non-empty"),
            ({"roles": (("a", "Weapon"),)}, "CANONICAL_STATS"),
            ({"roles": (("a", "@HpImprovement"),)}, "CANONICAL_STATS"),
            ({"max_fatigue": 0}, "positive"),
            ({"max_fatigue": -1}, "positive"),
        ):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                CrewPolicy(**{**valid, **change})
        self.assertEqual(len(CANONICAL_STATS), 9)

    def test_policy_is_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            DEFAULT_CREW_POLICY.max_fatigue = 1

    def test_room_names_come_from_training_v1s_room_types(self):
        expected = {"Academy": "academy", "Lunar College": "academy", "GYM": "gym", "Galaxy Gym": "gym"}
        self.assertEqual(dict(DEFAULT_CREW_POLICY.room_names), expected)
        self.assertEqual(dict(DEFAULT_CREW_POLICY.room_names), dict(DEFAULT_STRATEGY.room_types))
        # A policy that names none uses the same mapping.
        policy = CrewPolicy(DEFAULT_CREW_POLICY.roles, "Training", "Normal", "Completed", 100, ())
        self.assertEqual(dict(policy.room_names), expected)
        names = {"Dojo": "gym"}
        custom = CrewPolicy(DEFAULT_CREW_POLICY.roles, "Training", "Normal", "Completed", 100, (), names)
        names["Moon Library"] = "academy"
        self.assertEqual(dict(custom.room_names), {"Dojo": "gym"})
        with self.assertRaises(TypeError):
            custom.room_names["Moon Library"] = "academy"

    def test_room_type_of_is_case_insensitive_and_fails_closed(self):
        for name, kind in (("Academy", "academy"), ("Lunar College", "academy"), ("lunar college", "academy"),
                           (" ACADEMY ", "academy"), ("GYM", "gym"), ("gym", "gym"), ("Galaxy Gym", "gym"),
                           ("GALAXY GYM", "gym"), ("Moon Library", None), ("Academy Lv2", None), ("", None),
                           (None, None)):
            with self.subTest(name=name):
                self.assertEqual(DEFAULT_CREW_POLICY.room_type_of(name), kind)

    def test_invalid_room_names_are_refused(self):
        roles = DEFAULT_CREW_POLICY.roles
        for names, message in (({"": "gym"}, "non-empty"), ({" ": "gym"}, "non-empty"), ({"Dojo": ""}, "non-empty"),
                               ({"Dojo": 3}, "non-empty"), ({4: "gym"}, "non-empty"),
                               ({"Dojo": "gym", "DOJO": "academy"}, "conflicting")):
            with self.subTest(names=names), self.assertRaisesRegex(ValueError, message):
                CrewPolicy(roles, "Training", "Normal", "Completed", 100, (), names)
        same = CrewPolicy(roles, "Training", "Normal", "Completed", 100, (), {"Dojo": "gym", "DOJO": "gym"})
        self.assertEqual(same.room_type_of("dojo"), "gym")  # control: the same type twice is consistent

    def test_at_most_six_roles(self):
        stats = list(CANONICAL_STATS)
        CrewPolicy([(f"r{index}", stats[index]) for index in range(6)], "T", "N", "C", 1, ())  # control
        with self.assertRaisesRegex(ValueError, "at most 6 roles"):
            CrewPolicy([(f"r{index}", stats[index]) for index in range(7)], "T", "N", "C", 1, ())

    def test_skip_codes_pin(self):
        self.assertEqual(CREW_SKIP_CODES, frozenset({
            "not_in_training_room", "training_in_progress", "unclaimed_course", "room_not_ready", "room_level",
            "room_capacity", "research_missing", "research_unknown", "stats_unknown", "price_unavailable",
            "price_malformed", "premium_currency", "insufficient_funds", "cap_exceeded", "role_uncovered", "fatigued",
            "tp_cap_reached", "action_cap_reached", "design_missing"}))
        self.assertLessEqual(CREW_SKIP_CODES, set(VOCABULARY["skip_codes"]) | {"design_missing"})


def oracle_assignment(crew, roles):
    """Every injective partial assignment of crew to role slots, best first by: most slots filled, then the highest
    stat sum, then the higher scores held by lower instance ids, then earlier slots filled, then lower ids in slot
    order."""
    known = [entry for entry in crew if entry.stats is not None]
    slots = len(roles)
    options = [None, *known]
    best = None
    for choice in _choices(options, slots):
        used = [entry.character_id for entry in choice if entry is not None]
        if len(used) != len(set(used)):
            continue
        scored = [(entry.stats[stat], entry.character_id) for entry, (_, stat) in zip(choice, roles)
                  if entry is not None]
        key = (-len(scored), -sum(score for score, _ in scored), sorted((-score, cid) for score, cid in scored),
               [entry is None for entry in choice], [entry.character_id for entry in choice if entry is not None])
        if best is None or key < best[0]:
            best = (key, choice)
    return {word: entry.character_id for (word, _), entry in zip(roles, best[1]) if entry is not None}


def _choices(options, slots):
    if slots == 0:
        yield ()
        return
    for option in options:
        for rest in _choices(options, slots - 1):
            yield (option, *rest)


class RoleAssignmentTests(unittest.TestCase):
    # Verifies: S14-R1
    def test_empty_roster_uncovers_every_role(self):
        assignment = assign_roles((), DEFAULT_CREW_POLICY)
        self.assertEqual(assignment, RoleAssignment({}, ("repair", "shields", "weapons"), ()))

    def test_one_crew_takes_its_best_role(self):
        assignment = assign_roles([member(10001, Repair=20)], DEFAULT_CREW_POLICY)
        self.assertEqual(roles_of(assignment), {"repair": 10001})
        self.assertEqual(assignment.uncovered, ("shields", "weapons"))
        self.assertEqual(assignment.stats_unknown, ())
        weapon = assign_roles([member(10001, Weapon=20)], DEFAULT_CREW_POLICY)
        self.assertEqual((roles_of(weapon), weapon.uncovered), ({"weapons": 10001}, ("repair", "shields")))

    def test_distinct_roles_beat_the_greedy_best_stat(self):
        ana, ben, cyd = member(10001, Weapon=40, Repair=35), member(10002, Weapon=38), member(10003, Science=30)
        self.assertEqual(roles_of(assign_roles([ana, ben, cyd], DEFAULT_CREW_POLICY)),
                         {"repair": 10001, "weapons": 10002, "shields": 10003})
        two = assign_roles([ana, ben], DEFAULT_CREW_POLICY)
        self.assertEqual(roles_of(two), {"repair": 10001, "weapons": 10002})
        self.assertEqual(two.uncovered, ("shields",))

    def test_a_zero_score_crew_still_covers_a_role(self):
        assignment = assign_roles([member(10001, Weapon=40), member(10002)], DEFAULT_CREW_POLICY)
        self.assertEqual(roles_of(assignment), {"weapons": 10001, "repair": 10002})
        self.assertEqual(assignment.uncovered, ("shields",))

    def test_equal_scores_go_to_the_lower_id_in_any_input_order(self):
        tia, tob = member(10001, Repair=30), member(10002, Repair=30)
        for order in ([tia, tob], [tob, tia]):
            with self.subTest(order=[entry.character_id for entry in order]):
                assignment = assign_roles(order, DEFAULT_CREW_POLICY)
                self.assertEqual(roles_of(assignment), {"repair": 10001, "shields": 10002})
                self.assertEqual(assignment.uncovered, ("weapons",))

    def test_observed_zero_is_a_value(self):
        una = member(10001, stats={**full_stats(Weapon=30), "RepairImprovement": 0})
        obi = member(10002, Science=25)
        assignment = assign_roles([obi], DEFAULT_CREW_POLICY)
        self.assertEqual(roles_of(assignment), {"shields": 10002})
        self.assertEqual(assignment.stats_unknown, ())
        self.assertEqual(roles_of(assign_roles([una], DEFAULT_CREW_POLICY)), {"weapons": 10001})

    def test_an_unobserved_stat_is_stats_unknown(self):
        assignment = assign_roles([member(10001, stats=None)], DEFAULT_CREW_POLICY)
        self.assertEqual(assignment, RoleAssignment({}, ("repair", "shields", "weapons"), (10001,)))
        mixed = assign_roles([member(10003, stats=None), member(10002, Science=25), member(10001, stats=None)],
                             DEFAULT_CREW_POLICY)
        self.assertEqual((roles_of(mixed), mixed.stats_unknown), ({"shields": 10002}, (10001, 10003)))

    def test_stat_change_reassigns_deterministically(self):
        before = [member(10001, Weapon=40, Repair=35), member(10002, Weapon=38), member(10003, Science=30)]
        after = [member(10001, Weapon=38), member(10002, Weapon=40, Repair=35), member(10003, Science=30)]
        self.assertEqual(roles_of(assign_roles(before, DEFAULT_CREW_POLICY))["repair"], 10001)
        self.assertEqual(roles_of(assign_roles(after, DEFAULT_CREW_POLICY)),
                         {"repair": 10002, "weapons": 10001, "shields": 10003})

    def test_more_crew_than_roles_keeps_the_best_per_role(self):
        crew = [member(10000 + index, Repair=index) for index in range(1, 8)]
        assignment = assign_roles(crew, DEFAULT_CREW_POLICY)
        self.assertEqual(roles_of(assignment), {"repair": 10007, "shields": 10001, "weapons": 10002})

    def test_matches_an_exhaustive_oracle(self):
        roles = DEFAULT_CREW_POLICY.roles
        for seed in range(300):
            generator = random.Random(seed)
            ids = generator.sample(range(10001, 10040), generator.randint(0, 7))
            crew = []
            for cid in ids:
                if generator.random() < 0.15:
                    crew.append(member(cid, stats=None))
                else:
                    crew.append(member(cid, Repair=generator.randint(0, 2), Science=generator.randint(0, 2),
                                       Weapon=generator.randint(0, 2), Hp=generator.randint(0, 2)))
            with self.subTest(seed=seed):
                assignment = assign_roles(crew, DEFAULT_CREW_POLICY)
                expected = oracle_assignment(crew, roles)
                self.assertEqual(roles_of(assignment), expected)
                self.assertEqual(assignment.uncovered, tuple(word for word, _ in roles if word not in expected))
                self.assertEqual(assignment.stats_unknown,
                                 tuple(sorted(entry.character_id for entry in crew if entry.stats is None)))

    def test_custom_policy_roles_and_order(self):
        policy = CrewPolicy((("pilot", "PilotImprovement"),), "Training", "Normal", "Completed", 100, ())
        assignment = assign_roles([member(10001, Pilot=3), member(10002, Pilot=5)], policy)
        self.assertEqual((roles_of(assignment), assignment.uncovered), ({"pilot": 10002}, ()))

    def test_no_crew_holds_two_roles(self):
        assignment = assign_roles([member(10001, Repair=5, Science=5, Weapon=5)], DEFAULT_CREW_POLICY)
        self.assertEqual(roles_of(assignment), {"repair": 10001})
        self.assertEqual(assignment.uncovered, ("shields", "weapons"))

    def test_assignment_is_frozen(self):
        assignment = assign_roles([member(10001, Repair=5)], DEFAULT_CREW_POLICY)
        with self.assertRaises(TypeError):
            assignment.roles["shields"] = 1
        self.assertIsInstance(assignment.uncovered, tuple)



def code_of(blocker):
    return None if blocker is None else blocker[0]


class CrewBlockerTests(unittest.TestCase):
    # Verifies: S14-R2
    def blocked(self, crew=None, **changes):
        return code_of(crew_blocker(crew or eligible(), inputs(**changes), DEFAULT_CREW_POLICY))

    def test_control_is_eligible(self):
        self.assertIsNone(crew_blocker(eligible(), inputs(), DEFAULT_CREW_POLICY))

    def test_session(self):
        for session, code in (
            (TrainingSession(40001, END, "running"), "training_in_progress"),
            (TrainingSession(None, None, "unknown"), "training_in_progress"),
            (TrainingSession(40001, END, "ended_unclaimed"), "unclaimed_course"),
            (TrainingSession(40001, END, "finished"), None),
            (TrainingSession(None, END, "finished"), None),
            (IDLE, None),
        ):
            with self.subTest(phase=session.phase, course=session.course_id):
                self.assertEqual(self.blocked(eligible(session=session)), code)

    def test_stats_unknown(self):
        self.assertEqual(self.blocked(eligible(stats=None)), "stats_unknown")

    def test_fatigue(self):
        for fatigue, code in ((0, None), (99, None), (100, "fatigued"), (101, "fatigued"), (None, "fatigued")):
            with self.subTest(fatigue=fatigue):
                self.assertEqual(self.blocked(eligible(fatigue=fatigue)), code)
        policy = CrewPolicy(DEFAULT_CREW_POLICY.roles, "Training", "Normal", "Completed", 50, ())
        self.assertEqual(code_of(crew_blocker(eligible(fatigue=50), inputs(), policy)), "fatigued")
        self.assertIsNone(crew_blocker(eligible(fatigue=49), inputs(), policy))

    def test_training_points_against_the_design_capacity(self):
        for repair, code in ((69, None), (99, None), (100, "tp_cap_reached"), (101, "tp_cap_reached")):
            with self.subTest(total=repair):
                self.assertEqual(self.blocked(eligible(Repair=repair)), code)
        # The sum of all nine stats counts, not one stat.
        self.assertEqual(self.blocked(eligible(Repair=50, Hp=25, Stamina=25)), "tp_cap_reached")
        self.assertIsNone(self.blocked(eligible(Repair=50, Hp=25, Stamina=24)))
        self.assertEqual(self.blocked(eligible(design_id=None)), "tp_cap_reached")
        self.assertEqual(self.blocked(capacities={}), "tp_cap_reached")
        self.assertEqual(self.blocked(capacities={30002: 100}), "tp_cap_reached")

    def test_room(self):
        self.assertEqual(self.blocked(eligible(room_id=None)), "not_in_training_room")
        self.assertEqual(self.blocked(eligible(room_id="99")), "not_in_training_room")
        self.assertEqual(self.blocked(eligible(room_id="12")), "not_in_training_room")
        self.assertEqual(self.blocked(rooms={"11": academy(room_type="Bridge")}), "not_in_training_room")
        self.assertEqual(self.blocked(rooms={"11": academy(room_type=None)}), "not_in_training_room")
        policy = CrewPolicy(DEFAULT_CREW_POLICY.roles, "Gym", "Normal", "Completed", 100, ())
        self.assertEqual(code_of(crew_blocker(eligible(), inputs(), policy)), "not_in_training_room")
        self.assertIsNone(crew_blocker(eligible(), inputs(rooms={"11": academy(room_type="Gym")}), policy))

    def test_status(self):
        self.assertEqual(self.blocked(rooms={"11": academy(status="Upgrading")}), "room_not_ready")
        self.assertEqual(self.blocked(rooms={"11": academy(status=None)}), "room_not_ready")
        policy = CrewPolicy(DEFAULT_CREW_POLICY.roles, "Training", "Ready", "Completed", 100, ())
        self.assertEqual(code_of(crew_blocker(eligible(), inputs(), policy)), "room_not_ready")

    def test_capacity(self):
        for capacity, running, code in ((2, 1, None), (2, 0, None), (2, 2, "room_capacity"), (2, 3, "room_capacity"),
                                        (None, 0, "room_capacity"), (2, None, "room_capacity"),
                                        (None, None, "room_capacity"), (0, 0, "room_capacity")):
            with self.subTest(capacity=capacity, running=running):
                self.assertEqual(self.blocked(rooms={"11": academy(capacity=capacity, running=running)}), code)

    def test_order_of_checks(self):
        tired_and_busy = eligible(session=TrainingSession(40001, END, "running"), fatigue=100)
        self.assertEqual(self.blocked(tired_and_busy), "training_in_progress")
        self.assertEqual(self.blocked(eligible(fatigue=100, Repair=100)), "fatigued")
        self.assertEqual(self.blocked(eligible(Repair=100, room_id="12")), "tp_cap_reached")
        self.assertEqual(self.blocked(eligible(room_id="12"), rooms={"12": RoomObservation(
            "12", "Bridge", "Bridge", 1, 0, "Upgrading", 0)}), "not_in_training_room")
        self.assertEqual(self.blocked(rooms={"11": academy(status="Upgrading", running=2)}), "room_not_ready")

    def test_details_name_the_observation(self):
        self.assertEqual(crew_blocker(eligible(fatigue=100), inputs(), DEFAULT_CREW_POLICY),
                         ("fatigued", "fatigue 100, limit 100", False))
        self.assertEqual(crew_blocker(eligible(room_id="12"), inputs(), DEFAULT_CREW_POLICY),
                         ("not_in_training_room", "room 12 is not a Training room", False))

    def test_missing_data_is_labelled_unknown(self):
        unknown_session = TrainingSession(None, None, "unknown")
        cases = (
            # (crew, input changes, code, unknown)
            (eligible(stats=None), {}, "stats_unknown", True),
            (eligible(session=unknown_session), {}, "training_in_progress", True),
            (eligible(session=TrainingSession(40001, END, "running")), {}, "training_in_progress", False),
            (eligible(session=TrainingSession(40001, END, "ended_unclaimed")), {}, "unclaimed_course", False),
            (eligible(fatigue=None), {}, "fatigued", True),
            (eligible(fatigue=100), {}, "fatigued", False),
            (eligible(design_id=None), {}, "tp_cap_reached", True),
            (eligible(), {"capacities": {}}, "tp_cap_reached", True),
            (eligible(Repair=100), {}, "tp_cap_reached", False),
            (eligible(room_id=None), {}, "not_in_training_room", True),
            (eligible(room_id="99"), {}, "not_in_training_room", True),
            (eligible(), {"rooms": {"11": academy(room_type=None)}}, "not_in_training_room", True),
            (eligible(room_id="12"), {}, "not_in_training_room", False),
            (eligible(), {"rooms": {"11": academy(status=None)}}, "room_not_ready", True),
            (eligible(), {"rooms": {"11": academy(status="Upgrading")}}, "room_not_ready", False),
            (eligible(), {"rooms": {"11": academy(capacity=None)}}, "room_capacity", True),
            (eligible(), {"rooms": {"11": academy(running=None)}}, "room_capacity", True),
            (eligible(), {"rooms": {"11": academy(capacity=None, running=None)}}, "room_capacity", True),
            (eligible(), {"rooms": {"11": academy(running=2)}}, "room_capacity", False),
        )
        for crew, changes, code, unknown in cases:
            with self.subTest(code=code, unknown=unknown, changes=changes):
                blocker = crew_blocker(crew, inputs(**changes), DEFAULT_CREW_POLICY)
                self.assertEqual((blocker[0], blocker[2]), (code, unknown))
                self.assertIs(blocker[2], unknown)


class CourseBlockerTests(unittest.TestCase):
    # Verifies: S14-R2
    def blocked(self, room=None, policy=DEFAULT_CREW_POLICY, course_changes=None, **changes):
        return code_of(course_blocker(course(**(course_changes or {})), room or academy(), inputs(**changes), policy))

    def test_control_is_eligible(self):
        self.assertIsNone(course_blocker(course(), academy(), inputs(), DEFAULT_CREW_POLICY))

    def test_room_names_satisfy_their_room_type(self):
        for name, required, code in (
            ("Academy", "Academy", None),
            ("Lunar College", "Academy", None),
            ("lunar college", "academy", None),
            ("GYM", "Gym", None),
            ("Galaxy Gym", "Gym", None),
            ("Galaxy Gym", "GYM", None),
            ("Academy", "Gym", "not_in_training_room"),
            ("Lunar College", "Gym", "not_in_training_room"),
            ("GYM", "Academy", "not_in_training_room"),
            ("Moon Library", "Academy", "not_in_training_room"),
            ("Moon Library", "Moon Library", "not_in_training_room"),
        ):
            with self.subTest(room=name, required=required):
                self.assertEqual(self.blocked(academy(design_name=name), course_changes={"room": required}), code)
        # An unknown name is a known refusal; an unobserved name is unknown.
        self.assertEqual(course_blocker(course(), academy(design_name="Moon Library"), inputs(), DEFAULT_CREW_POLICY),
                         ("not_in_training_room", "course 40001 needs a Academy room, room 11 is a Moon Library", False))
        custom = CrewPolicy(DEFAULT_CREW_POLICY.roles, "Training", "Normal", "Completed", 100, (), {"Dojo": "gym"})
        self.assertIsNone(self.blocked(academy(design_name="Dojo"), custom, {"room": "Gym"}))
        self.assertEqual(self.blocked(academy(design_name="GYM"), custom, {"room": "Gym"}), "not_in_training_room")

    def test_room_name(self):
        self.assertEqual(self.blocked(course_changes={"room": "Gym"}), "not_in_training_room")
        self.assertIsNone(self.blocked(course_changes={"room": ""}))
        self.assertIsNone(self.blocked(academy(design_name=None), course_changes={"room": ""}))
        self.assertEqual(self.blocked(academy(design_name=None)), "not_in_training_room")
        self.assertEqual(self.blocked(course_changes={"room": None}), "not_in_training_room")

    def test_level_is_a_minimum(self):
        for required, room_level, code in ((2, 2, None), (2, 1, "room_level"), (1, 2, None), (2, 3, None),
                                           (None, 2, "room_level"), (2, None, "room_level"), (1, None, "room_level"),
                                           (0, None, None), (0, 1, None)):
            with self.subTest(required=required, room_level=room_level):
                self.assertEqual(self.blocked(academy(level=room_level), course_changes={"level": required}), code)

    def test_research(self):
        self.assertIsNone(self.blocked(course_changes={"research": 0}, research=None))
        self.assertIsNone(self.blocked(course_changes={"research": 0}, research={}))
        self.assertIsNone(self.blocked(research={60001: "Completed"}))
        self.assertEqual(self.blocked(research={60001: "Researching"}), "research_missing")
        self.assertEqual(self.blocked(research={}), "research_missing")
        self.assertEqual(self.blocked(research={60002: "Completed"}), "research_missing")
        self.assertEqual(self.blocked(research=None), "research_unknown")
        self.assertEqual(self.blocked(course_changes={"research": None}), "research_unknown")
        self.assertEqual(self.blocked(course_changes={"research": 1}, research={}), "research_missing")
        self.assertIsNone(self.blocked(course_changes={"research": 1}, research={1: "Completed"}))
        policy = CrewPolicy(DEFAULT_CREW_POLICY.roles, "Training", "Normal", "Done", 100, ())
        self.assertEqual(self.blocked(policy=policy), "research_missing")
        self.assertIsNone(self.blocked(policy=policy, research={60001: "Done"}))

    def test_price_status(self):
        for price, code in ((CoursePrice("unavailable"), "price_unavailable"),
                            (CoursePrice("malformed"), "price_malformed"),
                            (CoursePrice("premium", "starbux", 5), "premium_currency"),
                            (CoursePrice("premium", "starbux", 0), "premium_currency")):
            with self.subTest(price=price):
                self.assertEqual(self.blocked(course_changes={"price": price}), code)

    def test_funds_and_caps(self):
        def price(currency, amount):
            return {"price": CoursePrice("ok", currency, amount)}

        cases = (
            (price("mineral", 500), {"funds": {"mineral": 500, "gas": 0}}, None),
            (price("mineral", 500), {"funds": {"mineral": 499, "gas": 0}}, "insufficient_funds"),
            (price("mineral", 500), {"funds": {"mineral": None, "gas": 0}}, "insufficient_funds"),
            (price("mineral", 500), {"funds": {"mineral": None, "gas": None}}, "insufficient_funds"),
            (price("mineral", 1), {"funds": {"mineral": None, "gas": 0}}, "insufficient_funds"),
            (price("mineral", 1), {"funds": {"mineral": 0, "gas": 0}}, "insufficient_funds"),
            (price("mineral", 0), {"funds": {"mineral": 0, "gas": 0}}, None),
            (price("mineral", 0), {"funds": {"mineral": None, "gas": None}}, None),
            (price("mineral", 0), {"funds": {"mineral": -5, "gas": 0}}, None),
            (price("gas", 300), {"funds": {"mineral": 0, "gas": 300}}, None),
            (price("gas", 300), {"funds": {"mineral": 1000, "gas": 299}}, "insufficient_funds"),
            (price("mineral", 500), {"spend_caps": {"mineral": 499, "gas": None}}, "cap_exceeded"),
            (price("mineral", 500), {"spend_caps": {"mineral": 500, "gas": None}}, None),
            (price("mineral", 500), {"spend_caps": {"mineral": None, "gas": 0}}, None),
            (price("mineral", 500), {"spend_caps": {"mineral": None, "gas": None}}, None),
            (price("gas", 300), {"funds": {"mineral": 0, "gas": 300}, "spend_caps": {"mineral": 0, "gas": 299}},
             "cap_exceeded"),
            (price("gas", 300), {"funds": {"mineral": 0, "gas": 300}, "spend_caps": {"mineral": 0, "gas": 300}}, None),
            (price("mineral", 0), {"spend_caps": {"mineral": 0, "gas": 0}}, None),
            (price("mineral", 500), {"funds": {"mineral": 400, "gas": 0}, "spend_caps": {"mineral": 100, "gas": 0}},
             "insufficient_funds"),
        )
        for course_changes, changes, code in cases:
            with self.subTest(price=course_changes["price"], changes=changes):
                self.assertEqual(self.blocked(course_changes=course_changes, **changes), code)

    def test_an_earlier_reservation_leaves_too_little(self):
        # 900 minerals less an earlier 600 upgrade leaves 300 spendable: the 500 course no longer fits.
        self.assertEqual(self.blocked(funds={"mineral": 300, "gas": 0}), "insufficient_funds")
        self.assertIsNone(self.blocked(funds={"mineral": 1100 - 600, "gas": 0}))

    def test_order_of_checks(self):
        everything = {"room": "Gym", "level": 3, "research": None, "price": CoursePrice("unavailable")}
        self.assertEqual(self.blocked(course_changes=everything), "not_in_training_room")
        self.assertEqual(self.blocked(course_changes={**everything, "room": "Academy"}), "room_level")
        self.assertEqual(self.blocked(course_changes={**everything, "room": "Academy", "level": 2}),
                         "research_unknown")
        self.assertEqual(self.blocked(course_changes={"price": CoursePrice("unavailable")}, research={}),
                         "research_missing")
        self.assertEqual(self.blocked(course_changes={"price": CoursePrice("malformed")},
                                      funds={"mineral": 0, "gas": 0}), "price_malformed")

    def test_details_name_the_observation(self):
        self.assertEqual(course_blocker(course(), academy(level=1), inputs(), DEFAULT_CREW_POLICY),
                         ("room_level", "course 40001 needs room level 2, room level 1", False))
        self.assertEqual(course_blocker(course(), academy(), inputs(funds={"mineral": 499, "gas": 0}),
                                        DEFAULT_CREW_POLICY),
                         ("insufficient_funds", "course 40001 costs 500 mineral, 499 spendable", False))

    def test_missing_data_is_labelled_unknown(self):
        cases = (
            # (room, course changes, input changes, code, unknown)
            (academy(), {"room": None}, {}, "not_in_training_room", True),
            (academy(design_name=None), {}, {}, "not_in_training_room", True),
            (academy(), {"room": "Gym"}, {}, "not_in_training_room", False),
            (academy(), {"level": None}, {}, "room_level", True),
            (academy(level=None), {}, {}, "room_level", True),
            (academy(level=1), {}, {}, "room_level", False),
            (academy(), {"research": None}, {}, "research_unknown", True),
            (academy(), {}, {"research": None}, "research_unknown", True),
            (academy(), {}, {"research": {}}, "research_missing", False),
            (academy(), {"price": CoursePrice("unavailable")}, {}, "price_unavailable", False),
            (academy(), {}, {"funds": {"mineral": None, "gas": 0}}, "insufficient_funds", True),
            (academy(), {}, {"funds": {"mineral": 499, "gas": 0}}, "insufficient_funds", False),
            (academy(), {}, {"spend_caps": {"mineral": 499, "gas": None}}, "cap_exceeded", False),
        )
        for room, course_changes, changes, code, unknown in cases:
            with self.subTest(code=code, unknown=unknown, course=course_changes, changes=changes):
                blocker = course_blocker(course(**course_changes), room, inputs(**changes), DEFAULT_CREW_POLICY)
                self.assertEqual((blocker[0], blocker[2]), (code, unknown))
                self.assertIs(blocker[2], unknown)


SEMINAR = course(40004, name="Synthetic Repair Seminar", level=1, research=0, price=CoursePrice("ok", "gas", 300))
DRILLS = course(40002, name="Synthetic Weapon Drills", stat="WeaponImprovement", level=1,
                price=CoursePrice("ok", "mineral", 400))


class ChooseCourseTests(unittest.TestCase):
    # Verifies: S14-R2
    def choose(self, stat="RepairImprovement", room=None, **changes):
        return choose_course(stat, room or academy(), inputs(**changes), DEFAULT_CREW_POLICY)

    def test_control_chooses_the_matching_course(self):
        self.assertEqual(self.choose(), course())

    def test_wrong_stat_course_is_never_chosen(self):
        self.assertEqual(self.choose(courses=(DRILLS,)),
                         ("design_missing", "no observed course trains RepairImprovement", False))
        self.assertEqual(self.choose("WeaponImprovement", courses=(course(), DRILLS)), DRILLS)
        self.assertEqual(self.choose("ScienceImprovement", courses=(course(), DRILLS)),
                         ("design_missing", "no observed course trains ScienceImprovement", False))

    def test_lowest_eligible_design_id_wins_in_any_order(self):
        low, high = course(40001, name="Low"), course(40009, name="High")
        for courses in ((low, high), (high, low)):
            with self.subTest(first=courses[0].design_id):
                self.assertEqual(self.choose(courses=courses), low)

    def test_first_eligible_after_an_ineligible_one(self):
        level_one = academy(level=1)
        funds = {"mineral": 0, "gas": 300}
        for courses in ((course(), SEMINAR), (SEMINAR, course())):
            with self.subTest(first=courses[0].design_id):
                self.assertEqual(self.choose(room=level_one, courses=courses, funds=funds), SEMINAR)
        # Control: in a level 2 Academy with minerals, the lower id (the manual) wins.
        self.assertEqual(self.choose(courses=(SEMINAR, course()), funds={"mineral": 1000, "gas": 300}), course())

    def test_all_ineligible_reports_the_lowest_id_course_code(self):
        broke = {"mineral": 0, "gas": 0}
        self.assertEqual(code_of(self.choose(room=academy(level=1), courses=(SEMINAR, course()), funds=broke)),
                         "room_level")
        self.assertEqual(code_of(self.choose(courses=(SEMINAR, course()), funds=broke)), "insufficient_funds")
        self.assertEqual(self.choose(courses=(course(), SEMINAR), funds=broke, research={}),
                         ("research_missing", "course 40001 needs research 60001 Completed", False))

    def test_courses_unobserved_is_design_missing(self):
        self.assertEqual(self.choose(courses=None),
                         ("design_missing", "RepairImprovement courses: course designs not observed", True))
        self.assertEqual(self.choose(courses=()), ("design_missing", "no observed course trains RepairImprovement", False))


def summary(plan):
    """(role, crew, course id or None, code or None) per decision."""
    return [(entry.role, entry.character_id, entry.course.design_id if entry.course else None, entry.code)
            for entry in plan.decisions]


class PlanCrewTests(unittest.TestCase):
    # Verifies: S14-R1, S14-R2
    def test_control_starts_the_repair_specialist(self):
        plan = plan_crew(inputs())
        self.assertEqual(plan.decisions, (
            CrewDecision("repair", 10001, course(), None, "course 40001 Study Expert Engineering Manual"),
            CrewDecision("shields", None, None, "role_uncovered", "no observed crew for the ScienceImprovement slot"),
            CrewDecision("weapons", None, None, "role_uncovered", "no observed crew for the WeaponImprovement slot"),
        ))
        self.assertEqual(plan.start, plan.decisions[0])
        self.assertEqual(plan.assignment, assign_roles(inputs().crew, DEFAULT_CREW_POLICY))

    def test_decisions_follow_role_order_then_stats_unknown_by_id(self):
        crew = [member(10005, stats=None), member(10004, Weapon=9, room_id="12"), member(10003, stats=None),
                member(10002, Science=7, room_id="12"), member(10001, Repair=30)]
        plan = plan_crew(inputs(crew))
        self.assertEqual(summary(plan), [("repair", 10001, 40001, None),
                                         ("shields", 10002, None, "not_in_training_room"),
                                         ("weapons", 10004, None, "not_in_training_room"),
                                         (None, 10003, None, "stats_unknown"),
                                         (None, 10005, None, "stats_unknown")])
        self.assertEqual(plan.decisions[3].detail, "character 10003 has a stat that was not observed")

    def test_at_most_one_start(self):
        crew = [eligible(), member(10002, Weapon=20)]
        plan = plan_crew(inputs(crew, courses=(course(), DRILLS)))
        self.assertEqual(summary(plan), [("repair", 10001, 40001, None), ("shields", None, None, "role_uncovered"),
                                         ("weapons", 10002, None, "action_cap_reached")])
        self.assertEqual(plan.decisions[2].detail, "course 40002 waits: one training start per account per run")
        self.assertEqual(plan.start.character_id, 10001)
        # Control: alone, the weapons specialist does start.
        self.assertEqual(plan_crew(inputs([member(10002, Weapon=20)], courses=(DRILLS,))).start.course, DRILLS)

    def test_a_blocked_earlier_role_leaves_the_start_to_a_later_one(self):
        crew = [eligible(fatigue=100), member(10002, Weapon=20)]
        plan = plan_crew(inputs(crew, courses=(course(), DRILLS)))
        self.assertEqual(summary(plan), [("repair", 10001, None, "fatigued"), ("shields", None, None, "role_uncovered"),
                                         ("weapons", 10002, 40002, None)])

    def test_no_action_slot_left(self):
        for slots, started in ((None, True), (2, True), (1, True), (0, False), (-1, False)):
            with self.subTest(slots=slots):
                plan = plan_crew(inputs(action_slots=slots))
                self.assertEqual(plan.start is not None, started)
                if not started:
                    self.assertEqual(plan.decisions[0], CrewDecision(
                        "repair", 10001, None, "action_cap_reached",
                        f"course 40001 waits: {slots} requests left under max_requests"))

    def test_role_uncovered_has_no_crew(self):
        plan = plan_crew(inputs(()))
        self.assertEqual(summary(plan), [(word, None, None, "role_uncovered") for word, _ in DEFAULT_CREW_POLICY.roles])
        self.assertIsNone(plan.start)

    def test_skips_carry_the_blocker(self):
        plan = plan_crew(inputs(rooms={"11": academy(status="Upgrading")}))
        self.assertEqual(plan.decisions[0],
                         CrewDecision("repair", 10001, None, "room_not_ready", "room 11 status Upgrading"))
        plan = plan_crew(inputs(funds={"mineral": 499, "gas": 0}))
        self.assertEqual(plan.decisions[0], CrewDecision("repair", 10001, None, "insufficient_funds",
                                                         "course 40001 costs 500 mineral, 499 spendable"))
        self.assertIsNone(plan.start)

    def test_custom_policy(self):
        policy = CrewPolicy((("pilot", "PilotImprovement"),), "Training", "Normal", "Completed", 100, ())
        pilot_course = course(40007, name="Pilot Handbook", stat="PilotImprovement")
        plan = plan_crew(inputs([member(10001, Pilot=4)], courses=(course(), pilot_course)), policy)
        self.assertEqual(summary(plan), [("pilot", 10001, 40007, None)])

    def test_decision_validation(self):
        CrewDecision("repair", 10001, course(), None, "x")
        CrewDecision("repair", 10001, None, "fatigued", "x")
        self.assertIs(CrewDecision("repair", 10001, None, "fatigued", "x", True).unknown, True)
        self.assertIs(CrewDecision("repair", 10001, None, "fatigued", "x").unknown, False)
        for decision_course, code in ((course(), "fatigued"), (None, None), (None, "complete"), (None, "")):
            with self.subTest(course=decision_course, code=code), self.assertRaises(ValueError):
                CrewDecision("repair", 10001, decision_course, code, "x")
        with self.assertRaisesRegex(ValueError, "unknown"):
            CrewDecision("repair", 10001, course(), None, "x", True)
        for flag in (1, 0, None, "yes"):
            with self.subTest(unknown=flag), self.assertRaisesRegex(ValueError, "unknown must be a bool"):
                CrewDecision("repair", 10001, None, "fatigued", "x", flag)

    def test_decisions_carry_the_unknown_label(self):
        crew = [eligible(fatigue=None), member(10002, Weapon=9, room_id="12"), member(10003, stats=None)]
        plan = plan_crew(inputs(crew))
        self.assertEqual([(entry.code, entry.unknown) for entry in plan.decisions],
                         [("fatigued", True), ("role_uncovered", False), ("not_in_training_room", False),
                          ("stats_unknown", True)])
        self.assertIs(plan_crew(inputs()).start.unknown, False)
        unknown_research = plan_crew(inputs(research=None)).decisions[0]
        self.assertEqual((unknown_research.code, unknown_research.unknown), ("research_unknown", True))
        unseen = plan_crew(inputs(courses=None)).decisions[0]
        self.assertEqual((unseen.code, unseen.unknown), ("design_missing", True))
        capped = plan_crew(inputs(action_slots=0)).decisions[0]
        self.assertEqual((capped.code, capped.unknown), ("action_cap_reached", False))

    def test_plan_validation_and_start_property(self):
        start = CrewDecision("repair", 10001, course(), None, "x")
        other = CrewDecision("weapons", 10002, DRILLS, None, "y")
        skip = CrewDecision("shields", None, None, "role_uncovered", "z")
        empty = RoleAssignment({}, (), ())
        self.assertIs(CrewPlan(empty, (skip, start)).start, start)
        self.assertIsNone(CrewPlan(empty, (skip,)).start)
        self.assertIsNone(CrewPlan(empty, ()).start)
        with self.assertRaisesRegex(ValueError, "at most one start"):
            CrewPlan(empty, (start, skip, other))

    def test_missing_data_is_never_eligible(self):
        unknown = TrainingSession(None, None, "unknown")
        cases = {
            "crew design": {"crew": [eligible(design_id=None)]},
            "fatigue": {"crew": [eligible(fatigue=None)]},
            "room id": {"crew": [eligible(room_id=None)]},
            "stats": {"crew": [eligible(stats=None)]},
            "session": {"crew": [eligible(session=unknown)]},
            "room type": {"rooms": {"11": academy(room_type=None)}},
            "room status": {"rooms": {"11": academy(status=None)}},
            "room capacity": {"rooms": {"11": academy(capacity=None)}},
            "running": {"rooms": {"11": academy(running=None)}},
            "room level": {"rooms": {"11": academy(level=None)}},
            "room name": {"rooms": {"11": academy(design_name=None)}},
            "course room": {"courses": (course(room=None),)},
            "course level": {"courses": (course(level=None),)},
            "course research": {"courses": (course(research=None),)},
            "research map": {"research": None},
            "funds": {"funds": {"mineral": None, "gas": None}},
            "capacities": {"capacities": {}},
            "courses": {"courses": None},
        }
        self.assertIsNotNone(plan_crew(inputs()).start)  # control
        for name, changes in cases.items():
            with self.subTest(missing=name):
                plan = plan_crew(inputs(**changes))
                self.assertIsNone(plan.start)
                self.assertTrue(all(entry.code in CREW_SKIP_CODES for entry in plan.decisions))


class ObservationTests(unittest.TestCase):
    # Verifies: S14-R1, S14-R2
    def test_crew_observation_validation(self):
        member(1)  # control: the smallest positive id
        for cid in (0, -1, True, "10001", 1.0):
            with self.subTest(cid=cid), self.assertRaisesRegex(ValueError, "positive int"):
                member(cid)
        with self.assertRaisesRegex(ValueError, "nine CANONICAL_STATS"):
            member(stats={"RepairImprovement": 1})
        with self.assertRaisesRegex(ValueError, "nine CANONICAL_STATS"):
            member(stats={**full_stats(), "Repair": 1})
        observed = member(stats=dict(full_stats(Repair=3)))
        with self.assertRaises(TypeError):
            observed.stats["RepairImprovement"] = 4
        self.assertIsNone(member(stats=None).stats)

    def test_inputs_are_normalised_and_read_only(self):
        rooms, research = {"11": academy()}, {60001: "Completed"}
        built = TrainingInputs([eligible()], rooms, [course()], research, {30001: 100}, {"mineral": 1, "gas": None},
                               {"mineral": None, "gas": 5}, None)
        self.assertIsInstance(built.crew, tuple)
        self.assertIsInstance(built.courses, tuple)
        for name in ("rooms", "research", "capacities", "funds", "spend_caps"):
            with self.subTest(mapping=name), self.assertRaises(TypeError):
                getattr(built, name)["x"] = 1
        rooms["12"] = bridge()
        self.assertNotIn("12", built.rooms)
        unknown = {"mineral": None, "gas": None}
        unobserved = TrainingInputs((), {}, None, None, {}, unknown, unknown, 0)
        self.assertIsNone(unobserved.courses)
        self.assertIsNone(unobserved.research)

    def test_values_are_frozen(self):
        plan = plan_crew(inputs())
        for value in (eligible(), academy(), inputs(), plan.assignment, plan.decisions[0], plan):
            with self.subTest(type=type(value).__name__), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(value, dataclasses.fields(value)[0].name, None)

    def test_crew_observation_fatigue_and_design_must_be_whole_numbers(self):
        for value in (0, 1, 100, None):
            with self.subTest(value=value):
                self.assertEqual(member(fatigue=value).fatigue, value)
                self.assertEqual(member(design_id=value).design_id, value)
        for value in (-1, True, False, "5", 1.0):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "fatigue"):
                    member(fatigue=value)
                with self.assertRaisesRegex(ValueError, "design_id"):
                    member(design_id=value)

    def test_room_observation_validation(self):
        academy()  # control
        RoomObservation("12", None, None, None, None, None, None)
        academy(level=0, capacity=0, running=0)
        for changes, message in (
            ({"room_id": ""}, "room_id"), ({"room_id": 11}, "room_id"), ({"room_id": None}, "room_id"),
            ({"design_name": 5}, "design_name"), ({"room_type": 5}, "room_type"), ({"status": 1}, "status"),
            ({"level": -1}, "level"), ({"level": True}, "level"), ({"level": "2"}, "level"),
            ({"capacity": -1}, "capacity"), ({"capacity": 2.0}, "capacity"), ({"running": -1}, "running"),
            ({"running": False}, "running"),
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                academy(**changes)

    def test_role_assignment_validation(self):
        assignment = RoleAssignment({"repair": 1}, ["shields"], [2, 3])
        self.assertEqual((dict(assignment.roles), assignment.uncovered, assignment.stats_unknown),
                         ({"repair": 1}, ("shields",), (2, 3)))
        with self.assertRaises(TypeError):
            assignment.roles["weapons"] = 4
        for values, message in (
            (({"": 1}, (), ()), "role words"), (({3: 1}, (), ()), "role words"), (({"a": 0}, (), ()), "positive"),
            (({"a": True}, (), ()), "positive"), (({"a": "1"}, (), ()), "positive"),
            (({"a": 1, "b": 1}, (), ()), "distinct"), (({}, ("",), ()), "role words"), (({}, (4,), ()), "role words"),
            (({"a": 1}, ("a",), ()), "both covered and uncovered"), (({}, ("a", "a"), ()), "distinct"),
            (({}, (), (0,)), "positive"), (({}, (), (True,)), "positive"), (({}, (), (2, 2)), "distinct"),
        ):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, message):
                RoleAssignment(*values)

    def test_funds_and_caps_name_both_currencies(self):
        both = {"mineral": None, "gas": None}
        inputs(funds=both, spend_caps=both)  # control
        for field_name in ("funds", "spend_caps"):
            for value, message in (({}, "mineral"), ({"mineral": 1}, "gas"), ({"gas": 1}, "mineral"),
                                   ({"mineral": "1", "gas": 0}, "whole number"),
                                   ({"mineral": True, "gas": 0}, "whole number"),
                                   ({"mineral": 1.5, "gas": 0}, "whole number")):
                with self.subTest(field=field_name, value=value), self.assertRaisesRegex(ValueError, message):
                    inputs(**{field_name: value})
        self.assertEqual(dict(inputs(funds={"mineral": -5, "gas": 0, "starbux": 9}).funds),
                         {"mineral": -5, "gas": 0, "starbux": 9})

    def test_duplicate_crew_ids_are_refused(self):
        with self.assertRaisesRegex(ValueError, "distinct"):
            inputs([eligible(), member(10001, Weapon=3)])
        inputs([eligible(), member(10002, Weapon=3)])  # control


CADET, SUCCUBUS, VETERAN = 30001, 30002, 30003
RUNNING = TrainingSession(40001, END, "running")
UNCLAIMED = TrainingSession(40001, END, "ended_unclaimed")
FINISHED = TrainingSession(40001, END, "finished")
UNKNOWN = TrainingSession(None, None, "unknown")


def candidate(cid, design_id=CADET, session=IDLE):
    return PrestigeCandidate(cid, design_id, ingredient_eligible(session))


def recipe_pair(recipe):
    return tuple(sorted((recipe.first_design_id, recipe.second_design_id)))


class PrestigeTests(unittest.TestCase):
    # Verifies: S14-R3
    def test_values_are_frozen(self):
        for value in (candidate(10001), PrestigeRoute(SUCCUBUS, (CADET, CADET), 0, 0)):
            with self.subTest(type=type(value).__name__), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(value, dataclasses.fields(value)[0].name, None)

    def test_ingredient_eligibility_table(self):
        for session, expected in ((IDLE, True), (FINISHED, True), (RUNNING, False), (UNCLAIMED, False),
                                  (UNKNOWN, None), (None, None)):
            with self.subTest(session=session):
                self.assertIs(ingredient_eligible(session), expected)

    def test_targets_resolve_by_name_sorted_by_name_then_id(self):
        names = {30002: "Galactic Succubus", 30009: "Delish", 30004: "Galactic Succubus", 30001: "Synthetic Cadet",
                 30005: "galactic succubus"}
        self.assertEqual(prestige_targets(names, DEFAULT_CREW_POLICY.prestige_targets), (30009, 30002, 30004))
        self.assertEqual(prestige_targets(names, ()), ())
        self.assertEqual(prestige_targets({}, DEFAULT_CREW_POLICY.prestige_targets), ())

    def test_two_copy_recipe_counts_duplicates(self):
        recipe = PrestigeRecipe(CADET, CADET, SUCCUBUS)
        for roster, missing in (
            ([candidate(10001)], 1),
            ([candidate(10001), candidate(10002)], 0),
            ([candidate(10001), candidate(10002), candidate(10003)], 0),
            ([candidate(10001), candidate(10002, session=RUNNING)], 1),
            ([candidate(10001), candidate(10002, session=UNCLAIMED)], 1),
            ([candidate(10001), candidate(10002, session=FINISHED)], 0),
            ([], 2),
            ([candidate(10001, VETERAN), candidate(10002, VETERAN)], 2),
        ):
            with self.subTest(roster=roster):
                self.assertEqual(match_recipe(recipe, roster), PrestigeRoute(SUCCUBUS, (CADET, CADET), missing, 0))

    def test_unknown_eligibility_is_labelled(self):
        recipe = PrestigeRecipe(CADET, CADET, SUCCUBUS)
        self.assertEqual(match_recipe(recipe, [candidate(10001), candidate(10002, session=UNKNOWN)]),
                         PrestigeRoute(SUCCUBUS, (CADET, CADET), 1, 1))
        self.assertEqual(match_recipe(recipe, [candidate(10001, session=UNKNOWN), candidate(10002, session=UNKNOWN),
                                               candidate(10003, session=UNKNOWN)]),
                         PrestigeRoute(SUCCUBUS, (CADET, CADET), 2, 2))
        self.assertEqual(match_recipe(recipe, [candidate(10001, session=UNKNOWN)]),
                         PrestigeRoute(SUCCUBUS, (CADET, CADET), 2, 1))
        self.assertEqual(match_recipe(recipe, [candidate(10001), candidate(10002), candidate(10003, session=UNKNOWN)]),
                         PrestigeRoute(SUCCUBUS, (CADET, CADET), 0, 0))

    def test_distinct_ingredients(self):
        recipe = PrestigeRecipe(CADET, VETERAN, SUCCUBUS)
        self.assertEqual(match_recipe(recipe, [candidate(10001)]), PrestigeRoute(SUCCUBUS, (CADET, VETERAN), 1, 0))
        self.assertEqual(match_recipe(recipe, [candidate(10001), candidate(10002, VETERAN)]),
                         PrestigeRoute(SUCCUBUS, (CADET, VETERAN), 0, 0))
        self.assertEqual(match_recipe(recipe, [candidate(10001), candidate(10002)]),
                         PrestigeRoute(SUCCUBUS, (CADET, VETERAN), 1, 0))
        self.assertEqual(match_recipe(PrestigeRecipe(VETERAN, CADET, SUCCUBUS), [candidate(10001)]),
                         PrestigeRoute(SUCCUBUS, (CADET, VETERAN), 1, 0))

    def test_unobserved_roster_is_unknown(self):
        self.assertEqual(match_recipe(PrestigeRecipe(CADET, CADET, SUCCUBUS), None),
                         PrestigeRoute(SUCCUBUS, (CADET, CADET), None, None))

    def test_crew_of_unknown_design_are_possible_ingredients(self):
        two_copy, mixed = PrestigeRecipe(CADET, CADET, SUCCUBUS), PrestigeRecipe(CADET, VETERAN, SUCCUBUS)
        for recipe, roster, missing, unknown in (
            (two_copy, [candidate(10001, None)], 2, 1),
            (two_copy, [candidate(10001, None), candidate(10002, None), candidate(10003, None)], 2, 2),
            (two_copy, [candidate(10001), candidate(10002, None)], 1, 1),
            (two_copy, [candidate(10001), candidate(10002), candidate(10003, None)], 0, 0),
            (two_copy, [candidate(10001, None, session=UNKNOWN)], 2, 1),
            (two_copy, [candidate(10001, None, session=RUNNING)], 2, 0),
            (two_copy, [candidate(10001, None, session=UNCLAIMED)], 2, 0),
            (two_copy, [candidate(10001, session=UNKNOWN), candidate(10002, None)], 2, 2),
            (mixed, [candidate(10001, None)], 2, 1),
            (mixed, [candidate(10001, None), candidate(10002, None)], 2, 2),
            (mixed, [candidate(10001, session=UNKNOWN), candidate(10002, None)], 2, 2),
            (mixed, [candidate(10001, VETERAN, UNKNOWN), candidate(10002, VETERAN, UNKNOWN)], 2, 1),
            (mixed, [candidate(10001), candidate(10002, None)], 1, 1),
        ):
            with self.subTest(recipe=recipe, roster=roster):
                self.assertEqual(match_recipe(recipe, roster),
                                 PrestigeRoute(SUCCUBUS, recipe_pair(recipe), missing, unknown))

    def test_candidate_validation(self):
        PrestigeCandidate(1, None, None)  # control
        PrestigeCandidate(1, 1, False)
        for values, message in (
            ((0, 1, True), "character_id"), ((-1, 1, True), "character_id"), ((True, 1, True), "character_id"),
            (("1", 1, True), "character_id"), ((1, 0, True), "design_id"), ((1, -1, True), "design_id"),
            ((1, False, True), "design_id"), ((1, "30001", True), "design_id"), ((1, 1, 1), "eligible"),
            ((1, 1, 0), "eligible"), ((1, 1, "yes"), "eligible"),
        ):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, message):
                PrestigeCandidate(*values)

    def test_route_validation(self):
        PrestigeRoute(SUCCUBUS, (CADET, CADET), 0, 0)  # control
        PrestigeRoute(SUCCUBUS, (CADET, VETERAN), 2, 2)
        PrestigeRoute(SUCCUBUS, (CADET, CADET), None, None)
        for values, message in (
            ((0, (CADET, CADET), 0, 0), "target_design_id"), ((True, (CADET, CADET), 0, 0), "target_design_id"),
            ((SUCCUBUS, [CADET, CADET], 0, 0), "two"), ((SUCCUBUS, (CADET,), 0, 0), "two"),
            ((SUCCUBUS, (CADET, CADET, CADET), 0, 0), "two"), ((SUCCUBUS, (CADET, 0), 0, 0), "two"),
            ((SUCCUBUS, (CADET, True), 0, 0), "two"), ((SUCCUBUS, (CADET, "3"), 0, 0), "two"),
            ((SUCCUBUS, (CADET, CADET), -1, 0), "missing"), ((SUCCUBUS, (CADET, CADET), 3, 0), "missing"),
            ((SUCCUBUS, (CADET, CADET), True, 0), "missing"), ((SUCCUBUS, (CADET, CADET), 1, -1), "unknown"),
            ((SUCCUBUS, (CADET, CADET), 1, 2), "unknown"), ((SUCCUBUS, (CADET, CADET), 1, False), "unknown"),
            ((SUCCUBUS, (CADET, CADET), None, 0), "unknown"), ((SUCCUBUS, (CADET, CADET), 0, None), "unknown"),
        ):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, message):
                PrestigeRoute(*values)

    def test_other_targets_and_duplicate_recipes(self):
        recipes = {SUCCUBUS: (PrestigeRecipe(CADET, VETERAN, SUCCUBUS), PrestigeRecipe(VETERAN, CADET, SUCCUBUS),
                              PrestigeRecipe(CADET, CADET, VETERAN), PrestigeRecipe(CADET, CADET, SUCCUBUS))}
        routes = prestige_routes((SUCCUBUS,), recipes, [candidate(10001)])
        self.assertEqual(routes, (PrestigeRoute(SUCCUBUS, (CADET, VETERAN), 1, 0),
                                  PrestigeRoute(SUCCUBUS, (CADET, CADET), 1, 0)))

    def test_routes_follow_target_order(self):
        recipes = {SUCCUBUS: (PrestigeRecipe(CADET, CADET, SUCCUBUS),),
                   VETERAN: (PrestigeRecipe(CADET, SUCCUBUS, VETERAN),)}
        roster = [candidate(10001), candidate(10002, SUCCUBUS)]
        self.assertEqual(prestige_routes((VETERAN, SUCCUBUS), recipes, roster),
                         (PrestigeRoute(VETERAN, (CADET, SUCCUBUS), 0, 0),
                          PrestigeRoute(SUCCUBUS, (CADET, CADET), 1, 0)))
        self.assertEqual(prestige_routes((SUCCUBUS,), {}, roster), ())
        self.assertEqual(prestige_routes((SUCCUBUS,), recipes, None),
                         (PrestigeRoute(SUCCUBUS, (CADET, CADET), None, None),))


# --- run.py --crew-plan against FakePss ---------------------------------------------------------------------------

for _module in pkgutil.walk_packages(sdk.__path__, "sdk."):  # before any FakePss.install(): every clock is swapped
    importlib.import_module(_module.name)

import run  # noqa: E402 - after the sdk imports above
from sdk.journal import read_events  # noqa: E402
from tests.fakes.pss_server import FakePss  # noqa: E402
from tests.fakes.state import (  # noqa: E402
    FAKE_EPOCH,
    Account,
    Character,
    CharacterDesign,
    Research,
    ResearchDesign,
    Resources,
    Room,
    RoomDesign,
    TrainingDesign,
    World,
    stats,
)
from tests.fakes.state import TrainingSession as FakeSession  # noqa: E402
from tests.synthetic_fixtures import SYNTHETIC_DEVICE_KEY_IOS  # noqa: E402

HOUR = dt.timedelta(hours=1)
SALT_ENV = "CREW_PLAN_SALT"
SALT = "synthetic-crew-plan-salt-01"


def preview_world(*, prestige=True, characters=None, **options):
    """A strict, typed-resource world: Cora (Repair 20) in a level 2 Academy (capacity 2), a 500-mineral manual."""
    world = World(account=Account(), resources=Resources(mineral=1000, gas=0), typed_resource_items=True,
                  strict_training_rules=True, **options)
    world.room_designs = [RoomDesign("901", "Academy", "Training", level=2, capacity=2),
                          RoomDesign("101", "Bridge", "Bridge", level=1)]
    world.rooms = [Room("11", "901"), Room("12", "101")]
    world.research_designs = [ResearchDesign("61", "61", "Fitness 101", 1000)]
    world.researches = [Research("61")]
    world.character_designs = [CharacterDesign("31", "Synthetic Cadet"), CharacterDesign("32", "Galactic Succubus")]
    world.characters = characters if characters is not None else [
        Character("1001", "Cora", "31", "11", stats=stats(Repair=20))]
    world.training_designs = [TrainingDesign("41", "Study Expert Engineering Manual", "RepairImprovement", 2, 10, HOUR,
                                             price="mineral:500", rank=1, required_room_type="Academy",
                                             required_room_level=2, required_research_id="61")]
    if prestige:
        world.prestige_recipes = [("31", "31", "32")]
    return world


class CliCase(unittest.TestCase):
    """A scratch working directory with a one-account registry; ``fake`` answers every Pixel Starships request."""

    policy_id = "training-v2"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="tachikoma-crew-plan-")
        self.addCleanup(tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, cwd)
        self.directory = Path(tmp.name)
        self.registry = self.directory / "registry.json"
        self.write_registry()

    def write_registry(self, **account):
        entry = {"alias": "alpha", "objective": "grow", "policy_id": self.policy_id, "credentials_secret": "TWB_ALPHA",
                 **account}
        self.registry.write_text(json.dumps({"version": 1, "accounts": [entry]}), encoding="utf-8")

    def env(self, fake):
        return {"TWB_ALPHA": f"fleet|{SYNTHETIC_DEVICE_KEY_IOS}|{fake.state.account.refresh_token}|en",
                SALT_ENV: SALT}

    def crew_plan(self, fake, environ=None, registry=None, salt_env=SALT_ENV, **options):
        out, err = io.StringIO(), io.StringIO()
        with fake.install(), contextlib.redirect_stderr(err):
            code = run.run_crew_plan(str(registry or self.registry), salt_env, out=out,
                                     environ=self.env(fake) if environ is None else environ, **options)
        return code, out.getvalue().splitlines(), err.getvalue()

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["run.py", *argv]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = run.main()
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()


HEADER = re.compile(r"^Crew plan for account 'alpha' \(training-v2, observed \S+; spend journaled earlier this period "
                    r"is not counted\)$")
EXPECTED_LINES = [
    "  facility 11 Academy level 2: status Normal, training 0/2",
    "  research 61: Completed",
    "  crew 1001 repair: Study Expert Engineering Manual",
    "  role shields: skip role_uncovered",
    "  role weapons: skip role_uncovered",
    "  prestige Galactic Succubus <- Synthetic Cadet + Synthetic Cadet: missing 1",
]


class CrewPlanCommandTests(CliCase):
    # Verifies: S14-R3
    def test_prints_the_exact_lines(self):
        fake = FakePss(preview_world())
        code, lines, err = self.crew_plan(fake)
        self.assertEqual((code, err), (0, ""))
        self.assertRegex(lines[0], HEADER)
        self.assertEqual(lines[1:], EXPECTED_LINES)
        self.assertEqual((fake.violations, fake.unknown), ([], []))

    def test_read_only(self):
        fake = FakePss(preview_world())
        code, _, _ = self.crew_plan(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.mutations(), [])
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), ["registry.json"])

    def test_logs_in_through_the_ephemeral_device(self):
        fake = FakePss(preview_world())
        with patch.object(run, "Device", side_effect=AssertionError("run.Device must not be used")):
            code, lines, _ = self.crew_plan(fake)
        self.assertEqual(code, 0)
        self.assertIn("  crew 1001 repair: Study Expert Engineering Manual", lines)

    def test_deterministic_under_reply_permutation(self):
        characters = [Character("1002", "Tob", "31", "11", stats=stats(Repair=30)),
                      Character("1001", "Tia", "31", "11", stats=stats(Repair=30)),
                      Character("1003", "Una", "31", "12", stats=stats(Weapon=5))]
        outputs = []
        for seed in (1, 2, 3):
            with self.subTest(seed=seed):
                code, lines, _ = self.crew_plan(FakePss(preview_world(characters=characters), permute_seed=seed))
                self.assertEqual(code, 0)
                outputs.append(lines[1:])
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], outputs[2])
        self.assertIn("  crew 1001 repair: Study Expert Engineering Manual", outputs[0])
        self.assertIn("  crew 1002 shields: skip design_missing", outputs[0])
        self.assertIn("  crew 1003 weapons: skip not_in_training_room", outputs[0])
        self.assertIn("  prestige Galactic Succubus <- Synthetic Cadet + Synthetic Cadet: missing 0", outputs[0])

    def test_unknowns_are_labelled(self):
        characters = [Character("1001", "Cora", "31", "11", stats=stats(Repair=20),
                                omit_stats=("RepairImprovement",))]
        code, lines, _ = self.crew_plan(FakePss(preview_world(characters=characters, ship_omits_researches=True,
                                                              prestige=False)))
        self.assertEqual(code, 0)
        self.assertEqual(lines[1:], [
            "  facility 11 Academy level 2: status Normal, training 0/2",
            "  research 61: unknown",
            "  role repair: skip role_uncovered",
            "  role shields: skip role_uncovered",
            "  role weapons: skip role_uncovered",
            "  crew 1001: skip stats_unknown, eligibility unknown",
            "  prestige Galactic Succubus: recipes unavailable",
        ])

    def test_injected_clock_sets_the_observation_time_and_the_training_phase(self):
        session = FakeSession("41", FAKE_EPOCH - HOUR, FAKE_EPOCH + 2 * HOUR)
        characters = [Character("1001", "Cora", "31", "11", stats=stats(Repair=20), session=session)]
        code, lines, _ = self.crew_plan(FakePss(preview_world(characters=characters)))
        self.assertEqual(code, 0)
        self.assertIn("  crew 1001 repair: skip training_in_progress", lines)  # control: the fake's clock
        self.assertIn("  facility 11 Academy level 2: status Normal, training 1/2", lines)
        later = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
        fake = FakePss(preview_world(characters=characters))
        code, lines, _ = self.crew_plan(fake, clock=lambda: later)
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("Crew plan for account 'alpha' (training-v2, observed "
                                            "2026-11-20T15:00:00+00:00; "), lines[0])
        self.assertIn("  crew 1001 repair: skip unclaimed_course", lines)
        self.assertIn("  facility 11 Academy level 2: status Normal, training 0/2", lines)
        self.assertEqual(fake.mutations(), [])

    def test_a_failed_recipe_read_is_logged_without_identifiers(self):
        fake = FakePss(preview_world())
        secret = fake.state.account.refresh_token
        failure = RuntimeError(f"read failed for user {fake.state.account.user_id} token {secret}")
        with patch.object(run.Client, "prestigeCharacterTo", side_effect=failure), \
                self.assertLogs(level="WARNING") as logs:
            code, lines, _ = self.crew_plan(fake)
        self.assertEqual(code, 0)
        self.assertIn("  prestige Galactic Succubus: recipes unavailable", lines)
        self.assertIn("  crew 1001 repair: Study Expert Engineering Manual", lines)
        warnings = [record.getMessage() for record in logs.records if "Prestige" in record.getMessage()]
        self.assertEqual(warnings, ["Crew plan: the Prestige recipe read for character design 32 failed "
                                    "(RuntimeError); recipes unavailable."])
        for record in logs.records:
            self.assertNotIn(secret, record.getMessage())
            self.assertNotIn(fake.state.account.user_id, record.getMessage())
            self.assertNotIn("alpha", record.getMessage())

    def test_exit_codes(self):
        fake = FakePss(preview_world())
        self.assertEqual(self.crew_plan(fake, registry=self.directory / "missing.json")[0], 2)
        self.assertEqual(self.crew_plan(fake, environ={"TWB_ALPHA": self.env(fake)["TWB_ALPHA"]})[0], 2)
        code, lines, _ = self.crew_plan(fake, environ={SALT_ENV: SALT})
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("Crew plan for account 'alpha': error: "), lines)
        self.write_registry(policy_id="training-v1")
        code, lines, _ = self.crew_plan(FakePss(preview_world()))
        self.assertEqual((code, lines), (0, ["Crew plan for account 'alpha': policy training-v1 plans no crew roles"]))
        self.write_registry(enabled=False)
        self.assertEqual(self.crew_plan(FakePss(preview_world())), (0, [], ""))

    def test_identity_mismatch_is_refused(self):
        self.write_registry(expected_game_identity="gid1-" + "0" * 32)
        fake = FakePss(preview_world())
        code, lines, _ = self.crew_plan(fake)
        self.assertEqual(code, 1)
        self.assertTrue(lines[0].startswith("Crew plan for account 'alpha': error: "), lines)
        self.assertEqual(fake.mutations(), [])

    def test_argument_conflicts(self):
        conflicts = (("--fleet-journal-dir", "j"), ("--concurrent", "2"), ("--fleet-dry-run",), ("--fleet-execute",),
                     ("--auth-file", "a"), ("--capture-reads",), ("--baseline",), ("--output", "o"),
                     ("--authorization", "a"), ("--mode", "full"), ("--plan-only",), ("--smtp-email", "x"),
                     ("--report", "fleet"))
        for extra in conflicts:
            with self.subTest(extra=extra):
                code, _, err = self.main("--crew-plan", "--registry", str(self.registry), *extra)
                self.assertEqual(code, 2)
                self.assertIn("--crew-plan", err)
        code, _, err = self.main("--crew-plan")
        self.assertEqual(code, 2)
        self.assertIn("--crew-plan needs --registry PATH", err)

    def test_main_dispatches_to_the_crew_plan(self):
        with patch.object(run, "run_crew_plan", return_value=0) as crew_plan:
            code, _, _ = self.main("--crew-plan", "--registry", "r.json", "--identity-salt-env", SALT_ENV)
        self.assertEqual(code, 0)
        crew_plan.assert_called_once_with("r.json", SALT_ENV)


class PreviewParityTests(CliCase):
    # Verifies: S14-R1, S14-R3
    def dry_run(self, fake):
        out = io.StringIO()
        with fake.install():
            code = run.run_fleet_dry_run(str(self.registry), "journals", None, SALT_ENV, out=out,
                                         environ=self.env(fake))
        planned = [(event.params.get("character_id"), event.params.get("training_design_id"))
                   for event in read_events(self.directory / "journals" / "alpha.log")
                   if event.action_id == "add_training"]
        return code, planned

    def test_dry_run_and_crew_plan_choose_the_same_start(self):
        fake = FakePss(preview_world())
        code, lines, _ = self.crew_plan(fake)
        self.assertEqual(code, 0)
        self.assertIn("  crew 1001 repair: Study Expert Engineering Manual", lines)
        self.assertEqual(self.dry_run(fake), (0, [("1001", "41")]))
        self.assertEqual(fake.mutations(), [])

    def test_a_failed_precondition_is_the_same_skip_in_both_previews(self):
        fake = FakePss(preview_world())
        with patch("sdk.policy.training._unmet_preconditions", return_value="crew member busy"):
            code, lines, _ = self.crew_plan(fake)
            self.assertEqual(code, 0)
            self.assertIn("  crew 1001 repair: skip training_in_progress", lines)
            self.assertNotIn("  crew 1001 repair: Study Expert Engineering Manual", lines)
            self.assertEqual(self.dry_run(fake), (0, []))
        self.assertEqual(fake.mutations(), [])

    def test_both_plan_nothing_when_research_is_unknown(self):
        fake = FakePss(preview_world(ship_omits_researches=True))
        code, lines, _ = self.crew_plan(fake)
        self.assertEqual(code, 0)
        self.assertIn("  crew 1001 repair: skip research_unknown, eligibility unknown", lines)
        self.assertEqual(self.dry_run(fake), (0, []))



# Regression for the run-record race exposed by training-v2 overlap runs: two concurrent runs that started in the same
# second chose the same name (an exists() check, then os.replace), so the second record silently replaced the first.
class RunRecordNoReplaceTests(unittest.TestCase):
    STARTED_AT = "2026-01-02T03:04:05+00:00"

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.directory = Path(scratch.name)

    def records(self):
        return {path.name: json.loads(path.read_text(encoding="utf-8")) for path in self.directory.glob("*.json")}

    def test_same_second_writes_keep_both_records(self):
        first = run._write_run_record(self.directory, self.STARTED_AT, {"run": "first"})
        second = run._write_run_record(self.directory, self.STARTED_AT, {"run": "second"})
        self.assertEqual((first.name, second.name), ("20260102T030405Z.json", "20260102T030405Z-2.json"))
        self.assertEqual(self.records(), {first.name: {"run": "first"}, second.name: {"run": "second"}})

    def test_racing_writer_that_sees_no_record_does_not_replace_one(self):
        first = run._write_run_record(self.directory, self.STARTED_AT, {"run": "first"})
        with patch.object(Path, "exists", return_value=False):
            second = run._write_run_record(self.directory, self.STARTED_AT, {"run": "second"})
        self.assertNotEqual(first, second)
        self.assertEqual(self.records(), {first.name: {"run": "first"}, second.name: {"run": "second"}})

    def test_no_temporary_files_are_left_behind(self):
        run._write_run_record(self.directory, self.STARTED_AT, {"run": "first"})
        with patch.object(Path, "exists", return_value=False):
            run._write_run_record(self.directory, self.STARTED_AT, {"run": "second"})
        self.assertEqual(sorted(path.name for path in self.directory.glob(".run-*.tmp")), [])
        self.assertEqual(len(list(self.directory.iterdir())), 2)

    def test_without_hard_links_an_exclusive_create_still_keeps_both_records(self):
        refused = PermissionError(1, "Operation not permitted")
        with patch.object(run.os, "link", side_effect=refused), patch.object(Path, "exists", return_value=False):
            first = run._write_run_record(self.directory, self.STARTED_AT, {"run": "first"})
            second = run._write_run_record(self.directory, self.STARTED_AT, {"run": "second"})
        self.assertEqual(self.records(), {first.name: {"run": "first"}, second.name: {"run": "second"}})
        self.assertEqual(len(list(self.directory.iterdir())), 2)


if __name__ == "__main__":
    unittest.main()
