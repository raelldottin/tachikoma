"""training-v2's attempt store and per-identity account lock (SPEC-14 step 4, S14-R4 to S14-R6).

A training start is not idempotent, so ``--fleet-execute`` brackets it with a durable record:

1. after the account's identity is verified, the dispatch takes the account's non-blocking lock (``AccountLock``),
   keyed by that verified game identity, and holds it for the whole execute dispatch: planning, earlier progression
   actions, the start and its settlement. An overlapping dispatch for the same identity skips the account (``locked``)
   and sends nothing; separate accounts never contend;
2. under the lock, the account's store (``AttemptStore``) must be readable and belong to that identity. An attempt
   still ``pending`` or ``unresolved`` pauses every training start on the account (``unresolved_attempt``), whatever
   the crew member or course, until an operator records a reconciliation (``run.py --fleet-resolve-training``);
3. immediately before sending, the store is read again and the start is rechecked against a fresh account read;
   the intent is written atomically and durably (``pending``), and only then is the request sent. If the intent
   cannot be written, nothing is sent (``intent_not_persisted``);
4. after the outcome is journaled, the intent is settled: ``verified`` releases it, any other outcome keeps it
   blocking (``unresolved``). A settlement that cannot be written leaves it ``pending``, which blocks just the same.

The pure core (schema parsing, transitions, the block, resolution classification, the start recheck verdict and the
operator wording) takes explicit inputs and times. I/O lives in the two ports and their file implementations; the
in-memory implementations honour the same contracts for unit tests.

On disk, under ``<journal dir>/`` (directories 0700, files 0600):

- ``training-attempts/<alias>.json``: the account's store, one JSON object (``STORE_KIND``, version 1);
- ``training-attempts/<game identity>.lock``: the ``flock`` target. Each acquisition opens it anew, so two dispatches
  in one process (threads mode) contend exactly like two processes. ``flock`` needs a local file system;
- ``training-provisioned/<game identity>.json``: the establishment marker, written right after the store, outside the
  directory it protects, so a deleted store (or a deleted ``training-attempts/``) is told apart from one that never
  existed: established state is never recreated. The account journal is a second witness: once it records a start
  made under a training-v2 dry-run plan, a missing store is established too (``journal_witnesses_store``).

Nothing here retries, sleeps or blocks on the lock, and nothing catches ``BaseException``: a crashed process leaves
its intent ``pending`` and the operating system releases its lock.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Protocol

from sdk.execution.runner import COST_KINDS
from sdk.state import AccountSnapshot, Completeness
from sdk.state.snapshot import ACCOUNT_ALIAS_PATTERN, GAME_IDENTITY_PATTERN
from sdk.training.model import SESSION_ENDED_UNCLAIMED, SESSION_RUNNING, SESSION_UNKNOWN

STORE_DIR = "training-attempts"
MARKER_DIR = "training-provisioned"
# The journal evidence key a completed dry run's events carry their policy id in (sdk.fleet.factory.DRY_RUN_POLICY).
DRY_RUN_POLICY_KEY = "dry_run_policy"
STORE_KIND = "tachikoma.training-attempts"
MARKER_KIND = "tachikoma.training-attempts.marker"
STORE_VERSION = 1
STORE_SUFFIX, MARKER_SUFFIX, LOCK_SUFFIX = ".json", ".json", ".lock"

PENDING, VERIFIED, UNRESOLVED, RESOLVED = "pending", "verified", "unresolved", "resolved"
STATES = frozenset({PENDING, VERIFIED, UNRESOLVED, RESOLVED})
BLOCKING = frozenset({PENDING, UNRESOLVED})
GUARDED_ACTIONS = frozenset({"add_training"})

# Workflow skip codes this module emits (each operator text names exactly one of them, as a whole word).
LOCKED, UNRESOLVED_ATTEMPT, INTENT_NOT_PERSISTED = "locked", "unresolved_attempt", "intent_not_persisted"
STATE_MISSING, STATE_UNREADABLE, DRY_RUN_STALE = "state_missing", "state_unreadable", "dry_run_stale"
NOT_AUTHORIZED, LEGACY_RUN_NOT_SKIPPED, STOPPED = "not_authorized", "legacy_run_not_skipped", "stopped"
EXECUTION_SKIP_CODES = frozenset({LOCKED, UNRESOLVED_ATTEMPT, INTENT_NOT_PERSISTED, STATE_MISSING, STATE_UNREADABLE,
                                  DRY_RUN_STALE, NOT_AUTHORIZED, LEGACY_RUN_NOT_SKIPPED, STOPPED})

LOCKED_REASON = f"{LOCKED}: another dispatch holds this account's training lock; skipped without account writes"
UNKNOWN_IDENTITY_REASON = (f"{STATE_UNREADABLE}: the account's verified game identity is unknown, so its training "
                           "attempt store cannot be matched to it")
ONE_START_REASON = "training-v2 sends at most one training start per account per run; this start waits for a later run"
RECHECKED = "rechecked under the account lock before sending"
RESOLUTION_VERDICTS = ("started", "other_course", "not_running")


def guarded_training(policy: Any) -> bool:
    """Whether ``policy``'s training starts run under the attempt store and account lock (training-v2 sets
    ``holds_paid_training``; training-v1, fleet-v1 and baseline-v1 do not)."""
    return getattr(policy, "holds_paid_training", False) is True


class TrainingStateError(Exception):
    """The attempt store cannot be used; ``code`` is the workflow skip code."""

    code = ""


class StoreMissing(TrainingStateError):
    code = STATE_MISSING

    def __init__(self, detail: str, *, established: bool = False, journal_witness: bool = False) -> None:
        super().__init__(detail)
        self.established = established or journal_witness
        self.journal_witness = journal_witness


class StoreUnreadable(TrainingStateError):
    code = STATE_UNREADABLE

    def __init__(self, detail: str, *, other_identity: bool = False) -> None:
        super().__init__(detail)
        self.other_identity = other_identity


class AccountLocked(TrainingStateError):
    code = LOCKED


class ResolutionRefused(Exception):
    """No authoritative evidence for a reconciliation: nothing was resolved."""


def _check_alias(alias: Any) -> str:
    if not isinstance(alias, str) or not ACCOUNT_ALIAS_PATTERN.match(alias):
        raise ValueError("an account alias is lowercase letters, digits, '_' or '-'")
    return alias


def _check_identity(identity: Any) -> str:
    if not isinstance(identity, str) or not GAME_IDENTITY_PATTERN.match(identity):
        raise ValueError("the account lock needs a verified gid1- game identity")
    return identity


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("times in the attempt store are timezone-aware")
    return moment.isoformat()


# --- pure core -------------------------------------------------------------------------------------------------------

ATTEMPT_KEYS = ("intent_id", "action_id", "character_id", "training_design_id", "planned_cost", "state",
                "written_at", "settled_at", "outcome", "reason", "journal_attempt_id", "resolution")


@dataclass(frozen=True)
class Attempt:
    intent_id: str
    action_id: str
    character_id: str
    training_design_id: str
    planned_cost: Mapping[str, int]
    state: str
    written_at: str
    settled_at: Optional[str] = None
    outcome: Optional[str] = None
    reason: Optional[str] = None
    journal_attempt_id: Optional[str] = None
    resolution: Optional[Mapping[str, Any]] = None

    def to_dict(self) -> dict:
        return {"intent_id": self.intent_id, "action_id": self.action_id, "character_id": self.character_id,
                "training_design_id": self.training_design_id, "planned_cost": dict(self.planned_cost),
                "state": self.state, "written_at": self.written_at, "settled_at": self.settled_at,
                "outcome": self.outcome, "reason": self.reason, "journal_attempt_id": self.journal_attempt_id,
                "resolution": dict(self.resolution) if self.resolution is not None else None}

    @classmethod
    def from_dict(cls, data: Any) -> "Attempt":
        """Strict: exactly ``ATTEMPT_KEYS``, each of its type (ValueError or TypeError otherwise)."""
        if not isinstance(data, dict) or set(data) != set(ATTEMPT_KEYS):
            raise ValueError("an attempt has exactly the schema's keys")
        for key in ("intent_id", "action_id", "character_id", "training_design_id", "state", "written_at"):
            if not isinstance(data[key], str) or not data[key]:
                raise TypeError(f"attempt {key} is a non-empty string")
        for key in ("settled_at", "outcome", "reason", "journal_attempt_id"):
            if data[key] is not None and not isinstance(data[key], str):
                raise TypeError(f"attempt {key} is a string or null")
        if data["state"] not in STATES:
            raise ValueError("unknown attempt state")
        if data["action_id"] not in GUARDED_ACTIONS:
            raise ValueError("unknown attempt action")
        cost = data["planned_cost"]
        if not isinstance(cost, dict) or any(not isinstance(kind, str) or isinstance(amount, bool)
                                             or not isinstance(amount, int) for kind, amount in cost.items()):
            raise TypeError("attempt planned_cost maps resource names to integers")
        if data["resolution"] is not None and not isinstance(data["resolution"], dict):
            raise TypeError("attempt resolution is an object or null")
        if (data["state"] == RESOLVED) != (data["resolution"] is not None):
            raise ValueError("a resolution is recorded when, and only when, the attempt is resolved")
        return cls(**{key: data[key] for key in ATTEMPT_KEYS})


@dataclass(frozen=True)
class StoreState:
    alias: str
    game_identity: str
    provisioned_at: str
    attempts: tuple[Attempt, ...] = field(default_factory=tuple)

    def blocking(self) -> Optional[Attempt]:
        """The first attempt still pending or unresolved, or ``None``."""
        return next((attempt for attempt in self.attempts if attempt.state in BLOCKING), None)

    def to_dict(self) -> dict:
        return {"kind": STORE_KIND, "version": STORE_VERSION, "alias": self.alias,
                "game_identity": self.game_identity, "provisioned_at": self.provisioned_at,
                "attempts": [attempt.to_dict() for attempt in self.attempts]}


def parse_store(raw: bytes, alias: str) -> StoreState:
    """The store of ``alias`` from its bytes; ``StoreUnreadable`` (naming only the error class) for anything else."""
    try:
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"kind", "version", "alias", "game_identity",
                                                             "provisioned_at", "attempts"}:
            raise ValueError("not a training attempt store")
        version = payload["version"]
        if payload["kind"] != STORE_KIND or isinstance(version, bool) or version != STORE_VERSION:
            raise ValueError("not a version 1 training attempt store")
        if payload["alias"] != alias:
            raise ValueError("the store names another alias")
        identity = payload["game_identity"]
        if not isinstance(identity, str) or not GAME_IDENTITY_PATTERN.match(identity):
            raise ValueError("the store names no game identity")
        if not isinstance(payload["provisioned_at"], str) or not isinstance(payload["attempts"], list):
            raise TypeError("provisioned_at is a string and attempts a list")
        datetime.fromisoformat(payload["provisioned_at"])
        return StoreState(alias, identity, payload["provisioned_at"],
                          tuple(Attempt.from_dict(item) for item in payload["attempts"]))
    except (UnicodeDecodeError, ValueError, TypeError, KeyError) as error:
        raise StoreUnreadable(type(error).__name__) from None


def dump_store(state: StoreState) -> bytes:
    return (json.dumps(state.to_dict(), sort_keys=True, indent=1) + "\n").encode("utf-8")


def virgin_store(alias: str, identity: str, now: datetime) -> StoreState:
    return StoreState(_check_alias(alias), _check_identity(identity), _iso(now), ())


def identity_refusal(state: StoreState, identity: Optional[str]) -> Optional[str]:
    """Why ``state`` may not be used for the verified ``identity``, or ``None``."""
    if not isinstance(identity, str) or not GAME_IDENTITY_PATTERN.match(identity):
        return UNKNOWN_IDENTITY_REASON
    if state.game_identity != identity:
        return f"{STATE_UNREADABLE}: the training attempt store belongs to another game identity"
    return None


def block_reason(state: StoreState) -> Optional[str]:
    """Why every training start on the account is paused (``unresolved_attempt``), or ``None``."""
    attempt = state.blocking()
    if attempt is None:
        return None
    return (f"{UNRESOLVED_ATTEMPT}: training attempt {attempt.intent_id[:8]} (character {attempt.character_id}, "
            f"course {attempt.training_design_id}) is {attempt.state}; record a reconciliation with "
            "--fleet-resolve-training before another training start")


def new_intent(intent_id: str, params: Mapping[str, Any], planned: Mapping[str, int], now: datetime) -> Attempt:
    return Attempt(intent_id, "add_training", str(params["character_id"]), str(params["training_design_id"]),
                   dict(planned), PENDING, _iso(now))


def with_intent(state: StoreState, attempt: Attempt) -> StoreState:
    """``state`` with a new pending intent; refused while another attempt blocks or the id is taken."""
    if attempt.state != PENDING:
        raise ValueError("a new intent is pending")
    if state.blocking() is not None:
        raise ValueError("another training attempt is still pending or unresolved")
    if any(existing.intent_id == attempt.intent_id for existing in state.attempts):
        raise ValueError("the intent id is already recorded")
    return replace(state, attempts=state.attempts + (attempt,))


def _replace_attempt(state: StoreState, intent_id: str, update: Callable[[Attempt], Attempt]) -> StoreState:
    for index, attempt in enumerate(state.attempts):
        if attempt.intent_id == intent_id:
            return replace(state, attempts=state.attempts[:index] + (update(attempt),) + state.attempts[index + 1:])
    raise ValueError(f"no training attempt {intent_id[:8]} in the store")


def settled(state: StoreState, intent_id: str, settlement: Mapping[str, Any]) -> StoreState:
    """Settle a pending intent: outcome ``verified`` -> verified, any other outcome -> unresolved (still blocking)."""
    def settle(attempt: Attempt) -> Attempt:
        if attempt.state != PENDING:
            raise ValueError(f"training attempt {intent_id[:8]} is {attempt.state}, not pending")
        outcome = settlement.get("outcome")
        return replace(attempt, state=VERIFIED if outcome == "verified" else UNRESOLVED,
                       settled_at=settlement.get("settled_at"), outcome=outcome, reason=settlement.get("reason"),
                       journal_attempt_id=settlement.get("journal_attempt_id"))
    return _replace_attempt(state, intent_id, settle)


def _moment(text: Any) -> Optional[datetime]:
    try:
        moment = datetime.fromisoformat(text) if isinstance(text, str) else None
    except ValueError:
        return None
    return moment if moment is not None and moment.tzinfo is not None else None


def journal_attempt_link(attempt: Attempt, events: Iterable[Any], until: Optional[datetime] = None
                         ) -> Optional[str]:
    """The journal attempt id of ``attempt``'s request, or ``None``.

    The settlement records it; when the settlement was never written (its write failed, or the process died between
    the journal append and the settlement), it is the first attempted journal event of the same action, crew member
    and course that finished at or after the intent was written (and at or before ``until``, the resolution time).
    The attempt blocked every other start of the account until it was resolved, so no other request falls in there.
    """
    if attempt.journal_attempt_id:
        return attempt.journal_attempt_id
    written = _moment(attempt.written_at)
    if written is None:
        return None
    for event in events:
        params = getattr(event, "params", None) or {}
        if (getattr(event, "action_id", None) != attempt.action_id or not getattr(event, "attempted", False)
                or str(params.get("character_id")) != attempt.character_id
                or str(params.get("training_design_id")) != attempt.training_design_id):
            continue
        finished = _moment(getattr(event, "finished_at", None))
        if finished is not None and finished >= written and (until is None or finished <= until):
            return getattr(event, "attempt_id", None) or None
    return None


def reconciled_attempt_ids(state: StoreState, events: Iterable[Any] = ()) -> frozenset[str]:
    """Journal attempt ids an operator's reconciliation covers: those of the resolved attempts (linked through
    ``journal_attempt_link`` when no settlement recorded the id). Their journaled uncertainty no longer blocks the
    same crew member and course; the journal itself is never changed, so budgets still charge their uncertain spend.
    """
    events = tuple(events)
    ids = set()
    for attempt in state.attempts:
        if attempt.state != RESOLVED:
            continue
        resolved_at = _moment((attempt.resolution or {}).get("resolved_at"))
        link = journal_attempt_link(attempt, events, resolved_at)
        if link:
            ids.add(link)
    return frozenset(ids)


def journal_witnesses_store(events: Iterable[Any], alias: str, policy_id: str) -> bool:
    """Whether ``alias``'s journal shows its training attempt store was established: an attempted training start
    after a completed dry run of ``policy_id`` (execute starts only from such a plan, under the store). A start the
    journal records only under another policy's plans (training-v1 before the switch) is no witness."""
    planned = False
    for event in events:
        if getattr(event, "account_alias", None) != alias:
            continue
        evidence = getattr(event, "evidence", None) or {}
        if getattr(event, "dry_run", False) and evidence.get(DRY_RUN_POLICY_KEY) == policy_id:
            planned = True
        elif planned and getattr(event, "attempted", False) and getattr(event, "action_id", None) in GUARDED_ACTIONS:
            return True
    return False


