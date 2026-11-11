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
PAYMENT = "payment-hub/internal/payment"
HTTPAPI = "payment-hub/internal/httpapi"
TESTCONTROL = "payment-hub/internal/testcontrol"
POSTGRES = "payment-hub/internal/postgres"
MOCK = "payment-hub/internal/mock"
TESTS = "payment-hub/tests"
HARNESS = "payment-hub/tests/harness"
CMD = "payment-hub/cmd/payment-hub"
FOCUS_SUBTEST = "TestPaymentRequestBoundary/POST_rejection_priority"
EXPECTED = (APP, HARNESS, CMD)
REQUIRED = {
    "TestRequiredRoot": HARNESS,
    "TestRequiredRoot/sub_case": HARNESS,
}

# Frozen 26 PR3 roots read from the four review/regression test files. This
# literal is deliberately not derived from the source tree and is kept intact
# as the historical PR3 inventory; the US1 core roots are listed separately.
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

# First-batch US1 core default roots, next-batch query and HTTP contract
# roots, and the payment-directive protocol/client/consumer roots. Kept
# separate so the historical PR3 26-root assertion above is not silently
# deleted when the inventory grows.
EXPECTED_US1_DEFAULT_REQUIRED = {
    "TestSubmitPayment": PAYMENT,
    "TestEvidenceDecision": PAYMENT,
    "TestQueryPayment": PAYMENT,
    "TestPaymentRequestBoundary": HTTPAPI,
    "TestPaymentResponseContract": HTTPAPI,
    "TestPaymentRequestBoundary/POST_rejection_priority": HTTPAPI,
    "TestPaymentDirectiveProtocol": TESTCONTROL,
    "TestPaymentDirectiveClient": TESTCONTROL,
    "TestObserverPaymentDirective": HARNESS,
    "TestMockPaymentDirective": MOCK,
    "TestAppPaymentDirective": APP,
    "TestPaymentRuntimeWiring": APP,
    "TestPaymentRouterWiring": HTTPAPI,
}

US2_FOCUS_SUBTEST = (
    "TestPaymentRequestBoundary/POST_business_value_delegation"
)
# US2 default additions frozen by the parent in this order. The existing
# TestPaymentRequestBoundary root remains bound above; only the new delegated
# business-value subtest is appended here.
EXPECTED_US2_DEFAULT_REQUIRED = {
    "TestPaymentIdempotency": PAYMENT,
    "TestPaymentSubmitConflictPriority": PAYMENT,
    US2_FOCUS_SUBTEST: HTTPAPI,
}
# US3 default additions frozen by the parent in this order. The roots cover
# payment reconciliation/trace/cancellation, observation retry semantics,
# query-scenario protocol/client/consumer wiring, and observer query-directive
# plus controlled-termination safety.
EXPECTED_US3_DEFAULT_REQUIRED = {
    "TestPaymentReconciliation": PAYMENT,
    "TestQueryTraceFailureProtection": PAYMENT,
    "TestPaymentCancellationBounds": PAYMENT,
    "TestObservationRecordRetrySemantics": POSTGRES,
    "TestQueryScenarioProtocol": TESTCONTROL,
    "TestQueryScenarioClient": TESTCONTROL,
    "TestMockQueryScenarios": MOCK,
    "TestQueryScenarioAdapter": APP,
    "TestObserverQueryDirectiveSafety": HARNESS,
    "TestControlledTerminationProof": HARNESS,
    "TestNextCommittedPointBarrier": HARNESS,
    "TestSubmitTraceFailureProtection": PAYMENT,
    "TestTraceSaveIssueVariants": HTTPAPI,
}
EXPECTED_DEFAULT_REQUIRED_TOTAL = {
    **EXPECTED_DEFAULT_REQUIRED,
    **EXPECTED_US1_DEFAULT_REQUIRED,
    **EXPECTED_US2_DEFAULT_REQUIRED,
    **EXPECTED_US3_DEFAULT_REQUIRED,
}

PAYMENT_DIRECTIVE_DEFAULT_ROOTS = (
    "TestPaymentDirectiveProtocol",
    "TestPaymentDirectiveClient",
    "TestObserverPaymentDirective",
    "TestMockPaymentDirective",
    "TestAppPaymentDirective",
)

