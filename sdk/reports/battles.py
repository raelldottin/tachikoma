"""PvP battle results from journaled ``run_pvp_battle`` events (R3).

Each attempted ``run_pvp_battle`` event becomes one ``JournalBattle``: an
``sdk.pvp.BattleResult`` plus the facts the result type cannot hold (the
attempt id, the strategy key, the evidence tier, and whether the trophy
change was observed at all).

Rules:

- The outcome is ``win`` or ``loss`` only when the event reconciled as
  ``verified`` with evidence tier ``outcome-observed`` and a non-zero
  observed trophy change. Everything else is ``unknown``.
- The strategy comes from the experiment window active when the battle
  started. A battle outside every window has no strategy.
- Skipped and dry-run events are not battles.
- Opponent cohorts are labelled only from recorded opponent facts. Nothing
  records them yet, so they are ``unknown``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, List, Mapping, Optional

from sdk.journal import GameplayEvent
from sdk.pvp import BattleOutcome, BattleResult, BattleRole, BattleStrategy, ExperimentSchedule

BATTLE_ACTION_ID = "run_pvp_battle"
UNKNOWN_COHORT = "unknown"
HULL_BRACKETS = ((1, 4), (5, 7), (8, 10), (11, 13), (14, 20))
TROPHY_BRACKET_SIZE = 500


def hull_bracket(level: Optional[int]) -> str:
    if level is None:
        return UNKNOWN_COHORT
    for low, high in HULL_BRACKETS:
        if low <= level <= high:
            return f"hull {low}-{high}"
    return UNKNOWN_COHORT


def trophy_bracket(trophies: Optional[int]) -> str:
    if trophies is None or trophies < 0:
        return UNKNOWN_COHORT
    low = (trophies // TROPHY_BRACKET_SIZE) * TROPHY_BRACKET_SIZE
    return f"trophies {low}-{low + TROPHY_BRACKET_SIZE - 1}"


def _int_or_none(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class JournalBattle:
    result: BattleResult
    attempt_id: str
    evidence_tier: str
    trophy_delta: Optional[int]
    strategy_key: Optional[str]
    strategy_config_version: Optional[str]
    opponent_hull_bracket: str
    opponent_trophy_bracket: str
    started_at: str
    finished_at: str
    # Observed resource deltas from the event (negative values are spend); None = unobserved.
    cost: Mapping[str, Optional[float]] = field(default_factory=dict)

    @property
    def outcome_known(self) -> bool:
        return self.result.outcome != BattleOutcome.UNKNOWN.value


def _outcome(event: GameplayEvent, tier: str, trophy_delta: Optional[int]) -> str:
    if event.outcome != "verified" or tier != "outcome-observed" or not trophy_delta:
        return BattleOutcome.UNKNOWN.value
    return BattleOutcome.WIN.value if trophy_delta > 0 else BattleOutcome.LOSS.value


def battles_from_events(
    events: Iterable[GameplayEvent], schedule: Optional[ExperimentSchedule] = None
) -> List[JournalBattle]:
    battles = []
    for event in events:
        if event.action_id != BATTLE_ACTION_ID or not event.attempted or event.dry_run:
            continue
        evidence = dict(event.evidence or {})
        tier = str(evidence.get("evidence_tier") or "request-sent")
        trophy_delta = _int_or_none(evidence.get("trophies_delta"))
        outcome = _outcome(event, tier, trophy_delta)

        strategy = None
        if schedule is not None:
            strategy = schedule.active_strategy(event.account_alias, datetime.fromisoformat(event.started_at))

        opponent_hull = _int_or_none(evidence.get("opponent_hull_level"))
        opponent_trophies = _int_or_none(evidence.get("opponent_trophies"))
        battle_id = evidence.get("battle_id") or f"attempt-{event.attempt_id}"
        result = BattleResult(
            battle_id=str(battle_id),
            account_pseudonym=event.account_alias,
            role=BattleRole.ATTACKER.value,
            opponent_hull_level=opponent_hull if opponent_hull is not None and 1 <= opponent_hull <= 20 else None,
            outcome=outcome,
            trophy_delta=trophy_delta or 0,
            battle_strategy=strategy.battle_strategy if strategy else BattleStrategy.UNKNOWN.value,
            ship_config_version=strategy.config_version if strategy else "",
            created_at=event.finished_at,
        )
        battles.append(JournalBattle(
            result=result,
            attempt_id=event.attempt_id,
            evidence_tier=tier,
            trophy_delta=trophy_delta,
            strategy_key=strategy.key if strategy else None,
            strategy_config_version=strategy.config_version if strategy else None,
            opponent_hull_bracket=hull_bracket(result.opponent_hull_level),
            opponent_trophy_bracket=trophy_bracket(opponent_trophies),
            started_at=event.started_at,
            finished_at=event.finished_at,
            cost=dict(event.cost or {}),
        ))
    return battles
