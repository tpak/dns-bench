# Changelog

Notable changes to dns-bench, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Version numbers follow the policy in the
README's [Releasing](README.md#releasing) section. Each release's section becomes its GitHub
release notes.

## [Unreleased]

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
