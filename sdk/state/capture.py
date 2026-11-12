"""Sanitized captures of read-only responses for SPEC-03 (S03-R1, S03-R2).

``run.py --capture-reads`` logs each enabled registry account in and keeps the
responses of a fixed list of read-only reads (``CAPTURE_READS``). This module
turns each response into a file the owner can review and copy into
``tests/fixtures/live_sanitized/``. Nothing is written before it is sanitized
and scanned.

Sanitizing (``sanitize_payload``) walks the parsed response (xmltodict output,
or raw XML text that is parsed first) and replaces values by the name of the
attribute, key or element that holds them:

- secret fields: a name containing ``token``, ``secret``, ``password``,
  ``checksum``, ``email`` or ``devicekey`` (``AccessToken``, ``RefreshToken``,
  ``DeviceKey``, ``Checksum``, ``Email``, ``Password``...) becomes
  ``***REDACTED***`` (``***REDACTED_EMAIL***`` for e-mail fields);
- user ids: ``Id``, ``AccountId`` and any name ending in ``UserId``, in any
  case, become ``USER-A``, ``USER-B``...;
- names: ``Name`` and names ending in ``UserName`` or ``PlayerName`` become
  ``PLAYER-A``..., names ending in ``ShipName`` ``SHIP-A``..., and names
  ending in ``AllianceName`` ``ALLIANCE-A``...;
- any other value that ``sdk.redaction.redact_secrets``, the caller's
  ``scrub_text`` (``run.py`` passes the fleet ``LogScrubber``, which knows the
  account's credential values and display name) or ``scan_capture_text``
  would change or flag becomes ``***REDACTED***``.

Placeholders come from one ``Placeholders`` per account: the same value gets
the same placeholder in every file of that account, so cross-references (a
battle's ``AttackingUserId`` and the profile's ``Id``) still line up. The
account's own user id and display name are seeded first, so they are
``USER-A`` and ``PLAYER-A``. Placeholders are letters, never numbers, and are
not shared between accounts.

Scanning (``scan_capture_text``) applies the S03-R2 rules that
``tests/test_profile_reads.py`` applies to ``tests/fixtures/live_sanitized/``
(the rules are duplicated here so that the command and the fixture test use
the same checks; ``tests/test_capture_reads.py`` keeps the two in step). A
rendered capture with any finding is withheld: no file is written and only
the rule and line are reported, never the value.

``capture_account`` makes the reads. It calls only the methods in
``CAPTURE_READS`` (all ``read-only`` in ``docs/baseline/capability-inventory.json``);
the profile and the server settings are the responses ``Client.login``
already holds. While it runs, the client's ``quickReload`` is replaced so an
expired session raises instead of logging in again.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Optional, Sequence

import xmltodict

from sdk.redaction import REDACTION_PATTERNS, redact_secrets

CAPTURE_SOURCE = "run.py --capture-reads"
MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = "tachikoma-capture-manifest"
MANIFEST_FORMAT_VERSION = 1
REDACTED = "***REDACTED***"
REDACTED_EMAIL = "***REDACTED_EMAIL***"
# Columns of the "Record of captures" table in tests/fixtures/live_sanitized/README.md.
README_COLUMNS = ("File", "Endpoint", "Captured (date)", "Game version", "Sanitization steps applied", "Reviewed by")
REVIEW_PENDING = "pending"


# --- Reads -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class CaptureRead:
    """One response to capture: the file stem, the server endpoint, how it is read and where it is held.

    ``method`` is the ``Client`` method called, or ``None`` for a response
    ``Client.login`` already holds. ``wrap`` names the root element the held
    value is put under when the client keeps only part of the response.
    """

    stem: str
    endpoint: str
    method: Optional[str]
    attribute: str
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    wrap: Optional[str] = None
    note: Optional[str] = None


CAPTURE_READS: tuple[CaptureRead, ...] = (
    CaptureRead("user_login", "DeviceLogin17", None, "info", wrap="User",
                note="the User record of the login response, as Client.login holds it (the UserLogin wrapper is not "
                     "kept)"),
    CaptureRead("get_latest_version3", "GetLatestVersion3", None, "latestVersion",
                note="the server settings Client.login read"),
    CaptureRead("get_ship_by_user_id", "GetShipByUserId", "getShipByUserId", "shipByUserId"),
    CaptureRead("list_all_characters_of_user", "ListAllCharactersOfUser", "listAllCharactersOfUser",
                "allCharactersOfUser"),
    CaptureRead("list_items_of_a_ship", "ListItemsOfAShip", "listItemsOfAShip", "itemsOfAShip"),
    CaptureRead("find_user_ranking", "FindUserRanking", "findUserRanking", "userRanking"),
    CaptureRead("list_pvp_battles2", "ListPvPBattles2", "listPvPBattles2", "pvpBattles", {"take": 25}),
)
# Every Client method the command calls after login, in call order.
CAPTURE_METHODS: tuple[str, ...] = tuple(read.method for read in CAPTURE_READS if read.method)


# --- Scan (S03-R2) ---------------------------------------------------------------------------------

# Field names that hold a user id: ``Id`` exactly, or anything ending in ``UserId``, optionally
# ``@``-prefixed as xmltodict writes attributes. Design ids such as ``ShipDesignId`` are game data.
_USER_ID_KEY = r"(?<![A-Za-z0-9_])@?(?:[A-Za-z_]*UserId|Id)"
_NUMERIC_USER_ID_PATTERNS = (
    # XML attribute, JSON/dict key or query parameter: UserId="5", "@Id": 5, userId=5
    re.compile(_USER_ID_KEY + r"""["']?\s*[:=]\s*["']?\s*[-+]?\d""", re.IGNORECASE),
    # XML element: <UserId>5</UserId>
    re.compile(r"<" + _USER_ID_KEY + r"""(?:\s[^>]*)?>\s*[-+]?\d""", re.IGNORECASE),
)
_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_UUID_PATTERN = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_LONG_HEX_PATTERN = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_SECRET_FIELD_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])@?[A-Za-z_]*(?:token|secret|password|checksum|devicekey)[A-Za-z_]*"
    r"""["']?\s*[:=]\s*["']?(?P<value>[^\s"'&<>,;}\]]+)""",
    re.IGNORECASE,
)
_ALLOWED_SECRET_FIELD_VALUES = {"null", "none"}
_REDACTION_MARKER_PREFIX = "***REDACTED"


