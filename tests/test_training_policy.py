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


if __name__ == "__main__":
    unittest.main()
