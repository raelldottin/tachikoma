"""Tests for scripts/training_workflow_benchmark.py (SPEC-14, S14-R7): the frozen workflow fixtures, the opt-in FakePss
extensions they rely on, the oracle (comparator, normalizer, hard-violation detectors, witnesses), the paired
training-v1 controls on the unmodified code, and the red gate.

The checked-in training-v2 traces run in WorkflowGateTests (both suites at every seed, the controls, and a
determinism child under another PYTHONHASHSEED); the gate's own logic is tested on synthetic suites (GateTests). The
seeded defects (S14-AC7) run in SeededDefectRunTests and through ``--defects`` in DefectsCliTests. The full gate is
also ``python scripts/training_workflow_benchmark.py --gate``. No network: every Pixel Starships request goes to
FakePss.
"""

import contextlib
import copy
import datetime
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import xmltodict

from scripts import training_workflow_benchmark as wb
from sdk.journal import OUTCOMES
from sdk.state.client_provider import _container_records, _resource_from_items
from tests.fakes import pss_server
from tests.fakes.pss_server import ROUTES, FakePss, FakePssFleet, SimulatedCrash, _Reject
from tests.fakes.state import (
    Account,
    Character,
    CharacterDesign,
    Research,
    ResearchDesign,
    Resources,
    Room,
    RoomDesign,
    TrainingDesign,
    TrainingSession,
    World,
    default_world,
    fmt,
    stats,
)

BASE = "https://api.pixelstarships.com"
ADD = "/TrainingService/AddTraining"
REQUIRED = wb.load_suite(wb.FIXTURES["required"])
HOLDOUT = wb.load_suite(wb.FIXTURES["holdout"])
CODES = REQUIRED["vocabulary"]["skip_codes"]
CONTROL_CODES = sorted(set(CODES) | set(REQUIRED["vocabulary"]["control_skip_codes"]))
HOUR = datetime.timedelta(hours=1)


def case(suite, case_id):
    return next(item for item in suite["cases"] if item["id"] == case_id)


def scenario(case_id, scenario_id, suite=REQUIRED):
    return next(item for item in case(suite, case_id)["scenarios"] if item["id"] == scenario_id)


def paid_world(mineral=500, price="mineral:500", **world_options):
    """A strict, typed-resource world: an idle repair crew in a level 2 Academy (capacity 1) and a priced course."""
    world = World(account=Account(credits=world_options.pop("credits", 0)), resources=Resources(mineral=mineral, gas=0),
                  typed_resource_items=True, strict_training_rules=True, **world_options)
    world.room_designs = [RoomDesign("901", "Academy", "Training", level=2, capacity=1),
                          RoomDesign("101", "Bridge", "Bridge", level=1)]
    world.rooms = [Room("11", "901"), Room("12", "101")]
    world.research_designs = [ResearchDesign("61", "61", "Fitness 101", 1000)]
    world.researches = [Research("61")]
    world.character_designs = [CharacterDesign("31", "Synthetic Cadet"), CharacterDesign("32", "Galactic Succubus")]
    world.characters = [Character("1001", "Fixer", "31", "11", stats=stats(Repair=30)),
                        Character("1002", "Second", "31", "11", stats=stats(Repair=10))]
    world.training_designs = [TrainingDesign("41", "Manual", "RepairImprovement", 2, 10, HOUR, price=price, rank=1,
                                             required_room_type="Academy", required_room_level=2,
                                             required_research_id="61"),
                              TrainingDesign("42", "Drills", "WeaponImprovement", 2, 10, HOUR)]
    return world


def login(session, fake):
    reply = session.post(f"{BASE}/UserService/DeviceLogin17", json={"RefreshToken": fake.state.account.refresh_token})
    return xmltodict.parse(reply.text)["UserService"]["UserLogin"]["@accessToken"]


def add_training(session, fake, token, character="1001", design="41"):
    query = urllib.parse.urlencode({"trainingDesignId": design, "characterId": character,
                                    "trainingStartDate": fmt(fake.now), "accessToken": token})
    return session.post(f"{BASE}{ADD}?{query}")


def read(session, path, token=None, **params):
    if token is not None:
        params["accessToken"] = token
    return session.get(f"{BASE}{path}?{urllib.parse.urlencode(params)}").text


@contextlib.contextmanager
def scratch_ctx(case_id, scenario_id, seed=1401, suite=REQUIRED):
    """A started scenario (worlds, registry, authorization) in a scratch directory; nothing has run yet."""
    with tempfile.TemporaryDirectory() as directory:
        yield wb.start_scenario(suite["suite"], case_id, scenario(case_id, scenario_id, suite), suite["catalog"],
                                seed, None, Path(directory))


def ledger_start(ctx, crew="fixer", course="eng_manual", *, applied=True, mineral=-500, starbux=0, minutes=60,
                 thread="MainThread", session_course=None, exchange=5):
    cid, did = ctx.idmap.id("crew", crew), ctx.idmap.id("course", course)
    end = f"2026-11-26T{minutes // 60:02d}:{minutes % 60:02d}:00"
    session = [cid, ctx.idmap.id("course", session_course or course), "2026-11-26T00:00:00", end] if applied else None
    return {"seq": 1, "thread": thread, "method": "POST", "path": ADD,
            "params": {"characterId": cid, "trainingDesignId": did, "trainingStartDate": "2026-11-26T00:00:00"},
            "applied": applied, "refusal": None if applied else "Not enough minerals",
            "delta": {"mineral": mineral if applied else 0, "gas": 0, "starbux": starbux}, "session": session,
            "exchange": exchange}


def roster_read(index, *, thread="MainThread", status=200, error=None, roster=None):
    return {"index": index, "method": "GET", "path": "/CharacterService/ListAllCharactersOfUser", "status": status,
            "error": error, "thread": thread, "roster": roster}


def showing(ctx, crew="fixer", course="eng_manual", *, running=True):
    """What a roster read showed (FakePss Exchange.roster): the crew training the course, its timer running or not."""
    return {ctx.idmap.id("crew", crew): {"course": ctx.idmap.id("course", course) if course else "0",
                                         "running": running}}


def journal_start(ctx, crew="fixer", course="eng_manual", *, outcome="verified", minerals=-500):
    return {"alias": "alpha", "action": "add_training", "outcome": outcome, "attempted": True, "dry_run": False,
            "reason": "postcondition", "params": {"character_id": ctx.idmap.id("crew", crew),
                                                 "training_design_id": ctx.idmap.id("course", course)},
            "cost": {"minerals_delta": minerals, "gas_delta": 0, "starbux_delta": None}, "evidence": {}}


def raw_trace(*, ledger=(), delta=None, started=None, journal=(), stdout="", stderr="", exit_code=0, records=(),
              alias="alpha", outputs=None, exchanges=(), earlier=(), store=False):
    raw = {"exit": exit_code, "stdout": stdout, "stderr": stderr, "records": list(records),
           "accounts": {alias: {"ledger": list(ledger), "earlier_ledger": list(earlier), "exchanges": list(exchanges),
                                "requests": 9, "violations": [],
                                "delta": delta or {"mineral": 0, "gas": 0, "starbux": 0}, "started": started or {},
                                "journal": list(journal), "store": store}}}
    if outputs is not None:
        raw["dispatch_outputs"] = outputs
    return raw


def exact_500_trace(ctx, **changes):
    """The exact-500 execute as a correct workflow leaves it: one applied start (request 5), then a roster read."""
    cid, did = ctx.idmap.id("crew", "fixer"), ctx.idmap.id("course", "eng_manual")
    options = {"ledger": [ledger_start(ctx)], "delta": {"mineral": -500, "gas": 0, "starbux": 0},
               "started": {cid: (did, "2026-11-26T00:00:00", "2026-11-26T01:00:00")}, "journal": [journal_start(ctx)],
               "exchanges": [roster_read(3, roster=showing(ctx, course=None, running=False)),
                             roster_read(6, roster=showing(ctx))]}
    options.update(changes)
    return raw_trace(**options)


# Verifies: S14-R7
class FixtureSchemaTests(unittest.TestCase):
    def test_required_manifest_is_frozen_across_three_seeds(self):
        self.assertEqual(REQUIRED["cases_declared"], list(wb.REQUIRED_IDS))
        self.assertEqual([item["id"] for item in REQUIRED["cases"]], list(wb.REQUIRED_IDS))
        self.assertEqual(len(REQUIRED["seeds"]), 3)

    def test_holdout_is_declared_with_its_own_pinned_ids_and_seeds(self):
        self.assertEqual(HOLDOUT["cases_declared"], list(wb.HOLDOUT_IDS))
        self.assertEqual([item["id"] for item in HOLDOUT["cases"]], list(wb.HOLDOUT_IDS))
        self.assertEqual((REQUIRED["seeds"], HOLDOUT["seeds"]), (list(wb.REQUIRED_SEEDS), list(wb.HOLDOUT_SEEDS)))
        self.assertFalse(set(HOLDOUT["cases_declared"]) & set(wb.REQUIRED_IDS))
        self.assertFalse(set(HOLDOUT["seeds"]) & set(REQUIRED["seeds"]))
        self.assertEqual(sorted(wb.load_suites("all")), ["holdout", "required"])

    def test_fixture_files_match_their_pinned_digests(self):
        """Tripwire: a fixture edit fails here until an approved oracle change also updates FIXTURE_SHA256."""
        for name, path in wb.FIXTURES.items():
            with self.subTest(suite=name):
                self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), wb.FIXTURE_SHA256[name])

    def test_every_executing_training_v2_scenario_provisions_its_store_first(self):
        """The S14 safety invariant: a virgin store is provisioned explicitly before the first execution; the one
        exception is the scenario that proves an execute without it refuses (state_missing) and creates nothing."""
        for suite in (REQUIRED, HOLDOUT):
            for item in suite["cases"]:
                for entry in item["scenarios"]:
                    kinds = [step["kind"] for step in entry["steps"]]
                    if not {"execute", "overlap"} & set(kinds):
                        continue
                    with self.subTest(case=item["id"], scenario=entry["id"]):
                        codes = [skip["code"] for expected in entry["expected"].values()
                                 for skip in expected.get("skips", ())]
                        if kinds[0] != "provision":
                            self.assertIn("state_missing", codes)
                            self.assertNotIn("provision", kinds)
                        else:
                            self.assertEqual(entry["expected"]["provision"]["store"],
                                             {account["alias"]: "present" for account in entry["accounts"]})

    def test_every_case_has_a_control_a_red_cause_and_declared_witnesses(self):
        for suite in (REQUIRED, HOLDOUT):
            for item in suite["cases"]:
                with self.subTest(case=item["id"]):
                    self.assertEqual(item["control"]["policy_id"], "training-v1")
                    self.assertTrue(item["red"]["expected_cause"])
                    for entry in [*item["scenarios"], *item["control"]["scenarios"]]:
                        needed = set().union(*(wb._needed_witnesses(step) for step in entry["steps"]))
                        self.assertLessEqual(needed, set(entry["witnesses"]), entry["id"])

    def test_vocabulary_outcomes_are_the_journal_outcomes(self):
        for suite in (REQUIRED, HOLDOUT):
            self.assertEqual(set(suite["vocabulary"]["outcomes"]), set(OUTCOMES))
            self.assertLessEqual({"room_capacity", "cap_exceeded", "stopped", "fatigued", "tp_cap_reached",
                                  "action_cap_reached", "state_missing", "intent_not_persisted"},
                                 set(suite["vocabulary"]["skip_codes"]))

    def test_the_new_coverage_asserts_its_codes(self):
        """Findings of the first review: these codes and pre-states must each be asserted by a required trace."""
        asserted = {skip["code"] for item in REQUIRED["cases"] for entry in item["scenarios"]
                    for expected in entry["expected"].values() for skip in expected.get("skips", ())}
        self.assertLessEqual({"room_capacity", "cap_exceeded", "stopped", "fatigued", "tp_cap_reached",
                              "action_cap_reached", "state_missing", "unresolved_attempt", "intent_not_persisted"},
                             asserted)
        kinds = {step["kind"] for item in REQUIRED["cases"] for entry in item["scenarios"] for step in entry["steps"]}
        self.assertLessEqual({"provision", "resolve"}, kinds)

    def test_every_later_execute_follows_a_fresh_dry_run(self):
        """Round-2 M4: an execute after an earlier execute or resolve runs right after its own dry run (world edits
        aside), so the traces hold whether "a matching recent dry run" means SPEC-05's same registry and policy
        within 24 h or a dry run that planned this start; the reading is declared too."""
        for suite in (REQUIRED, HOLDOUT):
            self.assertTrue(any(item.startswith("I15 ") and "fresh dry-run" in item
                                for item in suite["authored_from"]["interpretations"]))
            for item in suite["cases"]:
                for entry in item["scenarios"]:  # training-v2 traces; the v1 controls keep v1's own sequence
                    steps = [step for step in entry["steps"] if step["kind"] != "mutate"]
                    for index, step in enumerate(steps):
                        earlier = {s["kind"] for s in steps[:index]}
                        if step["kind"] in ("execute", "overlap") and earlier & {"execute", "resolve"}:
                            with self.subTest(case=item["id"], scenario=entry["id"], step=step["id"]):
                                self.assertEqual(steps[index - 1]["kind"], "dry-run")
                                self.assertEqual(entry["expected"][steps[index - 1]["id"]]["exit"], 0)

    def test_room_level_and_locked_readings_are_declared(self):
        """Round-2 m4: course room level is a minimum; a locked account is skipped, not stopped (no halt share)."""
        for suite in (REQUIRED, HOLDOUT):
            readings = suite["authored_from"]["interpretations"]
            self.assertTrue(any(item.startswith("I16 ") and "minimum" in item for item in readings))
            self.assertTrue(any(item.startswith("I17 ") and "halt" in item and "locked" in item for item in readings))

    def test_a_settle_write_failure_after_an_applied_start_is_its_own_scenario(self):
        """Round-2 M3: the post-send settle write fails (not a lost reply): the store stays, the start applied once,
        and the next execute (after a fresh dry run) sends nothing and reports unresolved_attempt."""
        item = case(REQUIRED, "persisted-intent-restart")
        ids = [entry["id"] for entry in item["scenarios"]]
        self.assertIn("lost-reply-failed-post-read", ids)
        self.assertNotIn("settlement-failure", ids)
        entry = scenario("persisted-intent-restart", "settle-write-fails")
        steps = {step["id"]: step for step in entry["steps"]}
        self.assertTrue(steps["exec1"]["pre"]["fail_settle_write"])
        self.assertIn("exec1:settle_write_failed", entry["witnesses"])
        exec1, exec2 = entry["expected"]["exec1"], entry["expected"]["exec2"]
        self.assertEqual(exec1["store"], {"alpha": "present"})
        self.assertEqual(exec1["mutations"][wb.ADD_TRAINING], [{"alias": "alpha", "crew": "fixer",
                                                                "course": "eng_manual"}])
        self.assertEqual((exec1["applied"], exec1["timer"]["fixer"]["course"]),
                         ({wb.ADD_TRAINING: 1}, "eng_manual"))
        self.assertTrue(steps["exec2"]["blocked_after_unresolved"])
        self.assertEqual((exec2["mutations"][wb.ADD_TRAINING], exec2["store"]), ([], {"alpha": "present"}))
        self.assertIn({"alias": "alpha", "code": "unresolved_attempt"}, exec2["skips"])

    def test_overlap_barriers_and_holds_wait_up_to_30_seconds(self):
        """Round-2 m5: the timeouts are upper bounds only; under CI load a short one would fake a missing witness."""
        for suite in (REQUIRED, HOLDOUT):
            for item in suite["cases"]:
                for entry in [*item["scenarios"], *item["control"]["scenarios"]]:
                    for step in entry["steps"]:
                        if step["kind"] == "overlap":
                            self.assertGreaterEqual(min(step["barrier"]["timeout_s"], step["hold"]["timeout_s"]), 30)

    def test_a_missing_stat_is_never_frozen_as_the_only_outcome(self):
        """Holdout I2: S14-R1 allows an estimate from design and level for a missing stat, so una is neither frozen
        as stats_unknown nor ruled out of the dry run; obi's observed 0 stays frozen (planned, never unknown)."""
        entry = scenario("observed-zero-stat", "missing-versus-zero", HOLDOUT)
        skips = [skip for expected in entry["expected"].values() for skip in expected.get("skips", ())]
        self.assertFalse([skip for skip in skips if skip.get("crew") == "una"])
        self.assertIn({"crew": "obi", "role": "shields", "course": "science_primer"},
                      entry["expected"]["plan"]["crew_plan"])
        planned = entry["expected"]["dry"]["journal_planned"]
        self.assertEqual([sorted(alt["crew"] for alt in row["any_of"]) for row in planned], [["obi", "una"]])


# Verifies: S14-R7
class FixtureRejectionTests(unittest.TestCase):
    def rejects(self, data, message):
        with self.assertRaises(wb.FixtureError) as raised:
            wb.validate_suite(data)
        self.assertIn(message, str(raised.exception))

    def test_empty_suite(self):
        data = copy.deepcopy(REQUIRED)
        data["cases"] = []
        self.rejects(data, "empty suite")

    def test_missing_case(self):
        data = copy.deepcopy(REQUIRED)
        data["cases"].pop()
        self.rejects(data, "missing ['store-stop-controls']")

    def test_duplicate_case(self):
        data = copy.deepcopy(REQUIRED)
        data["cases"].append(copy.deepcopy(data["cases"][0]))
        self.rejects(data, "duplicate case id")

    def test_undeclared_holdout_case_and_reused_required_id(self):
        data = copy.deepcopy(HOLDOUT)
        data["cases"][0]["id"] = "role-coverage"
        data["cases_declared"][0] = "role-coverage"
        self.rejects(data, "holdout reuses required case ids")

    def test_unknown_label(self):
        data = copy.deepcopy(REQUIRED)
        data["cases"][0]["scenarios"][0]["accounts"][0]["world"]["crew"][0]["design"] = "no-such-design"
        self.rejects(data, "unknown character design label")

    def test_unknown_skip_code(self):
        data = copy.deepcopy(REQUIRED)
        case(data, "paid-boundary")["scenarios"][1]["expected"]["exec"]["skips"][0]["code"] = "too_poor"
        self.rejects(data, "unknown skip code 'too_poor'")

    def test_expected_step_that_does_not_exist(self):
        data = copy.deepcopy(REQUIRED)
        data["cases"][0]["scenarios"][0]["expected"]["ghost"] = {"exit": 0, "other_mutations": 0}
        self.rejects(data, "expected names step 'ghost'")

    def test_fault_without_a_declared_witness(self):
        data = copy.deepcopy(REQUIRED)
        entry = case(data, "reconciliation")["scenarios"][-1]
        entry["witnesses"] = []
        self.rejects(data, "needs witnesses ['exec1:fault:0']")

    def test_threads_overlap_without_concurrent_sessions(self):
        data = copy.deepcopy(REQUIRED)
        del case(data, "overlapping-dispatch")["scenarios"][0]["accounts"][0]["world"]["concurrent_sessions"]
        self.rejects(data, "a threads overlap needs concurrent_sessions")

    def test_declared_interpretations_are_required_and_distinct(self):
        data = copy.deepcopy(REQUIRED)
        data["authored_from"] = data["authored_from"]["text"]
        self.rejects(data, "authored_from.text is required")
        data = copy.deepcopy(REQUIRED)
        data["authored_from"]["interpretations"].append(data["authored_from"]["interpretations"][0])
        self.rejects(data, "authored_from.interpretations")

    def test_a_holdout_case_cannot_leave_with_its_declaration(self):
        data = copy.deepcopy(HOLDOUT)
        data["cases"].pop()
        data["cases_declared"].pop()
        self.rejects(data, "holdout manifest must be exactly the pinned")
        data = copy.deepcopy(HOLDOUT)
        data["seeds"] = [2401, 2402, 2404]
        self.rejects(data, "holdout seeds must be the pinned")

    def test_the_required_manifest_is_exactly_the_pinned_ids_in_their_order(self):
        """QA survivor H18: a required case cannot leave together with its declaration, none can be added, and the
        manifest order is pinned too (it is the report order; reordering is a fixture edit like any other)."""
        data = copy.deepcopy(REQUIRED)
        dropped = data["cases"].pop(2)
        data["cases_declared"].remove(dropped["id"])
        self.rejects(data, "required manifest must be exactly")
        self.rejects(data, f"missing ['{dropped['id']}']")
        data = copy.deepcopy(REQUIRED)
        extra = copy.deepcopy(data["cases"][0])
        extra["id"] = "extra-required-case"
        data["cases"].append(extra)
        data["cases_declared"].append(extra["id"])
        self.rejects(data, "unexpected ['extra-required-case']")
        data = copy.deepcopy(REQUIRED)
        data["cases_declared"].reverse()
        self.rejects(data, "required manifest must be exactly")
        self.rejects(data, "out of order")

    def test_unknown_pre_state_and_store_value(self):
        data = copy.deepcopy(REQUIRED)
        entry = case(data, "store-stop-controls")["scenarios"][0]
        next(step for step in entry["steps"] if step["kind"] == "execute")["pre"] = {"readonly_state": True}
        self.rejects(data, "unknown pre-state keys ['readonly_state']")
        data = copy.deepcopy(REQUIRED)
        entry = case(data, "paid-boundary")["scenarios"][0]
        entry["expected"]["provision"]["store"] = {"alpha": "maybe"}
        self.rejects(data, "store.alpha must be 'present' or 'absent'")

    def test_unknown_step_kind(self):
        """QA round 3 M13: a step kind outside STEP_KINDS (a typo) is a fixture error, never a silently empty step."""
        data = copy.deepcopy(REQUIRED)
        data["cases"][0]["scenarios"][0]["steps"][0]["kind"] = "bogus"
        self.rejects(data, "has unknown kind 'bogus'")

    def test_unknown_enum_like_step_and_world_values(self):
        """Siblings of M13: an overlap mode, a room status, a research state, a registry key or a registry_variant
        account outside what the harness reads is a fixture error, not a silently different world."""
        def edited(edit):
            data = copy.deepcopy(REQUIRED)
            edit(data)
            return data

        def overlap_step(data):
            entry = case(data, "overlapping-dispatch")["scenarios"][0]
            return next(step for step in entry["steps"] if step["kind"] == "overlap")

        first = REQUIRED["cases"][0]["scenarios"][0]
        alias, room = first["accounts"][0]["alias"], first["accounts"][0]["world"]["rooms"][0]["label"]

        def world(data):
            return data["cases"][0]["scenarios"][0]["accounts"][0]["world"]

        def step0(data):
            return data["cases"][0]["scenarios"][0]["steps"][0]

        self.rejects(edited(lambda d: overlap_step(d).update(mode="process")), "has unknown mode 'process'")
        self.rejects(edited(lambda d: step0(d).update(mode="threads")), "has unknown mode 'threads'")
        self.rejects(edited(lambda d: world(d)["rooms"][0].update(status="Broken")), "has unknown status 'Broken'")
        self.rejects(edited(lambda d: world(d)["researches"][0].update(state="Done")), "has unknown state 'Done'")
        self.rejects(edited(lambda d: step0(d).update(set=[{"room": room, "status": "upgrading"}])),
                     "sets unknown room status 'upgrading'")
        self.rejects(edited(lambda d: d["cases"][0]["scenarios"][0]["accounts"][0]["registry"].update(policy="x")),
                     "unknown registry keys ['policy']")
        self.rejects(edited(lambda d: step0(d).update(registry_variant={"zulu": {"enabled": False}})),
                     "registry_variant names unknown account or keys: 'zulu'")
        self.rejects(edited(lambda d: step0(d).update(registry_variant={alias: {"enable": False}})),
                     f"registry_variant names unknown account or keys: '{alias}'")

    def test_a_course_room_type_names_a_room_design(self):
        """Sibling of M38: FakePss strict rules compare a course's room_type with the crew's room design name, so a
        room_type that names no room design (a label, a room type, a typo) is a fixture error."""
        data = copy.deepcopy(REQUIRED)
        data["catalog"]["training_designs"]["eng_manual"]["room_type"] = "Training"
        self.rejects(data, "training design eng_manual: room_type 'Training' names no room design")
        data = copy.deepcopy(REQUIRED)
        entry = data["cases"][0]["scenarios"][0]
        entry["course_overrides"] = {entry["courses"][0]: {"room_type": "academy_l2"}}
        self.rejects(data, f"course_overrides.{entry['courses'][0]}: room_type 'academy_l2' names no room design")

    def test_missing_control(self):
        data = copy.deepcopy(REQUIRED)
        del data["cases"][3]["control"]
        self.rejects(data, "training-v1 control")

    def test_main_exits_2_on_a_broken_fixture(self):
        data = copy.deepcopy(REQUIRED)
        data["cases"].append(copy.deepcopy(data["cases"][0]))
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "broken.json")
            Path(path).write_text(json.dumps(data), encoding="utf-8")
            err = io.StringIO()
            with patch.dict(wb.FIXTURES, {"required": path}), contextlib.redirect_stderr(err):
                code = wb.main(["--report-only", "--suite", "required"])
        self.assertEqual(code, wb.EXIT_ERROR)
        self.assertIn("fixture error: duplicate case id", err.getvalue())


