# Supervised Queue Triage

Use this prompt to move raw incoming work — a request, a bug report, an external diff — into well-formed queue slices. Triage classifies and decomposes; it does not implement.

## Classify

- Give the item exactly one category: `bug` (something shipped is broken) or `enhancement` (new or improved behavior).
- Give it exactly one queue state: `queued` (ready to run), `blocked` or `deferred` (parked behind an `entry_condition`), or drop it (already implemented, or out of scope).
- If the category or state is ambiguous, say so and ask before proceeding rather than guessing.

## Verify the Claim Before Trusting It

- For a bug, reproduce it from the reporter's steps and name the code path; an unreproducible bug is a needs-more-information signal, not a `queued` slice.
- For a request, search the codebase by domain concept (not just the request's wording) for an existing implementation; if it already exists, drop it and point to where it lives.
- For an external diff, check it out and run the relevant validations before classifying what remains.
- Report where you looked. A verified claim makes a far stronger slice brief than a forwarded assertion.

## Decompose Into Vertical Slices

- Break the work into independently runnable slices, each a thin vertical tracer bullet that delivers one checkable outcome end to end — not a horizontal layer that cannot be validated alone.
- Order slices so every `depends_on` points only at slices already `done` or earlier in the queue; publish blockers first so dependents can name real slice IDs.
- Keep each slice inside one `domain`, with the narrowest `allowed_paths` and an honest `max_files_changed`.
- For anything still underspecified, route it through `intake.md` to grill it into shape before it becomes `queued`.

## Desired Output

- `Classification`: category and state, with the reasoning.
- `Verification`: reproduced, confirmed, or insufficient, with the code path or commands used.
- `Slice set`: the ordered, dependency-correct slices ready for the queue owner.
- `Dropped`: anything already-implemented or out-of-scope, with where it lives or why.

Do not implement during triage, and do not edit `automation/queue/slices.json` from inside an autonomous run.

---
Provenance: adapts the triage and issue-decomposition concepts from `mattpocock/skills` (`engineering/triage`, `engineering/to-issues`). See `THIRD-PARTY-NOTICES.md`.
