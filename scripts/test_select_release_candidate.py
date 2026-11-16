"""Standard-library tests for scripts/select-release-candidate.py.

All GitHub HTTP is faked in memory.  These tests make no network request, use
no real token, download no artifact, and never execute artifact content.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "select_release_candidate",
    REPO / "scripts" / "select-release-candidate.py",
)
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)
CI = MOD.CI

TOKEN = "ghs_test_token_1234567890"
SHA = "a" * 40
OTHER_SHA = "b" * 40
LATEST = "2026-01-02T00:00:00Z"
OLDER = "2026-01-01T00:00:00Z"
REPOSITORY_ID = 1448410805
POINTER_DIGEST = "sha256:" + "1" * 64
BUNDLE_DIGEST = "sha256:" + "2" * 64
DESCRIPTOR_SHA = "3" * 64


def make_workflow(path, workflow_id, state="active"):
    return {"id": workflow_id, "path": path, "state": state}


def make_run(
    run_id,
    workflow_id,
    *,
    attempt=1,
    status="completed",
    conclusion="success",
    sha=SHA,
    branch="main",
    event="push",
    repo=MOD.REPOSITORY,
    repository_id=REPOSITORY_ID,
    created_at=LATEST,
    run_path=None,
    **extra,
):
    path = MOD.WORKFLOW_PATH
    if run_path is None:
        run_path = f"{path}@main"
    data = {
        "id": run_id,
        "workflow_id": workflow_id,
        "run_attempt": attempt,
        "path": run_path,
        "head_sha": sha,
        "head_branch": branch,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "created_at": created_at,
        "repository": {"full_name": repo, "id": repository_id},
        "pull_requests": [],
    }
    data.update(extra)
    return data


def make_runs(total, runs):
    return {"total_count": total, "workflow_runs": runs}


def make_artifact(
    name,
    artifact_id,
    *,
    size=1024,
    digest=POINTER_DIGEST,
    expired=False,
    run_id=1001,
    workflow_run=True,
    **extra,
):
    data = {
        "id": artifact_id,
        "name": name,
        "size_in_bytes": size,
        "expired": expired,
        "digest": digest,
        "archive_download_url": (
            "https://api.github.com/repos/xtianxx/payment-hub/"
            f"actions/artifacts/{artifact_id}/zip"
        ),
        "url": (
            "https://api.github.com/repos/xtianxx/payment-hub/"
            f"actions/artifacts/{artifact_id}"
        ),
    }
    if workflow_run is True:
        data["workflow_run"] = {
            "id": run_id,
            "repository_id": REPOSITORY_ID,
            "head_repository_id": REPOSITORY_ID,
            "head_branch": "main",
            "head_sha": SHA,
        }
    elif workflow_run is not None:
        data["workflow_run"] = workflow_run
    data.update(extra)
    return data


def make_artifacts(total, artifacts):
    return {"total_count": total, "artifacts": artifacts}


def json_response(data, status=200, headers=None):
    merged = {"content-type": "application/json; charset=utf-8"}
    if headers:
        merged.update({str(k).lower(): str(v) for k, v in headers.items()})
    return CI.HttpResponse(
        status=status,
        headers=merged,
        body=json.dumps(data).encode("utf-8"),
    )


def raw_response(status, body=b"", headers=None):
    merged = {"content-type": "text/plain"}
    if headers:
        merged.update({str(k).lower(): str(v) for k, v in headers.items()})
    return CI.HttpResponse(status=status, headers=merged, body=body)


def base_env(**overrides):
    environment = {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": MOD.REPOSITORY,
        "GITHUB_SHA": SHA,
        "GITHUB_TOKEN": TOKEN,
    }
    environment.update(overrides)
    return environment


class FakeGitHub:
    def __init__(self):
        self.workflow_responses = {}
        self.page_responses = {}
        self.attempt_responses = {}
        self.artifact_responses = {}
        self.requests = []
        self.after_artifacts = None
        self.transport = _FakeTransport(self)


class _FakeTransport:
    def __init__(self, owner):
        self.owner = owner

    def get(self, url, token, timeout):
        self.owner.requests.append(url)
        parsed = urllib.parse.urlsplit(url)
        match = re.fullmatch(
            r"/repos/xtianxx/payment-hub/actions/workflows/([^/]+)/runs",
            parsed.path,
        )
        if match:
            workflow_id = int(match.group(1))
            page = int(urllib.parse.parse_qs(parsed.query)["page"][0])
            return self.owner.page_responses[(workflow_id, page)]
        match = re.fullmatch(
            r"/repos/xtianxx/payment-hub/actions/workflows/([^/]+)",
            parsed.path,
        )
        if match:
            return self.owner.workflow_responses[match.group(1)]
        match = re.fullmatch(
            r"/repos/xtianxx/payment-hub/actions/runs/([0-9]+)"
            r"/attempts/([0-9]+)",
            parsed.path,
        )
        if match:
            return self.owner.attempt_responses[
                (int(match.group(1)), int(match.group(2)))
            ]
        match = re.fullmatch(
            r"/repos/xtianxx/payment-hub/actions/runs/([0-9]+)/artifacts",
            parsed.path,
        )
        if match:
            run_id = int(match.group(1))
            page = int(urllib.parse.parse_qs(parsed.query)["page"][0])
            response = self.owner.artifact_responses[(run_id, page)]
            callback = self.owner.after_artifacts
            if callback is not None:
                self.owner.after_artifacts = None
                callback(self.owner)
            return response
        raise AssertionError(f"unexpected API request: {url}")


class SelectorTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.selection_path = self.root / "selection" / "selection.json"
        self.output_path = self.root / "github-output"
        self.verify_output_path = self.root / "verify-output"
        self.pointer_path = self.root / "pointer.json"
        self.fake = FakeGitHub()
        self.api = CI.GitHubApi(TOKEN, transport=self.fake.transport)
        self.workflow_id = 101
        self.run_id = 1001
        self.run_attempt = 1

    def add_ci_success(self, *, run_id=None, attempt=None, **overrides):
        run_id = self.run_id if run_id is None else run_id
        attempt = self.run_attempt if attempt is None else attempt
        self.fake.workflow_responses["ci.yml"] = json_response(
            make_workflow(MOD.WORKFLOW_PATH, self.workflow_id)
        )
        run = make_run(
            run_id,
            self.workflow_id,
            attempt=attempt,
            **overrides,
        )
        self.fake.page_responses[(self.workflow_id, 1)] = json_response(
            make_runs(1, [run])
        )
        self.fake.attempt_responses[(run_id, attempt)] = json_response(run)
        return run

    def add_artifacts(self, artifacts, *, run_id=None, total=None, headers=None):
        run_id = self.run_id if run_id is None else run_id
        if total is None:
            total = len(artifacts)
        self.fake.artifact_responses[(run_id, 1)] = json_response(
            make_artifacts(total, artifacts),
            headers=headers,
        )

    def add_default_artifacts(self, *, run_id=None, attempt=None):
        run_id = self.run_id if run_id is None else run_id
        attempt = self.run_attempt if attempt is None else attempt
        self.add_artifacts(
            [
                make_artifact(
                    MOD._artifact_name(MOD.POINTER_PREFIX, run_id, attempt),
                    11,
                    size=100,
                    digest=POINTER_DIGEST,
                    run_id=run_id,
                ),
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, run_id, attempt),
                    22,
                    size=1000,
                    digest=BUNDLE_DIGEST,
                    run_id=run_id,
                ),
            ],
            run_id=run_id,
        )

    def select_argv(self):
        return [
            "select",
            "--source-sha",
            SHA,
            "--summary",
            str(self.selection_path),
            "--github-output",
            str(self.output_path),
        ]

    def verify_argv(self):
        return [
            "verify",
            "--source-sha",
            SHA,
            "--selection",
            str(self.selection_path),
            "--pointer",
            str(self.pointer_path),
            "--github-output",
            str(self.verify_output_path),
        ]

    def run_main(self, argv, environment=None):
        environment = base_env() if environment is None else environment
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(MOD, "build_api", lambda token: self.api),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = MOD.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def run_select(self, environment=None):
        return self.run_main(self.select_argv(), environment)

    def selection_data(self):
        return json.loads(self.selection_path.read_text(encoding="utf-8"))

    def output_lines(self, path=None):
        path = self.output_path if path is None else path
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()

    def write_pointer(self, data=None):
        if data is None:
            data = {
                "schema": MOD.POINTER_SCHEMA,
                "repository": MOD.REPOSITORY,
                "workflow_path": MOD.WORKFLOW_PATH,
                "source_sha": SHA,
                "platform": MOD.PLATFORM,
                "run_id": self.run_id,
                "run_attempt": self.run_attempt,
                "bundle_artifact_id": 22,
                "bundle_artifact_digest": BUNDLE_DIGEST,
                "descriptor_sha256": DESCRIPTOR_SHA,
            }
        self.pointer_path.write_text(
            json.dumps(data), encoding="utf-8"
        )
        return data

    def write_selection(self, data=None):
        if data is None:
            data = MOD.build_selection_summary(
                result="pass",
                failure=None,
                source_sha=SHA,
                selection=MOD.Selection(
                    source_sha=SHA,
                    ci_run_id=self.run_id,
                    ci_run_attempt=self.run_attempt,
                    pointer_artifact_id=11,
                    pointer_artifact_digest=POINTER_DIGEST,
                    bundle_artifact_id=22,
                    bundle_artifact_digest=BUNDLE_DIGEST,
                ),
            )
        self.selection_path.parent.mkdir(parents=True, exist_ok=True)
        self.selection_path.write_text(json.dumps(data), encoding="utf-8")
        return data


class SelectSuccessTests(SelectorTestCase):
    def test_selector_workflow_path_is_fixed_candidate(self):
        self.assertEqual(MOD.WORKFLOW_PATH, CI.CANDIDATE_WORKFLOW_PATH)
        self.assertEqual(MOD.WORKFLOW_PATH, ".github/workflows/ci.yml")

    def test_select_uses_only_ci_workflow_path(self):
        self.add_ci_success()
        self.add_default_artifacts()
        code, stdout, stderr = self.run_select()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "release-selection: select pass\n")
        self.assertTrue(
            any(
                url.endswith("/actions/workflows/ci.yml")
                for url in self.fake.requests
            )
        )
        self.assertFalse(
            any("setup-check.yml" in url for url in self.fake.requests)
        )
        self.assertFalse(
            any(
                url.endswith("/actions/workflows/check.yml")
                for url in self.fake.requests
            )
        )
        self.assertEqual(len(self.fake.requests), 7)

    def test_selector_default_states_remain_legacy_pair(self):
        self.assertEqual(
            tuple(state.path for state in CI.default_workflow_states()),
            (
                ".github/workflows/setup-check.yml",
                ".github/workflows/check.yml",
            ),
        )
        self.assertEqual(
            CI.CI_WORKFLOW_PATHS,
            (CI.CANDIDATE_WORKFLOW_PATH,),
        )
        self.assertEqual(
            tuple(
                state.path
                for state in CI.default_workflow_states(
                    CI.CI_WORKFLOW_PATHS
                )
            ),
            (CI.CANDIDATE_WORKFLOW_PATH,),
        )

    def test_select_writes_fixed_provenance_and_outputs(self):
        self.add_ci_success()
        self.add_default_artifacts()
        code, stdout, stderr = self.run_select()
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "release-selection: select pass\n")
        self.assertEqual(stderr, "")
        data = self.selection_data()
        self.assertEqual(set(data), MOD.SELECTION_KEYS)
        self.assertEqual(data["schema"], MOD.SELECTION_SCHEMA)
        self.assertEqual(data["result"], "pass")
        self.assertIsNone(data["failure"])
        self.assertEqual(data["repository"], MOD.REPOSITORY)
        self.assertEqual(data["workflow_path"], MOD.WORKFLOW_PATH)
        self.assertEqual(data["source_sha"], SHA)
        self.assertEqual(data["platform"], MOD.PLATFORM)
        self.assertEqual(data["ci_run_id"], 1001)
        self.assertEqual(data["ci_run_attempt"], 1)
        self.assertEqual(data["pointer_artifact_id"], 11)
        self.assertEqual(data["pointer_artifact_digest"], POINTER_DIGEST)
        self.assertEqual(data["bundle_artifact_id"], 22)
        self.assertEqual(data["bundle_artifact_digest"], BUNDLE_DIGEST)
        self.assertEqual(
            self.output_lines(),
            [
                "ci_run_id=1001",
                "ci_run_attempt=1",
                "pointer_artifact_id=11",
            ],
        )
        text = self.selection_path.read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("https://", text)
        self.assertNotIn("archive_download_url", text)
        self.assertTrue(
            any(
                "/actions/workflows/ci.yml" in url
                for url in self.fake.requests
            )
        )
        self.assertTrue(
            any(
                "/actions/runs/1001/artifacts" in url
                for url in self.fake.requests
            )
        )
        self.assertTrue(all(url.startswith("https://api.github.com/") for url in self.fake.requests))

    def test_select_output_appends_after_existing_lf(self):
        self.add_ci_success()
        self.add_default_artifacts()
        self.output_path.write_text("existing=1\n", encoding="utf-8")
        code, stdout, stderr = self.run_select()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "release-selection: select pass\n")
        self.assertEqual(
            self.output_path.read_text(encoding="utf-8"),
            "existing=1\n"
            "ci_run_id=1001\n"
            "ci_run_attempt=1\n"
            "pointer_artifact_id=11\n",
        )

    def test_select_output_adds_separator_lf_for_existing_no_lf(self):
        self.add_ci_success()
        self.add_default_artifacts()
        self.output_path.write_text("existing=1", encoding="utf-8")
        code, stdout, stderr = self.run_select()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "release-selection: select pass\n")
        text = self.output_path.read_text(encoding="utf-8")
        self.assertEqual(
            text,
            "existing=1\n"
            "ci_run_id=1001\n"
            "ci_run_attempt=1\n"
            "pointer_artifact_id=11\n",
        )
        self.assertNotIn("existing=1ci_run_id", text)

    def test_select_output_empty_file_has_no_leading_blank_line(self):
        self.add_ci_success()
        self.add_default_artifacts()
        self.output_path.write_text("", encoding="utf-8")
        code, stdout, stderr = self.run_select()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "release-selection: select pass\n")
        text = self.output_path.read_text(encoding="utf-8")
        self.assertEqual(
            text,
            "ci_run_id=1001\n"
            "ci_run_attempt=1\n"
            "pointer_artifact_id=11\n",
        )
        self.assertFalse(text.startswith("\n"))

    def test_select_rechecks_same_run_after_artifacts(self):
        self.add_ci_success()
        self.add_default_artifacts()
        self.fake.after_artifacts = lambda fake: None
        code, stdout, stderr = self.run_select()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "release-selection: select pass\n")
        run_list_urls = [
            url
            for url in self.fake.requests
            if "/actions/workflows/101/runs" in url
        ]
        self.assertEqual(len(run_list_urls), 2)
        self.assertEqual(len(self.fake.requests), 7)
        self.assertTrue(
            all(
                url.startswith("https://api.github.com/")
                for url in self.fake.requests
            )
        )

    def test_select_accepts_artifact_without_workflow_run(self):
        self.add_ci_success()
        self.add_artifacts(
            [
                make_artifact(
                    MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
                    11,
                    digest=POINTER_DIGEST,
                    workflow_run=None,
                ),
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                    workflow_run=None,
                ),
            ]
        )
        code, stdout, _ = self.run_select()
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "release-selection: select pass\n")

    def test_unrelated_artifact_without_digest_is_ignored(self):
        self.add_ci_success()
        self.add_artifacts(
            [
                {"id": 1, "name": "coverage", "size_in_bytes": 10},
                make_artifact(
                    MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
                    11,
                    digest=POINTER_DIGEST,
                ),
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                ),
            ]
        )
        self.assertEqual(self.run_select()[0], 0)

    def test_select_artifact_pagination_reads_every_page(self):
        self.add_ci_success()
        page_one = [
            make_artifact(f"unrelated-{index}", 1000 + index)
            for index in range(100)
        ]
        page_two = [
            make_artifact(
                MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
                11,
                digest=POINTER_DIGEST,
            ),
            make_artifact(
                MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                22,
                digest=BUNDLE_DIGEST,
            ),
        ]
        self.fake.artifact_responses[(1001, 1)] = json_response(
            make_artifacts(102, page_one),
            headers={
                "link": (
                    '<https://api.github.com/repos/xtianxx/payment-hub/actions/'
                    'runs/1001/artifacts?per_page=100&page=2>; rel="next"'
                )
            },
        )
        self.fake.artifact_responses[(1001, 2)] = json_response(
            make_artifacts(102, page_two)
        )
        code, _, _ = self.run_select()
        self.assertEqual(code, 0)
        self.assertTrue(any("page=2" in url for url in self.fake.requests))


class SelectFailureTests(SelectorTestCase):
    def test_select_does_not_fall_back_to_legacy_success(self):
        self.fake.workflow_responses["ci.yml"] = raw_response(404, b"{}")
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(".github/workflows/setup-check.yml", 101)
        )
        self.fake.workflow_responses["check.yml"] = json_response(
            make_workflow(".github/workflows/check.yml", 202)
        )
        self.add_default_artifacts()
        self.assert_select_failure("workflow", "not-found")
        self.assertFalse(
            any("setup-check.yml" in url for url in self.fake.requests)
        )

    def test_new_run_after_artifacts_is_rejected(self):
        self.add_ci_success()
        self.add_default_artifacts()

        def switch_to_new_run(fake):
            newer = make_run(1002, 101, created_at=LATEST)
            fake.page_responses[(101, 1)] = json_response(
                make_runs(1, [newer])
            )
            fake.attempt_responses[(1002, 1)] = json_response(newer)

        self.fake.after_artifacts = switch_to_new_run
        self.assert_select_failure("run", "expected-mismatch")

    def test_new_attempt_after_artifacts_is_rejected(self):
        self.add_ci_success()
        self.add_default_artifacts()

        def switch_to_new_attempt(fake):
            newer = make_run(
                1001,
                101,
                attempt=2,
                created_at=LATEST,
            )
            fake.page_responses[(101, 1)] = json_response(
                make_runs(1, [newer])
            )
            fake.attempt_responses[(1001, 2)] = json_response(newer)

        self.fake.after_artifacts = switch_to_new_attempt
        self.assert_select_failure("run", "expected-mismatch")

    def test_queued_new_run_after_artifacts_is_rejected(self):
        self.add_ci_success()
        self.add_default_artifacts()

        def switch_to_queued_run(fake):
            newer = make_run(
                1002,
                101,
                status="queued",
                conclusion=None,
                created_at=LATEST,
            )
            fake.page_responses[(101, 1)] = json_response(
                make_runs(1, [newer])
            )
            fake.attempt_responses[(1002, 1)] = json_response(newer)

        self.fake.after_artifacts = switch_to_queued_run
        self.assert_select_failure("runs", "not-completed")

    def test_failed_new_run_after_artifacts_is_rejected(self):
        self.add_ci_success()
        self.add_default_artifacts()

        def switch_to_failed_run(fake):
            newer = make_run(
                1002,
                101,
                conclusion="failure",
                created_at=LATEST,
            )
            fake.page_responses[(101, 1)] = json_response(
                make_runs(1, [newer])
            )
            fake.attempt_responses[(1002, 1)] = json_response(newer)

        self.fake.after_artifacts = switch_to_failed_run
        self.assert_select_failure("runs", "not-success")

    def assert_select_failure(self, stage, kind, environment=None):
        code, stdout, stderr = self.run_select(environment)
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            f"release-selection: fail stage={stage} kind={kind}\n",
        )
        self.assertEqual(stderr, "")
        self.assertEqual(self.output_lines(), [])
        data = self.selection_data()
        self.assertEqual(data["result"], "fail")
        self.assertEqual(
            data["failure"], {"stage": stage, "kind": kind}
        )
        self.assertIsNone(data["ci_run_id"])
        self.assertIsNone(data["pointer_artifact_id"])

    def test_latest_ci_failure_fails_select(self):
        self.add_ci_success(conclusion="failure")
        self.add_default_artifacts()
        self.assert_select_failure("runs", "not-success")

    def test_latest_ci_running_fails_select(self):
        self.add_ci_success(status="in_progress", conclusion=None)
        self.add_default_artifacts()
        self.assert_select_failure("runs", "not-completed")

    def test_missing_pointer_fails_select(self):
        self.add_ci_success()
        self.add_artifacts(
            [
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                )
            ]
        )
        self.assert_select_failure("artifacts", "missing")

    def test_missing_bundle_fails_select(self):
        self.add_ci_success()
        self.add_artifacts(
            [
                make_artifact(
                    MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
                    11,
                    digest=POINTER_DIGEST,
                )
            ]
        )
        self.assert_select_failure("artifacts", "missing")

    def test_old_attempt_names_are_not_selected(self):
        self.run_attempt = 2
        self.add_ci_success(attempt=2)
        self.add_artifacts(
            [
                make_artifact(
                    MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
                    11,
                    digest=POINTER_DIGEST,
                ),
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                ),
            ]
        )
        self.assert_select_failure("artifacts", "missing")

    def test_wrong_run_name_is_not_selected(self):
        self.add_ci_success()
        self.add_artifacts(
            [
                make_artifact(
                    MOD._artifact_name(MOD.POINTER_PREFIX, 999, 1),
                    11,
                    digest=POINTER_DIGEST,
                ),
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 999, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                ),
            ]
        )
        self.assert_select_failure("artifacts", "missing")

    def test_expired_pointer_fails_select(self):
        self.add_ci_success()
        self.add_artifacts(
            [
                make_artifact(
                    MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
                    11,
                    digest=POINTER_DIGEST,
                    expired=True,
                ),
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                ),
            ]
        )
        self.assert_select_failure("artifacts", "expired")

    def test_duplicate_pointer_fails_select(self):
        self.add_ci_success()
        self.add_artifacts(
            [
                make_artifact(
                    MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
                    11,
                    digest=POINTER_DIGEST,
                ),
                make_artifact(
                    MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
                    12,
                    digest=POINTER_DIGEST,
                ),
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                ),
            ]
        )
        self.assert_select_failure("artifacts", "duplicate")

    def test_wrong_workflow_run_id_fails_select(self):
        self.add_ci_success()
        pointer = make_artifact(
            MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
            11,
            digest=POINTER_DIGEST,
        )
        pointer["workflow_run"]["id"] = 999
        self.add_artifacts(
            [
                pointer,
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                ),
            ]
        )
        self.assert_select_failure("artifacts", "identity")

    def test_wrong_head_sha_fails_select(self):
        self.add_ci_success()
        pointer = make_artifact(
            MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
            11,
            digest=POINTER_DIGEST,
        )
        pointer["workflow_run"]["head_sha"] = OTHER_SHA
        self.add_artifacts(
            [
                pointer,
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                ),
            ]
        )
        self.assert_select_failure("artifacts", "identity")

    def test_wrong_head_branch_fails_select(self):
        self.add_ci_success()
        pointer = make_artifact(
            MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
            11,
            digest=POINTER_DIGEST,
        )
        pointer["workflow_run"]["head_branch"] = "dev"
        self.add_artifacts(
            [
                pointer,
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                ),
            ]
        )
        self.assert_select_failure("artifacts", "identity")

    def test_wrong_repository_id_fails_select(self):
        self.add_ci_success()
        pointer = make_artifact(
            MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
            11,
            digest=POINTER_DIGEST,
        )
        pointer["workflow_run"]["repository_id"] = 999
        self.add_artifacts(
            [
                pointer,
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                ),
            ]
        )
        self.assert_select_failure("artifacts", "identity")

    def test_missing_digest_fails_select(self):
        self.add_ci_success()
        pointer = make_artifact(
            MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
            11,
            digest=POINTER_DIGEST,
        )
        del pointer["digest"]
        self.add_artifacts(
            [
                pointer,
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                ),
            ]
        )
        self.assert_select_failure("artifacts", "digest")

    def test_malformed_digest_fails_select(self):
        self.add_ci_success()
        self.add_artifacts(
            [
                make_artifact(
                    MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
                    11,
                    digest="sha256:not-a-digest",
                ),
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                ),
            ]
        )
        self.assert_select_failure("artifacts", "digest")

    def test_oversize_pointer_fails_select(self):
        self.add_ci_success()
        self.add_artifacts(
            [
                make_artifact(
                    MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
                    11,
                    size=MOD.MAX_POINTER_ARTIFACT_BYTES + 1,
                    digest=POINTER_DIGEST,
                ),
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                ),
            ]
        )
        self.assert_select_failure("artifacts", "size")

    def test_oversize_bundle_fails_select(self):
        self.add_ci_success()
        self.add_artifacts(
            [
                make_artifact(
                    MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
                    11,
                    digest=POINTER_DIGEST,
                ),
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    size=MOD.MAX_BUNDLE_ARTIFACT_BYTES + 1,
                    digest=BUNDLE_DIGEST,
                ),
            ]
        )
        self.assert_select_failure("artifacts", "size")

    def test_artifact_id_bool_fails_closed(self):
        self.add_ci_success()
        self.add_artifacts(
            [
                make_artifact(
                    MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
                    True,
                    digest=POINTER_DIGEST,
                ),
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                ),
            ]
        )
        self.assert_select_failure("artifacts", "schema")

    def test_zero_artifact_size_fails_closed(self):
        self.add_ci_success()
        self.add_artifacts(
            [
                make_artifact(
                    MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
                    11,
                    size=0,
                    digest=POINTER_DIGEST,
                ),
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                ),
            ]
        )
        self.assert_select_failure("artifacts", "schema")

    def test_artifacts_404_is_not_a_fallback(self):
        self.add_ci_success()
        self.fake.artifact_responses[(1001, 1)] = raw_response(404, b"{}")
        self.assert_select_failure("artifacts", "not-found")

    def test_malicious_artifacts_next_link_fails_closed(self):
        self.add_ci_success()
        page_one = [
            make_artifact(f"unrelated-{index}", 1000 + index)
            for index in range(100)
        ]
        self.fake.artifact_responses[(1001, 1)] = json_response(
            make_artifacts(101, page_one),
            headers={
                "link": '<https://evil.example/artifacts?page=2>; rel="next"'
            },
        )
        self.assert_select_failure("artifacts", "pagination")

    def test_artifact_page_count_mismatch_fails_closed(self):
        self.add_ci_success()
        self.add_artifacts(
            [make_artifact("only-one", 1)], total=2
        )
        self.assert_select_failure("artifacts", "pagination")

    def test_too_many_artifact_pages_fails_closed(self):
        self.add_ci_success()
        self.add_artifacts([], total=CI.MAX_PAGES * CI.PER_PAGE + 1)
        self.assert_select_failure("artifacts", "pagination-limit")

    def test_duplicate_artifact_ids_across_pages_fail_closed(self):
        self.add_ci_success()
        page_one = [
            make_artifact(f"unrelated-{index}", 1000 + index)
            for index in range(100)
        ]
        page_two = [make_artifact("duplicate-id", 1000)]
        self.fake.artifact_responses[(1001, 1)] = json_response(
            make_artifacts(101, page_one),
            headers={
                "link": (
                    '<https://api.github.com/repos/xtianxx/payment-hub/actions/'
                    'runs/1001/artifacts?per_page=100&page=2>; rel="next"'
                )
            },
        )
        self.fake.artifact_responses[(1001, 2)] = json_response(
            make_artifacts(101, page_two)
        )
        self.assert_select_failure("artifacts", "schema")

    def test_select_environment_must_be_trusted(self):
        cases = (
            ("event", {"GITHUB_EVENT_NAME": "push"}),
            ("ref", {"GITHUB_REF": "refs/heads/dev"}),
            ("repository", {"GITHUB_REPOSITORY": "evil/repo"}),
            ("sha", {"GITHUB_SHA": "not-a-sha"}),
            ("sha-mismatch", {"GITHUB_SHA": OTHER_SHA}),
            ("token", {"GITHUB_TOKEN": ""}),
        )
        for kind, overrides in cases:
            with self.subTest(kind=kind):
                self.setUp()
                self.add_ci_success()
                self.add_default_artifacts()
                self.assert_select_failure(
                    "env", kind, base_env(**overrides)
                )

    def test_select_invalid_source_sha_writes_failure_summary(self):
        code, stdout, _ = self.run_main(
            [
                "select",
                "--source-sha",
                "not-a-sha",
                "--summary",
                str(self.selection_path),
                "--github-output",
                str(self.output_path),
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "release-selection: fail stage=cli kind=source-sha\n",
        )
        data = self.selection_data()
        self.assertIsNone(data["source_sha"])
        self.assertEqual(
            data["failure"], {"stage": "cli", "kind": "source-sha"}
        )
        self.assertEqual(self.output_lines(), [])

    def test_current_run_wrong_name_fails_select(self):
        self.add_ci_success()
        self.add_artifacts(
            [
                make_artifact(
                    MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1) + "-x",
                    11,
                    digest=POINTER_DIGEST,
                ),
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                ),
            ]
        )
        self.assert_select_failure("artifacts", "missing")

    def test_artifact_repository_identity_mismatch_fails_select(self):
        self.add_ci_success()
        pointer = make_artifact(
            MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
            11,
            digest=POINTER_DIGEST,
        )
        pointer["workflow_run"]["head_repository_id"] = 999
        self.add_artifacts(
            [
                pointer,
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                ),
            ]
        )
        self.assert_select_failure("artifacts", "identity")

    def test_artifact_expired_field_missing_fails_select(self):
        self.add_ci_success()
        pointer = make_artifact(
            MOD._artifact_name(MOD.POINTER_PREFIX, 1001, 1),
            11,
            digest=POINTER_DIGEST,
        )
        del pointer["expired"]
        self.add_artifacts(
            [
                pointer,
                make_artifact(
                    MOD._artifact_name(MOD.BUNDLE_PREFIX, 1001, 1),
                    22,
                    digest=BUNDLE_DIGEST,
                ),
            ]
        )
        self.assert_select_failure("artifacts", "schema")

    def test_select_output_directory_is_rejected(self):
        self.add_ci_success()
        self.add_default_artifacts()
        self.output_path.mkdir()
        code, stdout, _ = self.run_select()
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "release-selection: fail stage=output kind=path\n",
        )
        self.assertEqual(
            self.selection_data()["failure"],
            {"stage": "output", "kind": "path"},
        )

    def test_select_does_not_leak_token_or_body(self):
        self.fake.workflow_responses["ci.yml"] = raw_response(
            500,
            b"RAW_SECRET_BODY " + TOKEN.encode("utf-8"),
        )
        code, stdout, stderr = self.run_select()
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "release-selection: fail stage=api kind=server-error\n",
        )
        self.assertEqual(stderr, "")
        text = self.selection_path.read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("RAW_SECRET_BODY", text)
        self.assertNotIn("https://", text)

    def test_select_summary_write_failure_leaves_no_output(self):
        self.add_ci_success()
        self.add_default_artifacts()
        self.output_path.write_text("previous=1", encoding="utf-8")
        with mock.patch.object(
            MOD.CI,
            "write_summary",
            side_effect=CI.PreflightError("summary", "write"),
        ):
            code, stdout, stderr = self.run_select()
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "release-selection: fail stage=summary kind=write\n",
        )
        self.assertEqual(stderr, "")
        self.assertEqual(
            self.output_path.read_text(encoding="utf-8"), "previous=1"
        )

    def test_select_output_write_failure_happens_after_summary(self):
        self.add_ci_success()
        self.add_default_artifacts()
        self.output_path.write_text("previous=1\n", encoding="utf-8")
        with mock.patch.object(
            MOD,
            "write_github_output",
            side_effect=CI.PreflightError("output", "write"),
        ):
            code, stdout, stderr = self.run_select()
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "release-selection: fail stage=output kind=write\n",
        )
        self.assertEqual(stderr, "")
        self.assertEqual(self.selection_data()["result"], "pass")
        self.assertEqual(
            self.output_path.read_text(encoding="utf-8"), "previous=1\n"
        )

    def test_select_partial_output_failure_rolls_back(self):
        self.add_ci_success()
        self.add_default_artifacts()
        self.output_path.write_text("previous=1\n", encoding="utf-8")
        calls = {"count": 0}

        def fsync(descriptor):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("simulated output fsync failure")

        with mock.patch.object(MOD.os, "fsync", side_effect=fsync):
            code, stdout, stderr = self.run_select()
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "release-selection: fail stage=output kind=write\n",
        )
        self.assertEqual(stderr, "")
        self.assertEqual(
            self.output_path.read_text(encoding="utf-8"), "previous=1\n"
        )

    def test_select_partial_output_failure_rolls_back_added_lf(self):
        self.add_ci_success()
        self.add_default_artifacts()
        self.output_path.write_text("previous=1", encoding="utf-8")
        calls = {"count": 0}

        def fsync(descriptor):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("simulated output fsync failure")

        with mock.patch.object(MOD.os, "fsync", side_effect=fsync):
            code, stdout, stderr = self.run_select()
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "release-selection: fail stage=output kind=write\n",
        )
        self.assertEqual(stderr, "")
        self.assertEqual(
            self.output_path.read_text(encoding="utf-8"), "previous=1"
        )

    def test_select_output_symlink_is_rejected(self):
        self.add_ci_success()
        self.add_default_artifacts()
        target = self.root / "output-target"
        target.write_text("", encoding="utf-8")
        self.output_path.symlink_to(target)
        self.assert_select_failure("output", "symlink")

    def test_write_github_output_rejects_newline_injection(self):
        path = self.root / "inject-output"
        with self.assertRaises(CI.PreflightError) as raised:
            MOD.write_github_output(
                path,
                [("ci_run_id", "1\nmalicious=1")],
                MOD.SELECT_OUTPUT_KEYS,
            )
        self.assertEqual(
            (raised.exception.stage, raised.exception.kind),
            ("output", "value"),
        )
        self.assertFalse(path.exists())

    def test_write_github_output_rejects_unknown_key(self):
        path = self.root / "inject-key"
        with self.assertRaises(CI.PreflightError) as raised:
            MOD.write_github_output(
                path,
                [("malicious", "1")],
                MOD.SELECT_OUTPUT_KEYS,
            )
        self.assertEqual(
            (raised.exception.stage, raised.exception.kind),
            ("output", "value"),
        )

    def test_verify_output_digest_must_be_canonical(self):
        canonical_path = self.root / "canonical-digest-output"
        MOD.write_github_output(
            canonical_path,
            [("bundle_artifact_digest", BUNDLE_DIGEST)],
            MOD.VERIFY_OUTPUT_KEYS,
        )
        self.assertEqual(
            canonical_path.read_text(encoding="utf-8"),
            f"bundle_artifact_digest={BUNDLE_DIGEST}\n",
        )

        bare_path = self.root / "bare-digest-output"
        with self.assertRaises(CI.PreflightError) as raised:
            MOD.write_github_output(
                bare_path,
                [("bundle_artifact_digest", BUNDLE_DIGEST.split(":", 1)[1])],
                MOD.VERIFY_OUTPUT_KEYS,
            )
        self.assertEqual(
            (raised.exception.stage, raised.exception.kind),
            ("output", "value"),
        )


class VerifySuccessTests(SelectorTestCase):
    def test_select_then_verify_round_trip(self):
        self.add_ci_success()
        self.add_default_artifacts()
        self.assertEqual(self.run_select()[0], 0)
        self.write_pointer()
        code, stdout, stderr = self.run_main(self.verify_argv())
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "release-selection: verify pass\n")
        self.assertEqual(stderr, "")
        self.assertEqual(
            self.output_lines(self.verify_output_path),
            [
                "ci_run_id=1001",
                "ci_run_attempt=1",
                "bundle_artifact_id=22",
                f"descriptor_sha256={DESCRIPTOR_SHA}",
                f"bundle_artifact_digest={BUNDLE_DIGEST}",
            ],
        )

    def test_verify_accepts_fixed_provenance_selection(self):
        self.write_selection()
        self.write_pointer()
        code, stdout, _ = self.run_main(self.verify_argv())
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "release-selection: verify pass\n")

    def test_verify_uses_pointer_descriptor_digest(self):
        self.write_selection()
        self.write_pointer(
            {
                "schema": MOD.POINTER_SCHEMA,
                "repository": MOD.REPOSITORY,
                "workflow_path": MOD.WORKFLOW_PATH,
                "source_sha": SHA,
                "platform": MOD.PLATFORM,
                "run_id": self.run_id,
                "run_attempt": self.run_attempt,
                "bundle_artifact_id": 22,
                "bundle_artifact_digest": BUNDLE_DIGEST,
                "descriptor_sha256": "7" * 64,
            }
        )
        code, stdout, _ = self.run_main(self.verify_argv())
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "release-selection: verify pass\n")
        self.assertIn(
            f"descriptor_sha256={'7' * 64}",
            self.output_lines(self.verify_output_path),
        )

    def test_verify_output_contains_no_token(self):
        self.write_selection()
        self.write_pointer()
        code, _, _ = self.run_main(self.verify_argv())
        self.assertEqual(code, 0)
        text = self.verify_output_path.read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, text)


class VerifyFailureTests(SelectorTestCase):
    def setUp(self):
        super().setUp()
        self.write_selection()
        self.write_pointer()

    def assert_verify_failure(self, stage, kind, environment=None):
        code, stdout, stderr = self.run_main(
            self.verify_argv(), environment
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            f"release-selection: fail stage={stage} kind={kind}\n",
        )
        self.assertEqual(stderr, "")
        if not self.verify_output_path.is_symlink() and not (
            self.verify_output_path.exists()
            and self.verify_output_path.is_dir()
        ):
            self.assertFalse(self.verify_output_path.exists())

    def mutate_pointer(self, **overrides):
        data = json.loads(self.pointer_path.read_text(encoding="utf-8"))
        data.update(overrides)
        self.write_pointer(data)

    def mutate_selection(self, **overrides):
        data = json.loads(self.selection_path.read_text(encoding="utf-8"))
        data.update(overrides)
        self.write_selection(data)

    def test_verify_repeats_trusted_environment(self):
        cases = (
            ("event", {"GITHUB_EVENT_NAME": "push"}),
            ("ref", {"GITHUB_REF": "refs/heads/dev"}),
            ("repository", {"GITHUB_REPOSITORY": "evil/repo"}),
            ("sha", {"GITHUB_SHA": "not-a-sha"}),
            ("sha-mismatch", {"GITHUB_SHA": OTHER_SHA}),
        )
        for kind, overrides in cases:
            with self.subTest(kind=kind):
                self.assert_verify_failure(
                    "env", kind, base_env(**overrides)
                )

    def test_verify_accepts_workflow_context_without_token(self):
        environment = base_env()
        del environment["GITHUB_TOKEN"]
        code, stdout, _ = self.run_main(self.verify_argv(), environment)
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "release-selection: verify pass\n")

    def test_verify_validates_token_when_present(self):
        self.assert_verify_failure(
            "env", "token", base_env(GITHUB_TOKEN="bad token")
        )

    def test_verify_checks_pointer_schema_and_identity(self):
        cases = (
            ("schema", {"schema": "other"}),
            ("repository", {"repository": "evil/repo"}),
            ("workflow-path", {"workflow_path": ".github/workflows/old.yml"}),
            ("source-sha", {"source_sha": OTHER_SHA}),
            ("platform", {"platform": "linux/arm64"}),
            ("run-id", {"run_id": 999}),
            ("run-attempt", {"run_attempt": 2}),
            ("bundle-artifact-id", {"bundle_artifact_id": 99}),
            ("bundle-artifact-digest", {"bundle_artifact_digest": "sha256:" + "4" * 64}),
        )
        for kind, overrides in cases:
            with self.subTest(kind=kind):
                self.setUp()
                self.write_pointer()
                self.mutate_pointer(**overrides)
                expected = (
                    "selection-mismatch"
                    if kind in (
                        "run-id",
                        "run-attempt",
                        "bundle-artifact-id",
                        "bundle-artifact-digest",
                    )
                    else kind
                )
                self.assert_verify_failure("pointer", expected)

    def test_verify_rejects_pointer_schema_drift(self):
        data = json.loads(self.pointer_path.read_text(encoding="utf-8"))
        data["extra"] = "value"
        self.write_pointer(data)
        self.assert_verify_failure("pointer", "schema")

        self.setUp()
        self.write_pointer()
        data = json.loads(self.pointer_path.read_text(encoding="utf-8"))
        del data["descriptor_sha256"]
        self.write_pointer(data)
        self.assert_verify_failure("pointer", "schema")

    def test_verify_rejects_bad_pointer_id_types(self):
        for value in (True, "22", 22.0, 0, -1):
            with self.subTest(value=value):
                self.setUp()
                self.write_pointer()
                self.mutate_pointer(bundle_artifact_id=value)
                self.assert_verify_failure("pointer", "schema")

    def test_verify_rejects_bad_pointer_hashes(self):
        cases = (
            ({"bundle_artifact_digest": "sha256:" + "A" * 64}, "digest"),
            ({"bundle_artifact_digest": "1" * 64}, "digest"),
            ({"descriptor_sha256": "3" * 63}, "digest"),
            ({"descriptor_sha256": "sha256:" + "3" * 64}, "digest"),
        )
        for overrides, kind in cases:
            with self.subTest(overrides=overrides):
                self.setUp()
                self.write_pointer()
                self.mutate_pointer(**overrides)
                self.assert_verify_failure("pointer", kind)

    def test_verify_rejects_pointer_with_selection_mismatch(self):
        self.mutate_selection(ci_run_id=2002)
        self.assert_verify_failure("pointer", "selection-mismatch")

        self.setUp()
        self.mutate_selection(bundle_artifact_id=99)
        self.assert_verify_failure("pointer", "selection-mismatch")

        self.setUp()
        self.mutate_selection(
            bundle_artifact_digest="sha256:" + "6" * 64
        )
        self.assert_verify_failure("pointer", "selection-mismatch")

    def test_verify_checks_selection_shape_and_provenance(self):
        cases = (
            ("schema", {"schema": "other"}),
            ("provenance", {"repository": "evil/repo"}),
            ("provenance", {"workflow_path": ".github/workflows/old.yml"}),
            ("provenance", {"platform": "linux/arm64"}),
            ("identity", {"source_sha": OTHER_SHA}),
            ("result", {"result": "fail"}),
            ("failure", {"failure": {"stage": "runs", "kind": "not-success"}}),
        )
        for kind, overrides in cases:
            with self.subTest(kind=kind, overrides=overrides):
                self.setUp()
                self.write_selection()
                self.write_pointer()
                self.mutate_selection(**overrides)
                self.assert_verify_failure("selection", kind)

    def test_verify_rejects_selection_extra_field(self):
        data = json.loads(self.selection_path.read_text(encoding="utf-8"))
        data["archive_download_url"] = "https://example.invalid"
        self.write_selection(data)
        self.assert_verify_failure("selection", "schema")

    def test_verify_rejects_selection_bad_digest_and_ids(self):
        cases = (
            ({"pointer_artifact_digest": "1" * 64}, "digest"),
            ({"bundle_artifact_digest": "sha256:" + "A" * 64}, "digest"),
            ({"ci_run_id": True}, "schema"),
            ({"ci_run_attempt": "1"}, "schema"),
            ({"pointer_artifact_id": 0}, "schema"),
            ({"bundle_artifact_id": 11}, "identity"),
        )
        for overrides, kind in cases:
            with self.subTest(overrides=overrides):
                self.setUp()
                self.write_selection()
                self.write_pointer()
                self.mutate_selection(**overrides)
                self.assert_verify_failure("selection", kind)

    def test_verify_rejects_selection_symlink(self):
        target = self.root / "selection-target.json"
        target.write_text(
            self.selection_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.selection_path.unlink()
        self.selection_path.symlink_to(target)
        self.assert_verify_failure("selection", "symlink")

    def test_verify_rejects_pointer_symlink(self):
        target = self.root / "pointer-target.json"
        target.write_text(
            self.pointer_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.pointer_path.unlink()
        self.pointer_path.symlink_to(target)
        self.assert_verify_failure("pointer", "symlink")

    def test_verify_rejects_oversize_selection(self):
        self.selection_path.write_text(
            "{" + "x" * (MOD.MAX_SELECTION_BYTES + 1),
            encoding="utf-8",
        )
        self.assert_verify_failure("selection", "body-too-large")

    def test_verify_rejects_oversize_pointer(self):
        self.pointer_path.write_text(
            "{" + "x" * (MOD.MAX_POINTER_BYTES + 1),
            encoding="utf-8",
        )
        self.assert_verify_failure("pointer", "body-too-large")

    def test_verify_output_adds_separator_lf_for_existing_no_lf(self):
        self.write_selection()
        self.write_pointer()
        self.verify_output_path.write_text("existing=1", encoding="utf-8")
        code, stdout, stderr = self.run_main(self.verify_argv())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "release-selection: verify pass\n")
        text = self.verify_output_path.read_text(encoding="utf-8")
        self.assertEqual(
            text,
            "existing=1\n"
            "ci_run_id=1001\n"
            "ci_run_attempt=1\n"
            "bundle_artifact_id=22\n"
            f"descriptor_sha256={DESCRIPTOR_SHA}\n"
            f"bundle_artifact_digest={BUNDLE_DIGEST}\n",
        )
        self.assertNotIn("existing=1ci_run_id", text)

    def test_verify_output_write_failure_rolls_back(self):
        self.write_selection()
        self.write_pointer()
        self.verify_output_path.write_text("previous=1\n", encoding="utf-8")
        calls = {"count": 0}

        def fsync(descriptor):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError("simulated verify output fsync failure")

        with mock.patch.object(MOD.os, "fsync", side_effect=fsync):
            code, stdout, stderr = self.run_main(self.verify_argv())
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "release-selection: fail stage=output kind=write\n",
        )
        self.assertEqual(stderr, "")
        self.assertEqual(
            self.verify_output_path.read_text(encoding="utf-8"),
            "previous=1\n",
        )

    def test_verify_rejects_output_symlink(self):
        target = self.root / "verify-target"
        target.write_text("", encoding="utf-8")
        self.verify_output_path.symlink_to(target)
        self.assert_verify_failure("output", "symlink")

    def test_verify_rejects_output_directory(self):
        self.verify_output_path.mkdir()
        self.assert_verify_failure("output", "path")

    def test_verify_does_not_leak_token(self):
        data = json.loads(self.pointer_path.read_text(encoding="utf-8"))
        data["token"] = TOKEN
        self.write_pointer(data)
        code, stdout, stderr = self.run_main(self.verify_argv())
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, stdout)
        self.assertNotIn(TOKEN, stderr)
        self.assertFalse(self.verify_output_path.exists())

    def test_verify_rejects_missing_selection(self):
        self.selection_path.unlink()
        self.assert_verify_failure("selection", "path")

    def test_verify_rejects_missing_pointer(self):
        self.pointer_path.unlink()
        self.assert_verify_failure("pointer", "path")

    def test_verify_rejects_non_object_selection(self):
        self.selection_path.write_text("[]", encoding="utf-8")
        self.assert_verify_failure("selection", "schema")

    def test_verify_rejects_bad_json_pointer(self):
        self.pointer_path.write_text("{bad", encoding="utf-8")
        self.assert_verify_failure("pointer", "bad-json")


class CrossModuleIntegrationTests(SelectorTestCase):
    """Offline interop: A's pointer CLI -> selector select -> selector verify."""

    PRODUCER_RUN_ID = 4242
    PRODUCER_RUN_ATTEMPT = 3
    POINTER_ARTIFACT_ID = 9001
    BUNDLE_ARTIFACT_ID = 222
    DESCRIPTOR_SHA = "3" * 64
    BUNDLE_DIGEST = "sha256:" + "2" * 64
    WORKFLOW_REF = (
        "xtianxx/payment-hub/.github/workflows/ci.yml@refs/heads/main"
    )

    def producer_env(self):
        return {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(self.root)),
            "PYTHONDONTWRITEBYTECODE": "1",
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": MOD.REPOSITORY,
            "GITHUB_WORKFLOW_REF": self.WORKFLOW_REF,
            "GITHUB_SHA": SHA,
            "GITHUB_RUN_ID": str(self.PRODUCER_RUN_ID),
            "GITHUB_RUN_ATTEMPT": str(self.PRODUCER_RUN_ATTEMPT),
        }

    def consumer_event_env(self):
        environment = self.producer_env()
        environment["GITHUB_EVENT_NAME"] = "workflow_dispatch"
        return environment

    def consumer_select_env(self):
        return base_env()

    def consumer_verify_env(self):
        environment = base_env()
        environment.pop("GITHUB_TOKEN", None)
        return environment

    def pointer_command(self, output):
        return [
            sys.executable,
            "-B",
            str(REPO / "scripts" / "ci-candidate.py"),
            "pointer",
            "--source-sha",
            SHA,
            "--descriptor-sha256",
            self.DESCRIPTOR_SHA,
            "--bundle-artifact-id",
            str(self.BUNDLE_ARTIFACT_ID),
            "--bundle-artifact-digest",
            self.BUNDLE_DIGEST,
            "--output",
            str(output),
        ]

    def generate_pointer_with_a(self):
        output = self.root / "producer" / "pointer.json"
        completed = subprocess.run(
            self.pointer_command(output),
            cwd=str(REPO),
            env=self.producer_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")
        return output, json.loads(output.read_text(encoding="utf-8"))

    def assert_verify_rejected(self, name, stage, kind):
        self.verify_output_path = self.root / f"reject-{name}"
        code, stdout, stderr = self.run_main(
            self.verify_argv(), self.consumer_verify_env()
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            f"release-selection: fail stage={stage} kind={kind}\n",
        )
        self.assertEqual(stderr, "")
        self.assertFalse(self.verify_output_path.exists())

    def test_pointer_cli_rejects_missing_or_wrong_workflow_ref(self):
        cases = (
            ("missing", None),
            (
                "wrong",
                "xtianxx/payment-hub/.github/workflows/old.yml"
                "@refs/heads/main",
            ),
        )
        for name, workflow_ref in cases:
            with self.subTest(name=name):
                output = self.root / f"workflow-ref-{name}" / "pointer.json"
                environment = self.producer_env()
                if workflow_ref is None:
                    environment.pop("GITHUB_WORKFLOW_REF", None)
                else:
                    environment["GITHUB_WORKFLOW_REF"] = workflow_ref
                completed = subprocess.run(
                    self.pointer_command(output),
                    cwd=str(REPO),
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(
                    completed.stderr, "ERROR: context/workflow-ref\n"
                )
                self.assertEqual(
                    completed.stdout,
                    "result: fail stage=context kind=workflow-ref\n",
                )
                self.assertFalse(output.exists())

    def test_a_pointer_select_verify_contract_and_mutations(self):
        producer = self.producer_env()
        for checker in (CI.validate_environment, MOD.validate_trusted_context):
            with self.assertRaises(CI.PreflightError) as raised:
                checker(producer, SHA)
            self.assertEqual(
                (raised.exception.stage, raised.exception.kind),
                ("env", "event"),
            )

        consumer_pointer = self.root / "consumer-pointer" / "pointer.json"
        completed = subprocess.run(
            self.pointer_command(consumer_pointer),
            cwd=str(REPO),
            env=self.consumer_event_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, "ERROR: context/event\n")
        self.assertEqual(
            completed.stdout,
            "result: fail stage=context kind=event\n",
        )
        self.assertFalse(consumer_pointer.exists())

        pointer_path, pointer = self.generate_pointer_with_a()
        self.assertEqual(set(pointer), MOD.POINTER_KEYS)
        self.assertEqual(pointer["schema"], MOD.POINTER_SCHEMA)
        self.assertEqual(pointer["repository"], MOD.REPOSITORY)
        self.assertEqual(pointer["workflow_path"], MOD.WORKFLOW_PATH)
        self.assertEqual(pointer["source_sha"], SHA)
        self.assertEqual(pointer["platform"], MOD.PLATFORM)
        self.assertEqual(pointer["run_id"], self.PRODUCER_RUN_ID)
        self.assertEqual(pointer["run_attempt"], self.PRODUCER_RUN_ATTEMPT)
        self.assertEqual(pointer["bundle_artifact_id"], self.BUNDLE_ARTIFACT_ID)
        self.assertEqual(pointer["bundle_artifact_digest"], self.BUNDLE_DIGEST)
        self.assertEqual(pointer["descriptor_sha256"], self.DESCRIPTOR_SHA)

        self.run_id = self.PRODUCER_RUN_ID
        self.run_attempt = self.PRODUCER_RUN_ATTEMPT
        self.add_ci_success(run_id=self.run_id, attempt=self.run_attempt)
        self.add_artifacts(
            [
                make_artifact(
                    MOD._artifact_name(
                        MOD.POINTER_PREFIX, self.run_id, self.run_attempt
                    ),
                    self.POINTER_ARTIFACT_ID,
                    size=128,
                    digest=POINTER_DIGEST,
                    run_id=self.run_id,
                ),
                make_artifact(
                    MOD._artifact_name(
                        MOD.BUNDLE_PREFIX, self.run_id, self.run_attempt
                    ),
                    self.BUNDLE_ARTIFACT_ID,
                    size=2048,
                    digest=self.BUNDLE_DIGEST,
                    run_id=self.run_id,
                ),
            ],
            run_id=self.run_id,
        )

        code, stdout, stderr = self.run_main(
            self.select_argv(), self.consumer_select_env()
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "release-selection: select pass\n")
        selection_path = self.selection_path
        selection = self.selection_data()
        self.assertEqual(selection["schema"], MOD.SELECTION_SCHEMA)
        self.assertEqual(selection["result"], "pass")
        self.assertEqual(selection["source_sha"], SHA)
        self.assertEqual(selection["ci_run_id"], self.PRODUCER_RUN_ID)
        self.assertEqual(selection["ci_run_attempt"], self.PRODUCER_RUN_ATTEMPT)
        self.assertEqual(
            selection["pointer_artifact_id"], self.POINTER_ARTIFACT_ID
        )
        self.assertEqual(selection["pointer_artifact_digest"], POINTER_DIGEST)
        self.assertEqual(selection["bundle_artifact_id"], self.BUNDLE_ARTIFACT_ID)
        self.assertEqual(selection["bundle_artifact_digest"], self.BUNDLE_DIGEST)

        self.pointer_path = pointer_path
        code, stdout, stderr = self.run_main(
            self.verify_argv(), self.consumer_verify_env()
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "release-selection: verify pass\n")
        lines = self.output_lines(self.verify_output_path)
        self.assertEqual(
            lines,
            [
                f"ci_run_id={self.PRODUCER_RUN_ID}",
                f"ci_run_attempt={self.PRODUCER_RUN_ATTEMPT}",
                f"bundle_artifact_id={self.BUNDLE_ARTIFACT_ID}",
                f"descriptor_sha256={self.DESCRIPTOR_SHA}",
                f"bundle_artifact_digest={self.BUNDLE_DIGEST}",
            ],
        )
        values = dict(line.split("=", 1) for line in lines)
        self.assertRegex(values["ci_run_id"], r"^[1-9][0-9]*$")
        self.assertRegex(values["ci_run_attempt"], r"^[1-9][0-9]*$")
        self.assertRegex(values["bundle_artifact_id"], r"^[1-9][0-9]*$")
        self.assertRegex(values["descriptor_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(values["bundle_artifact_digest"], r"^sha256:[0-9a-f]{64}$")

        pointer_mutations = (
            ("source", {"source_sha": OTHER_SHA}, "pointer", "source-sha"),
            (
                "run-id",
                {"run_id": self.PRODUCER_RUN_ID + 1},
                "pointer",
                "selection-mismatch",
            ),
            (
                "run-attempt",
                {"run_attempt": self.PRODUCER_RUN_ATTEMPT + 1},
                "pointer",
                "selection-mismatch",
            ),
            (
                "digest",
                {"bundle_artifact_digest": "sha256:" + "4" * 64},
                "pointer",
                "selection-mismatch",
            ),
        )
        for name, overrides, stage, kind in pointer_mutations:
            with self.subTest(pointer=name):
                mutated = dict(pointer)
                mutated.update(overrides)
                mutated_path = self.root / f"mutated-pointer-{name}.json"
                mutated_path.write_text(json.dumps(mutated), encoding="utf-8")
                self.pointer_path = mutated_path
                self.selection_path = selection_path
                self.assert_verify_rejected(name, stage, kind)

        selection_mutations = (
            ("source", {"source_sha": OTHER_SHA}, "selection", "identity"),
            (
                "run-id",
                {"ci_run_id": self.PRODUCER_RUN_ID + 1},
                "pointer",
                "selection-mismatch",
            ),
            (
                "run-attempt",
                {"ci_run_attempt": self.PRODUCER_RUN_ATTEMPT + 1},
                "pointer",
                "selection-mismatch",
            ),
            (
                "digest",
                {"bundle_artifact_digest": "sha256:" + "4" * 64},
                "pointer",
                "selection-mismatch",
            ),
        )
        for name, overrides, stage, kind in selection_mutations:
            with self.subTest(selection=name):
                mutated = dict(selection)
                mutated.update(overrides)
                mutated_path = self.root / f"mutated-selection-{name}.json"
                mutated_path.write_text(json.dumps(mutated), encoding="utf-8")
                self.pointer_path = pointer_path
                self.selection_path = mutated_path
                self.assert_verify_rejected(f"selection-{name}", stage, kind)


if __name__ == "__main__":
    unittest.main(verbosity=2)
