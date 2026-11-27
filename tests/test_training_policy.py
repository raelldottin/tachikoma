"""Tests for training-v1 (sdk.policy.training): fleet-v1 plus crew training. Synthetic snapshots and designs only.

Every negative test flips one field of the canonical fixture and, in the same test, checks that the canonical
fixture does plan the action (a paired control), so a test can never pass because nothing is planned at all.
"""

import copy
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from unittest.mock import patch

from sdk.actions import check_preconditions, get_spec
from sdk.execution.runner import planned_cost
from sdk.policy import ActionRequest, DesignData, PolicyConfig, Rejection
from sdk.policy.training import DEFERRED, TrainingV1, training_context, training_payloads, training_strategy
from sdk.training import (
    DEFAULT_STRATEGY,
    FinishTraining,
    Skip,
    StartTraining,
    UseConsumable,
    build_context,
    plan_training,
    with_consumables,
)
from sdk.training.strategy import RARITY_STARS
from tests.training_fixtures import (
    CHARACTER_DESIGNS,
    CPU,
    ITEM_DESIGNS,
    MIXED,
    RESEARCH,
    RESEARCH_DESIGNS,
    ROOM_DESIGNS,
    ROOMS,
    T0,
    TRAINING_DESIGNS,
    USE_CPU,
    crew,
    design,
    ended,
    ids,
    item,
    plan,
    reasons,
    server_time,
    snap,
    start,
    training_ids,
    training_requests,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def planner_reasons(snapshot, data=None):
    """{(decision type, character id): text} that ``sdk.training`` plans for ``snapshot``: a decision's reason, or a
    Skip's "code: detail". The policy only passes this text through, so most tests derive it from here; one literal
    pin per source (OneTests for a decision, ManyTests for a Skip) keeps the text itself checked.
    """
    data = data or design()
    context, _ = training_context(snapshot, data)
    assert context is not None, "the fixture plans training"
    return {(type(decision).__name__, decision.character_id):
            f"{decision.reason_code}: {decision.detail}" if isinstance(decision, Skip) else decision.reason
            for decision in plan_training(context, training_strategy(DEFAULT_STRATEGY, data))}


def catalog_reason(action_id, snapshot, params):
    """The catalog's failed preconditions as the policy records them (the finish-grace test pins one literally)."""
    return "; ".join(check_preconditions(get_spec(action_id), snapshot, params))


class DesignDataTrainingRecordTests(unittest.TestCase):
    def test_design_data_defaults_have_no_training_records(self):
        bare = DesignData()
        old_style = DesignData.from_records(room_designs=ROOM_DESIGNS, research_designs=RESEARCH_DESIGNS)
        for data in (bare, old_style):
            with self.subTest(data=data):
                self.assertEqual((data.training_designs, data.character_designs, data.item_designs), ((), (), ()))
        self.assertEqual(old_style, DesignData(rooms=old_style.rooms, research=old_style.research))
        self.assertIsInstance(hash(design()), int)
        self.assertEqual(hash(design()), hash(design()))

    def test_from_records_keeps_scalar_attributes_as_text(self):
        data = DesignData.from_records(training_designs=[
            {"@TrainingDesignId": 5010, "@TrainingName": "Bench Press", "@Missing": None, "@Nested": {"a": "b"},
             "@List": ["x"], "@Flag": True, "@Ratio": 0.5},
            "not a record",
        ])
        self.assertEqual(len(data.training_designs), 1)
        record = data.training_designs[0]
        self.assertEqual(record.as_dict(), {"@TrainingDesignId": "5010", "@TrainingName": "Bench Press"})
        self.assertEqual(record.attributes, (("@TrainingDesignId", "5010"), ("@TrainingName", "Bench Press")))
        data = design()
        self.assertEqual([r.as_dict() for r in data.character_designs], CHARACTER_DESIGNS)
        self.assertEqual([r.as_dict() for r in data.item_designs], ITEM_DESIGNS)
        self.assertEqual([r.as_dict() for r in data.training_designs], TRAINING_DESIGNS)


class ZeroTests(unittest.TestCase):
    def test_no_training_designs_plans_no_training_and_says_why(self):
        self.assertEqual(training_ids(plan()), [start()])  # paired control
        result = plan(design_data=design(training_designs=()))
        self.assertEqual(training_requests(result), [])
        self.assertEqual(reasons(result)["training"], "no TrainingDesign records in the design data")

    def test_no_character_designs_plans_no_training_and_says_why(self):
        self.assertEqual(training_ids(plan()), [start()])  # paired control
        result = plan(design_data=design(character_designs=()))
        self.assertEqual(training_requests(result), [])
        self.assertEqual(reasons(result)["training"],
                         "no CharacterDesign records in the design data, so training capacity is unknown")

    def test_crew_section_missing_rejects_training(self):
        self.assertEqual(training_ids(plan()), [start()])  # paired control
        result = plan(snap(crew_records=None))
        self.assertEqual(training_requests(result), [])
        self.assertEqual(reasons(result)["training"], "crew section not observed")


class OneTests(unittest.TestCase):
    def test_one_idle_crew_in_gym_starts_its_band_training(self):
        # The literal pin of a planner decision's reason (other tests derive it with planner_reasons).
        self.assertEqual(training_requests(plan()), [
            ActionRequest("add_training", {"training_design_id": "5010", "character_id": "101"},
                          "primary T1 Bench Press at 0% in gym", {"minerals": 0, "gas": 0})])

    def test_training_follows_the_fleet_v1_plan(self):
        training, fleet = plan(), plan(policy="fleet-v1")
        self.assertTrue(fleet.requests)
        self.assertEqual(training.policy, "training-v1")
        self.assertEqual(ids(training), ids(fleet) + [start()])
        self.assertEqual(training.requests[:len(fleet.requests)], fleet.requests)

    def test_training_v1_drops_only_the_not_modelled_rejection(self):
        dear = design(research_designs=RESEARCH_DESIGNS + [
            {"@ResearchDesignId": "620", "@RootResearchDesignId": "620", "@ResearchName": "Engine",
             "@GasCost": "9999"}])
        training, fleet = plan(design_data=dear), plan(design_data=dear, policy="fleet-v1")
        self.assertEqual(reasons(fleet)["add_research 620"], "needs 9999 gas, 2000 available above reserve")
        self.assertIn(Rejection("add_training", "crew training is not modelled by fleet-v1"), fleet.rejected)
        self.assertNotIn("add_training", reasons(training))
        kept = [item for item in fleet.rejected if item.candidate != "add_training"]
        self.assertTrue(kept)
        self.assertEqual(list(training.rejected[:len(kept)]), kept)


class FinishAndDeferTests(unittest.TestCase):
    def test_ended_training_is_finished_and_its_advisory_primary_deferred(self):
        result = plan(snap([ended()]))
        reason = planner_reasons(snap([ended()]))[("FinishTraining", 101)]
        self.assertEqual(training_requests(result), [ActionRequest("finish_training", {"character_id": "101"}, reason, {})])
        self.assertNotIn("add_training", [request.action_id for request in result.requests])
        self.assertIn(Rejection("add_training 101", DEFERRED), result.rejected)
        # The reason itself, not only the shared constant: the operator reads it in the run report.
        self.assertTrue(reasons(result)["add_training 101"].startswith("deferred to the next run: "))
        self.assertEqual(training_ids(plan()), [start()])  # paired control: idle, the same crew starts

    def test_advisory_consumable_after_a_finish_is_deferred(self):
        # No catalog precondition stops this one (an item may be used on a crew member at any time), so only the
        # deferral keeps the stale advisory use out of the plan.
        cpu = ended("104", "C.P.U.", room="12", design_id="5030", Science=1)
        result = plan(snap([cpu], inventory=[item()]))
        self.assertEqual(training_ids(result), [("finish_training", {"character_id": "104"})])
        self.assertEqual(reasons(result)["use_consumable 104"], DEFERRED)
        self.assertEqual(training_ids(plan(snap([CPU], inventory=[item()]))), [USE_CPU])  # paired control: idle

    def test_skip_after_a_finish_is_recorded_as_deferred(self):
        # Ada has no training role: the planner finishes her training, then skips her on the pre-finish stats.
        idle = snap([crew("105", "Ada")])
        self.assertEqual(reasons(plan(idle))["training 105"],
                         planner_reasons(idle)[("Skip", 105)])  # paired control: idle, the planner's own skip
        result = plan(snap([ended("105", "Ada")]))
        self.assertEqual(training_ids(result), [("finish_training", {"character_id": "105"})])
        self.assertEqual(reasons(result)["training 105"], DEFERRED)

    def test_deferral_does_not_depend_on_the_advisory_primary_coming_right_after_its_finish(self):
        # The planner puts a crew's advisory primary right after its FinishTraining; the deferral must hold even if
        # another crew's decision comes between them (C.P.U.'s advisory use has no catalog precondition to stop it).
        records = [ended("104", "C.P.U.", room="12", design_id="5030", Science=1), crew("102", "Delish", room="12")]
        decisions = (FinishTraining(104, "training ended"), StartTraining(102, 5030, "primary"),
                     UseConsumable(104, 902, "advisory"))
        with patch("sdk.policy.training.plan_training", return_value=decisions):
            result = plan(snap(records, inventory=[item()]))
        self.assertEqual(training_ids(result), [("finish_training", {"character_id": "104"}), start("102", "5030")])
        self.assertEqual(reasons(result)["use_consumable 104"], DEFERRED)
        with patch("sdk.policy.training.plan_training", return_value=decisions[1:]):  # paired control: no finish
            self.assertEqual(training_ids(plan(snap(records, inventory=[item()]))), [start("102", "5030"), USE_CPU])

    def test_finish_inside_the_grace_is_rejected_at_plan_time(self):
        end = server_time(T0 - timedelta(seconds=30))
        result = plan(snap([ended(ago=timedelta(seconds=30))]))
        self.assertEqual(training_requests(result), [])
        # The literal pin of a catalog precondition's text (other tests derive it with catalog_reason).
        self.assertEqual(reasons(result)["finish_training 101"],
                         f"101's training ends at {end}, less than 60 seconds before the observation; "
                         "finishing now could cost starbux")
        self.assertEqual(reasons(result)["add_training 101"], DEFERRED)
        boundary = plan(snap([ended(ago=timedelta(seconds=60))]))  # paired control: exactly the grace
        self.assertEqual(training_ids(boundary), [("finish_training", {"character_id": "101"})])

    def test_running_training_is_a_training_in_progress_rejection(self):
        running = snap([ended(ago=-timedelta(hours=1))])
        result = plan(running)
        self.assertEqual(training_requests(result), [])
        self.assertEqual(reasons(result)["training 101"], planner_reasons(running)[("Skip", 101)])
        self.assertTrue(reasons(result)["training 101"].startswith("training_in_progress: "))
        self.assertNotIn("training 101", reasons(plan()))  # paired control


class ManyTests(unittest.TestCase):
    def test_mixed_crew_finishes_first_then_primaries_in_crew_order(self):
        # The ended crew is listed second, so crew-order interleaving would put a start before the finish.
        records = [crew("102", "Delish", room="12"), ended(), crew("103", "Cyber Duck", design_id="7002")]
        self.assertEqual(training_ids(plan(snap(records))), [
            ("finish_training", {"character_id": "101"}), start("102", "5030"), start("103", "5040")])

    def test_each_skip_reason_is_a_rejection(self):
        records = [crew("105", "Ada"), crew("106", "Admiral Serena", room="13"),
                   crew("107", "Galactic Snow Maiden", design_id="7003", room="12", Weapon=80), crew()]
        result = plan(snap(records, research=[]))
        self.assertEqual(training_requests(result), [])
        # The literal pin of a Skip's "code: detail" (other tests derive it with planner_reasons).
        self.assertEqual({candidate: reason for candidate, reason in reasons(result).items()
                          if candidate.startswith("training ")}, {
            "training 105": "unknown_role: 'Ada' has no training role",
            "training 106": "not_in_training_room: room 'Bridge' is not a training room",
            "training 107": "tp_cap_reached: 80 TP >= cap 80 TP (80% of capacity 100)",
            "training 101": "research_missing: primary T1 research gate not met",
        })
        self.assertEqual(training_ids(plan()), [start()])  # paired control: Fitness completed


class ConsumableTests(unittest.TestCase):
    def test_consumable_used_when_no_design_and_item_in_stock(self):
        stocked = snap([CPU], inventory=[item()])
        reason = planner_reasons(stocked)[("UseConsumable", 104)]
        self.assertEqual(training_requests(plan(stocked)), [ActionRequest(*USE_CPU, reason, {"minerals": 0, "gas": 0})])
        none_left = snap([CPU], inventory=[item(quantity="0")])  # paired control: none in stock
        empty = plan(none_left)
        self.assertEqual(training_requests(empty), [])
        self.assertEqual(reasons(empty)["training 104"], planner_reasons(none_left)[("Skip", 104)])
        self.assertTrue(reasons(empty)["training 104"].startswith("design_missing: "))

    def test_consumable_in_two_inventory_records_is_rejected_at_plan_time(self):
        self.assertEqual(training_ids(plan(snap([CPU], inventory=[item()]))), [USE_CPU])  # paired control
        doubled = snap([CPU], inventory=[item(), item(item_id="9002")])
        result = plan(doubled)
        self.assertEqual(training_requests(result), [])
        self.assertEqual(reasons(result)["use_consumable 104"], catalog_reason("use_consumable", doubled, USE_CPU[1]))
        self.assertIn("2 inventory records", reasons(result)["use_consumable 104"])

    def test_a_strategy_with_its_own_consumables_is_used_unchanged(self):
        own = with_consumables(DEFAULT_STRATEGY, {("SCI", "Common"): 903})
        self.assertIs(training_strategy(own, design()), own)
        stock = [item(), item(item_id="9003", design_id="903")]
        # paired control: the default strategy takes the item designs' 902
        self.assertEqual(training_ids(plan(snap([CPU], inventory=stock))), [USE_CPU])
        custom = TrainingV1(own).plan(snap([CPU], inventory=stock), design(), PolicyConfig())
        self.assertEqual(training_ids(custom), [("use_consumable", {"item_design_id": "903", "character_id": "104"})])

    def test_no_item_designs_attaches_no_consumables(self):
        bare = design(item_designs=())
        self.assertIs(training_strategy(DEFAULT_STRATEGY, bare), DEFAULT_STRATEGY)
        self.assertEqual(training_ids(plan(snap([CPU], inventory=[item()]))), [USE_CPU])  # paired control
        result = plan(snap([CPU], inventory=[item()]), bare)
        self.assertEqual(training_requests(result), [])
        self.assertTrue(reasons(result)["training 104"].startswith("design_missing: "))

    def test_inventory_section_missing_plans_no_consumable(self):
        observed, _ = training_payloads(snap([CPU], inventory=[]), design())
        unobserved = snap([CPU], inventory=None)
        missing, _ = training_payloads(unobserved, design())
        assert observed is not None and missing is not None
        self.assertEqual(observed["items_raw"], {"Item": []})  # paired control: observed and empty
        self.assertIsNone(missing["items_raw"])
        result = plan(unobserved)
        self.assertEqual(training_requests(result), [])
        self.assertTrue(reasons(result)["training 104"].startswith("design_missing: "))

    def test_a_training_error_keeps_the_fleet_plan_and_rejects_training(self):
        # A custom strategy without the Legendary tier cannot take the item designs' Legendary consumable:
        # with_consumables re-runs validation and raises. fleet-v1's economy plan must survive it.
        no_legendary = replace(DEFAULT_STRATEGY, rarity_stars={tier: stars for tier, stars in RARITY_STARS.items()
                                                               if tier != "Legendary"})
        legendary = design(item_designs=[*ITEM_DESIGNS, {"@ItemDesignId": "905", "@ItemSubType": "Consumable",
                                                         "@ItemName": "Legendary Weapon Consumable"}])
        fleet = plan(snap(), legendary, policy="fleet-v1")
        self.assertNotEqual(fleet.requests, ())
        # paired control: the same strategy plans training when the item designs fit it
        self.assertEqual(training_ids(TrainingV1(no_legendary).plan(snap(), design(), PolicyConfig())), [start()])
        with self.assertRaises(ValueError):
            training_strategy(no_legendary, legendary)
        result = TrainingV1(no_legendary).plan(snap(), legendary, PolicyConfig())
        self.assertEqual(result.requests, fleet.requests)
        economy = tuple(item for item in fleet.rejected if item.candidate != "add_training")  # not "not modelled"
        self.assertEqual(result.rejected, economy + (Rejection(
            "training", "training not planned: ValueError: invalid consumable key: ('WPN', 'Legendary')"),))

    def test_consumable_stock_is_shared_between_crew(self):
        twin = crew("108", "C.P.U.", room="12", Science=1)
        self.assertEqual(training_ids(plan(snap([CPU, twin], inventory=[item(quantity="2")]))),
                         [USE_CPU, ("use_consumable", {"item_design_id": "902", "character_id": "108"})])
        self.assertEqual(training_ids(plan(snap([CPU, twin], inventory=[item(quantity="1")]))), [USE_CPU])


class BoundaryTests(unittest.TestCase):
    def test_max_requests_cuts_training_first(self):
        fleet = plan(snap(MIXED), policy="fleet-v1")
        limit = len(fleet.requests) + 1
        result = plan(snap(MIXED), max_requests=limit)
        self.assertEqual(ids(result), ids(fleet) + [("finish_training", {"character_id": "101"})])
        over = [item for item in result.rejected if item.reason == f"over max_requests ({limit})"]
        self.assertEqual(over, [Rejection("add_training", f"over max_requests ({limit})")] * 2)

    def test_tp_cap_boundary(self):
        # Capacity 10: at 5 TP (50%, primary T1) Cyber Duck starts; at 9 TP the cap is not reached yet (90% is
        # "complete", checked after the cap); at 10 TP the cap is reached.
        self.assertEqual(training_ids(plan(snap([crew("103", "Cyber Duck", design_id="7002", Hp=5)]))),
                         [start("103", "5040")])
        nine, ten = (snap([crew("103", "Cyber Duck", design_id="7002", Hp=hp)]) for hp in (9, 10))
        below = plan(nine)
        self.assertEqual(reasons(below)["training 103"], planner_reasons(nine)[("Skip", 103)])
        self.assertTrue(reasons(below)["training 103"].startswith("complete: "))
        at_cap = plan(ten)
        self.assertEqual(training_requests(at_cap), [])
        self.assertEqual(reasons(at_cap)["training 103"], planner_reasons(ten)[("Skip", 103)])
        self.assertTrue(reasons(at_cap)["training 103"].startswith("tp_cap_reached: "))


class ContractTests(unittest.TestCase):
    def test_snapshot_conversion_matches_parsing_the_raw_payloads(self):
        records = MIXED + [CPU, ended("109", "Huntress", ago=-timedelta(hours=1), Attack=3, Hp=2, fatigue="40")]
        items = [item(quantity="3"), item(quantity="1", item_id="9003", design_id="903")]
        context, rejected = training_context(snap(records, inventory=items), design())
        self.assertEqual(rejected, ())
        expected = build_context(
            now=T0, characters_raw={"Character": records}, character_designs_raw={"CharacterDesign": CHARACTER_DESIGNS},
            training_designs_raw={"TrainingDesign": TRAINING_DESIGNS}, rooms_raw={"Room": ROOMS},
            room_designs_raw={"RoomDesign": ROOM_DESIGNS}, researches_raw={"Research": RESEARCH},
            research_designs_raw={"ResearchDesign": RESEARCH_DESIGNS}, items_raw={"Item": items})
        self.assertEqual(len(expected.crew), 5)
        self.assertEqual(context.crew, expected.crew)
        self.assertEqual(context.designs, expected.designs)
        self.assertEqual(dict(context.rooms), dict(expected.rooms))
        self.assertEqual(dict(context.research_levels), dict(expected.research_levels))
        self.assertEqual(dict(context.items), {902: 3, 903: 1})
        self.assertEqual(context, expected)

    def test_research_course_reaches_the_parser(self):
        # The level is the live name's course ("Fitness 201" -> 201); an unverified @Level is neither kept nor read.
        levelled = [{"@ResearchDesignId": "611", "@RootResearchDesignId": "611", "@ResearchName": "Fitness 201",
                     "@Level": "2", "@GasCost": "10"}]
        research = [{"@ResearchDesignId": "611", "@ResearchState": "Completed"}]
        expected = build_context(
            now=T0, characters_raw={"Character": [crew()]}, character_designs_raw={"CharacterDesign": CHARACTER_DESIGNS},
            training_designs_raw={"TrainingDesign": TRAINING_DESIGNS}, researches_raw={"Research": research},
            research_designs_raw={"ResearchDesign": levelled})
        self.assertEqual(dict(expected.research_levels), {"Fitness": 201, "Fitness 201": 201})
        context, _ = training_context(snap(research=research), design(research_designs=levelled))
        self.assertEqual(dict(context.research_levels), dict(expected.research_levels))

    def test_each_observed_character_record_is_rebuilt_exactly(self):
        # Lossless on the wire shape the parser reads: an idle crew member keeps @TrainingDesignId "0" ("0" = none,
        # sdk.training.parse) and an empty @TrainingEndDate, a training one its design id and end date.
        records = MIXED + [CPU]
        payloads, rejected = training_payloads(snap(records), design())
        self.assertEqual(rejected, ())
        assert payloads is not None
        self.assertEqual(payloads["characters_raw"], {"Character": records})

    def test_research_family_can_come_only_from_the_root_design(self):
        # A course whose own name names another family: "Fitness" comes only through @RootResearchDesignId.
        levelled = [{"@ResearchDesignId": "610", "@RootResearchDesignId": "610", "@ResearchName": "Fitness 101",
                     "@GasCost": "10"},
                    {"@ResearchDesignId": "620", "@RootResearchDesignId": "610", "@ResearchName": "Conditioning 201",
                     "@GasCost": "20"}]
        research = [{"@ResearchDesignId": "620", "@ResearchState": "Completed"}]

        def raw_context(research_designs):
            return build_context(
                now=T0, characters_raw={"Character": [crew()]},
                character_designs_raw={"CharacterDesign": CHARACTER_DESIGNS},
                training_designs_raw={"TrainingDesign": TRAINING_DESIGNS}, rooms_raw={"Room": ROOMS},
                room_designs_raw={"RoomDesign": ROOM_DESIGNS}, researches_raw={"Research": research},
                research_designs_raw={"ResearchDesign": research_designs})

        expected = raw_context(levelled)
        self.assertEqual(dict(expected.research_levels), {"Fitness": 201, "Conditioning 201": 201})
        context, _ = training_context(snap(research=research), design(research_designs=levelled))
        assert context is not None
        self.assertEqual(dict(context.research_levels), dict(expected.research_levels))
        self.assertEqual(training_ids(plan(snap(research=research), design(research_designs=levelled))), [start()])
        # Paired control: without the root, the family is the design's own name and the Fitness gate is not met.
        rootless = [dict(record, **{"@RootResearchDesignId": ""}) for record in levelled]
        self.assertEqual(dict(raw_context(rootless).research_levels), {"Conditioning": 201, "Conditioning 201": 201})
        result = plan(snap(research=research), design(research_designs=rootless))
        self.assertEqual(training_requests(result), [])
        self.assertTrue(reasons(result)["training 101"].startswith("research_missing: "))

    def test_plan_uses_the_observation_time_not_the_wall_clock(self):
        running = ended(ago=-timedelta(hours=1))
        self.assertLess(T0 + timedelta(hours=1), datetime.now(timezone.utc))  # the end is past on the wall clock
        result = plan(snap([running]))
        self.assertEqual(training_requests(result), [])
        self.assertTrue(reasons(result)["training 101"].startswith("training_in_progress: "))
        later = plan(snap([running], observed_at=T0 + timedelta(hours=2)))  # paired control: observed after
        self.assertEqual(training_ids(later), [("finish_training", {"character_id": "101"})])

    def test_plan_is_deterministic_and_pure(self):
        snapshot, data = snap(MIXED + [CPU], inventory=[item()]), design()
        before = (copy.deepcopy(snapshot), copy.deepcopy(data))
        first, second = plan(snapshot, data), plan(snapshot, data)
        self.assertTrue(training_requests(first))
        self.assertEqual(first, second)
        self.assertEqual((snapshot, data), before)


class DesignClient:
    """A stub client: listAllDesigns4 keeps the design lists as Client.listAllDesigns4 does (ListAllDesigns shape)."""

    def __init__(self, loaded=True):
        self.loaded = loaded
        self.calls = []

    def listAllDesigns4(self):
        self.calls.append("listAllDesigns4")
        if not self.loaded:
            self.trainingDesigns = {"TrainingDesigns": {"TrainingDesign": TRAINING_DESIGNS}}  # stale, never read
            return False
        self.shipDesigns = {"ShipDesigns": {"ShipDesign": [{"@ShipDesignId": "1", "@ShipLevel": "1"}]}}
        self.trainingDesigns = {"TrainingDesigns": {"TrainingDesign": TRAINING_DESIGNS}}
        self.characterDesigns = {"CharacterDesigns": {"CharacterDesign": CHARACTER_DESIGNS}}
        self.itemDesigns = {"ItemDesigns": {"ItemDesign": ITEM_DESIGNS}}
        return True

    def listRoomDesigns2(self):
        self.calls.append("listRoomDesigns2")
        self.roomDesigns = {"RoomDesigns": {"RoomDesign": ROOM_DESIGNS}}

    # The per-list reads (Client._fetchDesignList: True and the attribute set only on a good reply).
    lists_ok = True

    def listAllTrainingDesigns2(self):
        self.calls.append("listAllTrainingDesigns2")
        if self.lists_ok:
            self.trainingDesigns = {"TrainingDesigns": {"TrainingDesign": TRAINING_DESIGNS}}
        return self.lists_ok

    def listAllCharacterDesigns2(self):
        self.calls.append("listAllCharacterDesigns2")
        # Left on the client even when this read fails (a stale earlier reply), so it must not be trusted then.
        self.allCharacterDesigns = {"CharacterDesigns": {"CharacterDesign": CHARACTER_DESIGNS}}
        return self.lists_ok

    def listAllResearchDesigns2(self):
        self.calls.append("listAllResearchDesigns2")
        self.allResearchDesigns = {"ResearchDesigns": {"ResearchDesign": RESEARCH_DESIGNS}}


class GateAndLiveDesignContractTests(unittest.TestCase):
    def test_zero_cost_actions_are_defined_once_and_pinned(self):
        from sdk.fleet import factory
        from sdk.policy import training

        # The one literal pin: widening the set lets a new uncosted action past the execute gate's caps.
        self.assertEqual(training.ZERO_COST_ACTIONS, frozenset({"add_training", "use_consumable"}))
        self.assertIs(factory.ZERO_COST_ACTIONS, training.ZERO_COST_ACTIONS)
        self.assertEqual(set(training.TRAINING_ZERO_COST), set(factory.CAP_KINDS))
        self.assertEqual(len(training.TRAINING_ZERO_COST), len(factory.CAP_KINDS))

    def test_planned_zero_satisfies_the_execute_gate_declaration(self):
        from sdk.fleet.factory import declares_zero_cost
        from sdk.policy.training import TRAINING_ZERO_COST, ZERO_COST_ACTIONS

        requests = training_requests(plan(snap(MIXED + [CPU], inventory=[item()])))
        self.assertEqual({request.action_id for request in requests}, {"finish_training"} | ZERO_COST_ACTIONS)
        for request in requests:
            with self.subTest(request=request):
                declared = request.expected_cost == dict(TRAINING_ZERO_COST)
                # Every request carrying the explicit zero is one the gate lets through, and only those carry it.
                self.assertEqual(declared, request.action_id in ZERO_COST_ACTIONS)
                self.assertEqual(declares_zero_cost(request.expected_cost), declared)
                self.assertEqual(planned_cost(request.expected_cost), {})

    def test_read_design_data_passes_training_records_without_new_calls(self):
        from sdk.fleet.factory import read_design_data

        client = DesignClient()
        data, notes = read_design_data(client, training=True)
        self.assertEqual(notes, ())
        self.assertEqual(client.calls, ["listAllDesigns4", "listRoomDesigns2", "listAllResearchDesigns2"])
        self.assertEqual(data, design(ship_designs=[{"@ShipDesignId": "1", "@ShipLevel": "1"}]))
        self.assertEqual(len(data.training_designs), len(TRAINING_DESIGNS))
        stale = DesignClient(loaded=False)
        stale.lists_ok = False  # the per-list reads fail too, so only the stale listAllDesigns4 records are left
        data, notes = read_design_data(stale, training=True)
        self.assertEqual((data.training_designs, data.character_designs, data.item_designs), ((), (), ()))
        self.assertEqual(notes[:1], ("hull designs unavailable (listAllDesigns4 returned no data)",))

    def test_a_raising_listAllDesigns4_leaves_no_stale_records(self):
        from sdk.fleet.factory import read_design_data

        class Raising(DesignClient):
            def listAllDesigns4(self):
                super().listAllDesigns4()  # an earlier reply's records stay on the client
                raise KeyError("ShipService")

        data, notes = read_design_data(Raising())
        self.assertEqual((data.hulls, data.training_designs, data.character_designs, data.item_designs),
                         ((), (), (), ()))
        self.assertEqual(notes, ("hull designs unavailable (listAllDesigns4 raised KeyError)",))
        self.assertEqual(len(read_design_data(DesignClient())[0].hulls), 1)  # paired control: the same records read

    def test_a_reply_without_ShipDesign_records_says_so(self):
        from sdk.fleet.factory import read_design_data

        class NoHulls(DesignClient):
            def listAllDesigns4(self):
                loaded = super().listAllDesigns4()
                del self.shipDesigns
                return loaded

        data, notes = read_design_data(NoHulls(), training=True)
        self.assertEqual(notes, ("hull designs unavailable (no ShipDesign records)",))
        self.assertEqual((data.hulls, len(data.training_designs)), ((), len(TRAINING_DESIGNS)))

    def test_live_shaped_design_data_reaches_training_v1(self):
        from sdk.fleet.factory import read_design_data

        data, _ = read_design_data(DesignClient(), training=True)
        self.assertEqual(training_ids(plan(design_data=data)), [start()])

    def test_a_non_training_account_converts_no_training_records(self):
        from sdk.fleet.factory import read_design_data

        reads = ["listAllDesigns4", "listRoomDesigns2", "listAllResearchDesigns2"]
        client = DesignClient()  # the reply carries every training list, as a live one does
        data, notes = read_design_data(client)
        self.assertEqual((client.calls, notes), (reads, ()))
        self.assertEqual((data.training_designs, data.character_designs, data.item_designs), ((), (), ()))
        self.assertEqual(data, design(ship_designs=[{"@ShipDesignId": "1", "@ShipLevel": "1"}], training_designs=(),
                                      character_designs=(), item_designs=()))
        trained = DesignClient()  # paired control: the same reply fills them for a training account, with no call
        data, notes = read_design_data(trained, training=True)
        self.assertEqual((trained.calls, notes), (reads, ()))
        self.assertEqual((len(data.training_designs), len(data.character_designs), len(data.item_designs)),
                         (len(TRAINING_DESIGNS), len(CHARACTER_DESIGNS), len(ITEM_DESIGNS)))

    def test_training_reads_fall_back_to_the_per_list_calls_when_listAllDesigns4_gives_none(self):
        from sdk.fleet.factory import read_design_data

        reads = ["listAllDesigns4", "listRoomDesigns2", "listAllResearchDesigns2"]
        plain = DesignClient(loaded=False)  # paired control: without training, no extra call and no records
        data, _ = read_design_data(plain)
        self.assertEqual((plain.calls, data.training_designs, data.character_designs), (reads, (), ()))
        client = DesignClient(loaded=False)
        data, notes = read_design_data(client, training=True)
        self.assertEqual(client.calls, reads + ["listAllTrainingDesigns2", "listAllCharacterDesigns2"])
        self.assertEqual(notes, ("hull designs unavailable (listAllDesigns4 returned no data)",))
        self.assertEqual((len(data.training_designs), len(data.character_designs)),
                         (len(TRAINING_DESIGNS), len(CHARACTER_DESIGNS)))
        self.assertEqual(training_ids(plan(design_data=data)), [start()])

    def test_training_reads_make_no_extra_call_when_listAllDesigns4_has_the_records(self):
        from sdk.fleet.factory import read_design_data

        client = DesignClient()
        data, notes = read_design_data(client, training=True)
        self.assertEqual(client.calls, ["listAllDesigns4", "listRoomDesigns2", "listAllResearchDesigns2"])
        self.assertEqual((notes, len(data.training_designs)), ((), len(TRAINING_DESIGNS)))

    def test_a_failed_per_list_read_leaves_its_records_empty_with_a_note(self):
        from sdk.fleet.factory import read_design_data

        client = DesignClient(loaded=False)
        client.lists_ok = False  # the stale allCharacterDesigns left on the client must not be read
        data, notes = read_design_data(client, training=True)
        self.assertEqual((data.training_designs, data.character_designs), ((), ()))
        self.assertEqual(notes[1:], ("TrainingDesign records unavailable (listAllTrainingDesigns2 returned no data)",
                                     "CharacterDesign records unavailable (listAllCharacterDesigns2 returned no data)"))

        class Raising(DesignClient):
            def listAllTrainingDesigns2(self):
                raise KeyError("TrainingService")

        data, notes = read_design_data(Raising(loaded=False), training=True)
        self.assertEqual(data.training_designs, ())
        self.assertIn("TrainingDesign records unavailable (listAllTrainingDesigns2 raised KeyError)", notes)
        self.assertEqual(len(data.character_designs), len(CHARACTER_DESIGNS))


class FailClosedTests(unittest.TestCase):
    """A crew member whose training inputs are not all observed is left out with the reason, never guessed."""

    def assert_excluded(self, record, reason, **snap_kwargs):
        self.assertEqual(training_ids(plan()), [start()])  # paired control: the canonical crew starts
        result = plan(snap([record], **snap_kwargs))
        self.assertEqual(training_requests(result), [])
        self.assertEqual(reasons(result)[f"training {record['@CharacterId']}"], reason)

    def test_unobserved_training_state_excludes_the_crew(self):
        record = crew()
        del record["@TrainingEndDate"]
        self.assert_excluded(record, "training state not observed")

    def test_unobserved_stat_excludes_the_crew(self):
        record = crew()
        del record["@AttackImprovement"]
        self.assert_excluded(record, "stat improvements not observed")

    def test_unobserved_fatigue_excludes_the_crew(self):
        record = crew()
        del record["@Fatigue"]
        self.assert_excluded(record, "fatigue not observed")

    def test_unobserved_item_quantity_gives_no_consumable(self):
        self.assertEqual(training_ids(plan(snap([CPU], inventory=[item()]))), [USE_CPU])  # paired control
        unread = item()
        del unread["@Quantity"]
        result = plan(snap([CPU], inventory=[unread]))
        self.assertEqual(training_requests(result), [])
        self.assertTrue(reasons(result)["training 104"].startswith("design_missing: "))

    def test_a_decision_with_no_action_is_a_type_error_even_under_python_O(self):
        # action_for returns None only for a Skip, which never reaches it; a decision it cannot map must fail loudly
        # (a TypeError, not an assert that python -O strips), and must not be swallowed as a training rejection.
        self.assertEqual(training_ids(plan()), [start()])  # paired control
        with patch("sdk.policy.training.action_for", return_value=None):
            with self.assertRaisesRegex(TypeError, r"^action_for mapped StartTraining\(.*\) to no action$"):
                plan()

    def test_non_numeric_character_id_is_rejected_not_dropped(self):
        self.assert_excluded(crew(cid="c1"), "character id 'c1' is not a positive whole number")


class RegistryImportTests(unittest.TestCase):
    def test_importing_the_training_module_first_still_registers_training_v1(self):
        code = ("import sdk.policy.training as t; from sdk.policy import POLICIES; "
                "assert isinstance(POLICIES['training-v1'], t.TrainingV1), POLICIES; print('ok')")
        done = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                              timeout=60, env={**os.environ, "PYTHONPATH": REPO_ROOT})
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "ok"), done.stderr)


