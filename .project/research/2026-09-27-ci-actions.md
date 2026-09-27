# CI and release workflow: action versions, uv on runners, Dependabot, and deep JSON on 3.14

- **Date:** 2026-09-27
- **Question:** For Phase 1 (CI, release, Dependabot): which action versions to pin, how uv and
  pre-commit behave on GitHub runners with no `pyproject.toml`, what Dependabot supports for SHA-pinned
  actions, and why two tests fail on Python 3.14.

## Findings

### Action versions (latest releases on 2026-09-27)

| Action | Latest | Published | Pinned | SHA |
|---|---|---|---|---|
| actions/checkout | v7.0.1 | 2026-07-20 | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| astral-sh/setup-uv | v10.2.0 | 2026-09-21 | **v10.1.0** (2026-09-10) | `bec219d24cd3e171d82865faccec33120bb574f4` |
| actions/cache | v6.1.0 | 2026-06-26 | v6.1.0 | `55cc8345863c7cc4c66a329aec7e433d2d1c52a9` |

setup-uv v10.2.0 was six days old, so v10.1.0 is pinned. That matches the 7-day Dependabot cooldown.
Looked up with `gh api repos/<r>/releases/latest` and `gh api repos/<r>/commits/<tag> --jq .sha`.

### setup-uv v10 inputs

- `python-version` sets `UV_PYTHON` for later steps, so `uv run python ...` uses the matrix Python
  (uv downloads it if the runner lacks it).
- `enable-cache` defaults to `auto` (on for GitHub-hosted runners). dns-bench has no dependencies
  and no lock file, so there is nothing to cache. It is set to `false`, which also avoids
  warnings about the cache glob matching no files.

### uv run and the launcher's shebang

`uv run --python 3.13 sh -c 'command -v python3'` shows the interpreter's bin directory first on
PATH. So `uv run ./dns-bench --version` runs the launcher through its real
`#!/usr/bin/env python3` shebang with the chosen Python. `uv run --python 3.12 python ./dns-bench`
exits 1 with "dns-bench needs Python 3.13 or newer (found 3.12)".

### pre-commit in CI

- Installed with `uvx pre-commit@4.6.2`, the version installed locally.
- The biomejs/pre-commit `biome-check` hook is `language: node`, and ubuntu runners ship Node.
- Hook environments are cached at `~/.cache/pre-commit`, keyed on the hash of
  `.pre-commit-config.yaml`.

### Dependabot (docs.github.com, dependabot-options-reference)

- `cooldown.default-days` is supported for GitHub Actions. The semver-bump-days variants are not.
- `pre-commit` is a supported ecosystem, also with `default-days` cooldown. It could replace the
  manual `pre-commit autoupdate` (follow-up, not done).
- `groups.<name>.patterns` and `commit-message.prefix` work as expected.

### Linting the workflows

- actionlint (`uvx --from actionlint-py actionlint`) is clean.
- zizmor 1.30.1 (`uvx zizmor --offline`) reported no warnings. Its pedantic persona reports
  unnamed jobs (ignored) and a missing release concurrency group (added). It also suggests a
  newer `$/...` "self-repository" syntax for `uses: ./.github/workflows/ci.yml`. That form is not
  verified here, so the documented `./` form stays.

### Deep JSON on Python 3.14

- Python 3.14's `json` parses 100,000-deep `[[[...]]]`. On 3.13 the same input raises
  `RecursionError`, which the code caught as "not valid JSON" / "Malformed JSON".
- Two tests pinned that behaviour and failed on 3.14. The plan called for relaxing them, but
  probing on 3.14 showed a real bug. A deep value inside an object (e.g.
  `{"settings": {"rounds": [[[...]]]}}`) got past parsing, and validation's `repr()` then overflowed
  the stack:
  - `PUT /api/config` returned 500;
  - `load_config` crashed;
  - `POST /api/run` with `{"settings": {...deep...}}` started a job.
- Fix: `config.loads_json()` enforces `MAX_JSON_DEPTH = 32` on every version. The tests are
  unchanged.
- Still open: `storage.py` parses run files with plain `json.loads`. That is a local, self-written
  file, so it is left for Phase 6 (`RunRepository` / `CorruptRun`).

### Timing tests on GitHub's macOS runners

- The first CI run passed lint and both Ubuntu legs. Both macOS legs failed four `RateLimitTest`
  tests, and the suite took 49–53 s there against about 10 s on a laptop.
- `check_spacing` bounded real-time start-to-start gaps at `max(interval × 1.1, previous query's
  duration) + 30 ms`. The runners measured gaps of 112–186 ms at a 50 ms interval: sleeping
  threads were woken 60–130 ms late.
- The runner itself was correct: it reserves the next slot from the query's start and sleeps
  `min(remaining, 50 ms)`.
- Reproduced locally by wrapping `time.sleep` to oversleep. The old test fails at +30 ms.
- The fix records what the runner asks for. `RecordingClock.sleep` is passed through
  `run_benchmark`'s `sleep` seam and asserts that no worker requests a wake-up later than its
  last scheduled start + interval × 1.1. The real-time bound keeps only 0.5 s of slack, for
  stalls.
- The new test passes at +150 ms oversleep and fails for each of three broken schedulers: 50 %
  jitter, the next slot measured from the query's end, and a full-slice sleep.

## Decision / recommendation

- Pin the actions above by SHA, updated by monthly grouped Dependabot PRs with a 7-day cooldown.
- A `ci-passed` gate job is the single required check.
- release.yml calls ci.yml through `workflow_call`. `workflow_dispatch` is a dry run that
  publishes nothing.
- Follow-up: consider Dependabot's `pre-commit` ecosystem for the hook revs.

## Sources

- https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference
- https://github.com/astral-sh/setup-uv (action.yml at v10.1.0 / v10.2.0)
- https://github.com/actions/checkout (action.yml at v7.0.1)
- https://github.com/biomejs/pre-commit (.pre-commit-hooks.yaml at v2.5.14)
