"""The legacy daily training step: read the account, plan, and carry each decision out through the action runner.

``run_training(client, now=...)`` is what ``Client.manageTraining`` runs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Optional

from sdk.execution.runner import ActionRunner
from sdk.journal import GameplayEvent
from sdk.redaction import redact_secrets
from sdk.reports.training import TRAINING_DECISION_ACTION, decision_evidence, validate_decision_evidence
from sdk.state.client_provider import ClientSnapshotProvider
from sdk.state.snapshot import AccountSnapshot, Completeness
from sdk.training.actions import action_for
from sdk.training.model import (
    CANONICAL_STATS,
    SKIP_TRAINING_IN_PROGRESS,
    CrewState,
    FinishTraining,
    Skip,
    StartTraining,
    TrainingContext,
    UseConsumable,
    frozen_mapping,
)
from sdk.training.parse import PAYLOAD_ERROR, build_context, classify_payload, parse_consumable_designs, parse_crew
from sdk.training.planner import (
    percent,
    plan_training,
    remaining_stock,
    replan_after_finish,
    resolve_room_name,
    room_type_for,
    total_points,
)
from sdk.training.strategy import DEFAULT_STRATEGY, TrainingStrategy, with_consumables

LEGACY_ALIAS = "legacy"
SNAPSHOT_SOURCE = "manageTraining"

VERIFIED = "verified"
NO_CHANGE = "no-change"
CONTRADICTED = "contradicted"
UNKNOWN = "unknown"
SKIPPED = "skipped"

logger = logging.getLogger(__name__)

# (label, client read method, attribute it stores the reply on, record key the payload must hold), in read order.
_READS = (
    ("characters", "listAllCharactersOfUser", "allCharactersOfUser", "Character"),
    ("ship", "getShipByUserId", "shipByUserId", "Room"),
    ("character designs", "listAllCharacterDesigns2", "allCharacterDesigns", "CharacterDesign"),
    ("training designs", "listAllTrainingDesigns2", "trainingDesigns", "TrainingDesign"),
    ("room designs", "listRoomDesigns2", "roomDesigns", "RoomDesign"),
    ("research designs", "listAllResearchDesigns2", "allResearchDesigns", "ResearchDesign"),
)
# Runner outcome -> verification (read-only: no module-level mutable state).
_VERIFICATION = frozen_mapping({"verified": VERIFIED, "not-observed": NO_CHANGE, "contradicted": CONTRADICTED,
                                "unknown": UNKNOWN, "skipped": SKIPPED})


def _read(client: Any, method: str, attribute: str) -> Any:
    """The payload a read stored, or None when the read did not return exactly True (a stale attribute is ignored)."""
    return getattr(client, attribute, None) if getattr(client, method)() is True else None


def verification_of(runner_outcome: str) -> str:
    """The report's verification for a runner outcome; anything unexpected is ``unknown``."""
    return _VERIFICATION.get(runner_outcome, UNKNOWN)


class MemoryJournal:
    """An in-memory journal (the runner needs only ``append`` and ``read``); events are validated like on disk."""

    def __init__(self) -> None:
        self._events: list = []

    def append(self, event) -> None:
        event.to_json_line()
        self._events.append(event)

    def read(self) -> list:
        return list(self._events)


class CachingSnapshotProvider:
    """Reuses the last snapshot until ``invalidate()``; one missing any of ``sections`` is never reused."""

    def __init__(self, provider: Callable[[], AccountSnapshot], sections: tuple[str, ...] = ("crew",)) -> None:
        self._provider = provider
        self._sections = sections
        self._cached: Optional[AccountSnapshot] = None
        self.last: Optional[AccountSnapshot] = None

    def __call__(self) -> AccountSnapshot:
        if self._cached is None:
            snapshot = self._provider()
            self.last = snapshot
            if all(snapshot.section(name) is not Completeness.MISSING for name in self._sections):
                self._cached = snapshot
        return self.last if self._cached is None else self._cached

    def invalidate(self) -> None:
        self._cached = None


