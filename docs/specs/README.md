# Spec-driven plan for completing the roadmap

**Status:** 2026-11-12. SPEC-03 and SPEC-04 are `Done` with G1 live evidence and SPEC-05 with G2 evidence; eight specs are `Verified offline`; SPEC-09 is approved but blocked on gate G4. G1 and G2 are open; G3–G5 are not.
**Covers:** everything left in [PRODUCT_ROADMAP.md](../PRODUCT_ROADMAP.md) after #152. The [Bazel migration](../BAZEL_MIGRATION_ROADMAP.md) is a separate track with its own plan.

Every offline slice of R0–R6 is merged. What is left is live work that changes real accounts, reads whose response shapes were never confirmed, and some library pieces that nothing uses yet. Mistakes in that work are hard to undo, so each piece starts as a spec: what must be true, how it will be proven, and what the owner must decide. Code comes after.

## How a piece of work moves

| Stage | Who | What happens | Done when |
| --- | --- | --- | --- |
| 1. Draft | agent or owner | A spec is written from [TEMPLATE.md](TEMPLATE.md) | Every requirement has an id, a level (MUST/SHOULD/MAY) and at least one acceptance criterion |
| 2. Review | reviewer (agent) | Checks the spec against the repo, the safety invariants and the roadmap | Findings are resolved or recorded as open questions |
| 3. Approve | **owner** | Answers the spec's owner decisions and sets `Status: Approved` | Status line and owner decisions are filled in |
| 4. Tests first | agent slice | Acceptance tests are written before the code they test, citing requirement ids | The tests exist and fail or skip for the right reason |
| 5. Build | agent slices | Queue slices implement the requirements; each slice lists the ids it covers | The spec's offline acceptance criteria pass in CI |
| 6. Offline verification | agent | Every MUST is covered by a passing test (checked by [SPEC-01](SPEC-01-traceability.md)) | `Status: Verified offline` |
| 7. Live evidence | **owner** | The spec's live acceptance criteria are run under an owner authorization and recorded in `docs/experiments/` | `Status: Done`, with links to the evidence |

A spec that needs no live evidence goes from stage 6 straight to `Done`.

### Rules

- **No code without an approved spec.** A queue slice for spec work names the spec and the requirement ids it implements in its `notes`, and its `allowed_paths` stay inside what the spec lists.
- **Ids are permanent.** Requirements are `Snn-Rk`, acceptance criteria `Snn-ACk`. A removed requirement is marked `Withdrawn`, never renumbered.
- **Traceability.** Each test that proves a requirement carries a comment `# Verifies: Snn-Rk`. [SPEC-01](SPEC-01-traceability.md) makes CI fail when a spec marked `Verified offline` or `Done` has a MUST with an offline acceptance criterion but no test, or a test cites an id that does not exist. Requirements proved only by live acceptance criteria need evidence instead of a test.
- **Live steps belong to the owner.** An agent never runs a live acceptance criterion, never edits a workflow that holds secrets, and never handles real credentials. Agent slices build and test offline; the owner runs and records.
- **The project invariants apply to every spec.** An API success is never a verified effect. Unobserved values are `None`, never 0. Accounts never mix. Secrets never reach journals, reports or error text. Starbux is never spent by fleet runs. Non-idempotent actions are not retried after an uncertain outcome. Each spec states how it keeps them.

## Specs

| Spec | Title | Release | Needs owner gate | Depends on | Status |
| --- | --- | --- | --- | --- | --- |
| [SPEC-01](SPEC-01-traceability.md) | Spec traceability check | process | — | — | Verified offline |
| [SPEC-02](SPEC-02-local-credential-safety.md) | Local test runs never touch real credential files | safety | — | — | Verified offline |
| [SPEC-03](SPEC-03-verified-profile-reads.md) | Verified trophy, starbux and battle-history reads | R1/R3 | G1 | 01 | Done ([evidence](../experiments/live/g1-evidence.md)) |
| [SPEC-04](SPEC-04-account-baselines.md) | Per-ship baselines and game-version provenance | R0 | G1 | 01, 03 | Done ([evidence](../experiments/baselines/2026-11-12-g1.md)) |
| [SPEC-05](SPEC-05-live-execution-gate.md) | Authorized live execution with a canary and kill switch | R2 | G2 for use | 01, 02 | Done ([evidence](../experiments/live/g2-evidence.md)) |
| [SPEC-06](SPEC-06-legacy-daily-run.md) | Fence the legacy daily run away from trial accounts | ops | G2 | 05 | Verified offline |
| [SPEC-07](SPEC-07-progression-trial.md) | Live progression trial, manual vs `baseline-v1` | R2 exit | G2 | 04, 05, 06 | Verified offline |
| [SPEC-08](SPEC-08-pvp-trial.md) | Live PvP strategy comparison | R3 exit | G3 | 03, 05, 07 | Verified offline |
| [SPEC-09](SPEC-09-market-trades.md) | Market trade calls and a reconciled live trade | R4 exit | G4 | 03, 05 | Approved, blocked on G4 |
| [SPEC-10](SPEC-10-fleet-trial.md) | Live fleet trial on two accounts | R5 exit | G5 | 05, 06, 07 | Verified offline |
| [SPEC-11](SPEC-11-alert-delivery.md) | Scheduled reports and alert delivery | R6 | G2 | 05 | Verified offline |
| [SPEC-12](SPEC-12-study-report.md) | Automation effectiveness study report | R6 exit | — | 07, 08, 09, 10 | Verified offline |
| [SPEC-13](SPEC-13-room-placement.md) | Automated room placement | ops (supports R2) | G6 | 03, 05 | Draft |

