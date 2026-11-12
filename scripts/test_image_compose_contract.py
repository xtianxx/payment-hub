#!/usr/bin/env python3
"""T073/T074 fixed application image and Compose overlay contract tests.

These tests are deliberately static. They read only the fixed repository files
and parse the three fixed Compose files with the already-present Docker Compose
CLI in read-only ``config --format json`` mode. They never connect to the Docker
daemon, start a service, pull or build an image, read a dotenv file, or use the
network. The parser uses only the Python standard library (subprocess + json),
so the suite runs under ``python3 -S``. Failure messages are fixed categories;
raw YAML, raw CLI stdout/stderr, parsed models, environment values, DSNs, keys,
and tool output are not written to artifacts or printed as assertion values.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE_APP = REPO / "deploy" / "Dockerfile.app"
DOCKERFILE_MIGRATE = REPO / "deploy" / "Dockerfile.migrate"
COMPOSE_APP = REPO / "deploy" / "compose.app.yaml"
BASE_COMPOSE = REPO / "deploy" / "compose.yaml"
TEST_COMPOSE = REPO / "deploy" / "compose.test.yaml"
GO_MOD = REPO / "go.mod"
GO_SUM = REPO / "go.sum"

BASE_COMPOSE_NAME = "deploy/compose.yaml"
TEST_COMPOSE_NAME = "deploy/compose.test.yaml"
COMPOSE_APP_NAME = "deploy/compose.app.yaml"
COMPOSE_FILES = (BASE_COMPOSE_NAME, TEST_COMPOSE_NAME, COMPOSE_APP_NAME)

COMPOSE_TIMEOUT_SECONDS = 30
COMPOSE_SEARCH_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
COMPOSE_CONFIG_FLAGS = ("--no-interpolate", "--no-normalize", "--no-path-resolution")
COMPOSE_PROFILES = ("app", "app-test", "migrate")

EXPECTED_GO_BUILDER = (
    "docker.io/library/golang:1.27.4-bookworm"
    "@sha256:4fa7c968ba93f1635670522dbb01a2c78150443e1385746924566079f36570ea"
)
EXPECTED_RUNTIME = (
    "docker.io/library/debian:bookworm-slim"
    "@sha256:c67e92c8099ba22853ab4cfda7cfb494d253d6012ca82dee98ba8976772af018"
)
EXPECTED_POSTGRES = (
    "docker.io/library/postgres:16.15-bookworm"
    "@sha256:5983db9a9c6ad225a48a46cfe2e03e6680b1826662befd0465eb05b34cb1c64e"
)

APP_IMAGE = "${PAYMENT_HUB_APP_IMAGE:?}"
MIGRATE_IMAGE = "${PAYMENT_HUB_MIGRATE_IMAGE:?}"
CLIENT_ID = "${PAYMENT_HUB_CLIENT_ID:?}"
API_KEY = "${PAYMENT_HUB_API_KEY:?}"
DATABASE_URL = "${PAYMENT_HUB_DATABASE_URL:?}"
HTTP_ADDR = "0.0.0.0:8080"
HTTP_PORT = "127.0.0.1:${PAYMENT_HUB_TEST_HTTP_PORT:-8080}:8080"
UID_GID = "${PAYMENT_HUB_TEST_UID:?}:${PAYMENT_HUB_TEST_GID:?}"
SOCKET_DIR = "${PAYMENT_HUB_TEST_SOCKET_DIR:?}"
GOOSE_DBSTRING = "${PAYMENT_HUB_DATABASE_URL:?}"
LOGGING = {
    "driver": "local",
    "options": {"max-size": "10m", "max-file": "3"},
}

SESSION_EMPTY_VALUES = {
    "PAYMENT_HUB_TEST_CONTROL_SOCKET": "${PAYMENT_HUB_TEST_CONTROL_SOCKET:-}",
    "PAYMENT_HUB_TEST_RUN_ID": "${PAYMENT_HUB_TEST_RUN_ID:-}",
    "PAYMENT_HUB_TEST_OBSERVER_EPOCH": "${PAYMENT_HUB_TEST_OBSERVER_EPOCH:-}",
    "PAYMENT_HUB_TEST_GENERATION": "${PAYMENT_HUB_TEST_GENERATION:-}",
    "PAYMENT_HUB_TEST_CONTROL_VERSION": "${PAYMENT_HUB_TEST_CONTROL_VERSION:-}",
}

APP_ENV_KEYS = {
    "PAYMENT_HUB_CLIENT_ID",
    "PAYMENT_HUB_API_KEY",
    "PAYMENT_HUB_DATABASE_URL",
    "PAYMENT_HUB_HTTP_ADDR",
    "PAYMENT_HUB_TEST_MODE",
    "TZ",
    "HOME",
}
APP_TEST_ENV_KEYS = APP_ENV_KEYS | set(SESSION_EMPTY_VALUES)
MIGRATE_ENV_KEYS = {"GOOSE_DRIVER", "GOOSE_DBSTRING", "TZ", "HOME"}

# Freeze hashes are intentionally literal. The two new files are initialized to
# None for the TDD red step and filled after their fixed contents are written.
FROZEN_SHA256 = {
    ".dockerignore": "a52495f2a13adc2da7c07ec2950082b6f3177e629b5551583f2b6b4dd986e4b7",
    "go.mod": "681547b25b5a9a34f1b2554f0e5e633be4f066ea8b42cbedd4225f064e0e83bf",
    "go.sum": "6ba9e14572fd680f0e28f0ead8b521f3f5501cb964a8cfa8b1cc825258b18019",
    "deploy/compose.yaml": "aa992418cd960cc9c8f55089119519dd61e2a559090659fcb5e1f2d7757bc68a",
    "deploy/compose.test.yaml": "0b6e3127d6c540506b2b4af1590b17972e8d61e7e006819c94c891f66f05eb04",
    "deploy/Dockerfile.app": "6a0b38b056978ea521613854eb7990cb3a48458bc23c17b99d5d5f6fe7f83ca5",
    "deploy/Dockerfile.migrate": "6bf8764e03dbd5bc3f4b4dc401a8c4b3340b23e804892924b9a59bcc0d0fc2ae",
    "deploy/goose-entrypoint.sh": "0c7f35a1d6a0bc40c46b8a1f1056463d8003328053c276216953c785afb2160a",
    "deploy/compose.app.yaml": "fe5dffdbcc2922d6b5d8755734eb6dfbfec0e9eb37e3c337f15d17c9e1f251ed",
}

EXPECTED_COPY_SOURCES = frozenset(
    {
        "go.mod",
        "go.sum",
        "cmd/payment-hub/main.go",
        "internal/app/app.go",
        "internal/app/business.go",
        "internal/app/config.go",
        "internal/app/lifecycle.go",
        "internal/app/logging.go",
        "internal/app/testmode.go",
        "internal/httpapi/auth.go",
        "internal/httpapi/payments.go",
        "internal/httpapi/readyz.go",
        "internal/httpapi/request.go",
        "internal/httpapi/response.go",
        "internal/httpapi/router.go",
        "internal/mock/hooks.go",
        "internal/mock/query.go",
        "internal/mock/transaction.go",
        "internal/payment/errors.go",
        "internal/payment/identity.go",
        "internal/payment/ports.go",
        "internal/payment/query.go",
        "internal/payment/query_types.go",
        "internal/payment/result.go",
        "internal/payment/submit.go",
        "internal/payment/submit_validation.go",
        "internal/payment/types.go",
        "internal/payment/validation.go",
        "internal/postgres/hooks.go",
        "internal/postgres/lookup.go",
        "internal/postgres/orders.go",
        "internal/postgres/results.go",
        "internal/testcontrol/client.go",
        "internal/testcontrol/protocol.go",
    }
)

FORBIDDEN_COPY_PREFIXES = (
    "tests/",
    "artifacts/",
    "deploy/",
    "migrations/",
    ".git/",
    ".env",
    "secrets/",
)


def read_text(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(
            f"image compose contract prerequisite: missing {path.relative_to(REPO)}"
        )
    return path.read_text(encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def service(overlay, name: str) -> dict:
    services = overlay.get("services")
    if not isinstance(services, dict) or name not in services:
        raise AssertionError(f"image compose contract: missing service {name}")
    value = services[name]
    if not isinstance(value, dict):
        raise AssertionError(f"image compose contract: service {name} is not a mapping")
    return value


class ImageComposeContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._docker = shutil.which("docker", path=COMPOSE_SEARCH_PATH)
        if cls._docker is None:
            raise AssertionError(
                "image compose contract prerequisite: Docker Compose CLI is unavailable"
            ) from None
        # Use the default TemporaryDirectory so a clean runner's TMPDIR is
        # honoured.  The suite must not depend on an agent-only parent
        # directory or create a global one as a fallback.
        cls._tmp = tempfile.TemporaryDirectory(
            prefix="payment-hub-compose-config-"
        )
        cls.addClassCleanup(cls._tmp.cleanup)
        cls._env = {
            "PATH": COMPOSE_SEARCH_PATH,
            "HOME": cls._tmp.name,
            "DOCKER_CONFIG": cls._tmp.name,
            "DOCKER_HOST": "unix:///nonexistent/payment-hub-compose-config.sock",
            "DOCKER_CLI_HINTS": "false",
            "COMPOSE_ANSI": "never",
            "TMPDIR": cls._tmp.name,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        cls._probe_compose_cli()
        cls._configs = {
            relative: cls._load_compose(relative) for relative in COMPOSE_FILES
        }

    @staticmethod
    def _forbidden_agent_path(value, clean_tmp):
        """True for the agent parent or an agent path outside this probe.

        A caller-chosen clean TMPDIR may itself be under the agent parent; its
        own legitimate temporary children must remain allowed.
        """
        path = Path(os.fspath(value)).resolve()
        agent_parent = Path("/tmp/opencode")
        try:
            path.relative_to(agent_parent)
        except ValueError:
            return False
        clean = Path(clean_tmp).resolve()
        return not (path == clean or clean in path.parents)

    def _run_clean_runner_probe(self, parent):
        parent = Path(parent)
        parent.mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(
            os.environ, {"TMPDIR": str(parent)}, clear=False
        ), mock.patch.object(tempfile, "tempdir", None):
            clean_tmp = Path(
                tempfile.mkdtemp(prefix="payment-hub-clean-tmpdir-")
            )
        self.addCleanup(shutil.rmtree, clean_tmp, True)
        # The scenario parent must actually have selected this TMPDIR; do not
        # fake a clean runner by leaving the ambient directory in effect.
        self.assertEqual(clean_tmp.resolve().parent, parent.resolve())

        real_temporary_directory = tempfile.TemporaryDirectory
        real_mkdir = os.mkdir
        created_default = []
        mkdir_paths = []

        def guarded_mkdir(path, *args, **kwargs):
            text = os.fspath(path)
            mkdir_paths.append(text)
            if self._forbidden_agent_path(text, clean_tmp):
                raise AssertionError(
                    "image compose contract: global agent directory must not be created"
                )
            return real_mkdir(path, *args, **kwargs)

        # The guard must still reject the hardcoded parent and agent paths
        # outside this probe's clean TMPDIR.
        for forbidden in ("/tmp/opencode", "/tmp/opencode/other-probe"):
            with self.assertRaises(AssertionError):
                guarded_mkdir(forbidden, 0o700)
            mkdir_paths.pop()

        def guarded_temporary_directory(*args, **kwargs):
            explicit = kwargs.get("dir")
            if explicit is not None:
                raise FileNotFoundError(
                    "clean runner: simulated absent TemporaryDirectory parent"
                )
            created_default.append(True)
            return real_temporary_directory(*args, **kwargs)

        class CleanRunnerProbe(ImageComposeContractTest):
            pass

        try:
            with mock.patch.dict(
                os.environ, {"TMPDIR": str(clean_tmp)}, clear=False
            ), mock.patch.object(tempfile, "tempdir", None), mock.patch.object(
                tempfile,
                "TemporaryDirectory",
                side_effect=guarded_temporary_directory,
            ), mock.patch.object(
                os, "mkdir", side_effect=guarded_mkdir
            ), mock.patch.object(
                ImageComposeContractTest,
                "_probe_compose_cli",
                new=classmethod(lambda cls: None),
            ), mock.patch.object(
                ImageComposeContractTest,
                "_load_compose",
                new=classmethod(lambda cls, relative: {}),
            ), mock.patch.object(
                shutil, "which", return_value="/usr/bin/docker"
            ), mock.patch.object(
                subprocess,
                "run",
                side_effect=AssertionError(
                    "image compose contract: no Docker command in the clean-runner probe"
                ),
            ):
                CleanRunnerProbe.setUpClass()
            self.assertTrue(created_default)
            self.assertTrue(
                Path(CleanRunnerProbe._tmp.name)
                .resolve()
                .is_relative_to(clean_tmp.resolve())
            )
            self.assertFalse(
                any(
                    self._forbidden_agent_path(path, clean_tmp)
                    for path in mkdir_paths
                )
            )
        finally:
            CleanRunnerProbe.doClassCleanups()

    def test_setup_class_honours_clean_runner_tmpdir_without_agent_directory(self):
        """Cover both the agent parent TMPDIR and a general runner TMPDIR.

        The real global directory is never removed.  The clean TMPDIR is
        selected through the real TMPDIR path, not by faking the variable after
        the fact.
        """
        agent_parent = Path("/tmp/opencode")
        if agent_parent.is_dir():
            with self.subTest(tmpdir="agent-parent"):
                self._run_clean_runner_probe(agent_parent)
        default_tmp = Path(tempfile.gettempdir()).resolve()
        try:
            default_tmp.relative_to(agent_parent)
        except ValueError:
            general_parent = default_tmp
        else:
            general_parent = Path("/tmp")
        with self.subTest(tmpdir="general-runner"):
            general_parent = Path(
                tempfile.mkdtemp(
                    prefix="payment-hub-general-parent-",
                    dir=str(general_parent),
                )
            )
            self.addCleanup(shutil.rmtree, general_parent, True)
            self._run_clean_runner_probe(general_parent)

    @classmethod
    def _run(cls, argv):
        try:
            return subprocess.run(
                argv,
                cwd=str(REPO),
                env=cls._env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=COMPOSE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise AssertionError(
                "image compose contract: Compose CLI exceeded the 30 second limit"
            ) from None
        except OSError:
            raise AssertionError(
                "image compose contract: Compose CLI could not be executed"
            ) from None

    @classmethod
    def _probe_compose_cli(cls):
        config_help = cls._run([cls._docker, "compose", "config", "--help"])
        if config_help.returncode != 0:
            raise AssertionError(
                "image compose contract prerequisite: Compose config help failed"
            ) from None
        config_text = (config_help.stdout + b"\n" + config_help.stderr).decode(
            "utf-8", "replace"
        )
        for flag in COMPOSE_CONFIG_FLAGS + ("--format",):
            if flag not in config_text:
                raise AssertionError(
                    "image compose contract prerequisite: Compose config flag unsupported"
                ) from None
        if "json" not in config_text:
            raise AssertionError(
                "image compose contract prerequisite: Compose JSON output unsupported"
            ) from None
        compose_help = cls._run([cls._docker, "compose", "--help"])
        if compose_help.returncode != 0:
            raise AssertionError(
                "image compose contract prerequisite: Compose help failed"
            ) from None
        compose_text = (compose_help.stdout + b"\n" + compose_help.stderr).decode(
            "utf-8", "replace"
        )
        for flag in ("--env-file", "--profile", "--file"):
            if flag not in compose_text:
                raise AssertionError(
                    "image compose contract prerequisite: Compose flag unsupported"
                ) from None

    @classmethod
    def _load_compose(cls, relative: str):
        if relative not in COMPOSE_FILES:
            raise AssertionError(
                "image compose contract: Compose file is not an allowed fixed input"
            ) from None
        if not (REPO / relative).is_file():
            raise AssertionError(
                "image compose contract prerequisite: missing " + relative
            ) from None
        argv = [
            cls._docker,
            "compose",
            "--env-file",
            "/dev/null",
        ]
        # Fixed profiles make the overlay's profile-gated services visible to
        # the read-only config command; config never starts a service.
        for profile in COMPOSE_PROFILES:
            argv.extend(("--profile", profile))
        argv.extend(
            (
                "-f",
                relative,
                "config",
            )
        )
        argv.extend(COMPOSE_CONFIG_FLAGS)
        argv.extend(("--format", "json"))
        result = cls._run(argv)
        if result.returncode != 0:
            raise AssertionError(
                "image compose contract: Compose config failed for " + relative
            ) from None
        try:
            data = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AssertionError(
                "image compose contract: invalid Compose config JSON for " + relative
            ) from None
        if not isinstance(data, dict):
            raise AssertionError(
                "image compose contract: Compose config root is not an object for "
                + relative
            ) from None
        return data

    def compose(self, relative: str):
        return self._configs[relative]

    def fail_category(self, category: str):
        self.fail("image compose contract: " + category)

    def require(self, condition, category: str):
        if not condition:
            self.fail_category(category)

    def require_equal(self, actual, expected, category: str):
        if actual != expected:
            self.fail_category(category)

    def require_in(self, container, item, category: str):
        if item not in container:
            self.fail_category(category)

    def require_not_in(self, container, item, category: str):
        if item in container:
            self.fail_category(category)

    def test_fixed_files_exist(self):
        for path in (
            DOCKERFILE_APP,
            COMPOSE_APP,
            DOCKERFILE_MIGRATE,
            BASE_COMPOSE,
            TEST_COMPOSE,
            GO_MOD,
            GO_SUM,
            REPO / ".dockerignore",
            REPO / "deploy" / "goose-entrypoint.sh",
        ):
            self.require(
                path.is_file(),
                "prerequisite: missing " + str(path.relative_to(REPO)),
            )

    def test_frozen_hashes(self):
        for relative, expected in FROZEN_SHA256.items():
            path = REPO / relative
            self.require(
                path.is_file(),
                "prerequisite: missing " + relative,
            )
            self.require(
                expected is not None,
                "freeze hash not initialized for " + relative,
            )
            self.require_equal(
                sha256(path),
                expected,
                "frozen hash mismatch for " + relative,
            )

    def test_base_compose_is_pg_only(self):
        base = self.compose(BASE_COMPOSE_NAME)
        test = self.compose(TEST_COMPOSE_NAME)
        self.require_equal(set(base), {"name", "services", "volumes"}, "base top-level shape mismatch")
        self.require_equal(set(base["services"]), {"postgres"}, "base service set mismatch")
        self.require_equal(set(base["volumes"]), {"postgres_data"}, "base volume set mismatch")
        self.require_equal(set(test), {"services"}, "test top-level shape mismatch")
        self.require_equal(set(test["services"]), {"postgres"}, "test service set mismatch")
        self.require_equal(base["services"]["postgres"]["image"], EXPECTED_POSTGRES, "base PostgreSQL pin mismatch")
        test_ports = test["services"]["postgres"]["ports"]
        self.require_equal(len(test_ports), 1, "test PostgreSQL port count mismatch")
        self.require_equal(test_ports[0]["host_ip"], "127.0.0.1", "test PostgreSQL host binding mismatch")
        published = test_ports[0]["published"]
        self.require(
            published.startswith("${PAYMENT_HUB_TEST_PG_PORT:?")
            and published.endswith("}"),
            "test PostgreSQL port expression mismatch",
        )
        for name in ("app", "app-test", "migrate"):
            self.require_not_in(base["services"], name, "base file contains application service")
            self.require_not_in(test["services"], name, "test file contains application service")

    def test_dockerfile_source_pins_and_builder(self):
        text = read_text(DOCKERFILE_APP)
        lines = [line.strip() for line in text.splitlines()]
        froms = [line for line in lines if line.startswith("FROM ")]
        self.require_equal(
            froms,
            [
                f"FROM {EXPECTED_GO_BUILDER} AS build",
                f"FROM {EXPECTED_RUNTIME}",
            ],
            "Dockerfile base pin mismatch",
        )
        migrate_froms = [
            line.strip()
            for line in read_text(DOCKERFILE_MIGRATE).splitlines()
            if line.strip().startswith("FROM ")
        ]
        self.require_equal(froms, migrate_froms, "app and migration base pins differ")

        env_lines = {line for line in lines if line.startswith("ENV ")}
        self.require_equal(
            env_lines,
            {
                "ENV CGO_ENABLED=0",
                "ENV GOTOOLCHAIN=local",
                "ENV GOENV=off",
                "ENV GOWORK=off",
                "ENV GOTELEMETRY=off",
                "ENV GOFLAGS=",
                "ENV GOPROXY=https://proxy.golang.org",
                "ENV GOSUMDB=sum.golang.org",
                "ENV TZ=UTC",
                "ENV HOME=/tmp",
            },
            "Dockerfile ENV set mismatch",
        )
        run_lines = {line for line in lines if line.startswith("RUN ")}
        self.require_equal(
            run_lines,
            {
                "RUN go telemetry off",
                "RUN go mod download",
                "RUN mkdir -p /out",
                "RUN go build -mod=readonly -trimpath -o /out/payment-hub ./cmd/payment-hub",
                "RUN chmod 0755 /usr/local/bin/payment-hub",
            },
            "Dockerfile RUN set mismatch",
        )
        self.require_in(lines, "WORKDIR /src", "Dockerfile WORKDIR missing")
        self.require_in(lines, "COPY go.mod go.sum /src/", "Dockerfile module COPY missing")
        self.require_in(
            lines,
            "COPY --from=build /out/payment-hub /usr/local/bin/payment-hub",
            "Dockerfile runtime binary COPY missing",
        )
        self.require_in(lines, "EXPOSE 8080", "Dockerfile HTTP EXPOSE missing")
        self.require_in(lines, "STOPSIGNAL SIGTERM", "Dockerfile stop signal missing")
        self.require_in(lines, "USER 65534:65534", "Dockerfile runtime user mismatch")
        self.require_in(
            lines,
            'ENTRYPOINT ["/usr/local/bin/payment-hub"]',
            "Dockerfile direct ENTRYPOINT missing",
        )
        self.require(not any(line.startswith("CMD ") for line in lines), "Dockerfile CMD is not allowed")
        self.require(not any(line.startswith("LABEL ") for line in lines), "Dockerfile source LABEL is not allowed")
        self.require(not any(line.startswith("ADD ") for line in lines), "Dockerfile ADD is not allowed")
        self.require(not any(line.startswith("VOLUME ") for line in lines), "Dockerfile VOLUME is not allowed")
        self.require(not any(line.startswith("HEALTHCHECK ") for line in lines), "Dockerfile HEALTHCHECK is not allowed")
        self.require(not any(line.startswith("SHELL ") for line in lines), "Dockerfile SHELL is not allowed")
        self.require(not any(line.startswith("ARG ") for line in lines), "Dockerfile ARG is not allowed")
        for line in lines:
            if line.startswith("USER "):
                self.require_not_in(line, "root", "Dockerfile root user is not allowed")
                self.require_not_in(line, ":0", "Dockerfile root group is not allowed")
        for token in ("curl", "wget", "apt-get", "git clone", "go get", "go install"):
            self.require_not_in(text, token, "forbidden Dockerfile build/download token")
        self.require_in(text, "CGO_ENABLED=0", "Dockerfile static CGO explanation missing")
        self.require_in(text, "CGO_ENABLED=1", "Dockerfile host CGO explanation missing")
        self.require_in(text, "normal and test mode", "Dockerfile normal/test-mode explanation missing")
        self.require_in(text, "source manifest label", "Dockerfile source-label note missing")

    def test_dockerfile_copy_allowlist(self):
        text = read_text(DOCKERFILE_APP)
        sources = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line.startswith("COPY "):
                continue
            if line.startswith("COPY --from="):
                continue
            parts = line.split()
            self.require(len(parts) >= 3, "malformed COPY instruction")
            sources.extend(parts[1:-1])
        self.require_equal(len(sources), len(set(sources)), "duplicate COPY source")
        self.require_equal(set(sources), EXPECTED_COPY_SOURCES, "COPY allowlist mismatch")
        self.require_not_in(sources, ".", "whole-context COPY is not allowed")
        self.require_not_in(sources, "./", "whole-context COPY is not allowed")
        for source in sources:
            for prefix in FORBIDDEN_COPY_PREFIXES:
                self.require(
                    not (source == prefix or source.startswith(prefix)),
                    "forbidden COPY source category",
                )
            self.require(
                (REPO / source).is_file(),
                "COPY source is missing",
            )

    def test_compose_topology_and_profiles(self):
        overlay = self.compose(COMPOSE_APP_NAME)
        self.require_equal(set(overlay), {"services"}, "overlay top-level shape mismatch")
        services = overlay["services"]
        self.require_equal(set(services), {"app", "app-test", "migrate"}, "overlay service set mismatch")
        for name in ("app", "app-test", "migrate"):
            current = service(overlay, name)
            self.require_equal(current["platform"], "linux/amd64", "service platform mismatch")
            self.require_equal(current["pull_policy"], "never", "service pull policy mismatch")
            self.require_equal(current["restart"], "no", "service restart policy mismatch")
            self.require_equal(current["stop_grace_period"], "25s", "service stop grace mismatch")
            self.require_equal(current["logging"], LOGGING, "service logging mismatch")
            self.require_not_in(current, "build", "service build is not allowed")
            self.require_not_in(current, "env_file", "service env_file is not allowed")
            self.require_not_in(current, "entrypoint", "service entrypoint override is not allowed")
            self.require_not_in(current, "privileged", "service privileged is not allowed")
            self.require_not_in(current, "network_mode", "service network_mode is not allowed")
            self.require_not_in(current, "pid", "service pid mode is not allowed")
            self.require_not_in(current, "ipc", "service ipc mode is not allowed")
            self.require_not_in(current, "volumes_from", "service volumes_from is not allowed")
        self.require_equal(service(overlay, "app")["image"], APP_IMAGE, "app image expression mismatch")
        self.require_equal(service(overlay, "app-test")["image"], APP_IMAGE, "app-test image expression mismatch")
        self.require_equal(service(overlay, "migrate")["image"], MIGRATE_IMAGE, "migrate image expression mismatch")
        self.require_equal(service(overlay, "app")["profiles"], ["app"], "app profile mismatch")
        self.require_equal(service(overlay, "app-test")["profiles"], ["app-test"], "app-test profile mismatch")
        self.require_equal(service(overlay, "migrate")["profiles"], ["migrate"], "migrate profile mismatch")
        self.require_equal(service(overlay, "migrate")["command"], ["validate"], "migrate default command mismatch")
        self.require(service(overlay, "migrate")["command"] != ["up"], "migrate default command must not run up")
        self.require_not_in(overlay, "volumes", "overlay top-level volume is not allowed")
        self.require_not_in(overlay, "networks", "overlay top-level network is not allowed")
        self.require_not_in(overlay, "secrets", "overlay top-level secret is not allowed")
        self.require_not_in(overlay, "configs", "overlay top-level config is not allowed")
        self.require_not_in(read_text(COMPOSE_APP), "docker.sock", "Docker socket mount is not allowed")
        for name in ("app", "app-test"):
            current = service(overlay, name)
            self.require_equal(current["ports"], [HTTP_PORT], "service HTTP port expression mismatch")
            depends_on = current["depends_on"]
            # Compose's read-only config model materializes required: true for
            # the fixed PostgreSQL dependency; no other dependency is allowed.
            self.require_equal(
                depends_on,
                {"postgres": {"condition": "service_healthy", "required": True}},
                "service PostgreSQL dependency mismatch",
            )
            self.require_not_in(depends_on, "migrate", "application must not depend on migration")
        self.require_not_in(service(overlay, "migrate"), "ports", "migrate port is not allowed")
        self.require_not_in(service(overlay, "migrate"), "depends_on", "migrate dependency is not allowed")
        self.require_not_in(service(overlay, "app"), "user", "normal app user override is not allowed")
        self.require_not_in(service(overlay, "migrate"), "user", "migrate user override is not allowed")

    def test_profiles_gate_each_service(self):
        overlay = self.compose(COMPOSE_APP_NAME)
        expected = {
            "app": ["app"],
            "app-test": ["app-test"],
            "migrate": ["migrate"],
        }
        for name, profiles in expected.items():
            self.require_equal(service(overlay, name)["profiles"], profiles, "service profile mismatch")
        # Negative proof: with no active profile, no application or migration
        # service is eligible. A static contract that accepted a profile-less
        # app/app-test would fail here.
        active = set()
        for name in expected:
            required = set(service(overlay, name)["profiles"])
            self.require(
                not required.issubset(active),
                "profile-gated service is eligible without its profile",
            )
        for profile, selected, excluded in (
            ("app", "app", ("app-test", "migrate")),
            ("app-test", "app-test", ("app", "migrate")),
            ("migrate", "migrate", ("app", "app-test")),
        ):
            active = {profile}
            self.require(
                set(service(overlay, selected)["profiles"]).issubset(active),
                "selected service is not eligible with its profile",
            )
            for name in excluded:
                self.require(
                    not set(service(overlay, name)["profiles"]).issubset(active),
                    "service is incorrectly eligible with another profile",
                )

    def test_normal_service_isolation(self):
        overlay = self.compose(COMPOSE_APP_NAME)
        app = service(overlay, "app")
        env = app["environment"]
        self.require_equal(set(env), APP_ENV_KEYS, "normal app environment set mismatch")
        self.require_equal(env["PAYMENT_HUB_CLIENT_ID"], CLIENT_ID, "normal app client id expression mismatch")
        self.require_equal(env["PAYMENT_HUB_API_KEY"], API_KEY, "normal app API key expression mismatch")
        self.require_equal(env["PAYMENT_HUB_DATABASE_URL"], DATABASE_URL, "normal app database URL expression mismatch")
        self.require_equal(env["PAYMENT_HUB_HTTP_ADDR"], HTTP_ADDR, "normal app HTTP address mismatch")
        self.require_equal(env["PAYMENT_HUB_TEST_MODE"], "off", "normal app test mode mismatch")
        self.require_equal(env["TZ"], "UTC", "normal app timezone mismatch")
        self.require_equal(env["HOME"], "/tmp", "normal app home mismatch")
        self.require_not_in(app, "volumes", "normal app must not mount a volume")
        self.require_not_in(app, "user", "normal app must not override user")

    def test_test_service_private_socket_and_session(self):
        overlay = self.compose(COMPOSE_APP_NAME)
        app_test = service(overlay, "app-test")
        env = app_test["environment"]
        self.require_equal(set(env), APP_TEST_ENV_KEYS, "test app environment set mismatch")
        self.require_equal(env["PAYMENT_HUB_CLIENT_ID"], CLIENT_ID, "test app client id expression mismatch")
        self.require_equal(env["PAYMENT_HUB_API_KEY"], API_KEY, "test app API key expression mismatch")
        self.require_equal(env["PAYMENT_HUB_DATABASE_URL"], DATABASE_URL, "test app database URL expression mismatch")
        self.require_equal(env["PAYMENT_HUB_HTTP_ADDR"], HTTP_ADDR, "test app HTTP address mismatch")
        self.require_equal(env["PAYMENT_HUB_TEST_MODE"], "on", "test app test mode mismatch")
        self.require_equal(env["TZ"], "UTC", "test app timezone mismatch")
        self.require_equal(env["HOME"], "/tmp", "test app home mismatch")
        for name, expected in SESSION_EMPTY_VALUES.items():
            self.require_equal(env[name], expected, "test session expression mismatch")
            self.require_not_in(env[name], ":?", "test session expression must allow empty normal-mode interpolation")
        self.require_equal(app_test["user"], UID_GID, "test app UID/GID expression mismatch")
        self.require(app_test["user"] != "0:0", "test app root fallback is not allowed")
        self.require_not_in(app_test["user"], "0:0", "test app root fallback is not allowed")
        self.require_equal(
            app_test["volumes"],
            [
                {
                    "type": "bind",
                    "source": SOCKET_DIR,
                    "target": "/run/ph-test",
                }
            ],
            "test app private socket mount mismatch",
        )
        self.require_not_in(read_text(COMPOSE_APP), "docker.sock", "Docker socket mount is not allowed")
        self.require_not_in(app_test, "privileged", "test app privileged is not allowed")
        self.require_not_in(app_test, "network_mode", "test app network_mode is not allowed")

    def test_migrate_is_explicit_and_uses_goose_env(self):
        overlay = self.compose(COMPOSE_APP_NAME)
        migrate = service(overlay, "migrate")
        env = migrate["environment"]
        self.require_equal(set(env), MIGRATE_ENV_KEYS, "migrate environment set mismatch")
        self.require_equal(env["GOOSE_DRIVER"], "postgres", "migrate Goose driver mismatch")
        self.require_equal(env["GOOSE_DBSTRING"], GOOSE_DBSTRING, "migrate Goose DSN expression mismatch")
        self.require_equal(env["TZ"], "UTC", "migrate timezone mismatch")
        self.require_equal(env["HOME"], "/tmp", "migrate home mismatch")
        self.require_equal(migrate["profiles"], ["migrate"], "migrate profile mismatch")
        self.require_equal(migrate["command"], ["validate"], "migrate default command mismatch")
        self.require_not_in(migrate, "ports", "migrate port is not allowed")
        self.require_not_in(migrate, "depends_on", "migrate dependency is not allowed")
        self.require_not_in(migrate, "user", "migrate user override is not allowed")
        for name in ("app", "app-test"):
            self.require_not_in(
                service(overlay, name).get("depends_on", {}),
                "migrate",
                "application must not depend on migration",
            )

    def test_no_literal_secret_or_dsn_leakage(self):
        texts = {
            "deploy/Dockerfile.app": read_text(DOCKERFILE_APP),
            "deploy/compose.app.yaml": read_text(COMPOSE_APP),
        }
        forbidden = (
            (re.compile(r"postgres(?:ql)?://"), "literal database URL"),
            (re.compile(r"Authorization\s*:", re.IGNORECASE), "authorization value"),
            (re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"), "private key material"),
            (
                re.compile(r"(?:ghp_|gho_|ghu_|ghs_|ghr_)[A-Za-z0-9_]+"),
                "registry token material",
            ),
            (
                re.compile(r"(?i)password\s*[:=]\s*(?!\$\{)"),
                "literal password assignment",
            ),
        )
        for relative, text in texts.items():
            for pattern, category in forbidden:
                self.require(
                    pattern.search(text) is None,
                    "forbidden " + category + " in " + relative,
                )
        overlay = self.compose(COMPOSE_APP_NAME)
        for name in ("app", "app-test"):
            env = service(overlay, name)["environment"]
            for key in (
                "PAYMENT_HUB_CLIENT_ID",
                "PAYMENT_HUB_API_KEY",
                "PAYMENT_HUB_DATABASE_URL",
            ):
                value = env[key]
                self.require(
                    value.startswith("${") and value.endswith("}"),
                    "non-expression credential field",
                )
        self.require(
            service(overlay, "migrate")["environment"]["GOOSE_DBSTRING"].startswith("${"),
            "non-expression Goose DSN",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
