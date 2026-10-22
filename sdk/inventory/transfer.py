from __future__ import annotations

"""Task I: pure, deterministic cross-account transfer planner (READ-ONLY).

Pairs SOURCE supply with DESTINATION unmet demand across validated
AccountPortfolios, joined ONLY by ItemDesignId. Every proposal is a
non-actionable HOLD: the GiftItem endpoint has zero captures (corpus
scan, docs/task-i-reconciliation-t_97ae279f.md §4), so transferability
cannot be established from any repository-supported policy or verified
metadata. The fail-closed decision is therefore constant this slice:

    HOLD_UNVERIFIED_TRANSFERABILITY, quantity 0, actionable=false

for every proposal, unconditionally — even when source != destination,
quantities are positive, snapshots are fresh, no condition is
unresolved and no protection conflict exists.

PURE: no network, no filesystem, no clock. ``now`` is caller-supplied;
freshness uses the Task H single-owner snapshot_freshness with the
exported STALE_AFTER_SECONDS=86400 threshold.

JOIN KEY: ItemDesignId ONLY. ItemId is account-scoped and never
appears in any cross-account join, key or equality test. The planner
consumes per-(account, design) facts re-derived from each snapshot
(design rows, stack rows, conditions) - never a raw stack identity -
so no ItemId can enter the matching arithmetic or the output.

MATCHING ARITHMETIC (deterministic, input-order independent):

- Usable source supply for (account, design) = the account's per-design
  candidate total MINUS candidates on protected/unknown stacks. A Task G
  candidate is an informational dry-run number, never automatically
  transferable; a candidate on a protected/unknown stack is excluded
  because the Task D policy forbids moving protected items.
- Destination unmet demand = design.unmet_quantity > 0 (pinned by the
  loader as max(0, requested - observed)). Unmet is a demand-side
  observation; pairing it here is a PROPOSAL, never an instruction.
- For each design (ascending ItemDesignId), destinations are served in
  ascending user_id order; each destination is offered from every
  eligible source in ascending user_id order, taking
  min(remaining_source_supply, remaining_destination_deficit) per match.
  Both sides decrement across matches: nothing is oversubscribed
  (source never gives more than its supply) and nothing is
  double-filled (destination never receives more than its deficit).
- Quantity bounds: 0 <= matched <= source usable supply AND
  <= destination unmet demand, by construction of min().

ELIGIBILITY GATE (fail closed, evaluated per (source, destination,
design) match; ANY unmet requirement forces a HOLD row with the
specific reason codes, sorted lexicographically for determinism):

1. DISTINCT_ACCOUNTS — source user_id != destination user_id
   (self-transfer is structurally impossible: aggregate_snapshots
   aborts on duplicate user_id, but the gate stays explicit).
2. POSITIVE_SOURCE_SUPPLY — usable source supply > 0.
3. POSITIVE_DESTINATION_DEMAND — destination unmet > 0.
4. FRESH_SNAPSHOTS — BOTH the source and destination snapshots are
   fresh (snapshot_freshness, stale_after=STALE_AFTER_SECONDS).
   A stale side suppresses the proposal.
5. NO_APPLICABLE_UNRESOLVED_CONDITION — no unresolved condition for
   that design in EITHER the source's or the destination's OWN
   snapshot (conditions_for(design_id), design-scoped only — the
   contract the Task H portfolio itself uses for per-design
   unresolved_codes). ALL design-scoped condition codes are treated
   as applicable — the corpus documents no semantics under which any
   unresolved condition is safe to override, so none may be. (The
   account-global SOURCE_UNSUPPORTED emissions document unconfigured
   demand sources; they are account-level annotations, exactly as
   Task H treats them, and never design-blocking.) The real pipeline
   stamps DEMAND_EXCEEDS_SUPPLY design-scoped on every deficit
   destination, so real-data pairings always fail closed here.
6. NO_PROTECTION_CONFLICT — no protected/unknown stack of that design
   on the source account (see usable supply) and none on the
   destination account either (receiving a design the destination
   cannot safely hold is a protection conflict).
7. NO_EVIDENCE_PROMOTION — the proposal's evidence tier is
   Evidence.weakest over both sides' (account, design) inputs plus
   GIFTITEM_EVIDENCE_TIER. Because the GiftItem tier is
   STATICALLY_DISCOVERED at best, every proposal is at or below it;
   structurally satisfied by the weakest-input computation, so it is
   never stamped as an unmet reason.
8. TRANSFER_ELIGIBILITY_UNPROVEN — no repository-supported policy or
   verified metadata establishes transferability (GiftItem uncaptured:
   recipient param, quantity semantics, checksum preimage unknown).
   Constant this slice; the reason code is stamped on EVERY proposal.

Rows with matched==0 (supply exhausted, deficit filled, or a gate
HOLD) are still emitted for every attempted (destination, source)
pair, with the gate's hold reasons - the planner reports attempts,
never silently drops them. A pair failing the gate consumes NO
supply or deficit capacity.

Every TransferProposal carries actionable=False and the planner emits
no route, no endpoint, no executor, no retry, no sequencing: nothing
here can move any item anywhere.
"""

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from ._types import Evidence
from .portfolio import AccountPortfolio
from .portfolio_render import (
    STALE_AFTER_SECONDS,
    PortfolioViewError,
    snapshot_freshness,
)
from .snapshot import AccountSnapshot, SnapshotError, snapshot_from_dict

