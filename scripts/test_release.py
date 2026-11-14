"""Focused fake-only tests for the T078 release gates.

No Docker daemon, PostgreSQL, registry, GitHub API, or external network is
used.  Every command and HTTP exchange is injected.  The only local socket use
is a loopback stub that proves the absolute wall-deadline guard interrupts a
slow response body.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import importlib.util
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "release.py"

SPEC = importlib.util.spec_from_file_location("release", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)

BUILDER_SCRIPT = REPO / "scripts" / "build-images.py"
BUILDER_SPEC = importlib.util.spec_from_file_location("build_images", BUILDER_SCRIPT)
build_images = importlib.util.module_from_spec(BUILDER_SPEC)
sys.modules[BUILDER_SPEC.name] = build_images
BUILDER_SPEC.loader.exec_module(build_images)

SOURCE_SHA = "a" * 40
MANIFEST_DIGEST = "b" * 64
APP_REF = "payment-hub-app:phase6-test"
MIGRATE_REF = "payment-hub-migrate:phase6-test"
GITHUB_TOKEN = "FAKE-github-token-canary-not-real"
AUDIT_TOKEN = "FAKE-audit-token-canary-not-real"
SCOPED_TOKEN = "FAKE-scoped-token-canary-not-real"
APP_PACKAGE_ID = 101
MIGRATE_PACKAGE_ID = 102


def package_metadata(
    role: str,
    *,
    package_id: int | None = None,
    visibility: str = "private",
    repository: Any = None,
    owner: Any = None,
    name: str | None = None,
    package_type: str = "container",
    repository_private: Any = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": package_id
        if package_id is not None
        else (APP_PACKAGE_ID if role == "app" else MIGRATE_PACKAGE_ID),
        "name": release.PACKAGE_NAMES[role] if name is None else name,
        "package_type": package_type,
        "visibility": visibility,
        "owner": {"login": "xtianxx"} if owner is None else owner,
        "repository": (
            {"full_name": release.REPOSITORY}
            if repository is None
            else repository
        ),
    }
    if repository_private is not None:
        value["repository"]["private"] = repository_private
    return value


def foreign_package(index: int) -> dict[str, Any]:
    return {
        "id": 100000 + index,
        "name": f"unrelated-package-{index}",
        "package_type": "container",
        "visibility": "private",
        "owner": {"login": "xtianxx"},
        "repository": {"full_name": release.REPOSITORY},
    }


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


class Pair:
    def __init__(self, app_id: str, migrate_id: str, root_bytes: dict[str, bytes], tar_path: Path):
        self.app_id = app_id
        self.migrate_id = migrate_id
        self.root_bytes = root_bytes
        self.tar_path = tar_path


def make_root(
    role: str,
    *,
    runtime_count: int = 1,
    unknown_descriptor: bool = False,
) -> tuple[bytes, str, int, str, str]:
    runtime_bytes = ("runtime-" + role).encode("utf-8")
    runtime_digest = "sha256:" + sha256(runtime_bytes)
    runtime_size = len(runtime_bytes)
    attestation_bytes = ("attestation-" + role).encode("utf-8")
    attestation_digest = "sha256:" + sha256(attestation_bytes)
    manifests: list[dict[str, Any]] = []
    for _ in range(runtime_count):
        manifests.append(
            {
                "mediaType": release.OCI_MANIFEST,
                "digest": runtime_digest,
                "size": runtime_size,
                "platform": {"architecture": "amd64", "os": "linux"},
            }
        )
    manifests.append(
        {
            "mediaType": release.OCI_MANIFEST,
            "digest": attestation_digest,
            "size": len(attestation_bytes),
            "annotations": {
                release.ATTESTATION_TYPE: "attestation-manifest",
                release.ATTESTATION_DIGEST: runtime_digest,
            },
            "platform": {"architecture": "unknown", "os": "unknown"},
        }
    )
    if unknown_descriptor:
        manifests.append(
            {
                "mediaType": release.OCI_MANIFEST,
                "digest": "sha256:" + "f" * 64,
                "size": 1,
                "platform": {"architecture": "amd64", "os": "linux"},
            }
        )
    root = {
        "schemaVersion": 2,
        "mediaType": release.OCI_INDEX,
        "manifests": manifests,
    }
    root_bytes = json_bytes(root)
    root_digest = "sha256:" + sha256(root_bytes)
    return root_bytes, root_digest, len(root_bytes), runtime_digest, attestation_digest


def make_pair(path: Path, *, runtime_count: int = 1, unknown_descriptor: bool = False) -> Pair:
    app_root, app_id, app_size, app_runtime, app_attestation = make_root(
        "app", runtime_count=runtime_count, unknown_descriptor=unknown_descriptor
    )
    migrate_root, migrate_id, migrate_size, migrate_runtime, migrate_attestation = make_root(
        "migrate", runtime_count=runtime_count, unknown_descriptor=unknown_descriptor
    )
    index = {
        "schemaVersion": 2,
        "mediaType": release.OCI_INDEX,
        "manifests": [
            {
                "mediaType": release.OCI_INDEX,
                "digest": app_id,
                "size": app_size,
                "annotations": {
                    "io.containerd.image.name": "docker.io/library/" + APP_REF,
                    "org.opencontainers.image.ref.name": "phase6-test",
                },
            },
            {
                "mediaType": release.OCI_INDEX,
                "digest": migrate_id,
                "size": migrate_size,
                "annotations": {
                    "io.containerd.image.name": "docker.io/library/" + MIGRATE_REF,
                    "org.opencontainers.image.ref.name": "phase6-test",
                },
            },
        ],
    }
    layout = {"imageLayoutVersion": "1.0.0"}
    manifest = [
        {"Config": "blobs/sha256/" + "1" * 64, "RepoTags": [APP_REF], "Layers": []},
        {"Config": "blobs/sha256/" + "2" * 64, "RepoTags": [MIGRATE_REF], "Layers": []},
    ]
    blobs = {
        app_id.split(":", 1)[1]: app_root,
        migrate_id.split(":", 1)[1]: migrate_root,
        app_runtime.split(":", 1)[1]: b"runtime-app",
        app_attestation.split(":", 1)[1]: b"attestation-app",
        migrate_runtime.split(":", 1)[1]: b"runtime-migrate",
        migrate_attestation.split(":", 1)[1]: b"attestation-migrate",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, mode="w:") as archive:
        def add(name: str, data: bytes, mode: int = 0o444) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))

        for directory in ("blobs", "blobs/sha256"):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = 0
            archive.addfile(info)
        add("index.json", json_bytes(index), 0o644)
        add("manifest.json", json_bytes(manifest), 0o644)
        add("oci-layout", json_bytes(layout), 0o444)
        for name, data in blobs.items():
            add("blobs/sha256/" + name, data)
    return Pair(
        app_id=app_id,
        migrate_id=migrate_id,
        root_bytes={app_id: app_root, migrate_id: migrate_root},
        tar_path=path,
    )


def build_record_bytes(pair: Pair) -> bytes:
    return json_bytes(
        {
            "schema": release.BUILD_RECORD_SCHEMA,
            "source_sha": SOURCE_SHA,
            "source_dirty": False,
            "git_head": SOURCE_SHA,
            "source_manifest_digest": MANIFEST_DIGEST,
            "platform": release.PLATFORM,
            "app_image": APP_REF,
            "app_image_id": pair.app_id,
            "migrate_image": MIGRATE_REF,
            "migrate_image_id": pair.migrate_id,
            "utc": "2026-11-12T00:00:00+00:00",
            "build_once_per_image": True,
        }
    )


def accepted_bytes(pair: Pair) -> bytes:
    return json_bytes(
        {
            "schema": release.IMAGES_SCHEMA,
            "generated_at": "2026-11-12T00:00:00+00:00",
            "run_id": "test-run",
            "platform": release.PLATFORM,
            "result": "pass",
            "release_candidate": True,
            "source": {
                "manifest_digest": MANIFEST_DIGEST,
                "manifest_file_count": 1,
                "manifest": [{"path": "x", "sha256": MANIFEST_DIGEST}],
                "unchanged": True,
            },
            "build_record": {
                "schema": release.BUILD_RECORD_SCHEMA,
                "verified": True,
            },
            "images": {
                "app": {
                    "ref": APP_REF,
                    "id": pair.app_id,
                    "platform": release.PLATFORM,
                    "source_label_match": True,
                    "registry_digest": None,
                    "registry_digest_status": "not_obtained",
                    "local_image_id_is_registry_digest": False,
                },
                "migrate": {
                    "ref": MIGRATE_REF,
                    "id": pair.migrate_id,
                    "platform": release.PLATFORM,
                    "source_label_match": True,
                    "registry_digest": None,
                    "registry_digest_status": "not_obtained",
                    "local_image_id_is_registry_digest": False,
                },
            },
            "pair": {
                "same_source": True,
                "same_platform": True,
                "distinct_ids": True,
                "ids_unchanged_after_run": True,
            },
            "go_test": {
                "result": "pass",
                "package_result": "pass",
                "exit_code": 0,
                "timed_out": False,
            },
            "cleanup": {"result": "pass", "errors": []},
        }
    )


def descriptor_value(pair: Pair, tar_path: Path, record: bytes, accepted: bytes) -> dict[str, Any]:
    tar_sha = sha256(tar_path.read_bytes())
    tar_size = tar_path.stat().st_size
    return {
        "schema": release.DESCRIPTOR_SCHEMA,
        "source_sha": SOURCE_SHA,
        "source_manifest_digest": MANIFEST_DIGEST,
        "platform": release.PLATFORM,
        "created_utc": "2026-11-12T00:00:00+00:00",
        "archive": {"file": release.PAIR_FILE, "sha256": tar_sha, "size": tar_size},
        "builder": {
            "build_record_sha256": sha256(record),
            "accepted_report_sha256": sha256(accepted),
            "accepted_report_file": release.ACCEPTED_BUNDLE_FILE,
        },
        "images": {
            "app": {
                "reference": APP_REF,
                "local_id": pair.app_id,
                "root_descriptor": {
                    "mediaType": release.OCI_INDEX,
                    "digest": pair.app_id,
                    "size": len(pair.root_bytes[pair.app_id]),
                },
                "source_label": MANIFEST_DIGEST,
                "platform": release.PLATFORM,
            },
            "migrate": {
                "reference": MIGRATE_REF,
                "local_id": pair.migrate_id,
                "root_descriptor": {
                    "mediaType": release.OCI_INDEX,
                    "digest": pair.migrate_id,
                    "size": len(pair.root_bytes[pair.migrate_id]),
                },
                "source_label": MANIFEST_DIGEST,
                "platform": release.PLATFORM,
            },
        },
        "targets": dict(release.TARGETS),
        "tag": release.TAG_PREFIX + SOURCE_SHA,
    }


def make_bundle(base: Path, pair: Pair | None = None, **kwargs: Any) -> tuple[Path, dict[str, Any]]:
    base.mkdir(parents=True, exist_ok=True)
    pair = pair or make_pair(base / release.PAIR_FILE)
    if pair.tar_path != base / release.PAIR_FILE:
        (base / release.PAIR_FILE).write_bytes(pair.tar_path.read_bytes())
    record = kwargs.get("record", build_record_bytes(pair))
    accepted = kwargs.get("accepted", accepted_bytes(pair))
    (base / release.BUILD_RECORD_FILE).write_bytes(record)
    (base / release.ACCEPTED_BUNDLE_FILE).write_bytes(accepted)
    descriptor = kwargs.get("descriptor")
    if descriptor is None:
        descriptor = descriptor_value(pair, base / release.PAIR_FILE, record, accepted)
    (base / release.DESCRIPTOR_FILE).write_bytes(json_bytes(descriptor))
    handoff = {
        "schema": release.HANDOFF_POINTER_SCHEMA,
        "source_sha": SOURCE_SHA,
        "descriptor_sha256": sha256((base / release.DESCRIPTOR_FILE).read_bytes()),
        "bundle": release.DEFAULT_BUNDLE,
        "tag": release.TAG_PREFIX + SOURCE_SHA,
    }
    (base.parent / release.HANDOFF_FILE).write_bytes(json_bytes(handoff))
    return base, descriptor


def arg(argv: list[str], name: str) -> str | None:
    try:
        return argv[argv.index(name) + 1]
    except ValueError:
        return None


def write_fake_git(path: Path) -> Path:
    path.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"rev-parse\" ]; then printf '%s\\n' \"" + SOURCE_SHA + "\"; exit 0; fi\n"
        "if [ \"$1\" = \"status\" ]; then exit 0; fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def write_fake_docker(path: Path) -> Path:
    script = r"""#!/usr/bin/env python3
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

command = sys.argv[1] if len(sys.argv) > 1 else ""
if command == "image" and len(sys.argv) > 2 and sys.argv[2] == "inspect":
    reference = sys.argv[-1]
    role = "app" if (
        reference == os.environ["FAKE_APP_ID"]
        or "payment-hub-app" in reference
    ) else "migrate"
    image_id = (
        os.environ["FAKE_APP_ID"]
        if role == "app"
        else os.environ["FAKE_MIGRATE_ID"]
    )
    size = int(
        os.environ["FAKE_APP_SIZE"]
        if role == "app"
        else os.environ["FAKE_MIGRATE_SIZE"]
    )
    print(json.dumps({
        "Id": image_id,
        "Os": "linux",
        "Architecture": "amd64",
        "Config": {
            "Labels": {os.environ["FAKE_SOURCE_LABEL"]: os.environ["FAKE_MANIFEST"]},
        },
        "Descriptor": {
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "digest": image_id,
            "size": size,
        },
    }))
    sys.exit(0)
if command == "load":
    sys.exit(0)
if command == "login":
    marker = os.environ.get("FAKE_LOGIN_MARKER")
    if marker:
        config = pathlib.Path(os.environ["DOCKER_CONFIG"])
        config.mkdir(parents=True, exist_ok=True)
        (config / "config.json").write_text("{}", encoding="utf-8")
        pathlib.Path(marker).write_text(str(config), encoding="utf-8")
        if os.environ.get("FAKE_LOGIN_SLEEP"):
            time.sleep(30)
    sys.exit(0)
