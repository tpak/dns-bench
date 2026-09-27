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

## Sources

- Firefox headless mode and `--screenshot`: https://firefox-source-docs.mozilla.org/testing/headless/index.html
- CSP `style-src` and CSSOM: https://www.w3.org/TR/CSP3/#directive-style-src (inline `style`
  attributes and `<style>` elements are governed; CSSOM property writes are not)
- Trusted Types: https://w3c.github.io/trusted-types/dist/spec/
