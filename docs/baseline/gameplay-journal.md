# Gameplay event journal (R1 evidence layer)

`sdk/journal.py` keeps an append-only JSONL record of gameplay actions. Each line describes one action for one account: the account alias, action id, attempt id, start and finish times, expected effect, reconciliation outcome and reason, whether a mutating call was made, the response flag, any error class, a dry-run flag, parameters and cost.

## Rules

- **Append-only.** Events are appended and fsynced. Existing lines are never rewritten.
- **Outcomes** are the [action catalog](action-catalog.md) statuses (`verified`, `not-observed`, `contradicted`, `unknown`) plus `skipped` for an action blocked before any call. A skipped or dry-run event cannot claim a mutating call was attempted.
- **Secrets.** Parameters pass through `sdk.redaction.redact_dict`, so sensitive keys and secret-shaped string values are redacted. If the serialised event still contains a secret-shaped value after that, for example an e-mail address in `reason`, the event is refused and nothing is written.
- **Identity.** `account_alias` follows the [snapshot](account-snapshot.md) alias rule. E-mail addresses and display names are refused.
- **Crash tolerance.** A final line without a newline, left by an interrupted write, is ignored on read. Any other malformed line raises `JournalError` with its line number.

## Summary

`summarize(events)` returns per-account counts of `attempted`, `verified`, `not_observed`, `contradicted`, `uncertain` (an attempted action with an `unknown` outcome), `failed` (an error class was recorded) and `skipped`. These counts are the inputs to the roadmap's *verified action success* and *incident/failure frequency* metrics.
