"""Latency statistics and aggregations.

Percentiles use the same nearest-rank method as the original awk script:
``idx = int(p/100 * n + 0.999999)`` clamped to [1, n] over sorted latencies.

Unlike the original, timeouts and errors are EXCLUDED from the latency
numbers (the old script recorded them as 0 ms, which made a failing resolver
look *faster*). Failures are reported separately via ``failure_rate``.
"""

from __future__ import annotations

import math

SLOW_LIST_MAX = 200
SLOW_PER_RESOLVER_MAX = 100  # rows kept per resolver in ``slow_by_resolver``


def nearest_rank(sorted_values: list, p: float):
    """Nearest-rank percentile exactly like the original awk program."""
    n = len(sorted_values)
    if n == 0:
        return None
    idx = int((p / 100) * n + 0.999999)
    idx = min(max(idx, 1), n)
    return sorted_values[idx - 1]


def median(sorted_values: list):
    n = len(sorted_values)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return sorted_values[mid]
    return (sorted_values[mid - 1] + sorted_values[mid]) / 2


def _r(x, nd=2):
    return None if x is None else round(float(x), nd)


def _attempts(row) -> int:
    """Attempts a query needed; rows saved before this field existed count as 1."""
    try:
        return int(row.get("attempts") or 1)
    except (TypeError, ValueError):
        return 1


def latency_stats(rows) -> dict:
    """Counts over all rows; latency stats over rows with status "ok" only.

    ``retried`` counts queries that needed more than one attempt (tries > 1):
    their earlier attempts timed out, which cost the user a full timeout even
    when the retry answered. ``retry_rate`` = retried / n.
    """
    rows = list(rows)
    n = len(rows)
    lat = sorted(float(r["ms"]) for r in rows if r.get("status") == "ok" and r.get("ms") is not None)
    ok = len(lat)
    timeouts = sum(1 for r in rows if r.get("status") == "timeout")
    failures = n - ok
    retried = sum(1 for r in rows if _attempts(r) > 1)
    out = {
        "n": n,
        "ok": ok,
        "failures": failures,
        "failure_rate": round(failures / n, 4) if n else 0.0,
        "timeouts": timeouts,
        "errors": failures - timeouts,
        "retried": retried,
        "retry_rate": round(retried / n, 4) if n else 0.0,
        "mean": None,
        "median": None,
        "p80": None,
        "p95": None,
        "p98": None,
        "min": None,
        "max": None,
        "stdev": None,
    }
    if ok:
        mean = math.fsum(lat) / ok
        var = math.fsum((x - mean) ** 2 for x in lat) / ok  # population
        out.update(
            {
                "mean": _r(mean),
                "median": _r(median(lat)),
                "p80": _r(nearest_rank(lat, 80)),
                "p95": _r(nearest_rank(lat, 95)),
                "p98": _r(nearest_rank(lat, 98)),
                "min": _r(lat[0]),
                "max": _r(lat[-1]),
                "stdev": _r(math.sqrt(var)),
            }
        )
    return out


def _ordered(present: list, preferred) -> list:
    """Items of ``preferred`` that are present (in that order), then the rest."""
    present_set = set(present)
    out = [x for x in (preferred or []) if x in present_set]
    seen = set(out)
    out += [x for x in dict.fromkeys(present) if x not in seen]
    return out


def _first_seen(values) -> list:
    return list(dict.fromkeys(values))


def orders_from_config(config: dict | None) -> dict:
    """kwargs for summarize() that keep config order for resolvers/domains/servers."""
    if not config:
        return {}
    resolvers = [r.get("name") for r in config.get("resolvers") or [] if isinstance(r, dict)]
    servers = {
        r.get("name"): list(r.get("servers") or [])
        for r in config.get("resolvers") or []
        if isinstance(r, dict)
    }
    kw = {
        "resolver_order": resolvers,
        "domain_order": list(config.get("domains") or []),
        "server_order": servers,
    }
    settings = config.get("settings") or {}
    if "slow_threshold_ms" in settings:
        kw["slow_threshold_ms"] = settings["slow_threshold_ms"]
    return kw


def summarize(
    results,
    resolver_order=None,
    slow_threshold_ms: float = 200,
    domain_order=None,
    server_order: dict | None = None,
) -> dict:
    """Aggregate result rows overall, by resolver, by server and by domain."""
    results = list(results)
    resolvers = _ordered(_first_seen(r["resolver"] for r in results), resolver_order)
    domains = _ordered(_first_seen(r["domain"] for r in results), domain_order)

    by_res: dict[str, list] = {name: [] for name in resolvers}
    by_srv: dict[str, dict[str, list]] = {name: {} for name in resolvers}
    by_dom: dict[str, dict[str, list]] = {d: {} for d in domains}
    for row in results:
        name = row["resolver"]
        by_res[name].append(row)
        by_srv[name].setdefault(row["server"], []).append(row)
        by_dom[row["domain"]].setdefault(name, []).append(row)

    by_server = {}
    for name in resolvers:
        preferred = (server_order or {}).get(name)
        servers = _ordered(list(by_srv[name].keys()), preferred)
        by_server[name] = {s: latency_stats(by_srv[name][s]) for s in servers}

    by_domain = {}
    for d in domains:
        per = by_dom[d]
        by_domain[d] = {name: latency_stats(per[name]) for name in resolvers if name in per}

    slow_all = [
        r
        for r in results
        if r.get("status") == "ok" and r.get("ms") is not None and r["ms"] > slow_threshold_ms
    ]
    slow_all.sort(key=lambda r: r["ms"], reverse=True)
    # The global list is capped across all resolvers, so a slow resolver can
    # crowd the others out of it; keep a (capped) list and a count per resolver.
    slow_by_resolver: dict[str, list] = {}
    slow_count_by_resolver: dict[str, int] = {}
    for row in slow_all:
        name = row["resolver"]
        slow_count_by_resolver[name] = slow_count_by_resolver.get(name, 0) + 1
        capped = slow_by_resolver.setdefault(name, [])
        if len(capped) < SLOW_PER_RESOLVER_MAX:
            capped.append(row)

    return {
        "overall": latency_stats(results),
        "resolvers": resolvers,
        "domains": domains,
        "by_resolver": {name: latency_stats(by_res[name]) for name in resolvers},
        "by_server": by_server,
        "by_domain": by_domain,
        "slow": slow_all[:SLOW_LIST_MAX],
        "slow_count": len(slow_all),
        "slow_by_resolver": {n: slow_by_resolver[n] for n in resolvers if n in slow_by_resolver},
        "slow_count_by_resolver": {
            n: slow_count_by_resolver[n] for n in resolvers if n in slow_count_by_resolver
        },
        "slow_threshold_ms": slow_threshold_ms,
    }


def summarize_run(run: dict) -> dict:
    return summarize(run.get("results") or [], **orders_from_config(run.get("config")))


def merge_runs(runs) -> list[dict]:
    """Concatenate the results of several runs (resolver identity = name).

    Each row is copied and tagged with ``run_id``.
    """
    merged = []
    for run in runs:
        rid = run.get("id")
        for row in run.get("results") or []:
            merged.append({**row, "run_id": rid})
    return merged
