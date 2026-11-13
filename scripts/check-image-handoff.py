#!/usr/bin/env python3
"""Same-job paired-image save/load handoff preflight.

This script is deliberately not a release handoff generator.  It starts from
the already accepted builder pair, verifies the builder record, the unique
accepted images report, and the current source identity, saves the two original
image IDs once into a private archive, proves that both originals are absent
from an explicitly isolated runner-local Docker daemon, loads the archive once,
and verifies the loaded identities.

It never builds, pulls, pushes, prunes, or evaluates a release workflow
context.  The only uploaded artifact is the fixed whitelist summary at
``<build-root>/upload/handoff-preflight.safe.json``.  The private ``pair.tar``
is never uploaded.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

_RELEASE_SPEC = importlib.util.spec_from_file_location(
    "release_for_image_handoff", SCRIPT_DIR / "release.py"
)
if _RELEASE_SPEC is None or _RELEASE_SPEC.loader is None:
    raise SystemExit("release.py is unavailable")
release = importlib.util.module_from_spec(_RELEASE_SPEC)
sys.modules.setdefault(_RELEASE_SPEC.name, release)
_RELEASE_SPEC.loader.exec_module(release)

# release.py loads check-images.py with the module name ``check_images``.
check_images = release.check_images

SUMMARY_FILE = "handoff-preflight.safe.json"
SUMMARY_SCHEMA = "payment-hub.image-handoff-preflight.v1"
SOURCE_MANIFEST_SCHEMA = "payment-hub.image-source-manifest.v1"
SOURCE_MANIFEST_FILE = "source-manifest.json"
VALIDATION_DIR = "validation"
EXPECTED_DOCKER_SERVER_VERSION = "29.9.3"
CONTAINERD_DRIVER_STATUS = "driver-type=io.containerd.snapshotter.v1"
DOCKER_DRIVER_FORMAT = (
    r'{{range .DriverStatus}}{{index . 0}}={{index . 1}}{{"\n"}}{{end}}'
)
ROLES = ("app", "migrate")

GIT_TIMEOUT_SECONDS = 30
DOCKER_VERSION_TIMEOUT_SECONDS = 15
IMAGE_INSPECT_TIMEOUT_SECONDS = 30
IMAGE_SAVE_TIMEOUT_SECONDS = 300
IMAGE_REMOVE_TIMEOUT_SECONDS = 60
IMAGE_LOAD_TIMEOUT_SECONDS = 300
IMAGE_LIST_TIMEOUT_SECONDS = 30
PS_TIMEOUT_SECONDS = 30
SOURCE_MANIFEST_MAX_BYTES = 8 * 1024 * 1024
CANCEL_TERM_GRACE_SECONDS = check_images.GROUP_TERM_GRACE_SECONDS
CANCEL_ACTIVE_WAIT_SECONDS = 0.5
CANCEL_DRAIN_SECONDS = check_images.MIN_GROUP_DRAIN_SECONDS + 0.5

CHECK_NAMES = (
    "git_clean",
    "source_manifest",
    "build_record",
    "accepted_report",
    "daemon_isolation",
    "daemon",
    "original_images",
    "save",
    "archive",
    "ownership",
    "remove",
    "absence",
    "load",
    "loaded_images",
    "source_unchanged",
)


class HandoffError(RuntimeError):
    """A fixed, non-dynamic failure classification."""

    def __init__(self, stage: str, kind: str) -> None:
        self.stage = stage
        self.kind = kind
        super().__init__(f"{stage}/{kind}")


class HandoffPreflight:
    def __init__(
        self,
        args: argparse.Namespace,
        *,
        repo: Path = REPO_ROOT,
        runner: Callable[..., check_images.ProcResult] | None = None,
        docker_path: str | None = None,
        git_path: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.args = args
        self.repo = Path(repo)
        self.runner = runner or check_images.run_process
        self.docker_path = docker_path
        self.git_path = git_path
        self.env = dict(os.environ) if env is None else dict(env)

        self.build_root: Path | None = None
        self.artifact_root: Path | None = None
        self.upload_dir: Path | None = None
        self.summary_path: Path | None = None
        self.pair_path: Path | None = None

        self.docker: str | None = None
        self.docker_env: dict[str, str] = {}
        self.daemon_version: str | None = None

        self.git_head: str | None = None
        self.source_sha: str | None = None
        self.source_manifest_digest: str | None = None
        self.source_entries: list[dict[str, str]] = []
        self.record: dict[str, Any] | None = None
        self.accepted_report_count = 0

        self.inspected: dict[str, dict[str, Any]] = {}
        self.saved_roots: dict[str, Any] = {}
        self.archive_sha: str | None = None
        self.archive_size: int | None = None
        self.absence: dict[str, str] = {}
        self.loaded: dict[str, dict[str, Any]] = {}

        self.counts = {"inspect": 0, "save": 0, "remove": 0, "load": 0}
        self.checks = {name: "not-run" for name in CHECK_NAMES}
        self.failure: dict[str, str] | None = None
        self.result = "fail"
        self.interrupted: str | None = None
        self.repeat_signal = False
        self._cleanup_started = False
        self._cancel_kill_scheduled = False
        self._cancel_watchdog_thread: threading.Thread | None = None
        self._cancel_pgid: int | None = None
        self._cancel_process: subprocess.Popen[bytes] | None = None
        self._cancel_runner_cleanup_failed = False
        self._cancel_group_absent: bool | None = None
        self._cancel_reaped: bool | None = None
        self._archive_cleanup = "not-run"
        self._build_root_ready = False
        self._summary_written = False

    # ------------------------------------------------------------------
    # CLI / filesystem safety
    # ------------------------------------------------------------------

    def _prepare(self) -> None:
        build_root = Path(self.args.build_root).expanduser()
        if not build_root.is_absolute():
            raise HandoffError("cli", "build-root")
        try:
            build_info = build_root.lstat()
        except OSError as exc:
            raise HandoffError("cli", "build-root") from exc
        if build_root.is_symlink() or not stat.S_ISDIR(build_info.st_mode):
            raise HandoffError("cli", "build-root")
        try:
            build_root = build_root.resolve()
        except (OSError, RuntimeError) as exc:
            raise HandoffError("cli", "build-root") from exc
        repo = self.repo.resolve()
        if build_root == repo or repo in build_root.parents:
            raise HandoffError("cli", "build-root")
        self.build_root = build_root

        upload = build_root / "upload"
        try:
            if upload.is_symlink():
                raise HandoffError("cli", "upload-root")
            if upload.exists():
                upload_info = upload.lstat()
                if (
                    not stat.S_ISDIR(upload_info.st_mode)
                    or upload_info.st_uid != os.getuid()
                ):
                    raise HandoffError("cli", "upload-root")
            else:
                upload.mkdir(mode=0o700, parents=False, exist_ok=False)
                upload.chmod(0o700)
        except HandoffError:
            raise
        except OSError as exc:
            raise HandoffError("cli", "upload-root") from exc
        self.upload_dir = upload
        self.summary_path = upload / SUMMARY_FILE
        self._build_root_ready = True

        if not self.args.isolated_daemon:
            raise HandoffError("daemon", "isolated-required")

        artifact_root = Path(self.args.artifact_root).expanduser()
        if not artifact_root.is_absolute():
            raise HandoffError("cli", "artifact-root")
        if artifact_root.is_symlink():
            raise HandoffError("cli", "artifact-root")
        try:
            artifact_root = artifact_root.resolve()
        except (OSError, RuntimeError) as exc:
            raise HandoffError("cli", "artifact-root") from exc
        if artifact_root == repo or repo in artifact_root.parents:
            raise HandoffError("cli", "artifact-root")
        if (
            artifact_root == build_root
            or artifact_root in build_root.parents
            or build_root in artifact_root.parents
        ):
            raise HandoffError("cli", "path-overlap")
        try:
            release._fresh_root(artifact_root)
        except release.ReleaseError as exc:
            raise HandoffError("cli", "artifact-root") from exc
        self.artifact_root = artifact_root
        self.pair_path = artifact_root / release.PAIR_FILE

    def _tool_env(self) -> dict[str, str]:
        allowed = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
        return {name: self.env[name] for name in allowed if name in self.env}

    def _docker_env(self) -> dict[str, str]:
        allowed = (
            "PATH",
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONFIG",
            "XDG_RUNTIME_DIR",
            "TMPDIR",
            "LANG",
            "LC_ALL",
        )
        return {name: self.env[name] for name in allowed if name in self.env}

    # ------------------------------------------------------------------
    # Fixed runner wrappers
    # ------------------------------------------------------------------

    def _git(self, *arguments: str) -> check_images.ProcResult:
        self._raise_if_interrupted()
        git = self.git_path or shutil.which("git", path=self.env.get("PATH"))
        if not git:
            raise HandoffError("source", "git-missing")
        result = self.runner(
            [git, *arguments],
            cwd=self.repo,
            env=self._tool_env(),
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if self.interrupted is not None:
            if result.cleanup_failed:
                self._cancel_runner_cleanup_failed = True
            raise HandoffError("interrupted", self.interrupted)
        if result.timed_out:
            raise HandoffError("source", "timeout")
        if result.cleanup_failed:
            raise HandoffError("source", "cleanup")
        if result.exit_code != 0:
            raise HandoffError("source", "git")
        return result

    def _run_docker(
        self,
        stage: str,
        kind: str,
        arguments: Sequence[str],
        budget: float,
        *,
        allow_failure: bool = False,
    ) -> check_images.ProcResult:
        self._raise_if_interrupted()
        if self.docker is None:
            raise HandoffError("daemon", "docker-missing")
        result = self.runner(
            [self.docker, *arguments],
            cwd=self.repo,
            env=dict(self.docker_env),
            timeout=budget,
        )
        if self.interrupted is not None:
            if result.cleanup_failed:
                self._cancel_runner_cleanup_failed = True
            raise HandoffError("interrupted", self.interrupted)
        if result.timed_out:
            raise HandoffError(stage, "timeout")
        if result.cleanup_failed:
            raise HandoffError(stage, "cleanup")
        if not allow_failure and result.exit_code != 0:
            raise HandoffError(stage, kind)
        return result

    def _raise_if_interrupted(self) -> None:
        if self.interrupted is not None:
            raise HandoffError("interrupted", self.interrupted)

    def _handle_signal(self, signum: int, _frame: object) -> None:
        if self._cleanup_started:
            self.repeat_signal = True
            return
        self.repeat_signal = self.interrupted is not None
        if self.interrupted is None:
            self.interrupted = (
                "sigterm" if signum == signal.SIGTERM else "sigint"
            )
        check_images._CANCEL_REQUESTED = True
        try:
            check_images._signal_active(signum)
        except Exception:
            pass
        self._start_cancel_watchdog()

    def _start_cancel_watchdog(self) -> None:
        if self._cancel_kill_scheduled:
            return
        self._cancel_kill_scheduled = True
        process = check_images._ACTIVE_PROCESS
        pgid = check_images._ACTIVE_PGID
        self._cancel_process = process
        self._cancel_pgid = pgid
        thread = threading.Thread(
            target=self._cancel_watchdog,
            args=(process, pgid),
            daemon=True,
            name="handoff-cancel-watchdog",
        )
        self._cancel_watchdog_thread = thread
        thread.start()

    def _cancel_group_gone(
        self,
        pgid: int | None,
        process: subprocess.Popen[bytes] | None,
    ) -> bool:
        if pgid is not None:
            return check_images._group_absent(pgid)
        return process is None or process.poll() is not None

    def _cancel_watchdog(
        self,
        process: subprocess.Popen[bytes] | None,
        pgid: int | None,
    ) -> None:
        try:
            deadline = time.monotonic() + CANCEL_ACTIVE_WAIT_SECONDS
            while (
                process is None
                and pgid is None
                and time.monotonic() < deadline
            ):
                process = check_images._ACTIVE_PROCESS
                pgid = check_images._ACTIVE_PGID
                if process is None and pgid is None:
                    time.sleep(0.01)
            self._cancel_process = process
            self._cancel_pgid = pgid
            if process is None and pgid is None:
                return
            grace_deadline = time.monotonic() + CANCEL_TERM_GRACE_SECONDS
            while time.monotonic() < grace_deadline:
                if self._cancel_group_gone(pgid, process):
                    return
                time.sleep(0.02)
            if self._cancel_group_gone(pgid, process):
                return
            if pgid is not None:
                check_images._signal_group_id(pgid, signal.SIGKILL)
            elif process is not None and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
        except Exception:
            pass

    def _finalize_cancel_state(self) -> None:
        if self.interrupted is None:
            return
        thread = self._cancel_watchdog_thread
        if thread is not None:
            thread.join(
                timeout=CANCEL_ACTIVE_WAIT_SECONDS
                + CANCEL_TERM_GRACE_SECONDS
                + 1.0
            )
        pgid = self._cancel_pgid
        process = self._cancel_process
        if pgid is None:
            pgid = check_images._ACTIVE_PGID
        if process is None:
            process = check_images._ACTIVE_PROCESS
        self._cancel_pgid = pgid
        self._cancel_process = process
        gone = self._cancel_group_gone(pgid, process)
        if not gone:
            if pgid is not None:
                check_images._signal_group_id(pgid, signal.SIGKILL)
            elif process is not None and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            deadline = time.monotonic() + CANCEL_DRAIN_SECONDS
            while (
                time.monotonic() < deadline
                and not self._cancel_group_gone(pgid, process)
            ):
                time.sleep(0.02)
            gone = self._cancel_group_gone(pgid, process)
        reaped = process is None or process.poll() is not None
        if process is not None and not reaped:
            try:
                process.wait(timeout=CANCEL_DRAIN_SECONDS)
            except (subprocess.TimeoutExpired, OSError):
                pass
            reaped = process.poll() is not None
        self._cancel_group_absent = gone
        self._cancel_reaped = bool(
            gone
            and reaped
            and not self._cancel_runner_cleanup_failed
        )

    def _cleanup_partial_archive(self) -> None:
        if self._cancel_reaped is not True:
            self._archive_cleanup = "skipped-unreaped"
            return
        if self.artifact_root is None or self.pair_path is None:
            self._archive_cleanup = "not-created"
            return
        try:
            root_info = self.artifact_root.lstat()
        except OSError:
            self._archive_cleanup = "not-present"
            return
        if (
            self.artifact_root.is_symlink()
            or not stat.S_ISDIR(root_info.st_mode)
            or root_info.st_uid != os.getuid()
        ):
            self._archive_cleanup = "skipped-unsafe-root"
            return
        if self.pair_path.parent != self.artifact_root:
            self._archive_cleanup = "skipped-unsafe-path"
            return
        try:
            info = self.pair_path.lstat()
        except FileNotFoundError:
            self._archive_cleanup = "not-present"
            return
        except OSError:
            self._archive_cleanup = "failed"
            return
        if (
            self.pair_path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
        ):
            self._archive_cleanup = "skipped-unsafe-file"
            return
        try:
            self.pair_path.unlink()
        except OSError:
            self._archive_cleanup = "failed"
            return
        self._archive_cleanup = "removed"

    def _read_json(
        self, path: Path, stage: str, kind: str, maximum: int
    ) -> dict[str, Any]:
        try:
            return release._read_json(path, stage, kind, maximum)
        except release.ReleaseError as exc:
            raise HandoffError(exc.stage, exc.kind) from exc

    # ------------------------------------------------------------------
    # Builder output and source identity
    # ------------------------------------------------------------------

    def _verify_source_and_builder(self) -> None:
        self._raise_if_interrupted()
        head_result = self._git("rev-parse", "HEAD")
        head = head_result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            raise HandoffError("source", "head")
        self.git_head = head
        status_result = self._git(
            "status", "--porcelain", "--untracked-files=normal"
        )
        if status_result.stdout.strip():
            raise HandoffError("source", "dirty")
        self.checks["git_clean"] = "pass"

        record_path = self.build_root / release.BUILD_RECORD_FILE
        record = self._read_json(
            record_path,
            "builder",
            "record",
            release.BUILD_RECORD_MAX_BYTES,
        )
        try:
            source_sha = release._source_sha(
                record.get("source_sha"), "builder", "record-source-sha"
            )
        except release.ReleaseError as exc:
            raise HandoffError(exc.stage, exc.kind) from exc
        if head != source_sha:
            raise HandoffError("source", "stale")
        if record.get("git_head") != source_sha:
            raise HandoffError("source", "record-git-head")
        if record.get("source_dirty") is not False:
            raise HandoffError("source", "record-dirty")
        if record.get("build_once_per_image") is not True:
            raise HandoffError("builder", "record-once")
        self.record = record
        self.source_sha = source_sha

        try:
            entries, digest = check_images._compute_source_manifest(
                self.repo, self.runner, self._tool_env()
            )
        except check_images.LauncherError as exc:
            raise HandoffError("source", "manifest") from exc
        if digest != record.get("source_manifest_digest"):
            raise HandoffError("source", "manifest")
        self.source_entries = entries
        self.source_manifest_digest = digest

        manifest_path = self.build_root / SOURCE_MANIFEST_FILE
        manifest = self._read_json(
            manifest_path,
            "source",
            "manifest-file",
            SOURCE_MANIFEST_MAX_BYTES,
        )
        self._validate_source_manifest(
            manifest, entries, digest, head, source_sha
        )
        self.checks["source_manifest"] = "pass"

        validation_dir = self.build_root / VALIDATION_DIR
        try:
            validation_info = validation_dir.lstat()
        except OSError as exc:
            raise HandoffError("builder", "accepted-report") from exc
        if validation_dir.is_symlink() or not stat.S_ISDIR(
            validation_info.st_mode
        ):
            raise HandoffError("builder", "accepted-report")
        reports = sorted(validation_dir.glob("*/images.json"))
        if len(reports) != 1:
            raise HandoffError("builder", "accepted-report")
        accepted_path = reports[0]
        try:
            resolved_report = accepted_path.resolve()
        except (OSError, RuntimeError) as exc:
            raise HandoffError("builder", "accepted-report") from exc
        if (
            accepted_path.is_symlink()
            or accepted_path.parent.is_symlink()
            or self.build_root not in resolved_report.parents
        ):
            raise HandoffError("builder", "accepted-report")
        accepted = self._read_json(
            accepted_path,
            "builder",
            "accepted-report",
            release.ACCEPTED_REPORT_MAX_BYTES,
        )

        # TrustedContext is only the existing pure validator's value object.
        # No workflow event/ref/repository trust is inferred or claimed here.
        # The two validators are unbound Release methods that use only their
        # explicit arguments; no Release.prepare/publish/trusted_context call
        # is made.
        trusted = release.TrustedContext(
            source_sha=source_sha, repo=self.repo
        )
        try:
            release.Release._validate_build_record(None, record, trusted)
            release.Release._validate_accepted_report(None, accepted, record)
        except release.ReleaseError as exc:
            raise HandoffError(exc.stage, exc.kind) from exc
        if accepted.get("platform") != release.PLATFORM:
            raise HandoffError("builder", "accepted-platform")
        accepted_source = accepted.get("source")
        if (
            not isinstance(accepted_source, dict)
            or accepted_source.get("manifest") != entries
            or accepted_source.get("manifest_file_count") != len(entries)
        ):
            raise HandoffError("builder", "accepted-source")
        self.checks["build_record"] = "pass"
        self.checks["accepted_report"] = "pass"
        self.accepted_report_count = len(reports)

    def _validate_source_manifest(
        self,
        value: Mapping[str, Any],
        entries: Sequence[Mapping[str, str]],
        digest: str,
        head: str,
        source_sha: str,
    ) -> None:
        if value.get("schema") != SOURCE_MANIFEST_SCHEMA:
            raise HandoffError("source", "manifest-file")
        if value.get("manifest_digest") != digest:
            raise HandoffError("source", "manifest-file")
        if value.get("files") != list(entries):
            raise HandoffError("source", "manifest-file")
        if value.get("git_head") != head:
            raise HandoffError("source", "manifest-file")
        if value.get("source_dirty") is not False:
            raise HandoffError("source", "manifest-file")
        if value.get("source_sha") != source_sha:
            raise HandoffError("source", "manifest-file")
        for name, key in (("go.mod", "go_mod_sha256"), ("go.sum", "go_sum_sha256")):
            path = self.repo / name
            try:
                info = path.lstat()
            except OSError as exc:
                raise HandoffError("source", key) from exc
            if path.is_symlink() or not stat.S_ISREG(info.st_mode):
                raise HandoffError("source", key)
            if value.get(key) != release.sha256_file(path):
                raise HandoffError("source", key)

    # ------------------------------------------------------------------
    # Isolated daemon
    # ------------------------------------------------------------------

    def _validate_isolated_host(self) -> None:
        runner_temp_value = self.env.get("RUNNER_TEMP", "")
        if not runner_temp_value:
            raise HandoffError("daemon", "runner-temp")
        runner_temp = Path(runner_temp_value).expanduser()
        if not runner_temp.is_absolute():
            raise HandoffError("daemon", "runner-temp")
        try:
            runner_temp = runner_temp.resolve()
        except (OSError, RuntimeError) as exc:
            raise HandoffError("daemon", "runner-temp") from exc
        if runner_temp == Path("/") or str(runner_temp) in ("/run", "/var/run"):
            raise HandoffError("daemon", "runner-temp")

        host = self.docker_env.get("DOCKER_HOST", "")
        if not host or not host.startswith("unix://"):
            raise HandoffError("daemon", "host")
        path_text = host[len("unix://") :]
        if not path_text or not path_text.startswith("/"):
            raise HandoffError("daemon", "host")
        if (
            any(char in path_text for char in ("\x00", "\r", "\n"))
            or "?" in path_text
            or "#" in path_text
        ):
            raise HandoffError("daemon", "host")
        socket = Path(path_text)
        if ".." in socket.parts:
            raise HandoffError("daemon", "host")
        if socket in (Path("/var/run/docker.sock"), Path("/run/docker.sock")):
            raise HandoffError("daemon", "system-socket")
        try:
            socket = socket.resolve()
        except (OSError, RuntimeError) as exc:
            raise HandoffError("daemon", "host") from exc
        if runner_temp not in socket.parents:
            raise HandoffError("daemon", "outside-runner-temp")

    def _docker_preflight(self) -> None:
        self._raise_if_interrupted()
        if not self.args.isolated_daemon:
            raise HandoffError("daemon", "isolated-required")
        # Docker CLI precedence makes DOCKER_CONTEXT override DOCKER_HOST.
        # The isolated boundary is explicitly HOST-only, so a non-empty
        # context is refused before any Docker command is executed.
        if self.env.get("DOCKER_CONTEXT", ""):
            raise HandoffError("daemon", "docker-context")
        docker = self.docker_path or shutil.which(
            "docker", path=self.env.get("PATH")
        )
        if not docker:
            raise HandoffError("daemon", "docker-missing")
        self.docker = docker
        self.docker_env = self._docker_env()
        self._validate_isolated_host()
        self.checks["daemon_isolation"] = "pass"
        version = self._run_docker(
            "daemon",
            "version",
            ["version", "--format", "{{.Server.Version}}"],
            DOCKER_VERSION_TIMEOUT_SECONDS,
        )
        if version.stdout.strip() != EXPECTED_DOCKER_SERVER_VERSION:
            raise HandoffError("daemon", "version")
        self.daemon_version = EXPECTED_DOCKER_SERVER_VERSION
        driver = self._run_docker(
            "daemon",
            "containerd",
            ["info", "--format", DOCKER_DRIVER_FORMAT],
            DOCKER_VERSION_TIMEOUT_SECONDS,
        )
        lines = {
            line.strip() for line in driver.stdout.splitlines() if line.strip()
        }
        if CONTAINERD_DRIVER_STATUS not in lines:
            raise HandoffError("daemon", "containerd")
        self.checks["daemon"] = "pass"

    # ------------------------------------------------------------------
    # Original pair inspection
    # ------------------------------------------------------------------

    def _inspect_raw(
        self, stage: str, kind: str, reference: str
    ) -> dict[str, Any]:
        self.counts["inspect"] += 1
        result = self._run_docker(
            stage,
            kind,
            ["image", "inspect", "--format", "{{json .}}", reference],
            IMAGE_INSPECT_TIMEOUT_SECONDS,
        )
        try:
            value = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HandoffError(stage, kind + "-json") from exc
        if not isinstance(value, dict):
            raise HandoffError(stage, kind + "-json")
        return value

    def _validate_inspect(
        self,
        value: Mapping[str, Any],
        expected_id: str,
        expected_root: Mapping[str, Any] | None,
        *,
        expected_ref: str,
        require_tags: bool,
        stage: str,
    ) -> dict[str, Any]:
        try:
            image_id = release._image_id(
                value.get("Id"), stage, "id"
            )
            if image_id != expected_id:
                raise HandoffError(stage, "id")
            if value.get("Os") != "linux" or value.get(
                "Architecture"
            ) != "amd64":
                raise HandoffError(stage, "platform")
            descriptor = value.get("Descriptor")
            if not isinstance(descriptor, dict):
                raise HandoffError(stage, "descriptor-missing")
            if descriptor.get("mediaType") != release.OCI_INDEX:
                raise HandoffError(stage, "descriptor-media-type")
            descriptor_digest = release._image_id(
                descriptor.get("digest"), stage, "descriptor-digest"
            )
            descriptor_size = release._positive_size(
                descriptor.get("size"),
                stage,
                "descriptor-size",
                release.ROOT_JSON_MAX_BYTES,
            )
            if descriptor_digest != image_id:
                raise HandoffError(stage, "descriptor-id")
            root = {
                "mediaType": release.OCI_INDEX,
                "digest": descriptor_digest,
                "size": descriptor_size,
            }
            if expected_root is not None and root != dict(expected_root):
                raise HandoffError(stage, "root")
            config = value.get("Config")
            labels = config.get("Labels") if isinstance(config, dict) else None
            if not isinstance(labels, dict):
                raise HandoffError(stage, "source-label")
            label = release._sha256_hex(
                labels.get(release.SOURCE_LABEL), stage, "source-label"
            )
            if label != self.source_manifest_digest:
                raise HandoffError(stage, "source-label")
            if require_tags:
                tags = value.get("RepoTags")
                if not isinstance(tags, list) or len(tags) != 1:
                    raise HandoffError(stage, "tags")
                expected_tag = self._normalized_reference(
                    expected_ref, stage=stage
                )
                if (
                    not isinstance(tags[0], str)
                    or self._normalized_reference(
                        tags[0], stage=stage
                    )
                    != expected_tag
                ):
                    raise HandoffError(stage, "tags")
            return {
                "id": image_id,
                "root_descriptor": root,
                "platform": release.PLATFORM,
                "source_label": label,
            }
        except release.ReleaseError as exc:
            raise HandoffError(stage, exc.kind) from exc

    def _repository_without_tag(
        self, reference: str, *, allow_tag: bool, stage: str
    ) -> str:
        repository = reference.split("@", 1)[0]
        slash = repository.rfind("/")
        colon = repository.rfind(":")
        if colon > slash:
            if not allow_tag:
                raise HandoffError(stage, "repo-digests")
            repository = repository[:colon]
        if (
            not repository
            or repository.startswith("/")
            or repository.endswith("/")
            or "//" in repository
            or "\\" in repository
            or any(
                ord(char) <= 32 or ord(char) == 0x7F
                for char in repository
            )
            or any(
                part in ("", ".", "..")
                for part in repository.split("/")
            )
        ):
            raise HandoffError(stage, "repo-digests")
        return repository

    def _normalized_repository(self, repository: str) -> str:
        first = repository.split("/", 1)[0]
        if "/" not in repository:
            return "docker.io/library/" + repository
        if first in (
            "docker.io",
            "index.docker.io",
            "registry-1.docker.io",
        ):
            return "docker.io/" + repository.split("/", 1)[1]
        if "." in first or ":" in first or first == "localhost":
            return repository
        return "docker.io/" + repository

    def _normalized_reference(self, reference: str, *, stage: str) -> str:
        if not isinstance(reference, str) or not reference:
            raise HandoffError(stage, "tags")
        if "@" in reference:
            raise HandoffError(stage, "tags")
        slash = reference.rfind("/")
        colon = reference.rfind(":")
        if colon > slash:
            repository = reference[:colon]
            tag = reference[colon + 1 :]
        else:
            repository = reference
            tag = "latest"
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
            raise HandoffError(stage, "tags")
        repository = self._repository_without_tag(
            repository, allow_tag=False, stage=stage
        )
        return self._normalized_repository(repository) + ":" + tag

    def _validate_repo_digests(
        self,
        value: Mapping[str, Any],
        expected_ref: str,
        expected_id: str,
        *,
        stage: str,
    ) -> None:
        # The existing builder test model gives tag-built images
        # RepoDigests=[].  Containerd variants may also report the local
        # repository with the image's own root digest.  Accept only those two
        # shapes; a foreign repository or a digest for any other image is a
        # shared/foreign reference and must fail before removal.
        values = value.get("RepoDigests")
        if values is None:
            return
        if not isinstance(values, list):
            raise HandoffError(stage, "repo-digests")
        expected_repo = self._normalized_repository(
            self._repository_without_tag(
                expected_ref, allow_tag=True, stage=stage
            )
        )
        for item in values:
            if (
                not isinstance(item, str)
                or not re.fullmatch(
                    r"[^@\s]+@sha256:[0-9a-f]{64}", item
                )
            ):
                raise HandoffError(stage, "repo-digests")
            repository_text, digest_text = item.rsplit("@", 1)
            if digest_text != expected_id:
                raise HandoffError(stage, "repo-digests")
            repository = self._repository_without_tag(
                repository_text, allow_tag=False, stage=stage
            )
            if (
                self._normalized_repository(repository)
                != expected_repo
            ):
                raise HandoffError(stage, "repo-digests")

    def _inspect_original_pair(self) -> None:
        self._raise_if_interrupted()
        if self.record is None:
            raise HandoffError("builder", "record")
        app_raw = self._inspect_raw(
            "image", "inspect", self.record["app_image"]
        )
        migrate_raw = self._inspect_raw(
            "image", "inspect", self.record["migrate_image"]
        )
        app = self._validate_inspect(
            app_raw,
            self.record["app_image_id"],
            None,
            expected_ref=self.record["app_image"],
            require_tags=True,
            stage="image",
        )
        migrate = self._validate_inspect(
            migrate_raw,
            self.record["migrate_image_id"],
            None,
            expected_ref=self.record["migrate_image"],
            require_tags=True,
            stage="image",
        )
        self._validate_repo_digests(
            app_raw,
            self.record["app_image"],
            app["id"],
            stage="image",
        )
        self._validate_repo_digests(
            migrate_raw,
            self.record["migrate_image"],
            migrate["id"],
            stage="image",
        )
        if app["id"] == migrate["id"]:
            raise HandoffError("image", "pair")
        self.inspected = {"app": app, "migrate": migrate}
        self.checks["original_images"] = "pass"

    # ------------------------------------------------------------------
    # Save and archive identity
    # ------------------------------------------------------------------

    def _save_pair(self) -> None:
        self._raise_if_interrupted()
        if self.record is None or self.pair_path is None:
            raise HandoffError("save", "artifact")
        if self.pair_path.exists() or self.pair_path.is_symlink():
            raise HandoffError("save", "artifact")
        app_id = self.record["app_image_id"]
        migrate_id = self.record["migrate_image_id"]
        self.counts["save"] += 1
        self._run_docker(
            "save",
            "save",
            [
                "image",
                "save",
                "--output",
                str(self.pair_path),
                app_id,
                migrate_id,
            ],
            IMAGE_SAVE_TIMEOUT_SECONDS,
        )
        try:
            info = self.pair_path.lstat()
        except OSError as exc:
            raise HandoffError("save", "artifact") from exc
        if (
            self.pair_path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_size <= 0
        ):
            raise HandoffError("save", "artifact")
        if info.st_size > release.TAR_MAX_BYTES:
            raise HandoffError("archive", "size")
        try:
            self.pair_path.chmod(0o600)
        except OSError as exc:
            raise HandoffError("save", "chmod") from exc
        self.checks["save"] = "pass"
        try:
            self.archive_sha, self.archive_size = release._stream_sha256(
                self.pair_path, "archive", "hash"
            )
            roots = release._parse_pair_tar(
                self.pair_path, [app_id, migrate_id]
            )
        except release.ReleaseError as exc:
            raise HandoffError("archive", exc.kind) from exc
        if self.archive_size != info.st_size:
            raise HandoffError("archive", "size")
        for role in ROLES:
            image_id = self.record[role + "_image_id"]
            expected = self.inspected[role]["root_descriptor"]
            parsed = roots.get(image_id)
            if parsed is None:
                raise HandoffError("archive", "root")
            if (
                parsed.media_type != expected["mediaType"]
                or parsed.digest != expected["digest"]
                or parsed.size != expected["size"]
            ):
                raise HandoffError("archive", "root")
        self.saved_roots = roots
        self.checks["archive"] = "pass"

    # ------------------------------------------------------------------
    # Ownership, absence, load
    # ------------------------------------------------------------------

    def _check_ownership(self) -> None:
        self._raise_if_interrupted()
        if self.record is None:
            raise HandoffError("builder", "record")
        app_raw = self._inspect_raw(
            "ownership", "inspect", self.record["app_image"]
        )
        migrate_raw = self._inspect_raw(
            "ownership", "inspect", self.record["migrate_image"]
        )
        app = self._validate_inspect(
            app_raw,
            self.record["app_image_id"],
            self.inspected["app"]["root_descriptor"],
            expected_ref=self.record["app_image"],
            require_tags=True,
            stage="ownership",
        )
        migrate = self._validate_inspect(
            migrate_raw,
            self.record["migrate_image_id"],
            self.inspected["migrate"]["root_descriptor"],
            expected_ref=self.record["migrate_image"],
            require_tags=True,
            stage="ownership",
        )
        self._validate_repo_digests(
            app_raw,
            self.record["app_image"],
            app["id"],
            stage="ownership",
        )
        self._validate_repo_digests(
            migrate_raw,
            self.record["migrate_image"],
            migrate["id"],
            stage="ownership",
        )
        if (
            app["id"] != self.inspected["app"]["id"]
            or migrate["id"] != self.inspected["migrate"]["id"]
            or app["root_descriptor"]
            != self.inspected["app"]["root_descriptor"]
            or migrate["root_descriptor"]
            != self.inspected["migrate"]["root_descriptor"]
        ):
            raise HandoffError("ownership", "changed")
        for role in ROLES:
            image_id = self.record[role + "_image_id"]
            result = self._run_docker(
                "ownership",
                "containers",
                [
                    "ps",
                    "-a",
                    "--filter",
                    "ancestor=" + image_id,
                    "--format",
                    "{{.ID}}",
                ],
                PS_TIMEOUT_SECONDS,
                allow_failure=True,
            )
            if result.exit_code != 0:
                raise HandoffError("ownership", "containers")
            if result.stdout.strip():
                raise HandoffError("ownership", "in-use")
        self.checks["ownership"] = "pass"

    def _remove_pair(self) -> None:
        self._raise_if_interrupted()
        if self.record is None:
            raise HandoffError("builder", "record")
        self.counts["remove"] += 1
        self._run_docker(
            "remove",
            "remove",
            [
                "image",
                "rm",
                self.record["app_image_id"],
                self.record["migrate_image_id"],
            ],
            IMAGE_REMOVE_TIMEOUT_SECONDS,
        )
        self.checks["remove"] = "pass"

    def _inspect_absent(self, image_id: str) -> None:
        self.counts["inspect"] += 1
        result = self._run_docker(
            "absence",
            "inspect",
            ["image", "inspect", "--format", "{{json .}}", image_id],
            IMAGE_INSPECT_TIMEOUT_SECONDS,
            allow_failure=True,
        )
        if result.exit_code == 0:
            raise HandoffError("absence", "present")
        stderr = result.stderr.lower()
        if "no such image" not in stderr and "not found" not in stderr:
            raise HandoffError("absence", "inspect-error")

    def _confirm_absence(self) -> None:
        self._raise_if_interrupted()
        if self.record is None:
            raise HandoffError("builder", "record")
        for role in ROLES:
            self._inspect_absent(self.record[role + "_image_id"])
            self.absence[role] = "confirmed"
        result = self._run_docker(
            "absence",
            "ls",
            ["image", "ls", "--no-trunc", "--format", "{{.ID}}"],
            IMAGE_LIST_TIMEOUT_SECONDS,
            allow_failure=True,
        )
        if result.exit_code != 0:
            raise HandoffError("absence", "ls")
        present: set[str] = set()
        for line in result.stdout.splitlines():
            text = line.strip()
            if not text:
                continue
            if re.fullmatch(r"sha256:[0-9a-f]{64}", text):
                present.add(text)
            elif re.fullmatch(r"[0-9a-f]{64}", text):
                present.add("sha256:" + text)
            else:
                raise HandoffError("absence", "ls-format")
        if (
            self.record["app_image_id"] in present
            or self.record["migrate_image_id"] in present
        ):
            raise HandoffError("absence", "present")
        self.checks["absence"] = "pass"

    def _load_pair(self) -> None:
        self._raise_if_interrupted()
        if self.pair_path is None:
            raise HandoffError("load", "artifact")
        try:
            info = self.pair_path.lstat()
        except OSError as exc:
            raise HandoffError("load", "artifact") from exc
        if (
            self.pair_path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_size != self.archive_size
        ):
            raise HandoffError("load", "artifact")
        self.counts["load"] += 1
        self._run_docker(
            "load",
            "load",
            ["load", "--input", str(self.pair_path)],
            IMAGE_LOAD_TIMEOUT_SECONDS,
        )
        self.checks["load"] = "pass"

    def _verify_loaded_pair(self) -> None:
        self._raise_if_interrupted()
        if self.record is None:
            raise HandoffError("builder", "record")
        for role in ROLES:
            image_id = self.record[role + "_image_id"]
            raw = self._inspect_raw("load", "identity", image_id)
            loaded = self._validate_inspect(
                raw,
                image_id,
                self.inspected[role]["root_descriptor"],
                expected_ref=self.record[role + "_image"],
                require_tags=False,
                stage="load",
            )
            self.loaded[role] = loaded
        if self.loaded["app"]["id"] == self.loaded["migrate"]["id"]:
            raise HandoffError("load", "pair")
        self.checks["loaded_images"] = "pass"

    def _verify_source_unchanged(self) -> None:
        self._raise_if_interrupted()
        head_result = self._git("rev-parse", "HEAD")
        if head_result.stdout.strip() != self.git_head:
            raise HandoffError("source", "unchanged")
        status_result = self._git(
            "status", "--porcelain", "--untracked-files=normal"
        )
        if status_result.stdout.strip():
            raise HandoffError("source", "unchanged")
        try:
            entries, digest = check_images._compute_source_manifest(
                self.repo, self.runner, self._tool_env()
            )
        except check_images.LauncherError as exc:
            raise HandoffError("source", "unchanged") from exc
        if (
            entries != self.source_entries
            or digest != self.source_manifest_digest
        ):
            raise HandoffError("source", "unchanged")
        self.checks["source_unchanged"] = "pass"

    # ------------------------------------------------------------------
    # Summary and orchestration
    # ------------------------------------------------------------------

    def _summary_value(self) -> dict[str, Any]:
        passed = self.failure is None and self.result == "pass"
        images: dict[str, Any] = {}
        if self.record is not None:
            for role in ROLES:
                identity = self.inspected.get(role)
                if identity is not None:
                    images[role] = {
                        "ref": self.record[role + "_image"],
                        "id": identity["id"],
                        "root_descriptor": dict(
                            identity["root_descriptor"]
                        ),
                    }
        loaded: dict[str, Any] = {}
        for role in ROLES:
            identity = self.loaded.get(role)
            if identity is not None:
                loaded[role] = {
                    "id": identity["id"],
                    "root_descriptor": dict(
                        identity["root_descriptor"]
                    ),
                    "platform": identity["platform"],
                    "source_label_match": True,
                }
        return {
            "schema": SUMMARY_SCHEMA,
            "generated_at": check_images.utc_now(),
            "result": "pass" if passed else "fail",
            "not_release": True,
            "release_eligible": False,
            "scope": "same-job-same-daemon-save-load-preflight",
            "upload": {
                "summary": True,
                "pair_tar": False,
                "images": False,
                "release_descriptor": False,
            },
            "not_proven": [
                "cross-job-handoff",
                "remote-registry-digest",
                "ghcr-package-access",
                "release-descriptor",
                "publish-eligibility",
                "deployment",
            ],
            "failure": self.failure,
            "checks": dict(self.checks),
            "counts": dict(self.counts),
            "source": {
                "sha": self.source_sha,
                "manifest_digest": self.source_manifest_digest,
                "git_head": self.git_head,
                "source_dirty": False
                if self.record is not None
                and self.record.get("source_dirty") is False
                and self.checks.get("git_clean") == "pass"
                else None,
                "manifest_file_count": len(self.source_entries),
                "current_manifest_match": self.checks.get(
                    "source_manifest"
                )
                == "pass",
                "unchanged": self.checks.get("source_unchanged") == "pass",
            },
            "builder": {
                "accepted_report_count": self.accepted_report_count,
                "build_once_per_image": bool(
                    self.record is not None
                    and self.record.get("build_once_per_image") is True
                ),
                "accepted_result": "pass"
                if self.checks.get("accepted_report") == "pass"
                else None,
            },
            "daemon": {
                "isolated": self.checks.get("daemon_isolation") == "pass",
                "server_version": self.daemon_version,
                "containerd_snapshotter": self.checks.get("daemon")
                == "pass",
            },
            "images": images,
            "archive": {
                "file": release.PAIR_FILE,
                "sha256": self.archive_sha,
                "size": self.archive_size,
            },
            "absence": {
                "app": self.absence.get("app"),
                "migrate": self.absence.get("migrate"),
                "method": "inspect-not-found+image-ls-no-trunc",
            },
            "load": {
                "performed": self.checks.get("load") == "pass",
                "calls": self.counts["load"],
                "images": loaded,
            },
            "cancellation": {
                "signal": self.interrupted,
                "repeat_signal": self.repeat_signal,
                "client_process_group_absent": self._cancel_group_absent,
                "client_process_reaped": self._cancel_reaped,
                "runner_cleanup_failed": (
                    self._cancel_runner_cleanup_failed
                ),
                "archive_cleanup": self._archive_cleanup,
                "daemon_side_effects_rolled_back": False,
                "daemon_side_effects_may_remain": (
                    self.interrupted is not None
                ),
                "boundary": (
                    "client process group only; "
                    "no daemon-side rollback claimed"
                ),
            },
        }

    def _write_summary(self) -> bool:
        if (
            not self._build_root_ready
            or self.upload_dir is None
            or self.summary_path is None
        ):
            return False
        try:
            info = self.upload_dir.lstat()
        except OSError:
            return False
        if self.upload_dir.is_symlink() or not stat.S_ISDIR(info.st_mode):
            return False
        data = (
            json.dumps(self._summary_value(), indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        descriptor: int | None = None
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=".handoff-preflight-",
                suffix=".json",
                dir=str(self.upload_dir),
            )
            temporary = Path(name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(data)
            os.replace(temporary, self.summary_path)
            temporary = None
            self._summary_written = True
            return True
        except OSError:
            return False
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def _write_summary_best_effort(self) -> None:
        if not self._build_root_ready:
            return
        try:
            written = self._write_summary()
        except Exception:
            written = False
        if not written:
            if self.failure is None:
                self.failure = {"stage": "summary", "kind": "write"}
            self.result = "fail"

    def run(self) -> int:
        check_images._reset_process_cancel()
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        installed_int = False
        installed_term = False
        try:
            try:
                signal.signal(signal.SIGINT, self._handle_signal)
                installed_int = True
                signal.signal(signal.SIGTERM, self._handle_signal)
                installed_term = True
            except (AttributeError, OSError, ValueError):
                if installed_int:
                    try:
                        signal.signal(signal.SIGINT, old_int)
                    except (OSError, ValueError):
                        pass
                self.failure = {"stage": "interrupted", "kind": "handler"}
                self._write_summary_best_effort()
                return 1
            try:
                self._prepare()
                self._raise_if_interrupted()
                self._verify_source_and_builder()
                self._docker_preflight()
                self._inspect_original_pair()
                self._save_pair()
                self._check_ownership()
                self._remove_pair()
                self._confirm_absence()
                self._load_pair()
                self._verify_loaded_pair()
                self._verify_source_unchanged()
                self.result = "pass"
            except HandoffError as exc:
                self.failure = {"stage": exc.stage, "kind": exc.kind}
            except release.ReleaseError as exc:
                self.failure = {"stage": exc.stage, "kind": exc.kind}
            except check_images.LauncherError as exc:
                self.failure = {"stage": exc.stage, "kind": exc.kind}
            except Exception:
                self.failure = {"stage": "internal", "kind": "unexpected"}
            finally:
                self._cleanup_started = True
                try:
                    self._finalize_cancel_state()
                except Exception:
                    if self.interrupted is not None:
                        self._cancel_group_absent = False
                        self._cancel_reaped = False
                if self.interrupted is not None:
                    try:
                        self._cleanup_partial_archive()
                    except Exception:
                        self._archive_cleanup = "failed"
                    self.failure = {
                        "stage": "interrupted",
                        "kind": self.interrupted,
                    }
                    self.result = "fail"
                self._write_summary_best_effort()
        finally:
            if installed_int:
                try:
                    signal.signal(signal.SIGINT, old_int)
                except (OSError, ValueError):
                    pass
            if installed_term:
                try:
                    signal.signal(signal.SIGTERM, old_term)
                except (OSError, ValueError):
                    pass
        if self.failure is not None:
            self.result = "fail"
        return (
            0
            if self.failure is None
            and self.result == "pass"
            and self._summary_written
            else 1
        )

    def print_summary(self) -> None:
        path = (
            str(self.summary_path)
            if self._summary_written and self.summary_path is not None
            else "not-written"
        )
        print("handoff-preflight-summary: " + path)
        print("save-calls: " + str(self.counts["save"]))
        print("remove-calls: " + str(self.counts["remove"]))
        print("load-calls: " + str(self.counts["load"]))
        print("result: " + self.result)
        if self.failure is not None:
            print(
                "failure: "
                + self.failure["stage"]
                + "/"
                + self.failure["kind"]
            )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Same-job save/load handoff preflight for the already accepted "
            "builder image pair.  This is not a release descriptor and never "
            "publishes or uploads the private archive."
        )
    )
    parser.add_argument("--build-root", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument(
        "--isolated-daemon",
        action="store_true",
        help=(
            "Required to permit removal and reload on an explicit "
            "runner-local Docker 29.9.3 containerd daemon."
        ),
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., check_images.ProcResult] | None = None,
    docker_path: str | None = None,
    git_path: str | None = None,
    repo: Path = REPO_ROOT,
    env: Mapping[str, str] | None = None,
) -> int:
    try:
        args = parse_args(argv)
        preflight = HandoffPreflight(
            args,
            repo=repo,
            runner=runner,
            docker_path=docker_path,
            git_path=git_path,
            env=env,
        )
        code = preflight.run()
        preflight.print_summary()
        return code
    except HandoffError as exc:
        print(f"ERROR: {exc.stage}/{exc.kind}", file=sys.stderr)
        print(f"result: fail stage={exc.stage} kind={exc.kind}")
        return 1
    except Exception:
        print("ERROR: internal/unexpected", file=sys.stderr)
        print("result: fail stage=internal kind=unexpected")
        return 1


if __name__ == "__main__":
    sys.exit(main())
