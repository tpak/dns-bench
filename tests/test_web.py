"""Smoke tests for the real web UI (dnsbench/web), served by the real server.

test_server.py serves stub UI files, so a missing or misnamed asset in the real UI passes there. These
tests serve paths.WEB_DIR itself. There is no JavaScript test runner (the UI has no Node tooling):
Biome, run by pre-commit and in CI, parses app.js, so a syntax error fails lint instead.
"""

from __future__ import annotations

import http.client
import json
import re
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from pathlib import Path

from dnsbench import config as C
from dnsbench import paths
from dnsbench import server as SV

# Ways to turn a string into markup. The UI builds every element with createElement/textContent, and the
# CSP is the second line of defence, not the first.
HTML_SINKS = re.compile(
    r"""(?:\.|\[\s*["'])(?:inner|outer)HTML(?:["']\s*\])?\s*\+?=(?!=)"""
    r"|\binsertAdjacentHTML\s*\("
    r"|\bdocument\s*\.\s*write(?:ln)?\s*\("
)


# A style attribute set from script; the CSP (style-src 'self') blocks it. app.js styles elements through
# CSSOM (element.style), which the CSP allows.
STYLE_ATTRIBUTE = re.compile(
    r"""setAttribute\s*\(\s*["']style["']|setAttributeNS\s*\([^,]*,\s*["']style["']"""
)


class _Refs(HTMLParser):
    """Collects the same-origin URLs a page loads, and what its CSP would block: inline scripts and styles."""

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.inline_scripts = 0
        self.inline_styles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "script" and not a.get("src"):
            self.inline_scripts += 1
        if tag == "style" or "style" in a:
            self.inline_styles.append(self.get_starttag_text() or tag)
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
        # No web_dir: the server falls back to the real paths.WEB_DIR. Serving files touches neither the
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
        self.assertEqual(self.srv.web_dir, paths.WEB_DIR)
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(body, (paths.WEB_DIR / "index.html").read_bytes())

    def test_index_has_a_strict_csp(self):
        _, headers, _ = self.get("/")
        csp = {
            d.split()[0]: d.split()[1:] for d in headers["content-security-policy"].split(";") if d.strip()
        }
        self.assertEqual(csp["default-src"], ["'self'"])
        self.assertEqual(csp["script-src"], ["'self'"])
        self.assertEqual(csp["style-src"], ["'self'"])  # no 'unsafe-inline'
        self.assertEqual(csp["require-trusted-types-for"], ["'script'"])
        self.assertEqual(csp["trusted-types"], ["'none'"])
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
                on_disk = paths.WEB_DIR / url.removeprefix("/static/")
                self.assertEqual(body, on_disk.read_bytes())
                self.assertEqual(headers["content-type"], types[on_disk.suffix])

    def test_no_inline_scripts_or_styles(self):
        # The CSP blocks both, so one would silently never run or apply.
        refs = self.index_refs()
        self.assertEqual(refs.inline_scripts, 0)
        self.assertEqual(refs.inline_styles, [])

    def test_app_never_sets_a_style_attribute(self):
        for n, line in enumerate((paths.WEB_DIR / "app.js").read_text(encoding="utf-8").splitlines(), 1):
            with self.subTest(line=n):
                self.assertIsNone(STYLE_ATTRIBUTE.search(line), line.strip())
        for bad in ("el.setAttribute('style', s)", 'el.setAttributeNS(null, "style", s)'):
            self.assertIsNotNone(STYLE_ATTRIBUTE.search(bad), bad)
        self.assertIsNone(STYLE_ATTRIBUTE.search("el.style.setProperty('color', c)"))

    def test_no_html_sinks_in_the_ui(self):
        files = [p for p in sorted(paths.WEB_DIR.rglob("*")) if p.is_file()]
        self.assertIn(paths.WEB_DIR / "app.js", files)
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