# Verifies: S14-R7
class SeedTransformTests(unittest.TestCase):
    def test_relabelling_keeps_order_and_length_for_every_seed(self):
        bases = [1, 9, 10001, 10002, 20001, 99999]
        for seed in [*REQUIRED["seeds"], *HOLDOUT["seeds"]]:
            ids = [wb.seeded_id(base, seed) for base in bases]
            self.assertEqual(len({len(value) for value in ids}), 1, seed)
            self.assertEqual(sorted(ids), ids)
            self.assertEqual(sorted(ids, key=int), ids)
        self.assertNotEqual(wb.seeded_id(10001, 1401), wb.seeded_id(10001, 1402))
        # (1 + seed % 9) * 100000 + base, worked by hand: 1401 % 9 = 6, 1403 % 9 = 8, 2403 % 9 = 0.
        self.assertEqual([wb.seeded_id(10001, seed) for seed in (1401, 1403, 2403)], ["710001", "910001", "110001"])

    def test_permutation_reorders_replies_stably_per_seed(self):
        world = default_world()
        orders = {}
        for seed in (None, 1401, 1402, 1401):
            fake = FakePss(copy.deepcopy(world), permute_seed=seed)
            session = fake.session()
            token = login(session, fake)
            reply = xmltodict.parse(read(session, "/CharacterService/ListAllCharactersOfUser", token))
            ids = [c["@CharacterId"] for c in reply["CharacterService"]["ListAllCharactersOfUser"]["Characters"]
                   ["Character"]]
            self.assertEqual(sorted(ids), sorted(c.id for c in world.characters))
            orders.setdefault(seed, ids)
            self.assertEqual(orders[seed], ids, "the same seed must give the same order")
        self.assertEqual(orders[None], [c.id for c in world.characters])
        self.assertNotEqual(orders[1401], orders[1402])

    def test_a_started_scenario_orders_replies_by_its_seed(self):
        """QA round 4 S17: every fake of a started scenario permutes its replies with the scenario's seed, so the
        crew order a roster read shows differs between two seeds (three crew here)."""
        orders = {}
        for seed in (1401, 1402):
            with scratch_ctx("role-coverage", "swap", seed=seed) as ctx:
                self.assertEqual({alias: fake.permute_seed for alias, fake in ctx.fakes.items()}, {"alpha": seed})
                fake = ctx.fakes["alpha"]
                session = fake.session()
                token = login(session, fake)
                reply = xmltodict.parse(read(session, "/CharacterService/ListAllCharactersOfUser", token))
                rows = reply["CharacterService"]["ListAllCharactersOfUser"]["Characters"]["Character"]
                orders[seed] = [ctx.idmap.label("crew", row["@CharacterId"]) for row in rows]
        self.assertEqual(sorted(orders[1401]), ["ana", "ben", "cyd"])
        self.assertEqual(sorted(orders[1402]), sorted(orders[1401]))
        self.assertNotEqual(orders[1401], orders[1402])
        with scratch_ctx("distinct-accounts", "alpha-held", seed=1403) as ctx:
            self.assertEqual({alias: fake.permute_seed for alias, fake in ctx.fakes.items()},
                             {"alpha": 1403, "bravo": 1403})

    def test_label_map_round_trips_and_marks_unknown_ids(self):
        with scratch_ctx("role-coverage", "swap", seed=1402) as ctx:
            for label in ("ana", "ben", "cyd"):
                self.assertEqual(ctx.idmap.label("crew", ctx.idmap.id("crew", label)), label)
            self.assertEqual(ctx.idmap.id("crew", "ana"), wb.seeded_id(10001, 1402))
            self.assertEqual(ctx.idmap.label("crew", "123"), "?123")
            fake = ctx.fakes["alpha"]
            self.assertEqual({c.id for c in fake.state.characters}, set(ctx.idmap.labels["crew"]))

    def test_accounts_stay_disjoint(self):
        with scratch_ctx("distinct-accounts", "alpha-held") as ctx:
            alpha = {c.id for c in ctx.fakes["alpha"].state.characters}
            bravo = {c.id for c in ctx.fakes["bravo"].state.characters}
            self.assertFalse(alpha & bravo)
            self.assertEqual(ctx.idmap.owners["crew:fixer_b"], "bravo")
            self.assertIsInstance(ctx.driver, FakePssFleet)


# Verifies: S14-R7
class FakeExtensionTests(unittest.TestCase):
    def test_default_world_replies_carry_no_opt_in_attribute(self):
        fake = FakePss(default_world())
        session = fake.session()
        token = login(session, fake)
        texts = [read(session, "/TrainingService/ListAllTrainingDesigns2", designVersion="14"),
                 read(session, "/RoomService/ListRoomDesigns2", designVersion="11"),
                 read(session, "/ItemService/ListItemsOfAShip", token),
                 read(session, "/ShipService/GetShipByUserId", token, userId=fake.state.account.user_id)]
        for marker in ("CostString", "Chance=", " Level=", " Capacity=", 'Type="Mineral"', "RequiredRoomLevel"):
            self.assertFalse(any(marker in text for text in texts), marker)
        self.assertIn("<Researches>", texts[3])
        self.assertNotIn(("GET", "/CharacterService/PrestigeCharacterTo"), fake.routes)
        self.assertTrue(token.endswith("-444444444444"))

    def test_opt_in_course_and_room_fields_are_emitted(self):
        fake = FakePss(paid_world())
        session = fake.session()
        courses = xmltodict.parse(read(session, "/TrainingService/ListAllTrainingDesigns2", designVersion="14"))
        manual, drills = courses["TrainingService"]["ListAllTrainingDesigns"]["TrainingDesigns"]["TrainingDesign"]
        self.assertEqual((manual["@CostString"], manual["@RepairChance"], manual["@RequiredRoomLevel"],
                          manual["@RequiredResearchDesignId"], manual["@RequiredRoomType"], manual["@Rank"]),
                         ("mineral:500", "100", "2", "61", "Academy", "1"))
        self.assertNotIn("@CostString", drills)
        self.assertNotIn("@WeaponChance", drills)
        rooms = xmltodict.parse(read(session, "/RoomService/ListRoomDesigns2", designVersion="11"))
        academy = rooms["RoomService"]["ListRoomDesigns"]["RoomDesigns"]["RoomDesign"][0]
        self.assertEqual((academy["@Level"], academy["@Capacity"]), ("2", "1"))

    def test_strict_rules_charge_exactly_the_price_and_refuse_one_short(self):
        for mineral, started in ((500, True), (499, False)):
            fake = FakePss(paid_world(mineral=mineral))
            session = fake.session()
            reply = add_training(session, fake, login(session, fake))
            self.assertEqual("errorMessage" not in reply.text, started, reply.text)
            self.assertEqual(fake.state.resources.mineral, 0 if started else 499)
            entry = fake.ledger[-1]
            self.assertEqual((entry.applied, entry.delta["mineral"]), (started, -500 if started else 0))
            self.assertEqual(entry.refusal, None if started else "Not enough minerals")
            if started:
                self.assertEqual(entry.session[:2], ("1001", "41"))
                self.assertEqual(entry.session[2:], (fmt(fake.now), fmt(fake.now + HOUR)))

    def test_strict_rules_charge_a_gas_price_from_gas_and_refuse_one_short(self):
        """QA survivor F2: a gas price is taken from gas, never from minerals, and gas one short refuses the start
        however many minerals the account holds."""
        for gas, started in ((300, True), (299, False)):
            world = paid_world(mineral=1000, price="gas:300")
            world.resources.gas = gas
            fake = FakePss(world)
            session = fake.session()
            reply = add_training(session, fake, login(session, fake))
            self.assertEqual("errorMessage" not in reply.text, started, reply.text)
            self.assertEqual((fake.state.resources.mineral, fake.state.resources.gas), (1000, 0 if started else 299))
            entry = fake.ledger[-1]
            self.assertEqual(entry.delta, {"mineral": 0, "gas": -300 if started else 0, "starbux": 0})
            self.assertEqual((entry.applied, entry.refusal), (started, None if started else "Not enough gas"))

    def test_starbux_price_comes_from_credits_and_unparseable_charge_is_free(self):
        fake = FakePss(paid_world(price="starbux:5", credits=7))
        session = fake.session()
        add_training(session, fake, login(session, fake))
        self.assertEqual((fake.state.account.credits, fake.ledger[-1].delta["starbux"]), (2, -5))
        free = FakePss(paid_world(price="mineral:5O0"))
        session = free.session()
        add_training(session, free, login(session, free))
        self.assertEqual((free.ledger[-1].applied, free.ledger[-1].delta["mineral"]), (True, 0))

    def test_strict_rules_refuse_room_status_level_capacity_and_research(self):
        def refusal(edit):
            world = paid_world(mineral=5000)
            edit(world)
            fake = FakePss(world)
            session = fake.session()
            add_training(session, fake, login(session, fake))
            return fake.ledger[-1].refusal, fake.state.resources.mineral

        def upgrading(world):
            world.rooms[0].status = "Upgrading"

        def low_level(world):
            world.room_designs[0].level = 1

        def full(world):  # the room's only slot is taken by a session that is still running at FAKE_EPOCH
            world.characters[1].session = TrainingSession("42", datetime.datetime(2026, 11, 20, 11),
                                                          datetime.datetime(2026, 11, 20, 13))

        def no_research(world):
            world.researches = []

        self.assertEqual(refusal(upgrading), ("Room is not ready", 5000))
        self.assertEqual(refusal(low_level), ("Room level is too low", 5000))
        self.assertEqual(refusal(full), ("Room is full", 5000))
        self.assertEqual(refusal(no_research), ("Research required", 5000))
        self.assertEqual(refusal(lambda world: None), (None, 4500))

    def test_strict_rules_refuse_a_fatigued_crew_and_one_at_its_training_point_capacity(self):
        def refusal(edit):
            world = paid_world(mineral=5000)
            edit(world)
            fake = FakePss(world)
            session = fake.session()
            add_training(session, fake, login(session, fake))
            return fake.ledger[-1].refusal, fake.state.resources.mineral

        self.assertEqual(refusal(lambda w: setattr(w.characters[0], "fatigue", 100)), ("Crew is too fatigued", 5000))
        self.assertEqual(refusal(lambda w: setattr(w.characters[0], "fatigue", 99)), (None, 4500))
        self.assertEqual(refusal(lambda w: setattr(w.characters[0], "stats", stats(Repair=60, Weapon=40))),
                         ("Training points are at capacity", 5000))  # 100 points, capacity 100
        self.assertEqual(refusal(lambda w: setattr(w.characters[0], "stats", stats(Repair=99))), (None, 4500))

    def test_strict_rules_need_a_completed_research_the_named_room_and_a_running_session_to_fill_it(self):
        """QA round 3 M19, M21, M38: research in progress is no research; the required room is matched by design
        name (another Training room does not qualify); a session that ended (claimed or not) leaves its slot free, and
        a session in another room never fills this one."""
        def refusal(edit):
            world = paid_world(mineral=5000)
            edit(world)
            fake = FakePss(world)
            session = fake.session()
            add_training(session, fake, login(session, fake))
            return fake.ledger[-1].refusal, fake.state.resources.mineral

        def ended_unclaimed(world):  # the slot's session ended an hour before FAKE_EPOCH and is not claimed yet
            world.characters[1].session = TrainingSession("42", datetime.datetime(2026, 11, 20, 9),
                                                          datetime.datetime(2026, 11, 20, 11))

        def other_room(world):  # a running session in a second Academy
            world.room_designs.append(RoomDesign("902", "Academy", "Training", level=2, capacity=1))
            world.rooms.append(Room("13", "902"))
            world.characters[1].room_id = "13"
            world.characters[1].session = TrainingSession("42", datetime.datetime(2026, 11, 20, 11),
                                                          datetime.datetime(2026, 11, 20, 13))

        self.assertEqual(refusal(lambda w: setattr(w, "researches", [Research("61", "Researching")])),
                         ("Research required", 5000))
        self.assertEqual(refusal(lambda w: setattr(w.room_designs[0], "name", "Gym")),
                         ("Wrong room for this training", 5000))  # a Training room, not the Academy
        self.assertEqual(refusal(ended_unclaimed), (None, 4500))
        self.assertEqual(refusal(other_room), (None, 4500))

    def test_a_hidden_crew_is_left_out_of_the_roster_read(self):
        world = paid_world()
        world.characters[0].hidden = True
        fake = FakePss(world)
        session = fake.session()
        crew = xmltodict.parse(read(session, "/CharacterService/ListAllCharactersOfUser", login(session, fake)))
        rows = crew["CharacterService"]["ListAllCharactersOfUser"]["Characters"]["Character"]
        self.assertEqual([rows["@CharacterId"]] if isinstance(rows, dict) else [r["@CharacterId"] for r in rows],
                         ["1002"])

    def test_raised_fault_still_runs_the_routes_reply_hooks(self):
        import requests

        fake = FakePss(paid_world())
        session = fake.session()
        token = login(session, fake)
        fake.fail(ADD, raises=requests.ConnectionError, times=1)
        seen = []
        fake.on_reply(f"POST {ADD}", lambda f, exchange: seen.append(exchange.error))
        with self.assertRaises(requests.ConnectionError):
            add_training(session, fake, token)
        self.assertEqual(seen, ["fault"])

    def test_exchanges_carry_their_thread_and_ledger_entries_their_request_index(self):
        fake = FakePss(paid_world())
        session = fake.session()
        add_training(session, fake, login(session, fake))
        entry = fake.ledger[-1]
        self.assertEqual(fake.exchanges[entry.exchange].path, ADD)
        self.assertEqual((fake.exchanges[entry.exchange].thread, entry.to_dict()["exchange"]),
                         (threading.current_thread().name, entry.exchange))

    def test_a_roster_read_records_what_it_showed(self):
        """Round-2 m6: each ListAllCharactersOfUser exchange keeps the course and timer state it showed per visible
        character (a hidden one left out), so a verification can be checked against that very read."""
        fake = FakePss(paid_world(mineral=1000))
        session = fake.session()
        token = login(session, fake)
        read(session, "/CharacterService/ListAllCharactersOfUser", token)
        self.assertEqual(fake.exchanges[-1].roster, {"1001": {"course": "0", "running": False},
                                                     "1002": {"course": "0", "running": False}})
        self.assertIsNone(fake.exchanges[0].roster)  # the login is not a roster read
        add_training(session, fake, token)
        fake.state.character("1002").hidden = True
        read(session, "/CharacterService/ListAllCharactersOfUser", token)
        self.assertEqual(fake.exchanges[-1].roster, {"1001": {"course": "41", "running": True}})
        fake.advance(hours=2)
        read(session, "/CharacterService/ListAllCharactersOfUser", token)
        self.assertEqual(fake.exchanges[-1].roster, {"1001": {"course": "41", "running": False}})

    def test_a_roster_read_after_finish_training_shows_no_course(self):
        """QA round 2 F8: once FinishTraining applied the session, the roster serves no course for that crew, and the
        read records exactly that (a stale course would let a later verification pass on an applied session)."""
        fake = FakePss(paid_world(mineral=1000))
        session = fake.session()
        token = login(session, fake)
        add_training(session, fake, token)
        fake.advance(hours=2)
        session.post(f"{BASE}/TrainingService/FinishTraining?"
                     f"{urllib.parse.urlencode({'characterId': '1001', 'accessToken': token})}")
        self.assertTrue(getattr(fake.state.character("1001").session, "applied", False))
        read(session, "/CharacterService/ListAllCharactersOfUser", token)
        self.assertEqual(fake.exchanges[-1].roster, {"1001": {"course": "0", "running": False},
                                                     "1002": {"course": "0", "running": False}})

    def test_without_strict_rules_nothing_is_charged(self):
        world = paid_world()
        world.strict_training_rules = False
        fake = FakePss(world)
        session = fake.session()
        add_training(session, fake, login(session, fake))
        self.assertEqual((fake.ledger[-1].applied, fake.state.resources.mineral), (True, 500))

    def test_typed_resource_items_parse_as_minerals_and_gas_in_the_snapshot_reader(self):
        world = paid_world(mineral=1234)
        world.resources.gas = 56
        fake = FakePss(world)
        session = fake.session()
        items = _container_records(xmltodict.parse(read(session, "/ItemService/ListItemsOfAShip",
                                                        login(session, fake))), "Items", "Item")
        self.assertEqual(_resource_from_items(items, "mineral"), "1234")
        self.assertEqual(_resource_from_items(items, "gas"), "56")

    def test_omitted_stats_and_researches_are_left_out_of_replies(self):
        world = paid_world(ship_omits_researches=True)
        world.characters[0].omit_stats = ("RepairImprovement",)
        fake = FakePss(world)
        session = fake.session()
        token = login(session, fake)
        crew = xmltodict.parse(read(session, "/CharacterService/ListAllCharactersOfUser", token))
        first = crew["CharacterService"]["ListAllCharactersOfUser"]["Characters"]["Character"][0]
        self.assertNotIn("@RepairImprovement", first)
        self.assertEqual(first["@WeaponImprovement"], "0")
        ship = read(session, "/ShipService/GetShipByUserId", token, userId=world.account.user_id)
        self.assertNotIn("Researches", ship)

    def test_fault_with_apply_changes_state_then_answers_with_the_fault(self):
        fake = FakePss(paid_world())
        fault = fake.fail(ADD, status=404, apply=True)
        session = fake.session()
        reply = add_training(session, fake, login(session, fake))
        self.assertEqual((reply.status_code, fault.hits), (404, 1))
        self.assertEqual((fake.ledger[-1].applied, fake.state.character("1001").session.design_id), (True, "41"))
        plain = FakePss(paid_world())
        plain.fail(ADD, status=404)
        session = plain.session()
        add_training(session, plain, login(session, plain))
        self.assertEqual((plain.ledger[-1].applied, plain.ledger[-1].refusal), (False, "fault"))
        self.assertIsNone(plain.state.character("1001").session)

    def test_on_reply_trigger_runs_after_the_nth_reply(self):
        fake = FakePss(paid_world())
        hook = fake.on_reply("GET /CharacterService/ListAllCharactersOfUser",
                             lambda f, exchange: setattr(f.state.rooms[0], "status", "Upgrading"), nth=2)
        session = fake.session()
        token = login(session, fake)
        read(session, "/CharacterService/ListAllCharactersOfUser", token)
        self.assertEqual((hook.hits, fake.state.rooms[0].status), (0, "Normal"))
        read(session, "/CharacterService/ListAllCharactersOfUser", token)
        self.assertEqual((hook.hits, fake.state.rooms[0].status), (1, "Upgrading"))
        with self.assertRaises(ValueError):
            fake.on_reply("GET /Nope/Nothing", lambda f, e: None)

    def test_crash_before_and_after_applying(self):
        for apply in (False, True):
            fake = FakePss(paid_world())
            crash = fake.crash(f"POST {ADD}", apply=apply)
            session = fake.session()
            token = login(session, fake)
            with self.assertRaises(SimulatedCrash):
                add_training(session, fake, token)
            self.assertEqual(crash.hits, 1)
            self.assertEqual(fake.ledger[-1].applied, apply)
            self.assertEqual(fake.state.resources.mineral, 0 if apply else 500)

    def test_misapply_starts_another_course_but_replies_with_the_requested_one(self):
        fake = FakePss(paid_world())
        misapply = fake.misapply_training("42")
        session = fake.session()
        reply = xmltodict.parse(add_training(session, fake, login(session, fake)).text)
        self.assertEqual(reply["TrainingService"]["AddTraining"]["Character"]["@TrainingDesignId"], "41")
        self.assertEqual((misapply.hits, fake.ledger[-1].session[1]), (1, "42"))

    def test_fleet_login_keeps_the_other_accounts_token_and_routes_by_token(self):
        alpha, bravo = paid_world(), paid_world(mineral=900)
        bravo.account = Account(user_id="700000777", refresh_token="RT-synthetic-other-0002", credits=0)
        fleet = FakePssFleet({"alpha": FakePss(alpha, token_tag="00000000000a"),
                              "bravo": FakePss(bravo, token_tag="00000000000b")})
        session = fleet.fakes["alpha"].session()
        session.mount("https://", fleet.adapter)  # the router, not alpha's own adapter
        token_a = login(session, fleet.fakes["alpha"])
        token_b = login(session, fleet.fakes["bravo"])
        self.assertTrue(token_a.endswith("00000000000a") and token_b.endswith("00000000000b"))
        add_training(session, fleet.fakes["alpha"], token_a)
        self.assertEqual([e.token_status for e in fleet.exchanges("alpha")][-1], "current")
        add_training(session, fleet.fakes["bravo"], token_b)
        self.assertEqual((alpha.resources.mineral, bravo.resources.mineral), (0, 400))
        self.assertEqual((len(fleet.ledger("alpha")), len(fleet.ledger("bravo"))), (1, 1))
        self.assertEqual(fleet.violations, [])
        self.assertIs(fleet.fakes["bravo"].clock, fleet.fakes["alpha"].clock)
        with self.assertRaises(ValueError):
            FakePssFleet({"alpha": FakePss(paid_world()), "bravo": FakePss(paid_world())})

    def test_a_fleet_routes_a_revoked_token_to_the_account_that_issued_it(self):
        """QA round 4 F6: bravo's second login revokes bravo's first token; a request with it is bravo's exchange,
        recorded as revoked, and never lands on alpha (the first fake)."""
        alpha, bravo = paid_world(), paid_world()
        bravo.account = Account(user_id="700000777", refresh_token="RT-synthetic-other-0002", credits=0)
        fleet = FakePssFleet({"alpha": FakePss(alpha, token_tag="00000000000a"),
                              "bravo": FakePss(bravo, token_tag="00000000000b")})
        session = fleet.fakes["alpha"].session()
        session.mount("https://", fleet.adapter)
        login(session, fleet.fakes["alpha"])
        old = login(session, fleet.fakes["bravo"])
        login(session, fleet.fakes["bravo"])
        before = len(fleet.exchanges("alpha"))
        read(session, "/ItemService/ListItemsOfAShip", old)
        self.assertEqual([(e.path, e.token_status) for e in fleet.exchanges("bravo")][-1],
                         ("/ItemService/ListItemsOfAShip", "revoked"))
        self.assertEqual(len(fleet.exchanges("alpha")), before)
        self.assertEqual(fleet.fakes["alpha"].violations, [])
        self.assertIs(fleet.owner("GET", f"{BASE}/ItemService/ListItemsOfAShip?accessToken={old}", None),
                      fleet.fakes["bravo"])

    def test_a_fleet_requires_every_account_to_serve_the_first_accounts_catalogs(self):
        """QA round 2 F3: every account is checked, the third as much as the second, for every shared catalog."""
        def fleet(changed=None):
            fakes = {}
            for index, alias in enumerate(("alpha", "bravo", "charlie"), start=1):
                world = paid_world()
                world.account = Account(user_id=f"70000077{index}", refresh_token=f"RT-synthetic-fleet-000{index}",
                                        credits=0)
                if alias == "charlie" and changed:
                    changed(world)
                fakes[alias] = FakePss(world, token_tag=f"00000000000{index}")
            return FakePssFleet(fakes)

        self.assertEqual(sorted(fleet().fakes), ["alpha", "bravo", "charlie"])
        changes = {"room_designs": lambda w: w.room_designs.append(RoomDesign("902", "Lab", "Laboratory", level=1)),
                   "training_designs": lambda w: w.training_designs.pop(),
                   "research_designs": lambda w: setattr(w, "research_designs",
                                                          [ResearchDesign("61", "61", "Fitness 101", 2000)]),
                   "character_designs": lambda w: w.character_designs.append(CharacterDesign("33", "Extra")),
                   "design_versions": lambda w: w.design_versions.update({"TrainingDesignVersion": "999"})}
        for name, change in changes.items():
            with self.subTest(catalog=name):
                with self.assertRaisesRegex(ValueError, f"charlie serves different {name} than the first account"):
                    fleet(change)

    def test_concurrent_sessions_keep_the_first_login_valid_only_when_opted_in(self):
        for concurrent, status in ((False, "revoked"), (True, "current")):
            fake = FakePss(paid_world(concurrent_sessions=concurrent))
            session = fake.session()
            first = login(session, fake)
            login(session, fake)
            read(session, "/ItemService/ListItemsOfAShip", first)
            self.assertEqual(fake.exchanges[-1].token_status, status)

    def test_prestige_route_is_served_only_when_opted_in(self):
        world = paid_world(prestige_recipes=[("31", "31", "32")])
        fake = FakePss(world)
        reply = xmltodict.parse(read(fake.session(), "/CharacterService/PrestigeCharacterTo", characterDesignId="32"))
        recipe = reply["CharacterService"]["PrestigeCharacterTo"]["Prestiges"]["Prestige"]
        self.assertEqual((recipe["@CharacterDesignId1"], recipe["@CharacterDesignId2"], recipe["@ToCharacterDesignId"]),
                         ("31", "31", "32"))
        self.assertNotIn(("GET", "/CharacterService/PrestigeCharacterTo"), ROUTES)
        plain = FakePss(paid_world())
        read(plain.session(), "/CharacterService/PrestigeCharacterTo", characterDesignId="32")
        self.assertEqual(plain.unknown, [("GET", "/CharacterService/PrestigeCharacterTo")])
        with self.assertRaises(ValueError):
            World(prestige_recipes=[("31", "31", "99")], character_designs=[CharacterDesign("31", "x")]).validate()

    def test_prestige_serves_exactly_the_recipes_that_make_the_target(self):
        """QA round 2 F6: a recipe that uses the target as an ingredient is not a recipe for the target."""
        world = paid_world(prestige_recipes=[("31", "31", "32"), ("32", "31", "33"), ("31", "32", "33")])
        world.character_designs.append(CharacterDesign("33", "Synthetic Third"))
        fake = FakePss(world)

        def recipes(target):
            reply = xmltodict.parse(read(fake.session(), "/CharacterService/PrestigeCharacterTo",
                                         characterDesignId=target))
            rows = (reply["CharacterService"]["PrestigeCharacterTo"]["Prestiges"] or {}).get("Prestige", [])
            rows = [rows] if isinstance(rows, dict) else rows
            return [(row["@CharacterDesignId1"], row["@CharacterDesignId2"], row["@ToCharacterDesignId"]) for row in rows]

        self.assertEqual(recipes("32"), [("31", "31", "32")])
        self.assertEqual(recipes("33"), [("32", "31", "33"), ("31", "32", "33")])
        self.assertEqual(recipes("31"), [])

    def test_blocking_hook_waits_outside_the_lock(self):
        fake = FakePss(paid_world())
        session = fake.session()
        token = login(session, fake)
        entered, other_done = threading.Event(), threading.Event()

        def wait_for_other(f, exchange):
            entered.set()
            other_done.wait(5)

        hook = fake.on_reply("GET /CharacterService/ListAllCharactersOfUser", wait_for_other, blocking=True)
        first = threading.Thread(target=read, args=(fake.session(), "/CharacterService/ListAllCharactersOfUser",
                                                    token))
        first.start()
        self.assertTrue(entered.wait(5), "the blocking hook never ran")
        read(session, "/ItemService/ListItemsOfAShip", token)  # answered while the first request's hook waits
        self.assertTrue(first.is_alive(), "the hook should still be waiting: the second request did not block")
        other_done.set()
        first.join(5)
        self.assertFalse(first.is_alive())
        self.assertEqual(hook.hits, 1)
        self.assertEqual([e.path for e in fake.exchanges][-2:],
                         ["/CharacterService/ListAllCharactersOfUser", "/ItemService/ListItemsOfAShip"])


