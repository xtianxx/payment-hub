#!/usr/bin/env python3
"""Safe CI contract preflight for the payment-hub workflow.

The preflight is deliberately project-specific and standard-library only.  It
validates the two approved workflow shapes, the source-manifest coverage, the
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
CHECK_RELATIVE = Path(".github/workflows/check.yml")
PUBLISH_RELATIVE = Path(".github/workflows/publish.yml")
APPROVED_WORKFLOW_FILES = ("check.yml", "publish.yml", "setup-check.yml")
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
        "${{ runner.temp }}/payment-hub-db/reports/upload/us3.safe.json",
        "${{ runner.temp }}/payment-hub-db/reports/upload/provenance.json",
    ],
    "vulnerability-check": [
        "${{ runner.temp }}/payment-hub-vulnerability/reports/upload/vulnerability.safe.json",
        "${{ runner.temp }}/payment-hub-vulnerability/reports/upload/provenance.json",
    ],
}
EXPECTED_EXPORTS = {
    "setup-check": "--expect default",
    "database-foundation": "--expect db,cancellation,us1,us2,us3",
    "vulnerability-check": "--expect vulnerability",
}

CHECK_WORKFLOW_NAME = "Paired image acceptance (linux/amd64)"
CHECK_JOB_ID = "image-acceptance"
CHECK_JOB_NAME = "Paired image acceptance (linux/amd64)"
CHECK_TIMEOUT_MINUTES = 30
CHECK_TOP_ENV = {**EXPECTED_TOP_ENV, "GOTELEMETRY": "off"}
SETUP_DOCKER = (
    "docker/setup-docker-action@3f1ab1ab0e29231562e4db4ca600c1a49653bac6"
)
CHECK_EXPECTED_ACTION_PINS = {CHECKOUT, SETUP_GO, UPLOAD, SETUP_DOCKER}
CHECK_EXPECTED_ACTION_COUNTS = {
    CHECKOUT: 1,
    SETUP_GO: 1,
    UPLOAD: 1,
    SETUP_DOCKER: 1,
}
CHECK_UPLOAD_NAME = (
    "image-acceptance-safe-reports-${{ github.run_id }}-${{ github.run_attempt }}"
)
CHECK_UPLOAD_PATHS = (
    "${{ runner.temp }}/payment-hub-images/artifacts/upload/build-summary.json",
    "${{ runner.temp }}/payment-hub-images/artifacts/upload/images-summary.json",
    "${{ runner.temp }}/payment-hub-images/artifacts/upload/handoff-preflight.safe.json",
)
CHECK_BUILDER_SCRIPT = "python3 -B scripts/build-images.py"
CHECK_HANDOFF_STEP = "Verify image handoff without publishing"
CHECK_HANDOFF_RUN = '''set -euo pipefail
python3 -B scripts/check-image-handoff.py --build-root "$RUNNER_TEMP/payment-hub-images/artifacts" --artifact-root "$RUNNER_TEMP/payment-hub-handoff-preflight" --isolated-daemon'''
DOWNLOAD = (
    "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333"
)
DOCKER_SETUP_STEP = "Set up Docker 29.9.3 with containerd image store"
DOCKER_VERIFY_STEP = "Verify Docker image store"
DOCKER_VERSION = "v29.9.3"
DOCKER_RUNTIME_BASEDIR = "${{ runner.temp }}/payment-hub-docker"
DOCKER_DAEMON_CONFIG = '{"features":{"containerd-snapshotter":true}}'
DOCKER_VERIFY_RUN = """set -euo pipefail
if [[ -z "${DOCKER_HOST:-}" ]]; then
  echo "::error::DOCKER_HOST is not set; DOCKER_CONFIG overrides would bypass the dedicated daemon"
  exit 1
fi
if [[ "$(docker version --format '{{.Server.Version}}')" != "29.9.3" ]]; then
  echo "::error::unexpected Docker server version"
  exit 1
fi
driver_status="$(docker info --format '{{range .DriverStatus}}{{index . 0}}={{index . 1}}{{"\\n"}}{{end}}')"
if ! grep -Fxq 'driver-type=io.containerd.snapshotter.v1' <<<"$driver_status"; then
  echo "::error::Docker image store is not the containerd snapshotter"
  exit 1
fi
daemon_id="$(docker info --format '{{.ID}}')"
auth_dir="$(mktemp -d)"
trap 'rm -rf "$auth_dir"' EXIT
if [[ "$(DOCKER_CONFIG="$auth_dir" docker info --format '{{.ID}}')" != "$daemon_id" ]]; then
  echo "::error::empty DOCKER_CONFIG reaches a different Docker daemon"
  exit 1
