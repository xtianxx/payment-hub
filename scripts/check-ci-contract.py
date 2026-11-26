#!/usr/bin/env python3
"""Safe CI contract preflight for the payment-hub workflow.

The preflight is deliberately project-specific and standard-library only.  It
validates the three approved workflow shapes (ci, nightly, release), the
source-manifest coverage, the launcher/default inventory cross-links, and two
read-only ``go list`` metadata passes.  It never runs tests, starts PostgreSQL/Docker, downloads modules, or
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
CI_RELATIVE = Path(".github/workflows/ci.yml")
NIGHTLY_RELATIVE = Path(".github/workflows/nightly.yml")
RELEASE_RELATIVE = Path(".github/workflows/release.yml")
APPROVED_WORKFLOW_FILES = ("ci.yml", "nightly.yml", "release.yml")
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

EXPECTED_TOP_ENV = {
    "GOTOOLCHAIN": "local",
    "GOENV": "off",
    "GOWORK": "off",
    "GOFLAGS": "",
    "PYTHONDONTWRITEBYTECODE": "1",
}
CI_TOP_ENV = {**EXPECTED_TOP_ENV, "GOTELEMETRY": "off"}
NIGHTLY_TOP_ENV = CI_TOP_ENV
RELEASE_TOP_ENV = CI_TOP_ENV

CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_GO = "actions/setup-go@b7ad1dad31e06c5925ef5d2fc7ad053ef454303e"
UPLOAD = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
DOWNLOAD = "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333"
SETUP_DOCKER = (
    "docker/setup-docker-action@3f1ab1ab0e29231562e4db4ca600c1a49653bac6"
)

CI_WORKFLOW_NAME = "CI / 持续集成"
CI_TRIGGERS = {
    "push": {"branches": ["main"]},
    "pull_request": {"branches": ["main"]},
}
CI_CONCURRENCY_GROUP = (
    "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}"
)
CI_CONCURRENCY = {
    "group": CI_CONCURRENCY_GROUP,
    "cancel-in-progress": False,
}
CI_JOBS = (
    "format",
    "vet",
    "build",
    "unit-tests",
    "integration-tests",
    "image-acceptance",
    "vulnerability-check",
    "ci-gate",
)
CI_JOB_NAMES = {
    "format": "Format",
    "vet": "Vet",
    "build": "Build",
    "unit-tests": "Unit tests",
    "integration-tests": "Integration tests",
    "image-acceptance": "Paired image acceptance (linux/amd64)",
    "vulnerability-check": "Vulnerability scan (linux/amd64, read-only)",
    "ci-gate": "CI gate",
}
CI_TIMEOUTS = {
    "format": 10,
    "vet": 10,
    "build": 10,
    "unit-tests": 20,
    "integration-tests": 20,
    "image-acceptance": 30,
    "vulnerability-check": 10,
    "ci-gate": 1,
}
CI_ACTION_PINS = {CHECKOUT, SETUP_GO, UPLOAD, SETUP_DOCKER}
CI_ACTION_COUNTS = {
    CHECKOUT: 7,
    SETUP_GO: 7,
    UPLOAD: 6,
    SETUP_DOCKER: 1,
}
CI_GATE_NEEDS = (
    "format",
    "vet",
    "build",
    "unit-tests",
    "integration-tests",
    "image-acceptance",
    "vulnerability-check",
)
CI_GATE_IF = "${{ always() }}"
CI_GATE_ENV = {
    "FORMAT_RESULT": "${{ needs.format.result }}",
    "VET_RESULT": "${{ needs.vet.result }}",
    "BUILD_RESULT": "${{ needs.build.result }}",
    "UNIT_RESULT": "${{ needs.unit-tests.result }}",
    "INTEGRATION_RESULT": "${{ needs.integration-tests.result }}",
    "IMAGE_RESULT": "${{ needs.image-acceptance.result }}",
    "VULNERABILITY_RESULT": "${{ needs.vulnerability-check.result }}",
}
CI_GATE_RUN = """set -euo pipefail
if [[ "${FORMAT_RESULT:-}" != "success" || "${VET_RESULT:-}" != "success" || "${BUILD_RESULT:-}" != "success" || "${UNIT_RESULT:-}" != "success" || "${INTEGRATION_RESULT:-}" != "success" || "${IMAGE_RESULT:-}" != "success" || "${VULNERABILITY_RESULT:-}" != "success" ]]; then
  echo "ci-gate: one or more required jobs did not succeed"
  exit 1
fi
echo "ci-gate: all required jobs succeeded"
exit 0"""

CI_FORMAT_RUN = r'''set -euo pipefail
mapfile -d '' -t go_files < <(git ls-files -z -- '*.go')
if [[ "${#go_files[@]}" -eq 0 ]]; then
  echo "::error::no tracked Go files found; refusing to report success"
  exit 1
fi
unformatted="$(gofmt -l -- "${go_files[@]}")"
if [[ -n "$unformatted" ]]; then
  echo "::error::tracked Go files need gofmt"
  printf '%s\n' "$unformatted"
  exit 1
fi'''
CI_VET_RUN = """set -euo pipefail
go vet -mod=readonly -p=1 ./...
go vet -mod=readonly -p=1 -tags=integration ./..."""
CI_BUILD_RUN = """set -euo pipefail
go build -mod=readonly -p=1 ./..."""
CI_METADATA_RUN = r'''set -euo pipefail
python3 -B scripts/check-ci-contract.py --go "$(command -v go)"'''
CI_IMAGE_BUILD_RUN = r'''set -euo pipefail
python3 -B scripts/build-images.py \
  --go "$(command -v go)" \
  --cache-dir "$RUNNER_TEMP/payment-hub-images" \
  --artifact-root "$RUNNER_TEMP/payment-hub-images/artifacts" \
  --prepare-inputs'''
CI_IMAGE_HANDOFF_RUN = """set -euo pipefail
python3 -B scripts/check-image-handoff.py --build-root "$RUNNER_TEMP/payment-hub-images/artifacts" --artifact-root "$RUNNER_TEMP/payment-hub-handoff-preflight" --isolated-daemon"""
CI_CANCELLATION_RUN = r'''set -euo pipefail
echo "cancellation-check: one SIGTERM to this run's healthy launcher; not a runner cancellation or hard-kill test"
python3 -B scripts/check-ci-cancellation.py \
  --go "$(command -v go)" \
  --cache-dir "$RUNNER_TEMP/payment-hub-db" \
  --artifact-root "$RUNNER_TEMP/payment-hub-db/cancellation-artifacts" \
  --report-dir "$RUNNER_TEMP/payment-hub-db/reports" \
  --prepare-images'''
CI_VULNERABILITY_SCAN_RUN = r'''set -euo pipefail
echo "vulnerability-scan: source/symbol/integration/test; test participation does not equal production exposure"
python3 -B scripts/check-vulnerabilities.py \
  --go "$(command -v go)" \
  --cache-dir "$RUNNER_TEMP/payment-hub-vulnerability" \
  --tool-dir "$RUNNER_TEMP/payment-hub-vulnerability/tools" \
  --artifact-root "$RUNNER_TEMP/payment-hub-vulnerability/reports" \
  --prepare-tool'''

CI_STEP_NAMES = {
    "format": (
        "Isolate format Go config and caches",
        "Check out repository",
        "Set up Go 1.27.4",
        "Check tracked Go formatting",
    ),
    "vet": (
        "Isolate vet Go config and caches",
        "Check out repository",
        "Set up Go 1.27.4",
        "Run go vet",
    ),
    "build": (
        "Isolate build Go config and caches",
        "Check out repository",
        "Set up Go 1.27.4",
        "Run explicit Go build",
    ),
    "unit-tests": (
        "Isolate Go config and caches",
        "Check out repository",
        "Set up Go 1.27.4",
        "Run setup checks",
        "Check CI contract metadata",
        "Run foundation unit checks",
        "Check launcher syntax and safety regressions",
        "Install benchmark test dependency",
        "Run benchmark script tests",
        "Export safe setup reports",
        "Upload safe setup reports",
    ),
    "integration-tests": (
        "Isolate database foundation Go config and caches",
        "Check out repository",
        "Set up Go 1.27.4",
        "Run database foundation checks",
        "Verify cancellation cleanup with real PostgreSQL",
        "Summarize database foundation results",
        "Export safe database reports",
        "Upload safe database reports",
    ),
    "image-acceptance": (
        "Isolate image acceptance Go config and caches",
        "Check out repository",
        "Set up Go 1.27.4",
        "Set up Docker 29.9.3 with containerd image store",
        "Verify Docker image store",
        "Build and accept paired images",
        "Export release candidate",
        "Verify image handoff without publishing",
        "Upload release candidate bundle",
        "Write release candidate pointer",
        "Upload release candidate pointer",
        "Upload safe image acceptance summaries",
    ),
    "vulnerability-check": (
        "Isolate vulnerability Go config and caches",
        "Check out repository",
        "Set up Go 1.27.4",
        "Run pinned vulnerability scan",
        "Summarize vulnerability scan results",
        "Export safe vulnerability report",
        "Upload safe vulnerability reports",
    ),
    "ci-gate": ("Require all CI jobs to succeed",),
}

CI_UNIT_UPLOAD_NAME = (
    "setup-check-safe-reports-${{ github.run_id }}-${{ github.run_attempt }}"
)
CI_UNIT_UPLOAD_PATHS = (
    "${{ runner.temp }}/payment-hub-setup/reports/upload/go-default.safe.json",
    "${{ runner.temp }}/payment-hub-setup/reports/upload/provenance.json",
)
CI_INTEGRATION_UPLOAD_NAME = (
    "database-foundation-safe-reports-${{ github.run_id }}-${{ github.run_attempt }}"
)
CI_INTEGRATION_UPLOAD_PATHS = (
    "${{ runner.temp }}/payment-hub-db/reports/upload/db-foundation.safe.json",
    "${{ runner.temp }}/payment-hub-db/reports/upload/cancellation.safe.json",
    "${{ runner.temp }}/payment-hub-db/reports/upload/us1.safe.json",
    "${{ runner.temp }}/payment-hub-db/reports/upload/us2.safe.json",
    "${{ runner.temp }}/payment-hub-db/reports/upload/us3.safe.json",
    "${{ runner.temp }}/payment-hub-db/reports/upload/provenance.json",
)
CI_IMAGE_UPLOAD_NAME = (
    "image-acceptance-safe-reports-${{ github.run_id }}-${{ github.run_attempt }}"
)
CI_IMAGE_UPLOAD_PATHS = (
    "${{ runner.temp }}/payment-hub-images/artifacts/upload/build-summary.json",
    "${{ runner.temp }}/payment-hub-images/artifacts/upload/images-summary.json",
    "${{ runner.temp }}/payment-hub-images/artifacts/upload/handoff-preflight.safe.json",
)
CI_VULNERABILITY_UPLOAD_NAME = (
    "vulnerability-check-safe-reports-${{ github.run_id }}-${{ github.run_attempt }}"
)
CI_VULNERABILITY_UPLOAD_PATHS = (
    "${{ runner.temp }}/payment-hub-vulnerability/reports/upload/vulnerability.safe.json",
    "${{ runner.temp }}/payment-hub-vulnerability/reports/upload/provenance.json",
)

CI_CANDIDATE_GUARD = (
    "${{ github.event_name == 'push' && github.ref == 'refs/heads/main' "
    "&& github.repository == 'xtianxx/payment-hub' }}"
)
CI_CANDIDATE_BUNDLE_NAME = (
    "ci-release-bundle-${{ github.run_id }}-${{ github.run_attempt }}"
)
CI_CANDIDATE_POINTER_NAME = (
    "ci-release-pointer-${{ github.run_id }}-${{ github.run_attempt }}"
)
CI_CANDIDATE_BUNDLE_PATH = "${{ steps.export-candidate.outputs.bundle_dir }}"
CI_CANDIDATE_POINTER_PATH = (
    "${{ runner.temp }}/payment-hub-ci-candidate/pointer/pointer.json"
)
CI_CANDIDATE_EXPORT_RUN = (
    'set -euo pipefail\npython3 -B scripts/ci-candidate.py export --source-sha "$GITHUB_SHA" '
    '--build-root "$RUNNER_TEMP/payment-hub-images/artifacts" '
    '--artifact-root "$RUNNER_TEMP/payment-hub-ci-candidate" '
    '--github-output "$GITHUB_OUTPUT"'
)
CI_CANDIDATE_POINTER_ENV = {
    "DESCRIPTOR_SHA": "${{ steps.export-candidate.outputs.descriptor_sha256 }}",
    "BUNDLE_ARTIFACT_ID": "${{ steps.upload-candidate.outputs.artifact-id }}",
    "BUNDLE_ARTIFACT_DIGEST": "${{ steps.upload-candidate.outputs.artifact-digest }}",
}
CI_CANDIDATE_POINTER_RUN = (
    'set -euo pipefail\npython3 -B scripts/ci-candidate.py pointer --source-sha "$GITHUB_SHA" '
    '--descriptor-sha256 "$DESCRIPTOR_SHA" '
    '--bundle-artifact-id "$BUNDLE_ARTIFACT_ID" '
    '--bundle-artifact-digest "$BUNDLE_ARTIFACT_DIGEST" '
    '--output "$RUNNER_TEMP/payment-hub-ci-candidate/pointer/pointer.json"'
)

CI_ALWAYS_IF_STEPS = frozenset({
    "Export safe setup reports",
    "Upload safe setup reports",
    "Export safe database reports",
    "Upload safe database reports",
    "Summarize database foundation results",
    "Upload safe image acceptance summaries",
    "Summarize vulnerability scan results",
    "Export safe vulnerability report",
    "Upload safe vulnerability reports",
})
CI_IMAGE_CANDIDATE_ORDER = (
    "Build and accept paired images",
    "Export release candidate",
    "Verify image handoff without publishing",
    "Upload release candidate bundle",
    "Write release candidate pointer",
    "Upload release candidate pointer",
)
CI_CANDIDATE_GUARD_STEPS = {
    "Export release candidate": CI_CANDIDATE_GUARD,
    "Upload release candidate bundle": CI_CANDIDATE_GUARD,
    "Write release candidate pointer": CI_CANDIDATE_GUARD,
    "Upload release candidate pointer": CI_CANDIDATE_GUARD,
}
NIGHTLY_ALWAYS_IF_STEPS = frozenset({
    "Summarize vulnerability scan results",
    "Export safe vulnerability report",
    "Upload safe vulnerability reports",
})
RELEASE_ALWAYS_IF_STEPS = frozenset({
    "Upload release summary",
    "Upload migration initialization summary",
})

NIGHTLY_WORKFLOW_NAME = "Nightly vulnerability scan / 每日漏洞扫描"
NIGHTLY_TRIGGERS = {"schedule": [{"cron": "17 2 * * *"}]}
NIGHTLY_CONCURRENCY_GROUP = "${{ github.workflow }}-nightly"
NIGHTLY_CONCURRENCY = {
    "group": NIGHTLY_CONCURRENCY_GROUP,
    "cancel-in-progress": False,
}
NIGHTLY_JOBS = ("vulnerability-check",)
NIGHTLY_JOB_NAME = "Vulnerability scan (linux/amd64, read-only)"
NIGHTLY_TIMEOUT = 10
NIGHTLY_ACTION_PINS = {CHECKOUT, SETUP_GO, UPLOAD}
NIGHTLY_ACTION_COUNTS = {CHECKOUT: 1, SETUP_GO: 1, UPLOAD: 1}
NIGHTLY_STEP_NAMES = (
    "Isolate vulnerability Go config and caches",
    "Check out repository",
    "Set up Go 1.27.4",
    "Run pinned vulnerability scan",
    "Summarize vulnerability scan results",
    "Export safe vulnerability report",
    "Upload safe vulnerability reports",
)

RELEASE_WORKFLOW_NAME = "Manual paired image release / 人工配对镜像发布"
RELEASE_INPUT_NAMES = ("operation", "packages_verified", "confirmation")
RELEASE_OPERATION_OPTIONS = ("publish", "initialize-migrate")
RELEASE_CONFIRMATION_WORDS = {
    "publish": "PUBLISH",
    "initialize-migrate": "INITIALIZE-MIGRATE-MAY-BECOME-PUBLIC",
}
RELEASE_LEGACY_INPUTS = (
    "bootstrap_packages",
    "publish_authorized",
    "initialize_authorized",
    "initialization_preconditions_verified",
    "public_creation_acknowledged",
)
RELEASE_INPUT_DESCRIPTIONS = {
    "operation": (
        "Operation: normal paired publish, or migration package initialization only"
    ),
    "packages_verified": (
        "Package preconditions verified: publish requires both packages existing, "
        "public and correctly linked; initialize-migrate requires the app already "
        "public and linked, migrate absent, and no other writer active"
    ),
    "confirmation": (
        "Confirmation word: PUBLISH for normal publish, or "
        "INITIALIZE-MIGRATE-MAY-BECOME-PUBLIC for initialization; first package "
        "creation may become public immediately and public content is not "
        "guaranteed retractable"
    ),
}
RELEASE_JOBS = ("resolve-candidate", "upload", "initialize-migrate")
RELEASE_JOB_NAMES = {
    "resolve-candidate": "Resolve release candidate (linux/amd64)",
    "upload": "Publish paired images (linux/amd64)",
    "initialize-migrate": "Initialize migration package (linux/amd64)",
}
RELEASE_TIMEOUT = 30
RELEASE_CONCURRENCY = {
    "group": "${{ github.workflow }}-${{ github.ref }}",
    "cancel-in-progress": False,
}
RELEASE_ACTION_PINS = {
    CHECKOUT,
    SETUP_GO,
    UPLOAD,
    DOWNLOAD,
    SETUP_DOCKER,
}
RELEASE_ACTION_COUNTS = {
    CHECKOUT: 3,
    SETUP_GO: 2,
    UPLOAD: 2,
    DOWNLOAD: 3,
    SETUP_DOCKER: 2,
}
RELEASE_OWNER_GUARD = (
    "${{ github.actor == 'xtianxx' && "
    "github.triggering_actor == 'xtianxx' }}"
)
RELEASE_PUBLISH_GUARD = (
    "${{ github.actor == 'xtianxx' && "
    "github.triggering_actor == 'xtianxx' && "
    "inputs.operation == 'publish' }}"
)
RELEASE_INITIALIZE_GUARD = (
    "${{ github.actor == 'xtianxx' && "
    "github.triggering_actor == 'xtianxx' && "
    "inputs.operation == 'initialize-migrate' }}"
)
RELEASE_GATE_ENV = {
    "SOURCE_SHA": "${{ github.sha }}",
    "EVENT_NAME": "${{ github.event_name }}",
    "REF_NAME": "${{ github.ref }}",
    "REPOSITORY": "${{ github.repository }}",
    "RELEASE_ACTOR": "${{ github.actor }}",
    "RELEASE_TRIGGERING_ACTOR": "${{ github.triggering_actor }}",
    "OPERATION": "${{ inputs.operation }}",
    "PACKAGES_VERIFIED": "${{ inputs.packages_verified }}",
}
RELEASE_CONFIRMATION_ERROR = "confirmation input is missing or invalid"
RELEASE_GATE_RUN = """set -euo pipefail
if [[ "$RELEASE_ACTOR" != "xtianxx" || "$RELEASE_TRIGGERING_ACTOR" != "xtianxx" ]]; then
  echo "::error::owner-only publish requires actor and triggering actor xtianxx"
  exit 1