def resolved(state: StoreState, resolutions: Mapping[str, Mapping[str, Any]], now: datetime,
             events: Iterable[Any] = ()) -> StoreState:
    """Record an operator's reconciliation (authoritative evidence) for each blocking attempt named.

    An attempt whose settlement was never written gets its journal attempt id from ``events`` (the account's
    journal, ``journal_attempt_link``), so its journaled uncertainty is lifted too.
    """
    events = tuple(events)
    for intent_id, evidence in resolutions.items():
        def resolve(attempt: Attempt, evidence=evidence) -> Attempt:
            if attempt.state not in BLOCKING:
                raise ValueError(f"training attempt {intent_id[:8]} is {attempt.state}; nothing to resolve")
            if evidence.get("verdict") not in RESOLUTION_VERDICTS:
                raise ValueError("a resolution has a known verdict")
            return replace(attempt, state=RESOLVED, journal_attempt_id=journal_attempt_link(attempt, events, now),
                           resolution={
                "resolved_at": _iso(now), "previous_state": attempt.state, "verdict": evidence["verdict"],
                "observed_phase": evidence.get("observed_phase"),
                "observed_course_id": evidence.get("observed_course_id"),
                "observed_end": evidence.get("observed_end")})
        state = _replace_attempt(state, intent_id, resolve)
    return state


