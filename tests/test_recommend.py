from __future__ import annotations

import unittest
from pathlib import Path

from dnsbench import recommend as RC
from dnsbench import stats as S

SETTINGS = {"timeout_ms": 1000}
ROOT = Path(__file__).resolve().parents[1]


def notes(rec):
    """The notes' text, as the report and the UI show them."""
    return [n["text"] for n in rec["notes"]]


def rows_for(resolver, server, latencies, timeouts=0, domain_prefix="d"):
    out = []
    for i, ms in enumerate(latencies):
        out.append(
            {
                "resolver": resolver,
                "server": server,
                "domain": f"{domain_prefix}{i}.com",
                "round": 1,
                "status": "ok",
                "ms": float(ms),
                "rcode": "NOERROR",
                "answers": 1,
                "error": None,
                "t": 0.0,
            }
        )
    for i in range(timeouts):
        out.append(
            {
                "resolver": resolver,
                "server": server,
                "domain": f"t{i}.com",
                "round": 1,
                "status": "timeout",
                "ms": None,
                "rcode": None,
                "answers": 0,
                "error": "timeout",
                "t": 0.0,
            }
        )
    return out


def summary(*groups):
    rows = [r for g in groups for r in g]
    return S.summarize(rows)


class RecommendTest(unittest.TestCase):
    def test_score_formula(self):
        st = {"ok": 10, "median": 10.0, "p95": 20.0, "mean": 12.0, "failure_rate": 0.05}
        self.assertAlmostEqual(RC.score(st, 1000), 0.5 * 10 + 0.3 * 20 + 0.2 * 12 + 0.05 * 1000 * 2)
        self.assertIsNone(RC.score({"ok": 0}, 1000))

    def test_ranking_and_fields(self):
        s = summary(
            rows_for("Slow", "9.9.9.9", [40] * 40),
            rows_for("Slow", "149.112.112.112", [45] * 40),
            rows_for("Fast", "1.1.1.1", [5] * 40),
            rows_for("Fast", "1.0.0.1", [4] * 40),
            rows_for("Mid", "8.8.8.8", [20] * 40),
            rows_for("Mid", "8.8.4.4", [22] * 40),
        )
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["best"], "Fast")
        self.assertEqual(rec["backup"], "Mid")
        self.assertEqual([e["resolver"] for e in rec["ranking"]], ["Fast", "Mid", "Slow"])
        self.assertEqual([e["rank"] for e in rec["ranking"]], [1, 2, 3])
        top = rec["ranking"][0]
        self.assertEqual(
            set(top),
            {
                "rank",
                "resolver",
                "score",
                "median",
                "p95",
                "mean",
                "failure_rate",
                "retry_rate",
                "ok",
                "n",
                "fastest_server",
                "median_ci",
                "p95_ci",
                "failure_ci",
                "failures_counted",
                "retries_counted",
                "ties",
            },
        )
        self.assertEqual(top["fastest_server"], "1.0.0.1")  # lower median wins
        self.assertEqual(rec["suggested_servers"], ["1.0.0.1", "8.8.8.8"])
        self.assertEqual(rec["tied_with"], [])
        self.assertIn("Use Fast", rec["summary"])
        self.assertIn("1.0.0.1 first", rec["summary"])
        self.assertIn("8.8.8.8 (Mid) second", rec["summary"])
        self.assertIn("lowest median", rec["summary"])
        self.assertIn("no failures", rec["summary"])
        self.assertTrue(any("steadier" in n for n in notes(rec)))

    def test_failures_penalised(self):
        # "Flaky" is faster when it answers but drops 10% of queries.
        s = summary(
            rows_for("Flaky", "1.1.1.1", [3] * 45, timeouts=5), rows_for("Solid", "8.8.8.8", [15] * 50)
        )
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["best"], "Solid")
        flaky = next(e for e in rec["ranking"] if e["resolver"] == "Flaky")
        self.assertAlmostEqual(flaky["failure_rate"], 0.1)
        self.assertTrue(any("Flaky" in n and "failed" in n for n in notes(rec)))

    def test_tie_detection(self):
        s = summary(
            rows_for("A", "1.1.1.1", [10.0] * 40),
            rows_for("B", "8.8.8.8", [10.5] * 40),
            rows_for("C", "9.9.9.9", [30.0] * 40),
        )
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["best"], "A")
        self.assertEqual(rec["tied_with"], ["B"])  # within max(2 ms, 10 %)
        self.assertTrue(any("within noise" in n for n in notes(rec)))

    def test_ties_come_from_overlapping_intervals(self):
        # Phase 8 replaced the 10 % score margin with the measurements' own intervals. A and B spread
        # widely and their medians' intervals overlap, so 5 ms between them is noise; C's values are
        # tight and clearly higher, so it isn't tied although its median is only 9 ms more.
        spread = [float(v) for v in range(80, 121)]  # 80..120, median 100
        s = summary(
            rows_for("A", "1.1.1.1", spread),
            rows_for("B", "8.8.8.8", [v + 5 for v in spread]),
            rows_for("C", "9.9.9.9", [109.0] * 41),
        )
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["best"], "A")
        self.assertEqual(rec["tied_with"], ["B"])
        a = rec["ranking"][0]
        self.assertEqual(a["median_ci"], [93.0, 107.0])  # ranks 14 and 28 of 41
        self.assertEqual(a["ties"], ["B"])
        self.assertTrue(any("no significant difference in median" in n for n in notes(rec)), rec["notes"])

    def test_backup_ties_are_reported(self):
        s = summary(
            rows_for("Best", "1.1.1.1", [5.0] * 40),
            rows_for("B", "8.8.8.8", [20.0] * 40),
            rows_for("C", "9.9.9.9", [20.5] * 40),
            rows_for("D", "208.67.222.222", [60.0] * 40),
        )
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual((rec["best"], rec["backup"]), ("Best", "B"))
        self.assertEqual(rec["tied_with"], [])
        self.assertEqual(rec["backup_tied_with"], ["C"])
        note = next(n for n in rec["notes"] if n["code"] == "backup_tie")
        self.assertEqual(note["params"], {"resolver": "B", "tied_with": ["C"]})
        self.assertIn("either can be the secondary", note["text"])

    def test_fastest_server_tiebreak_on_p95(self):
        # Same median (10); p95 50 vs 20. With 200 answers each the p95 intervals are apart, so the
        # lower p95 wins.
        s = summary(
            rows_for("A", "1.1.1.1", [10] * 180 + [50] * 20), rows_for("A", "1.0.0.1", [10] * 180 + [20] * 20)
        )
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["ranking"][0]["fastest_server"], "1.0.0.1")

    def test_p95_of_few_answers_is_noise_between_servers(self):
        # Phase 8: the same shape with 20 answers each is two slow answers against none. The p95
        # intervals overlap, so the servers are within noise and config order decides.
        s = summary(
            rows_for("A", "1.1.1.1", [10] * 18 + [50, 50]), rows_for("A", "1.0.0.1", [10] * 18 + [20, 20])
        )
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["ranking"][0]["fastest_server"], "1.1.1.1")

    def test_fastest_server_ignores_dead_server(self):
        s = summary(rows_for("A", "1.1.1.1", [], timeouts=10), rows_for("A", "1.0.0.1", [30] * 10))
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["ranking"][0]["fastest_server"], "1.0.0.1")

    def test_low_sample_note(self):
        s = summary(rows_for("A", "1.1.1.1", [10] * 5), rows_for("B", "8.8.8.8", [20] * 40))
        rec = RC.recommend(s, SETTINGS)
        self.assertTrue(any("A" in n and "low sample size" in n for n in notes(rec)))
        self.assertFalse(any(n.startswith("B:") and "low sample" in n for n in notes(rec)))

    def test_single_resolver(self):
        s = summary(rows_for("Only", "1.1.1.1", [10] * 40), rows_for("Only", "1.0.0.1", [12] * 40))
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["best"], "Only")
        self.assertIsNone(rec["backup"])
        self.assertEqual(rec["suggested_servers"], ["1.1.1.1", "1.0.0.1"])
        self.assertTrue(any("same provider" in n for n in notes(rec)))

    def test_resolver_with_no_answers_excluded(self):
        s = summary(rows_for("Dead", "10.0.0.1", [], timeouts=20), rows_for("Live", "1.1.1.1", [9] * 40))
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual([e["resolver"] for e in rec["ranking"]], ["Live"])
        self.assertTrue(any("Dead" in n and "no successful answers" in n for n in notes(rec)))

    def test_no_ok_samples(self):
        s = summary(rows_for("A", "1.1.1.1", [], timeouts=5), rows_for("B", "8.8.8.8", [], timeouts=5))
        rec = RC.recommend(s, SETTINGS)
        self.assertIsNone(rec["best"])
        self.assertIsNone(rec["backup"])
        self.assertEqual(rec["ranking"], [])
        self.assertEqual(rec["suggested_servers"], [])
        self.assertIn("No resolver", rec["summary"])

    def test_empty_summary(self):
        rec = RC.recommend(S.summarize([]), SETTINGS)
        self.assertIsNone(rec["best"])

    def test_failure_summary_text(self):
        s = summary(rows_for("A", "1.1.1.1", [10] * 99, timeouts=1), rows_for("B", "8.8.8.8", [50] * 100))
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["best"], "A")
        self.assertIn("1.0% failures (1 of 100)", rec["summary"])

    def test_timeout_setting_changes_penalty(self):
        # 10 % failures (significant against none): 1000 ms of penalty at a 5 s timeout, 40 ms at 200 ms
        s = summary(
            rows_for("Flaky", "1.1.1.1", [3] * 90, timeouts=10), rows_for("Solid", "8.8.8.8", [60] * 100)
        )
        self.assertEqual(RC.recommend(s, {"timeout_ms": 5000})["best"], "Solid")
        self.assertEqual(RC.recommend(s, {"timeout_ms": 200})["best"], "Flaky")