class _CrewSnapshotClient:
    """The client as the snapshot provider sees it: crew reads only (no ship read; items only with consumables)."""

    def __init__(self, client: Any, read_items: bool) -> None:
        self._client = client
        self._read_items = read_items

    def getShipByUserId(self) -> bool:
        return False

    def listItemsOfAShip(self) -> bool:
        return self._read_items and self._client.listItemsOfAShip()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class TrainingClient:
    """The client as the runner calls it: every mutating call drops the cached snapshot first and answers a bool."""

    def __init__(self, client: Any, invalidate: Callable[[], None]) -> None:
        self._client = client
        self._invalidate = invalidate
        self.finish_replies: dict[str, Mapping] = {}

    def finishTraining(self, character_id: str) -> bool:
        """True when the client returned the finished ``Character`` mapping, which is kept for the replan."""
        self._invalidate()
        reply = self._client.finishTraining(character_id)
        if not isinstance(reply, Mapping):
            return False
        self.finish_replies[character_id] = reply
        return True

    def addTraining(self, training_design_id: str, character_id: str) -> bool:
        self._invalidate()
        return self._client.addTraining(training_design_id, character_id) is True

    def useConsumable(self, item_design_id: str, character_id: str) -> bool:
        self._invalidate()
        return self._client.useConsumable(item_design_id, character_id) is True


@dataclass(frozen=True)
class ActionResult:
    action_id: str
    params: Mapping[str, str]
    verification: str
    reason: str
    attempted: bool
    harmless: bool  # not verified, yet no failure (a finish that was not due, or already collected)

    @property
    def failed(self) -> bool:
        return self.verification != VERIFIED and not self.harmless


@dataclass(frozen=True)
class CrewOutcome:
    character_id: int
    name: str
    decision: Any
    finish: Optional[ActionResult] = None
    action: Optional[ActionResult] = None
    error: Optional[str] = None

    @property
    def failed(self) -> bool:
        return self.error is not None or any(r.failed for r in (self.finish, self.action) if r is not None)


@dataclass(frozen=True)
class TrainingCounts:
    started: int = 0
    finished: int = 0
    consumables: int = 0
    skipped: int = 0
    failed: int = 0

    def line(self) -> str:
        return (f"training: started={self.started} finished={self.finished} consumables={self.consumables} "
                f"skipped={self.skipped} failed={self.failed}")


def _verified(result: Optional[ActionResult], action_id: str) -> bool:
    return result is not None and result.action_id == action_id and result.verification == VERIFIED


def count(outcomes: Iterable[CrewOutcome]) -> TrainingCounts:
    outcomes = tuple(outcomes)
    return TrainingCounts(
        started=sum(_verified(o.action, "add_training") for o in outcomes),
        finished=sum(_verified(o.finish, "finish_training") for o in outcomes),
        consumables=sum(_verified(o.action, "use_consumable") for o in outcomes),
        skipped=sum(isinstance(o.decision, Skip) for o in outcomes),
        failed=sum(o.failed for o in outcomes),
    )


@dataclass(frozen=True)
class TrainingRun:
    readable: bool
    unreadable: tuple[str, ...]
    crew: tuple[CrewOutcome, ...]
    journal: Any = None  # where the runner's events and the decision records went (None when nothing was read)

    @property
    def counts(self) -> TrainingCounts:
        return count(self.crew)


def _run_action(runner: ActionRunner, decision: Any, harmless: Callable[[Any], bool] = lambda result: False
                ) -> ActionResult:
    action_id, params = action_for(decision)
    result = runner.run(action_id, params)
    return ActionResult(action_id, dict(params), verification_of(result.outcome), result.reason, result.attempted,
                        harmless(result))


def _finish_harmless(snapshot: Callable[[], Optional[AccountSnapshot]], character_id: str,
                     replied: Callable[[], bool]) -> Callable[[Any], bool]:
    """Whether a finish_training that was not verified is still no failure, judged from what the runner observed.

    Refused by its preconditions while the crew member's training was observed and readable: the training is not due
    yet, or there is nothing to finish (a refusal over an unobserved or unreadable training is a failure: the
    uncollected training may still be there). Sent without a reply, and the crew member observed afterwards with no
    uncollected training: another device collected it (the server answers "No training to finish"). A contradicted
    finish (stats or starbux fell) is never harmless.
    """
    def harmless(result) -> bool:
        observed = snapshot()
        member = observed.crew_member(character_id) if observed is not None else None
        if member is None or verification_of(result.outcome) == CONTRADICTED:
            return False
        if not result.attempted:
            return bool(member.training_observed and (member.training_design_id is None or not member.training_active
                                                       or member.training_end_utc is not None))
        return not replied() and member.training_design_id is None

    return harmless


_STAT_KEYS = tuple("@" + stat for stat in CANONICAL_STATS)


