"""Task J dual-account GiftItem reconciliation and evidence-tier accounting.

PURE analysis surface: given four inventory observations (source and
destination, immediately before and immediately after ONE controlled
human-operated official-client GiftItem exchange) plus the observed wire
exchange, compute per-account inventory deltas, item-identity and stack
semantics, quantity conservation, and the evidence tier the exchange
actually supports. Fail closed on every ambiguity.

This module NEVER issues network I/O, never constructs a GiftItem
request, never mutates anything, and contains no execution, retry or
sequencing path. It is analysis-only: numbers in, verdicts out.

Identity rules pinned here (wire semantics, distinct from the Task I
planning join):
- Account identity on the wire is the PSS UserId (positive integer).
- Item identity on the wire is ItemId, scoped to ONE account: the same
  ItemId value on two accounts is NOT the same item and must never be
  treated as such.
- ItemDesignId names the item design (kind of item), never a concrete
  instance. Cross-account identity is BY DESIGN ONLY, and only as an
  analysis pairing - never as a wire identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from ._types import Evidence

# ---------------------------------------------------------------------------
# Verdict constants (stable strings for tests and the verifier).
# ---------------------------------------------------------------------------

RESULT_RECONCILED = "RECONCILED"
RESULT_FAIL_CLOSED = "FAIL_CLOSED"

# Failure reason codes (sorted lexicographically in every verdict).
FAIL_SAME_ACCOUNT = "FAIL_SAME_ACCOUNT"
FAIL_SOURCE_IDENTITY_AMBIGUOUS = "FAIL_SOURCE_IDENTITY_AMBIGUOUS"
FAIL_DESTINATION_IDENTITY_AMBIGUOUS = "FAIL_DESTINATION_IDENTITY_AMBIGUOUS"
FAIL_QUANTITY_SEMANTICS_AMBIGUOUS = "FAIL_QUANTITY_SEMANTICS_AMBIGUOUS"
FAIL_SOURCE_DELTA_AMBIGUOUS = "FAIL_SOURCE_DELTA_AMBIGUOUS"
FAIL_DESTINATION_DELTA_AMBIGUOUS = "FAIL_DESTINATION_DELTA_AMBIGUOUS"
FAIL_CONSERVATION_VIOLATED = "FAIL_CONSERVATION_VIOLATED"
FAIL_STACK_SEMANTICS_AMBIGUOUS = "FAIL_STACK_SEMANTICS_AMBIGUOUS"
FAIL_EVIDENCE_BELOW_CAP = "FAIL_EVIDENCE_BELOW_CAP"

# Permitted tier-accounting outcomes (evidence hierarchy ceiling rules).
TIER_UNCHANGED = "UNCHANGED"
TIER_PROMOTED = "PROMOTED"

# Item-identity semantics the reconciliation can classify an exchange as.
IDENTITY_ITEM_ID = "ITEM_ID"
IDENTITY_ITEM_ID_AND_ITEM_DESIGN_ID = "ITEM_ID_AND_ITEM_DESIGN_ID"
IDENTITY_ITEM_DESIGN_ID = "ITEM_DESIGN_ID"
IDENTITY_UNKNOWN = "IDENTITY_UNKNOWN"

# Stack behavior the reconciliation can classify an exchange as.
STACK_SEMANTICS_QUANTITY_SPLIT = "QUANTITY_SPLIT"
STACK_SEMANTICS_MERGED_INTO_EXISTING = "MERGED_INTO_EXISTING"
STACK_SEMANTICS_NEW_STACK_NEW_ITEM_ID = "NEW_STACK_NEW_ITEM_ID"
STACK_SEMANTICS_ENTIRE_STACK = "ENTIRE_STACK"
STACK_SEMANTICS_UNDETERMINED = "UNDETERMINED"


class ReconciliationError(ValueError):
    """Fail-closed reconciliation error (malformed input, contradiction).

    Raised instead of any silent repair or guessing: a reconciliation
    input that cannot be interpreted exactly is an error, never a
    partial verdict.
    """


# ---------------------------------------------------------------------------
# Pure input records (plain validated data; no secrets ever enter these).
# ---------------------------------------------------------------------------


def _require_int(value: object, path: str, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReconciliationError(f"{path} must be an integer, got {type(value).__name__}")
    if minimum is not None and value < minimum:
        raise ReconciliationError(f"{path} must be >= {minimum}, got {value}")
    return int(value)


def _require_str(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ReconciliationError(f"{path} must be a string, got {type(value).__name__}")
    return str(value)


@dataclass(frozen=True)
class InventoryObservation:
    """One account's inventory observation for one item design.

    The BEFORE/AFTER observation of ONE account for ONE design: the
    concrete stack rows for that design (ItemId -> quantity, the
    per-stack wire identity facts) plus the account's design-level
    total. Built from sanitized ListItemsOfAShip data only; never
    contains credentials.
    """

    user_id: int
    item_design_id: int
    captured_at: str
    stacks: Tuple[Tuple[int, int], ...] = ()  # (item_id, quantity), sorted
    design_total: int = 0

    def __post_init__(self) -> None:
        _require_int(self.user_id, "user_id", minimum=1)
        _require_int(self.item_design_id, "item_design_id", minimum=0)
        _require_str(self.captured_at, "captured_at")
        if self.design_total < 0:
            raise ReconciliationError("design_total must be non-negative")
        seen: Dict[int, int] = {}
        for item_id, quantity in self.stacks:
            _require_int(item_id, "stacks.item_id", minimum=0)
            _require_int(quantity, "stacks.quantity", minimum=0)
            if item_id in seen:
                raise ReconciliationError(
                    f"duplicate item_id {item_id} in observation for user "
                    f"{self.user_id} design {self.item_design_id}"
                )
            seen[item_id] = quantity
        if self.design_total < sum(q for _, q in self.stacks):
            raise ReconciliationError(
                f"design_total {self.design_total} is below the stack sum "
                f"{sum(q for _, q in self.stacks)} for user {self.user_id} "
                f"design {self.item_design_id}"
            )

    @property
    def stack_total(self) -> int:
        return sum(quantity for _, quantity in self.stacks)

    def quantity_for_item(self, item_id: int) -> int:
        for stack_id, quantity in self.stacks:
            if stack_id == int(item_id):
                return quantity
        return 0


@dataclass(frozen=True)
class GiftItemExchange:
    """The sanitized facts of ONE captured GiftItem exchange.

    account_from/account_to are the PSS UserIds as observed on the wire
    (sender identity is implied by the authenticated session that issued
    the request; recipient identity is the explicitly observed recipient
    parameter). item_id and item_design_id are the wire item
    references as captured. requested_quantity is the observed quantity
    parameter if present. The request verb, endpoint and parameter map
    are the sanitized capture facts. No token or checksum VALUE may be
    stored here: token placement and checksum presence are recorded as
    structural facts only.
    """

    endpoint: str
    http_method: str
    account_from: int
    account_to: int
    item_id: Optional[int]
    item_design_id: Optional[int]
    requested_quantity: Optional[int]
    recipient_parameter_name: str
    response_success: bool
    captured_at: str

    def __post_init__(self) -> None:
        _require_str(self.endpoint, "endpoint")
        _require_str(self.http_method, "http_method")
        _require_int(self.account_from, "account_from", minimum=1)
        _require_int(self.account_to, "account_to", minimum=1)
        for name, value in (
            ("item_id", self.item_id),
            ("item_design_id", self.item_design_id),
            ("requested_quantity", self.requested_quantity),
        ):
            if value is not None:
                _require_int(value, name, minimum=0)
        _require_str(self.recipient_parameter_name, "recipient_parameter_name")
        if not self.recipient_parameter_name:
            raise ReconciliationError("recipient_parameter_name must be non-empty")
        if isinstance(self.response_success, bool) is False and self.response_success not in (0, 1):
            raise ReconciliationError("response_success must be a boolean")
        _require_str(self.captured_at, "captured_at")


# ---------------------------------------------------------------------------
# Delta computation (pure).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InventoryDelta:
    """One account's before/after delta for one design.

    All ambiguity in the raw observations must be resolved BEFORE the
    delta is built; the builder below refuses to emit a delta that
    cannot be attributed exactly.
    """

    user_id: int
    item_design_id: int
    quantity_delta: int
    stack_created: Tuple[int, ...] = ()
    stack_removed: Tuple[int, ...] = ()
    stack_quantity_changed: Tuple[Tuple[int, int], ...] = ()

    def to_dict(self) -> Dict[str, object]:
        return {
            "user_id": int(self.user_id),
            "item_design_id": int(self.item_design_id),
            "quantity_delta": int(self.quantity_delta),
            "stack_created": list(self.stack_created),
            "stack_removed": list(self.stack_removed),
            "stack_quantity_changed": [
                [int(item_id), int(delta)] for item_id, delta in self.stack_quantity_changed
            ],
        }


def compute_inventory_delta(
    before: InventoryObservation, after: InventoryObservation
) -> InventoryDelta:
    """Compute one account's before/after delta for one design.

    Fail closed on identity contradictions: the observation pair must
    describe the same account and the same design, or the input is
    contradictory (never a cross-account comparison).
    """
    if before.user_id != after.user_id:
        raise ReconciliationError(
            f"observation pair describes different accounts "
            f"({before.user_id} vs {after.user_id})"
        )
    if before.item_design_id != after.item_design_id:
        raise ReconciliationError(
            f"observation pair describes different designs "
            f"({before.item_design_id} vs {after.item_design_id})"
        )
    before_ids = {item_id for item_id, _ in before.stacks}
    after_ids = {item_id for item_id, _ in after.stacks}
    created = tuple(sorted(after_ids - before_ids))
    removed = tuple(sorted(before_ids - after_ids))
    changed: List[Tuple[int, int]] = []
    for item_id in sorted(before_ids & after_ids):
        delta = after.quantity_for_item(item_id) - before.quantity_for_item(item_id)
        if delta != 0:
            changed.append((item_id, delta))
    quantity_delta = after.stack_total - before.stack_total
    return InventoryDelta(
        user_id=before.user_id,
        item_design_id=before.item_design_id,
        quantity_delta=quantity_delta,
        stack_created=created,
        stack_removed=removed,
        stack_quantity_changed=tuple(changed),
    )


# ---------------------------------------------------------------------------
# Reconciliation verdict.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationVerdict:
    """The full fail-closed verdict for ONE controlled exchange.

    result is RECONCILED only when EVERY identity, quantity, delta and
    conservation question is answered unambiguously from the four
    observations plus the exchange record. Any ambiguity fails closed
    with the exact reason codes in ``failure_reasons``.
    """

    source_user_id: int
    destination_user_id: int
    item_design_id: int
    result: str
    source_delta: Optional[InventoryDelta]
    destination_delta: Optional[InventoryDelta]
    identity_semantics: str
    stack_semantics: str
    sender_delta: Optional[int]
    recipient_delta: Optional[int]
    quantity_conserved: Optional[bool]
    failure_reasons: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, object]:
        return {
            "source_user_id": int(self.source_user_id),
            "destination_user_id": int(self.destination_user_id),
            "item_design_id": int(self.item_design_id),
            "result": str(self.result),
            "source_delta": self.source_delta.to_dict() if self.source_delta else None,
            "destination_delta": (
                self.destination_delta.to_dict() if self.destination_delta else None
            ),
            "identity_semantics": str(self.identity_semantics),
            "stack_semantics": str(self.stack_semantics),
            "sender_delta": self.sender_delta,
            "recipient_delta": self.recipient_delta,
            "quantity_conserved": self.quantity_conserved,
            "failure_reasons": list(self.failure_reasons),
        }


def _classify_stack_semantics(
    source_delta: InventoryDelta,
    destination_delta: InventoryDelta,
    exchange: GiftItemExchange,
) -> Tuple[str, List[str]]:
    """Classify stack split/merge/new-ItemId behavior; returns
    (semantics, failure_reasons). Classification is conservative:
    multiple behaviors matching the deltas is ambiguous -> fail closed.

    Evidence-level note: with ONE exchange, behaviors are classified
    from the observed deltas only; nothing is inferred beyond them.
    """
    failures: List[str] = []
    sender_split = bool(source_delta.stack_quantity_changed) and any(
        delta < 0 for _, delta in source_delta.stack_quantity_changed
    )
    sender_entire_stack = bool(source_delta.stack_removed) and not sender_split
    recipient_new_stack = bool(destination_delta.stack_created)
    recipient_merged = bool(destination_delta.stack_quantity_changed) and any(
        delta > 0 for _, delta in destination_delta.stack_quantity_changed
    )

    if sender_split and recipient_new_stack and not recipient_merged:
        return STACK_SEMANTICS_QUANTITY_SPLIT, failures
    if sender_split and recipient_merged and not recipient_new_stack:
        return STACK_SEMANTICS_MERGED_INTO_EXISTING, failures
    if recipient_new_stack and not recipient_merged and not sender_split:
        if sender_entire_stack:
            return STACK_SEMANTICS_NEW_STACK_NEW_ITEM_ID, failures
        return STACK_SEMANTICS_NEW_STACK_NEW_ITEM_ID, failures
    if sender_entire_stack and recipient_merged and not recipient_new_stack:
        return STACK_SEMANTICS_ENTIRE_STACK, failures
    failures.append(FAIL_STACK_SEMANTICS_AMBIGUOUS)
    return STACK_SEMANTICS_UNDETERMINED, failures


def reconcile_gift_exchange(
    exchange: GiftItemExchange,
    source_before: InventoryObservation,
    source_after: InventoryObservation,
    destination_before: InventoryObservation,
    destination_after: InventoryObservation,
) -> ReconciliationVerdict:
    """Reconcile ONE controlled dual-account GiftItem exchange.

    Pure and fail-closed: any ambiguity in identity, quantity, deltas
    or conservation produces RESULT_FAIL_CLOSED with the exact reason
    codes; the verdict never guesses semantics.

    The conservation invariant (sender delta + recipient delta == 0)
    is checked, NOT enforced: the protocol may demonstrate fees,
    transformations or stack replacement. If the observed deltas do
    not conserve, that is RECORDED as actual behavior and the verdict
    fails closed unless the deltas are still fully attributable.
    """
    failures: List[str] = []

    # -- Account identities ------------------------------------------------
    if exchange.account_from == exchange.account_to:
        failures.append(FAIL_SAME_ACCOUNT)
    for observation, expected, label in (
        (source_before, exchange.account_from, "source"),
        (source_after, exchange.account_from, "source"),
        (destination_before, exchange.account_to, "destination"),
        (destination_after, exchange.account_to, "destination"),
    ):
        if observation.user_id != expected:
            raise ReconciliationError(
                f"{label} observation user_id {observation.user_id} does not "
                f"match the exchange account {expected} (contradictory input)"
            )
    design_id = source_before.item_design_id
    for observation, label in (
        (source_after, "source"),
        (destination_before, "destination"),
        (destination_after, "destination"),
    ):
        if observation.item_design_id != design_id:
            raise ReconciliationError(
                f"{label} observation design {observation.item_design_id} "
                f"does not match source design {design_id}"
            )

    # -- Item identity on the wire ----------------------------------------
    identity = IDENTITY_UNKNOWN
    if exchange.item_id is not None and exchange.item_design_id is not None:
        identity = IDENTITY_ITEM_ID_AND_ITEM_DESIGN_ID
    elif exchange.item_id is not None:
        identity = IDENTITY_ITEM_ID
    elif exchange.item_design_id is not None:
        identity = IDENTITY_ITEM_DESIGN_ID
    else:
        failures.append(FAIL_SOURCE_IDENTITY_AMBIGUOUS)

    # Cross-account ItemId collision check (wire identity rule): the
    # same ItemId observed on both accounts is NOT the same item and
    # must never be treated as identity across accounts.
    if exchange.item_id is not None:
        source_ids = {i for i, _ in source_before.stacks} | {
            i for i, _ in source_after.stacks
        }
        destination_ids = {i for i, _ in destination_before.stacks} | {
            i for i, _ in destination_after.stacks
        }
        if exchange.item_id in source_ids and exchange.item_id in destination_ids:
            failures.append(FAIL_DESTINATION_IDENTITY_AMBIGUOUS)

    # -- Quantity semantics -----------------------------------------------
    if exchange.requested_quantity is None:
        failures.append(FAIL_QUANTITY_SEMANTICS_AMBIGUOUS)

    # -- Deltas ------------------------------------------------------------
    source_delta = compute_inventory_delta(source_before, source_after)
    destination_delta = compute_inventory_delta(destination_before, destination_after)

    # -- Stack semantics ----------------------------------------------------
    stack_semantics, stack_failures = _classify_stack_semantics(
        source_delta, destination_delta, exchange
    )
    failures.extend(stack_failures)

    # -- Conservation ------------------------------------------------------
    # Checked ALWAYS (independently of stack-semantics ambiguity) so a
    # verdict records every observed failure, never just the first.
    conserved: Optional[bool] = (
        source_delta.quantity_delta + destination_delta.quantity_delta == 0
    )
    if not conserved:
        # Not a protocol violation by itself (fees/transformations
        # are possible) but it IS a conservation failure for this
        # exchange and the verdict must record it, fail closed.
        failures.append(FAIL_CONSERVATION_VIOLATED)

    if failures:
        return ReconciliationVerdict(
            source_user_id=exchange.account_from,
            destination_user_id=exchange.account_to,
            item_design_id=design_id,
            result=RESULT_FAIL_CLOSED,
            source_delta=source_delta,
            destination_delta=destination_delta,
            identity_semantics=identity,
            stack_semantics=stack_semantics,
            sender_delta=source_delta.quantity_delta,
            recipient_delta=destination_delta.quantity_delta,
            quantity_conserved=None,
            failure_reasons=tuple(sorted(set(failures))),
        )

    return ReconciliationVerdict(
        source_user_id=exchange.account_from,
        destination_user_id=exchange.account_to,
        item_design_id=design_id,
        result=RESULT_RECONCILED,
        source_delta=source_delta,
        destination_delta=destination_delta,
        identity_semantics=identity,
        stack_semantics=stack_semantics,
        sender_delta=source_delta.quantity_delta,
        recipient_delta=destination_delta.quantity_delta,
        quantity_conserved=True,
        failure_reasons=(),
    )


# ---------------------------------------------------------------------------
# Evidence-tier accounting (prior tier vs new tier per claim).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TierChange:
    """One evidence-tier change for one claim.

    ceiling is the promotion ceiling actually proved by the supporting
    evidence (the strongest tier this claim may now carry). A
    promotion is legal only when ceiling is strictly stronger than the
    prior tier; otherwise the change is UNCHANGED and the claim keeps
    its prior tier.
    """

    claim: str
    prior_tier: Evidence
    new_tier: Evidence
    source: str
    independent_corroboration: str
    ceiling: Evidence
    outcome: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "claim": str(self.claim),
            "prior_tier": self.prior_tier.value,
            "new_tier": self.new_tier.value,
            "source": str(self.source),
            "independent_corroboration": str(self.independent_corroboration),
            "ceiling": self.ceiling.value,
            "outcome": str(self.outcome),
        }


def apply_tier_change(change: TierChange) -> TierChange:
    """Apply one tier change, enforcing the evidence ceiling.

    A change whose claimed new tier exceeds the evidence ceiling is
    rejected (fail closed): the returned change is clamped to the
    ceiling only when the ceiling is at or below the prior tier
    (UNCHANGED); a ceiling ABOVE the prior tier but BELOW the claimed
    new tier is a contradiction (the evidence does not support the
    claim) and raises.
    """
    if change.new_tier == change.prior_tier:
        return change
    claimed = change.new_tier
    # Promotion iff the claimed tier is strictly stronger than the
    # prior one (weaker() returns the claimed tier only when it is at
    # or below the prior tier).
    if Evidence.weaker(change.prior_tier, claimed) is claimed:
        # Demotion or equal-strength move: allowed, evidence may get
        # weaker (never fabricated).
        return change
    # Promotion: the ceiling must be at least as strong as the claimed
    # tier. weaker() returns the weaker of the two, so a supporting
    # ceiling yields the claimed tier itself (claimed at-or-below
    # ceiling strength); a weaker ceiling yields the ceiling, which
    # means the evidence does not support the claim (fail closed).
    if Evidence.weaker(claimed, change.ceiling) is not claimed:
        raise ReconciliationError(
            f"tier change for {change.claim!r}: ceiling {change.ceiling.value} "
            f"does not support promotion from {change.prior_tier.value} "
            f"to {claimed.value} (fail closed, no unsupported promotion)"
        )
    return change


def account_tier_changes(changes: Sequence[TierChange]) -> Tuple[TierChange, ...]:
    """Validate a full evidence-tier accounting; returns it unchanged.

    Every change is validated through apply_tier_change: any
    unsupported promotion raises ReconciliationError (fail closed on
    the whole accounting, never a partial ledger).
    """
    validated: List[TierChange] = []
    seen_claims: Dict[str, TierChange] = {}
    for change in changes:
        validated_change = apply_tier_change(change)
        if validated_change.claim in seen_claims:
            raise ReconciliationError(
                f"duplicate tier-change claim {validated_change.claim!r}"
            )
        seen_claims[validated_change.claim] = validated_change
        validated.append(validated_change)
    return tuple(validated)


# ---------------------------------------------------------------------------
# Single-observation evidence ceiling (checksum and semantics evidence rules).
# ---------------------------------------------------------------------------


def checksum_tier_ceiling(
    has_capture: bool, has_independent_preimage_evidence: bool
) -> Evidence:
    """The highest tier a checksum claim may carry under this evidence.

    Single-observation ceiling rules pinned by the task brief:
    - zero captures -> STATICALLY_DISCOVERED at best;
    - one capture and NO independent preimage evidence -> the claim
      CANNOT reach VERIFIED (CAPTURED_NOT_VERIFIED at best);
    - one capture PLUS independently discovered static code that
      uniquely establishes the preimage -> CAPTURED_NOT_VERIFIED still
      unless the static evidence is itself verified; a single capture
      plus static analysis remains below VERIFIED (the single
      dynamic observation corroborates the static derivation but is
      one observation, not a proof of the live formula).
    """
    if not has_capture:
        return Evidence.STATICALLY_DISCOVERED
    if has_independent_preimage_evidence:
        # Static derivation + one agreeing observation: still a
        # single-observation agreement; the ceiling stays below VERIFIED.
        return Evidence.CAPTURED_NOT_VERIFIED
    return Evidence.CAPTURED_AMBIGUOUS


def semantics_tier_ceiling(
    capture_count: int,
    reconciled_unambiguously: bool,
) -> Evidence:
    """The highest tier a protocol-semantics claim may carry.

    Semantics observed in a small number of captures and reconciled
    unambiguously reach CAPTURED_NOT_VERIFIED at best: n=1 (or any
    small n) does not prove the endpoint always behaves this way, so
    VERIFIED requires more than this slice can produce.
    """
    if capture_count <= 0:
        return Evidence.STATICALLY_DISCOVERED
    if capture_count == 1 and reconciled_unambiguously:
        return Evidence.CAPTURED_NOT_VERIFIED
    if capture_count > 1 and reconciled_unambiguously:
        return Evidence.CAPTURED_NOT_VERIFIED
    return Evidence.CAPTURED_AMBIGUOUS


# ---------------------------------------------------------------------------
# Transferability decision (explicit, evidence-bounded).
# ---------------------------------------------------------------------------

TRANSFERABILITY_INSUFFICIENT = "INSUFFICIENT"
TRANSFERABILITY_SUFFICIENT = "SUFFICIENT"

# The exact named evidence gaps that must be closed before the
# transferability predicate can even be defined for a future slice.
REQUIRED_TRANSFERABILITY_GAPS: Tuple[str, ...] = (
    "GiftItem:: complete endpoint/template",
    "GiftItem:: request verb (network semantics unknown)",
    "GiftItem:: recipient parameter semantics",
    "GiftItem:: ItemId vs ItemDesignId semantics",
    "GiftItem:: quantity semantics",
    "GiftItem:: checksum inputs/preimage",
    "GiftItem:: response/success semantics",
    "GiftItem:: sender inventory delta",
    "GiftItem:: recipient inventory delta",
    "GiftItem:: failure/error behavior",
    "ListItemsOfAShip genuine official-client capture",
    "ListItemsOfAShip response ItemId/ItemDesignId/quantity semantics",
    "ListItemsOfAShip duplicate/placeholder behavior",
    "ListItemsOfAShip captured_from_official_client provenance",
)


@dataclass(frozen=True)
class TransferabilityDecision:
    """The explicit transferability answer for the Task J slice.

    The decision NEVER makes Task I proposals actionable and never
    defines an executor: a SUFFICIENT verdict only records that the
    evidence is enough for a FUTURE slice to define a safe predicate.
    """

    decision: str
    remaining_gaps: Tuple[str, ...]
    proposed_quantity: int
    actionable: bool

    def __post_init__(self) -> None:
        _require_int(self.proposed_quantity, "proposed_quantity", minimum=0)
        if self.decision == TRANSFERABILITY_SUFFICIENT:
            if self.proposed_quantity != 0 or self.actionable:
                raise ReconciliationError(
                    "transferability decision may never make proposals "
                    "actionable or set a positive proposed quantity"
                )
        if self.remaining_gaps is not None and self.decision == TRANSFERABILITY_SUFFICIENT:
            if self.remaining_gaps:
                raise ReconciliationError(
                    "SUFFICIENT transferability must enumerate zero remaining gaps"
                )

    def to_dict(self) -> Dict[str, object]:
        return {
            "decision": str(self.decision),
            "remaining_gaps": list(self.remaining_gaps),
            "proposed_quantity": int(self.proposed_quantity),
            "actionable": bool(self.actionable),
        }


def decide_transferability(
    verdicts: Sequence[ReconciliationVerdict],
    gaps_closed: Sequence[str],
) -> TransferabilityDecision:
    """Decide transferability from reconciled verdicts and closed gaps.

    Pure decision function. A SUFFICIENT verdict requires ALL
    REQUIRED_TRANSFERABILITY_GAPS to be closed AND at least one
    RECONCILED dual-account verdict. This slice's evidence (one
    human-operated exchange) CANNOT close the failure/error-behavior
    gap, so the expected outcome is INSUFFICIENT; the function exists
    so the decision is explicit and tested, not inferred.
    """
    closed = set(str(gap) for gap in gaps_closed)
    missing = [gap for gap in REQUIRED_TRANSFERABILITY_GAPS if gap not in closed]
    has_reconciled = any(
        verdict.result == RESULT_RECONCILED for verdict in verdicts
    )
    if missing or not has_reconciled:
        return TransferabilityDecision(
            decision=TRANSFERABILITY_INSUFFICIENT,
            remaining_gaps=tuple(missing) if missing else REQUIRED_TRANSFERABILITY_GAPS,
            proposed_quantity=0,
            actionable=False,
        )
    return TransferabilityDecision(
        decision=TRANSFERABILITY_SUFFICIENT,
        remaining_gaps=(),
        proposed_quantity=0,
        actionable=False,
    )