class Finding(NamedTuple):
    """One scan hit. Holds where and what kind, never the matched value."""

    path: str
    line: int
    kind: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}"


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def scan_capture_text(text: str, path: str = "<text>") -> list[Finding]:
    """Every secret or user-id finding in ``text`` under the S03-R2 rules, sorted; values are never kept."""
    findings = []

    def add(match: re.Match, kind: str) -> None:
        findings.append(Finding(path, _line_of(text, match.start()), kind))

    for index, (pattern, replacement) in enumerate(REDACTION_PATTERNS):
        for match in pattern.finditer(text):
            # A value already reduced to the module's own marker redacts to itself; anything else is
            # something the module would hide.
            if match.expand(replacement) != match.group(0):
                add(match, f"redaction pattern {index}")
    for pattern in _NUMERIC_USER_ID_PATTERNS:
        for match in pattern.finditer(text):
            add(match, "numeric user id")
    for match in _EMAIL_PATTERN.finditer(text):
        add(match, "e-mail address")
    for pattern in (_UUID_PATTERN, _LONG_HEX_PATTERN):
        for match in pattern.finditer(text):
            add(match, "token-like string")
    for match in _SECRET_FIELD_PATTERN.finditer(text):
        value = match.group("value")
        if value.lower() in _ALLOWED_SECRET_FIELD_VALUES or value.startswith(_REDACTION_MARKER_PREFIX):
            continue
        add(match, "token-like string")
    return sorted(set(findings))


def describe_findings(findings: Iterable[Finding]) -> str:
    """The rules that fired and their lines (``numeric user id (line 4)``), never a value."""
    lines: dict[str, list[int]] = {}
    for finding in findings:
        lines.setdefault(finding.kind, []).append(finding.line)
    return "; ".join(f"{kind} (line {', '.join(str(n) for n in sorted(set(numbers)))})"
                     for kind, numbers in sorted(lines.items()))


