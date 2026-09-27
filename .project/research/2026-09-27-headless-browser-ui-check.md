# Checking the web UI in a headless browser from an agent session

- **Date:** 2026-09-27
- **Question:** How can an agent check that the real UI still renders after a change (Phase 3's
  CSP change: `style-src 'self'` without `'unsafe-inline'`, plus Trusted Types)? The Python tests
  can't run JavaScript, and there is no Node tooling in the repo.

## Findings

- **Headless Chrome hangs on this Mac, even for a `data:` URL.** It hung with `--headless`,
  `--headless=new`, `--no-sandbox`, `--use-mock-keychain --password-store=basic`, and outside the
  agent's shell sandbox. Each run hit a 45–90 s timeout with no DOM output. The only log lines came
  from GoogleUpdater. The cause is unknown. Don't spend more time on it without a new idea.
- **Headless Firefox works, inside the sandbox**, via `firefox --headless --no-remote --profile
  <tmpdir> --screenshot out.png --window-size=1280,1600 <url>`. It takes about 6 s per page. The
  `sandbox_extension_issue_file_to_process failed` lines on stderr are harmless.
- **`--screenshot` fires at the page's `load` event**, before app.js has fetched anything, so the
  first screenshots only showed "Loading DNS Bench…". The fix is to hold back `load`: append
  `<img src="/static/slow.png">` to a copy of index.html, and make the handler sleep 4 s before
  answering 404 for that file. That is enough for every view to fetch and draw its data.
- **Firefox (Sept 2026) enforces both `style-src` and Trusted Types.** Two negative controls were
  added to the index.html copy:
  - `<div style="background:red">` stayed unstyled;
  - a script calling `insertAdjacentHTML` inserted nothing.

  So a view that renders correctly in these screenshots doesn't depend on inline styles or Trusted
  Types sinks, at least on the code paths it exercised.
- Result for Phase 3: Overview, By resolver, By domain, History and Settings all render fully with the
  new CSP. The fixture runs from `tests/fixtures/runs-v1` supply the data. Colours set through CSSOM
  (dots, bars, heatmap cells) still apply.

## Recommendation

- Use the Firefox screenshot method for visual checks (Phase 7's `noDescendingSpecificity`
  before/after comparison needs it too). The script used is below; run it from the repo root with
  `uv run python <script> <out-dir> [route,route,...]`, then look at the PNGs.
- Screenshots don't exercise interaction: clicks, keyboard, a live run, or saving Settings. Before a
  release that changes the CSP, Chris should still click through the UI once in a normal browser.

```python
import shutil, subprocess, sys, tempfile, threading, time
from pathlib import Path
from dnsbench import paths, server as SV

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
routes = (
    sys.argv[2].split(",") if len(sys.argv) > 2 else ["overview", "resolver", "domain", "history", "settings"]
)
tmp = Path(tempfile.mkdtemp())
shutil.copytree(Path("tests/fixtures/runs-v1"), tmp / "runs")
web = tmp / "web"
shutil.copytree(paths.WEB_DIR, web)
html = (web / "index.html").read_text()
(web / "index.html").write_text(
    html.replace("</body>", '<img src="/static/slow.png" alt="" width="1" height="1"></body>')
)
orig = SV.Handler.h_static


def h_static(self, file):
    if file == "slow.png":
        time.sleep(4)  # hold back the load event, and so the screenshot, until the data is drawn
        raise SV.HTTPError(404, "Not found")
    return orig(self, file)


SV.Handler.h_static = h_static
srv = SV.make_server("127.0.0.1", 0, tmp / "config.json", tmp / "runs", web_dir=web)
threading.Thread(target=srv.serve_forever, daemon=True).start()
(tmp / "ff").mkdir()
for route in routes:
    subprocess.run(
        [
            "/Applications/Firefox.app/Contents/MacOS/firefox",
            "--headless",
            "--no-remote",
            "--profile",
            str(tmp / "ff"),
            "--screenshot",
            str(out / f"{route}.png"),
            "--window-size=1280,1600",
            f"http://127.0.0.1:{srv.server_address[1]}/#{route}",
        ],
        capture_output=True,
        timeout=90,
    )
srv.shutdown()
```

## Addendum (2026-09-28): interactive checks without WebDriver

Screenshots don't exercise clicks or typing. Phase 5 rewrote the Settings save flow, so it needed
more: **inject a test script into a copy of the UI and let it report back.**

