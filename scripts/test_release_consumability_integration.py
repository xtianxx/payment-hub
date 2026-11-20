"""Production-to-consumer integration for release consumability.

This module connects the real publish path to the offline verifier:

* ``Release.publish`` runs with the existing fake Docker/GitHub/registry
  peripherals from ``test_release.py`` and writes the real
  ``release-summary.json``.
* The handoff descriptor is produced by the existing ``make_bundle`` fixture.
* The ``Write release evidence`` shell block is read from the real
  ``.github/workflows/release.yml`` and executed with bash in a temporary
  ``RUNNER_TEMP`` to produce the descriptor copy and
  ``release-evidence.json``.
* ``verify_release`` must accept that exact artifact set and return only the
  two fixed digest references.

Negative cases mutate the generated success artifacts (or a real failed
publish summary) and require a rejection category from the verifier.  No
Docker daemon, network, registry, GitHub API, or real release is used.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, Mapping

import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
RELEASE_WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
VERIFIER_SCRIPT = SCRIPTS / "verify-release-consumable.py"
INIT_SCRIPT = SCRIPTS / "init-migrate-package.py"

RELEASE_RUN_ID = 44001
RELEASE_RUN_ATTEMPT = 2
CI_RUN_ID = 33001
CI_RUN_ATTEMPT = 1
BUNDLE_ARTIFACT_ID = 55001
BUNDLE_ARTIFACT_DIGEST = "sha256:" + "d" * 64
RELEASE_WORKFLOW_PATH = ".github/workflows/release.yml"
TRUSTED_REPOSITORY = "xtianxx/payment-hub"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"{path.name} is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


# Import the existing release test module only for its fake peripherals and
# its no-test base class.  Loading it under this private name keeps unittest
# discovery from seeing a second copy of test_release.py's test cases.
SUPPORT = _load_module("release_integration_support", SCRIPTS / "test_release.py")
release = SUPPORT.release

VERIFY = None
INIT = None


def setUpModule() -> None:
    global VERIFY, INIT
    if not VERIFIER_SCRIPT.is_file():
        raise AssertionError(
            "scripts/verify-release-consumable.py is missing; the release "
            "consumability gate must not be skipped"
        )
    VERIFY = _load_module("verify_release_consumable_under_test", VERIFIER_SCRIPT)
    INIT = _load_module("init_migrate_package_under_test", INIT_SCRIPT)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _extract_evidence_run() -> str:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    try:
        steps = workflow["jobs"]["upload"]["steps"]
    except (KeyError, TypeError) as exc:  # pragma: no cover - manifest drift
        raise AssertionError(
            "release.yml no longer has jobs.upload.steps"
        ) from exc
    for step in steps:
        if step.get("name") == "Write release evidence":
            run = step.get("run")
            if not isinstance(run, str) or "python3" not in run:
                raise AssertionError(
                    "Write release evidence step is not an executable script"
                )
            return run
    raise AssertionError("Write release evidence step is missing")


class ReleaseConsumabilityIntegrationTests(SUPPORT.ReleaseTestBase):
    """Real publish summary + real evidence shell + offline consumer."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.evidence_run = _extract_evidence_run()

    def setUp(self) -> None:
        super().setUp()
        self._delivery_cache: dict[str, Any] | None = None
        self._case_index = 0

    # -- artifact production -------------------------------------------------

    def _next_label(self, prefix: str) -> str:
        self._case_index += 1
        return f"{prefix}-{self._case_index}"

    def _run_evidence(
        self,
        runner_temp: Path,
        summary_bytes: bytes,
        descriptor_bytes: bytes,
        *,
        source_sha: str = SUPPORT.SOURCE_SHA,
        release_run_attempt: int = RELEASE_RUN_ATTEMPT,
    ) -> tuple[bytes, bytes]:
        root = runner_temp / "payment-hub-release-publish"
        bundle_dir = runner_temp / "payment-hub-release-bundle"
        root.mkdir(parents=True, exist_ok=True)
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (root / "release-summary.json").write_bytes(summary_bytes)
        (bundle_dir / "descriptor.json").write_bytes(descriptor_bytes)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "RUNNER_TEMP": str(runner_temp),
            "SOURCE_SHA": source_sha,
            "RELEASE_RUN_ID": str(RELEASE_RUN_ID),
            "RELEASE_RUN_ATTEMPT": str(release_run_attempt),
            "CI_RUN_ID": str(CI_RUN_ID),
            "CI_RUN_ATTEMPT": str(CI_RUN_ATTEMPT),
            "BUNDLE_ARTIFACT_ID": str(BUNDLE_ARTIFACT_ID),
            "BUNDLE_ARTIFACT_DIGEST": BUNDLE_ARTIFACT_DIGEST,
            "EXPECTED_DESCRIPTOR_SHA": _sha256_hex(descriptor_bytes),
            "REPOSITORY": TRUSTED_REPOSITORY,
        }
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        result = subprocess.run(
            [bash, "--noprofile", "--norc", "-c", self.evidence_run],
            cwd=str(runner_temp),
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(
            result.returncode,
            0,
            "release evidence shell failed:\n"
            + result.stdout
            + result.stderr,
        )
        return (
            (root / "release-evidence.json").read_bytes(),
            (root / "descriptor.json").read_bytes(),
        )

    def _build_delivery(self) -> dict[str, Any]:
        bundle, _ = SUPPORT.make_bundle(self.base / "bundle")
        descriptor_bytes = (bundle / release.DESCRIPTOR_FILE).read_bytes()
        descriptor_sha = _sha256_hex(descriptor_bytes)
        fake = SUPPORT.FakeRunner(self.pair)
        http = SUPPORT.FakeHTTP(self.pair)
        root = self.base / "payment-hub-release-publish"
        code = self.run_publish(root, bundle, descriptor_sha, fake, http)
        self.assertEqual(code, 0)
        summary_bytes = (root / release.SUMMARY_FILE).read_bytes()
        evidence_bytes, copied_descriptor = self._run_evidence(
            self.base, summary_bytes, descriptor_bytes
        )
        self.assertEqual(copied_descriptor, descriptor_bytes)
        return {
            "bundle": bundle,
            "descriptor_sha": descriptor_sha,
            "summary": summary_bytes,
            "descriptor": descriptor_bytes,
            "evidence": evidence_bytes,
            "run_metadata": self._run_metadata(),
        }

    def _delivery(self) -> dict[str, Any]:
        if self._delivery_cache is None:
            self._delivery_cache = self._build_delivery()
        return self._delivery_cache

    def _files_from(
        self,
        *,
        summary_bytes: bytes,
        descriptor_bytes: bytes,
        label: str,
    ) -> dict[str, Any]:
        runner_temp = self.base / "cases" / self._next_label(label)
        evidence_bytes, copied_descriptor = self._run_evidence(
            runner_temp, summary_bytes, descriptor_bytes
        )
        files = {
            "summary": summary_bytes,
            "descriptor": copied_descriptor,
            "evidence": evidence_bytes,
            "run_metadata": self._run_metadata(),
        }
        self.assertEqual(files["descriptor"], descriptor_bytes)
        return files

    def _files_with_descriptor(
        self,
        descriptor_value: Mapping[str, Any],
        *,
        label: str,
    ) -> dict[str, Any]:
        descriptor_bytes = _json_bytes(descriptor_value)
        summary = json.loads(self._delivery()["summary"])
        summary["descriptor_sha256"] = _sha256_hex(descriptor_bytes)
        return self._files_from(
            summary_bytes=_json_bytes(summary),
            descriptor_bytes=descriptor_bytes,
            label=label,
        )

    # -- run metadata and consumer ------------------------------------------

    def _run_metadata(self, **overrides: Any) -> bytes:
        value: dict[str, Any] = {
            "id": RELEASE_RUN_ID,
            "run_attempt": RELEASE_RUN_ATTEMPT,
            "head_sha": SUPPORT.SOURCE_SHA,
            "head_branch": "main",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "path": RELEASE_WORKFLOW_PATH,
            "repository": {"full_name": TRUSTED_REPOSITORY},
        }
        value.update(overrides)
        return _json_bytes(value)

    def _verify(
        self,
        files: Mapping[str, Any],
        *,
        run_metadata: bytes | None = None,
        expected_source_sha: str = SUPPORT.SOURCE_SHA,
        release_run_id: int = RELEASE_RUN_ID,
        release_run_attempt: int = RELEASE_RUN_ATTEMPT,
        expected_app_digest: str | None = None,
        expected_migrate_digest: str | None = None,
    ) -> dict[str, str]:
        metadata = (
            files["run_metadata"]
            if run_metadata is None
            else run_metadata
        )
        return VERIFY.verify_release(
            files["summary"],
            files["descriptor"],
            files["evidence"],
            metadata,
            expected_source_sha=expected_source_sha,
            release_run_id=release_run_id,
            release_run_attempt=release_run_attempt,
            expected_app_digest=expected_app_digest,
            expected_migrate_digest=expected_migrate_digest,
        )

    def _assert_rejected(
        self,
        files: Mapping[str, Any],
        *,
        run_metadata: bytes | None = None,
        stage: str | None = None,
        kind: str | None = None,
        **kwargs: Any,
    ) -> None:
        with self.assertRaises(VERIFY.VerificationError) as caught:
            self._verify(files, run_metadata=run_metadata, **kwargs)
        error = caught.exception
        self.assertIsInstance(error.stage, str)
        self.assertIsInstance(error.kind, str)
        self.assertTrue(error.stage)
        self.assertTrue(error.kind)
        if stage is not None:
            self.assertEqual(error.stage, stage)
        if kind is not None:
            self.assertEqual(error.kind, kind)

    # -- positive path ------------------------------------------------------

    def test_real_publish_handoff_is_accepted_with_fixed_digest_references(
        self,
    ) -> None:
        delivery = self._delivery()
        summary = json.loads(delivery["summary"])
        descriptor = json.loads(delivery["descriptor"])
        self.assertTrue(summary["published"])
        self.assertEqual(summary["outcome"], "pass")
        self.assertIsNone(summary["failure"])
        self.assertEqual(
            descriptor["source_sha"], SUPPORT.SOURCE_SHA
        )

        references = self._verify(delivery)

        expected = {
            role: release.TARGETS[role]
            + "@"
            + descriptor["images"][role]["root_descriptor"]["digest"]
            for role in release.ROLES
        }
        self.assertEqual(references, expected)
        self.assertNotEqual(references["app"], references["migrate"])
        for role in release.ROLES:
            digest = descriptor["images"][role]["root_descriptor"]["digest"]
            self.assertIn("@sha256:", references[role])
            self.assertEqual(
                summary["push"][role]["candidate_digest"], digest
            )
            self.assertEqual(summary["remote"][role]["digest"], digest)
            self.assertEqual(
                summary["pull"][role]["id"],
                descriptor["images"][role]["local_id"],
            )
        # The evidence file came from the real shell block, not from a
        # hand-written fixture.
        evidence = json.loads(delivery["evidence"])
        self.assertEqual(
            evidence["schema"], "payment-hub.release-evidence.v1"
        )
        self.assertEqual(evidence["repository"], TRUSTED_REPOSITORY)
        self.assertEqual(evidence["source_sha"], SUPPORT.SOURCE_SHA)
        self.assertEqual(evidence["release_run_id"], RELEASE_RUN_ID)
        self.assertEqual(
            evidence["release_run_attempt"], RELEASE_RUN_ATTEMPT
        )
        self.assertEqual(
            evidence["descriptor_sha256"],
            _sha256_hex(delivery["descriptor"]),
        )

    def test_paired_independent_digests_are_role_bound(self) -> None:
        delivery = self._delivery()
        descriptor = json.loads(delivery["descriptor"])
        app_digest = descriptor["images"]["app"]["root_descriptor"]["digest"]
        migrate_digest = descriptor["images"]["migrate"]["root_descriptor"][
            "digest"
        ]
        accepted = self._verify(
            delivery,
            expected_app_digest=app_digest,
            expected_migrate_digest=migrate_digest,
        )
        self.assertEqual(accepted["app"], release.TARGETS["app"] + "@" + app_digest)
        self.assertEqual(
            accepted["migrate"],
            release.TARGETS["migrate"] + "@" + migrate_digest,
        )
        # An unpaired expectation is rejected before any artifact checks.
        self._assert_rejected(
            delivery, expected_app_digest=app_digest
        )
        # Swapping the independent expectations by role must be rejected even
        # though both digests belong to the same verified pair.
        self._assert_rejected(
            delivery,
            expected_app_digest=migrate_digest,
            expected_migrate_digest=app_digest,
        )

    # -- run-level negatives ------------------------------------------------

    def test_successful_summary_with_failed_run_is_rejected(self) -> None:
        delivery = self._delivery()
        cases = {
            "conclusion-failure": {"conclusion": "failure"},
            "conclusion-cancelled": {"conclusion": "cancelled"},
            "status-in-progress": {
                "status": "in_progress",
                "conclusion": None,
            },
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                self._assert_rejected(
                    delivery, run_metadata=self._run_metadata(**overrides)
                )

    def test_wrong_release_attempt_is_rejected(self) -> None:
        delivery = self._delivery()
        self._assert_rejected(
            delivery,
            run_metadata=self._run_metadata(
                run_attempt=RELEASE_RUN_ATTEMPT + 1
            ),
        )
        # The evidence shell's attempt must also match the requested one.
        summary_bytes = delivery["summary"]
        descriptor_bytes = delivery["descriptor"]
        runner_temp = self.base / "cases" / self._next_label("attempt")
        evidence_bytes, _ = self._run_evidence(
            runner_temp,
            summary_bytes,
            descriptor_bytes,
            release_run_attempt=RELEASE_RUN_ATTEMPT + 1,
        )
        mismatched = {
            "summary": summary_bytes,
            "descriptor": descriptor_bytes,
            "evidence": evidence_bytes,
        }
        self._assert_rejected(
            mismatched,
            run_metadata=delivery["run_metadata"],
        )
        self._assert_rejected(
            delivery, release_run_attempt=RELEASE_RUN_ATTEMPT + 1
        )

    def test_source_mismatch_is_rejected(self) -> None:
        delivery = self._delivery()
        run_cases = {
            "head-sha": {"head_sha": "c" * 40},
            "head-branch": {"head_branch": "feature"},
            "event": {"event": "push"},
            "workflow": {"path": ".github/workflows/publish.yml"},
            "repository": {"repository": {"full_name": "other/repository"}},
        }
        for label, overrides in run_cases.items():
            with self.subTest(case=label):
                self._assert_rejected(
                    delivery, run_metadata=self._run_metadata(**overrides)
                )
        self._assert_rejected(
            delivery, expected_source_sha="c" * 40
        )

        # A descriptor for another source, even with a consistent evidence
        # digest and summary descriptor hash, is not this release.
        descriptor = copy.deepcopy(json.loads(delivery["descriptor"]))
        descriptor["source_sha"] = "c" * 40
        descriptor["tag"] = "sha-" + "c" * 40
        files = self._files_with_descriptor(
            descriptor, label="descriptor-source"
        )
        self._assert_rejected(files)

    # -- summary and artifact negatives ------------------------------------

    def test_replaced_descriptor_is_rejected(self) -> None:
        delivery = self._delivery()
        replaced = copy.deepcopy(json.loads(delivery["descriptor"]))
        replaced["created_utc"] = "2026-11-13T00:00:00+00:00"
        replaced_bytes = _json_bytes(replaced)
        self.assertNotEqual(
            _sha256_hex(replaced_bytes), _sha256_hex(delivery["descriptor"])
        )
        files = {
            "summary": delivery["summary"],
            "descriptor": replaced_bytes,
            "evidence": delivery["evidence"],
            "run_metadata": delivery["run_metadata"],
        }
        self._assert_rejected(files)

    def test_swapped_image_roles_are_rejected(self) -> None:
        delivery = self._delivery()
        descriptor = copy.deepcopy(json.loads(delivery["descriptor"]))
        app = descriptor["images"]["app"]
        migrate = descriptor["images"]["migrate"]
        descriptor["images"]["app"] = migrate
        descriptor["images"]["migrate"] = app
        files = self._files_with_descriptor(
            descriptor, label="role-swap"
        )
        # The summary still records the original role digests, so the
        # consumer must bind each summary role to the matching descriptor.
        self._assert_rejected(files)

    def test_initialization_summary_is_not_a_delivery_record(self) -> None:
        delivery = self._delivery()
        state = INIT._new_state(argparse.Namespace())
        state["outcome"] = "pass"
        state["initialization_verified"] = True
        self.assertEqual(state["schema"], INIT.SUMMARY_SCHEMA)
        self.assertFalse(state["published"])
        files = self._files_from(
            summary_bytes=_json_bytes(state),
            descriptor_bytes=delivery["descriptor"],
            label="initialization-summary",
        )
        self._assert_rejected(files)

    def test_partial_push_failure_summary_is_rejected(self) -> None:
        delivery = self._delivery()
        fake = SUPPORT.FakeRunner(self.pair)
        fake.push_fail_role = "migrate"
        http = SUPPORT.FakeHTTP(self.pair)
        root = self.base / "partial-publish"
        with self.assertRaises(release.ReleaseError):
            self.run_publish(
                root,
                delivery["bundle"],
                delivery["descriptor_sha"],
                fake,
                http,
            )
        summary_bytes = (root / release.SUMMARY_FILE).read_bytes()
        summary = json.loads(summary_bytes)
        # This is a real publish summary: app verified, migrate unknown.
        self.assertFalse(summary["published"])
        self.assertEqual(summary["pushed_roles"], ["app"])
        self.assertEqual(
            summary["push_side_effects"],
            {"app": "verified", "migrate": "unknown"},
        )
        # The evidence shell is run around this real failed summary only to
        # exercise the offline consumer; a failed publish step would normally
        # stop the job before that step.
        files = self._files_from(
            summary_bytes=summary_bytes,
            descriptor_bytes=delivery["descriptor"],
            label="partial-push",
        )
        self._assert_rejected(files)

    def test_cleanup_failure_is_rejected(self) -> None:
        delivery = self._delivery()
        summary = json.loads(delivery["summary"])
        self.assertFalse(summary["cleanup_failed"])
        summary["cleanup_failed"] = True
        files = self._files_from(
            summary_bytes=_json_bytes(summary),
            descriptor_bytes=delivery["descriptor"],
            label="cleanup-failed",
        )
        self._assert_rejected(files)

    def test_package_id_change_is_rejected(self) -> None:
        delivery = self._delivery()
        for role in release.ROLES:
            with self.subTest(role=role):
                summary = json.loads(delivery["summary"])
                original = summary["packages_initial"][role]["id"]
                summary["packages_postcheck"][role]["id"] = original + 1000
                summary["packages"][role]["id"] = original + 1000
                files = self._files_from(
                    summary_bytes=_json_bytes(summary),
                    descriptor_bytes=delivery["descriptor"],
                    label="package-id-" + role,
                )
                self._assert_rejected(
                    files, stage="summary", kind="package-original-id"
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
