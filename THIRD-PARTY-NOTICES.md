# Third-Party Notices

This repository contains original work, but some of its prompt fragments **adapt
concepts** from third-party material. No third-party files are vendored verbatim; the
ideas below were re-expressed in this harness's own vocabulary (slices, allowed paths,
required validations, proof ladder, handoff, clean stop).

## mattpocock/skills

- Source: <https://github.com/mattpocock/skills>
- License: MIT
- Copyright (c) 2026 Matt Pocock

Scope: the project's **engineering** and **productivity** skill sets. The
following `automation/prompts/` fragments adapt concepts from those skills:

| Fragment | Adapted from |
|---|---|
| `automation/prompts/intake.md` | `productivity/grilling`, `productivity/grill-me`, `engineering/grill-with-docs` |
| `automation/prompts/triage.md` | `engineering/triage`, `engineering/to-issues`, `engineering/to-prd` |
| `automation/prompts/design.md` | `engineering/codebase-design`, `engineering/domain-modeling`, `engineering/improve-codebase-architecture`, `engineering/implement` |
| `automation/prompts/tdd.md` | `engineering/tdd` |
| `automation/prompts/diagnose.md` | `engineering/diagnosing-bugs` |
| `automation/prompts/resolve-conflicts.md` | `engineering/resolving-merge-conflicts` |

### Skills intentionally not adapted

For completeness, the remaining engineering/productivity skills were considered
and left out, because they do not map onto a bounded slice-supervisor harness:

- `productivity/handoff` — the harness already has a native handoff model and
  schema (`automation/schemas/handoff.schema.json`), which this concept would
  duplicate.
- `engineering/prototype` — throwaway prototyping sits outside the harness's
  bounded, production-slice discipline.
- `engineering/ask-matt`, `engineering/setup-matt-pocock-skills` — author-specific
  Q&A and a skills installer, with no harness analogue.
- `productivity/teach`, `productivity/writing-great-skills` — teaching and
  skill-authoring guidance, unrelated to running supervised slices.

### MIT License (mattpocock/skills)

```
MIT License

Copyright (c) 2026 Matt Pocock

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
