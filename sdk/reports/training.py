"""Training outcome report over gameplay journals and snapshots.

``build_training_report(journal_entries, snapshots=None, now=...)`` summarises
crew training per account and for the fleet: training attempts by outcome,
finishes, dry runs, skip reasons, consumables used and, from snapshots, training
points gained, crew at their training cap and idle crew-hours in training
rooms. See docs/baseline/training-report.md for the full contract.

Journal records read:

- Training actions: events whose ``action_id`` is in ``TRAINING_ACTIONS``.
  ``add_training`` carries params ``character_id`` and ``training_design_id``,
  ``finish_training`` carries ``character_id``, ``use_consumable`` carries
  ``item_design_id`` and an optional positive ``quantity`` (default 1).
- Decision records (contract version ``CONTRACT_VERSION``, built with
  ``decision_evidence``): at most one per crew member per run, with
  ``action_id="training_decision"``, ``outcome="skipped"``, ``attempted=False``
  and evidence ``{"kind": "training_decision", "v": 1, "decision":
  "start"|"skip", "reason_code": <a SKIP_REASON_CODES code, skips only>,
  "character_id": ..., "in_training_room": bool, "training_active":
  bool|null}``. Room ids never go in the evidence. The state fields describe
  the crew member at the record's ``finished_at``; ``training_active`` is true
  only while ``training_end`` is after that moment, so finished but
  uncollected training is not active. A record without ``v`` is a
  ``bad_version`` violation but is still read; one with another ``v`` is a
  violation and is not read (an unknown future schema). A decision is an
  observation only when ``character_id`` is set and ``in_training_room`` is a
  real bool; dry-run decisions count. A verified, attempted ``add_training``
  with a ``character_id`` is an observation of training.

Each breach of the contract is counted under one of ``CONTRACT_VIOLATIONS``
(``parse_decision``, ``validate_decision_evidence``); one record can break
several rules. Runner skip events on training actions are separate signals
from decision skips, so one crew member can show in the skip histogram twice.

Snapshots are read duck-typed (``account_alias``, ``observed_at``, ``crew``,
``room(room_id)``); crew training points come from the optional
``training_points`` and ``training_capacity`` attributes. A crew member's
training is active only while ``training_end`` is after the snapshot's
``observed_at`` (``snapshot_training_active``), as for decision records. An
empty ``snapshots`` is not provided. Every timestamp must be timezone-aware
(``TrainingReportError`` otherwise).

Output never names room ids or character ids (crew show as ``crew_label``
pseudonyms) and never repeats a record's free-text ``reason``; the rendered
text is passed through ``sdk.redaction.redact_secrets``.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, astuple, dataclass, field, fields
from datetime import datetime, timezone
from typing import Iterable, Mapping, Optional

from sdk.journal import GameplayEvent
from sdk.redaction import redact_secrets

TRAINING_ACTIONS = ("add_training", "finish_training", "use_consumable")
TRAINING_DECISION_ACTION = "training_decision"
DECISION_KIND = "training_decision"
SKIP_REASON_CODES = ("complete", "design_missing", "fatigued", "not_in_training_room", "research_missing",
                     "tp_cap_reached", "training_in_progress", "unknown_role")
PRECONDITION_FAILED = "precondition_failed"
EXECUTE_GATE_REFUSED = "execute_gate_refused"
UNCERTAIN_PRIOR_ATTEMPT = "uncertain_prior_attempt"
OTHER = "other"
UNKNOWN_REASON_CODE = "unknown_reason_code"
CAPPED_REASON_CODES = ("complete", "tp_cap_reached")
CONTRACT_VERSION = 1
DECISIONS = ("start", "skip")
CONTRACT_VIOLATIONS = ("missing_kind", "bad_version", "bad_decision", "missing_character_id", "non_bool_in_room",
                       "bad_training_active", UNKNOWN_REASON_CODE, "reason_code_on_start", "decision_attempted",
                       "action_missing_param")
ACTION_REQUIRED_PARAM = {"add_training": "character_id", "finish_training": "character_id",
                         "use_consumable": "item_design_id"}
KNOWN_OUTCOMES = ("verified", "not-observed", "contradicted")
DEFAULT_MAX_GAP_HOURS = 26.0
TP_ATTRIBUTE = "training_points"
TP_CAP_ATTRIBUTE = "training_capacity"


class TrainingReportError(ValueError):
    """Raised for invalid report input."""


@dataclass(frozen=True)
class TrainingObservation:
    """One sighting of a crew member: in a training room or not, and whether training ran (None: unknown)."""

    account_alias: str
    character_id: str
    observed_at: datetime
    in_training_room: bool
    training_active: Optional[bool]
    reason_code: Optional[str] = None  # a SKIP_REASON_CODES code from a skip decision, else None

    @property
    def idle(self) -> bool:
        return self.in_training_room and self.training_active is False


@dataclass(frozen=True)
class Decision:
    """A parsed decision record. A field the record got wrong is None (see ``parse_decision``)."""

    decision: Optional[str]
    character_id: Optional[str]
    in_training_room: Optional[bool]
    training_active: Optional[bool]
    reason_code: Optional[str]


@dataclass(frozen=True)
class CrewIdle:
    """Idle time of one crew member. ``known_observations`` counts its observations with a bool
    ``training_active``; with none, its idle figures are unknown (``n/a``), not zero."""

    idle_seconds: float
    streak_seconds: float
    streak_start: Optional[datetime]
    last_observed_at: datetime
    known_observations: int
    last_reason_code: Optional[str] = None


@dataclass(frozen=True)
class OutcomeCounts:
    attempted: int = 0
    verified: int = 0
    not_observed: int = 0
    contradicted: int = 0
    unknown: int = 0
    failed: int = 0
    failed_or_unknown: int = 0


@dataclass(frozen=True)
class CrewTraining:
    crew: str
    tp_gained: Optional[int]
    idle_hours: Optional[float]
    idle_streak_hours: Optional[float]
    at_tp_cap: Optional[bool]


@dataclass(frozen=True)
class TrainingTotals:
    outcomes: OutcomeCounts
    by_action: Mapping[str, OutcomeCounts]
    failed_or_unknown_rate: Optional[float]
    finishes: int
    dry_runs: int
    skip_reasons: Mapping[str, int] = field(default_factory=dict)
    consumables_used: Mapping[str, int] = field(default_factory=dict)
    contract_violations: Mapping[str, int] = field(default_factory=dict)
    tp_gained: Optional[int] = None
    idle_crew_hours: Optional[float] = None
    crew_at_tp_cap: Optional[int] = None


@dataclass(frozen=True)
class AccountTraining:
    account_alias: str
    window_start: Optional[str]
    window_end: Optional[str]
    records: int
    observations: int
    snapshots: int
    totals: TrainingTotals
    crew: tuple[CrewTraining, ...] = ()


@dataclass(frozen=True)
class TrainingReport:
    generated_at: str
    duplicates_dropped: int
    accounts: tuple[AccountTraining, ...]
    fleet: TrainingTotals
    snapshots_provided: bool = True
    # Whether any snapshot crew member up to ``generated_at`` carried a known ``training_points`` value.
    snapshot_training_points: bool = False
    training_room_design_ids_provided: bool = False


def _aware(moment: datetime, what: str) -> datetime:
    if moment.tzinfo is None:
        raise TrainingReportError(f"{what} must be timezone-aware")
    return moment


def _ts(text: str) -> datetime:
    return _aware(datetime.fromisoformat(text), "finished_at")


def is_unknown_outcome(event: GameplayEvent) -> bool:
    """True when an attempt's outcome is none of ``KNOWN_OUTCOMES`` (``unknown`` or anything unexpected)."""
    return event.outcome not in KNOWN_OUTCOMES


