# Changelog

Notable changes to dns-bench, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Version numbers follow the policy in the
README's [Releasing](README.md#releasing) section. Each release's section becomes its GitHub
release notes.

## [Unreleased]

## [1.2.0] - 2026-09-29

### Upgrading

- `config.json` is no longer part of the repository (see Changed). If you have changed your config
  (saving Settings in the web UI does), move it aside while you pull. Otherwise `git pull` stops with
  "Your local changes to the following files would be overwritten by merge: config.json" (or, with
  `pull.rebase` set, "cannot pull with rebase: You have unstaged changes"):

  ```sh
  mv config.json config.json.mine && git pull && mv config.json.mine config.json
  ```

  Don't follow git's advice to stash instead: `git stash pop` then fails with a conflict, and
  resolving it with `git add` puts `config.json` back into the repository. If you never changed the
  file, pulling deletes it, and the next run creates a new one. Your saved runs are not affected.
- A config you keep has no **System** resolver (see Added), and still has "ISP", whose two addresses
  only work on the original author's network. Run `dns-bench config --detect` (or click
  **Add system resolvers** in Settings, then **Save**) to add System, and delete ISP in Settings.
- A config can now list at most 20 resolvers (disabled ones count), and a run can send at most
  50,000 queries: enabled servers × domains × rounds (see Changed). 1.1.0 had neither limit, so a very
  large `config.json` it ran can now be refused: `dns-bench run` stops before sending any queries, with
  a message naming the limit. Fix it in Settings (`dns-bench serve` still opens and lists the problem)
  or in the file: delete resolvers you don't use, or disable servers, or use fewer domains or rounds.
  Configs within the limits load unchanged.
- To go back to 1.1.0, move `config.json` aside first as well: 1.1.0 has the file in the repository,
  so checking it out silently replaces yours with 1.1.0's default.

  ```sh
  mv config.json config.json.mine && git checkout v1.1.0 && mv config.json.mine config.json
  ```

  1.1.0 lists runs saved by 1.2.0 and includes them in `report all` and "All runs combined". But
  `dns-bench report` (or `report <id>`) for one of them stops with `TypeError: can only concatenate
  str (not "dict") to str`, and the web UI shows that run's notes as "[object Object]", because 1.2.0
  stores notes as objects (see Changed). Nothing is lost: the run's `.txt` report still works, and
  1.2.0 reads the file again. A `config.json` saved by 1.2.0 works in 1.1.0.

### Added

- `python3 -m dnsbench`, run from inside the checkout, takes the same commands as `./dns-bench`, and
  also stops with a clear message on a Python older than 3.13.
- An optional `dns-bench` command on your PATH: run `uv tool install --editable .` in the checkout
  (README, "Installing a `dns-bench` command"). `./dns-bench` still needs no install. A non-editable
  install (`uv tool install .`) has no checkout to keep data in, so it stops with a message asking you
  to set `DNSBENCH_HOME` (or to pass `--config` and `--runs-dir`).
- A **System** resolver: the DNS servers your computer is set up to use (from `scutil --dns` on
  macOS, `/etc/resolv.conf` on Linux, following systemd-resolved to its upstream servers). A new
  config gets it, leaving out servers another resolver already has. `dns-bench config --detect`, or
  **Add system resolvers** in Settings followed by **Save**, adds it to an existing config, or updates
  it after you move to another network.
- `DNSBENCH_HOME=/some/dir` keeps `config.json` and `runs/` in that directory instead of the checkout.
  Files already in the checkout are not moved: move them yourself
  (`mkdir -p /some/dir && mv config.json runs /some/dir/`). `dns-bench serve` prints where both are
  when it starts, `dns-bench config --path` now also prints the runs folder (on stderr, so its output
  is still just the config path), and the Settings tab shows them.
- Web API: `GET /api/schema` (the built-in defaults, every setting's type and bounds, the limits, the
  presets, the scoring constants and the error codes), `GET /api/info` (the version, and where
  `config.json` and `runs/` are), `POST /api/config/validate` (checks a draft without saving it),
  `POST /api/config/system-resolver` (detects the System resolver for a draft) and
  `POST /api/estimate` (what a run would cost). `GET /api/runs/<id>` and `GET /api/aggregate` now
  include `kind` and `analysis_version`, and a run also `schema`. See README "JSON API".

### Changed

- `config.json` and `runs/` are created on first use and are git-ignored. A new config's defaults
  no longer include "ISP", whose two addresses only worked on the original author's network (anyone
  else saw it as unreachable); the System resolver takes its place. dns-bench never rewrites a config
  you already have: see Upgrading for adding System to it.
