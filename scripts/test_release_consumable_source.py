#!/usr/bin/env python3
"""Focused tests for the online release-consumable source collector.

The tests use only fake GitHub API and fake storage transports.  They make no
network, Docker, GHCR, or filesystem-write calls.  The normal fixture reuses
the public ``make_release_inputs`` helper from the offline verifier tests so
the three archived documents and run metadata stay mutually consistent.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import pathlib
import signal
import socket
import ssl
import stat
import sys
import threading
import time
import types
import unittest
import warnings
import zipfile
from typing import Any
from unittest import mock

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent


def _load_script(module_name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        module_name, SCRIPT_DIR / filename
    )
    if spec is None or spec.loader is None:
        raise SystemExit(f"{filename} is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


source = _load_script(
    "test_release_consumable_source_under_test",
    "release_consumable_source.py",
)
helper = _load_script(
    "test_release_consumable_fixture_helpers",
    "test_verify_release_consumable.py",
)
offline = helper.verifier

SOURCE_SHA = helper.SOURCE_SHA
RELEASE_RUN_ID = helper.RELEASE_RUN_ID
RELEASE_RUN_ATTEMPT = helper.RELEASE_RUN_ATTEMPT
CI_RUN_ID = helper.CI_RUN_ID
CI_RUN_ATTEMPT = helper.CI_RUN_ATTEMPT
BUNDLE_ARTIFACT_ID = helper.BUNDLE_ARTIFACT_ID
BUNDLE_ARTIFACT_DIGEST = helper.BUNDLE_ARTIFACT_DIGEST
APP_PACKAGE_ID = helper.APP_PACKAGE_ID
MIGRATE_PACKAGE_ID = helper.MIGRATE_PACKAGE_ID

RELEASE_WORKFLOW_ID = 4242
CI_WORKFLOW_ID = 4343
REPOSITORY_ID = 1234
RELEASE_ARTIFACT_ID = 901
TOKEN = "test-token-1234"

API_BASE = source.ci.API_BASE
RELEASE_WORKFLOW_URL = (
    f"{API_BASE}/repos/{source.REPOSITORY}/actions/workflows/release.yml"
)
CI_WORKFLOW_URL = (
    f"{API_BASE}/repos/{source.REPOSITORY}/actions/workflows/ci.yml"
)
RELEASE_RUN_URL = (
    f"{API_BASE}/repos/{source.REPOSITORY}/actions/runs/"
    f"{RELEASE_RUN_ID}/attempts/{RELEASE_RUN_ATTEMPT}"
)
CI_RUN_URL = (
    f"{API_BASE}/repos/{source.REPOSITORY}/actions/runs/"
    f"{CI_RUN_ID}/attempts/{CI_RUN_ATTEMPT}"
)
RELEASE_ARTIFACTS_ENDPOINT = (
    f"{API_BASE}/repos/{source.REPOSITORY}/actions/runs/"
    f"{RELEASE_RUN_ID}/artifacts"
)
CI_ARTIFACTS_ENDPOINT = (
    f"{API_BASE}/repos/{source.REPOSITORY}/actions/runs/"
    f"{CI_RUN_ID}/artifacts"
)
RELEASE_ARTIFACTS_URL = RELEASE_ARTIFACTS_ENDPOINT + "?per_page=100&page=1"
CI_ARTIFACTS_URL = CI_ARTIFACTS_ENDPOINT + "?per_page=100&page=1"
SIGNED_URL = (
    "https://productionresultssa1.blob.core.windows.net/actions-results/"
    "run/artifact.zip?sv=2024-01-01&sig=secret-signature"
)


def json_response(
    payload: Any,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> Any:
    response_headers = {"content-type": "application/json; charset=utf-8"}
    if headers:
        response_headers.update(headers)
    return source.ci.HttpResponse(
        status=status,
        headers=response_headers,
        body=json.dumps(payload).encode("utf-8"),
    )


def bytes_response(
    body: bytes,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> Any:
    response_headers = {
        "content-type": "application/zip",
        "content-length": str(len(body)),
    }
    if headers is not None:
        response_headers = dict(headers)
    return source.ci.HttpResponse(
        status=status, headers=response_headers, body=body
    )


def artifacts_response(
    artifacts: list[dict[str, Any]],
    *,
    total_count: int | None = None,
    link: str | None = None,
) -> Any:
    headers = {}
    if link is not None:
        headers["link"] = f'<{link}>; rel="next"'
    return json_response(
        {
            "total_count": (
                len(artifacts) if total_count is None else total_count
            ),
            "artifacts": artifacts,
        },
        headers=headers,
    )


class FakeApiTransport:
    """Fake api.github.com transport that records every call."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = dict(routes)
        self.calls: list[tuple[str, str, float]] = []
        self.failure: Any = None
        self.on_get: Any = None

    def get(self, url: str, token: str, timeout: float) -> Any:
        self.calls.append((url, token, timeout))
        if not url.startswith(API_BASE + "/"):
            raise AssertionError("token-bearing request escaped api.github.com")
        if self.on_get is not None:
            self.on_get(url)
        if self.failure is not None:
            raise self.failure
        try:
            response = self.routes[url]
        except KeyError:
            raise AssertionError("unexpected API URL: " + url) from None
        return response() if callable(response) else response

    def urls(self) -> list[str]:
        return [url for url, _token, _timeout in self.calls]


class FakeDownloadTransport:
    """Fake signed-storage transport; its signature has no token argument."""

    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[tuple[str, float]] = []
        self.failure: Any = None

    def get(self, url: str, timeout: float) -> Any:
        self.calls.append((url, timeout))
        if "api.github.com" in url:
            raise AssertionError("signed transport received the API host")
        if self.failure is not None:
            raise self.failure
        response = self.response
        return response() if callable(response) else response


