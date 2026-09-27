from __future__ import annotations

import csv
import http.client
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from dnsbench import config as C
from dnsbench import server as SV
from dnsbench import storage
from dnsbench.resolver import QueryResult

SECRET = "TOP-SECRET-DO-NOT-SERVE"


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
            "127.0.0.1", 0, self.cfg_path, self.runs_dir, query_fn=self.fake, web_dir=self.web_dir
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
    def test_get_config_and_defaults(self):
        status, data = self.jreq("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(data["domains"], ["d0.example", "d1.example", "d2.example"])
        status, data = self.jreq("GET", "/api/defaults")
        self.assertEqual(status, 200)
        self.assertEqual(data, C.default_config())

    def test_put_valid_config_normalises_and_saves(self):
        cfg = small_config()
        cfg["domains"] = ["  Example.COM. ", "example.com", "", "b.org"]
        cfg["resolvers"][0]["servers"] = "192.0.2.1, 2001:0db8::0001"
        status, data = self.jreq("PUT", "/api/config", cfg)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["domains"], ["example.com", "b.org"])
        self.assertEqual(data["resolvers"][0]["servers"], ["192.0.2.1", "2001:db8::1"])
        on_disk = json.loads(self.cfg_path.read_text())
        self.assertEqual(on_disk, data)
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
        self.assertTrue(any("not-an-ip" in d for d in data["details"]))
        self.assertTrue(any("per_server_interval_ms" in d for d in data["details"]))
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
        self.assertEqual(data, C.default_config())
        self.assertEqual(C.load_config(self.cfg_path), C.default_config())

    def test_corrupt_config_file(self):
        self.cfg_path.write_text("{ broken")
        status, data = self.jreq("GET", "/api/config")
        self.assertEqual(status, 500)
        self.assertIn("not valid JSON", " ".join(data["details"]))
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
                self.assertTrue(any("settings.rounds" in d for d in data["details"]))
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
                self.assertEqual(data["details"], [f"nested more than {C.MAX_JSON_DEPTH} levels deep"])
        self.assertFalse(self.jreq("GET", "/api/status")[1]["running"])

    def test_bad_number_in_config_file_keeps_settings_usable(self):
        bad = small_config()
        bad["settings"]["rounds"] = "--5"
        self.cfg_path.write_text(json.dumps(bad))
        status, data = self.jreq("GET", "/api/config")
        self.assertEqual((status, data["settings"]["rounds"]), (200, "--5"))
        status, data = self.jreq("POST", "/api/run")
        self.assertEqual((status, data["error"]), (400, "Invalid config"))

    def test_config_write_failure_is_500_not_invalid_input(self):
        err = C.ConfigWriteError("cannot write config.json: Permission denied")
        with mock.patch.object(C, "_atomic_write_text", side_effect=err):
            status, data = self.jreq("PUT", "/api/config", small_config())
            self.assertEqual((status, data["error"]), (500, "Cannot save config"))
            self.assertIn("Permission denied", data["details"][0])
            status, data = self.jreq("POST", "/api/config/reset")
            self.assertEqual((status, data["error"]), (500, "Cannot save config"))
            self.cfg_path.unlink()  # first-run default creation fails too
            status, data = self.jreq("POST", "/api/run")
            self.assertEqual((status, data["error"]), (500, "Cannot save config"))
        self.assertEqual(self.fake.calls, 0)

    def test_invalid_but_parseable_config_is_shown(self):
        bad = small_config()
        bad["settings"]["rounds"] = 99
        self.cfg_path.write_text(json.dumps(bad))
        status, data = self.jreq("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(data["settings"]["rounds"], 99)
        status, data = self.jreq("POST", "/api/run")
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "Invalid config")
        self.assertTrue(data["details"])


class RunsApiTest(ServerTestBase):
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

    def test_stop_job_waits_for_an_in_flight_query(self):
        # A query can't be interrupted, so the shutdown wait must cover a whole
        # timeout (not a fixed 5 s) or the partial run is lost.
        srv = self.srv
        thread = mock.Mock()
        thread.is_alive.return_value = False
        with srv.job.lock:
            srv.job.running = True
            srv.job.cancel_event = threading.Event()
            srv.job.thread = thread
            srv.job.timeout_s = 8.0
        self.assertTrue(srv.stop_job())
        thread.join.assert_called_once_with(11.0)
        self.assertTrue(srv.job.cancel_event.is_set())
        srv.job.cancel_event.clear()
        srv.job.timeout_s = 1.0
        thread.join.reset_mock()
        srv.stop_job()
        thread.join.assert_called_once_with(5.0)  # never less than before
        with srv.job.lock:
            srv.job.running = False

    def test_stop_job_saves_partial_run(self):
        cfg = small_config(domains=20)
        cfg["settings"]["timeout_ms"] = 400
        C.save_config(cfg, self.cfg_path)

        def blocking(server, domain, record_type="A", timeout_s=1.0, tries=1):
            self.fake.calls += 1
            if server == "192.0.2.2":
                time.sleep(timeout_s)  # unresponsive server: blocks the full timeout
                return QueryResult("timeout", error="timeout", attempts=1)
            return QueryResult("ok", ms=4.0, rcode="NOERROR", answers=1, attempts=1)

        self.srv.query_fn = blocking
        self.assertEqual(self.jreq("POST", "/api/run")[0], 202)
        self.assertEqual(self.srv.job.timeout_s, 0.4)
        time.sleep(0.15)
        self.assertTrue(self.srv.stop_job())
        self.assertFalse(self.srv.job.running)
        rows = storage.list_runs(self.runs_dir)
        self.assertEqual([r["status"] for r in rows], ["cancelled"])

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
        storage.save_run(run, self.runs_dir)
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
        storage.save_run(
            {
                "id": run_id,
                "started_at": started,
                "finished_at": started,
                "duration_s": 1.0,
                "host": "h",
                "status": "complete",
                "config": cfg,
                "results": results,
            },
            self.runs_dir,
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


if __name__ == "__main__":
    unittest.main()
