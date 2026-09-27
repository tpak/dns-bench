# TODO

## In Progress

- PR #3 `chore/lint-format` → `main`: lands Phase 0 in `main` (PR #2 merged into its stale parent branch). Merge with a merge commit so `.git-blame-ignore-revs` stays valid.
- PR #4 `docs/git-branch-hygiene`: CLAUDE.md §6 rules for branch cleanup and stacked PRs. Merge after #3; its diff then shows only the rule.
- After #3 and #4 merge: delete both branches locally and on GitHub, fast-forward `main` (CLAUDE.md §6).

## Up Next

- Phase 1 — CI: GitHub Actions for lint, test and release; fix the 2 tests that fail on Python 3.14 first
- Re-enable Biome's `noDescendingSpecificity` with a visual check of every view (Phase 7)
- Phases 2–8 — see REMEDIATION_PLAN.md

## Done This Week

- PR #1 merged: 3.13 floor, BACKGROUND.md rename, CLAUDE.md, REMEDIATION_PLAN.md, `.project/` (2026-09-27)
- Phase 0 — lint & format: ruff + Biome via pre-commit; existing code reformatted and cleaned up (2026-09-27)
- Tagged and published v1.0.0 (2026-09-27)
- Added Python code-quality rules to CLAUDE.md and renumbered it
