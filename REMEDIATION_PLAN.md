# dns-bench — Distinguished-Engineer Review & Remediation Plan

## Context
dns-bench was generated in one shot from a vague prompt (see BACKGROUND.md). The owner asked whether
a distinguished engineer would approve of its structure (modularity, maintainability, security, …)
and, if not, for a remediation plan. **No code changes until this plan is approved.**

Method: I read the core Python modules myself. Then a read-only review workflow ran 5 specialist
reviewers (architecture, security, correctness/concurrency, maintainability/testing, frontend). Each
was followed by an adversarial verifier that tried to refute every finding against the code and
listed what the reviewer missed. Finally, an architect agent red-teamed the draft plan for ordering
mistakes, cost and technical errors. Test suite today: **216 pass, 2 skipped, ~10 s, no network**.
No finding was refuted; several were downgraded. Severities below are the verified ones.

**Updated 2026-09-27:** two housekeeping phases (lint/format, then CI) now come first, and the
phases after them are renumbered. The floor is now Python 3.13, and uv replaces pip/pyenv. The
review findings below are kept as recorded.

## Verdict: would a distinguished engineer approve? **No, not as-is.**
The problems are structural, not rot. The Python core is better than typical one-shot output: an
acyclic import graph, pure leaf modules (resolver, stats), good injection seams
(`query_fn/clock/sleep/rng`), careful atomic persistence, and strong local-server security.

| Dimension | Grade | Why |
|---|---|---|
| Security | **A** (verified, up from B) | No path a malicious web page can exploit. The Host allow-list stops DNS rebinding. JSON-only writes force a CORS preflight, which gets a 405. Strict CSP, no innerHTML, path-traversal guards. Remaining gaps are defence-in-depth. |
| Correctness / concurrency | **B** | Pacing, locks and cancellation are sound and tested. But the **recommendation isn't statistically trustworthy**: 2 lost packets flipped the backup; the backup differed across your 8 saved runs; with more than 8 servers, resolvers are timed in *different time windows*. |
| Architecture | **C** | Rules duplicated in Python and JS (already drifted). Untyped run records with no schema version, and stale stored analysis. CLI and server orchestrate runs separately (the UI path can lose a finished run). Storage holds analysis logic. |
| Maintainability / hygiene | **C** | Tracked `config.json` rewritten by the UI. Not a package. No CI, lint or type checking. Your ISP IPs ship as defaults. One test silently skipped since the archive move. `recommend()` is 269 lines. |
| Frontend | **C** | 2,800-line IIFE around a 30-field mutable global. **Zero JS tests** (server tests stub app.js, so a syntax error ships green). Re-renders destroy keyboard focus. Hand-copied backend rules. The detail work (safe DOM, CSP, a11y effort) is good. |

### Top verified findings
- **FE-2 / MNT-2 (high).** No test loads the real app.js (test_server.py:65-67 stubs it).
- **ARCH-1 / MNT-3 / FE-4.** Defaults, bounds, limits, the estimate and the put-first ranking are
  copied into app.js and index.html, and they have drifted: the JS estimate includes `tries`; the JS
  treats `::ffff:1.1.1.1` ≠ `1.1.1.1`. `classifyServerError` regex-parses server error prose.
- **ARCH-2 / MNT-11 / MNT-M2.** Untyped dict run records with no `schema`. Summary and
  recommendation are persisted and trusted on load, so the single-run view shows old logic while
  "All runs" uses new logic. The measured `truncated` flag is dropped.
- **ARCH-3 / MNT-M1.** `cmd_run` and `DNSBenchServer._job_main` orchestrate separately. The server
  skips the runs-dir check and has no rescue path, so a save failure discards a finished run
  (server.py:165-172).
- **ARCH-4 / MNT-1 / MNT-9.** `config.json` is tracked but rewritten by the UI. GET `/api/config`
  can create a file. The default "ISP" (61.9.134.49 / 61.9.133.193) makes other users see
  "ISP unreachable", which falsely suggests their DNS is broken.
