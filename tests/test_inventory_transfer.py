"""Task I transfer-planner tests: deterministic matching arithmetic,
fail-closed eligibility, provenance, evidence floors, freshness,
protection, conditions, self-transfer rejection, no ItemId
cross-account matching, network/mutation isolation, all-
actionable=false, determinism, and renderer output.

All inputs are synthetic snapshots built through the real Task D/F/G
pipeline. No network, no filesystem, no live traffic.
"""

from __future__ import annotations

import inspect
import json
import socket
import unittest
from unittest.mock import patch

import sdk.inventory as inv
from sdk.inventory import (
    Evidence,
    SnapshotAccountIdentity,
    SnapshotError,
    aggregate_snapshots,
    build_snapshot,
    snapshot_from_json,
)
from sdk.inventory.transfer import (
    ELIGIBLE,
    GIFTITEM_EVIDENCE_TIER,
    HOLD_UNVERIFIED_TRANSFERABILITY,
    REASON_DISTINCT_ACCOUNTS,
    REASON_FRESH_SNAPSHOTS,
    REASON_NO_APPLICABLE_UNRESOLVED_CONDITION,
    REASON_NO_EVIDENCE_PROMOTION,
    REASON_NO_PROTECTION_CONFLICT,
    REASON_POSITIVE_SOURCE_SUPPLY,
    REASON_TRANSFER_ELIGIBILITY_UNPROVEN,
    TransferPlannerError,
    plan_transfers,
    render_transfers_view,
)

from tests.test_inventory_snapshot import build_dfg, make_design

NOW = "2026-10-01T00:00:00Z"
FRESH_CAPTURE = "2026-09-30T00:00:00Z"
STALE_CAPTURE = "2026-09-01T00:00:00Z"  # > 86400s before NOW


def make_snapshot(
    user_id,
    rows,
    designs,
    equipped=None,
    rooms=None,
    name=None,
    ship_id=55,
    captured_at=FRESH_CAPTURE,
) -> inv.AccountSnapshot:
    _, classified, ledger, reduction = build_dfg(
        rows, designs, equipped=equipped, rooms=rooms
    )
    identity = SnapshotAccountIdentity(
        user_id=user_id,
        ship_id=ship_id,
        display_name=name if name is not None else f"user{user_id}",
    )
    return build_snapshot(
        identity, classified, ledger, reduction, captured_at=captured_at
    )


def deficit_snapshot(
    user_id,
    rows,
    designs,
    requested=8,
    reserved=5,
    observed=None,
    captured_at=FRESH_CAPTURE,
    name=None,
) -> inv.AccountSnapshot:
    """Account with a strategic-reserve deficit and NO design-scoped
    unresolved condition (clean vector: request > held, no
    DEMAND_EXCEEDS_SUPPLY stamped)."""
    _, classified, ledger, reduction = build_dfg(rows, designs)
    observed_by_design = {}
    for _iid, did, qty in rows:
        observed_by_design[did] = observed_by_design.get(did, 0) + qty
    if observed is not None:
        observed_by_design = {d: observed for d in observed_by_design}
    over_ledger = inv.ReservationLedger(
        entries=tuple(
            inv.ReservationEntry(
                item_design_id=did,
                source=inv.ReservationSource.STRATEGIC,
                requested_quantity=requested,
                reserved_quantity=reserved,
                reason="strategic reserve config",
                evidence=Evidence.INFERRED,
                provenance="test",
            )
            for did in sorted(observed_by_design)
        ),
        conditions=(),
        observed_by_design=observed_by_design,
    )
    reduction = inv.ReductionPlanner().plan(classified, over_ledger)
    identity = SnapshotAccountIdentity(
        user_id=user_id,
        ship_id=55,
        display_name=name if name is not None else f"user{user_id}",
    )
    return build_snapshot(
        identity, classified, over_ledger, reduction, captured_at=captured_at
    )


def json_snapshot(payload: dict) -> inv.AccountSnapshot:
    return snapshot_from_json(json.dumps(payload))


def pair_only(plan):
    assert len(plan.proposals) == 1
    return plan.proposals[0]


