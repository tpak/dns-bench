"""Local web UI + JSON API (stdlib ThreadingHTTPServer).

Binds 127.0.0.1 by default. Requests whose Host header is not a loopback
name are rejected (DNS-rebinding protection) and state-changing requests must
be ``Content-Type: application/json`` (blocks simple cross-site form posts).
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import re
import socket
import sys
import threading
import time
from collections import deque
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__, runner, storage
from . import config as config_mod

MAX_BODY = 1024 * 1024  # 1 MB request body cap
_DRAIN_LIMIT = 8 * 1024 * 1024  # read (and discard) oversized bodies up to this so the 413 arrives
RECENT_MAX = 20

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}

CSV_COLUMNS = [
    "run_id",
    "resolver",
    "server",
    "domain",
    "round",
    "status",
    "ms",
    "rcode",
    "answers",
    "error",
    "t",
    "attempts",
]

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


class HTTPError(Exception):
    def __init__(self, status: int, message: str, details=None, headers=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details
        self.headers = headers or {}


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _csv_safe(value):
    """Neutralise spreadsheet formula injection in free-text cells."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


class JobState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.done = 0
        self.total = 0
        self.slow = 0
        self.failed = 0
        self.started_at: str | None = None
        self.t_start: float | None = None
        self.t_end: float | None = None
        self.est_seconds: float | None = None
        self.timeout_s: float | None = None  # per-query timeout of the running job
        self.slow_threshold_ms = 200
        self.last_run_id: str | None = None
        self.last_status: str | None = None
        self.error: str | None = None
        self.recent: deque = deque(maxlen=RECENT_MAX)
        self.cancel_event: threading.Event | None = None
        self.thread: threading.Thread | None = None


class DNSBenchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, host: str, port: int, config_path, runs_dir, query_fn=None, web_dir=None, quiet: bool = True
    ):
        if ":" in host:
            self.address_family = socket.AF_INET6
        self.config_path = Path(config_path)
        self.runs_dir = Path(runs_dir)
        self.query_fn = query_fn
        self.web_dir = Path(web_dir) if web_dir else config_mod.WEB_DIR
        self.quiet = quiet
        self.job = JobState()
        self.config_lock = threading.Lock()
        super().__init__((host, port), Handler)
        self.allowed_hosts = set(LOOPBACK_HOSTS)
        if host not in ("", "0.0.0.0", "::"):
            self.allowed_hosts.add(f"[{host.lower()}]" if ":" in host else host.lower())

    # -- background job ------------------------------------------------------
    def start_job(self, rounds: int | None = None) -> int:
        job = self.job
        with job.lock:
            if job.running:
                raise HTTPError(409, "A benchmark is already running")
            with self.config_lock:
                try:
                    cfg = config_mod.load_config(self.config_path, strict=True)
                except config_mod.ConfigWriteError as exc:  # disk problem, not bad input
                    raise HTTPError(500, "Cannot save config", exc.errors) from None
                except config_mod.ConfigError as exc:
                    raise HTTPError(400, "Invalid config", exc.errors) from None
            if rounds is not None:
                cfg["settings"]["rounds"] = rounds
            est = config_mod.estimate(cfg)
            total = sum(len(j["items"]) for j in runner.build_jobs(cfg))
            job.running = True
            job.done = 0
            job.total = total
            job.slow = 0
            job.failed = 0
            job.error = None
            job.recent.clear()
            job.started_at = _now_iso()
            job.t_start = time.monotonic()
            job.t_end = None
            job.est_seconds = est["est_seconds"]
            job.timeout_s = cfg["settings"]["timeout_ms"] / 1000.0
            job.slow_threshold_ms = cfg["settings"]["slow_threshold_ms"]
            job.cancel_event = threading.Event()
            job.thread = threading.Thread(
                target=self._job_main, args=(cfg, job.cancel_event), name="dnsbench-job", daemon=True
            )
            job.thread.start()
        return total

    def _on_progress(self, event: dict) -> None:
        row = event["result"]
        job = self.job
        with job.lock:
            job.done = event["done"]
            job.total = event["total"]
            job.recent.append(row)
            if row["status"] != "ok":
                job.failed += 1
            elif row["ms"] is not None and row["ms"] > job.slow_threshold_ms:
                job.slow += 1

    def _job_main(self, cfg: dict, cancel_event: threading.Event) -> None:
        run_id = None
        status = None
        error = None
        try:
            run = runner.run_benchmark(
                cfg, query_fn=self.query_fn, progress=self._on_progress, cancel_event=cancel_event
            )
            status = run["status"]
            storage.save_run(run, self.runs_dir)
            run_id = run["id"]
        except Exception as exc:  # report, never crash the server
            error = f"{type(exc).__name__}: {exc}"
            self.log_line(f"benchmark failed: {error}")
        with self.job.lock:
            self.job.running = False
            self.job.t_end = time.monotonic()
            if run_id:
                self.job.last_run_id = run_id
            self.job.last_status = status
            self.job.error = error

    def cancel_job(self) -> bool:
        with self.job.lock:
            if not self.job.running or self.job.cancel_event is None:
                return False
            self.job.cancel_event.set()
            return True

    def status(self) -> dict:
        job = self.job
        with job.lock:
            now = time.monotonic()
            if job.t_start is None:  # noqa: SIM108 - a nested conditional expression would be harder to read
                elapsed = 0.0
            else:
                elapsed = (now if job.running else (job.t_end or now)) - job.t_start
            eta = None
            if job.running:
                if job.done > 0:
                    eta = elapsed * (job.total - job.done) / job.done
                elif job.est_seconds is not None:
                    eta = max(0.0, job.est_seconds - elapsed)
            return {
                "running": job.running,
                "done": job.done,
                "total": job.total,
                "elapsed_s": round(elapsed, 2),
                "eta_s": None if eta is None else round(eta, 1),
                "started_at": job.started_at,
                "last_run_id": job.last_run_id,
                "last_status": job.last_status,
                "error": job.error,
                "slow": job.slow,
                "failed": job.failed,
                "recent": list(job.recent),
            }

    def stop_job(self, wait_s: float | None = None) -> bool:
        """Cancel a running job and wait for it to save the partial run.

        A query already in flight can't be interrupted, so by default this
        waits for up to one full query timeout plus a margin for writing the
        files (at least 5 s). Returns False if the job is still running.
        """
        if self.cancel_job():
            t = self.job.thread
            if t is not None:
                if wait_s is None:
                    wait_s = max(5.0, (self.job.timeout_s or 0.0) + 3.0)
                t.join(wait_s)
                return not t.is_alive()
        return True

    def log_line(self, msg: str) -> None:
        if not self.quiet:
            sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")


