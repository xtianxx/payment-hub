#!/usr/bin/env python3
"""T080 fixed local HTTP demo.

The demo is a small client, not a new payment platform. It performs exactly one
five-step HTTP flow against an already-running local application, prints only a
whitelisted typed report, and never reads the database, registry, image
metadata, or scenario-control fields.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

SCHEMA = "payment-hub.demo.v1"
TOTAL_BUDGET_SECONDS = 120.0
HTTP_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 64 * 1024
API_KEY_ENV = "PAYMENT_HUB_API_KEY"
EXPECTED_RESULTS = ("SUCCEEDED", "FAILED", "UNKNOWN")
PAYMENT_FIELDS = {
    "payment_id",
    "business_order_no",
    "amount",
    "currency",
    "result",
    "registered_at",
    "confirmed_at",
}
ISSUE_CODES = {
    "MOCK_RESULT_UNCONFIRMED",
    "MOCK_RESULT_NOT_FOUND",
    "MOCK_EVIDENCE_REJECTED",
    "RESULT_SAVE_ERROR",
    "TRACE_SAVE_ERROR",
}
UUID4_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
BUSINESS_ORDER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
AMOUNT_PATTERN = re.compile(r"^[0-9]+$")


class DemoError(Exception):
    """A fixed, non-sensitive demo failure category."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


class _DeadlineGuard:
    """Absolute SIGALRM deadline for the whole demo and one HTTP operation.

    The guard only installs on the Linux main thread and never replaces an
    earlier caller deadline.  The previous handler and timer are restored after
    the block, including on exceptions.
    """

    def __init__(self, seconds: float, category: str) -> None:
        self.seconds = seconds
        self.category = category
        self.old_handler: Any = None
        self.old_remaining = 0.0
        self.old_interval = 0.0
        self.old_deadline: float | None = None
        self.installed = False

    def __enter__(self) -> "_DeadlineGuard":
        if (
            not hasattr(signal, "SIGALRM")
            or not hasattr(signal, "setitimer")
            or not hasattr(signal, "getitimer")
            or threading.current_thread() is not threading.main_thread()
        ):
            raise DemoError("deadline-unsupported")
        if self.seconds <= 0:
            raise DemoError(self.category)
        try:
            old_handler = signal.getsignal(signal.SIGALRM)
            old_remaining, old_interval = signal.getitimer(signal.ITIMER_REAL)
        except (AttributeError, OSError, ValueError) as exc:
            raise DemoError("deadline-unsupported") from exc
        now = time.monotonic()
        if old_remaining > 0 and old_remaining <= self.seconds:
            # The caller's earlier absolute deadline remains authoritative.
            return self
        self.old_handler = old_handler
        self.old_remaining = old_remaining
        self.old_interval = old_interval
        if old_remaining > 0:
            self.old_deadline = now + old_remaining
        try:
            signal.signal(signal.SIGALRM, self._handler)
            signal.setitimer(signal.ITIMER_REAL, self.seconds)
        except (AttributeError, OSError, ValueError) as exc:
            try:
                signal.signal(signal.SIGALRM, old_handler)
                if old_remaining > 0:
                    signal.setitimer(
                        signal.ITIMER_REAL, old_remaining, old_interval
                    )
            except (AttributeError, OSError, ValueError):
                pass
            raise DemoError("deadline-unsupported") from exc
        self.installed = True
        return self

    def _handler(self, _signum, _frame) -> None:
        raise DemoError(self.category)

    def __exit__(self, _exc_type, _exc, _traceback) -> bool:
        if not self.installed:
            return False
        try:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
        finally:
            signal.signal(signal.SIGALRM, self.old_handler)
            if self.old_deadline is not None:
                remaining = self.old_deadline - time.monotonic()
                if remaining <= 0:
                    remaining = 1e-6
                signal.setitimer(
                    signal.ITIMER_REAL, remaining, self.old_interval
                )
        return False


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DemoError("config")

    def exit(self, status: int = 0, message: str | None = None) -> None:
        raise DemoError("config")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def _reject(self, req, fp, code, msg, headers):
        raise DemoError("redirect")

    http_error_301 = _reject
    http_error_302 = _reject
    http_error_303 = _reject
    http_error_307 = _reject
    http_error_308 = _reject


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _Parser(
        description="Fixed T080 local HTTP demo; no scenario-control fields."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--business-order-no", required=True)
    parser.add_argument("--amount", required=True)
    parser.add_argument("--expect-result", required=True)
    return parser.parse_args(argv)


def parse_base_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise DemoError("config") from exc
    if parsed.scheme != "http":
        raise DemoError("config")
    if parsed.username is not None or parsed.password is not None:
        raise DemoError("config")
    if parsed.query or parsed.fragment:
        raise DemoError("config")
    if parsed.path not in ("", "/"):
        raise DemoError("config")
    host = parsed.hostname
    if host not in ("127.0.0.1", "::1"):
        raise DemoError("config")
    try:
        port = parsed.port
    except ValueError as exc:
        raise DemoError("config") from exc
    if port is None or port < 1 or port > 65535:
        raise DemoError("config")
    host_text = "[::1]" if host == "::1" else host
    return f"http://{host_text}:{port}"


def parse_business_order_no(value: str) -> str:
    if not BUSINESS_ORDER_PATTERN.fullmatch(value):
        raise DemoError("config")
    return value


def parse_amount(value: str) -> int:
    if not AMOUNT_PATTERN.fullmatch(value):
        raise DemoError("config")
    amount = int(value)
    if amount < 1 or amount > 1_000_000:
        raise DemoError("config")
    return amount


def parse_expect_result(value: str) -> str:
    if value not in EXPECTED_RESULTS:
        raise DemoError("config")
    return value


def read_api_key() -> str:
    value = os.environ.get(API_KEY_ENV, "")
    if not value:
        raise DemoError("api-key-missing")
    return value


def build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect()
    )


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DemoError("http-timeout")
    return remaining


