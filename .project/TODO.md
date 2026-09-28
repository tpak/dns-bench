# TODO

## In Progress

- v1.2.0 release: notes rewritten after a pre-release review (39 findings; `.project/research/2026-09-28-release-1.2.0-review.md`); `serve --allow-remote` on 0.0.0.0 fixed; `python -m dnsbench` version check

## Up Next

- Consider Dependabot's `pre-commit` ecosystem for the hook revs (would replace manual `pre-commit autoupdate`; CLAUDE.md §7.2 would change) — `.project/research/2026-09-27-ci-actions.md`
- Phase 8 — measurement validity; changes results, ships separately (not in the current push)

## Done This Week

- Tag ruleset "release tags" for `v*`: updates and deletions restricted, admin bypass (2026-09-28)
- Chris clicked through the web UI in his browser after Phases 3–7: fine (2026-09-28)
- Phase 7 — Frontend (single file): one rAF-batched scheduleRender and read-only view builders; keyboard focus kept after sort, chips, Show all and closing details, and moved to the view on tab changes; charts one tab stop each (roving tabindex); <main> landmark restored; Settings errors linked to fields with a focused summary; Failed queries capped at 200; 15 s fetch timeouts and polling that recovers; light-theme contrast (WCAG AA) and reduced-motion fixes; Biome's noDescendingSpecificity back on (screenshots byte-identical). Review found four small UI issues — fixed. PR #13 (2026-09-28)
- Phase 6 — Backend layering & typed model: models.py (TypedDicts), RunRepository with migrate() and typed errors, analysis recomputed on load (cached per file, per object), BenchmarkService + JobManager as the one run path for CLI and UI, recommend split into rank/choose/explain with coded notes, strict mypy on ten core modules. All 9 real runs recompute to their stored recommendations. Review found an unhandled error in explicit aggregates and a shutdown race with a starting job — both fixed. PR #12 (2026-09-28)
- Phase 5 — One source of truth: structured validation errors (path, code, message), GET /api/schema, POST /api/config/validate and /api/estimate; app.js keeps no copy of any rule; ~230 lines of unreachable compat code removed. Review found an overflow 500 on absurd numbers — fixed. PR #11 (2026-09-28)
- Phase 4 — Data hygiene & defaults: config.json and runs/ untracked and created on first use (loading never writes); the hard-coded ISP resolver replaced by a detected "System" entry (scutil / resolv.conf / systemd-resolved; `config --detect`, Settings > Add system resolvers); `dnsbench/paths.py` with DNSBENCH_HOME; README History section, data location, API table checked by a test. Review found System could mix two networks (VPN) — fixed. PR #10 (2026-09-28)
- Phase 3 — Security hardening + correctness: Origin check, CORP/COOP, CSP without inline styles plus Trusted Types, JSON errors for malformed requests, 15 s request timeout, strict Host port, `serve --allow-remote`; the UI never loses a finished run (runs-dir check + rescue, shared with the CLI); at most 20 resolvers and 50,000 queries per run; every server measured at once (`max_parallel_servers` removed — changes results with >8 servers); crashed runs saved as `partial`; SIGTERM and a second Ctrl-C save promptly. An independent review found the process still waiting for stuck queries at exit (fixed with daemon workers); CI found a signal race (fixed with a stop_waiting event). PR #9 (2026-09-27)
- Phase 2 — Safety net: `pyproject.toml` (hatchling, dynamic version, `dns-bench` entry point, editable installs only), `python -m dnsbench`, mypy in pre-commit (13 behaviour-preserving fixes), no more `sys.path` hacks, the skipped `dns-test.sh` test now runs, real-UI smoke tests (`tests/test_web.py`), sanitized v1 run fixtures, `.venv/` ignored. CI's 3.12 launcher check fixed for the new `requires-python`. PR #8 (2026-09-27)
- v1.1.0: version bump and corrected release notes after an independent pre-release review; release.yml now publishes only commits on `main`; README states the browser minimum and release recovery steps. Tagged after the release PR merged (2026-09-27)
- Phase 1 — CI: `ci.yml` (lint; tests on Linux and macOS × 3.13 and 3.14; launcher checks; `ci-passed` gate), `release.yml` (tag → checks → GitHub release; manual dry run), Dependabot for actions, CHANGELOG.md, README CI and Releasing sections. Fixed a Python 3.14 bug (deeply nested JSON caused a 500) and made the rate-limit tests robust on slow runners. `main` protected by a ruleset requiring `ci-passed`. PR #5 (2026-09-27)
- Release dry run fixed and verified on GitHub (a concurrency-group deadlock between release.yml and the ci.yml it calls). PR #6 (2026-09-27)
- PR #4 merged: CLAUDE.md §6 rules for branch cleanup and stacked PRs; branch cleaned up (2026-09-27)
- PR #1 merged: 3.13 floor, BACKGROUND.md rename, CLAUDE.md, REMEDIATION_PLAN.md, `.project/` (2026-09-27)
- Phase 0 — lint & format: ruff + Biome via pre-commit; existing code reformatted and cleaned up. In `main` via PR #3 (PR #2 had merged into its stale parent branch); branches cleaned up (2026-09-27)
- Tagged and published v1.0.0 (2026-09-27)
- Added Python code-quality rules to CLAUDE.md and renumbered it
