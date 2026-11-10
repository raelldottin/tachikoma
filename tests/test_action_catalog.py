"""Tests for sdk.actions catalog and reconciliation. Synthetic snapshots only."""

import json
import unittest
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
from sdk.state import AccountMismatchError, build_snapshot

REPO_ROOT = Path(__file__).resolve().parents[1]
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
            {"collect_all_resources", "collect_daily_reward", "add_training", "upgrade_room", "add_research"},
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
        self.assertIn(
            "crew member c9 is not in the snapshot",
            check_preconditions(get_spec("add_training"), snap(), {"training_design_id": "1", "character_id": "c9"}),
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

    def test_add_training_outcomes(self):
        params = {"training_design_id": "7", "character_id": "c1"}
        idle = {"crew": [{"@CharacterId": "c1", "@TrainingEndDate": "", "@TrainingDesignId": "0"}]}

        def training(design, end="2026-11-10T13:00:00"):
            return {"crew": [{"@CharacterId": "c1", "@TrainingEndDate": end, "@TrainingDesignId": design}]}

        self.assertIs(outcome("add_training", training("7"), params, before_overrides=idle).status, V)
        self.assertIs(outcome("add_training", training(""), params, before_overrides=idle).status, V)
        self.assertIs(outcome("add_training", idle, params, before_overrides=idle).status, N)
        self.assertIs(outcome("add_training", training("8"), params, before_overrides=idle).status, C)
        self.assertIs(outcome("add_training", {"crew": [{"@CharacterId": "c2"}]}, params, before_overrides=idle).status, C)
        # Training fields absent on either side: unknown, never assumed.
        self.assertIs(outcome("add_training", {"crew": [{"@CharacterId": "c1", "@Level": "4"}]}, params).status, U)
        self.assertIs(outcome("add_training", {"crew": None}, params, before_overrides=idle).status, U)

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


if __name__ == "__main__":
    unittest.main()
