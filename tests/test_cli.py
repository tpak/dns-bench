from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from dnsbench import __version__, cli, paths, resolver, storage, sysdns
from dnsbench import config as C
from dnsbench.resolver import QueryResult

ROOT = Path(__file__).resolve().parents[1]
HOME_NET = sysdns.Detected(["192.0.2.53"], "a test")


def env_without_home() -> dict[str, str]:
    """The environment for a subprocess that must use the checkout, whatever the caller's DNSBENCH_HOME."""
    return {k: v for k, v in os.environ.items() if k != paths.HOME_ENV}


def small_config():
    cfg = C.default_config()
    cfg["resolvers"] = [
        {"name": "Fast", "servers": ["192.0.2.1"], "enabled": True},
        {"name": "Slow", "servers": ["192.0.2.2"], "enabled": True},
        {"name": "Spare", "servers": ["192.0.2.3"], "enabled": False},
    ]
    cfg["domains"] = ["a.example", "b.example", "c.example"]
    cfg["settings"]["per_server_interval_ms"] = 50
    cfg["settings"]["slow_threshold_ms"] = 30
    return cfg


class FakeQuery:
    def __init__(self, on_call=None):
        self.lock = threading.Lock()
        self.calls = 0
        self.on_call = on_call

    def __call__(self, server, domain, record_type="A", timeout_s=1.0, tries=1):
        with self.lock:
            self.calls += 1
            n = self.calls
        if self.on_call:
            self.on_call(n)
        if server == "192.0.2.2" and domain == "c.example":
            return QueryResult("timeout", error="timeout", attempts=1)
        return QueryResult(
            "ok", ms=40.0 if server == "192.0.2.2" else 3.0, rcode="NOERROR", answers=1, attempts=1
        )


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = root / "config.json"
        self.runs = root / "runs"
        C.save_config(small_config(), self.cfg)
        self.fake = FakeQuery()
        detect = mock.patch.object(sysdns, "detect", return_value=HOME_NET)  # never this computer's own
        detect.start()
        self.addCleanup(detect.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(resolver, "query", self.fake),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cli.main([*args, "--config", str(self.cfg), "--runs-dir", str(self.runs)])
        return code, out.getvalue(), err.getvalue()

    def test_run_saves_and_reports(self):
        code, out, err = self.cli("run")
        self.assertEqual(code, 0, err)
        self.assertIn("Fast: mean=3.0", out)
        self.assertIn("Recommendation: Use Fast", out)
        self.assertIn("Saved:", err)
        self.assertIn("[slow] Slow 192.0.2.2", err)
        self.assertIn("[fail] Slow 192.0.2.2 c.example -> timeout", err)
        self.assertIn("progress:", err)  # not a TTY -> periodic lines
        rows = storage.list_runs(self.runs)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.fake.calls, 6)

        code, out, _ = self.cli("list")
        self.assertEqual(code, 0)
        self.assertIn(rows[0]["id"], out)
        code, out, _ = self.cli("report")
        self.assertEqual(code, 0)
        self.assertIn(f"DNS Bench run {rows[0]['id']}", out)
        code, out, _ = self.cli("report", rows[0]["id"])
        self.assertEqual(code, 0)
        code, out, _ = self.cli("report", "all")
        self.assertEqual(code, 0)
        self.assertIn("all runs combined (1 run)", out)

    def test_run_json_no_save_quiet(self):
        code, out, err = self.cli("run", "--json", "--no-save", "--quiet")
        self.assertEqual(code, 0, err)
        run = json.loads(out)
        self.assertEqual(run["status"], "complete")
        self.assertEqual(run["recommendation"]["best"], "Fast")
        self.assertNotIn("[slow]", err)
        self.assertFalse(self.runs.exists() and any(self.runs.iterdir()))

    def test_run_overrides_are_not_saved(self):
        code, out, err = self.cli(
            "run",
            "--rounds",
            "2",
            "--resolvers",
            "spare,FAST",
            "--timeout-ms",
            "500",
            "--interval-ms",
            "60",
            "--json",
            "--no-save",
            "--quiet",
        )
        self.assertEqual(code, 0, err)
        run = json.loads(out)
        self.assertEqual({r["resolver"] for r in run["results"]}, {"Fast", "Spare"})
        self.assertEqual(len(run["results"]), 12)
        s = run["config"]["settings"]
        self.assertEqual((s["rounds"], s["timeout_ms"], s["per_server_interval_ms"]), (2, 500, 60))
        self.assertEqual(C.load_config(self.cfg), C.normalize_config(small_config()))

    def test_run_bad_overrides(self):
        code, _, err = self.cli("run", "--resolvers", "Nope")
        self.assertEqual(code, 2)
        self.assertIn("unknown resolver", err)
        # out-of-range overrides are usage errors that name the flag and the range
        for flag, value, rng in (
            ("--interval-ms", "10", "50 to 5000"),
            ("--rounds", "11", "1 to 10"),
            ("--rounds", "0", "1 to 10"),
            ("--timeout-ms", "20000", "200 to 10000"),
        ):
            with self.subTest(flag=flag, value=value):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                    cli.main(["run", flag, value, "--config", str(self.cfg), "--runs-dir", str(self.runs)])
                self.assertEqual(cm.exception.code, 2)
                self.assertIn(flag, err.getvalue())
                self.assertIn(rng, err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            cli.main(["run", "--rounds", "zero"])
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.fake.calls, 0)

    def test_ctrl_c_saves_partial_run(self):
        def interrupt(n):
            if n == 2:
                os.kill(os.getpid(), signal.SIGINT)

        self.fake.on_call = interrupt
        cfg = small_config()
        cfg["domains"] = [f"d{i}.example" for i in range(30)]
        C.save_config(cfg, self.cfg)
        before = signal.getsignal(signal.SIGINT)
        code, out, err = self.cli("run")
        self.assertEqual(code, 130, err)
        self.assertIn("Cancelling", err)
        self.assertIn("CANCELLED", out)
        rows = storage.list_runs(self.runs)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "cancelled")
        self.assertLess(rows[0]["n_queries"], 60)
        self.assertIs(signal.getsignal(signal.SIGINT), before)  # handler restored

    def test_a_crashed_run_is_saved_and_exits_1(self):
        real = self.fake

        def broken(server, domain, **kw):
            if server == "192.0.2.2":
                return {"status": "ok", "ms": "garbage"}  # float() of this raises inside the runner
            return real(server, domain, **kw)

        self.fake = broken
        code, out, err = self.cli("run")
        self.assertEqual(code, cli.EXIT_ERROR, err)
        self.assertIn("STOPPED BY AN ERROR", out)
        self.assertIn("stopped early after an internal error (ValueError", err)
        self.assertIn("and were saved", err)
        self.assertEqual([r["status"] for r in storage.list_runs(self.runs)], ["partial"])

    def test_sigterm_saves_partial_run_like_ctrl_c(self):
        def terminate(n):
            if n == 2:
                os.kill(os.getpid(), signal.SIGTERM)

        self.fake.on_call = terminate
        cfg = small_config()
        cfg["domains"] = [f"d{i}.example" for i in range(30)]
        C.save_config(cfg, self.cfg)
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        code, out, err = self.cli("run")
        self.assertEqual(code, cli.EXIT_INTERRUPTED, err)
        self.assertIn("Cancelling", err)
        self.assertIn("CANCELLED", out)
        self.assertEqual([r["status"] for r in storage.list_runs(self.runs)], ["cancelled"])
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)

    def test_second_ctrl_c_saves_without_waiting_for_queries_in_flight(self):
        # The first Ctrl-C comes while one query is stuck (like a server that never answers, with a
        # long timeout); the second comes while the run waits for it. The run is saved at once.
        def stuck(n):
            if n == 2:
                os.kill(os.getpid(), signal.SIGINT)
                threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGINT)).start()
                time.sleep(3)

        self.fake.on_call = stuck
        cfg = small_config()
        cfg["domains"] = [f"d{i}.example" for i in range(30)]
        C.save_config(cfg, self.cfg)
        t0 = time.monotonic()
        code, out, err = self.cli("run")
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertEqual(code, cli.EXIT_INTERRUPTED, err)
        self.assertIn("Press Ctrl-C again to save it now", err)
        self.assertIn("CANCELLED", out)
        rows = storage.list_runs(self.runs)
        self.assertEqual([r["status"] for r in rows], ["cancelled"])
        self.assertLess(rows[0]["n_queries"], 60)

    def test_second_ctrl_c_exits_without_waiting_for_the_stuck_query(self):
        # The in-process test above sees cmd_run return; only a real process shows whether the
        # interpreter then waits at exit for the stuck query's thread (it must not).
        script = textwrap.dedent(
            """
            import itertools, os, signal, sys, threading, time
            from dnsbench import cli, resolver
            from dnsbench.resolver import QueryResult

            calls = itertools.count(1)

            def query(server, domain, **kw):
                if next(calls) == 2:
                    os.kill(os.getpid(), signal.SIGINT)
                    threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGINT)).start()
                    time.sleep(8)  # a server that never answers, with a long timeout
                return QueryResult("ok", ms=1.0, rcode="NOERROR", answers=1)

            resolver.query = query
            sys.exit(cli.main(sys.argv[1:]))
            """
        )
        t0 = time.monotonic()
        p = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                "run",
                "--quiet",
                "--config",
                str(self.cfg),
                "--runs-dir",
                str(self.runs),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertLess(time.monotonic() - t0, 5, "the process waited for the stuck query")
        self.assertEqual(p.returncode, cli.EXIT_INTERRUPTED, p.stderr)
        self.assertEqual([r["status"] for r in storage.list_runs(self.runs)], ["cancelled"])

    def test_signals_during_the_save_are_ignored(self):
        real_save = storage.save_run_safely

        def save(run, runs_dir):
            os.kill(os.getpid(), signal.SIGINT)  # Ctrl-C just as the run is being saved
            os.kill(os.getpid(), signal.SIGTERM)
            return real_save(run, runs_dir)

        with mock.patch.object(storage, "save_run_safely", save):
            code, _, err = self.cli("run")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertEqual([r["status"] for r in storage.list_runs(self.runs)], ["complete"])

    def test_report_errors(self):
        self.assertEqual(self.cli("report")[0], 1)  # no runs yet
        self.assertEqual(self.cli("report", "all")[0], 1)
        self.assertEqual(self.cli("report", "../etc")[0], 2)
        self.assertEqual(self.cli("report", "20200101T000000Z")[0], 1)
        code, out, _ = self.cli("list")
        self.assertEqual(code, 0)
        self.assertIn("No runs saved yet", out)

    def test_config_commands(self):
        code, out, err = self.cli("config", "--path")
        self.assertEqual((code, out.strip()), (0, str(self.cfg)))
        self.assertIn(str(self.runs), err)  # stdout stays just the config path, for scripts
        code, out, _ = self.cli("config")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["domains"], small_config()["domains"])
        code, out, _ = self.cli("config", "--reset")
        self.assertEqual(code, 0)
        self.assertIn("System: 192.0.2.53 (from a test).", out)
        self.assertEqual(C.load_config(self.cfg), C.initial_config(lambda: HOME_NET)[0])

    def test_config_show_never_creates_the_file(self):
        self.cfg.unlink()
        code, out, err = self.cli("config")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), C.initial_config(lambda: HOME_NET)[0])
        self.assertIn("doesn't exist yet", err)
        self.assertFalse(self.cfg.exists())

    def test_config_detect_adds_or_updates_system(self):
        code, out, _ = self.cli("config", "--detect")
        self.assertEqual(code, 0, out)
        self.assertIn(f"Saved to {self.cfg}", out)
        cfg = C.load_config(self.cfg)
        self.assertEqual(cfg["resolvers"][:3], small_config()["resolvers"])
        self.assertEqual(cfg["resolvers"][3], {"name": "System", "servers": ["192.0.2.53"], "enabled": True})
        # On another network: the same entry gets the new servers.
        with mock.patch.object(sysdns, "detect", return_value=sysdns.Detected(["198.51.100.53"])):
            self.assertEqual(self.cli("config", "--detect")[0], 0)
        cfg = C.load_config(self.cfg)
        self.assertEqual(len(cfg["resolvers"]), 4)
        self.assertEqual(cfg["resolvers"][3]["servers"], ["198.51.100.53"])

    def test_config_detect_with_nothing_to_add(self):
        before = self.cfg.read_text()
        with mock.patch.object(sysdns, "detect", return_value=sysdns.Detected([], "/etc/resolv.conf")):
            code, _, err = self.cli("config", "--detect")
        self.assertEqual(code, 1)
        self.assertIn("No system resolvers found: /etc/resolv.conf lists no DNS servers", err)
        with mock.patch.object(sysdns, "detect", return_value=sysdns.Detected(["192.0.2.1"])):
            code, out, _ = self.cli("config", "--detect")  # already configured as "Fast"
        self.assertEqual(code, 0)
        self.assertIn("already in the list as Fast", out)
        self.assertEqual(self.cfg.read_text(), before)

    # A new config has the 60 default domains, 4 queries/s per server: 2 domains keep these tests quick.
    @mock.patch.dict(C.DEFAULT_CONFIG, {"domains": ["a.example", "b.example"]})
    def test_first_run_creates_the_config(self):
        self.cfg.unlink()
        code, _, err = self.cli("run", "--resolvers", "Cloudflare")
        self.assertEqual(code, 0, err)
        self.assertIn(f"Created {self.cfg} with the default resolvers. System: 192.0.2.53", err)
        self.assertEqual(C.load_config(self.cfg), C.initial_config(lambda: HOME_NET)[0])
        run = storage.load_run(storage.latest_run_id(self.runs), self.runs)
        self.assertIn("System", [r["name"] for r in run["config"]["resolvers"]])

    @mock.patch.dict(C.DEFAULT_CONFIG, {"domains": ["a.example", "b.example"]})
    def test_no_save_run_writes_no_config_either(self):
        self.cfg.unlink()
        code, out, _ = self.cli("run", "--no-save", "--json", "--quiet", "--resolvers", "System")
        self.assertEqual(code, 0)
        self.assertEqual({r["server"] for r in json.loads(out)["results"]}, {"192.0.2.53"})
        self.assertFalse(self.cfg.exists())
        self.assertFalse(self.runs.exists())

    def test_dnsbench_home_holds_the_data(self):
        home = Path(self.tmp.name) / "home"
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {paths.HOME_ENV: str(home)}),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err := io.StringIO()),
        ):
            self.assertEqual(cli.main(["config", "--path"]), 0)
        self.assertEqual(out.getvalue().strip(), str(home / "config.json"))
        self.assertIn(str(home / "runs"), err.getvalue())

    def test_outside_a_checkout_without_dnsbench_home_is_a_clear_error(self):
        err = io.StringIO()
        with (
            mock.patch.object(paths, "CHECKOUT_DIR", Path(self.tmp.name)),
            mock.patch.dict(os.environ, {paths.HOME_ENV: ""}),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(cli.main(["list"]), 1)
            self.assertIn("$DNSBENCH_HOME/config.json", cli.build_parser().format_help() + err.getvalue())
        self.assertIn("dns-bench: dns-bench is installed outside its checkout", err.getvalue())
        self.assertIn("Set DNSBENCH_HOME", err.getvalue())

    def test_invalid_config_file(self):
        self.cfg.write_text("{ nope")
        code, _, err = self.cli("run")
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", err)

    def test_no_command_is_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main([]), 2)

    def test_port_validation(self):
        parser = cli.build_parser()
        for bad in ("70000", "-1", "http"):
            with self.subTest(port=bad):
                with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as cm:
                    parser.parse_args(["serve", f"--port={bad}"])
                self.assertEqual(cm.exception.code, 2)
                self.assertIn("--port", err.getvalue())
        self.assertEqual(parser.parse_args(["serve", "--port", "0"]).port, 0)
        self.assertEqual(parser.parse_args(["serve"]).port, 8053)

    def test_rounds_override_cannot_exceed_the_query_limit(self):
        cfg = small_config()
        cfg["domains"] = [f"d{i}.example" for i in range(500)]
        cfg["resolvers"] = [
            {"name": f"R{i}", "servers": [f"192.0.2.{4 * i + j}" for j in range(1, 5)], "enabled": True}
            for i in range(3)
        ]
        C.save_config(cfg, self.cfg)
        code, _, err = self.cli("run", "--rounds", "10")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("a run would send 60,000 queries", err)
        self.assertEqual(self.fake.calls, 0)

    def test_serve_refuses_a_reachable_host_without_allow_remote(self):
        from dnsbench import server

        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        with mock.patch.object(server, "serve") as serve:
            for host in ("0.0.0.0", "::", "", "192.0.2.10", "2001:db8::1", "my-laptop.local"):
                with self.subTest(host=host):
                    code, _, err = self.cli("serve", "--host", host)
                    self.assertEqual(code, cli.EXIT_USAGE)
                    self.assertIn("refusing to listen", err)
                    self.assertIn("ssh -L 8053:127.0.0.1:8053", err)
            serve.assert_not_called()
            for args in (
                ("--host", "127.0.0.1"),
                ("--host", "localhost"),
                ("--host", "::1"),
                ("--host", "127.0.0.2"),
                ("--host", "0.0.0.0", "--allow-remote"),
            ):
                with self.subTest(args=args):
                    serve.reset_mock()
                    code, _, err = self.cli("serve", *args)
                    self.assertEqual(code, cli.EXIT_OK, err)
                    self.assertEqual(serve.call_args.args[0], args[1])
        # serve's Ctrl-C/kill handlers are put back afterwards
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)

    def test_unwritable_runs_dir_fails_before_any_query(self):
        blocker = Path(self.tmp.name) / "not-a-dir"
        blocker.write_text("x")
        for runs_dir in (blocker, blocker / "sub"):
            with self.subTest(runs_dir=runs_dir):
                out, err = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(resolver, "query", self.fake),
                    contextlib.redirect_stdout(out),
                    contextlib.redirect_stderr(err),
                ):
                    code = cli.main(["run", "--config", str(self.cfg), "--runs-dir", str(runs_dir)])
                self.assertEqual(code, 1)
                self.assertIn("cannot write to", err.getvalue())
                self.assertNotIn("Traceback", err.getvalue())
                self.assertEqual(self.fake.calls, 0)

    def test_save_failure_still_prints_report_and_keeps_data(self):
        rescue_dir = Path(self.tmp.name) / "rescue"
        rescue_dir.mkdir()
        with (
            mock.patch.object(storage, "save_run", side_effect=OSError(28, "No space left on device")),
            mock.patch.object(tempfile, "tempdir", str(rescue_dir)),
        ):
            code, out, err = self.cli("run")
        self.assertEqual(code, 1)
        self.assertIn("Recommendation: Use Fast", out)  # the measurements are not lost
        self.assertIn("could not save run", err)
        self.assertIn("No space left on device", err)
        rescued = list(rescue_dir.glob("dns-bench-*.json"))
        self.assertEqual(len(rescued), 1)
        self.assertEqual(len(json.loads(rescued[0].read_text())["results"]), 6)
        self.assertIn(str(rescued[0]), err)

    def test_all_failed_run_exits_1_but_is_saved(self):
        self.fake = mock.Mock(return_value=QueryResult("timeout", error="timeout", attempts=1))
        code, out, err = self.cli("run")
        self.assertEqual(code, 1)
        self.assertIn("nothing to recommend", out)
        self.assertIn("no resolver returned any successful answers", err)
        self.assertEqual(len(storage.list_runs(self.runs)), 1)
        code, out, _ = self.cli("run", "--json", "--no-save", "--quiet")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["summary"]["overall"]["ok"], 0)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_unwritable_config_is_a_clean_error(self):
        ro = Path(self.tmp.name) / "ro"
        ro.mkdir()
        (ro / "cfg.json").write_text(self.cfg.read_text())
        ro.chmod(0o555)
        try:
            for args in (
                ["config", "--reset", "--config", str(ro / "cfg.json")],
                ["config", "--detect", "--config", str(ro / "cfg.json")],
                ["run", "--config", str(ro / "missing.json")],  # a first run creates the config
            ):
                with self.subTest(args=args):
                    out, err = io.StringIO(), io.StringIO()
                    with (
                        mock.patch.object(resolver, "query", self.fake),
                        contextlib.redirect_stdout(out),
                        contextlib.redirect_stderr(err),
                    ):
                        code = cli.main([*args, "--runs-dir", str(self.runs)])
                    self.assertEqual(code, 1)
                    self.assertIn("cannot write", err.getvalue())
                    self.assertNotIn("Traceback", err.getvalue())
            self.assertEqual(self.fake.calls, 0)
        finally:
            ro.chmod(0o755)