if command == "tag":
    sys.exit(0)
if command == "push":
    pathlib.Path(os.environ["FAKE_PUSH_MARKER"]).write_text("push", encoding="utf-8")
    if os.environ.get("FAKE_PUSH_GRANDCHILD"):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        child = subprocess.Popen([
            sys.executable,
            "-c",
            "import os,time; os.close(1); os.close(2); time.sleep(30)",
        ])
        pathlib.Path(os.environ["FAKE_GRANDCHILD_PID"]).write_text(
            str(child.pid), encoding="utf-8"
        )
        time.sleep(0.1)
        sys.exit(0)
    digest = (
        os.environ["FAKE_APP_ID"]
        if "payment-hub-app" in sys.argv[-1]
        else os.environ["FAKE_MIGRATE_ID"]
    )
    print("digest: " + digest)
    sys.exit(0)
if command == "pull":
    sys.exit(0)
sys.exit(0)
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def wait_for_marker(marker: Path, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return True
        time.sleep(0.01)
    return False


def send_signal_when_marker(marker: Path, signum: int) -> threading.Thread:
    def sender() -> None:
        if not wait_for_marker(marker):
            return
        time.sleep(0.05)
        os.kill(os.getpid(), signum)

    thread = threading.Thread(target=sender, daemon=True)
    thread.start()
    return thread


def pid_running(pid: int) -> bool:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return False
    _, separator, rest = text.rpartition(")")
    if not separator:
        return False
    fields = rest.split()
    return bool(fields) and fields[0] != "Z"


def wait_pid_absent(pid: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_running(pid):
            return True
        time.sleep(0.02)
    return not pid_running(pid)


class FakeRunner:
    def __init__(self, pair: Pair, *, builder_fail: bool = False, dirty: bool = False):
        self.pair = pair
        self.builder_fail = builder_fail
        self.dirty = dirty
        self.calls: list[dict[str, Any]] = []
        self.builder_calls = 0
        self.save_calls = 0
        self.login_calls = 0
        self.push_calls: list[str] = []
        self.pull_calls: list[str] = []
        self.check_image_calls = 0
        self.login_input: str | None = None
        self.login_env: dict[str, str] | None = None
        self.after_pull_ids: dict[str, str] = {}
        self.push_fail_role: str | None = None
        self.push_cleanup_failed = False
        self.revalidation_fail = False
        self.revalidation_bad_report = False
        self.prepare_inputs_seen: list[list[str]] = []
        self.builder_mutator = None
        self.descriptor_mode: str | None = None
        self.local_root_override: dict[str, dict[str, Any]] = {}
        self.tag_override: dict[str, dict[str, Any]] = {}
        self.pull_root_override: dict[str, dict[str, Any]] = {}

    def _result(self, code: int = 0, stdout: str = "", stderr: str = "") -> release.ProcResult:
        return release.ProcResult(code, stdout, stderr, False, 0.0, False)

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float | None,
        input_text: str | None = None,
        reap_grace: float = 0.0,
    ) -> release.ProcResult:
        argv = list(argv)
        self.calls.append(
            {
                "argv": argv,
                "cwd": str(cwd),
                "env": dict(env),
                "timeout": timeout,
                "input_text": input_text,
            }
        )
        text = " ".join(argv)
        if "build-images.py" in text:
            self.builder_calls += 1
            if "--prepare-inputs" in argv:
                self.prepare_inputs_seen.append(argv)
            if self.builder_fail:
                return self._result(1)
            root = Path(arg(argv, "--artifact-root") or "")
            root.mkdir(parents=True, exist_ok=True)
            (root / release.BUILD_RECORD_FILE).write_bytes(build_record_bytes(self.pair))
            validation = root / "validation" / "run-1"
            validation.mkdir(parents=True, exist_ok=True)
            (validation / "images.json").write_bytes(accepted_bytes(self.pair))
            if self.builder_mutator is not None:
                self.builder_mutator(root)
            return self._result(0)
        if "check-db-foundation.py" in text:
            root = Path(arg(argv, "--artifact-root") or "")
            run = root / "run-1"
            run.mkdir(parents=True, exist_ok=True)
            (run / "db-foundation.json").write_text(
                json.dumps({"result": "pass", "cleanup": {"result": "pass"}}),
                encoding="utf-8",
            )
            return self._result(0)
        if "check-ci-cancellation.py" in text:
            report_dir = Path(arg(argv, "--report-dir") or "")
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / "cancellation-report.json").write_text(
                json.dumps({"result": "pass"}), encoding="utf-8"
            )
            return self._result(0)
        if "check-vulnerabilities.py" in text:
            root = Path(arg(argv, "--artifact-root") or "")
            root.mkdir(parents=True, exist_ok=True)
            (root / "vulnerability-report.json").write_text(
                json.dumps({"result": "pass"}), encoding="utf-8"
            )
            return self._result(0)
        if "check.py" in text:
            scope = arg(argv, "--scope")
            root = Path(arg(argv, "--artifact-root") or "")
            root.mkdir(parents=True, exist_ok=True)
            if scope == "image":
                self.check_image_calls += 1
                if self.revalidation_fail:
                    return self._result(1)
                report = (
                    {"result": "fail"}
                    if self.revalidation_bad_report
                    else {"result": "pass"}
                )
                checks = {
                    "schema": release.CHECK_SCHEMA,
                    "scope": "image",
                    "result": "pass" if not self.revalidation_bad_report else "fail",
                    "stages": {"image": {"status": "pass", "report": report}},
                }
                (root / "checks.json").write_text(json.dumps(checks), encoding="utf-8")
                return self._result(0 if not self.revalidation_bad_report else 1)
            checks = {
                "schema": release.CHECK_SCHEMA,
                "scope": scope,
                "result": "pass",
            }
            (root / "checks.json").write_text(json.dumps(checks), encoding="utf-8")
            return self._result(0)
        if "-m" in argv and "unittest" in argv:
            return self._result(0)
        if "check-setup.py" in text:
            return self._result(0)
        if "git" in Path(argv[0]).name:
            if "rev-parse" in argv:
                return self._result(0, SOURCE_SHA + "\n")
            if "status" in argv:
                return self._result(0, " M dirty.go\n" if self.dirty else "")
            return self._result(0)
        if Path(argv[0]).name in ("docker", "/fake/docker") or "docker" in Path(argv[0]).name:
            return self._docker(argv, env, input_text)
        if argv and argv[0].endswith("go") and "test" in argv:
            return self._result(0)
        return self._result(0)

    def _docker(
        self, argv: list[str], env: dict[str, str], input_text: str | None
    ) -> release.ProcResult:
        if len(argv) < 2:
            return self._result(0)
        command = argv[1]
        if command == "image" and len(argv) >= 3 and argv[2] == "inspect":
            reference = argv[-1]
            if reference == self.pair.app_id:
                role = "app"
                kind = "local"
            elif reference == self.pair.migrate_id:
                role = "migrate"
                kind = "local"
            else:
                role = "app" if "payment-hub-app" in reference else "migrate"
                if "@sha256:" in reference:
                    kind = "pull"
                elif reference.startswith("ghcr.io/") and ":sha-" in reference:
                    kind = "tag"
                else:
                    kind = "ref"
            image_id = (
                self.pair.app_id if role == "app" else self.pair.migrate_id
            )
            root = {
                "mediaType": release.OCI_INDEX,
                "digest": image_id,
                "size": len(self.pair.root_bytes[image_id]),
            }
            if kind in ("local", "ref") and role in self.local_root_override:
                root = dict(self.local_root_override[role])
            if kind == "tag" and role in self.tag_override:
                override = self.tag_override[role]
                image_id = override["id"]
                root = dict(override["root"])
            if kind == "pull" and role in self.pull_root_override:
                root = dict(self.pull_root_override[role])
            if "@sha256:" in reference and role in self.after_pull_ids:
                image_id = self.after_pull_ids[role]
                root = {
                    "mediaType": release.OCI_INDEX,
                    "digest": image_id,
                    "size": len(self.pair.root_bytes[
                        self.pair.app_id if role == "app" else self.pair.migrate_id
                    ]),
                }
            config: dict[str, Any] = {
                "Labels": {release.SOURCE_LABEL: MANIFEST_DIGEST}
            }
            descriptor: dict[str, Any] | None = dict(root)
            if self.descriptor_mode == "missing":
                descriptor = None
            elif self.descriptor_mode == "wrong-media":
                descriptor = {
                    **root,
                    "mediaType": release.OCI_MANIFEST,
                }
            elif self.descriptor_mode == "wrong-size":
                descriptor = {**root, "size": root["size"] + 1}
            elif self.descriptor_mode == "wrong-digest":
                descriptor = {
                    **root,
                    "digest": "sha256:" + "9" * 64,
                }
            elif self.descriptor_mode == "config-descriptor":
                config["Descriptor"] = dict(root)
                descriptor = None
            response: dict[str, Any] = {
                "Id": image_id,
                "Os": "linux",
                "Architecture": "amd64",
                "Config": config,
            }
            if descriptor is not None:
                response["Descriptor"] = descriptor
            return self._result(0, json.dumps(response))
        if command == "image" and len(argv) >= 3 and argv[2] == "save":
            self.save_calls += 1
            output = Path(arg(argv, "--output") or "")
            output.write_bytes((self._tar_source()).read_bytes())
            return self._result(0)
        if command == "load":
            return self._result(0)
        if command == "login":
            self.login_calls += 1
            self.login_input = input_text
            self.login_env = dict(env)
            return self._result(0)
        if command == "tag":
            return self._result(0)
        if command == "push":
            reference = argv[-1]
            role = "app" if "payment-hub-app" in reference else "migrate"
            self.push_calls.append(reference)
            if self.push_fail_role == role:
                return self._result(1, "", "push failed")
            digest = self.pair.app_id if role == "app" else self.pair.migrate_id
            return release.ProcResult(
                0,
                f"digest: {digest}\n",
                "",
                False,
                0.0,
                self.push_cleanup_failed,
            )
        if command == "pull":
            self.pull_calls.append(argv[-1])
            return self._result(0)
        if command == "pull" or command == "load":
            return self._result(0)
        return self._result(0)

    def _tar_source(self) -> Path:
        # The test stores the source tar beside the bundle; this is replaced by
        # the caller via tar_source.
        return self.tar_source


class FakeHTTP:
    """Fake GitHub audit API, GHCR registry, and per-package metadata."""

    def __init__(
        self,
        pair: Pair,
        *,
        package_failures: dict[str, list[Any]] | None = None,
        repository_private: Any = None,
    ):
        self.pair = pair
        self.calls: list[dict[str, Any]] = []
        self.package_failures = package_failures or {}
        self.package_counts: dict[str, int] = {"app": 0, "migrate": 0}
        self.manifest_header_override: dict[str, str] = {}
        self.manifest_body_override: dict[str, bytes] = {}
        self.runtime_override: dict[str, bytes] = {}
        self.block_package_role: str | None = None
        self.block_package_count: int | None = None
        self.block_marker: Path | None = None
        # Package model.  Tests mutate ``packages`` between inventory sessions
        # to model external disappearance, appearance, or ID replacement.
        self.packages: dict[str, dict[str, Any] | None] = {
            role: package_metadata(
                role, repository_private=repository_private
            )
            for role in release.ROLES
        }
        self.created_packages = {
            "app": package_metadata(
                "app", package_id=201, repository_private=repository_private
            ),
            "migrate": package_metadata(
                "migrate",
                package_id=202,
                repository_private=repository_private,
            ),
        }
        self.is_pushed = None
        self.package_metadata_after_push: dict[str, Any] = {}
        # Audit identity controls.
        self.identity_status = 200
        self.identity_login = "xtianxx"
        self.identity_scopes: str | None = "read:packages"
        # Inventory controls.
        self.inventory_session = 0
        self.inventory_calls: list[dict[str, Any]] = []
        self.inventory_page_overrides: dict[int, Any] = {}
        self.inventory_body_overrides: dict[int, bytes] = {}
        self.inventory_status_overrides: dict[int, int] = {}
        self.inventory_link_overrides: dict[int, str | None] = {}
        self.inventory_page_callback: Any = None
        self.block_inventory_session: int | None = None
        self.block_inventory_page: int | None = None
        self.block_inventory_marker: Path | None = None

    def _current_package(self, role: str) -> dict[str, Any] | None:
        if self.is_pushed is not None and self.is_pushed(role):
            if role in self.package_metadata_after_push:
                return self.package_metadata_after_push[role]
            return self.packages[role] or self.created_packages[role]
        return self.packages[role]

    def _inventory_items(self, page: int) -> list[dict[str, Any]]:
        if page in self.inventory_page_overrides:
            return self.inventory_page_overrides[page]
        items: list[dict[str, Any]] = []
        for role in release.ROLES:
            value = self._current_package(role)
            if value is not None:
                items.append(value)
        return items

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        max_bytes: int,
    ) -> release.HttpResult:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        if url == release.GITHUB_API + "/user":
            if self.identity_status != 200:
                return release.HttpResult(self.identity_status, {}, b"{}")
            response_headers: dict[str, str] = {}
            if self.identity_scopes is not None:
                response_headers["x-oauth-scopes"] = self.identity_scopes
            return release.HttpResult(
                200,
                response_headers,
                json_bytes({"login": self.identity_login}),
            )
        if url.startswith(release.GITHUB_API + "/user/packages?"):
            query = urllib.parse.parse_qs(
                urllib.parse.urlsplit(url).query
            )
            page = int(query.get("page", ["1"])[0])
            if page == 1:
                self.inventory_session += 1
            session = self.inventory_session
            self.inventory_calls.append(
                {"session": session, "page": page, "url": url}
            )
            if self.inventory_page_callback is not None:
                self.inventory_page_callback(session, page)
            if (
                self.block_inventory_session == session
                and self.block_inventory_page == page
            ):
                if self.block_inventory_marker is not None:
                    self.block_inventory_marker.write_text(
                        "blocked", encoding="utf-8"
                    )
                time.sleep(30)
            if page in self.inventory_status_overrides:
                return release.HttpResult(
                    self.inventory_status_overrides[page], {}, b"{}"
                )
            if page in self.inventory_body_overrides:
                body = self.inventory_body_overrides[page]
            else:
                body = json_bytes(self._inventory_items(page))
            response_headers = {}
            link = self.inventory_link_overrides.get(page)
            if link is not None:
                response_headers["link"] = link
            return release.HttpResult(200, response_headers, body)
        if url.startswith(release.GITHUB_API + "/user/packages/container/"):
            role = "app" if "payment-hub-app" in url else "migrate"
            self.package_counts[role] += 1
            if (
                self.block_package_role == role
                and self.block_package_count == self.package_counts[role]
            ):
                if self.block_marker is not None:
                    self.block_marker.write_text("blocked", encoding="utf-8")
                time.sleep(30)
            failures = self.package_failures.get(role, [])
            index = self.package_counts[role] - 1
            if index < len(failures):
                failure = failures[index]
                if isinstance(failure, int):
                    return release.HttpResult(failure, {}, b"{}")
                if isinstance(failure, dict):
                    return release.HttpResult(200, {}, json_bytes(failure))
            current = self._current_package(role)
            if current is None:
                return release.HttpResult(404, {}, b"{}")
            return release.HttpResult(200, {}, json_bytes(current))
        if url.startswith(release.TOKEN_REALM):
            return release.HttpResult(
                200, {}, json_bytes({"token": SCOPED_TOKEN})
            )
        if "/v2/" in url and "/manifests/" in url:
            role = "app" if "payment-hub-app" in url else "migrate"
            digest = url.rsplit("/", 1)[-1]
            if "Authorization" not in headers:
                scope = f"repository:xtianxx/payment-hub-{role}:pull"
                return release.HttpResult(
                    401,
                    {
                        "www-authenticate": (
                            'Bearer realm="https://ghcr.io/token",'
                            'service="ghcr.io",'
                            f'scope="{scope}"'
                        )
                    },
                    b"",
                )
            body = self.manifest_body_override.get(
                role, self.pair.root_bytes.get(digest, b"")
            )
            return release.HttpResult(
                200,
                {
                    "content-type": release.OCI_INDEX,
                    "docker-content-digest": self.manifest_header_override.get(
                        role, digest
                    ),
                },
                body,
            )
        return release.HttpResult(404, {}, b"{}")


class ReleaseTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="release-tests-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.pair = make_pair(self.base / "source-pair.tar")
        self.env = {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": SOURCE_SHA,
            "GITHUB_REPOSITORY": release.REPOSITORY,
            "GITHUB_ACTOR": "xtianxx",
            "GITHUB_TOKEN": GITHUB_TOKEN,
            "GHCR_PACKAGE_AUDIT_TOKEN": AUDIT_TOKEN,
            "PATH": os.environ.get("PATH", ""),
        }

    def prepare_args(self, root: Path, *, prepare_inputs: bool = False):
        return release._build_parser().parse_args(
            [
                "--stage",
                "prepare",
                "--source-sha",
                SOURCE_SHA,
                "--go",
                "/usr/bin/true",
                "--cache-dir",
                str(self.base / "cache"),
                "--artifact-root",
                str(root),
                *(["--prepare-inputs"] if prepare_inputs else []),
                "--repo",
                str(self.repo),
                "--git",
                "/fake/git",
                "--docker",
                "/fake/docker",
            ]
        )

    def publish_args(
        self,
        root: Path,
        bundle: Path,
        descriptor_sha: str,
        *,
        approve: bool = True,
        packages: bool = True,
        prepare_inputs: bool = False,
        bootstrap_packages: bool = False,
        git_path: str = "/fake/git",
        docker_path: str = "/fake/docker",
    ):
        values = [
            "--stage",
            "publish",
            "--source-sha",
            SOURCE_SHA,
            "--bundle-dir",
            str(bundle),
            "--expected-descriptor-sha256",
            descriptor_sha,
            "--go",
            "/usr/bin/true",
            "--cache-dir",
            str(self.base / "cache"),
            "--artifact-root",
            str(root),
            "--repo",
            str(self.repo),
            "--git",
            git_path,
            "--docker",
            docker_path,
        ]
        if approve:
            values.append("--approve-external")
        if packages:
            values.append("--package-preconditions-verified")
        if prepare_inputs:
            values.append("--prepare-inputs")
        if bootstrap_packages:
            values.append("--bootstrap-packages")
        return release._build_parser().parse_args(values)

    def verify_args(self, bundle: Path, expected: str):
        return release._build_parser().parse_args(
            [
                "--stage",
                "verify-handoff",
                "--source-sha",
                SOURCE_SHA,
                "--bundle-dir",
                str(bundle),
                "--expected-descriptor-sha256",
                expected,
            ]
        )

    def run_prepare(self, *, prepare_inputs: bool = False, fake: FakeRunner | None = None):
        root = self.base / "prepare-root"
        fake = fake or FakeRunner(self.pair)
        fake.tar_source = self.base / "source-pair.tar"
        instance = release.Release(
            repo=self.repo,
            runner=fake,
            delegated_runner=fake,
            env=self.env,
            git_path="/fake/git",
            docker_path="/fake/docker",
        )
        code = instance.prepare(self.prepare_args(root, prepare_inputs=prepare_inputs))
        return code, root, fake

    def run_publish(
        self,
        root: Path,
        bundle: Path,
        descriptor_sha: str,
        fake: FakeRunner,
        http: FakeHTTP,
        **kwargs: Any,
    ):
        fake.tar_source = self.base / "source-pair.tar"
        instance = release.Release(
            repo=self.repo,
            runner=fake,
            delegated_runner=fake,
            http=http,
            env=self.env,
            git_path="/fake/git",
            docker_path="/fake/docker",
        )
        args = self.publish_args(root, bundle, descriptor_sha, **kwargs)
        return instance.publish(args)


class PrepareTests(ReleaseTestBase):
    def test_prepare_happy_path_writes_four_file_bundle_and_one_build_save(self):
        code, root, fake = self.run_prepare()
        self.assertEqual(code, 0)
        bundle = root / release.DEFAULT_BUNDLE
        self.assertEqual(
            {path.name for path in bundle.iterdir()},
            {
                release.PAIR_FILE,
                release.DESCRIPTOR_FILE,
                release.BUILD_RECORD_FILE,
                release.ACCEPTED_BUNDLE_FILE,
            },
        )
        self.assertEqual(fake.builder_calls, 1)
        self.assertEqual(fake.save_calls, 1)
        handoff = json.loads((root / release.HANDOFF_FILE).read_text())
        self.assertEqual(handoff["source_sha"], SOURCE_SHA)
        self.assertEqual(handoff["descriptor_sha256"], sha256((bundle / release.DESCRIPTOR_FILE).read_bytes()))
        summary = json.loads((root / release.SUMMARY_FILE).read_text())
        self.assertEqual(summary["outcome"], "pass")
        self.assertEqual(summary["builder"]["calls"], 1)
        self.assertEqual(summary["save"]["calls"], 1)
        self.assertFalse(summary["builder"]["fixture_tool_is_release_candidate"])

    def test_prepare_inputs_maps_to_existing_fixed_switches(self):
        code, root, fake = self.run_prepare(prepare_inputs=True)
        self.assertEqual(code, 0)
        text = "\n".join(" ".join(call["argv"]) for call in fake.calls)
        self.assertIn("--prepare-images", text)
        self.assertIn("--download-modules", text)
        self.assertIn("--prepare-tool", text)
        vulnerability_calls = [
            call for call in fake.calls if "check-vulnerabilities.py" in " ".join(call["argv"])
        ]
        self.assertEqual(len(vulnerability_calls), 1)
        self.assertEqual(
            arg(vulnerability_calls[0]["argv"], "--tool-dir"),
            str(self.base / "cache" / "tools" / "go1.27.4-govulncheck-v1.9.0"),
        )
        self.assertEqual(fake.builder_calls, 1)
        builder_calls = [call for call in fake.calls if "build-images.py" in " ".join(call["argv"])]
        self.assertEqual(len(builder_calls), 1)
        self.assertIn("--prepare-inputs", builder_calls[0]["argv"])
        summary = json.loads((root / release.SUMMARY_FILE).read_text())
        self.assertTrue(summary["builder"]["prepare_inputs"])

    def test_non_trusted_ref_fails_before_builder(self):
        self.env["GITHUB_REF"] = "refs/heads/feature"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_prepare()
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("context", "ref"))

    def test_non_workflow_dispatch_fails_before_builder(self):
        self.env["GITHUB_EVENT_NAME"] = "push"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_prepare()
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("context", "event"))

    def test_source_sha_mismatch_fails_before_builder(self):
        self.env["GITHUB_SHA"] = "c" * 40
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_prepare()
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("context", "sha"))

    def test_dirty_worktree_fails_before_builder(self):
        fake = FakeRunner(self.pair, dirty=True)
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_prepare(fake=fake)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("context", "dirty"))
        self.assertEqual(fake.builder_calls, 0)

    def test_builder_failure_is_fixed_and_no_save(self):
        fake = FakeRunner(self.pair, builder_fail=True)
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_prepare(fake=fake)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("builder", "failed"))
        self.assertEqual(fake.builder_calls, 1)
        self.assertEqual(fake.save_calls, 0)

    def test_builder_source_dirty_true_rejected(self):
        fake = FakeRunner(self.pair)
        def mutate(root: Path) -> None:
            record = json.loads((root / release.BUILD_RECORD_FILE).read_text())
            record["source_dirty"] = True
            (root / release.BUILD_RECORD_FILE).write_text(json.dumps(record))
        fake.builder_mutator = mutate
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_prepare(fake=fake)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("builder", "record-dirty"))

    def test_builder_source_sha_mismatch_rejected(self):
        fake = FakeRunner(self.pair)
        def mutate(root: Path) -> None:
            record = json.loads((root / release.BUILD_RECORD_FILE).read_text())
            record["source_sha"] = "c" * 40
            (root / release.BUILD_RECORD_FILE).write_text(json.dumps(record))
        fake.builder_mutator = mutate
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_prepare(fake=fake)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("builder", "record-source-sha"))

    def test_builder_dirty_alias_not_accepted(self):
        fake = FakeRunner(self.pair)
        def mutate(root: Path) -> None:
            record = json.loads((root / release.BUILD_RECORD_FILE).read_text())
            record.pop("source_dirty", None)
            record["dirty"] = False
            (root / release.BUILD_RECORD_FILE).write_text(json.dumps(record))
        fake.builder_mutator = mutate
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_prepare(fake=fake)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("builder", "record-dirty"))

    def test_builder_git_head_mismatch_rejected(self):
        fake = FakeRunner(self.pair)
        def mutate(root: Path) -> None:
            record = json.loads((root / release.BUILD_RECORD_FILE).read_text())
            record["git_head"] = "c" * 40
            (root / release.BUILD_RECORD_FILE).write_text(json.dumps(record))
        fake.builder_mutator = mutate
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_prepare(fake=fake)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("builder", "record-git-head"))

    def test_accepted_report_id_mismatch_rejected(self):
        fake = FakeRunner(self.pair)
        def mutate(root: Path) -> None:
            path = root / "validation" / "run-1" / "images.json"
            value = json.loads(path.read_text())
            value["images"]["app"]["id"] = "sha256:" + "9" * 64
            path.write_text(json.dumps(value))
        fake.builder_mutator = mutate
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_prepare(fake=fake)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("builder", "accepted-id"))

    def test_missing_accepted_report_rejected(self):
        fake = FakeRunner(self.pair)
        def mutate(root: Path) -> None:
            (root / "validation" / "run-1" / "images.json").unlink()
        fake.builder_mutator = mutate
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_prepare(fake=fake)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("builder", "accepted-report"))

    def test_missing_actual_descriptor_rejected(self):
        fake = FakeRunner(self.pair)
        fake.descriptor_mode = "missing"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_prepare(fake=fake)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("image", "descriptor-missing"))

    def test_wrong_actual_descriptor_media_type_rejected(self):
        fake = FakeRunner(self.pair)
        fake.descriptor_mode = "wrong-media"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_prepare(fake=fake)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("image", "descriptor-media-type"))

    def test_wrong_actual_descriptor_digest_rejected(self):
        fake = FakeRunner(self.pair)
        fake.descriptor_mode = "wrong-digest"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_prepare(fake=fake)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("image", "descriptor-id"))

    def test_config_nested_descriptor_only_is_not_accepted(self):
        fake = FakeRunner(self.pair)
        fake.descriptor_mode = "config-descriptor"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_prepare(fake=fake)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("image", "descriptor-missing"))

    def test_tar_root_mismatch_actual_inspect_rejected(self):
        fake = FakeRunner(self.pair)
        fake.local_root_override = {
            "app": {
                "mediaType": release.OCI_INDEX,
                "digest": self.pair.app_id,
                "size": len(self.pair.root_bytes[self.pair.app_id]) + 1,
            }
        }
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_prepare(fake=fake)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("tar", "actual-root"))

    def test_default_tool_dir_is_cache_not_fresh_artifacts(self):
        code, root, fake = self.run_prepare()
        self.assertEqual(code, 0)
        calls = [
            call
            for call in fake.calls
            if "check-vulnerabilities.py" in " ".join(call["argv"])
        ]
        self.assertEqual(len(calls), 1)
        tool_dir = arg(calls[0]["argv"], "--tool-dir")
        self.assertEqual(
            tool_dir,
            str(self.base / "cache" / "tools" / "go1.27.4-govulncheck-v1.9.0"),
        )
        self.assertFalse(tool_dir.startswith(str(root)))
        self.assertNotIn("--prepare-tool", calls[0]["argv"])

    def test_builder_record_cross_adapter(self):
        temp = self.base / "builder-seam"
        temp.mkdir()
        args = argparse.Namespace(
            go="/usr/bin/true",
            cache_dir=str(temp / "cache"),
            artifact_root=str(temp / "build"),
            prepare_inputs=False,
        )
        builder = build_images.ImageBuilder(args)
        builder.source_digest = MANIFEST_DIGEST
        builder.source_sha = SOURCE_SHA
        builder.source_dirty = False
        builder.git_head = SOURCE_SHA
        builder.built = {
            "app": {"ref": APP_REF, "id": self.pair.app_id},
            "migrate": {"ref": MIGRATE_REF, "id": self.pair.migrate_id},
        }
        (temp / "build").mkdir()
        builder.build_record_path = temp / "build" / "build-record.json"
        builder._write_build_record()
        record = json.loads(builder.build_record_path.read_text())
        self.assertIs(record["build_once_per_image"], True)
        release.Release(repo=self.repo)._validate_build_record(
            record, release.TrustedContext(SOURCE_SHA, self.repo)
        )


