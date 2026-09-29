"""Latency statistics and aggregations.

Percentiles use the nearest-rank method: ``idx = int(p/100 * n + 0.999999)``
clamped to [1, n] over sorted latencies (the awk program in
archive/dns-test.sh; a test checks that they agree).

Timeouts and errors are EXCLUDED from the latency numbers: counting them as
0 ms would make a failing resolver look *faster*. Failures are reported
separately via ``failure_rate``.

Failures come in kinds (``failure_kind``). A local error (no socket, no route from this computer, a
bug) says nothing about the resolver, so it is left out of the failure and retry rates altogether.

Cache effects: a resolver answers a name it was just asked from its cache, and the benchmark's own
repeats (more rounds, or a provider's second server asking the same name) are exactly that. A user's
device caches an answer for its TTL, so a resolver sees a user's name about once per TTL: the first
answer is the realistic sample. Every latency figure (mean, median, percentiles, min, max, stdev and
the intervals) therefore uses only the first answer of each domain from each resolver in each run.
Repeats would otherwise pull the figures down, more so for a provider with more servers, and make them
look more precise than they are (samples of one domain are not independent). ``repeat_n`` and
``repeat_median`` show the repeats apart; they still count for the failure and retry rates.

Uncertainty: ``median_ci`` and ``p95_ci`` are exact 95 % confidence intervals from order statistics
(``quantile_ci``), ``wilson`` gives one for a rate, and ``rate_difference_low`` tells whether one rate
is significantly higher than another (Newcombe's interval for a difference, built from two Wilson
intervals). A p95 interval needs 72 samples before it has an upper bound, so tails are compared with
``tails_differ`` instead: Fisher's exact test on how many of each sample's answers lie above the p95 of
both pooled (a median test at the 95th percentile). That test has a floor too: with 60 answers each,
only a 6-to-0 split of the slowest 5 % counts. The intervals assume independent samples; they are a
guide to what is noise, not a guarantee.
"""

from __future__ import annotations

import functools
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal, cast

from .models import LatencyStats, QueryRow, Summary, TailsDiffer

# Result rows as they come: from the runner, from a run file (migrated), or built by a test.
Row = Mapping[str, Any]

SLOW_LIST_MAX = 200
SLOW_PER_RESOLVER_MAX = 100  # rows kept per resolver in ``slow_by_resolver``

CONFIDENCE_Z = 1.959963984540054  # two-sided 95 %
_ALPHA_HALF = (1, 40)  # 2.5 % in each tail, as a fraction for exact integer arithmetic
# Exact ranks up to this sample size (0.14 s at 10,000, once per size: cached). Above it, the normal
# approximation: within a rank of the exact answer, but for the skewed p95 one tail can exceed 2.5 %.
_EXACT_CI_MAX_N = 10_000

FailureKind = Literal["timeout", "answer", "network", "local"]
_HOST_UNREACHABLE = ("Host is down", "No route to host")  # strerror of EHOSTDOWN, EHOSTUNREACH


def nearest_rank[T](sorted_values: Sequence[T], p: float) -> T | None:
    """Nearest-rank percentile exactly like the original awk program."""
    n = len(sorted_values)
    if n == 0:
        return None
    idx = int((p / 100) * n + 0.999999)
    idx = min(max(idx, 1), n)
    return sorted_values[idx - 1]


def median(sorted_values: Sequence[float]) -> float | None:
    n = len(sorted_values)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return sorted_values[mid]
    return (sorted_values[mid - 1] + sorted_values[mid]) / 2


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(float(x), nd)


