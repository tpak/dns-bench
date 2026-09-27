from __future__ import annotations

import itertools
import random
import threading
import time
import unittest
from collections import Counter, defaultdict

from dnsbench import __version__, runner
from dnsbench.resolver import QueryResult

# Physical timestamps are taken inside the fake query_fn, a few microseconds
# after the scheduler's own start timestamp; allow that much slack for them.
# The scheduler's own timestamps are checked with NO slack.
PHYS_TOL_S = 0.002
# How much later than scheduled a worker may actually start. Only a stall should
# exceed it: a loaded machine wakes sleeping threads late (CI's macOS runners have
# measured 130 ms late), so precise spacing is checked on the requested sleeps.
STALL_TOL_S = 0.5


def make_config(
    resolvers=2,
    servers_per=2,
    domains=6,
    interval_ms=50,
    rounds=1,
    shuffle=True,
    parallel=8,
    tries=1,
    timeout_ms=200,
):
    return {
        "resolvers": [
            {
                "name": f"R{i}",
                "servers": [f"10.0.{i}.{j}" for j in range(1, servers_per + 1)],
                "enabled": True,
            }
            for i in range(resolvers)
        ],
        "domains": [f"d{k}.example" for k in range(domains)],
        "settings": {
            "per_server_interval_ms": interval_ms,
            "timeout_ms": timeout_ms,
            "tries": tries,
            "rounds": rounds,
            "max_parallel_servers": parallel,
            "slow_threshold_ms": 200,
            "record_type": "A",
            "shuffle": shuffle,
        },
    }


class RecordingClock:
    """time.monotonic, remembering the last value each thread read. The worker's
    last clock() read before calling query_fn is exactly its scheduled start.

    ``sleep`` (pass it to run_benchmark with the clock) records what the runner
    *asked* for: the latest wake-up any worker requested, measured from that
    worker's last scheduled start. Unlike measured gaps, that doesn't depend on
    how late a loaded machine wakes the thread (CI's macOS runners: 100+ ms)."""

    def __init__(self):
        self.local = threading.local()
        self.lock = threading.Lock()  # guards max_wait_after_query
        self.max_wait_after_query = 0.0

    def __call__(self):
        now = time.monotonic()
        self.local.last = now
        return now

    def last(self):
        return getattr(self.local, "last", None)

    def sleep(self, seconds):
        # The runner reads clock() just before each sleep, so last() is when it asked.
        sched = getattr(self.local, "query_sched", None)
        if sched is not None:
            with self.lock:
                self.max_wait_after_query = max(self.max_wait_after_query, self.last() + seconds - sched)
        time.sleep(seconds)


class FakeDNS:
    """Thread-safe fake query_fn recording (server, domain, sched_start, start, end)."""

    def __init__(self, clock=None, latency=0.005, outcome=None, latency_fn=None):
        self.clock = clock
        self.latency = latency
        self.latency_fn = latency_fn
        self.outcome = outcome or (lambda server, domain, n: "ok")
        self.lock = threading.Lock()
        self.events = []
        self.inflight = Counter()
        self.max_inflight = Counter()
        self.calls = Counter()

    def __call__(self, server, domain, record_type="A", timeout_s=1.0, tries=1):
        start = time.monotonic()
        sched = self.clock.last() if self.clock else None
        if self.clock:
            self.clock.local.query_sched = sched  # the start RecordingClock.sleep measures from
        with self.lock:
            self.inflight[server] += 1
            self.max_inflight[server] = max(self.max_inflight[server], self.inflight[server])
            n = self.calls[(server, domain)]
            self.calls[(server, domain)] += 1
        assert tries == 1, "runner must do retries itself, through the rate limiter"
        kind = self.outcome(server, domain, n)
        time.sleep(self.latency_fn(domain) if self.latency_fn else self.latency)
        with self.lock:
            self.inflight[server] -= 1
        end = time.monotonic()
        with self.lock:
            self.events.append((server, domain, sched, start, end))
        if kind == "ok":
            return QueryResult("ok", ms=self.latency * 1000 + 0.12345, rcode="NOERROR", answers=1, attempts=1)
        if kind == "timeout":
            return QueryResult("timeout", error="timeout", attempts=1)
        if kind == "servfail":
            return QueryResult("error", ms=3.0, rcode="SERVFAIL", error="SERVFAIL", attempts=1)
        raise RuntimeError("boom")

    def by_server(self):
        out = defaultdict(list)
        for e in sorted(self.events, key=lambda e: e[3]):
            out[e[0]].append(e)
        return out


