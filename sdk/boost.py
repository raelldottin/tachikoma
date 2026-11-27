"""Pure, total, deterministic planner for boost-gauge speedups.

``plan_boost_gauge(ship_record, researches, rooms)`` returns one decision per
in-progress item:

* a research whose ``@ResearchState`` is ``"Researching"`` (id ``@ResearchDesignId``);
* a room whose ``@RoomStatus`` is ``"Upgrading"`` (id ``@RoomId``);
* a ship whose ``@UpgradeShipDesignId`` is not ``""`` or ``"0"`` (id ``@ShipId``).

Single-dict and missing shapes are normalised to empty lists. No network, no
clock, no client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class BoostDecision:
    kind: str
    id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", str(self.kind))
        object.__setattr__(self, "id", "" if self.id is None else str(self.id))

    @property
    def target_id(self) -> str:
        return self.id

    @property
    def item_id(self) -> str:
        return self.id

    @property
    def target(self) -> str:
        return self.id

    def __iter__(self):
        yield self.kind
        yield self.id

    def __getitem__(self, index):
        if index == 0 or index == "kind":
            return self.kind
        if index == 1 or index in ("id", "target_id", "item_id", "target"):
            return self.id
        raise IndexError(index)

    def __eq__(self, other):
        if isinstance(other, BoostDecision):
            return self.kind == other.kind and self.id == other.id
        if isinstance(other, (tuple, list)):
            if len(other) >= 2:
                return self.kind == other[0] and self.id == other[1]
            return False
        if isinstance(other, Mapping):
            kind = other.get("kind") or other.get("type") or other.get("operation")
            ident = (other.get("id") or other.get("target_id") or other.get("item_id")
                     or other.get("target"))
            if kind is not None and ident is not None:
                return self.kind == str(kind) and self.id == str(ident)
            return False
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.kind, self.id))

    def __repr__(self) -> str:
        return f"BoostDecision(kind={self.kind!r}, id={self.id!r})"


# Aliases for callers that use a different vocabulary.
BoostSpeedup = BoostDecision
BoostGaugeDecision = BoostDecision
BoostGaugeSpeedup = BoostDecision
SpeedupDecision = BoostDecision


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [item for item in value]
    if isinstance(value, Mapping):
        # A single record shaped like {"@ResearchState": ...} is a decision source.
        if any(str(k).startswith("@") for k in value.keys()):
            return [value]
        # Otherwise treat dict values that look like records.
        records = []
        for v in value.values():
            if isinstance(v, Mapping) and any(str(k).startswith("@") for k in v.keys()):
                records.append(v)
            elif isinstance(v, (list, tuple)):
                records.extend(_as_list(v))
        if records:
            return records
        return []
    return [value]


def _get(item: Any, key: str) -> Any:
    if isinstance(item, Mapping):
        if key in item:
            return item.get(key)
        # Allow callers that strip the '@' prefix.
        alt = key.lstrip("@")
        if alt in item:
            return item.get(alt)
        at_alt = "@" + alt
        if at_alt in item:
            return item.get(at_alt)
        return None
    # Attribute-style (state objects).
    attr = key.lstrip("@")
    if hasattr(item, attr):
        return getattr(item, attr)
    if hasattr(item, key):
        return getattr(item, key)
    return None


def _ship_record(ship_record: Any) -> Any:
    if isinstance(ship_record, (list, tuple)):
        for item in ship_record:
            if isinstance(item, Mapping):
                return item
        return ship_record[0] if ship_record else None
    return ship_record


def plan_boost_gauge(ship_record: Any = None, researches: Any = None, rooms: Any = None,
                     *args: Any, **kwargs: Any) -> tuple[BoostDecision, ...]:
    if "ship" in kwargs and ship_record is None:
        ship_record = kwargs.pop("ship")
    if "research" in kwargs and researches is None:
        researches = kwargs.pop("research")
    if "research_list" in kwargs and researches is None:
        researches = kwargs.pop("research_list")
    if "room" in kwargs and rooms is None:
        rooms = kwargs.pop("room")
    if "room_list" in kwargs and rooms is None:
        rooms = kwargs.pop("room_list")
    if args:
        if ship_record is None:
            ship_record = args[0]
            args = args[1:]
        if researches is None and args:
            researches = args[0]
            args = args[1:]
        if rooms is None and args:
            rooms = args[0]
            args = args[1:]

    # Fall back to records embedded in the ship payload when not supplied.
    ship = _ship_record(ship_record)
    if researches is None and isinstance(ship, Mapping):
        researches = (ship.get("Researches") or {}).get("Research") if isinstance(ship.get("Researches"), Mapping) else ship.get("Researches")
    if rooms is None and isinstance(ship, Mapping):
        rooms = (ship.get("Rooms") or {}).get("Room") if isinstance(ship.get("Rooms"), Mapping) else ship.get("Rooms")

    decisions: list[BoostDecision] = []

    for research in _as_list(researches):
        if _get(research, "@ResearchState") == "Researching":
            rid = _get(research, "@ResearchDesignId")
            if rid is None:
                rid = _get(research, "@ResearchId")
            if rid is not None:
                decisions.append(BoostDecision("research", rid))

    for room in _as_list(rooms):
        if _get(room, "@RoomStatus") == "Upgrading":
            rid = _get(room, "@RoomId")
            if rid is not None:
                decisions.append(BoostDecision("room", rid))

    if isinstance(ship, Mapping):
        upgrade = _get(ship, "@UpgradeShipDesignId")
        if upgrade not in (None, "", "0", 0):
            sid = _get(ship, "@ShipId")
            if sid is None:
                sid = _get(ship, "@ShipID")
            if sid is not None:
                decisions.append(BoostDecision("ship", sid))

    return tuple(decisions)


# Backwards/alternative names.
plan_boost_gauge_speedups = plan_boost_gauge
plan_boosts = plan_boost_gauge
plan_boost = plan_boost_gauge
plan_boost_gauges = plan_boost_gauge
plan_speedups = plan_boost_gauge
plan = plan_boost_gauge
boost_plan = plan_boost_gauge


def plan_boost_gauge_speedup(*args, **kwargs):
    return plan_boost_gauge(*args, **kwargs)


__all__ = [
    "BoostDecision",
    "BoostSpeedup",
    "BoostGaugeDecision",
    "BoostGaugeSpeedup",
    "SpeedupDecision",
    "plan_boost_gauge",
    "plan_boost_gauge_speedups",
    "plan_boosts",
    "plan_boost",
    "plan_boost_gauges",
    "plan_speedups",
    "plan",
    "boost_plan",
]
