from __future__ import annotations

import random
import shutil
import subprocess
import unittest

from dnsbench import stats as S

# The exact awk program from the original dns-test.sh.
ORIGINAL_AWK = r"""
    {
        a[NR] = $1
        sum += $1
    }
    END {
        n = NR
        mean = sum / n
        median = (n % 2) ? a[(n+1)/2] : (a[n/2] + a[n/2+1]) / 2

        p80_idx = int((80/100) * n + 0.999999)
        p95_idx = int((95/100) * n + 0.999999)
        p98_idx = int((98/100) * n + 0.999999)
        if (p80_idx < 1) p80_idx = 1; if (p80_idx > n) p80_idx = n
        if (p95_idx < 1) p95_idx = 1; if (p95_idx > n) p95_idx = n
        if (p98_idx < 1) p98_idx = 1; if (p98_idx > n) p98_idx = n

        printf "mean=%.1f median=%.1f p80=%d p95=%d p98=%d min=%d max=%d n=%d",
            mean, median, a[p80_idx], a[p95_idx], a[p98_idx], a[1], a[n], n
    }"""


def row(ms, status="ok", resolver="R", server="1.1.1.1", domain="a.com", rnd=1):
    return {
        "resolver": resolver,
        "server": server,
        "domain": domain,
        "round": rnd,
        "status": status,
        "ms": ms if status != "timeout" else None,
        "rcode": "NOERROR" if status == "ok" else ("SERVFAIL" if status == "error" else None),
        "answers": 1,
        "error": None if status == "ok" else status,
        "t": 0.0,
    }


class PercentileTest(unittest.TestCase):
    def test_nearest_rank_known_values(self):
        vals = list(range(1, 11))  # 1..10
        self.assertEqual(S.nearest_rank(vals, 80), 8)
        self.assertEqual(S.nearest_rank(vals, 95), 10)
        self.assertEqual(S.nearest_rank(vals, 98), 10)
        vals = list(range(1, 21))  # n=20: 0.95*20 = 19 exactly -> 19th, not 20th
        self.assertEqual(S.nearest_rank(vals, 95), 19)
        self.assertEqual(S.nearest_rank(vals, 80), 16)
        self.assertEqual(S.nearest_rank(vals, 98), 20)
        self.assertEqual(S.nearest_rank([5], 80), 5)
        self.assertIsNone(S.nearest_rank([], 80))

    def test_nearest_rank_matches_ceil_where_exact(self):
        for n in range(1, 300):
            vals = list(range(n))
            for p in (80, 95, 98):
                idx = int((p / 100) * n + 0.999999)
                idx = min(max(idx, 1), n)
                self.assertEqual(S.nearest_rank(vals, p), vals[idx - 1])

    def test_median(self):
        self.assertEqual(S.median([1, 2, 3]), 2)
        self.assertEqual(S.median([1, 2, 3, 10]), 2.5)
        self.assertIsNone(S.median([]))

    @unittest.skipUnless(shutil.which("awk"), "awk not available")
    def test_matches_original_awk(self):
        rng = random.Random(1234)
        for trial in range(40):
            n = rng.choice([1, 2, 3, 7, 10, 20, 50, 60, 99, 120, 240, 480])
            vals = [rng.randint(1, 400) for _ in range(n)]
            data = "\n".join(str(v) for v in sorted(vals)) + "\n"
            out = subprocess.run(
                ["awk", ORIGINAL_AWK], input=data, capture_output=True, text=True, check=True
            ).stdout
            awk = dict(kv.split("=") for kv in out.split())
            st = S.latency_stats([row(v) for v in vals])
            with self.subTest(trial=trial, n=n):
                self.assertEqual(int(awk["n"]), st["n"])
                for k in ("p80", "p95", "p98", "min", "max"):
                    self.assertEqual(float(awk[k]), st[k], k)
                self.assertAlmostEqual(float(awk["median"]), st["median"], places=1)
                self.assertAlmostEqual(float(awk["mean"]), st["mean"], delta=0.051)