# Verifies: S14-R7
class ComparatorTests(unittest.TestCase):
    def assertUnmet(self, expected, raw, ctx, names, step="exec"):
        unmet = wb.compare(expected, wb.normalize(raw, ctx), ctx, step, CODES)
        self.assertEqual([u.assertion for u in unmet], names)
        return unmet

    def test_matching_trace_meets_every_assertion(self):
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            expected = scenario("paid-boundary", "exact-500")["expected"]["exec"]
            self.assertUnmet(expected, exact_500_trace(ctx), ctx, [])

    def test_extra_start_wrong_crew_wrong_delta_and_wrong_timer_are_named(self):
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            expected = scenario("paid-boundary", "exact-500")["expected"]["exec"]
            add = "POST /TrainingService/AddTraining"
            self.assertUnmet(expected, exact_500_trace(ctx, ledger=[ledger_start(ctx), ledger_start(ctx)]), ctx,
                             [f"mutations.{add}", f"applied.{add}"])
            self.assertUnmet(expected, exact_500_trace(ctx, delta={"mineral": 500, "gas": 0, "starbux": 0}), ctx,
                             ["resource_delta.alpha"])
            cid, did = ctx.idmap.id("crew", "fixer"), ctx.idmap.id("course", "eng_manual")
            self.assertUnmet(expected, exact_500_trace(
                ctx, started={cid: (did, "2026-11-26T00:00:00", "2026-11-26T00:59:00")}), ctx, ["timer.fixer"])
            self.assertUnmet(expected, exact_500_trace(ctx, journal=[journal_start(ctx, outcome="unknown")]), ctx,
                             ["journal"])
            self.assertUnmet(expected, exact_500_trace(ctx, journal=[journal_start(ctx, minerals=0)]), ctx,
                             ["journal"])
            self.assertUnmet({"exit": 2}, exact_500_trace(ctx), ctx, ["exit"])

    def test_an_attempted_skip_is_a_journal_row(self):
        """QA round 3 M4: only a deliberate skip record (skipped, not attempted) leaves the journal assertion; an
        attempt journaled as skipped is a row like any other."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            attempted = {**journal_start(ctx, outcome="skipped"), "reason": "insufficient_funds"}
            self.assertUnmet({"journal": []}, raw_trace(journal=[attempted]), ctx, ["journal"])
            self.assertUnmet({"journal": []}, raw_trace(journal=[{**attempted, "attempted": False}]), ctx, [])

    def test_room_and_design_ids_become_labels_in_mutations_and_the_journal(self):
        """QA round 3 M6 and its siblings: every PARAM_LABELS and JOURNAL_LABELS id (here roomId and
        upgradeRoomDesignId, room_id and upgrade_room_design_id) is turned back into its label, so an upgrade of
        another room or to another design is a mismatch."""
        route = "POST /RoomService/UpgradeRoom2"
        with scratch_ctx("paid-boundary", "earlier-reservation") as ctx:
            expected = {key: scenario("paid-boundary", "earlier-reservation")["expected"]["exec"][key]
                        for key in ("mutations", "journal")}

            def upgrade(room="storage", design="storage_l2"):
                rid, did = ctx.idmap.id("room", room), ctx.idmap.id("room_design", design)
                ledger = {"seq": 1, "thread": "MainThread", "method": "POST", "path": "/RoomService/UpgradeRoom2",
                          "params": {"roomId": rid, "upgradeRoomDesignId": did}, "applied": True, "refusal": None,
                          "delta": {"mineral": -600, "gas": 0, "starbux": 0}, "session": None, "exchange": 4}
                event = {"alias": "alpha", "action": "upgrade_room", "outcome": "verified", "attempted": True,
                         "dry_run": False, "reason": "postcondition",
                         "params": {"room_id": rid, "upgrade_room_design_id": did},
                         "cost": {"minerals_delta": -600, "gas_delta": 0, "starbux_delta": None}, "evidence": {}}
                return raw_trace(ledger=[ledger], journal=[event])

            trace = wb.normalize(upgrade(), ctx)
            self.assertEqual(trace["mutations"][route], [{"alias": "alpha", "room": "storage", "design": "storage_l2"}])
            self.assertEqual([{k: row[k] for k in ("room", "design")} for row in trace["journal"]],
                             [{"room": "storage", "design": "storage_l2"}])
            self.assertUnmet(expected, upgrade(), ctx, [])
            self.assertUnmet(expected, upgrade(room="academy"), ctx, [f"mutations.{route}", "journal"])
            self.assertUnmet(expected, upgrade(design="academy_l2"), ctx, [f"mutations.{route}", "journal"])

    def test_a_crew_plan_course_must_be_on_the_crews_own_line(self):
        """QA round 3 M11: the course name counts only on the line that carries the crew's id and role."""
        with scratch_ctx("role-coverage", "swap") as ctx:
            ana = ctx.idmap.id("crew", "ana")
            course = REQUIRED["catalog"]["training_designs"]["eng_manual"]["name"]
            expected = {"crew_plan": [{"crew": "ana", "role": "repair", "course": "eng_manual"}]}
            self.assertUnmet(expected, raw_trace(stdout=f"{ana} repair\n{course}"), ctx, ["crew_plan.ana"],
                             step="plan1")
            self.assertUnmet(expected, raw_trace(stdout=f"{ana} repair {course}"), ctx, [], step="plan1")

    def test_skip_needs_its_code_and_the_crews_instance_id(self):
        with scratch_ctx("paid-boundary", "short-499") as ctx:
            expected = {"skips": scenario("paid-boundary", "short-499")["expected"]["exec"]["skips"]}
            cid = ctx.idmap.id("crew", "fixer")
            missing = ["skips.alpha.insufficient_funds.fixer"]
            self.assertUnmet(expected, raw_trace(), ctx, missing)
            self.assertUnmet(expected, raw_trace(records=[("alpha", f"training {cid}: insufficient_funds: 499")]),
                             ctx, [])
            self.assertUnmet(expected, raw_trace(records=[("alpha", "training 1: insufficient_funds: 499")]), ctx,
                             missing)
            self.assertUnmet(expected, raw_trace(records=[("bravo", f"training {cid}: insufficient_funds")]), ctx,
                             missing)
            self.assertUnmet(expected, raw_trace(records=[("alpha", f"{cid}: insufficient_funds_soon")]), ctx, missing)

    def test_a_skip_code_in_free_output_or_a_legend_never_counts(self):
        """Finding 5: a printed legend of every code (stdout, stderr, or a refusal line naming several codes) must
        not satisfy a skip; a journal reason or a refusal line naming the account and one code does."""
        with scratch_ctx("paid-boundary", "short-499") as ctx:
            cid = ctx.idmap.id("crew", "fixer")
            expected = {"skips": [{"alias": "alpha", "crew": "fixer", "code": "insufficient_funds"},
                                  {"alias": "alpha", "code": "dry_run_stale"}]}
            legend = "codes: " + ", ".join(CODES) + f" | alpha | {cid}"
            both = ["skips.alpha.insufficient_funds.fixer", "skips.alpha.dry_run_stale"]
            self.assertUnmet(expected, raw_trace(stdout=legend, stderr=legend), ctx, both)
            self.assertUnmet(expected, raw_trace(stderr=f"  - account 'alpha': {legend}"), ctx, both)
            event = {**journal_start(ctx, outcome="skipped"), "attempted": False, "reason": "insufficient_funds"}
            refusal = "error: refused\n  - account 'alpha': dry_run_stale: no completed dry run\n"
            self.assertUnmet(expected, raw_trace(journal=[event], stderr=refusal), ctx, [])
            self.assertUnmet(expected, raw_trace(journal=[event], stderr=refusal.replace("'alpha'", "'bravo'")), ctx,
                             ["skips.alpha.dry_run_stale"])

    def test_a_crew_scoped_skip_needs_a_record_of_that_crew(self):
        """Round-2 m3: the code must sit with that crew's id, alone in a record or a record segment; a string about
        several candidates never satisfies another crew's expectation."""
        with scratch_ctx("role-coverage", "swap") as ctx:
            ana, ben = ctx.idmap.id("crew", "ana"), ctx.idmap.id("crew", "ben")
            expected = {"skips": [{"alias": "alpha", "crew": "ben", "code": "room_capacity"}]}
            missing = ["skips.alpha.room_capacity.ben"]
            mixed = f"reason: {ana} room_capacity, {ben} training_in_progress"
            self.assertUnmet(expected, raw_trace(records=[("alpha", mixed)]), ctx, missing)
            self.assertUnmet(expected, raw_trace(records=[("alpha", mixed.replace(",", ";"))]), ctx, missing)
            self.assertUnmet(expected, raw_trace(records=[("alpha", f"{ana}; {ben}: room_capacity")]), ctx, [])
            self.assertUnmet(expected, raw_trace(records=[("alpha", f"{ben}: room_capacity")]), ctx, [])
            event = {**journal_start(ctx, "ben", "weapon_drill", outcome="skipped"), "attempted": False,
                     "reason": "room_capacity"}
            self.assertUnmet(expected, raw_trace(journal=[event]), ctx, [])
            event["params"]["note"] = f"also {ana}"
            self.assertUnmet(expected, raw_trace(journal=[event]), ctx, missing)

    def test_a_refusal_line_names_its_account_with_or_without_a_colon(self):
        """Round-2 M2: today's pre-login lines read ``  - account 'alpha' is ...`` (no colon); either form names the
        account. Today's legacy refusal names legacy_run_skipped and legacy_run_serialized, neither a vocabulary
        code, so it never satisfies legacy_run_not_skipped."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            stale = {"skips": [{"alias": "alpha", "code": "dry_run_stale"}]}
            header = "error: --fleet-execute refused before any login:\n"
            for line in ("  - account 'alpha' has no recent dry run (dry_run_stale)",
                         "  - account 'alpha': dry_run_stale"):
                with self.subTest(line=line):
                    self.assertUnmet(stale, raw_trace(stderr=header + line + "\n"), ctx, [])
            self.assertUnmet(stale, raw_trace(stderr=header + "  - account 'alphabet' dry_run_stale\n"), ctx,
                             ["skips.alpha.dry_run_stale"])
            legacy = ("  - account 'alpha' is legacy daily run account 3, and the authorization does not confirm it "
                      "is skipped there (legacy_run_skipped) or never runs at the same time (legacy_run_serialized)")
            self.assertUnmet({"skips": [{"alias": "alpha", "code": "legacy_run_not_skipped"}]},
                             raw_trace(stderr=header + legacy + "\n"), ctx, ["skips.alpha.legacy_run_not_skipped"])
            self.assertUnmet({"skips": [{"alias": "alpha", "code": "legacy_run_not_skipped"}]},
                             raw_trace(stderr=header + "  - account 'alpha' legacy_run_not_skipped: list it in "
                                                       "LEGACY_RUN_SKIP\n"), ctx, [])

    def test_an_unreadable_journal_is_unmet_never_empty(self):
        """Round-2 m2: a malformed journal is reported as journal_unreadable, so ``"journal": []`` cannot pass."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            before = wb._before(ctx)
            (ctx.journals / "alpha.log").write_text("{not a journal line\n", encoding="utf-8")
            raw = wb._collect(ctx, before, 0, "", "")
            self.assertUnmet({"journal": []}, raw, ctx, ["journal_unreadable"])

    def test_a_crew_plan_skip_line_names_exactly_that_code(self):
        with scratch_ctx("role-coverage", "swap") as ctx:
            row = {"crew": "ben", "role": "weapons", "skip": "not_in_training_room"}
            ben = ctx.idmap.id("crew", "ben")
            self.assertUnmet({"crew_plan": [row]}, raw_trace(stdout=f"{ben} weapons not_in_training_room"), ctx, [],
                             step="plan1")
            self.assertUnmet({"crew_plan": [row]}, raw_trace(stdout=f"{ben} weapons " + " ".join(CODES)), ctx,
                             ["crew_plan.ben"], step="plan1")

    def test_store_presence_is_asserted_per_account(self):
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            self.assertUnmet({"store": {"alpha": "present"}}, raw_trace(store=False), ctx, ["store.alpha"])
            self.assertUnmet({"store": {"alpha": "present"}}, raw_trace(store=True), ctx, [])
            self.assertUnmet({"store": {"alpha": "absent"}}, raw_trace(store=True), ctx, ["store.alpha"])

    def test_journal_rows_pair_off_whatever_their_order_or_number(self):
        """Finding 15: no positional fallback; a row that matches several actual rows must not take the only actual
        row another expected row can match."""
        self.assertTrue(wb._multiset_diff([{"a": 1}, {"a": 1, "b": 2}], [{"a": 1, "b": 2}, {"a": 1, "b": 3}]))
        self.assertFalse(wb._multiset_diff([{"a": 1, "b": 2}, {"a": 1, "b": 2}], [{"a": 1, "b": 2}, {"a": 1}]))
        rows = [{"crew": f"c{index}", "outcome": "verified"} for index in range(8)]
        self.assertTrue(wb._multiset_diff(rows, list(reversed(rows))))
        self.assertFalse(wb._multiset_diff(rows, [*reversed(rows[1:]), {"crew": "c0", "outcome": "unknown"}]))
        self.assertTrue(wb._multiset_diff([{"outcome_in": ["unknown", "contradicted"]}], [{"outcome": "unknown"}]))

    def test_a_row_may_name_alternatives(self):
        """Holdout I2: ``any_of`` lists the rows the spec allows; one of them must match, with the row's other keys."""
        row = {"alias": "alpha", "any_of": [{"crew": "obi", "course": "science_primer"},
                                            {"crew": "una", "course": "weapon_drill"}]}
        self.assertTrue(wb._multiset_diff([row], [{"alias": "alpha", "crew": "una", "course": "weapon_drill"}]))
        self.assertFalse(wb._multiset_diff([row], [{"alias": "alpha", "crew": "una", "course": "science_primer"}]))
        self.assertFalse(wb._multiset_diff([row], [{"alias": "bravo", "crew": "obi", "course": "science_primer"}]))
        data = copy.deepcopy(HOLDOUT)
        planned = next(entry for item in data["cases"] for entry in item["scenarios"]
                       if entry["id"] == "missing-versus-zero")["expected"]["dry"]["journal_planned"]
        planned[0]["any_of"][1]["crew"] = "nobody"
        with self.assertRaisesRegex(wb.FixtureError, "unknown crew label 'nobody'"):
            wb.validate_suite(data)

    def test_crew_plan_lines_need_id_role_and_course_or_skip(self):
        with scratch_ctx("role-coverage", "swap") as ctx:
            expected = {"crew_plan": scenario("role-coverage", "swap")["expected"]["plan1"]["crew_plan"]}
            ana, ben, cyd = (ctx.idmap.id("crew", label) for label in ("ana", "ben", "cyd"))
            good = (f"{ana} repair Study Expert Engineering Manual\n{ben} weapons not_in_training_room\n"
                    f"{cyd} shields not_in_training_room\n")
            self.assertUnmet(expected, raw_trace(stdout=good), ctx, [], step="plan1")
            swapped = good.replace(f"{ana} repair", f"{ana} weapons")
            self.assertUnmet(expected, raw_trace(stdout=swapped), ctx, ["crew_plan.ana"], step="plan1")

    def test_prestige_line_needs_the_missing_count(self):
        with scratch_ctx("preview-prestige", "one-copy") as ctx:
            expected = {"prestige": [{"target": "succubus", "ingredient": "cadet", "missing": 1}]}
            line = "Galactic Succubus <- 2 x Synthetic Cadet: missing {}"
            self.assertUnmet(expected, raw_trace(stdout=line.format(1)), ctx, [], step="plan")
            self.assertUnmet(expected, raw_trace(stdout=line.format(0)), ctx, ["prestige.succubus"], step="plan")

    def test_dispatches_honor_candidate_one_of_and_canonical_order(self):
        with scratch_ctx("overlapping-dispatch", "threads") as ctx:
            expected = {"dispatches": scenario("overlapping-dispatch", "threads")["expected"]["overlap"]["dispatches"]}
            started = ledger_start(ctx, "gunner", "weapon_drill", mineral=-400, thread="dispatch-1")
            outputs = ["| alpha | skipped | locked: another dispatch holds the lock |", "| alpha | completed |"]
            raw = raw_trace(ledger=[started], outputs=outputs)
            self.assertUnmet(expected, raw, ctx, [], step="overlap")
            both = raw_trace(ledger=[ledger_start(ctx, thread="dispatch-0"), started], outputs=outputs)
            unmet = self.assertUnmet(expected, both, ctx, ["dispatches"], step="overlap")
            self.assertIn('"candidates":["*"]', unmet[0].actual)
            no_code = raw_trace(ledger=[started], outputs=["| alpha | skipped |", "| alpha | completed |"])
            self.assertUnmet(expected, no_code, ctx, ["dispatches"], step="overlap")
            legend = raw_trace(ledger=[started], outputs=["| alpha | skipped | " + " ".join(CODES), outputs[1]])
            self.assertUnmet(expected, legend, ctx, ["dispatches"], step="overlap")

    def test_a_dispatch_row_is_met_only_by_its_own_account(self):
        """QA round 2 H15 and its siblings: with two accounts, bravo's start, refusal or skip code never satisfies a
        row that names alpha."""
        with scratch_ctx("distinct-accounts", "alpha-held") as ctx:
            def two_accounts(alpha_ledger=(), bravo_ledger=(), output="| alpha | completed |\n| bravo | completed |"):
                raw = raw_trace(ledger=alpha_ledger, outputs=[output])
                raw["accounts"]["bravo"] = {**copy.deepcopy(raw["accounts"]["alpha"]), "ledger": list(bravo_ledger)}
                return raw

            alpha_start = ledger_start(ctx, "fixer", thread="dispatch-0")
            bravo_start = ledger_start(ctx, "fixer_b", thread="dispatch-0")
            started = {"dispatches": [[{"alias": "alpha", "result": "started"}]]}
            self.assertUnmet(started, two_accounts(alpha_ledger=[alpha_start]), ctx, [], step="overlap")
            self.assertUnmet(started, two_accounts(bravo_ledger=[bravo_start]), ctx, ["dispatches"], step="overlap")
            skipped = {"dispatches": [[{"alias": "bravo", "result": "skipped"}]]}
            self.assertUnmet(skipped, two_accounts(alpha_ledger=[alpha_start]), ctx, [], step="overlap")
            self.assertUnmet(skipped, two_accounts(bravo_ledger=[bravo_start]), ctx, ["dispatches"], step="overlap")
            locked = {"dispatches": [[{"alias": "alpha", "result": "skipped", "code": "locked"}]]}
            line = "| {} | skipped | locked: another dispatch holds the lock |"
            self.assertUnmet(locked, two_accounts(output=line.format("alpha")), ctx, [], step="overlap")
            self.assertUnmet(locked, two_accounts(output=line.format("bravo")), ctx, ["dispatches"], step="overlap")

    def test_every_row_key_must_match(self):
        """QA round 4 S7 and its siblings: a row naming outcome, outcome_in, any_of, a cost or attempted is unmet by
        a row that differs in that key alone."""
        actual = {"alias": "alpha", "action": "add_training", "crew": "fixer", "course": "eng_manual",
                  "outcome": "verified", "attempted": True, "cost": {"minerals": 500, "gas": 0}}
        self.assertTrue(wb._row_matches(dict(actual), actual))
        self.assertTrue(wb._row_matches({"outcome_in": ["unknown", "verified"], "any_of": [{"crew": "x"},
                                                                                           {"crew": "fixer"}]},
                                        actual))
        mismatches = {"outcome": {"outcome": "failed"}, "outcome_in": {"outcome_in": ["unknown", "contradicted"]},
                      "any_of": {"any_of": [{"crew": "gunner"}, {"course": "weapon_drill"}]},
                      "any_of keeps the row's keys": {"crew": "gunner", "any_of": [{"crew": "fixer"}]},
                      "cost minerals": {"cost": {"minerals": 499, "gas": 0}},
                      "cost gas": {"cost": {"minerals": 500, "gas": 1}},
                      "cost refund": {"cost": {"minerals": -500, "gas": 0}},
                      "attempted": {"attempted": False}, "alias": {"alias": "bravo"},
                      "action": {"action": "upgrade_room"},
                      "crew": {"crew": "gunner"}, "course": {"course": "weapon_drill"}}
        for name, row in mismatches.items():
            with self.subTest(key=name):
                self.assertFalse(wb._row_matches(row, actual))
                self.assertFalse(wb._multiset_diff([row], [actual]))
        self.assertFalse(wb._multiset_diff([{"action": "add_training", "outcome_in": ["verified"]}],
                                           [{"action": "add_training", "outcome": "failed"}]))

    def test_a_frozen_outcome_in_row_is_unmet_by_an_outcome_it_does_not_list(self):
        """QA round 4 S7 on a frozen step: reconciliation/different-course exec1 allows contradicted, unknown or
        not-observed, never verified."""
        with scratch_ctx("reconciliation", "different-course") as ctx:
            expected = {"journal": scenario("reconciliation", "different-course")["expected"]["exec1"]["journal"]}
            self.assertUnmet(expected, raw_trace(journal=[journal_start(ctx, outcome="unknown")]), ctx, [],
                             step="exec1")
            self.assertUnmet(expected, raw_trace(journal=[journal_start(ctx, outcome="verified")]), ctx, ["journal"],
                             step="exec1")

    def test_every_dispatch_row_field_must_match(self):
        """QA round 4 S9 and its siblings: result, add_training, mutations, candidate_one_of and code each decide a
        dispatch row alone."""
        got = {"alias": "alpha", "add_training": 1, "mutations": 1, "started": True, "candidates": ["fixer"],
               "codes": ["locked"]}
        met = [{}, {"result": "started"}, {"add_training": 1}, {"mutations": 1}, {"candidate_one_of": ["fixer", "x"]},
               {"code": "locked"}]
        unmet = [{"result": "skipped"}, {"add_training": 0}, {"add_training": 2}, {"mutations": 0}, {"mutations": 2},
                 {"candidate_one_of": ["gunner"]}, {"code": "room_capacity"}]
        for want in met:
            with self.subTest(met=want):
                self.assertTrue(wb._dispatch_row_ok(want, got))
        for want in unmet:
            with self.subTest(unmet=want):
                self.assertFalse(wb._dispatch_row_ok(want, got))
        idle = {**got, "add_training": 0, "mutations": 0, "started": False, "candidates": [], "codes": []}
        self.assertTrue(wb._dispatch_row_ok({"result": "skipped", "mutations": 0}, idle))
        self.assertFalse(wb._dispatch_row_ok({"result": "started"}, idle))
        self.assertFalse(wb._dispatch_row_ok({"candidate_one_of": ["fixer"]}, idle))  # no candidate is none of them

    def upgrade(self, thread, applied=True):
        return {"seq": 1, "thread": thread, "method": "POST", "path": "/RoomService/UpgradeRoom2",
                "params": {"roomId": "1", "upgradeRoomDesignId": "2"}, "applied": applied,
                "refusal": None if applied else "No", "delta": {"mineral": -600 if applied else 0, "gas": 0,
                                                                 "starbux": 0}, "session": None, "exchange": 4}

    def test_a_dispatch_rows_mutation_count_counts_every_mutating_request(self):
        """QA round 4 S9: a skipped dispatch frozen with no mutation is unmet when it sent any other mutation."""
        with scratch_ctx("overlapping-dispatch", "threads") as ctx:
            expected = {"dispatches": [[{"alias": "alpha", "result": "skipped", "mutations": 0}]]}
            outputs = ["| alpha | skipped |"]
            self.assertUnmet(expected, raw_trace(outputs=outputs), ctx, [], step="overlap")
            self.assertUnmet(expected, raw_trace(ledger=[self.upgrade("dispatch-0")], outputs=outputs), ctx,
                             ["dispatches"], step="overlap")
            self.assertUnmet(expected, raw_trace(ledger=[self.upgrade("dispatch-1")], outputs=outputs), ctx, [],
                             step="overlap")  # another dispatch's request is not this one's

    def test_a_dispatch_started_only_when_its_add_training_applied(self):
        """QA round 4 S10: an applied upgrade beside a refused AddTraining is a skipped dispatch, not a started one;
        the applied AddTraining is a start."""
        with scratch_ctx("overlapping-dispatch", "threads") as ctx:
            started = {"dispatches": [[{"alias": "alpha", "result": "started"}]]}
            skipped = {"dispatches": [[{"alias": "alpha", "result": "skipped"}]]}
            refused = raw_trace(ledger=[self.upgrade("dispatch-0"),
                                        ledger_start(ctx, applied=False, thread="dispatch-0")],
                                outputs=["| alpha | skipped |"])
            self.assertUnmet(started, refused, ctx, ["dispatches"], step="overlap")
            self.assertUnmet(skipped, refused, ctx, [], step="overlap")
            applied = raw_trace(ledger=[self.upgrade("dispatch-0", applied=False),
                                        ledger_start(ctx, thread="dispatch-0")],
                                outputs=["| alpha | completed |"])
            self.assertUnmet(started, applied, ctx, [], step="overlap")
            self.assertUnmet(skipped, applied, ctx, ["dispatches"], step="overlap")
            self.assertUnmet({"dispatches": [[{"alias": "bravo", "result": "skipped"}]]}, refused, ctx, ["dispatches"],
                             step="overlap")  # a row for an account the trace lacks is never met

    def test_fake_protocol_violations_are_semantic_mismatches(self):
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            raw = raw_trace()
            raw["accounts"]["alpha"]["violations"] = ["POST /x: unexpected foo"]
            self.assertUnmet({"other_mutations": 0}, raw, ctx, ["fake_violations"])


