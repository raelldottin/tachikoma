"""PvP battle results from journaled ``run_pvp_battle`` events (R3).

Each attempted ``run_pvp_battle`` event becomes one ``JournalBattle``: an
``sdk.pvp.BattleResult`` plus the facts the result type cannot hold (the
attempt id, the strategy key, the evidence tier, and whether the trophy
change was observed at all).

Rules:

- The outcome is ``win`` or ``loss`` only when the event reconciled as
  ``verified`` with evidence tier ``outcome-observed``, a non-zero observed
  trophy change, and no error from the call. Everything else is ``unknown``.
- The strategy comes from the experiment window active when the battle
  started (the start time is normalised to UTC first). A battle outside
  every window has no strategy.
- Skipped and dry-run events are not battles.
- Opponent cohorts are labelled only from recorded opponent facts (S03-R6).
  An observed battle record with the event's battle id that shows the account
  attacking supplies them: one passed in ``battle_records``, else the one the
  runner journaled as ``evidence["battle_record"]``. Without one, the
  ``opponent_*`` evidence fields are used; nothing else records them, so the
  cohort is ``unknown``. The captured battle list holds no hull level and
  reports every trophy count as 0 (read as unreported), so a record alone
  labels both cohorts ``unknown`` today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, List, Mapping, Optional

from sdk.journal import GameplayEvent
from sdk.pvp import BattleOutcome, BattleResult, BattleRole, BattleStrategy, ExperimentSchedule
from sdk.state.profile_reads import ATTACKER, BattleRecord, matching_battle

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


def _utc(value: str) -> datetime:
    """Journal times are timezone-aware but not always UTC; schedules require UTC."""
    moment = datetime.fromisoformat(value)
    return moment.astimezone(timezone.utc) if moment.tzinfo is not None else moment


def _outcome(event: GameplayEvent, tier: str, trophy_delta: Optional[int]) -> str:
    if (event.outcome != "verified" or event.error_class is not None or tier != "outcome-observed"
            or not trophy_delta):
        return BattleOutcome.UNKNOWN.value
    return BattleOutcome.WIN.value if trophy_delta > 0 else BattleOutcome.LOSS.value


def _opponent_facts(evidence: Mapping, records: List[BattleRecord]) -> Mapping:
    """Opponent facts from the observed attack record with the event's battle id, else from the evidence."""
    record = matching_battle(records, evidence.get("battle_id"))
    if record is not None and record.role == ATTACKER:
        return {"opponent_hull_level": record.opponent_hull_level, "opponent_trophies": record.opponent_trophies}
    journaled = evidence.get("battle_record")
    if isinstance(journaled, Mapping) and journaled.get("role") == ATTACKER:
        return journaled
    return evidence


def battles_from_events(
    events: Iterable[GameplayEvent], schedule: Optional[ExperimentSchedule] = None,
    battle_records: Iterable[BattleRecord] = (),
) -> List[JournalBattle]:
    """One ``JournalBattle`` per attempted battle; ``battle_records`` are observed ``ListPvPBattles2`` records."""
    records = list(battle_records)
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
            strategy = schedule.active_strategy(event.account_alias, _utc(event.started_at))

        opponent = _opponent_facts(evidence, records)
        opponent_hull = _int_or_none(opponent.get("opponent_hull_level"))
        opponent_trophies = _int_or_none(opponent.get("opponent_trophies"))
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
