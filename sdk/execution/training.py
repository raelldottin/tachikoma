"""The legacy daily training step: read the account, plan, and carry each decision out through the action runner.

``run_training(client, now=...)`` is what ``Client.manageTraining`` runs. The decisions are pure functions
(``pair_plan``, ``finish_is_harmless``, ``crew_from_reply``, ``crew_from_observation``, ``decision_after_finish``,
``decision_evidence_for`` and the ``*_message`` formatters); ``run_training`` and ``_train`` only read, run actions,
log and journal. Neither a message that cannot be logged nor a journal that refuses an event changes an outcome.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Optional, Sequence

from sdk.execution.runner import ActionRunner
from sdk.journal import GameplayEvent
from sdk.redaction import redact_secrets
from sdk.reports.training import TRAINING_DECISION_ACTION, decision_evidence, validate_decision_evidence
from sdk.state.client_provider import ClientSnapshotProvider
from sdk.state.snapshot import TRAINING_STAT_ATTRIBUTES, AccountSnapshot, Completeness, CrewMember
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
TRAINING_READS = (
    ("characters", "listAllCharactersOfUser", "allCharactersOfUser", "Character"),
    ("ship", "getShipByUserId", "shipByUserId", "Room"),
    ("character designs", "listAllCharacterDesigns2", "allCharacterDesigns", "CharacterDesign"),
    ("training designs", "listAllTrainingDesigns2", "trainingDesigns", "TrainingDesign"),
    ("room designs", "listRoomDesigns2", "roomDesigns", "RoomDesign"),
    ("research designs", "listAllResearchDesigns2", "allResearchDesigns", "ResearchDesign"),
)
# Runner outcome -> verification; any other outcome (the runner's "unknown" too) is UNKNOWN. Read-only.
_VERIFICATION = frozen_mapping({"verified": VERIFIED, "not-observed": NO_CHANGE, "contradicted": CONTRADICTED,
                                "skipped": SKIPPED})


class PlanShapeError(ValueError):
    """The plan does not pair with the roster: the planner's output shape changed. Raised before any action runs."""


def verification_of(runner_outcome: str) -> str:
    """The report's verification for a runner outcome; anything unexpected is ``unknown``."""
    return _VERIFICATION.get(runner_outcome, UNKNOWN)


# --- reading ----------------------------------------------------------------------------------------------------------


def _read(client: Any, method: str, attribute: str) -> Any:
    """The payload a read stored, or None when the read did not return exactly True (a stale attribute is ignored)."""
    return getattr(client, attribute, None) if getattr(client, method)() is True else None


@dataclass(frozen=True)
class TrainingReads:
    payloads: Mapping[str, Any]  # by TRAINING_READS label; None for a failed read
    items: Any  # the ship's items, read only for a strategy with consumables
    unreadable: tuple[str, ...]  # labels whose payload is an error (in read order)


def read_training_data(client: Any, *, read_items: bool) -> TrainingReads:
    """Every read the plan needs, once and in order (the items last, and only when ``read_items``)."""
    payloads = {label: _read(client, method, attribute) for label, method, attribute, _ in TRAINING_READS}
    items = _read(client, "listItemsOfAShip", "itemsOfAShip") if read_items else None
    unreadable = tuple(label for label, _, _, key in TRAINING_READS
                       if classify_payload(payloads[label], key) == PAYLOAD_ERROR)
    return TrainingReads(payloads, items, unreadable)


def context_from(reads: TrainingReads, now: datetime) -> TrainingContext:
    payloads = reads.payloads
    return build_context(
        now=now,
        characters_raw=payloads["characters"],
        character_designs_raw=payloads["character designs"],
        training_designs_raw=payloads["training designs"],
        rooms_raw=payloads["ship"],
        room_designs_raw=payloads["room designs"],
        researches_raw=payloads["ship"],
        research_designs_raw=payloads["research designs"],
        items_raw=reads.items,
    )


# --- the seams the runner sees ----------------------------------------------------------------------------------------


