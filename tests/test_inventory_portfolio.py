"""Task H portfolio tests: deterministic multi-account aggregation,
per-account provenance, weakest-input evidence, deficit reporting,
fail-closed aggregation, no-transfer-planning proofs, mutation
isolation, and non-actionability.

All inputs are synthetic snapshots built through the real Task D/F/G
pipeline. No network, no filesystem, no live traffic.
"""

from __future__ import annotations

import copy
import json
import unittest
from typing import Any, Dict, List

import sdk.inventory as inv
from sdk.inventory import (
    Evidence,
    ItemDesign,
    PortfolioError,
    SnapshotAccountIdentity,
    SnapshotError,
    aggregate_snapshots,
    build_snapshot,
    snapshot_from_json,
)

from tests.test_inventory_snapshot import build_dfg, make_design


def make_snapshot(
    user_id,
    rows,
    designs,
    equipped=None,
    rooms=None,
    name=None,
    ship_id=55,
    captured_at="2026-09-28T00:00:00Z",
) -> inv.AccountSnapshot:
    _, classified, ledger, reduction = build_dfg(rows, designs, equipped=equipped, rooms=rooms)
    identity = SnapshotAccountIdentity(
        user_id=user_id,
        ship_id=ship_id,
        display_name=name if name is not None else f"user{user_id}",
    )
    return build_snapshot(identity, classified, ledger, reduction, captured_at=captured_at)


class TestPortfolioAggregation(unittest.TestCase):
    def setUp(self):
        self.designs = [
            make_design(501, "Steel Plate"),
            make_design(502, "Crew Hat", sub_type="equipmentbody"),
            make_design(503, "Immunity Juice", sub_type="immunity"),
        ]

    def test_aggregates_by_design_across_accounts(self):
        a = make_snapshot(101, [(1, 501, 5), (2, 502, 3)], self.designs, equipped={2: 1})
        b = make_snapshot(202, [(1, 501, 2), (2, 503, 4)], self.designs)
        portfolio = aggregate_snapshots([a, b])
        self.assertEqual(portfolio.account_ids, (101, 202))
        row = portfolio.row_for(501)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.total_observed, 7)
        self.assertEqual(len(row.accounts), 2)
        self.assertEqual([c.user_id for c in row.accounts], [101, 202])

    def test_input_order_does_not_matter(self):
        a = make_snapshot(101, [(1, 501, 5)], self.designs)
        b = make_snapshot(202, [(1, 501, 2), (2, 502, 1)], self.designs)
        forward = aggregate_snapshots([a, b])
        backward = aggregate_snapshots([b, a])
        self.assertEqual(forward.to_dicts(), backward.to_dicts())
        self.assertEqual(forward.account_ids, backward.account_ids)
        # accounts ordered by user_id regardless of input order
        self.assertEqual(forward.account_ids, (101, 202))

    def test_determinism_byte_identical(self):
        a = make_snapshot(101, [(1, 501, 5), (2, 502, 3)], self.designs, equipped={2: 1})
        b = make_snapshot(202, [(1, 501, 2)], self.designs)
        c = make_snapshot(303, [(1, 503, 9)], self.designs)
        p1 = aggregate_snapshots([a, b, c])
        p2 = aggregate_snapshots([c, b, a])
        self.assertEqual(json.dumps(p1.to_dicts()), json.dumps(p2.to_dicts()))

    def test_design_only_on_one_account_gets_single_entry(self):
        a = make_snapshot(101, [(1, 501, 5)], self.designs)
        b = make_snapshot(202, [(1, 503, 4)], self.designs)
        portfolio = aggregate_snapshots([a, b])
        row = portfolio.row_for(503)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual([c.user_id for c in row.accounts], [202])
        row_501 = portfolio.row_for(501)
        self.assertIsNotNone(row_501)
        assert row_501 is not None
        self.assertEqual([c.user_id for c in row_501.accounts], [101])

    def test_provenance_carries_dfg_state(self):
        a = make_snapshot(
            101,
            [(1, 501, 5), (2, 502, 3)],
            self.designs,
            equipped={2: 1},
        )
        portfolio = aggregate_snapshots([a])
        row = portfolio.row_for(502)
        self.assertIsNotNone(row)
        assert row is not None
        entry = row.accounts[0]
        self.assertEqual(entry.observed_quantity, 3)
        self.assertEqual(entry.reserved_quantity, 3)  # equipped whole-stack claim
        self.assertEqual(entry.requested_quantity, 3)
        self.assertEqual(entry.stacks, 1)
        # equipped stack is protected -> candidate 0
        self.assertEqual(entry.candidate_quantity, 0)
        design_row = a.design_row(502)
        assert design_row is not None
        self.assertEqual(entry.evidence, design_row.evidence)

    def test_candidate_totals_are_informational(self):
        a = make_snapshot(101, [(1, 501, 5)], self.designs)
        b = make_snapshot(202, [(1, 501, 7)], self.designs)
        portfolio = aggregate_snapshots([a, b])
        row = portfolio.row_for(501)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.total_candidates, 12)  # dry-run arithmetic only
        rows_with_candidates = portfolio.rows_with_candidates()
        self.assertEqual([r.item_design_id for r in rows_with_candidates], [501])
        # and none of it is actionable
        self.assertEqual(portfolio.actionable_rows(), ())
        self.assertFalse(portfolio.actionable)
        self.assertTrue(all(r.actionable is False for r in portfolio.rows))