class RateLimitTest(unittest.TestCase):
    def check_spacing(self, fake, interval_s):
        for server, evs in fake.by_server().items():
            for prev, cur in itertools.pairwise(evs):
                # never more than one in flight to the same server
                self.assertGreaterEqual(cur[3], prev[4], f"overlap on {server}")
                # scheduler start-to-start spacing: exact, no tolerance
                self.assertGreaterEqual(
                    cur[2] - prev[2], interval_s, f"scheduled starts too close on {server}"
                )
                # physical spacing seen by the 'server'
                self.assertGreaterEqual(
                    cur[3] - prev[3], interval_s - PHYS_TOL_S, f"queries too close on {server}"
                )
                # and no stalls: the measured gap includes however late the OS woke the
                # worker, so this only catches gross delays; precision is checked below
                prev_took = prev[4] - prev[2]
                self.assertLess(cur[2] - prev[2], max(interval_s * 1.1, prev_took) + STALL_TOL_S)
            self.assertEqual(fake.max_inflight[server], 1)
        # not needlessly spaced either: no worker ever asked to sleep past its last
        # scheduled start + the interval + 10 % jitter (exact: no tolerance for load)
        self.assertLessEqual(fake.clock.max_wait_after_query, interval_s * 1.1 + 1e-9)

    def test_per_server_limit_and_cross_server_concurrency(self):
        interval_s = 0.05
        cfg = make_config(resolvers=2, servers_per=2, domains=6, interval_ms=50, rounds=2)
        clock = RecordingClock()
        fake = FakeDNS(clock=clock, latency=0.005)
        t0 = time.monotonic()
        run = runner.run_benchmark(cfg, query_fn=fake, clock=clock, sleep=clock.sleep)
        wall = time.monotonic() - t0

        self.assertEqual(run["status"], "complete")
        self.assertEqual(len(run["results"]), 4 * 6 * 2)
        self.check_spacing(fake, interval_s)

        # every (server, domain, round) exactly once
        keys = Counter((r["server"], r["domain"], r["round"]) for r in run["results"])
        self.assertEqual(len(keys), 48)
        self.assertEqual(set(keys.values()), {1})

        # servers really run concurrently: all per-server spans overlap and the
        # wall time is about one server's time, not the sum of all four.
        spans = {s: (evs[0][3], evs[-1][4]) for s, evs in fake.by_server().items()}
        self.assertEqual(len(spans), 4)
        latest_start = max(a for a, _ in spans.values())
        earliest_end = min(b for _, b in spans.values())
        self.assertLess(latest_start, earliest_end, "server workers did not overlap in time")
        per_server = max(b - a for a, b in spans.values())
        total_if_sequential = sum(b - a for a, b in spans.values())
        self.assertGreaterEqual(per_server, 11 * interval_s)  # 12 queries -> 11 gaps
        self.assertLess(wall, per_server + 0.25)
        self.assertLess(wall, 0.5 * total_if_sequential)

        # the runner's own "t" (rounded to ms) tells the same story
        by_srv = defaultdict(list)
        for r in run["results"]:
            by_srv[r["server"]].append(r["t"])
        for ts in by_srv.values():
            ts.sort()
            for a, b in itertools.pairwise(ts):
                self.assertGreaterEqual(b - a, interval_s - 0.0011)

    def test_slow_queries_and_timeouts_do_not_shift_to_bursts(self):
        # Queries slower than the interval (e.g. timeouts): the next one may start
        # right after, but never overlaps and is never closer than the interval.
        cfg = make_config(resolvers=1, servers_per=2, domains=5, interval_ms=50)
        clock = RecordingClock()
        slow = ("d1.example", "d3.example")
        fake = FakeDNS(
            clock=clock,
            latency_fn=lambda d: 0.08 if d in slow else 0.0,
            outcome=lambda s, d, n: "timeout" if d in slow else "ok",
        )
        run = runner.run_benchmark(cfg, query_fn=fake, clock=clock, sleep=clock.sleep)
        self.check_spacing(fake, 0.05)
        statuses = Counter(r["status"] for r in run["results"])
        self.assertEqual(statuses, {"ok": 6, "timeout": 4})
        # after an 80 ms timeout the next query goes out promptly (no extra wait)
        for evs in fake.by_server().values():
            for prev, cur in itertools.pairwise(evs):
                if prev[1] in slow:
                    self.assertLess(cur[2] - prev[4], 0.03)

    def test_retries_go_through_rate_limiter(self):
        cfg = make_config(resolvers=1, servers_per=1, domains=3, interval_ms=50, tries=3, shuffle=False)
        clock = RecordingClock()
        # d0 always times out (instantly), d1 times out once then answers
        fake = FakeDNS(
            clock=clock,
            latency=0.0,
            outcome=lambda s, d, n: (
                "timeout" if d == "d0.example" or (d == "d1.example" and n == 0) else "ok"
            ),
        )
        run = runner.run_benchmark(cfg, query_fn=fake, clock=clock, sleep=clock.sleep)
        self.assertEqual(fake.calls[("10.0.0.1", "d0.example")], 3)
        self.assertEqual(fake.calls[("10.0.0.1", "d1.example")], 2)
        self.assertEqual(fake.calls[("10.0.0.1", "d2.example")], 1)
        self.assertEqual(len(fake.events), 6)
        self.check_spacing(fake, 0.05)  # instant timeouts did not cause bursts
        res = {r["domain"]: r["status"] for r in run["results"]}
        self.assertEqual(res, {"d0.example": "timeout", "d1.example": "ok", "d2.example": "ok"})
        self.assertEqual(len(run["results"]), 3)
        # the lost attempts are kept in the output, not silently dropped
        attempts = {r["domain"]: r["attempts"] for r in run["results"]}
        self.assertEqual(attempts, {"d0.example": 3, "d1.example": 2, "d2.example": 1})

    def test_retry_after_first_timeout_is_recorded(self):
        cfg = make_config(resolvers=1, servers_per=1, domains=1, interval_ms=50, tries=2)
        fake = FakeDNS(latency=0.0, outcome=lambda s, d, n: "timeout" if n == 0 else "ok")
        run = runner.run_benchmark(cfg, query_fn=fake)
        (row,) = run["results"]
        self.assertEqual((row["status"], row["attempts"]), ("ok", 2))
        self.assertEqual(sum(fake.calls.values()), 2)

    def test_interval_floor_enforced(self):
        cfg = make_config(resolvers=1, servers_per=1, domains=4, interval_ms=5)  # below 50 ms floor
        clock = RecordingClock()
        fake = FakeDNS(clock=clock, latency=0.0)
        run = runner.run_benchmark(cfg, query_fn=fake, clock=clock, sleep=clock.sleep)
        self.check_spacing(fake, 0.05)
        self.assertEqual(run["config"]["settings"]["per_server_interval_ms"], 50)

    def test_max_parallel_servers_respected(self):
        cfg = make_config(resolvers=2, servers_per=2, domains=3, interval_ms=50, parallel=2)
        lock = threading.Lock()
        active_servers = set()
        peak = [0]
        state = defaultdict(int)
        remaining = Counter({f"10.0.{i}.{j}": 3 for i in range(2) for j in (1, 2)})

        def qfn(server, domain, **kw):
            # a server is "active" from its first query to its last
            with lock:
                active_servers.add(server)
                peak[0] = max(peak[0], len(active_servers))
                state[server] += 1
            time.sleep(0.002)
            with lock:
                remaining[server] -= 1
                if remaining[server] == 0:
                    active_servers.discard(server)
            return QueryResult("ok", ms=2.0, rcode="NOERROR", answers=1)

        t0 = time.monotonic()
        run = runner.run_benchmark(cfg, query_fn=qfn)
        wall = time.monotonic() - t0
        self.assertEqual(len(run["results"]), 12)
        self.assertLessEqual(peak[0], 2)
        self.assertGreaterEqual(wall, 2 * 2 * 0.05)  # two batches of 3 queries each

    def test_single_worker_is_sequential(self):
        cfg = make_config(resolvers=1, servers_per=3, domains=3, interval_ms=50, parallel=1)
        fake = FakeDNS(latency=0.002)
        runner.run_benchmark(cfg, query_fn=fake)
        spans = sorted((evs[0][3], evs[-1][4]) for evs in fake.by_server().values())
        for (_a1, b1), (a2, _b2) in itertools.pairwise(spans):
            self.assertGreaterEqual(a2, b1)