# --- training-v2 (SPEC-14 step 3) ---------------------------------------------------------------------------------

V2_COURSE = {"@TrainingDesignId": "40001", "@TrainingName": "Study Expert Engineering Manual", "@RepairChance": "100",
             "@Rank": "1", "@RequiredRoomType": "Academy", "@RequiredRoomLevel": "2",
             "@RequiredResearchDesignId": "610", "@CostString": "mineral:500"}
V2_ROOM_DESIGNS = {"RoomService": {"ListRoomDesigns": {"RoomDesigns": {"RoomDesign": [
    {"@RoomDesignId": "300", "@RoomName": "GYM", "@RoomType": "Training", "@Level": "1", "@Capacity": "2"},
    {"@RoomDesignId": "400", "@RoomName": "Academy", "@RoomType": "Training", "@Level": "2", "@Capacity": "2"},
    {"@RoomDesignId": "500", "@RoomName": "Bridge", "@RoomType": "Bridge", "@Level": "1"},
    {"@RoomDesignId": "600", "@RoomName": "Storage", "@RoomType": "Storage", "@Level": "1"},
]}}}}
STORAGE_ROOMS = ROOMS + [{"@RoomId": "14", "@RoomDesignId": "600", "@RoomStatus": "Normal"}]
STORAGE_DESIGNS = ROOM_DESIGNS + [{"@RoomDesignId": "600", "@RoomName": "Storage"},
                                  {"@RoomDesignId": "601", "@RoomName": "Storage Lv2",
                                   "@UpgradeFromRoomDesignId": "600", "@PriceString": "mineral:600"}]


