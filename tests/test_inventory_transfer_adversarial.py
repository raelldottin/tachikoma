"""Adversarial probe harness (lane t_628172e7) — Task I transfer planner.

Independent from tests/test_inventory_transfer.py: this builds a
from-scratch reference allocator (written from the TASK SPEC, not from
transfer.py's code) and differential-fuzzes plan_transfers against it,
plus targeted structural attacks (gate bypasses, provenance,
determinism, isolation).
"""
from __future__ import annotations

import random
import unittest
from unittest.mock import patch

import sdk.inventory as inv
from sdk.inventory import (  # noqa: E402
    Evidence,
    SnapshotAccountIdentity,
    aggregate_snapshots,
    build_snapshot,
)
from sdk.inventory.transfer import (  # noqa: E402
    ELIGIBLE,
    HOLD_UNVERIFIED_TRANSFERABILITY,
    REASON_DISTINCT_ACCOUNTS,
    REASON_FRESH_SNAPSHOTS,
    REASON_NO_APPLICABLE_UNRESOLVED_CONDITION,
    REASON_NO_PROTECTION_CONFLICT,
    REASON_POSITIVE_DESTINATION_DEMAND,
    REASON_POSITIVE_SOURCE_SUPPLY,
    REASON_TRANSFER_ELIGIBILITY_UNPROVEN,
    TransferPlannerError,
    plan_transfers,
    render_transfers_view,
)
from tests.test_inventory_snapshot import build_dfg, make_design  # noqa: E402

NOW = "2026-10-01T00:00:00Z"
FRESH = "2026-09-30T00:00:00Z"
STALE = "2026-09-01T00:00:00Z"


def make_snapshot(user_id, rows, designs, captured_at=FRESH):
    """Real D/F/G pipeline snapshot: rows = [(item_id, design_id, qty)]."""
    _, classified, ledger, reduction = build_dfg(rows, designs)
    identity = SnapshotAccountIdentity(
        user_id=user_id, ship_id=55, display_name=f"user{user_id}"
    )
    return build_snapshot(identity, classified, ledger, reduction, captured_at=captured_at)


def deficit_snapshot(user_id, rows, designs, requested=8, reserved=5, captured_at=FRESH):
    """Account whose ledger claims more than it holds (unmet > 0), with a
    clean condition list (no design-scoped stamp). reserved is clamped
    to observed per design: the Task H loader fail-closes reserved >
    observed, so loader-legal vectors never exceed it."""
    _, classified, ledger, reduction = build_dfg(rows, designs)
    observed_by_design = {}
    for _iid, did, qty in rows:
        observed_by_design[did] = observed_by_design.get(did, 0) + qty
    over = inv.ReservationLedger(
        entries=tuple(
            inv.ReservationEntry(
                item_design_id=did,
                source=inv.ReservationSource.STRATEGIC,
                requested_quantity=requested,
                reserved_quantity=min(reserved, observed_by_design[did]),
                reason="strategic reserve config",
                evidence=Evidence.INFERRED,
                provenance="probe",
            )
            for did in sorted(observed_by_design)
        ),
        conditions=(),
        observed_by_design=observed_by_design,
    )
    red = inv.ReductionPlanner().plan(classified, over)
    identity = SnapshotAccountIdentity(
        user_id=user_id, ship_id=55, display_name=f"user{user_id}"
    )
    return build_snapshot(identity, classified, over, red, captured_at=captured_at)


def make_identity(user_id):
    return SnapshotAccountIdentity(
        user_id=user_id, ship_id=55, display_name=f"user{user_id}"
    )


# ---------------------------------------------------------------------------
# REFERENCE ALLOCATOR — from the Task I spec, NOT from transfer.py.
# ---------------------------------------------------------------------------

def ref_conditions_for(snap, did):
    return {c.code for c in snap.conditions_for(did)}


def ref_usable_supply(snap, did):
    total = 0
    excluded = 0
    for s in snap.stacks_for_design(did):
        total += s.candidate_quantity
        if s.protected or s.unknown:
            excluded += s.candidate_quantity
    return total - excluded


def ref_fresh(snap, now, stale_after=86400):
    import datetime

    fmt = "%Y-%m-%dT%H:%M:%SZ"
    cap = datetime.datetime.strptime(snap.captured_at, fmt).replace(
        tzinfo=datetime.timezone.utc
    ).timestamp()
    cur = datetime.datetime.strptime(now, fmt).replace(
        tzinfo=datetime.timezone.utc
    ).timestamp()
    if cur - cap < 0:
        raise ValueError("future captured_at")
    return (cur - cap) <= stale_after