class TestPortfolioDeficits(unittest.TestCase):
    def setUp(self):
        self.designs = [make_design(501, "Steel Plate")]

    def _deficit_snapshot(self, user_id: int) -> inv.AccountSnapshot:
        # account holds 5, strategic config asks for 8 -> unmet 3
        _, classified, ledger, _reduction = build_dfg([(1, 501, 5)], self.designs)
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
            conditions=(),
            observed_by_design={501: 5},
        )
        reduction = inv.ReductionPlanner().plan(classified, over_ledger)
        return build_snapshot(
            SnapshotAccountIdentity(user_id=user_id, ship_id=55, display_name=f"u{user_id}"),
            classified,
            over_ledger,
            reduction,
        )

    def test_deficit_reported_not_resolved(self):
        a = self._deficit_snapshot(101)
        b = make_snapshot(202, [(1, 501, 5)], self.designs)
        portfolio = aggregate_snapshots([a, b])
        deficits = portfolio.rows_with_deficits()
        self.assertEqual(len(deficits), 1)
        self.assertEqual(deficits[0].item_design_id, 501)
        self.assertEqual(deficits[0].deficit_accounts, (101,))
        self.assertEqual(deficits[0].total_unmet, 3)
        # b has no deficit: deficit provenance is per-account
        entry_b = next(c for c in deficits[0].accounts if c.user_id == 202)
        self.assertEqual(entry_b.unmet_quantity, 0)

    def test_deficit_is_never_a_transfer_plan(self):
        # A deficit on one account AND candidates on another must NOT
        # produce any pairing, matching or proposal anywhere in the
        # portfolio output. This is the core Task H prohibition.
        a = self._deficit_snapshot(101)  # unmet 3 on design 501
        b = make_snapshot(202, [(1, 501, 10)], self.designs)  # candidates 10
        portfolio = aggregate_snapshots([a, b])
        row = portfolio.row_for(501)
        self.assertIsNotNone(row)
        assert row is not None
        # both facts are reported independently, per account:
        self.assertEqual(row.deficit_accounts, (101,))
        self.assertEqual(row.total_candidates, 10)
        # and there is no structure anywhere that pairs donor/recipient:
        payload = json.dumps(portfolio.to_dicts())
        for forbidden in ("donor", "recipient", "transfer", "proposal", "match"):
            self.assertNotIn(forbidden, payload.lower())

    def test_no_transfer_semantics_in_module(self):
        import inspect

        source = inspect.getsource(inv.portfolio)
        # the module must not even define transfer-matching machinery
        for forbidden_name in ("TransferPlanner", "match_accounts", "donor_for", "recipient_for"):
            self.assertNotIn(forbidden_name, source)


