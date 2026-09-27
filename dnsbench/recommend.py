"""Turn a summary into a resolver recommendation, in three steps.

* ``rank`` scores every resolver that answered (``SCORE_FORMULA``; lower is better) and orders its
  servers.
* ``choose`` picks the best resolver, a backup from a different provider, the two IPs to configure,
  and the resolvers tied with the best.
* ``explain`` writes the one-paragraph summary and the notes. Each note is ``{code, params, text}``:
  the text is written here, so no client keeps its own copy of the wording.

Median rewards typical speed, p95 rewards consistency, mean catches everything in between, and
failures are penalised heavily because every failed lookup costs the user a full timeout before
their OS falls back to the secondary server. A query that only answered on a retry (tries > 1) cost
a full timeout too, so retries are penalised as well.

The same failure and retry penalties apply when picking the server to put first within a resolver
(median + penalties, p95 breaking ties), so an IP that drops queries is never suggested over a
reliable sibling. Servers that never answered are left out of their resolver's score (they are never
suggested) and get a note of their own.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .config import server_key
from .models import Coverage, LatencyStats, Note, RankEntry, Recommendation, Summary

W_MEDIAN, W_P95, W_MEAN = 0.5, 0.3, 0.2
FAILURE_WEIGHT = 2.0
RETRY_WEIGHT = 1.0
TIE_ABS_MS = 2.0
TIE_REL = 0.10
# Sibling servers of one provider (often anycast) usually differ by tenths of
# a millisecond; medians this close are noise, so config order decides.
SERVER_TIE_ABS_MS = 0.5
SERVER_TIE_REL = 0.05
SERVER_TIE_FAIL = 0.01  # failure-rate difference treated as noise between siblings
FAILURE_WARN_RATE = 0.02
LOW_SAMPLE = 30


# The formula is for people: it uses the multiplication sign, as the README does.
def _times(weight: float, term: str) -> str:
    return term if weight == 1 else f"{weight:g} × {term}"  # noqa: RUF001 - the multiplication sign, on purpose


# The one statement of the score, shown in the text report and checked against the README by a test.
SCORE_FORMULA = (
    f"score = {_times(W_MEDIAN, 'median')} + {_times(W_P95, 'p95')} + {_times(W_MEAN, 'mean')} "
    f"+ failure_rate × timeout_ms × {FAILURE_WEIGHT:g} "  # noqa: RUF001 - as above
    f"+ {_times(RETRY_WEIGHT, 'retry_rate × timeout_ms')}"  # noqa: RUF001 - as above
)


def score(stats: LatencyStats | None, timeout_ms: float) -> float | None:
    if not stats or not stats.get("ok"):
        return None
    median, p95, mean = stats["median"], stats["p95"], stats["mean"]
    if median is None or p95 is None or mean is None:  # pragma: no cover - set whenever ok > 0
        return None
    return (
        W_MEDIAN * median
        + W_P95 * p95
        + W_MEAN * mean
        + (stats.get("failure_rate") or 0.0) * timeout_ms * FAILURE_WEIGHT
        + (stats.get("retry_rate") or 0.0) * timeout_ms * RETRY_WEIGHT
    )


def _unreliability(st: LatencyStats) -> float:
    return (st.get("failure_rate") or 0.0) + (st.get("retry_rate") or 0.0)


def _server_cost(st: LatencyStats, timeout_ms: float) -> float:
    """Median plus the score's failure and retry penalties.

    Sibling IPs of one provider differ mainly in network RTT (the median); their
    p95/mean are dominated by uncached lookups and swing from run to run, so
    they only break ties here.
    """
    return (
        (st["median"] or 0.0)
        + (st.get("failure_rate") or 0.0) * timeout_ms * FAILURE_WEIGHT
        + (st.get("retry_rate") or 0.0) * timeout_ms * RETRY_WEIGHT
    )


def _servers_ranked(
    server_stats: Mapping[str, LatencyStats], timeout_ms: float
) -> tuple[list[str], list[str]]:
    """A resolver's answering servers, the one to put first at the front.

    Returns ``(ordered, tied)``. Servers are ordered by median plus the same
    failure and retry penalties as the score, so an IP that drops queries never
    beats a reliable sibling. Servers whose median is within max(0.5 ms, 5 %)
    of the leader's and that failed at most 1 point more often are within noise
    of it; among those a clearly lower p95 wins, otherwise config order does,
    so the advice doesn't flip between runs over 0.1 ms. ``tied`` lists the
    other servers that were within noise of the one put first.
    """
    usable = [
        (i, s, st, _server_cost(st, timeout_ms))
        for i, (s, st) in enumerate((server_stats or {}).items())
        if st.get("ok")
    ]
    if not usable:
        return [], []
    usable.sort(key=lambda u: (u[3], u[2]["p95"] or 0.0, u[0]))
    lead = usable[0][2]
    lead_median = lead["median"] or 0.0
    margin = max(SERVER_TIE_ABS_MS, SERVER_TIE_REL * lead_median)
    tie = [
        u
        for u in usable
        if abs((u[2]["median"] or 0.0) - lead_median) <= margin
        and _unreliability(u[2]) - _unreliability(lead) <= SERVER_TIE_FAIL
    ]
    min_p95 = min(u[2]["p95"] or 0.0 for u in tie)
    p95_margin = max(TIE_ABS_MS, TIE_REL * min_p95)
    tie = [u for u in tie if (u[2]["p95"] or 0.0) - min_p95 <= p95_margin]
    pick = min(tie, key=lambda u: u[0])  # config order among equals
    ordered = [pick[1]] + [u[1] for u in usable if u is not pick]
    tied = [u[1] for u in sorted(tie, key=lambda u: u[0]) if u is not pick]
    return ordered, tied


def _live_stats(st: LatencyStats, server_stats: Mapping[str, LatencyStats]) -> LatencyStats:
    """Resolver stats with servers that never answered left out of the counts.

    Such a server adds no latency samples and is never suggested, so its
    failures must not sink a resolver whose other server is fine (e.g. an
    IPv6 address on a network without an IPv6 route). Latency fields are
    unchanged, since dead servers contribute none.
    """
    live = [x for x in (server_stats or {}).values() if x.get("ok")]
    if not live or len(live) == len(server_stats):
        return st
    n = sum(x["n"] for x in live)
    failures = sum(x["failures"] for x in live)
    retried = sum(x.get("retried") or 0 for x in live)
    return {
        **st,
        "n": n,
        "ok": sum(x["ok"] for x in live),
        "failures": failures,
        "failure_rate": round(failures / n, 4) if n else 0.0,
        "timeouts": sum(x.get("timeouts") or 0 for x in live),
        "retried": retried,
        "retry_rate": round(retried / n, 4) if n else 0.0,
    }


def _key(ip: str) -> str:
    return server_key(ip) or ip


def _ms(x: float | None) -> str:
    return f"{x or 0.0:.1f} ms"


def _pct(rate: float) -> str:
    pct = rate * 100
    return f"{pct:.1f}%" if pct < 10 else f"{pct:.0f}%"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _note(code: str, text: str, **params: Any) -> Note:
    return {"code": code, "params": params, "text": text}


# --------------------------------------------------------------------------- #
# rank
# --------------------------------------------------------------------------- #


@dataclass
class Ranked:
    """One ranked resolver: its public ranking entry, plus what choose() and explain() need."""

    entry: RankEntry
    servers: list[str]  # its answering servers, the one to put first at the front
    tied_servers: list[str]  # other servers within noise of the one put first
    dead: list[str]  # servers that never answered
    eff: LatencyStats  # its stats without the dead servers
    srv: Mapping[str, LatencyStats]  # per-server stats
    keys: set[str]  # host keys of all its servers, to spot the same host under two names

    @property
    def name(self) -> str:
        return self.entry["resolver"]

    @property
    def fastest(self) -> str | None:
        return self.entry["fastest_server"]


def rank(summary: Summary, timeout_ms: float) -> tuple[list[Ranked], list[str]]:
    """Every resolver that answered, best first, and the names of those that never did."""
    by_res = summary.get("by_resolver") or {}
    by_srv = summary.get("by_server") or {}
    order = summary.get("resolvers") or list(by_res.keys())
    ranked: list[tuple[float, float, int, Ranked]] = []
    no_answers: list[str] = []
    for name in order:
        st = by_res.get(name)
        if not st:
            continue
        srv = by_srv.get(name) or {}
        eff = _live_stats(st, srv)
        sc = score(eff, timeout_ms)
        if sc is None:
            no_answers.append(name)
            continue
        ordered, tied_servers = _servers_ranked(srv, timeout_ms)
        entry: RankEntry = {
            "rank": 0,
            "resolver": name,
            "score": round(sc, 2),
            "median": st["median"] or 0.0,
            "p95": st["p95"] or 0.0,
            "mean": st["mean"] or 0.0,
            "failure_rate": st["failure_rate"],
            "retry_rate": st.get("retry_rate") or 0.0,
            "ok": st["ok"],
            "n": st["n"],
            "fastest_server": ordered[0] if ordered else None,
        }
        item = Ranked(
            entry=entry,
            servers=ordered,
            tied_servers=tied_servers,
            dead=[s for s, x in srv.items() if not x.get("ok")],
            eff=eff,
            srv=srv,
            keys={_key(s) for s in srv},
        )
        ranked.append((entry["score"], entry["median"], len(ranked), item))
    ranked.sort(key=lambda r: r[:3])
    out = [r[3] for r in ranked]
    for i, r in enumerate(out, 1):
        r.entry["rank"] = i
    return out, no_answers


# --------------------------------------------------------------------------- #
# choose
# --------------------------------------------------------------------------- #


@dataclass
class Choice:
    best: Ranked
    backup: Ranked | None  # the best resolver of a different provider
    eligible: list[Ranked]  # may be recommended (in the current config), best first
    stale: list[Ranked]  # ranked, but not in the current config
    aliases: list[Ranked]  # other names for best's servers
    tied_with: list[str]
    tie_margin: float
    primary_ip: str | None
    secondary_ip: str | None
    secondary_txt: str | None  # how the summary names the secondary: "8.8.8.8 (Google)"
    suggested: list[str]


def choose(ranking: list[Ranked], current: Iterable[str] | None = None) -> Choice:
    """Best, backup and the servers to configure, from a non-empty ranking.

    ``current`` (aggregates only) names the resolvers that may be recommended; the others are ranked
    but never become best, backup or a suggested server. None means every resolver may be.
    """
    # In "All runs combined", a resolver seen only in older runs (since disabled,
    # removed or renamed) is ranked but not recommended: its numbers come from
    # other times than everyone else's, and the user no longer runs it.
    eligible, stale = ranking, []
    if current is not None:
        cur = set(current)
        in_cur = [e for e in ranking if e.name in cur]
        if in_cur:
            eligible = in_cur
            stale = [e for e in ranking if e.name not in cur]
    best = eligible[0]
    # Resolvers sharing a server with best are the same host(s) under another
    # name (e.g. renamed between runs in "All runs combined"): no redundancy.
    aliases = [e for e in ranking if e is not best and e.keys & best.keys]
    backup = next((e for e in eligible[1:] if not (e.keys & best.keys)), None)
    tie_margin = max(TIE_ABS_MS, TIE_REL * best.entry["score"])
    alias_names = {e.name for e in aliases}
    tied_with = [
        e.name
        for e in eligible[1:]
        if e.entry["score"] - best.entry["score"] <= tie_margin and e.name not in alias_names
    ]
    primary_ip = best.fastest
    secondary_ip = None
    secondary_txt = None
    if backup is not None and backup.fastest:
        secondary_ip = backup.fastest
        secondary_txt = f"{secondary_ip} ({backup.name})"
    if secondary_ip is None or (primary_ip and _key(secondary_ip) == _key(primary_ip)):
        others = [s for s in best.servers[1:] if not primary_ip or _key(s) != _key(primary_ip)]
        secondary_ip = others[0] if others else None
        secondary_txt = secondary_ip
    suggested = [ip for ip in (primary_ip, secondary_ip) if ip]
    return Choice(
        best,
        backup,
        eligible,
        stale,
        aliases,
        tied_with,
        tie_margin,
        primary_ip,
        secondary_ip,
        secondary_txt,
        suggested,
    )


# --------------------------------------------------------------------------- #
# explain
# --------------------------------------------------------------------------- #


def _summary_text(c: Choice, ranking: list[Ranked], tested: int) -> str:
    best = c.best
    b = best.entry
    lowest_median = all(b["median"] <= e.entry["median"] for e in c.eligible)
    lowest_p95 = all(b["p95"] <= e.entry["p95"] for e in c.eligible)
    # Say "of the current resolvers" only when the claim wouldn't hold over the
    # whole ranking (a resolver that is not recommended did better).
    beaten_median = any(e.entry["median"] < b["median"] for e in c.stale)
    beaten_p95 = any(e.entry["p95"] < b["p95"] for e in c.stale)
    tag = " of the current resolvers"
    if len(c.eligible) == 1:
        if c.stale:
            who = "the only current resolver that answered"
        else:
            who = "the only resolver tested" if tested == 1 else "the only resolver that answered"
        why = f"{best.name} was {who} (median {_ms(b['median'])}, p95 {_ms(b['p95'])})"
    elif lowest_median and lowest_p95:
        among = tag if beaten_median or beaten_p95 else ""
        why = f"{best.name} had the lowest median ({_ms(b['median'])}) and p95 ({_ms(b['p95'])}){among}"
    elif lowest_median:
        among = tag if beaten_median else ""
        why = f"{best.name} had the lowest median ({_ms(b['median'])}){among}, p95 {_ms(b['p95'])}"
    else:
        among = tag if best is not ranking[0] else ""
        why = (
            f"{best.name} had the best overall score{among} (median {_ms(b['median'])}, p95 {_ms(b['p95'])})"
        )
    eff = best.eff
    fails, n_eff, retried = eff["n"] - eff["ok"], eff["n"], eff.get("retried") or 0
    if fails == 0 and retried == 0:
        why += " with no failures"
    elif fails == 0:
        why += f" with no failures, though {retried} of {n_eff} queries needed a retry"
    else:
        why += f" with {_pct(eff['failure_rate'])} failures ({fails} of {n_eff})"
        if retried:
            why += f"; {retried} needed a retry"
    if best.dead:
        why += f" (not counting {_join(best.dead)}, which never answered)"
    why += "."
    if not c.primary_ip:
        first = f"Use {best.name}."
    elif c.secondary_txt:
        first = f"Use {best.name}: put {c.primary_ip} first and {c.secondary_txt} second."
    else:
        first = f"Use {best.name}: put {c.primary_ip} first."
    return f"{first} {why}"


def _reliability_notes(ranking: list[Ranked], timeout_ms: float) -> list[Note]:
    notes: list[Note] = []
    for e in ranking:
        for ip in e.dead:
            x = e.srv[ip]
            notes.append(
                _note(
                    "server_never_answered",
                    f"{e.name}: server {ip} never answered ({x['n'] - x['ok']} of {x['n']} "
                    "queries failed) — it is unreachable or blocked from this network, so it "
                    "was left out of the score; don't configure it.",
                    resolver=e.name,
                    server=ip,
                    failed=x["n"] - x["ok"],
                    n=x["n"],
                )
            )
    for e in ranking:
        eff = e.eff
        if eff["failure_rate"] > FAILURE_WARN_RATE:
            if eff.get("timeouts"):
                text = (
                    f"{e.name}: {_pct(eff['failure_rate'])} of queries failed "
                    f"(timeouts/errors) — each timeout costs a full {timeout_ms:.0f} ms "
                    "before your device falls back."
                )
            else:
                text = (
                    f"{e.name}: {_pct(eff['failure_rate'])} of queries failed "
                    "(error answers such as SERVFAIL/REFUSED, or network errors)."
                )
            notes.append(
                _note(
                    "failure_rate",
                    text,
                    resolver=e.name,
                    rate=eff["failure_rate"],
                    timeouts=bool(eff.get("timeouts")),
                    timeout_ms=timeout_ms,
                )
            )
        if (eff.get("retry_rate") or 0.0) > FAILURE_WARN_RATE:
            notes.append(
                _note(
                    "retry_rate",
                    f"{e.name}: {_pct(eff['retry_rate'])} of queries needed a retry — "
                    f"the first attempt timed out, which costs a full {timeout_ms:.0f} ms "
                    "even though the retry answered.",
                    resolver=e.name,
                    rate=eff["retry_rate"],
                    timeout_ms=timeout_ms,
                )
            )
        chosen = e.fastest
        if chosen and len(e.servers) > 1:
            cst = e.srv[chosen]
            for ip in e.servers[1:]:
                x = e.srv[ip]
                if _unreliability(x) - _unreliability(cst) <= SERVER_TIE_FAIL:
                    continue
                if (x.get("failure_rate") or 0.0) > FAILURE_WARN_RATE:
                    what, rate, chosen_rate = "failed", x["failure_rate"], cst.get("failure_rate") or 0.0
                elif (x.get("retry_rate") or 0.0) > FAILURE_WARN_RATE:
                    what, rate, chosen_rate = (
                        "needed a retry for",
                        x["retry_rate"],
                        cst.get("retry_rate") or 0.0,
                    )
                else:
                    continue
                notes.append(
                    _note(
                        "prefer_server",
                        f"{e.name}: {ip} {what} {_pct(rate)} of its queries "
                        f"({chosen}: {_pct(chosen_rate)}) — prefer {chosen}.",
                        resolver=e.name,
                        server=ip,
                        rate=rate,
                        prefer=chosen,
                        prefer_rate=chosen_rate,
                        kind="failures" if what == "failed" else "retries",
                    )
                )
    return notes


def _coverage_notes(c: Choice, coverage: Mapping[str, Coverage]) -> list[Note]:
    notes: list[Note] = []
    for e in c.stale:
        cv = coverage.get(e.name)
        if cv and cv.get("runs") and cv.get("of"):
            seen = f"was measured in only {cv['runs']} of {cv['of']} combined runs"
        else:
            seen = "was not measured in the newest run"
        notes.append(
            _note(
                "not_current",
                f"{e.name} {seen} and is not enabled in the current config, so it is "
                "ranked but not recommended.",
                resolver=e.name,
                runs=cv["runs"] if cv else None,
                of=cv["of"] if cv else None,
            )
        )
    for e in c.eligible:
        cv = coverage.get(e.name)
        if cv and cv.get("runs") and cv.get("of") and cv["runs"] < cv["of"]:
            notes.append(
                _note(
                    "partial_coverage",
                    f"{e.name} was measured in only {cv['runs']} of {cv['of']} combined "
                    "runs, so its numbers are less comparable with the others'.",
                    resolver=e.name,
                    runs=cv["runs"],
                    of=cv["of"],
                )
            )
    return notes


def _low_sample_note(ranking: list[Ranked]) -> list[Note]:
    low = [e for e in ranking if e.entry["ok"] < LOW_SAMPLE]
    if not low:  # one note, not one per resolver (a short run makes them all low)
        return []
    if len(low) == 1:
        what = f"{low[0].name}: only {low[0].entry['ok']} successful samples"
    elif len({e.entry["ok"] for e in low}) == 1:
        what = f"{_join([e.name for e in low])}: only {low[0].entry['ok']} successful samples each"
    else:
        counts = [f"{e.name} ({e.entry['ok']})" for e in low]
        what = f"{_join(counts)}: fewer than {LOW_SAMPLE} successful samples each"
    return [
        _note(
            "low_samples",
            f"{what} — low sample size, run more rounds for a steadier answer.",
            samples={e.name: e.entry["ok"] for e in low},
            minimum=LOW_SAMPLE,
        )
    ]


def _choice_notes(c: Choice, tested: int) -> list[Note]:
    notes: list[Note] = []
    best = c.best
    if c.tied_with:
        notes.append(
            _note(
                "tie",
                f"{best.name} is within noise of {', '.join(c.tied_with)} (score within "
                f"{c.tie_margin:.1f} ms) — any of them is a good choice.",
                resolver=best.name,
                tied_with=c.tied_with,
                margin_ms=round(c.tie_margin, 2),
            )
        )
    for pick, role in ((best, "go first"), (c.backup, "be the secondary")):
        if pick is None or not pick.tied_servers or not pick.fastest:
            continue
        if pick is c.backup and pick.fastest != c.secondary_ip:
            continue
        ips = [pick.fastest, *pick.tied_servers]
        medians = " vs ".join(f"{pick.srv[ip]['median'] or 0.0:.1f}" for ip in ips)
        either = "either" if len(ips) == 2 else "any of them"
        notes.append(
            _note(
                "servers_tied",
                f"{pick.name}'s servers {_join(ips)} were within noise of each other "
                f"(median {medians} ms) — {either} can {role}.",
                resolver=pick.name,
                servers=ips,
                role="primary" if pick is best else "secondary",
            )
        )
    if c.aliases:
        notes.append(
            _note(
                "aliases",
                f"{best.name} and {_join([e.name for e in c.aliases])} share server "
                "IPs — probably the same resolver under an older name in combined runs.",
                resolver=best.name,
                aliases=[e.name for e in c.aliases],
            )
        )
    if c.backup is None:
        if len(c.eligible) > 1:
            reason = "shared_servers"
            current = "current " if c.stale else ""
            prefix = f"Every other {current}resolver that answered shares servers with {best.name}"
            add = "add a different provider for redundancy."
        elif c.stale:
            reason, prefix, add = (
                "no_other_current",
                "No other current resolver answered",
                ("add another resolver for redundancy."),
            )
        elif tested == 1:
            reason, prefix, add = (
                "one_tested",
                "Only one resolver was tested",
                ("benchmark another resolver to compare and for redundancy."),
            )
        else:
            reason, prefix, add = (
                "one_answered",
                "Only one resolver answered",
                "add another resolver for redundancy.",
            )
        if len(c.suggested) >= 2:
            text = f"{prefix}, so both suggested servers are from the same provider; {add}"
        elif c.primary_ip:
            only = (
                "is its only server" if len(best.srv) <= 1 else "is the only one of its servers that answered"
            )
            text = (
                f"{prefix}, and {c.primary_ip} {only}, so there is no secondary server to "
                "suggest; add a second server or another resolver for redundancy."
            )
        else:
            text = f"{prefix}; {add}"
        notes.append(_note("no_backup", text, reason=reason, suggested=len(c.suggested)))
    return notes


def _closing_note(n_runs: int | None) -> Note:
    if n_runs is not None and n_runs >= 2:
        return _note(
            "combined_runs",
            f"Combined from {n_runs} runs. Results still reflect this network at the times "
            "those runs were taken; adding runs at other times of day gives a steadier answer.",
            runs=n_runs,
        )
    return _note(
        "one_time",
        "Results reflect this network at this time of day; combining several runs "
        '(UI "All runs combined" or `dns-bench report all`) gives a steadier answer.',
    )


def explain(
    ranking: list[Ranked],
    no_answers: list[str],
    choice: Choice,
    *,
    timeout_ms: float,
    n_runs: int | None = None,
    coverage: Mapping[str, Coverage] | None = None,
) -> tuple[str, list[Note]]:
    """The summary paragraph and the notes, for a ranking with at least one resolver."""
    tested = len(ranking) + len(no_answers)
    notes = [_no_answers_note(name) for name in no_answers]
    notes += _reliability_notes(ranking, timeout_ms)
    notes += _coverage_notes(choice, coverage or {})
    notes += _low_sample_note(ranking)
    notes += _choice_notes(choice, tested)
    notes.append(_closing_note(n_runs))
    return _summary_text(choice, ranking, tested), notes


def _no_answers_note(name: str) -> Note:
    return _note(
        "no_answers",
        f"{name} returned no successful answers at all — it is unreachable or blocked from this network.",
        resolver=name,
    )


def recommend(
    summary: Summary,
    settings: Mapping[str, Any] | None = None,
    *,
    n_runs: int | None = None,
    coverage: Mapping[str, Coverage] | None = None,
    current: Iterable[str] | None = None,
) -> Recommendation:
    """Rank resolvers and write the recommendation (``rank``, then ``choose``, then ``explain``).

    ``n_runs`` is the number of runs the summary combines (aggregates only);
    with 2 or more the closing note doesn't suggest combining runs again.
    ``coverage`` ({name: {"runs", "of", "last_run"}}, aggregates only) adds a
    note for resolvers measured in only some of the combined runs.
    ``current`` (aggregates only) names the resolvers that may be recommended;
    the others are ranked but never become best, backup or a suggested server.
    None means every resolver may be recommended.
    """
    timeout_ms = float((settings or {}).get("timeout_ms", 1000))
    ranking, no_answers = rank(summary, timeout_ms)
    if not ranking:
        return {
            "best": None,
            "backup": None,
            "tied_with": [],
            "suggested_servers": [],
            "ranking": [],
            "summary": "No resolver returned any successful answers, so there is nothing to "
            "recommend. Check your network connection or firewall (outbound UDP port 53).",
            "notes": [
                *(_no_answers_note(name) for name in no_answers),
                _note(
                    "check_network",
                    "Results reflect this network at this time; check your connection "
                    "(UDP port 53 must be allowed) and run again.",
                ),
            ],
        }
    choice = choose(ranking, current)
    text, notes = explain(
        ranking, no_answers, choice, timeout_ms=timeout_ms, n_runs=n_runs, coverage=coverage
    )
    return {
        "best": choice.best.name,
        "backup": choice.backup.name if choice.backup else None,
        "tied_with": choice.tied_with,
        "suggested_servers": choice.suggested,
        "ranking": [r.entry for r in ranking],
        "summary": text,
        "notes": notes,
    }