def _require_alive(deadline: float) -> None:
    if deadline - time.monotonic() <= 0:
        raise DemoError("http-timeout")


def _request(
    opener: urllib.request.OpenerDirector,
    method: str,
    url: str,
    api_key: str,
    payload: Mapping[str, Any] | None,
    deadline: float,
) -> tuple[int, Mapping[str, str], bytes]:
    seconds = min(HTTP_TIMEOUT_SECONDS, _remaining(deadline))
    with _DeadlineGuard(seconds, "http-timeout"):
        return _perform_request(
            opener, method, url, api_key, payload, seconds, deadline
        )


def _perform_request(
    opener: urllib.request.OpenerDirector,
    method: str,
    url: str,
    api_key: str,
    payload: Mapping[str, Any] | None,
    timeout: float,
    deadline: float,
) -> tuple[int, Mapping[str, str], bytes]:
    headers = {
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
    }
    data = None
    if payload is not None:
        data = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            response_headers = response.headers
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            status = exc.code
            response_headers = exc.headers
            body = exc.read(MAX_RESPONSE_BYTES + 1)
        finally:
            try:
                exc.close()
            except OSError:
                pass
    except DemoError:
        raise
    except (socket.timeout, TimeoutError) as exc:
        raise DemoError("http-timeout") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise DemoError("http-timeout") from exc
        raise DemoError("http-error") from exc
    except OSError as exc:
        raise DemoError("http-error") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise DemoError("response-too-large")
    _require_alive(deadline)
    return status, response_headers, body


def _require_status(status: int, want: int) -> None:
    if status != want:
        raise DemoError("http-status")


def _require_common_headers(headers: Mapping[str, str]) -> None:
    content_type = headers.get("Content-Type", "")
    if not content_type.lower().startswith("application/json"):
        raise DemoError("response-schema")
    cache_control = headers.get("Cache-Control", "")
    if "no-store" not in cache_control.lower():
        raise DemoError("response-schema")
    if headers.get("ETag") or headers.get("Last-Modified"):
        raise DemoError("response-schema")


def _decode_json(body: bytes) -> dict[str, Any]:
    if not body:
        raise DemoError("response-schema")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DemoError("response-schema") from exc
    if not isinstance(value, dict):
        raise DemoError("response-schema")
    return value


def _request_id(value: Any) -> str:
    if not isinstance(value, str) or not UUID4_PATTERN.fullmatch(value):
        raise DemoError("response-schema")
    return value


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DemoError("response-schema")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise DemoError("response-schema") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise DemoError("response-schema")
    return value


def _issue_codes(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise DemoError("response-schema")
    codes = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"code", "message"}:
            raise DemoError("response-schema")
        code = item.get("code")
        message = item.get("message")
        if code not in ISSUE_CODES or not isinstance(message, str) or not message:
            raise DemoError("response-schema")
        codes.append(code)
    return sorted(set(codes))