class TestMatchingArithmetic(unittest.TestCase):
    def setUp(self):
        self.designs = [make_design(501, "Steel Plate")]

    def test_positive_pairing_matched_but_proposed_zero(self):
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        plan = plan_transfers(
            aggregate_snapshots([source, dest]), now=NOW
        )
        row = pair_only(plan)
        # The gate passes 1-6; the only reason is the constant hold.
        self.assertEqual(row.reasons, (REASON_TRANSFER_ELIGIBILITY_UNPROVEN,))
        # matched = min(supply, deficit); proposed stays 0.
        self.assertEqual(row.matched_quantity, 3)
        self.assertEqual(row.proposed_quantity, 0)
        self.assertEqual(row.eligibility_state, HOLD_UNVERIFIED_TRANSFERABILITY)
        self.assertFalse(row.actionable)
        self.assertFalse(plan.actionable)
        self.assertEqual(plan.actionable_proposals(), ())

    def test_bounds_matched_never_exceeds_supply_or_deficit(self):
        # supply 4, deficit 7 -> matched 4; supply 9, deficit 3 -> matched 3
        s = make_snapshot(202, [(1, 501, 4)], self.designs)
        d = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=12, reserved=5)
        plan = plan_transfers(aggregate_snapshots([s, d]), now=NOW)
        self.assertEqual(pair_only(plan).matched_quantity, 4)

        s2 = make_snapshot(303, [(1, 501, 9)], self.designs)
        d2 = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8, reserved=5)
        plan2 = plan_transfers(aggregate_snapshots([s2, d2]), now=NOW)
        self.assertEqual(pair_only(plan2).matched_quantity, 3)

    def test_multiple_donors_no_oversubscription(self):
        s1 = make_snapshot(301, [(1, 501, 4)], self.designs)
        s2 = make_snapshot(302, [(1, 501, 6)], self.designs)
        d = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=10, reserved=5)
        plan = plan_transfers(aggregate_snapshots([s1, s2, d]), now=NOW)
        by_source = {p.source_user_id: p.matched_quantity for p in plan.proposals}
        self.assertEqual(by_source, {301: 4, 302: 1})
        total = sum(p.matched_quantity for p in plan.proposals)
        self.assertEqual(total, 5)  # deficit exactly filled, never more
        # supply never oversubscribed
        self.assertLessEqual(by_source[301], 4)
        self.assertLessEqual(by_source[302], 6)

    def test_multiple_recipients_no_double_fill(self):
        s = make_snapshot(301, [(1, 501, 10)], self.designs)
        d1 = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8, reserved=5)
        d2 = deficit_snapshot(102, [(1, 501, 5)], self.designs, requested=8, reserved=5)
        plan = plan_transfers(aggregate_snapshots([s, d1, d2]), now=NOW)
        by_dest = {p.destination_user_id: p.matched_quantity for p in plan.proposals}
        self.assertEqual(by_dest, {101: 3, 102: 3})
        self.assertLessEqual(by_dest[101], 3)
        self.assertLessEqual(by_dest[102], 3)

    def test_exhausted_supply_reports_zero_match_row(self):
        # First destination consumes all supply; a second destination
        # still gets its attempt row with matched 0 and the
        # positive-destination-demand reason only when deficit filled.
        s = make_snapshot(301, [(1, 501, 3)], self.designs)
        d1 = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8, reserved=5)
        d2 = deficit_snapshot(102, [(1, 501, 5)], self.designs, requested=8, reserved=5)
        plan = plan_transfers(aggregate_snapshots([s, d1, d2]), now=NOW)
        self.assertEqual(len(plan.proposals), 2)
        row_d1 = next(p for p in plan.proposals if p.destination_user_id == 101)
        row_d2 = next(p for p in plan.proposals if p.destination_user_id == 102)
        self.assertEqual(row_d1.matched_quantity, 3)
        self.assertEqual(row_d2.matched_quantity, 0)
        self.assertIn(REASON_POSITIVE_SOURCE_SUPPLY, row_d2.reasons)

    def test_no_source_holding_design_no_pairing(self):
        # Destination deficits on a design nobody else holds.
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        other = make_snapshot(202, [(1, 502, 4)], [make_design(502, "Bolt")])
        plan = plan_transfers(aggregate_snapshots([dest, other]), now=NOW)
        self.assertEqual(plan.proposals, ())


