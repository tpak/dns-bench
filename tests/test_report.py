from __future__ import annotations

import unittest

from samples import make_run, row

from dnsbench import analysis, recommend, report


class ReportTest(unittest.TestCase):
    def test_report_render(self):
        run = analysis.finalize(make_run())
        text = report.render_text(run)
        self.assertIn("DNS Bench run 20260925T023456Z", text)
        self.assertIn("Cloudflare: mean=5.5 median=5.5", text)
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


if __name__ == "__main__":
    unittest.main()
