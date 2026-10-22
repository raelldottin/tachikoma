"""Task H snapshot tests: determinism, version rejection, corruption,
duplicates, contradictions, evidence floors, secret leakage, fail-closed
loading, and non-actionability.

Every test builds synthetic catalogs/ledgers/plans only — no Client, no
network, no live traffic. Snapshots round-trip through pure JSON text
(no filesystem access in sdk/inventory).
"""

from __future__ import annotations

import inspect
import json
import unittest
from typing import Any, Dict, List

import sdk.inventory as inv
from sdk.inventory import (
    Evidence,
    InventoryCatalog,
    Item,
    ItemDesign,
    PlanningContext,
    SnapshotAccountIdentity,
    SnapshotError,
    build_snapshot,
    snapshot_from_dict,
    snapshot_from_json,
)


def make_design(
    design_id,
    name="Widget",
    sub_type="",
    item_type="",
    rarity="common",
    tags=(),
    ingredients=(),
    module_type=None,
    training_design_id=None,
):
    return ItemDesign(
        item_design_id=design_id,
        item_name=name,
        item_type=item_type,
        item_sub_type=sub_type,
        rarity=rarity,
        tags=tuple(tags),
        ingredients=tuple(ingredients),
        module_type=module_type,
        training_design_id=training_design_id,
    )


def make_catalog(rows, designs):
    """rows: (item_id, design_id, qty)."""
    items = [
        Item(
            item_id=iid,
            ship_id=55,
            item_design_id=did,
            quantity=qty,
            is_new=False,
            skin_key=None,
        )
        for iid, did, qty in rows
    ]
    return InventoryCatalog(items, inv.DesignCatalog(designs))


def build_dfg(rows, designs, equipped=None, rooms=None):
    """Run the real Task D + F + G pipeline over synthetic inputs."""
    catalog = make_catalog(rows, designs)
    ctx = PlanningContext(
        equipped_references=equipped if equipped is not None else {},
        room_references=rooms if rooms is not None else {},
    )
    classified = inv.InventoryClassifier(context=ctx.protection_context()).classify(catalog)
    ledger = inv.DemandPlanner().plan(catalog, ctx)
    reduction = inv.ReductionPlanner().plan(classified, ledger)
    return catalog, classified, ledger, reduction


def make_identity(user_id=101, ship_id=55, name="alpha") -> SnapshotAccountIdentity:
    return SnapshotAccountIdentity(user_id=user_id, ship_id=ship_id, display_name=name)


def valid_snapshot(user_id=101) -> inv.AccountSnapshot:
    designs = [make_design(501, "Steel Plate")]
    rows = [(1, 501, 5)]
    _, classified, ledger, reduction = build_dfg(rows, designs)
    return build_snapshot(
        make_identity(user_id),
        classified,
        ledger,
        reduction,
        captured_at="2026-09-28T00:00:00Z",
    )


def valid_payload() -> Dict[str, Any]:
    return json.loads(valid_snapshot().to_json())