class VerifyHandoffTests(ReleaseTestBase):
    def test_valid_offline_bundle_passes(self):
        bundle, descriptor = make_bundle(self.base / "bundle")
        expected = sha256((bundle / release.DESCRIPTOR_FILE).read_bytes())
        instance = release.Release(repo=self.repo, env=self.env)
        self.assertEqual(instance.verify_handoff(self.verify_args(bundle, expected)), 0)

    def test_expected_descriptor_authority_not_archive_self_report(self):
        bundle, _ = make_bundle(self.base / "bundle")
        with self.assertRaises(release.ReleaseError) as caught:
            release.Release(repo=self.repo, env=self.env).verify_handoff(
                self.verify_args(bundle, "c" * 64)
            )
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("handoff", "descriptor-sha"))

    def test_tar_hash_mismatch_rejected(self):
        bundle, _ = make_bundle(self.base / "bundle")
        expected = sha256((bundle / release.DESCRIPTOR_FILE).read_bytes())
        with (bundle / release.PAIR_FILE).open("ab") as handle:
            handle.write(b"x")
        with self.assertRaises(release.ReleaseError) as caught:
            release.Release(repo=self.repo, env=self.env).verify_handoff(
                self.verify_args(bundle, expected)
            )
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("archive", "hash"))

    def test_extra_executable_rejected(self):
        bundle, _ = make_bundle(self.base / "bundle")
        expected = sha256((bundle / release.DESCRIPTOR_FILE).read_bytes())
        extra = bundle / "extra.sh"
        extra.write_text("#!/bin/sh\n")
        extra.chmod(0o755)
        with self.assertRaises(release.ReleaseError) as caught:
            release.Release(repo=self.repo, env=self.env).verify_handoff(
                self.verify_args(bundle, expected)
            )
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("bundle", "files"))

    def test_symlink_rejected(self):
        bundle, _ = make_bundle(self.base / "bundle")
        expected = sha256((bundle / release.DESCRIPTOR_FILE).read_bytes())
        (bundle / release.BUILD_RECORD_FILE).unlink()
        (bundle / release.BUILD_RECORD_FILE).symlink_to(bundle / release.DESCRIPTOR_FILE)
        with self.assertRaises(release.ReleaseError) as caught:
            release.Release(repo=self.repo, env=self.env).verify_handoff(
                self.verify_args(bundle, expected)
            )
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("bundle", "file"))

    def test_missing_file_rejected(self):
        bundle, _ = make_bundle(self.base / "bundle")
        expected = sha256((bundle / release.DESCRIPTOR_FILE).read_bytes())
        (bundle / release.BUILD_RECORD_FILE).unlink()
        with self.assertRaises(release.ReleaseError) as caught:
            release.Release(repo=self.repo, env=self.env).verify_handoff(
                self.verify_args(bundle, expected)
            )
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("bundle", "files"))

    def test_build_record_snapshot_hash_mismatch_rejected(self):
        bundle, _ = make_bundle(self.base / "bundle")
        expected = sha256((bundle / release.DESCRIPTOR_FILE).read_bytes())
        (bundle / release.BUILD_RECORD_FILE).write_bytes(b"{}")
        with self.assertRaises(release.ReleaseError) as caught:
            release.Release(repo=self.repo, env=self.env).verify_handoff(
                self.verify_args(bundle, expected)
            )
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("bundle", "build-record-hash"))

    def test_accepted_report_snapshot_hash_mismatch_rejected(self):
        bundle, _ = make_bundle(self.base / "bundle")
        expected = sha256((bundle / release.DESCRIPTOR_FILE).read_bytes())
        (bundle / release.ACCEPTED_BUNDLE_FILE).write_bytes(b"{}")
        with self.assertRaises(release.ReleaseError) as caught:
            release.Release(repo=self.repo, env=self.env).verify_handoff(
                self.verify_args(bundle, expected)
            )
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("bundle", "accepted-report-hash"))

    def test_root_digest_not_in_tar_rejected(self):
        bundle, descriptor = make_bundle(self.base / "bundle")
        descriptor["images"]["app"]["local_id"] = "sha256:" + "9" * 64
        descriptor["images"]["app"]["root_descriptor"]["digest"] = "sha256:" + "9" * 64
        (bundle / release.DESCRIPTOR_FILE).write_bytes(json_bytes(descriptor))
        expected = sha256((bundle / release.DESCRIPTOR_FILE).read_bytes())
        with self.assertRaises(release.ReleaseError) as caught:
            release.Release(repo=self.repo, env=self.env).verify_handoff(
                self.verify_args(bundle, expected)
            )
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("tar", "roots"))

    def test_runtime_count_not_one_rejected(self):
        bundle = self.base / "bundle"
        pair = make_pair(bundle / release.PAIR_FILE, runtime_count=2)
        make_bundle(bundle, pair=pair)
        expected = sha256((bundle / release.DESCRIPTOR_FILE).read_bytes())
        with self.assertRaises(release.ReleaseError) as caught:
            release.Release(repo=self.repo, env=self.env).verify_handoff(
                self.verify_args(bundle, expected)
            )
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("tar", "roots"))

    def test_unknown_root_descriptor_rejected(self):
        bundle = self.base / "bundle"
        pair = make_pair(bundle / release.PAIR_FILE, unknown_descriptor=True)
        make_bundle(bundle, pair=pair)
        expected = sha256((bundle / release.DESCRIPTOR_FILE).read_bytes())
        with self.assertRaises(release.ReleaseError) as caught:
            release.Release(repo=self.repo, env=self.env).verify_handoff(
                self.verify_args(bundle, expected)
            )
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("tar", "roots"))


class PublishTests(ReleaseTestBase):
    def setUp(self):
        super().setUp()
        self.bundle, self.descriptor = make_bundle(self.base / "bundle")
        self.descriptor_sha = sha256(
            (self.bundle / release.DESCRIPTOR_FILE).read_bytes()
        )
        self.fake = FakeRunner(self.pair)
        self.http = FakeHTTP(self.pair)

    def test_no_approval_fails_before_auth_or_network(self):
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(
                root, self.bundle, self.descriptor_sha, self.fake, self.http, approve=False
            )
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("approval", "external"))
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.fake.login_calls, 0)
        summary = json.loads((root / release.SUMMARY_FILE).read_text())
        self.assertFalse(summary["published"])
        self.assertEqual(summary["outcome"], "fail")

    def test_no_package_preconditions_fails_before_auth_or_network(self):
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(
                root, self.bundle, self.descriptor_sha, self.fake, self.http, packages=False
            )
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("approval", "package-preconditions"),
        )
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.fake.login_calls, 0)

    def test_missing_token_fails_before_network(self):
        self.env.pop("GITHUB_TOKEN")
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("auth", "token-missing"))
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.fake.login_calls, 0)

    def test_package_403_fails_before_push(self):
        self.http.package_failures = {"app": [403]}
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("package", "app-forbidden"))
        self.assertEqual(self.fake.push_calls, [])

    def test_package_404_fails_before_push(self):
        self.http.package_failures = {"app": [404]}
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("package", "app-not-found"))
        self.assertEqual(self.fake.push_calls, [])

    def test_public_package_fails_before_push(self):
        self.http.package_failures = {
            "app": [package_metadata("app", visibility="public")]
        }
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("package", "app-visibility"))
        self.assertEqual(self.fake.push_calls, [])

    def test_unlinked_package_fails_before_push(self):
        self.http.package_failures = {
            "app": [package_metadata("app", repository={})]
        }
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("package", "app-repository"))
        self.assertEqual(self.fake.push_calls, [])

    def test_second_metadata_recheck_failure_fails_before_push(self):
        self.http.package_failures = {"app": [None, 403]}
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("package", "app-forbidden"))
        self.assertEqual(self.fake.push_calls, [])

    def test_one_push_failure_is_not_published(self):
        self.fake.push_fail_role = "migrate"
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("push", "migrate-failed"))
        summary = json.loads((root / release.SUMMARY_FILE).read_text())
        self.assertFalse(summary["published"])
        self.assertEqual(summary["pushed_roles"], ["app"])
        self.assertEqual(
            summary["push_attempted_roles"], ["app", "migrate"]
        )
        self.assertEqual(
            summary["push_side_effects"],
            {"app": "verified", "migrate": "unknown"},
        )
        self.assertEqual(summary["pull"], {})
        self.assertIsNone(summary["revalidation"])

    def test_remote_header_digest_mismatch_rejected(self):
        self.http.manifest_header_override = {"app": "sha256:" + "9" * 64}
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("registry", "app-header-digest"))
        summary = json.loads((root / release.SUMMARY_FILE).read_text())
        self.assertFalse(summary["published"])
        self.assertEqual(summary["pushed_roles"], ["app"])
        self.assertEqual(summary["push_attempted_roles"], ["app"])
        self.assertEqual(summary["push_side_effects"], {"app": "unknown"})

    def test_remote_body_hash_mismatch_rejected(self):
        self.http.manifest_body_override = {"app": b"not-the-manifest"}
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("registry", "app-body-hash"))
        summary = json.loads((root / release.SUMMARY_FILE).read_text())
        self.assertFalse(summary["published"])

    def test_remote_runtime_mismatch_rejected(self):
        # Exercise the registry gate directly: the manifest body is valid and
        # hashes to the candidate, but the expected runtime differs.
        body = json_bytes(
            {
                "schemaVersion": 2,
                "mediaType": release.OCI_INDEX,
                "manifests": [
                    {
                        "mediaType": release.OCI_MANIFEST,
                        "digest": "sha256:" + "1" * 64,
                        "size": 1,
                        "platform": {"architecture": "amd64", "os": "linux"},
                    }
                ],
            }
        )
        digest = "sha256:" + sha256(body)
        http = mock.Mock()
        http.side_effect = [
            release.HttpResult(
                401,
                {
                    "www-authenticate": (
                        'Bearer realm="https://ghcr.io/token",'
                        'service="ghcr.io",'
                        'scope="repository:xtianxx/payment-hub-app:pull"'
                    )
                },
                b"",
            ),
            release.HttpResult(200, {}, json_bytes({"token": SCOPED_TOKEN})),
            release.HttpResult(
                200,
                {
                    "content-type": release.OCI_INDEX,
                    "docker-content-digest": digest,
                },
                body,
            ),
        ]
        instance = release.Release(repo=self.repo, http=http, env=self.env)
        with self.assertRaises(release.ReleaseError) as caught:
            instance._registry_manifest(
                "app",
                digest,
                {
                    "mediaType": release.OCI_INDEX,
                    "size": len(body),
                    "runtime_digest": "sha256:" + "2" * 64,
                    "runtime_size": 1,
                    "runtime_media_type": release.OCI_MANIFEST,
                    "attestations": (),
                },
                GITHUB_TOKEN,
                "xtianxx",
            )
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("registry", "app-runtime"))

    def test_pull_failure_is_not_published(self):
        original = self.fake._docker
        def handler(argv, env, input_text):
            if len(argv) >= 2 and argv[1] == "pull":
                return release.ProcResult(1, "", "pull failed", False, 0.0, False)
            return original(argv, env=env, input_text=input_text)
        self.fake._docker = handler  # type: ignore[method-assign]
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("pull", "app-failed"))
        summary = json.loads((root / release.SUMMARY_FILE).read_text())
        self.assertFalse(summary["published"])

    def test_pull_identity_change_is_not_published(self):
        self.fake.after_pull_ids = {"app": "sha256:" + "9" * 64}
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("pull", "app-id"))
        summary = json.loads((root / release.SUMMARY_FILE).read_text())
        self.assertFalse(summary["published"])

    def test_load_root_mismatch_rejected(self):
        self.fake.local_root_override = {
            "app": {
                "mediaType": release.OCI_INDEX,
                "digest": self.pair.app_id,
                "size": len(self.pair.root_bytes[self.pair.app_id]) + 1,
            }
        }
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("load", "app-root"))
        summary = json.loads((root / release.SUMMARY_FILE).read_text())
        self.assertFalse(summary["published"])

    def test_pull_root_mismatch_rejected(self):
        self.fake.pull_root_override = {
            "app": {
                "mediaType": release.OCI_INDEX,
                "digest": self.pair.app_id,
                "size": len(self.pair.root_bytes[self.pair.app_id]) + 1,
            }
        }
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("pull", "app-root"))
        summary = json.loads((root / release.SUMMARY_FILE).read_text())
        self.assertFalse(summary["published"])

    def test_push_pre_tag_replacement_zeroes_that_push(self):
        replaced = "sha256:" + "9" * 64
        self.fake.tag_override = {
            "app": {
                "id": replaced,
                "root": {
                    "mediaType": release.OCI_INDEX,
                    "digest": replaced,
                    "size": len(self.pair.root_bytes[self.pair.app_id]),
                },
            }
        }
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("push", "app-id"))
        self.assertEqual(self.fake.push_calls, [])
        summary = json.loads((root / release.SUMMARY_FILE).read_text())
        self.assertFalse(summary["published"])

    def test_revalidation_failure_is_not_published(self):
        self.fake.revalidation_fail = True
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("revalidation", "check-images"))
        summary = json.loads((root / release.SUMMARY_FILE).read_text())
        self.assertFalse(summary["published"])

    def test_revalidation_bad_report_is_not_published(self):
        self.fake.revalidation_bad_report = True
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("revalidation", "check-images"))

    def test_push_cleanup_failure_is_reported(self):
        self.fake.push_cleanup_failed = True
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("tool", "cleanup-failed"))
        summary = json.loads((root / release.SUMMARY_FILE).read_text())
        self.assertFalse(summary["published"])
        self.assertTrue(summary["cleanup_failed"])

    def test_push_failure_removes_credentials(self):
        self.fake.push_fail_role = "app"
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("push", "app-failed"))
        summary = json.loads((root / release.SUMMARY_FILE).read_text())
        self.assertTrue(summary["credentials_removed"])
        self.assertIsNotNone(self.fake.login_env)
        self.assertFalse(Path(self.fake.login_env["DOCKER_CONFIG"]).exists())
        self.assertNotIn(GITHUB_TOKEN, json.dumps(summary))

    def test_success_marks_published_and_removes_credentials(self):
        original_record = (self.bundle / release.BUILD_RECORD_FILE).read_bytes()
        root = self.base / "publish-root"
        code = self.run_publish(root, self.bundle, self.descriptor_sha, self.fake, self.http)
        self.assertEqual(code, 0)
        self.assertEqual(
            (self.bundle / release.BUILD_RECORD_FILE).read_bytes(),
            original_record,
        )
        check_calls = [
            call
            for call in self.fake.calls
            if "check.py" in " ".join(call["argv"])
            and "--scope" in call["argv"]
            and call["argv"][call["argv"].index("--scope") + 1] == "image"
        ]
        self.assertEqual(len(check_calls), 1)
        delivery_record = check_calls[0]["env"]["PAYMENT_HUB_IMAGE_BUILD_RECORD"]
        self.assertTrue(delivery_record.endswith("delivery/run-record.json"))
        self.assertNotEqual(
            delivery_record, str(self.bundle / release.BUILD_RECORD_FILE)
        )
        summary = json.loads((root / release.SUMMARY_FILE).read_text())
        self.assertTrue(summary["published"])
        self.assertEqual(summary["outcome"], "pass")
        self.assertTrue(summary["credentials_removed"])
        self.assertEqual(summary["push"]["app"]["candidate_digest"], self.pair.app_id)
        self.assertEqual(summary["remote"]["app"]["digest"], self.pair.app_id)
        self.assertEqual(summary["pull"]["app"]["id"], self.pair.app_id)
        self.assertEqual(summary["revalidation"]["result"], "pass")
        self.assertNotIn(GITHUB_TOKEN, json.dumps(summary))
        self.assertNotIn(SCOPED_TOKEN, json.dumps(summary))
        self.assertEqual(self.fake.login_input, GITHUB_TOKEN)
        self.assertIsNotNone(self.fake.login_env)
        auth_dir = Path(self.fake.login_env["DOCKER_CONFIG"])
        self.assertFalse(auth_dir.exists())
        for call in self.fake.calls:
            self.assertNotIn(GITHUB_TOKEN, " ".join(call["argv"]))

    def test_prepare_inputs_publish_does_not_call_builder(self):
        root = self.base / "publish-root"
        code = self.run_publish(
            root,
            self.bundle,
            self.descriptor_sha,
            self.fake,
            self.http,
            prepare_inputs=True,
        )
        self.assertEqual(code, 0)
        text = "\n".join(" ".join(call["argv"]) for call in self.fake.calls)
        self.assertNotIn("build-images.py", text)
        self.assertIn("check-setup.py", text)
        summary = json.loads((root / release.SUMMARY_FILE).read_text())
        self.assertTrue(summary["prepare_inputs"]["requested"])
        self.assertFalse(summary["prepare_inputs"]["business_tests_run"])

    def test_verify_handoff_failure_writes_failure_summary(self):
        root = self.base / "publish-root"
        with self.assertRaises(release.ReleaseError) as caught:
            self.run_publish(root, self.bundle, "c" * 64, self.fake, self.http)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("handoff", "descriptor-sha"))
        summary = json.loads((root / release.SUMMARY_FILE).read_text())
        self.assertFalse(summary["published"])
        self.assertEqual(summary["failure"], {"stage": "handoff", "kind": "descriptor-sha"})