def ref_plan(portfolio, now, stale_after=86400):
    snaps = sorted(portfolio.accounts, key=lambda s: s.user_id)
    fresh = {s.user_id: ref_fresh(s, now, stale_after) for s in snaps}
    supply = {}
    deficit = {}
    for s in snaps:
        for d in s.designs:
            supply[(s.user_id, d.item_design_id)] = ref_usable_supply(s, d.item_design_id)
            deficit[(s.user_id, d.item_design_id)] = max(0, d.unmet_quantity)
    design_ids = sorted({d.item_design_id for s in snaps for d in s.designs})
    rows = []
    for did in design_ids:
        dests = [s for s in snaps if deficit.get((s.user_id, did), 0) > 0]
        sources = [
            s
            for s in snaps
            if deficit.get((s.user_id, did), 0) <= 0 and s.stacks_for_design(did)
        ]
        if not dests or not sources:
            continue
        rem_supply = {s.user_id: supply[(s.user_id, did)] for s in sources}
        rem_deficit = {s.user_id: deficit[(s.user_id, did)] for s in dests}
        for dest in sorted(dests, key=lambda s: s.user_id):
            for src in sorted(sources, key=lambda s: s.user_id):
                reasons = set()
                if src.user_id == dest.user_id:
                    reasons.add(REASON_DISTINCT_ACCOUNTS)
                if rem_supply[src.user_id] <= 0:
                    reasons.add(REASON_POSITIVE_SOURCE_SUPPLY)
                if rem_deficit[dest.user_id] <= 0:
                    reasons.add(REASON_POSITIVE_DESTINATION_DEMAND)
                if not fresh[src.user_id] or not fresh[dest.user_id]:
                    reasons.add(REASON_FRESH_SNAPSHOTS)
                if ref_conditions_for(src, did) or ref_conditions_for(dest, did):
                    reasons.add(REASON_NO_APPLICABLE_UNRESOLVED_CONDITION)
                if any(st.protected or st.unknown for st in src.stacks_for_design(did)):
                    reasons.add(REASON_NO_PROTECTION_CONFLICT)
                if any(st.protected or st.unknown for st in dest.stacks_for_design(did)):
                    reasons.add(REASON_NO_PROTECTION_CONFLICT)
                structural = reasons
                matched = 0
                if not structural:
                    matched = min(rem_supply[src.user_id], rem_deficit[dest.user_id])
                    rem_supply[src.user_id] -= matched
                    rem_deficit[dest.user_id] -= matched
                rows.append(
                    {
                        "item_design_id": did,
                        "source_user_id": src.user_id,
                        "destination_user_id": dest.user_id,
                        "matched": matched,
                        "structural_reasons": sorted(structural),
                    }
                )
    return rows


def normalize(plan):
    out = []
    for p in plan.proposals:
        structural = [r for r in p.reasons if r != REASON_TRANSFER_ELIGIBILITY_UNPROVEN]
        out.append(
            {
                "item_design_id": p.item_design_id,
                "source_user_id": p.source_user_id,
                "destination_user_id": p.destination_user_id,
                "matched": p.matched_quantity,
                "structural_reasons": sorted(structural),
            }
        )
    return out


class TestReferenceDifferential(unittest.TestCase):
    """Differential fuzz: plan_transfers vs the independent reference."""

    def assert_matches_reference(self, snaps, now=NOW):
        portfolio = aggregate_snapshots(snaps)
        got = normalize(plan_transfers(portfolio, now=now))
        want = ref_plan(portfolio, now)
        self.assertEqual(
            got,
            want,
            f"divergence from reference allocator\n got: {got}\nwant: {want}",
        )
        return got

    def test_ref_matches_basic_positive_pairing(self):
        designs = [make_design(501, "Steel Plate")]
        src = make_snapshot(202, [(1, 501, 10)], designs)
        dst = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8)
        rows = self.assert_matches_reference([src, dst])
        self.assertEqual(rows[0]["matched"], 3)

    def test_ref_matches_stale_pair(self):
        designs = [make_design(501, "Steel Plate")]
        src = make_snapshot(202, [(1, 501, 10)], designs, captured_at=STALE)
        dst = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8)
        self.assert_matches_reference([src, dst])

    def test_ref_randomized_60_trials(self):
        rng = random.Random(20261001)
        designs = [make_design(501, "A"), make_design(502, "B"), make_design(503, "C")]
        for trial in range(60):
            snaps = []
            uid = 100
            for _acct in range(rng.randint(2, 6)):
                uid += rng.randint(1, 20)
                rows = [
                    (iid, rng.choice([501, 502, 503]), rng.randint(1, 9))
                    for iid in range(1, rng.randint(1, 4))
                ]
                stale = rng.random() < 0.25
                cap = STALE if stale else FRESH
                if rng.random() < 0.5:
                    snaps.append(
                        deficit_snapshot(
                            uid,
                            rows,
                            designs,
                            requested=rng.randint(6, 12),
                            reserved=3,
                            captured_at=cap,
                        )
                    )
                else:
                    snaps.append(make_snapshot(uid, rows, designs, captured_at=cap))
            with self.subTest(trial=trial):
                self.assert_matches_reference(snaps)

    def test_ref_matches_protected_mix(self):
        designs = [make_design(501, "A")]
        _, classified, ledger, reduction = build_dfg(
            [(1, 501, 10), (2, 501, 3)], designs, equipped={1: 1}
        )
        src = build_snapshot(
            make_identity(202), classified, ledger, reduction, captured_at=FRESH
        )
        dst = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8)
        rows = self.assert_matches_reference([src, dst])
        # Source holds a protected stack of 501 (the equipped one). The
        # planner reads "no protection conflict" FAIL-CLOSED: ANY
        # protected/unknown stack of the design on either side suppresses
        # the pairing, even though usable supply already excludes the
        # protected candidates (belt and braces). matched=0, conflict
        # reason stamped. Conservative direction; verified consistent with
        # the planner lane's own pin (test_protected_source_stack_excluded_
        # from_supply).
        self.assertEqual(rows[0]["matched"], 0)
        self.assertIn(
            REASON_NO_PROTECTION_CONFLICT, rows[0]["structural_reasons"]
        )

    def test_ref_blocked_pair_consumes_no_capacity(self):
        designs = [make_design(501, "A")]
        src_fresh = make_snapshot(202, [(1, 501, 4)], designs)
        src_stale = make_snapshot(303, [(1, 501, 10)], designs, captured_at=STALE)
        dst = deficit_snapshot(101, [(1, 501, 3)], designs, requested=8)
        rows = self.assert_matches_reference([src_fresh, src_stale, dst])
        # destinations served ascending user_id; each source in ascending order.
        # src 202 fresh fills 4; stale 303 contributes 0 and eats no capacity.
        total = sum(r["matched"] for r in rows)
        self.assertLessEqual(total, 4)


