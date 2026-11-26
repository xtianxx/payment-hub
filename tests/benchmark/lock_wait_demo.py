#!/usr/bin/env python3
"""Hold a table lock and prove guild-added lock-wait metrics observe the waiter.

The demo opens one psql session that takes ACCESS EXCLUSIVE on the target table,
starts a second session that blocks on the table, and polls Prometheus until
pg_benchmark_waiting_locks > 0. Both sessions are always rolled back/terminated.

The DSN is read from an environment variable (default PAYMENT_HUB_DEMO_DSN) so
the password never appears in argv.

Exit codes: 0 observed, 2 usage/transport error, 3 not observed.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def query(prometheus, expression, timeout):
    url = (
        prometheus.rstrip("/")
        + "/api/v1/query?query="
        + urllib.parse.quote(expression, safe="")
    )
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("status") != "success":
        raise RuntimeError(payload.get("error") or "Prometheus query failed")
    return payload["data"]["result"]


def psql(dsn, statement):
    return subprocess.Popen(
        ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", dsn],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def wait_for(condition, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {description}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus", default="http://127.0.0.1:9090")
    parser.add_argument("--dsn-env", default="PAYMENT_HUB_DEMO_DSN")
    parser.add_argument("--table", default="payment_orders")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)

    if not IDENTIFIER.match(args.table):
        print(f"ERROR: invalid table identifier: {args.table!r}")
        return 2
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        print(f"ERROR: set {args.dsn_env} to a PostgreSQL DSN")
        return 2

    try:
        holder = psql(dsn, "")
        waiter = psql(dsn, "")
    except FileNotFoundError:
        print("ERROR: psql executable not found in PATH")
        return 2
    try:
        holder.stdin.write(
            f"BEGIN;\nLOCK TABLE {args.table} IN ACCESS EXCLUSIVE MODE;\n"
        )
        holder.stdin.flush()
        time.sleep(0.5)
        if holder.poll() is not None:
            print(f"ERROR: lock holder exited: {holder.stderr.read()}")
            return 3
        waiter.stdin.write(f"SELECT count(*) FROM {args.table};\n")
        waiter.stdin.flush()

        def observed():
            result = query(
                args.prometheus, "pg_benchmark_waiting_locks", args.timeout
            )
            return bool(result) and float(result[0]["value"][1]) > 0

        try:
            wait_for(observed, args.timeout, "pg_benchmark_waiting_locks > 0")
        except (RuntimeError, urllib.error.URLError, TimeoutError) as exc:
            print(f"ERROR: {exc}")
            return 3
        print("observed: pg_benchmark_waiting_locks > 0")
        print(
            "reproduce: psql \"$PAYMENT_HUB_DEMO_DSN\" -c "
            f"'BEGIN; LOCK TABLE {args.table} IN ACCESS EXCLUSIVE MODE;'"
        )
        print(
            "reproduce: psql \"$PAYMENT_HUB_DEMO_DSN\" -c "
            f"'SELECT count(*) FROM {args.table};'"
        )
        print("RESULT: OK")
        return 0
    finally:
        for process in (waiter, holder):
            try:
                if process.poll() is None:
                    process.stdin.write("ROLLBACK;\n\\q\n")
                    process.stdin.flush()
                    process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait()
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass


if __name__ == "__main__":
    sys.exit(main())