def crew_from_reply(reply: Mapping, character_id: int, *, character_designs_raw: Any, rooms_raw: Any,
                    room_designs_raw: Any) -> Optional[CrewState]:
    """The crew member rebuilt from a FinishTraining reply, or None unless it names ``character_id`` and carries all
    nine stat improvements as whole numbers (a partial reply must never plan the next tier from wrong stats)."""
    if reply.get("@CharacterId") != str(character_id):
        return None
    if not all(re.fullmatch(r"[0-9]+", str(reply.get(key))) for key in _STAT_KEYS):
        return None
    return parse_crew({"Character": dict(reply)}, character_designs_raw, rooms_raw, room_designs_raw)[0]


def _stat_changes(before: CrewState, after: Optional[CrewState]) -> str:
    if after is None:
        return "stat changes not reported"
    changes = [f"{stat.removesuffix('Improvement')} {after.stats[stat] - before.stats[stat]:+d}"
               for stat in CANONICAL_STATS if after.stats[stat] != before.stats[stat]]
    return ", ".join(changes) or "no stat change"


def run_training(
    client: Any,
    *,
    now: Callable[[], datetime],
    account_alias: str = LEGACY_ALIAS,
    journal: Any = None,
    strategy: TrainingStrategy = DEFAULT_STRATEGY,
    item_designs_raw: Optional[Mapping] = None,
    label: str = "",
) -> TrainingRun:
    prefix = f"[{label}] " if label else ""
    consumables = parse_consumable_designs(item_designs_raw)
    if consumables:
        strategy = with_consumables(strategy, consumables)
    read_items = bool(strategy.consumables)
    payloads = {name: _read(client, method, attribute) for name, method, attribute, _ in _READS}
    items = _read(client, "listItemsOfAShip", "itemsOfAShip") if read_items else None
    unreadable = tuple(name for name, _, _, key in _READS if classify_payload(payloads[name], key) == PAYLOAD_ERROR)
    if unreadable:
        logger.error("%straining: cannot read %s; no training this run", prefix, ", ".join(unreadable))
        logger.info("%s%s", prefix, TrainingCounts().line())
        return TrainingRun(False, unreadable, (), journal)
    ctx = build_context(
        now=now(),
        characters_raw=payloads["characters"],
        character_designs_raw=payloads["character designs"],
        training_designs_raw=payloads["training designs"],
        rooms_raw=payloads["ship"],
        room_designs_raw=payloads["room designs"],
        researches_raw=payloads["ship"],
        research_designs_raw=payloads["research designs"],
        items_raw=items,
    )
    cache = CachingSnapshotProvider(ClientSnapshotProvider(
        _CrewSnapshotClient(client, read_items), account_alias, clock=now, source=SNAPSHOT_SOURCE),
        ("crew", "inventory") if read_items else ("crew",))
    proxy = TrainingClient(client, cache.invalidate)
    runner = ActionRunner(account_alias, proxy, cache, MemoryJournal() if journal is None else journal, clock=now)
    plan = plan_training(ctx, strategy)
    finished = [decision.character_id for decision in plan if isinstance(decision, FinishTraining)]
    # The stock a finished crew's replan may use: what every final (non-finished) decision left of the ship's items.
    stock = remaining_stock(ctx.items, plan, exclude_character_ids=finished)
    session = _Session(ctx, payloads, strategy, runner, proxy, cache, prefix, stock, now)
    decisions = iter(plan)
    outcomes = []
    for crew in ctx.crew:
        first = next(decisions)
        finish = first if isinstance(first, FinishTraining) else None
        outcomes.append(session.train(crew, finish, next(decisions) if finish else first))
    logger.info("%s%s", prefix, count(outcomes).line())
    return TrainingRun(True, (), tuple(outcomes), runner.journal)