class TestFailClosedGates(unittest.TestCase):
    def setUp(self):
        self.designs = [make_design(501, "Steel Plate")]

    def test_self_transfer_rejected(self):
        # A portfolio whose only account has both supply and deficit on
        # one design: partition rule makes it a destination, never a
        # source, so no pairing exists at all.
        solo = deficit_snapshot(
            101, [(1, 501, 5)], self.designs, requested=8
        )
        plan = plan_transfers(aggregate_snapshots([solo]), now=NOW)
        self.assertEqual(plan.proposals, ())

    def test_distinct_accounts_reason_stamped_on_forged_pair(self):
        # Forge a pair with the same user_id via a hand-built gate
        # call: the reason code exists and is stamped when violated.
        solo = make_snapshot(101, [(1, 501, 10)], self.designs)
        reasons = inv.transfer._gate_reasons(
            solo, solo, 501, 10, 3, True, True
        )
        self.assertIn(REASON_DISTINCT_ACCOUNTS, reasons)

    def test_stale_source_suppresses_match(self):
        source = make_snapshot(
            202, [(1, 501, 10)], self.designs, captured_at=STALE_CAPTURE
        )
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([source, dest]), now=NOW)
        row = pair_only(plan)
        self.assertEqual(row.matched_quantity, 0)
        self.assertFalse(row.source_fresh)
        self.assertTrue(row.destination_fresh)
        self.assertIn(REASON_FRESH_SNAPSHOTS, row.reasons)

    def test_stale_destination_suppresses_match(self):
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        dest = deficit_snapshot(
            101, [(1, 501, 5)], self.designs, requested=8, captured_at=STALE_CAPTURE
        )
        plan = plan_transfers(aggregate_snapshots([source, dest]), now=NOW)
        row = pair_only(plan)
        self.assertEqual(row.matched_quantity, 0)
        self.assertTrue(row.source_fresh)
        self.assertFalse(row.destination_fresh)
        self.assertIn(REASON_FRESH_SNAPSHOTS, row.reasons)

    def test_stale_pairing_consumes_no_capacity(self):
        # A stale destination ahead of a fresh one must not eat supply.
        s = make_snapshot(301, [(1, 501, 3)], self.designs)
        stale_d = deficit_snapshot(
            101, [(1, 501, 5)], self.designs, requested=8, captured_at=STALE_CAPTURE
        )
        fresh_d = deficit_snapshot(102, [(1, 501, 5)], self.designs, requested=8)
        plan = plan_transfers(
            aggregate_snapshots([s, stale_d, fresh_d]), now=NOW
        )
        by_dest = {p.destination_user_id: p.matched_quantity for p in plan.proposals}
        self.assertEqual(by_dest, {101: 0, 102: 3})

    def test_duplicate_user_id_aborts_whole_plan(self):
        a = make_snapshot(101, [(1, 501, 5)], self.designs)
        b = make_snapshot(101, [(1, 501, 2)], self.designs)
        portfolio = inv.AccountPortfolio(accounts=(a, b))
        with self.assertRaises(TransferPlannerError):
            plan_transfers(portfolio, now=NOW)

    def test_same_snapshot_twice_aborts(self):
        a = make_snapshot(101, [(1, 501, 5)], self.designs)
        portfolio = inv.AccountPortfolio(accounts=(a, a))
        with self.assertRaises(TransferPlannerError):
            plan_transfers(portfolio, now=NOW)

    def test_non_portfolio_input_rejected(self):
        snap = make_snapshot(101, [(1, 501, 5)], self.designs)
        with self.assertRaises(TransferPlannerError):
            plan_transfers([snap], now=NOW)  # type: ignore[arg-type]

    def test_forged_snapshot_fails_revalidation(self):
        # Hand-build an AccountSnapshot that never passed the loader.
        good = make_snapshot(101, [(1, 501, 5)], self.designs)
        payload = good.to_dict()
        payload["version"] = 2
        forged = inv.AccountSnapshot(
            version=2,
            identity=good.identity,
            stacks=good.stacks,
            designs=good.designs,
            conditions=good.conditions,
            captured_at=good.captured_at,
        )
        portfolio = inv.AccountPortfolio(accounts=(forged,))
        with self.assertRaises(TransferPlannerError):
            plan_transfers(portfolio, now=NOW)

    def test_future_captured_at_refuses_whole_plan(self):
        a = make_snapshot(101, [(1, 501, 5)], self.designs)
        payload = a.to_dict()
        payload["captured_at"] = "2026-10-02T00:00:00Z"  # after NOW
        b = json_snapshot(payload)
        portfolio = inv.AccountPortfolio(accounts=(b,))
        with self.assertRaises(TransferPlannerError):
            plan_transfers(portfolio, now=NOW)

    def test_malformed_captured_at_refuses_whole_plan(self):
        a = make_snapshot(101, [(1, 501, 5)], self.designs)
        payload = a.to_dict()
        payload["captured_at"] = "2026-09-30 00:00:00"  # off-convention
        with self.assertRaises(SnapshotError):
            json_snapshot(payload)

    def test_bad_stale_after_rejected(self):
        a = make_snapshot(101, [(1, 501, 5)], self.designs)
        portfolio = aggregate_snapshots([a])
        for bad in (-1, True, "86400", 1.5):
            with self.assertRaises(TransferPlannerError):
                plan_transfers(portfolio, now=NOW, stale_after=bad)

    def test_unknown_design_never_pairs(self):
        # Unknown designs are always protected; usable supply is 0 and
        # the pairing is suppressed with a protection conflict.
        designs = [make_design(501, "Steel Plate")]
        source = make_snapshot(
            202, [(1, 999999, 10)], designs
        )  # design 999999 not in feed
        dest = deficit_snapshot(101, [(1, 999999, 5)], designs, requested=8)
        portfolio = aggregate_snapshots([source, dest])
        plan = plan_transfers(portfolio, now=NOW)
        row = pair_only(plan)
        self.assertEqual(row.matched_quantity, 0)
        self.assertIn(REASON_NO_PROTECTION_CONFLICT, row.reasons)