def is_failed_or_unknown(event: GameplayEvent) -> bool:
    """True when an attempt recorded an error class or has an unknown outcome (shared with the alert rules)."""
    return bool(event.error_class) or is_unknown_outcome(event)


def decision_evidence(*, character_id, decision: str, in_training_room: bool, training_active: Optional[bool],
                      reason_code: Optional[str] = None) -> dict:
    """The evidence of a version-``CONTRACT_VERSION`` decision record, for producers. It does not check the
    values: run ``validate_decision_evidence`` on it in tests."""
    evidence = {"kind": DECISION_KIND, "v": CONTRACT_VERSION, "decision": decision, "character_id": character_id,
                "in_training_room": in_training_room, "training_active": training_active}
    if reason_code is not None:
        evidence["reason_code"] = reason_code
    return evidence


def _parse_evidence(evidence) -> tuple[Optional[Decision], tuple[str, ...]]:
    if not isinstance(evidence, Mapping) or evidence.get("kind") != DECISION_KIND:
        return None, ("missing_kind",)
    violations = []
    if "v" not in evidence:
        violations.append("bad_version")
    elif type(evidence["v"]) is not int or evidence["v"] != CONTRACT_VERSION:
        return None, ("bad_version",)
    decision = evidence.get("decision")
    if decision not in DECISIONS:
        violations.append("bad_decision")
        decision = None
    character_id = evidence.get("character_id")
    if not character_id:
        violations.append("missing_character_id")
    in_room = evidence.get("in_training_room")
    if not isinstance(in_room, bool):
        violations.append("non_bool_in_room")
    active = evidence.get("training_active")
    if active is not None and not isinstance(active, bool):
        violations.append("bad_training_active")
    code = evidence.get("reason_code")
    if decision == "skip" and code not in SKIP_REASON_CODES:
        violations.append(UNKNOWN_REASON_CODE)
    if decision == "start" and code is not None:
        violations.append("reason_code_on_start")
    return Decision(
        decision=decision,
        character_id=str(character_id) if character_id else None,
        in_training_room=in_room if isinstance(in_room, bool) else None,
        training_active=active if isinstance(active, bool) else None,
        reason_code=code if decision == "skip" and code in SKIP_REASON_CODES else None,
    ), tuple(violations)


