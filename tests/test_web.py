"""Smoke tests for the real web UI (dnsbench/web), served by the real server.

test_server.py serves stub UI files, so a missing or misnamed asset in the real UI passes there. These
tests serve config.WEB_DIR itself. There is no JavaScript test runner (the UI has no Node tooling):
Biome, run by pre-commit and in CI, parses app.js, so a syntax error fails lint instead.
"""

from __future__ import annotations

import http.client
import re
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from pathlib import Path

from dnsbench import config as C
from dnsbench import server as SV

# Ways to turn a string into markup. The UI builds every element with createElement/textContent, and the
# CSP is the second line of defence, not the first.
HTML_SINKS = re.compile(
    r"""(?:\.|\[\s*["'])(?:inner|outer)HTML(?:["']\s*\])?\s*\+?=(?!=)"""
    r"|\binsertAdjacentHTML\s*\("
    r"|\bdocument\s*\.\s*write(?:ln)?\s*\("
)


class _Refs(HTMLParser):
    """Collects the same-origin URLs a page loads, and counts inline scripts."""

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.inline_scripts = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "script" and not a.get("src"):
            self.inline_scripts += 1
        for name in ("src", "href"):
            url = a.get(name)
            if url and url.startswith("/") and not url.startswith("//"):
                self.urls.append(url)


class RealWebUITest(unittest.TestCase):
    tmp: tempfile.TemporaryDirectory[str]
    srv: SV.DNSBenchServer
    port: int
    thread: threading.Thread

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        # No web_dir: the server falls back to the real config.WEB_DIR. Serving files touches neither the
        # config nor the runs dir, so both can point into an empty temp dir.
        cls.srv = SV.make_server("127.0.0.1", 0, root / "config.json", root / "runs")
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(
            target=cls.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(2)
        cls.tmp.cleanup()

    def get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def index_refs(self) -> _Refs:
        status, _, body = self.get("/")
        self.assertEqual(status, 200)
        refs = _Refs()
        refs.feed(body.decode("utf-8"))
        return refs

    def test_serves_the_real_ui_directory(self):
        self.assertEqual(self.srv.web_dir, C.WEB_DIR)
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(body, (C.WEB_DIR / "index.html").read_bytes())

    def test_index_has_a_strict_csp(self):
        _, headers, _ = self.get("/")
        csp = {
            d.split()[0]: d.split()[1:] for d in headers["content-security-policy"].split(";") if d.strip()
        }
        self.assertEqual(csp["default-src"], ["'self'"])
        self.assertEqual(csp["script-src"], ["'self'"])
        self.assertEqual(csp["object-src"], ["'none'"])
        self.assertEqual(csp["base-uri"], ["'none'"])
        self.assertEqual(csp["frame-ancestors"], ["'none'"])
        self.assertEqual(headers["x-content-type-options"], "nosniff")

    def test_every_asset_the_page_references_loads(self):
        urls = self.index_refs().urls
        self.assertIn("/static/app.js", urls)
        self.assertIn("/static/style.css", urls)
        types = {".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
        for url in urls:
            with self.subTest(url=url):
                self.assertTrue(url.startswith("/static/"), "the UI's files are served under /static/")
                status, headers, body = self.get(url)
                self.assertEqual(status, 200)
                on_disk = C.WEB_DIR / url.removeprefix("/static/")
                self.assertEqual(body, on_disk.read_bytes())
                self.assertEqual(headers["content-type"], types[on_disk.suffix])

    def test_no_inline_scripts(self):
        # script-src 'self' blocks inline scripts, so one would silently never run.
        self.assertEqual(self.index_refs().inline_scripts, 0)

    def test_no_html_sinks_in_the_ui(self):
        files = [p for p in sorted(C.WEB_DIR.rglob("*")) if p.is_file()]
        self.assertIn(C.WEB_DIR / "app.js", files)
        for path in files:
            text = path.read_text(encoding="utf-8")
            for n, line in enumerate(text.splitlines(), 1):
                with self.subTest(file=path.name, line=n):
                    self.assertIsNone(HTML_SINKS.search(line), line.strip())

    def test_sink_pattern_catches_what_it_should(self):
        for bad in (
            "el.innerHTML = s",
            "el.outerHTML=s",
            "el.innerHTML += s",
            "el['innerHTML'] = s",
            "el.insertAdjacentHTML('beforeend', s)",
            "document.write(s)",
            "document.writeln(s)",
        ):
            with self.subTest(bad=bad):
                self.assertIsNotNone(HTML_SINKS.search(bad))
        for ok in ("// innerHTML is never used", "if (a.innerHTML === b) {}", "el.textContent = s"):
            with self.subTest(ok=ok):
                self.assertIsNone(HTML_SINKS.search(ok))


if __name__ == "__main__":
    unittest.main()
