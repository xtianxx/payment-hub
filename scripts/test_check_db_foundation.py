"""Mock-only regression tests for scripts/check-db-foundation.py.

These tests never start Docker, PostgreSQL, Goose, Go, or the actual launcher
entrypoint. They use mocked subprocesses and standard-library temporary
directories (TMPDIR when set); they are not database acceptance evidence.
"""

import argparse
import ast
import json
import math
import shutil
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import importlib.util

try:
    import yaml
except ImportError:  # pragma: no cover - the CI image provides PyYAML
    yaml = None

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "check_db_foundation", REPO / "scripts/check-db-foundation.py"
)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)

# Read-only import of the setup checker; its main guard keeps execution off.
SETUP_SPEC = importlib.util.spec_from_file_location(
    "check_setup", REPO / "scripts/check-setup.py"
)
SETUP_MOD = importlib.util.module_from_spec(SETUP_SPEC)
SETUP_SPEC.loader.exec_module(SETUP_MOD)

CI_SPEC = importlib.util.spec_from_file_location(
    "ci_checks_for_launcher_test", REPO / "scripts/ci_checks.py"
)
CI_MOD = importlib.util.module_from_spec(CI_SPEC)
CI_SPEC.loader.exec_module(CI_MOD)


REQUIRED_INTEGRATION_TEST = "TestCommitHookIntegrationHidesReplyAfterRealCommit"
T020_REQUIRED_INTEGRATION_TEST = "TestHarnessProcessPreservesParentStateIntegration"
T022_REQUIRED_INTEGRATION_TEST = "TestApplicationLifecycleIntegration"
REQUIRED_SUBTEST = "TestApplicationLifecycleIntegration/startup_diagnostics"
T023_REQUIRED_INTEGRATION_TEST = "TestOrderRegistrationIntegration"
T024_REQUIRED_INTEGRATION_TEST = "TestObservationAtomicSaveIntegration"
T025_REQUIRED_INTEGRATION_TEST = "TestMockTransactionIntegration"
ORDER_LOOKUP_REQUIRED_INTEGRATION_TEST = "TestOrderLookupIntegration"
TEST_US1_PAYMENT = "TestUS1Payment"
TEST_US1_RESTART = "TestUS1Restart"
TEST_US1_HTTP_BOUNDARY = "TestUS1HTTPBoundary"
US2_ORDER_CONCURRENCY = "TestOrderRegistrationConcurrencyIntegration"
US2_MOCK_IDEMPOTENCY = "TestMockIdempotencyIntegration"
TEST_US2_REPLAY = "TestUS2Replay"
TEST_US2_CONFLICT = "TestUS2Conflict"
TEST_US2_CONCURRENCY = "TestUS2Concurrency"
TEST_US2_RESTART = "TestUS2Restart"
TEST_US2_REPORT_SAFETY = "TestUS2ReportSafety"
US3_OBSERVATION_FAULT = "TestObservationFaultIntegration"
TEST_US3_QUERY = "TestUS3Query"
TEST_US3_PROTECTION = "TestUS3Protection"
TEST_US3_RESTART = "TestUS3Restart"
TEST_US3_LIFECYCLE = "TestUS3Lifecycle"
TEST_US3_REPORT_SAFETY = "TestUS3ReportSafety"
CROSS_ENTRY_SECURITY = "TestCrossEntrySecurity"
HTTP_DEMO = "TestHTTPDemo"
HARNESS_PACKAGE = "payment-hub/tests/harness"
TESTS_PACKAGE = "payment-hub/tests"
POSTGRES_PACKAGE = "payment-hub/internal/postgres"
MOCK_PACKAGE = "payment-hub/internal/mock"

REQUIRED_TEST_PACKAGE = {
    REQUIRED_INTEGRATION_TEST: HARNESS_PACKAGE,
    T020_REQUIRED_INTEGRATION_TEST: HARNESS_PACKAGE,
    T022_REQUIRED_INTEGRATION_TEST: HARNESS_PACKAGE,
    REQUIRED_SUBTEST: HARNESS_PACKAGE,
    T023_REQUIRED_INTEGRATION_TEST: POSTGRES_PACKAGE,
    T024_REQUIRED_INTEGRATION_TEST: POSTGRES_PACKAGE,
    T025_REQUIRED_INTEGRATION_TEST: MOCK_PACKAGE,
    ORDER_LOOKUP_REQUIRED_INTEGRATION_TEST: POSTGRES_PACKAGE,
    TEST_US1_PAYMENT: TESTS_PACKAGE,
    TEST_US1_RESTART: TESTS_PACKAGE,
    TEST_US1_HTTP_BOUNDARY: TESTS_PACKAGE,
    US2_ORDER_CONCURRENCY: POSTGRES_PACKAGE,
    US2_MOCK_IDEMPOTENCY: MOCK_PACKAGE,
    TEST_US2_REPLAY: TESTS_PACKAGE,
    TEST_US2_CONFLICT: TESTS_PACKAGE,
    TEST_US2_CONCURRENCY: TESTS_PACKAGE,
    TEST_US2_RESTART: TESTS_PACKAGE,
    TEST_US2_REPORT_SAFETY: TESTS_PACKAGE,
    US3_OBSERVATION_FAULT: POSTGRES_PACKAGE,
    TEST_US3_QUERY: TESTS_PACKAGE,
    TEST_US3_PROTECTION: TESTS_PACKAGE,
    TEST_US3_RESTART: TESTS_PACKAGE,
    TEST_US3_LIFECYCLE: TESTS_PACKAGE,
    TEST_US3_REPORT_SAFETY: TESTS_PACKAGE,
    CROSS_ENTRY_SECURITY: TESTS_PACKAGE,
    HTTP_DEMO: TESTS_PACKAGE,
}


def json_event(action, package, test=None, elapsed=None, output=None):
    event = {"Action": action, "Package": package}
    if test is not None:
        event["Test"] = test
    if elapsed is not None:
        event["Elapsed"] = elapsed
    if output is not None:
        event["Output"] = output
    event["Time"] = "2026-11-08T00:00:00Z"
    return json.dumps(event, separators=(",", ":")) + "\n"


PACKAGE_OK_OUTPUT = (
    json_event("start", POSTGRES_PACKAGE)
    + json_event("run", POSTGRES_PACKAGE, "TestPackageHealth")
    + json_event("pass", POSTGRES_PACKAGE, "TestPackageHealth", elapsed=0.01)
    + json_event("pass", POSTGRES_PACKAGE, elapsed=0.1)
    + json_event("start", MOCK_PACKAGE)
    + json_event("run", MOCK_PACKAGE, "TestPackageHealth")
    + json_event("pass", MOCK_PACKAGE, "TestPackageHealth", elapsed=0.01)
    + json_event("pass", MOCK_PACKAGE, elapsed=0.2)
    + json_event("start", HARNESS_PACKAGE)
    + json_event("run", HARNESS_PACKAGE, "TestPackageHealth")
    + json_event("pass", HARNESS_PACKAGE, "TestPackageHealth", elapsed=0.01)
    + json_event("pass", HARNESS_PACKAGE, elapsed=0.3)
    + json_event("start", TESTS_PACKAGE)
    + json_event("run", TESTS_PACKAGE, "TestPackageHealth")
    + json_event("pass", TESTS_PACKAGE, "TestPackageHealth", elapsed=0.01)
    + json_event("pass", TESTS_PACKAGE, elapsed=0.4)
)


def required_root_pass_output(name=REQUIRED_INTEGRATION_TEST):
    package = REQUIRED_TEST_PACKAGE[name]
    return (
        json_event("run", package, name)
        + json_event("pass", package, name, elapsed=0.01)
    )


def required_pass_output(name=REQUIRED_INTEGRATION_TEST):
    if name == T022_REQUIRED_INTEGRATION_TEST:
        package = REQUIRED_TEST_PACKAGE[name]
        return (
            json_event("run", package, name)
            + json_event("run", package, REQUIRED_SUBTEST)
            + json_event("pass", package, REQUIRED_SUBTEST, elapsed=0.01)
            + json_event("pass", package, name, elapsed=0.02)
        )
    return required_root_pass_output(name)


def required_skip_output(name=REQUIRED_INTEGRATION_TEST):
    package = REQUIRED_TEST_PACKAGE[name]
    return (
        json_event("run", package, name)
        + json_event("skip", package, name)
    )


def required_fail_output(name=REQUIRED_INTEGRATION_TEST):
    package = REQUIRED_TEST_PACKAGE[name]
    return (
        json_event("run", package, name)
        + json_event("fail", package, name)
    )


def required_subtest_skip_output():
    return (
        json_event("run", HARNESS_PACKAGE, T022_REQUIRED_INTEGRATION_TEST)
        + json_event("run", HARNESS_PACKAGE, REQUIRED_SUBTEST)
        + json_event("skip", HARNESS_PACKAGE, REQUIRED_SUBTEST)
        + json_event("pass", HARNESS_PACKAGE, T022_REQUIRED_INTEGRATION_TEST, elapsed=0.02)
    )


def required_subtest_fail_output():
    return (
        json_event("run", HARNESS_PACKAGE, T022_REQUIRED_INTEGRATION_TEST)
        + json_event("run", HARNESS_PACKAGE, REQUIRED_SUBTEST)
        + json_event("fail", HARNESS_PACKAGE, REQUIRED_SUBTEST)
        + json_event("fail", HARNESS_PACKAGE, T022_REQUIRED_INTEGRATION_TEST)
    )


def both_required_pass_output():
    return required_pass_output(REQUIRED_INTEGRATION_TEST) + required_pass_output(
        T020_REQUIRED_INTEGRATION_TEST
    )


def pr3_required_pass_output():
    return both_required_pass_output() + required_pass_output(
        T022_REQUIRED_INTEGRATION_TEST
    )


