"""Versioned PvP strategies and experiment windows (R3, AUTO-006).

A strategy is data: what the ship's native AI, crew roles, power and targeting
should favour. Its ``config_version`` is a hash of that data, so two runs can
only be compared as "the same strategy" when the hash matches.

An ``ExperimentSchedule`` assigns at most one strategy to an account alias for
each UTC window. Windows are half-open (``start <= t < end``) and may not
overlap for the same alias, so every battle maps to exactly one strategy or
to none.

Pure and offline: nothing here talks to the game.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from sdk.redaction import redact_secrets
from sdk.state.snapshot import ACCOUNT_ALIAS_PATTERN

from .models import BattleStrategy, EvidenceTier, NativeAIRule, PvpInputError

STRATEGY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


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