def zip_files(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def mark_zip_encrypted(data: bytes) -> bytes:
    buffer = bytearray(data)
    index = 0
    while True:
        index = buffer.find(b"PK\x01\x02", index)
        if index < 0:
            break
        flags = int.from_bytes(buffer[index + 8 : index + 10], "little")
        buffer[index + 8 : index + 10] = (flags | 0x01).to_bytes(
            2, "little"
        )
        index += 4
    return bytes(buffer)


def make_release_artifact(
    zip_bytes: bytes,
    *,
    artifact_id: int = RELEASE_ARTIFACT_ID,
    name: str | None = None,
    size: int | None = None,
    digest: str | None = None,
    expired: Any = False,
    workflow_run: Any = "default",
) -> dict[str, Any]:
    selected_size = len(zip_bytes) if size is None else size
    selected_digest = (
        "sha256:" + hashlib.sha256(zip_bytes).hexdigest()
        if digest is None
        else digest
    )
    selected_workflow_run = (
        {
            "id": RELEASE_RUN_ID,
            "workflow_id": RELEASE_WORKFLOW_ID,
            "head_sha": SOURCE_SHA,
            "head_branch": "main",
            "event": "workflow_dispatch",
            "repository_id": REPOSITORY_ID,
            "head_repository_id": REPOSITORY_ID,
        }
        if workflow_run == "default"
        else workflow_run
    )
    artifact = {
        "id": artifact_id,
        "name": name
        if name is not None
        else f"release-summary-{RELEASE_RUN_ID}-{RELEASE_RUN_ATTEMPT}",
        "size_in_bytes": selected_size,
        "expired": expired,
        "digest": selected_digest,
        # Deliberately hostile: the collector must construct the API path
        # from the artifact ID and ignore these URLs.
        "archive_download_url": "https://attacker.invalid/never-download",
        "url": "https://attacker.invalid/never-fetch",
    }
    if selected_workflow_run is not None:
        artifact["workflow_run"] = selected_workflow_run
    return artifact


def make_ci_artifact(
    *,
    artifact_id: int = BUNDLE_ARTIFACT_ID,
    name: str | None = None,
    size: int = 50 * 1024 * 1024,
    digest: str = BUNDLE_ARTIFACT_DIGEST,
    expired: Any = False,
    workflow_run: Any = "default",
) -> dict[str, Any]:
    selected_workflow_run = (
        {
            "id": CI_RUN_ID,
            "workflow_id": CI_WORKFLOW_ID,
            "head_sha": SOURCE_SHA,
            "head_branch": "main",
            "event": "push",
            "repository_id": REPOSITORY_ID,
            "head_repository_id": REPOSITORY_ID,
        }
        if workflow_run == "default"
        else workflow_run
    )
    artifact = {
        "id": artifact_id,
        "name": name
        if name is not None
        else f"ci-release-bundle-{CI_RUN_ID}-{CI_RUN_ATTEMPT}",
        "size_in_bytes": size,
        "expired": expired,
        "digest": digest,
        "archive_download_url": "https://attacker.invalid/never-download",
        "url": "https://attacker.invalid/never-fetch",
    }
    if selected_workflow_run is not None:
        artifact["workflow_run"] = selected_workflow_run
    return artifact


class OnlineFixture:
    def __init__(self) -> None:
        self.inputs = helper.make_release_inputs()
        self.summary_bytes = self.inputs["summary_bytes"]
        self.descriptor_bytes = self.inputs["descriptor_bytes"]
        self.evidence_bytes = self.inputs["evidence_bytes"]
        self.run_metadata_bytes = self.inputs["run_metadata_bytes"]
        self.zip_bytes = zip_files(
            {
                "release-summary.json": self.summary_bytes,
                "descriptor.json": self.descriptor_bytes,
                "release-evidence.json": self.evidence_bytes,
            }
        )
        self.release_workflow = {
            "id": RELEASE_WORKFLOW_ID,
            "path": source.RELEASE_WORKFLOW_PATH,
            "state": "active",
        }
        self.ci_workflow = {
            "id": CI_WORKFLOW_ID,
            "path": source.CI_WORKFLOW_PATH,
            "state": "active",
        }
        self.release_run = helper.make_run_metadata()
        self.ci_run = helper.make_run_metadata(
            source_sha=SOURCE_SHA,
            run_id=CI_RUN_ID,
            run_attempt=CI_RUN_ATTEMPT,
            workflow_id=CI_WORKFLOW_ID,
            event="push",
            path=source.CI_WORKFLOW_PATH,
        )
        self.release_artifact = make_release_artifact(self.zip_bytes)
        self.ci_artifact = make_ci_artifact()
        self.release_artifacts = [
            self.release_artifact,
            {
                "id": 899,
                "name": f"release-summary-{RELEASE_RUN_ID}-1",
            },
        ]
        self.ci_artifacts = [
            self.ci_artifact,
            {
                "id": 556,
                "name": f"release-candidate-pointer-{CI_RUN_ID}",
            },
        ]
        self.signed_url = SIGNED_URL
        self.api = FakeApiTransport(self._routes())
        self.download = FakeDownloadTransport(bytes_response(self.zip_bytes))

    @property
    def zip_api_url(self) -> str:
        return (
            f"{API_BASE}/repos/{source.REPOSITORY}/actions/artifacts/"
            f"{self.release_artifact['id']}/zip"
        )

    def _routes(self) -> dict[str, Any]:
        return {
            RELEASE_WORKFLOW_URL: json_response(self.release_workflow),
            RELEASE_RUN_URL: json_response(self.release_run),
            RELEASE_ARTIFACTS_URL: artifacts_response(
                self.release_artifacts
            ),
            self.zip_api_url: source.ci.HttpResponse(
                status=302,
                headers={"location": self.signed_url},
                body=b"",
            ),
            CI_WORKFLOW_URL: json_response(self.ci_workflow),
            CI_RUN_URL: json_response(self.ci_run),
            CI_ARTIFACTS_URL: artifacts_response(self.ci_artifacts),
        }

    def set_release_artifacts(
        self, artifacts: list[dict[str, Any]], **kwargs: Any
    ) -> None:
        self.api.routes[RELEASE_ARTIFACTS_URL] = artifacts_response(
            artifacts, **kwargs
        )

    def set_ci_artifacts(
        self, artifacts: list[dict[str, Any]], **kwargs: Any
    ) -> None:
        self.api.routes[CI_ARTIFACTS_URL] = artifacts_response(
            artifacts, **kwargs
        )


class SourceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = OnlineFixture()

    def verify(self, **overrides: Any) -> dict[str, str]:
        kwargs: dict[str, Any] = {
            "token": TOKEN,
            "expected_source_sha": SOURCE_SHA,
            "release_run_id": RELEASE_RUN_ID,
            "release_run_attempt": RELEASE_RUN_ATTEMPT,
            "api_transport": self.fixture.api,
            "download_transport": self.fixture.download,
            "verifier": offline,
        }
        kwargs.update(overrides)
        return source.verify_online(**kwargs)

    def assert_error(
        self, stage: str, kind: str, **overrides: Any
    ) -> Any:
        with self.assertRaises(source.VerificationError) as caught:
            self.verify(**overrides)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind), (stage, kind)
        )
        self.assertEqual(str(caught.exception), f"{stage}/{kind}")
        return caught.exception

    def descriptor(self) -> dict[str, Any]:
        return json.loads(self.fixture.descriptor_bytes)


class ValidOnlineTests(SourceTestBase):
    def test_valid_release_returns_fixed_paired_references(self) -> None:
        references = self.verify()
        expected = offline.verify_release(**self.fixture.inputs)
        self.assertEqual(references, expected)
        self.assertEqual(set(references), {"app", "migrate"})
        for role in ("app", "migrate"):
            self.assertTrue(references[role].startswith(source.TARGETS[role] + "@"))

    def test_default_verifier_is_loaded_without_calling_main(self) -> None:
        references = self.verify(verifier=None)
        expected = offline.verify_release(**self.fixture.inputs)
        self.assertEqual(references, expected)
        loaded = source._default_verifier()
        self.assertTrue(callable(getattr(loaded, "verify_release", None)))
        self.assertNotEqual(loaded.__name__, "__main__")

    def test_optional_expected_digests_match_own_roles(self) -> None:
        descriptor = self.descriptor()
        expected_app = descriptor["images"]["app"]["root_descriptor"]["digest"]
        expected_migrate = descriptor["images"]["migrate"]["root_descriptor"][
            "digest"
        ]
        references = self.verify(
            expected_app_digest=expected_app,
            expected_migrate_digest=expected_migrate,
        )
        self.assertEqual(
            references["app"], source.TARGETS["app"] + "@" + expected_app
        )
        self.assertEqual(
            references["migrate"],
            source.TARGETS["migrate"] + "@" + expected_migrate,
        )

    def test_swapped_expected_digests_are_rejected_by_role(self) -> None:
        descriptor = self.descriptor()
        expected_app = descriptor["images"]["app"]["root_descriptor"]["digest"]
        expected_migrate = descriptor["images"]["migrate"]["root_descriptor"][
            "digest"
        ]
        self.assert_error(
            "binding",
            "app-digest",
            expected_app_digest=expected_migrate,
            expected_migrate_digest=expected_app,
        )

    def test_exact_zip_endpoint_and_signed_download_are_used(self) -> None:
        self.verify()
        urls = self.fixture.api.urls()
        self.assertIn(self.fixture.zip_api_url, urls)
        self.assertEqual(
            [call[0] for call in self.fixture.download.calls],
            [self.fixture.signed_url],
        )
        joined = "\n".join(urls + [call[0] for call in self.fixture.download.calls])
        self.assertNotIn("attacker.invalid", joined)
        self.assertNotIn("ghcr.io", joined)
        self.assertFalse(any("archive_download_url" in url for url in urls))

    def test_token_goes_only_to_api_requests(self) -> None:
        self.verify()
        self.assertTrue(self.fixture.api.calls)
        for _url, token, timeout in self.fixture.api.calls:
            self.assertEqual(token, TOKEN)
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, source.REQUEST_TIMEOUT_SECONDS)
        # FakeDownloadTransport.get only accepts (url, timeout); a token would
        # either be absent from its signature or a test failure.
        for _url, timeout in self.fixture.download.calls:
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, source.REQUEST_TIMEOUT_SECONDS)

    def test_no_workflow_runs_latest_lookup_is_performed(self) -> None:
        self.verify()
        self.assertFalse(
            any("/workflows/" in url and "/runs?" in url for url in self.fixture.api.urls())
        )
        self.assertTrue(
            any(url == RELEASE_RUN_URL for url in self.fixture.api.urls())
        )
        self.assertTrue(
            any(url == CI_RUN_URL for url in self.fixture.api.urls())
        )

    def test_ci_bundle_is_never_downloaded(self) -> None:
        self.verify()
        self.assertEqual(len(self.fixture.download.calls), 1)
        self.assertEqual(
            self.fixture.download.calls[0][0], self.fixture.signed_url
        )


class InputContractTests(SourceTestBase):
    def test_bad_source_sha_fails_before_network(self) -> None:
        self.assert_error(
            "input", "expected-source-sha", expected_source_sha="A" * 40
        )
        self.assertEqual(self.fixture.api.calls, [])

    def test_bool_run_id_is_rejected(self) -> None:
        self.assert_error("input", "release-run-id", release_run_id=True)

    def test_zero_attempt_is_rejected(self) -> None:
        self.assert_error(
            "input", "release-run-attempt", release_run_attempt=0
        )

    def test_string_run_id_is_rejected(self) -> None:
        self.assert_error(
            "input", "release-run-id", release_run_id=str(RELEASE_RUN_ID)
        )

    def test_digest_pair_is_required(self) -> None:
        self.assert_error(
            "input",
            "digest-pair",
            expected_app_digest="sha256:" + "1" * 64,
        )

    def test_bad_expected_digest_is_rejected(self) -> None:
        self.assert_error(
            "input",
            "expected-app-digest",
            expected_app_digest="sha256:short",
            expected_migrate_digest="sha256:" + "2" * 64,
        )

    def test_bad_token_is_rejected_before_network(self) -> None:
        self.assert_error("input", "token", token="short")
        self.assertEqual(self.fixture.api.calls, [])

    def test_empty_token_is_rejected(self) -> None:
        self.assert_error("input", "token", token="")