# Eligibility/hold state: the only state a proposal may carry this
# slice. ELIGIBLE exists in the vocabulary for the future
# capture-verified slice; the planner never assigns it (constant
# HOLD_UNVERIFIED_TRANSFERABILITY below).
ELIGIBLE = "ELIGIBLE"
HOLD_UNVERIFIED_TRANSFERABILITY = "HOLD_UNVERIFIED_TRANSFERABILITY"

# Reason codes (stable strings; lexicographic sort for determinism).
REASON_DISTINCT_ACCOUNTS = "REASON_DISTINCT_ACCOUNTS"
REASON_POSITIVE_SOURCE_SUPPLY = "REASON_POSITIVE_SOURCE_SUPPLY"
REASON_POSITIVE_DESTINATION_DEMAND = "REASON_POSITIVE_DESTINATION_DEMAND"
REASON_FRESH_SNAPSHOTS = "REASON_FRESH_SNAPSHOTS"
REASON_NO_APPLICABLE_UNRESOLVED_CONDITION = "REASON_NO_APPLICABLE_UNRESOLVED_CONDITION"
REASON_NO_PROTECTION_CONFLICT = "REASON_NO_PROTECTION_CONFLICT"
REASON_NO_EVIDENCE_PROMOTION = "REASON_NO_EVIDENCE_PROMOTION"
REASON_TRANSFER_ELIGIBILITY_UNPROVEN = "REASON_TRANSFER_ELIGIBILITY_UNPROVEN"

# Protocol-evidence floor for the only candidate transfer endpoint
# (ItemService/GiftItem, statically discovered, template truncated).
# Weakest-input propagation: this tier enters EVERY proposal.
GIFTITEM_EVIDENCE_TIER = Evidence.STATICALLY_DISCOVERED

# Every unresolved condition code is applicable (fail closed): the
# corpus documents no semantics under which a condition is
# overridable. Listed explicitly from the snapshot vocabulary so a
# new condition code added to demand.py is ALSO treated as applicable
# (the gate fails closed on the union, not the enumerated set).
_APPLICABLE_CONDITION_CODES: frozenset = frozenset(
    {
        "BOTH_EQUIPPED_AND_ROOM_ATTACHED",
        "CRAFT_OBJECTIVE_NOT_CRAFTABLE",
        "DEMAND_EXCEEDS_SUPPLY",
        "DUPLICATE_CONFIG_ENTRY",
        "DUPLICATE_INGREDIENT",
        "EQUIPMENT_SHARED_STACK",
        "MALFORMED_QUANTITY",
        "MODULE_MULTI_ATTACH",
        "RECIPE_CYCLE",
        "SOURCE_UNAVAILABLE",
        "SOURCE_UNSUPPORTED",
        "UNKNOWN_DESIGN",
        "UNKNOWN_ITEM_REFERENCE",
        "UNREASONABLE_QUANTITY",
    }
)

class TransferPlannerError(PortfolioViewError):
    """Fail-closed transfer-planning error.

    Subclasses the Task H view error so callers that already catch
    snapshot/portfolio corruption get the same fail-closed channel for
    malformed planner inputs (non-snapshot input, unparseable
    captured_at, future-dated captured_at, unknown evidence tier).
    Never a partial plan: one bad input refuses the whole plan.
    """


