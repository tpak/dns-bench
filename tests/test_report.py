from __future__ import annotations

import unittest

from samples import make_run, row

from dnsbench import analysis, recommend, report


class ReportTest(unittest.TestCase):
    def test_report_render(self):
        run = analysis.finalize(make_run())
        text = report.render_text(run)
        self.assertIn("DNS Bench run 20260925T023456Z", text)
        # Phase 8: latency uses each domain's first answer; Cloudflare's second server (6 ms) repeats
        # the first one's domains, so it no longer moves mean and median (they were 5.5)
        self.assertIn("Cloudflare: mean=5.0 median=5.0", text)
        self.assertIn("first_answers=2 repeat_median=6.0", text)
        self.assertIn(
            "Google: mean=20.0 median=20.0 p80=20.0 p95=20.0 p98=20.0 min=20.0 max=20.0 n=3 fail=33.3%", text
        )
        self.assertIn(f"Ranking ({recommend.SCORE_FORMULA}; lower is better):", text)
        self.assertIn("Recommendation:", text)
        self.assertIn("  - Results reflect this network", text)  # the notes' text
        run["status"] = "cancelled"
        self.assertIn("CANCELLED", report.render_text(run))
        run["status"], run["error"] = "partial", "ValueError: boom\x1b[2J (while measuring 1.1.1.1)"
        text = report.render_text(run)
        self.assertIn("[STOPPED BY AN ERROR — partial results]", text)
        self.assertIn("Error:     ValueError: boom\\x1b[2J (while measuring 1.1.1.1)", text)  # escaped
        del run["error"]
        run["status"] = "complete"
        agg = {
            "run_ids": ["20260925T023456Z"],
            "summary": run["summary"],
            "recommendation": run["recommendation"],
            "config": run["config"],
        }
        self.assertIn("all runs combined (1 run)", report.render_text(agg))

    def test_report_all_failed(self):
        run = make_run()
        for r in run["results"]:
            r.update(status="timeout", ms=None)
        text = report.render_text(analysis.finalize(run))
        self.assertIn("mean=- median=-", text)
        self.assertIn("No resolver returned", text)

    def test_report_per_server_columns_align(self):
        run = make_run()
        run["config"]["resolvers"] = [
            {"name": "CleanBrowsing", "servers": ["185.228.168.9"], "enabled": True},
            {"name": "Cloudflare", "servers": ["2606:4700:4700::1111", "1.1.1.1"], "enabled": True},
        ]
        run["results"] = [
            row(res, srv, "a.com", ms)
            for res, srv, ms in (
                ("CleanBrowsing", "185.228.168.9", 9.2),
                ("Cloudflare", "2606:4700:4700::1111", 5.2),
                ("Cloudflare", "1.1.1.1", 5.7),
            )
        ]
        lines = report.render_text(analysis.finalize(run)).splitlines()
        start = lines.index("Per server (ms):")
        block = [ln for ln in lines[start + 1 : start + 6] if ln.strip()]
        self.assertTrue(block[0].split() == ["Resolver", "Server", "Median", "p95", "Fail"], block)
        rows = block[2:]
        self.assertEqual(len(rows), 3)
        # each numeric column ends at the same offset on every row
        for col in (-1, -2, -3):
            ends = {len(ln) - len(" ".join(ln.split()[col:])) for ln in rows}
            self.assertEqual(len(ends), 1, rows)
        self.assertNotIn("Retried", "\n".join(lines))  # tries == 1: nothing retried

    def test_report_escapes_control_characters_from_old_files(self):
        run = make_run()
        for r in run["results"]:
            if r["resolver"] == "Google":
                r["resolver"] = "Goo\x1b[2Jgle‮"
        run["config"]["resolvers"][1]["name"] = "Goo\x1b[2Jgle‮"
        text = report.render_text(analysis.finalize(run))
        self.assertNotIn("\x1b", text)
        self.assertNotIn("‮", text)
        self.assertIn("Goo\\x1b[2Jgle\\u202e: mean=", text)

    def test_report_shows_retries(self):
        run = make_run()
        run["results"][0]["attempts"] = 2
        text = report.render_text(analysis.finalize(run))
        self.assertIn("Retried", text)
        self.assertIn("retried=1", text)

    def test_ranking_marks(self):
        # Two resolvers within noise, the second with failures the score leaves out: "=" and "*", with a
        # space in the other rows so the digits of the right-aligned columns line up.
        run = make_run()
        text = report.render_text(analysis.finalize(run))
        lines = text.splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.startswith("Ranking"))
        rows = lines[start + 3 : start + 5]
        self.assertTrue(rows[0].split()[0] == "1" and rows[1].split()[0] == "2=", rows)
        self.assertEqual(rows[0].index("1 "), rows[1].index("2="), rows)  # the digits line up
        self.assertIn("33.3%*", rows[1])
        self.assertEqual(rows[0].index("0.0% "), rows[1].index("33.3%*") + 1, rows)
        self.assertIn("* not significantly higher", text)
        self.assertIn("= within noise of the resolver above: medians within 2 ms", text)

    def test_every_query_failed_locally(self):
        # No failure rate exists when nothing left this computer: "-", not "0.0%" (review of 1.3.0)
        run = make_run()
        for r in run["results"]:
            if r["resolver"] == "Google":
                r.update(
                    status="error", ms=None, rcode=None, error="connect: [Errno 51] Network is unreachable"
                )
        text = report.render_text(analysis.finalize(run))
        self.assertIn("n=3 fail=-", text)
        self.assertIn("Queries:   7 (4 ok, 0 failed, 3 failed on this computer) across 3 domains", text)

    def test_no_star_with_a_single_resolver(self):
        run = make_run()
        run["results"] = [r for r in run["results"] if r["resolver"] == "Google"]
        text = report.render_text(analysis.finalize(run))
        self.assertIn("33.3%", text)
        self.assertNotIn("33.3%*", text)
        self.assertNotIn("* not significantly higher", text)


if __name__ == "__main__":
    unittest.main()