class TestPortfolioFailClosed(unittest.TestCase):
    def setUp(self):
        self.designs = [make_design(501, "Steel Plate")]

    def test_duplicate_user_ids_abort(self):
        a = make_snapshot(101, [(1, 501, 5)], self.designs)
        b = make_snapshot(101, [(1, 501, 2)], self.designs)
        with self.assertRaises(PortfolioError):
            aggregate_snapshots([a, b])

    def test_same_snapshot_passed_twice_aborts(self):
        a = make_snapshot(101, [(1, 501, 5)], self.designs)
        with self.assertRaises(PortfolioError):
            aggregate_snapshots([a, a])

    def test_non_snapshot_input_aborts(self):
        a = make_snapshot(101, [(1, 501, 5)], self.designs)
        with self.assertRaises(PortfolioError):
            aggregate_snapshots([a, {"version": 1}])  # type: ignore[list-item]

    def test_corrupt_snapshot_in_mix_aborts_everything(self):
        # fail-closed aggregation: one corrupt snapshot aborts the whole
        # portfolio — never a partial aggregate over the good ones.
        a = make_snapshot(101, [(1, 501, 5)], self.designs)
        b = make_snapshot(202, [(1, 501, 2)], self.designs)
        corrupt_payload = json.loads(b.to_json())
        corrupt_payload["version"] = 99
        with self.assertRaises(SnapshotError):
            snapshot_from_json(json.dumps(corrupt_payload))
        # aggregation itself receives typed snapshots; a corrupt one can
        # only exist if someone bypasses the loader — verify the loader
        # gate holds and aggregation over loaded snapshots succeeds only
        # when every input loaded cleanly:
        good = [snapshot_from_json(s.to_json()) for s in (a, b)]
        portfolio = aggregate_snapshots(good)
        self.assertEqual(portfolio.account_ids, (101, 202))

    def test_empty_portfolio_is_valid(self):
        portfolio = aggregate_snapshots([])
        self.assertEqual(portfolio.rows, ())
        self.assertEqual(portfolio.account_ids, ())
        self.assertFalse(portfolio.actionable)

    def test_single_account_portfolio(self):
        a = make_snapshot(101, [(1, 501, 5)], self.designs)
        portfolio = aggregate_snapshots([a])
        self.assertEqual(portfolio.account_ids, (101,))
        self.assertEqual(len(portfolio.rows), 1)


class TestPortfolioEvidence(unittest.TestCase):
    def setUp(self):
        self.designs = [make_design(501, "Steel Plate")]

    def test_weakest_input_evidence_across_accounts(self):
        # account A: stack evidence CAPTURED_NOT_VERIFIED (feed rows)
        a = make_snapshot(101, [(1, 501, 5)], self.designs)
        # account B: same design, also feed-derived
        b = make_snapshot(202, [(1, 501, 2)], self.designs)
        portfolio = aggregate_snapshots([a, b])
        row = portfolio.row_for(501)
        self.assertIsNotNone(row)
        assert row is not None
        # feed-derived tiers never reach VERIFIED (ListItemsOfAShip cap)
        self.assertNotEqual(Evidence(row.evidence), Evidence.VERIFIED)

    def test_unknown_design_floor_propagates(self):
        # account A's design 501 is unknown to the feed -> stack evidence
        # UNKNOWN; the portfolio row must carry the UNKNOWN floor.
        designs_a: List[ItemDesign] = []
        a = make_snapshot(101, [(1, 501, 5)], designs_a)
        b = make_snapshot(202, [(1, 501, 2)], self.designs)
        portfolio = aggregate_snapshots([a, b])
        row = portfolio.row_for(501)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(Evidence(row.evidence), Evidence.UNKNOWN)

    def test_evidence_never_promoted_by_aggregation(self):
        a = make_snapshot(101, [(1, 501, 5)], self.designs)
        portfolio = aggregate_snapshots([a])
        row = portfolio.row_for(501)
        self.assertIsNotNone(row)
        assert row is not None
        # a design with no ledger entries has UNKNOWN evidence; portfolio
        # must not invent a stronger tier just because aggregation ran.
        self.assertEqual(Evidence(row.evidence), Evidence.UNKNOWN)


