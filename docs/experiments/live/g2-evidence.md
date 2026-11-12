# G2 evidence: first live execution on pss5, 2026-11-12

**Gate:** G2, approved by the owner on 2026-11-12 for execution on `pss5` only, under [authorization-g2-pss5-canary.json](authorization-g2-pss5-canary.json) (canary mode only; `collect_all_resources` and `collect_daily_reward`; starbux cap 0; valid to 2026-11-26). See the [runbook](RUNBOOK.md).
**Who ran it:** the owner, by dispatching [live-trial.yml](../../../.github/workflows/live-trial.yml) on `main`. Agents only downloaded the artifacts, re-scanned them and read them. Raw run records and journals are not committed: they hold room and attempt ids.

## Runs

| Run | Step | Executed | Outcome | Notes |
| --- | --- | --- | --- | --- |
| 43247933379 | enrol | nothing (dry run) | — | gave pss5's salted fingerprint, recorded in #204 |
| 43251373229 | enrol | nothing (dry run) | — | the same fingerprint: the identity check is stable |
| 43256792025 | canary | nothing | — | minerals and gas were never observed (live items carry only `ItemDesignId`), and with no safe-to-retry request the canary had nothing to run; fixed in #214 |
| 43261519416 | canary | one `collect_all_resources` | `not-observed`: minerals and gas unchanged | server version `1.0+settings.3307714` recorded; one `no-verified-action` warning; dashboard written |
| 43262412245 | canary | one `collect_all_resources` | `not-observed`: minerals and gas unchanged | as above |

In both executing runs:

- exactly one action ran, the first safe-to-retry one (S05-R4); `collect_daily_reward` was held back as not safe to retry;
- nothing was spent and starbux stayed disabled (S05-R6); every room upgrade and research was refused by the caps (0) or for its starbux cost;
- the dry-run stamp, the authorization and the identity check passed (S05-R3, R8); the run record holds the authorization and registry hashes, the per-account server version and the results (S05-R7);
- alerts were evaluated and the dashboard written after the run (S11-R1); the only alert was the `no-verified-action` warning, which is correct: an API success is never a verified effect.

## Reconciliation

Both collections reconciled to `not-observed`: the before and after snapshots showed the same mineral and gas balances. The plan reported 500 minerals and no gas above the reserve, so the likely cause is full storage or nothing produced since the last collection; the runs cannot tell which. Nothing is claimed as verified.

## What this does not show

- A verified effect. A canary on an account with free storage, or the progression trial's snapshot diffs, will.
- That the owner has looked at the alerts and dashboard (S11-AC3); they are in each canary artifact (`journals/dashboard.html`), which expires after seven days.