class MemoryJournal:
    """An in-memory journal (the runner needs only ``append`` and ``read``); events are validated like on disk."""

    def __init__(self) -> None:
        self._events: list = []

    def append(self, event) -> None:
        event.to_json_line()
        self._events.append(event)

    def read(self) -> list:
        return list(self._events)


class GuardedJournal:
    """The journal as the runner sees it: a refused append or a failed read never stops training.

    A refused event is logged (redacted) and dropped: the runner's result, not the write, decides the crew's outcome.
    A journal that cannot be read is logged and taken as an empty history (no uncertain earlier attempt).
    """

    def __init__(self, journal: Any, log: Callable[[tuple[int, str]], None]) -> None:
        self._journal = journal
        self._log = log

    def append(self, event: GameplayEvent) -> None:
        try:
            self._journal.append(event)
        except Exception as caught:
            self._log((logging.ERROR, f"training: journal refused the {event.action_id} event: "
                                      f"{type(caught).__name__}: {redact_secrets(str(caught))}"))

    def read(self) -> list:
        try:
            return self._journal.read()
        except Exception as caught:
            self._log((logging.ERROR, f"training: journal not readable, taken as an empty history: "
                                      f"{type(caught).__name__}: {redact_secrets(str(caught))}"))
            return []


class CachingSnapshotProvider:
    """Reuses the last snapshot until ``invalidate()``; one missing any of ``sections`` is never reused."""

    def __init__(self, provider: Callable[[], AccountSnapshot], sections: tuple[str, ...]) -> None:
        self._provider = provider
        self._sections = sections
        self._cached: Optional[AccountSnapshot] = None
        self.last: Optional[AccountSnapshot] = None

    def __call__(self) -> AccountSnapshot:
        if self._cached is not None:
            return self._cached
        snapshot = self._provider()
        self.last = snapshot
        if all(snapshot.section(name) is not Completeness.MISSING for name in self._sections):
            self._cached = snapshot
        return snapshot

    def invalidate(self) -> None:
        self._cached = None


class CrewSnapshotClient:
    """The client as the snapshot provider sees it: the crew read, and the items read only when asked for.

    It has no ship read: the provider finds no ``getShipByUserId``, counts the read as failed and leaves the ship
    section missing (no action here needs it). Nothing else of the client is reachable through it.
    """

    def __init__(self, client: Any, read_items: bool) -> None:
        self._client = client
        self._read_items = read_items

    def listAllCharactersOfUser(self) -> bool:
        return self._client.listAllCharactersOfUser() is True

    @property
    def allCharactersOfUser(self) -> Any:
        return self._client.allCharactersOfUser

    def listItemsOfAShip(self) -> bool:
        return self._read_items and self._client.listItemsOfAShip() is True

    @property
    def itemsOfAShip(self) -> Any:
        return self._client.itemsOfAShip


class TrainingClient:
    """The client as the runner calls it: every mutating call drops the cached snapshot first and answers a bool."""

    def __init__(self, client: Any, invalidate: Callable[[], None]) -> None:
        self._client = client
        self._invalidate = invalidate
        self._finish_replies: dict[str, Mapping] = {}

    def finishTraining(self, character_id: str) -> bool:
        """True when the client returned the finished ``Character`` mapping, kept for ``take_finish_reply``."""
        self._invalidate()
        reply = self._client.finishTraining(character_id)
        if not isinstance(reply, Mapping):
            return False
        self._finish_replies[character_id] = reply
        return True

    def take_finish_reply(self, character_id: str) -> Optional[Mapping]:
        """The reply kept for ``character_id``'s finish (once), or None."""
        return self._finish_replies.pop(character_id, None)

    def addTraining(self, training_design_id: str, character_id: str) -> bool:
        self._invalidate()
        return self._client.addTraining(training_design_id, character_id) is True

    def useConsumable(self, item_design_id: str, character_id: str) -> bool:
        self._invalidate()
        return self._client.useConsumable(item_design_id, character_id) is True


# --- results ----------------------------------------------------------------------------------------------------------


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


# --- pure decisions ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CrewPlan:
    crew: CrewState
    finish: Optional[FinishTraining]  # collect the ended training first, then replan
    primary: Any  # the decision without a finish (advisory when ``finish`` is set: the replan replaces it)


