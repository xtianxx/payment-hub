#!/usr/bin/env python3
"""Fixed T072 check entrypoint.

The driver dispatches one of the fixed scopes ``format``, ``static``, ``unit``,
``db``, ``image`` or ``all``.  It accepts only absolute tool/cache/artifact
paths and never accepts a shell fragment or an arbitrary command.  Existing
launchers own their deadlines and cleanup; this driver does not add a second
outer deadline for them.

Only whitelist facts (argv, exit code, timing, budget, execution status and
fixed report result) are written to ``<artifact-root>/checks.json``.  Raw
stdout/stderr, environment values, DSNs and API keys are never archived.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import ci_checks  # noqa: E402

SCHEMA = "payment-hub.check-entrypoint.v1"
SCOPES = ("format", "static", "unit", "db", "image", "all")
STAGES = ("format", "static", "unit", "db", "image")
# Direct-tool budgets reuse existing budget classes: 30s tool/version checks,
# 120s Go vet/list checks.  The 180s image Go-test budget belongs to the
# check-images launcher and is not applied as an outer deadline here.
FORMAT_TIMEOUT_SECONDS = 30
STATIC_TIMEOUT_SECONDS = 120
IMAGE_TEST_TIMEOUT_SECONDS = 180
IMAGE_SCRIPT = SCRIPT_DIR / "check-images.py"
IMAGE_TEST_FILE = REPO_ROOT / "tests" / "image_test.go"
IMAGE_PREFLIGHT_FILES = (
    (IMAGE_TEST_FILE, "tests-image-missing"),
    (
        REPO_ROOT / "tests" / "us1_helpers_test.go",
        "tests-us1-helpers-missing",
    ),
    (REPO_ROOT / "deploy" / "Dockerfile.app", "dockerfile-app-missing"),
    (
        REPO_ROOT / "deploy" / "Dockerfile.migrate",
        "dockerfile-migrate-missing",
    ),
    (REPO_ROOT / "deploy" / "compose.yaml", "compose-base-missing"),
    (REPO_ROOT / "deploy" / "compose.test.yaml", "compose-test-missing"),
    (REPO_ROOT / "deploy" / "compose.app.yaml", "overlay-app-missing"),
)
IMAGE_ENV_KEYS = (
    "PAYMENT_HUB_APP_IMAGE",
    "PAYMENT_HUB_MIGRATE_IMAGE",
    "PAYMENT_HUB_IMAGE_BUILD_RECORD",
)
DOCKER_ENV_KEYS = (
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "XDG_RUNTIME_DIR",
)
IMAGE_DOCKER_ENV_KEYS = DOCKER_ENV_KEYS
DB_DOCKER_ENV_KEYS = DOCKER_ENV_KEYS
IMAGE_REPORT_SCHEMA = "payment-hub.images.v1"
IMAGE_REQUIRED_ROOT = "TestImagePairAcceptance"
IMAGE_BUILD_RECORD_SCHEMA = "payment-hub.image-build-record.v1"
IMAGE_BUILD_RECORD_KEYS = (
    "schema",
    "source_manifest_digest",
    "platform",
    "app_image",
    "app_image_id",
    "migrate_image",
    "migrate_image_id",
)
# The fixed check-images.py launcher owns its approved 180s Go-test budget and
# 15s reap grace, PostgreSQL setup/cleanup budgets, and final report.  This
# driver only dispatches it and validates the whitelist report subset.
IMAGE_DISPATCH_ENABLED = True
MAX_REPORT_BYTES = 8 * 1024 * 1024
DIRECT_TERM_GRACE_SECONDS = 2.0
DIRECT_KILL_DRAIN_SECONDS = 2.0

BUDGETS: dict[str, tuple[int | None, str]] = {
    "format": (
        FORMAT_TIMEOUT_SECONDS,
        "existing 30s tool/version budget class",
    ),
    "static": (
        STATIC_TIMEOUT_SECONDS,
        "check-db-foundation.BUDGETS.go_vet_seconds",
    ),
    "unit": (
        ci_checks.GO_TEST_BATCH_TIMEOUT_SECONDS,
        "ci_checks.GO_TEST_BATCH_TIMEOUT_SECONDS",
    ),
    "db": (None, "check-db-foundation launcher-owned budgets"),
    "image": (
        IMAGE_TEST_TIMEOUT_SECONDS,
        "user-approved image Go-test budget; check-images launcher-owned",
    ),
}


class CommandResult(NamedTuple):
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_seconds: float = 0.0
    cleanup_failed: bool = False


class CheckError(RuntimeError):
    """A controlled entrypoint failure with a fixed safe category."""

    def __init__(self, stage: str, kind: str, exit_code: int = 1) -> None:
        self.stage = stage
        self.kind = kind
        self.exit_code = exit_code
        super().__init__(stage + "/" + kind)


class StageError(Exception):
    """A controlled stage failure carrying only whitelist facts."""

    def __init__(
        self,
        kind: str,
        exit_code: int = 1,
        commands: Sequence[dict[str, Any]] | None = None,
        metrics: Mapping[str, Any] | None = None,
        problems: Sequence[str] | None = None,
    ) -> None:
        self.kind = kind
        self.exit_code = exit_code
        self.commands = list(commands or [])
        self.metrics = dict(metrics or {})
        self.problems = list(problems or [kind])
        super().__init__(kind)


class Context(NamedTuple):
    go: str
    cache: Path
    root: Path
    env: dict[str, str]


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CheckError("cli", "usage", 2)


_ACTIVE_PROCESS: subprocess.Popen[bytes] | None = None
_ACTIVE_IS_DIRECT = False
_ACTIVE_PGID: int | None = None
_CANCELLED_SIGNAL: int | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def selected_stages(scope: str) -> tuple[str, ...]:
    return STAGES if scope == "all" else (scope,)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _Parser(
        description=(
            "Fixed T072 check entrypoint. Scopes are explicit; no implicit all, "
            "no downloads, no shell fragments."
        )
    )
    parser.add_argument(
        "--scope",
        required=True,
        choices=SCOPES,
        help="One fixed check scope.",
    )
    parser.add_argument(
        "--go",
        required=True,
        help="Absolute path to the prepared Go executable.",
    )
    parser.add_argument(
        "--cache-dir",
        required=True,
        help="Absolute project cache directory outside the repository.",
    )
    parser.add_argument(
        "--artifact-root",
        required=True,
        help="Fresh absolute directory for this run.",
    )
    return parser.parse_args(argv)


def _resolve_go(value: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise CheckError("cli", "go-not-absolute", 2)
    path = path.resolve()
    if not path.is_file():
        raise CheckError("cli", "go-missing", 2)
    if not os.access(path, os.X_OK):
        raise CheckError("cli", "go-not-executable", 2)
    return str(path)


def _prepare_cache(value: str) -> Path:
    cache = Path(value).expanduser()
    if not cache.is_absolute():
        raise CheckError("cli", "cache-dir-not-absolute", 2)
    cache = cache.resolve()
    repo = REPO_ROOT.resolve()
    if cache == repo or repo in cache.parents:
        raise CheckError("cli", "cache-dir-inside-repo", 2)
    if cache.exists() and not cache.is_dir():
        raise CheckError("cli", "cache-dir-not-directory", 2)
    try:
        cache.mkdir(parents=True, exist_ok=True)
        for name in ("config", "gomod", "gobuild", "gopath", "tmp"):
            (cache / name).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CheckError("cli", "cache-dir-create-failed", 2) from exc
    return cache


def _resolve_artifact_root(value: str) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise CheckError("cli", "artifact-root-not-absolute", 2)
    if raw.is_symlink():
        raise CheckError("cli", "artifact-root-symlink", 2)
    root = raw.resolve()
    if root.exists():
        if not root.is_dir():
            raise CheckError("cli", "artifact-root-not-directory", 2)
        try:
            fresh = not any(root.iterdir())
        except OSError as exc:
            raise CheckError("cli", "artifact-root-unreadable", 2) from exc
        if not fresh:
            raise CheckError("cli", "artifact-root-not-fresh", 2)
    else:
        try:
            root.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise CheckError("cli", "artifact-root-create-failed", 2) from exc
    return root


def _base_env(cache: Path) -> dict[str, str]:
    env = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "LANG", "LC_ALL")
        if name in os.environ
    }
    env.update(
        {
            "TMPDIR": str(cache / "tmp"),
            "XDG_CONFIG_HOME": str(cache / "config"),
            "GOPATH": str(cache / "gopath"),
            "GOMODCACHE": str(cache / "gomod"),
            "GOCACHE": str(cache / "gobuild"),
            "GOENV": "off",
            "GOTOOLCHAIN": "local",
            "GOWORK": "off",
            "GOTELEMETRY": "off",
            "GOFLAGS": "",
            "GOPROXY": "off",
            "GOSUMDB": "off",
            "CGO_ENABLED": "1",
            "GOMAXPROCS": "2",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


def _image_env(ctx: Context) -> dict[str, str]:
    """Whitelist image launcher inputs and the same local Docker connection."""
    env = dict(ctx.env)
    for name in IMAGE_ENV_KEYS + IMAGE_DOCKER_ENV_KEYS:
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _db_env(ctx: Context) -> dict[str, str]:
    """Whitelist only the explicit Docker connection for the DB launcher.

    The DB launcher already rejects non-unix DOCKER_HOST values and inspects
    the selected context; forwarding these four overrides keeps rootless and
    isolated daemons on the same connection instead of falling back to HOME's
    default context.  Image-only inputs and other ambient variables stay out.
    """
    env = dict(ctx.env)
    for name in DB_DOCKER_ENV_KEYS:
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def _reset_cancellation() -> None:
    global _CANCELLED_SIGNAL
    _CANCELLED_SIGNAL = None


def _cancelled_signal() -> int | None:
    return _CANCELLED_SIGNAL


def _signal_name(signum: int) -> str:
    if signum == signal.SIGTERM:
        return "sigterm"
    if signum == signal.SIGINT:
        return "sigint"
    return "signal"


def _verify_new_session_group(
    process: subprocess.Popen[bytes],
) -> int | None:
    """Admit a group only when this call created it as a session leader."""
    try:
        pgid = os.getpgid(process.pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None
    return process.pid if pgid == process.pid else None


def _signal_group(pgid: int, signum: int) -> bool:
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
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
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


def _signal_process(
    process: subprocess.Popen[bytes], signum: int
) -> None:
    # Direct tools use the group id admitted and recorded at creation; this
    # remains valid after the leader exits.  Launchers own their cleanup and
    # receive the signal on their PID only.
    if _ACTIVE_IS_DIRECT and _ACTIVE_PGID is not None:
        _signal_group(_ACTIVE_PGID, signum)
        return
    try:
        process.send_signal(signum)
    except (OSError, ValueError):
        pass


def _forward_signal(signum: int, _frame: object) -> None:
    global _CANCELLED_SIGNAL
    if signum in (signal.SIGINT, signal.SIGTERM):
        _CANCELLED_SIGNAL = signum
    process = _ACTIVE_PROCESS
    if process is not None and process.poll() is None:
        _signal_process(process, signum)


def _drain_direct_tool(
    process: subprocess.Popen[bytes], timeout: float
) -> tuple[bytes, bytes, bool]:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return stdout, stderr, True
    except subprocess.TimeoutExpired:
        try:
            process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            pass
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        return b"", b"", process.poll() is not None
    except OSError:
        return b"", b"", False


def _terminate_direct_tool(
    process: subprocess.Popen[bytes],
    owned_pgid: int | None,
) -> tuple[bytes, bytes, bool]:
    """Bounded TERM/KILL/drain for a creation-time admitted direct group."""
    cleanup_failed = False
    if owned_pgid is not None:
        if not _signal_group(owned_pgid, signal.SIGTERM) and not _group_absent(
            owned_pgid
        ):
            cleanup_failed = True
    else:
        try:
            process.terminate()
        except OSError:
            cleanup_failed = True

    try:
        stdout, stderr = process.communicate(
            timeout=DIRECT_TERM_GRACE_SECONDS
        )
    except subprocess.TimeoutExpired:
        stdout, stderr = b"", b""
    except OSError:
        stdout, stderr = b"", b""
        cleanup_failed = True
    if owned_pgid is None or _group_absent(owned_pgid):
        return stdout, stderr, cleanup_failed

    if not _signal_group(owned_pgid, signal.SIGKILL):
        cleanup_failed = True
    stdout, stderr, drained = _drain_direct_tool(
        process, DIRECT_KILL_DRAIN_SECONDS
    )
    if not drained:
        cleanup_failed = True
    if not _group_absent(owned_pgid):
        cleanup_failed = True
    return stdout, stderr, cleanup_failed


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path = REPO_ROOT,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> CommandResult:
    """Run one fixed argv.  Never uses a shell and never records output."""

    global _ACTIVE_PROCESS, _ACTIVE_IS_DIRECT, _ACTIVE_PGID
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise StageError("tool-start-failed") from exc

    old_int = signal.signal(signal.SIGINT, _forward_signal)
    old_term = signal.signal(signal.SIGTERM, _forward_signal)
    _ACTIVE_PROCESS = process
    _ACTIVE_IS_DIRECT = timeout is not None
    try:
        owned_pgid = None
        if timeout is not None:
            owned_pgid = _verify_new_session_group(process)
            if owned_pgid is None:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=DIRECT_KILL_DRAIN_SECONDS)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                raise StageError("tool-group-not-owned")
        _ACTIVE_PGID = owned_pgid
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if timeout is None:
                raise
            # Direct format/static tools only.  Launchers use timeout=None and
            # retain their own bounded supervision and cleanup.
            stdout, stderr, cleanup_failed = _terminate_direct_tool(
                process, owned_pgid
            )
            return CommandResult(
                124,
                _decode(stdout),
                _decode(stderr),
                True,
                time.monotonic() - started,
                cleanup_failed,
            )
        except BaseException:
            if timeout is not None:
                _terminate_direct_tool(process, owned_pgid)
            raise
        return CommandResult(
            process.returncode,
            _decode(stdout),
            _decode(stderr),
            False,
            time.monotonic() - started,
        )
    finally:
        _ACTIVE_PROCESS = None
        _ACTIVE_IS_DIRECT = False
        _ACTIVE_PGID = None
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)


def _remaining(deadline: float, kind: str) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise StageError(kind, exit_code=124)
    return value


def _command_record(argv: Sequence[str], result: CommandResult) -> dict[str, Any]:
    record = {
        "argv": list(argv),
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_seconds": round(result.duration_seconds, 3),
    }
    if result.cleanup_failed:
        record["cleanup"] = "failed"
    return record


def _timeout_stage_error(
    kind: str,
    commands: Sequence[dict[str, Any]],
    result: CommandResult,
) -> StageError:
    problems = [kind]
    metrics: dict[str, Any] = {}
    if result.cleanup_failed:
        problems.append("direct-tool-cleanup-failed")
        metrics["cleanup"] = "failed"
    return StageError(kind, 124, commands, metrics, problems)


def _parse_go_files(stdout: str) -> list[str]:
    files: set[str] = set()
    for raw in stdout.split("\x00"):
        if not raw:
            continue
        if raw.startswith("/") or "\\" in raw or "\x00" in raw:
            raise StageError("format-file-list-invalid")
        parts = raw.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise StageError("format-file-list-invalid")
        files.add(raw)
    if not files:
        raise StageError("format-no-go-files")
    return sorted(files)


def _stage_format(
    ctx: Context, runner: Callable[..., CommandResult]
) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    git = shutil.which("git")
    if not git:
        raise StageError("tool-missing-git")
    gofmt = Path(ctx.go).resolve().with_name("gofmt")
    if not gofmt.is_file() or not os.access(gofmt, os.X_OK):
        raise StageError("tool-missing-gofmt")

    deadline = time.monotonic() + FORMAT_TIMEOUT_SECONDS
    argv = [
        git,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        "*.go",
    ]
    result = runner(
        argv,
        cwd=REPO_ROOT,
        env=ctx.env,
        timeout=_remaining(deadline, "format-timeout"),
    )
    commands.append(_command_record(argv, result))
    if result.timed_out:
        raise _timeout_stage_error("format-timeout", commands, result)
    if result.exit_code != 0:
        raise StageError("format-list-failed", result.exit_code, commands)
    try:
        files = _parse_go_files(result.stdout)
    except StageError as exc:
        raise StageError(
            exc.kind, exc.exit_code, commands, exc.metrics, exc.problems
        ) from exc
    for relative in files:
        if not (REPO_ROOT / relative).is_file():
            raise StageError("format-file-missing", 1, commands)

    argv = [str(gofmt), "-l", "--", *files]
    result = runner(
        argv,
        cwd=REPO_ROOT,
        env=ctx.env,
        timeout=_remaining(deadline, "format-timeout"),
    )
    commands.append(_command_record(argv, result))
    if result.timed_out:
        raise _timeout_stage_error("format-timeout", commands, result)
    if result.exit_code != 0:
        raise StageError("format-tool-failed", result.exit_code, commands)
    dirty = [line for line in result.stdout.splitlines() if line.strip()]
    metrics = {"go_files": len(files), "dirty_files": len(dirty)}
    if dirty:
        raise StageError("format-needed", 1, commands, metrics)
    return {
        "status": "pass",
        "exit_code": 0,
        "commands": commands,
        "metrics": metrics,
        "report": None,
        "problems": [],
    }


def _stage_static(
    ctx: Context, runner: Callable[..., CommandResult]
) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    contract = SCRIPT_DIR / "check-ci-contract.py"
    if not contract.is_file():
        raise StageError("tool-missing-check-ci-contract")
    deadline = time.monotonic() + STATIC_TIMEOUT_SECONDS

    argv = [ctx.go, "vet", "-mod=readonly", "-p=1", "./..."]
    result = runner(
        argv,
        cwd=REPO_ROOT,
        env=ctx.env,
        timeout=_remaining(deadline, "static-timeout"),
    )
    commands.append(_command_record(argv, result))
    if result.timed_out:
        raise _timeout_stage_error("static-timeout", commands, result)
    if result.exit_code != 0:
        raise StageError("static-vet-failed", result.exit_code, commands)

    argv = [sys.executable, "-B", str(contract), "--go", ctx.go]
    result = runner(
        argv,
        cwd=REPO_ROOT,
        env=ctx.env,
        timeout=_remaining(deadline, "static-timeout"),
    )
    commands.append(_command_record(argv, result))
    if result.timed_out:
        raise _timeout_stage_error("static-timeout", commands, result)
    if result.exit_code != 0:
        raise StageError("static-contract-failed", result.exit_code, commands)
    return {
        "status": "pass",
        "exit_code": 0,
        "commands": commands,
        "metrics": {},
        "report": None,
        "problems": [],
    }


def _load_report(path: Path, missing_kind: str, invalid_kind: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise StageError(missing_kind) from exc
    if len(raw) > MAX_REPORT_BYTES:
        raise StageError(invalid_kind)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise StageError(invalid_kind) from exc
    if not isinstance(value, dict):
        raise StageError(invalid_kind)
    return value


def _report_ref(path: Path, root: Path) -> dict[str, str]:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.name
    return {"path": relative, "result": "pass"}


def _validate_unit_report(path: Path, root: Path) -> dict[str, str]:
    value = _load_report(
        path, "unit-report-missing", "unit-report-invalid"
    )
    if (
        value.get("result") != "pass"
        or value.get("problems") != []
        or value.get("exit_code") != 0
        or value.get("list_exit_code") != 0
        or value.get("timed_out") is not False
    ):
        raise StageError("unit-report-not-pass")
    required = value.get("required_test_results")
    if not isinstance(required, dict):
        raise StageError("unit-report-invalid")
    expected = set(ci_checks.DEFAULT_REQUIRED_TESTS)
    if set(required) != expected:
        raise StageError("unit-report-required-root-missing")
    if any(required.get(name) != "pass" for name in expected):
        raise StageError("unit-report-required-root-not-pass")
    return _report_ref(path, root)


def _validate_db_report(path: Path, root: Path) -> dict[str, str]:
    value = _load_report(path, "db-report-missing", "db-report-invalid")
    if value.get("result") != "pass":
        raise StageError("db-report-not-pass")
    tests = value.get("tests")
    if not isinstance(tests, dict):
        raise StageError("db-report-invalid")
    if tests.get("test_exit") != 0 or tests.get("vet_exit") != 0:
        raise StageError("db-report-not-pass")
    required = tests.get("required_test_results")
    if not isinstance(required, dict):
        raise StageError("db-report-invalid")
    expected = set(ci_checks.DB_REQUIRED_TEST_KEYS)
    if set(required) != expected:
        raise StageError("db-report-required-root-missing")
    if any(required.get(name) != "pass" for name in expected):
        raise StageError("db-report-required-root-not-pass")
    cleanup = value.get("cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("result") != "pass":
        raise StageError("db-report-cleanup-not-pass")
    return _report_ref(path, root)


def _stage_unit(
    ctx: Context,
    runner: Callable[..., CommandResult],
    stage_dir: Path,
) -> dict[str, Any]:
    report_dir = stage_dir / "reports"
    argv = [
        sys.executable,
        "-B",
        str(SCRIPT_DIR / "ci_checks.py"),
        "run-default",
        "--go",
        ctx.go,
        "--report-dir",
        str(report_dir),
    ]
    result = runner(argv, cwd=REPO_ROOT, env=ctx.env, timeout=None)
    commands = [_command_record(argv, result)]
    if result.timed_out:
        raise StageError("unit-timeout", 124, commands)
    if result.exit_code != 0:
        raise StageError("unit-run-failed", result.exit_code, commands)
    try:
        report = _validate_unit_report(report_dir / "go-default.json", ctx.root)
    except StageError as exc:
        raise StageError(
            exc.kind, exc.exit_code, commands, exc.metrics, exc.problems
        ) from exc
    return {
        "status": "pass",
        "exit_code": 0,
        "commands": commands,
        "metrics": {},
        "report": report,
        "problems": [],
    }


def _stage_db(
    ctx: Context,
    runner: Callable[..., CommandResult],
    stage_dir: Path,
) -> dict[str, Any]:
    artifact_root = stage_dir / "db-artifacts"
    argv = [
        sys.executable,
        "-B",
        str(SCRIPT_DIR / "check-db-foundation.py"),
        "--go",
        ctx.go,
        "--cache-dir",
        str(ctx.cache),
        "--artifact-root",
        str(artifact_root),
        "--cleanup-owned-resources",
    ]
    result = runner(argv, cwd=REPO_ROOT, env=_db_env(ctx), timeout=None)
    commands = [_command_record(argv, result)]
    if result.timed_out:
        raise StageError("db-timeout", 124, commands)
    if result.exit_code != 0:
        raise StageError("db-launcher-failed", result.exit_code, commands)
    reports = sorted(artifact_root.glob("*/db-foundation.json"))
    if not reports:
        raise StageError("db-report-missing", 1, commands)
    if len(reports) != 1:
        raise StageError("db-report-ambiguous", 1, commands)
    try:
        report = _validate_db_report(reports[0], ctx.root)
    except StageError as exc:
        raise StageError(
            exc.kind, exc.exit_code, commands, exc.metrics, exc.problems
        ) from exc
    return {
        "status": "pass",
        "exit_code": 0,
        "commands": commands,
        "metrics": {},
        "report": report,
        "problems": [],
    }


def _image_build_record_reason() -> str | None:
    value = os.environ.get("PAYMENT_HUB_IMAGE_BUILD_RECORD", "")
    if not value:
        return "payment-hub-image-build-record-missing"
    path = Path(value).expanduser()
    if not path.is_absolute():
        return "image-build-record-not-absolute"
    try:
        info = path.lstat()
    except OSError:
        return "image-build-record-missing"
    if (
        path.is_symlink()
        or not path.is_file()
        or info.st_size <= 0
        or info.st_size > 64 * 1024
    ):
        return "image-build-record-invalid"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "image-build-record-invalid"
    if not isinstance(value, dict):
        return "image-build-record-invalid"
    if value.get("schema") != IMAGE_BUILD_RECORD_SCHEMA:
        return "image-build-record-invalid"
    if any(key not in value for key in IMAGE_BUILD_RECORD_KEYS):
        return "image-build-record-invalid"
    return None


def image_not_ready_reasons() -> list[str]:
    reasons: list[str] = []
    if IMAGE_SCRIPT.is_symlink() or not IMAGE_SCRIPT.is_file():
        reasons.append("script-missing")
    for key in IMAGE_ENV_KEYS:
        if not os.environ.get(key):
            reasons.append(key.lower().replace("_", "-") + "-missing")
    record_reason = _image_build_record_reason()
    if record_reason is not None and record_reason not in reasons:
        reasons.append(record_reason)
    for path, reason in IMAGE_PREFLIGHT_FILES:
        if path.is_symlink() or not path.is_file():
            reasons.append(reason)
    if not IMAGE_DISPATCH_ENABLED:
        reasons.append("launcher-not-wired")
    return reasons


def build_image_command(go: str, cache: Path, stage_dir: Path) -> list[str]:
    return [
        sys.executable,
        "-B",
        str(IMAGE_SCRIPT),
        "--go",
        str(go),
        "--cache-dir",
        str(cache),
        "--artifact-root",
        str(stage_dir / "images-artifacts"),
    ]


def _image_report_ref(path: Path, root: Path) -> dict[str, str]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise StageError("image-report-invalid") from exc
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.name
    return {
        "path": relative,
        "result": "pass",
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _validate_image_report(
    path: Path, root: Path
) -> tuple[dict[str, str], dict[str, Any]]:
    value = _load_report(path, "image-report-missing", "image-report-invalid")
    if value.get("schema") != IMAGE_REPORT_SCHEMA:
        raise StageError("image-report-invalid")
    if value.get("result") != "pass" or value.get("failure") is not None:
        raise StageError("image-report-not-pass")

    source = value.get("source")
    if not isinstance(source, dict) or source.get("unchanged") is not True:
        raise StageError("image-report-source-changed")

    build_record = value.get("build_record")
    if (
        not isinstance(build_record, dict)
        or build_record.get("verified") is not True
    ):
        raise StageError("image-report-build-record-not-verified")

    pair = value.get("pair")
    if not isinstance(pair, dict) or any(
        pair.get(key) is not True
        for key in (
            "same_source",
            "same_platform",
            "distinct_ids",
            "ids_unchanged_after_run",
        )
    ):
        raise StageError("image-report-pair-invalid")

    go_test = value.get("go_test")
    if not isinstance(go_test, dict):
        raise StageError("image-report-go-test-not-pass")
    if go_test.get("required_root") != IMAGE_REQUIRED_ROOT:
        raise StageError("image-report-required-root-missing")
    root_count = go_test.get("root_count")
    if (
        not isinstance(root_count, int)
        or isinstance(root_count, bool)
        or root_count < 1
    ):
        raise StageError("image-report-required-root-missing")
    if (
        go_test.get("result") != "pass"
        or go_test.get("package_result") != "pass"
        or go_test.get("exit_code") != 0
        or go_test.get("timed_out") is not False
    ):
        raise StageError("image-report-go-test-not-pass")

    cleanup = value.get("cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("result") != "pass":
        raise StageError("image-report-cleanup-not-pass")
    if cleanup.get("errors") != []:
        raise StageError("image-report-cleanup-not-pass")
    for key in (
        "containers_absent",
        "network_absent",
        "volume_absent",
        "env_file_removed",
        "socket_dir_removed",
    ):
        if cleanup.get(key) is not True:
            raise StageError("image-report-cleanup-resource-remaining")

    metrics = {
        "image_report_schema": IMAGE_REPORT_SCHEMA,
        "image_result": "pass",
        "build_record_verified": True,
        "source_unchanged": True,
        "pair_same_source": True,
        "pair_same_platform": True,
        "pair_distinct_ids": True,
        "pair_ids_unchanged_after_run": True,
        "go_test_result": "pass",
        "go_test_required_root": IMAGE_REQUIRED_ROOT,
        "go_test_package_result": "pass",
        "go_test_exit_code": 0,
        "go_test_timed_out": False,
        "cleanup_result": "pass",
        "cleanup_resources_absent": True,
    }
    return _image_report_ref(path, root), metrics


def _stage_image(
    ctx: Context,
    runner: Callable[..., CommandResult],
    stage_dir: Path,
) -> dict[str, Any]:
    reasons = image_not_ready_reasons()
    if reasons:
        raise StageError(
            "image-not-ready",
            problems=["image-not-ready"],
            metrics={"not_ready_reasons": reasons},
        )

    artifact_root = stage_dir / "images-artifacts"
    argv = build_image_command(ctx.go, ctx.cache, stage_dir)
    # The launcher owns its approved 180s Go-test budget, 15s reap grace, and
    # PostgreSQL setup/cleanup budgets.  Do not add an outer timeout here.
    result = runner(argv, cwd=REPO_ROOT, env=_image_env(ctx), timeout=None)
    commands = [_command_record(argv, result)]
    if result.timed_out:
        raise StageError("image-timeout", 124, commands)
    if result.exit_code != 0:
        raise StageError("image-launcher-failed", result.exit_code, commands)

    reports = sorted(artifact_root.glob("*/images.json"))
    if not reports:
        raise StageError("image-report-missing", 1, commands)
    if len(reports) != 1:
        raise StageError("image-report-ambiguous", 1, commands)
    try:
        report, metrics = _validate_image_report(reports[0], ctx.root)
    except StageError as exc:
        raise StageError(
            exc.kind, exc.exit_code, commands, exc.metrics, exc.problems
        ) from exc
    return {
        "status": "pass",
        "exit_code": 0,
        "commands": commands,
        "metrics": metrics,
        "report": report,
        "problems": [],
    }


def _budget(name: str) -> tuple[int | None, str]:
    return BUDGETS[name]


def _not_run_entry(name: str) -> dict[str, Any]:
    budget_seconds, budget_source = _budget(name)
    return {
        "status": "not-run",
        "exit_code": None,
        "commands": [],
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "budget_seconds": budget_seconds,
        "budget_source": budget_source,
        "report": None,
        "metrics": {},
        "problems": [],
    }


def _not_ready_entry(name: str, reasons: Sequence[str]) -> dict[str, Any]:
    entry = _not_run_entry(name)
    entry.update(
        {
            "status": "not-ready",
            "problems": ["image-not-ready"],
            "metrics": {"not_ready_reasons": list(reasons)},
        }
    )
    return entry


def _run_stage(
    name: str,
    ctx: Context,
    stage_dir: Path,
    runner: Callable[..., CommandResult],
) -> dict[str, Any]:
    budget_seconds, budget_source = _budget(name)
    entry: dict[str, Any] = {
        "status": "fail",
        "exit_code": 1,
        "commands": [],
        "started_at": utc_now(),
        "finished_at": None,
        "duration_seconds": None,
        "budget_seconds": budget_seconds,
        "budget_source": budget_source,
        "report": None,
        "metrics": {},
        "problems": [],
    }
    started = time.monotonic()
    try:
        if name == "format":
            outcome = _stage_format(ctx, runner)
        elif name == "static":
            outcome = _stage_static(ctx, runner)
        elif name == "unit":
            outcome = _stage_unit(ctx, runner, stage_dir)
        elif name == "db":
            outcome = _stage_db(ctx, runner, stage_dir)
        elif name == "image":
            outcome = _stage_image(ctx, runner, stage_dir)
        else:
            raise StageError("internal-unknown-stage")
        entry.update(outcome)
    except StageError as exc:
        status = "not-ready" if exc.kind == "image-not-ready" else "fail"
        entry.update(
            {
                "status": status,
                "exit_code": None if status == "not-ready" else exc.exit_code,
                "commands": exc.commands,
                "metrics": exc.metrics,
                "problems": exc.problems,
            }
        )
    signal_number = _cancelled_signal()
    if signal_number is not None:
        metrics = dict(entry["metrics"])
        metrics["signal"] = _signal_name(signal_number)
        entry.update(
            {
                "status": "interrupted",
                "exit_code": 128 + signal_number,
                "problems": ["interrupted"],
                "report": None,
                "metrics": metrics,
            }
        )
    entry["finished_at"] = utc_now()
    entry["duration_seconds"] = round(time.monotonic() - started, 3)
    return entry


def _write_checks(root: Path, report: dict[str, Any]) -> None:
    path = root / "checks.json"
    text = json.dumps(report, indent=2) + "\n"
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise CheckError("artifact", "checks-exists", 1) from exc
    except OSError as exc:
        raise CheckError("artifact", "checks-create-failed", 1) from exc
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as exc:
        raise CheckError("artifact", "checks-write-failed", 1) from exc


def _new_report(scope: str, ctx: Context) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "generated_at": utc_now(),
        "scope": scope,
        "result": "fail",
        "artifact_root": str(ctx.root),
        "go": {"name": Path(ctx.go).name},
        "stages": {},
        "problems": [],
    }


def _finish_interrupted(
    report: dict[str, Any],
    stages: Sequence[str],
    root: Path,
) -> int:
    report["problems"] = ["interrupted"]
    for name in stages:
        report["stages"].setdefault(name, _not_run_entry(name))
    _write_checks(root, report)
    return 1


def run(
    args: argparse.Namespace,
    *,
    command_runner: Callable[..., CommandResult] | None = None,
) -> int:
    go = _resolve_go(args.go)
    cache = _prepare_cache(args.cache_dir)
    root = _resolve_artifact_root(args.artifact_root)
    ctx = Context(go=go, cache=cache, root=root, env=_base_env(cache))
    runner = run_command if command_runner is None else command_runner
    report = _new_report(args.scope, ctx)
    stages = selected_stages(args.scope)
    _reset_cancellation()

    old_int = signal.signal(signal.SIGINT, _forward_signal)
    old_term = signal.signal(signal.SIGTERM, _forward_signal)
    try:
        if "image" in stages:
            reasons = image_not_ready_reasons()
            if _cancelled_signal() is not None:
                return _finish_interrupted(report, stages, root)
            if reasons:
                for name in stages:
                    report["stages"][name] = _not_run_entry(name)
                report["stages"]["image"] = _not_ready_entry("image", reasons)
                report["problems"] = ["image-not-ready"]
                _write_checks(root, report)
                return 1

        if _cancelled_signal() is not None:
            return _finish_interrupted(report, stages, root)

        for index, name in enumerate(stages):
            if _cancelled_signal() is not None:
                return _finish_interrupted(report, stages, root)
            stage_dir = root / name
            try:
                stage_dir.mkdir(parents=False, exist_ok=False)
            except FileExistsError:
                entry = _not_run_entry(name)
                entry.update(
                    {
                        "status": "fail",
                        "exit_code": 1,
                        "finished_at": utc_now(),
                        "problems": ["stage-dir-exists"],
                    }
                )
                report["stages"][name] = entry
                for rest in stages[index + 1 :]:
                    report["stages"][rest] = _not_run_entry(rest)
                report["problems"] = ["stage-dir-exists"]
                _write_checks(root, report)
                return 1
            except OSError:
                entry = _not_run_entry(name)
                entry.update(
                    {
                        "status": "fail",
                        "exit_code": 1,
                        "finished_at": utc_now(),
                        "problems": ["stage-dir-create-failed"],
                    }
                )
                report["stages"][name] = entry
                for rest in stages[index + 1 :]:
                    report["stages"][rest] = _not_run_entry(rest)
                report["problems"] = ["stage-dir-create-failed"]
                _write_checks(root, report)
                return 1

            if _cancelled_signal() is not None:
                return _finish_interrupted(report, stages, root)

            entry = _run_stage(name, ctx, stage_dir, runner)
            report["stages"][name] = entry
            if entry["status"] != "pass":
                report["problems"] = list(entry["problems"])
                for rest in stages[index + 1 :]:
                    report["stages"][rest] = _not_run_entry(rest)
                _write_checks(root, report)
                return 1

        if _cancelled_signal() is not None:
            return _finish_interrupted(report, stages, root)
        report["result"] = "pass"
        _write_checks(root, report)
        return 0
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)


def main(
    argv: Sequence[str] | None = None,
    *,
    command_runner: Callable[..., CommandResult] | None = None,
) -> int:
    try:
        args = parse_args(argv)
        return run(args, command_runner=command_runner)
    except CheckError as exc:
        print(f"ERROR: {exc.stage}/{exc.kind}", file=sys.stderr)
        print(f"result: fail stage={exc.stage} kind={exc.kind}")
        return exc.exit_code
    except KeyboardInterrupt:
        print("ERROR: interrupted/sigint", file=sys.stderr)
        print("result: fail stage=interrupted kind=sigint")
        return 1
    except Exception:
        print("ERROR: internal/unexpected", file=sys.stderr)
        print("result: fail stage=internal kind=unexpected")
        return 1


if __name__ == "__main__":
    sys.exit(main())
