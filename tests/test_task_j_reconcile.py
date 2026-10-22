"""Task J reconciliation tests: dual-account pre/post deltas, item identity,
stack semantics, quantity conservation, evidence-tier ceilings and the
fail-closed transferability decision.

All fixtures here are SYNTHETIC and SANITIZED: no token, credential or
secret value appears anywhere in this file. They model the shape of a
sanitized capture, never real account data.
"""

import unittest

from typing import Any, Dict

from sdk.inventory._types import Evidence
from sdk.inventory.reconcile import (
    FAIL_CONSERVATION_VIOLATED,
    FAIL_DESTINATION_IDENTITY_AMBIGUOUS,
    FAIL_QUANTITY_SEMANTICS_AMBIGUOUS,
    FAIL_SAME_ACCOUNT,
    FAIL_SOURCE_IDENTITY_AMBIGUOUS,
    FAIL_STACK_SEMANTICS_AMBIGUOUS,
    GiftItemExchange,
    IDENTITY_ITEM_ID_AND_ITEM_DESIGN_ID,
    InventoryObservation,
    ReconciliationError,
    RESULT_FAIL_CLOSED,
    RESULT_RECONCILED,
    STACK_SEMANTICS_MERGED_INTO_EXISTING,
    STACK_SEMANTICS_NEW_STACK_NEW_ITEM_ID,
    STACK_SEMANTICS_QUANTITY_SPLIT,
    ReconciliationVerdict,
    STACK_SEMANTICS_UNDETERMINED,
    TIER_PROMOTED,
    TRANSFERABILITY_INSUFFICIENT,
    TierChange,
    TransferabilityDecision,
    account_tier_changes,
    apply_tier_change,
    checksum_tier_ceiling,
    compute_inventory_delta,
    decide_transferability,
    reconcile_gift_exchange,
    semantics_tier_ceiling,
)


# Synthetic sanitized identifiers (never real accounts).
SOURCE_USER = 900001
DEST_USER = 900002
DESIGN = 4210
CAPTURED_AT = "2026-10-02T00:00:00Z"


def _obs(user_id, stacks, design_total=None, design=DESIGN, captured_at=CAPTURED_AT):
    total = design_total if design_total is not None else sum(q for _, q in stacks)
    return InventoryObservation(
        user_id=user_id,
        item_design_id=design,
        captured_at=captured_at,
        stacks=tuple(sorted(stacks)),
        design_total=total,
    )


def _exchange(**overrides):
    payload: Dict[str, Any] = dict(
        endpoint="ItemService/GiftItem",
        http_method="GET",
        account_from=SOURCE_USER,
        account_to=DEST_USER,
        item_id=501,
        item_design_id=DESIGN,
        requested_quantity=1,
        recipient_parameter_name="userId",
        response_success=True,
        captured_at=CAPTURED_AT,
    )
    payload.update(overrides)
    return GiftItemExchange(**payload)


class ComputeInventoryDeltaTests(unittest.TestCase):
    def test_quantity_split_sender_delta(self):
        before = _obs(SOURCE_USER, [(501, 5)])
        after = _obs(SOURCE_USER, [(501, 4)])
        delta = compute_inventory_delta(before, after)
        self.assertEqual(delta.quantity_delta, -1)
        self.assertEqual(delta.stack_quantity_changed, ((501, -1),))
        self.assertEqual(delta.stack_created, ())
        self.assertEqual(delta.stack_removed, ())

    def test_stack_removed_and_created(self):
        before = _obs(SOURCE_USER, [(501, 1)])
        after = _obs(SOURCE_USER, [(502, 1)])
        delta = compute_inventory_delta(before, after)
        self.assertEqual(delta.stack_removed, (501,))
        self.assertEqual(delta.stack_created, (502,))
        self.assertEqual(delta.quantity_delta, 0)

    def test_merged_recipient_delta(self):
        before = _obs(DEST_USER, [(700, 3)])
        after = _obs(DEST_USER, [(700, 4)])
        delta = compute_inventory_delta(before, after)
        self.assertEqual(delta.quantity_delta, 1)
        self.assertEqual(delta.stack_quantity_changed, ((700, 1),))

    def test_cross_account_pair_refused(self):
        before = _obs(SOURCE_USER, [(501, 1)])
        after = _obs(DEST_USER, [(501, 1)])
        with self.assertRaises(ReconciliationError):
            compute_inventory_delta(before, after)

    def test_cross_design_pair_refused(self):
        before = _obs(SOURCE_USER, [(501, 1)], design=DESIGN)
        after = _obs(SOURCE_USER, [(501, 1)], design=DESIGN + 1)
        with self.assertRaises(ReconciliationError):
            compute_inventory_delta(before, after)

    def test_duplicate_item_id_refused(self):
        with self.assertRaises(ReconciliationError):
            InventoryObservation(
                user_id=SOURCE_USER,
                item_design_id=DESIGN,
                captured_at=CAPTURED_AT,
                stacks=((501, 1), (501, 2)),
                design_total=3,
            )

    def test_design_total_below_stack_sum_refused(self):
        with self.assertRaises(ReconciliationError):
            InventoryObservation(
                user_id=SOURCE_USER,
                item_design_id=DESIGN,
                captured_at=CAPTURED_AT,
                stacks=((501, 5),),
                design_total=2,
            )


