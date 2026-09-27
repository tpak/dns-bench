from __future__ import annotations

import contextlib
import csv
import http.client
import io
import json
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from dnsbench import __version__, recommend, service, storage, sysdns
from dnsbench import config as C
from dnsbench import server as SV
from dnsbench.resolver import QueryResult

SECRET = "TOP-SECRET-DO-NOT-SERVE"
ROOT = Path(__file__).resolve().parents[1]
FAKE_SYSTEM = sysdns.Detected(["192.0.2.53"], "a test")  # what "this computer's resolvers" are in these tests


def small_config(domains=3, interval_ms=50):
    cfg = C.default_config()
    cfg["resolvers"] = [
        {"name": "Fast", "servers": ["192.0.2.1"], "enabled": True},
        {"name": "Slow", "servers": ["192.0.2.2"], "enabled": True},
        {"name": "Off", "servers": ["192.0.2.3"], "enabled": False},
    ]
    cfg["domains"] = [f"d{i}.example" for i in range(domains)]
    cfg["settings"]["per_server_interval_ms"] = interval_ms
    cfg["settings"]["slow_threshold_ms"] = 30
    return cfg


class FakeQuery:
    def __init__(self, latency=0.001):
        self.latency = latency
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, server, domain, record_type="A", timeout_s=1.0, tries=1):
        with self.lock:
            self.calls += 1
        time.sleep(self.latency)
        if domain == "d2.example" and server == "192.0.2.2":
            return QueryResult("timeout", error="timeout", attempts=1)
        ms = 40.0 if server == "192.0.2.2" else 4.0
        return QueryResult("ok", ms=ms, rcode="NOERROR", answers=1, attempts=1)


