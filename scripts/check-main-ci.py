#!/usr/bin/env python3
"""Fail-closed read-only GitHub Actions preflight for main release gates.

The publish workflow invokes this checker after checkout and before upload.
It verifies that the workflow_dispatch context is the trusted main branch of
``xtianxx/payment-hub``, that ``GITHUB_SHA`` equals ``--source-sha``, and that
the latest push run for that SHA in both approved workflows is ``completed``
with conclusion ``success``.  A newer failed, queued, cancelled, or running run
is never hidden by an older success.

Only bounded GET requests to ``api.github.com`` are made.  Redirects are not
followed, retries and PAT fallbacks are absent, and the token or raw response
body is never printed.  The summary contains only the fixed whitelist below.
The check is a metadata gate: a new run can still be created between this
check and a later push.  That residual race is not claimed to be closed.
"""

from __future__ import annotations

import argparse
import datetime
import http.client
import json
import os
import re
import socket
import ssl
import stat
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

REPOSITORY = "xtianxx/payment-hub"
BRANCH = "main"
REF = "refs/heads/main"
TRIGGER_EVENT = "workflow_dispatch"
PUSH_EVENT = "push"
WORKFLOW_PATHS = (
    ".github/workflows/setup-check.yml",
    ".github/workflows/check.yml",
)

API_HOST = "api.github.com"
API_BASE = f"https://{API_HOST}"
API_VERSION = "2022-11-28"
PER_PAGE = 100
MAX_PAGES = 5
MAX_BODY_BYTES = 4 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 15.0
TOTAL_TIMEOUT_SECONDS = 60.0

SUMMARY_SCHEMA = "payment-hub.ci-preflight.v1"
STDOUT_PREFIX = "ci-preflight:"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{8,512}$")

RUN_STATUSES = frozenset(
    {
        "completed",
        "action_required",
        "cancelled",
        "failure",
        "neutral",
        "skipped",
        "stale",
        "success",
        "timed_out",
        "in_progress",
        "queued",
        "requested",
        "waiting",
        "pending",
    }
)
RUN_CONCLUSIONS = frozenset(
    {
        "success",
        "failure",
        "neutral",
        "cancelled",
        "skipped",
        "timed_out",
        "action_required",
        "stale",
        "startup_failure",
    }
)

FAILURE_ENUM = {
    "cli": {
        "source-sha",
        "summary",
        "summary-path",
        "summary-symlink",
        "summary-exists",
        "summary-not-fresh",
    },
    "env": {"event", "ref", "repository", "sha", "sha-mismatch", "token"},
    "workflow": {"not-found", "disabled", "schema", "path"},
    "runs": {
        "not-found",
        "missing",
        "schema",
        "identity",
        "pagination",
        "pagination-limit",
        "not-completed",
        "not-success",
    },
    "run": {"not-found", "schema", "identity", "attempt-mismatch"},
    "api": {
        "host",
        "unauthorized",
        "forbidden",
        "rate-limit",
        "too-many-requests",
        "server-error",
        "timeout",
        "network",
        "tls",
        "redirect",
        "bad-json",
        "schema",
        "body-too-large",
        "http-error",
    },
    "summary": {"exists", "write"},
    "internal": {"unexpected"},
}


class PreflightError(RuntimeError):
    """A fixed, non-sensitive failure category."""

    def __init__(self, stage: str, kind: str) -> None:
        self.stage = stage
        self.kind = kind
        super().__init__(f"{stage}/{kind}")


def _safe_failure(stage: Any, kind: Any) -> tuple[str, str]:
    if (
        isinstance(stage, str)
        and isinstance(kind, str)
        and stage in FAILURE_ENUM
        and kind in FAILURE_ENUM[stage]
    ):
        return stage, kind
    return "internal", "unexpected"


@dataclass(frozen=True)
class Config:
    source_sha: str
    summary_path: Path


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class JsonResponse:
    data: dict[str, Any]
    headers: Mapping[str, str]


def _default_connection(host: str, timeout: float, context: ssl.SSLContext) -> Any:
    return http.client.HTTPSConnection(
        host,
        443,
        timeout=timeout,
        context=context,
    )


