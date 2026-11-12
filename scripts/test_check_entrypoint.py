"""Focused contract tests for the T072 fixed check entrypoint.

These tests never invoke Go, PostgreSQL, Docker, or the network.  Every stage
is exercised through an injected in-memory command runner; report fixtures are
written as plain JSON only to verify the entrypoint's fixed result checks.
"""

from __future__ import annotations

import copy
import importlib.util
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
SCRIPT = REPO / "scripts" / "check.py"
CHECK_SH = REPO / "scripts" / "check.sh"

SPEC = importlib.util.spec_from_file_location("check_entrypoint", SCRIPT)
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


def executable(path: Path, text: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def go_report(required=None, result="pass", missing=None):
    required = (
        set(check.ci_checks.DEFAULT_REQUIRED_TESTS)
        if required is None
        else set(required)
    )
    if missing is not None:
        required.discard(missing)
    return {
        "schema": "payment-hub.ci-go-default.v1",
        "result": result,
        "problems": [] if result == "pass" else ["test-exit"],
        "exit_code": 0 if result == "pass" else 1,
        "list_exit_code": 0,
        "timed_out": False,
        "required_test_results": {name: "pass" for name in required},
    }


def db_report(required=None, result="pass", missing=None):
    required = (
        set(check.ci_checks.DB_REQUIRED_TEST_KEYS)
        if required is None
        else set(required)
    )
    if missing is not None:
        required.discard(missing)
    return {
        "schema": "payment-hub.db-foundation.v1",
        "result": result,
        "tests": {
            "required_test_results": {name: "pass" for name in required},
            "test_exit": 0 if result == "pass" else 1,
            "vet_exit": 0,
        },
        "cleanup": {"result": "pass" if result == "pass" else "fail"},
    }


IMAGE_RUN_ID = "c" * 32
IMAGE_REPORT_CANARY = "IMAGE_REPORT_BODY_CANARY"
IMAGE_CHILD_CANARY = "IMAGE_CHILD_STDOUT_CANARY"


def image_report(
    *,
    schema="payment-hub.images.v1",
    result="pass",
    failure=None,
    source_unchanged=True,
    build_verified=True,
    pair=None,
    go_test=None,
    cleanup=None,
    observations=None,
):
    pair_value = {
        "same_source": True,
        "same_platform": True,
        "distinct_ids": True,
        "ids_unchanged_after_run": True,
    }
    if pair is not None:
        pair_value.update(pair)
    go_value = {
        "required_root": "TestImagePairAcceptance",
        "result": "pass",
        "package_result": "pass",
        "root_count": 1,
        "exit_code": 0,
        "timed_out": False,
    }
    if go_test is not None:
        go_value.update(go_test)
    cleanup_value = {
        "result": "pass",
        "errors": [],
        "containers_absent": True,
        "network_absent": True,
        "volume_absent": True,
        "env_file_removed": True,
        "socket_dir_removed": True,
    }
    if cleanup is not None:
        cleanup_value.update(cleanup)
    return {
        "schema": schema,
        "result": result,
        "failure": failure,
        "source": {"unchanged": source_unchanged},
        "build_record": {"verified": build_verified},
        "pair": pair_value,
        "go_test": go_value,
        "cleanup": cleanup_value,
        "observations": (
            {"canary": IMAGE_REPORT_CANARY}
            if observations is None
            else observations
        ),
    }


class FakeRunner:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler

    def __call__(self, argv, *, cwd, env, timeout):
        argv = list(argv)
        self.calls.append(
            {
                "argv": argv,
                "cwd": str(cwd),
                "env": dict(env),
                "timeout": timeout,
            }
        )
        if self.handler is not None:
            value = self.handler(argv, Path(cwd), dict(env), timeout)
            if value is not None:
                return value
        return check.CommandResult(0, "", "", False, 0.0)


class EntrypointTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="check-entrypoint-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.go = executable(self.base / "bin" / "go")
        self.gofmt = executable(self.base / "bin" / "gofmt")
        self.cache = self.base / "cache"
        self.cache.mkdir()
        self.root = self.base / "artifacts" / "run"

    def tearDown(self):
        check._reset_cancellation()

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

    def argv(self, scope="unit", root=None, go=None, cache=None):
        return [
            "--scope",
            scope,
            "--go",
            str(self.go if go is None else go),
            "--cache-dir",
            str(self.cache if cache is None else cache),
            "--artifact-root",
            str(self.root if root is None else root),
        ]

    def main(self, scope="unit", runner=None, root=None, go=None, cache=None):
        return check.main(
            self.argv(scope, root=root, go=go, cache=cache),
            command_runner=runner,
        )

    def checks(self, root=None):
        target = (self.root if root is None else root) / "checks.json"
        return json.loads(target.read_text(encoding="utf-8"))

    def build_record(self, name="build-record.json", **overrides):
        value = {
            "schema": "payment-hub.image-build-record.v1",
            "source_manifest_digest": "a" * 64,
            "platform": "linux/amd64",
            "app_image": "fake-app:local",
            "app_image_id": "sha256:" + "b" * 64,
            "migrate_image": "fake-migrate:local",
            "migrate_image_id": "sha256:" + "c" * 64,
        }
        value.update(overrides)
        path = self.base / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def image_environment(self, build_record=None, **overrides):
        values = {
            "PAYMENT_HUB_APP_IMAGE": "fake-app:local",
            "PAYMENT_HUB_MIGRATE_IMAGE": "fake-migrate:local",
            "PAYMENT_HUB_IMAGE_BUILD_RECORD": str(
                self.build_record() if build_record is None else build_record
            ),
            "DOCKER_HOST": "unix:///tmp/opencode/fake-docker.sock",
            "DOCKER_CONTEXT": "fake-local-context",
            "DOCKER_CONFIG": str(self.base / "docker-config"),
            "XDG_RUNTIME_DIR": str(self.base / "runtime"),
        }
        values.update(overrides)
        return values

    def image_handler(
        self,
        report=None,
        *,
        run_id=IMAGE_RUN_ID,
        stdout="",
        stderr="",
        write_report=True,
        extra_reports=0,
    ):
        report = image_report() if report is None else report

        def handler(argv, cwd, env, timeout):
            artifact_root = Path(argv[argv.index("--artifact-root") + 1])
            if write_report:
                run_dir = artifact_root / run_id
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "images.json").write_text(
                    json.dumps(report), encoding="utf-8"
                )
            for index in range(extra_reports):
                extra_dir = artifact_root / (str(index) + "d" * 31)
                extra_dir.mkdir(parents=True, exist_ok=True)
                (extra_dir / "images.json").write_text(
                    json.dumps(report), encoding="utf-8"
                )
            return check.CommandResult(0, stdout, stderr, False, 0.01)

        return handler

    def default_handler(self, argv, cwd, env, timeout):
        joined = " ".join(argv)
        if Path(argv[0]).name == "git":
            return check.CommandResult(
                0,
                "cmd/payment-hub/main.go\0tests/harness/artifacts.go\0",
                "",
                False,
                0.01,
            )
        if Path(argv[0]).name == "gofmt":
            return check.CommandResult(0, "", "", False, 0.01)
        if "run-default" in argv:
            report_dir = Path(argv[argv.index("--report-dir") + 1])
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / "go-default.json").write_text(
                json.dumps(go_report()), encoding="utf-8"
            )
        elif "check-db-foundation.py" in joined:
            root = Path(argv[argv.index("--artifact-root") + 1])
            run_dir = root / ("a" * 32)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "db-foundation.json").write_text(
                json.dumps(db_report()), encoding="utf-8"
            )
        return check.CommandResult(0, "", "", False, 0.01)


