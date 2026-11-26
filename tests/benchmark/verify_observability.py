#!/usr/bin/env python3
"""Verify the local observability stack exposes the required contract series.

Exit codes:
  0  all required series present and pg_up is 1
  2  arguments or HTTP transport error
  3  a required series is missing or exposes a forbidden label
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REQUIRED_QUERIES = (
    "pg_up",
    "pg_benchmark_waiting_locks",
    "pg_stat_database_xact_commit",
)
FORBIDDEN_LABELS = ("usename", "application_name")


def request_text(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def query(prometheus, expression, timeout):
    url = (
        prometheus.rstrip("/")
        + "/api/v1/query?query="
        + urllib.parse.quote(expression, safe="")
    )
    payload = json.loads(request_text(url, timeout))
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {expression}: {payload.get('error')}")
    return payload["data"]["result"]


def wait_for_series(prometheus, expression, timeout):
    deadline = time.monotonic() + timeout
    last = "no result"
    while time.monotonic() < deadline:
        try:
            result = query(prometheus, expression, max(1, timeout))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last = str(exc)
        else:
            if result:
                return result
            last = "empty result"
        time.sleep(0.5)
    raise RuntimeError(f"{expression} did not appear within {timeout:g}s ({last})")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus", default="http://127.0.0.1:9090")
    parser.add_argument("--exporter", default="http://127.0.0.1:9187")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)

    try:
        for expression in REQUIRED_QUERIES:
            result = wait_for_series(args.prometheus, expression, args.timeout)
            print(f"series: {expression} samples={len(result)}")
        pg_up = wait_for_series(args.prometheus, "pg_up", args.timeout)
        value = float(pg_up[0]["value"][1])
        if value != 1:
            print(f"ERROR: pg_up={value}, want 1")
            return 3
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot reach Prometheus at {args.prometheus}: {exc}")
        return 2
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 3

    try:
        body = request_text(args.exporter.rstrip("/") + "/metrics", args.timeout)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"ERROR: cannot reach exporter at {args.exporter}: {exc}")
        return 2
    leaked = [label for label in FORBIDDEN_LABELS if label in body]
    if leaked:
        print(f"ERROR: exporter metrics expose forbidden labels: {', '.join(leaked)}")
        return 3
    if 'pg_benchmark_waiting_locks' not in body:
        print("ERROR: exporter /metrics is missing pg_benchmark_waiting_locks")
        return 3
    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
