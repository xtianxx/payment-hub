"""从 Prometheus 导出 Benchmark 运行时间窗内的契约指标查询结果。

时间窗来自 ``<run-dir>/summary.json``：取所有 ``stage == "load200"`` 阶段中
有效的 ``k6_started_epoch`` / ``k6_finished_epoch``，整体窗口为最早开始到最晚结束。
查询在窗口结束时刻做一次即时查询（instant query）。

导出布局（``--out`` 指向的目录）：

- ``<query>.csv``：每个查询一份，表头固定为 ``metric,labels,timestamp,value``；
  ``labels`` 为按标签名排序后的 ``k=v;...`` 字符串（无标签为空），
  ``timestamp`` 与 ``value`` 保留 Prometheus 返回值原文；空结果只写表头。
- ``queries.json``：schema 固定为 ``payment-hub.metrics-export.v1``，记录
  ``run_id``、窗口（ISO Z 与 epoch）、Prometheus 地址以及每个查询的
  ``name``、``csv``、``promql``、``status``、``samples``。

整轮 P95 查询中的 ``$__range`` 占位符会替换为整体窗口的整数秒数（例如
``[330s]``）。其余查询按 ``contracts/metrics.md`` 的查询契约原样使用 ``[1m]``。

退出码：

- ``0``：全部查询成功导出；
- ``2``：参数、summary 或文件 IO 错误（包括输出目录已存在且非空）；
- ``4``：Prometheus 不可达、HTTP 错误、返回 status 非 success 或响应格式错误。

写入是整体原子落地的：先在同文件系统的临时目录写完全部文件，再用
``os.replace`` 换到 ``--out``。任一查询或写入失败都会清理临时目录，
``--out`` 不会出现残缺结果（已存在的空目录保持原样）。
"""

import argparse
import csv
import datetime
import http.client
import json
import math
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

SCHEMA = "payment-hub.metrics-export.v1"
LOAD_STAGE = "load200"
RANGE_PLACEHOLDER = "$__range"
REQUEST_TIMEOUT_SECONDS = 10.0
CSV_HEADER = ("metric", "labels", "timestamp", "value")

# 查询名称固定，顺序与 contracts/metrics.md 的“查询契约”一致。
# 整轮 P95 使用 $__range 占位符，导出时替换为整体窗口秒数。
QUERY_SPECS = (
    (
        "request_rate",
        "sum(rate(payment_hub_http_requests_total[1m])) or vector(0)",
    ),
    (
        "http_4xx_rate",
        'sum(rate(payment_hub_http_requests_total{status=~"4.."}[1m])) or vector(0)',
    ),
    (
        "http_5xx_rate",
        'sum(rate(payment_hub_http_requests_total{status=~"5.."}[1m])) or vector(0)',
    ),
    (
        "http_5xx_ratio",
        "(sum(rate(payment_hub_http_requests_total{status=~\"5..\"}[1m]))"
        " or vector(0))"
        " / (sum(rate(payment_hub_http_requests_total[1m])) > 0)",
    ),
    (
        "request_rate_by_route",
        "sum by (route, status) (rate(payment_hub_http_requests_total[1m]))",
    ),
    (
        "rolling_p95",
        "histogram_quantile(0.95, sum by (le, route)"
        " (rate(payment_hub_http_request_duration_seconds_bucket[1m])))",
    ),
    (
        "full_window_p95",
        "histogram_quantile(0.95, sum by (le, route)"
        " (increase(payment_hub_http_request_duration_seconds_bucket[$__range])))",
    ),
    ("gc_rate", "rate(go_gc_duration_seconds_count[1m])"),
    (
        "gc_average_pause",
        "rate(go_gc_duration_seconds_sum[1m])"
        " / rate(go_gc_duration_seconds_count[1m])",
    ),
    (
        "db_pool_utilization",
        "payment_hub_db_pool_connections_in_use"
        " / payment_hub_db_pool_connections_max",
    ),
    (
        "db_pool_empty_acquire_rate",
        "rate(payment_hub_db_pool_empty_acquires_total[1m])",
    ),
    (
        "db_pool_average_acquire_wait",
        "rate(payment_hub_db_pool_acquire_duration_seconds_total[1m])"
        " / (rate(payment_hub_db_pool_acquires_total[1m]) > 0)",
    ),
    (
        "pg_xact_commit_rate",
        "sum(rate(pg_stat_database_xact_commit[1m]))",
    ),
    ("pg_waiting_locks", "pg_benchmark_waiting_locks"),
    (
        "pg_cache_hit_ratio",
        "sum(rate(pg_stat_database_blks_hit[1m]))"
        " / ((sum(rate(pg_stat_database_blks_hit[1m]))"
        " + sum(rate(pg_stat_database_blks_read[1m]))) > 0)",
    ),
    (
        "pg_temp_files_rate",
        "sum(rate(pg_stat_database_temp_files[1m]))",
    ),
    (
        "pg_checkpoint_write_time_rate",
        "sum(rate(pg_stat_bgwriter_checkpoint_write_time_total[1m]))",
    ),
)