fi
if [[ "$EVENT_NAME" != "workflow_dispatch" ]]; then
  echo "::error::publish requires workflow_dispatch"
  exit 1
fi
if [[ "$REF_NAME" != "refs/heads/main" ]]; then
  echo "::error::publish requires refs/heads/main"
  exit 1
fi
if [[ "$REPOSITORY" != "xtianxx/payment-hub" ]]; then
  echo "::error::publish requires the trusted repository"
  exit 1
fi
if [[ ! "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::publish requires a 40-hex source SHA"
  exit 1
fi
case "$OPERATION" in
  publish)
    if [[ "$PACKAGES_VERIFIED" != "true" ]]; then
      echo "::error::publish requires packages_verified=true"
      exit 1
    fi
    expected="PUBLISH"
    ;;
  initialize-migrate)
    if [[ "$PACKAGES_VERIFIED" != "true" ]]; then
      echo "::error::initialize-migrate requires packages_verified=true"
      exit 1
    fi
    expected="INITIALIZE-MIGRATE-MAY-BECOME-PUBLIC"
    ;;
  *)
    echo "::error::OPERATION must be publish or initialize-migrate"
    exit 1
    ;;
esac
python3 -I - "$expected" <<'PY'
import json
import os
import sys
def fail():
    print("::error::confirmation input is missing or invalid")
    raise SystemExit(1)
try:
    expected = sys.argv[1]
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
        event = json.load(handle)
    if not isinstance(event, dict):
        fail()
    inputs = event.get("inputs")
    if not isinstance(inputs, dict):
        fail()
    confirmation = inputs.get("confirmation")
    if not isinstance(confirmation, str) or confirmation != expected:
        fail()
except SystemExit:
    raise
except Exception:
    fail()
PY
"""
RELEASE_BUNDLE_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
RELEASE_BUNDLE_DIGEST_ERROR = (
    "resolved candidate bundle artifact digest is not canonical sha256:64-hex"
)
RELEASE_DESCRIPTOR_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
RELEASE_RESOLVE_OUTPUTS = {
    "ci_run_id": "${{ steps.select-candidate.outputs.ci_run_id }}",
    "ci_run_attempt": "${{ steps.select-candidate.outputs.ci_run_attempt }}",
    "bundle_artifact_id": "${{ steps.verify-candidate.outputs.bundle_artifact_id }}",
    "descriptor_sha256": "${{ steps.verify-candidate.outputs.descriptor_sha256 }}",
    "bundle_artifact_digest": (
        "${{ steps.verify-candidate.outputs.bundle_artifact_digest }}"
    ),
}
RELEASE_RESOLVE_STEP_NAMES = (
    "Verify manual operation preconditions",
    "Check out repository",
    "Select release candidate",
    "Download release candidate pointer",
    "Verify release candidate",
)
RELEASE_UPLOAD_STEP_NAMES = (
    "Verify resolved candidate identity",
    "Isolate release Go config and caches",
    "Check out repository",
    "Set up Go 1.27.4",
    "Set up Docker 29.9.3 with containerd image store",
    "Verify Docker image store",
    "Prepare handoff download directory",
    "Download release bundle",
    "Restrict handoff bundle permissions",
    "Verify release handoff",
    "Require successful main CI",
    "Publish paired images",
    "Write release evidence",
    "Upload release summary",
)
RELEASE_AUDIT_SECRET_NAME = "GHCR_PACKAGE_AUDIT_TOKEN"
RELEASE_AUDIT_SECRET_EXPRESSION = "${{ secrets.GHCR_PACKAGE_AUDIT_TOKEN }}"
RELEASE_INIT_STEP_NAMES = (
    "Verify resolved candidate identity",
    "Isolate initialization Go config and caches",
    "Check out repository",
    "Set up Go 1.27.4",
    "Set up Docker 29.9.3 with containerd image store",
    "Verify Docker image store",
    "Prepare initialization download directory",
    "Download initialization bundle",
    "Restrict initialization bundle permissions",
    "Verify initialization handoff",
    "Require successful main CI",
    "Initialize migration package",
    "Upload migration initialization summary",
)
RELEASE_INIT_DOWNLOAD_PATH = (
    "${{ runner.temp }}/payment-hub-initialization-bundle"
)
RELEASE_INIT_SUMMARY_NAME = (
    "migrate-initialization-summary-${{ github.run_id }}-${{ github.run_attempt }}"
)
RELEASE_INIT_SUMMARY_PATH = (
    "${{ runner.temp }}/payment-hub-migrate-initialization/"
    "initialization-summary.json"
)
RELEASE_INIT_ISOLATION_BASE = "payment-hub-initialization"
RELEASE_INIT_DOCKER_CONTEXT = "payment-hub-initialization"
RELEASE_INIT_ENV = {
    "SOURCE_SHA": "${{ github.sha }}",
    "CI_RUN_ID": "${{ needs.resolve-candidate.outputs.ci_run_id }}",
    "CI_RUN_ATTEMPT": "${{ needs.resolve-candidate.outputs.ci_run_attempt }}",
    "BUNDLE_ARTIFACT_ID": (
        "${{ needs.resolve-candidate.outputs.bundle_artifact_id }}"
    ),
    "BUNDLE_ARTIFACT_DIGEST": (
        "${{ needs.resolve-candidate.outputs.bundle_artifact_digest }}"
    ),
    "EXPECTED_DESCRIPTOR_SHA": (
        "${{ needs.resolve-candidate.outputs.descriptor_sha256 }}"
    ),
    "PAYMENT_HUB_RELEASE_OPERATION": "${{ inputs.operation }}",
    "GITHUB_TOKEN": "${{ github.token }}",
    "GHCR_PACKAGE_AUDIT_TOKEN": RELEASE_AUDIT_SECRET_EXPRESSION,
}
RELEASE_INIT_VERIFY_HANDOFF_RUN = r'''set -euo pipefail
bash scripts/release.sh --stage verify-handoff \
  --source-sha "$SOURCE_SHA" \
  --bundle-dir "$RUNNER_TEMP/payment-hub-initialization-bundle" \
  --expected-descriptor-sha256 "$EXPECTED_DESCRIPTOR_SHA"'''
RELEASE_INIT_RUN = """set -euo pipefail
python3 -B scripts/init-migrate-package.py --source-sha "$SOURCE_SHA" --ci-run-id "$CI_RUN_ID" --ci-run-attempt "$CI_RUN_ATTEMPT" --bundle-artifact-id "$BUNDLE_ARTIFACT_ID" --bundle-artifact-digest "$BUNDLE_ARTIFACT_DIGEST" --bundle-dir "$RUNNER_TEMP/payment-hub-initialization-bundle" --expected-descriptor-sha256 "$EXPECTED_DESCRIPTOR_SHA" --artifact-root "$RUNNER_TEMP/payment-hub-migrate-initialization" --go "$(command -v go)" --cache-dir "$RUNNER_TEMP/payment-hub-initialization" --prepare-inputs --approve-initialization --acknowledge-public-creation --initialization-preconditions-verified"""

RELEASE_SELECT_RUN = (
    'set -euo pipefail\npython3 -B scripts/select-release-candidate.py select --source-sha '
    '"$SOURCE_SHA" --summary '
    '"$RUNNER_TEMP/payment-hub-release-selection/selection.json" '
    '--github-output "$GITHUB_OUTPUT"'
)
RELEASE_VERIFY_RUN = (
    'set -euo pipefail\npython3 -B scripts/select-release-candidate.py verify --source-sha '
    '"$SOURCE_SHA" --selection '
    '"$RUNNER_TEMP/payment-hub-release-selection/selection.json" '
    '--pointer "$RUNNER_TEMP/payment-hub-release-pointer/pointer.json" '
    '--github-output "$GITHUB_OUTPUT"'
)
RELEASE_POINTER_DOWNLOAD_PATH = "${{ runner.temp }}/payment-hub-release-pointer"
RELEASE_BUNDLE_DOWNLOAD_PATH = "${{ runner.temp }}/payment-hub-release-bundle"
RELEASE_VERIFY_HANDOFF_RUN = """set -euo pipefail
bash scripts/release.sh --stage verify-handoff \\
  --source-sha "$SOURCE_SHA" \\
  --bundle-dir "$RUNNER_TEMP/payment-hub-release-bundle" \\
  --expected-descriptor-sha256 "$EXPECTED_DESCRIPTOR_SHA"
