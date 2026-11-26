#!/usr/bin/env python3
"""Restart the benchmark PostgreSQL container and prove up/pg_up separation.

Precondition: Prometheus and postgres-exporter are running, up{job="postgres"}
is 1 and pg_up is 1. The script restarts the given container and requires:
  - up{job="postgres"} stays 1 throughout (exporter was not restarted),
  - pg_up becomes 0 at least once while the database is down,
  - pg_up returns to 1 after the database is ready.

Exit codes: 0 observed and recovered, 2 usage/transport error, 3 contract failure.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def scalar(prometheus, expression, timeout):
    url = (
        prometheus.rstrip("/")
        + "/api/v1/query?query="
        + urllib.parse.quote(expression, safe="")
    )
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("status") != "success":
        raise RuntimeError(payload.get("error") or "Prometheus query failed")
    result = payload["data"]["result"]
    if not result:
        return None
    return float(result[0]["value"][1])


def wait_value(prometheus, expression, wanted, timeout, description):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = scalar(prometheus, expression, max(1.0, timeout))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last = f"error: {exc}"
        else:
            if last == wanted:
                return
        time.sleep(0.5)
    raise RuntimeError(f"{description}: last value {last!r}, want {wanted}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus", default="http://127.0.0.1:9090")
    parser.add_argument("--container", required=True, help="benchmark PostgreSQL container name")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    try:
        names = subprocess.run(
            ["docker", "inspect", "--format", "{{.Name}}", args.container],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if names.returncode != 0:
            print(f"ERROR: container not found: {args.container}")
            return 2
        wait_value(args.prometheus, 'up{job="postgres"}', 1, args.timeout, "exporter up=1")
        wait_value(args.prometheus, "pg_up", 1, args.timeout, "pg_up=1 before restart")
    except (subprocess.TimeoutExpired, urllib.error.URLError, TimeoutError) as exc:
        print(f"ERROR: precondition failure: {exc}")
        return 2
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 3

    restart = subprocess.run(
        ["docker", "restart", args.container],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if restart.returncode != 0:
        print(f"ERROR: docker restart failed: {restart.stderr.strip()}")
        return 2

    saw_down = False
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            up = scalar(args.prometheus, 'up{job="postgres"}', 5)
            pg_up = scalar(args.prometheus, "pg_up", 5)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError):
            time.sleep(0.5)
            continue
        if up != 1:
            print(f"ERROR: exporter up{{job=\"postgres\"}}={up!r}; exporter was restarted or lost")
            return 3
        if pg_up == 0:
            saw_down = True
        elif pg_up == 1 and saw_down:
            print("observed: up=1 with pg_up=0 while the database restarted")
            print("observed: pg_up returned to 1 without restarting the exporter")
            print("RESULT: OK")
            return 0
        time.sleep(0.5)

    print(f"ERROR: did not observe pg_up 0 then 1 within {args.timeout:g}s")
    return 3


if __name__ == "__main__":
    sys.exit(main())