class ServerTestBase(unittest.TestCase):
    web_files = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg_path = root / "config.json"
        self.runs_dir = root / "runs"
        self.web_dir = root / "web"
        self.web_dir.mkdir()
        (root / "secret.txt").write_text(SECRET)
        (self.web_dir / ".hidden").write_text(SECRET)
        if self.web_files:
            (self.web_dir / "index.html").write_text("<!doctype html><title>DNS Bench</title>")
            (self.web_dir / "app.js").write_text("console.log('hi');")
            (self.web_dir / "style.css").write_text("body{}")
            (self.web_dir / "sub").mkdir()
            (self.web_dir / "sub" / "x.svg").write_text("<svg/>")
        C.save_config(small_config(), self.cfg_path)
        self.fake = FakeQuery()
        self.srv = SV.make_server(
            "127.0.0.1",
            0,
            self.cfg_path,
            self.runs_dir,
            query_fn=self.fake,
            web_dir=self.web_dir,
            detect_fn=lambda: FAKE_SYSTEM,
        )
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(
            target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()

    def tearDown(self):
        self.srv.stop_job(wait_s=5)
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(2)
        self.tmp.cleanup()

    # -- helpers --------------------------------------------------------------
    def req(self, method, path, body=None, headers=None, raw=None, json_body=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {}
        data = None
        if raw is not None:
            data = raw
        elif body is not None:
            data = json.dumps(body).encode()
        if json_body and method in ("POST", "PUT"):
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        hdrs = {k: v for k, v in hdrs.items() if v is not None}
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        content = resp.read()
        headers_out = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, headers_out, content

    def raw_request(self, data: bytes) -> bytes:
        """Send bytes as-is (for requests http.client won't build) and return the whole response."""
        chunks = []
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(data)
            with contextlib.suppress(ConnectionResetError):  # a close with unread input may end in a reset
                while chunk := sock.recv(65536):
                    chunks.append(chunk)
        return b"".join(chunks)

    def jreq(self, method, path, body=None, **kw):
        status, headers, content = self.req(method, path, body, **kw)
        self.assertTrue(headers["content-type"].startswith("application/json"), headers)
        self.assertEqual(headers.get("cache-control"), "no-store")
        return status, json.loads(content)

    def wait_idle(self, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, st = self.jreq("GET", "/api/status")
            if not st["running"]:
                return st
            time.sleep(0.02)
        self.fail("job did not finish")

    def run_job(self, body=None):
        status, data = self.jreq("POST", "/api/run", body)
        self.assertEqual(status, 202, data)
        return data, self.wait_idle()


class StaticTest(ServerTestBase):
    def test_index(self):
        status, headers, body = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertIn(b"DNS Bench", body)
        self.assertEqual(self.req("GET", "/index.html")[0], 200)

    def test_static_content_types(self):
        for path, ctype in (
            ("/static/app.js", "application/javascript"),
            ("/static/style.css", "text/css"),
            ("/static/index.html", "text/html"),
            ("/static/sub/x.svg", "image/svg+xml"),
        ):
            with self.subTest(path=path):
                status, headers, _ = self.req("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(headers["content-type"].startswith(ctype), headers["content-type"])
                self.assertEqual(headers["cache-control"], "no-store")
                self.assertEqual(headers["x-content-type-options"], "nosniff")

    def test_head(self):
        status, headers, body = self.req("HEAD", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertIn("content-security-policy", headers)
        self.assertEqual(self.req("HEAD", "/api/status")[0], 200)

    def test_static_query_string_ignored(self):
        self.assertEqual(self.req("GET", "/static/app.js?v=123")[0], 200)

    def test_static_missing(self):
        status, _, _ = self.req("GET", "/static/nope.js")
        self.assertEqual(status, 404)

    def test_static_traversal_rejected(self):
        for path in (
            "/static/../secret.txt",
            "/static/%2e%2e/secret.txt",
            "/static/..%2fsecret.txt",
            "/static/%2e%2e%2fsecret.txt",
            "/static/sub/../../secret.txt",
            "/static/sub/%2e%2e/%2e%2e/secret.txt",
            "/static//etc/passwd",
            "/static/%2fetc%2fpasswd",
            "/static/.hidden",
            "/static/%2ehidden",
            "/static/app.js%00.css",
            "/static/..%5csecret.txt",
            "/static/./app.js",
            "/static/sub",
            "/static/",
        ):
            with self.subTest(path=path):
                status, _, body = self.req("GET", path)
                self.assertIn(status, (403, 404))
                self.assertNotIn(SECRET.encode(), body)

    def test_favicon(self):
        self.assertEqual(self.req("GET", "/favicon.ico")[0], 204)


class NoUiFilesTest(ServerTestBase):
    web_files = False

    def test_index_missing_is_404_json(self):
        status, data = self.jreq("GET", "/")
        self.assertEqual(status, 404)
        self.assertIn("UI files not found", data["error"])


class SecurityTest(ServerTestBase):
    def test_host_header_check(self):
        port = self.port
        for host, expected in (
            (f"127.0.0.1:{port}", 200),
            (f"localhost:{port}", 200),
            (f"LOCALHOST:{port}", 200),
            (f"[::1]:{port}", 200),
            ("localhost", 200),
            ("evil.com", 403),
            (f"evil.com:{port}", 403),
            (f"127.0.0.1.evil.com:{port}", 403),
            (f"127.0.0.1:{port + 1}", 403),
            (f"localhost:{port}x", 403),
            ("[::1", 403),
            ("", 403),
        ):
            with self.subTest(host=host):
                status, _, body = self.req("GET", "/api/config", headers={"Host": host})
                self.assertEqual(status, expected, body)
        status, data = self.jreq("GET", "/", headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)
        self.assertIn("error", data)

    def test_cross_origin_state_changes_are_refused(self):
        port = self.port
        for method, path, body in (
            ("PUT", "/api/config", small_config()),
            ("POST", "/api/config/reset", {}),
            ("POST", "/api/run", {}),
            ("POST", "/api/run/cancel", {}),
            ("OPTIONS", "/api/config", None),
            ("DELETE", "/api/config", None),
        ):
            for origin in (
                "http://evil.example",
                "null",  # sandboxed iframes and file:// pages
                f"http://127.0.0.1:{port + 1}",
                f"https://127.0.0.1:{port}",
                f"http://localhost:{port}",  # this server, but not the name the request was sent to
                f"http://127.0.0.1:{port}.evil.example",
            ):
                with self.subTest(method=method, path=path, origin=origin):
                    status, headers, content = self.req(method, path, body, headers={"Origin": origin})
                    self.assertEqual(status, 403, content)
                    self.assertEqual(json.loads(content)["error"], "Forbidden: cross-origin request")
                    self.assertFalse([h for h in headers if h.startswith("access-control-")], headers)
        # nothing was started or changed
        self.assertEqual(self.fake.calls, 0)
        self.assertEqual(C.load_config(self.cfg_path), C.normalize_config(small_config()))

    def test_same_origin_and_originless_state_changes_are_allowed(self):
        cfg = small_config()
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}", "localhost"):
            for origin in (f"http://{host}", f"HTTP://{host.upper()}"):
                with self.subTest(host=host, origin=origin):
                    status, data = self.jreq(
                        "PUT", "/api/config", cfg, headers={"Host": host, "Origin": origin}
                    )
                    self.assertEqual(status, 200, data)
        # curl and scripts send no Origin; a web page can't leave it out
        self.assertEqual(self.jreq("PUT", "/api/config", cfg)[0], 200)
        # A GET has no side effects: it is answered even cross-origin, but without CORS headers the
        # browser never lets the other site read the response.
        status, headers, _ = self.req("GET", "/api/config", headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 200)
        self.assertFalse([h for h in headers if h.startswith("access-control-")], headers)

    def test_duplicate_host_or_origin_headers_are_refused(self):
        port = self.port
        body = b"{}"
        for extra in (
            "Host: evil.example\r\n",
            f"Origin: http://127.0.0.1:{port}\r\nOrigin: http://evil.example\r\n",
        ):
            with self.subTest(extra=extra):
                resp = self.raw_request(
                    (
                        f"POST /api/run/cancel HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{extra}"
                        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
                    ).encode()
                    + body
                )
                self.assertTrue(resp.startswith(b"HTTP/1.0 403 "), resp[:80])

    def test_host_port_must_be_ascii_digits(self):
        # int() rejects both of these, which used to turn into a 500
        for host in (
            "localhost:\u00b2",
            "localhost:" + "9" * 5000,
            "localhost:+80",
            f"localhost:{self.port}0",
        ):
            with self.subTest(host=host[:20]):
                status, data = self.jreq("GET", "/api/status", headers={"Host": host})
                self.assertEqual(status, 403, data)

    def test_malformed_requests_get_json_errors_with_security_headers(self):
        host = f"Host: 127.0.0.1:{self.port}\r\n"
        for request, code in (
            (f"GET / HTTP/9.9\r\n{host}\r\n", 505),
            (f"GET / FOO HTTP/1.1\r\n{host}\r\n", 400),
            (f"BREW /pot HTTP/1.1\r\n{host}\r\n", 501),
        ):
            with self.subTest(request=request.split("\r\n")[0]):
                head, _, body = self.raw_request(request.encode()).partition(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                self.assertTrue(lines[0].startswith(f"HTTP/1.0 {code} "), lines[0])
                headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
                self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertEqual(headers["cross-origin-resource-policy"], "same-origin")
                self.assertTrue(json.loads(body)["error"])

    def test_one_request_per_connection(self):
        host = f"Host: 127.0.0.1:{self.port}\r\n"
        # HTTP/1.0 even when the client asks for keep-alive: raw_request reads until the server closes.
        resp = self.raw_request(f"GET /api/status HTTP/1.1\r\n{host}Connection: keep-alive\r\n\r\n".encode())
        self.assertTrue(resp.startswith(b"HTTP/1.0 200 "), resp[:40])
        # A body the server never reads (the 405 goes out first) is never parsed as a second request.
        smuggled = (
            f"POST /api/config/reset HTTP/1.1\r\n{host}Content-Type: application/json\r\n"
            "Content-Length: 2\r\n\r\n{}"
        )
        resp = self.raw_request(
            f"POST /api/status HTTP/1.1\r\n{host}Content-Type: application/json\r\n"
            f"Content-Length: {len(smuggled)}\r\n\r\n{smuggled}".encode()
        )
        self.assertTrue(resp.startswith(b"HTTP/1.0 405 "), resp[:40])
        self.assertEqual(resp.count(b"HTTP/1.0 "), 1)
        self.assertEqual(C.load_config(self.cfg_path), C.normalize_config(small_config()))  # not reset

    def test_stalled_clients_are_dropped(self):
        self.assertEqual(SV.Handler.timeout, 15)
        host = f"Host: 127.0.0.1:{self.port}\r\n"
        with mock.patch.object(SV.Handler, "timeout", 0.3):
            for partial in (
                f"GET /api/status HTTP/1.1\r\n{host}",  # the headers never end
                f"PUT /api/config HTTP/1.1\r\n{host}Content-Type: application/json\r\n"
                'Content-Length: 100\r\n\r\n{"a"',  # the body never arrives
            ):
                with self.subTest(partial=partial.split("\r\n")[0]):
                    t0 = time.monotonic()
                    resp = self.raw_request(partial.encode())
                    self.assertLess(time.monotonic() - t0, 5)
                    self.assertEqual(resp, b"")  # dropped: no response, and no 500
        self.assertEqual(self.jreq("GET", "/api/status")[0], 200)
        self.assertEqual(C.load_config(self.cfg_path), C.normalize_config(small_config()))

    def test_options_is_405_without_cors_headers(self):
        status, headers, _ = self.req("OPTIONS", "/api/config")
        self.assertEqual(status, 405)
        self.assertFalse([h for h in headers if h.startswith("access-control-")], headers)

    def test_cross_origin_isolation_headers_on_every_response(self):
        for method, path, extra in (
            ("GET", "/", None),
            ("GET", "/static/app.js", None),
            ("GET", "/api/status", None),
            ("GET", "/api/nope", None),
            ("GET", "/api/config", {"Host": "evil.example"}),
            ("PUT", "/api/config", {"Origin": "http://evil.example"}),
        ):
            with self.subTest(method=method, path=path):
                _, headers, _ = self.req(method, path, {} if method == "PUT" else None, headers=extra)
                self.assertEqual(headers["cross-origin-resource-policy"], "same-origin")
                self.assertEqual(headers["cross-origin-opener-policy"], "same-origin")

    def test_state_changing_requires_json_content_type(self):
        for method, path in (
            ("PUT", "/api/config"),
            ("POST", "/api/config/reset"),
            ("POST", "/api/run"),
            ("POST", "/api/run/cancel"),
        ):
            for ctype in (
                None,
                "text/plain",
                "application/x-www-form-urlencoded",
                "multipart/form-data; boundary=x",
            ):
                with self.subTest(path=path, ctype=ctype):
                    status, data = self.jreq(
                        method, path, raw=b"{}", json_body=False, headers={"Content-Type": ctype}
                    )
                    self.assertEqual(status, 400)
                    self.assertIn("Content-Type", data["error"])
        # nothing was started or changed
        self.assertEqual(self.fake.calls, 0)
        self.assertEqual(C.load_config(self.cfg_path), C.normalize_config(small_config()))

    def test_body_limit(self):
        cfg = small_config()
        cfg["padding"] = "x" * (C_MB + 10)
        status, data = self.jreq("PUT", "/api/config", cfg)
        self.assertEqual(status, 413)
        self.assertIn("too large", data["error"])

    def test_unknown_route_and_method(self):
        status, data = self.jreq("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "Not found")
        status, headers, _ = self.req("DELETE", "/api/config")
        self.assertEqual(status, 405)
        self.assertEqual(headers["allow"], "GET, PUT")
        status, headers, _ = self.req("GET", "/api/run")
        self.assertEqual(status, 405)
        self.assertEqual(headers["allow"], "POST")
        self.assertEqual(self.req("POST", "/api/status", {})[0], 405)
        self.assertEqual(self.req("DELETE", "/api/runs/20260101T000000Z")[0], 405)


C_MB = 1024 * 1024


class ConfigApiTest(ServerTestBase):
    def test_get_config_with_its_problems_and_estimate(self):
        status, data = self.jreq("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(set(data), {"config", "errors", "estimate"})
        self.assertEqual(data["config"], C.normalize_config(small_config()))
        self.assertEqual(data["errors"], [])
        self.assertEqual(data["estimate"], service.estimate(small_config()))
        self.assertEqual(data["estimate"]["queries"], 6)  # 2 enabled servers x 3 domains

    def test_put_valid_config_normalises_and_saves(self):
        cfg = small_config()
        cfg["domains"] = ["  Example.COM. ", "example.com", "", "b.org"]
        cfg["resolvers"][0]["servers"] = "192.0.2.1, 2001:0db8::0001"
        status, data = self.jreq("PUT", "/api/config", cfg)
        self.assertEqual(status, 200, data)
        saved = data["config"]
        self.assertEqual(saved["domains"], ["example.com", "b.org"])
        self.assertEqual(saved["resolvers"][0]["servers"], ["192.0.2.1", "2001:db8::1"])
        self.assertEqual((data["errors"], data["estimate"]["domains"]), ([], 2))
        on_disk = json.loads(self.cfg_path.read_text())
        self.assertEqual(on_disk, saved)
        self.assertEqual(self.jreq("GET", "/api/config")[1], data)

    def test_put_charset_content_type_ok(self):
        status, _ = self.jreq(
            "PUT", "/api/config", small_config(), headers={"Content-Type": "application/json; charset=utf-8"}
        )
        self.assertEqual(status, 200)

    def test_put_invalid_config(self):
        cfg = small_config()
        cfg["resolvers"][0]["servers"] = ["not-an-ip"]
        cfg["settings"]["per_server_interval_ms"] = 10
        before = self.cfg_path.read_text()
        status, data = self.jreq("PUT", "/api/config", cfg)
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "Invalid config")
        details = {d["path"]: d for d in data["details"]}
        self.assertEqual(details["resolvers[0].servers[0]"]["code"], "invalid")
        self.assertIn("not-an-ip", details["resolvers[0].servers[0]"]["message"])
        self.assertEqual(details["settings.per_server_interval_ms"]["code"], "out_of_range")
        self.assertEqual(self.cfg_path.read_text(), before)

    def test_put_bad_bodies(self):
        status, data = self.jreq("PUT", "/api/config", raw=b"{ nope")
        self.assertEqual((status, data["error"]), (400, "Malformed JSON"))
        status, data = self.jreq("PUT", "/api/config", raw=b"[1,2]")
        self.assertEqual((status, data["error"]), (400, "Invalid config"))
        status, data = self.jreq("PUT", "/api/config", raw=b"")
        self.assertEqual(status, 400)
        status, data = self.jreq("PUT", "/api/config", raw=b"\xff\xfe")
        self.assertEqual(status, 400)

    def test_reset(self):
        status, data = self.jreq("POST", "/api/config/reset")
        self.assertEqual(status, 200)
        expected = C.initial_config(lambda: FAKE_SYSTEM)[0]  # the defaults plus this computer's resolvers
        self.assertEqual(data["config"], expected)
        self.assertEqual(data["config"]["resolvers"][-1]["servers"], ["192.0.2.53"])
        self.assertEqual(C.load_config(self.cfg_path), expected)

    def test_missing_config_is_shown_but_not_created(self):
        self.cfg_path.unlink()
        status, data = self.jreq("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(data["config"], C.initial_config(lambda: FAKE_SYSTEM)[0])
        self.assertFalse(self.cfg_path.exists())  # a GET never writes (ARCH-4)
        self.assertFalse(self.jreq("GET", "/api/info")[1]["config_exists"])

    def test_info_says_where_the_data_is(self):
        status, data = self.jreq("GET", "/api/info")
        self.assertEqual(status, 200)
        self.assertEqual(
            data,
            {
                "version": __version__,
                "config_path": str(self.cfg_path),
                "config_exists": True,
                "runs_dir": str(self.runs_dir),
            },
        )

    def test_system_resolver_for_a_settings_draft(self):
        draft = small_config()
        status, data = self.jreq("POST", "/api/config/system-resolver", {"resolvers": draft["resolvers"]})
        self.assertEqual(status, 200)
        self.assertEqual(data["resolver"], {"name": "System", "servers": ["192.0.2.53"], "enabled": True})
        self.assertEqual(data["message"], "System: 192.0.2.53 (from a test).")
        self.assertEqual((data["detected"], data["source"]), (["192.0.2.53"], "a test"))
        self.assertEqual(C.load_config(self.cfg_path), C.normalize_config(small_config()))  # nothing saved
        # The draft already has that server: nothing to add
        draft["resolvers"][0]["servers"].append("192.0.2.53")
        status, data = self.jreq("POST", "/api/config/system-resolver", {"resolvers": draft["resolvers"]})
        self.assertEqual(status, 200)
        self.assertIsNone(data["resolver"])
        self.assertIn("already in the list as Fast", data["message"])
        # A half-edited draft is fine; a body that isn't an object is not
        self.assertEqual(self.jreq("POST", "/api/config/system-resolver", {"resolvers": "junk"})[0], 200)
        self.assertEqual(self.jreq("POST", "/api/config/system-resolver", [])[0], 400)

    def test_corrupt_config_file(self):
        self.cfg_path.write_text("{ broken")
        status, data = self.jreq("GET", "/api/config")
        self.assertEqual(status, 500)
        (detail,) = data["details"]
        self.assertEqual((detail["path"], detail["code"]), ("", "invalid_json"))
        self.assertIn("not valid JSON", detail["message"])
        self.assertNotIn(self.tmp.name, detail["message"])  # no absolute paths in API errors (SEC-8)
        status, data = self.jreq("POST", "/api/run")
        self.assertEqual(status, 400)
        # the UI can still fix it
        self.assertEqual(self.jreq("POST", "/api/config/reset")[0], 200)

    def test_unconvertible_numbers_are_400_not_500(self):
        body = small_config()
        for bad in ("--5", "9" * 5000, "\u00b2"):
            with self.subTest(bad=bad[:10]):
                body["settings"]["rounds"] = bad
                status, data = self.jreq("PUT", "/api/config", body)
                self.assertEqual((status, data["error"]), (400, "Invalid config"))
                self.assertIn("settings.rounds", [d["path"] for d in data["details"]])
        for raw in (b'{"settings": {"rounds": ' + b"9" * 5000 + b"}}", b"[" * 100000 + b"]" * 100000):
            with self.subTest(raw=raw[:10]):
                status, data = self.jreq("PUT", "/api/config", raw=raw)
                self.assertEqual((status, data["error"]), (400, "Malformed JSON"))

    def test_deeply_nested_values_are_400_not_500(self):
        # Python 3.14 parses nesting that 3.13 rejects; validating it then overflowed the stack (a 500).
        nested = b"[" * 100000 + b"]" * 100000
        for method, path, raw in (
            ("PUT", "/api/config", b'{"settings": {"rounds": ' + nested + b"}}"),
            ("PUT", "/api/config", b'{"domains": ' + nested + b"}"),
            ("POST", "/api/run", b'{"settings": {"rounds": ' + nested + b"}}"),
        ):
            with self.subTest(path=path, raw=raw[:14]):
                status, data = self.jreq(method, path, raw=raw)
                self.assertEqual((status, data["error"]), (400, "Malformed JSON"))
                self.assertEqual(
                    data["details"],
                    [
                        {
                            "path": "",
                            "code": "malformed_json",
                            "message": f"nested more than {C.MAX_JSON_DEPTH} levels deep",
                        }
                    ],
                )
        self.assertFalse(self.jreq("GET", "/api/status")[1]["running"])

    def test_bad_number_in_config_file_keeps_settings_usable(self):
        bad = small_config()
        bad["settings"]["rounds"] = "--5"
        self.cfg_path.write_text(json.dumps(bad))
        status, data = self.jreq("GET", "/api/config")
        self.assertEqual((status, data["config"]["settings"]["rounds"]), (200, "--5"))
        self.assertEqual([d["path"] for d in data["errors"]], ["settings.rounds"])
        self.assertEqual(data["estimate"]["rounds"], 1)  # an unusable value counts as the default
        status, data = self.jreq("POST", "/api/run")
        self.assertEqual((status, data["error"]), (400, "Invalid config"))

    def test_config_write_failure_is_500_not_invalid_input(self):
        with mock.patch.object(
            C, "_atomic_write_text_raw", side_effect=PermissionError(13, "Permission denied")
        ):
            status, data = self.jreq("PUT", "/api/config", small_config())
            self.assertEqual((status, data["error"]), (500, "Cannot save config"))
            self.assertEqual(
                data["details"],
                [{"path": "", "code": "write_failed", "message": "cannot write the file: Permission denied"}],
            )
            status, data = self.jreq("POST", "/api/config/reset")
            self.assertEqual((status, data["error"]), (500, "Cannot save config"))
            self.cfg_path.unlink()  # first-run default creation fails too
            status, data = self.jreq("POST", "/api/run")
            self.assertEqual((status, data["error"]), (500, "Cannot save config"))
        self.assertEqual(self.fake.calls, 0)

    def test_an_absurd_number_in_the_config_file_is_shown_not_a_500(self):
        bad = small_config()
        bad["settings"]["rounds"] = 10**400
        self.cfg_path.write_text(json.dumps(bad))
        status, data = self.jreq("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(
            [(d["path"], d["code"]) for d in data["errors"]], [("settings.rounds", "out_of_range")]
        )
        self.assertEqual(data["estimate"]["rounds"], 10)  # clamped to the limit
        self.assertEqual(self.jreq("POST", "/api/estimate", {"config": bad})[0], 200)

    def test_invalid_but_parseable_config_is_shown(self):
        bad = small_config()
        bad["settings"]["rounds"] = 99
        self.cfg_path.write_text(json.dumps(bad))
        status, data = self.jreq("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(data["config"]["settings"]["rounds"], 99)
        self.assertEqual(
            [(d["path"], d["code"]) for d in data["errors"]], [("settings.rounds", "out_of_range")]
        )
        status, data = self.jreq("POST", "/api/run")
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "Invalid config")
        # The same structured error, without the config file's path in front of it
        self.assertEqual(
            [(d["path"], d["code"]) for d in data["details"]], [("settings.rounds", "out_of_range")]
        )
        self.assertTrue(data["details"][0]["message"].startswith("settings.rounds: "))


class RunsApiTest(ServerTestBase):
    @mock.patch.dict(C.DEFAULT_CONFIG, {"domains": ["d0.example"]})  # 60 domains would take 15 s
    def test_a_run_creates_a_missing_config_first(self):
        self.cfg_path.unlink()
        _, st = self.run_job()
        self.assertEqual(st["last_status"], "complete")
        created = C.load_config(self.cfg_path)
        self.assertEqual(created, C.initial_config(lambda: FAKE_SYSTEM)[0])
        _, run = self.jreq("GET", f"/api/runs/{st['last_run_id']}")
        self.assertEqual(run["config"]["resolvers"], created["resolvers"])

    def test_empty_state(self):
        self.assertEqual(self.jreq("GET", "/api/runs"), (200, {"runs": []}))
        status, _ = self.jreq("GET", "/api/aggregate?runs=all")
        self.assertEqual(status, 404)
        status, st = self.jreq("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(st)
            >= {
                "running",
                "done",
                "total",
                "elapsed_s",
                "eta_s",
                "started_at",
                "last_run_id",
                "error",
                "recent",
            },
            True,
        )
        self.assertFalse(st["running"])
        self.assertIsNone(st["last_run_id"])
        self.assertIsNone(st["eta_s"])
        self.assertEqual(st["recent"], [])

    def test_full_run_flow(self):
        started, st = self.run_job({"rounds": 1})
        self.assertEqual(started, {"job": "started", "total": 6})  # 2 enabled servers x 3 domains
        self.assertIsNone(st["error"])
        self.assertEqual((st["done"], st["total"]), (6, 6))
        self.assertEqual(st["last_status"], "complete")
        self.assertEqual(st["failed"], 1)  # Slow/d2 timed out
        self.assertEqual(st["slow"], 2)  # Slow's other two answers are 40 ms > 30 ms
        self.assertEqual(len(st["recent"]), 6)
        run_id = st["last_run_id"]
        self.assertTrue(storage.valid_run_id(run_id))

        status, data = self.jreq("GET", "/api/runs")
        self.assertEqual([r["id"] for r in data["runs"]], [run_id])
        self.assertEqual(data["runs"][0]["best"], "Fast")

        status, run = self.jreq("GET", f"/api/runs/{run_id}")
        self.assertEqual(status, 200)
        for key in ("id", "config", "results", "summary", "recommendation", "status"):
            self.assertIn(key, run)
        self.assertEqual(run["recommendation"]["best"], "Fast")
        self.assertEqual(run["summary"]["by_resolver"]["Slow"]["timeouts"], 1)
        self.assertEqual(len(run["results"]), 6)
        self.assertTrue((self.runs_dir / f"{run_id}.txt").exists())

        status, headers, body = self.req("GET", f"/api/runs/{run_id}/csv")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/csv"))
        self.assertIn("attachment", headers["content-disposition"])
        self.assertIn(run_id, headers["content-disposition"])
        rows = list(csv.reader(io.StringIO(body.decode())))
        self.assertEqual(
            rows[0],
            [
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
            ],
        )
        self.assertEqual(len(rows), 7)
        self.assertTrue(all(r[0] == run_id for r in rows[1:]))
        timeout_rows = [r for r in rows[1:] if r[5] == "timeout"]
        self.assertEqual(len(timeout_rows), 1)
        self.assertEqual(timeout_rows[0][6], "")  # ms empty, not 0

        status, agg = self.jreq("GET", "/api/aggregate?runs=all")
        self.assertEqual(status, 200)
        self.assertEqual(agg["run_ids"], [run_id])
        self.assertEqual(agg["recommendation"]["best"], "Fast")
        self.assertEqual(agg["summary"]["overall"]["n"], 6)
        status, agg2 = self.jreq("GET", f"/api/aggregate?runs={run_id}")
        self.assertEqual(status, 200)
        self.assertEqual(agg2["run_ids"], [run_id])
        self.assertEqual(self.jreq("GET", "/api/aggregate")[0], 200)  # defaults to all

        # second run -> aggregate over both
        _, st2 = self.run_job()
        self.assertNotEqual(st2["last_run_id"], run_id)
        status, agg = self.jreq("GET", "/api/aggregate?runs=all")
        self.assertEqual(len(agg["run_ids"]), 2)
        self.assertEqual(agg["run_ids"][0], st2["last_run_id"])  # newest first
        self.assertEqual(agg["summary"]["overall"]["n"], 12)
        status, agg = self.jreq("GET", f"/api/aggregate?runs={run_id},{st2['last_run_id']}")
        self.assertEqual((status, agg["summary"]["overall"]["n"]), (200, 12))

    def test_run_rounds_override(self):
        started, st = self.run_job({"rounds": 2})
        self.assertEqual(started["total"], 12)
        self.assertEqual(st["done"], 12)
        # override is for this run only
        self.assertEqual(C.load_config(self.cfg_path)["settings"]["rounds"], 1)
        _, run = self.jreq("GET", f"/api/runs/{st['last_run_id']}")
        self.assertEqual(run["config"]["settings"]["rounds"], 2)

    def test_run_bad_rounds(self):
        for body in ({"rounds": 0}, {"rounds": 11}, {"rounds": "3"}, {"rounds": True}, {"rounds": 1.5}, [1]):
            with self.subTest(body=body):
                status, data = self.jreq("POST", "/api/run", body)
                self.assertEqual(status, 400, data)
        self.assertEqual(self.jreq("POST", "/api/run", raw=b"{ nope")[0], 400)
        self.assertEqual(self.fake.calls, 0)

    def test_unwritable_runs_dir_is_refused_before_any_query(self):
        blocker = Path(self.tmp.name) / "not-a-dir"
        blocker.write_text("x")
        self.srv.service.repo = storage.RunRepository(blocker / "runs")
        status, data = self.jreq("POST", "/api/run")
        self.assertEqual(status, 500, data)
        self.assertEqual(data["error"], "Cannot save runs")
        self.assertEqual(data["details"][0]["code"], "runs_dir_unwritable")
        self.assertIn("cannot write to the runs folder", data["details"][0]["message"])
        self.assertNotIn(self.tmp.name, data["details"][0]["message"])
        self.assertEqual(self.fake.calls, 0)
        self.assertFalse(self.jreq("GET", "/api/status")[1]["running"])

    def test_a_run_that_cannot_be_saved_is_rescued(self):
        rescue_dir = Path(self.tmp.name) / "rescue"
        rescue_dir.mkdir()
        with (
            mock.patch.object(
                storage.RunRepository, "save", side_effect=OSError(28, "No space left on device")
            ),
            mock.patch.object(tempfile, "tempdir", str(rescue_dir)),
        ):
            _, st = self.run_job()
        self.assertFalse(st["running"])
        self.assertEqual(st["last_status"], "complete")
        self.assertIsNone(st["last_run_id"])  # nothing in runs/ to point the UI at
        self.assertIn("could not save run", st["error"])
        self.assertIn("No space left on device", st["error"])
        (rescued,) = rescue_dir.glob("dns-bench-*.json")
        self.assertIn(str(rescued), st["error"])
        record = json.loads(rescued.read_text(encoding="utf-8"))
        self.assertEqual(len(record["results"]), 6)
        self.assertIn("recommendation", record)

    def test_job_state_is_reset_whatever_happens(self):
        # SystemExit gets past `except Exception` (and the default thread excepthook ignores it): the
        # job must still end, or the UI would show a benchmark running forever and refuse new ones.
        with mock.patch.object(storage.RunRepository, "save", side_effect=SystemExit):
            _, st = self.run_job()
        self.assertFalse(st["running"])
        self.assertEqual(self.jreq("POST", "/api/run")[0], 202)  # a new run can start
        self.wait_idle()

    def test_rounds_override_cannot_exceed_the_query_limit(self):
        cfg = small_config(domains=500)
        cfg["resolvers"] = [
            {"name": f"R{i}", "servers": [f"192.0.2.{4 * i + j}" for j in range(1, 5)], "enabled": True}
            for i in range(3)
        ]
        C.save_config(cfg, self.cfg_path)  # 12 servers x 500 domains = 6,000 queries a round
        status, data = self.jreq("POST", "/api/run", {"rounds": 10})
        self.assertEqual(status, 400, data)
        self.assertEqual(data["error"], "Invalid run settings")
        self.assertEqual((data["details"][0]["path"], data["details"][0]["code"]), ("", "too_many_queries"))
        self.assertIn("the limit is 50,000", data["details"][0]["message"])
        self.assertEqual(self.fake.calls, 0)

    def test_a_crashed_run_is_saved_and_reported(self):
        def broken(server, domain, **kw):
            if server == "192.0.2.2":
                return {"status": "ok", "ms": "garbage"}
            return QueryResult("ok", ms=4.0, rcode="NOERROR", answers=1)

        self.srv.service.query_fn = broken
        _, st = self.run_job()
        self.assertEqual(st["last_status"], "partial")
        self.assertIn("stopped early after an internal error (ValueError", st["error"])
        self.assertIn("were saved", st["error"])
        _, run = self.jreq("GET", f"/api/runs/{st['last_run_id']}")
        self.assertEqual(run["status"], "partial")
        self.assertIn("192.0.2.2", run["error"])

    def test_conflict_and_cancel(self):
        C.save_config(small_config(domains=40, interval_ms=100), self.cfg_path)  # ~4 s run
        self.assertEqual(self.jreq("POST", "/api/run/cancel")[0], 409)
        status, data = self.jreq("POST", "/api/run")
        self.assertEqual(status, 202)
        self.assertEqual(data["total"], 80)
        status, data = self.jreq("POST", "/api/run")
        self.assertEqual(status, 409)
        self.assertIn("already running", data["error"])
        time.sleep(0.3)
        _, st = self.jreq("GET", "/api/status")
        self.assertTrue(st["running"])
        self.assertGreater(st["done"], 0)
        self.assertIsNotNone(st["eta_s"])
        self.assertIsNotNone(st["started_at"])
        self.assertGreater(st["elapsed_s"], 0)
        t0 = time.monotonic()
        status, data = self.jreq("POST", "/api/run/cancel")
        self.assertEqual((status, data), (200, {"cancelled": True}))
        st = self.wait_idle()
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(st["last_status"], "cancelled")
        self.assertLess(st["done"], 80)
        status, run = self.jreq("GET", f"/api/runs/{st['last_run_id']}")
        self.assertEqual(run["status"], "cancelled")  # partial run still saved
        self.assertEqual(self.jreq("POST", "/api/run/cancel")[0], 409)

    def test_stop_job_saves_the_partial_run(self):
        # JobManagerTest covers the waiting; this checks the server hands its job to it.
        cfg = small_config(domains=40)
        C.save_config(cfg, self.cfg_path)
        self.assertEqual(self.jreq("POST", "/api/run")[0], 202)
        time.sleep(0.1)
        self.assertTrue(self.srv.stop_job())
        self.assertEqual([r["status"] for r in self.srv.service.analysis.list_runs()], ["cancelled"])

    def test_recent_capped(self):
        C.save_config(small_config(domains=15, interval_ms=50), self.cfg_path)
        _, st = self.run_job()
        self.assertEqual(st["done"], 30)
        self.assertEqual(len(st["recent"]), 20)

    def test_bad_and_unknown_run_ids(self):
        for path in (
            "/api/runs/latest",
            "/api/runs/..%2f..%2fsecret",
            "/api/runs/2026",
            "/api/runs/latest/csv",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.jreq("GET", path)[0], 400)
        for path in ("/api/runs/20200101T000000Z", "/api/runs/20200101T000000Z-2/csv"):
            with self.subTest(path=path):
                self.assertEqual(self.jreq("GET", path)[0], 404)
        self.run_job()
        status, _ = self.jreq("GET", "/api/aggregate?runs=bogus")
        self.assertEqual(status, 400)
        status, _ = self.jreq("GET", "/api/aggregate?runs=20200101T000000Z")
        self.assertEqual(status, 404)

    def test_csv_formula_injection_neutralised(self):
        from dnsbench import runner

        cfg = small_config()
        cfg["resolvers"][0]["name"] = "=HYPERLINK(1)"
        run = runner.run_benchmark(cfg, query_fn=self.fake)
        self.srv.service.persist(run)
        status, _, body = self.req("GET", f"/api/runs/{run['id']}/csv")
        self.assertEqual(status, 200)
        rows = list(csv.reader(io.StringIO(body.decode())))
        self.assertIn("'=HYPERLINK(1)", [r[1] for r in rows])

    def test_corrupt_run_file(self):
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        (self.runs_dir / "20260101T000000Z.json").write_text("{ nope")
        import contextlib

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.jreq("GET", "/api/runs"), (200, {"runs": []}))
        self.assertEqual(self.jreq("GET", "/api/runs/20260101T000000Z")[0], 500)


class AggregateCurrentConfigTest(ServerTestBase):
    """The combined view recommends only resolvers enabled in the live config
    or measured in the newest run."""

    def _save(self, run_id, names_ms, enabled):
        cfg = small_config()
        for r in cfg["resolvers"]:
            r["enabled"] = r["name"] in enabled
        servers = {r["name"]: r["servers"][0] for r in cfg["resolvers"]}
        results = [
            {
                "resolver": n,
                "server": servers[n],
                "domain": d,
                "round": 1,
                "status": "ok",
                "ms": ms,
                "rcode": "NOERROR",
                "answers": 1,
                "error": None,
                "t": 0.0,
            }
            for n, ms in names_ms
            for d in cfg["domains"]
        ]
        started = f"{run_id[:4]}-{run_id[4:6]}-{run_id[6:8]}T00:00:00Z"
        self.srv.service.persist(
            {
                "id": run_id,
                "started_at": started,
                "finished_at": started,
                "duration_s": 1.0,
                "host": "h",
                "status": "complete",
                "config": cfg,
                "results": results,
            }
        )

    def test_resolver_only_in_old_runs_is_not_recommended(self):
        self._save("20260101T000000Z", [("Fast", 4.0), ("Slow", 40.0), ("Off", 1.0)], {"Fast", "Slow", "Off"})
        self._save("20260201T000000Z", [("Fast", 4.0), ("Slow", 40.0)], {"Fast", "Slow"})
        status, agg = self.jreq("GET", "/api/aggregate?runs=all")
        self.assertEqual(status, 200)
        self.assertEqual(agg["coverage"]["Off"], {"runs": 1, "of": 2, "last_run": "20260101T000000Z"})
        rec = agg["recommendation"]
        self.assertEqual(rec["ranking"][0]["resolver"], "Off")
        self.assertEqual((rec["best"], rec["backup"]), ("Fast", "Slow"))
        self.assertEqual(rec["suggested_servers"], ["192.0.2.1", "192.0.2.2"])
        # the user enables it again: the live config wins over the newest run's snapshot
        cfg = small_config()
        cfg["resolvers"][2]["enabled"] = True
        self.assertEqual(self.jreq("PUT", "/api/config", cfg)[0], 200)
        status, agg = self.jreq("GET", "/api/aggregate?runs=all")
        self.assertEqual(agg["recommendation"]["best"], "Off")
        _, runs = self.jreq("GET", "/api/runs")
        self.assertEqual(runs["runs"][0]["medians"], {"Fast": 4.0, "Slow": 40.0})


class SchemaAndDraftApiTest(ServerTestBase):
    def test_schema_is_the_python_rules(self):
        status, schema = self.jreq("GET", "/api/schema")
        self.assertEqual(status, 200)
        self.assertEqual(schema, SV.api_schema())
        self.assertEqual(schema["defaults"], C.default_config())
        self.assertEqual(schema["settings"], C.setting_schema())
        self.assertEqual(schema["presets"], C.PRESETS)
        self.assertEqual(
            schema["limits"],
            {
                "resolvers": C.MAX_RESOLVERS,
                "servers_per_resolver": C.MAX_SERVERS_PER_RESOLVER,
                "name_length": C.MAX_NAME_LEN,
                "domains": C.MAX_DOMAINS,
                "hostname_length": C.MAX_HOSTNAME_LEN,
                "queries_per_run": C.MAX_QUERIES_PER_RUN,
                "min_interval_ms": C.MIN_INTERVAL_MS,
                "request_bytes": SV.MAX_BODY,
            },
        )
        self.assertEqual(schema["error_codes"], C.ERROR_CODES)
        self.assertEqual(
            schema["scoring"]["weights"],
            {"median": recommend.W_MEDIAN, "p95": recommend.W_P95, "mean": recommend.W_MEAN},
        )
        self.assertEqual(schema["scoring"]["failure_weight"], recommend.FAILURE_WEIGHT)

    def test_validate_a_raw_settings_draft(self):
        draft = {
            "resolvers": [
                {"name": " Fast ", "servers": "192.0.2.1, 192.0.2.9", "enabled": True},
                {"name": "Slow", "servers": "192.0.2.9 not-an-ip", "enabled": True},
            ],
            "domains": "A.com\na.com.\nb.com\n-bad.com",
            "settings": {"rounds": "2", "timeout_ms": "", "shuffle": True},
        }
        before = self.cfg_path.read_text()
        status, data = self.jreq("POST", "/api/config/validate", draft)
        self.assertEqual(status, 200)
        self.assertEqual(
            data["config"]["resolvers"][0],
            {"name": "Fast", "servers": ["192.0.2.1", "192.0.2.9"], "enabled": True},
        )
        self.assertEqual(data["config"]["domains"], ["a.com", "b.com", "-bad.com"])
        self.assertEqual(data["config"]["settings"]["rounds"], 2)
        self.assertEqual(data["duplicate_domains"], 1)
        self.assertEqual(
            [(e["path"], e["code"]) for e in data["errors"]],
            [
                ("resolvers[1].servers[0]", "duplicate"),
                ("resolvers[1].servers[1]", "invalid"),
                ("domains[2]", "invalid"),
                ("settings.timeout_ms", "out_of_range"),
            ],
        )
        self.assertEqual((data["estimate"]["rounds"], data["estimate"]["domains"]), (2, 3))
        self.assertEqual(self.cfg_path.read_text(), before)  # nothing is saved
        status, data = self.jreq("POST", "/api/config/validate", [1])
        self.assertEqual((status, data["details"][0]["code"]), (400, "type"))

    def test_estimate(self):
        status, est = self.jreq("POST", "/api/estimate", {})
        self.assertEqual((status, est), (200, service.estimate(small_config())))
        status, est = self.jreq("POST", "/api/estimate", {"rounds": 3})
        self.assertEqual((est["rounds"], est["queries"]), (3, 18))
        other = {"resolvers": [{"name": "X", "servers": ["192.0.2.7"]}], "domains": ["a.com"]}
        status, est = self.jreq("POST", "/api/estimate", {"config": other})
        self.assertEqual((est["servers"], est["queries"]), (1, 1))
        status, data = self.jreq("POST", "/api/estimate", {"rounds": 11})
        self.assertEqual(status, 400)
        self.assertEqual([(d["path"], d["code"]) for d in data["details"]], [("rounds", "out_of_range")])

    def test_every_error_detail_is_structured(self):
        # One of each kind of error response that has details
        bad = small_config()
        bad["resolvers"][0]["servers"] = ["nope"]
        responses = [
            self.jreq("PUT", "/api/config", bad),
            self.jreq("PUT", "/api/config", raw=b"{ nope"),
            self.jreq("POST", "/api/run", {"rounds": 0}),
            self.jreq("GET", "/api/aggregate?runs=nope"),
            self.jreq("GET", "/api/aggregate?runs=20200101T000000Z"),
        ]
        for status, data in responses:
            with self.subTest(error=data.get("error")):
                self.assertGreaterEqual(status, 400)
                self.assertTrue(data["details"])
                for d in data["details"]:
                    self.assertEqual(set(d), {"path", "code", "message"})
                    self.assertTrue(all(isinstance(v, str) for v in d.values()))
                    self.assertNotIn(self.tmp.name, d["message"])


class ApiDocsTest(unittest.TestCase):
    def test_readme_lists_every_api_route(self):
        # README "JSON API" documents the API: every route in it exists, and every route is in it.
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        section = readme.split("## JSON API", 1)[1].split("\n## ", 1)[0]
        documented = set()
        for method, path in re.findall(r"`(GET|POST|PUT|DELETE) (/api/[^`? ]*)", section):
            documented.add((method, re.sub(r"<[a-z_]+>", "X", path)))
        routes = set()
        for pattern, methods in SV._ROUTES:
            path = re.sub(r"\(\?P<[a-z]+>[^)]*\)", "X", pattern.pattern.strip("^$")).replace("\\", "")
            if path.startswith("/api/"):
                routes |= {(method, path) for method in methods}
        self.assertEqual(documented, routes)


if __name__ == "__main__":
    unittest.main()
