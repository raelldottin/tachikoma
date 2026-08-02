# Supervised Slice Intake

Use this prompt to turn an underspecified request into a single queue-ready slice before any autonomous run. The goal is shared understanding: an intake that ends with an ambiguous brief produces a run that builds the wrong thing.

## Grill One Decision at a Time

- Interview the requester until the brief is unambiguous. Ask exactly one question at a time and wait for the answer before the next; batching questions is bewildering and loses decisions.
- For every question, propose your recommended answer so the requester can confirm or correct rather than start from blank.
- If a question can be answered by reading the repository, read the repository instead of asking. Spend questions only on intent and trade-offs the code cannot reveal.
- Walk the decision tree branch by branch, resolving dependencies between decisions before the decisions they gate.

## Resolve the Brief Into Slice Fields

Drive the conversation until each queued-slice field is concrete:

- `title` - one specific outcome, not a theme.
- `domain` - the owning product or runtime area used for context selection.
- `allowed_paths` - prefix-form paths the slice may leave dirty; the narrowest set that still allows the change.
- `required_validations` - exact command strings that must pass, copied verbatim.
- `max_files_changed` - an honest diff budget for the outcome.
- `depends_on` - slice IDs that must already be `done`.
- acceptance checks - the checkable conditions that mean the slice is finished.

## Park What You Cannot Resolve

- If the work waits on an external fact or an explicit human decision, do not force it `queued`. Mark it `blocked` or `deferred` and name the `entry_condition` that must become true first.
- If a smaller slice would satisfy that entry condition, name it in `recommended_unblocker` rather than making the blocked slice executable.
- Carry any question you could not resolve into the run as `open_questions` on the handoff, so the supervisor sees what intake left open.

## Desired Output

- `Slice record`: the queue-ready slice with every field above filled.
- `Open questions`: decisions intake could not close, if any.
- `Parked dependencies`: blocked or deferred follow-ups with their entry conditions.

Do not edit `automation/queue/slices.json` from inside an autonomous run; intake produces the record for the queue owner to add.

---
Provenance: adapts the grilling concept from `mattpocock/skills` (`productivity/grilling`, `productivity/grill-me`). See `THIRD-PARTY-NOTICES.md`.