def us1_required_pass_output():
    return (
        required_pass_output(T023_REQUIRED_INTEGRATION_TEST)
        + required_pass_output(T024_REQUIRED_INTEGRATION_TEST)
        + required_pass_output(T025_REQUIRED_INTEGRATION_TEST)
        + required_pass_output(ORDER_LOOKUP_REQUIRED_INTEGRATION_TEST)
        + required_pass_output(TEST_US1_PAYMENT)
        + required_pass_output(TEST_US1_RESTART)
        + required_pass_output(TEST_US1_HTTP_BOUNDARY)
    )


def us2_required_pass_output():
    return (
        required_pass_output(US2_ORDER_CONCURRENCY)
        + required_pass_output(US2_MOCK_IDEMPOTENCY)
        + required_pass_output(TEST_US2_REPLAY)
        + required_pass_output(TEST_US2_CONFLICT)
        + required_pass_output(TEST_US2_CONCURRENCY)
        + required_pass_output(TEST_US2_RESTART)
        + required_pass_output(TEST_US2_REPORT_SAFETY)
    )


def us3_required_pass_output():
    return (
        required_pass_output(US3_OBSERVATION_FAULT)
        + required_pass_output(TEST_US3_QUERY)
        + required_pass_output(TEST_US3_PROTECTION)
        + required_pass_output(TEST_US3_RESTART)
        + required_pass_output(TEST_US3_LIFECYCLE)
        + required_pass_output(TEST_US3_REPORT_SAFETY)
    )


def all_required_pass_output():
    return (
        pr3_required_pass_output()
        + us1_required_pass_output()
        + us2_required_pass_output()
        + us3_required_pass_output()
        + required_pass_output(CROSS_ENTRY_SECURITY)
        + required_pass_output(HTTP_DEMO)
    )


def roots_only_required_pass_output():
    return (
        required_root_pass_output(REQUIRED_INTEGRATION_TEST)
        + required_root_pass_output(T020_REQUIRED_INTEGRATION_TEST)
        + required_root_pass_output(T022_REQUIRED_INTEGRATION_TEST)
        + required_root_pass_output(T023_REQUIRED_INTEGRATION_TEST)
        + required_root_pass_output(T024_REQUIRED_INTEGRATION_TEST)
        + required_root_pass_output(T025_REQUIRED_INTEGRATION_TEST)
        + required_root_pass_output(ORDER_LOOKUP_REQUIRED_INTEGRATION_TEST)
        + required_root_pass_output(TEST_US1_PAYMENT)
        + required_root_pass_output(TEST_US1_RESTART)
        + required_root_pass_output(TEST_US1_HTTP_BOUNDARY)
        + required_root_pass_output(US2_ORDER_CONCURRENCY)
        + required_root_pass_output(US2_MOCK_IDEMPOTENCY)
        + required_root_pass_output(TEST_US2_REPLAY)
        + required_root_pass_output(TEST_US2_CONFLICT)
        + required_root_pass_output(TEST_US2_CONCURRENCY)
        + required_root_pass_output(TEST_US2_RESTART)
        + required_root_pass_output(TEST_US2_REPORT_SAFETY)
        + required_root_pass_output(US3_OBSERVATION_FAULT)
        + required_root_pass_output(TEST_US3_QUERY)
        + required_root_pass_output(TEST_US3_PROTECTION)
        + required_root_pass_output(TEST_US3_RESTART)
        + required_root_pass_output(TEST_US3_LIFECYCLE)
        + required_root_pass_output(TEST_US3_REPORT_SAFETY)
        + required_root_pass_output(CROSS_ENTRY_SECURITY)
        + required_root_pass_output(HTTP_DEMO)
    )


def without_test_events(text, test_name):
    kept = []
    for line in text.splitlines():
        if not line:
            continue
        value = json.loads(line)
        if value.get("Test") == test_name:
            continue
        kept.append(line)
    return "\n".join(kept) + "\n"


def replace_test_terminal(text, package, test_name, action):
    replaced = []
    for line in text.splitlines():
        if not line:
            continue
        value = json.loads(line)
        if (
            value.get("Package") == package
            and value.get("Test") == test_name
            and value.get("Action") == "pass"
        ):
            value["Action"] = action
            value.pop("Elapsed", None)
        replaced.append(json.dumps(value, separators=(",", ":")))
    return "\n".join(replaced) + "\n"