def classify_resolution(attempt: Attempt, snapshot: AccountSnapshot) -> tuple[Optional[dict], Optional[str]]:
    """The reconciliation evidence for ``attempt`` from a fresh, complete roster read, or why there is none.

    The requested course running or ended but unclaimed -> ``started``; another course -> ``other_course``; no
    course -> ``not_running``. A read not newer than the attempt (its intent and, when settled, its settlement), a
    roster read that is not complete, a member not on it, an unknown training phase, or a running or unclaimed course
    whose id is unknown gives no evidence: nothing is resolved.
    """
    character_id = attempt.character_id
    observed = snapshot.observed_at if snapshot.observed_at.utcoffset() is not None else None
    for label, text in (("intent", attempt.written_at), ("settlement", attempt.settled_at)):
        moment = _moment(text) if text is not None else None
        if text is not None and (moment is None or observed is None or observed <= moment):
            return None, f"the roster read is not newer than the attempt's {label}; nothing resolved"
    if snapshot.section("crew") is not Completeness.COMPLETE:
        return None, "the roster read was not complete; nothing resolved"
    if snapshot.crew_member(character_id) is None:
        return None, f"character {character_id} is not on the roster read; nothing resolved"
    session = snapshot.training_session(character_id)
    if session is None or session.phase == SESSION_UNKNOWN:
        return None, f"the training state of character {character_id} is unknown; nothing resolved"
    if session.phase in (SESSION_RUNNING, SESSION_ENDED_UNCLAIMED):
        if session.course_id is None:
            return None, f"the course character {character_id} is training is unknown; nothing resolved"
        verdict = "started" if str(session.course_id) == attempt.training_design_id.strip() else "other_course"
    else:
        verdict = "not_running"
    return {"verdict": verdict, "observed_phase": session.phase, "observed_course_id": session.course_id,
            "observed_end": session.end.isoformat() if session.end is not None else None}, None


