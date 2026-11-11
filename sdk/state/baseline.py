"""Sanitized per-account baseline files (SPEC-04, R0).

A baseline records one ``AccountSnapshot`` as a stable JSON file, together
with the game version it was observed on. The module performs no network
traffic. It only serialises and parses snapshots that were already built.

- ``write_baseline`` writes ``<alias>-<UTC time>.json`` and never overwrites an
  existing file (S04-R4).
- ``load_baseline`` reads the file back into an ``AccountSnapshot`` equal to the
  one written, so ``diff_snapshots`` can compare a later snapshot with it
  (S04-R5).
- Nothing is written or loaded if the text would trip the secret scan: the
  ``sdk.redaction`` patterns plus a numeric id in a field named like
  ``UserId`` or ``Id`` (S04-R2). ``scan_baseline_directory`` applies the same
  rules to every file under a directory such as ``docs/experiments/baselines/``
  (S04-R7). They are the rules SPEC-03 (S03-R2) sets for sanitized captures.

A section that was not observed is ``null`` in the file, never ``0`` or an
empty list. The snapshot's ``game_identity`` fingerprint is kept; a raw user
id is refused.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from sdk.redaction import REDACTION_PATTERNS
from sdk.state.snapshot import (
    ACCOUNT_ALIAS_PATTERN,
    SECTIONS,
    AccountSnapshot,
    Completeness,
    CrewMember,
    InventoryItem,
    Research,
    Resources,
    Room,
    SnapshotInputError,
    check_game_identity,
)

BASELINE_FORMAT = "tachikoma-account-baseline"
BASELINE_FORMAT_VERSION = 1
FILENAME_TIME_FORMAT = "%Y%m%dT%H%M%SZ"
LIST_SECTIONS = ("crew", "rooms", "research", "inventory")

# A numeric id in a field named ``Id`` or ending in ``UserId`` (any case, an
# optional ``_`` before ``id``), as an XML attribute or a JSON key. ``room_id``
# or ``@RoomDesignId`` do not match; ``UserId="0"`` (no user) does not match.
NUMERIC_ID_FIELD = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z0-9]*user_?id|id)[\"']?\s*[=:]\s*[\"']?0*[1-9]",
    re.IGNORECASE,
)

_TOP_LEVEL_KEYS = frozenset(
    {
        "format",
        "format_version",
        "account_alias",
        "observed_at",
        "game_version",
        "client_version",
        "game_identity",
        "source",
        "hull_level",
        "trophies",
        "resources",
        "crew",
        "rooms",
        "research",
        "inventory",
        "completeness",
        "notes",
    }
)
_RESOURCE_KEYS = ("minerals", "gas", "starbux")
_CREW_KEYS = (
    "character_id",
    "design_id",
    "name",
    "level",
    "fatigue",
    "room_id",
    "training_observed",
    "training_design_id",
    "training_end",
)
_ROOM_KEYS = ("room_id", "design_id", "status")
_RESEARCH_KEYS = ("design_id", "state")
_INVENTORY_KEYS = ("item_design_id", "quantity")

PathLike = Union[str, Path]


class BaselineError(ValueError):
    """Raised when a baseline cannot be written or loaded safely.

    The message never repeats the value that was refused.
    """


class BaselineExistsError(FileExistsError):
    """Raised when a baseline for the same alias and time already exists."""


@dataclass(frozen=True)
class ScanFinding:
    """One secret-scan hit. ``rule`` names the rule, never the matched text."""

    path: Optional[Path]
    line: int
    rule: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.path is not None else f"line {self.line}"
        return f"{where}: {self.rule}"


@dataclass(frozen=True)
class BaselineFile:
    """A loaded baseline: the snapshot plus the game version it was observed on."""

    path: Path
    game_version: Optional[str]
    snapshot: AccountSnapshot


# ---------------------------------------------------------------- scanning


def scan_text(text: str, path: Optional[Path] = None) -> list[ScanFinding]:
    """Return every line of ``text`` that the secret scan rejects.

    The rules are the ``sdk.redaction`` patterns (anything ``redact_secrets``
    would change) and a numeric id in a field named like ``UserId`` or ``Id``.
    """
    findings = []
    for number, line in enumerate(text.splitlines() or [text], start=1):
        for index, (pattern, _replacement) in enumerate(REDACTION_PATTERNS, start=1):
            if pattern.search(line):
                findings.append(ScanFinding(path, number, f"sdk.redaction pattern {index}"))
        if NUMERIC_ID_FIELD.search(line):
            findings.append(ScanFinding(path, number, "numeric id in a UserId/Id field"))
    return findings


def scan_baseline_directory(directory: PathLike) -> list[ScanFinding]:
    """Scan every file under ``directory`` (recursively) with ``scan_text``.

    A ``.json`` file is also decoded and each string scanned
    (``scan_json_values``), so JSON escaping cannot hide a match.

    A missing directory has nothing to scan. A file that is not UTF-8 text
    cannot be checked, so it is reported as a finding rather than skipped.
    """
    root = Path(directory)
    if not root.is_dir():
        return []
    findings: list[ScanFinding] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            findings.append(ScanFinding(path, 0, "not readable as UTF-8 text"))
            continue
        findings.extend(scan_text(text, path))
        if path.suffix.lower() == ".json":
            findings.extend(_scan_json_text(text, path))
    return findings


def _json_strings(value: Any):
    """Yield every key and string value in a decoded JSON document."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)