fi"""
CHECK_DOCKER_CONTEXT = "payment-hub-check"
PUBLISH_WORKFLOW_NAME = "Manual paired image publish / 人工配对镜像发布"
PUBLISH_JOBS = ("prepare", "upload")
PUBLISH_TIMEOUT_MINUTES = 30
PUBLISH_TOP_ENV = {**EXPECTED_TOP_ENV, "GOTELEMETRY": "off"}
PUBLISH_EXPECTED_ACTION_PINS = {
    CHECKOUT,
    SETUP_GO,
    UPLOAD,
    DOWNLOAD,
    SETUP_DOCKER,
}
PUBLISH_ACTION_COUNTS = {
    CHECKOUT: 2,
    SETUP_GO: 2,
    UPLOAD: 2,
    DOWNLOAD: 1,
    SETUP_DOCKER: 2,
}
PUBLISH_DOCKER_SETUP_STEP = DOCKER_SETUP_STEP
PUBLISH_DOCKER_VERIFY_STEP = DOCKER_VERIFY_STEP
PUBLISH_DOCKER_VERSION = DOCKER_VERSION
PUBLISH_DOCKER_CONTEXT = "payment-hub-release"
PUBLISH_DOCKER_RUNTIME_BASEDIR = DOCKER_RUNTIME_BASEDIR
PUBLISH_DOCKER_DAEMON_CONFIG = DOCKER_DAEMON_CONFIG
PUBLISH_DOCKER_VERIFY_RUN = DOCKER_VERIFY_RUN
PUBLISH_INPUT_NAMES = ("publish_authorized", "packages_verified")
PUBLISH_BUNDLE_PATHS = (
    "${{ runner.temp }}/payment-hub-release-prepare/bundle/pair.tar",
    "${{ runner.temp }}/payment-hub-release-prepare/bundle/descriptor.json",
    "${{ runner.temp }}/payment-hub-release-prepare/bundle/build-record.json",
    "${{ runner.temp }}/payment-hub-release-prepare/bundle/accepted-images.json",
)
PUBLISH_SUMMARY_PATH = (
    "${{ runner.temp }}/payment-hub-release-publish/release-summary.json"
)
PUBLISH_HANDOFF_PATH = (
    "${{ runner.temp }}/payment-hub-release-prepare/handoff.json"
)
PUBLISH_DOWNLOAD_PATH = "${{ runner.temp }}/payment-hub-release-handoff"
PUBLISH_PREPARE_COMMAND = "bash scripts/release.sh --stage prepare"
PUBLISH_PUBLISH_COMMAND = "bash scripts/release.sh --stage publish"
PUBLISH_CI_GATE_STEP = "Require successful main CI"
PUBLISH_CI_GATE_SUMMARY = (
    "${{ runner.temp }}/payment-hub-ci-gate/ci-preflight.json"
)
PUBLISH_CI_GATE_RUN = '''set -euo pipefail
python3 -B scripts/check-main-ci.py --source-sha "$SOURCE_SHA" --summary "$RUNNER_TEMP/payment-hub-ci-gate/ci-preflight.json"'''
CHECK_FORBIDDEN_TOKENS = (
    "pull_request_target",
    "workflow_run",
    "workflow_dispatch",
    "workflow_call",
    "secrets.",
    "GITHUB_TOKEN",
    "github.token",
    "packages: write",
    "id-token: write",
    "contents: write",
    "docker build",
    "docker push",
    "docker login",
    "ghcr.io",
    "set -x",
    "printenv",
    "tee ",
)
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
        "persist-credentials",
        "merge-multiple",
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


def _parse_workflow_dispatch(
    lines: list[tuple[int, str, int]], index: int
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines) and lines[index][0] > 2:
        indent, content, number = lines[index]
        if indent != 4:
            raise ContractError("workflow-syntax")
        key, value = _split_kv(content, number)
        if key != "inputs" or value is not None or "inputs" in result:
            raise ContractError("workflow-syntax")
        index += 1
        inputs: dict[str, Any] = {}
        while index < len(lines) and lines[index][0] > 4:
            input_indent, input_content, input_number = lines[index]
            if input_indent != 6:
                raise ContractError("workflow-syntax")
            name, input_value = _split_kv(input_content, input_number)
            if input_value is not None or name in inputs:
                raise ContractError("workflow-syntax")
            index += 1
            body: dict[str, Any] = {}
            while index < len(lines) and lines[index][0] > 6:
                body_indent, body_content, body_number = lines[index]
                if body_indent != 8:
                    raise ContractError("workflow-syntax")
                body_key, body_value = _split_kv(body_content, body_number)
                if body_key in body or body_value is None:
                    raise ContractError("workflow-syntax")
                if body_key == "required":
                    body[body_key] = _parse_bool(body_value)
                elif body_key == "default":
                    if body_value in ("true", "false"):
                        body[body_key] = _parse_bool(body_value)
                    else:
                        body[body_key] = _unquote(body_value)
                elif body_key in ("description", "type"):
                    body[body_key] = _unquote(body_value)
                else:
                    raise ContractError("workflow-syntax")
                index += 1
            inputs[name] = body
        result["inputs"] = inputs
    return result, index


def _parse_on(
    lines: list[tuple[int, str, int]], index: int
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines) and lines[index][0] > 0:
        indent, content, number = lines[index]
        if indent != 2:
            raise ContractError("workflow-syntax")
        event, value = _split_kv(content, number)
        if event == "workflow_dispatch":
            if value is not None or event in result:
                raise ContractError("workflow-syntax")
            body, index = _parse_workflow_dispatch(lines, index + 1)
            result[event] = body
            continue
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
        elif key in ("shell", "if", "uses", "continue-on-error", "id"):
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
        elif key in ("env", "permissions", "outputs"):
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
        if upload.get("with", {}).get("retention-days") != 30:
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


def validate_check_workflow(
    workflow: dict[str, Any], text: str, repo: Path = REPO_ROOT
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []

    def add(kind: str, package: str | None = None) -> None:
        issues.append(ContractIssue(kind, package))

    if set(workflow) - {"name", "on", "concurrency", "permissions", "env", "jobs"}:
        add("check-workflow-top-level")
    if workflow.get("name") != CHECK_WORKFLOW_NAME:
        add("check-workflow-name")
    if workflow.get("on") != {
        "push": {"branches": ["main"]},
        "pull_request": {"branches": ["main"]},
    }:
        add("check-workflow-triggers")
    if workflow.get("permissions") != {"contents": "read"}:
        add("check-workflow-permissions")
    if workflow.get("concurrency") != {
        "group": CONCURRENCY_GROUP,
        "cancel-in-progress": False,
    }:
        add("check-workflow-concurrency")
    if workflow.get("env") != CHECK_TOP_ENV:
        add("check-workflow-env")

    jobs = workflow.get("jobs", {})
    if tuple(jobs) != (CHECK_JOB_ID,):
        add("check-workflow-jobs")
    job = jobs.get(CHECK_JOB_ID)
    if not isinstance(job, dict):
        add("check-workflow-jobs", CHECK_JOB_ID)
        for token in CHECK_FORBIDDEN_TOKENS:
            if token in text:
                add("check-forbidden-token")
        return issues

    if job.get("name") != CHECK_JOB_NAME:
        add("check-job-name")
    if job.get("runs-on") != "ubuntu-24.04":
        add("check-job-runs-on")
    if job.get("timeout-minutes") != CHECK_TIMEOUT_MINUTES:
        add("check-job-timeout")
    if "permissions" in job:
        add("check-job-permissions")
    if "needs" in job:
        add("check-job-needs")
    if "if" in job:
        add("check-job-condition")
    if "env" in job:
        add("check-job-env")

    steps = job.get("steps", [])
    expected_steps = (
        "Isolate image acceptance Go config and caches",
        "Check out repository",
        "Set up Go 1.27.4",
        DOCKER_SETUP_STEP,
        DOCKER_VERIFY_STEP,
        "Build and accept paired images",
        CHECK_HANDOFF_STEP,
        "Upload safe image acceptance summaries",
    )
    names = [step.get("name") for step in steps]
    if tuple(names) != expected_steps:
        add("check-workflow-steps")
    for step in steps:
        if "continue-on-error" in step:
            add("check-continue-on-error")

    uses = [step.get("uses") for step in steps if "uses" in step]
    if set(uses) != CHECK_EXPECTED_ACTION_PINS:
        add("check-action-pins")
    for pin, count in CHECK_EXPECTED_ACTION_COUNTS.items():
        if uses.count(pin) != count:
            add("check-action-pins")

    isolation = _find_step(job, "Isolate image acceptance Go config and caches")
    if isolation is None:
        add("check-isolation-missing")
    else:
        if isolation.get("shell") != "bash":
            add("check-isolation-shell")
        run = isolation.get("run", "")
        for token in (
            "set -euo pipefail",
            "command -v go",
            "go telemetry off",
            "GITHUB_ENV",
            "payment-hub-images",
            "XDG_CONFIG_HOME",
            "GOPATH",
            "GOMODCACHE",
            "GOCACHE",
            "TMPDIR",
        ):
            if token not in run:
                add("check-isolation-command")
        for token in ("set -x", "printenv", "tee ", "env |"):
            if token in run:
                add("check-isolation-unsafe")

    checkout = _find_step(job, "Check out repository")
    if checkout is None:
        add("check-checkout-missing")
    else:
        if checkout.get("uses") != CHECKOUT:
            add("check-checkout-action")
        checkout_with = checkout.get("with", {})
        if checkout_with.get("persist-credentials") is not False:
            add("check-checkout-credentials")
        if checkout_with.get("fetch-depth") != "1":
            add("check-checkout-depth")

    setup_go = _find_step(job, "Set up Go 1.27.4")
    if setup_go is None:
        add("check-setup-go-missing")
    else:
        if setup_go.get("uses") != SETUP_GO:
            add("check-setup-go-action")
        setup_with = setup_go.get("with", {})
        if setup_with.get("go-version") != "1.27.4":
            add("check-setup-go-version")
        if setup_with.get("check-latest") is not False:
            add("check-setup-go-latest")
        if setup_with.get("cache") is not False:
            add("check-setup-go-cache")

    _validate_docker_steps(
        CHECK_JOB_ID,
        job,
        add,
        context=CHECK_DOCKER_CONTEXT,
        prefix="check-docker",
    )

    build = _find_step(job, "Build and accept paired images")
    if build is None:
        add("check-build-missing")
    else:
        if build.get("shell") != "bash":
            add("check-build-shell")
        if build.get("env") != {"CGO_ENABLED": "1", "GOMAXPROCS": "2"}:
            add("check-build-env")
        run = build.get("run", "")
        for token in (
            "set -euo pipefail",
            CHECK_BUILDER_SCRIPT,
            '--go "$(command -v go)"',
            '--cache-dir "$RUNNER_TEMP/payment-hub-images"',
            '--artifact-root "$RUNNER_TEMP/payment-hub-images/artifacts"',
            "--prepare-inputs",
        ):
            if token not in run:
                add("check-build-command")
        for token in (
            "tee ",
            "docker build",
            "docker push",
            "docker login",
            "secrets.",
        ):
            if token in run:
                add("check-build-unsafe")

    handoff = _find_step(job, CHECK_HANDOFF_STEP)
    if handoff is None:
        add("check-handoff-missing")
    else:
        if handoff.get("shell") != "bash":
            add("check-handoff-shell")
        if "if" in handoff or "continue-on-error" in handoff:
            add("check-handoff-skip")
        if "env" in handoff:
            add("check-handoff-env")
        run = handoff.get("run", "")
        if run.strip() != CHECK_HANDOFF_RUN.strip():
            add("check-handoff-run")
        for token in (
            "GITHUB_TOKEN",
            "github.token",
            "secrets.",
            "build-images.py",
            "pair.tar",
            "descriptor.json",
            "build-record.json",
            "accepted-images.json",
        ):
            if token in run:
                add("check-handoff-run")

    uploads = [step for step in steps if step.get("uses") == UPLOAD]
    if len(uploads) != 1:
        add("check-upload")
    else:
        upload = uploads[0]
        if upload.get("name") != "Upload safe image acceptance summaries":
            add("check-upload-step")
        if upload.get("if") != "always()":
            add("check-upload-condition")
        upload_with = upload.get("with", {})
        if upload_with.get("name") != CHECK_UPLOAD_NAME:
            add("check-upload-name")
        if upload_with.get("if-no-files-found") != "error":
            add("check-upload-missing")
        if upload_with.get("retention-days") != 30:
            add("check-upload-retention")
        if upload_with.get("include-hidden-files") is not False:
            add("check-upload-hidden")
        paths = _upload_paths(upload)
        if tuple(paths) != CHECK_UPLOAD_PATHS:
            add("check-upload-paths")
        allowed_basenames = {
            "build-summary.json",
            "images-summary.json",
            "handoff-preflight.safe.json",
        }
        for path in paths:
            if "*" in path or "?" in path or "[" in path:
                add("check-upload-wildcard")
            if not path.endswith(".json"):
                add("check-upload-paths")
            if path.rsplit("/", 1)[-1] not in allowed_basenames:
                add("check-upload-paths")
            if "/artifacts/" in path and "/upload/" not in path:
                add("check-upload-paths")
            if path.endswith((".tar", ".tar.gz")):
                add("check-upload-paths")
        if set(upload_with) != {
            "name",
            "path",
            "if-no-files-found",
            "retention-days",
            "include-hidden-files",
        }:
            add("check-upload-with")

    for token in CHECK_FORBIDDEN_TOKENS:
        if token in text:
            add("check-forbidden-token")
    if re.search(r"(?m)^\s*[A-Za-z0-9_-]+:\s*write\s*$", text):
        add("check-write-permissions")
    return issues


def _publish_inputs_ok(workflow: dict[str, Any]) -> bool:
    on_value = workflow.get("on")
    if not isinstance(on_value, dict) or set(on_value) != {"workflow_dispatch"}:
        return False
    dispatch = on_value["workflow_dispatch"]
    if not isinstance(dispatch, dict) or set(dispatch) != {"inputs"}:
        return False
    inputs = dispatch["inputs"]
    if not isinstance(inputs, dict) or set(inputs) != set(PUBLISH_INPUT_NAMES):
        return False
    for name in PUBLISH_INPUT_NAMES:
        body = inputs.get(name)
        if not isinstance(body, dict):
            return False
        if set(body) != {"description", "required", "default", "type"}:
            return False
        if body.get("required") is not True or body.get("default") is not False:
            return False
        if body.get("type") != "boolean":
            return False
        description = body.get("description")
        if not isinstance(description, str) or not description.strip():
            return False
    return True


def _validate_docker_steps(
    job_id: str,
    job: dict[str, Any],
    add: Any,
    *,
    context: str,
    prefix: str,
) -> None:
    setup = _find_step(job, DOCKER_SETUP_STEP)
    if setup is None:
        add(f"{prefix}-setup-missing", job_id)
    else:
        if setup.get("uses") != SETUP_DOCKER:
            add(f"{prefix}-action", job_id)
        if "if" in setup or "continue-on-error" in setup:
            add(f"{prefix}-setup-skip", job_id)
        setup_with = setup.get("with", {})
        if set(setup_with) != {
            "version",
            "context",
            "set-host",
            "runtime-basedir",
            "daemon-config",
            "github-token",
        }:
            add(f"{prefix}-setup-with", job_id)
        if setup_with.get("version") != DOCKER_VERSION:
            add(f"{prefix}-version", job_id)
        if setup_with.get("context") != context:
            add(f"{prefix}-context", job_id)
        if setup_with.get("set-host") not in (True, "true"):
            add(f"{prefix}-set-host", job_id)
        if setup_with.get("runtime-basedir") != DOCKER_RUNTIME_BASEDIR:
            add(f"{prefix}-runtime-basedir", job_id)
        if setup_with.get("daemon-config") != DOCKER_DAEMON_CONFIG:
            add(f"{prefix}-daemon-config", job_id)
        if setup_with.get("github-token") != "":
            add(f"{prefix}-token", job_id)
    verify = _find_step(job, DOCKER_VERIFY_STEP)
    if verify is None:
        add(f"{prefix}-verify-missing", job_id)
    else:
        if verify.get("shell") != "bash":
            add(f"{prefix}-verify-shell", job_id)
        if "if" in verify or "continue-on-error" in verify:
            add(f"{prefix}-verify-skip", job_id)
        if verify.get("run", "").strip() != DOCKER_VERIFY_RUN.strip():
            add(f"{prefix}-verify-run", job_id)


def _validate_publish_ci_gate(
    job_id: str, job: dict[str, Any], add: Any
) -> None:
    gate = _find_step(job, PUBLISH_CI_GATE_STEP)
    if gate is None:
        add("publish-ci-gate-missing", job_id)
    else:
        if gate.get("shell") != "bash":
            add("publish-ci-gate-shell", job_id)
        if "if" in gate or "continue-on-error" in gate:
            add("publish-ci-gate-skip", job_id)
        if gate.get("env") != {
            "GITHUB_TOKEN": "${{ github.token }}",
            "SOURCE_SHA": "${{ github.sha }}",
        }:
            add("publish-ci-gate-env", job_id)
        run = gate.get("run", "")
        if run.strip() != PUBLISH_CI_GATE_RUN.strip():
            add("publish-ci-gate-run", job_id)
        for token in ("secrets.", "PERSONAL_ACCESS_TOKEN", "GH_PAT"):
            if token in run:
                add("publish-ci-gate-unsafe", job_id)


def validate_publish_workflow(
    workflow: dict[str, Any], text: str, repo: Path = REPO_ROOT
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []

    def add(kind: str, package: str | None = None) -> None:
        issues.append(ContractIssue(kind, package))

    if set(workflow) - {"name", "on", "concurrency", "permissions", "env", "jobs"}:
        add("publish-workflow-top-level")
    if workflow.get("name") != PUBLISH_WORKFLOW_NAME:
        add("publish-workflow-name")
    if not _publish_inputs_ok(workflow):
        add("publish-workflow-trigger-inputs")
    if workflow.get("permissions") != {"contents": "read"}:
        add("publish-workflow-permissions")
    if workflow.get("concurrency") != {
        "group": "${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": False,
    }:
        add("publish-workflow-concurrency")
    if workflow.get("env") != PUBLISH_TOP_ENV:
        add("publish-workflow-env")

    jobs = workflow.get("jobs", {})
    if tuple(jobs) != PUBLISH_JOBS:
        add("publish-workflow-jobs")
    prepare = jobs.get("prepare")
    upload = jobs.get("upload")
    if not isinstance(prepare, dict):
        add("publish-workflow-jobs", "prepare")
    if not isinstance(upload, dict):
        add("publish-workflow-jobs", "upload")

    uses = [
        step.get("uses")
        for job in jobs.values()
        if isinstance(job, dict)
        for step in job.get("steps", [])
        if "uses" in step
    ]
    if set(uses) != PUBLISH_EXPECTED_ACTION_PINS:
        add("publish-action-pins")
    for pin, count in PUBLISH_ACTION_COUNTS.items():
        if uses.count(pin) != count:
            add("publish-action-pins")

    if isinstance(prepare, dict):
        if prepare.get("name") != "Prepare release handoff (linux/amd64)":
            add("publish-prepare-name")
        if prepare.get("runs-on") != "ubuntu-24.04":
            add("publish-prepare-runs-on")
        if prepare.get("timeout-minutes") != PUBLISH_TIMEOUT_MINUTES:
            add("publish-prepare-timeout")
        if prepare.get("permissions") != {
            "contents": "read",
            "actions": "read",
        }:
            add("publish-prepare-permissions")
        if "env" in prepare:
            add("publish-prepare-env")
        if "needs" in prepare:
            add("publish-prepare-needs")
        if "if" in prepare:
            add("publish-prepare-condition")
        if prepare.get("outputs") != {
            "artifact_id": "${{ steps.bundle.outputs.artifact-id }}",
            "artifact_digest": "${{ steps.bundle.outputs.artifact-digest }}",
            "source_sha": "${{ steps.handoff.outputs.source_sha }}",
            "descriptor_sha256": "${{ steps.handoff.outputs.descriptor_sha256 }}",
        }:
            add("publish-prepare-outputs")
        names = _step_names(prepare)
        if tuple(names) != (
            "Verify manual publish preconditions",
            "Isolate release Go config and caches",
            "Check out repository",
            PUBLISH_CI_GATE_STEP,
            "Set up Go 1.27.4",
            DOCKER_SETUP_STEP,
            DOCKER_VERIFY_STEP,
            "Prepare release handoff",
            "Read trusted prepare outputs",
            "Upload release bundle",
        ):
            add("publish-prepare-steps")
        gate = _find_step(prepare, "Verify manual publish preconditions")
        if gate is None:
            add("publish-prepare-gate-missing")
        else:
            if gate.get("shell") != "bash":
                add("publish-prepare-gate-shell")
            if "if" in gate or "continue-on-error" in gate:
                add("publish-prepare-gate-skip")
            if gate.get("env") != {
                "SOURCE_SHA": "${{ github.sha }}",
                "EVENT_NAME": "${{ github.event_name }}",
                "REF_NAME": "${{ github.ref }}",
                "REPOSITORY": "${{ github.repository }}",
                "PUBLISH_AUTHORIZED": "${{ inputs.publish_authorized }}",
                "PACKAGES_VERIFIED": "${{ inputs.packages_verified }}",
            }:
                add("publish-prepare-gate-env")
            run = gate.get("run", "")
            for token in (
                "set -euo pipefail",
                'if [[ "$EVENT_NAME" != "workflow_dispatch" ]]',
                'if [[ "$REF_NAME" != "refs/heads/main" ]]',
                'if [[ "$REPOSITORY" != "xtianxx/payment-hub" ]]',
                'if [[ ! "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]',
                'if [[ "$PUBLISH_AUTHORIZED" != "true" || "$PACKAGES_VERIFIED" != "true" ]]',
                "exit 1",
            ):
                if token not in run:
                    add("publish-prepare-gate-run")
        isolation = _find_step(prepare, "Isolate release Go config and caches")
        if isolation is None:
            add("publish-prepare-isolation-missing")
        else:
            if isolation.get("shell") != "bash":
                add("publish-prepare-isolation-shell")
            run = isolation.get("run", "")
            for token in (
                "set -euo pipefail",
                "command -v go",
                "go telemetry off",
                "GITHUB_ENV",
                "payment-hub-release-cache",
                "XDG_CONFIG_HOME",
                "GOPATH",
                "GOMODCACHE",
                "GOCACHE",
                "TMPDIR",
            ):
                if token not in run:
                    add("publish-prepare-isolation-command")
            for token in ("set -x", "printenv", "tee ", "env |"):
                if token in run:
                    add("publish-prepare-isolation-unsafe")
        checkout = _find_step(prepare, "Check out repository")
        if checkout is None:
            add("publish-prepare-checkout-missing")
        else:
            if checkout.get("uses") != CHECKOUT:
                add("publish-prepare-checkout-action")
            checkout_with = checkout.get("with", {})
            if checkout_with.get("ref") != "${{ github.sha }}":
                add("publish-prepare-checkout-ref")
            if checkout_with.get("persist-credentials") is not False:
                add("publish-prepare-checkout-credentials")
            if checkout_with.get("fetch-depth") != "1":
                add("publish-prepare-checkout-depth")
        _validate_publish_ci_gate("prepare", prepare, add)
        setup_go = _find_step(prepare, "Set up Go 1.27.4")
        if setup_go is None:
            add("publish-prepare-setup-go-missing")
        else:
            if setup_go.get("uses") != SETUP_GO:
                add("publish-prepare-setup-go-action")
            setup_with = setup_go.get("with", {})
            if setup_with.get("go-version") != "1.27.4":
                add("publish-prepare-setup-go-version")
            if setup_with.get("check-latest") is not False:
                add("publish-prepare-setup-go-latest")
            if setup_with.get("cache") is not False:
                add("publish-prepare-setup-go-cache")
        prepare_step = _find_step(prepare, "Prepare release handoff")
        if prepare_step is None:
            add("publish-prepare-command-missing")
        else:
            if prepare_step.get("shell") != "bash":
                add("publish-prepare-command-shell")
            if prepare_step.get("env") != {"SOURCE_SHA": "${{ github.sha }}"}:
                add("publish-prepare-command-env")
            run = prepare_step.get("run", "")
            for token in (
                "set -euo pipefail",
                PUBLISH_PREPARE_COMMAND,
                '--source-sha "$SOURCE_SHA"',
                '--go "$(command -v go)"',
                '--cache-dir "$RUNNER_TEMP/payment-hub-release-cache"',
                '--artifact-root "$RUNNER_TEMP/payment-hub-release-prepare"',
                "--prepare-inputs",
            ):
                if token not in run:
                    add("publish-prepare-command-run")
            for token in ("GITHUB_TOKEN", "secrets.", "docker build", "docker save"):
                if token in run:
                    add("publish-prepare-command-unsafe")
        handoff = _find_step(prepare, "Read trusted prepare outputs")
        if handoff is None:
            add("publish-prepare-handoff-missing")
        else:
            if handoff.get("id") != "handoff":
                add("publish-prepare-handoff-id")
            run = handoff.get("run", "")
            for token in (
                "set -euo pipefail",
                "handoff.json",
                "GITHUB_OUTPUT",
                "source_sha",
                "descriptor_sha256",
                "40",
                "64",
            ):
                if token not in run:
                    add("publish-prepare-handoff-run")
            if "GITHUB_TOKEN" in run or "secrets." in run:
                add("publish-prepare-handoff-unsafe")
        bundle = _find_step(prepare, "Upload release bundle")
        if bundle is None:
            add("publish-bundle-upload-missing")
        else:
            if bundle.get("id") != "bundle":
                add("publish-bundle-upload-id")
            if bundle.get("uses") != UPLOAD:
                add("publish-bundle-upload-action")
            if "if" in bundle:
                add("publish-bundle-upload-condition")
            bundle_with = bundle.get("with", {})
            if bundle_with.get("name") != (
                "release-bundle-${{ github.run_id }}-${{ github.run_attempt }}"
            ):
                add("publish-bundle-upload-name")
            if bundle_with.get("if-no-files-found") != "error":
                add("publish-bundle-upload-missing-policy")
            if bundle_with.get("retention-days") != 30:
                add("publish-bundle-upload-retention")
            if bundle_with.get("include-hidden-files") is not False:
                add("publish-bundle-upload-hidden")
            paths = _upload_paths(bundle)
            if tuple(paths) != PUBLISH_BUNDLE_PATHS:
                add("publish-bundle-upload-paths")
            for path in paths:
                if "*" in path or "?" in path or "[" in path:
                    add("publish-bundle-upload-wildcard")
                if not path.startswith(
                    "${{ runner.temp }}/payment-hub-release-prepare/bundle/"
                ):
                    add("publish-bundle-upload-paths")
                if "payment-hub-ci-gate" in path:
                    add("publish-bundle-upload-paths")
        _validate_docker_steps(
            "prepare",
            prepare,
            add,
            context=PUBLISH_DOCKER_CONTEXT,
            prefix="publish-docker",
        )

    if isinstance(upload, dict):
        if upload.get("name") != "Publish paired images (linux/amd64)":
            add("publish-upload-name")
        if upload.get("runs-on") != "ubuntu-24.04":
            add("publish-upload-runs-on")
        if upload.get("timeout-minutes") != PUBLISH_TIMEOUT_MINUTES:
            add("publish-upload-timeout")
        if upload.get("needs") != "prepare":
            add("publish-upload-needs")
        if upload.get("permissions") != {
            "contents": "read",
            "actions": "read",
            "packages": "write",
        }:
            add("publish-upload-permissions")
        if "env" in upload:
            add("publish-upload-env")
        if "if" in upload:
            add("publish-upload-condition")
        names = _step_names(upload)
        if tuple(names) != (
            "Verify release handoff identity",
            "Isolate release Go config and caches",
            "Check out repository",
            "Set up Go 1.27.4",
            DOCKER_SETUP_STEP,
            DOCKER_VERIFY_STEP,
            "Prepare handoff download directory",
            "Download release bundle",
            "Restrict handoff bundle permissions",
            PUBLISH_CI_GATE_STEP,
            "Publish paired images",
            "Upload release summary",
        ):
            add("publish-upload-steps")
        gate = _find_step(upload, "Verify release handoff identity")
        if gate is None:
            add("publish-upload-gate-missing")
        else:
            if gate.get("shell") != "bash":
                add("publish-upload-gate-shell")
            if "if" in gate or "continue-on-error" in gate:
                add("publish-upload-gate-skip")
            if gate.get("env") != {
                "SOURCE_SHA": "${{ github.sha }}",
                "PREPARED_SHA": "${{ needs.prepare.outputs.source_sha }}",
                "EXPECTED_DESCRIPTOR_SHA": (
                    "${{ needs.prepare.outputs.descriptor_sha256 }}"
                ),
            }:
                add("publish-upload-gate-env")
            run = gate.get("run", "")
            for token in (
                "set -euo pipefail",
                'if [[ "$PREPARED_SHA" != "$SOURCE_SHA" ]]',
                'if [[ ! "$EXPECTED_DESCRIPTOR_SHA" =~ ^[0-9a-f]{64}$ ]]',
                "exit 1",
            ):
                if token not in run:
                    add("publish-upload-gate-run")
        isolation = _find_step(upload, "Isolate release Go config and caches")
        if isolation is None:
            add("publish-upload-isolation-missing")
        else:
            if isolation.get("shell") != "bash":
                add("publish-upload-isolation-shell")
            run = isolation.get("run", "")
            for token in (
                "set -euo pipefail",
                "command -v go",
                "go telemetry off",
                "GITHUB_ENV",
                "payment-hub-release-cache",
                "XDG_CONFIG_HOME",
                "GOPATH",
                "GOMODCACHE",
                "GOCACHE",
                "TMPDIR",
            ):
                if token not in run:
                    add("publish-upload-isolation-command")
            for token in ("set -x", "printenv", "tee ", "env |"):
                if token in run:
                    add("publish-upload-isolation-unsafe")
        checkout = _find_step(upload, "Check out repository")
        if checkout is None:
            add("publish-upload-checkout-missing")
        else:
            if checkout.get("uses") != CHECKOUT:
                add("publish-upload-checkout-action")
            checkout_with = checkout.get("with", {})
            if checkout_with.get("ref") != "${{ github.sha }}":
                add("publish-upload-checkout-ref")
            if checkout_with.get("persist-credentials") is not False:
                add("publish-upload-checkout-credentials")
            if checkout_with.get("fetch-depth") != "1":
                add("publish-upload-checkout-depth")
        setup_go = _find_step(upload, "Set up Go 1.27.4")
        if setup_go is None:
            add("publish-upload-setup-go-missing")
        else:
            if setup_go.get("uses") != SETUP_GO:
                add("publish-upload-setup-go-action")
            setup_with = setup_go.get("with", {})
            if setup_with.get("go-version") != "1.27.4":
                add("publish-upload-setup-go-version")
            if setup_with.get("check-latest") is not False:
                add("publish-upload-setup-go-latest")
            if setup_with.get("cache") is not False:
                add("publish-upload-setup-go-cache")
        download_dir = _find_step(upload, "Prepare handoff download directory")
        if download_dir is None:
            add("publish-download-dir-missing")
        else:
            run = download_dir.get("run", "")
            for token in (
                "set -euo pipefail",
                "install -d -m 0700",
                "payment-hub-release-handoff",
            ):
                if token not in run:
                    add("publish-download-dir-run")
        download = _find_step(upload, "Download release bundle")
        if download is None:
            add("publish-download-missing")
        else:
            if download.get("uses") != DOWNLOAD:
                add("publish-download-action")
            download_with = download.get("with", {})
            if set(download_with) != {
                "artifact-ids",
                "path",
                "merge-multiple",
                "digest-mismatch",
            }:
                add("publish-download-with")
            if download_with.get("artifact-ids") != (
                "${{ needs.prepare.outputs.artifact_id }}"
            ):
                add("publish-download-artifact-id")
            if download_with.get("path") != PUBLISH_DOWNLOAD_PATH:
                add("publish-download-path")
            if download_with.get("merge-multiple") is not True:
                add("publish-download-merge")
            if download_with.get("digest-mismatch") != "error":
                add("publish-download-digest")
        restrict = _find_step(upload, "Restrict handoff bundle permissions")
        if restrict is None:
            add("publish-handoff-permissions-missing")
        else:
            run = restrict.get("run", "")
            if "chmod 0600" not in run:
                add("publish-handoff-permissions-run")
            for name in (
                "pair.tar",
                "descriptor.json",
                "build-record.json",
                "accepted-images.json",
            ):
                if name not in run:
                    add("publish-handoff-permissions-run")
        _validate_publish_ci_gate("upload", upload, add)
        publish_step = _find_step(upload, "Publish paired images")
        if publish_step is None:
            add("publish-command-missing")
        else:
            if publish_step.get("shell") != "bash":
                add("publish-command-shell")
            if publish_step.get("env") != {
                "SOURCE_SHA": "${{ github.sha }}",
                "EXPECTED_DESCRIPTOR_SHA": (
                    "${{ needs.prepare.outputs.descriptor_sha256 }}"
                ),
                "GITHUB_TOKEN": "${{ github.token }}",
            }:
                add("publish-command-env")
            run = publish_step.get("run", "")
            for token in (
                "set -euo pipefail",
                PUBLISH_PUBLISH_COMMAND,
                '--source-sha "$SOURCE_SHA"',
                '--go "$(command -v go)"',
                '--cache-dir "$RUNNER_TEMP/payment-hub-release-cache"',
                '--artifact-root "$RUNNER_TEMP/payment-hub-release-publish"',
                '--bundle-dir "$RUNNER_TEMP/payment-hub-release-handoff"',
                '--expected-descriptor-sha256 "$EXPECTED_DESCRIPTOR_SHA"',
                "--approve-external",
                "--package-preconditions-verified",
                "--prepare-inputs",
            ):
                if token not in run:
                    add("publish-command-run")
            for token in (
                "secrets.",
                "docker push",
                "docker login",
                "docker build",
                "ghcr.io",
                "cosign",
                "attest",
                "deploy",
            ):
                if token in run:
                    add("publish-command-unsafe")
        summary = _find_step(upload, "Upload release summary")
        if summary is None:
            add("publish-summary-upload-missing")
        else:
            if summary.get("if") != "always()":
                add("publish-summary-upload-condition")
            if summary.get("uses") != UPLOAD:
                add("publish-summary-upload-action")
            summary_with = summary.get("with", {})
            if summary_with.get("name") != (
                "release-summary-${{ github.run_id }}-${{ github.run_attempt }}"
            ):
                add("publish-summary-upload-name")
            if summary_with.get("if-no-files-found") != "error":
                add("publish-summary-upload-missing-policy")
            if summary_with.get("retention-days") != 30:
                add("publish-summary-upload-retention")
            if summary_with.get("include-hidden-files") is not False:
                add("publish-summary-upload-hidden")
            paths = _upload_paths(summary)
            if tuple(paths) != (PUBLISH_SUMMARY_PATH,):
                add("publish-summary-upload-paths")
            for path in paths:
                if "*" in path or "?" in path or "[" in path:
                    add("publish-summary-upload-wildcard")
        _validate_docker_steps(
            "upload",
            upload,
            add,
            context=PUBLISH_DOCKER_CONTEXT,
            prefix="publish-docker",
        )
    allowed_token_steps = {
        ("prepare", PUBLISH_CI_GATE_STEP),
        ("upload", PUBLISH_CI_GATE_STEP),
        ("upload", "Publish paired images"),
    }
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if "env" in job:
            add("publish-job-env", job_id)
        if "GITHUB_TOKEN" in job.get("env", {}):
            add("publish-token-location")
        for step in job.get("steps", []):
            if "continue-on-error" in step:
                add("publish-continue-on-error")
            if "GITHUB_TOKEN" in step.get("env", {}):
                if (job_id, step.get("name")) not in allowed_token_steps:
                    add("publish-token-location")
            if step.get("uses") == DOWNLOAD and job_id != "upload":
                add("publish-download-location")
    if "GITHUB_TOKEN" in workflow.get("env", {}):
        add("publish-token-location")

    for token in (
        "pull_request_target",
        "workflow_run",
        "workflow_call",
        "secrets.",
        "id-token",
        "attestations",
        "contents: write",
        "actions: write",
        "ghcr.io",
        "docker push",
        "docker login",
        "docker build",
        "cosign",
        "environment:",
    ):
        if token in text:
            add("publish-forbidden-token")
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
    for relative, validator in (
        (WORKFLOW_RELATIVE, validate_workflow),
        (CHECK_RELATIVE, validate_check_workflow),
    ):
        try:
            workflow, text = load_workflow(repo / relative)
            issues.extend(validator(workflow, text, repo))
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
