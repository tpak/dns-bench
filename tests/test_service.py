from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from samples import make_run

from dnsbench import config as C
from dnsbench import service, storage, sysdns
from dnsbench.resolver import QueryResult

HOME_NET = sysdns.Detected(["192.0.2.53"], "a test")


def small_config(domains: int = 3) -> dict:
    cfg = C.default_config()
    cfg["resolvers"] = [
        {"name": "Fast", "servers": ["192.0.2.1"], "enabled": True},
        {"name": "Slow", "servers": ["192.0.2.2"], "enabled": True},
        {"name": "Off", "servers": ["192.0.2.3"], "enabled": False},
    ]
    cfg["domains"] = [f"d{i}.example" for i in range(domains)]
    cfg["settings"]["per_server_interval_ms"] = 50
    return cfg


def fast_query(server, domain, record_type="A", timeout_s=1.0, tries=1):
    return QueryResult("ok", ms=4.0, rcode="NOERROR", answers=1, attempts=1)


class EstimateTest(unittest.TestCase):
    def test_estimate(self):
        e = service.estimate(C.default_config())
        self.assertEqual((e["servers"], e["queries"]), (6, 360))
        self.assertEqual((e["max_qps_per_server"], e["max_qps_total"]), (4.0, 24.0))
        self.assertTrue(14 <= e["est_seconds"] <= 20, e)
        # More servers: all measured at once, so no extra time (the old estimate added a batch per 8)
        many = C.default_config()
        many["resolvers"] = [
            {"name": f"R{i}", "servers": [f"192.0.2.{2 * i + 1}", f"192.0.2.{2 * i + 2}"], "enabled": True}
            for i in range(6)
        ]
        e12 = service.estimate(many)
        self.assertEqual(e12["servers"], 12)
        self.assertEqual(e12["est_seconds"], e["est_seconds"])
        self.assertEqual(e12["max_qps_total"], 48.0)

    def test_estimate_of_a_draft_never_raises(self):
        # Settings shows an estimate while the user types: junk counts as the default.
        draft = {
            "resolvers": [
                {"name": "A", "servers": "192.0.2.1, 192.0.2.2"},
                "junk",
                {"name": "B", "enabled": False},
            ],
            "domains": "a.com b.com\nc.com",
            "settings": {"rounds": "", "per_server_interval_ms": "10", "timeout_ms": None},
        }
        e = service.estimate(draft)
        self.assertEqual((e["resolvers"], e["servers"], e["domains"], e["rounds"]), (1, 2, 3, 1))
        self.assertEqual(e["queries"], 6)
        self.assertEqual(e["max_qps_per_server"], 20.0)  # 10 ms is below the 50 ms floor
        for junk in (None, [], "x", {"resolvers": 5, "domains": {}, "settings": []}):
            with self.subTest(junk=junk):
                self.assertEqual(service.estimate(junk)["queries"], 0)

    def test_estimate_clamps_out_of_bounds_values(self):
        # A hand-edited config can hold any integer; 10**400 overflowed float arithmetic (GET /api/config
        # answered 500 instead of showing the config and its errors).
        for key, (_lo, hi) in C.SETTING_BOUNDS.items():
            with self.subTest(key=key):
                c = C.default_config()
                c["settings"][key] = 10**400
                self.assertEqual(service.estimate(c)["rounds"], hi if key == "rounds" else 1)
        c = C.default_config()
        c["settings"]["rounds"] = 12  # typing past the limit: the estimate stays at the limit
        self.assertEqual(service.estimate(c)["rounds"], 10)

    def test_estimate_rounds_override_and_worst_case(self):
        c = C.default_config()
        c["settings"].update(tries=2, timeout_ms=1000, per_server_interval_ms=250)
        e = service.estimate(c, rounds=3)
        self.assertEqual((e["rounds"], e["queries_per_server"]), (3, 180))
        self.assertEqual(e["est_seconds"], round(180 * 0.25 * 1.05, 1))
        self.assertEqual(e["worst_seconds"], 180 * 2 * 1.0)  # every attempt times out after 1 s


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg_path = root / "config.json"
        self.runs_dir = root / "runs"
        C.save_config(small_config(), self.cfg_path)
        self.service = service.BenchmarkService(
            self.cfg_path, self.runs_dir, query_fn=fast_query, detect_fn=lambda: HOME_NET
        )

    def tearDown(self):
        self.tmp.cleanup()