def _decision_for(crew: CrewState, decision: Any) -> Any:
    """``decision`` when it names ``crew`` (a missing one names nobody)."""
    if getattr(decision, "character_id", None) != crew.character_id:
        raise PlanShapeError(f"the plan has no decision in place for crew member {crew.character_id}")
    return decision


def pair_plan(crew: Sequence[CrewState], plan: Iterable[Any]) -> tuple[CrewPlan, ...]:
    """The plan split per crew member, in roster order: an optional FinishTraining, then exactly one other decision,
    each naming that crew member. PlanShapeError for anything else (misordered, short or long plans)."""
    decisions = iter(plan)
    pairs = []
    for member in crew:
        first = _decision_for(member, next(decisions, None))
        finish = first if isinstance(first, FinishTraining) else None
        primary = first if finish is None else _decision_for(member, next(decisions, None))
        if isinstance(primary, FinishTraining):
            raise PlanShapeError(f"the plan finishes crew member {member.character_id} twice")
        pairs.append(CrewPlan(member, finish, primary))
    extra = next(decisions, None)
    if extra is not None:
        raise PlanShapeError(f"the plan has a decision for crew member {extra.character_id}, who is not on the roster")
    return tuple(pairs)


def finish_is_harmless(*, verification: str, attempted: bool, replied: bool, member: Optional[CrewMember]) -> bool:
    """Whether a finish_training that was not verified is still no failure, judged from the crew member the runner
    last observed (``member``; None when no snapshot saw them).

    Refused by its preconditions while the training state was readable: not due yet, not training, or nothing to
    finish (a refusal over an unobserved or unreadable training is a failure: the uncollected training may still be
    there). Sent without a reply, and observed afterwards with no training left to collect: another device collected
    it (the server answers "No training to finish"). A contradicted finish (stats fell) is never harmless.

    The step's snapshots hold no profile, so starbux is never observed here and the catalog's "starbux fell" check
    cannot fire: the guard against paying to speed a training up is the catalog's 60 s grace on the observed end date.
    """
    if member is None or verification == CONTRADICTED:
        return False
    if not attempted:
        return member.training_observed and (member.training_design_id is None or not member.training_active
                                             or member.training_end_utc is not None)
    return not replied and member.training_design_id is None


_STAT_KEYS = tuple("@" + stat for stat in CANONICAL_STATS)


def crew_from_reply(reply: Mapping, crew: CrewState, *, character_designs_raw: Any, rooms_raw: Any,
                    room_designs_raw: Any) -> Optional[CrewState]:
    """``crew`` with the stats, fatigue and training of a FinishTraining reply, or None unless the reply names ``crew``
    and carries all nine stat improvements and the fatigue as whole numbers (a partial reply must never plan the next
    tier from wrong stats). Name, design and room stay those of ``crew``: a reply that leaves them out changes no role,
    capacity or room."""
    if reply.get("@CharacterId") != str(crew.character_id):
        return None
    if not all(re.fullmatch(r"[0-9]+", str(reply.get(key))) for key in (*_STAT_KEYS, "@Fatigue")):
        return None
    parsed = parse_crew({"Character": dict(reply)}, character_designs_raw, rooms_raw, room_designs_raw)[0]
    return replace(crew, stats=parsed.stats, fatigue=parsed.fatigue, training_design_id=parsed.training_design_id,
                   training_end=parsed.training_end)


def crew_from_observation(crew: CrewState, member: Optional[CrewMember]) -> Optional[CrewState]:
    """``crew``, idle, with the stats and fatigue a snapshot observed (``member``), or None unless ``member`` shows the
    training state with no training left to collect, all nine stat improvements and the fatigue. Name, design and room
    stay those of ``crew``, as for a reply."""
    if member is None or not member.training_observed or (member.training_design_id is not None
                                                           and member.training_active):
        return None
    stats = {attribute.removeprefix("@"): getattr(member, name) for name, attribute in TRAINING_STAT_ATTRIBUTES}
    if member.fatigue is None or None in stats.values():
        return None
    return replace(crew, stats=stats, fatigue=member.fatigue, training_design_id=None, training_end=None)


