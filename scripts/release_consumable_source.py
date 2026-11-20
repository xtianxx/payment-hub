#!/usr/bin/env python3
"""Online, read-only collection of one Payment Hub release artifact set.

The module fetches, for one independently supplied release run and attempt:

1. the exact release workflow run attempt from ``api.github.com``;
2. the unique artifact named ``release-summary-<run_id>-<attempt>``;
3. the artifact ZIP through the API's signed redirect, without forwarding the
   GitHub bearer token to the storage host;
4. the three root release documents inside that ZIP.

It then delegates the byte-level consistency checks to the canonical offline
verifier (``verify-release-consumable.py``) before independently checking the
source CI run and bundle artifact named by the verified evidence document.
Only after every check succeeds does it return the ``app`` and ``migrate``
digest-pinned references from the offline verifier.

The complete operation runs under one absolute ``SIGALRM`` wall deadline that
covers API headers and bodies, the signed storage transfer, ZIP inspection,
and offline verification.  Socket timeouts alone are inactivity limits and
cannot bound a server that keeps sending bytes slowly.  Entry fails closed
with ``verify/deadline-unsupported`` on platforms or threads without usable
``SIGALRM``, when ``SIGALRM`` is blocked in the calling thread, or when the
handler cannot be installed.  An already-active ``ITIMER_REAL`` and a
concurrent in-process verification are never merged or waited for; both fail
closed with ``verify/deadline-conflict``.  The guard is never silently
dropped.

A module-level lock serializes this module's own verifications, but it cannot
serialize against unrelated components that concurrently change the
process-level ``SIGALRM`` handler or timer.  That external mutation remains a
known boundary: the guard detects a handler replaced while it is installed
and leaves that external state untouched instead of overwriting it.

Every GitHub API response is parsed once by a thin wrapper that rejects
duplicate object keys, non-finite JSON constants, and excessive recursion
before the shared read-only client receives it.

The module never writes files, never contacts GHCR, never pulls an image, and
never probes whether a digest is currently pullable.  It is not a signature
verification and does not defend against a principal who can rewrite the
workflow, its runs, and its artifacts.

All failures are raised as :class:`VerificationError` with a fixed ``stage``
and ``kind``.  Token values, signed download URLs, raw response bodies, and
original exception messages are never included or printed.

``workflow_run`` metadata on an artifact is optional.  When present, its run
ID, source SHA, branch, workflow ID (when supplied), repository ID, and head
repository ID (when supplied) must match the run being verified; a partial or
contradictory object is rejected.  The artifact list is scoped by run ID, so a
missing ``workflow_run`` object does not widen the selection.

The public boundary is :func:`verify_online`.  ``verifier`` may be injected
for tests; by default the canonical offline verifier is loaded lazily under a
private module name and its ``main`` is never called.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import ipaddress
import json
import re
import signal
import socket
import ssl
import stat
import sys
import threading
import time
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any, Callable, Mapping

REPOSITORY = "xtianxx/payment-hub"
RELEASE_WORKFLOW_PATH = ".github/workflows/release.yml"
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
RELEASE_BRANCH = "main"
RELEASE_EVENT = "workflow_dispatch"
ROLES = ("app", "migrate")
TARGETS = {
    "app": "ghcr.io/xtianxx/payment-hub-app",
    "migrate": "ghcr.io/xtianxx/payment-hub-migrate",
}
RELEASE_PATHS = frozenset(
    {
        RELEASE_WORKFLOW_PATH,
        f"{RELEASE_WORKFLOW_PATH}@{RELEASE_BRANCH}",
        f"{RELEASE_WORKFLOW_PATH}@refs/heads/{RELEASE_BRANCH}",
    }
)
RELEASE_FILES = (
    "release-summary.json",
    "descriptor.json",
    "release-evidence.json",
)
RELEASE_FILE_SET = frozenset(RELEASE_FILES)

MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_FILE_BYTES = 512 * 1024
MAX_SIGNED_URL_LENGTH = 16384
REQUEST_TIMEOUT_SECONDS = 15.0
TOTAL_TIMEOUT_SECONDS = 90.0

_SCRIPT_DIR = Path(__file__).resolve().parent
_VERIFIER_MODULE_NAME = "payment_hub_release_consumable_verifier"
_CHECKER_MODULE_NAME = "payment_hub_check_main_ci"
_SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]{8,512}")
_SAFE_CATEGORY_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
_TIMER_RESOLUTION_SECONDS = 0.05
_DEADLINE_LOCK = threading.Lock()

_ALLOWED_CHECKER_FAILURES = {
    "env": {"token"},
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
    "workflow": {"not-found", "disabled", "schema", "path"},
    "run": {
        "not-found",
        "schema",
        "identity",
        "attempt-mismatch",
        "expected-mismatch",
    },
    "artifacts": {
        "not-found",
        "schema",
        "identity",
        "pagination",
        "pagination-limit",
    },
    "ci-run": {"schema", "identity"},
    "internal": {"unexpected"},
}


class VerificationError(RuntimeError):
    """A fixed, non-sensitive online verification failure category."""

    def __init__(self, stage: str, kind: str) -> None:
        self.stage = stage
        self.kind = kind
        super().__init__(f"{stage}/{kind}")


class _DuplicateKey(ValueError):
    """Raised when an API JSON object repeats a key at any depth."""


def _reject_duplicate_pairs(pairs: Any) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> Any:
    raise ValueError


class _StrictJsonTransport:
    """Validate every 200 JSON API body before the shared client parses it.

    ``check-main-ci`` uses ``json.loads`` without a duplicate-key hook.  This
    thin wrapper keeps the shared module unchanged while rejecting duplicate
    keys, JSON constants, and recursion in the release API responses consumed
    here.  Non-JSON and non-200 responses pass through unchanged.
    """

    def __init__(self, transport: Any) -> None:
        self._transport = transport

    def get(self, url: str, token: str, timeout: float) -> Any:
        response = self._transport.get(url, token, timeout)
        status = getattr(response, "status", None)
        if (
            isinstance(status, int)
            and not isinstance(status, bool)
            and status == 200
        ):
            headers = getattr(response, "headers", None)
            content_type = (
                ci._header(headers, "content-type")
                if isinstance(headers, Mapping)
                else None
            )
            if (
                isinstance(content_type, str)
                and content_type.lower().startswith("application/json")
            ):
                body = getattr(response, "body", None)
                if not isinstance(body, bytes):
                    raise VerificationError("api", "schema")
                try:
                    text = body.decode("utf-8")
                except UnicodeDecodeError:
                    raise VerificationError("api", "bad-json") from None
                try:
                    json.loads(
                        text,
                        object_pairs_hook=_reject_duplicate_pairs,
                        parse_constant=_reject_json_constant,
                    )
                except _DuplicateKey:
                    raise VerificationError("api", "duplicate-key") from None
                except RecursionError:
                    raise VerificationError("api", "bad-json") from None
                except (TypeError, ValueError):
                    raise VerificationError("api", "bad-json") from None
        return response


class _DeadlineExceeded(BaseException):
    """Raised by the absolute wall-deadline signal handler.

    Deliberately not an ``Exception`` subclass: the shared HTTPS transport
    maps broad ``Exception`` failures to fixed network categories.  The wall
    deadline must pass through such handlers and be normalized once at the
    public boundary.
    """

    def __init__(self) -> None:
        super().__init__()


class _WallDeadline:
    """Absolute wall deadline around the complete online verification.

    Socket timeouts are inactivity timeouts, not absolute wall limits.  This
    uses ``SIGALRM`` so a server that keeps sending bytes cannot extend the
    exchange indefinitely.  Signal handlers are process-wide and delivered to
    the main thread, so installing this guard from any other thread fails
    closed as ``verify/deadline-unsupported`` before any signal state is
    touched.  A calling-thread mask that blocks ``SIGALRM`` also fails closed
    as ``verify/deadline-unsupported`` without changing that mask.  An
    already-active ``ITIMER_REAL``, whether one-shot or periodic, is never
    replaced or merged: that fails closed as ``verify/deadline-conflict`` and
    leaves the caller's handler and timer untouched.  On exit the guard
    restores its handler only while that handler is still installed, and
    clears its timer only while its remaining time still falls inside the
    arming interval it recorded; an externally replaced handler or timer is
    never overwritten.

    Platforms without ``SIGALRM`` fail closed as
    ``verify/deadline-unsupported`` rather than silently dropping the guard.
    """

    def __init__(
        self,
        seconds: float,
        stage: str = "verify",
        kind: str = "deadline",
    ) -> None:
        self.seconds = seconds
        self.stage = stage
        self.kind = kind
        self.old_handler: Any = None
        self.installed_handler: Any = None
        self.deadline_low: float | None = None
        self.deadline_high: float | None = None
        self.handler_owned = False
        self.timer_owned = False
        self.installed = False

    def __enter__(self) -> "_WallDeadline":
        if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
            raise VerificationError(
                self.stage, "deadline-unsupported"
            )
        if threading.current_thread() is not threading.main_thread():
            raise VerificationError(
                self.stage, "deadline-unsupported"
            )
        if self.seconds <= 0:
            raise VerificationError(self.stage, self.kind)
        pthread_sigmask = getattr(signal, "pthread_sigmask", None)
        if pthread_sigmask is not None:
            try:
                blocked = pthread_sigmask(signal.SIG_BLOCK, [])
            except (AttributeError, OSError, ValueError):
                raise VerificationError(
                    self.stage, "deadline-unsupported"
                ) from None
            if signal.SIGALRM in blocked:
                # Never unmask the caller's SIGALRM: their handler could not
                # be guaranteed to be ours while the guard is active.
                raise VerificationError(
                    self.stage, "deadline-unsupported"
                )
        try:
            old_handler = signal.getsignal(signal.SIGALRM)
            old_remaining, old_interval = signal.getitimer(signal.ITIMER_REAL)
        except (AttributeError, OSError, ValueError):
            raise VerificationError(
                self.stage, "deadline-unsupported"
            ) from None
        if old_remaining > 0 or old_interval > 0:
            raise VerificationError(self.stage, "deadline-conflict")
        if old_handler is None:
            # ``getsignal`` returns ``None`` for a handler that was not
            # installed from Python (for example a C-level handler).  Such a
            # handler cannot be restored through ``signal.signal``.
            raise VerificationError(
                self.stage, "deadline-unsupported"
            )
        self.old_handler = old_handler
        self.installed_handler = self._handler
        try:
            signal.signal(signal.SIGALRM, self.installed_handler)
        except (AttributeError, OSError, ValueError):
            raise VerificationError(
                self.stage, "deadline-unsupported"
            ) from None
        self.handler_owned = True
        # Record ownership before arming the timer: a signal delivered just
        # after ``setitimer`` returns must still let cleanup identify and
        # cancel this guard's own timer.  The real expiry lies between the
        # monotonic samples taken immediately before and after the call, so an
        # unbounded pause around ``setitimer`` cannot make our own timer look
        # external.
        self.timer_owned = True
        try:
            before = time.monotonic()
            # Establish a cleanup boundary before arming the timer.  If the
            # post-arm sample is interrupted, the upper bound stays open
            # enough to identify this guard's own timer, while a previously
            # short external timer remains below the lower bound.
            self.deadline_low = before + self.seconds
            self.deadline_high = float("inf")
            try:
                signal.setitimer(signal.ITIMER_REAL, self.seconds)
            finally:
                after = time.monotonic()
                self.deadline_high = after + self.seconds
        except (AttributeError, OSError, ValueError):
            self._restore()
            raise VerificationError(
                self.stage, "deadline-unsupported"
            ) from None
        except BaseException:
            self._restore()
            raise
        self.installed = True
        return self

    def _handler(self, _signum: int, _frame: Any) -> None:
        raise _DeadlineExceeded

    def _owns_signal_state(self) -> bool:
        try:
            return (
                signal.getsignal(signal.SIGALRM) is self.installed_handler
            )
        except (AttributeError, OSError, ValueError):
            return False

    def _timer_is_ours(self) -> bool:
        """Whether the active ``ITIMER_REAL`` is the one this guard set.

        The guard arms its timer between two monotonic samples, so the real
        expiry lies within the recorded ``[deadline_low, deadline_high]``
        interval.  Sampling the clock around ``getitimer`` bounds the remaining
        time from both sides without assuming any bound on scheduler pauses.
        A live timer outside that interval is an external replacement and is
        left running.
        """

        if self.deadline_low is None or self.deadline_high is None:
            return False
        before = time.monotonic()
        try:
            remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        except (AttributeError, OSError, ValueError):
            return False
        after = time.monotonic()
        if interval != 0:
            return False
        if remaining <= 0.0:
            return True
        lower = self.deadline_low - after
        # With an interrupted post-arm sample the upper bound is still open.
        # A timer this guard armed can never have more than ``seconds`` left,
        # so cap the unknown upper bound instead of accepting an arbitrary
        # external timer installed from a signal callback.
        upper = min(self.deadline_high - before, self.seconds)
        return (
            lower - _TIMER_RESOLUTION_SECONDS
            <= remaining
            <= upper + _TIMER_RESOLUTION_SECONDS
        )

    def _restore(self) -> None:
        try:
            if self.timer_owned:
                self.timer_owned = False
                if self._timer_is_ours():
                    try:
                        signal.setitimer(signal.ITIMER_REAL, 0)
                    except (AttributeError, OSError, ValueError):
                        pass
        finally:
            # Restore the handler even if clearing the timer raised, including
            # a signal-delivered ``_DeadlineExceeded`` from that call.  Timer
            # and handler ownership are decided independently: an external
            # handler replacement must not strand this guard's timer, and an
            # external timer must not suppress handler restoration.
            if self.handler_owned:
                self.handler_owned = False
                if self._owns_signal_state():
                    try:
                        signal.signal(signal.SIGALRM, self.old_handler)
                    except (AttributeError, OSError, ValueError):
                        pass

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if not self.installed:
            return False
        self._restore()
        return False


def _load_script(module_name: str, filename: str) -> Any:
    """Load one sibling script under a private name without running ``main``."""

    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    path = _SCRIPT_DIR / filename
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise VerificationError("internal", "unexpected")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except VerificationError:
            sys.modules.pop(module_name, None)
            raise
        except Exception:
            sys.modules.pop(module_name, None)
            raise VerificationError("internal", "unexpected") from None
        except BaseException:
            # Interrupt-style exits (KeyboardInterrupt, SystemExit) must not
            # leave the half-initialized module cached for a later call.
            sys.modules.pop(module_name, None)
            raise
    except VerificationError:
        raise
    except Exception:
        raise VerificationError("internal", "unexpected") from None
    return module


# Loading the checker is side-effect free and gives the online lane the same
# fixed-API GitHub client and bounded HTTPS transport as the release preflight.
ci = _load_script(_CHECKER_MODULE_NAME, "check-main-ci.py")


def _default_verifier() -> Any:
    return _load_script(_VERIFIER_MODULE_NAME, "verify-release-consumable.py")


def _safe_category(stage: Any, kind: Any) -> bool:
    return (
        isinstance(stage, str)
        and isinstance(kind, str)
        and _SAFE_CATEGORY_RE.fullmatch(stage) is not None
        and _SAFE_CATEGORY_RE.fullmatch(kind) is not None
    )


def _raise_checker_failure(exc: Any) -> None:
    stage = getattr(exc, "stage", None)
    kind = getattr(exc, "kind", None)
    if (
        isinstance(stage, str)
        and isinstance(kind, str)
        and stage in _ALLOWED_CHECKER_FAILURES
        and kind in _ALLOWED_CHECKER_FAILURES[stage]
    ):
        raise VerificationError(stage, kind) from None
    raise VerificationError("internal", "unexpected") from None


def _offline_failure(exc: Any) -> VerificationError:
    stage = getattr(exc, "stage", None)
    kind = getattr(exc, "kind", None)
    if _safe_category(stage, kind):
        return VerificationError(stage, kind)
    return VerificationError("verify", "verification")


def _positive_int(value: Any, stage: str, kind: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise VerificationError(stage, kind)
    return value


def _source_sha(value: Any, stage: str, kind: str) -> str:
    if not isinstance(value, str) or _SOURCE_SHA_RE.fullmatch(value) is None:
        raise VerificationError(stage, kind)
    return value


def _digest(value: Any, stage: str, kind: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise VerificationError(stage, kind)
    return value


def _validate_inputs(
    token: Any,
    expected_source_sha: Any,
    release_run_id: Any,
    release_run_attempt: Any,
    expected_app_digest: Any,
    expected_migrate_digest: Any,
) -> tuple[str, int, int, str | None, str | None]:
    if not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None:
        raise VerificationError("input", "token")
    source_sha = _source_sha(
        expected_source_sha, "input", "expected-source-sha"
    )
    run_id = _positive_int(release_run_id, "input", "release-run-id")
    attempt = _positive_int(
        release_run_attempt, "input", "release-run-attempt"
    )
    if (expected_app_digest is None) != (expected_migrate_digest is None):
        raise VerificationError("input", "digest-pair")
    app_digest: str | None = None
    migrate_digest: str | None = None
    if expected_app_digest is not None:
        app_digest = _digest(
            expected_app_digest, "input", "expected-app-digest"
        )
        migrate_digest = _digest(
            expected_migrate_digest, "input", "expected-migrate-digest"
        )
    return source_sha, run_id, attempt, app_digest, migrate_digest


def _remaining(deadline: float, clock: Callable[[], float], stage: str) -> float:
    try:
        remaining = deadline - clock()
    except Exception:
        raise VerificationError("internal", "unexpected") from None
    if remaining <= 0.0:
        raise VerificationError(stage, "timeout")
    return remaining


def _response_status(response: Any, stage: str) -> int:
    status = getattr(response, "status", None)
    if not isinstance(status, int) or isinstance(status, bool):
        raise VerificationError(stage, "schema")
    return status


def _response_headers(response: Any, stage: str) -> Mapping[str, str]:
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        raise VerificationError(stage, "schema")
    return headers


def _response_body(response: Any) -> bytes:
    body = getattr(response, "body", None)
    if not isinstance(body, bytes):
        raise VerificationError("api", "schema")
    return body


_HISTORY_WORKFLOW_STATES = frozenset(
    {
        "active",
        "disabled_manually",
        "disabled_inactivity",
        "disabled_fork",
    }
)


def _workflow_id_for_history(data: Any, expected_path: str) -> int:
    """Return the workflow ID for an already-completed release or CI run.

    A run stays verifiable after its workflow is disabled; the run and
    artifact identities are bound separately.  Deleted workflows are reported
    as not found and unknown states as schema failures.  The path must still
    name the expected workflow file.
    """

    if not isinstance(data, Mapping):
        raise VerificationError("workflow", "schema")
    workflow_id = _positive_int(data.get("id"), "workflow", "schema")
    path = data.get("path")
    state = data.get("state")
    if not isinstance(path, str) or not isinstance(state, str):
        raise VerificationError("workflow", "schema")
    if not ci._path_matches(path, expected_path):
        raise VerificationError("workflow", "path")
    if state == "deleted":
        raise VerificationError("workflow", "not-found")
    if state not in _HISTORY_WORKFLOW_STATES:
        raise VerificationError("workflow", "schema")
    return workflow_id


def _parse_release_run(
    raw: Any,
    *,
    workflow_id: int,
    source_sha: str,
    run_id: int,
    attempt: int,
) -> int:
    if not isinstance(raw, Mapping):
        raise VerificationError("run", "schema")
    if _positive_int(raw.get("id"), "run", "id") != run_id:
        raise VerificationError("run", "id")
    if _positive_int(raw.get("workflow_id"), "run", "schema") != workflow_id:
        raise VerificationError("run", "identity")
    if _positive_int(raw.get("run_attempt"), "run", "attempt") != attempt:
        raise VerificationError("run", "attempt")
    if _source_sha(raw.get("head_sha"), "run", "source") != source_sha:
        raise VerificationError("run", "source")
    if raw.get("head_branch") != RELEASE_BRANCH:
        raise VerificationError("run", "branch")
    if raw.get("event") != RELEASE_EVENT:
        raise VerificationError("run", "event")
    if raw.get("status") != "completed":
        raise VerificationError("run", "status")
    if raw.get("conclusion") != "success":
        raise VerificationError("run", "conclusion")
    path = raw.get("path")
    if not isinstance(path, str) or path not in RELEASE_PATHS:
        raise VerificationError("run", "workflow")
    repository = raw.get("repository")
    if not isinstance(repository, Mapping):
        raise VerificationError("run", "repository")
    full_name = repository.get("full_name")
    if (
        not isinstance(full_name, str)
        or full_name.casefold() != REPOSITORY.casefold()
    ):
        raise VerificationError("run", "repository")
    return _positive_int(repository.get("id"), "run", "repository")


def _validate_ci_run(
    raw: Any,
    *,
    workflow_id: int,
    source_sha: str,
    run_id: int,
    attempt: int,
) -> int:
    if not isinstance(raw, Mapping):
        raise VerificationError("ci-run", "schema")
    try:
        parsed = ci._parse_run(
            raw,
            workflow_id=workflow_id,
            workflow_path=CI_WORKFLOW_PATH,
            source_sha=source_sha,
            stage="ci-run",
        )
    except Exception as exc:
        _raise_checker_failure(exc)
    if parsed.id != run_id:
        raise VerificationError("ci-run", "id")
    if parsed.attempt != attempt:
        raise VerificationError("ci-run", "attempt")
    if parsed.status != "completed":
        raise VerificationError("ci-run", "status")
    if parsed.conclusion != "success":
        raise VerificationError("ci-run", "conclusion")
    if parsed.repository_id is None:
        raise VerificationError("ci-run", "schema")
    return parsed.repository_id


def _validate_artifact_workflow_run(
    artifact: Mapping[str, Any],
    *,
    stage: str,
    run_id: int,
    workflow_id: int,
    source_sha: str,
    repository_id: int,
) -> None:
    workflow_run = artifact.get("workflow_run")
    if workflow_run is None:
        return
    if not isinstance(workflow_run, Mapping):
        raise VerificationError(stage, "workflow-run")
    if _positive_int(workflow_run.get("id"), stage, "workflow-run") != run_id:
        raise VerificationError(stage, "workflow-run")
    if "workflow_id" in workflow_run:
        if (
            _positive_int(
                workflow_run.get("workflow_id"), stage, "workflow-run"
            )
            != workflow_id
        ):
            raise VerificationError(stage, "workflow-run")
    if (
        _source_sha(
            workflow_run.get("head_sha"), stage, "workflow-run"
        )
        != source_sha
    ):
        raise VerificationError(stage, "workflow-run")
    if workflow_run.get("head_branch") != RELEASE_BRANCH:
        raise VerificationError(stage, "workflow-run")
    if (
        _positive_int(
            workflow_run.get("repository_id"), stage, "workflow-run"
        )
        != repository_id
    ):
        raise VerificationError(stage, "workflow-run")
    if "head_repository_id" in workflow_run:
        if (
            _positive_int(
                workflow_run.get("head_repository_id"),
                stage,
                "workflow-run",
            )
            != repository_id
        ):
            raise VerificationError(stage, "workflow-run")


def _validate_artifact(
    artifact: Mapping[str, Any],
    *,
    stage: str,
    expected_name: str,
    run_id: int,
    workflow_id: int,
    source_sha: str,
    repository_id: int,
    maximum_bytes: int | None,
) -> None:
    if artifact.get("name") != expected_name:
        raise VerificationError(stage, "schema")
    _positive_int(artifact.get("id"), stage, "schema")
    size = _positive_int(artifact.get("size_in_bytes"), stage, "schema")
    if maximum_bytes is not None and size > maximum_bytes:
        raise VerificationError(stage, "size")
    if artifact.get("expired") is not False:
        raise VerificationError(stage, "expired")
    _digest(artifact.get("digest"), stage, "digest")
    _validate_artifact_workflow_run(
        artifact,
        stage=stage,
        run_id=run_id,
        workflow_id=workflow_id,
        source_sha=source_sha,
        repository_id=repository_id,
    )


def _select_artifact(
    artifacts: Any,
    *,
    stage: str,
    expected_name: str,
    run_id: int,
    workflow_id: int,
    source_sha: str,
    repository_id: int,
    maximum_bytes: int | None,
) -> Mapping[str, Any]:
    if not isinstance(artifacts, list):
        raise VerificationError(stage, "schema")
    matches: list[Mapping[str, Any]] = []
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise VerificationError(stage, "schema")
        if artifact.get("name") == expected_name:
            matches.append(artifact)
    if not matches:
        raise VerificationError(stage, "not-found")
    if len(matches) != 1:
        raise VerificationError(stage, "duplicate")
    selected = matches[0]
    _validate_artifact(
        selected,
        stage=stage,
        expected_name=expected_name,
        run_id=run_id,
        workflow_id=workflow_id,
        source_sha=source_sha,
        repository_id=repository_id,
        maximum_bytes=maximum_bytes,
    )
    return selected


def _artifact_zip_location(
    transport: Any,
    token: str,
    artifact_id: int,
    deadline: float,
    clock: Callable[[], float],
) -> str:
    url = (
        f"{ci.API_BASE}/repos/{REPOSITORY}/actions/artifacts/"
        f"{artifact_id}/zip"
    )
    remaining = _remaining(deadline, clock, "api")
    response = transport.get(
        url, token, min(REQUEST_TIMEOUT_SECONDS, remaining)
    )
    status = _response_status(response, "api")
    headers = _response_headers(response, "api")
    if status == 302:
        location = ci._header(headers, "location")
        if not isinstance(location, str) or not location:
            raise VerificationError("artifact", "redirect")
        _validate_signed_url(location)
        return location
    if 200 <= status < 400:
        raise VerificationError("artifact", "redirect")
    if status in (404, 410):
        raise VerificationError("artifact", "not-found")
    if status == 401:
        raise VerificationError("api", "unauthorized")
    if status == 403:
        if ci._rate_limited(headers, _response_body(response)):
            raise VerificationError("api", "rate-limit")
        raise VerificationError("api", "forbidden")
    if status == 429:
        raise VerificationError("api", "too-many-requests")
    if 500 <= status <= 599:
        raise VerificationError("api", "server-error")
    raise VerificationError("api", "http-error")


def _validate_signed_url(url: Any) -> urllib.parse.SplitResult:
    if (
        not isinstance(url, str)
        or not url
        or len(url) > MAX_SIGNED_URL_LENGTH
    ):
        raise VerificationError("download", "host")
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in url
    ):
        raise VerificationError("download", "host")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
        hostname = parsed.hostname
    except (TypeError, ValueError):
        raise VerificationError("download", "host") from None
    if (
        parsed.scheme != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise VerificationError("download", "host")
    hostname = hostname.casefold()
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise VerificationError("download", "host")
    if not (
        hostname == "githubusercontent.com"
        or hostname.endswith(".githubusercontent.com")
        or hostname.endswith(".blob.core.windows.net")
    ):
        raise VerificationError("download", "host")
    return parsed


def _read_signed_body(response: Any, declared: int | None) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(65536, MAX_ARCHIVE_BYTES + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_ARCHIVE_BYTES:
            raise VerificationError("download", "size")
        chunks.append(chunk)
    body = b"".join(chunks)
    if declared is not None and declared != len(body):
        raise VerificationError("download", "size")
    return body


class SignedHttpsTransport:
    """One bounded HTTPS GET to a signed storage URL, without credentials."""

    def __init__(
        self,
        connection_factory: (
            Callable[[str, float, ssl.SSLContext], Any] | None
        ) = None,
    ) -> None:
        self._connection_factory = (
            connection_factory
            if connection_factory is not None
            else ci._default_connection
        )

    def get(self, url: str, timeout: float) -> Any:
        parsed = _validate_signed_url(url)
        hostname = parsed.hostname
        if hostname is None:  # Defensive; _validate_signed_url rejects it.
            raise VerificationError("download", "host")
        target = parsed.path
        if parsed.query:
            target = f"{target}?{parsed.query}"
        headers = {
            "Accept": "application/octet-stream",
            "Accept-Encoding": "identity",
            "User-Agent": "payment-hub-release-consumable",
        }
        connection: Any = None
        try:
            try:
                limit = max(0.1, float(timeout))
            except (TypeError, ValueError):
                raise VerificationError("download", "timeout") from None
            connection = self._connection_factory(
                hostname, limit, ssl.create_default_context()
            )
            connection.request("GET", target, headers=headers)
            response = connection.getresponse()
            response_headers = {
                str(name).lower(): str(value)
                for name, value in response.getheaders()
            }
            status = int(response.status)
            declared: int | None = None
            declared_header = response_headers.get("content-length")
            if declared_header is not None:
                try:
                    declared = int(declared_header)
                except ValueError:
                    raise VerificationError("download", "size") from None
                if declared < 0 or declared > MAX_ARCHIVE_BYTES:
                    raise VerificationError("download", "size")
            if 300 <= status < 400:
                raise VerificationError("download", "redirect")
            if status in (404, 410):
                raise VerificationError("download", "not-found")
            if status != 200:
                raise VerificationError("download", "status")
            body = _read_signed_body(response, declared)
            return ci.HttpResponse(
                status=status,
                headers=response_headers,
                body=body,
            )
        except VerificationError:
            raise
        except (socket.timeout, TimeoutError):
            raise VerificationError("download", "timeout") from None
        except ssl.SSLError:
            raise VerificationError("download", "tls") from None
        except OSError:
            raise VerificationError("download", "network") from None
        except Exception:
            raise VerificationError("download", "network") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


def _download_archive(
    transport: Any,
    location: str,
    artifact_size: int,
    artifact_digest: str,
    deadline: float,
    clock: Callable[[], float],
) -> bytes:
    remaining = _remaining(deadline, clock, "download")
    response = transport.get(
        location, min(REQUEST_TIMEOUT_SECONDS, remaining)
    )
    status = _response_status(response, "download")
    headers = _response_headers(response, "download")
    if 300 <= status < 400:
        raise VerificationError("download", "redirect")
    if status in (404, 410):
        raise VerificationError("download", "not-found")
    if status != 200:
        raise VerificationError("download", "status")
    body = getattr(response, "body", None)
    if not isinstance(body, bytes):
        raise VerificationError("download", "schema")
    if len(body) > MAX_ARCHIVE_BYTES:
        raise VerificationError("download", "size")
    declared = ci._header(headers, "content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except (TypeError, ValueError):
            raise VerificationError("download", "size") from None
        if declared_size < 0 or declared_size != len(body):
            raise VerificationError("download", "size")
    if len(body) != artifact_size:
        raise VerificationError("download", "size")
    actual = "sha256:" + hashlib.sha256(body).hexdigest()
    if actual != artifact_digest:
        raise VerificationError("download", "hash")
    return body


def _read_release_archive(data: bytes) -> dict[str, bytes]:
    if not isinstance(data, bytes) or not data:
        raise VerificationError("archive", "zip")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            for info in infos:
                name = info.filename
                original = info.orig_filename
                if not isinstance(name, str) or not name:
                    raise VerificationError("archive", "entries")
                # zipfile truncates a filename at its first NUL byte while
                # retaining the raw name in orig_filename.  Any difference is
                # a malformed or disguised entry, never a root release file.
                if (
                    original != name
                    or "\x00" in name
                    or "\x00" in original
                ):
                    raise VerificationError("archive", "entries")
                if info.is_dir() or name.endswith("/"):
                    raise VerificationError("archive", "type")
                if (
                    "/" in name
                    or "\\" in name
                    or name.startswith("/")
                    or name in (".", "..")
                    or any(
                        part in ("", ".", "..")
                        for part in name.split("/")
                    )
                ):
                    raise VerificationError("archive", "entries")
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise VerificationError("archive", "symlink")
                file_type = stat.S_IFMT(mode)
                if file_type and file_type != stat.S_IFREG:
                    raise VerificationError("archive", "type")
                if info.flag_bits & 0x01 or info.flag_bits & 0x40:
                    raise VerificationError("archive", "encrypted")
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise VerificationError("archive", "duplicate")
            if set(names) != RELEASE_FILE_SET:
                raise VerificationError("archive", "entries")

            declared_total = 0
            for info in infos:
                size = info.file_size
                if (
                    not isinstance(size, int)
                    or isinstance(size, bool)
                    or size < 0
                ):
                    raise VerificationError("archive", "schema")
                if size > MAX_FILE_BYTES:
                    raise VerificationError("archive", "size")
                declared_total += size
                if declared_total > MAX_TOTAL_FILE_BYTES:
                    raise VerificationError("archive", "size")

            files: dict[str, bytes] = {}
            by_name = {info.filename: info for info in infos}
            for name in RELEASE_FILES:
                info = by_name[name]
                with archive.open(info) as handle:
                    content = handle.read(MAX_FILE_BYTES + 1)
                    if len(content) > MAX_FILE_BYTES:
                        raise VerificationError("archive", "size")
                    if handle.read(1):
                        raise VerificationError("archive", "size")
                if len(content) != info.file_size:
                    raise VerificationError("archive", "size")
                files[name] = content
            return files
    except VerificationError:
        raise
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        RuntimeError,
        NotImplementedError,
        OSError,
        ValueError,
        EOFError,
    ):
        raise VerificationError("archive", "zip") from None
    except Exception:
        raise VerificationError("archive", "zip") from None


def _read_evidence_binding(
    evidence_bytes: bytes,
    *,
    expected_source_sha: str,
    release_run_id: int,
    release_run_attempt: int,
) -> dict[str, Any]:
    try:
        value = json.loads(evidence_bytes.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError):
        raise VerificationError("evidence", "schema") from None
    if not isinstance(value, Mapping):
        raise VerificationError("evidence", "schema")
    if value.get("repository") != REPOSITORY:
        raise VerificationError("evidence", "repository")
    if (
        _source_sha(value.get("source_sha"), "evidence", "source")
        != expected_source_sha
    ):
        raise VerificationError("evidence", "source")
    if (
        _positive_int(
            value.get("release_run_id"), "evidence", "release-run-id"
        )
        != release_run_id
    ):
        raise VerificationError("evidence", "release-run-id")
    if (
        _positive_int(
            value.get("release_run_attempt"),
            "evidence",
            "release-run-attempt",
        )
        != release_run_attempt
    ):
        raise VerificationError("evidence", "release-run-attempt")
    ci_run_id = _positive_int(
        value.get("ci_run_id"), "evidence", "ci-run-id"
    )
    ci_run_attempt = _positive_int(
        value.get("ci_run_attempt"), "evidence", "ci-run-attempt"
    )
    bundle_artifact_id = _positive_int(
        value.get("bundle_artifact_id"), "evidence", "bundle-artifact-id"
    )
    bundle_artifact_digest = _digest(
        value.get("bundle_artifact_digest"),
        "evidence",
        "bundle-artifact-digest",
    )
    return {
        "ci_run_id": ci_run_id,
        "ci_run_attempt": ci_run_attempt,
        "bundle_artifact_id": bundle_artifact_id,
        "bundle_artifact_digest": bundle_artifact_digest,
    }


def _validate_references(
    value: Any,
    expected_app_digest: str | None,
    expected_migrate_digest: str | None,
) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(ROLES):
        raise VerificationError("verify", "result")
    expected = {
        "app": expected_app_digest,
        "migrate": expected_migrate_digest,
    }
    references: dict[str, str] = {}
    digests: dict[str, str] = {}
    for role in ROLES:
        reference = value.get(role)
        prefix = TARGETS[role] + "@"
        if not isinstance(reference, str) or not reference.startswith(prefix):
            raise VerificationError("verify", "result")
        digest = reference[len(prefix) :]
        _digest(digest, "verify", "result")
        if expected[role] is not None and digest != expected[role]:
            raise VerificationError("verify", "binding-" + role)
        references[role] = reference
        digests[role] = digest
    if digests["app"] == digests["migrate"]:
        raise VerificationError("verify", "result")
    return references


def _run_offline_verifier(
    verifier: Any,
    files: Mapping[str, bytes],
    run_metadata_bytes: bytes,
    *,
    expected_source_sha: str,
    release_run_id: int,
    release_run_attempt: int,
    expected_app_digest: str | None,
    expected_migrate_digest: str | None,
) -> dict[str, str]:
    verify_release = getattr(verifier, "verify_release", None)
    if not callable(verify_release):
        raise VerificationError("internal", "unexpected")
    try:
        value = verify_release(
            files["release-summary.json"],
            files["descriptor.json"],
            files["release-evidence.json"],
            run_metadata_bytes,
            expected_source_sha=expected_source_sha,
            release_run_id=release_run_id,
            release_run_attempt=release_run_attempt,
            expected_app_digest=expected_app_digest,
            expected_migrate_digest=expected_migrate_digest,
        )
    except Exception as exc:
        raise _offline_failure(exc) from None
    return _validate_references(
        value, expected_app_digest, expected_migrate_digest
    )


def verify_online(
    *,
    token: str,
    expected_source_sha: str,
    release_run_id: int,
    release_run_attempt: int,
    expected_app_digest: str | None = None,
    expected_migrate_digest: str | None = None,
    api_transport: Any | None = None,
    download_transport: Any | None = None,
    verifier: Any | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, str]:
    """Verify one release through GitHub and return the paired references.

    ``token`` is sent only to ``https://api.github.com``.  The signed storage
    download uses ``download_transport`` (or :class:`SignedHttpsTransport`)
    and receives no bearer token, cookies, or referrer.

    ``verifier`` defaults to the canonical offline verifier, loaded lazily.
    It must expose ``verify_release`` with the same contract.

    Calls within this process are serialized by a deadline guard lock; a
    concurrent call fails with ``verify/deadline-conflict`` before network
    access instead of waiting.

    Raises :class:`VerificationError` with a fixed ``stage``/``kind`` on every
    failure.  No partial reference mapping is returned.
    """

    if not _DEADLINE_LOCK.acquire(blocking=False):
        raise VerificationError("verify", "deadline-conflict")
    try:
        try:
            with _WallDeadline(TOTAL_TIMEOUT_SECONDS):
                return _verify_online(
                    token=token,
                    expected_source_sha=expected_source_sha,
                    release_run_id=release_run_id,
                    release_run_attempt=release_run_attempt,
                    expected_app_digest=expected_app_digest,
                    expected_migrate_digest=expected_migrate_digest,
                    api_transport=api_transport,
                    download_transport=download_transport,
                    verifier=verifier,
                    clock=clock,
                )
        except _DeadlineExceeded:
            raise VerificationError("verify", "deadline") from None
        except VerificationError:
            raise
        except ci.PreflightError as exc:
            _raise_checker_failure(exc)
        except Exception:
            raise VerificationError("internal", "unexpected") from None
    finally:
        _DEADLINE_LOCK.release()


def _verify_online(
    *,
    token: str,
    expected_source_sha: str,
    release_run_id: int,
    release_run_attempt: int,
    expected_app_digest: str | None,
    expected_migrate_digest: str | None,
    api_transport: Any | None,
    download_transport: Any | None,
    verifier: Any | None,
    clock: Callable[[], float] | None,
) -> dict[str, str]:
    (
        source_sha,
        run_id,
        attempt,
        app_digest,
        migrate_digest,
    ) = _validate_inputs(
        token,
        expected_source_sha,
        release_run_id,
        release_run_attempt,
        expected_app_digest,
        expected_migrate_digest,
    )
    selected_verifier = verifier if verifier is not None else _default_verifier()
    now = clock if clock is not None else time.monotonic
    if not callable(now):
        raise VerificationError("internal", "unexpected")
    try:
        deadline = now() + TOTAL_TIMEOUT_SECONDS
    except Exception:
        raise VerificationError("internal", "unexpected") from None
    base_transport = (
        api_transport if api_transport is not None else ci.HttpsTransport()
    )
    transport = _StrictJsonTransport(base_transport)
    signed_transport = (
        download_transport
        if download_transport is not None
        else SignedHttpsTransport()
    )
    api = ci.GitHubApi(token, transport=transport, clock=now)

    release_workflow = api.workflow(RELEASE_WORKFLOW_PATH)
    release_workflow_id = _workflow_id_for_history(
        release_workflow, RELEASE_WORKFLOW_PATH
    )

    release_run_raw = api.run_attempt(run_id, attempt)
    repository_id = _parse_release_run(
        release_run_raw,
        workflow_id=release_workflow_id,
        source_sha=source_sha,
        run_id=run_id,
        attempt=attempt,
    )
    try:
        run_metadata_bytes = (
            json.dumps(
                release_run_raw,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError):
        raise VerificationError("run", "schema") from None

    release_artifacts = api.run_artifacts(run_id)
    release_artifact = _select_artifact(
        release_artifacts,
        stage="artifact",
        expected_name=f"release-summary-{run_id}-{attempt}",
        run_id=run_id,
        workflow_id=release_workflow_id,
        source_sha=source_sha,
        repository_id=repository_id,
        maximum_bytes=MAX_ARCHIVE_BYTES,
    )
    release_artifact_id = _positive_int(
        release_artifact.get("id"), "artifact", "schema"
    )
    release_artifact_size = _positive_int(
        release_artifact.get("size_in_bytes"), "artifact", "schema"
    )
    release_artifact_digest = _digest(
        release_artifact.get("digest"), "artifact", "digest"
    )

    location = _artifact_zip_location(
        transport,
        token,
        release_artifact_id,
        deadline,
        now,
    )
    archive_bytes = _download_archive(
        signed_transport,
        location,
        release_artifact_size,
        release_artifact_digest,
        deadline,
        now,
    )
    files = _read_release_archive(archive_bytes)

    references = _run_offline_verifier(
        selected_verifier,
        files,
        run_metadata_bytes,
        expected_source_sha=source_sha,
        release_run_id=run_id,
        release_run_attempt=attempt,
        expected_app_digest=app_digest,
        expected_migrate_digest=migrate_digest,
    )

    binding = _read_evidence_binding(
        files["release-evidence.json"],
        expected_source_sha=source_sha,
        release_run_id=run_id,
        release_run_attempt=attempt,
    )
    ci_run_id = binding["ci_run_id"]
    ci_run_attempt = binding["ci_run_attempt"]

    ci_workflow = api.workflow(CI_WORKFLOW_PATH)
    ci_workflow_id = _workflow_id_for_history(ci_workflow, CI_WORKFLOW_PATH)
    ci_run_raw = api.run_attempt(ci_run_id, ci_run_attempt)
    ci_repository_id = _validate_ci_run(
        ci_run_raw,
        workflow_id=ci_workflow_id,
        source_sha=source_sha,
        run_id=ci_run_id,
        attempt=ci_run_attempt,
    )
    if ci_repository_id != repository_id:
        raise VerificationError("ci-run", "repository")
    ci_artifacts = api.run_artifacts(ci_run_id)
    ci_artifact = _select_artifact(
        ci_artifacts,
        stage="ci-artifact",
        expected_name=(
            f"ci-release-bundle-{ci_run_id}-{ci_run_attempt}"
        ),
        run_id=ci_run_id,
        workflow_id=ci_workflow_id,
        source_sha=source_sha,
        repository_id=ci_repository_id,
        maximum_bytes=None,
    )
    if (
        _positive_int(ci_artifact.get("id"), "ci-artifact", "schema")
        != binding["bundle_artifact_id"]
    ):
        raise VerificationError("ci-artifact", "id")
    if (
        _digest(ci_artifact.get("digest"), "ci-artifact", "digest")
        != binding["bundle_artifact_digest"]
    ):
        raise VerificationError("ci-artifact", "digest")

    return references


__all__ = [
    "MAX_ARCHIVE_BYTES",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_FILE_BYTES",
    "SignedHttpsTransport",
    "VerificationError",
    "verify_online",
]
