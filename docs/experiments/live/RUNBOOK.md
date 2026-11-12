# Live trial runbook (gates G1 and G2)

**Approved:** by the owner on 2026-11-12, for the recently created test accounts `PSS_ACCOUNT_1` to `PSS_ACCOUNT_5` (aliases `pss1` to `pss5`). G1 covers read-only access to all five. G2 covers live execution on `pss5` only, under [authorization-g2-pss5-canary.json](authorization-g2-pss5-canary.json).

Credentials stay in the repository secrets. The live steps run only when the owner dispatches [live-trial.yml](../../../.github/workflows/live-trial.yml); agents never trigger them and never see the credentials. Every output is sanitized and scanned before it is uploaded.

## Before the first run (owner)

1. Create the identity salt secret, used to fingerprint each logged-in game account:

   ```bash
   openssl rand -base64 32 | gh secret set TACHIKOMA_FLEET_SALT
   ```

2. Take `pss5` out of the legacy daily run (SPEC-06):

   ```bash
   gh variable set LEGACY_RUN_SKIP --body 5
   ```

3. Disable the LangGraph daily run while the trial lasts. It logs in to all five accounts in one step and cannot skip one:

   ```bash
   gh workflow disable daily-run-langgraph.yml
   ```

## G1: read-only (SPEC-03, SPEC-04)

```bash
gh workflow run live-trial.yml -f step=capture
gh workflow run live-trial.yml -f step=baseline
```

Each run uploads an artifact named `live-<step>-<run id>`. An agent downloads it with `gh run download`, re-scans it, and opens a pull request with:

- the reviewed captures in `tests/fixtures/live_sanitized/`, which unblock the SPEC-03 parsers;
- the baseline summary in `docs/experiments/baselines/`.

## G2: first live execution (SPEC-05, SPEC-07)

```bash
gh workflow run live-trial.yml -f step=enrol
```

The enrol run prints pss5's identity fingerprint. An agent adds it to [registry-g2-pss5.json](registry-g2-pss5.json) as `expected_game_identity` in a pull request. A fingerprint is not secret, because it is salted. Execution refuses any profile without one.

Done 2026-11-12: enrol run 43247933379 (a dry run; nothing executed, one `collect_daily_reward` planned) gave pss5's fingerprint, now in the registry.

After that pull request merges:

```bash
gh workflow run live-trial.yml -f step=canary
```

The canary runs a fresh dry run, then at most one safe-to-retry action (`collect_all_resources`) on pss5, and records the reconciliation, run record, alerts and dashboard. It never spends: the authorization allows `canary` mode only.

## What still needs the owner before the progression trial

SPEC-07's trial plan needs, before its window opens:

- the manual routine you follow each day;
- the trial windows;
- how operator minutes will be logged;
- the stop thresholds.

A new authorization file with `full` mode follows once the plan is written.

## Stopping

- Cancel a running dispatch: `gh run cancel <run id>`.
- Put pss5 back in the daily run: `gh variable delete LEGACY_RUN_SKIP`.
- Re-enable LangGraph: `gh workflow enable daily-run-langgraph.yml`.