def error_rows(resolver, server, count, prefix="e"):
    """Instant local failures (e.g. 'send: No route to host' for IPv6 on an IPv4-only network)."""
    return [
        {
            "resolver": resolver,
            "server": server,
            "domain": f"{prefix}{i}.com",
            "round": 1,
            "status": "error",
            "ms": None,
            "rcode": None,
            "answers": 0,
            "error": "send: No route to host",
            "t": 0.0,
        }
        for i in range(count)
    ]


def retried(rows, attempts=2):
    return [{**r, "attempts": attempts} for r in rows]


class ServerChoiceTest(unittest.TestCase):
    """Which IP of a resolver is put first (and suggested as secondary)."""

    def test_flaky_fast_server_not_put_first(self):
        s = summary(rows_for("A", "1.1.1.1", [10] * 45, timeouts=15), rows_for("A", "1.0.0.1", [11] * 60))
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["ranking"][0]["fastest_server"], "1.0.0.1")

    def test_flaky_sibling_not_suggested_as_secondary(self):
        s = summary(
            rows_for("Cloudflare", "1.1.1.1", [6.0] * 40),
            rows_for("Cloudflare", "1.0.0.1", [5.9] * 40, timeouts=40),
            rows_for("Google", "8.8.8.8", [3.0] * 80),
            rows_for("Google", "8.8.4.4", [3.1] * 80),
        )
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["suggested_servers"], ["8.8.8.8", "1.1.1.1"])
        # the note names the IP that failed, not just the provider
        self.assertTrue(any("1.0.0.1" in n and "prefer 1.1.1.1" in n for n in notes(rec)), rec["notes"])

    def test_flaky_primary_of_best_resolver(self):
        # ISP still wins overall, but its flaky IP must not be the primary
        s = summary(
            rows_for("ISP", "192.0.2.53", [2.0] * 48, timeouts=12),
            rows_for("ISP", "192.0.2.54", [2.3] * 60),
            rows_for("Google", "8.8.8.8", [300.0] * 60),
        )
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["best"], "ISP")
        self.assertEqual(rec["suggested_servers"][0], "192.0.2.54")

    def test_servers_within_noise_use_config_order(self):
        # 0.1 ms apart is noise: keep config order instead of flipping run to run
        s = summary(
            rows_for("Cloudflare", "1.1.1.1", [5.8] * 40),
            rows_for("Cloudflare", "1.0.0.1", [5.7] * 40),
            rows_for("Google", "8.8.8.8", [20] * 40),
        )
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["ranking"][0]["fastest_server"], "1.1.1.1")
        self.assertTrue(
            any("within noise of each other" in n and "1.0.0.1" in n for n in notes(rec)), rec["notes"]
        )

    def test_tied_server_with_more_failures_not_first(self):
        s = summary(rows_for("A", "1.1.1.1", [5.7] * 90, timeouts=10), rows_for("A", "1.0.0.1", [5.8] * 100))
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["ranking"][0]["fastest_server"], "1.0.0.1")

    def test_insignificant_failures_do_not_reorder_servers(self):
        # Phase 8: 2 of 100 failures against none is noise (it was a 1-point threshold), so the servers
        # are within noise and config order decides.
        s = summary(rows_for("A", "1.1.1.1", [5.7] * 98, timeouts=2), rows_for("A", "1.0.0.1", [5.8] * 100))
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["ranking"][0]["fastest_server"], "1.1.1.1")