@dataclass(frozen=True)
class TransferProposal:
    """One non-actionable cross-account proposal row (READ-ONLY).

    ``proposed_quantity`` is what the plan proposes to move: ALWAYS 0
    this slice (transferability unproven, fail closed). The
    eligibility state is ALWAYS HOLD_UNVERIFIED_TRANSFERABILITY.
    ``matched_quantity`` is the informational arithmetic pairing
    min(supply, deficit) - a dry-run number that is NOT a proposed
    move; it is 0 whenever any gate requirement is unmet.
    ``reasons`` are the gate's UNMET requirement codes plus the
    constant transferability hold, sorted lexicographically.
    ``actionable`` is ALWAYS False.
    """

    item_design_id: int
    source_user_id: int
    destination_user_id: int
    proposed_quantity: int
    matched_quantity: int
    evidence: str
    source_fresh: bool
    destination_fresh: bool
    eligibility_state: str
    reasons: Tuple[str, ...]
    actionable: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {
            "item_design_id": int(self.item_design_id),
            "source_user_id": int(self.source_user_id),
            "destination_user_id": int(self.destination_user_id),
            "proposed_quantity": int(self.proposed_quantity),
            "matched_quantity": int(self.matched_quantity),
            "evidence": str(self.evidence),
            "source_fresh": bool(self.source_fresh),
            "destination_fresh": bool(self.destination_fresh),
            "eligibility_state": str(self.eligibility_state),
            "reasons": list(self.reasons),
            "actionable": bool(self.actionable),
        }


@dataclass(frozen=True)
class TransferPlan:
    """Deterministic read-only output of plan_transfers.

    ``proposals`` sorted by (item_design_id, destination_user_id,
    source_user_id). ``actionable`` is False for the whole plan and
    every proposal; ``actionable_proposals()`` is structurally empty
    (proof surface for tests and the verifier).
    """

    proposals: Tuple[TransferProposal, ...] = ()
    actionable: bool = False

    def actionable_proposals(self) -> Tuple[TransferProposal, ...]:
        """Always empty — proof surface for tests."""
        return ()

    def to_dicts(self) -> List[Dict[str, object]]:
        return [row.to_dict() for row in self.proposals]


def _safe_evidence(tier: object) -> Evidence:
    """Evidence member or fail-closed planner error (never a guess)."""
    try:
        return Evidence(str(tier))
    except ValueError as exc:
        raise TransferPlannerError(f"unknown evidence tier {tier!r}") from exc


def _proposal_evidence(
    source: AccountSnapshot,
    destination: AccountSnapshot,
    design_id: int,
) -> str:
    """Weakest-input evidence tier for one proposal.

    Inputs: the GiftItem protocol floor plus, for EACH side, that
    design's row evidence and every stack's evidence and candidate
    evidence for the SAME design (all stacks, protected included:
    exclusion could only strengthen the tier, i.e. promote). Never
    promoted above the weakest input; per-account inputs never bleed
    across designs or accounts.
    """
    key = int(design_id)
    tiers: List[str] = [GIFTITEM_EVIDENCE_TIER.value]
    for snapshot in (source, destination):
        tiers.extend(
            design.evidence
            for design in snapshot.designs
            if design.item_design_id == key
        )
        for stack in snapshot.stacks_for_design(key):
            tiers.append(stack.evidence)
            tiers.append(stack.candidate_evidence)
    return Evidence.weakest(
        [_safe_evidence(tier) for tier in tiers if tier]
    ).value


def _usable_supply(snapshot: AccountSnapshot, design_id: int) -> int:
    """Usable source supply: per-design candidate total minus
    candidates on protected/unknown stacks (Task D policy: protected
    items are never movement candidates). Never negative; never above
    the account's candidate total."""
    key = int(design_id)
    total = 0
    excluded = 0
    for stack in snapshot.stacks_for_design(key):
        total += stack.candidate_quantity
        if stack.protected or stack.unknown:
            excluded += stack.candidate_quantity
    usable = total - excluded
    if usable < 0:
        # Loader-legal but producer-impossible ordering; fail closed
        # rather than silently clamping a contradiction.
        raise TransferPlannerError(
            f"source user {snapshot.user_id} design {key}: protected-stack "
            f"candidates {excluded} exceed total candidates {total}"
        )
    return usable


