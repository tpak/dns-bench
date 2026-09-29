"""Local web UI + JSON API (stdlib ThreadingHTTPServer).

Binds 127.0.0.1 by default. Requests whose Host header doesn't name this
server are rejected (DNS-rebinding protection): a loopback name, the --host
address, or, when bound to every interface, any IP address. A rebinding
attack always arrives under the attacker's host name, never a bare IP. State-changing requests must be
``Content-Type: application/json``, which a cross-site form can't send and which
forces a CORS preflight that is never granted. Their ``Origin``, when present,
must also be this server's own origin.
"""

from __future__ import annotations

import contextlib
import csv
import io
import ipaddress
import json
import re
import socket
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__, analysis, paths, recommend, stats, storage, sysdns
from . import config as config_mod
from . import service as service_mod

MAX_BODY = 1024 * 1024  # 1 MB request body cap
_DRAIN_LIMIT = 8 * 1024 * 1024  # read (and discard) oversized bodies up to this so the 413 arrives

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
    "truncated",
]

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
_PORT_RE = re.compile(r"[0-9]{1,5}")  # ASCII digits only: str.isdigit() also accepts e.g. "²"


class HTTPError(Exception):
    """An error response: ``{"error": message, "details": [{path, code, message}, ...]}``.

    ``details`` are ValidationErrors, the same shape the config validation uses, so the UI can put
    each one next to the field (``path``) it is about. They never hold absolute file paths or Python
    exception text.
    """

    def __init__(
        self,
        status: int,
        message: str,
        details: list[config_mod.ValidationError] | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details
        self.headers = headers or {}


def _problem(code: str, message: str, path: str = "") -> config_mod.ValidationError:
    """A detail for an error that isn't about the config (a request body, a run id, the runs folder)."""
    return config_mod.ValidationError(path, code, message)


def _rounds_param(body: dict) -> int | None:
    """The optional ``rounds`` override in a request body, checked against the same bounds as the config."""
    rounds = body.get("rounds")
    if rounds is None:
        return None
    lo, hi = config_mod.SETTING_BOUNDS["rounds"]
    if isinstance(rounds, float) and rounds.is_integer():
        rounds = int(rounds)
    if isinstance(rounds, bool) or not isinstance(rounds, int) or not lo <= rounds <= hi:
        raise HTTPError(
            400,
            f"rounds must be a whole number from {lo} to {hi}",
            [_problem("out_of_range", f"rounds must be a whole number from {lo} to {hi}", "rounds")],
        )
    return rounds


def api_schema() -> dict:
    """What the web UI needs to know about the rules, so it never keeps a copy of them (GET /api/schema)."""
    return {
        "version": __version__,
        "defaults": config_mod.default_config(),
        "settings": config_mod.setting_schema(),
        "limits": {
            "resolvers": config_mod.MAX_RESOLVERS,
            "servers_per_resolver": config_mod.MAX_SERVERS_PER_RESOLVER,
            "name_length": config_mod.MAX_NAME_LEN,
            "domains": config_mod.MAX_DOMAINS,
            "hostname_length": config_mod.MAX_HOSTNAME_LEN,
            "queries_per_run": config_mod.MAX_QUERIES_PER_RUN,
            "min_interval_ms": config_mod.MIN_INTERVAL_MS,
            "request_bytes": MAX_BODY,
        },
        "presets": config_mod.PRESETS,
        "error_codes": config_mod.ERROR_CODES,
        "scoring": {
            "weights": {"median": recommend.W_MEDIAN, "p95": recommend.W_P95, "mean": recommend.W_MEAN},
            "failure_weight": recommend.FAILURE_WEIGHT,
            "retry_weight": recommend.RETRY_WEIGHT,
            "formula": recommend.SCORE_FORMULA,
            "counted_rates": recommend.COUNTED_RATES,
            "confidence": 0.95,
            "tie_abs_ms": recommend.TIE_ABS_MS,
            "server_tie_abs_ms": recommend.SERVER_TIE_ABS_MS,
            "failure_warn_rate": recommend.FAILURE_WARN_RATE,
            "low_sample": recommend.LOW_SAMPLE,
        },
        "slow": {"list_max": stats.SLOW_LIST_MAX, "per_resolver_max": stats.SLOW_PER_RESOLVER_MAX},
        "analysis_version": analysis.ANALYSIS_VERSION,
    }


def _config_response(cfg: dict) -> dict:
    """What the config endpoints return: the config, its problems ([] when valid) and a run's cost."""
    return {
        "config": cfg,
        "errors": [e.to_dict() for e in config_mod.validate_config(cfg)],
        "estimate": service_mod.estimate(cfg),
    }


def _csv_safe(value):
    """Neutralise spreadsheet formula injection in free-text cells."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


class DNSBenchServer(ThreadingHTTPServer):
    """The HTTP front end. Runs, config and the background job belong to ``service`` and ``jobs``."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        host: str,
        port: int,
        config_path,
        runs_dir,
        query_fn=None,
        web_dir=None,
        quiet: bool = True,
        detect_fn: config_mod.Detect | None = None,
    ):
        if ":" in host:
            self.address_family = socket.AF_INET6
        self.detect_fn = detect_fn or sysdns.detect  # finds this computer's resolvers (a seam for tests)
        self.service = service_mod.BenchmarkService(
            config_path, runs_dir, query_fn=query_fn, detect_fn=self.detect_fn
        )
        self.jobs = service_mod.JobManager(self.service, log=self.log_line)
        self.web_dir = Path(web_dir) if web_dir else paths.WEB_DIR
        self.quiet = quiet
        super().__init__((host, port), Handler)
        self.allowed_hosts = set(LOOPBACK_HOSTS)
        # Bound to every interface (serve --allow-remote): other machines reach it under this machine's
        # IP addresses, which can't all be listed, so any IP literal is accepted as a Host.
        self.any_ip_host = host in ("", "0.0.0.0", "::")
        if not self.any_ip_host:
            self.allowed_hosts.add(f"[{host.lower()}]" if ":" in host else host.lower())

    @property
    def config_path(self) -> Path:
        return self.service.config_path

    @property
    def runs_dir(self) -> Path:
        return self.service.runs_dir

    def stop_job(self, wait_s: float | None = None) -> bool:
        """Cancel a running benchmark and wait for its partial run to be saved (see JobManager.stop)."""
        return self.jobs.stop(wait_s)

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
    (re.compile(r"^/api/config/system-resolver$"), {"POST": "system_resolver"}),
    (re.compile(r"^/api/config/validate$"), {"POST": "validate_config"}),
    (re.compile(r"^/api/estimate$"), {"POST": "estimate"}),
    (re.compile(r"^/api/schema$"), {"GET": "schema"}),
    (re.compile(r"^/api/info$"), {"GET": "info"}),
    (re.compile(r"^/api/runs$"), {"GET": "runs"}),
    (re.compile(r"^/api/runs/(?P<id>[^/]+)$"), {"GET": "run"}),
    (re.compile(r"^/api/runs/(?P<id>[^/]+)/csv$"), {"GET": "run_csv"}),
    (re.compile(r"^/api/aggregate$"), {"GET": "aggregate"}),
    (re.compile(r"^/api/run$"), {"POST": "start_run"}),
    (re.compile(r"^/api/run/cancel$"), {"POST": "cancel_run"}),
    (re.compile(r"^/api/status$"), {"GET": "status"}),
]

