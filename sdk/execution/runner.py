"""Run one catalog action with before/after snapshots, reconciliation and
journaling (roadmap R1 exit).

The runner owns the evidence loop for a single account:

1. observe a ``before`` snapshot and refuse if it belongs to another account;
2. check the action's preconditions and journal a ``skipped`` event if any fail;
3. in dry-run mode, journal what would have been called and stop;
4. call the ``Client`` method exactly once (never retried here);
5. observe an ``after`` snapshot, reconcile, and journal the outcome.

Exceptions, timeouts and ambiguous responses are journaled as ``unknown``
(uncertain) unless the effect was actually observed. An observed effect the
action's evidence says this call cannot account for (``outcome_supported``
is ``False``) is also ``unknown``, never ``verified``. A non-idempotent
action whose previous attempt is still uncertain is refused until an
operator reconciles it; for ``place_room`` and ``upgrade_ship`` any
uncertain attempt on the account blocks every later one, whatever its params.

An attempt's journaled ``cost`` holds the signed observed resource deltas
(``<kind>_delta``, ``None`` when unobserved). When the caller passes the
planned ``expected_cost`` and a resource's delta was not observed (including
when the ``after`` snapshot failed), the planned amount is also journaled as
``<kind>_uncertain``: an amount that may have been spent, kept apart from
observed spend so budgets can charge it without reports counting it.

A runner can be given per-action ``specs`` overrides (training-v2's strict
start verdict) and an ``attempt_guard`` (SPEC-14 step 4): before a guarded
request is sent the guard rechecks it, then its intent is written durably
through ``write_training_intent``; nothing is sent when either refuses or the
write fails. Once the outcome is journaled it is settled through
``settle_training_intent``; a settlement that cannot be written keeps the
result and raises ``AttemptSettlementError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Protocol

from sdk.actions import (
    ActionSpec,
    Idempotency,
    ReconciliationStatus,
    check_preconditions,
    get_spec,
    reconcile,
)
from sdk.journal import GameplayEvent, GameplayJournal
from sdk.state import AccountMismatchError, AccountSnapshot, diff_snapshots

SnapshotProvider = Callable[[], AccountSnapshot]
Clock = Callable[[], datetime]

def call_arguments(spec: ActionSpec, params: Mapping[str, Any]) -> tuple:
    """A catalog action's Client method arguments: its ``required_params``, in order."""
    return tuple(params[name] for name in spec.required_params)


# Actions whose uncertain outcome blocks every later attempt on the account, whatever its params (S13-R6):
# a placement whose room was not seen may still exist, so a second room at another position could be bought too;
# a hull upgrade that was not seen may still be under way, so another hull design could be paid for too.
ACCOUNT_WIDE_UNCERTAINTY = frozenset({"place_room", "upgrade_ship"})

# Planned-cost kinds (policy requests use "mineral") -> journal resource names.
COST_KINDS = {"mineral": "minerals", "minerals": "minerals", "gas": "gas", "starbux": "starbux"}


class UncertainPriorAttemptError(RuntimeError):
    """Raised when a non-idempotent action's last attempt is still uncertain."""


# --- guarded starts (SPEC-14 step 4) ---------------------------------------------------------------------------------
# A runner given an ``attempt_guard`` brackets each request of the guard's actions with a durable record: the intent is
# written before the request is sent (nothing is sent when it cannot be written) and settled once the outcome is
# journaled. Both go through these module-level hooks, looked up when they are called, so a test (or the offline
# workflow benchmark) can make either write fail. The store is the guard's (``sdk.fleet.attempts``); this module
# never imports it.

def write_training_intent(store: Any, alias: str, intent: Any) -> None:
    """Durably record a training attempt's intent before its request is sent."""
    store.write_intent(alias, intent)


def settle_training_intent(store: Any, alias: str, intent_id: str, settlement: Mapping[str, Any]) -> None:
    """Durably record a sent training attempt's outcome once it is journaled."""
    store.settle(alias, intent_id, settlement)