class PackageInventoryTests(ReleaseTestBase):
    def setUp(self):
        super().setUp()
        self.bundle, self.descriptor = make_bundle(self.base / "bundle")
        self.descriptor_sha = sha256(
            (self.bundle / release.DESCRIPTOR_FILE).read_bytes()
        )
        self.fake = FakeRunner(self.pair)
        self.http = FakeHTTP(self.pair)
        self.http.is_pushed = lambda role: any(
            "payment-hub-" + role in reference
            for reference in self.fake.push_calls
        )

    def _publish(self, *, bootstrap: bool = False, name: str = "publish-root"):
        root = self.base / name
        code = self.run_publish(
            root,
            self.bundle,
            self.descriptor_sha,
            self.fake,
            self.http,
            bootstrap_packages=bootstrap,
        )
        return code, root

    def _summary(self, root: Path) -> dict[str, Any]:
        return json.loads((root / release.SUMMARY_FILE).read_text())

    def _assert_successful_pair_publish(self, summary: dict[str, Any]) -> None:
        self.assertTrue(summary["published"])
        self.assertEqual(len(self.fake.push_calls), 2)
        for role in release.ROLES:
            expected = (
                self.pair.app_id if role == "app" else self.pair.migrate_id
            )
            self.assertEqual(
                summary["push"][role]["candidate_digest"], expected
            )
            self.assertEqual(summary["remote"][role]["digest"], expected)
            self.assertEqual(summary["pull"][role]["id"], expected)
        self.assertEqual(summary["revalidation"]["result"], "pass")
        self.assertEqual(self.fake.login_input, GITHUB_TOKEN)
        self.assertNotEqual(self.fake.login_input, AUDIT_TOKEN)
        self.assertNotIn(AUDIT_TOKEN, json.dumps(summary))
        self.assertNotIn(GITHUB_TOKEN, json.dumps(summary))
        for call in self.fake.calls:
            self.assertNotIn(AUDIT_TOKEN, " ".join(call["argv"]))
            self.assertNotIn(GITHUB_TOKEN, " ".join(call["argv"]))
            self.assertNotIn(AUDIT_TOKEN, call["env"].values())
            self.assertNotIn(GITHUB_TOKEN, call["env"].values())
        for call in self.http.calls:
            if call["url"].startswith(release.GITHUB_API):
                self.assertEqual(
                    call["headers"].get("Authorization"),
                    "Bearer " + AUDIT_TOKEN,
                )

    def _rewire(self, *, repository_private: Any = None) -> None:
        self.fake = FakeRunner(self.pair)
        self.http = FakeHTTP(
            self.pair, repository_private=repository_private
        )
        self.http.is_pushed = lambda role: any(
            "payment-hub-" + role in reference
            for reference in self.fake.push_calls
        )

    def test_normal_existing_packages_success_with_separate_audit_token(self):
        code, root = self._publish()
        self.assertEqual(code, 0)
        summary = self._summary(root)
        self.assertTrue(summary["published"])
        self.assertFalse(summary["bootstrap_requested"])
        self.assertEqual(
            summary["packages_initial"],
            {
                "app": {"status": "EXISTING_VALID", "id": APP_PACKAGE_ID},
                "migrate": {
                    "status": "EXISTING_VALID",
                    "id": MIGRATE_PACKAGE_ID,
                },
            },
        )
        self.assertEqual(
            summary["packages_postcheck"]["app"]["id"], APP_PACKAGE_ID
        )
        self.assertEqual(
            summary["packages_postcheck"]["migrate"]["id"],
            MIGRATE_PACKAGE_ID,
        )
        self.assertEqual(
            summary["push_attempted_roles"], ["app", "migrate"]
        )
        self.assertEqual(
            summary["push_side_effects"],
            {"app": "verified", "migrate": "verified"},
        )
        self.assertEqual(summary["audit_scope"], "read:packages")
        # The audit token is used only for the fixed identity/inventory
        # endpoints, never for docker login or registry authentication.
        audit_urls = [call["url"] for call in self.http.calls]
        self.assertIn(release.GITHUB_API + "/user", audit_urls)
        self.assertTrue(
            any(
                url.startswith(
                    release.GITHUB_API + "/user/packages?package_type=container"
                )
                for url in audit_urls
            )
        )
        self.assertTrue(
            any(
                url.startswith(
                    release.GITHUB_API + "/user/packages/container/"
                )
                for url in audit_urls
            )
        )
        self.assertFalse(any("/users/" in url for url in audit_urls))
        for call in self.http.calls:
            if call["url"].startswith(release.GITHUB_API):
                self.assertEqual(
                    call["headers"].get("Authorization"),
                    "Bearer " + AUDIT_TOKEN,
                )
        self.assertEqual(self.fake.login_input, GITHUB_TOKEN)
        self.assertNotEqual(self.fake.login_input, AUDIT_TOKEN)
        self.assertNotIn(AUDIT_TOKEN, json.dumps(summary))
        self.assertNotIn(GITHUB_TOKEN, json.dumps(summary))
        self.assertNotIn(SCOPED_TOKEN, json.dumps(summary))
        for call in self.fake.calls:
            self.assertNotIn(AUDIT_TOKEN, " ".join(call["argv"]))
            self.assertNotIn(GITHUB_TOKEN, " ".join(call["argv"]))
            self.assertNotIn(AUDIT_TOKEN, call["env"].values())
            self.assertNotIn(GITHUB_TOKEN, call["env"].values())
        for path in root.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                self.assertNotIn(AUDIT_TOKEN.encode(), data)
                self.assertNotIn(GITHUB_TOKEN.encode(), data)

    def test_normal_private_packages_with_public_repository_succeed(self):
        self._rewire(repository_private=False)
        code, root = self._publish()
        self.assertEqual(code, 0)
        summary = self._summary(root)
        self._assert_successful_pair_publish(summary)
        self.assertEqual(
            summary["packages_initial"]["app"]["status"], "EXISTING_VALID"
        )
        self.assertEqual(
            summary["packages_postcheck"]["migrate"]["id"],
            MIGRATE_PACKAGE_ID,
        )

    def test_repository_private_is_not_a_package_visibility_gate(self):
        for label, value in (("false", False), ("true", True), ("missing", None)):
            with self.subTest(label=label):
                metadata = (
                    package_metadata("app")
                    if value is None
                    else package_metadata(
                        "app", repository_private=value
                    )
                )
                projection = release._validate_package_metadata(
                    metadata, "app"
                )
                self.assertEqual(projection["id"], APP_PACKAGE_ID)
                self.assertEqual(projection["visibility"], "private")
                self.assertEqual(
                    projection["repository"], release.REPOSITORY
                )

    def test_normal_missing_package_rejected_before_login_or_push(self):
        self.http.packages["app"] = None
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "app-absent"),
        )
        self.assertEqual(self.fake.login_calls, 0)
        self.assertEqual(self.fake.push_calls, [])
        summary = self._summary(self.base / "publish-root")
        self.assertEqual(
            summary["packages_initial"]["app"],
            {"status": "ABSENT_CONFIRMED", "id": None},
        )
        self.assertFalse(summary["bootstrap_requested"])

    def test_bootstrap_zero_existing_creates_both(self):
        self.http.packages["app"] = None
        self.http.packages["migrate"] = None
        code, root = self._publish(bootstrap=True)
        self.assertEqual(code, 0)
        summary = self._summary(root)
        self.assertTrue(summary["published"])
        self.assertTrue(summary["bootstrap_requested"])
        self.assertEqual(
            summary["packages_initial"],
            {
                "app": {"status": "ABSENT_CONFIRMED", "id": None},
                "migrate": {"status": "ABSENT_CONFIRMED", "id": None},
            },
        )
        self.assertEqual(
            summary["packages_postcheck"]["app"]["id"], 201
        )
        self.assertEqual(
            summary["packages_postcheck"]["migrate"]["id"], 202
        )
        self.assertEqual(self.fake.push_calls[0].count("payment-hub-app"), 1)
        self.assertNotIn("created_roles", summary)

    def test_bootstrap_all_missing_with_public_repository_continues_after_first_push(
        self,
    ):
        self._rewire(repository_private=False)
        self.http.packages["app"] = None
        self.http.packages["migrate"] = None
        code, root = self._publish(bootstrap=True)
        self.assertEqual(code, 0)
        summary = self._summary(root)
        self._assert_successful_pair_publish(summary)
        self.assertEqual(
            summary["packages_initial"],
            {
                "app": {"status": "ABSENT_CONFIRMED", "id": None},
                "migrate": {"status": "ABSENT_CONFIRMED", "id": None},
            },
        )
        self.assertEqual(summary["packages_postcheck"]["app"]["id"], 201)
        self.assertEqual(
            summary["packages_postcheck"]["migrate"]["id"], 202
        )

    def test_bootstrap_one_existing_creates_only_missing(self):
        self.http.packages["app"] = None
        code, root = self._publish(bootstrap=True)
        self.assertEqual(code, 0)
        summary = self._summary(root)
        self.assertEqual(
            summary["packages_initial"]["app"]["status"], "ABSENT_CONFIRMED"
        )
        self.assertEqual(
            summary["packages_initial"]["migrate"]["status"], "EXISTING_VALID"
        )
        self.assertEqual(
            summary["packages_initial"]["migrate"]["id"], MIGRATE_PACKAGE_ID
        )
        self.assertEqual(summary["packages_postcheck"]["app"]["id"], 201)

    def test_bootstrap_mixed_with_public_repository_succeeds(self):
        self._rewire(repository_private=False)
        self.http.packages["migrate"] = None
        code, root = self._publish(bootstrap=True)
        self.assertEqual(code, 0)
        summary = self._summary(root)
        self._assert_successful_pair_publish(summary)
        self.assertEqual(
            summary["packages_initial"]["app"]["status"], "EXISTING_VALID"
        )
        self.assertEqual(
            summary["packages_initial"]["migrate"]["status"],
            "ABSENT_CONFIRMED",
        )
        self.assertEqual(
            summary["packages_postcheck"]["migrate"]["id"], 202
        )

    def test_bootstrap_two_existing_is_allowed(self):
        code, root = self._publish(bootstrap=True)
        self.assertEqual(code, 0)
        summary = self._summary(root)
        self.assertTrue(summary["bootstrap_requested"])
        self.assertEqual(
            summary["packages_initial"]["app"]["id"], APP_PACKAGE_ID
        )
        self.assertEqual(
            summary["packages_initial"]["migrate"]["id"],
            MIGRATE_PACKAGE_ID,
        )

    def test_missing_audit_token_fails_before_any_child_or_network(self):
        self.env.pop("GHCR_PACKAGE_AUDIT_TOKEN")
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("auth", "audit-token-missing"),
        )
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.fake.calls, [])

    def test_audit_wrong_owner_rejected(self):
        self.http.identity_login = "someone-else"
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("audit", "owner"),
        )
        self.assertEqual(self.fake.login_calls, 0)
        self.assertEqual(self.fake.push_calls, [])

    def test_audit_scope_too_broad_rejected(self):
        self.http.identity_scopes = "read:packages, repo, write:packages"
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("audit", "scope"),
        )
        self.assertEqual(self.fake.push_calls, [])

    def test_audit_missing_scope_header_is_unknown_not_trusted(self):
        self.http.identity_scopes = None
        code, root = self._publish()
        self.assertEqual(code, 0)
        self.assertEqual(self._summary(root)["audit_scope"], "unknown")

    def test_inventory_403_and_404_are_unknown_rejected(self):
        for status, kind in ((403, "inventory-forbidden"), (404, "inventory-http")):
            with self.subTest(status=status):
                self._rewire()
                self.http.inventory_status_overrides = {1: status}
                with self.assertRaises(release.ReleaseError) as caught:
                    self._publish(name=f"root-{status}")
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    ("package", kind),
                )
                self.assertEqual(self.fake.push_calls, [])

    def test_invalid_inventory_metadata_rejected(self):
        cases = [
            (
                "public",
                package_metadata("app", visibility="public"),
                "app-visibility",
            ),
            (
                "unlinked",
                package_metadata("app", repository={}),
                "app-repository",
            ),
            (
                "wrong-repository",
                package_metadata(
                    "app",
                    repository={
                        "full_name": "other/repository",
                        "private": False,
                    },
                ),
                "app-repository",
            ),
            (
                "public-with-public-repository",
                package_metadata(
                    "app", visibility="public", repository_private=False
                ),
                "app-visibility",
            ),
            (
                "wrong-owner",
                package_metadata("app", owner={"login": "other"}),
                "app-owner",
            ),
            (
                "wrong-type",
                package_metadata("app", package_type="npm"),
                "app-type",
            ),
            (
                "zero-id",
                package_metadata("app", package_id=0),
                "app-id",
            ),
        ]
        for label, metadata, kind in cases:
            with self.subTest(label=label):
                self._rewire()
                self.http.packages["app"] = metadata
                with self.assertRaises(release.ReleaseError) as caught:
                    self._publish(name=f"root-{label}")
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    ("package", kind),
                )
                self.assertEqual(self.fake.push_calls, [])

    def test_direct_metadata_name_mismatch_rejected(self):
        self.http.package_failures = {
            "app": [package_metadata("app", name="other-name")]
        }
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "app-name"),
        )
        self.assertEqual(self.fake.push_calls, [])

    def test_inventory_duplicate_target_rejected(self):
        self.http.inventory_page_overrides = {
            1: [package_metadata("app"), package_metadata("app")]
        }
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "inventory-duplicate"),
        )
        self.assertEqual(self.fake.push_calls, [])

    def test_inventory_malformed_page_rejected(self):
        self.http.inventory_body_overrides = {1: b'{"not":"a-list"}'}
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "inventory-json"),
        )
        self.assertEqual(self.fake.push_calls, [])

    def test_inventory_short_page_with_next_link_rejected(self):
        self.http.inventory_page_overrides = {1: [package_metadata("app")]}
        self.http.inventory_link_overrides = {
            1: (
                "<https://api.github.com/user/packages?"
                "package_type=container&per_page=100&page=2>; rel=\"next\""
            )
        }
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "inventory-incomplete"),
        )
        self.assertEqual(self.fake.push_calls, [])

    def test_link_header_malformed_fragment_after_valid_last_rejected(self):
        header = (
            "<https://api.github.com/user/packages?"
            "package_type=container&per_page=100&page=3>; rel=\"last\", "
            "malformed-next; rel=\"next\""
        )
        instance = release.Release(repo=self.repo)
        with self.assertRaises(release.ReleaseError) as caught:
            instance._inventory_next_page(header, 1)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "inventory-link"),
        )

    def test_link_header_first_and_last_terminal_page_accepted(self):
        header = (
            "<https://api.github.com/user/packages?"
            "package_type=container&per_page=100&page=1>; rel=\"first\", "
            "<https://api.github.com/user/packages?"
            "package_type=container&per_page=100&page=1>; rel=\"last\""
        )
        instance = release.Release(repo=self.repo)
        self.assertIsNone(instance._inventory_next_page(header, 1))

    def test_link_header_multiple_relations_with_next_accepted(self):
        header = (
            "<https://api.github.com/user/packages?"
            "package_type=container&per_page=100&page=2>; "
            "type=\"application/json\"; rel=\"next\", "
            "<https://api.github.com/user/packages?"
            "package_type=container&per_page=100&page=1>; rel=\"prev\", "
            "<https://api.github.com/user/packages?"
            "package_type=container&per_page=100&page=2>; rel=\"last\", "
            "<https://api.github.com/user/packages?"
            "package_type=container&per_page=100&page=1>; rel=\"first\""
        )
        instance = release.Release(repo=self.repo)
        self.assertEqual(instance._inventory_next_page(header, 1), 2)

    def test_link_header_malformed_variants_rejected(self):
        base = "https://api.github.com/user/packages"
        cases = [
            f"<{base}?package_type=container&per_page=100&page=2>; rel=\"next\" garbage",
            f"<{base}?package_type=container&per_page=100&page=2>",
            f"<{base}?package_type=container&per_page=100&page=2>; rel=\"next last\"",
            f"<{base}?package_type=container&per_page=100&page=2>; rel=\" next \"",
            f"<{base}?package_type=container&per_page=100&page=2>; rel=\"unknown\"",
            (
                f"<{base}?package_type=container&per_page=100&page=2>; "
                "rel=\"next\"; rel=\"last\""
            ),
            f"<{base}?package_type=container&per_page=100&page=2>; rel=\"next\",",
            f"<{base}?package_type=container&per_page=100&page=1>; rel=\"last\", "
            f"malformed; rel=\"next\"",
            f"<{base}?package_type=container&per_page=100&page=2>; rel=\"next\", "
            f"<{base}?package_type=container&per_page=100&page=2>; rel=\"next\"",
            (
                "<https://evil.example/user/packages?"
                "package_type=container&per_page=100&page=1>; rel=\"last\""
            ),
            (
                f"<{base}?package_type=container&per_page=50&page=1>; "
                "rel=\"last\""
            ),
            f"<{base}?package_type=container&per_page=100&page=zero>; rel=\"last\"",
        ]
        instance = release.Release(repo=self.repo)
        for header in cases:
            with self.subTest(header=header):
                with self.assertRaises(release.ReleaseError) as caught:
                    instance._inventory_next_page(header, 1)
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    ("package", "inventory-link"),
                )

    def test_malformed_link_stops_before_login_load_or_push(self):
        page_one = [foreign_package(index) for index in range(99)]
        page_one.append(package_metadata("app"))
        self.http.inventory_page_overrides = {1: page_one}
        self.http.inventory_link_overrides = {
            1: (
                "<https://api.github.com/user/packages?"
                "package_type=container&per_page=100&page=2>; rel=\"last\", "
                "malformed-next; rel=\"next\""
            )
        }
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "inventory-link"),
        )
        self.assertEqual(self.fake.login_calls, 0)
        self.assertEqual(self.fake.push_calls, [])
        load_calls = [
            call
            for call in self.fake.calls
            if len(call["argv"]) > 1 and call["argv"][1] == "load"
        ]
        self.assertEqual(load_calls, [])
        summary = self._summary(self.base / "publish-root")
        self.assertFalse(summary["published"])
        self.assertEqual(summary["packages_initial"], {})

    def test_link_header_last_beyond_current_without_next_rejected(self):
        header = (
            "<https://api.github.com/user/packages?"
            "package_type=container&per_page=100&page=3>; rel=\"last\""
        )
        instance = release.Release(repo=self.repo)
        with self.assertRaises(release.ReleaseError) as caught:
            instance._inventory_next_page(header, 1)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "inventory-link"),
        )

    def test_link_header_last_current_without_next_accepted(self):
        header = (
            "<https://api.github.com/user/packages?"
            "package_type=container&per_page=100&page=1>; rel=\"last\""
        )
        instance = release.Release(repo=self.repo)
        self.assertIsNone(instance._inventory_next_page(header, 1))

    def test_link_header_first_and_prev_terminal_page_accepted(self):
        header = (
            "<https://api.github.com/user/packages?"
            "package_type=container&per_page=100&page=1>; rel=\"first\", "
            "<https://api.github.com/user/packages?"
            "package_type=container&per_page=100&page=1>; rel=\"prev\""
        )
        instance = release.Release(repo=self.repo)
        self.assertIsNone(instance._inventory_next_page(header, 2))

    def test_link_header_last_before_next_rejected(self):
        header = (
            "<https://api.github.com/user/packages?"
            "package_type=container&per_page=100&page=2>; rel=\"next\", "
            "<https://api.github.com/user/packages?"
            "package_type=container&per_page=100&page=1>; rel=\"last\""
        )
        instance = release.Release(repo=self.repo)
        with self.assertRaises(release.ReleaseError) as caught:
            instance._inventory_next_page(header, 1)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "inventory-link"),
        )

    def test_last_beyond_current_without_next_stops_before_login_load_or_push(self):
        page_one = [foreign_package(index) for index in range(99)]
        page_one.append(package_metadata("app"))
        self.http.inventory_page_overrides = {1: page_one}
        self.http.inventory_link_overrides = {
            1: (
                "<https://api.github.com/user/packages?"
                "package_type=container&per_page=100&page=3>; rel=\"last\""
            )
        }
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "inventory-link"),
        )
        self.assertEqual(self.fake.login_calls, 0)
        self.assertEqual(self.fake.push_calls, [])
        load_calls = [
            call
            for call in self.fake.calls
            if len(call["argv"]) > 1 and call["argv"][1] == "load"
        ]
        self.assertEqual(load_calls, [])
        summary = self._summary(self.base / "publish-root")
        self.assertFalse(summary["published"])
        self.assertEqual(summary["packages_initial"], {})

    def test_last_declared_later_page_then_early_terminal_stops_before_login(self):
        page_one = [foreign_package(index) for index in range(99)]
        page_one.append(package_metadata("app"))
        self.http.inventory_page_overrides = {
            1: page_one,
            2: [package_metadata("migrate")],
        }
        self.http.inventory_link_overrides = {
            1: (
                "<https://api.github.com/user/packages?"
                "package_type=container&per_page=100&page=2>; rel=\"next\", "
                "<https://api.github.com/user/packages?"
                "package_type=container&per_page=100&page=3>; rel=\"last\""
            )
        }
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "inventory-link"),
        )
        self.assertEqual(self.fake.login_calls, 0)
        self.assertEqual(self.fake.push_calls, [])
        load_calls = [
            call
            for call in self.fake.calls
            if len(call["argv"]) > 1 and call["argv"][1] == "load"
        ]
        self.assertEqual(load_calls, [])
        summary = self._summary(self.base / "publish-root")
        self.assertFalse(summary["published"])
        self.assertEqual(summary["packages_initial"], {})

    def test_inventory_last_page_change_between_pages_rejected(self):
        page_one = [foreign_package(index) for index in range(99)]
        page_one.append(package_metadata("app"))
        self.http.inventory_page_overrides = {
            1: page_one,
            2: [package_metadata("migrate")],
        }
        self.http.inventory_link_overrides = {
            1: (
                "<https://api.github.com/user/packages?"
                "package_type=container&per_page=100&page=2>; rel=\"next\", "
                "<https://api.github.com/user/packages?"
                "package_type=container&per_page=100&page=3>; rel=\"last\""
            ),
            2: (
                "<https://api.github.com/user/packages?"
                "package_type=container&per_page=100&page=3>; rel=\"next\", "
                "<https://api.github.com/user/packages?"
                "package_type=container&per_page=100&page=4>; rel=\"last\""
            ),
        }
        instance = release.Release(
            repo=self.repo, http=self.http, env=self.env
        )
        with self.assertRaises(release.ReleaseError) as caught:
            instance._list_owner_packages(AUDIT_TOKEN)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "inventory-link"),
        )

    def test_inventory_second_page_target_is_found(self):
        page_one = [foreign_package(index) for index in range(99)]
        page_one.append(package_metadata("app"))
        self.http.inventory_page_overrides = {
            1: page_one,
            2: [package_metadata("migrate")],
        }
        self.http.inventory_link_overrides = {
            1: (
                "<https://api.github.com/user/packages?"
                "package_type=container&per_page=100&page=2>; rel=\"next\", "
                "<https://api.github.com/user/packages?"
                "package_type=container&per_page=100&page=2>; rel=\"last\", "
                "<https://api.github.com/user/packages?"
                "package_type=container&per_page=100&page=1>; rel=\"first\""
            ),
            2: (
                "<https://api.github.com/user/packages?"
                "package_type=container&per_page=100&page=1>; rel=\"prev\", "
                "<https://api.github.com/user/packages?"
                "package_type=container&per_page=100&page=1>; rel=\"first\", "
                "<https://api.github.com/user/packages?"
                "package_type=container&per_page=100&page=2>; rel=\"last\""
            ),
        }
        code, root = self._publish()
        self.assertEqual(code, 0)
        self.assertEqual(
            [(call["session"], call["page"]) for call in self.http.inventory_calls][
                :2
            ],
            [(1, 1), (1, 2)],
        )
        summary = self._summary(root)
        self.assertEqual(
            summary["packages_initial"]["migrate"]["id"],
            MIGRATE_PACKAGE_ID,
        )
        self.assertNotIn("unrelated-package", json.dumps(summary))

    def test_inventory_second_page_error_rejected(self):
        page_one = [foreign_package(index) for index in range(99)]
        page_one.append(package_metadata("app"))
        self.http.inventory_page_overrides = {1: page_one}
        self.http.inventory_link_overrides = {
            1: (
                "<https://api.github.com/user/packages?"
                "package_type=container&per_page=100&page=2>; rel=\"next\""
            )
        }
        self.http.inventory_status_overrides = {2: 500}
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "inventory-http"),
        )
        self.assertEqual(self.fake.push_calls, [])

    def test_inventory_foreign_next_link_rejected(self):
        page_one = [foreign_package(index) for index in range(99)]
        page_one.append(package_metadata("app"))
        self.http.inventory_page_overrides = {1: page_one}
        self.http.inventory_link_overrides = {
            1: (
                "<https://evil.example/user/packages?"
                "package_type=container&per_page=100&page=2>; rel=\"next\""
            )
        }
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "inventory-link"),
        )
        self.assertEqual(self.fake.push_calls, [])

    def test_inventory_page_limit_rejected(self):
        page_one = [foreign_package(index) for index in range(99)]
        page_one.append(package_metadata("app"))
        self.http.inventory_page_overrides = {1: page_one}
        self.http.inventory_link_overrides = {
            1: (
                "<https://api.github.com/user/packages?"
                "package_type=container&per_page=100&page=2>; rel=\"next\""
            )
        }
        with mock.patch.object(release, "INVENTORY_MAX_PAGES", 1):
            with self.assertRaises(release.ReleaseError) as caught:
                self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "inventory-pages"),
        )

    def test_inventory_deadline_rejected(self):
        with mock.patch.object(release, "INVENTORY_TIMEOUT_SECONDS", -1.0):
            with self.assertRaises(release.ReleaseError) as caught:
                self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "inventory-deadline"),
        )
        self.assertEqual(self.fake.push_calls, [])

    def test_existing_package_disappears_before_push_rejected(self):
        def mutate(session: int, page: int) -> None:
            if session == 2:
                self.http.packages["app"] = None

        self.http.inventory_page_callback = mutate
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "app-missing"),
        )
        self.assertEqual(self.fake.push_calls, [])

    def test_bootstrap_existing_disappearance_is_not_downgraded_to_create(self):
        self.http.packages["migrate"] = None

        def mutate(session: int, page: int) -> None:
            if session == 2:
                self.http.packages["app"] = None

        self.http.inventory_page_callback = mutate
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish(bootstrap=True)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "app-missing"),
        )
        self.assertEqual(self.fake.push_calls, [])

    def test_absent_package_appears_before_push_rejected(self):
        self.http.packages["app"] = None

        def mutate(session: int, page: int) -> None:
            if session == 2:
                self.http.packages["app"] = package_metadata(
                    "app", package_id=999
                )

        self.http.inventory_page_callback = mutate
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish(bootstrap=True)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "app-appeared"),
        )
        self.assertEqual(self.fake.push_calls, [])

    def test_existing_package_id_changes_before_push_rejected(self):
        def mutate(session: int, page: int) -> None:
            if session == 2:
                self.http.packages["app"] = package_metadata(
                    "app", package_id=999
                )

        self.http.inventory_page_callback = mutate
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "app-changed"),
        )
        self.assertEqual(self.fake.push_calls, [])

    def test_existing_package_metadata_changes_before_push_rejected(self):
        def mutate(session: int, page: int) -> None:
            if session == 2:
                self.http.packages["app"] = package_metadata(
                    "app", visibility="public"
                )

        self.http.inventory_page_callback = mutate
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "app-visibility"),
        )
        self.assertEqual(self.fake.push_calls, [])

    def test_postpush_bad_metadata_stops_before_second_push(self):
        self.http.package_metadata_after_push = {
            "app": package_metadata("app", visibility="public")
        }
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "app-visibility"),
        )
        self.assertEqual(len(self.fake.push_calls), 1)
        summary = self._summary(self.base / "publish-root")
        self.assertEqual(summary["push_attempted_roles"], ["app"])
        self.assertEqual(summary["push_side_effects"], {"app": "unknown"})
        self.assertEqual(summary["pushed_roles"], [])
        self.assertEqual(summary["pull"], {})
        self.assertIsNone(summary["revalidation"])

    def test_postpush_wrong_repository_stops_before_second_push(self):
        self.http.package_metadata_after_push = {
            "app": package_metadata(
                "app",
                repository={
                    "full_name": "other/repository",
                    "private": False,
                },
            )
        }
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "app-repository"),
        )
        self.assertEqual(len(self.fake.push_calls), 1)
        summary = self._summary(self.base / "publish-root")
        self.assertEqual(summary["push_attempted_roles"], ["app"])
        self.assertEqual(summary["push_side_effects"], {"app": "unknown"})
        self.assertEqual(summary["pushed_roles"], [])
        self.assertEqual(summary["pull"], {})
        self.assertIsNone(summary["revalidation"])

    def test_postpush_missing_metadata_stops_before_second_push(self):
        self.http.package_metadata_after_push = {"app": None}
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "app-not-found"),
        )
        self.assertEqual(len(self.fake.push_calls), 1)
        summary = self._summary(self.base / "publish-root")
        self.assertEqual(summary["push_attempted_roles"], ["app"])
        self.assertEqual(summary["push_side_effects"], {"app": "unknown"})
        self.assertEqual(summary["pull"], {})
        self.assertEqual(summary["pushed_roles"], [])

    def test_postpush_package_id_change_stops_before_second_push(self):
        self.http.package_metadata_after_push = {
            "app": package_metadata("app", package_id=999)
        }
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "app-changed"),
        )
        self.assertEqual(len(self.fake.push_calls), 1)
        summary = self._summary(self.base / "publish-root")
        self.assertEqual(summary["push_attempted_roles"], ["app"])
        self.assertEqual(summary["push_side_effects"], {"app": "unknown"})

    def test_partial_push_attempt_reports_unknown_without_retry(self):
        self.fake.push_fail_role = "app"
        with self.assertRaises(release.ReleaseError) as caught:
            self._publish()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("push", "app-failed"),
        )
        self.assertEqual(len(self.fake.push_calls), 1)
        summary = self._summary(self.base / "publish-root")
        self.assertEqual(summary["push_attempted_roles"], ["app"])
        self.assertEqual(summary["push_side_effects"], {"app": "unknown"})
        self.assertEqual(summary["pushed_roles"], [])
        self.assertEqual(summary["pull"], {})
        self.assertIsNone(summary["revalidation"])

    def test_inventory_cancellation_keeps_cancelled_semantics(self):
        marker = self.base / "inventory.blocked"
        self.http.block_inventory_session = 2
        self.http.block_inventory_page = 1
        self.http.block_inventory_marker = marker
        sender = send_signal_when_marker(marker, signal.SIGTERM)
        try:
            with self.assertRaises(release.ReleaseError) as caught:
                self._publish()
        finally:
            sender.join(timeout=3.0)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cancelled", "signal"),
        )
        self.assertEqual(self.fake.push_calls, [])
        summary = self._summary(self.base / "publish-root")
        self.assertFalse(summary["published"])
        self.assertEqual(summary["push_attempted_roles"], [])
        self.assertEqual(summary["push_side_effects"], {})

    def test_cli_rejects_bootstrap_flag_for_prepare_and_verify_handoff(self):
        prepare = self.prepare_args(self.base / "root")
        prepare.bootstrap_packages = True
        with self.assertRaises(release.ReleaseError) as caught:
            release._validate_cli(prepare)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cli", "bootstrap-stage"),
        )
        verify = self.verify_args(self.bundle, self.descriptor_sha)
        verify.bootstrap_packages = True
        with self.assertRaises(release.ReleaseError) as caught:
            release._validate_cli(verify)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cli", "bootstrap-stage"),
        )
        instance = release.Release(repo=self.repo, env=self.env)
        with self.assertRaises(release.ReleaseError) as caught:
            instance.prepare(prepare)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cli", "bootstrap-stage"),
        )
        with self.assertRaises(release.ReleaseError) as caught:
            instance.verify_handoff(verify)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cli", "bootstrap-stage"),
        )

    def test_prepare_never_passes_either_token_to_children(self):
        code, root, fake = self.run_prepare()
        self.assertEqual(code, 0)
        for call in fake.calls:
            self.assertNotIn(AUDIT_TOKEN, call["env"].values())
            self.assertNotIn(GITHUB_TOKEN, call["env"].values())
        self.assertNotIn(AUDIT_TOKEN, json.dumps(self._summary(root)))


