"""Concurrent fleet orchestration (R5).

``ConcurrentFleetOrchestrator`` runs the same per-account steps as the
sequential ``FleetOrchestrator`` on a bounded thread pool, so several
accounts can be in flight at once. Every sequential guarantee holds.

Each account is three worker steps, never more than one in flight at a time:

1. **open**: the factory call (which may log in);
2. **first snapshot**: ``context.snapshot_provider()``;
3. **schedule**: the budgeted scheduler run (``_run_scheduler``).

Between the steps the coordinating thread decides the account's claims, in
alias order: the journal claim after *open*, and the snapshot claim
(``FleetClaims.claim_snapshot``, the hook for identity claims) after *first
snapshot*. The first account in alias order wins a contested claim,
whatever order the steps finish in, and a refused account ends with
``error`` before its next step. So no claim is decided after the account's
first game call (the first snapshot is a read), and none of its scheduler's
calls happen before every claim is decided.

Scheduling. At most ``max_workers`` steps are in flight. When a worker is
free the coordinator starts the ready step of the earliest account in alias
order, so an account that has opened moves on before later accounts open.
With one worker the run is the sequential order exactly. The cost of
deterministic claims: an account's first snapshot waits until every earlier
account's factory has returned, and its scheduler waits until every earlier
account's first snapshot has been claimed. One slow login therefore delays
the later accounts' first snapshots, but not the schedulers of accounts
already past their claims.

Fleet stop. Each step checks the stop file before it starts. Once the file
is seen it stays in force for the run. An account not yet opened is
``not-run`` with the fleet stop reason. An account already opened (its
factory ran, so it may hold a login session) but not yet scheduled is also
``not-run``, with a reason that says it was opened. Running schedulers check
the file between actions, as in sequential mode.

An exception in one account is recorded as that account's ``error``.
Budgets, the starbux rule and dry run by default are unchanged, because the
per-account work is the sequential orchestrator's own code. Results are
returned in alias order regardless of completion order, and the run records
``mode`` ``concurrent``. The clock, the elapsed-time timer and the factory
are injectable as in the sequential orchestrator, and must be safe to call
from worker threads.
"""

from __future__ import annotations

import heapq
import itertools
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Optional

from sdk.fleet.accounts import AccountProfile, AccountRegistry
from sdk.fleet.orchestrator import (
    AccountContext,
    AccountFactory,
    AccountRun,
    FleetOrchestrator,
    FleetRun,
    error_reason,
)
from sdk.state import AccountSnapshot

MODE_CONCURRENT = "concurrent"
DEFAULT_MAX_WORKERS = 4


@dataclass
class _Account:
    profile: AccountProfile
    started: float = 0.0
    context: Optional[AccountContext] = None
    snapshot: Optional[AccountSnapshot] = None
    result: Optional[AccountRun] = None