class AttemptGuard(Protocol):
    """What the runner needs from a guard (``sdk.fleet.attempts.TrainingGuard``)."""

    actions: frozenset
    store: Any

    def before_send(self, before: AccountSnapshot, params: Mapping[str, Any],
                    expected_cost: Optional[Mapping[str, Any]]) -> Optional[tuple[str, bool]]: ...
    def intent(self, params: Mapping[str, Any], planned: Mapping[str, int]) -> Any: ...
    def mark_sent(self, intent: Any) -> None: ...
    def settlement(self, event: GameplayEvent) -> Mapping[str, Any]: ...
    def reconciled(self) -> frozenset: ...
    def intent_failure_reason(self, error: BaseException) -> str: ...
    def settle_failure_reason(self, intent: Any, error: BaseException) -> str: ...


class TrainingStartRefusedError(UncertainPriorAttemptError):
    """The guard refused a start before anything was written or sent; the account stops."""


class IntentNotPersistedError(UncertainPriorAttemptError):
    """The intent could not be written durably, so nothing was sent; the account stops."""


class AttemptSettlementError(UncertainPriorAttemptError):
    """The request was sent and journaled, but its outcome could not be written to the store (the intent stays
    pending and blocks later starts). ``result`` is the journaled result; the account stops."""

    def __init__(self, message: str, result: "RunResult") -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class RunResult:
    action_id: str
    outcome: str
    reason: str
    attempted: bool
    event: GameplayEvent


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _response_ok(result: Any) -> Optional[bool]:
    """Client methods return True/False, a count, None, or other values.

    A boolean is a definite response. A non-negative count (the collection
    methods return how many things they collected, 0 when there was nothing)
    is a completed call. Anything else is ambiguous.
    """
    if isinstance(result, bool):
        return result
    if isinstance(result, int) and result >= 0:
        return True
    return None


def planned_cost(expected_cost: Optional[Mapping[str, Any]]) -> dict[str, int]:
    """Normalise a planned cost to journal resource names with positive amounts.

    Unknown kinds and non-positive or non-numeric amounts are dropped; no
    budget exists for them, so there is nothing to charge.
    """
    planned: dict[str, int] = {}
    for kind, amount in (expected_cost or {}).items():
        name = COST_KINDS.get(str(kind).lower())
        if name is None or isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
            continue
        planned[name] = planned.get(name, 0) + int(amount)
    return planned


def with_uncertain_cost(cost: Mapping[str, Any], planned: Mapping[str, int]) -> dict:
    """Add ``<kind>_uncertain`` for each planned resource whose delta was not observed."""
    result = dict(cost)
    for kind, amount in planned.items():
        if result.get(f"{kind}_delta") is None:
            result[f"{kind}_uncertain"] = amount
    return result


def params_key(params: Mapping[str, Any]) -> tuple:
    """A hashable, order-independent key for an action's params, as journaled."""
    return tuple(sorted((str(key), str(value)) for key, value in params.items()))