"""
RELEASE_CI_GATE_RUN = '''set -euo pipefail
python3 -B scripts/check-main-ci.py --source-sha "$SOURCE_SHA" --summary "$RUNNER_TEMP/payment-hub-ci-gate/ci-preflight.json" --expected-run-id "$CI_RUN_ID" --expected-run-attempt "$CI_RUN_ATTEMPT"'''
RELEASE_PUBLISH_ENV = {
    "SOURCE_SHA": "${{ github.sha }}",
    "EXPECTED_DESCRIPTOR_SHA": "${{ needs.resolve-candidate.outputs.descriptor_sha256 }}",
    "GITHUB_TOKEN": "${{ github.token }}",
    "GHCR_PACKAGE_AUDIT_TOKEN": RELEASE_AUDIT_SECRET_EXPRESSION,
}
RELEASE_PUBLISH_RUN = """set -euo pipefail
bash scripts/release.sh --stage publish \\
  --source-sha "$SOURCE_SHA" \\
  --go "$(command -v go)" \\
  --cache-dir "$RUNNER_TEMP/payment-hub-release-cache" \\
  --artifact-root "$RUNNER_TEMP/payment-hub-release-publish" \\
  --bundle-dir "$RUNNER_TEMP/payment-hub-release-bundle" \\
  --expected-descriptor-sha256 "$EXPECTED_DESCRIPTOR_SHA" \\
  --approve-external \\
  --package-preconditions-verified \\
  --prepare-inputs
"""
RELEASE_SUMMARY_NAME = (
    "release-summary-${{ github.run_id }}-${{ github.run_attempt }}"
)
RELEASE_EVIDENCE_STEP_NAME = "Write release evidence"
RELEASE_EVIDENCE_SCHEMA = "payment-hub.release-evidence.v1"
RELEASE_EVIDENCE_ENV = {
    "SOURCE_SHA": "${{ github.sha }}",
    "RELEASE_RUN_ID": "${{ github.run_id }}",
    "RELEASE_RUN_ATTEMPT": "${{ github.run_attempt }}",
    "CI_RUN_ID": "${{ needs.resolve-candidate.outputs.ci_run_id }}",
    "CI_RUN_ATTEMPT": (
        "${{ needs.resolve-candidate.outputs.ci_run_attempt }}"
    ),
    "BUNDLE_ARTIFACT_ID": (
        "${{ needs.resolve-candidate.outputs.bundle_artifact_id }}"
    ),
    "BUNDLE_ARTIFACT_DIGEST": (
        "${{ needs.resolve-candidate.outputs.bundle_artifact_digest }}"
    ),
    "EXPECTED_DESCRIPTOR_SHA": (
        "${{ needs.resolve-candidate.outputs.descriptor_sha256 }}"
    ),
    "REPOSITORY": "${{ github.repository }}",
}
RELEASE_EVIDENCE_RUN = r"""set -euo pipefail
python3 -I - "$SOURCE_SHA" "$RELEASE_RUN_ID" "$RELEASE_RUN_ATTEMPT" "$CI_RUN_ID" "$CI_RUN_ATTEMPT" "$BUNDLE_ARTIFACT_ID" "$BUNDLE_ARTIFACT_DIGEST" "$EXPECTED_DESCRIPTOR_SHA" "$REPOSITORY" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
TRUSTED_REPOSITORY = "xtianxx/payment-hub"
EVIDENCE_SCHEMA = "payment-hub.release-evidence.v1"
MAX_DESCRIPTOR_BYTES = 64 * 1024
DESCRIPTOR_SOURCE = Path("payment-hub-release-bundle") / "descriptor.json"
PUBLISH_ROOT = Path("payment-hub-release-publish")
SUMMARY_FILE = "release-summary.json"
DESCRIPTOR_FILE = "descriptor.json"
EVIDENCE_FILE = "release-evidence.json"
def fail(message):
    print("::error::" + message)
    raise SystemExit(1)
def positive(value):
    if re.fullmatch(r"[1-9][0-9]*", value) is None:
        fail("release evidence requires a positive decimal identifier")
    return int(value)
def write_exclusive(path, data):
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
    except OSError:
        fail("release evidence destination is unavailable")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        fail("release evidence write failed")
def main():
    if len(sys.argv) != 10:
        fail("release evidence arguments are incomplete")
    (
        source_sha,
        release_run_id,
        release_run_attempt,
        ci_run_id,
        ci_run_attempt,
        bundle_artifact_id,
        bundle_artifact_digest,
        descriptor_sha256,
        repository,
    ) = sys.argv[1:]
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        fail("release evidence requires a 40-hex source SHA")
    if repository != TRUSTED_REPOSITORY:
        fail("release evidence requires the trusted repository")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", bundle_artifact_digest) is None:
        fail("release evidence requires a canonical bundle artifact digest")
    if re.fullmatch(r"[0-9a-f]{64}", descriptor_sha256) is None:
        fail("release evidence requires a bare 64-hex descriptor digest")
    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "repository": TRUSTED_REPOSITORY,
        "source_sha": source_sha,
        "release_run_id": positive(release_run_id),
        "release_run_attempt": positive(release_run_attempt),
        "ci_run_id": positive(ci_run_id),
        "ci_run_attempt": positive(ci_run_attempt),
        "bundle_artifact_id": positive(bundle_artifact_id),
        "bundle_artifact_digest": bundle_artifact_digest,
        "descriptor_sha256": descriptor_sha256,
    }
    runner_temp = os.environ.get("RUNNER_TEMP", "")
    if not runner_temp or not os.path.isabs(runner_temp):
        fail("release evidence requires an absolute RUNNER_TEMP")
    root = Path(runner_temp) / PUBLISH_ROOT
    source = Path(runner_temp) / DESCRIPTOR_SOURCE
    summary = root / SUMMARY_FILE
    destination = root / DESCRIPTOR_FILE
    evidence_path = root / EVIDENCE_FILE
    try:
        root_info = root.lstat()
        summary_info = summary.lstat()
        source_info = source.lstat()
    except OSError:
        fail("release evidence inputs are missing")
    if (
        root.is_symlink()
        or not stat.S_ISDIR(root_info.st_mode)
        or summary.is_symlink()
        or not stat.S_ISREG(summary_info.st_mode)
        or source.is_symlink()
        or not stat.S_ISREG(source_info.st_mode)
    ):
        fail("release evidence inputs are not regular files")
    if source_info.st_size <= 0 or source_info.st_size > MAX_DESCRIPTOR_BYTES:
        fail("downloaded descriptor size is invalid")
    try:
        original = source.read_bytes()
    except OSError:
        fail("downloaded descriptor cannot be read")
    if hashlib.sha256(original).hexdigest() != descriptor_sha256:
        fail("downloaded descriptor does not match the trusted digest")
    write_exclusive(destination, original)
    try:
        copied = destination.read_bytes()
        original_after = source.read_bytes()
    except OSError:
        fail("copied descriptor cannot be read")
    if hashlib.sha256(copied).hexdigest() != descriptor_sha256:
        fail("copied descriptor does not match the trusted digest")
    if hashlib.sha256(original_after).hexdigest() != descriptor_sha256:
        fail("downloaded descriptor changed during copy")
    text = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    write_exclusive(evidence_path, text.encode("utf-8"))
if __name__ == "__main__":
    main()
