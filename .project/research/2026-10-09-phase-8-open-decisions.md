# Phase 8's two open scoring decisions

- **Date:** 2026-10-09
- **Questions:**
  1. Once a resolver's failure (or retry) rate is significantly higher than another's (Newcombe),
     should its *whole* rate count in the score, or only the *excess* over the most reliable
     resolver's?
  2. Should every latency figure use only each domain's first answer (as shipped in 1.3.0), or
     only the tail figures (as the plan said), or every answer (as in 1.2.0)?

## Method

Four independent analysts, two per question: one statistician and one taking a user's view (the
1.4.0 review workflow). They scored the owner's 13 saved runs (copied, read-only) under each
option by monkeypatching the scoring code. They also ran simulations: bootstrapped domains from the
real runs, with timeouts drawn at chosen true rates, and either path loss shared by every resolver
or one resolver that really drops queries. Scripts are in that session's scratchpad; the numbers
are below.

## Findings

### Decision 1: count a significant rate in full, or only its excess

- **The real runs can't decide it.** The 13 runs hold 6 failures in about 7,800 counted queries
  (0.08 %), and none is significant. Every option tested gives the same best and backup in all 13
  runs and in "All runs combined".
- **The current rule has a cliff.**
  - On the default config (120 queries per provider, timeout 1,000 ms) the 5th timeout against
    a clean resolver is the first that counts, and it adds 83 points at once. Against 1/120 it
    is the 7th (+117). For a 60-query System resolver it is the 3rd (+100).
  - For scale: the owner's gaps between #1 and #2 are 0.6–35 points (median 16), and 1st to 5th
    spans about 50–70.
- **The current rule is robust to shared path loss.** When every resolver loses 1–6 % (bad Wi-Fi),
  it picks the true best 82–89 % of the time, with mean regret 2.2–4.7 points.
  - Counting every rate in full (1.2.0's rule, "d") is unbiased but noisy there: 39–62 % right,
    regret 9–17.
  - When one resolver really drops 2–4 %, the current rule is worse (regret 20–31 against
    d's 10–14), because the test has little power at n = 120: a 4 % excess is detected about
    half the time.
- **"Only the excess" is the wrong fix.** Both analysts agree.
  - The excess over the lowest observed rate behaves exactly like the current rule at the
    default sizes: with 5 resolvers the lowest rate is almost always 0, so excess = full rate.
    The cliff stays.
  - The conservative excess (Newcombe's lower bound) has no cliff, but it is biased low by about
    4 failures in 120. It charges 12 points for a true 80, so it keeps recommending resolvers
    that drop queries, the worst of all options there. It is also harder to explain.
- **An option outside the brief (statistician):** count every rate continuously after shrinking it
  toward the group's pooled rate (positive-part James–Stein, "e1").
  - It has no cliff and the lowest average regret of everything tested.
  - It is about a third better than "d" under shared loss.
  - It needs at least 4 resolvers to shrink at all.
  - It still charges about 8 points for a single lost packet with 5 resolvers, which can flip
    #1/#2 in some of the owner's runs.

### Decision 2: latency from first answers only

- **Both analysts say keep the shipped behaviour (first answers only).**
- **The plan's version (median and mean over every answer):**
  - It changes no best, backup or suggested IP on the 13 runs.
  - It gives multi-server providers a head start over a one-server System resolver: median up
    to 12 ms lower in a run with cold caches, mean −0.7 ms.
  - Its median intervals cover another run's median less often (55 % against 70 % for
    first-answer intervals).
- **Every answer for everything (1.2.0):** this moves Quad9 from 4th to 2nd on the benchmark's
  own cache hits. Its p95 averages 17 ms lower, worst case 146 ms.
- **A correction to the 2026-09-29 reasoning:** within one run, repeats make the median interval
  only about 8 % too narrow (ICC 0.23 on above/below the median; design effect gives an effective
  n of about 105 of 124). The real reasons are the bias between providers and the run-to-run
  behaviour.
- **What it costs:** about half the latency samples per run (62 instead of 124). The median
  interval is about twice as wide (2.1 ms against 1.0 ms nominal). More rounds no longer sharpen
  latency; the README and the Settings help already say so.
- **Making `rounds` space repeats past typical TTLs** was considered and rejected. TTLs span
  minutes to a day, so run time would become unpredictable.

## Decision/recommendation

- **Decision 1: keep the current rule for now.** Don't switch to "only the excess": both
  analysts reject it with numbers.
  - The current rule is what the evidence supports for the common case (home networks with
    shared loss), and it is the simplest to explain.
  - Its weakness is the cliff, and low power when one resolver really drops a few percent.
  - If that ever shows up in practice (a recommendation flipping when one more timeout crosses the
    threshold), the candidate to try is the statistician's shrinkage rule ("e1"), as its own
    minor release with a README rewrite. It would replace the significance gate in the score,
    while Newcombe's test stays for the "within noise" labels.
- **Decision 2: keep first-answer latency.** Record the correction above.

## Sources

- The 1.4.0 review workflow `wf_33a58d60-a30` (analysts d1-stats, d1-user, d2-stats, d2-user).
- .project/research/2026-09-29-phase-8-measurement-validity.md and its addenda.
- Newcombe R. G. (1998), *Statistics in Medicine* 17:873–890. Efron B. and Morris C. (1975),
  "Data analysis using Stein's estimator and its generalizations", *JASA* 70:311–319.