def courses_payload(*records):
    return {"TrainingService": {"ListAllTrainingDesigns": {"TrainingDesigns": {"TrainingDesign": list(records)}}}}


def fixer(cid="101", name="Fixer", room="12", **kwargs):
    """An idle repair crew (Repair 30, design 7003: capacity 100) in the level 2 Academy (room 12)."""
    return crew(cid, name, design_id="7003", room=room, **{"Repair": 30, **kwargs})


def v2_snap(records=(), course_records=(V2_COURSE,), **overrides):
    """A training-v2 snapshot: ``records`` () is the one canonical fixer; None leaves the crew unobserved."""
    return snap([fixer()] if records == () else records, training_designs=courses_payload(*course_records),
                room_designs=V2_ROOM_DESIGNS, **overrides)


def v2_plan(snapshot=None, design_data=None, **config):
    return plan(snapshot or v2_snap(), design_data, policy="training-v2", **config)


V2_START = ("add_training", {"training_design_id": "40001", "character_id": "101"})


class CrewInputsTests(unittest.TestCase):
    # Verifies: S14-R1, S14-R2
    def inputs(self, snapshot=None, data=None, **options):
        from sdk.policy.training import crew_inputs

        values = {"funds": {"mineral": 1000, "gas": 2000}, "spend_caps": {"mineral": None, "gas": None},
                  "action_slots": None, **options}
        return crew_inputs(snapshot or v2_snap(), data or design(), **values)

    def test_observed_crew_rooms_research_and_capacities(self):
        from sdk.training.model import TrainingSession
        from sdk.training.planner import RoomObservation

        inputs, rejected = self.inputs()
        self.assertEqual(rejected, ())
        (member,) = inputs.crew
        self.assertEqual((member.character_id, member.design_id, member.fatigue, member.room_id),
                         (101, 7003, 0, "12"))
        self.assertEqual(dict(member.stats), {**{f"{stat}Improvement": 0 for stat in STATS_NAMES},
                                              "RepairImprovement": 30})
        self.assertEqual(member.session, TrainingSession(None, None, "none"))
        self.assertEqual(inputs.rooms["12"], RoomObservation("12", "Academy", "Training", 2, 2, "Normal", 0))
        self.assertEqual(inputs.rooms["13"], RoomObservation("13", "Bridge", "Bridge", 1, None, "Normal", 0))
        self.assertEqual(set(inputs.rooms), {"11", "12", "13"})
        self.assertEqual(dict(inputs.research), {610: "Completed"})
        self.assertEqual(dict(inputs.capacities), {7001: 20, 7002: 10, 7003: 100})
        self.assertEqual([course.design_id for course in inputs.courses], [40001])
        self.assertEqual((dict(inputs.funds), dict(inputs.spend_caps), inputs.action_slots),
                         ({"mineral": 1000, "gas": 2000}, {"mineral": None, "gas": None}, None))
        self.assertEqual(self.inputs(action_slots=3)[0].action_slots, 3)

    def test_a_missing_stat_is_unknown_and_an_observed_zero_stays(self):
        record = fixer()
        del record["@AttackImprovement"]
        inputs, _ = self.inputs(v2_snap([record, fixer("102", Attack=0)]))
        self.assertIsNone(inputs.crew[0].stats)
        self.assertEqual(inputs.crew[1].stats["AttackImprovement"], 0)

    def test_running_sessions_and_unknowns(self):
        running = fixer("102", training="5010", end=server_time(T0 + timedelta(hours=1)))
        snapshot = v2_snap([fixer(), running])
        inputs, _ = self.inputs(snapshot)
        self.assertEqual(inputs.rooms["12"].running, snapshot.running_sessions("12"))
        self.assertEqual(inputs.rooms["12"].running, 1)
        self.assertEqual(inputs.crew[1].session.phase, "running")
        unknown = fixer()
        del unknown["@TrainingEndDate"]
        inputs, _ = self.inputs(v2_snap([unknown]))
        self.assertIsNone(inputs.rooms["12"].running)
        self.assertEqual(inputs.crew[0].session.phase, "unknown")

    def test_research_is_none_unless_completely_observed(self):
        from sdk.state import Completeness

        self.assertIsNone(self.inputs(v2_snap(research=None))[0].research)
        # A non-numeric id still parses as a complete section; crew_inputs drops only the bad entry.
        complete = v2_snap(research=[{"@ResearchDesignId": "610", "@ResearchState": "Completed"},
                                     {"@ResearchDesignId": "x"}])
        self.assertIs(complete.section("research"), Completeness.COMPLETE)
        self.assertEqual(dict(self.inputs(complete)[0].research), {610: "Completed"})

    def test_partial_research_section_is_unknown(self):
        # QA survivor M08: a PARTIAL research section must not be read as observed research.
        from sdk.state import Completeness
        from sdk.training.planner import plan_crew

        partial = v2_snap(research=[{"@ResearchDesignId": "610", "@ResearchState": "Completed"},
                                    {"@ResearchState": "Completed"}],
                          course_records=(dict(V2_COURSE, **{"@RequiredResearchDesignId": "611"}),))
        self.assertIs(partial.section("research"), Completeness.PARTIAL)
        inputs, _ = self.inputs(partial)
        self.assertIsNone(inputs.research)
        (decision,) = [entry for entry in plan_crew(inputs).decisions if entry.character_id == 101]
        self.assertEqual((decision.code, decision.unknown), ("research_unknown", True))

    def test_ids_that_are_not_whole_numbers(self):
        inputs, rejected = self.inputs(v2_snap([fixer("c1"), fixer("102"), crew("103", design_id="x7", room="12")]))
        self.assertEqual([member.character_id for member in inputs.crew], [102, 103])
        self.assertIsNone(inputs.crew[1].design_id)
        self.assertEqual(rejected, (Rejection("training c1", "character id 'c1' is not a positive whole number"),))
        data = design(character_designs=[{"@CharacterDesignId": "7003", "@TrainingCapacity": "lots"},
                                         {"@CharacterDesignId": "abc", "@TrainingCapacity": "5"},
                                         {"@CharacterDesignId": "7001", "@TrainingCapacity": "0"}])
        self.assertEqual(dict(self.inputs(data=data)[0].capacities), {7001: 0})
        research = v2_snap(research=[{"@ResearchDesignId": "r9", "@ResearchState": "Completed"}] + RESEARCH)
        self.assertEqual(dict(self.inputs(research)[0].research), {610: "Completed"})

    def test_no_crew_section_plans_nothing(self):
        inputs, rejected = self.inputs(v2_snap(None))
        self.assertIsNone(inputs)
        self.assertEqual(rejected, (Rejection("training", "crew section not observed"),))

    def test_a_partial_crew_section_is_an_unknown_roster(self):
        inputs, rejected = self.inputs(v2_snap([fixer(), {"@CharacterName": "No Id"}]))
        self.assertIsNone(inputs)
        self.assertEqual(rejected, (Rejection("training", "crew section partial: the roster is unknown"),))
        self.assertIsNotNone(self.inputs()[0])  # control: a complete section

    def test_courses_unobserved_stay_none(self):
        self.assertIsNone(self.inputs(snap([fixer()], room_designs=V2_ROOM_DESIGNS))[0].courses)