- **ARCH-6 / MNT-5.** `recommend()` (recommend.py:127-395) interleaves selection with ~20 branches
  of prose. The formula is restated in 3 places.
- **MNT-4 / COR-8.** A bare `KeyError` means "no runs", so data bugs show as "No runs saved yet".
- **MNT-6 / MNT-7.** No pyproject or `__main__`; sys.path hacks everywhere; no CI or linters;
  Python 3.9 untested (your `/usr/bin/python3` is 3.9.6).
- **COR-1 / COR-2 / COR-M1.** `FAILURE_WEIGHT=2 × timeout` lets 2/122 failures add ~33 ms (4× a
  median). No ties for the backup. Rounds ≥2 measure a warm cache. Servers beyond
  `max_parallel_servers` run in sequential batches (runner.py:181-185).
- **FE-1 / FE-3 / FE-M1 / FE-5.** Monolithic state; focus lost on sort/chip/close; form errors not
  linked to inputs; the Failed-queries table is uncapped (20k rows possible).

## Decisions (from owner)
- **Scope: all phases 0–8.**
- **Housekeeping first (2026-09-27).** Lint/format (Phase 0) and CI (Phase 1) land before any
  remediation work. Python 3.13 is the floor. uv manages interpreters, environments and tools. Ruff
  (Python) and Biome (JS/CSS/JSON) both lint and format, and pre-commit hooks enforce them.
- **Data stays in the checkout**, untracked (`./config.json`, `./runs`). No XDG move. Packaging
  supports editable installs only.
- **Defaults: auto-detect system resolvers; keep the 60 domains (including .com.au).**
- **Frontend: keep a single app.js with no Node.** Move logic server-side, delete the JS copies,
  and test via Python. Accepted trade-off: a DE would still flag the single file, but it shrinks
  and loses every duplicated rule.

Cut after red-team (cost > benefit for a single-user tool): `X-DNS-Bench` header (the JSON
content-type already forces a preflight), `fcntl` run lock (no Windows; wrong scope), multiprocessing
for serve mode (breaks `query_fn` injection), bootstrap CIs (replaced with exact/Wilson intervals),
stable resolver ids, dataclass models (use `TypedDict`), global token bucket.

---

## Remediation plan
Each phase lands as its own PR(s) with the suite green; from Phase 1 on, CI must be green too.
Behaviour-preserving refactors are kept separate from behaviour changes. The runtime stays
stdlib-only. POSIX (macOS/Linux) only, stated in the README.

### Phase 0 — Lint & format (S)
Housekeeping. It comes first so that every later diff is linted and formatted from the start.
- `ruff.toml`:
  - `target-version = "py313"`, `line-length = 110`, rules `E, W, F, I, B, UP, SIM, A, RUF`.
  - `extend-include = ["dns-bench"]` so the launcher is checked too. It is exempt from the
    printf-formatting rules (it must stay parseable by old interpreters so its version check runs).
  - isort `required-imports = ["from __future__ import annotations"]` for `dnsbench/` and `tests/`
    (not the launcher).
- `biome.json`: recommended rules for JS, CSS and JSON. Formatter options match app.js's current
  style (indent, quotes) so the reformat stays small.
- `.pre-commit-config.yaml` with pinned revs:
  - `astral-sh/ruff-pre-commit`: `ruff check --fix`, then `ruff format`.
  - `biomejs/pre-commit`: `biome check --write`. pre-commit fetches its own Node for this hook, so
    nothing Node-related lands in the repo.
  - Setup: `uv tool install pre-commit && pre-commit install`.