## Owner gates

The roadmap cannot be completed without these decisions. Each is asked once, recorded in the spec that needs it, and unblocks a milestone. Nothing in a gate is assumed.

| Gate | What the owner provides | Unblocks | Status |
| --- | --- | --- | --- |
| **G0** | Approve SPEC-01 and SPEC-02 (no live access) | M0 | Approved 2026-11-11 |
| **G1** | Read-only live access for one owned test account, and sanitized captures (no tokens, ids or e-mails) of the responses SPEC-03 lists | M1 | Approved 2026-11-12 for `pss1`–`pss5` (read-only) |
| **G2** | Live execution on one named test account; the manual routine; trial windows; removing the account from the legacy daily run; the stop thresholds | M2 | Approved 2026-11-12 for `pss5`; see [the runbook](../experiments/live/RUNBOOK.md) |
| **G3** | Live PvP on the trial account; the two strategies; whether a strategy is applied by the bot or by hand | M3 (PvP) | Not given |
| **G4** | Sanitized current captures of `SellItem3` and `Purchase4`; live trades on the test account with a value cap | M3 (market) | Not given |
| **G5** | A second owned test account; operator-minute logging; live fleet runs | M4 | Not given |
| **G6** | Sanitized captures of one room purchase with the ship and design records; one live room placement on the trial account | SPEC-13 | Not given (SPEC-13 is a draft) |

## Milestones

| Milestone | Specs | Owner gate | Exit evidence |
| --- | --- | --- | --- |
| **M0 Foundation** (offline) | 01, 02, 05 build, 06 design | G0 | Traceability check in CI; tests can no longer overwrite `.device`; execute gate built and tested with fakes |
| **M1 Read-only evidence** | 03, 04 | G1 | Parsers verified against real sanitized captures; baseline snapshot of each test ship with a recorded game version |
| **M2 First live execution** | 05 use, 06, 07 | G2 | Canary run reconciled; progression trial run for its windows; R2 exit report in `docs/experiments/` |
| **M3 PvP and market** | 08, 09 | G3, G4 | Strategy comparison with known-result battles; at least one completed trade reconciled live |
| **M4 Fleet** | 10 | G5 | Two-account trial with a clean isolation audit and fleet report |
| **M5 Study and operations** | 11, 12 | — | Study report; scheduled reports and alerts running |

M0 can start as soon as G0 is given. M1 and the offline build of M2 can then run in parallel. Each live milestone waits for its gate.

## Where it stands

Every offline slice in the plan is merged (#157–#176). What remains needs the owner:

- **G1 (open):** SPEC-03 and SPEC-04 are done ([G1 evidence](../experiments/live/g1-evidence.md), [baseline](../experiments/baselines/2026-11-12-g1.md)).
- **G2 (open):** the SPEC-05 canary ran ([evidence](../experiments/live/g2-evidence.md)); SPEC-06 is verified offline (#209 merged) and its last criterion follows the SPEC-07 trial; SPEC-07 needs the owner to approve its [draft trial plan](../experiments/r2-progression-trial.md) and a canary that reconciles `verified` (S07-R2); SPEC-11 needs the owner to confirm seeing the canary's alerts and dashboard.
- **G3, G4, G5:** PvP, market captures, and a second account, for SPEC-08, SPEC-09 and SPEC-10.
- SPEC-12's written report follows the trials.
- SPEC-13 (automated room placement) is a draft the owner asked for on 2026-11-12. It needs approval, then G6. It does not unblock SPEC-07 in time, so the owner places pss5's collector by hand.

## What "roadmap complete" means

The roadmap is complete when every release's exit evidence in [PRODUCT_ROADMAP.md](../PRODUCT_ROADMAP.md) exists as a recorded live result, and every spec here is `Done` or `Withdrawn` with a reason. Library code and offline tests alone do not complete a release.

## Working with the queue

Spec work runs through the repo-automation supervisor like everything else:

- A queue PR adds a spec's slices once the spec is approved. Live steps go in as `blocked` entries whose `entry_condition` names the owner gate; they are run by the owner, never queued for an agent.
- Each slice's `notes` start with `Implements Snn-Rk[, ...]`.
- The existing validations (`make syntax-check`, `make test`, `git diff --check`) stay. Once SPEC-01 lands, `make test` also checks traceability.
- Until [SPEC-02](SPEC-02-local-credential-safety.md) lands, run the test suite in a scratch clone, never from the repo root: a repo-root run rewrites `./.device`.
