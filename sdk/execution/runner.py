"""Run one catalog action with before/after snapshots, reconciliation and
journaling (roadmap R1 exit).

The runner owns the evidence loop for a single account:

1. observe a ``before`` snapshot and refuse if it belongs to another account;
2. check the action's preconditions and journal a ``skipped`` event if any fail;
3. in dry-run mode, journal what would have been called and stop;
4. call the ``Client`` method exactly once (never retried here);
5. observe an ``after`` snapshot, reconcile, and journal the outcome.

Exceptions, timeouts and ambiguous responses are journaled as ``unknown``
(uncertain) unless the effect was actually observed. A non-idempotent
action whose previous attempt is still uncertain is refused until an
operator reconciles it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

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

# How each catalog action's parameters map onto its Client method's arguments.
CALL_ARGUMENTS: dict[str, Callable[[Mapping[str, Any]], tuple]] = {
    "collect_all_resources": lambda params: (),
    "collect_daily_reward": lambda params: (),
    "add_training": lambda params: (params["training_design_id"], params["character_id"]),
    "upgrade_room": lambda params: (),
    "add_research": lambda params: (params["research_design_id"],),
}


class UncertainPriorAttemptError(RuntimeError):
    """Raised when a non-idempotent action's last attempt is still uncertain."""


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
    """Client methods return True/False, None, or other values. Only a
    boolean is treated as a definite response; anything else is ambiguous."""
    return result if isinstance(result, bool) else None


def _params_key(params: Mapping[str, Any]) -> tuple:
    return tuple(sorted((str(key), str(value)) for key, value in params.items()))


class ActionRunner:
    def __init__(
        self,
        account_alias: str,
        client: Any,
        snapshot_provider: SnapshotProvider,
        journal: GameplayJournal,
        clock: Clock = _utc_now,
    ) -> None:
        self.account_alias = account_alias
        self.client = client
        self.snapshot_provider = snapshot_provider
        self.journal = journal
        self.clock = clock

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

    def _last_attempt_uncertain(self, spec: ActionSpec, params: Mapping[str, Any]) -> bool:
        key = _params_key(params)
        attempts = [
            event
            for event in self.journal.read()
            if event.account_alias == self.account_alias
            and event.action_id == spec.action_id
            and event.attempted
            and _params_key(event.params) == key
        ]
        return bool(attempts) and attempts[-1].outcome == ReconciliationStatus.UNKNOWN.value

    # -- public API ------------------------------------------------------------------

    def run(
        self,
        action_id: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        dry_run: bool = False,
        allow_after_uncertain: bool = False,
    ) -> RunResult:
        spec = get_spec(action_id)
        params = dict(params or {})
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
                         reason="previous attempt is still uncertain; reconcile it before retrying",
                         attempted=False, error_class="UncertainPriorAttemptError", dry_run=dry_run)
            raise UncertainPriorAttemptError(f"{spec.action_id} has an uncertain prior attempt")

        if dry_run:
            return self._record(spec, started, params, outcome="skipped",
                                reason=f"dry run: would call Client.{spec.client_method}", attempted=False,
                                dry_run=True)

        method = getattr(self.client, spec.client_method)
        response_ok: Optional[bool] = None
        error_class: Optional[str] = None
        try:
            response_ok = _response_ok(method(*CALL_ARGUMENTS[spec.action_id](params)))
        except Exception as error:  # the request may still have reached the server
            error_class = type(error).__name__

        try:
            after = self._observe()
        except AccountMismatchError as error:
            self._record(spec, started, params, outcome="unknown", reason=str(error), attempted=True,
                         response_ok=response_ok, error_class="AccountMismatchError")
            raise
        except Exception as error:
            return self._record(spec, started, params, outcome="unknown",
                                reason="after-snapshot failed; effect cannot be reconciled", attempted=True,
                                response_ok=response_ok, error_class=error_class or type(error).__name__)

        result = reconcile(spec, before, after, params, response_ok=response_ok)
        outcome, reason = result.status, result.reason
        if outcome is not ReconciliationStatus.VERIFIED:
            if error_class:
                outcome, reason = ReconciliationStatus.UNKNOWN, f"call raised {error_class}; {reason}"
            elif response_ok is None and outcome is ReconciliationStatus.NOT_OBSERVED:
                outcome, reason = ReconciliationStatus.UNKNOWN, f"ambiguous response; {reason}"

        delta = diff_snapshots(before, after)
        # Signed observed resource deltas; negative values are spend. None = unobserved.
        cost = {"minerals_delta": delta.minerals, "gas_delta": delta.gas, "starbux_delta": delta.starbux}
        return self._record(spec, started, params, outcome=outcome.value, reason=reason, attempted=True,
                            response_ok=response_ok, error_class=error_class, cost=cost)
