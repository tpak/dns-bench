# dns-bench

[![ci](https://github.com/tpak/dns-bench/actions/workflows/ci.yml/badge.svg)](https://github.com/tpak/dns-bench/actions/workflows/ci.yml)

A fast, polite DNS resolver benchmark with a local web UI. It replaces `archive/dns-test.sh`,
which is left unchanged ([History](#history)).

It times how quickly each DNS resolver (Cloudflare, Google, OpenDNS, the one your computer
already uses, …) answers for a list of popular domains. It then recommends which resolver to use, which IP to put
first, and which to use as the backup. Every run is saved. Results can be viewed as text,
or as charts in a browser (averages, per resolver, per domain and over time).

It needs only Python 3.13+ (standard library only, no `pip install`). It doesn't need `dig`,
and the web UI doesn't load anything from the internet. The web UI needs Chrome or Edge 93,
Firefox 92 or Safari 15.4 (iOS/iPadOS 15.4) or later. A few row tints, focus-ring shadows and
the progress spinner use `color-mix()`, which needs Chrome/Edge 111, Firefox 113 or Safari 16.2;
older browsers leave them out.

If your `python3` is older (macOS ships 3.9, Debian 12 and Raspberry Pi OS bookworm ship
3.11, Ubuntu 24.04 ships 3.12), run dns-bench with a newer interpreter, for example
`python3.13 ./dns-bench`, or let [uv](https://docs.astral.sh/uv/) fetch one:
`uv run --no-project --python 3.13 ./dns-bench`. (`--no-project` runs the script as it is; without
it, uv sets up a `.venv/` in the checkout and downloads a build tool the first time.)

## Fast, and still polite

A run with the default settings (about 7 servers × 60 domains) takes 15–20 seconds, yet no
server ever gets more than 4 queries a second from it. `dns-bench` runs one worker per server
IP, and each worker:

* sends **one query at a time**, so a server never has more than one outstanding query
  from us;
* waits at least `per_server_interval_ms` (default 250 ms, plus 0–10 % random jitter)
  between the *starts* of consecutive queries. This also applies after a timeout and to
  retries, so a slow or dead server never gets a burst of catch-up queries;
* has a hard 50 ms floor on that interval (at most 20 q/s per server), whatever the
  config says.

All the servers run at the same time, however many there are, so the wall time is about
`domains × rounds × interval` (60 × 0.25 s ≈ 15 s) instead of the sum of all the servers. It
also means every server is measured over the same stretch of time, so network conditions
that change during a run affect them all alike.
The total load across every server is at most `servers × 1000/interval` queries/s
(about 7 × 4 = 28 q/s by default), spread over the enabled providers. A unit test
(`tests/test_runner.py`) uses real threads to check both rules: never more than one query
in flight per server, and never less than the interval between starts. It also checks that
servers really do run concurrently.

Each server's domain list is shuffled independently. This stops the two IPs of one provider
from asking for the same name at the same moment, which would skew results through a shared
cache.

## Quick start

```sh
git clone https://github.com/tpak/dns-bench.git
cd dns-bench
./dns-bench run              # benchmark now (~15-20 s), print a report, save it under runs/
./dns-bench serve --open     # web UI at http://127.0.0.1:8053/
```

The first run creates `config.json` in the checkout, with the default resolvers plus the ones
your computer already uses, listed as **System** (see [Configuration](#configuration-configjson)).

The launcher also works from any directory and through a symlink. For example,
`ln -s ~/dns-bench/dns-bench ~/bin/dnsb`. From the checkout, `python3 -m dnsbench` works too (with a
`python3` that is 3.13 or newer).

### Installing a `dns-bench` command (optional)

Nothing needs installing: `./dns-bench` runs straight from the checkout. To put a `dns-bench` command
on your PATH instead, install the checkout with [uv](https://docs.astral.sh/uv/):

```sh
cd ~/dns-bench
uv tool install --editable .
```

Only editable installs (`--editable`) are supported. dns-bench keeps `config.json` and `runs/` in
the checkout, so the command has to run the checkout's own code. A non-editable install stops with
a message asking you to set `DNSBENCH_HOME` ([Where your data is kept](#where-your-data-is-kept)).
Undo the install with `uv tool uninstall dns-bench`.

Example output:

```
Cloudflare: mean=5.8 median=5.7 p80=6.3 p95=6.8 p98=6.8 min=4.5 max=6.8 n=120 fail=0.0%
Google: mean=6.2 median=5.7 p80=6.8 p95=8.5 p98=8.5 min=4.8 max=8.5 n=120 fail=0.0%
...
Recommendation: Use Cloudflare: put 1.0.0.1 first and 8.8.8.8 (Google) second. ...
```

While a run is going, `[slow]` and `[fail]` lines are printed to stderr, like the original
did, along with a live progress line showing done/total, elapsed time and ETA. Press Ctrl-C
to stop early: the queries already done are still saved (with status `cancelled`) and the
exit code is 130. The first Ctrl-C waits for the queries still in flight, at most one timeout;
press it again to save straight away. `kill` (SIGTERM) works like Ctrl-C.

## Commands

| Command | What it does |
|---|---|
| `dns-bench run [--rounds N] [--interval-ms N] [--timeout-ms N] [--resolvers A,B] [--no-save] [--quiet] [--json]` | Runs a benchmark. The overrides apply to this run only and are not saved to the config. `--resolvers` picks resolvers by name, even disabled ones (e.g. `--resolvers Quad9,Cloudflare`). `--json` prints the full run record instead of the text report. |
| `dns-bench serve [--host 127.0.0.1] [--port 8053] [--open] [--allow-remote]` | Starts the web UI and JSON API. `--open` opens it in your browser. A `--host` that other machines can reach needs `--allow-remote` (see [The web UI](#the-web-ui)). |
| `dns-bench list` | Lists saved runs, newest first. |
| `dns-bench report [latest\|all\|RUN_ID]` | Prints the text report for the latest run, a specific run, or `all` runs combined. |
| `dns-bench config [--show\|--reset\|--detect\|--path]` | Shows the config (the default), resets it to the defaults, adds or updates the **System** resolver with the ones your computer uses now (`--detect`), or prints the config file's path (and the runs folder's, on stderr). Showing a config that doesn't exist yet prints what it would start with, without creating it. |

Every command accepts `--config PATH` and `--runs-dir DIR`, either before or after the
command name ([Where your data is kept](#where-your-data-is-kept)). Exit codes are: 0 ok, 1 error (including a run in which no query succeeded; that
run is still saved), 2 usage error, 130 interrupted (Ctrl-C). `run` checks that it can write to
the runs directory before sending any queries.

## The web UI

`./dns-bench serve` opens a single page with these tabs:

* **Overview** – the recommendation card (suggested primary and secondary IPs, with copy
  buttons), a chart of mean latency per resolver with median and p95 markers, failure rates,
  a sortable stats table, and median-over-time once there are several runs.
* **By resolver** – per-server comparison, latency histogram, per-domain chart and slow
  queries for one resolver.
* **By domain** – a heatmap of domains × resolvers, with search and sorting.
* **History** – every saved run, with a CSV download for each.
* **Settings** – edit the resolver list, the domain list and the tuning settings, then save
  them to `config.json`. The page shows the estimated run time and the maximum load before
  you save, and where the config and the runs are kept. **Add system resolvers** adds (or
  updates) a **System** row with the resolvers your computer uses now.

The dataset picker at the top chooses which data every tab shows: the latest run, any single
run, or **All runs combined**.

Everything works from the keyboard. The arrow keys move between the tabs; Tab reaches each chart
once, and the arrow keys (Home and End too) then move between its bars or points. Sorting a table,
picking a resolver or closing a domain's details leaves the keyboard where it was. After a Settings
save that fails, the list of problems gets the focus, and each problem takes you to its field. The
page also follows the system's light or dark theme and its reduced-motion setting.

By default the server listens on 127.0.0.1 only. It answers only requests addressed to it by a
loopback name (`localhost`, `127.0.0.1`, `[::1]`), by the address given to `--host`, or, when it
listens on every interface (`--host 0.0.0.0`), by any IP address; never by another host name
(protection against DNS rebinding). A state-changing request must be
`Content-Type: application/json`, and if it comes from a web page, that page must be the UI
itself (its `Origin` header is checked), so other sites can't change your config or start runs.
The page runs under a strict Content Security Policy.

There is no authentication, so `serve` refuses a `--host` that other machines can reach, such
as `0.0.0.0` or a LAN address, unless you add `--allow-remote`. Other computers then have to use
an IP address in the URL (for example http://192.168.1.10:8053/), not a host name. The safer way
to use the UI from another computer is to forward the port over SSH and keep the default host:

```sh
ssh -L 8053:127.0.0.1:8053 you@machine-running-dns-bench
```

Then open http://127.0.0.1:8053/ on the computer you ran `ssh` on.

## Configuration (`config.json`)

The Settings tab edits this file, and so can you (the web UI overwrites it when you save).
Everything is checked before it is saved. Invalid input is rejected with readable messages.
The file is yours: it is not part of the repository, and nothing but a save, a reset or
`config --detect` ever writes it.

```json
{
  "resolvers": [
    {"name": "Cloudflare", "servers": ["1.1.1.1", "1.0.0.1"], "enabled": true}
  ],
  "domains": ["google.com", "bbc.co.uk", "..."],
  "settings": {
    "per_server_interval_ms": 250,
    "timeout_ms": 1000,
    "tries": 1,
    "rounds": 1,
    "slow_threshold_ms": 200,
    "record_type": "A",
    "shuffle": true
  }
}
```

A new config starts with OpenDNS, Cloudflare, Google and Quad9 (Quad9 disabled), 60 popular
domains, and the settings below. It also gets a **System** resolver: the servers your computer is
set up to use, which are usually your ISP's or your router's. On macOS they come from
`scutil --dns`; on Linux from `/etc/resolv.conf`, or from `/run/systemd/resolve/resolv.conf` when
that only lists systemd-resolved's local stub. Local stubs (127.x, ::1) are left out, and so is any
server another resolver in the list already has. If nothing usable is found, there is no System
entry.

The System servers are looked up once, when the config is created or reset, and then stay in the
file. They are not looked up again for every run: on a laptop that moves between networks, that
would mix different networks' resolvers under one name. After moving to another network, run
`dns-bench config --detect` (or **Add system resolvers** in Settings) to update them.

| Key | Default | Range | Meaning |
|---|---|---|---|
| `resolvers` | 4 providers + System | 1–20 | The resolvers to compare. One run can send at most 50,000 queries (enabled servers × domains × rounds); a bigger config is rejected with a message saying so. |
| `resolvers[].name` | – | 1–40 chars, unique | Display name. No commas. |
| `resolvers[].servers` | – | 1–4 IPv4/IPv6 literals | Each IP can appear only once across all resolvers, however it is spelled (`::ffff:1.1.1.1` is `1.1.1.1`). Multicast, broadcast, reserved and unspecified addresses are rejected, and an IPv6 zone ID (`%en0`) is only allowed on a link-local `fe80::` address. |
| `resolvers[].enabled` | `true` | bool | At least one resolver must be enabled. |
| `domains` | 60 sites | 1–500 | Names are lower-cased, a trailing dot is removed, duplicates are dropped, and Unicode names are IDNA-encoded. |
| `per_server_interval_ms` | 250 | 50–5000 | Minimum gap between query starts to the same server. This is what keeps the load polite: 1000/interval = max queries/s per server. |
| `timeout_ms` | 1000 | 200–10000 | How long to wait for an answer. |
| `tries` | 1 | 1–3 | Attempts per query. A retry happens only after a timeout, and is paced by the interval like any other query. Each result row records its `attempts`; a query that needed a retry counts as `retried` and is penalised in the score, and its `ms` is the retry's round trip. |
| `rounds` | 1 | 1–10 | How many times each domain is queried on each server. More rounds give steadier numbers. |
| `slow_threshold_ms` | 200 | 1–10000 | Answers slower than this are listed as `[slow]`. |
| `record_type` | `A` | `A`, `AAAA` | Query type. |
| `shuffle` | `true` | bool | Shuffle each server's domain order independently. |

## How the recommendation works

A **status** is recorded for every query:

* `ok` – the server answered with NOERROR or NXDOMAIN;
* `error` – it answered with SERVFAIL, REFUSED and so on, or a socket error occurred;
* `timeout` – no answer arrived before the timeout.

Latency statistics (mean, median, p80/p95/p98, min, max, stdev) use **only `ok` answers**.
Percentiles use the same nearest-rank method as the original awk script,
`idx = ceil(p/100 × n)`, which a test checks against the original awk program. Failures are
reported separately as `failure_rate`.

Each resolver with at least one answer then gets a score, where lower is better:

```
score = 0.5 × median + 0.3 × p95 + 0.2 × mean + failure_rate × timeout_ms × 2 + retry_rate × timeout_ms
```

(The text report states the same formula; both come from `recommend.SCORE_FORMULA`, and a test keeps
this line in step with it.)

* The **median** measures typical speed and gets the most weight.
* The **p95** rewards consistency, because a resolver with occasional slow answers is annoying.
* The **mean** captures everything in between.
* **Failures** are penalised heavily. Each failed lookup makes your device wait a full
  timeout before it falls back, so a 2 % failure rate with a 1 s timeout adds 40 points.
* **Retries** (only with `tries` > 1) cost a full timeout each, even when the retry answered.
* A server that **never answered** (e.g. an IPv6 address on a network without IPv6) is left
  out of its resolver's score, because it is never suggested. A note names it.

From the ranking:

* **Best** is the lowest score. Any resolver within max(2 ms, 10 %) of it is reported as
  *tied*, meaning within noise.
* **Put first** is the best resolver's server with the lowest median once its failures and
  retries are penalised the same way as in the score, so an IP that drops queries never beats
  a reliable sibling. Medians within max(0.5 ms, 5 %) of each other are noise: a clearly lower
  p95 then decides, otherwise config order does, and a note says either can go first.
* **Put second** is the put-first server of the best *different* provider, so an outage at one
  provider doesn't take down both entries. A resolver that shares a server IP with the best
  one (in "All runs combined", e.g. the same resolver renamed) doesn't count as different.
  Without a different provider, it is the best resolver's next server.
* **Notes** warn about failure and retry rates above 2 % (naming the IP when one server of a
  resolver is to blame), fewer than 30 successful samples ("run more rounds"), ties, servers
  and resolvers that never answered, and when there is no second provider or server.

Results depend on your network and the time of day. "All runs combined" in the UI, or
`dns-bench report all`, merges every saved run and gives a steadier answer. In that view a
resolver seen only in older runs that is no longer enabled in `config.json` (disabled,
removed or renamed since) is still ranked but never suggested, and a note says so. The
"Runs" column and a note show when a resolver was measured in only some of the runs.

## Where your data is kept

Your config and your runs live in the checkout: `config.json` and `runs/`, next to the
`dns-bench` launcher, whatever directory you run it from. Neither is part of the repository
(both are git-ignored), so `git pull` never touches them. The one exception is the pull that
upgrades from 1.1.0 or earlier, which still had `config.json` in the repository: see "Upgrading"
in the [1.2.0 release notes](CHANGELOG.md).

* `DNSBENCH_HOME=/some/dir` keeps both in `/some/dir/config.json` and `/some/dir/runs/`. Files
  already in the checkout are not moved for you: `mkdir -p /some/dir && mv config.json runs /some/dir/`.
* `--config PATH` and `--runs-dir DIR` move one each, and win over `DNSBENCH_HOME`.
* A non-editable install (not in a checkout) needs `DNSBENCH_HOME`, or both flags.

`dns-bench serve` prints both paths when it starts, `dns-bench config --path` prints them, and
the Settings tab shows them.

Every run is saved in `runs/` and is **never deleted or overwritten** by the tool. Each run
produces two files:

* `runs/<id>.json` – the full record: a config snapshot and every raw query result. `<id>` is
  the UTC start time, e.g. `20260925T023456Z`. A `-2`, `-3`, … suffix is added if two runs start
  in the same second.
* `runs/<id>.txt` – the text report that `run` printed at the time.

The summary statistics and the recommendation are worked out from the raw results **every time a
run is loaded**, so every view (a single run, all runs combined, `dns-bench report`) uses the
current analysis, whatever version measured the run. The web UI's footer shows which analysis
version that is. The `.json` file also holds the summary and recommendation as they were when the
run was saved, for other tools, but dns-bench never reads them back. The `.txt` report is a
snapshot too: it keeps the verdict from the day of the run, while `dns-bench report <id>` prints
the same report with today's analysis. Run files from older versions load unchanged.

Raw results can also be downloaded as CSV from the History tab, or from
`/api/runs/<id>/csv`.

## JSON API (used by the UI)

| Method & path | Purpose |
|---|---|
| `GET /api/config` · `PUT /api/config` · `POST /api/config/reset` | Read, save, or reset the config. Each returns `{"config", "errors", "estimate"}`: the (normalised) config, its problems (`[]` when valid; a hand-edited file can have some) and what a run of it costs. Reading a config that doesn't exist yet returns what it would start with, without creating it. |
| `POST /api/config/validate` | Body: a config draft, which may be raw form input (servers and domains as text, numbers as strings). Returns the same three fields for it, plus `duplicate_domains`, and saves nothing. |
| `POST /api/config/system-resolver` | Body: a Settings draft (`{"resolvers": [...]}`). Returns this computer's resolvers as a System entry for it (`resolver`, or `null` if there is nothing to add) and a `message`. Saves nothing. |
| `POST /api/estimate` (`{"rounds": N, "config": {...}}`, both optional) | What a run costs: queries, expected and worst-case seconds, load per server and in total. Without `config`, of the saved config. |
| `GET /api/schema` | The rules: the defaults, each setting's type and bounds, the limits, the presets, the scoring constants and the error codes. The web UI builds its forms from this instead of keeping its own copy. |
| `GET /api/info` | The version, and where the config file (and whether it exists yet) and the runs folder are. |
| `GET /api/runs` · `GET /api/runs/<id>` · `GET /api/runs/<id>/csv` | List runs, get one run in full (with its summary and recommendation, freshly analysed, and `analysis_version`), or download its raw results as CSV. |
| `GET /api/aggregate?runs=all` or `?runs=id1,id2` | Merged summary and recommendation for several runs, plus `coverage` (how many of the runs measured each resolver). |
| `POST /api/run` (`{"rounds": N}` optional) · `GET /api/status` · `POST /api/run/cancel` | Start a background benchmark, poll its progress, or cancel it. |

Errors come back as `{"error": "...", "details": [...]}` with a matching HTTP status. Each detail is
`{"path", "code", "message"}`: `path` says where the problem is (`resolvers[0].servers[1]`,
`domains[3]`, `settings.rounds`, or `""` for the whole config or request), `code` what kind it is
(`GET /api/schema` lists the config ones), and `message` is the sentence the command line prints.
Messages never include a file's absolute path. The request body limit is 1 MB.

A recommendation's notes are `{"code", "params", "text"}` too: `code` and `params` say what a note
is about (for example `failure_rate` with the resolver and the rate), and `text` is the sentence to
show.

## Project layout

```
dns-bench            launcher (python3)
config.json          your config (created on first use; git-ignored)
runs/                every saved run (created on first use; git-ignored)
CHANGELOG.md         what changed in each release
BACKGROUND.md        why dns-bench exists (the original request)
archive/dns-test.sh  the script dns-bench replaced, unchanged
pyproject.toml       packaging (the `dns-bench` command) and mypy settings
uv.lock              uv's lock file (there are no dependencies to pin yet)
dnsbench/
  __main__.py        `python -m dnsbench`
  paths.py           where config.json and runs/ are (checkout, DNSBENCH_HOME, flags)
  config.py          defaults, validation, load / save
  sysdns.py          finds this computer's own resolvers (the System entry)
  models.py          the shapes of run records, summaries and recommendations (TypedDicts)
  resolver.py        pure-Python UDP DNS client (random ID, ID/source checks, IPv4+IPv6)
  runner.py          rate-limited concurrent scheduler
  service.py         one path for a run: prepare, measure, save (CLI and web UI), the UI's job
  storage.py         reads and writes run files, and brings old ones up to date (migrate)
  analysis.py        summaries and recommendations, recomputed from the raw results; aggregates
  stats.py           nearest-rank stats; summaries by resolver / server / domain
  recommend.py       scoring: rank, choose, explain
  report.py          text report
  server.py          HTTP server and JSON API (a thin layer over service.py)
  cli.py             command line (a thin layer over service.py)
  web/               the UI (plain HTML/CSS/JS, hand-drawn SVG charts)
tests/               unittest suite, one test_<module>.py per module (test_web.py for web/)
  samples.py         sample runs several test modules share
  fixtures/          sanitized run files written by older versions (see its README)
ruff.toml            Python lint and format settings
biome.json           JS, CSS and JSON lint and format settings
.pre-commit-config.yaml  git hooks that run ruff, mypy and Biome on every commit
.github/             CI and release workflows (GitHub Actions), Dependabot settings
```

## Running the tests

```sh
uv run python -m unittest discover -s tests -v
```

`uv run` uses the newest Python that uv manages. To check the 3.13 minimum too (CI tests 3.13 and
3.14 on Linux and macOS):

```sh
uv run --isolated --python 3.13 python -m unittest discover -s tests -v
```

The first `uv run` creates the project environment in `.venv/` (git-ignored). The tests don't need
the network: they use fakes, local UDP mock servers and a server started on a random localhost
port. A single optional live query to 1.1.1.1 runs only when
you ask for it:

```sh
DNSBENCH_LIVE=1 uv run python -m unittest discover -s tests -p test_resolver.py -v
```

## Development

Every commit is checked by [pre-commit](https://pre-commit.com) hooks:
[ruff](https://docs.astral.sh/ruff/) lints and formats Python, [mypy](https://mypy-lang.org/)
type-checks it, and [Biome](https://biomejs.dev/) lints and formats the web UI's JavaScript, CSS and
JSON. A commit is refused until they pass; most problems are fixed
automatically, so re-stage and commit again. One-time setup in each clone, with
[uv](https://docs.astral.sh/uv/):

```sh
uv tool install pre-commit
pre-commit install
```

| Task | Command |
|---|---|
| Check the whole repo | `pre-commit run --all-files` |
| Type-check only | `pre-commit run mypy --all-files` |
| Update the pinned tool versions | `pre-commit autoupdate` |
| Make `git blame` skip the one-off reformat commit | `git config blame.ignoreRevsFile .git-blame-ignore-revs` |
| Run one test module, or the tests whose name matches | `uv run python -m unittest discover -s tests -p test_config.py`, `uv run python -m unittest discover -s tests -k test_config_commands` |
| Try the UI on sample data, away from your own | `DNSBENCH_HOME=$(mktemp -d) ./dns-bench serve --runs-dir tests/fixtures/runs-v1` |

Settings live in `ruff.toml`, `biome.json` and `pyproject.toml` (`[tool.mypy]`). mypy checks the
core modules strictly (every function annotated, generics spelled out): config, stats, recommend,
report, models, storage, analysis, service, paths and sysdns. The front ends (cli, server), the
runner, the resolver and the tests use its lenient defaults, which skip the bodies of functions
without annotations.

### Continuous integration

GitHub Actions (`.github/workflows/ci.yml`) runs on every push to `main` and every pull request:

- `lint`: the same pre-commit hooks (ruff, mypy, Biome), at the same pinned versions, over the whole
  repo. If it fails
  on a PR, run `pre-commit run --all-files` locally and commit what it fixes.
- `test`: the unit tests on Linux and macOS with Python 3.13 and 3.14, plus the `./dns-bench`
  launcher, run directly and through a symlink, and refusing Python 3.12, and the `dns-bench`
  command that `pyproject.toml` defines.
- `ci-passed`: succeeds only if both jobs did. It is the check `main`'s branch protection requires,
  so a pull request can't merge until CI is green.

Actions are pinned to commit SHAs. Dependabot opens one grouped PR a month to update them, and
skips releases younger than a week.

## Releasing

Versions are `MAJOR.MINOR.PATCH`:

- **patch** for bug fixes;
- **minor** for new features or behaviour changes, including different results from the same
  measurements and a higher minimum Python;
- **major** for changes that break existing `config.json` or saved run files.

To release:

1. In a pull request, set `__version__` in `dnsbench/__init__.py`, and in `CHANGELOG.md` rename
   `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, add a fresh `## [Unreleased]` above it and
   update the compare links at the bottom. Merge it.
2. Optional dry run: Actions > release > Run workflow, on `main`. It runs every check and shows the
   release notes in the run summary without publishing anything.
3. Tag the merge commit and push the tag:

   ```sh
   git switch main && git pull --ff-only
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git merge-base --is-ancestor vX.Y.Z origin/main && git push origin vX.Y.Z
   ```

The release workflow re-runs lint and tests. It fails unless the tagged commit is on `main`, the tag
matches `__version__` and `CHANGELOG.md` has notes for it. Then it publishes the GitHub release with
those notes.

If the release run fails:

- A flaky check: open the run (Actions > release) and choose **Re-run failed jobs**. It re-runs at
  the same commit and then publishes.
- A wrong tag (wrong commit, or a version mismatch): delete the tag, fix `main` through a pull request,
  then tag again:

  ```sh
  git push origin :refs/tags/vX.Y.Z && git tag -d vX.Y.Z
  ```

## History

dns-bench started as `archive/dns-test.sh`, a zsh script its author used for years to compare
resolvers after an ISP change or just to check. [BACKGROUND.md](BACKGROUND.md) has the request
that turned it into this tool. The script is kept, unchanged, in `archive/`.

| | `dns-test.sh` | `dns-bench` (default settings) |
|---|---|---|
| Scheduling | every query one after another, with a 0.8 s sleep after each | servers are queried **in parallel**, and each server's queries are **strictly paced** |
| 4 resolvers × 2 servers × 60 domains = 480 queries | about **6.5 minutes** (480 × 0.8 s plus query time) | about **15–20 seconds** |
| Load on any one server | about 1 query/s, in bursts per provider | at most **4 queries/s** (≥ 250 ms apart), and never more than **1 in flight** |
| Timeouts | counted as **0 ms**, which made failing resolvers look *faster* | left out of the latency stats and reported as a failure rate |

The script was slow because of its global sleep, not because of DNS. Some of its choices live
on: the 60 default domains are its list, in its order; Quad9 is disabled by default because the
script defined it but left it out of its `order` list; and percentiles use its awk program's
nearest-rank method, which a test checks. Its fifth resolver was its author's ISP; a new config
gets your own computer's resolvers in that place, as **System**.
