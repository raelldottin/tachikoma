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
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from sdk.actions import CATALOG
from sdk.fleet.accounts import FORBIDDEN_KEYS, AccountRegistry
from sdk.redaction import redact_secrets
from sdk.state.snapshot import ACCOUNT_ALIAS_PATTERN

AUTHORIZATION_VERSION = 1
MAX_WINDOW = timedelta(days=14)
RUN_MODES = ("canary", "full")
CAP_CURRENCIES = ("minerals", "gas")
MAX_PURPOSE_LENGTH = 500
OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
ACTION_ID_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
MODE_PATTERN = re.compile(r"^(canary|full)$")
# Device keys and access tokens are UUID-shaped; an owner label never is.
UUID_PATTERN = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
AUTHORIZATION_FIELDS = frozenset({
    "version", "owner", "purpose", "accounts", "actions", "not_before", "expires", "caps", "modes",
})


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

    def cap(self, alias: str, currency: str) -> int:
        """The per-period cap for ``alias``; starbux is always 0, whatever the file says."""
        if currency == "starbux":
            return 0
        try:
            return self.caps[alias][currency]
        except KeyError:
            raise AuthorizationError("no cap for that account and currency") from None

    def to_dict(self) -> dict:
        return {
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
    unknown = set(payload) - AUTHORIZATION_FIELDS
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
    if not all(action in CATALOG for action in actions):
        raise AuthorizationError("actions must be action ids from the action catalog")

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
    return Authorization(
        owner=owner, purpose=purpose, accounts=accounts, actions=actions,
        not_before=not_before, expires=expires, caps=caps, modes=modes,
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


def check_authorization(auth: Authorization, registry: AccountRegistry, now: datetime,
                        aliases: Iterable[str], action_ids: Iterable[str], mode: str) -> list[str]:
    """Why the requested run falls outside ``auth``; an empty list means it is authorized.

    Refused: a naive ``now`` or one outside ``[not_before, expires)``; a mode
    the file does not allow; no requested account; an account the file does
    not name, that the registry does not hold, or that the registry disables;
    and an action the file does not allow.
    """
    reasons: list[str] = []
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
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
    for action_id in action_ids:
        if action_id not in auth.actions:
            reasons.append(f"action {_label(action_id, ACTION_ID_PATTERN)} is not allowed by the authorization")
    return reasons
