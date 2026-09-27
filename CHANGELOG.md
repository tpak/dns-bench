# Changelog

Notable changes to dns-bench, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Version numbers follow the policy in the
README's [Releasing](README.md#releasing) section. Each release's section becomes its GitHub
release notes.

## [Unreleased]

### Upgrading

- `config.json` is no longer part of the repository (see Changed). If you have changed your config
  (saving Settings in the web UI does), move it aside while you pull, or `git pull` stops with "Your
  local changes to the following files would be overwritten by merge: config.json":

  ```sh
  mv config.json config.json.mine && git pull && mv config.json.mine config.json
  ```

  If you never changed it, pulling deletes it, and the next run creates a new one. Your saved runs
  are not affected.

### Added

- `python3 -m dnsbench` runs dns-bench from the checkout, just like `./dns-bench`.
- An optional `dns-bench` command on your PATH: run `uv tool install --editable .` in the checkout
  (README, "Installing a `dns-bench` command"). `./dns-bench` still needs no install.
- A **System** resolver: the DNS servers your computer is set up to use (from `scutil --dns` on
  macOS, `/etc/resolv.conf` on Linux). A new config gets it automatically. After moving to another
  network, `dns-bench config --detect`, or **Add system resolvers** in Settings, updates it.
- `DNSBENCH_HOME=/some/dir` keeps `config.json` and `runs/` in that directory instead of the checkout.
  `dns-bench serve` prints where both are when it starts, and the Settings tab shows them.

### Changed

- `config.json` and `runs/` are created on first use and are git-ignored. A new config's defaults
  no longer include "ISP", whose two addresses only worked on the original author's network (anyone
  else saw it as unreachable); the System resolver takes its place. An existing `config.json` is
  not changed.
- Reading the config never creates or rewrites it. `dns-bench config` shows what a missing config
  would start with, without writing it. The file is created by the first saved run, by
  `dns-bench serve`, by saving or resetting Settings, or by `dns-bench config --reset` or `--detect`.
- A non-editable install, which has no checkout to keep data in, stops with a message asking you to
  set `DNSBENCH_HOME`.
- The Settings estimate no longer compares the run time with the old `dns-test.sh`. The README's
  new History section does.

- Every enabled server is now measured at the same time. With more than 8 servers (the default
  config has 8, so enabling Quad9 makes 10), the rest used to wait for a second batch, so they were timed at a different time from the others and the results
  weren't comparable. Results with more than 8 servers can therefore differ from earlier versions.
  Such runs also finish sooner. The `max_parallel_servers` setting is gone: it is ignored if your
  `config.json` still has it, and dropped the next time the config is saved. The load on each server
  is unchanged (one query at a time, at least `per_server_interval_ms` apart).
- `dns-bench serve` refuses a `--host` that other machines can reach (such as `0.0.0.0` or a LAN
  address) unless you add `--allow-remote`, because the web UI has no authentication. The README
  shows how to reach the UI from another computer over an SSH tunnel instead.
- A config can list at most 20 resolvers, and one run can send at most 50,000 queries (enabled
  servers × domains × rounds; the defaults send 480). A config or `--rounds` value that goes over
  either limit is rejected with a message saying which limit and by how much.

### Fixed

- A benchmark started from the web UI is no longer lost when it can't be saved (a full disk, or the
  runs folder became unwritable). As with `dns-bench run`, the full record is written to the
  system's temp folder instead, and the UI's error message says where. The UI also checks that the
  runs folder is writable before sending any queries.
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

[Unreleased]: https://github.com/tpak/dns-bench/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/tpak/dns-bench/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/tpak/dns-bench/releases/tag/v1.0.0