- Fix what the tools flag today, in separate commits so that each one is reviewable:
  1. Formatter only (`ruff format`, `biome format`). Ruff reformats 17 of 22 Python files (~1,900
     lines). Add that commit's hash to `.git-blame-ignore-revs`. Keep layout that aids reading (the
     aligned field comments in `QueryResult`, the `RCODES` table) with `# fmt: skip`.
  2. Safe autofixes (`ruff check --fix`, `biome lint --write`).
  3. Manual fixes for the rest. Measured on 2026-09-27: 61 ruff findings (24 autofixable); Biome
     6 errors, 89 warnings, 67 infos (mostly `useOptionalChain` and `useTemplate`). Fixes must be
     behaviour-preserving. Any fix that changes behaviour is split out and noted.
- Docs: CLAUDE.md (a linter is now configured; the gate adds `pre-commit run --all-files`), a README
  Development section (installing the hooks, running the tools), and a `.project/DECISIONS.md` entry.
- Done when `pre-commit run --all-files` is clean, the suite is green on 3.13, a short live run
  works (the fixes touch runner.py and resolver.py), and the UI loads with no console errors.

### Phase 1 — CI: lint, test, release (S)
- Fix the 2 tests that fail on Python 3.14 before 3.14 joins the matrix. They are
  `test_unparseable_json_values` and `test_unconvertible_numbers_are_400_not_500`. Both assume that
  100,000-deep JSON raises `RecursionError`, but 3.14 parses it. The code still rejects it, just
  with a different message. This is a deliberate test fix, with the reason in the commit (CLAUDE.md
  hard rule 1).
- `.github/workflows/ci.yml`, run on pushes to main and on every PR:
  - `lint`: `pre-commit run --all-files --show-diff-on-failure`. It uses the same pinned versions as
    the local hooks, so there is one source of truth.
  - `test`: `astral-sh/setup-uv`; ubuntu + macOS × Python 3.13 + 3.14;
    `uv run --python <v> python -m unittest discover -s tests -v`; plus a `./dns-bench --version`
    launcher check.
  - Branch protection on main requires both jobs.
- `.github/workflows/release.yml`, triggered by pushing a `vX.Y.Z` tag:
  - Re-run lint and test.
  - Fail unless the tag matches `dnsbench.__version__`.
  - Create the GitHub Release, using that version's `CHANGELOG.md` section as the notes.
  - `permissions: contents: write` on this job only.
- `CHANGELOG.md` (Keep a Changelog format; v1.0.0 is the first entry) and a version-bump policy in
  the README: patch for fixes, minor for features or behaviour changes, major for breaking
  config/run-file changes. To release: bump `__version__` and the CHANGELOG in a PR, merge, tag.
- Actions are pinned to commit SHAs; `.github/dependabot.yml` keeps them updated.

### Phase 2 — Safety net (S)
- `pyproject.toml`: dynamic version from `dnsbench.__version__`, `requires-python>=3.13`,
  `[project.scripts] dns-bench = "dnsbench.cli:main"`, package-data `web/*`, and mypy config
  (`python_version = "3.13"`). Ruff config stays in `ruff.toml` from Phase 0. Document
  `uv tool install --editable .` only (data lives in the checkout).
- Add mypy (lenient) to the pre-commit hooks, which puts it in CI too.
- `dnsbench/__main__.py`. `./dns-bench` stays as the primary shim (symlink use keeps working).
- Tests: remove the `sys.path.insert` hacks and run `uv run python -m unittest discover -s tests`
  (cwd is on the path). Compare against `dnsbench.__version__`, not the `"1.0.0"` literal
  (test_runner.py:248, test_cli.py:272).
- Fix the silently skipped test (COR-11): path → `archive/dns-test.sh`, and fail instead of skipping.
- New Python smoke tests against the **real** `config.WEB_DIR`: index.html and every referenced
  `/static/*` load; the CSP header is present; no `innerHTML|outerHTML|insertAdjacentHTML|
  document.write` under `dnsbench/web` (FE-2, FE-12).
- Sanitized copies of 2 real v1 run files as fixtures (hostname and ISP IPs stripped), to pin the
  Phase 6 `migrate()`.

### Phase 3 — Security hardening + correctness bug fixes (S–M)
server.py:
- For non-GET `/api/*`, require `Origin` (when present) == `http://<validated Host>`. Add
  `Cross-Origin-Resource-Policy: same-origin` and `Cross-Origin-Opener-Policy: same-origin`.
