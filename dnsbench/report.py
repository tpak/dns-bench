"""Plain-text report rendering (CLI output and the saved <id>.txt)."""

from __future__ import annotations

from datetime import UTC, datetime

from .config import DEFAULT_SETTINGS


def printable(text: str) -> str:
    """``text`` with non-printable characters (ESC, BEL, bidi overrides, ...)
    shown as escapes, so a hand-edited or pre-validation run file can't send
    control sequences to the terminal. Newlines are kept."""
    if text.isprintable():
        return text
    return "\n".join(
        "".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in line)
        for line in text.split("\n")
    )


def _f(x, nd=1) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def _pct(rate) -> str:
    return "-" if rate is None else f"{rate * 100:.1f}%"


def _local_time(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return iso
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def stats_line(name: str, st: dict) -> str:
    """One line per resolver, in the style of the original script."""
    line = (
        f"{name}: mean={_f(st.get('mean'))} median={_f(st.get('median'))} "
        f"p80={_f(st.get('p80'))} p95={_f(st.get('p95'))} p98={_f(st.get('p98'))} "
        f"min={_f(st.get('min'))} max={_f(st.get('max'))} n={st.get('n', 0)} "
        f"fail={_pct(st.get('failure_rate'))}"
    )
    if st.get("retried"):  # only with tries > 1: queries whose first attempt timed out
        line += f" retried={st['retried']}"
    return line


def _table(headers: list[str], rows: list[list[str]], right: set[int]) -> list[str]:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]

    def fmt(cells):
        return "  ".join(
            c.rjust(w) if i in right else c.ljust(w)
            for i, (c, w) in enumerate(zip(cells, widths, strict=True))
        ).rstrip()

    out = [fmt(headers), fmt(["-" * w for w in widths])]
    out += [fmt(r) for r in rows]
    return out


def render_text(bundle: dict) -> str:
    """Render a run record, or an aggregate bundle {run_ids, summary, recommendation, config}."""
    summary = bundle.get("summary") or {}
    rec = bundle.get("recommendation") or {}
    config = bundle.get("config") or {}
    settings = {**DEFAULT_SETTINGS, **(config.get("settings") or {})}
    overall = summary.get("overall") or {}
    lines: list[str] = []

    if "run_ids" in bundle:
        ids = bundle.get("run_ids") or []
        lines.append(f"DNS Bench — all runs combined ({len(ids)} run{'s' if len(ids) != 1 else ''})")
        if ids:
            lines.append(f"Runs:      {ids[-1]} … {ids[0]}" if len(ids) > 1 else f"Run:       {ids[0]}")
    else:
        status = bundle.get("status", "complete")
        lines.append(
            f"DNS Bench run {bundle.get('id', '?')}"
            + ("  [CANCELLED — partial results]" if status == "cancelled" else "")
        )
        lines.append(f"Started:   {_local_time(bundle.get('started_at'))}")
        lines.append(f"Duration:  {_f(bundle.get('duration_s'))} s on {bundle.get('host', '?')}")
    lines.append(
        f"Queries:   {overall.get('n', 0)} ({overall.get('ok', 0)} ok, "
        f"{overall.get('failures', 0)} failed) across {len(summary.get('domains') or [])} domains"
    )
    lines.append(
        f"Pacing:    one query at a time per server, ≥{settings['per_server_interval_ms']} ms apart "
        f"(≤{1000 / max(1, settings['per_server_interval_ms']):.1f} q/s per server), "
        f"timeout {settings['timeout_ms']} ms"
    )
    lines.append("")

    by_res = summary.get("by_resolver") or {}
    by_srv = summary.get("by_server") or {}
    names = summary.get("resolvers") or list(by_res)
    if not names:
        lines.append("No results.")
    for name in names:
        lines.append(stats_line(name, by_res.get(name, {})))
    srv_rows = [
        [
            name,
            server,
            _f(st.get("median")),
            _f(st.get("p95")),
            _pct(st.get("failure_rate")),
            str(st.get("retried") or 0),
        ]
        for name in names
        for server, st in (by_srv.get(name) or {}).items()
    ]
    if srv_rows:
        headers = ["Resolver", "Server", "Median", "p95", "Fail", "Retried"]
        if not any(r[5] != "0" for r in srv_rows):  # tries == 1: nothing is ever retried
            headers.pop()
            srv_rows = [r[:5] for r in srv_rows]
        lines.append("")
        lines.append("Per server (ms):")
        lines += ["  " + ln for ln in _table(headers, srv_rows, right={2, 3, 4, 5})]

    ranking = rec.get("ranking") or []
    if ranking:
        lines.append("")
        lines.append(
            "Ranking (score = 0.5·median + 0.3·p95 + 0.2·mean + failures·timeout·2 "
            "+ retries·timeout; lower is better):"
        )
        rows = [
            [
                str(e["rank"]),
                e["resolver"],
                _f(e["score"]),
                _f(e["median"]),
                _f(e["p95"]),
                _f(e["mean"]),
                _pct(e["failure_rate"]),
                f"{e['ok']}/{e['n']}",
                e.get("fastest_server") or "-",
            ]
            for e in ranking
        ]
        lines += [
            "  " + ln
            for ln in _table(
                ["#", "Resolver", "Score", "Median", "p95", "Mean", "Fail", "OK/N", "Best server"],
                rows,
                right={0, 2, 3, 4, 5, 6, 7},
            )
        ]

    slow_count = summary.get("slow_count", len(summary.get("slow") or []))
    slow_ms = summary.get("slow_threshold_ms", settings["slow_threshold_ms"])
    if slow_count:
        lines.append("")
        lines.append(
            f"{slow_count} successful quer{'y was' if slow_count == 1 else 'ies were'} "
            f"slower than {slow_ms:g} ms."
        )

    if rec.get("summary"):
        lines.append("")
        lines.append("Recommendation: " + rec["summary"])
    for note in rec.get("notes") or []:
        lines.append("  - " + note)
    return printable("\n".join(lines)) + "\n"