def decision_after_finish(crew: CrewState, finish: ActionResult, rebuilt: Optional[CrewState],
                          observed: Optional[CrewState], *, ctx: TrainingContext, strategy: TrainingStrategy,
                          stock: Mapping[int, int]) -> tuple[Any, CrewState, Mapping[int, int]]:
    """(decision, the crew it was planned for, the stock left) once finish_training ran.

    A finish that did not fail (verified, or harmless: nothing was left to collect) replans from the reply
    (``rebuilt``), else from the crew member the runner observed afterwards (``observed``, see
    ``crew_from_observation``), and takes any consumable from ``stock``. A failed finish, or one with neither, keeps the
    crew member in training, naming only the finish's verification (the runner's reason is logged, never recorded).
    """
    fresh = rebuilt if rebuilt is not None else observed
    if finish.failed or fresh is None:
        return Skip(crew.character_id, SKIP_TRAINING_IN_PROGRESS, f"finish_training {finish.verification}"), crew, stock
    decision = replan_after_finish(ctx, fresh, strategy, items=stock)[0]
    return decision, fresh, remaining_stock(stock, (decision,))


def recorded_decision(planned: CrewState, decision: Any, *, ctx: TrainingContext, strategy: TrainingStrategy) -> Any:
    """The decision a ``training_decision`` record names: a consumable stands in for the skip planned without one."""
    if isinstance(decision, UseConsumable):
        (alone,) = pair_plan((planned,), plan_training(replace(ctx, crew=(planned,), items={}), strategy))
        return alone.primary
    return decision


def contract_checked(evidence: Mapping) -> Mapping:
    """``evidence`` unchanged, or ValueError naming each contract violation: a broken record is never written."""
    violations = validate_decision_evidence(evidence)
    if violations:
        raise ValueError(f"decision record breaks the contract: {', '.join(violations)}")
    return evidence


def decision_evidence_for(crew: CrewState, decision: Any, *, ctx: TrainingContext, strategy: TrainingStrategy,
                          observed: Optional[CrewMember], at: datetime) -> Mapping:
    """The evidence of one crew member's ``training_decision`` record (docs/baseline/training-report.md, contract v1).

    The training counts as active while its end is after ``at``; the end is the last observed one (``observed``,
    when it saw the training), else the one the plan used.
    """
    end = observed.training_end_utc if observed is not None and observed.training_observed else crew.training_end
    skip = isinstance(decision, Skip)
    return contract_checked(decision_evidence(
        character_id=str(crew.character_id),
        decision="skip" if skip else "start",
        in_training_room=room_type_for(resolve_room_name(crew, ctx), strategy) is not None,
        training_active=end is not None and end > at,
        reason_code=decision.reason_code if skip else None,
    ))


def _stat_changes(before: CrewState, after: Optional[CrewState]) -> str:
    if after is None:
        return "stat changes not reported"
    changes = [f"{stat.removesuffix('Improvement')} {after.stats[stat] - before.stats[stat]:+d}"
               for stat in CANONICAL_STATS if after.stats[stat] != before.stats[stat]]
    return ", ".join(changes) or "no stat change"


# --- pure messages: (level, text) without the label prefix ------------------------------------------------------------


def _who(crew: CrewState) -> str:
    return f"{crew.name} ({crew.character_id})"


def unreadable_message(labels: Iterable[str]) -> tuple[int, str]:
    return logging.ERROR, f"training: cannot read {', '.join(labels)}; no training this run"


def summary_message(outcomes: Iterable[CrewOutcome]) -> tuple[int, str]:
    return logging.INFO, count(outcomes).line()


def result_message(who: str, result: ActionResult) -> tuple[int, str]:
    level = logging.WARNING if result.failed else logging.INFO
    return level, f"training: {who} {result.action_id} {result.verification}: {result.reason}"


def finish_message(crew: CrewState, room: str, finish: ActionResult, rebuilt: Optional[CrewState]) -> tuple[int, str]:
    if finish.verification != VERIFIED:
        return result_message(_who(crew), finish)
    return logging.INFO, f"Completed training for {crew.name} in {room} with {_stat_changes(crew, rebuilt)}"