# Verifies: S14-R7
class NormalizerTests(unittest.TestCase):
    BEHAVIOR = ("mutations", "applied", "resource_delta", "timer", "journal", "journal_planned", "requests")

    def test_only_environmental_values_change(self):
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            first = exact_500_trace(ctx)
            second = copy.deepcopy(first)
            cid, did = ctx.idmap.id("crew", "fixer"), ctx.idmap.id("course", "eng_manual")
            second["accounts"]["alpha"]["started"] = {cid: (did, "2026-12-01T07:00:00", "2026-12-01T08:00:00")}
            second["accounts"]["alpha"]["ledger"][0]["params"]["trainingStartDate"] = "2026-12-01T07:00:00"
            second["accounts"]["alpha"]["journal"][0]["reason"] = "another wording at another time"
            second["accounts"]["alpha"]["journal"][0]["evidence"] = {"attempt": "f00d" * 8}
            second["stdout"] = "Run record: journals/runs/20261201T070000Z.json"
            one, two = wb.normalize(first, ctx), wb.normalize(second, ctx)
            self.assertEqual({key: one[key] for key in self.BEHAVIOR}, {key: two[key] for key in self.BEHAVIOR})
            self.assertEqual(one["journal"][0]["cost"], {"minerals": 500, "gas": 0})
            self.assertEqual(one["journal"][0]["outcome"], "verified")
            costly = copy.deepcopy(first)
            costly["accounts"]["alpha"]["journal"][0]["cost"]["minerals_delta"] = -400
            self.assertNotEqual(wb.normalize(costly, ctx)["journal"], one["journal"])

    def test_the_behavior_digest_ignores_the_environment_but_not_a_choice(self):
        """Round-2 M1: determinism compares a digest of each step's behavior, so a run that starts another allowed
        crew differs even when both runs pass; which dispatch won, stamps, times and wording do not count."""
        with scratch_ctx("overlapping-dispatch", "threads") as ctx:
            def digest(raw):
                return wb.behavior_digest(wb.normalize(raw, ctx), ctx, CODES)

            fixer = ledger_start(ctx, thread="dispatch-0")
            outputs = ["| alpha | completed |", "| alpha | skipped | locked: held by 7f3a9c2e at 00:00:01 |"]
            event = journal_start(ctx)
            first = raw_trace(ledger=[fixer], outputs=outputs, stdout="\n".join(outputs), journal=[event],
                              records=[("alpha", "status: completed at 2026-11-26T00:00:03")])
            moved = {**fixer, "thread": "dispatch-1", "seq": 7, "exchange": 41,
                     "params": {**fixer["params"], "trainingStartDate": "2026-12-01T07:00:00"}}
            later = [outputs[1].replace("7f3a9c2e at 00:00:01", "0b1d2e3f at 07:00:09"), outputs[0]]
            same = raw_trace(ledger=[moved], outputs=later, stdout="\n".join(reversed(later)),
                             journal=[{**event, "evidence": {"attempt": "f00d" * 8}, "reason": "other words"}],
                             records=[("alpha", "status: completed at 2026-12-01T07:00:12")])
            self.assertEqual(digest(first), digest(same))
            gunner = ledger_start(ctx, "gunner", "weapon_drill", mineral=-400, thread="dispatch-0")
            other = raw_trace(ledger=[gunner], outputs=outputs, stdout="\n".join(outputs), journal=[event],
                              records=[("alpha", "status: completed at 2026-11-26T00:00:03")])
            self.assertNotEqual(digest(first), digest(other))
            coded = raw_trace(ledger=[fixer], outputs=outputs, stdout="\n".join(outputs), journal=[event],
                              records=[("alpha", "status: completed; room_capacity")])
            self.assertNotEqual(digest(first), digest(coded))

    def test_a_refund_normalizes_to_a_negative_cost_and_never_matches_a_spend(self):
        """QA survivor H5: a journal cost is signed (spend positive, refund negative), so a +100 minerals refund
        normalizes to cost.minerals -100 and fails a frozen row that expects a 100 minerals spend."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            refund = wb.normalize(exact_500_trace(ctx, journal=[journal_start(ctx, minerals=100)]), ctx)
            spend = wb.normalize(exact_500_trace(ctx, journal=[journal_start(ctx, minerals=-100)]), ctx)
            self.assertEqual(refund["journal"][0]["cost"], {"minerals": -100, "gas": 0})
            self.assertEqual(spend["journal"][0]["cost"], {"minerals": 100, "gas": 0})
            frozen = {"journal": [{"alias": "alpha", "action": "add_training", "crew": "fixer", "course": "eng_manual",
                                   "cost": {"minerals": 100, "gas": 0}}]}
            self.assertEqual([u.assertion for u in wb.compare(frozen, refund, ctx, "exec", CODES)], ["journal"])
            self.assertEqual(wb.compare(frozen, spend, ctx, "exec", CODES), [])

    def test_unmapped_ids_become_question_marks(self):
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            entry = ledger_start(ctx)
            entry["params"]["characterId"] = "424242"
            trace = wb.normalize(raw_trace(ledger=[entry]), ctx)
            self.assertEqual(trace["mutations"]["POST /TrainingService/AddTraining"][0]["crew"], "?424242")

    def test_cause_lines_drop_scratch_paths_and_stamps(self):
        raw = {"stderr": "error: --fleet-execute refused before any login:\n  - account 'alpha': stale in "
                         "/private/tmp/twb-x/journals/alpha.log stamp 0123456789abcdef\n", "exit": 2}
        self.assertEqual(wb.step_cause(raw), "execute refused before login: account 'alpha': stale in <path> stamp "
                                             "<hex>")
        self.assertIn("registry refuses training-v2",
                      wb.step_cause({"stderr": "error: alpha: unknown policy 'training-v2'", "exit": 2}))


# Verifies: S14-R7
class HardViolationTests(unittest.TestCase):
    def violations(self, raw, ctx, step=None, expected=None):
        step = step or {"id": "exec", "kind": "execute"}
        expected = expected if expected is not None else scenario("paid-boundary", "exact-500")["expected"]["exec"]
        return wb.hard_violations(ctx, step, expected, wb.normalize(raw, ctx))

    def test_clean_trace_has_none(self):
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            self.assertEqual(self.violations(exact_500_trace(ctx), ctx), [])

    def test_account_crossover(self):
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            entry = ledger_start(ctx)
            entry["params"]["characterId"] = wb.seeded_id(20001, 1401)
            found = self.violations(exact_500_trace(ctx, ledger=[entry]), ctx)
            self.assertTrue(any("account_crossover" in item for item in found), found)

    def test_premium_spend(self):
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            found = self.violations(exact_500_trace(ctx, ledger=[ledger_start(ctx, starbux=-5)]), ctx)
            self.assertTrue(any("premium_spend" in item for item in found), found)
            # QA round 2 H17: a single starbux is a premium spend; no starbux change is none.
            found = self.violations(exact_500_trace(ctx, ledger=[ledger_start(ctx, starbux=-1)]), ctx)
            self.assertIn("exec: premium_spend: alpha spent 1 starbux", found)
            self.assertEqual(self.violations(exact_500_trace(ctx, ledger=[ledger_start(ctx, starbux=0)]), ctx), [])

    def test_budget_spend_boundaries_are_exact(self):
        """Siblings of QA round 2 H17: spending exactly the cap (or exactly what the frozen trace allows) is no
        violation, one more mineral or gas is."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            def budget(mineral=0, gas=0, expected=None):
                entry = ledger_start(ctx, mineral=mineral)
                entry["delta"]["gas"] = gas
                found = self.violations(exact_500_trace(ctx, ledger=[entry], journal=[]), ctx,
                                        expected={} if expected is None else expected)
                return [item for item in found if "budget_spend" in item]

            self.assertEqual(budget(mineral=-1000), [])
            self.assertEqual(budget(mineral=-1001), ["exec: budget_spend: alpha spent 1001 mineral, above its cap 1000"])
            self.assertEqual(budget(mineral=-500, gas=-1000), [])
            self.assertEqual(budget(mineral=-500, gas=-1001), ["exec: budget_spend: alpha spent 1001 gas, above its cap 1000"])
            frozen = {"resource_delta": {"alpha": {"mineral": -500, "gas": 0, "starbux": 0}}}
            self.assertEqual(budget(mineral=-500, expected=frozen), [])
            self.assertEqual(budget(mineral=-501, expected=frozen),
                             ["exec: budget_spend: alpha spent 501 mineral, more than the frozen trace allows (500)"])
            self.assertEqual(budget(mineral=-500, gas=-1, expected=frozen),
                             ["exec: budget_spend: alpha spent 1 gas, more than the frozen trace allows (0)"])

    def test_budget_spend_above_cap_or_unjournaled(self):
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            found = self.violations(exact_500_trace(ctx, ledger=[ledger_start(ctx, mineral=-1500)]), ctx, expected={})
            self.assertTrue(any("above its cap 1000" in item for item in found), found)
            found = self.violations(exact_500_trace(ctx, journal=[journal_start(ctx, minerals=0)]), ctx)
            self.assertTrue(any("journaled 0 mineral spent, the fake charged 500" in item for item in found), found)

    def test_unobserved_or_unsettled_spend_is_not_a_budget_violation(self):
        """Finding 2: an unknown or not-observed attempt may journal no spend while the fake charged it."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            for outcome in ("unknown", "not-observed"):
                event = journal_start(ctx, outcome=outcome)
                event["cost"] = {"minerals_delta": None, "gas_delta": None, "starbux_delta": None}
                found = self.violations(exact_500_trace(ctx, journal=[event]), ctx)
                self.assertFalse([item for item in found if "budget_spend" in item], (outcome, found))
            observed_unknown = journal_start(ctx, outcome="unknown", minerals=0)
            self.assertFalse([item for item in self.violations(exact_500_trace(ctx, journal=[observed_unknown]), ctx)
                              if "budget_spend" in item])
            overclaimed = journal_start(ctx, outcome="verified", minerals=-900)
            pending = journal_start(ctx, "fixer", outcome="unknown")
            pending["cost"]["minerals_delta"] = None
            found = self.violations(exact_500_trace(ctx, journal=[overclaimed, pending]), ctx)
            self.assertTrue(any("journaled 900 mineral spent, the fake charged 500" in item for item in found), found)

    def test_a_later_reconciliation_may_restate_an_earlier_start(self):
        """Findings 2 and 4: a step that verifies an earlier step's start (a fresh roster read, nothing charged) is
        neither a false verification nor a budget violation; without the fresh read it is a false verification."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            restated = raw_trace(earlier=[ledger_start(ctx)], journal=[journal_start(ctx)],
                                 exchanges=[roster_read(9, roster=showing(ctx))])
            self.assertEqual(self.violations(restated, ctx, expected={}), [])
            unread = raw_trace(earlier=[ledger_start(ctx)], journal=[journal_start(ctx)])
            found = self.violations(unread, ctx, expected={})
            self.assertEqual([item.split(":")[1].strip() for item in found], ["false_verification"])

    def test_duplicate_write(self):
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            two = exact_500_trace(ctx, ledger=[ledger_start(ctx), ledger_start(ctx, "fixer", mineral=0)])
            self.assertTrue(any("duplicate_write" in item for item in self.violations(two, ctx, expected={})))
            sent = exact_500_trace(ctx, ledger=[ledger_start(ctx), ledger_start(ctx, "fixer", applied=False)])
            found = self.violations(sent, ctx, expected={})  # finding 14: a refused second request was still sent
            self.assertTrue(any("duplicate_write: 2 AddTraining sent" in item for item in found), found)
            blocked = {"id": "exec2", "kind": "execute", "blocked_after_unresolved": True}
            refused = raw_trace(ledger=[ledger_start(ctx, applied=False)])
            found = self.violations(refused, ctx, step=blocked, expected={})
            self.assertEqual([item.split(":")[1].strip() for item in found], ["duplicate_write"])

    def test_false_verification(self):
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            short = ledger_start(ctx)
            short["session"][3] = "2026-11-26T00:30:00"
            found = self.violations(exact_500_trace(ctx, ledger=[short]), ctx)
            self.assertTrue(any("false_verification" in item for item in found), found)
            elsewhere = ledger_start(ctx)
            elsewhere["session"][0] = wb.seeded_id(10002, 1401)
            found = self.violations(exact_500_trace(ctx, ledger=[elsewhere]), ctx)
            self.assertTrue(any("false_verification" in item for item in found), found)
            wrong_course = ledger_start(ctx, session_course="eng_manual")
            wrong_course["session"][1] = wb.seeded_id(40002, 1401)
            found = self.violations(exact_500_trace(ctx, ledger=[wrong_course]), ctx)
            self.assertTrue(any("false_verification" in item for item in found), found)

    def test_verification_needs_a_successful_roster_read_after_the_start_on_its_dispatch(self):
        """Finding 4 (S14-R5): the AddTraining reply alone never verifies, even when the fake applied the start."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            for reads in ([], [roster_read(3)], [roster_read(6, thread="dispatch-1")], [roster_read(6, status=404)],
                          [roster_read(6, error="fault")]):
                with self.subTest(reads=reads):
                    found = self.violations(exact_500_trace(ctx, exchanges=reads), ctx)
                    self.assertTrue(any("without a successful roster read after the start" in item
                                        for item in found), found)
            self.assertEqual(self.violations(exact_500_trace(ctx, exchanges=[roster_read(6, roster=showing(ctx))]),
                                             ctx), [])

    def test_verification_needs_the_post_read_to_show_the_requested_course_running(self):
        """Round-2 m6: a successful roster read is not enough. It must show the crew on the requested course with a
        running timer; a read that lacks the crew, shows another course or an ended timer leaves it unverified."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            cid = ctx.idmap.id("crew", "fixer")
            for roster in (None, {}, showing(ctx, course=None, running=False), {cid: {"course": "999", "running": True}},
                           showing(ctx, running=False)):
                with self.subTest(roster=roster):
                    found = self.violations(exact_500_trace(ctx, exchanges=[roster_read(6, roster=roster)]), ctx)
                    self.assertTrue(any("false_verification" in item and "running timer" in item for item in found),
                                    found)
            restated = raw_trace(earlier=[ledger_start(ctx)], journal=[journal_start(ctx)],
                                 exchanges=[roster_read(9, roster={})])
            found = self.violations(restated, ctx, expected={})
            self.assertEqual([item.split(":")[1].strip() for item in found], ["false_verification"])

    @staticmethod
    def two_accounts(alpha_ledger=(), bravo_ledger=(), alpha_journal=()):
        """alpha and bravo (distinct-accounts) with nothing but the given ledger entries and alpha's journal."""
        raw = raw_trace(ledger=alpha_ledger, journal=alpha_journal)
        raw["accounts"]["bravo"] = {**copy.deepcopy(raw["accounts"]["alpha"]), "ledger": list(bravo_ledger),
                                    "journal": []}
        return raw

    def kinds(self, found, kind):
        return [item for item in found if f": {kind}: " in item]

    def test_account_crossover_by_ledger_by_journal_alias_and_by_journal_crew(self):
        """QA round 4 S1 and S2 and their ledger sibling: each crossover branch fires for another account's alias or
        crew (a crew the scenario knows, not only an unknown id) and stays silent for the account's own."""
        with scratch_ctx("distinct-accounts", "alpha-held") as ctx:
            for_bravo = {**journal_start(ctx, outcome="unknown"), "alias": "bravo"}
            cases = {
                "ledger names bravo's crew": (self.two_accounts(alpha_ledger=[ledger_start(ctx, "fixer_b")]),
                                              ["exec: account_crossover: alpha received a request for character "
                                               "fixer_b of another account"]),
                "ledger names alpha's crew": (self.two_accounts(alpha_ledger=[ledger_start(ctx, "fixer")]), []),
                "journal event for bravo": (self.two_accounts(alpha_journal=[for_bravo]),
                                            ["exec: account_crossover: alpha's journal holds an event for bravo"]),
                "journal names bravo's crew": (
                    self.two_accounts(alpha_journal=[journal_start(ctx, "fixer_b", outcome="unknown")]),
                    ["exec: account_crossover: alpha's journal names character fixer_b"]),
                "journal names alpha's crew": (
                    self.two_accounts(alpha_journal=[journal_start(ctx, "fixer", outcome="unknown")]), []),
            }
            for name, (raw, want) in cases.items():
                with self.subTest(case=name):
                    found = self.violations(raw, ctx, expected={})
                    self.assertEqual(self.kinds(found, "account_crossover"), want, found)

    def test_a_journal_event_for_another_alias_is_a_crossover_on_one_account_too(self):
        """QA round 4 S1, as QA proposed it: the exact-500 trace with its journal event naming bravo."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            found = self.violations(exact_500_trace(ctx, journal=[{**journal_start(ctx), "alias": "bravo"}]), ctx)
            self.assertIn("exec: account_crossover: alpha's journal holds an event for bravo", found)

    def test_duplicate_write_is_counted_per_account(self):
        """Siblings of finding 14: one start on each of two accounts is no duplicate; two on alpha are, and only
        alpha is named; a blocked step names only the account that sent a start."""
        with scratch_ctx("distinct-accounts", "alpha-held") as ctx:
            fixer, fixer_b = ledger_start(ctx, "fixer", mineral=0), ledger_start(ctx, "fixer_b", mineral=0)
            self.assertEqual(self.kinds(self.violations(self.two_accounts([fixer], [fixer_b]), ctx, expected={}),
                                        "duplicate_write"), [])
            found = self.violations(self.two_accounts([fixer, fixer], [fixer_b]), ctx, expected={})
            self.assertEqual(self.kinds(found, "duplicate_write"),
                             ["exec: duplicate_write: 2 AddTraining sent on alpha in one step"])
            blocked = {"id": "exec2", "kind": "execute", "blocked_after_unresolved": True}
            found = self.violations(self.two_accounts([ledger_start(ctx, "fixer", applied=False)]), ctx, step=blocked,
                                    expected={})
            self.assertEqual(self.kinds(found, "duplicate_write"),
                             ["exec2: duplicate_write: 1 AddTraining on alpha after an unresolved attempt"])
            self.assertEqual(self.violations(self.two_accounts(), ctx, step=blocked, expected={}), [])

    def test_a_start_restated_from_an_earlier_step_and_applied_again_is_charged_here(self):
        """QA round 4 S4: the same crew and course applied in an earlier step and again in this one is this step's
        spend, so a journal claiming nothing was spent is a budget violation."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            again = exact_500_trace(ctx, earlier=[ledger_start(ctx)], journal=[journal_start(ctx, minerals=0)])
            self.assertIn("exec: budget_spend: alpha journaled 0 mineral spent, the fake charged 500",
                          self.violations(again, ctx))
            self.assertEqual(self.violations(exact_500_trace(ctx, earlier=[ledger_start(ctx)]), ctx), [])

    def test_the_journal_must_match_the_charge_in_gas_too(self):
        """Sibling of the mineral check: a gas charge the journal leaves out (or overstates) is a budget violation;
        the exact gas spend journaled is none."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            entry = ledger_start(ctx)
            entry["delta"]["gas"] = -300

            def gas(journaled):
                event = journal_start(ctx)
                event["cost"]["gas_delta"] = journaled
                return self.kinds(self.violations(exact_500_trace(ctx, ledger=[entry], journal=[event]), ctx,
                                                  expected={}), "budget_spend")

            self.assertEqual(gas(-300), [])
            self.assertEqual(gas(0), ["exec: budget_spend: alpha journaled 0 gas spent, the fake charged 300"])
            self.assertEqual(gas(-301), ["exec: budget_spend: alpha journaled 301 gas spent, the fake charged 300"])

    def test_only_an_executed_verified_add_training_is_checked_for_false_verification(self):
        """Every false-verification guard: with nothing applied and nothing read, a verified, executed add_training is
        false; a dry run, another outcome or another action is not checked."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            event = {**journal_start(ctx), "attempted": False, "cost": {"minerals_delta": None, "gas_delta": None,
                                                                         "starbux_delta": None}}
            cases = {"verified": (event, ["exec: false_verification: alpha journaled add_training fixer verified "
                                          "without the requested course and timer"]),
                     "dry run": ({**event, "dry_run": True}, []),
                     "unknown": ({**event, "outcome": "unknown"}, []),
                     "upgrade_room": ({**event, "action": "upgrade_room"}, [])}
            for name, (journaled, want) in cases.items():
                with self.subTest(case=name):
                    found = self.violations(raw_trace(journal=[journaled]), ctx, expected={})
                    self.assertEqual(self.kinds(found, "false_verification"), want, found)

    def test_an_account_without_an_authorization_has_no_budget(self):
        """QA round 4 S3 (equivalent on the pinned fixtures, every one of which authorizes every account): caps come
        from the account's authorization, 1000/1000 when it names none, and nothing without an authorization."""
        accounts = [{"alias": "alpha", "authorization": None}, {"alias": "bravo"},
                    {"alias": "charlie", "authorization": {"caps": {"minerals": 5, "gas": 6}}},
                    {"alias": "delta", "authorization": {}}]
        ctx = wb.ScenarioRun("required", "c", {"accounts": accounts}, REQUIRED["catalog"], 1401, None, Path("."))
        self.assertEqual([ctx.caps(alias) for alias in ("alpha", "bravo", "charlie", "delta", "echo")],
                         [{"minerals": 0, "gas": 0}, {"minerals": 0, "gas": 0}, {"minerals": 5, "gas": 6},
                          {"minerals": 1000, "gas": 1000}, {"minerals": 0, "gas": 0}])
        with scratch_ctx("paid-boundary", "exact-500") as run:
            with patch.object(type(run), "caps", lambda self, alias: {"minerals": 0, "gas": 0}):
                found = self.violations(exact_500_trace(run), run, expected={})
            self.assertEqual(self.kinds(found, "budget_spend"),
                             ["exec: budget_spend: alpha spent 500 mineral, above its cap 0"])