- Drop `'unsafe-inline'` from `style-src` (verified safe: app.js only writes styles via CSSOM,
  app.js:115/470/1359). Add `require-trusted-types-for 'script'`. Override `send_error` so it goes
  through `_error` with the same headers.
- `Handler.timeout = 15`. Parse the `_host_ok` port with `re.fullmatch(r"[0-9]{1,5}")`. Add a test
  pinning HTTP/1.0, plus a comment on why keep-alive needs body draining (SEC-M1).
- Non-loopback `--host` requires `--allow-remote`. README documents `ssh -L` (SEC-1).
- `_job_main`: a `try/finally` lifecycle, and save the finished run to a rescue file when
  `save_run` fails (the data-loss fix; it moves into the service in Phase 6).

config.py:
- Add `MAX_RESOLVERS` (e.g. 20) and a total-queries cap.
- Check domain length before IDNA encoding (SEC-3).

runner.py:
- **Interleave instead of batching** (COR-M1): `max_workers = len(jobs)`. Remove the
  `max_parallel_servers` setting (normalize drops the unknown key, so old configs load) and the
  batch logic in `estimate` (config.py:538-546) and in app.js. Load stays bounded by the per-server
  limiter × the caps. *This changes results: note it in the CHANGELOG.*
- Workers catch errors per item. An unexpected crash cancels the run and saves a `partial` run
  instead of discarding everything (COR-4).

cli.py:
- SIGTERM handled like SIGINT.
- A second Ctrl-C does a best-effort save and exits 130 (COR-5).

### Phase 4 — Data hygiene & defaults (M)
- `git rm --cached config.json`, add it to .gitignore, and delete `runs/.gitkeep` (runs/ is created
  on demand). Defaults come only from code. Release note: pulling this commit deletes an unmodified
  `config.json` in other clones; your local file is kept.
- New `dnsbench/paths.py` (path constants move out of config.py). Checkout-relative
  `./config.json` and `./runs`, overridable by `--config/--runs-dir` or `DNSBENCH_HOME`. If the
  package isn't in a checkout (a non-editable install), fail with a clear message asking for
  `DNSBENCH_HOME`. Print the resolved paths in the `serve` banner, `config --path` and Settings.
- `load_config` becomes side-effect free: the file is created only on save/reset.
- **System resolver detection** (`dnsbench/sysdns.py`): macOS `scutil --dns`; Linux
  `/etc/resolv.conf`, falling back to `/run/systemd/resolve/resolv.conf` when it points at the
  127.0.0.53 stub. Loopback stubs are skipped.
  - Detected IPs are **written into the config** as a "System" resolver when defaults are created
    or reset. They are not re-detected per run, so one name never mixes IPs from different networks.
  - If nothing is detected, the entry is omitted.
  - Settings gets an "Add system resolvers" action; `config --detect` does the same from the CLI.
- Remove the hard-coded "ISP" entry. Keep `DEFAULT_DOMAINS` as-is. Update the tests that pin the
  personal defaults (MNT-M5).
- Move the script-history comments and the "vs dns-test.sh" UI copy (`ORIGINAL_SECONDS_PER_QUERY`)
  into a README history section. Fix README paths, quick start and layout; extend the Development
  section (added in Phase 0); link BACKGROUND.md; reconcile the README API table with `_ROUTES`.

### Phase 5 — One source of truth: validation & API contract (M)
Done before the layering so the error format changes only once.
- config.py: per-section validators returning structured `ValidationError(path, code, message)`.
  `ConfigError` carries them; `str()` keeps today's CLI text. API `details` becomes
  `[{path, code, message}]` with no absolute paths or exception text (SEC-8, ARCH-8).