class ReleaseWorkflowRunTests(SourceTestBase):
    def _set_release_workflow(self, **overrides: Any) -> None:
        workflow = copy.deepcopy(self.fixture.release_workflow)
        workflow.update(overrides)
        self.fixture.api.routes[RELEASE_WORKFLOW_URL] = json_response(workflow)

    def _set_release_run(self, **overrides: Any) -> None:
        run = copy.deepcopy(self.fixture.release_run)
        run.update(overrides)
        self.fixture.api.routes[RELEASE_RUN_URL] = json_response(run)

    def test_disabled_release_workflow_is_allowed_for_history(self) -> None:
        for state in (
            "disabled_manually",
            "disabled_inactivity",
            "disabled_fork",
        ):
            with self.subTest(state=state):
                self._set_release_workflow(state=state)
                self.verify()

    def test_deleted_release_workflow_is_not_found(self) -> None:
        self._set_release_workflow(state="deleted")
        self.assert_error("workflow", "not-found")

    def test_unknown_release_workflow_state_is_schema(self) -> None:
        self._set_release_workflow(state="archived")
        self.assert_error("workflow", "schema")

    def test_release_workflow_path_mismatch_is_rejected(self) -> None:
        self._set_release_workflow(path=source.CI_WORKFLOW_PATH)
        self.assert_error("workflow", "path")

    def test_deleted_release_workflow_with_wrong_path_reports_path(
        self,
    ) -> None:
        self._set_release_workflow(
            state="deleted", path=source.CI_WORKFLOW_PATH
        )
        self.assert_error("workflow", "path")

    def test_release_run_wrong_workflow_id(self) -> None:
        self._set_release_run(workflow_id=CI_WORKFLOW_ID)
        self.assert_error("run", "identity")

    def test_release_run_wrong_id(self) -> None:
        self._set_release_run(id=RELEASE_RUN_ID + 1)
        self.assert_error("run", "id")

    def test_release_run_wrong_attempt(self) -> None:
        self._set_release_run(run_attempt=RELEASE_RUN_ATTEMPT + 1)
        self.assert_error("run", "attempt")

    def test_release_run_wrong_source(self) -> None:
        self._set_release_run(head_sha="b" * 40)
        self.assert_error("run", "source")

    def test_release_run_wrong_branch(self) -> None:
        self._set_release_run(head_branch="release")
        self.assert_error("run", "branch")

    def test_release_run_push_event_is_rejected(self) -> None:
        self._set_release_run(event="push")
        self.assert_error("run", "event")

    def test_release_run_running_status_is_rejected(self) -> None:
        self._set_release_run(status="in_progress", conclusion=None)
        self.assert_error("run", "status")

    def test_release_run_failed_conclusion_is_rejected(self) -> None:
        self._set_release_run(conclusion="failure")
        self.assert_error("run", "conclusion")

    def test_release_run_ci_workflow_path_is_rejected(self) -> None:
        self._set_release_run(path=source.CI_WORKFLOW_PATH)
        self.assert_error("run", "workflow")

    def test_release_run_wrong_repository_name(self) -> None:
        self._set_release_run(
            repository={"id": REPOSITORY_ID, "full_name": "other/repo"}
        )
        self.assert_error("run", "repository")

    def test_release_run_missing_repository_id(self) -> None:
        self._set_release_run(repository={"full_name": source.REPOSITORY})
        self.assert_error("run", "repository")

    def test_release_run_missing_required_field_is_rejected(self) -> None:
        run = copy.deepcopy(self.fixture.release_run)
        run.pop("conclusion")
        self.fixture.api.routes[RELEASE_RUN_URL] = json_response(run)
        self.assert_error("run", "conclusion")


class ReleaseArtifactTests(SourceTestBase):
    def _replace_release_artifact(self, **overrides: Any) -> None:
        artifact = make_release_artifact(
            self.fixture.zip_bytes, **overrides
        )
        self.fixture.set_release_artifacts(
            [artifact, self.fixture.release_artifacts[1]]
        )

    def test_old_attempt_artifact_is_not_used(self) -> None:
        old = make_release_artifact(
            self.fixture.zip_bytes,
            artifact_id=902,
            name=f"release-summary-{RELEASE_RUN_ID}-1",
        )
        self.fixture.set_release_artifacts([old])
        self.assert_error("artifact", "not-found")
        self.assertEqual(self.fixture.download.calls, [])

    def test_duplicate_exact_artifact_name_is_rejected(self) -> None:
        duplicate = make_release_artifact(
            self.fixture.zip_bytes, artifact_id=902
        )
        self.fixture.set_release_artifacts(
            [self.fixture.release_artifact, duplicate]
        )
        self.assert_error("artifact", "duplicate")
        self.assertEqual(self.fixture.download.calls, [])

    def test_expired_artifact_is_rejected(self) -> None:
        self._replace_release_artifact(expired=True)
        self.assert_error("artifact", "expired")

    def test_non_boolean_expired_is_rejected(self) -> None:
        self._replace_release_artifact(expired="false")
        self.assert_error("artifact", "expired")

    def test_oversize_artifact_is_rejected(self) -> None:
        self._replace_release_artifact(size=source.MAX_ARCHIVE_BYTES + 1)
        self.assert_error("artifact", "size")

    def test_non_canonical_artifact_digest_is_rejected(self) -> None:
        self._replace_release_artifact(digest="sha256:abc")
        self.assert_error("artifact", "digest")

    def test_bool_artifact_size_is_rejected(self) -> None:
        self._replace_release_artifact(size=True)
        self.assert_error("artifact", "schema")

    def test_workflow_run_source_mismatch_is_rejected(self) -> None:
        workflow_run = copy.deepcopy(
            self.fixture.release_artifact["workflow_run"]
        )
        workflow_run["head_sha"] = "b" * 40
        self._replace_release_artifact(workflow_run=workflow_run)
        self.assert_error("artifact", "workflow-run")

    def test_workflow_run_run_id_mismatch_is_rejected(self) -> None:
        workflow_run = copy.deepcopy(
            self.fixture.release_artifact["workflow_run"]
        )
        workflow_run["id"] = RELEASE_RUN_ID + 1
        self._replace_release_artifact(workflow_run=workflow_run)
        self.assert_error("artifact", "workflow-run")

    def test_workflow_run_repository_mismatch_is_rejected(self) -> None:
        workflow_run = copy.deepcopy(
            self.fixture.release_artifact["workflow_run"]
        )
        workflow_run["repository_id"] = 9999
        self._replace_release_artifact(workflow_run=workflow_run)
        self.assert_error("artifact", "workflow-run")

    def test_workflow_run_head_repository_mismatch_is_rejected(self) -> None:
        workflow_run = copy.deepcopy(
            self.fixture.release_artifact["workflow_run"]
        )
        workflow_run["head_repository_id"] = 9999
        self._replace_release_artifact(workflow_run=workflow_run)
        self.assert_error("artifact", "workflow-run")

    def test_workflow_run_null_head_repository_is_rejected(self) -> None:
        workflow_run = copy.deepcopy(
            self.fixture.release_artifact["workflow_run"]
        )
        workflow_run["head_repository_id"] = None
        self._replace_release_artifact(workflow_run=workflow_run)
        self.assert_error("artifact", "workflow-run")

    def test_workflow_run_without_head_repository_id_is_allowed(self) -> None:
        workflow_run = copy.deepcopy(
            self.fixture.release_artifact["workflow_run"]
        )
        workflow_run.pop("head_repository_id")
        self._replace_release_artifact(workflow_run=workflow_run)
        self.verify()

    def test_partial_workflow_run_object_is_rejected(self) -> None:
        self._replace_release_artifact(
            workflow_run={"id": RELEASE_RUN_ID, "head_sha": SOURCE_SHA}
        )
        self.assert_error("artifact", "workflow-run")

    def test_absent_workflow_run_metadata_is_allowed(self) -> None:
        self._replace_release_artifact(workflow_run=None)
        self.verify()

    def test_api_error_status_maps_to_fixed_categories(self) -> None:
        cases = {
            401: ("api", "unauthorized"),
            403: ("api", "forbidden"),
            429: ("api", "too-many-requests"),
            500: ("api", "server-error"),
            418: ("api", "http-error"),
        }
        for status, category in cases.items():
            with self.subTest(status=status):
                self.fixture.api.routes[RELEASE_RUN_URL] = json_response(
                    {"message": "not used"}, status=status
                )
                self.assert_error(*category)

    def test_rate_limited_403_is_distinguished(self) -> None:
        self.fixture.api.routes[RELEASE_RUN_URL] = json_response(
            {"message": "rate limit"},
            status=403,
            headers={"x-ratelimit-remaining": "0"},
        )
        self.assert_error("api", "rate-limit")


