#!/usr/bin/env python3
"""Offline adversarial tests for the release-consumable verifier.

The public ``make_*`` helpers build a complete, internally consistent release
artifact set.  Other tests (including the workflow/release integration test)
may import this module and reuse them.

No network, Docker, GitHub API, or GHCR access is performed here.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from typing import Any
from unittest import mock

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
VERIFIER_PATH = SCRIPT_DIR / "verify-release-consumable.py"
SPEC = importlib.util.spec_from_file_location(
    "verify_release_consumable", VERIFIER_PATH
)
if SPEC is None or SPEC.loader is None:
    raise SystemExit("verify-release-consumable.py is unavailable")
verifier = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verifier
SPEC.loader.exec_module(verifier)

SOURCE_SHA = "a" * 40
MANIFEST_DIGEST = "b" * 64
APP_DIGEST = "sha256:" + "1" * 64
MIGRATE_DIGEST = "sha256:" + "2" * 64
ARCHIVE_SHA = "c" * 64
BUILD_RECORD_SHA = "d" * 64
ACCEPTED_REPORT_SHA = "e" * 64
BUNDLE_ARTIFACT_DIGEST = "sha256:" + "3" * 64
RELEASE_RUN_ID = 7001
RELEASE_RUN_ATTEMPT = 2
CI_RUN_ID = 7000
CI_RUN_ATTEMPT = 1
BUNDLE_ARTIFACT_ID = 555
APP_PACKAGE_ID = 101
MIGRATE_PACKAGE_ID = 102
REPOSITORY = "xtianxx/payment-hub"
OWNER_LOGIN = "xtianxx"
PLATFORM = "linux/amd64"


def json_bytes(value: Any) -> bytes:
    """Serialize a fixture document exactly as the verifier will hash it."""
    return json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _descriptor_defaults(
    descriptor_bytes: bytes,
) -> tuple[str, str, str]:
    try:
        descriptor = json.loads(descriptor_bytes)
        source_sha = descriptor.get("source_sha", SOURCE_SHA)
        images = descriptor.get("images", {})
        app_digest = images["app"]["root_descriptor"]["digest"]
        migrate_digest = images["migrate"]["root_descriptor"]["digest"]
    except (KeyError, TypeError, ValueError):
        return SOURCE_SHA, APP_DIGEST, MIGRATE_DIGEST
    if not all(
        isinstance(value, str)
        for value in (source_sha, app_digest, migrate_digest)
    ):
        return SOURCE_SHA, APP_DIGEST, MIGRATE_DIGEST
    return source_sha, app_digest, migrate_digest


def make_descriptor(**overrides: Any) -> dict[str, Any]:
    """Return a descriptor matching the production release-handoff v1 schema."""
    descriptor: dict[str, Any] = {
        "schema": verifier.DESCRIPTOR_SCHEMA,
        "source_sha": SOURCE_SHA,
        "source_manifest_digest": MANIFEST_DIGEST,
        "platform": PLATFORM,
        "created_utc": "2026-11-20T00:00:00+00:00",
        "archive": {
            "file": "pair.tar",
            "sha256": ARCHIVE_SHA,
            "size": 4096,
        },
        "builder": {
            "build_record_sha256": BUILD_RECORD_SHA,
            "accepted_report_sha256": ACCEPTED_REPORT_SHA,
            "accepted_report_file": "accepted-images.json",
        },
        "images": {
            "app": {
                "reference": "payment-hub-app:phase6-test",
                "local_id": APP_DIGEST,
                "root_descriptor": {
                    "mediaType": verifier.OCI_INDEX,
                    "digest": APP_DIGEST,
                    "size": 1024,
                },
                "source_label": MANIFEST_DIGEST,
                "platform": PLATFORM,
            },
            "migrate": {
                "reference": "payment-hub-migrate:phase6-test",
                "local_id": MIGRATE_DIGEST,
                "root_descriptor": {
                    "mediaType": verifier.OCI_INDEX,
                    "digest": MIGRATE_DIGEST,
                    "size": 2048,
                },
                "source_label": MANIFEST_DIGEST,
                "platform": PLATFORM,
            },
        },
        "targets": dict(verifier.TARGETS),
        "tag": "sha-" + SOURCE_SHA,
    }
    descriptor.update(overrides)
    return descriptor


def _package_projection(
    role: str, *, package_id: int, include_owner: bool = True
) -> dict[str, Any]:
    package = {
        "name": verifier.PACKAGE_NAMES[role],
        "visibility": "public",
        "repository": REPOSITORY,
        "id": package_id,
    }
    if include_owner:
        package["owner"] = OWNER_LOGIN
    return package


def make_summary(
    descriptor_bytes: bytes,
    *,
    source_sha: str | None = None,
    app_digest: str | None = None,
    migrate_digest: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Return the successful publish summary for the supplied descriptor bytes."""
    default_source, default_app, default_migrate = _descriptor_defaults(
        descriptor_bytes
    )
    source = source_sha if source_sha is not None else default_source
    app = app_digest if app_digest is not None else default_app
    migrate = migrate_digest if migrate_digest is not None else default_migrate
    summary: dict[str, Any] = {
        "schema": verifier.SUMMARY_SCHEMA,
        "stage": "publish",
        "utc": "2026-11-20T00:00:00+00:00",
        "outcome": "pass",
        "published": True,
        "source_sha": source,
        "platform": PLATFORM,
        "descriptor_sha256": hashlib.sha256(descriptor_bytes).hexdigest(),
        "bootstrap_requested": False,
        "audit_scope": "read:packages",
        "pushed_roles": ["app", "migrate"],
        "push_attempted_roles": ["app", "migrate"],
        "push_side_effects": {"app": "verified", "migrate": "verified"},
        "packages": {
            role: _package_projection(role, package_id=package_id)
            for role, package_id in (
                ("app", APP_PACKAGE_ID),
                ("migrate", MIGRATE_PACKAGE_ID),
            )
        },
        "packages_initial": {
            "app": {"status": "EXISTING_VALID", "id": APP_PACKAGE_ID},
            "migrate": {
                "status": "EXISTING_VALID",
                "id": MIGRATE_PACKAGE_ID,
            },
        },
        "packages_postcheck": {
            "app": _package_projection("app", package_id=APP_PACKAGE_ID),
            "migrate": _package_projection(
                "migrate", package_id=MIGRATE_PACKAGE_ID
            ),
        },
        "push": {
            role: {"candidate_digest": digest, "exit": 0}
            for role, digest in (("app", app), ("migrate", migrate))
        },
        "remote": {
            role: {
                "digest": digest,
                "mediaType": verifier.OCI_INDEX,
                "body_sha256": digest.split(":", 1)[1],
                "size": 1024,
                "header_digest": digest,
            }
            for role, digest in (("app", app), ("migrate", migrate))
        },
        "pull": {
            role: {"exit": 0, "id": digest}
            for role, digest in (("app", app), ("migrate", migrate))
        },
        "revalidation": {
            "result": "pass",
            "checks_sha256": "f" * 64,
            "delivery_record_sha256": "0" * 64,
        },
        "prepare_inputs": {
            "requested": False,
            "postgres_pull": "not-run",
            "module_prepare": "not-run",
            "precompile_only": "not-run",
            "business_tests_run": False,
        },
        "credentials_removed": True,
        "cleanup_failed": False,
        "failure": None,
        "limits": [
            "package-inventory-not-atomic",
            "pair-publication-not-atomic",
        ],
        "http_deadline_policy": "bounded test fixture",
        "deployment": "not-performed",
    }
    summary.update(overrides)
    return summary


