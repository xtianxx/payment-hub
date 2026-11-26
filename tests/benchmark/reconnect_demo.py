#!/usr/bin/env python3
"""演示 postgres-exporter 在数据库停机窗口内保持存活。

前置条件：Prometheus 与 postgres-exporter 正常运行，up{job="postgres"} 为 1，
pg_up 为 1。脚本先记录 process_start_time_seconds{job="postgres"} 作为 exporter
身份，然后执行 docker stop 制造可观察的停机窗口，要求：

  - 停机窗口内 up{job="postgres"} 保持为 1，并且至少观测到一次 pg_up 为 0；
  - 执行 docker start 后，up 与 pg_up 都恢复为 1；
  - 恢复后 exporter 的 process_start_time_seconds 与停机前完全一致。

退出码：0 观察到停机并恢复；2 参数、环境或传输错误；3 契约不满足。
"""

import argparse
import json
import math
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

POLL_INTERVAL_SECONDS = 0.5
DOCKER_INSPECT_TIMEOUT_SECONDS = 15.0
DOCKER_STOP_TIMEOUT_SECONDS = 60.0
DOCKER_START_TIMEOUT_SECONDS = 60.0

UP_EXPRESSION = 'up{job="postgres"}'
PG_UP_EXPRESSION = "pg_up"
EXPORTER_IDENTITY_EXPRESSION = 'process_start_time_seconds{job="postgres"}'


class PreconditionError(RuntimeError):
    """参数、环境或前置条件错误，对应退出码 2。"""


class TransportError(RuntimeError):
    """查询 Prometheus 时的传输错误，对应退出码 2。"""


class ContractError(RuntimeError):
    """观测结果不满足契约，对应退出码 3。"""


class PrometheusClient:
    """只读的 Prometheus 查询客户端。"""

    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")
        self.successful_queries = 0

    def query_values(self, expression, timeout):
        """查询表达式并返回所有样本值。"""
        url = (
            self.base_url
            + "/api/v1/query?query="
            + urllib.parse.quote(expression, safe="")
        )
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                body = response.read()
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise TransportError(f"查询 {expression} 失败：{exc}") from exc

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError(
                f"查询 {expression} 返回的 JSON 无法解析：{exc}"
            ) from exc

        if not isinstance(payload, dict):
            raise TransportError(f"查询 {expression} 的响应不是 JSON 对象：{payload!r}")
        if payload.get("status") != "success":
            detail = payload.get("error") or payload.get("errorType")
            raise TransportError(
                f"查询 {expression} 被 Prometheus 拒绝：{detail or payload!r}"
            )

        data = payload.get("data")
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, list):
            raise TransportError(f"查询 {expression} 的响应缺少 result 列表")

        values = []
        for sample in result:
            if not isinstance(sample, dict):
                raise TransportError(f"查询 {expression} 返回了无效样本：{sample!r}")
            value = sample.get("value")
            if not isinstance(value, list) or len(value) != 2:
                raise TransportError(f"查询 {expression} 返回了无效样本：{sample!r}")
            try:
                values.append(float(value[1]))
            except (TypeError, ValueError) as exc:
                raise TransportError(
                    f"查询 {expression} 的样本值无效：{sample!r}"
                ) from exc

        self.successful_queries += 1
        return values


def format_values(values):
    if not values:
        return "[]"
    return "[" + ", ".join(f"{value:g}" for value in values) + "]"


def observe_up_state(client, query_timeout):
    up_values = client.query_values(UP_EXPRESSION, query_timeout)
    pg_up_values = client.query_values(PG_UP_EXPRESSION, query_timeout)
    summary = (
        f"up 样本={format_values(up_values)}，"
        f"pg_up 样本={format_values(pg_up_values)}"
    )
    return up_values, pg_up_values, summary


def all_up_is_one(up_values):
    return bool(up_values) and all(value == 1.0 for value in up_values)


