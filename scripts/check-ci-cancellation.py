#!/usr/bin/env python3
"""Controlled cancellation check for the database-foundation launcher.

The checker starts the existing safe launcher, waits until this run's
PostgreSQL Compose container is healthy and the launcher is still alive with no
finished report, sends exactly one SIGTERM to the launcher Python process, and
then verifies the launcher's interrupted report and owned-resource cleanup.

This is deliberately a small, single-purpose verifier.  It does not manage
arbitrary processes, does not use process groups, does not remove Docker
resources itself, and does not persist launcher stdout/stderr.  Output pipes
are drained continuously in memory; only a few fixed-prefix facts are retained.
The cancellation report contains only the whitelisted fields below.

Validation stage: PG healthy, launcher alive, report not present.  This does
not prove cancellation while Go tests or application grandchildren are
running, and it does not prove cleanup after a runner hard-kill.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "scripts" / "check-db-foundation.py"
SCHEMA = "payment-hub.ci-cancellation.v1"
LAUNCHER_SCHEMA = "payment-hub.db-foundation.v1"
REPORT_FILENAME = "cancellation-report.json"
STAGE = "pg-healthy"
SIGNAL_NAME = "SIGTERM"
STDOUT_PREFIX = "cancellation-check:"
REPORT_PREFIX = "cancellation-report:"
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROJECT_PREFIX = "payment-hub-test-db-"

BUDGETS = {
    "pg_healthy_seconds": 180.0,
    "exit_after_signal_seconds": 120.0,
    "fault_exit_seconds": 60.0,
    "total_seconds": 360.0,
    "poll_seconds": 1.0,
    "docker_seconds": 15.0,
    "drain_grace_seconds": 10.0,
}

FAILURE_ENUM = {
    "pg-wait": {"timeout", "not-healthy", "launcher-exited", "report-present", "run-dir"},
    "signal": {"not-running"},
    "exit": {"timeout"},
    "launcher": {"missing-report", "report-mismatch", "exit", "start"},
    "cleanup": {"not-pass", "unsafe-command"},
    "resources": {"remain", "unknown"},
    "preexisting": {"lost", "unknown"},
    "docker": {"unavailable", "ambiguous", "not-local"},
    "cli": {"go", "cache", "artifact-root", "report-dir", "report-exists"},
    "deadline": {"expired"},
    "report": {"write"},
    "drain": {"not-joined"},
    "internal": {"unexpected"},
}

PASS_FAILURE = {"stage": "interrupted", "kind": "sigterm"}

# The DockerClient constructor's local-daemon discovery/verification happens in
# default_runtime() before run_check starts its supervised D.  It is a small
# preflight bounded per call by docker_seconds (two calls), not part of D.
PREFLIGHT_DOCKER_SECONDS = 2 * BUDGETS["docker_seconds"]

# Final report serialization/printing is a single bounded filesystem action
# allowed after D; it starts no Docker query and no wait.  This is the only
# post-deadline scheduling allowance.
FINAL_REPORT_MARGIN_SECONDS = 5.0


class CancellationError(RuntimeError):
    """A controlled cancellation-check failure with a fixed stage/kind."""

    def __init__(self, stage: str, kind: str) -> None:
        self.stage = stage
        self.kind = kind
        super().__init__(f"{stage}/{kind}")


@dataclass(frozen=True)
class Config:
    go: str
    cache_dir: Path
    artifact_root: Path
    report_dir: Path
    prepare_images: bool

    @property
    def report_path(self) -> Path:
        return self.report_dir / REPORT_FILENAME


@dataclass
class Runtime:
    process_factory: Callable[[Sequence[str]], Any]
    docker: Any
    drain_factory: Callable[[Any, Any], "OutputDrain"]
    signal_sender: Callable[[int], None]
    clock: Callable[[], float]
    sleep: Callable[[float], None]


def default_cleanup() -> dict[str, Any]:
    return {
        "result": "unknown",
        "compose_down_exit": None,
        "network_removed": False,
        "volume_removed": False,
        "volume_retained": False,
        "repeat_signal_ignored": False,
        "error_count": 0,
        "tool_containers_removed": False,
    }


def default_absence() -> dict[str, bool]:
    return {"containers": False, "network": False, "volume": False}


def derive_project(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise CancellationError("pg-wait", "run-dir")
    return f"{PROJECT_PREFIX}{run_id}"


def discover_run(artifact_root: Path) -> str | None:
    try:
        entries = list(artifact_root.iterdir())
    except OSError as exc:
        raise CancellationError("pg-wait", "run-dir") from exc
    run_dirs = [
        entry
        for entry in entries
        if entry.is_dir()
        and not entry.is_symlink()
        and RUN_ID_RE.fullmatch(entry.name)
    ]
    if len(run_dirs) > 1:
        raise CancellationError("pg-wait", "run-dir")
    if run_dirs and len(entries) != 1:
        raise CancellationError("pg-wait", "run-dir")
    if not run_dirs:
        if entries:
            raise CancellationError("pg-wait", "run-dir")
        return None
    return run_dirs[0].name


# ---------------------------------------------------------------------------
# CLI and path validation
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Start the safe database-foundation launcher, wait for this run's "
            "PostgreSQL container to become healthy, send one SIGTERM to the "
            "launcher PID, and verify the interrupted report plus owned-resource "
            "cleanup.  Raw launcher output is never persisted."
        )
    )
    parser.add_argument("--go", required=True, help="Absolute Go executable.")
    parser.add_argument(
        "--cache-dir", required=True, help="Absolute cache directory outside the repo."
    )
    parser.add_argument(
        "--artifact-root",
        required=True,
        help="Absolute unique empty directory; the launcher creates one run subdirectory.",
    )
    parser.add_argument(
        "--report-dir",
        required=True,
        help=f"Absolute directory for the fixed {REPORT_FILENAME} report.",
    )
    parser.add_argument(
        "--prepare-images",
        action="store_true",
        help="Pass the launcher's explicit image preparation switch through.",
    )
    return parser.parse_args(argv)


def validate_config(args: argparse.Namespace) -> Config:
    go = Path(args.go).expanduser()
    if not go.is_absolute():
        raise CancellationError("cli", "go")
    go = go.resolve()
    if not go.is_file() or not os.access(go, os.X_OK):
        raise CancellationError("cli", "go")

    cache = Path(args.cache_dir).expanduser()
    if not cache.is_absolute():
        raise CancellationError("cli", "cache")
    cache = cache.resolve()
    repo = REPO_ROOT.resolve()
    if cache == repo or repo in cache.parents:
        raise CancellationError("cli", "cache")
    if cache.exists() and not cache.is_dir():
        raise CancellationError("cli", "cache")

    raw_artifact = Path(args.artifact_root).expanduser()
    if not raw_artifact.is_absolute():
        raise CancellationError("cli", "artifact-root")
    if raw_artifact.is_symlink():
        raise CancellationError("cli", "artifact-root")
    artifact = raw_artifact.resolve()

    raw_report = Path(args.report_dir).expanduser()
    if not raw_report.is_absolute():
        raise CancellationError("cli", "report-dir")
    if raw_report.is_symlink():
        raise CancellationError("cli", "report-dir")
    report = raw_report.resolve()

    if cache == artifact or artifact in cache.parents:
        raise CancellationError("cli", "artifact-root")
    if artifact == report or artifact in report.parents:
        raise CancellationError("cli", "artifact-root")
    if artifact.exists() and not artifact.is_dir():
        raise CancellationError("cli", "artifact-root")
    try:
        artifact.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CancellationError("cli", "artifact-root") from exc
    try:
        if any(artifact.iterdir()):
            raise CancellationError("cli", "artifact-root")
    except OSError as exc:
        raise CancellationError("cli", "artifact-root") from exc

    if report.exists() and not report.is_dir():
        raise CancellationError("cli", "report-dir")
    try:
        report.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CancellationError("cli", "report-dir") from exc
    report_file = report / REPORT_FILENAME
    if report_file.exists() or report_file.is_symlink():
        raise CancellationError("cli", "report-exists")

    return Config(
        go=str(go),
        cache_dir=cache,
        artifact_root=artifact,
        report_dir=report,
        prepare_images=bool(args.prepare_images),
    )


def build_launcher_argv(config: Config) -> list[str]:
    argv = [
        sys.executable,
        str(LAUNCHER),
        "--go",
        config.go,
        "--cache-dir",
        str(config.cache_dir),
        "--artifact-root",
        str(config.artifact_root),
        "--cleanup-owned-resources",
    ]
    if config.prepare_images:
        argv.append("--prepare-images")
    return argv


# ---------------------------------------------------------------------------
# Bounded, memory-only output drain
# ---------------------------------------------------------------------------


class OutputDrain:
    """Continuously drain two pipes; retain only canonical whitelisted facts."""

    def __init__(
        self,
        stdout: Any,
        stderr: Any,
        max_events: int = 64,
        max_line: int = 256,
    ) -> None:
        self._streams = [(stdout, "stdout"), (stderr, "stderr")]
        self._max_events = max(1, int(max_events))
        self._max_line = max(1, int(max_line))
        self._events: list[str] = []
        self._lock = threading.Lock()
        self._states: dict[int, dict[str, Any]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="cancellation-output-drain", daemon=True
        )
        self.report_seen = False
        self.truncated = False
        self.error: str | None = None
        self.joined = False

    @property
    def events(self) -> list[str]:
        with self._lock:
            return list(self._events)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        selector = selectors.DefaultSelector()
        registered: dict[int, str] = {}
        for stream, tag in self._streams:
            if stream is None:
                continue
            try:
                fd = stream.fileno()
                os.set_blocking(fd, False)
                selector.register(fd, selectors.EVENT_READ, tag)
            except (AttributeError, OSError, ValueError):
                self.error = "stream"
                continue
            registered[fd] = tag
            self._states[fd] = {"buf": b"", "discard": False}
        try:
            while registered and not self._stop.is_set():
                try:
                    ready = selector.select(timeout=0.1)
                except (OSError, ValueError):
                    break
                for key, _ in ready:
                    fd = key.fd
                    try:
                        chunk = os.read(fd, 4096)
                    except BlockingIOError:
                        continue
                    except (OSError, ValueError):
                        chunk = b""
                    if not chunk:
                        self._flush_partial(fd)
                        try:
                            selector.unregister(fd)
                        except (KeyError, ValueError):
                            pass
                        registered.pop(fd, None)
                        continue
                    self._consume(fd, chunk)
        finally:
            selector.close()

    def _consume(self, fd: int, chunk: bytes) -> None:
        state = self._states.get(fd)
        if state is None:
            return
        data = state["buf"] + chunk
        while True:
            newline = data.find(b"\n")
            if newline < 0:
                if state["discard"]:
                    state["buf"] = b""
                elif len(data) > 4096:
                    state["discard"] = True
                    state["buf"] = b""
                else:
                    state["buf"] = data
                return
            line = data[:newline]
            data = data[newline + 1 :]
            if state["discard"]:
                state["discard"] = False
                continue
            self._record_line(line)

    def _flush_partial(self, fd: int) -> None:
        state = self._states.get(fd)
        if state is None:
            return
        if state["buf"] and not state["discard"]:
            self._record_line(state["buf"])
        state["buf"] = b""
        state["discard"] = False

    def _record_line(self, raw: bytes) -> None:
        if raw.startswith(b"db-foundation-report:"):
            self.report_seen = True
            return
        if len(raw) > self._max_line:
            return
        line = raw.decode("utf-8", "replace").rstrip("\r")
        fact: str | None = None
        match = re.fullmatch(r"run-id: ([0-9a-f]{32})", line)
        if match:
            fact = f"run-id:{match.group(1)}"
        else:
            match = re.fullmatch(
                r"result: (pass|fail stage=[a-z0-9-]+ kind=[a-z0-9-]+)", line
            )
            if match:
                fact = f"result:{match.group(1)}"
            else:
                match = re.fullmatch(r"cleanup-result: (pass|fail|skipped|unknown)", line)
                if match:
                    fact = f"cleanup-result:{match.group(1)}"
                else:
                    match = re.fullmatch(r"ERROR: ([a-z0-9-]+/[a-z0-9-]+)", line)
                    if match:
                        fact = f"error:{match.group(1)}"
        if fact is None:
            return
        with self._lock:
            if len(self._events) >= self._max_events:
                self.truncated = True
                return
            self._events.append(fact)

    def _close_streams(self) -> None:
        for stream, _ in self._streams:
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    def finish(self, timeout: float) -> None:
        limit = max(0.0, float(timeout))
        if not self._thread.is_alive():
            self.joined = True
            self._close_streams()
            return
        started = time.monotonic()
        try:
            self._thread.join(limit)
        except (RuntimeError, ValueError):
            pass
        remaining = max(0.0, limit - (time.monotonic() - started))
        if self._thread.is_alive():
            self._stop.set()
            self._close_streams()
            try:
                self._thread.join(min(2.0, remaining))
            except (RuntimeError, ValueError):
                pass
        self.joined = not self._thread.is_alive()
        if self.joined:
            self._close_streams()
        else:
            self.error = "not-joined"


# ---------------------------------------------------------------------------
# Read-only Docker access
# ---------------------------------------------------------------------------


class DockerClient:
    """Small read-only Docker wrapper; no removal, prune, or daemon changes."""

    def __init__(self) -> None:
        docker = shutil.which("docker")
        if not docker:
            raise CancellationError("docker", "unavailable")
        self.docker = docker
        self.env: dict[str, str] = {}
        for name in (
            "HOME",
            "PATH",
            "TMPDIR",
            "LANG",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "XDG_RUNTIME_DIR",
        ):
            value = os.environ.get(name)
            if value is not None:
                self.env[name] = value
        # This local-daemon preflight is outside run_check's D.  It is bounded
        # by PREFLIGHT_DOCKER_SECONDS across its two calls, plus CLI startup.
        self._verify_local_daemon()

    def _run(self, arguments: Sequence[str], timeout: float | None = None) -> str:
        limit = BUDGETS["docker_seconds"]
        deadline_driven = False
        if timeout is not None:
            requested = max(0.0, float(timeout))
            limit = min(limit, requested)
            deadline_driven = requested <= BUDGETS["docker_seconds"]
        if limit <= 0.0:
            raise CancellationError("deadline", "expired")
        try:
            result = subprocess.run(
                [self.docker, *arguments],
                env=dict(self.env),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=limit,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            if deadline_driven:
                raise CancellationError("deadline", "expired") from exc
            raise CancellationError("docker", "unavailable") from exc
        except OSError as exc:
            raise CancellationError("docker", "unavailable") from exc
        if result.returncode != 0:
            raise CancellationError("docker", "unavailable")
        return result.stdout

    @staticmethod
    def _sub_timeout(timeout: float | None, started: float) -> float | None:
        if timeout is None:
            return None
        return max(0.0, float(timeout) - (time.monotonic() - started))

    def _lines(
        self, arguments: Sequence[str], timeout: float | None = None
    ) -> list[str]:
        return [
            line.strip()
            for line in self._run(arguments, timeout=timeout).splitlines()
            if line.strip()
        ]

    def _verify_local_daemon(self) -> None:
        host = self.env.get("DOCKER_HOST", "")
        if host and not host.startswith("unix://"):
            raise CancellationError("docker", "not-local")
        context = self._run(["context", "show"]).strip()
        if not context:
            raise CancellationError("docker", "not-local")
        raw = self._run(
            [
                "context",
                "inspect",
                context,
                "--format",
                "{{json .Endpoints.docker.Host}}",
            ]
        )
        try:
            endpoint = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CancellationError("docker", "not-local") from exc
        if not isinstance(endpoint, str) or not endpoint.startswith("unix://"):
            raise CancellationError("docker", "not-local")

    def snapshot(self, timeout: float | None = None) -> dict[str, set[str]]:
        started = time.monotonic()
        return {
            "containers": set(
                self._lines(
                    ["ps", "-a", "--format", "{{.ID}}"],
                    timeout=self._sub_timeout(timeout, started),
                )
            ),
            "networks": set(
                self._lines(
                    ["network", "ls", "--format", "{{.ID}}"],
                    timeout=self._sub_timeout(timeout, started),
                )
            ),
            "volumes": set(
                self._lines(
                    ["volume", "ls", "--format", "{{.Name}}"],
                    timeout=self._sub_timeout(timeout, started),
                )
            ),
        }

    def find_pg(self, project: str, timeout: float | None = None) -> list[str]:
        return self._lines(
            [
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                "label=com.docker.compose.service=postgres",
                "--format",
                "{{.ID}}",
            ],
            timeout=timeout,
        )

    def inspect_pg(
        self, container_id: str, timeout: float | None = None
    ) -> dict[str, Any]:
        raw = self._run(
            [
                "inspect",
                "--format",
                "{{.State.Running}}|{{.State.Status}}|"
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                container_id,
            ],
            timeout=timeout,
        ).strip()
        parts = raw.split("|", 2)
        if len(parts) != 3:
            raise CancellationError("docker", "unavailable")
        running_text, status, health = (part.strip() for part in parts)
        if running_text not in ("true", "false"):
            raise CancellationError("docker", "unavailable")
        health_state: dict[str, str] = {}
        if health not in ("", "none", "<no value>"):
            health_state["Status"] = health
        return {
            "Running": running_text == "true",
            "Status": status,
            "Health": health_state,
        }

    def absence(
        self, run_id: str, project: str, timeout: float | None = None
    ) -> dict[str, bool]:
        network_name = f"{project}_default"
        volume_name = f"{project}_postgres_data"
        started = time.monotonic()
        containers_remain = False
        for line in self._lines(
            [
                "ps",
                "-a",
                "--format",
                '{{.ID}}|{{.Label "com.docker.compose.project"}}|'
                '{{.Label "payment-hub.run-id"}}',
            ],
            timeout=self._sub_timeout(timeout, started),
        ):
            parts = line.split("|")
            _, project_label, run_label = (parts + ["", "", ""])[:3]
            if project_label == project or run_label == run_id:
                containers_remain = True
                break
        network_remain = False
        for line in self._lines(
            [
                "network",
                "ls",
                "--format",
                '{{.Name}}|{{.Label "com.docker.compose.project"}}',
            ],
            timeout=self._sub_timeout(timeout, started),
        ):
            parts = line.split("|")
            name, project_label = (parts + ["", ""])[:2]
            if name == network_name or project_label == project:
                network_remain = True
                break
        volume_remain = False
        for line in self._lines(
            [
                "volume",
                "ls",
                "--format",
                '{{.Name}}|{{.Label "com.docker.compose.project"}}',
            ],
            timeout=self._sub_timeout(timeout, started),
        ):
            parts = line.split("|")
            name, project_label = (parts + ["", ""])[:2]
            if name == volume_name or project_label == project:
                volume_remain = True
                break
        return {
            "containers": not containers_remain,
            "network": not network_remain,
            "volume": not volume_remain,
        }


def preexisting_subset(pre: Mapping[str, Any], post: Mapping[str, Any]) -> bool:
    for key in ("containers", "networks", "volumes"):
        before = pre.get(key)
        after = post.get(key)
        if not isinstance(before, set) or not isinstance(after, set):
            return False
        if not before.issubset(after):
            return False
    return True


# ---------------------------------------------------------------------------
# Report loading and verification
# ---------------------------------------------------------------------------


def load_report_stable(path: Path) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise CancellationError("launcher", "missing-report")
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise CancellationError("launcher", "report-mismatch")
        first = path.read_bytes()
        second = path.read_bytes()
    except OSError as exc:
        raise CancellationError("launcher", "missing-report") from exc
    first_hash = hashlib.sha256(first).hexdigest()
    if first != second or hashlib.sha256(second).hexdigest() != first_hash:
        raise CancellationError("launcher", "report-mismatch")
    try:
        data = json.loads(first)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CancellationError("launcher", "report-mismatch") from exc
    if not isinstance(data, dict):
        raise CancellationError("launcher", "report-mismatch")
    return data, first_hash


def _registered_container_ids(report: Mapping[str, Any]) -> set[str]:
    registered: set[str] = set()
    cleanup = report.get("cleanup")
    if isinstance(cleanup, dict):
        records = cleanup.get("tool_containers")
        if isinstance(records, list):
            for item in records:
                if isinstance(item, dict):
                    value = item.get("id")
                    if isinstance(value, str) and value:
                        registered.add(value)
    compose_created = report.get("compose_created")
    if isinstance(compose_created, dict):
        containers = compose_created.get("containers")
        if isinstance(containers, list):
            for value in containers:
                if isinstance(value, str) and value:
                    registered.add(value)
    preflight = report.get("preflight")
    if isinstance(preflight, dict):
        created = preflight.get("created")
        if isinstance(created, dict):
            value = created.get("container_id")
            if isinstance(value, str) and value:
                registered.add(value)
    return registered


def _container_targets(argv: Sequence[str], index: int) -> list[str]:
    targets: list[str] = []
    position = index + 1
    while position < len(argv):
        token = argv[position]
        if token in ("--time", "-t"):
            position += 2
            continue
        if token.startswith("--time="):
            position += 1
            continue
        if token.startswith("-"):
            position += 1
            continue
        targets.append(token)
        position += 1
    return targets


def validate_cleanup_commands(report: Mapping[str, Any], project: str) -> None:
    commands = report.get("commands")
    if not isinstance(commands, list):
        raise CancellationError("cleanup", "unsafe-command")
    registered = _registered_container_ids(report)
    volume_name = f"{project}_postgres_data"
    network_name = f"{project}_default"
    for record in commands:
        if not isinstance(record, dict):
            raise CancellationError("cleanup", "unsafe-command")
        argv = record.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) for item in argv)
        ):
            raise CancellationError("cleanup", "unsafe-command")
        if any(token == "prune" for token in argv):
            raise CancellationError("cleanup", "unsafe-command")
        if any(
            token in ("--host", "-H", "--context")
            or token.startswith("--host=")
            or token.startswith("--context=")
            for token in argv
        ):
            raise CancellationError("cleanup", "unsafe-command")
        if "compose" in argv:
            mutating = any(
                token in ("down", "rm", "stop", "kill") for token in argv
            )
            if not mutating:
                continue
            project_values: list[str] = []
            position = 0
            while position < len(argv):
                token = argv[position]
                if token == "-p":
                    if position + 1 >= len(argv):
                        raise CancellationError("cleanup", "unsafe-command")
                    project_values.append(argv[position + 1])
                    position += 2
                    continue
                if token == "--project-name":
                    if position + 1 >= len(argv):
                        raise CancellationError("cleanup", "unsafe-command")
                    project_values.append(argv[position + 1])
                    position += 2
                    continue
                if token.startswith("--project-name="):
                    project_values.append(token.split("=", 1)[1])
                position += 1
            if project_values != [project]:
                raise CancellationError("cleanup", "unsafe-command")
            if "down" in argv and any(
                token in ("-v", "--volumes") or token.startswith("--volumes=")
                for token in argv
            ):
                raise CancellationError("cleanup", "unsafe-command")
            for token in argv:
                if token.startswith("label=com.docker.compose.project="):
                    if token.split("=", 2)[2] != project:
                        raise CancellationError("cleanup", "unsafe-command")
            continue
        if Path(argv[0]).name != "docker":
            continue
        if "volume" in argv and "rm" in argv:
            index = argv.index("rm")
            targets = [item for item in argv[index + 1 :] if not item.startswith("-")]
            if targets != [volume_name]:
                raise CancellationError("cleanup", "unsafe-command")
            continue
        if "network" in argv and "rm" in argv:
            index = argv.index("rm")
            targets = [item for item in argv[index + 1 :] if not item.startswith("-")]
            if targets != [network_name]:
                raise CancellationError("cleanup", "unsafe-command")
            continue
        for verb in ("rm", "stop"):
            if verb not in argv[1:]:
                continue
            index = argv.index(verb)
            targets = _container_targets(argv, index)
            if not targets:
                raise CancellationError("cleanup", "unsafe-command")
            for target in targets:
                if target not in registered:
                    raise CancellationError("cleanup", "unsafe-command")
            break


def summarize_cleanup(report: Mapping[str, Any]) -> dict[str, Any]:
    summary = default_cleanup()
    cleanup = report.get("cleanup")
    if not isinstance(cleanup, dict):
        return summary
    result = cleanup.get("result")
    summary["result"] = result if result in ("pass", "fail", "skipped") else "unknown"
    exit_code = cleanup.get("compose_down_exit")
    summary["compose_down_exit"] = (
        exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None
    )
    summary["network_removed"] = cleanup.get("network_removed") is True
    summary["volume_removed"] = (
        cleanup.get("volume_removed") is True or cleanup.get("volume_absent") is True
    )
    summary["volume_retained"] = cleanup.get("volume_retained") is True
    summary["repeat_signal_ignored"] = cleanup.get("repeat_signal_ignored") is True
    errors = cleanup.get("errors")
    if isinstance(errors, list):
        summary["error_count"] = len(errors)
    else:
        summary["error_count"] = 0
        summary["result"] = "unknown"
    tools = cleanup.get("tool_containers")
    if isinstance(tools, list):
        summary["tool_containers_removed"] = all(
            isinstance(item, dict)
            and (item.get("removed") is True or item.get("absent") is True)
            for item in tools
        )
    else:
        summary["tool_containers_removed"] = False
    return summary


def _cleanup_ok(cleanup: Mapping[str, Any]) -> bool:
    return (
        cleanup.get("result") == "pass"
        and cleanup.get("error_count") == 0
        and cleanup.get("compose_down_exit") == 0
        and cleanup.get("network_removed") is True
        and cleanup.get("volume_removed") is True
        and cleanup.get("volume_retained") is False
        and cleanup.get("repeat_signal_ignored") is False
        and cleanup.get("tool_containers_removed") is True
    )


def verify_launcher_report(report: Mapping[str, Any], run_id: str, project: str) -> None:
    if not isinstance(report, dict):
        raise CancellationError("launcher", "report-mismatch")
    if report.get("schema") != LAUNCHER_SCHEMA:
        raise CancellationError("launcher", "report-mismatch")
    if report.get("run_id") != run_id or report.get("project") != project:
        raise CancellationError("launcher", "report-mismatch")
    if report.get("result") != "fail":
        raise CancellationError("launcher", "report-mismatch")
    failure = report.get("failure")
    if (
        not isinstance(failure, dict)
        or failure.get("stage") != "interrupted"
        or failure.get("kind") != "sigterm"
    ):
        raise CancellationError("launcher", "report-mismatch")
    raw_cleanup = report.get("cleanup")
    if not isinstance(raw_cleanup, dict):
        raise CancellationError("cleanup", "not-pass")
    required = {
        "result",
        "errors",
        "repeat_signal_ignored",
        "compose_down_exit",
        "network_removed",
        "volume_retained",
        "tool_containers",
    }
    if not required.issubset(raw_cleanup):
        raise CancellationError("cleanup", "not-pass")
    if raw_cleanup.get("volume_name") != f"{project}_postgres_data":
        raise CancellationError("cleanup", "not-pass")
    cleanup = summarize_cleanup(report)
    if not _cleanup_ok(cleanup):
        raise CancellationError("cleanup", "not-pass")
    validate_cleanup_commands(report, project)


# ---------------------------------------------------------------------------
# Cancellation report
# ---------------------------------------------------------------------------


def _safe_failure(stage: str, kind: str) -> dict[str, str]:
    if stage in FAILURE_ENUM and kind in FAILURE_ENUM[stage]:
        return {"stage": stage, "kind": kind}
    return {"stage": "internal", "kind": "unexpected"}


def _safe_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 3)
    return None


def _safe_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _normalize_cleanup(value: Any) -> dict[str, Any]:
    summary = default_cleanup()
    if not isinstance(value, Mapping):
        return summary
    result = value.get("result")
    summary["result"] = result if result in ("pass", "fail", "skipped") else "unknown"
    summary["compose_down_exit"] = _safe_int(value.get("compose_down_exit"))
    summary["network_removed"] = value.get("network_removed") is True
    summary["volume_removed"] = (
        value.get("volume_removed") is True or value.get("volume_absent") is True
    )
    summary["volume_retained"] = value.get("volume_retained") is True
    summary["repeat_signal_ignored"] = value.get("repeat_signal_ignored") is True
    error_count = value.get("error_count")
    summary["error_count"] = (
        error_count
        if isinstance(error_count, int) and not isinstance(error_count, bool) and error_count >= 0
        else 0
    )
    summary["tool_containers_removed"] = value.get("tool_containers_removed") is True
    return summary


def build_cancellation_report(
    *,
    result: str,
    run_id: str | None,
    project: str | None,
    launcher_exit: int | None,
    failure: tuple[str, str] | None,
    cleanup: Mapping[str, Any] | None,
    resource_absence: Mapping[str, Any] | None,
    preexisting_resource_preserved: bool,
    elapsed_seconds: float | None,
    pg_healthy_seconds: float | None,
    exit_after_signal_seconds: float | None,
    input_sha256: str | None,
) -> dict[str, Any]:
    safe_run_id = (
        run_id
        if isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id)
        else None
    )
    derived = f"{PROJECT_PREFIX}{safe_run_id}" if safe_run_id else None
    safe_project = derived if safe_run_id and project == derived else None
    safe_result = "pass" if result == "pass" else "fail"
    if safe_result == "pass" and (safe_run_id is None or safe_project is None):
        safe_result = "fail"
        safe_failure = _safe_failure("internal", "unexpected")
    elif safe_result == "pass":
        safe_failure = dict(PASS_FAILURE)
    else:
        pair = (
            failure
            if isinstance(failure, tuple) and len(failure) == 2
            else ("internal", "unexpected")
        )
        safe_failure = _safe_failure(*pair)
    absence = default_absence()
    if isinstance(resource_absence, Mapping):
        for key in absence:
            absence[key] = resource_absence.get(key) is True
    elapsed = _safe_float(elapsed_seconds)
    if elapsed is None:
        elapsed = 0.0
    digest = input_sha256 if isinstance(input_sha256, str) and SHA256_RE.fullmatch(input_sha256) else None
    return {
        "schema": SCHEMA,
        "result": safe_result,
        "run_id": safe_run_id,
        "project": safe_project,
        "stage": STAGE,
        "signal": SIGNAL_NAME,
        "launcher_exit": _safe_int(launcher_exit),
        "failure": safe_failure,
        "cleanup": _normalize_cleanup(cleanup),
        "resource_absence": absence,
        "preexisting_resource_preserved": bool(preexisting_resource_preserved),
        "elapsed_seconds": elapsed,
        "timings": {
            "pg_healthy_seconds": _safe_float(pg_healthy_seconds),
            "exit_after_signal_seconds": _safe_float(exit_after_signal_seconds),
        },
        "input_sha256": digest,
    }


def write_report(path: Path, data: Mapping[str, Any]) -> None:
    created = False
    try:
        with path.open("x", encoding="utf-8") as handle:
            created = True
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except (OSError, TypeError, ValueError) as exc:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise CancellationError("report", "write") from exc


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class _RunState:
    result: str = "fail"
    failure: tuple[str, str] = ("internal", "unexpected")
    run_id: str | None = None
    project: str | None = None
    launcher_exit: int | None = None
    signal_attempted: bool = False
    signal_sent: bool = False
    pg_seconds: float | None = None
    exit_seconds: float | None = None
    input_hash: str | None = None
    report: dict[str, Any] | None = None
    cleanup: dict[str, Any] = field(default_factory=default_cleanup)
    absence: dict[str, bool] = field(default_factory=default_absence)
    preexisting: bool = False
    absence_known: bool = False
    preexisting_known: bool = False
    pre: dict[str, set[str]] | None = None
    post_attempted: bool = False


def _remaining(runtime: Runtime, deadline: float) -> float:
    return max(0.0, deadline - runtime.clock())


def _ensure_time(runtime: Runtime, deadline: float) -> None:
    if _remaining(runtime, deadline) <= 0.0:
        raise CancellationError("deadline", "expired")


def _state_healthy(state: Mapping[str, Any]) -> bool:
    health = state.get("Health")
    return (
        state.get("Status") == "running"
        and state.get("Running") is True
        and isinstance(health, dict)
        and health.get("Status") == "healthy"
    )


def _wait_for_pg(
    config: Config,
    runtime: Runtime,
    proc: Any,
    start: float,
    deadline: float,
    state: _RunState,
) -> tuple[str, str, float]:
    pg_deadline = min(start + BUDGETS["pg_healthy_seconds"], deadline)
    while True:
        now = runtime.clock()
        if now >= deadline:
            raise CancellationError("deadline", "expired")
        if now >= pg_deadline:
            raise CancellationError("pg-wait", "timeout")
        run_id = discover_run(config.artifact_root)
        if run_id is not None:
            project = derive_project(run_id)
            state.run_id = run_id
            state.project = project
        if proc.poll() is not None:
            raise CancellationError("pg-wait", "launcher-exited")
        if run_id is not None:
            report_path = config.artifact_root / run_id / "db-foundation.json"
            if report_path.exists():
                raise CancellationError("pg-wait", "report-present")
            remaining = _remaining(runtime, deadline)
            if remaining <= 0.0:
                raise CancellationError("deadline", "expired")
            ids = runtime.docker.find_pg(project, timeout=remaining)
            if runtime.clock() >= deadline:
                raise CancellationError("deadline", "expired")
            if runtime.clock() >= pg_deadline:
                raise CancellationError("pg-wait", "timeout")
            if len(ids) > 1:
                raise CancellationError("docker", "ambiguous")
            if len(ids) == 1:
                remaining = _remaining(runtime, deadline)
                if remaining <= 0.0:
                    raise CancellationError("deadline", "expired")
                pg_state = runtime.docker.inspect_pg(ids[0], timeout=remaining)
                if runtime.clock() >= deadline:
                    raise CancellationError("deadline", "expired")
                if runtime.clock() >= pg_deadline:
                    raise CancellationError("pg-wait", "timeout")
                if _state_healthy(pg_state):
                    return run_id, project, round(runtime.clock() - start, 3)
                if (
                    pg_state.get("Status") in ("exited", "dead")
                    or pg_state.get("Running") is False
                ):
                    raise CancellationError("pg-wait", "not-healthy")
                health = pg_state.get("Health")
                if isinstance(health, dict) and health.get("Status") == "unhealthy":
                    raise CancellationError("pg-wait", "not-healthy")
        sleep_until = min(pg_deadline, deadline)
        runtime.sleep(
            min(BUDGETS["poll_seconds"], max(0.0, sleep_until - runtime.clock()))
        )


def _recheck_cancelable(
    config: Config,
    runtime: Runtime,
    proc: Any,
    run_id: str,
    project: str,
    deadline: float,
) -> None:
    _ensure_time(runtime, deadline)
    if proc.poll() is not None:
        raise CancellationError("pg-wait", "launcher-exited")
    if discover_run(config.artifact_root) != run_id:
        raise CancellationError("pg-wait", "run-dir")
    report_path = config.artifact_root / run_id / "db-foundation.json"
    if report_path.exists():
        raise CancellationError("pg-wait", "report-present")
    remaining = _remaining(runtime, deadline)
    if remaining <= 0.0:
        raise CancellationError("deadline", "expired")
    ids = runtime.docker.find_pg(project, timeout=remaining)
    if runtime.clock() >= deadline:
        raise CancellationError("deadline", "expired")
    if len(ids) != 1:
        raise CancellationError("pg-wait", "not-healthy")
    remaining = _remaining(runtime, deadline)
    if remaining <= 0.0:
        raise CancellationError("deadline", "expired")
    pg_state = runtime.docker.inspect_pg(ids[0], timeout=remaining)
    if runtime.clock() >= deadline:
        raise CancellationError("deadline", "expired")
    if not _state_healthy(pg_state):
        raise CancellationError("pg-wait", "not-healthy")


def _wait_exit(
    proc: Any, seconds: float, runtime: Runtime, deadline: float
) -> float:
    began = runtime.clock()
    limit = min(began + seconds, deadline)
    while True:
        if proc.poll() is not None:
            return round(runtime.clock() - began, 3)
        now = runtime.clock()
        if now >= limit:
            return round(runtime.clock() - began, 3)
        runtime.sleep(min(BUDGETS["poll_seconds"], max(0.0, limit - now)))


def _finish_drain(drain: Any, runtime: Runtime, deadline: float) -> None:
    if drain is None or getattr(drain, "joined", False):
        return
    drain.finish(min(BUDGETS["drain_grace_seconds"], _remaining(runtime, deadline)))


def _post_checks(state: _RunState, runtime: Runtime, deadline: float) -> None:
    if state.run_id is None or state.post_attempted:
        return
    state.post_attempted = True
    post: dict[str, set[str]] | None = None
    remaining = _remaining(runtime, deadline)
    if state.pre is not None:
        if remaining <= 0.0:
            state.preexisting = False
            state.preexisting_known = False
        else:
            try:
                post = runtime.docker.snapshot(timeout=remaining)
                if _remaining(runtime, deadline) <= 0.0:
                    post = None
                    state.preexisting = False
                    state.preexisting_known = False
                else:
                    state.preexisting = preexisting_subset(state.pre, post)
                    state.preexisting_known = True
            except Exception:
                post = None
                state.preexisting = False
                state.preexisting_known = False
    remaining = _remaining(runtime, deadline)
    if remaining <= 0.0:
        state.absence = default_absence()
        state.absence_known = False
        return
    try:
        absence = runtime.docker.absence(
            state.run_id, state.project, timeout=remaining
        )
        if _remaining(runtime, deadline) <= 0.0:
            state.absence = default_absence()
            state.absence_known = False
            return
        if post is not None:
            registered = _registered_container_ids(state.report or {})
            if registered & post.get("containers", set()):
                absence["containers"] = False
            if f"{state.project}_postgres_data" in post.get("volumes", set()):
                absence["volume"] = False
        state.absence = absence
        state.absence_known = True
    except Exception:
        state.absence = default_absence()
        state.absence_known = False


def _print_summary(state: _RunState, config: Config, data: Mapping[str, Any]) -> None:
    print(f"{STDOUT_PREFIX} run-id {state.run_id or 'unknown'}")
    print(f"{STDOUT_PREFIX} project {state.project or 'unknown'}")
    print(f"{STDOUT_PREFIX} stage {STAGE}")
    print(f"{STDOUT_PREFIX} signal {SIGNAL_NAME}")
    print(
        f"{STDOUT_PREFIX} launcher-exit "
        f"{state.launcher_exit if state.launcher_exit is not None else 'unknown'}"
    )
    print(f"{STDOUT_PREFIX} result {state.result}")
    launcher_report = (
        config.artifact_root / state.run_id / "db-foundation.json"
        if state.run_id is not None
        else "unknown"
    )
    print(f"{STDOUT_PREFIX} launcher-report {launcher_report}")
    print(f"{STDOUT_PREFIX} report {config.report_path}")
    print(
        f"{REPORT_PREFIX} "
        f"{json.dumps(data, separators=(',', ':'), sort_keys=True)}"
    )


def run_check(config: Config, runtime: Runtime) -> int:
    start = runtime.clock()
    deadline = start + BUDGETS["total_seconds"]
    state = _RunState()
    proc: Any = None
    drain: Any = None
    try:
        remaining = _remaining(runtime, deadline)
        if remaining <= 0.0:
            raise CancellationError("deadline", "expired")
        state.pre = runtime.docker.snapshot(timeout=remaining)
        _ensure_time(runtime, deadline)
        try:
            proc = runtime.process_factory(build_launcher_argv(config))
        except OSError as exc:
            raise CancellationError("launcher", "start") from exc
        drain = runtime.drain_factory(proc.stdout, proc.stderr)
        drain.start()
        run_id, project, pg_seconds = _wait_for_pg(
            config, runtime, proc, start, deadline, state
        )
        state.pg_seconds = pg_seconds
        _recheck_cancelable(config, runtime, proc, run_id, project, deadline)
        state.signal_attempted = True
        try:
            runtime.signal_sender(proc.pid)
        except ProcessLookupError as exc:
            raise CancellationError("signal", "not-running") from exc
        state.signal_sent = True
        _ensure_time(runtime, deadline)
        state.exit_seconds = _wait_exit(
            proc, BUDGETS["exit_after_signal_seconds"], runtime, deadline
        )
        state.launcher_exit = proc.poll()
        if state.launcher_exit is None:
            if _remaining(runtime, deadline) <= 0.0:
                raise CancellationError("deadline", "expired")
            raise CancellationError("exit", "timeout")
        _ensure_time(runtime, deadline)
        _finish_drain(drain, runtime, deadline)
        _ensure_time(runtime, deadline)
        report_path = config.artifact_root / run_id / "db-foundation.json"
        report, input_hash = load_report_stable(report_path)
        state.report = report
        state.input_hash = input_hash
        verify_launcher_report(report, run_id, project)
        if state.launcher_exit != 1:
            raise CancellationError("launcher", "exit")
        state.cleanup = summarize_cleanup(report)
        _post_checks(state, runtime, deadline)
        _ensure_time(runtime, deadline)
        if not state.preexisting:
            raise CancellationError(
                "preexisting",
                "lost" if state.preexisting_known else "unknown",
            )
        if not all(state.absence.values()):
            raise CancellationError(
                "resources",
                "remain" if state.absence_known else "unknown",
            )
        _ensure_time(runtime, deadline)
        state.result = "pass"
        state.failure = ("interrupted", "sigterm")
    except CancellationError as exc:
        state.result = "fail"
        state.failure = (exc.stage, exc.kind)
    except Exception:
        state.result = "fail"
        state.failure = ("internal", "unexpected")
    finally:
        if (
            state.result != "pass"
            and proc is not None
            and not state.signal_attempted
            and proc.poll() is None
        ):
            state.signal_attempted = True
            try:
                runtime.signal_sender(proc.pid)
                state.signal_sent = True
                state.exit_seconds = _wait_exit(
                    proc, BUDGETS["fault_exit_seconds"], runtime, deadline
                )
            except ProcessLookupError:
                pass
            except Exception:
                pass
        if state.launcher_exit is None and proc is not None:
            try:
                state.launcher_exit = proc.poll()
            except Exception:
                pass
        if drain is not None:
            if not getattr(drain, "joined", False):
                try:
                    drain.finish(min(BUDGETS["drain_grace_seconds"], _remaining(runtime, deadline)))
                except Exception:
                    pass
            if not getattr(drain, "joined", False):
                if state.result == "pass" or state.failure == (
                    "internal",
                    "unexpected",
                ):
                    state.result = "fail"
                    state.failure = ("drain", "not-joined")
        if state.launcher_exit is None and proc is not None:
            try:
                state.launcher_exit = proc.poll()
            except Exception:
                pass
        if state.run_id is None:
            try:
                found = discover_run(config.artifact_root)
                if found is not None:
                    state.run_id = found
                    state.project = derive_project(found)
            except CancellationError:
                pass
        if state.run_id is not None:
            report_path = config.artifact_root / state.run_id / "db-foundation.json"
            if state.report is None and report_path.exists():
                try:
                    report, input_hash = load_report_stable(report_path)
                    state.report = report
                    if state.input_hash is None:
                        state.input_hash = input_hash
                except CancellationError:
                    pass
            if state.report is not None:
                state.cleanup = summarize_cleanup(state.report)
            _post_checks(state, runtime, deadline)
        if state.result == "pass":
            if _remaining(runtime, deadline) <= 0.0:
                state.result = "fail"
                state.failure = ("deadline", "expired")
            elif (
                state.launcher_exit != 1
                or not state.signal_sent
                or not state.preexisting
                or not all(state.absence.values())
                or not _cleanup_ok(state.cleanup)
                or state.input_hash is None
            ):
                state.result = "fail"
                state.failure = ("cleanup", "not-pass")
        # The final report write below is a single bounded filesystem action
        # allowed within FINAL_REPORT_MARGIN_SECONDS after D; it starts no
        # Docker query and no wait.
        final_report_deadline = deadline + FINAL_REPORT_MARGIN_SECONDS
        if state.result == "pass" and runtime.clock() > final_report_deadline:
            state.result = "fail"
            state.failure = ("deadline", "expired")
        if state.failure == ("deadline", "expired"):
            state.absence = default_absence()
            state.absence_known = False
            state.preexisting = False
            state.preexisting_known = False
        data = build_cancellation_report(
            result=state.result,
            run_id=state.run_id,
            project=state.project,
            launcher_exit=state.launcher_exit,
            failure=state.failure,
            cleanup=state.cleanup,
            resource_absence=state.absence,
            preexisting_resource_preserved=state.preexisting,
            elapsed_seconds=runtime.clock() - start,
            pg_healthy_seconds=state.pg_seconds,
            exit_after_signal_seconds=state.exit_seconds,
            input_sha256=state.input_hash,
        )
        try:
            write_report(config.report_path, data)
        except CancellationError:
            print("ERROR: report/write", file=sys.stderr)
            print(f"{STDOUT_PREFIX} result fail")
            return 1
        if state.result != "pass":
            print(f"ERROR: {state.failure[0]}/{state.failure[1]}", file=sys.stderr)
        _print_summary(state, config, data)
    return 0 if state.result == "pass" else 1


def send_sigterm(pid: int) -> None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ProcessLookupError(pid)
    os.kill(pid, signal.SIGTERM)


def _start_launcher(argv: Sequence[str]) -> Any:
    return subprocess.Popen(
        list(argv),
        cwd=str(REPO_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )


def default_runtime() -> Runtime:
    return Runtime(
        process_factory=_start_launcher,
        docker=DockerClient(),
        drain_factory=lambda stdout, stderr: OutputDrain(stdout, stderr),
        signal_sender=send_sigterm,
        clock=time.monotonic,
        sleep=time.sleep,
    )


def _emit_early_failure(
    args: argparse.Namespace,
    exc: CancellationError,
    config: Config | None = None,
) -> None:
    try:
        if config is not None:
            report_dir = config.report_dir
            path = config.report_path
        else:
            raw = getattr(args, "report_dir", None)
            if not raw:
                return
            raw_report_dir = Path(raw).expanduser()
            if not raw_report_dir.is_absolute() or raw_report_dir.is_symlink():
                return
            report_dir = raw_report_dir.resolve()
            raw_artifact = getattr(args, "artifact_root", None)
            if raw_artifact:
                artifact = Path(raw_artifact).expanduser()
                if artifact.is_absolute():
                    artifact = artifact.resolve()
                    if report_dir == artifact or artifact in report_dir.parents:
                        return
            path = report_dir / REPORT_FILENAME
        report_dir.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return
        data = build_cancellation_report(
            result="fail",
            run_id=None,
            project=None,
            launcher_exit=None,
            failure=(exc.stage, exc.kind),
            cleanup=default_cleanup(),
            resource_absence=default_absence(),
            preexisting_resource_preserved=False,
            elapsed_seconds=0.0,
            pg_healthy_seconds=None,
            exit_after_signal_seconds=None,
            input_sha256=None,
        )
        write_report(path, data)
        print(f"{STDOUT_PREFIX} report {path}")
        print(
            f"{REPORT_PREFIX} "
            f"{json.dumps(data, separators=(',', ':'), sort_keys=True)}"
        )
    except Exception:
        return


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = validate_config(args)
    except CancellationError as exc:
        print(f"ERROR: {exc.stage}/{exc.kind}", file=sys.stderr)
        _emit_early_failure(args, exc)
        print(f"{STDOUT_PREFIX} result fail")
        return 1
    except Exception:
        exc = CancellationError("internal", "unexpected")
        print("ERROR: internal/unexpected", file=sys.stderr)
        _emit_early_failure(args, exc)
        print(f"{STDOUT_PREFIX} result fail")
        return 1
    try:
        runtime = default_runtime()
    except CancellationError as exc:
        print(f"ERROR: {exc.stage}/{exc.kind}", file=sys.stderr)
        _emit_early_failure(args, exc, config)
        print(f"{STDOUT_PREFIX} result fail")
        return 1
    except Exception:
        exc = CancellationError("internal", "unexpected")
        print("ERROR: internal/unexpected", file=sys.stderr)
        _emit_early_failure(args, exc, config)
        print(f"{STDOUT_PREFIX} result fail")
        return 1
    try:
        return run_check(config, runtime)
    except Exception:
        exc = CancellationError("internal", "unexpected")
        print("ERROR: internal/unexpected", file=sys.stderr)
        _emit_early_failure(args, exc, config)
        print(f"{STDOUT_PREFIX} result fail")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
