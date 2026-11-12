#!/usr/bin/env python3
"""T078 controlled release handoff and publish gates.

Fixed stages:

``--stage prepare``
    Verifies the trusted workflow context, runs the existing full Python,
    format, static, unit, database-foundation, cancellation, and vulnerability
    gates in order, invokes the paired-image builder exactly once, verifies
    both local images, saves them once as one ``pair.tar``, and writes a
    private descriptor plus a separately addressable handoff digest.

``--stage verify-handoff``
    Offline verification of the descriptor, archive, snapshots, OCI root
    indexes, and runtime/attestation descriptor sets.  It never loads Docker,
    builds, pulls, or uses the network.

``--stage publish``
    Explicitly authorised only.  Re-verifies the handoff, checks the two fixed
    GHCR packages through read-only GitHub REST metadata, loads and verifies
    the saved pair, pushes the already verified IDs, obtains the formal remote
    digest from a fixed GHCR manifest GET, pulls by that digest, inspects the
    pulled images, and re-runs the existing image acceptance launcher against
    the original artifact.  A temporary private Docker config is always
    removed.  No deployment is performed.

The script reads only a small, fixed subset of the builder and acceptance
reports.  Raw command output, tokens, DSNs, request bodies, and environment
dumps are never written to summaries.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

_CHECK_IMAGES_SPEC = importlib.util.spec_from_file_location(
    "check_images", SCRIPT_DIR / "check-images.py"
)
if _CHECK_IMAGES_SPEC is None or _CHECK_IMAGES_SPEC.loader is None:
    raise SystemExit("check-images.py is unavailable")
check_images = importlib.util.module_from_spec(_CHECK_IMAGES_SPEC)
sys.modules.setdefault("check_images", check_images)
_CHECK_IMAGES_SPEC.loader.exec_module(check_images)

ProcResult = check_images.ProcResult
run_process = check_images.run_process
LauncherError = check_images.LauncherError
sha256_file = check_images.sha256_file

PLATFORM = "linux/amd64"
SOURCE_LABEL = "payment-hub.source-manifest-digest"
BUILD_RECORD_SCHEMA = "payment-hub.image-build-record.v1"
IMAGES_SCHEMA = "payment-hub.images.v1"
CHECK_SCHEMA = "payment-hub.check-entrypoint.v1"
DESCRIPTOR_SCHEMA = "payment-hub.release-handoff.v1"
HANDOFF_POINTER_SCHEMA = "payment-hub.release-handoff-pointer.v1"
SUMMARY_SCHEMA = "payment-hub.release-summary.v1"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
ATTESTATION_TYPE = "vnd.docker.reference.type"
ATTESTATION_DIGEST = "vnd.docker.reference.digest"

REPOSITORY = "xtianxx/payment-hub"
GITHUB_API = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
GHCR = "ghcr.io"
GHCR_BASE = "https://ghcr.io"
TOKEN_REALM = "https://ghcr.io/token"
TOKEN_SERVICE = "ghcr.io"
TARGETS = {
    "app": "ghcr.io/xtianxx/payment-hub-app",
    "migrate": "ghcr.io/xtianxx/payment-hub-migrate",
}
PACKAGE_NAMES = {
    "app": "payment-hub-app",
    "migrate": "payment-hub-migrate",
}
ROLES = ("app", "migrate")
TAG_PREFIX = "sha-"
DEFAULT_BUNDLE = "bundle"
HANDOFF_FILE = "handoff.json"
SUMMARY_FILE = "release-summary.json"
PAIR_FILE = "pair.tar"
DESCRIPTOR_FILE = "descriptor.json"
BUILD_RECORD_FILE = "build-record.json"
ACCEPTED_BUNDLE_FILE = "accepted-images.json"
POSTGRES_IMAGE = (
    "docker.io/library/postgres:16.15-bookworm"
    "@sha256:5983db9a9c6ad225a48a46cfe2e03e6680b1826662befd0465eb05b34cb1c64e"
)

BUILD_RECORD_MAX_BYTES = 64 * 1024
ACCEPTED_REPORT_MAX_BYTES = 8 * 1024 * 1024
DESCRIPTOR_MAX_BYTES = 64 * 1024
TAR_MAX_BYTES = 2 * 1024 * 1024 * 1024
TAR_JSON_MAX_BYTES = 1024 * 1024
ROOT_JSON_MAX_BYTES = 4 * 1024 * 1024
HTTP_MAX_BYTES = 8 * 1024 * 1024
REGISTRY_MANIFEST_MAX_BYTES = 4 * 1024 * 1024
TOKEN_MAX_BYTES = 64 * 1024
MAX_TAR_MEMBERS = 20000
MAX_ATTESTATIONS = 8

# Fixed tool-class budgets.  The builder and check-images launcher own their
# internal build/test/PG budgets; these outer values only bound the process
# wrapper and are never extended or retried.
GIT_TIMEOUT_SECONDS = 30
LOCAL_GATE_TIMEOUT_SECONDS = 600
BUILDER_TIMEOUT_SECONDS = 1800
DOCKER_TIMEOUT_SECONDS = 120
SETUP_TIMEOUT_SECONDS = 600
PRECOMPILE_TIMEOUT_SECONDS = 600
HTTP_TIMEOUT_SECONDS = 30
DIRECT_REAP_GRACE_SECONDS = 2.0
DELEGATED_TERM_GRACE_SECONDS = 5.0
MAX_TRACKED_PROCESSES = 4096


class ReleaseError(RuntimeError):
    """A fixed, non-dynamic failure classification."""

    def __init__(self, stage: str, kind: str) -> None:
        self.stage = stage
        self.kind = kind
        super().__init__(f"{stage}/{kind}")


class HttpResult(NamedTuple):
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class TrustedContext:
    source_sha: str
    repo: Path


@dataclass(frozen=True)
class RootContent:
    digest: str
    media_type: str
    size: int
    runtime_digest: str
    runtime_size: int
    runtime_media_type: str
    attestations: tuple[str, ...]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_name(value: Any, stage: str, kind: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ReleaseError(stage, kind)
    if "/" in value or "\\" in value or value in (".", ".."):
        raise ReleaseError(stage, kind)
    if any(ord(char) <= 32 or ord(char) == 0x7F for char in value):
        raise ReleaseError(stage, kind)
    return value


def _safe_reference(value: Any, stage: str, kind: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ReleaseError(stage, kind)
    if value.startswith("-") or any(
        ord(char) <= 32 or ord(char) == 0x7F for char in value
    ):
        raise ReleaseError(stage, kind)
    return value


def _sha256_hex(value: Any, stage: str, kind: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ReleaseError(stage, kind)
    return value


def _source_sha(value: Any, stage: str, kind: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ReleaseError(stage, kind)
    return value


def _image_id(value: Any, stage: str, kind: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ReleaseError(stage, kind)
    return value


def _positive_size(value: Any, stage: str, kind: str, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
        or value > maximum
    ):
        raise ReleaseError(stage, kind)
    return value


def _read_bounded_bytes(path: Path, stage: str, kind: str, maximum: int) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReleaseError(stage, kind) from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ReleaseError(stage, kind)
    if info.st_size <= 0 or info.st_size > maximum:
        raise ReleaseError(stage, kind)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ReleaseError(stage, kind) from exc


def _read_json(path: Path, stage: str, kind: str, maximum: int) -> dict[str, Any]:
    data = _read_bounded_bytes(path, stage, kind, maximum)
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(stage, kind) from exc
    if not isinstance(value, dict):
        raise ReleaseError(stage, kind)
    return value


def _write_bytes_exclusive(path: Path, data: bytes, mode: int = 0o600) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError as exc:
        raise ReleaseError("artifact", "exists") from exc
    except OSError as exc:
        raise ReleaseError("artifact", "create-failed") from exc
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except OSError as exc:
        raise ReleaseError("artifact", "write-failed") from exc


def _write_json_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    _write_bytes_exclusive(path, text.encode("utf-8"))


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    """Write one summary atomically; a partial temp is never the summary."""
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    descriptor = None
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=".release-summary-", dir=str(path.parent)
        )
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(text.encode("utf-8"))
        if path.exists():
            raise ReleaseError("artifact", "summary-exists")
        os.replace(temporary, path)
        temporary = None
    except ReleaseError:
        raise
    except OSError as exc:
        raise ReleaseError("artifact", "summary-write") from exc
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


def _mkdir_private(path: Path) -> None:
    try:
        path.mkdir(parents=False, exist_ok=False)
        path.chmod(0o700)
    except FileExistsError as exc:
        raise ReleaseError("artifact", "exists") from exc
    except OSError as exc:
        raise ReleaseError("artifact", "create-failed") from exc


def _fresh_root(path: Path) -> None:
    """Accept a missing or existing empty root; never overwrite entries."""
    try:
        if path.is_symlink():
            raise ReleaseError("artifact", "symlink")
        if path.exists():
            if not path.is_dir():
                raise ReleaseError("artifact", "not-directory")
            try:
                if any(path.iterdir()):
                    raise ReleaseError("artifact", "not-fresh")
            except OSError as exc:
                raise ReleaseError("artifact", "unreadable") from exc
            path.chmod(0o700)
            return
        path.mkdir(parents=True, exist_ok=False)
        path.chmod(0o700)
    except ReleaseError:
        raise
    except OSError as exc:
        raise ReleaseError("artifact", "create-failed") from exc


def _stream_sha256(path: Path, stage: str, kind: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                if size > TAR_MAX_BYTES:
                    raise ReleaseError(stage, kind)
                digest.update(chunk)
    except ReleaseError:
        raise
    except OSError as exc:
        raise ReleaseError(stage, kind) from exc
    return digest.hexdigest(), size


def _parse_root_index(value: Any, stage: str, kind: str) -> RootContent:
    if not isinstance(value, dict):
        raise ReleaseError(stage, kind)
    if value.get("schemaVersion") != 2 or value.get("mediaType") != OCI_INDEX:
        raise ReleaseError(stage, kind)
    manifests = value.get("manifests")
    if not isinstance(manifests, list) or not manifests:
        raise ReleaseError(stage, kind)
    runtime: dict[str, Any] | None = None
    attestations: list[str] = []
    for item in manifests:
        if not isinstance(item, dict):
            raise ReleaseError(stage, kind)
        media_type = item.get("mediaType")
        digest = _image_id(item.get("digest"), stage, kind)
        size = _positive_size(item.get("size"), stage, kind, ROOT_JSON_MAX_BYTES)
        platform = item.get("platform")
        if not isinstance(platform, dict):
            raise ReleaseError(stage, kind)
        if media_type == OCI_MANIFEST and platform == {
            "architecture": "amd64",
            "os": "linux",
        }:
            if runtime is not None:
                raise ReleaseError(stage, kind)
            runtime = {
                "mediaType": media_type,
                "digest": digest,
                "size": size,
            }
            continue
        annotations = item.get("annotations")
        if (
            media_type == OCI_MANIFEST
            and platform == {"architecture": "unknown", "os": "unknown"}
            and isinstance(annotations, dict)
            and set(annotations) == {ATTESTATION_TYPE, ATTESTATION_DIGEST}
            and annotations.get(ATTESTATION_TYPE) == "attestation-manifest"
            and isinstance(annotations.get(ATTESTATION_DIGEST), str)
        ):
            attestations.append(digest)
            if len(attestations) > MAX_ATTESTATIONS:
                raise ReleaseError(stage, kind)
            continue
        raise ReleaseError(stage, kind)
    if runtime is None:
        raise ReleaseError(stage, kind)
    for digest in attestations:
        if digest == runtime["digest"]:
            raise ReleaseError(stage, kind)
    return RootContent(
        digest="",
        media_type=OCI_INDEX,
        size=0,
        runtime_digest=runtime["digest"],
        runtime_size=runtime["size"],
        runtime_media_type=runtime["mediaType"],
        attestations=tuple(sorted(attestations)),
    )


def _read_tar_root(
    archive: tarfile.TarFile,
    member_by_name: Mapping[str, tarfile.TarInfo],
    descriptor: Mapping[str, Any],
    stage: str,
    kind: str,
) -> RootContent:
    digest = _image_id(descriptor.get("digest"), stage, kind)
    size = _positive_size(descriptor.get("size"), stage, kind, ROOT_JSON_MAX_BYTES)
    if descriptor.get("mediaType") != OCI_INDEX:
        raise ReleaseError(stage, kind)
    name = "blobs/sha256/" + digest.split(":", 1)[1]
    member = member_by_name.get(name)
    if member is None or member.size != size:
        raise ReleaseError(stage, kind)
    extracted = archive.extractfile(member)
    if extracted is None:
        raise ReleaseError(stage, kind)
    data = extracted.read(size + 1)
    if len(data) != size or _sha256_bytes(data) != digest.split(":", 1)[1]:
        raise ReleaseError(stage, kind)
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(stage, kind) from exc
    content = _parse_root_index(value, stage, kind)
    # Verify that every child descriptor referenced by the root index is
    # present in the archive with the declared size.  Child blob bytes are not
    # read again: the remote manifest digest binds the root index bytes, and
    # the digest pull/inspect step binds the runtime content.
    for child_digest, child_size in (
        (content.runtime_digest, content.runtime_size),
        *((item, None) for item in content.attestations),
    ):
        child_name = "blobs/sha256/" + child_digest.split(":", 1)[1]
        child = member_by_name.get(child_name)
        if child is None:
            raise ReleaseError(stage, kind)
        if child_size is not None and child.size != child_size:
            raise ReleaseError(stage, kind)
    return RootContent(
        digest=digest,
        media_type=OCI_INDEX,
        size=size,
        runtime_digest=content.runtime_digest,
        runtime_size=content.runtime_size,
        runtime_media_type=content.runtime_media_type,
        attestations=content.attestations,
    )


def _parse_pair_tar(path: Path, expected_digests: Sequence[str]) -> dict[str, RootContent]:
    try:
        archive = tarfile.open(path, mode="r:")
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseError("tar", "open") from exc
    roots: dict[str, RootContent] = {}
    with archive:
        members: dict[str, tarfile.TarInfo] = {}
        for member in archive.getmembers():
            name = member.name
            if name in ("", ".", "..") or name.startswith("/"):
                raise ReleaseError("tar", "member")
            if "\\" in name or any(part in ("", ".", "..") for part in name.split("/")):
                raise ReleaseError("tar", "member")
            if member.issym() or member.islnk() or member.isdev():
                raise ReleaseError("tar", "member")
            if member.isdir():
                if name not in ("blobs", "blobs/sha256"):
                    raise ReleaseError("tar", "member")
                continue
            if not member.isfile():
                raise ReleaseError("tar", "member")
            if member.mode & 0o111:
                raise ReleaseError("tar", "executable")
            if not (
                name in ("index.json", "manifest.json", "oci-layout")
                or re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", name)
            ):
                raise ReleaseError("tar", "member")
            if name in members:
                raise ReleaseError("tar", "duplicate")
            members[name] = member
            if len(members) > MAX_TAR_MEMBERS:
                raise ReleaseError("tar", "too-many")
        index_member = members.get("index.json")
        if index_member is None or index_member.size > TAR_JSON_MAX_BYTES:
            raise ReleaseError("tar", "index")
        index_file = archive.extractfile(index_member)
        if index_file is None:
            raise ReleaseError("tar", "index")
        try:
            index = json.loads(index_file.read(index_member.size + 1))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("tar", "index") from exc
        if (
            not isinstance(index, dict)
            or index.get("schemaVersion") != 2
            or index.get("mediaType") != OCI_INDEX
            or not isinstance(index.get("manifests"), list)
        ):
            raise ReleaseError("tar", "index")
        manifests = index["manifests"]
        if len(manifests) != len(expected_digests):
            raise ReleaseError("tar", "roots")
        for descriptor in manifests:
            if not isinstance(descriptor, dict):
                raise ReleaseError("tar", "roots")
            digest = _image_id(descriptor.get("digest"), "tar", "roots")
            if digest in roots or digest not in expected_digests:
                raise ReleaseError("tar", "roots")
            roots[digest] = _read_tar_root(
                archive, members, descriptor, "tar", "roots"
            )
        if set(roots) != set(expected_digests):
            raise ReleaseError("tar", "roots")
        layout = members.get("oci-layout")
        if layout is None or layout.size > TAR_JSON_MAX_BYTES:
            raise ReleaseError("tar", "layout")
        layout_file = archive.extractfile(layout)
        if layout_file is None:
            raise ReleaseError("tar", "layout")
        try:
            layout_value = json.loads(layout_file.read(layout.size + 1))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("tar", "layout") from exc
        if not isinstance(layout_value, dict) or layout_value.get(
            "imageLayoutVersion"
        ) != "1.0.0":
            raise ReleaseError("tar", "layout")
        manifest_member = members.get("manifest.json")
        if manifest_member is None or manifest_member.size > TAR_JSON_MAX_BYTES:
            raise ReleaseError("tar", "manifest")
        manifest_file = archive.extractfile(manifest_member)
        if manifest_file is None:
            raise ReleaseError("tar", "manifest")
        try:
            manifest_value = json.loads(manifest_file.read(manifest_member.size + 1))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("tar", "manifest") from exc
        if not isinstance(manifest_value, list):
            raise ReleaseError("tar", "manifest")
    return roots


def _validate_descriptor(value: dict[str, Any]) -> dict[str, Any]:
    required = {
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
    if set(value) != required:
        raise ReleaseError("descriptor", "keys")
    if value.get("schema") != DESCRIPTOR_SCHEMA:
        raise ReleaseError("descriptor", "schema")
    source_sha = _source_sha(value.get("source_sha"), "descriptor", "source-sha")
    manifest_digest = _sha256_hex(
        value.get("source_manifest_digest"), "descriptor", "manifest-digest"
    )
    if value.get("platform") != PLATFORM:
        raise ReleaseError("descriptor", "platform")
    created = value.get("created_utc")
    if not isinstance(created, str) or len(created) > 64:
        raise ReleaseError("descriptor", "utc")
    archive = value.get("archive")
    if not isinstance(archive, dict) or set(archive) != {"file", "sha256", "size"}:
        raise ReleaseError("descriptor", "archive")
    if _safe_name(archive.get("file"), "descriptor", "archive") != PAIR_FILE:
        raise ReleaseError("descriptor", "archive")
    _sha256_hex(archive.get("sha256"), "descriptor", "archive-sha")
    _positive_size(archive.get("size"), "descriptor", "archive-size", TAR_MAX_BYTES)
    builder = value.get("builder")
    if not isinstance(builder, dict) or set(builder) != {
        "build_record_sha256",
        "accepted_report_sha256",
        "accepted_report_file",
    }:
        raise ReleaseError("descriptor", "builder")
    _sha256_hex(builder.get("build_record_sha256"), "descriptor", "builder-sha")
    _sha256_hex(builder.get("accepted_report_sha256"), "descriptor", "builder-sha")
    if (
        _safe_name(
            builder.get("accepted_report_file"), "descriptor", "builder-file"
        )
        != ACCEPTED_BUNDLE_FILE
    ):
        raise ReleaseError("descriptor", "builder-file")
    images = value.get("images")
    if not isinstance(images, dict) or set(images) != set(ROLES):
        raise ReleaseError("descriptor", "images")
    for role in ROLES:
        image = images.get(role)
        if not isinstance(image, dict) or set(image) != {
            "reference",
            "local_id",
            "root_descriptor",
            "source_label",
            "platform",
        }:
            raise ReleaseError("descriptor", "image")
        _safe_reference(image.get("reference"), "descriptor", "reference")
        local_id = _image_id(image.get("local_id"), "descriptor", "local-id")
        root = image.get("root_descriptor")
        if not isinstance(root, dict) or set(root) != {"mediaType", "digest", "size"}:
            raise ReleaseError("descriptor", "root")
        if root.get("mediaType") != OCI_INDEX:
            raise ReleaseError("descriptor", "root")
        root_digest = _image_id(root.get("digest"), "descriptor", "root-digest")
        if root_digest != local_id:
            raise ReleaseError("descriptor", "local-id")
        _positive_size(root.get("size"), "descriptor", "root-size", ROOT_JSON_MAX_BYTES)
        if _sha256_hex(
            image.get("source_label"), "descriptor", "source-label"
        ) != manifest_digest:
            raise ReleaseError("descriptor", "source-label")
        if image.get("platform") != PLATFORM:
            raise ReleaseError("descriptor", "platform")
    if images["app"]["local_id"] == images["migrate"]["local_id"]:
        raise ReleaseError("descriptor", "pair")
    targets = value.get("targets")
    if not isinstance(targets, dict) or targets != TARGETS:
        raise ReleaseError("descriptor", "targets")
    if value.get("tag") != TAG_PREFIX + source_sha:
        raise ReleaseError("descriptor", "tag")
    return value


def _verify_bundle_files(bundle: Path, descriptor: dict[str, Any]) -> None:
    try:
        info = bundle.lstat()
    except OSError as exc:
        raise ReleaseError("bundle", "missing") from exc
    if bundle.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise ReleaseError("bundle", "not-directory")
    allowed = {DESCRIPTOR_FILE, PAIR_FILE, BUILD_RECORD_FILE, ACCEPTED_BUNDLE_FILE}
    try:
        entries = list(bundle.iterdir())
    except OSError as exc:
        raise ReleaseError("bundle", "read-failed") from exc
    if {entry.name for entry in entries} != allowed:
        raise ReleaseError("bundle", "files")
    for entry in entries:
        try:
            entry_info = entry.lstat()
        except OSError as exc:
            raise ReleaseError("bundle", "file") from exc
        if entry.is_symlink() or not stat.S_ISREG(entry_info.st_mode):
            raise ReleaseError("bundle", "file")
        if entry_info.st_mode & 0o111:
            raise ReleaseError("bundle", "executable")
        if entry.name == PAIR_FILE:
            if entry_info.st_size <= 0 or entry_info.st_size > TAR_MAX_BYTES:
                raise ReleaseError("bundle", "archive-size")
        else:
            maximum = (
                DESCRIPTOR_MAX_BYTES
                if entry.name == DESCRIPTOR_FILE
                else BUILD_RECORD_MAX_BYTES
                if entry.name == BUILD_RECORD_FILE
                else ACCEPTED_REPORT_MAX_BYTES
            )
            if entry_info.st_size <= 0 or entry_info.st_size > maximum:
                raise ReleaseError("bundle", "file-size")


def _verify_handoff(
    bundle: Path,
    expected_descriptor_sha256: str,
    *,
    expected_source_sha: str,
) -> tuple[dict[str, Any], dict[str, RootContent]]:
    _sha256_hex(expected_descriptor_sha256, "handoff", "expected-sha")
    descriptor_bytes = _read_bounded_bytes(
        bundle / DESCRIPTOR_FILE, "handoff", "descriptor", DESCRIPTOR_MAX_BYTES
    )
    actual_descriptor_sha = _sha256_bytes(descriptor_bytes)
    if actual_descriptor_sha != expected_descriptor_sha256:
        raise ReleaseError("handoff", "descriptor-sha")
    try:
        descriptor_value = json.loads(descriptor_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("handoff", "descriptor-json") from exc
    if not isinstance(descriptor_value, dict):
        raise ReleaseError("handoff", "descriptor-json")
    descriptor = _validate_descriptor(descriptor_value)
    _source_sha(expected_source_sha, "handoff", "source-sha")
    if descriptor["source_sha"] != expected_source_sha:
        raise ReleaseError("handoff", "source-sha")
    _verify_bundle_files(bundle, descriptor)

    archive = descriptor["archive"]
    archive_path = bundle / PAIR_FILE
    archive_sha, archive_size = _stream_sha256(archive_path, "archive", "hash")
    if archive_sha != archive["sha256"] or archive_size != archive["size"]:
        raise ReleaseError("archive", "hash")
    build_record_bytes = _read_bounded_bytes(
        bundle / BUILD_RECORD_FILE,
        "bundle",
        "build-record",
        BUILD_RECORD_MAX_BYTES,
    )
    accepted_bytes = _read_bounded_bytes(
        bundle / ACCEPTED_BUNDLE_FILE,
        "bundle",
        "accepted-report",
        ACCEPTED_REPORT_MAX_BYTES,
    )
    if _sha256_bytes(build_record_bytes) != descriptor["builder"][
        "build_record_sha256"
    ]:
        raise ReleaseError("bundle", "build-record-hash")
    if _sha256_bytes(accepted_bytes) != descriptor["builder"][
        "accepted_report_sha256"
    ]:
        raise ReleaseError("bundle", "accepted-report-hash")
    expected_digests = [
        descriptor["images"][role]["root_descriptor"]["digest"] for role in ROLES
    ]
    roots = _parse_pair_tar(archive_path, expected_digests)
    for role in ROLES:
        image = descriptor["images"][role]
        root = image["root_descriptor"]
        parsed = roots[image["local_id"]]
        if (
            parsed.media_type != root["mediaType"]
            or parsed.digest != root["digest"]
            or parsed.size != root["size"]
        ):
            raise ReleaseError("tar", "descriptor")
    return descriptor, roots


def _cache_env(cache: Path) -> dict[str, str]:
    env = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "LANG", "LC_ALL")
        if name in os.environ
    }
    for name in ("config", "gomod", "gobuild", "gopath", "tmp"):
        (cache / name).mkdir(parents=True, exist_ok=True)
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


def _read_proc_identity(pid: int) -> tuple[str, int, int, int, int] | None:
    """Return (state, ppid, pgid, sid, starttime) for one live PID."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    _, separator, rest = text.rpartition(")")
    if not separator:
        return None
    fields = rest.split()
    if len(fields) < 20:
        return None
    try:
        return (
            fields[0],
            int(fields[1]),
            int(fields[2]),
            int(fields[3]),
            int(fields[19]),
        )
    except (IndexError, ValueError):
        return None


