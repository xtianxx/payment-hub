"""Focused fake-only tests for the one-shot migrate package initialization.

No Docker daemon, PostgreSQL, registry, GitHub API, or external network is
used.  Every command and HTTP exchange is injected.  The existing release
fixtures are reused without changing their normal-publish semantics; the
subclasses here only recognize the fixed ``init-<sha>`` tag, the absent-to-
created migrate package transition, and the single-migrate push.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import test_release as tr  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "init_migrate_package", SCRIPTS / "init-migrate-package.py"
)
init = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = init
SPEC.loader.exec_module(init)

release = tr.release
SOURCE_SHA = tr.SOURCE_SHA
OTHER_SHA = "c" * 40
GITHUB_TOKEN = tr.GITHUB_TOKEN
AUDIT_TOKEN = tr.AUDIT_TOKEN
MIGRATE_INITIALIZATION_ID = 201
BUNDLE_ARTIFACT_ID = 500
BUNDLE_ARTIFACT_DIGEST = "sha256:" + "d" * 64


class InitFakeHTTP(tr.FakeHTTP):
    def __init__(
        self,
        pair: tr.Pair,
        *,
        visibility: str = "private",
        migrate_ids: tuple[int, ...] = (MIGRATE_INITIALIZATION_ID,),
        migrate_owner: Any = None,
        migrate_repository: Any = None,
    ) -> None:
        super().__init__(pair)
        self.packages = {
            "app": tr.package_metadata("app"),
            "migrate": None,
        }
        self.migrate_created = False
        self.migrate_visibility = visibility
        self.migrate_ids = list(migrate_ids)
        self.migrate_owner = migrate_owner
        self.migrate_repository = migrate_repository
        self.is_pushed = lambda role: role == "migrate" and self.migrate_created

    def _current_package(self, role: str) -> dict[str, Any] | None:
        if role == "migrate" and self.migrate_created:
            index = max(0, self.package_counts["migrate"] - 1)
            index = min(index, len(self.migrate_ids) - 1)
            return tr.package_metadata(
                "migrate",
                package_id=self.migrate_ids[index],
                visibility=self.migrate_visibility,
                owner=self.migrate_owner,
                repository=self.migrate_repository,
            )
        return self.packages.get(role)


class InitFakeRunner(tr.FakeRunner):
    def __init__(
        self,
        pair: tr.Pair,
        *,
        http: InitFakeHTTP | None = None,
        git_sha: str = SOURCE_SHA,
    ) -> None:
        super().__init__(pair)
        self.http = http
        self.git_sha = git_sha
        self.tag_hook = None
        self.push_hook = None
        self.pull_hook = None
        self.push_digest_override: str | None = None
        self.push_fail = False
        self.tag_fail = False
        self.pull_fail = False
        self.revalidation_cleanup_failed = False
        self.logout_cleanup_failed = False

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float | None,
        input_text: str | None = None,
        reap_grace: float = 0.0,
    ) -> release.ProcResult:
        argv = list(argv)
        if "git" in Path(argv[0]).name:
            self.calls.append(
                {
                    "argv": argv,
                    "cwd": str(cwd),
                    "env": dict(env),
                    "timeout": timeout,
                    "input_text": input_text,
                }
            )
            if "rev-parse" in argv:
                return self._result(0, self.git_sha + "\n")
            if "status" in argv:
                return self._result(0, "")
            return self._result(0)
        if "check.py" in " ".join(argv) and self.revalidation_cleanup_failed:
            self.calls.append(
                {
                    "argv": argv,
                    "cwd": str(cwd),
                    "env": dict(env),
                    "timeout": timeout,
                    "input_text": input_text,
                }
            )
            return release.ProcResult(124, "", "", True, 0.0, True)
        result = super().__call__(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            input_text=input_text,
            reap_grace=reap_grace,
        )
        if len(argv) >= 2 and Path(argv[0]).name.endswith("docker"):
            if argv[1] == "tag" and self.tag_hook is not None:
                self.tag_hook()
            if argv[1] == "push":
                if self.push_hook is not None:
                    self.push_hook()
                if (
                    not self.push_fail
                    and self.push_digest_override is None
                    and self.http is not None
                ):
                    self.http.migrate_created = True
            if (
                argv[1] == "pull"
                and self.pull_hook is not None
                and "payment-hub-migrate" in argv[-1]
            ):
                self.pull_hook()
        return result

    def _docker(
        self, argv: list[str], env: dict[str, str], input_text: str | None
    ) -> release.ProcResult:
        if (
            len(argv) >= 2
            and argv[1] == "logout"
            and self.logout_cleanup_failed
        ):
            return release.ProcResult(0, "", "", False, 0.0, True)
        if len(argv) >= 2 and argv[1] == "push":
            self.push_calls.append(argv[-1])
            if self.push_fail:
                return self._result(1, "", "push failed")
            digest = self.push_digest_override or self.pair.migrate_id
            return self._result(0, f"digest: {digest}\n")
        if len(argv) >= 2 and argv[1] == "tag" and self.tag_fail:
            return self._result(1)
        if len(argv) >= 2 and argv[1] == "pull" and self.pull_fail:
            return self._result(1)
        return super()._docker(argv, env, input_text)


class InitTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="init-migrate-tests-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.pair = tr.make_pair(self.base / "source-pair.tar")
        self.bundle, self.descriptor = tr.make_bundle(
            self.base / "bundle", self.pair
        )
        self.descriptor_sha = tr.sha256(
            (self.bundle / release.DESCRIPTOR_FILE).read_bytes()
        )
        self.env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": SOURCE_SHA,
            "GITHUB_REPOSITORY": release.REPOSITORY,
            "GITHUB_ACTOR": "xtianxx",
            "GITHUB_TRIGGERING_ACTOR": "xtianxx",
            "GITHUB_WORKFLOW_REF": init.WORKFLOW_REF,
            "PAYMENT_HUB_RELEASE_OPERATION": init.OPERATION,
            "GITHUB_TOKEN": GITHUB_TOKEN,
            "GHCR_PACKAGE_AUDIT_TOKEN": AUDIT_TOKEN,
            "PATH": os.environ.get("PATH", ""),
        }
        self.http = InitFakeHTTP(self.pair)
        self.fake = InitFakeRunner(self.pair, http=self.http)
        self.selection = self.selection_value()
        self.selection_calls = 0
        self.selection_outcomes: list[Any] = []

        def fake_select(source_sha: str, api: Any) -> Any:
            self.selection_calls += 1
            if self.selection_outcomes:
                outcome = self.selection_outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome
            return self.selection

        selection_patcher = mock.patch.object(
            init.selection_module, "select_candidate", new=fake_select
        )
        selection_patcher.start()
        self.addCleanup(selection_patcher.stop)
        api_patcher = mock.patch.object(
            init.selection_module.CI, "GitHubApi", lambda token: object()
        )
        api_patcher.start()
        self.addCleanup(api_patcher.stop)
        self.root = self.base / "init-root"

    def selection_value(
        self,
        *,
        source_sha: str = SOURCE_SHA,
        run_id: int = 1001,
        attempt: int = 1,
        artifact_id: int = BUNDLE_ARTIFACT_ID,
        artifact_digest: str = BUNDLE_ARTIFACT_DIGEST,
    ) -> Any:
        return init.selection_module.Selection(
            source_sha=source_sha,
            ci_run_id=run_id,
            ci_run_attempt=attempt,
            pointer_artifact_id=400,
            pointer_artifact_digest="sha256:" + "e" * 64,
            bundle_artifact_id=artifact_id,
            bundle_artifact_digest=artifact_digest,
        )

    def make_args(
        self,
        *,
        approve: bool = True,
        acknowledge: bool = True,
        preconditions: bool = True,
        prepare: bool = True,
        source_sha: str = SOURCE_SHA,
        run_id: str = "1001",
        attempt: str = "1",
        artifact_id: str = str(BUNDLE_ARTIFACT_ID),
        artifact_digest: str = BUNDLE_ARTIFACT_DIGEST,
        bundle: Path | None = None,
        descriptor_sha: str | None = None,
        root: Path | None = None,
    ):
        values = [
            "--source-sha",
            source_sha,
            "--ci-run-id",
            run_id,
            "--ci-run-attempt",
            attempt,
            "--bundle-artifact-id",
            artifact_id,
            "--bundle-artifact-digest",
            artifact_digest,
            "--bundle-dir",
            str(bundle or self.bundle),
            "--expected-descriptor-sha256",
            descriptor_sha or self.descriptor_sha,
            "--artifact-root",
            str(root or self.root),
            "--go",
            "/usr/bin/true",
            "--cache-dir",
            str(self.base / "cache"),
        ]
        if prepare:
            values.append("--prepare-inputs")
        if approve:
            values.append("--approve-initialization")
        if acknowledge:
            values.append("--acknowledge-public-creation")
        if preconditions:
            values.append("--initialization-preconditions-verified")
        return init._build_parser().parse_args(values)

    def run_init(
        self,
        *,
        args=None,
        env: dict[str, str] | None = None,
        fake: InitFakeRunner | None = None,
        http: InitFakeHTTP | None = None,
    ) -> int:
        # Each subtest starts from a fresh artifact root, mirroring the real
        # workflow's one-shot invocation.  This is test-harness hygiene only.
        if self.root.exists():
            shutil.rmtree(self.root)
        fake = fake or self.fake
        instance = init.MigrateInitializer(
            repo=self.repo,
            runner=fake,
            delegated_runner=fake,
            http=http or self.http,
            env=dict(self.env) if env is None else dict(env),
            git_path="/fake/git",
            docker_path="/fake/docker",
        )
        return instance.initialize(args or self.make_args())

    def summary(self) -> dict[str, Any]:
        path = self.root / init.SUMMARY_FILE
        self.assertTrue(path.exists(), "initialization summary must exist")
        return json.loads(path.read_text(encoding="utf-8"))

    def docker_calls(
        self, runner: InitFakeRunner | None = None
    ) -> list[dict[str, Any]]:
        runner = runner or self.fake
        return [
            call
            for call in runner.calls
            if Path(call["argv"][0]).name.endswith("docker")
        ]

    def assert_only_migrate_push(self) -> None:
        self.assertEqual(len(self.fake.push_calls), 1)
        self.assertEqual(
            self.fake.push_calls[0],
            init.TARGET + ":" + init.INIT_TAG_PREFIX + SOURCE_SHA,
        )

    def assert_no_app_push(self) -> None:
        self.assertFalse(
            any("payment-hub-app" in reference for reference in self.fake.push_calls)
        )

    def assert_no_builder_or_source_gates(self) -> None:
        text = "\n".join(" ".join(call["argv"]) for call in self.fake.calls)
        self.assertNotIn("build-images.py", text)
        for scope in ("format", "static", "unit", "db", "all"):
            self.assertNotIn("--scope " + scope, text)
        image_calls = [
            call
            for call in self.fake.calls
            if "check.py" in " ".join(call["argv"])
        ]
        for call in image_calls:
            self.assertEqual(tr.arg(call["argv"], "--scope"), "image")

    def assert_no_visibility_or_delete_api(self) -> None:
        for call in self.http.calls:
            self.assertEqual(call["method"], "GET")
            self.assertNotIn("/visibility", call["url"])
            self.assertNotIn("/delete", call["url"])

    def assert_tokens_not_leaked(self) -> None:
        for call in self.fake.calls:
            self.assertNotIn("GITHUB_TOKEN", call["env"])
            self.assertNotIn(release.AUDIT_TOKEN_ENV, call["env"])
            joined = " ".join(call["argv"])
            self.assertNotIn(GITHUB_TOKEN, joined)
            self.assertNotIn(AUDIT_TOKEN, joined)
            if call["input_text"] is not None:
                self.assertEqual(call["input_text"], GITHUB_TOKEN)
        summary_text = (self.root / init.SUMMARY_FILE).read_text(
            encoding="utf-8"
        )
        for token in (GITHUB_TOKEN, AUDIT_TOKEN):
            self.assertNotIn(token, summary_text)
        for call in self.http.calls:
            url = call["url"]
            joined = url + " " + " ".join(call["headers"].values())
            if "/v2/" in url or url.startswith(release.TOKEN_REALM):
                self.assertNotIn(AUDIT_TOKEN, joined)
            else:
                self.assertNotIn(GITHUB_TOKEN, joined)

    def assert_failure(self, stage: str, kind: str) -> dict[str, Any]:
        summary = self.summary()
        self.assertEqual(summary["outcome"], "fail")
        self.assertIs(summary["initialization_verified"], False)
        self.assertIs(summary["published"], False)
        self.assertEqual(summary["failure"], {"stage": stage, "kind": kind})
        self.assertEqual(
            summary["bundle_artifact"],
            {
                "id": BUNDLE_ARTIFACT_ID,
                "digest": BUNDLE_ARTIFACT_DIGEST,
            },
        )
        self.assert_no_app_push()
        self.assert_no_builder_or_source_gates()
        self.assert_no_visibility_or_delete_api()
        self.assert_tokens_not_leaked()
        return summary


class SuccessTests(InitTestBase):
    def test_private_success_verifies_initialization_and_never_publishes(self):
        self.http.migrate_visibility = "private"
        self.assertEqual(self.run_init(), 0)
        summary = self.summary()
        self.assertEqual(summary["schema"], init.SUMMARY_SCHEMA)
        self.assertEqual(summary["operation"], init.OPERATION)
        self.assertEqual(summary["outcome"], "pass")
        self.assertIs(summary["initialization_verified"], True)
        self.assertIs(summary["published"], False)
        self.assertEqual(summary["source_sha"], SOURCE_SHA)
        self.assertEqual(summary["ci"], {"run_id": 1001, "run_attempt": 1})
        self.assertEqual(
            summary["bundle_artifact"],
            {
                "id": BUNDLE_ARTIFACT_ID,
                "digest": BUNDLE_ARTIFACT_DIGEST,
            },
        )
        self.assertEqual(summary["descriptor_sha256"], self.descriptor_sha)
        self.assertEqual(summary["target"], init.TARGET)
        self.assertEqual(
            summary["initialization_tag"],
            init.INIT_TAG_PREFIX + SOURCE_SHA,
        )
        self.assertEqual(
            summary["app"],
            {
                "package_id": tr.APP_PACKAGE_ID,
                "initial_status": "EXISTING_VALID",
            },
        )
        self.assertEqual(
            summary["migrate"]["initial_status"], "ABSENT_CONFIRMED"
        )
        self.assertEqual(
            summary["migrate"]["package_id"], MIGRATE_INITIALIZATION_ID
        )
        self.assertEqual(summary["migrate"]["visibility"], "private")
        self.assertIs(summary["requires_owner_visibility_action"], True)
        self.assertEqual(
            summary["last_observed"],
            {
                "app_phase": "post-acceptance",
                "app_package_id": tr.APP_PACKAGE_ID,
                "migrate_phase": "post-acceptance",
                "migrate_package_id": MIGRATE_INITIALIZATION_ID,
                "migrate_visibility": "private",
                "migrate_visibility_valid": True,
            },
        )
        self.assertIs(summary["push"]["attempted"], True)
        self.assertEqual(summary["push"]["side_effect"], "verified")
        self.assertEqual(
            summary["push"]["candidate_digest"], self.pair.migrate_id
        )
        self.assertEqual(summary["remote"]["digest"], self.pair.migrate_id)
        self.assertEqual(summary["pull"]["id"], self.pair.migrate_id)
        self.assertEqual(summary["pull"]["digest"], self.pair.migrate_id)
        self.assertEqual(summary["revalidation"]["result"], "pass")
        self.assertEqual(
            summary["revalidation"]["method"], "image-pair-runtime"
        )
        self.assertEqual(
            summary["credential_cleanup"],
            {
                "tokens_removed_from_child_env": True,
                "writer_logged_out": True,
                "docker_config_removed": True,
                "cleanup_failed": False,
            },
        )
        self.assertIsNone(summary["failure"])
        self.assertIs(summary["process_cleanup_failed"], False)
        self.assertEqual(
            stat.S_IMODE(
                (self.root / init.SUMMARY_FILE).stat().st_mode
            ),
            0o600,
        )
        self.assert_only_migrate_push()
        self.assert_no_app_push()
        self.assert_no_builder_or_source_gates()
        self.assert_no_visibility_or_delete_api()
        self.assert_tokens_not_leaked()
        self.assertEqual(self.selection_calls, 2)
        self.assertEqual(self.fake.builder_calls, 0)
        self.assertEqual(self.fake.save_calls, 0)
        self.assertEqual(self.fake.check_image_calls, 1)
        self.assertEqual(
            self.fake.pull_calls[-1],
            init.TARGET + "@" + self.pair.migrate_id,
        )
        record_path = self.root / init.VALIDATION_RECORD_FILE
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["schema"], release.BUILD_RECORD_SCHEMA)
        self.assertEqual(
            record["initialization_origin"]["app_source"],
            "bundle-local-image",
        )
        self.assertEqual(
            record["initialization_origin"]["migrate_source"],
            "pulled-registry-digest",
        )
        self.assertEqual(record["app_image"], tr.APP_REF)
        self.assertEqual(
            record["migrate_image"],
            init.TARGET + "@" + self.pair.migrate_id,
        )
        parsed = release.check_images._read_build_record(record_path)
        self.assertEqual(
            parsed["initialization_origin"]["descriptor_sha256"],
            self.descriptor_sha,
        )

    def test_public_success_sets_no_owner_visibility_action(self):
        self.http.migrate_visibility = "public"
        self.assertEqual(self.run_init(), 0)
        summary = self.summary()
        self.assertIs(summary["initialization_verified"], True)
        self.assertIs(summary["published"], False)
        self.assertIs(summary["process_cleanup_failed"], False)
        self.assertEqual(summary["migrate"]["visibility"], "public")
        self.assertIs(summary["requires_owner_visibility_action"], False)
        self.assertEqual(
            summary["last_observed"],
            {
                "app_phase": "post-acceptance",
                "app_package_id": tr.APP_PACKAGE_ID,
                "migrate_phase": "post-acceptance",
                "migrate_package_id": MIGRATE_INITIALIZATION_ID,
                "migrate_visibility": "public",
                "migrate_visibility_valid": True,
            },
        )
        self.assert_only_migrate_push()
        self.assert_no_app_push()
        self.assert_no_visibility_or_delete_api()

    def test_acceptance_failure_is_not_verified(self):
        self.fake.revalidation_fail = True
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("revalidation", "check-images"),
        )
        summary = self.assert_failure("revalidation", "check-images")
        self.assertIsNone(summary["revalidation"])
        self.assert_only_migrate_push()
        self.assert_no_app_push()


class PostAcceptanceMetadataTests(InitTestBase):
    def run_with_acceptance_mutation(self, mutation):
        original = init.MigrateInitializer._run_image_acceptance

        def wrapped(instance, *args, **kwargs):
            result = original(instance, *args, **kwargs)
            mutation()
            return result

        with mock.patch.object(
            init.MigrateInitializer,
            "_run_image_acceptance",
            new=wrapped,
        ):
            with self.assertRaises(init.ReleaseError) as caught:
                self.run_init()
        return caught

    def test_app_id_changed_after_acceptance_fails_closed(self):
        caught = self.run_with_acceptance_mutation(
            lambda: self.http.packages.__setitem__(
                "app", tr.package_metadata("app", package_id=999)
            )
        )
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "app-changed"),
        )
        summary = self.assert_failure("package", "app-changed")
        self.assertEqual(summary["app"]["package_id"], tr.APP_PACKAGE_ID)
        self.assertEqual(
            summary["last_observed"]["app_phase"], "post-acceptance"
        )
        self.assertEqual(summary["last_observed"]["app_package_id"], 999)
        self.assertEqual(
            summary["last_observed"]["migrate_phase"], "post-push"
        )
        self.assertEqual(summary["push"]["side_effect"], "verified")
        self.assertEqual(summary["revalidation"]["result"], "pass")
        self.assertIs(summary["initialization_verified"], False)
        self.assert_only_migrate_push()
        self.assert_no_app_push()

    def test_migrate_id_changed_after_acceptance_fails_closed(self):
        caught = self.run_with_acceptance_mutation(
            lambda: setattr(
                self.http,
                "migrate_ids",
                (
                    MIGRATE_INITIALIZATION_ID,
                    MIGRATE_INITIALIZATION_ID,
                    202,
                ),
            )
        )
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "migrate-changed"),
        )
        summary = self.assert_failure("package", "migrate-changed")
        self.assertEqual(
            summary["migrate"]["package_id"], MIGRATE_INITIALIZATION_ID
        )
        self.assertEqual(
            summary["last_observed"]["migrate_package_id"], 202
        )
        self.assertEqual(
            summary["last_observed"]["migrate_phase"], "post-acceptance"
        )
        self.assertEqual(
            summary["last_observed"]["migrate_visibility"], "private"
        )
        self.assertIs(
            summary["last_observed"]["migrate_visibility_valid"], True
        )
        self.assertEqual(summary["push"]["side_effect"], "verified")
        self.assertEqual(summary["revalidation"]["result"], "pass")
        self.assertIs(summary["initialization_verified"], False)

    def test_visibility_changed_after_acceptance_fails_closed(self):
        caught = self.run_with_acceptance_mutation(
            lambda: setattr(self.http, "migrate_visibility", "public")
        )
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "migrate-changed"),
        )
        summary = self.assert_failure("package", "migrate-changed")
        self.assertEqual(summary["migrate"]["visibility"], "private")
        self.assertEqual(
            summary["last_observed"]["migrate_visibility"], "public"
        )
        self.assertEqual(
            summary["last_observed"]["migrate_phase"], "post-acceptance"
        )
        self.assertIs(
            summary["last_observed"]["migrate_visibility_valid"], True
        )
        self.assertEqual(summary["push"]["side_effect"], "verified")
        self.assertIs(summary["initialization_verified"], False)

    def test_internal_visibility_after_acceptance_is_reported_as_other(self):
        caught = self.run_with_acceptance_mutation(
            lambda: setattr(self.http, "migrate_visibility", "internal")
        )
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "migrate-visibility"),
        )
        summary = self.assert_failure("package", "migrate-visibility")
        self.assertEqual(summary["migrate"]["visibility"], "private")
        self.assertEqual(
            summary["last_observed"]["migrate_visibility"], "other"
        )
        self.assertEqual(
            summary["last_observed"]["migrate_phase"], "post-acceptance"
        )
        self.assertIs(
            summary["last_observed"]["migrate_visibility_valid"], False
        )
        self.assertEqual(summary["push"]["side_effect"], "verified")
        self.assertIs(summary["initialization_verified"], False)


class CleanupClassificationTests(InitTestBase):
    def test_acceptance_process_cleanup_failure_is_not_hidden(self):
        self.fake.revalidation_cleanup_failed = True
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("revalidation", "cleanup-failed"),
        )
        summary = self.assert_failure("revalidation", "cleanup-failed")
        self.assertIsNone(summary["revalidation"])
        self.assertEqual(summary["push"]["side_effect"], "verified")
        self.assertIs(summary["process_cleanup_failed"], True)
        self.assertIs(
            summary["credential_cleanup"]["cleanup_failed"], False
        )

    def test_logout_process_cleanup_failure_is_not_hidden(self):
        self.fake.logout_cleanup_failed = True
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("tool", "cleanup-failed"),
        )
        summary = self.assert_failure("tool", "cleanup-failed")
        self.assertIs(summary["process_cleanup_failed"], True)
        self.assertIs(
            summary["credential_cleanup"]["writer_logged_out"], False
        )
        self.assertIs(
            summary["credential_cleanup"]["docker_config_removed"], True
        )
        self.assertEqual(summary["push"]["side_effect"], "verified")

    def test_credential_cleanup_failure_preserves_prior_stage(self):
        def fail_logout(instance, *args, **kwargs):
            raise init.ReleaseError("auth", "logout")

        def fail_remove(instance, auth_dir, state):
            state["credential_cleanup"]["cleanup_failed"] = True
            state["credential_cleanup"]["docker_config_removed"] = False

        with (
            mock.patch.object(
                init.MigrateInitializer, "_logout", new=fail_logout
            ),
            mock.patch.object(
                init.MigrateInitializer, "_remove_auth_dir", new=fail_remove
            ),
        ):
            with self.assertRaises(init.ReleaseError) as caught:
                self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("auth", "logout"),
        )
        summary = self.assert_failure("auth", "logout")
        self.assertIs(
            summary["credential_cleanup"]["cleanup_failed"], True
        )
        self.assertIs(summary["process_cleanup_failed"], False)

    def test_cancelled_cleanup_failure_classification_is_preserved(self):
        def fail(instance, *args, **kwargs):
            instance._cancel_cleanup_failed = True
            raise init.ReleaseError("cancelled", "cleanup-failed")

        with mock.patch.object(
            init.MigrateInitializer, "_run_image_acceptance", new=fail
        ):
            with self.assertRaises(init.ReleaseError) as caught:
                self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cancelled", "cleanup-failed"),
        )
        summary = self.assert_failure("cancelled", "cleanup-failed")
        self.assertEqual(summary["push"]["side_effect"], "verified")
        self.assertIs(summary["process_cleanup_failed"], True)
        self.assertIs(summary["initialization_verified"], False)


class ContextAndCliTests(InitTestBase):
    def test_wrong_operation_is_rejected_before_api_or_docker(self):
        env = dict(self.env)
        env["PAYMENT_HUB_RELEASE_OPERATION"] = "publish"
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init(env=env)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("context", "operation"),
        )
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.docker_calls(), [])
        self.assert_failure("context", "operation")

    def test_wrong_workflow_ref_is_rejected_before_api_or_docker(self):
        env = dict(self.env)
        env["GITHUB_WORKFLOW_REF"] = (
            "xtianxx/payment-hub/.github/workflows/ci.yml@refs/heads/main"
        )
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init(env=env)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("context", "workflow-ref"),
        )
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.docker_calls(), [])
        self.assert_failure("context", "workflow-ref")

    def test_wrong_ref_and_event_are_rejected_before_api_or_docker(self):
        cases = (
            ("GITHUB_REF", "refs/heads/feature", "ref"),
            ("GITHUB_EVENT_NAME", "push", "event"),
            ("GITHUB_ACTIONS", "false", "actions"),
        )
        for name, value, kind in cases:
            with self.subTest(name=name):
                env = dict(self.env)
                env[name] = value
                with self.assertRaises(init.ReleaseError) as caught:
                    self.run_init(env=env)
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    ("context", kind),
                )
                self.assertEqual(self.http.calls, [])
                self.assertEqual(self.docker_calls(), [])
                self.assert_failure("context", kind)

    def test_wrong_owner_fails_before_api(self):
        env = dict(self.env)
        env["GITHUB_ACTOR"] = "other-user"
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init(env=env)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("context", "actor-owner"),
        )
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.docker_calls(), [])
        self.assert_failure("context", "actor-owner")

    def test_source_mismatch_fails_before_api(self):
        env = dict(self.env)
        env["GITHUB_SHA"] = OTHER_SHA
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init(env=env)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("context", "sha"),
        )
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.docker_calls(), [])
        self.assert_failure("context", "sha")

    def test_missing_approval_flags_are_rejected_before_api_or_docker(self):
        cases = (
            ({"approve": False}, "initialization"),
            ({"acknowledge": False}, "public-creation"),
            ({"preconditions": False}, "preconditions"),
        )
        for overrides, kind in cases:
            with self.subTest(kind=kind):
                args = self.make_args(**overrides)
                with self.assertRaises(init.ReleaseError) as caught:
                    self.run_init(args=args)
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    ("approval", kind),
                )
                self.assertEqual(self.http.calls, [])
                self.assertEqual(self.docker_calls(), [])
                self.assert_failure("approval", kind)

    def test_positive_integer_and_descriptor_validation_rejects_injection(self):
        for value in ("0", "-1", "1e3", "1\n", "01", " 1"):
            with self.subTest(run_id=value):
                with self.assertRaises(init.ReleaseError) as caught:
                    self.run_init(args=self.make_args(run_id=value))
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    ("cli", "ci-run-id"),
                )
                self.assertEqual(self.http.calls, [])
        for value in ("0", "-2", "2.0", "true"):
            with self.subTest(attempt=value):
                with self.assertRaises(init.ReleaseError) as caught:
                    self.run_init(args=self.make_args(attempt=value))
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    ("cli", "ci-run-attempt"),
                )
        for value in ("0", "-1", "1e3", "1\n", "01", " 1"):
            with self.subTest(artifact_id=value):
                with self.assertRaises(init.ReleaseError) as caught:
                    self.run_init(args=self.make_args(artifact_id=value))
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    ("cli", "bundle-artifact-id"),
                )
        for value in (
            "d" * 64,
            "sha256:" + "d" * 63,
            "sha256:" + "D" * 64,
            "sha256:" + "d" * 64 + "\n",
        ):
            with self.subTest(artifact_digest=value):
                with self.assertRaises(init.ReleaseError) as caught:
                    self.run_init(args=self.make_args(artifact_digest=value))
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    ("cli", "bundle-artifact-digest"),
                )
        args = self.make_args()
        args.ci_run_id = True
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init(args=args)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cli", "ci-run-id"),
        )
        args = self.make_args()
        args.bundle_artifact_id = True
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init(args=args)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cli", "bundle-artifact-id"),
        )
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init(
                args=self.make_args(descriptor_sha="sha256:" + "a" * 64)
            )
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cli", "expected-descriptor-sha"),
        )

    def test_forbidden_namespace_controls_are_rejected(self):
        for name in (
            "bootstrap_packages",
            "approve_external",
            "target",
            "tag",
            "role",
            "ref",
        ):
            with self.subTest(name=name):
                args = self.make_args()
                setattr(args, name, True if name in (
                    "bootstrap_packages",
                    "approve_external",
                ) else "x")
                with self.assertRaises(init.ReleaseError) as caught:
                    self.run_init(args=args)
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    ("cli", "forbidden-option"),
                )

    def test_parser_rejects_bootstrap_external_and_unknown_arguments(self):
        base = [
            "--source-sha",
            SOURCE_SHA,
            "--ci-run-id",
            "1001",
            "--ci-run-attempt",
            "1",
            "--bundle-artifact-id",
            str(BUNDLE_ARTIFACT_ID),
            "--bundle-artifact-digest",
            BUNDLE_ARTIFACT_DIGEST,
            "--bundle-dir",
            str(self.bundle),
            "--expected-descriptor-sha256",
            self.descriptor_sha,
            "--artifact-root",
            str(self.root),
            "--go",
            "/usr/bin/true",
            "--cache-dir",
            str(self.base / "cache"),
        ]
        for extra in (
            ("--bootstrap-packages",),
            ("--approve-external",),
            ("--stage", "publish"),
            ("--target", "ghcr.io/other/repo"),
            ("--tag", "latest"),
            ("--role", "app"),
            ("--ref", "refs/heads/main"),
            ("--unknown-option",),
        ):
            with self.subTest(extra=extra):
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    init._build_parser().parse_args([*base, *extra])


class CiAndHandoffTests(InitTestBase):
    def test_selection_run_lock_mismatch_fails_before_api_or_docker(self):
        self.selection = self.selection_value(run_id=9999)
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("ci", "selection-mismatch"),
        )
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.docker_calls(), [])
        self.assert_failure("ci", "selection-mismatch")

    def test_selection_artifact_id_and_digest_locks_are_bound(self):
        cases = (
            (self.selection_value(artifact_id=501), "id"),
            (
                self.selection_value(
                    artifact_digest="sha256:" + "9" * 64
                ),
                "digest",
            ),
            (self.selection_value(source_sha=OTHER_SHA), "source"),
        )
        for selection, label in cases:
            with self.subTest(label=label):
                self.selection = selection
                with self.assertRaises(init.ReleaseError) as caught:
                    self.run_init()
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    ("ci", "selection-mismatch"),
                )
                self.assertEqual(self.http.calls, [])
                self.assertEqual(self.docker_calls(), [])
                self.assert_failure("ci", "selection-mismatch")

    def test_selector_failure_is_reported_with_fixed_taxonomy(self):
        self.selection_outcomes = [
            init.selection_module.CI.PreflightError("runs", "not-success")
        ]
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("ci", "runs-not-success"),
        )
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.docker_calls(), [])
        self.assert_failure("ci", "runs-not-success")

    def test_selector_recheck_immediately_before_push_blocks_the_only_push(self):
        self.selection_outcomes = [
            self.selection,
            init.selection_module.CI.PreflightError("runs", "not-success"),
        ]
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("ci", "runs-not-success"),
        )
        self.assertEqual(self.selection_calls, 2)
        summary = self.assert_failure("ci", "runs-not-success")
        self.assertEqual(self.fake.login_calls, 1)
        self.assertEqual(self.fake.push_calls, [])
        self.assertIs(summary["push"]["attempted"], False)
        self.assert_no_app_push()

    def test_package_refresh_advancing_ci_is_caught_by_the_last_selector(self):
        def advance_selection(session: int, page: int) -> None:
            if session == 2 and page == 1:
                self.selection = self.selection_value(
                    run_id=1002, attempt=2
                )

        self.http.inventory_page_callback = advance_selection
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("ci", "selection-mismatch"),
        )
        self.assertEqual(self.selection_calls, 2)
        summary = self.assert_failure("ci", "selection-mismatch")
        self.assertEqual(self.fake.login_calls, 1)
        self.assertEqual(self.fake.push_calls, [])
        self.assertIs(summary["push"]["attempted"], False)
        self.assert_no_app_push()

    def test_descriptor_hash_mismatch_fails_before_audit(self):
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init(args=self.make_args(descriptor_sha="c" * 64))
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("handoff", "descriptor-sha"),
        )
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.docker_calls(), [])
        self.assert_failure("handoff", "descriptor-sha")

    def test_handoff_source_mismatch_fails_before_audit(self):
        self.selection = self.selection_value(source_sha=OTHER_SHA)
        fake = InitFakeRunner(self.pair, http=self.http, git_sha=OTHER_SHA)
        env = dict(self.env)
        env["GITHUB_SHA"] = OTHER_SHA
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init(
                args=self.make_args(source_sha=OTHER_SHA),
                env=env,
                fake=fake,
            )
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("handoff", "source-sha"),
        )
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.docker_calls(fake), [])
        self.assert_failure("handoff", "source-sha")

    def test_handoff_source_mismatch_assertion_checks_runner_under_test(self):
        self.selection = self.selection_value(source_sha=OTHER_SHA)
        fake = InitFakeRunner(self.pair, http=self.http, git_sha=OTHER_SHA)
        env = dict(self.env)
        env["GITHUB_SHA"] = OTHER_SHA
        original = init.MigrateInitializer._verify_selection

        def mutated(instance, *args, **kwargs):
            instance._docker(["version"])
            return original(instance, *args, **kwargs)

        with mock.patch.object(
            init.MigrateInitializer, "_verify_selection", new=mutated
        ):
            with self.assertRaises(init.ReleaseError) as caught:
                self.run_init(
                    args=self.make_args(source_sha=OTHER_SHA),
                    env=env,
                    fake=fake,
                )
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("handoff", "source-sha"),
        )
        # The old helper target would miss this call; the runner-under-test
        # aware helper must see it.
        self.assertEqual(self.docker_calls(), [])
        self.assertNotEqual(self.docker_calls(fake), [])


class PackagePrecheckTests(InitTestBase):
    def test_missing_app_package_is_rejected(self):
        self.http.packages["app"] = None
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "app-absent"),
        )
        self.assertEqual(self.docker_calls(), [])
        self.assert_failure("package", "app-absent")

    def test_existing_public_migrate_package_is_rejected_without_takeover(self):
        self.http.packages["migrate"] = tr.package_metadata("migrate")
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "migrate-exists"),
        )
        self.assertEqual(self.docker_calls(), [])
        self.assertEqual(self.fake.push_calls, [])
        self.assert_failure("package", "migrate-exists")

    def test_existing_private_migrate_package_is_rejected(self):
        self.http.packages["migrate"] = tr.package_metadata(
            "migrate", visibility="private"
        )
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "migrate-visibility"),
        )
        self.assertEqual(self.docker_calls(), [])
        self.assertEqual(self.fake.push_calls, [])
        self.assert_failure("package", "migrate-visibility")

    def test_unknown_inventory_body_is_rejected(self):
        self.http.inventory_body_overrides[1] = b"{}"
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "inventory-json"),
        )
        self.assertEqual(self.docker_calls(), [])
        self.assert_failure("package", "inventory-json")

    def test_incomplete_inventory_pagination_is_rejected(self):
        self.http.inventory_page_overrides[1] = [
            tr.package_metadata("app")
        ]
        next_url = (
            release.GITHUB_API
            + "/user/packages?package_type=container&per_page=100&page=2"
        )
        self.http.inventory_link_overrides[1] = f'<{next_url}>; rel="next"'
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "inventory-incomplete"),
        )
        self.assertEqual(self.docker_calls(), [])
        self.assert_failure("package", "inventory-incomplete")


class PrePushRaceTests(InitTestBase):
    def test_app_changed_before_push_blocks_the_push(self):
        self.fake.tag_hook = lambda: self.http.packages.__setitem__(
            "app", tr.package_metadata("app", package_id=999)
        )
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "app-changed"),
        )
        summary = self.assert_failure("package", "app-changed")
        self.assertIs(summary["push"]["attempted"], False)
        self.assertEqual(self.fake.push_calls, [])
        self.assert_no_app_push()

    def test_app_changed_after_push_is_detected_by_normal_check(self):
        self.fake.push_hook = lambda: self.http.packages.__setitem__(
            "app", tr.package_metadata("app", package_id=999)
        )
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "app-changed"),
        )
        summary = self.assert_failure("package", "app-changed")
        self.assertIs(summary["push"]["attempted"], True)
        self.assertEqual(summary["push"]["side_effect"], "verified")
        self.assert_only_migrate_push()
        self.assert_no_app_push()
        self.assertEqual(self.fake.check_image_calls, 0)

    def test_new_migrate_id_must_stay_frozen_between_postchecks(self):
        self.http.migrate_ids = (MIGRATE_INITIALIZATION_ID, 202)
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "migrate-changed"),
        )
        summary = self.assert_failure("package", "migrate-changed")
        self.assertEqual(
            summary["migrate"]["package_id"], MIGRATE_INITIALIZATION_ID
        )
        self.assertEqual(summary["migrate"]["visibility"], "private")
        self.assertEqual(
            summary["last_observed"]["migrate_package_id"], 202
        )
        self.assertEqual(
            summary["last_observed"]["migrate_phase"], "post-push"
        )
        self.assertEqual(summary["push"]["side_effect"], "verified")
        self.assert_only_migrate_push()
        self.assertEqual(self.fake.check_image_calls, 0)


class PostPackageMetadataTests(InitTestBase):
    def test_internal_visibility_is_rejected_without_mutation(self):
        self.http.migrate_visibility = "internal"
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "migrate-visibility"),
        )
        self.assert_failure("package", "migrate-visibility")
        self.assert_only_migrate_push()
        self.assert_no_visibility_or_delete_api()

    def test_wrong_repository_is_rejected(self):
        self.http.migrate_repository = {"full_name": "other/repository"}
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "migrate-repository"),
        )
        self.assert_failure("package", "migrate-repository")
        self.assert_only_migrate_push()
        self.assert_no_visibility_or_delete_api()

    def test_wrong_owner_is_rejected(self):
        self.http.migrate_owner = {"login": "other-user"}
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("package", "migrate-owner"),
        )
        self.assert_failure("package", "migrate-owner")
        self.assert_only_migrate_push()
        self.assert_no_visibility_or_delete_api()


class PushAndPullMismatchTests(InitTestBase):
    def test_push_digest_mismatch_keeps_unknown_side_effect_and_stops(self):
        self.fake.push_digest_override = "sha256:" + "9" * 64
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("push", "migrate-digest"),
        )
        summary = self.assert_failure("push", "migrate-digest")
        self.assertIs(summary["push"]["attempted"], True)
        self.assertEqual(summary["push"]["side_effect"], "unknown")
        self.assertIsNone(summary["remote"])
        self.assertIsNone(summary["pull"])
        self.assert_only_migrate_push()
        self.assert_no_app_push()
        self.assertEqual(self.fake.check_image_calls, 0)

    def test_raw_manifest_hash_mismatch_is_rejected(self):
        self.http.manifest_body_override["migrate"] = b"{}"
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("registry", "migrate-body-hash"),
        )
        summary = self.assert_failure("registry", "migrate-body-hash")
        self.assertEqual(summary["push"]["side_effect"], "unknown")
        self.assertIsNone(summary["remote"])
        self.assert_only_migrate_push()
        self.assert_no_app_push()

    def test_pulled_root_mismatch_is_rejected(self):
        self.fake.pull_root_override["migrate"] = {
            "mediaType": release.OCI_INDEX,
            "digest": self.pair.migrate_id,
            "size": len(self.pair.root_bytes[self.pair.migrate_id]) + 1,
        }
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("pull", "migrate-root"),
        )
        summary = self.assert_failure("pull", "migrate-root")
        self.assertEqual(summary["push"]["side_effect"], "verified")
        self.assertIsNotNone(summary["remote"])
        self.assertIsNone(summary["pull"])
        self.assert_only_migrate_push()
        self.assert_no_app_push()
        self.assertEqual(self.fake.check_image_calls, 0)

    def test_pulled_id_mismatch_is_rejected(self):
        self.fake.after_pull_ids["migrate"] = "sha256:" + "9" * 64
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("pull", "migrate-id"),
        )
        summary = self.assert_failure("pull", "migrate-id")
        self.assertEqual(summary["push"]["side_effect"], "verified")
        self.assertIsNone(summary["pull"])
        self.assert_only_migrate_push()
        self.assert_no_app_push()


class CancellationAndCleanupTests(InitTestBase):
    def test_cancel_before_push_leaves_no_push_attempt(self):
        def cancel() -> None:
            raise KeyboardInterrupt

        self.fake.tag_hook = cancel
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cancelled", "signal"),
        )
        summary = self.assert_failure("cancelled", "signal")
        self.assertIs(summary["push"]["attempted"], False)
        self.assertEqual(self.fake.push_calls, [])
        self.assertEqual(self.fake.check_image_calls, 0)
        self.assert_no_app_push()

    def test_cancel_after_push_keeps_attempt_and_does_not_roll_back(self):
        def cancel() -> None:
            raise KeyboardInterrupt

        self.fake.pull_hook = cancel
        with self.assertRaises(init.ReleaseError) as caught:
            self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("cancelled", "signal"),
        )
        summary = self.assert_failure("cancelled", "signal")
        self.assertIs(summary["push"]["attempted"], True)
        self.assertEqual(summary["push"]["side_effect"], "verified")
        self.assertIsNotNone(summary["remote"])
        self.assertIsNone(summary["pull"])
        self.assert_only_migrate_push()
        self.assertEqual(self.fake.check_image_calls, 0)
        self.assert_no_app_push()
        self.assert_no_visibility_or_delete_api()

    def test_cleanup_failure_fails_overall_before_acceptance(self):
        auth_dir = self.base / "auth-dir"

        def make_auth_dir(*args: Any, **kwargs: Any) -> str:
            auth_dir.mkdir(parents=True, exist_ok=True)
            return str(auth_dir)

        with (
            mock.patch.object(init.tempfile, "mkdtemp", new=make_auth_dir),
            mock.patch.object(
                init.shutil, "rmtree", side_effect=OSError("boom")
            ),
        ):
            with self.assertRaises(init.ReleaseError) as caught:
                self.run_init()
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("auth", "cleanup-failed"),
        )
        summary = self.assert_failure("auth", "cleanup-failed")
        self.assertEqual(
            summary["credential_cleanup"]["cleanup_failed"], True
        )
        self.assertIs(summary["process_cleanup_failed"], False)
        self.assertEqual(
            summary["credential_cleanup"]["docker_config_removed"], False
        )
        self.assertEqual(self.fake.check_image_calls, 0)
        self.assert_only_migrate_push()
        self.assert_no_app_push()


if __name__ == "__main__":
    unittest.main()