class CancellationTests(ReleaseTestBase):
    def _docker_env(self, fake_docker: Path, **extra: str) -> dict[str, str]:
        env = dict(self.env)
        env.update(
            {
                "FAKE_APP_ID": self.pair.app_id,
                "FAKE_MIGRATE_ID": self.pair.migrate_id,
                "FAKE_APP_SIZE": str(len(self.pair.root_bytes[self.pair.app_id])),
                "FAKE_MIGRATE_SIZE": str(
                    len(self.pair.root_bytes[self.pair.migrate_id])
                ),
                "FAKE_SOURCE_LABEL": release.SOURCE_LABEL,
                "FAKE_MANIFEST": MANIFEST_DIGEST,
                "FAKE_DOCKER_PATH": str(fake_docker),
            }
        )
        env.update(extra)
        return env

    def _run_real_publish(
        self,
        root: Path,
        bundle: Path,
        descriptor_sha: str,
        fake_docker: Path,
        fake_git: Path,
        env: dict[str, str],
        http: FakeHTTP | None = None,
    ):
        instance = release.Release(
            repo=self.repo,
            http=http or FakeHTTP(self.pair),
            env=env,
            git_path=str(fake_git),
            docker_path=str(fake_docker),
        )
        args = self.publish_args(
            root,
            bundle,
            descriptor_sha,
            git_path=str(fake_git),
            docker_path=str(fake_docker),
        )
        return instance, args

    def test_login_sigterm_removes_credentials_and_does_not_push(self):
        temp = self.base / "login-signal"
        temp.mkdir()
        fake_docker = write_fake_docker(temp / "fake-docker")
        fake_git = write_fake_git(temp / "fake-git")
        auth_marker = temp / "login.started"
        push_marker = temp / "push.started"
        bundle, _ = make_bundle(self.base / "bundle-login")
        descriptor_sha = sha256((bundle / release.DESCRIPTOR_FILE).read_bytes())
        env = self._docker_env(
            fake_docker,
            FAKE_LOGIN_MARKER=str(auth_marker),
            FAKE_LOGIN_SLEEP="1",
            FAKE_PUSH_MARKER=str(push_marker),
        )
        instance, args = self._run_real_publish(
            temp / "root", bundle, descriptor_sha, fake_docker, fake_git, env
        )
        sender = send_signal_when_marker(auth_marker, signal.SIGTERM)
        try:
            with self.assertRaises(release.ReleaseError) as caught:
                instance.publish(args)
        finally:
            sender.join(timeout=3.0)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cancelled", "signal"),
        )
        self.assertFalse(push_marker.exists())
        summary = json.loads(
            (temp / "root" / release.SUMMARY_FILE).read_text()
        )
        self.assertFalse(summary["published"])
        self.assertTrue(summary["credentials_removed"])
        self.assertEqual(summary["push"], {})
        auth_dir = Path(auth_marker.read_text())
        self.assertFalse(auth_dir.exists())

    def test_http_sigterm_after_login_removes_credentials(self):
        temp = self.base / "http-signal"
        temp.mkdir()
        fake_docker = write_fake_docker(temp / "fake-docker")
        fake_git = write_fake_git(temp / "fake-git")
        auth_marker = temp / "login.started"
        push_marker = temp / "push.started"
        http_marker = temp / "http.started"
        bundle, _ = make_bundle(self.base / "bundle-http")
        descriptor_sha = sha256((bundle / release.DESCRIPTOR_FILE).read_bytes())
        env = self._docker_env(
            fake_docker,
            FAKE_LOGIN_MARKER=str(auth_marker),
            FAKE_PUSH_MARKER=str(push_marker),
        )
        http = FakeHTTP(self.pair)
        http.block_package_role = "app"
        http.block_package_count = 2
        http.block_marker = http_marker
        instance, args = self._run_real_publish(
            temp / "root", bundle, descriptor_sha, fake_docker, fake_git, env, http
        )
        sender = send_signal_when_marker(http_marker, signal.SIGTERM)
        try:
            with self.assertRaises(release.ReleaseError) as caught:
                instance.publish(args)
        finally:
            sender.join(timeout=3.0)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cancelled", "signal"),
        )
        self.assertFalse(push_marker.exists())
        summary = json.loads(
            (temp / "root" / release.SUMMARY_FILE).read_text()
        )
        self.assertFalse(summary["published"])
        self.assertTrue(summary["credentials_removed"])
        auth_dir = Path(auth_marker.read_text())
        self.assertFalse(auth_dir.exists())

    def test_push_sigint_leader_exit_grandchild_cleaned_and_not_published(self):
        temp = self.base / "push-signal"
        temp.mkdir()
        fake_docker = write_fake_docker(temp / "fake-docker")
        fake_git = write_fake_git(temp / "fake-git")
        push_marker = temp / "push.started"
        grandchild_pid = temp / "grandchild.pid"
        auth_marker = temp / "login.started"
        bundle, _ = make_bundle(self.base / "bundle-push")
        descriptor_sha = sha256((bundle / release.DESCRIPTOR_FILE).read_bytes())
        env = self._docker_env(
            fake_docker,
            FAKE_LOGIN_MARKER=str(auth_marker),
            FAKE_PUSH_MARKER=str(push_marker),
            FAKE_PUSH_GRANDCHILD="1",
            FAKE_GRANDCHILD_PID=str(grandchild_pid),
        )
        instance, args = self._run_real_publish(
            temp / "root", bundle, descriptor_sha, fake_docker, fake_git, env
        )
        sender = send_signal_when_marker(push_marker, signal.SIGINT)
        try:
            with self.assertRaises(release.ReleaseError) as caught:
                instance.publish(args)
        finally:
            sender.join(timeout=3.0)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cancelled", "signal"),
        )
        self.assertTrue(push_marker.exists())
        summary = json.loads(
            (temp / "root" / release.SUMMARY_FILE).read_text()
        )
        self.assertFalse(summary["published"])
        self.assertTrue(summary["credentials_removed"])
        self.assertEqual(summary["pull"], {})
        auth_dir = Path(auth_marker.read_text())
        self.assertFalse(auth_dir.exists())
        pid = int(grandchild_pid.read_text())
        deadline = time.monotonic() + 3.0
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.02)
        self.assertFalse(alive, "leftover push grandchild was not reaped")

    def test_delegated_child_signal_mask_has_sigint_sigterm_unblocked(self):
        code = (
            "import json,signal;"
            "print(json.dumps(sorted(int(s) for s in "
            "signal.pthread_sigmask(signal.SIG_BLOCK, []))))"
        )
        instance = release.Release(repo=self.repo, env=self.env)
        result = instance._run_delegated(
            [sys.executable, "-c", code], timeout=None
        )
        self.assertEqual(result.exit_code, 0)
        mask = set(json.loads(result.stdout))
        self.assertNotIn(int(signal.SIGINT), mask)
        self.assertNotIn(int(signal.SIGTERM), mask)

    def test_cancel_latch_set_during_popen_is_forwarded_to_child(self):
        instance = release.Release(repo=self.repo, env=self.env)
        real_popen = subprocess.Popen

        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            instance._cancel_signal = signal.SIGTERM
            return process

        with mock.patch.object(release.subprocess, "Popen", side_effect=spawn):
            with self.assertRaises(release.ReleaseError) as caught:
                with instance._cancel_handlers():
                    instance._run_delegated(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        timeout=None,
                    )
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cancelled", "signal"),
        )
        self.assertIsNone(instance._delegated_process)

    def test_delegated_exit_zero_after_cancel_still_fails(self):
        pid_file = self.base / "delegated.pid"
        code = (
            "import os,signal,time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            f"open({str(pid_file)!r},'w').write(str(os.getpid()));"
            "time.sleep(0.4);"
            "raise SystemExit(0)"
        )
        instance = release.Release(repo=self.repo, env=self.env)
        sender = send_signal_when_marker(pid_file, signal.SIGTERM)
        try:
            with self.assertRaises(release.ReleaseError) as caught:
                with instance._cancel_handlers():
                    instance._run_delegated(
                        [sys.executable, "-c", code], timeout=None
                    )
        finally:
            sender.join(timeout=3.0)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cancelled", "signal"),
        )
        self.assertIsNone(instance._delegated_process)
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 3.0
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.02)
        self.assertFalse(alive, "delegated child was not reaped")