class ActionRunner:
    def __init__(
        self,
        account_alias: str,
        client: Any,
        snapshot_provider: SnapshotProvider,
        journal: GameplayJournal,
        clock: Clock = _utc_now,
        *,
        specs: Optional[Mapping[str, ActionSpec]] = None,
        attempt_guard: Optional[AttemptGuard] = None,
    ) -> None:
        """``specs`` overrides catalog specs by action id (training-v2's strict start verdict); ``attempt_guard``
        brackets each request of its actions with a durable intent and settlement. Neither changes anything else."""
        self.account_alias = account_alias
        self.client = client
        self.snapshot_provider = snapshot_provider
        self.journal = journal
        self.clock = clock
        self.specs = dict(specs or {})
        self.attempt_guard = attempt_guard

    def spec_for(self, action_id: str) -> ActionSpec:
        """The spec this runner uses for ``action_id``: an override from ``specs``, else the catalog's."""
        override = self.specs.get(action_id)
        return override if override is not None else get_spec(action_id)

    # -- helpers ---------------------------------------------------------------------

    def _record(self, spec: ActionSpec, started: datetime, params, **fields) -> RunResult:
        event = GameplayEvent.create(
            account_alias=self.account_alias,
            action_id=spec.action_id,
            started_at=started,
            finished_at=self.clock(),
            expected_effect=spec.expected_effect,
            params=dict(params),
            **fields,
        )
        self.journal.append(event)
        return RunResult(spec.action_id, event.outcome, event.reason, event.attempted, event)

    def _observe(self) -> AccountSnapshot:
        snapshot = self.snapshot_provider()
        if snapshot.account_alias != self.account_alias:
            raise AccountMismatchError(
                f"snapshot belongs to {snapshot.account_alias!r}, runner is for {self.account_alias!r}"
            )
        return snapshot

    def _guard_for(self, spec: ActionSpec) -> Optional[AttemptGuard]:
        guard = self.attempt_guard
        return guard if guard is not None and spec.action_id in guard.actions else None

    def _last_attempt_uncertain(self, spec: ActionSpec, params: Mapping[str, Any]) -> bool:
        key = params_key(params)
        account_wide = spec.action_id in ACCOUNT_WIDE_UNCERTAINTY
        guard = self._guard_for(spec)
        # An attempt the guard's store records as reconciled by an operator is no longer uncertain. Without a guard (the
        # dry run) the journal may carry the same read-only view (``reconciled_attempts``), so both plan alike.
        if guard is not None:
            reconciled = guard.reconciled()
        else:
            provider = getattr(self.journal, "reconciled_attempts", None)
            reconciled = provider() if callable(provider) else frozenset()
        attempts = [
            event
            for event in self.journal.read()
            if event.account_alias == self.account_alias
            and event.action_id == spec.action_id
            and event.attempted
            and (account_wide or params_key(event.params) == key)
            and event.attempt_id not in reconciled
        ]
        return bool(attempts) and attempts[-1].outcome == ReconciliationStatus.UNKNOWN.value

    def _settle(self, guard: AttemptGuard, intent: Any, result: "RunResult") -> "RunResult":
        """Record the journaled outcome of a guarded request in the guard's store."""
        try:
            settle_training_intent(guard.store, self.account_alias, intent.intent_id, guard.settlement(result.event))
        except Exception as error:  # the intent stays pending, which blocks later starts just the same
            raise AttemptSettlementError(guard.settle_failure_reason(intent, error), result) from None
        return result

    # -- public API ------------------------------------------------------------------

    def run(
        self,
        action_id: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        dry_run: bool = False,
        allow_after_uncertain: bool = False,
        expected_cost: Optional[Mapping[str, Any]] = None,
    ) -> RunResult:
        spec = self.spec_for(action_id)
        params = dict(params or {})
        planned = planned_cost(expected_cost)
        started = self.clock()

        try:
            before = self._observe()
        except AccountMismatchError as error:
            self._record(spec, started, params, outcome="skipped", reason=str(error), attempted=False,
                         error_class="AccountMismatchError", dry_run=dry_run)
            raise

        failures = check_preconditions(spec, before, params)
        if failures:
            return self._record(spec, started, params, outcome="skipped",
                                reason="preconditions failed: " + "; ".join(failures), attempted=False,
                                dry_run=dry_run)

        if (
            spec.idempotency is Idempotency.NOT_IDEMPOTENT
            and not allow_after_uncertain
            and self._last_attempt_uncertain(spec, params)
        ):
            self._record(spec, started, params, outcome="skipped",
                         reason=(f"a previous {spec.action_id} attempt on this account is still uncertain, whatever "
                                 f"its params; reconcile it before running {spec.action_id} again"
                                 if spec.action_id in ACCOUNT_WIDE_UNCERTAINTY
                                 else "previous attempt is still uncertain; reconcile it before retrying"),
                         attempted=False, error_class="UncertainPriorAttemptError", dry_run=dry_run)
            raise UncertainPriorAttemptError(f"{spec.action_id} has an uncertain prior attempt")

        if dry_run:
            return self._record(spec, started, params, outcome="skipped",
                                reason=f"dry run: would call Client.{spec.client_method}", attempted=False,
                                dry_run=True)

        guard = self._guard_for(spec)
        intent = None
        if guard is not None:
            verdict = guard.before_send(before, params, expected_cost)
            if verdict is not None:
                reason, stop = verdict
                result = self._record(spec, started, params, outcome="skipped", reason=reason, attempted=False,
                                      error_class="TrainingStartRefusedError" if stop else None)
                if stop:
                    raise TrainingStartRefusedError(reason)
                return result
            intent = guard.intent(params, planned)
            try:
                write_training_intent(guard.store, self.account_alias, intent)  # durable before anything is sent
            except Exception as error:
                reason = guard.intent_failure_reason(error)
                self._record(spec, started, params, outcome="skipped", reason=reason, attempted=False,
                             error_class="IntentNotPersistedError")
                raise IntentNotPersistedError(reason) from None
            guard.mark_sent(intent)

        method = getattr(self.client, spec.client_method)
        probe_before = spec.evidence_probe(self.client) if spec.evidence_probe else None

        def evidence(status: ReconciliationStatus, delta=None) -> dict:
            if probe_before is None or spec.evidence is None:
                return {}
            return dict(spec.evidence(probe_before, spec.evidence_probe(self.client), status, delta))

        response_ok: Optional[bool] = None
        error_class: Optional[str] = None
        try:
            response_ok = _response_ok(method(*call_arguments(spec, params)))
        except Exception as error:  # the request may still have reached the server
            error_class = type(error).__name__

        try:
            after = self._observe()
        except AccountMismatchError as error:
            mismatch = self._record(spec, started, params, outcome="unknown", reason=str(error), attempted=True,
                                    response_ok=response_ok, error_class="AccountMismatchError",
                                    cost=with_uncertain_cost({}, planned),
                                    evidence=evidence(ReconciliationStatus.UNKNOWN))
            if guard is not None:
                try:
                    self._settle(guard, intent, mismatch)
                except AttemptSettlementError:  # still pending: it blocks just the same
                    pass
            raise
        except Exception as error:
            failed = self._record(spec, started, params, outcome="unknown",
                                  reason="after-snapshot failed; effect cannot be reconciled", attempted=True,
                                  response_ok=response_ok, error_class=error_class or type(error).__name__,
                                  cost=with_uncertain_cost({}, planned),
                                  evidence=evidence(ReconciliationStatus.UNKNOWN))
            return self._settle(guard, intent, failed) if guard is not None else failed

        result = reconcile(spec, before, after, params, response_ok=response_ok)
        outcome, reason = result.status, result.reason
        if outcome is not ReconciliationStatus.VERIFIED:
            if error_class:
                outcome, reason = ReconciliationStatus.UNKNOWN, f"call raised {error_class}; {reason}"
            elif response_ok is None and outcome is ReconciliationStatus.NOT_OBSERVED:
                outcome, reason = ReconciliationStatus.UNKNOWN, f"ambiguous response; {reason}"

        delta = diff_snapshots(before, after)
        observed = evidence(outcome, delta)
        # An observed effect that nothing in this call can account for (for
        # example a trophy change when no battle step was reached) is not verified.
        if outcome is ReconciliationStatus.VERIFIED and observed.get("outcome_supported") is False:
            support = observed.get("outcome_support_reason") or "the call's evidence does not support the effect"
            prefix = f"call raised {error_class}; " if error_class else ""
            outcome, reason = ReconciliationStatus.UNKNOWN, f"{prefix}{reason}; but {support}"
            observed = evidence(outcome, delta)
        # Signed observed resource deltas; negative values are spend. None = unobserved,
        # in which case the planned amount (if any) is kept apart as possibly spent.
        cost = with_uncertain_cost(
            {"minerals_delta": delta.minerals, "gas_delta": delta.gas, "starbux_delta": delta.starbux}, planned)
        result = self._record(spec, started, params, outcome=outcome.value, reason=reason, attempted=True,
                              response_ok=response_ok, error_class=error_class, cost=cost,
                              evidence=observed)
        return self._settle(guard, intent, result) if guard is not None else result
