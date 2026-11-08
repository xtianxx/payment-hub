"""Mock-only regression tests for scripts/check-ci-cancellation.py.

These tests never start Docker, PostgreSQL, Go, the actual launcher, or any
network service. They use standard-library fakes, temporary directories, and
in-memory pipes only. They are unit evidence, not PG/launcher cancellation
acceptance evidence.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import signal
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import importlib.util

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "check_ci_cancellation", REPO / "scripts/check-ci-cancellation.py"
)
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)

RUN_ID = "1" * 32
PROJECT = "payment-hub-test-db-" + RUN_ID
TOOL_ID = "a" * 64
PG_ID = "b" * 64
DOCKER = "/usr/bin/docker"
_UNSET = object()


def empty_snapshot():
    return {"containers": set(), "networks": set(), "volumes": set()}


def safe_commands(project=PROJECT):
    return [
        {
            "name": "compose down",
            "argv": [
                DOCKER,
                "compose",
                "--env-file",
                "/dev/null",
                "-f",
                "deploy/compose.yaml",
                "-f",
                "deploy/compose.test.yaml",
                "-p",
                project,
                "down",
            ],
        },
        {
            "name": "safety-entry stop",
            "argv": [DOCKER, "stop", "--time", "5", TOOL_ID],
        },
        {
            "name": "safety-entry remove",
            "argv": [DOCKER, "rm", "-f", TOOL_ID],
        },
        {
            "name": "volume remove",
            "argv": [DOCKER, "volume", "rm", f"{project}_postgres_data"],
        },
    ]


def launcher_report(
    run_id=RUN_ID,
    project=PROJECT,
    *,
    result="fail",
    failure=_UNSET,
    cleanup=_UNSET,
    commands=_UNSET,
):
    if failure is _UNSET:
        failure = {
            "stage": "interrupted",
            "kind": "sigterm",
            "message": "controlled interruption",
        }
    if cleanup is _UNSET:
        cleanup = {
            "result": "pass",
            "tool_containers": [
                {
                    "stage": "safety-entry",
                    "name": "ph-db-fixture-safety-entry",
                    "id": TOOL_ID,
                    "removed": True,
                    "absent": False,
                }
            ],
            "compose_down_exit": 0,
            "network_removed": True,
            "volume_name": f"{project}_postgres_data",
            "volume_retained": False,
            "volume_removed": True,
            "repeat_signal_ignored": False,
            "errors": [],
        }
    if commands is _UNSET:
        commands = safe_commands(project)
    return {
        "schema": MOD.LAUNCHER_SCHEMA,
        "run_id": run_id,
        "project": project,
        "result": result,
        "failure": failure,
        "cleanup": cleanup,
        "compose_created": {
            "containers": [PG_ID],
            "network": f"{project}_default",
            "volume": f"{project}_postgres_data",
        },
        "preflight": {"created": {"container_id": PG_ID}},
        "commands": commands,
    }


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, float(seconds))


class FakeDrain:
    def __init__(self, events=None, joined_on_finish=True):
        self.events = list(events or [])
        self.report_seen = False
        self.started = False
        self.finish_calls = 0
        self.last_timeout = None
        self.joined = False
        self._joined_on_finish = joined_on_finish

    def start(self):
        self.started = True

    def finish(self, timeout):
        self.finish_calls += 1
        self.last_timeout = timeout
        self.joined = self._joined_on_finish


class FakeProcess:
    def __init__(
        self,
        clock,
        run_dir,
        report_data,
        *,
        exit_at=None,
        exit_code=1,
        on_signal_exit=True,
        signal_delay=2.0,
        write_report=True,
        write_at_start=False,
        pid=4242,
    ):
        self.clock = clock
        self.run_dir = Path(run_dir)
        self.report_data = report_data
        self.exit_at = exit_at
        self.exit_code = exit_code
        self.on_signal_exit = on_signal_exit
        self.signal_delay = signal_delay
        self.write_report = write_report
        self.returncode = None
        self.signals = 0
        self.kill_calls = 0
        self.pid = pid
        self.stdout = None
        self.stderr = None
        self._written = False
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if write_at_start:
            self._write()

    def _write(self):
        if not self.write_report or self._written:
            return
        (self.run_dir / "db-foundation.json").write_text(
            json.dumps(self.report_data), encoding="utf-8"
        )
        self._written = True

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if self.exit_at is not None and self.clock() >= self.exit_at:
            self._write()
            self.returncode = self.exit_code
        return self.returncode

    def signal(self):
        self.signals += 1
        if self.on_signal_exit and self.exit_at is None:
            self.exit_at = self.clock() + self.signal_delay

    def kill(self):
        self.kill_calls += 1


class FakeDocker:
    def __init__(
        self,
        *,
        pg_ids=None,
        state=None,
        pre=None,
        post=None,
        absence=None,
    ):
        self.pg_ids = [PG_ID] if pg_ids is None else list(pg_ids)
        self.state = state or {
            "Status": "running",
            "Running": True,
            "Health": {"Status": "healthy"},
        }
        self.pre = pre if pre is not None else empty_snapshot()
        self.post = post if post is not None else self.pre
        self.absence_result = (
            absence
            if absence is not None
            else {"containers": True, "network": True, "volume": True}
        )
        self.snapshot_calls = 0

    def snapshot(self, timeout=None):
        self.snapshot_calls += 1
        return self.pre if self.snapshot_calls == 1 else self.post

    def find_pg(self, project, timeout=None):
        return list(self.pg_ids)

    def inspect_pg(self, container_id, timeout=None):
        return dict(self.state)

    def absence(self, run_id, project, timeout=None):
        return dict(self.absence_result)


class DeadlineDocker(FakeDocker):
    """Fake Docker that advances the mock clock and rejects post-deadline queries."""

    def __init__(self, clock, deadline, *, advance_on="inspect_pg"):
        super().__init__()
        self.clock = clock
        self.deadline = deadline
        self.advance_on = advance_on
        self.snapshot_calls = 0
        self.post_queries = 0
        self.query_timeouts = []

    def _record(self, timeout):
        self.query_timeouts.append(timeout)

    def _advance(self):
        self.clock.now = self.deadline + 40.0

    def _reject_if_late(self, label):
        if self.clock() >= self.deadline:
            self.post_queries += 1
            raise AssertionError(f"post-deadline {label} query started")

    def snapshot(self, timeout=None):
        self._record(timeout)
        self.snapshot_calls += 1
        if self.snapshot_calls > 1:
            self._reject_if_late("snapshot")
        return self.pre

    def find_pg(self, project, timeout=None):
        self._record(timeout)
        return list(self.pg_ids)

    def inspect_pg(self, container_id, timeout=None):
        self._record(timeout)
        state = dict(self.state)
        if self.advance_on == "inspect_pg":
            self._advance()
        return state

    def absence(self, run_id, project, timeout=None):
        self._record(timeout)
        self._reject_if_late("absence")
        return dict(self.absence_result)


class CliConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cancel-cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifact = self.root / "artifact"
        self.artifact.mkdir()
        self.report = self.root / "report"
        self.report.mkdir()
        self.cache = self.root / "cache"

    def args(self, **overrides):
        values = {
            "go": sys.executable,
            "cache_dir": str(self.cache),
            "artifact_root": str(self.artifact),
            "report_dir": str(self.report),
            "prepare_images": False,
        }
        values.update(overrides)
        return type("Args", (), values)()

    def test_parse_args_requires_frozen_options(self):
        parsed = MOD.parse_args(
            [
                "--go",
                sys.executable,
                "--cache-dir",
                str(self.cache),
                "--artifact-root",
                str(self.artifact),
                "--report-dir",
                str(self.report),
            ]
        )
        self.assertEqual(parsed.go, sys.executable)
        self.assertFalse(parsed.prepare_images)
        with self.assertRaises(SystemExit):
            MOD.parse_args(["--go", sys.executable])

    def test_parse_args_accepts_only_prepare_images_switch(self):
        parsed = MOD.parse_args(
            [
                "--go",
                sys.executable,
                "--cache-dir",
                str(self.cache),
                "--artifact-root",
                str(self.artifact),
                "--report-dir",
                str(self.report),
                "--prepare-images",
            ]
        )
        self.assertTrue(parsed.prepare_images)
        with self.assertRaises(SystemExit):
            MOD.parse_args(
                [
                    "--go",
                    sys.executable,
                    "--cache-dir",
                    str(self.cache),
                    "--artifact-root",
                    str(self.artifact),
                    "--report-dir",
                    str(self.report),
                    "--download-modules",
                ]
            )

    def test_validate_config_accepts_empty_artifact_root(self):
        config = MOD.validate_config(self.args())
        self.assertEqual(config.artifact_root, self.artifact.resolve())
        self.assertEqual(config.report_path.name, MOD.REPORT_FILENAME)

    def test_validate_config_rejects_relative_go(self):
        with self.assertRaises(MOD.CancellationError) as caught:
            MOD.validate_config(self.args(go="python3"))
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("cli", "go"))

    def test_validate_config_rejects_nonempty_artifact_root(self):
        (self.artifact / "leftover").write_text("x", encoding="utf-8")
        with self.assertRaises(MOD.CancellationError) as caught:
            MOD.validate_config(self.args())
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cli", "artifact-root"),
        )

    def test_validate_config_rejects_report_dir_inside_artifact_root(self):
        inside = self.artifact / "report"
        with self.assertRaises(MOD.CancellationError) as caught:
            MOD.validate_config(self.args(report_dir=str(inside)))
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cli", "artifact-root"),
        )

    def test_validate_config_rejects_existing_report(self):
        (self.report / MOD.REPORT_FILENAME).write_text("{}", encoding="utf-8")
        with self.assertRaises(MOD.CancellationError) as caught:
            MOD.validate_config(self.args())
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cli", "report-exists"),
        )

    def test_build_launcher_argv_is_exact_and_has_no_debug_flags(self):
        config = MOD.validate_config(self.args(prepare_images=True))
        argv = MOD.build_launcher_argv(config)
        self.assertEqual(
            argv,
            [
                sys.executable,
                str(REPO / "scripts" / "check-db-foundation.py"),
                "--go",
                str(Path(sys.executable).resolve()),
                "--cache-dir",
                str(self.cache.resolve()),
                "--artifact-root",
                str(self.artifact.resolve()),
                "--cleanup-owned-resources",
                "--prepare-images",
            ],
        )
        joined = " ".join(argv)
        self.assertNotIn("--download-modules", joined)
        self.assertNotIn("--debug", joined)
        self.assertNotIn("--verbose", joined)

    def test_main_early_failure_writes_safe_report(self):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = MOD.main(
                [
                    "--go",
                    str(self.root / "missing-go"),
                    "--cache-dir",
                    str(self.cache),
                    "--artifact-root",
                    str(self.artifact),
                    "--report-dir",
                    str(self.report),
                ]
            )
        self.assertEqual(code, 1)
        path = self.report / MOD.REPORT_FILENAME
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], MOD.SCHEMA)
        self.assertEqual(data["result"], "fail")
        self.assertIsNone(data["run_id"])
        self.assertIsNone(data["project"])
        self.assertIn("cancellation-check: result fail", out.getvalue())
        self.assertIn("ERROR: cli/go", err.getvalue())


class DiscoverAndDeriveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cancel-discover-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_derive_project_is_strict(self):
        self.assertEqual(MOD.derive_project(RUN_ID), PROJECT)
        for value in ("", "A" * 32, "1" * 31, "1" * 33, "../" + RUN_ID):
            with self.assertRaises(MOD.CancellationError):
                MOD.derive_project(value)

    def test_discover_run_single(self):
        (self.root / RUN_ID).mkdir()
        self.assertEqual(MOD.discover_run(self.root), RUN_ID)

    def test_discover_run_none_when_empty(self):
        self.assertIsNone(MOD.discover_run(self.root))

    def test_discover_run_rejects_multiple(self):
        (self.root / RUN_ID).mkdir()
        (self.root / ("2" * 32)).mkdir()
        with self.assertRaises(MOD.CancellationError) as caught:
            MOD.discover_run(self.root)
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("pg-wait", "run-dir"))

    def test_discover_run_rejects_unexpected_entry(self):
        (self.root / RUN_ID).mkdir()
        (self.root / "notes.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(MOD.CancellationError):
            MOD.discover_run(self.root)


class OutputDrainTests(unittest.TestCase):
    def _pipe(self):
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb", buffering=0)
        return stream, write_fd

    def test_drain_keeps_only_whitelisted_prefixes_and_discards_raw_report(self):
        stream, write_fd = self._pipe()
        drain = MOD.OutputDrain(stream, None)
        drain.start()
        os.write(
            write_fd,
            (
                f"run-id: {RUN_ID}\n"
                "result: fail stage=interrupted kind=sigterm\n"
                "cleanup-result: pass\n"
                "ERROR: cleanup/not-pass\n"
                "db-foundation-report: {\"commands\":[\"SECRET\"]}\n"
                "postgresql://user:password@127.0.0.1/db\n"
            ).encode(),
        )
        os.close(write_fd)
        drain.finish(2.0)
        self.assertTrue(drain.joined)
        self.assertTrue(drain.report_seen)
        self.assertEqual(
            drain.events,
            [
                f"run-id:{RUN_ID}",
                "result:fail stage=interrupted kind=sigterm",
                "cleanup-result:pass",
                "error:cleanup/not-pass",
            ],
        )
        self.assertNotIn("SECRET", " ".join(drain.events))
        self.assertNotIn("postgresql://", " ".join(drain.events))

    def test_drain_bounds_huge_output_and_joins(self):
        stream, write_fd = self._pipe()
        drain = MOD.OutputDrain(stream, None, max_events=4, max_line=64)
        drain.start()
        os.write(write_fd, b"x" * 8192 + b"\n")
        os.write(write_fd, f"run-id: {RUN_ID}\n".encode())
        os.close(write_fd)
        drain.finish(2.0)
        self.assertTrue(drain.joined)
        self.assertLessEqual(len(drain.events), 4)
        self.assertIn(f"run-id:{RUN_ID}", drain.events)

    def test_drain_flushes_partial_line_at_eof(self):
        stream, write_fd = self._pipe()
        drain = MOD.OutputDrain(stream, None)
        drain.start()
        os.write(write_fd, f"run-id: {RUN_ID}".encode())
        os.close(write_fd)
        drain.finish(2.0)
        self.assertEqual(drain.events, [f"run-id:{RUN_ID}"])

    def test_drain_does_not_persist_secret(self):
        stream, write_fd = self._pipe()
        drain = MOD.OutputDrain(stream, None)
        drain.start()
        os.write(
            write_fd,
            b"postgresql://user:password@host/db\n"
            b"GOOSE_DBSTRING=secret\n"
            b"PAYMENT_HUB_TEST_DATABASE_URL=secret\n",
        )
        os.close(write_fd)
        drain.finish(2.0)
        self.assertEqual(drain.events, [])
        self.assertNotIn("password", json.dumps(drain.events))


class ReportLoadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cancel-report-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "db-foundation.json"

    def test_load_report_stable_hash_and_data(self):
        payload = launcher_report()
        raw = json.dumps(payload).encode()
        self.path.write_bytes(raw)
        data, digest = MOD.load_report_stable(self.path)
        self.assertEqual(data["run_id"], RUN_ID)
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())

    def test_load_report_stable_rejects_changed_source(self):
        self.path.write_bytes(b'{"a":1}')
        with mock.patch.object(
            Path, "read_bytes", side_effect=[b'{"a":1}', b'{"a":2}']
        ):
            with self.assertRaises(MOD.CancellationError) as caught:
                MOD.load_report_stable(self.path)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("launcher", "report-mismatch"),
        )

    def test_load_report_stable_rejects_missing(self):
        with self.assertRaises(MOD.CancellationError):
            MOD.load_report_stable(self.path)

    def test_load_report_stable_rejects_non_object(self):
        self.path.write_text("[]", encoding="utf-8")
        with self.assertRaises(MOD.CancellationError):
            MOD.load_report_stable(self.path)

    def test_load_report_stable_rejects_symlink(self):
        target = self.root / "target.json"
        target.write_text(json.dumps(launcher_report()), encoding="utf-8")
        self.path.symlink_to(target)
        with self.assertRaises(MOD.CancellationError):
            MOD.load_report_stable(self.path)


class VerifyLauncherReportTests(unittest.TestCase):
    def test_valid_interrupted_report_passes(self):
        MOD.verify_launcher_report(launcher_report(), RUN_ID, PROJECT)

    def test_completed_report_rejected(self):
        with self.assertRaises(MOD.CancellationError) as caught:
            MOD.verify_launcher_report(
                launcher_report(result="pass", failure=None), RUN_ID, PROJECT
            )
        self.assertEqual((caught.exception.stage, caught.exception.kind), ("launcher", "report-mismatch"))

    def test_wrong_schema_run_project_rejected(self):
        cases = [
            ("schema", "payment-hub.db-foundation.v0"),
            ("run_id", "2" * 32),
            ("project", "payment-hub-test-db-" + "2" * 32),
        ]
        for key, value in cases:
            with self.subTest(key=key):
                report = launcher_report()
                report[key] = value
                with self.assertRaises(MOD.CancellationError):
                    MOD.verify_launcher_report(report, RUN_ID, PROJECT)

    def test_wrong_failure_rejected(self):
        for failure in (
            {"stage": "interrupted", "kind": "sigint"},
            {"stage": "cleanup", "kind": "cleanup-failed"},
            None,
        ):
            with self.subTest(failure=failure):
                with self.assertRaises(MOD.CancellationError):
                    MOD.verify_launcher_report(
                        launcher_report(failure=failure), RUN_ID, PROJECT
                    )

    def test_cleanup_variants_rejected(self):
        variants = [
            {"result": "fail"},
            {"errors": ["cleanup:volume-remains"]},
            {"repeat_signal_ignored": True},
            {"compose_down_exit": 1},
            {"network_removed": False},
            {"volume_retained": True},
            {"volume_removed": False, "volume_absent": False},
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                report = launcher_report()
                report["cleanup"].update(variant)
                with self.assertRaises(MOD.CancellationError):
                    MOD.verify_launcher_report(report, RUN_ID, PROJECT)

    def test_tool_container_not_removed_rejected(self):
        report = launcher_report()
        report["cleanup"]["tool_containers"] = [
            {"stage": "safety-entry", "id": TOOL_ID, "removed": False, "absent": False}
        ]
        with self.assertRaises(MOD.CancellationError):
            MOD.verify_launcher_report(report, RUN_ID, PROJECT)

    def test_unsafe_cleanup_commands_rejected(self):
        cases = [
            [{"name": "global", "argv": [DOCKER, "system", "prune", "-af"]}],
            [
                {
                    "name": "down-v",
                    "argv": [DOCKER, "compose", "-p", PROJECT, "down", "-v"],
                }
            ],
            [
                {
                    "name": "wrong-project",
                    "argv": [DOCKER, "compose", "-p", "other", "down"],
                }
            ],
            [{"name": "unregistered", "argv": [DOCKER, "rm", "-f", "deadbeefdeadbeef"]}],
            [{"name": "wrong-volume", "argv": [DOCKER, "volume", "rm", "other_volume"]}],
            [{"name": "wrong-network", "argv": [DOCKER, "network", "rm", "other_network"]}],
            [{"name": "remote", "argv": [DOCKER, "--host", "tcp://remote", "ps"]}],
        ]
        for commands in cases:
            with self.subTest(commands=commands):
                with self.assertRaises(MOD.CancellationError) as caught:
                    MOD.verify_launcher_report(
                        launcher_report(commands=commands), RUN_ID, PROJECT
                    )
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    ("cleanup", "unsafe-command"),
                )

    def test_safe_cleanup_commands_accepted(self):
        MOD.verify_launcher_report(launcher_report(), RUN_ID, PROJECT)

    def test_preexisting_subset(self):
        pre = {
            "containers": {"keep-container"},
            "networks": {"keep-network"},
            "volumes": {"keep-volume"},
        }
        post = {
            "containers": {"keep-container", "new-container"},
            "networks": {"keep-network", "new-network"},
            "volumes": {"keep-volume", "new-volume"},
        }
        self.assertTrue(MOD.preexisting_subset(pre, post))
        post["containers"].discard("keep-container")
        self.assertFalse(MOD.preexisting_subset(pre, post))


class CancellationReportSchemaTests(unittest.TestCase):
    def test_pass_schema_exact_and_no_raw_fields(self):
        data = MOD.build_cancellation_report(
            result="pass",
            run_id=RUN_ID,
            project=PROJECT,
            launcher_exit=1,
            failure=("interrupted", "sigterm"),
            cleanup={
                "result": "pass",
                "compose_down_exit": 0,
                "network_removed": True,
                "volume_removed": True,
                "volume_retained": False,
                "repeat_signal_ignored": False,
                "error_count": 0,
                "tool_containers_removed": True,
            },
            resource_absence={"containers": True, "network": True, "volume": True},
            preexisting_resource_preserved=True,
            elapsed_seconds=12.3456,
            pg_healthy_seconds=10.0,
            exit_after_signal_seconds=2.3456,
            input_sha256="f" * 64,
        )
        self.assertEqual(
            set(data),
            {
                "schema",
                "result",
                "run_id",
                "project",
                "stage",
                "signal",
                "launcher_exit",
                "failure",
                "cleanup",
                "resource_absence",
                "preexisting_resource_preserved",
                "elapsed_seconds",
                "timings",
                "input_sha256",
            },
        )
        self.assertEqual(set(data["cleanup"]), {
            "result",
            "compose_down_exit",
            "network_removed",
            "volume_removed",
            "volume_retained",
            "repeat_signal_ignored",
            "error_count",
            "tool_containers_removed",
        })
        self.assertEqual(set(data["timings"]), {"pg_healthy_seconds", "exit_after_signal_seconds"})
        self.assertEqual(data["failure"], {"stage": "interrupted", "kind": "sigterm"})
        self.assertEqual(data["stage"], MOD.STAGE)
        self.assertEqual(data["signal"], MOD.SIGNAL_NAME)
        encoded = json.dumps(data, sort_keys=True)
        for forbidden in (
            "commands",
            "argv",
            "env",
            "DSN",
            "postgresql://",
            "password",
            "raw",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_failure_enum_normalized(self):
        data = MOD.build_cancellation_report(
            result="fail",
            run_id=None,
            project=None,
            launcher_exit=None,
            failure=("not-a-stage", "not-a-kind"),
            cleanup=None,
            resource_absence=None,
            preexisting_resource_preserved=False,
            elapsed_seconds=None,
            pg_healthy_seconds=None,
            exit_after_signal_seconds=None,
            input_sha256=None,
        )
        self.assertEqual(data["failure"], {"stage": "internal", "kind": "unexpected"})
        self.assertEqual(data["cleanup"]["result"], "unknown")
        self.assertFalse(data["resource_absence"]["containers"])
        self.assertEqual(data["elapsed_seconds"], 0.0)

    def test_summarize_cleanup_no_error_strings(self):
        summary = MOD.summarize_cleanup(
            {
                "cleanup": {
                    "result": "fail",
                    "compose_down_exit": 1,
                    "network_removed": False,
                    "volume_removed": False,
                    "volume_retained": True,
                    "repeat_signal_ignored": True,
                    "errors": ["cleanup:secret-marker", "cleanup:other"],
                    "tool_containers": [{"removed": False, "absent": False}],
                }
            }
        )
        self.assertEqual(summary["error_count"], 2)
        self.assertNotIn("secret-marker", json.dumps(summary))
        self.assertFalse(summary["tool_containers_removed"])
        self.assertTrue(summary["volume_retained"])


class RunCheckOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cancel-run-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifact = self.root / "artifact"
        self.artifact.mkdir()
        self.report_dir = self.root / "report"
        self.report_dir.mkdir()
        self.config = MOD.Config(
            go=sys.executable,
            cache_dir=self.root / "cache",
            artifact_root=self.artifact,
            report_dir=self.report_dir,
            prepare_images=False,
        )

    def runtime(
        self,
        report_data,
        *,
        docker=None,
        proc_kwargs=None,
        drain=None,
        signal_sender=None,
        clock=None,
    ):
        clock = clock or FakeClock()
        holder = {}
        proc_kwargs = proc_kwargs or {}

        def factory(argv):
            proc = FakeProcess(clock, self.artifact / RUN_ID, report_data, **proc_kwargs)
            holder["proc"] = proc
            return proc

        signals = []

        def send(pid):
            signals.append(pid)
            if signal_sender is not None:
                return signal_sender(holder["proc"])
            holder["proc"].signal()

        runtime = MOD.Runtime(
            process_factory=factory,
            docker=docker or FakeDocker(),
            drain_factory=lambda out, err: drain or FakeDrain(),
            signal_sender=send,
            clock=clock,
            sleep=clock.sleep,
        )
        return runtime, holder, signals

    def _run(self, runtime):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = MOD.run_check(self.config, runtime)
        return code, out.getvalue(), err.getvalue()

    def read_report(self):
        path = self.report_dir / MOD.REPORT_FILENAME
        self.assertTrue(path.exists())
        return json.loads(path.read_text(encoding="utf-8"))

    def test_pass_sends_one_sigterm_and_writes_pass(self):
        runtime, holder, signals = self.runtime(launcher_report())
        code, out, _ = self._run(runtime)
        self.assertEqual(code, 0)
        self.assertEqual(signals, [holder["proc"].pid])
        self.assertEqual(holder["proc"].signals, 1)
        data = self.read_report()
        self.assertEqual(data["result"], "pass")
        self.assertEqual(data["run_id"], RUN_ID)
        self.assertEqual(data["project"], PROJECT)
        self.assertEqual(data["stage"], "pg-healthy")
        self.assertEqual(data["signal"], "SIGTERM")
        self.assertEqual(data["launcher_exit"], 1)
        self.assertEqual(data["failure"], {"stage": "interrupted", "kind": "sigterm"})
        self.assertEqual(data["cleanup"]["result"], "pass")
        self.assertEqual(data["cleanup"]["error_count"], 0)
        self.assertTrue(all(data["resource_absence"].values()))
        self.assertTrue(data["preexisting_resource_preserved"])
        self.assertEqual(len(data["input_sha256"]), 64)
        self.assertIn("cancellation-check: result pass", out)
        self.assertNotIn("postgresql://", out)

    def test_completed_process_before_signal_is_not_cancellation(self):
        runtime, holder, signals = self.runtime(
            launcher_report(result="pass", failure=None),
            proc_kwargs={"exit_at": 0.0, "exit_code": 0},
        )
        code, _, _ = self._run(runtime)
        self.assertEqual(code, 1)
        self.assertEqual(signals, [])
        self.assertEqual(holder["proc"].signals, 0)
        data = self.read_report()
        self.assertEqual(data["result"], "fail")
        self.assertEqual(data["failure"], {"stage": "pg-wait", "kind": "launcher-exited"})
        self.assertEqual(data["launcher_exit"], 0)

    def test_report_present_before_signal_is_not_cancellation(self):
        runtime, holder, signals = self.runtime(
            launcher_report(),
            proc_kwargs={"write_at_start": True},
        )
        code, _, _ = self._run(runtime)
        self.assertEqual(code, 1)
        self.assertEqual(signals, [holder["proc"].pid])
        data = self.read_report()
        self.assertEqual(data["result"], "fail")
        self.assertEqual(data["failure"], {"stage": "pg-wait", "kind": "report-present"})

    def test_wrong_report_identity_fails(self):
        wrong = launcher_report(run_id="2" * 32, project="payment-hub-test-db-" + "2" * 32)
        runtime, holder, signals = self.runtime(wrong)
        code, _, _ = self._run(runtime)
        self.assertEqual(code, 1)
        self.assertEqual(len(signals), 1)
        data = self.read_report()
        self.assertEqual(data["result"], "fail")
        self.assertEqual(data["failure"], {"stage": "launcher", "kind": "report-mismatch"})

    def test_not_healthy_times_out_and_fault_signals_once(self):
        docker = FakeDocker(
            state={"Status": "starting", "Running": True, "Health": {"Status": "starting"}},
            absence={"containers": False, "network": False, "volume": False},
        )
        runtime, holder, signals = self.runtime(launcher_report(), docker=docker)
        code, _, _ = self._run(runtime)
        self.assertEqual(code, 1)
        self.assertEqual(len(signals), 1)
        self.assertEqual(holder["proc"].signals, 1)
        data = self.read_report()
        self.assertEqual(data["result"], "fail")
        self.assertEqual(data["failure"], {"stage": "pg-wait", "kind": "timeout"})
        self.assertGreaterEqual(data["elapsed_seconds"], 180.0)

    def test_unhealthy_fails_without_full_budget(self):
        docker = FakeDocker(
            state={"Status": "running", "Running": True, "Health": {"Status": "unhealthy"}},
            absence={"containers": False, "network": False, "volume": False},
        )
        runtime, holder, signals = self.runtime(launcher_report(), docker=docker)
        code, _, _ = self._run(runtime)
        self.assertEqual(code, 1)
        self.assertEqual(len(signals), 1)
        data = self.read_report()
        self.assertEqual(data["failure"], {"stage": "pg-wait", "kind": "not-healthy"})
        self.assertLess(data["elapsed_seconds"], 180.0)

    def test_exit_timeout_does_not_resend_or_kill(self):
        runtime, holder, signals = self.runtime(
            launcher_report(),
            proc_kwargs={"on_signal_exit": False},
            docker=FakeDocker(absence={"containers": False, "network": False, "volume": False}),
        )
        code, _, _ = self._run(runtime)
        self.assertEqual(code, 1)
        self.assertEqual(len(signals), 1)
        self.assertEqual(holder["proc"].signals, 1)
        self.assertEqual(holder["proc"].kill_calls, 0)
        data = self.read_report()
        self.assertEqual(data["result"], "fail")
        self.assertEqual(data["failure"], {"stage": "exit", "kind": "timeout"})
        self.assertIsNone(data["launcher_exit"])
        self.assertFalse(any(data["resource_absence"].values()))

    def test_signal_failure_not_retried(self):
        def raise_missing(proc):
            raise ProcessLookupError()

        runtime, holder, signals = self.runtime(
            launcher_report(), signal_sender=raise_missing
        )
        code, _, _ = self._run(runtime)
        self.assertEqual(code, 1)
        self.assertEqual(len(signals), 1)
        data = self.read_report()
        self.assertEqual(data["result"], "fail")
        self.assertEqual(data["failure"], {"stage": "signal", "kind": "not-running"})

    def test_false_cleanup_fails(self):
        report = launcher_report()
        report["cleanup"]["errors"] = ["cleanup:volume-remains"]
        runtime, holder, signals = self.runtime(report)
        code, _, _ = self._run(runtime)
        self.assertEqual(code, 1)
        self.assertEqual(len(signals), 1)
        data = self.read_report()
        self.assertEqual(data["result"], "fail")
        self.assertEqual(data["failure"], {"stage": "cleanup", "kind": "not-pass"})
        self.assertEqual(data["cleanup"]["error_count"], 1)

    def test_resource_remains_fails(self):
        docker = FakeDocker(
            absence={"containers": False, "network": True, "volume": True}
        )
        runtime, holder, signals = self.runtime(launcher_report(), docker=docker)
        code, _, _ = self._run(runtime)
        self.assertEqual(code, 1)
        self.assertEqual(len(signals), 1)
        data = self.read_report()
        self.assertEqual(data["failure"], {"stage": "resources", "kind": "remain"})
        self.assertFalse(data["resource_absence"]["containers"])

    def test_preexisting_lost_fails(self):
        pre = {
            "containers": {"keep-container"},
            "networks": {"keep-network"},
            "volumes": {"keep-volume"},
        }
        post = {"containers": set(), "networks": {"keep-network"}, "volumes": {"keep-volume"}}
        docker = FakeDocker(pre=pre, post=post)
        runtime, holder, signals = self.runtime(launcher_report(), docker=docker)
        code, _, _ = self._run(runtime)
        self.assertEqual(code, 1)
        data = self.read_report()
        self.assertEqual(data["failure"], {"stage": "preexisting", "kind": "lost"})
        self.assertFalse(data["preexisting_resource_preserved"])

    def test_malicious_drain_events_not_persisted(self):
        drain = FakeDrain(
            events=[
                "postgresql://user:password@host/db",
                "db-foundation-report: {\"commands\":[\"SECRET\"]}",
            ]
        )
        runtime, holder, signals = self.runtime(launcher_report(), drain=drain)
        code, _, _ = self._run(runtime)
        self.assertEqual(code, 0)
        raw = (self.report_dir / MOD.REPORT_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn("postgresql://", raw)
        self.assertNotIn("SECRET", raw)
        self.assertNotIn("db-foundation-report", raw)

    def test_report_filename_fixed(self):
        runtime, holder, signals = self.runtime(launcher_report())
        code, out, _ = self._run(runtime)
        self.assertEqual(code, 0)
        files = sorted(path.name for path in self.report_dir.iterdir())
        self.assertEqual(files, [MOD.REPORT_FILENAME])
        self.assertIn(
            f"cancellation-check: report {self.report_dir / MOD.REPORT_FILENAME}",
            out,
        )

    def test_drain_not_joined_fails(self):
        drain = FakeDrain(joined_on_finish=False)
        runtime, holder, signals = self.runtime(launcher_report(), drain=drain)
        code, _, _ = self._run(runtime)
        self.assertEqual(code, 1)
        data = self.read_report()
        self.assertEqual(data["result"], "fail")
        self.assertEqual(data["failure"], {"stage": "drain", "kind": "not-joined"})

    def test_deadline_expiry_during_pg_query_skips_post_queries_and_signals_once(self):
        clock = FakeClock()
        deadline = clock.now + MOD.BUDGETS["total_seconds"]
        docker = DeadlineDocker(clock, deadline, advance_on="inspect_pg")
        drain = FakeDrain()
        runtime, holder, signals = self.runtime(
            launcher_report(), docker=docker, drain=drain, clock=clock
        )
        code, _, _ = self._run(runtime)
        self.assertEqual(code, 1)
        self.assertEqual(len(signals), 1)
        self.assertEqual(holder["proc"].signals, 1)
        self.assertEqual(docker.post_queries, 0)
        self.assertEqual(drain.last_timeout, 0.0)
        self.assertTrue(docker.query_timeouts)
        self.assertTrue(all(t is not None for t in docker.query_timeouts))
        self.assertTrue(
            all(t <= MOD.BUDGETS["total_seconds"] for t in docker.query_timeouts)
        )
        data = self.read_report()
        self.assertEqual(data["result"], "fail")
        self.assertEqual(data["failure"], {"stage": "deadline", "kind": "expired"})
        self.assertEqual(data["cleanup"]["result"], "unknown")
        self.assertIsNone(data["launcher_exit"])
        self.assertFalse(any(data["resource_absence"].values()))
        self.assertFalse(data["preexisting_resource_preserved"])
        self.assertIsNone(data["input_sha256"])
        self.assertGreaterEqual(
            data["elapsed_seconds"], MOD.BUDGETS["total_seconds"]
        )

    def test_deadline_expiry_after_signal_does_not_repeat_signal_or_query(self):
        clock = FakeClock()
        deadline = clock.now + MOD.BUDGETS["total_seconds"]
        docker = DeadlineDocker(clock, deadline, advance_on="none")
        drain = FakeDrain()

        def advance_signal(proc):
            clock.now = deadline + 40.0
            proc.signal()

        runtime, holder, signals = self.runtime(
            launcher_report(),
            docker=docker,
            drain=drain,
            signal_sender=advance_signal,
            clock=clock,
        )
        code, _, _ = self._run(runtime)
        self.assertEqual(code, 1)
        self.assertEqual(len(signals), 1)
        self.assertEqual(holder["proc"].signals, 1)
        self.assertEqual(docker.post_queries, 0)
        self.assertEqual(drain.last_timeout, 0.0)
        self.assertTrue(all(t is not None for t in docker.query_timeouts))
        self.assertTrue(
            all(t <= MOD.BUDGETS["total_seconds"] for t in docker.query_timeouts)
        )
        data = self.read_report()
        self.assertEqual(data["result"], "fail")
        self.assertEqual(data["failure"], {"stage": "deadline", "kind": "expired"})
        self.assertIsNone(data["launcher_exit"])
        self.assertFalse(any(data["resource_absence"].values()))


class SourceAstTests(unittest.TestCase):
    def setUp(self):
        self.source = (REPO / "scripts" / "check-ci-cancellation.py").read_text(
            encoding="utf-8"
        )
        self.tree = ast.parse(self.source)

    def test_source_has_no_killpg_sigkill_or_shell(self):
        self.assertNotIn("killpg", self.source)
        self.assertNotIn("SIGKILL", self.source)
        self.assertNotIn("shell=True", self.source)
        self.assertNotIn("pkill", self.source)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotEqual(node.func.attr, "killpg")
                for keyword in node.keywords:
                    if keyword.arg == "shell":
                        self.assertFalse(
                            isinstance(keyword.value, ast.Constant)
                            and keyword.value.value is True
                        )

    def test_only_sigterm_is_sent_to_a_pid(self):
        kill_calls = []
        for node in ast.walk(self.tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "kill"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
            ):
                kill_calls.append(node)
        self.assertEqual(len(kill_calls), 1)
        call = kill_calls[0]
        self.assertGreaterEqual(len(call.args), 2)
        second = call.args[1]
        self.assertTrue(
            isinstance(second, ast.Attribute)
            and second.attr == "SIGTERM"
            and isinstance(second.value, ast.Name)
            and second.value.id == "signal"
        )

    def test_source_has_no_network_imports(self):
        forbidden = {"requests", "urllib", "http", "socket", "aiohttp", "httpx"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], forbidden)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module.split(".")[0], forbidden)


if __name__ == "__main__":
    unittest.main(verbosity=2)
