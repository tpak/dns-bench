from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from samples import V1_FIXTURES, V1_RUN_IDS, make_run, row

from dnsbench import analysis, report, storage


def texts(rec):
    return [n["text"] for n in rec["notes"]]


class AnalysisTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = storage.RunRepository(Path(self.tmp.name) / "runs")
        self.runs = analysis.Analysis(self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, run):
        """Save a raw run the way 1.x did (with a stored summary, to show it is never trusted)."""
        run = analysis.finalize(copy.deepcopy(run))
        return self.repo.save(run, report.render_text(run))


class FinalizeTest(unittest.TestCase):
    def test_finalize_adds_the_analysis(self):
        run = analysis.finalize(make_run())
        self.assertEqual(run["analysis_version"], analysis.ANALYSIS_VERSION)
        self.assertEqual(run["recommendation"]["best"], "Cloudflare")
        self.assertEqual(run["summary"]["by_resolver"]["Google"]["timeouts"], 1)


class LoadTest(AnalysisTestBase):
    def test_load_recomputes_whatever_the_file_says(self):
        run = make_run()
        run["summary"] = {"stale": True}
        run["recommendation"] = {"best": "Google", "notes": ["from an old version"]}
        self.repo.dir.mkdir(parents=True)
        (self.repo.dir / "20260925T023456Z.json").write_text(json.dumps(run))
        loaded = self.runs.load("20260925T023456Z")
        self.assertEqual(loaded["recommendation"]["best"], "Cloudflare")
        self.assertEqual(loaded["analysis_version"], analysis.ANALYSIS_VERSION)
        self.assertEqual(self.runs.list_runs()[0]["best"], "Cloudflare")

    def test_load_uses_the_cache_until_the_file_changes(self):
        path = self.save(make_run())
        with mock.patch.object(analysis, "finalize", wraps=analysis.finalize) as fin:
            self.runs.load("20260925T023456Z")
            self.runs.load("20260925T023456Z")
            self.runs.list_runs()
            self.assertEqual(fin.call_count, 1)
            data = json.loads(path.read_text())
            data["host"] = "otherhost-with-longer-name"
            path.write_text(json.dumps(data))
            self.assertEqual(self.runs.list_runs()[0]["host"], "otherhost-with-longer-name")
            self.assertEqual(fin.call_count, 2)

    def test_a_new_analysis_version_recomputes(self):
        self.save(make_run())
        self.runs.list_runs()
        with (
            mock.patch.object(analysis, "ANALYSIS_VERSION", analysis.ANALYSIS_VERSION + 1),
            mock.patch.object(analysis, "finalize", wraps=analysis.finalize) as fin,
        ):
            self.runs.list_runs()
            self.assertEqual(fin.call_count, 1)

    def test_caches_belong_to_the_object(self):
        self.save(make_run())
        self.runs.list_runs()
        other = analysis.Analysis(self.repo)
        with mock.patch.object(analysis, "finalize", wraps=analysis.finalize) as fin:
            other.list_runs()
            self.assertEqual(fin.call_count, 1)

    def test_load_errors(self):
        with self.assertRaises(storage.RunNotFound):
            self.runs.load("20200101T000000Z")
        self.repo.dir.mkdir(parents=True)
        (self.repo.dir / "20260101T000000Z.json").write_text("{ nope")
        with self.assertRaises(storage.CorruptRun):
            self.runs.load("20260101T000000Z")


class ListTest(AnalysisTestBase):
    def test_list_runs_newest_first(self):
        for run_id, started in (
            ("20260101T000000Z", "2026-01-01T00:00:00Z"),
            ("20260301T000000Z", "2026-03-01T00:00:00Z"),
            ("20260201T000000Z", "2026-02-01T00:00:00Z"),
            ("20260301T000000Z", "2026-03-01T00:00:00Z"),  # -> -2
        ):
            self.save(make_run(run_id, started))
        rows = self.runs.list_runs()
        self.assertEqual(
            [r["id"] for r in rows],
            ["20260301T000000Z-2", "20260301T000000Z", "20260201T000000Z", "20260101T000000Z"],
        )
        r = rows[0]
        self.assertEqual(
            set(r),
            {
                "id",
                "started_at",
                "finished_at",
                "duration_s",
                "status",
                "host",
                "n_queries",
                "n_domains",
                "resolvers",
                "best",
                "best_median",
                "medians",
            },
        )
        self.assertEqual(r["medians"], {"Cloudflare": 5.5, "Google": 20.0})
        self.assertEqual((r["n_queries"], r["n_domains"]), (7, 3))
        self.assertEqual(r["resolvers"], ["Cloudflare", "Google"])
        self.assertEqual((r["best"], r["best_median"]), ("Cloudflare", 5.5))  # median of 5,5,6,6
        self.assertEqual(self.runs.latest_id(), "20260301T000000Z-2")

    def test_list_runs_skips_unreadable_files_with_one_warning(self):
        self.save(make_run())
        self.repo.dir.joinpath("20260101T000000Z.json").write_text("{ truncated")
        self.repo.dir.joinpath("20260102T000000Z.json").write_text("[1, 2, 3]")
        self.repo.dir.joinpath("20260103T000000Z.json").write_text(json.dumps({"results": [{"bogus": 1}]}))
        with self.assertLogs("dnsbench.analysis", "WARNING") as logs:
            rows = self.runs.list_runs()
            self.runs.list_runs()  # the same files: no new warnings
        self.assertEqual([r["id"] for r in rows], ["20260925T023456Z"])
        self.assertEqual(len(logs.records), 3)
        self.assertTrue(all("skipping run" in m for m in logs.output))

    def test_list_runs_missing_dir(self):
        self.assertEqual(
            analysis.Analysis(storage.RunRepository(Path(self.tmp.name) / "nope")).list_runs(), []
        )
        self.assertIsNone(self.runs.latest_id())


