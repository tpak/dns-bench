import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dnsbench import config as C  # noqa: E402
from dnsbench import report, storage  # noqa: E402


def make_run(run_id="20260925T023456Z", started="2026-09-25T02:34:56Z", fast_ms=5.0):
    cfg = C.default_config()
    cfg["resolvers"] = [{"name": "Cloudflare", "servers": ["1.1.1.1", "1.0.0.1"], "enabled": True},
                        {"name": "Google", "servers": ["8.8.8.8"], "enabled": True}]
    cfg["domains"] = ["a.com", "b.com"]
    results = []
    t = 0.0
    for res, srv, base in (("Cloudflare", "1.1.1.1", fast_ms), ("Cloudflare", "1.0.0.1", fast_ms + 1),
                           ("Google", "8.8.8.8", 20.0)):
        for d in cfg["domains"]:
            results.append({"resolver": res, "server": srv, "domain": d, "round": 1, "status": "ok",
                            "ms": base, "rcode": "NOERROR", "answers": 1, "error": None, "t": t})
            t += 0.25
    results.append({"resolver": "Google", "server": "8.8.8.8", "domain": "c.com", "round": 1,
                    "status": "timeout", "ms": None, "rcode": None, "answers": 0, "error": "timeout",
                    "t": t})
    return {"id": run_id, "version": "1.0.0", "started_at": started, "finished_at": started,
            "duration_s": 1.5, "host": "testhost", "status": "complete", "config": cfg,
            "results": results}


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "runs"

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_run_id(self):
        for good in ("20260925T023456Z", "20260925T023456Z-2", "20260925T023456Z-15"):
            self.assertTrue(storage.valid_run_id(good), good)
        for bad in ("", "latest", "../etc/passwd", "20260925T023456Z/../x", "20260925T023456",
                    "20260925T023456Z-", "20260925T023456Z-a", "2026-09-25", None, 5,
                    "20260925T023456Z\n"):
            self.assertFalse(storage.valid_run_id(bad), bad)

    def test_save_writes_json_and_txt(self):
        run = make_run()
        path = storage.save_run(run, self.dir)
        self.assertEqual(path, self.dir / "20260925T023456Z.json")
        self.assertTrue(path.exists())
        txt = self.dir / "20260925T023456Z.txt"
        self.assertTrue(txt.exists())
        saved = json.loads(path.read_text())
        self.assertIn("summary", saved)
        self.assertIn("recommendation", saved)
        self.assertEqual(saved["recommendation"]["best"], "Cloudflare")
        self.assertEqual(saved["summary"]["by_resolver"]["Google"]["timeouts"], 1)
        self.assertEqual(saved["results"], run["results"])
        self.assertIn("Cloudflare: mean=", txt.read_text())
        self.assertIn("Recommendation: Use Cloudflare", txt.read_text())
        self.assertIn('\n  "id": ', path.read_text())  # pretty-printed
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith(".")], [])

    def test_id_collision_never_overwrites(self):
        p1 = storage.save_run(make_run(fast_ms=5.0), self.dir)
        original = p1.read_text()
        run2 = make_run(fast_ms=9.0)
        p2 = storage.save_run(run2, self.dir)
        run3 = make_run(fast_ms=11.0)
        p3 = storage.save_run(run3, self.dir)
        self.assertEqual(p2.name, "20260925T023456Z-2.json")
        self.assertEqual(p3.name, "20260925T023456Z-3.json")
        self.assertEqual(run2["id"], "20260925T023456Z-2")
        self.assertEqual(p1.read_text(), original)
        self.assertEqual(json.loads(p2.read_text())["id"], "20260925T023456Z-2")
        self.assertTrue((self.dir / "20260925T023456Z-3.txt").exists())

    def test_list_runs_newest_first(self):
        storage.save_run(make_run("20260101T000000Z", "2026-01-01T00:00:00Z"), self.dir)
        storage.save_run(make_run("20260301T000000Z", "2026-03-01T00:00:00Z"), self.dir)
        storage.save_run(make_run("20260201T000000Z", "2026-02-01T00:00:00Z"), self.dir)
        storage.save_run(make_run("20260301T000000Z", "2026-03-01T00:00:00Z"), self.dir)  # -> -2
        rows = storage.list_runs(self.dir)
        self.assertEqual([r["id"] for r in rows], ["20260301T000000Z-2", "20260301T000000Z",
                                                   "20260201T000000Z", "20260101T000000Z"])
        r = rows[0]
        self.assertEqual(set(r), {"id", "started_at", "finished_at", "duration_s", "status", "host",
                                  "n_queries", "n_domains", "resolvers", "best", "best_median",
                                  "medians"})
        self.assertEqual(r["medians"], {"Cloudflare": 5.5, "Google": 20.0})
        self.assertEqual(r["n_queries"], 7)
        self.assertEqual(r["n_domains"], 3)
        self.assertEqual(r["resolvers"], ["Cloudflare", "Google"])
        self.assertEqual(r["best"], "Cloudflare")
        self.assertEqual(r["best_median"], 5.5)  # median of 5,5,6,6
        self.assertEqual(storage.latest_run_id(self.dir), "20260301T000000Z-2")

    def test_list_runs_tolerates_corrupt_files(self):
        storage.save_run(make_run(), self.dir)
        (self.dir / "20260101T000000Z.json").write_text("{ truncated")
        (self.dir / "20260102T000000Z.json").write_text("[1, 2, 3]")
        (self.dir / "notes.json").write_text("{}")  # not a run id: ignored
        (self.dir / "20260103T000000Z.json").write_text(json.dumps({"results": [{"bogus": 1}]}))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rows = storage.list_runs(self.dir)
        self.assertEqual([r["id"] for r in rows], ["20260925T023456Z"])
        self.assertIn("skipping", err.getvalue())

    def test_list_runs_missing_dir(self):
        self.assertEqual(storage.list_runs(self.dir / "nope"), [])

    def test_list_runs_cache_refreshes(self):
        path = storage.save_run(make_run(), self.dir)
        self.assertEqual(storage.list_runs(self.dir)[0]["host"], "testhost")
        data = json.loads(path.read_text())
        data["host"] = "otherhost-with-longer-name"
        path.write_text(json.dumps(data))
        self.assertEqual(storage.list_runs(self.dir)[0]["host"], "otherhost-with-longer-name")

    def test_load_run(self):
        storage.save_run(make_run(), self.dir)
        run = storage.load_run("20260925T023456Z", self.dir)
        self.assertEqual(run["id"], "20260925T023456Z")
        self.assertIn("summary", run)
        with self.assertRaises(KeyError):
            storage.load_run("20200101T000000Z", self.dir)
        for bad in ("../../etc/passwd", "latest", "20260925T023456Z.json"):
            with self.assertRaises(ValueError):
                storage.load_run(bad, self.dir)
        (self.dir / "20260101T000000Z.json").write_text("{ nope")
        with self.assertRaises(storage.StorageError):
            storage.load_run("20260101T000000Z", self.dir)

    def test_load_run_without_summary_computes_it(self):
        self.dir.mkdir(parents=True)
        (self.dir / "20260925T023456Z.json").write_text(json.dumps(make_run()))
        run = storage.load_run("20260925T023456Z", self.dir)
        self.assertEqual(run["recommendation"]["best"], "Cloudflare")
        self.assertEqual(storage.list_runs(self.dir)[0]["best"], "Cloudflare")

    def test_aggregate(self):
        storage.save_run(make_run("20260101T000000Z", "2026-01-01T00:00:00Z", fast_ms=5.0), self.dir)
        storage.save_run(make_run("20260201T000000Z", "2026-02-01T00:00:00Z", fast_ms=50.0), self.dir)
        agg = storage.aggregate(self.dir, "all")
        self.assertEqual(agg["run_ids"], ["20260201T000000Z", "20260101T000000Z"])
        self.assertEqual(agg["summary"]["overall"]["n"], 14)
        self.assertEqual(agg["summary"]["by_resolver"]["Cloudflare"]["n"], 8)
        self.assertIn("recommendation", agg)
        self.assertEqual(agg["config"]["domains"], ["a.com", "b.com"])
        one = storage.aggregate(self.dir, ["20260101T000000Z"])
        self.assertEqual(one["summary"]["overall"]["n"], 7)
        with self.assertRaises(KeyError):
            storage.aggregate(self.dir, ["20250101T000000Z"])
        with self.assertRaises(ValueError):
            storage.aggregate(self.dir, ["../x"])
        with self.assertRaises(KeyError):
            storage.aggregate(Path(self.tmp.name) / "empty", "all")

    def test_aggregate_all_skips_corrupt(self):
        storage.save_run(make_run(), self.dir)
        (self.dir / "20260101T000000Z.json").write_text("{ nope")
        with contextlib.redirect_stderr(io.StringIO()):
            agg = storage.aggregate(self.dir, "all")
        self.assertEqual(agg["run_ids"], ["20260925T023456Z"])

    def test_report_render(self):
        run = make_run()
        storage.finalize_run(run)
        text = report.render_text(run)
        self.assertIn("DNS Bench run 20260925T023456Z", text)
        self.assertIn("Cloudflare: mean=5.5 median=5.5", text)
        self.assertIn("Google: mean=20.0 median=20.0 p80=20.0 p95=20.0 p98=20.0 min=20.0 max=20.0 n=3 fail=33.3%", text)
        self.assertIn("Ranking", text)
        self.assertIn("Recommendation:", text)
        run["status"] = "cancelled"
        self.assertIn("CANCELLED", report.render_text(run))
        agg = {"run_ids": ["20260925T023456Z"], "summary": run["summary"],
               "recommendation": run["recommendation"], "config": run["config"]}
        self.assertIn("all runs combined (1 run)", report.render_text(agg))

    def test_report_all_failed(self):
        run = make_run()
        for r in run["results"]:
            r.update(status="timeout", ms=None)
        storage.finalize_run(run)
        text = report.render_text(run)
        self.assertIn("mean=- median=-", text)
        self.assertIn("No resolver returned", text)


    def test_aggregate_note_counts_runs(self):
        storage.save_run(make_run("20260101T000000Z", "2026-01-01T00:00:00Z"), self.dir)
        run = storage.load_run("20260101T000000Z", self.dir)
        self.assertTrue(any("report all" in n for n in run["recommendation"]["notes"]))
        storage.save_run(make_run("20260201T000000Z", "2026-02-01T00:00:00Z"), self.dir)
        notes = storage.aggregate(self.dir, "all")["recommendation"]["notes"]
        self.assertTrue(any("Combined from 2 runs" in n for n in notes), notes)
        self.assertFalse(any("report all" in n for n in notes))

    def test_aggregate_coverage(self):
        storage.save_run(make_run("20260101T000000Z", "2026-01-01T00:00:00Z"), self.dir)
        newer = make_run("20260201T000000Z", "2026-02-01T00:00:00Z")
        newer["results"] = [r for r in newer["results"] if r["resolver"] == "Cloudflare"]
        storage.save_run(newer, self.dir)
        cov = storage.aggregate(self.dir, "all")["coverage"]
        self.assertEqual(cov["Cloudflare"], {"runs": 2, "of": 2, "last_run": "20260201T000000Z"})
        self.assertEqual(cov["Google"], {"runs": 1, "of": 2, "last_run": "20260101T000000Z"})

    def test_aggregate_does_not_recommend_resolver_only_in_old_runs(self):
        # An old run measured Quad9 (fastest); it is disabled now and the newer
        # runs never measured it, so the combined view must not suggest it.
        old = make_run("20260101T000000Z", "2026-01-01T00:00:00Z")
        old["config"]["resolvers"].append({"name": "Quad9", "servers": ["9.9.9.9"], "enabled": True})
        old["results"] += [{"resolver": "Quad9", "server": "9.9.9.9", "domain": d, "round": 1,
                            "status": "ok", "ms": 1.0, "rcode": "NOERROR", "answers": 1,
                            "error": None, "t": 0.0} for d in ("a.com", "b.com")]
        storage.save_run(old, self.dir)
        for i in (2, 3):
            new = make_run(f"2026020{i}T000000Z", f"2026-02-0{i}T00:00:00Z")
            new["config"]["resolvers"].append({"name": "Quad9", "servers": ["9.9.9.9"], "enabled": False})
            storage.save_run(new, self.dir)
        agg = storage.aggregate(self.dir, "all")
        rec = agg["recommendation"]
        self.assertEqual(rec["ranking"][0]["resolver"], "Quad9")  # still ranked
        self.assertEqual(rec["best"], "Cloudflare")
        self.assertEqual(rec["backup"], "Google")
        self.assertNotIn("9.9.9.9", rec["suggested_servers"])
        self.assertNotIn("Quad9", rec["tied_with"])
        self.assertTrue(any(n.startswith("Quad9 was measured in only 1 of 3 combined runs")
                            and "not recommended" in n for n in rec["notes"]), rec["notes"])
        self.assertIn("of the current resolvers", rec["summary"])
        # enabled again in the live config: it can be recommended
        agg = storage.aggregate(self.dir, "all", current=["Cloudflare", "Google", "Quad9"])
        self.assertEqual(agg["recommendation"]["best"], "Quad9")
        self.assertTrue(any("Quad9 was measured in only 1 of 3" in n and "less comparable" in n
                            for n in agg["recommendation"]["notes"]))
        # the newest run measured it: it can be recommended whatever the live config says
        agg = storage.aggregate(self.dir, ["20260101T000000Z"], current=["Cloudflare"])
        self.assertEqual(agg["recommendation"]["best"], "Quad9")

    def test_report_per_server_columns_align(self):
        run = make_run()
        run["config"]["resolvers"] = [
            {"name": "CleanBrowsing", "servers": ["185.228.168.9"], "enabled": True},
            {"name": "Cloudflare", "servers": ["2606:4700:4700::1111", "1.1.1.1"], "enabled": True}]
        run["results"] = [
            {"resolver": res, "server": srv, "domain": "a.com", "round": 1, "status": "ok", "ms": ms,
             "rcode": "NOERROR", "answers": 1, "error": None, "t": 0.0}
            for res, srv, ms in (("CleanBrowsing", "185.228.168.9", 9.2),
                                 ("Cloudflare", "2606:4700:4700::1111", 5.2), ("Cloudflare", "1.1.1.1", 5.7))]
        storage.finalize_run(run)
        lines = report.render_text(run).splitlines()
        start = lines.index("Per server (ms):")
        block = [ln for ln in lines[start + 1:start + 6] if ln.strip()]
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
                r["resolver"] = "Goo\x1b[2Jgle\u202e"
        run["config"]["resolvers"][1]["name"] = "Goo\x1b[2Jgle\u202e"
        storage.finalize_run(run)
        text = report.render_text(run)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\u202e", text)
        self.assertIn("Goo\\x1b[2Jgle\\u202e: mean=", text)

    def test_report_shows_retries(self):
        run = make_run()
        run["results"][0]["attempts"] = 2
        storage.finalize_run(run)
        text = report.render_text(run)
        self.assertIn("Retried", text)
        self.assertIn("retried=1", text)


if __name__ == "__main__":
    unittest.main()