def _destination_deficit(snapshot: AccountSnapshot, design_id: int) -> int:
    """Destination unmet demand for a design (pinned by the loader as
    max(0, requested - observed)). Zero when the account holds no
    design row for it."""
    row = snapshot.design_row(design_id)
    if row is None:
        return 0
    return int(row.unmet_quantity)


def _design_conditions(snapshot: AccountSnapshot, design_id: int) -> Tuple[str, ...]:
    """Condition codes applicable to (account, design): the account's
    OWN design-scoped conditions for that design, exactly as pinned
    by the reconcile contract (docs/task-i-reconciliation-
    t_97ae279f.md section 3: "conditions_for(design_id)") and by the
    Task H portfolio precedent (DesignPortfolioRow.unresolved_codes
    pools only design-scoped conditions; account-global conditions
    are account-level annotations, never design-blocking).

    The real pipeline stamps DEMAND_EXCEEDS_SUPPLY design-scoped on
    every account whose demand could not be held against supply, so a
    real deficit destination always carries an applicable unresolved
    condition and its pairings always fail closed to matched 0 - the
    intended conservative behavior. Sorted for determinism."""
    key = int(design_id)
    codes = {condition.code for condition in snapshot.conditions_for(key)}
    return tuple(sorted(codes))


def _protected_conflict(snapshot: AccountSnapshot, design_id: int) -> bool:
    """True when the account holds any protected/unknown stack of the
    design. On the SOURCE this means usable supply excludes those
    stacks; on the DESTINATION it means the account cannot safely
    receive the design (protection conflict)."""
    return any(
        stack.protected or stack.unknown
        for stack in snapshot.stacks_for_design(int(design_id))
    )


def _fresh(
    snapshot: AccountSnapshot, now: str, stale_after: int
) -> bool:
    """Freshness annotation (never changes evidence; a stale side
    suppresses a positive proposal). Freshness computation failures
    (unparseable or future captured_at) surface as TransferPlannerError:
    the whole plan refuses, never a partial plan over fresh accounts."""
    try:
        return snapshot_freshness(
            snapshot.captured_at, now, stale_after=stale_after
        ).fresh
    except PortfolioViewError as exc:
        raise TransferPlannerError(str(exc)) from exc


def _gate_reasons(
    source: AccountSnapshot,
    destination: AccountSnapshot,
    design_id: int,
    usable_supply: int,
    deficit: int,
    source_fresh: bool,
    destination_fresh: bool,
) -> Tuple[str, ...]:
    """Hold reason codes for every UNMET gate requirement (sorted).

    Requirement 7 (no evidence promotion) is structurally satisfied
    by _proposal_evidence and can never be unmet, so it is never
    stamped as a reason. Requirement 8 (transfer eligibility
    unproven) is the constant hold stamped on every proposal.
    """
    reasons: List[str] = []
    if source.user_id == destination.user_id:
        reasons.append(REASON_DISTINCT_ACCOUNTS)
    if usable_supply <= 0:
        reasons.append(REASON_POSITIVE_SOURCE_SUPPLY)
    if deficit <= 0:
        reasons.append(REASON_POSITIVE_DESTINATION_DEMAND)
    if not source_fresh or not destination_fresh:
        reasons.append(REASON_FRESH_SNAPSHOTS)
    if _design_conditions(source, design_id) or _design_conditions(
        destination, design_id
    ):
        reasons.append(REASON_NO_APPLICABLE_UNRESOLVED_CONDITION)
    if _protected_conflict(source, design_id) or _protected_conflict(
        destination, design_id
    ):
        reasons.append(REASON_NO_PROTECTION_CONFLICT)
    reasons.append(REASON_TRANSFER_ELIGIBILITY_UNPROVEN)
    return tuple(sorted(set(reasons)))


