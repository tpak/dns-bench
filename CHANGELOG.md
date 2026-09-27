# Changelog

Notable changes to dns-bench, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Version numbers follow the policy in the
README's [Releasing](README.md#releasing) section. Each release's section becomes its GitHub
release notes.

## [Unreleased]

### Changed

- dns-bench needs Python 3.13 or newer (it ran on 3.9 before). The launcher says so when started
  with an older `python3`, and the README lists workarounds.

### Fixed

- On Python 3.14, a config file or API request with a value nested hundreds of levels deep crashed
  validation (the API answered HTTP 500). Anything nested more than 32 levels deep is now rejected
  as malformed JSON on every Python version.

### Added

- Every push and pull request is linted and tested on GitHub Actions, on Linux and macOS with
  Python 3.13 and 3.14. Pushing a version tag publishes the GitHub release.

## [1.0.0] - 2026-09-27

First release: a faster, rate-limited DNS benchmark with a web UI, rewritten in Python from the
original zsh script (kept in `archive/`). It is the one-shot generated version, tagged as a fixed
baseline before the work in REMEDIATION_PLAN.md.

[Unreleased]: https://github.com/tpak/dns-bench/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/tpak/dns-bench/releases/tag/v1.0.0
