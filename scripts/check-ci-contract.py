#!/usr/bin/env python3
"""Safe CI contract preflight for the payment-hub workflow.

The preflight is deliberately project-specific and standard-library only.  It
validates the one approved workflow shape, the source-manifest coverage, the
launcher/default inventory cross-links, and two read-only ``go list`` metadata
passes.  It never runs tests, starts PostgreSQL/Docker, downloads modules, or
prints raw tool output.  Unsupported YAML shapes fail closed instead of being
silently ignored.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_RELATIVE = Path(".github/workflows/setup-check.yml")
APPROVED_WORKFLOW_FILES = ("setup-check.yml",)
EXPECTED_MODULE = "payment-hub"
METADATA_TIMEOUT_SECONDS = 90

REQUIRED_SOURCE_GLOBS = (
    "internal/**/*.go",
    "tests/**/*.go",
    "cmd/**/*.go",
    "scripts/*.py",
    "migrations/*.sql",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
)
FORBIDDEN_MANIFEST_PREFIXES = (".git/", "artifacts/", ".slim/")

EXPECTED_JOBS = (
    "setup-check",
    "database-foundation",
    "vulnerability-check",
    "ci-gate",
)
EXPECTED_JOB_NAMES = {
    "setup-check": "Setup and foundation units (linux/amd64)",
    "database-foundation": (
        "PostgreSQL migration, constraints, and runtime harness (linux/amd64)"
    ),
    "vulnerability-check": "Vulnerability scan (linux/amd64, read-only)",
    "ci-gate": "CI gate",
}
EXPECTED_TIMEOUTS = {
    "setup-check": 20,
    "database-foundation": 20,
    "vulnerability-check": 10,
    "ci-gate": 1,
}
CONCURRENCY_GROUP = (
    "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}"
)
EXPECTED_TOP_ENV = {
    "GOTOOLCHAIN": "local",
    "GOENV": "off",
    "GOWORK": "off",
    "GOFLAGS": "",
    "PYTHONDONTWRITEBYTECODE": "1",
}
CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_GO = "actions/setup-go@b7ad1dad31e06c5925ef5d2fc7ad053ef454303e"
UPLOAD = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
EXPECTED_ACTION_PINS = {CHECKOUT, SETUP_GO, UPLOAD}
EXPECTED_ACTION_COUNTS = {CHECKOUT: 3, SETUP_GO: 3, UPLOAD: 3}
GATE_RUN = """set -euo pipefail
if [[ "${SETUP_RESULT:-}" != "success" || "${DATABASE_RESULT:-}" != "success" || "${VULNERABILITY_RESULT:-}" != "success" ]]; then
  echo "ci-gate: one or more required jobs did not succeed"
  exit 1
