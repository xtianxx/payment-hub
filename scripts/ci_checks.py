#!/usr/bin/env python3
"""Strict CI helpers for go test -json and safe report export.

This module is deliberately standard-library only. It provides:

* ``parse_go_test_json``: a pure, strict parser for ``go test -json`` output
  that binds terminal events to the complete Package/Test/Action tuple.
* ``run-default``: one bounded default Go gate using a caller-supplied
  absolute Go executable and a fixed report filename.
* ``export-reports``: whitelist reconstruction of safe default/DB reports and
  a provenance manifest for CI upload.

Raw tool output, environment values, DSNs, SQL, stack traces, and diagnostics
text are never copied into the safe reports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent

GO_DEFAULT_SCHEMA = "payment-hub.ci-go-default.v1"
SAFE_EXPORT_SCHEMA = "payment-hub.ci-safe-export.v1"
DB_SCHEMA = "payment-hub.db-foundation.v1"
US1_SCHEMA = "payment-hub.us1.v1"
US2_SCHEMA = "payment-hub.us2.v1"
VULNERABILITY_SCHEMA = "payment-hub.ci.vulnerability-report.v1"
CANCELLATION_SCHEMA = "payment-hub.ci-cancellation.v1"
MANIFEST_VERIFICATION_UNCHANGED = "verified-unchanged"
MANIFEST_VERIFICATION_CHANGED = "verified-changed"
MANIFEST_VERIFICATION_NOT_VERIFIED = "not-verified"
VULNERABILITY_RESULTS = frozenset({"pass", "vulnerable", "failure"})
CANCELLATION_RESULTS = frozenset({"pass", "fail"})
CANCELLATION_CLEANUP_RESULTS = frozenset(
    {"pass", "fail", "skipped", "unknown"}
)
CANCELLATION_PASS_FAILURE = {"stage": "interrupted", "kind": "sigterm"}
EXPECTED_VULNERABILITY_GO = "go1.27.3"
GO_ID_RE = re.compile(r"^GO-[0-9]{4}-[0-9]{4,}$")
FAILURE_CLASS_RE = re.compile(r"^[a-z0-9_-]{1,80}$")
REPORT_FILENAMES = {
    "default": "go-default.json",
    "db": "db-foundation.json",
    "us1": "us1.json",
    "us2": "us2.json",
    "vulnerability": "vulnerability-report.json",
    "cancellation": "cancellation-report.json",
}
SAFE_FILENAMES = {
    "default": "go-default.safe.json",
    "db": "db-foundation.safe.json",
    "us1": "us1.safe.json",
    "us2": "us2.safe.json",
    "vulnerability": "vulnerability.safe.json",
    "cancellation": "cancellation.safe.json",
}
EXPECT_NAMES = (
    "default",
    "db",
    "us1",
    "us2",
    "vulnerability",
    "cancellation",
)
MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_LINE_BYTES = 1024 * 1024

# One shared whole-batch supervision budget for the default and integration
# Go test entry points. The 180s cold build + 360s execution + 30s headroom
# split is capacity planning only, not staged reservations. Kill/reap and
# report time after a supervision timeout are outside this wait.
GO_TEST_BATCH_TIMEOUT_SECONDS = 570

PACKAGE_STATUSES = frozenset(
    {"pass", "fail", "skip", "missing", "invalid", "no-test-files"}
)
US1_SCENARIO_NAMES = (
    "payment_success",
    "payment_failed",
    "invalid_requests",
    "restart_success",
    "restart_failed",
    "http_boundary",
)
US1_RESULTS = frozenset({"PASS", "FAIL", "NOT_RUN"})
US1_HTTP_RESULTS = frozenset({"SUCCEEDED", "FAILED", "UNKNOWN"})
US1_PG_STATES = ("ORDER", "MOCK", "TRACE")
US1_COMMAND_KINDS = frozenset({"build", "process_stop", "http_probe"})
US1_MAX_RECORDS = 4096
US1_MAX_GENERATIONS = 64
US1_EVENT_ID_RE = re.compile(r"^[0-9a-f]{32}:[1-9][0-9]*$")
US2_SCENARIO_NAMES = (
    "replay_success",
    "replay_failed",
    "replay_unknown",
    "conflict_priority",
    "concurrency",
    "restart_success",
    "restart_failed",
    "key_rotation",
)
# US2 keeps the US1 scenario-result enum. UNKNOWN is a payment/HTTP result,
# never a scenario execution conclusion.
US2_RESULTS = US1_RESULTS
US2_COUNT_KEYS = (
    "payment_calls",
    "query_calls",
    "order_rows",
    "mock_rows",
    "trace_rows",
)
US2_CHECKS_KEYS = (
    "counts_before",
    "counts_after",
    "stable_facts",
    "readonly_probes",
    "lookup_fault",
    "waiter_count",
    "mixed_waiter_count",
)
US2_LOOKUP_FAULTS = frozenset({"none", "timeout", "unavailable"})
US2_MAX_HTTP = 32
US2_MAX_PG = 16
US2_MAX_RECEIPTS = 8
US2_MAX_COMMANDS = 8
US2_SETUP_COUNTS = {
    "payment_calls": 1,
    "query_calls": 0,
    "order_rows": 1,
    "mock_rows": 1,
    "trace_rows": 1,
}
US2_REPLAY_UNKNOWN_COUNTS = {
    "payment_calls": 0,
    "query_calls": 0,
    "order_rows": 1,
    "mock_rows": 0,
    "trace_rows": 0,
}
US2_CONCURRENCY_COUNTS = dict(US2_SETUP_COUNTS)
US2_CONCURRENCY_EMPTY_COUNTS = {
    "payment_calls": 0,
    "query_calls": 0,
    "order_rows": 0,
    "mock_rows": 0,
    "trace_rows": 0,
}
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
REQUIRED_STATUSES = frozenset(
    {
        "pass",
        "fail",
        "skip",
        "missing",
        "duplicate",
        "wrong-package",
        "not-run",
        "invalid",
    }
)
PROBLEM_RE = re.compile(r"^[a-z0-9-]{1,80}$")
PACKAGE_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
TEST_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,1024}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
DURATION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?s$")
LOCATION_RE = re.compile(r"([A-Za-z0-9_./-]+\.go:[0-9]+)")
SAFE_LOCATION_RE = re.compile(r"^[A-Za-z0-9_]+\.go:[0-9]+$")
SAFE_ROOT_RE = re.compile(r"^Test[A-Za-z0-9_]+$")
REDACTED_TEST_RE = re.compile(
    r"^(?:Test[A-Za-z0-9_]+/)?redacted-[0-9a-f]{8}$"
)
SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9 .,:+/()_-]{1,160}$")
FAILURE_PART_RE = re.compile(r"^[a-z0-9-]{1,80}$")
TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
TOOL_MODULE_RE = re.compile(r"^[A-Za-z0-9._/-]{1,240}$")
SCAN_MODE_RE = re.compile(r"^[a-z0-9-]{0,80}$")
SCAN_TAGS_RE = re.compile(r"^[A-Za-z0-9,._-]{0,160}$")
SCAN_DB_RE = re.compile(r"^[A-Za-z0-9._:/+-]{0,160}$")
SCAN_PATTERN_RE = re.compile(r"^[A-Za-z0-9._/*-]{0,160}$")

ALLOWED_EVENT_KEYS = frozenset(
    {
        "Time",
        "Action",
        "Package",
        "Test",
        "Elapsed",
        "Output",
        "OutputType",
        "FailedBuild",
        "Key",
        "Value",
        "Path",
    }
)
OUTPUT_TYPES = frozenset({"", "frame", "error", "error-continue"})
ALLOWED_ACTIONS = frozenset(
    {
        "start",
        "run",
        "pause",
        "cont",
        "pass",
        "fail",
        "skip",
        "bench",
        "output",
        "attr",
        "artifacts",
    }
)
TERMINAL_ACTIONS = frozenset({"pass", "fail", "skip"})
TEST_RUN_ACTIONS = frozenset({"run"})
DB_REQUIRED_TEST_KEYS = (
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
)
DB_PACKAGE_KEYS = (
    "internal/postgres",
    "internal/mock",
    "tests/harness",
    "tests",
)

# Frozen default inventory: 38 required roots plus one focused subtest entry
# (TestPaymentRequestBoundary/POST_rejection_priority), i.e. 39 required
# mapping entries total. Roots cover the original 26 PR3 roots, first-batch
# US1 core roots, query/HTTP contract roots, payment-directive
# protocol/client/consumer roots, and runtime/router wiring. The eleven DB
# integration checks (including the explicit lifecycle subtest) are build-tagged
# integration and remain required in the DB launcher (REQUIRED_TEST_PACKAGES),
# not in the untagged default ./... gate.
DEFAULT_REQUIRED_TESTS = {
    "TestStartupDiagnosticsDatabaseOpenFailure": "payment-hub/internal/app",
    "TestStartupDiagnosticsPingRetryExhaustion": "payment-hub/internal/app",
    "TestStartupDiagnosticsMigrationAfterStartupExpiry": "payment-hub/internal/app",
    "TestStartupDiagnosticsTestModeFailure": "payment-hub/internal/app",
    "TestStartupDiagnosticsListenFailure": "payment-hub/internal/app",
    "TestStartupDiagnosticsStartupCancellation": "payment-hub/internal/app",
    "TestStartupDiagnosticsLateListenCancellation": "payment-hub/internal/app",
    "TestStartupDiagnosticsWriterBlockingIsBounded": "payment-hub/internal/app",
    "TestStartupDiagnosticsNormalPathUnchanged": "payment-hub/internal/app",
    "TestShutdownCleanupSealFailureStillClosesDatabase": "payment-hub/internal/app",
    "TestShutdownCleanupActiveDatabaseUserPreventsEarlyClose": "payment-hub/internal/app",
    "TestArtifactsFinishRejectsUnclosedObserverAfterHalfFrame": "payment-hub/tests/harness",
    "TestArtifactsFinishAcceptsClosedTrustedObserver": "payment-hub/tests/harness",
    "TestObserverFinalSnapshotUnavailableBeforeClose": "payment-hub/tests/harness",
    "TestObserverFinalSnapshotDeepCopyAndStable": "payment-hub/tests/harness",
    "TestObserverCloseHalfFrameFinalUntrusted": "payment-hub/tests/harness",
    "TestObserverCloseDoesNotSealOpenGeneration": "payment-hub/tests/harness",
    "TestObserverCloseDeadlineHasNoFinal": "payment-hub/tests/harness",
    "TestObserverCloseSocketCleanupFailureHasNoFinal": "payment-hub/tests/harness",
    "TestProcessReviewEmptyEnvDoesNotInheritHost": "payment-hub/tests/harness",
    "TestProcessReviewLeaderReapedStopHonorsDeadline": "payment-hub/tests/harness",
    "TestProcessReviewLeaderReapedWaitHonorsDeadline": "payment-hub/tests/harness",
    "TestProcessReviewLeaderReapedLifetimeCancel": "payment-hub/tests/harness",
    "TestProcessReviewTerminalPublishedAfterGroupWatchJoined": "payment-hub/tests/harness",
    "TestProcessReviewWatchGroupStopUnblocksFullEventChannel": "payment-hub/tests/harness",
    "TestProcessReviewHelper": "payment-hub/tests/harness",
    "TestSubmitPayment": "payment-hub/internal/payment",
    "TestEvidenceDecision": "payment-hub/internal/payment",
    "TestQueryPayment": "payment-hub/internal/payment",
    "TestPaymentRequestBoundary": "payment-hub/internal/httpapi",
    "TestPaymentRequestBoundary/POST_rejection_priority": "payment-hub/internal/httpapi",
    "TestPaymentResponseContract": "payment-hub/internal/httpapi",
    "TestPaymentDirectiveProtocol": "payment-hub/internal/testcontrol",
    "TestPaymentDirectiveClient": "payment-hub/internal/testcontrol",
    "TestObserverPaymentDirective": "payment-hub/tests/harness",
    "TestMockPaymentDirective": "payment-hub/internal/mock",
    "TestAppPaymentDirective": "payment-hub/internal/app",
    "TestPaymentRuntimeWiring": "payment-hub/internal/app",
    "TestPaymentRouterWiring": "payment-hub/internal/httpapi",
    "TestPaymentIdempotency": "payment-hub/internal/payment",
    "TestPaymentSubmitConflictPriority": "payment-hub/internal/payment",
    "TestPaymentRequestBoundary/POST_business_value_delegation": (
        "payment-hub/internal/httpapi"
    ),
}


class ProcResult(NamedTuple):
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    reaped: bool = True
    wait_elapsed_seconds: float | None = None


class CiError(RuntimeError):
    """A controlled CI helper failure with a fixed safe category."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_problem(value: Any) -> str:
    if not isinstance(value, str) or not PROBLEM_RE.fullmatch(value):
        raise CiError("unsafe-problem")
    return value


