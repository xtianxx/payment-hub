import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_payment_create  # noqa: E402
from cpu_plan import DEFAULT_GROUPS, parse_cpu_set  # noqa: E402
from run_payment_create import (  # noqa: E402
    DEFAULT_ARTIFACTS_ROOT,
    METRICS_HOST_PORT,
    PG_HOST_PORT,
    PROFILE,
    REQUIRED_ROUNDS,
    REPO,
    PreconditionError,
    RunError,
    Runner,
    parse_args,
    read_process_affinity,
    summarize_cpu_windows,
    validate_artifacts_root,
)


def metrics(http_reqs=24000, iterations=24000, started=None, dropped=0,
            business_passes=None, response_passes=None, timeouts=0):
    if started is None:
        started = iterations
    if business_passes is None:
        business_passes = http_reqs
    if response_passes is None:
        response_passes = http_reqs
    business_rate = 1 if http_reqs > 0 and business_passes == http_reqs else 0
    response_rate = 1 if http_reqs > 0 and response_passes == http_reqs else 0
    return {
        "metrics": {
            "http_reqs": {"values": {"count": http_reqs}},
            "iterations": {"values": {"count": iterations}},
            "payment_iteration_started": {"values": {"count": started}},
            "dropped_iterations": {"values": {"count": dropped}},
            "http_req_duration": {"values": {"p(95)": 1.0, "p(99)": 2.0}},
            "payment_business_ok": {
                "values": {"rate": business_rate, "passes": business_passes}
            },
            "payment_response_received": {
                "values": {"rate": response_rate, "passes": response_passes}
            },
            "http_req_failed": {"values": {"rate": 0 if business_rate == 1 else 1}},
            "checks": {"values": {"rate": business_rate}},
            "payment_business_fail": {
                "values": {"count": max(0, http_reqs - business_passes)}
            },
            "payment_issues": {"values": {"count": 0}},
            "payment_timeout": {"values": {"count": timeouts}},
            "payment_transport_error": {"values": {"count": 0}},
            "payment_status_5xx": {"values": {"count": 0}},
            "payment_status_4xx": {"values": {"count": 0}},
            "payment_status_other": {"values": {"count": 0}},
            "vus_max": {"values": {"max": 128}},
        }
    }


class CliConfigTest(unittest.TestCase):
    def test_fixed_profile_and_rounds_remain_compatible(self):
        args = parse_args([
            "--migrate-image", "local/migrate:test",
            "--profile", "200", "--rounds", "5",
        ])
        self.assertEqual(args.profile, PROFILE)
        self.assertEqual(args.rounds, REQUIRED_ROUNDS)

    def test_other_profiles_and_round_counts_are_rejected(self):
        for argv in (
            ["--profile", "smoke"],
            ["--rounds", "1"],
            ["--rounds", "3"],
        ):
            with self.assertRaises(SystemExit, msg=argv):
                parse_args(argv)

    def test_default_evidence_root_is_outside_repository(self):
        self.assertFalse(DEFAULT_ARTIFACTS_ROOT.resolve().is_relative_to(REPO.resolve()))
        self.assertEqual(parse_args(["--check-cpu-plan"]).artifacts_dir, None)

    def test_relative_artifacts_dir_is_resolved(self):
        runner = Runner(parse_args(["--migrate-image", "local/migrate:test",
                                    "--artifacts-dir", "evidence"]))
        self.assertTrue(runner.run_root.is_absolute())

    def test_artifacts_root_inside_repository_is_rejected(self):
        with self.assertRaises(PreconditionError):
            validate_artifacts_root(REPO)
        with self.assertRaises(PreconditionError):
            validate_artifacts_root(REPO / "artifacts" / "run")

    def test_artifacts_root_outside_repository_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            validate_artifacts_root(Path(tmp) / "run")