class TestBuildSnapshot(unittest.TestCase):
    def test_build_projects_dfg_state(self):
        designs = [
            make_design(501, "Steel Plate"),
            make_design(502, "Crew Hat", sub_type="equipmentbody"),
        ]
        rows = [(1, 501, 5), (2, 502, 3)]
        _, classified, ledger, reduction = build_dfg(rows, designs, equipped={2: 1})
        snap = build_snapshot(make_identity(), classified, ledger, reduction)
        self.assertEqual(snap.version, inv.SNAPSHOT_VERSION)
        self.assertEqual(snap.user_id, 101)
        self.assertEqual(len(snap.stacks), 2)
        self.assertFalse(snap.actionable)
        self.assertEqual(snap.actionable_rows(), ())
        # stack 2 is equipped -> protected, candidate 0
        stack2 = next(s for s in snap.stacks if s.item_id == 2)
        self.assertTrue(stack2.protected)
        self.assertEqual(stack2.classification, "PROTECTED")
        self.assertEqual(stack2.candidate_quantity, 0)
        # stack 1 unprotected candidate equals its quantity
        stack1 = next(s for s in snap.stacks if s.item_id == 1)
        self.assertFalse(stack1.protected)
        self.assertEqual(stack1.candidate_quantity, 5)
        self.assertEqual(stack1.candidate_state, "CANDIDATE")

    def test_build_is_deterministic(self):
        designs = [make_design(501, "Steel Plate"), make_design(502, "Crew Hat")]
        rows = [(1, 501, 5), (2, 502, 3), (3, 501, 2)]
        _, classified, ledger, reduction = build_dfg(rows, designs)
        a = build_snapshot(
            make_identity(), classified, ledger, reduction, captured_at="2026-09-28T00:00:00Z"
        )
        b = build_snapshot(
            make_identity(), classified, ledger, reduction, captured_at="2026-09-28T00:00:00Z"
        )
        self.assertEqual(a.to_json(), b.to_json())

    def test_build_is_input_order_independent(self):
        designs = [make_design(501, "Steel Plate")]
        rows = [(1, 501, 5), (2, 501, 2)]
        _, classified, ledger, reduction = build_dfg(rows, designs)
        a = build_snapshot(make_identity(), classified, ledger, reduction)
        # reverse the classified sequence: builder must normalize
        b = build_snapshot(
            make_identity(),
            tuple(reversed(classified)),
            ledger,
            reduction,
        )
        self.assertEqual(a.to_json(), b.to_json())

    def test_build_rejects_missing_reduction_row(self):
        designs = [make_design(501)]
        rows = [(1, 501, 5)]
        _, classified, ledger, _reduction = build_dfg(rows, designs)
        with self.assertRaises(SnapshotError):
            build_snapshot(
                make_identity(),
                classified,
                ledger,
                inv.ReductionPlan(rows=()),
            )

    def test_json_round_trip_is_lossless(self):
        designs = [make_design(501, "Steel Plate"), make_design(502, "Crew Hat")]
        rows = [(1, 501, 5), (2, 502, 3)]
        _, classified, ledger, reduction = build_dfg(rows, designs, equipped={2: 1})
        snap = build_snapshot(
            make_identity(), classified, ledger, reduction, captured_at="2026-09-28T00:00:00Z"
        )
        text = snap.to_json()
        loaded = snapshot_from_json(text)
        self.assertEqual(loaded.to_json(), text)
        self.assertEqual(loaded, snap)

    def test_snapshot_carries_no_secret_fields(self):
        designs = [make_design(501)]
        rows = [(1, 501, 1)]
        _, classified, ledger, reduction = build_dfg(rows, designs)
        snap = build_snapshot(make_identity(), classified, ledger, reduction)
        text = json.dumps(snap.to_dict())
        for secret in (
            "accessToken",
            "refreshToken",
            "password",
            "deviceKey",
            "clientKey",
            "secret",
        ):
            self.assertNotIn(secret, text)

    def test_conditions_and_limitations_round_trip(self):
        designs = [make_design(501, "Steel Plate")]
        rows = [(1, 501, 5)]
        _, classified, ledger, reduction = build_dfg(rows, designs)
        snap = build_snapshot(
            make_identity(),
            classified,
            ledger,
            reduction,
            limitations=("equipped references unavailable",),
        )
        # Task F always emits the global SOURCE_UNSUPPORTED conditions
        self.assertTrue(snap.global_conditions)
        loaded = snapshot_from_json(snap.to_json())
        self.assertEqual(loaded.conditions, snap.conditions)
        self.assertEqual(loaded.limitations, snap.limitations)

    def test_design_rows_carry_ledger_totals(self):
        designs = [make_design(501, "Steel Plate")]
        rows = [(1, 501, 5)]
        _, classified, ledger, reduction = build_dfg(rows, designs)
        snap = build_snapshot(make_identity(), classified, ledger, reduction)
        d = snap.design_row(501)
        self.assertIsNotNone(d)
        assert d is not None
        self.assertEqual(d.observed_quantity, 5)
        self.assertEqual(d.requested_quantity, 0)
        self.assertEqual(d.reserved_quantity, 0)
        self.assertEqual(d.unmet_quantity, 0)
        self.assertEqual(d.stacks, 1)
        self.assertEqual(d.item_name, "Steel Plate")

    def test_design_unmet_is_request_minus_observed(self):
        # Hand-built ledger with unresolvable demand (the Task F planner
        # caps reservations at supply and emits DEMAND_EXCEEDS_SUPPLY; the
        # snapshot must surface the shortfall as unmet).
        designs = [make_design(501, "Steel Plate")]
        rows = [(1, 501, 5)]
        _, classified, ledger, _reduction = build_dfg(rows, designs)
        over_ledger = inv.ReservationLedger(
            entries=(
                inv.ReservationEntry(
                    item_design_id=501,
                    source=inv.ReservationSource.STRATEGIC,
                    requested_quantity=8,
                    reserved_quantity=5,
                    reason="test",
                    evidence=Evidence.CAPTURED_NOT_VERIFIED,
                    provenance="test",
                ),
            ),
            conditions=(),
            observed_by_design={501: 5},
        )
        reduction = inv.ReductionPlanner().plan(classified, over_ledger)
        snap = build_snapshot(make_identity(), classified, over_ledger, reduction)
        d = snap.design_row(501)
        self.assertIsNotNone(d)
        assert d is not None
        self.assertEqual(d.requested_quantity, 8)
        self.assertEqual(d.reserved_quantity, 5)
        self.assertEqual(d.unmet_quantity, 3)