class ReconcileGiftExchangeTests(unittest.TestCase):
    def _four(self, source_before, source_after, dest_before, dest_after):
        return dict(
            source_before=source_before,
            source_after=source_after,
            destination_before=dest_before,
            destination_after=dest_after,
        )

    def test_clean_quantity_split_reconciles(self):
        verdict = reconcile_gift_exchange(
            _exchange(),
            **self._four(
                _obs(SOURCE_USER, [(501, 5)]),
                _obs(SOURCE_USER, [(501, 4)]),
                _obs(DEST_USER, [(700, 3)]),
                _obs(DEST_USER, [(700, 4)]),
            ),
        )
        self.assertEqual(verdict.result, RESULT_RECONCILED)
        self.assertEqual(verdict.sender_delta, -1)
        self.assertEqual(verdict.recipient_delta, 1)
        self.assertTrue(verdict.quantity_conserved)
        self.assertEqual(verdict.identity_semantics, IDENTITY_ITEM_ID_AND_ITEM_DESIGN_ID)
        self.assertEqual(verdict.stack_semantics, STACK_SEMANTICS_MERGED_INTO_EXISTING)
        self.assertEqual(verdict.failure_reasons, ())
        self.assertEqual(verdict.source_user_id, SOURCE_USER)
        self.assertEqual(verdict.destination_user_id, DEST_USER)

    def test_new_stack_new_item_id_reconciles(self):
        verdict = reconcile_gift_exchange(
            _exchange(),
            **self._four(
                _obs(SOURCE_USER, [(501, 1)]),
                _obs(SOURCE_USER, []),
                _obs(DEST_USER, []),
                _obs(DEST_USER, [(999, 1)]),
            ),
        )
        self.assertEqual(verdict.result, RESULT_RECONCILED)
        self.assertEqual(verdict.stack_semantics, STACK_SEMANTICS_NEW_STACK_NEW_ITEM_ID)
        self.assertEqual(verdict.sender_delta, -1)
        self.assertEqual(verdict.recipient_delta, 1)

    def test_sender_entire_stack_split_detection(self):
        # Sender quantity split from a stack that survives.
        verdict = reconcile_gift_exchange(
            _exchange(),
            **self._four(
                _obs(SOURCE_USER, [(501, 2), (502, 2)]),
                _obs(SOURCE_USER, [(501, 1), (502, 2)]),
                _obs(DEST_USER, []),
                _obs(DEST_USER, [(800, 1)]),
            ),
        )
        self.assertEqual(verdict.result, RESULT_RECONCILED)
        self.assertEqual(verdict.stack_semantics, STACK_SEMANTICS_QUANTITY_SPLIT)

    def test_same_account_fails_closed(self):
        verdict = reconcile_gift_exchange(
            _exchange(account_to=SOURCE_USER),
            **self._four(
                _obs(SOURCE_USER, [(501, 5)]),
                _obs(SOURCE_USER, [(501, 4)]),
                _obs(SOURCE_USER, [(501, 1)]),
                _obs(SOURCE_USER, [(501, 2)]),
            ),
        )
        self.assertEqual(verdict.result, RESULT_FAIL_CLOSED)
        self.assertIn(FAIL_SAME_ACCOUNT, verdict.failure_reasons)

    def test_missing_item_identity_fails_closed(self):
        verdict = reconcile_gift_exchange(
            _exchange(item_id=None, item_design_id=None),
            **self._four(
                _obs(SOURCE_USER, [(501, 5)]),
                _obs(SOURCE_USER, [(501, 4)]),
                _obs(DEST_USER, [(700, 3)]),
                _obs(DEST_USER, [(700, 4)]),
            ),
        )
        self.assertEqual(verdict.result, RESULT_FAIL_CLOSED)
        self.assertIn(FAIL_SOURCE_IDENTITY_AMBIGUOUS, verdict.failure_reasons)

    def test_missing_quantity_fails_closed(self):
        verdict = reconcile_gift_exchange(
            _exchange(requested_quantity=None),
            **self._four(
                _obs(SOURCE_USER, [(501, 5)]),
                _obs(SOURCE_USER, [(501, 4)]),
                _obs(DEST_USER, [(700, 3)]),
                _obs(DEST_USER, [(700, 4)]),
            ),
        )
        self.assertEqual(verdict.result, RESULT_FAIL_CLOSED)
        self.assertIn(FAIL_QUANTITY_SEMANTICS_AMBIGUOUS, verdict.failure_reasons)

    def test_cross_account_item_id_collision_fails_closed(self):
        # The same wire ItemId on both accounts is NOT identity; the
        # ambiguity must fail closed, never merge the accounts.
        verdict = reconcile_gift_exchange(
            _exchange(item_id=501),
            **self._four(
                _obs(SOURCE_USER, [(501, 5)]),
                _obs(SOURCE_USER, [(501, 4)]),
                _obs(DEST_USER, [(501, 3)]),
                _obs(DEST_USER, [(501, 4)]),
            ),
        )
        self.assertEqual(verdict.result, RESULT_FAIL_CLOSED)
        self.assertIn(FAIL_DESTINATION_IDENTITY_AMBIGUOUS, verdict.failure_reasons)

    def test_conservation_violation_recorded_fails_closed(self):
        # Sender loses 1, recipient gains 0: unattributable delta.
        verdict = reconcile_gift_exchange(
            _exchange(),
            **self._four(
                _obs(SOURCE_USER, [(501, 5)]),
                _obs(SOURCE_USER, [(501, 4)]),
                _obs(DEST_USER, [(700, 3)]),
                _obs(DEST_USER, [(700, 3)]),
            ),
        )
        self.assertEqual(verdict.result, RESULT_FAIL_CLOSED)
        self.assertIn(FAIL_CONSERVATION_VIOLATED, verdict.failure_reasons)
        self.assertEqual(verdict.sender_delta, -1)
        self.assertEqual(verdict.recipient_delta, 0)

    def test_contradictory_observation_account_refused(self):
        with self.assertRaises(ReconciliationError):
            reconcile_gift_exchange(
                _exchange(),
                **self._four(
                    _obs(SOURCE_USER, [(501, 5)]),
                    _obs(DEST_USER, [(501, 4)]),  # wrong account
                    _obs(DEST_USER, [(700, 3)]),
                    _obs(DEST_USER, [(700, 4)]),
                ),
            )

    def test_fee_semantics_recorded_not_forced(self):
        # A fee (recipient gains less than sender lost) is RECORDED as
        # actual behavior: the verdict carries both deltas and fails
        # closed on conservation, never guessing a transfer quantity.
        verdict = reconcile_gift_exchange(
            _exchange(),
            **self._four(
                _obs(SOURCE_USER, [(501, 5)]),
                _obs(SOURCE_USER, [(501, 3)]),
                _obs(DEST_USER, [(700, 3)]),
                _obs(DEST_USER, [(700, 4)]),
            ),
        )
        self.assertEqual(verdict.result, RESULT_FAIL_CLOSED)
        self.assertEqual(verdict.sender_delta, -2)
        self.assertEqual(verdict.recipient_delta, 1)
        self.assertIn(FAIL_CONSERVATION_VIOLATED, verdict.failure_reasons)

    def test_ambiguous_stack_semantics_fails_closed(self):
        # Both a new stack AND a merge on the recipient: ambiguous.
        verdict = reconcile_gift_exchange(
            _exchange(),
            **self._four(
                _obs(SOURCE_USER, [(501, 2)]),
                _obs(SOURCE_USER, [(501, 1)]),
                _obs(DEST_USER, [(700, 1)]),
                _obs(DEST_USER, [(700, 2), (999, 1)]),
            ),
        )
        self.assertEqual(verdict.result, RESULT_FAIL_CLOSED)
        self.assertIn(FAIL_STACK_SEMANTICS_AMBIGUOUS, verdict.failure_reasons)
        self.assertEqual(verdict.stack_semantics, STACK_SEMANTICS_UNDETERMINED)

    def test_verdict_to_dict_deterministic(self):
        verdict = reconcile_gift_exchange(
            _exchange(),
            **self._four(
                _obs(SOURCE_USER, [(501, 5)]),
                _obs(SOURCE_USER, [(501, 4)]),
                _obs(DEST_USER, [(700, 3)]),
                _obs(DEST_USER, [(700, 4)]),
            ),
        )
        d1 = verdict.to_dict()
        d2 = ReconciliationVerdict(
            source_user_id=verdict.source_user_id,
            destination_user_id=verdict.destination_user_id,
            item_design_id=verdict.item_design_id,
            result=verdict.result,
            source_delta=verdict.source_delta,
            destination_delta=verdict.destination_delta,
            identity_semantics=verdict.identity_semantics,
            stack_semantics=verdict.stack_semantics,
            sender_delta=verdict.sender_delta,
            recipient_delta=verdict.recipient_delta,
            quantity_conserved=verdict.quantity_conserved,
            failure_reasons=verdict.failure_reasons,
        ).to_dict()
        self.assertEqual(d1, d2)
        self.assertFalse(d1["destination_delta"] is None and d1["source_delta"] is None)


