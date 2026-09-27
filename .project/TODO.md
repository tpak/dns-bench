# TODO

## In Progress

- PR #1 `chore/python-3.13-floor`: 3.13 floor, BACKGROUND.md rename, CLAUDE.md, REMEDIATION_PLAN.md, `.project/` state files (awaiting review)
- PR #2 `chore/lint-format` (Phase 0), stacked on PR #1 (awaiting review; merge with a merge commit so `.git-blame-ignore-revs` stays valid)

## Up Next

- Phase 1 — CI: GitHub Actions for lint, test and release; fix the 2 tests that fail on Python 3.14 first
- Re-enable Biome's `noDescendingSpecificity` with a visual check of every view (Phase 7)
- Phases 2–8 — see REMEDIATION_PLAN.md

## Done This Week

- Phase 0 — lint & format: ruff + Biome via pre-commit; existing code reformatted and cleaned up (2026-09-27)
- Tagged and published v1.0.0 (2026-09-27)
- Added Python code-quality rules to CLAUDE.md and renumbered it
