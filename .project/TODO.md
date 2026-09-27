# TODO

## In Progress

## Up Next

- Phase 2 — Safety net: `pyproject.toml`, mypy, `__main__`, drop the `sys.path` hacks, fix the silently skipped test, real-UI smoke tests, sanitized run-file fixtures, `.venv/` in `.gitignore` (REMEDIATION_PLAN.md)
- First release through `release.yml`: CHANGELOG's Unreleased section has the 3.13 floor and the deep-JSON fix, so the next version would be v1.1.0 (minor, for the higher minimum Python). Chris decides when; tagging publishes.
- Consider Dependabot's `pre-commit` ecosystem for the hook revs (would replace manual `pre-commit autoupdate`; CLAUDE.md §7.2 would change) — `.project/research/2026-09-27-ci-actions.md`
- Re-enable Biome's `noDescendingSpecificity` with a visual check of every view (Phase 7)
- Phases 3–8 — see REMEDIATION_PLAN.md

## Done This Week

- Phase 1 — CI: `ci.yml` (lint; tests on Linux and macOS × 3.13 and 3.14; launcher checks; `ci-passed` gate), `release.yml` (tag → checks → GitHub release; manual dry run), Dependabot for actions, CHANGELOG.md, README CI and Releasing sections. Fixed a Python 3.14 bug (deeply nested JSON caused a 500) and made the rate-limit tests robust on slow runners. `main` protected by a ruleset requiring `ci-passed`. PR #5 (2026-09-27)
- Release dry run fixed and verified on GitHub (a concurrency-group deadlock between release.yml and the ci.yml it calls). PR #6 (2026-09-27)
- PR #4 merged: CLAUDE.md §6 rules for branch cleanup and stacked PRs; branch cleaned up (2026-09-27)
- PR #1 merged: 3.13 floor, BACKGROUND.md rename, CLAUDE.md, REMEDIATION_PLAN.md, `.project/` (2026-09-27)
- Phase 0 — lint & format: ruff + Biome via pre-commit; existing code reformatted and cleaned up. In `main` via PR #3 (PR #2 had merged into its stale parent branch); branches cleaned up (2026-09-27)
- Tagged and published v1.0.0 (2026-09-27)
- Added Python code-quality rules to CLAUDE.md and renumbered it