class EvidenceTierAccountingTests(unittest.TestCase):
    def test_unchanged_when_same_tier(self):
        change = TierChange(
            claim="GiftItem endpoint",
            prior_tier=Evidence.STATICALLY_DISCOVERED,
            new_tier=Evidence.STATICALLY_DISCOVERED,
            source="static template",
            independent_corroboration="none",
            ceiling=Evidence.STATICALLY_DISCOVERED,
            outcome="UNCHANGED",
        )
        self.assertEqual(apply_tier_change(change).new_tier, Evidence.STATICALLY_DISCOVERED)

    def test_promotion_within_ceiling_allowed_and_outcome_promoted(self):
        change = TierChange(
            claim="GiftItem endpoint",
            prior_tier=Evidence.STATICALLY_DISCOVERED,
            new_tier=Evidence.CAPTURED_NOT_VERIFIED,
            source="sanitized Task J capture",
            independent_corroboration="static template (independently discovered)",
            ceiling=Evidence.CAPTURED_NOT_VERIFIED,
            outcome=TIER_PROMOTED,
        )
        self.assertEqual(apply_tier_change(change).new_tier, Evidence.CAPTURED_NOT_VERIFIED)

    def test_unsupported_promotion_rejected(self):
        change = TierChange(
            claim="GiftItem checksum preimage",
            prior_tier=Evidence.STATICALLY_DISCOVERED,
            new_tier=Evidence.VERIFIED,
            source="single capture",
            independent_corroboration="none",
            ceiling=Evidence.CAPTURED_AMBIGUOUS,
            outcome=TIER_PROMOTED,
        )
        with self.assertRaises(ReconciliationError):
            apply_tier_change(change)

    def test_demotion_allowed(self):
        change = TierChange(
            claim="ListItemsOfAShip method",
            prior_tier=Evidence.CAPTURED_NOT_VERIFIED,
            new_tier=Evidence.CAPTURED_AMBIGUOUS,
            source="re-analysis",
            independent_corroboration="none",
            ceiling=Evidence.CAPTURED_AMBIGUOUS,
            outcome="DEMOTED",
        )
        self.assertEqual(apply_tier_change(change).new_tier, Evidence.CAPTURED_AMBIGUOUS)

    def test_account_ledger_rejects_duplicate_claims(self):
        def change(claim):
            return TierChange(
                claim=claim,
                prior_tier=Evidence.STATICALLY_DISCOVERED,
                new_tier=Evidence.STATICALLY_DISCOVERED,
                source="x",
                independent_corroboration="none",
                ceiling=Evidence.STATICALLY_DISCOVERED,
                outcome="UNCHANGED",
            )

        with self.assertRaises(ReconciliationError):
            account_tier_changes([change("a"), change("a")])

    def test_checksum_single_capture_below_verified(self):
        self.assertIs(
            checksum_tier_ceiling(has_capture=True, has_independent_preimage_evidence=False),
            Evidence.CAPTURED_AMBIGUOUS,
        )
        self.assertIs(
            checksum_tier_ceiling(has_capture=True, has_independent_preimage_evidence=True),
            Evidence.CAPTURED_NOT_VERIFIED,
        )
        self.assertIs(
            checksum_tier_ceiling(has_capture=False, has_independent_preimage_evidence=False),
            Evidence.STATICALLY_DISCOVERED,
        )

    def test_semantics_ceiling_one_capture(self):
        self.assertIs(
            semantics_tier_ceiling(capture_count=1, reconciled_unambiguously=True),
            Evidence.CAPTURED_NOT_VERIFIED,
        )
        self.assertIs(
            semantics_tier_ceiling(capture_count=0, reconciled_unambiguously=False),
            Evidence.STATICALLY_DISCOVERED,
        )
        self.assertIs(
            semantics_tier_ceiling(capture_count=1, reconciled_unambiguously=False),
            Evidence.CAPTURED_AMBIGUOUS,
        )