class FixedPortTest(unittest.TestCase):
    def test_benchmark_ports_are_pinned(self):
        self.assertEqual(PG_HOST_PORT, 55432)
        self.assertEqual(METRICS_HOST_PORT, 9091)

    def test_benchmark_overlay_pins_the_fixed_port(self):
        text = (REPO / "tests" / "benchmark" / "compose.benchmark.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn('published: "55432"', text)
        self.assertIn("!override", text)

    def test_environment_uses_fixed_port_and_metrics_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(parse_args([
                "--migrate-image", "local/migrate:test",
                "--artifacts-dir", tmp,
            ]))
            runner.prepare_dirs()
            runner.env_dir.mkdir(parents=True, exist_ok=True)
            runner.create_environment()
            self.assertEqual(runner.pg_port, 55432)
            self.assertEqual(
                runner.app_env["PAYMENT_HUB_METRICS_ADDR"], "127.0.0.1:9091"
            )
            compose_env = runner.compose_env_path.read_text(encoding="utf-8")
            self.assertIn("PAYMENT_HUB_TEST_PG_PORT=55432", compose_env)

    def test_busy_port_is_rejected_with_the_port_number(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            busy_port = busy.getsockname()[1]
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as spare:
                spare.bind(("127.0.0.1", 0))
                free_port = spare.getsockname()[1]
            runner = Runner.__new__(Runner)
            with self.assertRaises(PreconditionError) as caught:
                runner.check_fixed_ports(pg_port=busy_port, metrics_port=free_port)
            self.assertIn(str(busy_port), str(caught.exception))


class RunLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock_path = Path(self.tmp.name) / "payment-hub-load.lock"
        patcher = mock.patch.object(
            run_payment_create, "run_lock_path", return_value=self.lock_path
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_runner(self, name):
        artifacts = Path(self.tmp.name) / name
        artifacts.mkdir()
        runner = Runner(parse_args([
            "--migrate-image", "local/migrate:test",
            "--artifacts-dir", str(artifacts),
        ]))
        runner.prepare_dirs()
        return runner

    def test_lock_is_host_wide_and_records_holder(self):
        first = self.make_runner("run-a")
        first.acquire_run_lock()
        holder = self.lock_path.read_text(encoding="utf-8")
        self.assertIn(f"pid={os.getpid()}", holder)
        self.assertIn(first.base_run_id, holder)
        self.assertNotIn("password", holder.lower())

        # A different artifacts root must not bypass the host-wide lock.
        second = self.make_runner("run-b")
        with self.assertRaises(PreconditionError) as caught:
            second.acquire_run_lock()
        self.assertIn(str(self.lock_path), str(caught.exception))
        self.assertIn(f"pid={os.getpid()}", str(caught.exception))

        first.release_run_lock()
        first.release_run_lock()
        second.acquire_run_lock()
        second.release_run_lock()

    def test_symlink_lock_path_is_rejected_without_touching_target(self):
        target = Path(self.tmp.name) / "target"
        target.write_text("keep\n", encoding="utf-8")
        self.lock_path.symlink_to(target)
        runner = self.make_runner("run-link")
        with self.assertRaises(PreconditionError) as caught:
            runner.acquire_run_lock()
        self.assertIn("symlink", str(caught.exception))
        self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")

    def test_hard_linked_lock_file_is_rejected(self):
        self.lock_path.write_text("", encoding="utf-8")
        os.link(self.lock_path, Path(self.tmp.name) / "second-link")
        runner = self.make_runner("run-hardlink")
        with self.assertRaises(PreconditionError) as caught:
            runner.acquire_run_lock()
        self.assertIn("hard links", str(caught.exception))


class CpuWindowTest(unittest.TestCase):
    def test_short_saturated_window_is_not_hidden_by_average(self):
        # 0.5, 0.5, 1.8, 0.5 cores in four 1 s windows: average 0.825, peak 1.8.
        ticks = [(0.0, 0), (1.0, 50), (2.0, 100), (3.0, 280), (4.0, 330)]
        result = summarize_cpu_windows(ticks, attempts=5, clock_ticks=100, allowed_cpus=2)
        self.assertTrue(result["saturated"])
        self.assertEqual(result["saturated_windows"], 1)
        self.assertEqual(result["peak_window_cores"], 1.8)
        self.assertLess(result["avg_cores"], 1.0)
        self.assertEqual(result["sampling_status"], "ok")

    def test_missing_cpu_sample_is_unknown(self):
        ticks = [(0.0, 0), (1.0, 50), (2.0, 100)]
        result = summarize_cpu_windows(ticks, attempts=4, clock_ticks=100, allowed_cpus=2)
        self.assertEqual(result["sampling_status"], "unknown")
        self.assertEqual(result["missing_samples"], 1)
        self.assertFalse(result["saturated"])


class ExecRaceTest(unittest.TestCase):
    def setUp(self):
        self.app_cpus = parse_cpu_set(DEFAULT_GROUPS["app"])
        try:
            allowed = set(os.sched_getaffinity(0))
        except AttributeError:
            self.skipTest("sched_getaffinity unavailable")
        if not self.app_cpus <= allowed:
            self.skipTest(f"{sorted(self.app_cpus)} not in guest affinity {sorted(allowed)}")
        start = sorted(allowed - self.app_cpus)
        if not start:
            self.skipTest("no CPU outside the app mask to start the launcher")
        self.start_cpus = {start[0]}
        self.sleep_path = shutil.which("sleep")
        if not self.sleep_path:
            self.skipTest("sleep executable unavailable")

    def test_wait_for_exec_waits_for_late_affinity_launcher(self):
        # A local launcher starts on a CPU outside the app mask, waits for the
        # parent's go-ahead, then pins itself and execs sleep. This reproduces
        # the read-before-exec window without depending on the caller's own
        # affinity or on a fixed sleep delay.
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / "ready"
            proceed = Path(tmp) / "proceed"
            launcher = (
                "import os,time,pathlib\n"
                f"os.sched_setaffinity(0,{sorted(self.start_cpus)!r})\n"
                f"pathlib.Path({str(ready)!r}).write_text('ready')\n"
                f"while not pathlib.Path({str(proceed)!r}).exists():\n"
                "    time.sleep(0.02)\n"
                f"os.sched_setaffinity(0,{sorted(self.app_cpus)!r})\n"
                f"os.execv({self.sleep_path!r}, ['sleep','2'])\n"
            )
            proc = subprocess.Popen([sys.executable, "-c", launcher])
            try:
                deadline = time.time() + 5
                while not ready.exists() and time.time() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "launcher did not reach ready state")
                pre = read_process_affinity(proc.pid)
                self.assertNotEqual(pre, self.app_cpus)
                proceed.write_text("go", encoding="utf-8")
                actual = Runner.__new__(Runner).wait_for_exec(
                    proc, Path(self.sleep_path), "sleep", 10
                )
                self.assertEqual(actual, Path(self.sleep_path).resolve())
                self.assertEqual(read_process_affinity(proc.pid), self.app_cpus)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)

    def test_wait_for_exec_with_real_taskset(self):
        if not shutil.which("taskset"):
            self.skipTest("taskset unavailable")
        proc = subprocess.Popen(
            ["taskset", "-c", DEFAULT_GROUPS["app"], self.sleep_path, "1"]
        )
        try:
            actual = Runner.__new__(Runner).wait_for_exec(
                proc, Path(self.sleep_path), "sleep", 10
            )
            self.assertEqual(actual, Path(self.sleep_path).resolve())
            self.assertEqual(read_process_affinity(proc.pid), self.app_cpus)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)

    def test_wait_for_exec_rejects_early_exit(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.05)"]
        )
        try:
            with self.assertRaises(RunError):
                Runner.__new__(Runner).wait_for_exec(
                    proc, Path(self.sleep_path), "sleep", 5
                )
        finally:
            proc.wait(timeout=5)


