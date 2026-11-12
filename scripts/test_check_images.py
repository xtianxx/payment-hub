"""Focused contract tests for the T075 image launcher.

No Go test, PostgreSQL, Docker daemon, or network is used.  Every external
command goes through an injected runner; metadata and runtime-harness reports
are small local JSON fixtures.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check-images.py"

SPEC = importlib.util.spec_from_file_location("check_images", SCRIPT)
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)

SOURCE_DIGEST = "a" * 64
APP_ID = "sha256:" + "b" * 64
MIGRATE_ID = "sha256:" + "c" * 64
APP_REF = "example.invalid/payment-hub/app:test"
MIGRATE_REF = "example.invalid/payment-hub/migrate:test"
API_KEY = "FAKE-image_key.9~+/b=="
DB_PASSWORD = "db-password-canary-not-real"


def executable(path: Path, text: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def read_env_file(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeRunner:
    def __init__(self, handler):
        self.calls = []
        self.handler = handler

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
        argv = list(argv)
        self.calls.append(
            {
                "argv": argv,
                "cwd": str(cwd),
                "env": dict(env),
                "timeout": timeout,
                "input_text": input_text,
                "reap_grace": reap_grace,
            }
        )
        return self.handler(argv, Path(cwd), dict(env), timeout, input_text)


def image_metadata(ref, image_id, digest=SOURCE_DIGEST, os_name="linux", arch="amd64"):
    return {
        "Id": image_id,
        "Os": os_name,
        "Architecture": arch,
        "Config": {"Labels": {check.SOURCE_LABEL: digest}},
    }


class ImageLauncherTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="check-images-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        for relative in (
            "go.mod",
            "go.sum",
            "deploy/compose.yaml",
            "deploy/compose.test.yaml",
            "deploy/compose.app.yaml",
            "deploy/Dockerfile.app",
            "deploy/Dockerfile.migrate",
            "tests/image_test.go",
            "tests/us1_helpers_test.go",
            ".dockerignore",
        ):
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if relative == "tests/us1_helpers_test.go":
                path.write_text(
                    'const us1APIKey = "' + API_KEY + '"\n',
                    encoding="utf-8",
                )
            else:
                path.write_text("fixture\n", encoding="utf-8")
        self.go = executable(self.base / "bin" / "go")
        self.cache = self.base / "cache"
        self.cache.mkdir()
        self.artifact = self.base / "artifacts" / "run"
        self.build_record = self.base / "build-record.json"
        write_json(
            self.build_record,
            {
                "schema": check.BUILD_RECORD_SCHEMA,
                "source_manifest_digest": SOURCE_DIGEST,
                "platform": "linux/amd64",
                "app_image": APP_REF,
                "app_image_id": APP_ID,
                "migrate_image": MIGRATE_REF,
                "migrate_image_id": MIGRATE_ID,
            },
        )
        self.clock = FakeClock()
        self.ports = itertools.cycle((18080, 15432))
        self._artifact_counter = 0
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.base),
            "TMPDIR": str(self.base / "tmp"),
            "LANG": "C.UTF-8",
            "PAYMENT_HUB_APP_IMAGE": APP_REF,
            "PAYMENT_HUB_MIGRATE_IMAGE": MIGRATE_REF,
            check.BUILD_RECORD_ENV: str(self.build_record),
        }
        (self.base / "tmp").mkdir()
        self.runner = FakeRunner(self.default_handler)

    def tearDown(self):
        check._reset_process_cancel()

    def argv(self, go=None, cache=None, artifact=None):
        return [
            "--go",
            str(self.go if go is None else go),
            "--cache-dir",
            str(self.cache if cache is None else cache),
            "--artifact-root",
            str(self.artifact if artifact is None else artifact),
        ]

    def make_launcher(self, handler=None, env_overrides=None):
        env = dict(self.env)
        if env_overrides:
            env.update(env_overrides)
        self._artifact_counter += 1
        artifact = self.base / "artifacts" / ("run-" + str(self._artifact_counter))
        return check.ImageLauncher(
            check.parse_args(self.argv(artifact=artifact)),
            repo=self.repo,
            runner=FakeRunner(handler or self.default_handler),
            docker_path="/tmp/fake-docker",
            clock=self.clock,
            sleep=self.clock.sleep,
            port_provider=lambda: next(self.ports),
            env=env,
        )

    def run_launcher(self, handler=None, env_overrides=None):
        launcher = self.make_launcher(handler, env_overrides)
        with mock.patch.object(
            check,
            "_compute_source_manifest",
            return_value=([{"path": "go.mod", "sha256": "d" * 64}], SOURCE_DIGEST),
        ):
            code = launcher.run()
        return code, launcher

    def report(self, launcher):
        return json.loads(
            (launcher.run_dir / "images.json").read_text(encoding="utf-8")
        )

    def compose_config(self, env):
        return {
            "name": env["COMPOSE_PROJECT_NAME"],
            "volumes": {
                "postgres_data": {
                    "name": env["COMPOSE_PROJECT_NAME"] + "_postgres_data",
                    "external": False,
                }
            },
            "networks": {
                "default": {
                    "name": env["COMPOSE_PROJECT_NAME"] + "_default",
                    "external": False,
                }
            },
            "services": {
                "postgres": {
                    "image": check.POSTGRES_IMAGE,
                    "platform": "linux/amd64",
                    "ports": [
                        {
                            "target": 5432,
                            "host_ip": "127.0.0.1",
                            "published": env["PAYMENT_HUB_TEST_PG_PORT"],
                        }
                    ],
                    "volumes": [
                        {
                            "type": "volume",
                            "source": "postgres_data",
                            "target": "/var/lib/postgresql/data",
                            "read_only": False,
                        }
                    ],
                },
                "app": {
                    "image": env["PAYMENT_HUB_APP_IMAGE"],
                    "platform": "linux/amd64",
                    "ports": [
                        {
                            "target": 8080,
                            "host_ip": "127.0.0.1",
                            "published": env["PAYMENT_HUB_TEST_HTTP_PORT"],
                        }
                    ],
                    "environment": {
                        "PAYMENT_HUB_CLIENT_ID": "image-preflight-client",
                        "PAYMENT_HUB_TEST_MODE": "off",
                    },
                    "volumes": [],
                },
                "app-test": {
                    "image": env["PAYMENT_HUB_APP_IMAGE"],
                    "platform": "linux/amd64",
                    "user": env["PAYMENT_HUB_TEST_UID"]
                    + ":"
                    + env["PAYMENT_HUB_TEST_GID"],
                    "ports": [
                        {
                            "target": 8080,
                            "host_ip": "127.0.0.1",
                            "published": env["PAYMENT_HUB_TEST_HTTP_PORT"],
                        }
                    ],
                    "environment": {
                        "PAYMENT_HUB_CLIENT_ID": "image-preflight-client",
                        "PAYMENT_HUB_TEST_MODE": "on",
                        "PAYMENT_HUB_TEST_CONTROL_SOCKET": "/run/ph-test/control.sock",
                        "PAYMENT_HUB_TEST_RUN_ID": env["PAYMENT_HUB_TEST_RUN_ID"],
                        "PAYMENT_HUB_TEST_OBSERVER_EPOCH": env[
                            "PAYMENT_HUB_TEST_OBSERVER_EPOCH"
                        ],
                        "PAYMENT_HUB_TEST_GENERATION": env[
                            "PAYMENT_HUB_TEST_GENERATION"
                        ],
                        "PAYMENT_HUB_TEST_CONTROL_VERSION": "1",
                    },
                    "volumes": [
                        {
                            "type": "bind",
                            "source": env["PAYMENT_HUB_TEST_SOCKET_DIR"],
                            "target": "/run/ph-test",
                            "read_only": False,
                        }
                    ],
                },
                "migrate": {
                    "image": env["PAYMENT_HUB_MIGRATE_IMAGE"],
                    "platform": "linux/amd64",
                    "environment": {
                        "GOOSE_DBSTRING": env["PAYMENT_HUB_DATABASE_URL"],
                    },
                },
            },
        }

    def write_metadata(self, env, *, image_id=APP_ID, digest=SOURCE_DIGEST):
        run_id = env["PAYMENT_HUB_IMAGE_RUN_ID"]
        run_dir = Path(env["PAYMENT_HUB_TEST_ARTIFACT_ROOT"]) / run_id
        write_json(
            run_dir / "image-metadata.json",
            {
                "schema": "payment-hub.image-metadata.v1",
                "run_id": run_id,
                "source_manifest_digest": digest,
                "platform": "linux/amd64",
                "app_image": env["PAYMENT_HUB_APP_IMAGE"],
                "app_image_id": image_id,
                "migrate_image": env["PAYMENT_HUB_MIGRATE_IMAGE"],
                "migrate_image_id": MIGRATE_ID,
                "metadata_verified": True,
                "registry_digest_status": "not_obtained",
                "local_image_id_is_registry_digest": False,
            },
        )

    def write_runtime(self, env, *, observer=True):
        run_id = env["PAYMENT_HUB_IMAGE_RUN_ID"]
        path = (
            Path(env["PAYMENT_HUB_TEST_ARTIFACT_ROOT"])
            / run_id
            / "scenarios"
            / "image_pair_acceptance"
            / run_id
            / "runtime-harness.json"
        )
        report = {
            "schema": "payment-hub.runtime-harness.v1",
            "run_id": run_id,
            "collection_complete": True,
            "faults": [],
            "versions": {
                "go": "go1.27.4",
                "goos": "linux",
                "goarch": "amd64",
                "postgres": "16.15",
                "goose": "v3.29.0",
            },
            "source": {"manifest": [], "manifest_digest": "e" * 64, "unchanged": True},
            "processes": [],
            "logs": [],
            "http": [{"generation": 0}, {"generation": 1}, {"generation": 2}],
            "sql": [{"generation": 0}, {"generation": 1}, {"generation": 2}],
            "budgets": [],
            "observer": (
                {
                    "epoch": "f" * 32,
                    "counts": {"payment": 1, "query": 0},
                    "receipts": [
                        {"run_id": run_id, "epoch": "f" * 32, "generation": 1, "last_seq": 1},
                        {"run_id": run_id, "epoch": "f" * 32, "generation": 2, "last_seq": 0},
                    ],
                }
                if observer
                else None
            ),
        }
        write_json(path, report)

    def go_stream(self, *, include_root=True, root_action="pass"):
        events = [
            {"Time": "2026-11-12T00:00:00Z", "Action": "run", "Package": "payment-hub/tests", "Test": "TestImagePairAcceptance"},
            {"Time": "2026-11-12T00:00:01Z", "Action": root_action, "Package": "payment-hub/tests", "Test": "TestImagePairAcceptance", "Elapsed": 0.1},
            {"Time": "2026-11-12T00:00:02Z", "Action": "pass", "Package": "payment-hub/tests", "Elapsed": 0.2},
        ]
        if not include_root:
            events = [events[0], events[2]]
        return "\n".join(json.dumps(item) for item in events) + "\n"

    def default_handler(self, argv, cwd, env, timeout, input_text):
        joined = " ".join(argv)
        if argv[0] == "/tmp/fake-docker":
            if argv[1:] == ["--version"]:
                return check.ProcResult(0, "Docker version 27.0.0\n", "", False, 0.01)
            if argv[1:] == ["context", "show"]:
                return check.ProcResult(0, "default\n", "", False, 0.01)
            if argv[1:3] == ["context", "inspect"]:
                return check.ProcResult(0, '"unix:///var/run/docker.sock"\n', "", False, 0.01)
            if argv[1:3] == ["version", "--format"]:
                return check.ProcResult(0, '{"Os":"linux","Arch":"amd64"}\n', "", False, 0.01)
            if argv[1:3] == ["info", "--format"]:
                return check.ProcResult(0, '["name=seccomp"]\n', "", False, 0.01)
            if argv[1:3] == ["compose", "version"]:
                return check.ProcResult(0, "Docker Compose version v2.29.0\n", "", False, 0.01)
            if argv[1] == "image" and argv[2] == "inspect":
                ref = argv[-1]
                if ref == APP_REF:
                    return check.ProcResult(0, json.dumps(image_metadata(ref, APP_ID)) + "\n", "", False, 0.01)
                if ref == MIGRATE_REF:
                    return check.ProcResult(0, json.dumps(image_metadata(ref, MIGRATE_ID)) + "\n", "", False, 0.01)
            if argv[1] == "compose":
                env_file = Path(argv[argv.index("--env-file") + 1])
                values = read_env_file(env_file)
                if "config" in argv:
                    return check.ProcResult(0, json.dumps(self.compose_config(values)) + "\n", "", False, 0.01)
                if "up" in argv:
                    return check.ProcResult(0, "", "", False, 0.01)
                if "ps" in argv and "-q" in argv:
                    return check.ProcResult(0, "abc123def456\n", "", False, 0.01)
                if "run" in argv and "migrate" in argv:
                    return check.ProcResult(0, "", "", False, 0.01)
                if "down" in argv:
                    return check.ProcResult(0, "", "", False, 0.01)
            if argv[1] == "inspect" and "--format" in argv:
                return check.ProcResult(0, '"healthy"\n', "", False, 0.01)
            if argv[1] == "exec" and "postgres" in argv and "--version" in argv:
                return check.ProcResult(0, "postgres (PostgreSQL) 16.15\n", "", False, 0.01)
            if argv[1] == "exec" and "psql" in argv:
                return check.ProcResult(0, "0\n0\n0\n", "", False, 0.01)
            if argv[1] == "ps" and "--filter" in argv:
                return check.ProcResult(0, "", "", False, 0.01)
            if argv[1] == "volume" and argv[2] == "ls":
                return check.ProcResult(0, "", "", False, 0.01)
            if argv[1] == "network" and argv[2] == "ls":
                return check.ProcResult(0, "", "", False, 0.01)
            if argv[1] == "volume" and argv[2] == "rm":
                return check.ProcResult(0, "", "", False, 0.01)
        if "go" in Path(argv[0]).name and "test" in argv:
            self.write_metadata(env)
            self.write_runtime(env)
            return check.ProcResult(0, self.go_stream(), "", False, 0.01)
        raise AssertionError("unexpected fake command: " + joined)


class ImageLauncherContractTests(ImageLauncherTestCase):
    def test_happy_path_writes_whitelist_images_report(self):
        code, launcher = self.run_launcher()
        self.assertEqual(code, 0)
        report = self.report(launcher)
        self.assertEqual(report["result"], "pass")
        self.assertTrue(report["release_candidate"])
        self.assertEqual(report["platform"], "linux/amd64")
        self.assertEqual(report["source"]["manifest_digest"], SOURCE_DIGEST)
        self.assertEqual(report["images"]["app"]["id"], APP_ID)
        self.assertEqual(report["images"]["migrate"]["id"], MIGRATE_ID)
        self.assertIsNone(report["images"]["app"]["registry_digest"])
        self.assertFalse(report["images"]["app"]["local_image_id_is_registry_digest"])
        self.assertEqual(report["go_test"]["required_root"], "TestImagePairAcceptance")
        self.assertEqual(report["go_test"]["result"], "pass")
        self.assertEqual(report["observations"]["observer"]["payment"], 1)
        self.assertEqual(report["observations"]["observer"]["query"], 0)
        self.assertEqual(report["observations"]["observer"]["generations"], [1, 2])
        self.assertEqual(
            report["db_fixture_counts"],
            {"payment_orders": 0, "mock_transactions": 0, "result_handling_records": 0},
        )
        self.assertEqual(report["cleanup"]["result"], "pass")
        path = launcher.run_dir / "images.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        text = path.read_text(encoding="utf-8")
        for secret in (API_KEY, DB_PASSWORD, "postgres://", "postgresql://"):
            self.assertNotIn(secret, text)

    def test_missing_image_environment_fails_closed(self):
        code, launcher = self.run_launcher(
            env_overrides={"PAYMENT_HUB_APP_IMAGE": ""}
        )
        self.assertNotEqual(code, 0)
        report = self.report(launcher)
        self.assertEqual(report["result"], "fail")
        self.assertEqual(report["failure"]["stage"], "environment")
        self.assertEqual(report["failure"]["kind"], "image-env")

    def test_missing_build_record_fails_closed(self):
        code, launcher = self.run_launcher(
            env_overrides={check.BUILD_RECORD_ENV: str(self.base / "missing.json")}
        )
        self.assertNotEqual(code, 0)
        report = self.report(launcher)
        self.assertEqual(report["failure"]["stage"], "image")
        self.assertEqual(report["failure"]["kind"], "build-record")

    def test_build_record_source_and_id_mismatch_fails(self):
        bad = json.loads(self.build_record.read_text())
        bad["source_manifest_digest"] = "e" * 64
        write_json(self.build_record, bad)
        code, launcher = self.run_launcher()
        self.assertNotEqual(code, 0)
        self.assertEqual(self.report(launcher)["failure"]["kind"], "build-record")

        bad = json.loads(self.build_record.read_text())
        bad["source_manifest_digest"] = SOURCE_DIGEST
        bad["app_image_id"] = "sha256:" + "9" * 64
        write_json(self.build_record, bad)
        code, launcher = self.run_launcher()
        self.assertNotEqual(code, 0)
        self.assertEqual(self.report(launcher)["failure"]["kind"], "build-record")

    def test_image_platform_or_source_label_mismatch_fails(self):
        def handler(argv, cwd, env, timeout, input_text):
            if argv[0] == "/tmp/fake-docker" and argv[1] == "image":
                ref = argv[-1]
                image_id = APP_ID if ref == APP_REF else MIGRATE_ID
                value = image_metadata(ref, image_id, arch="arm64")
                return check.ProcResult(0, json.dumps(value) + "\n", "", False, 0.01)
            return self.default_handler(argv, cwd, env, timeout, input_text)

        code, launcher = self.run_launcher(handler=handler)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.report(launcher)["failure"]["kind"], "image-platform")

    def test_go_test_missing_or_skipped_root_fails(self):
        for include_root, action in ((False, "pass"), (True, "skip")):
            with self.subTest(include_root=include_root, action=action):
                self.setUp()
                def handler(argv, cwd, env, timeout, input_text, include_root=include_root, action=action):
                    if "go" in Path(argv[0]).name and "test" in argv:
                        self.write_metadata(env)
                        self.write_runtime(env)
                        return check.ProcResult(
                            0,
                            self.go_stream(include_root=include_root, root_action=action),
                            "",
                            False,
                            0.01,
                        )
                    return self.default_handler(argv, cwd, env, timeout, input_text)
                code, launcher = self.run_launcher(handler=handler)
                self.assertNotEqual(code, 0)
                self.assertEqual(self.report(launcher)["failure"]["stage"], "test")

    def test_metadata_missing_or_observer_untrusted_fails(self):
        def missing(argv, cwd, env, timeout, input_text):
            if "go" in Path(argv[0]).name and "test" in argv:
                self.write_runtime(env)
                return check.ProcResult(0, self.go_stream(), "", False, 0.01)
            return self.default_handler(argv, cwd, env, timeout, input_text)

        code, launcher = self.run_launcher(handler=missing)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.report(launcher)["failure"]["stage"], "observations")

        def untrusted(argv, cwd, env, timeout, input_text):
            if "go" in Path(argv[0]).name and "test" in argv:
                self.write_metadata(env)
                self.write_runtime(env, observer=False)
                return check.ProcResult(0, self.go_stream(), "", False, 0.01)
            return self.default_handler(argv, cwd, env, timeout, input_text)

        self.setUp()
        code, launcher = self.run_launcher(handler=untrusted)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.report(launcher)["failure"]["stage"], "observations")

    def test_migration_failure_still_cleans_and_reports(self):
        def handler(argv, cwd, env, timeout, input_text):
            if argv[0] == "/tmp/fake-docker" and argv[1] == "compose" and "run" in argv:
                return check.ProcResult(1, "", "", False, 0.01)
            return self.default_handler(argv, cwd, env, timeout, input_text)

        code, launcher = self.run_launcher(handler=handler)
        self.assertNotEqual(code, 0)
        report = self.report(launcher)
        self.assertEqual(report["failure"]["stage"], "migration")
        self.assertEqual(report["cleanup"]["result"], "pass")
        self.assertTrue((launcher.run_dir / "images.json").is_file())

    def test_go_test_timeout_marks_failure_and_cleans(self):
        def handler(argv, cwd, env, timeout, input_text):
            if "go" in Path(argv[0]).name and "test" in argv:
                return check.ProcResult(124, "", "", True, 180.0)
            return self.default_handler(argv, cwd, env, timeout, input_text)

        code, launcher = self.run_launcher(handler=handler)
        self.assertNotEqual(code, 0)
        report = self.report(launcher)
        self.assertEqual(report["failure"]["stage"], "test")
        self.assertEqual(report["failure"]["kind"], "timeout")
        self.assertEqual(report["cleanup"]["result"], "pass")
        self.assertEqual(report["go_test"]["timeout_seconds"], 180)
        self.assertEqual(report["go_test"]["reap_grace_seconds"], 15)
        self.assertTrue(
            any("down" in call["argv"] for call in launcher.runner.calls)
        )
        go_calls = [
            call
            for call in launcher.runner.calls
            if "go" in Path(call["argv"][0]).name and "test" in call["argv"]
        ]
        self.assertEqual(len(go_calls), 1)
        self.assertEqual(go_calls[0]["timeout"], 180)
        self.assertEqual(go_calls[0]["reap_grace"], 15)

    def test_docker_environment_is_shared_with_go_test(self):
        env = {
            "DOCKER_HOST": "unix:///tmp/custom-docker.sock",
            "DOCKER_CONTEXT": "custom-context",
            "DOCKER_CONFIG": "/tmp/custom-docker-config",
            "XDG_RUNTIME_DIR": "/tmp/custom-xdg",
        }
        code, launcher = self.run_launcher(env_overrides=env)
        self.assertEqual(code, 0)
        go_calls = [
            call
            for call in launcher.runner.calls
            if "go" in Path(call["argv"][0]).name and "test" in call["argv"]
        ]
        self.assertEqual(len(go_calls), 1)
        for key, value in env.items():
            self.assertEqual(go_calls[0]["env"].get(key), value)
        for call in launcher.runner.calls:
            if call["argv"][0] == "/tmp/fake-docker" and "compose" in call["argv"]:
                for key, value in env.items():
                    self.assertEqual(call["env"].get(key), value)

    def test_fixed_profiles_are_passed_and_no_external_compose_bypass(self):
        code, launcher = self.run_launcher()
        self.assertEqual(code, 0)
        compose_calls = [
            call
            for call in launcher.runner.calls
            if call["argv"][0] == "/tmp/fake-docker"
            and "compose" in call["argv"]
            and "--env-file" in call["argv"]
        ]
        self.assertTrue(compose_calls)
        expected = [
            "--profile",
            "app",
            "--profile",
            "app-test",
            "--profile",
            "migrate",
        ]
        for call in compose_calls:
            argv = call["argv"]
            start = argv.index("--profile")
            self.assertEqual(argv[start : start + len(expected)], expected)
            self.assertNotIn("COMPOSE_FILE", call["env"])
            self.assertNotIn("COMPOSE_PROFILES", call["env"])

    def test_compose_model_rejects_external_or_wrong_pg_storage(self):
        def mutate(kind):
            def handler(argv, cwd, env, timeout, input_text):
                if (
                    argv[0] == "/tmp/fake-docker"
                    and "compose" in argv
                    and "config" in argv
                ):
                    values = read_env_file(Path(argv[argv.index("--env-file") + 1]))
                    config = self.compose_config(values)
                    if kind == "external-volume":
                        config["volumes"]["postgres_data"]["external"] = True
                    elif kind == "wrong-volume":
                        config["volumes"]["postgres_data"]["name"] = "foreign-volume"
                    elif kind == "wrong-network":
                        config["networks"]["default"]["name"] = "foreign-network"
                    elif kind == "extra-pg-mount":
                        config["services"]["postgres"]["volumes"].append(
                            {
                                "type": "bind",
                                "source": "/tmp",
                                "target": "/var/lib/postgresql/data-extra",
                                "read_only": False,
                            }
                        )
                    return check.ProcResult(
                        0, json.dumps(config) + "\n", "", False, 0.01
                    )
                return self.default_handler(argv, cwd, env, timeout, input_text)

            return handler

        for kind in (
            "external-volume",
            "wrong-volume",
            "wrong-network",
            "extra-pg-mount",
        ):
            with self.subTest(kind=kind):
                self.setUp()
                code, launcher = self.run_launcher(handler=mutate(kind))
                self.assertNotEqual(code, 0)
                report = self.report(launcher)
                self.assertEqual(report["failure"]["stage"], "environment")
                self.assertEqual(report["failure"]["kind"], "compose-config")
                joined = [" ".join(call["argv"]) for call in launcher.runner.calls]
                self.assertFalse(
                    any(" compose up " in " " + item + " " for item in joined)
                )
                self.assertFalse(
                    any(" compose down " in " " + item + " " for item in joined)
                )
                self.assertFalse(any("volume rm" in item for item in joined))

    def test_cleanup_down_start_failure_still_deletes_temp_and_writes_report(self):
        def handler(argv, cwd, env, timeout, input_text):
            if (
                argv[0] == "/tmp/fake-docker"
                and "compose" in argv
                and "down" in argv
            ):
                raise check.LauncherError("cleanup", "compose-down")
            return self.default_handler(argv, cwd, env, timeout, input_text)

        code, launcher = self.run_launcher(handler=handler)
        self.assertNotEqual(code, 0)
        report = self.report(launcher)
        self.assertEqual(report["result"], "fail")
        self.assertFalse(report["release_candidate"])
        self.assertNotEqual(report["cleanup"]["result"], "pass")
        self.assertFalse(launcher.env_file.exists())
        self.assertFalse(launcher.socket_dir.exists())
        text = (launcher.run_dir / "images.json").read_text(encoding="utf-8")
        for secret in (API_KEY, DB_PASSWORD, "postgres://", "postgresql://"):
            self.assertNotIn(secret, text)

    def _wait_pid_gone(self, pid, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            try:
                state = Path("/proc/" + str(pid) + "/stat").read_text().split()[2]
            except (FileNotFoundError, IndexError, OSError):
                return True
            if state == "Z":
                return True
            time.sleep(0.05)
        return False

    def _cleanup_owned_group(self, leader_pid, grand_pid):
        if leader_pid is None or grand_pid is None:
            return
        try:
            owned = os.getpgid(grand_pid) == leader_pid
        except (ProcessLookupError, PermissionError):
            owned = False
        if owned:
            try:
                os.killpg(leader_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.kill(grand_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_timeout_after_leader_reaped_uses_recorded_group(self):
        ready = self.base / "leader-ready"
        pid_file = self.base / "leader-pids"
        script = self.base / "leader_exit.py"
        script.write_text(
            "import os, subprocess, sys, time\n"
            "grand = subprocess.Popen([\n"
            "    sys.executable, '-B', '-c',\n"
            "    'import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)',\n"
            "])\n"
            "open(sys.argv[1], 'w').write(\n"
            "    str(os.getpid()) + ' ' + str(grand.pid)\n"
            ")\n"
            "time.sleep(0.05)\n"
            "open(sys.argv[2], 'w').write('ready')\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        leader_pid = None
        grand_pid = None
        real_getpgid = os.getpgid

        def getpgid(pid):
            if ready.exists():
                raise ProcessLookupError(pid)
            return real_getpgid(pid)

        try:
            with mock.patch.object(check.os, "getpgid", side_effect=getpgid):
                result = check.run_process(
                    [
                        sys.executable,
                        "-B",
                        str(script),
                        str(pid_file),
                        str(ready),
                    ],
                    cwd=self.base,
                    env=dict(os.environ),
                    timeout=0.5,
                    reap_grace=2.0,
                )
            self.assertTrue(result.timed_out)
            self.assertEqual(result.exit_code, 124)
            self.assertFalse(result.cleanup_failed)
            leader_pid, grand_pid = map(
                int, pid_file.read_text(encoding="utf-8").split()
            )
            self.assertTrue(self._wait_pid_gone(grand_pid))
        finally:
            if leader_pid is None or grand_pid is None:
                try:
                    leader_pid, grand_pid = map(
                        int, pid_file.read_text(encoding="utf-8").split()
                    )
                except (OSError, ValueError):
                    pass
            self._cleanup_owned_group(leader_pid, grand_pid)

    def test_signal_kills_grandchild_after_leader_exit_with_closed_pipe(self):
        ready = self.base / "pipe-ready"
        pid_file = self.base / "pipe-pids"
        script = self.base / "pipe_exit.py"
        script.write_text(
            "import os, subprocess, sys, time\n"
            "grand = subprocess.Popen([\n"
            "    sys.executable, '-B', '-c',\n"
            "    'import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)',\n"
            "], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "open(sys.argv[1], 'w').write(\n"
            "    str(os.getpid()) + ' ' + str(grand.pid)\n"
            ")\n"
            "time.sleep(0.05)\n"
            "open(sys.argv[2], 'w').write('ready')\n"
            "time.sleep(0.5)\n",
            encoding="utf-8",
        )
        leader_pid = None
        grand_pid = None

        def send():
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            os.kill(os.getpid(), signal.SIGTERM)

        def handler(signum, _frame):
            check._CANCEL_REQUESTED = True
            check._signal_active(signum)

        old = signal.signal(signal.SIGTERM, handler)
        try:
            thread = threading.Thread(target=send, daemon=True)
            thread.start()
            result = check.run_process(
                [sys.executable, "-B", str(script), str(pid_file), str(ready)],
                cwd=self.base,
                env=dict(os.environ),
                timeout=5,
                reap_grace=2.0,
            )
            thread.join(timeout=1)
            self.assertFalse(result.cleanup_failed)
            leader_pid, grand_pid = map(
                int, pid_file.read_text(encoding="utf-8").split()
            )
            self.assertTrue(self._wait_pid_gone(grand_pid))
        finally:
            signal.signal(signal.SIGTERM, old)
            if leader_pid is None or grand_pid is None:
                try:
                    leader_pid, grand_pid = map(
                        int, pid_file.read_text(encoding="utf-8").split()
                    )
                except (OSError, ValueError):
                    pass
            self._cleanup_owned_group(leader_pid, grand_pid)

    def test_signal_child_exit_zero_is_not_cleanup_success(self):
        ready = self.base / "zero-ready"
        pid_file = self.base / "zero-pid"
        script = self.base / "zero_exit.py"
        script.write_text(
            "import os, signal, sys, time\n"
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "open(sys.argv[2], 'w').write('ready')\n"
            "while True: time.sleep(0.05)\n",
            encoding="utf-8",
        )

        def send():
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        def handler(signum, _frame):
            check._CANCEL_REQUESTED = True
            check._signal_active(signum)

        old = signal.signal(signal.SIGTERM, handler)
        child_pid = None
        try:
            thread = threading.Thread(target=send, daemon=True)
            thread.start()
            result = check.run_process(
                [sys.executable, "-B", str(script), str(pid_file), str(ready)],
                cwd=self.base,
                env=dict(os.environ),
                timeout=5,
                reap_grace=2.0,
            )
            thread.join(timeout=1)
            self.assertEqual(result.exit_code, 0)
            self.assertFalse(result.cleanup_failed)
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            self.assertTrue(self._wait_pid_gone(child_pid))
        finally:
            signal.signal(signal.SIGTERM, old)
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_preexisting_project_resource_is_not_removed(self):
        def handler(argv, cwd, env, timeout, input_text):
            if argv[0] == "/tmp/fake-docker" and argv[1] == "ps" and "--filter" in argv:
                return check.ProcResult(0, "pre-existing-container\n", "", False, 0.01)
            return self.default_handler(argv, cwd, env, timeout, input_text)

        code, launcher = self.run_launcher(handler=handler)
        self.assertNotEqual(code, 0)
        report = self.report(launcher)
        self.assertEqual(report["failure"]["stage"], "environment")
        self.assertEqual(report["failure"]["kind"], "resource-conflict")
        joined = [" ".join(call["argv"]) for call in launcher.runner.calls]
        self.assertFalse(any(" compose up " in " " + item + " " for item in joined))
        self.assertFalse(any(" compose down " in " " + item + " " for item in joined))
        self.assertFalse(any("volume rm" in item for item in joined))

    def test_source_or_image_change_after_run_fails(self):
        state = {"after": False}

        def handler(argv, cwd, env, timeout, input_text):
            if "go" in Path(argv[0]).name and "test" in argv:
                state["after"] = True
                self.write_metadata(env)
                self.write_runtime(env)
                return check.ProcResult(0, self.go_stream(), "", False, 0.01)
            if state["after"] and argv[0] == "/tmp/fake-docker" and argv[1] == "image":
                ref = argv[-1]
                image_id = "sha256:" + ("9" * 64 if ref == APP_REF else "8" * 64)
                return check.ProcResult(
                    0, json.dumps(image_metadata(ref, image_id)) + "\n", "", False, 0.01
                )
            return self.default_handler(argv, cwd, env, timeout, input_text)

        code, launcher = self.run_launcher(handler=handler)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.report(launcher)["failure"]["kind"], "image-changed")

    def test_fixture_count_nonzero_fails(self):
        def handler(argv, cwd, env, timeout, input_text):
            if argv[0] == "/tmp/fake-docker" and argv[1] == "exec" and "psql" in argv:
                return check.ProcResult(0, "1\n0\n0\n", "", False, 0.01)
            return self.default_handler(argv, cwd, env, timeout, input_text)

        code, launcher = self.run_launcher(handler=handler)
        self.assertNotEqual(code, 0)
        report = self.report(launcher)
        self.assertEqual(report["failure"]["kind"], "fixture-count")
        self.assertFalse(report["release_candidate"])

    def test_cleanup_failure_is_fail_not_release_candidate(self):
        def handler(argv, cwd, env, timeout, input_text):
            if argv[0] == "/tmp/fake-docker" and argv[1] == "compose" and "down" in argv:
                return check.ProcResult(1, "", "", False, 0.01)
            return self.default_handler(argv, cwd, env, timeout, input_text)

        code, launcher = self.run_launcher(handler=handler)
        self.assertNotEqual(code, 0)
        report = self.report(launcher)
        self.assertEqual(report["result"], "fail")
        self.assertFalse(report["release_candidate"])
        self.assertNotEqual(report["cleanup"]["result"], "pass")

    def test_missing_fixed_compose_file_fails(self):
        (self.repo / "deploy/compose.app.yaml").unlink()
        code, launcher = self.run_launcher()
        self.assertNotEqual(code, 0)
        self.assertEqual(self.report(launcher)["failure"]["stage"], "source")

    def test_api_key_parser_reads_existing_fixture_without_hardcoding(self):
        value = check._read_api_key(self.repo)
        self.assertEqual(value, API_KEY)
        self.assertNotIn("FAKE-us1_key", SCRIPT.read_text(encoding="utf-8"))

    def test_fixed_budgets_are_declared(self):
        self.assertEqual(check.GO_TEST_TIMEOUT_SECONDS, 180)
        self.assertEqual(check.BUDGETS["compose_up_seconds"], 120)
        self.assertEqual(check.BUDGETS["pg_health_seconds"], 30)
        self.assertEqual(check.BUDGETS["migration_up_seconds"], 60)
        self.assertEqual(check.BUDGETS["compose_down_seconds"], 60)
        self.assertEqual(check.BUDGETS["docker_version_seconds"], 15)


if __name__ == "__main__":
    unittest.main()