class PrepareTest(ServiceTestBase):
    def test_plan(self):
        plan = self.service.prepare(service.Overrides(rounds=2))
        self.assertEqual(plan.config["settings"]["rounds"], 2)
        self.assertEqual(plan.total, 2 * 3 * 2)  # 2 enabled servers x 3 domains x 2 rounds
        self.assertEqual(plan.estimate["queries"], plan.total)
        self.assertIsNone(plan.created)
        self.assertEqual(C.load_config(self.cfg_path)["settings"]["rounds"], 1)  # overrides aren't saved

    def test_overrides(self):
        plan = self.service.prepare(
            service.Overrides(resolvers=[" off ", "FAST"], interval_ms=100, timeout_ms=300)
        )
        self.assertEqual([r["name"] for r in C.enabled_resolvers(plan.config)], ["Fast", "Off"])
        self.assertEqual(
            (plan.config["settings"]["per_server_interval_ms"], plan.config["settings"]["timeout_ms"]),
            (100, 300),
        )
        with self.assertRaises(service.InvalidRun) as cm:
            self.service.prepare(service.Overrides(resolvers=["Nope"]))
        self.assertEqual(
            cm.exception.messages, ["unknown resolver(s): Nope (configured: Fast, Slow, Off)"]
        )  # no file: the config itself is fine
        cfg = small_config(domains=500)
        cfg["resolvers"] = [
            {"name": f"R{i}", "servers": [f"192.0.2.{4 * i + j}" for j in range(1, 5)], "enabled": True}
            for i in range(3)
        ]
        C.save_config(cfg, self.cfg_path)  # 12 servers x 500 domains = 6,000 queries a round
        with self.assertRaises(service.InvalidRun) as cm:
            self.service.prepare(service.Overrides(rounds=10))
        self.assertEqual([e.code for e in cm.exception.errors], ["too_many_queries"])

    def test_a_first_saved_run_creates_the_config(self):
        self.cfg_path.unlink()
        with mock.patch.dict(C.DEFAULT_CONFIG, {"domains": ["a.example"]}):
            plan = self.service.prepare()
            self.assertEqual(plan.created.resolver["servers"], ["192.0.2.53"])
            self.assertEqual(C.load_config(self.cfg_path), C.initial_config(lambda: HOME_NET)[0])

    def test_an_unsaved_run_writes_nothing(self):
        self.cfg_path.unlink()
        plan = self.service.prepare(save=False)
        self.assertIsNone(plan.created)
        self.assertFalse(self.cfg_path.exists())
        self.assertFalse(self.runs_dir.exists())

    def test_config_and_runs_dir_problems(self):
        self.cfg_path.write_text("{ nope")
        with self.assertRaises(C.ConfigError) as cm:
            self.service.prepare()
        self.assertNotIsInstance(cm.exception, service.InvalidRun)
        C.save_config(small_config(), self.cfg_path)
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("x")
        self.service.repo = storage.RunRepository(blocker / "runs")
        with self.assertRaises(service.RunsDirUnwritable) as cm2:
            self.service.prepare()
        self.assertEqual(cm2.exception.reason, "Not a directory")
        self.service.prepare(save=False)  # fine without saving


class ExecuteAndPersistTest(ServiceTestBase):
    def test_a_run_is_measured_once_per_planned_query_and_saved(self):
        plan = self.service.prepare()
        run = self.service.execute(plan)
        self.assertEqual((run["schema"], run["kind"], len(run["results"])), (1, "run", plan.total))
        self.assertTrue(all(r["truncated"] is False for r in run["results"]))
        saved = self.service.persist(run)
        self.assertEqual(saved, service.SaveResult(self.runs_dir / f"{run['id']}.json"))
        on_disk = json.loads(saved.path.read_text())
        self.assertEqual(on_disk["analysis_version"], run["analysis_version"])
        self.assertIn("recommendation", on_disk)  # a snapshot for other tools; never read back
        self.assertIn("Recommendation:", (self.runs_dir / f"{run['id']}.txt").read_text())

    def test_persist_rescues_what_it_cannot_save(self):
        rescue_dir = Path(self.tmp.name) / "rescue"
        rescue_dir.mkdir()
        run = make_run()
        with (
            mock.patch.object(self.service.repo, "save", side_effect=OSError(13, "Permission denied")),
            mock.patch.object(tempfile, "tempdir", str(rescue_dir)),
        ):
            result = self.service.persist(run)
        self.assertIsNone(result.path)
        self.assertEqual(result.error, f"could not save run to {self.runs_dir}: Permission denied")
        self.assertEqual(result.rescued.parent, rescue_dir)
        self.assertEqual(json.loads(result.rescued.read_text(encoding="utf-8"))["results"], run["results"])
        self.assertIn("recommendation", run)  # analysed, so the caller can still print the report

    def test_persist_json_saved_but_report_failed(self):
        with mock.patch.object(storage.os, "replace", side_effect=OSError(28, "No space left on device")):
            result = self.service.persist(make_run())
        json_path = self.runs_dir / "20260925T023456Z.json"
        self.assertEqual(result.path, json_path)
        self.assertTrue(json_path.is_file())
        self.assertIn("text report could not be written: No space left on device", result.error)
        self.assertIsNone(result.rescued)  # the record itself is safe in runs/


