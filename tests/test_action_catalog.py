"""Tests for sdk.actions catalog and reconciliation. Synthetic snapshots only."""

import inspect
import json
import os
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sdk.actions import (
    CATALOG,
    ActionSpecError,
    Idempotency,
    ReconciliationStatus,
    check_preconditions,
    get_spec,
    reconcile,
)
from sdk.client import Client
from sdk.state import AccountMismatchError, build_snapshot, diff_snapshots

REPO_ROOT = Path(os.path.abspath(__file__)).parents[1]
T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
V, N, C, U = (
    ReconciliationStatus.VERIFIED,
    ReconciliationStatus.NOT_OBSERVED,
    ReconciliationStatus.CONTRADICTED,
    ReconciliationStatus.UNKNOWN,
)

BASE = dict(
    resources={"minerals": "100", "gas": "100", "starbux": "5"},
    crew=[{"@CharacterId": "c1", "@Level": "3", "@Fatigue": "0"}],
    rooms=[{"@RoomId": "r1", "@RoomDesignId": "11", "@RoomStatus": "Normal"}],
    research=[{"@ResearchDesignId": "500", "@ResearchState": "Completed"}],
    inventory=[{"@ItemDesignId": "900", "@Quantity": "1"}],
)


def snap(seconds=0, alias="alpha", **overrides):
    kwargs = dict(BASE)
    kwargs.update(overrides)
    return build_snapshot(alias, T0 + timedelta(seconds=seconds), "synthetic-fixture", **kwargs)


def outcome(action_id, after_overrides, params=None, before_overrides=None, response_ok=True):
    before = snap(**(before_overrides or {}))
    after = snap(seconds=60, **{**(before_overrides or {}), **after_overrides})
    return reconcile(get_spec(action_id), before, after, params, response_ok=response_ok)


class CatalogShapeTests(unittest.TestCase):
    def test_catalog_methods_are_mutating_client_methods_in_the_inventory(self):
        inventory = json.loads((REPO_ROOT / "docs/baseline/capability-inventory.json").read_text(encoding="utf-8"))
        kinds = {entry["name"]: entry["kind"] for entry in inventory["client_methods"]}
        for spec in CATALOG.values():
            with self.subTest(action=spec.action_id):
                self.assertIn(spec.client_method, kinds)
                self.assertEqual(kinds[spec.client_method], "mutating")
                self.assertTrue(spec.mutating)

    def test_representative_actions_are_present(self):
        self.assertEqual(
            set(CATALOG),
            {"collect_all_resources", "collect_daily_reward", "add_training", "upgrade_room", "add_research",
             "run_pvp_battle", "place_room", "upgrade_ship",
             "grab_flying_starbux", "collect_task_reward", "collect_available_markers", "collect_mining_drones",
             "fleet_pool_resources", "fleet_marketplace_sync", "fleet_battle_coordinate", "fleet_share_crew_xp",
             "finish_training", "use_consumable"},
        )
        self.assertIs(CATALOG["add_research"].idempotency, Idempotency.NOT_IDEMPOTENT)
        self.assertIs(CATALOG["collect_all_resources"].idempotency, Idempotency.SAFE_TO_RETRY)

    def test_unknown_action_and_missing_params_are_refused(self):
        with self.assertRaises(ActionSpecError):
            get_spec("sell_everything")
        with self.assertRaises(ActionSpecError):
            check_preconditions(get_spec("upgrade_room"), snap(), {})


class PreconditionTests(unittest.TestCase):
    def test_preconditions_pass_on_a_ready_account(self):
        self.assertEqual(check_preconditions(get_spec("upgrade_room"), snap(), {"room_id": "r1", "upgrade_room_design_id": "12"}), [])
        self.assertEqual(check_preconditions(get_spec("add_research"), snap(), {"research_design_id": "501"}), [])

    def test_preconditions_block_unsafe_or_unobserved_starts(self):
        upgrading = snap(rooms=[{"@RoomId": "r1", "@RoomStatus": "Upgrading"}])
        self.assertIn("room r1 is already upgrading", check_preconditions(get_spec("upgrade_room"), upgrading, {"room_id": "r1", "upgrade_room_design_id": "12"}))
        self.assertIn(
            "research 500 is already completed",
            check_preconditions(get_spec("add_research"), snap(), {"research_design_id": "500"}),
        )
        self.assertEqual(
            check_preconditions(get_spec("add_training"), snap(), {"training_design_id": "1", "character_id": "c9"}),
            ["crew member c9 is not in the snapshot"],
        )
        self.assertIn(
            "resources section was not observed before the action",
            check_preconditions(get_spec("collect_all_resources"), snap(resources=None)),
        )


class ReconcileTests(unittest.TestCase):
    def test_collect_all_resources_outcomes(self):
        self.assertIs(outcome("collect_all_resources", {"resources": {"minerals": "150", "gas": "100", "starbux": "5"}}).status, V)
        self.assertIs(outcome("collect_all_resources", {}).status, N)
        self.assertIs(outcome("collect_all_resources", {"resources": {"minerals": "90", "gas": "100", "starbux": "5"}}).status, C)
        self.assertIs(outcome("collect_all_resources", {"resources": None}).status, U)

    def test_collect_daily_reward_outcomes(self):
        self.assertIs(outcome("collect_daily_reward", {"resources": {"minerals": "100", "gas": "100", "starbux": "6"}}).status, V)
        self.assertIs(outcome("collect_daily_reward", {"inventory": [{"@ItemDesignId": "900", "@Quantity": "2"}]}).status, V)
        self.assertIs(outcome("collect_daily_reward", {}).status, N)
        self.assertIs(outcome("collect_daily_reward", {"inventory": None}).status, U)

    def test_upgrade_room_outcomes(self):
        params = {"room_id": "r1", "upgrade_room_design_id": "12"}
        self.assertIs(outcome("upgrade_room", {"rooms": [{"@RoomId": "r1", "@RoomDesignId": "11", "@RoomStatus": "Upgrading"}]}, params).status, V)
        self.assertIs(outcome("upgrade_room", {"rooms": [{"@RoomId": "r1", "@RoomDesignId": "12", "@RoomStatus": "Normal"}]}, params).status, V)
        self.assertIs(outcome("upgrade_room", {}, params).status, N)
        self.assertIs(outcome("upgrade_room", {"rooms": [{"@RoomId": "r2"}]}, params).status, C)
        self.assertIs(outcome("upgrade_room", {"rooms": None}, params).status, U)

    def test_add_research_outcomes(self):
        params = {"research_design_id": "501"}
        started = [BASE["research"][0], {"@ResearchDesignId": "501", "@ResearchState": "Researching"}]
        self.assertIs(outcome("add_research", {"research": started}, params).status, V)
        self.assertIs(outcome("add_research", {}, params).status, N)
        self.assertIs(
            outcome("add_research", {"research": BASE["research"]}, params, before_overrides={"research": started}).status, C
        )
        self.assertIs(outcome("add_research", {"research": None}, params).status, U)

    def test_http_success_alone_is_never_verified(self):
        for action_id, params in (
            ("collect_all_resources", None),
            ("collect_daily_reward", None),
            ("add_training", {"training_design_id": "1", "character_id": "c1"}),
            ("finish_training", {"character_id": "c1"}),
            ("use_consumable", {"item_design_id": "900", "character_id": "c1"}),
            ("upgrade_room", {"room_id": "r1", "upgrade_room_design_id": "12"}),
            ("add_research", {"research_design_id": "501"}),
        ):
            with self.subTest(action=action_id):
                result = outcome(action_id, {}, params, response_ok=True)
                self.assertIsNot(result.status, V)
                self.assertTrue(result.response_ok)

    def test_cross_account_reconciliation_is_refused(self):
        with self.assertRaises(AccountMismatchError):
            reconcile(get_spec("collect_all_resources"), snap(), snap(seconds=1, alias="bravo"))