def failure_kind(row: Row) -> FailureKind | None:
    """How a query failed, or None if it was answered.

    * ``timeout``: no reply in time;
    * ``answer``: the resolver replied with an error rcode (SERVFAIL, REFUSED, ...);
    * ``network``: an ICMP error came back (port or host unreachable), or the resolver's host is
      down or unreachable ("Host is down", "No route to host": macOS reports a dead on-link host on
      send, after ARP fails; Linux reports it on receive);
    * ``local``: the query never properly left this computer (no socket, no route to the network, a
      crash in the query code). Not the resolver's fault, so it is not charged to it.
    """
    status = row.get("status")
    if status == "ok":
        return None
    if status == "timeout":
        return "timeout"
    if row.get("rcode"):
        return "answer"
    # resolver.py prefixes each error with where it happened. "recv:" means the query left this
    # computer and something came back. A host that is down or has no route is the resolver's problem
    # wherever it shows up. The rest ("socket:", "connect:", "send:" otherwise, "address:", an invalid
    # name, a crash the runner caught) happened before the query got anywhere. The error text is
    # strerror's, which Python leaves in English, so this also reads files from other systems.
    error = str(row.get("error") or "")
    if error.startswith("recv:") or any(s in error for s in _HOST_UNREACHABLE):
        return "network"
    return "local"


def wilson(k: int, n: int) -> tuple[float, float]:
    """The 95 % Wilson score interval of the rate k/n; (0.0, 1.0) when n is 0."""
    if n <= 0:
        return (0.0, 1.0)
    z2 = CONFIDENCE_Z * CONFIDENCE_Z
    p = k / n
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = CONFIDENCE_Z / (1 + z2 / n) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def rate_difference_low(k1: int, n1: int, k2: int, n2: int) -> float:
    """Lower bound of the 95 % interval of k1/n1 - k2/n2 (Newcombe's hybrid score method).

    Positive means the first rate is significantly higher. With no samples on either side it is -1.
    """
    if n1 <= 0 or n2 <= 0:
        return -1.0
    p1, p2 = k1 / n1, k2 / n2
    lo1, _ = wilson(k1, n1)
    _, hi2 = wilson(k2, n2)
    return p1 - p2 - math.sqrt((p1 - lo1) ** 2 + (hi2 - p2) ** 2)


