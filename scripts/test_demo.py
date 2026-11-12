"""Focused pure-Python tests for the T080 local HTTP demo.

These tests use only a local loopback fake HTTP server and an injected in-process
runner.  They never invoke Go, PostgreSQL, Docker, the network, or the real
application.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import signal
import socket
import sys
import threading
import time
import unittest
import urllib.parse
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "demo.py"
SPEC = importlib.util.spec_from_file_location("payment_hub_demo", SCRIPT)
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)

DEMO_KEY = "FAKE-DEMO-KEY-0123456789"
CANARY = "DEMO_CANARY_RAW_BODY_9f2b"
BASE_ORDER = "M-DEMO-100"
STEPS = ("post", "get_by_id", "get_by_business", "replay", "conflict")
STATUSES = (201, 200, 200, 200, 409)


def uuid4(value):
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


class FakeState:
    def __init__(self, scenario="SUCCEEDED", mode="normal"):
        self.scenario = scenario
        self.mode = mode
        self.order_id = str(uuid.uuid4())
        self.created = False
        self.business_order_no = None
        self.amount = None
        self.payment = None
        self.requests = []
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.delay_seconds = 0.1
        self.first_post_request_id = None
        self.first_get_request_id = None

    def _now(self):
        return utc_now()

    def _payment(self, business_order_no, amount):
        confirmed = None
        if self.scenario != "UNKNOWN":
            confirmed = self._now()
        return {
            "payment_id": self.order_id,
            "business_order_no": business_order_no,
            "amount": amount,
            "currency": "CNY",
            "result": self.scenario,
            "registered_at": self._now(),
            "confirmed_at": confirmed,
        }

    def _issues(self, kind):
        if self.scenario != "UNKNOWN":
            return []
        if kind == "post":
            return [
                {
                    "code": "MOCK_RESULT_UNCONFIRMED",
                    "message": "mock result unconfirmed",
                }
            ]
        return [
            {
                "code": "MOCK_RESULT_NOT_FOUND",
                "message": "mock result not found",
            }
        ]

    def _payment_envelope(self, payment, issues, request_id=None):
        request_id = request_id or str(uuid.uuid4())
        envelope = {"request_id": request_id, "payment": payment}
        if issues:
            envelope["issues"] = issues
        return envelope

    def _error_envelope(self, code, message, request_id=None):
        return {
            "request_id": request_id or str(uuid.uuid4()),
            "error": {"code": code, "message": message},
        }

    def _json(self, status, payload, headers=None):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        merged = {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        }
        if headers:
            merged.update(headers)
        return status, merged, body

    def dispatch(self, handler):
        length = int(handler.headers.get("Content-Length", "0") or "0")
        body = handler.rfile.read(length) if length else b""
        request = {
            "method": handler.command,
            "path": handler.path,
            "headers": dict(handler.headers),
            "body": body,
        }
        with self.lock:
            self.requests.append(request)
            first = len(self.requests) == 1
        if self.mode == "timeout" and first:
            self.stop_event.wait(1.0)
        if self.mode == "delay":
            self.stop_event.wait(self.delay_seconds)
        if self.mode == "slow-headers":
            self._slow_headers(handler)
            return
        if self.mode == "slow-body":
            self._slow_body(handler)
            return
        if self.mode == "redirect" and first:
            status, headers, response = 302, {"Location": "/redirected"}, b""
        else:
            status, headers, response = self.handle(
                handler.command, handler.path, body
            )
        try:
            handler.send_response(status)
            for name, value in headers.items():
                handler.send_header(name, value)
            handler.send_header("Content-Length", str(len(response)))
            handler.end_headers()
            if response:
                handler.wfile.write(response)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _slow_headers(self, handler):
        try:
            handler.wfile.write(b"HTTP/1.1 200 OK\r\n")
            handler.wfile.flush()
            for _ in range(40):
                if self.stop_event.wait(0.05):
                    return
                handler.wfile.write(b"X-Slow: 1\r\n")
                handler.wfile.flush()
            handler.wfile.write(b"\r\n")
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _slow_body(self, handler):
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Content-Length", "4096")
            handler.end_headers()
            for _ in range(40):
                if self.stop_event.wait(0.05):
                    return
                handler.wfile.write(b" ")
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def handle(self, method, raw_path, body):
        parsed = urllib.parse.urlsplit(raw_path)
        if method == "POST" and parsed.path == "/api/v1/payments":
            return self._post(body)
        if method == "GET" and parsed.path == "/api/v1/payments":
            query = urllib.parse.parse_qs(parsed.query)
            values = query.get("business_order_no", [""])
            return self._get_business(values[0] if values else "")
        if method == "GET" and parsed.path.startswith("/api/v1/payments/"):
            return self._get_id(parsed.path.rsplit("/", 1)[-1])
        return self._json(404, self._error_envelope("PAYMENT_NOT_FOUND", "not found"))

    def _post(self, body):
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json(400, self._error_envelope("INVALID_REQUEST", "invalid"))
        business = value.get("business_order_no")
        amount = value.get("amount")
        first = not self.created
        if first:
            if self.mode == "canary-error":
                return self._json(
                    400,
                    self._error_envelope("INVALID_REQUEST", CANARY),
                )
            if self.mode == "schema":
                payment = self._payment(business, amount)
                payment.pop("currency", None)
                return self._json(201, {"request_id": str(uuid.uuid4()), "payment": payment})
            if self.mode == "wrong-status":
                payment = self._payment(business, amount)
                self.payment = payment
                self.business_order_no = business
                self.amount = amount
                self.created = True
                return self._json(200, self._payment_envelope(payment, []))
            if self.mode == "not-confirmed":
                payment = self._payment(business, amount)
                payment["confirmed_at"] = None
                self.payment = payment
                self.business_order_no = business
                self.amount = amount
                self.created = True
                return self._json(201, self._payment_envelope(payment, []))
            self.created = True
            self.business_order_no = business
            self.amount = amount
            self.payment = self._payment(business, amount)
            self.first_post_request_id = str(uuid.uuid4())
            issues = self._issues("post")
            return self._json(
                201,
                self._payment_envelope(
                    self.payment, issues, self.first_post_request_id
                ),
                {"Location": "/api/v1/payments/" + self.order_id},
            )
        if business == self.business_order_no and amount == self.amount:
            return self._json(200, self._payment_envelope(self.payment, []))
        return self._json(
            409,
            self._error_envelope("PAYMENT_CONFLICT", "amount or currency conflict"),
        )

    def _get_id(self, payment_id):
        if payment_id != self.order_id or not self.created:
            return self._json(
                404, self._error_envelope("PAYMENT_NOT_FOUND", "not found")
            )
        payment = dict(self.payment)
        if self.mode == "changed":
            payment["amount"] = payment["amount"] + 1
        request_id = str(uuid.uuid4())
        if self.mode == "duplicate-request-id":
            request_id = self.first_post_request_id or request_id
        return self._json(
            200,
            self._payment_envelope(payment, self._issues("get"), request_id),
        )

    def _get_business(self, business_order_no):
        if business_order_no != self.business_order_no or not self.created:
            return self._json(
                404, self._error_envelope("PAYMENT_NOT_FOUND", "not found")
            )
        return self._json(
            200,
            self._payment_envelope(dict(self.payment), self._issues("get")),
        )


class FakeServer:
    def __init__(self, scenario="SUCCEEDED", mode="normal"):
        self.state = FakeState(scenario, mode)
        self.stop_event = threading.Event()
        self.state.stop_event = self.stop_event
        self.httpd = None
        self.thread = None

    def __enter__(self):
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                state.dispatch(self)

            def do_GET(self):
                state.dispatch(self)

            def log_message(self, *_args):
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = False
        self.httpd.block_on_close = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.stop_event.set()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=3)

    @property
    def base_url(self):
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"


class DemoUnitTests(unittest.TestCase):
    def run_demo(
        self,
        server,
        *,
        expect="SUCCEEDED",
        base_url=None,
        business_order_no=BASE_ORDER,
        amount="100",
        key=DEMO_KEY,
    ):
        argv = [
            "--base-url",
            server.base_url if base_url is None else base_url,
            "--business-order-no",
            business_order_no,
            "--amount",
            amount,
            "--expect-result",
            expect,
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()
        env = {} if key is None else {"PAYMENT_HUB_API_KEY": key}
        with mock.patch.dict(os.environ, env, clear=False):
            if key is None:
                os.environ.pop("PAYMENT_HUB_API_KEY", None)
            code = demo.main(argv, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_budget_constants_are_fixed(self):
        self.assertEqual(demo.TOTAL_BUDGET_SECONDS, 120)
        self.assertEqual(demo.HTTP_TIMEOUT_SECONDS, 20)
        self.assertEqual(demo.SCHEMA, "payment-hub.demo.v1")

    def test_same_five_steps_for_three_expectations(self):
        for expect in ("SUCCEEDED", "FAILED", "UNKNOWN"):
            with self.subTest(expect):
                with FakeServer(scenario=expect) as server:
                    code, stdout, stderr = self.run_demo(server, expect=expect)
                    self.assertEqual(code, 0, stderr)
                    self.assertEqual(stderr, "")
                    report = json.loads(stdout)
                    self.assertEqual(
                        set(report), {"schema", "steps", "trace_complete"}
                    )
                    self.assertEqual(report["schema"], "payment-hub.demo.v1")
                    steps = report["steps"]
                    self.assertEqual(tuple(step["step"] for step in steps), STEPS)
                    self.assertEqual(
                        tuple(step["status"] for step in steps), STATUSES
                    )
                    request_ids = []
                    payment_ids = []
                    for step in steps:
                        self.assertEqual(
                            set(step),
                            {
                                "step",
                                "status",
                                "request_id",
                                "payment_id",
                                "result",
                                "issue_codes",
                            },
                        )
                        self.assertTrue(uuid4(step["request_id"]))
                        request_ids.append(step["request_id"])
                    self.assertEqual(len(set(request_ids)), 5)
                    for step in steps[:4]:
                        self.assertEqual(step["result"], expect)
                        self.assertTrue(uuid4(step["payment_id"]))
                        payment_ids.append(step["payment_id"])
                    self.assertEqual(len(set(payment_ids)), 1)
                    self.assertEqual(steps[4]["payment_id"], "")
                    self.assertEqual(steps[4]["result"], "")
                    self.assertEqual(steps[4]["issue_codes"], [])
                    self.assertEqual(
                        report["trace_complete"], expect != "UNKNOWN"
                    )
                    if expect == "UNKNOWN":
                        self.assertTrue(
                            any(step["issue_codes"] for step in steps)
                        )
                    else:
                        self.assertTrue(
                            all(not step["issue_codes"] for step in steps)
                        )

                    requests = list(server.state.requests)
                    posts = [r for r in requests if r["method"] == "POST"]
                    gets = [r for r in requests if r["method"] == "GET"]
                    self.assertEqual(len(posts), 3)
                    self.assertEqual(len(gets), 2)
                    self.assertTrue(all(r["method"] in ("GET", "POST") for r in requests))
                    for request in posts:
                        payload = json.loads(request["body"].decode("utf-8"))
                        self.assertEqual(
                            set(payload),
                            {"business_order_no", "amount", "currency"},
                        )
                        self.assertEqual(payload["business_order_no"], BASE_ORDER)
                        self.assertEqual(payload["currency"], "CNY")
                        self.assertNotIn("expect", payload)
                        self.assertNotIn("scenario", payload)
                    conflict_payload = json.loads(posts[2]["body"].decode("utf-8"))
                    self.assertIn(conflict_payload["amount"], (1, 2))
                    self.assertNotEqual(conflict_payload["amount"], 100)
                    get_paths = [
                        urllib.parse.urlsplit(request["path"]).path
                        for request in gets
                    ]
                    self.assertEqual(
                        sum(path == "/api/v1/payments" for path in get_paths), 1
                    )
                    self.assertEqual(
                        sum(
                            path.startswith("/api/v1/payments/")
                            and path != "/api/v1/payments"
                            for path in get_paths
                        ),
                        1,
                    )
                    for request in gets:
                        parsed = urllib.parse.urlsplit(request["path"])
                        if parsed.path != "/api/v1/payments":
                            continue
                        query = urllib.parse.parse_qs(parsed.query)
                        self.assertEqual(
                            query.get("business_order_no"), [BASE_ORDER]
                        )

    def test_missing_key_and_invalid_inputs_fail_before_http(self):
        with FakeServer() as server:
            cases = [
                ("api-key-missing", {"key": None}),
                ("config", {"base_url": "https://127.0.0.1:1"}),
                ("config", {"base_url": "http://localhost:1"}),
                ("config", {"base_url": "http://127.0.0.1"}),
                ("config", {"base_url": "http://user@127.0.0.1:1"}),
                ("config", {"base_url": "http://127.0.0.1:1/path"}),
                ("config", {"base_url": "http://127.0.0.1:1?x=1"}),
                ("config", {"base_url": "http://[::2]:1"}),
                ("config", {"business_order_no": ""}),
                ("config", {"business_order_no": "a" * 65}),
                ("config", {"business_order_no": "M 1"}),
                ("config", {"business_order_no": "订单"}),
                ("config", {"amount": "0"}),
                ("config", {"amount": "1000001"}),
                ("config", {"amount": "-1"}),
                ("config", {"amount": "1.5"}),
                ("config", {"amount": "1e2"}),
                ("config", {"amount": " 1"}),
                ("config", {"expect": "SUCCESS"}),
            ]
            for expected, overrides in cases:
                with self.subTest(expected=expected, overrides=overrides):
                    server.state.requests.clear()
                    code, stdout, stderr = self.run_demo(
                        server, **overrides
                    )
                    self.assertEqual(code, 1)
                    self.assertEqual(stdout, "")
                    self.assertIn("error=" + expected, stderr)
                    self.assertEqual(server.state.requests, [])

    def test_payload_has_no_control_fields_and_proxy_is_disabled(self):
        with FakeServer() as server:
            with mock.patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "HTTPS_PROXY": "http://127.0.0.1:1",
                    "ALL_PROXY": "http://127.0.0.1:1",
                },
                clear=False,
            ):
                code, stdout, stderr = self.run_demo(server)
            self.assertEqual(code, 0, stderr)
            self.assertTrue(stdout)
            self.assertEqual(len(server.state.requests), 5)
            for request in server.state.requests:
                if request["method"] != "POST":
                    continue
                payload = json.loads(request["body"].decode("utf-8"))
                self.assertEqual(
                    set(payload),
                    {"business_order_no", "amount", "currency"},
                )
                self.assertNotIn("expect", payload)
                self.assertNotIn("scenario", payload)
                self.assertNotIn("client_id", payload)

    def test_redirect_is_not_followed(self):
        with FakeServer(mode="redirect") as server:
            code, stdout, stderr = self.run_demo(server)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("error=redirect", stderr)
        self.assertEqual(len(server.state.requests), 1)

    def test_failure_categories_do_not_leak_raw_content(self):
        cases = [
            ("schema", "response-schema"),
            ("not-confirmed", "response-schema"),
            ("changed", "response-changed"),
            ("duplicate-request-id", "response-changed"),
            ("wrong-status", "http-status"),
            ("canary-error", "http-status"),
        ]
        for mode, expected in cases:
            with self.subTest(mode):
                with FakeServer(mode=mode) as server:
                    code, stdout, stderr = self.run_demo(server)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("error=" + expected, stderr)
                for forbidden in (
                    CANARY,
                    DEMO_KEY,
                    "http://127.0.0.1:",
                    "message",
                    "body",
                    "Authorization",
                ):
                    self.assertNotIn(forbidden, stderr)

    def test_expectation_mismatch_is_fixed_exit_one(self):
        with FakeServer(scenario="SUCCEEDED") as server:
            code, stdout, stderr = self.run_demo(server, expect="FAILED")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("error=expectation-mismatch", stderr)
        self.assertNotIn(CANARY, stderr)
        self.assertNotIn(DEMO_KEY, stderr)

    def test_timeout_is_bounded(self):
        with FakeServer(mode="timeout") as server:
            with mock.patch.object(demo, "HTTP_TIMEOUT_SECONDS", 0.2), mock.patch.object(
                demo, "TOTAL_BUDGET_SECONDS", 5
            ):
                started = time.monotonic()
                code, stdout, stderr = self.run_demo(server)
                elapsed = time.monotonic() - started
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("error=http-timeout", stderr)
        self.assertLess(elapsed, 3.0)

    def test_deadline_guard_restores_existing_timer_and_handler(self):
        old_handler = signal.getsignal(signal.SIGALRM)
        old_timer = signal.getitimer(signal.ITIMER_REAL)
        try:
            def previous(_signum, _frame):
                return

            signal.signal(signal.SIGALRM, previous)
            signal.setitimer(signal.ITIMER_REAL, 5.0)
            with demo._DeadlineGuard(0.05, "http-timeout"):
                self.assertIsNot(signal.getsignal(signal.SIGALRM), previous)
                remaining, _ = signal.getitimer(signal.ITIMER_REAL)
                self.assertGreater(remaining, 0.0)
                self.assertLessEqual(remaining, 0.05)
            self.assertIs(signal.getsignal(signal.SIGALRM), previous)
            remaining, _ = signal.getitimer(signal.ITIMER_REAL)
            self.assertGreater(remaining, 4.0)
            self.assertLessEqual(remaining, 5.0)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, old_handler)
            if old_timer[0] > 0:
                signal.setitimer(
                    signal.ITIMER_REAL, old_timer[0], old_timer[1]
                )

    def test_deadline_guard_does_not_overwrite_earlier_timer(self):
        old_handler = signal.getsignal(signal.SIGALRM)
        old_timer = signal.getitimer(signal.ITIMER_REAL)
        try:
            def previous(_signum, _frame):
                return

            signal.signal(signal.SIGALRM, previous)
            signal.setitimer(signal.ITIMER_REAL, 2.0)
            with demo._DeadlineGuard(5.0, "http-timeout"):
                self.assertIs(signal.getsignal(signal.SIGALRM), previous)
                remaining, _ = signal.getitimer(signal.ITIMER_REAL)
                self.assertGreater(remaining, 0.0)
                self.assertLessEqual(remaining, 2.0)
            self.assertIs(signal.getsignal(signal.SIGALRM), previous)
            remaining, _ = signal.getitimer(signal.ITIMER_REAL)
            self.assertLessEqual(remaining, 2.0)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, old_handler)
            if old_timer[0] > 0:
                signal.setitimer(
                    signal.ITIMER_REAL, old_timer[0], old_timer[1]
                )

    def test_deadline_unsupported_thread_fails_closed(self):
        with FakeServer() as server:
            result = {}

            def worker():
                stdout = io.StringIO()
                stderr = io.StringIO()
                code = demo.main(
                    [
                        "--base-url",
                        server.base_url,
                        "--business-order-no",
                        BASE_ORDER,
                        "--amount",
                        "100",
                        "--expect-result",
                        "SUCCEEDED",
                    ],
                    stdout=stdout,
                    stderr=stderr,
                )
                result["code"] = code
                result["stdout"] = stdout.getvalue()
                result["stderr"] = stderr.getvalue()

            with mock.patch.dict(
                os.environ, {"PAYMENT_HUB_API_KEY": DEMO_KEY}, clear=False
            ):
                thread = threading.Thread(target=worker)
                thread.start()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result["code"], 1)
            self.assertEqual(result["stdout"], "")
            self.assertIn("error=deadline-unsupported", result["stderr"])
            self.assertEqual(server.state.requests, [])

    def test_slow_headers_are_bounded_by_absolute_deadline(self):
        with FakeServer(mode="slow-headers") as server:
            with mock.patch.object(demo, "HTTP_TIMEOUT_SECONDS", 0.1), mock.patch.object(
                demo, "TOTAL_BUDGET_SECONDS", 0.3
            ):
                started = time.monotonic()
                code, stdout, stderr = self.run_demo(server)
                elapsed = time.monotonic() - started
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("error=http-timeout", stderr)
        self.assertLess(elapsed, 1.0)

    def test_slow_body_is_bounded_by_absolute_deadline(self):
        with FakeServer(mode="slow-body") as server:
            with mock.patch.object(demo, "HTTP_TIMEOUT_SECONDS", 0.1), mock.patch.object(
                demo, "TOTAL_BUDGET_SECONDS", 0.3
            ):
                started = time.monotonic()
                code, stdout, stderr = self.run_demo(server)
                elapsed = time.monotonic() - started
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("error=http-timeout", stderr)
        self.assertLess(elapsed, 1.0)

    def test_total_budget_crosses_steps(self):
        with FakeServer(mode="delay") as server:
            with mock.patch.object(demo, "HTTP_TIMEOUT_SECONDS", 0.2), mock.patch.object(
                demo, "TOTAL_BUDGET_SECONDS", 0.3
            ):
                started = time.monotonic()
                code, stdout, stderr = self.run_demo(server)
                elapsed = time.monotonic() - started
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("error=http-timeout", stderr)
        self.assertLess(elapsed, 1.0)

    def test_remaining_deadline_boundary(self):
        with mock.patch.object(demo.time, "monotonic", return_value=10.0):
            with self.assertRaises(demo.DemoError) as expired:
                demo._remaining(10.0)
            self.assertEqual(expired.exception.category, "http-timeout")
            self.assertAlmostEqual(demo._remaining(11.0), 1.0)
            with self.assertRaises(demo.DemoError) as final:
                demo._require_alive(9.0)
            self.assertEqual(final.exception.category, "http-timeout")

    def test_output_is_whitelist_only(self):
        with FakeServer() as server:
            code, stdout, stderr = self.run_demo(server)
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        self.assertEqual(set(report), {"schema", "steps", "trace_complete"})
        for step in report["steps"]:
            self.assertEqual(
                set(step),
                {
                    "step",
                    "status",
                    "request_id",
                    "payment_id",
                    "result",
                    "issue_codes",
                },
            )
        for forbidden in (
            DEMO_KEY,
            "http://",
            "Authorization",
            "message",
            "body",
            "business_order_no",
        ):
            self.assertNotIn(forbidden, stdout)

    def test_no_cleanup_or_new_business_number(self):
        with FakeServer() as server:
            code, _stdout, stderr = self.run_demo(server)
        self.assertEqual(code, 0, stderr)
        requests = list(server.state.requests)
        self.assertEqual(len(requests), 5)
        self.assertTrue(all(r["method"] in ("GET", "POST") for r in requests))
        for request in requests:
            parsed = urllib.parse.urlsplit(request["path"])
            self.assertTrue(parsed.path.startswith("/api/v1/payments"))
            self.assertNotIn("delete", parsed.path.lower())
        posts = [r for r in requests if r["method"] == "POST"]
        orders = {
            json.loads(r["body"].decode("utf-8"))["business_order_no"]
            for r in posts
        }
        self.assertEqual(orders, {BASE_ORDER})
        amounts = [
            json.loads(r["body"].decode("utf-8"))["amount"] for r in posts
        ]
        self.assertEqual(amounts[0], 100)
        self.assertEqual(amounts[1], 100)
        self.assertIn(amounts[2], (1, 2))


if __name__ == "__main__":
    unittest.main()
