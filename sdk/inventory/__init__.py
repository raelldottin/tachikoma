from __future__ import annotations

"""Inventory-domain package (read-only).

Public surface only. One-way dependency: sdk.inventory imports transport
helpers from sdk.client; sdk.client never imports sdk.inventory.

Scope: design feed normalization (Slice 1 / Task B), catalog
normalization (Task C), checksum helpers, Task D classification and
protection, Task F demand/reservation planning, Task G dry-run
reduction planning (``reduction.py``: bounded candidates, informational
disposition hints, actionable=false everywhere), and Task H versioned
account snapshots plus pure multi-account portfolio aggregation
(``snapshot.py`` / ``portfolio.py``: non-secret, deterministic,
fail-closed on corruption, read-only, actionable=false everywhere).
Task I adds the pure, read-only cross-account transfer planner
(``transfer.py``: accounts joined only by ItemDesignId; every proposal
is a non-actionable HOLD_UNVERIFIED_TRANSFERABILITY with proposed
quantity 0, actionable=false). Task J adds pure evidence tools only
(``sanitize.py``: capture-record sanitization; ``reconcile.py``:
dual-account delta reconciliation, evidence-tier ceiling accounting
and fail-closed transferability decisions) — analysis surfaces, not
execution. Transfer executor and marketplace execution remain later
tasks and intentionally absent; no mutation planning exists —
candidates are informational and never imply a transferable quantity.
"""

from ._types import (
    CAPTURED_ITEM_ATTRIBUTES,
    Classification,
    ClassifiedItem,
    Disposition,
    Evidence,
    Item,
    ItemDesign,
    ProtectionContext,
    Reservation,
    ReservationSource,
    Role,
    ROLES_FORBIDDEN_THIS_SLICE,
)
from .checksums import (
    checksum_activate_item4,
    checksum_consume_item2,
    checksum_mark_items_not_new,
    checksum_purchase4,
    checksum_sell_item3,
)
from .classify import InventoryClassifier, classify_catalog
from .demand import (
    BOTH_EQUIPPED_AND_ROOM_ATTACHED,
    CRAFT_OBJECTIVE_NOT_CRAFTABLE,
    DEMAND_EXCEEDS_SUPPLY,
    DUPLICATE_CONFIG_ENTRY,
    DUPLICATE_INGREDIENT,
    EQUIPMENT_SHARED_STACK,
    MALFORMED_QUANTITY,
    MODULE_MULTI_ATTACH,
    RECIPE_CYCLE,
    SOURCE_UNAVAILABLE,
    SOURCE_UNSUPPORTED,
    UNKNOWN_DESIGN,
    UNKNOWN_ITEM_REFERENCE,
    UNREASONABLE_QUANTITY,
    DemandPlanner,
    PlanningContext,
    ReservationEntry,
    ReservationLedger,
    UnresolvedDemand,
)
from .designs import DesignCatalog, parse_ingredients, parse_item_design
from .catalog import DuplicateItemIdError, InventoryCatalog, parse_item
from .policy import ProtectedItemPolicy, policy_limitations
from .plan_render import render_inventory_plan
from .reduction import (
    BLOCKED_ALLOCATION_EXHAUSTED,
    BLOCKED_DESIGN_CONDITION,
    BLOCKED_MALFORMED_ROW,
    BLOCKED_OVER_RESERVED,
    BLOCKED_PROTECTED_OR_UNKNOWN,
    BLOCKED_STACK_CONDITION,
    BLOCKED_STRATEGIC_ROLE,
    BLOCKED_UNSUPPORTED_DEMAND,
    BLOCKED_ZERO_QUANTITY,
    BLOCKED_ZERO_UNRESERVED,
    ReductionPlanner,
    ReductionPlan,
    ReductionResult,
    STATE_CANDIDATE,
    STATE_HOLD,
)
from .snapshot import (
    FORBIDDEN_SNAPSHOT_KEYS,
    SNAPSHOT_VERSION,
    AccountSnapshot,
    SnapshotAccountIdentity,
    SnapshotCondition,
    SnapshotDesignRow,
    SnapshotError,
    SnapshotStackRow,
    build_snapshot,
    snapshot_from_dict,
    snapshot_from_json,
)
from .portfolio_render import (
    STALE_AFTER_SECONDS,
    PortfolioViewError,
    SnapshotFreshness,
    render_accounts_view,
    render_deficits_view,
    snapshot_freshness,
)
from .portfolio import (
    AccountContribution,
    AccountPortfolio,
    DesignPortfolioRow,
    PortfolioError,
    aggregate_snapshots,
)
from .transfer import (
    ELIGIBLE,
    GIFTITEM_EVIDENCE_TIER,
    HOLD_UNVERIFIED_TRANSFERABILITY,
    REASON_DISTINCT_ACCOUNTS,
    REASON_FRESH_SNAPSHOTS,
    REASON_NO_APPLICABLE_UNRESOLVED_CONDITION,
    REASON_NO_EVIDENCE_PROMOTION,
    REASON_NO_PROTECTION_CONFLICT,
    REASON_POSITIVE_DESTINATION_DEMAND,
    REASON_POSITIVE_SOURCE_SUPPLY,
    REASON_TRANSFER_ELIGIBILITY_UNPROVEN,
    TransferPlan,
    TransferPlannerError,
    TransferProposal,
    plan_transfers,
    render_transfers_view,
)