# --- training and consumables ---------------------------------------------------------

# The nine stat improvement attributes of a character record, spelled out literally; distinct powers of two (total
# 511) so a dropped stat changes every sum.
STATS = {
    "@HpImprovement": "1",
    "@PilotImprovement": "2",
    "@RepairImprovement": "4",
    "@WeaponImprovement": "8",
    "@ScienceImprovement": "16",
    "@EngineImprovement": "32",
    "@AttackImprovement": "64",
    "@AbilityImprovement": "128",
    "@StaminaImprovement": "256",
}
DUE = "2026-11-10T11:00:00"  # an hour before T0, the before-snapshot's observation time
AT_T0 = "2026-11-10T12:00:00"  # exactly T0
BETWEEN_OBSERVATIONS = "2026-11-10T12:00:30"  # after T0, before the after-snapshot's observation (T0 + 60s)
E1, E2 = "2026-11-10T13:00:00", "2026-11-10T14:00:00"


def member(end=DUE, design="7", stats=STATS, **extra):
    record = {"@CharacterId": "c1", "@Level": "3", "@Fatigue": "0", "@TrainingEndDate": end,
              "@TrainingDesignId": design}
    return {"crew": [{**record, **stats, **extra}]}


def cleared(stats=STATS, **extra):
    return member(end="", design="0", stats=stats, **extra)


def raised(attribute, by=1):
    return {**STATS, attribute: str(int(STATS[attribute]) + by)}


SPENT = {"resources": {"minerals": "100", "gas": "100", "starbux": "4"}}  # BASE holds 5 starbux
OTHER_MEMBER = {"crew": [{"@CharacterId": "c2", "@Level": "3", "@TrainingEndDate": DUE, "@TrainingDesignId": "7",
                          **STATS}]}