class ArtifactRedirectAndDownloadTests(SourceTestBase):
    def _redirect(self, location: Any) -> None:
        headers = {} if location is None else {"location": location}
        self.fixture.api.routes[self.fixture.zip_api_url] = (
            source.ci.HttpResponse(status=302, headers=headers, body=b"")
        )

    def test_api_zip_must_redirect(self) -> None:
        self.fixture.api.routes[self.fixture.zip_api_url] = bytes_response(
            self.fixture.zip_bytes
        )
        self.assert_error("artifact", "redirect")
        self.assertEqual(self.fixture.download.calls, [])

    def test_missing_redirect_location_is_rejected(self) -> None:
        self._redirect(None)
        self.assert_error("artifact", "redirect")

    def test_api_zip_404_is_not_found(self) -> None:
        self.fixture.api.routes[self.fixture.zip_api_url] = json_response(
            {"message": "not found"}, status=404
        )
        self.assert_error("artifact", "not-found")

    def test_api_zip_410_is_not_found(self) -> None:
        self.fixture.api.routes[self.fixture.zip_api_url] = json_response(
            {"message": "gone"}, status=410
        )
        self.assert_error("artifact", "not-found")

    def test_unknown_signed_host_is_rejected_without_download(self) -> None:
        self._redirect("https://evil.example/archive.zip")
        error = self.assert_error("download", "host")
        self.assertNotIn("evil.example", str(error))
        self.assertEqual(self.fixture.download.calls, [])

    def test_signed_url_userinfo_is_rejected(self) -> None:
        self._redirect("https://user@objects.githubusercontent.com/x.zip")
        self.assert_error("download", "host")

    def test_signed_url_http_is_rejected(self) -> None:
        self._redirect(
            "http://objects.githubusercontent.com/x.zip"
        )
        self.assert_error("download", "host")

    def test_signed_url_odd_port_is_rejected(self) -> None:
        self._redirect(
            "https://objects.githubusercontent.com:8443/x.zip"
        )
        self.assert_error("download", "host")

    def test_signed_url_ip_host_is_rejected(self) -> None:
        self._redirect("https://1.2.3.4/x.zip")
        self.assert_error("download", "host")

    def test_signed_url_with_control_character_is_rejected(self) -> None:
        self._redirect(
            "https://objects.githubusercontent.com/x.zip\nInjected: yes"
        )
        self.assert_error("download", "host")

    def test_signed_url_allowed_hosts(self) -> None:
        valid = [
            "https://productionresultssa1.blob.core.windows.net/a.zip?x=1",
            "https://objects.githubusercontent.com/a.zip",
            "https://githubusercontent.com/a.zip",
        ]
        for url in valid:
            with self.subTest(url=url):
                source._validate_signed_url(url)
        invalid = [
            "https://blob.core.windows.net/a.zip",
            "https://evilblob.core.windows.net/a.zip",
            "https://foo.blob.core.windows.net.evil.example/a.zip",
            "https://objects.githubusercontent.com.evil.example/a.zip",
            "https://example.com/a.zip",
            "https://objects.githubusercontent.com/a.zip#fragment",
            "https://objects.githubusercontent.com",
        ]
        for url in invalid:
            with self.subTest(url=url):
                with self.assertRaises(source.VerificationError) as caught:
                    source._validate_signed_url(url)
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    ("download", "host"),
                )

    def test_download_hash_mismatch_is_rejected(self) -> None:
        changed = bytearray(self.fixture.zip_bytes)
        changed[-1] ^= 0xFF
        self.fixture.download.response = bytes_response(bytes(changed))
        self.assert_error("download", "hash")

    def test_download_length_mismatch_is_rejected(self) -> None:
        self.fixture.download.response = bytes_response(
            self.fixture.zip_bytes[:-1]
        )
        self.assert_error("download", "size")

    def test_download_oversize_body_is_rejected(self) -> None:
        body = b"x" * (source.MAX_ARCHIVE_BYTES + 1)
        self.fixture.download.response = source.ci.HttpResponse(
            status=200, headers={}, body=body
        )
        self.assert_error("download", "size")

    def test_download_declared_length_mismatch_is_rejected(self) -> None:
        self.fixture.download.response = bytes_response(
            self.fixture.zip_bytes,
            headers={
                "content-length": str(len(self.fixture.zip_bytes) + 1),
                "content-type": "application/zip",
            },
        )
        self.assert_error("download", "size")

    def test_download_redirect_is_not_followed(self) -> None:
        self.fixture.download.response = source.ci.HttpResponse(
            status=302,
            headers={"location": "https://objects.githubusercontent.com/x"},
            body=b"",
        )
        self.assert_error("download", "redirect")
        self.assertEqual(len(self.fixture.download.calls), 1)

    def test_download_410_is_not_found(self) -> None:
        self.fixture.download.response = source.ci.HttpResponse(
            status=410, headers={}, body=b""
        )
        self.assert_error("download", "not-found")

    def test_download_non_200_is_rejected(self) -> None:
        self.fixture.download.response = source.ci.HttpResponse(
            status=403, headers={}, body=b""
        )
        self.assert_error("download", "status")


class _FakeStorageResponse:
    def __init__(
        self,
        body: bytes = b"",
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self._headers = headers or {}
        self.read_error = read_error
        self.read_sizes: list[int] = []
        self.offset = 0

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self._headers.items())

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        if self.read_error is not None:
            raise self.read_error
        if self.offset >= len(self.body):
            return b""
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class _FakeStorageConnection:
    def __init__(
        self,
        response: Any,
        factory_error: Exception | None = None,
    ) -> None:
        self.response = response
        self.factory_error = factory_error
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False

    def request(
        self, method: str, target: str, headers: dict[str, str] | None = None
    ) -> None:
        self.requests.append((method, target, dict(headers or {})))

    def getresponse(self) -> Any:
        return self.response

    def close(self) -> None:
        self.closed = True


class SignedHttpsTransportTests(unittest.TestCase):
    def _transport(
        self,
        response: Any,
        *,
        factory_error: Exception | None = None,
    ) -> tuple[Any, _FakeStorageConnection, dict[str, Any]]:
        connection = _FakeStorageConnection(response, factory_error)
        captured: dict[str, Any] = {}

        def factory(
            host: str, timeout: float, context: ssl.SSLContext
        ) -> Any:
            captured.update(host=host, timeout=timeout, context=context)
            if factory_error is not None:
                raise factory_error
            return connection

        return (
            source.SignedHttpsTransport(connection_factory=factory),
            connection,
            captured,
        )

    def test_success_sends_no_credentials_and_verifies_tls(self) -> None:
        response = _FakeStorageResponse(
            b"payload",
            headers={
                "Content-Length": "7",
                "Content-Type": "application/zip",
            },
        )
        transport, connection, captured = self._transport(response)
        result = transport.get(SIGNED_URL, 5.0)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, b"payload")
        self.assertTrue(connection.closed)
        method, target, headers = connection.requests[0]
        self.assertEqual(method, "GET")
        self.assertEqual(
            target,
            "/actions-results/run/artifact.zip"
            "?sv=2024-01-01&sig=secret-signature",
        )
        lowered = {name.lower(): value for name, value in headers.items()}
        self.assertNotIn("authorization", lowered)
        self.assertNotIn("cookie", lowered)
        self.assertNotIn("referer", lowered)
        self.assertEqual(lowered["accept"], "application/octet-stream")
        self.assertEqual(lowered["accept-encoding"], "identity")
        self.assertEqual(captured["host"], "productionresultssa1.blob.core.windows.net")
        self.assertEqual(captured["timeout"], 5.0)
        context = captured["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_redirect_is_rejected_without_following(self) -> None:
        response = _FakeStorageResponse(
            b"",
            status=302,
            headers={"Location": "https://objects.githubusercontent.com/x"},
        )
        transport, connection, _captured = self._transport(response)
        with self.assertRaises(source.VerificationError) as caught:
            transport.get(SIGNED_URL, 5.0)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("download", "redirect"),
        )
        self.assertEqual(len(connection.requests), 1)

    def test_410_is_not_found(self) -> None:
        transport, _connection, _captured = self._transport(
            _FakeStorageResponse(b"", status=410)
        )
        with self.assertRaises(source.VerificationError) as caught:
            transport.get(SIGNED_URL, 5.0)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("download", "not-found"),
        )

    def test_declared_oversize_body_aborts_before_reading(self) -> None:
        response = _FakeStorageResponse(
            b"",
            headers={
                "Content-Length": str(source.MAX_ARCHIVE_BYTES + 1)
            },
        )
        transport, _connection, _captured = self._transport(response)
        with self.assertRaises(source.VerificationError) as caught:
            transport.get(SIGNED_URL, 5.0)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("download", "size"),
        )
        self.assertEqual(response.read_sizes, [])

    def test_streamed_oversize_body_is_bounded(self) -> None:
        response = _FakeStorageResponse(
            b"x" * (source.MAX_ARCHIVE_BYTES + 100)
        )
        transport, _connection, _captured = self._transport(response)
        with self.assertRaises(source.VerificationError) as caught:
            transport.get(SIGNED_URL, 5.0)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("download", "size"),
        )
        self.assertTrue(response.read_sizes)
        self.assertLessEqual(max(response.read_sizes), 65536)
        self.assertEqual(response.offset, source.MAX_ARCHIVE_BYTES + 1)

    def test_declared_length_mismatch_is_rejected(self) -> None:
        response = _FakeStorageResponse(
            b"abc", headers={"Content-Length": "4"}
        )
        transport, _connection, _captured = self._transport(response)
        with self.assertRaises(source.VerificationError) as caught:
            transport.get(SIGNED_URL, 5.0)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("download", "size"),
        )

    def test_timeout_and_tls_and_network_are_fixed_categories(self) -> None:
        cases = [
            (socket.timeout("secret"), ("download", "timeout")),
            (ssl.SSLError("secret"), ("download", "tls")),
            (OSError("secret"), ("download", "network")),
        ]
        for error, category in cases:
            with self.subTest(error=type(error).__name__):
                transport, _connection, _captured = self._transport(
                    _FakeStorageResponse(b""), factory_error=error
                )
                with self.assertRaises(source.VerificationError) as caught:
                    transport.get(SIGNED_URL, 5.0)
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind), category
                )
                self.assertNotIn("secret", str(caught.exception))

    def test_unknown_host_never_reaches_connection_factory(self) -> None:
        called = False

        def factory(_host: str, _timeout: float, _context: Any) -> Any:
            nonlocal called
            called = True
            raise AssertionError("factory must not be called")

        transport = source.SignedHttpsTransport(connection_factory=factory)
        with self.assertRaises(source.VerificationError) as caught:
            transport.get("https://evil.example/x.zip", 5.0)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind),
            ("download", "host"),
        )
        self.assertFalse(called)