class PoolConfigTest(unittest.TestCase):
    def make_runner(self, text):
        runner = Runner.__new__(Runner)
        root = Path(tempfile.mkdtemp(prefix="payment-pool-config-test-"))
        runner.app_log = root / "app.log"
        runner.app_log.write_text(text, encoding="utf-8")
        return runner

    def test_valid_max_conns_evidence(self):
        runner = self.make_runner(
            'POOL_CONFIG {"configured_max_conns":16,"max_conns":16}\n'
        )
        result = runner.parse_pool_config()
        self.assertTrue(result["ok"])
        self.assertEqual(result["configured_max_conns"], 16)
        self.assertEqual(result["max_conns"], 16)

    def test_wrong_max_conns_is_rejected(self):
        runner = self.make_runner(
            'POOL_CONFIG {"configured_max_conns":16,"max_conns":32}\n'
        )
        self.assertFalse(runner.parse_pool_config()["ok"])

    def test_missing_evidence_is_rejected(self):
        runner = self.make_runner("ordinary app log line\n")
        self.assertFalse(runner.parse_pool_config()["ok"])


class StageLogicTest(unittest.TestCase):
    def make_runner(self, data, sql_ok=True, cpu_available=True,
                    summary_error=None, k6_exit=0):
        runner = Runner.__new__(Runner)
        root = Path(tempfile.mkdtemp(prefix="payment-stage-test-"))
        runner.run_dir = root
        runner.logs_dir = root / "logs"
        runner.db_dir = root / "db"
        runner.logs_dir.mkdir()
        runner.db_dir.mkdir()
        observation = {
            "affinity_required": "0-1",
            "affinity_effective": "0-1",
            "started_epoch": 100.0,
            "finished_epoch": 220.0,
            "cpu": {
                "available": cpu_available,
                "sampling_status": "ok" if cpu_available else "unknown",
                "avg_cores": 0.5,
                "peak_window_cores": 0.5,
                "saturated": False,
            },
        }
        if summary_error:
            observation["summary_error"] = summary_error
        runner.run_k6 = lambda *args, **kwargs: (
            k6_exit, data, root / "summary.json", observation
        )
        runner.verify_sql = lambda *args, **kwargs: {
            "ok": sql_ok,
            "values": {"succeeded": args[2] if len(args) > 2 else 0},
            "seq_limit": kwargs.get("seq_limit", 0),
            "expected_count": args[2] if len(args) > 2 else 0,
        }
        return runner

    def test_clean_load_is_complete_and_nonfatal(self):
        runner = self.make_runner(metrics())
        record = runner.stage_record("load200", 200, 120, "P", 128, 128)
        self.assertTrue(record["complete"])
        self.assertFalse(record["fatal"])
        self.assertTrue(record["target_met"])
        self.assertTrue(record["target_exact"])
        self.assertTrue(record["key_metrics_ok"])
        self.assertEqual(record["sequence_set"], "exact")
        self.assertTrue(record["sequence_set_exact"])
        self.assertEqual(record["sql"]["seq_limit"], 24000)
        self.assertEqual(record["sql"]["expected_count"], 24000)
        self.assertEqual(record["started_iterations"], 24000)
        self.assertEqual(record["completed_iterations"], 24000)
        self.assertEqual(record["iterations"], 24000)
        self.assertAlmostEqual(record["actual_start_rate"], 200.0, places=6)
        self.assertAlmostEqual(record["actual_completion_rate"], 200.0, places=6)
        self.assertAlmostEqual(record["actual_http_rate"], 200.0, places=6)

    def test_dropped_is_incomplete_but_not_fatal(self):
        runner = self.make_runner(metrics(
            http_reqs=23999, iterations=24000, dropped=1,
            business_passes=23999, response_passes=23999,
        ))
        record = runner.stage_record("load200", 200, 120, "P", 128, 128)
        self.assertFalse(record["complete"])
        self.assertFalse(record["fatal"])
        self.assertEqual(record["sequence_set"], "not_exact_due_to_dropped_iterations")
        self.assertEqual(record["sql"]["seq_limit"], 24000)
        self.assertEqual(record["sql"]["expected_count"], 23999)

    def test_zero_drops_but_short_target_is_incomplete(self):
        runner = self.make_runner(metrics(
            http_reqs=23999, iterations=23999, started=23999, dropped=0,
            business_passes=23999, response_passes=23999,
        ))
        record = runner.stage_record("load200", 200, 120, "P", 128, 128)
        self.assertFalse(record["complete"])
        self.assertFalse(record["fatal"])
        self.assertFalse(record["target_met"])
        self.assertEqual(record["target_iterations"], 24000)
        self.assertIn("below target 24000", "; ".join(record["incomplete_reasons"]))
        self.assertEqual(record["sequence_set"], "not_exact_due_to_target_shortfall")

    def test_missing_p95_is_incomplete(self):
        data = metrics()
        del data["metrics"]["http_req_duration"]
        runner = self.make_runner(data)
        record = runner.stage_record("load200", 200, 120, "P", 128, 128)
        self.assertFalse(record["complete"])
        self.assertFalse(record["fatal"])
        self.assertFalse(record["key_metrics_ok"])
        self.assertIn("http_req_duration.p(95)", record["missing_metrics"])
        self.assertIn("missing key metrics", "; ".join(record["incomplete_reasons"]))
        self.assertEqual(record["sequence_set"], "not_exact_due_to_missing_metrics")

    def test_absent_dropped_metric_accepted_when_exit_key_target_ok(self):
        data = metrics(
            http_reqs=11, iterations=11, started=11,
            business_passes=11, response_passes=11,
        )
        del data["metrics"]["dropped_iterations"]
        record = self.make_runner(data).stage_record("smoke", 2, 5, "S", 4, 8)
        self.assertTrue(record["complete"])
        self.assertEqual(
            record["dropped_iterations_metric_state"],
            "absent_accepted_not_reported",
        )
        self.assertEqual(
            record["dropped_iterations_source"],
            "k6_v2.4.0_summary_absent_no_samples",
        )
        self.assertTrue(record["dropped_iterations_absent_accepted"])
        self.assertEqual(record["dropped_iterations"], 0)
        self.assertNotIn("dropped_iterations.count", record["missing_metrics"])

    def test_absent_dropped_metric_rejected_when_exit_nonzero(self):
        data = metrics(
            http_reqs=11, iterations=11, started=11,
            business_passes=11, response_passes=11,
        )
        del data["metrics"]["dropped_iterations"]
        record = self.make_runner(data, k6_exit=1).stage_record(
            "smoke", 2, 5, "S", 4, 8
        )
        self.assertFalse(record["complete"])
        self.assertEqual(
            record["dropped_iterations_metric_state"], "absent_not_accepted"
        )
        self.assertFalse(record["dropped_iterations_absent_accepted"])
        self.assertIn("dropped_iterations.count", record["missing_metrics"])
        self.assertIn("k6 exit 1", record["incomplete_reasons"])

    def test_absent_dropped_metric_rejected_when_target_short(self):
        data = metrics(
            http_reqs=9, iterations=9, started=9,
            business_passes=9, response_passes=9,
        )
        del data["metrics"]["dropped_iterations"]
        record = self.make_runner(data).stage_record("smoke", 2, 5, "S", 4, 8)
        self.assertFalse(record["complete"])
        self.assertEqual(
            record["dropped_iterations_metric_state"], "absent_not_accepted"
        )
        self.assertFalse(record["target_met"])
        self.assertIn("outside [10, 11] tolerance", "; ".join(record["incomplete_reasons"]))

    def test_absent_dropped_metric_rejected_when_other_metric_missing(self):
        data = metrics(
            http_reqs=11, iterations=11, started=11,
            business_passes=11, response_passes=11,
        )
        del data["metrics"]["dropped_iterations"]
        del data["metrics"]["http_req_duration"]
        record = self.make_runner(data).stage_record("smoke", 2, 5, "S", 4, 8)
        self.assertFalse(record["complete"])
        self.assertEqual(
            record["dropped_iterations_metric_state"], "absent_not_accepted"
        )
        self.assertIn("dropped_iterations.count", record["missing_metrics"])
        self.assertIn("http_req_duration.p(95)", record["missing_metrics"])

    def test_present_positive_dropped_is_rejected(self):
        data = metrics(
            http_reqs=10, iterations=10, started=10, dropped=1,
            business_passes=10, response_passes=10,
        )
        record = self.make_runner(data).stage_record("smoke", 2, 5, "S", 4, 8)
        self.assertFalse(record["complete"])
        self.assertEqual(
            record["dropped_iterations_metric_state"], "reported_positive"
        )
        self.assertEqual(record["dropped_iterations"], 1)
        self.assertIn("dropped_iterations 1", "; ".join(record["incomplete_reasons"]))

    def test_present_malformed_dropped_is_rejected(self):
        data = metrics(
            http_reqs=11, iterations=11, started=11,
            business_passes=11, response_passes=11,
        )
        data["metrics"]["dropped_iterations"] = {"values": {"count": "bad"}}
        record = self.make_runner(data).stage_record("smoke", 2, 5, "S", 4, 8)
        self.assertFalse(record["complete"])
        self.assertEqual(
            record["dropped_iterations_metric_state"], "present_malformed"
        )
        self.assertIn("dropped_iterations.malformed", record["missing_metrics"])
        self.assertIn(
            "dropped_iterations metric present but malformed",
            "; ".join(record["incomplete_reasons"]),
        )

    def test_present_null_dropped_is_rejected(self):
        data = metrics(
            http_reqs=11, iterations=11, started=11,
            business_passes=11, response_passes=11,
        )
        data["metrics"]["dropped_iterations"] = None
        record = self.make_runner(data).stage_record("smoke", 2, 5, "S", 4, 8)
        self.assertFalse(record["complete"])
        self.assertEqual(
            record["dropped_iterations_metric_state"], "present_malformed"
        )
        self.assertIn("dropped_iterations.malformed", record["missing_metrics"])

    def test_reported_zero_dropped_is_accepted(self):
        data = metrics(
            http_reqs=11, iterations=11, started=11,
            business_passes=11, response_passes=11,
        )
        record = self.make_runner(data).stage_record("smoke", 2, 5, "S", 4, 8)
        self.assertTrue(record["complete"])
        self.assertEqual(record["dropped_iterations_metric_state"], "reported_zero")
        self.assertEqual(record["dropped_iterations_source"], "summary_reported_zero")

    def test_absent_dropped_metric_rejected_when_load_target_short(self):
        data = metrics(
            http_reqs=23999, iterations=23999, started=23999,
            business_passes=23999, response_passes=23999,
        )
        del data["metrics"]["dropped_iterations"]
        record = self.make_runner(data).stage_record(
            "load200", 200, 120, "P", 128, 128
        )
        self.assertFalse(record["complete"])
        self.assertEqual(
            record["dropped_iterations_metric_state"], "absent_not_accepted"
        )
        self.assertIn("below target 24000", "; ".join(record["incomplete_reasons"]))

    def test_missing_started_counter_is_incomplete(self):
        data = metrics()
        del data["metrics"]["payment_iteration_started"]
        runner = self.make_runner(data)
        record = runner.stage_record("load200", 200, 120, "P", 128, 128)
        self.assertFalse(record["complete"])
        self.assertFalse(record["fatal"])
        self.assertIn("payment_iteration_started.count", record["missing_metrics"])
        self.assertFalse(record["target_met"])

    def test_one_over_target_is_recorded_not_silent(self):
        runner = self.make_runner(metrics(
            http_reqs=24001, iterations=24001, started=24001,
            business_passes=24001, response_passes=24001,
        ))
        record = runner.stage_record("load200", 200, 120, "P", 128, 128)
        self.assertTrue(record["complete"])
        self.assertTrue(record["target_met"])
        self.assertFalse(record["target_exact"])
        self.assertEqual(record["target_overshoot"], 1)
        self.assertIsNotNone(record["target_overshoot_note"])
        self.assertEqual(record["sequence_set"], "exact_with_target_overshoot")
        self.assertFalse(record["sequence_set_exact"])

    def test_smoke_10_or_11_tolerance(self):
        for count in (10, 11):
            runner = self.make_runner(metrics(
                http_reqs=count, iterations=count, started=count,
                business_passes=count, response_passes=count,
            ))
            record = runner.stage_record("smoke", 2, 5, "S", 4, 8)
            self.assertTrue(record["complete"], count)
            self.assertTrue(record["target_met"], count)
        runner = self.make_runner(metrics(
            http_reqs=9, iterations=9, started=9,
            business_passes=9, response_passes=9,
        ))
        record = runner.stage_record("smoke", 2, 5, "S", 4, 8)
        self.assertFalse(record["complete"])
        self.assertFalse(record["target_met"])
        self.assertIn("outside [10, 11] tolerance", "; ".join(record["incomplete_reasons"]))

    def test_sql_failure_is_incomplete_not_fatal(self):
        runner = self.make_runner(metrics(), sql_ok=False)
        record = runner.stage_record("load200", 200, 120, "P", 128, 128)
        self.assertFalse(record["complete"])
        self.assertFalse(record["fatal"])
        self.assertEqual(record["sequence_set"], "not_exact_due_to_sql_reconciliation")
        self.assertIn("SQL reconciliation gate failed", record["incomplete_reasons"])

    def test_missing_k6_summary_is_incomplete_not_fatal(self):
        runner = self.make_runner(
            None, summary_error="k6 did not write summary for load200"
        )
        record = runner.stage_record("load200", 200, 120, "P", 128, 128)
        self.assertFalse(record["complete"])
        self.assertFalse(record["fatal"])
        self.assertIn("k6 did not write summary", record["incomplete_reasons"][0])
        self.assertEqual(record["missing_metrics"], ["k6_summary"])

    def test_missing_k6_cpu_is_unknown_not_fatal(self):
        runner = self.make_runner(metrics(), cpu_available=False)
        record = runner.stage_record("load200", 200, 120, "P", 128, 128)
        self.assertTrue(record["complete"])
        self.assertFalse(record["fatal"])
        self.assertFalse(record["k6_cpu"]["available"])
        self.assertEqual(record["k6_cpu"]["sampling_status"], "unknown")

    def test_no_response_is_incomplete_not_fatal(self):
        runner = self.make_runner(metrics(response_passes=0))
        record = runner.stage_record("load200", 200, 120, "P", 128, 128)
        self.assertFalse(record["complete"])
        self.assertFalse(record["fatal"])
        self.assertIn("no HTTP responses received", record["incomplete_reasons"])


