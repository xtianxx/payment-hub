"""Standard-library tests for scripts/check-main-ci.py.

All HTTP is faked in memory.  These tests make no network request, never use a
real GitHub token, and never touch the repository's own files.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import ssl
import stat
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "check_main_ci",
    REPO / "scripts" / "check-main-ci.py",
)
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)

TOKEN = "ghs_test_token_1234567890"
SHA = "a" * 40
OLDER = "2026-01-01T00:00:00Z"
LATEST = "2026-01-02T00:00:00Z"


def make_workflow(path, workflow_id, state="active"):
    return {"id": workflow_id, "path": path, "state": state}


def make_run(
    run_id,
    workflow_id,
    path,
    *,
    attempt=1,
    status="completed",
    conclusion="success",
    sha=SHA,
    branch="main",
    event="push",
    repo=MOD.REPOSITORY,
    created_at=LATEST,
    run_path=None,
    **extra,
):
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
        "repository": {"full_name": repo},
        "pull_requests": [],
    }
    data.update(extra)
    return data


def make_runs(total, runs):
    return {"total_count": total, "workflow_runs": runs}


def json_response(data, status=200, headers=None):
    merged = {"content-type": "application/json; charset=utf-8"}
    if headers:
        merged.update({str(k).lower(): str(v) for k, v in headers.items()})
    return MOD.HttpResponse(
        status=status,
        headers=merged,
        body=json.dumps(data).encode("utf-8"),
    )


def raw_response(status, body=b"", headers=None):
    merged = {"content-type": "text/plain"}
    if headers:
        merged.update({str(k).lower(): str(v) for k, v in headers.items()})
    return MOD.HttpResponse(status=status, headers=merged, body=body)


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
        self.requests = []
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
            query = urllib.parse.parse_qs(parsed.query)
            page = int(query["page"][0])
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
        raise AssertionError(f"unexpected API request: {url}")


class FakeHTTPResponse:
    def __init__(self, status=200, headers=None, body=b"", error=None):
        self.status = status
        self.headers = list((headers or {}).items())
        self.body = body
        self.error = error
        self.position = 0

    def getheaders(self):
        return list(self.headers)

    def read(self, size=-1):
        if self.error is not None:
            raise self.error
        if size is None or size < 0:
            size = len(self.body) - self.position
        chunk = self.body[self.position : self.position + size]
        self.position += len(chunk)
        return chunk


class FakeConnection:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []
        self.closed = False

    def request(self, method, target, headers):
        self.requests.append((method, target, dict(headers)))
        if self.error is not None:
            raise self.error

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class GateTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.summary_path = self.root / "gate" / "ci-preflight.json"
        self.fake = FakeGitHub()
        self.api = MOD.GitHubApi(TOKEN, transport=self.fake.transport)
        self.config = MOD.Config(
            source_sha=SHA,
            summary_path=self.summary_path,
        )
        self.states = MOD.default_workflow_states()

    def gate(self):
        return MOD.run_gate(self.config, self.api, self.states)

    def summary_data(self, failure):
        return MOD.build_summary(
            result="pass" if failure is None else "fail",
            failure=failure,
            source_sha=SHA,
            workflows=self.states,
        )

    def add_success(self, path, workflow_id, run_id, *, attempt=1, created_at=LATEST):
        self.fake.workflow_responses[path.rsplit("/", 1)[-1]] = json_response(
            make_workflow(path, workflow_id)
        )
        run = make_run(
            run_id,
            workflow_id,
            path,
            attempt=attempt,
            created_at=created_at,
        )
        self.fake.page_responses[(workflow_id, 1)] = json_response(
            make_runs(1, [run])
        )
        self.fake.attempt_responses[(run_id, attempt)] = json_response(run)
        return run

    def add_both_success(self):
        self.add_success(MOD.WORKFLOW_PATHS[0], 101, 1001)
        self.add_success(MOD.WORKFLOW_PATHS[1], 202, 2002)

    def run_main(self, environment, argv=None, transport=None):
        if argv is None:
            argv = [
                "--source-sha",
                SHA,
                "--summary",
                str(self.summary_path),
            ]
        stdout = io.StringIO()
        stderr = io.StringIO()
        selected = transport or self.fake.transport
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(MOD, "HttpsTransport", lambda: selected),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = MOD.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()


class SummaryTests(GateTestCase):
    def test_pass_summary_is_fixed_whitelist(self):
        data = MOD.build_summary(
            result="pass",
            failure=None,
            source_sha=SHA,
            workflows=MOD.default_workflow_states(),
        )
        self.assertEqual(
            set(data),
            {"schema", "result", "failure", "source_sha", "workflows"},
        )
        self.assertEqual(data["schema"], MOD.SUMMARY_SCHEMA)
        self.assertEqual(data["result"], "pass")
        self.assertIsNone(data["failure"])
        self.assertEqual(data["source_sha"], SHA)
        self.assertEqual(len(data["workflows"]), 2)
        for entry in data["workflows"]:
            self.assertEqual(
                set(entry), {"path", "workflow_id", "latest_run"}
            )
            self.assertIsNone(entry["workflow_id"])
            self.assertIsNone(entry["latest_run"])

    def test_failure_summary_uses_safe_stage_and_kind(self):
        data = MOD.build_summary(
            result="fail",
            failure=("runs", "not-success"),
            source_sha=SHA,
            workflows=MOD.default_workflow_states(),
        )
        self.assertEqual(data["result"], "fail")
        self.assertEqual(
            data["failure"], {"stage": "runs", "kind": "not-success"}
        )

    def test_unknown_failure_is_sanitized(self):
        data = MOD.build_summary(
            result="fail",
            failure=(TOKEN, "raw-body"),
            source_sha=TOKEN,
            workflows=MOD.default_workflow_states(),
        )
        self.assertEqual(
            data["failure"], {"stage": "internal", "kind": "unexpected"}
        )
        self.assertIsNone(data["source_sha"])

    def test_pass_with_failure_is_forced_to_fail(self):
        data = MOD.build_summary(
            result="pass",
            failure=("runs", "missing"),
            source_sha=SHA,
            workflows=MOD.default_workflow_states(),
        )
        self.assertEqual(data["result"], "fail")

    def test_fail_without_failure_is_internal(self):
        data = MOD.build_summary(
            result="fail",
            failure=None,
            source_sha=SHA,
            workflows=MOD.default_workflow_states(),
        )
        self.assertEqual(
            data["failure"], {"stage": "internal", "kind": "unexpected"}
        )

    def test_state_summary_drops_non_whitelisted_values(self):
        state = MOD.WorkflowState(
            path=MOD.WORKFLOW_PATHS[0],
            workflow_id=True,
            latest_run_id=7,
            latest_run_attempt=0,
            latest_run_status=TOKEN,
            latest_run_conclusion=TOKEN,
        )
        data = MOD.build_summary(
            result="fail",
            failure=("internal", "unexpected"),
            source_sha=TOKEN,
            workflows=[state, MOD.WorkflowState(path=TOKEN)],
        )
        text = json.dumps(data)
        self.assertNotIn(TOKEN, text)
        self.assertEqual(
            [entry["path"] for entry in data["workflows"]],
            list(MOD.WORKFLOW_PATHS),
        )
        entry = data["workflows"][0]
        self.assertIsNone(entry["workflow_id"])
        self.assertEqual(entry["latest_run"]["run_id"], 7)
        self.assertIsNone(entry["latest_run"]["run_attempt"])
        self.assertIsNone(entry["latest_run"]["status"])
        self.assertIsNone(entry["latest_run"]["conclusion"])

    def test_validate_summary_requires_absolute_fresh_path(self):
        relative = "relative/ci-preflight.json"
        with self.assertRaises(MOD.PreflightError) as raised:
            MOD.validate_summary_path(relative)
        self.assertEqual((raised.exception.stage, raised.exception.kind),
                         ("cli", "summary-path"))

        parent = self.root / "parent"
        parent.mkdir()
        (parent / "other").write_text("x", encoding="utf-8")
        with self.assertRaises(MOD.PreflightError) as raised:
            MOD.validate_summary_path(str(parent / "ci-preflight.json"))
        self.assertEqual((raised.exception.stage, raised.exception.kind),
                         ("cli", "summary-not-fresh"))

        existing_parent = self.root / "existing"
        existing_parent.mkdir()
        existing = existing_parent / "ci-preflight.json"
        existing.write_text("{}", encoding="utf-8")
        with self.assertRaises(MOD.PreflightError) as raised:
            MOD.validate_summary_path(str(existing))
        self.assertEqual((raised.exception.stage, raised.exception.kind),
                         ("cli", "summary-exists"))

    def test_validate_summary_accepts_pathlike(self):
        path = MOD.validate_summary_path(self.summary_path)
        self.assertEqual(path, self.summary_path)

    def test_validate_summary_rejects_symlink_path(self):
        parent = self.root / "symlink-path"
        parent.mkdir()
        target = parent / "target.json"
        target.write_text("{}", encoding="utf-8")
        link = parent / "ci-preflight.json"
        link.symlink_to(target)
        with self.assertRaises(MOD.PreflightError) as raised:
            MOD.validate_summary_path(str(link))
        self.assertEqual((raised.exception.stage, raised.exception.kind),
                         ("cli", "summary-symlink"))

    def test_validate_summary_rejects_symlink_parent(self):
        real = self.root / "real-parent"
        real.mkdir()
        link = self.root / "linked-parent"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(MOD.PreflightError) as raised:
            MOD.validate_summary_path(str(link / "ci-preflight.json"))
        self.assertEqual((raised.exception.stage, raised.exception.kind),
                         ("cli", "summary-symlink"))

    def test_write_summary_is_private_atomic_and_fresh(self):
        path = MOD.validate_summary_path(str(self.summary_path))
        data = MOD.build_summary(
            result="pass",
            failure=None,
            source_sha=SHA,
            workflows=MOD.default_workflow_states(),
        )
        MOD.write_summary(path, data)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), data)
        self.assertEqual(
            [entry.name for entry in path.parent.iterdir()],
            [path.name],
        )

    def test_write_summary_refuses_existing_entry(self):
        path = MOD.validate_summary_path(str(self.summary_path))
        path.write_text("already-here", encoding="utf-8")
        with self.assertRaises(MOD.PreflightError) as raised:
            MOD.write_summary(path, {"schema": MOD.SUMMARY_SCHEMA})
        self.assertEqual((raised.exception.stage, raised.exception.kind),
                         ("summary", "exists"))
        self.assertEqual(path.read_text(encoding="utf-8"), "already-here")

    def test_write_summary_failure_cleans_temporary_file(self):
        path = MOD.validate_summary_path(str(self.summary_path))
        with mock.patch.object(
            MOD.os,
            "link",
            side_effect=OSError("simulated link failure"),
        ):
            with self.assertRaises(MOD.PreflightError) as raised:
                MOD.write_summary(path, {"schema": MOD.SUMMARY_SCHEMA})
        self.assertEqual((raised.exception.stage, raised.exception.kind),
                         ("summary", "write"))
        self.assertFalse(path.exists())
        self.assertEqual(list(path.parent.iterdir()), [])


class HttpTests(unittest.TestCase):
    def transport(self, connection):
        return MOD.HttpsTransport(
            connection_factory=lambda host, timeout, context: connection
        )

    def test_authorization_is_sent_only_to_api_github_com(self):
        connection = FakeConnection(
            FakeHTTPResponse(
                200,
                {"Content-Type": "application/json"},
                b"{}",
            )
        )
        transport = self.transport(connection)
        response = transport.get(
            "https://api.github.com/repos/xtianxx/payment-hub/actions/workflows/x.yml",
            TOKEN,
            5.0,
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(len(connection.requests), 1)
        method, target, headers = connection.requests[0]
        self.assertEqual(method, "GET")
        self.assertEqual(
            target,
            "/repos/xtianxx/payment-hub/actions/workflows/x.yml",
        )
        self.assertEqual(headers["Authorization"], f"Bearer {TOKEN}")

    def test_foreign_hosts_and_schemes_are_rejected_before_connecting(self):
        calls = []

        def factory(host, timeout, context):
            calls.append(host)
            return FakeConnection()

        transport = MOD.HttpsTransport(connection_factory=factory)
        for url in (
            "https://evil.example/repos/x",
            "https://api.github.com.evil.example/repos/x",
            "https://user:pass@api.github.com/repos/x",
            "https://api.github.com:8443/repos/x",
            "http://api.github.com/repos/x",
        ):
            with self.subTest(url=url):
                with self.assertRaises(MOD.PreflightError) as raised:
                    transport.get(url, TOKEN, 5.0)
                self.assertEqual(
                    (raised.exception.stage, raised.exception.kind),
                    ("api", "host"),
                )
        self.assertEqual(calls, [])

    def test_invalid_token_is_rejected(self):
        transport = self.transport(FakeConnection())
        with self.assertRaises(MOD.PreflightError) as raised:
            transport.get("https://api.github.com/repos/x", "bad token", 5.0)
        self.assertEqual((raised.exception.stage, raised.exception.kind),
                         ("env", "token"))

    def test_redirect_is_returned_not_followed(self):
        connection = FakeConnection(
            FakeHTTPResponse(302, {"Location": "https://evil.example"}, b"")
        )
        transport = self.transport(connection)
        response = transport.get(
            "https://api.github.com/repos/xtianxx/payment-hub/actions/workflows/x.yml",
            TOKEN,
            5.0,
        )
        self.assertEqual(response.status, 302)
        self.assertEqual(len(connection.requests), 1)

    def test_redirect_maps_to_redirect_failure(self):
        connection = FakeConnection(
            FakeHTTPResponse(302, {"Location": "https://evil.example"}, b"")
        )
        api = MOD.GitHubApi(TOKEN, transport=self.transport(connection))
        with self.assertRaises(MOD.PreflightError) as raised:
            api.workflow(MOD.WORKFLOW_PATHS[0])
        self.assertEqual((raised.exception.stage, raised.exception.kind),
                         ("api", "redirect"))
        self.assertEqual(len(connection.requests), 1)

    def test_body_limit(self):
        connection = FakeConnection(
            FakeHTTPResponse(
                200,
                {"Content-Type": "application/json"},
                b"x" * (MOD.MAX_BODY_BYTES + 1),
            )
        )
        transport = self.transport(connection)
        with self.assertRaises(MOD.PreflightError) as raised:
            transport.get("https://api.github.com/repos/x", TOKEN, 5.0)
        self.assertEqual((raised.exception.stage, raised.exception.kind),
                         ("api", "body-too-large"))

    def test_declared_content_length_limit(self):
        connection = FakeConnection(
            FakeHTTPResponse(
                200,
                {
                    "Content-Type": "application/json",
                    "Content-Length": str(MOD.MAX_BODY_BYTES + 1),
                },
                b"{}",
            )
        )
        transport = self.transport(connection)
        with self.assertRaises(MOD.PreflightError) as raised:
            transport.get("https://api.github.com/repos/x", TOKEN, 5.0)
        self.assertEqual((raised.exception.stage, raised.exception.kind),
                         ("api", "body-too-large"))

    def test_transport_error_classes(self):
        cases = (
            (TimeoutError("slow"), ("api", "timeout")),
            (OSError("network"), ("api", "network")),
            (ssl.SSLError("tls"), ("api", "tls")),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                connection = FakeConnection(error=error)
                transport = self.transport(connection)
                with self.assertRaises(MOD.PreflightError) as raised:
                    transport.get("https://api.github.com/repos/x", TOKEN, 5.0)
                self.assertEqual(
                    (raised.exception.stage, raised.exception.kind),
                    expected,
                )


class ApiTests(unittest.TestCase):
    class StaticTransport:
        def __init__(self, response):
            self.response = response
            self.requests = []

        def get(self, url, token, timeout):
            self.requests.append((url, token, timeout))
            return self.response

    def api_with(self, response):
        transport = self.StaticTransport(response)
        return MOD.GitHubApi(TOKEN, transport=transport), transport

    def test_status_mapping(self):
        cases = (
            (401, ("api", "unauthorized")),
            (403, ("api", "forbidden")),
            (404, ("workflow", "not-found")),
            (429, ("api", "too-many-requests")),
            (500, ("api", "server-error")),
            (503, ("api", "server-error")),
            (302, ("api", "redirect")),
            (204, ("api", "http-error")),
        )
        for status, expected in cases:
            with self.subTest(status=status):
                api, _ = self.api_with(
                    raw_response(status, b'{"message":"safe"}')
                )
                with self.assertRaises(MOD.PreflightError) as raised:
                    api.workflow(MOD.WORKFLOW_PATHS[0])
                self.assertEqual(
                    (raised.exception.stage, raised.exception.kind),
                    expected,
                )

    def test_403_rate_limit_is_not_reported_as_missing(self):
        cases = (
            raw_response(
                403,
                b"API rate limit exceeded",
                {"x-ratelimit-remaining": "0"},
            ),
            raw_response(
                403,
                b"secondary rate limit",
                {"retry-after": "60"},
            ),
            raw_response(403, b"API rate limit exceeded"),
        )
        for response in cases:
            with self.subTest(headers=response.headers):
                api, _ = self.api_with(response)
                with self.assertRaises(MOD.PreflightError) as raised:
                    api.workflow(MOD.WORKFLOW_PATHS[0])
                self.assertEqual(
                    (raised.exception.stage, raised.exception.kind),
                    ("api", "rate-limit"),
                )

    def test_bad_json_and_non_json_are_distinct(self):
        api, _ = self.api_with(
            MOD.HttpResponse(
                status=200,
                headers={"content-type": "application/json"},
                body=b"{not-json",
            )
        )
        with self.assertRaises(MOD.PreflightError) as raised:
            api.workflow(MOD.WORKFLOW_PATHS[0])
        self.assertEqual((raised.exception.stage, raised.exception.kind),
                         ("api", "bad-json"))

        api, _ = self.api_with(
            MOD.HttpResponse(
                status=200,
                headers={"content-type": "text/plain"},
                body=b"{}",
            )
        )
        with self.assertRaises(MOD.PreflightError) as raised:
            api.workflow(MOD.WORKFLOW_PATHS[0])
        self.assertEqual((raised.exception.stage, raised.exception.kind),
                         ("api", "schema"))

    def test_json_array_is_schema_failure(self):
        api, _ = self.api_with(json_response([]))
        with self.assertRaises(MOD.PreflightError) as raised:
            api.workflow(MOD.WORKFLOW_PATHS[0])
        self.assertEqual((raised.exception.stage, raised.exception.kind),
                         ("api", "schema"))

    def test_response_headers_are_case_insensitive(self):
        workflow = make_workflow(MOD.WORKFLOW_PATHS[0], 101)
        api, _ = self.api_with(
            MOD.HttpResponse(
                status=200,
                headers={"Content-Type": "application/json"},
                body=json.dumps(workflow).encode("utf-8"),
            )
        )
        self.assertEqual(api.workflow(MOD.WORKFLOW_PATHS[0])["id"], 101)

    def test_404_context_is_explicit(self):
        api, _ = self.api_with(raw_response(404, b"{}"))
        with self.assertRaises(MOD.PreflightError) as raised:
            api.workflow_runs(101, SHA)
        self.assertEqual((raised.exception.stage, raised.exception.kind),
                         ("runs", "not-found"))

        api, _ = self.api_with(raw_response(404, b"{}"))
        with self.assertRaises(MOD.PreflightError) as raised:
            api.run_attempt(1001, 1)
        self.assertEqual((raised.exception.stage, raised.exception.kind),
                         ("run", "not-found"))

    def test_workflow_lookup_uses_filename_and_runs_query_has_no_status(self):
        api, transport = self.api_with(
            json_response(make_runs(0, []))
        )
        api.workflow_runs(101, SHA)
        url = transport.requests[0][0]
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual(
            parsed.path,
            "/repos/xtianxx/payment-hub/actions/workflows/101/runs",
        )
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(query["head_sha"], [SHA])
        self.assertEqual(query["branch"], ["main"])
        self.assertEqual(query["event"], ["push"])
        self.assertEqual(query["per_page"], ["100"])
        self.assertEqual(query["page"], ["1"])
        self.assertNotIn("status", query)

    def test_request_timeout_is_bounded(self):
        api, transport = self.api_with(json_response(make_runs(0, [])))
        api.workflow_runs(101, SHA)
        self.assertLessEqual(transport.requests[0][2], MOD.REQUEST_TIMEOUT_SECONDS)
        self.assertGreater(transport.requests[0][2], 0)

    def test_total_timeout(self):
        values = iter([0.0, 0.0, MOD.TOTAL_TIMEOUT_SECONDS + 1.0])
        transport = self.StaticTransport(
            json_response(make_workflow(MOD.WORKFLOW_PATHS[0], 101))
        )
        api = MOD.GitHubApi(TOKEN, transport=transport, clock=lambda: next(values))
        api.workflow(MOD.WORKFLOW_PATHS[0])
        with self.assertRaises(MOD.PreflightError) as raised:
            api.workflow(MOD.WORKFLOW_PATHS[0])
        self.assertEqual((raised.exception.stage, raised.exception.kind),
                         ("api", "timeout"))

    def test_no_retry_on_server_error(self):
        api, transport = self.api_with(raw_response(500, b"{}"))
        with self.assertRaises(MOD.PreflightError):
            api.workflow(MOD.WORKFLOW_PATHS[0])
        self.assertEqual(len(transport.requests), 1)

    def test_next_link_validation(self):
        endpoint = "/repos/xtianxx/payment-hub/actions/workflows/101/runs"
        valid = (
            f"https://api.github.com{endpoint}?"
            f"head_sha={SHA}&branch=main&event=push&per_page=100&page=2"
        )
        self.assertEqual(
            MOD._validate_next_link(valid, endpoint, SHA, 1), 2
        )
        invalid = (
            f"https://evil.example{endpoint}?page=2",
            f"https://api.github.com/other?page=2",
            f"https://api.github.com{endpoint}?page=2&branch=dev",
            f"https://api.github.com{endpoint}?page=2&head_sha={'b' * 40}",
            f"https://api.github.com{endpoint}?page=2&extra=1",
            f"https://api.github.com{endpoint}?page=3",
        )
        for url in invalid:
            with self.subTest(url=url):
                with self.assertRaises(MOD.PreflightError) as raised:
                    MOD._validate_next_link(url, endpoint, SHA, 1)
                self.assertEqual(
                    (raised.exception.stage, raised.exception.kind),
                    ("runs", "pagination"),
                )
        with self.assertRaises(MOD.PreflightError) as raised:
            MOD._validate_next_link(
                f"https://api.github.com{endpoint}?page=6",
                endpoint,
                SHA,
                5,
            )
        self.assertEqual(
            (raised.exception.stage, raised.exception.kind),
            ("runs", "pagination-limit"),
        )

    def test_next_link_parser(self):
        self.assertIsNone(MOD._next_link(None))
        self.assertIsNone(MOD._next_link(""))
        self.assertIsNone(
            MOD._next_link('<https://api.github.com/x>; rel="last"')
        )
        self.assertEqual(
            MOD._next_link(
                '<https://api.github.com/x?page=2>; rel="next", '
                '<https://api.github.com/x?page=5>; rel="last"'
            ),
            "https://api.github.com/x?page=2",
        )
        with self.assertRaises(MOD.PreflightError):
            MOD._next_link("next page: https://api.github.com/x")


class GateTests(GateTestCase):
    def test_success_both_workflows(self):
        self.add_both_success()
        self.assertIsNone(self.gate())
        data = self.summary_data(None)
        self.assertEqual(data["result"], "pass")
        self.assertIsNone(data["failure"])
        self.assertEqual(
            [entry["path"] for entry in data["workflows"]],
            list(MOD.WORKFLOW_PATHS),
        )
        self.assertEqual(
            data["workflows"][0]["latest_run"],
            {
                "run_id": 1001,
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "success",
            },
        )
        self.assertTrue(
            any(
                url.endswith("/actions/workflows/setup-check.yml")
                for url in self.fake.requests
            )
        )
        run_url = next(
            url
            for url in self.fake.requests
            if "/actions/workflows/101/runs" in url
        )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(run_url).query)
        self.assertNotIn("status", query)
        self.assertTrue(
            any("/actions/runs/1001/attempts/1" in url for url in self.fake.requests)
        )

    def test_older_success_does_not_mask_latest_failure(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        older = make_run(1001, 101, path, created_at=OLDER)
        latest = make_run(
            1002,
            101,
            path,
            status="completed",
            conclusion="failure",
            created_at=LATEST,
        )
        self.fake.page_responses[(101, 1)] = json_response(
            make_runs(2, [older, latest])
        )
        self.fake.attempt_responses[(1002, 1)] = json_response(latest)
        self.assertEqual(self.gate(), ("runs", "not-success"))
        data = self.summary_data(("runs", "not-success"))
        self.assertEqual(data["workflows"][0]["latest_run"]["run_id"], 1002)
        self.assertEqual(
            data["workflows"][0]["latest_run"]["conclusion"], "failure"
        )

    def test_latest_in_progress_fails(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        latest = make_run(
            1001,
            101,
            path,
            status="in_progress",
            conclusion=None,
        )
        self.fake.page_responses[(101, 1)] = json_response(
            make_runs(1, [latest])
        )
        self.fake.attempt_responses[(1001, 1)] = json_response(latest)
        self.assertEqual(self.gate(), ("runs", "not-completed"))

    def test_latest_queued_fails(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        latest = make_run(
            1001,
            101,
            path,
            status="queued",
            conclusion=None,
        )
        self.fake.page_responses[(101, 1)] = json_response(
            make_runs(1, [latest])
        )
        self.fake.attempt_responses[(1001, 1)] = json_response(latest)
        self.assertEqual(self.gate(), ("runs", "not-completed"))

    def test_latest_cancelled_fails(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        latest = make_run(
            1001,
            101,
            path,
            status="completed",
            conclusion="cancelled",
        )
        self.fake.page_responses[(101, 1)] = json_response(
            make_runs(1, [latest])
        )
        self.fake.attempt_responses[(1001, 1)] = json_response(latest)
        self.assertEqual(self.gate(), ("runs", "not-success"))

    def test_no_runs_fails(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        self.fake.page_responses[(101, 1)] = json_response(make_runs(0, []))
        self.assertEqual(self.gate(), ("runs", "missing"))

    def test_disabled_workflow_fails_before_runs(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101, state="disabled_manually")
        )
        self.assertEqual(self.gate(), ("workflow", "disabled"))
        self.assertFalse(
            any("/runs" in url for url in self.fake.requests)
        )

    def test_deleted_workflow_is_missing(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101, state="deleted")
        )
        self.assertEqual(self.gate(), ("workflow", "not-found"))

    def test_workflow_404_is_missing(self):
        self.fake.workflow_responses["setup-check.yml"] = raw_response(404, b"{}")
        self.assertEqual(self.gate(), ("workflow", "not-found"))

    def test_runs_404_is_explicit(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        self.fake.page_responses[(101, 1)] = raw_response(404, b"{}")
        self.assertEqual(self.gate(), ("runs", "not-found"))

    def test_attempt_404_is_explicit(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.add_success(path, 101, 1001)
        self.fake.attempt_responses[(1001, 1)] = raw_response(404, b"{}")
        self.assertEqual(self.gate(), ("run", "not-found"))

    def test_failure_summary_keeps_known_workflow_id(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        self.fake.page_responses[(101, 1)] = raw_response(404, b"{}")
        failure = self.gate()
        self.assertEqual(failure, ("runs", "not-found"))
        data = self.summary_data(failure)
        self.assertEqual(data["workflows"][0]["workflow_id"], 101)
        self.assertIsNone(data["workflows"][0]["latest_run"])
        self.assertIsNone(data["workflows"][1]["workflow_id"])

    def test_workflow_path_mismatch_fails(self):
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(MOD.WORKFLOW_PATHS[1], 101)
        )
        self.assertEqual(self.gate(), ("workflow", "path"))

    def test_workflow_path_at_main_is_accepted(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(f"{path}@main", 101)
        )
        run = make_run(1001, 101, path)
        self.fake.page_responses[(101, 1)] = json_response(make_runs(1, [run]))
        self.fake.attempt_responses[(1001, 1)] = json_response(run)
        self.add_success(MOD.WORKFLOW_PATHS[1], 202, 2002)
        self.assertIsNone(self.gate())

    def test_run_path_without_ref_is_accepted(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        run = make_run(1001, 101, path, run_path=path)
        self.fake.page_responses[(101, 1)] = json_response(make_runs(1, [run]))
        self.fake.attempt_responses[(1001, 1)] = json_response(run)
        self.add_success(MOD.WORKFLOW_PATHS[1], 202, 2002)
        self.assertIsNone(self.gate())

    def test_run_path_with_refs_heads_main_is_accepted(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        run = make_run(
            1001,
            101,
            path,
            run_path=f"{path}@refs/heads/main",
        )
        self.fake.page_responses[(101, 1)] = json_response(make_runs(1, [run]))
        self.fake.attempt_responses[(1001, 1)] = json_response(run)
        self.add_success(MOD.WORKFLOW_PATHS[1], 202, 2002)
        self.assertIsNone(self.gate())

    def test_run_path_with_other_ref_fails(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        run = make_run(1001, 101, path, run_path=f"{path}@dev")
        self.fake.page_responses[(101, 1)] = json_response(make_runs(1, [run]))
        self.assertEqual(self.gate(), ("runs", "identity"))

    def test_pull_request_run_is_rejected(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        run = make_run(1001, 101, path, event="pull_request")
        self.fake.page_responses[(101, 1)] = json_response(make_runs(1, [run]))
        self.assertEqual(self.gate(), ("runs", "identity"))

    def test_pull_requests_field_alone_does_not_reject_push_run(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        run = make_run(
            1001,
            101,
            path,
            pull_requests=[{"id": 1, "number": 1}],
        )
        self.fake.page_responses[(101, 1)] = json_response(make_runs(1, [run]))
        self.fake.attempt_responses[(1001, 1)] = json_response(run)
        self.add_success(MOD.WORKFLOW_PATHS[1], 202, 2002)
        self.assertIsNone(self.gate())

    def test_wrong_sha_branch_repository_workflow_id_are_rejected(self):
        path = MOD.WORKFLOW_PATHS[0]
        cases = (
            ("sha", {"sha": "b" * 40}),
            ("branch", {"branch": "dev"}),
            ("repository", {"repo": "evil/repository"}),
            ("workflow_id", {}),
            ("head_branch", {"head_branch": None}),
        )
        for name, overrides in cases:
            with self.subTest(name=name):
                self.setUp()
                self.fake.workflow_responses["setup-check.yml"] = json_response(
                    make_workflow(path, 101)
                )
                run = make_run(1001, 101, path, **overrides)
                if name == "workflow_id":
                    run["workflow_id"] = 999
                self.fake.page_responses[(101, 1)] = json_response(
                    make_runs(1, [run])
                )
                self.assertEqual(self.gate(), ("runs", "identity"))

    def test_pagination_older_failure_does_not_mask_newer_success(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        page_one = [
            make_run(
                1000 + index,
                101,
                path,
                conclusion="failure",
                created_at=OLDER,
            )
            for index in range(100)
        ]
        latest = make_run(2000, 101, path, created_at=LATEST)
        self.fake.page_responses[(101, 1)] = json_response(
            make_runs(101, page_one)
        )
        self.fake.page_responses[(101, 2)] = json_response(
            make_runs(101, [latest])
        )
        self.fake.attempt_responses[(2000, 1)] = json_response(latest)
        self.add_success(MOD.WORKFLOW_PATHS[1], 202, 2002)
        self.assertIsNone(self.gate())
        self.assertTrue(
            any("page=2" in url for url in self.fake.requests)
        )

    def test_pagination_newer_failure_on_page_two_fails(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        page_one = [
            make_run(1000 + index, 101, path, created_at=OLDER)
            for index in range(100)
        ]
        latest = make_run(
            2000,
            101,
            path,
            conclusion="failure",
            created_at=LATEST,
        )
        self.fake.page_responses[(101, 1)] = json_response(
            make_runs(101, page_one)
        )
        self.fake.page_responses[(101, 2)] = json_response(
            make_runs(101, [latest])
        )
        self.fake.attempt_responses[(2000, 1)] = json_response(latest)
        self.assertEqual(self.gate(), ("runs", "not-success"))

    def test_foreign_next_link_fails_closed(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        run = make_run(1001, 101, path)
        self.fake.page_responses[(101, 1)] = json_response(
            make_runs(1, [run]),
            headers={
                "link": '<https://evil.example/runs?page=2>; rel="next"'
            },
        )
        self.assertEqual(self.gate(), ("runs", "pagination"))

    def test_too_many_pages_fails_closed(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        self.fake.page_responses[(101, 1)] = json_response(make_runs(501, []))
        self.assertEqual(self.gate(), ("runs", "pagination-limit"))

    def test_page_count_mismatch_fails_closed(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        run = make_run(1001, 101, path)
        self.fake.page_responses[(101, 1)] = json_response(
            make_runs(2, [run])
        )
        self.assertEqual(self.gate(), ("runs", "pagination"))

    def test_duplicate_run_ids_fail_closed(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        run = make_run(1001, 101, path)
        self.fake.page_responses[(101, 1)] = json_response(
            make_runs(2, [run, dict(run)])
        )
        self.assertEqual(self.gate(), ("runs", "schema"))

    def test_rerun_attempt_two_is_used(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.add_success(path, 101, 1001, attempt=2)
        self.add_success(MOD.WORKFLOW_PATHS[1], 202, 2002)
        self.assertIsNone(self.gate())
        self.assertTrue(
            any("/attempts/2" in url for url in self.fake.requests)
        )
        data = self.summary_data(None)
        self.assertEqual(
            data["workflows"][0]["latest_run"]["run_attempt"], 2
        )

    def test_attempt_identity_mismatch_fails_closed(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.add_success(path, 101, 1001, attempt=2)
        wrong = make_run(1001, 101, path, attempt=1)
        self.fake.attempt_responses[(1001, 2)] = json_response(wrong)
        self.assertEqual(self.gate(), ("run", "attempt-mismatch"))

    def test_attempt_status_mismatch_fails_closed(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.add_success(path, 101, 1001, attempt=2)
        wrong = make_run(
            1001,
            101,
            path,
            attempt=2,
            conclusion="failure",
        )
        self.fake.attempt_responses[(1001, 2)] = json_response(wrong)
        self.assertEqual(self.gate(), ("run", "attempt-mismatch"))

    def test_attempt_failure_matches_and_fails_not_success(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        run = make_run(
            1001,
            101,
            path,
            status="completed",
            conclusion="failure",
        )
        self.fake.page_responses[(101, 1)] = json_response(make_runs(1, [run]))
        self.fake.attempt_responses[(1001, 1)] = json_response(run)
        self.assertEqual(self.gate(), ("runs", "not-success"))

    def test_run_schema_failures(self):
        path = MOD.WORKFLOW_PATHS[0]
        base = make_run(1001, 101, path)
        cases = (
            ("created_at", {key: value for key, value in base.items() if key != "created_at"}),
            ("run_attempt", dict(base, run_attempt=0)),
            ("status", dict(base, status="unknown")),
            ("conclusion", dict(base, conclusion="unknown")),
            ("id", dict(base, id=True)),
            ("repository", {key: value for key, value in base.items() if key != "repository"}),
        )
        for name, run in cases:
            with self.subTest(name=name):
                self.setUp()
                self.fake.workflow_responses["setup-check.yml"] = json_response(
                    make_workflow(path, 101)
                )
                self.fake.page_responses[(101, 1)] = json_response(
                    make_runs(1, [run])
                )
                self.assertEqual(self.gate(), ("runs", "schema"))

    def test_workflow_schema_failures(self):
        path = MOD.WORKFLOW_PATHS[0]
        cases = (
            ("id", {"id": True, "path": path, "state": "active"}),
            ("state", {"id": 101, "path": path, "state": "unknown"}),
            ("path", {"id": 101, "path": 5, "state": "active"}),
        )
        for name, workflow in cases:
            with self.subTest(name=name):
                self.setUp()
                self.fake.workflow_responses["setup-check.yml"] = json_response(
                    workflow
                )
                self.assertEqual(self.gate(), ("workflow", "schema"))

    def test_attempt_schema_failure(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.add_success(path, 101, 1001)
        self.fake.attempt_responses[(1001, 1)] = json_response({})
        self.assertEqual(self.gate(), ("run", "schema"))

    def test_total_count_schema_failure(self):
        path = MOD.WORKFLOW_PATHS[0]
        self.fake.workflow_responses["setup-check.yml"] = json_response(
            make_workflow(path, 101)
        )
        self.fake.page_responses[(101, 1)] = json_response(
            {"total_count": True, "workflow_runs": []}
        )
        self.assertEqual(self.gate(), ("runs", "schema"))


class EnvironmentAndMainTests(GateTestCase):
    def test_environment_requires_exact_trusted_context(self):
        self.assertEqual(MOD.validate_environment(base_env(), SHA), TOKEN)
        cases = (
            ("event", {"GITHUB_EVENT_NAME": "push"}),
            ("ref", {"GITHUB_REF": "refs/heads/dev"}),
            ("repository", {"GITHUB_REPOSITORY": "evil/repo"}),
            ("sha", {"GITHUB_SHA": "not-a-sha"}),
            ("sha-mismatch", {"GITHUB_SHA": "b" * 40}),
            ("token", {"GITHUB_TOKEN": ""}),
        )
        for expected, overrides in cases:
            with self.subTest(kind=expected):
                with self.assertRaises(MOD.PreflightError) as raised:
                    MOD.validate_environment(base_env(**overrides), SHA)
                self.assertEqual(
                    (raised.exception.stage, raised.exception.kind),
                    ("env", expected),
                )

    def test_gh_token_is_not_a_fallback(self):
        environment = base_env()
        del environment["GITHUB_TOKEN"]
        environment["GH_TOKEN"] = TOKEN
        with self.assertRaises(MOD.PreflightError) as raised:
            MOD.validate_environment(environment, SHA)
        self.assertEqual((raised.exception.stage, raised.exception.kind),
                         ("env", "token"))

    def test_main_success_writes_summary_and_short_stdout(self):
        self.add_both_success()
        code, stdout, stderr = self.run_main(base_env())
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "ci-preflight: pass\n")
        self.assertEqual(stderr, "")
        data = json.loads(self.summary_path.read_text(encoding="utf-8"))
        self.assertEqual(data["result"], "pass")
        self.assertIsNone(data["failure"])

    def test_main_http_failure_writes_summary_without_secret(self):
        self.fake.workflow_responses["setup-check.yml"] = raw_response(
            500,
            b"RAW_SECRET_BODY " + TOKEN.encode("utf-8"),
        )
        code, stdout, stderr = self.run_main(base_env())
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "ci-preflight: fail stage=api kind=server-error\n",
        )
        self.assertEqual(stderr, "")
        text = self.summary_path.read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("RAW_SECRET_BODY", text)
        self.assertNotIn("https://", text)
        self.assertNotIn("head_sha", text)
        data = json.loads(text)
        self.assertEqual(data["result"], "fail")
        self.assertEqual(
            data["failure"], {"stage": "api", "kind": "server-error"}
        )

    def test_main_401_is_not_reported_as_missing(self):
        self.fake.workflow_responses["setup-check.yml"] = raw_response(
            401,
            b'{"message":"Bad credentials"}',
        )
        code, stdout, _ = self.run_main(base_env())
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "ci-preflight: fail stage=api kind=unauthorized\n",
        )

    def test_main_403_rate_limit_is_not_reported_as_missing(self):
        self.fake.workflow_responses["setup-check.yml"] = raw_response(
            403,
            b"API rate limit exceeded",
            {"x-ratelimit-remaining": "0"},
        )
        code, stdout, _ = self.run_main(base_env())
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "ci-preflight: fail stage=api kind=rate-limit\n",
        )

    def test_main_429_and_5xx_are_distinct(self):
        cases = (
            (429, "ci-preflight: fail stage=api kind=too-many-requests\n"),
            (500, "ci-preflight: fail stage=api kind=server-error\n"),
        )
        for status, expected in cases:
            with self.subTest(status=status):
                self.setUp()
                self.fake.workflow_responses["setup-check.yml"] = raw_response(
                    status,
                    b"{}",
                )
                code, stdout, _ = self.run_main(base_env())
                self.assertEqual(code, 1)
                self.assertEqual(stdout, expected)

    def test_main_bad_json_and_schema_are_distinct(self):
        self.fake.workflow_responses["setup-check.yml"] = MOD.HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=b"{bad",
        )
        code, stdout, _ = self.run_main(base_env())
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "ci-preflight: fail stage=api kind=bad-json\n",
        )

        self.setUp()
        self.fake.workflow_responses["setup-check.yml"] = json_response([])
        code, stdout, _ = self.run_main(base_env())
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "ci-preflight: fail stage=api kind=schema\n",
        )

    def test_main_missing_environment_still_writes_summary(self):
        environment = base_env()
        del environment["GITHUB_TOKEN"]
        code, stdout, _ = self.run_main(environment)
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "ci-preflight: fail stage=env kind=token\n",
        )
        data = json.loads(self.summary_path.read_text(encoding="utf-8"))
        self.assertEqual(data["result"], "fail")
        self.assertEqual(data["failure"], {"stage": "env", "kind": "token"})
        self.assertEqual(data["source_sha"], SHA)

    def test_main_invalid_source_sha_still_writes_summary(self):
        code, stdout, _ = self.run_main(
            base_env(),
            argv=[
                "--source-sha",
                "not-a-sha",
                "--summary",
                str(self.summary_path),
            ],
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "ci-preflight: fail stage=cli kind=source-sha\n",
        )
        data = json.loads(self.summary_path.read_text(encoding="utf-8"))
        self.assertIsNone(data["source_sha"])
        self.assertEqual(
            data["failure"], {"stage": "cli", "kind": "source-sha"}
        )

    def test_main_relative_summary_is_rejected_without_writing(self):
        relative = f"relative-ci-preflight-{os.getpid()}.json"
        code, stdout, _ = self.run_main(
            base_env(),
            argv=[
                "--source-sha",
                SHA,
                "--summary",
                relative,
            ],
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "ci-preflight: fail stage=cli kind=summary-path\n",
        )
        self.assertFalse(Path(relative).exists())

    def test_main_unexpected_exception_is_sanitized(self):
        with mock.patch.object(
            MOD,
            "run_gate",
            side_effect=RuntimeError("RAW_SECRET_BODY " + TOKEN),
        ):
            code, stdout, stderr = self.run_main(base_env())
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "ci-preflight: fail stage=internal kind=unexpected\n",
        )
        self.assertEqual(stderr, "")
        text = self.summary_path.read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("RAW_SECRET_BODY", text)

    def test_main_summary_write_failure_is_reported(self):
        self.add_both_success()
        with mock.patch.object(
            MOD,
            "write_summary",
            side_effect=MOD.PreflightError("summary", "write"),
        ):
            code, stdout, _ = self.run_main(base_env())
        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "ci-preflight: fail stage=summary kind=write\n",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