class RunnerBehaviourTest(unittest.TestCase):
    def test_record_shape(self):
        cfg = make_config(resolvers=1, servers_per=1, domains=2, interval_ms=50)
        run = runner.run_benchmark(cfg, query_fn=FakeDNS(latency=0.0))
        self.assertRegex(run["id"], r"^\d{8}T\d{6}Z$")
        self.assertRegex(run["started_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertRegex(run["finished_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(run["version"], __version__)
        self.assertEqual(run["status"], "complete")
        self.assertIsInstance(run["duration_s"], float)
        self.assertTrue(run["host"])
        r = run["results"][0]
        self.assertEqual(
            set(r),
            {
                "resolver",
                "server",
                "domain",
                "round",
                "status",
                "ms",
                "rcode",
                "answers",
                "error",
                "t",
                "attempts",
            },
        )
        self.assertEqual(r["attempts"], 1)
        self.assertEqual(r["ms"], 0.123)  # rounded to 3 dp
        self.assertEqual(r["resolver"], "R0")
        self.assertGreaterEqual(r["t"], 0)
        ts = [x["t"] for x in run["results"]]
        self.assertEqual(ts, sorted(ts))

    def test_config_snapshot_is_a_copy(self):
        cfg = make_config(resolvers=1, servers_per=1, domains=1)
        run = runner.run_benchmark(cfg, query_fn=FakeDNS(latency=0.0))
        cfg["domains"].append("later.example")
        self.assertEqual(run["config"]["domains"], ["d0.example"])

    def test_disabled_and_duplicate_servers(self):
        cfg = make_config(resolvers=3, servers_per=1, domains=2)
        cfg["resolvers"][1]["enabled"] = False
        cfg["resolvers"][2]["servers"] = ["10.0.0.1"]  # duplicate of R0's server
        fake = FakeDNS(latency=0.0)
        run = runner.run_benchmark(cfg, query_fn=fake)
        self.assertEqual({r["resolver"] for r in run["results"]}, {"R0"})
        self.assertEqual(len(run["results"]), 2)
        self.assertEqual(max(fake.max_inflight.values()), 1)

    def test_address_aliases_share_one_worker(self):
        # unvalidated config: every spelling of one host must map to one worker
        cfg = make_config(resolvers=3, servers_per=1, domains=2)
        cfg["resolvers"][0]["servers"] = ["1.1.1.1", "::1"]
        cfg["resolvers"][1]["servers"] = ["::ffff:1.1.1.1", "::1%1"]
        cfg["resolvers"][2]["servers"] = ["::ffff:1.1.1.1%3", "0:0:0:0:0:0:0:1"]
        jobs = runner.build_jobs(cfg)
        self.assertEqual([(j["resolver"], j["server"]) for j in jobs], [("R0", "1.1.1.1"), ("R0", "::1")])

    def test_shuffle_independent_per_server(self):
        cfg = make_config(resolvers=1, servers_per=2, domains=20, rounds=2)
        jobs = runner.build_jobs(cfg, random.Random(7))
        a, b = jobs[0]["items"], jobs[1]["items"]
        expected = sorted((d, r) for r in (1, 2) for d in cfg["domains"])
        self.assertEqual(sorted(a), expected)
        self.assertEqual(sorted(b), expected)
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, expected)

    def test_no_shuffle_keeps_order(self):
        cfg = make_config(resolvers=1, servers_per=1, domains=3, rounds=2, shuffle=False)
        jobs = runner.build_jobs(cfg)
        self.assertEqual(
            jobs[0]["items"],
            [
                ("d0.example", 1),
                ("d1.example", 1),
                ("d2.example", 1),
                ("d0.example", 2),
                ("d1.example", 2),
                ("d2.example", 2),
            ],
        )

    def test_progress_events(self):
        cfg = make_config(resolvers=2, servers_per=1, domains=3)
        events = []
        run = runner.run_benchmark(cfg, query_fn=FakeDNS(latency=0.0), progress=events.append)
        self.assertEqual([e["done"] for e in events], list(range(1, 7)))
        self.assertTrue(all(e["type"] == "result" and e["total"] == 6 for e in events))
        self.assertEqual(len(run["results"]), 6)

    def test_progress_exception_does_not_break_run(self):
        cfg = make_config(resolvers=1, servers_per=1, domains=2)

        def bad(event):
            raise ValueError("ui broke")

        run = runner.run_benchmark(cfg, query_fn=FakeDNS(latency=0.0), progress=bad)
        self.assertEqual(len(run["results"]), 2)

    def test_error_statuses_and_exceptions(self):
        cfg = make_config(resolvers=1, servers_per=1, domains=3, shuffle=False)
        outcomes = {"d0.example": "servfail", "d1.example": "raise", "d2.example": "ok"}
        fake = FakeDNS(latency=0.0, outcome=lambda s, d, n: outcomes[d])
        run = runner.run_benchmark(cfg, query_fn=fake)
        rows = {r["domain"]: r for r in run["results"]}
        self.assertEqual(rows["d0.example"]["status"], "error")
        self.assertEqual(rows["d0.example"]["rcode"], "SERVFAIL")
        self.assertEqual(rows["d0.example"]["ms"], 3.0)
        self.assertEqual(rows["d1.example"]["status"], "error")
        self.assertIn("boom", rows["d1.example"]["error"])
        self.assertEqual(rows["d2.example"]["status"], "ok")

    def test_dict_results_accepted(self):
        cfg = make_config(resolvers=1, servers_per=1, domains=1)
        run = runner.run_benchmark(
            cfg, query_fn=lambda s, d, **kw: {"status": "ok", "ms": 1.5, "rcode": "NOERROR", "answers": 2}
        )
        self.assertEqual(run["results"][0]["ms"], 1.5)
        self.assertEqual(run["results"][0]["answers"], 2)

    def test_cancel(self):
        cfg = make_config(resolvers=2, servers_per=2, domains=40, interval_ms=100)
        cancel = threading.Event()

        def progress(e):
            if e["done"] >= 6:
                cancel.set()

        t0 = time.monotonic()
        run = runner.run_benchmark(cfg, query_fn=FakeDNS(latency=0.0), progress=progress, cancel_event=cancel)
        took = time.monotonic() - t0
        self.assertEqual(run["status"], "cancelled")
        self.assertGreaterEqual(len(run["results"]), 6)
        self.assertLess(len(run["results"]), 160)
        self.assertLess(took, 1.5)  # full run would take ~4 s

    def test_cancel_before_start(self):
        cancel = threading.Event()
        cancel.set()
        run = runner.run_benchmark(make_config(), query_fn=FakeDNS(latency=0.0), cancel_event=cancel)
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(run["results"], [])

    def test_late_cancel_after_completion_is_complete(self):
        cfg = make_config(resolvers=1, servers_per=1, domains=2)
        cancel = threading.Event()

        def progress(e):
            if e["done"] == e["total"]:
                cancel.set()

        run = runner.run_benchmark(cfg, query_fn=FakeDNS(latency=0.0), progress=progress, cancel_event=cancel)
        self.assertEqual(run["status"], "complete")

    def test_default_expected_wall_time(self):
        """Default settings: 60 domains x 250 ms per server => ~15-17 s, not 6.5 min."""
        from dnsbench.config import default_config, estimate

        e = estimate(default_config())
        self.assertLess(e["est_seconds"], 20)
        self.assertEqual(e["max_qps_per_server"], 4.0)


if __name__ == "__main__":
    unittest.main()