class TestArithmeticAttacks(unittest.TestCase):
    """Direct attacks on allocation invariants."""

    def _pair(self, src_qty, dst_held, requested):
        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(1, 501, src_qty)], designs)
        dst = deficit_snapshot(101, [(1, 501, dst_held)], designs, requested=requested)
        return plan_transfers(aggregate_snapshots([src, dst]), now=NOW)

    def test_boundary_min_arithmetic(self):
        # supply 7 candidates vs deficit 5 -> matched 5
        self.assertEqual(self._pair(7, 3, 8).proposals[0].matched_quantity, 5)
        # supply 5 vs deficit 7 -> matched 5
        self.assertEqual(self._pair(5, 3, 10).proposals[0].matched_quantity, 5)

    def test_replanning_does_not_mutate_inputs(self):
        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(1, 501, 10)], designs)
        dst = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8)
        portfolio = aggregate_snapshots([src, dst])
        before = [s.to_dict() for s in portfolio.accounts]
        first = plan_transfers(portfolio, now=NOW)
        second = plan_transfers(portfolio, now=NOW)
        self.assertEqual(first.to_dicts(), second.to_dicts())
        self.assertEqual([s.to_dict() for s in portfolio.accounts], before)

    def test_one_source_two_dests_no_oversubscription(self):
        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(1, 501, 10)], designs)
        d1 = deficit_snapshot(101, [(1, 501, 3)], designs, requested=8)
        d2 = deficit_snapshot(102, [(1, 501, 3)], designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([src, d1, d2]), now=NOW)
        served = sum(p.matched_quantity for p in plan.proposals)
        self.assertEqual(served, 10)
        per_dest = {}
        for p in plan.proposals:
            per_dest[p.destination_user_id] = (
                per_dest.get(p.destination_user_id, 0) + p.matched_quantity
            )
        self.assertEqual(per_dest, {101: 5, 102: 5})

    def test_two_sources_one_dest_no_double_fill(self):
        designs = [make_design(501, "A")]
        s1 = make_snapshot(201, [(1, 501, 10)], designs)
        s2 = make_snapshot(202, [(1, 501, 10)], designs)
        dst = deficit_snapshot(101, [(1, 501, 3)], designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([s1, s2, dst]), now=NOW)
        received = sum(p.matched_quantity for p in plan.proposals)
        self.assertEqual(received, 5)
        for p in plan.proposals:
            if p.source_user_id == 202:
                self.assertEqual(p.matched_quantity, 0)

    def test_deficit_account_never_a_source_for_same_design(self):
        # Account 101 has deficit on 501 but holds supply on 501 too; it must
        # not appear as a source for design 501 anywhere in the plan.
        designs = [make_design(501, "A")]
        mixed = deficit_snapshot(101, [(1, 501, 20)], designs, requested=30)
        dst2 = deficit_snapshot(102, [(1, 501, 1)], designs, requested=9)
        plan = plan_transfers(aggregate_snapshots([mixed, dst2]), now=NOW)
        for p in plan.proposals:
            if p.item_design_id == 501:
                self.assertNotEqual(p.source_user_id, 101)
        # and no proposal pairs 101 with itself
        for p in plan.proposals:
            self.assertNotEqual(p.source_user_id, p.destination_user_id)

    def test_multi_design_partition_independent(self):
        # Account 101 deficit on 502 but supplier of 501: it may source 501.
        designs = [make_design(501, "A"), make_design(502, "B")]
        mixed = deficit_snapshot(101, [(1, 501, 20), (2, 502, 1)], designs, requested=9)
        dst2 = deficit_snapshot(102, [(1, 502, 1)], designs, requested=9)
        src3 = make_snapshot(103, [(1, 501, 0)] if False else [(1, 501, 7)], designs)
        plan = plan_transfers(aggregate_snapshots([mixed, dst2, src3]), now=NOW)
        for p in plan.proposals:
            if p.item_design_id == 501:
                self.assertNotEqual(p.source_user_id, 101)
            if p.item_design_id == 502:
                self.assertNotEqual(p.source_user_id, 102)