from .sanitize import (
    SanitizationError,
    sanitize_body,
    sanitize_capture_line,
    sanitize_capture_record,
    sanitize_headers,
    sanitize_query_string,
    scan_for_secrets,
)
from .reconcile import (
    GiftItemExchange,
    InventoryDelta,
    InventoryObservation,
    ReconciliationError,
    ReconciliationVerdict,
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

# Safe-by-default inventory configuration (AD-8: an absent/empty config
# section behaves identically to defaults). Consumed by the read-only
# command surface in this slice; later tasks extend it.
DEFAULT_INVENTORY_CONFIG = {
    "enabled": False,  # read-only slice: no setting enables any mutation
    "strategic_reserve": {},
    "training_reserve_tiers": 0,
    "equipment_reserve": "best_per_slot",
    "marketplace_min_confidence": "VERIFIED",
    "starbux_cap_per_run": 0,
}

__all__ = [
    "BOTH_EQUIPPED_AND_ROOM_ATTACHED",
    "BLOCKED_ALLOCATION_EXHAUSTED",
    "BLOCKED_DESIGN_CONDITION",
    "BLOCKED_MALFORMED_ROW",
    "BLOCKED_OVER_RESERVED",
    "BLOCKED_PROTECTED_OR_UNKNOWN",
    "BLOCKED_STACK_CONDITION",
    "BLOCKED_STRATEGIC_ROLE",
    "BLOCKED_UNSUPPORTED_DEMAND",
    "BLOCKED_ZERO_QUANTITY",
    "BLOCKED_ZERO_UNRESERVED",
    "CAPTURED_ITEM_ATTRIBUTES",
    "Classification",
    "ClassifiedItem",
    "CRAFT_OBJECTIVE_NOT_CRAFTABLE",
    "DEMAND_EXCEEDS_SUPPLY",
    "DEFAULT_INVENTORY_CONFIG",
    "ELIGIBLE",
    "DesignCatalog",
    "DesignPortfolioRow",
    "Disposition",
    "DUPLICATE_CONFIG_ENTRY",
    "DuplicateItemIdError",
    "DUPLICATE_INGREDIENT",
    "DemandPlanner",
    "EQUIPMENT_SHARED_STACK",
    "Evidence",
    "InventoryCatalog",
    "InventoryClassifier",
    "Item",
    "ItemDesign",
    "MALFORMED_QUANTITY",
    "MODULE_MULTI_ATTACH",
    "PlanningContext",
    "ProtectionContext",
    "ProtectedItemPolicy",
    "RECIPE_CYCLE",
    "ReductionPlan",
    "ReductionPlanner",
    "ReductionResult",
    "Reservation",
    "ReservationEntry",
    "ReservationLedger",
    "ReservationSource",
    "ROLES_FORBIDDEN_THIS_SLICE",
    "Role",
    "SNAPSHOT_VERSION",
    "SOURCE_UNAVAILABLE",
    "SOURCE_UNSUPPORTED",
    "STALE_AFTER_SECONDS",
    "STATE_CANDIDATE",
    "STATE_HOLD",
    "UNKNOWN_DESIGN",
    "UNKNOWN_ITEM_REFERENCE",
    "UNREASONABLE_QUANTITY",
    "UnresolvedDemand",
    "render_inventory_plan",
    "SnapshotFreshness",
    "PortfolioViewError",
    "render_accounts_view",
    "render_deficits_view",
    "snapshot_freshness",
    "AccountContribution",
    "AccountPortfolio",
    "AccountSnapshot",
    "DesignPortfolioRow",
    "ELIGIBLE",
    "FORBIDDEN_SNAPSHOT_KEYS",
    "GIFTITEM_EVIDENCE_TIER",
    "HOLD_UNVERIFIED_TRANSFERABILITY",
    "PortfolioError",
    "PortfolioViewError",
    "REASON_DISTINCT_ACCOUNTS",
    "REASON_FRESH_SNAPSHOTS",
    "REASON_NO_APPLICABLE_UNRESOLVED_CONDITION",
    "REASON_NO_EVIDENCE_PROMOTION",
    "REASON_NO_PROTECTION_CONFLICT",
    "REASON_POSITIVE_DESTINATION_DEMAND",
    "REASON_POSITIVE_SOURCE_SUPPLY",
    "REASON_TRANSFER_ELIGIBILITY_UNPROVEN",
    "SnapshotAccountIdentity",
    "SnapshotCondition",
    "SnapshotDesignRow",
    "SnapshotError",
    "SnapshotStackRow",
    "TransferPlan",
    "TransferPlannerError",
    "TransferProposal",
    "aggregate_snapshots",
    "build_snapshot",
    "account_tier_changes",
    "apply_tier_change",
    "checksum_activate_item4",
    "checksum_consume_item2",
    "checksum_mark_items_not_new",
    "checksum_purchase4",
    "checksum_sell_item3",
    "checksum_tier_ceiling",
    "classify_catalog",
    "compute_inventory_delta",
    "decide_transferability",
    "GiftItemExchange",
    "InventoryDelta",
    "InventoryObservation",
    "plan_transfers",
    "reconcile_gift_exchange",
    "render_transfers_view",
    "ReconciliationError",
    "ReconciliationVerdict",
    "SanitizationError",
    "sanitize_body",
    "sanitize_capture_line",
    "sanitize_capture_record",
    "sanitize_headers",
    "sanitize_query_string",
    "scan_for_secrets",
    "semantics_tier_ceiling",
    "TierChange",
    "TransferabilityDecision",
    "snapshot_from_dict",
    "snapshot_from_json",
    "parse_ingredients",
    "parse_item",
    "parse_item_design",
    "policy_limitations",
]