class LatencyStatsTest(unittest.TestCase):
    def test_basic(self):
        st = S.latency_stats([row(v) for v in (10, 20, 30, 40)])
        self.assertEqual(st["n"], 4)
        self.assertEqual(st["ok"], 4)
        self.assertEqual(st["failures"], 0)
        self.assertEqual(st["failure_rate"], 0.0)
        self.assertEqual(st["mean"], 25.0)
        self.assertEqual(st["median"], 25.0)
        self.assertEqual(st["min"], 10.0)
        self.assertEqual(st["max"], 40.0)
        self.assertEqual(st["p80"], 40.0)  # idx = int(3.2 + .999999) = 4
        self.assertEqual(st["stdev"], 11.18)  # population stdev

    def test_rounding(self):
        st = S.latency_stats([row(1.23456), row(2.34567)])
        self.assertEqual(st["min"], 1.23)
        self.assertEqual(st["mean"], 1.79)

    def test_failures_excluded_from_latency(self):
        """Regression: the original counted timeouts as 0 ms, dragging the stats
        down so a failing resolver looked FASTER. Failures must not touch latency."""
        ok_rows = [row(v) for v in (20, 22, 24, 26, 28)]
        bad_rows = [row(None, "timeout") for _ in range(5)] + [row(3.0, "error")]
        st = S.latency_stats(ok_rows + bad_rows)
        clean = S.latency_stats(ok_rows)
        for k in ("mean", "median", "p80", "p95", "p98", "min", "max", "stdev"):
            self.assertEqual(st[k], clean[k], k)
        self.assertEqual(st["min"], 20.0)  # not 0, not the 3 ms SERVFAIL
        self.assertEqual(st["n"], 11)
        self.assertEqual(st["ok"], 5)
        self.assertEqual(st["failures"], 6)
        self.assertEqual(st["timeouts"], 5)
        self.assertEqual(st["errors"], 1)
        self.assertAlmostEqual(st["failure_rate"], 6 / 11, places=4)

    def test_all_failed(self):
        st = S.latency_stats([row(None, "timeout"), row(None, "timeout")])
        self.assertEqual(st["ok"], 0)
        self.assertEqual(st["failure_rate"], 1.0)
        for k in ("mean", "median", "p80", "p95", "p98", "min", "max", "stdev"):
            self.assertIsNone(st[k], k)

    def test_empty(self):
        st = S.latency_stats([])
        self.assertEqual((st["n"], st["ok"], st["failure_rate"]), (0, 0, 0.0))
        self.assertIsNone(st["mean"])