class ProcessCleanupTests(ReleaseTestBase):
    """Real short-lived fake children only; no Docker, PG, HTTP, or network."""

    def _recorded_pid(self, path: Path) -> int | None:
        try:
            return int(path.read_text())
        except (OSError, ValueError):
            return None

    def _kill_recorded_pid(self, path: Path) -> None:
        pid = self._recorded_pid(path)
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def _kill_recorded_pid_after(self, path: Path, seconds: float) -> None:
        time.sleep(seconds)
        self._kill_recorded_pid(path)

    def _delegated_pipe_holder(
        self,
        pidfile: Path,
        sleep_seconds: float,
        *,
        close_pipes: bool = False,
    ) -> str:
        grandchild = (
            "import os,signal,time; "
            + ("os.close(1); os.close(2); " if close_pipes else "")
            + "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            + f"time.sleep({sleep_seconds})"
        )
        return (
            "import subprocess,sys,time; "
            f"child=subprocess.Popen([sys.executable,'-c',{grandchild!r}]); "
            f"open({str(pidfile)!r},'w').write(str(child.pid)); "
            "time.sleep(30)"
        )

    def test_delegated_timeout_reaps_pipe_holding_grandchild_bounded(self):
        pidfile = self.base / "grandchild-pipe.pid"
        self.addCleanup(self._kill_recorded_pid, pidfile)
        threading.Thread(
            target=self._kill_recorded_pid_after,
            args=(pidfile, 3.0),
            daemon=True,
        ).start()
        instance = release.Release(repo=self.repo, env=self.env)
        started = time.monotonic()
        with mock.patch.object(release, "DELEGATED_TERM_GRACE_SECONDS", 0.2), mock.patch.object(
            release, "DIRECT_REAP_GRACE_SECONDS", 0.5
        ):
            result = instance._run_delegated(
                [sys.executable, "-c", self._delegated_pipe_holder(pidfile, 1.5)],
                timeout=0.1,
            )
        elapsed = time.monotonic() - started
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, 124)
        self.assertFalse(result.cleanup_failed)
        self.assertLess(elapsed, 1.0, "timeout cleanup waited for natural exit")
        pid = self._recorded_pid(pidfile)
        self.assertIsNotNone(pid)
        self.assertTrue(wait_pid_absent(pid), "grandchild was not reaped")

    def test_delegated_timeout_reaps_closed_pipe_survivor_after_leader_exit(self):
        pidfile = self.base / "grandchild-closed.pid"
        self.addCleanup(self._kill_recorded_pid, pidfile)
        instance = release.Release(repo=self.repo, env=self.env)
        with mock.patch.object(release, "DELEGATED_TERM_GRACE_SECONDS", 0.2), mock.patch.object(
            release, "DIRECT_REAP_GRACE_SECONDS", 0.5
        ):
            result = instance._run_delegated(
                [
                    sys.executable,
                    "-c",
                    self._delegated_pipe_holder(
                        pidfile, 3.0, close_pipes=True
                    ),
                ],
                timeout=0.2,
            )
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, 124)
        self.assertFalse(result.cleanup_failed)
        pid = self._recorded_pid(pidfile)
        self.assertIsNotNone(pid)
        self.assertTrue(
            wait_pid_absent(pid, timeout=1.0),
            "closed-pipe survivor was not reaped",
        )

    def test_delegated_timeout_unconfirmable_cleanup_is_failed(self):
        pidfile = self.base / "grandchild-unconfirmed.pid"
        self.addCleanup(self._kill_recorded_pid, pidfile)
        instance = release.Release(repo=self.repo, env=self.env)
        with mock.patch.object(release, "DELEGATED_TERM_GRACE_SECONDS", 0.2), mock.patch.object(
            release, "DIRECT_REAP_GRACE_SECONDS", 0.5
        ), mock.patch.object(
            release, "_proc_snapshot", return_value=None, create=True
        ):
            result = instance._run_delegated(
                [sys.executable, "-c", self._delegated_pipe_holder(pidfile, 0.8)],
                timeout=0.1,
            )
        self.assertTrue(result.timed_out)
        self.assertTrue(result.cleanup_failed)
        self.assertTrue(instance._cancel_cleanup_failed)
        pid = self._recorded_pid(pidfile)
        self.assertIsNotNone(pid)
        self.assertTrue(wait_pid_absent(pid), "unconfirmed descendant was left")

    def test_direct_tool_timeout_is_not_user_cancellation(self):
        instance = release.Release(repo=self.repo, env=self.env)
        started = time.monotonic()
        try:
            result = instance._run_direct(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                timeout=0.1,
            )
        except release.ReleaseError as exc:
            self.fail(f"tool timeout misclassified as {exc.stage}/{exc.kind}")
        elapsed = time.monotonic() - started
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, 124)
        self.assertFalse(result.cleanup_failed)
        self.assertIsNone(instance._cancel_signal)
        self.assertTrue(release.check_images._CANCEL_REQUESTED)
        self.assertLess(elapsed, 1.5)

    def test_direct_real_signal_is_still_cancelled(self):
        pidfile = self.base / "direct-signal.pid"
        self.addCleanup(self._kill_recorded_pid, pidfile)
        code = (
            "import os,time; "
            f"open({str(pidfile)!r},'w').write(str(os.getpid())); "
            "time.sleep(30)"
        )
        instance = release.Release(repo=self.repo, env=self.env)
        sender = send_signal_when_marker(pidfile, signal.SIGTERM)
        try:
            with self.assertRaises(release.ReleaseError) as caught:
                with instance._cancel_handlers():
                    instance._run_direct(
                        [sys.executable, "-c", code], timeout=5
                    )
        finally:
            sender.join(timeout=3.0)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cancelled", "signal"),
        )
        pid = self._recorded_pid(pidfile)
        self.assertIsNotNone(pid)
        self.assertTrue(wait_pid_absent(pid), "signalled child was not reaped")

    def test_direct_timeout_cleanup_failure_is_not_hidden(self):
        class TimeoutCleanupRunner:
            def __call__(
                self,
                argv,
                *,
                cwd,
                env,
                timeout,
                input_text=None,
                reap_grace=0.0,
            ):
                release.check_images._CANCEL_REQUESTED = True
                return release.ProcResult(124, "", "", True, 0.0, True)

        instance = release.Release(
            repo=self.repo, runner=TimeoutCleanupRunner(), env=self.env
        )
        with self.assertRaises(release.ReleaseError) as caught:
            instance._run_direct(["fake-tool"], timeout=1)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("tool", "cleanup-failed"),
        )
        self.assertTrue(instance._cancel_cleanup_failed)

    def test_direct_nonzero_exit_is_returned_for_call_site_classification(self):
        instance = release.Release(repo=self.repo, env=self.env)
        result = instance._run_direct(
            [sys.executable, "-c", "raise SystemExit(7)"], timeout=5
        )
        self.assertEqual(result.exit_code, 7)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.cleanup_failed)