class FinishTrainingTests(unittest.TestCase):
    P = {"character_id": "c1"}

    def reasons(self, before):
        return check_preconditions(get_spec("finish_training"), before, self.P)

    def test_spec_shape(self):
        spec = get_spec("finish_training")
        self.assertEqual((spec.client_method, spec.idempotency, spec.required_params, spec.mutating, spec.canary_safe),
                         ("finishTraining", Idempotency.SAFE_TO_RETRY, ("character_id",), True, False))

    def test_client_signature_matches_required_params(self):
        self.assertEqual(list(inspect.signature(Client.finishTraining).parameters)[1:], ["characterId"])

    def test_due_training_passes(self):
        self.assertEqual(self.reasons(snap(**member())), [])

    # The grace is pinned at 60 seconds by the two boundary tests below: an end exactly 60 seconds before the
    # observation passes and one 59 seconds before is refused.
    def test_end_a_full_grace_before_the_observation_passes(self):
        self.assertEqual(self.reasons(snap(**member(end="2026-11-10T11:59:00"))), [])

    def test_end_within_the_grace_or_later_is_refused(self):
        # The local clock may run ahead of the server's: an end less than the grace before the observation may not
        # have passed on the server yet, and finishing it then could cost starbux.
        for end in ("2026-11-10T11:59:01", "2026-11-10T12:00:00", "2026-11-10T12:00:01"):
            with self.subTest(end=end):
                self.assertEqual(self.reasons(snap(**member(end=end))), [
                    f"c1's training ends at {end}, less than 60 seconds before the observation;"
                    " finishing now could cost starbux"
                ])

    def test_offset_observation_time_is_compared_in_utc(self):
        observed_at = T0.astimezone(timezone(timedelta(hours=-5)))

        def before(end):
            return build_snapshot("alpha", observed_at, "synthetic-fixture", crew=member(end=end)["crew"])

        self.assertEqual(self.reasons(before("2026-11-10T11:59:00")), [])
        self.assertEqual(self.reasons(before("2026-11-10T11:59:01")), [
            "c1's training ends at 2026-11-10T11:59:01, less than 60 seconds before the observation;"
            " finishing now could cost starbux"
        ])

    def test_a_placeholder_end_without_a_design_is_refused(self):
        # An idle member may carry a placeholder end in the past; with no training design there is nothing to finish.
        for design in ("0", ""):
            with self.subTest(design=design):
                self.assertEqual(self.reasons(snap(**member(end="0001-01-01T00:00:00", design=design))),
                                 ["c1 has no reported training design, so there is no training to finish"])

    def test_idle_unobserved_or_unreadable_training_is_refused(self):
        unobserved = {"crew": [{"@CharacterId": "c1", "@Level": "3", **STATS}]}
        for name, overrides, reason in (
            ("idle", member(end=""), "c1 is not training"),
            ("unobserved", unobserved, "training state of c1 was not observed before the action"),
            ("unreadable", member(end="soon"), "training end of c1 is not a readable server time"),
        ):
            with self.subTest(case=name):
                self.assertEqual(self.reasons(snap(**overrides)), [reason])

    def test_out_of_range_training_end_is_unreadable_not_raised(self):
        # Converting these offset times to UTC overflows datetime's range.
        for end in ("0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"):
            with self.subTest(end=end):
                self.assertEqual(self.reasons(snap(**member(end=end))),
                                 ["training end of c1 is not a readable server time"])

    def test_naive_observation_time_is_refused_not_raised(self):
        before = replace(snap(**member()), observed_at=T0.replace(tzinfo=None))
        self.assertEqual(self.reasons(before), ["observation time of the before-snapshot is not timezone-aware"])

    def test_design_cleared_with_the_end_kept_counts_as_cleared(self):
        # If FinishTraining resets @TrainingDesignId to 0 but keeps the past @TrainingEndDate, that is the same
        # training cleared, not a different training.
        kept_end = dict(end=DUE, design="0")
        cases = {
            "with a stat gain": (member(**kept_end, stats=raised("@HpImprovement", 3)), V),
            "with no gain, capacity unknown": (member(**kept_end), U),
        }
        for name, (after, expected) in cases.items():
            with self.subTest(case=name):
                result = outcome("finish_training", after, self.P, before_overrides=member())
                self.assertIs(result.status, expected, result.reason)

    def test_absent_member_or_crew_reports_once_and_never_raises(self):
        spec = get_spec("finish_training")
        self.assertEqual(check_preconditions(spec, snap(**member()), {"character_id": "c9"}),
                         ["crew member c9 is not in the snapshot"])
        self.assertEqual(self.reasons(snap(crew=None)),
                         ["crew section was not observed before the action", "crew member c1 is not in the snapshot"])

    def test_outcomes(self):
        missing_stamina = {key: str(int(value) + 1) for key, value in STATS.items() if key != "@StaminaImprovement"}
        unobserved_training = {"crew": [{"@CharacterId": "c1", "@Level": "3", **raised("@HpImprovement")}]}
        capacity_511, capacity_600 = {"@TrainingCapacity": "511"}, {"@TrainingCapacity": "600"}
        cases = {
            "cleared with a stat gain": (member(), cleared(raised("@HpImprovement", 3)), V),
            "cleared, a stat unobserved after": (member(), cleared(missing_stamina), U),
            "cleared, stats unobserved before": (member(stats={}), cleared(raised("@HpImprovement")), U),
            "cleared with no gain, capacity unknown": (member(), cleared(), U),
            "cleared with no gain at capacity": (member(**capacity_511), cleared(**capacity_511), V),
            "cleared with no gain below capacity": (member(**capacity_600), cleared(**capacity_600), U),
            "cleared but stats fell": (member(), cleared({**STATS, "@HpImprovement": "0"}), C),
            "still training and stats fell": (member(), member(stats={**STATS, "@HpImprovement": "0"}), C),
            # The server may leave an ended training in place, rewrite its end to the finish time, or set a
            # placeholder: what counts is that nothing runs after and the stats rose.
            "ended training left in place, stats rose": (member(), member(stats=raised("@HpImprovement")), V),
            "end rewritten to the finish time, stats rose": (
                member(), member(end="2026-11-10T12:00:30", stats=raised("@HpImprovement")), V),
            "placeholder end after, stats rose": (
                member(), member(end="0001-01-01T00:00:00", design="0", stats=raised("@HpImprovement")), V),
            "ended training left in place, nothing changed": (member(), member(), N),
            # A due finish never spends starbux.
            "starbux spent": (member(), {**cleared(raised("@HpImprovement")), **SPENT}, C),
            "starbux spent and nothing else changed": (member(), {**member(), **SPENT}, C),
            "starbux unobserved after, stats rose": (
                member(), {**cleared(raised("@HpImprovement")), "resources": None}, V),
            "a running training after with no design": (member(), member(end=E1, design="0"), C),
            "training end after the call unreadable": (
                member(), member(end="soon", stats=raised("@HpImprovement")), U),
            "training still running before the call": (
                member(end="2026-11-10T12:00:30"), cleared(raised("@HpImprovement")), U),
            "training end before the call unreadable": (member(end="soon"), cleared(raised("@HpImprovement")), U),
            "placeholder end before and after": (
                member(end="0001-01-01T00:00:00", design="0"), member(end="0001-01-01T00:00:00", design="0"), N),
            "stats fell and training unobserved after": (
                member(), {"crew": [{"@CharacterId": "c1", "@Level": "3", **STATS, "@HpImprovement": "0"}]}, C),
            "member absent before, cleared with a gain": (OTHER_MEMBER, cleared(raised("@HpImprovement")), U),
            "member absent before, unchanged after": (OTHER_MEMBER, member(), U),
            "stats unobserved before, capacity shown": (
                member(stats={}, **capacity_600), cleared(**capacity_600), U),
            "at capacity, capacity shown only before the call": (member(**capacity_511), cleared(), V),
            # Row 8: the same training still running before and after is not this call's, and not a contradiction.
            "the same training running before and after": (
                member(end="2026-11-10T20:00:00"), member(end="2026-11-10T20:00:00"), N),
            "another training after": (member(), member(end="2026-11-10T20:00:00", design="8"), C),
            "same design, new end": (member(), member(end="2026-11-10T20:00:00"), C),
            "member disappeared": (member(), {"crew": [{"@CharacterId": "c2"}]}, C),
            "crew unobserved after": (member(), {"crew": None}, U),
            "training unobserved after": (member(), unobserved_training, U),
            "not training before or after": (member(end="", design="0"), member(end="", design="0"), N),
            "not training before, training after": (member(end="", design="0"), member(end=E1, design="8"), C),
        }
        for name, (before, after, expected) in cases.items():
            with self.subTest(case=name):
                result = outcome("finish_training", after, self.P, before_overrides=before)
                self.assertIs(result.status, expected, result.reason)

    def test_each_stat_counts_toward_the_gain(self):
        for attribute in ("@HpImprovement", "@PilotImprovement", "@RepairImprovement", "@WeaponImprovement",
                          "@ScienceImprovement", "@EngineImprovement", "@AttackImprovement", "@AbilityImprovement",
                          "@StaminaImprovement"):
            with self.subTest(attribute=attribute):
                result = outcome("finish_training", cleared(raised(attribute)), self.P, before_overrides=member())
                self.assertIs(result.status, V, result.reason)


