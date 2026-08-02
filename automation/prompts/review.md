# Supervisor Stop Review

Use this prompt after the supervisor stops autonomous chaining.

Inspect these inputs together:

- `automation/queue/slices.json`
- the latest JSON artifact in `automation/handoffs/`
- the latest supervisor decision report
- `git status --short`
- `git diff --stat`

Review goals:

1. Explain why the supervisor stopped.
2. State what changed in the stopped run.
3. Identify any out-of-scope dirt, validation gaps, or cleanup required before another slice can start.
4. Call out any approval, queue edit, or reclassification needed before more autonomous work.
5. Check whether the handoff names a concrete `proof_level`, missing proof levels, contract status changes, residual risks, repo cleanliness, and git mirror status.
6. Decide whether the next slice should stay queued, be split, be reprioritized, or wait for cleanup.

Desired review output:

- `Stop reason`: the concrete stop trigger and whether it was expected
- `Changed scope`: the meaningful files or surfaces that changed
- `Cleanup required`: what must be cleaned, restored, or validated before continuing
- `Proof level`: the highest proof claimed, plus any missing proof that matters
- `Contract status`: status changes claimed by the handoff, if any
- `Residual risk`: risk left after the reported proof
- `Repo state`: repo clean status and git mirror status from the handoff
- `Approval required`: any product, design, or repo decision that needs a human
- `Queue recommendation`: what should happen to the current and next queued slices
- `Safe next action`: the smallest manual step that unblocks the harness

Do not auto-continue to another slice from inside this review run.