@functools.lru_cache(maxsize=512)
def quantile_ci_ranks(n: int, num: int, den: int) -> tuple[int, int]:
    """1-based ranks (lo, hi) of the order statistics bounding a 95 % CI of the num/den quantile.

    The count of samples below the true quantile is Binomial(n, q). ``lo`` is the largest rank with
    P(B < lo) <= 2.5 %, ``hi`` the smallest with P(B >= hi) <= 2.5 %, so P(x_lo <= quantile <= x_hi)
    >= 95 %. lo is 0 when no sample is low enough (unbounded below) and hi is n + 1 when none is high
    enough (unbounded above). Exact, in integers, up to _EXACT_CI_MAX_N; the normal approximation
    above (total coverage still about 95 %, but one tail may be a little over 2.5 %).
    """
    if not 0 < num < den:
        raise ValueError(f"quantile must be strictly between 0 and 1, got {num}/{den}")
    if n <= 0:
        return (0, 1)
    if n > _EXACT_CI_MAX_N:
        q = num / den
        spread = CONFIDENCE_Z * math.sqrt(n * q * (1 - q))
        lo = math.floor(n * q + 0.5 - spread)
        hi = math.ceil(n * q + 0.5 + spread)
        return (max(lo, 0), min(hi, n + 1))
    # P(B = k) = C(n, k) * num^k * (den - num)^(n - k) / den^n, kept as integer numerators.
    a, b = num, den - num
    total = den**n
    limit_num, limit_den = _ALPHA_HALF
    pmf = [b**n]
    for k in range(n):  # C(n, k+1) a^(k+1) b^(n-k-1) from the previous term; the division is exact
        pmf.append(pmf[-1] * (n - k) * a // ((k + 1) * b))
    lo, below = 0, 0  # below = P(B < lo) * total
    while lo < n and (below + pmf[lo]) * limit_den <= limit_num * total:
        below += pmf[lo]
        lo += 1
    hi, above = n + 1, 0  # above = P(B >= hi) * total
    while hi > 1 and (above + pmf[hi - 1]) * limit_den <= limit_num * total:
        above += pmf[hi - 1]
        hi -= 1
    return (lo, hi)


def quantile_ci(sorted_values: Sequence[float], num: int, den: int) -> list[float | None] | None:
    """95 % CI [lo, hi] of the num/den quantile of ``sorted_values``; None bounds are unbounded.

    None if there are no values.
    """
    n = len(sorted_values)
    if n == 0:
        return None
    lo, hi = quantile_ci_ranks(n, num, den)
    return [
        _r(sorted_values[lo - 1]) if lo >= 1 else None,
        _r(sorted_values[hi - 1]) if hi <= n else None,
    ]


def _attempts(row: Row) -> int:
    """Attempts a query needed; rows saved before this field existed count as 1."""
    try:
        return int(row.get("attempts") or 1)
    except (TypeError, ValueError):
        return 1


def _answered(row: Row) -> bool:
    return row.get("status") == "ok" and row.get("ms") is not None


def latency_stats(rows: Iterable[Row], first: set[int] | None = None) -> LatencyStats:
    """Counts over all rows; latency figures over the first answers among them.

    ``first`` holds the ``id()`` of the rows that were the first answer of their domain from their
    resolver (see ``first_answers``). Every latency figure uses only those; the other answers are the
    repeats (``repeat_n``, ``repeat_median``). None: every answer counts as a first.

    Local errors (``failure_kind``) are counted in ``local_errors`` and left out of everything else
    except ``n``: ``failure_rate`` and ``retry_rate`` are over the other ``n - local_errors`` queries.
    ``retried`` counts queries that answered only on a retry (tries > 1): their first attempt timed
    out, which cost the user a full timeout. A query whose every try timed out is a failure, not a
    retry.
    """
    rows = list(rows)
    n = len(rows)
    kinds = [failure_kind(r) for r in rows]
    local = kinds.count("local")
    counted = n - local
    answered = [r for r in rows if _answered(r)]
    ok = len(answered)
    timeouts = kinds.count("timeout")
    failures = counted - ok
    retried = sum(1 for r in answered if _attempts(r) > 1)
    lat = sorted(float(r["ms"]) for r in answered if first is None or id(r) in first)
    repeats = sorted(float(r["ms"]) for r in answered if first is not None and id(r) not in first)
    out: LatencyStats = {
        "n": n,
        "ok": ok,
        "failures": failures,
        "failure_rate": round(failures / counted, 4) if counted else 0.0,
        "timeouts": timeouts,
        "errors": failures - timeouts,
        "local_errors": local,
        "retried": retried,
        "retry_rate": round(retried / counted, 4) if counted else 0.0,
        "mean": None,
        "median": None,
        "p80": None,
        "p95": None,
        "p98": None,
        "min": None,
        "max": None,
        "stdev": None,
        "median_ci": quantile_ci(lat, 1, 2),
        "p95_ci": quantile_ci(lat, 19, 20),
        "first_n": len(lat),
        "repeat_n": len(repeats),
        "repeat_median": _r(median(repeats)),
    }
    if lat:  # every resolver, server or domain with an answer has a first answer
        mean = math.fsum(lat) / len(lat)
        var = math.fsum((x - mean) ** 2 for x in lat) / len(lat)  # population
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


def tails_differ(a: Sequence[float], b: Sequence[float]) -> bool:
    """True if one sample has significantly more answers above the p95 of both pooled than the other.

    Fisher's exact test, two-sided as two one-sided tests at 2.5 %: given how many answers lie above
    the pooled p95, how unlikely is a split this uneven? (The count above is fixed by the pooling, so
    the two sides are not independent binomials, and a test that assumes they are rejects too often
    when the samples differ in size.) Values equal to the threshold count as not above it.
    """
    if not a or not b:
        return False
    threshold = nearest_rank(sorted([*a, *b]), 95)
    assert threshold is not None  # both non-empty
    ka = sum(1 for x in a if x > threshold)
    kb = sum(1 for x in b if x > threshold)
    na, nb, k = len(a), len(b), ka + kb
    lo, hi = max(0, k - nb), min(k, na)
    # log of C(k, x) * C(na + nb - k, na - x) / C(na + nb, na): the chance that x of the k are in a
    base = _log_comb(na + nb, na)
    pmf = {x: math.exp(_log_comb(k, x) + _log_comb(na + nb - k, na - x) - base) for x in range(lo, hi + 1)}
    upper = sum(p for x, p in pmf.items() if x >= ka)  # a this far above its share, or further
    lower = sum(p for x, p in pmf.items() if x <= ka)
    return upper <= 0.025 or lower <= 0.025


def _log_comb(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def _tail_pairs(samples: Mapping[str, Sequence[float]]) -> dict[str, list[str]]:
    """{name: [names whose tail differs significantly from it]}, for every pair in ``samples``."""
    names = list(samples)
    out: dict[str, list[str]] = {}
    for i, x in enumerate(names):
        for y in names[i + 1 :]:
            if tails_differ(samples[x], samples[y]):
                out.setdefault(x, []).append(y)
                out.setdefault(y, []).append(x)
    return out


def first_answers(results: Sequence[Row], per_server: bool = False) -> set[int]:
    """The ``id()`` of each row that is the first answer of its domain from its resolver.

    "First" is by start time within a run (merged runs are told apart by ``run_id``), so a provider's
    two servers share one first answer per domain: they often share a cache too. ``per_server`` counts
    each server on its own instead, for comparing a provider's servers with each other (otherwise,
    without shuffling, one server could get every first answer).
    """
    order = sorted(
        range(len(results)), key=lambda i: (str(results[i].get("run_id") or ""), _t(results[i]), i)
    )
    seen: set[tuple[str, str, str, str]] = set()
    out: set[int] = set()
    for i in order:
        row = results[i]
        if not _answered(row):
            continue
        server = str(row["server"]) if per_server else ""
        key = (str(row.get("run_id") or ""), str(row["resolver"]), server, str(row["domain"]).lower())
        if key not in seen:
            seen.add(key)
            out.add(id(row))
    return out


def _t(row: Row) -> float:
    t = row.get("t")
    return float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else 0.0


def unanswered_domains(results: Iterable[Row], resolvers: Iterable[str]) -> dict[str, list[str]]:
    """{resolver: [domains]} it answered only with no address (NXDOMAIN, or NOERROR with no records)
    although another resolver returned records for them: a sign of filtering or blocking.

    A truncated reply may have had its records cut, so it counts as neither.
    """
    has_records: dict[str, set[str]] = {}  # domain -> resolvers that returned records
    empty: dict[str, set[str]] = {}  # domain -> resolvers that answered it without records
    for row in results:
        if row.get("status") != "ok" or row.get("truncated"):
            continue
        domain, name = str(row["domain"]), str(row["resolver"])
        if int(row.get("answers") or 0) > 0:
            has_records.setdefault(domain, set()).add(name)
        else:
            empty.setdefault(domain, set()).add(name)
    out: dict[str, list[str]] = {}
    for domain, names in empty.items():
        answered_by = has_records.get(domain, set())
        for name in names - answered_by:
            if answered_by:
                out.setdefault(name, []).append(domain)
    return {name: sorted(out[name]) for name in resolvers if name in out}


def _ordered(present: list[str], preferred: Iterable[str] | None) -> list[str]:
    """Items of ``preferred`` that are present (in that order), then the rest."""
    present_set = set(present)
    out = [x for x in (preferred or []) if x in present_set]
    seen = set(out)
    out += [x for x in dict.fromkeys(present) if x not in seen]
    return out


def _first_seen(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def orders_from_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """kwargs for summarize() that keep config order for resolvers/domains/servers."""
    if not config:
        return {}
    resolvers = [r.get("name") for r in config.get("resolvers") or [] if isinstance(r, dict)]
    servers = {
        r.get("name"): list(r.get("servers") or [])
        for r in config.get("resolvers") or []
        if isinstance(r, dict)
    }
    kw: dict[str, Any] = {
        "resolver_order": resolvers,
        "domain_order": list(config.get("domains") or []),
        "server_order": servers,
    }
    settings = config.get("settings") or {}
    if "slow_threshold_ms" in settings:
        kw["slow_threshold_ms"] = settings["slow_threshold_ms"]
    return kw


def summarize(
    results: Iterable[Row],
    resolver_order: Iterable[str] | None = None,
    slow_threshold_ms: float = 200,
    domain_order: Iterable[str] | None = None,
    server_order: Mapping[str, Iterable[str]] | None = None,
) -> Summary:
    """Aggregate result rows overall, by resolver, by server and by domain."""
    results = list(results)
    first = first_answers(results)
    first_per_server = first_answers(results, per_server=True)
    resolvers = _ordered(_first_seen(r["resolver"] for r in results), resolver_order)
    domains = _ordered(_first_seen(r["domain"] for r in results), domain_order)

    by_res: dict[str, list[Row]] = {name: [] for name in resolvers}
    by_srv: dict[str, dict[str, list[Row]]] = {name: {} for name in resolvers}
    by_dom: dict[str, dict[str, list[Row]]] = {d: {} for d in domains}
    for row in results:
        name = row["resolver"]
        by_res[name].append(row)
        by_srv[name].setdefault(row["server"], []).append(row)
        by_dom[row["domain"]].setdefault(name, []).append(row)

    by_server: dict[str, dict[str, LatencyStats]] = {}
    for name in resolvers:
        preferred = (server_order or {}).get(name)
        servers = _ordered(list(by_srv[name].keys()), preferred)
        by_server[name] = {s: latency_stats(by_srv[name][s], first_per_server) for s in servers}

    by_domain: dict[str, dict[str, LatencyStats]] = {}
    for d in domains:
        per = by_dom[d]
        by_domain[d] = {name: latency_stats(per[name], first) for name in resolvers if name in per}

    def first_ms(rows: Iterable[Row], first_set: set[int]) -> list[float]:
        return [float(r["ms"]) for r in rows if _answered(r) and id(r) in first_set]

    tails = {
        "resolvers": _tail_pairs({name: first_ms(by_res[name], first) for name in resolvers}),
        "servers": {
            name: pairs
            for name in resolvers
            if (
                pairs := _tail_pairs(
                    {s: first_ms(rows, first_per_server) for s, rows in by_srv[name].items()}
                )
            )
        },
    }

    slow_all = [
        r
        for r in results
        if r.get("status") == "ok" and r.get("ms") is not None and r["ms"] > slow_threshold_ms
    ]
    slow_all.sort(key=lambda r: r["ms"], reverse=True)
    # The global list is capped across all resolvers, so a slow resolver can
    # crowd the others out of it; keep a (capped) list and a count per resolver.
    slow_by_resolver: dict[str, list[Row]] = {}
    slow_count_by_resolver: dict[str, int] = {}
    for row in slow_all:
        name = row["resolver"]
        slow_count_by_resolver[name] = slow_count_by_resolver.get(name, 0) + 1
        capped = slow_by_resolver.setdefault(name, [])
        if len(capped) < SLOW_PER_RESOLVER_MAX:
            capped.append(row)

    return {
        "overall": latency_stats(results, first),
        "resolvers": resolvers,
        "domains": domains,
        "by_resolver": {name: latency_stats(by_res[name], first) for name in resolvers},
        "by_server": by_server,
        "by_domain": by_domain,
        "slow": cast(list[QueryRow], slow_all[:SLOW_LIST_MAX]),
        "slow_count": len(slow_all),
        "slow_by_resolver": {
            n: cast(list[QueryRow], slow_by_resolver[n]) for n in resolvers if n in slow_by_resolver
        },
        "slow_count_by_resolver": {
            n: slow_count_by_resolver[n] for n in resolvers if n in slow_count_by_resolver
        },
        "slow_threshold_ms": slow_threshold_ms,
        "unanswered": unanswered_domains(results, resolvers),
        "tails_differ": cast(TailsDiffer, tails),
    }


def summarize_run(run: Mapping[str, Any]) -> Summary:
    return summarize(run.get("results") or [], **orders_from_config(run.get("config")))


def merge_runs(runs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Concatenate the results of several runs (resolver identity = name).

    Each row is copied and tagged with ``run_id``.
    """
    merged = []
    for run in runs:
        rid = run.get("id")
        for row in run.get("results") or []:
            merged.append({**row, "run_id": rid})
    return merged