class ArchiveSafetyTests(SourceTestBase):
    def release_files(self) -> dict[str, bytes]:
        return {
            "release-summary.json": self.fixture.summary_bytes,
            "descriptor.json": self.fixture.descriptor_bytes,
            "release-evidence.json": self.fixture.evidence_bytes,
        }

    def assert_archive_error(self, stage: str, kind: str, data: bytes) -> None:
        with self.assertRaises(source.VerificationError) as caught:
            source._read_release_archive(data)
        self.assertEqual(
            (caught.exception.stage, caught.exception.kind), (stage, kind)
        )

    def test_valid_three_root_files_are_read(self) -> None:
        files = self.release_files()
        result = source._read_release_archive(zip_files(files))
        self.assertEqual(result, files)

    def test_missing_file_is_rejected(self) -> None:
        files = self.release_files()
        files.pop("release-summary.json")
        self.assert_archive_error(
            "archive", "entries", zip_files(files)
        )

    def test_extra_file_is_rejected(self) -> None:
        files = self.release_files()
        files["extra.json"] = b"{}"
        self.assert_archive_error(
            "archive", "entries", zip_files(files)
        )

    def test_nested_path_is_rejected(self) -> None:
        files = self.release_files()
        files["nested/release-summary.json"] = files.pop(
            "release-summary.json"
        )
        self.assert_archive_error(
            "archive", "entries", zip_files(files)
        )

    def test_directory_entry_is_rejected(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, data in self.release_files().items():
                archive.writestr(name, data)
            archive.writestr("subdir/", b"")
        self.assert_archive_error("archive", "type", buffer.getvalue())

    def test_directory_mode_without_slash_is_rejected(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo("release-summary.json")
            info.create_system = 3
            info.external_attr = (stat.S_IFDIR | 0o755) << 16
            archive.writestr(info, self.fixture.summary_bytes)
            archive.writestr(
                "descriptor.json", self.fixture.descriptor_bytes
            )
            archive.writestr(
                "release-evidence.json", self.fixture.evidence_bytes
            )
        self.assert_archive_error(
            "archive", "type", buffer.getvalue()
        )

    def test_symlink_entry_is_rejected(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo("release-summary.json")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, b"target")
            archive.writestr(
                "descriptor.json", self.fixture.descriptor_bytes
            )
            archive.writestr(
                "release-evidence.json", self.fixture.evidence_bytes
            )
        self.assert_archive_error(
            "archive", "symlink", buffer.getvalue()
        )

    def test_encrypted_entry_is_rejected(self) -> None:
        data = mark_zip_encrypted(zip_files(self.release_files()))
        self.assert_archive_error("archive", "encrypted", data)

    def test_duplicate_entry_name_is_rejected(self) -> None:
        buffer = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr(
                    "release-summary.json", self.fixture.summary_bytes
                )
                archive.writestr(
                    "release-summary.json", self.fixture.summary_bytes
                )
                archive.writestr(
                    "descriptor.json", self.fixture.descriptor_bytes
                )
                archive.writestr(
                    "release-evidence.json", self.fixture.evidence_bytes
                )
        self.assert_archive_error(
            "archive", "duplicate", buffer.getvalue()
        )

    def test_single_file_over_limit_is_rejected(self) -> None:
        files = self.release_files()
        files["release-summary.json"] = b"x" * (source.MAX_FILE_BYTES + 1)
        self.assert_archive_error("archive", "size", zip_files(files))

    def test_total_uncompressed_size_over_limit_is_rejected(self) -> None:
        files = {
            "release-summary.json": b"x" * 200_000,
            "descriptor.json": b"x" * 200_000,
            "release-evidence.json": b"x" * 200_000,
        }
        self.assert_archive_error("archive", "size", zip_files(files))

    def test_invalid_zip_is_rejected(self) -> None:
        self.assert_archive_error("archive", "zip", b"not a zip file")

    def test_nul_suffix_filename_is_rejected(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo("release-summary.json")
            info.orig_filename = "release-summary.json\x00evil"
            info.filename = info.orig_filename
            archive.writestr(info, self.fixture.summary_bytes)
            archive.writestr(
                "descriptor.json", self.fixture.descriptor_bytes
            )
            archive.writestr(
                "release-evidence.json", self.fixture.evidence_bytes
            )
        self.assert_archive_error(
            "archive", "entries", buffer.getvalue()
        )

    def test_path_like_entry_names_are_rejected(self) -> None:
        cases = {
            "nested/release-summary.json": "release-summary.json",
            "../release-summary.json": "release-summary.json",
            "/release-summary.json": "release-summary.json",
            "release-summary.json\\evil": "release-summary.json",
        }
        for bad_name, replaced in cases.items():
            with self.subTest(name=bad_name):
                files = self.release_files()
                files[bad_name] = files.pop(replaced)
                self.assert_archive_error(
                    "archive", "entries", zip_files(files)
                )

    def test_empty_archive_is_rejected(self) -> None:
        self.assert_archive_error("archive", "zip", b"")


class CiSourceTests(SourceTestBase):
    def _set_ci_run(self, **overrides: Any) -> None:
        run = copy.deepcopy(self.fixture.ci_run)
        run.update(overrides)
        self.fixture.api.routes[CI_RUN_URL] = json_response(run)

    def _set_ci_workflow(self, **overrides: Any) -> None:
        workflow = copy.deepcopy(self.fixture.ci_workflow)
        workflow.update(overrides)
        self.fixture.api.routes[CI_WORKFLOW_URL] = json_response(workflow)

    def test_disabled_ci_workflow_is_allowed_for_history(self) -> None:
        for state in (
            "disabled_manually",
            "disabled_inactivity",
            "disabled_fork",
        ):
            with self.subTest(state=state):
                self._set_ci_workflow(state=state)
                self.verify()

    def test_deleted_ci_workflow_is_not_found(self) -> None:
        self._set_ci_workflow(state="deleted")
        self.assert_error("workflow", "not-found")

    def test_unknown_ci_workflow_state_is_schema(self) -> None:
        self._set_ci_workflow(state="archived")
        self.assert_error("workflow", "schema")

    def test_ci_workflow_path_mismatch_is_rejected(self) -> None:
        self._set_ci_workflow(path=source.RELEASE_WORKFLOW_PATH)
        self.assert_error("workflow", "path")

    def test_ci_run_wrong_workflow_id(self) -> None:
        self._set_ci_run(workflow_id=RELEASE_WORKFLOW_ID)
        self.assert_error("ci-run", "identity")

    def test_ci_run_wrong_id(self) -> None:
        self._set_ci_run(id=CI_RUN_ID + 1)
        self.assert_error("ci-run", "id")

    def test_ci_run_wrong_attempt(self) -> None:
        self._set_ci_run(run_attempt=CI_RUN_ATTEMPT + 1)
        self.assert_error("ci-run", "attempt")

    def test_ci_run_non_push_event_is_rejected(self) -> None:
        self._set_ci_run(event="workflow_dispatch")
        self.assert_error("ci-run", "identity")

    def test_ci_run_wrong_source(self) -> None:
        self._set_ci_run(head_sha="b" * 40)
        self.assert_error("ci-run", "identity")

    def test_ci_run_wrong_branch(self) -> None:
        self._set_ci_run(head_branch="release")
        self.assert_error("ci-run", "identity")

    def test_ci_run_in_progress_is_rejected(self) -> None:
        self._set_ci_run(status="in_progress", conclusion=None)
        self.assert_error("ci-run", "status")

    def test_ci_run_failed_conclusion_is_rejected(self) -> None:
        self._set_ci_run(conclusion="failure")
        self.assert_error("ci-run", "conclusion")

    def test_ci_run_repository_id_mismatch_is_rejected(self) -> None:
        self._set_ci_run(
            repository={
                "id": REPOSITORY_ID + 1,
                "full_name": source.REPOSITORY,
            }
        )
        self.assert_error("ci-run", "repository")

    def test_ci_run_missing_repository_id_is_rejected(self) -> None:
        self._set_ci_run(repository={"full_name": source.REPOSITORY})
        self.assert_error("ci-run", "schema")

    def test_missing_ci_bundle_name_is_rejected(self) -> None:
        self.fixture.set_ci_artifacts(
            [make_ci_artifact(name=f"ci-release-bundle-{CI_RUN_ID}-2")]
        )
        self.assert_error("ci-artifact", "not-found")

    def test_duplicate_ci_bundle_name_is_rejected(self) -> None:
        self.fixture.set_ci_artifacts(
            [self.fixture.ci_artifact, make_ci_artifact(artifact_id=557)]
        )
        self.assert_error("ci-artifact", "duplicate")

    def test_ci_artifact_id_must_match_evidence(self) -> None:
        self.fixture.set_ci_artifacts(
            [make_ci_artifact(artifact_id=BUNDLE_ARTIFACT_ID + 1)]
        )
        self.assert_error("ci-artifact", "id")

    def test_ci_artifact_digest_must_match_evidence(self) -> None:
        self.fixture.set_ci_artifacts(
            [make_ci_artifact(digest="sha256:" + "4" * 64)]
        )
        self.assert_error("ci-artifact", "digest")

    def test_expired_ci_bundle_is_rejected(self) -> None:
        self.fixture.set_ci_artifacts([make_ci_artifact(expired=True)])
        self.assert_error("ci-artifact", "expired")

    def test_ci_artifact_workflow_run_mismatch_is_rejected(self) -> None:
        workflow_run = copy.deepcopy(
            self.fixture.ci_artifact["workflow_run"]
        )
        workflow_run["repository_id"] = REPOSITORY_ID + 1
        self.fixture.set_ci_artifacts(
            [make_ci_artifact(workflow_run=workflow_run)]
        )
        self.assert_error("ci-artifact", "workflow-run")

    def test_ci_artifact_head_repository_mismatch_is_rejected(self) -> None:
        workflow_run = copy.deepcopy(
            self.fixture.ci_artifact["workflow_run"]
        )
        workflow_run["head_repository_id"] = REPOSITORY_ID + 1
        self.fixture.set_ci_artifacts(
            [make_ci_artifact(workflow_run=workflow_run)]
        )
        self.assert_error("ci-artifact", "workflow-run")

    def test_ci_artifact_without_workflow_run_is_allowed(self) -> None:
        self.fixture.set_ci_artifacts([make_ci_artifact(workflow_run=None)])
        self.verify()

    def test_ci_bundle_api_url_is_not_downloaded(self) -> None:
        self.verify()
        for url, _token, _timeout in self.fixture.api.calls:
            self.assertNotIn(str(BUNDLE_ARTIFACT_ID) + "/zip", url)


class ApiJsonStrictnessTests(SourceTestBase):
    def _set_release_run_body(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self.fixture.api.routes[RELEASE_RUN_URL] = source.ci.HttpResponse(
            status=status,
            headers={"content-type": content_type},
            body=body,
        )

    def test_duplicate_top_level_key_is_rejected(self) -> None:
        self._set_release_run_body(b'{"id":7001,"id":7001}')
        self.assert_error("api", "duplicate-key")

    def test_duplicate_nested_key_is_rejected(self) -> None:
        self._set_release_run_body(
            b'{"repository":{"id":1234,"id":1234}}'
        )
        self.assert_error("api", "duplicate-key")

    def test_non_finite_json_constant_is_rejected(self) -> None:
        self._set_release_run_body(b'{"id":7001,"value":NaN}')
        self.assert_error("api", "bad-json")

    def test_excessive_json_recursion_is_rejected(self) -> None:
        body = b"[" * 10_000 + b"0" + b"]" * 10_000
        self._set_release_run_body(body)
        self.assert_error("api", "bad-json")

    def test_invalid_utf8_json_is_rejected(self) -> None:
        self._set_release_run_body(b"\xff\xfe")
        self.assert_error("api", "bad-json")

    def test_error_response_body_is_not_strictly_parsed(self) -> None:
        self._set_release_run_body(
            b'{"message":"duplicate","message":"duplicate"}', status=500
        )
        self.assert_error("api", "server-error")


class ModuleLoadingTests(unittest.TestCase):
    def _load_with_loader(self, module_name: str, error: BaseException) -> None:
        sys.modules.pop(module_name, None)
        spec = importlib.util.spec_from_file_location(
            module_name, source._SCRIPT_DIR / "release_consumable_source.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        with mock.patch.object(
            spec.loader, "exec_module", side_effect=error
        ):
            with mock.patch.object(
                source.importlib.util,
                "spec_from_file_location",
                return_value=spec,
            ):
                source._load_script(
                    module_name, "release_consumable_source.py"
                )

    def test_interrupted_module_load_leaves_no_partial_module(self) -> None:
        module_name = "payment_hub_release_consumable_interrupted_test"
        try:
            with self.assertRaises(KeyboardInterrupt):
                self._load_with_loader(module_name, KeyboardInterrupt())
        finally:
            self.assertNotIn(module_name, sys.modules)
            sys.modules.pop(module_name, None)

    def test_ordinary_module_failure_is_sanitized_and_removed(self) -> None:
        module_name = "payment_hub_release_consumable_failure_test"
        try:
            with self.assertRaises(source.VerificationError) as caught:
                self._load_with_loader(
                    module_name, RuntimeError("secret-loader-failure")
                )
            self.assertEqual(
                (caught.exception.stage, caught.exception.kind),
                ("internal", "unexpected"),
            )
            self.assertNotIn("secret", str(caught.exception))
        finally:
            self.assertNotIn(module_name, sys.modules)
            sys.modules.pop(module_name, None)


class WallDeadlineTests(SourceTestBase):
    def _install_main_timer(
        self, delay: float, interval: float = 0.0
    ) -> tuple[Any, Any]:
        """Store a benign main-thread timer; return its old and new handler."""

        def handler(_signum: int, _frame: Any) -> None:
            pass

        old_handler = source.signal.getsignal(source.signal.SIGALRM)
        source.signal.signal(source.signal.SIGALRM, handler)
        source.signal.setitimer(
            source.signal.ITIMER_REAL, delay, interval
        )
        return old_handler, handler

    def _restore_main_timer(self, old_handler: Any) -> None:
        source.signal.setitimer(source.signal.ITIMER_REAL, 0)
        source.signal.signal(source.signal.SIGALRM, old_handler)

    def test_worker_thread_does_not_consume_main_thread_alarm(self) -> None:
        old_handler, handler = self._install_main_timer(60.0)
        outcome: dict[str, Any] = {}

        def run() -> None:
            try:
                self.verify()
            except BaseException as exc:
                outcome["error"] = exc

        try:
            worker = threading.Thread(target=run)
            worker.start()
            worker.join(10.0)
            self.assertFalse(worker.is_alive())
            error = outcome.get("error")
            self.assertIsInstance(error, source.VerificationError)
            self.assertEqual(
                (error.stage, error.kind),
                ("verify", "deadline-unsupported"),
            )
            self.assertEqual(self.fixture.api.calls, [])
            remaining, interval = source.signal.getitimer(
                source.signal.ITIMER_REAL
            )
            self.assertGreater(remaining, 0.0)
            self.assertEqual(interval, 0.0)
            self.assertIs(
                source.signal.getsignal(source.signal.SIGALRM), handler
            )
        finally:
            self._restore_main_timer(old_handler)

    def test_active_main_thread_alarm_is_rejected_and_preserved(self) -> None:
        old_handler, handler = self._install_main_timer(60.0)
        try:
            started = time.monotonic()
            self.assert_error("verify", "deadline-conflict")
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(self.fixture.api.calls, [])
            remaining, interval = source.signal.getitimer(
                source.signal.ITIMER_REAL
            )
            self.assertGreater(remaining, 0.0)
            self.assertLessEqual(remaining, 60.0)
            self.assertEqual(interval, 0.0)
            self.assertIs(
                source.signal.getsignal(source.signal.SIGALRM), handler
            )
        finally:
            self._restore_main_timer(old_handler)

    def test_active_main_thread_periodic_timer_is_rejected_and_preserved(
        self,
    ) -> None:
        old_handler, handler = self._install_main_timer(60.0, 5.0)
        try:
            self.assert_error("verify", "deadline-conflict")
            self.assertEqual(self.fixture.api.calls, [])
            remaining, interval = source.signal.getitimer(
                source.signal.ITIMER_REAL
            )
            self.assertGreater(remaining, 0.0)
            self.assertEqual(interval, 5.0)
            self.assertIs(
                source.signal.getsignal(source.signal.SIGALRM), handler
            )
        finally:
            self._restore_main_timer(old_handler)

    @unittest.skipUnless(
        hasattr(signal, "pthread_sigmask") and hasattr(signal, "SIGALRM"),
        "pthread_sigmask is unavailable",
    )
    def test_blocked_sigalrm_fails_closed_without_unblocking(self) -> None:
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        try:
            signal.pthread_sigmask(
                signal.SIG_SETMASK, old_mask | {signal.SIGALRM}
            )
            self.assert_error("verify", "deadline-unsupported")
            self.assertEqual(self.fixture.api.calls, [])
            self.assertIn(
                signal.SIGALRM,
                signal.pthread_sigmask(signal.SIG_BLOCK, []),
            )
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)

    def test_concurrent_verification_is_rejected_without_network(self) -> None:
        acquired = source._DEADLINE_LOCK.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            self.assert_error("verify", "deadline-conflict")
            self.assertEqual(self.fixture.api.calls, [])
            self.assertEqual(self.fixture.download.calls, [])
        finally:
            source._DEADLINE_LOCK.release()
        self.assertFalse(source._DEADLINE_LOCK.locked())

    def test_unknown_installed_handler_is_rejected_before_network(
        self,
    ) -> None:
        with mock.patch.object(
            source.signal, "getsignal", return_value=None
        ):
            self.assert_error("verify", "deadline-unsupported")
        self.assertEqual(self.fixture.api.calls, [])
        self.assertEqual(self.fixture.download.calls, [])

    def test_timer_ownership_survives_pause_around_setitimer(self) -> None:
        old_handler = source.signal.getsignal(source.signal.SIGALRM)
        deadline = source._WallDeadline(60.0)
        try:
            with mock.patch.object(
                source.time,
                "monotonic",
                side_effect=[10.0, 11.0, 11.0, 11.0],
            ):
                with mock.patch.object(
                    source.signal,
                    "getitimer",
                    side_effect=[(0.0, 0.0), (60.0, 0.0)],
                ):
                    with mock.patch.object(
                        source.signal, "setitimer"
                    ) as setitimer_mock:
                        deadline.__enter__()
                        deadline.__exit__(None, None, None)
                    setitimer_mock.assert_any_call(
                        source.signal.ITIMER_REAL, 0
                    )
            self.assertIs(
                source.signal.getsignal(source.signal.SIGALRM), old_handler
            )
        finally:
            source.signal.setitimer(source.signal.ITIMER_REAL, 0)
            source.signal.signal(source.signal.SIGALRM, old_handler)

    def test_timer_cleanup_survives_interrupt_after_arming(self) -> None:
        old_handler = source.signal.getsignal(source.signal.SIGALRM)
        deadline = source._WallDeadline(60.0)
        try:
            with mock.patch.object(
                source.time,
                "monotonic",
                side_effect=[0.0, KeyboardInterrupt(), 0.0, 0.0],
            ):
                with mock.patch.object(
                    source.signal,
                    "getitimer",
                    side_effect=[(0.0, 0.0), (60.0, 0.0)],
                ):
                    with mock.patch.object(
                        source.signal, "setitimer"
                    ) as setitimer_mock:
                        with self.assertRaises(KeyboardInterrupt):
                            deadline.__enter__()
                    setitimer_mock.assert_any_call(
                        source.signal.ITIMER_REAL, 0
                    )
            self.assertIs(
                source.signal.getsignal(source.signal.SIGALRM), old_handler
            )
        finally:
            source.signal.setitimer(source.signal.ITIMER_REAL, 0)
            source.signal.signal(source.signal.SIGALRM, old_handler)

    def test_timer_ownership_survives_pause_after_getitimer(self) -> None:
        deadline = source._WallDeadline(60.0)
        deadline.deadline_low = 70.0
        deadline.deadline_high = 70.0
        with mock.patch.object(
            source.time, "monotonic", side_effect=[10.0, 11.0]
        ):
            with mock.patch.object(
                source.signal,
                "getitimer",
                return_value=(60.0, 0.0),
            ):
                self.assertTrue(deadline._timer_is_ours())

    def test_timer_ownership_rejects_remaining_outside_interval(
        self,
    ) -> None:
        deadline = source._WallDeadline(60.0)
        deadline.deadline_low = 70.0
        deadline.deadline_high = 71.0
        with mock.patch.object(
            source.time, "monotonic", side_effect=[10.0, 10.5]
        ):
            with mock.patch.object(
                source.signal,
                "getitimer",
                return_value=(50.0, 0.0),
            ):
                self.assertFalse(deadline._timer_is_ours())

    def test_timer_cleanup_failure_still_restores_handler(self) -> None:
        old_handler = source.signal.getsignal(source.signal.SIGALRM)
        deadline = source._WallDeadline(60.0)
        with mock.patch.object(
            source.signal,
            "setitimer",
            side_effect=[None, source._DeadlineExceeded()],
        ):
            with self.assertRaises(source._DeadlineExceeded):
                with deadline:
                    pass
        self.assertIs(
            source.signal.getsignal(source.signal.SIGALRM), old_handler
        )

    def test_externally_replaced_handler_is_left_untouched(self) -> None:
        old_handler = source.signal.getsignal(source.signal.SIGALRM)
        deadline = source._WallDeadline(60.0)
        deadline.__enter__()
        external_handler = lambda _signum, _frame: None
        source.signal.signal(source.signal.SIGALRM, external_handler)
        try:
            with mock.patch.object(
                source.signal, "setitimer", wraps=source.signal.setitimer
            ) as setitimer_mock:
                with mock.patch.object(
                    source.signal, "signal", wraps=source.signal.signal
                ) as signal_mock:
                    deadline.__exit__(None, None, None)
                setitimer_mock.assert_called_once_with(
                    source.signal.ITIMER_REAL, 0
                )
                signal_mock.assert_not_called()
            self.assertIs(
                source.signal.getsignal(source.signal.SIGALRM),
                external_handler,
            )
            remaining, interval = source.signal.getitimer(
                source.signal.ITIMER_REAL
            )
            self.assertEqual(remaining, 0.0)
            self.assertEqual(interval, 0.0)
        finally:
            source.signal.setitimer(source.signal.ITIMER_REAL, 0)
            source.signal.signal(source.signal.SIGALRM, old_handler)

    def test_externally_replaced_timer_is_not_cleared(self) -> None:
        old_handler = source.signal.getsignal(source.signal.SIGALRM)
        for delay, interval in ((5.0, 1.0), (5.0, 0.0)):
            with self.subTest(delay=delay, interval=interval):
                deadline = source._WallDeadline(60.0)
                deadline.__enter__()
                try:
                    source.signal.setitimer(
                        source.signal.ITIMER_REAL, delay, interval
                    )
                    with mock.patch.object(
                        source.signal,
                        "setitimer",
                        wraps=source.signal.setitimer,
                    ) as setitimer_mock:
                        deadline.__exit__(None, None, None)
                    setitimer_mock.assert_not_called()
                    remaining, current_interval = source.signal.getitimer(
                        source.signal.ITIMER_REAL
                    )
                    self.assertGreater(remaining, 0.0)
                    self.assertEqual(current_interval, interval)
                    self.assertIs(
                        source.signal.getsignal(source.signal.SIGALRM),
                        old_handler,
                    )
                finally:
                    source.signal.setitimer(source.signal.ITIMER_REAL, 0)
                    source.signal.signal(
                        source.signal.SIGALRM, old_handler
                    )

    def test_continuous_download_body_cannot_outlive_wall_deadline(self) -> None:
        class EndlessResponse:
            status = 200

            def __init__(self) -> None:
                self.limit = time.monotonic() + 5.0

            @staticmethod
            def getheaders() -> list[tuple[str, str]]:
                return []

            def read(self, _size: int) -> bytes:
                if time.monotonic() >= self.limit:
                    raise AssertionError(
                        "wall deadline did not interrupt the download"
                    )
                time.sleep(0.01)
                return b"x"

        response = EndlessResponse()
        connection = _FakeStorageConnection(response)
        captured: dict[str, Any] = {}

        def factory(
            host: str, timeout: float, context: ssl.SSLContext
        ) -> Any:
            captured.update(host=host, timeout=timeout, context=context)
            return connection

        transport = source.SignedHttpsTransport(
            connection_factory=factory
        )
        old_handler = source.signal.getsignal(source.signal.SIGALRM)
        started = time.monotonic()
        with mock.patch.object(source, "TOTAL_TIMEOUT_SECONDS", 0.3):
            self.assert_error(
                "verify", "deadline", download_transport=transport
            )
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(connection.closed)
        self.assertEqual(
            source.signal.getsignal(source.signal.SIGALRM), old_handler
        )

    def test_slow_api_response_cannot_outlive_wall_deadline(self) -> None:
        class SlowApiTransport:
            def __init__(self) -> None:
                self.entered = False

            def get(self, _url: str, _token: str, _timeout: float) -> Any:
                self.entered = True
                source.signal.setitimer(
                    source.signal.ITIMER_REAL, 0.05
                )
                limit = time.monotonic() + 5.0
                while time.monotonic() < limit:
                    time.sleep(0.01)
                raise AssertionError(
                    "wall deadline did not interrupt the slow API request"
                )

        transport = SlowApiTransport()
        started = time.monotonic()
        with mock.patch.object(source, "TOTAL_TIMEOUT_SECONDS", 5.0):
            self.assert_error(
                "verify", "deadline", api_transport=transport
            )
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(transport.entered)

    def test_slow_ci_request_cannot_return_success_after_wall_deadline(self) -> None:
        state = {"entered": False}

        def on_get(url: str) -> None:
            if url == CI_WORKFLOW_URL:
                state["entered"] = True
                source.signal.setitimer(
                    source.signal.ITIMER_REAL, 0.05
                )
                limit = time.monotonic() + 5.0
                while time.monotonic() < limit:
                    time.sleep(0.01)
                raise AssertionError(
                    "wall deadline did not interrupt the slow CI request"
                )

        self.fixture.api.on_get = on_get
        with mock.patch.object(source, "TOTAL_TIMEOUT_SECONDS", 5.0):
            self.assert_error("verify", "deadline")
        self.assertTrue(state["entered"])

    def test_shared_transport_cannot_swallow_wall_deadline(self) -> None:
        class EndlessResponse:
            status = 200

            def __init__(self) -> None:
                self.limit = time.monotonic() + 5.0

            @staticmethod
            def getheaders() -> list[tuple[str, str]]:
                return []

            def read(self, _size: int) -> bytes:
                if time.monotonic() >= self.limit:
                    raise AssertionError(
                        "wall deadline did not interrupt the shared transport"
                    )
                time.sleep(0.01)
                return b"x"

        connection = _FakeStorageConnection(EndlessResponse())

        def factory(
            _host: str, _timeout: float, _context: ssl.SSLContext
        ) -> Any:
            return connection

        transport = source.ci.HttpsTransport(connection_factory=factory)
        with mock.patch.object(source, "TOTAL_TIMEOUT_SECONDS", 0.3):
            self.assert_error(
                "verify", "deadline", api_transport=transport
            )
        self.assertTrue(connection.closed)

    def test_slow_offline_verification_is_covered(self) -> None:
        state = {"entered": False}

        def slow(*_args: Any, **_kwargs: Any) -> Any:
            state["entered"] = True
            source.signal.setitimer(source.signal.ITIMER_REAL, 0.05)
            limit = time.monotonic() + 5.0
            while time.monotonic() < limit:
                time.sleep(0.01)
            raise AssertionError(
                "wall deadline did not interrupt offline verification"
            )

        verifier = types.SimpleNamespace(verify_release=slow)
        with mock.patch.object(source, "TOTAL_TIMEOUT_SECONDS", 5.0):
            self.assert_error("verify", "deadline", verifier=verifier)
        self.assertTrue(state["entered"])

    def test_unsupported_wall_deadline_fails_closed(self) -> None:
        with mock.patch.object(
            source.signal, "setitimer", side_effect=AttributeError
        ):
            self.assert_error("verify", "deadline-unsupported")
        self.assertEqual(self.fixture.api.calls, [])

    def test_injected_clock_still_enforces_per_request_deadline(self) -> None:
        ticks = iter([0.0, 0.0, 1000.0])
        clock_calls = 0

        def clock() -> float:
            nonlocal clock_calls
            clock_calls += 1
            return next(ticks)

        self.assert_error("api", "timeout", clock=clock)
        self.assertEqual(self.fixture.api.calls, [])
        self.assertEqual(clock_calls, 3)


class PaginationTests(SourceTestBase):
    @staticmethod
    def noise_artifacts(count: int, base: int = 10_000) -> list[dict[str, Any]]:
        return [
            {"id": base + index, "name": f"noise-{base + index}"}
            for index in range(count)
        ]

    def _page(
        self,
        endpoint: str,
        first: list[dict[str, Any]],
        second: list[dict[str, Any]],
    ) -> None:
        next_url = endpoint + "?per_page=100&page=2"
        self.fixture.api.routes[endpoint + "?per_page=100&page=1"] = (
            artifacts_response(first, total_count=101, link=next_url)
        )
        self.fixture.api.routes[next_url] = artifacts_response(
            second, total_count=101
        )

    def test_release_artifact_on_second_page_is_found(self) -> None:
        first = self.noise_artifacts(100)
        self._page(
            RELEASE_ARTIFACTS_ENDPOINT,
            first,
            [self.fixture.release_artifact],
        )
        self.verify()

    def test_ci_artifact_on_second_page_is_found(self) -> None:
        first = self.noise_artifacts(100)
        self._page(
            CI_ARTIFACTS_ENDPOINT,
            first,
            [self.fixture.ci_artifact],
        )
        self.verify()

    def test_duplicate_exact_name_across_pages_is_rejected(self) -> None:
        first = self.noise_artifacts(99)
        first.append(
            make_release_artifact(
                self.fixture.zip_bytes, artifact_id=902
            )
        )
        self._page(
            RELEASE_ARTIFACTS_ENDPOINT,
            first,
            [self.fixture.release_artifact],
        )
        self.assert_error("artifact", "duplicate")

    def test_short_page_is_rejected_by_pagination_gate(self) -> None:
        self.fixture.api.routes[RELEASE_ARTIFACTS_URL] = artifacts_response(
            self.noise_artifacts(99), total_count=101
        )
        self.assert_error("artifacts", "pagination")

    def test_malformed_next_link_is_rejected(self) -> None:
        self.fixture.api.routes[RELEASE_ARTIFACTS_URL] = artifacts_response(
            self.noise_artifacts(100),
            total_count=101,
            link="https://evil.example/next",
        )
        self.assert_error("artifacts", "pagination")


class OrderingAndSanitizationTests(SourceTestBase):
    def test_offline_verifier_runs_before_ci_source_checks(self) -> None:
        events: list[str] = []
        original = offline.verify_release

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            events.append("offline")
            return original(*args, **kwargs)

        verifier = types.SimpleNamespace(verify_release=wrapped)

        def on_get(url: str) -> None:
            if url == CI_WORKFLOW_URL:
                events.append("ci")

        self.fixture.api.on_get = on_get
        self.verify(verifier=verifier)
        self.assertEqual(events, ["offline", "ci"])

    def test_offline_failure_prevents_ci_requests(self) -> None:
        def fail(*_args: Any, **_kwargs: Any) -> Any:
            raise offline.VerificationError("summary", "outcome")

        verifier = types.SimpleNamespace(verify_release=fail)
        self.assert_error("summary", "outcome", verifier=verifier)
        urls = self.fixture.api.urls()
        self.assertNotIn(CI_WORKFLOW_URL, urls)
        self.assertNotIn(CI_RUN_URL, urls)
        self.assertNotIn(CI_ARTIFACTS_URL, urls)

    def test_unknown_verifier_exception_is_sanitized(self) -> None:
        def fail(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("secret-token-and-url")

        verifier = types.SimpleNamespace(verify_release=fail)
        error = self.assert_error("verify", "verification", verifier=verifier)
        self.assertNotIn("secret", str(error))

    def test_unknown_api_exception_is_sanitized(self) -> None:
        self.fixture.api.failure = RuntimeError(
            "token=secret https://signed.example/secret"
        )
        error = self.assert_error("internal", "unexpected")
        self.assertNotIn("secret", str(error))
        self.assertNotIn("signed.example", str(error))

    def test_checker_unknown_category_is_sanitized(self) -> None:
        self.fixture.api.failure = source.ci.PreflightError(
            "api", "injected-secret-kind"
        )
        error = self.assert_error("internal", "unexpected")
        self.assertNotIn("injected", str(error))

    def test_checker_timeout_and_tls_are_preserved(self) -> None:
        cases = [
            (source.ci.PreflightError("api", "timeout"), ("api", "timeout")),
            (source.ci.PreflightError("api", "tls"), ("api", "tls")),
            (
                source.ci.PreflightError("api", "network"),
                ("api", "network"),
            ),
        ]
        for failure, category in cases:
            with self.subTest(category=category):
                self.fixture.api.failure = failure
                self.assert_error(*category)

    def test_unauthorized_body_is_not_echoed(self) -> None:
        self.fixture.api.routes[RELEASE_WORKFLOW_URL] = json_response(
            {"message": "token=secret-and-response-body"}, status=401
        )
        error = self.assert_error("api", "unauthorized")
        self.assertNotIn("secret", str(error))


if __name__ == "__main__":
    unittest.main()