def validate_decision_evidence(evidence) -> tuple[str, ...]:
    """The ``CONTRACT_VIOLATIONS`` codes a decision record's evidence breaks, in that order; empty when valid."""
    return _parse_evidence(evidence)[1]


def parse_decision(event: GameplayEvent) -> tuple[Optional[Decision], tuple[str, ...]]:
    """Parse a ``training_decision`` record: the decision (None when the kind is wrong or the version is not
    ``CONTRACT_VERSION``; a missing version is tolerated) and the contract violations. ``(None, ())`` for any
    other action."""
    if event.action_id != TRAINING_DECISION_ACTION:
        return None, ()
    decision, violations = _parse_evidence(event.evidence)
    if event.attempted:
        violations += ("decision_attempted",)
    return decision, violations


def contract_violations(event: GameplayEvent) -> tuple[str, ...]:
    """The contract violations of a decision record or an attempted training action."""
    required = ACTION_REQUIRED_PARAM.get(event.action_id)
    if required is not None:
        return ("action_missing_param",) if event.attempted and not event.params.get(required) else ()
    return parse_decision(event)[1]


def skip_reason(event: GameplayEvent) -> Optional[str]:
    """The skip-reason code of a training skip, or None when ``event`` is not one."""
    if event.action_id == TRAINING_DECISION_ACTION:
        decision = parse_decision(event)[0]
        if decision is None or decision.decision != "skip":
            return None
        return decision.reason_code or UNKNOWN_REASON_CODE
    if event.action_id not in TRAINING_ACTIONS or event.attempted or event.dry_run:
        return None
    if event.reason.startswith("preconditions failed"):
        return PRECONDITION_FAILED
    if event.reason.startswith("execute gate refused"):
        return EXECUTE_GATE_REFUSED
    if event.error_class == "UncertainPriorAttemptError":
        return UNCERTAIN_PRIOR_ATTEMPT
    return OTHER


def observations_from_journal(events: Iterable[GameplayEvent], alias: str) -> list[TrainingObservation]:
    """Observations from ``alias``'s decision records and verified ``add_training`` attempts."""
    observations = []
    for event in events:
        if event.account_alias != alias:
            continue
        decision = parse_decision(event)[0]
        if decision is not None:
            if decision.character_id is not None and decision.in_training_room is not None:
                observations.append(TrainingObservation(alias, decision.character_id, _ts(event.finished_at),
                                                        decision.in_training_room, decision.training_active,
                                                        decision.reason_code))
        elif (event.action_id == "add_training" and event.attempted and event.outcome == "verified"
              and event.params.get("character_id")):
            observations.append(TrainingObservation(alias, str(event.params["character_id"]),
                                                    _ts(event.finished_at), True, True))
    return observations


