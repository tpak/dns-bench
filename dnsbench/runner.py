"""Rate-limited concurrent benchmark scheduler (the speed + politeness core).

The original script was slow because it was fully sequential with a global
0.8 s sleep between *every* query. Here we parallelise ACROSS servers while
strictly rate-limiting PER server:

* one worker thread per server IP, all running at once, so every server is
  measured over the same stretch of time (network conditions drift, so servers
  measured in different time windows aren't comparable);
* each worker issues its queries sequentially, so there is never more than
  ONE query in flight to a given server;
* consecutive query start times to the same server are at least
  ``per_server_interval_ms`` apart (plus 0-10 % random jitter), also for
  retries and also after a timeout. The interval is clamped to a hard 50 ms
  floor no matter what config is passed in.

So the load on any one server is at most 1000/interval queries/s (4 qps by
default) while the wall time is roughly domains x rounds x interval instead of
the sum over all servers.
"""

from __future__ import annotations

import contextlib
import copy
import random
import socket
import threading
import time
from datetime import UTC, datetime

from . import __version__, resolver
from .config import DEFAULT_SETTINGS, MIN_INTERVAL_MS, normalize_server, server_key

JITTER = 0.10  # up to +10 % on top of the interval, never negative
_WAIT_SLICE_S = 0.05  # cancel responsiveness while waiting for the next slot


def effective_settings(config: dict) -> dict:
    s = {**DEFAULT_SETTINGS, **(config.get("settings") or {})}
    s["per_server_interval_ms"] = max(MIN_INTERVAL_MS, int(s["per_server_interval_ms"]))
    s["tries"] = max(1, int(s["tries"]))
    s["rounds"] = max(1, int(s["rounds"]))
    return s


def build_jobs(config: dict, rng: random.Random | None = None) -> list[dict]:
    """One job per unique server of each enabled resolver.

    Each job's (domain, round) list is shuffled independently when
    ``settings.shuffle`` is on, so the two servers of one provider don't ask
    for the same name at the same instant (shared-cache bias).
    """
    rng = rng or random.Random()
    settings = effective_settings(config)
    domains = list(config.get("domains") or [])
    rounds = settings["rounds"]
    jobs: list[dict] = []
    seen: set[str] = set()
    for r in config.get("resolvers") or []:
        if not r.get("enabled", True):
            continue
        for server in r.get("servers") or []:
            # Canonical host key: '1.1.1.1', '::ffff:1.1.1.1' and '::ffff:1.1.1.1%3'
            # are one host, so they must share one worker (and its rate limit).
            key = server_key(server) or normalize_server(server)
            if key in seen:  # never let two workers hit the same IP
                continue
            seen.add(key)
            items = [(d, rnd) for rnd in range(1, rounds + 1) for d in domains]
            if settings["shuffle"]:
                rng.shuffle(items)
            jobs.append({"resolver": r["name"], "server": server, "items": items})
    return jobs


def _field(res, name, default=None):
    if isinstance(res, dict):
        return res.get(name, default)
    return getattr(res, name, default)