class AggregateTest(AnalysisTestBase):
    def test_aggregate(self):
        self.save(make_run("20260101T000000Z", "2026-01-01T00:00:00Z", fast_ms=5.0))
        self.save(make_run("20260201T000000Z", "2026-02-01T00:00:00Z", fast_ms=50.0))
        agg = self.runs.aggregate("all")
        self.assertEqual((agg["kind"], agg["analysis_version"]), ("aggregate", analysis.ANALYSIS_VERSION))
        self.assertEqual(agg["run_ids"], ["20260201T000000Z", "20260101T000000Z"])
        self.assertEqual(agg["summary"]["overall"]["n"], 14)
        self.assertEqual(agg["summary"]["by_resolver"]["Cloudflare"]["n"], 8)
        self.assertEqual(agg["config"]["domains"], ["a.com", "b.com"])
        one = self.runs.aggregate(["20260101T000000Z"])
        self.assertEqual(one["summary"]["overall"]["n"], 7)
        with self.assertRaises(storage.RunNotFound):
            self.runs.aggregate(["20250101T000000Z"])
        with self.assertRaises(ValueError):
            self.runs.aggregate(["../x"])
        with self.assertRaises(storage.NoRuns):
            analysis.Analysis(storage.RunRepository(Path(self.tmp.name) / "empty")).aggregate("all")

    def test_a_one_run_aggregate_matches_the_single_run_view(self):
        self.save(make_run())
        run = self.runs.load("20260925T023456Z")
        agg = self.runs.aggregate(["20260925T023456Z"])
        self.assertEqual(agg["recommendation"]["best"], run["recommendation"]["best"])
        self.assertEqual(agg["recommendation"]["ranking"], run["recommendation"]["ranking"])
        self.assertEqual(agg["summary"]["by_resolver"], run["summary"]["by_resolver"])

    def test_odd_data_is_a_corrupt_run_not_a_crash(self):
        # A run file whose rows pass migrate() but can't be analysed: an explicit aggregate of it raised
        # AttributeError (an HTTP 500 with no details) before.
        run = make_run()
        run["config"]["settings"]["timeout_ms"] = {"nested": True}
        self.repo.dir.mkdir(parents=True)
        (self.repo.dir / "20260925T023456Z.json").write_text(json.dumps(run))
        with self.assertRaises(storage.CorruptRun):
            self.runs.aggregate(["20260925T023456Z"])
        with self.assertRaises(storage.CorruptRun):
            self.runs.load("20260925T023456Z")

    def test_aggregate_all_skips_corrupt(self):
        self.save(make_run())
        self.repo.dir.joinpath("20260101T000000Z.json").write_text("{ nope")
        with self.assertLogs("dnsbench.analysis", "WARNING"):
            agg = self.runs.aggregate("all")
        self.assertEqual(agg["run_ids"], ["20260925T023456Z"])
        with self.assertRaises(storage.CorruptRun):
            self.runs.aggregate(["20260101T000000Z", "20260925T023456Z"])

    def test_aggregate_note_counts_runs(self):
        self.save(make_run("20260101T000000Z", "2026-01-01T00:00:00Z"))
        self.assertTrue(
            any("report all" in n for n in texts(self.runs.load("20260101T000000Z")["recommendation"]))
        )
        self.save(make_run("20260201T000000Z", "2026-02-01T00:00:00Z"))
        notes = texts(self.runs.aggregate("all")["recommendation"])
        self.assertTrue(any("Combined from 2 runs" in n for n in notes), notes)
        self.assertFalse(any("report all" in n for n in notes))

    def test_aggregate_coverage(self):
        self.save(make_run("20260101T000000Z", "2026-01-01T00:00:00Z"))
        newer = make_run("20260201T000000Z", "2026-02-01T00:00:00Z")
        newer["results"] = [r for r in newer["results"] if r["resolver"] == "Cloudflare"]
        self.save(newer)
        cov = self.runs.aggregate("all")["coverage"]
        self.assertEqual(cov["Cloudflare"], {"runs": 2, "of": 2, "last_run": "20260201T000000Z"})
        self.assertEqual(cov["Google"], {"runs": 1, "of": 2, "last_run": "20260101T000000Z"})

    def test_aggregate_does_not_recommend_resolver_only_in_old_runs(self):
        # An old run measured Quad9 (fastest); it is disabled now and the newer
        # runs never measured it, so the combined view must not suggest it.
        old = make_run("20260101T000000Z", "2026-01-01T00:00:00Z")
        old["config"]["resolvers"].append({"name": "Quad9", "servers": ["9.9.9.9"], "enabled": True})
        old["results"] += [row("Quad9", "9.9.9.9", d, 1.0) for d in ("a.com", "b.com")]
        self.save(old)
        for i in (2, 3):
            new = make_run(f"2026020{i}T000000Z", f"2026-02-0{i}T00:00:00Z")
            new["config"]["resolvers"].append({"name": "Quad9", "servers": ["9.9.9.9"], "enabled": False})
            self.save(new)
        rec = self.runs.aggregate("all")["recommendation"]
        self.assertEqual(rec["ranking"][0]["resolver"], "Quad9")  # still ranked
        self.assertEqual((rec["best"], rec["backup"]), ("Cloudflare", "Google"))
        self.assertNotIn("9.9.9.9", rec["suggested_servers"])
        self.assertNotIn("Quad9", rec["tied_with"])
        self.assertTrue(
            any(
                n.startswith("Quad9 was measured in only 1 of 3 combined runs") and "not recommended" in n
                for n in texts(rec)
            ),
            rec["notes"],
        )
        self.assertIn("of the current resolvers", rec["summary"])
        # enabled again in the live config: it can be recommended
        rec = self.runs.aggregate("all", current=["Cloudflare", "Google", "Quad9"])["recommendation"]
        self.assertEqual(rec["best"], "Quad9")
        self.assertTrue(
            any("Quad9 was measured in only 1 of 3" in n and "less comparable" in n for n in texts(rec))
        )
        # the newest run measured it: it can be recommended whatever the live config says
        rec = self.runs.aggregate(["20260101T000000Z"], current=["Cloudflare"])["recommendation"]
        self.assertEqual(rec["best"], "Quad9")