def probe_ready(client, query_timeout):
    up_values, pg_up_values, summary = observe_up_state(client, query_timeout)
    return (all_up_is_one(up_values) and pg_up_values == [1.0], summary)


def probe_downtime(client, query_timeout):
    up_values, pg_up_values, summary = observe_up_state(client, query_timeout)
    if any(value != 1.0 for value in up_values):
        raise ContractError(f"停机窗口内 exporter up 出现非 1 样本：{summary}")
    return (all_up_is_one(up_values) and pg_up_values == [0.0], summary)


def probe_recovered(client, query_timeout):
    up_values, pg_up_values, summary = observe_up_state(client, query_timeout)
    if any(value != 1.0 for value in up_values):
        raise ContractError(f"恢复阶段 exporter up 出现非 1 样本：{summary}")
    return (all_up_is_one(up_values) and pg_up_values == [1.0], summary)


def wait_for_condition(client, description, probe, timeout):
    """轮询直到 probe 满足，返回最后一次观测摘要。"""
    deadline = time.monotonic() + timeout
    last_summary = "尚未取得观测"
    last_transport_error = None
    observed = False

    while True:
        remaining = deadline - time.monotonic()
        query_timeout = max(0.5, remaining)
        try:
            satisfied, summary = probe(client, query_timeout)
        except TransportError as exc:
            last_transport_error = exc
            last_summary = f"查询失败：{exc}"
        else:
            observed = True
            last_summary = summary
            if satisfied:
                return summary

        if time.monotonic() >= deadline:
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    if not observed:
        detail = (
            f"；最后传输错误：{last_transport_error}"
            if last_transport_error is not None
            else ""
        )
        raise TransportError(
            f"{description}：在 {timeout:g}s 内没有完成一次成功的 up/pg_up 观测{detail}"
        )
    raise ContractError(
        f"{description}：在 {timeout:g}s 内未满足，最后观测：{last_summary}"
    )


def run_docker(arguments, timeout):
    """执行 docker 命令；超时或非零退出码都转成明确的环境错误。"""
    command = ["docker", *arguments]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise PreconditionError("未找到 docker 可执行文件") from exc
    except subprocess.TimeoutExpired as exc:
        raise PreconditionError(
            f"docker 命令在 {timeout:g}s 内未返回：{' '.join(command)}"
        ) from exc
    except OSError as exc:
        raise PreconditionError(
            f"无法执行 docker 命令：{' '.join(command)}：{exc}"
        ) from exc

    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        detail = f"；stderr：{stderr}" if stderr else ""
        raise PreconditionError(
            f"docker 命令退出码为 {completed.returncode}："
            f"{' '.join(command)}{detail}"
        )
    return completed.stdout.strip()


def validate_container(container):
    name = run_docker(
        ["inspect", "--format", "{{.Name}}", container],
        DOCKER_INSPECT_TIMEOUT_SECONDS,
    )
    if not name:
        raise PreconditionError(f"docker inspect 未返回容器 {container} 的名称")


def stop_container(container):
    run_docker(
        ["stop", "--time", "5", container],
        DOCKER_STOP_TIMEOUT_SECONDS,
    )


def start_container(container):
    run_docker(["start", container], DOCKER_START_TIMEOUT_SECONDS)


def container_is_running(container):
    state = run_docker(
        ["inspect", "--format", "{{.State.Running}}", container],
        DOCKER_INSPECT_TIMEOUT_SECONDS,
    )
    return state.strip().lower() == "true"