STATS_NAMES = ("Hp", "Pilot", "Repair", "Weapon", "Science", "Engine", "Attack", "Ability", "Stamina")


class TrainingV2PlanTests(unittest.TestCase):
    # Verifies: S14-R1, S14-R2, S14-R3, S14-R4
    def test_registered_only_as_training_v2(self):
        from sdk.policy import POLICIES, TrainingV2, get_policy

        policy = get_policy("training-v2")
        self.assertIsInstance(policy, TrainingV2)
        self.assertIsInstance(policy, TrainingV1)
        self.assertEqual((policy.name, policy.models_training, policy.reads_course_designs),
                         ("training-v2", True, True))
        self.assertEqual(list(POLICIES)[0], "baseline-v1")
        self.assertEqual(list(POLICIES)[-1], "training-v2")
        self.assertFalse(getattr(get_policy("training-v1"), "reads_course_designs", False))
        self.assertEqual(v2_plan().policy, "training-v2")

    def test_training_after_the_progression_block(self):
        from sdk.policy import get_policy

        result = v2_plan()
        base = get_policy("fleet-v1").plan(v2_snap(), design(), PolicyConfig())
        self.assertEqual(result.requests[:-1], base.requests)
        self.assertGreater(len(base.requests), 0)
        self.assertEqual(result.requests[-1], ActionRequest(
            "add_training", {"training_design_id": "40001", "character_id": "101"},
            "repair specialist: Study Expert Engineering Manual", {"minerals": 500, "gas": 0}))
        self.assertNotIn("add_training", reasons(result))  # training is modelled: no "not modelled" rejection

    def test_earlier_spend_counts(self):
        data = design(room_designs=STORAGE_DESIGNS)
        for minerals, planned in (("900", False), ("1100", True)):
            with self.subTest(minerals=minerals):
                snapshot = v2_snap(rooms=STORAGE_ROOMS, resources={"minerals": minerals, "gas": "0"})
                result = v2_plan(snapshot, data)
                self.assertIn("upgrade_room", [request.action_id for request in result.requests])
                self.assertEqual(V2_START in training_ids(result), planned)
                if not planned:
                    self.assertEqual(reasons(result)["training 101"],
                                     "insufficient_funds: course 40001 costs 500 mineral, 300 spendable")

    def test_reserves_count(self):
        self.assertIn(V2_START, training_ids(v2_plan(mineral_reserve=500)))
        self.assertTrue(reasons(v2_plan(mineral_reserve=501))["training 101"].startswith("insufficient_funds: "))

    def test_spend_cap_counts(self):
        self.assertIn(V2_START, training_ids(v2_plan(max_mineral_spend=500)))
        self.assertEqual(reasons(v2_plan(max_mineral_spend=400))["training 101"],
                         "cap_exceeded: course 40001 costs 500 mineral, 400 left under the cap")
        data = design(room_designs=STORAGE_DESIGNS)
        snapshot = v2_snap(rooms=STORAGE_ROOMS, resources={"minerals": "5000", "gas": "0"})
        self.assertTrue(reasons(v2_plan(snapshot, data, max_mineral_spend=1000))["training 101"].startswith(
            "cap_exceeded: "))
        self.assertIn(V2_START, training_ids(v2_plan(snapshot, data, max_mineral_spend=1100)))

    def test_max_requests_counts(self):
        from sdk.policy import get_policy

        base = len(get_policy("fleet-v1").plan(v2_snap(), design(), PolicyConfig()).requests)
        capped = v2_plan(max_requests=base)
        self.assertEqual(len(capped.requests), base)
        self.assertEqual(reasons(capped)["training 101"],
                         "action_cap_reached: course 40001 waits: 0 requests left under max_requests")
        self.assertIn(V2_START, training_ids(v2_plan(max_requests=base + 1)))
        self.assertEqual(len(v2_plan(max_requests=1).requests), 1)

    def test_one_add_training_and_never_finish_or_consumable(self):
        weapon = {**V2_COURSE, "@TrainingDesignId": "40002", "@TrainingName": "Drills", "@WeaponChance": "100",
                  "@RepairChance": "0", "@RequiredRoomLevel": "1", "@RequiredResearchDesignId": "0"}
        science = {**weapon, "@TrainingDesignId": "40003", "@TrainingName": "Primer", "@ScienceChance": "100",
                   "@WeaponChance": "0"}
        records = [ended(name="Fixer", room="12", Repair=50), fixer("102", Repair=0, Weapon=40),
                   fixer("103", Repair=0, Science=20)]
        result = v2_plan(v2_snap(records, course_records=(V2_COURSE, weapon, science), inventory=[item()]))
        self.assertEqual(training_ids(result), [("add_training", {"training_design_id": "40003",
                                                                  "character_id": "103"})])
        self.assertEqual(reasons(result)["training 101"].split(":")[0], "unclaimed_course")
        self.assertEqual(reasons(result)["training 102"].split(":")[0], "action_cap_reached")

    def test_expected_cost(self):
        from sdk.policy.training import TRAINING_ZERO_COST

        for price, cost in (("mineral:500", {"minerals": 500, "gas": 0}), ("gas:300", {"minerals": 0, "gas": 300}),
                            ("mineral:0", dict(TRAINING_ZERO_COST))):
            with self.subTest(price=price):
                result = v2_plan(v2_snap(course_records=({**V2_COURSE, "@CostString": price},)))
                self.assertEqual(dict(result.requests[-1].expected_cost), cost)

    def test_plan_and_crew_plan_agree(self):
        from sdk.policy import get_policy

        snapshot = v2_snap([fixer(), fixer("102", room="13", Weapon=9), fixer("103", Science=5, room="11")])
        policy = get_policy("training-v2")
        preview = policy.crew_plan(snapshot, design(), PolicyConfig())
        result = policy.plan(snapshot, design(), PolicyConfig())
        start = preview.plan.start
        self.assertEqual(result.requests[-1].params, {"training_design_id": str(start.course.design_id),
                                                      "character_id": str(start.character_id)})
        self.assertEqual(preview.base, get_policy("training-v2").crew_plan(snapshot, design(), PolicyConfig()).base)
        for decision in preview.plan.decisions:
            if decision.code is not None:
                self.assertEqual(reasons(result)[f"training {decision.character_id}"],
                                 f"{decision.code}: {decision.detail}")
        # One result: the plan's training block is the preview's requests and rejections.
        self.assertEqual(preview.decisions, preview.plan.decisions)
        self.assertEqual(result.requests[len(preview.base.requests):], preview.requests)
        self.assertEqual(result.rejected[len(preview.base.rejected):], preview.rejected)
        self.assertEqual(policy.training_block(snapshot, design(), PolicyConfig(), preview.base),
                         (preview.requests, preview.rejected))

    def test_uncovered_roles_are_rejections_by_role(self):
        result = v2_plan()
        self.assertEqual(reasons(result)["training shields"],
                         "role_uncovered: no observed crew for the ScienceImprovement slot")
        self.assertTrue(reasons(result)["training weapons"].startswith("role_uncovered: "))

    def test_a_failed_precondition_is_a_rejection(self):
        with patch("sdk.policy.training._unmet_preconditions", return_value="crew member busy"):
            result = v2_plan()
        self.assertNotIn(V2_START, training_ids(result))
        self.assertEqual(reasons(result)["add_training 101"], "crew member busy")

    def test_a_failed_precondition_is_the_same_skip_in_both_previews(self):
        from sdk.policy import get_policy
        from sdk.policy.training import crew_plan_lines
        from sdk.training.planner import CrewDecision

        policy, snapshot, data = get_policy("training-v2"), v2_snap(), design()
        control = policy.crew_plan(snapshot, data, PolicyConfig())
        self.assertEqual(control.requests[0].params, V2_START[1])  # control: the start is planned
        self.assertIn("  crew 101 repair: Study Expert Engineering Manual",
                      crew_plan_lines("alpha", policy, snapshot, data, control, {}))
        with patch("sdk.policy.training._unmet_preconditions", return_value="crew member busy") as unmet:
            preview = policy.crew_plan(snapshot, data, PolicyConfig())
            block = policy.training_block(snapshot, data, PolicyConfig(), preview.base)
        unmet.assert_called_with("add_training", snapshot, V2_START[1])
        self.assertEqual(preview.requests, ())
        self.assertEqual(preview.plan.start.character_id, 101)  # the planner chose it; the safety net refused it
        self.assertEqual(preview.decisions[0], CrewDecision("repair", 101, None, "training_in_progress",
                                                            "preconditions failed: crew member busy"))
        self.assertEqual(preview.decisions[1:], preview.plan.decisions[1:])
        self.assertIn(Rejection("add_training 101", "crew member busy"), preview.rejected)
        self.assertNotIn("training 101", [rejection.candidate for rejection in preview.rejected])
        self.assertEqual(block, (preview.requests, preview.rejected))
        lines = crew_plan_lines("alpha", policy, snapshot, data, preview, {})
        self.assertIn("  crew 101 repair: skip training_in_progress", lines)
        self.assertNotIn("  crew 101 repair: Study Expert Engineering Manual", lines)

    def test_room_names_map_through_the_policys_room_types(self):
        renamed = copy.deepcopy(V2_ROOM_DESIGNS)
        records = renamed["RoomService"]["ListRoomDesigns"]["RoomDesigns"]["RoomDesign"]
        for name, planned in (("Lunar College", True), ("ACADEMY", True), ("Academy", True), ("GYM", False),
                              ("Moon Library", False)):
            with self.subTest(name=name):
                records[1]["@RoomName"] = name
                result = v2_plan(snap([fixer()], training_designs=courses_payload(V2_COURSE), room_designs=renamed))
                self.assertEqual(V2_START in training_ids(result), planned)
                if not planned:
                    self.assertTrue(reasons(result)["training 101"].startswith("not_in_training_room: "))

    def test_earlier_costs_count_under_every_spelling(self):
        from sdk.policy import Plan, get_policy

        policy, snapshot = get_policy("training-v2"), v2_snap()
        for kind, planned in (("minerals", False), ("mineral", False), ("Minerals", False), ("gas", True),
                              ("starbux", True)):
            with self.subTest(kind=kind):
                base = Plan("training-v2", "alpha", "", (ActionRequest("upgrade_room", {}, "r", {kind: 600}),), ())
                preview = policy.crew_plan(snapshot, design(), PolicyConfig(), base)
                self.assertEqual(bool(preview.requests), planned)
                if not planned:
                    self.assertEqual(dict(preview.inputs.funds), {"mineral": 400, "gas": 2000})
                    self.assertIn(Rejection("training 101", "insufficient_funds: course 40001 costs 500 mineral, "
                                                            "400 spendable"), preview.rejected)

    def test_an_unknown_earlier_cost_kind_plans_no_training(self):
        from sdk.policy import Plan, get_policy

        base = Plan("training-v2", "alpha", "", (ActionRequest("upgrade_room", {}, "r", {"crystals": 1}),), ())
        preview = get_policy("training-v2").crew_plan(v2_snap(), design(), PolicyConfig(), base)
        self.assertEqual((preview.plan, preview.inputs, preview.requests, preview.decisions), (None, None, (), ()))
        self.assertEqual(preview.rejected, (Rejection("training", "training not planned: ValueError: unknown cost "
                                                                  "kind 'crystals' in a planned request"),))

    def test_a_partial_crew_section_plans_nothing(self):
        from sdk.policy import get_policy

        partial = v2_snap([fixer(), {"@CharacterName": "No Id"}])
        self.assertEqual(partial.section("crew").value, "partial")
        preview = get_policy("training-v2").crew_plan(partial, design(), PolicyConfig())
        self.assertEqual((preview.plan, preview.inputs, preview.requests), (None, None, ()))
        self.assertEqual(preview.rejected, (Rejection("training", "crew section partial: the roster is unknown"),))
        result = v2_plan(partial)
        self.assertEqual(training_requests(result), [])
        self.assertEqual(reasons(result)["training"], "crew section partial: the roster is unknown")
        self.assertIn(V2_START, training_ids(v2_plan()))  # control

    def test_v1_and_baseline_unchanged(self):
        records = [ended(), crew("102", "Delish", room="12"), CPU]
        with_designs = snap(records, inventory=[item()], training_designs=courses_payload(V2_COURSE),
                            room_designs=V2_ROOM_DESIGNS)
        without = snap(records, inventory=[item()])
        for policy in ("training-v1", "baseline-v1", "fleet-v1"):
            with self.subTest(policy=policy):
                self.assertEqual(plan(with_designs, policy=policy), plan(without, policy=policy))

    def test_a_validation_error_keeps_the_fleet_plan(self):
        from sdk.policy import get_policy

        base = get_policy("fleet-v1").plan(v2_snap(), design(), PolicyConfig())
        with patch("sdk.policy.training.plan_crew", side_effect=ValueError("boom")):
            result = v2_plan()
            preview = get_policy("training-v2").crew_plan(v2_snap(), design(), PolicyConfig())
        self.assertEqual(result.requests, base.requests)
        self.assertIn(Rejection("training", "training not planned: ValueError: boom"), result.rejected)
        self.assertIsNone(preview.plan)
        self.assertEqual((preview.decisions, preview.requests), ((), ()))
        self.assertEqual(preview.rejected, (Rejection("training", "training not planned: ValueError: boom"),))

    def test_unobserved_crew_section_keeps_the_fleet_plan(self):
        result = v2_plan(v2_snap(None))
        self.assertEqual(training_requests(result), [])
        self.assertEqual(reasons(result)["training"], "crew section not observed")

    def test_earlier_gas_spend_counts(self):
        gas_course = {**V2_COURSE, "@CostString": "gas:300"}
        research = design(research_designs=RESEARCH_DESIGNS + [
            {"@ResearchDesignId": "620", "@RootResearchDesignId": "620", "@ResearchName": "Fitness 201",
             "@GasCost": "1800", "@StarbuxCost": "0"}])
        result = v2_plan(v2_snap(course_records=(gas_course,)), research)
        self.assertIn("add_research", [request.action_id for request in result.requests])
        self.assertEqual(reasons(result)["training 101"],
                         "insufficient_funds: course 40001 costs 300 gas, 200 spendable")
        self.assertIn(V2_START, training_ids(v2_plan(v2_snap(course_records=(gas_course,)))))  # control

    def test_preview_is_frozen_and_uses_the_default_crew_policy(self):
        import dataclasses

        from sdk.policy import get_policy
        from sdk.training.strategy import DEFAULT_CREW_POLICY

        policy = get_policy("training-v2")
        self.assertIs(policy.crew_policy, DEFAULT_CREW_POLICY)
        self.assertIs(policy.strategy, DEFAULT_STRATEGY)
        preview = policy.crew_plan(v2_snap(), design(), PolicyConfig())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            preview.plan = None