class DeadServerTest(unittest.TestCase):
    def test_dead_server_left_out_of_resolver_score(self):
        s = summary(
            rows_for("Cloudflare", "1.1.1.1", [4] * 60),
            error_rows("Cloudflare", "2606:4700:4700::1111", 60),
            rows_for("Google", "8.8.8.8", [45] * 60),
            rows_for("Google", "8.8.4.4", [46] * 60),
        )
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["best"], "Cloudflare")
        self.assertEqual(rec["suggested_servers"], ["1.1.1.1", "8.8.8.8"])
        cf = rec["ranking"][0]
        self.assertEqual(cf["score"], 4.0)
        # Phase 8: "No route to host" is a local error, not the resolver's failure (it was 0.5)
        self.assertEqual(cf["failure_rate"], 0.0)
        self.assertTrue(any(n["code"] == "local_errors" for n in rec["notes"]), rec["notes"])
        self.assertTrue(any("2606:4700:4700::1111" in n and "never answered" in n for n in notes(rec)))
        self.assertNotIn("50%", rec["summary"])
        self.assertIn("not counting 2606:4700:4700::1111", rec["summary"])
        # instant errors are not described as costing a timeout
        self.assertFalse(any("Cloudflare" in n and "timeout" in n for n in notes(rec)), rec["notes"])

    def test_dead_sibling_does_not_bury_fastest_server(self):
        s = summary(
            rows_for("Cloudflare", "1.1.1.1", [7] * 60),
            rows_for("Cloudflare", "1.0.0.1", [], timeouts=60),
            rows_for("Google", "8.8.8.8", [31] * 60),
            rows_for("ISP", "192.0.2.53", [26] * 60),
        )
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["best"], "Cloudflare")
        self.assertEqual(rec["suggested_servers"][0], "1.1.1.1")
        self.assertTrue(any("1.0.0.1" in n and "never answered" in n for n in notes(rec)))

    def test_error_only_failures_note_does_not_claim_timeouts(self):
        # Error answers, not local errors: since Phase 8 those aren't failures at all
        servfail = [{**r, "rcode": "SERVFAIL", "error": "SERVFAIL"} for r in error_rows("A", "1.1.1.1", 10)]
        s = summary(rows_for("A", "1.1.1.1", [10] * 90) + servfail, rows_for("B", "8.8.8.8", [500] * 100))
        rec = RC.recommend(s, SETTINGS)
        note = next(n for n in notes(rec) if n.startswith("A:") and "failed" in n)
        self.assertNotIn("timeout", note)