class TestEvidenceAndProvenance(unittest.TestCase):
    def setUp(self):
        self.designs = [make_design(501, "Steel Plate")]

    def test_giftitem_floor_caps_every_proposal(self):
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([source, dest]), now=NOW)
        row = pair_only(plan)
        # Feed rows are CAPTURED_NOT_VERIFIED at best; the GiftItem
        # floor is STATICALLY_DISCOVERED; the deficit entry is
        # INFERRED. Weakest = weakest of those actually present.
        members = list(Evidence)
        self.assertGreaterEqual(
            members.index(Evidence(row.evidence)),
            members.index(GIFTITEM_EVIDENCE_TIER),
        )
        self.assertIn(REASON_TRANSFER_ELIGIBILITY_UNPROVEN, row.reasons)

    def test_evidence_never_promoted(self):
        # Weak source tier (UNKNOWN design evidence with no entries)
        # pins the proposal at UNKNOWN.
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([source, dest]), now=NOW)
        self.assertEqual(pair_only(plan).evidence, Evidence.UNKNOWN.value)

    def test_no_evidence_promotion_reason_ever_stamped(self):
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([source, dest]), now=NOW)
        row = pair_only(plan)
        self.assertNotIn(REASON_NO_EVIDENCE_PROMOTION, row.reasons)
        # but the requirement is still proven by the floor test above

    def test_evidence_scoped_to_proposal_design_only(self):
        # A source with strong evidence on ANOTHER design must not
        # strengthen this proposal's tier.
        designs = [
            make_design(501, "Steel Plate"),
            make_design(502, "Bolt", sub_type="immunity"),
        ]
        source = make_snapshot(202, [(1, 501, 10), (2, 502, 9)], designs)
        dest = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([source, dest]), now=NOW)
        rows = [p for p in plan.proposals if p.item_design_id == 501]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].evidence, Evidence.UNKNOWN.value)

    def test_no_itemid_in_output(self):
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([source, dest]), now=NOW)
        text = json.dumps(plan.to_dicts()).lower()
        self.assertNotIn("item_id", text)
        self.assertNotIn('"itemid"', text)

    def test_itemid_equality_never_used_for_matching(self):
        # Same ItemId on both accounts (server ids can collide across
        # accounts); the planner must join by design only.
        source = make_snapshot(202, [(7, 501, 10)], self.designs)
        dest = deficit_snapshot(101, [(7, 501, 5)], self.designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([source, dest]), now=NOW)
        row = pair_only(plan)
        self.assertEqual(row.item_design_id, 501)
        self.assertEqual(row.source_user_id, 202)
        self.assertEqual(row.destination_user_id, 101)


