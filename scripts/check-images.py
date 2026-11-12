#!/usr/bin/env python3
"""T075 paired-image launcher.

Runs one fresh isolated PostgreSQL project, one independent migration, then the
single required image acceptance root.  It never builds, pulls, or downloads
images, and it never starts an application service itself: the Go test owns the
normal/test application lifecycles.

The launcher owns final project cleanup and writes a whitelist-only
``images.json``.  Raw tool output, environment dumps, DSNs, API keys and
database passwords are never archived.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import ci_checks  # noqa: E402

EXPECTED_PLATFORM = "linux/amd64"
EXPECTED_GO = "go1.27.4"
EXPECTED_PG = "16.15"
EXPECTED_GOOSE = "v3.29.0"
POSTGRES_IMAGE = (
    "docker.io/library/postgres:16.15-bookworm"
    "@sha256:5983db9a9c6ad225a48a46cfe2e03e6680b1826662befd0465eb05b34cb1c64e"
)
COMPOSE_FILES = (
    "deploy/compose.yaml",
    "deploy/compose.test.yaml",
    "deploy/compose.app.yaml",
)
REQUIRED_FILES = (
    "go.mod",
    "go.sum",
    *COMPOSE_FILES,
    "deploy/Dockerfile.app",
    "deploy/Dockerfile.migrate",
    "tests/image_test.go",
    "tests/us1_helpers_test.go",
)
BUILD_RECORD_SCHEMA = "payment-hub.image-build-record.v1"
BUILD_RECORD_ENV = "PAYMENT_HUB_IMAGE_BUILD_RECORD"
SOURCE_LABEL = "payment-hub.source-manifest-digest"
IMAGES_SCHEMA = "payment-hub.images.v1"
IMAGE_METADATA_SCHEMA = "payment-hub.image-metadata.v1"
RUNTIME_HARNESS_SCHEMA = "payment-hub.runtime-harness.v1"
REQUIRED_ROOT = "TestImagePairAcceptance"
TEST_PACKAGE = "payment-hub/tests"
MOCK_DB_USER = "payment_hub_test_user"
PREFLIGHT_CLIENT_ID = "image-preflight-client"
FIXTURE_COUNT_QUERY = "\n".join(
    [
        "SELECT count(*) FROM payment_orders;",
        "SELECT count(*) FROM mock_transactions;",
        "SELECT count(*) FROM result_handling_records;",
    ]
)
MAX_JSON_BYTES = 8 * 1024 * 1024
GO_TEST_TIMEOUT_SECONDS = 180
# Fixed bounded outer reap grace after Go's own 180s timeout.  Actual runs need
# the parent's approval for this additional grace before execution.
GO_TEST_KILL_GRACE_SECONDS = 15
GROUP_TERM_GRACE_SECONDS = 2.0
GROUP_KILL_DRAIN_SECONDS = 2.0

BUDGETS = {
    "docker_version_seconds": 15,
    "compose_config_seconds": 60,
    "compose_up_seconds": 120,
    "pg_health_seconds": 30,
    "pg_version_seconds": 15,
    "migration_up_seconds": 60,
    "go_test_external_seconds": GO_TEST_TIMEOUT_SECONDS,
    "fixture_count_seconds": 30,
    "compose_down_seconds": 60,
    "volume_remove_seconds": 30,
    "source_manifest_seconds": 60,
}


class ProcResult(NamedTuple):
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_seconds: float = 0.0
    cleanup_failed: bool = False


class LauncherError(RuntimeError):
    def __init__(self, stage: str, kind: str, message: str = "") -> None:
        self.stage = stage
        self.kind = kind
        self.message = message
        super().__init__(stage + "/" + kind)


_ACTIVE_PROCESS: subprocess.Popen[bytes] | None = None
_ACTIVE_PGID: int | None = None
_CANCEL_REQUESTED = False
MIN_GROUP_DRAIN_SECONDS = 0.5


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_digest(entries: Sequence[Mapping[str, str]]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(str(entry["path"]).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(entry["sha256"]).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def _reset_process_cancel() -> None:
    global _CANCEL_REQUESTED
    _CANCEL_REQUESTED = False


def _verify_group(process: subprocess.Popen[bytes]) -> int | None:
    try:
        pgid = os.getpgid(process.pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None
    return process.pid if pgid == process.pid else None


def _signal_group_id(pgid: int, signum: int) -> bool:
    try:
        os.killpg(pgid, signum)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _group_absent(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().split()
        except OSError:
            continue
        if len(fields) < 5:
            continue
        try:
            member_pgid = int(fields[4])
        except ValueError:
            continue
        if member_pgid == pgid and fields[2] != "Z":
            return False
    return True


def _signal_active(signum: int) -> None:
    if _ACTIVE_PGID is not None:
        _signal_group_id(_ACTIVE_PGID, signum)
        return
    process = _ACTIVE_PROCESS
    if process is not None and process.poll() is None:
        try:
            process.send_signal(signum)
        except (OSError, ValueError):
            pass


def _reap_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=MIN_GROUP_DRAIN_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _terminate_owned_group(
    process: subprocess.Popen[bytes],
    owned_pgid: int | None,
    reap_grace: float,
) -> bool:
    cleanup_failed = False
    if owned_pgid is not None:
        if not _signal_group_id(owned_pgid, signal.SIGTERM) and not _group_absent(
            owned_pgid
        ):
            cleanup_failed = True
    else:
        try:
            process.terminate()
        except OSError:
            cleanup_failed = True

    term_wait = min(GROUP_TERM_GRACE_SECONDS, max(0.0, reap_grace))
    if term_wait > 0:
        try:
            process.communicate(timeout=term_wait)
        except subprocess.TimeoutExpired:
            pass
        except OSError:
            cleanup_failed = True
    if owned_pgid is None or _group_absent(owned_pgid):
        _reap_process(process)
        return cleanup_failed

    if owned_pgid is not None:
        if not _signal_group_id(owned_pgid, signal.SIGKILL):
            cleanup_failed = True
    else:
        try:
            process.kill()
        except OSError:
            cleanup_failed = True
    kill_wait = min(
        GROUP_KILL_DRAIN_SECONDS,
        max(MIN_GROUP_DRAIN_SECONDS, reap_grace - term_wait),
    )
    try:
        process.communicate(timeout=kill_wait)
    except subprocess.TimeoutExpired:
        cleanup_failed = True
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
    except OSError:
        cleanup_failed = True
    if owned_pgid is not None and not _group_absent(owned_pgid):
        cleanup_failed = True
    _reap_process(process)
    return cleanup_failed


def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    input_text: str | None = None,
    reap_grace: float = 0.0,
) -> ProcResult:
    global _ACTIVE_PROCESS, _ACTIVE_PGID, _CANCEL_REQUESTED
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise LauncherError("tool", "start-failed") from exc
    owned_pgid = _verify_group(process)
    if owned_pgid is None:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=MIN_GROUP_DRAIN_SECONDS)
        except (subprocess.TimeoutExpired, OSError):
            pass
        raise LauncherError("process", "group-not-owned")
    _ACTIVE_PROCESS = process
    _ACTIVE_PGID = owned_pgid
    try:
        try:
            stdout, stderr = process.communicate(
                input=input_text.encode("utf-8") if input_text is not None else None,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            _CANCEL_REQUESTED = True
            cleanup_failed = _terminate_owned_group(
                process, owned_pgid, reap_grace
            )
            return ProcResult(
                124,
                "",
                "",
                True,
                time.monotonic() - started,
                cleanup_failed,
            )
        cleanup_failed = False
        if _CANCEL_REQUESTED:
            cleanup_failed = _terminate_owned_group(
                process, owned_pgid, reap_grace
            )
        return ProcResult(
            process.returncode,
            _decode(stdout),
            _decode(stderr),
            False,
            time.monotonic() - started,
            cleanup_failed,
        )
    finally:
        _ACTIVE_PROCESS = None
        _ACTIVE_PGID = None


def _read_api_key(repo: Path) -> str:
    path = repo / "tests/us1_helpers_test.go"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LauncherError("environment", "api-key") from exc
    values = re.findall(
        r'(?m)^\s*(?:const\s+)?us1APIKey\s*=\s*"([^"]+)"',
        text,
    )
    if len(values) != 1 or not values[0]:
        raise LauncherError("environment", "api-key")
    return values[0]


def _compute_source_manifest(
    repo: Path,
    runner: Callable[..., ProcResult],
    env: Mapping[str, str],
) -> tuple[list[dict[str, str]], str]:
    git = shutil.which("git", path=env.get("PATH"))
    if not git:
        raise LauncherError("source", "git-missing")
    result = runner(
        [
            git,
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=repo,
        env=env,
        timeout=BUDGETS["source_manifest_seconds"],
    )
    if result.timed_out or result.exit_code != 0:
        raise LauncherError("source", "manifest")
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in result.stdout.split("\x00"):
        if not raw:
            continue
        relative = raw
        if relative.startswith(("artifacts/", ".git/", ".slim/")):
            continue
        if relative.startswith("/") or "\\" in relative:
            raise LauncherError("source", "manifest")
        parts = relative.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise LauncherError("source", "manifest")
        if relative in seen:
            continue
        seen.add(relative)
        path = repo / relative
        try:
            info = path.lstat()
        except OSError as exc:
            raise LauncherError("source", "manifest") from exc
        if not info.st_mode or path.is_symlink() or not path.is_file():
            raise LauncherError("source", "manifest")
        entries.append({"path": relative, "sha256": sha256_file(path)})
    entries.sort(key=lambda item: item["path"])
    return entries, _manifest_digest(entries)


def _read_build_record(path: Path) -> dict[str, Any]:
    if not path.is_absolute():
        raise LauncherError("image", "build-record")
    try:
        info = path.lstat()
    except OSError as exc:
        raise LauncherError("image", "build-record") from exc
    if path.is_symlink() or not path.is_file() or info.st_size > 64 * 1024:
        raise LauncherError("image", "build-record")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LauncherError("image", "build-record") from exc
    if not isinstance(value, dict):
        raise LauncherError("image", "build-record")
    required = (
        "schema",
        "source_manifest_digest",
        "platform",
        "app_image",
        "app_image_id",
        "migrate_image",
        "migrate_image_id",
    )
    if any(key not in value for key in required):
        raise LauncherError("image", "build-record")
    if value.get("schema") != BUILD_RECORD_SCHEMA:
        raise LauncherError("image", "build-record")
    if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("source_manifest_digest"))):
        raise LauncherError("image", "build-record")
    if value.get("platform") != EXPECTED_PLATFORM:
        raise LauncherError("image", "build-record")
    for key in ("app_image", "migrate_image"):
        _validate_reference(value.get(key), "image")
    for key in ("app_image_id", "migrate_image_id"):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get(key))):
            raise LauncherError("image", "build-record")
    return value


def _validate_reference(value: Any, stage: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise LauncherError(stage, "reference")
    if value.startswith("-") or any(char <= " " or ord(char) == 0x7F for char in value):
        raise LauncherError(stage, "reference")
    return value


def _read_json(path: Path, stage: str, kind: str) -> dict[str, Any]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise LauncherError(stage, kind) from exc
    if path.is_symlink() or not path.is_file() or info.st_size > MAX_JSON_BYTES:
        raise LauncherError(stage, kind)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LauncherError(stage, kind) from exc
    if not isinstance(value, dict):
        raise LauncherError(stage, kind)
    return value


def _env_map(service: Mapping[str, Any]) -> dict[str, str]:
    value = service.get("environment")
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(key): "" if item is None else str(item) for key, item in value.items()}
    if isinstance(value, list):
        result = {}
        for item in value:
            if isinstance(item, str) and "=" in item:
                key, val = item.split("=", 1)
                result[key] = val
        return result
    raise LauncherError("environment", "compose-config")


def _published_port(service: Mapping[str, Any], target: int, expected: str) -> bool:
    ports = service.get("ports")
    if not isinstance(ports, list) or len(ports) != 1:
        return False
    item = ports[0]
    if not isinstance(item, dict):
        return False
    return (
        item.get("target") == target
        and item.get("host_ip") == "127.0.0.1"
        and str(item.get("published")) == str(expected)
        and item.get("protocol", "tcp") == "tcp"
    )


def _has_docker_socket(service: Mapping[str, Any]) -> bool:
    volumes = service.get("volumes")
    if not isinstance(volumes, list):
        return False
    for item in volumes:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source", ""))
        target = str(item.get("target", ""))
        if "docker.sock" in source or "docker.sock" in target:
            return True
    return False


class ImageLauncher:
    def __init__(
        self,
        args: argparse.Namespace,
        *,
        repo: Path = REPO_ROOT,
        runner: Callable[..., ProcResult] | None = None,
        docker_path: str | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        port_provider: Callable[[], int] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.args = args
        self.repo = Path(repo)
        self.runner = runner or run_process
        self.docker_path = docker_path
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.port_provider = port_provider
        self.env = dict(os.environ) if env is None else dict(env)
        self.run_id = secrets.token_hex(16)
        self.project = "payment-hub-test-image-" + self.run_id
        self.database = "payment_hub_test_" + self.run_id
        self.artifact_root = Path(args.artifact_root).expanduser()
        self.run_dir = self.artifact_root / self.run_id
        self.cache_dir = Path(args.cache_dir).expanduser()
        self.go = str(Path(args.go).expanduser())
        self.docker: str | None = None
        self.docker_env: dict[str, str] = {}
        self.env_file: Path | None = None
        self.socket_dir: Path | None = None
        self.socket_path: Path | None = None
        self.api_key: str | None = None
        self.db_password: str | None = None
        self.http_port: int | None = None
        self.pg_port: int | None = None
        self.uid = os.getuid()
        self.gid = os.getgid()
        self.epoch = secrets.token_hex(16)
        self.internal_dsn: str | None = None
        self.host_dsn: str | None = None
        self.source_entries: list[dict[str, str]] = []
        self.source_digest: str | None = None
        self.source_unchanged = False
        self.build_record: dict[str, Any] | None = None
        self.build_record_verified = False
        self.images: dict[str, dict[str, Any]] = {}
        self.images_unchanged = False
        self.go_test: dict[str, Any] = {}
        self.observations: dict[str, Any] = {}
        self.fixture_counts: dict[str, int] = {}
        self.preflight: dict[str, Any] = {}
        self.preflight_conflict = False
        self.compose_attempted = False
        self.pg_container_id: str | None = None
        self.cleanup: dict[str, Any] = {}
        self.commands: list[dict[str, Any]] = []
        self.process_cleanup_failures: list[str] = []
        self.failure: dict[str, str] | None = None
        self.interrupted: str | None = None
        self.repeat_signal = False
        self.cleanup_started = False
        self.report: dict[str, Any] | None = None
        self._report_written = False
        self._artifact_ready = False
        self._secret_markers: tuple[str, ...] = ()

    # ------------------------------------------------------------------
    # Controlled execution
    # ------------------------------------------------------------------

    def _handle_signal(self, signum: int, _frame: object) -> None:
        global _CANCEL_REQUESTED
        if self.cleanup_started:
            self.repeat_signal = True
            return
        self.interrupted = "sigterm" if signum == signal.SIGTERM else "sigint"
        _CANCEL_REQUESTED = True
        _signal_active(signum)

    def _record_command(
        self,
        name: str,
        argv: Sequence[str],
        result: ProcResult,
        budget: float,
        reap_grace: float = 0.0,
    ) -> None:
        for item in argv:
            text = str(item)
            for marker in self._secret_markers:
                if marker and marker in text:
                    raise LauncherError("safety", "argv-secret")
        record = {
            "name": name,
            "argv": [str(item) for item in argv],
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "duration_seconds": round(result.duration_seconds, 3),
            "budget_seconds": budget,
        }
        if reap_grace:
            record["reap_grace_seconds"] = reap_grace
        if result.cleanup_failed:
            record["cleanup"] = "failed"
        self.commands.append(record)

    def _run(
        self,
        stage: str,
        kind: str,
        name: str,
        argv: Sequence[str],
        budget: float,
        *,
        allow_failure: bool = False,
        input_text: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        reap_grace: float = 0.0,
    ) -> ProcResult:
        if self.interrupted is not None and not self.cleanup_started:
            raise LauncherError("interrupted", self.interrupted)
        result = self.runner(
            argv,
            cwd=cwd or self.repo,
            env=env or self.docker_env,
            timeout=budget,
            input_text=input_text,
            reap_grace=reap_grace,
        )
        self._record_command(name, argv, result, budget, reap_grace)
        if result.cleanup_failed:
            self.process_cleanup_failures.append(name)
        if self.interrupted is not None and not self.cleanup_started:
            raise LauncherError("interrupted", self.interrupted)
        if not allow_failure and (result.timed_out or result.exit_code != 0):
            raise LauncherError(stage, kind)
        return result

    # ------------------------------------------------------------------
    # Validation and setup
    # ------------------------------------------------------------------

    def _prepare(self) -> None:
        go = Path(self.go)
        if not go.is_absolute() or not go.is_file() or not os.access(go, os.X_OK):
            raise LauncherError("cli", "go")
        cache = self.cache_dir
        if not cache.is_absolute():
            raise LauncherError("cli", "cache")
        cache = cache.resolve()
        repo = self.repo.resolve()
        if cache == repo or repo in cache.parents:
            raise LauncherError("cli", "cache")
        try:
            cache.mkdir(parents=True, exist_ok=True)
            for name in ("config", "gomod", "gobuild", "gopath", "tmp"):
                (cache / name).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LauncherError("cli", "cache") from exc
        self.cache_dir = cache
        root = self.artifact_root
        if not root.is_absolute():
            raise LauncherError("cli", "artifact-root")
        if root.is_symlink():
            raise LauncherError("cli", "artifact-root")
        root = root.resolve()
        if root.exists():
            if not root.is_dir() or any(root.iterdir()):
                raise LauncherError("cli", "artifact-root")
        else:
            try:
                root.mkdir(parents=True, exist_ok=False)
            except OSError as exc:
                raise LauncherError("cli", "artifact-root") from exc
        self.artifact_root = root
        self.run_dir = root / self.run_id
        try:
            self.run_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        except OSError as exc:
            raise LauncherError("cli", "artifact-root") from exc
        self._artifact_ready = True
        for relative in REQUIRED_FILES:
            path = self.repo / relative
            try:
                info = path.lstat()
            except OSError as exc:
                raise LauncherError("source", "file-missing") from exc
            if path.is_symlink() or not path.is_file() or info.st_size == 0:
                raise LauncherError("source", "file-missing")

    def _manifest(self) -> tuple[list[dict[str, str]], str]:
        entries, digest = _compute_source_manifest(
            self.repo, self.runner, self.env
        )
        return entries, digest

    def _load_inputs(self) -> None:
        app_image = self.env.get("PAYMENT_HUB_APP_IMAGE", "")
        migrate_image = self.env.get("PAYMENT_HUB_MIGRATE_IMAGE", "")
        if not app_image or not migrate_image:
            raise LauncherError("environment", "image-env")
        self.app_image = _validate_reference(app_image, "environment")
        self.migrate_image = _validate_reference(migrate_image, "environment")
        record_path = Path(self.env.get(BUILD_RECORD_ENV, "")).expanduser()
        if not str(record_path):
            raise LauncherError("image", "build-record")
        self.build_record = _read_build_record(record_path)
        self.api_key = _read_api_key(self.repo)
        self.db_password = secrets.token_hex(24)
        self.http_port = self._next_port()
        self.pg_port = self._next_port()
        if self.http_port == self.pg_port:
            self.pg_port = self._next_port()
        self.internal_dsn = self._make_dsn("postgres", 5432)
        self.host_dsn = self._make_dsn("127.0.0.1", self.pg_port)
        self._secret_markers = (
            self.api_key,
            self.db_password,
            "postgres://",
            "postgresql://",
            "PAYMENT_HUB_API_KEY=",
            "PAYMENT_HUB_TEST_DATABASE_URL=",
            "GOOSE_DBSTRING=",
            "PGPASSWORD=",
        )
        self._create_temp_paths()

    def _next_port(self) -> int:
        if self.port_provider is not None:
            value = int(self.port_provider())
            if value <= 0 or value > 65535:
                raise LauncherError("environment", "port")
            return value
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _make_dsn(self, host: str, port: int) -> str:
        query = urllib.parse.urlencode({"sslmode": "disable"})
        return (
            "postgres://"
            + urllib.parse.quote(MOCK_DB_USER, safe="")
            + ":"
            + urllib.parse.quote(self.db_password or "", safe="")
            + "@"
            + host
            + ":"
            + str(port)
            + "/"
            + self.database
            + "?"
            + query
        )

    def _temp_parent(self) -> Path:
        candidate = Path("/tmp/opencode")
        if candidate.is_dir():
            return candidate
        return Path(tempfile.gettempdir())

    def _create_temp_paths(self) -> None:
        parent = self._temp_parent()
        if parent == self.repo.resolve() or self.repo.resolve() in parent.parents:
            raise LauncherError("environment", "temp-path")
        try:
            self.socket_dir = Path(
                tempfile.mkdtemp(prefix="ph-image-socket-", dir=str(parent))
            )
            os.chmod(self.socket_dir, 0o700)
            descriptor, name = tempfile.mkstemp(
                prefix="ph-image-env-",
                suffix=".env",
                dir=str(parent),
            )
            os.fchmod(descriptor, 0o600)
            self.env_file = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(self._env_file_text())
        except OSError as exc:
            raise LauncherError("environment", "temp-path") from exc
        self.socket_path = self.socket_dir / "control.sock"
        if len(str(self.socket_path)) >= 100:
            raise LauncherError("environment", "socket-path")

    def _env_file_text(self) -> str:
        values = {
            "COMPOSE_PROJECT_NAME": self.project,
            "PAYMENT_HUB_POSTGRES_DB": self.database,
            "PAYMENT_HUB_POSTGRES_USER": MOCK_DB_USER,
            "PAYMENT_HUB_POSTGRES_PASSWORD": self.db_password or "",
            "PAYMENT_HUB_TEST_PG_PORT": str(self.pg_port),
            "PAYMENT_HUB_TEST_HTTP_PORT": str(self.http_port),
            "PAYMENT_HUB_APP_IMAGE": self.app_image,
            "PAYMENT_HUB_MIGRATE_IMAGE": self.migrate_image,
            "PAYMENT_HUB_IMAGE_RUN_ID": self.run_id,
            "PAYMENT_HUB_TEST_UID": str(self.uid),
            "PAYMENT_HUB_TEST_GID": str(self.gid),
            "PAYMENT_HUB_TEST_SOCKET_DIR": str(self.socket_dir),
            "PAYMENT_HUB_API_KEY": self.api_key or "",
            "PAYMENT_HUB_CLIENT_ID": PREFLIGHT_CLIENT_ID,
            "PAYMENT_HUB_DATABASE_URL": self.internal_dsn or "",
            "GOOSE_DBSTRING": self.internal_dsn or "",
            "PAYMENT_HUB_HTTP_ADDR": "0.0.0.0:8080",
            "PAYMENT_HUB_TEST_MODE": "off",
            "PAYMENT_HUB_TEST_CONTROL_SOCKET": "/run/ph-test/control.sock",
            "PAYMENT_HUB_TEST_RUN_ID": self.run_id,
            "PAYMENT_HUB_TEST_OBSERVER_EPOCH": self.epoch,
            "PAYMENT_HUB_TEST_GENERATION": "1",
            "PAYMENT_HUB_TEST_CONTROL_VERSION": "1",
            "PAYMENT_HUB_TEST_PROJECT": self.project,
            "PAYMENT_HUB_SOURCE_MANIFEST_DIGEST": self.source_digest or "",
            "TZ": "UTC",
        }
        return "".join(f"{key}={value}\n" for key, value in values.items())

    # ------------------------------------------------------------------
    # Docker / Compose
    # ------------------------------------------------------------------

    def _docker_environment(self) -> dict[str, str]:
        allowed = (
            "PATH",
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "XDG_RUNTIME_DIR",
            "TMPDIR",
            "LANG",
            "LC_ALL",
        )
        return {name: self.env[name] for name in allowed if name in self.env}

    def _docker_preflight(self) -> None:
        self.docker = self.docker_path or shutil.which(
            "docker", path=self.env.get("PATH")
        )
        if not self.docker:
            raise LauncherError("environment", "docker")
        self.docker_env = self._docker_environment()
        ambient_host = self.docker_env.get("DOCKER_HOST", "")
        if ambient_host and not ambient_host.startswith("unix://"):
            raise LauncherError("environment", "remote-daemon")
        self._run(
            "environment",
            "docker-version",
            "docker --version",
            [self.docker, "--version"],
            BUDGETS["docker_version_seconds"],
        )
        context = self._run(
            "environment",
            "docker-context",
            "docker context show",
            [self.docker, "context", "show"],
            BUDGETS["docker_version_seconds"],
        ).stdout.strip()
        if not context:
            raise LauncherError("environment", "docker-context")
        endpoint = self._run(
            "environment",
            "docker-context",
            "docker context inspect",
            [
                self.docker,
                "context",
                "inspect",
                context,
                "--format",
                "{{json .Endpoints.docker.Host}}",
            ],
            BUDGETS["docker_version_seconds"],
        ).stdout.strip()
        try:
            endpoint_value = json.loads(endpoint)
        except json.JSONDecodeError as exc:
            raise LauncherError("environment", "docker-context") from exc
        if not isinstance(endpoint_value, str) or not endpoint_value.startswith(
            "unix://"
        ):
            raise LauncherError("environment", "remote-daemon")
        server = self._run(
            "environment",
            "docker-server",
            "docker version server",
            [self.docker, "version", "--format", "{{json .Server}}"],
            BUDGETS["docker_version_seconds"],
        ).stdout
        try:
            server_value = json.loads(server)
        except json.JSONDecodeError as exc:
            raise LauncherError("environment", "docker-server") from exc
        if (
            not isinstance(server_value, dict)
            or server_value.get("Os") != "linux"
            or server_value.get("Arch") != "amd64"
        ):
            raise LauncherError("environment", "docker-platform")
        security = self._run(
            "environment",
            "docker-security",
            "docker info security",
            [self.docker, "info", "--format", "{{json .SecurityOptions}}"],
            BUDGETS["docker_version_seconds"],
        ).stdout
        try:
            options = json.loads(security)
        except json.JSONDecodeError as exc:
            raise LauncherError("environment", "docker-security") from exc
        if not isinstance(options, list):
            raise LauncherError("environment", "docker-security")
        if any("rootless" in str(item) for item in options):
            raise LauncherError("environment", "rootless")
        self._run(
            "environment",
            "compose-version",
            "docker compose version",
            [self.docker, "compose", "version"],
            BUDGETS["docker_version_seconds"],
        )

    def _compose_argv(self, *arguments: str) -> list[str]:
        return [
            self.docker or "docker",
            "compose",
            "--env-file",
            str(self.env_file),
            "-f",
            COMPOSE_FILES[0],
            "-f",
            COMPOSE_FILES[1],
            "-f",
            COMPOSE_FILES[2],
            "-p",
            self.project,
            "--profile",
            "app",
            "--profile",
            "app-test",
            "--profile",
            "migrate",
            *arguments,
        ]

    def _compose(
        self,
        stage: str,
        kind: str,
        name: str,
        arguments: Sequence[str],
        budget: float,
        *,
        allow_failure: bool = False,
    ) -> ProcResult:
        return self._run(
            stage,
            kind,
            name,
            self._compose_argv(*arguments),
            budget,
            allow_failure=allow_failure,
        )

    def _resource_preflight(self) -> None:
        project_filter = f"label=com.docker.compose.project={self.project}"
        checks = (
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
                [self.docker or "docker", "volume", "ls", "--filter", f"name=^{self.project}_postgres_data$", "--format", "{{.Name}}"],
            ),
            (
                "network-name",
                [self.docker or "docker", "network", "ls", "--filter", f"name=^{self.project}_default$", "--format", "{{.Name}}"],
            ),
        )
        for label, argv in checks:
            result = self._run(
                "environment",
                "preflight-" + label,
                "preflight " + label,
                argv,
                BUDGETS["docker_version_seconds"],
            )
            found = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if found:
                self.preflight_conflict = True
                self.preflight = {"conflict": label, "no_existing_resources": False}
                raise LauncherError("environment", "resource-conflict")
        self.preflight = {"no_existing_resources": True}

    def _compose_config(self) -> None:
        result = self._compose(
            "environment",
            "compose-config",
            "compose config",
            ("config", "--format", "json"),
            BUDGETS["compose_config_seconds"],
        )
        try:
            config = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise LauncherError("environment", "compose-config") from exc
        if not isinstance(config, dict) or config.get("name") != self.project:
            raise LauncherError("environment", "compose-config")
        services = config.get("services")
        if not isinstance(services, dict) or set(services) != {
            "postgres",
            "app",
            "app-test",
            "migrate",
        }:
            raise LauncherError("environment", "compose-config")
        postgres = services["postgres"]
        app = services["app"]
        app_test = services["app-test"]
        migrate = services["migrate"]
        if not all(
            isinstance(item, dict) for item in (postgres, app, app_test, migrate)
        ):
            raise LauncherError("environment", "compose-config")
        volumes = config.get("volumes")
        if not isinstance(volumes, dict) or set(volumes) != {"postgres_data"}:
            raise LauncherError("environment", "compose-config")
        volume = volumes["postgres_data"]
        if (
            not isinstance(volume, dict)
            or volume.get("external") not in (False, None)
            or volume.get("name") != self.project + "_postgres_data"
        ):
            raise LauncherError("environment", "compose-config")
        networks = config.get("networks")
        if not isinstance(networks, dict) or set(networks) != {"default"}:
            raise LauncherError("environment", "compose-config")
        network = networks["default"]
        if (
            not isinstance(network, dict)
            or network.get("external") not in (False, None)
            or network.get("name") != self.project + "_default"
        ):
            raise LauncherError("environment", "compose-config")
        if (
            postgres.get("image") != POSTGRES_IMAGE
            or postgres.get("platform") != EXPECTED_PLATFORM
            or not _published_port(postgres, 5432, str(self.pg_port))
        ):
            raise LauncherError("environment", "compose-config")
        pg_mounts = postgres.get("volumes")
        if not isinstance(pg_mounts, list) or len(pg_mounts) != 1:
            raise LauncherError("environment", "compose-config")
        pg_mount = pg_mounts[0]
        if (
            not isinstance(pg_mount, dict)
            or pg_mount.get("type") != "volume"
            or pg_mount.get("source")
            not in ("postgres_data", self.project + "_postgres_data")
            or pg_mount.get("target") != "/var/lib/postgresql/data"
            or pg_mount.get("read_only") not in (False, None)
        ):
            raise LauncherError("environment", "compose-config")
        if (
            app.get("image") != self.app_image
            or app.get("platform") != EXPECTED_PLATFORM
            or not _published_port(app, 8080, str(self.http_port))
            or _has_docker_socket(app)
        ):
            raise LauncherError("environment", "compose-config")
        if (
            app_test.get("image") != self.app_image
            or app_test.get("platform") != EXPECTED_PLATFORM
            or app_test.get("user") != f"{self.uid}:{self.gid}"
            or not _published_port(app_test, 8080, str(self.http_port))
            or _has_docker_socket(app_test)
        ):
            raise LauncherError("environment", "compose-config")
        app_env = _env_map(app)
        if any(
            key.startswith("PAYMENT_HUB_TEST_") and key != "PAYMENT_HUB_TEST_MODE"
            for key in app_env
        ):
            raise LauncherError("environment", "compose-config")
        app_test_env = _env_map(app_test)
        expected_test = {
            "PAYMENT_HUB_TEST_MODE": "on",
            "PAYMENT_HUB_TEST_CONTROL_SOCKET": "/run/ph-test/control.sock",
            "PAYMENT_HUB_TEST_RUN_ID": self.run_id,
            "PAYMENT_HUB_TEST_OBSERVER_EPOCH": self.epoch,
            "PAYMENT_HUB_TEST_GENERATION": "1",
            "PAYMENT_HUB_TEST_CONTROL_VERSION": "1",
        }
        for key, value in expected_test.items():
            if app_test_env.get(key) != value:
                raise LauncherError("environment", "compose-config")
        mounts = app_test.get("volumes")
        if not isinstance(mounts, list) or not any(
            isinstance(item, dict)
            and item.get("type") == "bind"
            and item.get("source") == str(self.socket_dir)
            and item.get("target") == "/run/ph-test"
            and item.get("read_only") in (False, None)
            for item in mounts
        ):
            raise LauncherError("environment", "compose-config")
        app_mounts = app.get("volumes")
        if isinstance(app_mounts, list) and any(
            isinstance(item, dict) and item.get("target") == "/run/ph-test"
            for item in app_mounts
        ):
            raise LauncherError("environment", "compose-config")
        if migrate.get("image") != self.migrate_image or migrate.get(
            "platform"
        ) != EXPECTED_PLATFORM:
            raise LauncherError("environment", "compose-config")

    # ------------------------------------------------------------------
    # Images and source
    # ------------------------------------------------------------------

    def _inspect_image(self, role: str, reference: str) -> dict[str, Any]:
        result = self._run(
            "image",
            "image-inspect",
            f"inspect {role}",
            [
                self.docker or "docker",
                "image",
                "inspect",
                "--format",
                "{{json .}}",
                reference,
            ],
            BUDGETS["docker_version_seconds"],
            allow_failure=True,
        )
        if result.timed_out or result.exit_code != 0:
            raise LauncherError("image", "missing")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise LauncherError("image", "image-inspect") from exc
        if not isinstance(value, dict):
            raise LauncherError("image", "image-inspect")
        image_id = value.get("Id")
        if not isinstance(image_id, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", image_id
        ):
            raise LauncherError("image", "image-id")
        if value.get("Os") != "linux" or value.get("Architecture") != "amd64":
            raise LauncherError("image", "image-platform")
        config = value.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if not isinstance(labels, dict) or labels.get(SOURCE_LABEL) != self.source_digest:
            raise LauncherError("image", "source-label")
        return value

    def _verify_build_record(self, app: Mapping[str, Any], migrate: Mapping[str, Any]) -> None:
        record = self.build_record
        if record is None:
            raise LauncherError("image", "build-record")
        if (
            record.get("source_manifest_digest") != self.source_digest
            or record.get("platform") != EXPECTED_PLATFORM
            or record.get("app_image") != self.app_image
            or record.get("migrate_image") != self.migrate_image
            or record.get("app_image_id") != app.get("Id")
            or record.get("migrate_image_id") != migrate.get("Id")
        ):
            raise LauncherError("image", "build-record")
        self.build_record_verified = True

    def _inspect_images(self) -> None:
        app = self._inspect_image("app", self.app_image)
        migrate = self._inspect_image("migrate", self.migrate_image)
        if app.get("Id") == migrate.get("Id"):
            raise LauncherError("image", "image-pair")
        self._verify_build_record(app, migrate)
        self.images = {
            "app": {
                "ref": self.app_image,
                "id": app.get("Id"),
                "platform": EXPECTED_PLATFORM,
                "source_label_match": True,
                "registry_digest": None,
                "registry_digest_status": "not_obtained",
                "local_image_id_is_registry_digest": False,
            },
            "migrate": {
                "ref": self.migrate_image,
                "id": migrate.get("Id"),
                "platform": EXPECTED_PLATFORM,
                "source_label_match": True,
                "registry_digest": None,
                "registry_digest_status": "not_obtained",
                "local_image_id_is_registry_digest": False,
            },
        }

    def _verify_images_after(self) -> None:
        app = self._inspect_image("app", self.app_image)
        migrate = self._inspect_image("migrate", self.migrate_image)
        if (
            app.get("Id") != self.images["app"]["id"]
            or migrate.get("Id") != self.images["migrate"]["id"]
        ):
            raise LauncherError("image", "image-changed")
        self.images_unchanged = True

    # ------------------------------------------------------------------
    # PostgreSQL, migration, Go test
    # ------------------------------------------------------------------

    def _start_pg(self) -> None:
        self.compose_attempted = True
        self._compose(
            "environment",
            "compose-up",
            "compose up postgres",
            ("up", "-d", "--no-deps", "--pull", "never", "--no-build", "postgres"),
            BUDGETS["compose_up_seconds"],
        )
        result = self._compose(
            "environment",
            "pg-container",
            "compose ps postgres",
            ("ps", "-q", "postgres"),
            BUDGETS["docker_version_seconds"],
        )
        container = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", container):
            raise LauncherError("environment", "pg-container")
        self.pg_container_id = container

    def _wait_pg(self) -> None:
        if not self.pg_container_id:
            raise LauncherError("environment", "pg-health")
        deadline = self.clock() + BUDGETS["pg_health_seconds"]
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise LauncherError("environment", "pg-timeout")
            result = self._run(
                "environment",
                "pg-health",
                "postgres health",
                [
                    self.docker or "docker",
                    "inspect",
                    "--format",
                    "{{json .State.Health.Status}}",
                    self.pg_container_id,
                ],
                min(BUDGETS["docker_version_seconds"], remaining),
                allow_failure=True,
            )
            if result.timed_out or result.exit_code != 0:
                raise LauncherError("environment", "pg-health")
            try:
                status = json.loads(result.stdout.strip())
            except json.JSONDecodeError:
                status = None
            if status == "healthy":
                break
            if status == "unhealthy":
                raise LauncherError("environment", "pg-unhealthy")
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise LauncherError("environment", "pg-timeout")
            self.sleep(min(1.0, remaining))
        version = self._run(
            "environment",
            "pg-version",
            "postgres version",
            [
                self.docker or "docker",
                "exec",
                self.pg_container_id,
                "postgres",
                "--version",
            ],
            BUDGETS["pg_version_seconds"],
        )
        if EXPECTED_PG not in version.stdout:
            raise LauncherError("environment", "pg-version")

    def _migrate(self) -> None:
        self._compose(
            "migration",
            "migration-failed",
            "migrate up",
            ("run", "--rm", "--no-deps", "--pull", "never", "-T", "migrate", "up"),
            BUDGETS["migration_up_seconds"],
        )

    def _go_environment(self) -> dict[str, str]:
        env = {
            name: self.env[name]
            for name in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
            if name in self.env
        }
        for name in (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "XDG_RUNTIME_DIR",
        ):
            if name in self.docker_env:
                env[name] = self.docker_env[name]
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.cache_dir / "config"),
                "GOPATH": str(self.cache_dir / "gopath"),
                "GOMODCACHE": str(self.cache_dir / "gomod"),
                "GOCACHE": str(self.cache_dir / "gobuild"),
                "GOENV": "off",
                "GOTOOLCHAIN": "local",
                "GOWORK": "off",
                "GOTELEMETRY": "off",
                "GOFLAGS": "",
                "GOPROXY": "off",
                "GOSUMDB": "off",
                "CGO_ENABLED": "1",
                "GOMAXPROCS": "2",
                "TZ": "UTC",
            }
        )
        return env

    def _run_go_test(self) -> None:
        env = self._go_environment()
        env.update(
            {
                "PAYMENT_HUB_APP_IMAGE": self.app_image or "",
                "PAYMENT_HUB_MIGRATE_IMAGE": self.migrate_image or "",
                "PAYMENT_HUB_IMAGE_RUN_ID": self.run_id,
                "PAYMENT_HUB_TEST_PROJECT": self.project,
                "PAYMENT_HUB_TEST_HTTP_PORT": str(self.http_port),
                "PAYMENT_HUB_TEST_PG_PORT": str(self.pg_port),
                "PAYMENT_HUB_TEST_UID": str(self.uid),
                "PAYMENT_HUB_TEST_GID": str(self.gid),
                "PAYMENT_HUB_TEST_SOCKET_DIR": str(self.socket_dir),
                "PAYMENT_HUB_SOURCE_MANIFEST_DIGEST": self.source_digest or "",
                "PAYMENT_HUB_TEST_ARTIFACT_ROOT": str(self.artifact_root),
                "PAYMENT_HUB_IMAGE_ENV_FILE": str(self.env_file),
                "PAYMENT_HUB_TEST_DATABASE_URL": self.host_dsn or "",
                "PAYMENT_HUB_TEST_RUN_ID": self.run_id,
            }
        )
        argv = [
            self.go,
            "test",
            "-mod=readonly",
            "-tags=integration,image",
            "-race",
            "-count=1",
            "-p=1",
            f"-timeout={GO_TEST_TIMEOUT_SECONDS}s",
            "-json",
            "-run",
            "^TestImagePairAcceptance$",
            "./tests",
        ]
        result = self._run(
            "test",
            "timeout",
            "go test image",
            argv,
            GO_TEST_TIMEOUT_SECONDS,
            allow_failure=True,
            env=env,
            reap_grace=GO_TEST_KILL_GRACE_SECONDS,
        )
        if result.timed_out:
            self.go_test = {
                "required_root": REQUIRED_ROOT,
                "result": "fail",
                "package_result": "fail",
                "root_count": 0,
                "subtest_count": 0,
                "problems": ["timeout"],
                "exit_code": 124,
                "timed_out": True,
                "timeout_seconds": GO_TEST_TIMEOUT_SECONDS,
                "reap_grace_seconds": GO_TEST_KILL_GRACE_SECONDS,
                "cleanup_failed": result.cleanup_failed,
            }
            raise LauncherError("test", "timeout")
        parsed = ci_checks.parse_go_test_json(
            result.stdout,
            result.stderr,
            result.exit_code,
            (TEST_PACKAGE,),
            {REQUIRED_ROOT: TEST_PACKAGE},
            allowed_no_test_files=(),
        )
        required = parsed["required_test_results"].get(REQUIRED_ROOT)
        package_status = (
            parsed["package_results"].get(TEST_PACKAGE, {}).get("status")
        )
        problems = list(parsed["problems"])
        self.go_test = {
            "required_root": REQUIRED_ROOT,
            "result": "pass"
            if required == "pass"
            and package_status == "pass"
            and result.exit_code == 0
            and not problems
            else "fail",
            "package_result": package_status,
            "root_count": parsed["root_count"],
            "subtest_count": parsed["subtest_count"],
            "problems": problems,
            "exit_code": result.exit_code,
            "timed_out": False,
            "timeout_seconds": GO_TEST_TIMEOUT_SECONDS,
            "reap_grace_seconds": GO_TEST_KILL_GRACE_SECONDS,
            "cleanup_failed": result.cleanup_failed,
        }
        if self.go_test["result"] != "pass":
            raise LauncherError("test", "required-root")

    def _fixture_counts(self) -> None:
        if not self.pg_container_id or not self.db_password:
            raise LauncherError("test", "fixture-count")
        env = dict(self.docker_env)
        env.update(
            {
                "PGUSER": MOCK_DB_USER,
                "PGPASSWORD": self.db_password,
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
            self.pg_container_id,
            "psql",
            "-X",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            "-",
        ]
        result = self._run(
            "test",
            "fixture-count",
            "fixture rowcounts",
            argv,
            BUDGETS["fixture_count_seconds"],
            allow_failure=True,
            input_text=FIXTURE_COUNT_QUERY,
            env=env,
        )
        if result.timed_out or result.exit_code != 0:
            raise LauncherError("test", "fixture-count")
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != 3 or any(not line.isdigit() for line in lines):
            raise LauncherError("test", "fixture-count")
        counts = [int(line) for line in lines]
        self.fixture_counts = {
            "payment_orders": counts[0],
            "mock_transactions": counts[1],
            "result_handling_records": counts[2],
        }
        if any(counts):
            raise LauncherError("test", "fixture-count")

    # ------------------------------------------------------------------
    # Observation reports
    # ------------------------------------------------------------------

    def _verify_observations(self) -> None:
        metadata = _read_json(
            self.run_dir / "image-metadata.json",
            "observations",
            "metadata",
        )
        expected_metadata = {
            "schema": IMAGE_METADATA_SCHEMA,
            "run_id": self.run_id,
            "source_manifest_digest": self.source_digest,
            "platform": EXPECTED_PLATFORM,
            "app_image": self.app_image,
            "app_image_id": self.images["app"]["id"],
            "migrate_image": self.migrate_image,
            "migrate_image_id": self.images["migrate"]["id"],
            "metadata_verified": True,
            "registry_digest_status": "not_obtained",
            "local_image_id_is_registry_digest": False,
        }
        for key, value in expected_metadata.items():
            if metadata.get(key) != value:
                raise LauncherError("observations", "metadata")
        runtime_path = (
            self.run_dir
            / "scenarios"
            / "image_pair_acceptance"
            / self.run_id
            / "runtime-harness.json"
        )
        runtime = _read_json(runtime_path, "observations", "runtime")
        if (
            runtime.get("schema") != RUNTIME_HARNESS_SCHEMA
            or runtime.get("run_id") != self.run_id
            or runtime.get("collection_complete") is not True
            or runtime.get("faults") != []
        ):
            raise LauncherError("observations", "runtime")
        source = runtime.get("source")
        if not isinstance(source, dict) or source.get("unchanged") is not True:
            raise LauncherError("observations", "source")
        versions = runtime.get("versions")
        if not isinstance(versions, dict):
            raise LauncherError("observations", "runtime")
        if (
            versions.get("go") != EXPECTED_GO
            or versions.get("goos") != "linux"
            or versions.get("goarch") != "amd64"
            or versions.get("postgres") != EXPECTED_PG
            or versions.get("goose") != EXPECTED_GOOSE
        ):
            raise LauncherError("observations", "runtime")
        http = runtime.get("http")
        sql = runtime.get("sql")
        if not isinstance(http, list) or not isinstance(sql, list) or not http or not sql:
            raise LauncherError("observations", "runtime")
        http_generations = {
            item.get("generation") for item in http if isinstance(item, dict)
        }
        sql_generations = {
            item.get("generation") for item in sql if isinstance(item, dict)
        }
        if not {0, 1, 2} <= http_generations or not {0, 1, 2} <= sql_generations:
            raise LauncherError("observations", "runtime")
        observer = runtime.get("observer")
        if not isinstance(observer, dict):
            raise LauncherError("observations", "observer")
        counts = observer.get("counts")
        receipts = observer.get("receipts")
        if (
            not isinstance(counts, dict)
            or counts.get("payment") != 1
            or counts.get("query") != 0
            or not isinstance(receipts, list)
            or len(receipts) != 2
        ):
            raise LauncherError("observations", "observer")
        generations = [item.get("generation") for item in receipts if isinstance(item, dict)]
        if generations != [1, 2]:
            raise LauncherError("observations", "observer")
        self.observations = {
            "metadata": {"path": "image-metadata.json", "result": "pass"},
            "runtime_harness": {
                "path": str(runtime_path.relative_to(self.run_dir)),
                "result": "pass",
                "source_unchanged": True,
                "trusted_observer": True,
            },
            "observer": {
                "payment": 1,
                "query": 0,
                "generations": [1, 2],
            },
        }

    # ------------------------------------------------------------------
    # Cleanup and report
    # ------------------------------------------------------------------

    def _remove_env_file(self, errors: list[str]) -> bool:
        path = self.env_file
        if path is None:
            return True
        try:
            info = path.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            errors.append("cleanup:env-file")
            return False
        try:
            if path.is_symlink() or not path.is_file() or info.st_uid != os.getuid():
                errors.append("cleanup:env-file")
                return False
            if stat.S_IMODE(info.st_mode) != 0o600:
                errors.append("cleanup:env-file")
                return False
            path.unlink()
            return not path.exists()
        except OSError:
            errors.append("cleanup:env-file")
            return False

    def _remove_socket_dir(self, errors: list[str]) -> bool:
        path = self.socket_dir
        if path is None:
            return True
        try:
            info = path.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            errors.append("cleanup:socket-dir")
            return False
        try:
            if path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid():
                errors.append("cleanup:socket-dir")
                return False
            if stat.S_IMODE(info.st_mode) != 0o700:
                errors.append("cleanup:socket-dir")
                return False
            entries = list(path.iterdir())
            if any(entry.name != "control.sock" for entry in entries):
                errors.append("cleanup:socket-dir")
                return False
            for entry in entries:
                if entry.is_symlink() or not entry.is_socket():
                    errors.append("cleanup:socket-dir")
                    return False
                entry.unlink()
            path.rmdir()
            return not path.exists()
        except OSError:
            errors.append("cleanup:socket-dir")
            return False

    def _list_names(self, argv: Sequence[str], stage: str, kind: str) -> list[str]:
        result = self._run(
            stage,
            kind,
            kind,
            argv,
            BUDGETS["docker_version_seconds"],
            allow_failure=True,
        )
        if result.timed_out or result.exit_code != 0:
            raise LauncherError("cleanup", kind)
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _cleanup_compose(
        self, errors: list[str], result: dict[str, Any]
    ) -> None:
        project_filter = f"label=com.docker.compose.project={self.project}"
        try:
            down = self._compose(
                "cleanup",
                "compose-down",
                "compose down",
                ("down", "--remove-orphans"),
                BUDGETS["compose_down_seconds"],
                allow_failure=True,
            )
        except LauncherError:
            errors.append("cleanup:compose-down")
            return
        result["compose_down_exit"] = down.exit_code
        if down.timed_out or down.exit_code != 0:
            errors.append("cleanup:compose-down")
        try:
            containers = self._list_names(
                [
                    self.docker,
                    "ps",
                    "-a",
                    "--filter",
                    project_filter,
                    "--format",
                    "{{.ID}}",
                ],
                "cleanup",
                "cleanup-containers",
            )
            if containers:
                errors.append("cleanup:containers-remain")
            else:
                result["containers_absent"] = True
            networks = self._list_names(
                [
                    self.docker,
                    "network",
                    "ls",
                    "--filter",
                    project_filter,
                    "--format",
                    "{{.Name}}",
                ],
                "cleanup",
                "cleanup-networks",
            )
            if networks:
                errors.append("cleanup:network-remains")
            else:
                result["network_absent"] = True
            volumes = self._list_names(
                [
                    self.docker,
                    "volume",
                    "ls",
                    "--filter",
                    project_filter,
                    "--format",
                    "{{.Name}}",
                ],
                "cleanup",
                "cleanup-volumes",
            )
            if volumes:
                expected_volume = self.project + "_postgres_data"
                if volumes != [expected_volume]:
                    errors.append("cleanup:volume-identity")
                else:
                    inspect = self._run(
                        "cleanup",
                        "volume-inspect",
                        "volume inspect",
                        [
                            self.docker,
                            "volume",
                            "inspect",
                            "--format",
                            "{{json .}}",
                            expected_volume,
                        ],
                        BUDGETS["docker_version_seconds"],
                        allow_failure=True,
                    )
                    owned = False
                    if inspect.exit_code == 0 and not inspect.timed_out:
                        try:
                            value = json.loads(inspect.stdout)
                        except json.JSONDecodeError:
                            value = None
                        labels = (
                            value.get("Labels")
                            if isinstance(value, dict)
                            else None
                        )
                        owned = (
                            isinstance(value, dict)
                            and value.get("Name") == expected_volume
                            and isinstance(labels, dict)
                            and labels.get("com.docker.compose.project")
                            == self.project
                        )
                    if not owned:
                        errors.append("cleanup:volume-identity")
                    else:
                        removed = self._run(
                            "cleanup",
                            "volume-remove",
                            "volume remove",
                            [self.docker, "volume", "rm", expected_volume],
                            BUDGETS["volume_remove_seconds"],
                            allow_failure=True,
                        )
                        if removed.exit_code != 0 or removed.timed_out:
                            errors.append("cleanup:volume-remove")
                        else:
                            remaining = self._list_names(
                                [
                                    self.docker,
                                    "volume",
                                    "ls",
                                    "--filter",
                                    project_filter,
                                    "--format",
                                    "{{.Name}}",
                                ],
                                "cleanup",
                                "cleanup-volumes-after",
                            )
                            if remaining:
                                errors.append("cleanup:volume-remains")
                            else:
                                result["volume_absent"] = True
            else:
                result["volume_absent"] = True
        except LauncherError:
            errors.append("cleanup:verify")

    def _cleanup(self) -> None:
        self.cleanup_started = True
        errors: list[str] = []
        result: dict[str, Any] = {
            "result": "pass",
            "errors": [],
            "compose_down_exit": None,
            "containers_absent": False,
            "network_absent": False,
            "volume_absent": False,
            "env_file_removed": False,
            "socket_dir_removed": False,
            "skipped": False,
        }
        try:
            if self.preflight_conflict:
                result["skipped"] = True
                result["reason"] = "preflight-conflict"
                result["containers_absent"] = True
                result["network_absent"] = True
                result["volume_absent"] = True
            elif self.compose_attempted and self.docker:
                self._cleanup_compose(errors, result)
            else:
                result["containers_absent"] = True
                result["network_absent"] = True
                result["volume_absent"] = True
            if self.process_cleanup_failures:
                errors.append("process-cleanup-failed")
        except LauncherError:
            errors.append("cleanup:compose")
        except Exception:
            errors.append("cleanup:unexpected")
        finally:
            result["env_file_removed"] = self._remove_env_file(errors)
            result["socket_dir_removed"] = self._remove_socket_dir(errors)
            result["errors"] = sorted(set(errors))
            result["result"] = "pass" if not result["errors"] else "fail"
            self.cleanup = result

    def _result_pass(self) -> bool:
        return (
            self.failure is None
            and self.cleanup.get("result") == "pass"
            and self.source_unchanged
            and self.images_unchanged
            and self.go_test.get("result") == "pass"
            and bool(self.observations)
            and self.fixture_counts
            and all(value == 0 for value in self.fixture_counts.values())
        )

    def _build_report(self) -> dict[str, Any]:
        result = "pass" if self._result_pass() else "fail"
        return {
            "schema": IMAGES_SCHEMA,
            "generated_at": utc_now(),
            "run_id": self.run_id,
            "project": self.project,
            "platform": EXPECTED_PLATFORM,
            "result": result,
            "release_candidate": result == "pass",
            "failure": self.failure,
            "source": {
                "manifest_digest": self.source_digest,
                "manifest_file_count": len(self.source_entries),
                "manifest": self.source_entries,
                "unchanged": self.source_unchanged,
            },
            "build_record": {
                "schema": BUILD_RECORD_SCHEMA,
                "verified": self.build_record_verified,
            },
            "images": self.images,
            "pair": {
                "same_source": self.build_record_verified,
                "same_platform": bool(self.images)
                and all(
                    item.get("platform") == EXPECTED_PLATFORM
                    for item in self.images.values()
                ),
                "distinct_ids": bool(self.images)
                and self.images["app"]["id"] != self.images["migrate"]["id"],
                "ids_unchanged_after_run": self.images_unchanged,
            },
            "go_test": self.go_test,
            "observations": self.observations,
            "db_fixture_counts": self.fixture_counts,
            "preflight": self.preflight,
            "cleanup": self.cleanup,
            "budgets": {
                **BUDGETS,
                "go_test_timeout_seconds": GO_TEST_TIMEOUT_SECONDS,
                "go_test_kill_grace_seconds": GO_TEST_KILL_GRACE_SECONDS,
            },
            "commands": self.commands,
        }

    def _write_report(self) -> None:
        if not self._artifact_ready:
            return
        try:
            if not self.run_dir.exists():
                self.run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = self.run_dir / "images.json"
            report = self._build_report()
            text = json.dumps(report, indent=2) + "\n"
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(text)
            except OSError:
                raise LauncherError("report", "write")
            self.report = report
            self._report_written = True
        except LauncherError:
            raise
        except OSError as exc:
            raise LauncherError("report", "write") from exc

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def _run_pipeline(self) -> None:
        self._prepare()
        self.source_entries, self.source_digest = self._manifest()
        self._load_inputs()
        self._docker_preflight()
        self._resource_preflight()
        self._compose_config()
        self._inspect_images()
        self._start_pg()
        self._wait_pg()
        self._migrate()
        self._run_go_test()
        after_entries, after_digest = self._manifest()
        if after_digest != self.source_digest or after_entries != self.source_entries:
            raise LauncherError("source", "changed")
        self.source_unchanged = True
        self._verify_images_after()
        self._verify_observations()
        self._fixture_counts()
        if self.interrupted is not None:
            raise LauncherError("interrupted", self.interrupted)

    def run(self) -> int:
        _reset_process_cancel()
        old_int = signal.signal(signal.SIGINT, self._handle_signal)
        old_term = signal.signal(signal.SIGTERM, self._handle_signal)
        try:
            try:
                self._run_pipeline()
            except LauncherError as exc:
                self.failure = {"stage": exc.stage, "kind": exc.kind}
            except KeyboardInterrupt:
                self.failure = {"stage": "interrupted", "kind": "sigint"}
            finally:
                try:
                    self._cleanup()
                except Exception:
                    self.cleanup = {
                        "result": "fail",
                        "errors": ["cleanup:unexpected"],
                    }
                if self.interrupted is not None and self.failure is None:
                    self.failure = {
                        "stage": "interrupted",
                        "kind": self.interrupted,
                    }
                try:
                    self._write_report()
                except LauncherError as exc:
                    self.failure = {"stage": exc.stage, "kind": exc.kind}
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
        return 0 if self._result_pass() and self._report_written else 1


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "T075 paired-image launcher: run only no-build/no-pull local images, "
            "one fresh PG project, one migration, and TestImagePairAcceptance."
        )
    )
    parser.add_argument("--go", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--artifact-root", required=True)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., ProcResult] | None = None,
    docker_path: str | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    port_provider: Callable[[], int] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    try:
        args = parse_args(argv)
        launcher = ImageLauncher(
            args,
            runner=runner,
            docker_path=docker_path,
            clock=clock,
            sleep=sleep,
            port_provider=port_provider,
            env=env,
        )
        return launcher.run()
    except LauncherError as exc:
        print(f"ERROR: {exc.stage}/{exc.kind}", file=sys.stderr)
        print(f"result: fail stage={exc.stage} kind={exc.kind}")
        return 1
    except Exception:
        print("ERROR: internal/unexpected", file=sys.stderr)
        print("result: fail stage=internal kind=unexpected")
        return 1


if __name__ == "__main__":
    sys.exit(main())
