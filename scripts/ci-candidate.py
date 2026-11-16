#!/usr/bin/env python3
"""Export one trusted CI-built release candidate and its pointer.

This is intentionally limited to CI main-push context.  ``export`` consumes an
already accepted builder output tree, verifies the local image pair, and writes
the four-file release bundle plus the internal handoff pointer.  It never runs
the builder, local gates, Docker login/push, or any network request.

``pointer`` writes a closed, typed pointer for the uploaded candidate artifact.
The selector is responsible for independently checking the referenced run and
artifact provenance.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
_RELEASE_SPEC = importlib.util.spec_from_file_location(
    "ci_candidate_release", SCRIPT_DIR / "release.py"
)
if _RELEASE_SPEC is None or _RELEASE_SPEC.loader is None:
    raise SystemExit("release.py is unavailable")
release = importlib.util.module_from_spec(_RELEASE_SPEC)
sys.modules[_RELEASE_SPEC.name] = release
_RELEASE_SPEC.loader.exec_module(release)

CANDIDATE_SCHEMA = "payment-hub.ci-candidate.v1"
WORKFLOW_PATH = ".github/workflows/ci.yml"
WORKFLOW_REF = (
    "xtianxx/payment-hub/.github/workflows/ci.yml@refs/heads/main"
)


def _validate_main_push_context(
    env: Mapping[str, str], source_sha: str
) -> None:
    if env.get("GITHUB_ACTIONS") != "true":
        raise release.ReleaseError("context", "ci")
    if env.get("GITHUB_EVENT_NAME") != "push":
        raise release.ReleaseError("context", "event")
    if env.get("GITHUB_REF") != "refs/heads/main":
        raise release.ReleaseError("context", "ref")
    if env.get("GITHUB_REPOSITORY") != release.REPOSITORY:
        raise release.ReleaseError("context", "repository")
    if env.get("GITHUB_WORKFLOW_REF") != WORKFLOW_REF:
        raise release.ReleaseError("context", "workflow-ref")
    github_sha = release._source_sha(
        env.get("GITHUB_SHA"), "context", "github-sha"
    )
    if github_sha != source_sha:
        raise release.ReleaseError("context", "sha")


def _validate_git_context(instance: release.Release, source_sha: str) -> None:
    head = instance._git(["rev-parse", "HEAD"])
    if (
        head.timed_out
        or head.exit_code != 0
        or head.stdout.strip() != source_sha
    ):
        raise release.ReleaseError("context", "git-head")
    status = instance._git(["status", "--porcelain"])
    if status.timed_out or status.exit_code != 0:
        raise release.ReleaseError("context", "git-status")
    if status.stdout.strip():
        raise release.ReleaseError("context", "dirty")


def _absolute_path(value: str, stage: str, kind: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise release.ReleaseError(stage, kind)
    return path


def _positive_decimal(value: Any, stage: str, kind: str) -> int:
    if not isinstance(value, str) or re.fullmatch(
        r"[1-9][0-9]*", value
    ) is None:
        raise release.ReleaseError(stage, kind)
    return int(value)


def _normalize_artifact_digest(value: Any) -> str:
    if not isinstance(value, str):
        raise release.ReleaseError("pointer", "artifact-digest")
    digest = value[7:] if value.startswith("sha256:") else value
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise release.ReleaseError("pointer", "artifact-digest")
    return "sha256:" + digest


def _validate_output_value(value: str) -> str:
    """Reject values that could inject extra GITHUB_OUTPUT lines."""
    if not isinstance(value, str) or not value:
        raise release.ReleaseError("artifact", "github-output")
    if "\r" in value or "\n" in value:
        raise release.ReleaseError("artifact", "github-output")
    return value


def _validate_github_output_path(path: Path, root: Path) -> None:
    """Precheck the output file before any candidate artifact is written.

    The output may be an existing regular file outside the artifact root so
    later workflow steps can append their own keys.  It may not be a symlink,
    a non-regular file, or an alias/descendant of the artifact root (which
    contains the bundle, descriptor, and handoff pointer).
    """
    try:
        if path.is_symlink():
            raise release.ReleaseError("artifact", "github-output")
        if path.exists():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise release.ReleaseError("artifact", "github-output")
        parent_info = path.parent.lstat()
    except release.ReleaseError:
        raise
    except OSError as exc:
        raise release.ReleaseError("artifact", "github-output") from exc
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(
        parent_info.st_mode
    ):
        raise release.ReleaseError("artifact", "github-output")
    canonical = Path(os.path.realpath(path))
    canonical_root = Path(os.path.realpath(root))
    forbidden = {
        canonical_root,
        canonical_root / release.DEFAULT_BUNDLE,
        canonical_root / release.DEFAULT_BUNDLE / release.DESCRIPTOR_FILE,
        canonical_root / release.HANDOFF_FILE,
    }
    if canonical in forbidden:
        raise release.ReleaseError("artifact", "github-output")
    try:
        canonical.relative_to(canonical_root)
    except ValueError:
        return
    raise release.ReleaseError("artifact", "github-output")


def _write_github_output(
    path: Path,
    *,
    bundle_dir: Path,
    descriptor_sha256: str,
    source_sha: str,
) -> None:
    lines = [
        ("bundle_dir", str(bundle_dir)),
        ("descriptor_sha256", descriptor_sha256),
        ("source_sha", source_sha),
    ]
    for name, value in lines:
        _validate_output_value(value)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise release.ReleaseError("artifact", "github-output") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise release.ReleaseError("artifact", "github-output")
        os.fchmod(descriptor, 0o600)
    except release.ReleaseError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise release.ReleaseError("artifact", "github-output") from exc
    try:
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(
                "".join(f"{name}={value}\n" for name, value in lines)
            )
    except OSError as exc:
        raise release.ReleaseError("artifact", "github-output") from exc


def _export_impl(
    args: argparse.Namespace,
    env: Mapping[str, str],
    instance: release.Release,
) -> int:
    source_sha = release._source_sha(
        args.source_sha, "context", "source-sha"
    )
    root = _absolute_path(args.artifact_root, "cli", "artifact-root")
    build_root = _absolute_path(args.build_root, "cli", "build-root")
    output = _absolute_path(args.github_output, "cli", "github-output")
    _validate_main_push_context(env, source_sha)
    _validate_github_output_path(output, root)
    _validate_output_value(str(root / release.DEFAULT_BUNDLE))
    instance.env = dict(env)
    _validate_git_context(instance, source_sha)
    if build_root.is_symlink():
        raise release.ReleaseError("artifact", "symlink")
    release._fresh_root(root)
    exported = instance._export_release_bundle(
        root=root,
        build_root=build_root,
        trusted=release.TrustedContext(
            source_sha=source_sha,
            repo=instance.repo,
        ),
    )
    _write_github_output(
        output,
        bundle_dir=exported["bundle"],
        descriptor_sha256=exported["descriptor_sha"],
        source_sha=source_sha,
    )
    return 0


def _export(
    args: argparse.Namespace,
    env: Mapping[str, str],
    instance: release.Release | None = None,
) -> int:
    clean_env = dict(env)
    clean_env.pop("GITHUB_TOKEN", None)
    clean_env.pop(release.AUDIT_TOKEN_ENV, None)
    if instance is None:
        instance = release.Release(
            repo=release.REPO_ROOT,
            env=clean_env,
        )
    else:
        instance.env = clean_env
    instance._cancel_signal = None
    instance._cancel_cleanup_failed = False
    instance._in_cleanup = False
    instance._delegated_starting = False
    release.check_images._reset_process_cancel()
    with instance._cancel_handlers():
        return _export_impl(args, clean_env, instance)


def _pointer(
    args: argparse.Namespace,
    env: Mapping[str, str],
) -> int:
    source_sha = release._source_sha(
        args.source_sha, "pointer", "source-sha"
    )
    _validate_main_push_context(env, source_sha)
    run_id = _positive_decimal(
        env.get("GITHUB_RUN_ID"), "pointer", "run-id"
    )
    run_attempt = _positive_decimal(
        env.get("GITHUB_RUN_ATTEMPT"), "pointer", "run-attempt"
    )
    artifact_id = _positive_decimal(
        args.bundle_artifact_id, "pointer", "artifact-id"
    )
    artifact_digest = _normalize_artifact_digest(
        args.bundle_artifact_digest
    )
    descriptor_sha256 = release._sha256_hex(
        args.descriptor_sha256, "pointer", "descriptor-sha"
    )
    output = _absolute_path(args.output, "pointer", "output")
    if output.is_symlink():
        raise release.ReleaseError("pointer", "output-symlink")
    if output.exists():
        raise release.ReleaseError("pointer", "output")
    parent = output.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise release.ReleaseError("pointer", "output-parent")
    try:
        release._fresh_root(parent)
    except release.ReleaseError as exc:
        raise release.ReleaseError("pointer", "output-parent") from exc
    pointer = {
        "schema": CANDIDATE_SCHEMA,
        "repository": release.REPOSITORY,
        "workflow_path": WORKFLOW_PATH,
        "source_sha": source_sha,
        "platform": release.PLATFORM,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "bundle_artifact_id": artifact_id,
        "bundle_artifact_digest": artifact_digest,
        "descriptor_sha256": descriptor_sha256,
    }
    try:
        release._write_json_exclusive(output, pointer)
    except release.ReleaseError as exc:
        raise release.ReleaseError("pointer", "output") from exc
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export or point at one trusted CI release candidate."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export")
    export.add_argument("--source-sha", required=True)
    export.add_argument("--build-root", required=True)
    export.add_argument("--artifact-root", required=True)
    export.add_argument("--github-output", required=True)
    pointer = subparsers.add_parser("pointer")
    pointer.add_argument("--source-sha", required=True)
    pointer.add_argument("--descriptor-sha256", required=True)
    pointer.add_argument("--bundle-artifact-id", required=True)
    pointer.add_argument("--bundle-artifact-digest", required=True)
    pointer.add_argument("--output", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    instance: release.Release | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    source_env = dict(os.environ) if env is None else dict(env)
    try:
        args = _build_parser().parse_args(argv)
        if args.command == "export":
            return _export(args, source_env, instance)
        if args.command == "pointer":
            return _pointer(args, source_env)
        raise release.ReleaseError("cli", "command")
    except release.ReleaseError as exc:
        print(f"ERROR: {exc.stage}/{exc.kind}", file=sys.stderr)
        print(f"result: fail stage={exc.stage} kind={exc.kind}")
        return 1
    except Exception:
        print("ERROR: internal/unexpected", file=sys.stderr)
        print("result: fail stage=internal kind=unexpected")
        return 1


if __name__ == "__main__":
    sys.exit(main())