class RetryTest(unittest.TestCase):
    def test_retried_queries_are_penalised(self):
        # both answer in 10 ms, but every Lossy query needed a retry after a timeout
        s = summary(
            retried(rows_for("Lossy", "10.0.0.1", [10] * 20)), rows_for("Clean", "10.0.0.2", [10] * 20)
        )
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["best"], "Clean")
        lossy = next(e for e in rec["ranking"] if e["resolver"] == "Lossy")
        self.assertEqual(lossy["retry_rate"], 1.0)
        self.assertTrue(any("Lossy" in n and "retry" in n for n in notes(rec)))
        self.assertIn("no failures", rec["summary"])  # about Clean

    def test_retrying_sibling_not_put_first(self):
        s = summary(
            retried(rows_for("A", "1.1.1.1", [5.0] * 40)),
            rows_for("A", "1.0.0.1", [5.2] * 40),
            rows_for("B", "8.8.8.8", [30] * 40),
        )
        rec = RC.recommend(s, SETTINGS)
        a = next(e for e in rec["ranking"] if e["resolver"] == "A")
        self.assertEqual(a["fastest_server"], "1.0.0.1")
        self.assertEqual(rec["suggested_servers"], ["8.8.8.8", "1.0.0.1"])
        self.assertTrue(
            any("1.1.1.1 needed a retry for 100% of its queries" in n for n in notes(rec)), rec["notes"]
        )

    def test_summary_mentions_retries_of_best(self):
        s = summary(retried(rows_for("Lossy", "10.0.0.1", [10] * 20)))
        rec = RC.recommend(s, SETTINGS)
        self.assertNotIn("with no failures.", rec["summary"])
        self.assertIn("20 of 20 queries needed a retry", rec["summary"])