class TestSnapshotLoadFailClosed(unittest.TestCase):
    def test_rejects_unknown_version(self):
        payload = valid_payload()
        payload["version"] = 2
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)
        payload["version"] = "1"
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)

    def test_rejects_missing_version(self):
        payload = valid_payload()
        del payload["version"]
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)

    def test_rejects_malformed_json(self):
        with self.assertRaises(SnapshotError):
            snapshot_from_json("{not json")

    def test_rejects_non_object_root(self):
        with self.assertRaises(SnapshotError):
            snapshot_from_dict([1, 2, 3])
        with self.assertRaises(SnapshotError):
            snapshot_from_dict("nope")
        with self.assertRaises(SnapshotError):
            snapshot_from_json(json.dumps([1]))

    def test_rejects_duplicate_item_ids(self):
        payload = valid_payload()
        stacks: List[Dict[str, Any]] = payload["stacks"]
        stacks.append(dict(stacks[0]))
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)

    def test_rejects_duplicate_design_ids(self):
        payload = valid_payload()
        designs: List[Dict[str, Any]] = payload["designs"]
        designs.append(dict(designs[0]))
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)

    def test_rejects_negative_quantity(self):
        payload = valid_payload()
        payload["stacks"][0]["quantity"] = -1
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)

    def test_rejects_unknown_evidence_tier(self):
        payload = valid_payload()
        payload["stacks"][0]["evidence"] = "SUPER_VERIFIED"
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)

    def test_rejects_stack_design_mismatch(self):
        payload = valid_payload()
        payload["stacks"][0]["item_design_id"] = 9999
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)

    def test_rejects_stack_quantity_exceeding_design_observed(self):
        payload = valid_payload()
        payload["stacks"][0]["quantity"] = 99
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)

    def test_rejects_candidate_exceeding_quantity(self):
        payload = valid_payload()
        payload["stacks"][0]["candidate_quantity"] = 6  # > quantity 5
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)

    def test_rejects_design_observed_disagreeing_with_stack_sum(self):
        payload = valid_payload()
        payload["designs"][0]["observed_quantity"] = 4  # stacks sum to 5
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)

    def test_rejects_design_stack_count_disagreement(self):
        payload = valid_payload()
        payload["designs"][0]["stacks"] = 2  # only one stack row exists
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)

    def test_rejects_actionable_true(self):
        payload = valid_payload()
        payload["actionable"] = True
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)

    def test_rejects_secret_keys_fail_closed(self):
        payload = valid_payload()
        payload["accessToken"] = "abc123"
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)

    def test_rejects_nested_secret_keys(self):
        payload = valid_payload()
        payload["stacks"][0]["refreshToken"] = "xyz"
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)

    def test_rejects_wrong_field_types(self):
        payload = valid_payload()
        payload["identity"]["user_id"] = "101"
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)
        payload = valid_payload()
        payload["stacks"][0]["protected"] = "yes"
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)

    def test_rejects_missing_identity(self):
        payload = valid_payload()
        del payload["identity"]
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)

    def test_rejects_truncated_payload(self):
        payload = valid_payload()
        del payload["stacks"]
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)
        del payload["designs"]
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(payload)