class TestPortfolioMutationIsolation(unittest.TestCase):
    def setUp(self):
        self.designs = [make_design(501, "Steel Plate"), make_design(502, "Crew Hat")]

    def test_aggregation_does_not_mutate_inputs(self):
        a = make_snapshot(101, [(1, 501, 5), (2, 502, 1)], self.designs)
        b = make_snapshot(202, [(1, 502, 3)], self.designs)
        before_a = a.to_json()
        before_b = b.to_json()
        aggregate_snapshots([a, b])
        self.assertEqual(a.to_json(), before_a)
        self.assertEqual(b.to_json(), before_b)

    def test_loaded_payload_mutation_does_not_leak(self):
        a = make_snapshot(101, [(1, 501, 5)], self.designs)
        payload: Dict[str, Any] = json.loads(a.to_json())
        frozen_copy = copy.deepcopy(payload)
        loaded = inv.snapshot_from_dict(payload)
        # mutating the caller's dict after load must not affect snapshot
        payload["identity"]["user_id"] = 999
        self.assertEqual(loaded.user_id, 101)
        self.assertNotEqual(payload, frozen_copy)  # sanity: mutation happened


class TestPortfolioNonActionability(unittest.TestCase):
    def test_every_row_and_contribution_is_actionable_false(self):
        designs = [make_design(501, "Steel Plate")]
        a = make_snapshot(101, [(1, 501, 5)], designs)
        b = make_snapshot(202, [(1, 501, 7)], designs)
        portfolio = aggregate_snapshots([a, b])
        self.assertFalse(portfolio.actionable)
        self.assertEqual(portfolio.actionable_rows(), ())
        for row in portfolio.rows:
            self.assertFalse(row.actionable)
            self.assertFalse(row.deficit_accounts is None)
        # serialized output also carries actionable=false everywhere
        payload = json.dumps(portfolio.to_dicts())
        self.assertNotIn('"actionable": true', payload)
        self.assertIn('"actionable": false', payload)


