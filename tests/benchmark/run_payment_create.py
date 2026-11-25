#!/usr/bin/env python3
"""Repeatable local 200 RPS benchmark for POST /api/v1/payments.

Fixed CLI:
  python3 tests/benchmark/run_payment_create.py \
    --migrate-image <local image> [--k6 <local k6>] [--go <local go>] \
    [--artifacts-dir <outside-repo directory>]

The run is fixed at three sequential rounds, each with a fresh Compose project,
database, volume and host ports. Each round runs a 2 RPS x 5 s smoke stage, then
a 50 RPS x 20 s warmup on the real payment path, then the measured
200 RPS x 120 s stage with 128 pre-allocated/max VUs. Warmup must pass before
the measured stage starts. No stage is retried. The runner never downloads k6
or images, never uses the host 5432 port, and never removes a dedicated
PostgreSQL volume. Evidence is written outside the repository.

The application is built from the current worktree with the explicit
`paymenthub_benchmark` build tag, which pins pgxpool MaxConns=16 and emits one
startup POOL_CONFIG line. The default build is unchanged.

CPU placement is fixed: k6 on guest CPUs 0-1, PostgreSQL on 2-5, app on 6-9.
These are complete WSL2 sibling pairs. The runner refuses to start a controlled
stage when the effective masks do not match. This is guest affinity, not host
physical isolation.
"""
import argparse
import hashlib
import http.client
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cpu_plan import (
    CpuPlanError,
    DEFAULT_GROUPS,
    build_plan as build_cpu_plan,
    clock_ticks_per_second,
    format_cpu_set,
    parse_cpu_set,
    read_process_affinity,
    read_process_cpu_ticks,
    verify_taskset_child,
)

REPO = Path(__file__).resolve().parents[2]
COMPOSE_BASE = REPO / "deploy" / "compose.yaml"
COMPOSE_TEST = REPO / "deploy" / "compose.test.yaml"
COMPOSE_BENCHMARK = REPO / "tests" / "benchmark" / "compose.benchmark.yaml"
COMPOSE_APP = REPO / "deploy" / "compose.app.yaml"
MIGRATIONS = REPO / "migrations"
K6_JS = REPO / "tests" / "benchmark" / "payment_create.js"
SQL_VERIFY = REPO / "tests" / "benchmark" / "verify_payment_create.sql"
DEFAULT_ARTIFACTS_ROOT = Path(
    os.environ.get("PAYMENT_HUB_BENCHMARK_ARTIFACTS_DIR", "/tmp/opencode/payment-hub-benchmark")
)
DOCKER_SOCKET = Path("/var/run/docker.sock")
DOCKER_HOST = "unix:///var/run/docker.sock"

PROFILE = "200"
TARGET_RATE = 200
SMOKE_RATE = 2
SMOKE_DURATION_S = 5
WARMUP_RATE = 50
WARMUP_DURATION_S = 20
LOAD_DURATION_S = 120
MAX_VUS = 128
SMOKE_PRE_VUS = 4
SMOKE_MAX_VUS = 8
WARMUP_PRE_VUS = MAX_VUS
WARMUP_MAX_VUS = MAX_VUS
REQUIRED_ROUNDS = 3
BENCHMARK_BUILD_TAG = "paymenthub_benchmark"
POOL_CONFIG_PREFIX = "POOL_CONFIG "
EXPECTED_MAX_CONNS = 16
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_SQL_OUTPUT_BYTES = 2 * 1024 * 1024
APP_SHUTDOWN_TIMEOUT_S = 30
APP_EXEC_TIMEOUT_S = 10.0
K6_EXEC_TIMEOUT_S = 10.0
K6_CPU_SAMPLE_INTERVAL_S = 1.0
K6_CPU_SATURATION_RATIO = 0.90
K6_CPUS = DEFAULT_GROUPS["k6"]
PG_CPUS = DEFAULT_GROUPS["postgres"]
APP_CPUS = DEFAULT_GROUPS["app"]
K6_REQUIRED_VERSION = "v2.4.0"


class PreconditionError(RuntimeError):
    """A required local tool, image, or file is unavailable."""


class RunError(RuntimeError):
    """The experiment ran but did not complete successfully."""


def validate_artifacts_root(run_root):
    if run_root == REPO or REPO in run_root.parents:
        raise PreconditionError(
            f"artifacts root must be outside the repository: {run_root}"
        )


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def free_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def free_non_5432_loopback_port():
    for _ in range(64):
        port = free_loopback_port()
        if port != 5432:
            return port
    raise PreconditionError("could not obtain a dynamic loopback port other than 5432")


def sanitized_env(extra=None):
    """Keep only harmless process basics; no inherited K6_/proxy/PG settings."""
    keep = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.setdefault("PATH", "/usr/bin:/bin")
    env.setdefault("HOME", "/tmp")
    env.setdefault("TMPDIR", "/tmp")
    env["TZ"] = "UTC"
    if extra:
        env.update(extra)
    return env


def sanitized_docker_env(extra=None):
    """Only a fixed local Unix Docker endpoint; no ambient Compose/Docker overrides."""
    env = sanitized_env(extra)
    env["DOCKER_HOST"] = DOCKER_HOST
    env.pop("DOCKER_CONTEXT", None)
    env.pop("DOCKER_TLS_VERIFY", None)
    env.pop("DOCKER_CERT_PATH", None)
    return env


def pg_env(host, port, database, user, password):
    return sanitized_env({
        "PGHOST": host,
        "PGPORT": str(port),
        "PGDATABASE": database,
        "PGUSER": user,
        "PGPASSWORD": password,
        "PGSSLMODE": "disable",
        "PGCONNECT_TIMEOUT": "5",
    })


def summarize_cpu_windows(cpu_ticks, attempts, clock_ticks, allowed_cpus):
    """Turn periodic /proc CPU samples into fixed-size 1 s window evidence."""
    windows = []
    for index in range(1, len(cpu_ticks)):
        previous_time, previous_total = cpu_ticks[index - 1]
        current_time, current_total = cpu_ticks[index]
        wall_s = current_time - previous_time
        if wall_s <= 0:
            continue
        cores = ((current_total - previous_total) / clock_ticks) / wall_s
        windows.append({
            "start_s": round(previous_time - cpu_ticks[0][0], 6),
            "end_s": round(current_time - cpu_ticks[0][0], 6),
            "cores": round(cores, 6),
        })
    missing_samples = max(0, attempts - len(cpu_ticks))
    peak_window_cores = max((window["cores"] for window in windows), default=None)
    saturated_windows = sum(
        1 for window in windows
        if window["cores"] >= K6_CPU_SATURATION_RATIO * allowed_cpus
    )
    available = len(cpu_ticks) >= 2 and bool(windows)
    sampling_status = "ok" if available and missing_samples == 0 else "unknown"
    if len(cpu_ticks) >= 2:
        first_time, first_ticks = cpu_ticks[0]
        last_time, last_ticks = cpu_ticks[-1]
        wall_s = max(0.0, last_time - first_time)
        cpu_s = (last_ticks - first_ticks) / clock_ticks
        avg_cores = cpu_s / wall_s if wall_s > 0 else None
    else:
        wall_s = None
        cpu_s = None
        avg_cores = None
    return {
        "windows": windows,
        "available": available,
        "sampling_status": sampling_status,
        "samples": len(cpu_ticks),
        "attempts": attempts,
        "missing_samples": missing_samples,
        "cpu_seconds": round(cpu_s, 6) if cpu_s is not None else None,
        "wall_seconds": round(wall_s, 6) if wall_s is not None else None,
        "avg_cores": round(avg_cores, 6) if avg_cores is not None else None,
        "window_count": len(windows),
        "peak_window_cores": peak_window_cores,
        "saturated_windows": saturated_windows,
        "allowed_cpus": allowed_cpus,
        "saturated": saturated_windows > 0,
        "saturation_ratio": K6_CPU_SATURATION_RATIO,
    }