class EntrypointContractTests(EntrypointTestCase):
    def test_scope_choices_are_fixed_and_all_is_not_downgraded(self):
        self.assertEqual(
            check.SCOPES, ("format", "static", "unit", "db", "image", "all")
        )
        self.assertEqual(
            check.selected_stages("all"),
            ("format", "static", "unit", "db", "image"),
        )
        for scope in check.SCOPES[:-1]:
            with self.subTest(scope=scope):
                self.assertEqual(check.selected_stages(scope), (scope,))

    def test_missing_required_arguments_are_nonzero(self):
        runner = FakeRunner()
        with mock.patch("sys.stderr"):
            code = check.main(["--scope", "unit"], command_runner=runner)
        self.assertNotEqual(code, 0)
        self.assertEqual(runner.calls, [])

    def test_go_must_be_absolute_and_executable(self):
        runner = FakeRunner()
        with mock.patch("sys.stderr"):
            relative = check.main(
                [
                    "--scope",
                    "unit",
                    "--go",
                    "bin/go",
                    "--cache-dir",
                    str(self.cache),
                    "--artifact-root",
                    str(self.root),
                ],
                command_runner=runner,
            )
            missing = self.main(
                go=self.base / "bin" / "missing-go", runner=runner
            )
        self.assertNotEqual(relative, 0)
        self.assertNotEqual(missing, 0)
        self.assertEqual(runner.calls, [])

    def test_cache_dir_must_be_outside_repository(self):
        runner = FakeRunner()
        with mock.patch("sys.stderr"):
            code = self.main(cache=REPO / "tmp", runner=runner)
        self.assertNotEqual(code, 0)
        self.assertEqual(runner.calls, [])

    def test_artifact_root_must_be_fresh_and_not_overwrite(self):
        self.root.mkdir(parents=True)
        keep = self.root / "keep.txt"
        keep.write_text("old-run", encoding="utf-8")
        runner = FakeRunner()
        with mock.patch("sys.stderr"):
            code = self.main(scope="image", runner=runner)
        self.assertNotEqual(code, 0)
        self.assertEqual(keep.read_text(encoding="utf-8"), "old-run")
        self.assertFalse((self.root / "checks.json").exists())
        self.assertEqual(runner.calls, [])

    def test_existing_stage_directory_is_not_overwritten(self):
        self.root.mkdir(parents=True)
        stage = self.root / "format"
        stage.mkdir()
        keep = stage / "keep.txt"
        keep.write_text("old-stage", encoding="utf-8")
        runner = FakeRunner(self.default_handler)
        with mock.patch("sys.stderr"):
            code = self.main(scope="format", runner=runner)
        self.assertNotEqual(code, 0)
        self.assertEqual(keep.read_text(encoding="utf-8"), "old-stage")
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.root / "checks.json").exists())

    def test_check_sh_is_thin_and_never_defaults_to_all(self):
        self.assertTrue(CHECK_SH.is_file())
        text = CHECK_SH.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/usr/bin/env bash\n"))
        self.assertIn("exec python3", text)
        self.assertNotIn("set -x", text)
        self.assertNotIn("--scope", text)
        self.assertNotIn("|| true", text)
        self.assertNotIn("docker", text)
        self.assertNotIn("go test", text)


