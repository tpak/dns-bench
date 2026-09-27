"""Turn a summary into a plain-English resolver recommendation.

score = 0.5*median + 0.3*p95 + 0.2*mean
        + failure_rate * timeout_ms * 2 + retry_rate * timeout_ms

Lower is better. Median rewards typical speed, p95 rewards consistency, mean
catches everything in between, and failures are penalised heavily because
every failed lookup costs the user a full timeout before their OS falls back
to the secondary server. A query that only answered on a retry (tries > 1)
cost a full timeout too, so retries are penalised as well.

The same failure and retry penalties apply when picking the server to put first
within a resolver (median + penalties, p95 breaking ties), so an IP that drops
queries is never suggested over a reliable sibling. Servers that never
answered are left out of their resolver's score (they are never suggested)
and get a note of their own.
"""

from __future__ import annotations

from .config import server_key

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


def score(stats: dict, timeout_ms: float) -> float | None:
    if not stats or not stats.get("ok"):
        return None
    return (
        W_MEDIAN * stats["median"]
        + W_P95 * stats["p95"]
        + W_MEAN * stats["mean"]
        + (stats.get("failure_rate") or 0.0) * timeout_ms * FAILURE_WEIGHT
        + (stats.get("retry_rate") or 0.0) * timeout_ms * RETRY_WEIGHT
    )


def _unreliability(st: dict) -> float:
    return (st.get("failure_rate") or 0.0) + (st.get("retry_rate") or 0.0)


def _server_cost(st: dict, timeout_ms: float) -> float:
    """Median plus the score's failure and retry penalties.

    Sibling IPs of one provider differ mainly in network RTT (the median); their
    p95/mean are dominated by uncached lookups and swing from run to run, so
    they only break ties here.
    """
    return (
        st["median"]
        + (st.get("failure_rate") or 0.0) * timeout_ms * FAILURE_WEIGHT
        + (st.get("retry_rate") or 0.0) * timeout_ms * RETRY_WEIGHT
    )


def _servers_ranked(server_stats: dict, timeout_ms: float) -> tuple[list[str], list[str]]:
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
    usable.sort(key=lambda u: (u[3], u[2]["p95"], u[0]))
    lead = usable[0][2]
    margin = max(SERVER_TIE_ABS_MS, SERVER_TIE_REL * lead["median"])
    tie = [
        u
        for u in usable
        if abs(u[2]["median"] - lead["median"]) <= margin
        and _unreliability(u[2]) - _unreliability(lead) <= SERVER_TIE_FAIL
    ]
    min_p95 = min(u[2]["p95"] for u in tie)
    p95_margin = max(TIE_ABS_MS, TIE_REL * min_p95)
    tie = [u for u in tie if u[2]["p95"] - min_p95 <= p95_margin]
    pick = min(tie, key=lambda u: u[0])  # config order among equals
    ordered = [pick[1]] + [u[1] for u in usable if u is not pick]
    tied = [u[1] for u in sorted(tie, key=lambda u: u[0]) if u is not pick]
    return ordered, tied


def _live_stats(st: dict, server_stats: dict) -> dict:
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


def _ms(x) -> str:
    return f"{x:.1f} ms"


