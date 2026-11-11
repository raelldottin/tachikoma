"""Versioned PvP strategies and experiment windows (R3, AUTO-006).

A strategy is data: what the ship's native AI, crew roles, power and targeting
should favour. Its ``config_version`` is a hash of that data, so two runs can
only be compared as "the same strategy" when the hash matches.

An ``ExperimentSchedule`` assigns at most one strategy to an account alias for
each UTC window. Windows are half-open (``start <= t < end``) and may not
overlap for the same alias, so every battle maps to exactly one strategy or
to none.

An ``AdoptionLog`` records when each strategy version was put on an account's
ship and how: by a bot call identified from captures, or by the owner by hand
(SPEC-08 R1). A ``TrialRecord`` joins the schedule and the log: a battle has a
strategy only when a recorded adoption of the window's strategy, made inside
that window, precedes it. By-hand adoptions mark the attribution as
approximate in time (SPEC-08 R7).

Pure and offline: nothing here talks to the game. The log is read from and
written to a local JSON file only.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

from sdk.redaction import redact_secrets
from sdk.state.snapshot import ACCOUNT_ALIAS_PATTERN

from .models import BattleStrategy, EvidenceTier, NativeAIRule, PvpInputError, reject_secret_keys

STRATEGY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
CONFIG_VERSION_PATTERN = re.compile(r"^[0-9a-f]{16}$")
ADOPTION_LOG_SCHEMA_VERSION = 1


def _require_utc(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise PvpInputError(f"{name} must be a timezone-aware UTC datetime")


def _unique(values: Tuple[str, ...], name: str) -> None:
    if len(set(values)) != len(values):
        raise PvpInputError(f"{name} must not repeat entries")


@dataclass(frozen=True)
class PvpStrategy:
    strategy_id: str
    version: int
    description: str
    battle_strategy: str
    native_ai_rules: Tuple[NativeAIRule, ...] = field(default_factory=tuple)
    crew_roles: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)
    power_priorities: Tuple[str, ...] = field(default_factory=tuple)
    target_priorities: Tuple[str, ...] = field(default_factory=tuple)
    evidence_tier: str = EvidenceTier.UNKNOWN.value

    def __post_init__(self) -> None:
        if not isinstance(self.strategy_id, str) or not STRATEGY_ID_PATTERN.match(self.strategy_id):
            raise PvpInputError("strategy.strategy_id must be a lowercase slug")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise PvpInputError("strategy.version must be an integer >= 1")
        if self.battle_strategy not in {s.value for s in BattleStrategy}:
            raise PvpInputError("strategy.battle_strategy invalid")
        if self.evidence_tier not in {e.value for e in EvidenceTier}:
            raise PvpInputError("strategy.evidence_tier invalid")
        for rule in self.native_ai_rules:
            if not isinstance(rule, NativeAIRule):
                raise PvpInputError("strategy.native_ai_rules must hold NativeAIRule values")
        for pair in self.crew_roles:
            if not (isinstance(pair, tuple) and len(pair) == 2 and all(isinstance(p, str) and p for p in pair)):
                raise PvpInputError("strategy.crew_roles entries must be (role, room_category) pairs")
        _unique(tuple(role for role, _ in self.crew_roles), "strategy.crew_roles roles")
        _unique(self.power_priorities, "strategy.power_priorities")
        _unique(self.target_priorities, "strategy.target_priorities")
        if not isinstance(self.description, str) or redact_secrets(self.description) != self.description:
            raise PvpInputError("strategy.description must be plain text without secret material")

    @property
    def key(self) -> str:
        return f"{self.strategy_id}@v{self.version}"

    def _content(self) -> Dict[str, object]:
        return {
            "battle_strategy": self.battle_strategy,
            "ai": [
                {"group": r.group, "conditions": list(r.conditions), "actions": list(r.actions), "target": r.target_type}
                for r in self.native_ai_rules
            ],
            "crew_roles": [list(pair) for pair in self.crew_roles],
            "power": list(self.power_priorities),
            "targets": list(self.target_priorities),
        }

    @property
    def config_version(self) -> str:
        """Hash of the behaviour-defining fields; description and evidence tier are excluded."""
        canonical = json.dumps(self._content(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> Dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "version": self.version,
            "key": self.key,
            "config_version": self.config_version,
            "description": self.description,
            "evidence_tier": self.evidence_tier,
            **self._content(),
        }


class StrategyRegistry:
    """Strategies by ``(strategy_id, version)``. A published version never changes."""

    def __init__(self, strategies: Iterable[PvpStrategy] = ()) -> None:
        self._strategies: Dict[Tuple[str, int], PvpStrategy] = {}
        for strategy in strategies:
            self.register(strategy)

    def register(self, strategy: PvpStrategy) -> None:
        key = (strategy.strategy_id, strategy.version)
        existing = self._strategies.get(key)
        if existing is not None and existing.config_version != strategy.config_version:
            raise PvpInputError(f"{strategy.key} is already registered with different content; bump the version")
        self._strategies[key] = strategy

    def get(self, strategy_id: str, version: int) -> Optional[PvpStrategy]:
        return self._strategies.get((strategy_id, version))

    def latest(self, strategy_id: str) -> Optional[PvpStrategy]:
        versions = [s for (sid, _), s in self._strategies.items() if sid == strategy_id]
        return max(versions, key=lambda s: s.version) if versions else None

    def all(self) -> Tuple[PvpStrategy, ...]:
        return tuple(self._strategies[key] for key in sorted(self._strategies))


@dataclass(frozen=True)
class ExperimentWindow:
    account_alias: str
    strategy_id: str
    strategy_version: int
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.account_alias, str) or not ACCOUNT_ALIAS_PATTERN.match(self.account_alias):
            raise PvpInputError("window.account_alias must be a lowercase alias, not an e-mail or display name")
        if not isinstance(self.strategy_id, str) or not STRATEGY_ID_PATTERN.match(self.strategy_id):
            raise PvpInputError("window.strategy_id must be a lowercase slug")
        _require_utc(self.start, "window.start")
        _require_utc(self.end, "window.end")
        if self.end <= self.start:
            raise PvpInputError("window.end must be after window.start")

    def contains(self, at: datetime) -> bool:
        return self.start <= at < self.end

    def overlaps(self, other: "ExperimentWindow") -> bool:
        return self.account_alias == other.account_alias and self.start < other.end and other.start < self.end


class ExperimentSchedule:
    """Non-overlapping strategy windows per account alias."""

    def __init__(self, registry: StrategyRegistry, windows: Iterable[ExperimentWindow] = ()) -> None:
        self.registry = registry
        self._windows: List[ExperimentWindow] = []
        for window in windows:
            self.add(window)

    def add(self, window: ExperimentWindow) -> None:
        if self.registry.get(window.strategy_id, window.strategy_version) is None:
            raise PvpInputError(f"{window.strategy_id}@v{window.strategy_version} is not registered")
        for existing in self._windows:
            if existing.overlaps(window):
                raise PvpInputError(f"window for {window.account_alias} overlaps an existing window")
        self._windows.append(window)
        self._windows.sort(key=lambda w: (w.account_alias, w.start))

    def windows_for(self, account_alias: str) -> Tuple[ExperimentWindow, ...]:
        return tuple(w for w in self._windows if w.account_alias == account_alias)

    def window_at(self, account_alias: str, at: datetime) -> Optional[ExperimentWindow]:
        _require_utc(at, "at")
        for window in self.windows_for(account_alias):
            if window.contains(at):
                return window
        return None

    def active_strategy(self, account_alias: str, at: datetime) -> Optional[PvpStrategy]:
        window = self.window_at(account_alias, at)
        return self.registry.get(window.strategy_id, window.strategy_version) if window else None


class AdoptionMethod(str, Enum):
    """How a strategy version reached the ship (SPEC-08 R1, R7)."""

    BOT_CALL = "bot-call"  # a game call identified from captures
    BY_HAND = "by-hand"  # the owner set it in the game and noted the time


def _parse_utc(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise PvpInputError(f"{name} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PvpInputError(f"{name} must be an ISO-8601 UTC timestamp") from exc
    _require_utc(parsed, name)
    return parsed


def _format_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class StrategyAdoption:
    """One recorded adoption of a strategy version on one account's ship."""

    account_alias: str
    strategy_id: str
    strategy_version: int
    config_version: str
    adopted_at: datetime
    method: str
    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.account_alias, str) or not ACCOUNT_ALIAS_PATTERN.match(self.account_alias):
            raise PvpInputError("adoption.account_alias must be a lowercase alias, not an e-mail or display name")
        if not isinstance(self.strategy_id, str) or not STRATEGY_ID_PATTERN.match(self.strategy_id):
            raise PvpInputError("adoption.strategy_id must be a lowercase slug")
        version = self.strategy_version
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise PvpInputError("adoption.strategy_version must be an integer >= 1")
        if not isinstance(self.config_version, str) or not CONFIG_VERSION_PATTERN.match(self.config_version):
            raise PvpInputError("adoption.config_version must be 16 lowercase hex characters")
        _require_utc(self.adopted_at, "adoption.adopted_at")
        if self.method not in {m.value for m in AdoptionMethod}:
            raise PvpInputError("adoption.method must be 'bot-call' or 'by-hand'")
        if not isinstance(self.note, str) or redact_secrets(self.note) != self.note:
            raise PvpInputError("adoption.note must be plain text without secret material")

    @property
    def key(self) -> str:
        return f"{self.strategy_id}@v{self.strategy_version}"

    @property
    def approximate_timing(self) -> bool:
        """A by-hand adoption time is the owner's note, not an observed call (SPEC-08 R7)."""
        return self.method == AdoptionMethod.BY_HAND.value

    def to_dict(self) -> Dict[str, object]:
        return {
            "account_alias": self.account_alias,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "config_version": self.config_version,
            "adopted_at": _format_utc(self.adopted_at),
            "method": self.method,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StrategyAdoption":
        if not isinstance(payload, Mapping):
            raise PvpInputError("adoption entries must be JSON objects")
        reject_secret_keys(payload)
        required = ("account_alias", "strategy_id", "strategy_version", "config_version", "adopted_at", "method")
        missing = [name for name in required if name not in payload]
        if missing:
            raise PvpInputError(f"adoption entry is missing {', '.join(missing)}")
        unknown = sorted(set(payload) - set(required) - {"note"})
        if unknown:
            raise PvpInputError(f"adoption entry has unknown fields {', '.join(unknown)}")
        return cls(
            account_alias=payload["account_alias"],
            strategy_id=payload["strategy_id"],
            strategy_version=payload["strategy_version"],
            config_version=payload["config_version"],
            adopted_at=_parse_utc(payload["adopted_at"], "adoption.adopted_at"),
            method=payload["method"],
            note=payload.get("note", ""),
        )


class AdoptionLog:
    """Strategy adoptions per account alias, strictly in time order (SPEC-08 R1).

    Entries for one alias must arrive in increasing ``adopted_at`` order; an
    entry earlier than the alias's last one is refused as out of order, and one
    at the same instant is refused as overlapping. Aliases are independent.
    """

    def __init__(self, adoptions: Iterable[StrategyAdoption] = ()) -> None:
        self._by_alias: Dict[str, List[StrategyAdoption]] = {}
        for adoption in adoptions:
            self.add(adoption)

    def add(self, adoption: StrategyAdoption) -> None:
        if not isinstance(adoption, StrategyAdoption):
            raise PvpInputError("adoption log entries must be StrategyAdoption values")
        entries = self._by_alias.get(adoption.account_alias, [])
        if entries:
            last = entries[-1].adopted_at
            if adoption.adopted_at < last:
                raise PvpInputError(f"adoption for {adoption.account_alias} is out of order")
            if adoption.adopted_at == last:
                raise PvpInputError(f"adoption for {adoption.account_alias} overlaps an existing adoption")
        self._by_alias[adoption.account_alias] = entries + [adoption]

    def aliases(self) -> Tuple[str, ...]:
        return tuple(sorted(self._by_alias))

    def adoptions_for(self, account_alias: str) -> Tuple[StrategyAdoption, ...]:
        return tuple(self._by_alias.get(account_alias, ()))

    def all(self) -> Tuple[StrategyAdoption, ...]:
        return tuple(a for alias in self.aliases() for a in self._by_alias[alias])

    def latest_at(self, account_alias: str, at: datetime) -> Optional[StrategyAdoption]:
        """The last adoption for ``account_alias`` at or before ``at``, or ``None``."""
        _require_utc(at, "at")
        found = None
        for adoption in self._by_alias.get(account_alias, ()):
            if adoption.adopted_at > at:
                break
            found = adoption
        return found

    def adoptions_in(self, window: ExperimentWindow) -> Tuple[StrategyAdoption, ...]:
        """Adoptions for the window's alias whose time falls inside the window."""
        return tuple(a for a in self.adoptions_for(window.account_alias) if window.contains(a.adopted_at))

    def to_dict(self) -> Dict[str, object]:
        return {"schema_version": ADOPTION_LOG_SCHEMA_VERSION, "adoptions": [a.to_dict() for a in self.all()]}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AdoptionLog":
        if not isinstance(payload, Mapping):
            raise PvpInputError("adoption log must be a JSON object")
        if payload.get("schema_version") != ADOPTION_LOG_SCHEMA_VERSION:
            raise PvpInputError(f"adoption log schema_version must be {ADOPTION_LOG_SCHEMA_VERSION}")
        entries = payload.get("adoptions")
        if not isinstance(entries, list):
            raise PvpInputError("adoption log 'adoptions' must be a list")
        return cls(StrategyAdoption.from_dict(entry) for entry in entries)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "AdoptionLog":
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise PvpInputError("adoption log is not valid JSON") from exc
        return cls.from_dict(payload)

    @classmethod
    def load(cls, path: Union[str, Path]) -> "AdoptionLog":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def save(self, path: Union[str, Path]) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")


@dataclass(frozen=True)
class StrategyAttribution:
    """Why a battle at a given time is credited to a strategy."""

    strategy: PvpStrategy
    window: ExperimentWindow
    adoption: StrategyAdoption

    @property
    def method(self) -> str:
        return self.adoption.method

    @property
    def approximate_timing(self) -> bool:
        return self.adoption.approximate_timing


class TrialRecord:
    """Experiment windows joined with the adoption log (SPEC-08 R1, R7).

    A battle at ``at`` is attributed to a strategy only when ``at`` falls inside
    a window and the alias's latest adoption at or before ``at`` is of that
    window's strategy version and was made inside the window. Battles before
    the first adoption in a window, after a different strategy was adopted, or
    outside every window have no strategy.
    """

    def __init__(self, schedule: ExperimentSchedule, log: AdoptionLog) -> None:
        for adoption in log.all():
            registered = schedule.registry.get(adoption.strategy_id, adoption.strategy_version)
            if registered is None:
                raise PvpInputError(f"adoption of {adoption.key} names an unregistered strategy")
            if registered.config_version != adoption.config_version:
                raise PvpInputError(f"adoption of {adoption.key} does not match the registered config_version")
        self.schedule = schedule
        self.log = log

    def attribution_at(self, account_alias: str, at: datetime) -> Optional[StrategyAttribution]:
        window = self.schedule.window_at(account_alias, at)
        if window is None:
            return None
        adoption = self.log.latest_at(account_alias, at)
        if adoption is None or not window.contains(adoption.adopted_at):
            return None
        if (adoption.strategy_id, adoption.strategy_version) != (window.strategy_id, window.strategy_version):
            return None
        strategy = self.schedule.registry.get(window.strategy_id, window.strategy_version)
        if strategy is None or strategy.config_version != adoption.config_version:
            return None
        return StrategyAttribution(strategy=strategy, window=window, adoption=adoption)

    def strategy_in_force(self, account_alias: str, at: datetime) -> Optional[PvpStrategy]:
        attribution = self.attribution_at(account_alias, at)
        return attribution.strategy if attribution else None

    def has_approximate_timing(self) -> bool:
        """True when any recorded adoption was by hand, so a report must say timing is approximate."""
        return any(a.approximate_timing for a in self.log.all())


# Candidate strategies for the first R3 comparison. They are hypotheses to test,
# not recommendations: neither has battle evidence yet.
CANDIDATE_STRATEGIES: Tuple[PvpStrategy, ...] = (
    PvpStrategy(
        strategy_id="weapon-focus-fire",
        version=1,
        description="Hypothesis: concentrate fire on enemy weapons to cut incoming damage early.",
        battle_strategy=BattleStrategy.FOCUS_FIRE.value,
        native_ai_rules=(
            NativeAIRule(group="weapons", conditions=("always",), actions=("target_room",), target_type="weapon"),
            NativeAIRule(group="repair", conditions=("room_damaged",), actions=("repair_room",), target_type="own"),
        ),
        crew_roles=(("gunner", "weapon"), ("engineer", "reactor"), ("repair", "shield")),
        power_priorities=("shield", "weapon", "reactor"),
        target_priorities=("weapon", "shield", "reactor"),
    ),
    PvpStrategy(
        strategy_id="reactor-shield-devastation",
        version=1,
        description="Hypothesis: break shields, then the reactor, to starve enemy systems of power.",
        battle_strategy=BattleStrategy.REACTOR_SHIELD_DEVASTATION.value,
        native_ai_rules=(
            NativeAIRule(group="weapons", conditions=("enemy_shield_up",), actions=("target_room",), target_type="shield"),
            NativeAIRule(group="weapons", conditions=("enemy_shield_down",), actions=("target_room",), target_type="reactor"),
            NativeAIRule(group="repair", conditions=("room_damaged",), actions=("repair_room",), target_type="own"),
        ),
        crew_roles=(("gunner", "weapon"), ("engineer", "reactor"), ("repair", "shield")),
        power_priorities=("weapon", "shield", "reactor"),
        target_priorities=("shield", "reactor", "weapon"),
    ),
)


def candidate_registry() -> StrategyRegistry:
    return StrategyRegistry(CANDIDATE_STRATEGIES)


def strategy_index(strategies: Iterable[PvpStrategy]) -> Mapping[str, PvpStrategy]:
    """Strategies keyed by ``config_version``, for matching recorded battles back to a strategy."""
    return {s.config_version: s for s in strategies}
