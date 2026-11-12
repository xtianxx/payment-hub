"""Focused fake-runner tests for the T077 local image builder."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "build-images.py"
CHECK_IMAGES = REPO / "scripts" / "check-images.py"

CHECK_SPEC = importlib.util.spec_from_file_location(
    "check_images_for_build_test", CHECK_IMAGES
)
check_images = importlib.util.module_from_spec(CHECK_SPEC)
CHECK_SPEC.loader.exec_module(check_images)

BUILD_SPEC = importlib.util.spec_from_file_location(
    "build_images_for_test", SCRIPT
)
build_images = importlib.util.module_from_spec(BUILD_SPEC)
BUILD_SPEC.loader.exec_module(build_images)

HEAD = "1234567890abcdef1234567890abcdef12345678"
SOURCE_DIGEST = "a" * 64
APP_ID = "sha256:" + "b" * 64
MIGRATE_ID = "sha256:" + "c" * 64
APP_REF = build_images.APP_REPO + ":src-" + SOURCE_DIGEST[:12]
MIGRATE_REF = build_images.MIGRATE_REPO + ":src-" + SOURCE_DIGEST[:12]
GO_BUILDER = build_images.GO_BUILDER_IMAGE
DEBIAN = build_images.DEBIAN_IMAGE
PG = build_images.POSTGRES_IMAGE


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def image_json(ref: str, image_id: str, arch: str = "amd64") -> dict:
    return {
        "Id": image_id,
        "Os": "linux",
        "Architecture": arch,
        "RepoDigests": [ref] if "@sha256:" in ref else [],
        "Config": {"Labels": {}},
    }


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


class BuilderTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="build-images-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        write(
            self.repo / "go.mod",
            "module payment-hub\n\ngo 1.27.4\n",
        )
        write(
            self.repo / "go.sum",
            "example.invalid/dependency v1.0.0 h1:fixture\n",
        )
        write(
            self.repo / "deploy/Dockerfile.app",
            "FROM " + GO_BUILDER + "\nFROM " + DEBIAN + "\n",
        )
        write(
            self.repo / "deploy/Dockerfile.migrate",
            "FROM " + GO_BUILDER + "\nFROM " + DEBIAN + "\n",
        )
        write(self.repo / ".dockerignore", "artifacts/\n")
        self.go = self.base / "bin" / "go"
        self.go.parent.mkdir(parents=True)
        self.go.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.go.chmod(0o755)
        self.cache = self.base / "cache"
        self.cache.mkdir()
        self.artifact = self.base / "artifacts" / "build"
        self.clock = lambda: 1000.0
        self.state = {
            "clean": True,
            "fail_build": None,
            "tag_exists": set(),
            "missing_base": set(),
            "base_arch": "amd64",
            "mutate_go_mod": False,
            "prepare_fail": False,
            "validation_fail": False,
            "validation_bad_id": False,
            "stdout_canary": "SECRET_RAW_BUILD_OUTPUT",
        }
        self.source_digest = SOURCE_DIGEST
        self.source_entries = [{"path": "go.mod", "sha256": "d" * 64}]
        self.runner = FakeRunner(self.default_handler)
        self.validation_calls = []
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.base),
            "TMPDIR": str(self.base / "tmp"),
            "LANG": "C.UTF-8",
            "SECRET_CANARY": "SECRET_ENV_CANARY",
        }
        (self.base / "tmp").mkdir()

    def argv(self):
        return [
            "--go",
            str(self.go),
            "--cache-dir",
            str(self.cache),
            "--artifact-root",
            str(self.artifact),
        ]

    def make_builder(self, extra_args=None):
        argv = self.argv()
        if extra_args:
            argv = argv + list(extra_args)
        return build_images.ImageBuilder(
            build_images.parse_args(argv),
            repo=self.repo,
            runner=self.runner,
            delegated_runner=self.delegated_runner,
            docker_path="/tmp/fake-docker",
            clock=self.clock,
            env=self.env,
        )

    def run_builder(self, extra_args=None):
        builder = self.make_builder(extra_args)
        with mock.patch.object(
            build_images.check_images,
            "_compute_source_manifest",
            side_effect=self.manifest_side_effect,
        ):
            code = builder.run()
        return code, builder

    def manifest_side_effect(self, *args, **kwargs):
        return self.source_entries, self.source_digest

    def default_handler(self, argv, cwd, env, timeout, input_text):
        if argv[0] != "/tmp/fake-docker":
            if "git" in Path(argv[0]).name:
                if argv[1:3] == ["rev-parse", "HEAD"]:
                    return check_images.ProcResult(0, HEAD + "\n", "", False, 0.01)
                if argv[1] == "status":
                    status = "" if self.state["clean"] else " M go.mod\n"
                    return check_images.ProcResult(0, status, "", False, 0.01)
            if "check-setup.py" in " ".join(argv):
                return check_images.ProcResult(
                    1 if self.state["prepare_fail"] else 0,
                    "prepare fixture\n",
                    "",
                    False,
                    0.01,
                )
            raise AssertionError("unexpected command: " + " ".join(argv))
        if argv[1:] == ["--version"]:
            return check_images.ProcResult(0, "Docker version 27.0.0\n", "", False, 0.01)
        if argv[1:] == ["context", "show"]:
            return check_images.ProcResult(0, "default\n", "", False, 0.01)
        if argv[1:3] == ["context", "inspect"]:
            return check_images.ProcResult(
                0, '"unix:///var/run/docker.sock"\n', "", False, 0.01
            )
        if argv[1:3] == ["version", "--format"]:
            return check_images.ProcResult(
                0, '{"Os":"linux","Arch":"amd64"}\n', "", False, 0.01
            )
        if argv[1:3] == ["info", "--format"]:
            return check_images.ProcResult(0, '["name=seccomp"]\n', "", False, 0.01)
        if argv[1:3] == ["compose", "version"]:
            return check_images.ProcResult(
                0, "Docker Compose version v2.29.0\n", "", False, 0.01
            )
        if argv[1] == "pull":
            self.state["missing_base"].discard(argv[-1])
            return check_images.ProcResult(0, "", "", False, 0.01)
        if argv[1] == "push":
            raise AssertionError("builder must not push")
        if argv[1] == "build":
            tag = argv[argv.index("-t") + 1]
            self.state["built_" + tag] = True
            return self.build_command(argv)
        if argv[1:3] == ["image", "inspect"]:
            return self.inspect_command(argv)
        raise AssertionError("unexpected docker command: " + " ".join(argv))

    def build_command(self, argv):
        joined = " ".join(argv)
        if "--push" in argv or "--pull" in argv and "--pull=false" not in argv:
            raise AssertionError("invalid build flags")
        if self.state["fail_build"] and self.state["fail_build"] in joined:
            return check_images.ProcResult(1, "", "", False, 0.01)
        if self.state["mutate_go_mod"] and "Dockerfile.app" in joined:
            write(self.repo / "go.mod", "module payment-hub\n\ngo 1.27.4\n// changed\n")
        return check_images.ProcResult(0, self.state["stdout_canary"], "", False, 0.01)

    def inspect_command(self, argv):
        ref = argv[-1]
        if ref in (GO_BUILDER, DEBIAN, PG):
            if ref in self.state["missing_base"]:
                return check_images.ProcResult(1, "", "", False, 0.01)
            return check_images.ProcResult(
                0,
                json.dumps(image_json(ref, "sha256:" + "e" * 64, self.state["base_arch"]))
                + "\n",
                "",
                False,
                0.01,
            )
        if ref in (APP_REF, MIGRATE_REF):
            if ref in self.state["tag_exists"] or self.state.get("built_" + ref):
                image_id = APP_ID if ref == APP_REF else MIGRATE_ID
                value = image_json(ref, image_id)
                value["Config"]["Labels"] = {check_images.SOURCE_LABEL: self.source_digest}
                return check_images.ProcResult(0, json.dumps(value) + "\n", "", False, 0.01)
            return check_images.ProcResult(1, "", "", False, 0.01)
        raise AssertionError("unexpected inspect ref: " + ref)

    def delegated_runner(self, argv, cwd, env):
        if "check-setup.py" in " ".join(argv):
            return self.default_handler(argv, Path(cwd), dict(env), 0, None)
        return self.validation_runner(argv, cwd, env)

    def validation_runner(self, argv, cwd, env):
        self.validation_calls.append({"argv": list(argv), "cwd": str(cwd), "env": dict(env)})
        if self.state["validation_fail"]:
            return check_images.ProcResult(1, "", "", False, 0.01)
        root = Path(argv[argv.index("--artifact-root") + 1])
        record = json.loads(
            Path(env["PAYMENT_HUB_IMAGE_BUILD_RECORD"]).read_text(encoding="utf-8")
        )
        run_id = "a" * 32
        run_dir = root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        app_id = APP_ID if not self.state["validation_bad_id"] else "sha256:" + "9" * 64
        report = {
            "result": "pass",
            "source": {
                "manifest_digest": record["source_manifest_digest"],
                "unchanged": True,
            },
            "pair": {
                "same_source": True,
                "same_platform": True,
                "distinct_ids": True,
                "ids_unchanged_after_run": True,
            },
            "cleanup": {"result": "pass", "errors": []},
            "images": {
                "app": {"id": app_id},
                "migrate": {"id": record["migrate_image_id"]},
            },
        }
        write(run_dir / "images.json", json.dumps(report))
        return check_images.ProcResult(0, "", "", False, 0.01)

    def report(self, builder):
        return json.loads(builder.build_record_path.read_text(encoding="utf-8"))

    def execution(self, builder):
        return json.loads(builder.execution_path.read_text(encoding="utf-8"))


class BuilderContractTests(BuilderTestCase):
    def test_happy_path_builds_once_then_validates_and_uploads(self):
        code, builder = self.run_builder()
        self.assertEqual(code, 0)
        record = self.report(builder)
        self.assertEqual(record["schema"], "payment-hub.image-build-record.v1")
        self.assertEqual(record["source_manifest_digest"], SOURCE_DIGEST)
        self.assertEqual(record["app_image_id"], APP_ID)
        self.assertEqual(record["migrate_image_id"], MIGRATE_ID)
        self.assertEqual(record["platform"], "linux/amd64")
        self.assertEqual(record["source_sha"], HEAD)
        self.assertFalse(record["source_dirty"])
        self.assertIs(record["build_once_per_image"], True)
        execution = self.execution(builder)
        self.assertEqual(execution["result"], "pass")
        self.assertTrue(execution["source_stable"])
        self.assertEqual(len(execution["builds"]), 2)
        self.assertEqual(len(self.validation_calls), 1)
        self.assertEqual(
            self.validation_calls[0]["env"]["PAYMENT_HUB_APP_IMAGE"], APP_REF
        )
        self.assertEqual(
            self.validation_calls[0]["env"]["PAYMENT_HUB_MIGRATE_IMAGE"], MIGRATE_REF
        )
        self.assertEqual(
            self.validation_calls[0]["env"]["PAYMENT_HUB_IMAGE_BUILD_RECORD"],
            str(builder.build_record_path),
        )
        for name in ("build-summary.json", "images-summary.json"):
            path = builder.upload_dir / name
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        text = (
            (builder.upload_dir / "build-summary.json").read_text()
            + (builder.upload_dir / "images-summary.json").read_text()
        )
        for secret in ("SECRET_ENV_CANARY", self.state["stdout_canary"], "postgres://"):
            self.assertNotIn(secret, text)
        build_calls = [
            call for call in self.runner.calls if call["argv"][1] == "build"
        ]
        self.assertEqual(len(build_calls), 2)
        for call in build_calls:
            argv = call["argv"]
            self.assertIn("--platform", argv)
            self.assertIn("linux/amd64", argv)
            self.assertIn("--pull=false", argv)
            self.assertIn("--network", argv)
            self.assertIn("default", argv)
            self.assertIn(check_images.SOURCE_LABEL + "=" + SOURCE_DIGEST, argv)
            self.assertIn("-f", argv)
            self.assertIn(".", argv)
            self.assertNotIn("--push", argv)

    def test_default_does_not_pull_or_prepare(self):
        self.state["missing_base"].add(GO_BUILDER)
        code, builder = self.run_builder()
        self.assertNotEqual(code, 0)
        self.assertFalse(
            any(call["argv"][1] == "pull" for call in self.runner.calls)
        )
        self.assertFalse(
            any("check-setup.py" in " ".join(call["argv"]) for call in self.runner.calls)
        )
        self.assertEqual(self.execution(builder)["failure"]["kind"], "base-image")

    def test_prepare_inputs_pulls_fixed_bases_and_runs_check_setup(self):
        self.state["missing_base"].update({GO_BUILDER, DEBIAN, PG})
        code, builder = self.run_builder(extra_args=["--prepare-inputs"])
        self.assertEqual(code, 0)
        pulls = [
            call["argv"] for call in self.runner.calls if call["argv"][1] == "pull"
        ]
        self.assertEqual(
            pulls,
            [
                ["/tmp/fake-docker", "pull", "--platform", "linux/amd64", ref]
                for ref in (GO_BUILDER, DEBIAN, PG)
            ],
        )
        setup = [
            call["argv"]
            for call in builder.delegated_calls
            if "check-setup.py" in " ".join(call["argv"])
        ]
        self.assertEqual(len(setup), 1)
        self.assertIn("--download-modules", setup[0])
        self.assertIn("--go", setup[0])
        self.assertIn("--cache-dir", setup[0])
        self.assertTrue(self.execution(builder)["prepare_selected"])

    def test_missing_base_without_prepare_fails_without_build(self):
        self.state["missing_base"].add(PG)
        code, builder = self.run_builder()
        self.assertNotEqual(code, 0)
        self.assertFalse(any(call["argv"][1] == "build" for call in self.runner.calls))
        self.assertEqual(self.execution(builder)["failure"]["kind"], "base-image")

    def test_wrong_base_platform_fails_without_build(self):
        self.state["base_arch"] = "arm64"
        code, builder = self.run_builder()
        self.assertNotEqual(code, 0)
        self.assertFalse(any(call["argv"][1] == "build" for call in self.runner.calls))
        self.assertEqual(self.execution(builder)["failure"]["kind"], "base-platform")

    def test_source_change_after_app_stops_before_migrate(self):
        digests = [SOURCE_DIGEST, "f" * 64, "f" * 64]
        with mock.patch.object(
            build_images.check_images,
            "_compute_source_manifest",
            side_effect=[(self.source_entries, value) for value in digests],
        ):
            builder = self.make_builder()
            code = builder.run()
        self.assertNotEqual(code, 0)
        build_calls = [
            call for call in self.runner.calls if call["argv"][1] == "build"
        ]
        self.assertEqual(len(build_calls), 1)
        self.assertEqual(self.execution(builder)["failure"]["kind"], "source-changed")

    def test_build_failure_stops_without_second_build(self):
        self.state["fail_build"] = "Dockerfile.app"
        code, builder = self.run_builder()
        self.assertNotEqual(code, 0)
        build_calls = [
            call for call in self.runner.calls if call["argv"][1] == "build"
        ]
        self.assertEqual(len(build_calls), 1)
        self.assertEqual(self.execution(builder)["failure"]["kind"], "build")

    def test_existing_tag_fails_without_rebuild(self):
        self.state["tag_exists"].add(APP_REF)
        code, builder = self.run_builder()
        self.assertNotEqual(code, 0)
        self.assertFalse(any(call["argv"][1] == "build" for call in self.runner.calls))
        self.assertEqual(self.execution(builder)["failure"]["kind"], "tag-exists")

    def test_validation_failure_keeps_build_record_and_writes_no_upload(self):
        self.state["validation_fail"] = True
        code, builder = self.run_builder()
        self.assertNotEqual(code, 0)
        self.assertTrue(builder.build_record_path.is_file())
        self.assertFalse(builder.upload_dir.exists())
        self.assertEqual(self.execution(builder)["failure"]["kind"], "validation")

    def test_validation_id_mismatch_fails(self):
        self.state["validation_bad_id"] = True
        code, builder = self.run_builder()
        self.assertNotEqual(code, 0)
        self.assertEqual(self.execution(builder)["failure"]["kind"], "validation-mismatch")

    def test_dirty_tree_has_no_source_sha_and_no_revision_label(self):
        self.state["clean"] = False
        code, builder = self.run_builder()
        self.assertEqual(code, 0)
        record = self.report(builder)
        self.assertTrue(record["source_dirty"])
        self.assertIsNone(record["source_sha"])
        build_calls = [
            call for call in self.runner.calls if call["argv"][1] == "build"
        ]
        for call in build_calls:
            self.assertFalse(
                any("org.opencontainers.image.revision" in item for item in call["argv"])
            )

    def test_clean_tree_records_source_sha_and_revision_label(self):
        code, builder = self.run_builder()
        self.assertEqual(code, 0)
        record = self.report(builder)
        self.assertEqual(record["source_sha"], HEAD)
        self.assertFalse(record["source_dirty"])
        build_calls = [
            call for call in self.runner.calls if call["argv"][1] == "build"
        ]
        for call in build_calls:
            self.assertIn(
                "org.opencontainers.image.revision=" + HEAD, call["argv"]
            )

    def test_go_mod_change_fails_source_stability(self):
        self.state["mutate_go_mod"] = True
        code, builder = self.run_builder()
        self.assertNotEqual(code, 0)
        self.assertEqual(self.execution(builder)["failure"]["kind"], "source-changed")

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

    def _delegated_signal_script(self):
        ready = self.artifact / "delegated-ready"
        pid_file = self.artifact / "delegated-pid"
        script = self.base / "delegated_child.py"
        write(
            script,
            "import os, signal, sys, time\n"
            "from pathlib import Path\n"
            "args = sys.argv\n"
            "root = Path(args[args.index('--artifact-root') + 1]).parent\n"
            "ready = root / 'delegated-ready'\n"
            "pid_file = root / 'delegated-pid'\n"
            "open(pid_file, 'w').write(str(os.getpid()))\n"
            "open(ready, 'w').write('ready')\n"
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
            "time.sleep(1.0)\n",
        )
        return ready, pid_file, script

    def test_delegated_sigterm_exit_zero_fails_and_reaps(self):
        ready, pid_file, script = self._delegated_signal_script()
        builder = self.make_builder()
        builder.delegated_runner = None
        seen = {"signal": False}

        def hold(signum, _frame):
            seen["signal"] = True

        old = signal.signal(signal.SIGTERM, hold)

        def send():
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            os.kill(os.getpid(), signal.SIGTERM)

        try:
            thread = threading.Thread(target=send, daemon=True)
            thread.start()
            with mock.patch.object(
                build_images, "CHECK_IMAGES_PATH", script
            ), mock.patch.object(
                build_images.check_images,
                "_compute_source_manifest",
                side_effect=self.manifest_side_effect,
            ):
                code = builder.run()
            thread.join(timeout=1)
            self.assertNotEqual(code, 0)
            self.assertFalse(builder.upload_dir.exists())
            execution = self.execution(builder)
            self.assertEqual(execution["failure"]["stage"], "interrupted")
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            self.assertTrue(self._wait_pid_gone(child_pid))
        finally:
            signal.signal(signal.SIGTERM, old)

    def test_direct_build_sigterm_exit_zero_stops_before_second_image(self):
        ready = self.base / "build-ready"
        pid_file = self.base / "build-pid"
        script = self.base / "build_child.py"
        write(
            script,
            "import os, signal, sys, time\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "open(sys.argv[2], 'w').write('ready')\n"
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
            "time.sleep(1.0)\n",
        )
        build_calls = {"count": 0}

        def runner(argv, *, cwd, env, timeout, input_text=None, reap_grace=0.0):
            if len(argv) > 1 and argv[1] == "build":
                build_calls["count"] += 1
                return check_images.run_process(
                    [sys.executable, "-B", str(script), str(pid_file), str(ready)],
                    cwd=Path(cwd),
                    env=dict(env),
                    timeout=timeout,
                    input_text=None,
                    reap_grace=reap_grace,
                )
            return self.default_handler(argv, cwd, env, timeout, input_text)

        builder = self.make_builder()
        builder.runner = runner
        builder.delegated_runner = self.delegated_runner
        seen = {"signal": False}

        def hold(signum, _frame):
            seen["signal"] = True

        old = signal.signal(signal.SIGTERM, hold)

        def send():
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            os.kill(os.getpid(), signal.SIGTERM)

        try:
            thread = threading.Thread(target=send, daemon=True)
            thread.start()
            with mock.patch.object(
                build_images.check_images,
                "_compute_source_manifest",
                side_effect=self.manifest_side_effect,
            ):
                code = builder.run()
            thread.join(timeout=1)
            self.assertNotEqual(code, 0)
            self.assertEqual(build_calls["count"], 1)
            self.assertFalse(builder.upload_dir.exists())
            self.assertEqual(self.execution(builder)["failure"]["stage"], "interrupted")
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            self.assertTrue(self._wait_pid_gone(child_pid))
        finally:
            signal.signal(signal.SIGTERM, old)

    def test_source_manifest_write_failure_no_success_upload(self):
        builder = self.make_builder()
        with mock.patch.object(
            builder,
            "_write_source_manifest",
            side_effect=build_images.LauncherError("artifact", "write"),
        ), mock.patch.object(
            build_images.check_images,
            "_compute_source_manifest",
            side_effect=self.manifest_side_effect,
        ):
            code = builder.run()
        self.assertNotEqual(code, 0)
        self.assertFalse(builder.upload_dir.exists())

    def test_execution_write_failure_no_success_upload(self):
        builder = self.make_builder()
        with mock.patch.object(
            builder,
            "_write_execution",
            side_effect=build_images.LauncherError("artifact", "write"),
        ), mock.patch.object(
            build_images.check_images,
            "_compute_source_manifest",
            side_effect=self.manifest_side_effect,
        ):
            code = builder.run()
        self.assertNotEqual(code, 0)
        self.assertFalse(builder.upload_dir.exists())

    def test_second_summary_write_failure_no_upload(self):
        builder = self.make_builder()
        original = build_images._write_json_once

        def write_json(path, value):
            if path.name == "build-summary.json":
                raise build_images.LauncherError("artifact", "write")
            return original(path, value)

        with mock.patch.object(
            build_images, "_write_json_once", side_effect=write_json
        ), mock.patch.object(
            build_images.check_images,
            "_compute_source_manifest",
            side_effect=self.manifest_side_effect,
        ):
            code = builder.run()
        self.assertNotEqual(code, 0)
        self.assertFalse(builder.upload_dir.exists())

    def test_missing_args_is_nonzero(self):
        with self.assertRaises(SystemExit) as raised:
            build_images.main(["--go", str(self.go)])
        self.assertNotEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