class _Session:
    """One run's collaborators; ``train`` handles one crew member. Only the consumable stock carries over to the next
    crew member (as in the planner); rooms, designs and replies never do."""

    def __init__(self, ctx: TrainingContext, payloads: Mapping[str, Any], strategy: TrainingStrategy,
                 runner: ActionRunner, proxy: TrainingClient, cache: CachingSnapshotProvider, prefix: str,
                 stock: Mapping[int, int], now: Callable[[], datetime]) -> None:
        self.ctx, self.payloads, self.strategy = ctx, payloads, strategy
        self.runner, self.proxy, self.cache, self.prefix = runner, proxy, cache, prefix
        self.designs = {design.id: design.name for design in ctx.designs}
        self.stock = stock  # reduced by each replan's UseConsumable, so no item is committed twice
        self.now = now

    def train(self, crew: CrewState, finish_decision: Optional[FinishTraining], primary: Any) -> CrewOutcome:
        cid = crew.character_id
        who = f"{crew.name} ({cid})"
        room = resolve_room_name(crew, self.ctx) or "no room"
        decision = primary if finish_decision is None else Skip(cid, SKIP_TRAINING_IN_PROGRESS,
                                                                "finish_training not run")
        planned = crew
        finish = action = error = None
        started_at = self.now()
        try:
            if finish_decision is not None:
                finish, rebuilt = self._finish(finish_decision, crew)
                if finish.verification == VERIFIED:
                    logger.info("%sCompleted training for %s in %s with %s", self.prefix, crew.name, room,
                                _stat_changes(crew, rebuilt))
                else:
                    self._log_result(who, finish)
                if finish.verification == VERIFIED and rebuilt is not None:
                    planned = rebuilt
                    decision = replan_after_finish(self.ctx, rebuilt, self.strategy, items=self.stock)[0]
                    self.stock = remaining_stock(self.stock, (decision,))
                else:
                    decision = Skip(cid, SKIP_TRAINING_IN_PROGRESS, f"finish_training {finish.verification}: "
                                                                    f"{finish.reason}")
            if isinstance(decision, Skip):
                logger.info("%straining: %s skip %s: %s", self.prefix, who, decision.reason_code, decision.detail)
            else:
                action = _run_action(self.runner, decision)
                if action.verification == VERIFIED and isinstance(decision, StartTraining):
                    pct = percent(total_points(planned.stats), planned.training_capacity)
                    logger.info("%sStarting training %s for %s in %s with %d%% training complete", self.prefix,
                                self.designs[decision.training_design_id], crew.name, room, pct)
                else:
                    self._log_result(who, action)
        except Exception as caught:  # one crew's failure never stops the rest of the roster
            error = f"{type(caught).__name__}: {redact_secrets(str(caught))}"
            logger.error("%straining: %s failed: %s", self.prefix, who, error)
        try:
            self.runner.journal.append(self._decision_record(crew, planned, decision, started_at))
        except Exception as caught:  # a journal that refuses the record never stops the run either
            logger.error("%straining: %s decision record not written: %s: %s", self.prefix, who,
                         type(caught).__name__, redact_secrets(str(caught)))
        return CrewOutcome(cid, crew.name, decision, finish=finish, action=action, error=error)

    def _decision_record(self, crew: CrewState, planned: CrewState, decision: Any,
                         started_at: datetime) -> GameplayEvent:
        """The ``training_decision`` record for one crew member (docs/baseline/training-report.md, contract v1)."""
        if isinstance(decision, UseConsumable):  # a consumable stands in for the skip planned without one
            decision = plan_training(replace(self.ctx, crew=(planned,), items={}), self.strategy)[-1]
        finished_at = self.now()
        member = self.cache.last.crew_member(str(crew.character_id)) if self.cache.last is not None else None
        end = member.training_end_utc if member is not None and member.training_observed else crew.training_end
        skip = isinstance(decision, Skip)
        evidence = decision_evidence(
            character_id=str(crew.character_id),
            decision="skip" if skip else "start",
            in_training_room=room_type_for(resolve_room_name(crew, self.ctx), self.strategy) is not None,
            training_active=end is not None and end > finished_at,
            reason_code=decision.reason_code if skip else None,
        )
        violations = validate_decision_evidence(evidence)
        if violations:
            raise ValueError(f"decision record breaks the contract: {', '.join(violations)}")
        return GameplayEvent.create(account_alias=self.runner.account_alias, action_id=TRAINING_DECISION_ACTION,
                                    started_at=started_at, finished_at=finished_at, outcome="skipped",
                                    reason=decision.detail if skip else decision.reason, attempted=False,
                                    dry_run=False, evidence=evidence)

    def _finish(self, decision: FinishTraining, crew: CrewState) -> tuple[ActionResult, Optional[CrewState]]:
        """Run finish_training; the result and the crew rebuilt from the reply (None without a usable reply)."""
        key = str(crew.character_id)
        result = _run_action(self.runner, decision,
                             _finish_harmless(lambda: self.cache.last, key, lambda: key in self.proxy.finish_replies))
        reply = self.proxy.finish_replies.pop(key, None)
        if reply is None:
            return result, None
        return result, crew_from_reply(reply, crew.character_id,
                                       character_designs_raw=self.payloads["character designs"],
                                       rooms_raw=self.payloads["ship"], room_designs_raw=self.payloads["room designs"])

    def _log_result(self, who: str, result: ActionResult) -> None:
        log = logger.warning if result.failed else logger.info
        log("%straining: %s %s %s: %s", self.prefix, who, result.action_id, result.verification, result.reason)
