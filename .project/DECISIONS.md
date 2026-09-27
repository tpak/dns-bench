# Decisions

Append-only. One line per decision: `YYYY-MM-DD: [Topic] [Decision] — [Rationale].` Never edit a past entry; to reverse one, append a new entry that references it.

2026-09-27: [Release] Tagged the one-shot generated version as v1.0.0 — a fixed baseline before the remediation work in REMEDIATION_PLAN.md starts changing things.
2026-09-27: [Python] Raised the minimum Python from 3.9 to 3.13 — uv can supply any interpreter, 3.9 is past end-of-life, and the code needs nothing older; 3.13 is the owner's default interpreter and gets security fixes until October 2029.
2026-09-27: [Tooling] uv manages Python interpreters, environments and tools; pyenv, pip and pipx are not used — one tool, the same one the owner uses on other machines.
2026-09-27: [Dependencies] The runtime stays standard-library-first, but third-party packages are allowed when justified — the README's "no pip install" promise is no longer a hard constraint.
2026-09-27: [Plan] Lint/format (Phase 0) and CI (Phase 1) come before the remediation phases — every later change is then linted locally and tested in CI from its first commit.
2026-09-27: [Lint] Ruff lints and formats Python; Biome lints and formats JS, CSS and JSON; pre-commit runs both on every commit — one fast tool per language, and no Node packages in the repo (pre-commit fetches Node for the Biome hook itself).
2026-09-27: [Lint] Ruff uses an explicit rule list (E, W, F, I, B, UP, SIM, A, RUF), not its defaults — a ruff upgrade can't silently change what is checked.
2026-09-27: [Lint] Tool versions are pinned in .pre-commit-config.yaml, not as uv dev dependencies — no pyproject.toml/uv.lock is needed before Phase 2 packaging, and CI can run the same pinned hooks.
2026-09-27: [Lint] Biome's noDescendingSpecificity is off — its fixes reorder CSS rules, which can change which rule wins; revisit with a visual check (REMEDIATION_PLAN.md Phase 7).
2026-09-27: [JSON] Untrusted JSON (the config file, API request bodies) goes through config.loads_json, which rejects nesting deeper than 32 levels — Python 3.14's parser accepts nesting that 3.13's rejects, so the limit lives in code; the two tests the plan expected to relax were right and stay unchanged.
2026-09-27: [Tests] The rate-limit tests check the sleeps the runner requests, not measured wake-up times; real-time bounds keep only 0.5 s of slack for stalls — CI's macOS runners wake threads 60–130 ms late, and the requested sleep is exact.
2026-09-27: [CI] A single gate job, `ci-passed`, is the check branch protection requires — its name doesn't change when the test matrix does.
2026-09-27: [CI] Actions are pinned to commit SHAs, and a pin is at least 7 days old: setup-uv v10.1.0, not the 6-day-old v10.2.0; Dependabot's monthly grouped updates use a 7-day cooldown — time for a compromised or broken release to be noticed.
2026-09-27: [Release] release.yml re-runs ci.yml through workflow_call, and run by hand it is a dry run that publishes nothing — one definition of the checks, and a way to preview the release notes before tagging.
2026-09-27: [Versioning] A higher minimum Python is a minor version bump — it changes what users must install, not their config or run files (README "Releasing").
2026-09-27: [Release] release.yml publishes only commits that are on main (GitHub compare API), checked on tag pushes and on dry runs of main — the tag-name check alone would have released a tag pushed from an unmerged branch.
2026-09-27: [Web UI] The web UI's minimum browsers are Chrome/Edge 93, Firefox 92 and Safari 15.4 (ES2022, via Phase 0's lint fixes), documented rather than reverted — a localhost UI opened in the user's own, current browser.
2026-09-27: [Versioning] Released 1.1.0 (minor: the minimum Python rose to 3.13).
2026-09-27: [Packaging] pyproject.toml builds with hatchling and reads the version from dnsbench.__version__ — uv's own backend (uv_build) rejects a dynamic version, and __version__ stays the one place a release bumps (release.yml checks it). hatchling is needed only to build; the runtime still has no dependencies.
2026-09-27: [Packaging] Only editable installs are supported (`uv tool install --editable .`) — config.json and runs/ live in the checkout, so the command must run the checkout's code; Phase 4 makes a non-editable install fail with a clear message.
2026-09-27: [Types] mypy runs as a pre-commit hook (mirrors-mypy v2.3.1, pinned like ruff and Biome, so CI runs it too), over all of dnsbench/ and tests/ rather than the staged files, with default settings — a change in one module can break another, and lenient settings let it land with behaviour-preserving fixes only; Phase 6 tightens it.
2026-09-27: [Tests] Two real run files from 1.0.0, with the hostname, ISP IPs and a personal domain replaced, are test fixtures (tests/fixtures/runs-v1), and Biome skips tests/fixtures — they pin the on-disk run format for Phase 6's migrate(), and a formatter would rewrite them.