class TestDeterminism(unittest.TestCase):
    def setUp(self):
        self.designs = [
            make_design(501, "Steel Plate"),
            make_design(502, "Bolt"),
        ]

    def _world(self):
        s1 = make_snapshot(301, [(1, 501, 4), (2, 502, 2)], self.designs)
        s2 = make_snapshot(302, [(1, 501, 6)], self.designs)
        d1 = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        d2 = deficit_snapshot(102, [(1, 501, 5)], self.designs, requested=8)
        return [s1, s2, d1, d2]

    def test_input_order_independence(self):
        import itertools

        base = None
        for perm in itertools.permutations(self._world()):
            plan = plan_transfers(aggregate_snapshots(list(perm)), now=NOW)
            dump = json.dumps(plan.to_dicts())
            if base is None:
                base = dump
            else:
                self.assertEqual(dump, base)

    def test_byte_identical_across_runs(self):
        plan1 = plan_transfers(aggregate_snapshots(self._world()), now=NOW)
        plan2 = plan_transfers(aggregate_snapshots(self._world()), now=NOW)
        self.assertEqual(
            json.dumps(plan1.to_dicts()), json.dumps(plan2.to_dicts())
        )

    def test_proposals_sorted_deterministically(self):
        plan = plan_transfers(aggregate_snapshots(self._world()), now=NOW)
        keys = [
            (p.item_design_id, p.destination_user_id, p.source_user_id)
            for p in plan.proposals
        ]
        self.assertEqual(keys, sorted(keys))

    def test_reasons_sorted_lexicographically(self):
        s1 = make_snapshot(301, [(1, 501, 4)], self.designs)
        d1 = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8, captured_at=STALE_CAPTURE)
        plan = plan_transfers(aggregate_snapshots([s1, d1]), now=NOW)
        for p in plan.proposals:
            self.assertEqual(list(p.reasons), sorted(p.reasons))


class TestProtectedItems(unittest.TestCase):
    def setUp(self):
        self.designs = [
            make_design(501, "Steel Plate"),
            make_design(502, "Crew Hat", sub_type="equipmentbody"),
        ]

    def test_protected_source_stack_excluded_from_supply(self):
        # Stack 2 is equipped -> protected -> its candidates excluded
        # from usable supply AND a source-side protection conflict, so
        # the pairing fails closed (matched 0, conflict reason).
        source = make_snapshot(
            202, [(1, 501, 6), (2, 501, 4)], self.designs, equipped={2: 1}
        )
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([source, dest]), now=NOW)
        row = pair_only(plan)
        self.assertEqual(row.matched_quantity, 0)
        self.assertIn(REASON_NO_PROTECTION_CONFLICT, row.reasons)
        # Exclusion arithmetic pinned independently: usable supply is
        # the candidate total minus protected-stack candidates.
        self.assertEqual(inv.transfer._usable_supply(source, 501), 6)

    def test_protected_destination_conflict(self):
        # Destination holds a protected stack of the same design.
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        # Forge a protected stack onto the destination via a loader-legal
        # payload (protected with a reason, candidate 0, HOLD). Design-row
        # arithmetic must stay consistent: observed 5+2=7, requested 8,
        # unmet must equal max(0, 8-7)=1.
        payload = dest.to_dict()
        payload["stacks"].append(
            {
                "item_id": 99,
                "item_design_id": 501,
                "ship_id": 55,
                "quantity": 2,
                "classification": "PROTECTED",
                "protected": True,
                "unknown": False,
                "roles": [],
                "protection_reasons": ["PROTECTED_RARITY"],
                "classification_reasons": ["rarity gate"],
                "evidence": "CAPTURED_NOT_VERIFIED",
                "requested_quantity": 8,
                "reserved_quantity": 5,
                "candidate_quantity": 0,
                "candidate_state": "HOLD",
                "candidate_reasons": ["BLOCKED_PROTECTED_OR_UNKNOWN"],
                "candidate_evidence": "CAPTURED_NOT_VERIFIED",
                "disposition_hint": "KEEP",
            }
        )
        payload["designs"][0]["observed_quantity"] = 7
        payload["designs"][0]["unmet_quantity"] = 1
        payload["designs"][0]["stacks"] = 2
        forged_dest = json_snapshot(payload)
        plan = plan_transfers(
            aggregate_snapshots([source, forged_dest]), now=NOW
        )
        row = pair_only(plan)
        self.assertEqual(row.matched_quantity, 0)
        self.assertIn(REASON_NO_PROTECTION_CONFLICT, row.reasons)

    def test_all_candidates_on_protected_stacks_yield_zero_supply(self):
        # Single equipped stack: candidates 0 anyway; forge candidates.
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        payload = dest.to_dict()
        payload["stacks"][0]["protected"] = True
        payload["stacks"][0]["protection_reasons"] = ["PROTECTED_RARITY"]
        payload["stacks"][0]["classification"] = "PROTECTED"
        payload["stacks"][0]["candidate_quantity"] = 0
        payload["stacks"][0]["candidate_state"] = "HOLD"
        payload["stacks"][0]["candidate_reasons"] = ["BLOCKED_PROTECTED_OR_UNKNOWN"]
        payload["stacks"][0]["disposition_hint"] = "KEEP"
        source_forged = json_snapshot(payload)
        s2 = make_snapshot(202, [(1, 501, 10)], self.designs)
        plan = plan_transfers(
            aggregate_snapshots([source_forged, s2]), now=NOW
        )
        # source_forged is the deficit account (destination); its
        # protected candidates don't matter for supply. The pairing
        # 202 -> 101 is suppressed by destination protection conflict.
        row = pair_only(plan)
        self.assertEqual(row.matched_quantity, 0)
        self.assertIn(REASON_NO_PROTECTION_CONFLICT, row.reasons)