class RedundancyTest(unittest.TestCase):
    def test_renamed_resolver_is_not_the_backup(self):
        # combined runs: ISP was renamed Telstra, same IPs (one spelled IPv4-mapped)
        s = summary(
            rows_for("Telstra", "192.0.2.53", [10.0] * 40),
            rows_for("Telstra", "192.0.2.54", [11.0] * 40),
            rows_for("ISP", "::ffff:192.0.2.53", [10.1] * 40),
            rows_for("ISP", "192.0.2.54", [11.1] * 40),
            rows_for("Google", "8.8.8.8", [30.0] * 40),
        )
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["best"], "Telstra")
        self.assertEqual(rec["backup"], "Google")
        self.assertEqual(rec["suggested_servers"], ["192.0.2.53", "8.8.8.8"])
        self.assertNotIn("ISP", rec["tied_with"])
        self.assertTrue(any("share server IPs" in n and "ISP" in n for n in notes(rec)))

    def test_only_alias_left_gives_distinct_ips(self):
        s = summary(
            rows_for("Telstra", "192.0.2.53", [10.0] * 40),
            rows_for("Telstra", "192.0.2.54", [11.0] * 40),
            rows_for("ISP", "192.0.2.53", [10.1] * 40),
            rows_for("ISP", "192.0.2.54", [11.1] * 40),
        )
        rec = RC.recommend(s, SETTINGS)
        self.assertIsNone(rec["backup"])
        self.assertEqual(rec["suggested_servers"], ["192.0.2.53", "192.0.2.54"])
        self.assertFalse(any("Only one resolver" in n for n in notes(rec)))
        self.assertTrue(any("shares servers with Telstra" in n for n in notes(rec)))

    def test_single_resolver_single_server(self):
        s = summary(rows_for("ISP", "192.0.2.53", [10] * 60))
        rec = RC.recommend(s, SETTINGS)
        self.assertEqual(rec["suggested_servers"], ["192.0.2.53"])
        self.assertFalse(any("both suggested servers" in n for n in notes(rec)))
        self.assertTrue(any("no secondary server" in n for n in notes(rec)))

    def test_only_resolver_tested_wording(self):
        s = summary(rows_for("Quad9", "9.9.9.9", [9.7] * 40), rows_for("Quad9", "149.112.112.112", [12] * 40))
        rec = RC.recommend(s, SETTINGS)
        self.assertIn("the only resolver tested", rec["summary"])
        self.assertNotIn("answered", rec["summary"])
        self.assertTrue(
            any(n.startswith("Only one resolver was tested") and "same provider" in n for n in notes(rec))
        )

    def test_only_resolver_answered_wording(self):
        s = summary(rows_for("Dead", "10.0.0.1", [], timeouts=20), rows_for("Live", "1.1.1.1", [9] * 40))
        rec = RC.recommend(s, SETTINGS)
        self.assertIn("the only resolver that answered", rec["summary"])
        self.assertTrue(any(n.startswith("Only one resolver answered") for n in notes(rec)))


class CombinedRunsNoteTest(unittest.TestCase):
    def test_aggregate_note_is_not_self_referential(self):
        s = summary(rows_for("A", "1.1.1.1", [10] * 40), rows_for("B", "8.8.8.8", [20] * 40))
        texts = notes(RC.recommend(s, SETTINGS, n_runs=3))
        self.assertTrue(any("3 runs" in n and "steadier" in n for n in texts))
        self.assertFalse(any("report all" in n for n in texts))
        for n_runs in (None, 1):
            texts = notes(RC.recommend(s, SETTINGS, n_runs=n_runs))
            self.assertTrue(any("report all" in n for n in texts))


