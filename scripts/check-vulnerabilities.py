#!/usr/bin/env python3
"""Isolated govulncheck lane for CI hardening.

The script installs one pinned tool only when ``--prepare-tool`` is explicitly
given, runs exactly one source/symbol scan with the integration build tag, and
writes a fixed, whitelisted vulnerability report. It never prints raw tool
output, environment values, OSV details, traces, or source positions.

Only Python's standard library is used. The scan is not run by the unit tests;
they mock every subprocess and use temporary repositories.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent

SCHEMA = "payment-hub.ci.vulnerability-report.v1"
REPORT_VERSION = 1
REPORT_NAME = "vulnerability-report.json"

TOOL_BINARY = "govulncheck"
TOOL_MODULE = "golang.org/x/vuln"
TOOL_PACKAGE = "golang.org/x/vuln/cmd/govulncheck"
TOOL_VERSION = "v1.9.0"
TOOL_COMMIT = "e672cbe8fbc8c76e28fcb6db0efd9ce354e0dac5"

EXPECTED_GO_VERSION = "go1.27.4"
EXPECTED_GOOS = "linux"
EXPECTED_GOARCH = "amd64"
EXPECTED_CGO_ENABLED = "1"
EXPECTED_PROTOCOL_VERSION = "v1.0.0"
EXPECTED_SCANNER_NAME = "govulncheck"
EXPECTED_SCANNER_VERSION = TOOL_VERSION
VULN_DB = "https://vuln.go.dev"

SCAN_ARGS = (
    "-json",
    "-mode=source",
    "-scan=symbol",
    "-tags=integration",
    "-test",
    "./...",
)

INSTALL_TIMEOUT = 180
SCAN_TIMEOUT = 180
VERSION_TIMEOUT = 30

OSV_ID_RE = re.compile(r"^GO-\d{4}-\d+$")
MODULE_FILES = ("go.mod", "go.sum")

CONFIG_KEYS = {
    "protocol_version",
    "scanner_name",
    "scanner_version",
    "db",
    "db_last_modified",
    "go_version",
    "scan_level",
    "scan_mode",
}
FINDING_KEYS = {"osv", "fixed_version", "trace"}
FRAME_KEYS = {"module", "version", "package", "function", "receiver", "position"}
NON_CONFIG_MESSAGE_KEYS = {"progress", "SBOM", "osv", "finding"}

REPORT_KEYS = {
    "schema",
    "version",
    "result",
    "tool",
    "tool_verified",
    "expected_go_version",
    "go_version",
    "source_hash",
    "module_unchanged",
    "scan",
    "osv_ids",
    "reachable_ids",
    "unreachable_ids",
    "reachable_count",
    "unreachable_count",
    "failure_class",
    "command_exit",
    "duration_seconds",
}
TOOL_KEYS = {"name", "module", "version", "commit"}
SCAN_KEYS = {
    "mode",
    "level",
    "tags",
    "test",
    "db",
    "pattern",
    "cgo_enabled",
    "goos",
    "goarch",
}

FAILURE_CLASSES = {
    "tool_missing",
    "tool_version_mismatch",
    "go_toolchain_mismatch",
    "install_failed",
    "install_timeout",
    "scan_failed",
    "scan_timeout",
    "invalid_output",
    "module_missing",
    "module_changed",
    "artifact_write_failed",
    "internal_error",
}


class CheckFailure(Exception):
    """Fixed, non-sensitive failure classification."""

    def __init__(self, failure_class: str):
        super().__init__(failure_class)
        self.failure_class = failure_class


class InvalidOutput(CheckFailure):
    def __init__(self):
        super().__init__("invalid_output")


class CommandTimeout(Exception):
    pass


class CommandOSError(Exception):
    pass


class ParsedScan:
    __slots__ = ("go_version", "reachable", "unreachable")

    def __init__(
        self,
        go_version: str,
        reachable: frozenset[str],
        unreachable: frozenset[str],
    ):
        self.go_version = go_version
        self.reachable = reachable
        self.unreachable = unreachable


def _absolute_path(value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("must be an absolute path")
    return str(path)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one pinned govulncheck source/symbol scan with isolated Go "
            "caches and write a fixed, non-sensitive vulnerability report."
        )
    )
    parser.add_argument("--go", required=True, type=_absolute_path)
    parser.add_argument("--cache-dir", required=True, type=_absolute_path)
    parser.add_argument("--tool-dir", required=True, type=_absolute_path)
    parser.add_argument("--artifact-root", required=True, type=_absolute_path)
    parser.add_argument("--prepare-tool", action="store_true")
    return parser.parse_args(argv)


def module_hashes(repo: Path) -> dict[str, str]:
    repo = Path(repo)
    hashes: dict[str, str] = {}
    try:
        for name in MODULE_FILES:
            path = repo / name
            if not path.is_file():
                raise CheckFailure("module_missing")
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        raise CheckFailure("module_missing") from None
    return hashes


def build_env(go_path: Path, cache_dir: Path) -> dict[str, str]:
    go_path = Path(go_path)
    cache = Path(cache_dir)
    directories = {
        "home": cache / "home",
        "tmp": cache / "tmp",
        "xdg_cache": cache / "xdg-cache",
        "xdg_config": cache / "xdg-config",
        "xdg_data": cache / "xdg-data",
        "gopath": cache / "gopath",
        "gomodcache": cache / "gomodcache",
        "gocache": cache / "gocache",
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": f"{go_path.parent}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(directories["home"]),
        "TMPDIR": str(directories["tmp"]),
        "XDG_CACHE_HOME": str(directories["xdg_cache"]),
        "XDG_CONFIG_HOME": str(directories["xdg_config"]),
        "XDG_DATA_HOME": str(directories["xdg_data"]),
        "GOPATH": str(directories["gopath"]),
        "GOMODCACHE": str(directories["gomodcache"]),
        "GOCACHE": str(directories["gocache"]),
        "GOTOOLCHAIN": "local",
        "GOENV": "off",
        "GOWORK": "off",
        "GOFLAGS": "-mod=readonly",
        "GOTELEMETRY": "off",
        "GOPROXY": "https://proxy.golang.org,direct",
        "GOSUMDB": "sum.golang.org",
        "CGO_ENABLED": EXPECTED_CGO_ENABLED,
        "GOMAXPROCS": "2",
        "GOOS": EXPECTED_GOOS,
        "GOARCH": EXPECTED_GOARCH,
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _kill_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _run_bounded(
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError:
        raise CommandOSError() from None

    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        raise CommandTimeout() from None
    except BaseException:
        _kill_group(process)
        raise

    return subprocess.CompletedProcess(
        list(argv), process.returncode, stdout, stderr
    )


def _verify_go(go_path: Path, work_dir: Path, env: Mapping[str, str]) -> str:
    try:
        result = _run_bounded(
            [str(go_path), "version"], work_dir, env, VERSION_TIMEOUT
        )
    except (CommandTimeout, CommandOSError):
        raise CheckFailure("go_toolchain_mismatch") from None
    output = (result.stdout or "").strip()
    match = re.match(
        rf"^go version ({re.escape(EXPECTED_GO_VERSION)})(?:\s|$)", output
    )
    if result.returncode != 0 or match is None:
        raise CheckFailure("go_toolchain_mismatch")
    return match.group(1)


def _verify_tool(
    go_path: Path,
    tool_path: Path,
    work_dir: Path,
    env: Mapping[str, str],
) -> None:
    try:
        result = _run_bounded(
            [str(go_path), "version", "-m", str(tool_path)],
            work_dir,
            env,
            VERSION_TIMEOUT,
        )
    except (CommandTimeout, CommandOSError):
        raise CheckFailure("tool_version_mismatch") from None
    if result.returncode != 0:
        raise CheckFailure("tool_version_mismatch")

    lines = (result.stdout or "").splitlines()
    if not lines:
        raise CheckFailure("tool_version_mismatch")
    tool_prefix, separator, build_go = lines[0].rpartition(": ")
    if (
        not separator
        or build_go != EXPECTED_GO_VERSION
        or tool_prefix != str(tool_path)
    ):
        raise CheckFailure("tool_version_mismatch")

    path_ok = False
    module_ok = False
    for line in lines[1:]:
        if re.match(
            r"^\s*path\s+golang\.org/x/vuln/cmd/govulncheck\s*$", line
        ):
            path_ok = True
        if re.match(r"^\s*mod\s+golang\.org/x/vuln\s+v1\.9\.0(?:\s|$)", line):
            module_ok = True
    if not (path_ok and module_ok):
        raise CheckFailure("tool_version_mismatch")


def _install_tool(
    go_path: Path,
    tool_dir: Path,
    cache_dir: Path,
    env: Mapping[str, str],
) -> Path:
    tool_dir = Path(tool_dir)
    work_dir = Path(cache_dir) / "install"
    try:
        tool_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise CheckFailure("install_failed") from None

    install_env = dict(env)
    install_env["GOBIN"] = str(tool_dir)
    try:
        result = _run_bounded(
            [str(go_path), "install", TOOL_PACKAGE + "@" + TOOL_VERSION],
            work_dir,
            install_env,
            INSTALL_TIMEOUT,
        )
    except CommandTimeout:
        raise CheckFailure("install_timeout") from None
    except CommandOSError:
        raise CheckFailure("install_failed") from None
    if result.returncode != 0:
        raise CheckFailure("install_failed")

    tool_path = tool_dir / TOOL_BINARY
    if not tool_path.is_file():
        raise CheckFailure("install_failed")
    return tool_path


def _scan(
    tool_path: Path, repo: Path, env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    try:
        return _run_bounded(
            [str(tool_path), *SCAN_ARGS], repo, env, SCAN_TIMEOUT
        )
    except CommandTimeout:
        raise CheckFailure("scan_timeout") from None
    except CommandOSError:
        raise CheckFailure("scan_failed") from None


def _parse_finding(value: object) -> tuple[str, bool]:
    if not isinstance(value, dict):
        raise InvalidOutput()
    if set(value) - FINDING_KEYS:
        raise InvalidOutput()
    osv = value.get("osv")
    if not isinstance(osv, str) or OSV_ID_RE.fullmatch(osv) is None:
        raise InvalidOutput()
    if "fixed_version" in value and not isinstance(
        value["fixed_version"], str
    ):
        raise InvalidOutput()
    trace = value.get("trace")
    if not isinstance(trace, list) or not trace:
        raise InvalidOutput()
    for frame in trace:
        if not isinstance(frame, dict):
            raise InvalidOutput()
        if set(frame) - FRAME_KEYS:
            raise InvalidOutput()
        module = frame.get("module")
        if not isinstance(module, str) or not module:
            raise InvalidOutput()
        for key in ("version", "package", "function", "receiver"):
            if key in frame and not isinstance(frame[key], str):
                raise InvalidOutput()
        if (
            "position" in frame
            and frame["position"] is not None
            and not isinstance(frame["position"], dict)
        ):
            raise InvalidOutput()
    first = trace[0]
    function = first.get("function", "")
    reachable = isinstance(function, str) and bool(function.strip())
    return osv, reachable


def parse_stream(text: str) -> ParsedScan:
    if not isinstance(text, str):
        raise InvalidOutput()
    decoder = json.JSONDecoder()
    messages: list[object] = []
    index = 0
    length = len(text)
    while True:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break
        try:
            message, end = decoder.raw_decode(text, index)
        except (json.JSONDecodeError, ValueError):
            raise InvalidOutput() from None
        if not isinstance(message, dict):
            raise InvalidOutput()
        messages.append(message)
        index = end

    if not messages:
        raise InvalidOutput()
    first = messages[0]
    if not isinstance(first, dict) or set(first) != {"config"}:
        raise InvalidOutput()
    config = first["config"]
    if not isinstance(config, dict):
        raise InvalidOutput()
    if set(config) - CONFIG_KEYS:
        raise InvalidOutput()
    if config.get("protocol_version") != EXPECTED_PROTOCOL_VERSION:
        raise InvalidOutput()
    if config.get("scanner_name") != EXPECTED_SCANNER_NAME:
        raise InvalidOutput()
    if config.get("scanner_version") != EXPECTED_SCANNER_VERSION:
        raise InvalidOutput()
    if config.get("db") != VULN_DB:
        raise InvalidOutput()
    if config.get("go_version") != EXPECTED_GO_VERSION:
        raise InvalidOutput()
    if config.get("scan_level") != "symbol":
        raise InvalidOutput()
    if config.get("scan_mode") != "source":
        raise InvalidOutput()
    if "db_last_modified" in config and not isinstance(
        config["db_last_modified"], str
    ):
        raise InvalidOutput()

    reachable: set[str] = set()
    unreachable: set[str] = set()
    for message in messages[1:]:
        if not isinstance(message, dict) or len(message) != 1:
            raise InvalidOutput()
        key, value = next(iter(message.items()))
        if key not in NON_CONFIG_MESSAGE_KEYS:
            raise InvalidOutput()
        if key == "finding":
            osv, is_reachable = _parse_finding(value)
            if is_reachable:
                reachable.add(osv)
                unreachable.discard(osv)
            elif osv not in reachable:
                unreachable.add(osv)
        elif not isinstance(value, dict):
            raise InvalidOutput()

    return ParsedScan(
        go_version=config["go_version"],
        reachable=frozenset(reachable),
        unreachable=frozenset(unreachable),
    )


def _build_report(
    *,
    result: str,
    failure_class: str | None,
    command_exit: int | None,
    duration_seconds: float,
    source_hash: Mapping[str, str],
    module_unchanged: bool,
    reachable: set[str],
    unreachable: set[str],
    verified_go_version: str | None,
    tool_verified: bool,
) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "version": REPORT_VERSION,
        "result": result,
        "tool": {
            "name": EXPECTED_SCANNER_NAME,
            "module": TOOL_MODULE,
            "version": TOOL_VERSION,
            "commit": TOOL_COMMIT,
        },
        "tool_verified": bool(tool_verified),
        "expected_go_version": EXPECTED_GO_VERSION,
        "go_version": verified_go_version,
        "source_hash": dict(sorted(source_hash.items())),
        "module_unchanged": bool(module_unchanged),
        "scan": {
            "mode": "source",
            "level": "symbol",
            "tags": "integration",
            "test": True,
            "db": VULN_DB,
            "pattern": "./...",
            "cgo_enabled": EXPECTED_CGO_ENABLED,
            "goos": EXPECTED_GOOS,
            "goarch": EXPECTED_GOARCH,
        },
        "osv_ids": sorted(reachable | unreachable),
        "reachable_ids": sorted(reachable),
        "unreachable_ids": sorted(unreachable),
        "reachable_count": len(reachable),
        "unreachable_count": len(unreachable),
        "failure_class": failure_class,
        "command_exit": command_exit,
        "duration_seconds": round(float(duration_seconds), 3),
    }


def _write_report(artifact_root: Path, report: Mapping[str, object]) -> Path:
    if set(report) != REPORT_KEYS:
        raise ValueError("report schema")
    if report["result"] not in {"pass", "vulnerable", "failure"}:
        raise ValueError("report result")
    if report["failure_class"] is not None and report["failure_class"] not in FAILURE_CLASSES:
        raise ValueError("report failure class")
    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / REPORT_NAME
    temporary = root / (REPORT_NAME + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError):
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return path


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.monotonic()
    go_path = Path(args.go)
    cache_dir = Path(args.cache_dir)
    tool_dir = Path(args.tool_dir)
    artifact_root = Path(args.artifact_root)

    result = "failure"
    failure_class: str | None = None
    command_exit: int | None = None
    source_hashes: dict[str, str] = {}
    module_unchanged = False
    reachable: set[str] = set()
    unreachable: set[str] = set()
    verified_go_version: str | None = None
    tool_verified = False
    report_path: Path | None = None
    report_written = False

    try:
        source_hashes = module_hashes(REPO_ROOT)
        env = build_env(go_path, cache_dir)
        verified_go_version = _verify_go(go_path, cache_dir, env)

        tool_path = tool_dir / TOOL_BINARY
        if args.prepare_tool:
            tool_path = _install_tool(go_path, tool_dir, cache_dir, env)
        elif not tool_path.is_file():
            raise CheckFailure("tool_missing")

        _verify_tool(go_path, tool_path, cache_dir, env)
        tool_verified = True
        if module_hashes(REPO_ROOT) != source_hashes:
            raise CheckFailure("module_changed")

        scan = _scan(tool_path, REPO_ROOT, env)
        command_exit = scan.returncode
        if scan.returncode != 0:
            raise CheckFailure("scan_failed")

        parsed = parse_stream(scan.stdout)
        reachable = set(parsed.reachable)
        unreachable = set(parsed.unreachable)

        if module_hashes(REPO_ROOT) != source_hashes:
            raise CheckFailure("module_changed")
        module_unchanged = True
        result = "vulnerable" if reachable else "pass"
    except CheckFailure as exc:
        result = "failure"
        failure_class = exc.failure_class
    except Exception:
        result = "failure"
        failure_class = "internal_error"
    finally:
        try:
            final_hashes = module_hashes(REPO_ROOT)
            if source_hashes:
                module_unchanged = final_hashes == source_hashes
                if not module_unchanged:
                    result = "failure"
                    failure_class = "module_changed"
            else:
                module_unchanged = False
        except Exception:
            module_unchanged = False

        report = _build_report(
            result=result,
            failure_class=failure_class,
            command_exit=command_exit,
            duration_seconds=time.monotonic() - started,
            source_hash=source_hashes,
            module_unchanged=module_unchanged,
            reachable=reachable,
            unreachable=unreachable,
            verified_go_version=verified_go_version,
            tool_verified=tool_verified,
        )
        try:
            report_path = _write_report(artifact_root, report)
            report_written = True
        except Exception:
            result = "failure"
            failure_class = "artifact_write_failed"
            report_path = None
            report_written = False

    print(f"result={result}")
    print(
        "report-path="
        + (str(report_path) if report_written and report_path else "unavailable")
    )
    print(f"reachable-count={len(reachable)}")
    print(f"unreachable-count={len(unreachable)}")
    print(f"error-class={failure_class or 'none'}")

    if result == "pass":
        return 0
    if result == "vulnerable":
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
