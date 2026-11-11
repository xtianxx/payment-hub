#!/usr/bin/env python3
"""One-shot T017 database foundation launcher.

This script is the narrow T017 database foundation entry point: it prepares
the fixed-input migration image, starts one isolated PostgreSQL Compose
project, runs the real Goose migrations twice, runs the integration vet/test
commands, and cleans up only the resources it created. Its integration roots
cover the trusted private UDS observer and helper lifecycle, the current real
application binary's readyz/SIGTERM, bad-config, missing-migration,
startup-timeout, startup-cancel, and startup-listen diagnostics, and the
first-batch storage/mock core (order registration, observation atomic save,
and mock transaction) paths, the next-batch order-lookup path, the US1
real payment HTTP/byte-boundary/restart integration lane, and the US2
same-intent replay/conflict/concurrency/restart idempotency lane. It is not
the T072 full check and does not claim payment HTTP beyond US1/US2,
application image, full MVP, T077, or CD.

Only Python's standard library is used. No tool is installed automatically.
The script never prints raw Docker/build/Goose/Go output; all tool output is
captured in memory and only fixed summaries or whitelisted report fields are
written to stdout/artifacts.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_CHECKS_PATH = Path(__file__).resolve().parent / "ci_checks.py"
_CI_CHECKS_SPEC = importlib.util.spec_from_file_location(
    "payment_hub_ci_checks", CI_CHECKS_PATH
)
ci_checks = importlib.util.module_from_spec(_CI_CHECKS_SPEC)
_CI_CHECKS_SPEC.loader.exec_module(ci_checks)

EXPECTED_GO_VERSION = "go1.27.3"
EXPECTED_GOOS = "linux"
EXPECTED_GOARCH = "amd64"
EXPECTED_GOTOOLCHAIN = "local"
EXPECTED_GOTELEMETRY = "off"
EXPECTED_PLATFORM = "linux/amd64"
EXPECTED_PG_VERSION = "16.15"
EXPECTED_GOOSE_VERSION = "goose version: v3.29.0"

EXPECTED_MODULE = "payment-hub"
EXPECTED_GO_DIRECTIVE = "1.27.3"
APPROVED_DIRECT = {
    "github.com/gin-gonic/gin": "v1.12.0",
    "github.com/jackc/pgx/v5": "v5.11.0",
}

POSTGRES_IMAGE = (
    "docker.io/library/postgres:16.15-bookworm"
    "@sha256:5983db9a9c6ad225a48a46cfe2e03e6680b1826662befd0465eb05b34cb1c64e"
)
GO_BUILDER_IMAGE = (
    "docker.io/library/golang:1.27.3-bookworm"
    "@sha256:025eefc1e8dbe63201734a72aaa0a55d7a077f2d8410432f8ae2f3e939f32871"
)
MIGRATION_RUNTIME_IMAGE = (
    "docker.io/library/debian:bookworm-slim"
    "@sha256:c67e92c8099ba22853ab4cfda7cfb494d253d6012ca82dee98ba8976772af018"
)
BASE_IMAGES = {
    "postgres": POSTGRES_IMAGE,
    "go_builder": GO_BUILDER_IMAGE,
    "migration_runtime": MIGRATION_RUNTIME_IMAGE,
}

MIGRATION_IMAGE_REPO = "payment-hub-migrate"
MIGRATION_TAG_PREFIX = MIGRATION_IMAGE_REPO + ":foundation-"
MIGRATION_SQL_NAMES = (
    "00001_create_payment_orders.sql",
    "00002_create_mock_transactions.sql",
    "00003_create_result_handling_records.sql",
)
IMAGE_KEY_FILES = (
    "deploy/Dockerfile.migrate",
    "deploy/goose-entrypoint.sh",
    "migrations/00001_create_payment_orders.sql",
    "migrations/00002_create_mock_transactions.sql",
    "migrations/00003_create_result_handling_records.sql",
)
PACKAGES = ("internal/postgres", "internal/mock", "tests/harness", "tests")
GO_TEST_PACKAGE_TIMEOUT_SECONDS = 120
REQUIRED_INTEGRATION_TESTS = (
    "TestCommitHookIntegrationHidesReplyAfterRealCommit",
    "TestHarnessProcessPreservesParentStateIntegration",
    "TestApplicationLifecycleIntegration",
)
REQUIRED_TEST_PACKAGES = {
    name: f"{EXPECTED_MODULE}/tests/harness" for name in REQUIRED_INTEGRATION_TESTS
}
REQUIRED_TEST_PACKAGES[
    "TestApplicationLifecycleIntegration/startup_diagnostics"
] = f"{EXPECTED_MODULE}/tests/harness"
# First-batch storage/mock core roots and the next-batch order-lookup root are
# bound to their own packages; they must not inherit the harness root above.
REQUIRED_TEST_PACKAGES[
    "TestOrderRegistrationIntegration"
] = f"{EXPECTED_MODULE}/internal/postgres"
REQUIRED_TEST_PACKAGES[
    "TestObservationAtomicSaveIntegration"
] = f"{EXPECTED_MODULE}/internal/postgres"
REQUIRED_TEST_PACKAGES[
    "TestMockTransactionIntegration"
] = f"{EXPECTED_MODULE}/internal/mock"
REQUIRED_TEST_PACKAGES[
    "TestOrderLookupIntegration"
] = f"{EXPECTED_MODULE}/internal/postgres"
REQUIRED_TEST_PACKAGES[
    "TestUS1Payment"
] = f"{EXPECTED_MODULE}/tests"
REQUIRED_TEST_PACKAGES[
    "TestUS1Restart"
] = f"{EXPECTED_MODULE}/tests"
REQUIRED_TEST_PACKAGES[
    "TestUS1HTTPBoundary"
] = f"{EXPECTED_MODULE}/tests"
REQUIRED_TEST_PACKAGES[
    "TestOrderRegistrationConcurrencyIntegration"
] = f"{EXPECTED_MODULE}/internal/postgres"
REQUIRED_TEST_PACKAGES[
    "TestMockIdempotencyIntegration"
] = f"{EXPECTED_MODULE}/internal/mock"
REQUIRED_TEST_PACKAGES[
    "TestUS2Replay"
] = f"{EXPECTED_MODULE}/tests"
REQUIRED_TEST_PACKAGES[
    "TestUS2Conflict"
] = f"{EXPECTED_MODULE}/tests"
REQUIRED_TEST_PACKAGES[
    "TestUS2Concurrency"
] = f"{EXPECTED_MODULE}/tests"
REQUIRED_TEST_PACKAGES[
    "TestUS2Restart"
] = f"{EXPECTED_MODULE}/tests"
REQUIRED_TEST_PACKAGES[
    "TestUS2ReportSafety"
] = f"{EXPECTED_MODULE}/tests"
REQUIRED_TEST_PACKAGES[
    "TestObservationFaultIntegration"
] = f"{EXPECTED_MODULE}/internal/postgres"
REQUIRED_TEST_PACKAGES[
    "TestUS3Query"
] = f"{EXPECTED_MODULE}/tests"
REQUIRED_TEST_PACKAGES[
    "TestUS3Protection"
] = f"{EXPECTED_MODULE}/tests"
REQUIRED_TEST_PACKAGES[
    "TestUS3Restart"
] = f"{EXPECTED_MODULE}/tests"
REQUIRED_TEST_PACKAGES[
    "TestUS3Lifecycle"
] = f"{EXPECTED_MODULE}/tests"
REQUIRED_TEST_PACKAGES[
    "TestUS3ReportSafety"
] = f"{EXPECTED_MODULE}/tests"
EXPECTED_PACKAGE_PATHS = tuple(
    f"{EXPECTED_MODULE}/{name}" for name in PACKAGES
)

MOCK_DB_USER = "payment_hub_test_user"
MOCK_DB_PASSWORD = "payment-hub-test-db-mock-password-not-real"
MOCK_CANARY = "db-foundation-canary-not-real"

REJECTION_CASES = (
    ("env", ("env",)),
    ("down", ("down",)),
    ("reset", ("reset",)),
    ("up-extra", ("up", "--extra")),
    ("option", ("--extra",)),
)

FIXTURE_COUNT_QUERY = "\n".join(
    [
        "SELECT count(*) FROM payment_orders;",
        "SELECT count(*) FROM mock_transactions;",
        "SELECT count(*) FROM result_handling_records;",
    ]
)

SOURCE_MANIFEST_EXPLICIT = (
    "go.mod",
    "go.sum",
    "scripts/check-setup.py",
    "scripts/check-db-foundation.py",
    "scripts/test_check_db_foundation.py",
    "scripts/ci_checks.py",
    "scripts/test_ci_checks.py",
    "scripts/check-vulnerabilities.py",
    "scripts/test_check_vulnerabilities.py",
    "scripts/check-ci-cancellation.py",
    "scripts/test_check_ci_cancellation.py",
    "deploy/compose.yaml",
    "deploy/compose.test.yaml",
    "deploy/Dockerfile.migrate",
    "deploy/goose-entrypoint.sh",
    "migrations/00001_create_payment_orders.sql",
    "migrations/00002_create_mock_transactions.sql",
    "migrations/00003_create_result_handling_records.sql",
)
SOURCE_MANIFEST_GLOBS = (
    "internal/**/*.go",
    "tests/**/*.go",
    "cmd/**/*.go",
    "scripts/*.py",
    "migrations/*.sql",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
)

DOCKER_LOG_OPTIONS = (
    "--log-driver",
    "local",
    "--log-opt",
    "max-size=10m",
    "--log-opt",
    "max-file=3",
)

BUDGETS = {
    "go_telemetry_seconds": 30,
    "go_env_seconds": 30,
    "go_mod_download_seconds": 240,
    "go_list_seconds": 120,
    "go_mod_verify_seconds": 120,
    "docker_version_seconds": 30,
    "compose_config_seconds": 60,
    "image_pull_seconds": 300,
    "image_build_seconds": 600,
    "image_inspect_seconds": 30,
    "tool_version_seconds": 30,
    "tool_sql_hash_seconds": 30,
    "tool_create_seconds": 30,
    "tool_start_max_seconds": 30,
    "negative_dsn_seconds": 60,
    "pg_health_seconds": 30,
    "pg_version_seconds": 15,
    "fixture_count_seconds": 30,
    "compose_up_seconds": 120,
    "migration_up_seconds": 60,
    "migration_up_again_seconds": 60,
    "go_vet_seconds": 120,
    "go_test_external_seconds": ci_checks.GO_TEST_BATCH_TIMEOUT_SECONDS,
    "compose_down_seconds": 60,
    "container_stop_seconds": 15,
    "container_remove_seconds": 15,
    "volume_remove_seconds": 30,
}


class LauncherError(RuntimeError):
    """A controlled launcher failure with a safe stage/kind classification."""

    def __init__(self, stage: str, kind: str, message: str) -> None:
        self.stage = stage
        self.kind = kind
        self.message = message
        super().__init__(message)


class LauncherInterrupted(RuntimeError):
    """A controlled SIGINT/SIGTERM interruption."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__("controlled interruption")


def require(condition: bool, stage: str, kind: str, message: str) -> None:
    if not condition:
        raise LauncherError(stage, kind, message)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def make_dsn(host: str, port: int, database: str, user: str, password: str) -> str:
    """Build a runtime-only DSN; never log or persist the returned value."""
    return (
        "postgresql://"
        + quote(user, safe="")
        + ":"
        + quote(password, safe="")
        + "@"
        + host
        + ":"
        + str(port)
        + "/"
        + quote(database, safe="")
        + "?sslmode=disable&timezone=UTC"
    )