class LowSampleNoteTest(unittest.TestCase):
    def test_one_note_for_several_low_sample_resolvers(self):
        s = summary(
            rows_for("A", "1.1.1.1", [10] * 20),
            rows_for("B", "8.8.8.8", [20] * 20),
            rows_for("C", "9.9.9.9", [30] * 12),
            rows_for("D", "4.4.4.4", [40] * 40),
        )
        low = [n for n in notes(RC.recommend(s, SETTINGS)) if "low sample size" in n]
        self.assertEqual(
            low,
            [
                "A (20), B (20) and C (12): fewer than 30 successful samples each "
                "— low sample size, run more rounds for a steadier answer."
            ],
        )
        s = summary(rows_for("A", "1.1.1.1", [10] * 20), rows_for("B", "8.8.8.8", [20] * 20))
        low = [n for n in notes(RC.recommend(s, SETTINGS)) if "low sample size" in n]
        self.assertEqual(
            low,
            [
                "A and B: only 20 successful samples each — low sample size, "
                "run more rounds for a steadier answer."
            ],
        )


class CurrentResolversTest(unittest.TestCase):
    def test_stale_resolver_is_ranked_but_not_recommended(self):
        s = summary(
            rows_for("Old", "9.9.9.9", [2.0] * 40),
            rows_for("Now", "1.1.1.1", [10.0] * 40),
            rows_for("Now", "1.0.0.1", [11.0] * 40),
        )
        cov = {
            "Old": {"runs": 1, "of": 4, "last_run": "20260101T000000Z"},
            "Now": {"runs": 4, "of": 4, "last_run": "20260401T000000Z"},
        }
        rec = RC.recommend(s, SETTINGS, n_runs=4, coverage=cov, current={"Now"})
        self.assertEqual([e["resolver"] for e in rec["ranking"]], ["Old", "Now"])
        self.assertEqual(rec["best"], "Now")
        self.assertIsNone(rec["backup"])
        self.assertEqual(rec["suggested_servers"], ["1.1.1.1", "1.0.0.1"])
        self.assertIn("the only current resolver that answered", rec["summary"])
        self.assertTrue(
            any(n.startswith("Old was measured in only 1 of 4 combined runs") for n in notes(rec))
        )
        self.assertTrue(any(n.startswith("No other current resolver answered") for n in notes(rec)))

    def test_current_wording_only_when_a_stale_resolver_did_better(self):
        s = summary(
            rows_for("Old", "9.9.9.9", [50.0] * 40),
            rows_for("A", "1.1.1.1", [10.0] * 40),
            rows_for("B", "8.8.8.8", [20.0] * 40),
        )
        rec = RC.recommend(s, SETTINGS, current={"A", "B"})
        self.assertEqual(rec["best"], "A")
        self.assertNotIn("current resolvers", rec["summary"])
        s = summary(
            rows_for("Old", "9.9.9.9", [5.0] * 40),
            rows_for("A", "1.1.1.1", [10.0] * 40),
            rows_for("B", "8.8.8.8", [20.0] * 40),
        )
        rec = RC.recommend(s, SETTINGS, current={"A", "B"})
        self.assertEqual((rec["best"], rec["backup"]), ("A", "B"))
        self.assertIn(
            "had the lowest median (10.0 ms) and p95 (10.0 ms) of the current resolvers", rec["summary"]
        )

    def test_no_current_resolver_answered_falls_back_to_all(self):
        s = summary(rows_for("Old", "9.9.9.9", [2.0] * 40), rows_for("Gone", "8.8.8.8", [9.0] * 40))
        rec = RC.recommend(s, SETTINGS, current={"Missing"})
        self.assertEqual((rec["best"], rec["backup"]), ("Old", "Gone"))