def scan_json_values(document: Any, path: Optional[Path] = None) -> list[ScanFinding]:
    """Scan each decoded string of a JSON document.

    JSON escapes quotes and backslashes, so a value such as
    ``"DeviceKey":"..."`` inside a string does not match the patterns in the
    raw file text. Scanning the decoded strings closes that gap. Findings
    carry line 0 because a decoded value has no line of its own.
    """
    findings = []
    for text in _json_strings(document):
        for finding in scan_text(text, path):
            findings.append(ScanFinding(path, 0, f"{finding.rule} (in a JSON string)"))
    return findings


def _scan_json_text(text: str, path: Optional[Path]) -> list[ScanFinding]:
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return []  # not JSON: the line scan above is all that applies
    return scan_json_values(document, path)


def _refuse_if_flagged(text: str, action: str) -> None:
    findings = scan_text(text) + _scan_json_text(text, None)
    if findings:
        rules = sorted({finding.rule for finding in findings})
        raise BaselineError(f"refusing to {action} a baseline that fails the secret scan: {', '.join(rules)}")


# ---------------------------------------------------------------- writing


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise BaselineError("observed_at must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def baseline_filename(account_alias: str, observed_at: datetime) -> str:
    """Return ``<alias>-<YYYYMMDDTHHMMSSZ>.json`` for one alias and UTC second."""
    if not isinstance(account_alias, str) or not ACCOUNT_ALIAS_PATTERN.match(account_alias):
        raise BaselineError("account_alias must be a lowercase alias, not an e-mail or display name")
    return f"{account_alias}-{_utc(observed_at).strftime(FILENAME_TIME_FORMAT)}.json"


def _optional_text(value: Any, label: str) -> Optional[str]:
    if value is not None and not isinstance(value, str):
        raise BaselineError(f"{label} must be a string or None")
    return value


def baseline_record(snapshot: AccountSnapshot, game_version: Optional[str]) -> dict[str, Any]:
    """Return the JSON-ready baseline record for ``snapshot``.

    ``game_version`` is the version the observation was made on; the
    snapshot's own ``game_version`` is kept as ``client_version``. A list
    section whose completeness is ``missing`` is written as ``None``.
    """
    if not isinstance(snapshot, AccountSnapshot):
        raise BaselineError("snapshot must be an AccountSnapshot")
    game_version = _optional_text(game_version, "game_version")
    baseline_filename(snapshot.account_alias, snapshot.observed_at)  # validates alias and time
    try:
        game_identity = check_game_identity(snapshot.game_identity)
    except SnapshotInputError as error:
        raise BaselineError(str(error)) from None
    completeness = {name: snapshot.section(name).value for name in SECTIONS}

    def section(name: str, rows: list[dict[str, Any]]) -> Optional[list[dict[str, Any]]]:
        if completeness[name] == Completeness.MISSING.value:
            if rows:
                raise BaselineError(f"{name} is marked missing but holds records")
            return None
        return rows

    return {
        "format": BASELINE_FORMAT,
        "format_version": BASELINE_FORMAT_VERSION,
        "account_alias": snapshot.account_alias,
        "observed_at": _utc(snapshot.observed_at).isoformat(),
        "game_version": game_version,
        "client_version": snapshot.game_version,
        "game_identity": game_identity,
        "source": snapshot.source,
        "hull_level": snapshot.hull_level,
        "trophies": snapshot.trophies,
        "resources": {key: getattr(snapshot.resources, key) for key in _RESOURCE_KEYS},
        "crew": section("crew", [{key: getattr(m, key) for key in _CREW_KEYS} for m in snapshot.crew]),
        "rooms": section("rooms", [{key: getattr(r, key) for key in _ROOM_KEYS} for r in snapshot.rooms]),
        "research": section(
            "research", [{key: getattr(e, key) for key in _RESEARCH_KEYS} for e in snapshot.research]
        ),
        "inventory": section(
            "inventory", [{key: getattr(i, key) for key in _INVENTORY_KEYS} for i in snapshot.inventory]
        ),
        "completeness": completeness,
        "notes": list(snapshot.notes),
    }


def dumps_baseline(record: Mapping[str, Any]) -> str:
    """Serialise a baseline record as stable JSON (sorted keys, two-space indent)."""
    return json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_baseline(snapshot: AccountSnapshot, directory: PathLike, game_version: Optional[str]) -> Path:
    """Write ``snapshot`` to ``directory/<alias>-<UTC time>.json`` and return the path.

    Refuses (``BaselineExistsError``) when a file for the same alias and UTC
    second already exists, and refuses (``BaselineError``) anything that fails
    the secret scan. Nothing is written when either refusal applies.
    """
    record = baseline_record(snapshot, game_version)
    text = dumps_baseline(record)
    _refuse_if_flagged(text, "write")
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / baseline_filename(snapshot.account_alias, snapshot.observed_at)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError:
        raise BaselineExistsError(
            f"a baseline for {snapshot.account_alias!r} at this time already exists: {path.name}"
        ) from None
    return path


# ---------------------------------------------------------------- loading


def _expect_keys(value: Any, keys: tuple[str, ...], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise BaselineError(f"{label} must hold exactly the keys {', '.join(keys)}")
    return value


def _opt_int(value: Any, label: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise BaselineError(f"{label} must be an integer or null")
    return value


def _req_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BaselineError(f"{label} must be a non-empty string")
    return value


def _rows(record: Mapping[str, Any], name: str, keys: tuple[str, ...], completeness: Completeness) -> list:
    value = record[name]
    if completeness is Completeness.MISSING:
        if value is not None:
            raise BaselineError(f"{name} is marked missing, so it must be null")
        return []
    if not isinstance(value, list):
        raise BaselineError(f"{name} must be a list when observed")
    return [_expect_keys(row, keys, f"{name} record") for row in value]


def _parse_record(record: Any) -> tuple[Optional[str], AccountSnapshot]:
    if not isinstance(record, Mapping) or set(record) != _TOP_LEVEL_KEYS:
        raise BaselineError("baseline must hold exactly the documented top-level keys")
    if record["format"] != BASELINE_FORMAT or record["format_version"] != BASELINE_FORMAT_VERSION:
        raise BaselineError("unsupported baseline format or version")

    alias = record["account_alias"]
    if not isinstance(alias, str) or not ACCOUNT_ALIAS_PATTERN.match(alias):
        raise BaselineError("account_alias must be a lowercase alias")
    try:
        observed_at = datetime.fromisoformat(_req_text(record["observed_at"], "observed_at"))
    except ValueError:
        raise BaselineError("observed_at must be an ISO 8601 time") from None
    if observed_at.utcoffset() is None or observed_at.utcoffset().total_seconds() != 0:
        raise BaselineError("observed_at must be in UTC")
    try:
        game_identity = check_game_identity(record["game_identity"])
    except SnapshotInputError as error:
        raise BaselineError(str(error)) from None

    raw_completeness = _expect_keys(record["completeness"], SECTIONS, "completeness")
    try:
        completeness = {name: Completeness(raw_completeness[name]) for name in SECTIONS}
    except ValueError:
        raise BaselineError("completeness values must be complete, partial or missing") from None

    resources = _expect_keys(record["resources"], _RESOURCE_KEYS, "resources")
    notes = record["notes"]
    if not isinstance(notes, list) or not all(isinstance(note, str) for note in notes):
        raise BaselineError("notes must be a list of strings")

    def text(value: Any, label: str) -> Optional[str]:
        return _optional_text(value, label)

    crew = tuple(
        CrewMember(
            character_id=_req_text(row["character_id"], "crew.character_id"),
            design_id=text(row["design_id"], "crew.design_id"),
            name=text(row["name"], "crew.name"),
            level=_opt_int(row["level"], "crew.level"),
            fatigue=_opt_int(row["fatigue"], "crew.fatigue"),
            room_id=text(row["room_id"], "crew.room_id"),
            training_observed=_bool(row["training_observed"], "crew.training_observed"),
            training_design_id=text(row["training_design_id"], "crew.training_design_id"),
            training_end=text(row["training_end"], "crew.training_end"),
        )
        for row in _rows(record, "crew", _CREW_KEYS, completeness["crew"])
    )
    rooms = tuple(
        Room(
            room_id=_req_text(row["room_id"], "rooms.room_id"),
            design_id=text(row["design_id"], "rooms.design_id"),
            status=text(row["status"], "rooms.status"),
        )
        for row in _rows(record, "rooms", _ROOM_KEYS, completeness["rooms"])
    )
    research = tuple(
        Research(
            design_id=_req_text(row["design_id"], "research.design_id"),
            state=text(row["state"], "research.state"),
        )
        for row in _rows(record, "research", _RESEARCH_KEYS, completeness["research"])
    )
    inventory = tuple(
        InventoryItem(
            item_design_id=_req_text(row["item_design_id"], "inventory.item_design_id"),
            quantity=_opt_int(row["quantity"], "inventory.quantity"),
        )
        for row in _rows(record, "inventory", _INVENTORY_KEYS, completeness["inventory"])
    )

    snapshot = AccountSnapshot(
        account_alias=alias,
        observed_at=observed_at,
        source=_req_text(record["source"], "source"),
        game_version=text(record["client_version"], "client_version"),
        hull_level=_opt_int(record["hull_level"], "hull_level"),
        trophies=_opt_int(record["trophies"], "trophies"),
        resources=Resources(**{key: _opt_int(resources[key], f"resources.{key}") for key in _RESOURCE_KEYS}),
        crew=crew,
        rooms=rooms,
        research=research,
        inventory=inventory,
        completeness=completeness,
        notes=tuple(notes),
        game_identity=game_identity,
    )
    return text(record["game_version"], "game_version"), snapshot


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise BaselineError(f"{label} must be true or false")
    return value


def load_baseline_file(path: PathLike) -> BaselineFile:
    """Load and validate a baseline file, returning its snapshot and game version.

    The file must pass the secret scan and match the documented format
    exactly; unknown keys are refused rather than ignored.
    """
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    _refuse_if_flagged(text, "load")
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        raise BaselineError(f"{source.name} is not valid JSON") from None
    game_version, snapshot = _parse_record(record)
    return BaselineFile(path=source, game_version=game_version, snapshot=snapshot)


def load_baseline(path: PathLike) -> AccountSnapshot:
    """Load a baseline file back into the ``AccountSnapshot`` that was written."""
    return load_baseline_file(path).snapshot
