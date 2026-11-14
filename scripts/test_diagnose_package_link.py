"""Focused tests for the read-only package inventory Link diagnostic.

These tests use only fake HTTP responses.  They exercise the real
``release._list_owner_packages`` pagination path, the fixed context/token
checks, the HTTP endpoint allowlist, and the sanitized Link shape output.
No Docker, registry, GitHub API, or external network is used.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "diagnose-package-link.py"
SPEC = importlib.util.spec_from_file_location(
    "diagnose_package_link", SCRIPT
)
diagnose = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = diagnose
SPEC.loader.exec_module(diagnose)
release = diagnose.release

TOKEN = "SENTINEL_AUDIT_TOKEN_7c1d"
SENTINEL = "SENTINEL_LEAK_CANARY_9f3a"
INVENTORY_BASE = (
    "https://api.github.com/user/packages?"
    "package_type=container&per_page=100"
)


def package_metadata(
    role: str,
    *,
    package_id: int | None = None,
    name: str | None = None,
) -> dict[str, object]:
    return {
        "id": (
            package_id
            if package_id is not None
            else (101 if role == "app" else 102)
        ),
        "name": release.PACKAGE_NAMES[role] if name is None else name,
        "package_type": "container",
        "visibility": "private",
        "owner": {"login": "xtianxx"},
        "repository": {"full_name": release.REPOSITORY},
    }


def foreign_package(
    index: int, *, name: str | None = None
) -> dict[str, object]:
    return {
        "id": 100000 + index,
        "name": (
            f"unrelated-package-{index}" if name is None else name
        ),
        "package_type": "container",
        "visibility": "private",
        "owner": {"login": "xtianxx"},
        "repository": {"full_name": release.REPOSITORY},
    }


class FakeHTTP:
    def __init__(
        self,
        *,
        identity_login: str = "xtianxx",
        identity_scope: str | None = "read:packages",
        pages: dict[int, list[dict[str, object]]] | None = None,
        links: dict[int, str | None] | None = None,
        statuses: dict[int, int] | None = None,
        body_overrides: dict[int, bytes] | None = None,
        exception: Exception | None = None,
    ) -> None:
        self.identity_login = identity_login
        self.identity_scope = identity_scope
        self.pages = (
            pages
            if pages is not None
            else {
                1: [
                    package_metadata("app"),
                    package_metadata("migrate"),
                ]
            }
        )
        self.links = links or {}
        self.statuses = statuses or {}
        self.body_overrides = body_overrides or {}
        self.exception = exception
        self.calls: list[dict[str, object]] = []
        self.identity_calls = 0
        self.inventory_calls = 0

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        max_bytes: int,
    ) -> release.HttpResult:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "timeout": timeout,
                "max_bytes": max_bytes,
            }
        )
        if self.exception is not None:
            raise self.exception
        if url == release.GITHUB_API + "/user":
            self.identity_calls += 1
            response_headers: dict[str, str] = {}
            if self.identity_scope is not None:
                response_headers["x-oauth-scopes"] = self.identity_scope
            return release.HttpResult(
                200,
                response_headers,
                json.dumps({"login": self.identity_login}).encode(),
            )
        if url.startswith(release.GITHUB_API + "/user/packages?"):
            self.inventory_calls += 1
            page = int(
                urllib.parse.parse_qs(
                    urllib.parse.urlsplit(url).query
                )["page"][0]
            )
            if page in self.statuses:
                return release.HttpResult(
                    self.statuses[page], {}, b"{}"
                )
            body = self.body_overrides.get(page)
            if body is None:
                body = json.dumps(self.pages.get(page, [])).encode()
            response_headers = {}
            link = self.links.get(page)
            if link is not None:
                response_headers["link"] = link
            return release.HttpResult(200, response_headers, body)
        raise AssertionError("unexpected URL")


def context_env(**overrides: str) -> dict[str, str]:
    env = {
        diagnose.REPOSITORY_ENV: diagnose.TRUSTED_REPOSITORY,
        diagnose.REF_ENV: diagnose.TRUSTED_REF,
        diagnose.ACTOR_ENV: diagnose.TRUSTED_ACTOR,
        diagnose.TRIGGERING_ACTOR_ENV: diagnose.TRUSTED_ACTOR,
        diagnose.EVENT_ENV: diagnose.TRUSTED_EVENT,
        release.AUDIT_TOKEN_ENV: TOKEN,
    }
    env.update(overrides)
    return env


def run_main(
    *,
    env: dict[str, str] | None = None,
    http: FakeHTTP | None = None,
) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with mock.patch("sys.stderr", err):
        code = diagnose.main(
            env=context_env() if env is None else env,
            http=FakeHTTP() if http is None else http,
            out=out,
        )
    return code, out.getvalue(), err.getvalue()


def shape_text(header: object) -> str:
    shape = diagnose._shape_from_link_header(header)
    out = io.StringIO()
    diagnose._emit_shape(out, [(1, shape)])
    return out.getvalue()


class ShapeTests(unittest.TestCase):
    def test_path_kind_enum_and_no_raw_path(self):
        base = "https://api.github.com"
        query = "?package_type=container&per_page=100&page=2"
        cases = [
            (
                "standard-user",
                f"{base}/user/packages{query}",
                "standard-user",
            ),
            (
                "user-numeric",
                f"{base}/user/123/packages{query}",
                "user-numeric",
            ),
            (
                "users-numeric",
                f"{base}/users/123/packages{query}",
                "users-numeric",
            ),
            (
                "users-login",
                f"{base}/users/{SENTINEL}/packages{query}",
                "users-login",
            ),
            (
                "organizations-numeric",
                f"{base}/organizations/123/packages{query}",
                "organizations-numeric",
            ),
            (
                "other",
                f"{base}/orgs/{SENTINEL}/packages{query}",
                "other",
            ),
        ]
        for label, uri, expected in cases:
            with self.subTest(label=label):
                text = shape_text(f'<{uri}>; rel="next"')
                self.assertIn(f"path_kind:{expected}", text)
                self.assertNotIn(SENTINEL, text)
                self.assertNotIn("123", text)

    def test_query_missing_extra_duplicate_and_value_match(self):
        cases = [
            (
                "missing-page",
                INVENTORY_BASE,
                "query_page=present:false,value_match:false",
                "unknown_keys:0",
            ),
            (
                "extra-key",
                f"{INVENTORY_BASE}&page=2&{SENTINEL}=1",
                "query_page=present:true,value_match:true",
                "unknown_keys:1",
            ),
            (
                "duplicate-page",
                f"{INVENTORY_BASE}&page=2&page=3",
                "query_page=present:true,value_match:false",
                "unknown_keys:0",
            ),
            (
                "wrong-per-page",
                "https://api.github.com/user/packages?"
                "package_type=container&per_page=50&page=2",
                "query_per_page=present:true,value_match:false",
                "unknown_keys:0",
            ),
            (
                "wrong-package-type",
                "https://api.github.com/user/packages?"
                "package_type=npm&per_page=100&page=2",
                "query_package_type=present:true,value_match:false",
                "unknown_keys:0",
            ),
        ]
        for label, uri, expected, unknown in cases:
            with self.subTest(label=label):
                text = shape_text(f'<{uri}>; rel="next"')
                self.assertIn(expected, text)
                self.assertIn(unknown, text)
                self.assertNotIn(SENTINEL, text)

    def test_query_syntax_and_page1last0(self):
        syntax = shape_text(
            f'<{INVENTORY_BASE}&page=2&broken>; rel="next"'
        )
        self.assertIn("query=syntax:false", syntax)
        self.assertIn("unknown_keys:1", syntax)
        self.assertNotIn("broken", syntax)

        page_zero = shape_text(
            f'<{INVENTORY_BASE}&page=0>; rel="last"'
        )
        self.assertIn(
            "query_page=present:true,value_match:false",
            page_zero,
        )
        self.assertIn("last_links:1", page_zero)
        self.assertIn("last_page:0", page_zero)
        self.assertIn("next_page:-1", page_zero)

    def test_relation_whitelist_duplicate_and_unknown_no_leak(self):
        cases = [
            (
                "next-last",
                f'<{INVENTORY_BASE}&page=2>; rel="next", '
                f'<{INVENTORY_BASE}&page=3>; rel="last"',
                "relations=next:1,last:1,prev:0,first:0,unknown:0",
                ("rels:next", "rels:last"),
                "unknown_rels:0",
            ),
            (
                "unknown",
                f'<{INVENTORY_BASE}&page=2>; rel="{SENTINEL}"',
                "relations=next:0,last:0,prev:0,first:0,unknown:1",
                ("rels:none",),
                "unknown_rels:1",
            ),
            (
                "multi-rel",
                f'<{INVENTORY_BASE}&page=2>; rel="next last"',
                "relations=next:1,last:1,prev:0,first:0,unknown:0",
                ("rels:next,last",),
                "unknown_rels:0",
            ),
            (
                "missing-rel",
                f"<{INVENTORY_BASE}&page=2>",
                "relations=next:0,last:0,prev:0,first:0,unknown:0",
                ("rel_count:0",),
                "unknown_rels:0",
            ),
            (
                "duplicate-next",
                f'<{INVENTORY_BASE}&page=2>; rel="next", '
                f'<{INVENTORY_BASE}&page=3>; rel="next"',
                "relations=next:2,last:0,prev:0,first:0,unknown:0",
                ("rels:next",),
                "unknown_rels:0",
            ),
        ]
        for label, header, totals, rels, unknown in cases:
            with self.subTest(label=label):
                text = shape_text(header)
                self.assertIn(totals, text)
                for expected in rels:
                    self.assertIn(expected, text)
                self.assertIn(unknown, text)
                self.assertNotIn(SENTINEL, text)

    def test_header_syntax_and_duplicate_params(self):
        cases = [
            (
                "none",
                None,
                "present:false,oversized:false,syntax_ok:true",
            ),
            (
                "empty",
                "",
                "present:true,oversized:false,syntax_ok:false",
            ),
            (
                "garbage",
                "garbage",
                "present:true,oversized:false,syntax_ok:false",
            ),
            (
                "oversized",
                "x" * 8193,
                "present:true,oversized:true,syntax_ok:false",
            ),
            (
                "trailing-comma",
                f'<{INVENTORY_BASE}&page=2>; rel="next",',
                "present:true,oversized:false,syntax_ok:false",
            ),
        ]
        for label, header, expected in cases:
            with self.subTest(label=label):
                text = shape_text(header)
                self.assertIn(expected, text)

        duplicate = shape_text(
            f'<{INVENTORY_BASE}&page=2>; '
            'rel="next"; rel="last"'
        )
        self.assertIn("syntax_ok:false", duplicate)
        self.assertIn("duplicate_params:1", duplicate)

    def test_shape_never_emits_raw_uri_query_or_relation(self):
        header = (
            f'<https://api.github.com/users/{SENTINEL}/packages?'
            f"package_type=container&per_page=100&page=2&{SENTINEL}=1>; "
            f'rel="{SENTINEL}"'
        )
        text = shape_text(header)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn("users/", text)
        self.assertNotIn("rel=", text)


class MainTests(unittest.TestCase):
    def test_accepted_single_page(self):
        fake = FakeHTTP()
        code, out, err = run_main(http=fake)
        self.assertEqual(code, 0)
        self.assertIn("identity_result=accepted", out)
        self.assertIn("identity_scope=read:packages", out)
        self.assertIn("inventory_result=accepted", out)
        self.assertIn("inventory_pages=1", out)
        self.assertEqual(fake.identity_calls, 1)
        self.assertEqual(fake.inventory_calls, 1)
        for call in fake.calls:
            self.assertEqual(call["method"], "GET")
            self.assertTrue(
                call["url"] == release.GITHUB_API + "/user"
                or call["url"].startswith(
                    release.GITHUB_API + "/user/packages?"
                )
            )
            self.assertEqual(
                call["headers"]["Authorization"], "Bearer " + TOKEN
            )
        for value in (out, err):
            self.assertNotIn(TOKEN, value)
            self.assertNotIn("payment-hub-app", value)
            self.assertNotIn("payment-hub-migrate", value)

    def test_accepted_multi_page(self):
        page_one = [
            foreign_package(index) for index in range(99)
        ]
        page_one.append(package_metadata("app"))
        page_two = [package_metadata("migrate")]
        links = {
            1: (
                f'<{INVENTORY_BASE}&page=2>; rel="next", '
                f'<{INVENTORY_BASE}&page=2>; rel="last", '
                f'<{INVENTORY_BASE}&page=1>; rel="first"'
            ),
            2: (
                f'<{INVENTORY_BASE}&page=1>; rel="prev", '
                f'<{INVENTORY_BASE}&page=1>; rel="first", '
                f'<{INVENTORY_BASE}&page=2>; rel="last"'
            ),
        }
        fake = FakeHTTP(
            pages={1: page_one, 2: page_two}, links=links
        )
        code, out, err = run_main(http=fake)
        self.assertEqual(code, 0)
        self.assertIn("inventory_result=accepted", out)
        self.assertIn("inventory_pages=2", out)
        self.assertEqual(fake.identity_calls, 1)
        self.assertEqual(fake.inventory_calls, 2)
        for value in (out, err):
            self.assertNotIn(TOKEN, value)
            self.assertNotIn("payment-hub-app", value)
            self.assertNotIn("payment-hub-migrate", value)

    def test_link_failure_shape_and_no_leak(self):
        fake = FakeHTTP(
            pages={
                1: [
                    package_metadata("app"),
                    foreign_package(0, name=SENTINEL),
                ]
            },
            links={
                1: (
                    f'<{INVENTORY_BASE}&page=2&{SENTINEL}=1>; '
                    'rel="next"'
                )
            },
        )
        code, out, err = run_main(http=fake)
        self.assertEqual(code, 1)
        self.assertIn("inventory_result=rejected", out)
        self.assertIn("failure_stage=package", out)
        self.assertIn("failure_kind=inventory-link-query", out)
        self.assertIn("link_shape_page_count=1", out)
        self.assertIn(
            "link_shape_page_1_link_count=1", out
        )
        self.assertIn("unknown_keys:1", out)
        self.assertIn(
            "query_page=present:true,value_match:true",
            out,
        )
        self.assertIn("next_page:2", out)
        self.assertEqual(fake.identity_calls, 1)
        self.assertEqual(fake.inventory_calls, 1)
        for value in (out, err):
            self.assertNotIn(SENTINEL, value)
            self.assertNotIn(TOKEN, value)
            self.assertNotIn("payment-hub-app", value)
            self.assertNotIn("unrelated-package", value)
            self.assertNotIn(INVENTORY_BASE, value)
            self.assertNotIn("Traceback", value)
            self.assertNotIn("ValueError", value)

    def test_link_failure_does_not_echo_chained_exception(self):
        fake = FakeHTTP(
            pages={1: [package_metadata("app")]},
            links={
                1: (
                    f'<{INVENTORY_BASE}&page=2&broken>; '
                    'rel="next"'
                )
            },
        )
        code, out, err = run_main(http=fake)
        self.assertEqual(code, 1)
        self.assertIn("failure_kind=inventory-link-query", out)
        self.assertIn("query=syntax:false", out)
        for value in (out, err):
            self.assertNotIn("broken", value)
            self.assertNotIn("ValueError", value)
            self.assertNotIn("Traceback", value)

    def test_inventory_forbidden_is_fixed_and_sanitized(self):
        fake = FakeHTTP(statuses={1: 403})
        code, out, err = run_main(http=fake)
        self.assertEqual(code, 1)
        self.assertIn("failure_stage=package", out)
        self.assertIn("failure_kind=inventory-forbidden", out)
        self.assertNotIn("link_shape", out)
        for value in (out, err):
            self.assertNotIn(TOKEN, value)
            self.assertNotIn("payment-hub-app", value)
            self.assertNotIn("payment-hub-migrate", value)

    def test_context_failures_do_not_touch_network(self):
        cases = [
            (
                "repository",
                {diagnose.REPOSITORY_ENV: "other/repo"},
            ),
            ("ref", {diagnose.REF_ENV: "refs/heads/main"}),
            ("actor", {diagnose.ACTOR_ENV: "other"}),
            (
                "triggering-actor",
                {diagnose.TRIGGERING_ACTOR_ENV: "other"},
            ),
            ("event", {diagnose.EVENT_ENV: "workflow_dispatch"}),
        ]
        for kind, override in cases:
            with self.subTest(kind=kind):
                fake = FakeHTTP()
                code, out, err = run_main(
                    env=context_env(**override), http=fake
                )
                self.assertEqual(code, 1)
                self.assertIn("inventory_result=rejected", out)
                self.assertIn("failure_stage=context", out)
                self.assertIn(f"failure_kind={kind}", out)
                self.assertEqual(fake.calls, [])
                for value in (out, err):
                    self.assertNotIn(TOKEN, value)

        env = context_env()
        env.pop(release.AUDIT_TOKEN_ENV)
        fake = FakeHTTP()
        code, out, err = run_main(env=env, http=fake)
        self.assertEqual(code, 1)
        self.assertIn("failure_kind=missing-token", out)
        self.assertEqual(fake.calls, [])
        for value in (out, err):
            self.assertNotIn(TOKEN, value)

    def test_identity_owner_mismatch_uses_only_identity(self):
        fake = FakeHTTP(identity_login="other")
        code, out, err = run_main(http=fake)
        self.assertEqual(code, 1)
        self.assertIn("failure_stage=audit", out)
        self.assertIn("failure_kind=owner", out)
        self.assertEqual(fake.identity_calls, 1)
        self.assertEqual(fake.inventory_calls, 0)
        for value in (out, err):
            self.assertNotIn(TOKEN, value)
            self.assertNotIn("payment-hub-app", value)

    def test_unexpected_exception_is_fixed_internal_error(self):
        fake = FakeHTTP(
            exception=RuntimeError("SENTINEL_RUNTIME")
        )
        code, out, err = run_main(http=fake)
        self.assertEqual(code, 1)
        self.assertIn("inventory_result=internal-error", out)
        self.assertIn("failure_stage=internal", out)
        self.assertIn("failure_kind=internal-error", out)
        for value in (out, err):
            self.assertNotIn("SENTINEL_RUNTIME", value)
            self.assertNotIn("Traceback", value)
            self.assertNotIn("RuntimeError", value)

    def test_http_wrapper_rejects_method_and_unknown_endpoints(self):
        class Delegate:
            def __init__(self) -> None:
                self.calls = 0

            def __call__(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError("must not delegate")

        delegate = Delegate()
        wrapper = diagnose.DiagnosticHTTP(delegate)
        cases = [
            ("POST", release.GITHUB_API + "/user", "method"),
            (
                "GET",
                release.GITHUB_API
                + "/user/packages/container/payment-hub-app",
                "endpoint",
            ),
            (
                "GET",
                release.GITHUB_API
                + "/user/packages?package_type=container"
                + f"&per_page={release.INVENTORY_PER_PAGE}"
                + "&page=1&extra=1",
                "endpoint",
            ),
            ("GET", "https://evil.example/user", "endpoint"),
        ]
        for method, url, kind in cases:
            with self.subTest(url=url):
                with self.assertRaises(release.ReleaseError) as caught:
                    wrapper(
                        method,
                        url,
                        headers={},
                        timeout=1.0,
                        max_bytes=1,
                    )
                self.assertEqual(
                    (caught.exception.stage, caught.exception.kind),
                    ("diagnose", kind),
                )
        self.assertEqual(delegate.calls, 0)

    def test_http_wrapper_allows_only_fixed_get_endpoints(self):
        class Delegate:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def __call__(
                self, method, url, *, headers, timeout, max_bytes
            ):
                self.urls.append(url)
                return release.HttpResult(
                    200,
                    {
                        "link": (
                            f'<{INVENTORY_BASE}&page=2>; '
                            'rel="next"'
                        )
                    },
                    b"[]",
                )

        delegate = Delegate()
        wrapper = diagnose.DiagnosticHTTP(delegate)
        identity = wrapper(
            "GET",
            release.GITHUB_API + "/user",
            headers={},
            timeout=1.0,
            max_bytes=1,
        )
        self.assertEqual(identity.status, 200)
        inventory_url = (
            release.GITHUB_API
            + "/user/packages?package_type=container"
            + f"&per_page={release.INVENTORY_PER_PAGE}&page=1"
        )
        inventory = wrapper(
            "GET",
            inventory_url,
            headers={},
            timeout=1.0,
            max_bytes=1,
        )
        self.assertEqual(inventory.status, 200)
        self.assertEqual(delegate.urls, [
            release.GITHUB_API + "/user",
            inventory_url,
        ])
        self.assertEqual(wrapper.identity_calls, 1)
        self.assertEqual(wrapper.inventory_calls, 1)
        self.assertEqual(len(wrapper.page_shapes), 1)
        self.assertEqual(wrapper.page_shapes[0][0], 1)
        self.assertIn(
            "next:1,last:0,prev:0,first:0,unknown:0",
            shape_text(
                f'<{INVENTORY_BASE}&page=2>; rel="next"'
            ),
        )


class WorkflowContractTests(unittest.TestCase):
    def test_workflow_is_push_only_read_only_and_sanitized(self):
        workflow = (
            REPO / ".github/workflows/diagnose-package-link.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("workflow_dispatch", workflow)
        self.assertIn("push:", workflow)
        self.assertIn("fix/ghcr-inventory-link", workflow)
        self.assertIn("paths:", workflow)
        self.assertIn(
            "scripts/diagnose-package-link.py", workflow
        )
        self.assertIn(
            "scripts/test_diagnose_package_link.py", workflow
        )
        self.assertIn("permissions:", workflow)
        self.assertIn("contents: read", workflow)
        self.assertNotIn("packages: write", workflow)
        self.assertIn("timeout-minutes: 3", workflow)
        self.assertIn(
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            workflow,
        )
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("GHCR_PACKAGE_AUDIT_TOKEN", workflow)
        self.assertNotIn("GITHUB_TOKEN", workflow)
        self.assertNotIn("actions/upload-artifact", workflow)
        self.assertNotIn("docker", workflow.lower())
        self.assertNotIn("release.sh", workflow)
        self.assertNotIn("environment:", workflow)
        self.assertNotIn("set -x", workflow)
        self.assertIn(
            "github.repository == 'xtianxx/payment-hub'",
            workflow,
        )
        self.assertIn("github.actor == 'xtianxx'", workflow)
        self.assertIn(
            "github.triggering_actor == 'xtianxx'", workflow
        )
        self.assertEqual(
            workflow.count(
                "python3 -B scripts/diagnose-package-link.py"
            ),
            1,
        )
        self.assertEqual(
            workflow.count(
                "${{ secrets.GHCR_PACKAGE_AUDIT_TOKEN }}"
            ),
            1,
        )
        self.assertEqual(
            workflow.count("GHCR_PACKAGE_AUDIT_TOKEN:"), 1
        )


if __name__ == "__main__":
    unittest.main()