- Reading the config never creates or rewrites it. `dns-bench config` shows what a missing config
  would start with, without writing it. The file is created by the first saved run, by
  `dns-bench serve`, by saving or resetting Settings, or by `dns-bench config --reset` or `--detect`.
- The Settings estimate no longer compares the run time with the old `dns-test.sh`. The README's
  new History section does.
- The Settings page checks your edits with the server's own rules, the same ones a save uses. It
  used to keep a copy of them, which had drifted: for example, it treated `::ffff:1.1.1.1` and
  `1.1.1.1` as different servers. The estimate and the domain count (with duplicates and invalid
  names) update as you type; problems are still listed when you save, next to the field they are
  about.
- The worst-case run time in Settings now counts every try of every query, so it is no longer too
  low when `tries` is 2 or 3.
- Web API (breaking for scripts that use it): error `details` are `{path, code, message}` objects
  naming the field a problem is about, instead of plain strings, and never include a file's path.
  `GET /api/config`, `PUT /api/config` and `POST /api/config/reset` return
  `{"config", "errors", "estimate"}` instead of the bare config. `GET /api/defaults` is gone:
  `defaults` in `GET /api/schema` has the built-in defaults, without the System entry that a new or
  reset config adds from this computer's settings.
- `dns-bench` reports every problem with the config file as `<file>: <problem>`, for example
  `<file>: not valid JSON: ...`, `<file>: cannot read the file: Permission denied` or
  `<file>: must contain a JSON object`.
- Saved runs are analysed afresh every time they are loaded, so a single run, "All runs combined"
  and `dns-bench report` always use the same, current analysis. A single run used to show the
  summary and recommendation stored when it was measured, while "All runs combined" used the
  current code. For existing runs nothing changes yet: today's analysis is the one that stored
  them. A run's `.txt` report still shows the verdict of the day it was measured. The web UI's
  footer shows the DNS Bench version and the analysis version.
- New run files carry `"schema": 1` and `"kind": "run"`, and each result records whether the answer
  was truncated (`truncated`, also a new column in the CSV download). Files from earlier versions
  load as before. They didn't record truncation, so they show `false` (the CSV shows `False`).
- A recommendation's notes are `{"code", "params", "text"}` objects instead of strings (breaking for
  scripts that read them), in the web API, in `dns-bench run --json` and in new run files. `text` is
  the sentence that used to be the whole note.
- The text report states the score formula in the same words as the README.
- Web UI, keyboard and screen readers: sorting a table, picking a resolver, "Show all" and closing
  a domain's details keep the keyboard where it was. Clicking a tab, or following a link to another
  tab, moves it to the new view (the arrow keys still move along the tabs). A bar chart you can focus
  (Average latency, Failure rate, Median latency per domain) and the histogram are now a single Tab
  stop instead of one per bar, with the arrow keys moving between the bars. The page's main landmark
  is back (the tab panel used to replace it). In Settings, a field with a problem is marked invalid
  and linked to its message, and after a failed save the list of problems gets the focus, each
  problem leading to its field.
- Web UI: the Failed queries table shows at most 200 rows, says how many there are, and links to the
  CSV of every result (a run can have thousands of failures).
- Web UI: a request the server doesn't answer within 15 s gives up instead of hanging, and while a
  benchmark runs the page keeps checking on it (less and less often) after losing contact, then
  carries on when the server answers again. It used to give up after six tries.
- Web UI, light theme: chart axis labels and links have more contrast (WCAG AA). Reduced motion now
  also turns off button transitions and smooth scrolling. A resolver's dot in Settings has the
  resolver's own colour.
- Every enabled server is now measured at the same time. With more servers than the
  `max_parallel_servers` setting (**Servers in parallel** in Settings, 8 by default), the rest used
  to wait for a second batch, so they were timed at a different moment from the others and the
  results weren't comparable. 1.1.0's default config had 8 servers, so enabling Quad9 made 10; a new
  config has the 6 public servers plus your System servers. Results of runs with more servers than
  that setting can therefore differ from earlier versions, and such runs finish sooner. The setting
  is gone: it is ignored if your `config.json` still has it, and dropped the next time the config is
  saved. The load on each server is unchanged (one query at a time, at least
  `per_server_interval_ms` apart).