def skip_message(crew: CrewState, skip: Skip) -> tuple[int, str]:
    return logging.INFO, f"training: {_who(crew)} skip {skip.reason_code}: {skip.detail}"


def action_message(crew: CrewState, room: str, decision: Any, action: ActionResult, planned: CrewState,
                   design_names: Mapping[int, str]) -> tuple[int, str]:
    if action.verification != VERIFIED or not isinstance(decision, StartTraining):
        return result_message(_who(crew), action)
    pct = percent(total_points(planned.stats), planned.training_capacity)
    return logging.INFO, (f"Starting training {design_names[decision.training_design_id]} for {crew.name} in {room} "
                          f"with {pct}% training complete")


# --- the effectful shell ----------------------------------------------------------------------------------------------


def _with_item_designs(strategy: TrainingStrategy, item_designs_raw: Optional[Mapping]) -> TrainingStrategy:
    """``strategy`` with the consumables the item designs name; unchanged when they name none."""
    consumables = parse_consumable_designs(item_designs_raw)
    return with_consumables(strategy, consumables) if consumables else strategy


def _log(prefix: str, message: tuple[int, str]) -> None:
    level, text = message
    logger.log(level, "%s%s", prefix, text)


def _observed(snapshot: Optional[AccountSnapshot], crew: CrewState) -> Optional[CrewMember]:
    return snapshot.crew_member(str(crew.character_id)) if snapshot is not None else None


class _Run(NamedTuple):
    """One run's collaborators (read-only). Only the consumable stock carries over between crew members (threaded
    through ``_train``); rooms, designs and replies never do."""

    ctx: TrainingContext
    payloads: Mapping[str, Any]
    strategy: TrainingStrategy
    runner: ActionRunner
    proxy: TrainingClient
    cache: CachingSnapshotProvider
    prefix: str
    now: Callable[[], datetime]
    design_names: Mapping[int, str]
    journal: Any  # the journal itself (the runner writes through a GuardedJournal over it)

    def log(self, message: tuple[int, str]) -> None:
        _log(self.prefix, message)

    def report(self, crew: CrewState, message: Callable[[], tuple[int, str]]) -> None:
        """Log ``message()``; one that cannot be built or logged is itself logged, and changes no outcome."""
        try:
            self.log(message())
        except Exception as caught:
            self.log((logging.ERROR, f"training: {_who(crew)} message not logged: {type(caught).__name__}: "
                                     f"{redact_secrets(str(caught))}"))

    def run_action(self, decision: Any) -> tuple[str, dict, Any]:
        action_id, params = action_for(decision)
        return action_id, dict(params), self.runner.run(action_id, params)


def _result(action_id: str, params: dict, ran: Any, harmless: bool = False) -> ActionResult:
    return ActionResult(action_id, params, verification_of(ran.outcome), ran.reason, ran.attempted, harmless)


def _finish(run: _Run, decision: FinishTraining, crew: CrewState) -> tuple[ActionResult, Optional[CrewState]]:
    """Run finish_training; the result and the crew rebuilt from the reply (None without a usable reply)."""
    action_id, params, ran = run.run_action(decision)
    reply = run.proxy.take_finish_reply(str(crew.character_id))
    harmless = finish_is_harmless(verification=verification_of(ran.outcome), attempted=ran.attempted,
                                  replied=reply is not None, member=_observed(run.cache.last, crew))
    rebuilt = None if reply is None else crew_from_reply(
        reply, crew, character_designs_raw=run.payloads["character designs"], rooms_raw=run.payloads["ship"],
        room_designs_raw=run.payloads["room designs"])
    return _result(action_id, params, ran, harmless), rebuilt


def _record(run: _Run, crew: CrewState, planned: CrewState, decision: Any, started_at: datetime) -> GameplayEvent:
    decision = recorded_decision(planned, decision, ctx=run.ctx, strategy=run.strategy)
    finished_at = run.now()
    evidence = decision_evidence_for(crew, decision, ctx=run.ctx, strategy=run.strategy,
                                     observed=_observed(run.cache.last, crew), at=finished_at)
    skip = isinstance(decision, Skip)
    return GameplayEvent.create(account_alias=run.runner.account_alias, action_id=TRAINING_DECISION_ACTION,
                                started_at=started_at, finished_at=finished_at, outcome="skipped",
                                reason=decision.detail if skip else decision.reason, attempted=False, dry_run=False,
                                evidence=evidence)


