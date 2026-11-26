#!/usr/bin/env python3
"""演示支付更新路径上的锁等待，并验证自定义锁指标能观察到等待。

脚本用一个 psql 会话对 `payment_orders` 持有 `ACCESS EXCLUSIVE` 表锁，并在
holder 报告锁已获取后，用第二个会话执行同一张表上的支付更新（`UPDATE`），
使其阻塞在表锁上。随后同时要求：

- 自定义指标 `pg_benchmark_waiting_locks` 大于 0；
- `pg_locks` 中存在指向该表且尚未授予（`granted = false`）的锁。

DSN 从环境变量读取（默认 `PAYMENT_HUB_DEMO_DSN`），密码不会进入 argv：
连接串去掉密码后传给 `psql`，密码写入权限 `0600` 的临时 `PGPASSFILE`。

退出码：0 观察到等待；2 参数、连接或传输错误；3 未观察到契约要求。
"""

import argparse
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

TABLE = "payment_orders"
KEY_COLUMN = "business_order_no"
DEMO_KEY = "lock-wait-demo-missing"
WAITING_METRIC = "pg_benchmark_waiting_locks"
# 只允许简单的标识符，避免把用户输入拼进 SQL 时引入注入面。
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class UsageError(Exception):
    """参数或环境错误，退出码 2。"""


class TransportError(Exception):
    """Prometheus 不可达，退出码 2。"""


class DemoError(Exception):
    """契约未满足，退出码 3。"""


@dataclass
class Connection:
    """从 DSN 拆出的连接信息；`conninfo` 不含密码。"""

    conninfo: str
    host: str
    port: str
    dbname: str
    user: str
    password: str | None


def parse_dsn(dsn):
    parts = urlsplit(dsn)
    if parts.scheme not in ("postgres", "postgresql"):
        raise UsageError("DSN 必须以 postgres:// 或 postgresql:// 开头")
    if not parts.hostname:
        raise UsageError("DSN 必须包含主机名")
    dbname = unquote(parts.path.lstrip("/"))
    if not dbname:
        raise UsageError("DSN 必须包含数据库名")
    user = unquote(parts.username or "")
    password = unquote(parts.password) if parts.password is not None else None
    if password is not None and not user:
        raise UsageError("DSN 含密码时必须同时提供用户名")
    host = parts.hostname
    port = str(parts.port or 5432)
    netloc = host if not parts.port else f"{host}:{parts.port}"
    if user:
        netloc = f"{urllib.parse.quote(user, safe='')}@{netloc}"
    conninfo = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return Connection(
        conninfo=conninfo,
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
    )


def pgpass_line(connection):
    def escape(value):
        return value.replace("\\", "\\\\").replace(":", "\\:")

    fields = (
        connection.host,
        connection.port,
        connection.dbname,
        connection.user,
        connection.password,
    )
    return ":".join(escape(value) for value in fields) + "\n"


def prepare_environment(connection):
    """返回 (env, 临时目录)：有密码时使用 0600 PGPASSFILE，不写 argv。"""
    env = dict(os.environ)
    env.pop("PGPASSWORD", None)
    if connection.password is None:
        return env, None
    directory = tempfile.mkdtemp(prefix="payment-hub-lock-demo-")
    path = Path(directory) / "pgpass"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(pgpass_line(connection))
    env["PGPASSFILE"] = str(path)
    return env, Path(directory)