# --- Sanitize (S03-R1) -----------------------------------------------------------------------------

SECRET_NAME_WORDS = ("token", "secret", "password", "checksum", "email", "devicekey")
USER, PLAYER, SHIP, ALLIANCE = "USER", "PLAYER", "SHIP", "ALLIANCE"
STEP_LABELS = {
    "body": "2: response body only (no request URL or headers)",
    "secret": "3: secret fields set to " + REDACTED,
    USER: "4: user ids replaced with USER-* placeholders",
    "name": "5: player, ship and alliance names replaced with PLAYER-*, SHIP-*, ALLIANCE-* placeholders",
    "value": "6: other values the redaction or scan rules flag set to " + REDACTED,
}
STEP_ORDER = ("body", "secret", USER, "name", "value")


class CaptureWithheld(Exception):
    """A response that cannot be written. The message names a rule, never a value."""


def _letters(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA: placeholder labels that are never numeric."""
    label = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label


class Placeholders:
    """One account's stable placeholders: the same value always gets the same ``USER-A``-style label."""

    def __init__(self) -> None:
        self._labels: dict[str, dict[str, str]] = {}

    def get(self, category: str, value: str) -> str:
        labels = self._labels.setdefault(category, {})
        if value not in labels:
            labels[value] = f"{category}-{_letters(len(labels))}"
        return labels[value]

    def seed(self, category: str, value: Any) -> None:
        """Reserve the next label for ``value`` (the account's own id and name go first)."""
        if value is not None and str(value).strip():
            self.get(category, str(value))


def field_category(name: str) -> Optional[str]:
    """``secret``, ``email``, ``USER``, ``PLAYER``, ``SHIP``, ``ALLIANCE`` or ``None`` for a field name."""
    lower = name.lstrip("@").lower()
    if "email" in lower:
        return "email"
    if any(word in lower for word in SECRET_NAME_WORDS):
        return "secret"
    if lower in ("id", "accountid") or lower.endswith("userid") or lower.endswith("accountid"):
        return USER
    if lower in ("name", "playername") or lower.endswith("username") or lower.endswith("playername"):
        return PLAYER
    if lower.endswith("shipname"):
        return SHIP
    if lower.endswith("alliancename"):
        return ALLIANCE
    return None


def _flagged(value: str, scrub_text: Callable[[str], str]) -> bool:
    return redact_secrets(value) != value or scrub_text(value) != value or bool(scan_capture_text(value))


def sanitize_payload(payload: Any, placeholders: Placeholders,
                     scrub_text: Callable[[str], str] = redact_secrets) -> tuple[Any, Counter]:
    """A sanitized copy of parsed ``payload`` and how many values each step replaced (see the module doc)."""
    counts: Counter = Counter()

    def value_for(name: Optional[str], value: str) -> str:
        if value == "":
            return value
        category = field_category(name) if name else None
        if category == "email":
            counts["secret"] += 1
            return REDACTED_EMAIL
        if category == "secret":
            counts["secret"] += 1
            return REDACTED
        if category == USER:
            counts[USER] += 1
            return placeholders.get(USER, value)
        if category in (PLAYER, SHIP, ALLIANCE):
            counts["name"] += 1
            return placeholders.get(category, value)
        if _flagged(value, scrub_text):
            counts["value"] += 1
            return REDACTED
        return value

    def walk(node: Any, name: Optional[str]) -> Any:
        if isinstance(node, Mapping):
            # Element text (``#text``) belongs to the element that holds it.
            return {key: walk(value, name if key == "#text" else key) for key, value in node.items()}
        if isinstance(node, (list, tuple)):
            return [walk(item, name) for item in node]
        if isinstance(node, str):
            return value_for(name, node)
        if isinstance(node, (int, float)) and not isinstance(node, bool) and name and field_category(name):
            return value_for(name, str(node))  # a numeric id in JSON-shaped input
        return node  # None, booleans and other numbers: nothing to hide

    return walk(payload, None), counts


def parse_response(response: Any) -> Any:
    """``response`` as xmltodict output: dicts pass through, raw XML text or bytes is parsed."""
    if isinstance(response, (bytes, bytearray)):
        response = bytes(response).decode("utf-8", errors="replace")
    if isinstance(response, str):
        try:
            return xmltodict.parse(response, xml_attribs=True)
        except Exception:  # expat errors carry no value, but say only the rule
            raise CaptureWithheld("response is not well-formed XML") from None
    return response


def render(payload: Any) -> tuple[str, str]:
    """``payload`` as XML (one root element) or JSON otherwise; returns the text and the file extension."""
    if isinstance(payload, Mapping) and len(payload) == 1:
        try:
            text = xmltodict.unparse(payload, pretty=True, indent="  ") + "\n"
            xmltodict.parse(text)  # a key that is not a valid XML name: keep the structure as JSON
        except Exception:
            pass
        else:
            return text, ".xml"
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n", ".json"


@dataclass(frozen=True)
class SanitizedCapture:
    """A rendered, sanitized and scanned response."""

    text: str
    extension: str
    steps: tuple[str, ...]
    findings: tuple[Finding, ...]

    @property
    def clean(self) -> bool:
        return not self.findings


def sanitize_capture(response: Any, placeholders: Placeholders,
                     scrub_text: Callable[[str], str] = redact_secrets,
                     path: str = "<capture>") -> SanitizedCapture:
    """Parse, sanitize, render and scan one response. Raises ``CaptureWithheld`` for an unparseable one."""
    parsed = parse_response(response)
    if not isinstance(parsed, Mapping) or not parsed:
        raise CaptureWithheld("response held no data")
    sanitized, counts = sanitize_payload(parsed, placeholders, scrub_text)
    text, extension = render(sanitized)
    counts["body"] = max(counts["body"], 1)
    steps = tuple(STEP_LABELS[step] + (f" ({counts[step]})" if step != "body" else "")
                  for step in STEP_ORDER if counts[step])
    return SanitizedCapture(text, extension, steps, tuple(scan_capture_text(text, path)))


# --- Capture one account ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CaptureResult:
    """What happened to one alias's endpoint. ``text`` is set only for a clean capture."""

    alias: str
    read: CaptureRead
    status: str  # "captured" (the caller writes it, then "written"), "withheld", "no data" or "error"
    captured_at: datetime
    game_version: Optional[str]
    steps: tuple[str, ...] = ()
    scan: str = "not scanned"
    reason: Optional[str] = None
    text: Optional[str] = None
    extension: Optional[str] = None

    @property
    def relative_path(self) -> Optional[str]:
        return f"{self.alias}/{self.read.stem}{self.extension}" if self.text is not None else None


class SessionExpiredError(RuntimeError):
    """The session expired during a capture; the command never logs in again."""


@contextmanager
def no_relogin(client: Any):
    """Make the client's re-login path (``quickReload``, run on an expired token) raise for the duration."""

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise SessionExpiredError("the session expired during the capture; refusing to log in again")

    own = getattr(client, "__dict__", {})
    had, previous = "quickReload" in own, own.get("quickReload")
    client.quickReload = refuse
    try:
        yield
    finally:
        if had:
            client.quickReload = previous
        else:
            del client.quickReload


def _utc(clock: Callable[[], datetime]) -> datetime:
    moment = clock()
    return moment.astimezone(timezone.utc) if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def capture_account(alias: str, client: Any, *, game_version: Optional[str],
                    scrub_text: Callable[[str], str] = redact_secrets,
                    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                    reads: Sequence[CaptureRead] = CAPTURE_READS) -> list[CaptureResult]:
    """Capture every read in ``reads`` from a logged-in ``client``; one read's failure never stops the others.

    Error reasons go through ``scrub_text``; sanitizing and scanning happen
    here, before the caller writes anything.
    """
    placeholders = Placeholders()
    placeholders.seed(USER, getattr(getattr(client, "user", None), "id", None))
    info = getattr(client, "info", None)
    placeholders.seed(PLAYER, info.get("@Name") if isinstance(info, Mapping) else None)
    results = []
    with no_relogin(client):
        for read in reads:
            results.append(_capture_one(alias, client, read, placeholders, game_version, scrub_text, clock))
    return results


def _capture_one(alias: str, client: Any, read: CaptureRead, placeholders: Placeholders,
                 game_version: Optional[str], scrub_text: Callable[[str], str],
                 clock: Callable[[], datetime]) -> CaptureResult:
    def result(status: str, **fields: Any) -> CaptureResult:
        return CaptureResult(alias, read, status, _utc(clock), game_version, **fields)

    try:
        if read.method is not None:
            setattr(client, read.attribute, None)  # never capture a value left from an earlier call
            getattr(client, read.method)(**read.kwargs)
        response = getattr(client, read.attribute, None)
    except Exception as error:  # one read's failure must not stop the others
        return result("error", reason=f"{type(error).__name__}: {scrub_text(str(error))}")
    if response is None or (isinstance(response, (Mapping, str, bytes)) and not response):
        return result("no data", reason=f"{read.method or 'login'} left no response")
    if read.wrap is not None and isinstance(response, Mapping):
        response = {read.wrap: response}
    try:
        capture = sanitize_capture(response, placeholders, scrub_text, f"{alias}/{read.stem}")
    except CaptureWithheld as error:
        return result("withheld", scan=f"withheld: {error}", reason=str(error))
    except Exception as error:  # an unexpected shape is withheld, never written half-sanitized
        return result("withheld", scan="withheld: sanitizing failed", reason=type(error).__name__)
    if not capture.clean:
        rules = describe_findings(capture.findings)
        return result("withheld", steps=capture.steps, scan=f"withheld: {rules}", reason=f"scan found {rules}")
    return result("captured", steps=capture.steps, scan="clean", text=capture.text, extension=capture.extension)


# --- Output ----------------------------------------------------------------------------------------

def write_capture(output_dir: Path | str, result: CaptureResult) -> Path:
    """Write a clean capture to ``<output_dir>/<alias>/<stem>.<ext>``; never overwrites a file."""
    if result.text is None or result.scan != "clean":
        raise ValueError("only a clean capture is written")
    target = Path(output_dir) / str(result.relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as handle:
        handle.write(result.text)
    return target


def _cell(value: Any) -> str:
    return "n/a" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def table_row(result: CaptureResult) -> str:
    """The capture as a row of the README's "Record of captures" table (``README_COLUMNS``)."""
    steps = "; ".join([*result.steps, f"9: scan {result.scan}", f"by {CAPTURE_SOURCE}"])
    return (f"| {_cell(result.relative_path)} | {_cell(result.read.endpoint)} | "
            f"{result.captured_at.strftime('%Y-%m-%d')} | {_cell(result.game_version)} | {_cell(steps)} | "
            f"{REVIEW_PENDING} |")


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_manifest(results: Iterable[CaptureResult], created_at: datetime,
                   accounts: Optional[Mapping[str, Mapping[str, Any]]] = None) -> dict[str, Any]:
    """The manifest: one entry per alias and endpoint, plus each account's login outcome."""
    entries = []
    for result in results:
        entries.append({
            "file": result.relative_path if result.status == "written" else None,
            "alias": result.alias,
            "endpoint": result.read.endpoint,
            "client_method": result.read.method or "login (held response)",
            "status": result.status,
            "captured_at": _timestamp(result.captured_at),
            "game_version": result.game_version,
            "sanitization_steps": list(result.steps),
            "scan": result.scan,
            "reason": result.reason,
            "note": result.read.note,
            "table_row": table_row(result) if result.status == "written" else None,
        })
    return {
        "format": MANIFEST_FORMAT,
        "format_version": MANIFEST_FORMAT_VERSION,
        "source": CAPTURE_SOURCE,
        "created_at": _timestamp(created_at),
        "readme_columns": list(README_COLUMNS),
        "accounts": {alias: dict(fields) for alias, fields in sorted((accounts or {}).items())},
        "captures": entries,
    }