class WrapperTest(unittest.TestCase):
    def test_version_is_major_minor_patch(self):
        # release.yml runs only for vX.Y.Z tags, and only when the tag is "v" + __version__
        self.assertRegex(__version__, r"^[0-9]+\.[0-9]+\.[0-9]+$")

    def test_wrapper_from_other_cwd_and_symlink(self):
        wrapper = ROOT / "dns-bench"
        self.assertTrue(os.access(wrapper, os.X_OK), "dns-bench must be executable")
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "dnsb"
            link.symlink_to(wrapper)
            for exe in (wrapper, link):
                with self.subTest(exe=exe):
                    p = subprocess.run(
                        [str(exe), "--version"],
                        cwd=tmp,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        env=env_without_home(),
                    )
                    self.assertEqual(p.returncode, 0, p.stderr)
                    self.assertIn(f"dns-bench {__version__}", p.stdout)
                    self.assertEqual(p.stderr, "")
            p = subprocess.run(
                [str(link), "config", "--path"],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=30,
                env=env_without_home(),
            )
            self.assertEqual(p.stdout.strip(), str(ROOT / "config.json"))  # the checkout's, not the cwd's

    def test_installed_command_runs_cli_main(self):
        # `uv tool install --editable .` creates the dns-bench command from this entry, so a typo here
        # would only show up at install time.
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(project["scripts"], {"dns-bench": "dnsbench.cli:main"})
        self.assertEqual(project["requires-python"], ">=3.13")
        self.assertEqual(project["dynamic"], ["version"])  # from dnsbench.__version__, as release.yml checks

    def test_python_dash_m(self):
        # From the checkout (python -m puts the cwd on sys.path), so it needs no install.
        p = subprocess.run(
            [sys.executable, "-m", "dnsbench", "--version"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), f"dns-bench {__version__}")
        with tempfile.TemporaryDirectory() as tmp:
            p = subprocess.run(
                [sys.executable, "-m", "dnsbench", "report", "not-a-run-id", "--runs-dir", tmp],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(p.returncode, cli.EXIT_USAGE, "the exit code must reach the shell")
        self.assertIn("dns-bench: invalid run id", p.stderr)


if __name__ == "__main__":
    unittest.main()
