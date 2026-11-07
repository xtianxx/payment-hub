"""Mock-only regression tests for scripts/check-db-foundation.py.

These tests never start Docker, PostgreSQL, Goose, Go, or the actual launcher
entrypoint. They use mocked subprocesses and standard-library temporary
directories (TMPDIR when set); they are not database acceptance evidence.
"""

import argparse
import json
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import importlib.util

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "check_db_foundation", REPO / "scripts/check-db-foundation.py"
)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


REQUIRED_INTEGRATION_TEST = "TestCommitHookIntegrationHidesReplyAfterRealCommit"
T020_REQUIRED_INTEGRATION_TEST = "TestHarnessProcessPreservesParentStateIntegration"
PACKAGE_OK_OUTPUT = (
    "ok  \tpayment-hub/internal/postgres\t0.1s\n"
    "ok  \tpayment-hub/internal/mock\t0.2s\n"
    "ok  \tpayment-hub/tests/harness\t0.3s\n"
)


def completed(argv, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


def required_pass_output(name=REQUIRED_INTEGRATION_TEST):
    return (
        f"=== RUN   {name}\n"
        f"--- PASS: {name} (0.00s)\n"
        "PASS\n"
    )


def both_required_pass_output():
    return required_pass_output(REQUIRED_INTEGRATION_TEST) + required_pass_output(
        T020_REQUIRED_INTEGRATION_TEST
    )


class FakeProcess:
    def __init__(self, pid=12345):
        self.pid = pid
        self.returncode = 0
        self.calls = 0

    def communicate(self, timeout=None):
        self.calls += 1
        if self.calls == 1:
            raise subprocess.TimeoutExpired("go test", timeout)
        return "", ""

    def kill(self):
        return None

    def wait(self, timeout=None):
        return 0


class CheckDBFoundationMockTests(unittest.TestCase):
    """Mock-only launcher regression tests; not Docker/DB acceptance."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="launcher-revision-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.args = argparse.Namespace(
            go="/bin/true",
            cache_dir=str(root / "cache"),
            prepare_images=False,
            download_modules=False,
            cleanup_owned_resources=False,
            artifact_root=str(root / "artifacts"),
        )
        self.launcher = MOD.Launcher(self.args)
        self.launcher.docker = "/bin/true"
        self.launcher.docker_env = {}
        self.launcher.compose_env = {}

    def _write_manifest_fixture(self, root):
        for relative in MOD.SOURCE_MANIFEST_EXPLICIT:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n", encoding="utf-8")
        return root

    def test_runtime_source_manifest_tracks_untracked_cmd_build_inputs(self):
        fixture = self._write_manifest_fixture(
            Path(self.temp.name) / "manifest-cmd-fixture"
        )
        self.launcher.repo = fixture
        before = self.launcher.collect_source_manifest()
        manifest_paths = {entry["path"] for entry in self.launcher.source["manifest"]}
        self.assertNotIn("cmd/payment-hub/main.go", manifest_paths)

        cmd_main = fixture / "cmd" / "payment-hub" / "main.go"
        cmd_main.parent.mkdir(parents=True)
        cmd_main.write_text("package main\n\nfunc main() {}\n", encoding="utf-8")

        after = self.launcher.collect_source_manifest()
        self.assertNotEqual(after, before)
        manifest_paths = {entry["path"] for entry in self.launcher.source["manifest"]}
        self.assertIn("cmd/payment-hub/main.go", manifest_paths)
        self.launcher.verify_source_manifest()
        self.assertTrue(self.launcher.source["manifest_unchanged"])

    def test_runtime_parser_requires_required_root_verbose_pass(self):
        _, problems = self.launcher._parse_test_output(PACKAGE_OK_OUTPUT, "", 0)
        self.assertIn("required-test-missing", problems)

    def test_runtime_parser_accepts_required_root_verbose_pass(self):
        results, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT + both_required_pass_output(), "", 0
        )
        self.assertEqual(problems, [])
        self.assertEqual(results["tests/harness"]["status"], "pass")

    def test_runtime_parser_rejects_duplicate_required_root_pass(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT
            + both_required_pass_output()
            + required_pass_output(REQUIRED_INTEGRATION_TEST),
            "",
            0,
        )
        self.assertIn("required-test-duplicate", problems)

    def test_runtime_parser_rejects_required_root_skip(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT
            + required_pass_output(T020_REQUIRED_INTEGRATION_TEST)
            + f"--- SKIP: {REQUIRED_INTEGRATION_TEST} (0.00s)\n",
            "",
            0,
        )
        self.assertIn("required-test-skip", problems)

    def test_runtime_parser_rejects_required_root_fail(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT
            + required_pass_output(T020_REQUIRED_INTEGRATION_TEST)
            + f"--- FAIL: {REQUIRED_INTEGRATION_TEST} (0.00s)\n",
            "",
            0,
        )
        self.assertIn("required-test-fail", problems)

    def test_runtime_run_tests_uses_verbose_and_reports_required_root(self):
        self.launcher.published_port = 54321
        self.launcher._run_process_group = mock.Mock(
            side_effect=[
                completed(["go", "vet"], 0, "", ""),
                completed(
                    ["go", "test"],
                    0,
                    PACKAGE_OK_OUTPUT + both_required_pass_output(),
                    "",
                ),
            ]
        )
        self.launcher.run_tests()
        test_argv = self.launcher._run_process_group.call_args_list[1].args[3]
        self.assertIn("-v", test_argv)
        self.assertEqual(
            self.launcher.tests["required_test_results"].get(
                REQUIRED_INTEGRATION_TEST
            ),
            "pass",
        )
        report_tests = self.launcher._build_report()["tests"]
        self.assertEqual(
            report_tests["required_test_results"].get(REQUIRED_INTEGRATION_TEST),
            "pass",
        )

    def test_runtime_run_tests_rejects_missing_required_root(self):
        self.launcher.published_port = 54321
        self.launcher._run_process_group = mock.Mock(
            side_effect=[
                completed(["go", "vet"], 0, "", ""),
                completed(["go", "test"], 0, PACKAGE_OK_OUTPUT, ""),
            ]
        )
        with self.assertRaises(MOD.LauncherError) as raised:
            self.launcher.run_tests()
        self.assertEqual(raised.exception.stage, "test")
        self.assertEqual(
            self.launcher.tests["required_test_results"].get(
                REQUIRED_INTEGRATION_TEST
            ),
            "missing",
        )

    def test_t020_parser_requires_second_required_root(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT + required_pass_output(REQUIRED_INTEGRATION_TEST),
            "",
            0,
        )
        self.assertIn("required-test-missing", problems)

    def test_t020_parser_accepts_both_required_roots(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT + both_required_pass_output(), "", 0
        )
        self.assertEqual(problems, [])

    def test_t020_parser_rejects_second_root_skip(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT
            + required_pass_output(REQUIRED_INTEGRATION_TEST)
            + f"--- SKIP: {T020_REQUIRED_INTEGRATION_TEST} (0.00s)\n",
            "",
            0,
        )
        self.assertIn("required-test-skip", problems)

    def test_t020_parser_rejects_second_root_fail(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT
            + required_pass_output(REQUIRED_INTEGRATION_TEST)
            + f"--- FAIL: {T020_REQUIRED_INTEGRATION_TEST} (0.00s)\n",
            "",
            0,
        )
        self.assertIn("required-test-fail", problems)

    def test_t020_parser_rejects_second_root_duplicate(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT
            + required_pass_output(REQUIRED_INTEGRATION_TEST)
            + required_pass_output(T020_REQUIRED_INTEGRATION_TEST)
            + required_pass_output(T020_REQUIRED_INTEGRATION_TEST),
            "",
            0,
        )
        self.assertIn("required-test-duplicate", problems)

    def test_t020_parser_keeps_commit_root_mandatory(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT
            + required_pass_output(T020_REQUIRED_INTEGRATION_TEST),
            "",
            0,
        )
        self.assertIn("required-test-missing", problems)

    def test_t020_run_tests_reports_both_required_roots(self):
        self.launcher.published_port = 54321
        self.launcher._run_process_group = mock.Mock(
            side_effect=[
                completed(["go", "vet"], 0, "", ""),
                completed(
                    ["go", "test"],
                    0,
                    PACKAGE_OK_OUTPUT + both_required_pass_output(),
                    "",
                ),
            ]
        )
        self.launcher.run_tests()
        test_argv = self.launcher._run_process_group.call_args_list[1].args[3]
        self.assertIn("-v", test_argv)
        expected = {
            REQUIRED_INTEGRATION_TEST: "pass",
            T020_REQUIRED_INTEGRATION_TEST: "pass",
        }
        self.assertEqual(self.launcher.tests["required_test_results"], expected)
        self.assertEqual(
            self.launcher._build_report()["tests"]["required_test_results"],
            expected,
        )

    def test_t020_run_tests_rejects_only_commit_root_pass(self):
        self.launcher.published_port = 54321
        self.launcher._run_process_group = mock.Mock(
            side_effect=[
                completed(["go", "vet"], 0, "", ""),
                completed(
                    ["go", "test"],
                    0,
                    PACKAGE_OK_OUTPUT
                    + required_pass_output(REQUIRED_INTEGRATION_TEST),
                    "",
                ),
            ]
        )
        with self.assertRaises(MOD.LauncherError) as raised:
            self.launcher.run_tests()
        self.assertEqual(raised.exception.stage, "test")
        self.assertEqual(
            self.launcher.tests["required_test_results"],
            {
                REQUIRED_INTEGRATION_TEST: "pass",
                T020_REQUIRED_INTEGRATION_TEST: "missing",
            },
        )

    def test_go_environment_cgo_gomax_and_offline_test_env(self):
        self.args.download_modules = True
        online = MOD.Launcher(self.args)
        self.assertEqual(online.go_env["CGO_ENABLED"], "1")
        self.assertEqual(online.go_env["GOMAXPROCS"], "2")
        self.assertTrue(online.go_env["GOPROXY"].startswith("https://"))
        self.assertEqual(online.offline_go_env["GOPROXY"], "off")
        self.assertEqual(online.offline_go_env["GOSUMDB"], "off")

    def test_run_timeout_records_124_and_timed_out(self):
        with mock.patch.object(
            MOD.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["cmd"], 1),
        ):
            with self.assertRaises(MOD.LauncherError) as raised:
                self.launcher._run(
                    "environment", "test", "cmd", ["cmd"], {}, 1
                )
        self.assertEqual(raised.exception.stage, "environment")
        self.assertEqual(raised.exception.kind, "timeout")
        self.assertEqual(self.launcher.commands[-1]["exit_code"], 124)
        self.assertTrue(self.launcher.commands[-1]["timed_out"])

    def test_process_group_timeout_kills_group_and_reaps(self):
        fake = FakeProcess()
        with mock.patch.object(MOD.subprocess, "Popen", return_value=fake), mock.patch.object(
            MOD.os, "killpg"
        ) as killpg:
            with self.assertRaises(MOD.LauncherError) as raised:
                self.launcher._run_process_group(
                    "test", "test", "go test", ["go", "test"], {}, 1
                )
        self.assertEqual(raised.exception.kind, "timeout")
        killpg.assert_called_once_with(fake.pid, signal.SIGKILL)
        self.assertEqual(self.launcher.commands[-1]["exit_code"], 124)
        self.assertTrue(self.launcher.commands[-1]["timed_out"])

    def test_tool_create_failure_recovers_exact_registered_container(self):
        self.launcher._assert_container_name_absent = mock.Mock()
        self.launcher._run = mock.Mock(
            side_effect=MOD.LauncherError("tool", "container-create", "failed")
        )

        def recover(record):
            record["id"] = "a" * 64
            record["created"] = True
            return record["id"]

        self.launcher._recover_intended_container = mock.Mock(side_effect=recover)
        self.launcher._stop_remove_container = mock.Mock()
        with self.assertRaises(MOD.LauncherError):
            self.launcher._run_tool_container(
                "migration-up", "sha256:" + "b" * 64, ["up"], 60, expect_success=True
            )
        self.assertEqual(len(self.launcher.tool_containers), 1)
        record = self.launcher.tool_containers[0]
        self.assertTrue(record["name"].startswith("ph-db-"))
        self.assertEqual(record["run_id"], self.launcher.run_id)
        self.assertEqual(record["scope"], "db-foundation")
        self.launcher._stop_remove_container.assert_called_once()

    def test_tool_start_wait_share_one_deadline_and_timeout_removes(self):
        self.launcher._assert_container_name_absent = mock.Mock()
        self.launcher._run = mock.Mock(
            side_effect=[
                completed(["docker", "create"], 0, "a" * 64 + "\n"),
                completed(["docker", "start"], 0, ""),
            ]
        )
        self.launcher._stop_remove_container = mock.Mock()
        with mock.patch.object(
            MOD.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["docker", "wait"], 60),
        ):
            with self.assertRaises(MOD.LauncherError) as raised:
                self.launcher._run_tool_container(
                    "migration-up", "sha256:" + "b" * 64, ["up"], 60, expect_success=True
                )
        self.assertEqual(raised.exception.kind, "timeout")
        self.launcher._stop_remove_container.assert_called_once()
        self.assertEqual(self.launcher.tool_containers[0]["wait_client_exit"], 124)
        self.assertIsNone(self.launcher.tool_containers[0]["container_exit_code"])

    def test_stop_remove_refuses_label_mismatch(self):
        self.launcher._inspect_container = mock.Mock(
            return_value={
                "Name": "/different",
                "Config": {
                    "Labels": {
                        "payment-hub.run-id": self.launcher.run_id,
                        "payment-hub.scope": "db-foundation",
                    }
                },
            }
        )
        self.launcher._run = mock.Mock()
        record = {"stage": "x", "name": "expected", "id": "a" * 64}
        self.launcher._stop_remove_container("a" * 64, "cleanup", record)
        self.launcher._run.assert_not_called()
        self.assertIn("cleanup:container-identity", self.launcher.cleanup_failures)

    def test_cleanup_collection_failure_is_not_green(self):
        self.launcher.compose_attempted = True
        self.launcher._collect_compose_resources = mock.Mock(
            side_effect=MOD.LauncherError("cleanup", "compose-collect", "failed")
        )
        self.launcher._run = mock.Mock(return_value=completed(["compose", "down"], 0))
        self.launcher._list_ids = mock.Mock(return_value=[])
        self.launcher._exact_network_names = mock.Mock(return_value=[])
        self.launcher.cleanup_safely()
        self.assertEqual(self.launcher.cleanup["result"], "fail")
        self.assertIn("cleanup:compose-collect", self.launcher.cleanup["errors"])

    def test_volume_not_recorded_is_not_deleted(self):
        self.launcher.compose_attempted = True
        self.launcher.compose_created = {
            "containers": [],
            "networks": [],
            "network": None,
            "volumes": [],
            "volume": None,
        }
        self.launcher.args.cleanup_owned_resources = True
        self.launcher._collect_compose_resources = mock.Mock(return_value=self.launcher.compose_created)
        self.launcher._run = mock.Mock(return_value=completed(["compose", "down"], 0))
        self.launcher._list_ids = mock.Mock(return_value=[])
        self.launcher._exact_network_names = mock.Mock(return_value=[])
        self.launcher._exact_volume_names = mock.Mock(return_value=[self.launcher.volume_name])
        self.launcher.cleanup_safely()
        self.assertEqual(self.launcher.cleanup["result"], "fail")
        self.assertIn("cleanup:volume-not-recorded", self.launcher.cleanup["errors"])
        rm_calls = [
            call
            for call in self.launcher._run.call_args_list
            if len(call.args) >= 2 and "volume" in str(call.args[1])
        ]
        self.assertEqual(rm_calls, [])

    def test_volume_identity_mismatch_is_not_deleted(self):
        self.launcher._exact_volume_names = mock.Mock(return_value=[self.launcher.volume_name])
        self.launcher._run = mock.Mock(
            return_value=completed(
                ["docker", "volume", "inspect"],
                0,
                json.dumps(
                    {
                        "Name": self.launcher.volume_name,
                        "Labels": {"com.docker.compose.project": "wrong"},
                    }
                ),
            )
        )
        result = {}
        self.launcher._remove_owned_volume(result)
        self.assertIn("cleanup:volume-identity", self.launcher.cleanup_failures)
        self.assertEqual(self.launcher._run.call_count, 1)

    def test_collect_compose_resources_records_actual_project_objects(self):
        self.launcher._list_ids = mock.Mock(
            side_effect=[
                ["c" * 64],
                [self.launcher.network_name],
                [self.launcher.volume_name],
            ]
        )
        created = self.launcher._collect_compose_resources()
        self.assertEqual(created["containers"], ["c" * 64])
        self.assertEqual(created["network"], self.launcher.network_name)
        self.assertEqual(created["volume"], self.launcher.volume_name)

    def test_logs_collection_failure_does_not_pass_negative_case(self):
        self.launcher._assert_container_name_absent = mock.Mock()
        self.launcher._run = mock.Mock(
            side_effect=[
                completed(["docker", "create"], 0, "a" * 64 + "\n"),
                completed(["docker", "start"], 0, ""),
                MOD.LauncherError("negative-dsn", "container-logs", "logs failed"),
            ]
        )
        self.launcher._stop_remove_container = mock.Mock()
        with mock.patch.object(
            MOD.subprocess,
            "run",
            return_value=completed(["docker", "wait"], 0, "1\n"),
        ):
            with self.assertRaises(MOD.LauncherError) as raised:
                self.launcher._run_tool_container(
                    "negative-dsn",
                    "sha256:" + "b" * 64,
                    ["up"],
                    60,
                    expect_success=False,
                )
        self.assertEqual(raised.exception.kind, "container-logs")
        self.launcher._stop_remove_container.assert_called_once()

    def test_wait_client_and_container_exit_are_recorded_separately(self):
        self.launcher._assert_container_name_absent = mock.Mock()
        self.launcher._run = mock.Mock(
            side_effect=[
                completed(["docker", "create"], 0, "a" * 64 + "\n"),
                completed(["docker", "start"], 0, ""),
                completed(["docker", "logs"], 0, '{"event":"migration","command":"up","status":"started"}\n{"event":"migration","command":"up","status":"failed"}\n'),
            ]
        )
        self.launcher._stop_remove_container = mock.Mock()
        with mock.patch.object(
            MOD.subprocess,
            "run",
            return_value=completed(["docker", "wait"], 0, "1\n"),
        ):
            exit_code, _ = self.launcher._run_tool_container(
                "negative-dsn",
                "sha256:" + "b" * 64,
                ["up"],
                60,
                expect_success=False,
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(self.launcher.tool_containers[0]["wait_client_exit"], 0)
        self.assertEqual(self.launcher.tool_containers[0]["container_exit_code"], 1)

    def test_source_manifest_unchanged_after_collection(self):
        fixture = self._write_manifest_fixture(
            Path(self.temp.name) / "manifest-unchanged-fixture"
        )
        self.launcher.repo = fixture
        before = self.launcher.collect_source_manifest()
        self.launcher.verify_source_manifest()
        self.assertEqual(self.launcher.source["manifest_digest"], before)
        self.assertTrue(self.launcher.source["manifest_unchanged"])

    def test_compose_ps_uses_compose_env_not_docker_env(self):
        self.launcher.docker = "/bin/true"
        self.launcher.docker_env = {"DOCKER_ENV_ONLY": "docker"}
        self.launcher.compose_env = {"COMPOSE_ENV_ONLY": "compose"}
        responses = [
            completed(["docker", "compose", "up"], 0, ""),
            completed(["docker", "compose", "ps"], 0, "a" * 64 + "\n"),
            completed(
                ["docker", "inspect", "labels"],
                0,
                json.dumps(
                    {
                        "com.docker.compose.project": self.launcher.project,
                        "com.docker.compose.service": "postgres",
                    }
                ),
            ),
            completed(["docker", "compose", "port"], 0, "127.0.0.1:12345\n"),
            completed(
                ["docker", "volume", "inspect"],
                0,
                json.dumps(
                    {
                        "Name": self.launcher.volume_name,
                        "Labels": {
                            "com.docker.compose.project": self.launcher.project
                        },
                    }
                ),
            ),
            completed(
                ["docker", "network", "inspect"],
                0,
                json.dumps(
                    {
                        "Name": self.launcher.network_name,
                        "Labels": {
                            "com.docker.compose.project": self.launcher.project
                        },
                    }
                ),
            ),
        ]
        self.launcher._run = mock.Mock(side_effect=responses)
        self.launcher.start_compose()
        ps_calls = [
            call
            for call in self.launcher._run.call_args_list
            if "ps" in call.args[3]
        ]
        self.assertEqual(len(ps_calls), 1)
        self.assertIs(ps_calls[0].args[4], self.launcher.compose_env)
        self.assertIsNot(ps_calls[0].args[4], self.launcher.docker_env)

    def test_preexisting_exact_container_name_is_not_removed(self):
        self.launcher._run = mock.Mock(return_value=completed(["docker", "ps"], 0, "d" * 64 + "\n"))
        with self.assertRaises(MOD.LauncherError) as raised:
            self.launcher._assert_container_name_absent("expected-name")
        self.assertEqual(raised.exception.stage, "environment")
        self.assertEqual(raised.exception.kind, "resource-conflict")
        self.launcher._run.assert_called_once()

    def test_tool_protocol_rejects_extra_keys_and_raw_lines(self):
        self.launcher._parse_tool_events(
            '{"event":"migration","command":"up","status":"started"}\n'
            '{"event":"migration","command":"up","status":"ok"}',
            "up",
            ["started", "ok"],
        )
        with self.assertRaises(MOD.LauncherError):
            self.launcher._parse_tool_events(
                '{"event":"migration","command":"up","status":"ok","extra":1}',
                "up",
                ["ok"],
            )
        with self.assertRaises(MOD.LauncherError):
            self.launcher._parse_tool_events("CREATE TABLE secret", "up", ["ok"])

    def test_test_output_requires_three_real_ok_lines(self):
        results, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT + both_required_pass_output(),
            "",
            0,
        )
        self.assertEqual(problems, [])
        self.assertEqual(results["internal/postgres"]["status"], "pass")
        self.assertEqual(results["tests/harness"]["duration"], "0.3s")
        _, empty_problems = self.launcher._parse_test_output("", "", 0)
        self.assertIn("package-not-pass", empty_problems)
        _, skip_problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT
            + both_required_pass_output()
            + "--- SKIP: TestOne (0.00s)\n",
            "",
            0,
        )
        self.assertIn("skip", skip_problems)

    def test_test_failure_diagnostic_is_controlled(self):
        diagnostics = self.launcher._extract_diagnostics(
            "--- FAIL: TestPaymentOrdersSchema (0.00s)\n"
            "    schema_test.go:42: expected SQLSTATE 23514 rejection, got SQLSTATE 23505\n"
        )
        self.assertEqual(diagnostics[0]["test"], "TestPaymentOrdersSchema")
        self.assertEqual(diagnostics[0]["location"], "schema_test.go:42")
        self.assertEqual(diagnostics[0]["category"], "sqlstate")
        self.assertEqual(diagnostics[0]["expected"], "23514")
        self.assertEqual(diagnostics[0]["got"], "23505")
        self.assertNotIn("CREATE", json.dumps(diagnostics))

    def test_fixture_rowcounts_require_three_zeros(self):
        self.launcher.container_id = "c" * 64
        self.launcher._run_with_stdin = mock.Mock(
            return_value=completed(["psql"], 0, "0\n0\n0\n")
        )
        self.launcher.verify_fixture_cleanup()
        self.assertEqual(self.launcher.tests["fixture_rowcounts"], [0, 0, 0])
        self.assertEqual(self.launcher.tests["fixture_cleanup"], "pass")
        self.launcher._run_with_stdin = mock.Mock(
            return_value=completed(["psql"], 0, "0\n1\n0\n")
        )
        with self.assertRaises(MOD.LauncherError):
            self.launcher.verify_fixture_cleanup()

    def test_repeat_signal_during_cleanup_records_failure(self):
        self.launcher.repeat_signal = True
        self.launcher.compose_attempted = False
        self.launcher.args.cleanup_owned_resources = False
        self.launcher.cleanup_safely()
        self.assertEqual(self.launcher.failure["stage"], "interrupted")
        self.assertEqual(self.launcher.failure["kind"], "repeat-signal")
        self.assertEqual(self.launcher.cleanup["result"], "pass")

    def test_main_catches_controlled_interruption_and_finishes(self):
        fake = mock.Mock()
        fake.run.side_effect = MOD.LauncherInterrupted("sigterm")
        fake.finish = mock.Mock()
        fake._result_pass.return_value = False
        fake.running = False
        fake.cleanup_started = False
        fake.repeat_signal = False
        fake.failure = None
        with mock.patch.object(MOD, "Launcher", return_value=fake), mock.patch.object(
            MOD.signal, "signal"
        ):
            code = MOD.main(
                [
                    "--go",
                    "/bin/true",
                    "--cache-dir",
                    str(Path(self.temp.name) / "cache2"),
                ]
            )
        self.assertEqual(code, 1)
        fake.finish.assert_called_once()
        self.assertEqual(fake.failure["stage"], "interrupted")
        self.assertEqual(fake.failure["kind"], "sigterm")


if __name__ == "__main__":
    unittest.main(verbosity=2)