def _proc_snapshot() -> dict[int, tuple[str, int, int, int, int]] | None:
    """Bounded snapshot of local PIDs; None means the boundary is unprovable."""
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return None
    snapshot: dict[int, tuple[str, int, int, int, int]] = {}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        identity = _read_proc_identity(pid)
        if identity is None:
            continue
        snapshot[pid] = identity
        if len(snapshot) > MAX_TRACKED_PROCESSES:
            return None
    return snapshot


def _leader_identity_matches(
    owned_pgid: int | None,
    leader_identity: tuple[str, int, int, int, int] | None,
) -> bool:
    if owned_pgid is None or leader_identity is None:
        return False
    current = _read_proc_identity(owned_pgid)
    return current is not None and current[4] == leader_identity[4]


def _verified_owned_roots(
    leader_pid: int,
    leader_identity: tuple[str, int, int, int, int] | None,
    tracked: Mapping[int, tuple[str, int, int, int, int]],
    snapshot: Mapping[int, tuple[str, int, int, int, int]],
) -> list[int]:
    """Only roots whose current start time still matches a recorded identity."""
    roots: list[int] = []
    root_identity = snapshot.get(leader_pid)
    if (
        leader_identity is not None
        and root_identity is not None
        and root_identity[4] == leader_identity[4]
    ):
        roots.append(leader_pid)
    for pid, identity in tracked.items():
        current = snapshot.get(pid)
        if current is not None and current[4] == identity[4]:
            roots.append(pid)
    return roots