class SummarizeTest(unittest.TestCase):
    def setUp(self):
        rows = []
        for res, srv, base in (
            ("Fast", "1.1.1.1", 5),
            ("Fast", "1.0.0.1", 7),
            ("Slow", "9.9.9.9", 50),
            ("Slow", "9.9.9.10", 60),
        ):
            for i, dom in enumerate(("b.com", "a.com", "c.com")):
                rows.append(row(base + i, resolver=res, server=srv, domain=dom))
        rows.append(row(None, "timeout", resolver="Slow", server="9.9.9.9", domain="c.com", rnd=2))
        rows.append(row(250.0, resolver="Slow", server="9.9.9.10", domain="a.com", rnd=2))
        rows.append(row(300.0, resolver="Fast", server="1.0.0.1", domain="b.com", rnd=2))
        self.rows = rows

    def test_structure(self):
        s = S.summarize(self.rows)
        self.assertEqual(
            set(s),
            {
                "overall",
                "resolvers",
                "domains",
                "by_resolver",
                "by_server",
                "by_domain",
                "slow",
                "slow_count",
                "slow_by_resolver",
                "slow_count_by_resolver",
                "slow_threshold_ms",
                "unanswered",
                "tails_differ",
            },
        )
        self.assertEqual(s["resolvers"], ["Fast", "Slow"])
        self.assertEqual(s["domains"], ["b.com", "a.com", "c.com"])  # first appearance
        self.assertEqual(s["overall"]["n"], len(self.rows))
        self.assertEqual(s["by_resolver"]["Slow"]["timeouts"], 1)
        self.assertEqual(list(s["by_server"]["Fast"]), ["1.1.1.1", "1.0.0.1"])
        self.assertEqual(s["by_server"]["Fast"]["1.1.1.1"]["n"], 3)
        self.assertEqual(s["by_domain"]["c.com"]["Slow"]["n"], 3)
        self.assertEqual(s["by_domain"]["c.com"]["Slow"]["failures"], 1)

    def test_orders(self):
        s = S.summarize(
            self.rows,
            resolver_order=["Slow", "Missing", "Fast"],
            domain_order=["a.com", "b.com", "c.com"],
            server_order={"Fast": ["1.0.0.1", "1.1.1.1"]},
        )
        self.assertEqual(s["resolvers"], ["Slow", "Fast"])
        self.assertEqual(s["domains"], ["a.com", "b.com", "c.com"])
        self.assertEqual(list(s["by_server"]["Fast"]), ["1.0.0.1", "1.1.1.1"])
        self.assertEqual(list(s["by_resolver"]), ["Slow", "Fast"])

    def test_orders_from_config(self):
        cfg = {
            "resolvers": [
                {"name": "Slow", "servers": ["9.9.9.10", "9.9.9.9"]},
                {"name": "Fast", "servers": ["1.1.1.1", "1.0.0.1"]},
            ],
            "domains": ["c.com", "b.com", "a.com"],
            "settings": {"slow_threshold_ms": 100},
        }
        s = S.summarize(self.rows, **S.orders_from_config(cfg))
        self.assertEqual(s["resolvers"], ["Slow", "Fast"])
        self.assertEqual(s["domains"], ["c.com", "b.com", "a.com"])
        self.assertEqual(list(s["by_server"]["Slow"]), ["9.9.9.10", "9.9.9.9"])
        self.assertEqual(s["slow_threshold_ms"], 100)

    def test_slow_list(self):
        s = S.summarize(self.rows, slow_threshold_ms=200)
        self.assertEqual([r["ms"] for r in s["slow"]], [300.0, 250.0])
        self.assertEqual(s["slow_count"], 2)
        s = S.summarize(self.rows, slow_threshold_ms=55)
        self.assertEqual([r["ms"] for r in s["slow"]], [300.0, 250.0, 62, 61, 60])
        self.assertTrue(all(r["status"] == "ok" for r in s["slow"]))

    def test_slow_list_capped(self):
        rows = [row(1000 + i) for i in range(250)]
        s = S.summarize(rows, slow_threshold_ms=200)
        self.assertEqual(len(s["slow"]), 200)
        self.assertEqual(s["slow_count"], 250)
        self.assertEqual(s["slow"][0]["ms"], 1249)

    def test_slow_by_resolver_not_crowded_out(self):
        # 250 slow answers from one resolver fill the global list; the other
        # resolver's slow answers must still be listed and counted per resolver.
        rows = [row(1000 + i, resolver="Slow", server="9.9.9.9") for i in range(250)]
        rows += [row(300.0, resolver="Fast"), row(250.0, resolver="Fast"), row(5.0, resolver="Fast")]
        s = S.summarize(rows, resolver_order=["Fast", "Slow"], slow_threshold_ms=200)
        self.assertFalse(any(r["resolver"] == "Fast" for r in s["slow"]))
        self.assertEqual(list(s["slow_by_resolver"]), ["Fast", "Slow"])
        self.assertEqual([r["ms"] for r in s["slow_by_resolver"]["Fast"]], [300.0, 250.0])
        self.assertEqual(len(s["slow_by_resolver"]["Slow"]), S.SLOW_PER_RESOLVER_MAX)
        self.assertEqual(s["slow_by_resolver"]["Slow"][0]["ms"], 1249)
        self.assertEqual(s["slow_count_by_resolver"], {"Fast": 2, "Slow": 250})

    def test_merge_runs(self):
        r1 = {"id": "20260101T000000Z", "results": self.rows[:2]}
        r2 = {"id": "20260102T000000Z", "results": self.rows[2:5]}
        merged = S.merge_runs([r1, r2])
        self.assertEqual(len(merged), 5)
        self.assertEqual(merged[0]["run_id"], "20260101T000000Z")
        self.assertEqual(merged[-1]["run_id"], "20260102T000000Z")
        self.assertNotIn("run_id", self.rows[0])  # originals untouched
        s = S.summarize(merged)
        self.assertEqual(s["overall"]["n"], 5)


class RetriedStatsTest(unittest.TestCase):
    def test_retried_counts_retry_then_success_only(self):
        # A query whose every try timed out is a failure; counting it as retried as well charged it
        # twice (REMEDIATION_PLAN.md Phase 8, COR-M2).
        rows = [
            {**row(10), "attempts": 2},
            {**row(10), "attempts": 1},
            {**row(10), "attempts": 3},
            {**row(None, status="timeout"), "attempts": 2},
        ]
        st = S.latency_stats(rows)
        self.assertEqual(st["retried"], 2)
        self.assertEqual(st["retry_rate"], 0.5)
        self.assertEqual(st["failure_rate"], 0.25)

    def test_rows_without_attempts_count_as_one(self):
        st = S.latency_stats([row(10), row(12), {**row(11), "attempts": None}])
        self.assertEqual((st["retried"], st["retry_rate"]), (0, 0.0))
        empty = S.latency_stats([])
        self.assertEqual((empty["retried"], empty["retry_rate"]), (0, 0.0))


def err(error, rcode=None, **kw):
    return {**row(None, "error"), "rcode": rcode, "error": error, **kw}