class ConcurrentFleetOrchestrator(FleetOrchestrator):
    def __init__(self, registry: AccountRegistry, factory: AccountFactory, *,
                 max_workers: int = DEFAULT_MAX_WORKERS, **kwargs: Any) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        super().__init__(registry, factory, **kwargs)
        self.max_workers = max_workers

    def run(self, *, execute: bool = False) -> FleetRun:
        started_at = self.clock()
        runs: dict[str, AccountRun] = {}
        accounts: list[_Account] = []
        for alias in self.registry.aliases():
            profile = self.registry.get(alias)
            if profile.enabled:
                accounts.append(_Account(profile))
            else:
                runs[alias] = AccountRun(alias, "disabled", "account disabled in the registry")

        fleet_stop = f"fleet stop file present: {self.stop_file}"
        opened_stop = f"account opened but stopped before scheduling: {fleet_stop}"
        stopped = [False]  # written only on the coordinating thread
        claims = self._new_claims()

        def stop_seen() -> bool:
            # Sticky like the sequential loop: once seen, later steps stay stopped.
            if not stopped[0] and self._stop_requested():
                stopped[0] = True
            return stopped[0]

        # Worker steps. Each returns a callable that the coordinator applies to the account.
        def open_step(account: _Account) -> Callable[[], None]:
            started = self.timer()
            try:
                context = self.factory(account.profile)
            except Exception as error:  # one account's failure must not stop the others
                run = self._finish(account.profile, started, "error", error_reason(error))
                return lambda: setattr(account, "result", run)

            def opened() -> None:
                account.started, account.context = started, context
            return opened

        def snapshot_step(account: _Account) -> Callable[[], None]:
            try:
                snapshot = account.context.snapshot_provider()
            except Exception as error:  # one account's failure must not stop the others
                run = self._finish(account.profile, account.started, "error", error_reason(error))
                return lambda: setattr(account, "result", run)
            return lambda: setattr(account, "snapshot", snapshot)

        def schedule_step(account: _Account) -> Callable[[], None]:
            run = self._run_scheduler(account.profile, account.context, execute, account.started, claims)
            return lambda: setattr(account, "result", run)

        ready: list[tuple[int, int, Callable]] = []  # (alias index, sequence, step)
        sequence = itertools.count()

        def make_ready(index: int, step: Callable) -> None:
            heapq.heappush(ready, (index, next(sequence), step))

        def claim(account: _Account, decide: Callable[[], Optional[str]], next_step: Callable,
                  index: int) -> None:
            try:
                refusal = decide()
            except Exception as error:  # one account's failure must not stop the others
                refusal = error_reason(error)
            if refusal is not None:
                account.result = self._finish(account.profile, account.started, "error", refusal)
            else:
                make_ready(index, next_step)

        journal_next = snapshot_next = 0

        def decide_claims() -> None:
            """Decide claims in alias order, as far as the finished steps allow."""
            nonlocal journal_next, snapshot_next
            while journal_next < len(accounts):
                account = accounts[journal_next]
                if account.result is None and account.context is None:
                    break  # still opening: later accounts must wait so the earliest alias wins
                if account.result is None:
                    claim(account, lambda: claims.claim_journal(account.profile.alias, account.context.journal),
                          snapshot_step, journal_next)
                journal_next += 1
            while snapshot_next < journal_next:
                account = accounts[snapshot_next]
                if account.result is None and account.snapshot is None:
                    break
                if account.result is None:
                    claim(account, lambda: claims.claim_snapshot(account.profile.alias, account.snapshot),
                          schedule_step, snapshot_next)
                snapshot_next += 1

        for index in range(len(accounts)):
            make_ready(index, open_step)

        if accounts:
            in_flight: dict[Future, int] = {}
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(accounts)),
                                    thread_name_prefix="fleet") as pool:
                while True:
                    while ready and len(in_flight) < self.max_workers:
                        index, _, step = heapq.heappop(ready)
                        account = accounts[index]
                        if stop_seen():
                            account.result = (AccountRun(account.profile.alias, "not-run", fleet_stop)
                                              if step is open_step else
                                              self._finish(account.profile, account.started, "not-run",
                                                           opened_stop))
                            continue
                        in_flight[pool.submit(step, account)] = index
                    if not in_flight:
                        break
                    done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                    for future in sorted(done, key=in_flight.get):
                        account = accounts[in_flight.pop(future)]
                        try:
                            future.result()()
                        except Exception as error:  # one account's failure must not stop the others
                            account.result = AccountRun(account.profile.alias, "error", error_reason(error))
                    decide_claims()

        for account in accounts:
            runs[account.profile.alias] = account.result or AccountRun(
                account.profile.alias, "error", "account did not finish")  # defensive; not expected
        return FleetRun(
            mode=MODE_CONCURRENT,
            dry_run=not execute,
            started_at=started_at.isoformat(),
            finished_at=self.clock().isoformat(),
            accounts=tuple(runs[alias] for alias in self.registry.aliases()),
            stop_reason=fleet_stop if stopped[0] else None,
        )