def _cost_key(cost: Optional[Mapping[str, Any]]) -> dict[str, int]:
    """A planned cost in journal resource names with its nonzero whole amounts (an explicit zero equals no cost)."""
    key: dict[str, int] = {}
    for kind, amount in (cost or {}).items():
        name = COST_KINDS.get(str(kind).lower(), str(kind))
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            key[name] = -1  # unreadable: never equal to a readable price
            continue
        if int(amount):
            key[name] = key.get(name, 0) + int(amount)
    return key


def _cost_text(cost: Mapping[str, int]) -> str:
    return ", ".join(f"{amount} {kind}" for kind, amount in sorted(cost.items())) or "nothing"


def start_refusal(preview: Any, params: Mapping[str, Any], expected_cost: Optional[Mapping[str, Any]]
                  ) -> Optional[str]:
    """Why the planned start must not be sent after the fresh read ``preview`` (a ``CrewPreview``), or ``None``.

    The same start at the same price -> ``None``; another price -> ``price_unavailable``; a crew skip on the fresh
    read -> that skip's code; anything else is refused without a code.
    """
    character_id, course = str(params.get("character_id")), str(params.get("training_design_id"))
    for request in preview.requests:
        if request.action_id != "add_training" or str(request.params.get("character_id")) != character_id:
            continue
        if str(request.params.get("training_design_id")) != course:
            return (f"the fresh read plans course {request.params.get('training_design_id')} for character "
                    f"{character_id} instead; nothing sent ({RECHECKED})")
        planned, observed = _cost_key(expected_cost), _cost_key(request.expected_cost)
        if planned != observed:
            return (f"price_unavailable: the course's price changed since the plan ({_cost_text(planned)} planned, "
                    f"{_cost_text(observed)} observed); {RECHECKED}")
        return None
    for decision in preview.decisions:
        if str(decision.character_id) == character_id and decision.course is None:
            return f"{decision.code}: {decision.detail} ({RECHECKED})"
    for rejection in preview.rejected:
        if rejection.candidate in ("training", f"training {character_id}", f"add_training {character_id}"):
            return f"{rejection.reason} ({RECHECKED})"
    return f"the start is no longer planned on a fresh read; nothing sent ({RECHECKED})"