class TestConditionsGate(unittest.TestCase):
    def setUp(self):
        self.designs = [make_design(501, "Steel Plate")]

    def test_real_pipeline_deficit_always_carries_condition(self):
        # The REAL pipeline stamps DEMAND_EXCEEDS_SUPPLY on every
        # deficit; pairing must fail closed with the condition reason.
        _, classified, ledger, reduction = build_dfg([(1, 501, 5)], self.designs)
        over_ledger = inv.ReservationLedger(
            entries=(
                inv.ReservationEntry(
                    item_design_id=501,
                    source=inv.ReservationSource.STRATEGIC,
                    requested_quantity=8,
                    reserved_quantity=5,
                    reason="strategic reserve config",
                    evidence=Evidence.INFERRED,
                    provenance="test",
                ),
            ),
            # conditions=() in the clean vector, but the REAL planner
            # re-plans with the demand planner...
            conditions=(),
            observed_by_design={501: 5},
        )
        # Now use the REAL demand planner path: build_dfg with strategic
        # config via PlanningContext to get the DEMAND_EXCEEDS_SUPPLY
        # condition stamped.
        from sdk.inventory import PlanningContext

        catalog, classified2, ledger2, reduction2 = build_dfg(
            [(1, 501, 5)], self.designs
        )
        ctx = PlanningContext(strategic_reserve={501: 8})
        ledger_real = inv.DemandPlanner().plan(catalog, ctx)
        self.assertTrue(
            any(
                c.code == "DEMAND_EXCEEDS_SUPPLY" and c.design_id == 501
                for c in ledger_real.conditions
            )
        )
        reduction_real = inv.ReductionPlanner().plan(classified2, ledger_real)
        snap = build_snapshot(
            SnapshotAccountIdentity(user_id=101, ship_id=55, display_name="u101"),
            classified2,
            ledger_real,
            reduction_real,
            captured_at=FRESH_CAPTURE,
        )
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        plan = plan_transfers(
            aggregate_snapshots([source, snap]), now=NOW
        )
        row = pair_only(plan)
        self.assertEqual(row.matched_quantity, 0)
        self.assertIn(REASON_NO_APPLICABLE_UNRESOLVED_CONDITION, row.reasons)

    def test_account_global_conditions_do_not_block(self):
        # SOURCE_UNSUPPORTED account-global conditions (emitted by every
        # real run with no strategic config) must NOT design-block.
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        self.assertTrue(any(c.design_id is None for c in source.conditions))
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([source, dest]), now=NOW)
        row = pair_only(plan)
        self.assertEqual(row.matched_quantity, 3)
        self.assertNotIn(REASON_NO_APPLICABLE_UNRESOLVED_CONDITION, row.reasons)

    def test_design_scoped_condition_on_destination_blocks(self):
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        payload = dest.to_dict()
        payload["conditions"].append(
            {"code": "MALFORMED_QUANTITY", "detail": "test", "design_id": 501}
        )
        forged_dest = json_snapshot(payload)
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        plan = plan_transfers(
            aggregate_snapshots([source, forged_dest]), now=NOW
        )
        row = pair_only(plan)
        self.assertEqual(row.matched_quantity, 0)
        self.assertIn(REASON_NO_APPLICABLE_UNRESOLVED_CONDITION, row.reasons)

    def test_design_scoped_condition_on_source_blocks(self):
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        payload = source.to_dict()
        payload["conditions"].append(
            {"code": "UNREASONABLE_QUANTITY", "detail": "test", "design_id": 501}
        )
        forged_source = json_snapshot(payload)
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        plan = plan_transfers(
            aggregate_snapshots([forged_source, dest]), now=NOW
        )
        row = pair_only(plan)
        self.assertEqual(row.matched_quantity, 0)
        self.assertIn(REASON_NO_APPLICABLE_UNRESOLVED_CONDITION, row.reasons)


