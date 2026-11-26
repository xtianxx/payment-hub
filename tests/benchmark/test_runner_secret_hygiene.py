import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_payment_create import (  # noqa: E402
    MONITOR_ROLE,
    PreconditionError,
    RunError,
    Runner,
    parse_args,
)

PASSWORD = "0123456789abcdef" * 2


def make_runner(artifacts_dir):
    runner = Runner(parse_args([
        "--migrate-image", "local/migrate:test",
        "--artifacts-dir", str(artifacts_dir),
    ]))
    runner.prepare_dirs()
    return runner


class MonitorPasswordFileTest(unittest.TestCase):
    def test_password_file_requires_0600_and_one_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "monitor-password"
            path.write_text(PASSWORD + "\n", encoding="utf-8")
            os.chmod(path, 0o644)
            with mock.patch.dict(
                os.environ,
                {"PAYMENT_HUB_MONITOR_PASSWORD_FILE": str(path)},
            ):
                with self.assertRaises(PreconditionError) as permissive:
                    make_runner(Path(tmp) / "runs").read_monitor_password()
                self.assertNotIn(PASSWORD, str(permissive.exception))

                os.chmod(path, 0o600)
                runner = make_runner(Path(tmp) / "runs")
                self.assertEqual(runner.read_monitor_password(), PASSWORD)
                self.assertEqual(runner.monitor_password_path, path)

                path.write_text("first\nsecond\n", encoding="utf-8")
                with self.assertRaises(PreconditionError) as multiline:
                    runner.read_monitor_password()
                self.assertNotIn("first", str(multiline.exception))

    def test_missing_password_file_fails_without_reading_a_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent"
            with mock.patch.dict(
                os.environ,
                {"PAYMENT_HUB_MONITOR_PASSWORD_FILE": str(missing)},
            ):
                with self.assertRaises(PreconditionError) as caught:
                    make_runner(Path(tmp) / "runs").read_monitor_password()
            self.assertIn(str(missing), str(caught.exception))


class MonitorRoleSecretHygieneTest(unittest.TestCase):
    def test_role_creation_keeps_password_out_of_argv_logs_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = make_runner(Path(tmp) / "runs")
            runner.monitor_password = PASSWORD
            runner.psql_env = {"PGPASSWORD": PASSWORD}
            calls = {}

            def fake_success(cmd, **kwargs):
                calls["cmd"] = cmd
                calls["input"] = kwargs.get("input")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch("run_payment_create.subprocess.run", side_effect=fake_success):
                runner.ensure_monitor_role()

            self.assertIn(MONITOR_ROLE, calls["input"])
            self.assertIn("pg_monitor", calls["input"])
            self.assertNotIn(PASSWORD, shlex.join(str(part) for part in calls["cmd"]))
            self.assertNotIn(PASSWORD, runner.commands_log.read_text(encoding="utf-8"))

            def fake_failure(cmd, **kwargs):
                return subprocess.CompletedProcess(cmd, 1, "", f"error near {PASSWORD}")

            with mock.patch("run_payment_create.subprocess.run", side_effect=fake_failure):
                with self.assertRaises(RunError) as caught:
                    runner.ensure_monitor_role()
            self.assertNotIn(PASSWORD, str(caught.exception))
            self.assertNotIn(PASSWORD, runner.commands_log.read_text(encoding="utf-8"))
            role_log = runner.logs_dir / "monitor-role.log"
            self.assertNotIn(PASSWORD, role_log.read_text(encoding="utf-8"))

            runner.summary = {"monitoring": {"password_in_evidence": False}}
            summary_path = runner.write_summary()
            summary_text = summary_path.read_text(encoding="utf-8")
            self.assertNotIn(PASSWORD, summary_text)
            json.loads(summary_text)


class ArgumentHygieneTest(unittest.TestCase):
    def test_runner_cli_has_no_password_option(self):
        with self.assertRaises(SystemExit):
            parse_args(["--migrate-image", "local/migrate:test",
                        "--password", PASSWORD])
        self.assertNotIn(PASSWORD, " ".join(sys.argv))


if __name__ == "__main__":
    unittest.main()
