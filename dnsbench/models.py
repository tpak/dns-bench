"""The shapes of the records dns-bench measures, saves and serves.

These are TypedDicts, not classes: a run is JSON on disk and over HTTP, and these types describe that
JSON as it is, so mypy can check the code that builds and reads it (REMEDIATION_PLAN.md chose
TypedDict over dataclass models). Nothing here runs; the module only defines names.

A run file holds a ``RunRecord``. Its ``schema`` is the file format's version (1.x files had none and
are read as 1: see ``storage.migrate``). ``summary`` and ``recommendation`` are derived from
``results`` and ``config``; analysis.py recomputes them whenever a run is loaded.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, NotRequired, Protocol, TypedDict

RUN_SCHEMA = 1  # the run file format written by this version

QueryStatus = Literal["ok", "timeout", "error"]
RunStatus = Literal["complete", "cancelled", "partial"]


class QueryRow(TypedDict):
    """One query's result, as the runner records it and the run file stores it."""

    resolver: str
    server: str
    domain: str
    round: int
    status: QueryStatus
    ms: float | None  # round trip of the answering attempt; None for a timeout or an error
    rcode: str | None
    answers: int
    error: str | None
    t: float  # seconds from the start of the run to this query's (last) attempt
    attempts: int
    truncated: bool  # the answer had the TC bit set (its latency is still valid)
    run_id: NotRequired[str]  # only in rows merged from several runs


class LatencyStats(TypedDict):
    """Counts over every query, latency over the ``ok`` ones only (None when there are none).

    See stats.py: local errors are left out of the rates, the tail figures (p80, p95, p98) use only
    each domain's first answer from the resolver, and the intervals are 95 % ones.
    """

    n: int
    ok: int
    failures: int  # timeouts and errors, not counting local errors
    failure_rate: float  # failures / (n - local_errors)
    timeouts: int
    errors: int  # error answers (SERVFAIL, REFUSED, ...) and ICMP errors
    local_errors: int  # failed on this computer: not charged to the resolver
    retried: int  # answered only on a retry
    retry_rate: float
    mean: float | None
    median: float | None
    p80: float | None
    p95: float | None
    p98: float | None
    min: float | None
    max: float | None
    stdev: float | None
    median_ci: list[float | None] | None  # [lo, hi]; a None bound is unbounded
    p95_ci: list[float | None] | None
    first_n: int  # answers that were their domain's first from this resolver
    first_median: float | None
    repeat_n: int  # the other answers: repeats, usually from the resolver's cache
    repeat_median: float | None


class TailsDiffer(TypedDict):
    """Pairs whose first answers' tails differ significantly (stats.tails_differ), both ways round."""

    resolvers: dict[str, list[str]]  # resolver -> resolvers
    servers: dict[str, dict[str, list[str]]]  # resolver -> server -> its sibling servers


class Summary(TypedDict):
    overall: LatencyStats
    resolvers: list[str]
    domains: list[str]
    by_resolver: dict[str, LatencyStats]
    by_server: dict[str, dict[str, LatencyStats]]  # resolver -> server -> stats
    by_domain: dict[str, dict[str, LatencyStats]]  # domain -> resolver -> stats
    slow: list[QueryRow]
    slow_count: int
    slow_by_resolver: dict[str, list[QueryRow]]
    slow_count_by_resolver: dict[str, int]
    slow_threshold_ms: float
    unanswered: dict[str, list[str]]  # resolver -> domains it gave no records for, though others did
    tails_differ: TailsDiffer


class RankEntry(TypedDict):
    rank: int
    resolver: str
    score: float
    median: float
    p95: float
    mean: float
    failure_rate: float
    retry_rate: float
    ok: int
    n: int
    fastest_server: str | None
    median_ci: list[float | None] | None  # 95 % intervals, as in LatencyStats
    p95_ci: list[float | None] | None
    failure_ci: list[float]  # 95 % Wilson interval of the failure rate, servers that never answered left out
    failures_counted: bool  # the failure rate is in the score: significantly higher than another's
    retries_counted: bool
    ties: list[str]  # resolvers within noise of this one (recommend.within_noise)


class Note(TypedDict):
    """One note under a recommendation. ``code`` and ``params`` say what it is about, for programs;
    ``text`` is the sentence to show, so no client needs a copy of the wording."""

    code: str
    params: dict[str, Any]
    text: str


class Recommendation(TypedDict):
    best: str | None
    backup: str | None
    tied_with: list[str]  # within noise of best
    backup_tied_with: list[str]  # within noise of the backup, from a provider other than best's
    suggested_servers: list[str]
    ranking: list[RankEntry]
    summary: str
    notes: list[Note]


class RunRecord(TypedDict):
    """A run, as ``runner.run_benchmark`` returns it and the run file stores it."""

    schema: int
    kind: Literal["run"]
    id: str
    version: str  # the dns-bench version that measured it
    started_at: str
    finished_at: str
    duration_s: float
    host: str
    status: RunStatus
    error: NotRequired[str]  # why a "partial" run stopped
    config: dict[str, Any]  # the config snapshot the run used, settings filled in
    results: list[QueryRow]
    summary: NotRequired[Summary]
    recommendation: NotRequired[Recommendation]
    analysis_version: NotRequired[int]


class Coverage(TypedDict):
    runs: int  # how many of the combined runs measured the resolver
    of: int  # how many runs were combined
    last_run: str | None  # the newest of them that did


class Aggregate(TypedDict):
    """Several runs combined (``analysis.aggregate``). Not saved; recomputed on request."""

    kind: Literal["aggregate"]
    run_ids: list[str]  # newest first
    summary: Summary
    recommendation: Recommendation
    config: dict[str, Any]  # the newest run's config snapshot
    coverage: dict[str, Coverage]
    analysis_version: int


class ProgressEvent(TypedDict):
    type: Literal["result"]
    done: int
    total: int
    result: QueryRow


ProgressFn = Callable[[ProgressEvent], None]


class QueryFn(Protocol):
    """How the runner sends one query: ``resolver.query`` in real runs, a fake in tests. It returns a
    ``resolver.QueryResult`` or a mapping with the same fields."""

    def __call__(
        self, server: str, domain: str, *, record_type: str, timeout_s: float, tries: int
    ) -> Any: ...