# --------------------------------------------------------------------------- #
# Request handler
# --------------------------------------------------------------------------- #

_ROUTES = [
    (re.compile(r"^/$"), {"GET": "index"}),
    (re.compile(r"^/index\.html$"), {"GET": "index"}),
    (re.compile(r"^/favicon\.ico$"), {"GET": "favicon"}),
    (re.compile(r"^/static/(?P<file>.+)$"), {"GET": "static"}),
    (re.compile(r"^/api/config$"), {"GET": "get_config", "PUT": "put_config"}),
    (re.compile(r"^/api/config/reset$"), {"POST": "reset_config"}),
    (re.compile(r"^/api/defaults$"), {"GET": "defaults"}),
    (re.compile(r"^/api/runs$"), {"GET": "runs"}),
    (re.compile(r"^/api/runs/(?P<id>[^/]+)$"), {"GET": "run"}),
    (re.compile(r"^/api/runs/(?P<id>[^/]+)/csv$"), {"GET": "run_csv"}),
    (re.compile(r"^/api/aggregate$"), {"GET": "aggregate"}),
    (re.compile(r"^/api/run$"), {"POST": "start_run"}),
    (re.compile(r"^/api/run/cancel$"), {"POST": "cancel_run"}),
    (re.compile(r"^/api/status$"), {"GET": "status"}),
]

_HTML_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