class UseConsumableTests(unittest.TestCase):
    P = {"item_design_id": "900", "character_id": "c1"}

    def reasons(self, before, params=None):
        return check_preconditions(get_spec("use_consumable"), before, params or self.P)

    def test_spec_shape(self):
        spec = get_spec("use_consumable")
        self.assertEqual((spec.client_method, spec.idempotency, spec.required_params, spec.mutating, spec.canary_safe),
                         ("useConsumable", Idempotency.NOT_IDEMPOTENT, ("item_design_id", "character_id"), True, False))

    def test_client_signature_matches_required_params(self):
        signature = list(inspect.signature(Client.useConsumable).parameters)[1:]
        self.assertEqual(signature, ["consumableItemDesignId", "characterId"])
        client_names = {"item_design_id": "consumableItemDesignId", "character_id": "characterId"}
        self.assertEqual([client_names[name] for name in get_spec("use_consumable").required_params], signature)

    def test_preconditions(self):
        self.assertEqual(self.reasons(snap()), [])  # BASE holds item 900 at quantity 1, the boundary
        self.assertEqual(self.reasons(snap(), {**self.P, "item_design_id": 900}), [])
        self.assertEqual(self.reasons(snap(), {**self.P, "item_design_id": "901"}),
                         ["item 901 is not in the inventory"])
        refused = {
            "quantity 0": (snap(inventory=[{"@ItemDesignId": "900", "@Quantity": "0"}]),
                           ["item 900 has quantity 0"]),
            "quantity unreadable": (snap(inventory=[{"@ItemDesignId": "900", "@Quantity": "x"}]),
                                    ["quantity of item 900 was not observed"]),
            "inventory unobserved": (snap(inventory=None), ["inventory section was not observed before the action"]),
            "crew unobserved": (snap(crew=None),
                                ["crew section was not observed before the action",
                                 "crew member c1 is not in the snapshot"]),
        }
        for name, (before, expected) in refused.items():
            with self.subTest(case=name):
                self.assertEqual(self.reasons(before), expected)
        self.assertEqual(self.reasons(snap(), {**self.P, "character_id": "c9"}),
                         ["crew member c9 is not in the snapshot"])
        # A value that is not a design id also matches no item, so the inventory check reports it as well.
        for value, item in (("gas", "gas"), (" ", ""), (True, "True"), (1.5, "1.5")):
            with self.subTest(item_design_id=value):
                self.assertEqual(self.reasons(snap(), {**self.P, "item_design_id": value}),
                                 ["item_design_id must be a numeric design id", f"item {item} is not in the inventory"])
        with self.assertRaises(ActionSpecError):
            self.reasons(snap(), {**self.P, "item_design_id": ""})

    def test_unobserved_sections_are_reported_exactly_once(self):
        self.assertEqual(self.reasons(snap(inventory=None)), ["inventory section was not observed before the action"])
        self.assertEqual(self.reasons(snap(crew=None)),
                         ["crew section was not observed before the action", "crew member c1 is not in the snapshot"])

    def test_padded_or_non_ascii_design_id_is_refused(self):
        # The client gets the raw value, so a value that only passes once stripped, or a non-ASCII digit, would
        # pass the checks and then match no item.
        # A padded id names an item that is held once stripped, so the inventory check stays quiet and the refusal is
        # reported exactly once; any other bad value also matches no item and is reported by both checks.
        for value in (" 900", "900 ", "900\n"):
            with self.subTest(item_design_id=value):
                self.assertEqual(self.reasons(snap(), {**self.P, "item_design_id": value}),
                                 ["item_design_id must be a numeric design id"])
        for value in ("٩٠٠", "²", -900, "0", 0, "00"):
            with self.subTest(item_design_id=value):
                self.assertEqual(self.reasons(snap(), {**self.P, "item_design_id": value}),
                                 ["item_design_id must be a numeric design id", f"item {value} is not in the inventory"])

    def test_leading_zero_design_id_is_refused(self):
        # The server reports "900", never "0900": a zero-padded id would be sent as is and matches no held item.
        for value in ("007", "0900"):
            with self.subTest(item_design_id=value):
                self.assertEqual(self.reasons(snap(), {**self.P, "item_design_id": value}),
                                 ["item_design_id must be a numeric design id", f"item {value} is not in the inventory"])

    def test_design_held_in_several_stacks(self):
        stacks = {"inventory": [{"@ItemDesignId": "900", "@Quantity": "5"}, {"@ItemDesignId": "900", "@Quantity": "3"}]}
        self.assertEqual(self.reasons(snap(**stacks)),
                         ["item 900 is held in 2 inventory records, so its use cannot be reconciled"])
        # useConsumable uses the first stack; the per-design delta sees only the last, so this must not read as
        # not-observed, which would let a not-idempotent use repeat.
        first_used = {"inventory": [{"@ItemDesignId": "900", "@Quantity": "4"}, {"@ItemDesignId": "900", "@Quantity": "3"}]}
        result = outcome("use_consumable", first_used, self.P, before_overrides=stacks)
        self.assertIs(result.status, U, result.reason)
        merged = {"inventory": [{"@ItemDesignId": "900", "@Quantity": "7"}]}
        result = outcome("use_consumable", merged, self.P, before_overrides=stacks)
        self.assertIs(result.status, U, result.reason)

    def test_outcomes(self):
        three = {"inventory": [{"@ItemDesignId": "900", "@Quantity": "3"}]}
        cases = {
            "the last item is gone": ({}, {"inventory": []}, V),
            "the quantity reads 0": ({}, {"inventory": [{"@ItemDesignId": "900", "@Quantity": "0"}]}, V),
            "3 fell to 2": (three, {"inventory": [{"@ItemDesignId": "900", "@Quantity": "2"}]}, V),
            "unchanged": ({}, {}, N),
            # An item arriving concurrently must not halt the account, and one use takes exactly one item.
            "1 rose to 2": ({}, {"inventory": [{"@ItemDesignId": "900", "@Quantity": "2"}]}, U),
            "3 fell to 1": (three, {"inventory": [{"@ItemDesignId": "900", "@Quantity": "1"}]}, U),
            "3 all gone": (three, {"inventory": []}, U),
            "partial inventory before only, item used": (
                {"inventory": [{"@ItemDesignId": "900", "@Quantity": "1"}, {"@Quantity": "5"}]}, {"inventory": []}, V),
            "member vanished": ({}, {"inventory": [], "crew": [{"@CharacterId": "c2"}]}, C),
            "inventory unobserved after": ({}, {"inventory": None}, U),
            "quantity unreadable after": ({}, {"inventory": [{"@ItemDesignId": "900", "@Quantity": "x"}]}, U),
            "item absent from a partial inventory": ({}, {"inventory": [{"@Quantity": "5"}]}, U),
            # The per-design delta sees only the last record (5 -> 1 reads as 2 -> 1): several records after the call
            # alone must not read as one use.
            "item held in several records after only": (
                {"inventory": [{"@ItemDesignId": "900", "@Quantity": "2"}]},
                {"inventory": [{"@ItemDesignId": "900", "@Quantity": "5"}, {"@ItemDesignId": "900", "@Quantity": "1"}]},
                U,
            ),
            "crew unobserved after, item used": ({}, {"inventory": [], "crew": None}, V),
            "only fatigue fell": (
                {"crew": [{"@CharacterId": "c1", "@Level": "3", "@Fatigue": "50"}]},
                {"crew": [{"@CharacterId": "c1", "@Level": "3", "@Fatigue": "0"}]},
                N,
            ),
            "another item was used": (
                {"inventory": [{"@ItemDesignId": "900", "@Quantity": "1"}, {"@ItemDesignId": "901", "@Quantity": "2"}]},
                {"inventory": [{"@ItemDesignId": "900", "@Quantity": "1"}, {"@ItemDesignId": "901", "@Quantity": "1"}]},
                N,
            ),
        }
        for name, (before, after, expected) in cases.items():
            with self.subTest(case=name):
                result = outcome("use_consumable", after, self.P, before_overrides=before)
                self.assertIs(result.status, expected, result.reason)

    def test_reasons_name_the_change(self):
        three = {"inventory": [{"@ItemDesignId": "900", "@Quantity": "3"}]}
        result = outcome("use_consumable", {"inventory": [{"@ItemDesignId": "900", "@Quantity": "1"}]}, self.P,
                         before_overrides=three)
        self.assertIn("fell by 2, more than one use", result.reason)
        result = outcome("use_consumable", {"inventory": [{"@ItemDesignId": "900", "@Quantity": "2"}]}, self.P)
        self.assertIn("rose by 1", result.reason)

    def test_member_stat_and_fatigue_change_are_reported_but_never_decide(self):
        tired = {"crew": [{"@CharacterId": "c1", "@Level": "3", "@Fatigue": "50", **STATS}]}
        rested = {"crew": [{"@CharacterId": "c1", "@Level": "3", "@Fatigue": "0", **raised("@HpImprovement")}]}
        used = outcome("use_consumable", {**rested, "inventory": []}, self.P, before_overrides=tired)
        self.assertIs(used.status, V, used.reason)
        self.assertIn("c1 stat improvements +1, fatigue -50", used.reason)
        unused = outcome("use_consumable", rested, self.P, before_overrides=tired)
        self.assertIs(unused.status, N, unused.reason)
        self.assertIn("c1 stat improvements +1, fatigue -50", unused.reason)
        # BASE carries no stats: only the fatigue change is named.
        plain = outcome("use_consumable", {"inventory": []}, self.P)
        self.assertIn("c1 fatigue +0", plain.reason)
        self.assertNotIn("stat improvements", plain.reason)
        self.assertNotIn("c1", outcome("use_consumable", {"inventory": [], "crew": None}, self.P).reason)

    def test_stat_change_is_named_only_when_stats_are_observed_on_both_sides(self):
        no_stats = {"crew": [{"@CharacterId": "c1", "@Level": "3", "@Fatigue": "0"}]}
        stats = {"crew": [{"@CharacterId": "c1", "@Level": "3", "@Fatigue": "0", **STATS}]}
        for name, (before, after) in {"stats only after": (no_stats, stats), "stats only before": (stats, no_stats)}.items():
            with self.subTest(case=name):
                used = outcome("use_consumable", {**after, "inventory": []}, self.P, before_overrides=before)
                self.assertIs(used.status, V, used.reason)
                self.assertEqual(used.reason, "item 900 quantity fell by 1; c1 fatigue +0")

    def test_nothing_observed_about_the_member_adds_no_note(self):
        bare = {"crew": [{"@CharacterId": "c1", "@Level": "3"}]}  # no stats and no fatigue on either side
        used = outcome("use_consumable", {**bare, "inventory": []}, self.P, before_overrides=bare)
        self.assertIs(used.status, V, used.reason)
        self.assertEqual(used.reason, "item 900 quantity fell by 1")

    def test_padded_design_is_compared_stripped(self):
        # reconcile is public and does not run the preconditions; like add_training, it compares the id stripped.
        used = outcome("use_consumable", {"inventory": []}, {**self.P, "item_design_id": " 900"})
        self.assertIs(used.status, V, used.reason)
        self.assertEqual(used.reason, "item 900 quantity fell by 1; c1 fatigue +0")