def completed(argv, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


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


class SuccessProcess:
    """A successful process group whose communicate returns normally."""

    def __init__(self, stdout="", stderr="", pid=34567, returncode=0):
        self.pid = pid
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def communicate(self, timeout=None):
        return self._stdout, self._stderr

    def kill(self):
        return None

    def wait(self, timeout=None):
        return self.returncode


class PartialOutputProcess:
    """A timed-out process whose second communicate returns buffered output."""

    def __init__(self, stdout="", stderr="", pid=23456):
        self.pid = pid
        self.returncode = -9
        self.calls = 0
        self._stdout = stdout
        self._stderr = stderr

    def communicate(self, timeout=None):
        self.calls += 1
        if self.calls == 1:
            raise subprocess.TimeoutExpired("go test", timeout)
        return self._stdout, self._stderr

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
            PACKAGE_OK_OUTPUT + all_required_pass_output(), "", 0
        )
        self.assertEqual(problems, [])
        self.assertEqual(results["tests/harness"]["status"], "pass")

    def test_runtime_parser_rejects_duplicate_required_root_pass(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT
            + all_required_pass_output()
            + required_pass_output(REQUIRED_INTEGRATION_TEST),
            "",
            0,
        )
        self.assertIn("required-test-duplicate", problems)

    def test_runtime_parser_rejects_required_root_skip(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT
            + required_pass_output(T020_REQUIRED_INTEGRATION_TEST)
            + required_skip_output(REQUIRED_INTEGRATION_TEST),
            "",
            0,
        )
        self.assertIn("required-test-skip", problems)

    def test_runtime_parser_rejects_required_root_fail(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT
            + required_pass_output(T020_REQUIRED_INTEGRATION_TEST)
            + required_fail_output(REQUIRED_INTEGRATION_TEST),
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
                    PACKAGE_OK_OUTPUT + all_required_pass_output(),
                    "",
                ),
            ]
        )
        self.assertIn("tests", MOD.PACKAGES)
        self.launcher.run_tests()
        test_argv = self.launcher._run_process_group.call_args_list[1].args[3]
        self.assertIn("-json", test_argv)
        self.assertNotIn("-v", test_argv)
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
        self.assertIn("tests", MOD.PACKAGES)
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
            PACKAGE_OK_OUTPUT + all_required_pass_output(), "", 0
        )
        self.assertEqual(problems, [])

    def test_t020_parser_rejects_second_root_skip(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT
            + required_pass_output(REQUIRED_INTEGRATION_TEST)
            + required_skip_output(T020_REQUIRED_INTEGRATION_TEST),
            "",
            0,
        )
        self.assertIn("required-test-skip", problems)

    def test_t020_parser_rejects_second_root_fail(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT
            + required_pass_output(REQUIRED_INTEGRATION_TEST)
            + required_fail_output(T020_REQUIRED_INTEGRATION_TEST),
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
                    PACKAGE_OK_OUTPUT + all_required_pass_output(),
                    "",
                ),
            ]
        )
        self.assertIn("tests", MOD.PACKAGES)
        self.launcher.run_tests()
        test_argv = self.launcher._run_process_group.call_args_list[1].args[3]
        self.assertIn("-json", test_argv)
        self.assertNotIn("-v", test_argv)
        expected = {name: "pass" for name in REQUIRED_TEST_PACKAGE}
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
        self.assertIn("tests", MOD.PACKAGES)
        with self.assertRaises(MOD.LauncherError) as raised:
            self.launcher.run_tests()
        self.assertEqual(raised.exception.stage, "test")
        expected = {name: "missing" for name in REQUIRED_TEST_PACKAGE}
        expected[REQUIRED_INTEGRATION_TEST] = "pass"
        self.assertEqual(
            self.launcher.tests["required_test_results"],
            expected,
        )

    def test_t022_parser_requires_third_required_root(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT + both_required_pass_output(), "", 0
        )
        self.assertIn("required-test-missing", problems)

    def test_t022_parser_accepts_all_three_required_roots(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT + all_required_pass_output(), "", 0
        )
        self.assertEqual(problems, [])

    def test_t022_parser_rejects_third_root_skip(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT
            + both_required_pass_output()
            + required_skip_output(T022_REQUIRED_INTEGRATION_TEST),
            "",
            0,
        )
        self.assertIn("required-test-skip", problems)

    def test_t022_parser_rejects_third_root_fail(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT
            + both_required_pass_output()
            + required_fail_output(T022_REQUIRED_INTEGRATION_TEST),
            "",
            0,
        )
        self.assertIn("required-test-fail", problems)

    def test_t022_parser_rejects_third_root_duplicate(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT
            + both_required_pass_output()
            + required_root_pass_output(T022_REQUIRED_INTEGRATION_TEST)
            + required_root_pass_output(T022_REQUIRED_INTEGRATION_TEST),
            "",
            0,
        )
        self.assertIn("required-test-duplicate", problems)

    def test_t022_parser_keeps_old_roots_mandatory(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT
            + required_pass_output(T020_REQUIRED_INTEGRATION_TEST)
            + required_pass_output(T022_REQUIRED_INTEGRATION_TEST),
            "",
            0,
        )
        self.assertIn("required-test-missing", problems)
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT
            + required_pass_output(REQUIRED_INTEGRATION_TEST)
            + required_pass_output(T022_REQUIRED_INTEGRATION_TEST),
            "",
            0,
        )
        self.assertIn("required-test-missing", problems)

    def test_t022_run_tests_reports_all_three_required_roots(self):
        self.launcher.published_port = 54321
        self.launcher._run_process_group = mock.Mock(
            side_effect=[
                completed(["go", "vet"], 0, "", ""),
                completed(
                    ["go", "test"],
                    0,
                    PACKAGE_OK_OUTPUT + all_required_pass_output(),
                    "",
                ),
            ]
        )
        self.assertIn("tests", MOD.PACKAGES)
        self.launcher.run_tests()
        test_argv = self.launcher._run_process_group.call_args_list[1].args[3]
        self.assertIn("-json", test_argv)
        self.assertNotIn("-v", test_argv)
        expected = {name: "pass" for name in REQUIRED_TEST_PACKAGE}
        self.assertEqual(self.launcher.tests["required_test_results"], expected)
        self.assertEqual(
            self.launcher._build_report()["tests"]["required_test_results"],
            expected,
        )

    def test_t022_run_tests_rejects_only_old_two_roots(self):
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
        self.assertIn("tests", MOD.PACKAGES)
        with self.assertRaises(MOD.LauncherError) as raised:
            self.launcher.run_tests()
        self.assertEqual(raised.exception.stage, "test")
        expected = {name: "missing" for name in REQUIRED_TEST_PACKAGE}
        expected[REQUIRED_INTEGRATION_TEST] = "pass"
        expected[T020_REQUIRED_INTEGRATION_TEST] = "pass"
        self.assertEqual(
            self.launcher.tests["required_test_results"],
            expected,
        )

    def test_runtime_parser_rejects_missing_required_subtest(self):
        stdout = PACKAGE_OK_OUTPUT + roots_only_required_pass_output()
        required, problems = self.launcher._parse_required_test_results(stdout)
        self.assertEqual(required.get(REQUIRED_SUBTEST), "missing")
        self.assertIn("required-test-missing", problems)

    def test_runtime_parser_rejects_required_subtest_skip(self):
        stdout = (
            PACKAGE_OK_OUTPUT
            + roots_only_required_pass_output()
            + required_subtest_skip_output()
        )
        required, problems = self.launcher._parse_required_test_results(stdout)
        self.assertEqual(required.get(REQUIRED_SUBTEST), "skip")
        self.assertIn("required-test-skip", problems)

    def test_runtime_parser_rejects_required_subtest_fail(self):
        stdout = (
            PACKAGE_OK_OUTPUT
            + roots_only_required_pass_output()
            + required_subtest_fail_output()
        )
        required, problems = self.launcher._parse_required_test_results(stdout)
        self.assertEqual(required.get(REQUIRED_SUBTEST), "fail")
        self.assertIn("required-test-fail", problems)

    def test_runtime_parser_rejects_fake_output_subtest_pass(self):
        stdout = (
            PACKAGE_OK_OUTPUT
            + roots_only_required_pass_output()
            + json_event(
                "output",
                HARNESS_PACKAGE,
                output=(
                    "--- PASS: "
                    + REQUIRED_SUBTEST
                    + " (0.00s)\n"
                ),
            )
        )
        required, problems = self.launcher._parse_required_test_results(stdout)
        self.assertEqual(required.get(REQUIRED_SUBTEST), "missing")
        self.assertIn("required-test-missing", problems)

    def test_runtime_parser_rejects_cross_package_required_subtest(self):
        stdout = (
            PACKAGE_OK_OUTPUT
            + roots_only_required_pass_output()
            + json_event("run", MOCK_PACKAGE, REQUIRED_SUBTEST)
            + json_event("pass", MOCK_PACKAGE, REQUIRED_SUBTEST)
        )
        required, problems = self.launcher._parse_required_test_results(stdout)
        self.assertEqual(required.get(REQUIRED_SUBTEST), "wrong-package")
        self.assertIn("required-test-wrong-package", problems)

    def test_runtime_parser_rejects_package_suffix_lookalike(self):
        lookalike = "evil/payment-hub/internal/postgres"
        stdout = (
            json_event("start", lookalike)
            + json_event("run", lookalike, "TestOne")
            + json_event("pass", lookalike, "TestOne")
            + json_event("pass", lookalike, elapsed=0.1)
        )
        results, problems = self.launcher._parse_test_output(stdout, "", 0)
        self.assertNotEqual(results["internal/postgres"]["status"], "pass")
        self.assertIn("unexpected-package", problems)

    def test_runtime_parser_rejects_non_json_and_truncated(self):
        _, problems = self.launcher._parse_test_output("not json\n", "", 0)
        self.assertIn("non-json-output", problems)
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT[:-1], "", 0
        )
        self.assertIn("truncated-output", problems)

    def test_runtime_parser_rejects_nonzero_exit(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT + all_required_pass_output(), "", 1
        )
        self.assertIn("test-exit", problems)

    def test_required_package_bindings_are_centralized(self):
        expected = dict(REQUIRED_TEST_PACKAGE)
        self.assertEqual(MOD.REQUIRED_TEST_PACKAGES, expected)
        self.assertEqual(
            tuple(MOD.REQUIRED_TEST_PACKAGES),
            CI_MOD.DB_REQUIRED_TEST_KEYS,
        )
        self.assertEqual(
            MOD.EXPECTED_PACKAGE_PATHS,
            tuple("payment-hub/" + name for name in MOD.PACKAGES),
        )

    def test_http_demo_required_binding_uses_tests_root(self):
        self.assertEqual(MOD.REQUIRED_TEST_PACKAGES.get(HTTP_DEMO), TESTS_PACKAGE)
        self.assertNotEqual(
            MOD.REQUIRED_TEST_PACKAGES.get(HTTP_DEMO), HARNESS_PACKAGE
        )
        self.assertIn(HTTP_DEMO, CI_MOD.DB_REQUIRED_TEST_KEYS)

    def test_http_demo_required_root_mutations_are_not_green(self):
        stdout = PACKAGE_OK_OUTPUT + all_required_pass_output()
        with self.subTest(mutation="missing"):
            required, problems = self.launcher._parse_required_test_results(
                without_test_events(stdout, HTTP_DEMO)
            )
            self.assertEqual(required.get(HTTP_DEMO), "missing")
            self.assertIn("required-test-missing", problems)
        for action in ("skip", "fail"):
            with self.subTest(mutation=action):
                required, problems = (
                    self.launcher._parse_required_test_results(
                        replace_test_terminal(
                            stdout, TESTS_PACKAGE, HTTP_DEMO, action
                        )
                    )
                )
                self.assertEqual(required.get(HTTP_DEMO), action)
                self.assertIn("required-test-" + action, problems)
        with self.subTest(mutation="wrong-package"):
            wrong = without_test_events(stdout, HTTP_DEMO)
            wrong += json_event("run", HARNESS_PACKAGE, HTTP_DEMO)
            wrong += json_event(
                "pass", HARNESS_PACKAGE, HTTP_DEMO, elapsed=0.01
            )
            required, problems = self.launcher._parse_required_test_results(
                wrong
            )
            self.assertEqual(required.get(HTTP_DEMO), "wrong-package")
            self.assertIn("required-test-wrong-package", problems)
        with self.subTest(mutation="duplicate"):
            duplicate = stdout
            duplicate += json_event("run", TESTS_PACKAGE, HTTP_DEMO)
            duplicate += json_event(
                "pass", TESTS_PACKAGE, HTTP_DEMO, elapsed=0.01
            )
            required, _ = self.launcher._parse_required_test_results(
                duplicate
            )
            self.assertEqual(required.get(HTTP_DEMO), "duplicate")

    def test_us1_required_bindings_use_storage_and_mock_roots(self):
        self.assertEqual(
            MOD.REQUIRED_TEST_PACKAGES.get(T023_REQUIRED_INTEGRATION_TEST),
            POSTGRES_PACKAGE,
        )
        self.assertEqual(
            MOD.REQUIRED_TEST_PACKAGES.get(T024_REQUIRED_INTEGRATION_TEST),
            POSTGRES_PACKAGE,
        )
        self.assertEqual(
            MOD.REQUIRED_TEST_PACKAGES.get(T025_REQUIRED_INTEGRATION_TEST),
            MOCK_PACKAGE,
        )
        self.assertEqual(
            MOD.REQUIRED_TEST_PACKAGES.get(
                ORDER_LOOKUP_REQUIRED_INTEGRATION_TEST
            ),
            POSTGRES_PACKAGE,
        )
        for name in (TEST_US1_PAYMENT, TEST_US1_RESTART, TEST_US1_HTTP_BOUNDARY):
            with self.subTest(name=name):
                self.assertEqual(
                    MOD.REQUIRED_TEST_PACKAGES.get(name), TESTS_PACKAGE
                )
                self.assertNotEqual(
                    MOD.REQUIRED_TEST_PACKAGES.get(name), HARNESS_PACKAGE
                )
        for name in (
            T023_REQUIRED_INTEGRATION_TEST,
            T024_REQUIRED_INTEGRATION_TEST,
            T025_REQUIRED_INTEGRATION_TEST,
            ORDER_LOOKUP_REQUIRED_INTEGRATION_TEST,
            TEST_US1_PAYMENT,
            TEST_US1_RESTART,
            TEST_US1_HTTP_BOUNDARY,
        ):
            with self.subTest(name=name):
                self.assertNotEqual(
                    MOD.REQUIRED_TEST_PACKAGES.get(name), HARNESS_PACKAGE
                )
                self.assertIn(name, CI_MOD.DB_REQUIRED_TEST_KEYS)

    def test_us2_required_bindings_use_storage_mock_and_tests_roots(self):
        self.assertEqual(
            MOD.REQUIRED_TEST_PACKAGES.get(US2_ORDER_CONCURRENCY),
            POSTGRES_PACKAGE,
        )
        self.assertEqual(
            MOD.REQUIRED_TEST_PACKAGES.get(US2_MOCK_IDEMPOTENCY),
            MOCK_PACKAGE,
        )
        for name in (
            TEST_US2_REPLAY,
            TEST_US2_CONFLICT,
            TEST_US2_CONCURRENCY,
            TEST_US2_RESTART,
            TEST_US2_REPORT_SAFETY,
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    MOD.REQUIRED_TEST_PACKAGES.get(name), TESTS_PACKAGE
                )
                self.assertNotEqual(
                    MOD.REQUIRED_TEST_PACKAGES.get(name), HARNESS_PACKAGE
                )
        for name in (
            US2_ORDER_CONCURRENCY,
            US2_MOCK_IDEMPOTENCY,
            TEST_US2_REPLAY,
            TEST_US2_CONFLICT,
            TEST_US2_CONCURRENCY,
            TEST_US2_RESTART,
            TEST_US2_REPORT_SAFETY,
        ):
            with self.subTest(name=name):
                self.assertNotEqual(
                    MOD.REQUIRED_TEST_PACKAGES.get(name), HARNESS_PACKAGE
                )
                self.assertIn(name, CI_MOD.DB_REQUIRED_TEST_KEYS)

    def test_us2_parser_requires_new_storage_mock_and_tests_roots(self):
        required, problems = self.launcher._parse_required_test_results(
            PACKAGE_OK_OUTPUT + us1_required_pass_output()
        )
        for name in (
            US2_ORDER_CONCURRENCY,
            US2_MOCK_IDEMPOTENCY,
            TEST_US2_REPLAY,
            TEST_US2_CONFLICT,
            TEST_US2_CONCURRENCY,
            TEST_US2_RESTART,
            TEST_US2_REPORT_SAFETY,
        ):
            with self.subTest(name=name):
                self.assertEqual(required.get(name), "missing")
        self.assertIn("required-test-missing", problems)

    def test_us2_parser_accepts_all_twenty_six_required_roots(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT + all_required_pass_output(), "", 0
        )
        self.assertEqual(problems, [])

    def test_us2_parser_requires_each_new_root_individually(self):
        stdout = PACKAGE_OK_OUTPUT + all_required_pass_output()
        for name in (
            US2_ORDER_CONCURRENCY,
            US2_MOCK_IDEMPOTENCY,
            TEST_US2_REPLAY,
            TEST_US2_CONFLICT,
            TEST_US2_CONCURRENCY,
            TEST_US2_RESTART,
            TEST_US2_REPORT_SAFETY,
        ):
            with self.subTest(name=name):
                required, problems = self.launcher._parse_required_test_results(
                    without_test_events(stdout, name)
                )
                self.assertEqual(required.get(name), "missing")
                self.assertIn("required-test-missing", problems)

    def test_us2_parser_rejects_skip_fail_wrong_package_and_duplicate(self):
        for target, package in (
            (US2_ORDER_CONCURRENCY, POSTGRES_PACKAGE),
            (US2_MOCK_IDEMPOTENCY, MOCK_PACKAGE),
            (TEST_US2_REPLAY, TESTS_PACKAGE),
            (TEST_US2_CONFLICT, TESTS_PACKAGE),
            (TEST_US2_CONCURRENCY, TESTS_PACKAGE),
            (TEST_US2_RESTART, TESTS_PACKAGE),
            (TEST_US2_REPORT_SAFETY, TESTS_PACKAGE),
        ):
            for action in ("skip", "fail"):
                with self.subTest(target=target, action=action):
                    required, problems = (
                        self.launcher._parse_required_test_results(
                            replace_test_terminal(
                                PACKAGE_OK_OUTPUT
                                + all_required_pass_output(),
                                package,
                                target,
                                action,
                            )
                        )
                    )
                    self.assertEqual(required.get(target), action)
                    self.assertIn(
                        "required-test-" + action, problems
                    )
            with self.subTest(target=target, action="duplicate"):
                stdout = PACKAGE_OK_OUTPUT + all_required_pass_output()
                stdout += json_event("run", package, target)
                stdout += json_event("pass", package, target, elapsed=0.01)
                required, _ = self.launcher._parse_required_test_results(
                    stdout
                )
                self.assertEqual(required.get(target), "duplicate")
            with self.subTest(target=target, action="wrong-package"):
                stdout = without_test_events(
                    PACKAGE_OK_OUTPUT + all_required_pass_output(), target
                )
                stdout += json_event("run", HARNESS_PACKAGE, target)
                stdout += json_event(
                    "pass", HARNESS_PACKAGE, target, elapsed=0.01
                )
                required, problems = (
                    self.launcher._parse_required_test_results(stdout)
                )
                self.assertEqual(required.get(target), "wrong-package")
                self.assertIn("required-test-wrong-package", problems)
            with self.subTest(target=target, action="output-spoof"):
                stdout = without_test_events(
                    PACKAGE_OK_OUTPUT + all_required_pass_output(), target
                )
                stdout += json_event(
                    "output",
                    package,
                    test=target,
                    output="--- PASS: " + target + " (0.00s)\n",
                )
                required, problems = (
                    self.launcher._parse_required_test_results(stdout)
                )
                self.assertEqual(required.get(target), "missing")
                self.assertIn("required-test-missing", problems)

    def test_us3_parser_requires_new_roots(self):
        required, problems = self.launcher._parse_required_test_results(
            PACKAGE_OK_OUTPUT + us2_required_pass_output()
        )
        for name in (
            US3_OBSERVATION_FAULT,
            TEST_US3_QUERY,
            TEST_US3_PROTECTION,
            TEST_US3_RESTART,
            TEST_US3_LIFECYCLE,
            TEST_US3_REPORT_SAFETY,
        ):
            with self.subTest(name=name):
                self.assertEqual(required.get(name), "missing")
        self.assertIn("required-test-missing", problems)

    def test_us3_parser_requires_each_new_root_individually(self):
        stdout = PACKAGE_OK_OUTPUT + all_required_pass_output()
        for name in (
            US3_OBSERVATION_FAULT,
            TEST_US3_QUERY,
            TEST_US3_PROTECTION,
            TEST_US3_RESTART,
            TEST_US3_LIFECYCLE,
            TEST_US3_REPORT_SAFETY,
        ):
            with self.subTest(name=name):
                required, problems = self.launcher._parse_required_test_results(
                    without_test_events(stdout, name)
                )
                self.assertEqual(required.get(name), "missing")
                self.assertIn("required-test-missing", problems)

    def test_us3_parser_rejects_skip_fail_wrong_package_and_duplicate(self):
        for target in (
            US3_OBSERVATION_FAULT,
            TEST_US3_QUERY,
            TEST_US3_PROTECTION,
            TEST_US3_RESTART,
            TEST_US3_LIFECYCLE,
            TEST_US3_REPORT_SAFETY,
        ):
            package = REQUIRED_TEST_PACKAGE[target]
            for action in ("skip", "fail"):
                with self.subTest(target=target, action=action):
                    required, problems = (
                        self.launcher._parse_required_test_results(
                            replace_test_terminal(
                                PACKAGE_OK_OUTPUT
                                + all_required_pass_output(),
                                package,
                                target,
                                action,
                            )
                        )
                    )
                    self.assertEqual(required.get(target), action)
                    self.assertIn("required-test-" + action, problems)
            with self.subTest(target=target, action="duplicate"):
                stdout = PACKAGE_OK_OUTPUT + all_required_pass_output()
                stdout += json_event("run", package, target)
                stdout += json_event("pass", package, target, elapsed=0.01)
                required, _ = self.launcher._parse_required_test_results(
                    stdout
                )
                self.assertEqual(required.get(target), "duplicate")
            with self.subTest(target=target, action="wrong-package"):
                wrong_package = (
                    HARNESS_PACKAGE
                    if package != HARNESS_PACKAGE
                    else POSTGRES_PACKAGE
                )
                stdout = without_test_events(
                    PACKAGE_OK_OUTPUT + all_required_pass_output(), target
                )
                stdout += json_event("run", wrong_package, target)
                stdout += json_event(
                    "pass", wrong_package, target, elapsed=0.01
                )
                required, problems = (
                    self.launcher._parse_required_test_results(stdout)
                )
                self.assertEqual(required.get(target), "wrong-package")
                self.assertIn("required-test-wrong-package", problems)
            with self.subTest(target=target, action="output-spoof"):
                stdout = without_test_events(
                    PACKAGE_OK_OUTPUT + all_required_pass_output(), target
                )
                stdout += json_event(
                    "output",
                    package,
                    test=target,
                    output="--- PASS: " + target + " (0.00s)\n",
                )
                required, problems = (
                    self.launcher._parse_required_test_results(stdout)
                )
                self.assertEqual(required.get(target), "missing")
                self.assertIn("required-test-missing", problems)

    def test_cross_entry_security_required_binding_and_mutations(self):
        target = CROSS_ENTRY_SECURITY
        self.assertEqual(MOD.REQUIRED_TEST_PACKAGES.get(target), TESTS_PACKAGE)
        self.assertIn(target, CI_MOD.DB_REQUIRED_TEST_KEYS)
        required, problems = self.launcher._parse_required_test_results(
            PACKAGE_OK_OUTPUT + all_required_pass_output()
        )
        self.assertEqual(problems, [])
        self.assertEqual(required.get(target), "pass")
        required, problems = self.launcher._parse_required_test_results(
            without_test_events(
                PACKAGE_OK_OUTPUT + all_required_pass_output(), target
            )
        )
        self.assertEqual(required.get(target), "missing")
        self.assertIn("required-test-missing", problems)
        for action in ("skip", "fail"):
            with self.subTest(action=action):
                required, problems = (
                    self.launcher._parse_required_test_results(
                        replace_test_terminal(
                            PACKAGE_OK_OUTPUT + all_required_pass_output(),
                            TESTS_PACKAGE,
                            target,
                            action,
                        )
                    )
                )
                self.assertEqual(required.get(target), action)
                self.assertIn("required-test-" + action, problems)
        with self.subTest(action="wrong-package"):
            stdout = without_test_events(
                PACKAGE_OK_OUTPUT + all_required_pass_output(), target
            )
            stdout += json_event("run", HARNESS_PACKAGE, target)
            stdout += json_event(
                "pass", HARNESS_PACKAGE, target, elapsed=0.01
            )
            required, problems = self.launcher._parse_required_test_results(
                stdout
            )
            self.assertEqual(required.get(target), "wrong-package")
            self.assertIn("required-test-wrong-package", problems)
        with self.subTest(action="duplicate"):
            stdout = PACKAGE_OK_OUTPUT + all_required_pass_output()
            stdout += json_event("run", TESTS_PACKAGE, target)
            stdout += json_event(
                "pass", TESTS_PACKAGE, target, elapsed=0.01
            )
            required, _ = self.launcher._parse_required_test_results(stdout)
            self.assertEqual(required.get(target), "duplicate")

    def test_us3_required_bindings_use_storage_and_tests_roots(self):
        self.assertEqual(
            MOD.REQUIRED_TEST_PACKAGES.get(US3_OBSERVATION_FAULT),
            POSTGRES_PACKAGE,
        )
        for name in (
            TEST_US3_QUERY,
            TEST_US3_PROTECTION,
            TEST_US3_RESTART,
            TEST_US3_LIFECYCLE,
            TEST_US3_REPORT_SAFETY,
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    MOD.REQUIRED_TEST_PACKAGES.get(name), TESTS_PACKAGE
                )
                self.assertNotEqual(
                    MOD.REQUIRED_TEST_PACKAGES.get(name), HARNESS_PACKAGE
                )
                self.assertIn(name, CI_MOD.DB_REQUIRED_TEST_KEYS)

    def test_us1_parser_requires_new_storage_and_mock_roots(self):
        required, problems = self.launcher._parse_required_test_results(
            PACKAGE_OK_OUTPUT + pr3_required_pass_output()
        )
        for name in (
            T023_REQUIRED_INTEGRATION_TEST,
            T024_REQUIRED_INTEGRATION_TEST,
            T025_REQUIRED_INTEGRATION_TEST,
            ORDER_LOOKUP_REQUIRED_INTEGRATION_TEST,
            TEST_US1_PAYMENT,
            TEST_US1_RESTART,
            TEST_US1_HTTP_BOUNDARY,
        ):
            with self.subTest(name=name):
                self.assertEqual(required.get(name), "missing")
        self.assertIn("required-test-missing", problems)

    def test_us1_parser_accepts_all_eleven_required_roots(self):
        # Historical US1 11-entry subset remains independently green in the
        # larger 18-entry launcher mapping.
        required, problems = self.launcher._parse_required_test_results(
            PACKAGE_OK_OUTPUT + all_required_pass_output()
        )
        self.assertEqual(problems, [])
        for name in (
            REQUIRED_INTEGRATION_TEST,
            T020_REQUIRED_INTEGRATION_TEST,
            T022_REQUIRED_INTEGRATION_TEST,
            REQUIRED_SUBTEST,
            T023_REQUIRED_INTEGRATION_TEST,
            T024_REQUIRED_INTEGRATION_TEST,
            T025_REQUIRED_INTEGRATION_TEST,
            ORDER_LOOKUP_REQUIRED_INTEGRATION_TEST,
            TEST_US1_PAYMENT,
            TEST_US1_RESTART,
            TEST_US1_HTTP_BOUNDARY,
        ):
            with self.subTest(name=name):
                self.assertEqual(required.get(name), "pass")

    def test_parser_accepts_all_twenty_six_required_roots(self):
        _, problems = self.launcher._parse_test_output(
            PACKAGE_OK_OUTPUT + all_required_pass_output(), "", 0
        )
        self.assertEqual(problems, [])

    def test_us1_parser_requires_each_new_root_individually(self):
        stdout = PACKAGE_OK_OUTPUT + all_required_pass_output()
        for name in (
            T023_REQUIRED_INTEGRATION_TEST,
            T024_REQUIRED_INTEGRATION_TEST,
            T025_REQUIRED_INTEGRATION_TEST,
            ORDER_LOOKUP_REQUIRED_INTEGRATION_TEST,
            TEST_US1_PAYMENT,
            TEST_US1_RESTART,
            TEST_US1_HTTP_BOUNDARY,
        ):
            with self.subTest(name=name):
                required, problems = self.launcher._parse_required_test_results(
                    without_test_events(stdout, name)
                )
                self.assertEqual(required.get(name), "missing")
                self.assertIn("required-test-missing", problems)

    def test_us1_parser_rejects_new_root_skip(self):
        stdout = replace_test_terminal(
            PACKAGE_OK_OUTPUT + all_required_pass_output(),
            POSTGRES_PACKAGE,
            T023_REQUIRED_INTEGRATION_TEST,
            "skip",
        )
        required, problems = self.launcher._parse_required_test_results(stdout)
        self.assertEqual(
            required.get(T023_REQUIRED_INTEGRATION_TEST), "skip"
        )
        self.assertIn("required-test-skip", problems)

    def test_us1_parser_rejects_new_root_wrong_package(self):
        stdout = without_test_events(
            PACKAGE_OK_OUTPUT + all_required_pass_output(),
            T025_REQUIRED_INTEGRATION_TEST,
        )
        stdout += json_event("run", POSTGRES_PACKAGE, T025_REQUIRED_INTEGRATION_TEST)
        stdout += json_event(
            "pass", POSTGRES_PACKAGE, T025_REQUIRED_INTEGRATION_TEST, elapsed=0.01
        )
        required, problems = self.launcher._parse_required_test_results(stdout)
        self.assertEqual(
            required.get(T025_REQUIRED_INTEGRATION_TEST), "wrong-package"
        )
        self.assertIn("required-test-wrong-package", problems)

    def test_us1_parser_rejects_new_root_output_spoof(self):
        stdout = without_test_events(
            PACKAGE_OK_OUTPUT + all_required_pass_output(),
            T024_REQUIRED_INTEGRATION_TEST,
        )
        stdout += json_event(
            "output",
            POSTGRES_PACKAGE,
            output=(
                "--- PASS: "
                + T024_REQUIRED_INTEGRATION_TEST
                + " (0.00s)\n"
            ),
        )
        required, problems = self.launcher._parse_required_test_results(stdout)
        self.assertEqual(
            required.get(T024_REQUIRED_INTEGRATION_TEST), "missing"
        )
        self.assertIn("required-test-missing", problems)

    def test_us1_parser_rejects_lookup_root_skip(self):
        stdout = replace_test_terminal(
            PACKAGE_OK_OUTPUT + all_required_pass_output(),
            POSTGRES_PACKAGE,
            ORDER_LOOKUP_REQUIRED_INTEGRATION_TEST,
            "skip",
        )
        required, problems = self.launcher._parse_required_test_results(stdout)
        self.assertEqual(
            required.get(ORDER_LOOKUP_REQUIRED_INTEGRATION_TEST), "skip"
        )
        self.assertIn("required-test-skip", problems)

    def test_us1_parser_rejects_lookup_root_wrong_package(self):
        stdout = without_test_events(
            PACKAGE_OK_OUTPUT + all_required_pass_output(),
            ORDER_LOOKUP_REQUIRED_INTEGRATION_TEST,
        )
        stdout += json_event(
            "run", HARNESS_PACKAGE, ORDER_LOOKUP_REQUIRED_INTEGRATION_TEST
        )
        stdout += json_event(
            "pass",
            HARNESS_PACKAGE,
            ORDER_LOOKUP_REQUIRED_INTEGRATION_TEST,
            elapsed=0.01,
        )
        required, problems = self.launcher._parse_required_test_results(stdout)
        self.assertEqual(
            required.get(ORDER_LOOKUP_REQUIRED_INTEGRATION_TEST),
            "wrong-package",
        )
        self.assertIn("required-test-wrong-package", problems)

    def test_us1_parser_rejects_lookup_root_output_spoof(self):
        stdout = without_test_events(
            PACKAGE_OK_OUTPUT + all_required_pass_output(),
            ORDER_LOOKUP_REQUIRED_INTEGRATION_TEST,
        )
        stdout += json_event(
            "output",
            POSTGRES_PACKAGE,
            output=(
                "--- PASS: "
                + ORDER_LOOKUP_REQUIRED_INTEGRATION_TEST
                + " (0.00s)\n"
            ),
        )
        required, problems = self.launcher._parse_required_test_results(stdout)
        self.assertEqual(
            required.get(ORDER_LOOKUP_REQUIRED_INTEGRATION_TEST), "missing"
        )
        self.assertIn("required-test-missing", problems)

    def test_us1_parser_rejects_tests_package_roots_skip_wrong_and_output(self):
        for target in (
            TEST_US1_PAYMENT,
            TEST_US1_RESTART,
            TEST_US1_HTTP_BOUNDARY,
        ):
            with self.subTest(target=target, case="skip"):
                stdout = replace_test_terminal(
                    PACKAGE_OK_OUTPUT + all_required_pass_output(),
                    TESTS_PACKAGE,
                    target,
                    "skip",
                )
                required, problems = (
                    self.launcher._parse_required_test_results(stdout)
                )
                self.assertEqual(required.get(target), "skip")
                self.assertIn("required-test-skip", problems)
            with self.subTest(target=target, case="wrong-package"):
                stdout = without_test_events(
                    PACKAGE_OK_OUTPUT + all_required_pass_output(),
                    target,
                )
                stdout += json_event("run", HARNESS_PACKAGE, target)
                stdout += json_event(
                    "pass", HARNESS_PACKAGE, target, elapsed=0.01
                )
                required, problems = (
                    self.launcher._parse_required_test_results(stdout)
                )
                self.assertEqual(required.get(target), "wrong-package")
                self.assertIn("required-test-wrong-package", problems)
            with self.subTest(target=target, case="output-spoof"):
                stdout = without_test_events(
                    PACKAGE_OK_OUTPUT + all_required_pass_output(),
                    target,
                )
                stdout += json_event(
                    "output",
                    TESTS_PACKAGE,
                    output="--- PASS: " + target + " (0.00s)\n",
                )
                required, problems = (
                    self.launcher._parse_required_test_results(stdout)
                )
                self.assertEqual(required.get(target), "missing")
                self.assertIn("required-test-missing", problems)

    def test_source_manifest_includes_both_cli_wrappers(self):
        self.assertIn("scripts/demo.sh", MOD.SOURCE_MANIFEST_EXPLICIT)
        self.assertIn("scripts/release.sh", MOD.SOURCE_MANIFEST_EXPLICIT)
        fixture = self._write_manifest_fixture(
            Path(self.temp.name) / "manifest-cli-wrappers-fixture"
        )
        self.launcher.repo = fixture
        self.launcher.collect_source_manifest()
        manifest_paths = {
            entry["path"] for entry in self.launcher.source["manifest"]
        }
        self.assertIn("scripts/demo.sh", manifest_paths)
        self.assertIn("scripts/release.sh", manifest_paths)

    def test_source_manifest_release_shell_membership_is_explicit(self):
        self.assertIn("scripts/release.sh", MOD.SOURCE_MANIFEST_EXPLICIT)
        reduced = tuple(
            path
            for path in MOD.SOURCE_MANIFEST_EXPLICIT
            if path != "scripts/release.sh"
        )
        self.assertNotIn("scripts/release.sh", reduced)
        self.assertIn("scripts/demo.sh", reduced)

    def test_source_manifest_globs_cover_us1_go_files(self):
        fixture = self._write_manifest_fixture(
            Path(self.temp.name) / "manifest-us1-fixture"
        )
        self.launcher.repo = fixture
        new_files = (
            "internal/payment/errors.go",
            "internal/payment/query_types.go",
            "internal/httpapi/query_handler.go",
            "internal/postgres/order_registration.go",
            "internal/postgres/order_lookup.go",
            "internal/mock/transaction.go",
            "tests/harness/committed_postgres.go",
            "tests/us1_report_test.go",
            "tests/us1_report.go",
        )
        for relative in new_files:
            path = fixture / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("package fixture\n", encoding="utf-8")
        self.launcher.collect_source_manifest()
        manifest_paths = {
            entry["path"] for entry in self.launcher.source["manifest"]
        }
        for relative in new_files:
            with self.subTest(relative=relative):
                self.assertIn(relative, manifest_paths)

    def test_source_manifest_globs_cover_nested_tests_scripts_migrations(self):
        fixture = self._write_manifest_fixture(
            Path(self.temp.name) / "manifest-nested-fixture"
        )
        self.launcher.repo = fixture
        new_files = (
            "tests/us1/nested_test.go",
            "scripts/newhelper.py",
            "migrations/new.sql",
        )
        for relative in new_files:
            path = fixture / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n", encoding="utf-8")
        for pattern in (
            "tests/**/*.go",
            "scripts/*.py",
            "migrations/*.sql",
        ):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, MOD.SOURCE_MANIFEST_GLOBS)
        self.launcher.collect_source_manifest()
        manifest_paths = {
            entry["path"] for entry in self.launcher.source["manifest"]
        }
        for relative in new_files:
            with self.subTest(relative=relative):
                self.assertIn(relative, manifest_paths)

    def test_go_environment_cgo_gomax_and_offline_test_env(self):
        self.args.download_modules = True
        online = MOD.Launcher(self.args)
        self.assertEqual(online.go_env["CGO_ENABLED"], "1")
        self.assertEqual(online.go_env["GOMAXPROCS"], "2")
        self.assertTrue(online.go_env["GOPROXY"].startswith("https://"))
        self.assertEqual(online.offline_go_env["GOPROXY"], "off")
        self.assertEqual(online.offline_go_env["GOSUMDB"], "off")

    def test_run_tests_exports_trusted_artifact_root_env(self):
        self.launcher.published_port = 54321
        self.launcher._run_process_group = mock.Mock(
            side_effect=[
                completed(["go", "vet"], 0, "", ""),
                completed(
                    ["go", "test"],
                    0,
                    PACKAGE_OK_OUTPUT + all_required_pass_output(),
                    "",
                ),
            ]
        )
        self.assertIn("tests", MOD.PACKAGES)
        self.launcher.run_tests()
        env = self.launcher._run_process_group.call_args_list[1].args[4]
        artifact_root = env.get("PAYMENT_HUB_TEST_ARTIFACT_ROOT")
        self.assertIsInstance(artifact_root, str)
        self.assertTrue(Path(artifact_root).is_absolute())
        self.assertEqual(artifact_root, str(self.launcher.artifact_root))
        self.assertIn("PAYMENT_HUB_TEST_DATABASE_URL", env)
        self.assertIn("PAYMENT_HUB_TEST_RUN_ID", env)

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
            PACKAGE_OK_OUTPUT + all_required_pass_output(),
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
            + all_required_pass_output()
            + json_event("run", HARNESS_PACKAGE, "TestOne")
            + json_event("skip", HARNESS_PACKAGE, "TestOne"),
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

    def test_go_test_external_budget_is_one_shared_batch_constant(self):
        budget = MOD.BUDGETS["go_test_external_seconds"]
        self.assertEqual(budget, CI_MOD.GO_TEST_BATCH_TIMEOUT_SECONDS)
        self.assertEqual(budget, 570)
        self.assertEqual(MOD.GO_TEST_PACKAGE_TIMEOUT_SECONDS, 120)
        for removed in (
            "GO_TEST_EXTERNAL_TIMEOUT_SECONDS",
            "GO_TEST_COLD_BUILD_MARGIN_SECONDS",
            "GO_TEST_FINALIZE_MARGIN_SECONDS",
        ):
            self.assertFalse(hasattr(MOD, removed), removed)

    def test_shared_batch_grants_no_per_package_exclusive_quota(self):
        budget = MOD.BUDGETS["go_test_external_seconds"]
        inner = MOD.GO_TEST_PACKAGE_TIMEOUT_SECONDS
        # The shared 570-second batch is not divided into per-package
        # reservations: build, link, and serial test execution consume it
        # together. 3 * 120 remains only a capacity-planning reference, not a
        # guaranteed per-package quota or a fixed cold-build/finalize margin.
        self.assertEqual(budget, 570)
        self.assertEqual(inner, 120)
        self.assertGreater(budget, inner)

    def test_budget_source_uses_shared_constant_not_local_formula(self):
        source_path = Path(MOD.__file__).resolve()
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"), filename=str(source_path)
        )
        budgets_value = None
        package_timeout_value = None
        removed_assignments = set()
        removed_names = {
            "GO_TEST_EXTERNAL_TIMEOUT_SECONDS",
            "GO_TEST_COLD_BUILD_MARGIN_SECONDS",
            "GO_TEST_FINALIZE_MARGIN_SECONDS",
        }
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id == "BUDGETS":
                    budgets_value = node.value
                elif target.id == "GO_TEST_PACKAGE_TIMEOUT_SECONDS":
                    package_timeout_value = node.value
                elif target.id in removed_names:
                    removed_assignments.add(target.id)
        self.assertIsInstance(budgets_value, ast.Dict)
        external_value = None
        for key, value in zip(budgets_value.keys, budgets_value.values):
            if isinstance(key, ast.Constant) and key.value == "go_test_external_seconds":
                external_value = value
        self.assertIsInstance(external_value, ast.Attribute)
        self.assertIsInstance(external_value.value, ast.Name)
        self.assertEqual(external_value.value.id, "ci_checks")
        self.assertEqual(external_value.attr, "GO_TEST_BATCH_TIMEOUT_SECONDS")
        self.assertIsNotNone(package_timeout_value)
        self.assertEqual(ast.literal_eval(package_timeout_value), 120)
        self.assertEqual(removed_assignments, set())

    def test_run_tests_keeps_inner_timeout_and_shared_batch_outer_budget(self):
        self.launcher.published_port = 54321
        self.launcher._run_process_group = mock.Mock(
            side_effect=[
                completed(["go", "vet"], 0, "", ""),
                completed(
                    ["go", "test"],
                    0,
                    PACKAGE_OK_OUTPUT + all_required_pass_output(),
                    "",
                ),
            ]
        )
        self.assertIn("tests", MOD.PACKAGES)
        self.launcher.run_tests()
        test_call = self.launcher._run_process_group.call_args_list[1]
        self.assertEqual(test_call.args[2], "go test integration")
        argv = test_call.args[3]
        outer_timeout = test_call.args[5]
        self.assertIn("-timeout=120s", argv)
        self.assertNotIn("-timeout=570s", argv)
        for flag in ("-race", "-count=1", "-p=1", "-json"):
            self.assertIn(flag, argv)
        self.assertEqual(outer_timeout, 570)
        self.assertEqual(outer_timeout, CI_MOD.GO_TEST_BATCH_TIMEOUT_SECONDS)
        self.assertEqual(outer_timeout, MOD.BUDGETS["go_test_external_seconds"])
        self.assertEqual(self.launcher.tests["test_exit"], 0)

    def test_successful_go_test_records_real_supervision_metadata(self):
        stdout = PACKAGE_OK_OUTPUT + all_required_pass_output()
        fake = SuccessProcess(stdout)
        with mock.patch.object(MOD.subprocess, "Popen", return_value=fake):
            result = self.launcher._run_process_group(
                "test",
                "test",
                "go test integration",
                ["go", "test"],
                {},
                570,
            )
        parsed = self.launcher._parse_go_test_results(
            result.stdout or "", result.stderr or "", result.returncode
        )
        self.launcher._record_go_test_results(parsed, result.returncode)
        supervision = self.launcher.tests.get("test_supervision")
        self.assertIsInstance(supervision, dict)
        self.assertEqual(
            set(supervision),
            {"timeout_seconds", "wait_elapsed_seconds", "timed_out"},
        )
        self.assertEqual(supervision["timeout_seconds"], 570)
        self.assertIs(type(supervision["timeout_seconds"]), float)
        self.assertIs(type(supervision["wait_elapsed_seconds"]), float)
        self.assertTrue(math.isfinite(supervision["timeout_seconds"]))
        self.assertTrue(math.isfinite(supervision["wait_elapsed_seconds"]))
        self.assertGreaterEqual(supervision["wait_elapsed_seconds"], 0)
        self.assertIs(supervision["timed_out"], False)
        record = self.launcher.commands[-1]
        self.assertEqual(record["name"], "go test integration")
        self.assertEqual(
            supervision["timeout_seconds"], record["timeout_seconds"]
        )
        self.assertEqual(
            supervision["wait_elapsed_seconds"], record["duration_seconds"]
        )
        self.assertEqual(supervision["timed_out"], record["timed_out"])
        encoded = json.dumps(supervision, sort_keys=True)
        for forbidden in ("argv", "commands", "Output", "stderr"):
            self.assertNotIn(forbidden, encoded)

    def test_supervision_does_not_override_cleanup_gate(self):
        stdout = PACKAGE_OK_OUTPUT + all_required_pass_output()
        with mock.patch.object(
            MOD.subprocess, "Popen", return_value=SuccessProcess(stdout)
        ):
            result = self.launcher._run_process_group(
                "test",
                "test",
                "go test integration",
                ["go", "test"],
                {},
                570,
            )
        parsed = self.launcher._parse_go_test_results(
            result.stdout or "", result.stderr or "", result.returncode
        )
        self.launcher._record_go_test_results(parsed, result.returncode)
        self.assertIn("test_supervision", self.launcher.tests)
        self.launcher.cleanup_failures.append("test:reap")
        self.assertEqual(self.launcher._build_report()["result"], "fail")

    def test_no_measurement_omits_supervision_instead_of_fabricating_zero(self):
        parsed = self.launcher._parse_go_test_results(
            PACKAGE_OK_OUTPUT + all_required_pass_output(), "", 0
        )
        self.launcher._record_go_test_results(parsed, 0)
        self.assertNotIn("test_supervision", self.launcher.tests)
        self.launcher.commands.append(
            {
                "name": "go vet integration",
                "argv": ["go", "vet"],
                "exit_code": 0,
                "allow_failure": True,
                "timeout_seconds": 120,
                "duration_seconds": 1.5,
                "timed_out": False,
            }
        )
        self.launcher._record_go_test_results(parsed, 0)
        self.assertNotIn("test_supervision", self.launcher.tests)
        self.assertNotIn(
            "test_supervision", self.launcher._build_report()["tests"]
        )

    def test_synthetic_timeout_records_actual_passed_timeout(self):
        fake = PartialOutputProcess(PACKAGE_OK_OUTPUT)
        with mock.patch.object(
            MOD.subprocess, "Popen", return_value=fake
        ), mock.patch.object(MOD.os, "killpg"):
            with self.assertRaises(MOD.LauncherError) as raised:
                self.launcher._run_process_group(
                    "test",
                    "test",
                    "go test integration",
                    ["go", "test"],
                    {},
                    0.5,
                )
        self.assertEqual(raised.exception.kind, "timeout")
        supervision = self.launcher.tests.get("test_supervision")
        self.assertIsInstance(supervision, dict)
        self.assertEqual(supervision["timeout_seconds"], 0.5)
        self.assertTrue(supervision["timed_out"])
        self.assertTrue(math.isfinite(supervision["wait_elapsed_seconds"]))
        self.assertGreaterEqual(supervision["wait_elapsed_seconds"], 0)

    def test_go_test_timeout_records_safe_partial_results(self):
        partial = (
            json_event("start", POSTGRES_PACKAGE)
            + json_event("run", POSTGRES_PACKAGE, "TestPackageHealth")
            + json_event("pass", POSTGRES_PACKAGE, "TestPackageHealth", elapsed=0.01)
            + json_event("pass", POSTGRES_PACKAGE, elapsed=0.1)
            + json_event("start", HARNESS_PACKAGE)
            + json_event("run", HARNESS_PACKAGE, REQUIRED_INTEGRATION_TEST)
        )
        fake = PartialOutputProcess(partial)
        with mock.patch.object(
            MOD.subprocess, "Popen", return_value=fake
        ), mock.patch.object(MOD.os, "killpg") as killpg:
            with self.assertRaises(MOD.LauncherError) as raised:
                self.launcher._run_process_group(
                    "test",
                    "test",
                    "go test integration",
                    ["go", "test"],
                    {},
                    570,
                )
        self.assertEqual(raised.exception.stage, "test")
        self.assertEqual(raised.exception.kind, "timeout")
        killpg.assert_called_once_with(fake.pid, signal.SIGKILL)
        tests = self.launcher.tests
        self.assertEqual(tests.get("test_exit"), 124)
        package_results = tests.get("package_results", {})
        self.assertEqual(
            package_results.get("internal/postgres", {}).get("status"), "pass"
        )
        self.assertEqual(
            package_results.get("internal/mock", {}).get("status"), "missing"
        )
        self.assertEqual(
            tests.get("required_test_results", {}).get(REQUIRED_INTEGRATION_TEST),
            "missing",
        )
        self.assertEqual(tests.get("test_root_count"), 1)
        self.assertEqual(tests.get("test_subtest_count"), 0)
        problems = tests.get("problems", [])
        self.assertIn("test-exit", problems)
        self.assertIn("required-test-missing", problems)
        self.assertIn("test-incomplete", problems)
        self.assertNotIn("no-captured-test-events", problems)
        self.assertTrue(tests.get("failure_diagnostics"))
        supervision = tests.get("test_supervision")
        self.assertIsInstance(supervision, dict)
        self.assertEqual(
            set(supervision),
            {"timeout_seconds", "wait_elapsed_seconds", "timed_out"},
        )
        self.assertEqual(supervision["timeout_seconds"], 570)
        self.assertIs(type(supervision["wait_elapsed_seconds"]), float)
        self.assertTrue(math.isfinite(supervision["wait_elapsed_seconds"]))
        self.assertGreaterEqual(supervision["wait_elapsed_seconds"], 0)
        self.assertIs(supervision["timed_out"], True)
        record = self.launcher.commands[-1]
        self.assertEqual(record["name"], "go test integration")
        self.assertEqual(
            supervision["wait_elapsed_seconds"], record["duration_seconds"]
        )
        self.assertEqual(supervision["timed_out"], record["timed_out"])
        self.assertEqual(self.launcher._build_report()["result"], "fail")

    def test_go_test_timeout_without_events_marks_no_captured_events(self):
        canary = "db-foundation-canary-not-real"
        stdout = (
            canary
            + "\n"
            + json_event(
                "output",
                POSTGRES_PACKAGE,
                "TestPackageHealth",
                output=canary + "\n",
            )
        )
        fake = PartialOutputProcess(stdout)
        with mock.patch.object(
            MOD.subprocess, "Popen", return_value=fake
        ), mock.patch.object(MOD.os, "killpg"):
            with self.assertRaises(MOD.LauncherError) as raised:
                self.launcher._run_process_group(
                    "test",
                    "test",
                    "go test integration",
                    ["go", "test"],
                    {},
                    570,
                )
        self.assertEqual(raised.exception.kind, "timeout")
        tests = self.launcher.tests
        self.assertEqual(tests.get("test_exit"), 124)
        self.assertEqual(tests.get("test_root_count"), 0)
        self.assertEqual(tests.get("test_subtest_count"), 0)
        self.assertIn("no-captured-test-events", tests.get("problems", []))
        package_results = tests.get("package_results", {})
        for name in MOD.PACKAGES:
            self.assertEqual(
                package_results.get(name, {}).get("status"), "missing"
            )
        self.assertNotIn(canary, json.dumps(tests, sort_keys=True))
        self.assertNotIn(
            canary,
            json.dumps(self.launcher._build_report(), sort_keys=True),
        )

    def test_go_test_timeout_redacts_malicious_package_and_test_labels(self):
        canary = "db-foundation-canary-not-real"
        stdout = (
            json_event("run", "evil.example/" + canary, "Test/" + canary)
            + json_event("fail", "evil.example/" + canary, "Test/" + canary)
            + json_event("run", HARNESS_PACKAGE, REQUIRED_INTEGRATION_TEST)
            + json_event(
                "pass",
                HARNESS_PACKAGE,
                REQUIRED_INTEGRATION_TEST,
                elapsed=0.01,
            )
        )
        fake = PartialOutputProcess(stdout)
        with mock.patch.object(
            MOD.subprocess, "Popen", return_value=fake
        ), mock.patch.object(MOD.os, "killpg"):
            with self.assertRaises(MOD.LauncherError) as raised:
                self.launcher._run_process_group(
                    "test",
                    "test",
                    "go test integration",
                    ["go", "test"],
                    {},
                    570,
                )
        self.assertEqual(raised.exception.kind, "timeout")
        tests = self.launcher.tests
        self.assertEqual(tests.get("test_exit"), 124)
        self.assertIn("unexpected-package", tests.get("problems", []))
        diagnostics = tests.get("failure_diagnostics", [])
        self.assertTrue(diagnostics)
        for entry in diagnostics:
            self.assertIn(
                entry["package"],
                (
                    "",
                    "unknown",
                    POSTGRES_PACKAGE,
                    MOCK_PACKAGE,
                    HARNESS_PACKAGE,
                    TESTS_PACKAGE,
                ),
            )
            self.assertNotIn(canary, entry["test"])
            self.assertNotIn(canary, entry["location"])
        self.assertNotIn(
            canary,
            json.dumps(self.launcher._build_report(), sort_keys=True),
        )


