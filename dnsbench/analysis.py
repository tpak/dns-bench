"""What runs mean: summaries and recommendations, recomputed from the raw results.

A run's summary and recommendation are derived from its results and its config snapshot, so they are
recomputed whenever a run is loaded. The single-run view and "All runs combined" therefore always use
the same, current analysis. Whatever an older version stored in a file (and wrote in its ``.txt``
report) stays there as a snapshot of that version's verdict, but is never read back.

``ANALYSIS_VERSION`` goes up whenever stats.py or recommend.py change what they conclude from the same
results. It is saved with each run, shown in the web UI, and part of every cache key.

Recomputing takes about 4 ms a run. ``Analysis`` caches each run's summary and recommendation by the
file's modification time and size, so listing the runs doesn't recompute unchanged ones. The cache
belongs to the ``Analysis`` object (one per server, one per CLI command), not to the process.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Literal, cast

from . import recommend as recommend_mod
from . import stats
from .models import Aggregate, Coverage, Recommendation, RunRecord, Summary
from .storage import CorruptRun, NoRuns, RunNotFound, RunRepository, id_sort_key, valid_run_id

# 2 (Phase 8): local errors not charged, failure and retry rates counted only when significant,
# ties from confidence intervals, tail figures from first answers only, retried = answered on a retry.
ANALYSIS_VERSION = 2

log = logging.getLogger(__name__)


def finalize(run: RunRecord) -> RunRecord:
    """Add ``summary``, ``recommendation`` and ``analysis_version`` to a run record (in place)."""
    run["summary"] = stats.summarize_run(run)
    run["recommendation"] = recommend_mod.recommend(run["summary"], (run.get("config") or {}).get("settings"))
    run["analysis_version"] = ANALYSIS_VERSION
    return run


def coverage(runs: list[RunRecord]) -> dict[str, Coverage]:
    """{resolver: {"runs": k, "of": N, "last_run": id}} over ``runs`` (newest first):
    how many of the combined runs measured each resolver, and the newest one that did."""
    out: dict[str, Coverage] = {}
    for run in runs:
        names = dict.fromkeys(r.get("resolver") for r in run.get("results") or [] if isinstance(r, dict))
        for name in names:
            if name is None:
                continue
            c = out.setdefault(name, {"runs": 0, "of": len(runs), "last_run": run.get("id")})
            c["runs"] += 1
    return out


def list_row(run: RunRecord) -> dict[str, Any]:
    """One line of the run list: what the History tab and `dns-bench list` show. ``run`` is finalized."""
    rec = run["recommendation"]
    summary = run["summary"]
    best = next((e for e in rec["ranking"] if e["resolver"] == rec["best"]), None)
    by_res = summary["by_resolver"]
    return {
        "id": run["id"],
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "duration_s": run.get("duration_s"),
        "status": run.get("status"),
        "host": run.get("host"),
        "n_queries": len(run["results"]),
        "n_domains": len(summary["domains"])
        if run["results"]
        else len((run.get("config") or {}).get("domains") or []),
        "resolvers": summary["resolvers"],
        "best": rec["best"],
        "best_median": best["median"] if best else None,
        # per-resolver medians, so the UI's trend chart needs no full run records
        "medians": {name: by_res[name]["median"] for name in summary["resolvers"]},
    }


class Analysis:
    """Analysed runs from one RunRepository, with a per-run cache. Safe to share between threads."""

    def __init__(self, repo: RunRepository):
        self.repo = repo
        self._lock = threading.Lock()  # guards _cache and _warned
        # run id -> (file stamp, summary, recommendation, list row)
        self._cache: dict[str, tuple[tuple[int, int, int], Summary, Recommendation, dict[str, Any]]] = {}
        self._warned: set[tuple[str, tuple[int, int] | None]] = set()

    def load(self, run_id: str) -> RunRecord:
        """A run with its summary and recommendation. RunNotFound or CorruptRun if it can't be had."""
        stamp = self.repo.stamp(run_id)
        run = self.repo.load(run_id)
        key = (*stamp, ANALYSIS_VERSION) if stamp else None
        with self._lock:
            hit = self._cache.get(run_id)
        if key is not None and hit is not None and hit[0] == key:
            run["summary"], run["recommendation"] = hit[1], hit[2]
            run["analysis_version"] = ANALYSIS_VERSION
            return run
        try:
            finalize(run)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:  # migrate() let odd data through
            raise CorruptRun(run_id, f"{type(exc).__name__}: {exc}") from exc
        if key is not None:
            with self._lock:
                self._cache[run_id] = (key, run["summary"], run["recommendation"], list_row(run))
        return run

    def _row(self, run_id: str) -> dict[str, Any] | None:
        """The list row of a run, or None (and a warning, once per file version) if it can't be read."""
        stamp = self.repo.stamp(run_id)
        with self._lock:
            hit = self._cache.get(run_id)
        if stamp is not None and hit is not None and hit[0] == (*stamp, ANALYSIS_VERSION):
            return hit[3]
        try:
            run = self.load(run_id)
        except CorruptRun as exc:
            with self._lock:
                first = (run_id, stamp) not in self._warned
                self._warned.add((run_id, stamp))
            if first:
                log.warning("dns-bench: warning: skipping run %s: %s", run_id, exc.reason)
            return None
        except RunNotFound:  # deleted since the directory was listed
            return None
        return list_row(run)

    def list_runs(self) -> list[dict[str, Any]]:
        """Summary rows for all saved runs, newest first. Unreadable files are skipped with a warning."""
        ids = self.repo.ids()
        rows = [row for row in map(self._row, ids) if row is not None]
        with self._lock:
            for gone in set(self._cache) - set(ids):
                del self._cache[gone]
        return rows

    def latest_id(self) -> str | None:
        rows = self.list_runs()
        return rows[0]["id"] if rows else None

    def aggregate(
        self, run_ids: list[str] | Literal["all"] = "all", current: list[str] | None = None
    ) -> Aggregate:
        """Several runs combined into one bundle (see models.Aggregate).

        ``run_ids`` is "all" or a list of ids. The NEWEST selected run's config supplies the settings
        and display order. ``current`` is the names of the resolvers enabled in the live config (None:
        use the newest run's config). Only those, and resolvers the newest run measured, can be
        recommended: a resolver seen only in older runs (since disabled, removed or renamed) is still
        ranked but not suggested. NoRuns if there is nothing to combine; with "all", unreadable files
        are skipped, with a list they raise CorruptRun (or RunNotFound).
        """
        everything = run_ids == "all"
        if everything:
            ids = [row["id"] for row in self.list_runs()]
        else:
            ids = list(dict.fromkeys(run_ids))
            for rid in ids:
                if not valid_run_id(rid):
                    raise ValueError(f"invalid run id {rid!r}")
            ids.sort(key=id_sort_key, reverse=True)
        runs: list[RunRecord] = []
        for rid in ids:
            try:
                runs.append(self.repo.load(rid))  # raw rows only: the combined summary is computed below
            except CorruptRun:
                if everything:
                    continue  # tolerate a bad file in "all"
                raise
        if not runs:
            raise NoRuns
        newest = runs[0]
        config = newest.get("config") or {}
        try:
            return self._combine(runs, config, current)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:  # migrate() let odd data through
            raise CorruptRun(newest["id"], f"{type(exc).__name__}: {exc}") from exc

    def _combine(self, runs: list[RunRecord], config: dict[str, Any], current: list[str] | None) -> Aggregate:
        newest = runs[0]
        summary = stats.summarize(stats.merge_runs(runs), **stats.orders_from_config(config))
        cov = coverage(runs)
        if current is None:
            current = [
                r["name"]
                for r in config.get("resolvers") or []
                if isinstance(r, dict)
                and isinstance(r.get("name"), str)
                and r.get("enabled", True) is not False
            ]
        eligible = set(current) | {name for name, c in cov.items() if c["last_run"] == newest["id"]}
        rec = recommend_mod.recommend(
            summary, config.get("settings"), n_runs=len(runs), coverage=cov, current=eligible
        )
        return cast(
            Aggregate,
            {
                "kind": "aggregate",
                "run_ids": [r["id"] for r in runs],
                "summary": summary,
                "recommendation": rec,
                "config": config,
                "coverage": cov,
                "analysis_version": ANALYSIS_VERSION,
            },
        )
