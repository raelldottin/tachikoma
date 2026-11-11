# SPEC-nn: Title

**Status:** Draft | Approved | Verified offline | Done | Withdrawn
**Release:** R? (exit / partial)
**Owner gate:** G? (or —)
**Depends on:** SPEC-nn, ...
**Implements roadmap item:** quote the line from [PRODUCT_ROADMAP.md](../PRODUCT_ROADMAP.md) or [known-limitations.md](../baseline/known-limitations.md)

## Problem

What is missing or wrong today, with file references. Two to five sentences.

## Scope

- **In:** what this spec delivers.
- **Out:** what it deliberately leaves alone.

## Requirements

Each requirement is one testable statement. Levels: MUST (needed for `Done`), SHOULD (expected unless a reason is recorded), MAY (optional).

- **Snn-R1 (MUST)** ...
- **Snn-R2 (SHOULD)** ...

## Interfaces

Commands, flags, functions, file formats and data shapes this spec adds or changes, with their exact names. Say which existing modules are reused.

## Safety invariants

How this spec keeps each project invariant: API success is not verification; unobserved is `None`; accounts never mix; secrets never leak; no starbux spend in fleet runs; no retry after an uncertain outcome. Add any invariant specific to this spec.

## Acceptance criteria

Given / when / then. Each names the requirements it proves and whether it is offline (CI) or live (owner).

| Id | Proves | Kind | Criterion |
| --- | --- | --- | --- |
| Snn-AC1 | Snn-R1 | offline | Given ..., when ..., then ... |
| Snn-AC2 | Snn-R2 | live | ... |

## Test plan

The test modules to add or extend, the fixtures they need (synthetic only; captures must be sanitized), and which acceptance criteria each covers.

## Slices

| Slice id | Requirements | Files | Kind |
| --- | --- | --- | --- |
| `...` | Snn-R1..R3 | exact paths | agent / owner |

## Owner decisions

| Id | Question | Needed before | Safe default |
| --- | --- | --- | --- |
| Snn-Q1 | ... | slice or stage | ... |

## Evidence

Filled in when live acceptance criteria are run: date, account alias, game version, link to the record in `docs/experiments/`.

## Open questions and risks

Anything unverified, with how it will be resolved.
