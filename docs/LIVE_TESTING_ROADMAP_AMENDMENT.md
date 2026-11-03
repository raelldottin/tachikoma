# Tachikoma roadmap amendment: full-gameplay automation research

Updated: November 3, 2026

## Research mandate

The project owner states that SavySoda has approved a test program to **evaluate the impact and effectiveness of full Pixel Starships gameplay automation**. This amendment supersedes the offline-only and limited-live-test positioning in [PRODUCT_ROADMAP.md](PRODUCT_ROADMAP.md). The goal is to build, test, measure, and optimize an end-to-end gameplay automation system, not merely an offline ship advisor.

The reported approval scope is broad gameplay automation. Consequently, this roadmap does **not** categorically exclude automated PvP, resource collection, crew management, research, upgrades, crafting, marketplace workflows, or orchestration across the owned test accounts. Those are research workstreams rather than prohibited product features. The actual grant has not been provided in this repository; its terms should be retained privately as research provenance, without publishing credentials or confidential correspondence.

## Product hypothesis

An autonomous, policy-driven Tachikoma can progress new ships faster and improve PvP effectiveness compared with manual operation, while reducing the user's daily maintenance workload. That is a hypothesis to measure, not a promised result.

The core product combines:

- **Account state intelligence:** current hull, resources, rooms, research, inventory, crew, equipment, native AI, and trophies.
- **Progression decisions:** ranked actions, constraints, prerequisites, budgets, training and upgrade schedules.
- **Gameplay execution:** end-to-end orchestration of collection, training, research, upgrades, combat preparation, PvP, and supported market workflows.
- **Experimentation:** manual-vs-automated and strategy-vs-strategy comparisons with identical evaluation criteria.
- **Feedback optimization:** battle outcomes, growth efficiency, error rates and operating cost inform each new policy.
- **Multi-account research:** independent account strategies, concurrent versus sequential scheduling, and portfolio-level efficiency evaluation.

Existing `run.py`, `sdk/client.py`, crew formulas, ship layout analysis, battle tests and Hermes research should be audited and reused where accurate.

## Updated releases

| Phase | Product outcome | Exit evidence |
| --- | --- | --- |
| **R0 — Research baseline** | Inventory existing code, recover verified Hermes output, document game/version and account baselines | Reproducible baseline for each participating test ship |
| **R1 — Reliable account state** | Authenticated account snapshots, session handling, state and inventory reconciliation, patch-aware data | Repeatable accurate snapshots across test accounts |
| **R2 — Progression automation** | Resource collection, crew training, room/research upgrades, crafting, inventory strategy and scheduling | Measured progression per day, resource efficiency and execution success |
| **R3 — PvP automation** | Ship configuration, native AI strategy selection, battle preparation, battle runs and outcome capture | Quantified attack/defense performance by opponent cohort |
| **R4 — Market and resource economy** | Measure listing, purchasing, inventory movement, spend efficiency and resource redistribution strategies | Correlated market and inventory outcomes, fees, loss and opportunity costs |
| **R5 — Multi-account experiment orchestration** | Evaluate independent account policies, scheduling strategies and fleet-level progression | Reliable account isolation, measured throughput and comparative results |
| **R6 — Adaptive optimization and release** | Policy tuning, comparative analytics, operator dashboard, reports and regression suite | Repeatable improvements with transparent confidence, costs and failure modes |

The previous roadmap's offline components become **analysis modules inside this broader automated system**, not an upper bound on its capabilities.

## Priority epics

- **AUTO-001 Account baseline and state ingestion:** versioned profile schema, account provenance and current-game compatibility.
- **AUTO-002 Gameplay action reliability:** action contracts, observed server/game outcomes, retry and reconciliation semantics.
- **AUTO-003 Daily progression engine:** resource, crew, research, crafting and upgrade prioritization and execution.
- **AUTO-004 PvP strategy and battle automation:** native combat AI strategies, opponent-cohort analysis and battle outcome collection.
- **AUTO-005 Economic strategy experiments:** mineral/gas flows, inventory demand and marketplace effectiveness.
- **AUTO-006 Multi-account scheduling:** compare throughput, isolation and account-specific policies.
- **AUTO-007 Experimental design:** manual baseline, automated treatment, matched samples, counterexamples and patch changes.
- **AUTO-008 Observability and optimization:** time saved, progression velocity, trophy growth, win rates, resource ROI and operational failure rates.
- **AUTO-009 Operator UX and reproducible releases:** dashboard, reports, configuration, test fixtures and documentation.

These are proposed epics, not claims of completed implementation.

## Study design and measurements

Measure changes against the same baseline wherever practical:

| Metric | Operational definition |
| --- | --- |
| Progression velocity | Ship/room/crew/research objectives completed per elapsed day |
| Attack and defense effectiveness | Wins divided by known-result battles, split by role and opponent bracket |
| Trophy performance | Net trophy change per week and per battle |
| Resource productivity | Minerals/gas acquired and usefully invested per hour |
| Economic efficiency | Item/resource gain less market fees, purchase costs and losses |
| Automation coverage | Completed gameplay tasks without manual intervention divided by planned tasks |
| Execution reliability | Verified successful operations divided by attempted operations |
| Operator time saved | Measured minutes of manual effort avoided per account per week |
| Operating cost | Compute, model, and service cost per account and per verified achievement |

Log dataset size, confidence, changes in opponent strength and game version. Do not infer success from HTTP status alone; reconcile against game state. Use simulation and synthetic tests for continuous integration, with live experiments providing separate empirical evidence.

## Engineering program

Keep strategic logic testable independently of transport. Add a deterministic decision engine, action scheduler, state machine for operations, per-account isolation, event log, cost accounting, battle-results database and experiment evaluator. Expand endpoint support as justified by game behavior, and regression-test the implementation against sanitized evidence.

Maintain security hygiene for test accounts and logs: never commit account passwords or tokens, avoid reusing credentials previously exposed in Git history, and preserve private provider correspondence outside the public repository. These practices protect research validity and secrets; they are **not categorical exclusions of gameplay-automation features**.

## Immediate work

1. Reconcile GOAL-PVP-001 verifier and current merged code against the roadmap.
2. Capture starting metrics for the participating new accounts.
3. Verify current authentication and gameplay state pipelines; reuse the existing tested components.
4. Implement the first end-to-end automated daily progression workflow and measure its outcomes.
5. Expand into PvP automation and controlled strategy comparisons, then resource-economy and multi-account experiments.
6. Publish a reproducible study report on how effective full gameplay automation actually was, including failures and trade-offs.

**Governance of this amendment:** User-reported SavySoda approval authorizes the research direction described here. The repository should not invent details of the provider's correspondence. Any concrete conditions actually stated in that grant remain relevant factual inputs to the experiment design. The intended roadmap has no blanket gameplay-category bans.
