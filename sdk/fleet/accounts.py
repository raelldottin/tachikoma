"""Account registry for multi-account orchestration (R5, AUTO-009).

An ``AccountProfile`` describes one participating ship: what it is trying to
achieve, which progression policy and PvP strategy it uses, how much it may
spend per period, and the *name* of the secret that holds its credentials.
The registry never holds a credential value.

``load_registry`` reads a JSON file and fails closed: unknown fields,
duplicate aliases, e-mail addresses, secret-shaped values and secret-named
keys anywhere in the file are refused with ``RegistryError``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from sdk.policy import POLICIES
from sdk.pvp.models import FORBIDDEN_SECRET_KEYS
from sdk.redaction import redact_secrets
from sdk.state.snapshot import ACCOUNT_ALIAS_PATTERN

REGISTRY_VERSION = 1
SECRET_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
STRATEGY_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}@v[1-9][0-9]*$")
BUDGET_CURRENCIES = ("minerals", "gas", "starbux")
PROFILE_FIELDS = frozenset({
    "alias", "objective", "policy_id", "pvp_strategy", "budgets", "max_actions", "enabled", "credentials_secret",
})
FORBIDDEN_KEYS = frozenset(FORBIDDEN_SECRET_KEYS | {"email", "e-mail", "authstring", "auth_string"})


class RegistryError(ValueError):
    """Raised for an invalid registry or profile."""


def _scan_for_secrets(value: Any, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise RegistryError(f"secret-named key at {path or '<root>'}: {key}")
            _scan_for_secrets(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan_for_secrets(item, f"{path}[{index}]")
    elif isinstance(value, str) and redact_secrets(value) != value:
        raise RegistryError(f"secret-shaped value at {path or '<root>'}")


@dataclass(frozen=True)
class AccountProfile:
    alias: str
    objective: str
    policy_id: str
    credentials_secret: str
    pvp_strategy: Optional[str] = None
    budgets: Mapping[str, int] = field(default_factory=lambda: {"starbux": 0})
    max_actions: int = 20
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.alias, str) or not ACCOUNT_ALIAS_PATTERN.match(self.alias):
            raise RegistryError("alias must be a lowercase alias, not an e-mail or display name")
        if not isinstance(self.objective, str) or not self.objective.strip() or len(self.objective) > 200:
            raise RegistryError(f"{self.alias}: objective must be a short non-empty label")
        if self.policy_id not in POLICIES:
            raise RegistryError(f"{self.alias}: unknown policy {self.policy_id!r}")
        if not isinstance(self.credentials_secret, str) or not SECRET_NAME_PATTERN.match(self.credentials_secret):
            raise RegistryError(f"{self.alias}: credentials_secret must be a secret name like ALPHA_AUTH, not a value")
        if self.pvp_strategy is not None and (
            not isinstance(self.pvp_strategy, str) or not STRATEGY_KEY_PATTERN.match(self.pvp_strategy)
        ):
            raise RegistryError(f"{self.alias}: pvp_strategy must look like strategy-id@v1")
        if not isinstance(self.budgets, Mapping):
            raise RegistryError(f"{self.alias}: budgets must be a mapping")
        for currency, limit in self.budgets.items():
            if currency not in BUDGET_CURRENCIES:
                raise RegistryError(f"{self.alias}: unknown budget currency {currency!r}")
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise RegistryError(f"{self.alias}: budget for {currency} must be a non-negative integer")
        if isinstance(self.max_actions, bool) or not isinstance(self.max_actions, int) or self.max_actions < 1:
            raise RegistryError(f"{self.alias}: max_actions must be a positive integer")
        if not isinstance(self.enabled, bool):
            raise RegistryError(f"{self.alias}: enabled must be true or false")
        _scan_for_secrets({"objective": self.objective}, self.alias)

    def budget(self, currency: str) -> Optional[int]:
        """Spend limit per period, or ``None`` when the profile sets none for this currency."""
        return self.budgets.get(currency)

    def to_dict(self) -> dict:
        return {
            "alias": self.alias,
            "objective": self.objective,
            "policy_id": self.policy_id,
            "pvp_strategy": self.pvp_strategy,
            "budgets": dict(self.budgets),
            "max_actions": self.max_actions,
            "enabled": self.enabled,
            "credentials_secret": self.credentials_secret,
        }


class AccountRegistry:
    def __init__(self, profiles: Iterable[AccountProfile]) -> None:
        self._profiles: dict[str, AccountProfile] = {}
        for profile in profiles:
            if profile.alias in self._profiles:
                raise RegistryError(f"duplicate alias {profile.alias!r}")
            self._profiles[profile.alias] = profile

    def __contains__(self, alias: str) -> bool:
        return alias in self._profiles

    def __len__(self) -> int:
        return len(self._profiles)

    def get(self, alias: str) -> AccountProfile:
        try:
            return self._profiles[alias]
        except KeyError:
            raise RegistryError(f"unknown account {alias!r}") from None

    def aliases(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    def enabled(self) -> tuple[AccountProfile, ...]:
        return tuple(self._profiles[alias] for alias in self.aliases() if self._profiles[alias].enabled)


def registry_from_dict(payload: Mapping[str, Any]) -> AccountRegistry:
    if not isinstance(payload, Mapping):
        raise RegistryError("registry must be a JSON object")
    _scan_for_secrets(payload)
    if payload.get("version") != REGISTRY_VERSION:
        raise RegistryError(f"registry version must be {REGISTRY_VERSION}")
    unknown_top = set(payload) - {"version", "accounts"}
    if unknown_top:
        raise RegistryError(f"unknown registry fields: {', '.join(sorted(unknown_top))}")
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise RegistryError("accounts must be a list")
    profiles = []
    for index, entry in enumerate(accounts):
        if not isinstance(entry, Mapping):
            raise RegistryError(f"accounts[{index}] must be an object")
        unknown = set(entry) - PROFILE_FIELDS
        if unknown:
            raise RegistryError(f"accounts[{index}] has unknown fields: {', '.join(sorted(unknown))}")
        try:
            profiles.append(AccountProfile(**entry))
        except TypeError as error:
            raise RegistryError(f"accounts[{index}]: {error}") from None
    return AccountRegistry(profiles)


def load_registry(path: Path | str) -> AccountRegistry:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RegistryError(f"{path}: not valid JSON ({error.msg})") from None
    return registry_from_dict(payload)
