"""Tests for sdk.actions catalog and reconciliation. Synthetic snapshots only."""

import json
import os
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
             "run_pvp_battle"},
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
