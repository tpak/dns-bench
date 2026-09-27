"""Saved runs: <runs_dir>/<id>.json (the record) and <id>.txt (the text report written with it).

``RunRepository`` only reads and writes files. What a run means (its summary and recommendation) is
analysis.py's business: it recomputes them from the raw results whenever a run is loaded, so what a
file stored is never trusted. This module imports nothing that analyses runs.

Every record read from disk goes through ``migrate``, which checks its shape and brings older formats
up to the current one (1.x files had no ``schema``), so the rest of the code sees one format. A file
that can't be used raises ``CorruptRun``, never a bare KeyError or ValueError.

Runs are never deleted or overwritten by the tool. A finished run is never silently lost either:
``rescue_run`` writes it to the system temp dir when the runs dir can't take it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, cast

from . import config as config_mod
from .models import RUN_SCHEMA, RunRecord

RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z(-[0-9]+)?$")
# Keys a run file may hold that are derived from the rest, and so are dropped on load.
DERIVED_KEYS = ("summary", "recommendation", "analysis_version")
_ROW_TEXT_KEYS = ("resolver", "server", "domain", "status")


class StorageError(Exception):
    """A saved run can't be used. Messages never contain file paths, so the web API can show them."""


class RunNotFound(StorageError):
    def __init__(self, run_id: str):
        super().__init__(f"run {run_id} not found")
        self.run_id = run_id


class NoRuns(StorageError):
    def __init__(self) -> None:
        super().__init__("no runs saved yet")


class CorruptRun(StorageError):
    def __init__(self, run_id: str, reason: str):
        super().__init__(f"run {run_id} is unreadable: {reason}")
        self.run_id = run_id
        self.reason = reason


def valid_run_id(run_id: object) -> bool:
    return isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id) is not None


def id_sort_key(run_id: str) -> tuple[str, int]:
    """Sorts ids by start time, then by the -2, -3 suffix of runs that started in the same second."""
    base, _, suffix = run_id.partition("-")
    return (base, int(suffix) if suffix.isdigit() else 1)


def migrate(raw: object, run_id: str) -> RunRecord:
    """A run file's content as a current ``RunRecord``, or CorruptRun if it can't be one.

    Format 1 is what 1.x wrote, minus the ``schema`` key: a missing ``schema`` means 1. Rows get the
    fields later versions added (``attempts``: 1, ``truncated``: false). Derived keys (summary,
    recommendation) are dropped: analysis.py recomputes them. ``id`` is the file's name, whatever the
    record says.
    """
    if not isinstance(raw, dict):
        raise CorruptRun(run_id, "not a run record")
    schema = raw.get("schema", 1)
    if schema != RUN_SCHEMA:
        raise CorruptRun(run_id, f"unknown run file format {schema!r} (from a newer dns-bench?)")
    results = raw.get("results", [])
    if not isinstance(results, list):
        raise CorruptRun(run_id, "results is not a list")
    rows = []
    for i, row in enumerate(results):
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in _ROW_TEXT_KEYS):
            raise CorruptRun(run_id, f"result #{i + 1} is not a query result")
        ms = row.get("ms")
        if ms is not None and (isinstance(ms, bool) or not isinstance(ms, (int, float))):
            raise CorruptRun(run_id, f"result #{i + 1} has a latency that isn't a number")
        rows.append({"attempts": 1, "truncated": False, **row})
    config = raw.get("config", {})
    if not isinstance(config, dict):
        raise CorruptRun(run_id, "config is not an object")
    for key, kind in (("resolvers", list), ("domains", list), ("settings", dict)):
        if key in config and not isinstance(config[key], kind):
            raise CorruptRun(run_id, f"config.{key} is not {'a list' if kind is list else 'an object'}")
    record: dict[str, Any] = {k: v for k, v in raw.items() if k not in DERIVED_KEYS}
    record.update(
        schema=RUN_SCHEMA,
        kind="run",
        id=run_id,
        results=rows,
        config=config,
    )
    return cast(RunRecord, record)


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


class RunRepository:
    """The run files in one directory."""

    def __init__(self, runs_dir: str | os.PathLike[str]):
        self.dir = Path(runs_dir)

    def path(self, run_id: str) -> Path:
        """The ``.json`` file of ``run_id`` (ValueError for a malformed id, which could name any file)."""
        if not valid_run_id(run_id):
            raise ValueError(f"invalid run id {run_id!r}")
        return self.dir / f"{run_id}.json"

    def exists(self, run_id: str) -> bool:
        return valid_run_id(run_id) and self.path(run_id).is_file()

    def ids(self) -> list[str]:
        """Every saved run's id, newest first."""
        if not self.dir.is_dir():
            return []
        ids = [p.stem for p in self.dir.glob("*.json") if valid_run_id(p.stem)]
        return sorted(ids, key=id_sort_key, reverse=True)

    def stamp(self, run_id: str) -> tuple[int, int] | None:
        """(mtime_ns, size) of a run's file, for cache keys; None if it is gone."""
        try:
            st = self.path(run_id).stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def load(self, run_id: str) -> RunRecord:
        """The run, migrated to the current format. RunNotFound or CorruptRun if it can't be had."""
        path = self.path(run_id)
        if not path.is_file():
            raise RunNotFound(run_id)
        try:
            raw = config_mod.loads_json(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CorruptRun(run_id, exc.strerror or type(exc).__name__) from exc
        except ValueError as exc:  # bad JSON, bad UTF-8, absurd nesting: the same on every Python
            raise CorruptRun(run_id, f"not valid JSON ({str(exc)[:200]})") from exc
        return migrate(raw, run_id)

    def save(self, run: RunRecord, report_text: str) -> Path:
        """Write ``run`` and its report; returns the path of the .json file.

        If ``<id>.json`` already exists the id gets a -2, -3, ... suffix (``run["id"]`` is updated in
        place). Existing files are never overwritten. OSError if the files can't be written.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        base = str(run.get("id") or "")
        if not valid_run_id(base):
            raise ValueError(f"invalid run id {base!r}")
        base = base.split("-")[0]
        n = 1
        while True:
            candidate = base if n == 1 else f"{base}-{n}"
            json_path = self.dir / f"{candidate}.json"
            txt_path = self.dir / f"{candidate}.txt"
            if json_path.exists() or txt_path.exists():
                n += 1
                continue
            run["id"] = candidate
            tmp = _write_tmp(self.dir, json.dumps(run, indent=2, ensure_ascii=False) + "\n")
            try:
                _link_new(tmp, json_path)
            except FileExistsError:
                n += 1
                continue
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            break
        tmp = _write_tmp(self.dir, report_text)
        try:
            os.replace(tmp, txt_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return json_path

    def check_writable(self) -> str | None:
        """Why a run could not be saved here, or None if it can. Checked before any DNS traffic."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd, probe = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=str(self.dir))
            os.close(fd)
            os.unlink(probe)
        except OSError as exc:
            return exc.strerror or type(exc).__name__
        return None


def rescue_run(run: RunRecord) -> Path | None:
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
