#!/usr/bin/env python3
"""校验本地观测栈是否满足指标契约。

检查内容：
- Prometheus 中必须出现 pg_benchmark_waiting_locks 与
  pg_stat_database_xact_commit；
- pg_up 必须在超时内轮询到值 1；
- postgres-exporter 的 /metrics 必须包含 pg_benchmark_waiting_locks，
  且不得出现 usename 或 application_name 标签。

退出码：
  0  全部通过；
  2  参数或 HTTP 传输错误（连接失败、请求超时、响应无法解析）；
  3  契约要求未满足（序列缺失、pg_up 未到 1、exporter 指标或标签不符）。
"""

import argparse
import http.client
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REQUIRED_QUERIES = (
    "pg_benchmark_waiting_locks",
    "pg_stat_database_xact_commit",
)
FORBIDDEN_LABELS = ("usename", "application_name")
EXPORTER_SERIES = "pg_benchmark_waiting_locks"
PG_UP_EXPRESSION = "pg_up"
POLL_INTERVAL_SECONDS = 0.5


class TransportError(RuntimeError):
    """HTTP 传输或响应解析失败，对应退出码 2。"""


class ContractError(RuntimeError):
    """观测契约未满足，对应退出码 3。"""


def redact(text):
    """隐藏文本中 URL 可能携带的用户名和密码。"""
    return re.sub(r"(://)[^/@\s]+@", r"\1***@", text)


def request_text(url, timeout, service):
    """读取 URL 文本；连接失败、超时或 HTTP 异常统一转为传输错误。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        OSError,
        ValueError,
    ) as exc:
        detail = redact(str(exc)) or exc.__class__.__name__
        raise TransportError(f"{service} 请求失败：{detail}") from exc


def query(prometheus, expression, timeout):
    """执行一次 Prometheus 即时查询并返回样本列表。"""
    url = (
        prometheus.rstrip("/")
        + "/api/v1/query?query="
        + urllib.parse.quote(expression, safe="")
    )
    try:
        payload = json.loads(request_text(url, timeout, "Prometheus"))
    except json.JSONDecodeError as exc:
        raise TransportError(f"Prometheus 响应不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise TransportError("Prometheus 响应结构异常：顶层不是 JSON 对象")
    if payload.get("status") != "success":
        raise ContractError(
            f"Prometheus 查询 {expression} 失败：{payload.get('error')}"
        )
    data = payload.get("data")
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, list):
        raise TransportError("Prometheus 响应结构异常：缺少 data.result 列表")
    return result


def _sleep_before_retry(deadline):
    remaining = deadline - time.monotonic()
    time.sleep(max(0.0, min(POLL_INTERVAL_SECONDS, remaining)))


def wait_for_series(prometheus, expression, timeout):
    """在超时内轮询，直到表达式返回至少一个样本。

    超时后若期间至少成功查询过一次，说明序列确实缺失，抛出 ContractError；
    若一次都没有成功查询，则抛出 TransportError，交由上层映射为退出码 2。
    """
    deadline = time.monotonic() + timeout
    answered = False
    last_detail = "尚未查询"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            result = query(prometheus, expression, max(0.1, remaining))
        except TransportError as exc:
            last_detail = str(exc)
        else:
            answered = True
            if result:
                return result
            last_detail = "查询结果为空"
        _sleep_before_retry(deadline)
    if answered:
        raise ContractError(
            f"{expression} 在 {timeout:g}s 内未出现（{last_detail}）"
        )
    raise TransportError(
        f"在 {timeout:g}s 内未能成功查询 {expression}（{last_detail}）"
    )


def pg_up_value(result):
    """从 pg_up 查询结果中取第一个样本的数值。"""
    try:
        return float(result[0]["value"][1])
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise TransportError(f"pg_up 样本结构异常：{exc}") from exc


def wait_for_pg_up(prometheus, timeout):
    """在超时内轮询 pg_up，只有值等于 1 才通过。

    值 0 表示数据库尚未就绪，继续等待；超时时若期间一次都没有成功查询，
    返回传输错误，否则返回契约未满足。
    """
    deadline = time.monotonic() + timeout
    answered = False
    last_value = None
    last_detail = "尚未查询"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            result = query(prometheus, PG_UP_EXPRESSION, max(0.1, remaining))
        except TransportError as exc:
            last_detail = str(exc)
        else:
            answered = True
            if result:
                last_value = pg_up_value(result)
                if last_value == 1:
                    return last_value
                last_detail = f"pg_up={last_value:g}"
            else:
                last_detail = "pg_up 查询结果为空"
        _sleep_before_retry(deadline)
    if not answered:
        raise TransportError(
            f"在 {timeout:g}s 内未能成功查询 pg_up（{last_detail}）"
        )
    raise ContractError(
        f"pg_up 在 {timeout:g}s 内未达到 1（{last_detail}）"
    )


def check_exporter(exporter, timeout):
    """校验 exporter 暴露必需序列且不含禁用标签。"""
    body = request_text(
        exporter.rstrip("/") + "/metrics", timeout, "exporter"
    )
    leaked = [label for label in FORBIDDEN_LABELS if label in body]
    if leaked:
        raise ContractError(
            "exporter /metrics 暴露禁用标签：" + "、".join(leaked)
        )
    if EXPORTER_SERIES not in body:
        raise ContractError(f"exporter /metrics 缺少 {EXPORTER_SERIES}")


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "校验本地观测栈的指标契约：Prometheus 序列存在性、"
            "pg_up 数值以及 exporter 标签暴露情况。"
        ),
        epilog=(
            "退出码：0 全部通过；2 参数或 HTTP 传输错误；"
            "3 契约要求未满足。"
        ),
    )
    parser.add_argument(
        "--prometheus",
        default="http://127.0.0.1:9090",
        help="Prometheus 基础地址（默认：http://127.0.0.1:9090）",
    )
    parser.add_argument(
        "--exporter",
        default="http://127.0.0.1:9187",
        help="postgres-exporter 基础地址（默认：http://127.0.0.1:9187）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="单次请求及等待序列就绪的超时秒数（默认：10）",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout 必须是大于 0 的有限数值")

    try:
        for expression in REQUIRED_QUERIES:
            result = wait_for_series(args.prometheus, expression, args.timeout)
            print(f"序列 {expression} 可用，样本数 {len(result)}")
        value = wait_for_pg_up(args.prometheus, args.timeout)
        print(f"pg_up={value:g}")
        check_exporter(args.exporter, args.timeout)
        print("exporter /metrics 的指标与标签检查通过")
    except TransportError as exc:
        print(f"错误：HTTP 传输或响应解析失败：{exc}", file=sys.stderr)
        return 2
    except ContractError as exc:
        print(f"错误：观测契约未满足：{exc}", file=sys.stderr)
        return 3

    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