def plan_transfers(
    portfolio: AccountPortfolio,
    now: str,
    stale_after: int = STALE_AFTER_SECONDS,
) -> TransferPlan:
    """Plan cross-account transfer PROPOSALS over a validated
    portfolio (pure, deterministic, input-order independent).

    Re-validation discipline identical to aggregate_snapshots: every
    snapshot is re-validated through the loader gates, duplicate
    user_ids abort the whole plan, and accounts are processed in
    ascending user_id order. ``now`` is caller-supplied (no clock
    reads here). Output is ALWAYS non-actionable: every proposal
    carries HOLD_UNVERIFIED_TRANSFERABILITY, proposed quantity 0,
    actionable=false. Nothing here executes, retries, sequences or
    reaches any endpoint.
    """
    if not isinstance(portfolio, AccountPortfolio):
        raise TransferPlannerError(
            f"planner input must be AccountPortfolio, got {type(portfolio).__name__}"
        )
    if not isinstance(stale_after, int) or isinstance(stale_after, bool):
        raise TransferPlannerError(
            f"stale_after must be an integer number of seconds, got {stale_after!r}"
        )
    if stale_after < 0:
        raise TransferPlannerError(
            f"stale_after must be non-negative, got {stale_after}"
        )

    # Re-validation + duplicate detection + deterministic ordering.
    by_user: Dict[int, AccountSnapshot] = {}
    for snapshot in portfolio.accounts:
        if not isinstance(snapshot, AccountSnapshot):
            raise TransferPlannerError(
                "portfolio account must be AccountSnapshot, got "
                f"{type(snapshot).__name__}"
            )
        try:
            # to_dict() of a loader-valid snapshot always parses; a
            # forged/hand-built one fails the same fail-closed gates
            # the persisted-file path enforces.
            snapshot_from_dict(snapshot.to_dict())
        except SnapshotError as exc:
            raise TransferPlannerError(
                f"planner input for user {snapshot.identity.user_id} failed "
                f"re-validation: {exc}"
            ) from exc
        user_id = snapshot.identity.user_id
        if user_id in by_user:
            raise TransferPlannerError(
                f"contradictory planner inputs: two snapshots for user_id "
                f"{user_id} (fail closed; no partial plan)"
            )
        by_user[user_id] = snapshot
    snapshots = [by_user[uid] for uid in sorted(by_user)]
    if not snapshots:
        return TransferPlan(proposals=(), actionable=False)

    # Freshness for every snapshot up front; ANY problem (unparseable
    # or future captured_at) refuses the whole plan (fail closed).
    fresh: Dict[int, bool] = {
        snapshot.user_id: _fresh(snapshot, now, stale_after)
        for snapshot in snapshots
    }

    proposals: List[TransferProposal] = []

    # Design rows ascending (portfolio rows are already ascending;
    # sorted() makes the planner independent of that assumption).
    for row in sorted(portfolio.rows, key=lambda r: r.item_design_id):
        design_id = row.item_design_id
        # PARTITION RULE: an account with unmet demand on this design
        # is never a source for it (donating a design you are short
        # of is nonsensical, and a self-pairing must never consume
        # arithmetic capacity). A source must also HOLD the design
        # (stacks exist) for the pairing to be meaningful.
        dest_snapshots = [
            s for s in snapshots if _destination_deficit(s, design_id) > 0
        ]
        source_snapshots = [
            s
            for s in snapshots
            if _destination_deficit(s, design_id) <= 0
            and s.stacks_for_design(design_id)
        ]
        if not dest_snapshots or not source_snapshots:
            continue

        # Per-source usable supply (decrements across matches; a
        # contradiction aborts the whole plan, never partial).
        supply: Dict[int, int] = {
            s.user_id: _usable_supply(s, design_id) for s in source_snapshots
        }
        # Per-destination deficit (decrements across matches).
        deficit: Dict[int, int] = {
            s.user_id: _destination_deficit(s, design_id) for s in dest_snapshots
        }

        for destination in dest_snapshots:  # ascending user_id
            for source in source_snapshots:  # ascending user_id
                reasons = _gate_reasons(
                    source,
                    destination,
                    design_id,
                    supply[source.user_id],
                    deficit[destination.user_id],
                    fresh[source.user_id],
                    fresh[destination.user_id],
                )
                # Structural (non-constant) hold reasons: the gate
                # failed. The pair consumes NO capacity (a stale or
                # conditioned pairing must not eat the supply a
                # fresh pairing could use) and matched stays 0.
                structural = [
                    r
                    for r in reasons
                    if r != REASON_TRANSFER_ELIGIBILITY_UNPROVEN
                ]
                matched = 0
                if not structural:
                    matched = min(
                        supply[source.user_id], deficit[destination.user_id]
                    )
                    # Informational pairing: decrement BOTH sides so
                    # later matches see the remainder (no
                    # oversubscription, no double-fill).
                    supply[source.user_id] -= matched
                    deficit[destination.user_id] -= matched
                proposals.append(
                    TransferProposal(
                        item_design_id=design_id,
                        source_user_id=source.user_id,
                        destination_user_id=destination.user_id,
                        proposed_quantity=0,
                        matched_quantity=matched,
                        evidence=_proposal_evidence(
                            source, destination, design_id
                        ),
                        source_fresh=fresh[source.user_id],
                        destination_fresh=fresh[destination.user_id],
                        eligibility_state=HOLD_UNVERIFIED_TRANSFERABILITY,
                        reasons=reasons,
                        actionable=False,
                    )
                )

    proposals.sort(
        key=lambda p: (
            p.item_design_id,
            p.destination_user_id,
            p.source_user_id,
        )
    )
    return TransferPlan(proposals=tuple(proposals), actionable=False)