class TestGateBypasses(unittest.TestCase):
    """Attack the 8-requirement gate for bypass vectors."""

    def test_every_proposal_carries_constant_hold(self):
        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(1, 501, 10)], designs)
        dst = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([src, dst]), now=NOW)
        self.assertTrue(len(plan.proposals) >= 1)
        for p in plan.proposals:
            self.assertEqual(p.eligibility_state, HOLD_UNVERIFIED_TRANSFERABILITY)
            self.assertIn(REASON_TRANSFER_ELIGIBILITY_UNPROVEN, p.reasons)
            self.assertEqual(p.proposed_quantity, 0)
            self.assertFalse(p.actionable)
        self.assertFalse(plan.actionable)
        self.assertEqual(plan.actionable_proposals(), ())

    def test_eligible_state_unreachable(self):
        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(1, 501, 10)], designs)
        dst = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([src, dst]), now=NOW)
        for p in plan.proposals:
            self.assertNotEqual(p.eligibility_state, ELIGIBLE)

    def test_perfect_inputs_still_hold(self):
        # All gate requirements 1-6 satisfied; still HOLD with only the
        # constant reason. Transferability can never be established.
        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(1, 501, 10)], designs)
        dst = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([src, dst]), now=NOW)
        p = plan.proposals[0]
        self.assertEqual(
            p.reasons,
            (REASON_TRANSFER_ELIGIBILITY_UNPROVEN,),
        )
        self.assertEqual(p.matched_quantity, 3)  # arithmetic still reported

    def test_stale_destination_blocks_even_if_source_fresh(self):
        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(1, 501, 10)], designs)
        dst = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8, captured_at=STALE)
        plan = plan_transfers(aggregate_snapshots([src, dst]), now=NOW)
        p = plan.proposals[0]
        self.assertEqual(p.matched_quantity, 0)
        self.assertIn(REASON_FRESH_SNAPSHOTS, p.reasons)
        self.assertFalse(p.destination_fresh)
        self.assertTrue(p.source_fresh)

    def test_bad_stale_after_rejected(self):
        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(1, 501, 10)], designs)
        dst = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8)
        portfolio = aggregate_snapshots([src, dst])
        for bad in (-1, True, "86400", 86400.0):
            with self.assertRaises(TransferPlannerError):
                plan_transfers(portfolio, now=NOW, stale_after=bad)

    def test_non_portfolio_rejected(self):
        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(1, 501, 10)], designs)
        with self.assertRaises(TransferPlannerError):
            plan_transfers([src], now=NOW)
        with self.assertRaises(TransferPlannerError):
            plan_transfers(object(), now=NOW)

    def test_empty_portfolio_empty_plan(self):
        plan = plan_transfers(aggregate_snapshots([]), now=NOW)
        self.assertEqual(plan.proposals, ())
        self.assertFalse(plan.actionable)


class TestDeterminism(unittest.TestCase):
    """Input-order independence + byte stability, attacked directly."""

    def _random_portfolio(self, seed):
        rng = random.Random(seed)
        designs = [make_design(501, "A"), make_design(502, "B")]
        snaps = []
        uid = 100
        for _ in range(rng.randint(2, 7)):
            uid += rng.randint(1, 15)
            rows = [
                (iid, rng.choice([501, 502]), rng.randint(1, 8))
                for iid in range(1, rng.randint(1, 4))
            ]
            if rng.random() < 0.5:
                snaps.append(
                    deficit_snapshot(uid, rows, designs, requested=rng.randint(5, 12))
                )
            else:
                snaps.append(make_snapshot(uid, rows, designs))
        return snaps

    def test_permutation_invariance_20_seeds(self):
        import itertools

        for seed in range(20):
            snaps = self._random_portfolio(seed)
            base = None
            for perm in itertools.islice(itertools.permutations(snaps), 6):
                portfolio = aggregate_snapshots(list(perm))
                dumped = [p.to_dict() for p in plan_transfers(portfolio, now=NOW).proposals]
                if base is None:
                    base = dumped
                else:
                    self.assertEqual(dumped, base, f"seed={seed} order changed output")

    def test_byte_identical_json(self):
        for seed in range(10):
            snaps = self._random_portfolio(seed)
            p1 = plan_transfers(aggregate_snapshots(list(snaps)), now=NOW)
            p2 = plan_transfers(aggregate_snapshots(list(reversed(snaps))), now=NOW)
            self.assertEqual(
                [p.to_dict() for p in p1.proposals],
                [p.to_dict() for p in p2.proposals],
            )

    def test_reason_ordering_sorted(self):
        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(1, 501, 10)], designs, captured_at=STALE)
        dst = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8, captured_at=STALE)
        plan = plan_transfers(aggregate_snapshots([src, dst]), now=NOW)
        p = plan.proposals[0]
        self.assertEqual(list(p.reasons), sorted(p.reasons))
        self.assertIn(REASON_FRESH_SNAPSHOTS, p.reasons)
        self.assertIn(REASON_TRANSFER_ELIGIBILITY_UNPROVEN, p.reasons)

    def test_proposal_sort_key(self):
        # Multiple designs x destinations x sources: sorted by
        # (design, destination, source).
        designs = [make_design(501, "A"), make_design(502, "B")]
        s1 = make_snapshot(201, [(1, 501, 5), (2, 502, 5)], designs)
        s2 = make_snapshot(202, [(1, 501, 5), (2, 502, 5)], designs)
        d1 = deficit_snapshot(101, [(1, 501, 1), (2, 502, 1)], designs, requested=6)
        d2 = deficit_snapshot(102, [(1, 501, 1), (2, 502, 1)], designs, requested=6)
        plan = plan_transfers(aggregate_snapshots([s1, s2, d1, d2]), now=NOW)
        keys = [
            (p.item_design_id, p.destination_user_id, p.source_user_id)
            for p in plan.proposals
        ]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), len(set(keys)))