fi
echo "ci-gate: all required jobs succeeded"
exit 0"""
EXPECTED_UPLOADS = {
    "setup-check": [
        "${{ runner.temp }}/payment-hub-setup/reports/upload/go-default.safe.json",
        "${{ runner.temp }}/payment-hub-setup/reports/upload/provenance.json",
    ],
    "database-foundation": [
        "${{ runner.temp }}/payment-hub-db/reports/upload/db-foundation.safe.json",
        "${{ runner.temp }}/payment-hub-db/reports/upload/cancellation.safe.json",
        "${{ runner.temp }}/payment-hub-db/reports/upload/us1.safe.json",
        "${{ runner.temp }}/payment-hub-db/reports/upload/us2.safe.json",
        "${{ runner.temp }}/payment-hub-db/reports/upload/provenance.json",
    ],
    "vulnerability-check": [
        "${{ runner.temp }}/payment-hub-vulnerability/reports/upload/vulnerability.safe.json",
        "${{ runner.temp }}/payment-hub-vulnerability/reports/upload/provenance.json",
    ],
}
EXPECTED_EXPORTS = {
    "setup-check": "--expect default",
    "database-foundation": "--expect db,cancellation,us1,us2",
    "vulnerability-check": "--expect vulnerability",
}
FORBIDDEN_WORKFLOW_TOKENS = (
    "26 个",
    "38 个",
    "39 个",
    "42 个",
    "40 个",
    "十一个",
    "十八个",
    "TestSubmitPayment",
    "TestUS1Payment",
    "TestPaymentIdempotency",
    "TestUS2Replay",
    "TestOrderRegistrationConcurrencyIntegration",
    "TestApplicationLifecycleIntegration",
    "TestPaymentRequestBoundary/POST_rejection_priority",
)


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ContractError("internal-module")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


CI_CHECKS = _load_module(
    "ci_checks_for_ci_contract", REPO_ROOT / "scripts" / "ci_checks.py"
)
DB_FOUNDATION = _load_module(
    "check_db_foundation_for_ci_contract",
    REPO_ROOT / "scripts" / "check-db-foundation.py",
)


@dataclass(frozen=True)
class ContractIssue:
    kind: str
    package: str | None = None

    def safe_line(self) -> str:
        if self.package:
            return f"ci-contract-failure: kind={self.kind} package={self.package}"
        return f"ci-contract-failure: kind={self.kind}"


class ContractError(RuntimeError):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(kind)


# ---------------------------------------------------------------------------
# Project-specific workflow parser (not a general YAML parser)
# ---------------------------------------------------------------------------


def _tokenize_workflow(text: str) -> list[tuple[int, str, int]]:
    lines: list[tuple[int, str, int]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if "\t" in line:
            raise ContractError("workflow-syntax")
        if not line.strip():
            continue
        stripped = line.lstrip(" ")
        if stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        if indent % 2:
            raise ContractError("workflow-syntax")
        lines.append((indent, stripped, number))
    return lines


def _strip_inline_comment(value: str) -> str:
    in_single = False
    in_double = False
    for index, character in enumerate(value):
        if character == "'" and not in_double:
            in_single = not in_single
        elif character == '"' and not in_single:
            in_double = not in_double
        elif (
            character == "#"
            and not in_single
            and not in_double
            and (index == 0 or value[index - 1].isspace())
        ):
            return value[:index].rstrip()
    return value


def _split_kv(content: str, number: int) -> tuple[str, str | None]:
    match = re.fullmatch(r"([A-Za-z0-9_.-]+):(?: (.*))?", content)
    if match is None:
        raise ContractError("workflow-syntax")
    value = match.group(2)
    if value is not None:
        value = _strip_inline_comment(value)
    if value == "":
        value = None
    return match.group(1), value


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_bool(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ContractError("workflow-syntax")


def _parse_scalar(key: str, value: str) -> Any:
    if key in ("timeout-minutes", "retention-days"):
        try:
            return int(value)
        except ValueError:
            raise ContractError("workflow-syntax") from None
    if key in (
        "cancel-in-progress",
        "include-hidden-files",
        "continue-on-error",
        "check-latest",
        "cache",
    ):
        return _parse_bool(value)
    if key == "needs":
        if not (value.startswith("[") and value.endswith("]")):
            return _unquote(value)
        inner = value[1:-1].strip()
        if not inner:
            raise ContractError("workflow-syntax")
        return [item.strip() for item in inner.split(",") if item.strip()]
    return _unquote(value)


def _capture_block(
    lines: list[tuple[int, str, int]], index: int, indent: int
) -> tuple[str, int]:
    block: list[str] = []
    while index < len(lines) and lines[index][0] >= indent:
        current_indent, content, _ = lines[index]
        block.append(" " * (current_indent - indent) + content)
        index += 1
    if not block:
        raise ContractError("workflow-syntax")
    return "\n".join(block), index


def _parse_mapping(
    lines: list[tuple[int, str, int]], index: int, indent: int
) -> tuple[dict[str, Any], int]:
    mapping: dict[str, Any] = {}
    while index < len(lines) and lines[index][0] >= indent:
        current_indent, content, number = lines[index]
        if current_indent != indent:
            raise ContractError("workflow-syntax")
        key, value = _split_kv(content, number)
        if key in mapping:
            raise ContractError("workflow-syntax")
        if value == "|":
            block, index = _capture_block(lines, index + 1, indent + 2)
            mapping[key] = block
        elif value is None:
            raise ContractError("workflow-syntax")
        else:
            mapping[key] = _parse_scalar(key, value)
            index += 1
    return mapping, index


def _parse_on(
    lines: list[tuple[int, str, int]], index: int
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines) and lines[index][0] > 0:
        indent, content, number = lines[index]
        if indent != 2:
            raise ContractError("workflow-syntax")
        event, value = _split_kv(content, number)
        if event not in ("push", "pull_request") or value is not None:
            raise ContractError("workflow-syntax")
        if event in result:
            raise ContractError("workflow-syntax")
        index += 1
        event_body: dict[str, Any] = {}
        while index < len(lines) and lines[index][0] > 2:
            inner_indent, inner_content, inner_number = lines[index]
            if inner_indent != 4:
                raise ContractError("workflow-syntax")
            key, key_value = _split_kv(inner_content, inner_number)
            if key != "branches" or key_value is not None:
                raise ContractError("workflow-syntax")
            index += 1
            branches: list[str] = []
            while index < len(lines) and lines[index][0] > 4:
                branch_indent, branch_content, _ = lines[index]
                if branch_indent != 6 or not branch_content.startswith("- "):
                    raise ContractError("workflow-syntax")
                branches.append(_unquote(branch_content[2:].strip()))
                index += 1
            if not branches:
                raise ContractError("workflow-syntax")
            event_body["branches"] = branches
        result[event] = event_body
    return result, index


def _parse_step(
    lines: list[tuple[int, str, int]], index: int
) -> tuple[dict[str, Any], int]:
    indent, content, _ = lines[index]
    if indent != 6:
        raise ContractError("workflow-syntax")
    match = re.fullmatch(r"- name: (.*)", content)
    if match is None:
        raise ContractError("workflow-syntax")
    step: dict[str, Any] = {"name": _unquote(match.group(1))}
    index += 1
    while index < len(lines) and lines[index][0] > 6:
        current_indent, current_content, number = lines[index]
        if current_indent != 8:
            raise ContractError("workflow-syntax")
        key, value = _split_kv(current_content, number)
        if key in step:
            raise ContractError("workflow-syntax")
        if key == "run":
            if value != "|":
                raise ContractError("workflow-syntax")
            block, index = _capture_block(lines, index + 1, 10)
            step["run"] = block
        elif key in ("with", "env"):
            if value is not None:
                raise ContractError("workflow-syntax")
            mapping, index = _parse_mapping(lines, index + 1, 10)
            step[key] = mapping
        elif key in ("shell", "if", "uses", "continue-on-error"):
            if value is None:
                raise ContractError("workflow-syntax")
            step[key] = _parse_scalar(key, value)
            index += 1
        else:
            raise ContractError("workflow-syntax")
    if not step["name"]:
        raise ContractError("workflow-syntax")
    return step, index


def _parse_job(
    lines: list[tuple[int, str, int]], index: int
) -> tuple[dict[str, Any], int]:
    job: dict[str, Any] = {}
    while index < len(lines) and lines[index][0] > 2:
        indent, content, number = lines[index]
        if indent != 4:
            raise ContractError("workflow-syntax")
        key, value = _split_kv(content, number)
        if key in job:
            raise ContractError("workflow-syntax")
        if key == "steps":
            if value is not None:
                raise ContractError("workflow-syntax")
            index += 1
            steps: list[dict[str, Any]] = []
            while index < len(lines) and lines[index][0] > 4:
                if lines[index][0] != 6:
                    raise ContractError("workflow-syntax")
                step, index = _parse_step(lines, index)
                steps.append(step)
            job["steps"] = steps
        elif key in ("env", "permissions"):
            if value is not None:
                raise ContractError("workflow-syntax")
            mapping, index = _parse_mapping(lines, index + 1, 6)
            job[key] = mapping
        elif key in ("name", "runs-on", "timeout-minutes", "needs", "if"):
            if value is None:
                raise ContractError("workflow-syntax")
            job[key] = _parse_scalar(key, value)
            index += 1
        else:
            raise ContractError("workflow-syntax")
    return job, index


def _parse_jobs(
    lines: list[tuple[int, str, int]], index: int
) -> tuple[dict[str, Any], int]:
    jobs: dict[str, Any] = {}
    while index < len(lines) and lines[index][0] > 0:
        indent, content, number = lines[index]
        if indent != 2:
            raise ContractError("workflow-syntax")
        job_id, value = _split_kv(content, number)
        if value is not None or job_id in jobs:
            raise ContractError("workflow-syntax")
        index += 1
        job, index = _parse_job(lines, index)
        jobs[job_id] = job
    return jobs, index


def parse_workflow_text(text: str) -> dict[str, Any]:
    lines = _tokenize_workflow(text)
    workflow: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        indent, content, number = lines[index]
        if indent != 0:
            raise ContractError("workflow-syntax")
        key, value = _split_kv(content, number)
        if key in workflow:
            raise ContractError("workflow-syntax")
        if key == "name":
            if value is None:
                raise ContractError("workflow-syntax")
            workflow["name"] = _unquote(value)
            index += 1
        elif key == "on":
            if value is not None:
                raise ContractError("workflow-syntax")
            on_value, index = _parse_on(lines, index + 1)
            workflow["on"] = on_value
        elif key in ("permissions", "env", "concurrency"):
            if value is not None:
                raise ContractError("workflow-syntax")
            mapping, index = _parse_mapping(lines, index + 1, 2)
            workflow[key] = mapping
        elif key == "jobs":
            if value is not None:
                raise ContractError("workflow-syntax")
            jobs, index = _parse_jobs(lines, index + 1)
            workflow["jobs"] = jobs
        else:
            raise ContractError("workflow-syntax")
    return workflow


def load_workflow(path: Path) -> tuple[dict[str, Any], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise ContractError("workflow-missing") from None
    return parse_workflow_text(text), text


def _step_names(job: dict[str, Any]) -> list[str]:
    return [step.get("name", "") for step in job.get("steps", [])]


def _find_step(job: dict[str, Any], name: str) -> dict[str, Any] | None:
    for step in job.get("steps", []):
        if step.get("name") == name:
            return step
    return None


def _upload_paths(step: dict[str, Any]) -> list[str]:
    return [
        line.strip()
        for line in step.get("with", {}).get("path", "").splitlines()
        if line.strip()
    ]


def check_workflow_policy(repo: Path = REPO_ROOT) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    workflows_dir = repo / ".github" / "workflows"
    if not workflows_dir.is_dir():
        issues.append(ContractIssue("workflow-policy"))
        return issues
    actual = sorted(
        path.name
        for path in workflows_dir.iterdir()
        if path.is_file() and path.suffix in (".yml", ".yaml")
    )
    if tuple(actual) != APPROVED_WORKFLOW_FILES:
        issues.append(ContractIssue("workflow-policy"))
    return issues


def validate_workflow(
    workflow: dict[str, Any], text: str, repo: Path = REPO_ROOT
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []

    def add(kind: str, package: str | None = None) -> None:
        issues.append(ContractIssue(kind, package))

    if set(workflow) - {"name", "on", "concurrency", "permissions", "env", "jobs"}:
        add("workflow-top-level")
    if workflow.get("on") != {
        "push": {"branches": ["main"]},
        "pull_request": {"branches": ["main"]},
    }:
        add("workflow-triggers")
    if workflow.get("permissions") != {"contents": "read"}:
        add("workflow-permissions")
    concurrency = workflow.get("concurrency", {})
    if concurrency != {
        "group": CONCURRENCY_GROUP,
        "cancel-in-progress": False,
    }:
        add("workflow-concurrency")
    if workflow.get("env") != EXPECTED_TOP_ENV:
        add("workflow-env")

    jobs = workflow.get("jobs", {})
    if tuple(jobs) != EXPECTED_JOBS:
        add("workflow-jobs")
    for job_id in EXPECTED_JOBS:
        job = jobs.get(job_id)
        if not isinstance(job, dict):
            add("workflow-jobs", job_id)
            continue
        if job.get("name") != EXPECTED_JOB_NAMES[job_id]:
            add("workflow-job-name", job_id)
        if job.get("runs-on") != "ubuntu-24.04":
            add("workflow-runs-on", job_id)
        if job.get("timeout-minutes") != EXPECTED_TIMEOUTS[job_id]:
            add("workflow-timeout", job_id)
        if "permissions" in job:
            add("workflow-job-permissions", job_id)
        if "if" in job and job_id != "ci-gate":
            add("workflow-job-condition", job_id)
        for step in job.get("steps", []):
            if "continue-on-error" in step and step["continue-on-error"] is not False:
                add("workflow-continue-on-error", job_id)

    if "needs" in jobs.get("setup-check", {}):
        add("workflow-setup-needs")
    if jobs.get("database-foundation", {}).get("needs") != "setup-check":
        add("workflow-database-needs")
    if "needs" in jobs.get("vulnerability-check", {}):
        add("workflow-vulnerability-needs")

    uses = [
        step.get("uses")
        for job in jobs.values()
        for step in job.get("steps", [])
        if "uses" in step
    ]
    if set(uses) != EXPECTED_ACTION_PINS:
        add("workflow-action-pins")
    for pin, count in EXPECTED_ACTION_COUNTS.items():
        if uses.count(pin) != count:
            add("workflow-action-pins")

    setup = jobs.get("setup-check", {})
    setup_names = _step_names(setup)
    for required in (
        "Run setup checks",
        "Check CI contract metadata",
        "Run foundation unit checks",
    ):
        if required not in setup_names:
            add("workflow-step-missing", required)
    if all(
        name in setup_names
        for name in (
            "Run setup checks",
            "Check CI contract metadata",
            "Run foundation unit checks",
        )
    ):
        if not (
            setup_names.index("Run setup checks")
            < setup_names.index("Check CI contract metadata")
            < setup_names.index("Run foundation unit checks")
        ):
            add("workflow-preflight-order")
    preflight = _find_step(setup, "Check CI contract metadata")
    if preflight is None:
        add("workflow-preflight-missing")
    else:
        if preflight.get("shell") != "bash":
            add("workflow-preflight-shell")
        if preflight.get("env") != {
            "CGO_ENABLED": "1",
            "GOMAXPROCS": "2",
            "GOPROXY": "off",
            "GOSUMDB": "off",
        }:
            add("workflow-preflight-env")
        if (
            'python3 -B scripts/check-ci-contract.py --go "$(command -v go)"'
            not in preflight.get("run", "")
        ):
            add("workflow-preflight-command")
        if "set -euo pipefail" not in preflight.get("run", ""):
            add("workflow-preflight-command")
        if "|| true" in preflight.get("run", ""):
            add("workflow-preflight-command")

    foundation = _find_step(setup, "Run foundation unit checks")
    if foundation is not None and (
        "python3 -B scripts/check-ci-contract.py --summary default"
        not in foundation.get("run", "")
    ):
        add("workflow-summary-default")
    database = jobs.get("database-foundation", {})
    database_summary = _find_step(
        database, "Summarize database foundation results"
    )
    if database_summary is not None and (
        "python3 -B scripts/check-ci-contract.py --summary db"
        not in database_summary.get("run", "")
    ):
        add("workflow-summary-db")
    for token in FORBIDDEN_WORKFLOW_TOKENS:
        if token in text:
            add("workflow-hardcoded-inventory")

    database_run = _find_step(database, "Run database foundation checks")
    if database_run is None:
        add("workflow-db-run-missing")
    else:
        for flag in (
            "--download-modules",
            "--prepare-images",
            "--cleanup-owned-resources",
            "--artifact-root",
        ):
            if flag not in database_run.get("run", ""):
                add("workflow-db-flag")
        if "--cleanup-owned-resources" not in database_run.get("run", ""):
            add("workflow-db-cleanup")
    cancellation = _find_step(
        database, "Verify cancellation cleanup with real PostgreSQL"
    )
    if cancellation is None or "check-ci-cancellation.py" not in cancellation.get(
        "run", ""
    ):
        add("workflow-cancellation")

    for job_id, expected_export in EXPECTED_EXPORTS.items():
        job = jobs.get(job_id, {})
        export_steps = [
            step
            for step in job.get("steps", [])
            if str(step.get("name", "")).startswith("Export safe")
        ]
        if len(export_steps) != 1:
            add("workflow-export", job_id)
        elif expected_export not in export_steps[0].get("run", ""):
            add("workflow-export", job_id)
        uploads = [
            step
            for step in job.get("steps", [])
            if step.get("uses") == UPLOAD
        ]
        if len(uploads) != 1:
            add("workflow-upload", job_id)
            continue
        upload = uploads[0]
        if upload.get("if") != "always()":
            add("workflow-upload", job_id)
        if upload.get("with", {}).get("if-no-files-found") != "error":
            add("workflow-upload", job_id)
        if upload.get("with", {}).get("retention-days") != 14:
            add("workflow-upload", job_id)
        if upload.get("with", {}).get("include-hidden-files") is not False:
            add("workflow-upload", job_id)
        paths = _upload_paths(upload)
        if paths != EXPECTED_UPLOADS[job_id]:
            add("workflow-upload-paths", job_id)
        for path in paths:
            if not re.search(
                r"reports/upload/(?:[a-z0-9-]+\.safe\.json|provenance\.json)$",
                path,
            ):
                add("workflow-upload-paths", job_id)
            if "/artifacts/" in path or "check-db-foundation.log" in path:
                add("workflow-upload-paths", job_id)

    vulnerability = jobs.get("vulnerability-check", {})
    scan = _find_step(vulnerability, "Run pinned vulnerability scan")
    if scan is None:
        add("workflow-vulnerability-scan")
    else:
        if "check-vulnerabilities.py" not in scan.get("run", ""):
            add("workflow-vulnerability-scan")
        if "|| true" in scan.get("run", ""):
            add("workflow-vulnerability-scan")
        if "continue-on-error" in scan:
            add("workflow-vulnerability-scan")

    gate = jobs.get("ci-gate", {})
    if gate.get("name") != "CI gate":
        add("workflow-gate-name")
    if gate.get("runs-on") != "ubuntu-24.04":
        add("workflow-gate-runs-on")
    if gate.get("timeout-minutes") != 1:
        add("workflow-gate-timeout")
    if gate.get("if") != "${{ always() }}":
        add("workflow-gate-always")
    gate_needs = gate.get("needs")
    if not isinstance(gate_needs, list):
        add("workflow-gate-needs")
    else:
        if set(gate_needs) != set(jobs) - {"ci-gate"}:
            add("workflow-gate-needs")
    if gate.get("env") != {
        "SETUP_RESULT": "${{ needs.setup-check.result }}",
        "DATABASE_RESULT": "${{ needs.database-foundation.result }}",
        "VULNERABILITY_RESULT": "${{ needs.vulnerability-check.result }}",
    }:
        add("workflow-gate-env")
    gate_steps = gate.get("steps", [])
    if len(gate_steps) != 1:
        add("workflow-gate-steps")
    else:
        step = gate_steps[0]
        if step.get("name") != "Require all CI jobs to succeed":
            add("workflow-gate-steps")
        if step.get("shell") != "bash":
            add("workflow-gate-steps")
        if step.get("run", "").strip() != GATE_RUN:
            add("workflow-gate-run")
        if "uses" in step or "with" in step or "if" in step:
            add("workflow-gate-actions")

    if "workflow_dispatch" in text or "pull_request_target" in text:
        add("workflow-forbidden-trigger")
    if "secrets." in text:
        add("workflow-secrets")
    if re.search(r"(?m)^\s*[A-Za-z0-9_-]+:\s*write\s*$", text):
        add("workflow-write-permissions")
    if "workflow_call" in text:
        add("workflow-forbidden-trigger")

    issues.extend(check_workflow_policy(repo))
    return issues


# ---------------------------------------------------------------------------
# Manifest, binding, and metadata contracts
# ---------------------------------------------------------------------------


def expand_manifest(
    repo: Path = REPO_ROOT,
    explicit: Sequence[str] | None = None,
    globs: Sequence[str] | None = None,
) -> set[str]:
    explicit_paths = (
        DB_FOUNDATION.SOURCE_MANIFEST_EXPLICIT
        if explicit is None
        else tuple(explicit)
    )
    patterns = (
        DB_FOUNDATION.SOURCE_MANIFEST_GLOBS if globs is None else tuple(globs)
    )
    paths = set(explicit_paths)
    for pattern in patterns:
        for path in repo.glob(pattern):
            if path.is_file():
                paths.add(path.relative_to(repo).as_posix())
    return paths


def check_manifest_coverage(
    repo: Path = REPO_ROOT,
    explicit: Sequence[str] | None = None,
    globs: Sequence[str] | None = None,
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    patterns = tuple(
        DB_FOUNDATION.SOURCE_MANIFEST_GLOBS if globs is None else globs
    )
    for required in REQUIRED_SOURCE_GLOBS:
        if required not in patterns:
            issues.append(ContractIssue("manifest-missing-glob"))
    for pattern in patterns:
        normalized = pattern[2:] if pattern.startswith("./") else pattern
        if normalized.startswith(FORBIDDEN_MANIFEST_PREFIXES):
            issues.append(ContractIssue("manifest-forbidden-glob"))
    paths = expand_manifest(repo, explicit, patterns)
    for relative in sorted(paths):
        if relative.startswith(FORBIDDEN_MANIFEST_PREFIXES):
            issues.append(ContractIssue("manifest-forbidden-path"))
        if not (repo / relative).is_file():
            issues.append(ContractIssue("manifest-missing"))
    actual: set[str] = set()
    for pattern in REQUIRED_SOURCE_GLOBS:
        for path in repo.glob(pattern):
            if path.is_file():
                actual.add(path.relative_to(repo).as_posix())
    for relative in sorted(actual):
        if relative not in paths:
            issues.append(ContractIssue("manifest-uncovered"))
    return issues


def check_binding_inventory(
    packages: Sequence[str] | None = None,
    package_keys: Sequence[str] | None = None,
    required_keys: Sequence[str] | None = None,
    bindings: dict[str, str] | None = None,
    expected_paths: Sequence[str] | None = None,
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    package_values = (
        tuple(DB_FOUNDATION.PACKAGES) if packages is None else tuple(packages)
    )
    package_key_values = (
        tuple(CI_CHECKS.DB_PACKAGE_KEYS)
        if package_keys is None
        else tuple(package_keys)
    )
    required_key_values = (
        tuple(CI_CHECKS.DB_REQUIRED_TEST_KEYS)
        if required_keys is None
        else tuple(required_keys)
    )
    binding_values = (
        dict(DB_FOUNDATION.REQUIRED_TEST_PACKAGES)
        if bindings is None
        else dict(bindings)
    )
    path_values = (
        tuple(DB_FOUNDATION.EXPECTED_PACKAGE_PATHS)
        if expected_paths is None
        else tuple(expected_paths)
    )
    if DB_FOUNDATION.EXPECTED_MODULE != EXPECTED_MODULE:
        issues.append(ContractIssue("binding-module-mismatch"))
    if len(set(package_values)) != len(package_values):
        issues.append(ContractIssue("binding-packages-duplicate"))
    if set(package_values) != set(package_key_values):
        issues.append(ContractIssue("binding-packages-mismatch"))
    if tuple(binding_values) != required_key_values:
        issues.append(ContractIssue("binding-keys-mismatch"))
    if set(binding_values) != set(required_key_values):
        issues.append(ContractIssue("binding-keys-mismatch"))
    computed_paths = tuple(
        f"{DB_FOUNDATION.EXPECTED_MODULE}/{name}" for name in package_values
    )
    if computed_paths != path_values:
        issues.append(ContractIssue("binding-package-paths-mismatch"))
    for name, package in binding_values.items():
        if not isinstance(name, str) or not name:
            issues.append(ContractIssue("binding-keys-mismatch"))
        if package not in computed_paths:
            issues.append(
                ContractIssue("binding-package-unselected", str(package))
            )
    return issues


def parse_go_list_output(data: bytes | str) -> list[dict[str, Any]]:
    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ContractError("metadata-json") from None
    else:
        text = data
    decoder = json.JSONDecoder()
    entries: list[dict[str, Any]] = []
    index = 0
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        try:
            value, index = decoder.raw_decode(text, index)
        except ValueError:
            raise ContractError("metadata-json") from None
        if not isinstance(value, dict):
            raise ContractError("metadata-json")
        entries.append(value)
    if not entries:
        raise ContractError("metadata-empty")
    return entries


def _package_files(entry: dict[str, Any]) -> tuple[str, set[str], set[str], set[str]]:
    import_path = entry.get("ImportPath")
    if not isinstance(import_path, str) or not import_path:
        raise ContractError("metadata-json")

    def files(name: str) -> set[str]:
        value = entry.get(name, [])
        if value is None:
            return set()
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise ContractError("metadata-json")
        return set(value)

    return import_path, files("GoFiles"), files("TestGoFiles"), files(
        "XTestGoFiles"
    )


def _local_metadata(
    entries: Sequence[dict[str, Any]], module: str
) -> dict[str, dict[str, set[str]]]:
    result: dict[str, dict[str, set[str]]] = {}
    for entry in entries:
        import_path, go_files, test_files, xtest_files = _package_files(entry)
        if import_path != module and not import_path.startswith(module + "/"):
            continue
        if import_path in result:
            raise ContractError("metadata-duplicate-package")
        result[import_path] = {
            "go": go_files,
            "test": test_files,
            "xtest": xtest_files,
        }
    if not result:
        raise ContractError("metadata-empty")
    return result


def _relative_package(import_path: str, module: str) -> str:
    if import_path == module:
        return ""
    return import_path[len(module) + 1 :]


def check_metadata_contract(
    default_entries: Sequence[dict[str, Any]],
    integration_entries: Sequence[dict[str, Any]],
    packages: Sequence[str] | None = None,
    module: str = EXPECTED_MODULE,
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    selected = (
        tuple(DB_FOUNDATION.PACKAGES) if packages is None else tuple(packages)
    )
    default_map = _local_metadata(default_entries, module)
    integration_map = _local_metadata(integration_entries, module)
    for relative in selected:
        import_path = f"{module}/{relative}"
        # A selected package may legitimately be integration-only (for example
        # payment-hub/tests).  It must still appear in the integration listing;
        # a selected package absent from both listings is never silently ok.
        if import_path not in integration_map:
            issues.append(ContractIssue("metadata-package-missing", relative))
    for import_path, integration in integration_map.items():
        relative = _relative_package(import_path, module)
        default = default_map.get(import_path)
        if default is None:
            all_files = integration["go"] | integration["test"] | integration["xtest"]
            if all_files and relative not in selected:
                issues.append(
                    ContractIssue(
                        "metadata-unselected-integration-package", relative
                    )
                )
        else:
            added = (
                integration["go"] | integration["test"] | integration["xtest"]
            ) - (default["go"] | default["test"] | default["xtest"])
            if added and relative not in selected:
                issues.append(
                    ContractIssue(
                        "metadata-unselected-integration-files", relative
                    )
                )
    for import_path in default_map:
        if import_path not in integration_map:
            issues.append(
                ContractIssue(
                    "metadata-package-disappeared",
                    _relative_package(import_path, module),
                )
            )
    return issues


def validate_go(go: str) -> str:
    if not isinstance(go, str) or not go:
        raise ContractError("metadata-go-invalid")
    path = Path(go)
    if not path.is_absolute() or not path.is_file():
        raise ContractError("metadata-go-invalid")
    if not os.access(go, os.X_OK):
        raise ContractError("metadata-go-invalid")
    return go


def run_go_list(go: str, repo: Path, tags: str | None = None) -> bytes:
    validate_go(go)
    argv = [go, "list", "-mod=readonly"]
    if tags:
        argv.append("-tags=" + tags)
    argv.extend(["-json", "./..."])
    env = dict(os.environ)
    env.update(
        {
            "GO111MODULE": "on",
            "GOTOOLCHAIN": "local",
            "GOWORK": "off",
            "GOFLAGS": "",
            "GOENV": "off",
            "GOPROXY": "off",
            "GOSUMDB": "off",
            "CGO_ENABLED": "1",
            "GOMAXPROCS": "2",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    try:
        result = subprocess.run(
            argv,
            cwd=repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=METADATA_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ContractError("metadata-go-list") from None
    if result.returncode != 0:
        raise ContractError("metadata-go-list")
    return result.stdout


def build_summary(kind: str) -> str:
    if kind == "default":
        mapping = CI_CHECKS.DEFAULT_REQUIRED_TESTS
        if not isinstance(mapping, dict) or not mapping:
            raise ContractError("summary-inventory")
        roots = {name.split("/", 1)[0] for name in mapping}
        packages = sorted(set(mapping.values()))
        subtests = sorted(name for name in mapping if "/" in name)
        lines = [
            "summary: default",
            f"root-count: {len(roots)}",
            f"required-check-count: {len(mapping)}",
            f"package-count: {len(packages)}",
            "packages: " + ", ".join(packages),
            "focus-subtests: " + (", ".join(subtests) if subtests else "none"),
            "required-checks:",
        ]
        lines.extend(f"- {name}" for name in sorted(mapping))
        return "\n".join(lines) + "\n"
    if kind == "db":
        keys = tuple(CI_CHECKS.DB_REQUIRED_TEST_KEYS)
        bindings = dict(DB_FOUNDATION.REQUIRED_TEST_PACKAGES)
        packages = tuple(DB_FOUNDATION.PACKAGES)
        if set(keys) != set(bindings):
            raise ContractError("summary-bindings")
        lines = [
            "summary: db",
            f"db-check-count: {len(keys)}",
            f"db-package-count: {len(packages)}",
            "db-packages: " + ", ".join(packages),
            "db-bindings:",
        ]
        for name in keys:
            package = bindings.get(name)
            if not isinstance(package, str) or not package:
                raise ContractError("summary-bindings")
            lines.append(f"- {name} -> {package}")
        lines.append(
            "go-test-batch-timeout-seconds: "
            + str(CI_CHECKS.GO_TEST_BATCH_TIMEOUT_SECONDS)
        )
        return "\n".join(lines) + "\n"
    raise ContractError("summary-kind")


def check_all(go: str, repo: Path = REPO_ROOT) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    issues.extend(check_binding_inventory())
    issues.extend(check_manifest_coverage(repo))
    try:
        workflow, text = load_workflow(repo / WORKFLOW_RELATIVE)
        issues.extend(validate_workflow(workflow, text, repo))
    except ContractError as exc:
        issues.append(ContractIssue(exc.kind))
    if issues:
        return issues
    default_output = run_go_list(go, repo)
    integration_output = run_go_list(go, repo, tags="integration")
    issues.extend(
        check_metadata_contract(
            parse_go_list_output(default_output),
            parse_go_list_output(integration_output),
        )
    )
    return issues


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the payment-hub CI workflow and Go metadata contract."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--go", help="absolute path to the prepared Go binary")
    group.add_argument("--summary", choices=("default", "db"))
    args = parser.parse_args(argv)
    try:
        if args.summary:
            sys.stdout.write(build_summary(args.summary))
            return 0
        issues = check_all(args.go, REPO_ROOT)
        if issues:
            for issue in issues:
                print(issue.safe_line(), file=sys.stderr)
            print("ci-contract: fail", file=sys.stderr)
            return 1
        print("ci-contract: pass")
        return 0
    except ContractError as exc:
        print(f"ci-contract: fail kind={exc.kind}", file=sys.stderr)
        return 1
    except Exception:
        print("ci-contract: fail kind=internal", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