- New endpoints:
  - `GET /api/schema`: defaults, `SETTING_BOUNDS`, record types, limits (domains, servers per
    resolver, name length, resolvers), presets (moved from app.js into config.py), scoring
    constants, slow caps.
  - `POST /api/config/validate`: the server validates the Settings draft (debounced from the UI).
  - `POST /api/estimate`; the estimate is also included in `/api/config` responses.
- app.js: build fields, limits and presets from the schema.
  - Delete `DEFAULT_SETTINGS`, `MIN_INTERVAL_MS`, the bound literals, `estimate()`, the duplicate
    rules in `validateDraft`, `classifyServerError` (errors are keyed on `path`), the put-first
    ranking fallback and ~100 lines of unreachable compat code (FE-8, MNT-14).
  - index.html `max="10"` comes from the schema.
- Contract tests: schema == the Python constants; no known bound literals in the web files;
  structured-error round trip through the API.

### Phase 6 — Backend layering & typed model (L)
- `dnsbench/models.py`: `TypedDict`s for `QueryRow` (adds `truncated`), `RunRecord`
  (`schema: 1`, `kind: "run"|"aggregate"`), `LatencyStats`, `Summary` and `Recommendation`, plus
  typed `QueryFn` and `ProgressFn` callables. mypy tightened on stats/recommend/config.
- `storage.py` → `RunRepository(runs_dir)`:
  - `save/load/exists/list`; it owns its cache; `logging` instead of `print`.
  - Typed `RunNotFound`, `NoRuns` and `CorruptRun(StorageError)`; bare `KeyError` is no longer
    caught at the boundaries.
  - One `migrate(raw)` entry point, pinned by the Phase 2 fixtures.
  - No imports of stats/recommend/report. Replaces the `runs_dir / f"{id}.json"` checks
    (server.py:525, cli.py).
- `dnsbench/analysis.py`: `finalize` and `aggregate` move out of storage.
  - **Raw results + the config snapshot are the source of truth.** Summary and recommendation are
    recomputed on load (measured cost: 32 ms for 8 runs), cached by (path, mtime, `ANALYSIS_VERSION`).
  - `aggregate` loads raw rows only.
  - The UI shows the analysis version. The `.txt` file is documented as a historical snapshot.
- `dnsbench/service.py`: `BenchmarkService.prepare(overrides) → RunPlan` (load, apply overrides,
  validate, estimate, runs-dir check), then `execute` and `persist` (with rescue).
  - `JobManager` (start/cancel/status/stop) owns `JobState` and the thread.
  - `cmd_run` and `DNSBenchServer` become thin adapters, absorbing the Phase 3 lifecycle, SIGTERM
    and rescue patches.
  - `start_job` no longer holds the job lock during disk IO (ARCH-M5). Jobs are built once.
- `recommend.py` split into `rank()` → typed ranking, `choose()` → structured best/backup/servers
  and ties, and `explain()` → text.
  - Notes are `{code, params, text}`: the server renders the text, so the prose isn't copied into JS.
  - The formula string is generated from the `W_*` constants and reused by report.py and the README.
- `estimate` moves to the service; small helpers are de-duplicated (MNT-12); caches are no longer
  process-global (MNT-18).

### Phase 7 — Frontend, single file (M)
- Organize app.js into clear sections. A single rAF-batched `scheduleRender()` replaces the
  scattered `render()`/`updateTabs()`/`renderProgress()` calls. View builders stop mutating state;
  small action functions do that instead (FE-1, within one file).
- Focus: keep focus on the sort, chip, show-all and close actions; move focus to the view heading on
  route change, but not on arrow-key tab moves (FE-3).
- Settings errors: `aria-invalid`, `aria-describedby` and a focused error summary on a failed save
  (FE-M1). Colours follow the resolver (FE-M2).
- Cap Failed queries at 200 rows, with "showing N of M" and a CSV link (FE-5).
- `AbortController` fetch timeouts; polling backoff and recovery instead of a permanent "lost
  contact" (FE-7).
- Tab/landmark semantics and a roving tabindex on charts (FE-9/10). Contrast, reduced-motion and
  theme fixes (FE-11/15).
