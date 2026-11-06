#!/usr/bin/env python3
"""Setup-only checks for payment-hub.

The checks cover prepared Go module inputs/caches and the static Compose
configuration. This script intentionally does not build or test the
application, run a database, run migrations, make HTTP requests, restart
processes, pull/build images, start containers, or deploy anything.

Only Python's standard library is used. No tool is installed automatically.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_GO_VERSION = "go1.27.3"
EXPECTED_GOOS = "linux"
EXPECTED_GOARCH = "amd64"
EXPECTED_GOTOOLCHAIN = "local"
EXPECTED_GOTELEMETRY = "off"

EXPECTED_MODULE = "payment-hub"
EXPECTED_GO_DIRECTIVE = "1.27.3"
APPROVED_DIRECT = {
    "github.com/gin-gonic/gin": "v1.12.0",
    "github.com/jackc/pgx/v5": "v5.11.0",
}

PG_IMAGE = (
    "docker.io/library/postgres:16.15-bookworm"
    "@sha256:5983db9a9c6ad225a48a46cfe2e03e6680b1826662befd0465eb05b34cb1c64e"
)
EXPECTED_PLATFORM = "linux/amd64"

BASE_FILES = ("deploy/compose.yaml",)
TEST_FILES = ("deploy/compose.yaml", "deploy/compose.test.yaml")

MOCK_DB = "payment_hub_setup_db"
MOCK_USER = "payment_hub_setup_user"
MOCK_PASSWORD = "payment-hub-setup-mock-password-not-real"
MOCK_TEST_PORT = "15432"

CHECK_COUNT = 0


class CheckError(RuntimeError):
    """A setup check failed."""


def fail(message: str) -> None:
    raise CheckError(message)


def sanitize(text: str, limit: int = 800) -> str:
    """Return a short, single-line diagnostic without the mock password."""
    text = text.replace(MOCK_PASSWORD, "<redacted>")
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "...[truncated]"
    return text


def require(condition: bool, message: str) -> None:
    global CHECK_COUNT
    if not condition:
        fail(message)
    CHECK_COUNT += 1


def require_equal(actual: Any, expected: Any, label: str) -> None:
    require(actual == expected, f"{label}: expected {expected!r}, got {actual!r}")


def first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def run_command(
    label: str,
    command: Sequence[str],
    env: Mapping[str, str],
    timeout: int,
    *,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a bounded command with a caller-supplied whitelist environment."""
    try:
        result = subprocess.run(
            list(command),
            cwd=str(REPO_ROOT),
            env=dict(env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        fail(f"{label}: required command not found: {command[0]}; not installing")
    except subprocess.TimeoutExpired:
        fail(f"{label}: timed out after {timeout}s")
    except OSError as exc:
        fail(f"{label}: could not execute {command[0]}: {exc}")

    if result.returncode != 0 and not allow_failure:
        # Do not echo stdout: a failed compose command could otherwise print
        # rendered configuration. Errors from the tools used here go to stderr.
        detail = sanitize(result.stderr or "")
        suffix = f": {detail}" if detail else ""
        fail(f"{label}: command failed with exit {result.returncode}{suffix}")
    return result


def resolve_go(value: str | None) -> str:
    if value:
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            if not candidate.is_file():
                fail(f"--go does not point to a file: {candidate}")
            if not os.access(candidate, os.X_OK):
                fail(f"--go is not executable: {candidate}")
            return str(candidate)
        resolved = shutil.which(value)
        if not resolved:
            fail(f"--go not found on PATH: {value}")
        return resolved

    resolved = shutil.which("go")
    if not resolved:
        fail("go not found on PATH; pass --go /absolute/path/to/go (no automatic install)")
    return resolved


def prepare_cache(raw: str) -> Path:
    if not raw:
        fail("--cache-dir must not be empty")
    cache = Path(raw).expanduser()
    if not cache.is_absolute():
        fail("--cache-dir must be an absolute path outside the repository")
    cache = cache.resolve()
    repo = REPO_ROOT.resolve()
    if cache == repo or repo in cache.parents:
        fail("--cache-dir must be outside the repository; refusing to write cache inside the worktree")
    if cache.exists() and not cache.is_dir():
        fail(f"--cache-dir is not a directory: {cache}")
    cache.mkdir(parents=True, exist_ok=True)
    for name in ("config", "gomod", "gobuild", "gopath"):
        (cache / name).mkdir(parents=True, exist_ok=True)
    return cache


def go_environment(cache: Path, download_modules: bool) -> dict[str, str]:
    env: dict[str, str] = {}
    for name in ("HOME", "PATH", "TMPDIR", "LANG"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value

    env.update(
        {
            "XDG_CONFIG_HOME": str(cache / "config"),
            "GOPATH": str(cache / "gopath"),
            "GOMODCACHE": str(cache / "gomod"),
            "GOCACHE": str(cache / "gobuild"),
            "GOENV": "off",
            "GOTOOLCHAIN": "local",
            "GOWORK": "off",
            "GOFLAGS": "",
            "GOPRIVATE": "",
            "GONOPROXY": "",
            "GONOSUMDB": "",
        }
    )
    if download_modules:
        env["GOPROXY"] = "https://proxy.golang.org"
        env["GOSUMDB"] = "sum.golang.org"
    else:
        env["GOPROXY"] = "off"
        env["GOSUMDB"] = "off"
    return env


def compose_environment(
    docker_config: str,
    docker_host: str,
    extra: Mapping[str, str],
) -> dict[str, str]:
    env: dict[str, str] = {}
    for name in ("HOME", "PATH", "TMPDIR", "LANG"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    env["DOCKER_CONFIG"] = docker_config
    env["DOCKER_HOST"] = docker_host
    env.update(extra)
    return env


def compose_command(
    docker: str,
    files: Sequence[str],
    arguments: Sequence[str],
) -> list[str]:
    command = [docker, "compose", "--env-file", "/dev/null"]
    for path in files:
        command.extend(["-f", path])
    command.extend(arguments)
    return command


def check_go(go: str, env: Mapping[str, str]) -> dict[str, str]:
    print("[check] go telemetry off (isolated config)")
    run_command("go telemetry off", [go, "telemetry", "off"], env, 30)

    result = run_command(
        "go env",
        [
            go,
            "env",
            "-json",
            "GOVERSION",
            "GOOS",
            "GOARCH",
            "GOTOOLCHAIN",
            "GOTELEMETRY",
        ],
        env,
        30,
    )
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        fail(f"go env: invalid JSON: {exc}")
    if not isinstance(info, dict):
        fail("go env: expected a JSON object")

    require_equal(info.get("GOVERSION"), EXPECTED_GO_VERSION, "GOVERSION")
    require_equal(info.get("GOOS"), EXPECTED_GOOS, "GOOS")
    require_equal(info.get("GOARCH"), EXPECTED_GOARCH, "GOARCH")
    require_equal(info.get("GOTOOLCHAIN"), EXPECTED_GOTOOLCHAIN, "GOTOOLCHAIN")
    require_equal(info.get("GOTELEMETRY"), EXPECTED_GOTELEMETRY, "GOTELEMETRY")
    print(
        "[check] Go "
        f"{info['GOVERSION']} {info['GOOS']}/{info['GOARCH']} "
        f"GOTOOLCHAIN={info['GOTOOLCHAIN']} GOTELEMETRY={info['GOTELEMETRY']}"
    )
    return info


def check_module_declarations(go: str, env: Mapping[str, str]) -> None:
    print("[check] go.mod identity and approved direct dependencies")
    result = run_command(
        "go mod edit -json",
        [go, "mod", "edit", "-json"],
        env,
        30,
    )
    try:
        mod = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        fail(f"go mod edit: invalid JSON: {exc}")
    if not isinstance(mod, dict):
        fail("go mod edit: expected a JSON object")

    module_path = (mod.get("Module") or {}).get("Path")
    require_equal(module_path, EXPECTED_MODULE, "module path")
    require_equal(mod.get("Go"), EXPECTED_GO_DIRECTIVE, "go directive")

    direct: dict[str, Any] = {}
    for entry in mod.get("Require") or []:
        if not isinstance(entry, dict):
            fail("go mod edit: malformed Require entry")
        if entry.get("Indirect", False):
            continue
        path = entry.get("Path")
        if path in direct:
            fail(f"go mod edit: duplicate direct requirement {path}")
        direct[path] = entry.get("Version")
    require_equal(direct, APPROVED_DIRECT, "approved direct requirements")


def module_file_snapshot() -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for name in ("go.mod", "go.sum"):
        path = REPO_ROOT / name
        if not path.is_file():
            fail(f"{name} is missing")
        try:
            data = path.read_bytes()
        except OSError as exc:
            fail(f"cannot read {name}: {exc}")
        if name == "go.sum" and not data:
            fail("go.sum is empty")
        snapshot[name] = hashlib.sha256(data).hexdigest()
    return snapshot


def check_modules(go: str, env: Mapping[str, str], download_modules: bool) -> int:
    before = module_file_snapshot()

    if download_modules:
        print("[check] go mod download all (bounded 240s)")
        run_command(
            "go mod download all",
            [go, "mod", "download", "all"],
            env,
            240,
        )
    else:
        print("[check] module cache mode: offline (no download)")

    print("[check] go list -m -mod=readonly all")
    listed = run_command(
        "go list -m -mod=readonly all",
        [go, "list", "-m", "-mod=readonly", "all"],
        env,
        120,
    )
    lines = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    require(len(lines) >= 1, "go list -m: empty module graph")
    first_fields = lines[0].split()
    require_equal(
        first_fields[0] if first_fields else None,
        EXPECTED_MODULE,
        "go list -m: first module",
    )
    module_count = len(lines)
    print(f"[check] module graph: {module_count} modules")

    print("[check] go mod verify")
    verified = run_command(
        "go mod verify",
        [go, "mod", "verify"],
        env,
        120,
    )
    require(
        "all modules verified" in verified.stdout,
        "go mod verify: expected 'all modules verified'",
    )

    after = module_file_snapshot()
    require_equal(after, before, "go.mod/go.sum unchanged by checks")
    return module_count


def check_compose_model(
    docker: str,
    project: str,
    files: Sequence[str],
    values: Mapping[str, str],
    docker_config: str,
    docker_host: str,
    *,
    expect_test_port: bool,
) -> None:
    env = compose_environment(docker_config, docker_host, values)
    result = run_command(
        f"docker compose config ({project})",
        compose_command(docker, files, ["config", "--format", "json"]),
        env,
        60,
    )
    try:
        config = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        fail(f"{project}: compose config returned invalid JSON: {exc}")
    if not isinstance(config, dict):
        fail(f"{project}: compose config did not return an object")

    require_equal(config.get("name"), project, f"{project}: project name")

    services = config.get("services")
    if not isinstance(services, dict):
        fail(f"{project}: services missing")
    require_equal(set(services), {"postgres"}, f"{project}: services")
    service = services["postgres"]
    if not isinstance(service, dict):
        fail(f"{project}: postgres service malformed")

    require_equal(service.get("image"), PG_IMAGE, f"{project}: image")
    require_equal(service.get("platform"), EXPECTED_PLATFORM, f"{project}: platform")
    require(not service.get("container_name"), f"{project}: container_name must not be set")
    require(not service.get("network_mode"), f"{project}: network_mode must not be set")
    require_equal(service.get("restart"), "no", f"{project}: restart")
    require_equal(
        service.get("stop_grace_period"),
        "25s",
        f"{project}: stop_grace_period",
    )

    environment = service.get("environment")
    if not isinstance(environment, dict):
        fail(f"{project}: environment missing")
    require_equal(environment.get("POSTGRES_DB"), MOCK_DB, f"{project}: POSTGRES_DB")
    require_equal(
        environment.get("POSTGRES_USER"),
        MOCK_USER,
        f"{project}: POSTGRES_USER",
    )
    # Never include the password value (even the mock one) in diagnostics.
    require(
        environment.get("POSTGRES_PASSWORD") == MOCK_PASSWORD,
        f"{project}: POSTGRES_PASSWORD does not match the injected mock value",
    )
    require_equal(environment.get("TZ"), "UTC", f"{project}: TZ")
    require_equal(environment.get("PGTZ"), "UTC", f"{project}: PGTZ")

    command = service.get("command")
    if isinstance(command, list):
        command_text = " ".join(str(item) for item in command)
    elif isinstance(command, str):
        command_text = " ".join(shlex.split(command))
    else:
        fail(f"{project}: command missing or malformed")
    require("postgres" in command_text, f"{project}: command does not run postgres")
    for token in (
        "timezone=UTC",
        "log_timezone=UTC",
        "log_statement=none",
        "log_min_error_statement=panic",
        "log_parameter_max_length=0",
        "log_parameter_max_length_on_error=0",
        "logging_collector=off",
    ):
        require(token in command_text, f"{project}: command missing {token}")

    healthcheck = service.get("healthcheck")
    if not isinstance(healthcheck, dict):
        fail(f"{project}: healthcheck missing")
    health_test = healthcheck.get("test")
    if isinstance(health_test, list):
        health_text = " ".join(str(item) for item in health_test)
    elif isinstance(health_test, str):
        health_text = health_test
    else:
        fail(f"{project}: healthcheck test missing or malformed")
    require("pg_isready" in health_text, f"{project}: healthcheck is not pg_isready")
    require(
        "--host=127.0.0.1" in health_text,
        f"{project}: healthcheck host is not 127.0.0.1",
    )
    require("--port=5432" in health_text, f"{project}: healthcheck port is not 5432")
    require_equal(healthcheck.get("interval"), "5s", f"{project}: healthcheck interval")
    require_equal(healthcheck.get("timeout"), "3s", f"{project}: healthcheck timeout")
    require_equal(healthcheck.get("retries"), 10, f"{project}: healthcheck retries")
    require_equal(
        healthcheck.get("start_period"),
        "10s",
        f"{project}: healthcheck start_period",
    )

    logging = service.get("logging")
    if not isinstance(logging, dict):
        fail(f"{project}: logging missing")
    require_equal(logging.get("driver"), "local", f"{project}: logging driver")
    options = logging.get("options")
    if not isinstance(options, dict):
        fail(f"{project}: logging options missing")
    require_equal(str(options.get("max-size")), "10m", f"{project}: max-size")
    require_equal(str(options.get("max-file")), "3", f"{project}: max-file")

    mounts = service.get("volumes")
    if not isinstance(mounts, list):
        fail(f"{project}: service volumes missing")
    require_equal(len(mounts), 1, f"{project}: service volume count")
    mount = mounts[0]
    if not isinstance(mount, dict):
        fail(f"{project}: service volume malformed")
    require_equal(mount.get("type"), "volume", f"{project}: volume type")
    require_equal(mount.get("source"), "postgres_data", f"{project}: volume source")
    require_equal(
        mount.get("target"),
        "/var/lib/postgresql/data",
        f"{project}: volume target",
    )

    ports = service.get("ports")
    if expect_test_port:
        if not isinstance(ports, list):
            fail(f"{project}: test port missing")
        require_equal(len(ports), 1, f"{project}: test port count")
        port = ports[0]
        if not isinstance(port, dict):
            fail(f"{project}: test port malformed")
        require_equal(port.get("target"), 5432, f"{project}: test target port")
        require_equal(port.get("host_ip"), "127.0.0.1", f"{project}: test host IP")
        require_equal(
            str(port.get("published")),
            MOCK_TEST_PORT,
            f"{project}: published test port",
        )
        require_equal(port.get("protocol"), "tcp", f"{project}: test protocol")
    else:
        require(not ports, f"{project}: base model must not publish ports")

    volumes = config.get("volumes")
    if not isinstance(volumes, dict):
        fail(f"{project}: top-level volumes missing")
    require_equal(set(volumes), {"postgres_data"}, f"{project}: volumes")
    volume = volumes["postgres_data"]
    if not isinstance(volume, dict):
        fail(f"{project}: postgres_data volume malformed")
    require_equal(
        volume.get("name"),
        f"{project}_postgres_data",
        f"{project}: volume name",
    )
    require(
        not volume.get("external"),
        f"{project}: postgres_data volume must not be external",
    )

    networks = config.get("networks")
    if not isinstance(networks, dict):
        fail(f"{project}: top-level networks missing")
    require_equal(set(networks), {"default"}, f"{project}: networks")
    network = networks["default"]
    if not isinstance(network, dict):
        fail(f"{project}: default network malformed")
    require_equal(
        network.get("name"),
        f"{project}_default",
        f"{project}: network name",
    )
    require(
        not network.get("external"),
        f"{project}: default network must not be external",
    )

    service_networks = service.get("networks")
    if service_networks is not None:
        if not isinstance(service_networks, dict):
            fail(f"{project}: service networks malformed")
        require_equal(
            set(service_networks),
            {"default"},
            f"{project}: service networks",
        )

    print(f"[check] compose model {project}: ok")


def check_compose_rejections(
    docker: str,
    docker_config: str,
    docker_host: str,
    base_values: Mapping[str, str],
) -> None:
    cases: list[tuple[str, str, Sequence[str], dict[str, str]]] = []
    for variable in (
        "COMPOSE_PROJECT_NAME",
        "PAYMENT_HUB_POSTGRES_DB",
        "PAYMENT_HUB_POSTGRES_USER",
        "PAYMENT_HUB_POSTGRES_PASSWORD",
    ):
        for mode in ("missing", "empty"):
            cases.append((variable, mode, BASE_FILES, dict(base_values)))
    for mode in ("missing", "empty"):
        cases.append(
            (
                "PAYMENT_HUB_TEST_PG_PORT",
                mode,
                TEST_FILES,
                {**base_values, "PAYMENT_HUB_TEST_PG_PORT": MOCK_TEST_PORT},
            )
        )

    require_equal(len(cases), 10, "compose rejection case count")
    for variable, mode, files, values in cases:
        case_values = dict(values)
        if mode == "missing":
            case_values.pop(variable, None)
        else:
            case_values[variable] = ""

        label = f"compose rejection {variable} ({mode})"
        env = compose_environment(docker_config, docker_host, case_values)
        result = run_command(
            label,
            compose_command(docker, files, ["config", "--quiet"]),
            env,
            60,
            allow_failure=True,
        )
        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        detail = sanitize(combined, 300)
        require(
            result.returncode != 0,
            f"{label}: expected a non-zero exit, got 0",
        )
        require(
            variable in combined,
            f"{label}: error output does not mention {variable}: {detail}",
        )
        required_marker = re.search(
            r"required variable\s+[\"']?"
            + re.escape(variable)
            + r"[\"']?(?![\w-])",
            combined,
            re.IGNORECASE,
        ) or re.search(
            r"[\"']?"
            + re.escape(variable)
            + r"[\"']?(?![\w-]).*(?:missing a value|is required)",
            combined,
            re.IGNORECASE,
        )
        require(
            required_marker is not None,
            f"{label}: not a required-variable rejection: {detail}",
        )
        print(f"[check] {label}: rejected as expected")


def run_checks(args: argparse.Namespace) -> None:
    go = resolve_go(args.go)
    cache = prepare_cache(args.cache_dir)
    env = go_environment(cache, args.download_modules)
    print(f"[check] go binary: {go}")

    go_info = check_go(go, env)
    check_module_declarations(go, env)
    module_count = check_modules(go, env, args.download_modules)

    docker = shutil.which("docker")
    if not docker:
        fail("docker CLI not found; not installing")

    with tempfile.TemporaryDirectory(prefix="payment-hub-compose-") as docker_config:
        docker_host = "unix://" + str(Path(docker_config) / "no-daemon.sock")
        version_env = compose_environment(docker_config, docker_host, {})

        docker_result = run_command(
            "docker --version",
            [docker, "--version"],
            version_env,
            30,
        )
        docker_version = first_line(docker_result.stdout)
        require(
            docker_version.lower().startswith("docker version"),
            f"unexpected docker --version output: {sanitize(docker_version)}",
        )
        print(f"[check] docker: {docker_version}")

        compose_result = run_command(
            "docker compose version",
            [docker, "compose", "version"],
            version_env,
            30,
        )
        compose_version = first_line(compose_result.stdout)
        require(
            compose_version.lower().startswith("docker compose version"),
            f"unexpected docker compose version output: {sanitize(compose_version)}",
        )
        print(f"[check] compose: {compose_version}")

        base_values = {
            "COMPOSE_PROJECT_NAME": "payment-hub-setup-reject",
            "PAYMENT_HUB_POSTGRES_DB": MOCK_DB,
            "PAYMENT_HUB_POSTGRES_USER": MOCK_USER,
            "PAYMENT_HUB_POSTGRES_PASSWORD": MOCK_PASSWORD,
        }
        check_compose_model(
            docker,
            "payment-hub-dev",
            BASE_FILES,
            {**base_values, "COMPOSE_PROJECT_NAME": "payment-hub-dev"},
            docker_config,
            docker_host,
            expect_test_port=False,
        )
        check_compose_model(
            docker,
            "payment-hub-demo",
            BASE_FILES,
            {**base_values, "COMPOSE_PROJECT_NAME": "payment-hub-demo"},
            docker_config,
            docker_host,
            expect_test_port=False,
        )
        check_compose_model(
            docker,
            "payment-hub-test-setup-a",
            TEST_FILES,
            {
                **base_values,
                "COMPOSE_PROJECT_NAME": "payment-hub-test-setup-a",
                "PAYMENT_HUB_TEST_PG_PORT": MOCK_TEST_PORT,
            },
            docker_config,
            docker_host,
            expect_test_port=True,
        )
        check_compose_model(
            docker,
            "payment-hub-test-setup-b",
            TEST_FILES,
            {
                **base_values,
                "COMPOSE_PROJECT_NAME": "payment-hub-test-setup-b",
                "PAYMENT_HUB_TEST_PG_PORT": MOCK_TEST_PORT,
            },
            docker_config,
            docker_host,
            expect_test_port=True,
        )
        check_compose_rejections(docker, docker_config, docker_host, base_values)

    if args.download_modules:
        cache_mode = (
            "download via proxy.golang.org with sum.golang.org"
        )
    else:
        cache_mode = (
            "offline (GOPROXY=off, GOSUMDB=off; cache integrity only, "
            "not online authenticity)"
        )
    print(
        "scope: setup-only static checks; no application build/test/race, "
        "no real PostgreSQL/migration/HTTP/restart, no image build/pull/run, "
        "no deployment"
    )
    print(f"module-cache-mode: {cache_mode}")
    print(f"go-version: {go_info['GOVERSION']}")
    print(f"goos: {go_info['GOOS']}")
    print(f"goarch: {go_info['GOARCH']}")
    print(f"gotoolchain: {go_info['GOTOOLCHAIN']}")
    print(f"gotelemetry: {go_info['GOTELEMETRY']}")
    print(f"docker-version: {docker_version}")
    print(f"compose-version: {compose_version}")
    print(f"module-graph-count: {module_count}")
    print(f"checks-passed: {CHECK_COUNT}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate prepared Go module inputs and static Compose configuration. "
            "No application build/test, database, migration, HTTP, image, or deploy."
        )
    )
    parser.add_argument(
        "--go",
        default=None,
        help=(
            "Existing Go executable (absolute path preferred). "
            "Defaults to go on PATH; never installs a toolchain."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        required=True,
        help=(
            "Project-specific external cache directory. Must be absolute and "
            "outside the repository; config/gomod/gobuild/gopath are created there."
        ),
    )
    parser.add_argument(
        "--download-modules",
        action="store_true",
        help=(
            "Explicitly allow go mod download all via proxy.golang.org and "
            "sum.golang.org. Default is offline and only proves cache integrity."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run_checks(args)
    except CheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired as exc:
        print(f"ERROR: command timed out: {sanitize(str(exc))}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: {sanitize(str(exc))}", file=sys.stderr)
        return 1
    except Exception as exc:  # Keep failures concise and avoid dumping context.
        print(
            f"ERROR: unexpected {type(exc).__name__}: {sanitize(str(exc))}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