def _payment(
    value: Any,
    business_order_no: str,
    amount: int,
    expect_result: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != PAYMENT_FIELDS:
        raise DemoError("response-schema")
    payment_id = value.get("payment_id")
    if not isinstance(payment_id, str) or not UUID4_PATTERN.fullmatch(payment_id):
        raise DemoError("response-schema")
    if value.get("business_order_no") != business_order_no:
        raise DemoError("response-schema")
    stored_amount = value.get("amount")
    if (
        not isinstance(stored_amount, int)
        or isinstance(stored_amount, bool)
        or stored_amount < 1
        or stored_amount > 1_000_000
    ):
        raise DemoError("response-schema")
    if value.get("currency") != "CNY":
        raise DemoError("response-schema")
    result = value.get("result")
    if result not in EXPECTED_RESULTS:
        raise DemoError("response-schema")
    if result != expect_result:
        raise DemoError("expectation-mismatch")
    _timestamp(value.get("registered_at"))
    confirmed_at = value.get("confirmed_at")
    if result == "UNKNOWN":
        if confirmed_at is not None:
            raise DemoError("response-schema")
    else:
        if confirmed_at is None:
            raise DemoError("response-schema")
        _timestamp(confirmed_at)
    return value


def _payment_response(
    body: bytes,
    status: int,
    headers: Mapping[str, str],
    business_order_no: str,
    amount: int,
    expect_result: str,
) -> tuple[str, dict[str, Any], list[str]]:
    _require_common_headers(headers)
    envelope = _decode_json(body)
    request_id = _request_id(envelope.get("request_id"))
    if "payment" not in envelope or "error" in envelope:
        raise DemoError("response-schema")
    if set(envelope) - {"request_id", "payment", "issues"}:
        raise DemoError("response-schema")
    payment = _payment(
        envelope.get("payment"), business_order_no, amount, expect_result
    )
    issues = _issue_codes(envelope.get("issues"))
    return request_id, payment, issues


def _error_response(
    body: bytes, headers: Mapping[str, str], want_code: str
) -> tuple[str, str]:
    _require_common_headers(headers)
    envelope = _decode_json(body)
    request_id = _request_id(envelope.get("request_id"))
    if set(envelope) != {"request_id", "error"}:
        raise DemoError("response-schema")
    error = envelope.get("error")
    if not isinstance(error, dict) or set(error) != {"code", "message"}:
        raise DemoError("response-schema")
    code = error.get("code")
    message = error.get("message")
    if not isinstance(code, str) or not isinstance(message, str) or not message:
        raise DemoError("response-schema")
    if code != want_code:
        raise DemoError("response-schema")
    return request_id, code


def _same_payment(previous: Mapping[str, Any], current: Mapping[str, Any]) -> None:
    for key in (
        "payment_id",
        "business_order_no",
        "amount",
        "currency",
        "result",
        "registered_at",
        "confirmed_at",
    ):
        if previous.get(key) != current.get(key):
            raise DemoError("response-changed")


def _remember_request_id(request_id: str, seen: set[str]) -> None:
    if request_id in seen:
        raise DemoError("response-changed")
    seen.add(request_id)


def run_demo(
    base_url: str,
    business_order_no: str,
    amount: int,
    expect_result: str,
    api_key: str,
    opener: urllib.request.OpenerDirector | None = None,
) -> dict[str, Any]:
    opener = opener or build_opener()
    with _DeadlineGuard(TOTAL_BUDGET_SECONDS, "http-timeout"):
        deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
        return _run_demo_steps(
            base_url,
            business_order_no,
            amount,
            expect_result,
            api_key,
            opener,
            deadline,
        )


def _run_demo_steps(
    base_url: str,
    business_order_no: str,
    amount: int,
    expect_result: str,
    api_key: str,
    opener: urllib.request.OpenerDirector,
    deadline: float,
) -> dict[str, Any]:
    payload = {
        "business_order_no": business_order_no,
        "amount": amount,
        "currency": "CNY",
    }
    steps: list[dict[str, Any]] = []
    seen_request_ids: set[str] = set()
    original_payment: dict[str, Any] | None = None

    status, headers, body = _request(
        opener,
        "POST",
        base_url + "/api/v1/payments",
        api_key,
        payload,
        deadline,
    )
    _require_status(status, 201)
    request_id, original_payment, issues = _payment_response(
        body, status, headers, business_order_no, amount, expect_result
    )
    _remember_request_id(request_id, seen_request_ids)
    if original_payment["amount"] != amount:
        raise DemoError("response-schema")
    if headers.get("Location") != "/api/v1/payments/" + original_payment["payment_id"]:
        raise DemoError("response-schema")
    steps.append(
        {
            "step": "post",
            "status": status,
            "request_id": request_id,
            "payment_id": original_payment["payment_id"],
            "result": original_payment["result"],
            "issue_codes": issues,
        }
    )

    status, headers, body = _request(
        opener,
        "GET",
        base_url
        + "/api/v1/payments/"
        + urllib.parse.quote(original_payment["payment_id"], safe=""),
        api_key,
        None,
        deadline,
    )
    _require_status(status, 200)
    request_id, payment, issues = _payment_response(
        body, status, headers, business_order_no, amount, expect_result
    )
    _remember_request_id(request_id, seen_request_ids)
    _same_payment(original_payment, payment)
    steps.append(
        {
            "step": "get_by_id",
            "status": status,
            "request_id": request_id,
            "payment_id": payment["payment_id"],
            "result": payment["result"],
            "issue_codes": issues,
        }
    )

    status, headers, body = _request(
        opener,
        "GET",
        base_url
        + "/api/v1/payments?"
        + urllib.parse.urlencode({"business_order_no": business_order_no}),
        api_key,
        None,
        deadline,
    )
    _require_status(status, 200)
    request_id, payment, issues = _payment_response(
        body, status, headers, business_order_no, amount, expect_result
    )
    _remember_request_id(request_id, seen_request_ids)
    _same_payment(original_payment, payment)
    steps.append(
        {
            "step": "get_by_business",
            "status": status,
            "request_id": request_id,
            "payment_id": payment["payment_id"],
            "result": payment["result"],
            "issue_codes": issues,
        }
    )

    status, headers, body = _request(
        opener,
        "POST",
        base_url + "/api/v1/payments",
        api_key,
        payload,
        deadline,
    )
    _require_status(status, 200)
    request_id, payment, issues = _payment_response(
        body, status, headers, business_order_no, amount, expect_result
    )
    _remember_request_id(request_id, seen_request_ids)
    _same_payment(original_payment, payment)
    steps.append(
        {
            "step": "replay",
            "status": status,
            "request_id": request_id,
            "payment_id": payment["payment_id"],
            "result": payment["result"],
            "issue_codes": issues,
        }
    )

    conflict_amount = 2 if amount == 1 else 1
    conflict_payload = {
        "business_order_no": business_order_no,
        "amount": conflict_amount,
        "currency": "CNY",
    }
    status, headers, body = _request(
        opener,
        "POST",
        base_url + "/api/v1/payments",
        api_key,
        conflict_payload,
        deadline,
    )
    _require_status(status, 409)
    request_id, _code = _error_response(body, headers, "PAYMENT_CONFLICT")
    _remember_request_id(request_id, seen_request_ids)
    steps.append(
        {
            "step": "conflict",
            "status": status,
            "request_id": request_id,
            "payment_id": "",
            "result": "",
            "issue_codes": [],
        }
    )

    trace_complete = expect_result != "UNKNOWN" and all(
        not step["issue_codes"] for step in steps
    )
    _require_alive(deadline)
    return {
        "schema": SCHEMA,
        "steps": steps,
        "trace_complete": trace_complete,
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    opener: urllib.request.OpenerDirector | None = None,
    stdout=None,
    stderr=None,
) -> int:
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    try:
        args = parse_args(argv)
        base_url = parse_base_url(args.base_url)
        business_order_no = parse_business_order_no(args.business_order_no)
        amount = parse_amount(args.amount)
        expect_result = parse_expect_result(args.expect_result)
        api_key = read_api_key()
        report = run_demo(
            base_url,
            business_order_no,
            amount,
            expect_result,
            api_key,
            opener=opener,
        )
        json.dump(report, out, separators=(",", ":"), ensure_ascii=True)
        out.write("\n")
        return 0
    except DemoError as exc:
        print("demo: error=" + exc.category, file=err)
        return 1
    except Exception:
        print("demo: error=internal", file=err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
