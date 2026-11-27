"""Tolerant conversion of xmltodict-shaped Pixel Starships payloads into the training model.

Input shapes follow ``xmltodict.parse(..., xml_attribs=True)``: attributes are ``@``-prefixed strings, one element
is a dict, several are a list, an empty element is None or missing. Records are found envelope-agnostically (see
``extract_collection``). Nothing here raises on missing or malformed data: records without a positive id are
skipped and malformed numbers default to 0. Nothing here imports ``sdk.client`` or performs I/O.

Assumed live attributes (@TrainingDesignId/@TrainingName are confirmed by the existing client code and the
ResearchDesign ones by a live reply, see below; the rest are unverified):
- TrainingDesign: @Rank (tier), @Duration (seconds), @RequiredResearchDesignId ("0" = none), @RequiredRoomType,
  and per-stat chances @HpChance ... @StaminaChance (stat = highest positive chance, ties in canonical order).
- Character: @CharacterId, @CharacterName, @CharacterDesignId, @Level, @RoomId, @Fatigue, @TrainingDesignId
  ("0" = none), @TrainingEndDate, and the nine @*Improvement stats. An empty or missing @TrainingEndDate means idle
  (the design id is dropped too, matching the server snapshot); a present but unparseable one keeps the design id
  with ``training_end`` None, which the planner treats as "training end unknown" (fails closed). A repeated
  @CharacterId keeps its first record.
- CharacterDesign: @TrainingCapacity, @Rarity. Room: @RoomId, @RoomDesignId. RoomDesign: @RoomName.
- Research: @ResearchDesignId, @ResearchState ("Completed" counts). ResearchDesign: @ResearchName,
  @RootResearchDesignId. Item: @ItemDesignId, @Quantity. ItemDesign: @ItemName, @ItemSubType, @Rarity.

Research levels follow one contract (``research_course``), verified against the live ListAllResearchDesigns2 reply
(design version 717, 2026-11-26; tests/fixtures/training/live_research_designs.xml). Live names are
"<family> <course>" ("Fitness 101" ... "Fitness 203") or "<family> Lv<n>" ("Advanced Training Lv2"); a design carries
no level attribute. A completed design counts twice: under its family (the root design's family, via
@RootResearchDesignId, else its own), valued at the highest completed course, for the strategy's band gates; and
under its own exact @ResearchName for design gates. A TrainingDesign's required research is that exact name (e.g.
"Fitness 203"), so a lower course never satisfies it. A required research that cannot be resolved becomes
"research_design:<id>", which no research level matches, so it fails closed. An unknown @RequiredRoomType becomes
None (fails open): the server enforces rooms and the original code never checked it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from sdk.training.model import CANONICAL_STATS, CrewState, TrainingContext, TrainingDesign, frozen_mapping
from sdk.training.strategy import RARITY_STARS, ROOM_ACADEMY, ROOM_GYM, STAT_CODES


# Optional sign, ASCII digits, optional "." and ASCII digits. int()/float() would also take "1_000", Unicode digits,
# exponents and "inf"; none of those is a server number.
_NUMBER_RE = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?")


def to_int(value: object, default: int = 0) -> int:
    """Best-effort integer: ints pass through, decimal strings parse ("7", " 7 ", "+7", "7.0"); else -> default.

    Strings must be an optional sign, ASCII digits and an optional "." with ASCII digits (surrounding whitespace is
    ignored); underscores, Unicode digits, exponents and hex take the default. Non-whole values truncate toward zero
    ("7.6" -> 7, "-3.5" -> -3), so a level or quantity never rounds up. Bools are not counts and take the default;
    NaN, infinities and values beyond int()'s digit limit take the default too.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if _NUMBER_RE.fullmatch(text) is None:
            return default
        try:
            return int(text.partition(".")[0])
        except ValueError:
            return default
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return default


# ASCII digits only (re's \d and int() would otherwise accept any Unicode digit); offset minutes 00-59.
_DATETIME_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:[0-5]\d)?",
    re.ASCII,
)