class NoRuleCopiesTest(unittest.TestCase):
    """The rules live in Python and reach the UI through GET /api/schema and the validate/estimate
    endpoints (REMEDIATION_PLAN.md Phase 5). Copies in app.js drifted from them before."""

    js: str
    html: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.js = (paths.WEB_DIR / "app.js").read_text(encoding="utf-8")
        cls.html = (paths.WEB_DIR / "index.html").read_text(encoding="utf-8")

    def test_no_copied_rules(self):
        for name in (
            "DEFAULT_SETTINGS",
            "MIN_INTERVAL_MS",
            "MAX_DOMAINS",
            "PRESETS",
            "validateDraft",
            "classifyServerError",
            "looksLikeIp",
            "validHostname",
            "parseDomains",
            "function estimate(",
            "/api/defaults",
        ):
            with self.subTest(name=name):
                self.assertNotIn(name, self.js)

    def test_no_setting_bounds_or_defaults(self):
        web = self.js + self.html
        for key, (lo, hi) in C.SETTING_BOUNDS.items():
            for pattern in (rf"\bmin:\s*{lo}\b", rf"\bmax:\s*{hi}\b", rf'\bmin="{lo}"', rf'\bmax="{hi}"'):
                with self.subTest(key=key, pattern=pattern):
                    self.assertIsNone(re.search(pattern, web))
        for key, value in C.DEFAULT_SETTINGS.items():
            literal = f"['\"]{value}['\"]" if isinstance(value, str) else json.dumps(value)
            with self.subTest(default=key):
                self.assertIsNone(re.search(rf"\b{key}:\s*{literal}", web))
        self.assertIsNone(re.search(r"maxlength:\s*'\d+'", self.js))  # the name limit
        self.assertNotIn(f"{C.MAX_QUERIES_PER_RUN}", web.replace("_", ""))

    def test_no_preset_addresses(self):
        for preset in C.PRESETS:
            for ip in preset["servers"]:
                with self.subTest(ip=ip):
                    self.assertNotIn(ip, self.js)


class ApiContractTest(unittest.TestCase):
    """Every endpoint app.js calls exists, with the method it uses (REMEDIATION_PLAN.md Phase 7)."""

    # api('/api/x'), api(`/api/runs/${id}`), with an optional { method: 'POST' } after the path
    CALL_RE = re.compile(r"api\(\s*(['`])(/api/[^'`]*)\1\s*(?:,\s*\{\s*method:\s*'([A-Z]+)')?")
    LINK_RE = re.compile(r"href:\s*`(/api/[^`]*)`")

    @staticmethod
    def concrete(path: str) -> str:
        """A template path with every ${...} filled in with a valid run id, and no query string."""
        return re.sub(r"\$\{[^}]*\}", "20260101T000000Z", path).split("?", 1)[0]

    def calls(self) -> set[tuple[str, str]]:
        js = (paths.WEB_DIR / "app.js").read_text(encoding="utf-8")
        found = {(m[3] or "GET", self.concrete(m[2])) for m in self.CALL_RE.finditer(js)}
        found |= {("GET", self.concrete(m[1])) for m in self.LINK_RE.finditer(js)}  # CSV downloads
        return found

    def test_every_call_has_a_route_with_its_method(self):
        calls = self.calls()
        self.assertGreaterEqual(len(calls), 12, calls)  # the regexes still find the calls
        for method, path in sorted(calls):
            with self.subTest(call=f"{method} {path}"):
                routes = [methods for pattern, methods in SV._ROUTES if pattern.match(path)]
                self.assertTrue(routes, "no route")
                self.assertIn(method, routes[0])

    def test_no_route_is_left_unused_by_the_ui(self):
        # Not a rule for the API as a whole (curl users may call anything), but a check that the UI
        # still uses what it was built for; a route the UI dropped should be a deliberate decision.
        used = {path for _, path in self.calls()}
        unused = []
        for pattern, _methods in SV._ROUTES:
            if pattern.pattern.startswith("^/api/") and not any(pattern.match(p) for p in used):
                unused.append(pattern.pattern)
        self.assertEqual(unused, [])


if __name__ == "__main__":
    unittest.main()