class FailureKindTest(unittest.TestCase):
    def test_kinds(self):
        cases = [
            (row(5), None),
            (row(None, "timeout"), "timeout"),
            (err("SERVFAIL", "SERVFAIL"), "answer"),
            (err("REFUSED", "REFUSED"), "answer"),
            (err("recv: [Errno 61] Connection refused"), "network"),
            (err("send: [Errno 65] No route to host"), "local"),
            (err("connect: [Errno 51] Network is unreachable"), "local"),
            (err("socket: [Errno 24] Too many open files"), "local"),
            (err("address: [Errno 8] nodename nor servname provided"), "local"),
            (err("invalid label length in 'a..b'"), "local"),
            (err("RuntimeError: boom"), "local"),  # query_fn crashed; the runner recorded it
            (err(None), "local"),
        ]
        for r, kind in cases:
            with self.subTest(error=r.get("error")):
                self.assertEqual(S.failure_kind(r), kind)

    def test_local_errors_are_not_charged(self):
        rows = [row(10), row(12), {**row(11), "attempts": 2}, row(None, "timeout")]
        rows += [err("send: [Errno 65] No route to host") for _ in range(4)]
        st = S.latency_stats(rows)
        self.assertEqual((st["n"], st["ok"], st["local_errors"]), (8, 3, 4))
        self.assertEqual((st["failures"], st["timeouts"], st["errors"]), (1, 1, 0))
        self.assertEqual(st["failure_rate"], 0.25)  # 1 of the 4 queries that left this computer
        self.assertEqual(st["retry_rate"], 0.25)
        only_local = S.latency_stats([err("send: x"), err("send: y")])
        self.assertEqual((only_local["failures"], only_local["failure_rate"]), (0, 0.0))


class IntervalTest(unittest.TestCase):
    def test_wilson_known_values(self):
        lo, hi = S.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=4)
        self.assertAlmostEqual(hi, 0.7634, places=4)
        lo, hi = S.wilson(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.2775, places=4)
        self.assertEqual(S.wilson(0, 0), (0.0, 1.0))
        self.assertEqual(S.wilson(7, 7)[1], 1.0)

    def test_median_ci_ranks_match_the_tables(self):
        # Classic tables of the order-statistic CI of the median (95 %).
        self.assertEqual(S.quantile_ci_ranks(5, 1, 2), (0, 6))  # too few samples: unbounded
        self.assertEqual(S.quantile_ci_ranks(6, 1, 2), (1, 6))
        self.assertEqual(S.quantile_ci_ranks(10, 1, 2), (2, 9))
        self.assertEqual(S.quantile_ci_ranks(20, 1, 2), (6, 15))
        self.assertEqual(S.quantile_ci_ranks(100, 1, 2), (40, 61))

    def test_ci_ranks_cover_at_least_95_percent(self):
        from fractions import Fraction
        from math import comb

        for n in (1, 7, 13, 60, 120, 361):
            for num, den in ((1, 2), (19, 20)):
                lo, hi = S.quantile_ci_ranks(n, num, den)
                q = Fraction(num, den)
                pmf = [comb(n, k) * q**k * (1 - q) ** (n - k) for k in range(n + 1)]
                below, above = sum(pmf[:lo]), sum(pmf[hi:])
                with self.subTest(n=n, q=str(q)):
                    self.assertLessEqual(below, Fraction(1, 40))
                    self.assertLessEqual(above, Fraction(1, 40))
                    # and as tight as that allows
                    if lo < n:
                        self.assertGreater(below + pmf[lo], Fraction(1, 40))
                    if hi > 1:
                        self.assertGreater(above + pmf[hi - 1], Fraction(1, 40))

    def test_normal_approximation_is_within_a_rank_of_exact(self):
        from unittest import mock

        for n in (1001, 1500, 2400):
            for num, den in ((1, 2), (19, 20)):
                approx = S.quantile_ci_ranks(n, num, den)
                S.quantile_ci_ranks.cache_clear()
                with mock.patch.object(S, "_EXACT_CI_MAX_N", 10_000):
                    exact = S.quantile_ci_ranks(n, num, den)
                S.quantile_ci_ranks.cache_clear()
                with self.subTest(n=n, q=f"{num}/{den}"):
                    self.assertLessEqual(abs(approx[0] - exact[0]), 1)
                    self.assertLessEqual(abs(approx[1] - exact[1]), 1)

    def test_quantile_ci_values(self):
        vals = [float(v) for v in range(1, 11)]
        self.assertEqual(S.quantile_ci(vals, 1, 2), [2.0, 9.0])
        self.assertEqual(S.quantile_ci(vals, 19, 20), [8.0, None])  # nothing high enough above
        self.assertIsNone(S.quantile_ci([], 1, 2))
        with self.assertRaises(ValueError):
            S.quantile_ci_ranks(10, 2, 2)

    def test_stats_carry_intervals(self):
        st = S.latency_stats([row(float(v)) for v in range(1, 21)])
        self.assertEqual(st["median_ci"], [6.0, 15.0])
        self.assertEqual(S.latency_stats([])["median_ci"], None)