- `dns-bench serve` refuses a `--host` that other machines can reach (such as `0.0.0.0` or a LAN
  address) unless you add `--allow-remote`, because the web UI has no authentication. Other machines
  then have to address it by IP address, not by host name. The README shows how to reach the UI from
  another computer over an SSH tunnel instead.
- A config can list at most 20 resolvers, and one run can send at most 50,000 queries (enabled
  servers × domains × rounds; a new config sends a few hundred: 360, plus 60 for each System server).
  A config that goes over either limit is rejected with a message saying which limit and by how much.
  So is a run whose `--rounds`, **Rounds** box in the web UI, or `rounds` in `POST /api/run` would
  take it over (`POST /api/run` answers 400 with the detail code `too_many_queries`).

### Fixed

- A run file that is nested absurdly deep, or isn't shaped like a run, no longer crashes dns-bench
  or hides your other runs. `dns-bench list`, `report latest`, `report all` and the web UI skip it
  with a warning that gives the reason (for the web UI, in the terminal running `dns-bench serve`).
  Opening it by id says it is unreadable, and why. Such a file used to crash every view, or show up
  as an empty run and make `report all` and "All runs combined" say "No runs saved yet" even when
  other runs were fine.
- A benchmark started from the web UI that can't be saved (a full disk, or the runs folder became
  unwritable) is now written to the system's temp folder instead, as `dns-bench run` does, and the
  UI's error message says where. If that fails too, for example on the same full disk, the run is
  lost. Before sending any queries, the UI now also checks that the runs folder is writable
  (`POST /api/run` answers 500 with the detail code `runs_dir_unwritable` if it isn't).
- `dns-bench run` stopped with `kill` (SIGTERM) now saves the queries done so far, like Ctrl-C, instead
  of exiting without saving. A second Ctrl-C saves straight away instead of waiting for the
  queries still in flight, and a Ctrl-C while the run is being saved no longer interrupts the save.
- An internal error while a benchmark is running no longer throws away the queries already measured.
  The run stops, is saved with the status `partial` and the error, and its report says so.
  `dns-bench run` exits with 1 in that case.

### Security

- The web UI's API refuses a state-changing request whose `Origin` is not the UI's own, so another
  web page can't change your config or start and cancel runs. Requests without an `Origin` header,
  such as from `curl`, work as before.
- Responses carry `Cross-Origin-Resource-Policy` and `Cross-Origin-Opener-Policy`. The page's
  Content Security Policy no longer allows inline styles, and requires Trusted Types.
- The server drops a client that stalls for 15 seconds while sending a request. Malformed requests
  get a JSON error with the usual security headers instead of an HTML page, and an odd port in the
  `Host` header (such as `localhost:²`) is refused with 403 instead of causing a 500.

## [1.1.0] - 2026-09-27

### Added

- Releases are published on GitHub, with these notes. Each one is tested first on Linux and macOS
  with Python 3.13 and 3.14.

### Changed

- dns-bench needs Python 3.13 or newer (1.0.0 ran on 3.9). With an older `python3` the launcher
  stops with `dns-bench needs Python 3.13 or newer (found 3.9)`. macOS still ships 3.9 as
  `python3`, so run `python3.13 ./dns-bench ...` or `uv run --python 3.13 ./dns-bench ...` (see the
  README). Nothing else changes when upgrading: existing `config.json` and saved runs load as
  before.
- The web UI now needs Chrome or Edge 93, Firefox 92, or Safari 15.4 (iOS/iPadOS 15.4) or later. In
  older browsers the page doesn't load, or the Overview's recommendation card fails to draw.

### Fixed

- On Python 3.14, a config file or `PUT /api/config` body with a value nested tens of thousands of
  levels deep crashed dns-bench with a RecursionError: the command exited with a traceback and the
  API answered HTTP 500. A config file or API request body nested more than 32 levels deep is now
  rejected as malformed JSON on every supported Python: `dns-bench` reports
  `<path> is not valid JSON: nested more than 32 levels deep`, and the API answers HTTP 400
  `Malformed JSON`. A real config nests 4 levels, so valid configs are unaffected.

## [1.0.0] - 2026-09-27

First release: a faster, rate-limited DNS benchmark with a web UI, rewritten in Python from the
original zsh script (kept in `archive/`). It is the one-shot generated version, tagged as a fixed
baseline before the work in REMEDIATION_PLAN.md.

[Unreleased]: https://github.com/tpak/dns-bench/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/tpak/dns-bench/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/tpak/dns-bench/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/tpak/dns-bench/releases/tag/v1.0.0