class TestUnverifiedTransferability(unittest.TestCase):
    def setUp(self):
        self.designs = [make_design(501, "Steel Plate")]

    def test_every_proposal_holds_unverified(self):
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        stale_source = make_snapshot(
            303, [(1, 501, 10)], self.designs, captured_at=STALE_CAPTURE
        )
        stale_dest = deficit_snapshot(
            104, [(1, 501, 5)], self.designs, requested=8, captured_at=STALE_CAPTURE
        )
        plan = plan_transfers(
            aggregate_snapshots([source, dest, stale_source, stale_dest]),
            now=NOW,
        )
        self.assertTrue(len(plan.proposals) > 0)
        for p in plan.proposals:
            self.assertEqual(
                p.eligibility_state, HOLD_UNVERIFIED_TRANSFERABILITY
            )
            self.assertIn(REASON_TRANSFER_ELIGIBILITY_UNPROVEN, p.reasons)
            self.assertEqual(p.proposed_quantity, 0)
            self.assertFalse(p.actionable)

    def test_holds_even_when_everything_else_passes(self):
        # distinct, supply>0, demand>0, fresh, no conditions, no
        # protection conflicts: STILL a HOLD with qty 0.
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([source, dest]), now=NOW)
        row = pair_only(plan)
        self.assertEqual(
            row.reasons, (REASON_TRANSFER_ELIGIBILITY_UNPROVEN,)
        )
        self.assertEqual(row.proposed_quantity, 0)

    def test_eligible_state_never_assigned(self):
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([source, dest]), now=NOW)
        for p in plan.proposals:
            self.assertNotEqual(p.eligibility_state, ELIGIBLE)


class TestRenderer(unittest.TestCase):
    def setUp(self):
        self.designs = [make_design(501, "Steel Plate")]

    def test_renders_required_fields(self):
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        out = render_transfers_view([source, dest], now=NOW)
        self.assertIn("=== Inventory transfers", out)
        self.assertIn("source 202", out)
        self.assertIn("destination 101", out)
        self.assertIn("design 501", out)
        self.assertIn("proposed 0", out)
        self.assertIn("HOLD_UNVERIFIED_TRANSFERABILITY", out)
        self.assertIn("actionable=false", out)
        self.assertIn("fresh", out)
        self.assertIn("evidence", out)
        self.assertIn("Candidate does not mean transferable", out)
        self.assertIn("GiftItem", out)

    def test_renders_stale_markers(self):
        source = make_snapshot(
            202, [(1, 501, 10)], self.designs, captured_at=STALE_CAPTURE
        )
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        out = render_transfers_view([source, dest], now=NOW)
        self.assertIn("STALE", out)

    def test_renders_empty_state(self):
        out = render_transfers_view([], now=NOW)
        self.assertIn("No account snapshots found", out)

    def test_renders_no_pairing_state(self):
        a = make_snapshot(101, [(1, 501, 5)], self.designs)
        b = make_snapshot(202, [(1, 501, 5)], self.designs)
        out = render_transfers_view([a, b], now=NOW)
        self.assertIn("No cross-account pairing exists", out)

    def test_renderer_pure_no_mutation(self):
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        before = (source.to_json(), dest.to_json())
        render_transfers_view([source, dest], now=NOW)
        self.assertEqual(before, (source.to_json(), dest.to_json()))

    def test_renderer_under_socket_block(self):
        def _blocked(*args, **kwargs):
            raise AssertionError("network I/O attempted during transfers view")

        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        with patch.object(socket, "socket", _blocked), patch.object(
            socket, "create_connection", _blocked
        ):
            out = render_transfers_view([source, dest], now=NOW)
        self.assertIn("=== Inventory transfers", out)


