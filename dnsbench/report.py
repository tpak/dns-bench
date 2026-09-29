"""Plain-text report rendering (CLI output and the saved <id>.txt)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .config import DEFAULT_SETTINGS
from .recommend import COUNTED_RATES, NOISE_TEST, SCORE_FORMULA


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


def _f(x: float | None, nd: int = 1) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def _pct(rate: float | None) -> str:
    return "-" if rate is None else f"{rate * 100:.1f}%"


def _fail(st: Mapping[str, Any]) -> str:
    """The failure rate, or "-" when every query failed on this computer (there is no rate)."""
    if st.get("n") and st.get("n") == st.get("local_errors"):
        return "-"
    return _pct(st.get("failure_rate"))


def _ci(ci: Any) -> str:
    """A [lo, hi] interval, the bounds joined by an en dash; an unbounded side is "?"."""
    if not isinstance(ci, (list, tuple)) or len(ci) != 2:
        return "-"
    lo, hi = ci
    return f"{'?' if lo is None else _f(lo)}–{'?' if hi is None else _f(hi)}"  # noqa: RUF001 - an en dash between the bounds, on purpose


def local_time(iso: str | None, fmt: str = "%Y-%m-%d %H:%M:%S %Z") -> str:
    """A run's UTC timestamp (``2026-09-25T02:34:56Z``) in this computer's time zone; "?" if missing."""
    if not iso:
        return "?"
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return iso
    return dt.astimezone().strftime(fmt)


def stats_line(name: str, st: Mapping[str, Any]) -> str:
    """One line per resolver, in the style of the original script."""
    line = (
        f"{name}: mean={_f(st.get('mean'))} median={_f(st.get('median'))} "
        f"p80={_f(st.get('p80'))} p95={_f(st.get('p95'))} p98={_f(st.get('p98'))} "
        f"min={_f(st.get('min'))} max={_f(st.get('max'))} n={st.get('n', 0)} "
        f"fail={_fail(st)}"
    )
    if st.get("retried"):  # only with tries > 1: queries that answered on a retry
        line += f" retried={st['retried']}"
    if st.get("local_errors"):
        line += f" local_errors={st['local_errors']}"
    if st.get("repeat_n"):  # latency uses first answers; repeats (mostly cache hits) shown apart
        line += f" first_answers={st.get('first_n', 0)} repeat_median={_f(st.get('repeat_median'))}"
    return line


def table(headers: list[str], rows: list[list[str]], right: set[int]) -> list[str]:
    """Aligned text columns: the header, a rule, then the rows. Columns in ``right`` align right."""
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]

    def fmt(cells: list[str]) -> str:
        return "  ".join(
            c.rjust(w) if i in right else c.ljust(w)
            for i, (c, w) in enumerate(zip(cells, widths, strict=True))
        ).rstrip()

    out = [fmt(headers), fmt(["-" * w for w in widths])]
    out += [fmt(r) for r in rows]
    return out


def render_text(bundle: Mapping[str, Any]) -> str:
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
        flags = {
            "cancelled": "  [CANCELLED — partial results]",
            "partial": "  [STOPPED BY AN ERROR — partial results]",
        }
        lines.append(f"DNS Bench run {bundle.get('id', '?')}" + flags.get(status, ""))
        if status == "partial" and bundle.get("error"):
            lines.append(f"Error:     {printable(str(bundle['error']))}")
        lines.append(f"Started:   {local_time(bundle.get('started_at'))}")
        lines.append(f"Duration:  {_f(bundle.get('duration_s'))} s on {bundle.get('host', '?')}")
    lines.append(
        f"Queries:   {overall.get('n', 0)} ({overall.get('ok', 0)} ok, {overall.get('failures', 0)} failed"
        + (f", {overall['local_errors']} failed on this computer" if overall.get("local_errors") else "")
        + f") across {len(summary.get('domains') or [])} domains"
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
            _fail(st),
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
        lines += ["  " + ln for ln in table(headers, srv_rows, right={2, 3, 4, 5})]

    ranking = rec.get("ranking") or []
    if ranking:
        lines.append("")
        lines.append(f"Ranking ({SCORE_FORMULA}; lower is better):")
        # "=": within noise of the one above (recommend.within_noise). "*": a failure rate the score
        # leaves out; with a single resolver there is nothing to compare, so nothing to mark.
        tied = [i > 0 and ranking[i - 1]["resolver"] in (e.get("ties") or []) for i, e in enumerate(ranking)]
        uncounted = [
            len(ranking) > 1 and bool(e["failure_rate"]) and e.get("failures_counted") is False
            for e in ranking
        ]

        def mark(flags: list[bool], i: int, sign: str) -> str:
            # a space where another row has a mark, so the digits of right-aligned cells line up
            return sign if flags[i] else " " if any(flags) else ""

        rows = [
            [
                f"{e['rank']}{mark(tied, i, '=')}",
                e["resolver"],
                _f(e["score"]),
                _f(e["median"]),
                _ci(e.get("median_ci")),
                _f(e["p95"]),
                _f(e["mean"]),
                _pct(e["failure_rate"]) + mark(uncounted, i, "*"),
                f"{e['ok']}/{e['n']}",
                e.get("fastest_server") or "-",
            ]
            for i, e in enumerate(ranking)
        ]
        lines += [
            "  " + ln
            for ln in table(
                ["#", "Resolver", "Score", "Median", "95% CI", "p95", "Mean", "Fail", "OK/N", "Best server"],
                rows,
                right={0, 2, 3, 4, 5, 6, 7, 8},
            )
        ]
        lines.append(f"  {COUNTED_RATES}")
        if any(uncounted):
            lines.append("  * not significantly higher than another resolver's, so not in the score.")
        if any(tied):
            lines.append(f"  = within noise of the resolver above: {NOISE_TEST}.")

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
        lines.append("  - " + note["text"])
    return printable("\n".join(lines)) + "\n"