def make_evidence(
    descriptor_bytes: bytes,
    *,
    source_sha: str | None = None,
    release_run_id: int = RELEASE_RUN_ID,
    release_run_attempt: int = RELEASE_RUN_ATTEMPT,
    **overrides: Any,
) -> dict[str, Any]:
    """Return the frozen release-evidence v1 document for a descriptor."""
    default_source = _descriptor_defaults(descriptor_bytes)[0]
    source = source_sha if source_sha is not None else default_source
    evidence: dict[str, Any] = {
        "schema": verifier.EVIDENCE_SCHEMA,
        "repository": REPOSITORY,
        "source_sha": source,
        "release_run_id": release_run_id,
        "release_run_attempt": release_run_attempt,
        "ci_run_id": CI_RUN_ID,
        "ci_run_attempt": CI_RUN_ATTEMPT,
        "bundle_artifact_id": BUNDLE_ARTIFACT_ID,
        "bundle_artifact_digest": BUNDLE_ARTIFACT_DIGEST,
        "descriptor_sha256": hashlib.sha256(descriptor_bytes).hexdigest(),
    }
    evidence.update(overrides)
    return evidence


def make_run_metadata(
    *,
    source_sha: str = SOURCE_SHA,
    run_id: int = RELEASE_RUN_ID,
    run_attempt: int = RELEASE_RUN_ATTEMPT,
    **overrides: Any,
) -> dict[str, Any]:
    """Return a raw GitHub Actions run object with the required fields."""
    run: dict[str, Any] = {
        "id": run_id,
        "workflow_id": 4242,
        "name": "Release",
        "run_attempt": run_attempt,
        "head_sha": source_sha,
        "head_branch": "main",
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "path": ".github/workflows/release.yml",
        "created_at": "2026-11-20T00:00:00Z",
        "html_url": "https://example.invalid/actions/runs/7001",
        "repository": {"id": 1234, "full_name": REPOSITORY},
    }
    run.update(overrides)
    return run


def make_release_inputs(**overrides: Any) -> dict[str, Any]:
    """Build every positional/keyword input accepted by ``verify_release``.

    ``overrides`` may replace ``descriptor``/``descriptor_bytes``,
    ``summary``/``summary_bytes``, ``evidence``/``evidence_bytes``,
    ``run_metadata``/``run_metadata_bytes`` and the independent expected
    values.  Defaults are derived from the descriptor bytes so a caller can
    pass a real descriptor and still get a consistent set.
    """
    descriptor_bytes: bytes | None = overrides.pop("descriptor_bytes", None)
    descriptor = overrides.pop("descriptor", None)
    if descriptor_bytes is None:
        if descriptor is None:
            descriptor = make_descriptor()
        descriptor_bytes = json_bytes(descriptor)
    source_sha = overrides.get("expected_source_sha")
    if source_sha is None:
        source_sha = _descriptor_defaults(descriptor_bytes)[0]
    summary_bytes = overrides.pop("summary_bytes", None)
    summary = overrides.pop("summary", None)
    if summary_bytes is None:
        if summary is None:
            summary = make_summary(descriptor_bytes, source_sha=source_sha)
        summary_bytes = json_bytes(summary)
    evidence_bytes = overrides.pop("evidence_bytes", None)
    evidence = overrides.pop("evidence", None)
    if evidence_bytes is None:
        if evidence is None:
            evidence = make_evidence(descriptor_bytes, source_sha=source_sha)
        evidence_bytes = json_bytes(evidence)
    run_bytes = overrides.pop("run_metadata_bytes", None)
    run_metadata = overrides.pop("run_metadata", None)
    if run_bytes is None:
        if run_metadata is None:
            run_metadata = make_run_metadata(source_sha=source_sha)
        run_bytes = json_bytes(run_metadata)
    inputs: dict[str, Any] = {
        "summary_bytes": summary_bytes,
        "descriptor_bytes": descriptor_bytes,
        "evidence_bytes": evidence_bytes,
        "run_metadata_bytes": run_bytes,
        "expected_source_sha": source_sha,
        "release_run_id": RELEASE_RUN_ID,
        "release_run_attempt": RELEASE_RUN_ATTEMPT,
        "expected_app_digest": None,
        "expected_migrate_digest": None,
    }
    inputs.update(overrides)
    return inputs


class VerifierTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.inputs = make_release_inputs()
        self.summary = json.loads(self.inputs["summary_bytes"])
        self.descriptor = json.loads(self.inputs["descriptor_bytes"])
        self.evidence = json.loads(self.inputs["evidence_bytes"])
        self.run_metadata = json.loads(self.inputs["run_metadata_bytes"])

    def verify(
        self,
        *,
        summary: dict[str, Any] | None = None,
        descriptor: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
        run_metadata: dict[str, Any] | None = None,
        **overrides: Any,
    ) -> dict[str, str]:
        inputs = dict(self.inputs)
        if summary is not None:
            inputs["summary_bytes"] = json_bytes(summary)
        if descriptor is not None:
            inputs["descriptor_bytes"] = json_bytes(descriptor)
        if evidence is not None:
            inputs["evidence_bytes"] = json_bytes(evidence)
        if run_metadata is not None:
            inputs["run_metadata_bytes"] = json_bytes(run_metadata)
        inputs.update(overrides)
        return verifier.verify_release(**inputs)

    def assert_error(
        self,
        stage: str,
        kind: str,
        **kwargs: Any,
    ) -> None:
        with self.assertRaises(verifier.VerificationError) as caught:
            self.verify(**kwargs)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind), (stage, kind)
        )
        self.assertEqual(str(caught.exception), f"{stage}/{kind}")

    @staticmethod
    def changed(document: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(document)


class ValidReleaseTests(VerifierTestBase):
    def test_valid_fixture_returns_fixed_role_references(self) -> None:
        references = self.verify()
        self.assertEqual(
            references,
            {
                "app": verifier.TARGETS["app"] + "@" + APP_DIGEST,
                "migrate": verifier.TARGETS["migrate"] + "@" + MIGRATE_DIGEST,
            },
        )
        for reference in references.values():
            self.assertIn("@sha256:", reference)
            self.assertNotIn(":sha-", reference)

    def test_expected_digests_must_match_the_same_role(self) -> None:
        references = self.verify(
            expected_app_digest=APP_DIGEST,
            expected_migrate_digest=MIGRATE_DIGEST,
        )
        self.assertIn(APP_DIGEST, references["app"])
        self.assertIn(MIGRATE_DIGEST, references["migrate"])

    def test_role_order_in_pushed_lists_is_not_significant(self) -> None:
        summary = self.changed(self.summary)
        summary["pushed_roles"] = ["migrate", "app"]
        summary["push_attempted_roles"] = ["migrate", "app"]
        references = self.verify(summary=summary)
        self.assertIn(APP_DIGEST, references["app"])

    def test_summary_v1_unknown_root_and_nested_fields_are_ignored(self) -> None:
        summary = self.changed(self.summary)
        summary["future_summary_field"] = {"nested": [1, 2, 3]}
        summary["push"]["app"]["future_push_field"] = "ignored"
        summary["packages_postcheck"]["migrate"]["future_package_field"] = (
            "ignored"
        )
        self.verify(summary=summary)

    def test_run_metadata_unknown_fields_are_ignored(self) -> None:
        self.verify()

    def test_descriptor_hash_covers_exact_input_bytes(self) -> None:
        descriptor = make_descriptor()
        compact = json.dumps(
            descriptor, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        inputs = make_release_inputs(descriptor_bytes=compact)
        references = verifier.verify_release(**inputs)
        self.assertEqual(inputs["expected_source_sha"], SOURCE_SHA)
        self.assertIn(APP_DIGEST, references["app"])

    def test_function_is_repeatable_and_does_not_mutate_inputs(self) -> None:
        before = {
            key: value
            for key, value in self.inputs.items()
            if isinstance(value, bytes)
        }
        first = verifier.verify_release(**self.inputs)
        second = verifier.verify_release(**self.inputs)
        self.assertEqual(first, second)
        after = {
            key: value
            for key, value in self.inputs.items()
            if isinstance(value, bytes)
        }
        self.assertEqual(before, after)


class InputContractTests(VerifierTestBase):
    def test_non_bytes_input_is_rejected(self) -> None:
        self.assert_error(
            "summary", "type", summary_bytes=self.inputs["summary_bytes"].decode()
        )

    def test_empty_input_is_rejected(self) -> None:
        self.assert_error("descriptor", "empty", descriptor_bytes=b"")

    def test_invalid_utf8_is_rejected(self) -> None:
        self.assert_error("evidence", "encoding", evidence_bytes=b"\xff\xfe")

    def test_non_object_json_is_rejected(self) -> None:
        self.assert_error("run", "object", run_metadata_bytes=b"[]")

    def test_non_standard_nan_is_rejected(self) -> None:
        self.assert_error(
            "summary", "json", summary_bytes=b'{"schema": NaN}'
        )

    def test_duplicate_keys_are_rejected_for_every_input(self) -> None:
        duplicate = b'{"outer": {"x": 1, "x": 2}}'
        duplicate_root = b'{"x": 1, "x": 2}'
        self.assert_error(
            "summary", "duplicate-key", summary_bytes=duplicate_root
        )
        self.assert_error("summary", "duplicate-key", summary_bytes=duplicate)
        self.assert_error(
            "descriptor", "duplicate-key", descriptor_bytes=duplicate
        )
        self.assert_error("evidence", "duplicate-key", evidence_bytes=duplicate)
        self.assert_error(
            "run", "duplicate-key", run_metadata_bytes=duplicate
        )

    def test_oversized_descriptor_is_rejected(self) -> None:
        self.assert_error(
            "descriptor",
            "size",
            descriptor_bytes=b"{" + b" " * verifier.DESCRIPTOR_MAX_BYTES,
        )

    def test_excessive_json_nesting_becomes_a_fixed_category(self) -> None:
        depth = 20_000
        deeply_nested = b'{"x":' + b"[" * depth + b"]" * depth + b"}"
        self.assertLessEqual(
            len(deeply_nested), verifier.DESCRIPTOR_MAX_BYTES
        )
        self.assert_error(
            "descriptor", "json", descriptor_bytes=deeply_nested
        )

    def test_non_positive_or_boolean_expected_values_are_rejected(self) -> None:
        self.assert_error(
            "input",
            "expected-source-sha",
            expected_source_sha="A" * 40,
        )
        self.assert_error(
            "input", "release-run-id", release_run_id=True
        )
        self.assert_error(
            "input", "release-run-id", release_run_id=0
        )
        self.assert_error(
            "input", "release-run-attempt", release_run_attempt=0
        )

    def test_optional_digests_must_be_supplied_as_a_pair(self) -> None:
        self.assert_error(
            "input", "digest-pair", expected_app_digest=APP_DIGEST
        )
        self.assert_error(
            "input", "digest-pair", expected_migrate_digest=MIGRATE_DIGEST
        )

    def test_optional_digests_must_be_canonical(self) -> None:
        self.assert_error(
            "input",
            "expected-app-digest",
            expected_app_digest=APP_DIGEST.split(":", 1)[1],
            expected_migrate_digest=MIGRATE_DIGEST,
        )
        self.assert_error(
            "input",
            "expected-migrate-digest",
            expected_app_digest=APP_DIGEST,
            expected_migrate_digest="sha256:" + "F" * 64,
        )


class EvidenceTests(VerifierTestBase):
    def test_evidence_schema_is_frozen(self) -> None:
        extra = self.changed(self.evidence)
        extra["future"] = True
        self.assert_error("evidence", "keys", evidence=extra)
        missing = self.changed(self.evidence)
        missing.pop("ci_run_attempt")
        self.assert_error("evidence", "keys", evidence=missing)

    def test_evidence_schema_value_repository_and_source(self) -> None:
        self.assert_error(
            "evidence",
            "schema",
            evidence={**self.evidence, "schema": "payment-hub.release-evidence.v2"},
        )
        self.assert_error(
            "evidence",
            "repository",
            evidence={**self.evidence, "repository": "other/repo"},
        )
        self.assert_error(
            "evidence",
            "source",
            evidence={**self.evidence, "source_sha": "b" * 40},
        )

    def test_evidence_numeric_identifiers_are_positive_and_not_bool(self) -> None:
        for field, kind in (
            ("release_run_id", "release-run-id"),
            ("release_run_attempt", "release-run-attempt"),
            ("ci_run_id", "ci-run-id"),
            ("ci_run_attempt", "ci-run-attempt"),
            ("bundle_artifact_id", "bundle-artifact-id"),
        ):
            for bad in (0, -1, True, "1"):
                with self.subTest(field=field, bad=bad):
                    evidence = copy.deepcopy(self.evidence)
                    evidence[field] = bad
                    self.assert_error("evidence", kind, evidence=evidence)

    def test_evidence_digest_fields_are_canonical(self) -> None:
        cases = (
            (
                "bundle_artifact_digest",
                "bundle-artifact-digest",
                (
                    "",
                    "a" * 64,
                    "A" * 64,
                    "sha256:" + "A" * 64,
                    "sha256:" + "a" * 63,
                    "sha256:" + "a" * 65,
                ),
            ),
            (
                "descriptor_sha256",
                "descriptor-sha",
                (
                    "",
                    "sha256:" + "a" * 64,
                    "A" * 64,
                    "a" * 63,
                    "a" * 65,
                ),
            ),
        )
        for field, kind, values in cases:
            for bad in values:
                with self.subTest(field=field, bad=bad):
                    evidence = copy.deepcopy(self.evidence)
                    evidence[field] = bad
                    self.assert_error("evidence", kind, evidence=evidence)

    def test_evidence_run_bindings_must_match_independent_values(self) -> None:
        self.assert_error(
            "evidence",
            "release-run-id",
            evidence={**self.evidence, "release_run_id": RELEASE_RUN_ID + 1},
        )
        self.assert_error(
            "evidence",
            "release-run-attempt",
            evidence={
                **self.evidence,
                "release_run_attempt": RELEASE_RUN_ATTEMPT + 1,
            },
        )

    def test_evidence_descriptor_digest_must_match_actual_bytes(self) -> None:
        self.assert_error(
            "evidence",
            "descriptor-sha",
            evidence={**self.evidence, "descriptor_sha256": "9" * 64},
        )

    def test_evidence_repository_must_match_run_repository(self) -> None:
        self.assert_error(
            "evidence",
            "repository",
            evidence={**self.evidence, "repository": "other/payment-hub"},
        )


class DescriptorTests(VerifierTestBase):
    def test_descriptor_requires_exact_known_keys(self) -> None:
        extra = self.changed(self.descriptor)
        extra["future"] = True
        self.assert_error("descriptor", "keys", descriptor=extra)
        missing = self.changed(self.descriptor)
        missing.pop("tag")
        self.assert_error("descriptor", "keys", descriptor=missing)

    def test_descriptor_schema_source_manifest_and_platform(self) -> None:
        self.assert_error(
            "descriptor",
            "schema",
            descriptor={**self.descriptor, "schema": "payment-hub.release-handoff.v2"},
        )
        other_source = {**self.descriptor, "source_sha": "b" * 40}
        other_source["tag"] = "sha-" + "b" * 40
        self.assert_error("descriptor", "source", descriptor=other_source)
        self.assert_error(
            "descriptor",
            "manifest-digest",
            descriptor={
                **self.descriptor,
                "source_manifest_digest": "sha256:" + MANIFEST_DIGEST,
            },
        )
        self.assert_error(
            "descriptor",
            "platform",
            descriptor={**self.descriptor, "platform": "linux/arm64"},
        )
        self.assert_error(
            "descriptor",
            "utc",
            descriptor={**self.descriptor, "created_utc": ""},
        )

    def test_descriptor_archive_is_strict(self) -> None:
        wrong_file = self.changed(self.descriptor)
        wrong_file["archive"]["file"] = "other.tar"
        self.assert_error("descriptor", "archive", descriptor=wrong_file)
        wrong_sha = self.changed(self.descriptor)
        wrong_sha["archive"]["sha256"] = "sha256:" + ARCHIVE_SHA
        self.assert_error("descriptor", "archive-sha", descriptor=wrong_sha)
        bool_size = self.changed(self.descriptor)
        bool_size["archive"]["size"] = True
        self.assert_error("descriptor", "archive-size", descriptor=bool_size)
        extra = self.changed(self.descriptor)
        extra["archive"]["extra"] = 1
        self.assert_error("descriptor", "archive", descriptor=extra)

    def test_descriptor_builder_is_strict(self) -> None:
        for field, kind in (
            ("build_record_sha256", "builder-sha"),
            ("accepted_report_sha256", "builder-sha"),
        ):
            bad = self.changed(self.descriptor)
            bad["builder"][field] = "canary"
            self.assert_error("descriptor", kind, descriptor=bad)
        wrong_file = self.changed(self.descriptor)
        wrong_file["builder"]["accepted_report_file"] = "other.json"
        self.assert_error(
            "descriptor", "builder-file", descriptor=wrong_file
        )
        missing = self.changed(self.descriptor)
        missing["builder"].pop("accepted_report_file")
        self.assert_error("descriptor", "builder", descriptor=missing)

    def test_descriptor_images_and_pair_are_strict(self) -> None:
        missing_role = self.changed(self.descriptor)
        missing_role["images"].pop("migrate")
        self.assert_error("descriptor", "images", descriptor=missing_role)
        missing_field = self.changed(self.descriptor)
        missing_field["images"]["app"].pop("platform")
        self.assert_error("descriptor", "image", descriptor=missing_field)
        pair = self.changed(self.descriptor)
        pair["images"]["migrate"]["local_id"] = APP_DIGEST
        pair["images"]["migrate"]["root_descriptor"]["digest"] = APP_DIGEST
        self.assert_error("descriptor", "pair", descriptor=pair)

    def test_descriptor_image_identity_is_consistent(self) -> None:
        bad_local = self.changed(self.descriptor)
        bad_local["images"]["app"]["local_id"] = APP_DIGEST.split(":", 1)[1]
        self.assert_error("descriptor", "local-id", descriptor=bad_local)
        bad_root = self.changed(self.descriptor)
        bad_root["images"]["app"]["root_descriptor"]["digest"] = MIGRATE_DIGEST
        self.assert_error("descriptor", "local-id", descriptor=bad_root)
        bad_media = self.changed(self.descriptor)
        bad_media["images"]["app"]["root_descriptor"]["mediaType"] = (
            "application/vnd.oci.image.manifest.v1+json"
        )
        self.assert_error("descriptor", "root", descriptor=bad_media)
        bad_label = self.changed(self.descriptor)
        bad_label["images"]["app"]["source_label"] = ARCHIVE_SHA
        self.assert_error(
            "descriptor", "source-label", descriptor=bad_label
        )
        for bad_reference_value in ("-bad", "bad reference", ""):
            with self.subTest(reference=bad_reference_value):
                bad_reference = copy.deepcopy(self.descriptor)
                bad_reference["images"]["app"][
                    "reference"
                ] = bad_reference_value
                self.assert_error(
                    "descriptor", "reference", descriptor=bad_reference
                )

    def test_descriptor_targets_and_tag_are_fixed(self) -> None:
        bad_targets = self.changed(self.descriptor)
        bad_targets["targets"]["app"] = "ghcr.io/other/app"
        self.assert_error("descriptor", "targets", descriptor=bad_targets)
        bad_tag = self.changed(self.descriptor)
        bad_tag["tag"] = "sha-" + "b" * 40
        self.assert_error("descriptor", "tag", descriptor=bad_tag)


class SummaryGateTests(VerifierTestBase):
    def test_summary_requires_known_gate_fields(self) -> None:
        missing = self.changed(self.summary)
        missing.pop("cleanup_failed")
        self.assert_error("summary", "keys", summary=missing)
        missing = self.changed(self.summary)
        missing.pop("packages_initial")
        self.assert_error("summary", "keys", summary=missing)

    def test_summary_success_and_cleanup_fields(self) -> None:
        cases = (
            ("schema", "wrong", "schema"),
            ("stage", "prepare", "stage"),
            ("outcome", "fail", "outcome"),
            ("published", False, "published"),
            ("published", 1, "published"),
            ("failure", {"stage": "push", "kind": "app-failed"}, "failure"),
            ("credentials_removed", False, "credentials"),
            ("credentials_removed", 1, "credentials"),
            ("cleanup_failed", True, "cleanup"),
            ("cleanup_failed", 0, "cleanup"),
            ("source_sha", "b" * 40, "source"),
            ("platform", "linux/arm64", "platform"),
            ("descriptor_sha256", "9" * 64, "descriptor-sha"),
        )
        for field, value, kind in cases:
            with self.subTest(field=field, value=value):
                summary = copy.deepcopy(self.summary)
                summary[field] = value
                self.assert_error("summary", kind, summary=summary)

    def test_summary_role_lists_must_be_exactly_the_pair(self) -> None:
        for field, kind in (
            ("pushed_roles", "pushed-roles"),
            ("push_attempted_roles", "attempted-roles"),
        ):
            for bad in (
                ["app"],
                ["app", "app"],
                ["app", "migrate", "extra"],
                ["app", "other"],
                "app,migrate",
                ["app", 1],
            ):
                with self.subTest(field=field, bad=bad):
                    summary = copy.deepcopy(self.summary)
                    summary[field] = bad
                    self.assert_error("summary", kind, summary=summary)

    def test_both_roles_must_have_verified_push_side_effects(self) -> None:
        summary = self.changed(self.summary)
        summary["push_side_effects"]["migrate"] = "unknown"
        self.assert_error("summary", "side-effects", summary=summary)
        extra = self.changed(self.summary)
        extra["push_side_effects"]["extra"] = "verified"
        self.assert_error("summary", "roles", summary=extra)

    def test_push_exit_status_and_candidate_digest(self) -> None:
        for bad in (1, -1, True, False, None, "0", 0.0):
            with self.subTest(bad=bad):
                summary = copy.deepcopy(self.summary)
                summary["push"]["app"]["exit"] = bad
                self.assert_error("summary", "push-exit", summary=summary)
        summary = self.changed(self.summary)
        summary["push"]["app"]["candidate_digest"] = MIGRATE_DIGEST
        self.assert_error("summary", "push-digest", summary=summary)
        summary = self.changed(self.summary)
        summary["push"]["app"]["candidate_digest"] = APP_DIGEST.split(":", 1)[1]
        self.assert_error("summary", "push-digest", summary=summary)

    def test_remote_digest_must_match_descriptor_root(self) -> None:
        summary = self.changed(self.summary)
        summary["remote"]["app"]["digest"] = MIGRATE_DIGEST
        self.assert_error("summary", "remote-digest", summary=summary)
        summary = self.changed(self.summary)
        summary["remote"]["app"]["digest"] = "sha256:" + "F" * 64
        self.assert_error("summary", "remote-digest", summary=summary)

    def test_pull_exit_and_id_must_match_the_local_role(self) -> None:
        for bad in (1, -1, True, False, None, "0", 0.0):
            with self.subTest(bad=bad):
                summary = copy.deepcopy(self.summary)
                summary["pull"]["migrate"]["exit"] = bad
                self.assert_error("summary", "pull-exit", summary=summary)
        summary = self.changed(self.summary)
        summary["pull"]["app"]["id"] = MIGRATE_DIGEST
        self.assert_error("summary", "pull-id", summary=summary)
        summary = self.changed(self.summary)
        summary["pull"]["app"]["id"] = APP_DIGEST.split(":", 1)[1]
        self.assert_error("summary", "pull-id", summary=summary)

    def test_package_postcheck_metadata_is_complete_for_both_roles(self) -> None:
        cases = (
            ("name", "other", "package-name"),
            ("visibility", "private", "package-visibility"),
            ("visibility", "internal", "package-visibility"),
            ("repository", "other/repo", "package-repository"),
            ("owner", "someone", "package-owner"),
            ("id", 0, "package-id"),
            ("id", True, "package-id"),
            ("id", "101", "package-id"),
        )
        for field, value, kind in cases:
            for role in verifier.ROLES:
                with self.subTest(role=role, field=field, value=value):
                    summary = copy.deepcopy(self.summary)
                    summary["packages_postcheck"][role][field] = value
                    self.assert_error("summary", kind, summary=summary)

    def test_package_postcheck_must_keep_the_original_package_id(self) -> None:
        summary = self.changed(self.summary)
        summary["packages_initial"]["app"]["id"] = APP_PACKAGE_ID + 1
        self.assert_error("summary", "package-original-id", summary=summary)
        summary = self.changed(self.summary)
        summary["packages_initial"]["app"]["status"] = "ABSENT_CONFIRMED"
        self.assert_error("summary", "package-initial", summary=summary)
        summary = self.changed(self.summary)
        summary["packages_initial"]["app"]["id"] = True
        self.assert_error("summary", "package-initial-id", summary=summary)

    def test_package_names_and_ids_cannot_be_swapped_between_roles(self) -> None:
        summary = self.changed(self.summary)
        summary["packages_postcheck"]["app"]["name"] = verifier.PACKAGE_NAMES[
            "migrate"
        ]
        self.assert_error("summary", "package-name", summary=summary)
        summary = self.changed(self.summary)
        summary["packages_postcheck"]["app"]["id"] = MIGRATE_PACKAGE_ID
        self.assert_error(
            "summary", "package-original-id", summary=summary
        )

    def test_optional_packages_projection_is_checked_against_postcheck(
        self,
    ) -> None:
        # ``packages`` is optional for older v1 summaries.
        summary = self.changed(self.summary)
        summary.pop("packages")
        self.verify(summary=summary)

        summary = self.changed(self.summary)
        summary.pop("packages")
        summary["packages"] = {
            role: {
                key: value
                for key, value in summary["packages_postcheck"][role].items()
                if key != "owner"
            }
            for role in verifier.ROLES
        }
        self.verify(summary=summary)

        cases = (
            ("name", "other", "package-name"),
            ("visibility", "private", "package-visibility"),
            ("repository", "other/repo", "package-repository"),
            ("id", APP_PACKAGE_ID + 1, "packages"),
            ("id", 0, "package-id"),
            ("id", True, "package-id"),
            ("owner", "someone", "package-owner"),
        )
        for field, value, kind in cases:
            with self.subTest(field=field, value=value):
                summary = copy.deepcopy(self.summary)
                summary["packages"]["app"][field] = value
                self.assert_error("summary", kind, summary=summary)

        summary = self.changed(self.summary)
        summary["packages"] = None
        self.assert_error("summary", "roles", summary=summary)
        summary = self.changed(self.summary)
        summary["packages"].pop("migrate")
        self.assert_error("summary", "roles", summary=summary)

    def test_optional_revalidation_hashes_are_validated_when_present(
        self,
    ) -> None:
        summary = self.changed(self.summary)
        summary["revalidation"].pop("checks_sha256")
        summary["revalidation"].pop("delivery_record_sha256")
        self.verify(summary=summary)
        for field, kind in (
            ("checks_sha256", "revalidation-checks-sha"),
            ("delivery_record_sha256", "revalidation-delivery-record-sha"),
        ):
            for bad in ("", "sha256:" + "a" * 64, "A" * 64, "a" * 63):
                with self.subTest(field=field, bad=bad):
                    summary = copy.deepcopy(self.summary)
                    summary["revalidation"][field] = bad
                    self.assert_error("summary", kind, summary=summary)

    def test_revalidation_must_be_a_passing_report(self) -> None:
        for bad in ({"result": "fail"}, {}, {"result": None}, "pass", None):
            with self.subTest(bad=bad):
                summary = copy.deepcopy(self.summary)
                summary["revalidation"] = bad
                self.assert_error("summary", "revalidation", summary=summary)

    def test_package_role_maps_must_contain_only_the_pair(self) -> None:
        for field in (
            "packages_initial",
            "packages_postcheck",
            "push",
            "remote",
            "pull",
        ):
            with self.subTest(field=field):
                summary = self.changed(self.summary)
                summary[field]["extra"] = {}
                self.assert_error("summary", "roles", summary=summary)

    def test_descriptor_digest_mismatch_is_rejected(self) -> None:
        summary = self.changed(self.summary)
        summary["descriptor_sha256"] = "9" * 64
        self.assert_error("summary", "descriptor-sha", summary=summary)
        summary = self.changed(self.summary)
        summary["descriptor_sha256"] = (
            "sha256:" + summary["descriptor_sha256"]
        )
        self.assert_error("summary", "descriptor-sha", summary=summary)
        summary = self.changed(self.summary)
        summary["descriptor_sha256"] = summary["descriptor_sha256"].upper()
        self.assert_error("summary", "descriptor-sha", summary=summary)


class RunMetadataTests(VerifierTestBase):
    def test_run_requires_known_identity_fields(self) -> None:
        run = self.changed(self.run_metadata)
        run.pop("repository")
        self.assert_error("run", "keys", run_metadata=run)
        run = self.changed(self.run_metadata)
        run.pop("conclusion")
        self.assert_error("run", "keys", run_metadata=run)

    def test_run_must_be_the_expected_completed_manual_main_release(self) -> None:
        cases = (
            ("head_branch", "develop", "branch"),
            ("event", "push", "event"),
            ("status", "in_progress", "status"),
            ("status", "queued", "status"),
            ("conclusion", "failure", "conclusion"),
            ("conclusion", None, "conclusion"),
            ("path", ".github/workflows/ci.yml", "workflow"),
            ("path", "", "workflow"),
        )
        for field, value, kind in cases:
            with self.subTest(field=field, value=value):
                run = copy.deepcopy(self.run_metadata)
                run[field] = value
                self.assert_error("run", kind, run_metadata=run)

    def test_run_repository_must_be_trusted(self) -> None:
        run = self.changed(self.run_metadata)
        run["repository"]["full_name"] = "other/payment-hub"
        self.assert_error("run", "repository", run_metadata=run)
        run = self.changed(self.run_metadata)
        run["repository"] = "xtianxx/payment-hub"
        self.assert_error("run", "repository", run_metadata=run)

    def test_run_ids_must_match_independent_values(self) -> None:
        run = self.changed(self.run_metadata)
        run["id"] = RELEASE_RUN_ID + 1
        self.assert_error("run", "id", run_metadata=run)
        run = self.changed(self.run_metadata)
        run["run_attempt"] = RELEASE_RUN_ATTEMPT + 1
        self.assert_error("run", "attempt", run_metadata=run)

    def test_run_sha_must_match_independent_source(self) -> None:
        run = self.changed(self.run_metadata)
        run["head_sha"] = "b" * 40
        self.assert_error("run", "source", run_metadata=run)

    def test_run_numeric_fields_reject_booleans_and_zero(self) -> None:
        for field, kind in (("id", "id"), ("run_attempt", "attempt")):
            for bad in (True, False, 0, -1, "1"):
                with self.subTest(field=field, bad=bad):
                    run = copy.deepcopy(self.run_metadata)
                    run[field] = bad
                    self.assert_error("run", kind, run_metadata=run)

    def test_accepted_release_workflow_path_variants(self) -> None:
        for path in (
            ".github/workflows/release.yml",
            ".github/workflows/release.yml@main",
            ".github/workflows/release.yml@refs/heads/main",
        ):
            with self.subTest(path=path):
                self.verify(
                    run_metadata={**self.run_metadata, "path": path}
                )


class CrossBindingTests(VerifierTestBase):
    def test_source_sha_must_agree_across_all_inputs(self) -> None:
        self.assert_error(
            "summary",
            "source",
            summary={**self.summary, "source_sha": "b" * 40},
        )
        other_descriptor = {**self.descriptor, "source_sha": "b" * 40}
        other_descriptor["tag"] = "sha-" + "b" * 40
        self.assert_error(
            "descriptor", "source", descriptor=other_descriptor
        )
        self.assert_error(
            "evidence",
            "source",
            evidence={**self.evidence, "source_sha": "b" * 40},
        )
        self.assert_error(
            "run",
            "source",
            run_metadata={**self.run_metadata, "head_sha": "b" * 40},
        )
        self.assert_error(
            "summary",
            "source",
            expected_source_sha="b" * 40,
        )

    def test_independent_run_lock_must_match_evidence_and_run(self) -> None:
        self.assert_error(
            "run", "id", release_run_id=RELEASE_RUN_ID + 1
        )
        self.assert_error(
            "run", "attempt", release_run_attempt=RELEASE_RUN_ATTEMPT + 1
        )

    def test_swapped_expected_digests_are_rejected_by_role(self) -> None:
        self.assert_error(
            "binding",
            "app-digest",
            expected_app_digest=MIGRATE_DIGEST,
            expected_migrate_digest=MIGRATE_DIGEST,
        )
        self.assert_error(
            "binding",
            "migrate-digest",
            expected_app_digest=APP_DIGEST,
            expected_migrate_digest=APP_DIGEST,
        )

    def test_swapped_role_evidence_is_rejected(self) -> None:
        summary = self.changed(self.summary)
        summary["push"]["app"]["candidate_digest"] = MIGRATE_DIGEST
        summary["remote"]["app"]["digest"] = MIGRATE_DIGEST
        summary["pull"]["app"]["id"] = MIGRATE_DIGEST
        self.assert_error("summary", "push-digest", summary=summary)

    def test_evidence_run_id_cannot_be_borrowed_from_another_attempt(self) -> None:
        self.assert_error(
            "evidence",
            "release-run-id",
            evidence={**self.evidence, "release_run_id": 1},
        )
        self.assert_error(
            "evidence",
            "release-run-attempt",
            evidence={**self.evidence, "release_run_attempt": 9},
        )

    def test_two_roles_cannot_share_one_handoff_identity(self) -> None:
        descriptor = self.changed(self.descriptor)
        descriptor["images"]["migrate"]["local_id"] = APP_DIGEST
        descriptor["images"]["migrate"]["root_descriptor"]["digest"] = APP_DIGEST
        self.assert_error("descriptor", "pair", descriptor=descriptor)


class SanitizationTests(VerifierTestBase):
    def test_errors_do_not_echo_attacker_controlled_values(self) -> None:
        canary = "CANARY-sensitive-value"
        with self.assertRaises(verifier.VerificationError) as caught:
            self.verify(
                summary={**self.summary, "source_sha": canary}
            )
        rendered = str(caught.exception)
        self.assertEqual(rendered, "summary/source")
        self.assertNotIn(canary, rendered)
        self.assertNotIn(SOURCE_SHA, rendered)

    def test_unknown_descriptor_key_is_not_echoed(self) -> None:
        descriptor = self.changed(self.descriptor)
        descriptor["CANARY-sensitive-key"] = True
        with self.assertRaises(verifier.VerificationError) as caught:
            self.verify(descriptor=descriptor)
        self.assertEqual(str(caught.exception), "descriptor/keys")
        self.assertNotIn("CANARY", str(caught.exception))


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inputs = make_release_inputs()

    def _write_inputs(self, root: pathlib.Path) -> dict[str, pathlib.Path]:
        paths = {
            "summary": root / "release-summary.json",
            "descriptor": root / "descriptor.json",
            "evidence": root / "release-evidence.json",
            "run_metadata": root / "run.json",
        }
        paths["summary"].write_bytes(self.inputs["summary_bytes"])
        paths["descriptor"].write_bytes(self.inputs["descriptor_bytes"])
        paths["evidence"].write_bytes(self.inputs["evidence_bytes"])
        paths["run_metadata"].write_bytes(self.inputs["run_metadata_bytes"])
        return paths

    def _command(self, paths: dict[str, pathlib.Path]) -> list[str]:
        command = [
            sys.executable,
            "-B",
            str(VERIFIER_PATH),
            "--summary",
            str(paths["summary"]),
            "--descriptor",
            str(paths["descriptor"]),
            "--evidence",
            str(paths["evidence"]),
            "--run-metadata",
            str(paths["run_metadata"]),
            "--expected-source-sha",
            self.inputs["expected_source_sha"],
            "--release-run-id",
            str(self.inputs["release_run_id"]),
            "--release-run-attempt",
            str(self.inputs["release_run_attempt"]),
        ]
        return command

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_cli_success_reports_offline_mode_and_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._write_inputs(pathlib.Path(directory))
            result = self._run(self._command(paths))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mode=offline\n", result.stdout)
        self.assertIn(
            "app=" + verifier.TARGETS["app"] + "@" + APP_DIGEST,
            result.stdout,
        )
        self.assertIn(
            "migrate="
            + verifier.TARGETS["migrate"]
            + "@"
            + MIGRATE_DIGEST,
            result.stdout,
        )
        self.assertIn("not a GitHub source verification", result.stdout)
        self.assertIn("currently pullable from GHCR", result.stdout)
        self.assertIn("not deployment authorization", result.stdout)
        self.assertNotIn(SOURCE_SHA, result.stdout)
        self.assertEqual(result.stderr, "")

    def test_cli_accepts_matching_independent_digest_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._write_inputs(pathlib.Path(directory))
            command = self._command(paths) + [
                "--expected-app-digest",
                APP_DIGEST,
                "--expected-migrate-digest",
                MIGRATE_DIGEST,
            ]
            result = self._run(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mode=offline", result.stdout)

    def test_cli_failure_is_sanitized_and_prints_no_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            paths = self._write_inputs(root)
            summary = json.loads(self.inputs["summary_bytes"])
            summary["published"] = False
            paths["summary"].write_bytes(json_bytes(summary))
            result = self._run(self._command(paths))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr,
            "release-verification-failed: summary/published\n",
        )
        self.assertNotIn("sha256:", result.stderr)

    def test_cli_missing_file_failure_does_not_echo_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            paths = self._write_inputs(root)
            paths["evidence"] = root / "DO-NOT-ECHO"
            result = self._run(self._command(paths))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr,
            "release-verification-failed: input/evidence-file\n",
        )
        self.assertNotIn("DO-NOT-ECHO", result.stderr)

    def test_cli_requires_all_four_files_and_identity_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._write_inputs(pathlib.Path(directory))
            command = self._command(paths)
            index = command.index("--evidence")
            del command[index : index + 2]
            result = self._run(command)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            result.stderr, "release-verification-failed: cli/usage\n"
        )
        self.assertEqual(result.stdout, "")

    def test_cli_rejects_a_single_optional_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._write_inputs(pathlib.Path(directory))
            command = self._command(paths) + [
                "--expected-app-digest",
                APP_DIGEST,
            ]
            result = self._run(command)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr,
            "release-verification-failed: input/digest-pair\n",
        )
        self.assertEqual(result.stdout, "")

    def test_cli_rejects_wrong_expected_digest_for_the_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._write_inputs(pathlib.Path(directory))
            command = self._command(paths) + [
                "--expected-app-digest",
                MIGRATE_DIGEST,
                "--expected-migrate-digest",
                MIGRATE_DIGEST,
            ]
            result = self._run(command)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr,
            "release-verification-failed: binding/app-digest\n",
        )
        self.assertEqual(result.stdout, "")

    def test_cli_rejects_non_canonical_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._write_inputs(pathlib.Path(directory))
            command = self._command(paths)
            index = command.index("--release-run-id")
            command[index + 1] = "0"
            result = self._run(command)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr,
            "release-verification-failed: input/release-run-id\n",
        )
        self.assertEqual(result.stdout, "")


