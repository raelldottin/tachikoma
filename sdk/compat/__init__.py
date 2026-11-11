"""Patch-aware compatibility ledger of game rules, endpoints and formulas (roadmap AUTO-015)."""

from sdk.compat.entries import DECLARED_CLIENT_VERSION, LEDGER
from sdk.compat.ledger import (
    EVIDENCE_CLASSES,
    KINDS,
    STRONG_EVIDENCE,
    CompatibilityReport,
    Evidence,
    LedgerEntry,
    check_compatibility,
    compare_versions,
    entries_for_client_method,
    parse_version,
    validate_ledger,
)

__all__ = [
    "DECLARED_CLIENT_VERSION",
    "EVIDENCE_CLASSES",
    "KINDS",
    "LEDGER",
    "STRONG_EVIDENCE",
    "CompatibilityReport",
    "Evidence",
    "LedgerEntry",
    "check_compatibility",
    "compare_versions",
    "entries_for_client_method",
    "parse_version",
    "validate_ledger",
]