class TestSnapshotEvidenceFloors(unittest.TestCase):
    def test_evidence_round_trip_never_promotes(self):
        # Feed-derived rows stay at CAPTURED_NOT_VERIFIED or below (the
        # ListItemsOfAShip evidence cap). Round-tripping must not promote.
        designs = [make_design(501, "Steel Plate")]
        rows = [(1, 501, 5)]
        _, classified, ledger, reduction = build_dfg(rows, designs)
        snap = build_snapshot(make_identity(), classified, ledger, reduction)
        loaded = snapshot_from_json(snap.to_json())
        for stack in loaded.stacks:
            tier = Evidence(stack.evidence)
            self.assertNotEqual(tier, Evidence.VERIFIED)
            tier_c = Evidence(stack.candidate_evidence)
            self.assertNotEqual(tier_c, Evidence.VERIFIED)

    def test_unknown_design_stack_stays_unknown(self):
        # A stack whose design is absent from the feed classifies
        # PROTECTED/unknown with UNKNOWN evidence — the snapshot must
        # carry that floor, never upgrade it.
        designs: List[ItemDesign] = []
        rows = [(1, 501, 5)]
        _, classified, ledger, reduction = build_dfg(rows, designs)
        snap = build_snapshot(make_identity(), classified, ledger, reduction)
        self.assertEqual(len(snap.stacks), 1)
        self.assertTrue(snap.stacks[0].unknown)
        self.assertEqual(Evidence(snap.stacks[0].evidence), Evidence.UNKNOWN)
        loaded = snapshot_from_json(snap.to_json())
        self.assertEqual(Evidence(loaded.stacks[0].evidence), Evidence.UNKNOWN)


class TestSnapshotPurity(unittest.TestCase):
    def test_no_network_imports_in_new_modules(self):
        for module in (inv.snapshot, inv.portfolio):
            source = inspect.getsource(module)
            for token in (
                "import requests",
                "from requests",
                "import socket",
                "import urllib",
            ):
                self.assertNotIn(token, source)

    def test_module_has_no_filesystem_calls(self):
        for module in (inv.snapshot, inv.portfolio):
            source = inspect.getsource(module)
            for token in ("open(", "Path(", "os.remove", "shutil"):
                self.assertNotIn(token, source)

    def test_no_forbidden_endpoint_call_sites(self):
        # Repo safety convention (test_inventory_safety.py): scan for CALL
        # sites of mutation endpoints, not docstring mentions naming the
        # prohibitions. No call site may exist in the new modules.
        for module in (inv.snapshot, inv.portfolio):
            source = inspect.getsource(module)
            for endpoint in (
                "GiftItem",
                "SellItem3",
                "Purchase4",
                "ConsumeItem2",
                "DeleteItem3",
                "CombineItem4",
            ):
                self.assertNotIn(f"{endpoint}(", source)