class SharedSetupLauncherConfigurationTests(unittest.TestCase):
    """Read-only setup/launcher pin parity; an existing invariant, no Go/Docker."""

    def test_shared_setup_launcher_configuration_stays_in_sync(self):
        shared = {
            "EXPECTED_GO_VERSION": (
                MOD.EXPECTED_GO_VERSION,
                SETUP_MOD.EXPECTED_GO_VERSION,
            ),
            "EXPECTED_GOOS": (MOD.EXPECTED_GOOS, SETUP_MOD.EXPECTED_GOOS),
            "EXPECTED_GOARCH": (MOD.EXPECTED_GOARCH, SETUP_MOD.EXPECTED_GOARCH),
            "EXPECTED_GOTOOLCHAIN": (
                MOD.EXPECTED_GOTOOLCHAIN,
                SETUP_MOD.EXPECTED_GOTOOLCHAIN,
            ),
            "EXPECTED_GOTELEMETRY": (
                MOD.EXPECTED_GOTELEMETRY,
                SETUP_MOD.EXPECTED_GOTELEMETRY,
            ),
            "EXPECTED_PLATFORM": (
                MOD.EXPECTED_PLATFORM,
                SETUP_MOD.EXPECTED_PLATFORM,
            ),
            "EXPECTED_MODULE": (MOD.EXPECTED_MODULE, SETUP_MOD.EXPECTED_MODULE),
            "EXPECTED_GO_DIRECTIVE": (
                MOD.EXPECTED_GO_DIRECTIVE,
                SETUP_MOD.EXPECTED_GO_DIRECTIVE,
            ),
            "APPROVED_DIRECT": (
                MOD.APPROVED_DIRECT,
                SETUP_MOD.APPROVED_DIRECT,
            ),
            "POSTGRES_IMAGE/PG_IMAGE": (MOD.POSTGRES_IMAGE, SETUP_MOD.PG_IMAGE),
        }
        for field, (launcher_value, setup_value) in shared.items():
            with self.subTest(field=field):
                self.assertEqual(launcher_value, setup_value)

    def test_go_1_27_4_pins_and_verified_builder_digest(self):
        self.assertEqual(MOD.EXPECTED_GO_VERSION, "go1.27.4")
        self.assertEqual(SETUP_MOD.EXPECTED_GO_VERSION, "go1.27.4")
        self.assertEqual(MOD.EXPECTED_GO_DIRECTIVE, "1.27.4")
        self.assertEqual(SETUP_MOD.EXPECTED_GO_DIRECTIVE, "1.27.4")
        self.assertEqual(
            MOD.GO_BUILDER_IMAGE,
            "docker.io/library/golang:1.27.4-bookworm"
            "@sha256:4fa7c968ba93f1635670522dbb01a2c78150443e1385746924566079f36570ea",
        )
        self.assertNotIn("1.27.3", MOD.GO_BUILDER_IMAGE)