class JobManagerTest(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.jobs = service.JobManager(self.service)

    def tearDown(self):
        self.jobs.stop(wait_s=5)
        super().tearDown()

    def wait_idle(self, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self.jobs.status()
            if not st["running"]:
                return st
            time.sleep(0.02)
        self.fail("job did not finish")

    def test_a_job_runs_and_is_saved(self):
        total = self.jobs.start(service.Overrides(rounds=2))
        self.assertEqual(total, 12)
        st = self.wait_idle()
        self.assertEqual(
            (st["done"], st["total"], st["last_status"], st["error"]), (12, 12, "complete", None)
        )
        self.assertTrue(self.service.repo.exists(st["last_run_id"]))

    def test_one_job_at_a_time_and_prepare_runs_outside_the_lock(self):
        release = threading.Event()
        real_prepare = self.service.prepare

        def slow_prepare(*args, **kwargs):
            release.wait(5)
            return real_prepare(*args, **kwargs)

        with mock.patch.object(self.service, "prepare", side_effect=slow_prepare):
            starter = threading.Thread(target=self.jobs.start)
            starter.start()
            time.sleep(0.1)
            # While the first start is still checking the config (disk IO), the lock is free: status
            # answers at once, and a second start is refused instead of waiting.
            t0 = time.monotonic()
            self.assertFalse(self.jobs.status()["running"])
            with self.assertRaises(service.JobBusy):
                self.jobs.start()
            self.assertLess(time.monotonic() - t0, 1.0)
            release.set()
            starter.join(5)
        self.wait_idle()

    def test_stop_waits_for_a_job_that_is_still_starting(self):
        # A shutdown during prepare() used to find nothing running and let the job start afterwards.
        release = threading.Event()
        real_prepare = self.service.prepare

        def slow_prepare(*args, **kwargs):
            release.wait(5)
            return real_prepare(*args, **kwargs)

        with mock.patch.object(self.service, "prepare", side_effect=slow_prepare):
            starter = threading.Thread(target=self.jobs.start)
            starter.start()
            time.sleep(0.1)
            threading.Timer(0.2, release.set).start()
            self.assertTrue(self.jobs.stop())
            starter.join(5)
        st = self.jobs.status()
        self.assertFalse(st["running"])
        self.assertIn(st["last_status"], ("cancelled", "complete"))  # it ran, and stop() waited for it

    def test_a_failed_start_leaves_it_startable(self):
        self.cfg_path.write_text("{ nope")
        with self.assertRaises(C.ConfigError):
            self.jobs.start()
        C.save_config(small_config(), self.cfg_path)
        self.jobs.start()
        self.wait_idle()

    def test_stop_waits_for_an_in_flight_query(self):
        # A query can't be interrupted, so the shutdown wait must cover a whole
        # timeout (not a fixed 5 s) or the partial run is lost.
        thread = mock.Mock()
        thread.is_alive.return_value = False
        state = self.jobs._state
        state.running, state.cancel_event, state.thread, state.timeout_s = (
            True,
            threading.Event(),
            thread,
            8.0,
        )
        self.assertTrue(self.jobs.stop())
        thread.join.assert_called_once_with(11.0)
        self.assertTrue(state.cancel_event.is_set())
        state.cancel_event.clear()
        state.timeout_s = 1.0
        thread.join.reset_mock()
        self.jobs.stop()
        thread.join.assert_called_once_with(5.0)  # never less than before
        state.running = False

    def test_stop_saves_the_partial_run(self):
        cfg = small_config(domains=20)
        cfg["settings"]["timeout_ms"] = 400
        C.save_config(cfg, self.cfg_path)

        def blocking(server, domain, record_type="A", timeout_s=1.0, tries=1):
            if server == "192.0.2.2":
                time.sleep(timeout_s)  # unresponsive server: blocks the full timeout
                return QueryResult("timeout", error="timeout", attempts=1)
            return QueryResult("ok", ms=4.0, rcode="NOERROR", answers=1, attempts=1)

        self.service.query_fn = blocking
        self.jobs.start()
        self.assertEqual(self.jobs._state.timeout_s, 0.4)
        time.sleep(0.15)
        self.assertTrue(self.jobs.stop())
        self.assertFalse(self.jobs.running)
        rows = self.service.analysis.list_runs()
        self.assertEqual([r["status"] for r in rows], ["cancelled"])


if __name__ == "__main__":
    unittest.main()