class Handler(BaseHTTPRequestHandler):
    server: DNSBenchServer
    server_version = f"dns-bench/{__version__}"

    # -- plumbing ------------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")  # same headers as GET; _send() omits the body

    def do_PUT(self):
        self._dispatch("PUT")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    def log_message(self, format, *args):  # noqa: A002 - signature from base class
        pass  # replaced by the concise line in log_request / log_error

    def log_request(self, code="-", size="-"):
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = 0
        # quiet: silent. Otherwise one line for errors and state-changing requests
        # (the UI polls /api/status twice a second; logging that would be noise).
        interesting = not self.server.quiet and (code >= 400 or self.command in ("PUT", "POST"))
        if interesting:
            path = getattr(self, "path", "")
            sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.command} {path} -> {code}\n")

    def log_error(self, format, *args):  # noqa: A002
        if not self.server.quiet:
            sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] error: {format % args}\n")

    def _send(self, status: int, body: bytes, content_type: str, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj, headers: dict | None = None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", headers)

    def _error(self, status: int, message: str, details=None, headers=None):
        obj: dict[str, object] = {"error": message}
        if details is not None:
            obj["details"] = list(details)
        self._json(status, obj, headers)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        if host.startswith("["):
            end = host.find("]")
            if end < 0:
                return False
            name, rest = host[: end + 1], host[end + 1 :]
        else:
            name, sep, port = host.partition(":")
            rest = f":{port}" if sep else ""
        if rest:
            port = rest[1:]
            if not rest.startswith(":") or not port.isdigit():
                return False
            if int(port) != self.server.server_address[1]:
                return False
        return name in self.server.allowed_hosts

    def _dispatch(self, method: str):
        try:
            if not self._host_ok():
                raise HTTPError(403, "Forbidden: this server only answers requests for localhost")
            path = urlsplit(self.path).path
            for pattern, methods in _ROUTES:
                m = pattern.match(path)
                if not m:
                    continue
                name = methods.get(method)
                if name is None:
                    raise HTTPError(
                        405, f"Method {method} not allowed", headers={"Allow": ", ".join(sorted(methods))}
                    )
                getattr(self, f"h_{name}")(**m.groupdict())
                return
            raise HTTPError(404, "Not found")
        except HTTPError as exc:
            self._error(exc.status, exc.message, exc.details, exc.headers)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover - defensive
            self.server.log_line(f"internal error on {method} {self.path}: {exc!r}")
            with contextlib.suppress(OSError):
                self._error(500, "Internal server error", [f"{type(exc).__name__}: {exc}"])

    def _read_json(self, required: bool = True):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise HTTPError(400, "Content-Type must be application/json")
        if self.headers.get("Transfer-Encoding"):
            raise HTTPError(400, "Chunked request bodies are not supported; send Content-Length")
        raw_len = self.headers.get("Content-Length")
        try:
            length = int(raw_len) if raw_len is not None else 0
        except ValueError:
            raise HTTPError(400, "Invalid Content-Length") from None
        if length < 0:
            raise HTTPError(400, "Invalid Content-Length")
        if length > MAX_BODY:
            if length <= _DRAIN_LIMIT:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            self.close_connection = True
            raise HTTPError(413, "Request body too large (limit 1 MB)")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            if required:
                raise HTTPError(400, "Request body required")
            return None
        try:
            return config_mod.loads_json(raw.decode("utf-8"))
        except ValueError as exc:
            # bad UTF-8, bad JSON, a >4300-digit integer, or nesting past config_mod.MAX_JSON_DEPTH
            raise HTTPError(400, "Malformed JSON", [str(exc)[:200]]) from None

    # -- static --------------------------------------------------------------
    def _file(self, path: Path, content_type: str | None = None):
        try:
            data = path.read_bytes()
        except OSError:
            raise HTTPError(404, "Not found") from None
        ctype = content_type or CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        headers = {"Content-Security-Policy": _HTML_CSP} if ctype.startswith("text/html") else None
        self._send(200, data, ctype, headers)

    def h_index(self):
        index = self.server.web_dir / "index.html"
        if not index.is_file():
            raise HTTPError(404, f"UI files not found ({index})")
        self._file(index)

    def h_favicon(self):
        self._send(204, b"", "image/x-icon")

    def h_static(self, file: str):
        rel = unquote(file)
        if "\x00" in rel or "\\" in rel:
            raise HTTPError(404, "Not found")
        parts = rel.split("/")
        if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
            raise HTTPError(404, "Not found")
        base = self.server.web_dir.resolve()
        try:
            target = (base / rel).resolve()
        except (OSError, RuntimeError):
            raise HTTPError(404, "Not found") from None
        if not target.is_relative_to(base) or not target.is_file():
            raise HTTPError(404, "Not found")
        self._file(target)

    # -- config --------------------------------------------------------------
    def h_get_config(self):
        with self.server.config_lock:
            try:
                cfg = config_mod.load_config(self.server.config_path, strict=False)
            except config_mod.ConfigWriteError as exc:
                raise HTTPError(500, "Cannot save config", exc.errors) from None
            except config_mod.ConfigError as exc:
                raise HTTPError(500, "Cannot read config", exc.errors) from None
        self._json(200, cfg)

    def h_put_config(self):
        body = self._read_json(required=True)
        if not isinstance(body, dict):
            raise HTTPError(400, "Invalid config", ["Config must be a JSON object"])
        with self.server.config_lock:
            try:
                saved = config_mod.save_config(body, self.server.config_path)
            except config_mod.ConfigWriteError as exc:  # disk problem, not bad input
                raise HTTPError(500, "Cannot save config", exc.errors) from None
            except config_mod.ConfigError as exc:
                raise HTTPError(400, "Invalid config", exc.errors) from None
        self._json(200, saved)

    def h_reset_config(self):
        self._read_json(required=False)
        with self.server.config_lock:
            try:
                cfg = config_mod.reset_config(self.server.config_path)
            except config_mod.ConfigWriteError as exc:
                raise HTTPError(500, "Cannot save config", exc.errors) from None
        self._json(200, cfg)

    def h_defaults(self):
        self._json(200, config_mod.default_config())

    # -- runs ----------------------------------------------------------------
    def _load_run(self, run_id: str) -> dict:
        run_id = unquote(run_id)
        if not storage.valid_run_id(run_id):
            raise HTTPError(400, f"Invalid run id: {run_id}")
        try:
            return storage.load_run(run_id, self.server.runs_dir)
        except KeyError:
            raise HTTPError(404, f"Run {run_id} not found") from None
        except storage.StorageError as exc:
            raise HTTPError(500, str(exc)) from None

    def h_runs(self):
        self._json(200, {"runs": storage.list_runs(self.server.runs_dir)})

    def h_run(self, id: str):  # noqa: A002
        self._json(200, self._load_run(id))

    def h_run_csv(self, id: str):  # noqa: A002
        run = self._load_run(id)
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(CSV_COLUMNS)
        for row in run.get("results") or []:
            w.writerow(
                [run["id"]] + [_csv_safe("" if row.get(c) is None else row.get(c)) for c in CSV_COLUMNS[1:]]
            )
        self._send(
            200,
            buf.getvalue().encode("utf-8"),
            "text/csv; charset=utf-8",
            {"Content-Disposition": f'attachment; filename="dns-bench-{run["id"]}.csv"'},
        )

    def h_aggregate(self):
        qs = parse_qs(urlsplit(self.path).query)
        spec = (qs.get("runs") or ["all"])[0].strip()
        if spec in ("", "all"):
            ids = "all"
        else:
            ids = [p.strip() for p in spec.split(",") if p.strip()]
            bad = [i for i in ids if not storage.valid_run_id(i)]
            if bad:
                raise HTTPError(400, "Invalid run id", bad)
            missing = [i for i in ids if not (self.server.runs_dir / f"{i}.json").is_file()]
            if missing:
                raise HTTPError(404, "Run not found", missing)
        with self.server.config_lock:
            current = config_mod.current_resolver_names(self.server.config_path)
        try:
            bundle = storage.aggregate(self.server.runs_dir, ids, current=current)
        except KeyError:
            raise HTTPError(404, "No runs saved yet") from None
        except storage.StorageError as exc:
            raise HTTPError(500, str(exc)) from None
        self._json(200, bundle)

    # -- benchmark job ---------------------------------------------------------
    def h_start_run(self):
        body = self._read_json(required=False) or {}
        if not isinstance(body, dict):
            raise HTTPError(400, "Body must be a JSON object")
        rounds = body.get("rounds")
        if rounds is not None:
            lo, hi = config_mod.SETTING_BOUNDS["rounds"]
            if isinstance(rounds, float) and rounds.is_integer():
                rounds = int(rounds)
            if isinstance(rounds, bool) or not isinstance(rounds, int) or not lo <= rounds <= hi:
                raise HTTPError(400, f"rounds must be a whole number from {lo} to {hi}")
        total = self.server.start_job(rounds)
        self._json(202, {"job": "started", "total": total})

    def h_cancel_run(self):
        self._read_json(required=False)
        if not self.server.cancel_job():
            raise HTTPError(409, "No benchmark is running")
        self._json(200, {"cancelled": True})

    def h_status(self):
        self._json(200, self.server.status())


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def make_server(
    host: str, port: int, config_path, runs_dir, query_fn=None, web_dir=None, quiet: bool = True
) -> DNSBenchServer:
    return DNSBenchServer(host, port, config_path, runs_dir, query_fn=query_fn, web_dir=web_dir, quiet=quiet)


def server_url(server: DNSBenchServer) -> str:
    host, port = str(server.server_address[0]), server.server_address[1]  # always str for AF_INET/6
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}/"


def serve(
    host: str = "127.0.0.1",
    port: int = 8053,
    config_path=config_mod.DEFAULT_CONFIG_PATH,
    runs_dir=config_mod.DEFAULT_RUNS_DIR,
    open_browser: bool = False,
    query_fn=None,
    quiet: bool = True,
) -> None:
    """Run the web UI until Ctrl-C (KeyboardInterrupt propagates after cleanup)."""
    httpd = make_server(host, port, config_path, runs_dir, query_fn=query_fn, quiet=quiet)
    url = server_url(httpd)
    print(f"DNS Bench UI: {url}  (Ctrl-C to stop)", flush=True)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "warning: listening on a non-loopback address; only loopback Host names are "
            "accepted, and there is no authentication.",
            file=sys.stderr,
        )
    if open_browser:
        import webbrowser

        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever(poll_interval=0.25)
    finally:
        try:
            if httpd.job.running:
                print(
                    "Stopping: cancelling the running benchmark and saving the partial run...",
                    file=sys.stderr,
                    flush=True,
                )
            if not httpd.stop_job():
                print(
                    "warning: the benchmark is still finishing; its partial run may not be saved.",
                    file=sys.stderr,
                    flush=True,
                )
        finally:
            httpd.server_close()
