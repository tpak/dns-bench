"""Persist runs: <runs_dir>/<id>.json (full record) + <id>.txt (text report).

Runs are never deleted or overwritten by the tool. A finished run is never
silently lost either: ``save_run_safely`` falls back to the system temp dir
when the runs dir can't take it.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from . import recommend as recommend_mod
from . import report, stats

RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z(-[0-9]+)?$")

_cache_lock = threading.Lock()
_list_cache: dict[str, tuple[float, int, dict]] = {}
_warned: set[str] = set()


class StorageError(Exception):
    """A saved run exists but cannot be read."""


def valid_run_id(run_id) -> bool:
    return isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id) is not None


def _id_sort_key(run_id: str):
    base, _, suffix = run_id.partition("-")
    return (base, int(suffix) if suffix.isdigit() else 1)


def finalize_run(run: dict) -> dict:
    """Add ``summary`` and ``recommendation`` to a run record (in place)."""
    run["summary"] = stats.summarize_run(run)
    run["recommendation"] = recommend_mod.recommend(run["summary"], (run.get("config") or {}).get("settings"))
    return run


def _write_tmp(directory: Path, text: str) -> str:
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=str(directory))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    return tmp


def _link_new(tmp: str, dest: Path) -> None:
    """Atomically publish ``tmp`` at ``dest``; FileExistsError if dest exists."""
    try:
        os.link(tmp, dest)  # atomic, never overwrites
    except FileExistsError:
        raise
    except OSError:
        # Filesystem without hard links: best effort (tiny race window).
        if dest.exists():
            raise FileExistsError(str(dest)) from None
        os.replace(tmp, dest)


def save_run(run: dict, runs_dir) -> Path:
    """Finalize and save ``run``; returns the path of the .json file.

    If ``<id>.json`` already exists the id gets a -2, -3, ... suffix
    (``run["id"]`` is updated in place). Existing files are never overwritten.
    """
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    finalize_run(run)
    base = str(run.get("id") or "")
    if not valid_run_id(base):
        raise ValueError(f"invalid run id {base!r}")
    base = base.split("-")[0]
    n = 1
    while True:
        candidate = base if n == 1 else f"{base}-{n}"
        json_path = runs_dir / f"{candidate}.json"
        txt_path = runs_dir / f"{candidate}.txt"
        if json_path.exists() or txt_path.exists():
            n += 1
            continue
        run["id"] = candidate
        tmp = _write_tmp(runs_dir, json.dumps(run, indent=2, ensure_ascii=False) + "\n")
        try:
            _link_new(tmp, json_path)
        except FileExistsError:
            n += 1
            continue
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        break
    tmp = _write_tmp(runs_dir, report.render_text(run))
    try:
        os.replace(tmp, txt_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return json_path


def check_writable(runs_dir) -> str | None:
    """Why a run could not be saved in ``runs_dir``, or None if it can. Checked before any DNS traffic."""
    runs_dir = Path(runs_dir)
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=str(runs_dir))
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        return f"cannot write to {runs_dir}: {exc.strerror or exc}"
    return None


def rescue_run(run: dict) -> Path | None:
    """Last resort when the runs dir fails mid-save: write the record to the system temp dir.

    Returns the file's path, or None if that fails too.
    """
    try:
        fd, path = tempfile.mkstemp(prefix=f"dns-bench-{run.get('id', 'run')}-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(run, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        return Path(path)
    except OSError:
        return None


@dataclass
class SaveResult:
    """What ``save_run_safely`` did with a run."""

    path: Path | None  # the saved <id>.json; None if it could not be written
    error: str | None = None  # what went wrong, worded for the user; None if everything was saved
    rescued: Path | None = None  # where the record went instead, when <id>.json could not be written


def save_run_safely(run: dict, runs_dir) -> SaveResult:
    """``save_run`` that never loses a finished run to a disk problem (full disk, permissions changed).

    The run always ends up finalized. If its ``.json`` could not be written, the record is rescued to
    the system temp dir. A ``.json`` saved without its ``.txt`` report counts as saved, with an error.
    """
    try:
        return SaveResult(save_run(run, runs_dir))
    except OSError as exc:
        why = exc.strerror or str(exc)
        if "summary" not in run or "recommendation" not in run:
            finalize_run(run)
        json_path = Path(runs_dir) / f"{run.get('id')}.json"
        if valid_run_id(run.get("id")) and json_path.is_file():
            error = f"the run was saved as {json_path}, but its text report could not be written: {why}"
            return SaveResult(json_path, error)
        return SaveResult(None, f"could not save run to {runs_dir}: {why}", rescue_run(run))


def _warn(path: Path, msg: str) -> None:
    key = f"{path}:{msg}"
    if key not in _warned:
        _warned.add(key)
        print(f"dns-bench: warning: skipping {path.name}: {msg}", file=sys.stderr)


def _list_row(run: dict) -> dict:
    rec = run.get("recommendation") or {}
    ranking = rec.get("ranking") or []
    results = run.get("results") or []
    cfg = run.get("config") or {}
    summary = run.get("summary") or {}
    resolvers = summary.get("resolvers")
    if resolvers is None:
        resolvers = list(dict.fromkeys(r.get("resolver") for r in results))
    domains = summary.get("domains")
    n_domains = len(domains) if domains is not None else len({r.get("domain") for r in results})
    best = next((e for e in ranking if e.get("resolver") == rec.get("best")), None)
    by_res = summary.get("by_resolver") or {}
    return {
        "id": run.get("id"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "duration_s": run.get("duration_s"),
        "status": run.get("status"),
        "host": run.get("host"),
        "n_queries": len(results),
        "n_domains": n_domains if results else len(cfg.get("domains") or []),
        "resolvers": resolvers,
        "best": rec.get("best"),
        "best_median": best.get("median") if best else None,
        # per-resolver medians, so the UI's trend chart needs no full run records
        "medians": {name: (by_res.get(name) or {}).get("median") for name in resolvers or []},
    }


def list_runs(runs_dir) -> list[dict]:
    """Summary rows for all saved runs, newest first. Corrupt files are skipped."""
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    rows = []
    live = set()
    for path in runs_dir.glob("*.json"):
        run_id = path.stem
        if not valid_run_id(run_id):
            continue
        key = str(path)
        live.add(key)
        try:
            st = path.stat()
        except OSError:
            continue
        with _cache_lock:
            cached = _list_cache.get(key)
        if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
            rows.append(cached[2])
            continue
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(run, dict) or not isinstance(run.get("results", []), list):
                raise ValueError("not a run record")
            if "summary" not in run or "recommendation" not in run:
                finalize_run(run)
            run.setdefault("id", run_id)
            row = _list_row(run)
            row["id"] = run_id
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            _warn(path, f"{type(exc).__name__}: {exc}")
            continue
        with _cache_lock:
            _list_cache[key] = (st.st_mtime, st.st_size, row)
        rows.append(row)
    with _cache_lock:
        for key in [k for k in _list_cache if k.startswith(str(runs_dir)) and k not in live]:
            _list_cache.pop(key, None)
    rows.sort(key=lambda r: _id_sort_key(r["id"]), reverse=True)
    return rows


def load_run(run_id: str, runs_dir) -> dict:
    """Load one run. ValueError for a malformed id, KeyError if it doesn't exist,
    StorageError if the file is unreadable."""
    if not valid_run_id(run_id):
        raise ValueError(f"invalid run id {run_id!r}")
    path = Path(runs_dir) / f"{run_id}.json"
    if not path.is_file():
        raise KeyError(run_id)
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StorageError(f"run {run_id} is unreadable: {exc}") from exc
    if not isinstance(run, dict):
        raise StorageError(f"run {run_id} is not a run record")
    run["id"] = run_id
    if "summary" not in run or "recommendation" not in run:
        try:
            finalize_run(run)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise StorageError(f"run {run_id} is malformed: {exc}") from exc
    return run


def latest_run_id(runs_dir) -> str | None:
    rows = list_runs(runs_dir)
    return rows[0]["id"] if rows else None


def coverage(runs: list[dict]) -> dict:
    """{resolver: {"runs": k, "of": N, "last_run": id}} over ``runs`` (newest first):
    how many of the combined runs measured each resolver, and the newest one that did."""
    out: dict[str, dict] = {}
    for run in runs:
        names = dict.fromkeys(r.get("resolver") for r in run.get("results") or [] if isinstance(r, dict))
        for name in names:
            if name is None:
                continue
            c = out.setdefault(name, {"runs": 0, "of": len(runs), "last_run": run.get("id")})
            c["runs"] += 1
    return out


def aggregate(runs_dir, run_ids="all", current=None) -> dict:
    """Combine several runs into one {run_ids, summary, recommendation, config,
    coverage} bundle.

    ``run_ids`` is "all" or a list of ids. The NEWEST selected run's config
    supplies the settings and display order. ``current`` is the names of the
    resolvers enabled in the live config (None: use the newest run's config).
    Only those, and resolvers the newest run measured, can be recommended: a
    resolver seen only in older runs (since disabled, removed or renamed) is
    still ranked but not suggested. KeyError if nothing to aggregate,
    ValueError for malformed ids.
    """
    if run_ids == "all" or run_ids is None:
        ids = [r["id"] for r in list_runs(runs_dir)]
    else:
        ids = list(dict.fromkeys(run_ids))
        for rid in ids:
            if not valid_run_id(rid):
                raise ValueError(f"invalid run id {rid!r}")
        ids.sort(key=_id_sort_key, reverse=True)
    if not ids:
        raise KeyError("no runs")
    runs = []
    for rid in ids:
        try:
            runs.append(load_run(rid, runs_dir))
        except StorageError:
            if run_ids == "all" or run_ids is None:
                continue  # tolerate a bad file in "all"
            raise
    if not runs:
        raise KeyError("no readable runs")
    newest = runs[0]
    config = newest.get("config") or {}
    summary = stats.summarize(stats.merge_runs(runs), **stats.orders_from_config(config))
    cov = coverage(runs)
    if current is None:
        current = [
            r.get("name")
            for r in config.get("resolvers") or []
            if isinstance(r, dict) and r.get("enabled", True) is not False
        ]
    eligible = set(current) | {name for name, c in cov.items() if c["last_run"] == newest["id"]}
    rec = recommend_mod.recommend(
        summary, config.get("settings"), n_runs=len(runs), coverage=cov, current=eligible
    )
    return {
        "run_ids": [r["id"] for r in runs],
        "summary": summary,
        "recommendation": rec,
        "config": config,
        "coverage": cov,
    }
