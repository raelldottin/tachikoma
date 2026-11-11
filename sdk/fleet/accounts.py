"""Account registry for multi-account orchestration (R5, AUTO-009).

An ``AccountProfile`` describes one participating ship: what it is trying to
achieve, which progression policy and PvP strategy it uses, how much it may
spend per period, and the *name* of the secret that holds its credentials.
The registry never holds a credential value.

``load_registry`` reads a JSON file and fails closed: unknown fields,
duplicate aliases, e-mail addresses, secret-shaped values and secret-named
keys anywhere in the file are refused with ``RegistryError``.

A profile may declare ``expected_game_identity``, the ``gid1-`` fingerprint of
the game user id its credentials must log in as. Alias checks alone cannot
catch a factory that loads the wrong credentials, because the alias is a label
the caller stamps on every snapshot. ``game_identity_refusal`` compares the
observed fingerprint with the declared one, and ``pin_game_identity`` keeps
every later snapshot of the run on the same game account.

A profile may also declare ``legacy_run_account``, the number N of the
``PSS_ACCOUNT_N`` secret the legacy daily run (``daily-run.yml``) uses for the
same ship (SPEC-06). An execute run refuses such an account unless the
authorization confirms N is skipped by the legacy run (S06-R2).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from sdk.policy import POLICIES
from sdk.pvp.models import FORBIDDEN_SECRET_KEYS
from sdk.redaction import redact_secrets
from sdk.state.snapshot import (
    ACCOUNT_ALIAS_PATTERN,
    GAME_IDENTITY_PATTERN,
    AccountMismatchError,
    AccountSnapshot,
)

REGISTRY_VERSION = 1
SECRET_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
STRATEGY_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}@v[1-9][0-9]*$")
BUDGET_CURRENCIES = ("minerals", "gas", "starbux")
PROFILE_FIELDS = frozenset({
    "alias", "objective", "policy_id", "pvp_strategy", "budgets", "max_actions", "enabled", "credentials_secret",
    "expected_game_identity", "legacy_run_account",
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
    expected_game_identity: Optional[str] = None
    legacy_run_account: Optional[int] = None

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
        if "starbux" not in self.budgets:
            # Premium currency is never unlimited by omission.
            object.__setattr__(self, "budgets", {**self.budgets, "starbux": 0})
        for currency, limit in self.budgets.items():
            if currency not in BUDGET_CURRENCIES:
                raise RegistryError(f"{self.alias}: unknown budget currency {currency!r}")
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise RegistryError(f"{self.alias}: budget for {currency} must be a non-negative integer")
        if isinstance(self.max_actions, bool) or not isinstance(self.max_actions, int) or self.max_actions < 1:
            raise RegistryError(f"{self.alias}: max_actions must be a positive integer")
        if not isinstance(self.enabled, bool):
            raise RegistryError(f"{self.alias}: enabled must be true or false")
        if self.expected_game_identity is not None and (
            not isinstance(self.expected_game_identity, str)
            or not GAME_IDENTITY_PATTERN.match(self.expected_game_identity)
        ):
            raise RegistryError(
                f"{self.alias}: expected_game_identity must be a gid1- fingerprint, never a raw user id"
            )
        if self.legacy_run_account is not None and (
            isinstance(self.legacy_run_account, bool)
            or not isinstance(self.legacy_run_account, int)
            or self.legacy_run_account < 1
        ):
            raise RegistryError(f"{self.alias}: legacy_run_account must be a positive integer (the N of PSS_ACCOUNT_N)")
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
            "expected_game_identity": self.expected_game_identity,
            "legacy_run_account": self.legacy_run_account,
        }


def game_identity_refusal(alias: str, expected: Optional[str], observed: Optional[str],
                          seen: Mapping[str, str], declared: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Why ``alias`` must not run with the ``observed`` game identity, or ``None``.

    ``expected`` is the alias's own ``expected_game_identity``. ``declared``
    maps every fingerprint the registry declares to the alias declaring it,
    and ``seen`` maps fingerprints already accepted in this fleet run to their
    alias. Refused: a malformed fingerprint; an expected identity that is
    unknown or different; an identity the registry declares for another
    account (even when this account declares none); and an identity another
    account already used in this run.
    """
    if observed is not None and (not isinstance(observed, str) or not GAME_IDENTITY_PATTERN.match(observed)):
        return "observed game identity is malformed; refusing before any call"
    if expected is not None and observed is None:
        return "game identity is unknown but the registry declares one; refusing before any call"
    if expected is not None and observed != expected:
        return "logged-in game account does not match expected_game_identity; refusing before any call"
    if observed is None:
        return None
    owner = (declared or {}).get(observed)
    if owner is not None and owner != alias:
        return f"logged-in game account is declared for {owner!r} in the registry; refusing before any call"
    owner = seen.get(observed)
    if owner is not None and owner != alias:
        return f"game identity already observed for {owner!r} in this fleet run; refusing before any call"
    return None


def pin_game_identity(provider: Callable[[], AccountSnapshot],
                      expected: Optional[str]) -> Callable[[], AccountSnapshot]:
    """Wrap ``provider`` so a snapshot of any other game identity raises ``AccountMismatchError``.

    ``expected`` ``None`` (identity unknown at the first snapshot) is pinned
    too: a later snapshot with a known identity raises, because an account
    that becomes known only after its claims were decided was never checked
    against the registry or the other accounts.
    """

    def pinned() -> AccountSnapshot:
        snapshot = provider()
        if getattr(snapshot, "game_identity", None) != expected:
            raise AccountMismatchError("snapshot game identity changed during the run")
        return snapshot

    return pinned


class AccountRegistry:
    def __init__(self, profiles: Iterable[AccountProfile]) -> None:
        self._profiles: dict[str, AccountProfile] = {}
        for profile in profiles:
            if profile.alias in self._profiles:
                raise RegistryError(f"duplicate alias {profile.alias!r}")
            self._profiles[profile.alias] = profile
        declared: dict[str, str] = {}
        for profile in self._profiles.values():
            identity = profile.expected_game_identity
            if identity is not None and declared.setdefault(identity, profile.alias) != profile.alias:
                raise RegistryError(
                    f"{profile.alias}: expected_game_identity is already declared by {declared[identity]!r}")
        self._declared_identities = declared
        legacy: dict[int, str] = {}
        for profile in self._profiles.values():
            number = profile.legacy_run_account
            if number is not None and legacy.setdefault(number, profile.alias) != profile.alias:
                raise RegistryError(
                    f"{profile.alias}: legacy_run_account {number} is already declared by {legacy[number]!r}")

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

    def declared_identities(self) -> dict[str, str]:
        """Each declared ``expected_game_identity``, enabled or not, mapped to its alias."""
        return dict(self._declared_identities)

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