TARGET_DESIGNS = CHARACTER_DESIGNS + [
    {"@CharacterDesignId": "7101", "@CharacterDesignName": "Galactic Succubus", "@TrainingCapacity": "80"},
    {"@CharacterDesignId": "7102", "@CharacterDesignName": "Delish", "@TrainingCapacity": "80"},
    {"@CharacterDesignId": "x7", "@CharacterDesignName": "Galactic Snow Maiden", "@TrainingCapacity": "80"},
]


def recipes_reply(*rows):
    return {"CharacterService": {"PrestigeCharacterTo": {"Prestiges": {"Prestige": [
        {"@CharacterDesignId1": first, "@CharacterDesignId2": second, "@ToCharacterDesignId": to}
        for first, second, to in rows]}}}}


class CrewPlanLinesTests(unittest.TestCase):
    # Verifies: S14-R3
    def lines(self, snapshot, replies, preview=None, data=None):
        from sdk.policy import get_policy
        from sdk.policy.training import crew_plan_lines

        data = data or design(character_designs=[*TARGET_DESIGNS, {**CHARACTER_DESIGNS[2],
                                                                   "@CharacterDesignName": "Synthetic Cadet"}])
        policy = get_policy("training-v2")
        preview = preview or policy.crew_plan(snapshot, data, PolicyConfig())
        return list(crew_plan_lines("alpha", policy, snapshot, data, preview, replies))

    def test_every_line(self):
        replies = {7101: recipes_reply(("7102", "7003", "7101"), ("7003", "7003", "7101"), ("7003", "7003", "7102")),
                   7102: recipes_reply()}
        self.assertEqual(self.lines(v2_snap(), replies), [
            "Crew plan for account 'alpha' (training-v2, observed 2026-11-10T12:00:00+00:00; spend journaled earlier "
            "this period is not counted)",
            "  facility 11 GYM level 1: status Normal, training 0/2",
            "  facility 12 Academy level 2: status Normal, training 0/2",
            "  research 610: Completed",
            "  crew 101 repair: Study Expert Engineering Manual",
            "  role shields: skip role_uncovered",
            "  role weapons: skip role_uncovered",
            "  prestige Delish: no recipe",
            "  prestige Galactic Succubus <- Synthetic Cadet + Synthetic Cadet: missing 1",
            "  prestige Galactic Succubus <- Synthetic Cadet + Delish: missing 1",
        ])

    def test_unknowns(self):
        unknown = fixer(Weapon=5)
        del unknown["@TrainingEndDate"]
        snapshot = v2_snap([unknown, fixer("102", room="13")], research=[])
        replies = {7101: recipes_reply(("7003", "7003", "7101"), ("7003", "7999", "7101")), 7102: None}
        self.assertEqual(self.lines(snapshot, replies)[1:], [
            "  facility 11 GYM level 1: status Normal, training 0/2",
            "  facility 12 Academy level 2: status Normal, training unknown/2",
            "  research 610: not started",
            "  crew 102 repair: skip not_in_training_room",
            "  role shields: skip role_uncovered",
            "  crew 101 weapons: skip training_in_progress, eligibility unknown",
            "  prestige Delish: recipes unavailable",
            "  prestige Galactic Succubus <- Synthetic Cadet + Synthetic Cadet: missing 1, eligibility unknown for 1",
            "  prestige Galactic Succubus <- Synthetic Cadet + design 7999: missing 1",
        ])
        research_unobserved = self.lines(v2_snap(research=None), {})
        self.assertIn("  research 610: unknown", research_unobserved)
        self.assertIn("  crew 101 repair: skip research_unknown, eligibility unknown", research_unobserved)
        unobserved = self.lines(v2_snap(None), {7101: recipes_reply(("7003", "7003", "7101"))})
        self.assertEqual(unobserved[1:], [
            "  crew: not planned",
            "  prestige Delish: recipes unavailable",
            "  prestige Galactic Succubus <- Synthetic Cadet + Synthetic Cadet: missing unknown",
        ])

    def test_a_partial_roster_is_unknown_to_prestige(self):
        from sdk.policy.training import prestige_roster

        partial = v2_snap([fixer(), {"@CharacterName": "No Id"}])
        self.assertIsNone(prestige_roster(partial))
        self.assertIsNone(prestige_roster(v2_snap(None)))
        self.assertEqual([entry.character_id for entry in prestige_roster(v2_snap())], [101])  # control
        lines = self.lines(partial, {7101: recipes_reply(("7003", "7003", "7101"))})
        self.assertEqual(lines[1:], [
            "  crew: not planned",
            "  prestige Delish: recipes unavailable",
            "  prestige Galactic Succubus <- Synthetic Cadet + Synthetic Cadet: missing unknown",
        ])

    def test_eligibility_unknown_lines_hold_one_skip_code(self):
        import json
        import re

        with open(os.path.join(REPO_ROOT, "tests/fixtures/training/workflow_benchmark.json"), encoding="utf-8") as fh:
            vocabulary = json.load(fh)["vocabulary"]["skip_codes"]
        tired = fixer()
        del tired["@Fatigue"]
        lines = self.lines(v2_snap([tired]), {})
        self.assertIn("  crew 101 repair: skip fatigued, eligibility unknown", lines)
        self.assertIn("  crew 101 repair: skip fatigued", self.lines(v2_snap([fixer(fatigue="100")]), {}))  # known
        line = next(entry for entry in lines if entry.startswith("  crew 101"))
        found = [code for code in vocabulary if re.search(rf"(?<![A-Za-z0-9_]){code}(?![A-Za-z0-9_])", line)]
        self.assertEqual(found, ["fatigued"])
        self.assertNotIn("eligibility", vocabulary)
        self.assertNotIn("unknown", vocabulary)

    def test_facility_and_research_lines_come_from_the_planners_inputs(self):
        import dataclasses

        from sdk.policy import get_policy
        from sdk.training.planner import RoomObservation

        snapshot = v2_snap()
        preview = get_policy("training-v2").crew_plan(snapshot, design(), PolicyConfig())
        rooms = {**preview.inputs.rooms, "12": RoomObservation("12", "Lunar College", "Training", 3, 4, "Upgrading", 1)}
        changed = dataclasses.replace(preview, inputs=dataclasses.replace(preview.inputs, rooms=rooms, research={}))
        lines = self.lines(snapshot, {}, changed)
        self.assertIn("  facility 12 Lunar College level 3: status Upgrading, training 1/4", lines)
        self.assertIn("  research 610: not started", lines)
        self.assertNotIn("  facility 12 Academy level 2: status Normal, training 0/2", lines)
        self.assertIn("  facility 12 Academy level 2: status Normal, training 0/2", self.lines(snapshot, {}, preview))

    def test_header_says_period_spend_is_not_counted(self):
        import json

        header = self.lines(v2_snap(), {})[0]
        self.assertEqual(header, "Crew plan for account 'alpha' (training-v2, observed 2026-11-10T12:00:00+00:00; "
                                 "spend journaled earlier this period is not counted)")
        with open(os.path.join(REPO_ROOT, "tests/fixtures/training/workflow_benchmark.json"), encoding="utf-8") as fh:
            vocabulary = json.load(fh)["vocabulary"]["skip_codes"]
        self.assertEqual([code for code in vocabulary if code in header], [])

    def test_skip_lines_and_design_names(self):
        from sdk.policy.training import character_design_names, prestige_target_ids

        lines = self.lines(v2_snap([fixer(fatigue="100"), fixer("102", Science=4, Repair=0, room="13"),
                                    crew("c3", room="12")]), {})
        self.assertIn("  crew 101 repair: skip fatigued", lines)
        self.assertIn("  crew 102 shields: skip not_in_training_room", lines)
        data = design(character_designs=TARGET_DESIGNS)
        self.assertEqual(character_design_names(data), {7001: "Recruit", 7101: "Galactic Succubus", 7102: "Delish"})
        self.assertEqual(prestige_target_ids(data), (7102, 7101))


