"""Boost-gauge speedup planner: pure, no HTTP, no clock, no client.

``plan_boost_gauge(ship)`` reads one ``GetShipByUserId`` ``Ship`` record (as xmltodict parses it) and returns
the items the boost gauge can speed up, in a fixed order:

1. research: each ``Research`` whose ``@ResearchState`` is ``"Researching"``, by its ``@ResearchId`` (the
   research instance id ``SpeedUpResearchUsingBoostGauge`` takes, never ``@ResearchDesignId``);
2. room: each ``Room`` whose ``@RoomStatus`` is ``"Upgrading"``, by its ``@RoomId``;
3. ship: the ship itself, by its ``@ShipId``, when ``@UpgradeShipDesignId`` names a hull ("" and "0" mean no
   hull upgrade is under way).

Research and rooms keep their document order. A record without its id gives no candidate. ``Researches`` and
``Rooms`` may hold one record (a dict), several (a list), or nothing (missing, empty or None).
``Client.speedUpBoostGauge`` tries the candidates in this order and stops after the first success.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class BoostDecision:
    """One boost-gauge candidate: ``kind`` is "research", "room" or "ship"; ``id`` is the id the endpoint takes."""

    kind: str
    id: str


def _records(container: Any, key: str) -> list[Mapping]:
    """The ``key`` records under ``container``: one dict, a list of dicts, or none."""
    value = container.get(key) if isinstance(container, Mapping) else None
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, list):
        return [record for record in value if isinstance(record, Mapping)]
    return []


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def plan_boost_gauge(ship: Any) -> tuple[BoostDecision, ...]:
    """Boost-gauge candidates for ``ship``: research, then rooms, then the ship upgrade. Empty for a non-mapping."""
    if not isinstance(ship, Mapping):
        return ()
    decisions = []
    for research in _records(ship.get("Researches"), "Research"):
        research_id = _text(research.get("@ResearchId"))
        if research.get("@ResearchState") == "Researching" and research_id:
            decisions.append(BoostDecision("research", research_id))
    for room in _records(ship.get("Rooms"), "Room"):
        room_id = _text(room.get("@RoomId"))
        if room.get("@RoomStatus") == "Upgrading" and room_id:
            decisions.append(BoostDecision("room", room_id))
    ship_id = _text(ship.get("@ShipId"))
    if _text(ship.get("@UpgradeShipDesignId")) not in ("", "0") and ship_id:
        decisions.append(BoostDecision("ship", ship_id))
    return tuple(decisions)
