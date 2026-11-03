# Tachikoma Product Roadmap — Full Gameplay Automation Research

**Roadmap version:** 2.0
**Updated:** 2026-11-03
**Horizon:** November 2026–June 2027, then continuous experimentation
**Status:** Proposed work and research plan; release completion must be established from GitHub and verified evidence
**Repository:** [raelldottin/tachikoma](https://github.com/raelldottin/tachikoma)

> **North star:** Determine how effective full Pixel Starships gameplay automation can become at progressing new accounts and winning PvP battles, and quantify the benefits, costs, risks, and failure modes versus manual gameplay.

## 1. Product mission and research premise

The project owner reports **SavySoda authorization for testing the impact of full-gameplay automation**. Tachikoma's research program therefore includes the full game loop: account state acquisition, resource operations, crew development, research, upgrades, crafting, combat preparation, PvP, market/economy interactions, and orchestration across owned accounts.

**No gameplay activity is categorically excluded from the product roadmap.** PvP execution, unattended progression, farming, marketplace transactions, multi-account scheduling and direct game integration are candidate capabilities to develop and evaluate. The product must measure how effective they actually are rather than assuming automation is inherently beneficial.

The owner-provided scope description is the basis for this roadmap. The actual SavySoda correspondence was not independently available during drafting, so its precise wording, contacts, dates, limits and any special terms are not asserted as fact. Keep the source approval in private project records. This is a research-planning distinction, not an artificial ban on automation categories.

The product should answer:

- How much faster do new ships progress under automation than with a manual routine?
- What fraction of useful gameplay actions can be completed reliably without intervention?
- Which crew, room, research, equipment and AI investments generate the highest measurable PvP improvement?
- Can automated battle strategy adapt to hull, opponent characteristics, balance patches, and losses?
- How do market/economy strategies affect resources, inventory availability and account growth?
- Does operating several ships concurrently or sequentially increase throughput, and at what cost?
- What is the operational cost (compute, models, resources, failures) per unit of progression and per net PvP gain?

**Strategic outcome:** An instrumented system that develops, plays, analyzes, and continually optimizes owned Pixel Starships accounts, with reproducible evidence of results.

## 2. Current repository baseline (verified versus proposed)

The public default branch was reviewed while drafting this roadmap. Code exists, but test presence or historical successful execution does not establish current gameplay compatibility or shipping readiness.

| Capability or artifact | Present repo evidence | Plan |
| --- | --- | --- |
| Legacy game automation entrypoint | `run.py` | Assess current functionality and integrate with a reliable account scheduler |
| Protocol and client transport | `sdk/client.py`, `sdk/device.py`, `sdk/commands.py` | Version endpoint capabilities and verify state outcomes |
| Crew development formulas | `sdk/crew_leveling.py`, tests | Reuse and update for current balance and progression decisions |
| Game calculation library | `sdk/game_formulas.py` | Expand verified strategy calculations |
| Ship-layout evaluation | `sdk/ship_layout.py` | Improve room, armor, power and repair optimization |
| PvP/battle flow models and tests | `tests/test_pvp_battles.py`, `tests/test_battle_flow.py` | Integrate combat evaluation, match strategy and empirical outcomes |
| CI and autonomous development | `.github/workflows/`, `automation/`, `AGENTS.md` | Preserve reproducibility, add full-gameplay integration test coverage |
| Historical research and captures | Repo documentation and prior Hermes discussions | Reconcile against tracked branches before treating as implemented |

Prior Hermes Task D–J/J-M and GOAL-PVP-001 results have been discussed, but **their completion and integration into main were not verified**. GOAL-PVP-001's verifier was previously blocked by a Hermes managed-worker issue. Inspect its handoffs and commits before recreating or claiming those capabilities.

An earlier operator assessment reported exposed game credentials in public history. Credential rotation, token invalidation and history cleanup status are still to be confirmed by the maintainer. New test sessions should use secure current authentication, not known-exposed material.

## 3. Product pillars

### A. Account intelligence

Maintain current, account-scoped snapshots of hull level, trophies, crew, rooms, layout, ammunition, research, inventory, equipment, resources, game version and native AI configuration. Track observation time and source, distinguish complete from partial states, and prevent cross-account identity mix-ups.

### B. Autonomous progression

Develop decision policies and action orchestration for collection, resource management, crew training, healing, room upgrades, research, crafting, equipment, inventory management, maintenance and hull advancement. Coordinate prerequisites, cooldowns, scheduling, opportunity cost and the effect of actions on combat readiness.

### C. Competitive PvP

Optimize ship layouts, target selection, defensive resilience, crew specialization, weapon operation, boarding, shields, repairs and native combat AI. Evaluate automated preparation, battle initiation, outcomes and subsequent policy changes. Separate offensive from defensive outcomes.

### D. Game economy

Study minerals, gas, items, crafting requirements, market prices, transaction fees and inventories across accounts. Evaluate market listings, purchases and inventory movement with exact listing/transaction identity and economic reconciliation. Compare the economic return of different strategies.

### E. Multi-account orchestration

Develop separate account goals and a scheduling layer able to compare independent, sequential and concurrent operation. Measure how a portfolio of ships progresses without confusing credentials, state, resources, experiments or combat records. Assess orchestration strategies empirically.

### F. Adaptive learning

Feed observed game state and battle/economic outcomes back into recommendations and decision policies. Compare strategies, discover failure modes, update rules after patches and retain rollback-capable versions of policies. Optimize for long-term outcomes, not just the number of requests sent.

## 4. Releases and target windows

Windows are planning targets; exit criteria depend on working demonstrations and evidence rather than calendar dates.

| Release | Target | Primary deliverable | Exit evidence |
| --- | --- | --- | --- |
| **R0 — Baseline and experimental design** | Nov 2026 | Existing-system audit, research scope ledger, reproducible test accounts and initial metrics | Correct baseline for each ship and experiment plan |
| **R1 — Reliable game state and execution** | Nov–Dec 2026 | Account snapshots, lifecycle, typed commands, state reconciliation and observability | Repeatable verified operations with attributed outcomes |
| **R2 — Automated growth engine** | Dec 2026–Jan 2027 | Resource collection, crew, research, crafting, upgrades and scheduling | Measured account progression with reliability and resource accounting |
| **R3 — PvP automation engine** | Jan–Feb 2027 | Combat preparation, native AI policies, PvP runs, defense assessment | Win/loss/trophy evidence broken down by opponent cohort |
| **R4 — Marketplace and economy optimization** | Feb–Mar 2027 | Item/market execution models, resource allocation and economic experiments | Reconciled items, currency and efficiency across operations |
| **R5 — Multi-account orchestration** | Mar–Apr 2027 | Coordinated scheduling, differentiated account strategies and comparative experiments | Measured fleet throughput, isolation and operator time savings |
| **R6 — Adaptive optimization and release** | Apr–Jun 2027 | Data-driven strategy tuning, dashboards, alerts, durable test harness and releases | Reproducible improvements with known costs and failure cases |
| **Post-R6** | Continuous | New game-version support, strategy comparison, richer telemetry and optimization | Evidence-backed gains under new patches and account conditions |

### R0 — Baseline and experimental design

Produce a factual map of what current `main`, historical branches and Hermes workspaces actually contain. Record a profile for every participating new ship: hull, crew, layout, research, resource balances, current PvP standing and baseline gameplay schedule. Capture game build and test provenance. Preserve the reported SavySoda approval as private source evidence and document its actual terms without guessing.

Define manual and automated comparison groups or repeated before/after trials. Track the workload of daily maintenance and manual game time. Specify outcome metrics and how to compare ships of different hull levels fairly.

**Exit:** a reproducible baseline, test-account dataset, observed game version, validated data schema, current-test-capability inventory and defined initial experiments.

### R1 — Reliable game state and execution

Build an account-aware state provider and typed gameplay action catalog. Determine which current game endpoints correspond to desired operations, which fields represent specific items and ships, and how to reconcile success against observed game state. Replace ad hoc assumptions with explicit request/response and postcondition contracts.

Create a scheduler, event log, state-delta capture, command status model and operational dashboard. Reuse existing verified authentication and client code where suitable; keep secret material outside tracked artifacts.

**Exit:** multiple representative operations can be scheduled, attributed to the correct ship, observed, and checked for their actual effects. Errors, timeouts and uncertain outcomes are represented explicitly.

### R2 — Automated growth engine

Develop progression policies that rank available actions against resources, dependencies, cooldowns and PvP readiness. Include daily collection, crew leveling/training, healing, research, room upgrades, crafting, modules/equipment and resource reserves. Model hull-upgrade timing as a testable strategy rather than a universal rule.

Compare manual daily routines with automated policies over comparable periods. Analyze minerals/gas gained, account development, unproductive spending and unattended execution reliability.

**Exit:** measurable gains in progression tasks completed, operator time saved, and/or resource utilization, with transparent costs and a failure ledger.

### R3 — PvP automation engine

Build a combat policy layer that evaluates ship configurations, native room/crew AI, power allocation, targeting, repair support, boarding and match preparation. Evaluate PvP run management and outcome recording against matched opponent brackets.

Compare at least two candidate strategies with preserved configuration versions and clearly defined experimental windows. Track wins, losses, defensive results, trophies, duration and resource costs. Distinguish an HTTP response from a genuine game outcome.

**Exit:** reproducible battle trials, offense/defense scorecards, opponent-normalized comparisons and an evidence-based recommendation for the next strategy.

### R4 — Marketplace and economy optimization

Develop an economic model for item demand, crafting ingredients, minerals/gas, marketplace listings and purchases. Reuse prior Task J-M's insight that the visible client used market-mediated transfers, but confirm it against current repository code and captures before treating it as a production contract.

Correlate listing creation, discovered sale/listing identity, purchase, account inventory delta, price, fees and seller proceeds. Compare economic strategies and their effects on account growth.

**Exit:** successful reconciliation for representative market/economy experiments, meaningful efficiency comparisons, and no claims of completed transfers based only on listing creation.

### R5 — Multi-account orchestration

Add account-scoped objectives, scheduler isolation, resource budgets, state ownership, action histories and fleet-wide reporting. Compare independently optimized ships with shared planning policies, and evaluate sequential versus concurrent operation by throughput, failure rate, resource results and competition outcomes.

**Exit:** consistent multi-ship metrics, no unintended cross-account execution or data mix-ups, reproducible workload comparisons, and measurable operator time savings.

### R6 — Adaptive optimization and release

Implement a policy evaluation loop: baseline, new hypothesis, instrumented action, observed outcome, comparison, decision and versioned rollout. Generate reports for ship growth and PvP outcomes. Provide CLI and local dashboard usability, test fixtures, operational diagnostics and release instructions.

**Exit:** repeatable full-gameplay scenarios, clean releases, regression tests, documented known limitations, and a study report quantifying when automation helped, failed or was more expensive than manual play.

## 5. Prioritized epic backlog

These IDs are planning references, not claims that existing GitHub issues have been created.

| Epic | Priority | Focus | Owner lane / dependency |
| --- | --- | --- | --- |
| **AUTO-001** | P0 | Verified code and Hermes baseline, game/version provenance | Maintainer, verifier |
| **AUTO-002** | P0 | Multi-account identity, state and inventory snapshots | Protocol, architect / AUTO-001 |
| **AUTO-003** | P0 | Gameplay action catalog, postconditions and reconciliation | Protocol, safety / AUTO-002 |
| **AUTO-004** | P0 | Autonomous daily progression scheduler | Architect, analyst / AUTO-003 |
| **AUTO-005** | P0 | Crew/ship/research upgrade policy engine | Analyst, researcher / AUTO-002 |
| **AUTO-006** | P0 | Native combat AI and PvP strategy library | Researcher, analyst / AUTO-005 |
| **AUTO-007** | P0 | PvP execution and battle-result evidence | Protocol, architect / AUTO-003, AUTO-006 |
| **AUTO-008** | P1 | Market/item/economy experimentation | Analyst, protocol / AUTO-003 |
| **AUTO-009** | P1 | Multi-account orchestration and throughput study | Architect, safety / AUTO-004, AUTO-007 |
| **AUTO-010** | P0 | Controlled manual-vs-automation study design | Analyst, verifier / AUTO-001 |
| **AUTO-011** | P1 | Adaptive policy iteration and optimization | Analyst, architect / AUTO-007, AUTO-010 |
| **AUTO-012** | P1 | Dashboard, audit and reporting UX | Architect / AUTO-002, AUTO-010 |
| **AUTO-013** | P0 | Security and account-secret remediation | Safety, maintainer / parallel |
| **AUTO-014** | P0 | Comprehensive test and reliability harness | Verifier, architect / across releases |
| **AUTO-015** | P1 | Patch-aware rules and compatibility verification | Researcher, protocol / across releases |

**First three implementation slices:** (1) validate the current system and account baselines; (2) prove reliable account-specific state and action reconciliation; (3) connect a daily progression policy to the execution and measurement loop. Do not rebuild existing functionality without auditing it.

## 6. Experimental design

The study should compare at least the following modes, using matched hulls/ability levels or statistical adjustment where matching is imperfect:

- **Manual control:** a documented human routine with observed time and outcomes.
- **Rule-based automation:** stable deterministic schedules and progression policies.
- **Adaptive automation:** policies revised from feedback, optionally using AI agents for planning and review.
- **Multi-account operational variants:** independent execution versus coordinated scheduling, sequential versus concurrent, differentiated versus common strategies.

Use baseline windows, explicit configuration revisions, patch versions and comparable opponent cohorts. Track interventions such as new legendary crew, premium spending or unexpected game updates. Include failed tasks and losses, not only successful runs. State sample size and uncertainty.

**Example experiment:** For two ships at comparable hull levels, establish initial crew and trophy baselines; run a documented manual routine for one comparison period and an automated progression policy for another; compare progression and PvP after normalizing for time, resources and opponent strength. Treat the outcome as preliminary if the accounts or schedules are not genuinely comparable.

## 7. Product scorecard

| Metric | Definition | Why it matters |
| --- | --- | --- |
| Account growth velocity | Verified upgrades, crew levels and research progress per elapsed day | Core progression outcome |
| Attack win rate | Wins / known-result attacks in a comparable cohort | Offensive PvP effectiveness |
| Defense win rate | Wins / known-result defenses in comparable conditions | Defensive ship resilience |
| Net trophies | Trophy gains minus losses in a stated interval | Competitive movement |
| Resources per hour | Useful minerals/gas gained per elapsed hour | Economic efficiency |
| Market return | Net realized item/resource gain minus purchase and transaction costs | Trading/economy performance |
| Automation coverage | Completed game actions without manual intervention / intended actions | Breadth of autonomous capability |
| Verified action success | Operations with confirmed postconditions / attempted operations | Execution reliability |
| Time saved | Human active minutes avoided per ship per week | Operational value |
| Cost per progression unit | Resources, compute and model cost / verified achievement | Net usefulness of automation |
| Incident/failure frequency | Failed, duplicated, misattributed or unreconciled actions per test period | Research quality and reliability |

Report sample size, game version and data provenance alongside every headline metric. Set numerical improvement targets after collecting baselines; do not invent current win rates or claim unverified gains.

## 8. Architecture direction

```text
account-specific game state + versioned rules + experiment configuration
                                |
                        validated snapshots
                                |
                 strategy / progression policies
                                |
                    multi-account scheduler
                                |
                gameplay action adapter / client
                                |
                 observed response + game state
                                |
               reconciliation and event journal
                                |
             PvP, progression and economy metrics
                                |
             evaluator / policy revision / reports
```

- **Domain layer:** typed ship, room, crew, inventory, research and battle models; pure deterministic recommendation functions.
- **Execution layer:** game-client compatibility, typed actions, per-account state machines, scheduling, cooldown and retry logic based on observed operation semantics.
- **Evidence layer:** structured event records with account alias, game version, action ID, expected effect, observed effect, costs and confidence.
- **Experiment layer:** policy versions, manual and automated cohorts, configuration revisions, opponent comparisons and reports.
- **Operator layer:** CLI/local dashboard, configuration, intervention, review and observability.

Keep the domain and live transport separate to support repeatable offline simulation and real-environment validation. Model ambiguity explicitly: a successful request is not necessarily a completed gameplay action. Design retries based on proven idempotency and reconciliation rather than guessing.

## 9. Gameplay compatibility and evidence

Maintain a versioned game-rule source ledger, distinguishing official released mechanics, official announcements, community observations, static protocol discoveries and dynamically demonstrated behavior. Relevant recent anchors include [Hull 14 notes (2026-06-17)](https://blog.pixelstarships.com/2026/06/17/hull-14-major-update-patch-notes-v0-999-55/) and [Pixel Starships 1.0 preview (2026-10-16)](https://blog.pixelstarships.com/2026/10/16/sneak-peek-pixel-starships-1-0/).

For the observed marketplace path, prior operator-provided capture analysis identified `SellItem3`, `ListActiveMarketplaceMessages5`, and `Purchase4` with an exact `SaleId`. It did **not** establish a live `GiftItem` route or fully reconciled reciprocal trade. Treat this as historical test evidence to independently reconcile, not proof that every endpoint remains current.

For battles, classify evidence separately for request shape, battle acceptance, finalization, server outcome and actual competitive performance. Do not promote one successful protocol call into a claim of automation effectiveness.

## 10. Quality, reliability and security engineering

Engineering safeguards support reproducibility and protect the test accounts; they are **not categorical bans on gameplay-automation research areas**.

- Protect authentication material using secret storage and redaction. Never commit passwords or bearer/refresh tokens; replace credentials previously reported exposed.
- Keep versioned code and validated fixtures in CI; preserve synthetically mocked unit tests alongside separately analyzed real test results.
- Reconcile each gameplay action with observed state; record timeouts, duplicate operations, incomplete responses and negative results.
- Preserve account ownership and identity throughout execution so no ship receives another account's actions, resources or metrics.
- Record requested and realized resource expenditure, including premium spending where actually studied.
- Document specific SavySoda approval correspondence privately rather than inventing its provisions in a public file.
- Maintain runtime observability and the ability for the test operator to halt experiments or recover from faults.
- Give verifier evidence, tests, and failures the same priority as new feature implementation.

**Definition of done:** acceptance tests against representative fixtures; full repo test/lint requirements; live evidence where relevant; exact post-state reconciliation; performance metrics with limitations; no committed secrets; independent review; and an updated roadmap status.

## 11. Principal dependencies, risks and decisions

| Decision / risk | Required action |
| --- | --- |
| Reported SavySoda full-automation approval | Preserve actual grant privately and use its terms as factual research context; do not invent limits |
| Publicly exposed credentials reported previously | Verify rotation/revocation and clean test credentials; don't reuse compromised values |
| GOAL-PVP-001 verifier/merge status uncertain | Reconcile the local task evidence against main before duplicating implementation |
| Patch-dependent mechanics and old formulas | Maintain version/patch evidence and regression tests |
| PvP matching and sample-selection bias | Normalize by opponent cohort and report uncertainty |
| Long-running activity and failure loops | Build event journal, reconciliation, fault recovery and an operator stop |
| Cross-account state or execution confusion | Typed profile isolation, per-account audit and verification |
| Resource/premium cost of experiments | Measure costs and economic value rather than equating automation activity with success |

## 12. Operating cadence and ownership

**Weekly:** inspect per-ship growth, resource budgets, PvP results, failed actions and top policy changes. **Biweekly:** ship one or two vertical features and test their impact. **Monthly:** publish a comparative automation-effectiveness report with account growth, battle results, operator effort and operating cost.

Hermes owner lanes: `tachikoma-researcher` researches balance and strategic hypotheses; `tachikoma-protocol` reconciles observed client operations; `tachikoma-analyst` models progression and experiments; `tachikoma-architect` integrates execution and decision layers; `tachikoma-safety` audits account state, credential handling and experiment reliability; `tachikoma-verifier` challenges results; `tachikoma` synthesizes.

## 13. Next milestone

**AUTO-001 + AUTO-002 + AUTO-003:** establish validated multi-account state, current code capability map and one verified gameplay action flow. From that foundation, implement the first reproducible autonomous daily progression sequence with actual progress metrics, then proceed to PvP strategy tests and the economic experiments.

**Acceptance:** a participating test ship has a documented starting state; Tachikoma performs and reconciles a sequence of representative gameplay actions; progression, operator time and failures are measured; and the evidence is sufficient to compare automation with manual operation.

## 14. Sources and maintenance

Repository source files reviewed at initial planning: [README.template](../README.template), [AGENTS.md](../AGENTS.md), [client](../sdk/client.py), [crew](../sdk/crew_leveling.py), [layout](../sdk/ship_layout.py), [formulas](../sdk/game_formulas.py), [PvP tests](../tests/test_pvp_battles.py), [automation harness](../automation/README.md).

External references: [Pixel Starships official blog](https://blog.pixelstarships.com/), [Hull 14 notes](https://blog.pixelstarships.com/2026/06/17/hull-14-major-update-patch-notes-v0-999-55/), and [1.0 preview](https://blog.pixelstarships.com/2026/10/16/sneak-peek-pixel-starships-1-0/). The owner reports a separate SavySoda test authorization; its document was not supplied or verified here.

Keep this roadmap current with code that has actually merged, game releases, experiment results, and changes in the research mandate. **Full gameplay automation is the product objective; offline-only operation is no longer the governing restriction.**
