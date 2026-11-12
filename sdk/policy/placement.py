"""Room placement: the ship's interior grid and whether a room fits at a position (SPEC-13, S13-R3).

Read from game data captured live (capture runs 43314838395 and 43316397906, 2026-11-12):

- ``ShipDesign`` gives the hull's grid as ``@Rows`` x ``@Columns`` and ``@Mask``,
  one digit per cell, row by row from the top-left. ``1`` is a buildable cell,
  ``0`` is outside the hull. ``2`` appears on some hulls with a meaning not yet
  confirmed, so a placement over it is refused.
- A placed room's ``@Row``/``@Column`` (0-based) is the top-left cell of its
  footprint, which its ``RoomDesign`` gives as ``@Rows`` x ``@Columns``.
- ``RoomDesign`` also gives ``@PriceString`` (``kind:amount``), ``@MinShipLevel``
  and ``@RootRoomDesignId``; upgrades keep the root of their level-1 design.
- ``RoomDesignPurchase`` lists, per root room design, a ship ``@Level`` and a
  ``@Quantity``. Whether quantities add up across levels is not confirmed, so the
  stricter reading is used: the room may be placed while the ship has fewer
  rooms of that root than the largest quantity listed at or below its level.
  Entries with different ``@AvailabilityMask`` values are not understood, so
  that room is refused.

Every check refuses on an unobserved or unparseable input: an unknown is never
assumed free. ``placement_refusal`` and ``first_free`` are pure; they never call
the game.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from sdk.state import AccountSnapshot, Completeness

BUILDABLE_CELL = "1"
ALLOWED_PRICE_KINDS = ("mineral", "gas")
_DIGITS = re.compile(r"[0-9]+")
_WHOLE = re.compile(r"-?[0-9]+")


def _int(value: Any) -> Optional[int]:
    text = "" if value is None else str(value).strip()
    return int(text) if _WHOLE.fullmatch(text) else None


def _text(value: Any) -> Optional[str]:
    text = "" if value is None else str(value).strip()
    return text or None


def _records(records: Optional[Iterable[Any]]) -> list[Mapping[str, Any]]:
    return [record for record in records or () if isinstance(record, Mapping)]


@dataclass(frozen=True)
class Hull:
    rows: Optional[int]
    columns: Optional[int]
    mask: Optional[str]  # one digit per cell, row-major; spaces from a capture's split mask removed

    def cell(self, row: int, column: int) -> Optional[str]:
        if self.rows is None or self.columns is None or self.mask is None:
            return None
        if not (0 <= row < self.rows and 0 <= column < self.columns):
            return None
        return self.mask[row * self.columns + column]


@dataclass(frozen=True)
class RoomSpec:
    rows: Optional[int]
    columns: Optional[int]
    price_kind: Optional[str]
    price_amount: Optional[int]
    min_ship_level: Optional[int]
    root_id: Optional[str]
    name: Optional[str] = None


@dataclass(frozen=True)
class Purchase:
    level: Optional[int]
    quantity: Optional[int]
    availability: Optional[str]


@dataclass(frozen=True)
class ShipLayout:
    """Hulls, room footprints and purchase limits, parsed from captured design records."""

    hulls: Mapping[str, Hull] = field(default_factory=dict)
    rooms: Mapping[str, RoomSpec] = field(default_factory=dict)
    purchases: Mapping[str, tuple[Purchase, ...]] = field(default_factory=dict)

    @classmethod
    def from_records(cls, ship_designs: Optional[Iterable[Any]] = (), room_designs: Optional[Iterable[Any]] = (),
                     room_design_purchases: Optional[Iterable[Any]] = ()) -> "ShipLayout":
        hulls = {}
        for record in _records(ship_designs):
            design_id = _text(record.get("@ShipDesignId"))
            if design_id is None:
                continue
            rows, columns = _int(record.get("@Rows")), _int(record.get("@Columns"))
            mask = "".join(str(record.get("@Mask") or "").split())
            valid = (rows is not None and columns is not None and rows > 0 and columns > 0
                     and _DIGITS.fullmatch(mask) is not None and len(mask) == rows * columns)
            hulls[design_id] = Hull(rows, columns, mask if valid else None)
        rooms = {}
        for record in _records(room_designs):
            design_id = _text(record.get("@RoomDesignId"))
            if design_id is None:
                continue
            kind, separator, amount = str(record.get("@PriceString") or "").partition(":")
            rooms[design_id] = RoomSpec(
                rows=_int(record.get("@Rows")),
                columns=_int(record.get("@Columns")),
                price_kind=(kind.strip().lower() or None) if separator else None,
                price_amount=_int(amount) if separator else None,
                min_ship_level=_int(record.get("@MinShipLevel")),
                root_id=_text(record.get("@RootRoomDesignId")),
                name=_text(record.get("@RoomName")),
            )
        purchases: dict[str, list[Purchase]] = {}
        for record in _records(room_design_purchases):
            design_id = _text(record.get("@RoomDesignId"))
            if design_id is not None:
                purchases.setdefault(design_id, []).append(
                    Purchase(_int(record.get("@Level")), _int(record.get("@Quantity")),
                             _text(record.get("@AvailabilityMask"))))
        return cls(hulls, rooms, {key: tuple(value) for key, value in purchases.items()})

    def root_of(self, design_id: Optional[str]) -> Optional[str]:
        spec = self.rooms.get(design_id or "")
        if spec is None:
            return None
        return spec.root_id if spec.root_id not in (None, "0") else design_id


def _purchase_limit(layout: ShipLayout, root_id: str, ship_level: int) -> tuple[Optional[int], Optional[str]]:
    """How many rooms of ``root_id`` the ship may hold at ``ship_level``, or ``(None, reason)``."""
    entries = layout.purchases.get(root_id, ())
    if not entries:
        return None, f"no purchase record for room design {root_id}"
    if any(entry.level is None or entry.quantity is None for entry in entries):
        return None, f"a purchase record for room design {root_id} is unreadable"
    if len({entry.availability for entry in entries}) > 1:
        return None, f"purchase records for room design {root_id} have availability masks that are not understood"
    allowed = [entry.quantity for entry in entries if entry.level <= ship_level]
    if not allowed:
        return None, f"room design {root_id} is not purchasable at ship level {ship_level}"
    return max(allowed), None


def _occupied(snapshot: AccountSnapshot, layout: ShipLayout) -> tuple[Optional[set], Optional[str]]:
    """Every cell a placed room covers, or ``(None, reason)`` when any room's position or size is unknown."""
    cells = set()
    for room in snapshot.rooms:
        spec = layout.rooms.get(room.design_id or "")
        if room.row is None or room.column is None:
            return None, "a placed room's position is not observed"
        if spec is None or spec.rows is None or spec.columns is None:
            return None, f"the size of placed room design {room.design_id} is not known"
        cells.update((room.row + dy, room.column + dx) for dy in range(spec.rows) for dx in range(spec.columns))
    return cells, None


