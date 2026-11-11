# Alert rules (AUTO-012)

`sdk/reports/alerts.py` decides when a fleet run needs an operator's attention. `evaluate_alerts(journals, now=..., audit=None, rules=DEFAULT_RULES)` takes a mapping of journal owner alias → that account's [journal](gameplay-journal.md) events, an optional [isolation audit](fleet-audit.md) `AuditReport`, a timezone-aware evaluation time `now`, and a set of declarative `AlertRule`s. It returns an `AlertReport`.

This is a library only. Sending alerts (e-mail, chat, pager, webhooks) is out of scope: render the report with `to_json(report)` or `to_markdown(report)` and deliver it however you choose.

## Default rules

| Rule id | Kind | Severity | Fires when |
| --- | --- | --- | --- |
| `failure-rate-high` | `failure-rate` | warning | More than 20% of the account's attempts in the last 24h recorded an error class, with at least 5 attempts in the window |
| `uncertain-rate-high` | `uncertain-rate` | warning | More than 20% of the account's attempts in the last 24h have an `unknown` outcome, with at least 5 attempts in the window |
| `contradicted-action` | `contradicted` | warning | Any attempt in the last 24h was contradicted by observation |
| `no-verified-action` | `no-verified` | warning | The account attempted at least one action in the last 24h and none of them was verified |
| `isolation-incident` | `audit-incident` | critical | The audit has any incident for the account under a rule other than `budget-exceeded` (`foreign-event`, `unregistered-account`, `attempt-id-reused`, `overlapping-attempts`, and any rule the audit adds later). One alert per account and audit rule. |
| `budget-exceeded` | `audit-incident` | critical | The audit has any `budget-exceeded` incident for the account |

## Rule definitions

An `AlertRule` has `rule_id`, `kind`, `severity` (`critical`, `warning` or `info`), `threshold`, `min_attempts`, `window_hours`, `audit_rules`, `exclude_audit_rules` and a free-text `description`. A rule fires when the observed value is strictly above `threshold`: a fraction in `[0, 1)` for rate kinds, a count for `contradicted` and `audit-incident` (0 means "any"). To alert at more than one level, define more rules, for example a critical `failure-rate` rule at 50% beside the default warning. Rule ids must be unique within a set; an invalid rule raises `AlertRuleError`.

- **Window.** Journal rules look at events whose `finished_at` is in `(now - window_hours, now]` (24h by default). Events that finished after `now` are ignored, so a report can be re-evaluated at a past moment.
- **Sample-size guard.** Rate rules never fire with fewer than `min_attempts` attempts in the window (default 5). It cannot be turned off: a rate rule with `min_attempts` below 1 is rejected. Skipped, dry-run and out-of-window events do not count towards the minimum.
- **Denominators.** Rates are divided by attempted events, as in the [fleet report](fleet-report.md). A failed attempt is one with an `error_class`; an uncertain attempt is one with an `unknown` outcome, so one attempt can count as both.
- **Ownership.** Only the account's own events count. An event whose alias is not the journal owner is the audit's `foreign-event`, not the owner's work.
- **No verified action.** `no-verified` needs attempts inside the window, so an idle or disabled account does not alert forever. The evidence names the last verified attempt before the window, or `None` when there is none.
- **Audit rules.** `audit_rules` limits an `audit-incident` rule to those audit rules (empty means all), minus `exclude_audit_rules`. Audit incidents carry no timestamps, so these rules have no window: the audit's own scope applies. Without an audit, audit rules are listed in `rules_not_evaluated` and never reported as clean.

## Alerts and the report

Each `Alert` names `rule_id`, `severity`, `account_alias`, a one-line `message` and `evidence`: `counts`, `attempt_ids`, `window_start`/`window_end` (journal rules), `rate` and `threshold`, `audit_rule` and the audit incident `details` (audit rules), and `last_verified_at` (`no-verified`). Attempt ids are ordered by finish time; audit attempt ids are sorted.

`AlertReport` states `evaluated_at`, `accounts_checked`, `events_checked`, `rules_evaluated` and `rules_not_evaluated` even when no alert fires, and offers `by_severity()`. Alerts are ordered by severity (critical first), then account alias, then rule id, then audit rule, so the output does not depend on journal, event or rule order. `to_json` writes every field with sorted keys. `to_markdown` writes a severity summary and one table row per alert, listing at most five attempt ids per alert followed by `(+N more)`.