class TestProvenanceAndJoins(unittest.TestCase):
    """ItemId must never act as cross-account identity; no provenance bleed."""

    def test_identical_item_ids_across_accounts_do_not_conflate(self):
        # Both accounts use ItemId 1 for DIFFERENT designs; join must be by
        # design only.
        designs = [make_design(501, "A"), make_design(502, "B")]
        src = make_snapshot(202, [(1, 501, 10)], designs)
        dst = deficit_snapshot(101, [(1, 502, 5)], designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([src, dst]), now=NOW)
        self.assertEqual(plan.proposals, ())

    def test_no_itemid_in_serialized_output(self):
        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(7, 501, 10)], designs)
        dst = deficit_snapshot(101, [(9, 501, 5)], designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([src, dst]), now=NOW)
        for p in plan.proposals:
            d = p.to_dict()
            for key in d:
                self.assertNotIn("item_id", key)
            self.assertNotIn(7, d.values())
            self.assertNotIn(9, d.values())

    def test_evidence_never_promoted_and_giftitem_floor(self):
        from sdk.inventory.transfer import GIFTITEM_EVIDENCE_TIER

        self.assertEqual(GIFTITEM_EVIDENCE_TIER, Evidence.STATICALLY_DISCOVERED)
        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(1, 501, 10)], designs)
        dst = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([src, dst]), now=NOW)
        for p in plan.proposals:
            # weakest tier: STATICALLY_DISCOVERED or weaker only
            order = [
                "VERIFIED",
                "CAPTURED_NOT_VERIFIED",
                "CAPTURED_AMBIGUOUS",
                "STATICALLY_DISCOVERED",
                "INFERRED",
                "UNKNOWN",
            ]
            self.assertGreaterEqual(
                order.index(p.evidence), order.index("STATICALLY_DISCOVERED")
            )

    def test_evidence_scoped_to_design(self):
        # A weak design on the source account must not bleed into a proposal
        # for a different design.
        designs = [make_design(501, "A"), make_design(502, "B")]
        src = make_snapshot(202, [(1, 501, 10), (2, 502, 4)], designs)
        dst = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8)
        plan = plan_transfers(aggregate_snapshots([src, dst]), now=NOW)
        for p in plan.proposals:
            self.assertEqual(p.item_design_id, 501)


class TestIsolation(unittest.TestCase):
    """Network / mutation / filesystem / clock isolation probes."""

    def test_no_network_capability_at_runtime(self):
        import socket as sock_mod

        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(1, 501, 10)], designs)
        dst = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8)
        portfolio = aggregate_snapshots([src, dst])

        def blocked(*args, **kwargs):
            raise AssertionError("network reachability attempted")

        with patch.object(sock_mod, "create_connection", blocked), patch.object(
            sock_mod, "socket", blocked
        ):
            plan = plan_transfers(portfolio, now=NOW)
            self.assertTrue(len(plan.proposals) >= 1)

    def test_no_mutation_endpoints_reachable(self):
        # plan_transfers must not construct a Client or call any endpoint.
        import sdk.client as client_mod

        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(1, 501, 10)], designs)
        dst = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8)
        with patch.object(client_mod.Client, "__init__", side_effect=AssertionError("client constructed")):
            plan_transfers(aggregate_snapshots([src, dst]), now=NOW)

    def test_client_never_imports_inventory(self):
        # The boundary invariant is the absence of an IMPORT, not the
        # substring (docstrings may legitimately mention inventory).
        import ast
        import inspect

        import sdk.client as client_mod

        tree = ast.parse(inspect.getsource(client_mod))
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_names.add(node.module)
        for name in imported_names:
            self.assertNotIn("inventory", name)

    def test_transfer_module_source_clean(self):
        import inspect

        import sdk.inventory.transfer as tmod

        text = inspect.getsource(tmod)
        for bad in (
            "requests",
            "urllib",
            "socket",
            "GiftItem(",
            "SellItem",
            "Purchase4",
            "os.environ",
            "datetime.now",
            "time.time",
            "open(",
        ):
            self.assertNotIn(bad, text, f"forbidden token {bad!r} in transfer.py")

    def test_render_transfers_view_read_only(self):
        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(1, 501, 10)], designs)
        dst = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8)
        before = [s.to_dict() for s in (src, dst)]
        text = render_transfers_view([src, dst], NOW)
        self.assertIn("actionable=false", text)
        self.assertIn("HOLD_UNVERIFIED_TRANSFERABILITY", text)
        self.assertEqual([s.to_dict() for s in (src, dst)], before)

    def test_render_empty(self):
        text = render_transfers_view([], NOW)
        self.assertIn("No account snapshots found", text)

    def test_render_stale_marker(self):
        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(1, 501, 10)], designs, captured_at=STALE)
        dst = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8)
        text = render_transfers_view([src, dst], NOW)
        self.assertIn("STALE", text)


