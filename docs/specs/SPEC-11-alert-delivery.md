# SPEC-11: Scheduled reports and alert delivery

**Status:** Verified offline
**Approval:** owner, 2026-11-11, through the session goal "complete the plan in docs/specs/". This approves building and testing the spec offline; owner questions take their safe defaults. It does not open any live gate (G1–G5), so live acceptance criteria stay pending.
**Release:** R6 (AUTO-012 completion)
**Owner gate:** G2 (scheduled execution uses the same authorization model)
**Depends on:** SPEC-05
**Implements roadmap item:** "Dashboard and alerts are offline ... nothing sends alerts or runs them on a schedule" ([known-limitations.md](../baseline/known-limitations.md))

## Problem

`sdk/reports/alerts.py` evaluates alert rules and `run.py --report dashboard` renders a page, but nothing runs them after a live run or tells the operator. `run.py` already has an SMTP log-delivery path (`run.py:68`).

## Scope

- **In:** a post-run step that evaluates alerts and writes the dashboard, plus optional delivery through the existing SMTP path.
- **Out:** new notification services, new secrets in validation workflows, and paging.

## Requirements

- **S11-R1 (MUST)** Every `--fleet-execute` run ends by evaluating alerts on its journals and audit, writing them to the run record, and writing the dashboard next to the journals.
- **S11-R2 (MUST)** A critical alert sets a non-zero exit code after all accounts have finished, so a scheduler can see it.
- **S11-R3 (SHOULD)** With the existing SMTP flags configured, a run sends one summary e-mail listing alerts by severity, scrubbed like other output. Without them, nothing is sent.
- **S11-R4 (MUST)** Alert and summary text pass the same leak scan as the fleet dry run (no credentials, salt, raw ids or player names).
- **S11-R5 (MAY)** A scheduled workflow runs the authorized execute command; it is written and owned by the owner, never by an agent, because it holds secrets.

## Interfaces

- `run.py --fleet-execute ... --dashboard-out PATH`, reusing `sdk/reports/dashboard.py` and `sdk/reports/alerts.py`.
- SMTP delivery reuses `run.py`'s existing function and flags.

## Safety invariants

- No new secrets in validation workflows.
- Scrubbed text.
- Delivery is optional and off by default.

## Acceptance criteria

| Id | Proves | Kind | Criterion |
| --- | --- | --- | --- |
| S11-AC1 | R1, R2 | offline | A fake execute run writes alerts and the dashboard; a critical alert gives a non-zero exit after all accounts ran |
| S11-AC2 | R3, R4 | offline | With a fake SMTP server, one scrubbed summary is sent; without SMTP flags, none |
| S11-AC3 | R1–R4 | live | The owner sees alerts and the dashboard after the SPEC-07 canary |

## Test plan

Extend `tests/test_fleet_execute.py`; a fake `smtplib.SMTP` as the existing SMTP tests use.

## Slices

| Slice id | Requirements | Files | Kind |
| --- | --- | --- | --- |
| `spec11-post-run-alerts` | R1–R4 | `run.py`, tests, `README.template`, `README.md`, `docs/baseline/alerts.md` | agent |
| `spec11-schedule` | R5 | a new workflow | **owner** |

## Owner decisions

| Id | Question | Needed before | Safe default |
| --- | --- | --- | --- |
| S11-Q1 | Should summaries be e-mailed? | `spec11-post-run-alerts` | No |
| S11-Q2 | Should execution be scheduled at all? | R5 | No; the owner runs it by hand |

## Evidence

**Offline verification:** every MUST with an offline acceptance criterion is covered by a test that cites it (checked by `scripts/spec_trace.py`). Implemented in #173.

**Live evidence (S11-AC3, pending the owner's confirmation):** after each G2 canary of 2026-11-12, alerts were evaluated into the run record (one `no-verified-action` warning) and the dashboard was written next to the journals: [G2 evidence](../experiments/live/g2-evidence.md). The spec is `Done` once the owner confirms seeing them.

## Open questions and risks

None known.
