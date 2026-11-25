"""Tolerant conversion of xmltodict-shaped Pixel Starships payloads into the training model.

Input shapes follow ``xmltodict.parse(..., xml_attribs=True)``: attributes are ``@``-prefixed strings, one element
is a dict, several are a list, an empty element is None or missing. Records are found envelope-agnostically (see
``extract_collection``). Nothing here raises on missing or malformed data: records without a positive id are
skipped and malformed numbers default to 0. Nothing here imports ``sdk.client`` or performs I/O.

Assumed live attributes (only @TrainingDesignId/@TrainingName are confirmed by the existing client code):
- TrainingDesign: @Rank (tier), @Duration (seconds), @RequiredResearchDesignId ("0" = none), @RequiredRoomType,
  and per-stat chances @HpChance ... @StaminaChance (stat = highest positive chance, ties in canonical order).
- Character: @CharacterId, @CharacterName, @CharacterDesignId, @Level, @RoomId, @Fatigue, @TrainingDesignId
  ("0" = none), @TrainingEndDate ("" = idle), and the nine @*Improvement stats.
- CharacterDesign: @TrainingCapacity, @Rarity. Room: @RoomId, @RoomDesignId. RoomDesign: @RoomName.
- Research: @ResearchDesignId, @ResearchState ("Completed" counts), optional @ResearchLevel.
  ResearchDesign: @ResearchName, @RootResearchDesignId, optional @Level. Item: @ItemDesignId, @Quantity.
  ItemDesign: @ItemName, @ItemSubType, @Rarity.

Research names are normalised to a family: the root design's @ResearchName with a trailing "Lv N"/"Level N"
removed, valued at the highest completed level. A design's required research that cannot be resolved becomes
"research_design:<id>", which no research level matches, so it fails closed. An unknown @RequiredRoomType becomes
None (fails open): the server enforces rooms and the original code never checked it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

from sdk.training.model import CANONICAL_STATS, CrewState, TrainingContext, TrainingDesign
from sdk.training.strategy import RARITY_STARS, ROOM_ACADEMY, ROOM_GYM, STAT_CODES


def to_int(value: object, default: int = 0) -> int:
    """Best-effort integer: ints pass through, numeric strings parse ("7", " 7 ", "7.0"); anything else -> default."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            value = float(text)
        except ValueError:
            return default
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return default


_DATETIME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?")


def parse_datetime(value: object) -> datetime | None:
    """Parse a server timestamp into an aware UTC datetime; empty or malformed values -> None.

    Accepts "%Y-%m-%dT%H:%M:%S" with an optional fraction (truncated to microseconds) and an optional
    "Z" / "±hh:mm" offset. Timestamps without an offset are taken to be UTC (the server's convention).
    """
    if not isinstance(value, str):
        return None
    match = _DATETIME_RE.fullmatch(value.strip())
    if match is None:
        return None
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    micro = int((fraction or "0")[:6].ljust(6, "0"))
    try:
        tz = timezone.utc
        if offset and offset != "Z":
            sign = -1 if offset[0] == "-" else 1
            tz = timezone(sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6])))
        parsed = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), micro, tzinfo=tz)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


_MAX_DEPTH = 64


def extract_collection(data: object, item_key: str) -> list[dict]:
    """Records under ``item_key`` anywhere in an envelope, as a list of dicts (envelope-agnostic).

    A top-level key wins; otherwise nested dicts are searched depth-first and the first non-empty hit is used.
    A single record (dict) becomes a one-item list; non-dict list members are dropped; anything else -> [].
    """
    return _extract(data, item_key, 0)


def _extract(data: object, item_key: str, depth: int) -> list[dict]:
    if not isinstance(data, dict) or depth > _MAX_DEPTH:
        return []
    if item_key in data:
        value = data[item_key]
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        return []
    for value in data.values():
        found = _extract(value, item_key, depth + 1)
        if found:
            return found
    return []


PAYLOAD_OK = "ok"
PAYLOAD_EMPTY = "empty"
PAYLOAD_ERROR = "error"


def classify_payload(raw: object, item_key: str, container_key: str | None = None) -> str:
    """Tell "no data" apart from "broken response" (ORIGINAL_REQUEST R5).

    error: not a dict, an ``errorMessage`` anywhere, or neither the item nor its container is present
    (unexpected schema). empty: the container (default ``item_key + "s"``) is present but holds no records.
    ok: at least one record.
    """
    if not isinstance(raw, dict) or _has_key(raw, ("@errorMessage", "errorMessage"), 0):
        return PAYLOAD_ERROR
    if extract_collection(raw, item_key):
        return PAYLOAD_OK
    if _has_key(raw, (item_key, container_key or item_key + "s"), 0):
        return PAYLOAD_EMPTY
    return PAYLOAD_ERROR


def _has_key(data: object, keys: tuple[str, ...], depth: int) -> bool:
    if not isinstance(data, dict) or depth > _MAX_DEPTH:
        return False
    if any(key in data for key in keys):
        return True
    return any(_has_key(value, keys, depth + 1) for value in data.values())