class TestRealPipelineVectors(unittest.TestCase):
    """The planner lane claims real-pipeline deficits ALWAYS carry
    DEMAND_EXCEEDS_SUPPLY (design-scoped), so real-data pairings fail
    closed to matched 0. Verify against the REAL DemandPlanner."""

    def test_real_pipeline_deficit_stamped_and_blocks(self):
        # Real pipeline: strategic_reserve config requesting more than held.
        designs = [make_design(501, "Steel Plate")]
        catalog, _, _, _ = build_dfg([(1, 501, 5)], designs)
        ctx = inv.PlanningContext(
            strategic_reserve={501: 8},
        )
        ledger = inv.DemandPlanner().plan(catalog, ctx)
        codes = [c.code for c in ledger.conditions if c.design_id == 501]
        self.assertIn("DEMAND_EXCEEDS_SUPPLY", codes)
        # Any snapshot built from this ledger pairs to matched 0.
        _, classified, _ledger2, reduction = build_dfg([(1, 501, 5)], designs)
        red = inv.ReductionPlanner().plan(classified, ledger)
        dst = build_snapshot(
            make_identity(101), classified, ledger, red, captured_at=FRESH
        )
        src = make_snapshot(202, [(1, 501, 10)], designs)
        plan = plan_transfers(aggregate_snapshots([src, dst]), now=NOW)
        for p in plan.proposals:
            self.assertEqual(p.matched_quantity, 0)
            self.assertIn(REASON_NO_APPLICABLE_UNRESOLVED_CONDITION, p.reasons)


class TestStaleBoundaryAndNow(unittest.TestCase):
    """Freshness boundary arithmetic and `now` validation."""

    def _portfolio(self, src_cap, dst_cap):
        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(1, 501, 10)], designs, captured_at=src_cap)
        dst = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8, captured_at=dst_cap)
        return aggregate_snapshots([src, dst])

    def test_boundary_age_zero_fresh_even_at_stale_after_zero(self):
        # captured_at == now -> age 0 -> fresh (stale is age > stale_after).
        portfolio = self._portfolio(NOW, NOW)
        plan = plan_transfers(portfolio, now=NOW, stale_after=0)
        for p in plan.proposals:
            self.assertTrue(p.source_fresh and p.destination_fresh)
            self.assertNotIn(REASON_FRESH_SNAPSHOTS, p.reasons)
        self.assertEqual(plan.proposals[0].matched_quantity, 3)

    def test_boundary_age_one_stale_at_zero(self):
        portfolio = self._portfolio("2026-09-30T23:59:59Z", NOW)
        plan = plan_transfers(portfolio, now=NOW, stale_after=0)
        for p in plan.proposals:
            self.assertFalse(p.source_fresh)
            self.assertEqual(p.matched_quantity, 0)
            self.assertIn(REASON_FRESH_SNAPSHOTS, p.reasons)

    def test_boundary_exactly_86400_is_fresh(self):
        # age == 86400 (not >) is FRESH; 86401 is STALE.
        cap = "2026-09-30T00:00:01Z"  # 86399s before NOW -> fresh at 86400
        portfolio = self._portfolio(cap, FRESH)
        plan = plan_transfers(portfolio, now=NOW)  # default stale_after 86400
        self.assertTrue(all(p.source_fresh for p in plan.proposals))
        cap2 = "2026-09-30T00:00:00Z"  # exactly 86400 -> fresh
        portfolio2 = self._portfolio(cap2, FRESH)
        plan2 = plan_transfers(portfolio2, now=NOW)
        self.assertTrue(all(p.source_fresh for p in plan2.proposals))

    def test_malformed_now_refuses_whole_plan(self):
        portfolio = self._portfolio(FRESH, FRESH)
        for bad_now in ("not-a-time", "2026-10-01 00:00:00", "2026-10-01T00:00:00", 12345):
            with self.assertRaises(TransferPlannerError):
                plan_transfers(portfolio, now=bad_now)

    def test_now_before_capture_refuses(self):
        # now earlier than captured_at -> future snapshot -> fail closed.
        portfolio = self._portfolio(NOW, FRESH)
        with self.assertRaises(TransferPlannerError):
            plan_transfers(portfolio, now="2026-09-01T00:00:00Z")


class TestFullPermutationDeterminism(unittest.TestCase):
    """ALL permutations for small portfolios, not just samples."""

    def test_all_permutations_n4(self):
        import itertools

        designs = [make_design(501, "A"), make_design(502, "B")]
        snaps = [
            make_snapshot(201, [(1, 501, 7)], designs),
            make_snapshot(202, [(1, 501, 2), (2, 502, 9)], designs),
            deficit_snapshot(101, [(1, 501, 1)], designs, requested=6),
            deficit_snapshot(102, [(2, 502, 3)], designs, requested=9),
        ]
        base = None
        for perm in itertools.permutations(snaps):
            dumped = plan_transfers(
                aggregate_snapshots(list(perm)), now=NOW
            ).to_dicts()
            if base is None:
                base = dumped
            else:
                self.assertEqual(dumped, base)