class TestSnapshotAdversarialGates(unittest.TestCase):
    """Adversarial regression gates (lane t_2c696fd5).

    Each test pins one confirmed fail-open hole from the probe-first
    pass against the pre-fix tree. Fail-closed everywhere: any
    producer-impossible or off-vocabulary value must abort the load.
    """

    def _reject(self, mutate, fragment=None):
        payload = valid_payload()
        mutate(payload)
        with self.assertRaises(SnapshotError) as ctx:
            snapshot_from_dict(payload)
        if fragment is not None:
            self.assertIn(fragment, str(ctx.exception))

    def test_rejects_float_and_bool_version(self):
        # 1.0 and True compare == 1 in Python; the version gate must be
        # a strict int check, not an equality check.
        self._reject(lambda p: p.update(version=1.0), "unsupported snapshot version")
        self._reject(lambda p: p.update(version=True), "unsupported snapshot version")

    def test_rejects_user_id_zero(self):
        # Pinned §2/§3b: negative/zero user_id invalid.
        self._reject(
            lambda p: p["identity"].update(user_id=0), "identity.user_id must be >= 1"
        )

    def test_rejects_unknown_keys_at_every_level(self):
        # Pinned §3b/§7a: strict schema; unknown keys are malformed.
        self._reject(lambda p: p.update(totally_unknown=1), "unknown key")
        self._reject(lambda p: p["identity"].update(extra=1), "unknown key")
        self._reject(lambda p: p["stacks"][0].update(extra=1), "unknown key")
        self._reject(lambda p: p["designs"][0].update(extra=1), "unknown key")
        self._reject(
            lambda p: p["conditions"].append(
                {"code": "SOURCE_UNSUPPORTED", "detail": "d", "junk": 1}
            ),
            "unknown key",
        )

    def test_rejects_off_convention_captured_at(self):
        # Pinned §4: ISO-8601 UTC Z-suffix seconds exactly; unparseable
        # = malformed at deserialization.
        for bad in (
            "not-a-timestamp",
            "2026-09-28T00:00:00",  # naive, no Z
            "2026-09-28T00:00:00-04:00",  # offset form
            "2026-09-28T00:00:00.123Z",  # sub-second precision
            "",
        ):
            payload = valid_payload()
            payload["captured_at"] = bad
            with self.assertRaises(SnapshotError, msg=f"captured_at={bad!r}"):
                snapshot_from_dict(payload)

    def test_build_snapshot_rejects_off_convention_captured_at(self):
        designs = [make_design(501)]
        rows = [(1, 501, 1)]
        _, classified, ledger, reduction = build_dfg(rows, designs)
        for bad in ("T0", "", "2026-09-28 00:00:00"):
            with self.assertRaises(SnapshotError, msg=f"captured_at={bad!r}"):
                build_snapshot(
                    make_identity(), classified, ledger, reduction, captured_at=bad
                )

    def test_rejects_verified_evidence_under_the_cap(self):
        # ListItemsOfAShip cap: feed-derived rows can never be VERIFIED
        # while the endpoint has zero captures.
        self._reject(
            lambda p: p["stacks"][0].update(evidence="VERIFIED"),
            "above the ListItemsOfAShip evidence cap",
        )
        self._reject(
            lambda p: p["stacks"][0].update(candidate_evidence="VERIFIED"),
            "above the ListItemsOfAShip evidence cap",
        )
        self._reject(
            lambda p: p["designs"][0].update(evidence="VERIFIED"),
            "above the ListItemsOfAShip evidence cap",
        )

    def test_rejects_unknown_vocabulary_values(self):
        self._reject(
            lambda p: p["stacks"][0].update(disposition_hint="TRANSFER"),
            "not a known disposition",
        )
        self._reject(
            lambda p: p["stacks"][0].update(candidate_state="EXECUTE"),
            "HOLD or CANDIDATE",
        )
        self._reject(
            lambda p: p["stacks"][0].update(classification="SELLABLE"),
            "four buckets",
        )
        self._reject(
            lambda p: p["stacks"][0].update(classification="TRANSFERABLE"),
            "four buckets",
        )

    def test_rejects_forbidden_slice_roles(self):
        # Roles that imply transfer/marketplace/discard semantics are
        # not producible this slice and must not load.
        for role in ("TRANSFERABLE_CANDIDATE", "MARKETPLACE_SALE_CANDIDATE", "DISCARD_CANDIDATE"):
            self._reject(
                lambda p, r=role: p["stacks"][0].update(roles=[r]),
                "forbidden this-slice role",
            )

    def test_rejects_unknown_reason_and_condition_codes(self):
        self._reject(
            lambda p: p["stacks"][0].update(candidate_reasons=["BLOCKED_FREE_MONEY"]),
            "not a known BLOCKED_* reason code",
        )
        self._reject(
            lambda p: p["stacks"][0].update(protection_reasons=["NOT_A_RULE"]),
            "not a known protection reason code",
        )
        self._reject(
            lambda p: p.update(conditions=[{"code": "TRANSFER_READY", "detail": "x"}]),
            "not a known unresolved-condition code",
        )

    def test_rejects_producer_impossible_state_couplings(self):
        # CANDIDATE rows carry a positive candidate, no blocked reasons
        # and a non-KEEP hint; HOLD rows carry zero candidate and at
        # least one reason. Anything else was never produced by the
        # planner and is a hand-edit.
        self._reject(
            lambda p: p["stacks"][0].update(candidate_quantity=2, candidate_state="HOLD"),
            "CANDIDATE row always carries a positive candidate",
        )
        self._reject(
            lambda p: p["stacks"][0].update(
                candidate_state="CANDIDATE", candidate_quantity=0
            ),
            "HOLD row always carries zero",
        )
        self._reject(
            lambda p: p["stacks"][0].update(
                candidate_state="CANDIDATE",
                candidate_reasons=["BLOCKED_ZERO_QUANTITY"],
            ),
            "CANDIDATE but carries blocked reasons",
        )
        # KEEP hint on a CANDIDATE row (hints: KEEP is HOLD-only):
        self._reject(
            lambda p: p["stacks"][0].update(
                candidate_state="CANDIDATE",
                candidate_reasons=[],
                disposition_hint="KEEP",
            ),
            "CANDIDATE but carries blocked reasons or a KEEP hint",
        )

    def test_rejects_protection_contradictions(self):
        # Rule 10: an unknown design is always PROTECTED; protection
        # always names its reason; a reason always protects.
        self._reject(
            lambda p: p["stacks"][0].update(
                unknown=True, classification="CONSUMABLE", protected=False
            ),
            "unknown but not PROTECTED/protected",
        )
        self._reject(
            lambda p: p["stacks"][0].update(
                protected=True, protection_reasons=[]
            ),
            "protected with no protection reason",
        )
        self._reject(
            lambda p: p["stacks"][0].update(
                protected=False, protection_reasons=["PROTECTED_RARITY"]
            ),
            "protection reasons but is not protected",
        )

    def test_rejects_demand_arithmetic_contradictions(self):
        # Stack demand totals mirror the design row's ledger totals;
        # reserved never exceeds requested; unmet is exactly the
        # DEMAND_EXCEEDS_SUPPLY shortfall.
        self._reject(
            lambda p: (
                p["stacks"][0].update(requested_quantity=7),
            ),
            "requested 7 disagrees with design 501 requested 0",
        )
        # design-level over-reservation (reserved > requested):
        self._reject(
            lambda p: (
                p["stacks"][0].update(reserved_quantity=3),
                p["designs"][0].update(reserved_quantity=3),
            ),
            "exceeds requested",
        )
        # stack-level reserved disagreeing with the design row:
        self._reject(
            lambda p: p["stacks"][0].update(reserved_quantity=3),
            "disagrees with design",
        )
        self._reject(
            lambda p: (
                p["stacks"][0].update(requested_quantity=2, reserved_quantity=5),
                p["designs"][0].update(requested_quantity=2, reserved_quantity=5),
            ),
            "exceeds requested",
        )
        self._reject(
            lambda p: (
                p["stacks"][0].update(requested_quantity=8),
                p["designs"][0].update(requested_quantity=8, unmet_quantity=99),
            ),
            "is not the demand shortfall",
        )

    def test_rejects_candidate_pool_overrun(self):
        # Two stacks of one design whose candidates sum above the
        # design's unreserved pool were never produced by the planner.
        def mutate(p):
            first = dict(p["stacks"][0])
            second = dict(p["stacks"][0])
            second["item_id"] = 2
            for s in (first, second):
                s["quantity"] = 3
                s["candidate_quantity"] = 3
                s["candidate_state"] = "CANDIDATE"
                s["candidate_reasons"] = []
                s["disposition_hint"] = "USE"
            p["stacks"] = [first, second]
            p["designs"][0]["observed_quantity"] = 6
            p["designs"][0]["stacks"] = 2
            p["designs"][0]["reserved_quantity"] = 2
            for s in p["stacks"]:
                s["reserved_quantity"] = 2
                s["requested_quantity"] = 2
            p["designs"][0]["requested_quantity"] = 2

        payload = valid_payload()
        mutate(payload)
        # candidates 3+3=6 > unreserved 6-2=4
        with self.assertRaises(SnapshotError) as ctx:
            snapshot_from_dict(payload)
        self.assertIn("exceeds the per-design pool", str(ctx.exception))

    def test_rejects_condition_for_missing_design(self):
        self._reject(
            lambda p: p["conditions"].append(
                {"code": "DEMAND_EXCEEDS_SUPPLY", "detail": "x", "design_id": 424242}
            ),
            "references design 424242 with no design row",
        )

    def test_rejects_duplicate_classified_items_at_build(self):
        # AD-4: a duplicated classified sequence must refuse, not
        # double-count.
        designs = [make_design(501)]
        rows = [(1, 501, 5)]
        _, classified, ledger, reduction = build_dfg(rows, designs)
        with self.assertRaises(SnapshotError) as ctx:
            build_snapshot(make_identity(), classified + classified, ledger, reduction)
        self.assertIn("duplicate ItemId 1 in classified sequence", str(ctx.exception))

    def test_secret_scan_covers_list_of_list_nesting(self):
        payload = valid_payload()
        payload["limitations"] = [[{"accessToken": "smuggled"}]]
        with self.assertRaises(SnapshotError) as ctx:
            snapshot_from_dict(payload)
        self.assertIn("forbidden", str(ctx.exception))

    def test_rejects_duplicate_json_keys(self):
        # Last-wins duplicate keys make a payload ambiguous; reject
        # rather than silently resolve.
        text = json.dumps(valid_payload())
        duplicated = text[:-1] + ',"captured_at":"1970-01-01T00:00:00Z"}'
        with self.assertRaises(SnapshotError) as ctx:
            snapshot_from_json(duplicated)
        self.assertIn("duplicate JSON key", str(ctx.exception))

    def test_valid_snapshot_round_trips_through_every_gate(self):
        # A real-pipeline snapshot passes its own full gate battery
        # (the aggregate re-validation path exercises the same code).
        snap = valid_snapshot()
        again = snapshot_from_dict(snap.to_dict())
        self.assertEqual(again.to_json(), snap.to_json())


if __name__ == "__main__":
    unittest.main()
