from __future__ import annotations

"""Task H: pure, deterministic AccountPortfolio aggregation (READ-ONLY).

Aggregates N per-account AccountSnapshots by ItemDesignId. Read-only by
construction: it never mutates its inputs, never writes disk, never
touches the network, and carries no executor, transfer, marketplace or
mutation semantics anywhere.

SCOPE (Task H brief, hard boundaries):

- Aggregation reports per-account PROVENANCE: which accounts hold which
  quantity of each design, with per-account requested/reserved/unmet
  demand, Task G candidate quantities, weakest-input evidence and
  unresolved conditions.
- "Candidate does not mean transferable": a candidate quantity on one
  account is an informational dry-run number ONLY. This module performs
  NO donor/recipient matching, NO transfer planning, NO GiftItem /
  SellItem3 / Purchase4 semantics, and NO destination selection. Every
  deficit row is an observed fact about ONE account's demand, never an
  instruction to move anything anywhere.
- Deficits are demand-side unmet quantities (requested-but-not-held
  against the account's own observed supply). They are reported, never
  resolved here.
- Fail closed: an invalid snapshot (wrong version, corrupt, duplicated
  account or identity) aborts the WHOLE aggregation with
  PortfolioError - never a partial portfolio over the "good" accounts.

EVIDENCE: a design row's tier is the weakest across every input that
contributed to it (gate Q4): every stack row's evidence, every design
row's evidence, and every ledger-derived condition tier. Never promoted.

DETERMINISM: identical snapshots (in any input order) produce
byte-identical portfolios. Design rows sort ascending ItemDesignId;
per-account entries sort ascending user_id; conditions sort by
(code, detail, design_id). No clocks, randomness, dict-order or
set-order dependence.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from ._types import Evidence
from .snapshot import (
    AccountSnapshot,
    SnapshotDesignRow,
    SnapshotError,
    snapshot_from_dict,
)

# PortfolioError subclasses SnapshotError so callers that already catch
# snapshot corruption get the same fail-closed channel for aggregation
# problems (duplicate accounts, contradictory identities).
class PortfolioError(SnapshotError):
    """Fail-closed portfolio aggregation error."""


@dataclass(frozen=True)
class AccountContribution:
    """One account's provenance entry inside a design's portfolio row.

    candidate_quantity is that account's Task G dry-run total for the
    design (informational only - NEVER a transferable quantity). All
    other fields are read from the account's snapshot verbatim.
    """

    user_id: int
    display_name: str
    stacks: int
    observed_quantity: int
    requested_quantity: int
    reserved_quantity: int
    unmet_quantity: int
    candidate_quantity: int
    evidence: str
    conditions: Tuple[str, ...]

    def to_dict(self) -> Dict[str, object]:
        return {
            "user_id": int(self.user_id),
            "display_name": str(self.display_name),
            "stacks": int(self.stacks),
            "observed_quantity": int(self.observed_quantity),
            "requested_quantity": int(self.requested_quantity),
            "reserved_quantity": int(self.reserved_quantity),
            "unmet_quantity": int(self.unmet_quantity),
            "candidate_quantity": int(self.candidate_quantity),
            "evidence": str(self.evidence),
            "conditions": list(self.conditions),
        }


@dataclass(frozen=True)
class DesignPortfolioRow:
    """Portfolio state for ONE ItemDesignId across all accounts.

    ``accounts`` carries per-account provenance (ascending user_id).
    ``evidence`` is the weakest tier across all inputs. ``deficits`` is
    the list of accounts whose demand exceeds their own observed supply
    (unmet_quantity > 0) - an observed fact, never a transfer plan.
    ``actionable`` is ALWAYS False.
    """

    item_design_id: int
    item_name: str
    accounts: Tuple[AccountContribution, ...]
    total_observed: int
    total_requested: int
    total_reserved: int
    total_unmet: int
    total_candidates: int
    evidence: str
    deficit_accounts: Tuple[int, ...]
    unresolved_codes: Tuple[str, ...]
    actionable: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {
            "item_design_id": int(self.item_design_id),
            "item_name": str(self.item_name),
            "accounts": [a.to_dict() for a in self.accounts],
            "total_observed": int(self.total_observed),
            "total_requested": int(self.total_requested),
            "total_reserved": int(self.total_reserved),
            "total_unmet": int(self.total_unmet),
            "total_candidates": int(self.total_candidates),
            "evidence": str(self.evidence),
            "deficit_accounts": list(self.deficit_accounts),
            "unresolved_codes": list(self.unresolved_codes),
            "actionable": bool(self.actionable),
        }


@dataclass(frozen=True)
class AccountPortfolio:
    """Deterministic read-only multi-account aggregate.

    Built ONLY via aggregate_snapshots(); every field is derived state
    with no mutation surface. ``actionable`` is False for the whole
    portfolio and for every row (Task H: all output actionable=false).
    """

    accounts: Tuple[AccountSnapshot, ...]
    rows: Tuple[DesignPortfolioRow, ...] = ()
    actionable: bool = False

    @property
    def account_ids(self) -> Tuple[int, ...]:
        return tuple(s.identity.user_id for s in self.accounts)

    def row_for(self, design_id: int) -> Optional[DesignPortfolioRow]:
        key = int(design_id)
        for row in self.rows:
            if row.item_design_id == key:
                return row
        return None

    def rows_with_deficits(self) -> Tuple[DesignPortfolioRow, ...]:
        """Designs where at least one account has unmet demand.

        Read-only reporting surface (the future `inventory deficits`
        view consumes exactly this). Carries no matching or proposal
        semantics.
        """
        return tuple(r for r in self.rows if r.deficit_accounts)

    def rows_with_candidates(self) -> Tuple[DesignPortfolioRow, ...]:
        """Designs where at least one account has a dry-run candidate
        quantity above zero. Informational only."""
        return tuple(r for r in self.rows if r.total_candidates > 0)

    def actionable_rows(self) -> Tuple[DesignPortfolioRow, ...]:
        """Always empty - proof surface for tests."""
        return ()

    def to_dicts(self) -> List[Dict[str, object]]:
        return [row.to_dict() for row in self.rows]


def aggregate_snapshots(
    snapshots: Sequence[AccountSnapshot],
) -> AccountPortfolio:
    """Aggregate snapshots into a portfolio (pure, deterministic).

    Input order never matters: snapshots are keyed by user_id and
    processed in ascending order; duplicate user_ids are a
    contradiction and abort fail-closed (two on-disk artifacts claiming
    to be the same account cannot both be authoritative).

    Every input is RE-VALIDATED through the loader's full gate battery
    (snapshot_from_dict(snapshot.to_dict())): a hand-constructed
    AccountSnapshot that never passed the loader cannot bypass the
    schema/vocabulary/arithmetic gates by entering here directly.
    """
    by_user: Dict[int, AccountSnapshot] = {}
    for snapshot in snapshots:
        if not isinstance(snapshot, AccountSnapshot):
            raise PortfolioError(
                f"portfolio input must be AccountSnapshot, got {type(snapshot).__name__}"
            )
        try:
            # Re-validate: to_dict() of a valid snapshot always parses;
            # a forged/hand-built one fails the same fail-closed gates
            # the persisted-file path enforces (version, schema keys,
            # vocabulary, evidence cap, arithmetic, couplings, secrets).
            snapshot_from_dict(snapshot.to_dict())
        except SnapshotError as exc:
            raise PortfolioError(
                f"portfolio input for user {snapshot.identity.user_id} failed "
                f"re-validation: {exc}"
            ) from exc
        user_id = snapshot.identity.user_id
        existing = by_user.get(user_id)
        if existing is not None:
            if existing is snapshot:
                raise PortfolioError(
                    f"the same snapshot for user {user_id} was passed twice "
                    "(duplicate account input)"
                )
            raise PortfolioError(
                f"contradictory portfolio inputs: two different snapshots for "
                f"user_id {user_id} (fail closed; no partial aggregation)"
            )
        by_user[user_id] = snapshot

    ordered = [by_user[uid] for uid in sorted(by_user)]

    # design_id -> list of (snapshot, design_row)
    design_rows: Dict[int, List[Tuple[AccountSnapshot, SnapshotDesignRow]]] = {}
    # design_id -> set of unresolved condition codes across accounts
    design_conditions: Dict[int, set] = {}
    # design_id -> first non-empty item name (deterministic: ascending user_id)
    design_names: Dict[int, str] = {}

    for snapshot in ordered:
        for design in snapshot.designs:
            design_rows.setdefault(design.item_design_id, []).append((snapshot, design))
            if design.item_name:
                existing_name = design_names.get(design.item_design_id)
                if not existing_name:
                    design_names[design.item_design_id] = design.item_name
        for condition in snapshot.conditions:
            if condition.design_id is not None:
                design_conditions.setdefault(condition.design_id, set()).add(condition.code)

    # Per-account, per-design condition codes: a contribution row must
    # carry ONLY the conditions its OWN snapshot recorded for that
    # design. (Pooling design_conditions across accounts and stamping
    # every contribution with the union would attribute account A's
    # shortfall condition to account B - a provenance bug.)
    account_design_conditions: Dict[Tuple[int, int], Tuple[str, ...]] = {}
    for snapshot in ordered:
        design_ids_in_snapshot = {d.item_design_id for d in snapshot.designs}
        for design_id in design_ids_in_snapshot:
            codes = tuple(
                sorted(
                    {
                        condition.code
                        for condition in snapshot.conditions_for(design_id)
                    }
                )
            )
            account_design_conditions[(snapshot.identity.user_id, design_id)] = codes

    rows: List[DesignPortfolioRow] = []
    for design_id in sorted(design_rows):
        entries: List[AccountContribution] = []
        tiers: List[str] = []
        total_observed = 0
        total_requested = 0
        total_reserved = 0
        total_unmet = 0
        total_candidates = 0
        deficit_accounts: List[int] = []
        unresolved_codes: set = set(design_conditions.get(design_id, ()))

        for snapshot, design in design_rows[design_id]:
            stack_rows = snapshot.stacks_for_design(design_id)
            candidate_total = sum(s.candidate_quantity for s in stack_rows)
            stack_tiers = [s.candidate_evidence for s in stack_rows] + [
                s.evidence for s in stack_rows
            ]
            own_conditions = account_design_conditions.get(
                (snapshot.identity.user_id, design_id), ()
            )
            entries.append(
                AccountContribution(
                    user_id=snapshot.identity.user_id,
                    display_name=snapshot.identity.display_name,
                    stacks=len(stack_rows),
                    observed_quantity=design.observed_quantity,
                    requested_quantity=design.requested_quantity,
                    reserved_quantity=design.reserved_quantity,
                    unmet_quantity=design.unmet_quantity,
                    candidate_quantity=candidate_total,
                    evidence=design.evidence,
                    conditions=own_conditions,
                )
            )
            tiers.extend(stack_tiers)
            tiers.append(design.evidence)
            total_observed += design.observed_quantity
            total_requested += design.requested_quantity
            total_reserved += design.reserved_quantity
            total_unmet += design.unmet_quantity
            total_candidates += candidate_total
            if design.unmet_quantity > 0:
                deficit_accounts.append(snapshot.identity.user_id)

        evidence = Evidence.weakest(
            [Evidence(tier) for tier in tiers if tier]
        ).value if tiers else Evidence.UNKNOWN.value

        entries.sort(key=lambda e: e.user_id)
        rows.append(
            DesignPortfolioRow(
                item_design_id=design_id,
                item_name=design_names.get(design_id, ""),
                accounts=tuple(entries),
                total_observed=total_observed,
                total_requested=total_requested,
                total_reserved=total_reserved,
                total_unmet=total_unmet,
                total_candidates=total_candidates,
                evidence=evidence,
                deficit_accounts=tuple(sorted(set(deficit_accounts))),
                unresolved_codes=tuple(sorted(unresolved_codes)),
                actionable=False,
            )
        )

    return AccountPortfolio(
        accounts=tuple(ordered),
        rows=tuple(rows),
        actionable=False,
    )
