# Supervised Bug Diagnosis

Use this prompt when a slice's work is fixing a bug. The discipline is simple and strict: build a signal that can catch the bug before you theorize about its cause.

## Build a Red-Capable Loop First

- Before forming any hypothesis, produce one command — a test, a script, a request — that drives the actual bug path and asserts the user's exact symptom, and run it at least once so you have seen it go red.
- The loop must be red-capable (fails on this specific bug, passes once fixed), deterministic (same verdict each run), and fast (seconds, not minutes).
- If you catch yourself reading code to build a theory before this command exists, stop — jumping to a hypothesis with no failing signal is the failure this fragment prevents.
- If the bug is non-deterministic, pin it to a high, repeatable reproduction rate before continuing.

## Reproduce, Then Minimise

- Once it is red, shrink the repro to the smallest scenario that still goes red: cut inputs, callers, config, and steps one at a time, re-running the loop after each cut.
- Done when every remaining element is load-bearing — removing any one of them makes the loop go green. The minimal repro becomes the regression test.

## Hypothesise, Instrument, Fix

- Form one hypothesis at a time and let the loop confirm or kill it; bisection and instrumentation only consume that signal.
- Prefix any temporary instrumentation with `[DEBUG-...]` so it stays greppable for removal.
- Land the fix, then turn the minimal repro into a regression test recorded in the slice's `required_validations`.

## Clean Stop Before Handoff

- Re-run the original loop and confirm it now passes.
- Remove every `[DEBUG-...]` line (grep the prefix) and delete throwaway prototypes.
- State the hypothesis that proved correct in the commit message so the next debugger learns.
- Report the highest honest `proof_level` (a passing regression test is at least `domain-tested`) and keep `residual_risks` truthful.

---
Provenance: adapts the bug-diagnosis loop from `mattpocock/skills` (`engineering/diagnosing-bugs`). See `THIRD-PARTY-NOTICES.md`.