def _owned_descendants(
    leader_pid: int,
    owned_pgid: int | None,
    leader_identity: tuple[str, int, int, int, int] | None,
    tracked: Mapping[int, tuple[str, int, int, int, int]],
    snapshot: Mapping[int, tuple[str, int, int, int, int]],
) -> dict[int, tuple[str, int, int, int, int]]:
    """PIDs that are provably this invocation's descendants or session members.

    The delegated child starts a new session, so its SID is the confirmed
    PGID.  Descendants that create their own session are still followed while
    their parent chain is visible.  Traversal starts only from the recorded
    leader or a tracked PID whose current start time still matches; a reused
    PID is never used as a root.  Session members are only selected while the
    recorded leader identity still owns that SID; no unrelated group is ever
    selected.
    """
    roots = _verified_owned_roots(
        leader_pid, leader_identity, tracked, snapshot
    )
    if not roots:
        # The recorded leader PID is gone or has been reused and no tracked
        # identity remains.  Its current children are unrelated; never root
        # the traversal at that PID.
        return {}
    children: dict[int, list[int]] = {}
    for pid, identity in snapshot.items():
        children.setdefault(identity[1], []).append(pid)
    owned: dict[int, tuple[str, int, int, int, int]] = {}
    pending = list(roots)
    seen = set(roots)
    while pending:
        parent = pending.pop()
        for child in children.get(parent, ()):
            if child in seen:
                continue
            seen.add(child)
            identity = snapshot.get(child)
            if identity is None:
                continue
            owned[child] = identity
            pending.append(child)
    if _leader_identity_matches(owned_pgid, leader_identity):
        for pid, identity in snapshot.items():
            if identity[3] == owned_pgid:
                owned.setdefault(pid, identity)
    owned.pop(leader_pid, None)
    return owned


def _merge_tracked_descendants(
    tracked: dict[int, tuple[str, int, int, int, int]],
    leader_pid: int,
    owned_pgid: int | None,
    leader_identity: tuple[str, int, int, int, int] | None,
    *,
    require_leader_root: bool = False,
) -> bool:
    snapshot = _proc_snapshot()
    if snapshot is None:
        return False
    roots = _verified_owned_roots(
        leader_pid, leader_identity, tracked, snapshot
    )
    if not roots:
        # No live tracked identity remains.  At timeout entry the leader root
        # must be verifiable; otherwise ownership is unconfirmed.  Later
        # checks decide from the owned group/session and tracked PIDs.
        return not require_leader_root
    for pid, identity in _owned_descendants(
        leader_pid, owned_pgid, leader_identity, tracked, snapshot
    ).items():
        tracked.setdefault(pid, identity)
    return True


def _group_members_are_tracked(
    owned_pgid: int | None,
    tracked: Mapping[int, tuple[str, int, int, int, int]],
) -> bool:
    """True only when every live group member is a verified tracked PID."""
    if owned_pgid is None:
        return False
    snapshot = _proc_snapshot()
    if snapshot is None:
        return False
    found = False
    for pid, identity in snapshot.items():
        if identity[2] != owned_pgid or identity[0] == "Z":
            continue
        found = True
        tracked_identity = tracked.get(pid)
        if tracked_identity is None or tracked_identity[4] != identity[4]:
            return False
    return found


def _kill_tracked_descendants(
    tracked: Mapping[int, tuple[str, int, int, int, int]],
    owned_pgid: int | None,
    leader_identity: tuple[str, int, int, int, int] | None,
) -> bool:
    cleanup_failed = False
    if _leader_identity_matches(
        owned_pgid, leader_identity
    ) or _group_members_are_tracked(owned_pgid, tracked):
        if not check_images._signal_group_id(
            owned_pgid, signal.SIGKILL
        ) and not check_images._group_absent(owned_pgid):
            cleanup_failed = True
    for pid, identity in tracked.items():
        current = _read_proc_identity(pid)
        if current is None or current[4] != identity[4]:
            # The PID is gone or has been reused by an unrelated process.
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except OSError:
            cleanup_failed = True
    return cleanup_failed


def _tracked_descendant_present(
    tracked: Mapping[int, tuple[str, int, int, int, int]],
) -> bool:
    for pid, identity in tracked.items():
        current = _read_proc_identity(pid)
        if (
            current is not None
            and current[4] == identity[4]
            and current[0] != "Z"
        ):
            return True
    return False


def _owned_session_absent(owned_pgid: int | None) -> bool:
    if owned_pgid is None:
        return False
    snapshot = _proc_snapshot()
    if snapshot is None:
        return False
    for identity in snapshot.values():
        if identity[3] == owned_pgid and identity[0] != "Z":
            return False
    return True