class OnlineCliTests(unittest.TestCase):
    TOKEN = "ghp_online_FAKE_TOKEN_not_real"

    def setUp(self) -> None:
        self.inputs = make_release_inputs()

    def _references(
        self, app: str = APP_DIGEST, migrate: str = MIGRATE_DIGEST
    ) -> dict[str, str]:
        return {
            "app": verifier.TARGETS["app"] + "@" + app,
            "migrate": verifier.TARGETS["migrate"] + "@" + migrate,
        }

    def _module(
        self, callback: Any | None = None
    ) -> tuple[types.SimpleNamespace, list[dict[str, Any]]]:
        calls: list[dict[str, Any]] = []

        def verify_online(**kwargs: Any) -> Any:
            calls.append(kwargs)
            if callback is None:
                return self._references()
            return callback(**kwargs)

        return types.SimpleNamespace(verify_online=verify_online), calls

    def _main(
        self,
        argv: list[str],
        *,
        module: Any,
        env: dict[str, str],
    ) -> tuple[int, str, str, mock.Mock]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        loader = mock.Mock(return_value=module)
        with mock.patch.object(
            verifier, "_load_online_module", loader
        ), mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(
            stdout
        ), contextlib.redirect_stderr(stderr):
            code = verifier.main(argv)
        return code, stdout.getvalue(), stderr.getvalue(), loader

    def _online_args(self, *extra: str) -> list[str]:
        return [
            "--online",
            "--expected-source-sha",
            SOURCE_SHA,
            "--release-run-id",
            str(RELEASE_RUN_ID),
            "--release-run-attempt",
            str(RELEASE_RUN_ATTEMPT),
            *extra,
        ]

    def test_online_success_prefers_gh_token_and_outputs_mode(self) -> None:
        module, calls = self._module()
        code, out, err, _loader = self._main(
            self._online_args(),
            module=module,
            env={"GH_TOKEN": "gh-wins", "GITHUB_TOKEN": "github-fallback"},
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertIn("mode=online\n", out)
        self.assertIn(
            "app=" + verifier.TARGETS["app"] + "@" + APP_DIGEST, out
        )
        self.assertIn(
            "migrate="
            + verifier.TARGETS["migrate"]
            + "@"
            + MIGRATE_DIGEST,
            out,
        )
        self.assertIn("GitHub source verified", out)
        self.assertIn("currently pullable", out)
        self.assertIn("not deployment authorization", out)
        self.assertNotIn("gh-wins", out)
        self.assertNotIn("github-fallback", out)
        self.assertNotIn(SOURCE_SHA, out)
        self.assertNotIn(str(RELEASE_RUN_ID), out)
        self.assertEqual(
            calls,
            [
                {
                    "token": "gh-wins",
                    "expected_source_sha": SOURCE_SHA,
                    "release_run_id": RELEASE_RUN_ID,
                    "release_run_attempt": RELEASE_RUN_ATTEMPT,
                    "expected_app_digest": None,
                    "expected_migrate_digest": None,
                }
            ],
        )

    def test_online_falls_back_to_github_token(self) -> None:
        module, calls = self._module()
        code, _out, err, _loader = self._main(
            self._online_args(),
            module=module,
            env={"GITHUB_TOKEN": "github-fallback"},
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[0]["token"], "github-fallback")

    def test_online_cli_uses_fake_source_from_sys_modules(self) -> None:
        module, calls = self._module()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(
            sys.modules,
            {verifier.ONLINE_SOURCE_MODULE_NAME: module},
        ), mock.patch.dict(
            os.environ, {"GH_TOKEN": self.TOKEN}, clear=True
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
            stderr
        ):
            code = verifier.main(self._online_args())
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn("mode=online\n", stdout.getvalue())
        self.assertEqual(len(calls), 1)

    def test_online_passes_paired_expected_digests(self) -> None:
        module, calls = self._module()
        code, _out, err, _loader = self._main(
            self._online_args(
                "--expected-app-digest",
                APP_DIGEST,
                "--expected-migrate-digest",
                MIGRATE_DIGEST,
            ),
            module=module,
            env={"GH_TOKEN": self.TOKEN},
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[0]["expected_app_digest"], APP_DIGEST)
        self.assertEqual(
            calls[0]["expected_migrate_digest"], MIGRATE_DIGEST
        )

    def test_online_missing_token_is_fixed_and_does_not_load_source(
        self,
    ) -> None:
        module, calls = self._module()
        code, out, err, loader = self._main(
            self._online_args(), module=module, env={}
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(
            err, "release-verification-failed: input/token\n"
        )
        self.assertEqual(calls, [])
        loader.assert_not_called()

    def test_online_validates_expected_values_before_token_or_source(
        self,
    ) -> None:
        cases = (
            (
                [
                    "--online",
                    "--expected-source-sha",
                    "A" * 40,
                    "--release-run-id",
                    str(RELEASE_RUN_ID),
                    "--release-run-attempt",
                    str(RELEASE_RUN_ATTEMPT),
                ],
                "input/expected-source-sha",
            ),
            (
                self._online_args(
                    "--expected-app-digest", APP_DIGEST
                ),
                "input/digest-pair",
            ),
            (
                [
                    "--online",
                    "--expected-source-sha",
                    SOURCE_SHA,
                    "--release-run-id",
                    "0",
                    "--release-run-attempt",
                    str(RELEASE_RUN_ATTEMPT),
                ],
                "input/release-run-id",
            ),
        )
        for argv, category in cases:
            with self.subTest(category=category):
                module, calls = self._module()
                code, out, err, loader = self._main(
                    argv, module=module, env={}
                )
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertEqual(
                    err,
                    "release-verification-failed: "
                    + category
                    + "\n",
                )
                self.assertEqual(calls, [])
                loader.assert_not_called()

    def test_online_does_not_accept_a_command_line_token(self) -> None:
        module, calls = self._module()
        code, out, err, loader = self._main(
            self._online_args("--token", self.TOKEN),
            module=module,
            env={"GH_TOKEN": self.TOKEN},
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(
            err, "release-verification-failed: cli/usage\n"
        )
        self.assertEqual(calls, [])
        loader.assert_not_called()

    def test_online_conflicts_with_any_local_file_argument(self) -> None:
        for flag in ("--summary", "--descriptor", "--evidence", "--run-metadata"):
            with self.subTest(flag=flag):
                module, calls = self._module()
                code, out, err, loader = self._main(
                    self._online_args(flag, "unused.json"),
                    module=module,
                    env={"GH_TOKEN": self.TOKEN},
                )
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertEqual(
                    err, "release-verification-failed: cli/usage\n"
                )
                self.assertEqual(calls, [])
                loader.assert_not_called()

    def test_online_source_failure_is_fixed_and_sanitized(self) -> None:
        class ExternalFailure(RuntimeError):
            stage = "evil\nstage"
            kind = "evil\nkind"

        def fail(**_kwargs: Any) -> Any:
            raise ExternalFailure(
                "token=" + self.TOKEN + "\n::error::injected"
            )

        module = types.SimpleNamespace(verify_online=fail)
        code, out, err, _loader = self._main(
            self._online_args(),
            module=module,
            env={"GH_TOKEN": self.TOKEN},
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(
            err, "release-verification-failed: source/verification\n"
        )
        self.assertNotIn(self.TOKEN, err)
        self.assertNotIn("evil", err)

    def test_online_rejects_invalid_source_results_without_refs(self) -> None:
        valid = self._references()
        bad_results = (
            None,
            [],
            {**valid, "extra": valid["app"]},
            {"app": valid["app"]},
            {**valid, "app": valid["migrate"]},
            {**valid, "app": verifier.TARGETS["migrate"] + "@" + APP_DIGEST},
            {**valid, "app": verifier.TARGETS["app"] + "@sha256:" + "A" * 64},
            {**valid, "app": verifier.TARGETS["app"] + ":tag"},
            {**valid, "app": 1},
            self._references(app=APP_DIGEST, migrate=APP_DIGEST),
        )
        for result in bad_results:
            with self.subTest(result=result):
                module, calls = self._module(
                    lambda **_kwargs: result
                )
                code, out, err, _loader = self._main(
                    self._online_args(),
                    module=module,
                    env={"GH_TOKEN": self.TOKEN},
                )
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertEqual(
                    err,
                    "release-verification-failed: source/verification\n",
                )
                self.assertEqual(len(calls), 1)

    def test_online_expected_digest_mismatch_is_rejected(self) -> None:
        module, calls = self._module()
        code, out, err, _loader = self._main(
            self._online_args(
                "--expected-app-digest",
                "sha256:" + "9" * 64,
                "--expected-migrate-digest",
                MIGRATE_DIGEST,
            ),
            module=module,
            env={"GH_TOKEN": self.TOKEN},
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(
            err, "release-verification-failed: source/verification\n"
        )
        self.assertEqual(len(calls), 1)

    def test_offline_path_does_not_load_online_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            paths = {
                "summary": root / "summary.json",
                "descriptor": root / "descriptor.json",
                "evidence": root / "evidence.json",
                "run_metadata": root / "run.json",
            }
            paths["summary"].write_bytes(self.inputs["summary_bytes"])
            paths["descriptor"].write_bytes(self.inputs["descriptor_bytes"])
            paths["evidence"].write_bytes(self.inputs["evidence_bytes"])
            paths["run_metadata"].write_bytes(
                self.inputs["run_metadata_bytes"]
            )
            argv = [
                "--summary",
                str(paths["summary"]),
                "--descriptor",
                str(paths["descriptor"]),
                "--evidence",
                str(paths["evidence"]),
                "--run-metadata",
                str(paths["run_metadata"]),
                "--expected-source-sha",
                SOURCE_SHA,
                "--release-run-id",
                str(RELEASE_RUN_ID),
                "--release-run-attempt",
                str(RELEASE_RUN_ATTEMPT),
            ]
            module, calls = self._module()
            code, out, err, loader = self._main(
                argv, module=module, env={"GH_TOKEN": self.TOKEN}
            )
        self.assertEqual(code, 0, err)
        self.assertIn("mode=offline\n", out)
        self.assertEqual(calls, [])
        loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