class StructureTest(unittest.TestCase):
    """rank -> choose -> explain, and the notes' codes."""

    def test_notes_have_a_code_params_and_text(self):
        s = summary(
            rows_for("A", "1.1.1.1", [10] * 20, timeouts=2),
            rows_for("A", "1.0.0.1", [], timeouts=5),
            rows_for("B", "8.8.8.8", [11] * 20),
            rows_for("Dead", "9.9.9.9", [], timeouts=3),
        )
        rec = RC.recommend(s, SETTINGS)
        codes = [n["code"] for n in rec["notes"]]
        # "tie": since Phase 8, A's 2 failures in 22 aren't significantly more than B's none, so they
        # don't count and A and B (10 vs 11 ms) are within noise
        self.assertEqual(
            codes, ["no_answers", "server_never_answered", "failure_rate", "low_samples", "tie", "one_time"]
        )
        failure = rec["notes"][2]
        self.assertFalse(failure["params"]["counted"])
        self.assertIn("so the score doesn't count it", failure["text"])
        for n in rec["notes"]:
            self.assertEqual(set(n), {"code", "params", "text"})
            self.assertIsInstance(n["params"], dict)
        dead = rec["notes"][1]
        self.assertEqual(dead["params"], {"resolver": "A", "server": "1.0.0.1", "failed": 5, "n": 5})

    def test_rank_and_choose(self):
        s = summary(rows_for("Slow", "8.8.8.8", [30] * 40), rows_for("Fast", "1.1.1.1", [10] * 40))
        ranking, no_answers = RC.rank(s, 1000.0)
        self.assertEqual([r.name for r in ranking], ["Fast", "Slow"])
        self.assertEqual([r.entry["rank"] for r in ranking], [1, 2])
        self.assertEqual(no_answers, [])
        choice = RC.choose(ranking)
        self.assertEqual((choice.best.name, choice.backup.name if choice.backup else None), ("Fast", "Slow"))
        self.assertEqual(choice.suggested, ["1.1.1.1", "8.8.8.8"])
        choice = RC.choose(ranking, current=["Slow"])  # only Slow may be recommended now
        self.assertEqual(choice.best.name, "Slow")
        self.assertEqual([r.name for r in choice.stale], ["Fast"])

    def test_the_formula_is_stated_once(self):
        self.assertEqual(
            RC.SCORE_FORMULA,
            "score = 0.5 × median + 0.3 × p95 + 0.2 × mean + failure_rate × timeout_ms × 2 "  # noqa: RUF001 - as in the README
            "+ retry_rate × timeout_ms",  # noqa: RUF001 - as above
        )
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertTrue(RC.SCORE_FORMULA in readme, "README's formula")
        self.assertTrue(RC.COUNTED_RATES in " ".join(readme.split()), "README's counting rule")


class MeasurementNotesTest(unittest.TestCase):
    def test_local_errors_note_and_uncounted(self):
        s = summary(rows_for("A", "1.1.1.1", [10] * 40), error_rows("A", "1.1.1.1", 3))
        rec = RC.recommend(s, SETTINGS)
        note = next(n for n in rec["notes"] if n["code"] == "local_errors")
        self.assertEqual(note["params"], {"count": 3, "n": 43})
        self.assertIn("with no failures", rec["summary"])

    def test_first_answers_note_only_with_repeats(self):
        once = summary(rows_for("A", "1.1.1.1", [10] * 40))
        self.assertFalse(any(n["code"] == "first_answers" for n in RC.recommend(once, SETTINGS)["notes"]))
        twice = summary(rows_for("A", "1.1.1.1", [30] * 40), rows_for("A", "1.0.0.1", [10] * 40))
        note = next(n for n in RC.recommend(twice, SETTINGS)["notes"] if n["code"] == "first_answers")
        self.assertEqual(note["params"], {"first_median": 30.0, "repeat_median": 10.0, "repeats": 40})

    def test_unanswered_domains_note(self):
        blocked = [{**r, "answers": 0, "rcode": "NXDOMAIN"} for r in rows_for("Filter", "9.9.9.9", [10] * 7)]
        s = summary(rows_for("Open", "1.1.1.1", [10] * 7), blocked)
        note = next(n for n in RC.recommend(s, SETTINGS)["notes"] if n["code"] == "unanswered")
        self.assertEqual(note["params"]["resolver"], "Filter")
        self.assertEqual(len(note["params"]["domains"]), 7)
        self.assertIn(
            "7 domains that other resolvers answered (d0.com, d1.com, d2.com, d3.com, d4.com, …)",
            note["text"],
        )


if __name__ == "__main__":
    unittest.main()
