# Supervised Test-Driven Slice

Use this prompt when a slice builds behavior test-first. The discipline turns the slice's acceptance checks into a tight red-green loop and produces tests that survive refactors because they pin behavior, not structure.

## Test Behavior, Not Implementation

- Write tests against the public interface, describing what the system does, not how. A good test reads like a specification and keeps passing across an internal rewrite.
- Treat a test that breaks on a rename with no behavior change as a defect in the test: it was coupled to implementation. Prefer integration-style tests over mocking internal collaborators.
- Pull test and interface names from the project's domain vocabulary so the suite reads in the project's own language.

## One Tracer Bullet at a Time

- Work in vertical slices: one test to red, the minimal code to green, then the next test informed by what you just learned. Do not write all the tests first and then all the code — bulk tests verify imagined behavior and go insensitive to real change.
- Per cycle: write one failing test (red), write only enough code to pass it (green), do not anticipate later tests.
- Never refactor while red. Reach green first, then improve.

## Refactor Once Green

- With the loop green, extract duplication, deepen modules behind simpler interfaces, and let the new code reveal what the existing code should become. Re-run the loop after each step.
- Keep every refactor inside the slice's `allowed_paths` and diff budget; structural work that reaches further is its own slice (see `design.md`).

## Clean Stop Before Handoff

- The behavior tests land in the slice's `required_validations` and must report as passed, verbatim, in the handoff.
- Report the highest honest `proof_level` the suite reached; passing behavior tests are at least `domain-tested`. Keep `residual_risks` truthful about untested paths.

---
Provenance: adapts the test-driven-development concept from `mattpocock/skills` (`engineering/tdd`). See `THIRD-PARTY-NOTICES.md`.
