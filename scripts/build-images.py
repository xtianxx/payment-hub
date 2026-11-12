#!/usr/bin/env python3
"""T077 local one-shot paired-image builder and original-artifact validator.

The builder requires already-local fixed base images by default.  Only explicit
``--prepare-inputs`` pulls the three fixed base refs and prepares host modules.
It never pushes, authenticates, or publishes.  Dockerfile build steps may fetch
the pinned module versions with checksum verification; no offline build is
claimed.

The build uses fixed Dockerfiles and fixed build flags.  Validation delegates to
``scripts/check-images.py`` on the original built artifacts.  Only whitelist
summaries are written for CI upload.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
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

CHECK_IMAGES_PATH = SCRIPT_DIR / "check-images.py"
CHECK_SETUP_PATH = SCRIPT_DIR / "check-setup.py"
_SPEC = importlib.util.spec_from_file_location("check_images_for_builder", CHECK_IMAGES_PATH)
check_images = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_images)

EXPECTED_PLATFORM = check_images.EXPECTED_PLATFORM
SOURCE_LABEL = check_images.SOURCE_LABEL
BUILD_RECORD_SCHEMA = check_images.BUILD_RECORD_SCHEMA
POSTGRES_IMAGE = check_images.POSTGRES_IMAGE
GO_BUILDER_IMAGE = (
    "docker.io/library/golang:1.27.4-bookworm"
    "@sha256:4fa7c968ba93f1635670522dbb01a2c78150443e1385746924566079f36570ea"
)
DEBIAN_IMAGE = (
    "docker.io/library/debian:bookworm-slim"
    "@sha256:c67e92c8099ba22853ab4cfda7cfb494d253d6012ca82dee98ba8976772af018"
)
BASE_IMAGES = (GO_BUILDER_IMAGE, DEBIAN_IMAGE, POSTGRES_IMAGE)
APP_REPO = "payment-hub-app"
MIGRATE_REPO = "payment-hub-migrate"
APP_DOCKERFILE = "deploy/Dockerfile.app"
MIGRATE_DOCKERFILE = "deploy/Dockerfile.migrate"
OCI_SOURCE_LABEL = "org.opencontainers.image.source"
OCI_SOURCE = "https://github.com/xtianxx/payment-hub"
OCI_REVISION_LABEL = "org.opencontainers.image.revision"
BUILD_BUDGET_SECONDS = 600
BASE_PULL_BUDGET_SECONDS = 300
SOURCE_MANIFEST_SCHEMA = "payment-hub.image-source-manifest.v1"
BUILD_EXECUTION_SCHEMA = "payment-hub.image-build-execution.v1"
BUILD_SUMMARY_SCHEMA = "payment-hub.image-build-summary.v1"
IMAGES_SUMMARY_SCHEMA = "payment-hub.images-safe-summary.v1"
MAX_JSON_BYTES = 8 * 1024 * 1024


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


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def _write_json_once(path: Path, value: Any) -> None:
    text = json.dumps(value, indent=2) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise LauncherError("artifact", "exists") from exc
    except OSError as exc:
        raise LauncherError("artifact", "write") from exc
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as exc:
        raise LauncherError("artifact", "write") from exc


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


class ImageBuilder:
    def __init__(
        self,
        args: argparse.Namespace,
        *,
        repo: Path = REPO_ROOT,
        runner: Callable[..., ProcResult] | None = None,
        delegated_runner: Callable[..., ProcResult] | None = None,
        docker_path: str | None = None,
        clock: Callable[[], float] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.args = args
        self.repo = Path(repo)
        self.runner = runner or check_images.run_process
        self.delegated_runner = delegated_runner
        self.docker_path = docker_path
        self.clock = clock or time.monotonic
        self.env = dict(os.environ) if env is None else dict(env)
        self.go = str(Path(args.go).expanduser())
        self.cache_dir = Path(args.cache_dir).expanduser()
        self.artifact_root = Path(args.artifact_root).expanduser()
        self.prepare_inputs = bool(getattr(args, "prepare_inputs", False))
        self.build_record_path = self.artifact_root / "build-record.json"
        self.source_manifest_path = self.artifact_root / "source-manifest.json"
        self.execution_path = self.artifact_root / "build-execution.json"
        self.validation_root = self.artifact_root / "validation"
        self.upload_dir = self.artifact_root / "upload"
        self.launcher: check_images.ImageLauncher | None = None
        self.docker: str | None = None
        self.docker_env: dict[str, str] = {}
        self.source_entries: list[dict[str, str]] = []
        self.source_digest: str | None = None
        self.git_head: str | None = None
        self.source_dirty = True
        self.source_sha: str | None = None
        self.go_mod_sha256: str | None = None
        self.go_sum_sha256: str | None = None
        self.source_stable = False
        self.builds: list[dict[str, Any]] = []
        self.base_images: list[dict[str, Any]] = []
        self.built: dict[str, dict[str, Any]] = {}
        self.validation: dict[str, Any] = {}
        self.delegated_calls: list[dict[str, Any]] = []
        self.failure: dict[str, str] | None = None
        self.result = "fail"
        self.interrupted: str | None = None
        self.repeat_signal = False
        self._delegated_process: subprocess.Popen[bytes] | None = None
        self.prepare_selected = self.prepare_inputs
        self._artifact_ready = False
        self._record_written = False
        self._upload_written = False
        self._pending_upload_dir: Path | None = None
        self._source_written = False
        self._execution_written = False

    # ------------------------------------------------------------------
    # Validation and source
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
        self.build_record_path = root / "build-record.json"
        self.source_manifest_path = root / "source-manifest.json"
        self.execution_path = root / "build-execution.json"
        self.validation_root = root / "validation"
        self.upload_dir = root / "upload"
        self._artifact_ready = True

    def _git(self, *arguments: str) -> ProcResult:
        git = shutil.which("git", path=self.env.get("PATH"))
        if not git:
            raise LauncherError("source", "git")
        return self.runner(
            [git, *arguments],
            cwd=self.repo,
            env=self.env,
            timeout=60,
            input_text=None,
            reap_grace=0.0,
        )

    def _snapshot(self) -> tuple[list[dict[str, str]], str]:
        entries, digest = check_images._compute_source_manifest(
            self.repo, self.runner, self.env
        )
        head = self._git("rev-parse", "HEAD")
        if head.timed_out or head.exit_code != 0:
            raise LauncherError("source", "git")
        head_value = head.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", head_value):
            raise LauncherError("source", "git")
        status = self._git("status", "--porcelain", "--untracked-files=normal")
        if status.timed_out or status.exit_code != 0:
            raise LauncherError("source", "git")
        dirty = bool(status.stdout.strip())
        go_mod = self.repo / "go.mod"
        go_sum = self.repo / "go.sum"
        if not go_mod.is_file() or not go_sum.is_file():
            raise LauncherError("source", "module-files")
        self.source_entries = entries
        self.source_digest = digest
        self.git_head = head_value
        self.source_dirty = dirty
        self.source_sha = None if dirty else head_value
        self.go_mod_sha256 = sha256_file(go_mod)
        self.go_sum_sha256 = sha256_file(go_sum)
        return entries, digest

    def _verify_source_stable(self) -> None:
        entries, digest = check_images._compute_source_manifest(
            self.repo, self.runner, self.env
        )
        if entries != self.source_entries or digest != self.source_digest:
            raise LauncherError("source", "source-changed")
        if sha256_file(self.repo / "go.mod") != self.go_mod_sha256:
            raise LauncherError("source", "source-changed")
        if sha256_file(self.repo / "go.sum") != self.go_sum_sha256:
            raise LauncherError("source", "source-changed")
        self.source_stable = True

    # ------------------------------------------------------------------
    # Docker preflight / inputs
    # ------------------------------------------------------------------

    def _docker_preflight(self) -> None:
        self.launcher = check_images.ImageLauncher(
            self.args,
            repo=self.repo,
            runner=self.runner,
            docker_path=self.docker_path,
            clock=self.clock,
            env=self.env,
        )
        self.launcher._docker_preflight()
        self.docker = self.launcher.docker
        self.docker_env = self.launcher.docker_env

    def _handle_signal(self, signum: int, _frame: object) -> None:
        self.repeat_signal = self.interrupted is not None
        self.interrupted = "sigterm" if signum == signal.SIGTERM else "sigint"
        check_images._CANCEL_REQUESTED = True
        try:
            check_images._signal_active(signum)
        except Exception:
            pass
        process = self._delegated_process
        if process is not None and process.poll() is None:
            try:
                process.send_signal(signum)
            except (OSError, ValueError):
                pass

    def _run_docker(
        self,
        stage: str,
        kind: str,
        name: str,
        argv: Sequence[str],
        budget: float,
        *,
        allow_failure: bool = False,
    ) -> ProcResult:
        if self.launcher is None:
            raise LauncherError("docker", "not-initialized")
        before = len(self.launcher.process_cleanup_failures)
        result = self.launcher._run(
            stage,
            kind,
            name,
            argv,
            budget,
            allow_failure=allow_failure,
        )
        if len(self.launcher.process_cleanup_failures) > before:
            raise LauncherError(stage, "cleanup")
        if self.interrupted is not None:
            raise LauncherError("interrupted", self.interrupted)
        return result

    def _check_fixed_dockerfiles(self) -> None:
        for relative in (APP_DOCKERFILE, MIGRATE_DOCKERFILE):
            path = self.repo / relative
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise LauncherError("source", "dockerfile") from exc
            refs = re.findall(r"(?m)^FROM\s+(\S+)", text)
            if not refs or set(refs) != {GO_BUILDER_IMAGE, DEBIAN_IMAGE}:
                raise LauncherError("source", "dockerfile")
            if ":latest" in text:
                raise LauncherError("source", "dockerfile")

    def _inspect_base(self, ref: str) -> dict[str, Any] | None:
        result = self._run_docker(
            "prepare",
            "base-inspect",
            "inspect base",
            [
                self.docker or "docker",
                "image",
                "inspect",
                "--format",
                "{{json .}}",
                ref,
            ],
            30,
            allow_failure=True,
        )
        if result.timed_out or result.exit_code != 0:
            return None
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise LauncherError("prepare", "base-inspect") from exc
        if not isinstance(value, dict):
            raise LauncherError("prepare", "base-inspect")
        return value

    def _validate_base(self, ref: str, value: Mapping[str, Any]) -> None:
        image_id = value.get("Id")
        if not isinstance(image_id, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", image_id
        ):
            raise LauncherError("prepare", "base-id")
        if value.get("Os") != "linux" or value.get("Architecture") != "amd64":
            raise LauncherError("prepare", "base-platform")
        digest = ref.split("@", 1)[1]
        repo_digests = value.get("RepoDigests") or []
        if not isinstance(repo_digests, list) or not any(
            isinstance(item, str) and item.endswith("@" + digest)
            for item in repo_digests
        ):
            raise LauncherError("prepare", "base-digest")

    def _check_bases(self) -> None:
        for ref in BASE_IMAGES:
            value = self._inspect_base(ref)
            if value is None:
                raise LauncherError("prepare", "base-image")
            self._validate_base(ref, value)
            self.base_images.append(
                {
                    "ref": ref,
                    "id": value.get("Id"),
                    "platform": EXPECTED_PLATFORM,
                }
            )

    def _prepare_inputs_if_selected(self) -> None:
        if not self.prepare_inputs:
            return
        for ref in BASE_IMAGES:
            pulled = self._run_docker(
                "prepare",
                "base-pull",
                "pull base",
                [
                    self.docker or "docker",
                    "pull",
                    "--platform",
                    "linux/amd64",
                    ref,
                ],
                BASE_PULL_BUDGET_SECONDS,
                allow_failure=True,
            )
            if pulled.timed_out or pulled.exit_code != 0:
                raise LauncherError("prepare", "base-pull")
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
        argv = [
            sys.executable,
            "-B",
            str(CHECK_SETUP_PATH),
            "--go",
            self.go,
            "--cache-dir",
            str(self.cache_dir),
            "--download-modules",
        ]
        result = self._run_delegated(argv, env)
        if result.timed_out or result.exit_code != 0:
            raise LauncherError("prepare", "module-prepare")
        self._verify_source_stable()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _tags(self) -> tuple[str, str]:
        if self.source_digest is None:
            raise LauncherError("source", "manifest")
        prefix = self.source_digest[:12]
        return APP_REPO + ":src-" + prefix, MIGRATE_REPO + ":src-" + prefix

    def _tag_exists(self, tag: str) -> bool:
        result = self._run_docker(
            "build",
            "tag-inspect",
            "inspect tag",
            [
                self.docker or "docker",
                "image",
                "inspect",
                "--format",
                "{{json .}}",
                tag,
            ],
            30,
            allow_failure=True,
        )
        return not result.timed_out and result.exit_code == 0

    def _build_command(self, role: str, dockerfile: str, tag: str) -> list[str]:
        if self.source_digest is None:
            raise LauncherError("source", "manifest")
        argv = [
            self.docker or "docker",
            "build",
            "--platform",
            "linux/amd64",
            "--pull=false",
            "--network",
            "default",
            "--label",
            SOURCE_LABEL + "=" + self.source_digest,
            "--label",
            OCI_SOURCE_LABEL + "=" + OCI_SOURCE,
        ]
        if self.source_sha is not None:
            argv.extend(
                ["--label", OCI_REVISION_LABEL + "=" + self.source_sha]
            )
        argv.extend(["-f", dockerfile, "-t", tag, "."])
        return argv

    def _build_one(self, role: str, dockerfile: str, tag: str) -> None:
        started = self.clock()
        result = self._run_docker(
            "build",
            "build-" + role,
            "build " + role,
            self._build_command(role, dockerfile, tag),
            BUILD_BUDGET_SECONDS,
            allow_failure=True,
        )
        elapsed = round(self.clock() - started, 3)
        entry = {
            "role": role,
            "image": tag,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "duration_seconds": elapsed,
            "budget_seconds": BUILD_BUDGET_SECONDS,
            "cleanup_failed": result.cleanup_failed,
        }
        self.builds.append(entry)
        if result.timed_out or result.exit_code != 0:
            raise LauncherError("build", "build")
        if result.cleanup_failed:
            raise LauncherError("build", "cleanup")
        if self.launcher is None:
            raise LauncherError("build", "not-initialized")
        self.launcher.source_digest = self.source_digest
        value = self.launcher._inspect_image(role, tag)
        entry["id"] = value.get("Id")
        entry["platform"] = EXPECTED_PLATFORM
        entry["source_label_match"] = True
        self.built[role] = {
            "ref": tag,
            "id": value.get("Id"),
            "platform": EXPECTED_PLATFORM,
        }

    # ------------------------------------------------------------------
    # Delegated validation
    # ------------------------------------------------------------------

    def _run_delegated(
        self, argv: Sequence[str], env: Mapping[str, str]
    ) -> ProcResult:
        if self.delegated_runner is not None:
            result = self.delegated_runner(argv, self.repo, env)
        else:
            started = time.monotonic()
            try:
                process = subprocess.Popen(
                    list(argv),
                    cwd=str(self.repo),
                    env=dict(env),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except OSError as exc:
                raise LauncherError("delegated", "start") from exc
            self._delegated_process = process
            try:
                stdout, stderr = process.communicate()
            finally:
                self._delegated_process = None
            result = ProcResult(
                process.returncode,
                _decode(stdout),
                _decode(stderr),
                False,
                time.monotonic() - started,
            )
        self.delegated_calls.append(
            {
                "argv": [str(item) for item in argv],
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_seconds": round(result.duration_seconds, 3),
            }
        )
        if self.interrupted is not None:
            raise LauncherError("interrupted", self.interrupted)
        return result

    def _validation_env(self) -> dict[str, str]:
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
                "PAYMENT_HUB_APP_IMAGE": self.built["app"]["ref"],
                "PAYMENT_HUB_MIGRATE_IMAGE": self.built["migrate"]["ref"],
                "PAYMENT_HUB_IMAGE_BUILD_RECORD": str(self.build_record_path),
            }
        )
        return env

    def _run_validation(self) -> None:
        if self.validation_root.exists():
            raise LauncherError("validation", "root")
        argv = [
            sys.executable,
            "-B",
            str(CHECK_IMAGES_PATH),
            "--go",
            self.go,
            "--cache-dir",
            str(self.cache_dir),
            "--artifact-root",
            str(self.validation_root),
        ]
        result = self._run_delegated(argv, self._validation_env())
        self.validation = {
            "argv": argv,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "duration_seconds": round(result.duration_seconds, 3),
        }
        if result.timed_out or result.exit_code != 0:
            raise LauncherError("validation", "validation")

    def _validation_report(self) -> Path:
        reports = sorted(self.validation_root.glob("*/images.json"))
        if len(reports) != 1:
            raise LauncherError("validation", "report")
        return reports[0]

    def _verify_validation(self) -> None:
        path = self._validation_report()
        report = _read_json(path, "validation", "report")
        if (
            report.get("result") != "pass"
            or not isinstance(report.get("cleanup"), dict)
            or report["cleanup"].get("result") != "pass"
            or not isinstance(report.get("source"), dict)
            or report["source"].get("unchanged") is not True
            or not isinstance(report.get("pair"), dict)
            or not all(
                report["pair"].get(key) is True
                for key in (
                    "same_source",
                    "same_platform",
                    "distinct_ids",
                    "ids_unchanged_after_run",
                )
            )
            or not isinstance(report.get("images"), dict)
        ):
            raise LauncherError("validation", "report")
        images = report["images"]
        if (
            not isinstance(images.get("app"), dict)
            or not isinstance(images.get("migrate"), dict)
            or images["app"].get("id") != self.built["app"]["id"]
            or images["migrate"].get("id") != self.built["migrate"]["id"]
            or report["source"].get("manifest_digest") != self.source_digest
        ):
            raise LauncherError("validation", "validation-mismatch")
        self.validation["report_path"] = str(path)
        self.validation["report_sha256"] = sha256_file(path)
        self.validation["result"] = "pass"
        self.validation["run_id"] = report.get("run_id")

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------

    def _write_build_record(self) -> None:
        if self.built["app"]["id"] == self.built["migrate"]["id"]:
            raise LauncherError("build", "pair")
        record = {
            "schema": BUILD_RECORD_SCHEMA,
            "source_manifest_digest": self.source_digest,
            "platform": EXPECTED_PLATFORM,
            "app_image": self.built["app"]["ref"],
            "app_image_id": self.built["app"]["id"],
            "migrate_image": self.built["migrate"]["ref"],
            "migrate_image_id": self.built["migrate"]["id"],
            "source_sha": self.source_sha,
            "source_dirty": self.source_dirty,
            "git_head": self.git_head,
            "build_once_per_image": True,
        }
        _write_json_once(self.build_record_path, record)
        self._record_written = True

    def _write_source_manifest(self) -> None:
        if not self._artifact_ready or not self.source_entries:
            return
        _write_json_once(
            self.source_manifest_path,
            {
                "schema": SOURCE_MANIFEST_SCHEMA,
                "manifest_digest": self.source_digest,
                "files": self.source_entries,
                "git_head": self.git_head,
                "source_dirty": self.source_dirty,
                "source_sha": self.source_sha,
                "go_mod_sha256": self.go_mod_sha256,
                "go_sum_sha256": self.go_sum_sha256,
            },
        )
        self._source_written = True

    def _write_execution(
        self, result: str, upload_committed: bool
    ) -> None:
        if not self._artifact_ready:
            return
        commands = list(self.launcher.commands) if self.launcher else []
        commands.extend(self.delegated_calls)
        _write_json_once(
            self.execution_path,
            {
                "schema": BUILD_EXECUTION_SCHEMA,
                "generated_at": utc_now(),
                "platform": EXPECTED_PLATFORM,
                "args": {
                    "go": self.go,
                    "cache_dir": str(self.cache_dir),
                    "artifact_root": str(self.artifact_root),
                },
                "prepare_selected": self.prepare_selected,
                "source": {
                    "manifest_digest": self.source_digest,
                    "git_head": self.git_head,
                    "source_dirty": self.source_dirty,
                    "source_sha": self.source_sha,
                    "go_mod_sha256": self.go_mod_sha256,
                    "go_sum_sha256": self.go_sum_sha256,
                    "stable": self.source_stable,
                },
                "source_stable": self.source_stable,
                "base_images": self.base_images,
                "builds": self.builds,
                "validation": self.validation,
                "failure": self.failure,
                "cleanup_failed": any(
                    item.get("cleanup_failed") for item in self.builds
                ),
                "interrupted": self.interrupted,
                "upload_committed": upload_committed,
                "result": result,
                "commands": commands,
            },
        )
        self._execution_written = True

    def _prepare_upload(self) -> None:
        if self.validation.get("result") != "pass":
            raise LauncherError("upload", "validation")
        try:
            pending = Path(
                tempfile.mkdtemp(prefix=".upload-", dir=str(self.artifact_root))
            )
            os.chmod(pending, 0o700)
        except OSError as exc:
            raise LauncherError("upload", "create") from exc
        self._pending_upload_dir = pending
        build_summary = {
            "schema": BUILD_SUMMARY_SCHEMA,
            "generated_at": utc_now(),
            "result": "pass",
            "source_sha": self.source_sha,
            "source_dirty": self.source_dirty,
            "git_head": self.git_head,
            "source_manifest_digest": self.source_digest,
            "platform": EXPECTED_PLATFORM,
            "app_image": self.built["app"]["ref"],
            "app_image_id": self.built["app"]["id"],
            "migrate_image": self.built["migrate"]["ref"],
            "migrate_image_id": self.built["migrate"]["id"],
            "registry_digest": None,
            "local_image_id_is_registry_digest": False,
            "prepare_selected": self.prepare_selected,
            "build_record_sha256": sha256_file(self.build_record_path),
            "validation_report_sha256": self.validation["report_sha256"],
        }
        report = _read_json(
            Path(self.validation["report_path"]), "upload", "report"
        )
        images_summary = {
            "schema": IMAGES_SUMMARY_SCHEMA,
            "generated_at": utc_now(),
            "result": "pass",
            "run_id": report.get("run_id"),
            "source_manifest_digest": self.source_digest,
            "source_unchanged": True,
            "platform": EXPECTED_PLATFORM,
            "images": {
                "app": {
                    "ref": self.built["app"]["ref"],
                    "id": self.built["app"]["id"],
                    "platform": EXPECTED_PLATFORM,
                },
                "migrate": {
                    "ref": self.built["migrate"]["ref"],
                    "id": self.built["migrate"]["id"],
                    "platform": EXPECTED_PLATFORM,
                },
            },
            "pair": {
                "same_source": True,
                "same_platform": True,
                "distinct_ids": True,
                "ids_unchanged_after_run": True,
            },
            "cleanup": {"result": "pass"},
            "validation_report_sha256": self.validation["report_sha256"],
        }
        try:
            _write_json_once(
                self._pending_upload_dir / "images-summary.json",
                images_summary,
            )
            _write_json_once(
                self._pending_upload_dir / "build-summary.json",
                build_summary,
            )
        except LauncherError:
            self._remove_pending_upload()
            raise

    def _remove_pending_upload(self) -> None:
        pending = self._pending_upload_dir
        self._pending_upload_dir = None
        if pending is None:
            return
        self._remove_owned_upload_dir(pending)

    def _remove_owned_upload_dir(self, path: Path) -> None:
        try:
            info = path.lstat()
        except OSError:
            return
        if path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid():
            return
        for name in ("images-summary.json", "build-summary.json"):
            target = path / name
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                return
        try:
            path.rmdir()
        except OSError:
            pass

    def _commit_upload(self) -> None:
        pending = self._pending_upload_dir
        if pending is None:
            raise LauncherError("upload", "pending")
        if self.upload_dir.exists():
            self._remove_pending_upload()
            raise LauncherError("upload", "exists")
        try:
            os.replace(pending, self.upload_dir)
        except OSError as exc:
            self._remove_pending_upload()
            raise LauncherError("upload", "commit") from exc
        self._pending_upload_dir = None
        self._upload_written = True

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def _run_pipeline(self) -> None:
        self._prepare()
        self._snapshot()
        self._docker_preflight()
        self._prepare_inputs_if_selected()
        self._check_fixed_dockerfiles()
        self._check_bases()
        app_tag, migrate_tag = self._tags()
        if self._tag_exists(app_tag) or self._tag_exists(migrate_tag):
            raise LauncherError("build", "tag-exists")
        self._build_one("app", APP_DOCKERFILE, app_tag)
        self._verify_source_stable()
        self._build_one("migrate", MIGRATE_DOCKERFILE, migrate_tag)
        self._verify_source_stable()
        if self.built["app"]["id"] == self.built["migrate"]["id"]:
            raise LauncherError("build", "pair")
        self._write_build_record()
        self._run_validation()
        self._verify_validation()
        self._verify_source_stable()
        if self.interrupted is not None:
            raise LauncherError("interrupted", self.interrupted)
        self._write_source_manifest()
        self._prepare_upload()
        self._commit_upload()
        self._write_execution("pass", True)
        self.result = "pass"

    def run(self) -> int:
        check_images._reset_process_cancel()
        old_int = signal.signal(signal.SIGINT, self._handle_signal)
        old_term = signal.signal(signal.SIGTERM, self._handle_signal)
        try:
            try:
                self._run_pipeline()
            except LauncherError as exc:
                self.failure = {"stage": exc.stage, "kind": exc.kind}
            except Exception:
                self.failure = {"stage": "internal", "kind": "unexpected"}
            finally:
                if self.interrupted is not None and self.failure is None:
                    self.failure = {
                        "stage": "interrupted",
                        "kind": self.interrupted,
                    }
                if not self._source_written:
                    try:
                        self._write_source_manifest()
                    except LauncherError as exc:
                        self.failure = {
                            "stage": exc.stage,
                            "kind": exc.kind,
                        }
                if self.failure is not None and self._upload_written:
                    self._remove_owned_upload_dir(self.upload_dir)
                    self._upload_written = False
                self._remove_pending_upload()
                if not self._execution_written:
                    try:
                        self._write_execution(
                            "pass"
                            if self.failure is None and self._upload_written
                            else "fail",
                            self._upload_written,
                        )
                    except LauncherError as exc:
                        self.failure = {
                            "stage": exc.stage,
                            "kind": exc.kind,
                        }
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
        if self.failure is not None or self.interrupted is not None:
            self.result = "fail"
        return (
            0
            if self.failure is None
            and self.result == "pass"
            and self._upload_written
            and self._execution_written
            else 1
        )

    def print_summary(self) -> None:
        print(
            "build-record-path: "
            + (str(self.build_record_path) if self._record_written else "not-written")
        )
        print(
            "summary-dir: "
            + (str(self.upload_dir) if self._upload_written else "not-written")
        )
        print("result: " + self.result)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the fixed paired images once from already-local bases and "
            "validate the original artifacts with check-images.py."
        )
    )
    parser.add_argument("--go", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument(
        "--prepare-inputs",
        action="store_true",
        help=(
            "Explicitly pull the three fixed base images and run the existing "
            "check-setup.py module preparation; default does not pull bases or "
            "prepare host modules. Dockerfile builds may fetch pinned modules."
        ),
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., ProcResult] | None = None,
    delegated_runner: Callable[..., ProcResult] | None = None,
    docker_path: str | None = None,
    clock: Callable[[], float] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    args = parse_args(argv)
    builder = ImageBuilder(
        args,
        runner=runner,
        delegated_runner=delegated_runner,
        docker_path=docker_path,
        clock=clock,
        env=env,
    )
    code = builder.run()
    builder.print_summary()
    return code


if __name__ == "__main__":
    sys.exit(main())
