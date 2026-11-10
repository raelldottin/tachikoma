"""Gameplay action catalog and reconciliation (roadmap R1 / AUTO-003)."""

from sdk.actions.catalog import (
    CATALOG,
    ActionSpec,
    ActionSpecError,
    Idempotency,
    Reconciliation,
    ReconciliationStatus,
    check_preconditions,
    get_spec,
    reconcile,
)

__all__ = [
    "CATALOG",
    "ActionSpec",
    "ActionSpecError",
    "Idempotency",
    "Reconciliation",
    "ReconciliationStatus",
    "check_preconditions",
    "get_spec",
    "reconcile",
]