_MAX_DURATION_SECONDS = 10 * 365 * 86400
_ROOM_TYPE_WORDS = MappingProxyType({"gym": ROOM_GYM, "academy": ROOM_ACADEMY})


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _design_stat(record: dict) -> str | None:
    best: str | None = None
    best_chance = 0
    for stat in CANONICAL_STATS:
        chance = to_int(record.get("@" + stat.replace("Improvement", "Chance")))
        if chance > best_chance:
            best, best_chance = stat, chance
    return best


def _room_type_word(value: object) -> str | None:
    return _ROOM_TYPE_WORDS.get(_text(value).lower())


_LEVEL_SUFFIX_RE = re.compile(r"\s*(?:lv\.?|level)\s*(\d+)\s*$", re.IGNORECASE)


def _split_level(name: str) -> tuple[str, int]:
    """("Fitness Lv2") -> ("Fitness", 2); names without a level suffix -> (name, 0)."""
    match = _LEVEL_SUFFIX_RE.search(name)
    if match is None:
        return name, 0
    return name[: match.start()].strip(), to_int(match.group(1))


def _research_catalog(research_designs_raw: object) -> dict[int, tuple[str, int]]:
    """ResearchDesign id -> (family name, design level).

    The family is the root design's @ResearchName (via @RootResearchDesignId, else the design's own name) with a
    trailing "Lv N"/"Level N" removed. The level is @Level when positive, else the own name's suffix, else 0.
    """
    records = _by_id(research_designs_raw, "ResearchDesign", "@ResearchDesignId")
    catalog: dict[int, tuple[str, int]] = {}
    for design_id, record in records.items():
        own_family, own_level = _split_level(_text(record.get("@ResearchName")))
        root = records.get(to_int(record.get("@RootResearchDesignId")))
        root_family = _split_level(_text(root.get("@ResearchName")))[0] if root is not None else ""
        family = root_family or own_family or f"research_design:{design_id}"
        level = to_int(record.get("@Level"))
        catalog[design_id] = (family, level if level > 0 else own_level)
    return catalog


def _required_research(research_id: int, catalog: dict[int, tuple[str, int]]) -> str | None:
    if research_id <= 0:
        return None
    if research_id in catalog:
        return catalog[research_id][0]
    return f"research_design:{research_id}"


def parse_training_designs(raw: object, research_designs: object = None) -> tuple[TrainingDesign, ...]:
    """TrainingDesign records -> model designs, in payload order; records without a positive id are skipped.

    @RequiredResearchDesignId "0"/missing -> None; otherwise the research family name from ``research_designs``,
    or "research_design:<id>" when it cannot be resolved (no research of that name exists, so it fails closed).
    """
    catalog = _research_catalog(research_designs)
    designs: list[TrainingDesign] = []
    for record in extract_collection(raw, "TrainingDesign"):
        design_id = to_int(record.get("@TrainingDesignId"))
        if design_id <= 0:
            continue
        seconds = min(max(to_int(record.get("@Duration")), 0), _MAX_DURATION_SECONDS)
        research_id = to_int(record.get("@RequiredResearchDesignId"))
        designs.append(
            TrainingDesign(
                id=design_id,
                name=_text(record.get("@TrainingName")),
                tier=to_int(record.get("@Rank")),
                stat=_design_stat(record),
                duration=timedelta(seconds=seconds),
                required_research=_required_research(research_id, catalog),
                required_room_type=_room_type_word(record.get("@RequiredRoomType")),
            )
        )
    return tuple(designs)


def parse_rooms(rooms_raw: object, room_designs_raw: object) -> Mapping[int, str]:
    """Room id -> room design name; rooms whose design (or name) is unknown are omitted."""
    names = {
        design_id: _text(record.get("@RoomName"))
        for design_id, record in _by_id(room_designs_raw, "RoomDesign", "@RoomDesignId").items()
    }
    rooms: dict[int, str] = {}
    for room_id, record in _by_id(rooms_raw, "Room", "@RoomId").items():
        name = names.get(to_int(record.get("@RoomDesignId")), "")
        if name:
            rooms[room_id] = name
    return MappingProxyType(rooms)


def parse_crew(
    characters_raw: object,
    character_designs_raw: object,
    rooms_raw: object = None,
    room_designs_raw: object = None,
) -> tuple[CrewState, ...]:
    """Character records joined with their CharacterDesign (capacity, rarity) and room name.

    A character whose design is missing gets capacity 0 and rarity "" (never another design's values); one without
    a positive @CharacterId is skipped. Stat keys are stored without the XML '@'; missing/malformed stats are 0.
    """
    designs = _by_id(character_designs_raw, "CharacterDesign", "@CharacterDesignId")
    rooms = parse_rooms(rooms_raw, room_designs_raw)
    crew: list[CrewState] = []
    for record in extract_collection(characters_raw, "Character"):
        character_id = to_int(record.get("@CharacterId"))
        if character_id <= 0:
            continue
        design_id = to_int(record.get("@CharacterDesignId"))
        design = designs.get(design_id, {})
        room_id = to_int(record.get("@RoomId"))
        training_design_id = to_int(record.get("@TrainingDesignId"))
        crew.append(
            CrewState(
                character_id=character_id,
                name=_text(record.get("@CharacterName")),
                design_id=design_id,
                rarity=_text(design.get("@Rarity")),
                level=to_int(record.get("@Level")),
                room_id=room_id if room_id > 0 else None,
                room_name=rooms.get(room_id),
                stats={stat: to_int(record.get("@" + stat)) for stat in CANONICAL_STATS},
                training_capacity=to_int(design.get("@TrainingCapacity")),
                fatigue=to_int(record.get("@Fatigue")),
                training_design_id=training_design_id if training_design_id > 0 else None,
                training_end=parse_datetime(record.get("@TrainingEndDate")),
            )
        )
    return tuple(crew)


