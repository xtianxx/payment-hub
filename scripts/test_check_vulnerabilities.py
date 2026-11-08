"""Mock-only regression tests for scripts/check-vulnerabilities.py.

These tests never invoke Go, govulncheck, Docker, or the network. All process
execution is mocked, and all repositories/artifacts are standard-library
temporary directories. They are unit evidence only, not a real vulnerability
scan.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "check_vulnerabilities", REPO / "scripts/check-vulnerabilities.py"
)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)

TOOL_BUILD_INFO = (
    "/tmp/tool/govulncheck: go1.27.3\n"
    "\tpath\tgolang.org/x/vuln/cmd/govulncheck\n"
    "\tmod\tgolang.org/x/vuln\tv1.9.0\th1:fixture\n"
)

FIXED_GO_VERSION = "go1.27.3"
FIXED_SCANNER_VERSION = "v1.9.0"
FIXED_DB = "https://vuln.go.dev"


def completed(argv, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(list(argv), code, stdout, stderr)


def config_message(**overrides):
    config = {
        "protocol_version": "v1.0.0",
        "scanner_name": "govulncheck",
        "scanner_version": FIXED_SCANNER_VERSION,
        "db": FIXED_DB,
        "db_last_modified": "2026-01-01T00:00:00Z",
        "go_version": FIXED_GO_VERSION,
        "scan_level": "symbol",
        "scan_mode": "source",
    }
    config.update(overrides)
    return {"config": config}


def sbom_message():
    return {
        "SBOM": {
            "go_version": FIXED_GO_VERSION,
            "modules": [{"path": "example.com/app", "version": "v0.0.0"}],
            "roots": ["example.com/app"],
        }
    }


def progress_message(text="Checking the code against the vulnerabilities..."):
    return {"progress": {"message": text}}


def osv_message(osv="GO-2026-0001", details="raw-osv-details-must-not-be-archived"):
    return {
        "osv": {
            "schema_version": "1.3.1",
            "id": osv,
            "details": details,
            "affected": [{"package": {"name": "example.com/mod"}}],
        }
    }


def finding_message(
    osv="GO-2026-0001",
    *,
    function=None,
    package=None,
    module="example.com/mod",
    version="v1.0.0",
):
    frame = {"module": module, "version": version}
    if package is not None:
        frame["package"] = package
    if function is not None:
        frame["function"] = function
    return {
        "finding": {
            "osv": osv,
            "fixed_version": "v1.0.1",
            "trace": [frame],
        }
    }


def stream(*messages, indent=None):
    return "".join(json.dumps(message, indent=indent) + "\n" for message in messages)


def pass_stream():
    return stream(config_message(), sbom_message(), progress_message())


class FakeRunner:
    """Deterministic stand-in for MOD._run_bounded."""

    def __init__(
        self,
        *,
        go_version=FIXED_GO_VERSION,
        tool_build_info=TOOL_BUILD_INFO,
        install_exit=0,
        install_timeout=False,
        install_error=False,
        scan_stdout="",
        scan_exit=0,
        scan_timeout=False,
        scan_error=False,
        on_install=None,
        on_scan=None,
    ):
        self.go_version = go_version
        self.tool_build_info = tool_build_info
        self.install_exit = install_exit
        self.install_timeout = install_timeout
        self.install_error = install_error
        self.scan_stdout = scan_stdout
        self.scan_exit = scan_exit
        self.scan_timeout = scan_timeout
        self.scan_error = scan_error
        self.on_install = on_install
        self.on_scan = on_scan
        self.calls = []

    def __call__(self, argv, cwd, env, timeout):
        argv = list(argv)
        self.calls.append((argv, str(cwd), dict(env), timeout))
        if argv[1:] == ["version"]:
            return completed(
                argv,
                0,
                f"go version {self.go_version} linux/amd64\n",
            )
        if len(argv) >= 3 and argv[1:3] == ["version", "-m"]:
            return completed(argv, 0, self.tool_build_info)
        if len(argv) >= 2 and argv[1] == "install":
            if self.install_timeout:
                raise MOD.CommandTimeout()
            if self.install_error:
                raise MOD.CommandOSError()
            if self.on_install is not None:
                self.on_install()
            return completed(argv, self.install_exit, "", "install-stderr-secret")
        if self.scan_timeout:
            raise MOD.CommandTimeout()
        if self.scan_error:
            raise MOD.CommandOSError()
        if self.on_scan is not None:
            self.on_scan()
        return completed(
            argv,
            self.scan_exit,
            self.scan_stdout,
            "scan-stderr-secret",
        )

    def scan_calls(self):
        return [
            call
            for call in self.calls
            if call[0] and call[0][0].endswith("govulncheck") and "-json" in call[0]
        ]

    def install_calls(self):
        return [call for call in self.calls if len(call[0]) > 1 and call[0][1] == "install"]


class TimeoutProcess:
    def __init__(self):
        self.pid = 4242
        self.returncode = -9
        self.communicate_calls = 0

    def communicate(self, timeout=None):
        self.communicate_calls += 1
        if self.communicate_calls == 1:
            raise subprocess.TimeoutExpired(["fixture"], timeout)
        return "", ""

    def kill(self):
        return None

    def wait(self, timeout=None):
        return self.returncode


class CheckVulnerabilitiesMockTests(unittest.TestCase):
    """Mock-only unit tests; no real Go/network/Docker execution."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vuln-check-unit-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / "go.mod").write_text(
            "module example.com/app\n\ngo 1.27.3\n", encoding="utf-8"
        )
        (self.repo / "go.sum").write_text(
            "example.com/mod v1.0.0 h1:fixture\n", encoding="utf-8"
        )
        self.cache = root / "cache"
        self.tool_dir = root / "tools"
        self.tool_dir.mkdir()
        (self.tool_dir / "govulncheck").write_text("fake-tool\n", encoding="utf-8")
        self.artifact = root / "artifacts"
        self.go = root / "go"
        self.go.write_text("fake-go\n", encoding="utf-8")
        self.go.chmod(0o755)

    def cli(self, prepare=False):
        args = [
            "--go",
            str(self.go),
            "--cache-dir",
            str(self.cache),
            "--tool-dir",
            str(self.tool_dir),
            "--artifact-root",
            str(self.artifact),
        ]
        if prepare:
            args.append("--prepare-tool")
        return args

    def run_main(self, runner, prepare=False):
        with mock.patch.object(MOD, "REPO_ROOT", self.repo), mock.patch.object(
            MOD, "_run_bounded", side_effect=runner
        ), contextlib.redirect_stdout(io.StringIO()) as stdout, (
            contextlib.redirect_stderr(io.StringIO())
        ) as stderr:
            code = MOD.main(self.cli(prepare=prepare))
        return code, stdout.getvalue(), stderr.getvalue()

    def report(self):
        return json.loads(
            (self.artifact / "vulnerability-report.json").read_text(encoding="utf-8")
        )

    def stdout_fields(self, text):
        lines = [line for line in text.splitlines() if line]
        self.assertEqual(len(lines), 5, text)
        fields = {}
        for line in lines:
            key, separator, value = line.partition("=")
            self.assertEqual(separator, "=", line)
            fields[key] = value
        self.assertEqual(
            set(fields),
            {
                "result",
                "report-path",
                "reachable-count",
                "unreachable-count",
                "error-class",
            },
        )
        return fields

    # ------------------------------------------------------------------
    # Source hash / environment
    # ------------------------------------------------------------------

    def test_source_hash_is_stable_across_copied_tree(self):
        first = MOD.module_hashes(self.repo)
        copied = Path(self.temp.name) / "copied-repo"
        shutil.copytree(self.repo, copied)
        second = MOD.module_hashes(copied)
        self.assertEqual(first, second)
        self.assertEqual(set(first), {"go.mod", "go.sum"})
        self.assertEqual(len(first["go.mod"]), 64)
        self.assertEqual(len(first["go.sum"]), 64)
        self.assertNotIn(str(self.repo), json.dumps(first))

    def test_build_env_isolates_caches_and_uses_utc(self):
        env = MOD.build_env(self.go, self.cache)
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertEqual(env["GOENV"], "off")
        self.assertEqual(env["GOWORK"], "off")
        self.assertEqual(env["TZ"], "UTC")
        self.assertEqual(env["GOTELEMETRY"], "off")
        self.assertEqual(env["CGO_ENABLED"], "1")
        self.assertEqual(env["GOMAXPROCS"], "2")
        for key in (
            "GOPATH",
            "GOMODCACHE",
            "GOCACHE",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "HOME",
            "TMPDIR",
        ):
            self.assertTrue(
                Path(env[key]).is_relative_to(self.cache), f"{key}={env[key]}"
            )
        self.assertTrue(env["PATH"].startswith(str(self.go.parent) + ":"))
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertNotIn("GOINSECURE", env)
        self.assertEqual(env["GOPROXY"], "https://proxy.golang.org,direct")
        self.assertEqual(env["GOSUMDB"], "sum.golang.org")

    def test_build_env_matches_ci_cgo1_and_does_not_inherit_env(self):
        with mock.patch.dict(
            MOD.os.environ,
            {
                "CGO_ENABLED": "0",
                "GOMAXPROCS": "1",
                "SECRET_TOKEN": "not-real",
            },
            clear=False,
        ):
            env = MOD.build_env(self.go, self.cache)
        self.assertEqual(env["CGO_ENABLED"], "1")
        self.assertEqual(env["GOMAXPROCS"], "2")
        self.assertNotIn("SECRET_TOKEN", env)
        self.assertNotIn("CC", env)
        self.assertNotIn("CXX", env)
        self.assertIn("/usr/bin", env["PATH"].split(":"))

    def test_build_env_matches_ci_platform(self):
        env = MOD.build_env(self.go, self.cache)
        self.assertEqual(env.get("GOOS"), "linux")
        self.assertEqual(env.get("GOARCH"), "amd64")
        self.assertEqual(env.get("CGO_ENABLED"), "1")

    def test_verify_go_returns_actual_verified_version(self):
        runner = FakeRunner(go_version=FIXED_GO_VERSION)
        with mock.patch.object(MOD, "_run_bounded", side_effect=runner):
            version = MOD._verify_go(self.go, self.cache, {})
        self.assertEqual(version, FIXED_GO_VERSION)

    def test_module_hashes_requires_go_mod_and_go_sum(self):
        for missing in ("go.mod", "go.sum"):
            with self.subTest(missing=missing):
                repo = Path(self.temp.name) / f"missing-{missing}"
                shutil.copytree(self.repo, repo)
                (repo / missing).unlink()
                with self.assertRaises(MOD.CheckFailure) as raised:
                    MOD.module_hashes(repo)
                self.assertEqual(raised.exception.failure_class, "module_missing")

        directory_sum = Path(self.temp.name) / "directory-go-sum"
        shutil.copytree(self.repo, directory_sum)
        (directory_sum / "go.sum").unlink()
        (directory_sum / "go.sum").mkdir()
        with self.assertRaises(MOD.CheckFailure) as raised:
            MOD.module_hashes(directory_sum)
        self.assertEqual(raised.exception.failure_class, "module_missing")

    def test_report_uses_observed_go_and_marks_unverified_tool_on_preflight_failure(self):
        runner = FakeRunner(go_version="go1.26.0", scan_stdout=pass_stream())
        code, _, _ = self.run_main(runner)
        self.assertEqual(code, 2)
        self.assertEqual(runner.scan_calls(), [])
        report = self.report()
        self.assertIn("expected_go_version", report)
        self.assertEqual(report["expected_go_version"], "go1.27.3")
        self.assertIsNone(report["go_version"])
        self.assertIn("tool_verified", report)
        self.assertFalse(report["tool_verified"])
        self.assertEqual(report["failure_class"], "go_toolchain_mismatch")

    def test_report_marks_tool_verified_only_after_tool_check(self):
        runner = FakeRunner(scan_stdout=pass_stream())
        code, _, _ = self.run_main(runner)
        self.assertEqual(code, 0)
        report = self.report()
        self.assertIn("tool_verified", report)
        self.assertTrue(report["tool_verified"])
        self.assertEqual(report["go_version"], FIXED_GO_VERSION)
        self.assertEqual(report["expected_go_version"], FIXED_GO_VERSION)

        mismatch_runner = FakeRunner(
            scan_stdout=pass_stream(),
            tool_build_info=(
                "/tmp/tool/govulncheck: go1.27.3\n"
                "\tpath\tgolang.org/x/vuln/cmd/govulncheck\n"
                "\tmod\tgolang.org/x/vuln\tv1.8.0\th1:fixture\n"
            ),
        )
        code, _, _ = self.run_main(mismatch_runner)
        self.assertEqual(code, 2)
        report = self.report()
        self.assertIn("tool_verified", report)
        self.assertFalse(report["tool_verified"])
        self.assertEqual(report["go_version"], FIXED_GO_VERSION)

    def test_report_exposes_disjoint_deduplicated_id_lists(self):
        payload = stream(
            config_message(),
            finding_message("GO-2026-0001"),
            finding_message("GO-2026-0001", function="VulnerableCall"),
            finding_message("GO-2026-0002", function="OtherCall"),
            finding_message("GO-2026-0002", function="OtherCall"),
            finding_message("GO-2026-0003", package="example.com/mod/sub"),
            finding_message("GO-2026-0004"),
            finding_message("GO-2026-0004"),
        )
        runner = FakeRunner(scan_stdout=payload)
        code, _, _ = self.run_main(runner)
        self.assertEqual(code, 1)
        report = self.report()
        self.assertIn("reachable_ids", report)
        self.assertIn("unreachable_ids", report)
        self.assertEqual(
            report["reachable_ids"], ["GO-2026-0001", "GO-2026-0002"]
        )
        self.assertEqual(
            report["unreachable_ids"], ["GO-2026-0003", "GO-2026-0004"]
        )
        self.assertEqual(
            set(report["reachable_ids"]) & set(report["unreachable_ids"]), set()
        )
        self.assertEqual(
            report["osv_ids"],
            sorted(set(report["reachable_ids"]) | set(report["unreachable_ids"])),
        )
        self.assertEqual(report["reachable_count"], 2)
        self.assertEqual(report["unreachable_count"], 2)

    # ------------------------------------------------------------------
    # CLI and scan command freeze
    # ------------------------------------------------------------------

    def test_relative_paths_and_unknown_flags_are_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                MOD.parse_args(
                    [
                        "--go",
                        "go",
                        "--cache-dir",
                        str(self.cache),
                        "--tool-dir",
                        str(self.tool_dir),
                        "--artifact-root",
                        str(self.artifact),
                    ]
                )
        self.assertEqual(raised.exception.code, 2)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                MOD.parse_args(self.cli() + ["--extra"])
        self.assertEqual(raised.exception.code, 2)

    def test_scan_command_is_exact_and_isolated(self):
        runner = FakeRunner(scan_stdout=pass_stream())
        code, stdout, _ = self.run_main(runner)
        self.assertEqual(code, 0)
        scan_calls = runner.scan_calls()
        self.assertEqual(len(scan_calls), 1)
        argv, cwd, env, timeout = scan_calls[0]
        self.assertEqual(
            argv,
            [
                str(self.tool_dir / "govulncheck"),
                "-json",
                "-mode=source",
                "-scan=symbol",
                "-tags=integration",
                "-test",
                "./...",
            ],
        )
        self.assertEqual(cwd, str(self.repo))
        self.assertEqual(timeout, 180)
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertEqual(env["GOENV"], "off")
        self.assertEqual(env["GOWORK"], "off")
        self.assertEqual(env["TZ"], "UTC")
        self.assertEqual(runner.install_calls(), [])
        fields = self.stdout_fields(stdout)
        self.assertEqual(fields["result"], "pass")
        self.assertEqual(
            fields["report-path"], str(self.artifact / "vulnerability-report.json")
        )

    def test_install_is_pinned_and_uses_isolated_tool_dir(self):
        (self.tool_dir / "govulncheck").unlink()
        runner = FakeRunner(
            scan_stdout=pass_stream(),
            on_install=lambda: (self.tool_dir / "govulncheck").write_text(
                "fake-tool\n", encoding="utf-8"
            ),
        )
        code, _, _ = self.run_main(runner, prepare=True)
        self.assertEqual(code, 0)
        install_calls = runner.install_calls()
        self.assertEqual(len(install_calls), 1)
        argv, cwd, env, timeout = install_calls[0]
        self.assertEqual(
            argv,
            [
                str(self.go),
                "install",
                "golang.org/x/vuln/cmd/govulncheck@v1.9.0",
            ],
        )
        self.assertEqual(env["GOBIN"], str(self.tool_dir))
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertEqual(env["GOWORK"], "off")
        self.assertEqual(timeout, 180)
        self.assertNotEqual(cwd, str(self.repo))
        self.assertTrue(Path(cwd).is_relative_to(self.cache))
        self.assertNotIn("@latest", argv[2])
        self.assertTrue((self.tool_dir / "govulncheck").is_file())
        report = self.report()
        self.assertEqual(report["tool"]["version"], "v1.9.0")
        self.assertEqual(
            report["tool"]["commit"], "e672cbe8fbc8c76e28fcb6db0efd9ce354e0dac5"
        )
        self.assertTrue(report["module_unchanged"])

    def test_missing_tool_without_prepare_does_not_install(self):
        (self.tool_dir / "govulncheck").unlink()
        runner = FakeRunner(scan_stdout=pass_stream())
        code, stdout, _ = self.run_main(runner, prepare=False)
        self.assertEqual(code, 2)
        self.assertEqual(runner.install_calls(), [])
        self.assertEqual(runner.scan_calls(), [])
        fields = self.stdout_fields(stdout)
        self.assertEqual(fields["result"], "failure")
        self.assertEqual(fields["error-class"], "tool_missing")
        report = self.report()
        self.assertEqual(report["result"], "failure")
        self.assertEqual(report["failure_class"], "tool_missing")
        self.assertEqual(report["source_hash"], MOD.module_hashes(self.repo))

    def test_tool_version_mismatch_blocks_scan(self):
        runner = FakeRunner(
            scan_stdout=pass_stream(),
            tool_build_info=(
                "/tmp/tool/govulncheck: go1.27.3\n"
                "\tpath\tgolang.org/x/vuln/cmd/govulncheck\n"
                "\tmod\tgolang.org/x/vuln\tv1.8.0\th1:fixture\n"
            ),
        )
        code, stdout, _ = self.run_main(runner)
        self.assertEqual(code, 2)
        self.assertEqual(runner.scan_calls(), [])
        self.assertEqual(
            self.stdout_fields(stdout)["error-class"], "tool_version_mismatch"
        )
        self.assertEqual(self.report()["failure_class"], "tool_version_mismatch")

    def test_go_version_mismatch_blocks_scan(self):
        runner = FakeRunner(go_version="go1.26.0", scan_stdout=pass_stream())
        code, stdout, _ = self.run_main(runner)
        self.assertEqual(code, 2)
        self.assertEqual(runner.scan_calls(), [])
        self.assertEqual(
            self.stdout_fields(stdout)["error-class"], "go_toolchain_mismatch"
        )
        self.assertEqual(self.report()["failure_class"], "go_toolchain_mismatch")

    # ------------------------------------------------------------------
    # Install / network failures
    # ------------------------------------------------------------------

    def test_install_failure_is_safe_and_never_scans(self):
        for kwargs, expected in (
            ({"install_exit": 1}, "install_failed"),
            ({"install_timeout": True}, "install_timeout"),
            ({"install_error": True}, "install_failed"),
        ):
            with self.subTest(expected=expected):
                runner = FakeRunner(scan_stdout=pass_stream(), **kwargs)
                code, stdout, _ = self.run_main(runner, prepare=True)
                self.assertEqual(code, 2)
                self.assertEqual(runner.scan_calls(), [])
                fields = self.stdout_fields(stdout)
                self.assertEqual(fields["result"], "failure")
                self.assertEqual(fields["error-class"], expected)
                report = self.report()
                self.assertEqual(report["result"], "failure")
                self.assertEqual(report["failure_class"], expected)
                self.assertIsNone(report["command_exit"])
                combined = stdout + json.dumps(report)
                self.assertNotIn("install-stderr-secret", combined)
                self.assertNotIn("scan-stderr-secret", combined)

    # ------------------------------------------------------------------
    # JSON stream parsing
    # ------------------------------------------------------------------

    def test_reachable_finding_with_exit_zero_is_vulnerable(self):
        payload = stream(
            config_message(),
            sbom_message(),
            osv_message(),
            finding_message(function="VulnerableCall"),
        )
        runner = FakeRunner(scan_stdout=payload, scan_exit=0)
        code, stdout, _ = self.run_main(runner)
        self.assertEqual(code, 1)
        fields = self.stdout_fields(stdout)
        self.assertEqual(fields["result"], "vulnerable")
        self.assertEqual(fields["reachable-count"], "1")
        self.assertEqual(fields["unreachable-count"], "0")
        self.assertEqual(fields["error-class"], "none")
        report = self.report()
        self.assertEqual(report["result"], "vulnerable")
        self.assertEqual(report["osv_ids"], ["GO-2026-0001"])
        self.assertEqual(report["reachable_count"], 1)
        self.assertEqual(report["unreachable_count"], 0)
        self.assertIsNone(report["failure_class"])
        self.assertEqual(report["command_exit"], 0)

    def test_module_and_package_only_findings_are_unreachable(self):
        payload = stream(
            config_message(),
            sbom_message(),
            osv_message("GO-2026-0001"),
            finding_message("GO-2026-0001"),
            osv_message("GO-2026-0002"),
            finding_message("GO-2026-0002", package="example.com/mod/sub"),
            finding_message("GO-2026-0001"),
        )
        runner = FakeRunner(scan_stdout=payload, scan_exit=0)
        code, stdout, _ = self.run_main(runner)
        self.assertEqual(code, 0)
        fields = self.stdout_fields(stdout)
        self.assertEqual(fields["result"], "pass")
        self.assertEqual(fields["reachable-count"], "0")
        self.assertEqual(fields["unreachable-count"], "2")
        report = self.report()
        self.assertEqual(report["result"], "pass")
        self.assertEqual(report["reachable_count"], 0)
        self.assertEqual(report["unreachable_count"], 2)
        self.assertEqual(report["osv_ids"], ["GO-2026-0001", "GO-2026-0002"])
        self.assertEqual(report["command_exit"], 0)

    def test_duplicate_reachable_findings_are_deduplicated(self):
        payload = stream(
            config_message(),
            finding_message("GO-2026-0001"),
            finding_message("GO-2026-0001", package="example.com/mod"),
            finding_message("GO-2026-0001", function="VulnerableCall"),
            finding_message("GO-2026-0001", function="OtherCall"),
        )
        runner = FakeRunner(scan_stdout=payload, scan_exit=0)
        code, _, _ = self.run_main(runner)
        self.assertEqual(code, 1)
        report = self.report()
        self.assertEqual(report["reachable_count"], 1)
        self.assertEqual(report["unreachable_count"], 0)
        self.assertEqual(report["osv_ids"], ["GO-2026-0001"])

    def test_multiline_pretty_json_stream_is_accepted(self):
        payload = stream(
            config_message(),
            sbom_message(),
            progress_message(),
            indent=2,
        )
        runner = FakeRunner(scan_stdout=payload, scan_exit=0)
        code, _, _ = self.run_main(runner)
        self.assertEqual(code, 0)
        self.assertEqual(self.report()["result"], "pass")

    def test_truncated_stream_is_invalid_output(self):
        payload = json.dumps(config_message())[:-6]
        runner = FakeRunner(scan_stdout=payload, scan_exit=0)
        code, stdout, _ = self.run_main(runner)
        self.assertEqual(code, 2)
        self.assertEqual(self.stdout_fields(stdout)["error-class"], "invalid_output")
        report = self.report()
        self.assertEqual(report["result"], "failure")
        self.assertEqual(report["failure_class"], "invalid_output")
        self.assertEqual(report["command_exit"], 0)

    def test_unknown_or_error_top_level_messages_are_invalid_output(self):
        payloads = {
            "error": stream(config_message(), {"error": "SECRET-ERROR-DETAIL"}),
            "unknown": stream(config_message(), {"unknown": {"value": 1}}),
            "duplicate-config": stream(config_message(), config_message()),
            "multi-key": stream(config_message(), {"progress": {}, "osv": {}}),
            "not-object": stream(config_message()) + "[]\n",
        }
        for name, payload in payloads.items():
            with self.subTest(name=name):
                runner = FakeRunner(scan_stdout=payload, scan_exit=0)
                code, stdout, _ = self.run_main(runner)
                self.assertEqual(code, 2)
                self.assertEqual(
                    self.stdout_fields(stdout)["error-class"], "invalid_output"
                )
                report = self.report()
                self.assertEqual(report["failure_class"], "invalid_output")
                combined = stdout + json.dumps(report)
                self.assertNotIn("SECRET-ERROR-DETAIL", combined)

    def test_config_validation_failures_are_invalid_output(self):
        cases = {
            "missing-config": stream(progress_message()),
            "protocol": stream(config_message(protocol_version="v2.0.0")),
            "scanner-version": stream(config_message(scanner_version="v1.8.0")),
            "go-version": stream(config_message(go_version="go1.26.0")),
            "scan-mode": stream(config_message(scan_mode="binary")),
            "scan-level": stream(config_message(scan_level="module")),
            "db": stream(config_message(db="http://insecure.example")),
            "scanner-name": stream(config_message(scanner_name="other-scanner")),
            "error-key": stream(
                {"config": {**config_message()["config"], "error": "SECRET"}}
            ),
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                runner = FakeRunner(scan_stdout=payload, scan_exit=0)
                code, stdout, _ = self.run_main(runner)
                self.assertEqual(code, 2)
                self.assertEqual(
                    self.stdout_fields(stdout)["error-class"], "invalid_output"
                )
                self.assertEqual(self.report()["failure_class"], "invalid_output")

    def test_bad_finding_structure_or_osv_id_is_invalid_output(self):
        cases = {
            "cve-id": stream(config_message(), finding_message("CVE-2026-0001")),
            "missing-trace": stream(
                config_message(), {"finding": {"osv": "GO-2026-0001"}}
            ),
            "empty-trace": stream(
                config_message(),
                {"finding": {"osv": "GO-2026-0001", "trace": []}},
            ),
            "bad-frame": stream(
                config_message(),
                {"finding": {"osv": "GO-2026-0001", "trace": ["bad"]}},
            ),
            "unknown-finding-key": stream(
                config_message(),
                {
                    "finding": {
                        "osv": "GO-2026-0001",
                        "trace": [{"module": "example.com/mod"}],
                        "details": "SECRET",
                    }
                },
            ),
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                runner = FakeRunner(scan_stdout=payload, scan_exit=0)
                code, stdout, _ = self.run_main(runner)
                self.assertEqual(code, 2)
                self.assertEqual(
                    self.stdout_fields(stdout)["error-class"], "invalid_output"
                )
                self.assertNotIn("SECRET", stdout + json.dumps(self.report()))

    def test_scan_nonzero_exit_and_exec_failure_are_tool_failures(self):
        for kwargs, expected, expected_exit in (
            (
                {"scan_exit": 1, "scan_stdout": pass_stream()},
                "scan_failed",
                1,
            ),
            ({"scan_error": True}, "scan_failed", None),
            ({"scan_timeout": True}, "scan_timeout", None),
        ):
            with self.subTest(expected=expected):
                runner = FakeRunner(**kwargs)
                code, stdout, _ = self.run_main(runner)
                self.assertEqual(code, 2)
                self.assertEqual(self.stdout_fields(stdout)["error-class"], expected)
                report = self.report()
                self.assertEqual(report["result"], "failure")
                self.assertEqual(report["failure_class"], expected)
                self.assertEqual(report["command_exit"], expected_exit)

    # ------------------------------------------------------------------
    # Module immutability and report safety
    # ------------------------------------------------------------------

    def test_module_change_during_scan_is_a_failure(self):
        original = MOD.module_hashes(self.repo)

        def mutate_module():
            (self.repo / "go.mod").write_text(
                "module example.com/app\n\ngo 1.27.3\n\nrequire example.com/x v1.0.0\n",
                encoding="utf-8",
            )

        runner = FakeRunner(scan_stdout=pass_stream(), on_scan=mutate_module)
        code, stdout, _ = self.run_main(runner)
        self.assertEqual(code, 2)
        self.assertEqual(self.stdout_fields(stdout)["error-class"], "module_changed")
        report = self.report()
        self.assertEqual(report["result"], "failure")
        self.assertEqual(report["failure_class"], "module_changed")
        self.assertFalse(report["module_unchanged"])
        self.assertEqual(report["source_hash"], original)

    def test_success_reports_unchanged_module(self):
        runner = FakeRunner(scan_stdout=pass_stream())
        code, _, _ = self.run_main(runner)
        self.assertEqual(code, 0)
        report = self.report()
        self.assertTrue(report["module_unchanged"])
        self.assertEqual(report["source_hash"], MOD.module_hashes(self.repo))

    def test_report_is_whitelisted_and_contains_no_raw_data(self):
        payload = stream(
            config_message(),
            sbom_message(),
            osv_message(details="SECRET-OSV-DETAILS"),
            finding_message(function="VulnerableCall"),
        )
        runner = FakeRunner(scan_stdout=payload)
        code, stdout, _ = self.run_main(runner)
        self.assertEqual(code, 1)
        report = self.report()
        self.assertEqual(set(report), MOD.REPORT_KEYS)
        self.assertEqual(set(report["tool"]), MOD.TOOL_KEYS)
        self.assertEqual(set(report["scan"]), MOD.SCAN_KEYS)
        self.assertEqual(report["schema"], "payment-hub.ci.vulnerability-report.v1")
        self.assertEqual(report["version"], 1)
        self.assertEqual(report["tool"]["name"], "govulncheck")
        self.assertEqual(report["tool"]["version"], "v1.9.0")
        self.assertEqual(report["go_version"], FIXED_GO_VERSION)
        self.assertEqual(report["scan"]["db"], FIXED_DB)
        self.assertEqual(report["scan"]["mode"], "source")
        self.assertEqual(report["scan"]["level"], "symbol")
        self.assertEqual(report["scan"]["tags"], "integration")
        self.assertIs(report["scan"]["test"], True)
        self.assertIsInstance(report["duration_seconds"], (int, float))
        for osv in report["osv_ids"]:
            self.assertRegex(osv, r"^GO-\d{4}-\d+$")
        serialized = json.dumps(report)
        for forbidden in (
            str(self.temp.name),
            "SECRET",
            "scan-stderr-secret",
            "install-stderr-secret",
            "raw-osv-details",
            "trace",
            "position",
            "function",
            "details",
            "stderr",
            "environment",
        ):
            self.assertNotIn(forbidden, serialized)
        self.stdout_fields(stdout)

    def test_report_scan_metadata_matches_ci_platform(self):
        runner = FakeRunner(scan_stdout=pass_stream())
        code, _, _ = self.run_main(runner)
        self.assertEqual(code, 0)
        scan = self.report().get("scan", {})
        self.assertEqual(scan.get("cgo_enabled"), "1")
        self.assertEqual(scan.get("goos"), "linux")
        self.assertEqual(scan.get("goarch"), "amd64")

    def test_entrypoint_exit_is_one_for_vulnerable(self):
        fake_dir = Path(self.temp.name) / "fake-bin"
        fake_dir.mkdir()
        fake_go = fake_dir / "go"
        fake_go.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "version" ] && [ "$2" = "-m" ]; then\n'
            "  printf '\\tpath\\tgolang.org/x/vuln/cmd/govulncheck\\n"
            "\\tmod\\tgolang.org/x/vuln\\tv1.9.0\\th1:fixture\\n'\n"
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "version" ]; then\n'
            "  echo 'go version go1.27.3 linux/amd64'\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        fake_go.chmod(0o755)
        fake_tool = fake_dir / "govulncheck"
        fake_tool.write_text(
            "#!/bin/sh\n"
            "cat <<'JSON'\n"
            + stream(
                config_message(),
                sbom_message(),
                progress_message(),
                finding_message("GO-2026-0001", function="VulnerableCall"),
            )
            + "JSON\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_tool.chmod(0o755)
        artifact = Path(self.temp.name) / "entrypoint-artifacts"
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO / "scripts/check-vulnerabilities.py"),
                "--go",
                str(fake_go),
                "--cache-dir",
                str(Path(self.temp.name) / "entrypoint-cache"),
                "--tool-dir",
                str(fake_dir),
                "--artifact-root",
                str(artifact),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 1, completed.stdout)
        self.assertIn("result=vulnerable", completed.stdout)
        report = json.loads(
            (artifact / "vulnerability-report.json").read_text(encoding="utf-8")
        )
        self.assertEqual(report.get("result"), "vulnerable")
        self.assertEqual(report.get("reachable_count"), 1)
        self.assertEqual(report.get("reachable_ids"), ["GO-2026-0001"])
        scan = report.get("scan", {})
        self.assertEqual(scan.get("cgo_enabled"), "1")
        self.assertEqual(scan.get("goos"), "linux")
        self.assertEqual(scan.get("goarch"), "amd64")

    def test_process_group_timeout_kills_the_group(self):
        process = TimeoutProcess()
        with mock.patch.object(
            MOD.subprocess, "Popen", return_value=process
        ) as popen, mock.patch.object(MOD.os, "killpg") as killpg:
            with self.assertRaises(MOD.CommandTimeout):
                MOD._run_bounded(["fixture"], self.repo, {}, 1)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertGreaterEqual(process.communicate_calls, 2)


if __name__ == "__main__":
    unittest.main()