def psql(connection, env, extra=(), stdout=subprocess.PIPE):
    try:
        return subprocess.Popen(
            ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", connection.conninfo, *extra],
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except FileNotFoundError as exc:
        raise UsageError("PATH 中找不到 psql 可执行文件") from exc


def run_psql(connection, env, sql):
    try:
        proc = subprocess.run(
            ["psql", "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1", "-d", connection.conninfo, "-c", sql],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
    except FileNotFoundError as exc:
        raise UsageError("PATH 中找不到 psql 可执行文件") from exc
    except subprocess.TimeoutExpired as exc:
        raise UsageError("查询 pg_locks 超时") from exc
    if proc.returncode != 0:
        raise UsageError(f"psql 查询失败：{proc.stderr.strip() or proc.returncode}")
    return proc.stdout.strip()


def wait_for_marker(process, marker, timeout):
    """读取 holder 的原始输出，直到出现 marker，证明表锁已经获取。"""
    descriptor = process.stdout.fileno()
    data = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([descriptor], [], [], min(0.2, remaining))
        if not ready:
            continue
        chunk = os.read(descriptor, 4096)
        if not chunk:
            raise UsageError(
                f"lock holder 在获取表锁前退出：{process.stderr.read().strip()}"
            )
        data += chunk
        if marker.encode("utf-8") in data:
            return
    raise DemoError(f"lock holder 在 {timeout:g}s 内没有报告 {marker}")


def pending_table_locks(connection, env):
    sql = (
        "SELECT count(*) FROM pg_locks l "
        "JOIN pg_class c ON c.oid = l.relation "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE c.relname = '{TABLE}' AND n.nspname = current_schema "
        "AND NOT l.granted"
    )
    output = run_psql(connection, env, sql)
    try:
        return int(output)
    except ValueError as exc:
        raise UsageError(f"无法解析 pg_locks 计数：{output!r}") from exc


def metric_value(prometheus, expression, timeout):
    url = (
        prometheus.rstrip("/")
        + "/api/v1/query?query="
        + urllib.parse.quote(expression, safe="")
    )
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransportError(f"无法访问 Prometheus：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise TransportError(f"Prometheus 返回了无效 JSON：{exc}") from exc
    if payload.get("status") != "success":
        raise TransportError(payload.get("error") or "Prometheus 查询失败")
    result = payload["data"]["result"]
    if not result:
        return None
    return float(result[0]["value"][1])


def wait_for_waiting_lock(waiter, connection, env, args):
    deadline = time.monotonic() + args.timeout
    last = "尚未观察到等待"
    last_transport = None
    saw_sample = False
    while time.monotonic() < deadline:
        if waiter.poll() is not None:
            detail = waiter.stderr.read().strip()
            if "does not exist" in detail:
                raise UsageError(
                    f"UPDATE 未阻塞且表不存在，请先运行迁移：{detail}"
                )
            raise DemoError(f"waiter 在阻塞前退出：{detail or waiter.returncode}")
        pending = pending_table_locks(connection, env)
        try:
            value = metric_value(args.prometheus, WAITING_METRIC, max(1.0, args.timeout))
        except TransportError as exc:
            last_transport = exc
            last = str(exc)
        else:
            last_transport = None
            if value is not None:
                saw_sample = True
            last = f"waiting_locks={value}，pending={pending}"
            if pending > 0 and value is not None and value > 0:
                return
        time.sleep(0.5)
    if last_transport is not None and not saw_sample:
        raise TransportError(str(last_transport))
    raise DemoError(
        f"在 {args.timeout:g}s 内没有同时看到本表演示的等待和指标（{last}）"
    )


def stop_process(process, graceful):
    if process is None:
        return
    if process.poll() is None and graceful:
        try:
            process.stdin.write("ROLLBACK;\n\\q\n")
            process.stdin.flush()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    if process.poll() is None:
        process.kill()
        process.wait()
    for stream in (process.stdin, process.stdout, process.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus", default="http://127.0.0.1:9090")
    parser.add_argument("--dsn-env", default="PAYMENT_HUB_DEMO_DSN")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)

    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        print(f"ERROR: 请设置 {args.dsn_env} 为本地基准库 DSN", file=sys.stderr)
        return 2
    for name, value in (("TABLE", TABLE), ("KEY_COLUMN", KEY_COLUMN)):
        if not IDENTIFIER.match(value):
            print(f"ERROR: {name} 不是合法标识符：{value!r}", file=sys.stderr)
            return 2

    holder = waiter = None
    passfile_dir = None
    try:
        connection = parse_dsn(dsn)
        env, passfile_dir = prepare_environment(connection)
        holder = psql(connection, env)
        holder.stdin.write(
            f"BEGIN;\nLOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE;\n"
            "\\echo LOCK_ACQUIRED\n"
        )
        holder.stdin.flush()
        wait_for_marker(holder, "LOCK_ACQUIRED", args.timeout)

        waiter = psql(connection, env, stdout=subprocess.DEVNULL)
        waiter.stdin.write(
            f"UPDATE {TABLE} SET result = result WHERE {KEY_COLUMN} = '{DEMO_KEY}';\n"
        )
        waiter.stdin.flush()
        time.sleep(0.2)

        wait_for_waiting_lock(waiter, connection, env, args)
        print(f"observed: {WAITING_METRIC} > 0 且 pg_locks 存在未授予的本表锁")
        print("复现（终端 1，保持会话不退出；用你自己的本地 DSN 连接）：")
        print(f"  BEGIN;")
        print(f"  LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE;")
        print("复现（终端 2）：")
        print(
            f"  psql -d <本地演示 DSN> -c \"UPDATE {TABLE} SET result = result "
            f"WHERE {KEY_COLUMN} = '{DEMO_KEY}';\""
        )
        print("释放（终端 1）：ROLLBACK;")
        print("RESULT: OK")
        return 0
    except UsageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except TransportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except DemoError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    finally:
        # waiter 可能仍阻塞在 UPDATE 上，直接断开会话；holder 先回滚释放表锁。
        stop_process(waiter, graceful=False)
        stop_process(holder, graceful=True)
        if passfile_dir is not None:
            shutil.rmtree(passfile_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
