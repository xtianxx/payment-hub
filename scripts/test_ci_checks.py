"""Mock-only regression tests for scripts/ci_checks.py.

No Go, PostgreSQL, Docker, network, or launcher process is started. All
subprocess boundaries are mocked. These tests exercise the strict go test -json
parser, the frozen default required-root inventory, the run-default report
writer, and the safe export whitelist.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import inspect
import io
import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "ci_checks", REPO / "scripts/ci_checks.py"
)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)

APP = "payment-hub/internal/app"
HARNESS = "payment-hub/tests/harness"
CMD = "payment-hub/cmd/payment-hub"
EXPECTED = (APP, HARNESS, CMD)
REQUIRED = {
    "TestRequiredRoot": HARNESS,
    "TestRequiredRoot/sub_case": HARNESS,
}

# Frozen 26 PR3 roots read from the four review/regression test files, plus the
# three original integration roots and the explicitly required lifecycle
# subtest. This literal is deliberately not derived from the source tree.
EXPECTED_DEFAULT_REQUIRED = {
    "TestStartupDiagnosticsDatabaseOpenFailure": APP,
    "TestStartupDiagnosticsPingRetryExhaustion": APP,
    "TestStartupDiagnosticsMigrationAfterStartupExpiry": APP,
    "TestStartupDiagnosticsTestModeFailure": APP,
    "TestStartupDiagnosticsListenFailure": APP,
    "TestStartupDiagnosticsStartupCancellation": APP,
    "TestStartupDiagnosticsLateListenCancellation": APP,
    "TestStartupDiagnosticsWriterBlockingIsBounded": APP,
    "TestStartupDiagnosticsNormalPathUnchanged": APP,
    "TestShutdownCleanupSealFailureStillClosesDatabase": APP,
    "TestShutdownCleanupActiveDatabaseUserPreventsEarlyClose": APP,
    "TestArtifactsFinishRejectsUnclosedObserverAfterHalfFrame": HARNESS,
    "TestArtifactsFinishAcceptsClosedTrustedObserver": HARNESS,
    "TestObserverFinalSnapshotUnavailableBeforeClose": HARNESS,
    "TestObserverFinalSnapshotDeepCopyAndStable": HARNESS,
    "TestObserverCloseHalfFrameFinalUntrusted": HARNESS,
    "TestObserverCloseDoesNotSealOpenGeneration": HARNESS,
    "TestObserverCloseDeadlineHasNoFinal": HARNESS,
    "TestObserverCloseSocketCleanupFailureHasNoFinal": HARNESS,
    "TestProcessReviewEmptyEnvDoesNotInheritHost": HARNESS,
    "TestProcessReviewLeaderReapedStopHonorsDeadline": HARNESS,
    "TestProcessReviewLeaderReapedWaitHonorsDeadline": HARNESS,
    "TestProcessReviewLeaderReapedLifetimeCancel": HARNESS,
    "TestProcessReviewTerminalPublishedAfterGroupWatchJoined": HARNESS,
    "TestProcessReviewWatchGroupStopUnblocksFullEventChannel": HARNESS,
    "TestProcessReviewHelper": HARNESS,
}


def event(
    action,
    package,
    test=None,
    elapsed=None,
    output=None,
    time="2026-11-08T00:00:00Z",
    **extra,
):
    value = {"Time": time, "Action": action, "Package": package}
    if test is not None:
        value["Test"] = test
    if elapsed is not None:
        value["Elapsed"] = elapsed
    if output is not None:
        value["Output"] = output
    value.update(extra)
    return json.dumps(value, separators=(",", ":")) + "\n"


def stream(*parts):
    return "".join(parts)


def package_block(package, duration=0.1, test_name="TestPackageHealth"):
    return stream(
        event("start", package),
        event("run", package, test_name),
        event("pass", package, test_name, elapsed=0.01),
        event("pass", package, elapsed=duration),
    )


def no_test_files_block(package=CMD):
    return stream(
        event("start", package),
        event(
            "output",
            package,
            output="?   \t" + package + "\t[no test files]\n",
        ),
        event("skip", package, elapsed=0),
    )


def required_root_block(name, package=HARNESS):
    return stream(
        event("run", package, name),
        event("pass", package, name, elapsed=0.01),
    )


def required_subtest_block(parent, sub, package=HARNESS):
    full = parent + "/" + sub
    return stream(
        event("run", package, parent),
        event("run", package, full),
        event("pass", package, full, elapsed=0.01),
        event("pass", package, parent, elapsed=0.02),
    )


def complete_stream():
    return stream(
        package_block(APP, 0.12),
        event("run", HARNESS, "TestRequiredRoot"),
        event("run", HARNESS, "TestRequiredRoot/sub_case"),
        event("pass", HARNESS, "TestRequiredRoot/sub_case", elapsed=0.01),
        event("pass", HARNESS, "TestRequiredRoot", elapsed=0.02),
        event("start", HARNESS),
        event("run", HARNESS, "TestPackageHealth"),
        event("pass", HARNESS, "TestPackageHealth", elapsed=0.01),
        event("pass", HARNESS, elapsed=0.34),
        no_test_files_block(),
    )


def default_stream(expected_packages, required_tests):
    """Build a valid stream matching the frozen default inventory."""
    parts = []
    for package in expected_packages:
        if package == CMD:
            parts.append(no_test_files_block(package))
            continue
        parts.append(event("start", package))
        parts.append(
            stream(
                event("run", package, "TestPackageHealth"),
                event("pass", package, "TestPackageHealth", elapsed=0.01),
            )
        )
        roots = {}
        for name, bound in required_tests.items():
            if bound != package:
                continue
            if "/" in name:
                parent, sub = name.split("/", 1)
                roots.setdefault(parent, []).append(sub)
            else:
                roots.setdefault(name, [])
        for root, subs in roots.items():
            parts.append(event("run", package, root))
            for sub in subs:
                parts.append(event("run", package, root + "/" + sub))
                parts.append(
                    event(
                        "pass",
                        package,
                        root + "/" + sub,
                        elapsed=0.01,
                    )
                )
            parts.append(event("pass", package, root, elapsed=0.02))
        parts.append(event("pass", package, elapsed=0.5))
    return "".join(parts)


def proc(code, stdout, stderr="", timed_out=False):
    if isinstance(stdout, str):
        stdout = stdout.encode("utf-8")
    if isinstance(stderr, str):
        stderr = stderr.encode("utf-8")
    return MOD.ProcResult(code, stdout, stderr, timed_out)


def json_bytes(value):
    return json.dumps(value, sort_keys=True).encode("utf-8")


class ParseGoTestJSONTests(unittest.TestCase):
    def parse(self, stdout, stderr="", exit_code=0, expected=EXPECTED, required=None):
        return MOD.parse_go_test_json(
            stdout,
            stderr,
            exit_code,
            expected,
            REQUIRED if required is None else required,
            allowed_no_test_files=(CMD,),
        )

    def test_accepts_complete_json_stream(self):
        result = self.parse(complete_stream())
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["package_results"][APP]["status"], "pass")
        self.assertEqual(result["package_results"][APP]["duration"], "0.12s")
        self.assertEqual(result["package_results"][HARNESS]["duration"], "0.34s")
        self.assertEqual(result["package_results"][CMD]["status"], "no-test-files")
        self.assertEqual(result["required_test_results"]["TestRequiredRoot"], "pass")
        self.assertEqual(
            result["required_test_results"]["TestRequiredRoot/sub_case"], "pass"
        )
        self.assertEqual(result["root_count"], 3)
        self.assertEqual(result["subtest_count"], 1)
        rendered = json.dumps(result)
        self.assertNotIn("=== RUN", rendered)
        self.assertNotIn("no test files", rendered)

    def test_accepts_output_type_enum(self):
        stdout = stream(
            event("start", APP),
            event(
                "output",
                APP,
                output="package regular\n",
                OutputType="",
            ),
            event(
                "output",
                APP,
                output="=== RUN frame\n",
                OutputType="frame",
            ),
            event("run", APP, "TestOne"),
            event(
                "output",
                APP,
                test="TestOne",
                output="error output\n",
                OutputType="error",
            ),
            event(
                "output",
                APP,
                test="TestOne",
                output="error continuation\n",
                OutputType="error-continue",
            ),
            event("pass", APP, "TestOne", elapsed=0.01),
            event("pass", APP, elapsed=0.1),
        )
        result = self.parse(stdout, expected=(APP,), required={})
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["package_results"][APP]["status"], "pass")

    def test_rejects_package_suffix_lookalike(self):
        lookalike = "evil/" + APP
        stdout = stream(
            event("start", lookalike),
            event("run", lookalike, "TestOne"),
            event("pass", lookalike, "TestOne", elapsed=0.01),
            event("pass", lookalike, elapsed=0.1),
        )
        result = self.parse(stdout, expected=(APP,), required={})
        self.assertIn("unexpected-package", result["problems"])
        self.assertIn("package-missing", result["problems"])
        self.assertNotEqual(result["package_results"][APP]["status"], "pass")

    def test_rejects_cross_package_required_pass(self):
        # The root passes in APP instead of its bound HARNESS package.
        stdout = stream(
            event("start", APP),
            event("run", APP, "TestPackageHealth"),
            event("pass", APP, "TestPackageHealth", elapsed=0.01),
            event("run", APP, "TestRequiredRoot"),
            event("pass", APP, "TestRequiredRoot", elapsed=0.01),
            event("pass", APP, elapsed=0.12),
            event("start", HARNESS),
            event("run", HARNESS, "TestPackageHealth"),
            event("pass", HARNESS, "TestPackageHealth", elapsed=0.01),
            event("pass", HARNESS, elapsed=0.2),
            no_test_files_block(),
        )
        result = self.parse(stdout)
        self.assertEqual(
            result["required_test_results"]["TestRequiredRoot"], "wrong-package"
        )
        self.assertIn("required-test-wrong-package", result["problems"])

    def test_rejects_fake_output_pass(self):
        stdout = stream(
            package_block(APP, 0.1),
            event("start", HARNESS),
            event("run", HARNESS, "TestPackageHealth"),
            event("pass", HARNESS, "TestPackageHealth", elapsed=0.01),
            event(
                "output",
                HARNESS,
                output="=== RUN   TestRequiredRoot\n--- PASS: TestRequiredRoot (0.00s)\n",
            ),
            event("pass", HARNESS, elapsed=0.2),
            no_test_files_block(),
        )
        result = self.parse(stdout)
        self.assertEqual(
            result["required_test_results"]["TestRequiredRoot"], "missing"
        )
        self.assertEqual(
            result["required_test_results"]["TestRequiredRoot/sub_case"], "missing"
        )
        self.assertIn("required-test-missing", result["problems"])

    def test_rejects_missing_subtest(self):
        stdout = stream(
            package_block(APP, 0.1),
            event("start", HARNESS),
            event("run", HARNESS, "TestPackageHealth"),
            event("pass", HARNESS, "TestPackageHealth", elapsed=0.01),
            event("run", HARNESS, "TestRequiredRoot"),
            event("pass", HARNESS, "TestRequiredRoot", elapsed=0.02),
            event("pass", HARNESS, elapsed=0.2),
            no_test_files_block(),
        )
        result = self.parse(stdout)
        self.assertEqual(
            result["required_test_results"]["TestRequiredRoot"], "pass"
        )
        self.assertEqual(
            result["required_test_results"]["TestRequiredRoot/sub_case"], "missing"
        )
        self.assertIn("required-test-missing", result["problems"])

    def test_rejects_required_skip_and_fail(self):
        for action, expected_problem, status in (
            ("skip", "required-test-skip", "skip"),
            ("fail", "required-test-fail", "fail"),
        ):
            with self.subTest(action=action):
                stdout = stream(
                    package_block(APP, 0.1),
                    event("start", HARNESS),
                    event("run", HARNESS, "TestPackageHealth"),
                    event("pass", HARNESS, "TestPackageHealth", elapsed=0.01),
                    event("run", HARNESS, "TestRequiredRoot"),
                    event("run", HARNESS, "TestRequiredRoot/sub_case"),
                    event(action, HARNESS, "TestRequiredRoot/sub_case"),
                    event(action, HARNESS, "TestRequiredRoot"),
                    event("pass", HARNESS, elapsed=0.2),
                    no_test_files_block(),
                )
                result = self.parse(stdout)
                self.assertEqual(
                    result["required_test_results"]["TestRequiredRoot"], status
                )
                self.assertIn(expected_problem, result["problems"])

    def test_rejects_required_duplicate(self):
        stdout = stream(
            package_block(APP, 0.1),
            event("start", HARNESS),
            event("run", HARNESS, "TestPackageHealth"),
            event("pass", HARNESS, "TestPackageHealth", elapsed=0.01),
            event("run", HARNESS, "TestRequiredRoot"),
            event("run", HARNESS, "TestRequiredRoot/sub_case"),
            event("pass", HARNESS, "TestRequiredRoot/sub_case", elapsed=0.01),
            event("pass", HARNESS, "TestRequiredRoot", elapsed=0.02),
            event("pass", HARNESS, "TestRequiredRoot", elapsed=0.03),
            event("pass", HARNESS, elapsed=0.2),
            no_test_files_block(),
        )
        result = self.parse(stdout)
        self.assertEqual(
            result["required_test_results"]["TestRequiredRoot"], "duplicate"
        )
        self.assertIn("required-test-duplicate", result["problems"])

    def test_rejects_required_terminal_without_run(self):
        stdout = stream(
            package_block(APP, 0.1),
            event("start", HARNESS),
            event("run", HARNESS, "TestPackageHealth"),
            event("pass", HARNESS, "TestPackageHealth", elapsed=0.01),
            event("pass", HARNESS, "TestRequiredRoot", elapsed=0.02),
            event("pass", HARNESS, elapsed=0.2),
            no_test_files_block(),
        )
        result = self.parse(stdout)
        self.assertEqual(
            result["required_test_results"]["TestRequiredRoot"], "not-run"
        )
        self.assertIn("required-test-not-run", result["problems"])

        # A terminal before the run event must not become a pass even if a run
        # event is forged afterwards.
        stdout = stream(
            event("start", HARNESS),
            event("run", HARNESS, "TestPackageHealth"),
            event("pass", HARNESS, "TestPackageHealth", elapsed=0.01),
            event("pass", HARNESS, "TestRequiredRoot", elapsed=0.02),
            event("run", HARNESS, "TestRequiredRoot"),
            event("pass", HARNESS, elapsed=0.2),
        )
        result = self.parse(stdout)
        self.assertEqual(
            result["required_test_results"]["TestRequiredRoot"], "not-run"
        )
        self.assertIn("required-test-not-run", result["problems"])

    def test_rejects_non_required_fail_and_skip(self):
        for action, problem in (("fail", "test-fail"), ("skip", "test-skip")):
            with self.subTest(action=action):
                stdout = stream(
                    event("start", APP),
                    event("run", APP, "TestOther"),
                    event(action, APP, "TestOther"),
                    event("pass", APP, elapsed=0.1),
                )
                result = self.parse(stdout, expected=(APP,), required={})
                self.assertIn(problem, result["problems"])
                self.assertNotEqual(result["package_results"][APP]["status"], "pass")

    def test_rejects_package_fail_missing_duplicate(self):
        fail = stream(
            event("start", APP),
            event("run", APP, "TestOne"),
            event("pass", APP, "TestOne", elapsed=0.01),
            event("fail", APP, elapsed=0.1),
        )
        result = self.parse(fail, expected=(APP,), required={})
        self.assertIn("package-fail", result["problems"])
        self.assertEqual(result["package_results"][APP]["status"], "fail")

        missing = self.parse("", expected=(APP,), required={})
        self.assertIn("package-missing", missing["problems"])

        duplicate = stream(
            event("start", APP),
            event("run", APP, "TestOne"),
            event("pass", APP, "TestOne", elapsed=0.01),
            event("pass", APP, elapsed=0.1),
            event("pass", APP, elapsed=0.2),
        )
        result = self.parse(duplicate, expected=(APP,), required={})
        self.assertIn("package-duplicate", result["problems"])

    def test_rejects_package_pass_without_test_run(self):
        stdout = stream(event("start", APP), event("pass", APP, elapsed=0.1))
        result = self.parse(stdout, expected=(APP,), required={})
        self.assertIn("package-no-tests", result["problems"])
        self.assertNotEqual(result["package_results"][APP]["status"], "pass")

    def test_rejects_malformed_non_json_and_bad_types(self):
        cases = {
            "non-json-output": "not json\n",
            "malformed-event": "[]\n",
            "unknown-action": stream(
                event("dance", APP),
            ),
            "unknown-key": json.dumps(
                {
                    "Time": "2026-11-08T00:00:00Z",
                    "Action": "start",
                    "Package": APP,
                    "Extra": 1,
                }
            )
            + "\n",
            "elapsed-string": json.dumps(
                {
                    "Time": "2026-11-08T00:00:00Z",
                    "Action": "pass",
                    "Package": APP,
                    "Elapsed": "0.1",
                }
            )
            + "\n",
            "elapsed-negative": json.dumps(
                {
                    "Time": "2026-11-08T00:00:00Z",
                    "Action": "pass",
                    "Package": APP,
                    "Elapsed": -1,
                }
            )
            + "\n",
            "elapsed-nan": json.dumps(
                {
                    "Time": "2026-11-08T00:00:00Z",
                    "Action": "pass",
                    "Package": APP,
                    "Elapsed": float("nan"),
                }
            )
            + "\n",
            "missing-package": json.dumps(
                {"Time": "2026-11-08T00:00:00Z", "Action": "start"}
            )
            + "\n",
            "missing-test": stream(event("run", APP)),
            "output-type": json.dumps(
                {
                    "Time": "2026-11-08T00:00:00Z",
                    "Action": "output",
                    "Package": APP,
                    "Output": 1,
                }
            )
            + "\n",
            "outputtype-badtype": json.dumps(
                {
                    "Time": "2026-11-08T00:00:00Z",
                    "Action": "output",
                    "Package": APP,
                    "Output": "x",
                    "OutputType": 1,
                }
            )
            + "\n",
            "outputtype-unknown": json.dumps(
                {
                    "Time": "2026-11-08T00:00:00Z",
                    "Action": "output",
                    "Package": APP,
                    "Output": "x",
                    "OutputType": "bogus",
                }
            )
            + "\n",
            "outputtype-nonoutput": json.dumps(
                {
                    "Time": "2026-11-08T00:00:00Z",
                    "Action": "run",
                    "Package": APP,
                    "Test": "TestOne",
                    "OutputType": "frame",
                }
            )
            + "\n",
            "duplicate-key": (
                '{"Action":"start","Action":"pass","Package":"'
                + APP
                + '"}\n'
            ),
        }
        expected_problem = {
            "non-json-output": "non-json-output",
            "malformed-event": "malformed-event",
            "unknown-action": "unknown-action",
            "unknown-key": "malformed-event",
            "elapsed-string": "malformed-event",
            "elapsed-negative": "malformed-event",
            "elapsed-nan": "malformed-event",
            "missing-package": "malformed-event",
            "missing-test": "malformed-event",
            "output-type": "malformed-event",
            "outputtype-badtype": "malformed-event",
            "outputtype-unknown": "malformed-event",
            "outputtype-nonoutput": "malformed-event",
            "duplicate-key": "non-json-output",
        }
        for name, stdout in cases.items():
            with self.subTest(case=name):
                result = self.parse(stdout, expected=(APP,), required={})
                self.assertIn(expected_problem[name], result["problems"])

    def test_rejects_truncated_and_empty_streams(self):
        truncated = complete_stream()[:-1]
        result = self.parse(truncated)
        self.assertIn("truncated-output", result["problems"])

        result = self.parse("", exit_code=0)
        self.assertIn("empty-output", result["problems"])
        self.assertIn("package-missing", result["problems"])
        self.assertNotEqual(result["package_results"][APP]["status"], "pass")

    def test_rejects_nonzero_exit_even_when_all_pass(self):
        result = self.parse(complete_stream(), exit_code=1)
        self.assertIn("test-exit", result["problems"])

    def test_rejects_stderr_without_archiving_it(self):
        result = self.parse(complete_stream(), stderr="SECRET_STDERR_CANARY")
        self.assertIn("stderr-output", result["problems"])
        self.assertNotIn("SECRET_STDERR_CANARY", json.dumps(result))

    def test_allows_only_explicit_no_test_files_package(self):
        stdout = no_test_files_block()
        result = MOD.parse_go_test_json(
            stdout, "", 0, (CMD,), {}, allowed_no_test_files=(CMD,)
        )
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["package_results"][CMD]["status"], "no-test-files")
        self.assertEqual(result["root_count"], 0)

        result = MOD.parse_go_test_json(stdout, "", 0, (CMD,), {})
        self.assertIn("package-skip", result["problems"])
        self.assertIn("no-test-files", result["problems"])
        self.assertNotEqual(result["package_results"][CMD]["status"], "no-test-files")

    def test_no_test_files_requires_marker(self):
        stdout = stream(event("start", CMD), event("skip", CMD, elapsed=0))
        result = MOD.parse_go_test_json(
            stdout, "", 0, (CMD,), {}, allowed_no_test_files=(CMD,)
        )
        self.assertIn("package-no-test-files-unconfirmed", result["problems"])

    def test_no_test_files_cannot_hide_real_test_skip(self):
        stdout = stream(
            event("start", CMD),
            event("run", CMD, "TestReal"),
            event("skip", CMD, "TestReal"),
            event("pass", CMD, elapsed=0.1),
        )
        result = MOD.parse_go_test_json(
            stdout, "", 0, (CMD,), {}, allowed_no_test_files=(CMD,)
        )
        self.assertIn("test-skip", result["problems"])
        self.assertNotEqual(result["package_results"][CMD]["status"], "no-test-files")

    def test_duration_only_from_numeric_elapsed(self):
        stdout = stream(
            event("start", APP),
            event("run", APP, "TestOne"),
            event("pass", APP, "TestOne", elapsed=0.01),
            event("output", APP, output="ok  \tpayment-hub/internal/app\t9.99s\n"),
            event("pass", APP, elapsed=0.25),
        )
        result = self.parse(stdout, expected=(APP,), required={})
        self.assertEqual(result["package_results"][APP]["duration"], "0.25s")

        no_elapsed = stream(
            event("start", APP),
            event("run", APP, "TestOne"),
            event("pass", APP, "TestOne", elapsed=0.01),
            event("output", APP, output="ok  \tpayment-hub/internal/app\t9.99s\n"),
            event("pass", APP),
        )
        result = self.parse(no_elapsed, expected=(APP,), required={})
        self.assertNotIn("duration", result["package_results"][APP])

    def test_failure_diagnostics_are_safe_and_located(self):
        stdout = stream(
            event("start", APP),
            event("run", APP, "TestBad"),
            event(
                "output",
                APP,
                test="TestBad",
                output=(
                    "    schema_test.go:42: secret DSN postgres://u:p@h/db "
                    "SELECT * FROM payment_orders\n"
                ),
            ),
            event("fail", APP, "TestBad"),
            event("fail", APP, elapsed=0.1),
        )
        result = self.parse(stdout, expected=(APP,), required={})
        diagnostics = result["failure_diagnostics"]
        self.assertTrue(diagnostics)
        rendered = json.dumps(diagnostics)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("postgres://", rendered)
        self.assertNotIn("SELECT", rendered)
        test_diagnostic = [
            entry for entry in diagnostics if entry.get("test") == "TestBad"
        ][0]
        self.assertEqual(test_diagnostic["location"], "schema_test.go:42")
        self.assertEqual(test_diagnostic["category"], "test-fail")
        self.assertEqual(test_diagnostic["package"], APP)

    def test_diagnostics_redact_malicious_test_and_path(self):
        evil_test = "TestEvil/Authorization: Bearer FAKE_CANARY"
        stdout = stream(
            event("start", APP),
            event("run", APP, evil_test),
            event(
                "output",
                APP,
                test=evil_test,
                output=(
                    "    ../../FAKE_CANARY/Authorization.go:7: "
                    "Authorization: Bearer FAKE_CANARY\n"
                ),
            ),
            event("fail", APP, evil_test),
            event("pass", APP, elapsed=0.1),
        )
        result = self.parse(stdout, expected=(APP,), required={})
        rendered = json.dumps(result["failure_diagnostics"])
        self.assertNotIn("FAKE_CANARY", rendered)
        self.assertNotIn("Authorization", rendered)
        test_diagnostics = [
            entry
            for entry in result["failure_diagnostics"]
            if entry["category"] == "test-fail"
        ]
        self.assertTrue(test_diagnostics)
        self.assertTrue(
            test_diagnostics[0]["test"].startswith("TestEvil/redacted-")
        )
        self.assertEqual(test_diagnostics[0]["location"], "")

        stdout = stream(
            event("start", "evil/FAKE_CANARY"),
            event("run", "evil/FAKE_CANARY", evil_test),
            event("fail", "evil/FAKE_CANARY", evil_test),
            event("fail", "evil/FAKE_CANARY", elapsed=0.1),
        )
        result = self.parse(stdout, expected=(APP,), required={})
        rendered = json.dumps(result["failure_diagnostics"])
        self.assertNotIn("FAKE_CANARY", rendered)
        self.assertNotIn("Authorization", rendered)
        unexpected = [
            entry
            for entry in result["failure_diagnostics"]
            if entry["category"] == "unexpected-package"
        ]
        self.assertTrue(unexpected)
        self.assertEqual(unexpected[0]["package"], "unknown")

    def test_output_cannot_create_test_events(self):
        stdout = stream(
            event("start", APP),
            event(
                "output",
                APP,
                output="=== RUN   TestFake\n--- PASS: TestFake (0.00s)\n",
            ),
            event("pass", APP, elapsed=0.1),
        )
        result = self.parse(
            stdout, expected=(APP,), required={"TestFake": APP}
        )
        self.assertEqual(result["required_test_results"]["TestFake"], "missing")
        self.assertEqual(result["root_count"], 0)
        self.assertIn("required-test-missing", result["problems"])

    def test_rejects_duplicate_terminal_and_incomplete_run(self):
        duplicate = stream(
            event("start", APP),
            event("run", APP, "TestOne"),
            event("pass", APP, "TestOne", elapsed=0.01),
            event("pass", APP, "TestOne", elapsed=0.02),
            event("pass", APP, elapsed=0.1),
        )
        result = self.parse(duplicate, expected=(APP,), required={})
        self.assertIn("test-duplicate", result["problems"])

        incomplete = stream(
            event("start", APP),
            event("run", APP, "TestOne"),
            event("pass", APP, elapsed=0.1),
        )
        result = self.parse(incomplete, expected=(APP,), required={})
        self.assertIn("test-incomplete", result["problems"])

    def test_rejects_required_root_consecutive_duplicate_run(self):
        run = event("run", HARNESS, "TestRequiredRoot")
        stdout = complete_stream().replace(run, run + run, 1)
        result = self.parse(stdout)
        self.assertIn("test-run-duplicate", result["problems"])
        self.assertEqual(
            result["required_test_results"].get("TestRequiredRoot"),
            "duplicate",
        )

    def test_rejects_required_root_pass_then_rerun(self):
        run = event("run", HARNESS, "TestRequiredRoot")
        package_pass = event("pass", HARNESS, elapsed=0.34)
        stdout = complete_stream().replace(
            package_pass, run + package_pass, 1
        )
        result = self.parse(stdout)
        self.assertIn("test-run-duplicate", result["problems"])
        self.assertEqual(
            result["required_test_results"].get("TestRequiredRoot"),
            "duplicate",
        )

    def test_rejects_required_subtest_consecutive_duplicate_run(self):
        sub_run = event("run", HARNESS, "TestRequiredRoot/sub_case")
        stdout = complete_stream().replace(
            sub_run, sub_run + sub_run, 1
        )
        result = self.parse(stdout)
        self.assertIn("test-run-duplicate", result["problems"])
        self.assertEqual(
            result["required_test_results"].get(
                "TestRequiredRoot/sub_case"
            ),
            "duplicate",
        )

    def test_rejects_required_subtest_pass_then_rerun(self):
        sub_run = event("run", HARNESS, "TestRequiredRoot/sub_case")
        root_pass = event(
            "pass", HARNESS, "TestRequiredRoot", elapsed=0.02
        )
        stdout = complete_stream().replace(
            root_pass, sub_run + root_pass, 1
        )
        result = self.parse(stdout)
        self.assertIn("test-run-duplicate", result["problems"])
        self.assertEqual(
            result["required_test_results"].get(
                "TestRequiredRoot/sub_case"
            ),
            "duplicate",
        )

    def test_rejects_non_required_consecutive_duplicate_run(self):
        health_run = event("run", APP, "TestPackageHealth")
        stdout = complete_stream().replace(
            health_run, health_run + health_run, 1
        )
        result = self.parse(stdout)
        self.assertIn("test-run-duplicate", result["problems"])
        self.assertNotEqual(
            result["package_results"][APP]["status"], "pass"
        )

    def test_rejects_non_required_pass_then_rerun(self):
        health_run = event("run", APP, "TestPackageHealth")
        package_pass = event("pass", APP, elapsed=0.12)
        stdout = complete_stream().replace(
            package_pass, health_run + package_pass, 1
        )
        result = self.parse(stdout)
        self.assertIn("test-run-duplicate", result["problems"])
        self.assertNotEqual(
            result["package_results"][APP]["status"], "pass"
        )

    def test_rejects_no_tests_to_run_marker(self):
        stdout = stream(
            event("start", APP),
            event(
                "output",
                APP,
                output="testing: warning: no tests to run\nPASS\n",
            ),
            event("pass", APP, elapsed=0.1),
        )
        result = self.parse(stdout, expected=(APP,), required={})
        self.assertIn("no-tests", result["problems"])
        self.assertIn("package-no-tests", result["problems"])

    def test_required_package_must_be_expected(self):
        result = self.parse(
            package_block(APP, 0.1),
            expected=(APP,),
            required={"TestRequiredRoot": HARNESS},
        )
        self.assertIn("required-package-not-expected", result["problems"])

    def test_public_signature_is_stable(self):
        signature = inspect.signature(MOD.parse_go_test_json)
        self.assertEqual(
            list(signature.parameters),
            [
                "stdout",
                "stderr",
                "exit_code",
                "expected_packages",
                "required_tests",
                "allowed_no_test_files",
            ],
        )
        self.assertEqual(
            signature.parameters["allowed_no_test_files"].default, ()
        )


class DefaultInventoryTests(unittest.TestCase):
    def test_default_required_mapping_is_frozen(self):
        self.assertEqual(MOD.DEFAULT_REQUIRED_TESTS, EXPECTED_DEFAULT_REQUIRED)
        self.assertEqual(len(MOD.DEFAULT_REQUIRED_TESTS), 26)
        roots = [
            name
            for name in MOD.DEFAULT_REQUIRED_TESTS
            if "/" not in name
        ]
        self.assertEqual(len(roots), 26)

    def test_default_required_covers_d1_d2_and_helper(self):
        for name in (
            "TestShutdownCleanupSealFailureStillClosesDatabase",
            "TestArtifactsFinishRejectsUnclosedObserverAfterHalfFrame",
            "TestProcessReviewHelper",
        ):
            with self.subTest(name=name):
                self.assertIn(name, MOD.DEFAULT_REQUIRED_TESTS)

    def test_integration_roots_are_db_required_not_default(self):
        for name in MOD.DB_REQUIRED_TEST_KEYS:
            with self.subTest(name=name):
                self.assertIn(name, MOD.DB_REQUIRED_TEST_KEYS)
                self.assertNotIn(name, MOD.DEFAULT_REQUIRED_TESTS)

    def test_db_required_keys_are_the_four_explicit_bindings(self):
        self.assertEqual(
            MOD.DB_REQUIRED_TEST_KEYS,
            (
                "TestCommitHookIntegrationHidesReplyAfterRealCommit",
                "TestHarnessProcessPreservesParentStateIntegration",
                "TestApplicationLifecycleIntegration",
                "TestApplicationLifecycleIntegration/startup_diagnostics",
            ),
        )


class RunDefaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ci-checks-run-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report_dir = self.root / "report"
        self.report_dir.mkdir()
        self.packages = [CMD, APP, HARNESS]
        self.list_stdout = "\n".join(self.packages) + "\n"
        self.xdg_config = str(self.root / "xdg-config")
        self.controlled_env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "tmp"),
            "GOCACHE": str(self.root / "gocache"),
            "GOMODCACHE": str(self.root / "gomodcache"),
            "GOPATH": str(self.root / "gopath"),
            "XDG_CONFIG_HOME": self.xdg_config,
            "XDG_CACHE_HOME": str(self.root / "xdg-cache"),
            "XDG_DATA_HOME": str(self.root / "xdg-data"),
        }
        self.env_patch = mock.patch.dict(
            os.environ, self.controlled_env, clear=True
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def fake_go(self):
        return "/bin/true"

    def test_missing_required_cache_environment_fails_closed(self):
        for key in ("TMPDIR", "GOCACHE", "GOMODCACHE", "GOPATH"):
            with self.subTest(key=key), mock.patch.dict(os.environ, clear=True):
                os.environ.update(self.controlled_env)
                os.environ.pop(key)
                _, problems = MOD._isolated_go_env()
                self.assertTrue(problems, "missing cache must not use host defaults")

    def run_default(self, side_effect):
        with mock.patch.object(
            MOD, "_run_process_group", side_effect=side_effect
        ) as runner:
            code = MOD.run_default(self.fake_go(), str(self.report_dir))
        return code, runner

    def read_report(self):
        return json.loads((self.report_dir / "go-default.json").read_text())

    def test_run_default_writes_safe_pass_report(self):
        test_stdout = default_stream(
            self.packages, MOD.DEFAULT_REQUIRED_TESTS
        )
        with mock.patch.dict(
            os.environ,
            {
                "PAYMENT_HUB_TEST_DATABASE_URL": "postgres://secret",
                "CI_SECRET_CANARY": "secret",
            },
        ):
            code, runner = self.run_default(
                [proc(0, self.list_stdout), proc(0, test_stdout)]
            )
        self.assertEqual(code, 0)
        self.assertEqual(runner.call_count, 2)
        list_argv = runner.call_args_list[0].args[0]
        test_argv = runner.call_args_list[1].args[0]
        self.assertEqual(
            list_argv,
            [self.fake_go(), "list", "-mod=readonly", "./..."],
        )
        self.assertEqual(
            test_argv,
            [
                self.fake_go(),
                "test",
                "-mod=readonly",
                "-race",
                "-count=1",
                "-p=1",
                "-timeout=90s",
                "-json",
                "./...",
            ],
        )
        env = runner.call_args_list[1].args[1]
        self.assertNotIn("PAYMENT_HUB_TEST_DATABASE_URL", env)
        self.assertNotIn("CI_SECRET_CANARY", env)
        self.assertEqual(env["GOPROXY"], "off")
        self.assertEqual(env["GOSUMDB"], "off")
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertEqual(env["GOENV"], "off")
        self.assertEqual(env.get("XDG_CONFIG_HOME"), self.xdg_config)
        self.assertEqual(
            env.get("XDG_CACHE_HOME"),
            self.controlled_env["XDG_CACHE_HOME"],
        )
        self.assertEqual(
            env.get("XDG_DATA_HOME"),
            self.controlled_env["XDG_DATA_HOME"],
        )
        report = self.read_report()
        self.assertEqual(report["schema"], "payment-hub.ci-go-default.v1")
        self.assertEqual(report["result"], "pass")
        self.assertEqual(report["expected_packages"], self.packages)
        self.assertEqual(report["required_test_results"], {
            name: "pass" for name in MOD.DEFAULT_REQUIRED_TESTS
        })
        self.assertTrue(report["root_count"] > 0)
        self.assertRegex(report["input_hash"], r"^[0-9a-f]{64}$")
        rendered = json.dumps(report)
        self.assertNotIn("Output", rendered)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("PAYMENT_HUB_TEST_DATABASE_URL", rendered)

    def test_run_default_accepts_output_type_enum_and_export_hides_output(self):
        test_stdout = default_stream(
            self.packages, MOD.DEFAULT_REQUIRED_TESTS
        )
        canary = "OUTPUT_TYPE_CANARY"
        marker = event("run", APP, "TestPackageHealth")
        replacement = (
            marker
            + event(
                "output",
                APP,
                test="TestPackageHealth",
                output="error " + canary + "\n",
                OutputType="error-continue",
            )
            + event(
                "output",
                APP,
                output="frame " + canary + "\n",
                OutputType="frame",
            )
        )
        test_stdout = test_stdout.replace(marker, replacement, 1)
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(0, test_stdout)]
        )
        self.assertEqual(code, 0)
        report = self.read_report()
        self.assertEqual(report["result"], "pass")
        self.assertNotIn(canary, json.dumps(report))
        db_root = self.root / "empty-db-root"
        db_root.mkdir()
        export_code = MOD.export_reports(
            str(self.report_dir), str(db_root), ["default"]
        )
        self.assertEqual(export_code, 0)
        safe_text = (
            self.report_dir / "upload" / "go-default.safe.json"
        ).read_text()
        self.assertNotIn(canary, safe_text)

    def test_run_default_rejects_unknown_output_type(self):
        test_stdout = default_stream(
            self.packages, MOD.DEFAULT_REQUIRED_TESTS
        )
        marker = event("run", APP, "TestPackageHealth")
        test_stdout = test_stdout.replace(
            marker,
            marker
            + event(
                "output",
                APP,
                output="bad\n",
                OutputType="bogus",
            ),
            1,
        )
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(0, test_stdout)]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertIn("malformed-event", report["problems"])

    def test_run_default_rejects_duplicate_required_run(self):
        target = "TestStartupDiagnosticsDatabaseOpenFailure"
        test_stdout = default_stream(
            self.packages, MOD.DEFAULT_REQUIRED_TESTS
        )
        marker = event("run", APP, target)
        self.assertEqual(test_stdout.count(marker), 1)
        test_stdout = test_stdout.replace(
            marker, marker + marker, 1
        )
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(0, test_stdout)]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertIn("test-run-duplicate", report["problems"])
        self.assertEqual(
            report["required_test_results"].get(target), "duplicate"
        )

    def test_run_default_list_failure_writes_fail_report(self):
        code, runner = self.run_default([proc(1, "", "SECRET_LIST_STDERR")])
        self.assertEqual(code, 1)
        self.assertEqual(runner.call_count, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertIn("go-list-exit", report["problems"])
        self.assertNotIn("SECRET_LIST_STDERR", json.dumps(report))

    def test_run_default_test_failure_not_masked_by_summary(self):
        test_stdout = default_stream(
            self.packages, MOD.DEFAULT_REQUIRED_TESTS
        )
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(1, test_stdout)]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertIn("test-exit", report["problems"])
        self.assertTrue(
            all(
                value == "pass"
                for value in report["required_test_results"].values()
            )
        )

    def test_run_default_missing_required_root_fails(self):
        test_stdout = default_stream(
            self.packages, MOD.DEFAULT_REQUIRED_TESTS
        )
        missing = "TestStartupDiagnosticsDatabaseOpenFailure"
        test_stdout = "\n".join(
            line
            for line in test_stdout.splitlines()
            if missing not in line
        ) + "\n"
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(0, test_stdout)]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertEqual(
            report["required_test_results"].get(missing), "missing"
        )

    def test_run_default_timeout_writes_fail_report(self):
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(124, "", "", timed_out=True)]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertIn("timeout", report["problems"])
        self.assertTrue(report["timed_out"])

    def test_run_default_malformed_json_writes_fail_report(self):
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(0, "not json\n")]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertIn("non-json-output", report["problems"])

    def test_run_default_rejects_relative_or_missing_go(self):
        code = MOD.run_default("go", str(self.report_dir))
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertIn("go-not-absolute", report["problems"])

        code = MOD.run_default(
            str(self.root / "missing-go"), str(self.report_dir)
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertIn("go-not-executable", report["problems"])

    def test_run_default_rejects_relative_report_dir(self):
        code = MOD.run_default("/bin/true", "relative-report-dir")
        self.assertEqual(code, 1)

    def test_run_default_rejects_missing_xdg_config(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(
                MOD,
                "_run_process_group",
                return_value=proc(1, "", ""),
            ) as runner:
                code = MOD.run_default(
                    self.fake_go(), str(self.report_dir)
                )
        self.assertEqual(code, 1)
        runner.assert_not_called()
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertIn("env-xdg-config-missing", report["problems"])

    def test_run_default_rejects_relative_xdg_config(self):
        env = dict(self.controlled_env)
        env["XDG_CONFIG_HOME"] = "relative-xdg-config"
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(
                MOD,
                "_run_process_group",
                return_value=proc(1, "", ""),
            ) as runner:
                code = MOD.run_default(
                    self.fake_go(), str(self.report_dir)
                )
        self.assertEqual(code, 1)
        runner.assert_not_called()
        report = self.read_report()
        self.assertIn("env-relative-xdg-config-home", report["problems"])

    def test_run_default_reap_unconfirmed_is_fail(self):
        class ReapResult:
            exit_code = 124
            stdout = b""
            stderr = b""
            timed_out = True
            reaped = False

        code, _ = self.run_default(
            [proc(0, self.list_stdout), ReapResult()]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertIn("process-reap-unconfirmed", report["problems"])

    def test_run_default_does_not_archive_raw_output(self):
        test_stdout = default_stream(
            self.packages, MOD.DEFAULT_REQUIRED_TESTS
        ).replace(
            event("start", APP),
            event(
                "output",
                APP,
                output="SECRET_RAW_OUTPUT_CANARY\n",
            )
            + event("start", APP),
            1,
        )
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(0, test_stdout)]
        )
        self.assertEqual(code, 0)
        self.assertNotIn(
            "SECRET_RAW_OUTPUT_CANARY",
            (self.report_dir / "go-default.json").read_text(),
        )


class ProcessGroupTests(unittest.TestCase):
    class FakeProcess:
        pid = 4242

        def __init__(self, effects, poll_value=-9):
            self.effects = list(effects)
            self.poll_value = poll_value
            self.calls = 0
            self.returncode = -9 if poll_value is None else poll_value

        def communicate(self, timeout=None):
            self.calls += 1
            effect = self.effects.pop(0) if self.effects else (b"", b"")
            if isinstance(effect, BaseException):
                raise effect
            return effect

        def poll(self):
            return self.poll_value

        def wait(self, timeout=None):
            return self.returncode

    def test_timeout_kills_group_and_returns_124(self):
        fake = self.FakeProcess(
            [
                subprocess.TimeoutExpired(["go", "test"], 120),
                (b"out", b"err"),
            ]
        )
        with mock.patch.object(
            MOD.subprocess, "Popen", return_value=fake
        ) as popen, mock.patch.object(MOD.os, "killpg") as killpg:
            result = MOD._run_process_group(
                ["go", "test"], {"PATH": "/bin"}, 120
            )
        self.assertTrue(result.timed_out)
        self.assertTrue(getattr(result, "reaped", False))
        self.assertEqual(result.exit_code, 124)
        self.assertEqual(result.stdout, b"out")
        self.assertEqual(result.stderr, b"err")
        killpg.assert_called_once_with(fake.pid, signal.SIGKILL)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_timeout_reap_unconfirmed_is_not_reaped(self):
        fake = self.FakeProcess(
            [
                subprocess.TimeoutExpired(["go", "test"], 120),
                subprocess.TimeoutExpired(["go", "test"], 5),
            ],
            poll_value=None,
        )
        with mock.patch.object(
            MOD.subprocess, "Popen", return_value=fake
        ), mock.patch.object(MOD.os, "killpg") as killpg:
            result = MOD._run_process_group(
                ["go", "test"], {"PATH": "/bin"}, 120
            )
        self.assertTrue(result.timed_out)
        self.assertFalse(getattr(result, "reaped", True))
        self.assertEqual(result.exit_code, 124)
        killpg.assert_called_once_with(fake.pid, signal.SIGKILL)

    def test_oserror_kills_group_and_maps_ci_error(self):
        fake = self.FakeProcess([OSError("pipe"), (b"", b"")])
        with mock.patch.object(
            MOD.subprocess, "Popen", return_value=fake
        ), mock.patch.object(MOD.os, "killpg") as killpg:
            with self.assertRaises(MOD.CiError) as raised:
                MOD._run_process_group(
                    ["go", "test"], {"PATH": "/bin"}, 120
                )
        self.assertEqual(raised.exception.category, "process-io-failed")
        killpg.assert_called_once_with(fake.pid, signal.SIGKILL)

    def test_keyboard_interrupt_kills_group_and_maps_ci_error(self):
        fake = self.FakeProcess([KeyboardInterrupt(), (b"", b"")])
        category = None
        with mock.patch.object(
            MOD.subprocess, "Popen", return_value=fake
        ), mock.patch.object(MOD.os, "killpg") as killpg:
            try:
                MOD._run_process_group(
                    ["go", "test"], {"PATH": "/bin"}, 120
                )
            except MOD.CiError as exc:
                category = exc.category
            except BaseException as exc:
                self.fail(
                    "unexpected exception " + type(exc).__name__
                )
            else:
                self.fail("CiError not raised")
        self.assertEqual(category, "process-interrupted")
        killpg.assert_called_once_with(fake.pid, signal.SIGKILL)


class ExportReportsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ci-checks-export-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report_dir = self.root / "report"
        self.report_dir.mkdir()
        self.db_root = self.root / "db-artifacts"
        self.db_root.mkdir()
        self.run_id = "a" * 32

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def go_default(self, result="pass"):
        return {
            "schema": "payment-hub.ci-go-default.v1",
            "generated_at": "2026-11-08T00:00:00+00:00",
            "result": result,
            "expected_packages": [APP, HARNESS, CMD],
            "package_results": {
                APP: {"status": "pass", "duration": "0.1s"},
                HARNESS: {"status": "pass", "duration": "0.2s"},
                CMD: {"status": "no-test-files"},
            },
            "required_test_results": {
                name: "pass" for name in MOD.DEFAULT_REQUIRED_TESTS
            },
            "root_count": 3,
            "subtest_count": 1,
            "problems": [],
            "failure_diagnostics": [],
            "exit_code": 0,
            "list_exit_code": 0,
            "timed_out": False,
            "input_hash": "b" * 64,
            "raw_secret": "SECRET_RAW_FIELD",
            "command": {"argv": ["SECRET_COMMAND"]},
        }

    def manifest_digest(self, manifest):
        digest = hashlib.sha256()
        for entry in manifest:
            digest.update(entry["path"].encode("utf-8"))
            digest.update(b"\x00")
            digest.update(entry["sha256"].encode("utf-8"))
            digest.update(b"\x00")
        return digest.hexdigest()

    def db_report(self, result="pass"):
        manifest = [
            {"path": "go.mod", "sha256": "c" * 64},
            {"path": "scripts/check-db-foundation.py", "sha256": "d" * 64},
        ]
        required = {name: "pass" for name in MOD.DB_REQUIRED_TEST_KEYS}
        if result != "pass":
            required[MOD.DB_REQUIRED_TEST_KEYS[0]] = "fail"
        package_results = {
            "internal/postgres": {"status": "pass", "duration": "0.1s"},
            "internal/mock": {"status": "pass", "duration": "0.2s"},
            "tests/harness": {
                "status": "pass" if result == "pass" else "fail",
                "duration": "0.3s",
            },
        }
        return {
            "schema": "payment-hub.db-foundation.v1",
            "run_id": self.run_id,
            "project": "payment-hub-test-db-" + self.run_id,
            "platform": "linux/amd64",
            "started_at": "2026-11-08T00:00:00+00:00",
            "finished_at": "2026-11-08T00:01:00+00:00",
            "result": result,
            "failure": None if result == "pass" else {
                "stage": "test",
                "kind": "test-failed",
            },
            "source": {
                "manifest_digest": self.manifest_digest(manifest),
                "manifest_unchanged": True,
                "manifest": manifest,
            },
            "tests": {
                "required_test_results": required,
                "package_results": package_results,
                "vet_exit": 0,
                "test_exit": 0,
                "problems": [],
            },
            "cleanup": {
                "result": "pass" if result == "pass" else "fail",
                "errors": [] if result == "pass" else ["cleanup:volume"],
                "repeat_signal_ignored": False,
                "network_removed": True,
                "volume_removed": result == "pass",
                "volume_retained": False,
                "compose_down_exit": 0,
            },
            "versions": {
                "go": "go1.27.3",
                "postgres": "16.15",
                "goose": "v3.29.0",
            },
            "commands": [
                {"argv": ["psql", "postgres://user:pass@host/db"]}
            ],
            "diagnostics": ["SELECT * FROM payment_orders"],
            "env": {"PAYMENT_HUB_TEST_DATABASE_URL": "postgres://secret"},
            "unknown_field": "SECRET_RAW_DB",
        }

    def write_db(self, value, run_id=None):
        run_id = value.get("run_id", self.run_id) if run_id is None else run_id
        path = self.db_root / run_id / "db-foundation.json"
        self.write(path, value)
        return path

    def export(self, expect=None):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = MOD.export_reports(
                str(self.report_dir),
                str(self.db_root),
                expect if expect is not None else ["default"],
            )
        return code, stdout.getvalue()

    def test_export_default_safe_whitelist(self):
        self.write(self.report_dir / "go-default.json", self.go_default())
        code, output = self.export()
        self.assertEqual(code, 0)
        safe_path = self.report_dir / "upload" / "go-default.safe.json"
        self.assertTrue(safe_path.is_file())
        text = safe_path.read_text()
        safe = json.loads(text)
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "pass")
        self.assertNotIn("SECRET_RAW_FIELD", text)
        self.assertNotIn("SECRET_COMMAND", text)
        self.assertNotIn("command", text)
        provenance = json.loads(
            (self.report_dir / "upload" / "provenance.json").read_text()
        )
        self.assertEqual(provenance["reports"][0]["status"], "exported")
        self.assertEqual(
            provenance["reports"][0]["export_sha256"],
            hashlib.sha256(safe_path.read_bytes()).hexdigest(),
        )
        self.assertIn("safe-manifest:", output)

    def test_export_missing_default_generates_missing_and_nonzero(self):
        code, _ = self.export()
        self.assertEqual(code, 1)
        safe_path = self.report_dir / "upload" / "go-default.safe.json"
        self.assertTrue(safe_path.is_file())
        safe = json.loads(safe_path.read_text())
        self.assertEqual(safe["status"], "missing")
        provenance = json.loads(
            (self.report_dir / "upload" / "provenance.json").read_text()
        )
        self.assertEqual(provenance["reports"][0]["status"], "missing")

    def test_export_db_rejects_unsafe_version_string(self):
        value = self.db_report()
        value["versions"]["go"] = "postgres://u:p@h/db"
        self.write_db(value)
        code, _ = self.export(["db"])
        self.assertEqual(code, 1)
        text = (
            self.report_dir / "upload" / "db-foundation.safe.json"
        ).read_text()
        self.assertNotIn("postgres://", text)
        safe = json.loads(text)
        self.assertEqual(safe["status"], "error")

    def test_export_vulnerability_rejects_unsafe_scan_field(self):
        value = self.vulnerability_report("vulnerable")
        value["scan"]["db"] = "postgres://u:p@h/db"
        self.write(self.report_dir / "vulnerability-report.json", value)
        code, _ = self.export(["vulnerability"])
        self.assertEqual(code, 1)
        text = (
            self.report_dir / "upload" / "vulnerability.safe.json"
        ).read_text()
        self.assertNotIn("postgres://", text)
        safe = json.loads(text)
        self.assertEqual(safe["status"], "error")

    def test_export_default_rejects_unsafe_diagnostic_category(self):
        value = self.go_default()
        value["failure_diagnostics"] = [
            {
                "package": APP,
                "test": "TestBad",
                "location": "schema_test.go:42",
                "category": "postgres://u:p@h/db",
            }
        ]
        self.write(self.report_dir / "go-default.json", value)
        code, _ = self.export()
        self.assertEqual(code, 1)
        text = (
            self.report_dir / "upload" / "go-default.safe.json"
        ).read_text()
        self.assertNotIn("postgres://", text)
        safe = json.loads(text)
        self.assertEqual(safe["status"], "error")

    def test_export_default_rejects_malicious_test_metadata(self):
        value = self.go_default()
        value["result"] = "fail"
        value["failure_diagnostics"] = [
            {
                "package": APP,
                "test": "TestEvil/Authorization: Bearer FAKE_CANARY",
                "location": "../../FAKE_CANARY/Authorization.go:7",
                "category": "test-fail",
            }
        ]
        self.write(self.report_dir / "go-default.json", value)
        code, _ = self.export()
        self.assertEqual(code, 1)
        text = (
            self.report_dir / "upload" / "go-default.safe.json"
        ).read_text()
        self.assertNotIn("FAKE_CANARY", text)
        self.assertNotIn("Authorization", text)
        safe = json.loads(text)
        self.assertEqual(safe["status"], "error")

    def test_export_default_accepts_redacted_diagnostic(self):
        value = self.go_default()
        value["result"] = "fail"
        value["failure_diagnostics"] = [
            {
                "package": APP,
                "test": "TestEvil/redacted-deadbeef",
                "location": "schema_test.go:42",
                "category": "test-fail",
            }
        ]
        self.write(self.report_dir / "go-default.json", value)
        code, _ = self.export()
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "go-default.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")
        self.assertEqual(
            safe["failure_diagnostics"][0]["test"],
            "TestEvil/redacted-deadbeef",
        )
        self.assertEqual(
            safe["failure_diagnostics"][0]["location"],
            "schema_test.go:42",
        )

    def test_export_default_accepts_duplicate_run_problem(self):
        value = self.go_default()
        value["result"] = "fail"
        value["problems"] = ["test-run-duplicate"]
        self.write(self.report_dir / "go-default.json", value)
        code, _ = self.export()
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "go-default.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")
        self.assertEqual(
            safe.get("problems"), ["test-run-duplicate"]
        )

    def test_export_default_failure_before_list_is_safe(self):
        value = {
            "schema": "payment-hub.ci-go-default.v1",
            "generated_at": "2026-11-08T00:00:00+00:00",
            "result": "fail",
            "expected_packages": [],
            "package_results": {},
            "required_test_results": {},
            "root_count": 0,
            "subtest_count": 0,
            "problems": ["go-list-exit"],
            "failure_diagnostics": [],
            "exit_code": 1,
            "list_exit_code": 1,
            "timed_out": False,
            "input_hash": "a" * 64,
            "raw_secret": "SECRET_RAW_FIELD",
        }
        self.write(self.report_dir / "go-default.json", value)
        code, _ = self.export()
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "go-default.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")
        self.assertEqual(safe["expected_packages"], [])
        self.assertEqual(safe["package_results"], {})
        self.assertNotIn("SECRET_RAW_FIELD", json.dumps(safe))

    def test_export_db_failure_before_tests_is_safe(self):
        value = self.db_report(result="fail")
        value["source"] = {}
        value["tests"] = {
            "required_test_results": {
                name: "missing" for name in MOD.DB_REQUIRED_TEST_KEYS
            }
        }
        value["versions"] = {}
        value["failure"] = {"stage": "environment", "kind": "docker"}
        self.write_db(value)
        code, _ = self.export(["db"])
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "db-foundation.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")
        self.assertEqual(safe["package_results"], {})
        self.assertEqual(safe["source"]["files"], [])
        self.assertIsNone(safe["source"]["manifest_digest"])
        self.assertTrue(
            all(
                value == "missing"
                for value in safe["required_test_results"].values()
            )
        )

    def db_timeout_report(self, manifest_unchanged="omit"):
        """Main-run shape: result fail, source manifest collected, no verify."""
        value = self.db_report(result="fail")
        value["failure"] = {
            "stage": "test",
            "kind": "timeout",
            "message": (
                "rawOutput SECRET_MAIN_CI_RAW "
                "Authorization: Bearer FAKE_CANARY_TOKEN"
            ),
        }
        value["tests"] = {
            "required_test_results": {
                name: "missing" for name in MOD.DB_REQUIRED_TEST_KEYS
            },
        }
        value["cleanup"] = {
            "result": "pass",
            "errors": [],
            "repeat_signal_ignored": False,
            "network_removed": True,
            "volume_removed": True,
            "volume_retained": False,
            "compose_down_exit": 0,
        }
        value["commands"] = [
            {
                "argv": [
                    "psql",
                    "postgres://FAKE_USER:FAKE_PASSWORD@FAKE_HOST/FAKE_DB",
                ]
            }
        ]
        value["diagnostics"] = ["SELECT * FROM FAKE_ORDERS"]
        value["env"] = {
            "PAYMENT_HUB_TEST_DATABASE_URL": (
                "postgres://FAKE_USER:FAKE_PASSWORD@FAKE_HOST/FAKE_DB"
            )
        }
        value["raw_output"] = "rawOutput SECRET_MAIN_CI_RAW"
        value["authorization"] = "Authorization: Bearer FAKE_CANARY_TOKEN"
        value["unknown_field"] = "SECRET_MAIN_CI_UNKNOWN"
        if manifest_unchanged == "omit":
            value["source"].pop("manifest_unchanged", None)
        else:
            value["source"]["manifest_unchanged"] = manifest_unchanged
        return value

    def read_safe_db(self):
        return json.loads(
            (
                self.report_dir / "upload" / "db-foundation.safe.json"
            ).read_text()
        )

    def assert_no_main_run_canaries(self, text):
        for forbidden in (
            "SECRET_MAIN_CI_RAW",
            "rawOutput",
            "FAKE_CANARY_TOKEN",
            "Authorization",
            "postgres://",
            "SELECT",
            "FAKE_ORDERS",
            "SECRET_MAIN_CI_UNKNOWN",
            "raw_output",
            "unknown_field",
            '"commands":',
            '"diagnostics":',
            '"env":',
        ):
            self.assertNotIn(forbidden, text)

    def test_export_db_fail_missing_manifest_unchanged_is_not_verified(self):
        value = self.db_timeout_report()
        self.assertNotIn("manifest_unchanged", value["source"])
        self.write_db(value)
        code, _ = self.export(["db"])
        self.assertEqual(code, 1)
        safe_path = self.report_dir / "upload" / "db-foundation.safe.json"
        text = safe_path.read_text()
        safe = json.loads(text)
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")
        self.assertEqual(
            safe["failure"], {"stage": "test", "kind": "timeout"}
        )
        self.assertIsNone(safe["source"]["manifest_unchanged"])
        self.assertEqual(
            safe["source"].get("manifest_verification"), "not-verified"
        )
        self.assertEqual(
            safe["source"]["manifest_digest"],
            value["source"]["manifest_digest"],
        )
        self.assertEqual(safe["source"]["file_count"], 2)
        self.assertEqual(
            safe["source"]["files"], value["source"]["manifest"]
        )
        self.assertIsNone(safe["vet_exit"])
        self.assertIsNone(safe["test_exit"])
        self.assertEqual(safe["package_results"], {})
        self.assertEqual(safe["cleanup"]["result"], "pass")
        self.assertTrue(
            all(
                status == "missing"
                for status in safe["required_test_results"].values()
            )
        )
        self.assert_no_main_run_canaries(text)
        provenance = json.loads(
            (self.report_dir / "upload" / "provenance.json").read_text()
        )
        self.assertEqual(provenance["errors"], [])
        self.assertEqual(provenance["expect"], ["db"])
        self.assertEqual(provenance["reports"][0]["status"], "exported")
        self.assertEqual(provenance["reports"][0]["result"], "fail")
        self.assertNotIn("error", provenance["reports"][0])
        self.assertEqual(
            provenance["reports"][0]["export_sha256"],
            hashlib.sha256(safe_path.read_bytes()).hexdigest(),
        )

    def test_export_db_fail_explicit_none_manifest_unchanged_is_same(self):
        self.write_db(self.db_timeout_report(manifest_unchanged=None))
        code, _ = self.export(["db"])
        self.assertEqual(code, 1)
        safe = self.read_safe_db()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")
        self.assertIsNone(safe["source"]["manifest_unchanged"])
        self.assertEqual(
            safe["source"].get("manifest_verification"), "not-verified"
        )
        provenance = json.loads(
            (self.report_dir / "upload" / "provenance.json").read_text()
        )
        self.assertEqual(provenance["errors"], [])
        self.assertEqual(provenance["reports"][0]["status"], "exported")

    def test_export_db_fail_rejects_non_bool_manifest_unchanged(self):
        for marker in ("true", 1, [], {}):
            with self.subTest(marker=marker):
                self.write_db(
                    self.db_timeout_report(manifest_unchanged=marker)
                )
                code, _ = self.export(["db"])
                self.assertEqual(code, 1)
                safe = self.read_safe_db()
                self.assertEqual(safe["status"], "error")
                self.assertEqual(
                    safe["error"], "unsafe-field-source.manifest_unchanged"
                )

    def test_export_db_fail_bool_manifest_unchanged_keeps_semantics(self):
        for marker, expected, verification in (
            (True, True, "verified-unchanged"),
            (False, False, "verified-changed"),
        ):
            with self.subTest(marker=marker):
                self.write_db(
                    self.db_timeout_report(manifest_unchanged=marker)
                )
                code, _ = self.export(["db"])
                self.assertEqual(code, 1)
                safe = self.read_safe_db()
                self.assertEqual(safe["status"], "exported")
                self.assertEqual(safe["result"], "fail")
                self.assertIs(
                    safe["source"]["manifest_unchanged"], expected
                )
                self.assertEqual(
                    safe["source"].get("manifest_verification"),
                    verification,
                )

    def test_export_db_pass_requires_explicit_true_manifest_unchanged(self):
        for marker in ("omit", None, False, "true", 1):
            with self.subTest(marker=marker):
                value = self.db_report()
                if marker == "omit":
                    del value["source"]["manifest_unchanged"]
                else:
                    value["source"]["manifest_unchanged"] = marker
                self.write_db(value)
                code, _ = self.export(["db"])
                self.assertEqual(code, 1)
                safe = self.read_safe_db()
                self.assertEqual(safe["status"], "error")
                self.assertEqual(
                    safe["error"], "unsafe-field-source.manifest_unchanged"
                )
                self.assertNotEqual(safe.get("result"), "pass")

    def test_export_db_fail_empty_source_is_unknown_not_changed(self):
        value = self.db_report(result="fail")
        value["source"] = {}
        value["tests"] = {
            "required_test_results": {
                name: "missing" for name in MOD.DB_REQUIRED_TEST_KEYS
            },
        }
        self.write_db(value)
        code, _ = self.export(["db"])
        self.assertEqual(code, 1)
        safe = self.read_safe_db()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")
        self.assertIsNone(safe["source"]["manifest_unchanged"])
        self.assertEqual(
            safe["source"].get("manifest_verification"), "not-verified"
        )
        self.assertIsNone(safe["source"]["manifest_digest"])
        self.assertEqual(safe["source"]["files"], [])

    def test_export_db_fail_manifest_security_still_rejected(self):
        def with_manifest(manifest, digest=None):
            value = self.db_timeout_report()
            value["source"]["manifest"] = manifest
            value["source"]["manifest_digest"] = (
                self.manifest_digest(manifest)
                if digest is None
                else digest
            )
            return value

        cases = {
            "digest-mismatch": with_manifest(
                self.db_timeout_report()["source"]["manifest"],
                digest="e" * 64,
            ),
            "path-traversal": with_manifest(
                [{"path": "../../etc/passwd", "sha256": "c" * 64}]
            ),
            "bad-hash": with_manifest(
                [{"path": "go.mod", "sha256": "not-a-hash"}]
            ),
            "duplicate": with_manifest(
                [
                    {"path": "go.mod", "sha256": "c" * 64},
                    {"path": "go.mod", "sha256": "d" * 64},
                ]
            ),
            "order": with_manifest(
                [
                    {
                        "path": "scripts/check-db-foundation.py",
                        "sha256": "d" * 64,
                    },
                    {"path": "go.mod", "sha256": "c" * 64},
                ]
            ),
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                self.write_db(value)
                code, _ = self.export(["db"])
                self.assertEqual(code, 1)
                text = (
                    self.report_dir
                    / "upload"
                    / "db-foundation.safe.json"
                ).read_text()
                safe = json.loads(text)
                self.assertEqual(safe["status"], "error")
                self.assertNotIn("../../etc/passwd", text)
                self.assertNotIn("not-a-hash", text)

    def test_cli_export_db_fail_timeout_is_exported_but_nonzero(self):
        self.write_db(self.db_timeout_report())
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(stderr):
                code = MOD.main(
                    [
                        "export-reports",
                        "--report-dir",
                        str(self.report_dir),
                        "--db-artifact-root",
                        str(self.db_root),
                        "--expect",
                        "db",
                    ]
                )
        self.assertEqual(code, 1)
        safe = self.read_safe_db()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")
        self.assertEqual(
            safe["failure"], {"stage": "test", "kind": "timeout"}
        )
        self.assertIsNone(safe["source"]["manifest_unchanged"])
        self.assertEqual(
            safe["source"].get("manifest_verification"), "not-verified"
        )
        provenance = json.loads(
            (self.report_dir / "upload" / "provenance.json").read_text()
        )
        self.assertEqual(provenance["errors"], [])
        self.assertEqual(provenance["expect"], ["db"])
        self.assertEqual(provenance["reports"][0]["status"], "exported")
        self.assertEqual(provenance["reports"][0]["result"], "fail")
        self.assertNotIn("unsafe-field", stderr.getvalue())
        self.assertNotIn("unsafe-field", json.dumps(provenance))

    def test_export_missing_cancellation_is_placeholder_non_green(self):
        self.write_db(self.db_timeout_report())
        code, _ = self.export(["db", "cancellation"])
        self.assertEqual(code, 1)
        cancellation = json.loads(
            (
                self.report_dir / "upload" / "cancellation.safe.json"
            ).read_text()
        )
        self.assertEqual(cancellation["status"], "missing")
        provenance = json.loads(
            (self.report_dir / "upload" / "provenance.json").read_text()
        )
        statuses = {
            entry["name"]: entry["status"]
            for entry in provenance["reports"]
        }
        self.assertEqual(
            statuses, {"db": "exported", "cancellation": "missing"}
        )

    def test_export_db_safe_rebuild(self):
        self.write_db(self.db_report())
        code, _ = self.export(["db"])
        self.assertEqual(code, 0)
        safe_path = self.report_dir / "upload" / "db-foundation.safe.json"
        self.assertTrue(safe_path.is_file())
        text = safe_path.read_text()
        safe = json.loads(text)
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "pass")
        self.assertEqual(safe["run_id"], self.run_id)
        self.assertEqual(
            safe["source"]["manifest_digest"],
            self.db_report()["source"]["manifest_digest"],
        )
        self.assertEqual(len(safe["source"]["files"]), 2)
        self.assertIn("required_test_results", safe)
        self.assertIn("package_results", safe)
        self.assertIn("cleanup", safe)
        self.assertIn("versions", safe)
        for forbidden in (
            "postgres://",
            "SELECT",
            '"commands":',
            '"diagnostics":',
            '"env":',
            "SECRET_RAW_DB",
            "unknown_field",
        ):
            self.assertNotIn(forbidden, text)

    def test_export_db_digest_mismatch_fails_without_raw_fields(self):
        value = self.db_report()
        value["source"]["manifest_digest"] = "e" * 64
        self.write_db(value)
        code, _ = self.export(["db"])
        self.assertEqual(code, 1)
        text = (
            self.report_dir / "upload" / "db-foundation.safe.json"
        ).read_text()
        safe = json.loads(text)
        self.assertEqual(safe["status"], "error")
        self.assertNotIn("postgres://", text)
        self.assertNotIn("SECRET_RAW_DB", text)

    def test_export_db_ambiguous_runs_fail(self):
        self.write_db(self.db_report())
        other = "f" * 32
        value = self.db_report()
        value["run_id"] = other
        value["project"] = "payment-hub-test-db-" + other
        self.write_db(value, run_id=other)
        code, _ = self.export(["db"])
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "db-foundation.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "error")
        self.assertEqual(safe["error"], "db-run-ambiguous")

    def test_export_db_rejects_path_traversal_manifest(self):
        value = self.db_report()
        value["source"]["manifest"] = [
            {"path": "../../etc/passwd", "sha256": "c" * 64}
        ]
        value["source"]["manifest_digest"] = self.manifest_digest(
            value["source"]["manifest"]
        )
        self.write_db(value)
        code, _ = self.export(["db"])
        self.assertEqual(code, 1)
        text = (
            self.report_dir / "upload" / "db-foundation.safe.json"
        ).read_text()
        self.assertNotIn("../../etc/passwd", text)

    def test_export_db_rejects_run_id_directory_mismatch(self):
        value = self.db_report()
        value["run_id"] = "b" * 32
        self.write_db(value, run_id=self.run_id)
        code, _ = self.export(["db"])
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "db-foundation.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "error")

    def test_export_db_fail_result_is_preserved(self):
        self.write_db(self.db_report(result="fail"))
        code, _ = self.export(["db"])
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "db-foundation.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")

    def test_export_db_pass_nonzero_test_exit_is_fail(self):
        value = self.db_report()
        value["tests"]["test_exit"] = 1
        self.write_db(value)
        code, _ = self.export(["db"])
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "db-foundation.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")
        self.assertEqual(safe.get("test_exit"), 1)

    def test_export_db_pass_problems_is_fail(self):
        value = self.db_report()
        value["tests"]["problems"] = ["test-failure"]
        self.write_db(value)
        code, _ = self.export(["db"])
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "db-foundation.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")
        self.assertEqual(safe.get("problems"), ["test-failure"])

    def test_export_db_pass_missing_vet_exit_is_fail(self):
        value = self.db_report()
        del value["tests"]["vet_exit"]
        self.write_db(value)
        code, _ = self.export(["db"])
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "db-foundation.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")
        self.assertIsNone(safe.get("vet_exit"))

    def test_export_db_pass_incomplete_cleanup_is_fail(self):
        value = self.db_report()
        value["cleanup"]["network_removed"] = False
        self.write_db(value)
        code, _ = self.export(["db"])
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "db-foundation.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")
        self.assertFalse(safe["cleanup"]["network_removed"])

    def test_export_missing_declared_db_is_explicit(self):
        self.write(self.report_dir / "go-default.json", self.go_default())
        code, _ = self.export(["default", "db"])
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "db-foundation.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "missing")
        provenance = json.loads(
            (self.report_dir / "upload" / "provenance.json").read_text()
        )
        statuses = {entry["name"]: entry["status"] for entry in provenance["reports"]}
        self.assertEqual(statuses["default"], "exported")
        self.assertEqual(statuses["db"], "missing")

    def vulnerability_report(self, result="vulnerable"):
        if result == "pass":
            reachable = []
            unreachable = ["GO-2024-0002"]
        elif result == "vulnerable":
            reachable = ["GO-2024-0001"]
            unreachable = ["GO-2024-0002"]
        else:
            reachable = []
            unreachable = []
        osv_ids = sorted(set(reachable) | set(unreachable))
        trusted = result in {"pass", "vulnerable"}
        scan = {
            "mode": "source",
            "level": "symbol",
            "tags": "integration",
            "test": True,
            "db": "vulndb",
            "pattern": "./...",
            "cgo_enabled": "1",
            "goos": "linux",
            "goarch": "amd64",
        }
        return {
            "schema": "payment-hub.ci.vulnerability-report.v1",
            "version": 1,
            "result": result,
            "tool": {
                "name": "govulncheck",
                "module": "golang.org/x/vuln/cmd/govulncheck",
                "version": "v1.9.0",
                "commit": "e672cbe8fbc8c76e28fcb6db0efd9ce354e0dac5",
            },
            "tool_verified": trusted,
            "expected_go_version": "go1.27.3",
            "go_version": "go1.27.3" if trusted else None,
            "source_hash": {
                "go.mod": "b" * 64,
                "go.sum": "c" * 64,
            },
            "module_unchanged": trusted,
            "scan": scan,
            "osv_ids": osv_ids,
            "reachable_ids": reachable,
            "unreachable_ids": unreachable,
            "reachable_count": len(reachable),
            "unreachable_count": len(unreachable),
            "failure_class": None if trusted else "tool_error",
            "command_exit": 0 if trusted else None,
            "duration_seconds": 1.25,
            "raw_trace": "SECRET_VULN_RAW",
            "osv_detail": {"secret": "SECRET_OSV_DETAIL"},
            "unknown_field": "SECRET_VULN_UNKNOWN",
        }

    def test_export_vulnerability_vulnerable_is_not_pass(self):
        self.write(
            self.report_dir / "vulnerability-report.json",
            self.vulnerability_report("vulnerable"),
        )
        code, _ = self.export(["vulnerability"])
        self.assertEqual(code, 1)
        safe_path = (
            self.report_dir / "upload" / "vulnerability.safe.json"
        )
        self.assertTrue(safe_path.is_file())
        text = safe_path.read_text()
        safe = json.loads(text)
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "vulnerable")
        self.assertEqual(safe["reachable_ids"], ["GO-2024-0001"])
        self.assertEqual(safe["reachable_count"], 1)
        self.assertEqual(safe["scan"]["cgo_enabled"], "1")
        self.assertEqual(safe["source_hash"]["go.mod"], "b" * 64)
        self.assertEqual(safe["source_hash"]["go.sum"], "c" * 64)
        for forbidden in (
            "SECRET_VULN_RAW",
            "SECRET_OSV_DETAIL",
            "SECRET_VULN_UNKNOWN",
            "raw_trace",
            "osv_detail",
        ):
            self.assertNotIn(forbidden, text)

    def test_export_vulnerability_failure_preserves_result(self):
        self.write(
            self.report_dir / "vulnerability-report.json",
            self.vulnerability_report("failure"),
        )
        code, _ = self.export(["vulnerability"])
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "vulnerability.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "failure")
        self.assertEqual(safe["failure_class"], "tool_error")
        self.assertIsNone(safe["go_version"])
        self.assertIsNone(safe["command_exit"])

    def test_export_vulnerability_pass_is_green(self):
        self.write(
            self.report_dir / "vulnerability-report.json",
            self.vulnerability_report("pass"),
        )
        code, _ = self.export(["vulnerability"])
        self.assertEqual(code, 0)
        safe = json.loads(
            (
                self.report_dir / "upload" / "vulnerability.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "pass")
        self.assertEqual(safe["reachable_ids"], [])

    def test_export_vulnerability_reachable_cannot_be_pass(self):
        value = self.vulnerability_report("pass")
        value["reachable_ids"] = ["GO-2024-0001"]
        value["reachable_count"] = 1
        value["osv_ids"] = ["GO-2024-0001", "GO-2024-0002"]
        self.write(self.report_dir / "vulnerability-report.json", value)
        code, _ = self.export(["vulnerability"])
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "vulnerability.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "error")
        self.assertEqual(safe["error"], "vulnerability-inconsistent")
        self.assertNotEqual(safe.get("result"), "pass")

    def test_export_vulnerability_lists_must_be_consistent(self):
        value = self.vulnerability_report("vulnerable")
        value["osv_ids"] = ["GO-2024-0001"]
        self.write(self.report_dir / "vulnerability-report.json", value)
        code, _ = self.export(["vulnerability"])
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "vulnerability.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "error")
        self.assertEqual(safe["error"], "vulnerability-inconsistent")

    def cancellation_report(self, result="pass"):
        pass_result = result == "pass"
        return {
            "schema": "payment-hub.ci-cancellation.v1",
            "result": "pass" if pass_result else "fail",
            "run_id": self.run_id if pass_result else None,
            "project": (
                "payment-hub-test-db-" + self.run_id
                if pass_result
                else None
            ),
            "stage": "pg-healthy",
            "signal": "SIGTERM",
            "launcher_exit": 1 if pass_result else 0,
            "failure": (
                {"stage": "interrupted", "kind": "sigterm"}
                if pass_result
                else {"stage": "cleanup", "kind": "not-pass"}
            ),
            "cleanup": {
                "result": "pass" if pass_result else "fail",
                "compose_down_exit": 0 if pass_result else 1,
                "network_removed": pass_result,
                "volume_removed": pass_result,
                "volume_retained": False,
                "repeat_signal_ignored": False,
                "error_count": 0 if pass_result else 1,
                "tool_containers_removed": pass_result,
            },
            "resource_absence": {
                "containers": pass_result,
                "network": pass_result,
                "volume": pass_result,
            },
            "preexisting_resource_preserved": pass_result,
            "elapsed_seconds": 12.5,
            "timings": {
                "pg_healthy_seconds": 3.5,
                "exit_after_signal_seconds": 8.0,
            },
            "input_sha256": "d" * 64 if pass_result else None,
            "raw_logs": "SECRET_CANCEL_RAW",
            "unknown_field": "SECRET_CANCEL_UNKNOWN",
        }

    def test_export_cancellation_pass_preserves_launcher_observation(self):
        self.write(
            self.report_dir / "cancellation-report.json",
            self.cancellation_report("pass"),
        )
        code, _ = self.export(["cancellation"])
        self.assertEqual(code, 0)
        safe_path = (
            self.report_dir / "upload" / "cancellation.safe.json"
        )
        self.assertTrue(safe_path.is_file())
        text = safe_path.read_text()
        safe = json.loads(text)
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "pass")
        self.assertEqual(
            safe["failure"], {"stage": "interrupted", "kind": "sigterm"}
        )
        self.assertEqual(safe["launcher_exit"], 1)
        self.assertEqual(safe["input_sha256"], "d" * 64)
        for forbidden in ("SECRET_CANCEL_RAW", "SECRET_CANCEL_UNKNOWN"):
            self.assertNotIn(forbidden, text)

    def test_export_cancellation_failure_preserves_result(self):
        self.write(
            self.report_dir / "cancellation-report.json",
            self.cancellation_report("fail"),
        )
        code, _ = self.export(["cancellation"])
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "cancellation.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")
        self.assertEqual(
            safe["failure"], {"stage": "cleanup", "kind": "not-pass"}
        )

    def test_export_cancellation_pass_requires_launcher_exit_1(self):
        value = self.cancellation_report("pass")
        value["launcher_exit"] = 0
        self.write(self.report_dir / "cancellation-report.json", value)
        code, _ = self.export(["cancellation"])
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "cancellation.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "error")
        self.assertEqual(safe["error"], "cancellation-inconsistent")

    def test_export_cancellation_pass_failure_must_be_observation(self):
        value = self.cancellation_report("pass")
        value["failure"] = {"stage": "internal", "kind": "unexpected"}
        self.write(self.report_dir / "cancellation-report.json", value)
        code, _ = self.export(["cancellation"])
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "cancellation.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "error")
        self.assertEqual(safe["error"], "cancellation-inconsistent")

    def test_export_does_not_copy_unrelated_logs(self):
        self.write(self.report_dir / "go-default.json", self.go_default())
        (self.report_dir / "raw.log").write_text(
            "SECRET_LOG_CANARY", encoding="utf-8"
        )
        code, _ = self.export()
        self.assertEqual(code, 0)
        upload = self.report_dir / "upload"
        self.assertEqual(
            sorted(path.name for path in upload.iterdir()),
            ["go-default.safe.json", "provenance.json"],
        )

    def test_export_rejects_unknown_expect_and_relative_paths(self):
        code = MOD.export_reports(
            str(self.report_dir), str(self.db_root), ["bogus"]
        )
        self.assertEqual(code, 1)
        code = MOD.export_reports("relative", str(self.db_root), ["default"])
        self.assertEqual(code, 1)

    def test_export_rejects_upload_symlink_escape(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.report_dir / "upload").symlink_to(outside, target_is_directory=True)
        self.write(self.report_dir / "go-default.json", self.go_default())
        code, _ = self.export()
        self.assertEqual(code, 1)
        self.assertEqual(list(outside.iterdir()), [])

    def test_provenance_hashes_match_safe_files(self):
        self.write(self.report_dir / "go-default.json", self.go_default())
        self.write_db(self.db_report())
        code, _ = self.export(["default", "db"])
        self.assertEqual(code, 0)
        upload = self.report_dir / "upload"
        provenance = json.loads((upload / "provenance.json").read_text())
        for entry in provenance["reports"]:
            with self.subTest(name=entry["name"]):
                path = upload / entry["export"]
                self.assertEqual(
                    entry["export_sha256"],
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
                self.assertEqual(
                    entry["source_sha256"],
                    hashlib.sha256(
                        (self.report_dir / entry["source"]).read_bytes()
                        if entry["name"] == "default"
                        else (
                            self.db_root
                            / self.run_id
                            / "db-foundation.json"
                        ).read_bytes()
                    ).hexdigest(),
                )


class CliTests(unittest.TestCase):
    def test_main_dispatches_run_default(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(
                MOD, "run_default", return_value=0
            ) as runner:
                code = MOD.main(
                    [
                        "run-default",
                        "--go",
                        "/bin/true",
                        "--report-dir",
                        temp,
                    ]
                )
        self.assertEqual(code, 0)
        runner.assert_called_once_with("/bin/true", temp)

    def test_main_dispatches_export_with_comma_list(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(
                MOD, "export_reports", return_value=0
            ) as exporter:
                code = MOD.main(
                    [
                        "export-reports",
                        "--report-dir",
                        temp,
                        "--db-artifact-root",
                        temp,
                        "--expect",
                        "default,db",
                    ]
                )
        self.assertEqual(code, 0)
        exporter.assert_called_once_with(temp, temp, ["default", "db"])

    def test_main_requires_subcommand_and_rejects_unknown_expect(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                MOD.main([])
            self.assertEqual(raised.exception.code, 2)
            with self.assertRaises(SystemExit) as raised:
                MOD.main(
                    [
                        "export-reports",
                        "--report-dir",
                        "/tmp/x",
                        "--db-artifact-root",
                        "/tmp/y",
                        "--expect",
                        "bogus",
                    ]
                )
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