# Eight explicit DB-launcher integration bindings: the original four harness
# roots, the first-batch storage/mock core roots, and the order-lookup root.
EXPECTED_DB_REQUIRED = {
    "TestCommitHookIntegrationHidesReplyAfterRealCommit": HARNESS,
    "TestHarnessProcessPreservesParentStateIntegration": HARNESS,
    "TestApplicationLifecycleIntegration": HARNESS,
    "TestApplicationLifecycleIntegration/startup_diagnostics": HARNESS,
    "TestOrderRegistrationIntegration": POSTGRES,
    "TestObservationAtomicSaveIntegration": POSTGRES,
    "TestMockTransactionIntegration": MOCK,
    "TestOrderLookupIntegration": POSTGRES,
    "TestUS1Payment": TESTS,
    "TestUS1Restart": TESTS,
    "TestUS1HTTPBoundary": TESTS,
    "TestOrderRegistrationConcurrencyIntegration": POSTGRES,
    "TestMockIdempotencyIntegration": MOCK,
    "TestUS2Replay": TESTS,
    "TestUS2Conflict": TESTS,
    "TestUS2Concurrency": TESTS,
    "TestUS2Restart": TESTS,
    "TestUS2ReportSafety": TESTS,
    "TestObservationFaultIntegration": POSTGRES,
    "TestUS3Query": TESTS,
    "TestUS3Protection": TESTS,
    "TestUS3Restart": TESTS,
    "TestUS3Lifecycle": TESTS,
    "TestUS3ReportSafety": TESTS,
}
EXPECTED_PR3_DB_REQUIRED = {
    name: package
    for name, package in EXPECTED_DB_REQUIRED.items()
    if name
    in {
        "TestCommitHookIntegrationHidesReplyAfterRealCommit",
        "TestHarnessProcessPreservesParentStateIntegration",
        "TestApplicationLifecycleIntegration",
        "TestApplicationLifecycleIntegration/startup_diagnostics",
    }
}
EXPECTED_LEGACY_DB_REQUIRED = {
    name: package
    for name, package in EXPECTED_DB_REQUIRED.items()
    if name
    in {
        "TestCommitHookIntegrationHidesReplyAfterRealCommit",
        "TestHarnessProcessPreservesParentStateIntegration",
        "TestApplicationLifecycleIntegration",
        "TestApplicationLifecycleIntegration/startup_diagnostics",
        "TestOrderRegistrationIntegration",
        "TestObservationAtomicSaveIntegration",
        "TestMockTransactionIntegration",
        "TestOrderLookupIntegration",
        "TestUS1Payment",
        "TestUS1Restart",
        "TestUS1HTTPBoundary",
    }
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


def without_test_events(text, test_name):
    """Drop every event for one test; package terminal events are preserved."""
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
    """Replace one test's pass terminal with another terminal action."""
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


def proc(code, stdout, stderr="", timed_out=False, wait_elapsed_seconds=None):
    if isinstance(stdout, str):
        stdout = stdout.encode("utf-8")
    if isinstance(stderr, str):
        stderr = stderr.encode("utf-8")
    result = MOD.ProcResult(code, stdout, stderr, timed_out)
    # Older ProcResult shapes have no timing field; keep the construction
    # compatible so the red run fails on the report assertion, not a TypeError.
    if wait_elapsed_seconds is not None and hasattr(
        result, "wait_elapsed_seconds"
    ):
        result = result._replace(
            wait_elapsed_seconds=wait_elapsed_seconds
        )
    return result


@contextlib.contextmanager
def monotonic_clock(*values):
    """Patch the module clock when present; old code simply never calls it."""
    clock = mock.Mock(side_effect=list(values))
    module_time = getattr(MOD, "time", None)
    if module_time is None:
        yield clock
        return
    with mock.patch.object(module_time, "monotonic", clock):
        yield clock


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
        # Historical PR3 26-root inventory remains asserted as its own set.
        self.assertEqual(len(EXPECTED_DEFAULT_REQUIRED), 26)
        self.assertEqual(
            {
                name: MOD.DEFAULT_REQUIRED_TESTS.get(name)
                for name in EXPECTED_DEFAULT_REQUIRED
            },
            EXPECTED_DEFAULT_REQUIRED,
        )
        self.assertEqual(
            {
                name: MOD.DEFAULT_REQUIRED_TESTS.get(name)
                for name in EXPECTED_US1_DEFAULT_REQUIRED
            },
            EXPECTED_US1_DEFAULT_REQUIRED,
        )
        self.assertEqual(
            {
                name: MOD.DEFAULT_REQUIRED_TESTS.get(name)
                for name in EXPECTED_US2_DEFAULT_REQUIRED
            },
            EXPECTED_US2_DEFAULT_REQUIRED,
        )
        self.assertEqual(
            {
                name: MOD.DEFAULT_REQUIRED_TESTS.get(name)
                for name in EXPECTED_US3_DEFAULT_REQUIRED
            },
            EXPECTED_US3_DEFAULT_REQUIRED,
        )
        self.assertEqual(
            MOD.DEFAULT_REQUIRED_TESTS, EXPECTED_DEFAULT_REQUIRED_TOTAL
        )
        self.assertEqual(
            len(MOD.DEFAULT_REQUIRED_TESTS),
            len(EXPECTED_DEFAULT_REQUIRED_TOTAL),
        )
        self.assertEqual(len(EXPECTED_DEFAULT_REQUIRED_TOTAL), 55)
        roots = {
            name.split("/", 1)[0]
            for name in MOD.DEFAULT_REQUIRED_TESTS
        }
        expected_roots = {
            name.split("/", 1)[0]
            for name in EXPECTED_DEFAULT_REQUIRED_TOTAL
        }
        self.assertEqual(roots, expected_roots)
        self.assertEqual(len(roots), 53)
        subtests = [
            name
            for name in MOD.DEFAULT_REQUIRED_TESTS
            if "/" in name
        ]
        self.assertEqual(subtests, [FOCUS_SUBTEST, US2_FOCUS_SUBTEST])
        self.assertEqual(
            MOD.DEFAULT_REQUIRED_TESTS.get(FOCUS_SUBTEST), HTTPAPI
        )
        self.assertEqual(
            MOD.DEFAULT_REQUIRED_TESTS.get(US2_FOCUS_SUBTEST), HTTPAPI
        )

    def test_us2_default_roots_bind_to_payment_and_http_core(self):
        self.assertEqual(
            tuple(EXPECTED_US2_DEFAULT_REQUIRED),
            (
                "TestPaymentIdempotency",
                "TestPaymentSubmitConflictPriority",
                US2_FOCUS_SUBTEST,
            ),
        )
        self.assertEqual(
            MOD.DEFAULT_REQUIRED_TESTS.get("TestPaymentIdempotency"),
            PAYMENT,
        )
        self.assertEqual(
            MOD.DEFAULT_REQUIRED_TESTS.get("TestPaymentSubmitConflictPriority"),
            PAYMENT,
        )
        self.assertEqual(
            MOD.DEFAULT_REQUIRED_TESTS.get(US2_FOCUS_SUBTEST), HTTPAPI
        )
        # The existing root remains bound; only the delegated subtest is new.
        self.assertEqual(
            MOD.DEFAULT_REQUIRED_TESTS.get("TestPaymentRequestBoundary"),
            HTTPAPI,
        )
        self.assertEqual(
            MOD.DEFAULT_REQUIRED_TESTS.get(FOCUS_SUBTEST), HTTPAPI
        )
        for name in EXPECTED_US2_DEFAULT_REQUIRED:
            with self.subTest(name=name):
                self.assertNotIn(name, MOD.DB_REQUIRED_TEST_KEYS)

    def test_us3_default_roots_bind_to_exact_packages(self):
        self.assertEqual(
            tuple(EXPECTED_US3_DEFAULT_REQUIRED),
            (
                "TestPaymentReconciliation",
                "TestQueryTraceFailureProtection",
                "TestPaymentCancellationBounds",
                "TestObservationRecordRetrySemantics",
                "TestQueryScenarioProtocol",
                "TestQueryScenarioClient",
                "TestMockQueryScenarios",
                "TestQueryScenarioAdapter",
                "TestObserverQueryDirectiveSafety",
                "TestControlledTerminationProof",
                "TestNextCommittedPointBarrier",
                "TestSubmitTraceFailureProtection",
                "TestTraceSaveIssueVariants",
            ),
        )
        for name, package in EXPECTED_US3_DEFAULT_REQUIRED.items():
            with self.subTest(name=name):
                self.assertEqual(
                    MOD.DEFAULT_REQUIRED_TESTS.get(name), package
                )
                self.assertNotIn(name, MOD.DB_REQUIRED_TEST_KEYS)
        for name in (
            "TestPaymentReconciliation",
            "TestQueryTraceFailureProtection",
            "TestPaymentCancellationBounds",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    MOD.DEFAULT_REQUIRED_TESTS.get(name), PAYMENT
                )
        self.assertEqual(
            MOD.DEFAULT_REQUIRED_TESTS.get(
                "TestObservationRecordRetrySemantics"
            ),
            POSTGRES,
        )
        for name in (
            "TestQueryScenarioProtocol",
            "TestQueryScenarioClient",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    MOD.DEFAULT_REQUIRED_TESTS.get(name), TESTCONTROL
                )
        self.assertEqual(
            MOD.DEFAULT_REQUIRED_TESTS.get("TestMockQueryScenarios"), MOCK
        )
        self.assertEqual(
            MOD.DEFAULT_REQUIRED_TESTS.get("TestQueryScenarioAdapter"), APP
        )
        for name in (
            "TestObserverQueryDirectiveSafety",
            "TestControlledTerminationProof",
            "TestNextCommittedPointBarrier",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    MOD.DEFAULT_REQUIRED_TESTS.get(name), HARNESS
                )

    def test_default_required_covers_d1_d2_and_helper(self):
        for name in (
            "TestShutdownCleanupSealFailureStillClosesDatabase",
            "TestArtifactsFinishRejectsUnclosedObserverAfterHalfFrame",
            "TestProcessReviewHelper",
        ):
            with self.subTest(name=name):
                self.assertIn(name, MOD.DEFAULT_REQUIRED_TESTS)

    def test_us1_default_roots_bind_to_payment_and_http_core(self):
        self.assertEqual(
            tuple(EXPECTED_US1_DEFAULT_REQUIRED),
            (
                "TestSubmitPayment",
                "TestEvidenceDecision",
                "TestQueryPayment",
                "TestPaymentRequestBoundary",
                "TestPaymentResponseContract",
                FOCUS_SUBTEST,
                "TestPaymentDirectiveProtocol",
                "TestPaymentDirectiveClient",
                "TestObserverPaymentDirective",
                "TestMockPaymentDirective",
                "TestAppPaymentDirective",
                "TestPaymentRuntimeWiring",
                "TestPaymentRouterWiring",
            ),
        )
        for name, package in EXPECTED_US1_DEFAULT_REQUIRED.items():
            with self.subTest(name=name):
                self.assertEqual(
                    MOD.DEFAULT_REQUIRED_TESTS.get(name), package
                )
        for name in (
            "TestPaymentRequestBoundary",
            "TestPaymentResponseContract",
            FOCUS_SUBTEST,
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    MOD.DEFAULT_REQUIRED_TESTS.get(name), HTTPAPI
                )
                self.assertNotEqual(
                    MOD.DEFAULT_REQUIRED_TESTS.get(name), PAYMENT
                )
        for name, package in (
            ("TestPaymentDirectiveProtocol", TESTCONTROL),
            ("TestPaymentDirectiveClient", TESTCONTROL),
            ("TestObserverPaymentDirective", HARNESS),
            ("TestMockPaymentDirective", MOCK),
            ("TestAppPaymentDirective", APP),
            ("TestPaymentRuntimeWiring", APP),
            ("TestPaymentRouterWiring", HTTPAPI),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    MOD.DEFAULT_REQUIRED_TESTS.get(name), package
                )

    def test_integration_roots_are_db_required_not_default(self):
        for name in MOD.DB_REQUIRED_TEST_KEYS:
            with self.subTest(name=name):
                self.assertIn(name, MOD.DB_REQUIRED_TEST_KEYS)
                self.assertNotIn(name, MOD.DEFAULT_REQUIRED_TESTS)

    def test_db_required_keys_are_the_twenty_four_explicit_bindings(self):
        self.assertEqual(
            MOD.DB_REQUIRED_TEST_KEYS,
            (
                "TestCommitHookIntegrationHidesReplyAfterRealCommit",
                "TestHarnessProcessPreservesParentStateIntegration",
                "TestApplicationLifecycleIntegration",
                "TestApplicationLifecycleIntegration/startup_diagnostics",
                "TestOrderRegistrationIntegration",
                "TestObservationAtomicSaveIntegration",
                "TestMockTransactionIntegration",
                "TestOrderLookupIntegration",
                "TestUS1Payment",
                "TestUS1Restart",
                "TestUS1HTTPBoundary",
                "TestOrderRegistrationConcurrencyIntegration",
                "TestMockIdempotencyIntegration",
                "TestUS2Replay",
                "TestUS2Conflict",
                "TestUS2Concurrency",
                "TestUS2Restart",
                "TestUS2ReportSafety",
                "TestObservationFaultIntegration",
                "TestUS3Query",
                "TestUS3Protection",
                "TestUS3Restart",
                "TestUS3Lifecycle",
                "TestUS3ReportSafety",
            ),
        )
        self.assertEqual(
            tuple(MOD.DB_REQUIRED_TEST_KEYS), tuple(EXPECTED_DB_REQUIRED)
        )
        self.assertEqual(len(MOD.DB_REQUIRED_TEST_KEYS), 24)

    def test_db_required_keys_are_the_eleven_explicit_bindings(self):
        # Historical 11-entry subset remains the exact prefix of the frozen
        # 18-entry order; this keeps the pre-US2 regression name and coverage.
        self.assertEqual(
            tuple(MOD.DB_REQUIRED_TEST_KEYS)[: len(EXPECTED_LEGACY_DB_REQUIRED)],
            tuple(EXPECTED_LEGACY_DB_REQUIRED),
        )
        for name in EXPECTED_LEGACY_DB_REQUIRED:
            with self.subTest(name=name):
                self.assertIn(name, MOD.DB_REQUIRED_TEST_KEYS)

    def test_us1_safe_report_kind_is_registered(self):
        self.assertIn("us1", MOD.EXPECT_NAMES)
        self.assertEqual(MOD.REPORT_FILENAMES.get("us1"), "us1.json")
        self.assertEqual(
            MOD.SAFE_FILENAMES.get("us1"), "us1.safe.json"
        )

    def test_us2_safe_report_kind_is_registered(self):
        self.assertIn("us2", MOD.EXPECT_NAMES)
        self.assertEqual(MOD.REPORT_FILENAMES.get("us2"), "us2.json")
        self.assertEqual(
            MOD.SAFE_FILENAMES.get("us2"), "us2.safe.json"
        )

    def test_us3_safe_report_kind_is_registered(self):
        self.assertIn("us3", MOD.EXPECT_NAMES)
        self.assertEqual(MOD.REPORT_FILENAMES.get("us3"), "us3.json")
        self.assertEqual(
            MOD.SAFE_FILENAMES.get("us3"), "us3.safe.json"
        )

    def test_shared_batch_timeout_constant_is_570(self):
        # One shared whole-batch budget; the 180+360+30 split is capacity
        # planning only and must not become staged reservations.
        self.assertEqual(
            getattr(MOD, "GO_TEST_BATCH_TIMEOUT_SECONDS", None), 570
        )

    def test_vulnerability_go_pin_is_1_27_4(self):
        self.assertEqual(MOD.EXPECTED_VULNERABILITY_GO, "go1.27.4")

    def test_proc_result_has_optional_trailing_wait_elapsed(self):
        self.assertIn("wait_elapsed_seconds", MOD.ProcResult._fields)
        self.assertEqual(
            MOD.ProcResult._fields[-1], "wait_elapsed_seconds"
        )
        self.assertIsNone(
            getattr(
                MOD.ProcResult(0, b"", b""),
                "wait_elapsed_seconds",
                None,
            )
        )

    def test_db_required_binds_storage_mock_and_tests_packages(self):
        self.assertEqual(
            MOD.DB_PACKAGE_KEYS,
            ("internal/postgres", "internal/mock", "tests/harness", "tests"),
        )
        for name in (
            "TestOrderRegistrationIntegration",
            "TestObservationAtomicSaveIntegration",
            "TestOrderLookupIntegration",
        ):
            with self.subTest(name=name):
                self.assertEqual(EXPECTED_DB_REQUIRED[name], POSTGRES)
                self.assertNotEqual(EXPECTED_DB_REQUIRED[name], HARNESS)
        self.assertEqual(
            EXPECTED_DB_REQUIRED["TestMockTransactionIntegration"], MOCK
        )
        for name in (
            "TestUS1Payment",
            "TestUS1Restart",
            "TestUS1HTTPBoundary",
            "TestUS2Replay",
            "TestUS2Conflict",
            "TestUS2Concurrency",
            "TestUS2Restart",
            "TestUS2ReportSafety",
        ):
            with self.subTest(name=name):
                self.assertEqual(EXPECTED_DB_REQUIRED[name], TESTS)
                self.assertNotEqual(EXPECTED_DB_REQUIRED[name], HARNESS)
        self.assertEqual(
            EXPECTED_DB_REQUIRED[
                "TestOrderRegistrationConcurrencyIntegration"
            ],
            POSTGRES,
        )
        self.assertEqual(
            EXPECTED_DB_REQUIRED["TestMockIdempotencyIntegration"], MOCK
        )
        self.assertEqual(
            EXPECTED_DB_REQUIRED["TestObservationFaultIntegration"], POSTGRES
        )
        for name in (
            "TestUS3Query",
            "TestUS3Protection",
            "TestUS3Restart",
            "TestUS3Lifecycle",
            "TestUS3ReportSafety",
        ):
            with self.subTest(name=name):
                self.assertEqual(EXPECTED_DB_REQUIRED[name], TESTS)
                self.assertNotEqual(EXPECTED_DB_REQUIRED[name], HARNESS)
                self.assertIn(name, MOD.DB_REQUIRED_TEST_KEYS)


class DbRequiredParserTests(unittest.TestCase):
    """Parser-level DB launcher inventory for the twenty-four explicit bindings."""

    def parse(self, stdout, required=None):
        mapping = {
            name: EXPECTED_DB_REQUIRED[name]
            for name in MOD.DB_REQUIRED_TEST_KEYS
        }
        return MOD.parse_go_test_json(
            stdout,
            "",
            0,
            (POSTGRES, MOCK, HARNESS, TESTS),
            mapping if required is None else required,
            allowed_no_test_files=(),
        )

    def full_stream(self):
        return default_stream(
            (POSTGRES, MOCK, HARNESS, TESTS), EXPECTED_DB_REQUIRED
        )

    def test_full_twenty_four_binding_stream_is_green(self):
        result = self.parse(self.full_stream())
        self.assertEqual(result["problems"], [])
        self.assertEqual(
            set(result["required_test_results"]),
            set(EXPECTED_DB_REQUIRED),
        )
        self.assertTrue(
            all(
                value == "pass"
                for value in result["required_test_results"].values()
            )
        )

    def test_full_eleven_binding_stream_is_green(self):
        # Historical US1 11-entry mapping remains independently green.
        stream = default_stream(
            (POSTGRES, MOCK, HARNESS, TESTS),
            EXPECTED_LEGACY_DB_REQUIRED,
        )
        result = MOD.parse_go_test_json(
            stream,
            "",
            0,
            (POSTGRES, MOCK, HARNESS, TESTS),
            EXPECTED_LEGACY_DB_REQUIRED,
            allowed_no_test_files=(),
        )
        self.assertEqual(result["problems"], [])
        self.assertEqual(
            set(result["required_test_results"]),
            set(EXPECTED_LEGACY_DB_REQUIRED),
        )

    def test_each_new_db_root_is_required(self):
        for target in (
            "TestOrderRegistrationIntegration",
            "TestObservationAtomicSaveIntegration",
            "TestMockTransactionIntegration",
            "TestOrderLookupIntegration",
            "TestUS1Payment",
            "TestUS1Restart",
            "TestUS1HTTPBoundary",
            "TestOrderRegistrationConcurrencyIntegration",
            "TestMockIdempotencyIntegration",
            "TestUS2Replay",
            "TestUS2Conflict",
            "TestUS2Concurrency",
            "TestUS2Restart",
            "TestUS2ReportSafety",
            "TestObservationFaultIntegration",
            "TestUS3Query",
            "TestUS3Protection",
            "TestUS3Restart",
            "TestUS3Lifecycle",
            "TestUS3ReportSafety",
        ):
            with self.subTest(target=target):
                result = self.parse(
                    without_test_events(self.full_stream(), target)
                )
                self.assertEqual(
                    result["required_test_results"].get(target), "missing"
                )
                self.assertIn("required-test-missing", result["problems"])

    def test_new_db_root_skip_is_not_green(self):
        target = "TestOrderRegistrationIntegration"
        result = self.parse(
            replace_test_terminal(
                self.full_stream(), POSTGRES, target, "skip"
            )
        )
        self.assertEqual(
            result["required_test_results"].get(target), "skip"
        )
        self.assertIn("required-test-skip", result["problems"])

    def test_new_db_root_wrong_package_is_not_green(self):
        target = "TestMockTransactionIntegration"
        stdout = without_test_events(self.full_stream(), target)
        stdout += event("run", POSTGRES, target)
        stdout += event("pass", POSTGRES, target, elapsed=0.01)
        result = self.parse(stdout)
        self.assertEqual(
            result["required_test_results"].get(target), "wrong-package"
        )
        self.assertIn("required-test-wrong-package", result["problems"])

    def test_new_db_root_output_is_not_a_pass(self):
        target = "TestObservationAtomicSaveIntegration"
        stdout = without_test_events(self.full_stream(), target)
        stdout += event(
            "output",
            POSTGRES,
            test=target,
            output="--- PASS: " + target + " (0.00s)\n",
        )
        result = self.parse(stdout)
        self.assertEqual(
            result["required_test_results"].get(target), "missing"
        )
        self.assertIn("required-test-missing", result["problems"])

    def test_new_lookup_root_skip_is_not_green(self):
        target = "TestOrderLookupIntegration"
        result = self.parse(
            replace_test_terminal(
                self.full_stream(), POSTGRES, target, "skip"
            )
        )
        self.assertEqual(
            result["required_test_results"].get(target), "skip"
        )
        self.assertIn("required-test-skip", result["problems"])

    def test_new_lookup_root_wrong_package_is_not_green(self):
        target = "TestOrderLookupIntegration"
        stdout = without_test_events(self.full_stream(), target)
        stdout += event("run", HARNESS, target)
        stdout += event("pass", HARNESS, target, elapsed=0.01)
        result = self.parse(stdout)
        self.assertEqual(
            result["required_test_results"].get(target), "wrong-package"
        )
        self.assertIn("required-test-wrong-package", result["problems"])

    def test_new_lookup_root_output_is_not_a_pass(self):
        target = "TestOrderLookupIntegration"
        stdout = without_test_events(self.full_stream(), target)
        stdout += event(
            "output",
            POSTGRES,
            test=target,
            output="--- PASS: " + target + " (0.00s)\n",
        )
        result = self.parse(stdout)
        self.assertEqual(
            result["required_test_results"].get(target), "missing"
        )
        self.assertIn("required-test-missing", result["problems"])

    def test_us2_db_bindings_are_exact(self):
        for name, package in (
            ("TestOrderRegistrationConcurrencyIntegration", POSTGRES),
            ("TestMockIdempotencyIntegration", MOCK),
            ("TestUS2Replay", TESTS),
            ("TestUS2Conflict", TESTS),
            ("TestUS2Concurrency", TESTS),
            ("TestUS2Restart", TESTS),
            ("TestUS2ReportSafety", TESTS),
        ):
            with self.subTest(name=name):
                self.assertEqual(EXPECTED_DB_REQUIRED[name], package)
                self.assertIn(name, MOD.DB_REQUIRED_TEST_KEYS)
                self.assertEqual(
                    MOD.DB_REQUIRED_TEST_KEYS[
                        MOD.DB_REQUIRED_TEST_KEYS.index(name)
                    ],
                    name,
                )
                self.assertNotEqual(package, HARNESS)

    def test_us3_db_bindings_are_exact(self):
        for name, package in (
            ("TestObservationFaultIntegration", POSTGRES),
            ("TestUS3Query", TESTS),
            ("TestUS3Protection", TESTS),
            ("TestUS3Restart", TESTS),
            ("TestUS3Lifecycle", TESTS),
            ("TestUS3ReportSafety", TESTS),
        ):
            with self.subTest(name=name):
                self.assertEqual(EXPECTED_DB_REQUIRED[name], package)
                self.assertIn(name, MOD.DB_REQUIRED_TEST_KEYS)
                self.assertEqual(
                    MOD.DB_REQUIRED_TEST_KEYS[
                        MOD.DB_REQUIRED_TEST_KEYS.index(name)
                    ],
                    name,
                )
                self.assertNotEqual(package, HARNESS)

    def test_us3_db_roots_mutations_are_not_green(self):
        targets = (
            "TestObservationFaultIntegration",
            "TestUS3Query",
            "TestUS3Protection",
            "TestUS3Restart",
            "TestUS3Lifecycle",
            "TestUS3ReportSafety",
        )
        for target in targets:
            package = EXPECTED_DB_REQUIRED[target]
            with self.subTest(target=target, mutation="missing"):
                result = self.parse(
                    without_test_events(self.full_stream(), target)
                )
                self.assertEqual(
                    result["required_test_results"].get(target), "missing"
                )
                self.assertIn("required-test-missing", result["problems"])
            for action in ("skip", "fail"):
                with self.subTest(target=target, mutation=action):
                    result = self.parse(
                        replace_test_terminal(
                            self.full_stream(), package, target, action
                        )
                    )
                    self.assertEqual(
                        result["required_test_results"].get(target), action
                    )
                    self.assertIn(
                        "required-test-" + action, result["problems"]
                    )
            with self.subTest(target=target, mutation="wrong-package"):
                wrong_package = HARNESS if package != HARNESS else APP
                stdout = without_test_events(self.full_stream(), target)
                stdout += event("run", wrong_package, target)
                stdout += event("pass", wrong_package, target, elapsed=0.01)
                result = self.parse(stdout)
                self.assertEqual(
                    result["required_test_results"].get(target),
                    "wrong-package",
                )
                self.assertIn(
                    "required-test-wrong-package", result["problems"]
                )
            with self.subTest(target=target, mutation="duplicate"):
                stdout = self.full_stream()
                stdout += event("run", package, target)
                stdout += event("pass", package, target, elapsed=0.01)
                result = self.parse(stdout)
                self.assertEqual(
                    result["required_test_results"].get(target), "duplicate"
                )
            with self.subTest(target=target, mutation="output-spoof"):
                stdout = without_test_events(self.full_stream(), target)
                stdout += event(
                    "output",
                    package,
                    test=target,
                    output="--- PASS: " + target + " (0.00s)\n",
                )
                result = self.parse(stdout)
                self.assertEqual(
                    result["required_test_results"].get(target), "missing"
                )
                self.assertIn("required-test-missing", result["problems"])

    def test_us2_db_roots_skip_and_fail_are_not_green(self):
        for target in (
            "TestOrderRegistrationConcurrencyIntegration",
            "TestMockIdempotencyIntegration",
            "TestUS2Replay",
            "TestUS2Conflict",
            "TestUS2Concurrency",
            "TestUS2Restart",
            "TestUS2ReportSafety",
        ):
            package = EXPECTED_DB_REQUIRED[target]
            for action in ("skip", "fail"):
                with self.subTest(target=target, action=action):
                    result = self.parse(
                        replace_test_terminal(
                            self.full_stream(), package, target, action
                        )
                    )
                    self.assertEqual(
                        result["required_test_results"].get(target), action
                    )
                    self.assertIn(
                        "required-test-" + action, result["problems"]
                    )

    def test_us2_db_roots_wrong_package_are_not_green(self):
        for target, wrong_package in (
            ("TestOrderRegistrationConcurrencyIntegration", MOCK),
            ("TestMockIdempotencyIntegration", POSTGRES),
            ("TestUS2Replay", HARNESS),
            ("TestUS2Conflict", HARNESS),
            ("TestUS2Concurrency", HARNESS),
            ("TestUS2Restart", HARNESS),
            ("TestUS2ReportSafety", HARNESS),
        ):
            with self.subTest(target=target):
                stdout = without_test_events(self.full_stream(), target)
                stdout += event("run", wrong_package, target)
                stdout += event("pass", wrong_package, target, elapsed=0.01)
                result = self.parse(stdout)
                self.assertEqual(
                    result["required_test_results"].get(target),
                    "wrong-package",
                )
                self.assertIn(
                    "required-test-wrong-package", result["problems"]
                )

    def test_us2_db_roots_duplicate_is_not_green(self):
        for target in (
            "TestOrderRegistrationConcurrencyIntegration",
            "TestMockIdempotencyIntegration",
            "TestUS2Replay",
            "TestUS2Conflict",
            "TestUS2Concurrency",
            "TestUS2Restart",
            "TestUS2ReportSafety",
        ):
            package = EXPECTED_DB_REQUIRED[target]
            with self.subTest(target=target):
                stdout = self.full_stream()
                stdout += event("run", package, target)
                stdout += event("pass", package, target, elapsed=0.01)
                result = self.parse(stdout)
                self.assertEqual(
                    result["required_test_results"].get(target), "duplicate"
                )

    def test_us2_db_roots_output_spoof_is_not_a_pass(self):
        for target in (
            "TestOrderRegistrationConcurrencyIntegration",
            "TestMockIdempotencyIntegration",
            "TestUS2Replay",
            "TestUS2Conflict",
            "TestUS2Concurrency",
            "TestUS2Restart",
            "TestUS2ReportSafety",
        ):
            package = EXPECTED_DB_REQUIRED[target]
            with self.subTest(target=target):
                stdout = without_test_events(self.full_stream(), target)
                stdout += event(
                    "output",
                    package,
                    test=target,
                    output="--- PASS: " + target + " (0.00s)\n",
                )
                result = self.parse(stdout)
                self.assertEqual(
                    result["required_test_results"].get(target), "missing"
                )
                self.assertIn("required-test-missing", result["problems"])


class Us2DefaultRequiredParserTests(unittest.TestCase):
    """Parser-level default inventory for the three frozen US2 additions."""

    PACKAGES = (
        APP,
        HARNESS,
        CMD,
        PAYMENT,
        HTTPAPI,
        MOCK,
        TESTCONTROL,
        POSTGRES,
    )

    def parse(self, stdout):
        return MOD.parse_go_test_json(
            stdout,
            "",
            0,
            self.PACKAGES,
            MOD.DEFAULT_REQUIRED_TESTS,
            allowed_no_test_files=(CMD,),
        )

    def full_stream(self):
        return default_stream(self.PACKAGES, MOD.DEFAULT_REQUIRED_TESTS)

    def test_full_fifty_five_binding_stream_is_green(self):
        result = self.parse(self.full_stream())
        self.assertEqual(result["problems"], [])
        self.assertEqual(
            set(result["required_test_results"]),
            set(EXPECTED_DEFAULT_REQUIRED_TOTAL),
        )

    def test_us2_default_roots_missing_skip_fail_are_not_green(self):
        for target in EXPECTED_US2_DEFAULT_REQUIRED:
            package = EXPECTED_US2_DEFAULT_REQUIRED[target]
            with self.subTest(target=target, mutation="missing"):
                result = self.parse(
                    without_test_events(self.full_stream(), target)
                )
                self.assertEqual(
                    result["required_test_results"].get(target), "missing"
                )
                self.assertIn("required-test-missing", result["problems"])
            for action in ("skip", "fail"):
                with self.subTest(target=target, mutation=action):
                    result = self.parse(
                        replace_test_terminal(
                            self.full_stream(), package, target, action
                        )
                    )
                    self.assertEqual(
                        result["required_test_results"].get(target), action
                    )
                    self.assertIn(
                        "required-test-" + action, result["problems"]
                    )

    def test_us2_default_roots_wrong_package_are_not_green(self):
        for target, wrong_package in (
            ("TestPaymentIdempotency", HTTPAPI),
            ("TestPaymentSubmitConflictPriority", HTTPAPI),
            (US2_FOCUS_SUBTEST, APP),
        ):
            with self.subTest(target=target):
                stdout = without_test_events(self.full_stream(), target)
                stdout += event("run", wrong_package, target)
                stdout += event("pass", wrong_package, target, elapsed=0.01)
                result = self.parse(stdout)
                self.assertEqual(
                    result["required_test_results"].get(target),
                    "wrong-package",
                )
                self.assertIn(
                    "required-test-wrong-package", result["problems"]
                )

    def test_us2_default_roots_duplicate_are_not_green(self):
        for target in EXPECTED_US2_DEFAULT_REQUIRED:
            package = EXPECTED_US2_DEFAULT_REQUIRED[target]
            with self.subTest(target=target):
                stdout = self.full_stream()
                stdout += event("run", package, target)
                stdout += event("pass", package, target, elapsed=0.01)
                result = self.parse(stdout)
                self.assertEqual(
                    result["required_test_results"].get(target), "duplicate"
                )

    def test_us2_default_roots_output_spoof_is_not_a_pass(self):
        for target in EXPECTED_US2_DEFAULT_REQUIRED:
            package = EXPECTED_US2_DEFAULT_REQUIRED[target]
            with self.subTest(target=target):
                stdout = without_test_events(self.full_stream(), target)
                stdout += event(
                    "output",
                    package,
                    test=target,
                    output="--- PASS: " + target + " (0.00s)\n",
                )
                result = self.parse(stdout)
                self.assertEqual(
                    result["required_test_results"].get(target), "missing"
                )
                self.assertIn("required-test-missing", result["problems"])


class Us3DefaultRequiredParserTests(unittest.TestCase):
    """Parser-level default inventory for the eleven frozen US3 additions."""

    PACKAGES = (
        APP,
        HARNESS,
        CMD,
        PAYMENT,
        HTTPAPI,
        MOCK,
        TESTCONTROL,
        POSTGRES,
    )

    def parse(self, stdout):
        return MOD.parse_go_test_json(
            stdout,
            "",
            0,
            self.PACKAGES,
            MOD.DEFAULT_REQUIRED_TESTS,
            allowed_no_test_files=(CMD,),
        )

    def full_stream(self):
        return default_stream(self.PACKAGES, MOD.DEFAULT_REQUIRED_TESTS)

    def test_full_fifty_five_binding_stream_is_green(self):
        result = self.parse(self.full_stream())
        self.assertEqual(result["problems"], [])
        self.assertEqual(
            set(result["required_test_results"]),
            set(EXPECTED_DEFAULT_REQUIRED_TOTAL),
        )

    def test_us3_default_roots_missing_skip_fail_are_not_green(self):
        for target in EXPECTED_US3_DEFAULT_REQUIRED:
            package = EXPECTED_US3_DEFAULT_REQUIRED[target]
            with self.subTest(target=target, mutation="missing"):
                result = self.parse(
                    without_test_events(self.full_stream(), target)
                )
                self.assertEqual(
                    result["required_test_results"].get(target), "missing"
                )
                self.assertIn("required-test-missing", result["problems"])
            for action in ("skip", "fail"):
                with self.subTest(target=target, mutation=action):
                    result = self.parse(
                        replace_test_terminal(
                            self.full_stream(), package, target, action
                        )
                    )
                    self.assertEqual(
                        result["required_test_results"].get(target), action
                    )
                    self.assertIn(
                        "required-test-" + action, result["problems"]
                    )

    def test_us3_default_roots_wrong_package_are_not_green(self):
        for target, package in EXPECTED_US3_DEFAULT_REQUIRED.items():
            wrong_package = HARNESS if package != HARNESS else APP
            with self.subTest(target=target):
                stdout = without_test_events(self.full_stream(), target)
                stdout += event("run", wrong_package, target)
                stdout += event("pass", wrong_package, target, elapsed=0.01)
                result = self.parse(stdout)
                self.assertEqual(
                    result["required_test_results"].get(target),
                    "wrong-package",
                )
                self.assertIn(
                    "required-test-wrong-package", result["problems"]
                )

    def test_us3_default_roots_duplicate_are_not_green(self):
        for target, package in EXPECTED_US3_DEFAULT_REQUIRED.items():
            with self.subTest(target=target):
                stdout = self.full_stream()
                stdout += event("run", package, target)
                stdout += event("pass", package, target, elapsed=0.01)
                result = self.parse(stdout)
                self.assertEqual(
                    result["required_test_results"].get(target), "duplicate"
                )

    def test_us3_default_roots_output_spoof_is_not_a_pass(self):
        for target, package in EXPECTED_US3_DEFAULT_REQUIRED.items():
            with self.subTest(target=target):
                stdout = without_test_events(self.full_stream(), target)
                stdout += event(
                    "output",
                    package,
                    test=target,
                    output="--- PASS: " + target + " (0.00s)\n",
                )
                result = self.parse(stdout)
                self.assertEqual(
                    result["required_test_results"].get(target), "missing"
                )
                self.assertIn("required-test-missing", result["problems"])


class RunDefaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ci-checks-run-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report_dir = self.root / "report"
        self.report_dir.mkdir()
        self.packages = [
            CMD,
            APP,
            HTTPAPI,
            MOCK,
            PAYMENT,
            POSTGRES,
            TESTCONTROL,
            HARNESS,
        ]
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

    def test_run_default_requires_each_new_us1_default_root(self):
        for target in EXPECTED_US1_DEFAULT_REQUIRED:
            with self.subTest(target=target):
                test_stdout = without_test_events(
                    default_stream(
                        self.packages, MOD.DEFAULT_REQUIRED_TESTS
                    ),
                    target,
                )
                code, _ = self.run_default(
                    [proc(0, self.list_stdout), proc(0, test_stdout)]
                )
                self.assertEqual(code, 1)
                report = self.read_report()
                self.assertEqual(report["result"], "fail")
                self.assertEqual(
                    report["required_test_results"].get(target), "missing"
                )

    def test_run_default_focus_subtest_missing_fails_even_root_passes(self):
        test_stdout = without_test_events(
            default_stream(self.packages, MOD.DEFAULT_REQUIRED_TESTS),
            FOCUS_SUBTEST,
        )
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(0, test_stdout)]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertEqual(
            report["required_test_results"].get(FOCUS_SUBTEST), "missing"
        )
        self.assertEqual(
            report["required_test_results"].get(
                "TestPaymentRequestBoundary"
            ),
            "pass",
        )
        self.assertIn("required-test-missing", report["problems"])

    def test_run_default_focus_subtest_skip_and_fail_cannot_pass(self):
        for action, problem in (
            ("skip", "required-test-skip"),
            ("fail", "required-test-fail"),
        ):
            with self.subTest(action=action):
                test_stdout = replace_test_terminal(
                    default_stream(
                        self.packages, MOD.DEFAULT_REQUIRED_TESTS
                    ),
                    HTTPAPI,
                    FOCUS_SUBTEST,
                    action,
                )
                code, _ = self.run_default(
                    [proc(0, self.list_stdout), proc(0, test_stdout)]
                )
                self.assertEqual(code, 1)
                report = self.read_report()
                self.assertEqual(report["result"], "fail")
                self.assertEqual(
                    report["required_test_results"].get(FOCUS_SUBTEST),
                    action,
                )
                self.assertIn(problem, report["problems"])

    def test_run_default_focus_subtest_wrong_package_cannot_pass(self):
        test_stdout = without_test_events(
            default_stream(self.packages, MOD.DEFAULT_REQUIRED_TESTS),
            FOCUS_SUBTEST,
        )
        test_stdout += event("run", APP, FOCUS_SUBTEST)
        test_stdout += event("pass", APP, FOCUS_SUBTEST, elapsed=0.01)
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(0, test_stdout)]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertEqual(
            report["required_test_results"].get(FOCUS_SUBTEST),
            "wrong-package",
        )
        self.assertIn("required-test-wrong-package", report["problems"])

    def test_run_default_focus_subtest_duplicate_cannot_pass(self):
        test_stdout = default_stream(
            self.packages, MOD.DEFAULT_REQUIRED_TESTS
        )
        test_stdout += event("run", HTTPAPI, FOCUS_SUBTEST)
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(0, test_stdout)]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertEqual(
            report["required_test_results"].get(FOCUS_SUBTEST),
            "duplicate",
        )
        self.assertIn("test-run-duplicate", report["problems"])

    def test_run_default_focus_subtest_output_spoof_cannot_pass(self):
        test_stdout = without_test_events(
            default_stream(self.packages, MOD.DEFAULT_REQUIRED_TESTS),
            FOCUS_SUBTEST,
        )
        test_stdout += event(
            "output",
            HTTPAPI,
            output="--- PASS: " + FOCUS_SUBTEST + " (0.00s)\n",
        )
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(0, test_stdout)]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertEqual(
            report["required_test_results"].get(FOCUS_SUBTEST), "missing"
        )

    def test_run_default_focus_subtest_present_is_green(self):
        test_stdout = default_stream(
            self.packages, MOD.DEFAULT_REQUIRED_TESTS
        )
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(0, test_stdout)]
        )
        self.assertEqual(code, 0)
        report = self.read_report()
        self.assertEqual(report["result"], "pass")
        self.assertEqual(
            report["required_test_results"].get(FOCUS_SUBTEST), "pass"
        )
        self.assertEqual(
            report["required_test_results"].get(
                "TestPaymentRequestBoundary"
            ),
            "pass",
        )

    def test_run_default_rejects_new_us1_default_root_skip(self):
        target = "TestSubmitPayment"
        test_stdout = replace_test_terminal(
            default_stream(self.packages, MOD.DEFAULT_REQUIRED_TESTS),
            PAYMENT,
            target,
            "skip",
        )
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(0, test_stdout)]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertEqual(
            report["required_test_results"].get(target), "skip"
        )
        self.assertIn("required-test-skip", report["problems"])

    def test_run_default_rejects_new_us1_default_root_wrong_package(self):
        target = "TestEvidenceDecision"
        test_stdout = without_test_events(
            default_stream(self.packages, MOD.DEFAULT_REQUIRED_TESTS),
            target,
        )
        test_stdout += event("run", APP, target)
        test_stdout += event("pass", APP, target, elapsed=0.01)
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(0, test_stdout)]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertEqual(
            report["required_test_results"].get(target), "wrong-package"
        )
        self.assertIn("required-test-wrong-package", report["problems"])

    def test_run_default_new_us1_default_output_is_not_pass(self):
        target = "TestSubmitPayment"
        test_stdout = without_test_events(
            default_stream(self.packages, MOD.DEFAULT_REQUIRED_TESTS),
            target,
        )
        test_stdout += event(
            "output",
            PAYMENT,
            test=target,
            output="--- PASS: " + target + " (0.00s)\n",
        )
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(0, test_stdout)]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertEqual(
            report["required_test_results"].get(target), "missing"
        )

    def test_run_default_rejects_new_http_root_skip(self):
        target = "TestPaymentRequestBoundary"
        test_stdout = replace_test_terminal(
            default_stream(self.packages, MOD.DEFAULT_REQUIRED_TESTS),
            HTTPAPI,
            target,
            "skip",
        )
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(0, test_stdout)]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertEqual(
            report["required_test_results"].get(target), "skip"
        )
        self.assertIn("required-test-skip", report["problems"])

    def test_run_default_rejects_new_http_root_wrong_package(self):
        target = "TestPaymentResponseContract"
        test_stdout = without_test_events(
            default_stream(self.packages, MOD.DEFAULT_REQUIRED_TESTS),
            target,
        )
        test_stdout += event("run", PAYMENT, target)
        test_stdout += event("pass", PAYMENT, target, elapsed=0.01)
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(0, test_stdout)]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertEqual(
            report["required_test_results"].get(target), "wrong-package"
        )
        self.assertIn("required-test-wrong-package", report["problems"])

    def test_run_default_new_query_root_output_is_not_pass(self):
        target = "TestQueryPayment"
        test_stdout = without_test_events(
            default_stream(self.packages, MOD.DEFAULT_REQUIRED_TESTS),
            target,
        )
        test_stdout += event(
            "output",
            PAYMENT,
            test=target,
            output="--- PASS: " + target + " (0.00s)\n",
        )
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(0, test_stdout)]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertEqual(
            report["required_test_results"].get(target), "missing"
        )

    def test_run_default_rejects_directive_roots_skip(self):
        for target in PAYMENT_DIRECTIVE_DEFAULT_ROOTS:
            with self.subTest(target=target):
                package = EXPECTED_US1_DEFAULT_REQUIRED[target]
                test_stdout = replace_test_terminal(
                    default_stream(
                        self.packages, MOD.DEFAULT_REQUIRED_TESTS
                    ),
                    package,
                    target,
                    "skip",
                )
                code, _ = self.run_default(
                    [proc(0, self.list_stdout), proc(0, test_stdout)]
                )
                self.assertEqual(code, 1)
                report = self.read_report()
                self.assertEqual(report["result"], "fail")
                self.assertEqual(
                    report["required_test_results"].get(target), "skip"
                )
                self.assertIn("required-test-skip", report["problems"])

    def test_run_default_rejects_directive_roots_wrong_package(self):
        for target in PAYMENT_DIRECTIVE_DEFAULT_ROOTS:
            with self.subTest(target=target):
                package = EXPECTED_US1_DEFAULT_REQUIRED[target]
                wrong = HARNESS if package != HARNESS else PAYMENT
                test_stdout = without_test_events(
                    default_stream(
                        self.packages, MOD.DEFAULT_REQUIRED_TESTS
                    ),
                    target,
                )
                test_stdout += event("run", wrong, target)
                test_stdout += event("pass", wrong, target, elapsed=0.01)
                code, _ = self.run_default(
                    [proc(0, self.list_stdout), proc(0, test_stdout)]
                )
                self.assertEqual(code, 1)
                report = self.read_report()
                self.assertEqual(report["result"], "fail")
                self.assertEqual(
                    report["required_test_results"].get(target),
                    "wrong-package",
                )
                self.assertIn(
                    "required-test-wrong-package", report["problems"]
                )

    def test_run_default_directive_roots_output_is_not_pass(self):
        for target in PAYMENT_DIRECTIVE_DEFAULT_ROOTS:
            with self.subTest(target=target):
                package = EXPECTED_US1_DEFAULT_REQUIRED[target]
                test_stdout = without_test_events(
                    default_stream(
                        self.packages, MOD.DEFAULT_REQUIRED_TESTS
                    ),
                    target,
                )
                test_stdout += event(
                    "output",
                    package,
                    test=target,
                    output="--- PASS: " + target + " (0.00s)\n",
                )
                code, _ = self.run_default(
                    [proc(0, self.list_stdout), proc(0, test_stdout)]
                )
                self.assertEqual(code, 1)
                report = self.read_report()
                self.assertEqual(report["result"], "fail")
                self.assertEqual(
                    report["required_test_results"].get(target), "missing"
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

    def test_run_default_uses_shared_batch_budget_not_legacy_120(self):
        test_stdout = default_stream(
            self.packages, MOD.DEFAULT_REQUIRED_TESTS
        )
        code, runner = self.run_default(
            [proc(0, self.list_stdout), proc(0, test_stdout)]
        )
        self.assertEqual(code, 0)
        call = runner.call_args_list[1].args
        timeout = call[2] if len(call) > 2 else None
        self.assertEqual(timeout, 570)
        self.assertNotEqual(timeout, 120)
        self.assertEqual(runner.call_args_list[0].args[2], 60)
        self.assertEqual(
            call[0],
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

    def test_run_default_reports_test_supervision(self):
        test_stdout = default_stream(
            self.packages, MOD.DEFAULT_REQUIRED_TESTS
        )
        code, _ = self.run_default(
            [
                proc(0, self.list_stdout),
                proc(0, test_stdout, wait_elapsed_seconds=144.563),
            ]
        )
        self.assertEqual(code, 0)
        report = self.read_report()
        supervision = report.get("test_supervision")
        self.assertEqual(
            supervision,
            {
                "timeout_seconds": 570,
                "wait_elapsed_seconds": 144.563,
                "timed_out": False,
            },
        )
        self.assertEqual(
            set(supervision or {}),
            {"timeout_seconds", "wait_elapsed_seconds", "timed_out"},
        )

    def test_run_default_unknown_elapsed_is_none_not_zero(self):
        test_stdout = default_stream(
            self.packages, MOD.DEFAULT_REQUIRED_TESTS
        )
        code, _ = self.run_default(
            [proc(0, self.list_stdout), proc(0, test_stdout)]
        )
        self.assertEqual(code, 0)
        supervision = self.read_report().get("test_supervision") or {}
        self.assertIn("wait_elapsed_seconds", supervision)
        self.assertIsNone(supervision["wait_elapsed_seconds"])

    def test_run_default_timeout_reports_timed_out_supervision(self):
        code, _ = self.run_default(
            [
                proc(0, self.list_stdout),
                proc(
                    124,
                    "",
                    "",
                    timed_out=True,
                    wait_elapsed_seconds=570.001,
                ),
            ]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertIn("timeout", report["problems"])
        self.assertEqual(
            report.get("test_supervision"),
            {
                "timeout_seconds": 570,
                "wait_elapsed_seconds": 570.001,
                "timed_out": True,
            },
        )

    def test_run_default_reap_unknown_keeps_non_green_and_unknown_elapsed(self):
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
        supervision = report.get("test_supervision") or {}
        self.assertTrue(supervision.get("timed_out"))
        self.assertIsNone(supervision.get("wait_elapsed_seconds"))

    def test_run_default_io_failure_omits_test_supervision(self):
        code, _ = self.run_default(
            [proc(0, self.list_stdout), MOD.CiError("process-io-failed")]
        )
        self.assertEqual(code, 1)
        report = self.read_report()
        self.assertEqual(report["result"], "fail")
        self.assertIn("go-test-process-io-failed", report["problems"])
        self.assertNotIn("test_supervision", report)


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

    def test_success_records_wait_elapsed_from_supervision_clock(self):
        fake = self.FakeProcess([(b"out", b"err")])
        with mock.patch.object(MOD.subprocess, "Popen", return_value=fake):
            with monotonic_clock(100.0, 101.25) as clock:
                result = MOD._run_process_group(
                    ["go", "test"], {"PATH": "/bin"}, 570
                )
        self.assertEqual(
            getattr(result, "wait_elapsed_seconds", None), 1.25
        )
        self.assertEqual(clock.call_count, 2)

    def test_timeout_records_wait_elapsed_before_reap(self):
        fake = self.FakeProcess(
            [
                subprocess.TimeoutExpired(["go", "test"], 570),
                (b"out", b"err"),
            ]
        )
        with mock.patch.object(
            MOD.subprocess, "Popen", return_value=fake
        ), mock.patch.object(MOD.os, "killpg"):
            with monotonic_clock(200.0, 770.5, 1070.5) as clock:
                result = MOD._run_process_group(
                    ["go", "test"], {"PATH": "/bin"}, 570
                )
        self.assertTrue(result.timed_out)
        self.assertEqual(
            getattr(result, "wait_elapsed_seconds", None), 570.5
        )
        # The third fake clock value is the fake kill/reap cost and must not
        # be included in the supervision measurement.
        self.assertEqual(clock.call_count, 2)


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
        self.us3_epoch = "f" * 32
        self.us3_order_id = "123e4567-e89b-42d3-a456-426614174000"
        self.us3_mock_id = "223e4567-e89b-42d3-a456-426614174001"
        self.us3_record_id = "323e4567-e89b-42d3-a456-426614174002"
        self.us3_request_id = self.us3_request_id_n(1)

    def us3_request_id_n(self, index):
        return "423e4567-e89b-42d3-a456-426614174" + f"{400 + index:03d}"

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def go_default(self, result="pass"):
        return {
            "schema": "payment-hub.ci-go-default.v1",
            "generated_at": "2026-11-08T00:00:00+00:00",
            "result": result,
            "expected_packages": [
                CMD,
                APP,
                HTTPAPI,
                MOCK,
                PAYMENT,
                TESTCONTROL,
                HARNESS,
            ],
            "package_results": {
                APP: {"status": "pass", "duration": "0.1s"},
                HTTPAPI: {"status": "pass", "duration": "0.12s"},
                MOCK: {"status": "pass", "duration": "0.14s"},
                PAYMENT: {"status": "pass", "duration": "0.15s"},
                TESTCONTROL: {"status": "pass", "duration": "0.18s"},
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
            "tests": {
                "status": "pass" if result == "pass" else "fail",
                "duration": "0.4s",
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
                "go": "go1.27.4",
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

    def test_export_default_missing_new_us1_root_cannot_pass(self):
        value = self.go_default()
        value["required_test_results"] = {
            name: "pass" for name in EXPECTED_DEFAULT_REQUIRED
        }
        self.write(self.report_dir / "go-default.json", value)
        code, _ = self.export()
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "go-default.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "error")
        self.assertNotEqual(safe.get("result"), "pass")

    def test_export_default_missing_new_http_root_cannot_pass(self):
        target = "TestPaymentResponseContract"
        value = self.go_default()
        value["required_test_results"] = {
            name: "pass"
            for name in EXPECTED_DEFAULT_REQUIRED_TOTAL
            if name != target
        }
        self.write(self.report_dir / "go-default.json", value)
        code, _ = self.export()
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "go-default.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "error")
        self.assertNotEqual(safe.get("result"), "pass")

    def test_export_default_requires_focus_subtest_entry(self):
        value = self.go_default()
        value["required_test_results"] = {
            name: "pass" for name in EXPECTED_DEFAULT_REQUIRED_TOTAL
        }
        self.write(self.report_dir / "go-default.json", value)
        code, _ = self.export()
        self.assertEqual(code, 0)
        safe = json.loads(
            (
                self.report_dir / "upload" / "go-default.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "pass")
        self.assertEqual(
            len(safe["required_test_results"]),
            len(EXPECTED_DEFAULT_REQUIRED_TOTAL),
        )
        self.assertEqual(
            safe["required_test_results"].get(FOCUS_SUBTEST), "pass"
        )
        value["required_test_results"] = {
            name: "pass"
            for name in EXPECTED_DEFAULT_REQUIRED_TOTAL
            if name != FOCUS_SUBTEST
        }
        self.write(self.report_dir / "go-default.json", value)
        code, _ = self.export()
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "go-default.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "error")
        self.assertNotEqual(safe.get("result"), "pass")

    def test_export_default_old_fail_report_stays_non_green(self):
        value = self.go_default(result="fail")
        value["required_test_results"] = {
            name: "pass" for name in EXPECTED_DEFAULT_REQUIRED
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
        self.assertNotEqual(
            set(safe["required_test_results"]),
            set(MOD.DEFAULT_REQUIRED_TESTS),
        )

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

    def read_safe_default(self):
        return json.loads(
            (
                self.report_dir / "upload" / "go-default.safe.json"
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

    def test_export_db_missing_new_roots_cannot_pass(self):
        value = self.db_report()
        value["tests"]["required_test_results"] = {
            name: "pass" for name in EXPECTED_PR3_DB_REQUIRED
        }
        self.write_db(value)
        code, _ = self.export(["db"])
        self.assertEqual(code, 1)
        safe = json.loads(
            (
                self.report_dir / "upload" / "db-foundation.safe.json"
            ).read_text()
        )
        self.assertEqual(safe["status"], "error")
        self.assertNotEqual(safe.get("result"), "pass")

    def test_export_db_old_fail_report_backfills_eleven_non_green(self):
        # Historical 11-entry report remains non-green and backfills the old
        # subset; the full 24-key assertion lives in the next test.
        value = self.db_report(result="fail")
        value["tests"]["required_test_results"] = {
            name: "pass" for name in EXPECTED_PR3_DB_REQUIRED
        }
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
        for name in EXPECTED_LEGACY_DB_REQUIRED:
            if name in EXPECTED_PR3_DB_REQUIRED:
                continue
            with self.subTest(name=name):
                self.assertEqual(
                    safe["required_test_results"].get(name), "missing"
                )

    def test_export_db_old_fail_report_backfills_twenty_four_non_green(self):
        value = self.db_report(result="fail")
        value["tests"]["required_test_results"] = {
            name: "pass" for name in EXPECTED_PR3_DB_REQUIRED
        }
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
        self.assertEqual(
            len(safe["required_test_results"]),
            len(EXPECTED_DB_REQUIRED),
        )
        for name in (
            "TestOrderRegistrationIntegration",
            "TestObservationAtomicSaveIntegration",
            "TestMockTransactionIntegration",
            "TestOrderLookupIntegration",
            "TestUS1Payment",
            "TestUS1Restart",
            "TestUS1HTTPBoundary",
            "TestOrderRegistrationConcurrencyIntegration",
            "TestMockIdempotencyIntegration",
            "TestUS2Replay",
            "TestUS2Conflict",
            "TestUS2Concurrency",
            "TestUS2Restart",
            "TestUS2ReportSafety",
            "TestObservationFaultIntegration",
            "TestUS3Query",
            "TestUS3Protection",
            "TestUS3Restart",
            "TestUS3Lifecycle",
            "TestUS3ReportSafety",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    safe["required_test_results"].get(name), "missing"
                )

    def test_export_db_required_results_cover_eleven_bindings(self):
        # Historical 11-entry subset remains present in the 24-entry report.
        required = self.db_report()["tests"]["required_test_results"]
        for name in EXPECTED_LEGACY_DB_REQUIRED:
            with self.subTest(name=name):
                self.assertIn(name, required)

    def test_export_db_required_results_cover_twenty_four_bindings(self):
        self.assertEqual(
            set(self.db_report()["tests"]["required_test_results"]),
            set(EXPECTED_DB_REQUIRED),
        )
        self.assertEqual(len(EXPECTED_DB_REQUIRED), 24)

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

    US1_ORDER_ID = "11111111-1111-4111-8111-111111111111"
    US1_OTHER_ID = "44444444-4444-4444-8444-444444444444"
    US1_EPOCH = "fedcba9876543210fedcba9876543210"
    US1_BEGIN_ID = "0123456789abcdef0123456789abcdef"
    US1_EVENT_ID = US1_BEGIN_ID + ":1"

    def us1_request_id(self, index):
        return "22222222-2222-4222-8222-" + format(index, "012x")

    def us1_scenario(self, name):
        payment = name.startswith("payment_") or name.startswith("restart_")
        success = name.endswith("_success")
        if name in ("payment_success", "payment_failed"):
            generations = [1]
            statuses = [201, 200, 200]
        elif name in ("restart_success", "restart_failed"):
            generations = [1, 2]
            statuses = [201, 200, 200, 200, 200]
        elif name == "invalid_requests":
            generations = [1]
            statuses = [400, 422]
        else:
            generations = []
            statuses = [201, 400, 401, 413, 415]
        if payment:
            result = "SUCCEEDED" if success else "FAILED"
            http = [
                {
                    "status": status,
                    "request_id": self.us1_request_id(index + 1),
                    "payment_id": self.US1_ORDER_ID,
                    "result": result,
                }
                for index, status in enumerate(statuses)
            ]
            pg = [
                {"state": state, "rows": 1}
                for state in ("ORDER", "MOCK", "TRACE")
            ]
            receipts = []
            for generation in generations:
                receipts.append(
                    {
                        "run_id": self.run_id,
                        "epoch": self.US1_EPOCH,
                        "generation": generation,
                        "last_seq": 2 if generation == 1 else 0,
                    }
                )
            observer = {
                "trusted": True,
                "counts": {"payment": 1, "query": 0},
                "receipts": receipts,
            }
            directive = {
                "result": result,
                "binding": {
                    "run_id": self.run_id,
                    "epoch": self.US1_EPOCH,
                    "generation": 1,
                    "call_seq": 1,
                    "event_id": self.US1_EVENT_ID,
                    "order_id": self.US1_ORDER_ID,
                },
            }
            commands = [
                {"kind": "build", "exit": 0, "not_run": False},
                {"kind": "http_probe", "exit": 0, "not_run": False},
            ]
            if name.startswith("restart_"):
                commands.append(
                    {"kind": "process_stop", "exit": 0, "not_run": False}
                )
        else:
            if name == "invalid_requests":
                http = [
                    {
                        "status": status,
                        "request_id": (
                            "" if status == 400 else self.us1_request_id(10)
                        ),
                        "payment_id": "",
                        "result": "",
                    }
                    for status in statuses
                ]
                pg = [
                    {"state": state, "rows": 0}
                    for state in ("ORDER", "MOCK", "TRACE")
                ]
                observer = {
                    "trusted": True,
                    "counts": {"payment": 0, "query": 0},
                    "receipts": [
                        {
                            "run_id": self.run_id,
                            "epoch": self.US1_EPOCH,
                            "generation": 1,
                            "last_seq": 0,
                        }
                    ],
                }
                commands = [
                    {"kind": "build", "exit": 0, "not_run": False}
                ]
            else:
                http = []
                for index, status in enumerate(statuses):
                    if status == 201:
                        http.append(
                            {
                                "status": status,
                                "request_id": self.us1_request_id(20),
                                "payment_id": self.US1_OTHER_ID,
                                "result": "SUCCEEDED",
                            }
                        )
                    else:
                        http.append(
                            {
                                "status": status,
                                "request_id": (
                                    "" if status == 400
                                    else self.us1_request_id(21 + index)
                                ),
                                "payment_id": "",
                                "result": "",
                            }
                        )
                pg = []
                observer = None
                commands = [
                    {"kind": "http_probe", "exit": 0, "not_run": False}
                ]
            directive = None
        return {
            "name": name,
            "result": "PASS",
            "generations": generations,
            "http": http,
            "pg": pg,
            "observer": observer,
            "directive": directive,
            "commands": commands,
        }

    def us1_report(self):
        return {
            "schema": "payment-hub.us1.v1",
            "run_id": self.run_id,
            "scenarios": [
                self.us1_scenario(name)
                for name in (
                    "payment_success",
                    "payment_failed",
                    "invalid_requests",
                    "restart_success",
                    "restart_failed",
                    "http_boundary",
                )
            ],
        }

    def write_us1(self, value, run_id=None):
        run_id = value.get("run_id", self.run_id) if run_id is None else run_id
        path = self.db_root / run_id / "us1.json"
        self.write(path, value)
        return path

    def export_db_us1(self):
        return self.export(["db", "us1"])

    def read_us1_safe(self):
        path = self.report_dir / "upload" / "us1.safe.json"
        self.assertTrue(path.is_file(), "us1 safe report is missing")
        return json.loads(path.read_text())

    def test_export_us1_complete_pass_rebuilds_safe_report(self):
        self.write_db(self.db_report())
        self.write_us1(self.us1_report())
        code, _ = self.export_db_us1()
        self.assertEqual(code, 0)
        safe = self.read_us1_safe()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "pass")
        self.assertEqual(len(safe["scenarios"]), 6)
        self.assertEqual(safe["run_id"], self.run_id)
        text = json.dumps(safe)
        for forbidden in ("SECRET", "Authorization", "postgres://"):
            self.assertNotIn(forbidden, text)
        provenance = json.loads(
            (self.report_dir / "upload" / "provenance.json").read_text()
        )
        record = next(
            entry for entry in provenance["reports"] if entry["name"] == "us1"
        )
        self.assertEqual(record["source"], self.run_id + "/us1.json")
        self.assertEqual(record["export"], "us1.safe.json")
        self.assertRegex(record["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(record["export_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(record["source_manifest_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            record["source_manifest_verification"], "verified-unchanged"
        )

    def test_export_us1_partial_failure_exports_non_green(self):
        value = self.us1_report()
        value["scenarios"][1]["result"] = "FAIL"
        self.write_db(self.db_report())
        self.write_us1(value)
        code, _ = self.export_db_us1()
        self.assertEqual(code, 1)
        safe = self.read_us1_safe()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")

    def test_export_us1_missing_scenario_is_non_green(self):
        value = self.us1_report()
        value["scenarios"] = value["scenarios"][:-1]
        self.write_db(self.db_report())
        self.write_us1(value)
        code, _ = self.export_db_us1()
        self.assertEqual(code, 1)
        safe = self.read_us1_safe()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")
        self.assertIn("http_boundary", safe["missing_scenarios"])

    def test_export_us1_duplicate_scenario_rejected(self):
        value = self.us1_report()
        value["scenarios"].append(value["scenarios"][0])
        self.write_db(self.db_report())
        self.write_us1(value)
        code, _ = self.export_db_us1()
        self.assertEqual(code, 1)
        safe = self.read_us1_safe()
        self.assertEqual(safe["status"], "error")
        self.assertNotEqual(safe.get("result"), "pass")

    def test_export_us1_unknown_sensitive_field_rejected(self):
        value = self.us1_report()
        value["authorization"] = "Bearer SECRET_CANARY"
        value["scenarios"][0]["raw_log"] = "SECRET_CANARY"
        self.write_db(self.db_report())
        self.write_us1(value)
        code, _ = self.export_db_us1()
        self.assertEqual(code, 1)
        path = self.report_dir / "upload" / "us1.safe.json"
        self.assertTrue(path.is_file(), "us1 safe report is missing")
        text = path.read_text()
        self.assertNotIn("SECRET_CANARY", text)
        safe = json.loads(text)
        self.assertEqual(safe["status"], "error")

    def test_export_us1_invalid_identity_epoch_rejected(self):
        value = self.us1_report()
        value["scenarios"][0]["directive"]["binding"]["epoch"] = -1
        self.write_db(self.db_report())
        self.write_us1(value)
        code, _ = self.export_db_us1()
        self.assertEqual(code, 1)
        self.assertEqual(self.read_us1_safe()["status"], "error")

    def test_export_us1_binding_miscompare_is_non_green(self):
        value = self.us1_report()
        value["scenarios"][0]["directive"]["binding"]["order_id"] = (
            self.US1_OTHER_ID
        )
        self.write_db(self.db_report())
        self.write_us1(value)
        code, _ = self.export_db_us1()
        self.assertEqual(code, 1)
        safe = self.read_us1_safe()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")

    def test_export_us1_untrusted_receipts_is_non_green(self):
        value = self.us1_report()
        value["scenarios"][0]["observer"]["trusted"] = False
        self.write_db(self.db_report())
        self.write_us1(value)
        code, _ = self.export_db_us1()
        self.assertEqual(code, 1)
        safe = self.read_us1_safe()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")

    def test_export_us1_wrong_result_count_is_non_green(self):
        value = self.us1_report()
        value["scenarios"][0]["http"] = value["scenarios"][0]["http"][:2]
        value["scenarios"][1]["pg"][2]["rows"] = 2
        self.write_db(self.db_report())
        self.write_us1(value)
        code, _ = self.export_db_us1()
        self.assertEqual(code, 1)
        safe = self.read_us1_safe()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")

    def test_export_us1_restart_identity_mismatch_is_non_green(self):
        value = self.us1_report()
        restart = value["scenarios"][3]
        restart["http"][-1]["payment_id"] = self.US1_OTHER_ID
        self.write_db(self.db_report())
        self.write_us1(value)
        code, _ = self.export_db_us1()
        self.assertEqual(code, 1)
        safe = self.read_us1_safe()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")

    def test_export_us1_run_id_mismatch_rejected(self):
        value = self.us1_report()
        value["run_id"] = "b" * 32
        self.write_db(self.db_report())
        self.write_us1(value, run_id=self.run_id)
        code, _ = self.export_db_us1()
        self.assertEqual(code, 1)
        self.assertEqual(self.read_us1_safe()["status"], "error")

    def test_export_us1_db_test_failed_cannot_pass(self):
        self.write_db(self.db_report(result="fail"))
        self.write_us1(self.us1_report())
        code, _ = self.export_db_us1()
        self.assertEqual(code, 1)
        safe = self.read_us1_safe()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")

    def test_export_us1_missing_file_is_missing_nonzero(self):
        self.write_db(self.db_report())
        code, _ = self.export_db_us1()
        self.assertEqual(code, 1)
        safe = self.read_us1_safe()
        self.assertEqual(safe["status"], "missing")

    def test_export_us1_multiple_db_candidates_rejected(self):
        self.write_db(self.db_report())
        other = "f" * 32
        value = self.db_report()
        value["run_id"] = other
        value["project"] = "payment-hub-test-db-" + other
        self.write_db(value, run_id=other)
        self.write_us1(self.us1_report())
        code, _ = self.export_db_us1()
        self.assertEqual(code, 1)
        self.assertEqual(self.read_us1_safe()["status"], "error")

    def test_export_us1_source_verification_three_states(self):
        value = self.db_report(result="fail")
        value["source"]["manifest_unchanged"] = None
        self.write_db(value)
        self.write_us1(self.us1_report())
        code, _ = self.export_db_us1()
        self.assertEqual(code, 1)
        safe = self.read_us1_safe()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")
        self.assertEqual(
            safe["source"]["manifest_verification"], "not-verified"
        )

    def test_export_us1_commands_reject_unobserved_kind(self):
        value = self.us1_report()
        value["scenarios"][0]["commands"].append(
            {"kind": "go_test", "exit": 0, "not_run": False}
        )
        self.write_db(self.db_report())
        self.write_us1(value)
        code, _ = self.export_db_us1()
        self.assertEqual(code, 1)
        self.assertEqual(self.read_us1_safe()["status"], "error")

    def export_us1_case(self, value):
        self.write_db(self.db_report())
        self.write_us1(value)
        return self.export_db_us1()

    def assert_us1_error(self, value):
        code, _ = self.export_us1_case(value)
        self.assertEqual(code, 1)
        safe = self.read_us1_safe()
        self.assertEqual(safe["status"], "error")

    def assert_us1_non_green(self, value):
        code, _ = self.export_us1_case(value)
        self.assertEqual(code, 1)
        safe = self.read_us1_safe()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")

    def test_export_us1_epoch_must_be_lowerhex32(self):
        for mutation in ("receipt-upper", "receipt-int", "binding-int"):
            with self.subTest(mutation=mutation):
                value = self.us1_report()
                if mutation == "receipt-upper":
                    value["scenarios"][0]["observer"]["receipts"][0][
                        "epoch"
                    ] = self.US1_EPOCH.upper()
                elif mutation == "receipt-int":
                    value["scenarios"][0]["observer"]["receipts"][0][
                        "epoch"
                    ] = 1
                else:
                    value["scenarios"][0]["directive"]["binding"][
                        "epoch"
                    ] = 1
                self.assert_us1_error(value)

    def test_export_us1_event_id_must_match_call_seq(self):
        for mutation in ("uuid", "suffix", "prefix", "too-large"):
            with self.subTest(mutation=mutation):
                value = self.us1_report()
                binding = value["scenarios"][0]["directive"]["binding"]
                if mutation == "uuid":
                    binding["event_id"] = self.US1_OTHER_ID
                elif mutation == "suffix":
                    binding["event_id"] = self.US1_BEGIN_ID + ":2"
                elif mutation == "prefix":
                    binding["event_id"] = self.US1_BEGIN_ID.upper() + ":1"
                else:
                    binding["call_seq"] = 4097
                    binding["event_id"] = self.US1_BEGIN_ID + ":4097"
                self.assert_us1_error(value)

    def test_export_us1_receipt_binding_epoch_mismatch_is_non_green(self):
        value = self.us1_report()
        value["scenarios"][0]["directive"]["binding"]["epoch"] = "a" * 32
        self.assert_us1_non_green(value)

    def test_export_us1_invalid_observer_requires_real_receipt(self):
        for mutation in (
            "empty-receipts",
            "generation-two",
            "untrusted",
            "payment-count",
        ):
            with self.subTest(mutation=mutation):
                value = self.us1_report()
                observer = value["scenarios"][2]["observer"]
                if mutation == "empty-receipts":
                    observer["receipts"] = []
                elif mutation == "generation-two":
                    observer["receipts"][0]["generation"] = 2
                elif mutation == "untrusted":
                    observer["trusted"] = False
                else:
                    observer["counts"]["payment"] = 1
                self.assert_us1_non_green(value)

    def test_export_us1_boundary_requires_legal_201_and_reject_matrix(self):
        for mutation in (
            "no-201",
            "201-empty-payment",
            "4xx-payment-id",
            "missing-401",
        ):
            with self.subTest(mutation=mutation):
                value = self.us1_report()
                http = value["scenarios"][5]["http"]
                if mutation == "no-201":
                    value["scenarios"][5]["http"] = [
                        item for item in http if item["status"] != 201
                    ]
                elif mutation == "201-empty-payment":
                    next(
                        item for item in http if item["status"] == 201
                    )["payment_id"] = ""
                elif mutation == "4xx-payment-id":
                    next(
                        item for item in http if item["status"] == 413
                    )["payment_id"] = self.US1_OTHER_ID
                else:
                    value["scenarios"][5]["http"] = [
                        item for item in http if item["status"] != 401
                    ]
                self.assert_us1_non_green(value)

    def test_export_us1_payment_http_identity_gaps_are_non_green(self):
        for mutation in (
            "empty-request-id",
            "duplicate-request-id",
            "empty-result",
            "empty-payment-id",
        ):
            with self.subTest(mutation=mutation):
                value = self.us1_report()
                http = value["scenarios"][0]["http"]
                if mutation == "empty-request-id":
                    http[1]["request_id"] = ""
                elif mutation == "duplicate-request-id":
                    http[1]["request_id"] = http[0]["request_id"]
                elif mutation == "empty-result":
                    http[1]["result"] = ""
                else:
                    http[1]["payment_id"] = ""
                self.assert_us1_non_green(value)

    def test_export_us1_commands_must_be_observed(self):
        for mutation in ("empty", "nonzero"):
            with self.subTest(mutation=mutation):
                value = self.us1_report()
                if mutation == "empty":
                    value["scenarios"][0]["commands"] = []
                else:
                    value["scenarios"][0]["commands"][0]["exit"] = 1
                self.assert_us1_non_green(value)

    def test_export_us1_receipt_last_seq_must_cover_call_seq(self):
        value = self.us1_report()
        value["scenarios"][0]["observer"]["receipts"][0]["last_seq"] = 1
        self.assert_us1_non_green(value)

    def test_export_us1_future_binding_generation_is_non_green(self):
        value = self.us1_report()
        value["scenarios"][0]["directive"]["binding"]["generation"] = 2
        self.assert_us1_non_green(value)

    US2_ORDER_ID = "11111111-1111-4111-8111-111111111111"
    US2_OTHER_ID = "44444444-4444-4444-8444-444444444444"
    US2_EPOCH = "fedcba9876543210fedcba9876543210"
    US2_BEGIN_ID = "0123456789abcdef0123456789abcdef"
    US2_EVENT_ID = US2_BEGIN_ID + ":1"
    US2_SCENARIOS = (
        "replay_success",
        "replay_failed",
        "replay_unknown",
        "conflict_priority",
        "concurrency",
        "restart_success",
        "restart_failed",
        "key_rotation",
    )

    def us2_request_id(self, index):
        return "33333333-3333-4333-8333-" + format(index, "012x")

    def us2_counts(self, payment, query, order, mock, trace):
        return {
            "payment_calls": payment,
            "query_calls": query,
            "order_rows": order,
            "mock_rows": mock,
            "trace_rows": trace,
        }

    def us2_checks(
        self,
        before,
        after,
        *,
        stable_facts=True,
        readonly_probes=0,
        lookup_fault="none",
        waiter_count=0,
        mixed_waiter_count=0,
    ):
        return {
            "counts_before": dict(before),
            "counts_after": dict(after),
            "stable_facts": stable_facts,
            "readonly_probes": readonly_probes,
            "lookup_fault": lookup_fault,
            "waiter_count": waiter_count,
            "mixed_waiter_count": mixed_waiter_count,
        }

    def us2_http(self, status, request_id, payment_id, result):
        return {
            "status": status,
            "request_id": request_id,
            "payment_id": payment_id,
            "result": result,
        }

    def us2_directive(self, order_id=None):
        return {
            "result": "SUCCEEDED",
            "binding": {
                "run_id": self.run_id,
                "epoch": self.US2_EPOCH,
                "generation": 1,
                "call_seq": 1,
                "event_id": self.US2_EVENT_ID,
                "order_id": order_id or self.US2_ORDER_ID,
            },
        }

    def us2_commands(self):
        # Producer facts.commands records build + process_stop. HTTP probes are
        # represented by the http array and test assertions, not necessarily a
        # separate command record; http_probe remains allowed but optional.
        return [
            {"kind": "build", "exit": 0, "not_run": False},
            {"kind": "process_stop", "exit": 0, "not_run": False},
        ]

    def us2_scenario(self, name):
        if name == "replay_unknown":
            counts = self.us2_counts(0, 0, 1, 0, 0)
            checks = self.us2_checks(counts, counts)
            generations = [1]
            http = [
                self.us2_http(200, self.us2_request_id(1), self.US2_ORDER_ID, "UNKNOWN"),
                self.us2_http(200, self.us2_request_id(2), self.US2_ORDER_ID, "UNKNOWN"),
            ]
            directive = None
            commands = self.us2_commands()
        elif name == "concurrency":
            before = self.us2_counts(0, 0, 0, 0, 0)
            after = self.us2_counts(1, 0, 1, 1, 1)
            checks = self.us2_checks(
                before, after, waiter_count=2, mixed_waiter_count=2
            )
            generations = [1]
            http = [
                self.us2_http(201, self.us2_request_id(1), self.US2_ORDER_ID, "SUCCEEDED"),
                self.us2_http(200, self.us2_request_id(2), self.US2_ORDER_ID, "UNKNOWN"),
                self.us2_http(200, self.us2_request_id(3), self.US2_ORDER_ID, "SUCCEEDED"),
                self.us2_http(409, "", "", ""),
                self.us2_http(200, self.us2_request_id(4), self.US2_ORDER_ID, "SUCCEEDED"),
                self.us2_http(200, self.us2_request_id(5), self.US2_ORDER_ID, "SUCCEEDED"),
            ]
            directive = self.us2_directive()
            commands = self.us2_commands()
        elif name == "conflict_priority":
            counts = self.us2_counts(1, 0, 1, 1, 1)
            checks = self.us2_checks(
                counts,
                counts,
                readonly_probes=5,
                lookup_fault="timeout",
            )
            generations = [1]
            http = [
                self.us2_http(201, self.us2_request_id(1), self.US2_ORDER_ID, "SUCCEEDED"),
                self.us2_http(409, "", "", ""),
                self.us2_http(409, "", "", ""),
                self.us2_http(400, "", "", ""),
                self.us2_http(400, "", "", ""),
                self.us2_http(400, "", "", ""),
                self.us2_http(400, "", "", ""),
                self.us2_http(401, "", "", ""),
                self.us2_http(503, "", "", ""),
            ]
            directive = None
            commands = self.us2_commands()
        else:
            counts = self.us2_counts(1, 0, 1, 1, 1)
            checks = self.us2_checks(counts, counts)
            if name in ("restart_success", "restart_failed"):
                generations = [1, 2]
            elif name == "key_rotation":
                generations = [1, 2]
            else:
                generations = [1]
            result = (
                "FAILED" if name.endswith("_failed") else "SUCCEEDED"
            )
            http = [
                self.us2_http(
                    201, self.us2_request_id(1), self.US2_ORDER_ID, result
                ),
                self.us2_http(
                    200, self.us2_request_id(2), self.US2_ORDER_ID, result
                ),
                self.us2_http(
                    200, self.us2_request_id(3), self.US2_ORDER_ID, result
                ),
            ]
            if name == "key_rotation":
                http.append(self.us2_http(401, "", "", ""))
            directive = (
                self.us2_directive()
                if name in ("key_rotation",)
                else None
            )
            commands = self.us2_commands()
        receipts = [
            {
                "run_id": self.run_id,
                "epoch": self.US2_EPOCH,
                "generation": generation,
                "last_seq": 2,
            }
            for generation in generations
        ]
        observer = {
            "trusted": True,
            "counts": {
                "payment": checks["counts_after"]["payment_calls"],
                "query": checks["counts_after"]["query_calls"],
            },
            "receipts": receipts,
        }
        pg = [
            {"state": "ORDER", "rows": checks["counts_after"]["order_rows"]},
            {"state": "MOCK", "rows": checks["counts_after"]["mock_rows"]},
            {"state": "TRACE", "rows": checks["counts_after"]["trace_rows"]},
        ]
        return {
            "name": name,
            "result": "PASS",
            "generations": generations,
            "http": http,
            "pg": pg,
            "observer": observer,
            "directive": directive,
            "commands": commands,
            "checks": checks,
        }

    def us2_report(self):
        return {
            "schema": "payment-hub.us2.v1",
            "run_id": self.run_id,
            "scenarios": [
                self.us2_scenario(name) for name in self.US2_SCENARIOS
            ],
        }

    def write_us2(self, value, run_id=None):
        run_id = value.get("run_id", self.run_id) if run_id is None else run_id
        path = self.db_root / run_id / "us2.json"
        self.write(path, value)
        return path

    def export_db_us2(self):
        return self.export(["db", "us2"])

    def read_us2_safe(self):
        path = self.report_dir / "upload" / "us2.safe.json"
        self.assertTrue(path.is_file(), "us2 safe report is missing")
        return json.loads(path.read_text())

    def us3_counts(self, payment, query, order, mock, trace):
        return {
            "payment_calls": payment,
            "query_calls": query,
            "order_rows": order,
            "mock_rows": mock,
            "trace_rows": trace,
        }

    def us3_fact(self, result="SUCCEEDED"):
        value = {
            "payment_id": self.us3_order_id,
            "client_ref_sha256": "a" * 64,
            "business_ref_sha256": "b" * 64,
            "amount": 100,
            "currency": "CNY",
            "result": result,
            "registered_at": "2026-11-11T00:00:00Z",
        }
        if result == "SUCCEEDED":
            value["confirmed_at"] = "2026-11-11T00:00:01Z"
        return value

    def us3_http(
        self,
        kind="PRIMARY",
        status=200,
        result="SUCCEEDED",
        issues=None,
        request_id=None,
        generation=1,
    ):
        return {
            "kind": kind,
            "generation": generation,
            "status": status,
            "request_id": self.us3_request_id if request_id is None else request_id,
            "payment_id": self.us3_order_id,
            "result": result,
            "issues": [] if issues is None else issues,
        }

    def us3_binding(self, generation=1, call_seq=1):
        return {
            "run_id": self.run_id,
            "epoch": self.us3_epoch,
            "generation": generation,
            "call_seq": call_seq,
            "event_id": self.us3_epoch + ":" + str(call_seq),
            "order_id": self.us3_order_id,
        }

    def us3_query(
        self,
        directive="normal",
        outcome="found",
        decision="ACCEPT_CONFIRMATION",
        before="UNKNOWN",
        after="SUCCEEDED",
        trace_outcome="SAVED",
        applied=True,
        generation=1,
        call_seq=1,
        trusted=True,
        path="BY_BUSINESS_ORDER_NO",
        request_id=None,
        confirmed_returned=True,
    ):
        return {
            "generation": generation,
            "path": path,
            "query_call_seq": call_seq,
            "request_id": self.us3_request_id if request_id is None else request_id,
            "directive": directive,
            "outcome": outcome,
            "applied": applied,
            "apply_count": 1 if applied else 0,
            "decision": decision,
            "result_before": before,
            "result_after": after,
            "trace_outcome": trace_outcome,
            "confirmed_returned": confirmed_returned,
            "new_mock_calls": 0,
            "binding": self.us3_binding(generation, call_seq),
            "trusted": trusted,
        }

    def us3_record(
        self,
        decision="ACCEPT_CONFIRMATION",
        before="UNKNOWN",
        after="SUCCEEDED",
        reason="",
        differences=None,
        source="QUERY",
        request_id=None,
    ):
        evidence = {"differences": [] if differences is None else differences}
        if decision == "ACCEPT_CONFIRMATION":
            evidence.update(
                {
                    "platform_reference_id": self.us3_mock_id,
                    "mock_transaction_id": self.us3_mock_id,
                    "amount": 100,
                    "currency": "CNY",
                    "result": "SUCCEEDED",
                }
            )
        return {
            "record_id": self.us3_record_id,
            "request_id": self.us3_request_id if request_id is None else request_id,
            "source": source,
            "decision": decision,
            "result_before": before,
            "result_after": after,
            "reason_code": reason,
            "evidence": evidence,
        }

    def us3_complete_proof(self, generation=1, last_seq=1):
        return {
            "generation": generation,
            "mode": "complete",
            "sealed": True,
            "trusted": True,
            "receipt": {
                "run_id": self.run_id,
                "epoch": self.us3_epoch,
                "generation": generation,
                "last_seq": last_seq,
            },
            "counts": self.us3_counts(0, 0, 0, 0, 0),
            "exact_counts": True,
        }

    def us3_controlled(self, point="REGISTERED"):
        return {
            "binding": {
                "run_id": self.run_id,
                "epoch": self.us3_epoch,
                "generation": 1,
                "point": point,
                "order_id": self.us3_order_id,
                "call_seq": 1,
                "event_id": self.us3_epoch + ":1",
                "prefix_count": 0,
            },
            "exit": {
                "code": -1,
                "canceled": True,
                "reaped": True,
                "group_gone": True,
                "output_complete": True,
            },
            "controlled_truncated": True,
            "no_receipt": True,
            "exact_counts": False,
            "observed_prefix": self.us3_counts(0, 0, 0, 0, 0),
        }

    def us3_scenario(self, name):
        scenario = {
            "name": name,
            "result": "PASS",
            "generations": [1],
            "transport": "RESPONSE",
            "http": [self.us3_http()],
            "pg": [
                {"state": "ORDER", "rows": 1},
                {"state": "MOCK", "rows": 1},
                {"state": "TRACE", "rows": 1},
            ],
            "calls": [
                {
                    "generation": 1,
                    "exact": True,
                    "payment_calls": 1,
                    "query_calls": 0,
                }
            ],
            "records": [],
            "queries": [],
            "faults": [],
            "facts": {
                "before": self.us3_fact("SUCCEEDED"),
                "after": self.us3_fact("SUCCEEDED"),
                "transition": "UNCHANGED",
                "stable_facts": True,
            },
            "checks": {
                "counts_before": self.us3_counts(0, 0, 1, 1, 1),
                "counts_after": self.us3_counts(1, 0, 1, 1, 1),
                "counts_exact": True,
                "stable_facts": True,
            },
            "generation_proofs": [self.us3_complete_proof(1)],
            "budgets": [],
            "commands": [
                {"kind": "build", "exit": 0, "not_run": False},
                {"kind": "process_stop", "exit": 0, "not_run": False},
            ],
        }
        if name == "submit_unknown_no_transaction":
            scenario["http"] = [
                self.us3_http(status=200, result="UNKNOWN", issues=["MOCK_RESULT_UNCONFIRMED"])
            ]
            scenario["pg"] = [
                {"state": "ORDER", "rows": 1},
                {"state": "MOCK", "rows": 0},
                {"state": "TRACE", "rows": 0},
            ]
            scenario["records"] = [
                self.us3_record(
                    decision="KEEP_UNKNOWN",
                    before="UNKNOWN",
                    after="UNKNOWN",
                    reason="MOCK_RESULT_UNCONFIRMED",
                    source="PAYMENT",
                )
            ]
            scenario["facts"] = {
                "after": self.us3_fact("UNKNOWN"),
                "transition": "CREATED_UNKNOWN",
                "stable_facts": True,
            }
            scenario["checks"]["counts_before"] = self.us3_counts(0, 0, 0, 0, 0)
            scenario["checks"]["counts_after"] = self.us3_counts(1, 0, 1, 0, 0)
            scenario["budgets"] = [
                {
                    "class": "CONTROLLED",
                    "kind": "MOCK_DB_SUBTIMEOUT",
                    "source": "APP",
                    "pass_budget": False,
                    "limit_ms": 2000,
                    "elapsed_ms": 1500,
                },
                {
                    "class": "NORMAL",
                    "kind": "MOCK",
                    "source": "MOCK",
                    "pass_budget": True,
                    "limit_ms": 3000,
                    "elapsed_ms": 1500,
                },
            ]
        elif name == "submit_commit_unknown":
            scenario["http"] = [
                self.us3_http(status=200, result="UNKNOWN", issues=["RESULT_SAVE_ERROR"])
            ]
            scenario["pg"] = [
                {"state": "ORDER", "rows": 1},
                {"state": "MOCK", "rows": 0},
                {"state": "TRACE", "rows": 0},
            ]
            scenario["records"] = []
            scenario["facts"] = {
                "after": self.us3_fact("UNKNOWN"),
                "transition": "CREATED_UNKNOWN",
                "stable_facts": False,
            }
            scenario["checks"] = {
                "counts_before": self.us3_counts(0, 0, 0, 0, 0),
                "counts_after": self.us3_counts(0, 0, 1, 0, 0),
                "counts_exact": True,
                "stable_facts": False,
            }
            scenario["faults"] = [
                {
                    "kind": "REGISTRATION_COMMIT_REPLY_LOST",
                    "sqlstate": "",
                    "sqlstate_source": "none",
                    "fixture_copy": False,
                    "platform_table_read": False,
                    "cleanup": {
                        "kind": "UDS_SCENARIO",
                        "removed": True,
                        "verified": True,
                    },
                }
            ]
            scenario["save"] = {
                "attempts": 1,
                "record_id": "",
                "commit_state": "INDETERMINATE",
                "application_readback": "ORDER_ONLY",
                "external_pg_readback": "NONE",
                "readback_verified": True,
                "readback_scope": "APPLICATION_ORDER_ONLY",
                "readback_source": "APP",
                "created_claimed": False,
                "rollback_claimed": False,
                "candidate_id_leaked": False,
            }
        elif name == "query_unknown_confirmed":
            scenario["queries"] = [self.us3_query()]
            scenario["records"] = [self.us3_record()]
            scenario["facts"] = {
                "before": self.us3_fact("UNKNOWN"),
                "after": self.us3_fact("SUCCEEDED"),
                "transition": "UNKNOWN_TO_CONFIRMED",
                "stable_facts": True,
            }
            scenario["checks"]["counts_after"] = self.us3_counts(1, 1, 1, 1, 1)
            scenario["trace"] = {
                "outcome": "SAVED",
                "issue": "",
                "sqlstate": "00000",
                "sqlstate_source": "application_observed",
                "confirmed_fact_returned": True,
                "new_lookup": False,
                "proof_source": "OBSERVER",
            }
        elif name == "query_unknown_failure":
            scenario["queries"] = [
                self.us3_query(
                    directive="failure",
                    outcome="unavailable",
                    decision="KEEP_UNKNOWN",
                    before="UNKNOWN",
                    after="UNKNOWN",
                    trace_outcome="SAVED",
                )
            ]
            scenario["records"] = [
                self.us3_record(
                    decision="KEEP_UNKNOWN",
                    before="UNKNOWN",
                    after="UNKNOWN",
                    reason="MOCK_RESULT_UNCONFIRMED",
                )
            ]
            scenario["http"][0]["issues"] = ["MOCK_RESULT_UNCONFIRMED"]
            scenario["faults"] = [
                {
                    "kind": "MOCK_QUERY_FAILURE",
                    "sqlstate": "08006",
                    "sqlstate_source": "application_observed",
                    "fixture_copy": False,
                    "platform_table_read": False,
                    "cleanup": {
                        "kind": "UDS_SCENARIO",
                        "removed": True,
                        "verified": True,
                    },
                }
            ]
            scenario["facts"] = {
                "before": self.us3_fact("UNKNOWN"),
                "after": self.us3_fact("UNKNOWN"),
                "transition": "UNCHANGED",
                "stable_facts": True,
            }
            scenario["checks"]["counts_after"] = self.us3_counts(1, 1, 1, 1, 0)
        elif name == "query_unknown_subtimeout":
            scenario["queries"] = [
                self.us3_query(
                    directive="subtimeout",
                    outcome="subtimeout",
                    decision="KEEP_UNKNOWN",
                    before="UNKNOWN",
                    after="UNKNOWN",
                    trace_outcome="SAVED",
                )
            ]
            scenario["records"] = [
                self.us3_record(
                    decision="KEEP_UNKNOWN",
                    before="UNKNOWN",
                    after="UNKNOWN",
                    reason="MOCK_RESULT_UNCONFIRMED",
                )
            ]
            scenario["http"][0]["issues"] = ["MOCK_RESULT_UNCONFIRMED"]
            scenario["faults"] = [
                {
                    "kind": "MOCK_QUERY_SUBTIMEOUT_250MS",
                    "sqlstate": "57014",
                    "sqlstate_source": "application_observed",
                    "fixture_copy": False,
                    "platform_table_read": False,
                    "cleanup": {
                        "kind": "UDS_SCENARIO",
                        "removed": True,
                        "verified": True,
                    },
                }
            ]
            scenario["budgets"] = [
                {
                    "class": "CONTROLLED",
                    "kind": "QUERY_SUBTIMEOUT",
                    "source": "APP",
                    "pass_budget": False,
                    "limit_ms": 250,
                    "elapsed_ms": 251,
                },
                {
                    "class": "NORMAL",
                    "kind": "MOCK",
                    "source": "MOCK",
                    "pass_budget": True,
                    "limit_ms": 3000,
                    "elapsed_ms": 251,
                },
            ]
            scenario["facts"] = {
                "before": self.us3_fact("UNKNOWN"),
                "after": self.us3_fact("UNKNOWN"),
                "transition": "UNCHANGED",
                "stable_facts": True,
            }
            scenario["checks"]["counts_after"] = self.us3_counts(1, 1, 1, 1, 0)
        elif name == "query_unknown_not_visible":
            scenario["queries"] = [
                self.us3_query(
                    directive="notvisible",
                    outcome="not_found",
                    decision="KEEP_UNKNOWN",
                    before="UNKNOWN",
                    after="UNKNOWN",
                    request_id=self.us3_request_id_n(1),
                ),
                self.us3_query(
                    call_seq=2, request_id=self.us3_request_id_n(2)
                ),
            ]
            scenario["records"] = [
                self.us3_record(
                    decision="KEEP_UNKNOWN",
                    before="UNKNOWN",
                    after="UNKNOWN",
                    reason="MOCK_RESULT_NOT_FOUND",
                    request_id=self.us3_request_id_n(1),
                ),
                self.us3_record(request_id=self.us3_request_id_n(2)),
            ]
            scenario["http"] = [
                self.us3_http(
                    status=200,
                    result="UNKNOWN",
                    issues=["MOCK_RESULT_NOT_FOUND"],
                    request_id=self.us3_request_id_n(1),
                ),
                self.us3_http(request_id=self.us3_request_id_n(2)),
            ]
            scenario["faults"] = [
                {
                    "kind": "MOCK_QUERY_NOT_VISIBLE",
                    "sqlstate": "",
                    "sqlstate_source": "none",
                    "fixture_copy": False,
                    "platform_table_read": False,
                    "cleanup": {
                        "kind": "UDS_SCENARIO",
                        "removed": True,
                        "verified": True,
                    },
                }
            ]
            scenario["facts"] = {
                "before": self.us3_fact("UNKNOWN"),
                "after": self.us3_fact("SUCCEEDED"),
                "transition": "UNKNOWN_TO_CONFIRMED",
                "stable_facts": True,
            }
            scenario["checks"]["counts_after"] = self.us3_counts(1, 1, 1, 1, 1)
            scenario["trace"] = {
                "outcome": "SAVED",
                "issue": "",
                "sqlstate": "00000",
                "sqlstate_source": "application_observed",
                "confirmed_fact_returned": True,
                "new_lookup": False,
                "proof_source": "OBSERVER",
            }
        elif name == "query_unknown_no_mock_match":
            scenario["queries"] = [
                self.us3_query(
                    outcome="not_found",
                    decision="KEEP_UNKNOWN",
                    before="UNKNOWN",
                    after="UNKNOWN",
                    trace_outcome="SAVED",
                )
            ]
            scenario["records"] = [
                self.us3_record(
                    decision="KEEP_UNKNOWN",
                    before="UNKNOWN",
                    after="UNKNOWN",
                    reason="MOCK_RESULT_NOT_FOUND",
                )
            ]
            scenario["http"][0]["issues"] = ["MOCK_RESULT_NOT_FOUND"]
            scenario["pg"] = [
                {"state": "ORDER", "rows": 1},
                {"state": "MOCK", "rows": 0},
                {"state": "TRACE", "rows": 0},
            ]
            scenario["facts"] = {
                "before": self.us3_fact("UNKNOWN"),
                "after": self.us3_fact("UNKNOWN"),
                "transition": "UNCHANGED",
                "stable_facts": True,
            }
            scenario["checks"]["counts_after"] = self.us3_counts(1, 1, 1, 0, 0)
        elif name.startswith("query_mismatch_"):
            difference = {
                "query_mismatch_reference": "platform_reference_id",
                "query_mismatch_amount": "amount",
                "query_mismatch_currency": "currency",
                "query_mismatch_transaction": "mock_transaction_id",
            }[name]
            directive = {
                "platform_reference_id": "mismatch_reference",
                "amount": "mismatch_amount",
                "currency": "mismatch_currency",
                "mock_transaction_id": "mismatch_transaction",
            }[difference]
            scenario["queries"] = [
                self.us3_query(
                    directive=directive,
                    outcome="mismatch_applied",
                    decision="REJECT_EVIDENCE",
                    before="UNKNOWN",
                    after="UNKNOWN",
                    trace_outcome="SAVED",
                )
            ]
            scenario["records"] = [
                self.us3_record(
                    decision="REJECT_EVIDENCE",
                    before="UNKNOWN",
                    after="UNKNOWN",
                    reason="MOCK_EVIDENCE_MISMATCH",
                    differences=[difference],
                )
            ]
            scenario["http"][0]["issues"] = ["MOCK_EVIDENCE_REJECTED"]
            scenario["faults"] = [
                {
                    "kind": {
                        "platform_reference_id": "MISMATCH_REFERENCE",
                        "amount": "MISMATCH_AMOUNT",
                        "currency": "MISMATCH_CURRENCY",
                        "mock_transaction_id": "MISMATCH_TRANSACTION",
                    }[difference],
                    "sqlstate": "",
                    "sqlstate_source": "none",
                    "fixture_copy": False,
                    "platform_table_read": False,
                    "cleanup": {
                        "kind": "UDS_SCENARIO",
                        "removed": True,
                        "verified": True,
                    },
                }
            ]
            scenario["facts"] = {
                "before": self.us3_fact("UNKNOWN"),
                "after": self.us3_fact("UNKNOWN"),
                "transition": "UNCHANGED",
                "stable_facts": True,
            }
            scenario["checks"]["counts_after"] = self.us3_counts(1, 1, 1, 1, 0)
        elif name == "query_not_found":
            scenario["http"] = [
                self.us3_http(status=404, result="")
            ]
            scenario["http"][0]["payment_id"] = ""
            scenario["pg"] = [
                {"state": "ORDER", "rows": 0},
                {"state": "MOCK", "rows": 0},
                {"state": "TRACE", "rows": 0},
            ]
            scenario["calls"] = [
                {
                    "generation": 1,
                    "exact": True,
                    "payment_calls": 0,
                    "query_calls": 0,
                }
            ]
            scenario["records"] = []
            scenario["queries"] = []
            scenario["facts"] = {"transition": "NONE", "stable_facts": True}
            scenario["checks"] = {
                "counts_before": self.us3_counts(0, 0, 0, 0, 0),
                "counts_after": self.us3_counts(0, 0, 0, 0, 0),
                "counts_exact": True,
                "stable_facts": True,
            }
            scenario["generation_proofs"] = [
                self.us3_complete_proof(1, last_seq=0)
            ]
        elif name == "query_confirmed_plain":
            scenario["queries"] = []
            scenario["records"] = []
            scenario["pg"] = [
                {"state": "ORDER", "rows": 1},
                {"state": "MOCK", "rows": 1},
                {"state": "TRACE", "rows": 0},
            ]
            scenario["calls"] = [
                {
                    "generation": 1,
                    "exact": True,
                    "payment_calls": 0,
                    "query_calls": 0,
                }
            ]
            scenario["facts"] = {
                "before": self.us3_fact("SUCCEEDED"),
                "after": self.us3_fact("SUCCEEDED"),
                "transition": "UNCHANGED",
                "stable_facts": True,
            }
            scenario["checks"]["counts_before"] = self.us3_counts(0, 0, 1, 1, 0)
            scenario["checks"]["counts_after"] = self.us3_counts(0, 0, 1, 1, 0)
            scenario["checks"]["stable_facts"] = True
            scenario["generation_proofs"] = [
                self.us3_complete_proof(1, last_seq=0)
            ]
        elif name == "query_confirmed_trace_failure":
            scenario["queries"] = [
                self.us3_query(
                    path="BY_ID",
                    directive="failure",
                    outcome="unavailable",
                    decision="NOT_RECORDED",
                    before="",
                    after="",
                    trace_outcome="FAILED",
                    request_id=self.us3_request_id_n(1),
                ),
                self.us3_query(
                    call_seq=2, request_id=self.us3_request_id_n(2)
                ),
            ]
            scenario["records"] = [
                self.us3_record(request_id=self.us3_request_id_n(2))
            ]
            scenario["http"] = [
                self.us3_http(
                    status=200,
                    result="SUCCEEDED",
                    issues=["MOCK_RESULT_UNCONFIRMED", "TRACE_SAVE_ERROR"],
                    request_id=self.us3_request_id_n(1),
                ),
                self.us3_http(request_id=self.us3_request_id_n(2)),
            ]
            scenario["faults"] = [
                {
                    "kind": "TRACE_INSERT_SQL",
                    "target_id": "order-1",
                    "sqlstate": "P0001",
                    "sqlstate_source": "fixture_probe",
                    "fixture_copy": True,
                    "platform_table_read": False,
                    "cleanup": {
                        "kind": "TRIGGER_FUNCTION",
                        "removed": True,
                        "verified": True,
                    },
                }
            ]
            scenario["facts"] = {
                "before": self.us3_fact("UNKNOWN"),
                "after": self.us3_fact("SUCCEEDED"),
                "transition": "UNKNOWN_TO_CONFIRMED",
                "stable_facts": True,
            }
            scenario["checks"]["counts_after"] = self.us3_counts(1, 2, 1, 1, 1)
            scenario["trace"] = {
                "outcome": "FAILED",
                "issue": "TRACE_SAVE_ERROR",
                "sqlstate": "P0001",
                "sqlstate_source": "fixture_probe",
                "confirmed_fact_returned": True,
                "new_lookup": False,
                "proof_source": "APPLICATION_OBSERVED",
            }
        elif name == "query_result_commit_unknown":
            scenario["queries"] = [self.us3_query()]
            scenario["records"] = [self.us3_record()]
            scenario["http"][0]["issues"] = ["RESULT_SAVE_ERROR"]
            scenario["faults"] = [
                {
                    "kind": "RESULT_COMMIT_REPLY_HIDDEN",
                    "sqlstate": "",
                    "sqlstate_source": "none",
                    "fixture_copy": False,
                    "platform_table_read": False,
                    "cleanup": {
                        "kind": "UDS_SCENARIO",
                        "removed": True,
                        "verified": True,
                    },
                }
            ]
            scenario["facts"] = {
                "before": self.us3_fact("UNKNOWN"),
                "after": self.us3_fact("SUCCEEDED"),
                "transition": "UNKNOWN_TO_CONFIRMED",
                "stable_facts": False,
            }
            scenario["checks"]["counts_before"] = self.us3_counts(0, 0, 1, 1, 0)
            scenario["checks"]["counts_after"] = self.us3_counts(1, 1, 1, 1, 0)
            scenario["checks"]["stable_facts"] = False
            scenario["save"] = {
                "attempts": 1,
                "record_id": self.us3_record_id,
                "commit_state": "INDETERMINATE",
                "application_readback": "ORDER_ONLY",
                "external_pg_readback": "ORDER_AND_RECORD",
                "readback_verified": True,
                "readback_scope": "INDEPENDENT_ORDER_AND_RECORD",
                "readback_source": "APP_AND_EXTERNAL",
                "created_claimed": False,
                "rollback_claimed": False,
                "candidate_id_leaked": False,
            }
        elif name == "protection_inflight_failure":
            scenario["queries"] = [
                self.us3_query(
                    decision="KEEP_CONFIRMED",
                    before="SUCCEEDED",
                    after="SUCCEEDED",
                    request_id=self.us3_request_id_n(1),
                ),
                self.us3_query(
                    call_seq=2, request_id=self.us3_request_id_n(2)
                ),
            ]
            scenario["records"] = [
                self.us3_record(
                    decision="KEEP_CONFIRMED",
                    before="SUCCEEDED",
                    after="SUCCEEDED",
                    reason="CONFIRMED_RESULT_PRESERVED",
                    request_id=self.us3_request_id_n(1),
                ),
                self.us3_record(request_id=self.us3_request_id_n(2)),
            ]
            scenario["http"] = [
                self.us3_http(
                    issues=["MOCK_RESULT_UNCONFIRMED"],
                    request_id=self.us3_request_id_n(1),
                ),
                self.us3_http(request_id=self.us3_request_id_n(2)),
            ]
            scenario["faults"] = [
                {
                    "kind": "MOCK_QUERY_FAILURE",
                    "sqlstate": "08006",
                    "sqlstate_source": "application_observed",
                    "fixture_copy": False,
                    "platform_table_read": False,
                    "cleanup": {
                        "kind": "UDS_SCENARIO",
                        "removed": True,
                        "verified": True,
                    },
                }
            ]
            scenario["facts"] = {
                "before": self.us3_fact("SUCCEEDED"),
                "after": self.us3_fact("SUCCEEDED"),
                "transition": "UNCHANGED",
                "stable_facts": True,
            }
            scenario["checks"]["counts_after"] = self.us3_counts(1, 2, 1, 1, 1)
            scenario["protection"] = {
                "kind": "INFLIGHT_FAILURE",
                "initial_result": "UNKNOWN",
                "confirmed_result": "SUCCEEDED",
                "protected_result": "SUCCEEDED",
                "first_applied": "KEEP_CONFIRMED",
                "second_applied": "ACCEPT_CONFIRMATION",
                "post_confirmation_mock_calls": 0,
                "original_fact_preserved": True,
            }
            scenario["trace"] = {
                "outcome": "SAVED",
                "issue": "",
                "sqlstate": "00000",
                "sqlstate_source": "application_observed",
                "confirmed_fact_returned": True,
                "new_lookup": False,
                "proof_source": "OBSERVER",
            }
        elif name == "protection_inflight_mismatch":
            scenario["queries"] = [
                self.us3_query(
                    directive="mismatch_reference",
                    outcome="mismatch_applied",
                    decision="REJECT_EVIDENCE",
                    before="SUCCEEDED",
                    after="SUCCEEDED",
                    trace_outcome="SAVED",
                    request_id=self.us3_request_id_n(1),
                ),
                self.us3_query(
                    call_seq=2, request_id=self.us3_request_id_n(2)
                ),
            ]
            scenario["records"] = [
                self.us3_record(
                    decision="REJECT_EVIDENCE",
                    before="SUCCEEDED",
                    after="SUCCEEDED",
                    reason="MOCK_EVIDENCE_MISMATCH",
                    differences=["platform_reference_id"],
                    request_id=self.us3_request_id_n(1),
                ),
                self.us3_record(request_id=self.us3_request_id_n(2)),
            ]
            scenario["http"] = [
                self.us3_http(
                    issues=["MOCK_EVIDENCE_REJECTED"],
                    request_id=self.us3_request_id_n(1),
                ),
                self.us3_http(request_id=self.us3_request_id_n(2)),
            ]
            scenario["faults"] = [
                {
                    "kind": "MISMATCH_REFERENCE",
                    "sqlstate": "",
                    "sqlstate_source": "none",
                    "fixture_copy": False,
                    "platform_table_read": False,
                    "cleanup": {
                        "kind": "UDS_SCENARIO",
                        "removed": True,
                        "verified": True,
                    },
                }
            ]
            scenario["facts"] = {
                "before": self.us3_fact("SUCCEEDED"),
                "after": self.us3_fact("SUCCEEDED"),
                "transition": "UNCHANGED",
                "stable_facts": True,
            }
            scenario["checks"]["counts_after"] = self.us3_counts(1, 2, 1, 1, 1)
            scenario["protection"] = {
                "kind": "INFLIGHT_MISMATCH",
                "initial_result": "UNKNOWN",
                "confirmed_result": "SUCCEEDED",
                "protected_result": "SUCCEEDED",
                "first_applied": "REJECT_EVIDENCE",
                "second_applied": "ACCEPT_CONFIRMATION",
                "post_confirmation_mock_calls": 0,
                "original_fact_preserved": True,
            }
            scenario["trace"] = {
                "outcome": "SAVED",
                "issue": "",
                "sqlstate": "00000",
                "sqlstate_source": "application_observed",
                "confirmed_fact_returned": True,
                "new_lookup": False,
                "proof_source": "OBSERVER",
            }
        elif name in {
            "restart_registered_unpaid",
            "restart_mock_committed_reply_lost",
        }:
            point = (
                "REGISTERED"
                if name == "restart_registered_unpaid"
                else "MOCK_COMMITTED"
            )
            controlled = self.us3_controlled(point)
            second_proof = self.us3_complete_proof(2)
            second_proof["counts"] = self.us3_counts(0, 1, 0, 0, 0)
            replay_id = self.us3_request_id_n(1)
            get_id = self.us3_request_id_n(2)
            mock_rows = 0 if point == "REGISTERED" else 1
            get_result = "UNKNOWN" if point == "REGISTERED" else "SUCCEEDED"
            get_issues = (
                ["MOCK_RESULT_NOT_FOUND"] if point == "REGISTERED" else []
            )
            scenario["generations"] = [1, 2]
            scenario["http"] = [
                self.us3_http(
                    generation=2,
                    status=200,
                    result="UNKNOWN",
                    request_id=replay_id,
                ),
                self.us3_http(
                    generation=2,
                    status=200,
                    result=get_result,
                    request_id=get_id,
                    issues=get_issues,
                ),
            ]
            scenario["pg"] = [
                {"state": "ORDER", "rows": 1},
                {"state": "MOCK", "rows": mock_rows},
                {"state": "TRACE", "rows": 1},
            ]
            scenario["calls"] = [
                {
                    "generation": 1,
                    "exact": False,
                    "payment_calls": 0,
                    "query_calls": 0,
                },
                {
                    "generation": 2,
                    "exact": True,
                    "payment_calls": 0,
                    "query_calls": 1,
                },
            ]
            scenario["facts"] = {
                "before": self.us3_fact("UNKNOWN"),
                "after": self.us3_fact(get_result),
                "transition": (
                    "UNCHANGED"
                    if point == "REGISTERED"
                    else "UNKNOWN_TO_CONFIRMED"
                ),
                "stable_facts": point == "REGISTERED",
            }
            scenario["checks"]["counts_before"] = self.us3_counts(0, 0, 0, 0, 0)
            scenario["checks"]["counts_after"] = self.us3_counts(
                0, 1, 1, mock_rows, 1
            )
            scenario["checks"]["counts_exact"] = False
            scenario["checks"]["stable_facts"] = point == "REGISTERED"
            scenario["queries"] = [
                self.us3_query(
                    generation=2,
                    call_seq=1,
                    request_id=get_id,
                    decision=(
                        "ACCEPT_CONFIRMATION"
                        if point == "MOCK_COMMITTED"
                        else "KEEP_UNKNOWN"
                    ),
                    before="UNKNOWN",
                    after=get_result,
                    trace_outcome="SAVED",
                    confirmed_returned=point == "MOCK_COMMITTED",
                )
            ]
            scenario["records"] = [
                self.us3_record(
                    request_id=get_id,
                    decision=(
                        "ACCEPT_CONFIRMATION"
                        if point == "MOCK_COMMITTED"
                        else "KEEP_UNKNOWN"
                    ),
                    before="UNKNOWN",
                    after=get_result,
                    reason=(
                        ""
                        if point == "MOCK_COMMITTED"
                        else "MOCK_RESULT_NOT_FOUND"
                    ),
                )
            ]
            scenario["generation_proofs"] = [
                {
                    "generation": 1,
                    "mode": "controlled_truncated",
                    "sealed": False,
                    "trusted": False,
                    "exact_counts": False,
                    "controlled_termination": controlled,
                },
                second_proof,
            ]
            scenario["faults"] = [
                {
                    "kind": "PROCESS_TERMINATED",
                    "sqlstate": "",
                    "sqlstate_source": "none",
                    "fixture_copy": False,
                    "platform_table_read": False,
                    "cleanup": {
                        "kind": "PROCESS_TERMINAL",
                        "removed": True,
                        "verified": True,
                    },
                }
            ]
            scenario["joint"] = {
                "kind": "CONTROLLED_TRUNCATED_RESTART",
                "first_generation": 1,
                "second_generation": 2,
                "first_mode": "controlled_truncated",
                "second_mode": "complete",
                "first_exact_counts": False,
                "second_exact_counts": True,
                "second_receipt": second_proof["receipt"],
                "held_point": point,
                "held_binding": controlled["binding"],
                "process_exit": controlled["exit"],
                "observed_prefix": controlled["observed_prefix"],
                "no_extra_payment_effect": True,
                "no_new_payment_call_after_restart": True,
                "original_intent_same": True,
                "limitation": "CROSS_GENERATION_EXACT_COUNTS_NOT_PROVABLE",
            }
        elif name in {
            "restart_unknown_no_confirm",
            "restart_unknown_then_confirmed",
        }:
            confirmed = name == "restart_unknown_then_confirmed"
            scenario["generations"] = [1, 2]
            scenario["http"] = [
                self.us3_http(
                    status=200,
                    result="SUCCEEDED" if confirmed else "UNKNOWN",
                )
            ]
            scenario["pg"] = [
                {"state": "ORDER", "rows": 1},
                {"state": "MOCK", "rows": 1 if confirmed else 0},
                {"state": "TRACE", "rows": 1 if confirmed else 0},
            ]
            scenario["calls"] = [
                {
                    "generation": 1,
                    "exact": True,
                    "payment_calls": 1,
                    "query_calls": 1,
                },
                {
                    "generation": 2,
                    "exact": True,
                    "payment_calls": 1,
                    "query_calls": 1,
                },
            ]
            scenario["generation_proofs"] = [
                self.us3_complete_proof(1),
                self.us3_complete_proof(2),
            ]
            scenario["queries"] = [
                self.us3_query(
                    decision=(
                        "ACCEPT_CONFIRMATION" if confirmed else "KEEP_UNKNOWN"
                    ),
                    before="UNKNOWN",
                    after="SUCCEEDED" if confirmed else "UNKNOWN",
                    trace_outcome="SAVED" if confirmed else "NOT_ATTEMPTED",
                )
            ]
            scenario["records"] = [
                self.us3_record(
                    decision=(
                        "ACCEPT_CONFIRMATION" if confirmed else "KEEP_UNKNOWN"
                    ),
                    before="UNKNOWN",
                    after="SUCCEEDED" if confirmed else "UNKNOWN",
                    reason="" if confirmed else "MOCK_RESULT_NOT_FOUND",
                )
            ]
            scenario["facts"] = {
                "before": self.us3_fact("UNKNOWN"),
                "after": self.us3_fact(
                    "SUCCEEDED" if confirmed else "UNKNOWN"
                ),
                "transition": (
                    "UNKNOWN_TO_CONFIRMED" if confirmed else "UNCHANGED"
                ),
                "stable_facts": True,
            }
            scenario["checks"]["counts_after"] = self.us3_counts(
                1, 1, 1, 1 if confirmed else 0, 1 if confirmed else 0
            )
        elif name == "lifecycle_cancel_before_mock":
            scenario["transport"] = "CLIENT_CANCELED"
            scenario["http"] = []
            scenario["pg"] = [
                {"state": "ORDER", "rows": 0},
                {"state": "MOCK", "rows": 0},
                {"state": "TRACE", "rows": 0},
            ]
            scenario["calls"] = [
                {
                    "generation": 1,
                    "exact": True,
                    "payment_calls": 0,
                    "query_calls": 0,
                }
            ]
            scenario["facts"] = {"transition": "NONE", "stable_facts": True}
            scenario["checks"] = {
                "counts_before": self.us3_counts(0, 0, 0, 0, 0),
                "counts_after": self.us3_counts(0, 0, 0, 0, 0),
                "counts_exact": True,
                "stable_facts": True,
            }
            scenario["generation_proofs"] = [
                self.us3_complete_proof(1, last_seq=0)
            ]
            scenario["faults"] = [
                {
                    "kind": "REGISTRATION_INSERT_HOLD",
                    "sqlstate": "",
                    "sqlstate_source": "",
                    "fixture_copy": False,
                    "platform_table_read": False,
                    "cleanup": {
                        "kind": "TABLE_SHARE_LOCK",
                        "removed": True,
                        "verified": True,
                    },
                }
            ]
            scenario["cancel"] = {
                "phase": "BEFORE_MOCK",
                "post_cancel_payment_calls": 0,
                "post_cancel_query_calls": 0,
                "finish_checked_mock": False,
                "rollback_claimed": False,
            }
        elif name == "lifecycle_cancel_after_mock":
            scenario["transport"] = "CLIENT_CANCELED"
            scenario["http"] = []
            scenario["pg"] = [
                {"state": "ORDER", "rows": 1},
                {"state": "MOCK", "rows": 1},
                {"state": "TRACE", "rows": 0},
            ]
            scenario["calls"] = [
                {
                    "generation": 1,
                    "exact": True,
                    "payment_calls": 1,
                    "query_calls": 0,
                }
            ]
            scenario["facts"] = {
                "before": self.us3_fact("UNKNOWN"),
                "after": self.us3_fact("UNKNOWN"),
                "transition": "NONE",
                "stable_facts": True,
            }
            scenario["checks"] = {
                "counts_before": self.us3_counts(0, 0, 0, 0, 0),
                "counts_after": self.us3_counts(1, 0, 1, 1, 0),
                "counts_exact": True,
                "stable_facts": True,
            }
            scenario["faults"] = [
                {
                    "kind": "RESULT_HANDLING_INSERT_HOLD",
                    "sqlstate": "",
                    "sqlstate_source": "",
                    "fixture_copy": False,
                    "platform_table_read": False,
                    "cleanup": {
                        "kind": "TABLE_SHARE_LOCK",
                        "removed": True,
                        "verified": True,
                    },
                }
            ]
            scenario["cancel"] = {
                "phase": "AFTER_MOCK",
                "post_cancel_payment_calls": 0,
                "post_cancel_query_calls": 0,
                "finish_checked_mock": False,
                "rollback_claimed": False,
            }
        elif name == "lifecycle_exit_budget":
            scenario["transport"] = "CLIENT_CANCELED"
            scenario["http"] = [
                self.us3_http(
                    kind="SHUTDOWN_PROBE",
                    status=503,
                    result="",
                )
            ]
            scenario["http"][0]["request_id"] = ""
            scenario["http"][0]["payment_id"] = ""
            scenario["pg"] = [
                {"state": "ORDER", "rows": 0},
                {"state": "MOCK", "rows": 0},
                {"state": "TRACE", "rows": 0},
            ]
            scenario["calls"] = [
                {
                    "generation": 1,
                    "exact": True,
                    "payment_calls": 0,
                    "query_calls": 0,
                }
            ]
            scenario["facts"] = {"transition": "NONE", "stable_facts": True}
            scenario["checks"] = {
                "counts_before": self.us3_counts(0, 0, 0, 0, 0),
                "counts_after": self.us3_counts(0, 0, 0, 0, 0),
                "counts_exact": True,
                "stable_facts": True,
            }
            scenario["generation_proofs"] = [
                self.us3_complete_proof(1, last_seq=0)
            ]
            scenario["faults"] = [
                {
                    "kind": "REGISTRATION_INSERT_HOLD",
                    "sqlstate": "",
                    "sqlstate_source": "",
                    "fixture_copy": False,
                    "platform_table_read": False,
                    "cleanup": {
                        "kind": "TABLE_SHARE_LOCK",
                        "removed": True,
                        "verified": True,
                    },
                }
            ]
            scenario["budgets"] = [
                {
                    "class": "NORMAL",
                    "kind": "EXIT",
                    "source": "APP",
                    "pass_budget": True,
                    "limit_ms": 20000,
                    "elapsed_ms": 10000,
                },
                {
                    "class": "NORMAL",
                    "kind": "PROCESS_GRACE",
                    "source": "HARNESS",
                    "pass_budget": False,
                    "limit_ms": 25000,
                    "elapsed_ms": 0,
                },
            ]
            scenario["shutdown"] = {
                "post_shutdown_payment_calls": 0,
                "post_shutdown_query_calls": 0,
                "rollback_claimed": False,
                "no_unmanaged_goroutine": True,
                "no_unmanaged_goroutine_source": "APP_UNIT_PLUS_GROUP_GONE",
                "pool_closed_after_users": True,
                "pool_closed_after_users_source": "APP_UNIT_EVIDENCE",
                "exit_elapsed_ms": 10000,
                "exit_upper_bound_ms": 20000,
                "observation_tolerance_ms": 250,
                "host_grace_ms": 25000,
                "host_grace_pass_budget": False,
            }
        return scenario

    def us3_report(self):
        return {
            "schema": "payment-hub.us3.v1",
            "run_id": self.run_id,
            "scenarios": [
                self.us3_scenario(name) for name in MOD.US3_SCENARIO_NAMES
            ],
        }

    def write_us3(self, value, run_id=None):
        run_id = value.get("run_id", self.run_id) if run_id is None else run_id
        path = self.db_root / run_id / "us3.json"
        self.write(path, value)
        return path

    def export_db_us3(self):
        return self.export(["db", "us3"])

    def read_us3_safe(self):
        path = self.report_dir / "upload" / "us3.safe.json"
        self.assertTrue(path.is_file(), "us3 safe report is missing")
        return json.loads(path.read_text())

    def test_export_us3_complete_pass_rebuilds_safe_report(self):
        self.write_db(self.db_report())
        self.write_us3(self.us3_report())
        code, _ = self.export_db_us3()
        self.assertEqual(code, 0)
        safe = self.read_us3_safe()
        self.assertEqual(safe["schema"], "payment-hub.ci-safe.us3.v1")
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe.get("result"), "pass")
        self.assertEqual(len(safe["scenarios"]), 24)
        self.assertEqual(
            [item["name"] for item in safe["scenarios"]],
            list(MOD.US3_SCENARIO_NAMES),
        )
        text = json.dumps(safe)
        for forbidden in ("SECRET", "Authorization", "postgres://", "client_id", "business_order_no"):
            self.assertNotIn(forbidden, text)
        provenance = json.loads(
            (self.report_dir / "upload" / "provenance.json").read_text()
        )
        record = next(
            entry for entry in provenance["reports"] if entry["name"] == "us3"
        )
        self.assertEqual(record["source"], self.run_id + "/us3.json")
        self.assertEqual(record["export"], "us3.safe.json")
        self.assertRegex(record["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(record["export_sha256"], r"^[0-9a-f]{64}$")

    def test_export_us3_missing_file_is_missing_nonzero(self):
        self.write_db(self.db_report())
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe["status"], "missing")

    def test_export_us3_missing_scenario_is_non_green(self):
        value = self.us3_report()
        value["scenarios"].pop()
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-missing-scenario", safe["problems"])

    def test_export_us3_requires_each_required_scenario(self):
        for index, name in enumerate(MOD.US3_SCENARIO_NAMES):
            with self.subTest(name=name):
                value = self.us3_report()
                value["scenarios"].pop(index)
                self.write_db(self.db_report())
                self.write_us3(value)
                code, _ = self.export_db_us3()
                self.assertEqual(code, 1)
                safe = self.read_us3_safe()
                self.assertEqual(safe.get("result"), "fail")
                self.assertIn("us3-missing-scenario", safe["problems"])
                self.assertIn(name, safe["missing_scenarios"])

    def test_export_us3_wrong_run_id_rejected(self):
        value = self.us3_report()
        value["run_id"] = "f" * 32
        self.write_db(self.db_report())
        self.write_us3(value, run_id=self.run_id)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        self.assertEqual(self.read_us3_safe()["status"], "error")
        self.assertEqual(
            self.read_us3_safe()["error"], "us3-run-id-mismatch"
        )

    def test_export_us3_db_not_green_non_green(self):
        self.write_db(self.db_report(result="fail"))
        self.write_us3(self.us3_report())
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-db-not-green", safe["problems"])

    def test_export_us3_unknown_fields_rejected(self):
        for mutation in ("top", "scenario", "record"):
            with self.subTest(mutation=mutation):
                value = self.us3_report()
                if mutation == "top":
                    value["raw_log"] = "SECRET"
                elif mutation == "scenario":
                    value["scenarios"][0]["reason_message"] = "SECRET"
                else:
                    value["scenarios"][2]["records"][0]["reason_message"] = "SECRET"
                self.write_db(self.db_report())
                self.write_us3(value)
                code, _ = self.export_db_us3()
                self.assertEqual(code, 1)
                safe = self.read_us3_safe()
                self.assertEqual(safe["status"], "error")
                self.assertIn("unsafe-field", safe["error"])
                self.assertNotIn("SECRET", json.dumps(safe))

    def test_export_us3_unsafe_identity_rejected(self):
        value = self.us3_report()
        value["scenarios"][2]["facts"]["before"]["client_id"] = "raw-client"
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe["status"], "error")

    def test_export_us3_fake_partial_rejected_outside_windows(self):
        value = self.us3_report()
        scenario = value["scenarios"][2]
        controlled = self.us3_controlled()
        scenario["generation_proofs"][0] = {
            "generation": 1,
            "mode": "controlled_truncated",
            "sealed": False,
            "trusted": False,
            "exact_counts": False,
            "controlled_termination": controlled,
        }
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn(
            "us3-controlled-proof-outside-window", safe["problems"]
        )

    def test_export_us3_missing_query_applied_proof_non_green(self):
        value = self.us3_report()
        value["scenarios"][2]["queries"] = []
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-missing-query-applied", safe["problems"])

    def test_export_us3_mismatch_scenarios_are_independent(self):
        for index, name in enumerate(
            (
                "query_mismatch_reference",
                "query_mismatch_amount",
                "query_mismatch_currency",
                "query_mismatch_transaction",
            )
        ):
            with self.subTest(name=name):
                value = self.us3_report()
                value["scenarios"].pop(
                    MOD.US3_SCENARIO_NAMES.index(name)
                )
                self.write_db(self.db_report())
                self.write_us3(value)
                code, _ = self.export_db_us3()
                self.assertEqual(code, 1)
                safe = self.read_us3_safe()
                self.assertEqual(safe.get("result"), "fail")
                self.assertIn(name, safe["missing_scenarios"])

    def test_export_us3_over_limit_http_rejected(self):
        value = self.us3_report()
        entry = dict(value["scenarios"][0]["http"][0])
        value["scenarios"][0]["http"] = [entry] * 17
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        self.assertEqual(self.read_us3_safe()["status"], "error")

    def test_export_us3_missing_fault_cleanup_rejected(self):
        value = self.us3_report()
        value["scenarios"][3]["faults"][0]["cleanup"]["verified"] = False
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        self.assertEqual(self.read_us3_safe()["status"], "error")

    def test_export_us3_wrong_lifecycle_combinations(self):
        cases = []
        value = self.us3_report()
        value["scenarios"][22]["http"] = [
            self.us3_http(kind="SHUTDOWN_PROBE", status=503, result="")
        ]
        cases.append(("before-cancel-probe", value))
        value = self.us3_report()
        value["scenarios"][22]["http"] = [
            self.us3_http(status=503, result="UNKNOWN")
        ]
        cases.append(("cancel-primary-503", value))
        value = self.us3_report()
        value["scenarios"][23]["http"] = [
            self.us3_http(kind="SHUTDOWN_PROBE", status=200, result="")
        ]
        cases.append(("probe-200", value))
        value = self.us3_report()
        value["scenarios"][0]["http"] = [
            self.us3_http(kind="SHUTDOWN_PROBE", status=503, result="")
        ]
        cases.append(("response-only-probe", value))
        value = self.us3_report()
        value["scenarios"][23]["checks"]["counts_after"] = self.us3_counts(
            0, 0, 1, 1, 1
        )
        cases.append(("exit-after-rows", value))
        for name, value in cases:
            with self.subTest(case=name):
                self.write_db(self.db_report())
                self.write_us3(value)
                code, _ = self.export_db_us3()
                self.assertEqual(code, 1)
                safe = self.read_us3_safe()
                self.assertNotEqual(safe.get("result"), "pass")

    def test_export_us3_exit_primary_cancel_with_probe_is_valid(self):
        value = self.us3_report()
        value["scenarios"][23]["transport"] = "CLIENT_CANCELED"
        value["scenarios"][23]["http"] = [
            self.us3_http(kind="SHUTDOWN_PROBE", status=503, result="")
        ]
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 0)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "pass")

    def test_export_us3_exit_primary_no_response_without_probe_is_valid(self):
        index = MOD.US3_SCENARIO_NAMES.index("lifecycle_exit_budget")
        value = self.us3_report()
        value["scenarios"][index]["transport"] = "NO_RESPONSE"
        value["scenarios"][index]["http"] = []
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 0)
        self.assertEqual(self.read_us3_safe().get("result"), "pass")

    def test_export_us3_exit_without_probe_still_requires_shutdown_proof(self):
        index = MOD.US3_SCENARIO_NAMES.index("lifecycle_exit_budget")
        value = self.us3_report()
        value["scenarios"][index]["transport"] = "NO_RESPONSE"
        value["scenarios"][index]["http"] = []
        del value["scenarios"][index]["shutdown"]
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-lifecycle-shutdown-missing", safe["problems"])

    def test_export_us3_multiple_exit_probes_rejected(self):
        index = MOD.US3_SCENARIO_NAMES.index("lifecycle_exit_budget")
        value = self.us3_report()
        value["scenarios"][index]["http"] = [
            self.us3_http(kind="SHUTDOWN_PROBE", status=503, result=""),
            self.us3_http(kind="SHUTDOWN_PROBE", status=503, result=""),
        ]
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-shutdown-probe-count", safe["problems"])

    def test_export_us3_exit_requires_unmanaged_goroutine_source(self):
        index = MOD.US3_SCENARIO_NAMES.index("lifecycle_exit_budget")
        for mutation in ("missing", "wrong"):
            with self.subTest(mutation=mutation):
                value = self.us3_report()
                shutdown = value["scenarios"][index]["shutdown"]
                if mutation == "missing":
                    del shutdown["no_unmanaged_goroutine_source"]
                else:
                    shutdown["no_unmanaged_goroutine_source"] = "OBSERVED"
                self.write_db(self.db_report())
                self.write_us3(value)
                code, _ = self.export_db_us3()
                self.assertEqual(code, 1)
                safe = self.read_us3_safe()
                self.assertEqual(safe["status"], "error")
                expected = (
                    "unsafe-field-us3.shutdown"
                    if mutation == "missing"
                    else "unsafe-field-us3.shutdown.no_unmanaged_goroutine_source"
                )
                self.assertEqual(safe["error"], expected)

    def test_export_us3_exit_unmanaged_goroutine_requires_group_proof(self):
        index = MOD.US3_SCENARIO_NAMES.index("lifecycle_exit_budget")
        value = self.us3_report()
        value["scenarios"][index]["commands"] = [
            command
            for command in value["scenarios"][index]["commands"]
            if command["kind"] != "process_stop"
        ]
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-lifecycle-goroutine-proof", safe["problems"])

    def test_export_us3_symlink_source_rejected(self):
        self.write_db(self.db_report())
        target = self.root / "outside-us3.json"
        target.write_text(json.dumps(self.us3_report()), encoding="utf-8")
        link = self.db_root / self.run_id / "us3.json"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe["status"], "error")
        self.assertEqual(safe["error"], "unsafe-source-path")

    def test_export_us3_no_mock_match_and_not_visible_are_distinct(self):
        cases = []
        value = self.us3_report()
        value["scenarios"][
            MOD.US3_SCENARIO_NAMES.index("query_unknown_no_mock_match")
        ]["pg"] = [
            {"state": "ORDER", "rows": 1},
            {"state": "MOCK", "rows": 1},
            {"state": "TRACE", "rows": 0},
        ]
        cases.append(("no-mock-match-with-mock", value))
        value = self.us3_report()
        value["scenarios"][
            MOD.US3_SCENARIO_NAMES.index("query_unknown_not_visible")
        ]["pg"] = [
            {"state": "ORDER", "rows": 1},
            {"state": "MOCK", "rows": 0},
            {"state": "TRACE", "rows": 1},
        ]
        cases.append(("not-visible-without-mock", value))
        for name, value in cases:
            with self.subTest(case=name):
                self.write_db(self.db_report())
                self.write_us3(value)
                code, _ = self.export_db_us3()
                self.assertEqual(code, 1)
                safe = self.read_us3_safe()
                self.assertEqual(safe.get("result"), "fail")

    def test_export_us3_trace_failure_requires_explicit_sqlstate_source(self):
        value = self.us3_report()
        scenario = value["scenarios"][
            MOD.US3_SCENARIO_NAMES.index("query_confirmed_trace_failure")
        ]
        scenario["trace"]["sqlstate_source"] = "application_observed"
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-trace-failure-sqlstate", safe["problems"])

    def test_export_us3_save_readback_scope_and_source_pair(self):
        index = MOD.US3_SCENARIO_NAMES.index("query_result_commit_unknown")
        value = self.us3_report()
        value["scenarios"][index]["save"].update(
            {
                "readback_scope": "INDEPENDENT_ORDER_AND_RECORD",
                "readback_source": "APP_AND_EXTERNAL",
            }
        )
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 0)
        self.assertEqual(self.read_us3_safe().get("result"), "pass")

        value["scenarios"][index]["save"]["readback_source"] = "APP"
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-save-readback-source", safe["problems"])

    def test_export_us3_scenario_limit_rejected(self):
        value = self.us3_report()
        value["scenarios"].extend(
            [value["scenarios"][0]] * (MOD.US3_SCENARIO_MAX + 1)
        )
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        self.assertEqual(self.read_us3_safe()["status"], "error")

    def test_export_us3_lifecycle_after_mock_confirmed_shape_is_valid(self):
        index = MOD.US3_SCENARIO_NAMES.index("lifecycle_cancel_after_mock")
        value = self.us3_report()
        scenario = value["scenarios"][index]
        scenario["pg"] = [
            {"state": "ORDER", "rows": 1},
            {"state": "MOCK", "rows": 1},
            {"state": "TRACE", "rows": 1},
        ]
        scenario["checks"]["counts_after"] = self.us3_counts(1, 0, 1, 1, 1)
        scenario["facts"] = {
            "before": self.us3_fact("UNKNOWN"),
            "after": self.us3_fact("SUCCEEDED"),
            "transition": "UNKNOWN_TO_CONFIRMED",
            "stable_facts": True,
        }
        scenario["records"] = [
            self.us3_record(
                decision="ACCEPT_CONFIRMATION",
                before="UNKNOWN",
                after="SUCCEEDED",
                source="PAYMENT",
            )
        ]
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 0)
        self.assertEqual(self.read_us3_safe().get("result"), "pass")

    def test_export_us3_producer_query_not_found_shape_rejects_fake_query(self):
        index = MOD.US3_SCENARIO_NAMES.index("query_not_found")
        value = self.us3_report()
        value["scenarios"][index]["queries"] = [
            self.us3_query(
                outcome="not_found",
                decision="KEEP_UNKNOWN",
                before="UNKNOWN",
                after="UNKNOWN",
                trace_outcome="NOT_ATTEMPTED",
                applied=False,
            )
        ]
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-query-not-found-queries", safe["problems"])

    def test_export_us3_producer_confirmed_plain_shape_rejects_new_work(self):
        index = MOD.US3_SCENARIO_NAMES.index("query_confirmed_plain")
        cases = ("query", "trace", "save")
        for case in cases:
            with self.subTest(case=case):
                value = self.us3_report()
                scenario = value["scenarios"][index]
                if case == "query":
                    scenario["queries"] = [self.us3_query(applied=False)]
                elif case == "trace":
                    scenario["trace"] = {
                        "outcome": "SAVED",
                        "issue": "",
                        "sqlstate": "00000",
                        "sqlstate_source": "application_observed",
                        "confirmed_fact_returned": True,
                        "new_lookup": False,
                        "proof_source": "OBSERVER",
                    }
                else:
                    scenario["save"] = {
                        "attempts": 1,
                        "record_id": self.us3_record_id,
                        "commit_state": "INDETERMINATE",
                        "application_readback": "ORDER_ONLY",
                        "external_pg_readback": "NONE",
                        "readback_verified": True,
                        "readback_scope": "APPLICATION_ORDER_ONLY",
                        "readback_source": "APP",
                        "created_claimed": False,
                        "rollback_claimed": False,
                        "candidate_id_leaked": False,
                    }
                self.write_db(self.db_report())
                self.write_us3(value)
                code, _ = self.export_db_us3()
                self.assertEqual(code, 1)
                safe = self.read_us3_safe()
                self.assertEqual(safe.get("result"), "fail")

    def test_export_us3_producer_result_commit_unknown_shape_accepts_no_trace(self):
        index = MOD.US3_SCENARIO_NAMES.index("query_result_commit_unknown")
        value = self.us3_report()
        scenario = value["scenarios"][index]
        self.assertNotIn("trace", scenario)
        self.assertEqual(scenario["save"]["record_id"], self.us3_record_id)
        self.assertEqual(
            scenario["save"]["readback_scope"],
            "INDEPENDENT_ORDER_AND_RECORD",
        )
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 0)
        self.assertEqual(self.read_us3_safe().get("result"), "pass")

    def test_export_us3_producer_result_commit_unknown_rejects_trace_claim(self):
        index = MOD.US3_SCENARIO_NAMES.index("query_result_commit_unknown")
        value = self.us3_report()
        value["scenarios"][index]["trace"] = {
            "outcome": "FAILED",
            "issue": "TRACE_SAVE_ERROR",
            "sqlstate": "P0001",
            "sqlstate_source": "fixture_probe",
            "confirmed_fact_returned": True,
            "new_lookup": False,
            "proof_source": "APPLICATION_OBSERVED",
        }
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-trace-claim", safe["problems"])

    def test_export_us3_producer_submit_commit_unknown_accepts_empty_record(self):
        index = MOD.US3_SCENARIO_NAMES.index("submit_commit_unknown")
        value = self.us3_report()
        scenario = value["scenarios"][index]
        self.assertEqual(scenario["save"]["record_id"], "")
        self.assertEqual(scenario["records"], [])
        self.assertEqual(scenario["queries"], [])
        self.assertNotIn("trace", scenario)
        self.assertEqual(
            scenario["faults"][0]["kind"], "REGISTRATION_COMMIT_REPLY_LOST"
        )
        self.assertEqual(
            scenario["faults"][0]["cleanup"]["kind"], "UDS_SCENARIO"
        )
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 0)
        self.assertEqual(self.read_us3_safe().get("result"), "pass")

    def test_export_us3_producer_submit_commit_unknown_rejects_fake_record(self):
        index = MOD.US3_SCENARIO_NAMES.index("submit_commit_unknown")
        for mutation in ("uuid", "records"):
            with self.subTest(mutation=mutation):
                value = self.us3_report()
                scenario = value["scenarios"][index]
                if mutation == "uuid":
                    scenario["save"]["record_id"] = self.us3_record_id
                else:
                    scenario["records"] = [
                        self.us3_record(
                            decision="KEEP_UNKNOWN",
                            before="UNKNOWN",
                            after="UNKNOWN",
                            reason="MOCK_RESULT_UNCONFIRMED",
                            source="PAYMENT",
                        )
                    ]
                self.write_db(self.db_report())
                self.write_us3(value)
                code, _ = self.export_db_us3()
                self.assertEqual(code, 1)
                safe = self.read_us3_safe()
                self.assertEqual(safe.get("result"), "fail")

    def test_export_us3_producer_result_save_rejects_empty_or_unbound_record(self):
        index = MOD.US3_SCENARIO_NAMES.index("query_result_commit_unknown")
        for mutation in ("empty", "unbound"):
            with self.subTest(mutation=mutation):
                value = self.us3_report()
                scenario = value["scenarios"][index]
                if mutation == "empty":
                    scenario["save"]["record_id"] = ""
                else:
                    scenario["save"]["record_id"] = (
                        "523e4567-e89b-42d3-a456-426614174004"
                    )
                self.write_db(self.db_report())
                self.write_us3(value)
                code, _ = self.export_db_us3()
                self.assertEqual(code, 1)
                safe = self.read_us3_safe()
                if mutation == "empty":
                    self.assertEqual(safe["status"], "error")
                else:
                    self.assertEqual(safe.get("result"), "fail")
                    self.assertIn(
                        "us3-save-record-binding", safe["problems"]
                    )

    def test_export_us3_query_subtimeout_budget_contract(self):
        index = MOD.US3_SCENARIO_NAMES.index("query_unknown_subtimeout")
        value = self.us3_report()
        scenario = value["scenarios"][index]
        self.assertEqual(
            scenario["budgets"][0]["pass_budget"], False
        )
        self.assertEqual(scenario["budgets"][0]["limit_ms"], 250)
        self.assertEqual(scenario["budgets"][0]["elapsed_ms"], 251)
        self.assertEqual(scenario["budgets"][1]["kind"], "MOCK")
        self.assertEqual(scenario["budgets"][1]["limit_ms"], 3000)
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 0)

        for mutation in ("no-companion", "short-elapsed", "wall-pass"):
            with self.subTest(mutation=mutation):
                value = self.us3_report()
                scenario = value["scenarios"][index]
                if mutation == "no-companion":
                    scenario["budgets"] = scenario["budgets"][:1]
                elif mutation == "short-elapsed":
                    scenario["budgets"][0]["elapsed_ms"] = 249
                    scenario["budgets"][1]["elapsed_ms"] = 249
                else:
                    scenario["budgets"][0]["pass_budget"] = True
                self.write_db(self.db_report())
                self.write_us3(value)
                code, _ = self.export_db_us3()
                self.assertEqual(code, 1)
                safe = self.read_us3_safe()
                self.assertNotEqual(safe.get("result"), "pass")

    def test_export_us3_mock_db_subtimeout_budget_contract(self):
        index = MOD.US3_SCENARIO_NAMES.index(
            "submit_unknown_no_transaction"
        )
        value = self.us3_report()
        value["scenarios"][index]["budgets"] = value["scenarios"][index][
            "budgets"
        ][:1]
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-mock-db-subtimeout-companion", safe["problems"])

        value = self.us3_report()
        value["scenarios"][index]["budgets"][0]["pass_budget"] = True
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        self.assertEqual(self.read_us3_safe()["status"], "error")

    def test_export_us3_trace_failure_not_recorded_shape(self):
        index = MOD.US3_SCENARIO_NAMES.index("query_confirmed_trace_failure")
        value = self.us3_report()
        scenario = value["scenarios"][index]
        self.assertEqual(
            scenario["queries"][0]["decision"], "NOT_RECORDED"
        )
        self.assertEqual(scenario["queries"][0]["result_before"], "")
        self.assertEqual(scenario["queries"][0]["result_after"], "")
        self.assertEqual(
            scenario["queries"][0]["trace_outcome"], "FAILED"
        )
        self.assertEqual(
            scenario["records"][0]["decision"], "ACCEPT_CONFIRMATION"
        )
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 0)

        cases = []
        value = self.us3_report()
        value["scenarios"][index]["queries"][0]["result_before"] = "SUCCEEDED"
        cases.append(("nonempty-before", value, "fail"))
        value = self.us3_report()
        value["scenarios"][index]["queries"][1]["decision"] = "NOT_RECORDED"
        cases.append(("second-not-recorded", value, "fail"))
        value = self.us3_report()
        value["scenarios"][
            MOD.US3_SCENARIO_NAMES.index("query_mismatch_reference")
        ]["queries"][0]["decision"] = "NOT_RECORDED"
        cases.append(("mismatch-not-recorded", value, "fail"))
        value = self.us3_report()
        value["scenarios"][index]["records"][0]["decision"] = "NOT_RECORDED"
        cases.append(("record-not-recorded", value, "error"))
        for name, value, expected in cases:
            with self.subTest(case=name):
                self.write_db(self.db_report())
                self.write_us3(value)
                code, _ = self.export_db_us3()
                self.assertEqual(code, 1)
                safe = self.read_us3_safe()
                if expected == "error":
                    self.assertEqual(safe["status"], "error")
                else:
                    self.assertEqual(safe.get("result"), "fail")

    def test_export_us3_protection_first_second_request_order(self):
        for name in (
            "protection_inflight_failure",
            "protection_inflight_mismatch",
        ):
            index = MOD.US3_SCENARIO_NAMES.index(name)
            value = self.us3_report()
            protection = value["scenarios"][index]["protection"]
            self.assertNotEqual(
                protection["first_applied"], "ACCEPT_CONFIRMATION"
            )
            self.assertEqual(
                protection["second_applied"], "ACCEPT_CONFIRMATION"
            )
            self.write_db(self.db_report())
            self.write_us3(value)
            code, _ = self.export_db_us3()
            self.assertEqual(code, 0)

            value = self.us3_report()
            protection = value["scenarios"][index]["protection"]
            protection["first_applied"] = "ACCEPT_CONFIRMATION"
            protection["second_applied"] = (
                "KEEP_CONFIRMED"
                if name == "protection_inflight_failure"
                else "REJECT_EVIDENCE"
            )
            self.write_db(self.db_report())
            self.write_us3(value)
            code, _ = self.export_db_us3()
            self.assertEqual(code, 1)
            safe = self.read_us3_safe()
            self.assertEqual(safe.get("result"), "fail")
            self.assertIn(
                "us3-protection-first-applied", safe["problems"]
            )

    def test_export_us3_query_request_id_identity(self):
        index = MOD.US3_SCENARIO_NAMES.index("query_unknown_confirmed")
        value = self.us3_report()
        value["scenarios"][index]["queries"][0].pop("request_id")
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        self.assertEqual(self.read_us3_safe()["status"], "error")

        value = self.us3_report()
        scenario = value["scenarios"][index]
        scenario["queries"][0]["request_id"] = self.us3_request_id_n(9)
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-query-request-http", safe["problems"])

        value = self.us3_report()
        scenario = value["scenarios"][index]
        scenario["queries"].append(dict(scenario["queries"][0]))
        scenario["http"].append(
            self.us3_http(request_id=scenario["queries"][0]["request_id"])
        )
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-query-request-id-duplicate", safe["problems"])

    def test_export_us3_restart_query_proof_required(self):
        for name in (
            "restart_registered_unpaid",
            "restart_mock_committed_reply_lost",
        ):
            index = MOD.US3_SCENARIO_NAMES.index(name)
            with self.subTest(name=name, case="baseline"):
                self.write_db(self.db_report())
                self.write_us3(self.us3_report())
                code, _ = self.export_db_us3()
                self.assertEqual(code, 0)
                self.assertEqual(self.read_us3_safe()["result"], "pass")

            cases = {}
            value = self.us3_report()
            scenario = value["scenarios"][index]
            scenario["queries"] = []
            scenario["records"] = []
            scenario["calls"][1]["query_calls"] = 0
            scenario["checks"]["counts_after"] = self.us3_counts(
                0, 0, 1, scenario["pg"][1]["rows"], 0
            )
            scenario["pg"][2]["rows"] = 0
            scenario["generation_proofs"][1]["counts"] = self.us3_counts(
                0, 0, 0, 0, 0
            )
            scenario["generation_proofs"][1]["receipt"]["last_seq"] = 0
            cases["missing-query-record-zero-calls-trace-seq"] = value

            value = self.us3_report()
            value["scenarios"][index]["http"] = value["scenarios"][index][
                "http"
            ][:1]
            cases["replay-http-only"] = value

            value = self.us3_report()
            value["scenarios"][index]["records"] = []
            cases["missing-saved-record"] = value

            value = self.us3_report()
            scenario = value["scenarios"][index]
            scenario["queries"][0]["request_id"] = scenario["http"][0][
                "request_id"
            ]
            cases["swapped-request-id"] = value

            value = self.us3_report()
            value["scenarios"][index]["queries"][0]["trusted"] = False
            cases["untrusted-query"] = value

            value = self.us3_report()
            value["scenarios"][index]["queries"][0]["generation"] = 1
            cases["bad-query-generation"] = value

            for case, value in cases.items():
                with self.subTest(name=name, case=case):
                    self.write_db(self.db_report())
                    self.write_us3(value)
                    code, _ = self.export_db_us3()
                    self.assertEqual(code, 1)
                    safe = self.read_us3_safe()
                    self.assertTrue(
                        safe.get("result") == "fail"
                        or safe.get("status") == "error"
                    )

    def test_export_us3_query_request_must_match_primary_not_probe(self):
        index = MOD.US3_SCENARIO_NAMES.index("query_unknown_confirmed")
        value = self.us3_report()
        scenario = value["scenarios"][index]
        probe_id = self.us3_request_id_n(7)
        scenario["queries"][0]["request_id"] = probe_id
        scenario["http"].append(
            self.us3_http(
                kind="SHUTDOWN_PROBE",
                status=503,
                result="",
                request_id=probe_id,
            )
        )
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-query-request-http", safe["problems"])

    def test_export_us3_query_record_identity_must_match_saved_query(self):
        index = MOD.US3_SCENARIO_NAMES.index("query_unknown_confirmed")
        value = self.us3_report()
        scenario = value["scenarios"][index]
        scenario["records"][0]["request_id"] = self.us3_request_id_n(8)
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-query-record", safe["problems"])

    def test_export_us3_trace_failure_accept_record_binds_second_request(self):
        index = MOD.US3_SCENARIO_NAMES.index("query_confirmed_trace_failure")
        value = self.us3_report()
        scenario = value["scenarios"][index]
        scenario["records"][0]["request_id"] = self.us3_request_id_n(9)
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-trace-failure-accept-binding", safe["problems"])

    def test_export_us3_http_array_order_is_not_business_relation(self):
        index = MOD.US3_SCENARIO_NAMES.index("protection_inflight_failure")
        value = self.us3_report()
        scenario = value["scenarios"][index]
        scenario["http"] = list(reversed(scenario["http"]))
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 0)
        self.assertEqual(self.read_us3_safe().get("result"), "pass")

    def test_export_us3_seed_historical_record_not_in_http_allowed(self):
        index = MOD.US3_SCENARIO_NAMES.index("query_unknown_confirmed")
        value = self.us3_report()
        scenario = value["scenarios"][index]
        scenario["records"].append(
            self.us3_record(
                decision="KEEP_UNKNOWN",
                before="UNKNOWN",
                after="UNKNOWN",
                reason="MOCK_RESULT_NOT_FOUND",
                source="PAYMENT",
                request_id=self.us3_request_id_n(9),
            )
        )
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 0)
        self.assertEqual(self.read_us3_safe().get("result"), "pass")

    def test_export_us3_zero_receipts_allow_only_zero_business_events(self):
        for name in (
            "query_not_found",
            "query_confirmed_plain",
            "lifecycle_cancel_before_mock",
            "lifecycle_exit_budget",
        ):
            index = MOD.US3_SCENARIO_NAMES.index(name)
            value = self.us3_report()
            scenario = value["scenarios"][index]
            self.assertEqual(
                scenario["generation_proofs"][0]["receipt"]["last_seq"], 0
            )
            self.assertEqual(
                scenario["calls"][0]["payment_calls"], 0
            )
            self.assertEqual(scenario["calls"][0]["query_calls"], 0)
            self.assertEqual(scenario["queries"], [])
            self.write_db(self.db_report())
            self.write_us3(value)
            code, _ = self.export_db_us3()
            self.assertEqual(code, 0)

    def test_export_us3_zero_receipt_rejects_nonzero_calls(self):
        index = MOD.US3_SCENARIO_NAMES.index("query_not_found")
        value = self.us3_report()
        value["scenarios"][index]["calls"][0]["payment_calls"] = 1
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-zero-receipt-calls", safe["problems"])

    def test_export_us3_zero_receipt_rejects_query_application(self):
        index = MOD.US3_SCENARIO_NAMES.index("lifecycle_cancel_before_mock")
        value = self.us3_report()
        scenario = value["scenarios"][index]
        scenario["http"] = [
            self.us3_http(request_id=self.us3_request_id_n(1))
        ]
        scenario["queries"] = [
            self.us3_query(
                outcome="not_found",
                decision="KEEP_UNKNOWN",
                before="UNKNOWN",
                after="UNKNOWN",
                trace_outcome="NOT_ATTEMPTED",
                applied=False,
                request_id=self.us3_request_id_n(1),
            )
        ]
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-zero-receipt-queries", safe["problems"])

    def test_export_us3_receipt_seq_bounds_and_generation(self):
        index = MOD.US3_SCENARIO_NAMES.index("query_not_found")
        for mutation, expected in (
            ("negative", "unsafe-field-us3.receipt.last_seq"),
            ("overlimit", "unsafe-field-us3.receipt.last_seq"),
            ("generation", "unsafe-field-us3.receipt.generation"),
        ):
            with self.subTest(mutation=mutation):
                value = self.us3_report()
                receipt = value["scenarios"][index]["generation_proofs"][0][
                    "receipt"
                ]
                if mutation == "negative":
                    receipt["last_seq"] = -1
                elif mutation == "overlimit":
                    receipt["last_seq"] = 4097
                else:
                    receipt["generation"] = 2
                self.write_db(self.db_report())
                self.write_us3(value)
                code, _ = self.export_db_us3()
                self.assertEqual(code, 1)
                safe = self.read_us3_safe()
                self.assertEqual(safe["status"], "error")
                self.assertEqual(safe["error"], expected)

    def test_export_us3_controlled_truncated_receipt_rejected(self):
        index = MOD.US3_SCENARIO_NAMES.index("restart_registered_unpaid")
        value = self.us3_report()
        value["scenarios"][index]["generation_proofs"][0]["receipt"] = {
            "run_id": self.run_id,
            "epoch": self.us3_epoch,
            "generation": 1,
            "last_seq": 0,
        }
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 1)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us3-controlled-proof", safe["problems"])

    def test_export_us3_trace_proof_source_exact_literal_is_canonicalized(self):
        index = MOD.US3_SCENARIO_NAMES.index("query_confirmed_trace_failure")
        value = self.us3_report()
        value["scenarios"][index]["trace"]["proof_source"] = (
            MOD.US3_TRACE_PROOF_SOURCE_LITERAL
        )
        self.write_db(self.db_report())
        self.write_us3(value)
        code, _ = self.export_db_us3()
        self.assertEqual(code, 0)
        safe = self.read_us3_safe()
        self.assertEqual(safe.get("result"), "pass")
        trace = next(
            scenario["trace"]
            for scenario in safe["scenarios"]
            if scenario["name"] == "query_confirmed_trace_failure"
        )
        self.assertEqual(
            trace["proof_source"],
            MOD.US3_TRACE_PROOF_SOURCE_CANONICAL,
        )
        self.assertNotIn(
            "actual first-request HTTP TRACE_SAVE_ERROR",
            json.dumps(safe),
        )

    def test_export_us3_trace_proof_source_arbitrary_prose_rejected(self):
        index = MOD.US3_SCENARIO_NAMES.index("query_confirmed_trace_failure")
        for mutation in (
            "arbitrary prose with spaces",
            MOD.US3_TRACE_PROOF_SOURCE_LITERAL + " SECRET_CANARY",
            "sk_live_" + "A" * 100,
        ):
            with self.subTest(mutation=mutation[:24]):
                value = self.us3_report()
                value["scenarios"][index]["trace"]["proof_source"] = mutation
                self.write_db(self.db_report())
                self.write_us3(value)
                code, _ = self.export_db_us3()
                self.assertEqual(code, 1)
                safe = self.read_us3_safe()
                self.assertEqual(safe["status"], "error")
                self.assertEqual(
                    safe["error"],
                    "unsafe-field-us3.trace.proof_source",
                )
                self.assertNotIn("SECRET_CANARY", json.dumps(safe))

    def test_export_us3_tablelock_fault_empty_source_canonicalizes_to_none(self):
        for name in (
            "lifecycle_cancel_before_mock",
            "lifecycle_cancel_after_mock",
            "lifecycle_exit_budget",
        ):
            index = MOD.US3_SCENARIO_NAMES.index(name)
            value = self.us3_report()
            scenario = value["scenarios"][index]
            self.assertEqual(
                scenario["faults"][0]["sqlstate_source"], ""
            )
            self.assertEqual(scenario["faults"][0]["sqlstate"], "")
            self.assertEqual(
                scenario["faults"][0]["cleanup"]["kind"],
                "TABLE_SHARE_LOCK",
            )
            self.write_db(self.db_report())
            self.write_us3(value)
            code, _ = self.export_db_us3()
            self.assertEqual(code, 0)
            safe = self.read_us3_safe()
            projected = next(
                item for item in safe["scenarios"] if item["name"] == name
            )
            self.assertEqual(
                projected["faults"][0]["sqlstate_source"], "none"
            )

    def test_export_us3_empty_fault_source_rejects_other_shapes(self):
        index = MOD.US3_SCENARIO_NAMES.index("lifecycle_cancel_before_mock")
        for mutation in (
            "sqlstate",
            "sql-fault",
            "other-kind",
            "wrong-cleanup",
            "missing-key",
            "none-with-sqlstate",
        ):
            with self.subTest(mutation=mutation):
                value = self.us3_report()
                fault = value["scenarios"][index]["faults"][0]
                if mutation == "sqlstate":
                    fault["sqlstate"] = "P0001"
                elif mutation == "sql-fault":
                    fault["kind"] = "TRACE_INSERT_SQL"
                    fault["sqlstate"] = "P0001"
                elif mutation == "other-kind":
                    fault["kind"] = "MOCK_QUERY_FAILURE"
                elif mutation == "wrong-cleanup":
                    fault["cleanup"]["kind"] = "UDS_SCENARIO"
                elif mutation == "missing-key":
                    del fault["sqlstate_source"]
                else:
                    fault["sqlstate"] = "P0001"
                    fault["sqlstate_source"] = "none"
                self.write_db(self.db_report())
                self.write_us3(value)
                code, _ = self.export_db_us3()
                self.assertEqual(code, 1)
                safe = self.read_us3_safe()
                self.assertEqual(safe["status"], "error")
                expected = (
                    "unsafe-field-us3.fault"
                    if mutation == "missing-key"
                    else "unsafe-field-us3.fault.sqlstate_source"
                )
                self.assertEqual(safe["error"], expected)

    def test_export_us2_complete_pass_rebuilds_safe_report(self):
        self.write_db(self.db_report())
        self.write_us2(self.us2_report())
        code, _ = self.export_db_us2()
        self.assertEqual(code, 0)
        safe = self.read_us2_safe()
        self.assertEqual(safe["schema"], "payment-hub.ci-safe.us2.v1")
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe.get("result"), "pass")
        self.assertEqual(len(safe["scenarios"]), 8)
        self.assertEqual(safe["run_id"], self.run_id)
        self.assertEqual(
            [item["name"] for item in safe["scenarios"]],
            list(self.US2_SCENARIOS),
        )
        for scenario in safe["scenarios"]:
            self.assertIn("checks", scenario)
            self.assertEqual(
                set(scenario["checks"]),
                {
                    "counts_before",
                    "counts_after",
                    "stable_facts",
                    "readonly_probes",
                    "lookup_fault",
                    "waiter_count",
                    "mixed_waiter_count",
                },
            )
        text = json.dumps(safe)
        for forbidden in ("SECRET", "Authorization", "postgres://"):
            self.assertNotIn(forbidden, text)
        provenance = json.loads(
            (self.report_dir / "upload" / "provenance.json").read_text()
        )
        record = next(
            entry for entry in provenance["reports"] if entry["name"] == "us2"
        )
        self.assertEqual(record["source"], self.run_id + "/us2.json")
        self.assertEqual(record["export"], "us2.safe.json")
        self.assertRegex(record["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(record["export_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(record["source_manifest_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            record["source_manifest_verification"], "verified-unchanged"
        )

    def test_export_us2_missing_file_is_missing_nonzero(self):
        self.write_db(self.db_report())
        code, _ = self.export_db_us2()
        self.assertEqual(code, 1)
        safe = self.read_us2_safe()
        self.assertEqual(safe["status"], "missing")

    def test_export_us2_wrong_schema_rejected(self):
        value = self.us2_report()
        value["schema"] = "payment-hub.us1.v1"
        self.write_db(self.db_report())
        self.write_us2(value)
        code, _ = self.export_db_us2()
        self.assertEqual(code, 1)
        self.assertEqual(self.read_us2_safe()["status"], "error")

    def test_export_us2_missing_scenario_is_non_green(self):
        value = self.us2_report()
        value["scenarios"] = value["scenarios"][:-1]
        self.write_db(self.db_report())
        self.write_us2(value)
        code, _ = self.export_db_us2()
        self.assertEqual(code, 1)
        safe = self.read_us2_safe()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("key_rotation", safe["missing_scenarios"])

    def test_export_us2_duplicate_scenario_rejected(self):
        value = self.us2_report()
        value["scenarios"].append(value["scenarios"][0])
        self.write_db(self.db_report())
        self.write_us2(value)
        code, _ = self.export_db_us2()
        self.assertEqual(code, 1)
        self.assertEqual(self.read_us2_safe()["status"], "error")

    def test_export_us2_unknown_scenario_rejected(self):
        value = self.us2_report()
        value["scenarios"][0]["name"] = "bogus_scenario"
        self.write_db(self.db_report())
        self.write_us2(value)
        code, _ = self.export_db_us2()
        self.assertEqual(code, 1)
        self.assertEqual(self.read_us2_safe()["status"], "error")

    def test_export_us2_scenario_result_unknown_is_rejected(self):
        # UNKNOWN is an HTTP/payment result, never a scenario execution
        # conclusion. This guards against treating replay_unknown as a green
        # UNKNOWN scenario instead of PASS with HTTP result UNKNOWN.
        value = self.us2_report()
        value["scenarios"][2]["result"] = "UNKNOWN"
        self.write_db(self.db_report())
        self.write_us2(value)
        code, _ = self.export_db_us2()
        self.assertEqual(code, 1)
        safe = self.read_us2_safe()
        self.assertEqual(safe["status"], "error")
        self.assertNotEqual(safe.get("result"), "pass")

    def test_export_us2_replay_unknown_pass_with_http_unknown(self):
        self.write_db(self.db_report())
        self.write_us2(self.us2_report())
        code, _ = self.export_db_us2()
        self.assertEqual(code, 0)
        safe = self.read_us2_safe()
        replay = safe["scenarios"][2]
        self.assertEqual(replay["name"], "replay_unknown")
        self.assertEqual(replay["result"], "PASS")
        self.assertEqual([item["status"] for item in replay["http"]], [200, 200])
        self.assertEqual(
            [item["result"] for item in replay["http"]],
            ["UNKNOWN", "UNKNOWN"],
        )
        self.assertIsNone(replay["directive"])

    def test_export_us2_replay_result_mismatch_is_non_green(self):
        for index, bad_result in ((0, "UNKNOWN"), (1, "SUCCEEDED"), (2, "SUCCEEDED")):
            with self.subTest(index=index):
                value = self.us2_report()
                value["scenarios"][index]["http"][-1]["result"] = bad_result
                self.write_db(self.db_report())
                self.write_us2(value)
                code, _ = self.export_db_us2()
                self.assertEqual(code, 1)
                self.assertEqual(self.read_us2_safe().get("result"), "fail")

    def test_export_us2_replay_requires_two_200_and_all_results(self):
        for index in (0, 1):
            expected = (
                "SUCCEEDED"
                if self.us2_report()["scenarios"][index]["name"]
                == "replay_success"
                else "FAILED"
            )
            for mutation in (
                "drop-second-200",
                "change-nonfinal-result",
                "unexpected-401",
            ):
                with self.subTest(index=index, mutation=mutation):
                    value = self.us2_report()
                    http = value["scenarios"][index]["http"]
                    if mutation == "drop-second-200":
                        for position, item in enumerate(http):
                            if item["status"] == 200:
                                http.pop(position)
                                break
                    elif mutation == "change-nonfinal-result":
                        for item in http:
                            if item["status"] == 200:
                                item["result"] = (
                                    "FAILED"
                                    if expected == "SUCCEEDED"
                                    else "SUCCEEDED"
                                )
                                break
                    else:
                        http.append(self.us2_http(401, "", "", ""))
                    self.write_db(self.db_report())
                    self.write_us2(value)
                    code, _ = self.export_db_us2()
                    self.assertEqual(code, 1)
                    self.assertEqual(self.read_us2_safe().get("result"), "fail")

    def test_export_us2_restart_requires_setup_and_two_200(self):
        for index in (5, 6):
            expected = (
                "SUCCEEDED"
                if self.us2_report()["scenarios"][index]["name"]
                == "restart_success"
                else "FAILED"
            )
            for mutation in (
                "drop-second-200",
                "change-nonfinal-result",
                "unexpected-409",
            ):
                with self.subTest(index=index, mutation=mutation):
                    value = self.us2_report()
                    http = value["scenarios"][index]["http"]
                    if mutation == "drop-second-200":
                        for position, item in enumerate(http):
                            if item["status"] == 200:
                                http.pop(position)
                                break
                    elif mutation == "change-nonfinal-result":
                        for item in http:
                            if item["status"] == 200:
                                item["result"] = (
                                    "FAILED"
                                    if expected == "SUCCEEDED"
                                    else "SUCCEEDED"
                                )
                                break
                    else:
                        http.append(self.us2_http(409, "", "", ""))
                    self.write_db(self.db_report())
                    self.write_us2(value)
                    code, _ = self.export_db_us2()
                    self.assertEqual(code, 1)
                    self.assertEqual(self.read_us2_safe().get("result"), "fail")

    def test_export_us2_key_rotation_requires_201_two_200_401(self):
        for mutation in (
            "drop-second-200",
            "change-nonfinal-result",
            "missing-401",
            "401-result",
            "401-new-id",
            "unexpected-409",
        ):
            with self.subTest(mutation=mutation):
                value = self.us2_report()
                http = value["scenarios"][7]["http"]
                if mutation == "drop-second-200":
                    for position, item in enumerate(http):
                        if item["status"] == 200:
                            http.pop(position)
                            break
                elif mutation == "change-nonfinal-result":
                    next(
                        item for item in http if item["status"] == 200
                    )["result"] = "FAILED"
                elif mutation == "missing-401":
                    http.remove(
                        next(item for item in http if item["status"] == 401)
                    )
                elif mutation == "401-result":
                    next(
                        item for item in http if item["status"] == 401
                    )["result"] = "FAILED"
                elif mutation == "401-new-id":
                    next(
                        item for item in http if item["status"] == 401
                    )["payment_id"] = self.US2_OTHER_ID
                else:
                    http.append(self.us2_http(409, "", "", ""))
                self.write_db(self.db_report())
                self.write_us2(value)
                code, _ = self.export_db_us2()
                self.assertEqual(code, 1)
                self.assertEqual(self.read_us2_safe().get("result"), "fail")

    def test_export_us2_all_scenarios_must_pass(self):
        value = self.us2_report()
        value["scenarios"][0]["result"] = "FAIL"
        self.write_db(self.db_report())
        self.write_us2(value)
        code, _ = self.export_db_us2()
        self.assertEqual(code, 1)
        safe = self.read_us2_safe()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe.get("result"), "fail")
        self.assertIn("us2-scenario-not-pass", safe["problems"])

    def test_export_us2_observer_required_and_trusted(self):
        for mutation in ("missing", "untrusted", "empty-receipts"):
            with self.subTest(mutation=mutation):
                value = self.us2_report()
                scenario = value["scenarios"][0]
                if mutation == "missing":
                    scenario["observer"] = None
                elif mutation == "untrusted":
                    scenario["observer"]["trusted"] = False
                else:
                    scenario["observer"]["receipts"] = []
                self.write_db(self.db_report())
                self.write_us2(value)
                code, _ = self.export_db_us2()
                self.assertEqual(code, 1)
                self.assertEqual(self.read_us2_safe().get("result"), "fail")

    def test_export_us2_counts_must_match_observer_and_pg(self):
        for mutation in ("after-payment", "observer-payment", "pg-order"):
            with self.subTest(mutation=mutation):
                value = self.us2_report()
                scenario = value["scenarios"][0]
                if mutation == "after-payment":
                    scenario["checks"]["counts_after"]["payment_calls"] = 2
                elif mutation == "observer-payment":
                    scenario["observer"]["counts"]["payment"] = 2
                else:
                    next(
                        item
                        for item in scenario["pg"]
                        if item["state"] == "ORDER"
                    )["rows"] = 2
                self.write_db(self.db_report())
                self.write_us2(value)
                code, _ = self.export_db_us2()
                self.assertEqual(code, 1)
                self.assertEqual(self.read_us2_safe().get("result"), "fail")

    def test_export_us2_checks_exact_fields_and_uint_types(self):
        for mutation in (
            "extra-field",
            "bool-uint",
            "negative-uint",
            "bad-fault",
            "missing-count-key",
        ):
            with self.subTest(mutation=mutation):
                value = self.us2_report()
                checks = value["scenarios"][0]["checks"]
                if mutation == "extra-field":
                    checks["raw_secret"] = "SECRET_CANARY"
                elif mutation == "bool-uint":
                    checks["waiter_count"] = True
                elif mutation == "negative-uint":
                    checks["readonly_probes"] = -1
                elif mutation == "bad-fault":
                    checks["lookup_fault"] = "error"
                else:
                    del checks["counts_after"]["trace_rows"]
                self.write_db(self.db_report())
                self.write_us2(value)
                code, _ = self.export_db_us2()
                self.assertEqual(code, 1)
                text = json.dumps(self.read_us2_safe())
                self.assertNotIn("SECRET_CANARY", text)
                self.assertNotEqual(
                    json.loads(
                        (self.report_dir / "upload" / "us2.safe.json").read_text()
                    ).get("result"),
                    "pass",
                )

    def test_export_us2_mixed_waiter_count_required_and_scoped(self):
        self.write_db(self.db_report())
        self.write_us2(self.us2_report())
        code, _ = self.export_db_us2()
        self.assertEqual(code, 0)
        safe = self.read_us2_safe()
        self.assertEqual(safe.get("result"), "pass")
        for scenario in safe["scenarios"]:
            with self.subTest(name=scenario["name"]):
                self.assertIn(
                    "mixed_waiter_count", scenario["checks"]
                )
                expected = (
                    2 if scenario["name"] == "concurrency" else 0
                )
                self.assertEqual(
                    scenario["checks"]["mixed_waiter_count"], expected
                )

    def test_export_us2_missing_mixed_waiter_count_is_rejected(self):
        value = self.us2_report()
        for scenario in value["scenarios"]:
            del scenario["checks"]["mixed_waiter_count"]
        self.write_db(self.db_report())
        self.write_us2(value)
        code, _ = self.export_db_us2()
        self.assertEqual(code, 1)
        self.assertEqual(self.read_us2_safe()["status"], "error")

    def test_export_us2_mixed_waiter_count_type_rejections(self):
        # Baseline with the valid field must be accepted first; the old
        # validator rejects the required new field as an unknown extra.
        self.write_db(self.db_report())
        self.write_us2(self.us2_report())
        code, _ = self.export_db_us2()
        self.assertEqual(code, 0)
        for mutation in ("bool", "negative", "string"):
            with self.subTest(mutation=mutation):
                value = self.us2_report()
                checks = value["scenarios"][0]["checks"]
                if mutation == "bool":
                    checks["mixed_waiter_count"] = True
                elif mutation == "negative":
                    checks["mixed_waiter_count"] = -1
                else:
                    checks["mixed_waiter_count"] = "2"
                self.write_db(self.db_report())
                self.write_us2(value)
                code, _ = self.export_db_us2()
                self.assertEqual(code, 1)
                self.assertEqual(self.read_us2_safe()["status"], "error")

    def test_export_us2_mixed_waiter_count_scenario_mismatch(self):
        for mutation in (
            "concurrency-0",
            "concurrency-1",
            "concurrency-3",
            "other-1",
            "borrowed-waiter-2-mixed-0",
        ):
            with self.subTest(mutation=mutation):
                value = self.us2_report()
                if mutation == "concurrency-0":
                    value["scenarios"][4]["checks"][
                        "mixed_waiter_count"
                    ] = 0
                elif mutation == "concurrency-1":
                    value["scenarios"][4]["checks"][
                        "mixed_waiter_count"
                    ] = 1
                elif mutation == "concurrency-3":
                    value["scenarios"][4]["checks"][
                        "mixed_waiter_count"
                    ] = 3
                elif mutation == "other-1":
                    value["scenarios"][0]["checks"][
                        "mixed_waiter_count"
                    ] = 1
                else:
                    value["scenarios"][4]["checks"][
                        "waiter_count"
                    ] = 2
                    value["scenarios"][4]["checks"][
                        "mixed_waiter_count"
                    ] = 0
                self.write_db(self.db_report())
                self.write_us2(value)
                code, _ = self.export_db_us2()
                self.assertEqual(code, 1)
                safe = self.read_us2_safe()
                self.assertEqual(safe["status"], "exported")
                self.assertEqual(safe.get("result"), "fail")

    def test_export_us2_stable_facts_must_be_true(self):
        value = self.us2_report()
        value["scenarios"][0]["checks"]["stable_facts"] = False
        self.write_db(self.db_report())
        self.write_us2(value)
        code, _ = self.export_db_us2()
        self.assertEqual(code, 1)
        self.assertEqual(self.read_us2_safe().get("result"), "fail")

    def test_export_us2_concurrency_requires_two_waiters_and_statuses(self):
        for mutation in (
            "waiters",
            "no-201",
            "two-201",
            "no-409",
            "too-few-200",
            "final-get-different-id",
            "final-get-different-result",
        ):
            with self.subTest(mutation=mutation):
                value = self.us2_report()
                scenario = value["scenarios"][4]
                http = scenario["http"]
                if mutation == "waiters":
                    scenario["checks"]["waiter_count"] = 1
                elif mutation == "no-201":
                    next(item for item in http if item["status"] == 201)[
                        "status"
                    ] = 200
                elif mutation == "two-201":
                    http[1]["status"] = 201
                elif mutation == "no-409":
                    next(item for item in http if item["status"] == 409)[
                        "status"
                    ] = 200
                elif mutation == "too-few-200":
                    scenario["http"] = [
                        item for item in http if item["status"] != 200
                    ][:1] + [
                        item for item in http if item["status"] == 200
                    ][:2]
                elif mutation == "final-get-different-id":
                    http[-1]["payment_id"] = self.US2_OTHER_ID
                else:
                    http[-1]["result"] = "UNKNOWN"
                self.write_db(self.db_report())
                self.write_us2(value)
                code, _ = self.export_db_us2()
                self.assertEqual(code, 1)
                self.assertEqual(self.read_us2_safe().get("result"), "fail")

    def test_export_us2_conflict_requires_setup201_and_status_matrix(self):
        for mutation in (
            "no-setup-201",
            "two-201",
            "missing-409",
            "missing-400",
            "missing-401",
            "missing-503",
            "normal-500",
            "new-id-on-error",
            "result-on-error",
        ):
            with self.subTest(mutation=mutation):
                value = self.us2_report()
                scenario = value["scenarios"][3]
                http = scenario["http"]
                if mutation == "no-setup-201":
                    next(item for item in http if item["status"] == 201)[
                        "status"
                    ] = 200
                elif mutation == "two-201":
                    http.append(
                        self.us2_http(
                            201,
                            self.us2_request_id(90),
                            self.US2_OTHER_ID,
                            "SUCCEEDED",
                        )
                    )
                elif mutation == "missing-409":
                    http.remove(next(item for item in http if item["status"] == 409))
                elif mutation == "missing-400":
                    http.remove(next(item for item in http if item["status"] == 400))
                elif mutation == "missing-401":
                    http.remove(next(item for item in http if item["status"] == 401))
                elif mutation == "missing-503":
                    http.remove(next(item for item in http if item["status"] == 503))
                elif mutation == "normal-500":
                    http.append(self.us2_http(500, "", "", ""))
                elif mutation == "new-id-on-error":
                    next(item for item in http if item["status"] == 409)[
                        "payment_id"
                    ] = self.US2_OTHER_ID
                else:
                    next(item for item in http if item["status"] == 400)[
                        "result"
                    ] = "FAILED"
                self.write_db(self.db_report())
                self.write_us2(value)
                code, _ = self.export_db_us2()
                self.assertEqual(code, 1)
                self.assertEqual(self.read_us2_safe().get("result"), "fail")

    def test_export_us2_controlled_500_is_not_accepted_in_binary_scenarios(self):
        for index in (0, 3, 4):
            with self.subTest(index=index):
                value = self.us2_report()
                value["scenarios"][index]["http"].append(
                    self.us2_http(500, "", "", "")
                )
                self.write_db(self.db_report())
                self.write_us2(value)
                code, _ = self.export_db_us2()
                self.assertEqual(code, 1)
                self.assertEqual(self.read_us2_safe().get("result"), "fail")

    def test_export_us2_readonly_and_fault_matrix(self):
        for mutation in ("readonly", "fault", "other-readonly", "other-fault"):
            with self.subTest(mutation=mutation):
                value = self.us2_report()
                if mutation == "readonly":
                    value["scenarios"][3]["checks"]["readonly_probes"] = 4
                elif mutation == "fault":
                    value["scenarios"][3]["checks"]["lookup_fault"] = "none"
                elif mutation == "other-readonly":
                    value["scenarios"][0]["checks"]["readonly_probes"] = 1
                else:
                    value["scenarios"][4]["checks"]["lookup_fault"] = "timeout"
                self.write_db(self.db_report())
                self.write_us2(value)
                code, _ = self.export_db_us2()
                self.assertEqual(code, 1)
                self.assertEqual(self.read_us2_safe().get("result"), "fail")

    def test_export_us2_receipts_bind_run_epoch_generation(self):
        for mutation in (
            "run-id",
            "epoch",
            "generation",
            "missing-generation",
        ):
            with self.subTest(mutation=mutation):
                value = self.us2_report()
                scenario = value["scenarios"][5]
                receipts = scenario["observer"]["receipts"]
                if mutation == "run-id":
                    receipts[0]["run_id"] = "b" * 32
                elif mutation == "epoch":
                    receipts[0]["epoch"] = "a" * 32
                elif mutation == "generation":
                    receipts[0]["generation"] = 2
                else:
                    receipts.pop()
                self.write_db(self.db_report())
                self.write_us2(value)
                code, _ = self.export_db_us2()
                self.assertEqual(code, 1)
                self.assertEqual(self.read_us2_safe().get("result"), "fail")

    def test_export_us2_generations_restart_and_key_rotation(self):
        for mutation in (
            "restart-missing-generation",
            "restart-not-two",
            "key-rotation-one-generation",
            "duplicate-generation",
        ):
            with self.subTest(mutation=mutation):
                value = self.us2_report()
                if mutation == "restart-missing-generation":
                    value["scenarios"][5]["generations"] = [1]
                elif mutation == "restart-not-two":
                    value["scenarios"][5]["generations"] = [1, 2, 3]
                elif mutation == "key-rotation-one-generation":
                    value["scenarios"][7]["generations"] = [1]
                else:
                    value["scenarios"][0]["generations"] = [1, 1]
                self.write_db(self.db_report())
                self.write_us2(value)
                code, _ = self.export_db_us2()
                self.assertEqual(code, 1)
                safe = self.read_us2_safe()
                if mutation in ("restart-not-two", "duplicate-generation"):
                    self.assertEqual(safe["status"], "error")
                else:
                    self.assertEqual(safe.get("result"), "fail")

    def test_export_us2_directive_binding_checks(self):
        for mutation in (
            "run-id",
            "epoch",
            "generation",
            "last-seq",
        ):
            with self.subTest(mutation=mutation):
                value = self.us2_report()
                scenario = value["scenarios"][4]
                binding = scenario["directive"]["binding"]
                if mutation == "run-id":
                    binding["run_id"] = "b" * 32
                elif mutation == "epoch":
                    binding["epoch"] = "a" * 32
                elif mutation == "generation":
                    binding["generation"] = 2
                else:
                    scenario["observer"]["receipts"][0]["last_seq"] = 1
                self.write_db(self.db_report())
                self.write_us2(value)
                code, _ = self.export_db_us2()
                self.assertEqual(code, 1)
                self.assertEqual(self.read_us2_safe().get("result"), "fail")

    def test_export_us2_commands_must_be_observed(self):
        for mutation in (
            "empty",
            "not-run",
            "nonzero",
            "missing-build",
            "missing-process-stop",
        ):
            with self.subTest(mutation=mutation):
                value = self.us2_report()
                commands = value["scenarios"][0]["commands"]
                if mutation == "empty":
                    value["scenarios"][0]["commands"] = []
                elif mutation == "not-run":
                    commands[0]["not_run"] = True
                    commands[0]["exit"] = None
                elif mutation == "nonzero":
                    commands[0]["exit"] = 1
                elif mutation == "missing-build":
                    value["scenarios"][0]["commands"] = [
                        item
                        for item in commands
                        if item["kind"] != "build"
                    ]
                else:
                    value["scenarios"][0]["commands"] = [
                        item
                        for item in commands
                        if item["kind"] != "process_stop"
                    ]
                self.write_db(self.db_report())
                self.write_us2(value)
                code, _ = self.export_db_us2()
                self.assertEqual(code, 1)
                self.assertEqual(self.read_us2_safe().get("result"), "fail")

    def test_export_us2_optional_http_probe_command_is_allowed(self):
        value = self.us2_report()
        value["scenarios"][0]["commands"].append(
            {"kind": "http_probe", "exit": 0, "not_run": False}
        )
        self.write_db(self.db_report())
        self.write_us2(value)
        code, _ = self.export_db_us2()
        self.assertEqual(code, 0)
        self.assertEqual(self.read_us2_safe().get("result"), "pass")

    def test_export_us2_request_id_duplicate_non_green(self):
        value = self.us2_report()
        value["scenarios"][0]["http"][1]["request_id"] = value[
            "scenarios"
        ][0]["http"][0]["request_id"]
        self.write_db(self.db_report())
        self.write_us2(value)
        code, _ = self.export_db_us2()
        self.assertEqual(code, 1)
        self.assertEqual(self.read_us2_safe().get("result"), "fail")

    def test_export_us2_run_id_mismatch_rejected(self):
        value = self.us2_report()
        value["run_id"] = "b" * 32
        self.write_db(self.db_report())
        self.write_us2(value, run_id=self.run_id)
        code, _ = self.export_db_us2()
        self.assertEqual(code, 1)
        self.assertEqual(self.read_us2_safe()["status"], "error")

    def test_export_us2_db_not_green_non_green(self):
        self.write_db(self.db_report(result="fail"))
        self.write_us2(self.us2_report())
        code, _ = self.export_db_us2()
        self.assertEqual(code, 1)
        safe = self.read_us2_safe()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe.get("result"), "fail")

    def test_export_us2_db_missing_new_roots_error(self):
        value = self.db_report()
        value["tests"]["required_test_results"] = {
            name: "pass"
            for name in (
                "TestCommitHookIntegrationHidesReplyAfterRealCommit",
                "TestHarnessProcessPreservesParentStateIntegration",
                "TestApplicationLifecycleIntegration",
                "TestApplicationLifecycleIntegration/startup_diagnostics",
                "TestOrderRegistrationIntegration",
                "TestObservationAtomicSaveIntegration",
                "TestMockTransactionIntegration",
                "TestOrderLookupIntegration",
                "TestUS1Payment",
                "TestUS1Restart",
                "TestUS1HTTPBoundary",
            )
        }
        self.write_db(value)
        self.write_us2(self.us2_report())
        code, _ = self.export_db_us2()
        self.assertEqual(code, 1)
        safe = self.read_us2_safe()
        self.assertEqual(safe["status"], "error")

    def test_export_us2_unsafe_extras_rejected(self):
        value = self.us2_report()
        value["authorization"] = "Bearer SECRET_CANARY"
        value["scenarios"][0]["raw_log"] = "SECRET_CANARY"
        self.write_db(self.db_report())
        self.write_us2(value)
        code, _ = self.export_db_us2()
        self.assertEqual(code, 1)
        text = json.dumps(self.read_us2_safe())
        self.assertNotIn("SECRET_CANARY", text)
        self.assertEqual(self.read_us2_safe()["status"], "error")

    def test_export_us2_absent_directive_stays_none(self):
        self.write_db(self.db_report())
        self.write_us2(self.us2_report())
        code, _ = self.export_db_us2()
        self.assertEqual(code, 0)
        safe = self.read_us2_safe()
        replay = safe["scenarios"][0]
        self.assertIsNone(replay["directive"])
        conflict = safe["scenarios"][3]
        self.assertIsNone(conflict["directive"])

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
            "expected_go_version": "go1.27.4",
            "go_version": "go1.27.4" if trusted else None,
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

    def test_export_default_accepts_test_supervision(self):
        for timeout_seconds in (570, 0.05):
            with self.subTest(timeout_seconds=timeout_seconds):
                value = self.go_default()
                value["test_supervision"] = {
                    "timeout_seconds": timeout_seconds,
                    "wait_elapsed_seconds": 0.01,
                    "timed_out": False,
                }
                self.write(self.report_dir / "go-default.json", value)
                code, _ = self.export()
                self.assertEqual(code, 0)
                safe = self.read_safe_default()
                self.assertEqual(safe["result"], "pass")
                self.assertEqual(
                    safe.get("test_supervision"),
                    {
                        "timeout_seconds": float(timeout_seconds),
                        "wait_elapsed_seconds": 0.01,
                        "timed_out": False,
                    },
                )

    def test_export_default_timeout_supervision_is_exported_not_green(self):
        value = self.go_default(result="fail")
        value["exit_code"] = 124
        value["timed_out"] = True
        value["problems"] = ["timeout"]
        value["test_supervision"] = {
            "timeout_seconds": 570,
            "wait_elapsed_seconds": 570.25,
            "timed_out": True,
        }
        self.write(self.report_dir / "go-default.json", value)
        code, _ = self.export()
        self.assertEqual(code, 1)
        safe = self.read_safe_default()
        self.assertEqual(safe["status"], "exported")
        self.assertEqual(safe["result"], "fail")
        self.assertEqual(
            safe.get("test_supervision"),
            {
                "timeout_seconds": 570.0,
                "wait_elapsed_seconds": 570.25,
                "timed_out": True,
            },
        )

    def test_export_default_historical_supervision_is_unknown(self):
        for mode in ("missing", "none"):
            with self.subTest(mode=mode):
                value = self.go_default()
                if mode == "none":
                    value["test_supervision"] = None
                self.write(self.report_dir / "go-default.json", value)
                code, _ = self.export()
                self.assertEqual(code, 0)
                safe = self.read_safe_default()
                self.assertEqual(safe["result"], "pass")
                self.assertIsNone(safe.get("test_supervision"))

    def test_export_default_rejects_malicious_test_supervision(self):
        base = {
            "timeout_seconds": 570,
            "wait_elapsed_seconds": 1.0,
            "timed_out": False,
        }
        cases = {
            "nan-timeout": {**base, "timeout_seconds": float("nan")},
            "inf-wait": {**base, "wait_elapsed_seconds": float("inf")},
            "negative-wait": {**base, "wait_elapsed_seconds": -1.0},
            "zero-timeout": {**base, "timeout_seconds": 0},
            "bool-timeout": {**base, "timeout_seconds": True},
            "bool-wait": {**base, "wait_elapsed_seconds": True},
            "int-timed-out": {**base, "timed_out": 1},
            "extra-raw-key": {**base, "events": ["SECRET_RAW_TIMING"]},
            "non-dict": ["SECRET_RAW_TIMING"],
        }
        for name, supervision in cases.items():
            with self.subTest(case=name):
                value = self.go_default()
                value["test_supervision"] = supervision
                self.write(self.report_dir / "go-default.json", value)
                code, _ = self.export()
                self.assertEqual(code, 1)
                text = (
                    self.report_dir / "upload" / "go-default.safe.json"
                ).read_text()
                safe = json.loads(text)
                self.assertEqual(safe["status"], "error")
                self.assertNotIn("SECRET_RAW_TIMING", text)

    def test_export_default_timed_out_supervision_cannot_override_result(self):
        cases = {
            "pass-with-timeout": {
                "result": "pass",
                "exit_code": 0,
                "timed_out": False,
            },
            "exit-zero-with-timeout": {
                "result": "fail",
                "exit_code": 0,
                "timed_out": False,
            },
        }
        for name, overrides in cases.items():
            with self.subTest(case=name):
                value = self.go_default()
                value.update(overrides)
                value["test_supervision"] = {
                    "timeout_seconds": 570,
                    "wait_elapsed_seconds": 570.0,
                    "timed_out": True,
                }
                self.write(self.report_dir / "go-default.json", value)
                code, _ = self.export()
                self.assertEqual(code, 1)
                safe = self.read_safe_default()
                self.assertEqual(safe["status"], "exported")
                self.assertEqual(safe["result"], "fail")
                self.assertTrue(
                    safe.get("test_supervision", {}).get("timed_out")
                )

    def test_export_default_test_supervision_does_not_grant_pass(self):
        value = self.go_default(result="fail")
        value["exit_code"] = 1
        value["problems"] = ["test-exit"]
        value["test_supervision"] = {
            "timeout_seconds": 570,
            "wait_elapsed_seconds": 1.0,
            "timed_out": False,
        }
        self.write(self.report_dir / "go-default.json", value)
        code, _ = self.export()
        self.assertEqual(code, 1)
        safe = self.read_safe_default()
        self.assertEqual(safe["result"], "fail")
        self.assertEqual(
            safe.get("test_supervision"),
            {
                "timeout_seconds": 570.0,
                "wait_elapsed_seconds": 1.0,
                "timed_out": False,
            },
        )

    def test_export_db_accepts_test_supervision(self):
        for timeout_seconds in (570, 0.05):
            with self.subTest(timeout_seconds=timeout_seconds):
                value = self.db_report()
                value["tests"]["test_supervision"] = {
                    "timeout_seconds": timeout_seconds,
                    "wait_elapsed_seconds": 0.01,
                    "timed_out": False,
                }
                self.write_db(value)
                code, _ = self.export(["db"])
                self.assertEqual(code, 0)
                safe = self.read_safe_db()
                self.assertEqual(safe["result"], "pass")
                self.assertEqual(
                    safe.get("test_supervision"),
                    {
                        "timeout_seconds": float(timeout_seconds),
                        "wait_elapsed_seconds": 0.01,
                        "timed_out": False,
                    },
                )

    def test_export_db_timeout_supervision_cannot_be_green(self):
        for test_exit in (0, 124):
            with self.subTest(test_exit=test_exit):
                value = self.db_report()
                value["tests"]["test_exit"] = test_exit
                value["tests"]["test_supervision"] = {
                    "timeout_seconds": 570,
                    "wait_elapsed_seconds": 570.0,
                    "timed_out": True,
                }
                self.write_db(value)
                code, _ = self.export(["db"])
                self.assertEqual(code, 1)
                safe = self.read_safe_db()
                self.assertEqual(safe["status"], "exported")
                self.assertEqual(safe["result"], "fail")
                self.assertTrue(
                    safe.get("test_supervision", {}).get("timed_out")
                )

    def test_export_db_historical_supervision_is_unknown(self):
        for mode in ("missing", "none"):
            with self.subTest(mode=mode):
                value = self.db_report()
                if mode == "none":
                    value["tests"]["test_supervision"] = None
                self.write_db(value)
                code, _ = self.export(["db"])
                self.assertEqual(code, 0)
                safe = self.read_safe_db()
                self.assertEqual(safe["result"], "pass")
                self.assertIsNone(safe.get("test_supervision"))

    def test_export_db_rejects_malicious_test_supervision(self):
        base = {
            "timeout_seconds": 570,
            "wait_elapsed_seconds": 1.0,
            "timed_out": False,
        }
        cases = {
            "nan-timeout": {**base, "timeout_seconds": float("nan")},
            "inf-wait": {**base, "wait_elapsed_seconds": float("inf")},
            "negative-wait": {**base, "wait_elapsed_seconds": -1.0},
            "zero-timeout": {**base, "timeout_seconds": 0},
            "bool-timeout": {**base, "timeout_seconds": True},
            "bool-wait": {**base, "wait_elapsed_seconds": True},
            "int-timed-out": {**base, "timed_out": 1},
            "extra-raw-key": {**base, "events": ["SECRET_RAW_TIMING"]},
            "non-dict": ["SECRET_RAW_TIMING"],
        }
        for name, supervision in cases.items():
            with self.subTest(case=name):
                value = self.db_report()
                value["tests"]["test_supervision"] = supervision
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
                self.assertNotIn("SECRET_RAW_TIMING", text)


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