def read_identity_values(client, timeout):
    return client.query_values(EXPORTER_IDENTITY_EXPRESSION, timeout)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--prometheus",
        default="http://127.0.0.1:9090",
        help="Prometheus 基础 URL（默认 http://127.0.0.1:9090）",
    )
    parser.add_argument(
        "--container",
        required=True,
        help="benchmark PostgreSQL 容器名，会先用 docker inspect 校验存在",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="每个轮询阶段的最长等待秒数（默认 60）",
    )
    args = parser.parse_args(argv)

    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout 必须是大于 0 的有限数值")

    client = PrometheusClient(args.prometheus)
    stop_issued = False
    start_confirmed = False
    cleanup_failed = False
    exit_code = 0

    try:
        validate_container(args.container)

        summary = wait_for_condition(
            client,
            "前置条件 up=1 且 pg_up=1",
            probe_ready,
            args.timeout,
        )
        print(f"observed: 前置条件已满足（{summary}）")

        identity_values = read_identity_values(client, args.timeout)
        if len(identity_values) != 1:
            raise PreconditionError(
                "无法取得唯一的 exporter 身份："
                f"{EXPORTER_IDENTITY_EXPRESSION} 返回 {len(identity_values)} 个样本 "
                f"{format_values(identity_values)}"
            )
        identity_before = identity_values[0]
        print(f"observed: exporter 身份 process_start_time_seconds={identity_before!r}")

        # 在调用 stop 之前置位：即使客户端超时或返回码异常，服务端也可能已经
        # 停止了容器，因此 finally 中仍需尝试 docker start 恢复数据库。
        stop_issued = True
        stop_container(args.container)
        print(f"action: docker stop --time 5 {args.container} 已返回")

        summary = wait_for_condition(
            client,
            "停机窗口 up=1 且 pg_up=0",
            probe_downtime,
            args.timeout,
        )
        print(f"observed: 停机窗口内 {summary}")

        start_container(args.container)
        if not container_is_running(args.container):
            raise PreconditionError(
                f"docker start 后容器 {args.container} 未处于运行状态"
            )
        start_confirmed = True
        print(f"action: docker start {args.container} 已返回且容器正在运行")

        summary = wait_for_condition(
            client,
            "恢复 up=1 且 pg_up=1",
            probe_recovered,
            args.timeout,
        )
        print(f"observed: 恢复后 {summary}")

        identity_values = read_identity_values(client, args.timeout)
        if len(identity_values) != 1:
            raise PreconditionError(
                "恢复后无法取得唯一的 exporter 身份，无法完成一致性校验："
                f"{EXPORTER_IDENTITY_EXPRESSION} 返回 {len(identity_values)} 个样本 "
                f"{format_values(identity_values)}"
            )
        identity_after = identity_values[0]
        if identity_after != identity_before:
            raise ContractError(
                "exporter 身份发生变化："
                f"{identity_before!r} -> {identity_after!r}，"
                "exporter 可能随数据库一起重启"
            )
        print(f"observed: exporter 身份保持不变（{identity_after!r}）")
        print("RESULT: OK")
    except PreconditionError as exc:
        print(f"ERROR: 前置条件或环境错误：{exc}", file=sys.stderr)
        exit_code = 2
    except TransportError as exc:
        print(f"ERROR: Prometheus 传输错误：{exc}", file=sys.stderr)
        exit_code = 2
    except ContractError as exc:
        if client.successful_queries == 0:
            print(
                f"ERROR: Prometheus 传输错误：整个流程没有任何一次查询成功：{exc}",
                file=sys.stderr,
            )
            exit_code = 2
        else:
            print(f"ERROR: 契约不满足：{exc}", file=sys.stderr)
            exit_code = 3
    finally:
        if stop_issued and not start_confirmed:
            try:
                start_container(args.container)
                if not container_is_running(args.container):
                    raise PreconditionError(
                        f"docker start 后容器 {args.container} 未处于运行状态"
                    )
            except Exception as exc:
                # 清理阶段即使出现意外异常，也只报告恢复失败，不覆盖原始错误。
                cleanup_failed = True
                print(
                    "ERROR: 恢复失败，数据库容器可能仍处于停止状态："
                    f"{exc}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"cleanup: 已执行 docker start，容器 {args.container} 处于运行状态"
                )

    if cleanup_failed and exit_code == 0:
        exit_code = 2
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