class TestPortfolioAdversarialGates(unittest.TestCase):
    """Adversarial portfolio gates (lane t_2c696fd5).

    Pins the two confirmed fail-open holes: aggregation accepting a
    hand-constructed AccountSnapshot that bypassed every loader gate,
    and cross-account condition bleed in per-account provenance.
    """

    def setUp(self):
        self.designs = [make_design(501, "Steel Plate")]

    def _loaded(self, user_id=101):
        _, classified, ledger, reduction = build_dfg([(1, 501, 5)], self.designs)
        snap = inv.build_snapshot(
            SnapshotAccountIdentity(user_id, 55, f"u{user_id}"),
            classified,
            ledger,
            reduction,
            captured_at="2026-09-28T00:00:00Z",
        )
        return snapshot_from_json(snap.to_json())

    def test_rejects_hand_constructed_snapshot_bypassing_loader(self):
        # A directly-constructed AccountSnapshot that never passed the
        # loader must fail aggregate re-validation (user_id=0,
        # garbage captured_at, actionable=True are all loader-rejected
        # values smuggled in as typed fields).
        from sdk.inventory.snapshot import AccountSnapshot

        rogue = AccountSnapshot(
            version=1,
            identity=SnapshotAccountIdentity(user_id=0, ship_id=55, display_name="r"),
            stacks=(),
            designs=(),
            conditions=(),
            limitations=(),
            captured_at="garbage",
            actionable=True,
        )
        with self.assertRaises(PortfolioError) as ctx:
            aggregate_snapshots([rogue])
        self.assertIn("failed re-validation", str(ctx.exception))

    def test_rejects_forged_vocab_via_direct_construction(self):
        # Even a "plausible" forged snapshot (valid-looking types, valid
        # user_id) carrying loader-forbidden vocabulary (TRANSFER hint,
        # VERIFIED evidence) is caught by the re-validation pass.
        from sdk.inventory.snapshot import (
            AccountSnapshot,
            SnapshotDesignRow,
            SnapshotStackRow,
        )

        forged = AccountSnapshot(
            version=1,
            identity=SnapshotAccountIdentity(user_id=101, ship_id=55, display_name="f"),
            stacks=(
                SnapshotStackRow(
                    item_id=1,
                    item_design_id=501,
                    ship_id=55,
                    quantity=5,
                    classification="CONSUMABLE",
                    protected=False,
                    unknown=False,
                    roles=(),
                    protection_reasons=(),
                    classification_reasons=(),
                    evidence="VERIFIED",
                    requested_quantity=0,
                    reserved_quantity=0,
                    candidate_quantity=5,
                    candidate_state="CANDIDATE",
                    candidate_reasons=(),
                    candidate_evidence="CAPTURED_NOT_VERIFIED",
                    disposition_hint="TRANSFER",
                ),
            ),
            designs=(
                SnapshotDesignRow(
                    item_design_id=501,
                    item_name="X",
                    observed_quantity=5,
                    requested_quantity=0,
                    reserved_quantity=0,
                    unmet_quantity=0,
                    evidence="UNKNOWN",
                    stacks=1,
                ),
            ),
            captured_at="2026-09-28T00:00:00Z",
            actionable=False,
        )
        with self.assertRaises(PortfolioError) as ctx:
            aggregate_snapshots([forged])
        message = str(ctx.exception)
        self.assertTrue(
            "VERIFIED" in message or "TRANSFER" in message,
            f"re-validation did not name the forged vocabulary: {message}",
        )

    def test_no_condition_bleed_between_accounts(self):
        # Account A has a design-scoped DEMAND_EXCEEDS_SUPPLY condition
        # on 501; account B does not. B's contribution must NOT carry
        # A's condition code (provenance must stay per-account).
        _, classified_a, _ledger_a, _red_a = build_dfg([(1, 501, 5)], self.designs)
        over_a = inv.ReservationLedger(
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
            conditions=(
                inv.UnresolvedDemand(
                    code=inv.DEMAND_EXCEEDS_SUPPLY, detail="d", design_id=501
                ),
            ),
            observed_by_design={501: 5},
        )
        red_a = inv.ReductionPlanner().plan(classified_a, over_a)
        a = inv.build_snapshot(
            SnapshotAccountIdentity(101, 55, "A"),
            classified_a,
            over_a,
            red_a,
            captured_at="2026-09-28T00:00:00Z",
        )
        b = self._loaded(user_id=202)
        portfolio = aggregate_snapshots([a, b])
        row = portfolio.row_for(501)
        assert row is not None
        entry_a = next(c for c in row.accounts if c.user_id == 101)
        entry_b = next(c for c in row.accounts if c.user_id == 202)
        self.assertIn("DEMAND_EXCEEDS_SUPPLY", entry_a.conditions)
        self.assertEqual(entry_b.conditions, ())
        # Row-level unresolved codes still pool for reporting:
        self.assertIn("DEMAND_EXCEEDS_SUPPLY", row.unresolved_codes)

    def test_batch_fails_closed_on_forged_input(self):
        # A valid snapshot plus a forged one aborts the ENTIRE
        # aggregation (batch rule §3b holds even when the corruption
        # only exists as a typed object, not on-disk JSON).
        from sdk.inventory.snapshot import AccountSnapshot

        good = self._loaded(user_id=101)
        forged = AccountSnapshot(
            version=2,  # unknown version smuggled past the loader
            identity=SnapshotAccountIdentity(user_id=202, ship_id=55, display_name="f"),
            captured_at="2026-09-28T00:00:00Z",
            actionable=False,
        )
        with self.assertRaises(PortfolioError):
            aggregate_snapshots([good, forged])


if __name__ == "__main__":
    unittest.main()