# Verifies: S14-R7
class WitnessTests(unittest.TestCase):
    def test_a_fault_that_never_fired_is_reported_missing(self):
        with scratch_ctx("reconciliation", "missing-post-read") as ctx:
            step = next(s for s in scenario("reconciliation", "missing-post-read")["steps"] if s["id"] == "exec1")
            checks = wb._arm(ctx, step, step["id"])
            for check in checks:
                check()
            self.assertEqual(ctx.witnesses["exec1:fault:0"], "fault never fired")
            # Finding 12: the unfired after_route hook is disarmed too, so it can never arm the fault in a later step.
            armers = [hook for hook in ctx.fakes["alpha"].hooks if hook.path == ADD]
            self.assertEqual([(hook.hits, hook.times) for hook in armers], [(0, 0)])

    def test_store_artifacts_are_only_the_named_accounts_store(self):
        with tempfile.TemporaryDirectory() as directory:
            journals = Path(directory)
            store = journals / wb.STORE_DIR
            (store / "alpha").mkdir(parents=True)
            for name in ("alpha/attempts.json", "alpha/held.lock", "alpha.json", "alpha.lock", "alphabet.json",
                         "bravo.json"):
                (store / name).write_text("{}", encoding="utf-8")
            for name in ("alpha.log", "STOP", "alpha.json.lock", "dashboard.html"):
                (journals / name).write_text("x", encoding="utf-8")
            found = sorted(path.relative_to(store).as_posix() for path in wb.store_artifacts(journals, ["alpha"]))
            self.assertEqual(found, ["alpha", "alpha.json", "alpha/attempts.json"])
            self.assertEqual(wb.store_artifacts(journals / "nowhere", ["alpha"]), [])

    def test_failed_intent_writes_attach_at_the_hook_or_report_why_not(self):
        """Finding 3: the intent-write failure goes through INTENT_WRITE_HOOK; a hook that is absent or never
        called is a missing witness with its cause, never a pass."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            with patch.object(wb, "INTENT_WRITE_HOOK", ("tests.fakes.state", "no_such_intent_hook")):
                wb._fail_intent_writes(ctx, "exec")()
            self.assertIn("does not exist", ctx.witnesses["exec:intent_write_failed"])
            store = types.ModuleType("twb_fake_store")
            store.write_intent = lambda *args: "written"
            with patch.dict(sys.modules, {"twb_fake_store": store}), \
                    patch.object(wb, "INTENT_WRITE_HOOK", ("twb_fake_store", "write_intent")):
                check = wb._fail_intent_writes(ctx, "exec")
                check()
                self.assertEqual(ctx.witnesses["exec:intent_write_failed"],
                                 "intent-write hook twb_fake_store.write_intent was never called")
                check = wb._fail_intent_writes(ctx, "exec")
                with self.assertRaises(OSError):
                    store.write_intent("intent")
                check()
                self.assertIsNone(ctx.witnesses["exec:intent_write_failed"])
                self.assertEqual(store.write_intent("intent"), "written")  # restored after the step

    def test_failed_settle_writes_attach_after_an_applied_start_or_report_why_not(self):
        """Round-2 M3: SETTLE_WRITE_HOOK raises OSError only once the step's fake has applied an AddTraining (a
        settle write before that goes through); an absent or never-failed hook is a missing witness with its cause."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            with patch.object(wb, "SETTLE_WRITE_HOOK", ("tests.fakes.state", "no_such_settle_hook")):
                wb._fail_settle_writes(ctx, "exec")()
            self.assertIn("does not exist", ctx.witnesses["exec:settle_write_failed"])
            store = types.ModuleType("twb_fake_settle")
            store.settle = lambda *args: "settled"
            with patch.dict(sys.modules, {"twb_fake_settle": store}), \
                    patch.object(wb, "SETTLE_WRITE_HOOK", ("twb_fake_settle", "settle")):
                check = wb._fail_settle_writes(ctx, "exec")
                self.assertEqual(store.settle("before any start"), "settled")
                check()
                self.assertEqual(ctx.witnesses["exec:settle_write_failed"],
                                 "settle-write hook twb_fake_settle.settle was never called after an applied start")
                check = wb._fail_settle_writes(ctx, "exec")
                fake = ctx.fakes["alpha"]
                session = fake.session()
                add_training(session, fake, login(session, fake), ctx.idmap.id("crew", "fixer"),
                             ctx.idmap.id("course", "eng_manual"))
                self.assertTrue(fake.ledger[-1].applied)
                with self.assertRaises(OSError):
                    store.settle("after the start")
                check()
                self.assertIsNone(ctx.witnesses["exec:settle_write_failed"])
                self.assertEqual(store.settle("a later step"), "settled")  # restored after the step

    def test_a_refused_start_never_arms_the_settle_write_failure(self):
        """QA round 3 M36: an AddTraining the fake refused (not applied) leaves settle writes passing through, and
        the witness stays missing with its cause."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            store = types.ModuleType("twb_fake_settle_refused")
            store.settle = lambda *args: "settled"
            with patch.dict(sys.modules, {"twb_fake_settle_refused": store}), \
                    patch.object(wb, "SETTLE_WRITE_HOOK", ("twb_fake_settle_refused", "settle")):
                check = wb._fail_settle_writes(ctx, "exec")
                fake = ctx.fakes["alpha"]
                fake.state.resources.mineral = 0
                session = fake.session()
                add_training(session, fake, login(session, fake), ctx.idmap.id("crew", "fixer"),
                             ctx.idmap.id("course", "eng_manual"))
                self.assertEqual((fake.ledger[-1].applied, fake.ledger[-1].refusal), (False, "Not enough minerals"))
                self.assertEqual(store.settle("after a refused start"), "settled")
                check()
            self.assertEqual(ctx.witnesses["exec:settle_write_failed"], "settle-write hook "
                             "twb_fake_settle_refused.settle was never called after an applied start")

    def test_corrupting_a_store_that_holds_no_file_is_a_missing_witness(self):
        """Round-2 m1: the unreadable-state pre-state must overwrite at least one file of the store."""
        with scratch_ctx("store-stop-controls", "unreadable-state") as ctx:
            store = ctx.journals / wb.STORE_DIR / "alpha"
            store.mkdir(parents=True)
            step = {"id": "exec2", "kind": "execute", "pre": {"corrupt_state": True}}
            wb._prepare_store(ctx, step, "exec2")
            self.assertIn("holds no file", ctx.witnesses["exec2:store_artifacts"])
            (store / "attempts.json").write_text("{}", encoding="utf-8")
            wb._prepare_store(ctx, step, "exec2")
            self.assertIsNone(ctx.witnesses["exec2:store_artifacts"])
            self.assertNotEqual((store / "attempts.json").read_bytes(), b"{}")

    def test_a_stop_file_trigger_creates_the_operator_stop_during_the_run(self):
        with scratch_ctx("store-stop-controls", "stop-mid-run") as ctx:
            step = next(s for s in scenario("store-stop-controls", "stop-mid-run")["steps"] if s["id"] == "exec")
            checks = wb._arm(ctx, step, "exec")
            fake = ctx.fakes["alpha"]
            session = fake.session()
            read(session, "/CharacterService/ListAllCharactersOfUser", login(session, fake))
            for check in checks:
                check()
            self.assertTrue((ctx.journals / "STOP").exists())
            self.assertIsNone(ctx.witnesses["exec:trigger:0"])

    def test_unfired_declared_witness_fails_a_real_run(self):
        entry = copy.deepcopy(case(REQUIRED, "paid-boundary")["control"]["scenarios"][0])
        entry["steps"][1]["faults"] = [{"route": "POST /RoomService/UpgradeRoom2", "status": 404}]
        entry["witnesses"] = ["exec:fault:0"]
        result = wb.run_scenario("required", "paid-boundary", entry, REQUIRED["catalog"], REQUIRED["seeds"][0],
                                 CONTROL_CODES, policy_override="training-v1")
        self.assertFalse(result.passed)
        self.assertEqual(result.witnesses_missing, ["exec:fault:0: fault never fired"])
        self.assertEqual(result.unmet, [])


# Verifies: S14-R7
class ControlTests(unittest.TestCase):
    """Every paired training-v1 control passes on the unmodified code through the real run.main under FakePss."""

    def run_controls(self, suite, seeds):
        for item in suite["cases"]:
            for entry in item["control"]["scenarios"]:
                for seed in seeds:
                    with self.subTest(case=item["id"], scenario=entry["id"], seed=seed):
                        result = wb.run_scenario(suite["suite"], item["id"], entry, suite["catalog"], seed,
                                                 CONTROL_CODES, policy_override="training-v1")
                        self.assertTrue(result.passed, json.dumps(result.to_dict(), indent=1))

    def test_required_controls_pass_at_the_first_seed(self):
        self.run_controls(REQUIRED, REQUIRED["seeds"][:1])

    def test_holdout_controls_pass_at_the_first_seed(self):
        self.run_controls(HOLDOUT, HOLDOUT["seeds"][:1])

    def test_one_control_passes_at_every_seed(self):
        item = case(REQUIRED, "paid-boundary")
        self.run_controls({**REQUIRED, "cases": [item]}, REQUIRED["seeds"])


# Verifies: S14-R7
class OverlapTests(unittest.TestCase):
    """The overlap machinery itself, on training-v1 (whose named crew it does start): a real barrier is crossed, a
    hold is released by the other dispatch, and two processes that both start are caught as a duplicate write."""

    def overlap_scenario(self, mode, hold_timeout):
        entry = copy.deepcopy(case(REQUIRED, "overlapping-dispatch")["control"]["scenarios"][0])
        entry["accounts"][0]["world"]["concurrent_sessions"] = True  # threads share one fake account
        entry["steps"] = [{"id": "dry", "kind": "dry-run"},
                          {"id": "overlap", "kind": "overlap", "mode": mode, "dispatches": 2,
                           "barrier": {"route": "POST /UserService/DeviceLogin17", "parties": 2, "timeout_s": 10},
                           "hold": {"route": "POST /TrainingService/AddTraining", "timeout_s": hold_timeout}}]
        # v1 rules: whichever dispatch starts Delish first; the other, reading after that start, starts nothing.
        entry["expected"] = {"overlap": {"dispatches": [
            [{"alias": "alpha", "result": "started", "add_training": 1, "candidate_one_of": ["delish"]}],
            [{"alias": "alpha", "result": "skipped"}]]}}
        entry["witnesses"] = ["overlap:barrier", "overlap:hold_overlap"]
        return entry

    def run_overlap(self, mode, hold_timeout):
        """The overlap step's raw record, its witnesses and hard violations (training-v1, first seed)."""
        entry = self.overlap_scenario(mode, hold_timeout)
        wb.preimport_sdk()
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                ctx = wb.start_scenario("required", "overlapping-dispatch", entry, REQUIRED["catalog"],
                                        REQUIRED["seeds"][0], "training-v1", Path(directory))
                for step in entry["steps"]:
                    raw = wb.run_step(ctx, step)
                found = wb.hard_violations(ctx, step, entry["expected"]["overlap"], wb.normalize(raw, ctx))
            finally:
                os.chdir(cwd)
        return ctx, raw, found

    def assert_duplicate_write_matches_the_ledger(self, raw, found):
        """Whatever the candidate's locking, a duplicate write is reported exactly when two starts were sent."""
        sent = [entry for entry in raw["accounts"]["alpha"]["ledger"]
                if f"{entry['method']} {entry['path']}" == "POST /TrainingService/AddTraining"]
        self.assertTrue(sent, "no dispatch sent a start")
        self.assertEqual(any("duplicate_write" in item for item in found), len(sent) > 1, found)
        return sent

    def test_threads_cross_a_real_barrier_and_the_hold_is_released_by_the_other_dispatch(self):
        ctx, raw, found = self.run_overlap("threads", 10)
        self.assertEqual((ctx.witnesses["overlap:barrier"], ctx.witnesses["overlap:hold_overlap"]), (None, None))
        sent = self.assert_duplicate_write_matches_the_ledger(raw, found)
        self.assertLessEqual(sum(entry["applied"] for entry in sent), 1, "one shared world starts a crew once")
        # Finding 10: each dispatch's printed text is captured on its own (ThreadStreams), its accounts table included.
        self.assertEqual([("alpha" in output) for output in raw["dispatch_outputs"]], [True, True])

    def test_processes_cross_a_file_barrier_and_report_what_each_child_sent(self):
        ctx, raw, found = self.run_overlap("processes", 1)
        self.assertIsNone(ctx.witnesses["overlap:barrier"])
        self.assert_duplicate_write_matches_the_ledger(raw, found)
        self.assertEqual(len(raw["dispatch_codes"]), 2)

    def test_the_file_barrier_breaks_unless_every_party_arrives_in_time(self):
        """QA round 3 M17: a processes-mode dispatch waits at the file barrier for every party's arrive file, not only
        its own; alone it breaks the barrier at its (short) timeout."""
        with tempfile.TemporaryDirectory() as directory:
            sync = Path(directory)
            status = {"hits": 0, "broken": False}
            wb.file_barrier(sync, 0, 2, 0.2, status)
            self.assertEqual(status, {"hits": 1, "broken": True})
            self.assertTrue((sync / "arrive-0").exists())
            status = {"hits": 0, "broken": False}
            wb.file_barrier(sync, 1, 2, 10, status)  # dispatch 0 has arrived already
            self.assertEqual(status, {"hits": 1, "broken": False})

    @staticmethod
    def child_result(*, exit_code=0, ledger=(), exchanges=(), requests=0, violations=(), hits=1, broken=False,
                     hold=None, text=""):
        """One ``dispatch_child`` result JSON for account alpha."""
        return {"exit": exit_code, "stdout": f"out{text}", "stderr": f"err{text}",
                "barrier": {"hits": hits, "broken": broken}, "hold": hold, "ledger": {"alpha": list(ledger)},
                "exchanges": {"alpha": list(exchanges)}, "requests": {"alpha": requests},
                "violations": {"alpha": list(violations)}}

    def merged(self, results):
        """merge_process_results on a started processes overlap (nothing ran); returns ctx, step and raw record."""
        entry = self.overlap_scenario("processes", 1)
        with tempfile.TemporaryDirectory() as directory:
            ctx = wb.start_scenario("required", "overlapping-dispatch", entry, REQUIRED["catalog"],
                                    REQUIRED["seeds"][0], "training-v1", Path(directory))
            step = entry["steps"][1]
            raw = wb.merge_process_results(ctx, step, wb._before(ctx), results)
        return ctx, step, raw

    def test_processes_results_merge_into_one_record_with_offset_indexes_and_summed_counts(self):
        """QA round 3 M18, M28, M29 and sibling merge fields: per child, exchange and ledger indexes share one offset
        (a start followed by its roster read on the same child stays verified); requests and every currency of the
        delta are summed; violations, outputs and exit codes are kept per child in order."""
        offset = wb.PROCESS_EXCHANGE_OFFSET
        entry = self.overlap_scenario("processes", 1)
        with tempfile.TemporaryDirectory() as directory:
            ctx = wb.start_scenario("required", "overlapping-dispatch", entry, REQUIRED["catalog"],
                                    REQUIRED["seeds"][0], "training-v1", Path(directory))
            start = ledger_start(ctx, "delish", "v1_weapon_theory", starbux=-5, exchange=3)
            results = [self.child_result(exchanges=[roster_read(0)], requests=4, violations=["v0"], hold="event",
                                         text="0"),
                       self.child_result(exit_code=1, ledger=[start], requests=6, violations=["v1"], text="1",
                                         exchanges=[roster_read(5, roster=showing(ctx, "delish", "v1_weapon_theory"))])]
            step = entry["steps"][1]
            raw = wb.merge_process_results(ctx, step, wb._before(ctx), results)
            account = raw["accounts"]["alpha"]
            self.assertEqual([(e["thread"], e["exchange"]) for e in account["ledger"]], [("dispatch-1", 3 + 2 * offset)])
            self.assertEqual([(x["thread"], x["index"]) for x in account["exchanges"]],
                             [("dispatch-0", offset), ("dispatch-1", 5 + 2 * offset)])
            self.assertEqual(account["requests"], 10)
            self.assertEqual(account["delta"], {"mineral": -500, "gas": 0, "starbux": -5})
            self.assertEqual((account["violations"], account["started"]), (["v0", "v1"], {}))
            self.assertEqual(raw["dispatch_codes"], [0, 1])
            self.assertEqual(raw["dispatch_outputs"], ["out0\nerr0", "out1\nerr1"])
            self.assertEqual((ctx.witnesses["overlap:barrier"], ctx.witnesses["overlap:hold_overlap"]), (None, None))
            account["journal"] = [journal_start(ctx, "delish", "v1_weapon_theory")]
            found = wb.hard_violations(ctx, step, {}, wb.normalize(raw, ctx))
            self.assertEqual([item for item in found if "false_verification" in item], [], found)
            self.assertTrue(any("premium_spend: alpha spent 5 starbux" in item for item in found), found)

    def test_processes_barrier_and_hold_witnesses_name_what_went_wrong(self):
        ctx, _, _ = self.merged([self.child_result(hold="event"), self.child_result(broken=True)])
        self.assertEqual(ctx.witnesses["overlap:barrier"], "file barrier broken by timeout")
        ctx, _, _ = self.merged([self.child_result(hold="timeout"), self.child_result(hits=0)])
        self.assertEqual(ctx.witnesses["overlap:barrier"], "file barrier reached by 1 of 2")
        self.assertEqual(ctx.witnesses["overlap:hold_overlap"], "file hold released by ['timeout']")
        ctx, _, _ = self.merged([self.child_result(), self.child_result()])
        self.assertEqual(ctx.witnesses["overlap:hold_overlap"], "file hold never reached")

    def run_stub_overlap(self, dispatch, hold_timeout):
        """The threads overlap step alone, THREADS_ENTRY replaced by ``dispatch(fake)`` (no production code runs)."""
        entry = self.overlap_scenario("threads", hold_timeout)
        wb.preimport_sdk()
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                ctx = wb.start_scenario("required", "overlapping-dispatch", entry, REQUIRED["catalog"],
                                        REQUIRED["seeds"][0], "training-v1", Path(directory))
                with patch.object(wb._run_module(), wb.THREADS_ENTRY,
                                  new=lambda *args, **kwargs: dispatch(ctx.fakes["alpha"])):
                    raw = wb.run_step(ctx, entry["steps"][1])
            finally:
                os.chdir(cwd)
        return ctx, raw

    def test_a_hold_released_by_its_timeout_is_a_missing_witness(self):
        """QA round 2 H13: the hold witness fires only when the other dispatch released it, never on the timeout."""
        def dispatches(other_waits):
            held = threading.Event()

            def dispatch(fake):
                session = fake.session()
                token = login(session, fake)  # both dispatches cross the login barrier
                if threading.current_thread().name == "dispatch-0":
                    add_training(session, fake, token)  # the hold: dispatch-1 must finish meanwhile
                    held.set()
                elif other_waits:
                    held.wait(10)  # finishes only after the hold gave up
                return 0
            return dispatch

        ctx, _ = self.run_stub_overlap(dispatches(other_waits=True), hold_timeout=0.2)
        self.assertIsNone(ctx.witnesses["overlap:barrier"])
        self.assertEqual(ctx.witnesses["overlap:hold_overlap"], "hold released by timeout, not by the other dispatch")
        ctx, _ = self.run_stub_overlap(dispatches(other_waits=False), hold_timeout=10)
        self.assertEqual((ctx.witnesses["overlap:barrier"], ctx.witnesses["overlap:hold_overlap"]), (None, None))

    def test_each_dispatch_output_holds_only_that_dispatchs_text(self):
        """QA round 2 H14: what one dispatch printed (stdout or stderr) never lands in another's output."""
        def dispatch(fake):
            index = threading.current_thread().name.rsplit("-", 1)[1]
            session = fake.session()
            token = login(session, fake)
            print(f"stdout-only-{index}")
            print(f"stderr-only-{index}", file=sys.stderr)
            if index == "0":
                add_training(session, fake, token)
            return 0

        _, raw = self.run_stub_overlap(dispatch, hold_timeout=10)
        self.assertEqual(len(raw["dispatch_outputs"]), 2)
        for index, output in enumerate(raw["dispatch_outputs"]):
            other = 1 - index
            self.assertIn(f"stdout-only-{index}", output)
            self.assertIn(f"stderr-only-{index}", output)
            self.assertNotIn(f"stdout-only-{other}", output)
            self.assertNotIn(f"stderr-only-{other}", output)
        self.assertEqual(raw["dispatch_codes"], [0, 0])

    def test_thread_streams_keep_each_threads_text_apart(self):
        streams = ThreadStreamsProbe()
        self.assertEqual(streams.texts, {"dispatch-0": "zero\n", "dispatch-1": "one\n"})