class TestStressAllocationInvariants(unittest.TestCase):
    """300-trial stress fuzz with independent oversubscription /
    double-fill accounting (not differential: direct invariant checks)."""

    def test_stress_no_oversubscription_no_double_fill(self):
        rng = random.Random(7919)
        designs = [make_design(501, "A"), make_design(502, "B"), make_design(503, "C")]
        for trial in range(300):
            snaps = []
            uid = 50
            for _ in range(rng.randint(2, 8)):
                uid += rng.randint(1, 9)
                rows = [
                    (iid, rng.choice([501, 502, 503]), rng.randint(1, 12))
                    for iid in range(1, rng.randint(1, 5))
                ]
                if rng.random() < 0.45:
                    snaps.append(
                        deficit_snapshot(
                            uid, rows, designs, requested=rng.randint(7, 15)
                        )
                    )
                else:
                    snaps.append(make_snapshot(uid, rows, designs))
            portfolio = aggregate_snapshots(snaps)
            plan = plan_transfers(portfolio, now=NOW)

            # Independent capacity accounting from the snapshots themselves.
            true_supply = {}
            true_deficit = {}
            for s in portfolio.accounts:
                for d in s.designs:
                    true_supply[(s.user_id, d.item_design_id)] = sum(
                        st.candidate_quantity
                        for st in s.stacks_for_design(d.item_design_id)
                        if not st.protected and not st.unknown
                    )
                    true_deficit[(s.user_id, d.item_design_id)] = d.unmet_quantity

            given = {}
            received = {}
            for p in plan.proposals:
                given[(p.source_user_id, p.item_design_id)] = (
                    given.get((p.source_user_id, p.item_design_id), 0)
                    + p.matched_quantity
                )
                received[(p.destination_user_id, p.item_design_id)] = (
                    received.get((p.destination_user_id, p.item_design_id), 0)
                    + p.matched_quantity
                )
                # Bounds per row: 0 <= matched <= min(supply, deficit).
                sup = true_supply.get((p.source_user_id, p.item_design_id), 0)
                dem = true_deficit.get((p.destination_user_id, p.item_design_id), 0)
                self.assertLessEqual(p.matched_quantity, min(sup, dem))
                self.assertGreaterEqual(p.matched_quantity, 0)
                # Self-pairing never appears.
                self.assertNotEqual(p.source_user_id, p.destination_user_id)
                # Constant hold on every row.
                self.assertEqual(p.proposed_quantity, 0)
                self.assertFalse(p.actionable)
                self.assertEqual(
                    p.eligibility_state, HOLD_UNVERIFIED_TRANSFERABILITY
                )
            # No oversubscription: given <= usable supply per (source, design).
            for key, qty in given.items():
                self.assertLessEqual(qty, true_supply.get(key, 0))
            # No double-fill: received <= deficit per (dest, design).
            for key, qty in received.items():
                self.assertLessEqual(qty, true_deficit.get(key, 0))
            # An account with deficit on a design never sources that design.
            for p in plan.proposals:
                self.assertLessEqual(
                    true_deficit.get((p.source_user_id, p.item_design_id), 0),
                    0,
                    f"trial {trial}: deficit account {p.source_user_id} "
                    f"sourced design {p.item_design_id}",
                )


class TestItemIdCollisionJoins(unittest.TestCase):
    """ItemId must never act as cross-account identity — collision vectors."""

    def test_same_item_id_same_design_both_accounts(self):
        # Both accounts hold ItemId=1 of design 501 with DIFFERENT
        # quantities. Supply must be each account's own candidates; the
        # shared ItemId must not conflate them.
        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(1, 501, 10)], designs)
        dst = deficit_snapshot(101, [(1, 501, 2)], designs, requested=5)
        plan = plan_transfers(aggregate_snapshots([src, dst]), now=NOW)
        p = plan.proposals[0]
        self.assertEqual(p.source_user_id, 202)
        self.assertEqual(p.destination_user_id, 101)
        # dst holds 2, requested 5 -> unmet 3; src supply 10 -> matched 3.
        self.assertEqual(p.matched_quantity, 3)

    def test_item_id_never_in_rendered_view(self):
        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(777, 501, 10)], designs)
        dst = deficit_snapshot(101, [(777, 501, 5)], designs, requested=8)
        text = render_transfers_view([src, dst], NOW)
        self.assertNotIn("777", text)
        self.assertIn("501", text)  # design id IS in the view


class TestAttemptReporting(unittest.TestCase):
    """Gate-failing pairs are REPORTED (matched 0 with reasons), never
    silently dropped — verify row counts equal the full pairing grid."""

    def test_every_pair_attempted_design(self):
        designs = [make_design(501, "A")]
        s1 = make_snapshot(201, [(1, 501, 5)], designs)
        s2 = make_snapshot(202, [(1, 501, 5)], designs)
        d1 = deficit_snapshot(101, [(1, 501, 1)], designs, requested=4)
        d2 = deficit_snapshot(102, [(1, 501, 1)], designs, requested=4)
        plan = plan_transfers(aggregate_snapshots([s1, s2, d1, d2]), now=NOW)
        # 2 sources x 2 destinations = 4 attempted pairs, all reported.
        keys = {
            (p.source_user_id, p.destination_user_id) for p in plan.proposals
        }
        self.assertEqual(
            keys,
            {(201, 101), (201, 102), (202, 101), (202, 102)},
        )
        # Each row carries the constant hold.
        for p in plan.proposals:
            self.assertIn(REASON_TRANSFER_ELIGIBILITY_UNPROVEN, p.reasons)