def placement_refusal(snapshot: AccountSnapshot, layout: ShipLayout, room_design_id: str,
                      row: int, column: int) -> Optional[str]:
    """``None`` when ``room_design_id`` may be placed with its top-left cell at ``row``, ``column``; else why not."""
    if snapshot.section("rooms") is Completeness.MISSING:
        return "the ship's rooms are not observed"
    if snapshot.ship_design_id is None or snapshot.hull_level is None:
        return "the ship's hull design or level is not observed"
    hull = layout.hulls.get(snapshot.ship_design_id)
    if hull is None or hull.mask is None:
        return f"the interior grid of hull design {snapshot.ship_design_id} is not known"
    spec = layout.rooms.get(str(room_design_id))
    if spec is None or spec.rows is None or spec.columns is None or spec.rows <= 0 or spec.columns <= 0:
        return f"the size of room design {room_design_id} is not known"
    if spec.price_kind not in ALLOWED_PRICE_KINDS or spec.price_amount is None or spec.price_amount < 0:
        return f"room design {room_design_id} is not priced in minerals or gas"
    if spec.min_ship_level is None:
        return f"the minimum ship level of room design {room_design_id} is not known"
    if snapshot.hull_level < spec.min_ship_level:
        return f"room design {room_design_id} needs ship level {spec.min_ship_level}"
    root = layout.root_of(str(room_design_id))
    if root is None:
        return f"the root design of room design {room_design_id} is not known"
    limit, reason = _purchase_limit(layout, root, snapshot.hull_level)
    if reason is not None:
        return reason
    roots = [layout.root_of(room.design_id) for room in snapshot.rooms]
    if any(value is None for value in roots):
        return "a placed room's design is not known, so the room count cannot be checked"
    if roots.count(root) >= limit:
        return f"the ship already has {roots.count(root)} of room design {root}, the most allowed at its level"
    occupied, reason = _occupied(snapshot, layout)
    if reason is not None:
        return reason
    for dy in range(spec.rows):
        for dx in range(spec.columns):
            cell = (row + dy, column + dx)
            value = hull.cell(*cell)
            if value is None:
                return f"cell {cell} is outside the ship's grid"
            if value != BUILDABLE_CELL:
                return f"cell {cell} is not a buildable interior cell"
            if cell in occupied:
                return f"cell {cell} is occupied by a placed room"
    return None


def first_free(snapshot: AccountSnapshot, layout: ShipLayout, room_design_id: str) -> Optional[tuple[int, int]]:
    """The first position, scanning rows then columns from the top-left, where the room may be placed; else ``None``."""
    hull = layout.hulls.get(snapshot.ship_design_id or "")
    if hull is None or hull.rows is None or hull.columns is None:
        return None
    for row in range(hull.rows):
        for column in range(hull.columns):
            if placement_refusal(snapshot, layout, room_design_id, row, column) is None:
                return row, column
    return None
