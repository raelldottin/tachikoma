# Tachikoma roadmap amendment: SavySoda-authorized live testing

Updated: November 3, 2026

The project owner reports that SavySoda has approved live testing for Tachikoma. This changes the product plan from offline-only analysis to **offline strategy plus controlled, authorized live validation**. The full scope of the approval has not yet been recorded or independently verified in this repository. Before enabling live operations, preserve the approval privately and document only non-sensitive scope metadata: permitted accounts, activities, dates, limits, and exclusions.

## Product direction

Keep deterministic ship and crew analysis, hull readiness, upgrade planning, native combat AI recommendations, and PvP analytics as the core product. Add a live-validation track to test recommendations against the real game under the exact provider authorization. Do not assume permission for any operation not explicitly included in that authorization.

## Updated release sequence

| Phase | Deliverable | Exit criterion |
| --- | --- | --- |
| R0 | Record approval scope and confirm secure test-account readiness | Provider terms documented privately and security gates passed |
| R1 | Ship profiles and optional authorized read-only account snapshots | Account identity and data provenance verified |
| R2 | Progression planner and controlled upgrade research | Costs and pre/post effects explainable; specific tests approved |
| R3 | Native AI and PvP experiment design | Controlled test plan with measurable hypothesis and test budget |
| R4 | Authorized live PvP validation | Comparable match results and reliable outcome evidence |
| R5 | Independent multi-ship progression reporting | Account records isolated; no unapproved orchestration |
| R6 | Hardened reports, audit trail and test operations | Reproducible release, privacy review and test-specific safety approval |

## Release gates

- Run normal CI against mocks and synthetic fixtures, not production gameplay.
- Permit live sessions only when their exact activity and account are within the recorded SavySoda approval, and after remediation of any previously exposed credentials.
- Require human oversight, defined limits, redacted logs, clear stop criteria and a verifiable before/after record.
- Treat read-only data collection, battles, progression changes, purchases, marketplace activity and multi-account automation as **different approval classes**. A general testing approval does not establish blanket permission for all classes.
- Default to dry-run when permission scope is missing, expired or ambiguous. Do not enable old live automation workflows merely by merging documentation.

## First milestone

Reconcile existing PvP work, produce two credential-free ship profiles and explainable progression plans, record the provider authorization privately, and design one specifically permitted read-only live pilot. After scope and credential review, conduct that pilot under the documented conditions. Plan PvP trials only when their operation class is in the approval.

This amendment supersedes the original roadmap's **offline-only limitation**, not its goals for evidence quality, security, account isolation or measurable PvP improvement. The complete roadmap remains in [PRODUCT_ROADMAP.md](PRODUCT_ROADMAP.md) and should be consolidated with this amendment during the next documentation revision.
