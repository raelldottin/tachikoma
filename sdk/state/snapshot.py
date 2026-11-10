"""Typed, account-scoped game state snapshots (roadmap R1 / AUTO-002).

A snapshot is built from response dictionaries that ``sdk.client.Client``
already parsed with ``xmltodict``. The builder is pure: it performs no
network traffic, copies only whitelisted gameplay fields, and never invents a
value. A section that was not supplied is ``MISSING``; a section with records
that could not be parsed is ``PARTIAL``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping, Optional

from sdk.redaction import redact_secrets

ACCOUNT_ALIAS_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SECTIONS = ("profile", "ship", "resources", "crew", "rooms", "research", "inventory")


class SnapshotInputError(ValueError):
    """Raised when snapshot input is malformed or carries secret-shaped data."""


class AccountMismatchError(ValueError):
    """Raised when two snapshots from different accounts are compared."""


class Completeness(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    MISSING = "missing"


@dataclass(frozen=True)
class Resources:
    minerals: Optional[int] = None
    gas: Optional[int] = None
    starbux: Optional[int] = None


@dataclass(frozen=True)
class CrewMember:
    character_id: str
    design_id: Optional[str]
    name: Optional[str]
    level: Optional[int]
    fatigue: Optional[int]
    room_id: Optional[str]
    # Training is observed only when the record carries @TrainingEndDate
    # (empty when no training runs). Unobserved training stays None/False.
    training_observed: bool = False
    training_design_id: Optional[str] = None
    training_end: Optional[str] = None

    @property
    def training_active(self) -> Optional[bool]:
        if not self.training_observed:
            return None
        return bool(self.training_end)


@dataclass(frozen=True)
class Room:
    room_id: str
    design_id: Optional[str]
    status: Optional[str]

    @property
    def upgrading(self) -> bool:
        return self.status == "Upgrading"


@dataclass(frozen=True)
class Research:
    design_id: str
    state: Optional[str]


@dataclass(frozen=True)
class InventoryItem:
    item_design_id: str
    quantity: Optional[int]


@dataclass(frozen=True)
class AccountSnapshot:
    account_alias: str
    observed_at: datetime
    source: str
    game_version: Optional[str]
    hull_level: Optional[int]
    trophies: Optional[int]
    resources: Resources
    crew: tuple[CrewMember, ...]
    rooms: tuple[Room, ...]
    research: tuple[Research, ...]
    inventory: tuple[InventoryItem, ...]
    completeness: Mapping[str, Completeness] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def section(self, name: str) -> Completeness:
        return self.completeness.get(name, Completeness.MISSING)

    def room(self, room_id: str) -> Optional[Room]:
        return next((room for room in self.rooms if room.room_id == room_id), None)

    def crew_member(self, character_id: str) -> Optional[CrewMember]:
        return next((member for member in self.crew if member.character_id == character_id), None)

    def research_state(self, design_id: str) -> Optional[str]:
        entry = next((item for item in self.research if item.design_id == design_id), None)
        return entry.state if entry else None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view, suitable for journaling."""
        return {
            "account_alias": self.account_alias,
            "observed_at": self.observed_at.isoformat(),
            "source": self.source,
            "game_version": self.game_version,
            "hull_level": self.hull_level,
            "trophies": self.trophies,
            "resources": {
                "minerals": self.resources.minerals,
                "gas": self.resources.gas,
                "starbux": self.resources.starbux,
            },
            "crew": [member.__dict__.copy() for member in self.crew],
            "rooms": [{**room.__dict__, "upgrading": room.upgrading} for room in self.rooms],
            "research": [entry.__dict__.copy() for entry in self.research],
            "inventory": [item.__dict__.copy() for item in self.inventory],
            "completeness": {name: self.section(name).value for name in SECTIONS},
            "notes": list(self.notes),
        }