class TransferabilityDecisionTests(unittest.TestCase):
    def _reconciled_verdict(self):
        return reconcile_gift_exchange(
            _exchange(),
            source_before=_obs(SOURCE_USER, [(501, 5)]),
            source_after=_obs(SOURCE_USER, [(501, 4)]),
            destination_before=_obs(DEST_USER, [(700, 3)]),
            destination_after=_obs(DEST_USER, [(700, 4)]),
        )

    def test_insufficient_with_gaps(self):
        decision = decide_transferability([self._reconciled_verdict()], gaps_closed=[])
        self.assertEqual(decision.decision, TRANSFERABILITY_INSUFFICIENT)
        self.assertEqual(decision.proposed_quantity, 0)
        self.assertFalse(decision.actionable)
        self.assertTrue(decision.remaining_gaps)

    def test_insufficient_without_reconciled_verdict(self):
        decision = decide_transferability([], gaps_closed=["everything"])
        self.assertEqual(decision.decision, TRANSFERABILITY_INSUFFICIENT)

    def test_sufficient_requires_all_gaps_closed(self):
        verdict = self._reconciled_verdict()
        from sdk.inventory.reconcile import REQUIRED_TRANSFERABILITY_GAPS

        decision = decide_transferability(
            [verdict], gaps_closed=list(REQUIRED_TRANSFERABILITY_GAPS)
        )
        self.assertEqual(decision.decision, "SUFFICIENT")
        self.assertEqual(decision.proposed_quantity, 0)
        self.assertFalse(decision.actionable)

    def test_sufficient_still_non_actionable_by_construction(self):
        with self.assertRaises(ReconciliationError):
            TransferabilityDecision(
                decision="SUFFICIENT",
                remaining_gaps=(),
                proposed_quantity=1,
                actionable=False,
            )
        with self.assertRaises(ReconciliationError):
            TransferabilityDecision(
                decision="SUFFICIENT",
                remaining_gaps=(),
                proposed_quantity=0,
                actionable=True,
            )


class PurityTests(unittest.TestCase):
    def test_no_network_imports(self):
        import ast
        import inspect
        import sdk.inventory.reconcile as mod

        source = inspect.getsource(mod)
        for banned in ("import requests", "import socket", "import urllib", "import http"):
            self.assertNotIn(banned, source)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            self.assertNotIsInstance(node, ast.AsyncFunctionDef)

    def test_reconciliation_is_deterministic(self):
        args = dict(
            source_before=_obs(SOURCE_USER, [(501, 5)]),
            source_after=_obs(SOURCE_USER, [(501, 4)]),
            destination_before=_obs(DEST_USER, [(700, 3)]),
            destination_after=_obs(DEST_USER, [(700, 4)]),
        )
        v1 = reconcile_gift_exchange(_exchange(), **args)
        v2 = reconcile_gift_exchange(_exchange(), **args)
        self.assertEqual(v1.to_dict(), v2.to_dict())


def ast_parse(source):
    import ast

    return ast.parse(source)


if __name__ == "__main__":
    unittest.main()