class Runner:
    def __init__(self, args):
        self.args = args
        self.profile = PROFILE
        self.target_rate = TARGET_RATE
        self.rounds = REQUIRED_ROUNDS
        self.base_run_id = secrets.token_hex(6)
        self.run_id = self.base_run_id
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        artifacts_root = (
            Path(args.artifacts_dir).expanduser()
            if getattr(args, "artifacts_dir", None)
            else DEFAULT_ARTIFACTS_ROOT
        )
        artifacts_root = artifacts_root.resolve()
        self.run_root = artifacts_root / "load-runs" / f"{timestamp}-{self.base_run_id}"
        self.run_dir = self.run_root
        self.env_dir = self.run_root / "env"
        self.logs_dir = self.run_root / "logs"
        self.results_dir = self.run_root / "results"
        self.db_dir = self.run_root / "db"
        self.commands_log = self.run_root / "commands.log"
        self.summary = {}
        self.app_proc = None
        self.compose_env_path = self.env_dir / "compose.env"
        self.compose_started = False
        self.compose_project = None
        self.compose_network = None
        self.volume = None
        self.cleanup_warning = None
        self.cleanup_warnings = []
        self.stage_records = []
        self.round_records = []
        self.volumes = []
        self.started_at = utc_now()
        self.round_index = 0
        self.cpu_plan = None
        self.pool_config = None
        self.started_stages = set()
        self.app_binary = None
        self.app_sha256 = None
        self.migrate_image = None
        self.source_info = {}
        self.k6_path = None
        self.k6_version = None
        self.fatal_reason = None

    # ---------- logging / process helpers ----------

    def prepare_dirs(self):
        self.docker_config_dir = self.run_root / "docker-config"
        self.docker_home = self.run_root / "docker-home"
        self.k6_home = self.run_root / "k6-home"
        self.build_dir = self.run_root / "build"
        for path in (self.run_root, self.logs_dir, self.build_dir,
                     self.docker_config_dir, self.docker_home, self.k6_home):
            path.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
        os.chmod(self.run_dir, 0o700)
        self.log_command(f"# run started {self.started_at}")
        self.log_command(f"# repository {REPO}")

    def prepare_round_dirs(self):
        self.env_dir = self.run_dir / "env"
        self.logs_dir = self.run_dir / "logs"
        self.results_dir = self.run_dir / "results"
        self.db_dir = self.run_dir / "db"
        self.compose_env_path = self.env_dir / "compose.env"
        for path in (self.run_dir, self.env_dir, self.logs_dir,
                     self.results_dir, self.db_dir):
            path.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
        self.log_command(
            f"# round {self.round_index} started {utc_now()} "
            f"profile={self.profile} target_rps={self.target_rate}"
        )

    def log_command(self, line):
        with open(self.commands_log, "a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")

    @staticmethod
    def cap_log(path, max_bytes=MAX_LOG_BYTES):
        """Keep a bounded head+tail sample and record that truncation happened."""
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size <= max_bytes:
            return None
        keep_head = max_bytes // 4
        keep_tail = max_bytes - keep_head
        try:
            with open(path, "rb") as source:
                head = source.read(keep_head)
                source.seek(-keep_tail, os.SEEK_END)
                tail = source.read(keep_tail)
            marker = (
                f"\n[TRUNCATED original_bytes={size} kept_head={keep_head} "
                f"kept_tail={keep_tail} cap_bytes={max_bytes}]\n"
            ).encode("ascii")
            with open(path, "wb") as target:
                target.write(head)
                target.write(marker)
                target.write(tail)
        except OSError:
            return None
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return {
            "path": str(path),
            "original_bytes": size,
            "kept_bytes": keep_head + len(marker) + keep_tail,
            "max_bytes": max_bytes,
        }

    @staticmethod
    def tree_bytes(path):
        total = 0
        try:
            for root, _directories, files in os.walk(path):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        pass
        except OSError:
            return total
        return total

    def docker_env(self):
        return sanitized_docker_env({
            "DOCKER_CONFIG": str(self.docker_config_dir),
            "HOME": str(self.docker_home),
        })

    def validate_docker_endpoint(self):
        ambient_host = os.environ.get("DOCKER_HOST", "")
        ambient_context = os.environ.get("DOCKER_CONTEXT", "")
        if ambient_context:
            raise PreconditionError(
                f"DOCKER_CONTEXT is set ({ambient_context!r}); only the fixed local "
                f"Unix socket {DOCKER_HOST} is supported"
            )
        if ambient_host and ambient_host.rstrip("/") != DOCKER_HOST:
            raise PreconditionError(
                f"DOCKER_HOST={ambient_host!r} is not the fixed local Unix socket "
                f"{DOCKER_HOST}; remote/custom Docker endpoints are rejected"
            )
        try:
            mode = DOCKER_SOCKET.stat().st_mode
        except OSError as exc:
            raise PreconditionError(
                f"local Docker socket {DOCKER_SOCKET} is unavailable; remote/non-Unix "
                f"Docker endpoints are not supported"
            ) from exc
        if not stat.S_ISSOCK(mode):
            raise PreconditionError(f"{DOCKER_SOCKET} is not a Unix socket")

    def run_cmd(self, cmd, log_name, *, env=None, cwd=None, check=True):
        self.log_command("$ " + shlex.join(str(part) for part in cmd))
        log_path = self.logs_dir / log_name
        with open(log_path, "ab") as log:
            proc = subprocess.run(
                [str(part) for part in cmd],
                cwd=str(cwd or REPO),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        if check and proc.returncode != 0:
            raise RunError(
                f"command failed with exit {proc.returncode}: {shlex.join(str(p) for p in cmd)}; "
                f"see {log_path}"
            )
        return proc

    def capture_cmd(self, cmd, *, env=None, cwd=None, timeout=60):
        self.log_command("$ " + shlex.join(str(part) for part in cmd))
        try:
            return subprocess.run(
                [str(part) for part in cmd],
                cwd=str(cwd or REPO),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RunError(f"command timed out: {shlex.join(str(p) for p in cmd)}") from exc

    # ---------- preflight ----------

    def resolve_executable(self, candidate, what):
        if os.path.sep not in candidate and shutil.which(candidate):
            candidate = shutil.which(candidate)
        path = Path(candidate).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise PreconditionError(f"{what} executable not found or not executable: {candidate}")
        return path.resolve()

    def resolve_k6(self):
        return self.resolve_executable(self.args.k6, "k6")

    def inspect_image(self, image, what):
        proc = self.capture_cmd(
            ["docker", "image", "inspect", image],
            env=self.docker_env(),
        )
        if proc.returncode != 0:
            raise PreconditionError(f"{what} image is not available locally: {image}")
        ident = self.capture_cmd(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            env=self.docker_env(),
        )
        if ident.returncode != 0 or not ident.stdout.strip():
            raise PreconditionError(f"cannot inspect {what} image id: {image}")
        labels = self.capture_cmd(
            ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", image],
            env=self.docker_env(),
        )
        try:
            labels_value = json.loads(labels.stdout.strip()) if labels.stdout.strip() else {}
        except json.JSONDecodeError:
            labels_value = {}
        digests = self.capture_cmd(
            ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image],
            env=self.docker_env(),
        )
        try:
            repo_digests = json.loads(digests.stdout.strip()) if digests.stdout.strip() else []
        except json.JSONDecodeError:
            repo_digests = []
        return {
            "image": image,
            "image_id": ident.stdout.strip(),
            "labels": labels_value,
            "repo_digests": repo_digests,
        }

    def verify_migration_image(self, image):
        source_files = sorted(MIGRATIONS.glob("*.sql"))
        if not source_files:
            raise PreconditionError(f"no current migration files under {MIGRATIONS}")
        verify_dir = self.run_dir / "verify-migrations"
        verify_dir.mkdir(mode=0o700, exist_ok=True)
        cid = self.capture_cmd(
            ["docker", "create", "--pull=never", image],
            env=self.docker_env(),
        )
        if cid.returncode != 0 or not cid.stdout.strip():
            raise PreconditionError(f"cannot create migration-inspection container from {image}")
        container_id = cid.stdout.strip()
        try:
            copy = self.capture_cmd(
                ["docker", "cp", f"{container_id}:/migrations/.", str(verify_dir)],
                env=self.docker_env(),
            )
            if copy.returncode != 0:
                raise PreconditionError(f"image {image} has no readable /migrations directory")
        finally:
            self.capture_cmd(
                ["docker", "rm", "-f", container_id],
                env=self.docker_env(),
            )
        image_files = sorted(verify_dir.glob("*.sql"))
        source_names = [p.name for p in source_files]
        image_names = [p.name for p in image_files]
        if source_names != image_names:
            raise PreconditionError(
                f"migration image files do not match current source: image={image_names} source={source_names}"
            )
        file_hashes = {}
        for source in source_files:
            image_file = verify_dir / source.name
            source_hash = sha256_file(source)
            image_hash = sha256_file(image_file)
            file_hashes[source.name] = {
                "source_sha256": source_hash,
                "image_sha256": image_hash,
                "matched": source_hash == image_hash,
            }
            if source_hash != image_hash:
                raise PreconditionError(
                    f"migration image content differs from current source: {source.name}"
                )
        return {
            "image": image,
            "files": file_hashes,
            "matched": True,
        }

    def read_pg_image(self):
        text = COMPOSE_BASE.read_text(encoding="utf-8")
        matches = re.findall(r"^\s*image:\s*(\S+)\s*$", text, re.MULTILINE)
        if not matches:
            raise PreconditionError(f"cannot find PostgreSQL image in {COMPOSE_BASE}")
        return matches[0]

    def preflight(self):
        validate_artifacts_root(self.run_root)
        try:
            self.cpu_plan = build_cpu_plan()
        except CpuPlanError as exc:
            raise PreconditionError(f"CPU placement plan is not valid: {exc}") from exc
        (self.run_root / "cpu-plan.json").write_text(
            json.dumps(self.cpu_plan, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.validate_docker_endpoint()
        docker_version = self.capture_cmd(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            env=self.docker_env(),
        )
        if docker_version.returncode != 0:
            raise PreconditionError(
                f"fixed local Docker socket {DOCKER_HOST} is not usable"
            )
        self.docker_server_version = docker_version.stdout.strip()
        compose_version = self.capture_cmd(
            ["docker", "compose", "version"],
            env=self.docker_env(),
        )
        if compose_version.returncode != 0:
            raise PreconditionError(
                "docker compose plugin is not available in the isolated Docker environment"
            )
        docker_ncpu = self.capture_cmd(
            ["docker", "info", "--format", "{{.NCPU}}"],
            env=self.docker_env(),
        )
        if docker_ncpu.returncode != 0:
            raise PreconditionError("cannot read Docker daemon CPU count")
        try:
            self.docker_ncpu = int(docker_ncpu.stdout.strip())
        except ValueError as exc:
            raise PreconditionError(
                f"Docker daemon CPU count is not numeric: {docker_ncpu.stdout.strip()}"
            ) from exc
        self.guest_ncpu = os.cpu_count() or 0
        if self.docker_ncpu != self.guest_ncpu:
            raise PreconditionError(
                "Docker daemon CPU domain "
                f"({self.docker_ncpu}) differs from guest CPUs ({self.guest_ncpu}); "
                "container cpuset would not isolate PostgreSQL on the same guest"
            )
        self.k6_path = self.resolve_k6()
        k6_version = self.capture_cmd([self.k6_path, "version"], env=sanitized_env())
        if k6_version.returncode != 0:
            raise PreconditionError(f"k6 version check failed: {self.k6_path}")
        self.k6_version = (k6_version.stdout or k6_version.stderr).strip()
        if not re.match(rf"^k6 {re.escape(K6_REQUIRED_VERSION)}(\s|$)", self.k6_version):
            raise PreconditionError(
                f"k6 version must be {K6_REQUIRED_VERSION}, got: {self.k6_version}"
            )
        self.go_path = self.resolve_executable(self.args.go, "go")
        go_version = self.capture_cmd([self.go_path, "version"], env=sanitized_env())
        if go_version.returncode != 0:
            raise PreconditionError(f"go version check failed: {self.go_path}")
        self.go_version = (go_version.stdout or go_version.stderr).strip()
        for tool in ("docker", "psql", "taskset"):
            if not shutil.which(tool):
                raise PreconditionError(f"required executable not found in PATH: {tool}")

        if not self.args.migrate_image:
            raise PreconditionError("--migrate-image is required for a load run")
        self.migrate_image = self.args.migrate_image
        self.migrate_image_info = self.inspect_image(self.migrate_image, "migration")
        self.migration_check = self.verify_migration_image(self.migrate_image)
        self.migration_check["image_id"] = self.migrate_image_info["image_id"]
        self.migration_check["labels"] = self.migrate_image_info["labels"]
        self.migration_check["repo_digests"] = self.migrate_image_info["repo_digests"]

        self.pg_image = self.read_pg_image()
        self.pg_image_info = self.inspect_image(self.pg_image, "PostgreSQL")

        for path, what in (
            (COMPOSE_BASE, "compose base"),
            (COMPOSE_TEST, "compose test overlay"),
            (COMPOSE_BENCHMARK, "compose benchmark overlay"),
            (COMPOSE_APP, "compose app overlay"),
            (K6_JS, "k6 script"),
            (SQL_VERIFY, "SQL verifier"),
        ):
            if not path.is_file():
                raise PreconditionError(f"missing {what}: {path}")

        source = self.capture_cmd(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            env=sanitized_env(),
        )
        branch = self.capture_cmd(
            ["git", "-C", str(REPO), "branch", "--show-current"],
            env=sanitized_env(),
        )
        status = self.capture_cmd(
            ["git", "-C", str(REPO), "status", "--porcelain"],
            env=sanitized_env(),
        )
        dirty_lines = (
            [line for line in status.stdout.splitlines() if line.strip()]
            if status.returncode == 0
            else []
        )
        self.source_info = {
            "kind": "git_worktree",
            "path": str(REPO),
            "sha": source.stdout.strip() if source.returncode == 0 else "",
            "branch": branch.stdout.strip() if branch.returncode == 0 else "",
            "dirty": bool(dirty_lines) if status.returncode == 0 else None,
            "dirty_count": len(dirty_lines) if status.returncode == 0 else None,
            "dirty_entries": dirty_lines[:100],
        }
        self.source_sha = self.source_info.get("sha", "")
        self.source_branch = self.source_info.get("branch", "")
        self.source_dirty = self.source_info.get("dirty")

    # ---------- isolated environment ----------

    def create_environment(self):
        self.pg_port = free_non_5432_loopback_port()
        if self.pg_port == 5432:
            raise PreconditionError("host PostgreSQL port 5432 is forbidden")
        self.app_port = free_loopback_port()
        while self.app_port == self.pg_port or self.app_port == 5432:
            self.app_port = free_loopback_port()
        self.compose_project = f"payment-hub-load-{self.run_id}"
        self.compose_network = f"{self.compose_project}_default"
        self.volume = f"{self.compose_project}_postgres_data"
        self.db_name = f"payment_load_{self.run_id}"
        self.db_user = f"load_{self.run_id}"
        self.db_password = secrets.token_hex(24)
        self.client_id = f"load-client-{self.run_id}"
        self.api_key = secrets.token_urlsafe(36)
        self.socket_dir = self.run_dir / "socket-unused"
        self.socket_dir.mkdir(mode=0o700, exist_ok=True)

        compose_lines = {
            "COMPOSE_PROJECT_NAME": self.compose_project,
            "PAYMENT_HUB_POSTGRES_DB": self.db_name,
            "PAYMENT_HUB_POSTGRES_USER": self.db_user,
            "PAYMENT_HUB_POSTGRES_PASSWORD": self.db_password,
            "PAYMENT_HUB_TEST_PG_PORT": str(self.pg_port),
            "PAYMENT_HUB_TEST_PG_CPUSET": PG_CPUS,
            "PAYMENT_HUB_APP_IMAGE": self.migrate_image,
            "PAYMENT_HUB_MIGRATE_IMAGE": self.migrate_image,
            "PAYMENT_HUB_CLIENT_ID": self.client_id,
            "PAYMENT_HUB_API_KEY": self.api_key,
            "PAYMENT_HUB_DATABASE_URL": (
                f"postgres://{self.db_user}:{self.db_password}@postgres:5432/"
                f"{self.db_name}?sslmode=disable"
            ),
            "PAYMENT_HUB_TEST_HTTP_PORT": str(self.app_port),
            "PAYMENT_HUB_TEST_UID": str(os.getuid()),
            "PAYMENT_HUB_TEST_GID": str(os.getgid()),
            "PAYMENT_HUB_TEST_SOCKET_DIR": str(self.socket_dir),
            "PAYMENT_HUB_TEST_RUN_ID": "",
            "PAYMENT_HUB_TEST_OBSERVER_EPOCH": "",
            "PAYMENT_HUB_TEST_GENERATION": "",
            "PAYMENT_HUB_TEST_CONTROL_VERSION": "",
        }
        self.compose_env_path.write_text(
            "".join(f"{key}={value}\n" for key, value in compose_lines.items()),
            encoding="utf-8",
        )
        os.chmod(self.compose_env_path, 0o600)

        self.app_dsn = (
            f"postgres://{self.db_user}:{self.db_password}@127.0.0.1:{self.pg_port}/"
            f"{self.db_name}?sslmode=disable"
        )
        self.app_env = sanitized_env({
            "PAYMENT_HUB_CLIENT_ID": self.client_id,
            "PAYMENT_HUB_API_KEY": self.api_key,
            "PAYMENT_HUB_DATABASE_URL": self.app_dsn,
            "PAYMENT_HUB_HTTP_ADDR": f"127.0.0.1:{self.app_port}",
            "PAYMENT_HUB_TEST_MODE": "off",
            # No pool diagnostic or COMMIT-tracing environment is set.
        })
        self.psql_env = pg_env(
            "127.0.0.1", self.pg_port, self.db_name, self.db_user, self.db_password
        )

    def compose_cmd(self, with_app=False, profile_migrate=False):
        cmd = [
            "docker", "compose",
            "--env-file", str(self.compose_env_path),
            "-f", str(COMPOSE_BASE),
            "-f", str(COMPOSE_TEST),
            "-f", str(COMPOSE_BENCHMARK),
        ]
        if with_app:
            cmd += ["-f", str(COMPOSE_APP)]
        cmd += ["-p", self.compose_project]
        if profile_migrate:
            cmd += ["--profile", "migrate"]
        return cmd

    def build_app(self):
        self.app_binary = self.build_dir / "payment-hub"
        self.run_cmd(
            [
                str(self.go_path), "build", "-mod=readonly", "-trimpath",
                "-tags", BENCHMARK_BUILD_TAG,
                "-o", str(self.app_binary), "./cmd/payment-hub",
            ],
            "go-build.log",
            env=sanitized_env({
                "GOWORK": "off",
                "GOPROXY": "off",
                "GOSUMDB": "off",
                "GOTOOLCHAIN": "local",
                # Keep the build cache inside this isolated run tree instead of
                # writing to the invoking user's cache directory.
                "GOCACHE": str(self.build_dir / "gocache"),
            }),
            cwd=REPO,
        )
        self.app_sha256 = sha256_file(self.app_binary)

    def start_compose(self):
        self.compose_started = True
        self.run_cmd(
            self.compose_cmd() + ["config", "-q"],
            "compose-config.log",
            env=self.docker_env(),
            cwd=REPO,
        )
        self.run_cmd(
            self.compose_cmd() + ["up", "-d", "--wait", "--pull", "never", "postgres"],
            "compose-up.log",
            env=self.docker_env(),
            cwd=REPO,
        )
        self.run_cmd(
            self.compose_cmd(with_app=True, profile_migrate=True)
            + ["run", "--rm", "--no-deps", "--pull", "never", "migrate", "up"],
            "compose-migrate.log",
            env=self.docker_env(),
            cwd=REPO,
        )
        inspect = self.capture_cmd(
            ["docker", "inspect", f"{self.compose_project}-postgres-1"],
            env=self.docker_env(),
        )
        if inspect.returncode != 0:
            raise RunError("PostgreSQL container inspection failed")
        host_config = self.capture_cmd(
            ["docker", "inspect", "--format", "{{json .HostConfig}}",
             f"{self.compose_project}-postgres-1"],
            env=self.docker_env(),
        )
        try:
            hc = json.loads(host_config.stdout)
        except json.JSONDecodeError:
            hc = {}
        self.postgres_host_config = {
            key: hc.get(key) for key in
            ("NanoCpus", "CpuQuota", "CpuPeriod", "CpusetCpus", "CpuShares")
        }
        self.verify_container_cpuset()

    def verify_container_cpuset(self):
        expected = parse_cpu_set(PG_CPUS)
        configured = (self.postgres_host_config.get("CpusetCpus") or "").strip()
        if not configured:
            raise RunError("PostgreSQL container has no configured CpusetCpus")
        try:
            configured_set = parse_cpu_set(configured)
        except CpuPlanError as exc:
            raise RunError(f"cannot parse PostgreSQL CpusetCpus {configured!r}: {exc}") from exc
        if configured_set != expected:
            raise RunError(
                f"PostgreSQL configured cpuset {format_cpu_set(configured_set)} "
                f"!= required {PG_CPUS}"
            )
        container = f"{self.compose_project}-postgres-1"
        effective = self.capture_cmd(
            ["docker", "exec", container, "grep", "^Cpus_allowed_list", "/proc/1/status"],
            env=self.docker_env(),
        )
        if effective.returncode != 0:
            raise RunError(
                f"cannot verify PostgreSQL effective cpuset in {container}: "
                f"{effective.stderr.strip()}"
            )
        observed = None
        for line in effective.stdout.splitlines():
            if line.startswith("Cpus_allowed_list:"):
                observed = parse_cpu_set(line.split(":", 1)[1].strip())
                break
        if observed is None:
            raise RunError("PostgreSQL container did not report Cpus_allowed_list")
        if observed != expected:
            raise RunError(
                f"PostgreSQL effective cpuset {format_cpu_set(observed)} "
                f"!= required {PG_CPUS}"
            )
        self.pg_affinity = {
            "configured": format_cpu_set(configured_set),
            "effective": format_cpu_set(observed),
            "required": PG_CPUS,
        }

    def verify_round_cpus(self):
        self.verify_container_cpuset()
        if self.app_proc is None or self.app_proc.poll() is not None:
            raise RunError("app is not running; cannot verify app CPU affinity")
        try:
            app_affinity = read_process_affinity(self.app_proc.pid)
            k6_child_affinity = verify_taskset_child(parse_cpu_set(K6_CPUS))
        except CpuPlanError as exc:
            raise RunError(f"CPU affinity verification failed: {exc}") from exc
        if app_affinity != parse_cpu_set(APP_CPUS):
            raise RunError(
                f"app effective affinity {format_cpu_set(app_affinity)} != required {APP_CPUS}"
            )
        self.app_affinity = {
            **getattr(self, "app_affinity", {}),
            "required": APP_CPUS,
            "effective": format_cpu_set(app_affinity),
            "shares_pg": bool(app_affinity & parse_cpu_set(PG_CPUS)),
            "verified_again_before_stage": True,
        }
        return {
            "k6_taskset_child": format_cpu_set(k6_child_affinity),
            "app": self.app_affinity,
            "pg": self.pg_affinity,
        }

    def wait_for_exec(self, proc, expected_path, what, timeout_s):
        """Wait until the same PID has exec'd the expected binary.

        `taskset` sets affinity and then execs the target without changing PID.
        Reading /proc/<pid>/status immediately after Popen can therefore observe
        the unpinned launcher (0-15) before that exec. This helper only returns
        once the process image is the expected binary, so the caller can make an
        affinity decision about the real target rather than the launcher.
        """
        expected = Path(expected_path).resolve()
        deadline = time.monotonic() + timeout_s
        last = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RunError(
                    f"{what} exited before exec/affinity verification "
                    f"(exit={proc.returncode})"
                )
            try:
                actual = Path(f"/proc/{proc.pid}/exe").resolve()
            except OSError as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if actual == expected:
                    return actual
                last = str(actual)
            time.sleep(0.01)
        raise RunError(
            f"{what} did not exec expected binary {expected} within "
            f"{timeout_s:g}s; last={last!r}"
        )

    def start_app(self):
        self.app_log = self.logs_dir / "app.log"
        with open(self.app_log, "ab") as log:
            self.app_proc = subprocess.Popen(
                ["taskset", "-c", APP_CPUS, str(self.app_binary)],
                cwd=str(REPO),
                env=self.app_env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        (self.run_dir / "app.pid").write_text(str(self.app_proc.pid) + "\n", encoding="utf-8")
        exec_path = self.wait_for_exec(
            self.app_proc, self.app_binary, "application", APP_EXEC_TIMEOUT_S
        )
        try:
            observed = read_process_affinity(self.app_proc.pid)
        except (CpuPlanError, OSError) as exc:
            raise RunError(f"cannot verify app effective affinity: {exc}") from exc
        if observed != parse_cpu_set(APP_CPUS):
            raise RunError(
                f"app effective affinity after exec "
                f"{format_cpu_set(observed)} != required {APP_CPUS}"
            )
        self.app_affinity = {
            "required": APP_CPUS,
            "effective": format_cpu_set(observed),
            "shares_pg": bool(observed & parse_cpu_set(PG_CPUS)),
            "exec_path": str(exec_path),
            "verified_after_exec": True,
            "verified_after_readiness": False,
        }
        deadline = time.monotonic() + 30
        last_error = None
        while time.monotonic() < deadline:
            if self.app_proc.poll() is not None:
                raise RunError(f"application exited before readiness; see {self.app_log}")
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.app_port, timeout=1.0)
                conn.request("GET", "/readyz")
                response = conn.getresponse()
                body = response.read()
                conn.close()
                if response.status == 200 and b'"ready"' in body:
                    try:
                        observed_ready = read_process_affinity(self.app_proc.pid)
                    except (CpuPlanError, OSError) as exc:
                        raise RunError(
                            f"cannot verify app effective affinity after readiness: {exc}"
                        ) from exc
                    if observed_ready != parse_cpu_set(APP_CPUS):
                        raise RunError(
                            f"app effective affinity after readiness "
                            f"{format_cpu_set(observed_ready)} != required {APP_CPUS}"
                        )
                    self.app_affinity = {
                        "required": APP_CPUS,
                        "effective": format_cpu_set(observed_ready),
                        "shares_pg": bool(observed_ready & parse_cpu_set(PG_CPUS)),
                        "exec_path": str(exec_path),
                        "verified_after_exec": True,
                        "verified_after_readiness": True,
                    }
                    self.pool_config = self.parse_pool_config()
                    if not self.pool_config.get("ok"):
                        raise RunError(
                            "benchmark build MaxConns=16 evidence unavailable: "
                            f"{self.pool_config.get('error')}"
                        )
                    return
                last_error = f"HTTP {response.status}: {body[:80]!r}"
            except RunError:
                raise
            except Exception as exc:
                last_error = repr(exc)
            time.sleep(0.1)
        raise RunError(f"application did not become ready: {last_error}")

    def parse_pool_config(self):
        """Read the single startup POOL_CONFIG line emitted by the benchmark build.

        This is configuration evidence, not a periodic collection stream. The
        line contains only the configured and effective pool size.
        """
        try:
            with open(self.app_log, "rb") as source:
                source.seek(0, os.SEEK_END)
                size = source.tell()
                source.seek(max(0, size - MAX_LOG_BYTES))
                tail = source.read().decode("utf-8", errors="replace")
        except OSError as exc:
            return {"ok": False, "error": f"cannot read app log: {exc}"}
        for line in reversed(tail.splitlines()):
            if not line.startswith(POOL_CONFIG_PREFIX):
                continue
            try:
                document = json.loads(line[len(POOL_CONFIG_PREFIX):])
            except json.JSONDecodeError as exc:
                return {"ok": False, "error": f"invalid POOL_CONFIG JSON: {exc}"}
            if not isinstance(document, dict):
                return {"ok": False, "error": "POOL_CONFIG is not an object"}
            configured = document.get("configured_max_conns")
            effective = document.get("max_conns")
            ok = (
                configured == EXPECTED_MAX_CONNS
                and effective == EXPECTED_MAX_CONNS
            )
            return {
                "ok": ok,
                "configured_max_conns": configured,
                "max_conns": effective,
                "expected_max_conns": EXPECTED_MAX_CONNS,
                "error": None if ok else (
                    f"configured={configured!r} effective={effective!r} "
                    f"expected={EXPECTED_MAX_CONNS}"
                ),
            }
        return {"ok": False, "error": "POOL_CONFIG line missing from app log"}

    # ---------- k6 and SQL ----------

    def k6_env(self):
        # Isolate k6 from the invoking user's $HOME/XDG config and cache. Only
        # the fixed no-usage-report switch is set; inherited K6_*/proxy/PG
        # names were already removed by sanitized_env().
        for path in (self.k6_home, self.k6_home / ".config",
                     self.k6_home / ".cache", self.k6_home / ".local"):
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        env = sanitized_env({
            "HOME": str(self.k6_home),
            "XDG_CONFIG_HOME": str(self.k6_home / ".config"),
            "XDG_CACHE_HOME": str(self.k6_home / ".cache"),
            "XDG_DATA_HOME": str(self.k6_home / ".local"),
            "K6_NO_USAGE_REPORT": "true",
            "PERF_API_KEY": self.api_key,
        })
        env.pop("K6_CONFIG", None)
        env.pop("K6_NO_COLOR", None)
        return env

    def run_k6(self, stage, rate, duration_s, prefix, pre_vus, max_vus):
        summary_rel = f"results/{stage}-summary.json"
        summary_abs = self.run_dir / summary_rel
        if summary_abs.exists():
            summary_abs.unlink()
        cmd = [
            "taskset", "-c", K6_CPUS,
            str(self.k6_path), "run", "--quiet", "--no-color", "--no-usage-report",
            "-e", f"STAGE={stage}",
            "-e", f"RATE={rate}",
            "-e", f"DURATION={duration_s}s",
            "-e", f"BASE_URL=http://127.0.0.1:{self.app_port}",
            "-e", f"ORDER_PREFIX={prefix}",
            "-e", f"PRE_VUS={pre_vus}",
            "-e", f"MAX_VUS={max_vus}",
            "-e", f"SUMMARY_PATH={summary_rel}",
            str(K6_JS),
        ]
        log_path = self.logs_dir / f"k6-{stage}.log"
        # k6 runs from the round directory so handleSummary's relative path is
        # unambiguous; the JS file is still addressed absolutely.
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.run_dir),
                env=self.k6_env(),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        started_epoch = time.time()
        affinity = None
        k6_exec_path = None
        cpu_ticks: list[tuple[float, int]] = []
        cpu_attempts = 0
        try:
            k6_exec_path = self.wait_for_exec(
                proc, self.k6_path, "k6", K6_EXEC_TIMEOUT_S
            )
            affinity = read_process_affinity(proc.pid)
            if affinity != parse_cpu_set(K6_CPUS):
                raise RunError(
                    f"k6 effective affinity after exec "
                    f"{format_cpu_set(affinity)} != required {K6_CPUS}"
                )
            while proc.poll() is None:
                cpu_attempts += 1
                try:
                    utime, stime = read_process_cpu_ticks(proc.pid)
                    cpu_ticks.append((time.monotonic(), utime + stime))
                except (CpuPlanError, OSError):
                    pass
                time.sleep(K6_CPU_SAMPLE_INTERVAL_S)
        except BaseException:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            raise
        returncode = proc.wait()
        finished_epoch = time.time()

        clock_ticks = clock_ticks_per_second()
        allowed = len(parse_cpu_set(K6_CPUS))
        cpu_summary = summarize_cpu_windows(cpu_ticks, cpu_attempts, clock_ticks, allowed)
        windows = cpu_summary.pop("windows")
        cpu_points_path = self.db_dir / f"k6-{stage}-cpu.jsonl"
        try:
            with open(cpu_points_path, "w", encoding="utf-8") as handle:
                for offset, (_mono, ticks) in enumerate(cpu_ticks):
                    handle.write(json.dumps({
                        "kind": "sample",
                        "index": offset,
                        "ticks": ticks,
                        "offset_s": round(_mono - cpu_ticks[0][0], 6) if cpu_ticks else None,
                    }, sort_keys=True) + "\n")
                for window in windows:
                    handle.write(json.dumps({"kind": "window", **window}, sort_keys=True) + "\n")
            os.chmod(cpu_points_path, 0o600)
            self.cap_log(cpu_points_path, 1024 * 1024)
        except OSError:
            cpu_points_path = None
        k6_cpu = {
            **cpu_summary,
            "points_path": str(cpu_points_path) if cpu_points_path else None,
            "attribution_note": (
                "k6 CPU saturated in at least one 1 s window; PostgreSQL attribution is blocked"
                if cpu_summary["saturated"] else
                ("k6 CPU window sampling incomplete; attribution is unknown"
                 if cpu_summary["sampling_status"] != "ok" else
                 "k6 CPU threshold not exceeded is not causal permission; "
                 "single-thread/runtime bottlenecks remain possible")
            ),
        }
        self.cap_log(log_path)
        observation = {
            "affinity_required": K6_CPUS,
            "affinity_effective": format_cpu_set(affinity) if affinity else None,
            "exec_path": str(k6_exec_path) if k6_exec_path else None,
            "started_epoch": started_epoch,
            "finished_epoch": finished_epoch,
            "cpu": k6_cpu,
        }
        if not summary_abs.is_file():
            observation["summary_error"] = (
                f"k6 did not write summary for {stage}; see {log_path}"
            )
            return returncode, None, None, observation
        try:
            data = json.loads(summary_abs.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            observation["summary_error"] = f"invalid k6 summary for {stage}: {exc}"
            return returncode, None, None, observation
        return returncode, data, summary_abs, observation

    def verify_sql(self, stage, prefix, expected_count, exact, seq_limit=None):
        if seq_limit is None:
            seq_limit = expected_count
        output_path = self.db_dir / f"{stage}-verify.txt"
        cmd = [
            "psql", "-X", "-q", "-A", "-F", "\t", "-t",
            "-v", "ON_ERROR_STOP=1",
            "-v", f"client_id={self.client_id}",
            "-v", f"prefix={prefix}",
            "-v", f"expected_count={expected_count}",
            "-v", f"seq_limit={seq_limit}",
            "-v", f"exact={exact}",
            "-f", str(SQL_VERIFY),
        ]
        proc = self.capture_cmd(cmd, env=self.psql_env, cwd=REPO, timeout=60)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(proc.stdout)
            if proc.stderr:
                f.write("\nSTDERR:\n" + proc.stderr)
        sql_cap = self.cap_log(output_path, MAX_SQL_OUTPUT_BYTES)
        sections = {}
        current = None
        for line in proc.stdout.splitlines():
            if line.startswith("== ") and line.endswith(" =="):
                current = line.strip("= ").strip()
                sections[current] = []
            elif line.strip():
                sections.setdefault(current or "preamble", []).append(line)
        summary_line = sections.get("summary", [""])[0].split("\t")
        if len(summary_line) != 12 or summary_line[0] != "summary":
            return {
                "exit": proc.returncode,
                "ok": False,
                "error": f"unexpected SQL summary: {summary_line!r}",
                "sections": {key: value[:20] for key, value in sections.items()},
                "raw_prefix": proc.stdout[:2000],
                "path": str(output_path),
                "output_truncated": sql_cap,
            }
        _, rows, distinct, succeeded, amount, currency, confirmed, mock_linked, seq_ok, seq_in_range, min_seq, max_seq = summary_line
        values = {
            "rows": int(rows),
            "distinct": int(distinct),
            "succeeded": int(succeeded),
            "amount_100": int(amount),
            "currency_CNY": int(currency),
            "confirmed": int(confirmed),
            "mock_linked": int(mock_linked),
            "seq_format_ok": int(seq_ok),
            "seq_in_range": int(seq_in_range),
            "min_seq": min_seq,
            "max_seq": max_seq,
        }
        mock_mismatch = int(sections.get("mock_mismatch", ["0"])[0])
        handling_mismatch = int(sections.get("handling_mismatch", ["0"])[0])
        duplicate_groups = int(sections.get("duplicate_groups", ["0"])[0])
        anomaly_rows = [line for line in sections.get("anomaly_rows", []) if line.strip()]
        missing = [line for line in sections.get("missing_expected_orders", []) if line.strip()]
        extra = [line for line in sections.get("extra_or_out_of_range_orders", []) if line.strip()]
        anomaly_orders = [line.split("\t")[0] for line in anomaly_rows]
        # A dropped iteration never reaches the server. A timed-out or failed
        # HTTP request may or may not have created an order. In those cases the
        # SQL gate must accept rows <= actual HTTP requests and still reject
        # every internal inconsistency. Only a fully clean response set demands
        # the exact order-number set.
        ok = (
            proc.returncode == 0
            and values["rows"] <= expected_count
            and values["distinct"] == values["rows"]
            and values["succeeded"] == values["rows"]
            and values["amount_100"] == values["rows"]
            and values["currency_CNY"] == values["rows"]
            and values["confirmed"] == values["rows"]
            and values["mock_linked"] == values["rows"]
            and values["seq_format_ok"] == values["rows"]
            and values["seq_in_range"] == values["rows"]
            and mock_mismatch == 0
            and handling_mismatch == 0
            and duplicate_groups == 0
            and not anomaly_rows
        )
        if exact:
            ok = (
                ok
                and values["rows"] == expected_count
                and not missing
                and not extra
                and (expected_count == 0 or (
                    values["min_seq"] == "0"
                    and values["max_seq"] == str(expected_count - 1)
                ))
            )
        return {
            "exit": proc.returncode,
            "ok": bool(ok),
            "exact": bool(exact),
            "expected_count": expected_count,
            "seq_limit": seq_limit,
            "row_count_within_expected": values["rows"] <= expected_count,
            "values": values,
            "mock_mismatch": mock_mismatch,
            "handling_mismatch": handling_mismatch,
            "duplicate_groups": duplicate_groups,
            "anomaly_count": len(anomaly_rows),
            "anomaly_orders": anomaly_orders[:100],
            "anomaly_orders_truncated": max(0, len(anomaly_orders) - 100),
            "missing_count": len(missing),
            "missing_orders": missing[:100],
            "extra_count": len(extra),
            "extra_orders": extra[:100],
            "path": str(output_path),
            "output_truncated": sql_cap,
        }

    @staticmethod
    def metric_values(data, name):
        return data.get("metrics", {}).get(name, {}).get("values", {})

    def stage_record(self, stage, rate, duration_s, prefix, pre_vus, max_vus):
        started_stages = getattr(self, "started_stages", None)
        if started_stages is None:
            started_stages = set()
            self.started_stages = started_stages
        started_stages.add(stage)
        k6_exit, data, summary_path, observation = self.run_k6(
            stage, rate, duration_s, prefix, pre_vus, max_vus
        )
        target_iterations = int(rate * duration_s)
        cpu = observation.get("cpu", {})
        iterations_semantics = (
            "k6 summary `iterations` is completed iterations; "
            "`started_iterations` comes from the payment_iteration_started "
            "counter at iteration entry"
        )
        if data is None:
            reason = observation.get(
                "summary_error", f"k6 did not produce a usable summary for {stage}"
            )
            return {
                "stage": stage,
                "target_rps": rate,
                "duration_s": duration_s,
                "target_iterations": target_iterations,
                "order_prefix": prefix,
                "vus": {
                    "pre_allocated": pre_vus,
                    "max": max_vus,
                    "observed_max": None,
                },
                "k6_exit": k6_exit,
                "k6_affinity": observation.get("affinity_effective"),
                "k6_affinity_required": observation.get("affinity_required"),
                "k6_exec_path": observation.get("exec_path"),
                "k6_started_epoch": observation.get("started_epoch"),
                "k6_finished_epoch": observation.get("finished_epoch"),
                "k6_cpu": cpu,
                "http_reqs": None,
                "iterations": None,
                "completed_iterations": None,
                "iterations_semantics": iterations_semantics,
                "started_iterations": None,
                "started_counter": "payment_iteration_started",
                "dropped_iterations": None,
                "dropped_iterations_metric_present": False,
                "dropped_iterations_metric_state": "summary_missing",
                "dropped_iterations_source": "k6_summary_missing",
                "dropped_iterations_absent_accepted": False,
                "dropped_iterations_note": (
                    "k6 summary missing; dropped_iterations cannot be classified"
                ),
                "other_key_metrics_ok": False,
                "dropped_rate": None,
                "actual_start_rate": None,
                "actual_completion_rate": None,
                "actual_http_rate": None,
                "target_check": "summary_missing",
                "target_met": False,
                "target_exact": False,
                "started_target_delta": None,
                "completed_target_delta": None,
                "target_overshoot": None,
                "target_overshoot_note": None,
                "missing_metrics": ["k6_summary"],
                "key_metrics_ok": False,
                "p95_ms": None,
                "p99_ms": None,
                "http_req_failed_rate": None,
                "checks_rate": None,
                "payment_business_rate": None,
                "payment_response_received_rate": None,
                "payment_business_passes": None,
                "payment_response_received_passes": None,
                "business_fail": None,
                "issues": None,
                "timeout": None,
                "transport_error": None,
                "status_5xx": None,
                "status_4xx": None,
                "status_other": None,
                "vus_max": None,
                "sequence_set": "incomplete_or_failed",
                "sequence_set_exact": False,
                "sql": None,
                "complete": False,
                "fatal": False,
                "fatal_reasons": [],
                "incomplete_reasons": [reason],
                "summary_path": None,
                "k6_log": str(self.logs_dir / f"k6-{stage}.log"),
            }

        metrics = data.get("metrics", {})
        if not isinstance(metrics, dict):
            metrics = {}
        http_values = self.metric_values(data, "http_reqs")
        iterations_values = self.metric_values(data, "iterations")
        dropped_metric_present = "dropped_iterations" in metrics
        dropped_metric = metrics.get("dropped_iterations")
        dropped_values = {}
        dropped_state = "absent"
        dropped = 0
        if dropped_metric_present:
            if not isinstance(dropped_metric, dict):
                dropped_state = "present_malformed"
            else:
                dropped_values = dropped_metric.get("values")
                if not isinstance(dropped_values, dict):
                    dropped_state = "present_malformed"
                else:
                    raw_count = dropped_values.get("count")
                    if (
                        isinstance(raw_count, (int, float))
                        and not isinstance(raw_count, bool)
                        and raw_count >= 0
                        and int(raw_count) == raw_count
                    ):
                        dropped = int(raw_count)
                        dropped_state = (
                            "reported_zero" if dropped == 0 else "reported_positive"
                        )
                    else:
                        dropped_state = "present_malformed"
        started_values = self.metric_values(data, "payment_iteration_started")
        http_reqs = int(http_values.get("count", 0) or 0)
        iterations = int(iterations_values.get("count", 0) or 0)
        completed_iterations = iterations
        started_iterations = int(started_values.get("count", 0) or 0)
        duration = self.metric_values(data, "http_req_duration")
        p95 = duration.get("p(95)")
        p99 = duration.get("p(99)")
        business_values = self.metric_values(data, "payment_business_ok")
        response_values = self.metric_values(data, "payment_response_received")
        business_rate = business_values.get("rate")
        response_rate = response_values.get("rate")
        business_passes = business_values.get("passes")
        response_passes = response_values.get("passes")
        http_failed_values = self.metric_values(data, "http_req_failed")
        checks_values = self.metric_values(data, "checks")
        http_failed = http_failed_values.get("rate")
        checks_rate = checks_values.get("rate")
        vus_values = self.metric_values(data, "vus_max")
        vus_max = vus_values.get("max")
        counts = {
            "business_fail": int(self.metric_values(data, "payment_business_fail").get("count", 0)),
            "issues": int(self.metric_values(data, "payment_issues").get("count", 0)),
            "timeout": int(self.metric_values(data, "payment_timeout").get("count", 0)),
            "transport_error": int(self.metric_values(data, "payment_transport_error").get("count", 0)),
            "status_5xx": int(self.metric_values(data, "payment_status_5xx").get("count", 0)),
            "status_4xx": int(self.metric_values(data, "payment_status_4xx").get("count", 0)),
            "status_other": int(self.metric_values(data, "payment_status_other").get("count", 0)),
            "vus_max": vus_max,
        }
        missing_metrics = []
        if not isinstance(http_values.get("count"), (int, float)):
            missing_metrics.append("http_reqs.count")
        if not isinstance(iterations_values.get("count"), (int, float)):
            missing_metrics.append("iterations.count")
        # dropped_iterations is evaluated separately: k6 v2.4.0 omits the
        # metric from the summary when no drop samples were emitted. A complete
        # absence is accepted only under the conditions below, while a present
        # but malformed bucket or an explicit positive count is rejected.
        if not isinstance(started_values.get("count"), (int, float)):
            missing_metrics.append("payment_iteration_started.count")
        if not isinstance(p95, (int, float)):
            missing_metrics.append("http_req_duration.p(95)")
        if not isinstance(p99, (int, float)):
            missing_metrics.append("http_req_duration.p(99)")
        if not isinstance(business_rate, (int, float)):
            missing_metrics.append("payment_business_ok.rate")
        if not isinstance(business_passes, (int, float)):
            missing_metrics.append("payment_business_ok.passes")
        if not isinstance(response_rate, (int, float)):
            missing_metrics.append("payment_response_received.rate")
        if not isinstance(response_passes, (int, float)):
            missing_metrics.append("payment_response_received.passes")
        if not isinstance(http_failed, (int, float)):
            missing_metrics.append("http_req_failed.rate")
        if not isinstance(checks_rate, (int, float)):
            missing_metrics.append("checks.rate")
        if not isinstance(vus_max, (int, float)):
            missing_metrics.append("vus_max.max")
        other_key_metrics_ok = not missing_metrics

        actual_start_rate = (
            round(started_iterations / duration_s, 6) if duration_s else None
        )
        actual_completion_rate = (
            round(completed_iterations / duration_s, 6) if duration_s else None
        )
        actual_http_rate = round(http_reqs / duration_s, 6) if duration_s else None
        dropped_rate = (
            round(dropped / target_iterations, 9) if target_iterations else None
        )
        target_overshoot = started_iterations - target_iterations
        if stage == "smoke":
            target_check = "smoke_tolerance_10_or_11"
            allowed_targets = {target_iterations, target_iterations + 1}
            target_met = (
                started_iterations in allowed_targets
                and completed_iterations in allowed_targets
            )
        else:
            target_check = "at_least_target"
            allowed_targets = None
            target_met = (
                started_iterations >= target_iterations
                and completed_iterations >= target_iterations
            )
        dropped_absent_accepted = False
        if dropped_state == "absent":
            if k6_exit == 0 and other_key_metrics_ok and target_met:
                dropped_state = "absent_accepted_not_reported"
                dropped_absent_accepted = True
                dropped = 0
            else:
                dropped_state = "absent_not_accepted"
                missing_metrics.append("dropped_iterations.count")
        elif dropped_state == "present_malformed":
            missing_metrics.append("dropped_iterations.malformed")
        key_metrics_ok = not missing_metrics
        clean_responses = (
            dropped == 0
            and http_reqs == iterations
            and started_iterations == iterations
            and counts["timeout"] == 0
            and counts["transport_error"] == 0
            and counts["business_fail"] == 0
            and counts["status_5xx"] == 0
            and counts["status_4xx"] == 0
            and counts["status_other"] == 0
            and business_rate == 1
            and response_rate == 1
            and http_failed == 0
            and checks_rate == 1
        )
        exact = 1 if clean_responses else 0
        # A dropped iteration was never started. The sequence bound covers the
        # largest observed count across starts, completions and HTTP requests.
        seq_limit = max(http_reqs, iterations, started_iterations)
        sql = self.verify_sql(stage, prefix, http_reqs, exact, seq_limit=seq_limit)
        reasons = []
        if not key_metrics_ok:
            reasons.append("missing key metrics: " + ", ".join(missing_metrics))
        if dropped_state == "absent_not_accepted":
            reasons.append(
                "dropped_iterations metric absent and not accepted "
                "(requires exit 0, other key metrics complete and target aligned)"
            )
        elif dropped_state == "present_malformed":
            reasons.append(
                "dropped_iterations metric present but malformed "
                "(count missing, non-numeric or negative)"
            )
        if k6_exit != 0:
            reasons.append(f"k6 exit {k6_exit}")
        if dropped > 0:
            reasons.append(f"dropped_iterations {dropped} (not a server failure)")
        if http_reqs == 0:
            reasons.append("no HTTP requests started")
        if started_iterations != iterations:
            reasons.append(
                f"started_iterations {started_iterations} "
                f"!= completed_iterations {iterations}"
            )
        if http_reqs != iterations:
            reasons.append(f"http_reqs {http_reqs} != iterations {iterations}")
        if not target_met:
            if stage == "smoke":
                reasons.append(
                    f"smoke started/completed {started_iterations}/{iterations} "
                    f"outside {sorted(allowed_targets)} tolerance"
                )
            else:
                reasons.append(
                    f"started/completed {started_iterations}/{iterations} "
                    f"below target {target_iterations}"
                )
        if target_overshoot > 1:
            reasons.append(
                f"started_iterations {started_iterations} exceeds target "
                f"{target_iterations} by {target_overshoot} (>1)"
            )
        if completed_iterations > target_iterations + 1:
            reasons.append(
                f"completed_iterations {completed_iterations} exceeds target "
                f"{target_iterations} by more than 1"
            )
        if counts["timeout"] > 0:
            reasons.append(f"timeouts {counts['timeout']}")
        if counts["transport_error"] > 0:
            reasons.append(f"transport errors {counts['transport_error']}")
        if counts["business_fail"] > 0:
            reasons.append(f"business failures {counts['business_fail']}")
        if business_rate is not None and business_rate < 1:
            reasons.append(f"business rate {business_rate} < 1")
        if response_rate is not None and response_rate < 1:
            reasons.append(f"response received rate {response_rate} < 1")
        if http_failed is not None and http_failed > 0:
            reasons.append(f"http_req_failed rate {http_failed} > 0")
        if checks_rate is not None and checks_rate < 1:
            reasons.append(f"checks rate {checks_rate} < 1")
        if any(counts[key] for key in ("status_5xx", "status_4xx", "status_other")):
            reasons.append("non-201 HTTP status observed")
        if response_passes == 0 and http_reqs > 0:
            reasons.append("no HTTP responses received")
        if business_passes == 0 and http_reqs > 0:
            reasons.append("no successful business responses")
        if not sql.get("ok"):
            reasons.append("SQL reconciliation gate failed")
        # Ordinary measurement failures never abort the remaining rounds. Only
        # safety/environment failures are escalated by run_round().
        complete = not reasons
        if clean_responses and target_met and key_metrics_ok and sql.get("ok"):
            sequence_set = (
                "exact" if target_overshoot == 0
                else "exact_with_target_overshoot"
            )
        elif dropped > 0:
            sequence_set = "not_exact_due_to_dropped_iterations"
        elif any(counts[key] for key in (
            "timeout", "transport_error", "business_fail",
            "status_5xx", "status_4xx", "status_other",
        )):
            sequence_set = "not_exact_due_to_incomplete_or_failed_responses"
        elif not key_metrics_ok:
            sequence_set = "not_exact_due_to_missing_metrics"
        elif not target_met:
            sequence_set = "not_exact_due_to_target_shortfall"
        elif started_iterations != iterations:
            sequence_set = "not_exact_due_to_started_completed_mismatch"
        elif not clean_responses:
            sequence_set = "not_exact_due_to_incomplete_or_failed_responses"
        elif not sql.get("ok"):
            sequence_set = "not_exact_due_to_sql_reconciliation"
        else:
            sequence_set = "incomplete_or_failed"
        target_overshoot_note = None
        if target_overshoot > 0:
            if stage == "smoke":
                target_overshoot_note = (
                    "smoke 10/11 tolerance permits one extra iteration; recorded "
                    "explicitly, not normalized."
                )
            else:
                target_overshoot_note = (
                    "over-target iterations are recorded explicitly, not normalized; "
                    "the runner does not assume a fixed boundary overshoot"
                )
        if dropped_state == "absent_accepted_not_reported":
            dropped_source = "k6_v2.4.0_summary_absent_no_samples"
            dropped_note = (
                "k6 v2.4.0 omitted dropped_iterations because no drop samples "
                "were emitted; accepted as not reported only because k6 exit=0, "
                "other key metrics are complete and target is aligned. This is "
                "not an explicit zero sample."
            )
        elif dropped_state == "absent_not_accepted":
            dropped_source = "summary_absent_not_accepted"
            dropped_note = (
                "k6 v2.4.0 omitted dropped_iterations; not accepted because "
                "exit, other key metrics or target alignment did not qualify."
            )
        elif dropped_state == "present_malformed":
            dropped_source = "summary_present_malformed"
            dropped_note = (
                "dropped_iterations metric is present but values.count is "
                "missing, non-numeric or negative; rejected."
            )
        elif dropped_state == "reported_zero":
            dropped_source = "summary_reported_zero"
            dropped_note = "explicit zero dropped_iterations count in k6 summary."
        else:
            dropped_source = "summary_reported_positive"
            dropped_note = "explicit positive dropped_iterations count; rejected."
        return {
            "stage": stage,
            "target_rps": rate,
            "duration_s": duration_s,
            "target_iterations": target_iterations,
            "order_prefix": prefix,
            "vus": {
                "pre_allocated": pre_vus,
                "max": max_vus,
                "observed_max": counts["vus_max"],
            },
            "k6_exit": k6_exit,
            "k6_affinity": observation.get("affinity_effective"),
            "k6_affinity_required": observation.get("affinity_required"),
            "k6_exec_path": observation.get("exec_path"),
            "k6_started_epoch": observation.get("started_epoch"),
            "k6_finished_epoch": observation.get("finished_epoch"),
            "k6_cpu": cpu,
            "http_reqs": http_reqs,
            "iterations": iterations,
            "completed_iterations": completed_iterations,
            "iterations_semantics": iterations_semantics,
            "started_iterations": started_iterations,
            "started_counter": "payment_iteration_started",
            "dropped_iterations": dropped,
            "dropped_iterations_metric_present": dropped_metric_present,
            "dropped_iterations_metric_state": dropped_state,
            "dropped_iterations_source": dropped_source,
            "dropped_iterations_absent_accepted": dropped_absent_accepted,
            "dropped_iterations_note": dropped_note,
            "other_key_metrics_ok": other_key_metrics_ok,
            "dropped_rate": dropped_rate,
            "actual_start_rate": actual_start_rate,
            "actual_completion_rate": actual_completion_rate,
            "actual_http_rate": actual_http_rate,
            "target_check": target_check,
            "target_met": target_met,
            "target_exact": bool(target_met and target_overshoot == 0),
            "started_target_delta": started_iterations - target_iterations,
            "completed_target_delta": completed_iterations - target_iterations,
            "target_overshoot": target_overshoot,
            "target_overshoot_note": target_overshoot_note,
            "missing_metrics": missing_metrics,
            "key_metrics_ok": key_metrics_ok,
            "p95_ms": p95,
            "p99_ms": p99,
            "http_req_failed_rate": http_failed,
            "checks_rate": checks_rate,
            "payment_business_rate": business_rate,
            "payment_response_received_rate": response_rate,
            "payment_business_passes": business_passes,
            "payment_response_received_passes": response_passes,
            **counts,
            "sequence_set": sequence_set,
            "sequence_set_exact": bool(
                clean_responses
                and target_met
                and target_overshoot == 0
                and key_metrics_ok
                and sql.get("ok")
            ),
            "sql": sql,
            "complete": complete,
            "fatal": False,
            "fatal_reasons": [],
            "incomplete_reasons": reasons,
            "summary_path": str(summary_path),
            "k6_log": str(self.logs_dir / f"k6-{stage}.log"),
        }

    # ---------- orchestration ----------

    def begin_round(self, index):
        self.round_index = index
        self.run_id = f"{self.base_run_id}r{index}"
        self.run_dir = self.run_root / f"round-{index}"
        self.order_base = f"R{index}K{self.base_run_id}"
        self.smoke_prefix = f"{self.order_base}SMK"
        self.warmup_prefix = f"{self.order_base}W50"
        self.load_prefix = f"{self.order_base}L{self.profile}"
        self.compose_started = False
        self.app_proc = None
        self.volume = None
        self.cleanup_warning = None
        self.pool_config = None
        self.started_stages = set()
        self.pg_affinity = None
        self.app_affinity = None
        self.prepare_round_dirs()
        self.app_log = self.logs_dir / "app.log"
        self.create_environment()
        return {
            "round": index,
            "round_id": self.run_id,
            "started_at_utc": utc_now(),
            "status": "running",
            "stages": [],
            "fatal": False,
            "fatal_reasons": [],
        }

    def not_run_round(self, index, reason):
        return {
            "round": index,
            "round_id": f"{self.base_run_id}r{index}",
            "status": "not_run",
            "not_run": True,
            "reason": reason,
            "stages": [],
        }

    def not_run_stage(self, stage, rate, duration_s, reason):
        return {
            "stage": stage,
            "target_rps": rate,
            "duration_s": duration_s,
            "not_run": True,
            "reason": reason,
            "complete": False,
            "fatal": False,
            "fatal_reasons": [],
            "incomplete_reasons": [reason],
        }

    def stop_app(self):
        if self.app_proc is None:
            return {"ok": True, "already_stopped": True, "exit": None}
        proc = self.app_proc
        terminated = False
        forced = False
        if proc.poll() is None:
            terminated = True
            try:
                proc.terminate()
                proc.wait(timeout=APP_SHUTDOWN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                forced = True
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception as exc:
                    self.cleanup_warning = f"application kill failed: {exc}"
            except Exception as exc:
                self.cleanup_warning = f"application stop failed: {exc}"
        exit_code = proc.poll()
        still_running = exit_code is None
        if still_running:
            self.cleanup_warning = "application process still running after stop"
        self.app_proc = None
        return {
            "ok": not forced and not still_running and exit_code == 0,
            "already_stopped": not terminated,
            "terminated": terminated,
            "forced": forced,
            "still_running": still_running,
            "exit": exit_code,
        }

    def stop_compose(self):
        if not self.compose_started:
            return {"ok": True, "already_stopped": True, "exit": None}
        try:
            proc = self.run_cmd(
                self.compose_cmd() + ["down"],
                "compose-down.log",
                env=self.docker_env(),
                cwd=REPO,
                check=False,
            )
        except Exception as exc:
            self.cleanup_warning = f"docker compose down failed: {exc}"
            return {"ok": False, "exit": None, "error": str(exc)}
        if proc.returncode != 0:
            self.cleanup_warning = f"docker compose down exit {proc.returncode}"
            return {"ok": False, "exit": proc.returncode}
        self.compose_started = False
        return {"ok": True, "exit": 0}

    def ensure_stage_markers(self, record, reason):
        reason_text = "; ".join(reason) or "round terminated before stage completion"
        existing = {stage.get("stage") for stage in record.get("stages", [])}
        started_stages = getattr(self, "started_stages", set())
        stages = [
            ("smoke", SMOKE_RATE, SMOKE_DURATION_S),
            ("warmup", WARMUP_RATE, WARMUP_DURATION_S),
            (f"load{self.profile}", self.target_rate, LOAD_DURATION_S),
        ]
        for name, rate, duration_s in stages:
            if name in existing:
                continue
            marker = self.not_run_stage(name, rate, duration_s, reason_text)
            if name in started_stages:
                marker["not_run"] = False
                marker["status"] = "failed"
                marker["incomplete_reasons"] = [
                    f"stage started but no complete record: {reason_text}"
                ]
            record["stages"].append(marker)
            self.stage_records.append(marker)

    def run_round(self, index):
        record = self.begin_round(index)
        fatal_reasons: list[str] = []
        measurement_reasons: list[str] = []
        try:
            self.start_compose()
            self.start_app()
            record["pool_config"] = self.pool_config
            record["cpu_pre_smoke"] = self.verify_round_cpus()
            smoke = self.stage_record(
                "smoke", SMOKE_RATE, SMOKE_DURATION_S, self.smoke_prefix,
                SMOKE_PRE_VUS, SMOKE_MAX_VUS,
            )
            record["stages"].append(smoke)
            self.stage_records.append(smoke)
            self.print_stage(smoke)
            if not smoke.get("complete"):
                warmup = self.not_run_stage(
                    "warmup", WARMUP_RATE, WARMUP_DURATION_S,
                    "smoke did not pass; warmup not started",
                )
                record["stages"].append(warmup)
                self.stage_records.append(warmup)
                load_name = f"load{self.profile}"
                load_stage = self.not_run_stage(
                    load_name, self.target_rate, LOAD_DURATION_S,
                    "smoke did not pass; load not started",
                )
                record["stages"].append(load_stage)
                self.stage_records.append(load_stage)
                measurement_reasons.append(
                    "smoke did not pass; warmup and load not started"
                )
            else:
                record["cpu_pre_warmup"] = self.verify_round_cpus()
                warmup = self.stage_record(
                    "warmup", WARMUP_RATE, WARMUP_DURATION_S,
                    self.warmup_prefix, WARMUP_PRE_VUS, WARMUP_MAX_VUS,
                )
                record["stages"].append(warmup)
                self.stage_records.append(warmup)
                self.print_stage(warmup)
                if not warmup.get("complete"):
                    load_name = f"load{self.profile}"
                    load_stage = self.not_run_stage(
                        load_name, self.target_rate, LOAD_DURATION_S,
                        "warmup did not pass; load not started",
                    )
                    record["stages"].append(load_stage)
                    self.stage_records.append(load_stage)
                    measurement_reasons.append(
                        "warmup did not pass; load not started"
                    )
                else:
                    record["cpu_pre_load"] = self.verify_round_cpus()
                    load_stage = self.stage_record(
                        f"load{self.profile}", self.target_rate, LOAD_DURATION_S,
                        self.load_prefix, MAX_VUS, MAX_VUS,
                    )
                    record["stages"].append(load_stage)
                    self.stage_records.append(load_stage)
                    self.print_stage(load_stage)
                    if not load_stage.get("complete"):
                        measurement_reasons.append(
                            "load stage incomplete: "
                            + "; ".join(load_stage.get("incomplete_reasons", []))
                        )
        except (RunError, PreconditionError, OSError, CpuPlanError) as exc:
            # Safety/environment failure: stop the sequence, preserve volumes.
            fatal_reasons.append(f"{type(exc).__name__}: {exc}")
        finally:
            self.ensure_stage_markers(
                record, fatal_reasons or measurement_reasons
            )
            record["pool_config"] = self.pool_config
            app_stop = self.stop_app()
            record["app_stop"] = app_stop
            record["app_log_truncated"] = self.cap_log(self.app_log)
            if not app_stop.get("ok"):
                fatal_reasons.append(
                    f"application did not stop cleanly (exit={app_stop.get('exit')}, "
                    f"forced={app_stop.get('forced')})"
                )
            load_records = [
                stage for stage in record["stages"]
                if stage.get("stage", "").startswith("load") and not stage.get("not_run")
            ]
            if load_records:
                load_record = load_records[0]
                k6_cpu = load_record.get("k6_cpu", {})
                record["k6_cpu_saturated"] = bool(k6_cpu.get("saturated"))
                record["k6_cpu_peak_window_cores"] = k6_cpu.get("peak_window_cores")
                record["k6_cpu_sampling"] = k6_cpu.get("sampling_status")
                # CPU threshold is a blocker, never a causal permission.
                record["attribution_to_pg_allowed"] = False
                if k6_cpu.get("saturated"):
                    record["attribution_note"] = (
                        "k6 CPU saturated in at least one 1 s window; do not attribute "
                        "dropped iterations or latency to PostgreSQL"
                    )
                elif k6_cpu.get("sampling_status") != "ok":
                    record["attribution_note"] = (
                        "k6 CPU window sampling incomplete; attribution is unknown"
                    )
                else:
                    record["attribution_note"] = (
                        "k6 CPU threshold not exceeded is not causal permission; "
                        "single-thread/runtime bottlenecks remain possible"
                    )
            compose_stop = self.stop_compose()
            record["compose_stop"] = compose_stop
            if self.volume:
                self.volumes.append({"round": index, "volume": self.volume})
            if not compose_stop.get("ok"):
                fatal_reasons.append("compose teardown failed")
            if self.cleanup_warning:
                self.cleanup_warnings.append(self.cleanup_warning)
                fatal_reasons.append(f"cleanup warning: {self.cleanup_warning}")
            if compose_stop.get("ok"):
                try:
                    shutil.rmtree(self.env_dir)
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    self.cleanup_warnings.append(f"credential directory cleanup failed: {exc}")
                    fatal_reasons.append("credential directory cleanup failed")
            else:
                self.cleanup_warnings.append(
                    "credential directory retained for compose teardown retry"
                )

        record["finished_at_utc"] = utc_now()
        record["fatal"] = bool(fatal_reasons)
        record["fatal_reasons"] = fatal_reasons
        record["measurement_failures"] = measurement_reasons
        record["complete"] = (
            not fatal_reasons
            and all(stage.get("complete") for stage in record["stages"])
        )
        record["baseline_eligible"] = bool(record["complete"])
        record["status"] = (
            "complete" if record["complete"] else
            ("aborted" if fatal_reasons else "incomplete")
        )
        record["cpu"] = {
            "k6": K6_CPUS,
            "postgres": PG_CPUS,
            "app": APP_CPUS,
            "docker_ncpu": getattr(self, "docker_ncpu", None),
            "guest_ncpu": getattr(self, "guest_ncpu", None),
            "pg_affinity": getattr(self, "pg_affinity", None),
            "app_affinity": getattr(self, "app_affinity", None),
            "app_shares_pg": bool(
                getattr(self, "app_affinity", None)
                and self.app_affinity.get("shares_pg")
            ),
            "wsl_scope_note": self.cpu_plan.get("wsl_scope_note") if self.cpu_plan else None,
        }
        record["isolation"] = {
            "compose_project": self.compose_project,
            "compose_network": self.compose_network,
            "volume": self.volume,
            "app_bind": f"127.0.0.1:{self.app_port}",
            "pg_bind": f"127.0.0.1:{self.pg_port}",
            "pg_port_is_5432": self.pg_port == 5432,
            "test_mode": "off",
            "app_binary_sha256": self.app_sha256,
        }
        record["evidence_bytes"] = self.tree_bytes(self.run_dir)
        return record

    def run(self):
        self.prepare_dirs()
        self.preflight()
        self.build_app()
        for index in range(1, self.rounds + 1):
            if self.fatal_reason:
                self.round_records.append(
                    self.not_run_round(index, f"previous safety/environment abort: {self.fatal_reason}")
                )
                continue
            try:
                record = self.run_round(index)
            except Exception as exc:
                record = {
                    "round": index,
                    "round_id": f"{self.base_run_id}r{index}",
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "fatal": True,
                    "fatal_reasons": [f"{type(exc).__name__}: {exc}"],
                    "stages": [],
                    "complete": False,
                }
                self.cleanup()
            self.round_records.append(record)
            if record.get("fatal"):
                self.fatal_reason = "; ".join(record.get("fatal_reasons", [])) or "safety/environment round failure"
                break
        while len(self.round_records) < self.rounds:
            index = len(self.round_records) + 1
            self.round_records.append(
                self.not_run_round(index, f"previous safety/environment abort: {self.fatal_reason}")
            )
        # The binary is not evidence; keep its SHA in the summary and remove the
        # 26 MB build artifact so the evidence budget reflects logs/results.
        try:
            shutil.rmtree(self.build_dir)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        complete = all(record.get("complete") for record in self.round_records)
        incomplete = []
        for record in self.round_records:
            if record.get("not_run"):
                incomplete.append(f"round {record['round']}: not run ({record.get('reason')})")
            elif not record.get("complete"):
                for stage in record.get("stages", []):
                    if stage.get("not_run"):
                        incomplete.append(
                            f"round {record['round']} {stage['stage']}: not run ({stage.get('reason')})"
                        )
                    elif not stage.get("complete"):
                        incomplete.append(
                            f"round {record['round']} {stage['stage']}: "
                            + "; ".join(stage.get("incomplete_reasons", []))
                        )
                if record.get("fatal_reasons"):
                    incomplete.append(
                        f"round {record['round']}: " + "; ".join(record["fatal_reasons"])
                    )
        baseline_eligible = bool(complete and not self.fatal_reason)
        self.summary = {
            "schema_version": 3,
            "kind": "payment_create_benchmark",
            "run_id": self.base_run_id,
            "profile": self.profile,
            "rounds_requested": self.rounds,
            "started_at_utc": self.started_at,
            "finished_at_utc": utc_now(),
            "target": {
                "rps": self.target_rate,
                "duration_s": LOAD_DURATION_S,
                "max_vus": MAX_VUS,
                "pre_allocated_vus": MAX_VUS,
                "max_conns": EXPECTED_MAX_CONNS,
                "max_conns_evidence": self.pool_config,
                "smoke": {
                    "rps": SMOKE_RATE,
                    "duration_s": SMOKE_DURATION_S,
                    "pre_vus": SMOKE_PRE_VUS,
                    "max_vus": SMOKE_MAX_VUS,
                },
                "warmup": {
                    "rps": WARMUP_RATE,
                    "duration_s": WARMUP_DURATION_S,
                    "pre_vus": WARMUP_PRE_VUS,
                    "max_vus": WARMUP_MAX_VUS,
                    "separate_prefix": True,
                    "real_payment_path": True,
                    "requires_success_before_measurement": True,
                },
                "rounds": self.rounds,
                "fresh_database_each_round": True,
                "no_rerun": True,
                "host_port_5432": False,
                "persistence_parameters_changed": False,
                "k6_cpu_sample_interval_ms": int(K6_CPU_SAMPLE_INTERVAL_S * 1000),
            },
            "source": {
                "repo": str(REPO),
                "kind": self.source_info.get("kind"),
                "sha": self.source_info.get("sha", ""),
                "branch": self.source_info.get("branch", ""),
                "dirty": self.source_info.get("dirty"),
                "dirty_count": self.source_info.get("dirty_count"),
                "dirty_entries": self.source_info.get("dirty_entries", []),
            },
            "build": {
                "tags": [BENCHMARK_BUILD_TAG],
                "binary_sha256": self.app_sha256,
                "max_conns": EXPECTED_MAX_CONNS,
            },
            "artifacts_dir": str(self.run_root),
            "cpu_plan": self.cpu_plan,
            "go": {
                "path": str(self.go_path),
                "version": self.go_version,
                "build_env": {
                    "GOPROXY": "off",
                    "GOSUMDB": "off",
                    "GOTOOLCHAIN": "local",
                    "GOWORK": "off",
                    "tags": [BENCHMARK_BUILD_TAG],
                },
            },
            "k6": {
                "path": str(self.k6_path),
                "version": self.k6_version,
                "native_execution": "k6 invoked directly as a local executable under taskset; not a container",
                "home": str(self.k6_home),
                "xdg_config_home": str(self.k6_home / ".config"),
                "config_isolated": True,
                "cpu_group": K6_CPUS,
                "cpu_saturation_ratio": K6_CPU_SATURATION_RATIO,
            },
            "docker": {
                "host": DOCKER_HOST,
                "endpoint_policy": "fixed local default Unix socket; ambient DOCKER_HOST (other than the exact default) and DOCKER_CONTEXT are rejected; all Docker/Compose calls use a sanitized environment",
                "server_version": self.docker_server_version,
            },
            "migration": self.migration_check,
            "postgres": {
                "image": self.pg_image,
                "image_id": self.pg_image_info["image_id"],
                "host_config": getattr(self, "postgres_host_config", None),
                "cpu_group": PG_CPUS,
                "persistence_parameters_changed": False,
                "host_port_5432": False,
            },
            "diagnostics": {
                "pg_sampler": False,
                "commit_tracer": False,
                "periodic_pool_trace": False,
                "app_pool_config": "one benchmark-build startup line only; no per-query or periodic stream",
            },
            "baseline": {
                "profile": PROFILE,
                "rounds_requested": REQUIRED_ROUNDS,
                "eligible": baseline_eligible,
                "note": (
                    "The parent reviewer decides whether the measured rounds form "
                    "the baseline. This runner only marks eligibility."
                ),
            },
            "rounds": self.round_records,
            "stages": self.stage_records,
            "overall": {
                "complete": complete,
                "status": (
                    "passed" if complete else
                    ("aborted" if self.fatal_reason else "incomplete")
                ),
                "baseline_eligible": baseline_eligible,
                "fatal_reason": self.fatal_reason,
                "abort_policy": (
                    "Only safety/environment failures (CPU mask/cpuset, Docker/"
                    "Compose/DB startup or teardown, app readiness/stop, MaxConns "
                    "evidence, credential cleanup) abort later rounds. Ordinary "
                    "measurement failures are preserved and the sequence continues."
                ),
                "incomplete_reasons": incomplete,
                "rounds_run": [r["round"] for r in self.round_records if not r.get("not_run")],
                "rounds_not_run": [r["round"] for r in self.round_records if r.get("not_run")],
                "volumes": self.volumes,
                "volume_retained": True,
                "compose_down_uses_volume_flag": False,
                "cleanup_ok": not self.cleanup_warnings,
                "cleanup_warnings": self.cleanup_warnings,
                "evidence_bytes_total": self.tree_bytes(self.run_root),
                "evidence_policy": {
                    "per_file_cap_bytes": MAX_LOG_BYTES,
                    "sql_output_cap_bytes": MAX_SQL_OUTPUT_BYTES,
                    "no_bpf": True,
                    "raw_logs_capped_head_tail": True,
                },
                "attribution": (
                    "k6 CPU not saturated is not causal permission for PostgreSQL; a single "
                    "runtime thread can still bottleneck. Saturated or missing 1 s k6 windows "
                    "block attribution. Guest CPU affinity is not host physical isolation. "
                    "No BPF/scheduler-delay platform was added."
                ),
                "residual_limitations": [
                    "runner process itself is not pinned; only k6, PostgreSQL and the app are pinned",
                    "WSL2 guest affinity does not prove Windows host physical CPU isolation",
                    "k6 CPU saturation is an aggregate /proc CPU proxy, not a scheduler-delay measurement",
                ],
            },
        }
        return complete

    def print_stage(self, record):
        if record.get("not_run"):
            print(f"{record['stage']}: NOT RUN ({record.get('reason')})")
            return
        cpu = record.get("k6_cpu", {})
        print(
            f"{record['stage']}: target={record['target_rps']}rps "
            f"target_iters={record.get('target_iterations')} "
            f"started={record.get('started_iterations')} "
            f"completed={record.get('completed_iterations')} "
            f"target_ok={record.get('target_met')} "
            f"start_rps={record.get('actual_start_rate')} "
            f"http_rps={record.get('actual_http_rate')} "
            f"http_reqs={record.get('http_reqs')} "
            f"dropped={record.get('dropped_iterations')} "
            f"p95_ms={record.get('p95_ms')} "
            f"p99_ms={record.get('p99_ms')} "
            f"http_failed={record.get('http_req_failed_rate')} "
            f"business_rate={record.get('payment_business_rate')} "
            f"timeout={record.get('timeout')} "
            f"k6_cores={cpu.get('avg_cores')} "
            f"k6_saturated={cpu.get('saturated')} "
            f"sql_ok={(record.get('sql') or {}).get('ok')} "
            f"complete={record.get('complete')} "
            f"fatal={record.get('fatal')}"
        )

    def write_summary(self, error=None):
        if not self.summary:
            self.summary = {
                "schema_version": 3,
                "kind": "payment_create_benchmark",
                "run_id": getattr(self, "base_run_id", ""),
                "profile": getattr(self, "profile", ""),
                "rounds_requested": getattr(self, "rounds", 0),
                "started_at_utc": getattr(self, "started_at", utc_now()),
                "finished_at_utc": utc_now(),
                "source": getattr(self, "source_info", {}),
                "rounds": getattr(self, "round_records", []),
                "overall": {},
            }
        if error:
            self.summary.setdefault("overall", {})
            self.summary["overall"]["status"] = "failed"
            self.summary["overall"]["complete"] = False
            self.summary["error"] = error
        overall = self.summary.setdefault("overall", {})
        overall["volumes"] = getattr(self, "volumes", [])
        overall["volume_retained"] = True
        overall["compose_down_uses_volume_flag"] = False
        overall["cleanup_warnings"] = getattr(self, "cleanup_warnings", [])
        overall["cleanup_ok"] = not overall["cleanup_warnings"]
        if overall["cleanup_warnings"] and not error:
            overall["status"] = "cleanup_failed"
            overall["complete"] = False
        if error or overall["cleanup_warnings"]:
            self.summary.setdefault("baseline", {})["eligible"] = False
        summary_path = self.run_root / "summary.json"
        summary_path.write_text(
            json.dumps(self.summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(summary_path, 0o600)
        except OSError:
            pass
        return summary_path

    def cleanup(self):
        self.stop_app()
        self.stop_compose()
        if getattr(self, "env_dir", None):
            try:
                shutil.rmtree(self.env_dir)
            except FileNotFoundError:
                pass
            except Exception as exc:
                self.cleanup_warnings.append(f"credential directory cleanup failed: {exc}")
        print(f"ARTIFACTS: {self.run_root}")


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Repeatable local 200 RPS benchmark for POST /api/v1/payments."
    )
    parser.add_argument("--migrate-image",
                        help="existing local migration image matching current migrations")
    parser.add_argument("--k6", default="k6",
                        help="local k6 executable (default: PATH k6)")
    parser.add_argument("--go", default="go",
                        help="local Go executable whose version satisfies go.mod (default: PATH go)")
    parser.add_argument("--profile", choices=[PROFILE], default=PROFILE,
                        help="fixed benchmark profile (only 200)")
    parser.add_argument("--rounds", type=int, choices=[REQUIRED_ROUNDS],
                        default=REQUIRED_ROUNDS,
                        help="fixed number of sequential rounds (only 3)")
    parser.add_argument("--artifacts-dir", default=None,
                        help=("evidence root outside the repository "
                              f"(default: {DEFAULT_ARTIFACTS_ROOT})"))
    parser.add_argument("--check-cpu-plan", action="store_true",
                        help="validate the fixed k6/PostgreSQL/app CPU plan and exit without Docker")
    return parser.parse_args(argv)


def main(argv=None):
    os.umask(0o077)
    args = parse_args(argv)
    if args.check_cpu_plan:
        try:
            plan = build_cpu_plan()
        except CpuPlanError as exc:
            print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False))
            print("RESULT: CPU_PLAN_INVALID")
            return 2
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        print("RESULT: CPU_PLAN_OK")
        return 0
    if not args.migrate_image:
        print("ERROR: --migrate-image is required for a load run")
        print("RESULT: PRECONDITION_FAILED")
        return 2

    def _terminate(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _terminate)
    runner = Runner(args)
    exit_code = 0
    error = None
    try:
        complete = runner.run()
        exit_code = 0 if complete else 1
    except SystemExit as exc:
        error = f"interrupted by signal (exit {exc.code})"
        exit_code = exc.code if isinstance(exc.code, int) and exc.code else 1
        raise
    except PreconditionError as exc:
        error = f"precondition failed: {exc}"
        exit_code = 2
    except RunError as exc:
        error = str(exc)
        exit_code = 1
    except Exception as exc:  # keep the artifact directory even on unexpected failures
        error = f"unexpected failure: {type(exc).__name__}: {exc}"
        exit_code = 1
    finally:
        try:
            runner.cleanup()
        finally:
            if getattr(runner, "cleanup_warnings", None) and exit_code == 0:
                exit_code = 1
            summary_path = runner.write_summary(error)
            if error:
                print(f"ERROR: {error}")
            for warning in getattr(runner, "cleanup_warnings", []):
                print(f"CLEANUP WARNING: {warning}")
            overall = runner.summary.get("overall", {})
            if overall.get("status") in ("incomplete", "cleanup_failed", "aborted"):
                for reason in overall.get("incomplete_reasons", []):
                    print(f"INCOMPLETE: {reason}")
            print(f"SUMMARY: {summary_path}")
    if exit_code == 0:
        print("RESULT: PASS")
    elif exit_code == 2:
        print("RESULT: PRECONDITION_FAILED")
    else:
        print("RESULT: INCOMPLETE_OR_FAILED")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