def compute_image_key(repo: Path) -> tuple[str, dict[str, str]]:
    digest = hashlib.sha256()
    sql_hashes: dict[str, str] = {}
    for relative in IMAGE_KEY_FILES:
        path = repo / relative
        if not path.is_file():
            raise LauncherError("image", "input-missing", f"missing image input {relative}")
        data = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(data)
        digest.update(b"\x00")
        if relative.startswith("migrations/"):
            sql_hashes[path.name] = hashlib.sha256(data).hexdigest()
    return digest.hexdigest(), sql_hashes


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


class Launcher:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_id = uuid.uuid4().hex
        self.project = f"payment-hub-test-db-{self.run_id}"
        self.database = f"payment_hub_test_{self.run_id}"
        self.repo = REPO_ROOT
        self.scope = (
            "database foundation plus wired runtime harness: fixed-input migration "
            "image, isolated real PostgreSQL Compose project, Goose up/up-again, "
            "integration vet/test with required commit-hook experiment, trusted "
            "private-UDS observer/helper two-generation parent retention, and "
            "current real application-binary readyz/SIGTERM, bad-config, "
            "missing-migration, startup-timeout, startup-cancel, and "
            "startup-listen diagnostics; default packages cover private-UDS/"
            "readyz/lifecycle short real-connection semantics, explicit empty-Env "
            "child isolation, post-leader-exit cleanup deadlines, bounded startup "
            "diagnostic logs, safe release after failed Seal, immutable final "
            "snapshot archiving, helper self-tests, first-batch storage/mock "
            "core paths, order lookup, US1 real payment HTTP/byte-boundary/"
            "restart integration, and US2 same-intent replay/conflict/"
            "concurrency/restart idempotency integration; owned cleanup only; "
            "not payment HTTP beyond US1/US2, application image, "
            "full MVP, T072, T077, or CD"
        )
        self.started_at = utc_now()

        self.go = self._resolve_go(args.go)
        self.cache = self._prepare_cache(args.cache_dir)
        self.go_env = self._go_environment()
        self.offline_go_env = self._offline_go_environment()
        self.artifact_root = self._resolve_artifact_root(args.artifact_root)
        self.run_dir = self.artifact_root / self.run_id
        try:
            self.run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise LauncherError(
                "artifact", "exists", "artifact run directory already exists"
            ) from exc
        except OSError as exc:
            raise LauncherError(
                "artifact", "create", "could not create artifact run directory"
            ) from exc
        self.report_path = self.run_dir / "db-foundation.json"

        self.docker: str | None = None
        self.docker_env: dict[str, str] = {}
        self.compose_env: dict[str, str] = {}
        self.server_os: str | None = None
        self.server_arch: str | None = None

        self.image_key: str | None = None
        self.migration_tag: str | None = None
        self.migration_image_id: str | None = None
        self.sql_hashes: dict[str, str] = {}
        self.source: dict[str, Any] = {}
        self.source_manifest_digest: str | None = None
        self.images: dict[str, Any] = {"platform": EXPECTED_PLATFORM, "base": {}}
        self.versions: dict[str, str] = {}
        self.module: dict[str, Any] = {}
        self.preflight: dict[str, Any] = {}
        self.pg: dict[str, Any] = {}
        self.migration: dict[str, Any] = {}
        self.negative: dict[str, Any] = {}
        self.safety_entry: dict[str, Any] = {}
        self.fixtures: dict[str, Any] = {}
        self.tests: dict[str, Any] = {
            "required_test_results": {
                name: "missing" for name in REQUIRED_TEST_PACKAGES
            },
        }
        self.cleanup: dict[str, Any] = {}

        self.container_id: str | None = None
        self.volume_name = f"{self.project}_postgres_data"
        self.network_name = f"{self.project}_default"
        self.published_port: int | None = None
        self.compose_attempted = False
        self.project_created = False
        self.volume_created = False
        self.network_created = False
        self.compose_created: dict[str, Any] = {
            "containers": [],
            "network": None,
            "volume": None,
        }

        self.commands: list[dict[str, Any]] = []
        self.tool_containers: list[dict[str, Any]] = []
        self.cleanup_failures: list[str] = []
        self.failure: dict[str, str] | None = None
        self.required_complete = False
        self.artifact_written = False
        self.report_data: dict[str, Any] | None = None
        self.running = False
        self.cleanup_started = False
        self.repeat_signal = False

    # ------------------------------------------------------------------
    # CLI/path validation and isolated environments
    # ------------------------------------------------------------------

    def _resolve_go(self, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise LauncherError("cli", "go", "--go must be an absolute path")
        if not path.is_file():
            raise LauncherError("cli", "go", "--go does not point to a file")
        if not os.access(path, os.X_OK):
            raise LauncherError("cli", "go", "--go is not executable")
        return str(path.resolve())

    def _prepare_cache(self, value: str) -> Path:
        if not value:
            raise LauncherError("cli", "cache", "--cache-dir must not be empty")
        cache = Path(value).expanduser()
        if not cache.is_absolute():
            raise LauncherError("cli", "cache", "--cache-dir must be absolute")
        cache = cache.resolve()
        repo = self.repo.resolve()
        if cache == repo or repo in cache.parents:
            raise LauncherError(
                "cli", "cache", "--cache-dir must be outside the repository"
            )
        if cache.exists() and not cache.is_dir():
            raise LauncherError("cli", "cache", "--cache-dir is not a directory")
        try:
            cache.mkdir(parents=True, exist_ok=True)
            for name in ("config", "gomod", "gobuild", "gopath"):
                (cache / name).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LauncherError(
                "cli", "cache", "could not create cache directories"
            ) from exc
        return cache

    def _resolve_artifact_root(self, value: str) -> Path:
        if not value:
            raise LauncherError("cli", "artifact-root", "--artifact-root is empty")
        root = Path(value).expanduser()
        if not root.is_absolute():
            root = (self.repo / root).resolve()
        else:
            root = root.resolve()
        if root.exists() and not root.is_dir():
            raise LauncherError(
                "artifact", "root", "--artifact-root is not a directory"
            )
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LauncherError(
                "artifact", "root", "could not create artifact root"
            ) from exc
        return root

    def _go_environment(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for name in ("HOME", "PATH", "TMPDIR", "LANG"):
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.cache / "config"),
                "GOPATH": str(self.cache / "gopath"),
                "GOMODCACHE": str(self.cache / "gomod"),
                "GOCACHE": str(self.cache / "gobuild"),
                "GOENV": "off",
                "GOTOOLCHAIN": "local",
                "GOWORK": "off",
                "GOFLAGS": "",
                "GOPRIVATE": "",
                "GONOPROXY": "",
                "GONOSUMDB": "",
                "CGO_ENABLED": "1",
                "GOMAXPROCS": "2",
            }
        )
        if self.args.download_modules:
            env["GOPROXY"] = "https://proxy.golang.org"
            env["GOSUMDB"] = "sum.golang.org"
        else:
            env["GOPROXY"] = "off"
            env["GOSUMDB"] = "off"
        return env

    def _offline_go_environment(self) -> dict[str, str]:
        env = dict(self.go_env)
        env["GOPROXY"] = "off"
        env["GOSUMDB"] = "off"
        return env

    def _docker_environment(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for name in (
            "HOME",
            "PATH",
            "TMPDIR",
            "LANG",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "XDG_RUNTIME_DIR",
        ):
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        return env

    # ------------------------------------------------------------------
    # Bounded command execution and safe recording
    # ------------------------------------------------------------------

    def _record_command(
        self,
        name: str,
        argv: Sequence[str],
        exit_code: int,
        allow_failure: bool,
        timeout: float,
        elapsed: float,
        *,
        timed_out: bool = False,
    ) -> None:
        secret_markers = (
            "postgresql://",
            "postgres://",
            MOCK_DB_PASSWORD,
            MOCK_CANARY,
            "GOOSE_DBSTRING=",
            "PGPASSWORD=",
            "PAYMENT_HUB_TEST_DATABASE_URL=",
        )
        for item in argv:
            text = str(item)
            for marker in secret_markers:
                if marker and marker in text:
                    raise LauncherError(
                        "safety",
                        "argv-secret",
                        "a connection value would enter the command record",
                    )
        self.commands.append(
            {
                "name": name,
                "argv": [str(item) for item in argv],
                "exit_code": exit_code,
                "allow_failure": allow_failure,
                "timeout_seconds": round(timeout, 3),
                "duration_seconds": round(elapsed, 3),
                "timed_out": timed_out,
            }
        )

    def _run(
        self,
        stage: str,
        kind: str,
        name: str,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: float,
        *,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        start = time.monotonic()
        try:
            result = subprocess.run(
                list(argv),
                cwd=str(self.repo),
                env=dict(env),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise LauncherError(stage, kind, "required tool not found") from exc
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - start
            self._record_command(
                name,
                argv,
                124,
                allow_failure,
                timeout,
                elapsed,
                timed_out=True,
            )
            raise LauncherError(
                stage, "timeout", f"timed out after {timeout}s"
            ) from exc
        except OSError as exc:
            raise LauncherError(stage, kind, "could not execute tool") from exc

        elapsed = time.monotonic() - start
        self._record_command(name, argv, result.returncode, allow_failure, timeout, elapsed)
        if result.returncode != 0 and not allow_failure:
            raise LauncherError(
                stage, kind, f"command failed with exit {result.returncode}"
            )
        return result

    def _run_with_stdin(
        self,
        stage: str,
        kind: str,
        name: str,
        argv: Sequence[str],
        env: Mapping[str, str],
        stdin_text: str,
        timeout: float,
        *,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        start = time.monotonic()
        try:
            result = subprocess.run(
                list(argv),
                cwd=str(self.repo),
                env=dict(env),
                input=stdin_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise LauncherError(stage, kind, "required tool not found") from exc
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - start
            self._record_command(
                name,
                argv,
                124,
                allow_failure,
                timeout,
                elapsed,
                timed_out=True,
            )
            raise LauncherError(
                stage, "timeout", f"timed out after {timeout}s"
            ) from exc
        except OSError as exc:
            raise LauncherError(stage, kind, "could not execute tool") from exc

        elapsed = time.monotonic() - start
        self._record_command(name, argv, result.returncode, allow_failure, timeout, elapsed)
        if result.returncode != 0 and not allow_failure:
            raise LauncherError(
                stage, kind, f"command failed with exit {result.returncode}"
            )
        return result

    def _run_process_group(
        self,
        stage: str,
        kind: str,
        name: str,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: float,
        *,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        start = time.monotonic()
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=str(self.repo),
                env=dict(env),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise LauncherError(stage, kind, "required tool not found") from exc
        except OSError as exc:
            raise LauncherError(stage, kind, "could not execute tool") from exc

        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - start
            self._record_command(
                name,
                argv,
                124,
                allow_failure,
                timeout,
                elapsed,
                timed_out=True,
            )
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.cleanup_failures.append(f"{stage}:reap")
                stdout, stderr = "", ""
            if name == "go test integration":
                parsed = self._parse_go_test_results(
                    stdout or "", stderr or "", 124
                )
                self._record_go_test_results(parsed, 124)
                captured_events = bool(
                    parsed["root_count"]
                    or parsed["subtest_count"]
                    or {
                        "test-incomplete",
                        "test-run-duplicate",
                    }.intersection(parsed["problems"])
                )
                if not captured_events:
                    problems = list(self.tests.get("problems", []))
                    problems.append("no-captured-test-events")
                    self.tests["problems"] = sorted(set(problems))
            raise LauncherError(
                stage, "timeout", f"process group timed out after {timeout}s"
            ) from exc
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.cleanup_failures.append(f"{stage}:reap")
            raise

        elapsed = time.monotonic() - start
        self._record_command(
            name, argv, process.returncode, allow_failure, timeout, elapsed
        )
        if process.returncode != 0 and not allow_failure:
            raise LauncherError(
                stage, kind, f"command failed with exit {process.returncode}"
            )
        return subprocess.CompletedProcess(
            list(argv), process.returncode, stdout, stderr
        )

    # ------------------------------------------------------------------
    # Go preparation
    # ------------------------------------------------------------------

    def _module_snapshot(self) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for name in ("go.mod", "go.sum"):
            path = self.repo / name
            if not path.is_file():
                raise LauncherError("environment", "module", f"{name} is missing")
            data = path.read_bytes()
            if name == "go.sum" and not data:
                raise LauncherError("environment", "module", "go.sum is empty")
            snapshot[name] = hashlib.sha256(data).hexdigest()
        return snapshot

    def collect_source_manifest(self) -> str:
        paths = set(SOURCE_MANIFEST_EXPLICIT)
        for pattern in SOURCE_MANIFEST_GLOBS:
            for path in self.repo.glob(pattern):
                if path.is_file():
                    paths.add(str(path.relative_to(self.repo)))
        manifest: list[dict[str, str]] = []
        digest = hashlib.sha256()
        for relative in sorted(paths):
            path = self.repo / relative
            if not path.is_file():
                raise LauncherError("source", "missing", "a source manifest file is missing")
            file_hash = sha256_file(path)
            manifest.append({"path": relative, "sha256": file_hash})
            digest.update(relative.encode("utf-8"))
            digest.update(b"\x00")
            digest.update(file_hash.encode("utf-8"))
            digest.update(b"\x00")
        self.source["manifest"] = manifest
        self.source["manifest_digest"] = digest.hexdigest()
        self.source_manifest_digest = digest.hexdigest()
        return self.source_manifest_digest

    def verify_source_manifest(self) -> None:
        require(
            self.source_manifest_digest is not None,
            "test",
            "source-manifest",
            "source manifest was not collected",
        )
        before = self.source_manifest_digest
        after = self.collect_source_manifest()
        require(
            after == before,
            "test",
            "source-manifest",
            "source manifest changed during validation",
        )
        self.source["manifest_unchanged"] = True

    def check_go(self) -> None:
        self._run(
            "environment",
            "go",
            "go telemetry off",
            [self.go, "telemetry", "off"],
            self.offline_go_env,
            BUDGETS["go_telemetry_seconds"],
        )

        env_result = self._run(
            "environment",
            "go-env",
            "go env",
            [
                self.go,
                "env",
                "-json",
                "GOVERSION",
                "GOOS",
                "GOARCH",
                "GOTOOLCHAIN",
                "GOTELEMETRY",
                "CGO_ENABLED",
            ],
            self.offline_go_env,
            BUDGETS["go_env_seconds"],
        )
        try:
            info = json.loads(env_result.stdout)
        except json.JSONDecodeError as exc:
            raise LauncherError("environment", "go-env", "invalid go env JSON") from exc
        if not isinstance(info, dict):
            raise LauncherError("environment", "go-env", "go env did not return an object")
        expected = {
            "GOVERSION": EXPECTED_GO_VERSION,
            "GOOS": EXPECTED_GOOS,
            "GOARCH": EXPECTED_GOARCH,
            "GOTOOLCHAIN": EXPECTED_GOTOOLCHAIN,
            "GOTELEMETRY": EXPECTED_GOTELEMETRY,
            "CGO_ENABLED": "1",
        }
        for key, value in expected.items():
            if info.get(key) != value:
                raise LauncherError(
                    "environment", "go-version", f"{key} does not match the approved value"
                )
        self.versions["go"] = EXPECTED_GO_VERSION

        edit_result = self._run(
            "environment",
            "module",
            "go mod edit",
            [self.go, "mod", "edit", "-json"],
            self.offline_go_env,
            BUDGETS["go_env_seconds"],
        )
        try:
            module = json.loads(edit_result.stdout)
        except json.JSONDecodeError as exc:
            raise LauncherError(
                "environment", "module", "invalid go mod edit JSON"
            ) from exc
        if not isinstance(module, dict):
            raise LauncherError(
                "environment", "module", "go mod edit did not return an object"
            )
        module_path = (module.get("Module") or {}).get("Path")
        if module_path != EXPECTED_MODULE:
            raise LauncherError(
                "environment", "module", "module path does not match the approved value"
            )
        if module.get("Go") != EXPECTED_GO_DIRECTIVE:
            raise LauncherError(
                "environment", "module", "go directive does not match the approved value"
            )
        direct: dict[str, Any] = {}
        for entry in module.get("Require") or []:
            if not isinstance(entry, dict):
                raise LauncherError(
                    "environment", "module", "malformed go.mod require entry"
                )
            if entry.get("Indirect", False):
                continue
            path = entry.get("Path")
            if path in direct:
                raise LauncherError(
                    "environment", "module", "duplicate direct requirement"
                )
            direct[path] = entry.get("Version")
        if direct != APPROVED_DIRECT:
            raise LauncherError(
                "environment", "module", "direct requirements changed"
            )

    def check_modules(self) -> None:
        before = self._module_snapshot()

        if self.args.download_modules:
            self._run(
                "environment",
                "module-download",
                "go mod download all",
                [self.go, "mod", "download", "all"],
                self.go_env,
                BUDGETS["go_mod_download_seconds"],
            )

        listed = self._run(
            "environment",
            "module-list",
            "go list -m all",
            [self.go, "list", "-m", "-mod=readonly", "all"],
            self.offline_go_env,
            BUDGETS["go_list_seconds"],
        )
        lines = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        if not lines:
            raise LauncherError("environment", "module-list", "empty module graph")
        first_fields = lines[0].split()
        if not first_fields or first_fields[0] != EXPECTED_MODULE:
            raise LauncherError(
                "environment", "module-list", "module graph does not start with the project"
            )
        if any(line.split()[0].startswith("github.com/pressly/goose") for line in lines if line.split()):
            raise LauncherError(
                "environment",
                "module-list",
                "Goose must not be part of the application module graph",
            )

        verified = self._run(
            "environment",
            "module-verify",
            "go mod verify",
            [self.go, "mod", "verify"],
            self.offline_go_env,
            BUDGETS["go_mod_verify_seconds"],
        )
        if "all modules verified" not in verified.stdout:
            raise LauncherError(
                "environment", "module-verify", "go mod verify did not confirm the cache"
            )

        after = self._module_snapshot()
        if after != before:
            raise LauncherError(
                "environment", "module-hash", "go.mod/go.sum changed during preparation"
            )
        self.module = {
            "before": before,
            "after": after,
            "count": len(lines),
        }
        self.source["go_mod_sha256"] = before["go.mod"]
        self.source["go_sum_sha256"] = before["go.sum"]

    # ------------------------------------------------------------------
    # Docker/Compose preflight
    # ------------------------------------------------------------------

    def check_docker(self) -> None:
        docker = shutil.which("docker")
        if not docker:
            raise LauncherError("environment", "docker", "docker CLI not found")
        self.docker = docker
        self.docker_env = self._docker_environment()

        ambient_host = self.docker_env.get("DOCKER_HOST", "")
        if ambient_host and not ambient_host.startswith("unix://"):
            raise LauncherError(
                "environment",
                "remote-daemon",
                "DOCKER_HOST must be a local unix socket",
            )

        version = self._run(
            "environment",
            "docker-version",
            "docker --version",
            [docker, "--version"],
            self.docker_env,
            BUDGETS["docker_version_seconds"],
        )
        self.versions["docker"] = first_line(version.stdout)

        context_result = self._run(
            "environment",
            "docker-context",
            "docker context show",
            [docker, "context", "show"],
            self.docker_env,
            BUDGETS["docker_version_seconds"],
        )
        context = context_result.stdout.strip()
        if not context:
            raise LauncherError(
                "environment", "docker-context", "docker context is empty"
            )
        endpoint_result = self._run(
            "environment",
            "docker-context",
            "docker context inspect",
            [
                docker,
                "context",
                "inspect",
                context,
                "--format",
                "{{json .Endpoints.docker.Host}}",
            ],
            self.docker_env,
            BUDGETS["docker_version_seconds"],
        )
        try:
            endpoint = json.loads(endpoint_result.stdout)
        except json.JSONDecodeError as exc:
            raise LauncherError(
                "environment", "docker-context", "invalid docker context endpoint"
            ) from exc
        if not isinstance(endpoint, str) or not endpoint.startswith("unix://"):
            raise LauncherError(
                "environment",
                "remote-daemon",
                "docker context must use a local unix socket",
            )

        server_result = self._run(
            "environment",
            "docker-server",
            "docker version server",
            [docker, "version", "--format", "{{json .Server}}"],
            self.docker_env,
            BUDGETS["docker_version_seconds"],
        )
        try:
            server = json.loads(server_result.stdout)
        except json.JSONDecodeError as exc:
            raise LauncherError(
                "environment", "docker-server", "invalid docker server metadata"
            ) from exc
        if not isinstance(server, dict):
            raise LauncherError(
                "environment", "docker-server", "docker server metadata missing"
            )
        self.server_os = server.get("Os")
        self.server_arch = server.get("Arch")
        if self.server_os != EXPECTED_GOOS or self.server_arch != EXPECTED_GOARCH:
            raise LauncherError(
                "environment",
                "docker-platform",
                "docker daemon must be linux/amd64",
            )

        compose_version = self._run(
            "environment",
            "compose-version",
            "docker compose version",
            [docker, "compose", "version"],
            self.docker_env,
            BUDGETS["docker_version_seconds"],
        )
        self.versions["compose"] = first_line(compose_version.stdout)

        self.compose_env = dict(self.docker_env)
        self.compose_env.update(
            {
                "COMPOSE_PROJECT_NAME": self.project,
                "PAYMENT_HUB_POSTGRES_DB": self.database,
                "PAYMENT_HUB_POSTGRES_USER": MOCK_DB_USER,
                "PAYMENT_HUB_POSTGRES_PASSWORD": MOCK_DB_PASSWORD,
                "PAYMENT_HUB_TEST_PG_PORT": "0",
            }
        )

    def _compose_argv(self, *arguments: str) -> list[str]:
        return [
            self.docker or "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            "deploy/compose.yaml",
            "-f",
            "deploy/compose.test.yaml",
            "-p",
            self.project,
            *arguments,
        ]

    def check_compose_model(self) -> None:
        result = self._run(
            "environment",
            "compose-config",
            "compose config",
            self._compose_argv("config", "--format", "json"),
            self.compose_env,
            BUDGETS["compose_config_seconds"],
        )
        try:
            config = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise LauncherError(
                "environment", "compose-config", "invalid compose config JSON"
            ) from exc
        if not isinstance(config, dict):
            raise LauncherError(
                "environment", "compose-config", "compose config is not an object"
            )
        if config.get("name") != self.project:
            raise LauncherError(
                "environment", "compose-config", "compose project name mismatch"
            )
        services = config.get("services")
        if not isinstance(services, dict) or set(services) != {"postgres"}:
            raise LauncherError(
                "environment", "compose-config", "compose services mismatch"
            )
        service = services["postgres"]
        if not isinstance(service, dict):
            raise LauncherError(
                "environment", "compose-config", "postgres service is malformed"
            )
        if service.get("image") != POSTGRES_IMAGE:
            raise LauncherError(
                "environment", "compose-config", "postgres image mismatch"
            )
        if service.get("platform") != EXPECTED_PLATFORM:
            raise LauncherError(
                "environment", "compose-config", "postgres platform mismatch"
            )
        environment = service.get("environment")
        if not isinstance(environment, dict):
            raise LauncherError(
                "environment", "compose-config", "postgres environment missing"
            )
        expected_environment = {
            "POSTGRES_DB": self.database,
            "POSTGRES_USER": MOCK_DB_USER,
            "POSTGRES_PASSWORD": MOCK_DB_PASSWORD,
            "TZ": "UTC",
            "PGTZ": "UTC",
        }
        for key, value in expected_environment.items():
            if environment.get(key) != value:
                raise LauncherError(
                    "environment",
                    "compose-config",
                    f"postgres environment mismatch for {key}",
                )
        ports = service.get("ports")
        if not isinstance(ports, list) or len(ports) != 1:
            raise LauncherError(
                "environment", "compose-config", "postgres test port missing"
            )
        port = ports[0]
        if not isinstance(port, dict):
            raise LauncherError(
                "environment", "compose-config", "postgres test port malformed"
            )
        if (
            port.get("target") != 5432
            or port.get("host_ip") != "127.0.0.1"
            or str(port.get("published")) != "0"
            or port.get("protocol") != "tcp"
        ):
            raise LauncherError(
                "environment",
                "compose-config",
                "postgres test port must be 127.0.0.1:0/tcp",
            )
        volumes = config.get("volumes")
        if not isinstance(volumes, dict) or set(volumes) != {"postgres_data"}:
            raise LauncherError(
                "environment", "compose-config", "compose volume model mismatch"
            )
        volume = volumes["postgres_data"]
        if not isinstance(volume, dict) or volume.get("name") != self.volume_name:
            raise LauncherError(
                "environment", "compose-config", "compose volume name mismatch"
            )
        networks = config.get("networks")
        if not isinstance(networks, dict) or set(networks) != {"default"}:
            raise LauncherError(
                "environment", "compose-config", "compose network model mismatch"
            )
        network = networks["default"]
        if not isinstance(network, dict) or network.get("name") != self.network_name:
            raise LauncherError(
                "environment", "compose-config", "compose network name mismatch"
            )
        logging = service.get("logging")
        if not isinstance(logging, dict) or logging.get("driver") != "local":
            raise LauncherError(
                "environment", "compose-config", "postgres logging must be local"
            )
        options = logging.get("options") or {}
        if str(options.get("max-size")) != "10m" or str(options.get("max-file")) != "3":
            raise LauncherError(
                "environment", "compose-config", "postgres log rotation mismatch"
            )
        healthcheck = service.get("healthcheck")
        if not isinstance(healthcheck, dict):
            raise LauncherError(
                "environment", "compose-config", "postgres healthcheck missing"
            )
        health_test = healthcheck.get("test")
        if isinstance(health_test, list):
            health_text = " ".join(str(item) for item in health_test)
        elif isinstance(health_test, str):
            health_text = health_test
        else:
            raise LauncherError(
                "environment", "compose-config", "postgres healthcheck malformed"
            )
        if "pg_isready" not in health_text:
            raise LauncherError(
                "environment", "compose-config", "postgres healthcheck is not pg_isready"
            )

    def _docker_ids(self, argv: Sequence[str], stage: str, kind: str) -> list[str]:
        result = self._run(
            stage,
            kind,
            kind,
            argv,
            self.docker_env,
            BUDGETS["docker_version_seconds"],
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def preflight_resources(self) -> None:
        project_filter = f"label=com.docker.compose.project={self.project}"
        checks = [
            (
                "containers",
                [self.docker or "docker", "ps", "-a", "--filter", project_filter, "--format", "{{.ID}}"],
            ),
            (
                "volumes",
                [self.docker or "docker", "volume", "ls", "--filter", project_filter, "--format", "{{.Name}}"],
            ),
            (
                "networks",
                [self.docker or "docker", "network", "ls", "--filter", project_filter, "--format", "{{.Name}}"],
            ),
            (
                "volume-name",
                [self.docker or "docker", "volume", "ls", "--filter", f"name=^{self.volume_name}$", "--format", "{{.Name}}"],
            ),
            (
                "network-name",
                [self.docker or "docker", "network", "ls", "--filter", f"name=^{self.network_name}$", "--format", "{{.Name}}"],
            ),
        ]
        for label, argv in checks:
            found = self._docker_ids(argv, "environment", f"preflight-{label}")
            if found:
                raise LauncherError(
                    "environment",
                    "resource-conflict",
                    f"existing resource detected for {label}",
                )
        self.preflight = {"no_existing_resources": True}

    # ------------------------------------------------------------------
    # Images
    # ------------------------------------------------------------------

    def _inspect_image(self, ref: str, stage: str) -> dict[str, Any] | None:
        result = self._run(
            stage,
            "image-inspect",
            "docker image inspect",
            [self.docker or "docker", "image", "inspect", "--format", "{{json .}}", ref],
            self.docker_env,
            BUDGETS["image_inspect_seconds"],
            allow_failure=True,
        )
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise LauncherError(stage, "image-inspect", "invalid image metadata") from exc
        if not isinstance(data, dict):
            raise LauncherError(stage, "image-inspect", "image metadata is not an object")
        return data

    def _verify_base_image(self, name: str, ref: str, image: Mapping[str, Any]) -> None:
        image_id = image.get("Id")
        os_name = image.get("Os")
        arch = image.get("Architecture")
        if not isinstance(image_id, str) or not image_id:
            raise LauncherError("image", "image-id", f"{name} image ID is missing")
        if os_name != EXPECTED_GOOS or arch != EXPECTED_GOARCH:
            raise LauncherError("image", "image-platform", f"{name} image platform mismatch")
        digest = ref.split("@", 1)[1]
        repo_digests = image.get("RepoDigests") or []
        if not isinstance(repo_digests, list) or not any(
            isinstance(item, str) and item.endswith("@" + digest)
            for item in repo_digests
        ):
            raise LauncherError("image", "image-digest", f"{name} image digest mismatch")
        self.images["base"][name] = {
            "ref": ref,
            "id": image_id,
            "os": os_name,
            "arch": arch,
            "digest_match": True,
        }

    def _pull_image(self, ref: str, stage: str) -> None:
        self._run(
            stage,
            "image-pull",
            "docker pull",
            [self.docker or "docker", "pull", "--platform", EXPECTED_PLATFORM, ref],
            self.docker_env,
            BUDGETS["image_pull_seconds"],
        )

    def prepare_images(self) -> None:
        self.image_key, self.sql_hashes = compute_image_key(self.repo)
        self.migration_tag = MIGRATION_TAG_PREFIX + self.image_key[:20]
        self.source.update(
            {
                "dockerfile_migrate_sha256": sha256_file(
                    self.repo / "deploy/Dockerfile.migrate"
                ),
                "goose_entrypoint_sha256": sha256_file(
                    self.repo / "deploy/goose-entrypoint.sh"
                ),
                "sql": [
                    {"name": name, "sha256": self.sql_hashes[name]}
                    for name in MIGRATION_SQL_NAMES
                ],
                "image_key": self.image_key,
                "migration_tag": self.migration_tag,
            }
        )

        dockerfile_text = (self.repo / "deploy/Dockerfile.migrate").read_text(
            encoding="utf-8"
        )
        for ref in (GO_BUILDER_IMAGE, MIGRATION_RUNTIME_IMAGE):
            if ref not in dockerfile_text:
                raise LauncherError(
                    "image", "fixed-input", "Dockerfile does not use an approved base ref"
                )
        if re.search(r"(?i):latest\b", dockerfile_text):
            raise LauncherError(
                "image", "fixed-input", "Dockerfile contains a latest tag"
            )

        for name, ref in BASE_IMAGES.items():
            image = self._inspect_image(ref, "image")
            if image is None:
                if not self.args.prepare_images:
                    raise LauncherError(
                        "image", "missing", f"base image {name} is not available"
                    )
                self._pull_image(ref, "image")
                image = self._inspect_image(ref, "image")
                if image is None:
                    raise LauncherError(
                        "image", "pull", f"base image {name} is still unavailable"
                    )
            self._verify_base_image(name, ref, image)

        image = self._inspect_image(self.migration_tag, "image")
        if image is None:
            if not self.args.prepare_images:
                raise LauncherError(
                    "image", "missing", "migration image tag is not available"
                )
            self._run(
                "image-build",
                "build",
                "docker build migration",
                [
                    self.docker or "docker",
                    "build",
                    "--platform",
                    EXPECTED_PLATFORM,
                    "-f",
                    "deploy/Dockerfile.migrate",
                    "-t",
                    self.migration_tag,
                    ".",
                ],
                self.docker_env,
                BUDGETS["image_build_seconds"],
            )
            image = self._inspect_image(self.migration_tag, "image")
            if image is None:
                raise LauncherError(
                    "image", "build", "migration image was not produced"
                )
        self._record_migration_image(image)

    def _record_migration_image(self, image: Mapping[str, Any]) -> None:
        image_id = image.get("Id")
        os_name = image.get("Os")
        arch = image.get("Architecture")
        if not isinstance(image_id, str) or not image_id:
            raise LauncherError("image", "image-id", "migration image ID is missing")
        if os_name != EXPECTED_GOOS or arch != EXPECTED_GOARCH:
            raise LauncherError(
                "image", "image-platform", "migration image platform mismatch"
            )
        config = image.get("Config")
        env = config.get("Env") if isinstance(config, dict) else None
        if isinstance(env, list):
            for item in env:
                text = str(item)
                if (
                    text.startswith("GOOSE_DBSTRING=")
                    or text.startswith("PGPASSWORD=")
                    or "postgres://" in text
                    or "postgresql://" in text
                    or MOCK_DB_PASSWORD in text
                    or MOCK_CANARY in text
                ):
                    raise LauncherError(
                        "image", "baked-dsn", "migration image contains a baked connection value"
                    )
        self.migration_image_id = image_id
        self.images["migration"] = {
            "tag": self.migration_tag,
            "id": image_id,
            "os": os_name,
            "arch": arch,
            "has_baked_dsn": False,
        }

    # ------------------------------------------------------------------
    # Tool containers
    # ------------------------------------------------------------------

    def _assert_container_name_absent(self, name: str) -> None:
        result = self._run(
            "environment",
            "container-preflight",
            "container preflight",
            [
                self.docker or "docker",
                "ps",
                "-a",
                "--filter",
                f"name=^{name}$",
                "--format",
                "{{.ID}}",
            ],
            self.docker_env,
            BUDGETS["docker_version_seconds"],
        )
        require(
            not result.stdout.strip(),
            "environment",
            "resource-conflict",
            "intended tool container name already exists",
        )

    def _inspect_container(
        self, container_id: str, stage: str
    ) -> dict[str, Any] | None:
        result = self._run(
            stage,
            "container-inspect",
            "container inspect",
            [
                self.docker or "docker",
                "inspect",
                "--format",
                "{{json .}}",
                container_id,
            ],
            self.docker_env,
            BUDGETS["docker_version_seconds"],
            allow_failure=True,
        )
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise LauncherError(
                stage, "container-inspect", "invalid container metadata"
            ) from exc
        if not isinstance(data, dict):
            raise LauncherError(
                stage, "container-inspect", "container metadata is not an object"
            )
        return data

    def _record_matches(self, record: Mapping[str, Any], data: Mapping[str, Any]) -> bool:
        config = data.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        labels = labels if isinstance(labels, dict) else {}
        name = str(data.get("Name", "")).lstrip("/")
        return (
            name == record.get("name")
            and labels.get("payment-hub.run-id") == self.run_id
            and labels.get("payment-hub.scope") == "db-foundation"
        )

    def _find_intended_container(self, record: dict[str, Any]) -> str | None:
        result = self._run(
            "cleanup",
            "container-find",
            "container find",
            [
                self.docker or "docker",
                "ps",
                "-a",
                "--filter",
                f"name=^{record['name']}$",
                "--format",
                "{{.ID}}",
            ],
            self.docker_env,
            BUDGETS["docker_version_seconds"],
            allow_failure=True,
        )
        if result.returncode != 0:
            self.cleanup_failures.append("cleanup:container-list")
            return None
        ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not ids:
            record["absent"] = True
            return None
        if len(ids) != 1:
            self.cleanup_failures.append("cleanup:container-ambiguous")
            return None
        container_id = ids[0]
        data = self._inspect_container(container_id, "cleanup")
        if data is None or not self._record_matches(record, data):
            self.cleanup_failures.append("cleanup:container-identity")
            return None
        record["id"] = container_id
        record["created"] = True
        return container_id

    def _recover_intended_container(self, record: dict[str, Any]) -> str | None:
        container_id = self._find_intended_container(record)
        if container_id:
            return container_id
        if record.get("absent"):
            time.sleep(0.5)
            container_id = self._find_intended_container(record)
            if container_id:
                return container_id
        return None

    def _stop_remove_container(
        self, container_id: str, stage: str, record: dict[str, Any]
    ) -> None:
        data = self._inspect_container(container_id, "cleanup")
        if data is None or not self._record_matches(record, data):
            self.cleanup_failures.append(f"{stage}:container-identity")
            return
        try:
            self._run(
                "cleanup",
                "container-stop",
                f"{stage} stop",
                [self.docker or "docker", "stop", "--time", "5", container_id],
                self.docker_env,
                BUDGETS["container_stop_seconds"],
                allow_failure=True,
            )
            self._run(
                "cleanup",
                "container-remove",
                f"{stage} remove",
                [self.docker or "docker", "rm", "-f", container_id],
                self.docker_env,
                BUDGETS["container_remove_seconds"],
                allow_failure=True,
            )
            verify = self._run(
                "cleanup",
                "container-verify",
                f"{stage} verify removal",
                [
                    self.docker or "docker",
                    "ps",
                    "-a",
                    "--filter",
                    f"id={container_id}",
                    "--format",
                    "{{.ID}}",
                ],
                self.docker_env,
                BUDGETS["container_remove_seconds"],
            )
            if verify.stdout.strip():
                self.cleanup_failures.append(f"{stage}:container-remains")
            else:
                record["removed"] = True
        except LauncherError:
            self.cleanup_failures.append(f"{stage}:container-cleanup")

    def _check_leaks(self, stage: str, text: str, markers: Sequence[str]) -> None:
        for marker in markers:
            if marker and marker in text:
                raise LauncherError(
                    "safety",
                    "log-leak",
                    f"sensitive marker detected in {stage} output",
                )

    def _run_tool_container(
        self,
        stage: str,
        image_id: str,
        arguments: Sequence[str],
        timeout: float,
        *,
        container_env: Mapping[str, str] | None = None,
        network: str = "none",
        entrypoint: str | None = None,
        expect_success: bool,
        leak_markers: Sequence[str] = (),
    ) -> tuple[int, str]:
        container_env = container_env or {}
        name = f"ph-db-{self.run_id[:12]}-{stage}"
        record: dict[str, Any] = {
            "stage": stage,
            "name": name,
            "run_id": self.run_id,
            "scope": "db-foundation",
            "id": None,
            "created": False,
            "removed": False,
            "wait_client_exit": None,
            "container_exit_code": None,
        }
        self._assert_container_name_absent(name)
        self.tool_containers.append(record)

        create_argv: list[str] = [
            self.docker or "docker",
            "create",
            "--name",
            name,
            "--label",
            f"payment-hub.run-id={self.run_id}",
            "--label",
            "payment-hub.scope=db-foundation",
            "--label",
            f"payment-hub.role={stage}",
            "--platform",
            EXPECTED_PLATFORM,
            "--network",
            network,
            *DOCKER_LOG_OPTIONS,
        ]
        if entrypoint:
            create_argv.extend(["--entrypoint", entrypoint])
        for key in container_env:
            create_argv.extend(["--env", key])
        create_argv.append(image_id)
        create_argv.extend(str(item) for item in arguments)

        create_env = dict(self.docker_env)
        create_env.update(container_env)
        try:
            created = self._run(
                stage,
                "container-create",
                f"{stage} create",
                create_argv,
                create_env,
                BUDGETS["tool_create_seconds"],
            )
        except LauncherError:
            try:
                self._recover_intended_container(record)
            except LauncherError:
                pass
            if record.get("id"):
                self._stop_remove_container(record["id"], stage, record)
            raise
        container_id = created.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            try:
                self._recover_intended_container(record)
            except LauncherError:
                pass
            if record.get("id"):
                self._stop_remove_container(record["id"], stage, record)
            raise LauncherError(
                stage, "container-id", "docker create did not return a container ID"
            )
        record["id"] = container_id
        record["created"] = True

        deadline = time.monotonic() + timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._stop_remove_container(container_id, stage, record)
            raise LauncherError(
                stage, "timeout", "container budget expired before start"
            )
        start_timeout = min(BUDGETS["tool_start_max_seconds"], remaining)
        try:
            self._run(
                stage,
                "container-start",
                f"{stage} start",
                [self.docker or "docker", "start", container_id],
                self.docker_env,
                start_timeout,
            )
        except LauncherError:
            self._stop_remove_container(container_id, stage, record)
            raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._stop_remove_container(container_id, stage, record)
            raise LauncherError(
                stage, "timeout", "container budget expired after start"
            )

        wait_start = time.monotonic()
        try:
            waited = subprocess.run(
                [self.docker or "docker", "wait", container_id],
                cwd=str(self.repo),
                env=dict(self.docker_env),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=remaining,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - wait_start
            self._record_command(
                f"{stage} wait",
                [self.docker or "docker", "wait", container_id],
                124,
                False,
                remaining,
                elapsed,
                timed_out=True,
            )
            record["wait_client_exit"] = 124
            self._stop_remove_container(container_id, stage, record)
            raise LauncherError(
                stage,
                "timeout",
                f"container exceeded {timeout}s and was stopped/removed",
            ) from exc
        except OSError as exc:
            self._stop_remove_container(container_id, stage, record)
            raise LauncherError(
                stage, "container-wait", "could not wait for container"
            ) from exc

        elapsed = time.monotonic() - wait_start
        wait_client_exit = waited.returncode
        wait_text = waited.stdout.strip()
        record["wait_client_exit"] = wait_client_exit
        if wait_client_exit != 0 or not wait_text.isdigit():
            self._record_command(
                f"{stage} wait",
                [self.docker or "docker", "wait", container_id],
                wait_client_exit,
                False,
                remaining,
                elapsed,
            )
            self._stop_remove_container(container_id, stage, record)
            raise LauncherError(
                stage,
                "container-wait",
                "docker wait client failed or returned no exit code",
            )
        container_exit = int(wait_text)
        record["container_exit_code"] = container_exit
        self._record_command(
            f"{stage} wait",
            [self.docker or "docker", "wait", container_id],
            wait_client_exit,
            False,
            remaining,
            elapsed,
        )

        try:
            logs = self._run(
                stage,
                "container-logs",
                f"{stage} logs",
                [self.docker or "docker", "logs", container_id],
                self.docker_env,
                BUDGETS["docker_version_seconds"],
            )
        except LauncherError:
            self._stop_remove_container(container_id, stage, record)
            raise
        combined = (logs.stdout or "") + (
            "\n" if logs.stdout and logs.stderr else ""
        ) + (logs.stderr or "")
        try:
            self._check_leaks(stage, combined, leak_markers)
        except LauncherError:
            self._stop_remove_container(container_id, stage, record)
            raise
        self._stop_remove_container(container_id, stage, record)

        if expect_success and container_exit != 0:
            raise LauncherError(
                stage, "container-exit", f"container exited {container_exit}"
            )
        if not expect_success and container_exit == 0:
            raise LauncherError(
                stage, "expected-nonzero", "container unexpectedly exited 0"
            )
        return container_exit, combined

    def _parse_tool_events(
        self, output: str, command: str, statuses: Sequence[str]
    ) -> list[dict[str, Any]]:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        require(
            len(lines) == len(statuses),
            "safety",
            "tool-protocol",
            "tool output did not contain the expected fixed event count",
        )
        events: list[dict[str, Any]] = []
        for line, expected_status in zip(lines, statuses):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LauncherError(
                    "safety", "tool-protocol", "tool output was not fixed JSON"
                ) from exc
            require(
                isinstance(event, dict)
                and set(event) == {"event", "command", "status"},
                "safety",
                "tool-protocol",
                "tool event keys are not whitelisted",
            )
            require(
                event.get("event") == "migration",
                "safety",
                "tool-protocol",
                "tool event name is not whitelisted",
            )
            require(
                event.get("command") == command,
                "safety",
                "tool-protocol",
                "tool event command is not whitelisted",
            )
            require(
                event.get("status") == expected_status,
                "safety",
                "tool-protocol",
                "tool event status is not whitelisted",
            )
            events.append(event)
        return events

    def run_safety_entry_rejections(self) -> None:
        if not self.migration_image_id:
            raise LauncherError("image", "missing", "migration image ID is unavailable")
        cases: list[dict[str, Any]] = []
        leak_markers = (
            MOCK_CANARY,
            "postgresql://",
            "postgres://",
            "CREATE TABLE",
            "SELECT ",
            "INSERT INTO",
        )
        for label, arguments in REJECTION_CASES:
            try:
                exit_code, output = self._run_tool_container(
                    f"reject-{label}",
                    self.migration_image_id,
                    arguments,
                    BUDGETS["tool_version_seconds"],
                    network="none",
                    expect_success=False,
                    leak_markers=leak_markers,
                )
            except LauncherError as exc:
                raise LauncherError(
                    "safety-entry", exc.kind, exc.message
                ) from exc
            require(
                exit_code == 2,
                "safety-entry",
                "exit",
                "rejection case did not exit 2",
            )
            try:
                self._parse_tool_events(output, "rejected", ["error"])
            except LauncherError as exc:
                raise LauncherError(
                    "safety-entry", exc.kind, exc.message
                ) from exc
            cases.append({"case": label, "exit": exit_code, "protocol": "ok"})
        self.safety_entry = {"cases": cases}

    def verify_migration_image(self) -> None:
        if not self.migration_image_id:
            raise LauncherError("image", "missing", "migration image ID is unavailable")
        image = self._inspect_image(self.migration_tag or "", "image")
        if image is None:
            raise LauncherError("image", "missing", "migration image tag disappeared")
        self._record_migration_image(image)

        exit_code, output = self._run_tool_container(
            "version",
            self.migration_image_id,
            ["-version"],
            BUDGETS["tool_version_seconds"],
            network="none",
            expect_success=True,
        )
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if exit_code != 0 or lines != [EXPECTED_GOOSE_VERSION]:
            raise LauncherError(
                "image", "goose-version", "migration image version mismatch"
            )
        self.versions["goose"] = "v3.29.0"

        hash_script = "\n".join(
            [
                "set -eu",
                "cd /migrations",
                "ls -1A | sort",
                "printf '%s\\n' '---'",
                "sha256sum -- *.sql | sort",
            ]
        )
        exit_code, output = self._run_tool_container(
            "sql-hash",
            self.migration_image_id,
            ["-c", hash_script],
            BUDGETS["tool_sql_hash_seconds"],
            network="none",
            entrypoint="/bin/sh",
            expect_success=True,
        )
        if exit_code != 0:
            raise LauncherError("image", "sql-manifest", "migration SQL manifest failed")
        lines = output.splitlines()
        try:
            separator = lines.index("---")
        except ValueError as exc:
            raise LauncherError(
                "image", "sql-manifest", "migration SQL manifest separator missing"
            ) from exc
        listing = [line.strip() for line in lines[:separator] if line.strip()]
        if listing != list(MIGRATION_SQL_NAMES):
            raise LauncherError(
                "image", "sql-manifest", "migration image does not contain exactly three SQL files"
            )
        found: dict[str, str] = {}
        for line in lines[separator + 1 :]:
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                found[parts[1].strip()] = parts[0].strip()
        if set(found) != set(MIGRATION_SQL_NAMES):
            raise LauncherError(
                "image", "sql-manifest", "migration image SQL hash list mismatch"
            )
        for name in MIGRATION_SQL_NAMES:
            if found.get(name) != self.sql_hashes.get(name):
                raise LauncherError(
                    "image", "sql-manifest", f"migration image SQL hash mismatch for {name}"
                )
        self.images["migration"]["sql_manifest_match"] = True
        self.images["migration"]["version"] = "v3.29.0"

    # ------------------------------------------------------------------
    # Negative DSN regression
    # ------------------------------------------------------------------

    def run_negative_dsn(self) -> None:
        if not self.migration_image_id:
            raise LauncherError("image", "missing", "migration image ID is unavailable")
        bad_dsn = make_dsn(
            "127.0.0.1",
            1,
            "payment_hub_never",
            MOCK_DB_USER,
            MOCK_CANARY,
        )
        exit_code, output = self._run_tool_container(
            "negative-dsn",
            self.migration_image_id,
            ["up"],
            BUDGETS["negative_dsn_seconds"],
            container_env={
                "GOOSE_DRIVER": "postgres",
                "GOOSE_DBSTRING": bad_dsn,
            },
            network="none",
            expect_success=False,
            leak_markers=(
                MOCK_CANARY,
                "postgresql://",
                "postgres://",
                "CREATE TABLE",
                "SELECT ",
                "INSERT INTO",
            ),
        )
        self._parse_tool_events(output, "up", ["started", "failed"])
        self.negative = {
            "expected_nonzero": True,
            "observed_exit": exit_code,
            "safe": True,
        }

    # ------------------------------------------------------------------
    # Compose PostgreSQL
    # ------------------------------------------------------------------

    def start_compose(self) -> None:
        self.compose_attempted = True
        self._run(
            "environment",
            "compose-up",
            "compose up",
            self._compose_argv("up", "-d", "--no-deps", "postgres"),
            self.compose_env,
            BUDGETS["compose_up_seconds"],
        )

        ps_result = self._run(
            "environment",
            "compose-ps",
            "compose ps",
            self._compose_argv("ps", "-q", "postgres"),
            self.compose_env,
            BUDGETS["docker_version_seconds"],
        )
        ids = [line.strip() for line in ps_result.stdout.splitlines() if line.strip()]
        if len(ids) != 1:
            raise LauncherError(
                "environment", "compose-container", "expected one postgres container"
            )
        self.container_id = ids[0]

        labels = self._inspect_json(
            "environment",
            "container-labels",
            [
                self.docker or "docker",
                "inspect",
                "--format",
                "{{json .Config.Labels}}",
                self.container_id,
            ],
        )
        if (
            labels.get("com.docker.compose.project") != self.project
            or labels.get("com.docker.compose.service") != "postgres"
        ):
            raise LauncherError(
                "environment", "container-labels", "postgres container labels mismatch"
            )

        port_result = self._run(
            "environment",
            "compose-port",
            "compose port",
            self._compose_argv("port", "postgres", "5432"),
            self.compose_env,
            BUDGETS["docker_version_seconds"],
        )
        port_text = port_result.stdout.strip()
        if not port_text or "\n" in port_text:
            raise LauncherError(
                "environment", "published-port", "expected one published postgres port"
            )
        host, separator, port_number = port_text.rpartition(":")
        if not separator or host != "127.0.0.1":
            raise LauncherError(
                "environment",
                "published-port",
                "postgres must publish on 127.0.0.1",
            )
        try:
            published = int(port_number)
        except ValueError as exc:
            raise LauncherError(
                "environment", "published-port", "published port is not numeric"
            ) from exc
        if published <= 0 or published > 65535:
            raise LauncherError(
                "environment", "published-port", "published port is invalid"
            )
        self.published_port = published

        volume = self._inspect_json(
            "environment",
            "volume-inspect",
            [
                self.docker or "docker",
                "volume",
                "inspect",
                "--format",
                "{{json .}}",
                self.volume_name,
            ],
        )
        volume_labels = volume.get("Labels") or {}
        if (
            volume.get("Name") != self.volume_name
            or volume_labels.get("com.docker.compose.project") != self.project
        ):
            raise LauncherError(
                "environment", "volume-identity", "created volume identity mismatch"
            )
        self.volume_created = True

        network = self._inspect_json(
            "environment",
            "network-inspect",
            [
                self.docker or "docker",
                "network",
                "inspect",
                "--format",
                "{{json .}}",
                self.network_name,
            ],
        )
        network_labels = network.get("Labels") or {}
        if (
            network.get("Name") != self.network_name
            or network_labels.get("com.docker.compose.project") != self.project
        ):
            raise LauncherError(
                "environment", "network-identity", "created network identity mismatch"
            )
        self.network_created = True
        self.project_created = True
        self.compose_created = {
            "containers": [self.container_id],
            "network": self.network_name,
            "volume": self.volume_name,
        }
        self.preflight["created"] = {
            "container_id": self.container_id,
            "volume": self.volume_name,
            "network": self.network_name,
            "published_port": self.published_port,
        }

    def _inspect_json(
        self,
        stage: str,
        kind: str,
        argv: Sequence[str],
        timeout: int = 30,
    ) -> dict[str, Any]:
        result = self._run(
            stage,
            kind,
            kind,
            argv,
            self.docker_env,
            timeout,
            allow_failure=False,
        )
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise LauncherError(stage, kind, "docker inspect returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise LauncherError(stage, kind, "docker inspect did not return an object")
        return data

    def wait_pg(self) -> None:
        if not self.container_id:
            raise LauncherError("environment", "pg-health", "postgres container is missing")
        start = time.monotonic()
        deadline = start + BUDGETS["pg_health_seconds"]
        status: str | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LauncherError(
                    "environment",
                    "pg-timeout",
                    "postgres did not become healthy within the probe budget",
                )
            result = self._run(
                "environment",
                "pg-health",
                "postgres health",
                [
                    self.docker or "docker",
                    "inspect",
                    "--format",
                    "{{json .State.Health.Status}}",
                    self.container_id,
                ],
                self.docker_env,
                min(BUDGETS["docker_version_seconds"], remaining),
                allow_failure=True,
            )
            if result.returncode != 0:
                raise LauncherError(
                    "environment", "pg-health", "could not read postgres health"
                )
            raw = result.stdout.strip()
            try:
                status = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                status = None
            now = time.monotonic()
            if now > deadline:
                raise LauncherError(
                    "environment",
                    "pg-timeout",
                    "postgres health exceeded the independent probe budget",
                )
            if status == "healthy":
                break
            if status == "unhealthy":
                raise LauncherError(
                    "environment", "pg-unhealthy", "postgres health is unhealthy"
                )
            remaining = deadline - now
            if remaining <= 0:
                raise LauncherError(
                    "environment",
                    "pg-timeout",
                    "postgres did not become healthy within the probe budget",
                )
            time.sleep(min(1, remaining))
        self.pg["health_wait_seconds"] = round(time.monotonic() - start, 3)
        self.pg["health_status"] = "healthy"

        version = self._run(
            "environment",
            "pg-version",
            "postgres version",
            [
                self.docker or "docker",
                "exec",
                self.container_id,
                "postgres",
                "--version",
            ],
            self.docker_env,
            BUDGETS["pg_version_seconds"],
        )
        if EXPECTED_PG_VERSION not in version.stdout:
            raise LauncherError(
                "environment", "pg-version", "postgres binary version mismatch"
            )
        self.versions["postgres"] = EXPECTED_PG_VERSION
        self.pg["version"] = EXPECTED_PG_VERSION

    # ------------------------------------------------------------------
    # Real migrations
    # ------------------------------------------------------------------

    def run_migrations(self) -> None:
        if not self.migration_image_id:
            raise LauncherError("image", "missing", "migration image ID is unavailable")
        container_dsn = make_dsn(
            "postgres",
            5432,
            self.database,
            MOCK_DB_USER,
            MOCK_DB_PASSWORD,
        )
        container_env = {
            "GOOSE_DRIVER": "postgres",
            "GOOSE_DBSTRING": container_dsn,
        }
        leak_markers = (
            MOCK_DB_PASSWORD,
            "postgresql://",
            "postgres://",
            "CREATE TABLE",
            "SELECT ",
            "INSERT INTO",
        )

        up_start = time.monotonic()
        up_exit, up_output = self._run_tool_container(
            "migration-up",
            self.migration_image_id,
            ["up"],
            BUDGETS["migration_up_seconds"],
            container_env=container_env,
            network=self.network_name,
            expect_success=True,
            leak_markers=leak_markers,
        )
        up_elapsed = round(time.monotonic() - up_start, 3)
        self._parse_tool_events(up_output, "up", ["started", "ok"])

        again_start = time.monotonic()
        again_exit, again_output = self._run_tool_container(
            "migration-up-again",
            self.migration_image_id,
            ["up"],
            BUDGETS["migration_up_again_seconds"],
            container_env=container_env,
            network=self.network_name,
            expect_success=True,
            leak_markers=leak_markers,
        )
        again_elapsed = round(time.monotonic() - again_start, 3)
        self._parse_tool_events(again_output, "up", ["started", "ok"])

        self.migration = {
            "up_exit": up_exit,
            "up_again_exit": again_exit,
            "up_seconds": up_elapsed,
            "up_again_seconds": again_elapsed,
            "up_timeout_seconds": BUDGETS["migration_up_seconds"],
            "up_again_timeout_seconds": BUDGETS["migration_up_again_seconds"],
        }

    # ------------------------------------------------------------------
    # Host integration tests
    # ------------------------------------------------------------------

    def _extract_diagnostics(self, output: str) -> list[dict[str, str]]:
        diagnostics: list[dict[str, str]] = []
        current_test = ""
        for line in output.splitlines():
            match = re.match(r"^--- FAIL:\s+(\S+)", line.strip())
            if match:
                current_test = match.group(1)
                continue
            match = re.match(r"^\s+(\S+\.go:\d+):\s*(.*)$", line)
            if not match:
                continue
            location = match.group(1)
            message = match.group(2)
            diagnostic = {
                "test": current_test,
                "location": location,
                "category": "assertion",
            }
            sqlstate = re.search(
                r"expected SQLSTATE ([0-9A-Z]{5}) rejection, got SQLSTATE ([0-9A-Z]{5})",
                message,
            )
            if sqlstate:
                diagnostic["category"] = "sqlstate"
                diagnostic["expected"] = sqlstate.group(1)
                diagnostic["got"] = sqlstate.group(2)
            else:
                sqlstate = re.search(
                    r"expected SQLSTATE ([0-9A-Z]{5}) rejection, statement succeeded",
                    message,
                )
                if sqlstate:
                    diagnostic["category"] = "sqlstate"
                    diagnostic["expected"] = sqlstate.group(1)
                    diagnostic["got"] = "success"
            diagnostics.append(diagnostic)
            if len(diagnostics) >= 10:
                break
        if not diagnostics:
            diagnostics.append(
                {"test": "", "location": "", "category": "test-output"}
            )
        return diagnostics

    def _parse_go_test_results(
        self, stdout: str, stderr: str, exit_code: int
    ) -> dict[str, Any]:
        return ci_checks.parse_go_test_json(
            stdout,
            stderr,
            exit_code,
            EXPECTED_PACKAGE_PATHS,
            REQUIRED_TEST_PACKAGES,
            allowed_no_test_files=(),
        )

    def _parse_required_test_results(
        self, stdout: str, stderr: str = "", exit_code: int = 0
    ) -> tuple[dict[str, str], list[str]]:
        parsed = self._parse_go_test_results(stdout, stderr, exit_code)
        return parsed["required_test_results"], parsed["problems"]

    def _parse_test_output(
        self, stdout: str, stderr: str, exit_code: int
    ) -> tuple[dict[str, dict[str, str]], list[str]]:
        parsed = self._parse_go_test_results(stdout, stderr, exit_code)
        results: dict[str, dict[str, str]] = {}
        for name, package in zip(PACKAGES, EXPECTED_PACKAGE_PATHS):
            value = parsed["package_results"].get(package, {"status": "missing"})
            results[name] = (
                dict(value) if isinstance(value, dict) else {"status": str(value)}
            )
        return results, parsed["problems"]

    def _go_test_supervision(self) -> dict[str, Any] | None:
        """Reuse the actual go test command record; never fabricate timing."""
        for command in reversed(self.commands):
            if command.get("name") != "go test integration":
                continue
            timeout = command.get("timeout_seconds")
            elapsed = command.get("duration_seconds")
            timed_out = command.get("timed_out")
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                return None
            if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
                return None
            if not isinstance(timed_out, bool):
                return None
            timeout_value = float(timeout)
            elapsed_value = float(elapsed)
            if not math.isfinite(timeout_value) or timeout_value <= 0:
                return None
            if not math.isfinite(elapsed_value) or elapsed_value < 0:
                return None
            return {
                "timeout_seconds": timeout_value,
                "wait_elapsed_seconds": elapsed_value,
                "timed_out": timed_out,
            }
        return None

    def _record_go_test_results(
        self, parsed: Mapping[str, Any], exit_code: int
    ) -> None:
        """Record only whitelisted go-test results for normal and timeout paths."""
        self.tests["test_exit"] = exit_code
        results: dict[str, dict[str, str]] = {}
        for name, package in zip(PACKAGES, EXPECTED_PACKAGE_PATHS):
            value = parsed["package_results"].get(package, {"status": "missing"})
            results[name] = (
                dict(value) if isinstance(value, dict) else {"status": str(value)}
            )
        self.tests["package_results"] = results
        self.tests["required_test_results"] = parsed["required_test_results"]
        self.tests["test_root_count"] = parsed["root_count"]
        self.tests["test_subtest_count"] = parsed["subtest_count"]
        problems = list(parsed["problems"])
        if exit_code != 0 or problems:
            self.tests["failure_diagnostics"] = parsed["failure_diagnostics"]
            self.tests["problems"] = problems
        self.tests.pop("test_supervision", None)
        supervision = self._go_test_supervision()
        if supervision is not None:
            self.tests["test_supervision"] = supervision

    def run_tests(self) -> None:
        if self.published_port is None:
            raise LauncherError("environment", "published-port", "published port is unavailable")
        host_dsn = make_dsn(
            "127.0.0.1",
            self.published_port,
            self.database,
            MOCK_DB_USER,
            MOCK_DB_PASSWORD,
        )
        test_env = dict(self.offline_go_env)
        test_env["PAYMENT_HUB_TEST_DATABASE_URL"] = host_dsn
        test_env["PAYMENT_HUB_TEST_RUN_ID"] = self.run_id
        test_env["PAYMENT_HUB_TEST_ARTIFACT_ROOT"] = str(
            self.artifact_root
        )

        package_args = [f"./{name}" for name in PACKAGES]
        vet = self._run_process_group(
            "test",
            "vet",
            "go vet integration",
            [
                self.go,
                "vet",
                "-mod=readonly",
                "-tags=integration",
                "-p=1",
                *package_args,
            ],
            test_env,
            BUDGETS["go_vet_seconds"],
            allow_failure=True,
        )
        self.tests["vet_exit"] = vet.returncode
        if vet.returncode != 0:
            self.tests["vet_diagnostics"] = self._extract_diagnostics(
                (vet.stdout or "") + "\n" + (vet.stderr or "")
            )
            raise LauncherError(
                "test", "vet-failed", f"go vet exited {vet.returncode}"
            )

        test = self._run_process_group(
            "test",
            "test",
            "go test integration",
            [
                self.go,
                "test",
                "-mod=readonly",
                "-tags=integration",
                "-race",
                "-count=1",
                "-p=1",
                f"-timeout={GO_TEST_PACKAGE_TIMEOUT_SECONDS}s",
                "-json",
                *package_args,
            ],
            test_env,
            BUDGETS["go_test_external_seconds"],
            allow_failure=True,
        )
        parsed = self._parse_go_test_results(
            test.stdout or "", test.stderr or "", test.returncode
        )
        self._record_go_test_results(parsed, test.returncode)
        problems = parsed["problems"]
        if test.returncode != 0 or problems:
            kind = "test-failed" if test.returncode != 0 else "package-result"
            raise LauncherError(
                "test",
                kind,
                f"go test exited {test.returncode} with {len(problems)} package/protocol problems",
            )

    def verify_fixture_cleanup(self) -> None:
        if self.container_id is None:
            raise LauncherError(
                "test", "fixture-count", "postgres container is unavailable"
            )
        exec_env = dict(self.docker_env)
        exec_env.update(
            {
                "PGUSER": MOCK_DB_USER,
                "PGPASSWORD": MOCK_DB_PASSWORD,
                "PGDATABASE": self.database,
            }
        )
        argv = [
            self.docker or "docker",
            "exec",
            "-i",
            "--env",
            "PGUSER",
            "--env",
            "PGPASSWORD",
            "--env",
            "PGDATABASE",
            self.container_id,
            "psql",
            "-X",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            "-",
        ]
        result = self._run_with_stdin(
            "test",
            "fixture-count",
            "fixture rowcounts",
            argv,
            exec_env,
            FIXTURE_COUNT_QUERY,
            BUDGETS["fixture_count_seconds"],
            allow_failure=True,
        )
        self.tests["fixture_exit"] = result.returncode
        if result.returncode != 0:
            raise LauncherError(
                "test", "fixture-count", "fixture rowcount query failed"
            )
        lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        counts = [int(line) if line.isdigit() else None for line in lines]
        self.tests["fixture_rowcounts"] = counts
        if counts != [0, 0, 0]:
            raise LauncherError(
                "test", "fixture-residue", "fixture rows were not fully rolled back"
            )
        self.tests["fixture_cleanup"] = "pass"

    # ------------------------------------------------------------------
    # Cleanup and report
    # ------------------------------------------------------------------

    def _list_ids(self, argv: Sequence[str], stage: str, kind: str) -> list[str]:
        result = self._run(
            stage,
            kind,
            kind,
            argv,
            self.docker_env,
            BUDGETS["docker_version_seconds"],
            allow_failure=False,
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _collect_compose_resources(self) -> dict[str, Any]:
        project_filter = f"label=com.docker.compose.project={self.project}"
        containers = self._list_ids(
            [
                self.docker or "docker",
                "ps",
                "-a",
                "--filter",
                project_filter,
                "--format",
                "{{.ID}}",
            ],
            "cleanup",
            "compose-containers",
        )
        networks = self._list_ids(
            [
                self.docker or "docker",
                "network",
                "ls",
                "--filter",
                project_filter,
                "--format",
                "{{.Name}}",
            ],
            "cleanup",
            "compose-networks",
        )
        volumes = self._list_ids(
            [
                self.docker or "docker",
                "volume",
                "ls",
                "--filter",
                project_filter,
                "--format",
                "{{.Name}}",
            ],
            "cleanup",
            "compose-volumes",
        )
        self.compose_created = {
            "containers": containers,
            "networks": networks,
            "network": self.network_name if self.network_name in networks else None,
            "volumes": volumes,
            "volume": self.volume_name if self.volume_name in volumes else None,
        }
        if len(networks) > 1:
            self.cleanup_failures.append("cleanup:network-ambiguous")
        elif networks and self.network_name not in networks:
            self.cleanup_failures.append("cleanup:network-identity")
        if len(volumes) > 1:
            self.cleanup_failures.append("cleanup:volume-ambiguous")
        elif volumes and self.volume_name not in volumes:
            self.cleanup_failures.append("cleanup:volume-identity")
        return self.compose_created

    def _exact_volume_names(self) -> list[str]:
        return self._list_ids(
            [
                self.docker or "docker",
                "volume",
                "ls",
                "--filter",
                f"name=^{self.volume_name}$",
                "--format",
                "{{.Name}}",
            ],
            "cleanup",
            "volume-list",
        )

    def _exact_network_names(self) -> list[str]:
        return self._list_ids(
            [
                self.docker or "docker",
                "network",
                "ls",
                "--filter",
                f"name=^{self.network_name}$",
                "--format",
                "{{.Name}}",
            ],
            "cleanup",
            "network-list",
        )

    def _remove_owned_volume(self, result: dict[str, Any]) -> None:
        names = self._exact_volume_names()
        if not names:
            result["volume_absent"] = True
            return
        if names != [self.volume_name]:
            self.cleanup_failures.append("cleanup:volume-identity")
            return
        inspect = self._run(
            "cleanup",
            "volume-inspect",
            "volume inspect",
            [
                self.docker or "docker",
                "volume",
                "inspect",
                "--format",
                "{{json .}}",
                self.volume_name,
            ],
            self.docker_env,
            BUDGETS["volume_remove_seconds"],
            allow_failure=False,
        )
        try:
            volume = json.loads(inspect.stdout)
        except json.JSONDecodeError:
            self.cleanup_failures.append("cleanup:volume-inspect")
            return
        if not isinstance(volume, dict):
            self.cleanup_failures.append("cleanup:volume-inspect")
            return
        labels = volume.get("Labels") or {}
        if (
            volume.get("Name") != self.volume_name
            or labels.get("com.docker.compose.project") != self.project
        ):
            self.cleanup_failures.append("cleanup:volume-identity")
            return
        removed = self._run(
            "cleanup",
            "volume-remove",
            "volume remove",
            [self.docker or "docker", "volume", "rm", self.volume_name],
            self.docker_env,
            BUDGETS["volume_remove_seconds"],
            allow_failure=True,
        )
        if removed.returncode != 0:
            self.cleanup_failures.append("cleanup:volume-remove")
            return
        remaining = self._exact_volume_names()
        if remaining:
            self.cleanup_failures.append("cleanup:volume-remains")
        else:
            result["volume_removed"] = True

    def cleanup_safely(self) -> None:
        result: dict[str, Any] = {
            "result": "pass",
            "tool_containers": [],
            "compose_down_exit": None,
            "network_removed": False,
            "volume_name": self.volume_name,
            "volume_retained": False,
            "volume_removed": False,
            "repeat_signal_ignored": self.repeat_signal,
            "errors": [],
        }
        if self.docker is None:
            result["result"] = "skipped"
            result["reason"] = "docker-not-initialized"
            result["repeat_signal_ignored"] = self.repeat_signal
            self.cleanup = result
            return

        for record in self.tool_containers:
            if record.get("removed"):
                continue
            try:
                if not record.get("id"):
                    self._recover_intended_container(record)
                if record.get("id"):
                    self._stop_remove_container(record["id"], "cleanup", record)
            except Exception:
                self.cleanup_failures.append("cleanup:tool-container")
        result["tool_containers"] = [
            {
                "stage": item.get("stage"),
                "name": item.get("name"),
                "id": item.get("id"),
                "container_exit_code": item.get("container_exit_code"),
                "wait_client_exit": item.get("wait_client_exit"),
                "removed": bool(item.get("removed")),
                "absent": bool(item.get("absent")),
            }
            for item in self.tool_containers
        ]

        if self.compose_attempted:
            try:
                self._collect_compose_resources()
            except Exception:
                self.cleanup_failures.append("cleanup:compose-collect")

            try:
                down = self._run(
                    "cleanup",
                    "compose-down",
                    "compose down",
                    self._compose_argv("down"),
                    self.compose_env,
                    BUDGETS["compose_down_seconds"],
                    allow_failure=True,
                )
                result["compose_down_exit"] = down.returncode
                if down.returncode != 0:
                    self.cleanup_failures.append("cleanup:compose-down")
            except Exception:
                self.cleanup_failures.append("cleanup:compose-down")

            try:
                containers = self._list_ids(
                    [
                        self.docker or "docker",
                        "ps",
                        "-a",
                        "--filter",
                        f"label=com.docker.compose.project={self.project}",
                        "--format",
                        "{{.ID}}",
                    ],
                    "cleanup",
                    "cleanup-containers",
                )
                if containers:
                    self.cleanup_failures.append("cleanup:containers-remain")
                networks = self._exact_network_names()
                if networks:
                    self.cleanup_failures.append("cleanup:network-remains")
                else:
                    result["network_removed"] = True
            except Exception:
                self.cleanup_failures.append("cleanup:network-verify")

        if self.args.cleanup_owned_resources:
            recorded_volume = self.compose_created.get("volume") == self.volume_name
            try:
                if recorded_volume:
                    self._remove_owned_volume(result)
                else:
                    names = self._exact_volume_names()
                    if names:
                        self.cleanup_failures.append("cleanup:volume-not-recorded")
                    else:
                        result["volume_absent"] = True
            except Exception:
                self.cleanup_failures.append("cleanup:volume")
        else:
            result["volume_retained"] = True

        if self.repeat_signal and self.failure is None:
            self.failure = {
                "stage": "interrupted",
                "kind": "repeat-signal",
                "message": "signal received during cleanup; cleanup continued",
            }
        result["errors"] = sorted(set(self.cleanup_failures))
        result["repeat_signal_ignored"] = self.repeat_signal
        result["result"] = "fail" if self.cleanup_failures else "pass"
        self.cleanup = result

    def _result_pass(self) -> bool:
        return (
            self.failure is None
            and not self.cleanup_failures
            and self.artifact_written
            and self.required_complete
        )

    def _build_report(self) -> dict[str, Any]:
        report_result = (
            "pass"
            if self.failure is None
            and not self.cleanup_failures
            and self.required_complete
            else "fail"
        )
        return {
            "schema": "payment-hub.db-foundation.v1",
            "scope": self.scope,
            "run_id": self.run_id,
            "project": self.project,
            "platform": EXPECTED_PLATFORM,
            "started_at": self.started_at,
            "finished_at": utc_now(),
            "result": report_result,
            "failure": self.failure,
            "source": self.source,
            "images": self.images,
            "versions": self.versions,
            "budgets": BUDGETS,
            "commands": self.commands,
            "module": self.module,
            "preflight": self.preflight,
            "compose_created": self.compose_created,
            "pg": self.pg,
            "migration": self.migration,
            "negative_dsn": self.negative,
            "safety_entry": self.safety_entry,
            "tests": self.tests,
            "cleanup": self.cleanup,
            "artifact": str(self.report_path),
        }

    def write_report_safely(self) -> None:
        if self.cleanup_failures and self.failure is None:
            self.failure = {
                "stage": "cleanup",
                "kind": "cleanup-failed",
                "message": "owned resource cleanup failed",
            }
        if self.repeat_signal and self.failure is None:
            self.failure = {
                "stage": "interrupted",
                "kind": "repeat-signal",
                "message": "signal received during cleanup; cleanup continued",
            }
        data = self._build_report()
        try:
            with self.report_path.open("x", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
        except (OSError, TypeError, ValueError):
            self.artifact_written = False
            if self.failure is None:
                self.failure = {
                    "stage": "artifact",
                    "kind": "write",
                    "message": "could not write artifact report",
                }
            print("ERROR: artifact/write", file=sys.stderr)
            return
        self.report_data = data
        self.artifact_written = True

    def print_summary(self) -> None:
        if "go" in self.versions:
            print(f"go-version: {self.versions['go']}")
        if "postgres" in self.versions:
            print(f"postgres-version: {self.versions['postgres']}")
        if "goose" in self.versions:
            print(f"goose-version: {self.versions['goose']}")
        package_results = self.tests.get("package_results")
        if isinstance(package_results, dict):
            for name in PACKAGES:
                if name not in package_results:
                    continue
                result = package_results[name]
                if isinstance(result, dict):
                    status = result.get("status", "unknown")
                    duration = result.get("duration")
                    suffix = f" duration={duration}" if duration else ""
                    print(f"package-result: {name}={status}{suffix}")
                else:
                    print(f"package-result: {name}={result}")
        required_results = self.tests.get("required_test_results")
        if isinstance(required_results, dict):
            for name in REQUIRED_TEST_PACKAGES:
                if name in required_results:
                    print(f"required-test-result: {name}={required_results[name]}")
        print(f"cleanup-result: {self.cleanup.get('result', 'unknown')}")
        if self.artifact_written:
            print(f"artifact-report: {self.report_path}")
        if self._result_pass():
            print("result: pass")
        else:
            failure = self.failure or {"stage": "unknown", "kind": "unknown"}
            print(f"result: fail stage={failure['stage']} kind={failure['kind']}")
        if self.report_data is not None:
            print(
                "db-foundation-report: "
                + json.dumps(self.report_data, separators=(",", ":"), sort_keys=True)
            )
        else:
            minimal = {
                "run_id": self.run_id,
                "result": "fail",
                "failure": self.failure,
            }
            print(
                "db-foundation-report: "
                + json.dumps(minimal, separators=(",", ":"), sort_keys=True)
            )

    def run(self) -> None:
        print(f"scope: {self.scope}")
        print(f"run-id: {self.run_id}")
        self.check_go()
        self.check_modules()
        self.collect_source_manifest()
        self.check_docker()
        self.check_compose_model()
        self.preflight_resources()
        self.prepare_images()
        self.verify_migration_image()
        self.run_safety_entry_rejections()
        self.run_negative_dsn()
        self.start_compose()
        self.wait_pg()
        self.run_migrations()
        self.run_tests()
        self.verify_source_manifest()
        self.verify_fixture_cleanup()
        self.required_complete = True

    def finish(self) -> None:
        self.cleanup_started = True
        self.cleanup_safely()
        self.write_report_safely()
        self.print_summary()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "One-shot database foundation and wired runtime harness launcher: "
            "fixed-input migration image, isolated PostgreSQL Compose project, real "
            "Goose up/up-again, integration vet/test with required commit-hook "
            "experiment, trusted private-UDS observer/helper two-generation parent "
            "retention, and current real application-binary readyz/SIGTERM/"
            "bad-config/missing-migration/startup-timeout/startup-cancel/"
            "startup-listen diagnostics, and owned cleanup only. Default packages "
            "cover private UDS, readyz/lifecycle short real-connection semantics, "
            "explicit empty-Env child isolation, post-leader-exit cleanup deadlines, "
            "bounded startup diagnostic logs, safe release after failed Seal, "
            "immutable final snapshot archiving, helper self-tests, first-batch "
            "storage/mock core paths, order lookup, US1 real payment HTTP/"
            "byte-boundary/restart integration, and US2 same-intent replay/"
            "conflict/concurrency/restart idempotency integration. Not payment "
            "HTTP beyond US1/US2, application image, the T072 "
            "full check, T077, or CD."
        )
    )
    parser.add_argument(
        "--go",
        required=True,
        help="Absolute path to the existing Go 1.27.3 executable; never installs.",
    )
    parser.add_argument(
        "--cache-dir",
        required=True,
        help=(
            "Absolute project-specific cache directory outside the repository; "
            "config/gomod/gobuild/gopath are created there."
        ),
    )
    parser.add_argument(
        "--prepare-images",
        action="store_true",
        help="Explicitly allow missing base-image pulls and migration-image build.",
    )
    parser.add_argument(
        "--download-modules",
        action="store_true",
        help=(
            "Explicitly allow go mod download all via proxy.golang.org and "
            "sum.golang.org; otherwise module operations are readonly/offline."
        ),
    )
    parser.add_argument(
        "--cleanup-owned-resources",
        action="store_true",
        help=(
            "After normal stop, delete only the exact volume recorded as created "
            "by this run after re-checking its Compose project label."
        ),
    )
    parser.add_argument(
        "--artifact-root",
        default=str(REPO_ROOT / "artifacts"),
        help=(
            "Artifact base directory; results go to <root>/<run_id>/db-foundation.json. "
            "Relative values are resolved against the repository root."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        launcher = Launcher(args)
    except LauncherError as exc:
        print(f"ERROR: {exc.stage}/{exc.kind}", file=sys.stderr)
        print(f"result: fail stage={exc.stage} kind={exc.kind}")
        return 1
    except Exception:
        print("ERROR: internal/init", file=sys.stderr)
        print("result: fail stage=internal kind=init")
        return 1

    def handle_signal(signum: int, _frame: Any) -> None:
        if (
            launcher.cleanup_started
            or not launcher.running
            or launcher.failure is not None
        ):
            launcher.repeat_signal = True
            return
        raise LauncherInterrupted(
            "sigterm" if signum == signal.SIGTERM else "sigint"
        )

    old_int = signal.signal(signal.SIGINT, handle_signal)
    old_term = signal.signal(signal.SIGTERM, handle_signal)
    try:
        launcher.running = True
        try:
            launcher.run()
        except LauncherInterrupted as exc:
            launcher.failure = {
                "stage": "interrupted",
                "kind": exc.kind,
                "message": "controlled interruption",
            }
        except KeyboardInterrupt:
            launcher.failure = {
                "stage": "interrupted",
                "kind": "sigint",
                "message": "controlled interruption",
            }
        except LauncherError as exc:
            launcher.failure = {
                "stage": exc.stage,
                "kind": exc.kind,
                "message": exc.message,
            }
        except Exception:
            launcher.failure = {
                "stage": "internal",
                "kind": "unexpected",
                "message": "internal launcher failure",
            }
        finally:
            launcher.running = False
            launcher.cleanup_started = True
            try:
                launcher.finish()
            except Exception:
                print("ERROR: internal/finish", file=sys.stderr)
                return 1
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
    return 0 if launcher._result_pass() else 1


if __name__ == "__main__":
    sys.exit(main())
