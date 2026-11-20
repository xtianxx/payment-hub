#!/usr/bin/env python3
"""Offline consumer-side verification of one paired Payment Hub release.

The offline verifier accepts the three release artifacts produced by a
successful ``release.yml`` publish run plus the corresponding GitHub Actions
run metadata.  It answers only whether those bytes agree with each other and
with an independently supplied source SHA, run ID, and attempt.  It does not
contact GitHub or GHCR, does not prove where local files came from, and does
not prove that either image can currently be pulled or may be deployed.

``verify_release`` remains a pure function over bytes.  The offline CLI reads
only the four supplied files.  The separate ``--online`` branch reads
``GH_TOKEN`` or ``GITHUB_TOKEN`` from the environment and delegates source
collection to ``release_consumable_source.verify_online``; it never imports
that module on the offline path and never probes GHCR pullability.

All failures are reported as fixed, sanitized categories.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from typing import Any, Mapping, Sequence

EVIDENCE_SCHEMA = "payment-hub.release-evidence.v1"
SUMMARY_SCHEMA = "payment-hub.release-summary.v1"
DESCRIPTOR_SCHEMA = "payment-hub.release-handoff.v1"
REPOSITORY = "xtianxx/payment-hub"
OWNER_LOGIN = "xtianxx"
PLATFORM = "linux/amd64"
RELEASE_WORKFLOW_PATH = ".github/workflows/release.yml"
RELEASE_BRANCH = "main"
RELEASE_EVENT = "workflow_dispatch"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
ROLES = ("app", "migrate")
TARGETS = {
    "app": "ghcr.io/xtianxx/payment-hub-app",
    "migrate": "ghcr.io/xtianxx/payment-hub-migrate",
}
PACKAGE_NAMES = {
    "app": "payment-hub-app",
    "migrate": "payment-hub-migrate",
}
OFFLINE_MODE = "offline"
ONLINE_MODE = "online"
ONLINE_SOURCE_MODULE = "release_consumable_source.py"
ONLINE_SOURCE_MODULE_NAME = "release_consumable_source"

SUMMARY_MAX_BYTES = 8 * 1024 * 1024
DESCRIPTOR_MAX_BYTES = 64 * 1024
EVIDENCE_MAX_BYTES = 64 * 1024
RUN_METADATA_MAX_BYTES = 8 * 1024 * 1024

EVIDENCE_KEYS = frozenset(
    {
        "schema",
        "repository",
        "source_sha",
        "release_run_id",
        "release_run_attempt",
        "ci_run_id",
        "ci_run_attempt",
        "bundle_artifact_id",
        "bundle_artifact_digest",
        "descriptor_sha256",
    }
)
DESCRIPTOR_KEYS = frozenset(
    {
        "schema",
        "source_sha",
        "source_manifest_digest",
        "platform",
        "created_utc",
        "archive",
        "builder",
        "images",
        "targets",
        "tag",
    }
)
SUMMARY_REQUIRED_KEYS = frozenset(
    {
        "schema",
        "stage",
        "outcome",
        "published",
        "failure",
        "source_sha",
        "platform",
        "descriptor_sha256",
        "pushed_roles",
        "push_attempted_roles",
        "push_side_effects",
        "packages_initial",
        "packages_postcheck",
        "push",
        "remote",
        "pull",
        "revalidation",
        "credentials_removed",
        "cleanup_failed",
    }
)
RUN_REQUIRED_KEYS = frozenset(
    {
        "id",
        "run_attempt",
        "head_sha",
        "head_branch",
        "event",
        "status",
        "conclusion",
        "path",
        "repository",
    }
)
RELEASE_PATHS = frozenset(
    {
        RELEASE_WORKFLOW_PATH,
        f"{RELEASE_WORKFLOW_PATH}@{RELEASE_BRANCH}",
        f"{RELEASE_WORKFLOW_PATH}@refs/heads/{RELEASE_BRANCH}",
    }
)

_SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


class VerificationError(RuntimeError):
    """A fixed, sanitized verification failure category."""

    def __init__(self, stage: str, kind: str) -> None:
        self.stage = stage
        self.kind = kind
        super().__init__(f"{stage}/{kind}")


class _DuplicateKey(ValueError):
    """Raised when a JSON object repeats a key at any nesting depth."""


def _reject_duplicate_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> Any:
    raise ValueError


def _parse_json_object(data: bytes, stage: str, maximum: int) -> dict[str, Any]:
    if not isinstance(data, bytes):
        raise VerificationError(stage, "type")
    if not data:
        raise VerificationError(stage, "empty")
    if len(data) > maximum:
        raise VerificationError(stage, "size")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError(stage, "encoding") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateKey as exc:
        raise VerificationError(stage, "duplicate-key") from exc
    except (TypeError, ValueError, RecursionError) as exc:
        raise VerificationError(stage, "json") from exc
    if not isinstance(value, dict):
        raise VerificationError(stage, "object")
    return value


def _source_sha(value: Any, stage: str, kind: str = "source") -> str:
    if not isinstance(value, str) or _SOURCE_SHA_RE.fullmatch(value) is None:
        raise VerificationError(stage, kind)
    return value


def _sha256_hex(value: Any, stage: str, kind: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise VerificationError(stage, kind)
    return value


def _digest(value: Any, stage: str, kind: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise VerificationError(stage, kind)
    return value


def _positive_int(value: Any, stage: str, kind: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise VerificationError(stage, kind)
    return value


def _bounded_text(
    value: Any, stage: str, kind: str, *, maximum: int = 512
) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise VerificationError(stage, kind)
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise VerificationError(stage, kind)
    return value


def _safe_reference(value: Any, stage: str, kind: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise VerificationError(stage, kind)
    if value.startswith("-") or any(
        ord(character) <= 0x20 or ord(character) == 0x7F
        for character in value
    ):
        raise VerificationError(stage, kind)
    return value


def _mapping(value: Any, stage: str, kind: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VerificationError(stage, kind)
    return value


def _role_keys(value: Any, stage: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(ROLES):
        raise VerificationError(stage, "roles")
    return value


def _role_map(value: Any, stage: str) -> dict[str, dict[str, Any]]:
    role_values = _role_keys(value, stage)
    result: dict[str, dict[str, Any]] = {}
    for role in ROLES:
        result[role] = _mapping(role_values[role], stage, role)
    return result


def _role_list(value: Any, stage: str, kind: str) -> list[str]:
    if not isinstance(value, list) or len(value) != len(ROLES):
        raise VerificationError(stage, kind)
    if any(not isinstance(item, str) for item in value):
        raise VerificationError(stage, kind)
    if len(set(value)) != len(value) or set(value) != set(ROLES):
        raise VerificationError(stage, kind)
    return value


def _validate_expected(
    expected_source_sha: Any,
    release_run_id: Any,
    release_run_attempt: Any,
    expected_app_digest: Any,
    expected_migrate_digest: Any,
) -> tuple[str, int, int, str | None, str | None]:
    source_sha = _source_sha(
        expected_source_sha, "input", "expected-source-sha"
    )
    run_id = _positive_int(release_run_id, "input", "release-run-id")
    attempt = _positive_int(
        release_run_attempt, "input", "release-run-attempt"
    )
    if (expected_app_digest is None) != (expected_migrate_digest is None):
        raise VerificationError("input", "digest-pair")
    app_digest: str | None = None
    migrate_digest: str | None = None
    if expected_app_digest is not None:
        app_digest = _digest(
            expected_app_digest, "input", "expected-app-digest"
        )
        migrate_digest = _digest(
            expected_migrate_digest, "input", "expected-migrate-digest"
        )
    return source_sha, run_id, attempt, app_digest, migrate_digest


def _validate_evidence(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != EVIDENCE_KEYS:
        raise VerificationError("evidence", "keys")
    if value.get("schema") != EVIDENCE_SCHEMA:
        raise VerificationError("evidence", "schema")
    if value.get("repository") != REPOSITORY:
        raise VerificationError("evidence", "repository")
    _source_sha(value.get("source_sha"), "evidence", "source")
    _positive_int(value.get("release_run_id"), "evidence", "release-run-id")
    _positive_int(
        value.get("release_run_attempt"), "evidence", "release-run-attempt"
    )
    _positive_int(value.get("ci_run_id"), "evidence", "ci-run-id")
    _positive_int(
        value.get("ci_run_attempt"), "evidence", "ci-run-attempt"
    )
    _positive_int(
        value.get("bundle_artifact_id"), "evidence", "bundle-artifact-id"
    )
    _digest(
        value.get("bundle_artifact_digest"),
        "evidence",
        "bundle-artifact-digest",
    )
    _sha256_hex(
        value.get("descriptor_sha256"), "evidence", "descriptor-sha"
    )
    return value


def _validate_run_metadata(value: dict[str, Any]) -> dict[str, Any]:
    missing = RUN_REQUIRED_KEYS - set(value)
    if missing:
        raise VerificationError("run", "keys")
    _positive_int(value.get("id"), "run", "id")
    _positive_int(value.get("run_attempt"), "run", "attempt")
    head_sha = value.get("head_sha")
    if not isinstance(head_sha, str) or _SOURCE_SHA_RE.fullmatch(head_sha) is None:
        raise VerificationError("run", "source")
    if value.get("head_branch") != RELEASE_BRANCH:
        raise VerificationError("run", "branch")
    if value.get("event") != RELEASE_EVENT:
        raise VerificationError("run", "event")
    if value.get("status") != "completed":
        raise VerificationError("run", "status")
    if value.get("conclusion") != "success":
        raise VerificationError("run", "conclusion")
    path = value.get("path")
    if not isinstance(path, str) or path not in RELEASE_PATHS:
        raise VerificationError("run", "workflow")
    repository = _mapping(value.get("repository"), "run", "repository")
    full_name = repository.get("full_name")
    if (
        not isinstance(full_name, str)
        or full_name.casefold() != REPOSITORY.casefold()
    ):
        raise VerificationError("run", "repository")
    return value


def _validate_descriptor(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != DESCRIPTOR_KEYS:
        raise VerificationError("descriptor", "keys")
    if value.get("schema") != DESCRIPTOR_SCHEMA:
        raise VerificationError("descriptor", "schema")
    _source_sha(value.get("source_sha"), "descriptor", "source")
    manifest_digest = _sha256_hex(
        value.get("source_manifest_digest"), "descriptor", "manifest-digest"
    )
    if value.get("platform") != PLATFORM:
        raise VerificationError("descriptor", "platform")
    _bounded_text(value.get("created_utc"), "descriptor", "utc", maximum=64)
    archive = _mapping(value.get("archive"), "descriptor", "archive")
    if set(archive) != {"file", "sha256", "size"}:
        raise VerificationError("descriptor", "archive")
    if archive.get("file") != "pair.tar":
        raise VerificationError("descriptor", "archive")
    _sha256_hex(archive.get("sha256"), "descriptor", "archive-sha")
    archive_size = _positive_int(
        archive.get("size"), "descriptor", "archive-size"
    )
    if archive_size > 2 * 1024 * 1024 * 1024:
        raise VerificationError("descriptor", "archive-size")
    builder = _mapping(value.get("builder"), "descriptor", "builder")
    if set(builder) != {
        "build_record_sha256",
        "accepted_report_sha256",
        "accepted_report_file",
    }:
        raise VerificationError("descriptor", "builder")
    _sha256_hex(
        builder.get("build_record_sha256"), "descriptor", "builder-sha"
    )
    _sha256_hex(
        builder.get("accepted_report_sha256"), "descriptor", "builder-sha"
    )
    if builder.get("accepted_report_file") != "accepted-images.json":
        raise VerificationError("descriptor", "builder-file")
    images = _mapping(value.get("images"), "descriptor", "images")
    if set(images) != set(ROLES):
        raise VerificationError("descriptor", "images")
    local_ids: dict[str, str] = {}
    for role in ROLES:
        image = _mapping(images.get(role), "descriptor", "image")
        if set(image) != {
            "reference",
            "local_id",
            "root_descriptor",
            "source_label",
            "platform",
        }:
            raise VerificationError("descriptor", "image")
        _safe_reference(image.get("reference"), "descriptor", "reference")
        local_id = _digest(
            image.get("local_id"), "descriptor", "local-id"
        )
        local_ids[role] = local_id
        root = _mapping(
            image.get("root_descriptor"), "descriptor", "root"
        )
        if set(root) != {"mediaType", "digest", "size"}:
            raise VerificationError("descriptor", "root")
        if root.get("mediaType") != OCI_INDEX:
            raise VerificationError("descriptor", "root")
        root_digest = _digest(
            root.get("digest"), "descriptor", "root-digest"
        )
        if root_digest != local_id:
            raise VerificationError("descriptor", "local-id")
        root_size = _positive_int(
            root.get("size"), "descriptor", "root-size"
        )
        if root_size > 4 * 1024 * 1024:
            raise VerificationError("descriptor", "root-size")
        if (
            _sha256_hex(
                image.get("source_label"), "descriptor", "source-label"
            )
            != manifest_digest
        ):
            raise VerificationError("descriptor", "source-label")
        if image.get("platform") != PLATFORM:
            raise VerificationError("descriptor", "platform")
    if local_ids["app"] == local_ids["migrate"]:
        raise VerificationError("descriptor", "pair")
    targets = value.get("targets")
    if not isinstance(targets, dict) or targets != TARGETS:
        raise VerificationError("descriptor", "targets")
    source_sha = value["source_sha"]
    if value.get("tag") != "sha-" + source_sha:
        raise VerificationError("descriptor", "tag")
    return value


def _validate_packages_projection(
    summary: dict[str, Any], postcheck: dict[str, Any]
) -> None:
    """Validate ``summary.packages`` when present.

    The success summary writes this compatibility projection without an
    ``owner`` field, while ``packages_postcheck`` includes it.  Unknown
    fields stay acceptable, but every known field must agree with the
    validated postcheck metadata.
    """
    if "packages" not in summary:
        return
    packages = _role_map(summary.get("packages"), "summary")
    for role in ROLES:
        package = _mapping(packages[role], "summary", "packages")
        if package.get("name") != PACKAGE_NAMES[role]:
            raise VerificationError("summary", "package-name")
        if package.get("visibility") != "public":
            raise VerificationError("summary", "package-visibility")
        if package.get("repository") != REPOSITORY:
            raise VerificationError("summary", "package-repository")
        package_id = _positive_int(
            package.get("id"), "summary", "package-id"
        )
        if "owner" in package and package.get("owner") != OWNER_LOGIN:
            raise VerificationError("summary", "package-owner")
        checked = postcheck[role]
        if (
            package.get("name") != checked.get("name")
            or package.get("visibility") != checked.get("visibility")
            or package.get("repository") != checked.get("repository")
            or package_id != checked.get("id")
        ):
            raise VerificationError("summary", "packages")


def _validate_package_metadata(
    value: Any, role: str, stage: str
) -> dict[str, Any]:
    package = _mapping(value, stage, role)
    if package.get("name") != PACKAGE_NAMES[role]:
        raise VerificationError(stage, "package-name")
    if package.get("visibility") != "public":
        raise VerificationError(stage, "package-visibility")
    if package.get("repository") != REPOSITORY:
        raise VerificationError(stage, "package-repository")
    _positive_int(package.get("id"), stage, "package-id")
    if package.get("owner") != OWNER_LOGIN:
        raise VerificationError(stage, "package-owner")
    return package


def _validate_summary(value: dict[str, Any]) -> dict[str, Any]:
    missing = SUMMARY_REQUIRED_KEYS - set(value)
    if missing:
        raise VerificationError("summary", "keys")
    if value.get("schema") != SUMMARY_SCHEMA:
        raise VerificationError("summary", "schema")
    if value.get("stage") != "publish":
        raise VerificationError("summary", "stage")
    if value.get("outcome") != "pass":
        raise VerificationError("summary", "outcome")
    if value.get("published") is not True:
        raise VerificationError("summary", "published")
    if value.get("failure") is not None:
        raise VerificationError("summary", "failure")
    if value.get("credentials_removed") is not True:
        raise VerificationError("summary", "credentials")
    if value.get("cleanup_failed") is not False:
        raise VerificationError("summary", "cleanup")
    _source_sha(value.get("source_sha"), "summary", "source")
    if value.get("platform") != PLATFORM:
        raise VerificationError("summary", "platform")
    _sha256_hex(
        value.get("descriptor_sha256"), "summary", "descriptor-sha"
    )
    _role_list(value.get("pushed_roles"), "summary", "pushed-roles")
    _role_list(
        value.get("push_attempted_roles"), "summary", "attempted-roles"
    )
    side_effects = _role_keys(value.get("push_side_effects"), "summary")
    for role in ROLES:
        if side_effects[role] != "verified":
            raise VerificationError("summary", "side-effects")
    initial = _role_map(value.get("packages_initial"), "summary")
    postcheck = _role_map(value.get("packages_postcheck"), "summary")
    for role in ROLES:
        package_initial = _mapping(initial[role], "summary", "package-initial")
        if package_initial.get("status") != "EXISTING_VALID":
            raise VerificationError("summary", "package-initial")
        _positive_int(
            package_initial.get("id"), "summary", "package-initial-id"
        )
        package_postcheck = _validate_package_metadata(
            postcheck[role], role, "summary"
        )
        if package_initial["id"] != package_postcheck["id"]:
            raise VerificationError("summary", "package-original-id")
    _validate_packages_projection(value, postcheck)
    push = _role_map(value.get("push"), "summary")
    remote = _role_map(value.get("remote"), "summary")
    pull = _role_map(value.get("pull"), "summary")
    for role in ROLES:
        push_role = _mapping(push[role], "summary", "push")
        push_exit = push_role.get("exit")
        if type(push_exit) is not int or push_exit != 0:
            raise VerificationError("summary", "push-exit")
        _digest(
            push_role.get("candidate_digest"), "summary", "push-digest"
        )
        remote_role = _mapping(remote[role], "summary", "remote")
        _digest(remote_role.get("digest"), "summary", "remote-digest")
        pull_role = _mapping(pull[role], "summary", "pull")
        pull_exit = pull_role.get("exit")
        if type(pull_exit) is not int or pull_exit != 0:
            raise VerificationError("summary", "pull-exit")
        _digest(pull_role.get("id"), "summary", "pull-id")
    revalidation = _mapping(
        value.get("revalidation"), "summary", "revalidation"
    )
    if revalidation.get("result") != "pass":
        raise VerificationError("summary", "revalidation")
    for field, kind in (
        ("checks_sha256", "revalidation-checks-sha"),
        ("delivery_record_sha256", "revalidation-delivery-record-sha"),
    ):
        if field in revalidation:
            _sha256_hex(revalidation.get(field), "summary", kind)
    return value


def _cross_check(
    summary: dict[str, Any],
    descriptor: dict[str, Any],
    evidence: dict[str, Any],
    run_metadata: dict[str, Any],
    expected_source_sha: str,
    release_run_id: int,
    release_run_attempt: int,
    descriptor_hex: str,
) -> None:
    if summary.get("source_sha") != expected_source_sha:
        raise VerificationError("summary", "source")
    if descriptor.get("source_sha") != expected_source_sha:
        raise VerificationError("descriptor", "source")
    if evidence.get("source_sha") != expected_source_sha:
        raise VerificationError("evidence", "source")
    if run_metadata.get("head_sha") != expected_source_sha:
        raise VerificationError("run", "source")

    if run_metadata.get("id") != release_run_id:
        raise VerificationError("run", "id")
    if run_metadata.get("run_attempt") != release_run_attempt:
        raise VerificationError("run", "attempt")
    if evidence.get("release_run_id") != release_run_id:
        raise VerificationError("evidence", "release-run-id")
    if evidence.get("release_run_attempt") != release_run_attempt:
        raise VerificationError("evidence", "release-run-attempt")

    if summary.get("descriptor_sha256") != descriptor_hex:
        raise VerificationError("summary", "descriptor-sha")
    if evidence.get("descriptor_sha256") != descriptor_hex:
        raise VerificationError("evidence", "descriptor-sha")

    if summary.get("platform") != descriptor.get("platform"):
        raise VerificationError("summary", "platform")

    repository = run_metadata.get("repository")
    full_name = repository.get("full_name") if isinstance(repository, dict) else None
    if (
        not isinstance(full_name, str)
        or full_name.casefold() != REPOSITORY.casefold()
    ):
        raise VerificationError("run", "repository")
    if evidence.get("repository") != REPOSITORY:
        raise VerificationError("evidence", "repository")


def _verify_roles(
    summary: dict[str, Any],
    descriptor: dict[str, Any],
    expected_app_digest: str | None,
    expected_migrate_digest: str | None,
) -> dict[str, str]:
    push = _role_map(summary["push"], "summary")
    remote = _role_map(summary["remote"], "summary")
    pull = _role_map(summary["pull"], "summary")
    expected = {
        "app": expected_app_digest,
        "migrate": expected_migrate_digest,
    }
    references: dict[str, str] = {}
    for role in ROLES:
        image = descriptor["images"][role]
        root_digest = image["root_descriptor"]["digest"]
        local_id = image["local_id"]
        candidate = _digest(
            push[role].get("candidate_digest"), "summary", "push-digest"
        )
        remote_digest = _digest(
            remote[role].get("digest"), "summary", "remote-digest"
        )
        pull_id = _digest(pull[role].get("id"), "summary", "pull-id")
        if candidate != root_digest:
            raise VerificationError("summary", "push-digest")
        if remote_digest != root_digest:
            raise VerificationError("summary", "remote-digest")
        if pull_id != local_id:
            raise VerificationError("summary", "pull-id")
        if expected[role] is not None and expected[role] != remote_digest:
            raise VerificationError("binding", role + "-digest")
        references[role] = TARGETS[role] + "@" + remote_digest
    return references


def verify_release(
    summary_bytes: bytes,
    descriptor_bytes: bytes,
    evidence_bytes: bytes,
    run_metadata_bytes: bytes,
    *,
    expected_source_sha: str,
    release_run_id: int,
    release_run_attempt: int,
    expected_app_digest: str | None = None,
    expected_migrate_digest: str | None = None,
) -> dict[str, str]:
    """Verify one offline release artifact set and return fixed references.

    Raises :class:`VerificationError` with a fixed ``stage/kind`` category on
    any mismatch.  The returned mapping contains only ``app`` and ``migrate``
    digest-pinned ``ghcr.io`` references from the verified release summary.
    """
    (
        expected_sha,
        expected_run_id,
        expected_attempt,
        expected_app,
        expected_migrate,
    ) = _validate_expected(
        expected_source_sha,
        release_run_id,
        release_run_attempt,
        expected_app_digest,
        expected_migrate_digest,
    )
    summary = _parse_json_object(
        summary_bytes, "summary", SUMMARY_MAX_BYTES
    )
    descriptor = _parse_json_object(
        descriptor_bytes, "descriptor", DESCRIPTOR_MAX_BYTES
    )
    evidence = _parse_json_object(
        evidence_bytes, "evidence", EVIDENCE_MAX_BYTES
    )
    run_metadata = _parse_json_object(
        run_metadata_bytes, "run", RUN_METADATA_MAX_BYTES
    )
    evidence = _validate_evidence(evidence)
    run_metadata = _validate_run_metadata(run_metadata)
    descriptor = _validate_descriptor(descriptor)
    summary = _validate_summary(summary)
    _cross_check(
        summary,
        descriptor,
        evidence,
        run_metadata,
        expected_sha,
        expected_run_id,
        expected_attempt,
        hashlib.sha256(descriptor_bytes).hexdigest(),
    )
    return _verify_roles(
        summary, descriptor, expected_app, expected_migrate
    )


def _read_bounded(path: str, name: str, maximum: int) -> bytes:
    try:
        with open(path, "rb") as handle:
            data = handle.read(maximum + 1)
    except OSError as exc:
        raise VerificationError("input", name + "-file") from exc
    if len(data) > maximum:
        raise VerificationError(name, "size")
    return data


def _run_offline(
    args: argparse.Namespace, run_id: int, attempt: int
) -> dict[str, str]:
    return verify_release(
        _read_bounded(args.summary, "summary", SUMMARY_MAX_BYTES),
        _read_bounded(args.descriptor, "descriptor", DESCRIPTOR_MAX_BYTES),
        _read_bounded(args.evidence, "evidence", EVIDENCE_MAX_BYTES),
        _read_bounded(
            args.run_metadata, "run", RUN_METADATA_MAX_BYTES
        ),
        expected_source_sha=args.expected_source_sha,
        release_run_id=run_id,
        release_run_attempt=attempt,
        expected_app_digest=args.expected_app_digest,
        expected_migrate_digest=args.expected_migrate_digest,
    )


def _online_token() -> str:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(name)
        if token:
            return token
    raise VerificationError("input", "token")


def _load_online_module() -> Any:
    cached = sys.modules.get(ONLINE_SOURCE_MODULE_NAME)
    if cached is not None and callable(
        getattr(cached, "verify_online", None)
    ):
        return cached
    path = os.path.join(
        os.path.dirname(os.path.realpath(__file__)),
        ONLINE_SOURCE_MODULE,
    )
    spec = importlib.util.spec_from_file_location(
        ONLINE_SOURCE_MODULE_NAME, path
    )
    if spec is None or spec.loader is None:
        raise VerificationError("source", "verification")
    module = importlib.util.module_from_spec(spec)
    sys.modules[ONLINE_SOURCE_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(ONLINE_SOURCE_MODULE_NAME, None)
        raise VerificationError("source", "verification") from exc
    if not callable(getattr(module, "verify_online", None)):
        sys.modules.pop(ONLINE_SOURCE_MODULE_NAME, None)
        raise VerificationError("source", "verification")
    return module


def _validate_online_references(
    value: Any,
    expected_app_digest: str | None,
    expected_migrate_digest: str | None,
) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(ROLES):
        raise VerificationError("source", "verification")
    expected = {
        "app": expected_app_digest,
        "migrate": expected_migrate_digest,
    }
    references: dict[str, str] = {}
    digests: dict[str, str] = {}
    for role in ROLES:
        reference = value.get(role)
        prefix = TARGETS[role] + "@"
        if not isinstance(reference, str) or not reference.startswith(prefix):
            raise VerificationError("source", "verification")
        digest = reference[len(prefix) :]
        _digest(digest, "source", "verification")
        if expected[role] is not None and digest != expected[role]:
            raise VerificationError("source", "verification")
        digests[role] = digest
        references[role] = reference
    if digests["app"] == digests["migrate"]:
        raise VerificationError("source", "verification")
    return references


def _run_online(
    args: argparse.Namespace,
    run_id: int,
    attempt: int,
    *,
    online_module: Any | None = None,
) -> dict[str, str]:
    (
        expected_source_sha,
        expected_run_id,
        expected_attempt,
        expected_app_digest,
        expected_migrate_digest,
    ) = _validate_expected(
        args.expected_source_sha,
        run_id,
        attempt,
        args.expected_app_digest,
        args.expected_migrate_digest,
    )
    token = _online_token()
    try:
        module = (
            online_module
            if online_module is not None
            else _load_online_module()
        )
        verify_online = getattr(module, "verify_online", None)
        if not callable(verify_online):
            raise VerificationError("source", "verification")
        value = verify_online(
            token=token,
            expected_source_sha=expected_source_sha,
            release_run_id=expected_run_id,
            release_run_attempt=expected_attempt,
            expected_app_digest=expected_app_digest,
            expected_migrate_digest=expected_migrate_digest,
        )
        return _validate_online_references(
            value, expected_app_digest, expected_migrate_digest
        )
    except Exception as exc:
        # Never echo an external exception string or its stage/kind.  The
        # source lane may use its own exception class; every such failure is
        # collapsed to one fixed category at this boundary.
        raise VerificationError("source", "verification") from exc


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise VerificationError("cli", "usage")


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        description=(
            "Verify one Payment Hub release artifact set. Offline mode reads "
            "four local files; --online fetches GitHub source evidence."
        ),
        allow_abbrev=False,
    )
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--summary")
    parser.add_argument("--descriptor")
    parser.add_argument("--evidence")
    parser.add_argument("--run-metadata")
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--release-run-id", required=True)
    parser.add_argument("--release-run-attempt", required=True)
    parser.add_argument("--expected-app-digest")
    parser.add_argument("--expected-migrate-digest")
    return parser


def _parse_positive_decimal(value: str, name: str) -> int:
    if re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise VerificationError("input", name)
    return int(value)


def _offline_files_present(args: argparse.Namespace) -> bool:
    return any(
        getattr(args, name) is not None
        for name in ("summary", "descriptor", "evidence", "run_metadata")
    )


def _emit_success(
    mode: str, references: Mapping[str, str], notice: str
) -> None:
    print(f"mode={mode}")
    print(f"app={references['app']}")
    print(f"migrate={references['migrate']}")
    print(f"notice={notice}")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
        if args.online:
            if _offline_files_present(args):
                raise VerificationError("cli", "usage")
        elif not all(
            getattr(args, name) is not None
            for name in (
                "summary",
                "descriptor",
                "evidence",
                "run_metadata",
            )
        ):
            raise VerificationError("cli", "usage")
        run_id = _parse_positive_decimal(
            args.release_run_id, "release-run-id"
        )
        attempt = _parse_positive_decimal(
            args.release_run_attempt, "release-run-attempt"
        )
        if args.online:
            references = _run_online(args, run_id, attempt)
            mode = ONLINE_MODE
            notice = (
                "GitHub source verified; not a live GHCR pull check or proof "
                "these digests are currently pullable, and not deployment "
                "authorization"
            )
        else:
            references = _run_offline(args, run_id, attempt)
            mode = OFFLINE_MODE
            notice = (
                "offline file consistency only; not a GitHub source "
                "verification, not proof these digests are currently "
                "pullable from GHCR, and not deployment authorization"
            )
    except VerificationError as exc:
        print(f"release-verification-failed: {exc}", file=sys.stderr)
        return 2 if exc.stage == "cli" else 1
    except Exception:
        print("release-verification-failed: internal/error", file=sys.stderr)
        return 1
    _emit_success(mode, references, notice)
    return 0


if __name__ == "__main__":
    sys.exit(main())