PY"""
RELEASE_SUMMARY_FILES = (
    "${{ runner.temp }}/payment-hub-release-publish/release-summary.json",
    "${{ runner.temp }}/payment-hub-release-publish/descriptor.json",
    "${{ runner.temp }}/payment-hub-release-publish/release-evidence.json",
)
RELEASE_SUMMARY_PATH = "\n".join(RELEASE_SUMMARY_FILES)
RELEASE_CI_GATE_SUMMARY = (
    "${{ runner.temp }}/payment-hub-ci-gate/ci-preflight.json"
)

SECRETS_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])secrets(?![A-Za-z0-9_])", re.IGNORECASE
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
                if body_key in body:
                    raise ContractError("workflow-syntax")
                if body_key == "options":
                    if body_value is not None:
                        raise ContractError("workflow-syntax")
                    index += 1
                    options: list[str] = []
                    while index < len(lines) and lines[index][0] > 8:
                        option_indent, option_content, _ = lines[index]
                        if option_indent != 10 or not option_content.startswith(
                            "- "
                        ):
                            raise ContractError("workflow-syntax")
                        option = _unquote(option_content[2:].strip())
                        if not option:
                            raise ContractError("workflow-syntax")
                        options.append(option)
                        index += 1
                    if not options:
                        raise ContractError("workflow-syntax")
                    body[body_key] = options
                    continue
                if body_value is None:
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
        if event == "schedule":
            if value is not None or event in result:
                raise ContractError("workflow-syntax")
            index += 1
            schedule: list[dict[str, str]] = []
            while index < len(lines) and lines[index][0] > 2:
                entry_indent, entry_content, entry_number = lines[index]
                if entry_indent != 4 or not entry_content.startswith("- "):
                    raise ContractError("workflow-syntax")
                key, key_value = _split_kv(entry_content[2:], entry_number)
                if key != "cron" or key_value is None:
                    raise ContractError("workflow-syntax")
                cron = _unquote(key_value)
                if not cron.strip():
                    raise ContractError("workflow-syntax")
                schedule.append({"cron": cron})
                index += 1
            if not schedule:
                raise ContractError("workflow-syntax")
            result[event] = schedule
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


def iter_github_expressions(text: str) -> list[str]:
    """Return the bodies of all ``${{ ... }}`` expressions in text.

    This is deliberately not a general GitHub expression parser.  It only
    locates expression boundaries, ignoring closing braces inside single- or
    double-quoted strings, so callers can fail closed on the ``secrets``
    context.  Unclosed or ambiguous expressions are a fixed contract error.
    """
    expressions: list[str] = []
    index = 0
    while True:
        start = text.find("${{", index)
        if start == -1:
            return expressions
        position = start + 3
        quote: str | None = None
        while position < len(text):
            character = text[position]
            if quote == "'":
                if character == "'":
                    if (
                        position + 1 < len(text)
                        and text[position + 1] == "'"
                    ):
                        position += 2
                        continue
                    quote = None
                    position += 1
                    continue
                position += 1
                continue
            if quote == '"':
                if character == "\\" and position + 1 < len(text):
                    position += 2
                    continue
                if character == '"':
                    quote = None
                position += 1
                continue
            if character in ("'", '"'):
                quote = character
                position += 1
                continue
            if text.startswith("}}", position):
                expressions.append(text[start + 3 : position])
                index = position + 2
                break
            position += 1
        else:
            raise ContractError("workflow-expression-unclosed")


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


def _run(job: dict[str, Any], name: str) -> str:
    step = _find_step(job, name)
    if step is None:
        return ""
    return step.get("run", "")


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


def _validate_checkout(
    job_id: str,
    job: dict[str, Any],
    add: Any,
    *,
    ref: bool,
    prefix: str,
) -> None:
    step = _find_step(job, "Check out repository")
    if step is None:
        add(f"{prefix}-checkout-missing", job_id)
        return
    if step.get("uses") != CHECKOUT:
        add(f"{prefix}-checkout-action", job_id)
    checkout_with = step.get("with", {})
    expected_keys = {"persist-credentials", "fetch-depth"}
    if ref:
        expected_keys.add("ref")
        if checkout_with.get("ref") != "${{ github.sha }}":
            add(f"{prefix}-checkout-ref", job_id)
    if set(checkout_with) != expected_keys:
        add(f"{prefix}-checkout-with", job_id)
    if checkout_with.get("persist-credentials") is not False:
        add(f"{prefix}-checkout-credentials", job_id)
    if checkout_with.get("fetch-depth") != "1":
        add(f"{prefix}-checkout-depth", job_id)


def _validate_setup_go(
    job_id: str, job: dict[str, Any], add: Any, *, prefix: str
) -> None:
    step = _find_step(job, "Set up Go 1.27.4")
    if step is None:
        add(f"{prefix}-setup-go-missing", job_id)
        return
    if step.get("uses") != SETUP_GO:
        add(f"{prefix}-setup-go-action", job_id)
    setup_with = step.get("with", {})
    if set(setup_with) != {"go-version", "check-latest", "cache"}:
        add(f"{prefix}-setup-go-with", job_id)
    if setup_with.get("go-version") != "1.27.4":
        add(f"{prefix}-setup-go-version", job_id)
    if setup_with.get("check-latest") is not False:
        add(f"{prefix}-setup-go-latest", job_id)
    if setup_with.get("cache") is not False:
        add(f"{prefix}-setup-go-cache", job_id)


def _validate_isolation(
    job_id: str,
    job: dict[str, Any],
    add: Any,
    *,
    step_name: str,
    base: str,
    prefix: str,
) -> None:
    step = _find_step(job, step_name)
    if step is None:
        add(f"{prefix}-isolation-missing", job_id)
        return
    if step.get("shell") != "bash":
        add(f"{prefix}-isolation-shell", job_id)
    run = step.get("run", "")
    for token in (
        "set -euo pipefail",
        "command -v go",
        "go telemetry off",
        "GITHUB_ENV",
        base,
        "XDG_CONFIG_HOME",
        "GOPATH",
        "GOMODCACHE",
        "GOCACHE",
        "TMPDIR",
    ):
        if token not in run:
            add(f"{prefix}-isolation-command", job_id)
    for token in ("set -x", "printenv", "tee ", "env |"):
        if token in run:
            add(f"{prefix}-isolation-unsafe", job_id)


def _validate_safe_upload(
    job_id: str,
    job: dict[str, Any],
    add: Any,
    *,
    step_name: str,
    artifact_name: str,
    paths: tuple[str, ...],
    prefix: str,
) -> None:
    step = _find_step(job, step_name)
    if step is None:
        add(f"{prefix}-upload-missing", job_id)
        return
    if step.get("if") != "always()":
        add(f"{prefix}-upload-condition", job_id)
    if step.get("uses") != UPLOAD:
        add(f"{prefix}-upload-action", job_id)
    upload_with = step.get("with", {})
    if set(upload_with) != {
        "name",
        "path",
        "if-no-files-found",
        "retention-days",
        "include-hidden-files",
    }:
        add(f"{prefix}-upload-with", job_id)
    if upload_with.get("name") != artifact_name:
        add(f"{prefix}-upload-name", job_id)
    if upload_with.get("if-no-files-found") != "error":
        add(f"{prefix}-upload-missing-policy", job_id)
    if upload_with.get("retention-days") != 30:
        add(f"{prefix}-upload-retention", job_id)
    if upload_with.get("include-hidden-files") is not False:
        add(f"{prefix}-upload-hidden", job_id)
    actual_paths = tuple(_upload_paths(step))
    if actual_paths != paths:
        add(f"{prefix}-upload-paths", job_id)
    for path in actual_paths:
        if "*" in path or "?" in path or "[" in path:
            add(f"{prefix}-upload-wildcard", job_id)
        if "/artifacts/" in path and "/upload/" not in path:
            add(f"{prefix}-upload-paths", job_id)


def _validate_docker_steps(
    job_id: str,
    job: dict[str, Any],
    add: Any,
    *,
    context: str,
    prefix: str,
) -> None:
    setup = _find_step(job, "Set up Docker 29.9.3 with containerd image store")
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
        if setup_with.get("version") != "v29.9.3":
            add(f"{prefix}-version", job_id)
        if setup_with.get("context") != context:
            add(f"{prefix}-context", job_id)
        if setup_with.get("set-host") not in (True, "true"):
            add(f"{prefix}-set-host", job_id)
        if setup_with.get("runtime-basedir") != (
            "${{ runner.temp }}/payment-hub-docker"
        ):
            add(f"{prefix}-runtime-basedir", job_id)
        if setup_with.get("daemon-config") != (
            '{"features":{"containerd-snapshotter":true}}'
        ):
            add(f"{prefix}-daemon-config", job_id)
        if setup_with.get("github-token") != "":
            add(f"{prefix}-token", job_id)
    verify = _find_step(job, "Verify Docker image store")
    if verify is None:
        add(f"{prefix}-verify-missing", job_id)
    else:
        if verify.get("shell") != "bash":
            add(f"{prefix}-verify-shell", job_id)
        if "if" in verify or "continue-on-error" in verify:
            add(f"{prefix}-verify-skip", job_id)
        run = verify.get("run", "")
        for token in (
            "set -euo pipefail",
            "DOCKER_HOST",
            '!= "29.9.3"',
            "io.containerd.snapshotter.v1",
            'DOCKER_CONFIG="$auth_dir"',
        ):
            if token not in run:
                add(f"{prefix}-verify-run", job_id)
        for token in ("set -x", "printenv", "tee "):
            if token in run:
                add(f"{prefix}-verify-unsafe", job_id)


def _validate_common_forbidden_tokens(
    text: str, add: Any, *, allow_secrets: bool = False
) -> None:
    for token in (
        "pull_request_target",
        "workflow_run",
        "workflow_call",
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
            add("workflow-forbidden-token")
    if not allow_secrets and SECRETS_TOKEN_RE.search(text):
        add("workflow-secrets")


def _validate_step_if_policy(
    job_id: str,
    job: dict[str, Any],
    add: Any,
    *,
    prefix: str,
    allowed_always: frozenset[str],
    allowed_guards: dict[str, str] | None = None,
) -> None:
    guards = allowed_guards or {}
    for step in job.get("steps", []):
        name = step.get("name", "")
        if "if" not in step:
            continue
        value = step["if"]
        if name in allowed_always and value == "always()":
            continue
        if name in guards and value == guards[name]:
            continue
        add(f"{prefix}-step-if", job_id)


def validate_ci_workflow(
    workflow: dict[str, Any], text: str, repo: Path = REPO_ROOT
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []

    def add(kind: str, package: str | None = None) -> None:
        issues.append(ContractIssue(kind, package))

    if set(workflow) - {"name", "on", "concurrency", "permissions", "env", "jobs"}:
        add("ci-workflow-top-level")
    if workflow.get("name") != CI_WORKFLOW_NAME:
        add("ci-workflow-name")
    if workflow.get("on") != CI_TRIGGERS:
        add("ci-workflow-triggers")
    if workflow.get("permissions") != {"contents": "read"}:
        add("ci-workflow-permissions")
    if workflow.get("concurrency") != CI_CONCURRENCY:
        add("ci-workflow-concurrency")
    if workflow.get("env") != CI_TOP_ENV:
        add("ci-workflow-env")

    jobs = workflow.get("jobs", {})
    if tuple(jobs) != CI_JOBS:
        add("ci-workflow-jobs")
    for job_id in CI_JOBS:
        job = jobs.get(job_id)
        if not isinstance(job, dict):
            add("ci-workflow-jobs", job_id)
            continue
        if job.get("name") != CI_JOB_NAMES[job_id]:
            add("ci-job-name", job_id)
        if job.get("runs-on") != "ubuntu-24.04":
            add("ci-job-runs-on", job_id)
        if job.get("timeout-minutes") != CI_TIMEOUTS[job_id]:
            add("ci-job-timeout", job_id)
        if "permissions" in job:
            add("ci-job-permissions", job_id)
        if job_id == "ci-gate":
            if job.get("if") != CI_GATE_IF:
                add("ci-gate-always", job_id)
        elif "if" in job:
            add("ci-job-condition", job_id)
        if job_id == "integration-tests":
            if job.get("needs") != "unit-tests":
                add("ci-integration-needs", job_id)
            if job.get("env") != {"CGO_ENABLED": "1", "GOMAXPROCS": "2"}:
                add("ci-integration-env", job_id)
        elif job_id == "ci-gate":
            needs = job.get("needs")
            if not isinstance(needs, list) or tuple(needs) != CI_GATE_NEEDS:
                add("ci-gate-needs", job_id)
            elif set(needs) != set(jobs) - {"ci-gate"}:
                add("ci-gate-needs", job_id)
        else:
            if "needs" in job:
                add("ci-job-needs", job_id)
            if "env" in job:
                add("ci-job-env", job_id)
        if tuple(_step_names(job)) != CI_STEP_NAMES[job_id]:
            add("ci-job-steps", job_id)
        _validate_step_if_policy(
            job_id,
            job,
            add,
            prefix="ci",
            allowed_always=CI_ALWAYS_IF_STEPS,
            allowed_guards=(
                CI_CANDIDATE_GUARD_STEPS if job_id == "image-acceptance" else {}
            ),
        )
        for step in job.get("steps", []):
            if "continue-on-error" in step:
                add("ci-continue-on-error", job_id)

    uses = [
        step.get("uses")
        for job in jobs.values()
        if isinstance(job, dict)
        for step in job.get("steps", [])
        if "uses" in step
    ]
    if set(uses) != CI_ACTION_PINS:
        add("ci-action-pins")
    for pin, count in CI_ACTION_COUNTS.items():
        if uses.count(pin) != count:
            add("ci-action-pins")

    for job_id in ("format", "vet", "build", "unit-tests", "integration-tests",
                   "image-acceptance", "vulnerability-check"):
        job = jobs.get(job_id)
        if not isinstance(job, dict):
            continue
        _validate_checkout(job_id, job, add, ref=False, prefix="ci")
        _validate_setup_go(job_id, job, add, prefix="ci")

    format_job = jobs.get("format", {})
    if isinstance(format_job, dict):
        _validate_isolation(
            "format",
            format_job,
            add,
            step_name="Isolate format Go config and caches",
            base="payment-hub-format",
            prefix="ci-format",
        )
        run = _run(format_job, "Check tracked Go formatting")
        if run.strip() != CI_FORMAT_RUN.strip():
            add("ci-format-run")

    vet_job = jobs.get("vet", {})
    if isinstance(vet_job, dict):
        _validate_isolation(
            "vet",
            vet_job,
            add,
            step_name="Isolate vet Go config and caches",
            base="payment-hub-vet",
            prefix="ci-vet",
        )
        run = _run(vet_job, "Run go vet")
        if run.strip() != CI_VET_RUN.strip():
            add("ci-vet-run")

    build_job = jobs.get("build", {})
    if isinstance(build_job, dict):
        _validate_isolation(
            "build",
            build_job,
            add,
            step_name="Isolate build Go config and caches",
            base="payment-hub-build",
            prefix="ci-build",
        )
        run = _run(build_job, "Run explicit Go build")
        if run.strip() != CI_BUILD_RUN.strip():
            add("ci-build-run")
        for token in ("build-images.py", "docker build", "docker push"):
            if token in run:
                add("ci-build-unsafe")

    unit_job = jobs.get("unit-tests", {})
    if isinstance(unit_job, dict):
        _validate_isolation(
            "unit-tests",
            unit_job,
            add,
            step_name="Isolate Go config and caches",
            base="payment-hub-setup",
            prefix="ci-unit",
        )
        contract = _find_step(unit_job, "Check CI contract metadata")
        if contract is None:
            add("ci-unit-contract-missing")
        else:
            if contract.get("shell") != "bash":
                add("ci-unit-contract-shell")
            if contract.get("env") != {
                "CGO_ENABLED": "1",
                "GOMAXPROCS": "2",
                "GOPROXY": "off",
                "GOSUMDB": "off",
            }:
                add("ci-unit-contract-env")
            if contract.get("run", "").strip() != CI_METADATA_RUN.strip():
                add("workflow-preflight-command")
                add("ci-unit-contract-run")
        run = _run(unit_job, "Run foundation unit checks")
        for token in (
            "set -euo pipefail",
            "scripts/ci_checks.py run-default",
            '--report-dir "$RUNNER_TEMP/payment-hub-setup/reports"',
            "--summary default",
            "570",
            "-timeout=90s",
            "20 分钟",
            "共享整批预算",
        ):
            if token not in run:
                add("ci-unit-run")
        for token in ("gofmt -l", "go vet -mod"):
            if token in run:
                add("ci-unit-duplicate-static-check")
        launcher = _run(
            unit_job, "Check launcher syntax and safety regressions"
        )
        for token in (
            "sh -n deploy/goose-entrypoint.sh",
            "unittest discover -s scripts",
        ):
            if token not in launcher:
                add("ci-unit-launcher")
        export = _find_step(unit_job, "Export safe setup reports")
        if export is None:
            add("ci-unit-export-missing")
        else:
            if export.get("if") != "always()":
                add("ci-unit-export-condition")
            if "--expect default" not in export.get("run", ""):
                add("ci-unit-export-run")
        _validate_safe_upload(
            "unit-tests",
            unit_job,
            add,
            step_name="Upload safe setup reports",
            artifact_name=CI_UNIT_UPLOAD_NAME,
            paths=CI_UNIT_UPLOAD_PATHS,
            prefix="ci-unit",
        )

    integration_job = jobs.get("integration-tests", {})
    if isinstance(integration_job, dict):
        _validate_isolation(
            "integration-tests",
            integration_job,
            add,
            step_name="Isolate database foundation Go config and caches",
            base="payment-hub-db",
            prefix="ci-integration",
        )
        run = _run(integration_job, "Run database foundation checks")
        for token in (
            "set -euo pipefail",
            "scripts/check-db-foundation.py",
            '--go "$(command -v go)"',
            '--cache-dir "$RUNNER_TEMP/payment-hub-db"',
            "--download-modules",
            "--prepare-images",
            "--cleanup-owned-resources",
            '--artifact-root "$RUNNER_TEMP/payment-hub-db/artifacts"',
        ):
            if token not in run:
                add("ci-integration-run")
        cancellation = _run(
            integration_job, "Verify cancellation cleanup with real PostgreSQL"
        )
        if cancellation.strip() != CI_CANCELLATION_RUN.strip():
            add("ci-integration-cancellation")
        summary = _run(
            integration_job, "Summarize database foundation results"
        )
        for token in (
            "--summary db",
            "570",
            "120 秒",
            "ready 180s / exit 120s / total 360s",
            "20 分钟",
            "kill/reap",
        ):
            if token not in summary:
                add("ci-integration-summary")
        export = _find_step(integration_job, "Export safe database reports")
        if export is None:
            add("ci-integration-export-missing")
        else:
            if export.get("if") != "always()":
                add("ci-integration-export-condition")
            if "--expect db,cancellation,us1,us2,us3" not in export.get(
                "run", ""
            ):
                add("ci-integration-export-run")
        _validate_safe_upload(
            "integration-tests",
            integration_job,
            add,
            step_name="Upload safe database reports",
            artifact_name=CI_INTEGRATION_UPLOAD_NAME,
            paths=CI_INTEGRATION_UPLOAD_PATHS,
            prefix="ci-integration",
        )

    image_job = jobs.get("image-acceptance", {})
    if isinstance(image_job, dict):
        names = _step_names(image_job)
        if all(name in names for name in CI_IMAGE_CANDIDATE_ORDER):
            positions = [names.index(name) for name in CI_IMAGE_CANDIDATE_ORDER]
            if positions != sorted(positions):
                add("ci-image-candidate-order")
        else:
            add("ci-image-candidate-order")
        _validate_isolation(
            "image-acceptance",
            image_job,
            add,
            step_name="Isolate image acceptance Go config and caches",
            base="payment-hub-images",
            prefix="ci-image",
        )
        _validate_docker_steps(
            "image-acceptance",
            image_job,
            add,
            context="payment-hub-check",
            prefix="ci-image-docker",
        )
        build = _find_step(image_job, "Build and accept paired images")
        if build is None:
            add("ci-image-build-missing")
        else:
            if build.get("shell") != "bash":
                add("ci-image-build-shell")
            if build.get("env") != {"CGO_ENABLED": "1", "GOMAXPROCS": "2"}:
                add("ci-image-build-env")
            run = build.get("run", "")
            if run.strip() != CI_IMAGE_BUILD_RUN.strip():
                add("ci-image-build-run")
            for token in ("docker build", "docker push", "docker login", "secrets."):
                if token in run:
                    add("ci-image-build-unsafe")
        handoff = _find_step(image_job, "Verify image handoff without publishing")
        if handoff is None:
            add("ci-image-handoff-missing")
        else:
            if handoff.get("shell") != "bash":
                add("ci-image-handoff-shell")
            if "if" in handoff or "continue-on-error" in handoff:
                add("ci-image-handoff-skip")
            if "env" in handoff:
                add("ci-image-handoff-env")
            run = handoff.get("run", "")
            if run.strip() != CI_IMAGE_HANDOFF_RUN.strip():
                add("ci-image-handoff-run")
            for token in ("build-images.py", "pair.tar", "descriptor.json",
                          "GITHUB_TOKEN", "secrets."):
                if token in run:
                    add("ci-image-handoff-run")

        export_candidate = _find_step(image_job, "Export release candidate")
        if export_candidate is None:
            add("ci-candidate-export-missing")
        else:
            if export_candidate.get("id") != "export-candidate":
                add("ci-candidate-export-id")
            if export_candidate.get("if") != CI_CANDIDATE_GUARD:
                add("ci-candidate-export-guard")
            if export_candidate.get("shell") != "bash":
                add("ci-candidate-export-shell")
            if set(export_candidate) != {"name", "id", "if", "shell", "run"}:
                add("ci-candidate-export-shape")
            if export_candidate.get("run", "").strip() != CI_CANDIDATE_EXPORT_RUN:
                add("ci-candidate-export-run")
            if "secrets." in export_candidate.get("run", ""):
                add("ci-candidate-export-unsafe")

        upload_candidate = _find_step(
            image_job, "Upload release candidate bundle"
        )
        if upload_candidate is None:
            add("ci-candidate-bundle-missing")
        else:
            if upload_candidate.get("id") != "upload-candidate":
                add("ci-candidate-bundle-id")
            if upload_candidate.get("if") != CI_CANDIDATE_GUARD:
                add("ci-candidate-bundle-guard")
            if upload_candidate.get("uses") != UPLOAD:
                add("ci-candidate-bundle-action")
            if set(upload_candidate) != {"name", "id", "if", "uses", "with"}:
                add("ci-candidate-bundle-shape")
            upload_with = upload_candidate.get("with", {})
            if set(upload_with) != {
                "name",
                "path",
                "if-no-files-found",
                "retention-days",
                "include-hidden-files",
            }:
                add("ci-candidate-bundle-with")
            if upload_with.get("name") != CI_CANDIDATE_BUNDLE_NAME:
                add("ci-candidate-bundle-name")
            if upload_with.get("path") != CI_CANDIDATE_BUNDLE_PATH:
                add("ci-candidate-bundle-path")
            if upload_with.get("if-no-files-found") != "error":
                add("ci-candidate-bundle-missing-policy")
            if upload_with.get("retention-days") != 30:
                add("ci-candidate-bundle-retention")
            if upload_with.get("include-hidden-files") is not False:
                add("ci-candidate-bundle-hidden")
            if any(token in str(upload_with.get("path", "")) for token in
                   ("*", "?", "[")):
                add("ci-candidate-bundle-wildcard")

        pointer = _find_step(image_job, "Write release candidate pointer")
        if pointer is None:
            add("ci-candidate-pointer-missing")
        else:
            if pointer.get("id") != "write-candidate-pointer":
                add("ci-candidate-pointer-id")
            if pointer.get("if") != CI_CANDIDATE_GUARD:
                add("ci-candidate-pointer-guard")
            if pointer.get("shell") != "bash":
                add("ci-candidate-pointer-shell")
            if pointer.get("env") != CI_CANDIDATE_POINTER_ENV:
                add("ci-candidate-pointer-env")
            run = pointer.get("run", "")
            if run.strip() != CI_CANDIDATE_POINTER_RUN:
                add("ci-candidate-pointer-run")
            if "${{" in run or "secrets." in run:
                add("ci-candidate-pointer-unsafe")

        pointer_upload = _find_step(
            image_job, "Upload release candidate pointer"
        )
        if pointer_upload is None:
            add("ci-candidate-pointer-upload-missing")
        else:
            if pointer_upload.get("id") != "upload-candidate-pointer":
                add("ci-candidate-pointer-upload-id")
            if pointer_upload.get("if") != CI_CANDIDATE_GUARD:
                add("ci-candidate-pointer-upload-guard")
            if pointer_upload.get("uses") != UPLOAD:
                add("ci-candidate-pointer-upload-action")
            pointer_with = pointer_upload.get("with", {})
            if set(pointer_with) != {
                "name",
                "path",
                "if-no-files-found",
                "retention-days",
                "include-hidden-files",
            }:
                add("ci-candidate-pointer-upload-with")
            if pointer_with.get("name") != CI_CANDIDATE_POINTER_NAME:
                add("ci-candidate-pointer-upload-name")
            if pointer_with.get("path") != CI_CANDIDATE_POINTER_PATH:
                add("ci-candidate-pointer-upload-path")
            if pointer_with.get("if-no-files-found") != "error":
                add("ci-candidate-pointer-upload-missing-policy")
            if pointer_with.get("retention-days") != 30:
                add("ci-candidate-pointer-upload-retention")
            if pointer_with.get("include-hidden-files") is not False:
                add("ci-candidate-pointer-upload-hidden")
            for token in ("*", "?", "["):
                if token in str(pointer_with.get("path", "")):
                    add("ci-candidate-pointer-upload-wildcard")

        _validate_safe_upload(
            "image-acceptance",
            image_job,
            add,
            step_name="Upload safe image acceptance summaries",
            artifact_name=CI_IMAGE_UPLOAD_NAME,
            paths=CI_IMAGE_UPLOAD_PATHS,
            prefix="ci-image",
        )

    vulnerability_job = jobs.get("vulnerability-check", {})
    if isinstance(vulnerability_job, dict):
        _validate_isolation(
            "vulnerability-check",
            vulnerability_job,
            add,
            step_name="Isolate vulnerability Go config and caches",
            base="payment-hub-vulnerability",
            prefix="ci-vulnerability",
        )
        scan = _find_step(vulnerability_job, "Run pinned vulnerability scan")
        if scan is None:
            add("ci-vulnerability-scan-missing")
        else:
            if scan.get("shell") != "bash":
                add("ci-vulnerability-scan-shell")
            if "continue-on-error" in scan:
                add("ci-vulnerability-scan-skip")
            if scan.get("run", "").strip() != (
                CI_VULNERABILITY_SCAN_RUN.strip()
            ):
                add("ci-vulnerability-scan-run")
        export = _find_step(vulnerability_job, "Export safe vulnerability report")
        if export is None:
            add("ci-vulnerability-export-missing")
        else:
            if export.get("if") != "always()":
                add("ci-vulnerability-export-condition")
            if "--expect vulnerability" not in export.get("run", ""):
                add("ci-vulnerability-export-run")
        _validate_safe_upload(
            "vulnerability-check",
            vulnerability_job,
            add,
            step_name="Upload safe vulnerability reports",
            artifact_name=CI_VULNERABILITY_UPLOAD_NAME,
            paths=CI_VULNERABILITY_UPLOAD_PATHS,
            prefix="ci-vulnerability",
        )

    gate = jobs.get("ci-gate", {})
    if isinstance(gate, dict):
        if gate.get("env") != CI_GATE_ENV:
            add("ci-gate-env")
        steps = gate.get("steps", [])
        if len(steps) != 1:
            add("ci-gate-steps")
        else:
            step = steps[0]
            if set(step) != {"name", "shell", "run"}:
                add("ci-gate-steps")
            if step.get("shell") != "bash":
                add("ci-gate-steps")
            if step.get("run", "").strip() != CI_GATE_RUN:
                add("ci-gate-run")

    _validate_common_forbidden_tokens(text, add)
    for token in FORBIDDEN_WORKFLOW_TOKENS:
        if token in text:
            add("workflow-hardcoded-inventory")
    return issues


def validate_nightly_workflow(
    workflow: dict[str, Any], text: str, repo: Path = REPO_ROOT
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []

    def add(kind: str, package: str | None = None) -> None:
        issues.append(ContractIssue(kind, package))

    if set(workflow) - {"name", "on", "concurrency", "permissions", "env", "jobs"}:
        add("nightly-workflow-top-level")
    if workflow.get("name") != NIGHTLY_WORKFLOW_NAME:
        add("nightly-workflow-name")
    if workflow.get("on") != NIGHTLY_TRIGGERS:
        add("nightly-workflow-triggers")
    if workflow.get("permissions") != {"contents": "read"}:
        add("nightly-workflow-permissions")
    if workflow.get("concurrency") != NIGHTLY_CONCURRENCY:
        add("nightly-workflow-concurrency")
    if workflow.get("env") != NIGHTLY_TOP_ENV:
        add("nightly-workflow-env")

    jobs = workflow.get("jobs", {})
    if tuple(jobs) != NIGHTLY_JOBS:
        add("nightly-workflow-jobs")
    job = jobs.get("vulnerability-check")
    if not isinstance(job, dict):
        add("nightly-workflow-jobs", "vulnerability-check")
    else:
        if job.get("name") != NIGHTLY_JOB_NAME:
            add("nightly-job-name")
        if job.get("runs-on") != "ubuntu-24.04":
            add("nightly-job-runs-on")
        if job.get("timeout-minutes") != NIGHTLY_TIMEOUT:
            add("nightly-job-timeout")
        for key in ("permissions", "needs", "if", "env"):
            if key in job:
                add("nightly-job-shape")
        if tuple(_step_names(job)) != NIGHTLY_STEP_NAMES:
            add("nightly-job-steps")
        _validate_step_if_policy(
            "vulnerability-check",
            job,
            add,
            prefix="nightly",
            allowed_always=NIGHTLY_ALWAYS_IF_STEPS,
        )
        for step in job.get("steps", []):
            if "continue-on-error" in step:
                add("nightly-continue-on-error")
        _validate_checkout("vulnerability-check", job, add, ref=False, prefix="nightly")
        _validate_setup_go(
            "vulnerability-check", job, add, prefix="nightly"
        )
        _validate_isolation(
            "vulnerability-check",
            job,
            add,
            step_name="Isolate vulnerability Go config and caches",
            base="payment-hub-vulnerability",
            prefix="nightly-vulnerability",
        )
        scan = _find_step(job, "Run pinned vulnerability scan")
        if scan is None:
            add("nightly-scan-missing")
        else:
            if scan.get("shell") != "bash":
                add("nightly-scan-shell")
            if "continue-on-error" in scan:
                add("nightly-scan-skip")
            if scan.get("run", "").strip() != (
                CI_VULNERABILITY_SCAN_RUN.strip()
            ):
                add("nightly-scan-run")
        export = _find_step(job, "Export safe vulnerability report")
        if export is None:
            add("nightly-export-missing")
        else:
            if export.get("if") != "always()":
                add("nightly-export-condition")
            if "--expect vulnerability" not in export.get("run", ""):
                add("nightly-export-run")
        _validate_safe_upload(
            "vulnerability-check",
            job,
            add,
            step_name="Upload safe vulnerability reports",
            artifact_name=CI_VULNERABILITY_UPLOAD_NAME,
            paths=CI_VULNERABILITY_UPLOAD_PATHS,
            prefix="nightly",
        )
        uses = [
            step.get("uses")
            for step in job.get("steps", [])
            if "uses" in step
        ]
        if set(uses) != NIGHTLY_ACTION_PINS:
            add("nightly-action-pins")
        for pin, count in NIGHTLY_ACTION_COUNTS.items():
            if uses.count(pin) != count:
                add("nightly-action-pins")

    _validate_common_forbidden_tokens(text, add)
    for token in (
        "workflow_dispatch",
        "ci-candidate",
        "select-release-candidate",
        "ci-release-bundle",
        "ci-release-pointer",
        "ci-gate",
    ):
        if token in text:
            add("nightly-forbidden-token")
    for token in FORBIDDEN_WORKFLOW_TOKENS:
        if token in text:
            add("workflow-hardcoded-inventory")
    return issues


def _release_inputs_ok(workflow: dict[str, Any]) -> bool:
    on_value = workflow.get("on")
    if not isinstance(on_value, dict) or set(on_value) != {"workflow_dispatch"}:
        return False
    dispatch = on_value["workflow_dispatch"]
    if not isinstance(dispatch, dict) or set(dispatch) != {"inputs"}:
        return False
    inputs = dispatch["inputs"]
    if not isinstance(inputs, dict) or tuple(inputs) != RELEASE_INPUT_NAMES:
        return False
    for name in RELEASE_INPUT_NAMES:
        body = inputs.get(name)
        if not isinstance(body, dict):
            return False
        if body.get("required") is not True:
            return False
        if body.get("description") != RELEASE_INPUT_DESCRIPTIONS[name]:
            return False
        if name == "operation":
            if set(body) != {
                "description",
                "required",
                "default",
                "type",
                "options",
            }:
                return False
            if body.get("default") != "publish":
                return False
            if body.get("type") != "choice":
                return False
            if body.get("options") != list(RELEASE_OPERATION_OPTIONS):
                return False
        elif name == "packages_verified":
            if set(body) != {"description", "required", "default", "type"}:
                return False
            if body.get("default") is not False:
                return False
            if body.get("type") != "boolean":
                return False
        else:
            if set(body) != {"description", "required", "type"}:
                return False
            if body.get("type") != "string":
                return False
    return True


def validate_release_workflow(
    workflow: dict[str, Any], text: str, repo: Path = REPO_ROOT
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []

    def add(kind: str, package: str | None = None) -> None:
        issues.append(ContractIssue(kind, package))

    if set(workflow) - {"name", "on", "concurrency", "permissions", "env", "jobs"}:
        add("release-workflow-top-level")
    if workflow.get("name") != RELEASE_WORKFLOW_NAME:
        add("release-workflow-name")
    if not _release_inputs_ok(workflow):
        add("release-workflow-trigger-inputs")
    for token in RELEASE_LEGACY_INPUTS:
        if token in text:
            add("release-legacy-input")
    if "inputs.confirmation" in text:
        add("release-confirmation-raw-input")
    if workflow.get("permissions") != {"contents": "read"}:
        add("release-workflow-permissions")
    if workflow.get("concurrency") != RELEASE_CONCURRENCY:
        add("release-workflow-concurrency")
    if workflow.get("env") != RELEASE_TOP_ENV:
        add("release-workflow-env")

    jobs = workflow.get("jobs", {})
    if tuple(jobs) != RELEASE_JOBS:
        add("release-workflow-jobs")
    resolve = jobs.get("resolve-candidate")
    upload = jobs.get("upload")
    initialize = jobs.get("initialize-migrate")
    if not isinstance(resolve, dict):
        add("release-workflow-jobs", "resolve-candidate")
    if not isinstance(upload, dict):
        add("release-workflow-jobs", "upload")
    if not isinstance(initialize, dict):
        add("release-workflow-jobs", "initialize-migrate")

    uses = [
        step.get("uses")
        for job in jobs.values()
        if isinstance(job, dict)
        for step in job.get("steps", [])
        if "uses" in step
    ]
    if set(uses) != RELEASE_ACTION_PINS:
        add("release-action-pins")
    for pin, count in RELEASE_ACTION_COUNTS.items():
        if uses.count(pin) != count:
            add("release-action-pins")

    if isinstance(resolve, dict):
        if resolve.get("name") != RELEASE_JOB_NAMES["resolve-candidate"]:
            add("release-resolve-name")
        if resolve.get("runs-on") != "ubuntu-24.04":
            add("release-resolve-runs-on")
        if resolve.get("timeout-minutes") != RELEASE_TIMEOUT:
            add("release-resolve-timeout")
        if resolve.get("permissions") != {
            "contents": "read",
            "actions": "read",
        }:
            add("release-resolve-permissions")
        for key in ("env", "needs", "if"):
            if key in resolve:
                add("release-resolve-shape")
        if resolve.get("outputs") != RELEASE_RESOLVE_OUTPUTS:
            add("release-resolve-outputs")
        if tuple(_step_names(resolve)) != RELEASE_RESOLVE_STEP_NAMES:
            add("release-resolve-steps")
        _validate_step_if_policy(
            "resolve-candidate",
            resolve,
            add,
            prefix="release-resolve",
            allowed_always=frozenset(),
        )
        gate = _find_step(resolve, "Verify manual operation preconditions")
        if gate is None:
            add("release-gate-missing")
        else:
            if gate.get("shell") != "bash":
                add("release-gate-shell")
            if "if" in gate or "continue-on-error" in gate:
                add("release-gate-skip")
            if gate.get("env") != RELEASE_GATE_ENV:
                add("release-gate-env")
            if gate.get("run", "").strip() != RELEASE_GATE_RUN.strip():
                add("release-gate-run")
        _validate_checkout(
            "resolve-candidate", resolve, add, ref=True, prefix="release-resolve"
        )
        select = _find_step(resolve, "Select release candidate")
        if select is None:
            add("release-select-missing")
        else:
            if select.get("id") != "select-candidate":
                add("release-select-id")
            if select.get("shell") != "bash":
                add("release-select-shell")
            if select.get("env") != {
                "GITHUB_TOKEN": "${{ github.token }}",
                "SOURCE_SHA": "${{ github.sha }}",
            }:
                add("release-select-env")
            if select.get("run", "").strip() != RELEASE_SELECT_RUN:
                add("release-select-run")
            if "secrets." in select.get("run", ""):
                add("release-select-unsafe")
        pointer_download = _find_step(
            resolve, "Download release candidate pointer"
        )
        if pointer_download is None:
            add("release-pointer-download-missing")
        else:
            if pointer_download.get("uses") != DOWNLOAD:
                add("release-pointer-download-action")
            download_with = pointer_download.get("with", {})
            if set(download_with) != {
                "artifact-ids",
                "run-id",
                "github-token",
                "repository",
                "path",
                "merge-multiple",
                "digest-mismatch",
            }:
                add("release-pointer-download-with")
            if download_with.get("artifact-ids") != (
                "${{ steps.select-candidate.outputs.pointer_artifact_id }}"
            ):
                add("release-pointer-download-artifact")
            if download_with.get("run-id") != (
                "${{ steps.select-candidate.outputs.ci_run_id }}"
            ):
                add("release-pointer-download-run")
            if download_with.get("github-token") != "${{ github.token }}":
                add("release-pointer-download-token")
            if download_with.get("repository") != "${{ github.repository }}":
                add("release-pointer-download-repository")
            if download_with.get("path") != RELEASE_POINTER_DOWNLOAD_PATH:
                add("release-pointer-download-path")
            if download_with.get("merge-multiple") is not True:
                add("release-pointer-download-merge")
            if download_with.get("digest-mismatch") != "error":
                add("release-pointer-download-digest")
            if "name" in download_with or "latest" in str(download_with).lower():
                add("release-pointer-download-floating")
        verify = _find_step(resolve, "Verify release candidate")
        if verify is None:
            add("release-verify-missing")
        else:
            if verify.get("id") != "verify-candidate":
                add("release-verify-id")
            if verify.get("shell") != "bash":
                add("release-verify-shell")
            if verify.get("env") != {"SOURCE_SHA": "${{ github.sha }}"}:
                add("release-verify-env")
            if verify.get("run", "").strip() != RELEASE_VERIFY_RUN:
                add("release-verify-run")
            if "secrets." in verify.get("run", "") or "secrets." in str(
                verify.get("env", {})
            ):
                add("release-verify-unsafe")

    if isinstance(upload, dict):
        if upload.get("name") != RELEASE_JOB_NAMES["upload"]:
            add("release-upload-name")
        if upload.get("runs-on") != "ubuntu-24.04":
            add("release-upload-runs-on")
        if upload.get("timeout-minutes") != RELEASE_TIMEOUT:
            add("release-upload-timeout")
        if upload.get("needs") != "resolve-candidate":
            add("release-upload-needs")
        if upload.get("if") != RELEASE_PUBLISH_GUARD:
            add("release-upload-owner-guard")
        if upload.get("permissions") != {
            "contents": "read",
            "actions": "read",
            "packages": "write",
        }:
            add("release-upload-permissions")
        if "env" in upload:
            add("release-upload-env")
        if tuple(_step_names(upload)) != RELEASE_UPLOAD_STEP_NAMES:
            add("release-upload-steps")
        _validate_step_if_policy(
            "upload",
            upload,
            add,
            prefix="release-upload",
            allowed_always=RELEASE_ALWAYS_IF_STEPS,
        )
        identity = _find_step(upload, "Verify resolved candidate identity")
        if identity is None:
            add("release-identity-missing")
        else:
            if identity.get("shell") != "bash":
                add("release-identity-shell")
            if "if" in identity or "continue-on-error" in identity:
                add("release-identity-skip")
            if identity.get("env") != {
                "CI_RUN_ID": "${{ needs.resolve-candidate.outputs.ci_run_id }}",
                "CI_RUN_ATTEMPT": (
                    "${{ needs.resolve-candidate.outputs.ci_run_attempt }}"
                ),
                "BUNDLE_ARTIFACT_ID": (
                    "${{ needs.resolve-candidate.outputs.bundle_artifact_id }}"
                ),
                "BUNDLE_ARTIFACT_DIGEST": (
                    "${{ needs.resolve-candidate.outputs.bundle_artifact_digest }}"
                ),
                "EXPECTED_DESCRIPTOR_SHA": (
                    "${{ needs.resolve-candidate.outputs.descriptor_sha256 }}"
                ),
            }:
                add("release-identity-env")
            run = identity.get("run", "")
            for token in (
                "set -euo pipefail",
                'if [[ ! "$CI_RUN_ID" =~ ^[1-9][0-9]*$',
                'if [[ ! "$BUNDLE_ARTIFACT_ID" =~ ^[1-9][0-9]*$',
                (
                    'if [[ ! "$BUNDLE_ARTIFACT_DIGEST" =~ '
                    + RELEASE_BUNDLE_DIGEST_PATTERN
                ),
                RELEASE_BUNDLE_DIGEST_ERROR,
                (
                    'if [[ ! "$EXPECTED_DESCRIPTOR_SHA" =~ '
                    + RELEASE_DESCRIPTOR_DIGEST_PATTERN
                ),
            ):
                if token not in run:
                    add("release-identity-run")
            if (
                'if [[ ! "$BUNDLE_ARTIFACT_DIGEST" =~ ^[0-9a-f]{64}$'
                in run
            ):
                add("release-identity-run")
        _validate_isolation(
            "upload",
            upload,
            add,
            step_name="Isolate release Go config and caches",
            base="payment-hub-release-cache",
            prefix="release-upload",
        )
        _validate_checkout(
            "upload", upload, add, ref=True, prefix="release-upload"
        )
        _validate_setup_go("upload", upload, add, prefix="release-upload")
        _validate_docker_steps(
            "upload",
            upload,
            add,
            context="payment-hub-release",
            prefix="release-docker",
        )
        prepare_dir = _find_step(upload, "Prepare handoff download directory")
        if prepare_dir is None:
            add("release-download-dir-missing")
        else:
            run = prepare_dir.get("run", "")
            for token in (
                "set -euo pipefail",
                "install -d -m 0700",
                "payment-hub-release-bundle",
            ):
                if token not in run:
                    add("release-download-dir-run")
        bundle_download = _find_step(upload, "Download release bundle")
        if bundle_download is None:
            add("release-bundle-download-missing")
        else:
            if bundle_download.get("uses") != DOWNLOAD:
                add("release-bundle-download-action")
            download_with = bundle_download.get("with", {})
            if set(download_with) != {
                "artifact-ids",
                "run-id",
                "github-token",
                "repository",
                "path",
                "merge-multiple",
                "digest-mismatch",
            }:
                add("release-bundle-download-with")
            if download_with.get("artifact-ids") != (
                "${{ needs.resolve-candidate.outputs.bundle_artifact_id }}"
            ):
                add("release-bundle-download-artifact")
            if download_with.get("run-id") != (
                "${{ needs.resolve-candidate.outputs.ci_run_id }}"
            ):
                add("release-bundle-download-run")
            if download_with.get("github-token") != "${{ github.token }}":
                add("release-bundle-download-token")
            if download_with.get("repository") != "${{ github.repository }}":
                add("release-bundle-download-repository")
            if download_with.get("path") != RELEASE_BUNDLE_DOWNLOAD_PATH:
                add("release-bundle-download-path")
            if download_with.get("merge-multiple") is not True:
                add("release-bundle-download-merge")
            if download_with.get("digest-mismatch") != "error":
                add("release-bundle-download-digest")
            if "name" in download_with or "latest" in str(download_with).lower():
                add("release-bundle-download-floating")
        restrict = _find_step(upload, "Restrict handoff bundle permissions")
        if restrict is None:
            add("release-chmod-missing")
        else:
            run = restrict.get("run", "")
            if "chmod 0600" not in run:
                add("release-chmod-run")
            for name in (
                "pair.tar",
                "descriptor.json",
                "build-record.json",
                "accepted-images.json",
            ):
                if name not in run:
                    add("release-chmod-run")
        verify_handoff = _find_step(upload, "Verify release handoff")
        if verify_handoff is None:
            add("release-verify-handoff-missing")
        else:
            if verify_handoff.get("shell") != "bash":
                add("release-verify-handoff-shell")
            if verify_handoff.get("env") != {
                "SOURCE_SHA": "${{ github.sha }}",
                "EXPECTED_DESCRIPTOR_SHA": (
                    "${{ needs.resolve-candidate.outputs.descriptor_sha256 }}"
                ),
            }:
                add("release-verify-handoff-env")
            if verify_handoff.get("run", "").strip() != (
                RELEASE_VERIFY_HANDOFF_RUN.strip()
            ):
                add("release-verify-handoff-run")
            run = verify_handoff.get("run", "")
            for token in (
                "scripts/release.sh --stage verify-handoff",
                '--source-sha "$SOURCE_SHA"',
                '--bundle-dir "$RUNNER_TEMP/payment-hub-release-bundle"',
                '--expected-descriptor-sha256 "$EXPECTED_DESCRIPTOR_SHA"',
            ):
                if token not in run:
                    add("release-verify-handoff-run")
            for token in ("--stage prepare", "build-images.py", "secrets."):
                if token in run:
                    add("release-verify-handoff-unsafe")
        ci_gate = _find_step(upload, "Require successful main CI")
        if ci_gate is None:
            add("release-ci-gate-missing")
        else:
            if ci_gate.get("shell") != "bash":
                add("release-ci-gate-shell")
            if "if" in ci_gate or "continue-on-error" in ci_gate:
                add("release-ci-gate-skip")
            if ci_gate.get("env") != {
                "GITHUB_TOKEN": "${{ github.token }}",
                "SOURCE_SHA": "${{ github.sha }}",
                "CI_RUN_ID": (
                    "${{ needs.resolve-candidate.outputs.ci_run_id }}"
                ),
                "CI_RUN_ATTEMPT": (
                    "${{ needs.resolve-candidate.outputs.ci_run_attempt }}"
                ),
            }:
                add("release-ci-gate-env")
            if ci_gate.get("run", "").strip() != RELEASE_CI_GATE_RUN.strip():
                add("release-ci-gate-run")
            run = ci_gate.get("run", "")
            for token in (
                "scripts/check-main-ci.py",
                '--source-sha "$SOURCE_SHA"',
                '--summary "$RUNNER_TEMP/payment-hub-ci-gate/ci-preflight.json"',
                '--expected-run-id "$CI_RUN_ID"',
                '--expected-run-attempt "$CI_RUN_ATTEMPT"',
            ):
                if token not in run:
                    add("release-ci-gate-run")
        publish = _find_step(upload, "Publish paired images")
        if publish is None:
            add("release-publish-missing")
        else:
            if set(publish) != {"name", "shell", "env", "run"}:
                add("release-publish-shape")
            if publish.get("shell") != "bash":
                add("release-publish-shell")
            if publish.get("env") != RELEASE_PUBLISH_ENV:
                add("release-publish-env")
            if publish.get("run", "").strip() != RELEASE_PUBLISH_RUN.strip():
                add("release-publish-run")
            run = publish.get("run", "")
            for token in (
                "set -euo pipefail",
                "scripts/release.sh --stage publish",
                '--source-sha "$SOURCE_SHA"',
                '--go "$(command -v go)"',
                '--cache-dir "$RUNNER_TEMP/payment-hub-release-cache"',
                '--artifact-root "$RUNNER_TEMP/payment-hub-release-publish"',
                '--bundle-dir "$RUNNER_TEMP/payment-hub-release-bundle"',
                '--expected-descriptor-sha256 "$EXPECTED_DESCRIPTOR_SHA"',
                "--approve-external",
                "--package-preconditions-verified",
                "--prepare-inputs",
            ):
                if token not in run:
                    add("release-publish-run")
            for token in (
                "--bootstrap-packages",
                "secrets.",
                "GHCR_PACKAGE_AUDIT_TOKEN",
                "set -x",
                "printenv",
                "docker push",
                "docker login",
                "docker build",
                "ghcr.io",
                "cosign",
                "attest",
                "deploy",
                "--stage prepare",
            ):
                if token in run:
                    add("release-publish-unsafe")
        evidence = _find_step(upload, RELEASE_EVIDENCE_STEP_NAME)
        if evidence is None:
            add("release-evidence-missing")
        else:
            if set(evidence) != {"name", "shell", "env", "run"}:
                add("release-evidence-shape")
            if evidence.get("shell") != "bash":
                add("release-evidence-shell")
            if "if" in evidence or "continue-on-error" in evidence:
                add("release-evidence-skip")
            if evidence.get("env") != RELEASE_EVIDENCE_ENV:
                add("release-evidence-env")
            if evidence.get("run", "").strip() != RELEASE_EVIDENCE_RUN.strip():
                add("release-evidence-run")
            run = evidence.get("run", "")
            for token in (
                "set -euo pipefail",
                "python3 -I -",
                RELEASE_EVIDENCE_SCHEMA,
                "xtianxx/payment-hub",
                "os.O_EXCL",
                "hashlib.sha256",
                "release-summary.json",
                "descriptor.json",
                "release-evidence.json",
                "RUNNER_TEMP",
            ):
                if token not in run:
                    add("release-evidence-run")
            for token in (
                "GITHUB_TOKEN",
                RELEASE_AUDIT_SECRET_NAME,
                "secrets.",
                "docker",
                "ghcr.io",
                "curl",
                "wget",
                "--stage publish",
                "--stage prepare",
                "set -x",
                "printenv",
            ):
                if token in run:
                    add("release-evidence-unsafe")
        summary = _find_step(upload, "Upload release summary")
        if summary is None:
            add("release-summary-missing")
        else:
            if summary.get("if") != "always()":
                add("release-summary-condition")
            if summary.get("uses") != UPLOAD:
                add("release-summary-action")
            summary_with = summary.get("with", {})
            if set(summary_with) != {
                "name",
                "path",
                "if-no-files-found",
                "retention-days",
                "include-hidden-files",
            }:
                add("release-summary-with")
            if summary_with.get("name") != RELEASE_SUMMARY_NAME:
                add("release-summary-name")
            summary_path = summary_with.get("path")
            if summary_path != RELEASE_SUMMARY_PATH:
                add("release-summary-path")
            if (
                not isinstance(summary_path, str)
                or tuple(summary_path.splitlines()) != RELEASE_SUMMARY_FILES
            ):
                add("release-summary-path")
            if summary_with.get("if-no-files-found") != "error":
                add("release-summary-missing-policy")
            if summary_with.get("retention-days") != 30:
                add("release-summary-retention")
            if summary_with.get("include-hidden-files") is not False:
                add("release-summary-hidden")
            if not isinstance(summary_path, str) or any(
                marker in summary_path for marker in ("*", "?", "[")
            ):
                add("release-summary-wildcard")

    if isinstance(initialize, dict):
        if initialize.get("name") != RELEASE_JOB_NAMES["initialize-migrate"]:
            add("release-init-name")
        if initialize.get("runs-on") != "ubuntu-24.04":
            add("release-init-runs-on")
        if initialize.get("timeout-minutes") != RELEASE_TIMEOUT:
            add("release-init-timeout")
        if initialize.get("needs") != "resolve-candidate":
            add("release-init-needs")
        if initialize.get("if") != RELEASE_INITIALIZE_GUARD:
            add("release-init-guard")
        if initialize.get("permissions") != {
            "contents": "read",
            "actions": "read",
            "packages": "write",
        }:
            add("release-init-permissions")
        if "env" in initialize:
            add("release-init-env")
        if tuple(_step_names(initialize)) != RELEASE_INIT_STEP_NAMES:
            add("release-init-steps")
        _validate_step_if_policy(
            "initialize-migrate",
            initialize,
            add,
            prefix="release-init",
            allowed_always=RELEASE_ALWAYS_IF_STEPS,
        )
        identity = _find_step(
            initialize, "Verify resolved candidate identity"
        )
        if identity is None:
            add("release-init-identity-missing")
        else:
            if identity.get("shell") != "bash":
                add("release-init-identity-shell")
            if "if" in identity or "continue-on-error" in identity:
                add("release-init-identity-skip")
            if identity.get("env") != {
                "CI_RUN_ID": "${{ needs.resolve-candidate.outputs.ci_run_id }}",
                "CI_RUN_ATTEMPT": (
                    "${{ needs.resolve-candidate.outputs.ci_run_attempt }}"
                ),
                "BUNDLE_ARTIFACT_ID": (
                    "${{ needs.resolve-candidate.outputs.bundle_artifact_id }}"
                ),
                "BUNDLE_ARTIFACT_DIGEST": (
                    "${{ needs.resolve-candidate.outputs.bundle_artifact_digest }}"
                ),
                "EXPECTED_DESCRIPTOR_SHA": (
                    "${{ needs.resolve-candidate.outputs.descriptor_sha256 }}"
                ),
            }:
                add("release-init-identity-env")
            run = identity.get("run", "")
            for token in (
                "set -euo pipefail",
                'if [[ ! "$CI_RUN_ID" =~ ^[1-9][0-9]*$',
                'if [[ ! "$BUNDLE_ARTIFACT_ID" =~ ^[1-9][0-9]*$',
                (
                    'if [[ ! "$BUNDLE_ARTIFACT_DIGEST" =~ '
                    + RELEASE_BUNDLE_DIGEST_PATTERN
                ),
                RELEASE_BUNDLE_DIGEST_ERROR,
                (
                    'if [[ ! "$EXPECTED_DESCRIPTOR_SHA" =~ '
                    + RELEASE_DESCRIPTOR_DIGEST_PATTERN
                ),
            ):
                if token not in run:
                    add("release-init-identity-run")
            if (
                'if [[ ! "$BUNDLE_ARTIFACT_DIGEST" =~ ^[0-9a-f]{64}$'
                in run
            ):
                add("release-init-identity-run")
        _validate_isolation(
            "initialize-migrate",
            initialize,
            add,
            step_name="Isolate initialization Go config and caches",
            base=RELEASE_INIT_ISOLATION_BASE,
            prefix="release-init",
        )
        _validate_checkout(
            "initialize-migrate",
            initialize,
            add,
            ref=True,
            prefix="release-init",
        )
        _validate_setup_go(
            "initialize-migrate", initialize, add, prefix="release-init"
        )
        _validate_docker_steps(
            "initialize-migrate",
            initialize,
            add,
            context=RELEASE_INIT_DOCKER_CONTEXT,
            prefix="release-init-docker",
        )
        prepare_dir = _find_step(
            initialize, "Prepare initialization download directory"
        )
        if prepare_dir is None:
            add("release-init-download-dir-missing")
        else:
            run = prepare_dir.get("run", "")
            for token in (
                "set -euo pipefail",
                "install -d -m 0700",
                "payment-hub-initialization-bundle",
            ):
                if token not in run:
                    add("release-init-download-dir-run")
        bundle_download = _find_step(
            initialize, "Download initialization bundle"
        )
        if bundle_download is None:
            add("release-init-bundle-missing")
        else:
            if bundle_download.get("uses") != DOWNLOAD:
                add("release-init-bundle-action")
            download_with = bundle_download.get("with", {})
            if set(download_with) != {
                "artifact-ids",
                "run-id",
                "github-token",
                "repository",
                "path",
                "merge-multiple",
                "digest-mismatch",
            }:
                add("release-init-bundle-with")
            if download_with.get("artifact-ids") != (
                "${{ needs.resolve-candidate.outputs.bundle_artifact_id }}"
            ):
                add("release-init-bundle-artifact")
            if download_with.get("run-id") != (
                "${{ needs.resolve-candidate.outputs.ci_run_id }}"
            ):
                add("release-init-bundle-run")
            if download_with.get("github-token") != "${{ github.token }}":
                add("release-init-bundle-token")
            if download_with.get("repository") != "${{ github.repository }}":
                add("release-init-bundle-repository")
            if download_with.get("path") != RELEASE_INIT_DOWNLOAD_PATH:
                add("release-init-bundle-path")
            if download_with.get("merge-multiple") is not True:
                add("release-init-bundle-merge")
            if download_with.get("digest-mismatch") != "error":
                add("release-init-bundle-digest")
            if "name" in download_with or "latest" in str(
                download_with
            ).lower():
                add("release-init-bundle-floating")
        restrict = _find_step(
            initialize, "Restrict initialization bundle permissions"
        )
        if restrict is None:
            add("release-init-chmod-missing")
        else:
            run = restrict.get("run", "")
            if "chmod 0600" not in run:
                add("release-init-chmod-run")
            for name in (
                "pair.tar",
                "descriptor.json",
                "build-record.json",
                "accepted-images.json",
            ):
                if name not in run:
                    add("release-init-chmod-run")
            if "payment-hub-initialization-bundle" not in run:
                add("release-init-chmod-run")
        verify_handoff = _find_step(
            initialize, "Verify initialization handoff"
        )
        if verify_handoff is None:
            add("release-init-verify-handoff-missing")
        else:
            if verify_handoff.get("shell") != "bash":
                add("release-init-verify-handoff-shell")
            if verify_handoff.get("env") != {
                "SOURCE_SHA": "${{ github.sha }}",
                "EXPECTED_DESCRIPTOR_SHA": (
                    "${{ needs.resolve-candidate.outputs.descriptor_sha256 }}"
                ),
            }:
                add("release-init-verify-handoff-env")
            if verify_handoff.get("run", "").strip() != (
                RELEASE_INIT_VERIFY_HANDOFF_RUN.strip()
            ):
                add("release-init-verify-handoff-run")
            run = verify_handoff.get("run", "")
            for token in ("--stage prepare", "build-images.py", "secrets."):
                if token in run:
                    add("release-init-verify-handoff-unsafe")
        ci_gate = _find_step(initialize, "Require successful main CI")
        if ci_gate is None:
            add("release-init-ci-gate-missing")
        else:
            if ci_gate.get("shell") != "bash":
                add("release-init-ci-gate-shell")
            if "if" in ci_gate or "continue-on-error" in ci_gate:
                add("release-init-ci-gate-skip")
            if ci_gate.get("env") != {
                "GITHUB_TOKEN": "${{ github.token }}",
                "SOURCE_SHA": "${{ github.sha }}",
                "CI_RUN_ID": (
                    "${{ needs.resolve-candidate.outputs.ci_run_id }}"
                ),
                "CI_RUN_ATTEMPT": (
                    "${{ needs.resolve-candidate.outputs.ci_run_attempt }}"
                ),
            }:
                add("release-init-ci-gate-env")
            if ci_gate.get("run", "").strip() != RELEASE_CI_GATE_RUN.strip():
                add("release-init-ci-gate-run")
            run = ci_gate.get("run", "")
            for token in (
                "scripts/check-main-ci.py",
                '--source-sha "$SOURCE_SHA"',
                '--summary "$RUNNER_TEMP/payment-hub-ci-gate/ci-preflight.json"',
                '--expected-run-id "$CI_RUN_ID"',
                '--expected-run-attempt "$CI_RUN_ATTEMPT"',
            ):
                if token not in run:
                    add("release-init-ci-gate-run")
        initializer = _find_step(initialize, "Initialize migration package")
        if initializer is None:
            add("release-init-command-missing")
        else:
            if set(initializer) != {"name", "shell", "env", "run"}:
                add("release-init-command-shape")
            if initializer.get("shell") != "bash":
                add("release-init-command-shell")
            if initializer.get("env") != RELEASE_INIT_ENV:
                add("release-init-command-env")
            if initializer.get("run", "").strip() != RELEASE_INIT_RUN:
                add("release-init-command-run")
            run = initializer.get("run", "")
            for token in (
                "scripts/init-migrate-package.py",
                '--source-sha "$SOURCE_SHA"',
                '--ci-run-id "$CI_RUN_ID"',
                '--ci-run-attempt "$CI_RUN_ATTEMPT"',
                '--bundle-artifact-id "$BUNDLE_ARTIFACT_ID"',
                '--bundle-artifact-digest "$BUNDLE_ARTIFACT_DIGEST"',
                '--bundle-dir "$RUNNER_TEMP/payment-hub-initialization-bundle"',
                '--expected-descriptor-sha256 "$EXPECTED_DESCRIPTOR_SHA"',
                '--artifact-root "$RUNNER_TEMP/payment-hub-migrate-initialization"',
                '--go "$(command -v go)"',
                '--cache-dir "$RUNNER_TEMP/payment-hub-initialization"',
                "--prepare-inputs",
                "--approve-initialization",
                "--acknowledge-public-creation",
                "--initialization-preconditions-verified",
            ):
                if token not in run:
                    add("release-init-command-run")
            for token in (
                "scripts/release.sh",
                "--stage publish",
                "--stage prepare",
                "build-images.py",
                "--bootstrap-packages",
                "--target",
                "--tag",
                "--role",
                "set -x",
                "printenv",
                "docker push",
                "docker login",
                "docker build",
                "ghcr.io",
                "cosign",
                "attest",
                "deploy",
            ):
                if token in run:
                    add("release-init-command-unsafe")
        summary = _find_step(
            initialize, "Upload migration initialization summary"
        )
        if summary is None:
            add("release-init-summary-missing")
        else:
            if summary.get("if") != "always()":
                add("release-init-summary-condition")
            if summary.get("uses") != UPLOAD:
                add("release-init-summary-action")
            summary_with = summary.get("with", {})
            if set(summary_with) != {
                "name",
                "path",
                "if-no-files-found",
                "retention-days",
                "include-hidden-files",
            }:
                add("release-init-summary-with")
            if summary_with.get("name") != RELEASE_INIT_SUMMARY_NAME:
                add("release-init-summary-name")
            if summary_with.get("path") != RELEASE_INIT_SUMMARY_PATH:
                add("release-init-summary-path")
            if summary_with.get("if-no-files-found") != "error":
                add("release-init-summary-missing-policy")
            if summary_with.get("retention-days") != 30:
                add("release-init-summary-retention")
            if summary_with.get("include-hidden-files") is not False:
                add("release-init-summary-hidden")
            if "*" in str(summary_with.get("path", "")):
                add("release-init-summary-wildcard")
        init_text = json.dumps(initialize)
        for token in (
            "--stage publish",
            "--stage prepare",
            "build-images.py",
            "--bootstrap-packages",
            "--target",
            "--tag",
            "--role",
        ):
            if token in init_text:
                add("release-init-forbidden-token")

    _validate_common_forbidden_tokens(text, add, allow_secrets=True)
    for token in ("ci-candidate", "build-images.py", "check-image-handoff.py"):
        if token in text:
            add("release-forbidden-token")

    allowed_token_steps = {
        ("resolve-candidate", "Select release candidate"),
        ("upload", "Require successful main CI"),
        ("upload", "Publish paired images"),
        ("initialize-migrate", "Require successful main CI"),
        ("initialize-migrate", "Initialize migration package"),
    }
    allowed_audit_steps = {
        ("upload", "Publish paired images"),
        ("initialize-migrate", "Initialize migration package"),
    }
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if "env" in job:
            add("release-job-env", job_id)
        job_env = job.get("env", {})
        if "GITHUB_TOKEN" in job_env:
            add("release-token-location", job_id)
        if RELEASE_AUDIT_SECRET_NAME in job_env:
            add("release-token-location", job_id)
        for step in job.get("steps", []):
            if "continue-on-error" in step:
                add("release-continue-on-error", job_id)
            step_env = step.get("env", {})
            location = (job_id, step.get("name"))
            if "GITHUB_TOKEN" in step_env:
                if location not in allowed_token_steps:
                    add("release-token-location", job_id)
                if step_env["GITHUB_TOKEN"] != "${{ github.token }}":
                    add("release-token-location", job_id)
            if RELEASE_AUDIT_SECRET_NAME in step_env:
                if location not in allowed_audit_steps:
                    add("release-token-location", job_id)
                if (
                    step_env[RELEASE_AUDIT_SECRET_NAME]
                    != RELEASE_AUDIT_SECRET_EXPRESSION
                ):
                    add("release-token-location", job_id)
            for key, value in step_env.items():
                if key != RELEASE_AUDIT_SECRET_NAME and (
                    RELEASE_AUDIT_SECRET_EXPRESSION in str(value)
                ):
                    add("release-token-location", job_id)
                if key not in (
                    "GITHUB_TOKEN",
                    RELEASE_AUDIT_SECRET_NAME,
                ) and "secrets." in str(value):
                    add("release-token-location", job_id)
            for field in ("run", "with"):
                field_value = step.get(field)
                if field_value is None:
                    continue
                blob = (
                    field_value
                    if isinstance(field_value, str)
                    else json.dumps(field_value)
                )
                if "secrets." in blob or RELEASE_AUDIT_SECRET_NAME in blob:
                    add("release-token-location", job_id)
    if "GITHUB_TOKEN" in workflow.get("env", {}):
        add("release-token-location")
    if RELEASE_AUDIT_SECRET_NAME in workflow.get("env", {}):
        add("release-token-location")

    try:
        expressions = iter_github_expressions(text)
    except ContractError:
        add("release-secret-policy")
    else:
        allowed_expression_count = 0
        for expression in expressions:
            if SECRETS_TOKEN_RE.search(expression) is None:
                continue
            if "${{" + expression + "}}" == RELEASE_AUDIT_SECRET_EXPRESSION:
                allowed_expression_count += 1
            else:
                add("release-secret-policy")
        if allowed_expression_count != 2:
            add("release-secret-policy")
    if text.count(RELEASE_AUDIT_SECRET_NAME + ":") != 2:
        add("release-token-location")
    if "--stage prepare" in text:
        add("release-forbidden-token")
    return issues

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
    issues.extend(check_workflow_policy(repo))
    for relative, validator in (
        (CI_RELATIVE, validate_ci_workflow),
        (NIGHTLY_RELATIVE, validate_nightly_workflow),
        (RELEASE_RELATIVE, validate_release_workflow),
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
