#!/usr/bin/env python3
"""Select and verify a release candidate from one trusted main CI run.

``select`` reuses the read-only preflight in ``check-main-ci.py``.  It accepts
only the trusted ``workflow_dispatch`` main/repository/SHA context, requires
the latest push/main CI run for that SHA to be completed successfully, and
then lists every artifact page for that exact run.  The pointer and bundle
artifacts must use the exact run/attempt-bound names, be unique, unexpired,
and expose a real ``sha256:`` digest.  No artifact content is downloaded or
executed here.

``verify`` revalidates the same trusted environment context, reads the
selection summary and the pointer downloaded by the workflow, and compares
every provenance field.  The pointer cannot prove a source by its own hash;
the bundle digest must equal the digest returned by the GitHub API for the
selected run.  Its fixed outputs keep the canonical ``sha256:`` bundle digest
and the bare 64-hex descriptor SHA expected by the release workflow.

Only bounded GET requests to ``api.github.com`` are made.  Redirects, retries,
PAT fallbacks, arbitrary artifact URLs, and credential logging are absent.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


def _load_check_main_ci() -> Any:
    path = Path(__file__).resolve().with_name("check-main-ci.py")
    spec = importlib.util.spec_from_file_location(
        "payment_hub_check_main_ci_for_release",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load check-main-ci.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


CI = _load_check_main_ci()

REPOSITORY = CI.REPOSITORY
WORKFLOW_PATH = CI.CANDIDATE_WORKFLOW_PATH
PLATFORM = "linux/amd64"
POINTER_PREFIX = "ci-release-pointer-"
BUNDLE_PREFIX = "ci-release-bundle-"

SELECTION_SCHEMA = "payment-hub.release-selection.v1"
POINTER_SCHEMA = "payment-hub.ci-candidate.v1"

MAX_SELECTION_BYTES = 64 * 1024
MAX_POINTER_BYTES = 64 * 1024
MAX_POINTER_ARTIFACT_BYTES = 1024 * 1024
MAX_BUNDLE_ARTIFACT_BYTES = 8 * 1024 * 1024 * 1024

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
DECIMAL_RE = re.compile(r"^[1-9][0-9]*$")

STDOUT_PREFIX = "release-selection:"

SELECTION_KEYS = frozenset(
    {
        "schema",
        "result",
        "failure",
        "repository",
        "workflow_path",
        "source_sha",
        "platform",
        "ci_run_id",
        "ci_run_attempt",
        "pointer_artifact_id",
        "pointer_artifact_digest",
        "bundle_artifact_id",
        "bundle_artifact_digest",
    }
)
POINTER_KEYS = frozenset(
    {
        "schema",
        "repository",
        "workflow_path",
        "source_sha",
        "platform",
        "run_id",
        "run_attempt",
        "bundle_artifact_id",
        "bundle_artifact_digest",
        "descriptor_sha256",
    }
)
SELECT_OUTPUT_KEYS = frozenset(
    {"ci_run_id", "ci_run_attempt", "pointer_artifact_id"}
)
VERIFY_OUTPUT_KEYS = frozenset(
    {
        "ci_run_id",
        "ci_run_attempt",
        "bundle_artifact_id",
        "descriptor_sha256",
        "bundle_artifact_digest",
    }
)

FAILURE_ENUM = {
    "cli": set(CI.FAILURE_ENUM["cli"])
    | {
        "github-output",
        "github-output-path",
        "github-output-symlink",
        "selection",
        "selection-path",
        "selection-symlink",
        "selection-body-too-large",
        "pointer",
        "pointer-path",
        "pointer-symlink",
        "pointer-body-too-large",
    },
    "env": set(CI.FAILURE_ENUM["env"]),
    "workflow": set(CI.FAILURE_ENUM["workflow"]),
    "runs": set(CI.FAILURE_ENUM["runs"]),
    "run": set(CI.FAILURE_ENUM["run"]),
    "artifacts": set(CI.FAILURE_ENUM["artifacts"])
    | {"missing", "duplicate", "expired", "digest", "size", "name"},
    "selection": {
        "schema",
        "bad-json",
        "result",
        "failure",
        "provenance",
        "identity",
        "digest",
        "body-too-large",
        "path",
        "symlink",
    },
    "pointer": {
        "schema",
        "bad-json",
        "repository",
        "workflow-path",
        "source-sha",
        "platform",
        "run-id",
        "run-attempt",
        "bundle-artifact-id",
        "bundle-artifact-digest",
        "descriptor-sha256",
        "digest",
        "selection-mismatch",
        "body-too-large",
        "path",
        "symlink",
    },
    "output": {"path", "symlink", "write", "value"},
    "summary": set(CI.FAILURE_ENUM["summary"]),
    "api": set(CI.FAILURE_ENUM["api"]),
    "internal": {"unexpected"},
}


def _safe_failure(stage: Any, kind: Any) -> tuple[str, str]:
    if (
        isinstance(stage, str)
        and isinstance(kind, str)
        and stage in FAILURE_ENUM
        and kind in FAILURE_ENUM[stage]
    ):
        return stage, kind
    return "internal", "unexpected"


@dataclass(frozen=True)
class Artifact:
    id: int
    name: str
    size_in_bytes: int
    expired: bool
    digest: str


@dataclass(frozen=True)
class Selection:
    source_sha: str
    ci_run_id: int
    ci_run_attempt: int
    pointer_artifact_id: int
    pointer_artifact_digest: str
    bundle_artifact_id: int
    bundle_artifact_digest: str


@dataclass(frozen=True)
class Pointer:
    source_sha: str
    run_id: int
    run_attempt: int
    bundle_artifact_id: int
    bundle_artifact_digest: str
    descriptor_sha256: str


@dataclass(frozen=True)
class VerifyConfig:
    source_sha: str
    selection_path: Path
    pointer_path: Path
    github_output: Path


@dataclass(frozen=True)
class VerifiedHandoff:
    ci_run_id: int
    ci_run_attempt: int
    bundle_artifact_id: int
    descriptor_sha256: str
    bundle_artifact_digest: str


def build_api(token: str) -> Any:
    return CI.GitHubApi(token)


def _positive_int(value: Any, stage: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CI.PreflightError(stage, "schema")
    return value


def _sha256(value: Any, stage: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise CI.PreflightError(stage, "digest")
    return value


def _digest(value: Any, stage: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise CI.PreflightError(stage, "digest")
    return value


def _artifact_name(prefix: str, run_id: int, run_attempt: int) -> str:
    return f"{prefix}{run_id}-{run_attempt}"


def _parse_selected_artifact(
    raw: Mapping[str, Any],
    *,
    expected_name: str,
    run_id: int,
    source_sha: str,
    trusted_repository_id: int | None,
) -> Artifact:
    artifact_id = _positive_int(raw.get("id"), "artifacts")
    name = raw.get("name")
    if not isinstance(name, str) or name != expected_name:
        raise CI.PreflightError("artifacts", "name")
    size = _positive_int(raw.get("size_in_bytes"), "artifacts")
    expired = raw.get("expired")
    if not isinstance(expired, bool):
        raise CI.PreflightError("artifacts", "schema")
    digest = _digest(raw.get("digest"), "artifacts")

    workflow_run = raw.get("workflow_run")
    if workflow_run is not None:
        if not isinstance(workflow_run, Mapping):
            raise CI.PreflightError("artifacts", "identity")
        if _positive_int(workflow_run.get("id"), "artifacts") != run_id:
            raise CI.PreflightError("artifacts", "identity")
        if workflow_run.get("head_sha") != source_sha:
            raise CI.PreflightError("artifacts", "identity")
        if workflow_run.get("head_branch") != CI.BRANCH:
            raise CI.PreflightError("artifacts", "identity")
        repository_id = _positive_int(
            workflow_run.get("repository_id"), "artifacts"
        )
        head_repository_id = _positive_int(
            workflow_run.get("head_repository_id"), "artifacts"
        )
        if repository_id != head_repository_id:
            raise CI.PreflightError("artifacts", "identity")
        if (
            trusted_repository_id is not None
            and repository_id != trusted_repository_id
        ):
            raise CI.PreflightError("artifacts", "identity")

    return Artifact(
        id=artifact_id,
        name=name,
        size_in_bytes=size,
        expired=expired,
        digest=digest,
    )


def _select_artifact(
    artifacts: Sequence[Any],
    *,
    expected_name: str,
    run_id: int,
    source_sha: str,
    trusted_repository_id: int | None,
    max_bytes: int,
) -> Artifact:
    matches: list[Artifact] = []
    for raw in artifacts:
        if not isinstance(raw, Mapping):
            raise CI.PreflightError("artifacts", "schema")
        name = raw.get("name")
        if not isinstance(name, str):
            raise CI.PreflightError("artifacts", "schema")
        if name == expected_name:
            matches.append(
                _parse_selected_artifact(
                    raw,
                    expected_name=expected_name,
                    run_id=run_id,
                    source_sha=source_sha,
                    trusted_repository_id=trusted_repository_id,
                )
            )
    if not matches:
        raise CI.PreflightError("artifacts", "missing")
    if len(matches) != 1:
        raise CI.PreflightError("artifacts", "duplicate")
    artifact = matches[0]
    if artifact.expired:
        raise CI.PreflightError("artifacts", "expired")
    if artifact.size_in_bytes > max_bytes:
        raise CI.PreflightError("artifacts", "size")
    return artifact


def select_candidate(source_sha: str, api: Any) -> Selection:
    states = CI.default_workflow_states()
    config = CI.Config(
        source_sha=source_sha,
        summary_path=Path("/unused-release-selection"),
    )
    failure = CI.run_gate(config, api, states)
    if failure is not None:
        raise CI.PreflightError(failure[0], failure[1])
    if len(states) != 1:
        raise CI.PreflightError("internal", "unexpected")
    state = states[0]
    run_id = _positive_int(state.latest_run_id, "internal")
    run_attempt = _positive_int(state.latest_run_attempt, "internal")

    artifacts = api.run_artifacts(run_id)
    pointer = _select_artifact(
        artifacts,
        expected_name=_artifact_name(POINTER_PREFIX, run_id, run_attempt),
        run_id=run_id,
        source_sha=source_sha,
        trusted_repository_id=state.latest_run_repository_id,
        max_bytes=MAX_POINTER_ARTIFACT_BYTES,
    )
    bundle = _select_artifact(
        artifacts,
        expected_name=_artifact_name(BUNDLE_PREFIX, run_id, run_attempt),
        run_id=run_id,
        source_sha=source_sha,
        trusted_repository_id=state.latest_run_repository_id,
        max_bytes=MAX_BUNDLE_ARTIFACT_BYTES,
    )

    # Artifact listing and validation take additional API calls.  Re-run the
    # real preflight with the selected run lock so a newer run or rerun that
    # appeared in that window fails instead of silently switching candidates.
    # This narrows the window; it does not claim atomicity with publishing.
    recheck_states = CI.default_workflow_states()
    recheck_config = CI.Config(
        source_sha=source_sha,
        summary_path=Path("/unused-release-recheck"),
        expected_run_id=run_id,
        expected_run_attempt=run_attempt,
    )
    failure = CI.run_gate(recheck_config, api, recheck_states)
    if failure is not None:
        raise CI.PreflightError(failure[0], failure[1])
    if (
        len(recheck_states) != 1
        or recheck_states[0].latest_run_id != run_id
        or recheck_states[0].latest_run_attempt != run_attempt
    ):
        raise CI.PreflightError("run", "expected-mismatch")

    return Selection(
        source_sha=source_sha,
        ci_run_id=run_id,
        ci_run_attempt=run_attempt,
        pointer_artifact_id=pointer.id,
        pointer_artifact_digest=pointer.digest,
        bundle_artifact_id=bundle.id,
        bundle_artifact_digest=bundle.digest,
    )


def build_selection_summary(
    *,
    result: str,
    failure: tuple[str, str] | None,
    source_sha: Any,
    selection: Selection | None,
) -> dict[str, Any]:
    safe_result = "pass" if result == "pass" else "fail"
    if safe_result == "pass" and failure is not None:
        safe_result = "fail"
    if safe_result == "pass" and selection is None:
        safe_result = "fail"
        failure = ("internal", "unexpected")
    if safe_result == "fail" and failure is None:
        failure = ("internal", "unexpected")
    safe_failure = None
    if safe_result == "fail":
        pair = failure if isinstance(failure, tuple) and len(failure) == 2 else None
        stage, kind = _safe_failure(
            pair[0] if pair is not None else None,
            pair[1] if pair is not None else None,
        )
        safe_failure = {"stage": stage, "kind": kind}
    safe_sha = (
        source_sha
        if isinstance(source_sha, str) and CI.SHA_RE.fullmatch(source_sha)
        else None
    )
    summary: dict[str, Any] = {
        "schema": SELECTION_SCHEMA,
        "result": safe_result,
        "failure": safe_failure,
        "repository": REPOSITORY,
        "workflow_path": WORKFLOW_PATH,
        "source_sha": safe_sha,
        "platform": PLATFORM,
        "ci_run_id": None,
        "ci_run_attempt": None,
        "pointer_artifact_id": None,
        "pointer_artifact_digest": None,
        "bundle_artifact_id": None,
        "bundle_artifact_digest": None,
    }
    if safe_result == "pass" and selection is not None:
        summary.update(
            {
                "source_sha": selection.source_sha,
                "ci_run_id": selection.ci_run_id,
                "ci_run_attempt": selection.ci_run_attempt,
                "pointer_artifact_id": selection.pointer_artifact_id,
                "pointer_artifact_digest": selection.pointer_artifact_digest,
                "bundle_artifact_id": selection.bundle_artifact_id,
                "bundle_artifact_digest": selection.bundle_artifact_digest,
            }
        )
    return summary


def _coerce_path(raw: Any, stage: str) -> Path:
    if isinstance(raw, os.PathLike):
        try:
            raw = os.fspath(raw)
        except TypeError:
            raise CI.PreflightError(stage, "path") from None
    if not isinstance(raw, str) or not raw:
        raise CI.PreflightError(stage, "path")
    path = Path(raw).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise CI.PreflightError(stage, "path")
    return path


def _reject_symlink_components(path: Path, stage: str) -> None:
    current = path
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            raise CI.PreflightError(stage, "path") from None
        else:
            if stat.S_ISLNK(info.st_mode):
                raise CI.PreflightError(stage, "symlink")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _validate_read_path(raw: Any, stage: str) -> Path:
    path = _coerce_path(raw, stage)
    _reject_symlink_components(path, stage)
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise CI.PreflightError(stage, "path") from None
    except OSError:
        raise CI.PreflightError(stage, "path") from None
    if stat.S_ISLNK(info.st_mode):
        raise CI.PreflightError(stage, "symlink")
    if not stat.S_ISREG(info.st_mode):
        raise CI.PreflightError(stage, "path")
    return path


def _read_bounded_json(path: Path, max_bytes: int, stage: str) -> dict[str, Any]:
    path = _validate_read_path(path, stage)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError:
        raise CI.PreflightError(stage, "path") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise CI.PreflightError(stage, "path")
        if info.st_size > max_bytes:
            raise CI.PreflightError(stage, "body-too-large")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(65536, max_bytes + 1 - total),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise CI.PreflightError(stage, "body-too-large")
            chunks.append(chunk)
    except CI.PreflightError:
        raise
    except OSError:
        raise CI.PreflightError(stage, "path") from None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    try:
        data = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CI.PreflightError(stage, "bad-json") from None
    if not isinstance(data, dict):
        raise CI.PreflightError(stage, "schema")
    return data


def load_selection(path: Path, source_sha: str) -> Selection:
    data = _read_bounded_json(path, MAX_SELECTION_BYTES, "selection")
    if set(data) != SELECTION_KEYS:
        raise CI.PreflightError("selection", "schema")
    if data.get("schema") != SELECTION_SCHEMA:
        raise CI.PreflightError("selection", "schema")
    if data.get("result") != "pass":
        raise CI.PreflightError("selection", "result")
    if data.get("failure") is not None:
        raise CI.PreflightError("selection", "failure")
    if data.get("repository") != REPOSITORY:
        raise CI.PreflightError("selection", "provenance")
    if data.get("workflow_path") != WORKFLOW_PATH:
        raise CI.PreflightError("selection", "provenance")
    if data.get("platform") != PLATFORM:
        raise CI.PreflightError("selection", "provenance")
    selected_sha = data.get("source_sha")
    if (
        not isinstance(selected_sha, str)
        or CI.SHA_RE.fullmatch(selected_sha) is None
    ):
        raise CI.PreflightError("selection", "provenance")
    if selected_sha != source_sha:
        raise CI.PreflightError("selection", "identity")
    ci_run_id = _positive_int(data.get("ci_run_id"), "selection")
    ci_run_attempt = _positive_int(data.get("ci_run_attempt"), "selection")
    pointer_artifact_id = _positive_int(
        data.get("pointer_artifact_id"), "selection"
    )
    bundle_artifact_id = _positive_int(
        data.get("bundle_artifact_id"), "selection"
    )
    if pointer_artifact_id == bundle_artifact_id:
        raise CI.PreflightError("selection", "identity")
    pointer_digest = _digest(
        data.get("pointer_artifact_digest"), "selection"
    )
    bundle_digest = _digest(
        data.get("bundle_artifact_digest"), "selection"
    )
    return Selection(
        source_sha=selected_sha,
        ci_run_id=ci_run_id,
        ci_run_attempt=ci_run_attempt,
        pointer_artifact_id=pointer_artifact_id,
        pointer_artifact_digest=pointer_digest,
        bundle_artifact_id=bundle_artifact_id,
        bundle_artifact_digest=bundle_digest,
    )


def load_pointer(path: Path, source_sha: str) -> Pointer:
    data = _read_bounded_json(path, MAX_POINTER_BYTES, "pointer")
    if set(data) != POINTER_KEYS:
        raise CI.PreflightError("pointer", "schema")
    if data.get("schema") != POINTER_SCHEMA:
        raise CI.PreflightError("pointer", "schema")
    if data.get("repository") != REPOSITORY:
        raise CI.PreflightError("pointer", "repository")
    if data.get("workflow_path") != WORKFLOW_PATH:
        raise CI.PreflightError("pointer", "workflow-path")
    if data.get("platform") != PLATFORM:
        raise CI.PreflightError("pointer", "platform")
    pointer_sha = data.get("source_sha")
    if (
        not isinstance(pointer_sha, str)
        or CI.SHA_RE.fullmatch(pointer_sha) is None
    ):
        raise CI.PreflightError("pointer", "source-sha")
    if pointer_sha != source_sha:
        raise CI.PreflightError("pointer", "source-sha")
    run_id = _positive_int(data.get("run_id"), "pointer")
    run_attempt = _positive_int(data.get("run_attempt"), "pointer")
    bundle_artifact_id = _positive_int(
        data.get("bundle_artifact_id"), "pointer"
    )
    bundle_digest = _digest(
        data.get("bundle_artifact_digest"), "pointer"
    )
    descriptor_sha256 = _sha256(
        data.get("descriptor_sha256"), "pointer"
    )
    return Pointer(
        source_sha=pointer_sha,
        run_id=run_id,
        run_attempt=run_attempt,
        bundle_artifact_id=bundle_artifact_id,
        bundle_artifact_digest=bundle_digest,
        descriptor_sha256=descriptor_sha256,
    )


def validate_trusted_context(
    environment: Mapping[str, str],
    source_sha: str,
) -> None:
    """Repeat the trusted main context check without needing an API token.

    ``verify`` does not call GitHub, so the workflow intentionally does not
    pass ``GITHUB_TOKEN`` to that step.  When a token is present it is still
    validated with the same full environment check as ``select``; otherwise
    the event, ref, repository, and SHA checks bind the selection to the same
    trusted workflow_dispatch context.
    """
    if environment.get("GITHUB_EVENT_NAME") != CI.TRIGGER_EVENT:
        raise CI.PreflightError("env", "event")
    if environment.get("GITHUB_REF") != CI.REF:
        raise CI.PreflightError("env", "ref")
    if environment.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise CI.PreflightError("env", "repository")
    sha = environment.get("GITHUB_SHA")
    if not isinstance(sha, str) or CI.SHA_RE.fullmatch(sha) is None:
        raise CI.PreflightError("env", "sha")
    if sha != source_sha:
        raise CI.PreflightError("env", "sha-mismatch")


def verify_candidate(
    config: VerifyConfig,
    environment: Mapping[str, str],
) -> VerifiedHandoff:
    if environment.get("GITHUB_TOKEN") is not None:
        CI.validate_environment(environment, config.source_sha)
    else:
        validate_trusted_context(environment, config.source_sha)
    selection = load_selection(config.selection_path, config.source_sha)
    pointer = load_pointer(config.pointer_path, config.source_sha)
    if pointer.run_id != selection.ci_run_id:
        raise CI.PreflightError("pointer", "selection-mismatch")
    if pointer.run_attempt != selection.ci_run_attempt:
        raise CI.PreflightError("pointer", "selection-mismatch")
    if pointer.bundle_artifact_id != selection.bundle_artifact_id:
        raise CI.PreflightError("pointer", "selection-mismatch")
    if pointer.bundle_artifact_digest != selection.bundle_artifact_digest:
        raise CI.PreflightError("pointer", "selection-mismatch")
    return VerifiedHandoff(
        ci_run_id=pointer.run_id,
        ci_run_attempt=pointer.run_attempt,
        bundle_artifact_id=pointer.bundle_artifact_id,
        descriptor_sha256=pointer.descriptor_sha256,
        bundle_artifact_digest=pointer.bundle_artifact_digest,
    )


def validate_output_path(raw: Any) -> Path:
    path = _coerce_path(raw, "output")
    _reject_symlink_components(path, "output")
    try:
        info = path.lstat()
    except FileNotFoundError:
        parent = path.parent
        try:
            parent_info = parent.lstat()
        except OSError:
            raise CI.PreflightError("output", "path") from None
        if not stat.S_ISDIR(parent_info.st_mode):
            raise CI.PreflightError("output", "path")
        return path
    except OSError:
        raise CI.PreflightError("output", "path") from None
    if stat.S_ISLNK(info.st_mode):
        raise CI.PreflightError("output", "symlink")
    if not stat.S_ISREG(info.st_mode):
        raise CI.PreflightError("output", "path")
    return path


def _valid_output_value(key: str, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if key in ("ci_run_id", "ci_run_attempt", "pointer_artifact_id",
               "bundle_artifact_id"):
        return DECIMAL_RE.fullmatch(value) is not None
    if key == "descriptor_sha256":
        return SHA256_RE.fullmatch(value) is not None
    if key == "bundle_artifact_digest":
        return DIGEST_RE.fullmatch(value) is not None
    return False


def _rollback_github_output(descriptor: int, initial_size: int | None) -> None:
    """Best-effort removal of partially appended output lines."""
    if initial_size is None:
        return
    try:
        os.ftruncate(descriptor, initial_size)
        os.fsync(descriptor)
    except OSError:
        pass


def write_github_output(
    path: Path,
    pairs: Sequence[tuple[str, str]],
    allowed_keys: frozenset[str],
) -> None:
    """Append fixed key/value lines, separating only when needed."""
    for key, value in pairs:
        if key not in allowed_keys or not _valid_output_value(key, value):
            raise CI.PreflightError("output", "value")
    try:
        descriptor = os.open(
            path,
            os.O_RDWR
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except OSError:
        raise CI.PreflightError("output", "write") from None
    initial_size: int | None = None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise CI.PreflightError("output", "path")
        initial_size = info.st_size
        if initial_size > 0:
            last = os.pread(descriptor, 1, initial_size - 1)
            if last != b"\n":
                _write_all(descriptor, b"\n")
        for key, value in pairs:
            _write_all(descriptor, f"{key}={value}\n".encode("ascii"))
        os.fsync(descriptor)
    except CI.PreflightError:
        _rollback_github_output(descriptor, initial_size)
        raise
    except OSError:
        _rollback_github_output(descriptor, initial_size)
        raise CI.PreflightError("output", "write") from None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise CI.PreflightError("output", "write")
        offset += written


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description=(
            "Select or verify a run-bound release candidate from the latest "
            "successful main CI run."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    select_parser = subparsers.add_parser("select", allow_abbrev=False)
    select_parser.add_argument("--source-sha", required=True)
    select_parser.add_argument("--summary", required=True)
    select_parser.add_argument("--github-output", required=True)

    verify_parser = subparsers.add_parser("verify", allow_abbrev=False)
    verify_parser.add_argument("--source-sha", required=True)
    verify_parser.add_argument("--selection", required=True)
    verify_parser.add_argument("--pointer", required=True)
    verify_parser.add_argument("--github-output", required=True)
    return parser.parse_args(argv)


def _print_failure(stage: str, kind: str) -> None:
    safe_stage, safe_kind = _safe_failure(stage, kind)
    print(f"{STDOUT_PREFIX} fail stage={safe_stage} kind={safe_kind}")


def _main_select(args: argparse.Namespace) -> int:
    try:
        summary_path = CI.validate_summary_path(args.summary)
    except CI.PreflightError as exc:
        _print_failure(exc.stage, exc.kind)
        return 1
    except Exception:
        _print_failure("internal", "unexpected")
        return 1

    raw_source = getattr(args, "source_sha", None)
    safe_source = (
        raw_source
        if isinstance(raw_source, str) and CI.SHA_RE.fullmatch(raw_source)
        else None
    )
    failure: tuple[str, str] | None = None
    selection: Selection | None = None
    output_path: Path | None = None
    try:
        output_path = validate_output_path(args.github_output)
        source_sha = CI.validate_source_sha(raw_source)
        token = CI.validate_environment(os.environ, source_sha)
        api = build_api(token)
        selection = select_candidate(source_sha, api)
    except CI.PreflightError as exc:
        failure = (exc.stage, exc.kind)
    except Exception:
        failure = ("internal", "unexpected")

    try:
        summary = build_selection_summary(
            result="pass" if failure is None else "fail",
            failure=failure,
            source_sha=safe_source,
            selection=selection,
        )
        # Persist provenance before exposing candidate outputs.  The summary
        # and GITHUB_OUTPUT are separate files and are not written atomically.
        CI.write_summary(summary_path, summary)
    except CI.PreflightError as exc:
        _print_failure(exc.stage, exc.kind)
        return 1
    except Exception:
        _print_failure("summary", "write")
        return 1

    if failure is not None:
        _print_failure(failure[0], failure[1])
        return 1

    if selection is None or output_path is None:
        _print_failure("internal", "unexpected")
        return 1
    try:
        write_github_output(
            output_path,
            [
                ("ci_run_id", str(selection.ci_run_id)),
                ("ci_run_attempt", str(selection.ci_run_attempt)),
                ("pointer_artifact_id", str(selection.pointer_artifact_id)),
            ],
            SELECT_OUTPUT_KEYS,
        )
    except CI.PreflightError as exc:
        _print_failure(exc.stage, exc.kind)
        return 1
    except Exception:
        _print_failure("output", "write")
        return 1
    print(f"{STDOUT_PREFIX} select pass")
    return 0


def _main_verify(args: argparse.Namespace) -> int:
    try:
        output_path = validate_output_path(args.github_output)
        source_sha = CI.validate_source_sha(args.source_sha)
        config = VerifyConfig(
            source_sha=source_sha,
            selection_path=Path(args.selection),
            pointer_path=Path(args.pointer),
            github_output=output_path,
        )
        handoff = verify_candidate(config, os.environ)
    except CI.PreflightError as exc:
        _print_failure(exc.stage, exc.kind)
        return 1
    except Exception:
        _print_failure("internal", "unexpected")
        return 1

    try:
        write_github_output(
            output_path,
            [
                ("ci_run_id", str(handoff.ci_run_id)),
                ("ci_run_attempt", str(handoff.ci_run_attempt)),
                ("bundle_artifact_id", str(handoff.bundle_artifact_id)),
                ("descriptor_sha256", handoff.descriptor_sha256),
                ("bundle_artifact_digest", handoff.bundle_artifact_digest),
            ],
            VERIFY_OUTPUT_KEYS,
        )
    except CI.PreflightError as exc:
        _print_failure(exc.stage, exc.kind)
        return 1
    except Exception:
        _print_failure("output", "write")
        return 1
    print(f"{STDOUT_PREFIX} verify pass")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "select":
        return _main_select(args)
    return _main_verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