- The harness copies `dnsbench/web`, adds `<script src="/static/uitest.js">` (allowed by the CSP:
  it is same-origin) and the `slow.png` load hold-back to `index.html`, and patches `h_static` so
  `GET /static/report?k=...&v=...` prints `k: v` to stdout, and `k=done` releases `slow.png` (and so
  the screenshot).
- `uitest.js` is a plain script that waits for bootstrap, then drives the page the way a user would:
  set `input.value` and dispatch an `input` event, `.click()` buttons found by their text, change
  `location.hash`. After each step it `fetch`es `/static/report` with what the DOM shows (an error
  summary, a field's error, the estimate box, the rows). It also reports `window` `error` and
  `unhandledrejection` events, so a script error can't pass unnoticed.
- One Firefox run then gives a transcript of every step, plus a screenshot of the final state.
  About 15 s. Keyboard focus (Phase 7) can be checked the same way through `document.activeElement`.

The Phase 5 harness, run as `uv run python harness.py <out-dir>` from the repo root with
`uitest.js` next to it:

```python
"""Serve a copy of the UI with uitest.js injected, open it in headless Firefox, print what it reports."""

import shutil, subprocess, sys, tempfile, threading, time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from dnsbench import paths, server as SV

here = Path(__file__).parent
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
tmp = Path(tempfile.mkdtemp())
shutil.copytree(Path("tests/fixtures/runs-v1"), tmp / "runs")
web = tmp / "web"
shutil.copytree(paths.WEB_DIR, web)
shutil.copy(here / "uitest.js", web / "uitest.js")
html = (web / "index.html").read_text()
(web / "index.html").write_text(
    html.replace(
        "</body>",
        '<script src="/static/uitest.js"></script><img src="/static/slow.png" alt="" width="1" height="1"></body>',
    )
)
done = threading.Event()
orig = SV.Handler.h_static


def h_static(self, file):
    if file == "slow.png":
        done.wait(40)  # hold back the load event (and the screenshot) until the script is finished
        raise SV.HTTPError(404, "Not found")
    if file == "report":
        q = parse_qs(urlsplit(self.path).query)
        k, v = q.get("k", [""])[0], q.get("v", [""])[0]
        print(f"{k}: {v}", flush=True)
        if k == "done":
            done.set()
        self._send(204, b"", "text/plain")
        return
    return orig(self, file)


SV.Handler.h_static = h_static
srv = SV.make_server("127.0.0.1", 0, tmp / "config.json", tmp / "runs", web_dir=web)
threading.Thread(target=srv.serve_forever, daemon=True).start()
(tmp / "ff").mkdir()
subprocess.run(
    [
        "/Applications/Firefox.app/Contents/MacOS/firefox",
        "--headless",
        "--no-remote",
        "--profile",
        str(tmp / "ff"),
        "--screenshot",
        str(out / "final.png"),
        "--window-size=1280,1600",
        f"http://127.0.0.1:{srv.server_address[1]}/#overview",
    ],
    capture_output=True,
    timeout=120,
)
print("finished:", done.is_set())
srv.shutdown()
```

## Addendum (2026-09-28): screenshots are deterministic, so hashes prove "no visual change"

Phase 7 reordered CSS rules and had to show that nothing moved. Two runs of the same UI, with the
same fixed data and a fixed data directory, gave byte-identical PNGs for every view in both themes.
So a change meant to be invisible is checked by comparing SHA-256 hashes before and after, not by
eye. The script used took the screenshot method above and added:

- the fixture runs copied to a **fixed** directory (Settings shows the config path, so a random temp
  dir changes the picture), a fixed port, and a fake `detect_fn`;
- one Firefox profile per theme, with `user.js` setting `ui.systemUsesDarkTheme` and
  `layout.css.prefers-color-scheme.content-override`;
- `--window-size=1280,2400`, so long views are captured whole;
- a SHA-256 of each PNG printed next to the route and theme.

The injected-script method also checks keyboard behaviour: dispatch `KeyboardEvent('keydown', {key,
bubbles: true})`, call `.focus()` and `.click()`, then report `document.activeElement`. `focus()`
works in headless mode even though the window never has the system focus.

## Sources

- Firefox headless mode and `--screenshot`: https://firefox-source-docs.mozilla.org/testing/headless/index.html
- CSP `style-src` and CSSOM: https://www.w3.org/TR/CSP3/#directive-style-src (inline `style`
  attributes and `<style>` elements are governed; CSSOM property writes are not)
- Trusted Types: https://w3c.github.io/trusted-types/dist/spec/