def parse_datetime(value: object) -> datetime | None:
    """Parse a server timestamp into an aware UTC datetime; empty or malformed values -> None.

    Accepts "%Y-%m-%dT%H:%M:%S" with an optional fraction (truncated to microseconds) and an optional
    "Z" / "±hh:mm" offset (hh < 24, mm < 60). Timestamps without an offset are taken to be UTC (the server's
    convention). The whole stripped string must match: trailing or leading junk, Unicode digits and
    out-of-range fields all fail closed.
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


# Item key -> the element that wraps its records in live payloads ("Research" is *not* "Researchs").
_CONTAINERS: Mapping[str, str] = frozen_mapping(
    {
        "Character": "Characters",
        "CharacterDesign": "CharacterDesigns",
        "TrainingDesign": "TrainingDesigns",
        "Room": "Rooms",
        "RoomDesign": "RoomDesigns",
        "Research": "Researches",
        "ResearchDesign": "ResearchDesigns",
        "Item": "Items",
        "ItemDesign": "ItemDesigns",
    }
)

# classify_payloads keyword -> item key: build_context's payload keywords plus item_designs_raw (consumables).
_PAYLOAD_ITEM_KEYS: Mapping[str, str] = frozen_mapping(
    {
        "characters_raw": "Character",
        "character_designs_raw": "CharacterDesign",
        "training_designs_raw": "TrainingDesign",
        "rooms_raw": "Room",
        "room_designs_raw": "RoomDesign",
        "researches_raw": "Research",
        "research_designs_raw": "ResearchDesign",
        "items_raw": "Item",
        "item_designs_raw": "ItemDesign",
    }
)


def classify_payload(raw: object, item_key: str, container_key: str | None = None) -> str:
    """Tell "no data" apart from "broken response" (ORIGINAL_REQUEST R5).

    error: not a dict, an ``errorMessage`` anywhere (inside dicts or lists), an item element holding something other
    than records (text, numbers, a list without records), or neither the item nor its container is present
    (unexpected schema). empty: the container (or a bare item element) is present but holds no records. ok: at least
    one record. The container defaults to the known live name for ``item_key`` (e.g. "Research" -> "Researches"),
    else ``item_key + "s"``.
    """
    if not isinstance(raw, dict) or _has_error(raw, 0):
        return PAYLOAD_ERROR
    if extract_collection(raw, item_key):
        return PAYLOAD_OK
    if _has_junk_item(raw, item_key, 0):
        return PAYLOAD_ERROR
    container = container_key or _CONTAINERS.get(item_key, item_key + "s")
    if _has_key(raw, (item_key, container), 0):
        return PAYLOAD_EMPTY
    return PAYLOAD_ERROR


def classify_payloads(**raws: object) -> Mapping[str, str]:
    """``classify_payload`` for each given payload, keyed by the same keyword names ``build_context`` takes.

    Keywords: characters_raw, character_designs_raw, training_designs_raw, rooms_raw, room_designs_raw,
    researches_raw, research_designs_raw, items_raw, plus item_designs_raw. Each keyword's item key is pre-bound,
    so a payload cannot be classified against the wrong record type. An unknown keyword raises TypeError (a
    caller bug, not a data problem).
    """
    unknown = sorted(set(raws) - set(_PAYLOAD_ITEM_KEYS))
    if unknown:
        raise TypeError(f"classify_payloads() got unexpected keyword arguments: {unknown}")
    return frozen_mapping({name: classify_payload(raw, _PAYLOAD_ITEM_KEYS[name]) for name, raw in raws.items()})


def _has_key(data: object, keys: tuple[str, ...], depth: int) -> bool:
    if not isinstance(data, dict) or depth > _MAX_DEPTH:
        return False
    if any(key in data for key in keys):
        return True
    return any(_has_key(value, keys, depth + 1) for value in data.values())


_ERROR_KEYS = ("@errorMessage", "errorMessage")


def _has_error(data: object, depth: int) -> bool:
    """An errorMessage key anywhere: dicts and lists (several sibling elements) are both searched."""
    if depth > _MAX_DEPTH:
        return False
    if isinstance(data, dict):
        return any(key in data for key in _ERROR_KEYS) or any(_has_error(value, depth + 1) for value in data.values())
    if isinstance(data, list):
        return any(_has_error(value, depth + 1) for value in data)
    return False


def _is_junk(value: object) -> bool:
    """Item-element content that is neither records nor empty: text, numbers, or a list holding such members."""
    if isinstance(value, list):
        return any(member is not None and not isinstance(member, dict) for member in value)
    return value is not None and not isinstance(value, dict)


def _has_junk_item(data: object, item_key: str, depth: int) -> bool:
    if not isinstance(data, dict) or depth > _MAX_DEPTH:
        return False
    if item_key in data and _is_junk(data[item_key]):
        return True
    return any(_has_junk_item(value, item_key, depth + 1) for value in data.values())


_MAX_DURATION_SECONDS = 10 * 365 * 86400
_ROOM_TYPE_WORDS = frozen_mapping({"gym": ROOM_GYM, "academy": ROOM_ACADEMY})


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


# The " <course>" or " Lv<course>" ending of "<family> <course>": a positive ASCII number without a leading zero.
# Anchored at the end and searched, so a long name costs linear time (a lazy "(.+?)\s+" family is quadratic).
_COURSE_SUFFIX_RE = re.compile(r"\s(?:Lv)?([1-9][0-9]*)\Z", re.ASCII)


def research_course(name: str) -> tuple[str, int]:
    """A research design name -> (family, level): the one research-name contract, shared with Client.getResearchLevel.

    "Fitness 203" -> ("Fitness", 203) and "Advanced Training Lv2" -> ("Advanced Training", 2), the two live shapes.
    Any other name is its own family at level 1 ("Advanced Training", the first of its line, -> 1). Surrounding
    whitespace is stripped; a course of 0, a zero-padded or non-ASCII number is part of the name; a course beyond
    int()'s digit limit reads as level 0 (never a level).
    """
    text = name.strip()
    match = _COURSE_SUFFIX_RE.search(text)
    if match is None:
        return text, 1
    return text[: match.start()].rstrip(), to_int(match.group(1))


def _research_catalog(research_designs_raw: object) -> dict[int, tuple[str, str, int]]:
    """ResearchDesign id -> (family, exact name, level), all from ``research_course``.

    The family is the root design's family (via @RootResearchDesignId, else the design's own family). The exact name
    is the design's own stripped @ResearchName (a nameless design uses "research_design:<id>" at level 1). The level
    is the design's own course number.
    """
    records = _by_id(research_designs_raw, "ResearchDesign", "@ResearchDesignId")
    catalog: dict[int, tuple[str, str, int]] = {}
    for design_id, record in records.items():
        own_name = _text(record.get("@ResearchName"))
        own_family, level = research_course(own_name)
        root = records.get(to_int(record.get("@RootResearchDesignId")))
        root_family = research_course(_text(root.get("@ResearchName")))[0] if root is not None else ""
        fallback = f"research_design:{design_id}"
        catalog[design_id] = (root_family or own_family or fallback, own_name or fallback, level)
    return catalog


def _required_research(research_id: int, catalog: dict[int, tuple[str, str, int]]) -> str | None:
    if research_id <= 0:
        return None
    if research_id in catalog:
        return catalog[research_id][1]
    return f"research_design:{research_id}"


def parse_training_designs(raw: object, research_designs: object = None) -> tuple[TrainingDesign, ...]:
    """TrainingDesign records -> model designs, in payload order; records without a positive id are skipped.

    @RequiredResearchDesignId "0"/missing -> None; otherwise the exact research design name from
    ``research_designs`` (e.g. "Fitness 203"), or "research_design:<id>" when it cannot be resolved (no research of
    that name exists, so it fails closed). @Duration (seconds) is clamped to 0..10 years: a negative value becomes
    0, never a negative timedelta.
    """
    return _parse_training_designs(raw, _research_catalog(research_designs))


def _parse_training_designs(raw: object, catalog: dict[int, tuple[str, str, int]]) -> tuple[TrainingDesign, ...]:
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
    return frozen_mapping(rooms)


def parse_crew(
    characters_raw: object,
    character_designs_raw: object,
    rooms_raw: object = None,
    room_designs_raw: object = None,
) -> tuple[CrewState, ...]:
    """Character records joined with their CharacterDesign (capacity, rarity) and room name.

    A character whose design is missing gets capacity 0 and rarity "" (never another design's values); one without
    a positive @CharacterId is skipped and a repeated @CharacterId keeps its first record. Stat keys are stored
    without the XML '@'; missing/malformed stats are 0. Training fields: see the module docstring.
    """
    return _parse_crew(characters_raw, character_designs_raw, parse_rooms(rooms_raw, room_designs_raw))


def _parse_crew(
    characters_raw: object, character_designs_raw: object, rooms: Mapping[int, str]
) -> tuple[CrewState, ...]:
    designs = _by_id(character_designs_raw, "CharacterDesign", "@CharacterDesignId")
    crew: list[CrewState] = []
    for character_id, record in _by_id(characters_raw, "Character", "@CharacterId").items():
        design_id = to_int(record.get("@CharacterDesignId"))
        design = designs.get(design_id, {})
        room_id = to_int(record.get("@RoomId"))
        raw_end = record.get("@TrainingEndDate")
        no_end = raw_end is None or (isinstance(raw_end, str) and not raw_end.strip())
        training_design_id = 0 if no_end else to_int(record.get("@TrainingDesignId"))
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
                training_end=parse_datetime(raw_end),
            )
        )
    return tuple(crew)


def parse_research_levels(researches_raw: object, research_designs_raw: object) -> Mapping[str, int]:
    """Research family -> highest completed course, plus each completed design's exact name -> its course.

    Only @ResearchState == "Completed" records count; records whose design id is unknown are ignored. The level is
    the design name's course (``research_course``); a record's own attributes never change it. The family keys serve
    the strategy's band gates; the exact names serve TrainingDesign.required_research.
    """
    return _parse_research_levels(researches_raw, _research_catalog(research_designs_raw))


def parse_research_family_levels(researches_raw: object, research_designs_raw: object) -> Mapping[str, int]:
    """Research family -> highest completed course, with no exact-name keys (Client.getResearchLevel's line query).

    The family keys equal ``parse_research_levels``' family keys; a design's exact name never answers a line query.
    """
    levels: dict[str, int] = {}
    for family, _, level in _completed(researches_raw, _research_catalog(research_designs_raw)):
        levels[family] = max(levels.get(family, 0), level)
    return frozen_mapping(levels)


def _parse_research_levels(researches_raw: object, catalog: dict[int, tuple[str, str, int]]) -> Mapping[str, int]:
    levels: dict[str, int] = {}
    for family, exact, level in _completed(researches_raw, catalog):
        for name in (family, exact):
            levels[name] = max(levels.get(name, 0), level)
    return frozen_mapping(levels)


def _completed(researches_raw: object, catalog: dict[int, tuple[str, str, int]]) -> list[tuple[str, str, int]]:
    """The catalog entry of each "Completed" record whose design id the catalog knows, in payload order."""
    entries: list[tuple[str, str, int]] = []
    for record in extract_collection(researches_raw, "Research"):
        if _text(record.get("@ResearchState")) != "Completed":
            continue
        entry = catalog.get(to_int(record.get("@ResearchDesignId")))
        if entry is not None:
            entries.append(entry)
    return entries


def parse_items(items_raw: object) -> Mapping[int, int]:
    """Item design id -> total quantity on the ship (@Quantity summed; missing -> 1; malformed/negative -> 0)."""
    stock: dict[int, int] = {}
    for record in extract_collection(items_raw, "Item"):
        design_id = to_int(record.get("@ItemDesignId"))
        if design_id <= 0:
            continue
        quantity = to_int(record.get("@Quantity")) if "@Quantity" in record else 1
        stock[design_id] = stock.get(design_id, 0) + max(quantity, 0)
    return frozen_mapping(stock)


_STAT_TOKENS = frozen_mapping(
    {
        **{code: code for code in STAT_CODES.values()},
        **{stat.removesuffix("Improvement").upper(): code for stat, code in STAT_CODES.items()},
    }
)
_TIER_TOKENS = frozen_mapping({tier.upper(): tier for tier in RARITY_STARS})


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
    return frozen_mapping(found)


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
    """Assemble a TrainingContext from raw payloads; ``now`` must be timezone-aware (the model rejects naive).

    Rooms are parsed once and shared, so each crew's ``room_name`` and ``ctx.rooms`` come from one source; the
    research catalog is likewise built once for both the design gates and the research levels.
    """
    rooms = parse_rooms(rooms_raw, room_designs_raw)
    catalog = _research_catalog(research_designs_raw)
    return TrainingContext(
        now=now,
        crew=_parse_crew(characters_raw, character_designs_raw, rooms),
        designs=_parse_training_designs(training_designs_raw, catalog),
        rooms=rooms,
        research_levels=_parse_research_levels(researches_raw, catalog),
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


# --- SPEC-14 observations (S14-R2) -----------------------------------------------------------------------------
# Imported here, not at the top: automation/mutation/config.json pins two equivalent mutants of this file by line
# number (94 and 99), so everything above this section must stay byte for byte as it is.
from sdk.training.model import (
    CURRENCY_STARBUX,
    PRICE_CURRENCIES,
    PRICE_MALFORMED,
    PRICE_OK,
    PRICE_PREMIUM,
    PRICE_UNAVAILABLE,
    SESSION_ENDED_UNCLAIMED,
    SESSION_FINISHED,
    SESSION_NONE,
    SESSION_RUNNING,
    SESSION_UNKNOWN,
    CourseObservation,
    CoursePrice,
    PrestigeRecipe,
    RoomDesignObservation,
    TrainingSession,
)

# A whole number: ASCII digits only, no sign, no fraction, no separator. Unlike ``to_int``, nothing malformed reads
# as 0: an observation must keep "not readable" (None) apart from an observed zero.
_WHOLE_RE = re.compile(r"[0-9]+")


def _whole_or_none(value: object) -> int | None:
    """A whole number from a non-bool int >= 0 or a string of ASCII digits (surrounding whitespace ignored).

    Anything else (a bool, a negative int, a float, other text) -> None.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _WHOLE_RE.fullmatch(text) is None:
        return None
    try:
        return int(text)
    except ValueError:  # beyond int()'s digit limit
        return None


def _stripped_or_none(value: object) -> str | None:
    """Reported text, stripped ("" when blank); None when it is not reported as text."""
    return value.strip() if isinstance(value, str) else None


def parse_course_price(value: object) -> CoursePrice:
    """A TrainingDesign @CostString ("<currency>:<amount>") -> a classified ``CoursePrice``.

    Missing or blank -> unavailable. Mineral or gas with a whole amount -> ok (an observed "mineral:0" stays ok, 0).
    Starbux with a whole amount -> premium. Case and whitespace around the currency and the amount are tolerated, as
    are leading zeros; anything that could change the amount (a sign, a fraction, a separator, a Unicode digit, a
    second ":", a letter) or an unknown currency -> malformed. The amount is read before the currency is classed, so
    a malformed Starbux price is malformed, not premium.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return CoursePrice(PRICE_UNAVAILABLE)
    if not isinstance(value, str):
        return CoursePrice(PRICE_MALFORMED)
    head, _, tail = value.strip().partition(":")
    currency = head.strip().lower()
    amount = _whole_or_none(tail)
    if currency not in PRICE_CURRENCIES or amount is None:
        return CoursePrice(PRICE_MALFORMED)
    return CoursePrice(PRICE_PREMIUM if currency == CURRENCY_STARBUX else PRICE_OK, currency, amount)


def parse_course_observations(raw: object) -> tuple[CourseObservation, ...]:
    """TrainingDesign records -> ``CourseObservation``s, in payload order (the first record of a repeated id wins).

    Unlike ``parse_training_designs``, nothing defaults: @Rank, @RequiredRoomLevel and @RequiredResearchDesignId are
    whole numbers ("0" stays 0: no minimum, no research) or None when missing or malformed. @RequiredRoomType is kept
    as stripped text: None when the record does not report it (unknown), "" when it reports a blank requirement
    (explicitly no room requirement). The price comes from @CostString (``parse_course_price``).
    """
    return tuple(
        CourseObservation(
            design_id=design_id,
            name=_text(record.get("@TrainingName")),
            rank=_whole_or_none(record.get("@Rank")),
            stat=_design_stat(record),
            required_room_name=_stripped_or_none(record.get("@RequiredRoomType")),
            required_room_level=_whole_or_none(record.get("@RequiredRoomLevel")),
            required_research_design_id=_whole_or_none(record.get("@RequiredResearchDesignId")),
            price=parse_course_price(record.get("@CostString")),
        )
        for design_id, record in _by_id(raw, "TrainingDesign", "@TrainingDesignId").items()
    )


def parse_room_design_observations(raw: object) -> Mapping[int, RoomDesignObservation]:
    """RoomDesign id -> ``RoomDesignObservation`` (read-only; the first record of a repeated id wins).

    @Level and @Capacity are whole numbers ("0" stays 0) or None when missing or malformed; @RoomName is stripped.
    @RoomType is stripped text ("" when blank) or None when the record does not report it.
    """
    return frozen_mapping(
        {
            design_id: RoomDesignObservation(
                design_id=design_id,
                name=_text(record.get("@RoomName")),
                level=_whole_or_none(record.get("@Level")),
                capacity=_whole_or_none(record.get("@Capacity")),
                room_type=_stripped_or_none(record.get("@RoomType")),
            )
            for design_id, record in _by_id(raw, "RoomDesign", "@RoomDesignId").items()
        }
    )


def parse_training_session(course: object, end: object, at: datetime) -> TrainingSession:
    """A crew member's @TrainingDesignId and @TrainingEndDate, judged at the aware observation time ``at``.

    | End                       | Course                               | Phase           | course_id | end    |
    |---------------------------|--------------------------------------|-----------------|-----------|--------|
    | missing (None)            | -                                    | unknown         | None      | None   |
    | ""                        | -                                    | none            | None      | None   |
    | unreadable (incl. blank)  | -                                    | unknown         | positive  | None   |
    | after ``at``              | -                                    | running         | positive  | the end|
    | at or before ``at``       | a positive id                        | ended_unclaimed | the id    | the end|
    | at or before ``at``       | an explicit zero ("0", "00", 0)      | finished        | None      | the end|
    | at or before ``at``       | missing, blank or malformed          | unknown         | None      | None   |

    "positive" means the course id when it is a positive whole number (``_whole_or_none``), else None. The end is
    aware UTC. Only an explicit zero says "no course": a missing or blank course with an ended timer could be an
    ended course not yet claimed, so it fails closed. A naive ``at`` raises ValueError, whatever the record holds.

    Unlike ``parse_crew`` (where a missing or whitespace-only end reads as idle), a missing end is unknown here and
    only an exactly empty end is "none": a whitespace-only end is unreadable, as it is for the catalog's training
    phase.
    """
    if at.utcoffset() is None:
        raise ValueError("at must be a timezone-aware datetime")
    if end is None:
        return TrainingSession(None, None, SESSION_UNKNOWN)
    if end == "":
        return TrainingSession(None, None, SESSION_NONE)
    whole = _whole_or_none(course)
    course_id = whole or None
    parsed = parse_datetime(end)
    if parsed is None:
        return TrainingSession(course_id, None, SESSION_UNKNOWN)
    if parsed > at:
        return TrainingSession(course_id, parsed, SESSION_RUNNING)
    if course_id is not None:
        return TrainingSession(course_id, parsed, SESSION_ENDED_UNCLAIMED)
    if whole == 0:
        return TrainingSession(None, parsed, SESSION_FINISHED)
    return TrainingSession(None, None, SESSION_UNKNOWN)


def parse_prestige_recipes(raw: object) -> tuple[PrestigeRecipe, ...]:
    """Prestige records -> ``PrestigeRecipe``s, in payload order; identical rows are all kept.

    A record needs three positive whole design ids (@CharacterDesignId1, @CharacterDesignId2, @ToCharacterDesignId);
    any other record is skipped. Both ingredients may be the same design (a two-copy recipe).
    """
    recipes: list[PrestigeRecipe] = []
    for record in extract_collection(raw, "Prestige"):
        first = _whole_or_none(record.get("@CharacterDesignId1"))
        second = _whole_or_none(record.get("@CharacterDesignId2"))
        target = _whole_or_none(record.get("@ToCharacterDesignId"))
        if first and second and target:
            recipes.append(PrestigeRecipe(first, second, target))
    return tuple(recipes)
