# Phase 8: measurement validity, design checks and measurements

- **Date:** 2026-09-29
- **Question:** For REMEDIATION_PLAN.md Phase 8, which statistical tests keep the recommendation
  from following noise without hiding real differences, at the sample sizes dns-bench actually has
  (60 domains, 1 round: 60–120 answers per resolver)? And does serve mode distort the timings
  (COR-3)?

## Findings

### The intervals the plan named are too weak at these sample sizes

- **The overlap of two 95 % Wilson intervals is far too conservative for failure rates.** It
  roughly amounts to a test at p < 0.006. At that level, 5 timeouts in 50 against none (10 %
  against 0 %) was "not significant", so a resolver dropping a tenth of its queries escaped any
  penalty. **Newcombe's hybrid score interval** for the difference of two rates is a proper
  two-sample test and is built from the same Wilson intervals. With it:
  - 5/50 against 0/50 is significant (the lower bound is 0.009);
  - 2/122 against 0/122 is not (the lower bound is −0.016). That is the COR-1 case, where two
    lost packets decided the ranking.
- **A p95 confidence interval has no upper bound below 72 samples.** Bounding it at 95 %
  requires 0.95^n ≤ 0.025. A single run has 60–62 first answers per resolver, so every p95
  interval was unbounded, every pair "overlapped", and Cloudflare (p95 12 ms) came out tied with
  Google (p95 173 ms). Tails are compared with a **two-sample quantile test** instead:
  - take the p95 of both samples pooled;
  - count each side's answers above it;
  - compare the two shares with Newcombe's interval.

  This is Mood's median test at the 95th percentile.
- **The p95 point estimate from about 60 answers depends on one or two answers.** In run
  20260926T220645Z:
  - Cloudflare's p95 was 21 ms and Google's was 175 ms;
  - Cloudflare had 2 answers above 170 ms and Google had 4.

  The old score weighted that difference at 0.3 × 150 ms. The tail test rightly calls it noise.
- **The median intervals are informative from 6 samples on.** The exact order-statistic ranks
  match the classic tables (n = 10: ranks 2 and 9; n = 20: 6 and 15; n = 100: 40 and 61). Ranks
  are computed exactly in integers up to n = 1000. Above that, the normal approximation is within
  one rank of the exact answer, which a test checks at n = 1001, 1500 and 2400.

### Cache effects are large

- In fixture run 20260925T091918Z (2 rounds, 2 servers per provider), Quad9's p95 over every answer
  was 19.4 ms. Over the first answer of each domain it is 165.1 ms: 183 of its 244 answers were
  repeats. A provider with two servers used to get half its tail from cache hits, and a
  single-server System resolver got none.
- "First" is per (run, resolver, domain), by start time: a provider's servers often share a cache.
  Per-server statistics use per (run, server, domain) instead. Otherwise, without shuffling, one
  server could get every first answer and the sibling comparison would be meaningless.

### Serve mode doesn't distort the timings (COR-3)

The setup:

- 10 fake servers mapped to UDP responders on loopback ports, through the real `resolver.query`;
- 60 domains, a 100 ms interval, 4 alternating repetitions per mode;
- CLI mode calls `runner.run_benchmark` directly, printing progress to stderr;
- serve mode is the real HTTP server, with `POST /api/run` and a `GET /api/status` poll every 500 ms
  like the UI.

Results over 2,400 answers per mode:

| Mode | Median | Mean | p95 | p99 | Max |
|---|---|---|---|---|---|
| CLI | 0.448 ms | 0.631 ms | 1.739 ms | 3.486 ms | 8.0 ms |
| serve | 0.414 ms | 0.585 ms | 1.360 ms | 3.371 ms | 24.8 ms (one outlier) |

The difference is noise, and it is two orders of magnitude below the latencies being measured
(5–200 ms). So kernel receive timestamps (`SO_TIMESTAMP` via `recvmsg`) aren't needed. The plan
made them conditional on a distortion. The script is below.

### Old against new, on the 13 runs in `runs/`

- **Best:** unchanged in all 13 (Cloudflare).
- **Suggested servers:** changed in 9 of the 13:
  - put first: 1.0.0.1 → 1.1.1.1, because the siblings are within noise and config order decides;
  - the backup: Quad9 ↔ OpenDNS ↔ ISP, following the first-answer p95.