def state_reason(location: str, error: TrainingStateError) -> str:
    """The operator text, code first, for an attempt store that cannot be used."""
    if isinstance(error, StoreMissing):
        if error.journal_witness:
            return (f"{STATE_MISSING}: the account journal records a training-v2 training start, so the training "
                    f"attempt store {location} was established; it is missing and is never recreated")
        if error.established:
            return (f"{STATE_MISSING}: the established training attempt store {location} is missing and is never "
                    "recreated")
        return f"{STATE_MISSING}: no training attempt store at {location}; provision it with --fleet-provision-training"
    if isinstance(error, StoreUnreadable) and error.other_identity:
        return f"{STATE_UNREADABLE}: training attempt store {location} belongs to another game identity"
    if isinstance(error, AccountLocked):
        return LOCKED_REASON
    return f"{STATE_UNREADABLE}: training attempt store {location} is unreadable ({error})"


def intent_failure_reason(error: BaseException) -> str:
    """The write may have landed (only its directory fsync failed, say), so the text does not claim it did not."""
    return (f"{INTENT_NOT_PERSISTED}: the training intent may not have been written durably "
            f"({type(error).__name__}); nothing was sent. If the store still shows it pending, record a "
            "reconciliation with --fleet-resolve-training before the next training start")


def settle_failure_reason(attempt: Attempt, error: BaseException) -> str:
    """The settlement may have landed, so the text names both store states it can leave."""
    return (f"{UNRESOLVED_ATTEMPT}: the outcome of training attempt {attempt.intent_id[:8]} may not have been "
            f"recorded durably ({type(error).__name__}); while the store shows it pending or unresolved, later "
            "training starts pause until --fleet-resolve-training records a reconciliation")


# --- ports -----------------------------------------------------------------------------------------------------------

class AttemptStore(Protocol):
    def location(self, alias: str) -> str: ...
    def exists(self) -> bool: ...
    def ensure_root(self) -> None: ...
    def load(self, alias: str, identity: Optional[str] = None) -> StoreState: ...
    def established(self, identity: str) -> bool: ...
    def provision(self, alias: str, identity: str, now: datetime,
                  journal_witness: Callable[[], bool] = lambda: False) -> bool: ...
    def write_intent(self, alias: str, attempt: Attempt) -> None: ...
    def settle(self, alias: str, intent_id: str, settlement: Mapping[str, Any]) -> None: ...
    def resolve(self, alias: str, resolutions: Mapping[str, Mapping[str, Any]], now: datetime,
                events: Iterable[Any] = ()) -> None: ...


class LockHandle(Protocol):
    def release(self) -> None: ...


class AccountLock(Protocol):
    def try_acquire(self, identity: str) -> Optional[LockHandle]: ...


class _StoreBase:
    """The store operations over four primitives: read, exclusive create, atomic replace and the marker."""

    def _read(self, alias: str) -> Optional[bytes]:
        raise NotImplementedError

    def _create(self, alias: str, data: bytes) -> None:
        raise NotImplementedError

    def _replace(self, alias: str, data: bytes) -> None:
        raise NotImplementedError

    def established(self, identity: str) -> bool:
        raise NotImplementedError

    def _mark(self, alias: str, identity: str, now: datetime) -> None:
        raise NotImplementedError

    def ensure_root(self) -> None:
        raise NotImplementedError

    def load(self, alias: str, identity: Optional[str] = None) -> StoreState:
        """The store of ``alias``; ``StoreMissing`` (``established`` when ``identity``'s marker exists) or
        ``StoreUnreadable``."""
        raw = self._read(_check_alias(alias))
        if raw is None:
            established = isinstance(identity, str) and bool(GAME_IDENTITY_PATTERN.match(identity)) \
                and self.established(identity)
            raise StoreMissing("no training attempt store", established=established)
        return parse_store(raw, alias)

    def provision(self, alias: str, identity: str, now: datetime,
                  journal_witness: Callable[[], bool] = lambda: False) -> bool:
        """Create the virgin store: ``True`` when created, ``False`` when it already exists for ``identity`` (its
        marker is written again when missing).

        Refused: a store of another identity or an unreadable one (``StoreUnreadable``, bytes untouched) and an
        established store that is missing (``StoreMissing``, nothing recreated): its marker exists, or
        ``journal_witness()`` says the account journal records a start made under the store.
        """
        _check_alias(alias)
        _check_identity(identity)
        self.ensure_root()
        raw = self._read(alias)
        if raw is not None:
            state = parse_store(raw, alias)
            if state.game_identity != identity:
                raise StoreUnreadable("another game identity", other_identity=True)
            if not self.established(identity):
                self._mark(alias, identity, now)
            return False
        if self.established(identity):
            raise StoreMissing("established store missing", established=True)
        if journal_witness():
            raise StoreMissing("the journal records an established store", journal_witness=True)
        self._create(alias, dump_store(virgin_store(alias, identity, now)))  # the store first, then its marker
        self._mark(alias, identity, now)
        return True

    def write_intent(self, alias: str, attempt: Attempt) -> None:
        self._replace(alias, dump_store(with_intent(self.load(alias), attempt)))

    def settle(self, alias: str, intent_id: str, settlement: Mapping[str, Any]) -> None:
        self._replace(alias, dump_store(settled(self.load(alias), intent_id, settlement)))

    def resolve(self, alias: str, resolutions: Mapping[str, Mapping[str, Any]], now: datetime,
                events: Iterable[Any] = ()) -> None:
        self._replace(alias, dump_store(resolved(self.load(alias), resolutions, now, events)))


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


