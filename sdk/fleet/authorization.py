"""Live execution authorization file (SPEC-05, S05-R2).

An ``Authorization`` is the owner-written, dated and scoped permission an
execute run needs: who wrote it, which registry accounts and catalog actions
it covers, the UTC window it is valid in (at most 14 days), per-account
per-period mineral and gas caps, the run modes it allows and why it exists.
It holds no credential, and starbux is never authorizable.

``load_authorization`` and ``authorization_from_dict`` fail closed with
``AuthorizationError``. ``check_authorization`` returns the reasons a
requested run falls outside the authorization (an empty list means it is
inside). ``authorization_hash`` identifies the exact authorization in a run
record. This module has no execute path.

Optional sections, all still ``version`` 1 (a file without them loads as
before and hashes the same):

- ``legacy_run_skipped`` (SPEC-06, S06-R2): the ``PSS_ACCOUNT_N`` numbers the
  owner confirms are listed in the legacy daily run's ``LEGACY_RUN_SKIP``.
  ``check_authorization`` refuses an account whose registry profile declares
  ``legacy_run_account: N`` unless N is listed here.
- ``battles`` (SPEC-08, S08-R2): ``{"max_per_window", "window_hours"}``,
  required exactly when ``run_pvp_battle`` is in ``actions``.
  ``battle_cap_refusal`` and ``battle_fits_cap`` decide from journal events
  whether one more battle fits the cap in the rolling window ending now.
- ``market`` (SPEC-09, S09-R4): ``{"item_design_ids", "max_value_per_trade",
  "max_value_per_window", "window_hours"}``, required exactly when a market
  action is in ``actions``. The market action ids ``sell_item`` and
  ``purchase_item`` are not in the action catalog yet (``spec09-trade-actions``
  adds them); until then they are accepted in ``actions`` only when the file
  also has a ``market`` section. ``check_trade`` returns why one trade falls
  outside the section. A starbux trade is always refused.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from sdk.actions import CATALOG
from sdk.fleet.accounts import FORBIDDEN_KEYS, AccountRegistry
from sdk.journal import GameplayEvent
from sdk.redaction import redact_secrets
from sdk.state.snapshot import ACCOUNT_ALIAS_PATTERN

AUTHORIZATION_VERSION = 1
MAX_WINDOW = timedelta(days=14)
MAX_WINDOW_HOURS = int(MAX_WINDOW.total_seconds() // 3600)
RUN_MODES = ("canary", "full", "place")
CAP_CURRENCIES = ("minerals", "gas")
MAX_PURPOSE_LENGTH = 500
OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
ACTION_ID_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
MODE_PATTERN = re.compile(r"^(canary|full|place)$")
# Device keys and access tokens are UUID-shaped; an owner label never is.
UUID_PATTERN = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
AUTHORIZATION_FIELDS = frozenset({
    "version", "owner", "purpose", "accounts", "actions", "not_before", "expires", "caps", "modes",
})
OPTIONAL_AUTHORIZATION_FIELDS = frozenset({"legacy_run_skipped", "battles", "market", "placements"})
BATTLE_ACTION_ID = "run_pvp_battle"
# SPEC-09 market actions the catalog does not hold yet; valid only next to a market section.
MARKET_ACTION_IDS = frozenset({"sell_item", "purchase_item"})
BATTLE_FIELDS = ("max_per_window", "window_hours")
PLACE_ACTION_ID = "place_room"
FIRST_FREE = "first-free"
MARKET_FIELDS = ("item_design_ids", "max_value_per_trade", "max_value_per_window", "window_hours")


class AuthorizationError(ValueError):
    """Raised for an invalid authorization file. The text never repeats an offending value."""


def _scan_for_secrets(value: Any, path: str = "") -> None:
    """The registry's scan: secret-named keys anywhere, and any value ``redact_secrets`` would change."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise AuthorizationError(f"secret-named key at {path or '<root>'}")
            _scan_for_secrets(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan_for_secrets(item, f"{path}[{index}]")
    elif isinstance(value, str) and redact_secrets(value) != value:
        raise AuthorizationError(f"secret-shaped value at {path or '<root>'}")


def _parse_utc(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise AuthorizationError(f"{name} must be an ISO 8601 UTC time string")
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise AuthorizationError(f"{name} is not a valid ISO 8601 time") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise AuthorizationError(f"{name} must be timezone-aware UTC (Z or +00:00)")
    return parsed


def _format_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _unique_strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise AuthorizationError(f"{name} must be a non-empty list")
    if not all(isinstance(item, str) for item in value):
        raise AuthorizationError(f"{name} must hold strings only")
    if len(set(value)) != len(value):
        raise AuthorizationError(f"{name} has duplicate entries")
    return tuple(value)


def _positive_int(value: Any, name: str, maximum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or (maximum is not None and value > maximum):
        bound = f" of at most {maximum}" if maximum is not None else ""
        raise AuthorizationError(f"{name} must be a positive integer{bound}")
    return value


def _section(value: Any, name: str, fields: tuple[str, ...]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AuthorizationError(f"{name} must be an object with {', '.join(fields)}")
    if "starbux" in value:
        raise AuthorizationError(f"{name}: starbux is never authorizable")
    unknown = set(value) - set(fields)
    if unknown:
        raise AuthorizationError(f"{name} has unknown fields: {', '.join(sorted(map(str, unknown)))}")
    missing = [item for item in fields if item not in value]
    if missing:
        raise AuthorizationError(f"{name} is missing: {', '.join(missing)}")
    return value


@dataclass(frozen=True)
class BattleLimits:
    """At most ``max_per_window`` battles per account in any rolling ``window_hours`` window (S08-R2)."""

    max_per_window: int
    window_hours: int

    @property
    def window(self) -> timedelta:
        return timedelta(hours=self.window_hours)

    def to_dict(self) -> dict:
        return {"max_per_window": self.max_per_window, "window_hours": self.window_hours}


@dataclass(frozen=True)
class MarketLimits:
    """The items that may be traded and the value caps per trade and per rolling window (S09-R4)."""

    item_design_ids: tuple[int, ...]
    max_value_per_trade: int
    max_value_per_window: int
    window_hours: int

    @property
    def window(self) -> timedelta:
        return timedelta(hours=self.window_hours)

    def to_dict(self) -> dict:
        return {
            "item_design_ids": list(self.item_design_ids),
            "max_value_per_trade": self.max_value_per_trade,
            "max_value_per_window": self.max_value_per_window,
            "window_hours": self.window_hours,
        }


def _parse_battles(value: Any) -> BattleLimits:
    section = _section(value, "battles", BATTLE_FIELDS)
    return BattleLimits(
        max_per_window=_positive_int(section["max_per_window"], "battles.max_per_window"),
        window_hours=_positive_int(section["window_hours"], "battles.window_hours", MAX_WINDOW_HOURS),
    )


def _parse_market(value: Any) -> MarketLimits:
    section = _section(value, "market", MARKET_FIELDS)
    items = section["item_design_ids"]
    if not isinstance(items, list) or not items:
        raise AuthorizationError("market.item_design_ids must be a non-empty list")
    for item in items:
        _positive_int(item, "each market.item_design_ids entry")
    if len(set(items)) != len(items):
        raise AuthorizationError("market.item_design_ids has duplicate entries")
    per_trade = _positive_int(section["max_value_per_trade"], "market.max_value_per_trade")
    per_window = _positive_int(section["max_value_per_window"], "market.max_value_per_window")
    if per_trade > per_window:
        raise AuthorizationError("market.max_value_per_trade may not exceed market.max_value_per_window")
    return MarketLimits(
        item_design_ids=tuple(items),
        max_value_per_trade=per_trade,
        max_value_per_window=per_window,
        window_hours=_positive_int(section["window_hours"], "market.window_hours", MAX_WINDOW_HOURS),
    )


@dataclass(frozen=True)
class Placement:
    """One room placement the owner allows (SPEC-13, S13-R7): a room design at a row and column, or first-free.

    ``row`` and ``column`` are ``None`` for ``first-free``: the first position, in the placement check's
    top-left scan, that a fresh ship read accepts.
    """

    room_design_id: int
    row: Optional[int] = None
    column: Optional[int] = None

    @property
    def first_free(self) -> bool:
        return self.row is None

    def describe(self) -> str:
        where = FIRST_FREE if self.first_free else f"row {self.row}, column {self.column}"
        return f"room design {self.room_design_id} at {where}"

    def to_dict(self) -> dict:
        if self.first_free:
            return {"room_design_id": self.room_design_id, "position": FIRST_FREE}
        return {"room_design_id": self.room_design_id, "row": self.row, "column": self.column}


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AuthorizationError(f"{name} must be a non-negative integer")
    return value


def parse_placement(value: Any, name: str = "placement") -> Placement:
    """A placement entry: ``{room_design_id, position: "first-free"}`` or ``{room_design_id, row, column}``."""
    if not isinstance(value, Mapping):
        raise AuthorizationError(f"{name} must be an object with room_design_id and a position")
    if "starbux" in value:
        raise AuthorizationError(f"{name}: starbux is never authorizable")
    design = _positive_int(value.get("room_design_id"), f"{name}.room_design_id")
    if set(value) == {"room_design_id", "position"}:
        if value["position"] != FIRST_FREE:
            raise AuthorizationError(f"{name}.position may only be {FIRST_FREE!r}; give row and column otherwise")
        return Placement(design)
    if set(value) == {"room_design_id", "row", "column"}:
        return Placement(design, _non_negative_int(value["row"], f"{name}.row"),
                         _non_negative_int(value["column"], f"{name}.column"))
    raise AuthorizationError(f"{name} needs room_design_id with either position {FIRST_FREE!r} or row and column")


def _parse_placements(value: Any, accounts: tuple[str, ...]) -> dict[str, tuple[Placement, ...]]:
    if not isinstance(value, Mapping) or not value:
        raise AuthorizationError("placements must be an object keyed by account alias")
    if set(value) - set(accounts):
        raise AuthorizationError("placements names an account that is not in accounts")
    placements = {}
    for alias in sorted(value):
        entries = value[alias]
        if not isinstance(entries, list) or not entries:
            raise AuthorizationError(f"placements.{alias} must be a non-empty list")
        parsed = tuple(parse_placement(entry, f"placements.{alias} entry") for entry in entries)
        if len(set(parsed)) != len(parsed):
            raise AuthorizationError(f"placements.{alias} has duplicate entries")
        placements[alias] = parsed
    return placements


def _parse_legacy_run_skipped(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise AuthorizationError("legacy_run_skipped must be a list of PSS_ACCOUNT_N numbers")
    for number in value:
        _positive_int(number, "each legacy_run_skipped entry")
    if len(set(value)) != len(value):
        raise AuthorizationError("legacy_run_skipped has duplicate entries")
    return tuple(value)


@dataclass(frozen=True)
class Authorization:
    owner: str
    purpose: str
    accounts: tuple[str, ...]
    actions: tuple[str, ...]
    not_before: datetime
    expires: datetime
    caps: Mapping[str, Mapping[str, int]]
    modes: tuple[str, ...]
    legacy_run_skipped: tuple[int, ...] = ()
    battles: Optional[BattleLimits] = None
    market: Optional[MarketLimits] = None
    placements: Mapping[str, tuple[Placement, ...]] = field(default_factory=dict)

    def cap(self, alias: str, currency: str) -> int:
        """The per-period cap for ``alias``; starbux is always 0, whatever the file says."""
        if currency == "starbux":
            return 0
        try:
            return self.caps[alias][currency]
        except KeyError:
            raise AuthorizationError("no cap for that account and currency") from None

    def to_dict(self) -> dict:
        payload = {
            "version": AUTHORIZATION_VERSION,
            "owner": self.owner,
            "purpose": self.purpose,
            "accounts": list(self.accounts),
            "actions": list(self.actions),
            "not_before": _format_utc(self.not_before),
            "expires": _format_utc(self.expires),
            "caps": {alias: dict(self.caps[alias]) for alias in sorted(self.caps)},
            "modes": list(self.modes),
        }
        # Optional sections appear only when set, so a file without them hashes as before.
        if self.legacy_run_skipped:
            payload["legacy_run_skipped"] = list(self.legacy_run_skipped)
        if self.battles is not None:
            payload["battles"] = self.battles.to_dict()
        if self.market is not None:
            payload["market"] = self.market.to_dict()
        if self.placements:
            payload["placements"] = {alias: [entry.to_dict() for entry in self.placements[alias]]
                                     for alias in sorted(self.placements)}
        return payload


def _parse_caps(value: Any, accounts: tuple[str, ...]) -> dict[str, dict[str, int]]:
    if not isinstance(value, Mapping):
        raise AuthorizationError("caps must be an object keyed by account alias")
    extra = set(value) - set(accounts)
    if extra:
        raise AuthorizationError("caps names an account that is not in accounts")
    caps: dict[str, dict[str, int]] = {}
    for alias in accounts:
        entry = value.get(alias)
        if not isinstance(entry, Mapping):
            raise AuthorizationError(f"caps.{alias} must be an object with minerals and gas")
        if "starbux" in entry:
            raise AuthorizationError(f"caps.{alias}: starbux spending is never authorizable")
        unknown = set(entry) - set(CAP_CURRENCIES)
        if unknown:
            raise AuthorizationError(f"caps.{alias} has unknown fields: {', '.join(sorted(map(str, unknown)))}")
        missing = [currency for currency in CAP_CURRENCIES if currency not in entry]
        if missing:
            raise AuthorizationError(f"caps.{alias} is missing: {', '.join(missing)}")
        for currency in CAP_CURRENCIES:
            limit = entry[currency]
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise AuthorizationError(f"caps.{alias}.{currency} must be a non-negative integer")
        caps[alias] = {currency: entry[currency] for currency in CAP_CURRENCIES}
    return caps


def authorization_from_dict(payload: Mapping[str, Any]) -> Authorization:
    if not isinstance(payload, Mapping):
        raise AuthorizationError("authorization must be a JSON object")
    _scan_for_secrets(payload)
    unknown = set(payload) - AUTHORIZATION_FIELDS - OPTIONAL_AUTHORIZATION_FIELDS
    if unknown:
        raise AuthorizationError(f"unknown authorization fields: {', '.join(sorted(map(str, unknown)))}")
    missing = sorted(AUTHORIZATION_FIELDS - set(payload))
    if missing:
        raise AuthorizationError(f"missing authorization fields: {', '.join(missing)}")
    version = payload["version"]
    if isinstance(version, bool) or version != AUTHORIZATION_VERSION:
        raise AuthorizationError(f"authorization version must be {AUTHORIZATION_VERSION}")

    owner = payload["owner"]
    if not isinstance(owner, str) or not OWNER_PATTERN.match(owner) or UUID_PATTERN.search(owner):
        raise AuthorizationError("owner must be a short label such as 'owner', never an e-mail address")

    purpose = payload["purpose"]
    if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > MAX_PURPOSE_LENGTH:
        raise AuthorizationError(f"purpose must be non-empty text of at most {MAX_PURPOSE_LENGTH} characters")

    accounts = _unique_strings(payload["accounts"], "accounts")
    if not all(ACCOUNT_ALIAS_PATTERN.match(alias) for alias in accounts):
        raise AuthorizationError("accounts must be registry aliases, not e-mails or display names")

    actions = _unique_strings(payload["actions"], "actions")
    has_market = "market" in payload
    if not all(action in CATALOG or (has_market and action in MARKET_ACTION_IDS) for action in actions):
        raise AuthorizationError(
            "actions must be action ids from the action catalog (sell_item and purchase_item need a market section)")
    if (BATTLE_ACTION_ID in actions) != ("battles" in payload):
        raise AuthorizationError("a battles section is required when, and only when, actions include run_pvp_battle")
    if bool(MARKET_ACTION_IDS & set(actions)) != has_market:
        raise AuthorizationError(
            "a market section is required when, and only when, actions include sell_item or purchase_item")
    if (PLACE_ACTION_ID in actions) != ("placements" in payload):
        raise AuthorizationError("a placements section is required when, and only when, actions include place_room")
    battles = _parse_battles(payload["battles"]) if "battles" in payload else None
    market = _parse_market(payload["market"]) if has_market else None
    legacy_run_skipped = (
        _parse_legacy_run_skipped(payload["legacy_run_skipped"]) if "legacy_run_skipped" in payload else ()
    )

    modes = _unique_strings(payload["modes"], "modes")
    if not set(modes) <= set(RUN_MODES):
        raise AuthorizationError(f"modes may only hold: {', '.join(RUN_MODES)}")

    not_before = _parse_utc(payload["not_before"], "not_before")
    expires = _parse_utc(payload["expires"], "expires")
    if expires <= not_before:
        raise AuthorizationError("expires must be later than not_before")
    if expires - not_before > MAX_WINDOW:
        raise AuthorizationError("not_before and expires may be at most 14 days apart")

    caps = _parse_caps(payload["caps"], accounts)
    placements = _parse_placements(payload["placements"], accounts) if "placements" in payload else {}
    return Authorization(
        owner=owner, purpose=purpose, accounts=accounts, actions=actions,
        not_before=not_before, expires=expires, caps=caps, modes=modes,
        legacy_run_skipped=legacy_run_skipped, battles=battles, market=market, placements=placements,
    )


def load_authorization(path: Path | str) -> Authorization:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise AuthorizationError(f"{path}: cannot read authorization file ({error.strerror or 'error'})") from None
    except UnicodeDecodeError:
        raise AuthorizationError(f"{path}: authorization file is not UTF-8") from None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise AuthorizationError(f"{path}: not valid JSON ({error.msg})") from None
    return authorization_from_dict(payload)


def authorization_hash(auth: Authorization) -> str:
    """``sha256:`` plus the hex SHA-256 of the authorization's canonical JSON, for run records."""
    canonical = json.dumps(auth.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _label(value: Any, pattern: re.Pattern) -> str:
    """``value`` quoted when it is a well-formed label, otherwise a placeholder that repeats nothing."""
    return repr(value) if isinstance(value, str) and pattern.match(value) else "<malformed>"


def _is_aware(now: Any) -> bool:
    return isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None


def check_authorization(auth: Authorization, registry: AccountRegistry, now: datetime,
                        aliases: Iterable[str], action_ids: Iterable[str], mode: str, *,
                        battle_events: Optional[Iterable[GameplayEvent]] = None) -> list[str]:
    """Why the requested run falls outside ``auth``; an empty list means it is authorized.

    Refused: a naive ``now`` or one outside ``[not_before, expires)``; a mode
    the file does not allow; no requested account; an account the file does
    not name, that the registry does not hold, or that the registry disables;
    an account whose profile declares ``legacy_run_account`` N when N is not
    in ``legacy_run_skipped`` (S06-R2); and an action the file does not allow.

    With ``battle_events`` (journal events) and ``run_pvp_battle`` requested
    and authorized, an account whose battle cap is already full is refused
    too (S08-R2).
    """
    reasons: list[str] = []
    if not _is_aware(now):
        reasons.append("current time is not timezone-aware; refusing")
    elif now < auth.not_before:
        reasons.append("authorization is not valid yet (before not_before)")
    elif now >= auth.expires:
        reasons.append("authorization has expired")
    if mode not in auth.modes:
        reasons.append(f"mode {_label(mode, MODE_PATTERN)} is not allowed by the authorization")
    requested = list(aliases)
    if not requested:
        reasons.append("no account requested")
    for alias in requested:
        name = _label(alias, ACCOUNT_ALIAS_PATTERN)
        if alias not in auth.accounts:
            reasons.append(f"account {name} is not authorized")
        elif alias not in registry:
            reasons.append(f"account {name} is not in the registry")
        elif not registry.get(alias).enabled:
            reasons.append(f"account {name} is disabled in the registry")
        else:
            legacy = registry.get(alias).legacy_run_account
            if legacy is not None and legacy not in auth.legacy_run_skipped:
                reasons.append(
                    f"account {name} is legacy daily run account {legacy}, and the authorization does not "
                    f"confirm it is skipped there (legacy_run_skipped)")
    requested_actions = list(action_ids)
    for action_id in requested_actions:
        if action_id not in auth.actions:
            reasons.append(f"action {_label(action_id, ACTION_ID_PATTERN)} is not allowed by the authorization")
    if (battle_events is not None and _is_aware(now)
            and BATTLE_ACTION_ID in requested_actions and BATTLE_ACTION_ID in auth.actions):
        events = list(battle_events)
        for alias in requested:
            if alias in auth.accounts:
                refusal = battle_cap_refusal(auth, alias, events, now)
                if refusal is not None:
                    reasons.append(refusal)
    return reasons


def placement_refusal_reason(auth: Authorization, alias: str, placement: Placement) -> Optional[str]:
    """Why ``placement`` on ``alias`` is outside ``auth`` (S13-R7), or ``None``. The design and position must
    match an entry exactly: an entry for a row and column does not allow first-free, nor the reverse."""
    if PLACE_ACTION_ID not in auth.actions:
        return "action 'place_room' is not allowed by the authorization"
    if placement not in auth.placements.get(alias, ()):
        return (f"{placement.describe()} on account {_label(alias, ACCOUNT_ALIAS_PATTERN)} is not named in the "
                "authorization's placements")
    return None


def _moment(value: Any) -> Optional[datetime]:
    """A timestamp (datetime or ISO 8601 text) as an aware datetime, or ``None`` when it cannot be read as one."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value)
        except ValueError:
            return None
    return value if _is_aware(value) else None


def battles_in_window(auth: Authorization, alias: str, events: Iterable[GameplayEvent], now: datetime) -> int:
    """Battles ``alias`` attempted in the rolling battle window ending at ``now``.

    Every attempted, non-dry-run ``run_pvp_battle`` event of ``alias`` counts,
    whatever its outcome: an uncertain battle still used a slot. An event whose
    start time cannot be read, or lies after ``now``, counts too (fail closed).
    """
    if auth.battles is None:
        raise AuthorizationError("the authorization has no battles section")
    since = now - auth.battles.window
    count = 0
    for event in events:
        if (getattr(event, "action_id", None) != BATTLE_ACTION_ID or getattr(event, "account_alias", None) != alias
                or not getattr(event, "attempted", False) or getattr(event, "dry_run", False)):
            continue
        started = _moment(getattr(event, "started_at", None))
        if started is None or started > since:
            count += 1
    return count


def battle_cap_refusal(auth: Authorization, alias: str, events: Iterable[GameplayEvent],
                       now: datetime) -> Optional[str]:
    """Why ``alias`` may not start one more battle at ``now``, or ``None`` when it fits the cap (S08-R2)."""
    name = _label(alias, ACCOUNT_ALIAS_PATTERN)
    if not _is_aware(now):
        return "current time is not timezone-aware; refusing"
    if BATTLE_ACTION_ID not in auth.actions or auth.battles is None:
        return "battles are not allowed by the authorization"
    if alias not in auth.accounts:
        return f"account {name} is not authorized"
    used = battles_in_window(auth, alias, events, now)
    if used >= auth.battles.max_per_window:
        return (f"account {name} has used {used} of {auth.battles.max_per_window} battles allowed in "
                f"{auth.battles.window_hours} hours")
    return None


def battle_fits_cap(auth: Authorization, alias: str, events: Iterable[GameplayEvent], now: datetime) -> bool:
    """Whether ``alias`` may start one more battle at ``now`` under the authorization's battle cap."""
    return battle_cap_refusal(auth, alias, events, now) is None


@dataclass(frozen=True)
class TradeRecord:
    """An earlier trade attempt, for the market window: when, which item, and its value (``None`` = unobserved)."""

    at: datetime
    item_design_id: int
    value: Optional[int]


def check_trade(auth: Authorization, item_design_id: Any, value: Any, prior_trades: Iterable[TradeRecord], *,
                now: Optional[datetime] = None, currency: Optional[str] = None) -> list[str]:
    """Why one trade falls outside the authorization's market section; an empty list means it is inside (S09-R4).

    Call it before any client call. Refused: no market section; a starbux
    ``currency``; an item not in ``item_design_ids``; a value that is unknown,
    not a positive integer, or above ``max_value_per_trade``; and prior
    trades plus this one above ``max_value_per_window``. With ``now``, only
    prior trades in the rolling window ending at ``now`` count and a ``now``
    outside the authorization window is refused; without it every prior trade
    counts. A prior trade with an unobserved value is refused rather than
    counted as 0, and one with an unreadable time counts as inside the window.
    """
    market = auth.market
    if market is None or not MARKET_ACTION_IDS & set(auth.actions):
        return ["market trades are not allowed by the authorization"]
    reasons: list[str] = []
    if currency == "starbux":
        reasons.append("starbux trades are never authorizable")
    since: Optional[datetime] = None
    if now is not None:
        if not _is_aware(now):
            reasons.append("current time is not timezone-aware; refusing")
        elif now < auth.not_before:
            reasons.append("authorization is not valid yet (before not_before)")
        elif now >= auth.expires:
            reasons.append("authorization has expired")
        else:
            since = now - market.window
    if isinstance(item_design_id, bool) or not isinstance(item_design_id, int):
        reasons.append("item design id is malformed")
    elif item_design_id not in market.item_design_ids:
        reasons.append(f"item {item_design_id} is not in the authorization's item list")
    if value is None:
        reasons.append("trade value is unknown; refusing")
    elif isinstance(value, bool) or not isinstance(value, int) or value < 1:
        reasons.append("trade value must be a positive integer")
    elif value > market.max_value_per_trade:
        reasons.append(f"trade value {value} exceeds max_value_per_trade {market.max_value_per_trade}")
    else:
        total = value
        for trade in prior_trades:
            at = _moment(getattr(trade, "at", None))
            if since is not None and at is not None and at <= since:
                continue
            prior = getattr(trade, "value", None)
            if isinstance(prior, bool) or not isinstance(prior, int) or prior < 0:
                reasons.append("a prior trade in the window has an unknown value; refusing")
                break
            total += prior
        else:
            if total > market.max_value_per_window:
                reasons.append(f"trades in the last {market.window_hours} hours would total {total}, above "
                               f"max_value_per_window {market.max_value_per_window}")
    return reasons
