# A dead LAN resolver on Linux, and CI on Ubuntu 26.04

- **Date:** 2026-09-29
- **Questions:**
  1. The 1.3.0 review found that on macOS a LAN resolver that goes down shows up as
     "Host is down" / "No route to host" send errors, which 1.3.0 counts as the resolver's
     failures. How does it show up on Linux?
  2. GitHub moves `ubuntu-latest` to Ubuntu 26.04 from 2026-10-19. Does CI still pass there?

## Findings

### 1. On Linux, a dead on-link resolver is a timeout

- Setup: a `python:3.13-slim` container under OrbStack on this Mac. The container's address was
  192.168.215.2/24, and the probe queried an unused address on the same subnet (.250, .251). It
  used `resolver.query`, and then the whole CLI (`run --json` with that address as the only
  resolver).
- Result: every attempt timed out, with a 1 s timeout (10 attempts) and with a 5 s timeout
  (4 attempts). No `connect:`, `send:` or `recv:` error ever appeared. The run's summary gave
  `failures 12, timeouts 12, local_errors 0, failure_rate 1.0`, with the notes `no_answers` and
  `check_network`.
- Why: when ARP fails, Linux drops the queued datagram and doesn't report an error to an ordinary
  UDP socket. macOS reports EHOSTUNREACH, then EHOSTDOWN, on the next `send()`.
- So on both systems a dead LAN resolver counts as the resolver's failure: timeouts on Linux, and
  (since 1.3.0) "Host is down" / "No route to host" on macOS. No code change is needed.
- Caveat: this was a container on OrbStack's virtual bridge, not a Linux machine on a physical
  LAN. The kernel's neighbour code is the same, though.

### 2. Ubuntu 26.04 exposed a CPython bug that our server relied on being fixed

- A temporary commit in PR #19 added `ubuntu-26.04` to the CI matrix and to the lint job.
  - The label exists and runs.
  - Lint passed, and tests passed on 3.13.
  - Tests **failed on 3.14**: `test_malformed_requests_get_json_errors_with_security_headers`,
    for `GET / HTTP/9.9`. The first line of the response was the JSON body; there was no status
    line.
- Cause:
  - setup-uv's `python-version: 3.14` let uv pick the image's system interpreter,
    `/usr/bin/python3.14`, which is 3.14.4.
  - CPython before 3.14.7 and 3.13.15 has gh-54930: for a malformed request line (a bad or too new
    version, or a bad HTTP/0.9 request) `request_version` stays at "HTTP/0.9". The error then goes
    out the HTTP/0.9 way, with no status line and no headers. That means none of the security
    headers the 1.2.0 notes promise for malformed requests.
  - The fix, [3.14] commit 553d7fa228 of 2026-07-05, first shipped in 3.14.7 and 3.13.15. The
    diff of `Lib/http/server.py` between v3.14.4 and v3.14.7 shows it: `self.request_version = ''`
    before those `send_error` calls.
  - Our other jobs use uv-managed Pythons, which are the latest patch releases, so nothing showed.
    Anyone running `serve` on Ubuntu 26.04's own Python would get header-less error replies.
- The fix in dns-bench: `Handler.send_error` does what gh-54930 does. A request still at
  "HTTP/0.9" that isn't a real HTTP/0.9 request (`GET /path`) is answered with a status line and
  headers. A new test sets up the state an old interpreter leaves behind, so it checks the fix on
  any Python.
- With the fix, the suite passes locally on 3.13.0, 3.14.0 and 3.14.4, the oldest supported patch
  releases and the one Ubuntu 26.04 ships.

## Decision/recommendation

- **#5:** no change. The 1.3.0 classification is right on both systems.
- **#6:**
  - Fix the server as above.
  - Add a weekly scheduled CI run (plus a manual trigger), so a change in the runner images or in
    Python releases shows up even when nobody pushes. GitHub emails the repo owner when a
    scheduled run fails.
  - Add test jobs on the oldest patch release of each supported Python (3.13.0, 3.14.0). This bug
    showed that "3.13" and "3.14" in CI meant the newest patch releases only, while users run
    whatever their system has.
  - Revert the temporary `ubuntu-26.04` probe. `ubuntu-latest` becomes that image on 2026-10-19,
    and the weekly run will catch anything else.
- **A limit of scheduled workflows:** GitHub disables them after 60 days without activity in the
  repository. Re-enable the workflow in the Actions tab if that happens.

## Sources

- CPython gh-54930 and commit 553d7fa228 (3.14 branch, 2026-07-05); `Lib/http/server.py` at
  v3.14.4, v3.14.5, v3.14.6 and v3.14.7, and v3.13.13–v3.13.15.
- PR #19's CI run on `ubuntu-26.04` (job 109390921137): "Using CPython 3.14.4 interpreter at:
  /usr/bin/python3.14".
- GitHub Actions: scheduled workflows are disabled after 60 days of repository inactivity
  (docs: "Disabling and enabling a workflow").