def _harvest_delegated_cleanup(
    process: subprocess.Popen[bytes],
    owned_pgid: int | None,
    leader_identity: tuple[str, int, int, int, int] | None,
    tracked: dict[int, tuple[str, int, int, int, int]],
    *,
    drain: bool,
) -> bool:
    """Boundedly kill and reap the confirmed owned session and descendants."""
    cleanup_failed = False
    if not _merge_tracked_descendants(
        tracked, process.pid, owned_pgid, leader_identity
    ):
        cleanup_failed = True
    if _kill_tracked_descendants(tracked, owned_pgid, leader_identity):
        cleanup_failed = True
    if not _leader_identity_matches(owned_pgid, leader_identity):
        # The confirmed group boundary is unavailable, so only the direct
        # Popen child is killed here.  Descendants remain tracked individually.
        if process.poll() is None:
            try:
                process.kill()
            except (OSError, ValueError):
                cleanup_failed = True
    if drain:
        try:
            process.communicate(timeout=DIRECT_REAP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            cleanup_failed = True
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            try:
                process.wait(timeout=check_images.MIN_GROUP_DRAIN_SECONDS)
            except (subprocess.TimeoutExpired, OSError):
                cleanup_failed = True
        except (OSError, ValueError):
            cleanup_failed = True
            try:
                process.wait(timeout=check_images.MIN_GROUP_DRAIN_SECONDS)
            except (subprocess.TimeoutExpired, OSError):
                cleanup_failed = True
    else:
        try:
            process.wait(timeout=check_images.MIN_GROUP_DRAIN_SECONDS)
        except (subprocess.TimeoutExpired, OSError):
            cleanup_failed = True
    if (
        _tracked_descendant_present(tracked)
        or not _owned_session_absent(owned_pgid)
        or (
            owned_pgid is not None
            and not check_images._group_absent(owned_pgid)
        )
    ):
        if not _merge_tracked_descendants(
            tracked, process.pid, owned_pgid, leader_identity
        ):
            cleanup_failed = True
        if _kill_tracked_descendants(
            tracked, owned_pgid, leader_identity
        ):
            cleanup_failed = True
        try:
            process.wait(timeout=check_images.MIN_GROUP_DRAIN_SECONDS)
        except (subprocess.TimeoutExpired, OSError):
            pass
    if owned_pgid is not None and not check_images._group_absent(owned_pgid):
        cleanup_failed = True
    if not _owned_session_absent(owned_pgid):
        cleanup_failed = True
    if _tracked_descendant_present(tracked):
        cleanup_failed = True
    return cleanup_failed


class Release:
    def __init__(
        self,
        *,
        repo: Path = REPO_ROOT,
        runner: Callable[..., ProcResult] | None = None,
        delegated_runner: Callable[..., ProcResult] | None = None,
        http: Callable[..., HttpResult] | None = None,
        env: Mapping[str, str] | None = None,
        git_path: str | None = None,
        docker_path: str | None = None,
    ) -> None:
        self.repo = Path(repo)
        self.runner = runner or run_process
        self.delegated_runner = delegated_runner
        self.http = http or _default_http
        self.env = dict(os.environ) if env is None else dict(env)
        self.git_path = git_path
        self.docker_path = docker_path
        self._bundle: str | None = None
        self._cancel_signal: int | None = None
        self._direct_tool_active = False
        self._delegated_process: subprocess.Popen[bytes] | None = None
        self._delegated_starting = False
        self._cancel_cleanup_failed = False
        self._in_cleanup = False

    # ------------------------------------------------------------------
    # Cancellation and process helpers
    # ------------------------------------------------------------------

    def _handle_cancel(self, signum: int, frame: Any) -> None:
        first = self._cancel_signal is None
        self._cancel_signal = signum
        check_images._CANCEL_REQUESTED = True
        if not first or self._in_cleanup:
            # Repeated signals and signals during cleanup must not skip
            # credential finally blocks.
            return
        if self._delegated_starting:
            # The child PID is not assigned yet; the delegated runner will
            # re-check the latch and forward this signal after Popen.
            return
        process = self._delegated_process
        if process is not None:
            try:
                process.send_signal(signum)
            except (OSError, ValueError):
                pass
            return
        if self._direct_tool_active:
            check_images._signal_active(signum)
            return
        raise ReleaseError("cancelled", "signal")

    @contextlib.contextmanager
    def _cancel_handlers(self):
        old_int = None
        old_term = None
        installed_int = False
        installed_term = False
        try:
            old_int = signal.getsignal(signal.SIGINT)
            old_term = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGINT, self._handle_cancel)
            installed_int = True
            signal.signal(signal.SIGTERM, self._handle_cancel)
            installed_term = True
        except (AttributeError, OSError, ValueError) as exc:
            if installed_int and old_int is not None:
                try:
                    signal.signal(signal.SIGINT, old_int)
                except (AttributeError, OSError, ValueError):
                    pass
            raise ReleaseError("cancelled", "handler-unsupported") from exc
        try:
            yield
        finally:
            for signum, old in ((signal.SIGINT, old_int), (signal.SIGTERM, old_term)):
                if old is not None:
                    try:
                        signal.signal(signum, old)
                    except (AttributeError, OSError, ValueError):
                        pass

    def _cleanup_direct_active(self) -> bool:
        process = check_images._ACTIVE_PROCESS
        pgid = check_images._ACTIVE_PGID
        if process is None:
            return False
        try:
            return check_images._terminate_owned_group(
                process, pgid, DIRECT_REAP_GRACE_SECONDS
            )
        except BaseException:
            return True

    def _run_direct(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None,
        input_text: str | None = None,
    ) -> ProcResult:
        if self._cancel_signal is not None:
            raise ReleaseError("cancelled", "signal")
        check_images._reset_process_cancel()
        self._direct_tool_active = True
        try:
            try:
                result = self.runner(
                    list(argv),
                    cwd=Path(cwd or self.repo),
                    env=dict(env or self.env),
                    timeout=timeout,
                    input_text=input_text,
                    reap_grace=DIRECT_REAP_GRACE_SECONDS,
                )
            except (LauncherError, OSError) as exc:
                raise ReleaseError("tool", "start-failed") from exc
            except BaseException as exc:
                cleanup_failed = self._cleanup_direct_active()
                self._cancel_cleanup_failed = (
                    self._cancel_cleanup_failed or cleanup_failed
                )
                raise ReleaseError(
                    "cancelled",
                    "cleanup-failed" if cleanup_failed else "signal",
                ) from exc
        finally:
            self._direct_tool_active = False
        # run_process also latches _CANCEL_REQUESTED for its own tool timeout.
        # Only a real SIGINT/SIGTERM may be reported as cancellation; a normal
        # timeout is returned with timed_out=True so each call site keeps its
        # fixed classification.  A cleanup failure is never hidden by timeout.
        if self._cancel_signal is not None:
            self._cancel_cleanup_failed = (
                self._cancel_cleanup_failed or result.cleanup_failed
            )
            raise ReleaseError(
                "cancelled",
                "cleanup-failed"
                if self._cancel_cleanup_failed
                else "signal",
            )
        if result.cleanup_failed:
            self._cancel_cleanup_failed = True
            raise ReleaseError("tool", "cleanup-failed")
        return result

    def _run_delegated(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None,
        input_text: str | None = None,
    ) -> ProcResult:
        if self._cancel_signal is not None:
            raise ReleaseError("cancelled", "signal")
        cwd_value = Path(cwd or self.repo)
        env_value = dict(env or self.env)
        if self.delegated_runner is not None:
            result = self.delegated_runner(
                list(argv),
                cwd=cwd_value,
                env=env_value,
                timeout=timeout,
                input_text=input_text,
            )
            if self._cancel_signal is not None:
                raise ReleaseError("cancelled", "signal")
            return result
        started = time.monotonic()
        process: subprocess.Popen[bytes] | None = None
        owned_pgid: int | None = None
        leader_identity: tuple[str, int, int, int, int] | None = None
        self._delegated_starting = True
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=str(cwd_value),
                env=env_value,
                stdin=subprocess.PIPE
                if input_text is not None
                else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            self._delegated_process = process
            # Record the confirmed own session group and leader identity
            # immediately; the leader may be reaped before timeout cleanup.
            try:
                confirmed_pgid = os.getpgid(process.pid)
            except (ProcessLookupError, PermissionError, OSError):
                confirmed_pgid = None
            if confirmed_pgid == process.pid:
                owned_pgid = confirmed_pgid
            leader_identity = _read_proc_identity(process.pid)
        except OSError as exc:
            if self._cancel_signal is not None:
                raise ReleaseError("cancelled", "signal") from exc
            raise ReleaseError("tool", "start-failed") from exc
        finally:
            self._delegated_starting = False
        if process is None:
            raise ReleaseError("tool", "start-failed")
        if self._cancel_signal is not None:
            # The signal may have arrived in the Popen/assignment window.
            try:
                process.send_signal(self._cancel_signal)
            except (OSError, ValueError):
                pass
        cleanup_failed = False
        try:
            try:
                stdout, stderr = process.communicate(
                    input=input_text.encode("utf-8")
                    if input_text is not None
                    else None,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                if owned_pgid is None:
                    try:
                        confirmed_pgid = os.getpgid(process.pid)
                    except (ProcessLookupError, PermissionError, OSError):
                        confirmed_pgid = None
                    if confirmed_pgid == process.pid:
                        owned_pgid = confirmed_pgid
                if leader_identity is None:
                    leader_identity = _read_proc_identity(process.pid)
                tracked: dict[int, tuple[str, int, int, int, int]] = {}
                if not _merge_tracked_descendants(
                    tracked,
                    process.pid,
                    owned_pgid,
                    leader_identity,
                    require_leader_root=True,
                ):
                    cleanup_failed = True
                # Give the delegated owner its existing bounded cleanup window.
                try:
                    process.send_signal(signal.SIGTERM)
                except (OSError, ValueError):
                    pass
                communicated = False
                deadline = time.monotonic() + DELEGATED_TERM_GRACE_SECONDS
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        stdout, stderr = process.communicate(
                            timeout=min(0.1, remaining)
                        )
                        communicated = True
                        break
                    except subprocess.TimeoutExpired:
                        if not _merge_tracked_descendants(
                            tracked,
                            process.pid,
                            owned_pgid,
                            leader_identity,
                        ):
                            cleanup_failed = True
                    except (OSError, ValueError):
                        cleanup_failed = True
                        break
                survivors = (
                    _tracked_descendant_present(tracked)
                    or not _owned_session_absent(owned_pgid)
                    or (
                        owned_pgid is not None
                        and not check_images._group_absent(owned_pgid)
                    )
                )
                if not communicated or survivors:
                    if _harvest_delegated_cleanup(
                        process,
                        owned_pgid,
                        leader_identity,
                        tracked,
                        drain=not communicated,
                    ):
                        cleanup_failed = True
                if cleanup_failed:
                    self._cancel_cleanup_failed = True
                if self._cancel_signal is not None:
                    raise ReleaseError(
                        "cancelled",
                        "cleanup-failed" if cleanup_failed else "signal",
                    )
                return ProcResult(
                    124,
                    "",
                    "",
                    True,
                    time.monotonic() - started,
                    cleanup_failed,
                )
            if self._cancel_signal is not None:
                raise ReleaseError("cancelled", "signal")
            return ProcResult(
                process.returncode,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
                False,
                time.monotonic() - started,
                cleanup_failed,
            )
        finally:
            self._delegated_process = None

    def _git(self, args: Sequence[str]) -> ProcResult:
        git = self.git_path or shutil.which("git", path=self.env.get("PATH"))
        if not git:
            raise ReleaseError("context", "git-missing")
        return self._run_direct(
            [git, *args], cwd=self.repo, timeout=GIT_TIMEOUT_SECONDS
        )

    def _docker(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> ProcResult:
        docker = self.docker_path or shutil.which(
            "docker", path=self.env.get("PATH")
        )
        if not docker:
            raise ReleaseError("tool", "docker-missing")
        return self._run_direct(
            [docker, *args],
            cwd=self.repo,
            env=env,
            timeout=DOCKER_TIMEOUT_SECONDS,
            input_text=input_text,
        )

    # ------------------------------------------------------------------
    # Trusted context
    # ------------------------------------------------------------------

    def trusted_context(self, source_sha: str) -> TrustedContext:
        if self.env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
            raise ReleaseError("context", "event")
        if self.env.get("GITHUB_REF") != "refs/heads/main":
            raise ReleaseError("context", "ref")
        if self.env.get("GITHUB_REPOSITORY") != REPOSITORY:
            raise ReleaseError("context", "repository")
        github_sha = _source_sha(
            self.env.get("GITHUB_SHA"), "context", "github-sha"
        )
        requested = _source_sha(source_sha, "context", "source-sha")
        if github_sha != requested:
            raise ReleaseError("context", "sha")
        head = self._git(["rev-parse", "HEAD"])
        if head.timed_out or head.exit_code != 0 or head.stdout.strip() != github_sha:
            raise ReleaseError("context", "git-head")
        status = self._git(["status", "--porcelain"])
        if status.timed_out or status.exit_code != 0:
            raise ReleaseError("context", "git-status")
        if status.stdout.strip():
            raise ReleaseError("context", "dirty")
        return TrustedContext(source_sha=github_sha, repo=self.repo)

    # ------------------------------------------------------------------
    # Builder output adapter
    # ------------------------------------------------------------------

    def _read_builder_outputs(
        self, build_root: Path, trusted: TrustedContext
    ) -> tuple[dict[str, Any], Path, Path, str, str]:
        record_path = build_root / BUILD_RECORD_FILE
        record = _read_json(
            record_path, "builder", "build-record", BUILD_RECORD_MAX_BYTES
        )
        reports = sorted((build_root / "validation").glob("*/images.json"))
        if len(reports) != 1:
            raise ReleaseError("builder", "accepted-report")
        accepted_path = reports[0]
        accepted = _read_json(
            accepted_path,
            "builder",
            "accepted-report",
            ACCEPTED_REPORT_MAX_BYTES,
        )
        self._validate_build_record(record, trusted)
        self._validate_accepted_report(accepted, record)
        record_bytes = _read_bounded_bytes(
            record_path, "builder", "build-record", BUILD_RECORD_MAX_BYTES
        )
        accepted_bytes = _read_bounded_bytes(
            accepted_path,
            "builder",
            "accepted-report",
            ACCEPTED_REPORT_MAX_BYTES,
        )
        return (
            record,
            record_path,
            accepted_path,
            _sha256_bytes(record_bytes),
            _sha256_bytes(accepted_bytes),
        )

    def _validate_build_record(
        self, record: dict[str, Any], trusted: TrustedContext
    ) -> None:
        if record.get("schema") != BUILD_RECORD_SCHEMA:
            raise ReleaseError("builder", "record-schema")
        if record.get("source_sha") != trusted.source_sha:
            raise ReleaseError("builder", "record-source-sha")
        if record.get("source_dirty") is not False:
            raise ReleaseError("builder", "record-dirty")
        git_head = record.get("git_head")
        if git_head is not None and git_head != trusted.source_sha:
            raise ReleaseError("builder", "record-git-head")
        _sha256_hex(
            record.get("source_manifest_digest"),
            "builder",
            "record-manifest",
        )
        if record.get("platform") != PLATFORM:
            raise ReleaseError("builder", "record-platform")
        for role in ROLES:
            _safe_reference(
                record.get(role + "_image"), "builder", "record-reference"
            )
            _image_id(record.get(role + "_image_id"), "builder", "record-id")
        if record["app_image_id"] == record["migrate_image_id"]:
            raise ReleaseError("builder", "record-pair")
        if record.get("build_once_per_image") is not True:
            raise ReleaseError("builder", "record-once")

    def _validate_accepted_report(
        self, accepted: dict[str, Any], record: dict[str, Any]
    ) -> None:
        if accepted.get("schema") != IMAGES_SCHEMA:
            raise ReleaseError("builder", "accepted-schema")
        if (
            accepted.get("result") != "pass"
            or accepted.get("release_candidate") is not True
        ):
            raise ReleaseError("builder", "accepted-result")
        source = accepted.get("source")
        if not isinstance(source, dict):
            raise ReleaseError("builder", "accepted-source")
        if source.get("manifest_digest") != record.get("source_manifest_digest"):
            raise ReleaseError("builder", "accepted-manifest")
        if source.get("unchanged") is not True:
            raise ReleaseError("builder", "accepted-source-changed")
        build_record = accepted.get("build_record")
        if (
            not isinstance(build_record, dict)
            or build_record.get("verified") is not True
        ):
            raise ReleaseError("builder", "accepted-build-record")
        images = accepted.get("images")
        if not isinstance(images, dict) or set(images) != set(ROLES):
            raise ReleaseError("builder", "accepted-images")
        for role in ROLES:
            entry = images.get(role)
            if not isinstance(entry, dict):
                raise ReleaseError("builder", "accepted-images")
            if entry.get("id") != record.get(role + "_image_id"):
                raise ReleaseError("builder", "accepted-id")
            if entry.get("ref") != record.get(role + "_image"):
                raise ReleaseError("builder", "accepted-ref")
            if entry.get("platform") != PLATFORM:
                raise ReleaseError("builder", "accepted-platform")
            if entry.get("source_label_match") is not True:
                raise ReleaseError("builder", "accepted-source-label")
            if entry.get("registry_digest") is not None:
                raise ReleaseError("builder", "accepted-remote-digest")
            if entry.get("registry_digest_status") != "not_obtained":
                raise ReleaseError("builder", "accepted-remote-status")
            if entry.get("local_image_id_is_registry_digest") is not False:
                raise ReleaseError("builder", "accepted-id-role")
        pair = accepted.get("pair")
        if not isinstance(pair, dict) or any(
            pair.get(key) is not True
            for key in (
                "same_source",
                "same_platform",
                "distinct_ids",
                "ids_unchanged_after_run",
            )
        ):
            raise ReleaseError("builder", "accepted-pair")
        go_test = accepted.get("go_test")
        if (
            not isinstance(go_test, dict)
            or go_test.get("result") != "pass"
            or go_test.get("package_result") != "pass"
            or go_test.get("exit_code") != 0
            or go_test.get("timed_out") is not False
        ):
            raise ReleaseError("builder", "accepted-go-test")
        cleanup = accepted.get("cleanup")
        if (
            not isinstance(cleanup, dict)
            or cleanup.get("result") != "pass"
            or cleanup.get("errors") != []
        ):
            raise ReleaseError("builder", "accepted-cleanup")

    # ------------------------------------------------------------------
    # Image inspection / save
    # ------------------------------------------------------------------

    def _inspect_image(self, reference: str) -> dict[str, Any]:
        result = self._docker(
            ["image", "inspect", "--format", "{{json .}}", reference]
        )
        if result.timed_out or result.exit_code != 0:
            raise ReleaseError("image", "inspect")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ReleaseError("image", "inspect-json") from exc
        if not isinstance(value, dict):
            raise ReleaseError("image", "inspect-json")
        image_id = _image_id(value.get("Id"), "image", "id")
        if value.get("Os") != "linux" or value.get("Architecture") != "amd64":
            raise ReleaseError("image", "platform")
        descriptor = value.get("Descriptor")
        if not isinstance(descriptor, dict):
            raise ReleaseError("image", "descriptor-missing")
        media_type = descriptor.get("mediaType")
        if media_type != OCI_INDEX:
            raise ReleaseError("image", "descriptor-media-type")
        descriptor_digest = _image_id(
            descriptor.get("digest"), "image", "descriptor-digest"
        )
        descriptor_size = _positive_size(
            descriptor.get("size"),
            "image",
            "descriptor-size",
            ROOT_JSON_MAX_BYTES,
        )
        if descriptor_digest != image_id:
            raise ReleaseError("image", "descriptor-id")
        config = value.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if not isinstance(labels, dict):
            raise ReleaseError("image", "source-label")
        label = labels.get(SOURCE_LABEL)
        _sha256_hex(label, "image", "source-label")
        return {
            "id": image_id,
            "platform": PLATFORM,
            "source_label": label,
            "root_descriptor": {
                "mediaType": media_type,
                "digest": descriptor_digest,
                "size": descriptor_size,
            },
        }

    def _verify_local_images(
        self, record: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        inspected: dict[str, dict[str, Any]] = {}
        for role in ROLES:
            reference = record[role + "_image"]
            value = self._inspect_image(reference)
            if value["id"] != record[role + "_image_id"]:
                raise ReleaseError("image", "id-mismatch")
            if value["source_label"] != record["source_manifest_digest"]:
                raise ReleaseError("image", "source-label")
            inspected[role] = value
        if inspected["app"]["id"] == inspected["migrate"]["id"]:
            raise ReleaseError("image", "pair")
        return inspected

    def _verify_image_identity(
        self,
        role: str,
        value: Mapping[str, Any],
        descriptor: Mapping[str, Any],
        stage: str,
    ) -> None:
        expected = descriptor["images"][role]
        if value.get("id") != expected["local_id"]:
            raise ReleaseError(stage, role + "-id")
        actual_root = value.get("root_descriptor")
        expected_root = expected["root_descriptor"]
        if (
            not isinstance(actual_root, dict)
            or actual_root.get("mediaType") != expected_root["mediaType"]
            or actual_root.get("digest") != expected_root["digest"]
            or actual_root.get("size") != expected_root["size"]
        ):
            raise ReleaseError(stage, role + "-root")
        if value.get("platform") != expected["platform"]:
            raise ReleaseError(stage, role + "-platform")
        if value.get("source_label") != descriptor["source_manifest_digest"]:
            raise ReleaseError(stage, role + "-source-label")

    # ------------------------------------------------------------------
    # Existing gates
    # ------------------------------------------------------------------

    def _run_gate(
        self,
        name: str,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        delegated: bool = True,
    ) -> ProcResult:
        runner = self._run_delegated if delegated else self._run_direct
        result = runner(
            argv,
            cwd=self.repo,
            env=env,
            timeout=None,
        )
        if result.timed_out or result.exit_code != 0:
            raise ReleaseError("preflight", name)
        return result

    def _run_local_gates(
        self, args: argparse.Namespace, root: Path
    ) -> dict[str, Any]:
        gates: dict[str, Any] = {}
        preflight_root = root / "preflight"
        _mkdir_private(preflight_root)
        cache = Path(args.cache_dir).expanduser()
        if not cache.is_absolute():
            raise ReleaseError("cli", "cache-dir")
        try:
            cache.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ReleaseError("preflight", "cache-dir") from exc
        python_result = self._run_direct(
            [
                sys.executable,
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                "scripts",
                "-p",
                "test_*.py",
            ],
            cwd=self.repo,
            timeout=LOCAL_GATE_TIMEOUT_SECONDS,
        )
        if python_result.timed_out or python_result.exit_code != 0:
            raise ReleaseError("preflight", "python")
        gates["python"] = {"exit": 0, "timed_out": False}

        setup_argv = [
            sys.executable,
            "-B",
            str(SCRIPT_DIR / "check-setup.py"),
            "--go",
            args.go,
            "--cache-dir",
            args.cache_dir,
        ]
        if args.prepare_inputs:
            setup_argv.append("--download-modules")
        self._run_gate("setup", setup_argv, delegated=False)
        gates["setup"] = {"exit": 0, "timed_out": False}

        for scope in ("format", "static", "unit"):
            stage_root = root / "preflight" / scope
            _mkdir_private(stage_root)
            result = self._run_delegated(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT_DIR / "check.py"),
                    "--scope",
                    scope,
                    "--go",
                    args.go,
                    "--cache-dir",
                    args.cache_dir,
                    "--artifact-root",
                    str(stage_root),
                ],
                cwd=self.repo,
                timeout=None,
            )
            if result.timed_out or result.exit_code != 0:
                raise ReleaseError("preflight", scope)
            checks = _read_json(
                stage_root / "checks.json", "preflight", scope, 8 * 1024 * 1024
            )
            if (
                checks.get("schema") != CHECK_SCHEMA
                or checks.get("scope") != scope
                or checks.get("result") != "pass"
            ):
                raise ReleaseError("preflight", scope + "-report")
            gates[scope] = {
                "exit": 0,
                "timed_out": False,
                "report_sha256": sha256_file(stage_root / "checks.json"),
            }

        db_root = root / "preflight" / "db"
        _mkdir_private(db_root)
        db_argv = [
            sys.executable,
            "-B",
            str(SCRIPT_DIR / "check-db-foundation.py"),
            "--go",
            args.go,
            "--cache-dir",
            args.cache_dir,
            "--artifact-root",
            str(db_root),
            "--cleanup-owned-resources",
        ]
        if args.prepare_inputs:
            db_argv.extend(["--prepare-images", "--download-modules"])
        self._run_gate("db", db_argv)
        db_reports = sorted(db_root.glob("*/db-foundation.json"))
        if len(db_reports) != 1:
            raise ReleaseError("preflight", "db-report")
        db_report = _read_json(
            db_reports[0], "preflight", "db-report", 8 * 1024 * 1024
        )
        cleanup = db_report.get("cleanup")
        if (
            db_report.get("result") != "pass"
            or not isinstance(cleanup, dict)
            or cleanup.get("result") != "pass"
        ):
            raise ReleaseError("preflight", "db-report")
        gates["db"] = {
            "exit": 0,
            "timed_out": False,
            "report_sha256": sha256_file(db_reports[0]),
        }

        cancellation_root = root / "preflight" / "cancellation-artifacts"
        cancellation_reports = root / "preflight" / "reports"
        _mkdir_private(cancellation_root)
        _mkdir_private(cancellation_reports)
        cancellation_argv = [
            sys.executable,
            "-B",
            str(SCRIPT_DIR / "check-ci-cancellation.py"),
            "--go",
            args.go,
            "--cache-dir",
            args.cache_dir,
            "--artifact-root",
            str(cancellation_root),
            "--report-dir",
            str(cancellation_reports),
        ]
        if args.prepare_inputs:
            cancellation_argv.append("--prepare-images")
        self._run_gate("cancellation", cancellation_argv)
        cancellation_report = cancellation_reports / "cancellation-report.json"
        value = _read_json(
            cancellation_report,
            "preflight",
            "cancellation-report",
            8 * 1024 * 1024,
        )
        if value.get("result") != "pass":
            raise ReleaseError("preflight", "cancellation-report")
        gates["cancellation"] = {
            "exit": 0,
            "timed_out": False,
            "report_sha256": sha256_file(cancellation_report),
        }

        vulnerability_root = root / "preflight" / "vulnerability"
        tool_dir = (
            Path(args.cache_dir).expanduser()
            / "tools"
            / "go1.27.4-govulncheck-v1.9.0"
        )
        _mkdir_private(vulnerability_root)
        vulnerability_argv = [
            sys.executable,
            "-B",
            str(SCRIPT_DIR / "check-vulnerabilities.py"),
            "--go",
            args.go,
            "--cache-dir",
            args.cache_dir,
            "--tool-dir",
            str(tool_dir),
            "--artifact-root",
            str(vulnerability_root),
        ]
        if args.prepare_inputs:
            vulnerability_argv.append("--prepare-tool")
        self._run_gate("vulnerability", vulnerability_argv)
        vulnerability_report = vulnerability_root / "vulnerability-report.json"
        value = _read_json(
            vulnerability_report,
            "preflight",
            "vulnerability-report",
            8 * 1024 * 1024,
        )
        if value.get("result") != "pass":
            raise ReleaseError("preflight", "vulnerability-report")
        gates["vulnerability"] = {
            "exit": 0,
            "timed_out": False,
            "report_sha256": sha256_file(vulnerability_report),
        }
        return gates

    # ------------------------------------------------------------------
    # prepare
    # ------------------------------------------------------------------

    def prepare(self, args: argparse.Namespace) -> int:
        self._cancel_signal = None
        self._cancel_cleanup_failed = False
        self._in_cleanup = False
        self._delegated_starting = False
        check_images._reset_process_cancel()
        with self._cancel_handlers():
            return self._prepare_impl(args)

    def _prepare_impl(self, args: argparse.Namespace) -> int:
        root = Path(args.artifact_root).expanduser()
        if not root.is_absolute():
            raise ReleaseError("cli", "artifact-root")
        _fresh_root(root)
        # prepare never needs the publish credential and must not pass it to
        # builder or check subprocesses.
        self.env.pop("GITHUB_TOKEN", None)

        trusted = self.trusted_context(args.source_sha)
        gates = self._run_local_gates(args, root)

        build_root = root / "build"
        _mkdir_private(build_root)
        builder_argv = [
            sys.executable,
            "-B",
            str(SCRIPT_DIR / "build-images.py"),
            "--go",
            args.go,
            "--cache-dir",
            args.cache_dir,
            "--artifact-root",
            str(build_root),
        ]
        if args.prepare_inputs:
            builder_argv.append("--prepare-inputs")
        builder_result = self._run_delegated(
            builder_argv,
            cwd=self.repo,
            timeout=BUILDER_TIMEOUT_SECONDS,
        )
        if builder_result.timed_out or builder_result.exit_code != 0:
            raise ReleaseError("builder", "failed")
        (
            record,
            record_path,
            accepted_path,
            record_sha,
            accepted_sha,
        ) = self._read_builder_outputs(build_root, trusted)
        inspected = self._verify_local_images(record)

        bundle = root / DEFAULT_BUNDLE
        _mkdir_private(bundle)
        pair_path = bundle / PAIR_FILE
        save_result = self._docker(
            [
                "image",
                "save",
                "--output",
                str(pair_path),
                record["app_image"],
                record["migrate_image"],
            ]
        )
        if save_result.timed_out or save_result.exit_code != 0:
            raise ReleaseError("image", "save")
        try:
            pair_path.chmod(0o600)
        except OSError as exc:
            raise ReleaseError("artifact", "chmod") from exc
        roots = _parse_pair_tar(
            pair_path,
            [record["app_image_id"], record["migrate_image_id"]],
        )
        for role in ROLES:
            tar_root = roots[record[role + "_image_id"]]
            actual_root = inspected[role]["root_descriptor"]
            if (
                tar_root.media_type != actual_root["mediaType"]
                or tar_root.digest != actual_root["digest"]
                or tar_root.size != actual_root["size"]
            ):
                raise ReleaseError("tar", "actual-root")

        record_bytes = _read_bounded_bytes(
            record_path, "builder", "build-record", BUILD_RECORD_MAX_BYTES
        )
        accepted_bytes = _read_bounded_bytes(
            accepted_path,
            "builder",
            "accepted-report",
            ACCEPTED_REPORT_MAX_BYTES,
        )
        _write_bytes_exclusive(bundle / BUILD_RECORD_FILE, record_bytes)
        _write_bytes_exclusive(bundle / ACCEPTED_BUNDLE_FILE, accepted_bytes)
        archive_sha, archive_size = _stream_sha256(
            pair_path, "archive", "hash"
        )
        descriptor = {
            "schema": DESCRIPTOR_SCHEMA,
            "source_sha": trusted.source_sha,
            "source_manifest_digest": record["source_manifest_digest"],
            "platform": PLATFORM,
            "created_utc": utc_now(),
            "archive": {
                "file": PAIR_FILE,
                "sha256": archive_sha,
                "size": archive_size,
            },
            "builder": {
                "build_record_sha256": record_sha,
                "accepted_report_sha256": accepted_sha,
                "accepted_report_file": ACCEPTED_BUNDLE_FILE,
            },
            "images": {
                role: {
                    "reference": record[role + "_image"],
                    "local_id": record[role + "_image_id"],
                    "root_descriptor": dict(
                        inspected[role]["root_descriptor"]
                    ),
                    "source_label": record["source_manifest_digest"],
                    "platform": PLATFORM,
                }
                for role in ROLES
            },
            "targets": dict(TARGETS),
            "tag": TAG_PREFIX + trusted.source_sha,
        }
        descriptor_path = bundle / DESCRIPTOR_FILE
        _write_json_exclusive(descriptor_path, descriptor)
        descriptor_sha = sha256_file(descriptor_path)
        handoff = {
            "schema": HANDOFF_POINTER_SCHEMA,
            "source_sha": trusted.source_sha,
            "descriptor_sha256": descriptor_sha,
            "bundle": DEFAULT_BUNDLE,
            "tag": descriptor["tag"],
        }
        _write_json_exclusive(root / HANDOFF_FILE, handoff)
        summary = {
            "schema": SUMMARY_SCHEMA,
            "stage": "prepare",
            "utc": utc_now(),
            "outcome": "pass",
            "source_sha": trusted.source_sha,
            "source_dirty": False,
            "platform": PLATFORM,
            "descriptor_sha256": descriptor_sha,
            "bundle": DEFAULT_BUNDLE,
            "gates": gates,
            "db_fixture_tool": {
                "role": "test-only-foundation-tool",
                "is_release_pair_candidate": False,
            },
            "builder": {
                "calls": 1,
                "prepare_inputs": bool(args.prepare_inputs),
                "fixture_tool_is_release_candidate": False,
                "build_record_sha256": record_sha,
                "accepted_report_sha256": accepted_sha,
                "accepted_report_file": ACCEPTED_BUNDLE_FILE,
            },
            "save": {"calls": 1, "archive_sha256": archive_sha, "size": archive_size},
            "images": {
                role: {
                    "reference": record[role + "_image"],
                    "local_id": record[role + "_image_id"],
                    "root_descriptor": descriptor["images"][role][
                        "root_descriptor"
                    ],
                }
                for role in ROLES
            },
            "not_proven": [
                "remote-registry-digest",
                "ghcr-package-access",
                "digest-pull",
                "deployment",
            ],
        }
        _write_json_atomic(root / SUMMARY_FILE, summary)
        return 0

    # ------------------------------------------------------------------
    # verify-handoff
    # ------------------------------------------------------------------

    def verify_handoff(self, args: argparse.Namespace) -> int:
        bundle = Path(args.bundle_dir).expanduser()
        descriptor, _ = _verify_handoff(
            bundle,
            args.expected_descriptor_sha256,
            expected_source_sha=args.source_sha,
        )
        summary = {
            "result": "pass",
            "stage": "verify-handoff",
            "source_sha": descriptor["source_sha"],
            "platform": descriptor["platform"],
            "descriptor_sha256": sha256_file(bundle / DESCRIPTOR_FILE),
            "archive_sha256": descriptor["archive"]["sha256"],
            "images": {
                role: descriptor["images"][role]["local_id"] for role in ROLES
            },
        }
        print(json.dumps(summary, sort_keys=True))
        return 0

    # ------------------------------------------------------------------
    # publish
    # ------------------------------------------------------------------

    def _github_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "payment-hub-release",
        }

    def _check_package(self, role: str, token: str) -> dict[str, Any]:
        name = PACKAGE_NAMES[role]
        url = f"{GITHUB_API}/users/xtianxx/packages/container/{name}"
        response = self.http(
            "GET",
            url,
            headers=self._github_headers(token),
            timeout=HTTP_TIMEOUT_SECONDS,
            max_bytes=HTTP_MAX_BYTES,
        )
        if response.status == 403:
            raise ReleaseError("package", role + "-forbidden")
        if response.status == 404:
            raise ReleaseError("package", role + "-not-found")
        if response.status != 200:
            raise ReleaseError("package", role + "-http")
        if len(response.body) > HTTP_MAX_BYTES:
            raise ReleaseError("package", role + "-body")
        try:
            value = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("package", role + "-json") from exc
        if not isinstance(value, dict):
            raise ReleaseError("package", role + "-json")
        if value.get("visibility") != "private":
            raise ReleaseError("package", role + "-visibility")
        repository = value.get("repository")
        if (
            not isinstance(repository, dict)
            or repository.get("full_name") != REPOSITORY
        ):
            raise ReleaseError("package", role + "-repository")
        if value.get("package_type") not in (None, "container"):
            raise ReleaseError("package", role + "-type")
        if value.get("name") not in (None, name):
            raise ReleaseError("package", role + "-name")
        owner = value.get("owner")
        if isinstance(owner, dict) and owner.get("login") not in (
            None,
            "xtianxx",
        ):
            raise ReleaseError("package", role + "-owner")
        return {
            "name": name,
            "visibility": "private",
            "repository": REPOSITORY,
        }

    def _check_packages(self, token: str) -> dict[str, dict[str, Any]]:
        return {role: self._check_package(role, token) for role in ROLES}

    def _registry_manifest(
        self,
        role: str,
        candidate_digest: str,
        expected_root: Mapping[str, Any],
        token: str,
        actor: str,
    ) -> dict[str, Any]:
        target = TARGETS[role]
        repository = target.split("/", 1)[1]
        url = f"{GHCR_BASE}/v2/{repository}/manifests/{candidate_digest}"
        headers = {
            "Accept": OCI_INDEX + ", " + OCI_MANIFEST,
            "User-Agent": "payment-hub-release",
        }
        response = self.http(
            "GET",
            url,
            headers=headers,
            timeout=HTTP_TIMEOUT_SECONDS,
            max_bytes=REGISTRY_MANIFEST_MAX_BYTES,
        )
        if response.status == 200:
            raise ReleaseError("registry", role + "-unexpected-public")
        if response.status != 401:
            raise ReleaseError("registry", role + "-challenge-status")
        challenge = response.headers.get("www-authenticate", "")
        realm, service, scope = _parse_bearer_challenge(challenge, role)
        expected_scope = f"repository:{repository}:pull"
        if (
            realm != TOKEN_REALM
            or service != TOKEN_SERVICE
            or scope != expected_scope
        ):
            raise ReleaseError("registry", role + "-challenge")
        basic = base64.b64encode(
            (actor + ":" + token).encode("utf-8")
        ).decode("ascii")
        token_url = TOKEN_REALM + "?" + urllib.parse.urlencode(
            {"service": TOKEN_SERVICE, "scope": expected_scope}
        )
        token_response = self.http(
            "GET",
            token_url,
            headers={
                "Authorization": "Basic " + basic,
                "User-Agent": "payment-hub-release",
            },
            timeout=HTTP_TIMEOUT_SECONDS,
            max_bytes=TOKEN_MAX_BYTES,
        )
        if token_response.status != 200:
            raise ReleaseError("registry", role + "-token")
        try:
            token_value = json.loads(token_response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("registry", role + "-token-json") from exc
        if not isinstance(token_value, dict):
            raise ReleaseError("registry", role + "-token-json")
        scoped_token = token_value.get("token") or token_value.get("access_token")
        if (
            not isinstance(scoped_token, str)
            or not scoped_token
            or len(scoped_token) > 8192
        ):
            raise ReleaseError("registry", role + "-token-value")
        manifest_response = self.http(
            "GET",
            url,
            headers={
                **headers,
                "Authorization": "Bearer " + scoped_token,
            },
            timeout=HTTP_TIMEOUT_SECONDS,
            max_bytes=REGISTRY_MANIFEST_MAX_BYTES,
        )
        if manifest_response.status != 200:
            raise ReleaseError("registry", role + "-manifest-status")
        if len(manifest_response.body) > REGISTRY_MANIFEST_MAX_BYTES:
            raise ReleaseError("registry", role + "-manifest-body")
        body_sha = _sha256_bytes(manifest_response.body)
        expected_hex = candidate_digest.split(":", 1)[1]
        if body_sha != expected_hex:
            raise ReleaseError("registry", role + "-body-hash")
        header_digest = manifest_response.headers.get("docker-content-digest", "")
        if header_digest != candidate_digest:
            raise ReleaseError("registry", role + "-header-digest")
        content_type = manifest_response.headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip()
        if media_type != expected_root["mediaType"]:
            raise ReleaseError("registry", role + "-media-type")
        if len(manifest_response.body) != expected_root["size"]:
            raise ReleaseError("registry", role + "-size")
        try:
            value = json.loads(manifest_response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("registry", role + "-manifest-json") from exc
        content = _parse_root_index(value, "registry", role + "-root")
        if (
            content.runtime_digest != expected_root.get("runtime_digest")
            or content.runtime_size != expected_root.get("runtime_size")
            or content.runtime_media_type != expected_root.get("runtime_media_type")
            or not set(content.attestations).issubset(
                set(expected_root.get("attestations", ()))
            )
        ):
            raise ReleaseError("registry", role + "-runtime")
        return {
            "digest": candidate_digest,
            "mediaType": media_type,
            "body_sha256": body_sha,
            "size": len(manifest_response.body),
            "header_digest": header_digest,
        }

    def _prepare_publish_inputs(
        self, args: argparse.Namespace, root: Path
    ) -> dict[str, Any]:
        if not args.prepare_inputs:
            return {
                "requested": False,
                "postgres_pull": "not-run",
                "module_prepare": "not-run",
                "precompile_only": "not-run",
            }
        pull = self._docker(
            ["pull", "--platform", PLATFORM, POSTGRES_IMAGE]
        )
        if pull.timed_out or pull.exit_code != 0:
            raise ReleaseError("preflight", "postgres-pull")
        cache = Path(args.cache_dir).expanduser()
        if not cache.is_absolute():
            raise ReleaseError("cli", "cache-dir")
        try:
            cache.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ReleaseError("preflight", "cache-dir") from exc
        setup = self._run_direct(
            [
                sys.executable,
                "-B",
                str(SCRIPT_DIR / "check-setup.py"),
                "--go",
                args.go,
                "--cache-dir",
                args.cache_dir,
                "--download-modules",
            ],
            cwd=self.repo,
            timeout=SETUP_TIMEOUT_SECONDS,
        )
        if setup.timed_out or setup.exit_code != 0:
            raise ReleaseError("preflight", "module-prepare")
        env = dict(self.env)
        env.update(_cache_env(cache))
        precompile = self._run_direct(
            [
                args.go,
                "test",
                "-race",
                "-run",
                "^$",
                "./...",
            ],
            cwd=self.repo,
            env=env,
            timeout=PRECOMPILE_TIMEOUT_SECONDS,
        )
        if precompile.timed_out or precompile.exit_code != 0:
            raise ReleaseError("preflight", "precompile-only")
        return {
            "requested": True,
            "postgres_pull": "pass",
            "module_prepare": "pass",
            "precompile_only": "pass",
            "business_tests_run": False,
        }

    def _delivery_record(
        self,
        descriptor: dict[str, Any],
        remote: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        return {
            "schema": BUILD_RECORD_SCHEMA,
            "source_sha": descriptor["source_sha"],
            "source_dirty": False,
            "source_manifest_digest": descriptor["source_manifest_digest"],
            "platform": PLATFORM,
            "app_image": TARGETS["app"] + "@" + remote["app"]["digest"],
            "app_image_id": descriptor["images"]["app"]["local_id"],
            "migrate_image": TARGETS["migrate"] + "@" + remote["migrate"]["digest"],
            "migrate_image_id": descriptor["images"]["migrate"]["local_id"],
            "utc": utc_now(),
            "build_once_per_image": True,
            "delivery_origin": {
                "build_record_sha256": descriptor["builder"][
                    "build_record_sha256"
                ],
                "accepted_report_sha256": descriptor["builder"][
                    "accepted_report_sha256"
                ],
                "descriptor_sha256": sha256_file(
                    Path(self._bundle) / DESCRIPTOR_FILE
                ),
                "source_sha": descriptor["source_sha"],
            },
        }

    def _revalidate(
        self,
        args: argparse.Namespace,
        root: Path,
        descriptor: dict[str, Any],
        remote: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        delivery_dir = root / "delivery"
        _mkdir_private(delivery_dir)
        record = self._delivery_record(descriptor, remote)
        record_path = delivery_dir / "run-record.json"
        _write_json_exclusive(record_path, record)
        revalidation_root = root / "revalidation"
        _mkdir_private(revalidation_root)
        env = dict(self.env)
        env["PAYMENT_HUB_APP_IMAGE"] = record["app_image"]
        env["PAYMENT_HUB_MIGRATE_IMAGE"] = record["migrate_image"]
        env["PAYMENT_HUB_IMAGE_BUILD_RECORD"] = str(record_path)
        result = self._run_delegated(
            [
                sys.executable,
                "-B",
                str(SCRIPT_DIR / "check.py"),
                "--scope",
                "image",
                "--go",
                args.go,
                "--cache-dir",
                args.cache_dir,
                "--artifact-root",
                str(revalidation_root),
            ],
            cwd=self.repo,
            env=env,
            timeout=None,
        )
        if result.timed_out or result.exit_code != 0:
            raise ReleaseError("revalidation", "check-images")
        checks_path = revalidation_root / "checks.json"
        checks = _read_json(
            checks_path, "revalidation", "checks", 8 * 1024 * 1024
        )
        stages = checks.get("stages")
        stage = stages.get("image") if isinstance(stages, dict) else None
        report = stage.get("report") if isinstance(stage, dict) else None
        if (
            checks.get("schema") != CHECK_SCHEMA
            or checks.get("scope") != "image"
            or checks.get("result") != "pass"
            or not isinstance(stage, dict)
            or stage.get("status") != "pass"
            or not isinstance(report, dict)
            or report.get("result") != "pass"
        ):
            raise ReleaseError("revalidation", "report")
        return {
            "result": "pass",
            "checks_sha256": sha256_file(checks_path),
            "delivery_record_sha256": sha256_file(record_path),
        }

    def _write_summary(
        self,
        root: Path,
        *,
        stage: str,
        source_sha: str,
        descriptor_sha256: str | None,
        state: Mapping[str, Any],
    ) -> None:
        if state.get("published") is True and self._cancel_signal is not None:
            raise ReleaseError("cancelled", "signal")
        summary = {
            "schema": SUMMARY_SCHEMA,
            "stage": stage,
            "utc": utc_now(),
            "outcome": "pass"
            if state.get("published") is True
            else "fail",
            "published": bool(state.get("published")),
            "source_sha": source_sha,
            "platform": PLATFORM,
            "descriptor_sha256": descriptor_sha256,
            "pushed_roles": list(state.get("pushed_roles", [])),
            "packages": state.get("packages", {}),
            "push": state.get("push", {}),
            "remote": state.get("remote", {}),
            "pull": state.get("pull", {}),
            "revalidation": state.get("revalidation"),
            "prepare_inputs": state.get("prepare_inputs"),
            "credentials_removed": state.get("credentials_removed", False),
            "cleanup_failed": bool(
                state.get("cleanup_failed", self._cancel_cleanup_failed)
            ),
            "failure": state.get("failure"),
            "limits": [
                "package-existence-check-not-atomic-no-create",
                "package-deletion-race-not-excluded",
                "pre-push-tag-identity-check-not-atomic",
                "remote-blob-availability-not-proven-beyond-cache-hit-pull",
            ],
            "http_deadline_policy": (
                "SIGALRM absolute wall deadline around headers and bounded body; "
                "socket inactivity timeout is not claimed as an absolute bound"
            ),
            "deployment": "not-performed",
        }
        _write_json_atomic(root / SUMMARY_FILE, summary)

    def _bundle_descriptor_sha(self) -> str | None:
        if not self._bundle:
            return None
        try:
            return sha256_file(Path(self._bundle) / DESCRIPTOR_FILE)
        except OSError:
            return None

    def publish(self, args: argparse.Namespace) -> int:
        self._cancel_signal = None
        self._cancel_cleanup_failed = False
        self._in_cleanup = False
        self._delegated_starting = False
        check_images._reset_process_cancel()
        try:
            with self._cancel_handlers():
                return self._publish_impl(args)
        except ReleaseError as exc:
            root = Path(args.artifact_root).expanduser()
            if root.is_absolute() and root.exists() and (root / SUMMARY_FILE).exists():
                raise
            if root.is_absolute():
                try:
                    _fresh_root(root)
                except ReleaseError:
                    pass
                else:
                    state = {
                        "published": False,
                        "pushed_roles": [],
                        "credentials_removed": False,
                        "packages": {},
                        "push": {},
                        "remote": {},
                        "pull": {},
                        "revalidation": None,
                        "prepare_inputs": None,
                        "cleanup_failed": self._cancel_cleanup_failed,
                        "failure": {"stage": exc.stage, "kind": exc.kind},
                    }
                    try:
                        self._write_summary(
                            root,
                            stage="publish",
                            source_sha=args.source_sha,
                            descriptor_sha256=self._bundle_descriptor_sha(),
                            state=state,
                        )
                    except ReleaseError:
                        pass
            raise

    def _publish_impl(self, args: argparse.Namespace) -> int:
        root = Path(args.artifact_root).expanduser()
        if not root.is_absolute():
            raise ReleaseError("cli", "artifact-root")
        _fresh_root(root)
        source_sha = _source_sha(args.source_sha, "cli", "source-sha")
        state: dict[str, Any] = {
            "published": False,
            "pushed_roles": [],
            "credentials_removed": False,
            "packages": {},
            "push": {},
            "remote": {},
            "pull": {},
            "revalidation": None,
            "prepare_inputs": None,
            "cleanup_failed": False,
            "failure": None,
        }
        try:
            if not args.approve_external:
                raise ReleaseError("approval", "external")
            if not args.package_preconditions_verified:
                raise ReleaseError("approval", "package-preconditions")
            token = self.env.get("GITHUB_TOKEN", "")
            if not token:
                raise ReleaseError("auth", "token-missing")
            # The token is kept only in memory and passed to docker login via
            # stdin.  Do not inherit it into Git/check/Docker subprocess
            # environments.
            self.env.pop("GITHUB_TOKEN", None)
            trusted = self.trusted_context(source_sha)
            actor = self.env.get("GITHUB_ACTOR", "")
            if not actor or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", actor):
                raise ReleaseError("auth", "actor")
            if not args.bundle_dir or not args.expected_descriptor_sha256:
                raise ReleaseError("cli", "bundle")
            bundle = Path(args.bundle_dir).expanduser()
            self._bundle = str(bundle)
            descriptor, roots = _verify_handoff(
                bundle,
                args.expected_descriptor_sha256,
                expected_source_sha=trusted.source_sha,
            )
            state["prepare_inputs"] = self._prepare_publish_inputs(args, root)
            state["packages"] = self._check_packages(token)
            load_result = self._docker(["load", "--input", str(bundle / PAIR_FILE)])
            if load_result.timed_out or load_result.exit_code != 0:
                raise ReleaseError("load", "docker-load")
            for role in ROLES:
                value = self._inspect_image(descriptor["images"][role]["local_id"])
                self._verify_image_identity(role, value, descriptor, "load")
            auth_dir = Path(tempfile.mkdtemp(prefix="payment-hub-release-auth-"))
            try:
                auth_dir.chmod(0o700)
                auth_env = dict(self.env)
                auth_env["DOCKER_CONFIG"] = str(auth_dir)
                login = self._docker(
                    [
                        "login",
                        GHCR,
                        "--username",
                        actor,
                        "--password-stdin",
                    ],
                    env=auth_env,
                    input_text=token,
                )
                if login.timed_out or login.exit_code != 0:
                    raise ReleaseError("auth", "login")
                for role in ROLES:
                    # Metadata and the target SHA tag are rechecked immediately
                    # before each push.  A replacement tag must not be pushed.
                    state["packages"][role] = self._check_package(role, token)
                    local_id = descriptor["images"][role]["local_id"]
                    target_ref = TARGETS[role] + ":" + descriptor["tag"]
                    tag = self._docker(["tag", local_id, target_ref], env=auth_env)
                    if tag.timed_out or tag.exit_code != 0:
                        raise ReleaseError("push", role + "-tag")
                    tagged = self._inspect_image(target_ref)
                    self._verify_image_identity(role, tagged, descriptor, "push")
                    push = self._docker(["push", target_ref], env=auth_env)
                    if push.timed_out or push.exit_code != 0:
                        raise ReleaseError("push", role + "-failed")
                    candidate = _push_candidate_digest(push)
                    expected = descriptor["images"][role]["root_descriptor"][
                        "digest"
                    ]
                    if candidate != expected:
                        raise ReleaseError("push", role + "-digest")
                    state["push"][role] = {
                        "candidate_digest": candidate,
                        "exit": 0,
                    }
                    state["pushed_roles"].append(role)
                    state["remote"][role] = self._registry_manifest(
                        role,
                        candidate,
                        {
                            **descriptor["images"][role]["root_descriptor"],
                            "runtime_digest": roots[
                                descriptor["images"][role]["local_id"]
                            ].runtime_digest,
                            "runtime_size": roots[
                                descriptor["images"][role]["local_id"]
                            ].runtime_size,
                            "runtime_media_type": roots[
                                descriptor["images"][role]["local_id"]
                            ].runtime_media_type,
                            "attestations": roots[
                                descriptor["images"][role]["local_id"]
                            ].attestations,
                        },
                        token,
                        actor,
                    )
                for role in ROLES:
                    remote_ref = (
                        TARGETS[role] + "@" + state["remote"][role]["digest"]
                    )
                    pull = self._docker(["pull", remote_ref], env=auth_env)
                    if pull.timed_out or pull.exit_code != 0:
                        raise ReleaseError("pull", role + "-failed")
                    value = self._inspect_image(remote_ref)
                    self._verify_image_identity(role, value, descriptor, "pull")
                    state["pull"][role] = {"exit": 0, "id": value["id"]}
            finally:
                self._in_cleanup = True
                try:
                    shutil.rmtree(auth_dir, ignore_errors=True)
                finally:
                    self._in_cleanup = False
                state["credentials_removed"] = not auth_dir.exists()
                if not state["credentials_removed"]:
                    state["failure"] = {
                        "stage": "auth",
                        "kind": "credentials-remaining",
                    }
            if state["failure"] is not None:
                raise ReleaseError(
                    state["failure"]["stage"], state["failure"]["kind"]
                )
            state["revalidation"] = self._revalidate(
                args, root, descriptor, state["remote"]
            )
            if self._cancel_signal is not None:
                raise ReleaseError("cancelled", "signal")
            state["published"] = True
            self._write_summary(
                root,
                stage="publish",
                source_sha=trusted.source_sha,
                descriptor_sha256=self._bundle_descriptor_sha(),
                state=state,
            )
            return 0
        except ReleaseError as exc:
            state["published"] = False
            state["failure"] = {"stage": exc.stage, "kind": exc.kind}
            state["cleanup_failed"] = self._cancel_cleanup_failed
            self._write_summary(
                root,
                stage="publish",
                source_sha=source_sha,
                descriptor_sha256=self._bundle_descriptor_sha(),
                state=state,
            )
            raise


def _parse_bearer_challenge(value: str, role: str) -> tuple[str, str, str]:
    if not value or len(value) > 4096:
        raise ReleaseError("registry", role + "-challenge")
    scheme, _, rest = value.partition(" ")
    if scheme.lower() != "bearer":
        raise ReleaseError("registry", role + "-challenge")
    fields: dict[str, str] = {}
    for match in re.finditer(r'([A-Za-z]+)="([^"]*)"', rest):
        key = match.group(1).lower()
        if key in fields:
            raise ReleaseError("registry", role + "-challenge")
        fields[key] = match.group(2)
    if set(fields) != {"realm", "service", "scope"}:
        raise ReleaseError("registry", role + "-challenge")
    return fields["realm"], fields["service"], fields["scope"]


def _push_candidate_digest(result: ProcResult) -> str:
    values = set(
        re.findall(
            r"(?i)(?:digest|docker-content-digest)\s*:\s*(sha256:[0-9a-f]{64})",
            result.stdout + "\n" + result.stderr,
        )
    )
    if len(values) != 1:
        raise ReleaseError("push", "digest")
    return values.pop()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReleaseError("registry", "redirect")


class _HttpDeadline:
    """Small Linux-main-thread absolute wall deadline for one HTTP exchange.

    Socket timeouts are inactivity timeouts, not absolute wall limits.  This
    guard uses SIGALRM around both header receipt and the bounded body read.
    It never replaces an earlier absolute alarm with a later one, and restores
    the caller's handler/timer on exit, including on exceptions.
    """

    def __init__(self, seconds: float, category: str = "http") -> None:
        self.seconds = seconds
        self.category = category
        self.old_handler: Any = None
        self.old_remaining = 0.0
        self.old_interval = 0.0
        self.started = 0.0
        self.installed = False

    def __enter__(self) -> "_HttpDeadline":
        if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
            raise ReleaseError(self.category, "deadline-unsupported")
        if self.seconds <= 0:
            raise ReleaseError(self.category, "deadline")
        try:
            old_handler = signal.getsignal(signal.SIGALRM)
            old_remaining, old_interval = signal.getitimer(signal.ITIMER_REAL)
        except (AttributeError, OSError, ValueError) as exc:
            raise ReleaseError(self.category, "deadline-unsupported") from exc
        self.old_handler = old_handler
        self.old_remaining = old_remaining
        self.old_interval = old_interval
        self.started = time.monotonic()
        limit = self.seconds
        if old_remaining > 0:
            limit = min(limit, old_remaining)
        try:
            signal.signal(signal.SIGALRM, self._handler)
            signal.setitimer(signal.ITIMER_REAL, limit)
        except (AttributeError, OSError, ValueError) as exc:
            try:
                signal.signal(signal.SIGALRM, old_handler)
                if old_remaining > 0:
                    signal.setitimer(
                        signal.ITIMER_REAL, old_remaining, old_interval
                    )
            except (AttributeError, OSError, ValueError):
                pass
            raise ReleaseError(self.category, "deadline-unsupported") from exc
        self.installed = True
        return self

    def _handler(self, signum: int, frame: Any) -> None:
        raise ReleaseError(self.category, "deadline")

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if not self.installed:
            return False
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self.old_handler)
            if self.old_remaining > 0:
                elapsed = time.monotonic() - self.started
                remaining = max(0.0, self.old_remaining - elapsed)
                if remaining > 0:
                    signal.setitimer(
                        signal.ITIMER_REAL, remaining, self.old_interval
                    )
        except (AttributeError, OSError, ValueError):
            pass
        return False


def _default_http(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
    max_bytes: int,
) -> HttpResult:
    request = urllib.request.Request(url, method=method, headers=dict(headers))
    opener = urllib.request.build_opener(_NoRedirect)
    with _HttpDeadline(timeout, "http"):
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            try:
                try:
                    body = exc.read(max_bytes + 1)
                except OSError as read_exc:
                    raise ReleaseError("tool", "http") from read_exc
            finally:
                exc.close()
            if len(body) > max_bytes:
                raise ReleaseError("tool", "http-body") from exc
            return HttpResult(
                exc.code,
                {key.lower(): value for key, value in exc.headers.items()},
                body,
            )
        except (urllib.error.URLError, OSError) as exc:
            raise ReleaseError("tool", "http") from exc
        with response:
            try:
                body = response.read(max_bytes + 1)
            except OSError as read_exc:
                raise ReleaseError("tool", "http") from read_exc
            if len(body) > max_bytes:
                raise ReleaseError("tool", "http-body")
            return HttpResult(
                response.status,
                {key.lower(): value for key, value in response.headers.items()},
                body,
            )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="T078 controlled release stages (prepare|verify-handoff|publish)."
    )
    parser.add_argument(
        "--stage",
        required=True,
        choices=("prepare", "verify-handoff", "publish"),
    )
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--go")
    parser.add_argument("--cache-dir")
    parser.add_argument("--artifact-root")
    parser.add_argument("--bundle-dir")
    parser.add_argument("--expected-descriptor-sha256")
    parser.add_argument("--prepare-inputs", action="store_true")
    parser.add_argument("--approve-external", action="store_true")
    parser.add_argument("--package-preconditions-verified", action="store_true")
    parser.add_argument("--repo", default=str(REPO_ROOT))
    parser.add_argument("--git")
    parser.add_argument("--docker")
    return parser


def _validate_cli(args: argparse.Namespace) -> None:
    _source_sha(args.source_sha, "cli", "source-sha")
    if args.stage in ("prepare", "publish"):
        if not args.go or not args.cache_dir or not args.artifact_root:
            raise ReleaseError("cli", "required")
        for name, value in (
            ("go", args.go),
            ("cache-dir", args.cache_dir),
            ("artifact-root", args.artifact_root),
        ):
            if not Path(value).expanduser().is_absolute():
                raise ReleaseError("cli", name)
    if args.stage in ("verify-handoff", "publish"):
        if not args.bundle_dir or not args.expected_descriptor_sha256:
            raise ReleaseError("cli", "bundle")
        _sha256_hex(
            args.expected_descriptor_sha256, "cli", "expected-descriptor-sha"
        )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
        _validate_cli(args)
        release = Release(
            repo=Path(args.repo),
            git_path=args.git,
            docker_path=args.docker,
        )
        if args.stage == "prepare":
            return release.prepare(args)
        if args.stage == "verify-handoff":
            return release.verify_handoff(args)
        if args.stage == "publish":
            return release.publish(args)
        raise ReleaseError("cli", "stage")
    except ReleaseError as exc:
        print(f"ERROR: {exc.stage}/{exc.kind}", file=sys.stderr)
        print(f"result: fail stage={exc.stage} kind={exc.kind}")
        return 1
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