class ExportInputError(Exception):
    """参数、summary 或文件 IO 错误，对应退出码 2。"""


class ExportPrometheusError(Exception):
    """Prometheus 请求或响应错误，对应退出码 4。"""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "从 Prometheus 导出基准运行整体时间窗内的契约指标查询结果；"
            "每个查询一份 CSV，并写 queries.json 汇总。"
        )
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="基准运行目录，summary.json 提供 load200 阶段时间窗",
    )
    parser.add_argument(
        "--prometheus",
        default="http://127.0.0.1:9090",
        help="Prometheus 基础地址（默认 http://127.0.0.1:9090）",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="导出结果目录；已存在且非空时拒绝，成功时原子替换空目录",
    )
    return parser.parse_args(argv)


def _coerce_epoch(value):
    """把 epoch 字段转为有限的 float；无效值返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number):
        return None
    return number


def _iter_stage_records(summary):
    """遍历顶层 stages 与 rounds[].stages 中的阶段记录。"""
    top = summary.get("stages")
    if isinstance(top, list):
        for record in top:
            if isinstance(record, dict):
                yield record
    rounds = summary.get("rounds")
    if isinstance(rounds, list):
        for round_record in rounds:
            if not isinstance(round_record, dict):
                continue
            stages = round_record.get("stages")
            if isinstance(stages, list):
                for record in stages:
                    if isinstance(record, dict):
                        yield record


def compute_window(summary):
    """返回 (start_epoch, end_epoch)：所有有效 load200 阶段的最早开始与最晚结束。"""
    windows = set()
    for record in _iter_stage_records(summary):
        if record.get("stage") != LOAD_STAGE:
            continue
        start = _coerce_epoch(record.get("k6_started_epoch"))
        end = _coerce_epoch(record.get("k6_finished_epoch"))
        if start is None or end is None or end <= start:
            continue
        windows.add((start, end))
    if not windows:
        raise ExportInputError(
            "summary.json 中没有有效的 load200 阶段窗口"
            "（需要 k6_started_epoch < k6_finished_epoch）"
        )
    return (
        min(start for start, _ in windows),
        max(end for _, end in windows),
    )


def _load_summary(run_dir):
    path = os.path.join(os.fspath(run_dir), "summary.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            summary = json.load(handle)
    except OSError as exc:
        raise ExportInputError(f"无法读取 summary.json（{path}）：{exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportInputError(f"summary.json 不是有效 JSON（{path}）：{exc}") from exc
    if not isinstance(summary, dict):
        raise ExportInputError(f"summary.json 顶层必须是对象（{path}）")
    return summary


def _prepare_out(out):
    """校验输出目录可用，返回绝对路径。已存在且非空时拒绝覆盖。"""
    out_abs = os.path.abspath(os.fspath(out))
    if not os.path.basename(out_abs):
        raise ExportInputError("--out 必须指向具体目录，不能是文件系统根目录")
    if os.path.lexists(out_abs):
        if os.path.islink(out_abs) or not os.path.isdir(out_abs):
            raise ExportInputError(f"输出路径已存在且不是目录：{out_abs}")
        try:
            entries = os.listdir(out_abs)
        except OSError as exc:
            raise ExportInputError(f"无法读取输出目录（{out_abs}）：{exc}") from exc
        if entries:
            raise ExportInputError(
                f"输出目录已存在且非空，拒绝覆盖：{out_abs}"
            )
    parent = os.path.dirname(out_abs) or os.curdir
    if not os.path.isdir(parent):
        raise ExportInputError(f"输出目录的上级目录不存在：{parent}")
    return out_abs


def _utc_iso(epoch):
    moment = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


def _epoch_json_value(epoch):
    if float(epoch).is_integer():
        return int(epoch)
    return epoch


def _format_labels(metric):
    pairs = sorted(
        (str(key), str(value))
        for key, value in metric.items()
        if key != "__name__"
    )
    return ";".join(f"{key}={value}" for key, value in pairs)


def _scalar_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "+Inf" if value > 0 else "-Inf"
        return repr(value)
    raise ExportPrometheusError(f"无法解析指标值类型：{type(value).__name__}")


def _parse_sample_value(name, raw):
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ExportPrometheusError(
            f"查询 {name} 的样本缺少 [timestamp, value] 二元组"
        )
    timestamp, value = raw
    if isinstance(timestamp, bool) or not isinstance(
        timestamp, (int, float, str)
    ):
        raise ExportPrometheusError(f"查询 {name} 的样本时间戳无效：{timestamp!r}")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ExportPrometheusError(f"查询 {name} 的样本值无效：{value!r}")
    return {
        "metric": {},
        "timestamp": str(timestamp),
        "value": _scalar_text(value),
    }


def _parse_samples(name, payload):
    """把 Prometheus 即时查询响应转为样本列表；空结果返回 []。"""
    if not isinstance(payload, dict):
        raise ExportPrometheusError(f"查询 {name} 的 Prometheus 响应不是 JSON 对象")
    if payload.get("status") != "success":
        message = payload.get("error") or "未知错误"
        raise ExportPrometheusError(
            f"查询 {name} 返回 Prometheus 错误状态：{message}"
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ExportPrometheusError(f"查询 {name} 的响应缺少 data 对象")
    result_type = data.get("resultType")
    result = data.get("result")
    if result is None:
        result = []
    if result_type == "scalar":
        sample = _parse_sample_value(name, result)
        sample["metric"] = {}
        return [sample]
    if not isinstance(result, list):
        raise ExportPrometheusError(f"查询 {name} 的 data.result 不是数组")
    if not result:
        return []
    if result_type != "vector":
        raise ExportPrometheusError(
            f"查询 {name} 的 resultType 不是 vector（实际 {result_type!r}）"
        )
    samples = []
    for item in result:
        if not isinstance(item, dict):
            raise ExportPrometheusError(f"查询 {name} 的向量样本不是对象")
        metric = item.get("metric", {})
        if not isinstance(metric, dict):
            raise ExportPrometheusError(f"查询 {name} 的样本标签不是对象")
        sample = _parse_sample_value(name, item.get("value"))
        sample["metric"] = {
            str(key): str(value) for key, value in metric.items()
        }
        samples.append(sample)
    return samples


def fetch_query(base_url, name, promql, timestamp):
    """在 timestamp（epoch 秒）对单条 PromQL 做即时查询并返回样本列表。"""
    params = urllib.parse.urlencode(
        {"query": promql, "time": f"{timestamp:.6f}"}
    )
    url = f"{base_url}/api/v1/query?{params}"
    request = urllib.request.Request(
        url, headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # pragma: no cover - 诊断信息尽力而为
            detail = ""
        raise ExportPrometheusError(
            f"查询 {name} 的 Prometheus 请求返回 HTTP {exc.code}：{detail}"
        ) from exc
    except (
        urllib.error.URLError,
        OSError,
        http.client.HTTPException,
        ValueError,
    ) as exc:
        raise ExportPrometheusError(
            f"查询 {name} 请求 Prometheus 失败：{exc}"
        ) from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportPrometheusError(
            f"查询 {name} 的 Prometheus 响应不是有效 JSON：{exc}"
        ) from exc
    return _parse_samples(name, payload)


def _write_csv(path, samples):
    ordered = sorted(
        samples,
        key=lambda sample: (
            sample["metric"].get("__name__", ""),
            _format_labels(sample["metric"]),
            sample["timestamp"],
        ),
    )
    try:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(CSV_HEADER)
            for sample in ordered:
                writer.writerow(
                    (
                        sample["metric"].get("__name__", ""),
                        _format_labels(sample["metric"]),
                        sample["timestamp"],
                        sample["value"],
                    )
                )
    except OSError as exc:
        raise ExportInputError(f"写入 CSV 失败（{path}）：{exc}") from exc


def _write_json(path, payload):
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except OSError as exc:
        raise ExportInputError(f"写入 JSON 失败（{path}）：{exc}") from exc


def export_metrics(run_dir, prometheus, out):
    """执行导出；返回退出码。失败时不会在 out 留下任何结果。"""
    summary = _load_summary(run_dir)
    start_epoch, end_epoch = compute_window(summary)
    window_seconds = int(math.floor((end_epoch - start_epoch) + 0.5))
    if window_seconds <= 0:
        raise ExportInputError("load200 整体窗口不足 1 秒，无法导出整轮查询")

    out_abs = _prepare_out(out)
    prometheus_base = str(prometheus).rstrip("/")
    if not prometheus_base:
        raise ExportInputError("--prometheus 不能为空")

    run_id = summary.get("run_id")
    parent = os.path.dirname(out_abs) or os.curdir
    basename = os.path.basename(out_abs)
    try:
        tmp_dir = tempfile.mkdtemp(prefix=f".{basename}.tmp-", dir=parent)
    except OSError as exc:
        raise ExportInputError(
            f"无法在输出目录的上级目录创建临时目录（{parent}）：{exc}"
        ) from exc
    committed = False
    try:
        query_entries = []
        for name, promql_template in QUERY_SPECS:
            promql = promql_template.replace(
                RANGE_PLACEHOLDER, f"{window_seconds}s"
            )
            samples = fetch_query(
                prometheus_base, name, promql, end_epoch
            )
            csv_name = f"{name}.csv"
            _write_csv(os.path.join(tmp_dir, csv_name), samples)
            query_entries.append(
                {
                    "name": name,
                    "csv": csv_name,
                    "promql": promql,
                    "status": "success",
                    "samples": len(samples),
                }
            )

        manifest = {
            "schema": SCHEMA,
            "run_id": run_id,
            "window": {
                "start_utc": _utc_iso(start_epoch),
                "end_utc": _utc_iso(end_epoch),
                "start_epoch": _epoch_json_value(start_epoch),
                "end_epoch": _epoch_json_value(end_epoch),
            },
            "prometheus": prometheus_base,
            "queries": query_entries,
        }
        _write_json(os.path.join(tmp_dir, "queries.json"), manifest)

        try:
            os.replace(tmp_dir, out_abs)
        except OSError as exc:
            raise ExportInputError(
                f"无法把临时导出目录原子替换为 {out_abs}：{exc}"
            ) from exc
        committed = True
        print(
            f"已导出 {len(query_entries)} 个查询到 {out_abs}"
            f"（窗口 {window_seconds}s，run_id={run_id}）"
        )
        return 0
    finally:
        if not committed:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main(argv=None):
    args = parse_args(argv)
    try:
        return export_metrics(args.run_dir, args.prometheus, args.out)
    except ExportInputError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except ExportPrometheusError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 4
    except OSError as exc:
        # 兜底：任何未包装的文件系统错误都按参数/IO 错误处理。
        print(f"错误：文件 IO 失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