def _training_end(value) -> Optional[datetime]:
    """A crew member's ``training_end`` as an aware datetime, or None when it cannot be read. The game server
    writes naive UTC times, so a naive value is read as UTC."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def snapshot_training_active(member, observed_at: datetime) -> Optional[bool]:
    """Whether ``member`` was training at ``observed_at``: None when training was not observed or its end cannot
    be read, False when no training is set, else whether ``training_end`` is after ``observed_at``. Finished but
    uncollected training is not active."""
    active = member.training_active
    if not active:
        return active
    end = _training_end(getattr(member, "training_end", None))
    return None if end is None else end > observed_at


def observations_from_snapshots(snapshots: Iterable, *, training_room_design_ids) -> list[TrainingObservation]:
    """One observation per crew member per snapshot. A crew member with no room is out of a training room; one
    whose room is not in the snapshot is not observed. Training state comes from ``snapshot_training_active``."""
    designs = {str(design_id) for design_id in training_room_design_ids}
    observations = []
    for snapshot in snapshots:
        for member in snapshot.crew:
            if member.room_id is None:
                in_room = False
            else:
                room = snapshot.room(member.room_id)
                if room is None:
                    continue
                in_room = str(room.design_id) in designs
            observations.append(TrainingObservation(snapshot.account_alias, member.character_id,
                                                    snapshot.observed_at, in_room,
                                                    snapshot_training_active(member, snapshot.observed_at)))
    return observations


def crew_idle(observations: Iterable[TrainingObservation], *, now: datetime,
              max_gap_hours: float = DEFAULT_MAX_GAP_HOURS) -> dict[tuple[str, str], CrewIdle]:
    """Idle time in a training room per ``(account_alias, character_id)``.

    An interval between two consecutive observations counts only when both
    are idle and it is at most ``max_gap_hours`` long, so idle time is an upper
    bound: training could have run between the two. Observations at the same
    moment are idle only if all of them are. ``last_reason_code`` is the
    alphabetically first reason code at the last observed moment, or None.
    """
    cap = max_gap_hours * 3600
    moments: dict[tuple[str, str], dict[datetime, bool]] = {}
    known: Counter = Counter()
    codes: dict[tuple[str, str, datetime], set[str]] = {}
    for obs in observations:
        if _aware(obs.observed_at, "observed_at") <= now:
            key = (obs.account_alias, obs.character_id)
            crew = moments.setdefault(key, {})
            crew[obs.observed_at] = crew.get(obs.observed_at, True) and obs.idle
            known[key] += obs.training_active is not None
            if obs.reason_code is not None:
                codes.setdefault((*key, obs.observed_at), set()).add(obs.reason_code)
    result = {}
    for key, crew in moments.items():
        idle_seconds, streak_start, previous = 0.0, None, None
        for moment in sorted(crew):
            idle = crew[moment]
            gap = (moment - previous).total_seconds() if previous else 0.0
            if idle and previous and crew[previous] and gap <= cap:
                idle_seconds += gap
            if not idle:
                streak_start = None
            elif streak_start is None or gap > cap:
                streak_start = moment
            previous = moment
        streak = (previous - streak_start).total_seconds() if streak_start else 0.0
        last_codes = codes.get((*key, previous))
        result[key] = CrewIdle(idle_seconds, streak, streak_start, previous, known[key],
                               min(last_codes) if last_codes else None)
    return result


def crew_label(account_alias: str, character_id) -> str:
    """A stable pseudonym for a crew member: ``crew-`` plus the first 8 hex characters of
    ``sha256("<alias>:<character id>")``. It hides the id from a casual reader but is not a secret: anyone who
    knows the alias can test candidate ids against it."""
    return "crew-" + hashlib.sha256(f"{account_alias}:{character_id}".encode("utf-8")).hexdigest()[:8]


def _hours(seconds: float) -> float:
    return round(seconds / 3600, 4)


def _known(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_sum(values: Iterable) -> Optional[float]:
    known = [value for value in values if value is not None]
    return round(sum(known), 4) if known else None


def _training_points(snapshots: list) -> tuple[dict[str, list[int]], dict[str, bool]]:
    """Known training points per crew member in time order, and whether each was at its cap in the most recent
    snapshot that showed both values."""
    points: dict[str, list[int]] = {}
    at_cap: dict[str, bool] = {}
    for snapshot in snapshots:
        for member in snapshot.crew:
            tp = _known(getattr(member, TP_ATTRIBUTE, None))
            cap = _known(getattr(member, TP_CAP_ATTRIBUTE, None))
            if tp is None:
                continue
            points.setdefault(member.character_id, []).append(tp)
            if cap is not None:
                at_cap[member.character_id] = tp >= cap
    return points, at_cap


def _gained(values: list[int]) -> Optional[int]:
    return sum(max(0, after - before) for before, after in zip(values, values[1:])) if len(values) > 1 else None


def _outcomes(events: list[GameplayEvent]) -> OutcomeCounts:
    attempted = [e for e in events if e.attempted]

    def count(predicate) -> int:
        return sum(1 for e in attempted if predicate(e))

    return OutcomeCounts(
        attempted=len(attempted),
        verified=count(lambda e: e.outcome == "verified"),
        not_observed=count(lambda e: e.outcome == "not-observed"),
        contradicted=count(lambda e: e.outcome == "contradicted"),
        unknown=count(is_unknown_outcome),
        failed=count(lambda e: bool(e.error_class)),
        failed_or_unknown=count(is_failed_or_unknown),
    )


def _sum_counts(items: Iterable[OutcomeCounts]) -> OutcomeCounts:
    items = list(items)
    return OutcomeCounts(**{f.name: sum(getattr(item, f.name) for item in items) for f in fields(OutcomeCounts)})


def _quantity(value) -> int:
    return int(value) if str(value).isdecimal() and int(value) > 0 else 1


def _totals(by_action: Mapping[str, OutcomeCounts], **rest) -> TrainingTotals:
    outcomes = _sum_counts(by_action.values())
    rate = round(outcomes.failed_or_unknown / outcomes.attempted, 4) if outcomes.attempted else None
    return TrainingTotals(outcomes=outcomes, by_action=by_action, failed_or_unknown_rate=rate, **rest)


def _account_totals(training: list[GameplayEvent], crew: tuple[CrewTraining, ...],
                    idle_seconds: Optional[float]) -> TrainingTotals:
    by_action = {}
    for action in TRAINING_ACTIONS:
        events = [e for e in training if e.action_id == action]
        if events:
            by_action[action] = _outcomes(events)
    consumables: Counter = Counter()
    for e in training:
        if e.action_id == "use_consumable" and e.attempted and e.outcome == "verified":
            consumables[str(e.params.get("item_design_id") or "unknown")] += _quantity(e.params.get("quantity"))
    skips = Counter(code for code in map(skip_reason, training) if code)
    violations = Counter(code for e in training for code in contract_violations(e))
    return _totals(
        by_action,
        finishes=sum(1 for e in training
                     if e.action_id == "finish_training" and e.attempted and e.outcome == "verified"),
        dry_runs=sum(1 for e in training if e.action_id in TRAINING_ACTIONS and not e.attempted and e.dry_run),
        skip_reasons=dict(sorted(skips.items())),
        consumables_used=dict(sorted(consumables.items())),
        contract_violations=dict(sorted(violations.items())),
        tp_gained=_optional_sum(row.tp_gained for row in crew),
        idle_crew_hours=None if idle_seconds is None else _hours(idle_seconds),
        crew_at_tp_cap=_optional_sum(row.at_tp_cap for row in crew),
    )


def _fleet_totals(totals: list[TrainingTotals]) -> TrainingTotals:
    by_action = {}
    for action in TRAINING_ACTIONS:
        counts = [row.by_action[action] for row in totals if action in row.by_action]
        if counts:
            by_action[action] = _sum_counts(counts)
    skips: Counter = Counter()
    consumables: Counter = Counter()
    violations: Counter = Counter()
    for row in totals:
        skips.update(row.skip_reasons)
        consumables.update(row.consumables_used)
        violations.update(row.contract_violations)
    return _totals(
        by_action,
        finishes=sum(row.finishes for row in totals),
        dry_runs=sum(row.dry_runs for row in totals),
        skip_reasons=dict(sorted(skips.items())),
        consumables_used=dict(sorted(consumables.items())),
        contract_violations=dict(sorted(violations.items())),
        tp_gained=_optional_sum(row.tp_gained for row in totals),
        idle_crew_hours=_optional_sum(row.idle_crew_hours for row in totals),
        crew_at_tp_cap=_optional_sum(row.crew_at_tp_cap for row in totals),
    )


def build_training_report(journal_entries: Iterable[GameplayEvent], snapshots=None, *, now: datetime,
                          training_room_design_ids=None,
                          max_gap_hours: float = DEFAULT_MAX_GAP_HOURS) -> TrainingReport:
    """Build the report at ``now`` (timezone-aware). An empty ``snapshots`` is the same as None: not provided."""
    if now.tzinfo is None:
        raise TrainingReportError("now must be timezone-aware")
    snapshots = list(snapshots or ())
    by_alias: dict[str, list[GameplayEvent]] = {}
    seen: set[tuple[str, str]] = set()
    dropped = 0
    for event in journal_entries:
        if _ts(event.finished_at) > now:
            continue
        key = (event.account_alias, event.attempt_id)
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        by_alias.setdefault(event.account_alias, []).append(event)

    snapshots_by_alias: dict[str, list] = {}
    for snapshot in snapshots:
        if _aware(snapshot.observed_at, "observed_at") <= now:
            snapshots_by_alias.setdefault(snapshot.account_alias, []).append(snapshot)

    accounts = []
    any_points = False
    for alias in sorted(set(by_alias) | set(snapshots_by_alias)):
        events = by_alias.get(alias, [])
        account_snapshots = sorted(snapshots_by_alias.get(alias, []), key=lambda snapshot: snapshot.observed_at)
        training = [e for e in events if e.action_id in TRAINING_ACTIONS or e.action_id == TRAINING_DECISION_ACTION]
        times = sorted(_ts(e.finished_at) for e in training)
        observations = observations_from_journal(events, alias)
        if training_room_design_ids:
            observations += observations_from_snapshots(account_snapshots,
                                                        training_room_design_ids=training_room_design_ids)
        observed = crew_idle(observations, now=now, max_gap_hours=max_gap_hours)
        idle = {cid: value for (_, cid), value in observed.items() if value.known_observations}
        points, at_cap = _training_points(account_snapshots)
        any_points = any_points or bool(points)
        labels = {cid: crew_label(alias, cid) for cid in {cid for _, cid in observed} | set(points)}
        crew = tuple(
            CrewTraining(
                crew=labels[cid],
                tp_gained=_gained(points.get(cid, [])),
                idle_hours=_hours(idle[cid].idle_seconds) if cid in idle else None,
                idle_streak_hours=_hours(idle[cid].streak_seconds) if cid in idle else None,
                at_tp_cap=at_cap.get(cid),
            )
            for cid in sorted(labels, key=lambda cid: (labels[cid], cid))
        )
        accounts.append(AccountTraining(
            account_alias=alias,
            window_start=times[0].isoformat() if times else None,
            window_end=times[-1].isoformat() if times else None,
            records=len(training), observations=len(observations), snapshots=len(account_snapshots),
            totals=_account_totals(training, crew, _optional_sum(value.idle_seconds for value in idle.values())),
            crew=crew,
        ))
    return TrainingReport(generated_at=now.isoformat(), duplicates_dropped=dropped, accounts=tuple(accounts),
                          fleet=_fleet_totals([row.totals for row in accounts]),
                          snapshots_provided=bool(snapshots), snapshot_training_points=any_points,
                          training_room_design_ids_provided=bool(training_room_design_ids))


ACTION_HEADER = ("Action", "Attempted", "Verified", "Not observed", "Contradicted", "Unknown", "Failed",
                 "Failed or unknown")
CREW_HEADER = ("Crew", "TP gained", "Idle hours", "Idle streak hours", "At TP cap")
NO_SNAPSHOTS_LINE = "Snapshots not provided: TP gained and crew at TP cap are n/a."
NO_TRAINING_POINTS_LINE = "Snapshot crew carry no training points: TP gained and crew at TP cap are n/a."
NO_ROOM_DESIGN_IDS_LINE = ("Snapshots provided without training room design ids: idle crew-hours use journal "
                           "records only.")


def _cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(header: tuple, rows: Iterable[tuple]) -> list[str]:
    return ["| " + " | ".join(_cell(value) for value in row) + " |"
            for row in (header, ("---",) * len(header), *rows)]


def _or_na(value, template: str = "{}") -> str:
    return "n/a" if value is None else template.format(value)


def _metric_table(totals: TrainingTotals) -> list[str]:
    o = totals.outcomes
    rate = totals.failed_or_unknown_rate
    starts = totals.by_action.get("add_training", OutcomeCounts())
    return _table(("Metric", "Value"), [
        ("Training actions attempted", o.attempted),
        ("Training starts attempted (add_training)", starts.attempted),
        ("Training starts verified (add_training)", starts.verified),
        ("Verified", o.verified),
        ("Not observed", o.not_observed),
        ("Contradicted", o.contradicted),
        ("Unknown", o.unknown),
        ("Failed (error class)", o.failed),
        ("Failed or unknown", _or_na(rate, f"{{:.0%}} ({o.failed_or_unknown}/{o.attempted})")),
        ("Finishes", totals.finishes),
        ("Dry runs", totals.dry_runs),
        ("Skips", ", ".join(f"{code} {n}" for code, n in sorted(totals.skip_reasons.items())) or "none"),
        ("Consumables used", ", ".join(f"{item} x{n}" for item, n in sorted(totals.consumables_used.items()))
         or "none"),
        ("Contract violations", ", ".join(f"{code} {n}" for code, n in sorted(totals.contract_violations.items()))
         or "none"),
        ("TP gained", _or_na(totals.tp_gained)),
        ("Idle crew-hours in training rooms", _or_na(totals.idle_crew_hours, "{:.2f}")),
        ("Crew at TP cap", _or_na(totals.crew_at_tp_cap)),
    ])


def _action_table(totals: TrainingTotals) -> list[str]:
    if not totals.by_action:
        return ["No training actions."]
    return _table(ACTION_HEADER, [(action, *astuple(counts)) for action, counts in sorted(totals.by_action.items())])


def _crew_table(crew: tuple[CrewTraining, ...]) -> list[str]:
    if not crew:
        return ["No crew observed."]
    return _table(CREW_HEADER, [
        (row.crew, _or_na(row.tp_gained), _or_na(row.idle_hours, "{:.2f}"), _or_na(row.idle_streak_hours, "{:.2f}"),
         "n/a" if row.at_tp_cap is None else ("yes" if row.at_tp_cap else "no"))
        for row in crew
    ])


def render_markdown(report: TrainingReport) -> str:
    lines = ["# Training report", "",
             f"Generated at {report.generated_at}. Accounts: {len(report.accounts)}. "
             f"Duplicate records dropped: {report.duplicates_dropped}.", ""]
    if not report.snapshots_provided:
        lines += [NO_SNAPSHOTS_LINE, ""]
    else:
        notes = []
        if not report.snapshot_training_points:
            notes.append(NO_TRAINING_POINTS_LINE)
        if not report.training_room_design_ids_provided:
            notes.append(NO_ROOM_DESIGN_IDS_LINE)
        if notes:
            lines += [*notes, ""]
    if not report.accounts:
        lines.append("No training records or snapshots.")
    else:
        lines += ["## Fleet", "", *_metric_table(report.fleet), "", *_action_table(report.fleet)]
    for row in report.accounts:
        records = (f"Training records: {row.records} ({row.window_start} to {row.window_end})." if row.records
                   else "No training records.")
        lines += ["", f"## {_cell(row.account_alias)}", "",
                  f"{records} Observations: {row.observations}. Snapshots: {row.snapshots}.", "",
                  *_metric_table(row.totals), "", *_action_table(row.totals), "", *_crew_table(row.crew)]
    return redact_secrets("\n".join(lines) + "\n")


to_markdown = render_markdown


def to_json(report: TrainingReport) -> str:
    return redact_secrets(json.dumps(asdict(report), indent=2, sort_keys=True))
