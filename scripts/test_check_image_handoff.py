"""Focused fake-only tests for the same-job image handoff preflight.

No Docker daemon, PostgreSQL, registry, or network is used.  Docker and Git
commands are injected through a fake runner.  The private archive is built in a
temporary directory with the same OCI layout that release.py already parses.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check-image-handoff.py"

SPEC = importlib.util.spec_from_file_location(
    "check_image_handoff", SCRIPT
)
handoff = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = handoff
SPEC.loader.exec_module(handoff)

release = handoff.release
check_images = handoff.check_images

SOURCE_SHA = "a" * 40
APP_REF = "payment-hub-app:src-abcdef123456"
MIGRATE_REF = "payment-hub-migrate:src-abcdef123456"
SECRETS = {
    "GITHUB_TOKEN": "FAKE-token-canary-9f3",
    "PAYMENT_HUB_DATABASE_URL": (
        "postgres://fake-user:fake-pass@127.0.0.1/fake-db"
    ),
    "PAYMENT_HUB_API_KEY": "FAKE-api-key-canary-7c1",
}
SUMMARY_KEYS = {
    "schema",
    "generated_at",
    "result",
    "not_release",
    "release_eligible",
    "scope",
    "upload",
    "not_proven",
    "failure",
    "checks",
    "counts",
    "source",
    "builder",
    "daemon",
    "images",
    "archive",
    "absence",
    "load",
    "cancellation",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def temp_parent() -> Path:
    candidate = Path("/tmp/opencode")
    if candidate.is_dir():
        return candidate
    return Path(tempfile.gettempdir())


def pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
    except OSError:
        return False
    return len(fields) > 2 and fields[2] != "Z"


def cancel_tool_code(pidfile: Path, grandchild_pidfile: Path) -> str:
    grandchild = (
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        f"Path({str(grandchild_pidfile)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    return (
        "import os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        "deadline = time.monotonic() + 5\n"
        f"while not Path({str(grandchild_pidfile)!r}).exists() "
        "and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "time.sleep(60)\n"
    )


class Pair:
    def __init__(
        self,
        app_id: str,
        migrate_id: str,
        app_size: int,
        migrate_size: int,
        tar_bytes: bytes,
        root_descriptors: dict[str, dict[str, Any]],
    ) -> None:
        self.app_id = app_id
        self.migrate_id = migrate_id
        self.app_size = app_size
        self.migrate_size = migrate_size
        self.tar_bytes = tar_bytes
        self.root_descriptors = root_descriptors


def _make_root(
    role: str,
) -> tuple[bytes, str, int, str, int, str, int]:
    runtime_bytes = ("runtime-" + role).encode("utf-8")
    runtime_digest = "sha256:" + sha256(runtime_bytes)
    attestation_bytes = ("attestation-" + role).encode("utf-8")
    attestation_digest = "sha256:" + sha256(attestation_bytes)
    root = {
        "schemaVersion": 2,
        "mediaType": release.OCI_INDEX,
        "manifests": [
            {
                "mediaType": release.OCI_MANIFEST,
                "digest": runtime_digest,
                "size": len(runtime_bytes),
                "platform": {"architecture": "amd64", "os": "linux"},
            },
            {
                "mediaType": release.OCI_MANIFEST,
                "digest": attestation_digest,
                "size": len(attestation_bytes),
                "platform": {
                    "architecture": "unknown",
                    "os": "unknown",
                },
                "annotations": {
                    release.ATTESTATION_TYPE: "attestation-manifest",
                    release.ATTESTATION_DIGEST: runtime_digest,
                },
            },
        ],
    }
    root_bytes = json_bytes(root)
    root_id = "sha256:" + sha256(root_bytes)
    return (
        root_bytes,
        root_id,
        len(root_bytes),
        runtime_digest,
        len(runtime_bytes),
        attestation_digest,
        len(attestation_bytes),
    )


def make_pair() -> Pair:
    app_root, app_id, app_size, app_runtime, app_runtime_size, app_att, app_att_size = (
        _make_root("app")
    )
    migrate_root, migrate_id, migrate_size, migrate_runtime, migrate_runtime_size, migrate_att, migrate_att_size = (
        _make_root("migrate")
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
                    "org.opencontainers.image.ref.name": "src-abcdef123456",
                },
            },
            {
                "mediaType": release.OCI_INDEX,
                "digest": migrate_id,
                "size": migrate_size,
                "annotations": {
                    "io.containerd.image.name": (
                        "docker.io/library/" + MIGRATE_REF
                    ),
                    "org.opencontainers.image.ref.name": "src-abcdef123456",
                },
            },
        ],
    }
    layout = {"imageLayoutVersion": "1.0.0"}
    manifest = [
        {
            "Config": "blobs/sha256/" + "1" * 64,
            "RepoTags": [APP_REF],
            "Layers": [],
        },
        {
            "Config": "blobs/sha256/" + "2" * 64,
            "RepoTags": [MIGRATE_REF],
            "Layers": [],
        },
    ]
    blobs = {
        app_id.split(":", 1)[1]: app_root,
        migrate_id.split(":", 1)[1]: migrate_root,
        app_runtime.split(":", 1)[1]: b"runtime-app",
        app_att.split(":", 1)[1]: b"attestation-app",
        migrate_runtime.split(":", 1)[1]: b"runtime-migrate",
        migrate_att.split(":", 1)[1]: b"attestation-migrate",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:") as archive:
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
        app_size=app_size,
        migrate_size=migrate_size,
        tar_bytes=buffer.getvalue(),
        root_descriptors={
            "app": {
                "mediaType": release.OCI_INDEX,
                "digest": app_id,
                "size": app_size,
            },
            "migrate": {
                "mediaType": release.OCI_INDEX,
                "digest": migrate_id,
                "size": migrate_size,
            },
        },
    )


def raw_image(
    pair: Pair,
    role: str,
    manifest_digest: str,
    *,
    descriptor: bool = True,
    descriptor_size: int | None = None,
    repo_tags: list[str] | None = None,
    repo_digests: list[str] | None = None,
    image_id: str | None = None,
    os_name: str = "linux",
    arch: str = "amd64",
    source_label: str | None = None,
) -> dict[str, Any]:
    expected_id = pair.app_id if role == "app" else pair.migrate_id
    expected_size = pair.app_size if role == "app" else pair.migrate_size
    expected_ref = APP_REF if role == "app" else MIGRATE_REF
    actual_id = expected_id if image_id is None else image_id
    value: dict[str, Any] = {
        "Id": actual_id,
        "Os": os_name,
        "Architecture": arch,
        "Config": {
            "Labels": {
                release.SOURCE_LABEL: (
                    manifest_digest if source_label is None else source_label
                )
            }
        },
        "Descriptor": {
            "mediaType": release.OCI_INDEX,
            "digest": actual_id,
            "size": (
                expected_size if descriptor_size is None else descriptor_size
            ),
        },
        "RepoTags": [expected_ref] if repo_tags is None else repo_tags,
        "RepoDigests": [] if repo_digests is None else repo_digests,
    }
    if not descriptor:
        value.pop("Descriptor")
    return value


class FakeTools:
    def __init__(
        self,
        pair: Pair,
        manifest_digest: str,
        repo_files: list[str],
        *,
        source_head: str = SOURCE_SHA,
        source_status: str = "",
        app_raw: dict[str, Any] | None = None,
        migrate_raw: dict[str, Any] | None = None,
        load_app_raw: dict[str, Any] | None = None,
        load_migrate_raw: dict[str, Any] | None = None,
        containers_in_use: set[str] | None = None,
        absence_error: bool = False,
        image_ls_fail: bool = False,
        save_error: bool = False,
        remove_error: bool = False,
        load_error: bool = False,
        ps_error: bool = False,
        server_version: str = handoff.EXPECTED_DOCKER_SERVER_VERSION,
        driver_status: str = handoff.CONTAINERD_DRIVER_STATUS,
    ) -> None:
        self.pair = pair
        self.manifest_digest = manifest_digest
        self.repo_files = repo_files
        self.source_head = source_head
        self.source_status = source_status
        self.app_raw = app_raw or raw_image(pair, "app", manifest_digest)
        self.migrate_raw = migrate_raw or raw_image(
            pair, "migrate", manifest_digest
        )
        self.load_app_raw = load_app_raw or raw_image(
            pair, "app", manifest_digest
        )
        self.load_migrate_raw = load_migrate_raw or raw_image(
            pair, "migrate", manifest_digest
        )
        self.images: dict[str, dict[str, Any]] = {
            APP_REF: self.app_raw,
            pair.app_id: self.app_raw,
            MIGRATE_REF: self.migrate_raw,
            pair.migrate_id: self.migrate_raw,
        }
        self.removed: set[str] = set()
        self.containers_in_use = containers_in_use or set()
        self.absence_error = absence_error
        self.image_ls_fail = image_ls_fail
        self.save_error = save_error
        self.remove_error = remove_error
        self.load_error = load_error
        self.ps_error = ps_error
        self.server_version = server_version
        self.driver_status = driver_status
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.save_calls = 0
        self.remove_calls = 0
        self.load_calls = 0
        self.saved_ids: list[str] = []
        self.removed_ids: list[str] = []
        self.unknown_commands: list[list[str]] = []

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        input_text: str | None = None,
        reap_grace: float = 0.0,
    ) -> check_images.ProcResult:
        self.commands.append([str(item) for item in argv])
        self.envs.append(dict(env))
        executable = Path(argv[0]).name
        if executable == "git":
            return self._git([str(item) for item in argv[1:]])
        if executable == "docker":
            return self._docker([str(item) for item in argv[1:]])
        self.unknown_commands.append([str(item) for item in argv])
        return self._result(1)

    def _result(
        self, code: int, stdout: str = "", stderr: str = ""
    ) -> check_images.ProcResult:
        return check_images.ProcResult(code, stdout, stderr, False, 0.0)

    def _git(self, args: list[str]) -> check_images.ProcResult:
        if args[:1] == ["rev-parse"]:
            return self._result(0, self.source_head + "\n")
        if args[:1] == ["status"]:
            return self._result(0, self.source_status)
        if args[:1] == ["ls-files"]:
            return self._result(
                0, "\x00".join(self.repo_files) + "\x00"
            )
        return self._result(1)

    def _docker(self, args: list[str]) -> check_images.ProcResult:
        if args[:2] == ["version", "--format"]:
            return self._result(0, self.server_version + "\n")
        if args[:2] == ["info", "--format"]:
            return self._result(0, self.driver_status + "\n")
        if args[:3] == ["image", "inspect", "--format"]:
            reference = args[-1]
            if reference in self.removed:
                if self.absence_error:
                    return self._result(
                        1,
                        stderr=(
                            "Error response from daemon: "
                            "i/o timeout\n"
                        ),
                    )
                return self._result(
                    1, stderr="Error: No such image: " + reference + "\n"
                )
            value = self.images.get(reference)
            if value is None:
                return self._result(
                    1, stderr="Error: No such image: " + reference + "\n"
                )
            return self._result(0, json.dumps(value) + "\n")
        if args[:2] == ["image", "save"]:
            self.save_calls += 1
            output = args[args.index("--output") + 1]
            self.saved_ids = args[args.index("--output") + 2 :]
            if self.save_error:
                return self._result(
                    1,
                    stderr=(
                        "Error response from daemon: "
                        + SECRETS["GITHUB_TOKEN"]
                        + " "
                        + SECRETS["PAYMENT_HUB_DATABASE_URL"]
                        + "\n"
                    ),
                )
            Path(output).write_bytes(self.pair.tar_bytes)
            return self._result(0)
        if args[:2] == ["ps", "-a"]:
            if self.ps_error:
                return self._result(
                    1, stderr="Error response from daemon: ps failed\n"
                )
            image_id = ""
            for item in args:
                if item.startswith("ancestor="):
                    image_id = item.split("=", 1)[1]
                    break
            if image_id in self.containers_in_use:
                return self._result(0, "container-in-use\n")
            return self._result(0)
        if args[:2] == ["image", "rm"]:
            self.remove_calls += 1
            ids = args[2:]
            self.removed_ids = ids
            if self.remove_error:
                return self._result(
                    1,
                    stderr=(
                        "Error response from daemon: "
                        + SECRETS["PAYMENT_HUB_API_KEY"]
                        + "\n"
                    ),
                )
            for image_id in ids:
                self.removed.add(image_id)
                for key, value in list(self.images.items()):
                    if value.get("Id") == image_id:
                        del self.images[key]
            return self._result(0)
        if args[:2] == ["image", "ls"]:
            if self.image_ls_fail:
                return self._result(
                    1, stderr="Error response from daemon: ls failed\n"
                )
            remaining = [
                image_id
                for image_id in (self.pair.app_id, self.pair.migrate_id)
                if image_id not in self.removed
            ]
            return self._result(
                0,
                "".join(image_id + "\n" for image_id in remaining),
            )
        if args[:1] == ["load"]:
            self.load_calls += 1
            if self.load_error:
                return self._result(
                    1, stderr="Error response from daemon: load failed\n"
                )
            self.images = {
                self.pair.app_id: self.load_app_raw,
                self.pair.migrate_id: self.load_migrate_raw,
            }
            self.removed.clear()
            return self._result(0, "Loaded image ID\n")
        self.unknown_commands.append(["docker", *args])
        return self._result(1)


class HandoffTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="ph-handoff-", dir=str(temp_parent())
        )
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "go.mod").write_text(
            "module example.test\n", encoding="utf-8"
        )
        (self.repo / "go.sum").write_text("", encoding="utf-8")
        (self.repo / "marker.txt").write_text("marker\n", encoding="utf-8")
        self.repo_files = ["go.mod", "go.sum", "marker.txt"]
        self.entries = [
            {
                "path": relative,
                "sha256": sha256(
                    (self.repo / relative).read_bytes()
                ),
            }
            for relative in sorted(self.repo_files)
        ]
        self.manifest_digest = check_images._manifest_digest(self.entries)

        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("git", "docker"):
            executable = self.bin / name
            executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            executable.chmod(0o755)

        self.runner_temp = self.root / "runner-temp"
        self.runner_temp.mkdir()
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.root / "home"),
            "RUNNER_TEMP": str(self.runner_temp),
            "DOCKER_HOST": "unix://" + str(self.runner_temp / "docker.sock"),
            **SECRETS,
        }
        self.build_root = self.root / "build"
        self.artifact_root = self.runner_temp / "handoff-preflight"
        self.pair = make_pair()
        self.write_build_root()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_build_root(
        self,
        *,
        record_overrides: dict[str, Any] | None = None,
        accepted_overrides: dict[str, Any] | None = None,
        manifest_overrides: dict[str, Any] | None = None,
        duplicate_accepted: bool = False,
        missing_accepted: bool = False,
        missing_record: bool = False,
        missing_manifest: bool = False,
    ) -> None:
        if self.build_root.exists():
            shutil.rmtree(self.build_root)
        self.build_root.mkdir()
        upload = self.build_root / "upload"
        upload.mkdir(mode=0o700)
        validation = self.build_root / "validation" / "run-1"
        validation.mkdir(parents=True)

        manifest = {
            "schema": handoff.SOURCE_MANIFEST_SCHEMA,
            "manifest_digest": self.manifest_digest,
            "files": self.entries,
            "git_head": SOURCE_SHA,
            "source_dirty": False,
            "source_sha": SOURCE_SHA,
            "go_mod_sha256": sha256(
                (self.repo / "go.mod").read_bytes()
            ),
            "go_sum_sha256": sha256(
                (self.repo / "go.sum").read_bytes()
            ),
        }
        if manifest_overrides:
            manifest.update(manifest_overrides)
        if not missing_manifest:
            (self.build_root / "source-manifest.json").write_text(
                json.dumps(manifest, sort_keys=True), encoding="utf-8"
            )

        record = {
            "schema": release.BUILD_RECORD_SCHEMA,
            "source_sha": SOURCE_SHA,
            "source_dirty": False,
            "git_head": SOURCE_SHA,
            "source_manifest_digest": self.manifest_digest,
            "platform": release.PLATFORM,
            "app_image": APP_REF,
            "app_image_id": self.pair.app_id,
            "migrate_image": MIGRATE_REF,
            "migrate_image_id": self.pair.migrate_id,
            "utc": "2026-11-13T00:00:00+00:00",
            "build_once_per_image": True,
        }
        if record_overrides:
            record.update(record_overrides)
        if not missing_record:
            (self.build_root / "build-record.json").write_text(
                json.dumps(record, sort_keys=True), encoding="utf-8"
            )

        accepted = {
            "schema": release.IMAGES_SCHEMA,
            "generated_at": "2026-11-13T00:00:00+00:00",
            "run_id": "run-1",
            "platform": release.PLATFORM,
            "result": "pass",
            "release_candidate": True,
            "failure": None,
            "source": {
                "manifest_digest": self.manifest_digest,
                "manifest_file_count": len(self.entries),
                "manifest": self.entries,
                "unchanged": True,
            },
            "build_record": {
                "schema": release.BUILD_RECORD_SCHEMA,
                "verified": True,
            },
            "images": {
                role: {
                    "ref": APP_REF if role == "app" else MIGRATE_REF,
                    "id": (
                        self.pair.app_id
                        if role == "app"
                        else self.pair.migrate_id
                    ),
                    "platform": release.PLATFORM,
                    "source_label_match": True,
                    "registry_digest": None,
                    "registry_digest_status": "not_obtained",
                    "local_image_id_is_registry_digest": False,
                }
                for role in ("app", "migrate")
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
        if accepted_overrides:
            accepted.update(accepted_overrides)
        if not missing_accepted:
            (validation / "images.json").write_text(
                json.dumps(accepted, sort_keys=True), encoding="utf-8"
            )
            if duplicate_accepted:
                second = self.build_root / "validation" / "run-2"
                second.mkdir()
                (second / "images.json").write_text(
                    json.dumps(accepted, sort_keys=True), encoding="utf-8"
                )

    def make_tools(self, **kwargs: Any) -> FakeTools:
        return FakeTools(
            self.pair,
            self.manifest_digest,
            self.repo_files,
            **kwargs,
        )

    def run_main(
        self,
        tools: FakeTools,
        *,
        isolated: bool = True,
        build_root: Path | None = None,
        artifact_root: Path | None = None,
    ) -> int:
        argv = [
            "--build-root",
            str(build_root or self.build_root),
            "--artifact-root",
            str(artifact_root or self.artifact_root),
        ]
        if isolated:
            argv.append("--isolated-daemon")
        return handoff.main(
            argv,
            runner=tools,
            docker_path=str(self.bin / "docker"),
            git_path=str(self.bin / "git"),
            repo=self.repo,
            env=self.env,
        )

    def summary(self) -> dict[str, Any]:
        path = self.build_root / "upload" / handoff.SUMMARY_FILE
        return json.loads(path.read_text(encoding="utf-8"))

    def assert_failure(
        self,
        summary: dict[str, Any],
        stage: str,
        kind: str,
    ) -> None:
        self.assertEqual(summary["schema"], handoff.SUMMARY_SCHEMA)
        self.assertEqual(summary["result"], "fail")
        self.assertEqual(
            summary["failure"], {"stage": stage, "kind": kind}
        )
        self.assertTrue(summary["not_release"])
        self.assertFalse(summary["release_eligible"])
        self.assertEqual(
            summary["upload"],
            {
                "summary": True,
                "pair_tar": False,
                "images": False,
                "release_descriptor": False,
            },
        )

    def docker_commands(self, tools: FakeTools) -> list[list[str]]:
        return [
            command
            for command in tools.commands
            if Path(command[0]).name == "docker"
        ]

    def test_success_saves_and_loads_once_without_rebuild(self) -> None:
        tools = self.make_tools()
        code = self.run_main(tools)
        self.assertEqual(code, 0)
        summary = self.summary()
        self.assertEqual(summary["result"], "pass")
        self.assertIsNone(summary["failure"])
        self.assertEqual(
            summary["checks"],
            {name: "pass" for name in handoff.CHECK_NAMES},
        )
        self.assertEqual(
            summary["counts"],
            {"inspect": 8, "save": 1, "remove": 1, "load": 1},
        )
        self.assertEqual(tools.save_calls, 1)
        self.assertEqual(tools.remove_calls, 1)
        self.assertEqual(tools.load_calls, 1)
        self.assertEqual(
            tools.saved_ids, [self.pair.app_id, self.pair.migrate_id]
        )
        self.assertEqual(
            tools.removed_ids, [self.pair.app_id, self.pair.migrate_id]
        )

        docker = self.docker_commands(tools)
        save = [
            command
            for command in docker
            if command[1:3] == ["image", "save"]
        ]
        remove = [
            command
            for command in docker
            if command[1:3] == ["image", "rm"]
        ]
        load = [
            command for command in docker if command[1:2] == ["load"]
        ]
        self.assertEqual(len(save), 1)
        self.assertEqual(
            save[0][1:],
            [
                "image",
                "save",
                "--output",
                str(self.artifact_root / release.PAIR_FILE),
                self.pair.app_id,
                self.pair.migrate_id,
            ],
        )
        self.assertEqual(len(remove), 1)
        self.assertEqual(
            remove[0][1:],
            [
                "image",
                "rm",
                self.pair.app_id,
                self.pair.migrate_id,
            ],
        )
        self.assertNotIn("--force", remove[0])
        self.assertNotIn("-f", remove[0])
        self.assertEqual(len(load), 1)
        self.assertEqual(
            load[0][1:],
            [
                "load",
                "--input",
                str(self.artifact_root / release.PAIR_FILE),
            ],
        )

        flat = [
            item for command in tools.commands for item in command[1:]
        ]
        for forbidden in (
            "build",
            "pull",
            "push",
            "prune",
            "volume",
            "network",
            "container",
        ):
            self.assertNotIn(forbidden, flat)
        self.assertEqual(tools.unknown_commands, [])

        archive_path = self.artifact_root / release.PAIR_FILE
        self.assertEqual(
            summary["archive"],
            {
                "file": release.PAIR_FILE,
                "sha256": sha256(self.pair.tar_bytes),
                "size": len(self.pair.tar_bytes),
            },
        )
        self.assertEqual(
            sha256(archive_path.read_bytes()),
            summary["archive"]["sha256"],
        )
        self.assertEqual(
            stat.S_IMODE(archive_path.stat().st_mode), 0o600
        )

        self.assertEqual(summary["source"]["sha"], SOURCE_SHA)
        self.assertEqual(
            summary["source"]["manifest_digest"], self.manifest_digest
        )
        self.assertFalse(summary["source"]["source_dirty"])
        self.assertTrue(summary["source"]["current_manifest_match"])
        self.assertTrue(summary["source"]["unchanged"])
        self.assertEqual(
            summary["images"]["app"],
            {
                "ref": APP_REF,
                "id": self.pair.app_id,
                "root_descriptor": self.pair.root_descriptors["app"],
            },
        )
        self.assertEqual(
            summary["images"]["migrate"],
            {
                "ref": MIGRATE_REF,
                "id": self.pair.migrate_id,
                "root_descriptor": self.pair.root_descriptors[
                    "migrate"
                ],
            },
        )
        self.assertTrue(summary["load"]["performed"])
        self.assertEqual(summary["load"]["calls"], 1)
        self.assertEqual(
            summary["load"]["images"]["app"]["root_descriptor"],
            self.pair.root_descriptors["app"],
        )
        self.assertEqual(
            summary["load"]["images"]["migrate"]["root_descriptor"],
            self.pair.root_descriptors["migrate"],
        )
        self.assertEqual(summary["absence"]["app"], "confirmed")
        self.assertEqual(summary["absence"]["migrate"], "confirmed")
        self.assertEqual(
            summary["daemon"],
            {
                "isolated": True,
                "server_version": "29.9.3",
                "containerd_snapshotter": True,
            },
        )
        self.assertEqual(summary["builder"]["accepted_report_count"], 1)
        self.assertTrue(summary["builder"]["build_once_per_image"])
        self.assertEqual(summary["builder"]["accepted_result"], "pass")
        self.assertIn("cross-job-handoff", summary["not_proven"])
        self.assertIn("ghcr-package-access", summary["not_proven"])
        self.assertEqual(
            summary["scope"], "same-job-same-daemon-save-load-preflight"
        )
        self.assertEqual(set(summary), SUMMARY_KEYS)

        text = json.dumps(summary)
        self.assertNotIn(str(self.root), text)
        for secret in SECRETS.values():
            self.assertNotIn(secret, text)
        for marker in ("stdout", "stderr", "DOCKER_HOST", "postgres://"):
            self.assertNotIn(marker, text)
        for env in tools.envs:
            for key in SECRETS:
                self.assertNotIn(key, env)
            self.assertNotIn("DOCKER_CONTEXT", env)

    def test_own_repo_digests_are_accepted(self) -> None:
        tools = self.make_tools(
            app_raw=raw_image(
                self.pair,
                "app",
                self.manifest_digest,
                repo_digests=[
                    "payment-hub-app@" + self.pair.app_id
                ],
            ),
            migrate_raw=raw_image(
                self.pair,
                "migrate",
                self.manifest_digest,
                repo_digests=[
                    "docker.io/library/payment-hub-migrate@"
                    + self.pair.migrate_id
                ],
            ),
        )
        self.assertEqual(self.run_main(tools), 0)
        summary = self.summary()
        self.assertEqual(summary["result"], "pass")
        self.assertEqual(tools.save_calls, 1)
        self.assertEqual(tools.remove_calls, 1)
        self.assertEqual(tools.load_calls, 1)
        self.assertEqual(
            summary["checks"],
            {name: "pass" for name in handoff.CHECK_NAMES},
        )

    def test_normalized_own_repo_tag_is_accepted(self) -> None:
        tools = self.make_tools(
            app_raw=raw_image(
                self.pair,
                "app",
                self.manifest_digest,
                repo_tags=["docker.io/library/" + APP_REF],
            )
        )
        self.assertEqual(self.run_main(tools), 0)
        summary = self.summary()
        self.assertEqual(summary["result"], "pass")
        self.assertEqual(tools.save_calls, 1)
        self.assertEqual(tools.remove_calls, 1)
        self.assertEqual(tools.load_calls, 1)

    def test_missing_descriptor_fails_before_save(self) -> None:
        tools = self.make_tools(
            app_raw=raw_image(
                self.pair,
                "app",
                self.manifest_digest,
                descriptor=False,
            )
        )
        self.assertEqual(self.run_main(tools), 1)
        summary = self.summary()
        self.assert_failure(summary, "image", "descriptor-missing")
        self.assertEqual(tools.save_calls, 0)
        self.assertEqual(tools.remove_calls, 0)
        self.assertEqual(tools.load_calls, 0)
        self.assertEqual(summary["counts"]["save"], 0)

    def test_archive_root_mismatch_fails_before_remove(self) -> None:
        tools = self.make_tools(
            app_raw=raw_image(
                self.pair,
                "app",
                self.manifest_digest,
                descriptor_size=self.pair.app_size + 1,
            )
        )
        self.assertEqual(self.run_main(tools), 1)
        summary = self.summary()
        self.assert_failure(summary, "archive", "root")
        self.assertEqual(tools.save_calls, 1)
        self.assertEqual(tools.remove_calls, 0)
        self.assertEqual(tools.load_calls, 0)
        self.assertEqual(summary["counts"]["save"], 1)
        self.assertEqual(summary["archive"]["sha256"], sha256(
            self.pair.tar_bytes
        ))

    def test_stale_source_fails_before_docker_save(self) -> None:
        self.write_build_root(
            record_overrides={
                "source_sha": "c" * 40,
                "git_head": "c" * 40,
            }
        )
        tools = self.make_tools()
        self.assertEqual(self.run_main(tools), 1)
        summary = self.summary()
        self.assert_failure(summary, "source", "stale")
        self.assertEqual(self.docker_commands(tools), [])
        self.assertEqual(tools.save_calls, 0)
        self.assertEqual(tools.load_calls, 0)
        self.assertEqual(summary["checks"]["git_clean"], "pass")

    def test_dirty_worktree_is_rejected(self) -> None:
        tools = self.make_tools(source_status=" M go.mod\n")
        self.assertEqual(self.run_main(tools), 1)
        summary = self.summary()
        self.assert_failure(summary, "source", "dirty")
        self.assertEqual(self.docker_commands(tools), [])
        self.assertEqual(tools.save_calls, 0)

    def test_record_dirty_is_rejected(self) -> None:
        self.write_build_root(
            record_overrides={"source_dirty": True}
        )
        tools = self.make_tools()
        self.assertEqual(self.run_main(tools), 1)
        self.assert_failure(
            self.summary(), "source", "record-dirty"
        )
        self.assertEqual(tools.save_calls, 0)

    def test_build_once_false_is_rejected(self) -> None:
        self.write_build_root(
            record_overrides={"build_once_per_image": False}
        )
        tools = self.make_tools()
        self.assertEqual(self.run_main(tools), 1)
        self.assert_failure(
            self.summary(), "builder", "record-once"
        )
        self.assertEqual(tools.save_calls, 0)

    def test_missing_build_record_is_rejected(self) -> None:
        self.write_build_root(missing_record=True)
        tools = self.make_tools()
        self.assertEqual(self.run_main(tools), 1)
        self.assert_failure(self.summary(), "builder", "record")
        self.assertEqual(tools.save_calls, 0)

    def test_missing_accepted_report_is_rejected(self) -> None:
        self.write_build_root(missing_accepted=True)
        tools = self.make_tools()
        self.assertEqual(self.run_main(tools), 1)
        self.assert_failure(
            self.summary(), "builder", "accepted-report"
        )
        self.assertEqual(tools.save_calls, 0)

    def test_missing_source_manifest_is_rejected(self) -> None:
        self.write_build_root(missing_manifest=True)
        tools = self.make_tools()
        self.assertEqual(self.run_main(tools), 1)
        self.assert_failure(
            self.summary(), "source", "manifest-file"
        )
        self.assertEqual(tools.save_calls, 0)

    def test_non_isolated_daemon_is_refused(self) -> None:
        with self.subTest("missing-flag"):
            tools = self.make_tools()
            self.assertEqual(self.run_main(tools, isolated=False), 1)
            summary = self.summary()
            self.assert_failure(
                summary, "daemon", "isolated-required"
            )
            self.assertEqual(tools.commands, [])
            self.assertEqual(tools.save_calls, 0)

        with self.subTest("system-socket"):
            self.env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
            tools = self.make_tools()
            self.assertEqual(self.run_main(tools), 1)
            self.assert_failure(
                self.summary(), "daemon", "system-socket"
            )
            self.assertEqual(self.docker_commands(tools), [])
            self.assertEqual(tools.save_calls, 0)

        with self.subTest("outside-runner-temp"):
            self.env["DOCKER_HOST"] = (
                "unix:///tmp/not-the-runner/docker.sock"
            )
            tools = self.make_tools()
            self.assertEqual(self.run_main(tools), 1)
            self.assert_failure(
                self.summary(), "daemon", "outside-runner-temp"
            )
            self.assertEqual(self.docker_commands(tools), [])
            self.assertEqual(tools.save_calls, 0)

        with self.subTest("remote-host"):
            self.env["DOCKER_HOST"] = "tcp://127.0.0.1:2375"
            tools = self.make_tools()
            self.assertEqual(self.run_main(tools), 1)
            self.assert_failure(self.summary(), "daemon", "host")
            self.assertEqual(self.docker_commands(tools), [])
            self.assertEqual(tools.save_calls, 0)

    def test_docker_context_is_refused_before_any_docker_command(self) -> None:
        self.env["DOCKER_CONTEXT"] = "payment-hub-release"
        tools = self.make_tools()
        self.assertEqual(self.run_main(tools), 1)
        summary = self.summary()
        self.assert_failure(summary, "daemon", "docker-context")
        self.assertEqual(self.docker_commands(tools), [])
        self.assertEqual(tools.save_calls, 0)
        self.assertEqual(tools.remove_calls, 0)
        self.assertEqual(tools.load_calls, 0)
        self.assertIsNone(summary["daemon"]["server_version"])
        for env in tools.envs:
            self.assertNotIn("DOCKER_CONTEXT", env)

    def test_daemon_version_and_containerd_are_enforced(self) -> None:
        with self.subTest("version"):
            tools = self.make_tools(server_version="29.6.1")
            self.assertEqual(self.run_main(tools), 1)
            summary = self.summary()
            self.assert_failure(summary, "daemon", "version")
            self.assertIsNone(summary["daemon"]["server_version"])
            self.assertEqual(tools.save_calls, 0)
            self.assertEqual(tools.load_calls, 0)
        with self.subTest("containerd"):
            tools = self.make_tools(
                driver_status="driver-type=overlayfs"
            )
            self.assertEqual(self.run_main(tools), 1)
            summary = self.summary()
            self.assert_failure(
                summary, "daemon", "containerd"
            )
            self.assertEqual(
                summary["daemon"]["server_version"], "29.9.3"
            )
            self.assertEqual(tools.save_calls, 0)
            self.assertEqual(tools.load_calls, 0)

    def test_invalid_daemon_version_is_not_summarized(self) -> None:
        tools = self.make_tools(
            server_version=SECRETS["GITHUB_TOKEN"]
        )
        self.assertEqual(self.run_main(tools), 1)
        summary = self.summary()
        self.assert_failure(summary, "daemon", "version")
        self.assertIsNone(summary["daemon"]["server_version"])
        self.assertNotIn(
            SECRETS["GITHUB_TOKEN"], json.dumps(summary)
        )
        self.assertEqual(tools.save_calls, 0)
        self.assertEqual(tools.load_calls, 0)

    def test_shared_tags_are_refused_without_removal(self) -> None:
        tools = self.make_tools(
            app_raw=raw_image(
                self.pair,
                "app",
                self.manifest_digest,
                repo_tags=[APP_REF, MIGRATE_REF],
            )
        )
        self.assertEqual(self.run_main(tools), 1)
        self.assert_failure(self.summary(), "image", "tags")
        self.assertEqual(tools.save_calls, 0)
        self.assertEqual(tools.remove_calls, 0)
        self.assertEqual(tools.load_calls, 0)

    def test_foreign_single_repo_tag_is_refused_without_removal(self) -> None:
        tools = self.make_tools(
            app_raw=raw_image(
                self.pair,
                "app",
                self.manifest_digest,
                repo_tags=[MIGRATE_REF],
            )
        )
        self.assertEqual(self.run_main(tools), 1)
        self.assert_failure(self.summary(), "image", "tags")
        self.assertEqual(tools.save_calls, 0)
        self.assertEqual(tools.remove_calls, 0)
        self.assertEqual(tools.load_calls, 0)

    def test_foreign_repo_digest_is_refused_without_removal(self) -> None:
        tools = self.make_tools(
            app_raw=raw_image(
                self.pair,
                "app",
                self.manifest_digest,
                repo_digests=[
                    "example.invalid/other@" + self.pair.app_id
                ],
            )
        )
        self.assertEqual(self.run_main(tools), 1)
        self.assert_failure(
            self.summary(), "image", "repo-digests"
        )
        self.assertEqual(tools.save_calls, 0)
        self.assertEqual(tools.remove_calls, 0)
        self.assertEqual(tools.load_calls, 0)

    def test_repo_digest_for_other_id_is_refused_without_removal(self) -> None:
        tools = self.make_tools(
            app_raw=raw_image(
                self.pair,
                "app",
                self.manifest_digest,
                repo_digests=[
                    "payment-hub-app@" + self.pair.migrate_id
                ],
            )
        )
        self.assertEqual(self.run_main(tools), 1)
        self.assert_failure(
            self.summary(), "image", "repo-digests"
        )
        self.assertEqual(tools.save_calls, 0)
        self.assertEqual(tools.remove_calls, 0)
        self.assertEqual(tools.load_calls, 0)

    def test_container_using_image_is_not_removed(self) -> None:
        tools = self.make_tools(
            containers_in_use={self.pair.app_id}
        )
        self.assertEqual(self.run_main(tools), 1)
        summary = self.summary()
        self.assert_failure(summary, "ownership", "in-use")
        self.assertEqual(tools.save_calls, 1)
        self.assertEqual(tools.remove_calls, 0)
        self.assertEqual(tools.load_calls, 0)
        self.assertIn(self.pair.app_id, tools.images)
        self.assertEqual(summary["checks"]["save"], "pass")
        self.assertEqual(summary["checks"]["archive"], "pass")

    def test_container_query_failure_blocks_removal(self) -> None:
        tools = self.make_tools(ps_error=True)
        self.assertEqual(self.run_main(tools), 1)
        summary = self.summary()
        self.assert_failure(summary, "ownership", "containers")
        self.assertEqual(tools.save_calls, 1)
        self.assertEqual(tools.remove_calls, 0)
        self.assertEqual(tools.load_calls, 0)
        self.assertIn(self.pair.app_id, tools.images)

    def test_absence_daemon_error_is_not_treated_as_missing(self) -> None:
        tools = self.make_tools(absence_error=True)
        self.assertEqual(self.run_main(tools), 1)
        summary = self.summary()
        self.assert_failure(
            summary, "absence", "inspect-error"
        )
        self.assertEqual(tools.remove_calls, 1)
        self.assertEqual(tools.load_calls, 0)
        self.assertIsNone(summary["absence"]["app"])

    def test_absence_image_ls_failure_blocks_load(self) -> None:
        tools = self.make_tools(image_ls_fail=True)
        self.assertEqual(self.run_main(tools), 1)
        summary = self.summary()
        self.assert_failure(summary, "absence", "ls")
        self.assertEqual(tools.remove_calls, 1)
        self.assertEqual(tools.load_calls, 0)
        self.assertEqual(summary["absence"]["app"], "confirmed")
        self.assertEqual(
            summary["absence"]["migrate"], "confirmed"
        )

    def test_load_identity_mismatch_fails(self) -> None:
        cases = {
            "id": raw_image(
                self.pair,
                "app",
                self.manifest_digest,
                image_id="sha256:" + "f" * 64,
            ),
            "root": raw_image(
                self.pair,
                "app",
                self.manifest_digest,
                descriptor_size=self.pair.app_size + 1,
            ),
            "source-label": raw_image(
                self.pair,
                "app",
                self.manifest_digest,
                source_label="0" * 64,
            ),
            "platform": raw_image(
                self.pair,
                "app",
                self.manifest_digest,
                os_name="windows",
            ),
        }
        for kind, loaded in cases.items():
            with self.subTest(kind):
                if self.artifact_root.exists():
                    shutil.rmtree(self.artifact_root)
                self.write_build_root()
                tools = self.make_tools(load_app_raw=loaded)
                self.assertEqual(self.run_main(tools), 1)
                summary = self.summary()
                self.assert_failure(summary, "load", kind)
                self.assertEqual(tools.load_calls, 1)
                self.assertTrue(summary["load"]["performed"])
                self.assertEqual(
                    summary["checks"]["load"], "pass"
                )
                self.assertEqual(
                    summary["checks"]["loaded_images"], "not-run"
                )

    def test_duplicate_accepted_report_is_rejected(self) -> None:
        self.write_build_root(duplicate_accepted=True)
        tools = self.make_tools()
        self.assertEqual(self.run_main(tools), 1)
        self.assert_failure(
            self.summary(), "builder", "accepted-report"
        )
        self.assertEqual(tools.save_calls, 0)

    def test_source_manifest_mismatch_is_rejected(self) -> None:
        self.write_build_root(
            manifest_overrides={"manifest_digest": "0" * 64}
        )
        tools = self.make_tools()
        self.assertEqual(self.run_main(tools), 1)
        self.assert_failure(
            self.summary(), "source", "manifest-file"
        )
        self.assertEqual(tools.save_calls, 0)

    def test_remove_failure_is_honest_and_load_not_attempted(self) -> None:
        tools = self.make_tools(remove_error=True)
        self.assertEqual(self.run_main(tools), 1)
        summary = self.summary()
        self.assert_failure(summary, "remove", "remove")
        self.assertEqual(tools.remove_calls, 1)
        self.assertEqual(tools.load_calls, 0)
        self.assertEqual(summary["checks"]["remove"], "not-run")
        self.assertIn(self.pair.app_id, tools.images)

    def test_load_failure_is_honest(self) -> None:
        tools = self.make_tools(load_error=True)
        self.assertEqual(self.run_main(tools), 1)
        summary = self.summary()
        self.assert_failure(summary, "load", "load")
        self.assertEqual(tools.remove_calls, 1)
        self.assertEqual(tools.load_calls, 1)
        self.assertEqual(summary["checks"]["load"], "not-run")

    def test_safe_summary_excludes_secrets_and_raw_output(self) -> None:
        tools = self.make_tools(save_error=True)
        self.assertEqual(self.run_main(tools), 1)
        summary = self.summary()
        self.assert_failure(summary, "save", "save")
        self.assertEqual(tools.save_calls, 1)
        self.assertEqual(tools.remove_calls, 0)
        self.assertEqual(tools.load_calls, 0)
        self.assertEqual(set(summary), SUMMARY_KEYS)
        text = json.dumps(summary)
        for secret in SECRETS.values():
            self.assertNotIn(secret, text)
        for marker in (
            "stdout",
            "stderr",
            "Error response",
            "DOCKER_HOST",
            "postgres://",
            str(self.root),
        ):
            self.assertNotIn(marker, text)
        for env in tools.envs:
            for key in SECRETS:
                self.assertNotIn(key, env)


    def _run_cancel_case(
        self,
        signum: int,
        expect_kind: str,
        *,
        double: bool = False,
    ) -> None:
        case_root = Path(
            tempfile.mkdtemp(prefix="cancel-", dir=str(self.root))
        )
        build_root = case_root / "build"
        build_root.mkdir()
        (build_root / "keep.json").write_text(
            "keep-build", encoding="utf-8"
        )
        upload = build_root / "upload"
        upload.mkdir(mode=0o700)
        (upload / "images-summary.json").write_text(
            "keep-upload", encoding="utf-8"
        )
        artifact_root = case_root / "artifact"
        partial_path = artifact_root / "pair.tar"
        keep_path = artifact_root / "keep.txt"
        tool_pidfile = case_root / "tool.pid"
        grandchild_pidfile = case_root / "grandchild.pid"
        remove_marker = case_root / "remove.marker"
        load_marker = case_root / "load.marker"
        config = {
            "build_root": str(build_root),
            "artifact_root": str(artifact_root),
            "partial_path": str(partial_path),
            "keep_path": str(keep_path),
            "remove_marker": str(remove_marker),
            "load_marker": str(load_marker),
            "tool_code": cancel_tool_code(
                tool_pidfile, grandchild_pidfile
            ),
        }
        config_path = case_root / "config.json"
        config_path.write_text(
            json.dumps(config), encoding="utf-8"
        )

        product = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(Path(__file__).resolve()),
                "--cancel-child",
                str(config_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
        )
        tool_pid: int | None = None
        grandchild_pid: int | None = None
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if product.poll() is not None:
                    break
                if (
                    tool_pidfile.exists()
                    and grandchild_pidfile.exists()
                ):
                    break
                time.sleep(0.01)
            self.assertTrue(
                tool_pidfile.exists() and grandchild_pidfile.exists(),
                "tool/grandchild did not start; product stderr="
                + (
                    product.stderr.read().decode("utf-8", "replace")
                    if product.poll() is not None
                    else "<still-running>"
                ),
            )
            tool_pid = int(tool_pidfile.read_text(encoding="utf-8"))
            grandchild_pid = int(
                grandchild_pidfile.read_text(encoding="utf-8")
            )

            product.send_signal(signum)
            if double:
                time.sleep(0.1)
                try:
                    product.send_signal(
                        signal.SIGINT
                        if signum == signal.SIGTERM
                        else signal.SIGTERM
                    )
                except ProcessLookupError:
                    pass
            try:
                _stdout, stderr = product.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                self.fail(
                    "product did not exit within the bounded cancellation "
                    "window"
                )
            self.assertEqual(
                product.returncode,
                1,
                "product exit="
                + str(product.returncode)
                + " stderr="
                + stderr.decode("utf-8", "replace"),
            )
            # These assertions must run before the test finally fallback.
            self.assertFalse(
                pid_alive(tool_pid),
                "tool process survived cancellation: " + str(tool_pid),
            )
            self.assertFalse(
                pid_alive(grandchild_pid),
                "grandchild process survived cancellation: "
                + str(grandchild_pid),
            )
            self.assertTrue(
                check_images._group_absent(tool_pid),
                "tool process group still has live members: "
                + str(tool_pid),
            )

            summary_path = upload / handoff.SUMMARY_FILE
            self.assertTrue(summary_path.is_file())
            summary = json.loads(
                summary_path.read_text(encoding="utf-8")
            )
            self.assertEqual(summary["result"], "fail")
            self.assertEqual(
                summary["failure"],
                {"stage": "interrupted", "kind": expect_kind},
            )
            cancellation = summary["cancellation"]
            self.assertEqual(cancellation["signal"], expect_kind)
            self.assertIs(cancellation["repeat_signal"], double)
            self.assertIs(
                cancellation["client_process_group_absent"], True
            )
            self.assertIs(cancellation["client_process_reaped"], True)
            self.assertIs(
                cancellation["runner_cleanup_failed"], False
            )
            self.assertEqual(
                cancellation["archive_cleanup"], "removed"
            )
            self.assertIs(
                cancellation["daemon_side_effects_rolled_back"], False
            )
            self.assertIs(
                cancellation["daemon_side_effects_may_remain"], True
            )
            self.assertIn(
                "client process group", cancellation["boundary"]
            )
            self.assertIn(
                "no daemon-side rollback", cancellation["boundary"]
            )
            self.assertEqual(summary["counts"]["save"], 1)
            self.assertEqual(summary["counts"]["remove"], 0)
            self.assertEqual(summary["counts"]["load"], 0)
            self.assertFalse(partial_path.exists())
            self.assertTrue(keep_path.is_file())
            self.assertTrue((build_root / "keep.json").is_file())
            self.assertTrue(
                (upload / "images-summary.json").is_file()
            )
            self.assertFalse(remove_marker.exists())
            self.assertFalse(load_marker.exists())
            self.assertEqual(
                stat.S_IMODE(artifact_root.stat().st_mode), 0o700
            )
            self.assertEqual(
                stat.S_IMODE(summary_path.stat().st_mode), 0o600
            )
        finally:
            if product.poll() is None:
                try:
                    product.kill()
                except OSError:
                    pass
            try:
                product.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
            for pid in (tool_pid, grandchild_pid):
                if pid_alive(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
            if tool_pid is not None and not check_images._group_absent(
                tool_pid
            ):
                try:
                    os.killpg(tool_pid, signal.SIGKILL)
                except OSError:
                    pass
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if not pid_alive(tool_pid) and not pid_alive(
                    grandchild_pid
                ):
                    break
                time.sleep(0.02)

    def test_cancel_sigterm_reaps_tool_and_grandchild(self) -> None:
        self._run_cancel_case(signal.SIGTERM, "sigterm")

    def test_cancel_sigint_reaps_tool_and_grandchild(self) -> None:
        self._run_cancel_case(signal.SIGINT, "sigint")

    def test_cancel_double_signal_cleanup_completes(self) -> None:
        self._run_cancel_case(
            signal.SIGTERM, "sigterm", double=True
        )

    def test_run_restores_signal_handlers(self) -> None:
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        args = handoff.argparse.Namespace(
            build_root=str(self.root / "unused-build"),
            artifact_root=str(self.root / "unused-artifact"),
            isolated_daemon=True,
        )
        preflight = handoff.HandoffPreflight(args, env=os.environ)
        preflight._prepare = lambda: None
        preflight._verify_source_and_builder = lambda: None
        preflight._docker_preflight = lambda: None
        preflight._inspect_original_pair = lambda: None
        preflight._save_pair = lambda: None
        preflight._check_ownership = lambda: None
        preflight._remove_pair = lambda: None
        preflight._confirm_absence = lambda: None
        preflight._load_pair = lambda: None
        preflight._verify_loaded_pair = lambda: None
        preflight._verify_source_unchanged = lambda: None
        self.assertEqual(preflight.run(), 1)
        self.assertIs(signal.getsignal(signal.SIGINT), old_int)
        self.assertIs(signal.getsignal(signal.SIGTERM), old_term)

    def test_timeout_path_still_reaps_without_signal(self) -> None:
        pidfile = self.root / "timeout.pid"
        code = (
            "import os, time\n"
            "from pathlib import Path\n"
            f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
            "time.sleep(60)\n"
        )
        args = handoff.argparse.Namespace(
            build_root=str(self.root / "unused-build"),
            artifact_root=str(self.root / "unused-artifact"),
            isolated_daemon=True,
        )
        preflight = handoff.HandoffPreflight(args, env=os.environ)
        preflight.docker = sys.executable
        preflight.docker_env = {
            name: os.environ[name]
            for name in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
            if name in os.environ
        }
        pid: int | None = None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                with self.assertRaises(handoff.HandoffError) as raised:
                    preflight._run_docker(
                        "save", "save", ["-c", code], 0.2
                    )
            self.assertEqual(
                (raised.exception.stage, raised.exception.kind),
                ("save", "timeout"),
            )
            self.assertTrue(pidfile.is_file())
            pid = int(pidfile.read_text(encoding="utf-8"))
            self.assertFalse(
                pid_alive(pid),
                "timeout path left a live tool: " + str(pid),
            )
            self.assertTrue(
                check_images._group_absent(pid),
                "timeout path left a live tool group: " + str(pid),
            )
        finally:
            if pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass


def _cancel_child_main(config_path: str) -> None:
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))

    def fake_docker_preflight(self: Any) -> None:
        self.docker = sys.executable
        self.docker_env = {
            name: os.environ[name]
            for name in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
            if name in os.environ
        }

    def fake_save(self: Any) -> None:
        self.counts["save"] += 1
        Path(config["partial_path"]).write_bytes(b"partial-archive")
        Path(config["keep_path"]).write_bytes(b"keep-archive")
        self._run_docker(
            "save", "save", ["-c", config["tool_code"]], 300
        )

    def fake_remove(self: Any) -> None:
        Path(config["remove_marker"]).write_text(
            "remove", encoding="utf-8"
        )

    def fake_load(self: Any) -> None:
        Path(config["load_marker"]).write_text(
            "load", encoding="utf-8"
        )

    handoff.HandoffPreflight._docker_preflight = fake_docker_preflight
    handoff.HandoffPreflight._verify_source_and_builder = (
        lambda self: None
    )
    handoff.HandoffPreflight._inspect_original_pair = lambda self: None
    handoff.HandoffPreflight._save_pair = fake_save
    handoff.HandoffPreflight._check_ownership = lambda self: None
    handoff.HandoffPreflight._remove_pair = fake_remove
    handoff.HandoffPreflight._confirm_absence = lambda self: None
    handoff.HandoffPreflight._load_pair = fake_load
    handoff.HandoffPreflight._verify_loaded_pair = lambda self: None
    handoff.HandoffPreflight._verify_source_unchanged = lambda self: None

    code = handoff.main(
        [
            "--build-root",
            config["build_root"],
            "--artifact-root",
            config["artifact_root"],
            "--isolated-daemon",
        ],
        env=os.environ,
    )
    raise SystemExit(code)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--cancel-child":
        _cancel_child_main(sys.argv[2])
    else:
        unittest.main(verbosity=2)