def _clean_text(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    if redact_secrets(text) != text:
        raise SnapshotInputError(f"secret-shaped value refused in {label}")
    return text


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _training_design_id(value: Any) -> Optional[str]:
    text = _clean_text(value, "crew.training_design_id")
    return None if text in (None, "", "0") else text


def _records(value: Any) -> Optional[list[Mapping[str, Any]]]:
    """xmltodict yields a dict for one element and a list for several."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, list) and all(isinstance(item, Mapping) for item in value):
        return list(value)
    raise SnapshotInputError("section records must be a mapping or a list of mappings")


class _Builder:
    def __init__(self) -> None:
        self.completeness: dict[str, Completeness] = {}
        self.notes: list[str] = []

    def parse_section(self, name: str, raw: Any, id_key: str, make) -> tuple:
        records = _records(raw)
        if records is None:
            self.completeness[name] = Completeness.MISSING
            return ()
        parsed = []
        skipped = 0
        for record in records:
            identifier = record.get(id_key)
            if identifier in (None, ""):
                skipped += 1
                continue
            parsed.append(make(_clean_text(identifier, f"{name}.{id_key}"), record))
        if skipped:
            self.notes.append(f"{name}: skipped {skipped} record(s) without {id_key}")
        self.completeness[name] = Completeness.PARTIAL if skipped else Completeness.COMPLETE
        return tuple(parsed)


def build_snapshot(
    account_alias: str,
    observed_at: datetime,
    source: str,
    *,
    game_version: Optional[str] = None,
    profile: Optional[Mapping[str, Any]] = None,
    ship: Optional[Mapping[str, Any]] = None,
    resources: Optional[Mapping[str, Any]] = None,
    crew: Any = None,
    rooms: Any = None,
    research: Any = None,
    inventory: Any = None,
) -> AccountSnapshot:
    """Build a snapshot from already-parsed client responses.

    ``profile`` is the parsed ``User`` element, ``ship`` the ``Ship`` element
    of ``GetShipByUserId``, and the list sections are its ``Room``,
    ``Research``, character and item records. ``resources`` takes
    ``minerals``, ``gas`` and ``starbux`` keys. Fields outside the whitelist,
    including account e-mail, tokens and device keys, are never copied.
    """
    if not ACCOUNT_ALIAS_PATTERN.match(account_alias or ""):
        raise SnapshotInputError("account_alias must be a lowercase alias, not an e-mail or display name")
    if observed_at.tzinfo is None:
        raise SnapshotInputError("observed_at must be timezone-aware")
    source = _clean_text(source, "source") or ""
    if not source:
        raise SnapshotInputError("source is required")

    builder = _Builder()

    trophies = None
    if profile is None:
        builder.completeness["profile"] = Completeness.MISSING
    else:
        trophies = _int_or_none(profile.get("@Trophy"))
        builder.completeness["profile"] = Completeness.COMPLETE if trophies is not None else Completeness.PARTIAL

    hull_level = None
    if ship is None:
        builder.completeness["ship"] = Completeness.MISSING
    else:
        hull_level = _int_or_none(ship.get("@ShipLevel"))
        builder.completeness["ship"] = Completeness.COMPLETE if hull_level is not None else Completeness.PARTIAL

    if resources is None:
        resource_values = Resources()
        builder.completeness["resources"] = Completeness.MISSING
    else:
        resource_values = Resources(
            minerals=_int_or_none(resources.get("minerals")),
            gas=_int_or_none(resources.get("gas")),
            starbux=_int_or_none(resources.get("starbux")),
        )
        known = [resource_values.minerals, resource_values.gas, resource_values.starbux]
        builder.completeness["resources"] = (
            Completeness.COMPLETE if all(value is not None for value in known) else Completeness.PARTIAL
        )

    crew_records = builder.parse_section(
        "crew",
        crew,
        "@CharacterId",
        lambda identifier, record: CrewMember(
            character_id=identifier,
            design_id=_clean_text(record.get("@CharacterDesignId"), "crew.design_id"),
            name=_clean_text(record.get("@CharacterName"), "crew.name"),
            level=_int_or_none(record.get("@Level")),
            fatigue=_int_or_none(record.get("@Fatigue")),
            room_id=_clean_text(record.get("@RoomId"), "crew.room_id"),
            training_observed="@TrainingEndDate" in record,
            training_design_id=_training_design_id(record.get("@TrainingDesignId")),
            training_end=_clean_text(record.get("@TrainingEndDate"), "crew.training_end") or None,
        ),
    )
    room_records = builder.parse_section(
        "rooms",
        rooms,
        "@RoomId",
        lambda identifier, record: Room(
            room_id=identifier,
            design_id=_clean_text(record.get("@RoomDesignId"), "rooms.design_id"),
            status=_clean_text(record.get("@RoomStatus"), "rooms.status"),
        ),
    )
    research_records = builder.parse_section(
        "research",
        research,
        "@ResearchDesignId",
        lambda identifier, record: Research(
            design_id=identifier, state=_clean_text(record.get("@ResearchState"), "research.state")
        ),
    )
    inventory_records = builder.parse_section(
        "inventory",
        inventory,
        "@ItemDesignId",
        lambda identifier, record: InventoryItem(
            item_design_id=identifier, quantity=_int_or_none(record.get("@Quantity"))
        ),
    )

    return AccountSnapshot(
        account_alias=account_alias,
        observed_at=observed_at,
        source=source,
        game_version=_clean_text(game_version, "game_version"),
        hull_level=hull_level,
        trophies=trophies,
        resources=resource_values,
        crew=crew_records,
        rooms=room_records,
        research=research_records,
        inventory=inventory_records,
        completeness=dict(builder.completeness),
        notes=tuple(builder.notes),
    )


def _delta(before: Optional[int], after: Optional[int]) -> Optional[int]:
    if before is None or after is None:
        return None
    return after - before


@dataclass(frozen=True)
class SnapshotDelta:
    """Observed change between two snapshots of one account.

    A ``None`` field means the change is unknown because one side did not
    observe it; it never means "no change". Keyed sections listed in
    ``unknown_sections`` were missing from one side, so their mappings are
    empty by construction and say nothing about change.
    """

    account_alias: str
    elapsed_seconds: float
    minerals: Optional[int]
    gas: Optional[int]
    starbux: Optional[int]
    trophies: Optional[int]
    hull_level: Optional[int]
    crew_level: Mapping[str, Optional[int]]
    room_design: Mapping[str, tuple[Optional[str], Optional[str]]]
    room_status: Mapping[str, tuple[Optional[str], Optional[str]]]
    research_state: Mapping[str, tuple[Optional[str], Optional[str]]]
    inventory_quantity: Mapping[str, Optional[int]]
    unknown_sections: tuple[str, ...] = ()
    # (training_design_id, training_end) before -> after, only for crew whose
    # training was observed on both sides.
    crew_training: Mapping[str, tuple[tuple, tuple]] = field(default_factory=dict)


def _keyed_changes(before: Iterable, after: Iterable, key, value) -> dict[str, tuple]:
    before_map = {key(item): value(item) for item in before}
    after_map = {key(item): value(item) for item in after}
    changes = {}
    for identifier in sorted(set(before_map) | set(after_map)):
        old, new = before_map.get(identifier), after_map.get(identifier)
        if old != new:
            changes[identifier] = (old, new)
    return changes


def diff_snapshots(before: AccountSnapshot, after: AccountSnapshot) -> SnapshotDelta:
    """Return the observed change from ``before`` to ``after`` for one account."""
    if before.account_alias != after.account_alias:
        raise AccountMismatchError(
            f"refusing to compare snapshots of {before.account_alias!r} and {after.account_alias!r}"
        )
    elapsed = (after.observed_at - before.observed_at).total_seconds()
    if elapsed < 0:
        raise SnapshotInputError("after snapshot was observed before the before snapshot")

    unknown = tuple(
        name
        for name in ("crew", "rooms", "research", "inventory")
        if Completeness.MISSING in (before.section(name), after.section(name))
    )

    def observed(name: str, side: AccountSnapshot) -> tuple:
        return () if name in unknown else getattr(side, name)

    crew_level = {
        identifier: _delta(old, new)
        for identifier, (old, new) in _keyed_changes(
            observed("crew", before),
            observed("crew", after),
            lambda member: member.character_id,
            lambda member: member.level,
        ).items()
    }
    # An item absent from an observed inventory has quantity 0; an item present
    # with an unparseable quantity is unknown (None), not 0.
    items_before = {item.item_design_id: item.quantity for item in observed("inventory", before)}
    items_after = {item.item_design_id: item.quantity for item in observed("inventory", after)}
    inventory_quantity = {}
    for identifier in sorted(set(items_before) | set(items_after)):
        old = items_before.get(identifier, 0)
        new = items_after.get(identifier, 0)
        if old != new:
            inventory_quantity[identifier] = _delta(old, new)
    rooms_before, rooms_after = observed("rooms", before), observed("rooms", after)
    training_before = {m.character_id: m for m in observed("crew", before) if m.training_observed}
    crew_training = {}
    for member in observed("crew", after):
        old = training_before.get(member.character_id)
        if old is None or not member.training_observed:
            continue
        old_value = (old.training_design_id, old.training_end)
        new_value = (member.training_design_id, member.training_end)
        if old_value != new_value:
            crew_training[member.character_id] = (old_value, new_value)
    return SnapshotDelta(
        account_alias=after.account_alias,
        elapsed_seconds=elapsed,
        minerals=_delta(before.resources.minerals, after.resources.minerals),
        gas=_delta(before.resources.gas, after.resources.gas),
        starbux=_delta(before.resources.starbux, after.resources.starbux),
        trophies=_delta(before.trophies, after.trophies),
        hull_level=_delta(before.hull_level, after.hull_level),
        crew_level=crew_level,
        room_design=_keyed_changes(rooms_before, rooms_after, lambda room: room.room_id, lambda room: room.design_id),
        room_status=_keyed_changes(rooms_before, rooms_after, lambda room: room.room_id, lambda room: room.status),
        research_state=_keyed_changes(
            observed("research", before),
            observed("research", after),
            lambda entry: entry.design_id,
            lambda entry: entry.state,
        ),
        inventory_quantity=inventory_quantity,
        unknown_sections=unknown,
        crew_training=crew_training,
    )