def _pct(rate: float) -> str:
    pct = rate * 100
    return f"{pct:.1f}%" if pct < 10 else f"{pct:.0f}%"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def recommend(
    summary: dict,
    settings: dict | None = None,
    *,
    n_runs: int | None = None,
    coverage: dict | None = None,
    current=None,
) -> dict:
    """Rank resolvers and write the recommendation.

    ``n_runs`` is the number of runs the summary combines (aggregates only);
    with 2 or more the closing note doesn't suggest combining runs again.
    ``coverage`` ({name: {"runs", "of", "last_run"}}, aggregates only) adds a
    note for resolvers measured in only some of the combined runs.
    ``current`` (aggregates only) names the resolvers that may be recommended;
    the others are ranked but never become best, backup or a suggested server.
    None means every resolver may be recommended.
    """
    settings = settings or {}
    timeout_ms = float(settings.get("timeout_ms", 1000))
    by_res = summary.get("by_resolver") or {}
    by_srv = summary.get("by_server") or {}
    order = summary.get("resolvers") or list(by_res.keys())

    ranking: list[dict] = []
    no_answers = []
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
        ranking.append(
            {
                "rank": 0,
                "resolver": name,
                "score": round(sc, 2),
                "median": st["median"],
                "p95": st["p95"],
                "mean": st["mean"],
                "failure_rate": st["failure_rate"],
                "retry_rate": st.get("retry_rate") or 0.0,
                "ok": st["ok"],
                "n": st["n"],
                "fastest_server": ordered[0] if ordered else None,
                "_servers": ordered,
                "_tied_servers": tied_servers,
                "_dead": [s for s, x in srv.items() if not x.get("ok")],
                "_eff": eff,
                "_srv": srv,
                "_keys": {_key(s) for s in srv},
                "_order": len(ranking),
            }
        )
    ranking.sort(key=lambda e: (e["score"], e["median"], e["_order"]))
    for i, e in enumerate(ranking, 1):
        e["rank"] = i

    notes: list[str] = []
    for name in no_answers:
        notes.append(
            f"{name} returned no successful answers at all — it is unreachable or blocked from this network."
        )

    if not ranking:
        notes.append(
            "Results reflect this network at this time; check your connection "
            "(UDP port 53 must be allowed) and run again."
        )
        return {
            "best": None,
            "backup": None,
            "tied_with": [],
            "suggested_servers": [],
            "ranking": [],
            "summary": "No resolver returned any successful answers, so there is nothing to "
            "recommend. Check your network connection or firewall (outbound UDP port 53).",
            "notes": notes,
        }

    # In "All runs combined", a resolver seen only in older runs (since disabled,
    # removed or renamed) is ranked but not recommended: its numbers come from
    # other times than everyone else's, and the user no longer runs it.
    eligible, stale = ranking, []
    if current is not None:
        cur = set(current)
        in_cur = [e for e in ranking if e["resolver"] in cur]
        if in_cur:
            eligible = in_cur
            stale = [e for e in ranking if e["resolver"] not in cur]
    best = eligible[0]
    tested = len(ranking) + len(no_answers)
    # Resolvers sharing a server with best are the same host(s) under another
    # name (e.g. renamed between runs in "All runs combined"): no redundancy.
    aliases = [e for e in ranking if e is not best and e["_keys"] & best["_keys"]]
    backup = next((e for e in eligible[1:] if not (e["_keys"] & best["_keys"])), None)
    tie_margin = max(TIE_ABS_MS, TIE_REL * best["score"])
    alias_names = {e["resolver"] for e in aliases}
    tied_with = [
        e["resolver"]
        for e in eligible[1:]
        if e["score"] - best["score"] <= tie_margin and e["resolver"] not in alias_names
    ]

    primary_ip = best["fastest_server"]
    secondary_ip = None
    secondary_txt = None
    if backup is not None and backup["fastest_server"]:
        secondary_ip = backup["fastest_server"]
        secondary_txt = f"{secondary_ip} ({backup['resolver']})"
    if secondary_ip is None or (primary_ip and _key(secondary_ip) == _key(primary_ip)):
        others = [s for s in best["_servers"][1:] if not primary_ip or _key(s) != _key(primary_ip)]
        secondary_ip = others[0] if others else None
        secondary_txt = secondary_ip
    suggested = [ip for ip in (primary_ip, secondary_ip) if ip]

    # -- summary sentence ----------------------------------------------------
    lowest_median = all(best["median"] <= e["median"] for e in eligible)
    lowest_p95 = all(best["p95"] <= e["p95"] for e in eligible)
    # Say "of the current resolvers" only when the claim wouldn't hold over the
    # whole ranking (a resolver that is not recommended did better).
    beaten_median = any(e["median"] < best["median"] for e in stale)
    beaten_p95 = any(e["p95"] < best["p95"] for e in stale)
    tag = " of the current resolvers"
    if len(eligible) == 1:
        if stale:
            who = "the only current resolver that answered"
        else:
            who = "the only resolver tested" if tested == 1 else "the only resolver that answered"
        why = f"{best['resolver']} was {who} (median {_ms(best['median'])}, p95 {_ms(best['p95'])})"
    elif lowest_median and lowest_p95:
        among = tag if beaten_median or beaten_p95 else ""
        why = (
            f"{best['resolver']} had the lowest median ({_ms(best['median'])}) "
            f"and p95 ({_ms(best['p95'])}){among}"
        )
    elif lowest_median:
        among = tag if beaten_median else ""
        why = (
            f"{best['resolver']} had the lowest median ({_ms(best['median'])}){among}, p95 {_ms(best['p95'])}"
        )
    else:
        among = tag if best is not ranking[0] else ""
        why = (
            f"{best['resolver']} had the best overall score{among} "
            f"(median {_ms(best['median'])}, p95 {_ms(best['p95'])})"
        )
    eff = best["_eff"]
    fails, n_eff, retried = eff["n"] - eff["ok"], eff["n"], eff.get("retried") or 0
    if fails == 0 and retried == 0:
        why += " with no failures"
    elif fails == 0:
        why += f" with no failures, though {retried} of {n_eff} queries needed a retry"
    else:
        why += f" with {_pct(eff['failure_rate'])} failures ({fails} of {n_eff})"
        if retried:
            why += f"; {retried} needed a retry"
    if best["_dead"]:
        why += f" (not counting {_join(best['_dead'])}, which never answered)"
    why += "."
    if not primary_ip:
        first = f"Use {best['resolver']}."
    elif secondary_txt:
        first = f"Use {best['resolver']}: put {primary_ip} first and {secondary_txt} second."
    else:
        first = f"Use {best['resolver']}: put {primary_ip} first."
    summary_text = f"{first} {why}"

    # -- notes ---------------------------------------------------------------
    for e in ranking:
        for ip in e["_dead"]:
            x = e["_srv"][ip]
            notes.append(
                f"{e['resolver']}: server {ip} never answered ({x['n'] - x['ok']} of {x['n']} "
                "queries failed) — it is unreachable or blocked from this network, so it "
                "was left out of the score; don't configure it."
            )
    for e in ranking:
        eff = e["_eff"]
        if eff["failure_rate"] > FAILURE_WARN_RATE:
            if eff.get("timeouts"):
                notes.append(
                    f"{e['resolver']}: {_pct(eff['failure_rate'])} of queries failed "
                    f"(timeouts/errors) — each timeout costs a full {timeout_ms:.0f} ms "
                    "before your device falls back."
                )
            else:
                notes.append(
                    f"{e['resolver']}: {_pct(eff['failure_rate'])} of queries failed "
                    "(error answers such as SERVFAIL/REFUSED, or network errors)."
                )
        if (eff.get("retry_rate") or 0.0) > FAILURE_WARN_RATE:
            notes.append(
                f"{e['resolver']}: {_pct(eff['retry_rate'])} of queries needed a retry — "
                f"the first attempt timed out, which costs a full {timeout_ms:.0f} ms "
                "even though the retry answered."
            )
        chosen = e["fastest_server"]
        if chosen and len(e["_servers"]) > 1:
            cst = e["_srv"][chosen]
            for ip in e["_servers"][1:]:
                x = e["_srv"][ip]
                if _unreliability(x) - _unreliability(cst) <= SERVER_TIE_FAIL:
                    continue
                if (x.get("failure_rate") or 0.0) > FAILURE_WARN_RATE:
                    what, key = "failed", "failure_rate"
                elif (x.get("retry_rate") or 0.0) > FAILURE_WARN_RATE:
                    what, key = "needed a retry for", "retry_rate"
                else:
                    continue
                notes.append(
                    f"{e['resolver']}: {ip} {what} {_pct(x[key])} of its queries "
                    f"({chosen}: {_pct(cst.get(key) or 0.0)}) — prefer {chosen}."
                )
    cov = coverage or {}
    for e in stale:
        c = cov.get(e["resolver"]) or {}
        seen = (
            f"was measured in only {c['runs']} of {c['of']} combined runs"
            if c.get("runs") and c.get("of")
            else "was not measured in the newest run"
        )
        notes.append(
            f"{e['resolver']} {seen} and is not enabled in the current config, so it is "
            "ranked but not recommended."
        )
    for e in eligible:
        c = cov.get(e["resolver"]) or {}
        if c.get("runs") and c.get("of") and c["runs"] < c["of"]:
            notes.append(
                f"{e['resolver']} was measured in only {c['runs']} of {c['of']} combined "
                "runs, so its numbers are less comparable with the others'."
            )
    low = [e for e in ranking if e["ok"] < LOW_SAMPLE]
    if low:  # one note, not one per resolver (a short run makes them all low)
        if len(low) == 1:
            what = f"{low[0]['resolver']}: only {low[0]['ok']} successful samples"
        elif len({e["ok"] for e in low}) == 1:
            what = f"{_join([e['resolver'] for e in low])}: only {low[0]['ok']} successful samples each"
        else:
            counts = [f"{e['resolver']} ({e['ok']})" for e in low]
            what = f"{_join(counts)}: fewer than {LOW_SAMPLE} successful samples each"
        notes.append(f"{what} — low sample size, run more rounds for a steadier answer.")
    if tied_with:
        names = ", ".join(tied_with)
        notes.append(
            f"{best['resolver']} is within noise of {names} (score within "
            f"{tie_margin:.1f} ms) — any of them is a good choice."
        )
    for pick, role in ((best, "go first"), (backup, "be the secondary")):
        if pick is None or not pick["_tied_servers"] or not pick["fastest_server"]:
            continue
        if pick is backup and pick["fastest_server"] != secondary_ip:
            continue
        ips = [pick["fastest_server"]] + pick["_tied_servers"]
        medians = " vs ".join(f"{pick['_srv'][ip]['median']:.1f}" for ip in ips)
        either = "either" if len(ips) == 2 else "any of them"
        notes.append(
            f"{pick['resolver']}'s servers {_join(ips)} were within noise of each other "
            f"(median {medians} ms) — {either} can {role}."
        )
    if aliases:
        notes.append(
            f"{best['resolver']} and {_join([e['resolver'] for e in aliases])} share server "
            "IPs — probably the same resolver under an older name in combined runs."
        )
    if backup is None:
        if len(eligible) > 1:
            prefix = (
                f"Every other {'current ' if stale else ''}resolver that answered shares "
                f"servers with {best['resolver']}"
            )
            add = "add a different provider for redundancy."
        elif stale:
            prefix = "No other current resolver answered"
            add = "add another resolver for redundancy."
        elif tested == 1:
            prefix = "Only one resolver was tested"
            add = "benchmark another resolver to compare and for redundancy."
        else:
            prefix = "Only one resolver answered"
            add = "add another resolver for redundancy."
        if len(suggested) >= 2:
            notes.append(f"{prefix}, so both suggested servers are from the same provider; {add}")
        elif primary_ip:
            only = (
                "is its only server"
                if len(best["_srv"]) <= 1
                else "is the only one of its servers that answered"
            )
            notes.append(
                f"{prefix}, and {primary_ip} {only}, so there is no secondary server to "
                "suggest; add a second server or another resolver for redundancy."
            )
        else:
            notes.append(f"{prefix}; {add}")
    if n_runs is not None and n_runs >= 2:
        notes.append(
            f"Combined from {n_runs} runs. Results still reflect this network at the times "
            "those runs were taken; adding runs at other times of day gives a steadier answer."
        )
    else:
        notes.append(
            "Results reflect this network at this time of day; combining several runs "
            '(UI "All runs combined" or `dns-bench report all`) gives a steadier answer.'
        )

    for e in ranking:
        for k in ("_servers", "_tied_servers", "_dead", "_eff", "_srv", "_keys", "_order"):
            e.pop(k, None)
    return {
        "best": best["resolver"],
        "backup": backup["resolver"] if backup else None,
        "tied_with": tied_with,
        "suggested_servers": suggested,
        "ranking": ranking,
        "summary": summary_text,
        "notes": notes,
    }