class StageDispatchTests(EntrypointTestCase):
    def test_format_clean_uses_gofmt_list_without_writing(self):
        runner = FakeRunner(self.default_handler)
        self.assertEqual(self.main(scope="format", runner=runner), 0)
        report = self.checks()
        stage = report["stages"]["format"]
        self.assertEqual(stage["status"], "pass")
        self.assertEqual(stage["exit_code"], 0)
        self.assertEqual(stage["budget_seconds"], 30)
        self.assertEqual(stage["metrics"]["dirty_files"], 0)
        self.assertEqual(len(stage["commands"]), 2)
        git_argv = stage["commands"][0]["argv"]
        gofmt_argv = stage["commands"][1]["argv"]
        self.assertEqual(Path(git_argv[0]).name, "git")
        self.assertIn("--cached", git_argv)
        self.assertIn("--others", git_argv)
        self.assertIn("--exclude-standard", git_argv)
        self.assertEqual(Path(gofmt_argv[0]).name, "gofmt")
        self.assertIn("-l", gofmt_argv)
        self.assertNotIn("-w", gofmt_argv)
        self.assertNotIn("stdout", stage)
        self.assertNotIn("stderr", stage)

    def test_format_dirty_output_fails_without_echoing_paths(self):
        def handler(argv, cwd, env, timeout):
            if Path(argv[0]).name == "git":
                return check.CommandResult(
                    0,
                    "cmd/payment-hub/main.go\0tests/harness/artifacts.go\0",
                    "",
                    False,
                    0.01,
                )
            return check.CommandResult(
                0, "tests/canary_dirty.go\n", "", False, 0.01
            )

        runner = FakeRunner(handler)
        self.assertNotEqual(self.main(scope="format", runner=runner), 0)
        stage = self.checks()["stages"]["format"]
        self.assertEqual(stage["status"], "fail")
        self.assertIn("format-needed", stage["problems"])
        self.assertNotIn("canary_dirty", json.dumps(self.checks()))

    def test_static_runs_vet_then_contract(self):
        runner = FakeRunner(self.default_handler)
        self.assertEqual(self.main(scope="static", runner=runner), 0)
        stage = self.checks()["stages"]["static"]
        self.assertEqual(stage["status"], "pass")
        self.assertEqual(stage["budget_seconds"], 120)
        self.assertEqual(len(stage["commands"]), 2)
        self.assertEqual(
            stage["commands"][0]["argv"][1:],
            ["vet", "-mod=readonly", "-p=1", "./..."],
        )
        contract_argv = stage["commands"][1]["argv"]
        self.assertIn("check-ci-contract.py", " ".join(contract_argv))
        self.assertIn("--go", contract_argv)
        self.assertIn(str(self.go), contract_argv)

    def test_unit_runs_existing_default_gate_and_requires_pass_report(self):
        runner = FakeRunner(self.default_handler)
        self.assertEqual(self.main(scope="unit", runner=runner), 0)
        stage = self.checks()["stages"]["unit"]
        self.assertEqual(stage["status"], "pass")
        self.assertEqual(stage["budget_seconds"], 570)
        self.assertEqual(len(stage["commands"]), 1)
        argv = stage["commands"][0]["argv"]
        self.assertIn("ci_checks.py", " ".join(argv))
        self.assertIn("run-default", argv)
        self.assertIn("--go", argv)
        self.assertIn("--report-dir", argv)
        self.assertEqual(stage["report"]["result"], "pass")
        self.assertEqual(
            stage["report"]["path"], "unit/reports/go-default.json"
        )

    def test_unit_report_missing_required_root_fails_closed(self):
        def handler(argv, cwd, env, timeout):
            report_dir = Path(argv[argv.index("--report-dir") + 1])
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / "go-default.json").write_text(
                json.dumps(
                    go_report(missing="TestPaymentRequestBoundary")
                ),
                encoding="utf-8",
            )
            return check.CommandResult(0, "", "", False, 0.01)

        runner = FakeRunner(handler)
        self.assertNotEqual(self.main(scope="unit", runner=runner), 0)
        stage = self.checks()["stages"]["unit"]
        self.assertEqual(stage["status"], "fail")
        self.assertIn("unit-report-required-root-missing", stage["problems"])

    def test_db_uses_fixed_flags_and_requires_25_key_pass_report(self):
        runner = FakeRunner(self.default_handler)
        self.assertEqual(self.main(scope="db", runner=runner), 0)
        stage = self.checks()["stages"]["db"]
        self.assertEqual(stage["status"], "pass")
        argv = stage["commands"][0]["argv"]
        joined = " ".join(argv)
        self.assertIn("check-db-foundation.py", joined)
        for flag in ("--go", "--cache-dir", "--artifact-root"):
            self.assertIn(flag, argv)
        self.assertIn("--cleanup-owned-resources", argv)
        self.assertNotIn("--prepare-images", argv)
        self.assertNotIn("--download-modules", argv)
        self.assertEqual(stage["report"]["result"], "pass")
        self.assertIn("db-foundation.json", stage["report"]["path"])

    def test_db_dispatch_forwards_explicit_docker_connection_env(self):
        env = {
            "HOME": str(self.base / "home"),
            "DOCKER_HOST": "unix:///run/user/1000/docker.sock",
            "DOCKER_CONTEXT": "env-rootless",
            "DOCKER_CONFIG": str(self.base / "alt-docker"),
            "XDG_RUNTIME_DIR": str(self.base / "runtime"),
            "PAYMENT_HUB_TEST_DATABASE_URL": "postgres://SECRET_CANARY",
            "PAYMENT_HUB_APP_IMAGE": "image-canary",
        }
        runner = FakeRunner(self.default_handler)
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(self.main(scope="db", runner=runner), 0)
        self.assertEqual(len(runner.calls), 1)
        child_env = runner.calls[0]["env"]
        self.assertEqual(child_env["HOME"], env["HOME"])
        for key in (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "XDG_RUNTIME_DIR",
        ):
            self.assertEqual(child_env[key], env[key])
        for key in (
            "PAYMENT_HUB_TEST_DATABASE_URL",
            "PAYMENT_HUB_APP_IMAGE",
        ):
            self.assertNotIn(key, child_env)
        text = (self.root / "checks.json").read_text(encoding="utf-8")
        self.assertNotIn("SECRET_CANARY", text)
        self.assertNotIn(env["DOCKER_HOST"], text)

    def test_db_launcher_rejects_remote_docker_host_before_any_docker_call(self):
        spec = importlib.util.spec_from_file_location(
            "check_db_foundation_probe",
            REPO / "scripts" / "check-db-foundation.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        launcher = module.Launcher.__new__(module.Launcher)
        remote = "tcp://127.0.0.1:2375"
        with mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.base / "home"),
                "DOCKER_HOST": remote,
                "DOCKER_CONTEXT": "remote-context",
                "DOCKER_CONFIG": str(self.base / "alt-docker"),
                "XDG_RUNTIME_DIR": str(self.base / "runtime"),
            },
            clear=False,
        ), mock.patch.object(
            module.shutil, "which", return_value="/fake/docker"
        ), mock.patch.object(launcher, "_run") as run:
            with self.assertRaises(module.LauncherError) as raised:
                launcher.check_docker()
        self.assertEqual(raised.exception.stage, "environment")
        self.assertEqual(raised.exception.kind, "remote-daemon")
        self.assertEqual(launcher.docker_env["DOCKER_HOST"], remote)
        for key in (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "XDG_RUNTIME_DIR",
        ):
            self.assertIn(key, launcher.docker_env)
        run.assert_not_called()

    def test_db_report_missing_cross_entry_security_fails_closed(self):
        def handler(argv, cwd, env, timeout):
            root = Path(argv[argv.index("--artifact-root") + 1])
            run_dir = root / ("b" * 32)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "db-foundation.json").write_text(
                json.dumps(db_report(missing="TestCrossEntrySecurity")),
                encoding="utf-8",
            )
            return check.CommandResult(0, "", "", False, 0.01)

        runner = FakeRunner(handler)
        self.assertNotEqual(self.main(scope="db", runner=runner), 0)
        stage = self.checks()["stages"]["db"]
        self.assertEqual(stage["status"], "fail")
        self.assertIn("db-report-required-root-missing", stage["problems"])

    def test_image_gate_is_not_ready_and_never_fake_passes(self):
        runner = FakeRunner()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PAYMENT_HUB_APP_IMAGE", None)
            os.environ.pop("PAYMENT_HUB_MIGRATE_IMAGE", None)
            code = self.main(scope="image", runner=runner)
        self.assertNotEqual(code, 0)
        self.assertEqual(runner.calls, [])
        report = self.checks()
        self.assertEqual(report["result"], "fail")
        self.assertEqual(report["stages"]["image"]["status"], "not-ready")
        self.assertIn("image-not-ready", report["problems"])

    def test_image_command_contract_is_reserved_without_build_or_pull(self):
        stage = self.root / "image"
        argv = check.build_image_command(self.go, self.cache, stage)
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1], "-B")
        self.assertTrue(argv[2].endswith("check-images.py"))
        self.assertIn("--go", argv)
        self.assertIn("--cache-dir", argv)
        self.assertIn("--artifact-root", argv)
        self.assertNotIn("--build", argv)
        self.assertNotIn("--pull", argv)
        self.assertNotIn("--prepare-images", argv)
        self.assertNotIn("--download-modules", argv)

    def test_all_preflight_fails_before_any_expensive_stage(self):
        runner = FakeRunner(
            lambda *args: self.fail("all must not dispatch any stage")
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PAYMENT_HUB_APP_IMAGE", None)
            os.environ.pop("PAYMENT_HUB_MIGRATE_IMAGE", None)
            code = self.main(scope="all", runner=runner)
        self.assertNotEqual(code, 0)
        self.assertEqual(runner.calls, [])
        report = self.checks()
        self.assertEqual(report["result"], "fail")
        self.assertEqual(
            tuple(report["stages"]),
            ("format", "static", "unit", "db", "image"),
        )
        for name in ("format", "static", "unit", "db"):
            self.assertEqual(report["stages"][name]["status"], "not-run")
        self.assertEqual(report["stages"]["image"]["status"], "not-ready")

    def test_checks_json_is_0600_and_excludes_raw_output_and_env(self):
        canary = "CANARY_RAW_STDOUT_SECRET"

        def handler(argv, cwd, env, timeout):
            report_dir = Path(argv[argv.index("--report-dir") + 1])
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / "go-default.json").write_text(
                json.dumps(go_report()), encoding="utf-8"
            )
            return check.CommandResult(
                0, canary, canary, False, 0.01
            )

        runner = FakeRunner(handler)
        with mock.patch.dict(
            os.environ,
            {"PAYMENT_HUB_TEST_DATABASE_URL": "postgres://CANARY_SECRET"},
            clear=False,
        ):
            self.assertEqual(self.main(scope="unit", runner=runner), 0)
        path = self.root / "checks.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            canary,
            "CANARY_SECRET",
            "postgres://",
            '"stdout"',
            '"stderr"',
            '"env"',
        ):
            self.assertNotIn(forbidden, text)

    def test_signal_forwarding_targets_only_the_direct_child(self):
        class FakeProcess:
            def __init__(self):
                self.signals = []
                self.returncode = None

            def poll(self):
                return self.returncode

            def send_signal(self, signum):
                self.signals.append(signum)

        process = FakeProcess()
        old = check._ACTIVE_PROCESS
        check._ACTIVE_PROCESS = process
        try:
            check._forward_signal(signal.SIGTERM, None)
        finally:
            check._ACTIVE_PROCESS = old
        self.assertEqual(process.signals, [signal.SIGTERM])

    def test_direct_tool_timeout_kills_owned_group_and_is_bounded(self):
        script = self.base / "timeout_group.py"
        script.write_text(
            "import signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "grand = subprocess.Popen([\n"
            "    sys.executable, '-B', '-c',\n"
            "    'import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)',\n"
            "])\n"
            "open(sys.argv[1], 'w').write(str(grand.pid))\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        pid_file = self.base / "grandchild.pid"
        started = time.monotonic()
        with mock.patch.object(
            check, "DIRECT_TERM_GRACE_SECONDS", 0.2
        ), mock.patch.object(check, "DIRECT_KILL_DRAIN_SECONDS", 0.5):
            result = check.run_command(
                [sys.executable, "-B", str(script), str(pid_file)],
                cwd=self.base,
                env=dict(os.environ),
                timeout=0.5,
            )
        elapsed = time.monotonic() - started
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, 124)
        self.assertLess(elapsed, 5.0)
        self.assertTrue(pid_file.is_file())
        self.assertTrue(
            self._wait_pid_gone(int(pid_file.read_text(encoding="utf-8")))
        )

    def test_timeout_after_leader_reaped_uses_recorded_owned_group(self):
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
            # Simulate the leader already being reaped after the child has
            # spawned its grandchild; creation-time ownership was recorded
            # before the ready marker exists.
            if ready.exists():
                raise ProcessLookupError(pid)
            return real_getpgid(pid)

        try:
            with mock.patch.object(
                check, "DIRECT_TERM_GRACE_SECONDS", 0.2
            ), mock.patch.object(
                check, "DIRECT_KILL_DRAIN_SECONDS", 0.5
            ), mock.patch.object(
                check.os, "getpgid", side_effect=getpgid
            ):
                result = check.run_command(
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
                )
            self.assertTrue(result.timed_out)
            self.assertEqual(result.exit_code, 124)
            leader_pid, grand_pid = map(
                int, pid_file.read_text(encoding="utf-8").split()
            )
            self.assertTrue(self._wait_pid_gone(grand_pid))
            self.assertFalse(result.cleanup_failed)
        finally:
            if leader_pid is None or grand_pid is None:
                try:
                    leader_pid, grand_pid = map(
                        int, pid_file.read_text(encoding="utf-8").split()
                    )
                except (OSError, ValueError):
                    pass
            if leader_pid is not None and grand_pid is not None:
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

    def test_signal_latches_even_when_child_exits_zero(self):
        ready = self.base / "ready"
        script = self.base / "signal_zero.py"
        script.write_text(
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
            "open(sys.argv[1], 'w').write('ready')\n"
            "while True: time.sleep(0.05)\n",
            encoding="utf-8",
        )

        def send():
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        result = check.run_command(
            [sys.executable, "-B", str(script), str(ready)],
            cwd=self.base,
            env=dict(os.environ),
            timeout=5,
        )
        thread.join(timeout=1)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(check._cancelled_signal(), signal.SIGTERM)
        ctx = check.Context(
            go=str(self.go),
            cache=self.cache,
            root=self.root,
            env=dict(os.environ),
        )
        stage_dir = self.root / "format"
        stage_dir.mkdir(parents=True)
        entry = check._run_stage(
            "format", ctx, stage_dir, FakeRunner(self.default_handler)
        )
        self.assertEqual(entry["status"], "interrupted")
        self.assertIn("interrupted", entry["problems"])
        self.assertIsNone(entry["report"])
        self.assertNotIn("SECRET", json.dumps(entry))

    def test_cancelled_run_marks_remaining_stages_not_run(self):
        secret = "SECRET_CANARY_SYNTHETIC"

        def handler(argv, cwd, env, timeout):
            check._forward_signal(signal.SIGTERM, None)
            result = self.default_handler(argv, cwd, env, timeout)
            return result._replace(stderr=secret)

        with mock.patch.object(
            check, "image_not_ready_reasons", return_value=[]
        ):
            code = self.main(scope="all", runner=FakeRunner(handler))
        self.assertNotEqual(code, 0)
        report = self.checks()
        self.assertEqual(report["result"], "fail")
        self.assertEqual(report["stages"]["format"]["status"], "interrupted")
        for name in ("static", "unit", "db", "image"):
            self.assertEqual(report["stages"][name]["status"], "not-run")
        self.assertIn("interrupted", report["problems"])
        self.assertNotIn(
            secret, (self.root / "checks.json").read_text(encoding="utf-8")
        )


class ImageDispatchTests(EntrypointTestCase):
    IMAGE_ENV = (
        "PAYMENT_HUB_APP_IMAGE",
        "PAYMENT_HUB_MIGRATE_IMAGE",
        "PAYMENT_HUB_IMAGE_BUILD_RECORD",
    )
    DOCKER_ENV = (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "XDG_RUNTIME_DIR",
    )

    def test_image_dispatch_passes_fixed_launcher_env_and_subset_report(self):
        env = self.image_environment()
        runner = FakeRunner(
            self.image_handler(stdout=IMAGE_CHILD_CANARY, stderr=IMAGE_CHILD_CANARY)
        )
        with mock.patch.dict(os.environ, env, clear=False):
            code = self.main(scope="image", runner=runner)
        self.assertEqual(code, 0)
        report = self.checks()
        stage = report["stages"]["image"]
        self.assertEqual(stage["status"], "pass")
        self.assertEqual(stage["budget_seconds"], 180)
        self.assertEqual(len(stage["commands"]), 1)
        self.assertEqual(len(runner.calls), 1)
        call = runner.calls[0]
        argv = call["argv"]
        self.assertIsNone(call["timeout"])
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1], "-B")
        self.assertTrue(argv[2].endswith("check-images.py"))
        self.assertEqual(argv[argv.index("--go") + 1], str(self.go))
        self.assertEqual(argv[argv.index("--cache-dir") + 1], str(self.cache))
        self.assertEqual(
            argv[argv.index("--artifact-root") + 1],
            str(self.root / "image" / "images-artifacts"),
        )
        self.assertNotIn("--build", argv)
        self.assertNotIn("--pull", argv)
        for key in self.IMAGE_ENV + self.DOCKER_ENV:
            self.assertEqual(call["env"][key], env[key])
        self.assertNotIn("PAYMENT_HUB_TEST_DATABASE_URL", call["env"])

        ref = stage["report"]
        self.assertEqual(ref["result"], "pass")
        self.assertEqual(
            ref["path"],
            "image/images-artifacts/" + IMAGE_RUN_ID + "/images.json",
        )
        self.assertRegex(ref["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(stage["metrics"]["go_test_required_root"], "TestImagePairAcceptance")
        self.assertTrue(stage["metrics"]["cleanup_resources_absent"])

        text = (self.root / "checks.json").read_text(encoding="utf-8")
        for forbidden in (
            IMAGE_CHILD_CANARY,
            IMAGE_REPORT_CANARY,
            "observations",
            env["DOCKER_HOST"],
            env["PAYMENT_HUB_APP_IMAGE"],
            env["PAYMENT_HUB_IMAGE_BUILD_RECORD"],
        ):
            self.assertNotIn(forbidden, text)

    def test_image_preflight_requires_absolute_valid_build_record(self):
        valid_record = self.build_record()
        invalid_record = self.base / "invalid-build-record.json"
        invalid_record.write_text("not-json", encoding="utf-8")
        empty_record = self.base / "empty-build-record.json"
        empty_record.write_text("{}", encoding="utf-8")
        cases = [
            ("missing", None, "payment-hub-image-build-record-missing"),
            ("relative", "relative-build-record.json", "image-build-record-not-absolute"),
            ("absent", str(self.base / "absent.json"), "image-build-record-missing"),
            ("invalid", str(invalid_record), "image-build-record-invalid"),
            ("empty", str(empty_record), "image-build-record-invalid"),
        ]
        for name, value, reason in cases:
            with self.subTest(name):
                root = self.base / "artifacts" / ("build-record-" + name)
                runner = FakeRunner(
                    lambda *args: self.fail("image launcher must not run")
                )
                env = self.image_environment(build_record=valid_record)
                if value is None:
                    env.pop("PAYMENT_HUB_IMAGE_BUILD_RECORD", None)
                else:
                    env["PAYMENT_HUB_IMAGE_BUILD_RECORD"] = value
                with mock.patch.dict(os.environ, env, clear=False):
                    if value is None:
                        os.environ.pop("PAYMENT_HUB_IMAGE_BUILD_RECORD", None)
                    code = self.main(scope="image", root=root, runner=runner)
                self.assertNotEqual(code, 0)
                self.assertEqual(runner.calls, [])
                stage = self.checks(root)["stages"]["image"]
                self.assertEqual(stage["status"], "not-ready")
                self.assertIn("image-not-ready", stage["problems"])
                self.assertIn(reason, stage["metrics"]["not_ready_reasons"])

    def test_image_preflight_requires_script_testfiles_dockerfile_and_overlay(self):
        env = self.image_environment()
        missing_script = self.base / "missing-check-images.py"
        missing_files = (
            (self.base / "missing-image_test.go", "tests-image-missing"),
            (self.base / "missing-us1_helpers_test.go", "tests-us1-helpers-missing"),
            (self.base / "missing-Dockerfile.app", "dockerfile-app-missing"),
            (self.base / "missing-Dockerfile.migrate", "dockerfile-migrate-missing"),
            (self.base / "missing-compose.yaml", "compose-base-missing"),
            (self.base / "missing-compose.test.yaml", "compose-test-missing"),
            (self.base / "missing-compose.app.yaml", "overlay-app-missing"),
        )
        runner = FakeRunner(lambda *args: self.fail("image launcher must not run"))
        with mock.patch.object(check, "IMAGE_SCRIPT", missing_script), mock.patch.object(
            check, "IMAGE_PREFLIGHT_FILES", missing_files
        ), mock.patch.dict(os.environ, env, clear=False):
            code = self.main(scope="image", runner=runner)
        self.assertNotEqual(code, 0)
        self.assertEqual(runner.calls, [])
        stage = self.checks()["stages"]["image"]
        self.assertEqual(stage["status"], "not-ready")
        reasons = set(stage["metrics"]["not_ready_reasons"])
        self.assertIn("script-missing", reasons)
        for _, reason in missing_files:
            self.assertIn(reason, reasons)

    def test_image_missing_or_ambiguous_report_fails_closed(self):
        env = self.image_environment()
        cases = [
            ("missing", False, 0, "image-report-missing"),
            ("ambiguous", True, 1, "image-report-ambiguous"),
        ]
        for name, write_report, extra, problem in cases:
            with self.subTest(name):
                root = self.base / "artifacts" / ("report-" + name)
                runner = FakeRunner(
                    self.image_handler(
                        write_report=write_report, extra_reports=extra
                    )
                )
                with mock.patch.dict(os.environ, env, clear=False):
                    code = self.main(scope="image", root=root, runner=runner)
                self.assertNotEqual(code, 0)
                self.assertEqual(len(runner.calls), 1)
                stage = self.checks(root)["stages"]["image"]
                self.assertEqual(stage["status"], "fail")
                self.assertIn(problem, stage["problems"])
                self.assertIsNone(stage["report"])

    def test_image_report_subset_rejects_fake_or_incomplete_pass(self):
        cases = [
            ("schema", image_report(schema="wrong"), "image-report-invalid"),
            ("result", image_report(result="fail"), "image-report-not-pass"),
            ("failure", image_report(failure={"stage": "test"}), "image-report-not-pass"),
            ("source", image_report(source_unchanged=False), "image-report-source-changed"),
            ("build", image_report(build_verified=False), "image-report-build-record-not-verified"),
            ("pair", image_report(pair={"same_source": False}), "image-report-pair-invalid"),
            ("required-root", image_report(go_test={"required_root": "Other"}), "image-report-required-root-missing"),
            ("root-count", image_report(go_test={"root_count": 0}), "image-report-required-root-missing"),
            ("go-result", image_report(go_test={"result": "fail"}), "image-report-go-test-not-pass"),
            ("package", image_report(go_test={"package_result": "fail"}), "image-report-go-test-not-pass"),
            ("exit", image_report(go_test={"exit_code": 1}), "image-report-go-test-not-pass"),
            ("timed-out", image_report(go_test={"timed_out": True}), "image-report-go-test-not-pass"),
            ("cleanup-result", image_report(cleanup={"result": "fail"}), "image-report-cleanup-not-pass"),
            ("cleanup-resource", image_report(cleanup={"containers_absent": False}), "image-report-cleanup-resource-remaining"),
            ("cleanup-errors", image_report(cleanup={"errors": ["cleanup:compose"]}), "image-report-cleanup-not-pass"),
        ]
        env = self.image_environment()
        for name, report, problem in cases:
            with self.subTest(name):
                root = self.base / "artifacts" / ("subset-" + name)
                runner = FakeRunner(self.image_handler(copy.deepcopy(report)))
                with mock.patch.dict(os.environ, env, clear=False):
                    code = self.main(scope="image", root=root, runner=runner)
                self.assertNotEqual(code, 0)
                stage = self.checks(root)["stages"]["image"]
                self.assertEqual(stage["status"], "fail")
                self.assertIn(problem, stage["problems"])
                self.assertIsNone(stage["report"])

    def test_image_env_whitelist_is_not_applied_to_unit_or_db(self):
        env = self.image_environment()
        for scope in ("unit", "db"):
            with self.subTest(scope):
                root = self.base / "artifacts" / ("isolation-" + scope)
                runner = FakeRunner(self.default_handler)
                with mock.patch.dict(os.environ, env, clear=False):
                    code = self.main(scope=scope, root=root, runner=runner)
                self.assertEqual(code, 0)
                for call in runner.calls:
                    for key in self.IMAGE_ENV:
                        self.assertNotIn(key, call["env"])
                    if scope == "unit":
                        for key in self.DOCKER_ENV:
                            self.assertNotIn(key, call["env"])
                    else:
                        for key in self.DOCKER_ENV:
                            self.assertEqual(call["env"][key], env[key])

    def test_all_with_image_inputs_dispatches_image_without_omitting_it(self):
        env = self.image_environment()

        def handler(argv, cwd, env, timeout):
            result = self.default_handler(argv, cwd, env, timeout)
            if "check-images.py" in " ".join(argv):
                artifact_root = Path(argv[argv.index("--artifact-root") + 1])
                run_dir = artifact_root / IMAGE_RUN_ID
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "images.json").write_text(
                    json.dumps(image_report()), encoding="utf-8"
                )
            return result

        runner = FakeRunner(handler)
        with mock.patch.dict(os.environ, env, clear=False):
            code = self.main(scope="all", runner=runner)
        self.assertEqual(code, 0)
        report = self.checks()
        self.assertEqual(
            tuple(report["stages"]),
            ("format", "static", "unit", "db", "image"),
        )
        self.assertEqual(report["stages"]["image"]["status"], "pass")
        image_calls = [
            call
            for call in runner.calls
            if "check-images.py" in " ".join(call["argv"])
        ]
        self.assertEqual(len(image_calls), 1)
        self.assertIsNone(image_calls[0]["timeout"])


FAKE_GO_TEMPLATE = """#!{python}
import os
import subprocess
import sys
import time

PIDFILE = {pidfile!r}
MODE = {mode!r}

if len(sys.argv) > 1 and sys.argv[1] == "list":
    if MODE == "list":
        with open(PIDFILE, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        time.sleep(60)
    print("payment-hub/cmd/payment-hub")
    sys.exit(0)

if len(sys.argv) > 1 and sys.argv[1] == "test":
    if MODE == "leader-exit":
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", "import time; time.sleep(60)"]
        )
        with open(PIDFILE, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()) + " " + str(child.pid))
        sys.exit(0)
    with open(PIDFILE, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    time.sleep(60)

sys.exit(0)
"""


class UnitCancellationChainTests(EntrypointTestCase):
    """Real check.py -> ci_checks.py -> fake Go chain for owned-group cancel.

    No real Go, PostgreSQL, Docker, or network is used.  The fake Go process
    creates its own process group through ci_checks' start_new_session and the
    tests verify that the entrypoint does not leave that group behind.
    """

    @staticmethod
    def _group_members(pgid):
        members = []
        try:
            entries = list(Path("/proc").iterdir())
        except OSError:
            return members
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().split()
            except OSError:
                continue
            if len(fields) < 5:
                continue
            try:
                member_pgid = int(fields[4])
            except ValueError:
                continue
            if member_pgid == pgid:
                members.append((int(entry.name), fields[2]))
        return members

    def _run_cancel_chain(self, mode):
        pidfile = self.base / ("fake-go-" + mode + ".pid")
        script = self.base / "cancel-bin" / "go"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            FAKE_GO_TEMPLATE.format(
                python=sys.executable,
                pidfile=str(pidfile),
                mode=mode,
            ),
            encoding="utf-8",
        )
        script.chmod(0o755)
        cache = self.base / "cancel-cache"
        root = self.base / "cancel-artifacts"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "scripts/check.py",
                "--scope",
                "unit",
                "--go",
                str(script),
                "--cache-dir",
                str(cache),
                "--artifact-root",
                str(root),
            ],
            cwd=str(REPO),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        pgid = None
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not pidfile.exists():
                time.sleep(0.05)
            self.assertTrue(pidfile.exists(), "fake go did not start")
            pids = [
                int(value)
                for value in pidfile.read_text(encoding="utf-8").split()
            ]
            pgid = pids[0]
            time.sleep(0.15)
            os.kill(proc.pid, signal.SIGTERM)
            try:
                proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                self.fail("check.py did not exit after SIGTERM")

            self.assertNotEqual(proc.returncode, 0)

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                remaining = [
                    (pid, state)
                    for pid, state in self._group_members(pgid)
                    if state != "Z"
                ]
                if not remaining:
                    break
                time.sleep(0.05)
            self.assertEqual(
                [
                    (pid, state)
                    for pid, state in self._group_members(pgid)
                    if state != "Z"
                ],
                [],
            )

            checks = json.loads(
                (root / "checks.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checks["result"], "fail")
            self.assertEqual(checks["stages"]["unit"]["status"], "interrupted")
            self.assertIn("interrupted", checks["stages"]["unit"]["problems"])
            report = json.loads(
                (root / "unit" / "reports" / "go-default.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["result"], "fail")
            self.assertIn("interrupted", report["problems"])
        finally:
            if pgid is not None:
                members = self._group_members(pgid)
                if members:
                    for pid, _state in members:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
            if proc.poll() is None:
                proc.kill()
            try:
                proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass

    def test_sigterm_terminates_go_test_process_group(self):
        self._run_cancel_chain("test")

    def test_sigterm_during_go_list_terminates_process_group(self):
        self._run_cancel_chain("list")

    def test_sigterm_after_leader_exit_terminates_descendant_group(self):
        self._run_cancel_chain("leader-exit")


if __name__ == "__main__":
    unittest.main()