def parse_research_levels(researches_raw: object, research_designs_raw: object) -> Mapping[str, int]:
    """Research family name -> highest completed level.

    Only @ResearchState == "Completed" records count; records whose design id is unknown are ignored.
    Level precedence: the record's @ResearchLevel, then the design's @Level, then a "Lv N" name suffix, then 1.
    """
    catalog = _research_catalog(research_designs_raw)
    levels: dict[str, int] = {}
    for record in extract_collection(researches_raw, "Research"):
        if _text(record.get("@ResearchState")) != "Completed":
            continue
        entry = catalog.get(to_int(record.get("@ResearchDesignId")))
        if entry is None:
            continue
        family, design_level = entry
        level = to_int(record.get("@ResearchLevel"))
        if level <= 0:
            level = design_level if design_level > 0 else 1
        levels[family] = max(levels.get(family, 0), level)
    return MappingProxyType(levels)


def parse_items(items_raw: object) -> Mapping[int, int]:
    """Item design id -> total quantity on the ship (@Quantity summed; missing -> 1; malformed/negative -> 0)."""
    stock: dict[int, int] = {}
    for record in extract_collection(items_raw, "Item"):
        design_id = to_int(record.get("@ItemDesignId"))
        if design_id <= 0:
            continue
        quantity = to_int(record.get("@Quantity")) if "@Quantity" in record else 1
        stock[design_id] = stock.get(design_id, 0) + max(quantity, 0)
    return MappingProxyType(stock)


_STAT_TOKENS = MappingProxyType(
    {
        **{code: code for code in STAT_CODES.values()},
        **{stat.removesuffix("Improvement").upper(): code for stat, code in STAT_CODES.items()},
    }
)
_TIER_TOKENS = MappingProxyType({tier.upper(): tier for tier in RARITY_STARS})


def parse_consumable_designs(item_designs_raw: object) -> Mapping[tuple[str, str], int]:
    """(stat code, tier) -> consumable ItemDesign id, for ``with_consumables``.

    A record qualifies when @ItemSubType == "Consumable" or "Consumable" appears in @ItemName. The stat code and the
    tier are whole-word tokens of the name (so "Shipment" never reads as HP); a code (WPN) or the stat's word
    (WEAPON, REPAIR, ...) both count. A tier word in the
    name wins over @Rarity. Records without both are ignored; the lowest id wins for a repeated key.
    """
    found: dict[tuple[str, str], int] = {}
    for design_id, record in sorted(_by_id(item_designs_raw, "ItemDesign", "@ItemDesignId").items()):
        name = _text(record.get("@ItemName"))
        if _text(record.get("@ItemSubType")) != "Consumable" and "Consumable" not in name:
            continue
        tokens = re.findall(r"[A-Z]+", name.upper())
        code = next((_STAT_TOKENS[token] for token in tokens if token in _STAT_TOKENS), None)
        tier = next((_TIER_TOKENS[token] for token in tokens if token in _TIER_TOKENS), None)
        tier = tier or _TIER_TOKENS.get(_text(record.get("@Rarity")).upper())
        if code is not None and tier is not None:
            found.setdefault((code, tier), design_id)
    return MappingProxyType(found)


def build_context(
    *,
    now: datetime,
    characters_raw: object,
    character_designs_raw: object,
    training_designs_raw: object,
    rooms_raw: object = None,
    room_designs_raw: object = None,
    researches_raw: object = None,
    research_designs_raw: object = None,
    items_raw: object = None,
) -> TrainingContext:
    """Assemble a TrainingContext from raw payloads; ``now`` must be timezone-aware (the model rejects naive)."""
    return TrainingContext(
        now=now,
        crew=parse_crew(characters_raw, character_designs_raw, rooms_raw, room_designs_raw),
        designs=parse_training_designs(training_designs_raw, research_designs_raw),
        rooms=parse_rooms(rooms_raw, room_designs_raw),
        research_levels=parse_research_levels(researches_raw, research_designs_raw),
        items=parse_items(items_raw),
    )


def _by_id(raw: object, item_key: str, id_key: str) -> dict[int, dict]:
    """Records keyed by positive integer id; the first record wins for a duplicated id."""
    records: dict[int, dict] = {}
    for record in extract_collection(raw, item_key):
        record_id = to_int(record.get(id_key))
        if record_id > 0:
            records.setdefault(record_id, record)
    return records