class SetupWorkflowBatchBudgetTests(unittest.TestCase):
    """Static PyYAML and bash -n checks for the frozen workflow contract."""

    WORKFLOW = REPO / ".github" / "workflows" / "setup-check.yml"
    CHECKOUT = (
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    )
    SETUP_GO = (
        "actions/setup-go@b7ad1dad31e06c5925ef5d2fc7ad053ef454303e"
    )
    UPLOAD = (
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
    )

    @classmethod
    def setUpClass(cls):
        if yaml is None:
            raise unittest.SkipTest("PyYAML is unavailable")
        cls.text = cls.WORKFLOW.read_text(encoding="utf-8")
        cls.data = yaml.safe_load(cls.text)

    def _job(self, name):
        return self.data["jobs"][name]

    def _steps(self, job_name):
        return self._job(job_name)["steps"]

    def _run(self, job_name, step_name):
        for step in self._steps(job_name):
            if step.get("name") == step_name:
                return step.get("run", "")
        self.fail(f"missing workflow step: {step_name}")

    def _upload(self, job_name):
        for step in self._steps(job_name):
            if step.get("uses") == self.UPLOAD:
                return step
        self.fail(f"missing upload step in {job_name}")

    def test_job_timeouts_are_20_20_10(self):
        self.assertEqual(self._job("setup-check")["timeout-minutes"], 20)
        self.assertEqual(
            self._job("database-foundation")["timeout-minutes"], 20
        )
        self.assertEqual(
            self._job("vulnerability-check")["timeout-minutes"], 10
        )

    def test_ci_gate_timeout_is_one_minute_separate_from_original_three(self):
        self.assertEqual(self._job("ci-gate")["timeout-minutes"], 1)

    def test_permissions_needs_concurrency_and_pins_unchanged(self):
        self.assertEqual(self.data["permissions"], {"contents": "read"})
        self.assertEqual(
            self.data["concurrency"],
            {
                "group": (
                    "${{ github.workflow }}-${{ github.event.pull_request.number "
                    "|| github.ref }}"
                ),
                "cancel-in-progress": False,
            },
        )
        self.assertEqual(self._job("database-foundation")["needs"], "setup-check")
        self.assertNotIn("needs", self._job("setup-check"))
        self.assertNotIn("needs", self._job("vulnerability-check"))
        uses = [
            step["uses"]
            for job in self.data["jobs"].values()
            for step in job.get("steps", [])
            if "uses" in step
        ]
        self.assertEqual(set(uses), {self.CHECKOUT, self.SETUP_GO, self.UPLOAD})
        self.assertEqual(uses.count(self.CHECKOUT), 3)
        self.assertEqual(uses.count(self.SETUP_GO), 3)
        self.assertEqual(uses.count(self.UPLOAD), 3)

    def test_safe_upload_paths_and_exports_unchanged(self):
        expected = {
            "setup-check": (
                "Upload safe setup reports",
                "setup-check-safe-reports-${{ github.run_id }}-${{ github.run_attempt }}",
                [
                    "${{ runner.temp }}/payment-hub-setup/reports/upload/go-default.safe.json",
                    "${{ runner.temp }}/payment-hub-setup/reports/upload/provenance.json",
                ],
                "--expect default",
            ),
            "database-foundation": (
                "Upload safe database reports",
                "database-foundation-safe-reports-${{ github.run_id }}-${{ github.run_attempt }}",
                [
                    "${{ runner.temp }}/payment-hub-db/reports/upload/db-foundation.safe.json",
                    "${{ runner.temp }}/payment-hub-db/reports/upload/cancellation.safe.json",
                    "${{ runner.temp }}/payment-hub-db/reports/upload/us1.safe.json",
                    "${{ runner.temp }}/payment-hub-db/reports/upload/us2.safe.json",
                    "${{ runner.temp }}/payment-hub-db/reports/upload/us3.safe.json",
                    "${{ runner.temp }}/payment-hub-db/reports/upload/provenance.json",
                ],
                "--expect db,cancellation,us1,us2,us3",
            ),
            "vulnerability-check": (
                "Upload safe vulnerability reports",
                "vulnerability-check-safe-reports-${{ github.run_id }}-${{ github.run_attempt }}",
                [
                    "${{ runner.temp }}/payment-hub-vulnerability/reports/upload/vulnerability.safe.json",
                    "${{ runner.temp }}/payment-hub-vulnerability/reports/upload/provenance.json",
                ],
                "--expect vulnerability",
            ),
        }
        for job_name, (step_name, artifact, paths, expect) in expected.items():
            with self.subTest(job=job_name):
                step = self._upload(job_name)
                self.assertEqual(step["name"], step_name)
                self.assertEqual(step.get("if"), "always()")
                self.assertEqual(step["with"]["name"], artifact)
                actual_paths = [
                    line.strip()
                    for line in step["with"]["path"].splitlines()
                    if line.strip()
                ]
                self.assertEqual(actual_paths, paths)
                self.assertEqual(step["with"]["if-no-files-found"], "error")
                self.assertEqual(step["with"]["retention-days"], 30)
                self.assertIs(step["with"]["include-hidden-files"], False)
                export_steps = [
                    candidate
                    for candidate in self._steps(job_name)
                    if candidate.get("name", "").startswith("Export safe")
                ]
                self.assertEqual(len(export_steps), 1)
                self.assertIn(expect, export_steps[0].get("run", ""))

    def test_summaries_share_honest_570_batch_contract(self):
        default = self._run("setup-check", "Run foundation unit checks")
        database = self._run(
            "database-foundation", "Summarize database foundation results"
        )
        for label, text in (("default", default), ("database", database)):
            with self.subTest(summary=label):
                self.assertIn("570", text)
                self.assertIn("整批", text)
                self.assertIn("构建", text)
                self.assertIn("链接", text)
                self.assertIn("串行", text)
                self.assertIn("共享", text)
                self.assertIn("20 分钟", text)
        self.assertIn("-timeout=90s", default)
        self.assertIn("120 秒", database)
        self.assertIn("kill/reap", database)
        self.assertIn("不包含超时后的 kill/reap", database)
        self.assertIn("硬杀", database)
        self.assertIn("ready 180s / exit 120s / total 360s", database)
        for label, text in (("default", default), ("database", database)):
            with self.subTest(summary=label):
                for forbidden in ("3×120", "3 * 120", "3个120", "三个 120"):
                    self.assertNotIn(forbidden, text)

    def test_all_16_bash_blocks_parse_with_bash_n(self):
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        scripts = [
            step.get("run", "")
            for job in self.data["jobs"].values()
            for step in job.get("steps", [])
            if step.get("shell") == "bash"
        ]
        self.assertEqual(len(scripts), 16)
        for index, script in enumerate(scripts):
            result = subprocess.run(
                [bash, "-n"],
                input=script,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                result.returncode,
                0,
                f"bash -n block {index} failed: {result.stderr}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
