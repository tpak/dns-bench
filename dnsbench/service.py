"""Running a benchmark: one path for both front ends, the CLI (`dns-bench run`) and the web UI.

``BenchmarkService`` does it in three steps:

* ``prepare`` loads the config (creating it on first use), applies this run's overrides, validates
  the result, checks that the run can be saved before any DNS traffic, and builds the per-server
  jobs. Jobs are built once, so the total the UI shows is the total the runner measures.
* ``execute`` measures (runner.run_benchmark).
* ``persist`` analyses the run, writes it and its report, and, if the runs dir fails, tries to rescue
  it to the system temp dir (which can fail too, for example on the same full disk).

``JobManager`` runs one benchmark at a time in the background for the web server. It holds its lock
only to read or change the job's state, never across disk IO (ARCH-M5).

The front ends only translate: arguments or a request body into ``Overrides``, exceptions into exit
codes or HTTP statuses, progress into a terminal line or ``status()``.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import analysis, report, runner, storage
from . import config as config_mod
from .models import ProgressEvent, ProgressFn, QueryFn, QueryRow, RunRecord

RECENT_MAX = 20  # recent results kept for the web UI's live view

# Average and worst-case slack the runner adds to the interval (runner.JITTER is up to 10 %).
_AVG_JITTER, _MAX_JITTER = 1 + runner.JITTER / 2, 1 + runner.JITTER


def estimate(cfg: object, rounds: int | None = None) -> dict[str, Any]:
    """Rough cost of a run of ``cfg`` (with ``rounds`` instead of its own, if given). Never raises.

    Works on any config, even an invalid draft: a setting that isn't a usable number counts as its
    default, so the Settings page can show an estimate while the user is still typing.
    ``est_seconds`` assumes every query is answered at once; ``worst_seconds`` assumes every attempt
    times out.
    """
    norm = config_mod.normalize_config(cfg) if isinstance(cfg, dict) else {}
    settings = norm.get("settings")
    raw = settings if isinstance(settings, dict) else {}

    def setting(key: str) -> int:
        # Clamped to the bounds: the estimate follows the typing smoothly, and a hand-edited 10**400
        # can't overflow the float arithmetic below.
        v = raw.get(key)
        lo, hi = config_mod.SETTING_BOUNDS[key]
        default = config_mod.DEFAULT_SETTINGS[key]
        assert isinstance(default, int)  # only called for the whole-number settings
        return min(max(v, lo), hi) if type(v) is int and v > 0 else default

    resolvers = norm.get("resolvers")
    enabled = [
        r
        for r in (resolvers if isinstance(resolvers, list) else [])
        if isinstance(r, dict) and r.get("enabled", True) is not False and isinstance(r.get("servers"), list)
    ]
    servers = sum(len(r["servers"]) for r in enabled)
    domains = norm.get("domains")
    n_domains = len(domains) if isinstance(domains, list) else 0
    n_rounds = rounds if rounds is not None else setting("rounds")
    per_server = n_domains * n_rounds
    interval_s = (
        max(config_mod.MIN_INTERVAL_MS, setting("per_server_interval_ms")) / 1000.0
    )  # the runner's floor
    timeout_s = setting("timeout_ms") / 1000.0
    per_server_qps = 1.0 / interval_s
    return {
        "resolvers": len(enabled),
        "servers": servers,
        "domains": n_domains,
        "rounds": n_rounds,
        "queries": servers * per_server,
        "queries_per_server": per_server,
        # every server is measured at the same time, each at its own pace
        "est_seconds": round(per_server * interval_s * _AVG_JITTER, 1) if servers else 0.0,
        # each attempt waits for its slot and then, at worst, for the whole timeout
        "worst_seconds": round(per_server * setting("tries") * max(interval_s * _MAX_JITTER, timeout_s), 1)
        if servers
        else 0.0,
        "max_qps_per_server": round(per_server_qps, 2),
        "max_qps_total": round(per_server_qps * servers, 2),
    }


# --------------------------------------------------------------------------- #
# Preparing a run
# --------------------------------------------------------------------------- #


@dataclass
class Overrides:
    """Changes for one run only; the config file is not touched."""

    rounds: int | None = None
    interval_ms: int | None = None
    timeout_ms: int | None = None
    resolvers: list[str] | None = None  # measure only these (by name, any case), enabled or not


class InvalidRun(config_mod.ConfigError):
    """The overrides make an otherwise valid config unusable: an unknown resolver name, or a run over
    the query limit. The config file itself is fine, so the CLI treats it as a usage error."""


class RunsDirUnwritable(Exception):
    def __init__(self, runs_dir: Path, reason: str):
        super().__init__(f"cannot write to {runs_dir}: {reason}")
        self.runs_dir = runs_dir
        self.reason = reason


@dataclass
class RunPlan:
    config: dict[str, Any]  # normalised and validated, overrides applied
    jobs: list[dict[str, Any]]  # runner.build_jobs(config)
    estimate: dict[str, Any]
    save: bool
    created: config_mod.SystemResolver | None = None  # set when prepare() created the config file

    @property
    def total(self) -> int:
        return sum(len(j["items"]) for j in self.jobs)


def apply_overrides(cfg: dict[str, Any], overrides: Overrides) -> list[config_mod.ValidationError]:
    """Apply ``overrides`` to a loaded config (in place); returns the problems, [] if none."""
    s = cfg["settings"]
    for key, value in (
        ("rounds", overrides.rounds),
        ("per_server_interval_ms", overrides.interval_ms),
        ("timeout_ms", overrides.timeout_ms),
    ):
        if value is not None:
            s[key] = value
    if overrides.resolvers:
        wanted = [w.strip() for w in overrides.resolvers if w.strip()]
        by_name = {r["name"].casefold(): r for r in cfg["resolvers"]}
        unknown = [w for w in wanted if w.casefold() not in by_name]
        if unknown:
            names = ", ".join(r["name"] for r in cfg["resolvers"])
            message = f"unknown resolver(s): {', '.join(unknown)} (configured: {names})"
            return [config_mod.ValidationError("resolvers", "invalid", message)]
        keep = {w.casefold() for w in wanted}
        for r in cfg["resolvers"]:
            r["enabled"] = r["name"].casefold() in keep
    return config_mod.validate_config(cfg)


@dataclass
class SaveResult:
    """What ``BenchmarkService.persist`` did with a run."""

    path: Path | None  # the saved <id>.json; None if it could not be written
    error: str | None = None  # what went wrong, worded for the user; None if everything was saved
    rescued: Path | None = None  # where the record went instead, when <id>.json could not be written


class BenchmarkService:
    """Prepare, measure and save runs with one config file and one runs dir.

    ``query_fn`` and ``detect_fn`` are seams for tests (a fake resolver, a fake system-resolver
    lookup). ``config_lock`` serialises every read and write of the config file in this process.
    """

    def __init__(
        self,
        config_path: str | Path,
        runs_dir: str | Path,
        *,
        query_fn: QueryFn | None = None,
        detect_fn: config_mod.Detect | None = None,
    ):
        self.config_path = Path(config_path)
        self.repo = storage.RunRepository(runs_dir)
        self.analysis = analysis.Analysis(self.repo)
        self.query_fn = query_fn
        self.detect_fn = detect_fn
        self.config_lock = threading.Lock()  # guards the config file

    @property
    def runs_dir(self) -> Path:
        return self.repo.dir

    def prepare(self, overrides: Overrides | None = None, *, save: bool = True) -> RunPlan:
        """Everything that can go wrong before the first query. Raises ConfigError (the config file:
        ConfigWriteError if it can't be created), InvalidRun (the overrides) or RunsDirUnwritable.

        With ``save`` a missing config is created (so every run of a new config uses the same System
        entry) and the runs dir must be writable; without it, nothing is written.
        """
        created = None
        with self.config_lock:
            if save:
                created = config_mod.ensure_config(self.config_path, self.detect_fn)
            cfg = config_mod.load_config(self.config_path, strict=True, detect=self.detect_fn)
        errors = apply_overrides(cfg, overrides or Overrides())
        if errors:
            raise InvalidRun(errors)
        if save:
            problem = self.repo.check_writable()
            if problem:
                raise RunsDirUnwritable(self.repo.dir, problem)
        cfg = config_mod.normalize_config(cfg)
        return RunPlan(cfg, runner.build_jobs(cfg), estimate(cfg), save, created)

    def execute(
        self,
        plan: RunPlan,
        *,
        progress: ProgressFn | None = None,
        cancel_event: threading.Event | None = None,
        stop_waiting: threading.Event | None = None,
    ) -> RunRecord:
        return runner.run_benchmark(
            plan.config,
            jobs=plan.jobs,
            query_fn=self.query_fn,
            progress=progress,
            cancel_event=cancel_event,
            stop_waiting=stop_waiting,
        )

    def persist(self, run: RunRecord) -> SaveResult:
        """Analyse and save ``run``; never loses it to a disk problem (full disk, permissions changed).

        The run always ends up analysed. If its ``.json`` could not be written, the record is rescued
        to the system temp dir. A ``.json`` saved without its ``.txt`` report counts as saved, with an
        error.
        """
        analysis.finalize(run)
        try:
            return SaveResult(self.repo.save(run, report.render_text(run)))
        except OSError as exc:
            why = exc.strerror or str(exc)
            if self.repo.exists(run.get("id", "")):
                json_path = self.repo.path(run["id"])
                error = f"the run was saved as {json_path}, but its text report could not be written: {why}"
                return SaveResult(json_path, error)
            return SaveResult(None, f"could not save run to {self.repo.dir}: {why}", storage.rescue_run(run))


# --------------------------------------------------------------------------- #
# The web UI's background job
# --------------------------------------------------------------------------- #


class JobBusy(Exception):
    """A benchmark is already running (or starting)."""


@dataclass
class _JobState:
    running: bool = False
    starting: bool = False  # prepare() is checking the config and the runs dir
    done: int = 0
    total: int = 0
    slow: int = 0
    failed: int = 0
    started_at: str | None = None
    t_start: float | None = None
    t_end: float | None = None
    est_seconds: float | None = None
    timeout_s: float | None = None  # per-query timeout of the running job
    slow_threshold_ms: float = 200
    last_run_id: str | None = None
    last_status: str | None = None
    error: str | None = None
    recent: deque[QueryRow] = field(default_factory=lambda: deque(maxlen=RECENT_MAX))
    cancel_event: threading.Event | None = None
    thread: threading.Thread | None = None


class JobManager:
    """One background benchmark at a time: start, cancel, poll, and stop on shutdown."""

    def __init__(self, service: BenchmarkService, log: Callable[[str], None] | None = None):
        self.service = service
        self.log = log or (lambda msg: None)
        self._lock = threading.Lock()  # guards _state
        self._state = _JobState()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._state.running

    def start(self, overrides: Overrides | None = None) -> int:
        """Start a benchmark in the background; returns its number of queries.

        Raises JobBusy, or what ``BenchmarkService.prepare`` raises. The lock is not held while the
        config and the runs dir are checked; ``starting`` keeps a second start out meanwhile.
        """
        with self._lock:
            if self._state.running or self._state.starting:
                raise JobBusy
            self._state.starting = True
        try:
            plan = self.service.prepare(overrides)
        except BaseException:
            with self._lock:
                self._state.starting = False
            raise
        cancel_event = threading.Event()
        thread = threading.Thread(
            target=self._main, args=(plan, cancel_event), name="dnsbench-job", daemon=True
        )
        with self._lock:
            last_run_id = (
                self._state.last_run_id
            )  # the previous run stays the UI's latest until this one is saved
            self._state = _JobState(
                running=True,
                total=plan.total,
                started_at=runner.utc_iso(),
                t_start=time.monotonic(),
                est_seconds=plan.estimate["est_seconds"],
                timeout_s=plan.config["settings"]["timeout_ms"] / 1000.0,
                slow_threshold_ms=plan.config["settings"]["slow_threshold_ms"],
                last_run_id=last_run_id,
                cancel_event=cancel_event,
                thread=thread,
            )
            thread.start()
        return plan.total

    def _on_progress(self, event: ProgressEvent) -> None:
        row = event["result"]
        with self._lock:
            job = self._state
            job.done = event["done"]
            job.total = event["total"]
            job.recent.append(row)
            if row["status"] != "ok":
                job.failed += 1
            elif row["ms"] is not None and row["ms"] > job.slow_threshold_ms:
                job.slow += 1

    def _main(self, plan: RunPlan, cancel_event: threading.Event) -> None:
        run_id = None
        status = None
        error = None
        try:
            run = self.service.execute(plan, progress=self._on_progress, cancel_event=cancel_event)
            status = run["status"]
            if status == "partial":
                error = f"the benchmark stopped early after an internal error ({run.get('error')})"
                self.log(error)
            saved = self.service.persist(run)
            if saved.path is not None:
                run_id = run["id"]
            if saved.error is not None:
                problem = saved.error
                if saved.rescued is not None:
                    problem += f"; the full run record was written to {saved.rescued} instead"
                self.log(problem)
                error = f"{error}; {problem}" if error else problem
            elif error:
                error += f"; the {len(run['results'])} queries measured before it were saved"
        except Exception as exc:  # the job's own thread: report the failure, never take the server down
            error = f"{type(exc).__name__}: {exc}"
            self.log(f"benchmark failed: {error}")
        finally:  # whatever happened, the job is over: never leave the UI showing a run that isn't going
            with self._lock:
                self._state.running = False
                self._state.t_end = time.monotonic()
                if run_id:
                    self._state.last_run_id = run_id
                self._state.last_status = status
                self._state.error = error

    def cancel(self) -> bool:
        with self._lock:
            if not self._state.running or self._state.cancel_event is None:
                return False
            self._state.cancel_event.set()
            return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            job = self._state
            now = time.monotonic()
            if job.t_start is None:  # noqa: SIM108 - a nested conditional expression would be harder to read
                elapsed = 0.0
            else:
                elapsed = (now if job.running else (job.t_end or now)) - job.t_start
            eta = None
            if job.running:
                if job.done > 0:
                    eta = elapsed * (job.total - job.done) / job.done
                elif job.est_seconds is not None:
                    eta = max(0.0, job.est_seconds - elapsed)
            return {
                "running": job.running,
                "done": job.done,
                "total": job.total,
                "elapsed_s": round(elapsed, 2),
                "eta_s": None if eta is None else round(eta, 1),
                "started_at": job.started_at,
                "last_run_id": job.last_run_id,
                "last_status": job.last_status,
                "error": job.error,
                "slow": job.slow,
                "failed": job.failed,
                "recent": list(job.recent),
            }

    def stop(self, wait_s: float | None = None) -> bool:
        """Cancel a running job and wait for it to save the partial run.

        A job still starting (its config and runs dir being checked) is waited for first, then
        cancelled, so a shutdown never abandons it. A query already in flight can't be interrupted,
        so by default this waits for up to one full query timeout plus a margin for writing the files
        (at least 5 s). Returns False if the job is still running.
        """
        deadline = time.monotonic() + 5.0
        while True:
            with self._lock:
                starting = self._state.starting
            if not starting:
                break
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        if not self.cancel():
            return True
        with self._lock:
            thread, timeout_s = self._state.thread, self._state.timeout_s
        if thread is None:
            return True
        thread.join(wait_s if wait_s is not None else max(5.0, (timeout_s or 0.0) + 3.0))
        return not thread.is_alive()