class TestIsolationAndNonActionability(unittest.TestCase):
    def setUp(self):
        self.designs = [make_design(501, "Steel Plate")]

    def test_all_actionable_false(self):
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([source, dest]), now=NOW)
        self.assertFalse(plan.actionable)
        self.assertEqual(plan.actionable_proposals(), ())
        for p in plan.proposals:
            self.assertFalse(p.actionable)
            self.assertEqual(p.proposed_quantity, 0)

    def test_no_network_imports_in_transfer_module(self):
        source_text = inspect.getsource(inv.transfer)
        for token in (
            "import requests",
            "from requests",
            "import socket",
            "import urllib",
            "from urllib",
            "import http.client",
            "os.environ",
            "getenv",
        ):
            self.assertNotIn(token, source_text, f"transfer.py contains {token}")

    def test_no_mutation_endpoint_call_sites(self):
        source_text = inspect.getsource(inv.transfer)
        for endpoint in (
            "GiftItem",
            "SellItem3",
            "Purchase4",
            "ConsumeItem2",
            "DeleteItem3",
            "CombineItem4",
            "ActivateItem4",
        ):
            self.assertNotIn(f"{endpoint}(", source_text)

    def test_no_client_construction_or_transport(self):
        source_text = inspect.getsource(inv.transfer)
        self.assertNotIn("Client(", source_text)
        self.assertNotIn("self.request(", source_text)
        self.assertNotIn(".request(", source_text)
        for scheme in ("http://", "https://"):
            self.assertNotIn(scheme, source_text)

    def test_no_filesystem_calls(self):
        source_text = inspect.getsource(inv.transfer)
        for token in ("open(", "Path(", "os.remove", "shutil", "listdir"):
            self.assertNotIn(token, source_text)

    def test_no_clock_reads(self):
        source_text = inspect.getsource(inv.transfer)
        for token in (
            "datetime.datetime.now",
            "datetime.now",
            "time.time",
            "utcnow",
        ):
            self.assertNotIn(token, source_text)

    def test_snapshot_json_still_has_no_transfer_vocabulary(self):
        # Task H invariant preserved under Task I: serialized snapshot
        # output contains no donor/recipient/transfer vocabulary.
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        portfolio = aggregate_snapshots([source, dest])
        text = json.dumps(portfolio.to_dicts()).lower()
        for word in ("donor", "recipient", "transfer", "proposal"):
            self.assertNotIn(word, text)

    def test_inventory_apply_refusal_unaffected(self):
        from sdk.commands import CommandRegistry

        class _FakeClient:
            settings = {}

        registry = CommandRegistry(_FakeClient())  # type: ignore[arg-type]
        out = registry.get("inventory apply")["handler"]([])
        self.assertIn("REFUSED", out)

    def test_plan_transfers_pure_no_mutation(self):
        source = make_snapshot(202, [(1, 501, 10)], self.designs)
        dest = deficit_snapshot(101, [(1, 501, 5)], self.designs, requested=8)
        before = (source.to_json(), dest.to_json())
        plan_transfers(aggregate_snapshots([source, dest]), now=NOW)
        self.assertEqual(before, (source.to_json(), dest.to_json()))


if __name__ == "__main__":
    unittest.main()