class CrewDesignReaderTests(unittest.TestCase):
    # Verifies: S14-R2
    def test_reader_returns_each_payload_only_after_a_good_read(self):
        from sdk.fleet.factory import course_design_reader

        class Reads:
            def __init__(self, training_ok, rooms_ok):
                self.training_ok, self.rooms_ok, self.calls = training_ok, rooms_ok, []
                self.trainingDesigns, self.roomDesigns = "stale training", "stale rooms"

            def listAllTrainingDesigns2(self):
                self.calls.append("training")
                return self.training_ok

            def listRoomDesigns2(self):
                self.calls.append("rooms")
                return self.rooms_ok

        for training_ok, rooms_ok, expected in ((True, True, ("stale training", "stale rooms")),
                                                (False, True, (None, "stale rooms")),
                                                (True, False, ("stale training", None)),
                                                (None, None, (None, None))):
            with self.subTest(training_ok=training_ok, rooms_ok=rooms_ok):
                client = Reads(training_ok, rooms_ok)
                read = course_design_reader(client)
                self.assertEqual(client.calls, [])
                self.assertEqual(read(), expected)
                self.assertEqual(client.calls, ["training", "rooms"])
                read()
                self.assertEqual(len(client.calls), 4)

    def test_only_training_v2_snapshots_get_the_reader(self):
        import tempfile
        import types

        from sdk.fleet.accounts import AccountProfile
        from sdk.fleet.factory import EnvironmentAccountFactory

        class DesignListsClient:
            """Logs in and serves the design lists the factory reads; nothing else."""

            def __init__(self, device=None, settings=None):
                self.calls = []

            def login(self, email=None, password=None):
                self.user = types.SimpleNamespace(id="1001", name=None)
                return True

            def listAllDesigns4(self):
                return False

            def listRoomDesigns2(self):
                self.calls.append("listRoomDesigns2")
                self.roomDesigns = V2_ROOM_DESIGNS
                return True

            def listAllResearchDesigns2(self):
                self.allResearchDesigns = {"ResearchDesigns": {"ResearchDesign": RESEARCH_DESIGNS}}

            def listAllTrainingDesigns2(self):
                self.calls.append("listAllTrainingDesigns2")
                self.trainingDesigns = courses_payload(V2_COURSE)
                return True

            def listAllCharacterDesigns2(self):
                self.allCharacterDesigns = {"CharacterDesigns": {"CharacterDesign": CHARACTER_DESIGNS}}
                return True

        environ = {"BRAVO_EMAIL": "synthetic@example.invalid", "BRAVO_PASSWORD": "synthetic-password-0001"}
        for policy_id, reads in (("training-v2", True), ("training-v1", False), ("fleet-v1", False),
                                 ("baseline-v1", False)):
            with self.subTest(policy_id=policy_id), tempfile.TemporaryDirectory() as tmp, \
                    patch("sdk.fleet.factory.ClientSnapshotProvider") as provider:
                factory = EnvironmentAccountFactory(tmp, client_cls=DesignListsClient, environ=environ)
                context = factory(AccountProfile(alias="bravo", objective="grow", policy_id=policy_id,
                                                 credentials_secret="BRAVO"))
                options = provider.call_args.kwargs
                self.assertEqual("design_reader" in options, reads)
                if reads:
                    del context.client.calls[:]
                    self.assertEqual(options["design_reader"](), (courses_payload(V2_COURSE), V2_ROOM_DESIGNS))
                    self.assertEqual(context.client.calls, ["listAllTrainingDesigns2", "listRoomDesigns2"])