def _utc_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def run_benchmark(
    config: dict,
    query_fn=None,
    progress=None,
    cancel_event=None,
    clock=time.monotonic,
    sleep=time.sleep,
    rng: random.Random | None = None,
) -> dict:
    """Run the benchmark described by ``config`` and return the run record.

    ``query_fn(server, domain, record_type=..., timeout_s=..., tries=1)`` must
    return a ``resolver.QueryResult`` (or a dict with the same fields).
    ``progress(event)`` is called for every finished query; calls are
    serialised by the runner so the callback needn't be thread-safe.
    Setting ``cancel_event`` stops workers promptly; the record then has
    ``status == "cancelled"`` and contains the results gathered so far.
    If a worker crashes (a bug, not a failed query: query_fn's own exceptions
    are recorded as ``error`` rows), the run stops the same way and comes back
    with ``status == "partial"`` and the crash in ``error``, so the queries
    already measured are still returned and can be saved.

    A KeyboardInterrupt in the calling thread cancels the run like
    ``cancel_event``. A second one, once cancelled, stops waiting for the
    queries still in flight (up to one timeout each): the record is returned at
    once, and rows that finish later are dropped.
    """
    query_fn = query_fn or resolver.query
    cancel_event = cancel_event or threading.Event()
    rng = rng or random.Random()
    settings = effective_settings(config)
    interval_s = settings["per_server_interval_ms"] / 1000.0
    timeout_s = settings["timeout_ms"] / 1000.0
    tries = settings["tries"]
    record_type = settings["record_type"]

    snapshot = copy.deepcopy(config)
    snapshot["settings"] = dict(settings)

    jobs = build_jobs(config, rng)
    total = sum(len(j["items"]) for j in jobs)
    results: list[dict] = []
    lock = threading.Lock()  # guards results and state
    state: dict = {"done": 0, "crash": None, "closed": False}  # closed: the record has been returned

    started_wall = datetime.now(UTC)
    t0 = clock()

    def wait_until(deadline: float) -> bool:
        """Sleep until clock() >= deadline. False if cancelled meanwhile."""
        while True:
            if cancel_event.is_set():
                return False
            remaining = deadline - clock()
            if remaining <= 0:
                return True
            sleep(min(remaining, _WAIT_SLICE_S))

    def worker(job: dict, seed: int) -> None:
        try:
            measure(job, seed)
        except Exception as exc:  # a bug in the measuring itself: stop the run, keep what was measured
            with lock:
                if state["crash"] is None:
                    state["crash"] = f"{type(exc).__name__}: {exc} (while measuring {job['server']})"
            cancel_event.set()

    def measure(job: dict, seed: int) -> None:
        wrng = random.Random(seed)
        name, server = job["resolver"], job["server"]
        next_start: float | None = None  # earliest allowed start of the next query
        for domain, rnd in job["items"]:
            attempts = 0
            while True:
                if next_start is not None and not wait_until(next_start):
                    return
                if cancel_event.is_set():
                    return
                start = clock()
                # Reserve the next slot *before* sending: start-to-start spacing
                # holds regardless of how long this query takes (or times out).
                next_start = start + interval_s * (1.0 + wrng.uniform(0.0, JITTER))
                attempts += 1
                try:
                    res = query_fn(server, domain, record_type=record_type, timeout_s=timeout_s, tries=1)
                except Exception as exc:  # a broken query_fn must not kill the run
                    res = resolver.QueryResult("error", error=f"{type(exc).__name__}: {exc}")
                status = _field(res, "status", "error")
                # Retries go through the same rate limiter as everything else.
                if status == "timeout" and attempts < tries:
                    continue
                break
            ms = _field(res, "ms")
            row = {
                "resolver": name,
                "server": server,
                "domain": domain,
                "round": rnd,
                "status": status,
                "ms": round(float(ms), 3) if ms is not None else None,
                "rcode": _field(res, "rcode"),
                "answers": int(_field(res, "answers", 0) or 0),
                "error": _field(res, "error"),
                "t": round(start - t0, 3),
                # >1 means earlier attempts timed out: each cost the user a full
                # timeout even if the retry answered (ms is the retry's RTT).
                "attempts": attempts,
            }
            with lock:
                if state["closed"]:  # the caller stopped waiting (a second Ctrl-C): nothing more to report
                    return
                results.append(row)
                state["done"] += 1
                if progress is not None:
                    with contextlib.suppress(Exception):  # a UI hiccup must never break the measurement
                        progress({"type": "result", "done": state["done"], "total": total, "result": row})

    # One thread per server, all at once. The load stays bounded: one query in flight per server, at
    # most 1000/interval queries/s each, and config.MAX_RESOLVERS x MAX_SERVERS_PER_RESOLVER servers.
    # Daemon threads, not a ThreadPoolExecutor: Python joins executor threads when the process exits,
    # so a query abandoned by a second Ctrl-C would still hold up the exit for its whole timeout.
    seeds = [rng.getrandbits(64) for _ in jobs]
    threads = [
        threading.Thread(target=worker, args=(job, seed), name=f"dnsbench-{i}", daemon=True)
        for i, (job, seed) in enumerate(zip(jobs, seeds, strict=True))
    ]
    for thread in threads:
        thread.start()
    interrupted = False
    pending = threads
    while pending:
        try:
            pending[0].join(timeout=0.2)
        except KeyboardInterrupt:
            if cancel_event.is_set():  # a second interrupt: stop waiting for the queries in flight
                break
            interrupted = True
            cancel_event.set()
        pending = [thread for thread in pending if thread.is_alive()]
    with lock:
        state["closed"] = True  # a worker still in flight drops its row instead of adding it

    finished_wall = datetime.now(UTC)
    duration = clock() - t0
    cancelled = (cancel_event.is_set() or interrupted) and len(results) < total
    results.sort(key=lambda r: r["t"])
    record = {
        "id": started_wall.strftime("%Y%m%dT%H%M%SZ"),
        "version": __version__,
        "started_at": _utc_iso(started_wall),
        "finished_at": _utc_iso(finished_wall),
        "duration_s": round(duration, 2),
        "host": socket.gethostname(),
        "status": "partial" if state["crash"] else "cancelled" if cancelled else "complete",
        "config": snapshot,
        "results": results,
    }
    if state["crash"]:
        record["error"] = state["crash"]
    return record