class HttpsTransport:
    """One bounded HTTPS GET to api.github.com; no redirects and no retries."""

    def __init__(
        self,
        connection_factory: Callable[[str, float, ssl.SSLContext], Any] | None = None,
    ) -> None:
        self._connection_factory = connection_factory or _default_connection

    @staticmethod
    def _parse_api_url(url: str) -> urllib.parse.SplitResult:
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except (TypeError, ValueError):
            raise PreflightError("api", "host") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname != API_HOST
            or port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise PreflightError("api", "host")
        return parsed

    def get(self, url: str, token: str, timeout: float) -> HttpResponse:
        parsed = self._parse_api_url(url)
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            raise PreflightError("env", "token")
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        headers = {
            "Accept": "application/vnd.github+json",
            "Accept-Encoding": "identity",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "payment-hub-ci-preflight",
            # The URL was checked above; authorization is never sent to a
            # different host.
            "Authorization": f"Bearer {token}",
        }
        connection: Any = None
        try:
            try:
                limit = max(0.1, float(timeout))
            except (TypeError, ValueError):
                raise PreflightError("api", "timeout") from None
            connection = self._connection_factory(
                API_HOST, limit, ssl.create_default_context()
            )
            connection.request("GET", target, headers=headers)
            response = connection.getresponse()
            response_headers = {
                str(name).lower(): str(value)
                for name, value in response.getheaders()
            }
            declared = response_headers.get("content-length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError:
                    declared_size = None
                if declared_size is not None and declared_size > MAX_BODY_BYTES:
                    raise PreflightError("api", "body-too-large")
            body = self._read_body(response)
            return HttpResponse(
                status=int(response.status),
                headers=response_headers,
                body=body,
            )
        except PreflightError:
            raise
        except (socket.timeout, TimeoutError):
            raise PreflightError("api", "timeout") from None
        except ssl.SSLError:
            raise PreflightError("api", "tls") from None
        except OSError:
            raise PreflightError("api", "network") from None
        except Exception:
            raise PreflightError("api", "network") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    @staticmethod
    def _read_body(response: Any) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(min(65536, MAX_BODY_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                raise PreflightError("api", "body-too-large")
            chunks.append(chunk)
        return b"".join(chunks)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if isinstance(value, str):
        return value
    lower = name.lower()
    for key, candidate in headers.items():
        if (
            isinstance(key, str)
            and key.lower() == lower
            and isinstance(candidate, str)
        ):
            return candidate
    return None


def _rate_limited(headers: Mapping[str, str], body: bytes) -> bool:
    remaining = _header(headers, "x-ratelimit-remaining")
    if isinstance(remaining, str) and remaining.strip() == "0":
        return True
    if _header(headers, "retry-after") is not None:
        return True
    return b"rate limit" in body.lower()


class GitHubApi:
    """Read-only workflow/run lookups with a fixed endpoint allowlist."""

    def __init__(
        self,
        token: str,
        transport: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            raise PreflightError("env", "token")
        self._token = token
        self._transport = transport or HttpsTransport()
        self._clock = clock
        self._deadline = clock() + TOTAL_TIMEOUT_SECONDS

    def _get_json(
        self,
        path: str,
        not_found_stage: str,
        not_found_kind: str,
    ) -> JsonResponse:
        prefix = f"/repos/{REPOSITORY}/actions/"
        if not isinstance(path, str) or not path.startswith(prefix):
            raise PreflightError("api", "host")
        if (
            "#" in path
            or "://" in path
            or ".." in path
            or "\r" in path
            or "\n" in path
        ):
            raise PreflightError("api", "host")
        remaining = self._deadline - self._clock()
        if remaining <= 0.0:
            raise PreflightError("api", "timeout")
        url = API_BASE + path
        response = self._transport.get(
            url,
            self._token,
            min(REQUEST_TIMEOUT_SECONDS, remaining),
        )
        return self._interpret(response, not_found_stage, not_found_kind)

    @staticmethod
    def _interpret(
        response: HttpResponse,
        not_found_stage: str,
        not_found_kind: str,
    ) -> JsonResponse:
        try:
            status = int(response.status)
        except (TypeError, ValueError):
            raise PreflightError("api", "schema") from None
        if 300 <= status < 400:
            raise PreflightError("api", "redirect")
        if status == 401:
            raise PreflightError("api", "unauthorized")
        if status == 403:
            if _rate_limited(response.headers, response.body):
                raise PreflightError("api", "rate-limit")
            raise PreflightError("api", "forbidden")
        if status == 404:
            raise PreflightError(not_found_stage, not_found_kind)
        if status == 429:
            raise PreflightError("api", "too-many-requests")
        if 500 <= status <= 599:
            raise PreflightError("api", "server-error")
        if status != 200:
            raise PreflightError("api", "http-error")
        content_type = _header(response.headers, "content-type") or ""
        if not content_type.lower().startswith("application/json"):
            raise PreflightError("api", "schema")
        try:
            data = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PreflightError("api", "bad-json") from None
        if not isinstance(data, dict):
            raise PreflightError("api", "schema")
        return JsonResponse(data=data, headers=response.headers)

    def workflow(self, workflow_path: str) -> dict[str, Any]:
        filename = workflow_path.rsplit("/", 1)[-1]
        path = (
            f"/repos/{REPOSITORY}/actions/workflows/"
            f"{urllib.parse.quote(filename, safe='')}"
        )
        return self._get_json(path, "workflow", "not-found").data

    @staticmethod
    def _page_path(
        endpoint: str,
        base_params: Sequence[tuple[str, str]],
        page: int,
    ) -> str:
        params = list(base_params) + [("page", str(page))]
        return f"{endpoint}?{urllib.parse.urlencode(params)}"

    def workflow_runs(self, workflow_id: int, source_sha: str) -> list[dict[str, Any]]:
        endpoint = (
            f"/repos/{REPOSITORY}/actions/workflows/{workflow_id}/runs"
        )
        base_params = (
            ("head_sha", source_sha),
            ("branch", BRANCH),
            ("event", PUSH_EVENT),
            ("per_page", str(PER_PAGE)),
        )
        first = self._get_json(
            self._page_path(endpoint, base_params, 1),
            "runs",
            "not-found",
        )
        total = _nonnegative_int(first.data.get("total_count"), "runs")
        first_runs = first.data.get("workflow_runs")
        if not isinstance(first_runs, list):
            raise PreflightError("runs", "schema")
        if total == 0:
            if first_runs:
                raise PreflightError("runs", "schema")
            return []

        pages = (total + PER_PAGE - 1) // PER_PAGE
        if pages > MAX_PAGES:
            raise PreflightError("runs", "pagination-limit")

        collected: list[dict[str, Any]] = []
        for page in range(1, pages + 1):
            if page == 1:
                current = first
                runs = first_runs
            else:
                current = self._get_json(
                    self._page_path(endpoint, base_params, page),
                    "runs",
                    "not-found",
                )
                if _nonnegative_int(
                    current.data.get("total_count"), "runs"
                ) != total:
                    raise PreflightError("runs", "pagination")
                runs = current.data.get("workflow_runs")
                if not isinstance(runs, list):
                    raise PreflightError("runs", "schema")
            expected = min(PER_PAGE, total - (page - 1) * PER_PAGE)
            if len(runs) != expected:
                raise PreflightError("runs", "pagination")
            collected.extend(runs)

            next_url = _next_link(_header(current.headers, "link"))
            if next_url is not None:
                next_page = _validate_next_link(
                    next_url,
                    endpoint,
                    source_sha,
                    page,
                )
                if next_page != page + 1 or next_page > pages:
                    raise PreflightError("runs", "pagination")
        if len(collected) != total:
            raise PreflightError("runs", "pagination")
        return collected

    def run_attempt(self, run_id: int, attempt: int) -> dict[str, Any]:
        path = (
            f"/repos/{REPOSITORY}/actions/runs/{run_id}"
            f"/attempts/{attempt}"
        )
        return self._get_json(path, "run", "not-found").data


def _next_link(header: Any) -> str | None:
    if not isinstance(header, str) or not header:
        return None
    parts: list[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(header):
        if character == "<":
            depth += 1
        elif character == ">":
            depth = max(0, depth - 1)
        elif character == "," and depth == 0:
            parts.append(header[start:index])
            start = index + 1
    parts.append(header[start:])
    for part in parts:
        match = re.match(r"\s*<([^<>]*)>(.*)\Z", part, re.S)
        if match is None:
            if "next" in part:
                raise PreflightError("runs", "pagination")
            continue
        relation = re.search(r'rel\s*=\s*"([^"]*)"', match.group(2))
        if relation is None:
            if "next" in match.group(2):
                raise PreflightError("runs", "pagination")
            continue
        if "next" in relation.group(1).split():
            return match.group(1)
    return None


def _validate_next_link(
    url: str,
    endpoint: str,
    source_sha: str,
    current_page: int,
) -> int:
    if not isinstance(url, str):
        raise PreflightError("runs", "pagination")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise PreflightError("runs", "pagination") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != API_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != endpoint
    ):
        raise PreflightError("runs", "pagination")
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if set(query) - {"page", "per_page", "head_sha", "branch", "event"}:
        raise PreflightError("runs", "pagination")
    expected = {
        "head_sha": source_sha,
        "branch": BRANCH,
        "event": PUSH_EVENT,
        "per_page": str(PER_PAGE),
    }
    for name, value in expected.items():
        if name in query and query[name] != [value]:
            raise PreflightError("runs", "pagination")
    pages = query.get("page")
    if pages is None or len(pages) != 1:
        raise PreflightError("runs", "pagination")
    try:
        page = int(pages[0])
    except ValueError:
        raise PreflightError("runs", "pagination") from None
    if page != current_page + 1:
        raise PreflightError("runs", "pagination")
    if page > MAX_PAGES:
        raise PreflightError("runs", "pagination-limit")
    return page


def _positive_int(value: Any, stage: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
    ):
        raise PreflightError(stage, "schema")
    return value


def _nonnegative_int(value: Any, stage: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise PreflightError(stage, "schema")
    return value


def _parse_created_at(value: Any, stage: str) -> datetime.datetime:
    if not isinstance(value, str):
        raise PreflightError(stage, "schema")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PreflightError(stage, "schema") from None
    if parsed.tzinfo is None:
        raise PreflightError(stage, "schema")
    return parsed.astimezone(datetime.timezone.utc)


def _path_matches(value: str, expected: str) -> bool:
    return value in (
        expected,
        f"{expected}@{BRANCH}",
        f"{expected}@refs/heads/{BRANCH}",
    )


@dataclass(frozen=True)
class ParsedRun:
    id: int
    workflow_id: int
    attempt: int
    path: str
    head_sha: str
    head_branch: str
    event: str
    status: str
    conclusion: str | None
    created_at: datetime.datetime


def _parse_run(
    raw: Any,
    *,
    workflow_id: int,
    workflow_path: str,
    source_sha: str,
    stage: str,
) -> ParsedRun:
    if not isinstance(raw, Mapping):
        raise PreflightError(stage, "schema")
    run_id = _positive_int(raw.get("id"), stage)
    parent_id = _positive_int(raw.get("workflow_id"), stage)
    attempt = _positive_int(raw.get("run_attempt"), stage)
    path = raw.get("path")
    head_sha = raw.get("head_sha")
    head_branch = raw.get("head_branch")
    event = raw.get("event")
    status = raw.get("status")
    conclusion = raw.get("conclusion")
    if not isinstance(path, str):
        raise PreflightError(stage, "schema")
    if not isinstance(head_sha, str):
        raise PreflightError(stage, "schema")
    if not isinstance(event, str):
        raise PreflightError(stage, "schema")
    if not isinstance(status, str) or status not in RUN_STATUSES:
        raise PreflightError(stage, "schema")
    if conclusion is not None and (
        not isinstance(conclusion, str)
        or conclusion not in RUN_CONCLUSIONS
    ):
        raise PreflightError(stage, "schema")
    created_at = _parse_created_at(raw.get("created_at"), stage)
    repository = raw.get("repository")
    if not isinstance(repository, Mapping):
        raise PreflightError(stage, "schema")
    full_name = repository.get("full_name")
    if not isinstance(full_name, str):
        raise PreflightError(stage, "schema")

    if parent_id != workflow_id:
        raise PreflightError(stage, "identity")
    if not _path_matches(path, workflow_path):
        raise PreflightError(stage, "identity")
    if head_sha != source_sha:
        raise PreflightError(stage, "identity")
    if not isinstance(head_branch, str) or head_branch != BRANCH:
        raise PreflightError(stage, "identity")
    if event != PUSH_EVENT:
        raise PreflightError(stage, "identity")
    if full_name.casefold() != REPOSITORY.casefold():
        raise PreflightError(stage, "identity")

    return ParsedRun(
        id=run_id,
        workflow_id=parent_id,
        attempt=attempt,
        path=path,
        head_sha=head_sha,
        head_branch=head_branch,
        event=event,
        status=status,
        conclusion=conclusion,
        created_at=created_at,
    )


def _workflow_id(data: Any, expected_path: str) -> int:
    if not isinstance(data, Mapping):
        raise PreflightError("workflow", "schema")
    workflow_id = _positive_int(data.get("id"), "workflow")
    path = data.get("path")
    state = data.get("state")
    if not isinstance(path, str) or not isinstance(state, str):
        raise PreflightError("workflow", "schema")
    if not _path_matches(path, expected_path):
        raise PreflightError("workflow", "path")
    if state != "active":
        if state in ("disabled_manually", "disabled_inactivity", "disabled_fork"):
            raise PreflightError("workflow", "disabled")
        if state == "deleted":
            raise PreflightError("workflow", "not-found")
        raise PreflightError("workflow", "schema")
    return workflow_id


def _select_latest(runs: Sequence[ParsedRun]) -> ParsedRun:
    if not runs:
        raise PreflightError("runs", "missing")
    seen: set[int] = set()
    for run in runs:
        if run.id in seen:
            raise PreflightError("runs", "schema")
        seen.add(run.id)
    return max(runs, key=lambda run: (run.created_at, run.id))


@dataclass
class WorkflowState:
    path: str
    workflow_id: int | None = None
    latest_run_id: int | None = None
    latest_run_attempt: int | None = None
    latest_run_status: str | None = None
    latest_run_conclusion: str | None = None

    def to_summary(self) -> dict[str, Any]:
        workflow_id = (
            self.workflow_id
            if isinstance(self.workflow_id, int)
            and not isinstance(self.workflow_id, bool)
            and self.workflow_id > 0
            else None
        )
        run_id = (
            self.latest_run_id
            if isinstance(self.latest_run_id, int)
            and not isinstance(self.latest_run_id, bool)
            and self.latest_run_id > 0
            else None
        )
        attempt = (
            self.latest_run_attempt
            if isinstance(self.latest_run_attempt, int)
            and not isinstance(self.latest_run_attempt, bool)
            and self.latest_run_attempt > 0
            else None
        )
        status = (
            self.latest_run_status
            if isinstance(self.latest_run_status, str)
            and self.latest_run_status in RUN_STATUSES
            else None
        )
        conclusion = (
            self.latest_run_conclusion
            if isinstance(self.latest_run_conclusion, str)
            and self.latest_run_conclusion in RUN_CONCLUSIONS
            else None
        )
        latest_run = None
        if run_id is not None:
            latest_run = {
                "run_id": run_id,
                "run_attempt": attempt,
                "status": status,
                "conclusion": conclusion,
            }
        return {
            "path": self.path,
            "workflow_id": workflow_id,
            "latest_run": latest_run,
        }


def default_workflow_states() -> list[WorkflowState]:
    return [WorkflowState(path=path) for path in WORKFLOW_PATHS]


def build_summary(
    *,
    result: str,
    failure: tuple[str, str] | None,
    source_sha: Any,
    workflows: Sequence[WorkflowState],
) -> dict[str, Any]:
    safe_result = "pass" if result == "pass" else "fail"
    if safe_result == "pass" and failure is not None:
        safe_result = "fail"
    if safe_result == "fail" and failure is None:
        failure = ("internal", "unexpected")
    safe_failure = None
    if safe_result == "fail":
        pair = failure if isinstance(failure, tuple) and len(failure) == 2 else None
        stage, kind = _safe_failure(
            pair[0] if pair is not None else None,
            pair[1] if pair is not None else None,
        )
        safe_failure = {"stage": stage, "kind": kind}
    safe_sha = (
        source_sha
        if isinstance(source_sha, str) and SHA_RE.fullmatch(source_sha)
        else None
    )
    by_path = {
        state.path: state
        for state in workflows
        if isinstance(state, WorkflowState) and state.path in WORKFLOW_PATHS
    }
    entries = [
        by_path.get(path, WorkflowState(path=path)).to_summary()
        for path in WORKFLOW_PATHS
    ]
    return {
        "schema": SUMMARY_SCHEMA,
        "result": safe_result,
        "failure": safe_failure,
        "source_sha": safe_sha,
        "workflows": entries,
    }


def _reject_symlink_components(path: Path) -> None:
    current = path
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            raise PreflightError("cli", "summary-path") from None
        else:
            if stat.S_ISLNK(info.st_mode):
                raise PreflightError("cli", "summary-symlink")
        parent = current.parent
        if parent == current:
            return
        current = parent


def validate_summary_path(raw: Any) -> Path:
    if isinstance(raw, os.PathLike):
        try:
            raw = os.fspath(raw)
        except TypeError:
            raise PreflightError("cli", "summary") from None
    if not isinstance(raw, str) or not raw:
        raise PreflightError("cli", "summary")
    path = Path(raw).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise PreflightError("cli", "summary-path")
    _reject_symlink_components(path)
    if path.is_symlink():
        raise PreflightError("cli", "summary-symlink")
    if path.exists():
        raise PreflightError("cli", "summary-exists")
    parent = path.parent
    if parent.exists():
        if parent.is_symlink() or not parent.is_dir():
            raise PreflightError("cli", "summary-path")
        try:
            if any(parent.iterdir()):
                raise PreflightError("cli", "summary-not-fresh")
            parent.chmod(0o700)
        except PreflightError:
            raise
        except OSError:
            raise PreflightError("cli", "summary-path") from None
    else:
        try:
            parent.mkdir(parents=True, exist_ok=False, mode=0o700)
            parent.chmod(0o700)
        except FileExistsError:
            raise PreflightError("cli", "summary-path") from None
        except OSError:
            raise PreflightError("cli", "summary-path") from None
    if path.exists() or path.is_symlink():
        raise PreflightError("cli", "summary-exists")
    return path


def write_summary(path: Path, data: Mapping[str, Any]) -> None:
    """Write one private summary atomically without overwriting an entry."""
    try:
        path = Path(path)
    except TypeError:
        raise PreflightError("summary", "write") from None
    try:
        text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    except (TypeError, ValueError):
        raise PreflightError("summary", "write") from None
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=".ci-preflight-",
            dir=str(path.parent),
        )
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # link(2) publishes the complete file and fails if the summary
            # already exists; unlike replace(2) it cannot silently overwrite.
            os.link(temporary, path)
        except FileExistsError:
            raise PreflightError("summary", "exists") from None
        except OSError:
            raise PreflightError("summary", "write") from None
        try:
            temporary.unlink()
        except OSError:
            pass
        temporary = None
    except PreflightError:
        raise
    except (OSError, TypeError, ValueError):
        raise PreflightError("summary", "write") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def validate_source_sha(value: Any) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise PreflightError("cli", "source-sha")
    return value


def validate_environment(env: Mapping[str, str], source_sha: str) -> str:
    if env.get("GITHUB_EVENT_NAME") != TRIGGER_EVENT:
        raise PreflightError("env", "event")
    if env.get("GITHUB_REF") != REF:
        raise PreflightError("env", "ref")
    if env.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise PreflightError("env", "repository")
    sha = env.get("GITHUB_SHA")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise PreflightError("env", "sha")
    if sha != source_sha:
        raise PreflightError("env", "sha-mismatch")
    token = env.get("GITHUB_TOKEN")
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise PreflightError("env", "token")
    return token


def run_gate(
    config: Config,
    api: GitHubApi,
    workflows: Sequence[WorkflowState],
) -> tuple[str, str] | None:
    try:
        for state in workflows:
            state.workflow_id = None
            state.latest_run_id = None
            state.latest_run_attempt = None
            state.latest_run_status = None
            state.latest_run_conclusion = None
        for state in workflows:
            workflow_data = api.workflow(state.path)
            state.workflow_id = _workflow_id(workflow_data, state.path)
            raw_runs = api.workflow_runs(
                state.workflow_id,
                config.source_sha,
            )
            parsed_runs = [
                _parse_run(
                    raw,
                    workflow_id=state.workflow_id,
                    workflow_path=state.path,
                    source_sha=config.source_sha,
                    stage="runs",
                )
                for raw in raw_runs
            ]
            latest = _select_latest(parsed_runs)
            state.latest_run_id = latest.id
            state.latest_run_attempt = latest.attempt
            state.latest_run_status = latest.status
            state.latest_run_conclusion = latest.conclusion

            attempt_data = api.run_attempt(latest.id, latest.attempt)
            attempt = _parse_run(
                attempt_data,
                workflow_id=state.workflow_id,
                workflow_path=state.path,
                source_sha=config.source_sha,
                stage="run",
            )
            if (
                attempt.id != latest.id
                or attempt.attempt != latest.attempt
                or attempt.status != latest.status
                or attempt.conclusion != latest.conclusion
            ):
                raise PreflightError("run", "attempt-mismatch")
            state.latest_run_status = attempt.status
            state.latest_run_conclusion = attempt.conclusion
            if attempt.status != "completed":
                raise PreflightError("runs", "not-completed")
            if attempt.conclusion != "success":
                raise PreflightError("runs", "not-success")
        return None
    except PreflightError as exc:
        return exc.stage, exc.kind


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description=(
            "Fail-closed read-only check that the latest main push run for "
            "the supplied source SHA succeeded in both approved workflows."
        ),
    )
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--summary", required=True)
    return parser.parse_args(argv)


def _print_failure(stage: str, kind: str) -> None:
    safe_stage, safe_kind = _safe_failure(stage, kind)
    print(f"{STDOUT_PREFIX} fail stage={safe_stage} kind={safe_kind}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary_path = validate_summary_path(args.summary)
    except PreflightError as exc:
        _print_failure(exc.stage, exc.kind)
        return 1
    except Exception:
        _print_failure("internal", "unexpected")
        return 1

    raw_source = getattr(args, "source_sha", None)
    safe_source = (
        raw_source
        if isinstance(raw_source, str) and SHA_RE.fullmatch(raw_source)
        else None
    )
    workflows = default_workflow_states()
    failure: tuple[str, str] | None = None
    try:
        config = Config(
            source_sha=validate_source_sha(raw_source),
            summary_path=summary_path,
        )
        token = validate_environment(os.environ, config.source_sha)
        failure = run_gate(config, GitHubApi(token), workflows)
    except PreflightError as exc:
        failure = (exc.stage, exc.kind)
    except Exception:
        failure = ("internal", "unexpected")

    result = "pass" if failure is None else "fail"
    try:
        summary = build_summary(
            result=result,
            failure=failure,
            source_sha=safe_source,
            workflows=workflows,
        )
    except Exception:
        result = "fail"
        failure = ("internal", "unexpected")
        summary = build_summary(
            result="fail",
            failure=failure,
            source_sha=None,
            workflows=default_workflow_states(),
        )
    try:
        write_summary(summary_path, summary)
    except PreflightError as exc:
        _print_failure(exc.stage, exc.kind)
        return 1
    except Exception:
        _print_failure("summary", "write")
        return 1

    if result == "pass":
        print(f"{STDOUT_PREFIX} pass")
        return 0
    if failure is None:
        failure = ("internal", "unexpected")
    _print_failure(failure[0], failure[1])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
