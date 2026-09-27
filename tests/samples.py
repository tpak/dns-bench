"""Sample runs shared by the storage, analysis, report and service tests (not a test module)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dnsbench import config as C

# Two records written by dns-bench 1.0.0, from real runs, with the hostname, the ISP resolver's IPs and a
# personal domain replaced (tests/fixtures/README.md). They pin what an old run file looks like, so a
# change to the run format or to loading (storage.migrate) is tested against real data rather than
# against records built by today's code.
V1_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "runs-v1"
V1_RUN_IDS = ["20260925T091918Z", "20260925T090918Z"]  # newest first


def row(resolver: str, server: str, domain: str, ms: float | None, status: str = "ok", **extra: Any) -> dict:
    """One result row as dns-bench 1.x wrote it (no attempts or truncated fields)."""
    return {
        "resolver": resolver,
        "server": server,
        "domain": domain,
        "round": 1,
        "status": status,
        "ms": ms,
        "rcode": "NOERROR" if status == "ok" else None,
        "answers": 1 if status == "ok" else 0,
        "error": None if status == "ok" else status,
        "t": 0.0,
        **extra,
    }


def make_run(
    run_id: str = "20260925T023456Z", started: str = "2026-09-25T02:34:56Z", fast_ms: float = 5.0
) -> dict:
    """A small raw run: Cloudflare (two servers) beats Google, which also has one timeout."""
    cfg = C.default_config()
    cfg["resolvers"] = [
        {"name": "Cloudflare", "servers": ["1.1.1.1", "1.0.0.1"], "enabled": True},
        {"name": "Google", "servers": ["8.8.8.8"], "enabled": True},
    ]
    cfg["domains"] = ["a.com", "b.com"]
    results = []
    t = 0.0
    for res, srv, base in (
        ("Cloudflare", "1.1.1.1", fast_ms),
        ("Cloudflare", "1.0.0.1", fast_ms + 1),
        ("Google", "8.8.8.8", 20.0),
    ):
        for d in cfg["domains"]:
            results.append(row(res, srv, d, base, t=t))
            t += 0.25
    results.append(row("Google", "8.8.8.8", "c.com", None, status="timeout", t=t))
    return {
        "id": run_id,
        "version": "1.0.0",
        "started_at": started,
        "finished_at": started,
        "duration_s": 1.5,
        "host": "testhost",
        "status": "complete",
        "config": cfg,
        "results": results,
    }