class TestHandBuiltPortfolioAttacks(unittest.TestCase):
    """plan_transfers must survive hand-constructed AccountPortfolio
    objects that never passed aggregate_snapshots."""

    def _fresh_pair(self):
        designs = [make_design(501, "A")]
        src = make_snapshot(202, [(1, 501, 10)], designs)
        dst = deficit_snapshot(101, [(1, 501, 5)], designs, requested=8)
        return src, dst

    def test_hand_built_portfolio_with_valid_snapshots_ok(self):
        from sdk.inventory.portfolio import AccountPortfolio

        src, dst = self._fresh_pair()
        portfolio = AccountPortfolio(accounts=(src, dst), rows=(), actionable=False)
        # Rows empty: planner iterates portfolio.rows, so no proposals —
        # but it must not crash or invent pairings from accounts alone.
        plan = plan_transfers(portfolio, now=NOW)
        self.assertEqual(plan.proposals, ())

    def test_hand_built_portfolio_forged_rows_no_accounts(self):
        from sdk.inventory.portfolio import AccountPortfolio, DesignPortfolioRow

        src, dst = self._fresh_pair()
        forged_row = DesignPortfolioRow(
            item_design_id=9999,
            item_name="ghost",
            accounts=(),
            total_observed=0,
            total_requested=100,
            total_reserved=0,
            total_unmet=100,
            total_candidates=0,
            evidence="INFERRED",
            deficit_accounts=(101,),
            unresolved_codes=(),
            actionable=False,
        )
        portfolio = AccountPortfolio(
            accounts=(src, dst), rows=(forged_row,), actionable=False
        )
        # Ghost design 9999: no account holds stacks for it -> no sources
        # -> skipped. Nothing invented.
        plan = plan_transfers(portfolio, now=NOW)
        self.assertEqual(plan.proposals, ())

    def test_hand_built_portfolio_actionable_flag_ignored(self):
        from sdk.inventory.portfolio import AccountPortfolio

        src, dst = self._fresh_pair()
        portfolio = AccountPortfolio(accounts=(src, dst), rows=(), actionable=True)
        plan = plan_transfers(portfolio, now=NOW)
        self.assertFalse(plan.actionable)

    def test_duplicate_user_id_across_snapshots_aborts(self):
        from sdk.inventory.portfolio import AccountPortfolio

        src, dst = self._fresh_pair()
        # Two DISTINCT snapshots claiming user 202 (same user_id, different
        # captured content) — hand-built portfolio bypasses aggregate's
        # check, so the PLANNER's own duplicate gate must fire.
        designs = [make_design(501, "A")]
        imposter = make_snapshot(202, [(5, 501, 3)], designs)
        portfolio = AccountPortfolio(
            accounts=(src, imposter, dst), rows=(), actionable=False
        )
        with self.assertRaises(TransferPlannerError):
            plan_transfers(portfolio, now=NOW)

    def test_non_snapshot_account_rejected(self):
        from sdk.inventory.portfolio import AccountPortfolio

        src, dst = self._fresh_pair()
        portfolio = AccountPortfolio(
            accounts=(src, {"user_id": 1}, dst), rows=(), actionable=False
        )
        with self.assertRaises(TransferPlannerError):
            plan_transfers(portfolio, now=NOW)

    def test_forged_snapshot_fails_revalidation_inside_planner(self):
        from sdk.inventory.portfolio import AccountPortfolio

        src, dst = self._fresh_pair()
        # Build an AccountSnapshot-shaped object that fails loader gates
        # via dataclasses.replace on the real one (unmet != max(0, req-obs)).
        import dataclasses

        tampered = dataclasses.replace(
            dst,
            designs=tuple(
                dataclasses.replace(d, unmet_quantity=999)
                if d.item_design_id == 501
                else d
                for d in dst.designs
            ),
        )
        portfolio = AccountPortfolio(
            accounts=(src, tampered), rows=(), actionable=False
        )
        with self.assertRaises(TransferPlannerError):
            plan_transfers(portfolio, now=NOW)


class TestApplyRefusalIntegration(unittest.TestCase):
    """inventory apply refusal is unconditional; transfer planner does
    not open any path around it."""

    def test_apply_command_refuses(self):
        # AST-level: the refusal is a single unconditional return;
        # no If, no settings/env access anywhere in the method.
        import ast
        import inspect

        from sdk.commands import CommandRegistry

        src = inspect.getsource(CommandRegistry)
        tree = ast.parse(src)
        method = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_cmd_inventory_apply":
                method = node
                break
        self.assertIsNotNone(method)
        returns = [n for n in ast.walk(method) if isinstance(n, ast.Return)]
        self.assertEqual(len(returns), 1)
        self.assertIn(
            "REFUSED",
            ast.literal_eval(returns[0].value),  # type: ignore[arg-type]
        )
        # Method body is: docstring + the single return. No If, no
        # settings/env attribute access, no other statements.
        self.assertEqual(len(method.body), 2)
        for node in ast.walk(method):
            if isinstance(node, ast.If):
                self.fail("conditional found in _cmd_inventory_apply")
            if isinstance(node, ast.Attribute) and node.attr in (
                "settings",
                "environ",
                "getenv",
            ):
                self.fail(f"config/env access {node.attr} found in refusal stub")

    def test_transfer_module_has_no_apply_path(self):
        import inspect

        import sdk.inventory.transfer as tmod

        text = inspect.getsource(tmod)
        # The docstring documents the refusal; verify the CODE (strip
        # the module docstring) carries no apply/execute/executor path.
        import ast

        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ("apply", "execute", "executor"))
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ("apply", "execute", "executor"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