class V1RunFixturesTest(unittest.TestCase):
    def setUp(self):
        # A copy, so nothing a test does can change the fixtures.
        self.tmp = tempfile.TemporaryDirectory()
        shutil.copytree(V1_FIXTURES, Path(self.tmp.name) / "runs")
        self.runs = analysis.Analysis(storage.RunRepository(Path(self.tmp.name) / "runs"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_stored_analysis_matches_a_fresh_one(self):
        # Today's analysis of the raw results reproduces what 1.0.0 stored, so recomputing on load changes
        # nothing for these files until the scoring changes on purpose (REMEDIATION_PLAN.md Phase 8),
        # which must then update this test. Two things differ only in form: notes are {code, params, text}
        # (1.0.0 stored the text), and the slow-query rows quoted in the summary carry the fields
        # storage.migrate adds to 1.x rows (attempts, truncated).
        def migrated(rows):
            return [{"attempts": 1, "truncated": False, **r} for r in rows]

        for run_id in V1_RUN_IDS:
            with self.subTest(run_id=run_id):
                stored = json.loads((V1_FIXTURES / f"{run_id}.json").read_text(encoding="utf-8"))
                summary = stored["summary"]
                summary["slow"] = migrated(summary["slow"])
                summary["slow_by_resolver"] = {k: migrated(v) for k, v in summary["slow_by_resolver"].items()}
                fresh = json.loads(json.dumps(self.runs.load(run_id)))  # tuples -> lists, as on disk
                self.assertEqual(fresh["summary"], summary)
                self.assertEqual(
                    {**fresh["recommendation"], "notes": texts(fresh["recommendation"])},
                    stored["recommendation"],
                )

    def test_list_report_and_aggregate(self):
        rows = self.runs.list_runs()
        self.assertEqual([r["id"] for r in rows], V1_RUN_IDS)
        for r in rows:
            self.assertEqual(r["best"], "Cloudflare")
            self.assertIsInstance(r["best_median"], float)
            self.assertEqual(r["host"], "example-host")
        for run_id in V1_RUN_IDS:
            self.assertIn("Recommendation:", report.render_text(self.runs.load(run_id)))
        bundle = self.runs.aggregate("all", current=None)
        self.assertEqual(bundle["run_ids"], V1_RUN_IDS)
        self.assertEqual(bundle["coverage"]["ISP"], {"runs": 2, "of": 2, "last_run": V1_RUN_IDS[0]})
        self.assertIn(bundle["recommendation"]["best"], {r["name"] for r in bundle["config"]["resolvers"]})


if __name__ == "__main__":
    unittest.main()
