#!/usr/bin/env python3
"""Read-only GHCR package inventory Link diagnostic.

This diagnostic is deliberately narrow:

* it validates the trusted repository, branch, push event, and owner actors
  before reading the audit token;
* it uses the existing ``release.Release._audit_identity`` and
  ``release.Release._list_owner_packages`` read-only HTTP paths;
* it allows only ``GET /user`` and the fixed ``GET /user/packages`` inventory
  URL through its HTTP wrapper, so no package-metadata request is possible;
* it never prints package names/metadata, raw Link headers, URLs, query
  keys/values, cursors, tokens, exception causes, or tracebacks.

On an inventory Link rejection it prints a fixed taxonomy plus a sanitized
structural description.  The description contains only enums, booleans,
bounded counts, whitelisted relation names, and bounded page integers.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

SCRIPT_DIR = Path(__file__).resolve().parent
_RELEASE_SPEC = importlib.util.spec_from_file_location(
    "diagnose_release", SCRIPT_DIR / "release.py"
)
if _RELEASE_SPEC is None or _RELEASE_SPEC.loader is None:
    raise SystemExit("release.py is unavailable")
release = importlib.util.module_from_spec(_RELEASE_SPEC)
sys.modules[_RELEASE_SPEC.name] = release
_RELEASE_SPEC.loader.exec_module(release)

TRUSTED_REPOSITORY = "xtianxx/payment-hub"
TRUSTED_REF = "refs/heads/fix/ghcr-inventory-link"
TRUSTED_ACTOR = "xtianxx"
TRUSTED_EVENT = "push"

REPOSITORY_ENV = "DIAGNOSE_REPOSITORY"
REF_ENV = "DIAGNOSE_REF"
ACTOR_ENV = "DIAGNOSE_ACTOR"
TRIGGERING_ACTOR_ENV = "DIAGNOSE_TRIGGERING_ACTOR"
EVENT_ENV = "DIAGNOSE_EVENT_NAME"

_INVENTORY_PREFIX = (
    f"{release.GITHUB_API}/user/packages?package_type=container"
    f"&per_page={release.INVENTORY_PER_PAGE}&page="
)
_INVENTORY_URL_RE = re.compile(
    re.escape(_INVENTORY_PREFIX) + r"[1-9][0-9]{0,8}"
)

MAX_SHAPE_LINKS = 16
MAX_LINK_HEADER = 8192
MAX_BOUNDED_COUNT = 999
MAX_PAGE = 999_999_999

PREDEFINED_QUERY_KEYS = (
    "page",
    "per_page",
    "package_type",
    "after",
    "before",
    "visibility",
)
WHITELIST_RELATIONS = ("next", "last", "prev", "first")

_TOKEN_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_PAGE_VALUE_RE = re.compile(r"[1-9][0-9]{0,8}")
_PAGE_SHAPE_RE = re.compile(r"[0-9]{1,9}")


def _bounded_count(value: int) -> int:
    return min(value, MAX_BOUNDED_COUNT)


def _body_is_empty_list(body: bytes) -> bool:
    """Return only whether the body is a JSON empty list; never its content."""
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(value, list) and not value


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _emit(out: TextIO, key: str, value: Any) -> None:
    print(f"{key}={value}", file=out)


def _validate_context(env: Mapping[str, str]) -> None:
    if env.get(REPOSITORY_ENV) != TRUSTED_REPOSITORY:
        raise release.ReleaseError("context", "repository")
    if env.get(REF_ENV) != TRUSTED_REF:
        raise release.ReleaseError("context", "ref")
    if env.get(ACTOR_ENV) != TRUSTED_ACTOR:
        raise release.ReleaseError("context", "actor")
    if env.get(TRIGGERING_ACTOR_ENV) != TRUSTED_ACTOR:
        raise release.ReleaseError("context", "triggering-actor")
    if env.get(EVENT_ENV) != TRUSTED_EVENT:
        raise release.ReleaseError("context", "event")


def _path_kind(path: str) -> str:
    if path == "/user/packages":
        return "standard-user"
    if re.fullmatch(r"/user/[0-9]+/packages", path):
        return "user-numeric"
    if re.fullmatch(r"/users/[0-9]+/packages", path):
        return "users-numeric"
    if re.fullmatch(r"/users/[^/]+/packages", path):
        return "users-login"
    if re.fullmatch(r"/organizations/[0-9]+/packages", path):
        return "organizations-numeric"
    return "other"


def _value_matches(key: str, value: str) -> bool:
    if key == "package_type":
        return value == "container"
    if key == "per_page":
        return value == str(release.INVENTORY_PER_PAGE)
    if key == "page":
        return _PAGE_VALUE_RE.fullmatch(value) is not None
    return False


def _bounded_page(values: list[str]) -> int:
    if len(values) != 1:
        return -1
    value = values[0]
    if _PAGE_SHAPE_RE.fullmatch(value) is None:
        return -1
    number = int(value)
    if number > MAX_PAGE:
        return -1
    return number


def _scan_links(value: str) -> tuple[list[dict[str, Any]], bool, bool]:
    """Best-effort Link scan for shape only; never raises or prints input."""
    oversized = len(value) > MAX_LINK_HEADER
    text = value[:MAX_LINK_HEADER] if oversized else value
    syntax_ok = not oversized
    links: list[dict[str, Any]] = []
    length = len(text)
    pos = 0
    while pos < length:
        while pos < length and text[pos] in " \t":
            pos += 1
        if pos >= length:
            break
        if text[pos] != "<":
            syntax_ok = False
            break
        end = text.find(">", pos + 1)
        if end < 0:
            syntax_ok = False
            links.append(
                {
                    "uri": text[pos + 1 :],
                    "params": [],
                    "params_ok": False,
                    "duplicate_params": 0,
                }
            )
            break
        uri = text[pos + 1 : end]
        if not uri or any(
            ord(char) <= 0x20 or ord(char) == 0x7F or char in '<">'
            for char in uri
        ):
            syntax_ok = False
        pos = end + 1
        params: list[tuple[str, str | None]] = []
        params_ok = True
        duplicate_params = 0
        seen_params: set[str] = set()
        while True:
            while pos < length and text[pos] in " \t":
                pos += 1
            if pos >= length or text[pos] != ";":
                break
            pos += 1
            while pos < length and text[pos] in " \t":
                pos += 1
            match = _TOKEN_RE.match(text, pos)
            if match is None:
                params_ok = False
                syntax_ok = False
                break
            name = match.group(0).lower()
            pos = match.end()
            while pos < length and text[pos] in " \t":
                pos += 1
            param_value: str | None = None
            if pos < length and text[pos] == "=":
                pos += 1
                while pos < length and text[pos] in " \t":
                    pos += 1
                if pos < length and text[pos] == '"':
                    pos += 1
                    chars: list[str] = []
                    closed = False
                    while pos < length:
                        char = text[pos]
                        if char == "\\":
                            if pos + 1 >= length:
                                params_ok = False
                                syntax_ok = False
                                break
                            chars.append(text[pos + 1])
                            pos += 2
                            continue
                        if char == '"':
                            pos += 1
                            closed = True
                            break
                        chars.append(char)
                        pos += 1
                    if not closed:
                        params_ok = False
                        syntax_ok = False
                    param_value = "".join(chars)
                else:
                    match = _TOKEN_RE.match(text, pos)
                    if match is None:
                        params_ok = False
                        syntax_ok = False
                    else:
                        param_value = match.group(0)
                        pos = match.end()
            if name in seen_params:
                duplicate_params += 1
                syntax_ok = False
            else:
                seen_params.add(name)
            params.append((name, param_value))
        if not params_ok:
            syntax_ok = False
        links.append(
            {
                "uri": uri,
                "params": params,
                "params_ok": params_ok,
                "duplicate_params": duplicate_params,
            }
        )
        while pos < length and text[pos] in " \t":
            pos += 1
        if pos >= length:
            break
        if text[pos] != ",":
            syntax_ok = False
            break
        pos += 1
        while pos < length and text[pos] in " \t":
            pos += 1
        if pos >= length:
            syntax_ok = False
    return links, syntax_ok, oversized


def _link_shape(link: Mapping[str, Any]) -> dict[str, Any]:
    uri = link["uri"]
    parsed: urllib.parse.SplitResult | None = None
    port: int | None = None
    port_ok = False
    uri_parse_ok = False
    try:
        parsed = urllib.parse.urlsplit(uri)
        try:
            port = parsed.port
            port_ok = True
        except ValueError:
            port_ok = False
    except ValueError:
        parsed = None

    if parsed is None:
        scheme_https = False
        host_ok = False
        port_present = False
        userinfo_present = False
        fragment_present = False
        path_kind = "other"
        query_syntax_ok = False
        query_pairs: list[tuple[str, str]] = []
        query_counts: Counter[str] = Counter()
        page_values: list[str] = []
    else:
        try:
            scheme_https = parsed.scheme == "https"
            host_ok = parsed.hostname == "api.github.com"
            port_present = (port is not None) if port_ok else True
            userinfo_present = (
                parsed.username is not None or parsed.password is not None
            )
            fragment_present = bool(parsed.fragment)
            path_kind = _path_kind(parsed.path)
            uri_parse_ok = port_ok
        except ValueError:
            scheme_https = False
            host_ok = False
            port_present = False
            userinfo_present = False
            fragment_present = False
            path_kind = "other"
            uri_parse_ok = False
        try:
            urllib.parse.parse_qs(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
            )
            query_syntax_ok = True
        except ValueError:
            query_syntax_ok = False
        try:
            query_pairs = urllib.parse.parse_qsl(
                parsed.query, keep_blank_values=True
            )
        except ValueError:
            query_pairs = []
        query_counts = Counter(key for key, _ in query_pairs)
        page_values = [
            value for key, value in query_pairs if key == "page"
        ]

    query_shape: dict[str, dict[str, bool]] = {}
    for key in PREDEFINED_QUERY_KEYS:
        count = query_counts.get(key, 0)
        first_value = next(
            (value for pair_key, value in query_pairs if pair_key == key),
            "",
        )
        query_shape[key] = {
            "present": count > 0,
            "duplicate": count > 1,
            "value_match": count == 1 and _value_matches(key, first_value),
        }

    rel_values: list[str] = []
    for name, param_value in link["params"]:
        if name != "rel" or param_value is None:
            continue
        rel_values.extend(part.lower() for part in param_value.split())
    next_link_count = sum(1 for rel in rel_values if rel == "next")
    last_link_count = sum(1 for rel in rel_values if rel == "last")
    relations = {rel: rel in rel_values for rel in WHITELIST_RELATIONS}
    unknown_relation_count = sum(
        1 for rel in rel_values if rel not in WHITELIST_RELATIONS
    )

    return {
        "uri_parse_ok": uri_parse_ok,
        "scheme_https": scheme_https,
        "host_api_github": host_ok,
        "port_present": port_present,
        "userinfo_present": userinfo_present,
        "fragment_present": fragment_present,
        "path_kind": path_kind,
        "params_ok": link["params_ok"],
        "duplicate_param_count": _bounded_count(
            link["duplicate_params"]
        ),
        "query_syntax_ok": query_syntax_ok,
        "query_pair_count": _bounded_count(len(query_pairs)),
        "query_unknown_key_count": _bounded_count(
            len(
                {
                    key
                    for key in query_counts
                    if key not in PREDEFINED_QUERY_KEYS
                }
            )
        ),
        "query": query_shape,
        "next_link_count": _bounded_count(next_link_count),
        "last_link_count": _bounded_count(last_link_count),
        "next_page": _bounded_page(page_values) if next_link_count else -1,
        "last_page": _bounded_page(page_values) if last_link_count else -1,
        "relations": relations,
        "rel_count": _bounded_count(len(rel_values)),
        "unknown_relation_count": _bounded_count(unknown_relation_count),
    }


def _relation_totals(
    links: list[dict[str, Any]],
) -> tuple[dict[str, int], int]:
    counts: Counter[str] = Counter()
    unknown = 0
    for link in links:
        for name, param_value in link["params"]:
            if name != "rel" or param_value is None:
                continue
            for part in param_value.split():
                rel = part.lower()
                counts[rel] += 1
                if rel not in WHITELIST_RELATIONS:
                    unknown += 1
    return (
        {
            rel: _bounded_count(counts.get(rel, 0))
            for rel in WHITELIST_RELATIONS
        },
        _bounded_count(unknown),
    )


def _shape_from_link_header(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "present": False,
            "oversized": False,
            "syntax_ok": True,
            "link_count": 0,
            "links": [],
            "truncated": False,
            "relation_totals": {
                rel: 0 for rel in WHITELIST_RELATIONS
            },
            "unknown_relation_total": 0,
        }
    if not isinstance(value, str):
        return {
            "present": True,
            "oversized": False,
            "syntax_ok": False,
            "link_count": 0,
            "links": [],
            "truncated": False,
            "relation_totals": {
                rel: 0 for rel in WHITELIST_RELATIONS
            },
            "unknown_relation_total": 0,
        }
    links, syntax_ok, oversized = _scan_links(value)
    if not value or not links:
        syntax_ok = False
    relation_totals, unknown_relation_total = _relation_totals(links)
    return {
        "present": True,
        "oversized": oversized,
        "syntax_ok": syntax_ok,
        "link_count": _bounded_count(len(links)),
        "links": [_link_shape(link) for link in links[:MAX_SHAPE_LINKS]],
        "truncated": len(links) > MAX_SHAPE_LINKS,
        "relation_totals": relation_totals,
        "unknown_relation_total": unknown_relation_total,
    }


def _format_uri(link: Mapping[str, Any]) -> str:
    return (
        f"parse_ok:{_bool(link['uri_parse_ok'])},"
        f"scheme_https:{_bool(link['scheme_https'])},"
        f"host_api_github:{_bool(link['host_api_github'])},"
        f"port:{_bool(link['port_present'])},"
        f"userinfo:{_bool(link['userinfo_present'])},"
        f"fragment:{_bool(link['fragment_present'])},"
        f"path_kind:{link['path_kind']},"
        f"params_ok:{_bool(link['params_ok'])},"
        f"duplicate_params:{link['duplicate_param_count']}"
    )


def _format_query(link: Mapping[str, Any]) -> str:
    return (
        f"syntax:{_bool(link['query_syntax_ok'])},"
        f"pairs:{link['query_pair_count']},"
        f"unknown_keys:{link['query_unknown_key_count']}"
    )


def _format_query_key(link: Mapping[str, Any], key: str) -> str:
    value = link["query"][key]
    return (
        f"present:{_bool(value['present'])},"
        f"value_match:{_bool(value['value_match'])}"
    )


def _format_nav(link: Mapping[str, Any]) -> str:
    rels = [
        rel for rel in WHITELIST_RELATIONS if link["relations"][rel]
    ]
    return (
        f"next_links:{link['next_link_count']},"
        f"last_links:{link['last_link_count']},"
        f"next_page:{link['next_page']},"
        f"last_page:{link['last_page']},"
        f"rel_count:{link['rel_count']},"
        f"rels:{','.join(rels) if rels else 'none'},"
        f"unknown_rels:{link['unknown_relation_count']}"
    )


def _format_relation_totals(shape: Mapping[str, Any]) -> str:
    totals = shape["relation_totals"]
    return (
        f"next:{totals['next']},last:{totals['last']},"
        f"prev:{totals['prev']},first:{totals['first']},"
        f"unknown:{shape['unknown_relation_total']}"
    )


def _emit_inventory_empty(
    out: TextIO, page_shapes: list[tuple[int, dict[str, Any]]]
) -> None:
    if page_shapes:
        _emit(
            out,
            "inventory_empty",
            _bool(page_shapes[0][1].get("empty", False)),
        )


def _emit_page_empty(
    out: TextIO, page_shapes: list[tuple[int, dict[str, Any]]]
) -> None:
    for page, shape in page_shapes:
        _emit(
            out,
            f"link_shape_page_{page}_empty",
            _bool(shape.get("empty", False)),
        )


def _emit_shape(
    out: TextIO, page_shapes: list[tuple[int, dict[str, Any]]]
) -> None:
    _emit(out, "link_shape_page_count", len(page_shapes))
    for page, shape in page_shapes:
        prefix = f"link_shape_page_{page}_"
        _emit(
            out,
            prefix + "header",
            (
                f"present:{_bool(shape['present'])},"
                f"oversized:{_bool(shape['oversized'])},"
                f"syntax_ok:{_bool(shape['syntax_ok'])}"
            ),
        )
        _emit(
            out,
            prefix + "empty",
            _bool(shape.get("empty", False)),
        )
        _emit(out, prefix + "link_count", shape["link_count"])
        _emit(out, prefix + "relations", _format_relation_totals(shape))
        if shape["truncated"]:
            _emit(out, prefix + "truncated", "true")
        for index, link in enumerate(shape["links"], 1):
            link_prefix = prefix + f"link_{index}_"
            _emit(out, link_prefix + "uri", _format_uri(link))
            _emit(out, link_prefix + "query", _format_query(link))
            for key in PREDEFINED_QUERY_KEYS:
                _emit(
                    out,
                    link_prefix + f"query_{key}",
                    _format_query_key(link, key),
                )
            _emit(out, link_prefix + "nav", _format_nav(link))


class DiagnosticHTTP:
    """Allow only the two fixed read-only GET endpoints and shape Links."""

    def __init__(
        self, delegate: Callable[..., release.HttpResult]
    ) -> None:
        self.delegate = delegate
        self.identity_calls = 0
        self.inventory_calls = 0
        self.page_shapes: list[tuple[int, dict[str, Any]]] = []

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
    ) -> release.HttpResult:
        if method != "GET":
            raise release.ReleaseError("diagnose", "method")
        if url == release.GITHUB_API + "/user":
            self.identity_calls += 1
            inventory = False
        elif _INVENTORY_URL_RE.fullmatch(url):
            self.inventory_calls += 1
            inventory = True
        else:
            raise release.ReleaseError("diagnose", "endpoint")
        response = self.delegate(
            method,
            url,
            headers=headers,
            timeout=timeout,
            max_bytes=max_bytes,
        )
        if inventory:
            link = release._header_value(response.headers, "link")
            shape = _shape_from_link_header(link)
            shape["empty"] = _body_is_empty_list(response.body)
            self.page_shapes.append((self.inventory_calls, shape))
        return response


def main(
    env: Mapping[str, str] | None = None,
    http: Callable[..., release.HttpResult] | None = None,
    out: TextIO | None = None,
) -> int:
    source_env = dict(os.environ) if env is None else dict(env)
    output = sys.stdout if out is None else out
    token = source_env.pop(release.AUDIT_TOKEN_ENV, "")
    page_shapes: list[tuple[int, dict[str, Any]]] = []
    try:
        _validate_context(source_env)
        if not token:
            raise release.ReleaseError("context", "missing-token")
        wrapper = DiagnosticHTTP(
            release._default_http if http is None else http
        )
        page_shapes = wrapper.page_shapes
        instance = release.Release(
            repo=release.REPO_ROOT,
            http=wrapper,
            env=source_env,
        )
        identity = instance._audit_identity(token)
        _emit(output, "identity_result", "accepted")
        _emit(output, "identity_scope", identity["scope"])
        instance._list_owner_packages(token)
        _emit(output, "inventory_result", "accepted")
        _emit(output, "inventory_pages", wrapper.inventory_calls)
        _emit_inventory_empty(output, page_shapes)
        _emit_shape(output, page_shapes)
        return 0
    except release.ReleaseError as exc:
        _emit(output, "inventory_result", "rejected")
        _emit(output, "failure_stage", exc.stage)
        _emit(output, "failure_kind", exc.kind)
        _emit_inventory_empty(output, page_shapes)
        if exc.stage == "package" and (
            exc.kind == "inventory-link"
            or exc.kind.startswith("inventory-link-")
        ):
            _emit_shape(output, page_shapes)
        else:
            _emit_page_empty(output, page_shapes)
        return 1
    except Exception:
        _emit(output, "inventory_result", "internal-error")
        _emit(output, "failure_stage", "internal")
        _emit(output, "failure_kind", "internal-error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