- Re-enable Biome's `noDescendingSpecificity` (turned off in Phase 0) and fix its 12 warnings by
  reordering style.css, checking every view visually before and after.
- Python tests: the real-UI smoke tests from Phase 2, plus an API-contract test for every endpoint
  app.js calls (the endpoint names are grepped from app.js).

### Phase 8 — Measurement validity (M) — changes results; ship separately
- **Failure accounting** (COR-1, COR-M2):
  - Classify failures as timeout, fast rcode (SERVFAIL/REFUSED) or local error (send/socket/
    exception). Local errors are not charged to the resolver.
  - Penalise failures only when the difference is significant (Wilson interval).
  - `retried` counts retry-then-success only.
- **Uncertainty** (COR-2): an exact median CI (order statistics via `math.comb`) plus Wilson
  intervals, both deterministic. Positions whose intervals overlap, including the backup, are
  reported as ties.
- **Cache effects**: tail stats use only the first query per (provider, domain); warm and cold are
  shown separately; the report carries a note.
- **Reply validation** (SEC-4, COR-9):
  - Require the question section to echo the query (name compared case-insensitively). Accept
    `QDCOUNT=0` on error rcodes, so FORMERR/REFUSED don't turn into timeouts.
  - Require opcode 0.
  - Use a connected UDP socket (ICMP errors then show as "error" rather than "timeout").
- **Answer quality** (COR-M3): record `truncated`; flag NXDOMAIN or empty NOERROR where other
  resolvers answered (filtering/hijack detection).
- **Serve-mode timing** (COR-3): first measure CLI vs serve on the same config. If serve distorts
  results, use kernel receive timestamps (`SO_TIMESTAMP` via `recvmsg`) in resolver.py, which fixes
  both paths.
- Bump `ANALYSIS_VERSION`. Old runs recompute under the new scoring; their `.txt` files keep the
  old verdicts. Documented in the CHANGELOG.

## Critical files
dnsbench/{server,cli,config,storage,runner,recommend,stats,report,resolver}.py; new
dnsbench/{paths,sysdns,models,analysis,service,__main__}.py; dnsbench/web/{app.js,index.html};
tests/*; ruff.toml; biome.json; .pre-commit-config.yaml; .git-blame-ignore-revs;
.github/workflows/{ci,release}.yml; .github/dependabot.yml; pyproject.toml; .gitignore; README.md;
CHANGELOG.md.

Reuse:
- `runner.run_benchmark` seams (`query_fn/clock/sleep/rng`) and `make_server(..., web_dir=)`
- `config.server_key/normalize_server`, `stats.nearest_rank`
- `config._atomic_write_text_raw`, `storage._link_new`
- `cli._check_runs_dir/_rescue_run`, which move into the service

## Verification (every phase)
- `uv run python -m unittest discover -s tests` green locally, and under
  `uv run --isolated --python 3.13`. `pre-commit run --all-files` clean. From Phase 1 on, CI green
  (lint, plus tests on 3.13/3.14 × ubuntu/macOS); mypy clean from Phase 2.
- End to end:
  - `./dns-bench run --rounds 1`; `./dns-bench serve --open`, then run from the UI and check
    Overview, By resolver, By domain, History and Settings.
  - Save an invalid config: errors must be linked to their fields.
  - Keyboard-only pass: sort, chips, detail close.
- Security regression tests: a cross-site `Origin` on a mutation, a bad Host or port, OPTIONS →
  405 with no CORS headers, an oversized body, CORP/COOP/CSP headers present.
- Phase 4: a fresh clone creates a config with a "System" resolver; your existing
  `~/source/dns-bench/config.json` and 8 runs keep working unchanged.
- Phase 6: every file in `runs/` loads via `migrate()`. A one-run aggregate matches the single-run
  view.
- Phases 3 & 8: compare old vs new recommendations on the 8 saved runs. With 10 servers enabled,
  per-resolver time spans overlap.