def _train(run: _Run, item: CrewPlan, stock: Mapping[int, int]) -> tuple[CrewOutcome, Mapping[int, int]]:
    """Carry out one crew member's decisions; the outcome and the stock left for the next crew member."""
    crew = item.crew
    room = resolve_room_name(crew, run.ctx) or "no room"
    decision = item.primary if item.finish is None else Skip(crew.character_id, SKIP_TRAINING_IN_PROGRESS,
                                                             "finish_training did not complete")
    planned, finish, rebuilt, action, error = crew, None, None, None, None
    started_at = run.now()
    try:  # the outcome only: every message is built and logged afterwards, where it cannot change it
        if item.finish is not None:
            finish, rebuilt = _finish(run, item.finish, crew)
            observed = crew_from_observation(crew, _observed(run.cache.last, crew))
            decision, planned, stock = decision_after_finish(crew, finish, rebuilt, observed, ctx=run.ctx,
                                                             strategy=run.strategy, stock=stock)
        if not isinstance(decision, Skip):
            action = _result(*run.run_action(decision))
    except Exception as caught:  # one crew's failure never stops the rest of the roster
        error = f"{type(caught).__name__}: {redact_secrets(str(caught))}"
    if finish is not None:
        run.report(crew, lambda: finish_message(crew, room, finish, rebuilt))
    if action is not None:
        run.report(crew, lambda: action_message(crew, room, decision, action, planned, run.design_names))
    elif error is None:  # nothing ran and nothing failed: the decision is a skip
        run.report(crew, lambda: skip_message(crew, decision))
    if error is not None:
        run.log((logging.ERROR, f"training: {_who(crew)} failed: {error}"))
    try:
        run.journal.append(_record(run, crew, planned, decision, started_at))
    except Exception as caught:  # a journal that refuses the record never stops the run either
        run.log((logging.ERROR, f"training: {_who(crew)} decision record not written: {type(caught).__name__}: "
                                f"{redact_secrets(str(caught))}"))
    return CrewOutcome(crew.character_id, crew.name, decision, finish=finish, action=action, error=error), stock


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
    strategy = _with_item_designs(strategy, item_designs_raw)
    read_items = bool(strategy.consumables)
    reads = read_training_data(client, read_items=read_items)
    if reads.unreadable:
        _log(prefix, unreadable_message(reads.unreadable))
        _log(prefix, summary_message(()))
        return TrainingRun(False, reads.unreadable, (), journal)
    ctx = context_from(reads, now())
    plan = plan_training(ctx, strategy)
    pairs = pair_plan(ctx.crew, plan)  # before any action: a plan of another shape stops the run here
    cache = CachingSnapshotProvider(ClientSnapshotProvider(
        CrewSnapshotClient(client, read_items), account_alias, clock=now, source=SNAPSHOT_SOURCE),
        ("crew", "inventory") if read_items else ("crew",))
    proxy = TrainingClient(client, cache.invalidate)
    journal = MemoryJournal() if journal is None else journal
    guarded: Any = GuardedJournal(journal, lambda message: _log(prefix, message))  # the runner needs append and read
    runner = ActionRunner(account_alias, proxy, cache, guarded, clock=now)
    run = _Run(ctx, reads.payloads, strategy, runner, proxy, cache, prefix, now,
               {design.id: design.name for design in ctx.designs}, journal)
    # The stock a finished crew's replan may use: what every final (non-finished) decision left of the ship's items.
    finished = [item.crew.character_id for item in pairs if item.finish is not None]
    stock = remaining_stock(ctx.items, plan, exclude_character_ids=finished)
    outcomes = []
    for item in pairs:
        outcome, stock = _train(run, item, stock)
        outcomes.append(outcome)
    _log(prefix, summary_message(outcomes))
    return TrainingRun(True, (), tuple(outcomes), journal)