class AddTrainingTests(unittest.TestCase):
    P = {"training_design_id": "7", "character_id": "c1"}
    IDLE = {"crew": [{"@CharacterId": "c1", "@TrainingEndDate": "", "@TrainingDesignId": "0"}]}

    @staticmethod
    def training(design, end=E1):
        return {"crew": [{"@CharacterId": "c1", "@TrainingEndDate": end, "@TrainingDesignId": design}]}

    def test_spec_shape(self):
        spec = get_spec("add_training")
        self.assertEqual((spec.client_method, spec.idempotency, spec.required_params),
                         ("addTraining", Idempotency.NOT_IDEMPOTENT, ("training_design_id", "character_id")))
        signature = list(inspect.signature(Client.addTraining).parameters)[1:]
        self.assertEqual(signature, ["trainingDesignId", "characterId"])

    def test_non_numeric_design_id_is_refused(self):
        spec, idle = get_spec("add_training"), snap(**self.IDLE)
        self.assertEqual(check_preconditions(spec, idle, self.P), [])
        self.assertEqual(check_preconditions(spec, idle, {**self.P, "training_design_id": 7}), [])
        for value in ("gas", " ", True, " 7", "7 ", "²", 7.0, "0", 0, "000"):
            with self.subTest(training_design_id=value):
                self.assertEqual(check_preconditions(spec, idle, {**self.P, "training_design_id": value}),
                                 ["training_design_id must be a numeric design id"])

    def test_leading_zero_design_id_is_refused(self):
        # The server reports "7", never "007": a zero-padded id would be sent as is and then compared with the
        # reported design, so a correct start would read as another design.
        spec, idle = get_spec("add_training"), snap(**self.IDLE)
        for value in ("007", "0900", "07"):
            with self.subTest(training_design_id=value):
                self.assertEqual(check_preconditions(spec, idle, {**self.P, "training_design_id": value}),
                                 ["training_design_id must be a numeric design id"])

    def test_preconditions_require_an_idle_member(self):
        spec = get_spec("add_training")
        placeholder = {"crew": [{"@CharacterId": "c1", "@TrainingEndDate": "0001-01-01T00:00:00",
                                 "@TrainingDesignId": "0"}]}
        # A training ending exactly at the observation is over, not running: with no design it is a placeholder.
        placeholder_at_observation = self.training("0", AT_T0)
        cases = {
            "idle": (self.IDLE, []),
            "idle with a placeholder end": (placeholder, []),
            "idle with a placeholder end exactly at the observation": (placeholder_at_observation, []),
            # A zero design written with more digits is still no design, so a past placeholder end is idle.
            "idle with a placeholder end and design 00": (self.training("00", "0001-01-01T00:00:00"), []),
            "training unobserved": ({}, ["training state of c1 was not observed before the action"]),
            "a training running": (self.training("7"), [f"c1 is already training 7 until {E1}"]),
            "a running end with no design": (
                self.training("0"), [f"c1 is already training (design not reported) until {E1}"]),
            "an ended training not collected": (
                self.training("7", DUE), [f"c1 has an ended training 7 (ended {DUE}) that was not collected"]),
            "an unreadable end": (self.training("0", "soon"), ["training end of c1 is not a readable server time"]),
        }
        for name, (before, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(check_preconditions(spec, snap(**before), self.P), expected)
        self.assertEqual(check_preconditions(spec, snap(**self.IDLE), {**self.P, "character_id": "c9"}),
                         ["crew member c9 is not in the snapshot"])
        self.assertEqual(check_preconditions(spec, snap(crew=None), self.P),
                         ["crew section was not observed before the action", "crew member c1 is not in the snapshot"])

    def test_naive_observation_time_is_refused_not_raised(self):
        # A naive observation time cannot be compared with a server end time; the member is refused, never raised on.
        spec = get_spec("add_training")
        cases = {
            "a training running": self.training("7"),
            "a placeholder end in the past": self.training("0", "0001-01-01T00:00:00"),
        }
        for name, before in cases.items():
            with self.subTest(case=name):
                naive = replace(snap(**before), observed_at=T0.replace(tzinfo=None))
                self.assertEqual(check_preconditions(spec, naive, self.P),
                                 ["training end of c1 is not a readable server time"])

    def test_outcomes(self):
        training, idle = self.training, self.IDLE
        cases = {
            "idle starts the requested training": (idle, training("7"), V),
            "idle starts a training whose design is not reported": (idle, training(""), U),
            "idle stays idle": (idle, idle, N),
            # Row 5 before row 6: an unchanged placeholder end in the past is not this call's, not an unknown start.
            "placeholder idle stays unchanged": (
                training("0", "0001-01-01T00:00:00"), training("0", "0001-01-01T00:00:00"), N),
            # Row 5 compares the design too: a design appearing on the same past end is a change, judged by row 6.
            "a design appears on an unchanged placeholder end": (
                training("0", "0001-01-01T00:00:00"), training("7", "0001-01-01T00:00:00"), U),
            "idle starts another training": (idle, training("8"), C),
            "member vanished": (idle, {"crew": [{"@CharacterId": "c2"}]}, C),
            "training fields absent": ({}, {"crew": [{"@CharacterId": "c1", "@Level": "4"}]}, U),
            "crew unobserved after": (idle, {"crew": None}, U),
            # The regression: a member already training the requested design before the call is not this call's.
            "already training the requested design": (training("7"), training("7"), N),
            "the requested design restarted with a new end": (training("7", E1), training("7", E2), V),
            "already training another design": (training("8"), training("8"), N),
            "another design replaced by the requested one": (training("8"), training("7", E2), V),
            "training stopped": (training("7"), idle, N),
            "the requested design replaced by another": (training("7"), training("8", E2), C),
            # A training is only new if it ends after the before-observation; one already over is not this call's.
            "a 'new' training already over": (idle, training("7", DUE), U),
            "a 'new' training ending exactly at the before-observation": (idle, training("7", AT_T0), U),
            # Judged at the before-observation, not the after one: a training that ends between the two snapshots
            # was still running when the call was made.
            "a new training ending between the two observations": (idle, training("7", BETWEEN_OBSERVATIONS), V),
            "a new training with an unreadable end": (idle, training("7", "soon"), U),
            "member absent before": ({"crew": [{"@CharacterId": "c2", "@TrainingEndDate": ""}]}, training("7"), U),
        }
        for name, (before, after, expected) in cases.items():
            with self.subTest(case=name):
                result = outcome("add_training", after, self.P, before_overrides=before)
                self.assertIs(result.status, expected, result.reason)

    def test_padded_requested_design_is_compared_stripped(self):
        result = outcome("add_training", self.training("7"), {**self.P, "training_design_id": " 7"},
                         before_overrides=self.IDLE)
        self.assertIs(result.status, V, result.reason)


NEW_ROOM = {"@RoomId": "room-instance-9911", "@RoomDesignId": "42", "@RoomStatus": "Building", "@Row": "1",
            "@Column": "1"}
PLACE = {"room_design_id": "42", "row": 1, "column": 1}


class PlaceRoomTests(unittest.TestCase):
    """``place_room`` (SPEC-13): reconciled from the ship's rooms alone, never from the response."""

    # Verifies: S13-R4
    def test_spec_shape(self):
        spec = get_spec("place_room")
        self.assertEqual((spec.client_method, spec.idempotency, spec.required_params, spec.mutating),
                         ("placeRoom", Idempotency.NOT_IDEMPOTENT, ("room_design_id", "row", "column"), True))

    # Verifies: S13-R4
    def test_bad_parameters_are_refused_before_the_call(self):
        spec = get_spec("place_room")
        for params in ({**PLACE, "row": -1}, {**PLACE, "column": True}, {**PLACE, "row": "1"},
                       {**PLACE, "room_design_id": "gas"}, {**PLACE, "room_design_id": " "}):
            with self.subTest(params=params):
                self.assertTrue(check_preconditions(spec, snap(), params))
        with self.assertRaises(ActionSpecError):  # an empty id is a missing parameter
            check_preconditions(spec, snap(), {**PLACE, "room_design_id": ""})
        self.assertEqual(check_preconditions(spec, snap(), PLACE), [])
        self.assertTrue(check_preconditions(spec, snap(rooms=None), PLACE))

    # Verifies: S13-R5
    def test_outcomes(self):
        rooms = BASE["rooms"]
        cases = {
            "the room appears where asked": ({"rooms": rooms + [NEW_ROOM]}, V),
            "no new room": ({}, N),
            "a new room elsewhere": ({"rooms": rooms + [{**NEW_ROOM, "@Column": "2"}]}, N),
            "a new room of another design": ({"rooms": rooms + [{**NEW_ROOM, "@RoomDesignId": "26"}]}, N),
            "a new room whose position is not observed": ({"rooms": rooms + [
                {key: value for key, value in NEW_ROOM.items() if key != "@Row"}]}, N),
            "a room that was there is gone": ({"rooms": [NEW_ROOM]}, C),
            "an existing room finished upgrading": ({"rooms": [{**rooms[0], "@RoomDesignId": "12"}, NEW_ROOM]}, V),
            "rooms not observed after": ({"rooms": None}, U),
        }
        for name, (after, expected) in cases.items():
            with self.subTest(case=name):
                result = outcome("place_room", after, PLACE)
                self.assertIs(result.status, expected, result.reason)
                self.assertNotIn("room-instance-9911", result.reason)

    # Verifies: S13-R5
    def test_a_room_already_at_that_position_before_the_call_is_not_this_placement(self):
        result = outcome("place_room", {}, PLACE, before_overrides={"rooms": BASE["rooms"] + [NEW_ROOM]})
        self.assertIs(result.status, N)


class PlacementEvidenceTests(unittest.TestCase):
    """What the journal keeps of a placement's server reply (SPEC-13): scanned text, never the outcome."""

    def setUp(self):
        spec = get_spec("place_room")
        self.probe, self.evidence = spec.evidence_probe, spec.evidence

    def reply(self, **fields):
        base = {"http_status": 200, "success_shape": False, "parsed": True, "length": 60,
                "elements": ["RoomService", "BuyRoom2"], "messages": [{"attribute": "errorMessage",
                                                                       "text": "Cell is occupied."}]}
        base.update(fields)
        return base

    # Verifies: S13-R5
    def test_the_reply_is_journaled_after_scanning(self):
        reply = self.reply()
        evidence = self.evidence({"reply": None}, {"reply": reply}, U, None)
        self.assertEqual(evidence["server_reply"]["messages"], [{"attribute": "errorMessage",
                                                                 "text": "Cell is occupied."}])
        self.assertEqual(evidence["server_reply"]["http_status"], 200)
        self.assertNotIn("outcome_supported", evidence)

    # Verifies: S13-R5
    def test_secret_looking_text_is_withheld(self):
        for text in ("token aaaaaaaa-1111-2222-3333-444444444444", "QUFB" * 11, "mail me at someone@example.com"):
            with self.subTest(text=text):
                evidence = self.evidence({}, {"reply": self.reply(messages=[{"attribute": "message", "text": text}])},
                                         U, None)
                self.assertEqual(evidence["server_reply"]["messages"][0]["text"],
                                 "withheld: the leak scan flagged this text")

    # Verifies: S13-R5
    def test_no_new_reply_and_malformed_probes_journal_none(self):
        reply = self.reply()
        self.assertEqual(self.evidence({"reply": reply}, {"reply": reply}, U, None), {"server_reply": None})
        self.assertEqual(self.evidence({}, {"reply": None}, U, None), {"server_reply": None})
        self.assertEqual(self.evidence({}, {"probe_failed": True}, U, None), {"server_reply": None})
        odd = self.evidence({}, {"reply": self.reply(http_status=True, length="x", messages=["bad"])}, U, None)
        self.assertEqual((odd["server_reply"]["http_status"], odd["server_reply"]["length"],
                          odd["server_reply"]["messages"]), (None, None, []))

    def test_probe_reads_only_the_client_attribute(self):
        self.assertEqual(self.probe(object()), {"reply": None})
        holder = type("Holder", (), {"placeRoomReply": {"http_status": 200}})()
        self.assertEqual(self.probe(holder), {"reply": {"http_status": 200}})


# pss5's ship record shape (tests/fixtures/live_sanitized/rooms/buy_room2.json): a level 1 hull, design 2, and
# @UpgradeShipDesignId "0" beside a stale @UpgradeStartDate while no hull upgrade is under way.
SHIP = {"@ShipDesignId": "2", "@ShipLevel": "1", "@RaceId": "2", "@UpgradeShipDesignId": "0",
        "@UpgradeStartDate": "2026-10-19T19:15:13"}
HULL = {"ship_design_id": "3"}


class UpgradeShipTests(unittest.TestCase):
    """``upgrade_ship``: reconciled from the ship record alone, accepting a started (timed) upgrade."""

    def test_spec_shape(self):
        spec = get_spec("upgrade_ship")
        self.assertEqual((spec.client_method, spec.idempotency, spec.required_params, spec.mutating),
                         ("upgradeShip", Idempotency.NOT_IDEMPOTENT, ("ship_design_id",), True))
        self.assertFalse(spec.canary_safe)

    def test_bad_parameters_and_an_unobserved_ship_are_refused_before_the_call(self):
        spec = get_spec("upgrade_ship")
        self.assertEqual(check_preconditions(spec, snap(ship=SHIP), HULL), [])
        for params in ({"ship_design_id": "x"}, {"ship_design_id": " "}, {"ship_design_id": "-3"}):
            with self.subTest(params=params):
                self.assertTrue(check_preconditions(spec, snap(ship=SHIP), params))
        with self.assertRaises(ActionSpecError):
            check_preconditions(spec, snap(ship=SHIP), {"ship_design_id": ""})
        self.assertIn("ship section was not observed", check_preconditions(spec, snap(), HULL)[0])

    def test_outcomes(self):
        cases = {
            "the upgrade started (timed)": ({"ship": {**SHIP, "@UpgradeShipDesignId": "3"}}, V),
            "the hull changed at once": ({"ship": {**SHIP, "@ShipDesignId": "3", "@ShipLevel": "2"}}, V),
            "the ship is unchanged": ({"ship": SHIP}, N),
            "an upgrade to another hull started": ({"ship": {**SHIP, "@UpgradeShipDesignId": "4"}}, N),
            "the ship was not observed after": ({"ship": None}, U),
        }
        for name, (after, expected) in cases.items():
            with self.subTest(case=name):
                result = outcome("upgrade_ship", after, HULL, before_overrides={"ship": SHIP})
                self.assertIs(result.status, expected, result.reason)
        self.assertIs(outcome("upgrade_ship", {"ship": SHIP}, HULL).status, U)  # not observed before

    def test_a_hull_already_shown_before_the_call_is_not_this_upgrade(self):
        started = {**SHIP, "@UpgradeShipDesignId": "3"}
        self.assertIs(outcome("upgrade_ship", {}, HULL, before_overrides={"ship": started}).status, N)
        done = {**SHIP, "@ShipDesignId": "3"}
        self.assertIs(outcome("upgrade_ship", {}, HULL, before_overrides={"ship": done}).status, N)

    def test_the_reply_probe_reads_upgrade_ship_reply_and_is_journaled_after_scanning(self):
        spec = get_spec("upgrade_ship")
        self.assertEqual(spec.evidence_probe(object()), {"reply": None})  # a client without the summary
        reply = {"http_status": 200, "success_shape": False, "parsed": True, "length": 40,
                 "elements": ["ShipService", "UpgradeShip"], "messages": [{"attribute": "errorMessage",
                                                                           "text": "Not enough minerals."}]}
        holder = type("Holder", (), {"upgradeShipReply": reply})()
        self.assertEqual(spec.evidence_probe(holder), {"reply": reply})
        evidence = spec.evidence({"reply": None}, {"reply": reply}, U, None)
        self.assertEqual(evidence["server_reply"]["messages"][0]["text"], "Not enough minerals.")
        self.assertEqual(spec.evidence({"reply": None}, {"reply": None}, U, None), {"server_reply": None})


class CollectionEvidenceTests(unittest.TestCase):
    """The collection's room-fill probe and evidence never raise and never invent a fill."""

    def setUp(self):
        spec = get_spec("collect_all_resources")
        self.probe, self.evidence = spec.evidence_probe, spec.evidence

    def test_collection_has_a_room_fill_probe(self):
        self.assertIsNotNone(self.probe)
        self.assertIsNotNone(self.evidence)

    def test_probe_reads_only_client_attributes_and_survives_bad_ones(self):
        class Exploding:
            @property
            def rooms(self):
                raise RuntimeError("boom")

        class Odd:
            rooms = [{"@RoomId": "x1", "@RoomDesignId": "5", "@CapacityUsed": "7"}, "not a record", {"@RoomDesignId": "5"}]
            roomDesigns = "not designs"

        self.assertEqual(self.probe(Exploding()), {"probe_failed": True})
        self.assertEqual(self.probe(object()), {"probe_failed": True})
        odd = self.probe(Odd())
        # A record that is not a room is skipped; a room without an id still counts.
        self.assertEqual((odd["fills"], odd["designs"]), ({"x1": ("5", 7), "#1": ("5", None)}, {}))
        single = type("Single", (), {"rooms": {"@RoomId": "x2", "@RoomDesignId": "6", "@CapacityUsed": "0"}})()
        self.assertEqual(self.probe(single)["fills"], {"x2": ("6", 0)})

    def test_room_designs_that_cannot_be_read_keep_the_fill(self):
        class BadDesigns:
            rooms = [{"@RoomId": "x1", "@RoomDesignId": "5", "@CapacityUsed": "7"}]

            @property
            def roomDesigns(self):
                raise RuntimeError("boom")

        probe = self.probe(BadDesigns())
        self.assertEqual((probe["fills"], probe["designs"]), ({"x1": ("5", 7)}, {}))

    def test_evidence_never_raises_on_malformed_probes(self):
        malformed = ({"fills": "nonsense"}, {"fills": {"r": "bad"}}, {"fills": {"r": ("1", 2)}, "designs": 4}, {})
        for before in malformed:
            for after in malformed:
                with self.subTest(before=before, after=after):
                    for delta in (None, self.delta):
                        self.assertIsInstance(self.evidence(before, after, N, delta), dict)

    @property
    def delta(self):
        return diff_snapshots(snap(), snap(seconds=60))

    @staticmethod
    def empty_rooms():
        return {"fills": {"r": ("1", 0)}, "designs": {}, "source": [object()]}

    def test_both_probes_failed_journals_no_evidence(self):
        self.assertEqual(self.evidence({"probe_failed": True}, {"probe_failed": True}, N, self.delta), {})

    def test_empty_note_only_for_a_not_observed_collection(self):
        self.assertIn("room_fill_note", self.evidence(self.empty_rooms(), self.empty_rooms(), N, self.delta))
        for status in (V, C, U):
            with self.subTest(status=status):
                self.assertNotIn("room_fill_note",
                                 self.evidence(self.empty_rooms(), self.empty_rooms(), status, self.delta))

    def test_rooms_not_read_fresh_on_both_sides_are_unobserved(self):
        before = self.empty_rooms()
        rooms_missing_after = diff_snapshots(snap(), snap(seconds=60, rooms=None))
        cases = {
            "after-snapshot failed": (before, self.empty_rooms(), None),
            "rooms section missing": (before, self.empty_rooms(), rooms_missing_after),
            "same list as before": (before, dict(before), self.delta),
        }
        for name, (first, second, delta) in cases.items():
            with self.subTest(case=name):
                evidence = self.evidence(first, second, N, delta)
                self.assertEqual((evidence["room_fill_observed"], evidence["room_fill_before_total"],
                                  evidence["room_fill_after_total"], evidence["room_fill"]), (False, None, None, []))
                self.assertNotIn("room_fill_note", evidence)


if __name__ == "__main__":
    unittest.main()
