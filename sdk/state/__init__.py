"""Account-scoped game state (roadmap R1 / AUTO-002)."""

from sdk.state.snapshot import (
    AccountMismatchError,
    AccountSnapshot,
    Completeness,
    CrewMember,
    InventoryItem,
    Research,
    Resources,
    Room,
    SnapshotDelta,
    SnapshotInputError,
    build_snapshot,
    diff_snapshots,
)

__all__ = [
    "AccountMismatchError",
    "AccountSnapshot",
    "Completeness",
    "CrewMember",
    "InventoryItem",
    "Research",
    "Resources",
    "Room",
    "SnapshotDelta",
    "SnapshotInputError",
    "build_snapshot",
    "diff_snapshots",
]
