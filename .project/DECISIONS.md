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