class ProcessOwnershipTests(unittest.TestCase):
    """Synthetic /proc snapshots; os.kill/os.killpg are mocked and recorded."""

    leader_pid = 1000
    owned_pgid = 1000
    leader_identity = ("S", 1, 1000, 1000, 500)

    def test_reused_leader_root_and_unrelated_children_never_tracked_or_killed(self):
        snapshot = {
            1000: ("S", 1, 2000, 2000, 900),
            2000: ("S", 1000, 2000, 2000, 901),
            2001: ("S", 2000, 2000, 2000, 902),
        }
        tracked: dict[int, tuple[str, int, int, int, int]] = {}
        with mock.patch.object(
            release, "_proc_snapshot", return_value=snapshot
        ), mock.patch.object(
            release,
            "_read_proc_identity",
            side_effect=lambda pid: snapshot.get(pid),
        ), mock.patch.object(
            release.os, "kill"
        ) as kill_mock, mock.patch.object(
            release.os, "killpg"
        ) as killpg_mock:
            self.assertTrue(
                release._merge_tracked_descendants(
                    tracked,
                    self.leader_pid,
                    self.owned_pgid,
                    self.leader_identity,
                )
            )
            self.assertEqual(tracked, {})
            release._kill_tracked_descendants(
                tracked, self.owned_pgid, self.leader_identity
            )
            kill_mock.assert_not_called()
            killpg_mock.assert_not_called()
            self.assertFalse(
                release._merge_tracked_descendants(
                    tracked,
                    self.leader_pid,
                    self.owned_pgid,
                    self.leader_identity,
                    require_leader_root=True,
                )
            )

    def test_reused_tracked_pid_identity_is_not_killed(self):
        tracked = {2000: ("S", 1000, 2000, 2000, 500)}
        snapshot = {2000: ("S", 1000, 2000, 2000, 900)}
        with mock.patch.object(
            release, "_proc_snapshot", return_value=snapshot
        ), mock.patch.object(
            release,
            "_read_proc_identity",
            side_effect=lambda pid: snapshot.get(pid),
        ), mock.patch.object(
            release.os, "kill"
        ) as kill_mock, mock.patch.object(
            release.os, "killpg"
        ) as killpg_mock:
            self.assertFalse(release._tracked_descendant_present(tracked))
            release._kill_tracked_descendants(tracked, None, None)
            kill_mock.assert_not_called()
            killpg_mock.assert_not_called()

    def test_verified_root_child_is_tracked_and_killed_positive_control(self):
        snapshot = {
            1000: ("S", 1, 1000, 1000, 500),
            2000: ("S", 1000, 1000, 1000, 501),
        }
        tracked: dict[int, tuple[str, int, int, int, int]] = {}
        with mock.patch.object(
            release, "_proc_snapshot", return_value=snapshot
        ), mock.patch.object(
            release,
            "_read_proc_identity",
            side_effect=lambda pid: snapshot.get(pid),
        ), mock.patch.object(
            release.os, "kill"
        ) as kill_mock, mock.patch.object(
            release.os, "killpg"
        ) as killpg_mock:
            self.assertTrue(
                release._merge_tracked_descendants(
                    tracked,
                    self.leader_pid,
                    self.owned_pgid,
                    self.leader_identity,
                )
            )
            self.assertIn(2000, tracked)
            release._kill_tracked_descendants(
                tracked, self.owned_pgid, self.leader_identity
            )
            kill_mock.assert_called_once_with(2000, signal.SIGKILL)
            killpg_mock.assert_called_once_with(1000, signal.SIGKILL)


class HttpDeadlineTests(unittest.TestCase):
    def test_absolute_deadline_interrupts_slow_fake_body(self):
        started = time.monotonic()
        with self.assertRaises(release.ReleaseError) as caught:
            with release._HttpDeadline(0.1, "http"):
                time.sleep(2.0)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("http", "deadline"))
        self.assertLess(time.monotonic() - started, 1.5)

    def test_default_http_interrupts_slow_local_stub_body(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(2.0)

            def log_message(self, *args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/slow"
            started = time.monotonic()
            with self.assertRaises(release.ReleaseError) as caught:
                release._default_http(
                    "GET", url, headers={}, timeout=0.2, max_bytes=64
                )
            self.assertEqual((caught.exception.stage, caught.exception.kind), ("http", "deadline"))
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

    def test_default_http_rejects_redirect_without_following(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/other")
                self.end_headers()

            def log_message(self, *args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/redirect"
            with self.assertRaises(release.ReleaseError) as caught:
                release._default_http(
                    "GET", url, headers={}, timeout=1.0, max_bytes=64
                )
            self.assertEqual((caught.exception.stage, caught.exception.kind), ("registry", "redirect"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