class ThreadStreamsProbe:
    """Two named threads writing to one ThreadStreams."""

    def __init__(self):
        stream = wb.ThreadStreams()
        threads = [threading.Thread(target=lambda text=text: print(text, file=stream), name=f"dispatch-{index}")
                   for index, text in enumerate(("zero", "one"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.texts = {f"dispatch-{index}": stream.text(f"dispatch-{index}") for index in range(2)}


# Verifies: S14-R7
class StepArgvTests(unittest.TestCase):
    """Each step kind's run.py argv (INTERFACE ASSUMPTIONS item 1), with production's main() replaced by a recorder."""

    def test_account_steps_name_their_account_and_run_once_per_alias(self):
        """QA round 2 H10: provision and resolve end in ``--account <alias>``, one run per named account."""
        with scratch_ctx("distinct-accounts", "alpha-held") as ctx:
            for kind in wb.ACCOUNT_KINDS:
                argv = wb._argv(ctx, {"id": kind, "kind": kind}, wb.REGISTRY_FILE, "bravo")
                self.assertEqual(argv[-2:], ["--account", "bravo"], kind)
            for kind in ("dry-run", "crew-plan", "execute"):
                self.assertNotIn("--account", wb._argv(ctx, {"id": kind, "kind": kind}, wb.REGISTRY_FILE), kind)
            concurrent = wb._argv(ctx, {"id": "x", "kind": "execute", "concurrent": 2}, wb.REGISTRY_FILE)
            self.assertEqual(concurrent[-2:], ["--concurrent", "2"])
            for step, aliases in (({"id": "provision", "kind": "provision"}, ["alpha", "bravo"]),
                                  ({"id": "resolve", "kind": "resolve", "aliases": ["bravo"]}, ["bravo"])):
                calls = []
                with patch.object(wb, "call_main", side_effect=lambda argv, env, driver: calls.append(list(argv))
                                  or (0, "", "")):
                    wb.run_step(ctx, step)
                self.assertEqual([argv[-2:] for argv in calls], [["--account", alias] for alias in aliases])
                self.assertTrue(all(argv[0] == wb.ARGV_TEMPLATES[str(step["kind"])][0] for argv in calls))


# Verifies: S14-R7
class RobustnessTests(unittest.TestCase):
    def test_a_production_exception_is_the_steps_exit_and_the_comparison_goes_on(self):
        """Finding 9: any exception run.main lets escape is recorded as ``error <Type>``; the report survives."""
        entry = copy.deepcopy(case(REQUIRED, "role-coverage")["control"]["scenarios"][0])
        with patch.object(wb._run_module(), "main", side_effect=RuntimeError("synthetic production defect")):
            result = wb.run_scenario("required", "role-coverage", entry, REQUIRED["catalog"], REQUIRED["seeds"][0],
                                     CONTROL_CODES, policy_override="training-v1")
        self.assertFalse(result.passed)
        unmet = {u["assertion"]: u for u in result.unmet}
        self.assertEqual(unmet["exit"]["actual"], '"error RuntimeError"')
        self.assertIn("journal_planned", unmet)  # later assertions of the step were still compared
        self.assertIn("production raised RuntimeError", unmet["exit"]["cause"])


EXPECTED_KEYS = ("exit", "requests", "mutations", "other_mutations", "applied", "resource_delta", "timer", "journal",
                 "journal_planned", "store", "crew_plan", "prestige", "skips", "dispatches")


def frozen_scenarios():
    """(suite data, case, scenario, policy override) for every scenario of both checked-in fixtures, controls too."""
    for data in (REQUIRED, HOLDOUT):
        for item in data["cases"]:
            for entry in item["scenarios"]:
                yield data, item, entry, None
            for entry in item["control"]["scenarios"]:
                yield data, item, entry, item["control"]["policy_id"]


def assertion_names(expected):
    """(every assertion name compare() must emit against a fully perturbed trace, the subset an empty trace must
    fail too: all but a zero count, an empty row list or an expected absence of a timer)."""
    unknown = sorted(set(expected) - set(EXPECTED_KEYS))
    if unknown:
        raise AssertionError(f"expected keys this coverage check does not know: {unknown}")
    names, nontrivial = set(), set()

    def add(name, trivial=False):
        names.add(name)
        if not trivial:
            nontrivial.add(name)

    if "exit" in expected:
        add("exit")
    for alias, count in expected.get("requests", {}).items():
        add(f"requests.{alias}", count == 0)
    for route, rows in expected.get("mutations", {}).items():
        add(f"mutations.{route}", not rows)
    if "other_mutations" in expected:
        add("other_mutations", expected["other_mutations"] == 0)
    for route, count in expected.get("applied", {}).items():
        add(f"applied.{route}", count == 0)
    for alias in expected.get("resource_delta", {}):
        add(f"resource_delta.{alias}")
    for crew, timer in expected.get("timer", {}).items():
        add(f"timer.{crew}", timer is None)
    if expected.get("timer") == {}:
        add("timer.perturbed-crew", True)
    for key in ("journal", "journal_planned"):
        if key in expected:
            add(key, not expected[key])
    for alias in expected.get("store", {}):
        add(f"store.{alias}")
    for row in expected.get("crew_plan", ()):
        add(f"crew_plan.{row.get('crew') or row['role']}")
    for row in expected.get("prestige", ()):
        add(f"prestige.{row['target']}")
    for row in expected.get("skips", ()):
        add(f"skips.{row['alias']}.{row['code']}" + (f".{row['crew']}" if row.get("crew") else ""))
    if "dispatches" in expected:
        add("dispatches", not expected["dispatches"])
    return names, nontrivial


def neutral_trace():
    """A normalized trace in which nothing happened: no exit, no request, no output, no record."""
    return {"exit": None, "requests": {}, "mutations": {}, "applied": {}, "resource_delta": {}, "timer": {},
            "journal": [], "journal_planned": [], "store": {}, "stdout_lines": [], "texts": [],
            "journal_unreadable": [], "fake_violations": [], "raw": {"accounts": {}, "dispatch_outputs": []}}


def perturb(value):
    """``value`` with every leaf changed: an int negated (0 becomes 1, so a sign-blind comparison fails too), a
    string prefixed, a bool flipped; ``outcome_in`` and ``any_of`` become plain keys holding perturbed values."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return -value if value else 1
    if isinstance(value, str):
        return f"perturbed-{value}"
    if isinstance(value, list):
        return [perturb(item) for item in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "outcome_in":
                out["outcome"] = perturb(item[0])
            elif key == "any_of":
                for alternative in item:
                    out.update(perturb(alternative))
            else:
                out[key] = perturb(item)
        return out
    return "perturbed"


def perturbed_trace(expected):
    """A normalized trace that differs from the frozen step in every value it names (row lists keep their length, so
    a comparison that only counts rows fails too); free-text expectations meet an empty output."""
    trace = neutral_trace()
    trace["exit"] = 1 if expected.get("exit") == 0 else 0
    trace["requests"] = {alias: count + 1 for alias, count in expected.get("requests", {}).items()}
    trace["mutations"] = {route: perturb(rows) or [{"alias": "perturbed"}]
                          for route, rows in expected.get("mutations", {}).items()}
    if "other_mutations" in expected:
        trace["mutations"]["POST /Perturbed/Route"] = [{"alias": "perturbed"}] * (expected["other_mutations"] + 1)
    trace["applied"] = {route: count + 1 for route, count in expected.get("applied", {}).items()}
    trace["resource_delta"] = {alias: perturb(delta) for alias, delta in expected.get("resource_delta", {}).items()}
    trace["timer"] = {crew: perturb(timer) if timer else {"course": "perturbed", "duration_s": 1}
                      for crew, timer in expected.get("timer", {}).items()}
    if expected.get("timer") == {}:
        trace["timer"] = {"perturbed-crew": {"course": "perturbed", "duration_s": 1}}
    for key in ("journal", "journal_planned"):
        trace[key] = perturb(expected.get(key, [])) or [{"alias": "perturbed"}]
    trace["store"] = {alias: "absent" if state == "present" else "present"
                      for alias, state in expected.get("store", {}).items()}
    return trace


# Verifies: S14-R7
class OracleCoverageTests(unittest.TestCase):
    """Production-independent checks that the oracle never silently drops an assertion, a witness or a score reason
    (QA defects H1, H11, H13): adversarial traces against every frozen step of both checked-in fixtures."""

    def test_every_frozen_expectation_is_reported_unmet_by_an_adversarial_trace(self):
        steps = 0
        for data, item, entry, policy in frozen_scenarios():
            vocabulary = data["vocabulary"]
            codes = sorted(set(vocabulary["skip_codes"]) | (set(vocabulary["control_skip_codes"]) if policy else set()))
            with tempfile.TemporaryDirectory() as directory:
                ctx = wb.start_scenario(data["suite"], item["id"], entry, data["catalog"], data["seeds"][0], policy,
                                        Path(directory))
                for step_id, expected in entry["expected"].items():
                    steps += 1
                    names, nontrivial = assertion_names(expected)
                    where = f"{data['suite']}/{item['id']}/{entry['id']}/{step_id}"
                    with self.subTest(step=where, trace="perturbed"):
                        got = {u.assertion for u in wb.compare(expected, perturbed_trace(expected), ctx, step_id, codes)}
                        self.assertEqual(sorted(names - got), [])
                    with self.subTest(step=where, trace="neutral"):
                        got = {u.assertion for u in wb.compare(expected, neutral_trace(), ctx, step_id, codes)}
                        self.assertEqual(sorted(nontrivial - got), [])
        self.assertGreater(steps, 100)

    def test_every_declared_witness_is_missing_unless_it_fired(self):
        declared_total = 0
        for data, item, entry, _ in frozen_scenarios():
            declared = list(entry.get("witnesses", ()))
            declared_total += len(declared)
            with self.subTest(scenario=f"{data['suite']}/{item['id']}/{entry['id']}"):
                self.assertEqual([line.split(": ")[0] for line in wb.missing_witnesses(declared, {})], declared)
                recorded = {name: "fault never fired" for name in declared}
                self.assertEqual(wb.missing_witnesses(declared, recorded),
                                 [f"{name}: fault never fired" for name in declared])
                self.assertEqual(wb.missing_witnesses(declared, dict.fromkeys(declared)), [])
        self.assertGreater(declared_total, 10)

    def test_the_gate_names_a_reason_whenever_any_trace_fails(self):
        failures = {"unmet": {"unmet": [{"step": "s", "assertion": "exit", "expected": "0", "actual": "1",
                                          "cause": "c"}]},
                    "witness": {"witnesses_missing": ["exec:fault:0: fault never fired"]},
                    "violation": {"hard_violations": ["exec: premium_spend"]}, "error": {"errors": ["fake"]}}
        for kind, change in failures.items():
            for score in ("50.000000", "100.000000"):  # the second is inconsistent: a failing trace still counts
                good = {"pass": True, "unmet": [], "witnesses_missing": [], "hard_violations": [], "errors": []}
                bad = {**good, **change, "pass": False}
                report = {"suites": {"required": {"total": 2, "passed": 1, "score": score, "cases": {
                    "c": {"traces": {"s@1401": good, "s@1402": bad}, "control": {"pass": True, "traces": {}}}}}}}
                with self.subTest(kind=kind, score=score):
                    self.assertNotEqual(wb.gate_reasons(report, deterministic=True, subset=False), [])


def mini_suite(suite, case_id, scenarios, control):
    """A synthetic suite: the checked-in suite's vocabulary and catalog, one case of the given scenarios."""
    base = REQUIRED if suite == "required" else HOLDOUT
    data = {key: copy.deepcopy(base[key]) for key in ("schema", "suite", "declared", "authored_from", "seeds",
                                                         "vocabulary", "catalog")}
    data["cases_declared"] = [case_id]
    data["cases"] = [{"id": case_id, "requirements": ["S14-R7"], "acceptance": ["S14-AC6"],
                      "summary": "synthetic gate self-test", "red": {"expected_cause": "synthetic"},
                      "scenarios": scenarios, "control": {"policy_id": "training-v1", "scenarios": [control]}}]
    return data


def v1_dry_run(scenario_id, planned=True):
    """role-coverage's training-v1 control (Delish in the Academy) as a scored scenario; ``planned=False`` freezes
    a deliberately wrong trace (nothing planned), a semantic mismatch on any unmodified revision."""
    entry = copy.deepcopy(case(REQUIRED, "role-coverage")["control"]["scenarios"][0])
    entry["id"] = scenario_id
    if not planned:
        entry["expected"]["dry"]["journal_planned"] = []
    return entry


class StubChild:
    """Stands in for the determinism child: the same selection, run in this process instead of a second
    interpreter."""

    def __init__(self, command):
        self.args = wb.parse_args(command[2:])

    def report(self):
        suites = wb.load_suites(self.args.suite)
        return wb.to_json(wb.run_suite(suites, cases=self.args.case, probe=False,
                                       control_seed_limit=self.args.control_seeds))

    def close(self):
        pass


class VaryingChild(StubChild):
    """A determinism child whose run behaves otherwise: scenario ``loose``'s crew sits outside the Academy, so v1
    plans nothing there, while the trace (which does not pin the plan) still passes."""

    def report(self):
        suites = wb.load_suites(self.args.suite)
        for suite in suites.values():
            for item in suite["cases"]:
                for entry in item["scenarios"]:
                    for account in entry["accounts"] if entry["id"] == "loose" else ():
                        for crew in account["world"]["crew"]:
                            crew["room"] = "bridge"
        return wb.to_json(wb.run_suite(suites, cases=self.args.case, probe=False,
                                       control_seed_limit=self.args.control_seeds))


# Verifies: S14-R7
class GateTests(unittest.TestCase):
    """The gate on synthetic suites (training-v1 worlds whose behavior the spec preserves). The checked-in training-v2
    traces run in WorkflowGateTests, and seeded defects in SeededDefectRunTests; the full gate is also
    ``python scripts/training_workflow_benchmark.py --gate`` (SPEC-14 test plan)."""

    def run_gate(self, required_scenarios, *argv, holdout_scenarios=None, control=None, holdout_control=None,
                 child=None, digests=None):
        """main() on two synthetic one-case suites whose digests are pinned for the run (``digests`` overrides)."""
        required = mini_suite("required", "mini-required", required_scenarios, control or v1_dry_run("v1-control"))
        holdout = mini_suite("holdout", "mini-holdout", holdout_scenarios or [v1_dry_run("held")],
                             holdout_control or v1_dry_run("v1-held-control"))
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            paths = {"required": os.path.join(directory, "required.json"),
                     "holdout": os.path.join(directory, "holdout.json")}
            pinned = {}
            for name, data in (("required", required), ("holdout", holdout)):
                Path(paths[name]).write_text(json.dumps(data), encoding="utf-8")
                pinned[name] = hashlib.sha256(Path(paths[name]).read_bytes()).hexdigest()
            pinned.update(digests or {})
            with patch.dict(wb.FIXTURES, paths), patch.dict(wb.FIXTURE_SHA256, pinned), \
                    patch.object(wb, "REQUIRED_IDS", ("mini-required",)), \
                    patch.object(wb, "HOLDOUT_IDS", ("mini-holdout",)), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                code = wb.main(list(argv), child=child or StubChild)
        return code, out.getvalue(), err.getvalue()

    def test_a_suite_whose_every_trace_matches_passes_the_gate(self):
        code, out, err = self.run_gate([v1_dry_run("matches")], "--gate", "--no-probe")
        self.assertEqual(code, wb.EXIT_OK, out + err)
        self.assertIn("required: score 100.000000 (3 of 3 traces pass)", out)
        self.assertNotIn("Gate: FAIL", out)

    def test_a_semantic_mismatch_fails_the_gate_naming_the_assertion_its_cause_and_the_control(self):
        code, out, err = self.run_gate([v1_dry_run("matches"), v1_dry_run("mismatch", planned=False)],
                                       "--gate", "--no-probe")
        self.assertEqual(code, wb.EXIT_GATE, err)
        self.assertIn("required: score 50.000000 (3 of 6 traces pass)", out)
        self.assertRegex(out, r"FAIL required/mini-required/mismatch seed=1401 step=dry journal_planned: expected \[\] "
                              r"actual \[.*\] -- cause: behavioral mismatch .* \| control training-v1: PASS")
        self.assertIn("PASS required/mini-required/matches seed=1403 | control training-v1: PASS", out)
        self.assertIn("- required: score 50.000000 (3 of 6 traces pass)", out)
        self.assertNotIn("nondeterministic", out)

    def test_differing_behavior_with_equal_pass_status_is_nondeterministic(self):
        """Round-2 M1: every run passes a trace that does not pin the plan, but the child's run plans otherwise (as a
        candidate whose choice follows the hash seed or reply order would): the step digests differ, so it fails."""
        loose = v1_dry_run("loose")
        del loose["expected"]["dry"]["journal_planned"]
        code, out, err = self.run_gate([loose], "--gate", "--no-probe")
        self.assertEqual(code, wb.EXIT_OK, out + err)
        code, out, err = self.run_gate([loose], "--gate", "--no-probe", child=VaryingChild)
        self.assertEqual(code, wb.EXIT_GATE, out + err)
        self.assertIn("required: score 100.000000 (3 of 3 traces pass)", out)
        self.assertIn("- nondeterministic: repeated runs produced different scored reports", out)

    def test_a_failing_control_fails_the_gate_even_when_every_trace_passes(self):
        code, out, _ = self.run_gate([v1_dry_run("matches")], "--gate", "--no-probe",
                                     control=v1_dry_run("v1-control", planned=False))
        self.assertEqual(code, wb.EXIT_GATE)
        self.assertIn("CONTROL-FAIL required/mini-required/v1-control seed=1401 step=dry journal_planned", out)
        self.assertIn("- required/mini-required: training-v1 control failed", out)

    def test_a_failing_holdout_control_fails_the_gate(self):
        """QA round 2 H8: a holdout control counts as much as a required one."""
        code, out, _ = self.run_gate([v1_dry_run("matches")], "--gate", "--no-probe",
                                     holdout_control=v1_dry_run("v1-held-control", planned=False))
        self.assertEqual(code, wb.EXIT_GATE, out)
        self.assertIn("- holdout/mini-holdout: training-v1 control failed", out)
        self.assertNotIn("- required/mini-required: training-v1 control failed", out)

    def test_every_suite_names_every_reason_whether_or_not_its_trace_passes(self):
        """QA round 2 H8 and H11, and their siblings: no reason is scoped to one suite or to a passing trace."""
        for name in ("required", "holdout"):
            for passed in (True, False):
                with self.subTest(suite=name, trace_pass=passed):
                    trace = {"pass": passed, "unmet": [] if passed else ["exec.journal_planned"],
                             "witnesses_missing": ["exec:fault:0: fault never fired"],
                             "hard_violations": ["exec: premium_spend: alpha spent 1 starbux"], "errors": []}
                    suite = {"total": 1, "passed": int(passed), "score": "100.000000" if passed else "0.000000",
                             "cases": {"c": {"traces": {"s@1401": trace}, "control": {"pass": False, "traces": {}}}}}
                    reasons = wb.gate_reasons({"suites": {name: suite}}, deterministic=True, subset=False)
                    expected = [] if passed else [f"{name}: score 0.000000 (0 of 1 traces pass)"]
                    expected += [f"{name}/c/s@1401: missing witness", f"{name}/c/s@1401: hard violation",
                                 f"{name}/c: training-v1 control failed"]
                    self.assertEqual(reasons, expected)
            stale = {"total": 1, "passed": 1, "score": "100.000000", "cases": {"c": {"traces": {"s@1401": {
                "pass": False, "unmet": ["x"], "witnesses_missing": [], "hard_violations": [], "errors": []}},
                "control": {"pass": True, "traces": {}}}}}
            self.assertEqual(wb.gate_reasons({"suites": {name: stale}}, deterministic=True, subset=False),
                             [f"{name}: 1 failing traces under a score of 100.000000"])
            empty = {"total": 0, "passed": 0, "score": "0.000000", "cases": {}}
            self.assertEqual(wb.gate_reasons({"suites": {name: empty}}, deterministic=True, subset=False),
                             [f"{name}: empty suite", f"{name}: score 0.000000 (0 of 0 traces pass)"])

    def test_a_subset_never_passes_and_report_only_never_gates(self):
        code, out, _ = self.run_gate([v1_dry_run("matches")], "--gate", "--no-probe", "--suite", "required")
        self.assertEqual(code, wb.EXIT_GATE)
        self.assertIn("subset: only some cases ran", out)
        code, out, _ = self.run_gate([v1_dry_run("mismatch", planned=False)], "--report-only", "--json", "-")
        self.assertEqual(code, wb.EXIT_OK)
        report = json.loads(out)
        self.assertEqual(report["suites"]["required"]["score"], "0.000000")
        self.assertTrue(report["suites"]["required"]["cases"]["mini-required"]["probe"])
        self.assertNotIn("probe", wb.scored_view(report)["suites"]["required"]["cases"]["mini-required"])

    def test_a_json_report_written_to_a_file_is_the_whole_report(self):
        """QA round 4 S16: ``--json PATH`` writes what ``--json -`` prints, probe text included (only the
        determinism comparison drops it)."""
        argv = ("--report-only", "--suite", "required", "--case", "mini-required")
        code, printed, _ = self.run_gate([v1_dry_run("mismatch", planned=False)], *argv, "--json", "-")
        self.assertEqual(code, wb.EXIT_OK)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "report.json")
            code, out, _ = self.run_gate([v1_dry_run("mismatch", planned=False)], *argv, "--json", path)
            written = Path(path).read_text(encoding="utf-8")
        self.assertEqual(code, wb.EXIT_OK)
        self.assertEqual(written, printed)
        self.assertTrue(json.loads(written)["suites"]["required"]["cases"]["mini-required"]["probe"])
        self.assertIn("required: score 0.000000", out)  # the text report still goes to stdout

    def test_the_determinism_child_runs_the_same_selection(self):
        """Sibling of QA round 4 S13: the child gets the parent's --suite, every --case and --control-seeds, and
        reports JSON on stdout without probes."""
        command = wb.child_command(wb.parse_args(["--gate", "--suite", "holdout", "--case", "x", "--case", "y",
                                                  "--control-seeds", "1"]))
        self.assertEqual(command[:2], [sys.executable, os.path.abspath(wb.__file__)])
        self.assertEqual(command[2:], ["--report-only", "--json", "-", "--quiet", "--no-probe", "--suite", "holdout",
                                       "--case", "x", "--case", "y", "--control-seeds", "1"])
        self.assertEqual(wb.child_command(wb.parse_args(["--gate"]))[-2:], ["--suite", "all"])

    def test_the_determinism_child_runs_under_another_fixed_hash_seed(self):
        """QA round 4 S12: whatever this process's PYTHONHASHSEED (none, 0, 1 or 7), the child's is fixed and
        differs from it, and ChildRun starts the child with it."""
        for parent in (None, "0", "1", "7", "random"):
            with self.subTest(parent=parent), patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PYTHONHASHSEED", None)
                if parent is not None:
                    os.environ["PYTHONHASHSEED"] = parent
                seed = wb.child_hash_seed()
                self.assertIn(seed, ("1", "2"))
                self.assertNotEqual(seed, parent)
                started = {}

                def popen(command, **kwargs):
                    started.update(kwargs, command=command)
                    return types.SimpleNamespace(poll=lambda: 0)

                wb.ChildRun(["child"], popen=popen)
                self.assertEqual(started["env"]["PYTHONHASHSEED"], seed)
                self.assertEqual(started["env"][wb.CHILD_ENV], "1")
                self.assertEqual(started["command"], ["child"])

    def test_a_fixture_error_exits_2(self):
        code, _, err = self.run_gate([v1_dry_run("dup"), v1_dry_run("dup")], "--gate", "--no-probe")
        self.assertEqual(code, wb.EXIT_ERROR)
        self.assertIn("fixture error:", err)

    def test_hard_violations_and_missing_witnesses_fail_the_gate_whatever_the_score(self):
        trace = {"pass": True, "unmet": [], "witnesses_missing": [], "hard_violations": [], "errors": []}
        report = {"suites": {"required": {"total": 1, "passed": 1, "score": "100.000000", "cases": {
            "c": {"traces": {"s@1401": trace}, "control": {"pass": True, "traces": {}}}}}}}
        self.assertEqual(wb.gate_reasons(report, deterministic=True, subset=False), [])
        trace["hard_violations"] = ["exec: premium_spend: alpha spent 5 starbux"]
        trace["witnesses_missing"] = ["exec:fault:0: fault never fired"]
        self.assertEqual(wb.gate_reasons(report, deterministic=False, subset=False),
                         ["nondeterministic: repeated runs produced different scored reports",
                          "required/c/s@1401: missing witness", "required/c/s@1401: hard violation"])

    def test_a_second_in_process_run_that_differs_is_nondeterministic_even_when_the_child_agrees(self):
        """QA survivor H14: the two in-process runs differ only in one step's behavior digest and the child echoes
        the first, so only the in-process comparison can catch it."""
        def report(digest):
            trace = {"pass": True, "unmet": [], "witnesses_missing": [], "hard_violations": [], "errors": [],
                     "behavior": {"dry": digest}}
            suite = {"seeds": [1401], "passed": 1, "total": 1, "score": "100.000000", "cases": {
                "c": {"traces": {"s@1401": trace}, "control": {"pass": True, "traces": {}},
                      "expected_red_cause": "synthetic"}}}
            return {"schema": wb.SCHEMA, "suites": {"required": suite, "holdout": copy.deepcopy(suite)}}

        class EchoChild:
            def __init__(self, *_):
                pass

            def report(self):
                return wb.to_json(report("aaaa"))

            def close(self):
                pass

        with patch.object(wb, "run_suite", side_effect=[report("aaaa"), report("aaaa")]):
            self.assertEqual(wb.check_determinism({}, start_child=EchoChild), (True, report("aaaa")))
        with patch.object(wb, "run_suite", side_effect=[report("aaaa"), report("bbbb")]):
            self.assertEqual(wb.check_determinism({}, start_child=EchoChild), (False, report("aaaa")))
        with patch.object(wb, "run_suite", side_effect=[report("aaaa"), report("bbbb")]):
            code, out, err = self.run_gate([v1_dry_run("matches")], "--gate", "--no-probe", child=EchoChild)
        self.assertEqual(code, wb.EXIT_GATE, out + err)
        self.assertEqual(out.split("Gate: FAIL\n")[1], "- nondeterministic: repeated runs produced different scored "
                                                       "reports\n")

    def test_the_gate_checks_the_pinned_fixture_digests_before_running_anything(self):
        """A fixture edited without an approved digest update stops ``--gate`` itself (exit 2), not only the tests."""
        with patch.object(wb, "run_suite", side_effect=AssertionError("the suite ran")):
            code, _, err = self.run_gate([v1_dry_run("matches")], "--gate", "--no-probe",
                                         digests={"holdout": "0" * 64})
        self.assertEqual(code, wb.EXIT_ERROR, err)
        self.assertIn("fixture error: holdout fixture", err)
        self.assertIn("does not match its pinned FIXTURE_SHA256", err)
        edited = Path(wb.FIXTURES["required"]).read_text(encoding="utf-8").replace('"mineral": -500', '"mineral": 0', 1)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "workflow_benchmark.json")
            Path(path).write_text(edited, encoding="utf-8")
            err = io.StringIO()
            with patch.dict(wb.FIXTURES, {"required": path}), contextlib.redirect_stderr(err), \
                    patch.object(wb, "run_suite", side_effect=AssertionError("the suite ran")):
                code = wb.main(["--gate", "--suite", "required"], child=StubChild)
        self.assertEqual(code, wb.EXIT_ERROR, err.getvalue())
        self.assertIn("required fixture", err.getvalue())

    def test_the_fakes_strict_rules_pass_the_gates_self_check(self):
        self.assertEqual(wb.fake_self_check(), [])

    def test_the_self_check_compares_the_refusal_in_the_ledger_and_in_the_reply(self):
        """QA round 3 M22: a refusal case fails when the ledger entry records another refusal (the reply still right)
        and when the reply names another refusal (the ledger still right)."""
        record_ledger, envelope = FakePss._record_ledger, pss_server.error_envelope

        def full(text):
            return "Room is full" if text == "Room level is too low" else text

        def ledger_says_full(fake, exchange, applied, refusal, before, session):
            return record_ledger(fake, exchange, applied, full(refusal), before, session)

        with patch.object(FakePss, "_record_ledger", ledger_says_full):
            failures = wb.fake_self_check()
        self.assertEqual([f.split(" (")[0] for f in failures], ["room level refused: ledger entry"], failures)
        with patch.object(pss_server, "error_envelope", lambda service, element, text: envelope(service, element,
                                                                                                  full(text))):
            failures = wb.fake_self_check()
        self.assertEqual(len(failures), 1, failures)
        self.assertTrue(failures[0].startswith("room level refused: AddTraining refusal text"), failures)
        self.assertIn("expected 'Room level is too low'", failures[0])

    def test_a_defect_in_the_fakes_strict_rules_stops_the_gate_with_exit_2(self):
        """The gate self-checks the fake's pricing and refusal rules before running the suite (QA: in the red stage
        no control charges a priced course, so the suite alone would not notice)."""
        def minerals_for_everything(fake, price):  # QA defect F2: every non-starbux price taken from minerals
            if price is not None and price[0] != "starbux":
                if price[1] > fake.state.resources.mineral:
                    raise _Reject("Not enough minerals")
                fake.state.resources.mineral -= price[1]

        defects = {"gas price charged as gas": ("_charge", minerals_for_everything),
                   "mineral price charged as minerals": ("_charge", lambda fake, price: None),
                   "room level refused": ("_strict_training_checks", lambda fake, crew, design: None),
                   "insufficient minerals refused: 0 AddTraining ledger entries": ("_record_ledger",
                                                                                   lambda fake, *args: None)}
        for name, (method, replacement) in defects.items():
            with self.subTest(defect=method, check=name), patch.object(FakePss, method, replacement), \
                    patch.object(wb, "run_suite", side_effect=AssertionError("the suite ran")):
                code, out, err = self.run_gate([v1_dry_run("matches")], "--gate", "--no-probe")
                self.assertEqual(code, wb.EXIT_ERROR, out + err)
                self.assertIn("fake self-check failed:", err)
                self.assertIn(name, err)

    def test_the_gate_refuses_to_run_while_a_defect_is_active(self):
        """S14-AC7: a seeded defect never reaches a gate run; the gate stops with exit 2 instead of scoring it."""
        with patch.object(wb, "_ACTIVE_DEFECT", "two-starts"), \
                patch.object(wb, "run_suite", side_effect=AssertionError("the suite ran")):
            code, out, err = self.run_gate([v1_dry_run("matches")], "--gate", "--no-probe")
        self.assertEqual(code, wb.EXIT_ERROR, out + err)
        self.assertIn("oracle error: a seeded defect is active (two-starts); --gate never runs with one", err)

    def test_the_gate_child_and_dispatch_children_never_carry_defect_state(self):
        """The determinism child's argv and environment carry no defect: a defect reaches a dispatch child only
        through its spec JSON, and only while one is active in this process."""
        command = wb.child_command(wb.parse_args(["--gate"]))
        self.assertFalse([part for part in command if part.startswith("--defect")], command)
        started = {}

        def popen(command, **kwargs):
            started.update(kwargs)
            return types.SimpleNamespace(poll=lambda: 0, communicate=lambda timeout=None: ("", ""))

        wb.ChildRun(command, popen=popen)
        self.assertFalse([key for key in started["env"] if "DEFECT" in key.upper()])
        self.assertIsNone(wb.active_defect())
        self.assertEqual(wb.dispatch_defect_field(), {})


# Verifies: S14-R7
class ScenarioInputTests(unittest.TestCase):
    """QA round 5 survivors: v2-only scenario inputs and diagnosis code the checked-in suites never reach."""

    def test_course_overrides_replace_the_catalog_price_and_charge(self):
        """B4: a scenario's course_overrides win over the catalog row of that course."""
        entry = copy.deepcopy(scenario("paid-boundary", "exact-500"))
        entry["course_overrides"] = {"eng_manual": {"price": "starbux:5", "charge": "mineral:7"}}
        idmap = wb.IdMap()
        world = wb.build_world(REQUIRED["catalog"], entry, entry["accounts"][0], 1401, wb.real_now(), idmap)
        course = world.training_design(idmap.id("course", "eng_manual"))
        self.assertEqual((course.price, course.charge), ("starbux:5", "mineral:7"))

    def test_room_operations_reach_the_account_that_owns_the_room(self):
        """B6: build_world records each room's owner, so a mutate step's room op edits that account's world only."""
        with scratch_ctx("distinct-accounts", "alpha-held") as ctx:
            self.assertEqual((ctx.idmap.owners["room:academy"], ctx.idmap.owners["room:academy_b"]),
                             ("alpha", "bravo"))
            wb.run_step(ctx, {"id": "m", "kind": "mutate", "set": [{"room": "academy_b", "status": "Upgrading"}]})
            self.assertEqual(ctx.fakes["bravo"].state.room(ctx.idmap.id("room", "academy_b")).status, "Upgrading")
            self.assertEqual(ctx.fakes["alpha"].state.room(ctx.idmap.id("room", "academy")).status, "Normal")

    def test_a_registry_variant_overrides_the_accounts_base_registry(self):
        """R1: the step's registry_variant wins over the account's own registry keys."""
        account = dict(scenario("paid-boundary", "exact-500")["accounts"][0], registry={"max_actions": 5})
        [base] = wb.registry_payload([account])["accounts"]
        [varied] = wb.registry_payload([account], variant={account["alias"]: {"max_actions": 19}})["accounts"]
        self.assertEqual((base["max_actions"], varied["max_actions"]), (5, 19))

    def test_a_crash_step_arms_the_fake_with_its_apply_flag(self):
        """C1: the crash's apply flag reaches FakePss.crash (after the handler when true, before it otherwise)."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            for spec, applied in (({"route": wb.ADD_TRAINING, "apply": True}, True),
                                  ({"route": wb.ADD_TRAINING, "apply": False}, False),
                                  ({"route": wb.ADD_TRAINING}, False)):
                checks = wb._arm(ctx, {"id": "s", "kind": "execute", "crash": spec}, "s")
                crash = ctx.fakes["alpha"].crashes[-1]
                self.assertEqual((crash.method, crash.path, crash.apply), ("POST", ADD, applied), spec)
                for check in checks:
                    check()

    def test_a_simulated_crash_is_recorded_as_crashed_and_diagnosed(self):
        """C2 and D1: SimulatedCrash out of run.main is the exit "crashed" (never an int or a production error), and
        step_cause names the armed crash."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            with patch.object(wb._run_module(), "main", side_effect=SimulatedCrash("armed request")):
                code, _out, err = wb.call_main(["--help"], ctx.env, ctx.driver)
        self.assertEqual(code, "crashed")
        self.assertNotIn("production raised", err)
        self.assertEqual(wb.step_cause({"exit": "crashed", "stderr": ""}), "simulated crash at the armed request")
        self.assertTrue(wb.step_cause({"exit": 1, "stderr": ""}).startswith("behavioral mismatch"))

    def test_a_step_reads_only_the_run_records_it_wrote(self):
        """J3: run records an earlier step left in runs/ are never read again by a later step."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            written = []

            def main(argv, env, driver):
                written.append(len(written) + 1)
                runs = ctx.journals / "runs"
                runs.mkdir(exist_ok=True)
                record = {"accounts": [{"alias": "alpha", "status": f"step{written[-1]}"}]}
                (runs / f"{written[-1]}.json").write_text(json.dumps(record), encoding="utf-8")
                return 0, "", ""

            with patch.object(wb, "call_main", side_effect=main):
                first = wb.run_step(ctx, {"id": "e1", "kind": "execute"}) or {}
                second = wb.run_step(ctx, {"id": "e2", "kind": "execute"}) or {}
        self.assertEqual(first["records"], [("alpha", "status: step1")])
        self.assertEqual(second["records"], [("alpha", "status: step2")])

    def test_an_account_steps_exit_is_the_first_nonzero_run(self):
        """E1: a provision/resolve step runs once per account; its exit is the first nonzero one, else 0."""
        with scratch_ctx("distinct-accounts", "alpha-held") as ctx:
            for exits, expected in (((2, 0), 2), ((0, 3), 3), ((4, 5), 4), ((0, 0), 0)):
                replies = iter(exits)
                with patch.object(wb, "call_main", side_effect=lambda argv, env, driver: (next(replies), "", "")):
                    raw = wb.run_step(ctx, {"id": "p", "kind": "provision"}) or {}
                self.assertEqual(raw["exit"], expected, exits)

    def test_each_step_moves_the_fake_clock_to_real_now_never_past_or_back(self):
        """K1: _set_clock puts the fake clock exactly at real now when that is later, and never moves it back."""
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            clock = ctx.fakes["alpha"].clock
            later = clock.now + HOUR
            with patch.object(wb, "call_main", return_value=(0, "", "")):
                with patch.object(wb, "real_now", return_value=later):
                    wb.run_step(ctx, {"id": "e1", "kind": "execute"})
                self.assertEqual(clock.now, later)
                with patch.object(wb, "real_now", return_value=later - HOUR):
                    wb.run_step(ctx, {"id": "e2", "kind": "execute"})
                self.assertEqual(clock.now, later)

    def test_the_cost_string_advertises_the_price_never_the_charge(self):
        """X4: CostString is the course's advertised price; a course with only a charge advertises none."""
        world = paid_world()
        world.training_designs[0].charge = "mineral:700"
        world.training_designs[1].charge = "mineral:7"
        session = FakePss(world).session()
        courses = xmltodict.parse(read(session, "/TrainingService/ListAllTrainingDesigns2", designVersion="14"))
        manual, drills = courses["TrainingService"]["ListAllTrainingDesigns"]["TrainingDesigns"]["TrainingDesign"]
        self.assertEqual(manual["@CostString"], "mineral:500")
        self.assertNotIn("@CostString", drills)


# The seeded-defect catalogue's ids (S14-AC7, SPEC-14 step 5): every spec defect, the crew-policy defects and the
# three published-only (non-critical) ones.
REQUIRED_DEFECT_IDS = (
    "two-starts", "guard-latch-off", "lock-skipped", "intent-not-persisted", "intent-after-send", "stop-ignored",
    "scheduler-stop-ignored", "missing-store-fresh", "unreadable-store-fresh", "settle-loses-pending",
    "v1-under-v2-path", "v2-down-v1-path", "unresolved-guard-only", "unresolved-ignored", "premium-spend",
    "false-verification", "funds-plus-one", "funds-minus-one", "reservation-ignored", "recheck-off",
    "recheck-off-frozen", "price-under", "price-over", "currency-swapped", "zero-price-unknown", "zero-stat-unknown",
    "greedy-roles", "tie-high-id", "tie-input-order", "two-copy-as-one", "wrong-stat-course")
NON_CRITICAL_DEFECT_IDS = ("guard-latch-off", "recheck-off-frozen", "tie-input-order")
# QA round 1 (SPEC-14 step 5): each planted production defect the workflow channel now kills, by its catalogue id.
QA1_DEFECT_IDS = {"D3": "settle-failure-swallowed", "D4": "training-block-dropped", "D5": "runner-cap-window",
                  "D7": "resolve-ignores-lock", "D8": "malformed-attempt-dropped", "D9": "recheck-spend-frozen",
                  "D12": "strict-spec-dropped", "D13": "stop-checked-once", "D15": "currency-blind-recheck", "D16": "journal-witness-wrong-key",
                  "D17": "provision-overwrites-unmarked"}
# QA round 2: the planted defects killed in the workflow channel (the others are named in the SPEC-14 doc section).
QA2_DEFECT_IDS = {"D1": "lock-keyed-by-alias", "D2": "resolution-links-intent", "D3": "verified-still-blocks",
                  "D5": "lock-full-mode-only", "D7": "store-write-not-atomic",
                  "D11": "store-unreadable-as-missing", "D13": "dry-run-age-by-day", "D14": "unobserved-funds-enough",
                  "D15": "v1-uncertainty-ignored-under-guard"}
ADD_MUTATION = f"exec:mutations.{wb.ADD_TRAINING}"
SUITES = {"required": REQUIRED, "holdout": HOLDOUT}


def first_seed(target):
    return SUITES[target.suite]["seeds"][0]


def synthetic_result(unmet=(), hard=(), errors=()):
    rows = [dict({"expected": None, "actual": None, "cause": "x"}, **row) for row in unmet]
    return wb.TraceResult(not rows and not hard and not errors, rows, [], list(hard), list(errors))


def observed(result):
    """A detection failure's diagnosis: what the run did show."""
    return (f"unmet={sorted({u['step'] + ':' + u['assertion'] for u in result.unmet})} "
            f"hard={result.hard_violations} errors={result.errors[:2]}")


# Verifies: S14-R7
class SeededDefectCatalogueTests(unittest.TestCase):
    """S14-AC7: the seeded-defect catalogue itself, its targets and the detection rule (no scenario runs)."""

    def test_catalogue_ids_are_unique_and_cover_every_spec_defect(self):
        ids = [item.id for item in wb.SEEDED_DEFECTS]
        self.assertEqual(len(ids), len(set(ids)), ids)
        self.assertLessEqual(set(REQUIRED_DEFECT_IDS), set(ids))
        self.assertEqual({item.id for item in wb.SEEDED_DEFECTS if not item.critical}, set(NON_CRITICAL_DEFECT_IDS))
        for item in wb.SEEDED_DEFECTS:
            with self.subTest(defect=item.id):
                self.assertTrue(item.title)
                if item.critical:
                    self.assertTrue(item.targets)
                for target in item.targets:
                    self.assertTrue(target.expect or target.hard, target.label)

    def test_every_target_resolves_to_a_frozen_scenario_and_names_its_steps(self):
        for item in wb.SEEDED_DEFECTS:
            for target in item.targets:
                with self.subTest(defect=item.id, target=target.label):
                    entry, codes, policy = wb.target_scenario(target, SUITES)
                    data = SUITES[target.suite]
                    frozen_case = case(data, target.case_id)
                    pool = frozen_case["control"]["scenarios"] if target.control else frozen_case["scenarios"]
                    frozen = next(s for s in pool if s["id"] == target.scenario_id)
                    if target.derive:
                        self.assertEqual(entry["id"], f"{target.scenario_id}~{target.derive[0]}")
                        self.assertIsNot(entry, frozen)
                    else:
                        self.assertIs(entry, frozen)
                    self.assertEqual(policy, frozen_case["control"]["policy_id"] if target.control else None)
                    self.assertLessEqual(set(data["vocabulary"]["skip_codes"]), set(codes))
                    steps = {step["id"] for step in entry["steps"]}
                    for item_text in target.expect:
                        step, assertion = item_text.split(":", 1)
                        self.assertIn(step, steps, item_text)
                        self.assertTrue(assertion, item_text)
                    for item_text in target.hard:
                        step, code = item_text.split(":", 1)
                        self.assertIn(step, steps, item_text)
                        self.assertIn(code, wb.HARD_CODES, item_text)

    def test_a_derived_variant_is_built_in_code_and_leaves_the_frozen_fixtures_unchanged(self):
        suite = copy.deepcopy(REQUIRED)
        frozen = case(suite, "stale-preflight")["scenarios"]
        price = next(s for s in frozen if s["id"] == "price-raised")
        untouched = copy.deepcopy(price)
        derived = wb.derived_scenario(price, ("nth3", wb.trigger_after(3)))
        self.assertEqual(derived["id"], "price-raised~nth3")
        triggers = [t for step in derived["steps"] for t in step.get("triggers", ())]
        self.assertTrue(triggers)
        self.assertEqual({t["after_nth"] for t in triggers}, {3})
        self.assertEqual(price, untouched)
        wb.verify_fixture_digests(["required", "holdout"])
        room_full = next(s for s in frozen if s["id"] == "room-full")
        with self.assertRaises(ValueError):
            wb.derived_scenario(room_full, ("nth2", wb.trigger_after(2)))

    def test_detection_requires_every_named_assertion_and_hard_code_and_no_crash_or_error(self):
        target = wb.DefectTarget("required", "paid-boundary", "exact-500", expect=(ADD_MUTATION,),
                                 hard=("exec:duplicate_write",))
        add = {"step": "exec", "assertion": f"mutations.{wb.ADD_TRAINING}"}
        duplicate = "exec: duplicate_write: 2 AddTraining sent on alpha in one step"
        self.assertEqual(wb.detection(target, synthetic_result([add], [duplicate])), (True, []))
        self.assertEqual(wb.detection(target, synthetic_result([], [duplicate])), (False, [ADD_MUTATION]))
        self.assertFalse(wb.detection(target, synthetic_result(
            [add], ["exec2: duplicate_write: 2 AddTraining sent on alpha in one step"]))[0])
        self.assertFalse(wb.detection(target, synthetic_result([add], [duplicate], ["FakePss error x"]))[0])
        for actual in ("crashed", "error RuntimeError"):
            crash = {"step": "exec", "assertion": "exit", "expected": 0, "actual": actual}
            with self.subTest(actual=actual):
                self.assertFalse(wb.detection(target, synthetic_result([add, crash], [duplicate]))[0])
        expected_crash = wb.DefectTarget("required", "persisted-intent-restart", "crash-after-send",
                                         expect=("exec2:journal",))
        self.assertEqual(wb.detection(expected_crash, synthetic_result([{"step": "exec2", "assertion": "journal"}])),
                         (True, []))
        glob = wb.DefectTarget("holdout", "two-copy-recipe", "one-training", expect=("plan:prestige.*",))
        self.assertEqual(wb.detection(glob, synthetic_result([{"step": "plan", "assertion": "prestige.x"}])),
                         (True, []))
        self.assertFalse(wb.detection(glob, synthetic_result([{"step": "dry", "assertion": "prestige.x"}]))[0])

    def test_every_qa_round_1_kill_is_a_critical_catalogue_defect_with_targets(self):
        """QA round 1: each planted defect killed in the workflow channel is a critical catalogue defect, so
        ``--defects`` fails unless it is caught on every trace at every seed."""
        for qa_id, defect_id in sorted(QA1_DEFECT_IDS.items()):
            with self.subTest(qa=qa_id, defect=defect_id):
                item = wb.defect(defect_id)
                self.assertTrue(item.critical)
                self.assertTrue(item.targets)

    def test_every_qa_round_2_kill_is_a_critical_catalogue_defect_with_a_derived_target(self):
        """QA round 2: each planted defect killed in the workflow channel is a critical catalogue defect whose
        targets are derived variants (a frozen trace never moved), so ``--defects`` fails unless every seed sees it."""
        for qa_id, defect_id in sorted(QA2_DEFECT_IDS.items()):
            with self.subTest(qa=qa_id, defect=defect_id):
                item = wb.defect(defect_id)
                self.assertTrue(item.critical)
                self.assertTrue(item.targets)
                self.assertTrue(all(target.derive for target in item.targets))
                self.assertIn(f"QA2 {qa_id}", item.note)

    def test_every_seeding_declares_the_production_attributes_it_patches(self):
        """The seam-liveness check spies on what each seeding declares: a catalogue seeding declares at least one
        seam, and each names an attribute production has now."""
        for item in wb.SEEDED_DEFECTS:
            with self.subTest(defect=item.id):
                seams = wb.seams(item)
                self.assertTrue(seams)
                for module_name, dotted in seams:
                    owner = wb._module(module_name)
                    for part in dotted.split("."):
                        owner = getattr(owner, part)

    def test_derived_only_step_options_are_refused_in_a_fixture(self):
        """The derived-variant options (store marker removal, a corrupted attempt, a held lock and a timer that runs
        out) never change a frozen trace: a fixture naming one is refused."""
        for key in sorted(wb.DERIVED_PRE_KEYS):
            suite = copy.deepcopy(REQUIRED)
            scenario = case(suite, "paid-boundary")["scenarios"][0]
            scenario["steps"][-1].setdefault("pre", {})[key] = True
            with self.subTest(pre=key), self.assertRaises(wb.FixtureError):
                wb.validate_suite(suite)
        suite = copy.deepcopy(REQUIRED)
        price = next(s for s in case(suite, "stale-preflight")["scenarios"] if s["id"] == "price-raised")
        price["steps"][-1]["triggers"][0]["apply"] = [{"timer_over": "fixer"}]
        with self.assertRaises(wb.FixtureError):
            wb.validate_suite(suite)

    def test_the_scheduler_stop_defect_also_covers_a_stop_between_two_actions(self):
        """QA round 1 D13: a stop file created after the first action of a two-action plan must stop the second."""
        labels = {target.label for target in wb.defect("scheduler-stop-ignored").targets}
        self.assertIn("required/paid-boundary/earlier-reservation~stop-after-upgrade", labels)
        self.assertEqual({target.label for target in wb.defect("stop-checked-once").targets},
                         {"required/paid-boundary/earlier-reservation~stop-after-upgrade"})

    def test_activating_a_defect_patches_at_call_time_and_restores_on_exit(self):
        import sdk.fleet.attempts as attempts

        original = attempts.FileAccountLock.try_acquire
        self.assertIsNone(wb.active_defect())
        with wb.defect("lock-skipped").active():
            self.assertIsNot(attempts.FileAccountLock.try_acquire, original)
            self.assertEqual(wb.active_defect(), "lock-skipped")
            self.assertEqual(wb.dispatch_defect_field(), {"defect": "lock-skipped"})
            with self.assertRaises(wb.OracleError):
                with wb.defect("two-starts").active():
                    pass
            self.assertEqual(wb.active_defect(), "lock-skipped")
        self.assertIs(attempts.FileAccountLock.try_acquire, original)
        self.assertIsNone(wb.active_defect())
        self.assertEqual(wb.dispatch_defect_field(), {})
        with self.assertRaises(KeyError):
            wb.defect("nope")


# Verifies: S14-R7
class SeededDefectRunTests(unittest.TestCase):
    """S14-AC7: each critical defect, seeded into production at call time, is caught by a named assertion or hard
    code on every target at the suite's first seed; each derived variant passes unpatched first (the clean half).
    One test per critical defect is generated below the class, so each defect appears by name in the output."""

    def test_every_derived_variant_passes_on_the_clean_revision(self):
        targets = {target.label: target for item in wb.SEEDED_DEFECTS for target in item.targets if target.derive}
        self.assertTrue(targets)
        for label, target in sorted(targets.items()):
            with self.subTest(target=label):
                result = wb.run_target(target, SUITES, first_seed(target))
                self.assertTrue(result.passed, observed(result))
                self.assertEqual((result.unmet, result.hard_violations, result.errors), ([], [], []))

    def check_caught(self, defect_id):
        item = wb.defect(defect_id)
        self.assertTrue(item.critical)
        with item.active():
            for target in item.targets:
                with self.subTest(target=target.label):
                    result = wb.run_target(target, SUITES, first_seed(target))
                    detected, missing = wb.detection(target, result)
                    self.assertEqual(result.errors, [], observed(result))
                    self.assertEqual(missing, [], observed(result))
                    self.assertTrue(detected, observed(result))

    def test_non_critical_defects_run_without_harness_errors(self):
        for item in wb.SEEDED_DEFECTS:
            if item.critical:
                continue
            with item.active():
                for target in item.targets:
                    with self.subTest(defect=item.id, target=target.label):
                        result = wb.run_target(target, SUITES, first_seed(target))
                        self.assertEqual(result.errors, [], observed(result))
                        crashed = [u for u in result.unmet if u["assertion"] == "exit"
                                   and (u["actual"] == "crashed" or str(u["actual"]).startswith("error "))]
                        self.assertEqual(crashed, [], observed(result))

    def test_a_production_exception_at_a_step_with_no_exit_expectation_is_never_a_detection(self):
        """Review regression: missing-post-read's execute steps freeze no exit, so a seeded change that makes
        production raise there (exit ``error TypeError``) changed the expected assertions and was counted as
        detected. Every step's raw exit is now checked, not only the unmet exit assertions."""
        target = wb.DefectTarget("required", "reconciliation", "missing-post-read",
                                 expect=("exec2:skips.alpha.unresolved_attempt",))
        raising = wb.SeededDefect("raises", "production raises at the stop check", True,
                                  wb._set("run", "FLEET_STOP_FILE", None), (target,))
        with raising.active():
            result = wb.run_target(target, SUITES, first_seed(target))
        self.assertIn("exec2:skips.alpha.unresolved_attempt", wb._named(result), observed(result))
        self.assertNotIn("exit", {u["assertion"] for u in result.unmet}, observed(result))
        self.assertIn("exec1:exit error TypeError", result.crashes)
        self.assertEqual(wb.detection(target, result), (False, ["crashed"]))

    def test_a_dispatch_that_raises_inside_an_overlap_step_is_a_crash(self):
        """An overlap step's exit is the list of its dispatches' exits: a production exception in any one of them is
        a crash (overlap steps freeze no exit)."""
        raw = {"exit": [0, "error RuntimeError"]}
        self.assertEqual(wb.production_exceptions("overlap", raw), ["overlap:exit error RuntimeError"])
        self.assertEqual(wb.production_exceptions("exec", {"exit": "error OSError"}), ["exec:exit error OSError"])
        self.assertEqual(wb.production_exceptions("exec", {"exit": "crashed"}), [])  # an armed crash: compared
        self.assertEqual(wb.production_exceptions("overlap", {"exit": [0, 1]}), [])
        target = wb.DefectTarget("required", "overlapping-dispatch", "threads", expect=("overlap:dispatches",))
        result = wb.TargetResult(False, [{"step": "overlap", "assertion": "dispatches", "expected": None,
                                          "actual": None, "cause": "x"}], [], [], [],
                                 crashes=["overlap:exit error RuntimeError"])
        self.assertEqual(wb.detection(target, result), (False, ["crashed"]))

    def test_a_dispatch_child_spec_carries_the_defect_only_while_one_is_active(self):
        """Review regression (coverage): the processes-mode dispatch children's spec JSON, as written, has no
        defect field on an unpatched run and names the active defect under one; neither the argv nor the env
        carries it."""
        target = next(t for t in wb.defect("lock-skipped").targets if t.scenario_id == "processes")
        real = subprocess.Popen

        def run(active):
            specs = []

            def spy(command, **kwargs):
                if "--dispatch-child" in command:
                    specs.append((list(command), dict(kwargs.get("env") or {}),
                                  json.loads(Path(command[-1]).read_text(encoding="utf-8"))))
                return real(command, **kwargs)

            with patch.object(wb.subprocess, "Popen", spy), \
                    (wb.defect("lock-skipped").active() if active else contextlib.nullcontext()):
                result = wb.run_target(target, SUITES, first_seed(target))
            self.assertEqual(result.errors, [], observed(result))
            self.assertEqual(len(specs), 2, specs)
            for command, env, spec in specs:
                self.assertFalse([part for part in command if "defect" in part.lower()], command)
                self.assertFalse([key for key in env if "DEFECT" in key.upper()])
            return [spec for _, _, spec in specs]

        self.assertEqual([spec.get("defect", "absent") for spec in run(False)], ["absent", "absent"])
        self.assertEqual([spec.get("defect") for spec in run(True)], ["lock-skipped", "lock-skipped"])


def _caught_test(defect_id):
    def test(self):
        self.check_caught(defect_id)

    test.__doc__ = f"S14-AC7: seeded defect {defect_id} is caught on every target."
    return test


for _item in wb.SEEDED_DEFECTS:
    if _item.critical:
        setattr(SeededDefectRunTests, f"test_defect_{_item.id.replace('-', '_')}_is_caught", _caught_test(_item.id))
del _item


# Verifies: S14-R7
class SeamLivenessTests(unittest.TestCase):
    """QA round 1: every seeded defect patches an attribute production really calls. A refactor that bypasses a
    seam would turn its defect into a no-op that nothing reports; here it is a named failure."""

    def test_every_seeded_defect_patches_a_seam_a_clean_run_of_its_targets_calls(self):
        unused = wb.seam_liveness(SUITES)
        self.assertEqual(wb.unexcused(unused), {})
        # every exemption is still needed: a seam it names really is uncalled, or the exemption is stale
        self.assertEqual({(defect_id, seam) for defect_id, seams in unused.items() for seam in seams},
                         set(wb.SEAMS_NOT_CALLED))
        for key, why in wb.SEAMS_NOT_CALLED.items():
            self.assertTrue(why, key)

    def test_a_seam_production_never_calls_is_named(self):
        bypassed = wb.SeededDefect("bypassed", "patches an attribute file-backed runs never call", True,
                                   wb._set("sdk.fleet.attempts", "MemoryAccountLock.try_acquire", wb._nothing), (
                                       wb.DefectTarget("required", "paid-boundary", "exact-500",
                                                       expect=(ADD_MUTATION,)),))
        self.assertEqual(wb.seam_liveness(SUITES, (bypassed,)),
                         {"bypassed": ["sdk.fleet.attempts.MemoryAccountLock.try_acquire"]})

    def test_spies_call_through_count_and_restore(self):
        import sdk.fleet.attempts as attempts

        original = attempts.journal_witnesses_store
        pair = ("sdk.fleet.attempts", "journal_witnesses_store")
        with wb.seam_spies([pair]) as calls:
            self.assertIsNot(attempts.journal_witnesses_store, original)
            self.assertFalse(attempts.journal_witnesses_store((), "alpha", "training-v2"))
        self.assertEqual(calls, {pair: 1})
        self.assertIs(attempts.journal_witnesses_store, original)

    def test_each_exemption_names_a_catalogue_seam_that_cannot_be_spied_and_says_why(self):
        used = {pair for item in wb.SEEDED_DEFECTS for pair in wb.seams(item)}
        for (module_name, dotted), why in wb.SEAM_EXEMPT.items():
            with self.subTest(seam=f"{module_name}.{dotted}"):
                self.assertIn((module_name, dotted), used)
                self.assertTrue(why)
                owner = wb._module(module_name)
                for part in dotted.split("."):
                    owner = getattr(owner, part)
                self.assertFalse(callable(owner))


# Verifies: S14-R7
class DerivedOptionTests(unittest.TestCase):
    """QA round 2: the derived-variant step options added to reach the round's survivors. Each is refused in a
    fixture (test_derived_only_step_options_are_refused_in_a_fixture); here each does what its variants rely on."""

    def test_policy_id_option_writes_the_steps_registry_under_that_policy_over_a_control_override(self):
        with tempfile.TemporaryDirectory() as directory:
            control = case(HOLDOUT, "retry-different-crew")["control"]
            entry = next(s for s in control["scenarios"] if s["id"] == "v1-lost-reply")
            ctx = wb.start_scenario("holdout", "retry-different-crew", entry, HOLDOUT["catalog"], 2401,
                                    control["policy_id"], Path(directory))
            self.assertEqual(wb._registry_for(ctx, {"id": "exec", "kind": "execute"}), wb.REGISTRY_FILE)
            name = wb._registry_for(ctx, {"id": "exec2", "kind": "execute", "pre": {"policy_id": "training-v2"}})
            self.assertNotEqual(name, wb.REGISTRY_FILE)
            payload = json.loads((ctx.directory / name).read_text(encoding="utf-8"))
            self.assertEqual([item["policy_id"] for item in payload["accounts"]], ["training-v2"])
            base = json.loads((ctx.directory / wb.REGISTRY_FILE).read_text(encoding="utf-8"))
            self.assertEqual([item["policy_id"] for item in base["accounts"]], ["training-v1"])

    def test_mode_option_runs_execute_in_that_mode_under_an_authorization_allowing_it_and_its_fallback(self):
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            plain = wb._argv(ctx, {"id": "exec", "kind": "execute"}, wb.REGISTRY_FILE)
            self.assertEqual(plain[plain.index("--mode") + 1], "full")
            self.assertEqual(plain[plain.index("--authorization") + 1], wb.AUTHORIZATION_FILE)
            argv = wb._argv(ctx, {"id": "exec", "kind": "execute", "pre": {"mode": "canary"}}, wb.REGISTRY_FILE)
            self.assertEqual(argv[argv.index("--mode") + 1], "canary")
            name = argv[argv.index("--authorization") + 1]
            self.assertNotEqual(name, wb.AUTHORIZATION_FILE)
            payload = json.loads((ctx.directory / name).read_text(encoding="utf-8"))
            base = json.loads((ctx.directory / wb.AUTHORIZATION_FILE).read_text(encoding="utf-8"))
            self.assertEqual(payload["modes"], ["canary"])
            self.assertEqual(payload["actions"], sorted({*base["actions"], "collect_all_resources"}))
            self.assertEqual((payload["accounts"], payload["caps"]), (base["accounts"], base["caps"]))

    def test_hold_lock_holds_the_declared_identity_lock_only_even_before_production_created_it(self):
        """QA round 2 D1: holding every lock file present let a lock keyed by alias look held; the option holds the
        lock production must take, <declared game identity>.lock, and nothing else."""
        import fcntl

        from sdk.fleet.attempts import FileAccountLock
        from sdk.state.snapshot import game_identity_fingerprint

        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            step = {"id": "exec", "kind": "execute", "pre": {"hold_lock": True}}
            wb._hold_locks(ctx, step, "exec")()
            self.assertIn("exec:lock_held", ctx.witnesses)
            self.assertIsNotNone(ctx.witnesses["exec:lock_held"])  # no store directory yet: nothing to hold
            root = ctx.journals / wb.STORE_DIR
            root.mkdir()
            stray = root / "alpha.lock"
            stray.write_bytes(b"")
            identity = game_identity_fingerprint(ctx.accounts[0]["user_id"], wb.SALT)
            release = wb._hold_locks(ctx, step, "exec")
            try:
                self.assertTrue((root / f"{identity}.lock").is_file())
                self.assertIsNone(FileAccountLock(root).try_acquire(identity))
                fd = os.open(stray, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # not held: raises if it were
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
            finally:
                release()
            self.assertIsNone(ctx.witnesses["exec:lock_held"])
            handle = FileAccountLock(root).try_acquire(identity)
            self.assertIsNotNone(handle)
            if handle is not None:  # narrows the type; the assertion above already failed otherwise
                handle.release()

    def test_store_and_clock_options_name_what_they_found_nothing_to_act_on(self):
        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            step = {"id": "exec", "kind": "execute", "pre": {"store_as_directory": True, "dry_run_age_s": 60}}
            wb._derived_pre(ctx, step, "exec")
            self.assertIn("no attempt store file", ctx.witnesses["exec:store_is_directory"])
            self.assertIn("no stamped dry run", ctx.witnesses["exec:dry_run_aged"])
            store = ctx.journals / wb.STORE_DIR / "alpha.json"
            store.parent.mkdir()
            store.write_text("{}", encoding="utf-8")
            wb._derived_pre(ctx, {"id": "exec2", "kind": "execute", "pre": {"store_as_directory": True}}, "exec2")
            self.assertTrue(store.is_dir())
            self.assertIsNone(ctx.witnesses["exec2:store_is_directory"])

    def test_fail_replace_fails_only_the_attempt_stores_replace_and_restores_it(self):
        import sdk.fleet.attempts as attempts

        with scratch_ctx("paid-boundary", "exact-500") as ctx:
            checks = wb._arm(ctx, {"id": "exec", "kind": "execute", "pre": {"fail_replace": True}}, "exec")
            try:
                with self.assertRaises(OSError):
                    attempts.os.replace("a", "b")
                self.assertIs(attempts.os.fsync, os.fsync)
                self.assertIsNot(attempts.os, os)  # the process-wide os module is never patched
            finally:
                for check in checks:
                    check()
            self.assertIs(attempts.os, os)
            self.assertIsNone(ctx.witnesses["exec:replace_failed"])

    def test_a_dry_run_exactly_24_hours_old_still_qualifies(self):
        """The companion of the dry-run-age-by-day kill: 24 hours to the second is not too old (the clean revision
        starts), and one second more is (dry_run_stale), with both runs on the fake clock after a UTC midnight."""
        target = next(t for t in wb.defect("dry-run-age-by-day").targets)
        stale = wb.run_target(target, SUITES, first_seed(target))
        self.assertTrue(stale.passed, observed(stale))
        exact = wb.DefectTarget("required", "reconciliation", "verified", expect=("exec:exit",),
                                derive=("dry-run-24h", wb.edits(wb.step_pre("dry", day_start=True),
                                                                wb.step_pre("exec", ["exec:dry_run_aged"],
                                                                            dry_run_age_s=86400))))
        result = wb.run_target(exact, SUITES, first_seed(exact))
        self.assertTrue(result.passed, observed(result))


# Verifies: S14-R7
class CapLayerTests(unittest.TestCase):
    """QA round 1 D5 and D9: the spend cap is enforced three times (the crew plan's reservation of earlier requests,
    the under-lock recheck, the gate runner). With the other two disabled, each layer alone still prevents the
    overspend on paid-boundary/spend-cap-after-earlier, so a defect in any one of them is detectable (the
    runner-cap-window and recheck-spend-frozen defects disable the other two layers while they seed theirs)."""

    def test_each_cap_layer_alone_prevents_the_overspend(self):
        target = wb.DefectTarget("required", "paid-boundary", "spend-cap-after-earlier", expect=(ADD_MUTATION,),
                                 hard=("exec:budget_spend",))
        for layer in sorted(wb.CAP_LAYERS_OFF):
            others = wb._stack(*(seeding for name, seeding in sorted(wb.CAP_LAYERS_OFF.items()) if name != layer))
            alone = wb.SeededDefect(f"only-{layer}", f"only the {layer} cap layer left", True, others, (target,))
            with self.subTest(layer=layer), alone.active():
                result = wb.run_target(target, SUITES, first_seed(target))
                self.assertEqual((result.errors, result.crashes, result.hard_violations), ([], [], []),
                                 observed(result))
                self.assertNotIn(ADD_MUTATION, wb._named(result), observed(result))
                if layer != "runner":  # the runner refuses after the crew plan: its refusal stops the run (exit 1)
                    self.assertTrue(result.passed, observed(result))

    def test_all_three_layers_off_overspends(self):
        target = wb.DefectTarget("required", "paid-boundary", "spend-cap-after-earlier", expect=(ADD_MUTATION,),
                                 hard=("exec:budget_spend",))
        none = wb.SeededDefect("no-cap-layer", "every cap layer disabled", True,
                               wb._stack(*wb.CAP_LAYERS_OFF.values()), (target,))
        with none.active():
            result = wb.run_target(target, SUITES, first_seed(target))
        self.assertEqual(wb.detection(target, result), (True, []), observed(result))


# Verifies: S14-R7
class DefectsCliTests(unittest.TestCase):
    """``--defects``: every catalogued defect seeded in turn, the clean run first; exit 0 only when every critical
    defect is caught on every trace."""

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = wb.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_defects_mode_exits_0_when_the_clean_runs_pass_and_the_defect_is_caught(self):
        code, out, err = self.run_main("--defects", "--defect", "premium-spend")
        self.assertEqual(code, wb.EXIT_OK, out + err)
        for seed in (1401, 1402, 1403):
            self.assertIn(f"CLEAN PASS required/paid-boundary/premium seed={seed}", out)
            self.assertIn(f"DETECTED premium-spend required/paid-boundary/premium seed={seed} [benchmark]", out)
        self.assertIn("premium-spend (critical): detected 3 of 3 traces (100.000000)", out)
        self.assertTrue(out.rstrip().endswith("Defects: PASS"), out)
        code, out, err = self.run_main("--defects", "--defect", "premium-spend", "--json", "-")
        self.assertEqual(code, wb.EXIT_OK, err)
        report = json.loads(out)
        self.assertEqual(report["schema"], wb.DEFECTS_SCHEMA)
        [entry] = report["defects"]
        self.assertEqual((entry["id"], entry["detected"], entry["total"], entry["survivor"]),
                         ("premium-spend", 3, 3, False))
        self.assertEqual(report["seeds"], {"required": [1401, 1402, 1403], "holdout": [2401, 2402, 2403]})
        self.assertEqual(report["summary"]["survivors"], {"critical": [], "non_critical": []})

    def test_a_defect_that_changes_nothing_is_a_surviving_critical_defect_and_fails_the_run(self):
        noop = wb.SeededDefect("noop", "changes nothing", True, contextlib.nullcontext, (
            wb.DefectTarget("required", "paid-boundary", "exact-500", expect=(ADD_MUTATION,)),))
        with patch.object(wb, "SEEDED_DEFECTS", (noop,)):
            code, out, err = self.run_main("--defects")
        self.assertEqual(code, wb.EXIT_GATE, out + err)
        self.assertIn(f"MISSED noop required/paid-boundary/exact-500 seed=1401 [benchmark] missing {ADD_MUTATION}",
                      out)
        self.assertIn("Defects: FAIL", out)
        self.assertIn("- critical defect noop detected on 0 of 3 traces", out)

    def test_a_target_that_fails_on_the_clean_revision_fails_the_run(self):
        def unplanned(entry):
            entry["expected"]["dry"]["journal_planned"] = []

        broken = wb.SeededDefect("broken", "a deliberately wrong derived trace", True, contextlib.nullcontext, (
            wb.DefectTarget("required", "paid-boundary", "exact-500", expect=(ADD_MUTATION,),
                            derive=("broken", unplanned)),))
        with patch.object(wb, "SEEDED_DEFECTS", (broken,)):
            code, out, err = self.run_main("--defects")
        self.assertEqual(code, wb.EXIT_GATE, out + err)
        self.assertIn("CLEAN FAIL required/paid-boundary/exact-500~broken seed=1401", out)
        self.assertIn("- clean: required/paid-boundary/exact-500~broken seed=1401 fails unpatched", out)

    def test_a_critical_defect_with_no_trace_or_an_empty_catalogue_never_passes(self):
        """Review regression: 0 of 0 traces was not a survivor, so a critical defect with no target (or a catalogue
        with no defect) let ``--defects`` exit 0 with nothing measured."""
        hollow = wb.SeededDefect("hollow", "no target", True, contextlib.nullcontext, ())
        with patch.object(wb, "SEEDED_DEFECTS", (hollow,)):
            code, out, err = self.run_main("--defects")
        self.assertEqual(code, wb.EXIT_GATE, out + err)
        self.assertIn("- critical defect hollow detected on 0 of 0 traces", out)
        with patch.object(wb, "SEEDED_DEFECTS", ()):
            code, out, err = self.run_main("--defects")
        self.assertEqual(code, wb.EXIT_GATE, out + err)
        self.assertIn("- no seeded defect ran", out)

    def test_a_seeding_that_fails_to_apply_is_an_oracle_error_and_leaves_nothing_active(self):
        """Review regression: a seeding whose production attribute is missing escaped ``main`` as a traceback
        (process exit 1, the gate-failure code); it is a harness error (exit 2), and no defect stays active."""
        import sdk.fleet.attempts as attempts

        original = attempts.FileAccountLock.try_acquire
        unapplied = wb.SeededDefect("unapplied", "a seeding naming a missing attribute", True,
                                    wb._stack(wb._set("sdk.fleet.attempts", "FileAccountLock.try_acquire", None),
                                              wb._set("sdk.fleet.attempts", "NoSuchOwner.attr", None)), (
                                        wb.DefectTarget("required", "paid-boundary", "exact-500",
                                                        expect=(ADD_MUTATION,)),))
        with patch.object(wb, "SEEDED_DEFECTS", (unapplied,)):
            code, out, err = self.run_main("--defects")
        self.assertEqual(code, wb.EXIT_ERROR, out + err)
        self.assertIn("oracle error: seeded defect unapplied could not be applied: AttributeError", err)
        self.assertIsNone(wb.active_defect())
        self.assertIs(attempts.FileAccountLock.try_acquire, original)

    def test_defects_mode_refuses_on_a_failed_fake_self_check(self):
        """Review regression: ``--gate`` self-checks the fake's strict pricing and refusal rules before scoring;
        ``--defects`` scored detections against the fake without that check."""
        with patch.object(wb, "fake_self_check", return_value=["priced: no refusal"]), \
                patch.object(wb, "run_defect", side_effect=AssertionError("a defect ran")):
            code, out, err = self.run_main("--defects", "--defect", "premium-spend")
        self.assertEqual(code, wb.EXIT_ERROR, out + err)
        self.assertIn("oracle error: fake self-check failed: priced: no refusal", err)

    def test_a_harness_error_under_a_defect_is_a_harness_error_even_for_a_non_critical_defect(self):
        """Review regression: a harness error in the patched run was only a miss, so under a non-critical defect
        (rates published, not gated) ``--defects`` exited 0 over a run that measured nothing."""
        def broken(original):
            def run(*args, **kwargs):
                raise wb.OracleError("a dispatch thread did not finish")

            return run

        unmeasured = wb.SeededDefect("unmeasured", "the patched run breaks the harness", False,
                                     wb._swap(wb.__name__, "run_scenario", broken), (
                                         wb.DefectTarget("required", "paid-boundary", "exact-500",
                                                         expect=(ADD_MUTATION,)),))
        with patch.object(wb, "SEEDED_DEFECTS", (unmeasured,)):
            code, out, err = self.run_main("--defects")
        self.assertEqual(code, wb.EXIT_ERROR, out + err)
        self.assertIn("defect harness error: unmeasured required/paid-boundary/exact-500 seed=1401: harness raised "
                      "OracleError: a dispatch thread did not finish", err)

    def test_a_clean_failure_from_a_missing_witness_names_the_witness(self):
        """Review regression: a clean run failing only on a missing witness printed ``CLEAN FAIL ...:`` with no
        reason, and its JSON entry carried none either."""
        target = wb.DefectTarget("required", "paid-boundary", "exact-500", expect=(ADD_MUTATION,))
        silent = wb.SeededDefect("silent", "a witness that never fires", True, contextlib.nullcontext, (target,))
        result = wb.TargetResult(False, [], ["exec:hold: step never ran"], [], [])
        with patch.object(wb, "SEEDED_DEFECTS", (silent,)), patch.object(wb, "run_target", return_value=result):
            code, out, err = self.run_main("--defects", "--json", "-")
            report = json.loads(out)
            self.assertEqual(report["defects"][0]["clean"][0]["witnesses_missing"], ["exec:hold: step never ran"])
            code, out, err = self.run_main("--defects")
        self.assertEqual(code, wb.EXIT_GATE, out + err)
        self.assertIn("CLEAN FAIL required/paid-boundary/exact-500 seed=1401: exec:hold: step never ran", out)

    def test_a_seam_no_clean_run_calls_fails_the_run_by_name(self):
        """QA round 1: a defect whose patched attribute production no longer calls is a named ``--defects``
        failure even when its targets still change (here another seam of the same seeding does the work)."""
        bypassed = wb.SeededDefect("bypassed", "one live seam and one production never calls", True,
                                   wb._stack(wb._swap("sdk.client", "Client.addTraining", wb._send_twice),
                                             wb._set("sdk.fleet.attempts", "MemoryAccountLock.try_acquire",
                                                     wb._nothing)), (
                                       wb.DefectTarget("required", "paid-boundary", "exact-500",
                                                       expect=(ADD_MUTATION,), hard=("exec:duplicate_write",)),))
        with patch.object(wb, "SEEDED_DEFECTS", (bypassed,)):
            code, out, err = self.run_main("--defects")
        self.assertEqual(code, wb.EXIT_GATE, out + err)
        self.assertIn("bypassed (critical): detected 3 of 3 traces", out)
        self.assertIn("- defect bypassed: seam sdk.fleet.attempts.MemoryAccountLock.try_acquire is never called on a "
                      "clean run of its targets", out)

    def test_defects_flag_errors(self):
        with self.subTest(case="unknown id"):
            code, _, err = self.run_main("--defects", "--defect", "nope")
            self.assertEqual(code, wb.EXIT_ERROR)
            self.assertIn("unknown defect ids: ['nope']", err)
        for argv in (["--defect", "premium-spend"], ["--gate", "--defects"], ["--defects", "--case", "paid-boundary"],
                     ["--defects", "--suite", "required"], ["--defects", "--no-probe"],
                     ["--defects", "--control-seeds", "1"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as stop:
                self.run_main(*argv)
            self.assertEqual(stop.exception.code, 2)


def _spy_popen(started):
    def popen(command, **kwargs):
        started.update(kwargs, command=command)
        return subprocess.Popen(command, **kwargs)

    return popen


# Verifies: S14-R7
class WorkflowGateTests(unittest.TestCase):
    """The checked-in training-v2 traces: both suites score 100.000000 at every seed with no hard violation, missing
    witness or fake error, every training-v1 control passes, and a child under another PYTHONHASHSEED reproduces
    overlapping-dispatch and persisted-intent-restart byte for byte (S14-R7, S14-AC6)."""

    CHILD_CASES = ("overlapping-dispatch", "persisted-intent-restart")

    @classmethod
    def setUpClass(cls):
        cls.started = {}
        argv = ["--gate", "--suite", "required"]
        for case_id in cls.CHILD_CASES:
            argv += ["--case", case_id]
        cls.child = wb.ChildRun(wb.child_command(wb.parse_args(argv)), popen=_spy_popen(cls.started), timeout=300)
        try:
            cls.report = wb.run_suite(wb.load_suites(), probe=False)
        except BaseException:
            cls.child.close()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.child.close()

    def test_both_suites_score_100_with_no_hard_violation_missing_witness_or_error(self):
        suites = self.report["suites"]
        self.assertEqual((suites["required"]["score"], suites["required"]["passed"], suites["required"]["total"]),
                         ("100.000000", 129, 129))
        self.assertEqual((suites["holdout"]["score"], suites["holdout"]["passed"], suites["holdout"]["total"]),
                         ("100.000000", 30, 30))
        for name, suite in suites.items():
            for case_id, result in suite["cases"].items():
                for key, trace in result["traces"].items():
                    with self.subTest(trace=f"{name}/{case_id}/{key}"):
                        self.assertTrue(trace["pass"], trace["unmet"])
                        self.assertEqual((trace["unmet"], trace["hard_violations"], trace["witnesses_missing"],
                                          trace["errors"]), ([], [], [], []))
        self.assertEqual(wb.fake_errors(self.report), [])

    def test_every_training_v1_control_passes(self):
        for name, suite in self.report["suites"].items():
            for case_id, result in suite["cases"].items():
                with self.subTest(case=f"{name}/{case_id}"):
                    self.assertTrue(result["control"]["traces"])
                    self.assertTrue(result["control"]["pass"])
                    for key, trace in result["control"]["traces"].items():
                        self.assertTrue(trace["pass"], (key, trace["unmet"]))

    def test_the_gate_names_no_reason(self):
        self.assertEqual(wb.gate_reasons(self.report, deterministic=True, subset=False), [])

    def test_every_frozen_defect_target_passes_in_the_shared_run(self):
        checked = 0
        for item in wb.SEEDED_DEFECTS:
            for target in item.targets:
                if target.derive:
                    continue
                result = self.report["suites"][target.suite]["cases"][target.case_id]
                traces = result["control"]["traces"] if target.control else result["traces"]
                for seed in SUITES[target.suite]["seeds"]:
                    with self.subTest(defect=item.id, target=target.label, seed=seed):
                        trace = traces[f"{target.scenario_id}@{seed}"]
                        self.assertTrue(trace["pass"], trace["unmet"])
                        checked += 1
        self.assertGreater(checked, 0)

    def test_a_child_under_another_hash_seed_reproduces_the_shared_run(self):
        child = json.loads(self.child.report())
        ours = wb.scored_view(self.report)["suites"]["required"]["cases"]
        theirs = wb.scored_view(child)["suites"]["required"]["cases"]
        self.assertEqual(sorted(theirs), sorted(self.CHILD_CASES))
        for case_id in self.CHILD_CASES:
            with self.subTest(case=case_id):
                self.assertEqual(theirs[case_id], ours[case_id])
        self.assertEqual(self.started["env"]["PYTHONHASHSEED"], wb.child_hash_seed())
        self.assertNotEqual(self.started["env"]["PYTHONHASHSEED"], os.environ.get("PYTHONHASHSEED"))
        self.assertEqual(self.started["env"][wb.CHILD_ENV], "1")


# Verifies: S14-R7
class DeterminismTests(unittest.TestCase):
    """The real check_determinism with a real child interpreter under another PYTHONHASHSEED (no stub)."""

    def test_check_determinism_with_a_real_child_under_another_hash_seed(self):
        args = wb.parse_args(["--gate", "--suite", "required", "--case", "distinct-accounts"])
        started = {}
        same, report = wb.check_determinism(
            wb.load_suites("required"),
            start_child=lambda: wb.ChildRun(wb.child_command(args), popen=_spy_popen(started), timeout=300),
            cases=["distinct-accounts"], probe=False)
        self.assertTrue(same)
        self.assertEqual(started["env"]["PYTHONHASHSEED"], wb.child_hash_seed())
        self.assertNotEqual(started["env"]["PYTHONHASHSEED"], os.environ.get("PYTHONHASHSEED"))
        traces = report["suites"]["required"]["cases"]["distinct-accounts"]["traces"]
        self.assertEqual(sorted(traces), [f"alpha-held@{seed}" for seed in REQUIRED["seeds"]])
        self.assertTrue(all(trace["pass"] for trace in traces.values()), traces)

    def test_repeat_in_process_runs_give_identical_behavior_digests(self):
        for case_id, scenario_id in (("overlapping-dispatch", "threads"),
                                     ("persisted-intent-restart", "crash-after-send")):
            with self.subTest(scenario=f"{case_id}/{scenario_id}"):
                entry = scenario(case_id, scenario_id)
                runs = [wb.run_scenario("required", case_id, entry, REQUIRED["catalog"], REQUIRED["seeds"][0], CODES)
                        for _ in range(2)]
                self.assertTrue(runs[0].behavior)
                self.assertEqual(runs[0].behavior, runs[1].behavior)


if __name__ == "__main__":
    unittest.main()
