# Tachikoma Product Roadmap

**Version:** 1.0 (planning baseline)
**Updated:** 2026-11-03
**Planning horizon:** November 2026–June 2027; later work is directional
**Status:** Proposed roadmap, not a statement that future capabilities have shipped
**Primary product:** Offline, evidence-based Pixel Starships progression and PvP decision support
**Repository:** [raelldottin/tachikoma](https://github.com/raelldottin/tachikoma)

> **North star:** Help each ship make the next best legal, measurable investment in its crew, layout, research, equipment, and in-game combat AI—then demonstrate whether the change improved performance against comparable opponents.

## 1. Executive decision

Pivot Tachikoma's *new product development* away from expanding authenticated gameplay automation, credential reverse engineering, and market-transfer execution. Build a **local-first competitive ship advisor** that imports sanitized or manually entered game data, explains strategic weaknesses, recommends changes a player can make in the official client, and measures the results of human-played battles.

The existing automation code is legacy surface area to inventory, isolate, and make an explicit maintenance/retirement decision about—not automatically a roadmap commitment. Do not expand or re-enable it through this roadmap.

**Why now:** A working network endpoint is not itself a product outcome. Competitive improvement needs account-level baselines, a reasoned progression plan, and battle evidence. The game also evolves: the June 2026 Hull 14 update changed combat options; the October 2026 1.0 preview announced further equipment and training changes. Hard-coded recommendations are likely to become stale without versioned rule sets ([official Hull 14 notes](https://blog.pixelstarships.com/2026/06/17/hull-14-major-update-patch-notes-v0-999-55/); [official 1.0 preview](https://blog.pixelstarships.com/2026/10/16/sneak-peek-pixel-starships-1-0/)).

### Non-negotiable product boundary

Pixel Starships' [EULA](https://www.pixelstarships.com/EULA) and [fair-play statement](https://blog.pixelstarships.com/2022/09/12/fair-play-and-automation/) prohibit scripts or automation that confer an unfair advantage, explicitly calling out automated market interactions, item transfers, and tools controlling multiple accounts. Tachikoma's *new* features must be **offline/read-only decision support**. The user makes each gameplay change using the official game UI and its built-in crew/room AI.

No autonomous PvP battles, bot farming, scripted market trades, simultaneous account control, token extraction, premium purchases, or live gameplay mutations. A user-controlled dashboard that *recommends* an in-game AI rule is in scope; a bot that *applies* it is not.

## 2. Who the product serves and what it does

| User / job | Problem | Promised outcome |
| --- | --- | --- |
| Owner of a developing ship | Too many possible upgrades, limited minerals/gas/time | Ranked, cost-aware seven-day and 30-day growth plan |
| Competitive PvP player | Repeated losses without diagnosis | Evidence-based attack/defense weaknesses and native-AI suggestions |
| Player managing several ships | Different hulls and progression stages are easy to conflate | **Independent** private profiles and apples-to-apples comparisons; no coordinated account control |
| Maintainer / tester | Old protocol research and fragile runtime behavior obscure product progress | Pure strategy engine, synthetic fixtures, reproducible tests, transparent evidence status |

**Jobs to be done:** “What should I upgrade next?”, “Why did that defense fail?”, “Should I level the hull?”, “Which crew fits this weapon/repair role?”, “What native AI condition should I try?”, and “Did last week's changes help?”

**Positioning:** A competitive ship planning workbench, not an unattended Pixel Starships client.

## 3. Current-state inventory and evidence boundaries

The following exists on GitHub **main at roadmap drafting time**; it is not a claim that all features are correct or game-version-current.

| Area | Existing repo evidence | Roadmap disposition |
| --- | --- | --- |
| Legacy app and command flow | `run.py`, `sdk/client.py`, `sdk/commands.py`, TUI | Audit; separate offline strategy from authenticated execution |
| Crew analysis | `sdk/crew_leveling.py`, `tests/test_crew_leveling.py` | Reuse verified pure formulas; validate against current game balance |
| Game formulas | `sdk/game_formulas.py`, `tests/test_game_formulas.py` | Make versions/assumptions explicit; test boundary cases |
| Ship-layout evaluation | `sdk/ship_layout.py`, `tests/test_ship_layout.py` | Evolve heuristic scoring into explainable, confidence-labeled recommendations |
| PvP/battle tests | `tests/test_pvp_battles.py`, `tests/test_battle_flow.py` | Use only offline/mocked fixtures for new analytics; tests are not proof of live competitive wins |
| CI and agent harness | `.github/workflows/`, `automation/`, `AGENTS.md` | Retain safety gates and reproducibility; no mandatory live gameplay tests |
| Existing product documentation | `README.template`, `README.md`, `docs/auth-operations.md` | Roadmap becomes strategic source of truth; historical auth docs are not future-feature mandates |

**Not verified as merged into main:** the prior Hermes Kanban inventory Tasks D–J / Task J-M, and GOAL-PVP-001. Their results may exist in local workspaces, branches, or reports. The last operator-provided status for the PvP goal (October 28) showed its verifier blocked by Hermes managed-worker infrastructure, **not a passed release gate**. Reconcile commit hashes, tests, provenance, and review gates before treating that work as shipped. Likewise, do not assume the repo's old crew training caps remain correct after future 1.0 changes.

**Security debt:** A previous operator report identified publicly exposed account credentials/tokens. Rotation, invalidation, and public-ref cleanup were requested but their completed status has **not been independently verified here**. Keep incident remediation urgent and isolated; never use those credentials for roadmap development. Offline planning should not be blocked by the security incident, while any network-connected legacy execution stays disabled until separate authorization/compliance/security review.

## 4. Product principles

1. **Evidence before optimization.** State the input source, capture date, game version, and what is unknown. Never invent item inventory, levels, battle results, or opponent details.
2. **Improve an account, don't merely accumulate trophies.** Compare against similar opponent hulls, ship configurations, and battle types. Track attack and defense separately.
3. **One recommendation, one measurable hypothesis.** Every suggested action has a reason, estimated resource/time cost where known, expected mechanism, risk, and validation method.
4. **Hull upgrades are gates, not a race.** Readiness is a transparent checklist; do not give false precision or guarantee winning.
5. **Pure computation, human execution.** Imports and analytics are local/offline; gameplay decisions are performed in the game client.
6. **Separate each ship.** Pseudonymous account identity with strict data isolation; no account coordination or market manipulation.
7. **Version every game assumption.** Rules are tagged by patch, with explicit unknown/announced/published/observed status.
8. **Secure by default.** No credentials in fixtures, reports, logs, repository files, or telemetry.

## 5. Roadmap at a glance

Time boxes below are **prioritization targets**, not commitments. Move a phase only when its exit criteria are met.

| Release / target window | User-facing outcome | Priority | Exit gate |
| --- | --- | --- | --- |
| **R0 Foundation & trust** · Nov 2026 | Honest baseline, security separation, source-of-truth requirements | P0 | Product contract, evidence ledger, non-mutation tests and status audit |
| **R1 Ship Profile MVP** · Nov–Dec 2026 | Import/enter ship and crew state, view weaknesses without live login | P0 | Two sanitized sample profiles + deterministic analysis + unknown handling |
| **R2 Progression Planner** · Dec 2026–Jan 2027 | Ranked crew/room/research upgrades and hull readiness for each ship | P0 | Explainable 7/30-day plan, budget constraints, no invented costs |
| **R3 Native AI & Build Lab** · Jan–Feb 2027 | Compare candidate layouts/crew roles and prepare human-applied native AI rules | P1 | At least two build strategies compared with clear assumptions |
| **R4 PvP Results & Experiments** · Feb–Mar 2027 | Log human-played outcomes and identify recurring attack/defense failure patterns | P0 | Comparable-battle cohorts, uncertainty, before/after experiments |
| **R5 Portfolio Workbench** · Mar–Apr 2027 | One offline view across independent ships and their next actions | P1 | Per-ship isolation, private local data, prioritized work queue |
| **R6 Polished Reports & Release** · Apr–Jun 2027 | Usable CLI/local HTML reporting, exports, docs, and reproducible releases | P1 | End-to-end acceptance, docs, privacy/security review |
| **Later / conditional** | Deeper replay analysis, optimization experiments, community-contributed strategy packs | P2 | Proven demand, legally permissible data, enough validation samples |

### R0 — Foundation & trust (P0)

**Deliver:** a `roadmap-status` inventory of code and unfinished Hermes artifacts; repo release baseline and test commands; patch/version ledger; documented offline boundary; signed-off security-isolation policy; sanitized example dataset.

**Work:** review legacy live workflows and credential storage without reading or republishing secrets; establish an explicit owner decision on disabling/retiring network automation; confirm which former GOAL-PVP-001 components have merged; label each capability `shipped`, `prototype`, `unverified`, `deferred` or `prohibited`.

**Accept when:** new advisor code has zero authenticated HTTP paths; all tests use mocked/synthetic data; the repository is scanned for sensitive material without outputting values; game rules have source/version metadata; no claim is based merely on a passing old CI badge.

**Dependencies:** source audit; incident owner retains separate responsibility for credential rotation and historical purge. Do not make game-account login a prerequisite.

### R1 — Ship Profile MVP (P0)

**Deliver:** versioned local schema for `ShipSnapshot`, `CrewSnapshot`, `RoomSnapshot`, `ResearchSnapshot`, and `DataProvenance`; local JSON/CSV/manual form import; validation with clear missing-field diagnostics; per-ship report.

**Must capture:** pseudonymous ship ID, hull/trophies (optional), game patch, source and timestamp, rooms/levels/placement/power, crew level/role/ability/equipment/training, research, available resources, native AI rules if supplied. Credentials and authentication artifacts are forbidden.

**MVP views:** current strengths/weaknesses, exposed critical rooms, power and repair coverage, crew readiness, missing evidence, five most promising manual improvements.

**Accept when:** fixtures for at least two materially different hull configurations produce stable, reproducible output; invalid/missing inputs do not silently become zero; two ships' records cannot mix; every recommendation links to relevant input fields.

### R2 — Progression Planner (P0)

**Deliver:** opportunity scoring, upgrade dependency graph, per-ship upgrade readiness and seven-/30-day investment plans.

**Prioritization dimensions:** expected PvP impact (hypothesis, not fact), minerals/gas/time required, crew/room dependencies, prerequisite research, irreversibility, risk and confidence. Unknown costs remain unknown rather than estimated from fabricated numbers.

**Decision support:** crew leveling/training/equipment recommendations; weapon, shield, engine, reactor, repair, security, hangar upgrades where relevant; research ordering; protected inventory/reserve awareness if an independently verified offline inventory module is available; hull readiness checklist.

**Accept when:** hard resource ceilings and protected-reserve constraints are respected; incompatible upgrades are never suggested simultaneously; priority changes are explainable; the same inputs and rules yield identical results; a hull-upgrade decision shows explicit unmet gates and requires human judgment.

### R3 — Native AI & Build Lab (P1)

**Deliver:** offline reference cards for native crew/room AI conditions and actions; role-to-room match suggestions; layout comparison and hypothetical loadout scenarios.

**Hypotheses to test:** coordinated target focus versus broader targeting, repair travel time, shield and reactor resilience, defensive crew allocation, boarding/anti-boarding readiness, missile/craft interactions at supported hulls. Availability and legal conditions must be checked against the versioned game data.

**Output:** human-readable in-client setup steps, before/after expected mechanism, assumptions, and manual rollback instructions—not executable instructions for direct server API mutation.

**Accept when:** invalid game-version combinations are rejected; suggestions are clearly labeled `candidate` until corroborated by battle evidence; the system cannot send or apply AI changes to the game.

### R4 — PvP Results & Experiments (P0)

**Deliver:** manual battle journal, optional sanitized replay metadata import (only when obtained lawfully), attack/defense scorecards, loss-cause taxonomy, and controlled before/after comparison.

**Data grain:** one row per human-played battle: profile, timestamp, game version, configuration version, mode, attack/defense, own/opponent hull if known, result, trophy change if known, duration if known, notable damaged/lost rooms and suspected cause. Unknown outcomes are not counted as defeats.

**Analytics:** win rate and sample size; confidence interval or simple uncertainty notice; net trophies; breakdown by opponent bracket and mode; repeated loss causes; performance before/after a single ship change. Do not pool mismatched account cohorts or treat correlational improvements as causal proof.

**Suggested initial targets (to validate after baseline):** 30 comparable attacks **and** 30 comparable defenses per tested configuration where practical; 7-day reporting freshness; improving comparable-opponent performance by 10 percentage points versus baseline is a hypothesis, not an SLA or guarantee.

**Accept when:** tests cover win/loss/tie/unknown, data leakage across profiles, uneven sample sizes, confounded comparisons, and reproducible reports. Recommendations must not claim measured improvement without adequately comparable observations.

### R5 — Portfolio Workbench (P1)

**Deliver:** offline account overview and separate account improvement queues; a budget-aware planning calendar; progress against last recommendation; model-change audit trail.

**Design:** independent ship profiles with no simultaneous logins or multi-account gameplay actions. Comparisons normalize for hull, patch, opponents, and missing data rather than presenting an unsupported global ranking.

**Accept when:** profile separation is enforced in import/storage/reporting; switching profiles cannot leak another ship's data; a combined dashboard is only an offline summary.

### R6 — Polished Reports & Release (P1)

**Deliver:** documented local-first CLI and/or static HTML report generator; screenshot/manual data workflow; accessible summaries; reproducible sample reports; migration documentation; user guide; source citations and changelog.

**Accept when:** a new user can install the tool, import synthetic example data, view a ship assessment, plan one change, record an outcome, and compare results without entering credentials, connecting to a game API, or making any gameplay action. The release contains no secrets, raw captures, or private profile data.

### Later options — Do not pre-commit

- Structured replay annotation and per-room survival visualization if non-sensitive, authorized sources are available.
- Scenario scoring across strategies and hull levels; distinguish heuristic from validated simulation.
- Community-contributed versioned, reviewed strategy packs with attribution and provenance.
- Optional local desktop app after CLI/report usage demonstrates demand.
- More sophisticated ranking/forecasting only after battle data is sufficient and well-calibrated.

## 6. Prioritized epic backlog

Each epic produces independently verifiable artifacts; IDs are stable planning identifiers, **not assertions that GitHub issues/Kanban cards already exist**.

| Epic | Priority | Owner lane | Depends on | First demonstrable acceptance |
| --- | --- | --- | --- | --- |
| **PV-001 Baseline & policy gate** | P0 | Safety + verifier | None | Non-mutation contract, repo audit and known/unknown status ledger |
| **PV-002 Snapshot schema/import** | P0 | Architect + analyst | PV-001 | Two isolated sanitized profiles parse and validate |
| **PV-003 Ship/crew readiness** | P0 | Analyst | PV-002 | Repeatable, explainable room/crew gap assessment |
| **PV-004 Upgrade dependency & budget planner** | P0 | Analyst + architect | PV-003 | 7/30-day plan respects dependencies and unknown costs |
| **PV-005 Patch-aware game data** | P0 | Researcher + verifier | PV-001 | Rule set names patch, source, effective/uncertain status |
| **PV-006 Battle journal & cohorts** | P0 | Analyst | PV-002 | Attack/defense and opponent bracket statistics correct |
| **PV-007 Native AI recommendations** | P1 | Researcher + architect | PV-003, PV-005 | Manual rule cards with conditions, rationale, rollback |
| **PV-008 Layout/loadout comparison** | P1 | Architect + analyst | PV-003, PV-005 | Candidate A/B scored without pretending to simulate battles |
| **PV-009 Experiment review** | P1 | Analyst + verifier | PV-006, PV-007 | Change-to-result comparison flags small/confounded samples |
| **PV-010 Portfolio/local reporting** | P1 | Architect | PV-004, PV-006 | Private per-account dashboards and reproducible exports |
| **PV-011 Distribution & product QA** | P1 | Verifier + maintainer | PV-010 | Install → analyze → plan → journal → compare offline demo |
| **PV-012 Live/legacy deprecation decision** | P0 risk | Maintainer + safety | PV-001 | Documented decision on obsolete authenticated workflows; no silent activation |

**Suggested first three vertical slices:** (1) inventory current repository / security and game-version assumptions, (2) import two synthetic ship profiles and run existing layout/crew evaluation, (3) generate the first human-readable readiness report. Avoid a large parallel swarm until a single slice works end to end.

## 7. Measurement framework

**North-star outcome:** Change in **human-played PvP win rate against comparable opponents**, measured separately for attack and defense at a comparable hull/game-version/strategy level. This is a long-run outcome, not an engineering gate for shipping an offline MVP.

| Measure | Definition | Use / guardrail |
| --- | --- | --- |
| Comparable attack win rate | Wins / known-result attacks in the chosen opponent bracket and version | Always show denominator and period |
| Comparable defense win rate | Wins / known-result defenses in the same bracket and version | Do not mix attack/defense |
| Net trophy change | Trophy gains minus losses over a stated window | Supporting signal; susceptible to matchmaking |
| Recommendation completion | Human-confirmed changes / planned changes | Leading indicator, not proof of improvement |
| Time-to-first-insight | Minutes from valid import to first explainable report | MVP usability |
| Snapshot freshness | Age of latest manually supplied data | Suppress high-confidence advice on stale inputs |
| Explanation coverage | Recommendations with provenance, reason, confidence and unknowns / total | Quality gate; target 100% |
| Safety regressions | New unauthorized network/gameplay mutation or secret leak paths | Release tolerance: zero |

No win-rate or trophy target should drive behavior that violates platform policy. Targets are revised after valid baseline data arrives.

## 8. Game-version and evidence policy

Maintain a versioned `GameRuleset` manifest with `version`, `release_status`, `source_url`, `observed_on`, `effective_from`, `supersedes`, and confidence per rule. Official patch notes outrank older community observations, but game-client behavior may require re-validation. Keep **announced** distinct from **released**.

Specific watchpoints:

- **Released in June 2026:** Hull 14, new hangars/rooms, new craft and tactical AI features, several power and balance changes ([V0.999.55](https://blog.pixelstarships.com/2026/06/17/hull-14-major-update-patch-notes-v0-999-55/)).
- **Released in August 2026:** V0.999.60 patch; review full notes before attributing current values ([official game blog](https://blog.pixelstarships.com/)).
- **Announced October 16, 2026, not independently confirmed shipped in this roadmap:** Version 1.0 changes to Legendary Equipment and Legendary Crew training points, plus UI/mining-drone behavior ([1.0 preview](https://blog.pixelstarships.com/2026/10/16/sneak-peek-pixel-starships-1-0/)). Do not overwrite published crew formulas with announced caps until release is confirmed and tested.

Every scored recommendation must expose its ruleset version. Unknown patch compatibility degrades confidence or blocks the specific recommendation rather than making up an answer.

## 9. Data and architecture contract

**Architecture direction:**

```text
manual entry / sanitized export / screenshot-derived observations
                    |
             schema validation
                    |
        local pseudonymous ship snapshots
                    |
       pure game-rule and planning functions
                    |
    readiness + budget + native-AI recommendation
                    |
         human-readable report / local CLI
                    |
       human plays using official PSS client
                    |
          manual battle-results journal
                    |
      comparison / evidence confidence / next plan
```

- Pure strategy/domain modules take typed, validated data and return deterministic results. **No import of authenticated game client transport.**
- Keep any old `sdk/client.py` functionality in the separately audited legacy boundary. Never transitively reach it from the offline advisor.
- Use local portable structured files with an explicit schema version; define privacy/retention/export/delete behavior before building a database or telemetry.
- Ingestion defaults to manual/sanitized import. Do not make reverse-engineered live API calls, credentials, game-client interception, or automated screen control a dependency.
- Keep a per-rule explanation and per-input provenance trace; do not silently normalize missing resource values to zero.
- Compatibility: Python 3.11 baseline according to `pyproject.toml`; respect repository test conventions and `AGENTS.md` restrictions. Use `README.template` as the maintained README source.
- No remote user telemetry, hosted account dashboard, or multi-device sync in initial scope.

## 10. Quality, security, and release gates

**Definition of ready:** explicit user problem; one independent vertical slice; known sources and patch version; synthetic examples; permitted paths; no live gameplay requirement; measurable acceptance; failure behavior specified.

**Definition of done:** deterministic tests and adversarial fixtures; full existing test suite and configured lint/type checks; `git diff --check`; reviewed docs; no new authenticated network path; no secrets in code/test/report data; provenance and limitations visible; code review or verifier gate pass; one realistic synthetic end-to-end walkthrough.

**Security incident track (parallel but not optional):** identify owner and independently verify the earlier credential-rotation, refresh-token invalidation, GitHub history cleanup, and scheduled workflow disablement decisions. Do not publish values or re-use leaked material. Do not equate history deletion with token invalidation. Block network-enabled changes until this incident and platform-policy review are resolved, while leaving offline development unblocked.

**Legacy risk control:** new advisor releases must not include an automatic PvP, marketplace, farming, or cross-account action even behind a flag. Audit whether the old scheduled workflows should be retired, quarantined, or moved to a separately approved historical branch. This roadmap itself does not disable, delete, or rewrite those workflows.

## 11. Key risks and explicit decisions

| Risk / decision | Impact | Mitigation / owner |
| --- | --- | --- |
| Public credentials/tokens reported exposed; current remediation status unknown | Account compromise and workflow misuse | **P0 security owner** validates revocation/rotation, public refs, and automation status separately |
| Existing repo advertises authenticated automated battles/collections | Account enforcement risk under official EULA | **Product owner** decides quarantine/deprecation; no expansion or automatic use |
| GOAL-PVP-001 local swarm not proven merged or verified | Duplicate implementation; phantom progress | **Maintainer** checks commit IDs, tests, verifier metadata, and branch merge state |
| 1.0 announced balance changes may invalidate crew scores | Misleading recommendations | **Researcher** uses announced/released version states; verifier validates rules |
| Sparse or biased PvP samples | Overfitted strategy recommendations | **Analyst** shows sample sizes, confidence and opponent brackets |
| Data entry cost | Low adoption | **Product** starts with minimal typed fields and gradual detail |
| Cross-account identity/mix-up | Incorrect or private recommendations | **Architect** enforces pseudonymous profile isolation |
| Market or item-transfer automation pressure | Fair-play and asset-loss risk | **Safety owner** keeps trading execution explicitly out of scope |

**Open product decisions:** (A) whether to deprecate legacy online `run.py` workflows immediately; (B) CLI-first versus generated static HTML as first user surface (recommend CLI + static report); (C) minimum fields users will actually enter manually; (D) whether a future community guide feature is useful after offline MVP adoption. None is a prerequisite to publishing this document.

## 12. Operating cadence and ownership

- **Weekly:** inspect one ship's latest data, choose its top three feasible manual actions, log completed actions and battle outcomes, and update risks and patch-watch notes.
- **Every two weeks:** ship at most one or two vertical slices with tests and concrete demo evidence; avoid unbounded multi-agent parallelism and retry loops.
- **Monthly:** compare normalized PvP results and adjust the plan; retain counter-evidence and assumptions rather than claiming victory from anecdotes.
- **When official patch notes release:** revalidate affected formulas, room compatibility, equipment and AI rules before applying them to recommendations.

Kanban roles: `tachikoma-researcher` owns game-rules sourcing; `tachikoma-analyst` owns evaluation and measurement; `tachikoma-architect` owns typed pure modules and report integration; `tachikoma-protocol` assists **offline data/schema interpretation only**; `tachikoma-safety` owns policy, privacy and mutation audit; `tachikoma-verifier` independently tests claims; `tachikoma` synthesizes accepted work. Workers do not declare their own verifier gates passed.

## 13. Immediate next action (first accepted milestone)

**PV-001/PV-002 thin vertical slice:** produce a complete offline recommendation for **one synthetic ship** and a contrasting second ship:

1. Audit the current branch and last GOAL-PVP-001 handoffs; record what truly exists.
2. Define a versioned, credential-free `ShipSnapshot` schema and two synthetic fixtures.
3. Feed both into existing pure ship-layout and crew-readiness analysis.
4. Produce a report naming the top three recommendations **with evidence, unknowns and explicit manual next actions**.
5. Verify no network traffic and no gameplay mutation can occur from the report path.
6. Independently test and present the output; only then begin resource budgeting and historical battle journals.

**Acceptance:** a new user can run the example completely offline, compare the two ships, understand the reasons and limitations, and see that no game state was changed.

## 14. Sources and maintenance

Repository evidence reviewed on 2026-11-03: [README.template](../README.template), [AGENTS.md](../AGENTS.md), [crew leveling](../sdk/crew_leveling.py), [ship layout](../sdk/ship_layout.py), [game formulas](../sdk/game_formulas.py), [PvP tests](../tests/test_pvp_battles.py), [automation harness](../automation/README.md), and [workflow directory](../.github/workflows/).

External sources:

1. [Pixel Starships EULA](https://www.pixelstarships.com/EULA).
2. [SavySoda: Fair Play and Automation (2022-09-12)](https://blog.pixelstarships.com/2022/09/12/fair-play-and-automation/).
3. [SavySoda: Hull 14 Major Update (2026-06-17)](https://blog.pixelstarships.com/2026/06/17/hull-14-major-update-patch-notes-v0-999-55/).
4. [SavySoda: Pixel Starships 1.0 Preview (2026-10-16)](https://blog.pixelstarships.com/2026/10/16/sneak-peek-pixel-starships-1-0/).
5. [SavySoda game updates index](https://blog.pixelstarships.com/).

**Maintenance rule:** update this roadmap when a release gate passes, the repo's verified implementation changes, or the game's official rules change. Separate delivered facts from proposals. Do not retroactively mark earlier Kanban goals done solely because their task titles appear in a planning document.