class FileAttemptStore(_StoreBase):
    """The store as files under ``<journal dir>/training-attempts/``; every write is atomic and durable."""

    def __init__(self, journal_dir: Path | str) -> None:
        self.root = Path(journal_dir) / STORE_DIR
        self.marker_root = Path(journal_dir) / MARKER_DIR

    def path(self, alias: str) -> Path:
        return self.root / f"{_check_alias(alias)}{STORE_SUFFIX}"

    def location(self, alias: str) -> str:
        return str(self.path(alias))

    def exists(self) -> bool:
        return self.root.is_dir()

    def ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _read(self, alias: str) -> Optional[bytes]:
        try:
            return self.path(alias).read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:  # a directory in its place, permissions, ...: never treated as fresh
            raise StoreUnreadable(type(error).__name__) from None

    def _temporary(self, path: Path, data: bytes) -> Path:
        """``data`` in a new, fsynced temporary file beside ``path`` (removed again if writing fails)."""
        temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
        written = False
        try:
            _write_all(fd, data)
            os.fsync(fd)
            written = True
        finally:
            os.close(fd)
            if not written:
                temporary.unlink(missing_ok=True)
        return temporary

    def _sync_root(self, directory: Optional[Path] = None) -> None:
        fd = os.open(directory if directory is not None else self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write_atomic(self, path: Path, data: bytes) -> None:
        temporary = self._temporary(path, data)
        replaced = False
        try:
            os.replace(temporary, path)
            replaced = True
        finally:
            if not replaced:
                temporary.unlink(missing_ok=True)
        self._sync_root(path.parent)

    def _create(self, alias: str, data: bytes) -> None:
        path = self.path(alias)
        temporary = self._temporary(path, data)
        try:
            os.link(temporary, path)  # exclusive publish: FileExistsError if another provision won
        finally:
            temporary.unlink(missing_ok=True)
        self._sync_root()

    def _replace(self, alias: str, data: bytes) -> None:
        self._write_atomic(self.path(alias), data)

    def _marker(self, identity: str) -> Path:
        return self.marker_root / f"{_check_identity(identity)}{MARKER_SUFFIX}"

    def established(self, identity: str) -> bool:
        return self._marker(identity).exists()

    def _mark(self, alias: str, identity: str, now: datetime) -> None:
        self.marker_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {"kind": MARKER_KIND, "version": STORE_VERSION, "alias": alias, "game_identity": identity,
                   "provisioned_at": _iso(now)}
        self._write_atomic(self._marker(identity), (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))


class MemoryAttemptStore(_StoreBase):
    """The same contract in memory (unit tests)."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}
        self._markers: set[str] = set()
        self._mutex = threading.Lock()

    def location(self, alias: str) -> str:
        return f"memory:{_check_alias(alias)}"

    def exists(self) -> bool:
        with self._mutex:
            return bool(self._data or self._markers)

    def ensure_root(self) -> None:
        return None

    def _read(self, alias: str) -> Optional[bytes]:
        with self._mutex:
            return self._data.get(alias)

    def _create(self, alias: str, data: bytes) -> None:
        with self._mutex:
            if alias in self._data:
                raise FileExistsError(alias)
            self._data[alias] = data

    def _replace(self, alias: str, data: bytes) -> None:
        with self._mutex:
            self._data[alias] = data

    def established(self, identity: str) -> bool:
        with self._mutex:
            return identity in self._markers

    def _mark(self, alias: str, identity: str, now: datetime) -> None:
        with self._mutex:
            self._markers.add(identity)


class _FileLockHandle:
    def __init__(self, fd: int) -> None:
        self._fd: Optional[int] = fd
        self._mutex = threading.Lock()

    def release(self) -> None:
        with self._mutex:
            fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class FileAccountLock:
    """A non-blocking ``flock`` on ``<root>/<game identity>.lock``, opened anew for every acquisition.

    The root directory must already exist (it is never created here).
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def try_acquire(self, identity: str) -> Optional[_FileLockHandle]:
        """The held lock, or ``None`` at once when another dispatch holds it (never waits)."""
        path = self.root / f"{_check_identity(identity)}{LOCK_SUFFIX}"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
        acquired = False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            return None
        finally:
            if not acquired:
                os.close(fd)
        return _FileLockHandle(fd)


class _MemoryLockHandle:
    def __init__(self, owner: "MemoryAccountLock", identity: str) -> None:
        self._owner, self._identity, self._held = owner, identity, True

    def release(self) -> None:
        with self._owner._mutex:
            if self._held:
                self._held = False
                self._owner._held.discard(self._identity)


class MemoryAccountLock:
    """The same contract in memory (unit tests)."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._held: set[str] = set()

    def try_acquire(self, identity: str) -> Optional[_MemoryLockHandle]:
        _check_identity(identity)
        with self._mutex:
            if identity in self._held:
                return None
            self._held.add(identity)
        return _MemoryLockHandle(self, identity)


# --- the dispatch and the runner's guard -----------------------------------------------------------------------------

Recheck = Callable[[AccountSnapshot, Mapping[str, Any], Optional[Mapping[str, Any]]], Optional[str]]


class TrainingGuard:
    """The runner's bracket around one training start (``sdk.execution.runner.AttemptGuard``).

    ``before_send`` reads the store again (under the dispatch's lock) and rechecks the start; ``intent`` and
    ``settlement`` build what the runner hands to ``write_training_intent`` and ``settle_training_intent``.
    """

    actions = GUARDED_ACTIONS

    def __init__(self, store: AttemptStore, alias: str, identity: str, recheck: Optional[Recheck],
                 clock: Callable[[], datetime]) -> None:
        self.store = store
        self.alias = alias
        self.identity = identity
        self.recheck = recheck
        self.clock = clock
        self._sent = False

    def before_send(self, before: AccountSnapshot, params: Mapping[str, Any],
                    expected_cost: Optional[Mapping[str, Any]]) -> Optional[tuple[str, bool]]:
        """``(reason, stop_account)`` when the start must not be sent, else ``None``."""
        if self._sent:
            return ONE_START_REASON, False
        try:
            state = self.store.load(self.alias, identity=self.identity)
        except TrainingStateError as error:
            return state_reason(self.store.location(self.alias), error), True
        refusal = identity_refusal(state, self.identity) or block_reason(state)
        if refusal is not None:
            return refusal, True
        if self.recheck is not None:
            reason = self.recheck(before, params, expected_cost)
            if reason is not None:
                return reason, False
        return None

    def intent(self, params: Mapping[str, Any], planned: Mapping[str, int]) -> Attempt:
        return new_intent(uuid.uuid4().hex, params, planned, self.clock())

    def mark_sent(self, attempt: Attempt) -> None:
        self._sent = True

    def settlement(self, event: Any) -> dict:
        return {"outcome": event.outcome, "reason": event.reason, "journal_attempt_id": event.attempt_id,
                "settled_at": _iso(self.clock())}

    def reconciled(self) -> frozenset[str]:
        """Journal attempt ids of resolved attempts: an operator's reconciliation lifts their uncertainty."""
        try:
            state = self.store.load(self.alias)
        except TrainingStateError:
            return frozenset()
        return reconciled_attempt_ids(state)

    @staticmethod
    def intent_failure_reason(error: BaseException) -> str:
        return intent_failure_reason(error)

    @staticmethod
    def settle_failure_reason(attempt: Attempt, error: BaseException) -> str:
        return settle_failure_reason(attempt, error)


@dataclass
class Dispatch:
    """One account's execute dispatch: refused, locked out, or holding the lock with the store's state."""

    alias: str
    identity: Optional[str]
    store: Optional[AttemptStore] = None
    state: Optional[StoreState] = None
    refusal: Optional[str] = None
    locked: bool = False

    def block_reason(self) -> Optional[str]:
        return block_reason(self.state) if self.state is not None else None

    def guard(self, recheck: Optional[Recheck], clock: Callable[[], datetime]) -> TrainingGuard:
        if self.store is None or self.state is None or self.identity is None:
            raise ValueError("only a dispatch that holds the lock guards a start")
        return TrainingGuard(self.store, self.alias, self.identity, recheck, clock)


@dataclass(frozen=True)
class TrainingAttempts:
    """The attempt store and account lock the execute and operator commands are given (both injected)."""

    store: Any
    lock: Any

    @classmethod
    def for_journal_dir(cls, journal_dir: Path | str) -> "TrainingAttempts":
        store = FileAttemptStore(journal_dir)
        return cls(store, FileAccountLock(store.root))

    def location(self, alias: str) -> str:
        return self.store.location(alias)

    def reconciled(self, alias: str) -> frozenset[str]:
        """Read-only, without the lock, and never raising: the journal attempt ids an operator's reconciliation
        covers (``reconciled_attempt_ids``). A store that is missing or unreadable covers none, which keeps every
        journaled uncertainty blocking. The dry run's runner uses it, so it plans what the executor will."""
        try:
            return reconciled_attempt_ids(self.store.load(alias))
        except (TrainingStateError, ValueError):
            return frozenset()

    def preflight(self, alias: str, declared_identity: Optional[str]) -> Optional[str]:
        """Before any login, read-only and without the lock: why the store of ``alias`` cannot be used, or ``None``."""
        try:
            state = self.store.load(alias, identity=declared_identity)
        except TrainingStateError as error:
            return state_reason(self.location(alias), error)
        if declared_identity is not None and state.game_identity != declared_identity:
            return state_reason(self.location(alias), StoreUnreadable("identity", other_identity=True))
        return None

    @contextmanager
    def dispatch(self, alias: str, identity: Optional[str]) -> Iterator[Dispatch]:
        """Take the account's lock and read its store; the lock is released when the block exits, however it exits.

        Nothing is settled here: an intent left pending by a crash stays pending.
        """
        if not isinstance(identity, str) or not GAME_IDENTITY_PATTERN.match(identity):
            yield Dispatch(alias, identity, refusal=UNKNOWN_IDENTITY_REASON)
            return
        if not self.store.exists():  # no lock file is created; the marker tells a removed store directory apart
            missing = StoreMissing("no store", established=self.store.established(identity))
            yield Dispatch(alias, identity, refusal=state_reason(self.location(alias), missing))
            return
        try:
            handle = self.lock.try_acquire(identity)
        except (OSError, ValueError) as error:
            yield Dispatch(alias, identity, refusal=(f"{STATE_UNREADABLE}: the account's training lock could not "
                                                     f"be opened ({type(error).__name__})"))
            return
        if handle is None:
            yield Dispatch(alias, identity, refusal=LOCKED_REASON, locked=True)
            return
        try:
            try:
                state: Optional[StoreState] = self.store.load(alias, identity=identity)
                refusal = identity_refusal(state, identity)
            except TrainingStateError as error:
                state, refusal = None, state_reason(self.location(alias), error)
            yield Dispatch(alias, identity, self.store, state if refusal is None else None, refusal)
        finally:
            handle.release()

    @contextmanager
    def hold(self, alias: str, identity: Optional[str]) -> Iterator[Dispatch]:
        """Only the account's lock, for an execute dispatch that never starts training (canary, place, upgrade-ship):
        an overlapping dispatch of the same identity skips the account. The store is neither read nor required (its
        directory is created for the lock file); the lock is released when the block exits, however it exits."""
        if not isinstance(identity, str) or not GAME_IDENTITY_PATTERN.match(identity):
            yield Dispatch(alias, identity, refusal=UNKNOWN_IDENTITY_REASON)
            return
        try:
            self.store.ensure_root()
            handle = self.lock.try_acquire(identity)
        except (OSError, ValueError) as error:
            yield Dispatch(alias, identity, refusal=(f"{STATE_UNREADABLE}: the account's training lock could not "
                                                     f"be opened ({type(error).__name__})"))
            return
        if handle is None:
            yield Dispatch(alias, identity, refusal=LOCKED_REASON, locked=True)
            return
        try:
            yield Dispatch(alias, identity)
        finally:
            handle.release()

    def provision(self, alias: str, identity: str, now: datetime, *, events: Callable[[], Iterable[Any]] = tuple,
                  policy_id: Optional[str] = None) -> bool:
        """Provision the store of ``alias`` for the verified ``identity`` under its lock (``AccountLocked``).

        With ``policy_id``, the account journal (``events``, read under the lock) is a second witness of an
        established store (``journal_witnesses_store``).
        """
        _check_identity(identity)
        self.store.ensure_root()
        handle = self.lock.try_acquire(identity)
        if handle is None:
            raise AccountLocked(LOCKED_REASON)
        try:
            return self.store.provision(alias, identity, now, lambda: policy_id is not None
                                        and journal_witnesses_store(events(), alias, policy_id))
        finally:
            handle.release()

    def resolve(self, alias: str, identity: str, observe: Callable[[], AccountSnapshot],
                clock: Callable[[], datetime], events: Callable[[], Iterable[Any]] = tuple
                ) -> list[tuple[Attempt, dict]]:
        """Under the lock of the declared ``identity``, record a reconciliation for every blocking attempt: all or
        nothing.

        The lock is taken first; only then is the evidence read (``observe``: a fresh, read-only roster read of the
        account, which must be ``identity``'s), so no dispatch can send or settle an attempt between the read and the
        record, and each attempt's evidence must be newer than it (``classify_resolution``). The account journal
        (``events``) is read under the lock too, to link an attempt its settlement never linked. Raises
        ``AccountLocked``, ``TrainingStateError`` or ``ResolutionRefused`` (nothing persisted); ``observe`` may raise
        too. Returns ``[]`` when nothing blocks any more.
        """
        _check_identity(identity)
        handle = self.lock.try_acquire(identity)
        if handle is None:
            raise AccountLocked(LOCKED_REASON)
        try:
            state = self.store.load(alias, identity=identity)
            if state.game_identity != identity:
                raise StoreUnreadable("identity", other_identity=True)
            blocking = [attempt for attempt in state.attempts if attempt.state in BLOCKING]
            if not blocking:
                return []
            snapshot = observe()
            if snapshot.game_identity != identity:
                raise ResolutionRefused("the roster read is of another game identity; nothing resolved")
            resolutions: dict[str, dict] = {}
            for attempt in blocking:
                evidence, why = classify_resolution(attempt, snapshot)
                if evidence is None:
                    raise ResolutionRefused(why)
                resolutions[attempt.intent_id] = evidence
            self.store.resolve(alias, resolutions, clock(), tuple(events()))
            return [(attempt, resolutions[attempt.intent_id]) for attempt in blocking]
        finally:
            handle.release()
