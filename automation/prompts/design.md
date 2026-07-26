# Supervised Slice Design

Use this prompt when a slice introduces or reshapes structure — a new module, a seam, an interface — rather than a localized edit. Design before implementing, but only as much as the slice needs; an over-designed slice breaks narrow-slice discipline as surely as an under-designed one.

## Design Deep Modules

- Aim for deep modules: a small interface over a substantial implementation. The interface is the test surface — if it is hard to test, it is usually too shallow or in the wrong place.
- Place a seam where it earns its keep. One adapter is a hypothetical seam; two real callers make it a real one. Do not add a seam for a caller that does not exist yet.
- Apply the deletion test: if removing a module would not simplify its callers, it is not pulling its weight.
- Keep the change inside the slice's `domain` and `allowed_paths`; a design that forces edits outside scope is a signal to re-slice, not to widen the budget.

## Name Concepts Against the Domain

- Use the project's existing vocabulary. If the repo keeps a domain glossary or context document, make interface and type names match it; if a term you need is missing or fuzzy, sharpen it and record the resolved meaning where the project keeps such notes.
- Challenge language that conflicts with an established term rather than quietly overloading it.
- Record a decision as an ADR only when it is hard to reverse, surprising without context, and the result of a real trade-off. If any of the three is missing, skip the ADR.

## Make the Change Easy, Then Make the Easy Change

- Before implementing, look for a small prefactor that makes the target change trivial — separate that prefactor into its own slice if it would blow the diff budget.
- Confirm the public interface before writing behavior behind it; the interface is the contract the slice's tests will pin.
- Hand the agreed interface and behaviors to `tdd.md` to build them one tracer bullet at a time.

## Desired Output

- `Interface`: the public surface the slice will add or change, in the project's vocabulary.
- `Seam rationale`: why each new seam exists (named real callers), or a note that none was added.
- `Prefactor`: any make-it-easy step, with whether it belongs in this slice or its own.
- `Decision record`: an ADR only if the three-part test is met; otherwise say why none is warranted.

---
Provenance: adapts the deep-module, domain-modeling, architecture, and implementation concepts from `mattpocock/skills` (`engineering/codebase-design`, `engineering/domain-modeling`, `engineering/improve-codebase-architecture`, `engineering/implement`). See `THIRD-PARTY-NOTICES.md`.