class InterruptRecordTest(unittest.TestCase):
    def test_system_exit_finalizes_and_saves_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner.__new__(Runner)
            runner.base_run_id = "abc123"
            runner.profile = "200"
            runner.target_rate = 200
            runner.round_index = 1
            runner.round_records = []
            runner.stage_records = []
            runner.volumes = []
            runner.cleanup_warnings = []
            runner.cpu_plan = {}
            runner.pool_config = None
            runner.volume = None
            runner.cleanup_warning = None
            runner.compose_started = False
            runner.compose_project = None
            runner.compose_network = None
            runner.app_port = 0
            runner.pg_port = 0
            runner.app_sha256 = None
            runner.app_log = Path(tmp) / "app.log"
            runner.env_dir = Path(tmp) / "env"
            runner.run_dir = Path(tmp) / "run"
            runner.begin_round = lambda index: {
                "round": index,
                "round_id": "abc123r1",
                "started_at_utc": "2026-11-25T00:00:00+00:00",
                "status": "running",
                "stages": [],
                "fatal": False,
                "fatal_reasons": [],
            }

            def interrupt():
                raise SystemExit(143)

            runner.start_compose = interrupt
            runner.stop_app = lambda: {"ok": True, "exit": 0}
            runner.stop_compose = lambda: {"ok": True, "exit": 0}
            runner.cap_log = lambda path: False
            runner.tree_bytes = lambda path: 0

            with self.assertRaises(SystemExit):
                runner.run_round(1)

            record = runner.round_records[0]
            self.assertTrue(record["interrupted"])
            self.assertEqual(record["status"], "aborted")
            self.assertIn("finished_at_utc", record)
            self.assertFalse(record["complete"])


if __name__ == "__main__":
    unittest.main()
