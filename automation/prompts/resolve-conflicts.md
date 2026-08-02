# Supervised Conflict Resolution

Use this prompt when a fetch-then-rebase before the clean GitHub stop leaves the slice in an in-progress merge or rebase with conflicts. The goal is to preserve both sides' intent and finish, never to abandon the integration.

## Understand Before Resolving

- See the current state of the merge or rebase: the git history of both sides and every conflicting file.
- Find the primary source of each conflicting change. Understand why each side made it and what it intended — read the commit messages, the PRs, and the original issues. In a multi-agent repo a conflict is usually another agent's real work, not noise.

## Resolve Each Hunk on Intent

- Preserve both intents where they can coexist. Where they genuinely cannot, keep the one matching the merge's stated goal and note the trade-off in the handoff `residual_risks`.
- Do not invent new behavior while resolving, and do not silently drop the other side's change. Always resolve; never `--abort` away another agent's commits.
- Keep the resolution within the slice's scope — a conflict that forces edits outside `allowed_paths` is a stop-for-review signal, not a license to widen the diff.

## Verify, Then Finish

- Discover and run the project's automated checks in the usual order — type check, then tests, then format — and fix anything the integration broke. Re-run the slice's `required_validations`.
- Stage everything and complete the merge or rebase; if rebasing, continue until every commit is replayed.
- Confirm the clean GitHub stop: working tree clean and the branch even with its upstream after pushing, so `repo_clean_status` and `git_mirror_status` are honest in the handoff.

---
Provenance: adapts the merge-conflict-resolution concept from `mattpocock/skills` (`engineering/resolving-merge-conflicts`). See `THIRD-PARTY-NOTICES.md`.
