# Decisions

Append-only. One line per decision: `YYYY-MM-DD: [Topic] [Decision] — [Rationale].` Never edit a past entry; to reverse one, append a new entry that references it.

2026-09-27: [Release] Tagged the one-shot generated version as v1.0.0 — a fixed baseline before the remediation work in REMEDIATION_PLAN.md starts changing things.
2026-09-27: [Python] Raised the minimum Python from 3.9 to 3.13 — uv can supply any interpreter, 3.9 is past end-of-life, and the code needs nothing older; 3.13 is the owner's default interpreter and gets security fixes until October 2029.
2026-09-27: [Tooling] uv manages Python interpreters, environments and tools; pyenv, pip and pipx are not used — one tool, the same one the owner uses on other machines.
2026-09-27: [Dependencies] The runtime stays standard-library-first, but third-party packages are allowed when justified — the README's "no pip install" promise is no longer a hard constraint.
2026-09-27: [Plan] Lint/format (Phase 0) and CI (Phase 1) come before the remediation phases — every later change is then linted locally and tested in CI from its first commit.
