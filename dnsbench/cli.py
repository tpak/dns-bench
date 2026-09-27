"""Command-line entry point: ``dns-bench run|serve|list|report|config``."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import signal
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, paths, report, runner, storage, sysdns
from . import config as config_mod

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_INTERRUPTED = 0, 1, 2, 130


def _err(msg: str) -> None:
    print(f"dns-bench: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Live progress
# --------------------------------------------------------------------------- #


class Progress:
    """Progress callback for run_benchmark (calls are serialised by the runner)."""

    def __init__(self, total: int, slow_threshold_ms: float, stream=None, quiet: bool = False):
        self.stream = stream or sys.stderr
        self.total = total
        self.slow_ms = slow_threshold_ms
        self.quiet = quiet
        try:
            self.tty = self.stream.isatty()
        except (AttributeError, ValueError):
            self.tty = False
        self.t0 = time.monotonic()
        self.done = 0
        self.slow = 0
        self.failed = 0
        self._last_draw = 0.0
        self._last_line = self.t0
        self._drawn = False

    def __call__(self, event: dict) -> None:
        row = event["result"]
        self.done = event["done"]
        self.total = event["total"]
        where = f"{row['resolver']} {row['server']} {row['domain']}"
        if row["status"] != "ok":
            self.failed += 1
            if row["status"] == "timeout":
                why = "timeout"
            elif row.get("rcode"):
                why = row["rcode"] + (f" ({row['ms']:.1f}ms)" if row.get("ms") is not None else "")
            else:
                why = f"error: {row.get('error')}"
            self._emit(f"  [fail] {where} -> {why}")
        elif row["ms"] is not None and row["ms"] > self.slow_ms:
            self.slow += 1
            self._emit(f"  [slow] {where} -> {row['ms']:.1f}ms")
        self._draw()

    def _status(self) -> str:
        elapsed = time.monotonic() - self.t0
        pct = 100.0 * self.done / self.total if self.total else 100.0
        eta = elapsed * (self.total - self.done) / self.done if self.done else None
        eta_txt = f"{eta:.0f}s" if eta is not None else "?"
        return (
            f"{self.done}/{self.total} {pct:3.0f}%  elapsed {elapsed:.1f}s  ETA {eta_txt}  "
            f"slow {self.slow}  fail {self.failed}"
        )

    def _emit(self, line: str) -> None:
        if self.quiet:
            return
        if self.tty:
            self.stream.write("\r\x1b[K" + line + "\n")
            self._draw(force=True)
        else:
            self.stream.write(line + "\n")
        self.stream.flush()

    def _draw(self, force: bool = False) -> None:
        if self.quiet:
            return
        now = time.monotonic()
        final = self.done >= self.total
        if self.tty:
            if force or final or now - self._last_draw >= 0.1:
                self._last_draw = now
                self._drawn = True
                self.stream.write("\r\x1b[K  " + self._status())
                self.stream.flush()
        elif final or now - self._last_line >= 5.0:
            self._last_line = now
            self.stream.write("  progress: " + self._status() + "\n")
            self.stream.flush()

    def finish(self) -> None:
        if self.tty and self._drawn and not self.quiet:
            self.stream.write("\r\x1b[K")
            self.stream.flush()


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _apply_run_overrides(cfg: dict, args) -> list[str]:
    s = cfg["settings"]
    if args.rounds is not None:
        s["rounds"] = args.rounds
    if args.interval_ms is not None:
        s["per_server_interval_ms"] = args.interval_ms
    if args.timeout_ms is not None:
        s["timeout_ms"] = args.timeout_ms
    if args.resolvers:
        wanted = [w.strip() for w in args.resolvers.split(",") if w.strip()]
        by_name = {r["name"].casefold(): r for r in cfg["resolvers"]}
        unknown = [w for w in wanted if w.casefold() not in by_name]
        if unknown:
            names = ", ".join(r["name"] for r in cfg["resolvers"])
            return [f"unknown resolver(s): {', '.join(unknown)} (configured: {names})"]
        keep = {w.casefold() for w in wanted}
        for r in cfg["resolvers"]:
            r["enabled"] = r["name"].casefold() in keep
    return [str(e) for e in config_mod.validate_config(cfg)]


def cmd_run(args) -> int:
    if not args.no_save:  # --no-save writes nothing, so a missing config is used as loaded, not created
        created = config_mod.ensure_config(args.config)
        if created is not None and not args.quiet:
            print(f"Created {args.config} with the default resolvers. {created.message}", file=sys.stderr)
    cfg = config_mod.load_config(args.config)
    errors = _apply_run_overrides(cfg, args)
    if errors:
        for e in errors:
            _err(e)
        return EXIT_USAGE
    if not args.no_save:
        problem = storage.check_writable(args.runs_dir)
        if problem:
            _err(problem + " (use --runs-dir DIR, or --no-save)")
            return EXIT_ERROR
    cfg = config_mod.normalize_config(cfg)
    est = config_mod.estimate(cfg)
    s = cfg["settings"]
    names = [r["name"] for r in config_mod.enabled_resolvers(cfg)]
    if not args.quiet:
        print(
            f"Benchmarking {len(names)} resolver{'s' if len(names) != 1 else ''} "
            f"({est['servers']} server{'s' if est['servers'] != 1 else ''}: {', '.join(names)}) "
            f"x {len(cfg['domains'])} domains x {s['rounds']} round{'s' if s['rounds'] != 1 else ''} "
            f"= {est['queries']} queries",
            file=sys.stderr,
        )
        print(
            f"Polite pacing: 1 query in flight per server, >= {s['per_server_interval_ms']} ms apart "
            f"(<= {est['max_qps_per_server']:g} q/s per server, <= {est['max_qps_total']:g} q/s total). "
            f"Estimated time ~{est['est_seconds']:.0f} s. Ctrl-C to stop early.",
            file=sys.stderr,
        )

    progress = Progress(est["queries"], s["slow_threshold_ms"], quiet=args.quiet)
    cancel = threading.Event()
    stop_waiting = threading.Event()
    signals = {"count": 0, "measuring": True}

    def on_signal(signum, frame):
        # Ctrl-C (SIGINT) and `kill` (SIGTERM) alike. The first stops the run once the queries in flight
        # are answered or time out; a second stops waiting for them. Neither may interrupt the save.
        # Events, not exceptions: an exception raised here could land anywhere in the main thread.
        signals["count"] += 1
        if not signals["measuring"]:
            return
        if signals["count"] > 1:
            stop_waiting.set()  # run_benchmark returns what it has, without waiting
            return
        cancel.set()
        if not args.quiet:
            sys.stderr.write("\r\x1b[K" if progress.tty else "\n")
            sys.stderr.write(
                f"Cancelling: waiting up to {s['timeout_ms'] / 1000:g} s for the queries in flight, then "
                "saving the partial run. Press Ctrl-C again to save it now.\n"
            )
            sys.stderr.flush()

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        try:
            run = runner.run_benchmark(cfg, progress=progress, cancel_event=cancel, stop_waiting=stop_waiting)
        finally:
            signals["measuring"] = False
            progress.finish()
        return _finish_run(args, run, est)
    finally:
        for sig, handler in previous.items():
            if handler is not None:  # None: installed outside Python, can't be put back
                signal.signal(sig, handler)


def _finish_run(args, run: dict, est: dict) -> int:
    """Save and report a finished (or stopped) run; returns the exit code."""
    saved = None
    if args.no_save:
        storage.finalize_run(run)
    else:
        saved = storage.save_run_safely(run, args.runs_dir)

    # The report goes out first, so the measurements are never lost.
    if args.json:
        json.dump(run, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(report.render_text(run))
    sys.stdout.flush()
    if saved is not None and saved.error is None and saved.path is not None:
        print(f"Saved: {saved.path} (report: {saved.path.with_suffix('.txt').name})", file=sys.stderr)
    if saved is not None and saved.error is not None:
        _err(saved.error)
        if saved.rescued is not None:
            _err(f"the full run record was written to {saved.rescued} instead")
    if run["status"] == "cancelled":
        print(f"Run cancelled after {len(run['results'])} of {est['queries']} queries.", file=sys.stderr)
        return EXIT_INTERRUPTED
    if run["status"] == "partial":
        _err(
            f"the benchmark stopped early after an internal error ({run.get('error')}); "
            f"the {len(run['results'])} of {est['queries']} queries measured before it are in the report"
            + ("" if args.no_save or (saved is not None and saved.error) else " and were saved")
        )
        return EXIT_ERROR
    if saved is not None and saved.error is not None:
        return EXIT_ERROR
    overall = (run.get("summary") or {}).get("overall") or {}
    if run.get("results") and not overall.get("ok"):
        _err(
            "no resolver returned any successful answers "
            "(check your network connection and that outbound UDP port 53 is allowed)"
        )
        return EXIT_ERROR
    return EXIT_OK


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def cmd_serve(args) -> int:
    from . import server

    if not args.allow_remote and not server.is_loopback_host(args.host):
        _err(
            f"refusing to listen on {args.host or 'every interface'}: other machines could reach it, and "
            "the web UI has no authentication. To use it from another machine, forward the port over "
            f"SSH instead (ssh -L {args.port}:127.0.0.1:{args.port} <this machine>), or add --allow-remote"
        )
        return EXIT_USAGE
    # Explicit handlers: Ctrl-C and `kill` both stop cleanly (a running job is
    # cancelled and its partial run saved), even if SIGINT was inherited as ignored.
    previous = {sig: signal.signal(sig, _raise_interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        server.serve(args.host, args.port, args.config, args.runs_dir, open_browser=args.open, quiet=False)
    except (OSError, OverflowError) as exc:
        _err(f"cannot listen on {args.host}:{args.port}: {exc}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return EXIT_INTERRUPTED
    finally:
        for sig, handler in previous.items():
            if handler is not None:  # None: installed outside Python, can't be put back
                signal.signal(sig, handler)
    return EXIT_OK


def _local(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def cmd_list(args) -> int:
    rows = storage.list_runs(args.runs_dir)
    if not rows:
        print(f"No runs saved yet in {args.runs_dir}. Start one with: dns-bench run")
        return EXIT_OK
    table = []
    for r in rows:
        table.append(
            [
                r["id"],
                _local(r.get("started_at")),
                f"{r['duration_s']:.1f}s" if isinstance(r.get("duration_s"), (int, float)) else "-",
                r.get("status") or "-",
                str(r.get("n_queries", 0)),
                report.printable(",".join(map(str, r.get("resolvers") or []))),
                report.printable(str(r.get("best") or "-")),
                f"{r['best_median']:.1f} ms" if isinstance(r.get("best_median"), (int, float)) else "-",
            ]
        )
    headers = ["ID", "Started", "Duration", "Status", "Queries", "Resolvers", "Best", "Best median"]
    widths = [max(len(h), *(len(row[i]) for row in table)) for i, h in enumerate(headers)]
    right = {2, 4, 7}

    def fmt(cells):
        return "  ".join(
            c.rjust(w) if i in right else c.ljust(w)
            for i, (c, w) in enumerate(zip(cells, widths, strict=True))
        ).rstrip()

    print(fmt(headers))
    print(fmt(["-" * w for w in widths]))
    for row in table:
        print(fmt(row))
    print(f"\n{len(rows)} run{'s' if len(rows) != 1 else ''} in {args.runs_dir}")
    return EXIT_OK


def cmd_report(args) -> int:
    target = args.target
    if target == "all":
        try:
            bundle = storage.aggregate(
                args.runs_dir, "all", current=config_mod.current_resolver_names(args.config)
            )
        except KeyError:
            _err(f"no runs saved yet in {args.runs_dir}")
            return EXIT_ERROR
        sys.stdout.write(report.render_text(bundle))
        return EXIT_OK
    if target == "latest":
        run_id = storage.latest_run_id(args.runs_dir)
        if run_id is None:
            _err(f"no runs saved yet in {args.runs_dir}")
            return EXIT_ERROR
    else:
        run_id = target
        if not storage.valid_run_id(run_id):
            _err(f"invalid run id {run_id!r} (expected e.g. 20260925T023456Z; see `dns-bench list`)")
            return EXIT_USAGE
    try:
        run = storage.load_run(run_id, args.runs_dir)
    except KeyError:
        _err(f"run {run_id} not found in {args.runs_dir}")
        return EXIT_ERROR
    except storage.StorageError as exc:
        _err(str(exc))
        return EXIT_ERROR
    sys.stdout.write(report.render_text(run))
    return EXIT_OK


def cmd_config(args) -> int:
    path = Path(args.config)
    if args.path:
        print(path)
        print(f"Runs are saved in {args.runs_dir}", file=sys.stderr)
        return EXIT_OK
    if args.reset:
        _, system = config_mod.reset_config(path)
        print(f"Config reset to defaults: {path}")
        print(system.message)
        return EXIT_OK
    if args.detect:
        return _config_detect(path)
    cfg = config_mod.load_config(path, strict=False)
    print(config_mod.dumps_config(cfg), end="")
    if not path.exists():
        print(
            f"({path} doesn't exist yet. This is what it will start with, on the first run or with "
            "`dns-bench config --reset`.)",
            file=sys.stderr,
        )
    errors = config_mod.validate_config(cfg)
    for e in errors:
        _err(f"config problem: {e}")
    return EXIT_ERROR if errors else EXIT_OK


def _config_detect(path: Path) -> int:
    """Add this computer's resolvers to the config as "System", or update that entry's servers."""
    cfg = config_mod.load_config(path, strict=False)
    system = config_mod.system_resolver(cfg.get("resolvers"), sysdns.detect())
    if system.resolver is None:
        if not system.detected.servers:
            _err(system.message)
            return EXIT_ERROR
        print(system.message)  # found, but every server is already configured: nothing to do
        return EXIT_OK
    config_mod.save_config(config_mod.with_system_resolver(cfg, system.resolver), path)
    print(system.message)
    print(f"Saved to {path}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def _int_in_range(value: str, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {value!r}") from None
    if not lo <= n <= hi:
        raise argparse.ArgumentTypeError(f"must be from {lo} to {hi} (got {n})")
    return n


def _bounded_int(key: str):
    """argparse type for a run override, with the same bounds as config.json."""
    lo, hi = config_mod.SETTING_BOUNDS[key]

    def conv(value: str) -> int:
        return _int_in_range(value, lo, hi)

    conv.__name__ = key  # argparse's fallback 'invalid <name> value' message
    return conv


def _port(value: str) -> int:
    return _int_in_range(value, 0, 65535)  # 0 = let the OS pick a free port


def _range_help(text: str, key: str, unit: str = "") -> str:
    lo, hi = config_mod.SETTING_BOUNDS[key]
    return f"{text} ({lo}-{hi}{unit}; default: from the config, built-in {config_mod.DEFAULT_SETTINGS[key]})"


def _default_path_help() -> tuple[str, str]:
    """The default config file and runs dir, for --help (they depend on DNSBENCH_HOME)."""
    try:
        default = paths.resolve()
    except paths.DataHomeError:
        return f"${paths.HOME_ENV}/{paths.CONFIG_NAME}", f"${paths.HOME_ENV}/{paths.RUNS_NAME}"
    return str(default.config), str(default.runs_dir)


def build_parser() -> argparse.ArgumentParser:
    config_default, runs_default = _default_path_help()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help=f"config file (default: {config_default})",
    )
    common.add_argument(
        "--runs-dir",
        default=argparse.SUPPRESS,
        metavar="DIR",
        help=f"where runs are saved (default: {runs_default})",
    )

    p = argparse.ArgumentParser(
        prog="dns-bench",
        description="Fast, polite DNS resolver benchmark with a local web UI.",
        epilog=f"Data is kept in the checkout, or in ${paths.HOME_ENV} if it is set. "
        "Run `dns-bench <command> -h` for command options.",
    )
    p.add_argument("--version", action="version", version=f"dns-bench {__version__}")
    p.add_argument("--config", metavar="PATH", help=f"config file (default: {config_default})")
    p.add_argument("--runs-dir", metavar="DIR", help=f"where runs are saved (default: {runs_default})")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    r = sub.add_parser(
        "run",
        parents=[common],
        help="run a benchmark now",
        description="Run a benchmark. Overrides apply to this run only.",
    )
    r.add_argument(
        "--rounds",
        type=_bounded_int("rounds"),
        metavar="N",
        help=_range_help("query every domain N times per server", "rounds"),
    )
    r.add_argument(
        "--interval-ms",
        type=_bounded_int("per_server_interval_ms"),
        metavar="MS",
        help=_range_help("min gap between queries to the same server", "per_server_interval_ms", " ms"),
    )
    r.add_argument(
        "--timeout-ms",
        type=_bounded_int("timeout_ms"),
        metavar="MS",
        help=_range_help("per-query timeout", "timeout_ms", " ms"),
    )
    r.add_argument(
        "--resolvers",
        metavar="A,B",
        help="only these resolvers (by name, comma separated; may include disabled ones)",
    )
    r.add_argument("--no-save", action="store_true", help="don't save the run")
    r.add_argument("--quiet", action="store_true", help="no progress or [slow]/[fail] lines")
    r.add_argument("--json", action="store_true", help="print the full run record as JSON")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("serve", parents=[common], help="start the web UI")
    s.add_argument("--host", default="127.0.0.1", help="bind address (default: %(default)s)")
    s.add_argument("--port", type=_port, default=8053, help="port, 0-65535 (default: %(default)s)")
    s.add_argument("--open", action="store_true", help="open the UI in your browser")
    s.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a --host that other machines can reach (there is no authentication; "
        "prefer an SSH tunnel: ssh -L 8053:127.0.0.1:8053 <host>)",
    )
    s.set_defaults(func=cmd_serve)

    ls = sub.add_parser("list", parents=[common], help="list saved runs")
    ls.set_defaults(func=cmd_list)

    rp = sub.add_parser(
        "report",
        parents=[common],
        help="text report for a saved run",
        description="Print the report for the latest run, a run id, or all runs combined.",
    )
    rp.add_argument("target", nargs="?", default="latest", metavar="latest|all|RUN_ID")
    rp.set_defaults(func=cmd_report)

    c = sub.add_parser("config", parents=[common], help="show, locate, reset or update the config")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--show", action="store_true", help="print the config (default)")
    g.add_argument(
        "--reset",
        action="store_true",
        help="overwrite the config with the defaults, plus this computer's own resolvers as 'System'",
    )
    g.add_argument(
        "--detect",
        action="store_true",
        help="add this computer's own resolvers as 'System', or update that entry, and save",
    )
    g.add_argument(
        "--path", action="store_true", help="print the config file path (and the runs dir, on stderr)"
    )
    c.set_defaults(func=cmd_config)
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):  # not e.g. a StringIO swapped in by a test
            with contextlib.suppress(ValueError):  # a closed stream: nothing to reconfigure
                stream.reconfigure(errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        data = paths.resolve(args.config, args.runs_dir)
    except paths.DataHomeError as exc:
        _err(str(exc))
        return EXIT_ERROR
    args.config, args.runs_dir = data.config, data.runs_dir
    try:
        return args.func(args)
    except config_mod.ConfigError as exc:
        for message in exc.messages:
            _err(message)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        # e.g. `dns-bench list | head`: silence the flush error at interpreter exit
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return EXIT_ERROR