# app.js sets styles only through CSSOM (element.style), which style-src doesn't restrict, so inline styles
# can stay blocked. Trusted Types make the HTML-parsing sinks (innerHTML and friends) throw, and the UI
# never needs them; 'none' also stops injected code from creating a policy.
_HTML_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'; "
    "require-trusted-types-for 'script'; trusted-types 'none'"
)


class Handler(BaseHTTPRequestHandler):
    server: DNSBenchServer
    server_version = f"dns-bench/{__version__}"
    # One request per connection (the base class's default, stated here on purpose). Several responses go
    # out before the request body is read (403, 404, 405, a 400 for the wrong Content-Type), and only
    # oversized bodies are drained. With keep-alive, such an unread body would be parsed as the next
    # request on the connection, so HTTP/1.1 needs every path to drain the body first (SEC-M1).
    protocol_version = "HTTP/1.0"
    # Seconds a client gets to send its request (and to take each chunk of the response) before the
    # connection is dropped, so a stalled or slow-dripping client can't hold a thread forever.
    timeout = 15

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
            if self.command:
                what = f"{self.command} {getattr(self, 'path', '')}"
            else:  # a request line the base class couldn't parse: show it (escaped, cut short)
                what = repr(getattr(self, "requestline", "")[:80]) or "(no request line)"
            sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {what} -> {code}\n")

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
        # Other sites can't embed our responses (e.g. <script src>) or keep a handle on our window.
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj, headers: dict | None = None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", headers)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """The base class calls this for requests it can't parse or route: a malformed request line, an
        oversized header, an unsupported method or HTTP version. Answer in JSON with the usual security
        headers, like every other error, instead of its HTML page."""
        self._no_http09()
        self.close_connection = True
        try:
            phrase = HTTPStatus(code).phrase
        except ValueError:
            phrase = "Error"
        with contextlib.suppress(OSError):
            self._error(code, message or phrase)

    def _error(
        self,
        status: int,
        message: str,
        details: list[config_mod.ValidationError] | None = None,
        headers: dict[str, str] | None = None,
    ):
        obj: dict[str, object] = {"error": message}
        if details is not None:
            obj["details"] = [d.to_dict() for d in details]
        self._json(status, obj, headers)

    def _request_host(self) -> str | None:
        """The Host header (lower-cased) if it names this server, else None: DNS-rebinding protection."""
        # For an HTTP/0.9 request CPython 3.13.15+ and 3.14.4+ set headers to a plain {} (gh-70765).
        get_all = getattr(self.headers, "get_all", None)
        values = (get_all("Host") if get_all else None) or []
        if len(values) != 1:
            return None
        host = values[0].strip().lower()
        if not host:
            return None
        if host.startswith("["):
            end = host.find("]")
            if end < 0:
                return None
            name, rest = host[: end + 1], host[end + 1 :]
        else:
            name, sep, port = host.partition(":")
            rest = f":{port}" if sep else ""
        if rest:
            port = rest[1:]
            if not rest.startswith(":") or not _PORT_RE.fullmatch(port):
                return None
            if int(port) != self.server.server_address[1]:
                return None
        if name in self.server.allowed_hosts:
            return host
        if self.server.any_ip_host:
            with contextlib.suppress(ValueError):
                ipaddress.ip_address(name.strip("[]"))
                return host
        return None

    def _origin_ok(self, host: str) -> bool:
        """Browsers send Origin with every request that isn't a GET or HEAD, and a page can't remove or
        forge it, so a cross-site one is refused. A request without Origin is not from a browser page."""
        origins = self.headers.get_all("Origin") or []
        if not origins:
            return True
        return len(origins) == 1 and origins[0].strip().lower() == f"http://{host}"

    def _no_http09(self) -> None:
        """Answer with a status line and headers even where the base class would answer the HTTP/0.9 way.

        The base class omits both when request_version is "HTTP/0.9": for a real HTTP/0.9 request, and
        on CPython before 3.13.15 and 3.14.7 (gh-54930) also for a malformed request line, whose version
        it never sets (Ubuntu 26.04 ships 3.14.4). No client of this server speaks HTTP/0.9, and every
        answer should carry the security headers.
        """
        if getattr(self, "request_version", "") == "HTTP/0.9":
            self.request_version = ""

    def _dispatch(self, method: str):
        self._no_http09()
        try:
            host = self._request_host()
            if host is None:
                raise HTTPError(403, "Forbidden: this server only answers requests for localhost")
            if method != "GET" and not self._origin_ok(host):  # HEAD is dispatched as GET
                raise HTTPError(403, "Forbidden: cross-origin request")
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
        # The client went away, or stalled for longer than `timeout`: there is no one to answer.
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except (
            Exception
        ) as exc:  # pragma: no cover - defensive; the exception goes to the log, not the client
            self.server.log_line(f"internal error on {method} {self.path}: {exc!r}")
            with contextlib.suppress(OSError):
                self._error(500, "Internal server error")

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
            raise HTTPError(400, "Malformed JSON", [_problem("malformed_json", str(exc)[:200])]) from None

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
        with self.server.service.config_lock:
            try:
                cfg = config_mod.load_config(
                    self.server.config_path, strict=False, detect=self.server.detect_fn
                )
            except config_mod.ConfigError as exc:
                raise HTTPError(500, "Cannot read config", exc.errors) from None
        self._json(200, _config_response(cfg))

    def h_put_config(self):
        body = self._read_json(required=True)
        if not isinstance(body, dict):
            raise HTTPError(400, "Invalid config", config_mod.validate_config(body))
        with self.server.service.config_lock:
            try:
                saved = config_mod.save_config(body, self.server.config_path)
            except config_mod.ConfigWriteError as exc:  # disk problem, not bad input
                raise HTTPError(500, "Cannot save config", exc.errors) from None
            except config_mod.ConfigError as exc:
                raise HTTPError(400, "Invalid config", exc.errors) from None
        self._json(200, _config_response(saved))

    def h_reset_config(self):
        self._read_json(required=False)
        with self.server.service.config_lock:
            try:
                cfg, _ = config_mod.reset_config(self.server.config_path, self.server.detect_fn)
            except config_mod.ConfigWriteError as exc:
                raise HTTPError(500, "Cannot save config", exc.errors) from None
        self._json(200, _config_response(cfg))

    def h_validate_config(self):
        """Check a Settings draft without saving it: the normalised config, its problems and its cost.

        The draft may be raw form input: servers and domains as text, numbers as strings.
        ``duplicate_domains`` counts the entries normalising dropped as repeats.
        """
        body = self._read_json(required=True)
        if not isinstance(body, dict):
            raise HTTPError(400, "Invalid config", config_mod.validate_config(body))
        cfg = config_mod.normalize_config(body)
        self._json(200, {**_config_response(cfg), "duplicate_domains": config_mod.duplicate_domains(body)})

    def h_estimate(self):
        """The cost of a run: of ``config`` in the body if given, else of the saved config; ``rounds``
        overrides its rounds, as POST /api/run does."""
        body = self._read_json(required=False) or {}
        if not isinstance(body, dict):
            raise HTTPError(400, "Body must be a JSON object")
        rounds = _rounds_param(body)
        cfg = body.get("config")
        if cfg is None:
            with self.server.service.config_lock:
                try:
                    cfg = config_mod.load_config(
                        self.server.config_path, strict=False, detect=self.server.detect_fn
                    )
                except config_mod.ConfigError as exc:
                    raise HTTPError(500, "Cannot read config", exc.errors) from None
        self._json(200, service_mod.estimate(cfg, rounds))

    def h_schema(self):
        self._json(200, api_schema())

    def h_system_resolver(self):
        """This computer's resolvers as a "System" entry for the Settings draft in the body. Saves nothing:
        the UI puts the entry into the draft, and the user saves it like any other edit."""
        body = self._read_json(required=True)
        if not isinstance(body, dict):
            raise HTTPError(400, "Body must be a JSON object")
        system = config_mod.system_resolver(body.get("resolvers"), self.server.detect_fn())
        self._json(
            200,
            {
                "resolver": system.resolver,
                "message": system.message,
                "detected": system.detected.servers,
                "source": system.detected.source,
            },
        )

    def h_info(self):
        """Where this server keeps its data, for the Settings page."""
        self._json(
            200,
            {
                "version": __version__,
                "config_path": str(self.server.config_path),
                "config_exists": self.server.config_path.is_file(),
                "runs_dir": str(self.server.runs_dir),
            },
        )

    # -- runs ----------------------------------------------------------------
    def _load_run(self, run_id: str) -> dict:
        run_id = unquote(run_id)
        if not storage.valid_run_id(run_id):
            raise HTTPError(400, f"Invalid run id: {run_id}")
        try:
            return dict(self.server.service.analysis.load(run_id))
        except storage.RunNotFound:
            raise HTTPError(404, f"Run {run_id} not found") from None
        except storage.CorruptRun as exc:
            raise HTTPError(500, str(exc)) from None

    def h_runs(self):
        self._json(200, {"runs": self.server.service.analysis.list_runs()})

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
        repo = self.server.service.repo
        ids: list[str] | Literal["all"] = "all"
        if spec not in ("", "all"):
            ids = [p.strip() for p in spec.split(",") if p.strip()]
            bad = [i for i in ids if not storage.valid_run_id(i)]
            if bad:
                raise HTTPError(
                    400,
                    "Invalid run id",
                    [_problem("invalid_run_id", f"not a run id: {i!r}", "runs") for i in bad],
                )
            missing = [i for i in ids if not repo.exists(i)]
            if missing:
                raise HTTPError(
                    404, "Run not found", [_problem("not_found", f"no run {i}", "runs") for i in missing]
                )
        with self.server.service.config_lock:
            current = config_mod.current_resolver_names(self.server.config_path)
        try:
            bundle = self.server.service.analysis.aggregate(ids, current=current)
        except storage.NoRuns:
            raise HTTPError(404, "No runs saved yet") from None
        except storage.RunNotFound as exc:  # deleted since the check above
            raise HTTPError(404, "Run not found", [_problem("not_found", str(exc), "runs")]) from None
        except storage.CorruptRun as exc:
            raise HTTPError(500, str(exc)) from None
        self._json(200, bundle)

    # -- benchmark job ---------------------------------------------------------
    def h_start_run(self):
        body = self._read_json(required=False) or {}
        if not isinstance(body, dict):
            raise HTTPError(400, "Body must be a JSON object")
        try:
            total = self.server.jobs.start(service_mod.Overrides(rounds=_rounds_param(body)))
        except service_mod.JobBusy:
            raise HTTPError(409, "A benchmark is already running") from None
        except config_mod.ConfigWriteError as exc:  # creating the config failed: a disk problem
            raise HTTPError(500, "Cannot save config", exc.errors) from None
        except service_mod.InvalidRun as exc:  # more rounds can take a run past its limits
            raise HTTPError(400, "Invalid run settings", exc.errors) from None
        except config_mod.ConfigError as exc:
            raise HTTPError(400, "Invalid config", exc.errors) from None
        except service_mod.RunsDirUnwritable as exc:  # before any DNS traffic, like the CLI
            detail = _problem("runs_dir_unwritable", f"cannot write to the runs folder: {exc.reason}")
            raise HTTPError(500, "Cannot save runs", [detail]) from None
        self._json(202, {"job": "started", "total": total})

    def h_cancel_run(self):
        self._read_json(required=False)
        if not self.server.jobs.cancel():
            raise HTTPError(409, "No benchmark is running")
        self._json(200, {"cancelled": True})

    def h_status(self):
        self._json(200, self.server.jobs.status())


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def make_server(
    host: str,
    port: int,
    config_path,
    runs_dir,
    query_fn=None,
    web_dir=None,
    quiet: bool = True,
    detect_fn: config_mod.Detect | None = None,
) -> DNSBenchServer:
    return DNSBenchServer(
        host,
        port,
        config_path,
        runs_dir,
        query_fn=query_fn,
        web_dir=web_dir,
        quiet=quiet,
        detect_fn=detect_fn,
    )


def is_loopback_host(host: str) -> bool:
    """True if a server bound to ``host`` accepts connections only from this machine."""
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip().strip("[]")).is_loopback
    except ValueError:  # "", a hostname, or junk: can't tell, so assume other machines can reach it
        return False


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
    host: str,
    port: int,
    config_path,
    runs_dir,
    open_browser: bool = False,
    query_fn=None,
    quiet: bool = True,
) -> None:
    """Run the web UI until Ctrl-C (KeyboardInterrupt propagates after cleanup).

    A missing config is created first (ConfigError if it can't be), so Settings shows, and the first
    run uses, one fixed System entry.
    """
    created = config_mod.ensure_config(config_path)
    httpd = make_server(host, port, config_path, runs_dir, query_fn=query_fn, quiet=quiet)
    url = server_url(httpd)
    print(f"DNS Bench UI: {url}  (Ctrl-C to stop)", flush=True)
    print(f"Config: {config_path}{' (created with the defaults)' if created else ''}", file=sys.stderr)
    if created:
        print(f"        {created.message}", file=sys.stderr)
    print(f"Runs:   {runs_dir}", file=sys.stderr, flush=True)
    if not is_loopback_host(host):
        print(
            "warning: listening on an address other machines can reach, with no authentication. Only "
            "requests addressed to an IP address or a loopback name are answered, not to host names.",
            file=sys.stderr,
        )
    if open_browser:
        import webbrowser

        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever(poll_interval=0.25)
    finally:
        try:
            if httpd.jobs.running:
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