class TailsDifferTest(unittest.TestCase):
    def test_a_clean_split_of_the_slowest_answers_differs(self):
        fast = [10.0] * 60
        slow = [10.0] * 54 + [200.0] * 6
        self.assertTrue(S.tails_differ(fast, slow))
        self.assertTrue(S.tails_differ(slow, fast))
        self.assertFalse(S.tails_differ(fast, [10.0] * 55 + [200.0] * 5))  # 5 of the 6 slowest: chance
        self.assertFalse(S.tails_differ([], slow))

    def test_unbalanced_samples_keep_the_error_rate(self):
        # Both from the same distribution, sizes 10 vs 600: a test that treats the counts above the
        # pooled p95 as independent binomials said "different" about 8 % of the time; Fisher's test
        # stays under 2.5 % per side (review of PR #15).
        import random

        rng = random.Random(8)
        hits = sum(
            S.tails_differ([rng.expovariate(1) for _ in range(10)], [rng.expovariate(1) for _ in range(600)])
            for _ in range(400)
        )
        self.assertLess(hits / 400, 0.05)


class FirstAnswersTest(unittest.TestCase):
    def test_latency_uses_first_answers_only(self):
        # Round 1 misses the cache (slow), rounds 2 and 3 hit it (fast). Without the split the figures
        # would be mostly cache hits'.
        rows = []
        t = 0.0
        for rnd, base in ((1, 100.0), (2, 5.0), (3, 5.0)):
            for i, d in enumerate(f"d{k}.com" for k in range(20)):
                rows.append({**row(base + i, domain=d, rnd=rnd), "t": t})
                t += 0.1
        s = S.summarize(rows)
        st = s["by_resolver"]["R"]
        self.assertEqual((st["first_n"], st["repeat_n"]), (20, 40))
        self.assertEqual(st["p95"], 118.0)  # 19th of the 20 first answers (nearest rank)
        self.assertEqual((st["median"], st["mean"], st["min"], st["max"]), (109.5, 109.5, 100.0, 119.0))
        self.assertEqual(st["repeat_median"], 14.5)
        self.assertEqual(st["median_ci"], [105.0, 114.0])  # ranks 6 and 15 of 20
        self.assertEqual(st["p95_ci"], [116.0, None])
        self.assertEqual((st["ok"], st["failure_rate"]), (60, 0.0))  # the repeats still count here

    def test_first_is_per_resolver_domain_and_run_by_time(self):
        rows = [
            {**row(9, server="1.0.0.1", domain="A.com"), "t": 0.5},
            {**row(7, server="1.1.1.1", domain="a.com"), "t": 0.2},  # first for (R, a.com)
            {**row(None, "timeout", domain="b.com"), "t": 0.1},  # failed: not an answer
            {**row(8, domain="b.com"), "t": 0.3},  # first answer for (R, b.com)
            {**row(6, resolver="Q", domain="a.com"), "t": 0.9},  # first for (Q, a.com)
            {**row(5, domain="a.com"), "t": 0.1, "run_id": "other"},  # another run: its own first
        ]
        first = S.first_answers(rows)
        self.assertEqual({i for i, r in enumerate(rows) if id(r) in first}, {1, 3, 4, 5})

    def test_unanswered_domains(self):
        def ans(resolver, domain, answers=1, rcode="NOERROR", **kw):
            return {**row(5, resolver=resolver, domain=domain), "answers": answers, "rcode": rcode, **kw}

        rows = [
            ans("A", "x.com"),
            ans("B", "x.com", answers=0, rcode="NXDOMAIN"),  # blocked at B
            ans("C", "x.com", answers=0),  # empty NOERROR at C
            ans("A", "y.com", answers=0),  # nobody has records: no AAAA, say
            ans("B", "y.com", answers=0),
            ans("A", "z.com"),
            ans("B", "z.com", answers=0, truncated=True),  # records cut: can't tell
            ans("C", "w.com", answers=0),
            ans("C", "w.com"),  # C answered it once: not blocked
            ans("A", "w.com"),
        ]
        self.assertEqual(S.unanswered_domains(rows, ["A", "B", "C"]), {"B": ["x.com"], "C": ["x.com"]})


if __name__ == "__main__":
    unittest.main()