def _safe_location(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 256:
        raise CiError("unsafe-location")
    if value and not SAFE_LOCATION_RE.fullmatch(value):
        raise CiError("unsafe-location")
    return value


def _safe_export_diagnostic_package(
    value: Any, expected: set[str]
) -> str:
    if not isinstance(value, str):
        raise CiError("unsafe-field-diagnostic-package")
    if value in expected or value in {"unknown", ""}:
        return value
    raise CiError("unsafe-field-diagnostic-package")


def _safe_export_diagnostic_test(value: Any) -> str:
    if not isinstance(value, str):
        raise CiError("unsafe-field-diagnostic-test")
    if value == "":
        return ""
    if (
        value in DEFAULT_REQUIRED_TESTS
        or SAFE_ROOT_RE.fullmatch(value)
        or REDACTED_TEST_RE.fullmatch(value)
    ):
        return value
    raise CiError("unsafe-field-diagnostic-test")


def _safe_test_name(value: Any) -> str:
    if not isinstance(value, str) or not TEST_RE.fullmatch(value):
        raise CiError("unsafe-test-name")
    return value


def _safe_package(value: Any, *, module_only: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise CiError("unsafe-package")
    if not PACKAGE_RE.fullmatch(value):
        raise CiError("unsafe-package")
    if value.startswith("/") or "\\" in value:
        raise CiError("unsafe-package")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise CiError("unsafe-package")
    if module_only and value != "payment-hub" and not value.startswith(
        "payment-hub/"
    ):
        raise CiError("unsafe-package")
    return value


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise CiError("unsafe-relative-path")
    if value.startswith("/") or "\\" in value or "\x00" in value:
        raise CiError("unsafe-relative-path")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise CiError("unsafe-relative-path")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", value):
        raise CiError("unsafe-relative-path")
    return value


def _safe_version(value: Any) -> str:
    if not isinstance(value, str) or not SAFE_VERSION_RE.fullmatch(value):
        raise CiError("unsafe-version")
    return value


def _format_elapsed(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    if text in ("", "-0"):
        text = "0"
    return text + "s"


def _location(output: str) -> str:
    match = LOCATION_RE.search(output)
    if match is None:
        return ""
    return match.group(1)[:256]


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _validate_event(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(value, dict):
        return None, "malformed-event"
    if set(value) - ALLOWED_EVENT_KEYS:
        return None, "malformed-event"
    action = value.get("Action")
    if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        return None, "unknown-action"
    package = value.get("Package")
    if package is None:
        return None, "malformed-event"
    try:
        _safe_package(package)
    except CiError:
        return None, "malformed-event"
    test = value.get("Test")
    if test is not None:
        try:
            _safe_test_name(test)
        except CiError:
            return None, "malformed-event"
    if "OutputType" in value:
        output_type = value["OutputType"]
        if not isinstance(output_type, str):
            return None, "malformed-event"
        if output_type not in OUTPUT_TYPES:
            return None, "malformed-event"
        if action != "output":
            return None, "malformed-event"
    if "Elapsed" in value:
        elapsed = value["Elapsed"]
        if (
            isinstance(elapsed, bool)
            or not isinstance(elapsed, (int, float))
            or not math.isfinite(float(elapsed))
            or elapsed < 0
        ):
            return None, "malformed-event"
    for key in ("Time", "Output", "FailedBuild", "Key", "Value", "Path"):
        if key in value and not isinstance(value[key], str):
            return None, "malformed-event"
    if "Output" in value and len(value["Output"]) > MAX_OUTPUT_LINE_BYTES:
        return None, "malformed-event"
    if "Time" in value and len(value["Time"]) > 128:
        return None, "malformed-event"
    if "FailedBuild" in value and len(value["FailedBuild"]) > 512:
        return None, "malformed-event"
    for key in ("Key", "Value", "Path"):
        if key in value and len(value[key]) > 1024:
            return None, "malformed-event"
    if action == "start" and test is not None:
        return None, "malformed-event"
    if action in {"run", "pause", "cont", "bench"} and test is None:
        return None, "malformed-event"
    return value, None


def _diagnostic_package(package: str, expected: set[str]) -> str:
    if package in expected:
        return package
    return "unknown" if package else ""


def _diagnostic_test(test: str, required_names: set[str]) -> str:
    if not test:
        return ""
    if test in required_names:
        return test
    if SAFE_ROOT_RE.fullmatch(test):
        return test
    root = test.split("/", 1)[0]
    digest = sha256_bytes(test.encode("utf-8"))[:8]
    if SAFE_ROOT_RE.fullmatch(root):
        return root + "/redacted-" + digest
    return "redacted-" + digest


def _diagnostic_location(location: str) -> str:
    if not isinstance(location, str):
        return ""
    return location if SAFE_LOCATION_RE.fullmatch(location) else ""


def _append_diagnostic(
    diagnostics: list[dict[str, str]],
    package: str,
    test: str,
    category: str,
    location: str = "",
) -> None:
    if len(diagnostics) >= 20:
        return
    entry = {
        "package": package[:512],
        "test": test[:1024],
        "location": location[:256],
        "category": category[:80],
    }
    if entry not in diagnostics:
        diagnostics.append(entry)


def _no_test_files_marker(output: str, package: str) -> bool:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped == "?   \t" + package + "\t[no test files]":
            return True
        if stripped.startswith("?") and stripped.endswith("[no test files]"):
            fields = stripped.split()
            if len(fields) >= 3 and fields[1] == package:
                return True
    return False


def parse_go_test_json(
    stdout: str,
    stderr: str,
    exit_code: int,
    expected_packages: Iterable[str],
    required_tests: Mapping[str, str],
    allowed_no_test_files: Iterable[str] = (),
) -> dict[str, Any]:
    """Parse ``go test -json`` output with strict Package/Test/Action binding.

    Returns a safe dictionary with ``package_results``,
    ``required_test_results``, ``problems``, ``failure_diagnostics``,
    ``root_count`` and ``subtest_count``. Output text is never archived or
    used to synthesize test events.
    """
    problems: set[str] = set()
    diagnostics: list[dict[str, str]] = []
    expected = []
    for package in expected_packages:
        try:
            expected.append(_safe_package(package, module_only=True))
        except CiError:
            problems.add("invalid-expected-package")
    expected_set = set(expected)
    allowed_no_test = set()
    for package in allowed_no_test_files:
        try:
            allowed_no_test.add(_safe_package(package, module_only=True))
        except CiError:
            problems.add("invalid-allowed-no-test-package")

    required_names: set[str] = set()
    if isinstance(required_tests, Mapping):
        required_names = {
            name for name in required_tests if isinstance(name, str)
        }

    def add_diag(
        package: str,
        test: str,
        category: str,
        location: str = "",
    ) -> None:
        _append_diagnostic(
            diagnostics,
            _diagnostic_package(package, expected_set),
            _diagnostic_test(test, required_names),
            category,
            _diagnostic_location(location),
        )

    if not isinstance(stdout, str):
        problems.add("malformed-stdout")
        stdout = ""
    if not isinstance(stderr, str):
        problems.add("malformed-stderr")
        stderr = ""
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        problems.add("malformed-exit-code")
        exit_code = 1

    package_info: dict[str, dict[str, Any]] = {}
    all_test_terminals: dict[
        tuple[str, str], list[tuple[str, bool]]
    ] = {}
    all_test_runs: set[tuple[str, str]] = set()
    run_counts: dict[tuple[str, str], int] = {}
    locations: dict[tuple[str, str], str] = {}

    def info_for(package: str) -> dict[str, Any]:
        return package_info.setdefault(
            package,
            {
                "starts": 0,
                "terminals": [],
                "test_runs": set(),
                "test_terminals": {},
                "no_test_marker": False,
                "no_tests_marker": False,
            },
        )

    if stdout.strip() == "":
        problems.add("empty-output")
    else:
        if not stdout.endswith("\n"):
            problems.add("truncated-output")
        for raw_line in stdout.splitlines():
            if not raw_line.strip():
                problems.add("non-json-output")
                add_diag("", "", "protocol", "")
                continue
            if len(raw_line.encode("utf-8", errors="replace")) > MAX_OUTPUT_LINE_BYTES:
                problems.add("malformed-event")
                add_diag("", "", "protocol", "")
                continue
            try:
                event = json.loads(
                    raw_line, object_pairs_hook=_no_duplicate_keys
                )
            except (json.JSONDecodeError, ValueError):
                problems.add("non-json-output")
                add_diag("", "", "protocol", "")
                continue
            validated, error = _validate_event(event)
            if validated is None:
                problems.add(error or "malformed-event")
                add_diag("", "", "protocol", "")
                continue
            action = validated["Action"]
            package = validated["Package"]
            test = validated.get("Test")
            info = info_for(package)
            if package not in expected_set:
                problems.add("unexpected-package")
                add_diag(package, test or "", "unexpected-package", "")
            if action == "start":
                info["starts"] += 1
                continue
            if action == "output":
                output = validated.get("Output", "")
                if _no_test_files_marker(output, package):
                    info["no_test_marker"] = True
                if "no tests to run" in output:
                    info["no_tests_marker"] = True
                if test is not None:
                    location = _location(output)
                    if location and (package, test) not in locations:
                        locations[(package, test)] = location
                continue
            if action in TEST_RUN_ACTIONS:
                if test is None:
                    continue
                key = (package, test)
                run_counts[key] = run_counts.get(key, 0) + 1
                if run_counts[key] > 1:
                    problems.add("test-run-duplicate")
                    add_diag(
                        package,
                        test,
                        "test-run-duplicate",
                        locations.get(key, ""),
                    )
                all_test_runs.add(key)
                info["test_runs"].add(test)
                continue
            if action in TERMINAL_ACTIONS:
                elapsed = validated.get("Elapsed")
                if test is None:
                    info["terminals"].append((action, elapsed))
                else:
                    all_test_terminals.setdefault((package, test), []).append(
                        (action, (package, test) in all_test_runs)
                    )
                    info["test_terminals"].setdefault(test, []).append(action)
                continue
            # attr/artifacts are non-terminal metadata; never events.

    # Test-level completeness and non-required failures/skips.
    for (package, test), actions in sorted(all_test_terminals.items()):
        location = locations.get((package, test), "")
        if len(actions) > 1:
            problems.add("test-duplicate")
            add_diag(package, test, "test-duplicate", location)
        for action, had_run in actions:
            if action == "fail":
                problems.add("test-fail")
                problems.add("test-failure")
                add_diag(package, test, "test-fail", location)
            elif action == "skip":
                problems.add("test-skip")
                problems.add("skip")
                add_diag(package, test, "test-skip", location)
        if not all(had_run for _, had_run in actions):
            problems.add("test-not-run")
            add_diag(package, test, "test-not-run", location)
    for package, test in sorted(all_test_runs):
        if (package, test) not in all_test_terminals:
            problems.add("test-incomplete")
            add_diag(package,
                test,
                "test-incomplete",
                locations.get((package, test), ""),
            )

    package_results: dict[str, dict[str, Any]] = {}
    for package in expected:
        info = package_info.get(package)
        if info is None or not info["terminals"]:
            package_results[package] = {"status": "missing"}
            problems.add("package-missing")
            problems.add("package-not-pass")
            add_diag(package, "", "package-missing", "")
            continue
        if len(info["terminals"]) > 1:
            package_results[package] = {"status": "invalid"}
            problems.add("package-duplicate")
            problems.add("package-not-pass")
            add_diag(package, "", "package-duplicate", "")
            continue

        action, elapsed = info["terminals"][0]
        test_actions = [
            item
            for actions in info["test_terminals"].values()
            for item in actions
        ]
        has_tests = bool(info["test_runs"] or info["test_terminals"])
        if package in allowed_no_test and not has_tests:
            if action in {"skip", "pass"} and info["no_test_marker"]:
                status = "no-test-files"
            elif action == "fail":
                status = "fail"
                problems.add("package-fail")
                problems.add("package-not-pass")
                add_diag(package, "", "package-fail", "")
            else:
                status = "invalid"
                problems.add("package-no-test-files-unconfirmed")
                problems.add("package-not-pass")
                add_diag(package,
                    "",
                    "package-no-test-files-unconfirmed",
                    "",
                )
        elif action == "pass":
            if not info["test_runs"]:
                status = "invalid"
                problems.add("package-no-tests")
                problems.add("package-not-pass")
                add_diag(package, "", "package-no-tests", "")
            else:
                status = "pass"
        elif action == "fail":
            status = "fail"
            problems.add("package-fail")
            problems.add("package-not-pass")
            add_diag(package, "", "package-fail", "")
        elif action == "skip":
            status = "skip"
            problems.add("package-skip")
            problems.add("package-not-pass")
            if info["no_test_marker"]:
                problems.add("no-test-files")
            add_diag(package, "", "package-skip", "")
        else:
            status = "invalid"
            problems.add("package-not-pass")
            add_diag(package, "", "protocol", "")

        if info["no_tests_marker"]:
            problems.add("no-tests")

        # A package-level pass cannot hide a failed or skipped test.
        if any(item == "fail" for item in test_actions):
            status = "fail"
            problems.add("package-not-pass")
        elif any(item == "skip" for item in test_actions):
            status = "skip"
            problems.add("package-not-pass")
        elif any(
            len(info["test_terminals"].get(name, [])) > 1
            for name in info["test_terminals"]
        ):
            status = "invalid"
            problems.add("package-not-pass")
        if any(
            (package, test) in all_test_runs
            and (package, test) not in all_test_terminals
            for test in info["test_runs"]
        ):
            status = "invalid"
            problems.add("package-not-pass")
        if any(
            run_counts.get((package, name), 0) > 1
            for name in info["test_runs"]
        ):
            status = "invalid"
            problems.add("package-not-pass")

        result: dict[str, Any] = {"status": status}
        if elapsed is not None:
            result["duration"] = _format_elapsed(float(elapsed))
        package_results[package] = result

    required_results: dict[str, str] = {}
    if not isinstance(required_tests, Mapping):
        problems.add("invalid-required-tests")
        required_tests = {}
    for name, package in required_tests.items():
        try:
            test_name = _safe_test_name(name)
            package_name = _safe_package(package, module_only=True)
        except CiError:
            required_results[str(name)[:1024]] = "invalid"
            problems.add("invalid-required-test")
            continue
        if package_name not in expected_set:
            problems.add("required-package-not-expected")
        actions = all_test_terminals.get((package_name, test_name), [])
        if len(actions) > 1:
            required_results[test_name] = "duplicate"
            problems.add("required-test-duplicate")
            add_diag(package_name,
                test_name,
                "required-test-duplicate",
                locations.get((package_name, test_name), ""),
            )
            continue
        if run_counts.get((package_name, test_name), 0) > 1:
            required_results[test_name] = "duplicate"
            continue
        if len(actions) == 0:
            wrong_package = any(
                other_test == test_name and other_package != package_name
                for (other_package, other_test) in all_test_terminals
            )
            if wrong_package:
                required_results[test_name] = "wrong-package"
                problems.add("required-test-wrong-package")
                add_diag(package_name,
                    test_name,
                    "required-test-wrong-package",
                    "",
                )
            else:
                required_results[test_name] = "missing"
                problems.add("required-test-missing")
                add_diag(package_name,
                    test_name,
                    "required-test-missing",
                    "",
                )
        else:
            action, had_run = actions[0]
            if not had_run:
                required_results[test_name] = "not-run"
                problems.add("required-test-not-run")
                add_diag(package_name,
                    test_name,
                    "required-test-not-run",
                    locations.get((package_name, test_name), ""),
                )
            elif action == "pass":
                required_results[test_name] = "pass"
            elif action == "fail":
                required_results[test_name] = "fail"
                problems.add("required-test-fail")
            elif action == "skip":
                required_results[test_name] = "skip"
                problems.add("required-test-skip")
            else:
                required_results[test_name] = "invalid"
                problems.add("required-test-invalid")

    root_count = sum(
        1
        for (package, test) in all_test_terminals
        if package in expected_set and "/" not in test
    )
    subtest_count = sum(
        1
        for (package, test) in all_test_terminals
        if package in expected_set and "/" in test
    )

    if stderr.strip():
        problems.add("stderr-output")
    if exit_code != 0:
        problems.add("test-exit")

    return {
        "package_results": package_results,
        "required_test_results": required_results,
        "problems": sorted(problems),
        "failure_diagnostics": diagnostics[:20],
        "root_count": root_count,
        "subtest_count": subtest_count,
    }


def _isolated_go_env() -> tuple[dict[str, str], list[str]]:
    allowed = (
        "PATH",
        "HOME",
        "TMPDIR",
        "GOCACHE",
        "GOMODCACHE",
        "GOPATH",
        "GOROOT",
        "LANG",
        "LC_ALL",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
    )
    env = {
        key: os.environ[key]
        for key in allowed
        if key in os.environ
    }
    problems: list[str] = []
    for key in (
        "TMPDIR",
        "GOCACHE",
        "GOMODCACHE",
        "GOPATH",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
    ):
        value = env.get(key)
        if value and not os.path.isabs(value):
            problems.append(
                "env-relative-" + key.lower().replace("_", "-")
            )
    if not env.get("XDG_CONFIG_HOME"):
        problems.append("env-xdg-config-missing")
    for key in ("TMPDIR", "GOCACHE", "GOMODCACHE", "GOPATH"):
        if not env.get(key):
            problems.append("env-missing-" + key.lower().replace("_", "-"))
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.update(
        {
            "GOENV": "off",
            "GOFLAGS": "",
            "GOTOOLCHAIN": "local",
            "GOTELEMETRY": "off",
            "GOPROXY": "off",
            "GOSUMDB": "off",
            "CGO_ENABLED": "1",
            "GOMAXPROCS": "2",
            "GOWORK": "off",
        }
    )
    return env, sorted(set(problems))


def _terminate_owned_group(process: Any) -> tuple[bool, bytes, bytes]:
    """Best-effort kill/reap of the process group created by this call.

    This does not cover grandchildren that create their own process group or
    session, nor a runner hard-kill; it only prevents this call from leaving
    its own direct child group behind on controlled errors.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        return False, b"", b""
    except (OSError, ValueError):
        try:
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return False, b"", b""
        except BaseException:
            return False, b"", b""
        return process.poll() is not None, b"", b""
    except BaseException:
        return False, b"", b""
    return process.poll() is not None, stdout, stderr


def _run_process_group(
    argv: Sequence[str], env: Mapping[str, str], timeout: int
) -> ProcResult:
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(REPO_ROOT),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise CiError("process-start-failed") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Measure at the timeout instant, before kill/reap cleanup.
        wait_elapsed = time.monotonic() - started
        reaped, stdout, stderr = _terminate_owned_group(process)
        return ProcResult(124, stdout, stderr, True, reaped, wait_elapsed)
    except OSError as exc:
        reaped, _, _ = _terminate_owned_group(process)
        category = (
            "process-io-failed"
            if reaped
            else "process-io-failed-reap-unconfirmed"
        )
        raise CiError(category) from exc
    except BaseException as exc:
        reaped, _, _ = _terminate_owned_group(process)
        category = (
            "process-interrupted"
            if reaped
            else "process-interrupted-reap-unconfirmed"
        )
        raise CiError(category) from exc
    wait_elapsed = time.monotonic() - started
    return ProcResult(
        process.returncode,
        stdout,
        stderr,
        False,
        process.poll() is not None,
        wait_elapsed,
    )


def _decode(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CiError("non-utf8-output") from exc


def _parse_go_list(stdout: str) -> list[str]:
    packages: list[str] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        package = _safe_package(candidate, module_only=True)
        if package in seen:
            raise CiError("go-list-duplicate-package")
        seen.add(package)
        packages.append(package)
    if not packages:
        raise CiError("go-list-empty")
    return sorted(packages)


def _input_hash(
    go_name: str,
    list_argv: Sequence[str],
    test_argv: Sequence[str],
) -> str:
    payload = {
        "go_name": go_name,
        "list_argv": list(list_argv),
        "test_argv": list(test_argv),
        "required_tests": DEFAULT_REQUIRED_TESTS,
    }
    return sha256_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def _write_json_report(report_dir: Path, filename: str, value: Any) -> Path:
    target = report_dir / filename
    if target.is_symlink():
        raise CiError("unsafe-report-path")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=str(report_dir),
            prefix=".ci-report-",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, target)
    except (OSError, TypeError, ValueError) as exc:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise CiError("report-write-failed") from exc
    return target


def _base_go_report(go: str) -> dict[str, Any]:
    return {
        "schema": GO_DEFAULT_SCHEMA,
        "generated_at": utc_now(),
        "result": "fail",
        "go": {
            "name": Path(go).name
            if isinstance(go, str) and go
            else ""
        },
        "input_hash": sha256_bytes(
            json.dumps(
                {
                    "schema": GO_DEFAULT_SCHEMA,
                    "required_tests": DEFAULT_REQUIRED_TESTS,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
        "expected_packages": [],
        "package_results": {},
        "required_test_results": {
            name: "missing" for name in DEFAULT_REQUIRED_TESTS
        },
        "root_count": 0,
        "subtest_count": 0,
        "problems": [],
        "failure_diagnostics": [],
        "exit_code": 1,
        "list_exit_code": 1,
        "timed_out": False,
    }


def _finish_go_report(
    report_dir: Path, report: dict[str, Any]
) -> int:
    report["problems"] = sorted(set(report["problems"]))
    report["result"] = (
        "pass"
        if report["result"] == "pass"
        and not report["problems"]
        and report["exit_code"] == 0
        and report["list_exit_code"] == 0
        and not report["timed_out"]
        else "fail"
    )
    try:
        path = _write_json_report(report_dir, "go-default.json", report)
    except CiError as exc:
        print(f"ERROR: {exc.category}", file=sys.stderr)
        print("result: fail stage=run-default kind=report-write")
        return 1
    print(f"go-default-report: {path}")
    print(f"result: {report['result']}")
    if report["problems"]:
        print("go-default-problems: " + ",".join(report["problems"]))
    return 0 if report["result"] == "pass" else 1


def run_default(go: str, report_dir: str) -> int:
    """Run one bounded default Go gate and write a safe go-default.json."""
    report_dir_path = Path(report_dir)
    if not report_dir_path.is_absolute():
        print("ERROR: report-dir/relative", file=sys.stderr)
        print("result: fail stage=run-default kind=report-dir")
        return 1
    try:
        report_dir_path.mkdir(parents=True, exist_ok=True)
    except OSError:
        print("ERROR: report-dir/create", file=sys.stderr)
        print("result: fail stage=run-default kind=report-dir")
        return 1
    if report_dir_path.is_symlink() or not report_dir_path.is_dir():
        print("ERROR: report-dir/unsafe", file=sys.stderr)
        print("result: fail stage=run-default kind=report-dir")
        return 1

    report = _base_go_report(go)
    problems: list[str] = []

    if not isinstance(go, str) or not os.path.isabs(go):
        problems.append("go-not-absolute")
        report["problems"] = problems
        return _finish_go_report(report_dir_path, report)
    go_path = Path(go)
    if not go_path.is_file() or not os.access(go_path, os.X_OK):
        problems.append("go-not-executable")
        report["problems"] = problems
        return _finish_go_report(report_dir_path, report)
    try:
        report["go"]["sha256"] = sha256_file(go_path)
    except OSError:
        pass

    env, env_problems = _isolated_go_env()
    if env_problems:
        problems.extend(env_problems)
        report["problems"] = problems
        return _finish_go_report(report_dir_path, report)
    list_argv = [go, "list", "-mod=readonly", "./..."]
    test_argv = [
        go,
        "test",
        "-mod=readonly",
        "-race",
        "-count=1",
        "-p=1",
        "-timeout=90s",
        "-json",
        "./...",
    ]
    report["command"] = {"list": list_argv, "test": test_argv}
    report["input_hash"] = _input_hash(go_path.name, list_argv, test_argv)

    try:
        listed = _run_process_group(list_argv, env, 60)
    except CiError as exc:
        problems.append("go-list-" + exc.category)
        report["problems"] = problems
        return _finish_go_report(report_dir_path, report)
    report["list_exit_code"] = listed.exit_code
    if listed.timed_out:
        problems.append("go-list-timeout")
    if not listed.reaped:
        problems.append("process-reap-unconfirmed")
    if listed.exit_code != 0:
        problems.append("go-list-exit")
    try:
        list_stdout = _decode(listed.stdout)
        list_stderr = _decode(listed.stderr)
    except CiError as exc:
        problems.append("go-list-" + exc.category)
        report["problems"] = problems
        return _finish_go_report(report_dir_path, report)
    if list_stderr.strip():
        problems.append("go-list-stderr")
    if problems:
        report["problems"] = problems
        return _finish_go_report(report_dir_path, report)
    try:
        expected = _parse_go_list(list_stdout)
    except CiError as exc:
        problems.append("go-list-" + exc.category)
        report["problems"] = problems
        return _finish_go_report(report_dir_path, report)
    report["expected_packages"] = expected
    allowed_no_test = tuple(
        package
        for package in expected
        if package == "payment-hub/cmd/payment-hub"
    )

    try:
        tested = _run_process_group(
            test_argv, env, GO_TEST_BATCH_TIMEOUT_SECONDS
        )
    except CiError as exc:
        problems.append("go-test-" + exc.category)
        report["problems"] = problems
        return _finish_go_report(report_dir_path, report)
    wait_elapsed = getattr(tested, "wait_elapsed_seconds", None)
    if wait_elapsed is not None and (
        isinstance(wait_elapsed, bool)
        or not isinstance(wait_elapsed, (int, float))
        or not math.isfinite(float(wait_elapsed))
        or wait_elapsed < 0
    ):
        wait_elapsed = None
    report["test_supervision"] = {
        "timeout_seconds": GO_TEST_BATCH_TIMEOUT_SECONDS,
        "wait_elapsed_seconds": (
            None if wait_elapsed is None else float(wait_elapsed)
        ),
        "timed_out": tested.timed_out,
    }
    test_exit = 124 if tested.timed_out else tested.exit_code
    report["exit_code"] = test_exit
    report["timed_out"] = tested.timed_out
    try:
        test_stdout = _decode(tested.stdout)
        test_stderr = _decode(tested.stderr)
    except CiError as exc:
        problems.append(exc.category)
        parsed = parse_go_test_json(
            "",
            "",
            test_exit,
            expected,
            DEFAULT_REQUIRED_TESTS,
            allowed_no_test,
        )
    else:
        parsed = parse_go_test_json(
            test_stdout,
            test_stderr,
            test_exit,
            expected,
            DEFAULT_REQUIRED_TESTS,
            allowed_no_test,
        )
    if tested.timed_out:
        problems.append("timeout")
    if not tested.reaped:
        problems.append("process-reap-unconfirmed")
    report["package_results"] = parsed["package_results"]
    report["required_test_results"] = parsed["required_test_results"]
    report["root_count"] = parsed["root_count"]
    report["subtest_count"] = parsed["subtest_count"]
    report["failure_diagnostics"] = parsed["failure_diagnostics"]
    problems.extend(parsed["problems"])
    report["problems"] = problems
    report["result"] = (
        "pass"
        if not problems and test_exit == 0 and not tested.timed_out
        else "fail"
    )
    return _finish_go_report(report_dir_path, report)


def _read_source_json(
    path: Path, *, expected_parent: Path | None = None
) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise CiError("unsafe-source-path")
    if expected_parent is not None:
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise CiError("unsafe-source-path") from exc
        if resolved.parent != expected_parent.resolve():
            raise CiError("unsafe-source-path")
    try:
        if path.stat().st_size > MAX_REPORT_BYTES:
            raise CiError("source-too-large")
        raw = path.read_bytes()
    except OSError as exc:
        raise CiError("source-read-failed") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CiError("invalid-source-json") from exc
    if not isinstance(value, dict):
        raise CiError("invalid-source-json")
    return value, sha256_bytes(raw)


def _require_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CiError("unsafe-field-" + name)
    return value


def _require_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise CiError("unsafe-field-" + name)
    return value


def _require_str(value: Any, name: str, max_length: int = 160) -> str:
    if not isinstance(value, str) or len(value) > max_length:
        raise CiError("unsafe-field-" + name)
    return value


def _require_timestamp(value: Any, name: str) -> str:
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        raise CiError("unsafe-field-" + name)
    return value


def _require_failure_part(value: Any, name: str) -> str:
    if not isinstance(value, str) or not FAILURE_PART_RE.fullmatch(value):
        raise CiError("unsafe-field-" + name)
    return value


def _require_pattern(regex: re.Pattern[str], value: Any, name: str) -> str:
    if not isinstance(value, str) or not regex.fullmatch(value):
        raise CiError("unsafe-field-" + name)
    return value


def _require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise CiError("unsafe-field-" + name)
    return value


def _require_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CiError("unsafe-field-" + name)
    return value


def _safe_status(value: Any, allowed: frozenset[str], name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise CiError("unsafe-field-" + name)
    return value


def _safe_duration(value: Any, name: str) -> str:
    if not isinstance(value, str) or not DURATION_RE.fullmatch(value):
        raise CiError("unsafe-field-" + name)
    return value


def _require_number(value: Any, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
    ):
        raise CiError("unsafe-field-" + name)
    return float(value)


def _safe_float_or_none(value: Any, name: str) -> float | None:
    if value is None:
        return None
    return round(_require_number(value, name), 3)


TEST_SUPERVISION_KEYS = frozenset(
    {"timeout_seconds", "wait_elapsed_seconds", "timed_out"}
)


def _safe_test_supervision(value: Any) -> dict[str, Any] | None:
    """Rebuild the optional timing object; None means not measured.

    Timing is informational only. It never grants pass status, and a
    contradictory timed_out value is handled by the caller as non-green.
    """
    if value is None:
        return None
    entry = _require_dict(value, "test_supervision")
    if set(entry) != TEST_SUPERVISION_KEYS:
        raise CiError("unsafe-field-test_supervision")
    timeout_seconds = _require_number(
        entry["timeout_seconds"], "test_supervision.timeout_seconds"
    )
    if timeout_seconds <= 0:
        raise CiError("unsafe-field-test_supervision.timeout_seconds")
    wait_elapsed = _safe_float_or_none(
        entry["wait_elapsed_seconds"],
        "test_supervision.wait_elapsed_seconds",
    )
    timed_out = _require_bool(
        entry["timed_out"], "test_supervision.timed_out"
    )
    return {
        "timeout_seconds": timeout_seconds,
        "wait_elapsed_seconds": wait_elapsed,
        "timed_out": timed_out,
    }


def _safe_int_or_none(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return _require_int(value, name)


def _safe_go_ids(value: Any, name: str) -> list[str]:
    items = _require_list(value, name)
    result: list[str] = []
    for item in items:
        if not isinstance(item, str) or not GO_ID_RE.fullmatch(item):
            raise CiError("unsafe-field-" + name)
        result.append(item)
    if result != sorted(set(result)):
        raise CiError("unsafe-field-" + name + ".order")
    return result


def _safe_manifest(
    source: dict[str, Any],
    *,
    require_verified: bool,
) -> dict[str, Any]:
    manifest = _require_list(source.get("manifest"), "source.manifest")
    if not manifest:
        raise CiError("unsafe-field-source.manifest")
    files: list[dict[str, str]] = []
    digest = hashlib.sha256()
    previous = ""
    for entry in manifest:
        entry = _require_dict(entry, "source.manifest.entry")
        path = _safe_relative_path(entry.get("path"))
        file_hash = entry.get("sha256")
        if not isinstance(file_hash, str) or not HEX64_RE.fullmatch(file_hash):
            raise CiError("unsafe-field-source.manifest.sha256")
        if path <= previous:
            raise CiError("unsafe-field-source.manifest.order")
        previous = path
        digest.update(path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(file_hash.encode("utf-8"))
        digest.update(b"\x00")
        files.append({"path": path, "sha256": file_hash})
    declared = source.get("manifest_digest")
    if not isinstance(declared, str) or not HEX64_RE.fullmatch(declared):
        raise CiError("unsafe-field-source.manifest_digest")
    if declared != digest.hexdigest():
        raise CiError("source-manifest-mismatch")
    unchanged_value = source.get("manifest_unchanged")
    if isinstance(unchanged_value, bool):
        unchanged: bool | None = unchanged_value
        verification = (
            MANIFEST_VERIFICATION_UNCHANGED
            if unchanged_value
            else MANIFEST_VERIFICATION_CHANGED
        )
    elif unchanged_value is None:
        if require_verified:
            raise CiError("unsafe-field-source.manifest_unchanged")
        unchanged = None
        verification = MANIFEST_VERIFICATION_NOT_VERIFIED
    else:
        raise CiError("unsafe-field-source.manifest_unchanged")
    if require_verified and unchanged is not True:
        raise CiError("unsafe-field-source.manifest_unchanged")
    return {
        "manifest_digest": declared,
        "manifest_unchanged": unchanged,
        "manifest_verification": verification,
        "file_count": len(files),
        "files": files,
    }


def _safe_default_report(
    source: dict[str, Any],
) -> dict[str, Any]:
    if source.get("schema") != GO_DEFAULT_SCHEMA:
        raise CiError("unsupported-report-schema")
    source_result = source.get("result")
    expected_value = source.get("expected_packages")
    if not isinstance(expected_value, list):
        if source_result == "pass":
            raise CiError("unsafe-field-expected_packages")
        expected_value = []
    expected = [
        _safe_package(value, module_only=True)
        for value in expected_value
    ]
    if source_result == "pass" and not expected:
        raise CiError("unsafe-field-expected_packages")
    if len(set(expected)) != len(expected):
        raise CiError("unsafe-field-expected_packages")
    package_value = source.get("package_results")
    if not isinstance(package_value, dict):
        if source_result == "pass":
            raise CiError("unsafe-field-package_results")
        package_value = {}
    package_results_source = package_value
    package_results: dict[str, dict[str, Any]] = {}
    for name, value in package_results_source.items():
        package = _safe_package(name, module_only=True)
        entry = _require_dict(value, "package_results.entry")
        status = _safe_status(
            entry.get("status"), PACKAGE_STATUSES, "package_results.status"
        )
        safe_entry: dict[str, Any] = {"status": status}
        if "duration" in entry:
            safe_entry["duration"] = _safe_duration(
                entry["duration"], "package_results.duration"
            )
        package_results[package] = safe_entry
    if not set(package_results) <= set(expected):
        raise CiError("unsafe-field-package_results")
    required_value = source.get("required_test_results")
    if not isinstance(required_value, dict):
        if source_result == "pass":
            raise CiError("unsafe-field-required_test_results")
        required_value = {}
    required_source = required_value
    required_results: dict[str, str] = {}
    for name, value in required_source.items():
        test_name = _safe_test_name(name)
        if test_name not in DEFAULT_REQUIRED_TESTS:
            raise CiError("unsafe-field-required_test_results")
        required_results[test_name] = _safe_status(
            value, REQUIRED_STATUSES, "required_test_results.status"
        )
    if source_result == "pass" and set(required_results) != set(
        DEFAULT_REQUIRED_TESTS
    ):
        raise CiError("unsafe-field-required_test_results")
    problems = [
        _safe_problem(value)
        for value in _require_list(source.get("problems"), "problems")
    ]
    diagnostics: list[dict[str, str]] = []
    for value in _require_list(
        source.get("failure_diagnostics"), "failure_diagnostics"
    ):
        entry = _require_dict(value, "failure_diagnostics.entry")
        diagnostics.append(
            {
                "package": _safe_export_diagnostic_package(
                    entry.get("package", ""), set(expected)
                ),
                "test": _safe_export_diagnostic_test(
                    entry.get("test", "")
                ),
                "location": _safe_location(
                    entry.get("location", "")
                ),
                "category": _safe_problem(
                    entry.get("category", "")
                ),
            }
        )
    exit_code = _require_int(source.get("exit_code"), "exit_code")
    list_exit_code = _require_int(
        source.get("list_exit_code"), "list_exit_code"
    )
    timed_out = _require_bool(source.get("timed_out"), "timed_out")
    test_supervision = _safe_test_supervision(
        source.get("test_supervision")
    )
    root_count = _require_int(source.get("root_count"), "root_count")
    subtest_count = _require_int(
        source.get("subtest_count"), "subtest_count"
    )
    if root_count < 0 or subtest_count < 0:
        raise CiError("unsafe-field-root_count")
    input_hash = source.get("input_hash")
    if not isinstance(input_hash, str) or not HEX64_RE.fullmatch(input_hash):
        raise CiError("unsafe-field-input_hash")

    safe_result = "pass"
    if (
        source.get("result") != "pass"
        or problems
        or diagnostics
        or exit_code != 0
        or list_exit_code != 0
        or timed_out
        or root_count < 1
    ):
        safe_result = "fail"
    if set(required_results) != set(DEFAULT_REQUIRED_TESTS):
        safe_result = "fail"
    if any(value != "pass" for value in required_results.values()):
        safe_result = "fail"
    if any(
        package_results.get(package, {}).get("status")
        not in {"pass", "no-test-files"}
        for package in expected
    ):
        safe_result = "fail"
    if test_supervision is not None:
        if test_supervision["timed_out"] != timed_out:
            safe_result = "fail"
        if test_supervision["timed_out"] and (
            source.get("result") == "pass" or exit_code == 0
        ):
            safe_result = "fail"
    return {
        "schema": "payment-hub.ci-safe.go-default.v1",
        "status": "exported",
        "source_schema": GO_DEFAULT_SCHEMA,
        "generated_at": utc_now(),
        "result": safe_result,
        "expected_packages": expected,
        "package_results": package_results,
        "required_test_results": required_results,
        "root_count": root_count,
        "subtest_count": subtest_count,
        "problems": problems,
        "failure_diagnostics": diagnostics,
        "exit_code": exit_code,
        "list_exit_code": list_exit_code,
        "timed_out": timed_out,
        "test_supervision": test_supervision,
        "input_hash": input_hash,
    }


def _find_db_report(db_root: Path) -> Path | None:
    if not db_root.exists():
        return None
    if db_root.is_symlink() or not db_root.is_dir():
        raise CiError("unsafe-db-artifact-root")
    candidates: list[Path] = []
    for child in sorted(db_root.iterdir()):
        if child.is_symlink():
            raise CiError("unsafe-source-path")
        if not child.is_dir():
            continue
        candidate = child / "db-foundation.json"
        if candidate.is_file():
            candidates.append(candidate)
    if not candidates:
        return None
    if len(candidates) > 1:
        raise CiError("db-run-ambiguous")
    return candidates[0]


def _safe_db_report(source: dict[str, Any]) -> dict[str, Any]:
    if source.get("schema") != DB_SCHEMA:
        raise CiError("unsupported-report-schema")
    run_id = source.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise CiError("unsafe-field-run_id")
    project = source.get("project")
    if project != "payment-hub-test-db-" + run_id:
        raise CiError("unsafe-field-project")
    platform = source.get("platform")
    if not isinstance(platform, str) or not re.fullmatch(
        r"[a-z0-9]+/[a-z0-9]+", platform
    ):
        raise CiError("unsafe-field-platform")
    started_at = _require_timestamp(source.get("started_at"), "started_at")
    finished_at = _require_timestamp(source.get("finished_at"), "finished_at")
    source_result = source.get("result")
    source_value = source.get("source")
    if isinstance(source_value, dict) and source_value:
        source_safe = _safe_manifest(
            source_value, require_verified=source_result == "pass"
        )
    elif source_result == "pass":
        raise CiError("unsafe-field-source")
    else:
        source_safe = {
            "manifest_digest": None,
            "manifest_unchanged": None,
            "manifest_verification": MANIFEST_VERIFICATION_NOT_VERIFIED,
            "file_count": 0,
            "files": [],
        }
    tests_value = source.get("tests")
    if not isinstance(tests_value, dict):
        if source_result == "pass":
            raise CiError("unsafe-field-tests")
        tests_value = {}
    required_source = tests_value.get("required_test_results")
    if not isinstance(required_source, dict):
        if source_result == "pass":
            raise CiError("unsafe-field-required_test_results")
        required_source = {}
    if not set(required_source) <= set(DB_REQUIRED_TEST_KEYS):
        raise CiError("unsafe-field-required_test_results")
    if source_result == "pass" and set(required_source) != set(
        DB_REQUIRED_TEST_KEYS
    ):
        raise CiError("unsafe-field-required_test_results")
    required_results = {
        name: _safe_status(
            required_source.get(name, "missing"),
            REQUIRED_STATUSES,
            "required_test_results.status",
        )
        for name in DB_REQUIRED_TEST_KEYS
    }
    package_source = tests_value.get("package_results")
    if not isinstance(package_source, dict):
        if source_result == "pass":
            raise CiError("unsafe-field-package_results")
        package_source = {}
    if not set(package_source) <= set(DB_PACKAGE_KEYS):
        raise CiError("unsafe-field-package_results")
    if source_result == "pass" and set(package_source) != set(
        DB_PACKAGE_KEYS
    ):
        raise CiError("unsafe-field-package_results")
    package_results: dict[str, dict[str, Any]] = {}
    for name in DB_PACKAGE_KEYS:
        if name not in package_source:
            continue
        entry = _require_dict(package_source[name], "package_results.entry")
        status = _safe_status(
            entry.get("status"), PACKAGE_STATUSES, "package_results.status"
        )
        safe_entry: dict[str, Any] = {"status": status}
        if "duration" in entry:
            safe_entry["duration"] = _safe_duration(
                entry["duration"], "package_results.duration"
            )
        package_results[name] = safe_entry

    vet_exit = _safe_int_or_none(
        tests_value.get("vet_exit"), "tests.vet_exit"
    )
    test_exit = _safe_int_or_none(
        tests_value.get("test_exit"), "tests.test_exit"
    )
    test_supervision = _safe_test_supervision(
        tests_value.get("test_supervision")
    )
    problems_value = tests_value.get("problems")
    if problems_value is None:
        test_problems: list[str] = []
    elif isinstance(problems_value, list):
        test_problems = sorted(
            {_safe_problem(value) for value in problems_value}
        )
    else:
        raise CiError("unsafe-field-tests.problems")

    cleanup = _require_dict(source.get("cleanup"), "cleanup")
    cleanup_result = _safe_status(
        cleanup.get("result"), frozenset({"pass", "fail"}), "cleanup.result"
    )
    errors = []
    for value in _require_list(cleanup.get("errors"), "cleanup.errors"):
        if not isinstance(value, str) or not re.fullmatch(
            r"[a-z0-9-]{1,80}:[a-z0-9-]{1,80}", value
        ):
            raise CiError("unsafe-field-cleanup.errors")
        errors.append(value)
    cleanup_safe = {
        "result": cleanup_result,
        "errors": errors,
        "repeat_signal_ignored": _require_bool(
            cleanup.get("repeat_signal_ignored", False),
            "cleanup.repeat_signal_ignored",
        ),
        "network_removed": _require_bool(
            cleanup.get("network_removed", False), "cleanup.network_removed"
        ),
        "volume_removed": _require_bool(
            cleanup.get("volume_removed", False), "cleanup.volume_removed"
        ),
        "volume_retained": _require_bool(
            cleanup.get("volume_retained", False), "cleanup.volume_retained"
        ),
        "compose_down_exit": _safe_int_or_none(
            cleanup.get("compose_down_exit"), "cleanup.compose_down_exit"
        ),
    }
    if cleanup_result == "pass" and errors:
        raise CiError("cleanup-inconsistent")

    versions_source = _require_dict(source.get("versions"), "versions")
    versions: dict[str, str] = {}
    for name in ("go", "postgres", "goose", "docker", "compose"):
        if name in versions_source:
            versions[name] = _safe_version(versions_source[name])

    failure = source.get("failure")
    failure_safe = None
    if failure is not None:
        failure_dict = _require_dict(failure, "failure")
        failure_safe = {
            "stage": _require_failure_part(
                failure_dict.get("stage"), "failure.stage"
            ),
            "kind": _require_failure_part(
                failure_dict.get("kind"), "failure.kind"
            ),
        }

    safe_result = "pass"
    if source.get("result") != "pass":
        safe_result = "fail"
    if any(value != "pass" for value in required_results.values()):
        safe_result = "fail"
    if any(
        value["status"] != "pass" for value in package_results.values()
    ):
        safe_result = "fail"
    if cleanup_result != "pass" or errors:
        safe_result = "fail"
    if vet_exit != 0 or test_exit != 0 or test_problems:
        safe_result = "fail"
    if (
        not cleanup_safe["network_removed"]
        or not cleanup_safe["volume_removed"]
        or cleanup_safe["volume_retained"]
        or cleanup_safe["compose_down_exit"] != 0
    ):
        safe_result = "fail"
    if failure_safe is not None:
        safe_result = "fail"
    if not source_safe["manifest_unchanged"]:
        safe_result = "fail"
    if test_supervision is not None:
        if test_supervision["timed_out"] and (
            source.get("result") == "pass" or test_exit == 0
        ):
            safe_result = "fail"

    return {
        "schema": "payment-hub.ci-safe.db-foundation.v1",
        "status": "exported",
        "source_schema": DB_SCHEMA,
        "generated_at": utc_now(),
        "run_id": run_id,
        "project": project,
        "platform": platform,
        "started_at": started_at,
        "finished_at": finished_at,
        "result": safe_result,
        "failure": failure_safe,
        "source": source_safe,
        "vet_exit": vet_exit,
        "test_exit": test_exit,
        "test_supervision": test_supervision,
        "problems": test_problems,
        "required_test_results": required_results,
        "package_results": package_results,
        "cleanup": cleanup_safe,
        "versions": versions,
    }


def _us1_exact_keys(value: Any, keys: Sequence[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise CiError("unsafe-field-us1." + name)
    return value


def _us1_int(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CiError("unsafe-field-us1." + name)
    return value


def _us1_uuid(value: Any, name: str, nullable: bool = False) -> str | None:
    if value in (None, "") and nullable:
        return value
    if not isinstance(value, str) or not UUID_RE.fullmatch(value):
        raise CiError("unsafe-field-us1." + name)
    return value


def _us1_hex32(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise CiError("unsafe-field-us1." + name)
    return value


def _us1_event_id(value: Any, name: str, call_seq: int) -> str:
    if (
        not isinstance(value, str)
        or not US1_EVENT_ID_RE.fullmatch(value)
        or call_seq > US1_MAX_RECORDS
    ):
        raise CiError("unsafe-field-us1." + name)
    suffix = int(value.rsplit(":", 1)[1])
    if suffix != call_seq:
        raise CiError("unsafe-field-us1." + name)
    return value


def _us1_run_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise CiError("unsafe-field-us1." + name)
    return value


def _us1_scenario(value: Any) -> dict[str, Any]:
    scenario = _us1_exact_keys(
        value,
        (
            "name",
            "result",
            "generations",
            "http",
            "pg",
            "observer",
            "directive",
            "commands",
        ),
        "scenario",
    )
    name = scenario["name"]
    if name not in US1_SCENARIO_NAMES:
        raise CiError("unsafe-field-us1.scenario.name")
    result = scenario["result"]
    if result not in US1_RESULTS:
        raise CiError("unsafe-field-us1.scenario.result")
    generations_value = _require_list(
        scenario["generations"], "us1.scenario.generations"
    )
    if len(generations_value) > 2:
        raise CiError("unsafe-field-us1.scenario.generations")
    generations = [
        _us1_int(item, "scenario.generations", minimum=1)
        for item in generations_value
    ]
    if any(
        item not in (1, 2) or item > US1_MAX_GENERATIONS
        for item in generations
    ):
        raise CiError("unsafe-field-us1.scenario.generations")
    if len(set(generations)) != len(generations):
        raise CiError("unsafe-field-us1.scenario.generations")

    http_value = _require_list(scenario["http"], "us1.scenario.http")
    if len(http_value) > 32:
        raise CiError("unsafe-field-us1.scenario.http")
    http: list[dict[str, Any]] = []
    for entry in http_value:
        item = _us1_exact_keys(
            entry,
            ("status", "request_id", "payment_id", "result"),
            "scenario.http",
        )
        status = _us1_int(item["status"], "scenario.http.status", minimum=100)
        if status > 599:
            raise CiError("unsafe-field-us1.scenario.http.status")
        request_id = _us1_uuid(
            item["request_id"], "scenario.http.request_id", nullable=True
        )
        payment_id = _us1_uuid(
            item["payment_id"], "scenario.http.payment_id", nullable=True
        )
        http_result = item["result"]
        if http_result not in (None, "") and http_result not in US1_HTTP_RESULTS:
            raise CiError("unsafe-field-us1.scenario.http.result")
        http.append(
            {
                "status": status,
                "request_id": request_id,
                "payment_id": payment_id,
                "result": http_result,
            }
        )

    pg_value = _require_list(scenario["pg"], "us1.scenario.pg")
    if len(pg_value) > 16:
        raise CiError("unsafe-field-us1.scenario.pg")
    pg: list[dict[str, Any]] = []
    for entry in pg_value:
        item = _us1_exact_keys(
            entry, ("state", "rows"), "scenario.pg"
        )
        state = item["state"]
        if state not in US1_PG_STATES:
            raise CiError("unsafe-field-us1.scenario.pg.state")
        pg.append(
            {
                "state": state,
                "rows": _us1_int(item["rows"], "scenario.pg.rows"),
            }
        )

    observer_value = scenario["observer"]
    observer: dict[str, Any] | None
    if observer_value is None:
        observer = None
    else:
        item = _us1_exact_keys(
            observer_value,
            ("trusted", "counts", "receipts"),
            "scenario.observer",
        )
        if not isinstance(item["trusted"], bool):
            raise CiError("unsafe-field-us1.scenario.observer.trusted")
        counts = _us1_exact_keys(
            item["counts"], ("payment", "query"), "scenario.observer.counts"
        )
        receipts_value = _require_list(
            item["receipts"], "us1.scenario.observer.receipts"
        )
        if len(receipts_value) > 8:
            raise CiError("unsafe-field-us1.scenario.observer.receipts")
        receipts = []
        for entry in receipts_value:
            receipt = _us1_exact_keys(
                entry,
                ("run_id", "epoch", "generation", "last_seq"),
                "scenario.observer.receipts",
            )
            receipts.append(
                {
                    "run_id": _us1_run_id(
                        receipt["run_id"], "scenario.observer.receipts.run_id"
                    ),
                    "epoch": _us1_hex32(
                        receipt["epoch"], "scenario.observer.receipts.epoch"
                    ),
                    "generation": _us1_int(
                        receipt["generation"],
                        "scenario.observer.receipts.generation",
                        minimum=1,
                    ),
                    "last_seq": _us1_int(
                        receipt["last_seq"],
                        "scenario.observer.receipts.last_seq",
                    ),
                }
            )
        observer = {
            "trusted": item["trusted"],
            "counts": {
                "payment": _us1_int(
                    counts["payment"], "scenario.observer.counts.payment"
                ),
                "query": _us1_int(
                    counts["query"], "scenario.observer.counts.query"
                ),
            },
            "receipts": receipts,
        }

    directive_value = scenario["directive"]
    directive: dict[str, Any] | None
    if directive_value is None:
        directive = None
    else:
        item = _us1_exact_keys(
            directive_value,
            ("result", "binding"),
            "scenario.directive",
        )
        if item["result"] not in {"SUCCEEDED", "FAILED"}:
            raise CiError("unsafe-field-us1.scenario.directive.result")
        binding = _us1_exact_keys(
            item["binding"],
            (
                "run_id",
                "epoch",
                "generation",
                "call_seq",
                "event_id",
                "order_id",
            ),
            "scenario.directive.binding",
        )
        directive = {
            "result": item["result"],
            "binding": {
                "run_id": _us1_run_id(
                    binding["run_id"], "scenario.directive.binding.run_id"
                ),
                "epoch": _us1_hex32(
                    binding["epoch"], "scenario.directive.binding.epoch"
                ),
                "generation": _us1_int(
                    binding["generation"],
                    "scenario.directive.binding.generation",
                    minimum=1,
                ),
                "call_seq": _us1_int(
                    binding["call_seq"],
                    "scenario.directive.binding.call_seq",
                    minimum=1,
                ),
                "event_id": _us1_event_id(
                    binding["event_id"],
                    "scenario.directive.binding.event_id",
                    binding["call_seq"],
                ),
                "order_id": _us1_uuid(
                    binding["order_id"],
                    "scenario.directive.binding.order_id",
                ),
            },
        }
        if (
            directive["binding"]["generation"] > US1_MAX_GENERATIONS
            or directive["binding"]["call_seq"] > US1_MAX_RECORDS
        ):
            raise CiError("unsafe-field-us1.scenario.directive.binding")

    commands_value = _require_list(
        scenario["commands"], "us1.scenario.commands"
    )
    if len(commands_value) > 8:
        raise CiError("unsafe-field-us1.scenario.commands")
    commands = []
    for entry in commands_value:
        item = _us1_exact_keys(
            entry, ("kind", "exit", "not_run"), "scenario.commands"
        )
        if item["kind"] not in US1_COMMAND_KINDS:
            raise CiError("unsafe-field-us1.scenario.commands.kind")
        if not isinstance(item["not_run"], bool):
            raise CiError("unsafe-field-us1.scenario.commands.not_run")
        exit_value = item["exit"]
        if item["not_run"]:
            if exit_value is not None:
                raise CiError("unsafe-field-us1.scenario.commands.exit")
        else:
            exit_value = _us1_int(
                exit_value, "scenario.commands.exit"
            )
        commands.append(
            {
                "kind": item["kind"],
                "exit": exit_value,
                "not_run": item["not_run"],
            }
        )

    return {
        "name": name,
        "result": result,
        "generations": generations,
        "http": http,
        "pg": pg,
        "observer": observer,
        "directive": directive,
        "commands": commands,
    }


def _us1_scenario_problems(
    scenario: Mapping[str, Any], run_id: str
) -> list[str]:
    if scenario["result"] != "PASS":
        return []
    name = scenario["name"]
    generations = scenario["generations"]
    http = scenario["http"]
    pg = scenario["pg"]
    observer = scenario["observer"]
    directive = scenario["directive"]
    commands = scenario["commands"]
    problems: list[str] = []
    payment = name.startswith("payment_") or name.startswith("restart_")
    success = name.endswith("_success")
    expected_generations = (
        [1, 2]
        if name.startswith("restart_")
        else ([1] if name != "http_boundary" else [])
    )
    if generations != expected_generations:
        problems.append("us1-generation")
    if not commands:
        problems.append("us1-commands")
    elif any(
        item["not_run"] or item["exit"] != 0 for item in commands
    ):
        problems.append("us1-commands")
    request_ids = [
        item["request_id"] for item in http if item["request_id"]
    ]
    if len(request_ids) != len(set(request_ids)):
        problems.append("us1-http-request-id")

    if payment:
        if observer is None or directive is None:
            return sorted(set(problems + ["us1-observer", "us1-directive"]))
        expected_result = "SUCCEEDED" if success else "FAILED"
        if directive["result"] != expected_result:
            problems.append("us1-directive-result")
        binding = directive["binding"]
        if binding["run_id"] != run_id:
            problems.append("us1-identity")
        if binding["generation"] != 1:
            problems.append("us1-generation")
        if observer["trusted"] is not True:
            problems.append("us1-observer")
        if observer["counts"] != {"payment": 1, "query": 0}:
            problems.append("us1-counts")
        receipts = observer["receipts"]
        if len(receipts) != len(generations):
            problems.append("us1-receipts")
        if sorted(item["generation"] for item in receipts) != sorted(
            generations
        ):
            problems.append("us1-receipts")
        if any(item["run_id"] != run_id for item in receipts):
            problems.append("us1-identity")
        if any(item["epoch"] != binding["epoch"] for item in receipts):
            problems.append("us1-identity")
        binding_receipts = [
            item
            for item in receipts
            if item["generation"] == binding["generation"]
        ]
        if not binding_receipts or binding_receipts[0]["last_seq"] < (
            binding["call_seq"] + 1
        ):
            problems.append("us1-receipts")
        payment_ids = {
            item["payment_id"]
            for item in http
            if item["payment_id"]
        }
        if payment_ids != {binding["order_id"]}:
            problems.append("us1-identity")
        for item in http:
            if not (200 <= item["status"] <= 299):
                problems.append("us1-http")
                continue
            if (
                not item["request_id"]
                or not item["payment_id"]
                or not item["result"]
            ):
                problems.append("us1-http")
            elif item["result"] != directive["result"]:
                problems.append("us1-http-result")
        statuses = [item["status"] for item in http]
        if statuses.count(201) != 1:
            problems.append("us1-http")
        required_200 = 4 if name.startswith("restart_") else 2
        if statuses.count(200) < required_200:
            problems.append("us1-http")
        if len(http) != (required_200 + 1):
            problems.append("us1-http")
        if len(pg) != 3 or any(item["rows"] != 1 for item in pg):
            problems.append("us1-pg")
        if {item["state"] for item in pg} != set(US1_PG_STATES):
            problems.append("us1-pg")
    elif name == "invalid_requests":
        if observer is None or directive is not None:
            problems.append("us1-observer")
        else:
            if observer["trusted"] is not True:
                problems.append("us1-observer")
            if observer["counts"] != {"payment": 0, "query": 0}:
                problems.append("us1-counts")
            receipts = observer["receipts"]
            if (
                len(receipts) != 1
                or receipts[0]["generation"] != 1
                or receipts[0]["run_id"] != run_id
            ):
                problems.append("us1-receipts")
        if len(pg) != 3 or any(item["rows"] != 0 for item in pg):
            problems.append("us1-pg")
        if {item["state"] for item in pg} != set(US1_PG_STATES):
            problems.append("us1-pg")
        if not http:
            problems.append("us1-http")
        for item in http:
            if not (400 <= item["status"] <= 499):
                problems.append("us1-http")
                continue
            if item["payment_id"] or item["result"]:
                problems.append("us1-http")
            if item["status"] != 400 and not item["request_id"]:
                problems.append("us1-http-request-id")
    else:
        if observer is not None or directive is not None or pg:
            problems.append("us1-observer")
        statuses = [item["status"] for item in http]
        if not http or statuses.count(201) < 1:
            problems.append("us1-http")
        if not {400, 401, 413, 415} <= set(statuses):
            problems.append("us1-http")
        for item in http:
            if item["status"] == 201:
                if (
                    not item["request_id"]
                    or not item["payment_id"]
                    or item["result"] != "SUCCEEDED"
                ):
                    problems.append("us1-http")
                continue
            if not (400 <= item["status"] <= 499):
                problems.append("us1-http")
                continue
            if item["payment_id"] or item["result"]:
                problems.append("us1-http")
            if item["status"] != 400 and not item["request_id"]:
                problems.append("us1-http-request-id")
    return sorted(set(problems))


def _safe_us1_report(
    source: dict[str, Any], db_safe: Mapping[str, Any]
) -> dict[str, Any]:
    if source.get("schema") != US1_SCHEMA:
        raise CiError("unsupported-report-schema")
    _us1_exact_keys(source, ("schema", "run_id", "scenarios"), "top")
    run_id = _us1_run_id(source.get("run_id"), "run_id")
    if run_id != db_safe.get("run_id"):
        raise CiError("us1-run-id-mismatch")
    scenarios_value = _require_list(source.get("scenarios"), "us1.scenarios")
    if len(scenarios_value) > len(US1_SCENARIO_NAMES):
        raise CiError("unsafe-field-us1.scenarios")
    scenarios: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in scenarios_value:
        scenario = _us1_scenario(value)
        if scenario["name"] in seen:
            raise CiError("us1-scenario-duplicate")
        seen.add(scenario["name"])
        scenarios.append(scenario)

    missing = [name for name in US1_SCENARIO_NAMES if name not in seen]
    problems: list[str] = []
    if missing:
        problems.append("us1-missing-scenario")
    for scenario in scenarios:
        problems.extend(_us1_scenario_problems(scenario, run_id))
    if any(item["result"] != "PASS" for item in scenarios):
        problems.append("us1-scenario-not-pass")

    db_source = db_safe.get("source", {})
    required = db_safe.get("required_test_results", {})
    packages = db_safe.get("package_results", {})
    db_green = (
        db_safe.get("result") == "pass"
        and db_safe.get("vet_exit") == 0
        and db_safe.get("test_exit") == 0
        and db_source.get("manifest_verification")
        == MANIFEST_VERIFICATION_UNCHANGED
        and all(required.get(name) == "pass" for name in DB_REQUIRED_TEST_KEYS)
        and packages.get("tests", {}).get("status") == "pass"
    )
    if not db_green:
        problems.append("us1-db-not-green")
    result = "pass" if not problems else "fail"
    return {
        "schema": "payment-hub.ci-safe.us1.v1",
        "status": "exported",
        "source_schema": US1_SCHEMA,
        "generated_at": utc_now(),
        "run_id": run_id,
        "result": result,
        "scenarios": scenarios,
        "missing_scenarios": missing,
        "problems": sorted(set(problems)),
        "source": {
            "manifest_digest": db_source.get("manifest_digest"),
            "manifest_verification": db_source.get("manifest_verification"),
            "file_count": db_source.get("file_count", 0),
        },
    }


def _us2_exact_keys(
    value: Any, keys: Sequence[str], name: str
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise CiError("unsafe-field-us2." + name)
    return value


def _us2_int(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CiError("unsafe-field-us2." + name)
    return value


def _us2_uuid(
    value: Any, name: str, nullable: bool = False
) -> str | None:
    if value in (None, "") and nullable:
        return value
    if not isinstance(value, str) or not UUID_RE.fullmatch(value):
        raise CiError("unsafe-field-us2." + name)
    return value


def _us2_hex32(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise CiError("unsafe-field-us2." + name)
    return value


def _us2_run_id(value: Any, name: str) -> str:
    return _us2_hex32(value, name)


def _us2_event_id(value: Any, name: str, call_seq: int) -> str:
    if (
        not isinstance(value, str)
        or not US1_EVENT_ID_RE.fullmatch(value)
        or call_seq > US1_MAX_RECORDS
    ):
        raise CiError("unsafe-field-us2." + name)
    suffix = int(value.rsplit(":", 1)[1])
    if suffix != call_seq:
        raise CiError("unsafe-field-us2." + name)
    return value


def _us2_counts(value: Any, name: str) -> dict[str, int]:
    item = _us2_exact_keys(value, US2_COUNT_KEYS, name)
    return {
        key: _us2_int(item[key], name + "." + key)
        for key in US2_COUNT_KEYS
    }


def _us2_checks(value: Any) -> dict[str, Any]:
    item = _us2_exact_keys(value, US2_CHECKS_KEYS, "checks")
    before = _us2_counts(item["counts_before"], "checks.counts_before")
    after = _us2_counts(item["counts_after"], "checks.counts_after")
    stable_facts = item["stable_facts"]
    if not isinstance(stable_facts, bool):
        raise CiError("unsafe-field-us2.checks.stable_facts")
    readonly_probes = _us2_int(
        item["readonly_probes"], "checks.readonly_probes"
    )
    lookup_fault = item["lookup_fault"]
    if lookup_fault not in US2_LOOKUP_FAULTS:
        raise CiError("unsafe-field-us2.checks.lookup_fault")
    waiter_count = _us2_int(item["waiter_count"], "checks.waiter_count")
    mixed_waiter_count = _us2_int(
        item["mixed_waiter_count"], "checks.mixed_waiter_count"
    )
    return {
        "counts_before": before,
        "counts_after": after,
        "stable_facts": stable_facts,
        "readonly_probes": readonly_probes,
        "lookup_fault": lookup_fault,
        "waiter_count": waiter_count,
        "mixed_waiter_count": mixed_waiter_count,
    }


def _us2_scenario(value: Any) -> dict[str, Any]:
    scenario = _us2_exact_keys(
        value,
        (
            "name",
            "result",
            "generations",
            "http",
            "pg",
            "observer",
            "directive",
            "commands",
            "checks",
        ),
        "scenario",
    )
    name = scenario["name"]
    if name not in US2_SCENARIO_NAMES:
        raise CiError("unsafe-field-us2.scenario.name")
    result = scenario["result"]
    if result not in US2_RESULTS:
        raise CiError("unsafe-field-us2.scenario.result")

    generations_value = _require_list(
        scenario["generations"], "us2.scenario.generations"
    )
    if not generations_value or len(generations_value) > 2:
        raise CiError("unsafe-field-us2.scenario.generations")
    generations = [
        _us2_int(item, "scenario.generations", minimum=1)
        for item in generations_value
    ]
    if any(item not in (1, 2) for item in generations):
        raise CiError("unsafe-field-us2.scenario.generations")
    if generations != sorted(set(generations)):
        raise CiError("unsafe-field-us2.scenario.generations")

    checks = _us2_checks(scenario["checks"])

    http_value = _require_list(scenario["http"], "us2.scenario.http")
    if len(http_value) > US2_MAX_HTTP:
        raise CiError("unsafe-field-us2.scenario.http")
    http: list[dict[str, Any]] = []
    for entry in http_value:
        item = _us2_exact_keys(
            entry,
            ("status", "request_id", "payment_id", "result"),
            "scenario.http",
        )
        status = _us2_int(item["status"], "scenario.http.status", minimum=100)
        if status > 599:
            raise CiError("unsafe-field-us2.scenario.http.status")
        request_id = _us2_uuid(
            item["request_id"], "scenario.http.request_id", nullable=True
        )
        payment_id = _us2_uuid(
            item["payment_id"], "scenario.http.payment_id", nullable=True
        )
        http_result = item["result"]
        if http_result not in (None, "") and http_result not in US1_HTTP_RESULTS:
            raise CiError("unsafe-field-us2.scenario.http.result")
        http.append(
            {
                "status": status,
                "request_id": request_id,
                "payment_id": payment_id,
                "result": http_result,
            }
        )

    pg_value = _require_list(scenario["pg"], "us2.scenario.pg")
    if len(pg_value) > US2_MAX_PG:
        raise CiError("unsafe-field-us2.scenario.pg")
    pg: list[dict[str, Any]] = []
    for entry in pg_value:
        item = _us2_exact_keys(
            entry, ("state", "rows"), "scenario.pg"
        )
        state = item["state"]
        if state not in US1_PG_STATES:
            raise CiError("unsafe-field-us2.scenario.pg.state")
        pg.append(
            {
                "state": state,
                "rows": _us2_int(item["rows"], "scenario.pg.rows"),
            }
        )

    observer_value = scenario["observer"]
    observer: dict[str, Any] | None
    if observer_value is None:
        observer = None
    else:
        item = _us2_exact_keys(
            observer_value,
            ("trusted", "counts", "receipts"),
            "scenario.observer",
        )
        if not isinstance(item["trusted"], bool):
            raise CiError("unsafe-field-us2.scenario.observer.trusted")
        counts = _us2_exact_keys(
            item["counts"], ("payment", "query"), "scenario.observer.counts"
        )
        receipts_value = _require_list(
            item["receipts"], "us2.scenario.observer.receipts"
        )
        if len(receipts_value) > US2_MAX_RECEIPTS:
            raise CiError("unsafe-field-us2.scenario.observer.receipts")
        receipts = []
        for entry in receipts_value:
            receipt = _us2_exact_keys(
                entry,
                ("run_id", "epoch", "generation", "last_seq"),
                "scenario.observer.receipts",
            )
            generation = _us2_int(
                receipt["generation"],
                "scenario.observer.receipts.generation",
                minimum=1,
            )
            if generation > US1_MAX_GENERATIONS:
                raise CiError(
                    "unsafe-field-us2.scenario.observer.receipts.generation"
                )
            receipts.append(
                {
                    "run_id": _us2_run_id(
                        receipt["run_id"], "scenario.observer.receipts.run_id"
                    ),
                    "epoch": _us2_hex32(
                        receipt["epoch"], "scenario.observer.receipts.epoch"
                    ),
                    "generation": generation,
                    "last_seq": _us2_int(
                        receipt["last_seq"],
                        "scenario.observer.receipts.last_seq",
                    ),
                }
            )
        observer = {
            "trusted": item["trusted"],
            "counts": {
                "payment": _us2_int(
                    counts["payment"], "scenario.observer.counts.payment"
                ),
                "query": _us2_int(
                    counts["query"], "scenario.observer.counts.query"
                ),
            },
            "receipts": receipts,
        }

    directive_value = scenario["directive"]
    directive: dict[str, Any] | None
    if directive_value is None:
        directive = None
    else:
        item = _us2_exact_keys(
            directive_value,
            ("result", "binding"),
            "scenario.directive",
        )
        if item["result"] not in {"SUCCEEDED", "FAILED"}:
            raise CiError("unsafe-field-us2.scenario.directive.result")
        binding = _us2_exact_keys(
            item["binding"],
            (
                "run_id",
                "epoch",
                "generation",
                "call_seq",
                "event_id",
                "order_id",
            ),
            "scenario.directive.binding",
        )
        call_seq = _us2_int(
            binding["call_seq"],
            "scenario.directive.binding.call_seq",
            minimum=1,
        )
        generation = _us2_int(
            binding["generation"],
            "scenario.directive.binding.generation",
            minimum=1,
        )
        if (
            generation > US1_MAX_GENERATIONS
            or call_seq > US1_MAX_RECORDS
        ):
            raise CiError("unsafe-field-us2.scenario.directive.binding")
        directive = {
            "result": item["result"],
            "binding": {
                "run_id": _us2_run_id(
                    binding["run_id"], "scenario.directive.binding.run_id"
                ),
                "epoch": _us2_hex32(
                    binding["epoch"], "scenario.directive.binding.epoch"
                ),
                "generation": generation,
                "call_seq": call_seq,
                "event_id": _us2_event_id(
                    binding["event_id"],
                    "scenario.directive.binding.event_id",
                    call_seq,
                ),
                "order_id": _us2_uuid(
                    binding["order_id"],
                    "scenario.directive.binding.order_id",
                ),
            },
        }

    commands_value = _require_list(
        scenario["commands"], "us2.scenario.commands"
    )
    if len(commands_value) > US2_MAX_COMMANDS:
        raise CiError("unsafe-field-us2.scenario.commands")
    commands = []
    for entry in commands_value:
        item = _us2_exact_keys(
            entry, ("kind", "exit", "not_run"), "scenario.commands"
        )
        if item["kind"] not in US1_COMMAND_KINDS:
            raise CiError("unsafe-field-us2.scenario.commands.kind")
        if not isinstance(item["not_run"], bool):
            raise CiError("unsafe-field-us2.scenario.commands.not_run")
        exit_value = item["exit"]
        if item["not_run"]:
            if exit_value is not None:
                raise CiError("unsafe-field-us2.scenario.commands.exit")
        else:
            exit_value = _us2_int(
                exit_value, "scenario.commands.exit"
            )
        commands.append(
            {
                "kind": item["kind"],
                "exit": exit_value,
                "not_run": item["not_run"],
            }
        )

    return {
        "name": name,
        "result": result,
        "generations": generations,
        "http": http,
        "pg": pg,
        "observer": observer,
        "directive": directive,
        "commands": commands,
        "checks": checks,
    }


def _us2_status_counts(http: Sequence[Mapping[str, Any]]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for item in http:
        status = item["status"]
        counts[status] = counts.get(status, 0) + 1
    return counts


def _us2_2xx(http: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [item for item in http if 200 <= item["status"] <= 299]


def _us2_scenario_problems(
    scenario: Mapping[str, Any], run_id: str
) -> list[str]:
    if scenario["result"] != "PASS":
        return []
    name = scenario["name"]
    checks = scenario["checks"]
    before = checks["counts_before"]
    after = checks["counts_after"]
    http = scenario["http"]
    pg = scenario["pg"]
    observer = scenario["observer"]
    directive = scenario["directive"]
    commands = scenario["commands"]
    problems: list[str] = []

    command_kinds = {item["kind"] for item in commands}
    if not {"build", "process_stop"} <= command_kinds or any(
        item["not_run"] or item["exit"] != 0 for item in commands
    ):
        problems.append("us2-commands")

    request_ids = [
        item["request_id"] for item in http if item["request_id"]
    ]
    if len(request_ids) != len(set(request_ids)):
        problems.append("us2-http-request-id")

    if not checks["stable_facts"]:
        problems.append("us2-checks")

    # PG after-state must carry exactly one ORDER/MOCK/TRACE observation and
    # its row counts must equal counts_after.
    pg_rows = {item["state"]: item["rows"] for item in pg}
    if (
        len(pg) != 3
        or set(pg_rows) != set(US1_PG_STATES)
        or pg_rows.get("ORDER") != after["order_rows"]
        or pg_rows.get("MOCK") != after["mock_rows"]
        or pg_rows.get("TRACE") != after["trace_rows"]
    ):
        problems.append("us2-pg")

    two_xx = _us2_2xx(http)
    for item in two_xx:
        if (
            not item["request_id"]
            or not item["payment_id"]
            or not item["result"]
            or item["result"] not in US1_HTTP_RESULTS
        ):
            problems.append("us2-http")
            break
    payment_ids = {item["payment_id"] for item in two_xx if item["payment_id"]}
    if len(payment_ids) > 1:
        problems.append("us2-identity")

    if observer is None:
        problems.append("us2-observer")
    else:
        if observer["trusted"] is not True:
            problems.append("us2-observer")
        receipts = observer["receipts"]
        if not receipts:
            problems.append("us2-receipts")
        if any(item["run_id"] != run_id for item in receipts):
            problems.append("us2-identity")
        epochs = {item["epoch"] for item in receipts}
        if len(epochs) > 1:
            problems.append("us2-identity")
        if sorted(item["generation"] for item in receipts) != sorted(
            scenario["generations"]
        ):
            problems.append("us2-receipts")
        if observer["counts"] != {
            "payment": after["payment_calls"],
            "query": after["query_calls"],
        }:
            problems.append("us2-counts")
        if directive is not None:
            binding = directive["binding"]
            if binding["run_id"] != run_id:
                problems.append("us2-identity")
            if epochs and binding["epoch"] not in epochs:
                problems.append("us2-identity")
            if binding["generation"] not in scenario["generations"]:
                problems.append("us2-generation")
            matching = [
                item
                for item in receipts
                if item["generation"] == binding["generation"]
            ]
            if not matching or matching[0]["last_seq"] < (
                binding["call_seq"] + 1
            ):
                problems.append("us2-receipts")
    if directive is not None and observer is None:
        binding = directive["binding"]
        if binding["run_id"] != run_id:
            problems.append("us2-identity")
        if binding["generation"] not in scenario["generations"]:
            problems.append("us2-generation")

    statuses = _us2_status_counts(http)
    if statuses.get(500, 0) != 0:
        # A controlled 500 belongs only to the separate TestUS2Conflict
        # subtest; it must never enter the eight-scenario binary report.
        problems.append("us2-http")
    if name == "replay_unknown":
        if (
            before != US2_REPLAY_UNKNOWN_COUNTS
            or after != US2_REPLAY_UNKNOWN_COUNTS
        ):
            problems.append("us2-checks")
        if (
            checks["readonly_probes"] != 0
            or checks["lookup_fault"] != "none"
            or checks["waiter_count"] != 0
            or checks["mixed_waiter_count"] != 0
        ):
            problems.append("us2-checks")
        if statuses.get(200, 0) != 2 or statuses.get(201, 0) != 0:
            problems.append("us2-http")
        if any(
            item["result"] != "UNKNOWN" for item in two_xx
        ) or len(payment_ids) != 1:
            problems.append("us2-http-result")
        if directive is not None:
            problems.append("us2-directive")
    elif name == "concurrency":
        if (
            before != US2_CONCURRENCY_EMPTY_COUNTS
            or after != US2_CONCURRENCY_COUNTS
        ):
            problems.append("us2-checks")
        if (
            checks["readonly_probes"] != 0
            or checks["lookup_fault"] != "none"
            or checks["waiter_count"] != 2
            or checks["mixed_waiter_count"] != 2
        ):
            problems.append("us2-checks")
        if (
            statuses.get(201, 0) != 1
            or statuses.get(200, 0) < 4
            or statuses.get(409, 0) < 1
        ):
            problems.append("us2-http")
        if len(payment_ids) != 1:
            problems.append("us2-identity")
        if len(http) < 2 or any(
            item["status"] != 200 for item in http[-2:]
        ):
            problems.append("us2-http")
        elif (
            http[-2]["payment_id"] != http[-1]["payment_id"]
            or http[-2]["result"] != http[-1]["result"]
        ):
            problems.append("us2-http-result")
    elif name == "conflict_priority":
        if before != US2_SETUP_COUNTS or after != US2_SETUP_COUNTS:
            problems.append("us2-checks")
        if (
            checks["readonly_probes"] != 5
            or checks["lookup_fault"] != "timeout"
            or checks["waiter_count"] != 0
            or checks["mixed_waiter_count"] != 0
        ):
            problems.append("us2-checks")
        if (
            statuses.get(201, 0) != 1
            or statuses.get(409, 0) < 2
            or statuses.get(400, 0) < 4
            or statuses.get(401, 0) < 1
            or statuses.get(503, 0) != 1
            or statuses.get(500, 0) != 0
        ):
            problems.append("us2-http")
        setup = [item for item in http if item["status"] == 201]
        if len(setup) != 1 or setup[0]["result"] != "SUCCEEDED":
            problems.append("us2-http")
        setup_id = setup[0]["payment_id"] if setup else None
        for item in http:
            if item["status"] < 400:
                continue
            if item["result"] not in (None, ""):
                problems.append("us2-http-result")
            if item["payment_id"] not in (None, "", setup_id):
                problems.append("us2-identity")
    else:
        if before != US2_SETUP_COUNTS or after != US2_SETUP_COUNTS:
            problems.append("us2-checks")
        if (
            checks["readonly_probes"] != 0
            or checks["lookup_fault"] != "none"
            or checks["waiter_count"] != 0
            or checks["mixed_waiter_count"] != 0
        ):
            problems.append("us2-checks")
        if name in ("restart_success", "restart_failed"):
            if scenario["generations"] != [1, 2]:
                problems.append("us2-generation")
        elif name == "key_rotation":
            if len(scenario["generations"]) < 2:
                problems.append("us2-generation")
        elif len(scenario["generations"]) != 1:
            problems.append("us2-generation")
        expected_result = (
            "SUCCEEDED"
            if name in ("replay_success", "restart_success", "key_rotation")
            else "FAILED"
        )
        if name in ("replay_success", "replay_failed"):
            if statuses.get(201, 0) != 1 or statuses.get(200, 0) < 2:
                problems.append("us2-http")
            if set(statuses) - {200, 201}:
                problems.append("us2-http")
        elif name in ("restart_success", "restart_failed"):
            if statuses.get(201, 0) < 1 or statuses.get(200, 0) < 2:
                problems.append("us2-http")
            if set(statuses) - {200, 201}:
                problems.append("us2-http")
        else:  # key_rotation
            if (
                statuses.get(201, 0) < 1
                or statuses.get(200, 0) < 2
                or statuses.get(401, 0) != 1
            ):
                problems.append("us2-http")
            if set(statuses) - {200, 201, 401}:
                problems.append("us2-http")
            setup = [item for item in http if item["status"] == 201]
            setup_id = setup[0]["payment_id"] if setup else None
            for item in http:
                if item["status"] != 401:
                    continue
                if item["result"] not in (None, "") or item[
                    "payment_id"
                ] not in (None, "", setup_id):
                    problems.append("us2-http")
        if any(item["result"] != expected_result for item in two_xx):
            problems.append("us2-http-result")
        if len(payment_ids) != 1:
            problems.append("us2-identity")

    return sorted(set(problems))


def _safe_us2_report(
    source: dict[str, Any], db_safe: Mapping[str, Any]
) -> dict[str, Any]:
    if source.get("schema") != US2_SCHEMA:
        raise CiError("unsupported-report-schema")
    _us2_exact_keys(source, ("schema", "run_id", "scenarios"), "top")
    run_id = _us2_run_id(source.get("run_id"), "run_id")
    if run_id != db_safe.get("run_id"):
        raise CiError("us2-run-id-mismatch")
    scenarios_value = _require_list(source.get("scenarios"), "us2.scenarios")
    if len(scenarios_value) > len(US2_SCENARIO_NAMES):
        raise CiError("unsafe-field-us2.scenarios")
    scenarios: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in scenarios_value:
        scenario = _us2_scenario(value)
        if scenario["name"] in seen:
            raise CiError("us2-scenario-duplicate")
        seen.add(scenario["name"])
        scenarios.append(scenario)

    missing = [
        name for name in US2_SCENARIO_NAMES if name not in seen
    ]
    problems: list[str] = []
    if missing:
        problems.append("us2-missing-scenario")
    for scenario in scenarios:
        if scenario["result"] != "PASS":
            problems.append("us2-scenario-not-pass")
        problems.extend(_us2_scenario_problems(scenario, run_id))

    db_source = db_safe.get("source", {})
    required = db_safe.get("required_test_results", {})
    packages = db_safe.get("package_results", {})
    db_green = (
        db_safe.get("result") == "pass"
        and db_safe.get("vet_exit") == 0
        and db_safe.get("test_exit") == 0
        and db_source.get("manifest_verification")
        == MANIFEST_VERIFICATION_UNCHANGED
        and all(required.get(name) == "pass" for name in DB_REQUIRED_TEST_KEYS)
        and packages.get("tests", {}).get("status") == "pass"
    )
    if not db_green:
        problems.append("us2-db-not-green")
    result = "pass" if not problems else "fail"
    return {
        "schema": "payment-hub.ci-safe.us2.v1",
        "status": "exported",
        "source_schema": US2_SCHEMA,
        "generated_at": utc_now(),
        "run_id": run_id,
        "result": result,
        "scenarios": scenarios,
        "missing_scenarios": missing,
        "problems": sorted(set(problems)),
        "source": {
            "manifest_digest": db_source.get("manifest_digest"),
            "manifest_verification": db_source.get("manifest_verification"),
            "file_count": db_source.get("file_count", 0),
        },
    }


def _safe_vulnerability_report(source: dict[str, Any]) -> dict[str, Any]:
    if source.get("schema") != VULNERABILITY_SCHEMA:
        raise CiError("unsupported-report-schema")
    if source.get("version") != 1:
        raise CiError("unsafe-field-version")
    result = _safe_status(
        source.get("result"), VULNERABILITY_RESULTS, "result"
    )

    tool_source = _require_dict(source.get("tool"), "tool")
    tool = {
        "name": _require_pattern(
            TOOL_NAME_RE, tool_source.get("name"), "tool.name"
        ),
        "module": _require_pattern(
            TOOL_MODULE_RE, tool_source.get("module"), "tool.module"
        ),
        "version": _safe_version(tool_source.get("version")),
        "commit": _require_str(
            tool_source.get("commit"), "tool.commit", 64
        ),
    }
    for key, value in tool.items():
        if not value:
            raise CiError("unsafe-field-tool." + key)
    if not re.fullmatch(r"[0-9a-f]{40}", tool["commit"]):
        raise CiError("unsafe-field-tool.commit")

    tool_verified = _require_bool(
        source.get("tool_verified"), "tool_verified"
    )
    expected_go = _require_str(
        source.get("expected_go_version"), "expected_go_version", 32
    )
    if expected_go != EXPECTED_VULNERABILITY_GO:
        raise CiError("unsafe-field-expected_go_version")
    observed_value = source.get("go_version")
    if observed_value is None:
        observed_go = None
    else:
        observed_go = _safe_version(observed_value)

    source_hash_source = _require_dict(
        source.get("source_hash"), "source_hash"
    )
    if set(source_hash_source) != {"go.mod", "go.sum"}:
        raise CiError("unsafe-field-source_hash")
    source_hash: dict[str, str] = {}
    for name in ("go.mod", "go.sum"):
        value = source_hash_source[name]
        if not isinstance(value, str) or not HEX64_RE.fullmatch(value):
            raise CiError("unsafe-field-source_hash")
        source_hash[name] = value
    module_unchanged = _require_bool(
        source.get("module_unchanged"), "module_unchanged"
    )

    scan_source = _require_dict(source.get("scan"), "scan")
    scan: dict[str, Any] = {}
    scan["mode"] = _require_pattern(
        SCAN_MODE_RE, scan_source.get("mode"), "scan.mode"
    )
    scan["level"] = _require_pattern(
        SCAN_MODE_RE, scan_source.get("level"), "scan.level"
    )
    scan["tags"] = _require_pattern(
        SCAN_TAGS_RE, scan_source.get("tags"), "scan.tags"
    )
    scan["db"] = _require_pattern(
        SCAN_DB_RE, scan_source.get("db"), "scan.db"
    )
    scan["pattern"] = _require_pattern(
        SCAN_PATTERN_RE, scan_source.get("pattern"), "scan.pattern"
    )
    test_value = scan_source.get("test")
    if isinstance(test_value, bool):
        scan["test"] = test_value
    elif isinstance(test_value, str):
        scan["test"] = _require_pattern(
            SCAN_MODE_RE, test_value, "scan.test"
        )
    else:
        raise CiError("unsafe-field-scan.test")
    cgo_value = scan_source.get("cgo_enabled")
    if isinstance(cgo_value, bool):
        cgo_value = "1" if cgo_value else "0"
    elif isinstance(cgo_value, int):
        cgo_value = str(cgo_value)
    if cgo_value != "1":
        raise CiError("unsafe-field-scan.cgo_enabled")
    scan["cgo_enabled"] = "1"
    goos = _require_str(scan_source.get("goos"), "scan.goos", 32)
    if goos != "linux":
        raise CiError("unsafe-field-scan.goos")
    scan["goos"] = goos
    goarch = _require_str(
        scan_source.get("goarch"), "scan.goarch", 32
    )
    if goarch != "amd64":
        raise CiError("unsafe-field-scan.goarch")
    scan["goarch"] = goarch

    osv_ids = _safe_go_ids(source.get("osv_ids"), "osv_ids")
    reachable_ids = _safe_go_ids(
        source.get("reachable_ids"), "reachable_ids"
    )
    unreachable_ids = _safe_go_ids(
        source.get("unreachable_ids"), "unreachable_ids"
    )
    if set(reachable_ids) & set(unreachable_ids):
        raise CiError("vulnerability-inconsistent")
    if osv_ids != sorted(set(reachable_ids) | set(unreachable_ids)):
        raise CiError("vulnerability-inconsistent")
    reachable_count = _require_int(
        source.get("reachable_count"), "reachable_count"
    )
    unreachable_count = _require_int(
        source.get("unreachable_count"), "unreachable_count"
    )
    if reachable_count != len(reachable_ids):
        raise CiError("vulnerability-inconsistent")
    if unreachable_count != len(unreachable_ids):
        raise CiError("vulnerability-inconsistent")

    failure_value = source.get("failure_class")
    if failure_value is None:
        failure_class = None
    elif isinstance(failure_value, str) and FAILURE_CLASS_RE.fullmatch(
        failure_value
    ):
        failure_class = failure_value
    else:
        raise CiError("unsafe-field-failure_class")
    command_exit_value = source.get("command_exit")
    if command_exit_value is None:
        command_exit = None
    else:
        command_exit = _require_int(
            command_exit_value, "command_exit"
        )
    duration_seconds = _require_number(
        source.get("duration_seconds"), "duration_seconds"
    )

    trusted = (
        tool_verified
        and observed_go == expected_go
        and module_unchanged
        and scan.get("cgo_enabled") == "1"
        and scan.get("goos") == "linux"
        and scan.get("goarch") == "amd64"
    )
    if result == "pass":
        if (
            not trusted
            or reachable_count != 0
            or command_exit != 0
        ):
            raise CiError("vulnerability-inconsistent")
        if failure_class is not None:
            raise CiError("vulnerability-inconsistent")
    elif result == "vulnerable":
        if (
            not trusted
            or reachable_count == 0
            or command_exit != 0
        ):
            raise CiError("vulnerability-inconsistent")
        if failure_class is not None:
            raise CiError("vulnerability-inconsistent")
    else:
        if failure_class is None:
            raise CiError("vulnerability-inconsistent")

    return {
        "schema": "payment-hub.ci-safe.vulnerability.v1",
        "status": "exported",
        "source_schema": VULNERABILITY_SCHEMA,
        "generated_at": utc_now(),
        "version": 1,
        "result": result,
        "tool": tool,
        "tool_verified": tool_verified,
        "expected_go_version": expected_go,
        "go_version": observed_go,
        "source_hash": source_hash,
        "module_unchanged": module_unchanged,
        "scan": scan,
        "osv_ids": osv_ids,
        "reachable_ids": reachable_ids,
        "unreachable_ids": unreachable_ids,
        "reachable_count": reachable_count,
        "unreachable_count": unreachable_count,
        "failure_class": failure_class,
        "command_exit": command_exit,
        "duration_seconds": duration_seconds,
    }


def _safe_cancellation_report(source: dict[str, Any]) -> dict[str, Any]:
    if source.get("schema") != CANCELLATION_SCHEMA:
        raise CiError("unsupported-report-schema")
    result = _safe_status(
        source.get("result"), CANCELLATION_RESULTS, "result"
    )
    run_id_value = source.get("run_id")
    if run_id_value is None:
        run_id = None
    elif isinstance(run_id_value, str) and re.fullmatch(
        r"[0-9a-f]{32}", run_id_value
    ):
        run_id = run_id_value
    else:
        raise CiError("unsafe-field-run_id")
    project_value = source.get("project")
    if project_value is None:
        project = None
    elif (
        isinstance(project_value, str)
        and run_id is not None
        and project_value == "payment-hub-test-db-" + run_id
    ):
        project = project_value
    else:
        raise CiError("unsafe-field-project")
    stage = _require_str(source.get("stage"), "stage", 32)
    if stage != "pg-healthy":
        raise CiError("unsafe-field-stage")
    signal_name = _require_str(source.get("signal"), "signal", 32)
    if signal_name != "SIGTERM":
        raise CiError("unsafe-field-signal")
    launcher_exit = _safe_int_or_none(
        source.get("launcher_exit"), "launcher_exit"
    )

    failure_source = _require_dict(source.get("failure"), "failure")
    failure = {
        "stage": _require_str(failure_source.get("stage"), "failure.stage", 80),
        "kind": _require_str(failure_source.get("kind"), "failure.kind", 80),
    }
    for value in failure.values():
        if not re.fullmatch(r"[a-z0-9-]{1,80}", value):
            raise CiError("unsafe-field-failure")

    cleanup_source = _require_dict(source.get("cleanup"), "cleanup")
    cleanup_result = _safe_status(
        cleanup_source.get("result"),
        CANCELLATION_CLEANUP_RESULTS,
        "cleanup.result",
    )
    compose_down_exit = _safe_int_or_none(
        cleanup_source.get("compose_down_exit"), "cleanup.compose_down_exit"
    )
    network_removed = _require_bool(
        cleanup_source.get("network_removed"), "cleanup.network_removed"
    )
    volume_removed = _require_bool(
        cleanup_source.get("volume_removed"), "cleanup.volume_removed"
    )
    volume_retained = _require_bool(
        cleanup_source.get("volume_retained"), "cleanup.volume_retained"
    )
    repeat_signal_ignored = _require_bool(
        cleanup_source.get("repeat_signal_ignored"),
        "cleanup.repeat_signal_ignored",
    )
    error_count = _require_int(
        cleanup_source.get("error_count"), "cleanup.error_count"
    )
    if error_count < 0:
        raise CiError("unsafe-field-cleanup.error_count")
    tool_containers_removed = _require_bool(
        cleanup_source.get("tool_containers_removed"),
        "cleanup.tool_containers_removed",
    )
    cleanup = {
        "result": cleanup_result,
        "compose_down_exit": compose_down_exit,
        "network_removed": network_removed,
        "volume_removed": volume_removed,
        "volume_retained": volume_retained,
        "repeat_signal_ignored": repeat_signal_ignored,
        "error_count": error_count,
        "tool_containers_removed": tool_containers_removed,
    }

    absence_source = _require_dict(
        source.get("resource_absence"), "resource_absence"
    )
    absence = {
        key: _require_bool(
            absence_source.get(key), "resource_absence." + key
        )
        for key in ("containers", "network", "volume")
    }
    preexisting = _require_bool(
        source.get("preexisting_resource_preserved"),
        "preexisting_resource_preserved",
    )
    elapsed_seconds = _require_number(
        source.get("elapsed_seconds"), "elapsed_seconds"
    )
    timings_source = _require_dict(source.get("timings"), "timings")
    timings = {
        "pg_healthy_seconds": _safe_float_or_none(
            timings_source.get("pg_healthy_seconds"),
            "timings.pg_healthy_seconds",
        ),
        "exit_after_signal_seconds": _safe_float_or_none(
            timings_source.get("exit_after_signal_seconds"),
            "timings.exit_after_signal_seconds",
        ),
    }
    input_value = source.get("input_sha256")
    if input_value is None:
        input_sha256 = None
    elif isinstance(input_value, str) and HEX64_RE.fullmatch(input_value):
        input_sha256 = input_value
    else:
        raise CiError("unsafe-field-input_sha256")

    if result == "pass":
        cleanup_ok = (
            cleanup_result == "pass"
            and error_count == 0
            and compose_down_exit == 0
            and network_removed
            and volume_removed
            and not volume_retained
            and not repeat_signal_ignored
            and tool_containers_removed
        )
        if (
            run_id is None
            or project is None
            or launcher_exit != 1
            or failure != CANCELLATION_PASS_FAILURE
            or not cleanup_ok
            or not all(absence.values())
            or not preexisting
            or input_sha256 is None
        ):
            raise CiError("cancellation-inconsistent")

    return {
        "schema": "payment-hub.ci-safe.cancellation.v1",
        "status": "exported",
        "source_schema": CANCELLATION_SCHEMA,
        "generated_at": utc_now(),
        "result": result,
        "run_id": run_id,
        "project": project,
        "stage": stage,
        "signal": signal_name,
        "launcher_exit": launcher_exit,
        "failure": failure,
        "cleanup": cleanup,
        "resource_absence": absence,
        "preexisting_resource_preserved": preexisting,
        "elapsed_seconds": elapsed_seconds,
        "timings": timings,
        "input_sha256": input_sha256,
    }


def _missing_report(name: str) -> dict[str, Any]:
    return {
        "schema": "payment-hub.ci-safe." + name + ".v1",
        "status": "missing",
        "expected_source": REPORT_FILENAMES[name],
        "generated_at": utc_now(),
    }


def _error_report(name: str, category: str) -> dict[str, Any]:
    return {
        "schema": "payment-hub.ci-safe." + name + ".v1",
        "status": "error",
        "error": category,
        "expected_source": REPORT_FILENAMES[name],
        "generated_at": utc_now(),
    }


def _export_one(
    name: str,
    report_dir: Path,
    db_root: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    safe_path = report_dir / "upload" / SAFE_FILENAMES[name]
    if name == "default":
        source_path = report_dir / REPORT_FILENAMES[name]
        if not source_path.exists():
            return _missing_report(name), None
        source, source_hash = _read_source_json(
            source_path, expected_parent=report_dir
        )
        safe = _safe_default_report(source)
        return safe, {
            "source": REPORT_FILENAMES[name],
            "source_sha256": source_hash,
        }
    if name == "db":
        source_path = _find_db_report(db_root)
        if source_path is None:
            return _missing_report(name), None
        source, source_hash = _read_source_json(
            source_path, expected_parent=source_path.parent
        )
        if source.get("run_id") != source_path.parent.name:
            raise CiError("unsafe-field-run_id")
        safe = _safe_db_report(source)
        return safe, {
            "source": source_path.parent.name
            + "/"
            + REPORT_FILENAMES[name],
            "source_sha256": source_hash,
        }
    if name == "us1":
        db_source_path = _find_db_report(db_root)
        if db_source_path is None:
            return _missing_report(name), None
        db_source, _ = _read_source_json(
            db_source_path, expected_parent=db_source_path.parent
        )
        if db_source.get("run_id") != db_source_path.parent.name:
            raise CiError("unsafe-field-run_id")
        db_safe = _safe_db_report(db_source)
        source_path = db_source_path.parent / REPORT_FILENAMES[name]
        if not source_path.exists():
            return _missing_report(name), None
        source, source_hash = _read_source_json(
            source_path, expected_parent=db_source_path.parent
        )
        safe = _safe_us1_report(source, db_safe)
        return safe, {
            "source": db_source_path.parent.name
            + "/"
            + REPORT_FILENAMES[name],
            "source_sha256": source_hash,
            "source_manifest_digest": db_safe["source"]["manifest_digest"],
            "source_manifest_verification": db_safe["source"][
                "manifest_verification"
            ],
        }
    if name == "us2":
        db_source_path = _find_db_report(db_root)
        if db_source_path is None:
            return _missing_report(name), None
        db_source, _ = _read_source_json(
            db_source_path, expected_parent=db_source_path.parent
        )
        if db_source.get("run_id") != db_source_path.parent.name:
            raise CiError("unsafe-field-run_id")
        db_safe = _safe_db_report(db_source)
        source_path = db_source_path.parent / REPORT_FILENAMES[name]
        if not source_path.exists():
            return _missing_report(name), None
        source, source_hash = _read_source_json(
            source_path, expected_parent=db_source_path.parent
        )
        safe = _safe_us2_report(source, db_safe)
        return safe, {
            "source": db_source_path.parent.name
            + "/"
            + REPORT_FILENAMES[name],
            "source_sha256": source_hash,
            "source_manifest_digest": db_safe["source"]["manifest_digest"],
            "source_manifest_verification": db_safe["source"][
                "manifest_verification"
            ],
        }
    source_path = report_dir / REPORT_FILENAMES[name]
    if not source_path.exists():
        return _missing_report(name), None
    if name == "vulnerability":
        source, source_hash = _read_source_json(
            source_path, expected_parent=report_dir
        )
        safe = _safe_vulnerability_report(source)
        return safe, {
            "source": REPORT_FILENAMES[name],
            "source_sha256": source_hash,
        }
    if name == "cancellation":
        source, source_hash = _read_source_json(
            source_path, expected_parent=report_dir
        )
        safe = _safe_cancellation_report(source)
        return safe, {
            "source": REPORT_FILENAMES[name],
            "source_sha256": source_hash,
        }
    raise CiError("unsupported-report-name")


def _write_provenance(
    report_dir: Path,
    expect: list[str],
    records: list[dict[str, Any]],
    errors: list[str],
) -> Path:
    upload = report_dir / "upload"
    for record in records:
        path = upload / record["export"]
        if path.is_file() and not path.is_symlink():
            record["export_sha256"] = sha256_file(path)
    provenance = {
        "schema": SAFE_EXPORT_SCHEMA,
        "generated_at": utc_now(),
        "expect": expect,
        "reports": records,
        "errors": sorted(set(errors)),
    }
    return _write_json_report(upload, "provenance.json", provenance)


def export_reports(
    report_dir: str,
    db_artifact_root: str,
    expect: Sequence[str] | None = None,
) -> int:
    """Reconstruct safe upload reports and a provenance manifest."""
    report_dir_path = Path(report_dir)
    db_root_path = Path(db_artifact_root)
    if not report_dir_path.is_absolute() or not db_root_path.is_absolute():
        print("ERROR: export/relative-path", file=sys.stderr)
        print("result: fail stage=export kind=path")
        return 1
    requested = list(expect) if expect is not None else ["default"]
    if not requested or any(name not in EXPECT_NAMES for name in requested):
        print("ERROR: export/unknown-expect", file=sys.stderr)
        print("result: fail stage=export kind=expect")
        return 1
    try:
        report_dir_path.mkdir(parents=True, exist_ok=True)
    except OSError:
        print("ERROR: export/report-dir", file=sys.stderr)
        print("result: fail stage=export kind=report-dir")
        return 1
    if report_dir_path.is_symlink() or not report_dir_path.is_dir():
        print("ERROR: export/unsafe-report-dir", file=sys.stderr)
        print("result: fail stage=export kind=report-dir")
        return 1
    upload = report_dir_path / "upload"
    if upload.is_symlink() or (upload.exists() and not upload.is_dir()):
        print("ERROR: export/unsafe-upload", file=sys.stderr)
        print("result: fail stage=export kind=upload")
        return 1
    try:
        upload.mkdir(parents=True, exist_ok=True)
    except OSError:
        print("ERROR: export/upload-create", file=sys.stderr)
        print("result: fail stage=export kind=upload")
        return 1

    records: list[dict[str, Any]] = []
    errors: list[str] = []
    all_ok = True
    for name in requested:
        safe_name = SAFE_FILENAMES[name]
        record: dict[str, Any] = {
            "name": name,
            "source": REPORT_FILENAMES[name],
            "status": "missing",
            "export": safe_name,
            "result": "missing",
        }
        try:
            safe, source_info = _export_one(
                name, report_dir_path, db_root_path
            )
            if source_info is not None:
                record.update(source_info)
            record["status"] = safe["status"]
            record["result"] = safe.get("result", safe["status"])
            _write_json_report(upload, safe_name, safe)
        except CiError as exc:
            safe = _error_report(name, exc.category)
            record["status"] = "error"
            record["result"] = "error"
            record["error"] = exc.category
            try:
                _write_json_report(upload, safe_name, safe)
            except CiError as write_exc:
                errors.append(write_exc.category)
            errors.append(exc.category)
            all_ok = False
        else:
            if safe["status"] == "missing":
                all_ok = False
            elif safe["status"] == "unsupported":
                all_ok = False
            elif safe.get("result") != "pass":
                all_ok = False
        records.append(record)
    try:
        provenance = _write_provenance(
            report_dir_path, requested, records, errors
        )
    except CiError as exc:
        print(f"ERROR: {exc.category}", file=sys.stderr)
        print("result: fail stage=export kind=provenance")
        return 1
    print(f"upload-dir: {report_dir_path / 'upload'}")
    for record in records:
        print(
            "safe-report: "
            + record["name"]
            + "="
            + record["status"]
            + " result="
            + str(record["result"])
        )
    print(f"safe-manifest: {provenance}")
    print("result: " + ("pass" if all_ok and not errors else "fail"))
    return 0 if all_ok and not errors else 1


def _parse_expect(values: Sequence[str] | None) -> list[str]:
    if not values:
        return ["default"]
    parsed: list[str] = []
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if item not in EXPECT_NAMES:
                raise CiError("unknown-expect")
            if item not in parsed:
                parsed.append(item)
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Strict CI JSON gate helper: bounded default go test -json run and "
            "safe whitelist report export."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run-default")
    run_parser.add_argument("--go", required=True)
    run_parser.add_argument("--report-dir", required=True)
    export_parser = subparsers.add_parser("export-reports")
    export_parser.add_argument("--report-dir", required=True)
    export_parser.add_argument("--db-artifact-root", required=True)
    export_parser.add_argument(
        "--expect",
        action="append",
        default=None,
        help="Comma/repeated list of expected reports; default: default",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "run-default":
            return run_default(args.go, args.report_dir)
        try:
            expect = _parse_expect(args.expect)
        except CiError:
            parser.error("unknown --expect value")
        return export_reports(
            args.report_dir, args.db_artifact_root, expect
        )
    except CiError as exc:
        print(f"ERROR: {exc.category}", file=sys.stderr)
        print("result: fail stage=ci-checks kind=" + exc.category)
        return 1
    except Exception:
        print("ERROR: internal", file=sys.stderr)
        print("result: fail stage=internal kind=unexpected")
        return 1


if __name__ == "__main__":
    sys.exit(main())