def execute_gate(actions=("add_training", "finish_training", "use_consumable", "upgrade_room",
                          "collect_all_resources", "collect_daily_reward"), mode="full"):
    """An --fleet-execute gate for the fixtures' account "alpha" (an authorization that allows ``actions``)."""
    from sdk.fleet.accounts import registry_from_dict
    from sdk.fleet.authorization import authorization_from_dict
    from sdk.fleet.factory import ExecutionGate

    now = datetime.now(timezone.utc)

    def utc(moment):
        return moment.isoformat().replace("+00:00", "Z")

    authorization = authorization_from_dict({
        "version": 1, "owner": "owner", "purpose": "offline execute test", "accounts": ["alpha"],
        "actions": list(actions), "not_before": utc(now - timedelta(hours=1)), "expires": utc(now + timedelta(days=7)),
        "caps": {"alpha": {"minerals": 1000, "gas": 1000}}, "modes": ["canary", "full"]})
    registry = registry_from_dict({"version": 1, "accounts": [
        {"alias": "alpha", "objective": "grow", "policy_id": "training-v2", "credentials_secret": "ALPHA"}]})
    return ExecutionGate(authorization, registry, mode, lambda: now)


class ExecuteHoldTests(unittest.TestCase):
    # Verifies: S14-R4
    def plan(self, policy_id, gate):
        from sdk.fleet.factory import AuthorizedPolicy
        from sdk.policy import get_policy

        snapshot = v2_snap() if policy_id == "training-v2" else snap([crew("102", "Delish", room="12")])
        return AuthorizedPolicy(get_policy(policy_id), gate).plan(snapshot, design(), PolicyConfig())

    def test_training_v2_add_training_is_held_under_execute(self):
        self.assertIn(V2_START, training_ids(v2_plan()))  # control: the plain policy (dry run) plans it
        result = self.plan("training-v2", execute_gate())
        self.assertEqual(training_requests(result), [])
        self.assertEqual(reasons(result)["add_training 40001 101"],
                         "training_v2_execute_hold: training-v2 starts no paid course under --fleet-execute until "
                         "SPEC-14 step 4")
        self.assertTrue(result.requests)  # the progression block still runs

    def test_hold_comes_before_authorization_and_canary(self):
        for gate in (execute_gate(mode="canary"), execute_gate(actions=["collect_daily_reward"])):
            with self.subTest(mode=gate.canary):
                result = self.plan("training-v2", gate)
                self.assertTrue(reasons(result)["add_training 40001 101"].startswith("training_v2_execute_hold: "))

    def test_training_v1_add_training_unchanged(self):
        result = self.plan("training-v1", execute_gate())
        self.assertEqual([(request.action_id, dict(request.params), dict(request.expected_cost))
                          for request in training_requests(result)],
                         [("add_training", {"training_design_id": "5030", "character_id": "102"},
                           {"minerals": 0, "gas": 0})])

    def test_hold_follows_the_policy_attribute_not_its_name(self):
        from sdk.fleet.factory import AuthorizedPolicy
        from sdk.policy import TrainingV2, get_policy

        class RenamedV2(TrainingV2):
            name = "training-v2-renamed"

        self.assertIs(TrainingV2.holds_paid_training, True)
        self.assertFalse(hasattr(get_policy("training-v1"), "holds_paid_training"))
        result = AuthorizedPolicy(RenamedV2(), execute_gate()).plan(v2_snap(), design(), PolicyConfig())
        self.assertEqual(training_requests(result), [])
        self.assertTrue(reasons(result)["add_training 40001 101"].startswith("training_v2_execute_hold: "))

        class Unheld(TrainingV2):
            holds_paid_training = False

        released = AuthorizedPolicy(Unheld(), execute_gate()).plan(v2_snap(), design(), PolicyConfig())
        self.assertEqual([request.action_id for request in training_requests(released)], ["add_training"])

    def test_hold_code_is_stable(self):
        import json

        from sdk.fleet import factory

        self.assertEqual(factory.TRAINING_V2_EXECUTE_HOLD, "training_v2_execute_hold")
        with open(os.path.join(REPO_ROOT, "tests/fixtures/training/workflow_benchmark.json"), encoding="utf-8") as fh:
            vocabulary = json.load(fh)["vocabulary"]["skip_codes"]
        self.assertNotIn(factory.TRAINING_V2_EXECUTE_HOLD, vocabulary)


if __name__ == "__main__":
    unittest.main()