- **Single runs:** these report more ties with the best than before (up to all four others),
  because 60 answers can't separate them.
- **"All runs combined":** separates them: Cloudflare, then OpenDNS with ISP and Quad9 within
  noise of it, then Google. There is no tie for best.

## Decision/recommendation

- Use Newcombe's interval (from Wilson intervals) for "significantly higher" failure and retry rates.
- Compare medians by the overlap of their exact 95 % intervals, per the plan, with a 2 ms floor
  (0.5 ms between sibling servers).
- Compare tails with the pooled-p95 quantile test (Fisher's exact test, after the review).
- Use only first answers for every latency figure (after the review; see the addendum).
- Keep the plan's exact `median_ci` / `p95_ci` in the output for people to read.
- Don't implement `SO_TIMESTAMP`.
- Revisit the tests if people find single-run ties unhelpful. One option is to show "All runs
  combined" more prominently.

## Addendum: the review of PR #15

Three time-boxed review agents covered the statistics, the recommendation logic, and the resolver
plus docs. They confirmed:

- the exact ranks, checked against brute-force `Fraction` sums for n = 1–159, 300 and 500;
- Wilson and Newcombe against Newcombe (1998): 56/70 vs 48/80 gives 0.0524–0.3339;
- that the resolver rejects no real reply, and that the docs matched the code.

They found the following, and it changed the design:

- **The median and mean used every answer.** Repeats are cache hits the benchmark causes itself.
  - They pulled down the median of providers with more servers. With identical cache-miss
    distributions, the provider whose second server hits the shared cache got a median of 6.8 ms
    against 33.6 ms.
  - They made the median interval falsely narrow, because samples of one domain are correlated.
    Quad9's first-answer median (6.54) lay outside its all-answer interval [5.83, 6.23].
  - A user's own cache means a resolver sees each name about once per TTL.
  - **Now every latency figure uses first answers only.** This goes further than the plan. More
    rounds now sharpen only the failure rates.
- **The tail test assumed independent binomials, but the pooled threshold fixes the total above
  it.** With unbalanced sizes it said "different" too often: 13 % at 3 vs 100, 8 % at 10 vs 600,
  against a nominal 5 %. **Now it is Fisher's exact test** (hypergeometric, via `lgamma`), which
  gives the same answer at 60 vs 60.
- **Ties aren't transitive.** The best could be "tied" with #3 and #4 but not #2, and one such
  tie came only from the 2 ms floor while the note claimed "no significant difference in median".
  **Now `tied_with` / `backup_tied_with` are a run down the ranking**, stopping at the first
  resolver not within noise. The notes and the `=` legend say what was tested.
- The ranking's failure rate and counts included servers that never answered, while the score
  and `failures_counted` didn't.
- A stale resolver (older runs only) could make a current one's failures count.
- The normal approximation let one tail of the p95 interval reach 2.8 % above n = 1000. Exact
  ranks now go up to n = 10,000, which costs 0.14 s once per size.
- Smaller fixes: no `*` with a single resolver, the backup's aliases were listed as its ties, the
  local-errors note was missing when nothing answered, and digit alignment in the report.

**Known limitations, left as they are:**

- **The tail test has a floor.** It can't fire below about 50 first answers per side. At 60 vs
  60 only a 6-to-0 split of the slowest 5 % counts. That is inherent in estimating a p95 from 60
  answers.
- **Values equal to the pooled p95 count as not above it.** A block of slow answers tied exactly
  at the threshold can hide. This is rare with 0.001 ms resolution.
- **"First" orders answers by the start of the answering attempt**, which is the retry for a
  retried query, so a sibling's later query can take "first". This only happens with tries > 1.
- **The pairwise tail tests have no correction for multiple comparisons.** With 20 resolvers
  there are 190 pairs, and a false positive only breaks a tie.
- **A significant failure rate counts in full, not just its excess over the best rate.** The
  review flagged this as a design choice for Chris.

After these changes, on the 13 saved runs:

- the best resolver is Cloudflare in all 13;
- the suggested servers changed in 9;
- 7 single runs report ties with the best, down the ranking;
- "All runs combined" (Cloudflare, then OpenDNS with ISP and Quad9 within noise of it, then
  Google) has no tie for best.

## Addendum (2026-10-09): the two open decisions

`.project/research/2026-10-09-phase-8-open-decisions.md` analysed the two questions this phase left
to Chris. On 2026-10-09 he settled them:
- **Failure-rate counting:** stays as built (left alone for now).
- **First-answer latency:** kept.

That note also corrects the "falsely narrow" argument above. Within a run, repeats make the median
interval only about 8 % too narrow. The bias between providers and the run-to-run behaviour carry
the decision.

## Sources

- Newcombe R. G. (1998), "Interval estimation for the difference between independent proportions:
  comparison of eleven methods", *Statistics in Medicine* 17:873–890 (method 10).
- Wilson E. B. (1927), "Probable inference, the law of succession, and statistical inference",
  *JASA* 22:209–212.
- Conover W. J., *Practical Nonparametric Statistics*, 3rd ed.: the confidence interval for a
  quantile from order statistics, and the median test.

## The serve-timing script

Run it from the repo root with `uv run python <script> 2>/dev/null`.

```python
import http.client, json, socket, statistics, struct, sys, tempfile, threading, time
from pathlib import Path
from dnsbench import config as C, resolver, runner, server as SV

N, DOMAINS, INTERVAL, REPS = 10, 60, 100, 4


def responder():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))

    def loop():
        while True:
            data, addr = s.recvfrom(4096)
            qid, flags = struct.unpack_from("!HH", data, 0)
            s.sendto(struct.pack("!HHHHHH", qid, 0x8180, 1, 0, 0, 0) + data[12:], addr)

    threading.Thread(target=loop, daemon=True).start()
    return s.getsockname()[1]


ports = {f"192.0.2.{i + 1}": responder() for i in range(N)}


def qf(server, domain, **kw):
    return resolver.query("127.0.0.1", domain, port=ports[server], **kw)


cfg = C.default_config()
cfg["resolvers"] = [{"name": f"R{i}", "servers": [ip], "enabled": True} for i, ip in enumerate(ports)]
cfg["domains"] = [f"d{i}.example" for i in range(DOMAINS)]
cfg["settings"].update(per_server_interval_ms=INTERVAL, rounds=1, timeout_ms=1000)


def cli():
    run = runner.run_benchmark(
        cfg, query_fn=qf, progress=lambda e: print(e["result"]["domain"], file=sys.stderr)
    )
    return [r["ms"] for r in run["results"] if r["ms"] is not None]


tmp = Path(tempfile.mkdtemp())
(tmp / "config.json").write_text(json.dumps(cfg))
srv = SV.make_server("127.0.0.1", 0, tmp / "config.json", tmp / "runs", query_fn=qf)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]


def req(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request(
        method,
        path,
        body=json.dumps(body) if body is not None else None,
        headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{port}"},
    )
    r = c.getresponse()
    data = json.loads(r.read())
    c.close()
    return data


def serve():
    req("POST", "/api/run", {})
    while True:
        time.sleep(0.5)  # the UI's POLL_MS
        st = req("GET", "/api/status")
        if not st["running"] and st["last_run_id"]:
            break
    run = req("GET", f"/api/runs/{st['last_run_id']}")
    return [r["ms"] for r in run["results"] if r["ms"] is not None]


def describe(ms):
    ms = sorted(ms)
    q = statistics.quantiles(ms, n=100)
    return f"n={len(ms)} median={statistics.median(ms):.3f} mean={statistics.fmean(ms):.3f} p95={q[94]:.3f} p99={q[98]:.3f} max={ms[-1]:.3f}"


res = {"cli": [], "serve": []}
for rep in range(REPS):
    for mode, fn in (("cli", cli), ("serve", serve)) if rep % 2 == 0 else (("serve", serve), ("cli", cli)):
        ms = fn()
        res[mode] += ms
        print(f"rep {rep} {mode:5} {describe(ms)}", flush=True)
for mode, ms in res.items():
    print(f"ALL   {mode:5} {describe(ms)}")
srv.shutdown()
```