def render_transfers_view(
    snapshots: Sequence[AccountSnapshot], now: str
) -> str:
    """Render `inventory transfers`: read-only cross-account pairing.

    Shows, per proposal (ascending design, destination, source):
    source account, destination account, ItemDesignId, proposed
    quantity (always 0 this slice: transferability unproven),
    matched arithmetic (informational), weakest-input evidence,
    per-side freshness, eligibility/hold state, hold reasons, and
    actionable=false. The GiftItem capture gap is documented in the
    footer as the named blocker for any future actionable transfer
    planning.
    """
    from .portfolio import aggregate_snapshots

    if not snapshots:
        return (
            "No account snapshots found. Nothing to report: this view "
            "reads persisted account-snapshot files (schema v1, written "
            "by the sdk.inventory build_snapshot API) from the configured "
            "snapshots directory, and none are present. This view is "
            "strictly read-only; no game state is read or modified here."
        )
    portfolio = aggregate_snapshots(snapshots)
    plan = plan_transfers(portfolio, now=now)

    stale = [
        snapshot.user_id
        for snapshot in portfolio.accounts
        if not snapshot_freshness(snapshot.captured_at, now).fresh
    ]
    lines = [
        "=== Inventory transfers (READ-ONLY Task I — cross-account "
        "pairing proposals; nothing executable) ===",
        f"Accounts: {len(portfolio.accounts)}; proposed pairings: "
        f"{len(plan.proposals)}. Every proposal is a non-actionable "
        "HOLD: the GiftItem endpoint has zero captures, so "
        "transferability cannot be established from repository-"
        "supported policy or verified metadata. Proposed quantity is "
        "0 for every row by construction.",
        "Candidate does not mean transferable. Accounts are matched "
        "only by ItemDesignId; ItemId is never cross-account "
        "identity. STALE markers annotate snapshot age and never "
        "change any evidence tier.",
        "",
    ]
    for proposal in plan.proposals:
        source_marker = "fresh" if proposal.source_fresh else "STALE"
        dest_marker = "fresh" if proposal.destination_fresh else "STALE"
        reasons = ", ".join(proposal.reasons) if proposal.reasons else "-"
        lines.append(
            f"  design {proposal.item_design_id} | source "
            f"{proposal.source_user_id} ({source_marker}) -> destination "
            f"{proposal.destination_user_id} ({dest_marker}) | proposed "
            f"{proposal.proposed_quantity} | matched (informational) "
            f"{proposal.matched_quantity} | evidence {proposal.evidence} | "
            f"{proposal.eligibility_state} | reasons: {reasons} | "
            "actionable=false"
        )
    if not plan.proposals:
        lines.append(
            "No cross-account pairing exists for the loaded snapshots "
            "(no design has both a source with usable dry-run "
            "candidates and a destination with unmet demand)."
        )
    lines.append("")
    if stale:
        lines.append(
            "Stale accounts (no positive proposal may involve them): "
            + ", ".join(str(user_id) for user_id in sorted(stale))
            + "."
        )
    lines.append(
        "Transfer execution is not implemented: GiftItem is "
        "STATICALLY_DISCOVERED only (URL template truncated at "
        "'?itemId='; recipient parameter, quantity semantics and "
        "checksum preimage are unknown) with zero captures in the "
        "corpus. Unblock condition: capture + dual-account reconcile "
        "(docs/task-i-reconciliation-t_97ae279f.md). All Task-I "
        "output is actionable=false and 'inventory apply' remains "
        "refused."
    )
    return "\n".join(lines)
