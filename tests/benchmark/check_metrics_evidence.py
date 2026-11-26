"""检查一次基准运行的指标证据。

脚本读取 ``<运行目录>/summary.json`` 中所有有效的 ``stage == "load200"``
测量阶段，在窗口结束时刻查询 Prometheus，并把结果写入
``<运行目录>/metrics-evidence.json``（schema
``payment-hub.metrics-evidence.v2``）。只判定实际运行的测量阶段；未运行的
轮次只记入整体缺失列表，不伪造阶段记录。

退出码：

- 0：全部测量阶段完整；
- 3：存在不完整阶段或缺失轮次；
- 2：参数、文件或 JSON 错误；
- 4：Prometheus 不可达或返回错误；此时仍会写出不完整证据和原因。
"""

import argparse
import datetime
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

SCHEMA = "payment-hub.metrics-evidence.v2"
LOAD_STAGE = "load200"
DEFAULT_ROUNDS = 5
SCRAPE_INTERVAL_SECONDS = 5.0
MAX_SAMPLE_GAP_SECONDS = 7.5
RUN_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{12}$")

# 严格按 data-model.md 的 required_series 清单；顺序也保持一致。
REQUIRED_SERIES = (
    "payment_hub_http_requests_total",
    "payment_hub_http_request_duration_seconds_bucket",
    "go_goroutines",
    "payment_hub_db_pool_connections_max",
    "payment_hub_db_pool_connections_in_use",
    "payment_hub_db_pool_acquires_total",
    "payment_hub_db_pool_acquire_duration_seconds_total",
    "payment_hub_db_pool_empty_acquires_total",
    "pg_up",
    "pg_stat_database_xact_commit",
    "pg_stat_database_blks_hit",
    "pg_stat_database_blks_read",
    "pg_benchmark_waiting_locks",
    "pg_stat_bgwriter_checkpoints_timed_total",
    "pg_stat_bgwriter_checkpoint_write_time_total",
)


class InputError(Exception):
    """输入文件或参数内容不符合约定。"""


class PrometheusError(Exception):
    """Prometheus 不可达、返回错误或响应无法解析。"""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="检查基准运行目录中的 Prometheus 指标证据。"
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="基准运行目录，必须包含 summary.json",
    )
    parser.add_argument(
        "--prometheus",
        default="http://127.0.0.1:9090",
        help="Prometheus 基础地址（默认 http://127.0.0.1:9090）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="单次 Prometheus 请求超时秒数，必须有限且大于 0（默认 10）",
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout 必须是有限且大于 0 的秒数")
    return args


def utc_iso(epoch):
    """把 Unix 时间戳格式化为 UTC 的 ISO 8601 秒级时间。"""
    moment = datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def format_epoch(epoch):
    """格式化 Prometheus HTTP API 使用的时间参数。"""
    return "%.3f" % float(epoch)


def parse_round_number(value):
    """解析正整数轮次；非法时返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    else:
        return None
    if number <= 0:
        return None
    return number


def parse_epoch_value(value):
    """解析有限数值 epoch；非法时返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def parse_window(stage):
    """从 load200 阶段读取有效的 [start, end] 窗口。"""
    start = parse_epoch_value(stage.get("k6_started_epoch"))
    end = parse_epoch_value(stage.get("k6_finished_epoch"))
    if start is None or end is None or end <= start:
        return None
    try:
        utc_iso(start)
        utc_iso(end)
    except (OverflowError, OSError, ValueError):
        return None
    return start, end


def load_json_object(path, label):
    """读取 JSON 对象；文件或 JSON 错误转换为 InputError。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        raise InputError("找不到%s：%s" % (label, path))
    except json.JSONDecodeError as exc:
        raise InputError("%s 不是有效 JSON：%s" % (label, exc))
    except OSError as exc:
        raise InputError("无法读取%s：%s" % (label, exc))
    if not isinstance(document, dict):
        raise InputError("%s 顶层必须是 JSON 对象" % label)
    return document


def load_summary(run_dir):
    return load_json_object(os.path.join(run_dir, "summary.json"), "summary.json")


def load_existing_evidence(run_dir):
    path = os.path.join(run_dir, "metrics-evidence.json")
    if not os.path.exists(path):
        return None
    return load_json_object(path, "已有的 metrics-evidence.json")


def extract_run_id(summary):
    run_id = summary.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.match(run_id):
        raise InputError(
            "summary.json 的 run_id 必须是 12 位十六进制字符串，实际为 %r"
            % (run_id,)
        )
    return run_id


def collect_stage_windows(summary, run_id):
    """收集并排序有效的 load200 阶段窗口，返回 (窗口列表, 整体缺失)。"""
    rounds = summary.get("rounds", [])
    if rounds is None:
        rounds = []
    if not isinstance(rounds, list):
        raise InputError("summary.json 的 rounds 必须是数组")

    windows = []
    valid_rounds = set()
    for index, record in enumerate(rounds):
        if not isinstance(record, dict):
            continue
        round_number = parse_round_number(record.get("round"))
        if round_number is None:
            round_number = index + 1
        round_id = record.get("round_id")
        if not isinstance(round_id, str) or not round_id.strip():
            round_id = "%sr%d" % (run_id, round_number)
        stages = record.get("stages", [])
        if stages is None:
            stages = []
        if not isinstance(stages, list):
            continue
        for stage in stages:
            if not isinstance(stage, dict) or stage.get("stage") != LOAD_STAGE:
                continue
            window = parse_window(stage)
            if window is None:
                continue
            start, end = window
            windows.append(
                {
                    "round": round_number,
                    "round_id": round_id,
                    "start_epoch": start,
                    "end_epoch": end,
                }
            )
            valid_rounds.add(round_number)

    windows.sort(key=lambda item: (item["round"], item["start_epoch"]))

    requested = parse_round_number(summary.get("rounds_requested"))
    if requested is None:
        requested = DEFAULT_ROUNDS
    missing = []
    if not windows:
        missing.append("summary.json 中没有有效的 load200 测量阶段")
    for round_number in range(1, requested + 1):
        if round_number not in valid_rounds:
            missing.append("第 %d 轮缺少有效的 load200 测量阶段" % round_number)
    return windows, missing


class PrometheusClient(object):
    """访问 Prometheus HTTP API 的最小客户端。"""

    def __init__(self, base_url, timeout):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, path, params):
        url = self.base_url + path + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "payment-hub-metrics-evidence/2",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace").strip()
            except Exception:
                detail = ""
            message = "Prometheus 返回 HTTP %s" % exc.code
            if detail:
                message += "：" + detail[:500]
            raise PrometheusError(message)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            raise PrometheusError(
                "无法访问 Prometheus %s：%s" % (self.base_url, reason)
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PrometheusError("Prometheus 响应不是有效 JSON：%s" % exc)
        if not isinstance(payload, dict):
            raise PrometheusError("Prometheus 响应顶层不是 JSON 对象")
        if payload.get("status") != "success":
            error_type = payload.get("errorType") or "error"
            error_message = payload.get("error") or "未知错误"
            raise PrometheusError(
                "Prometheus 返回错误（%s）：%s" % (error_type, error_message)
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise PrometheusError("Prometheus 响应缺少 data 对象")
        return data

    def _instant_result(self, expression, at_epoch):
        return self._request(
            "/api/v1/query",
            {"query": expression, "time": format_epoch(at_epoch)},
        )

    @staticmethod
    def _parse_sample_value(pair):
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            raise PrometheusError("Prometheus 返回的样本格式无效")
        try:
            number = float(pair[1])
        except (TypeError, ValueError):
            raise PrometheusError("Prometheus 返回的样本值不是数值")
        if not math.isfinite(number):
            raise PrometheusError("Prometheus 返回的样本值不是有限数值")
        return number

    def instant_values(self, expression, at_epoch):
        """返回即时向量/标量的所有数值；空结果返回空列表。"""
        data = self._instant_result(expression, at_epoch)
        result_type = data.get("resultType")
        result = data.get("result")
        if result_type == "vector":
            if not isinstance(result, list):
                raise PrometheusError("Prometheus 的 vector 结果不是数组")
            values = []
            for item in result:
                if not isinstance(item, dict):
                    raise PrometheusError("Prometheus 的 vector 样本格式无效")
                values.append(self._parse_sample_value(item.get("value")))
            return values
        if result_type == "scalar":
            return [self._parse_sample_value(result)]
        raise PrometheusError("不支持的即时查询结果类型：%r" % (result_type,))

    def has_series(self, expression, at_epoch):
        """在指定时刻查询序列名，非空结果即视为存在。"""
        data = self._instant_result(expression, at_epoch)
        result_type = data.get("resultType")
        result = data.get("result")
        if result_type == "vector":
            if not isinstance(result, list):
                raise PrometheusError("Prometheus 的 vector 结果不是数组")
            return bool(result)
        if result_type == "scalar":
            return True
        raise PrometheusError("不支持的即时查询结果类型：%r" % (result_type,))

    def range_series(self, expression, start_epoch, end_epoch):
        """按 5 秒步长查询窗口内的矩阵，返回 [(标签, 时间戳列表)]。"""
        data = self._request(
            "/api/v1/query_range",
            {
                "query": expression,
                "start": format_epoch(start_epoch),
                "end": format_epoch(end_epoch),
                "step": "5s",
            },
        )
        if data.get("resultType") != "matrix":
            raise PrometheusError(
                "不支持的区间查询结果类型：%r" % (data.get("resultType"),)
            )
        result = data.get("result")
        if not isinstance(result, list):
            raise PrometheusError("Prometheus 的 matrix 结果不是数组")
        series = []
        for item in result:
            if not isinstance(item, dict):
                raise PrometheusError("Prometheus 的 matrix 样本格式无效")
            metric = item.get("metric")
            if not isinstance(metric, dict):
                metric = {}
            values = item.get("values")
            if not isinstance(values, list):
                raise PrometheusError("Prometheus 的 matrix 样本缺少 values")
            timestamps = []
            for pair in values:
                if not isinstance(pair, (list, tuple)) or len(pair) < 1:
                    raise PrometheusError("Prometheus 的 matrix 样本格式无效")
                try:
                    timestamp = float(pair[0])
                except (TypeError, ValueError):
                    raise PrometheusError("Prometheus 的样本时间戳不是数值")
                if not math.isfinite(timestamp):
                    raise PrometheusError("Prometheus 的样本时间戳不是有限数值")
                timestamps.append(timestamp)
            series.append((metric, timestamps))
        return series


def sum_as_int(values):
    """把多个序列的计数求和并转为整数。"""
    if not values:
        return 0
    total = sum(values)
    if not math.isfinite(total) or total < 0:
        raise PrometheusError("Prometheus 返回的计数不是有效非负数值")
    return int(round(total))


def detect_gaps(label, series, start_epoch, end_epoch):
    """找出窗口内相邻样本间隔超过 7.5 秒的位置。"""
    messages = []
    for _metric, timestamps in series:
        ordered = sorted(
            {
                timestamp
                for timestamp in timestamps
                if start_epoch - 1e-9 <= timestamp <= end_epoch + 1e-9
            }
        )
        for previous, current in zip(ordered, ordered[1:]):
            gap = current - previous
            if gap > MAX_SAMPLE_GAP_SECONDS + 1e-9:
                messages.append(
                    "%s 窗口内相邻样本间隔 %.1fs（>7.5s）：%.3f -> %.3f"
                    % (label, gap, previous, current)
                )
    if len(messages) > 10:
        extra = len(messages) - 10
        messages = messages[:10]
        messages.append("%s 窗口内另有 %d 处采样缺口" % (label, extra))
    return messages


def target_state(observed, up_1):
    """根据抓取样本数和 up=1 样本数生成目标状态。"""
    if observed == 0:
        return "absent"
    if up_1 == observed:
        return "up"
    return "down"


def evaluate_stage(window, client):
    """查询一个测量阶段并返回证据记录。"""
    start = window["start_epoch"]
    end = window["end_epoch"]
    duration = end - start
    expected = max(
        0, int(math.floor(duration / SCRAPE_INTERVAL_SECONDS)) - 1
    )
    range_seconds = max(1, int(math.floor(duration)))

    observed = sum_as_int(
        client.instant_values(
            'count_over_time(up{job="payment-hub"}[%ds])' % range_seconds, end
        )
    )
    up_1 = sum_as_int(
        client.instant_values(
            'sum_over_time((up{job="payment-hub"} == 1)[%ds:5s])'
            % range_seconds,
            end,
        )
    )
    exporter_observed = sum_as_int(
        client.instant_values(
            'count_over_time(up{job="postgres"}[%ds])' % range_seconds, end
        )
    )
    exporter_up_1 = sum_as_int(
        client.instant_values(
            'sum_over_time((up{job="postgres"} == 1)[%ds:5s])'
            % range_seconds,
            end,
        )
    )
    pg_observed = sum_as_int(
        client.instant_values(
            "count_over_time(pg_up[%ds])" % range_seconds, end
        )
    )
    pg_up_1 = sum_as_int(
        client.instant_values(
            "sum_over_time((pg_up == 1)[%ds:5s])" % range_seconds, end
        )
    )

    required_series = {}
    for name in REQUIRED_SERIES:
        required_series[name] = (
            "present" if client.has_series(name, end) else "absent"
        )

    missing = []
    if observed < expected:
        missing.append(
            "payment-hub 抓取次数不足：observed=%d < expected=%d"
            % (observed, expected)
        )
    if up_1 < expected:
        message = (
            "payment-hub 成功抓取次数不足：up_1=%d < expected=%d"
            % (up_1, expected)
        )
        if up_1 == 0:
            message += "（窗口内 up=0）"
        missing.append(message)
    elif up_1 < observed:
        missing.append(
            "payment-hub 存在 up=0 的抓取：up_1=%d < observed=%d"
            % (up_1, observed)
        )

    if exporter_observed < expected:
        missing.append(
            "postgres exporter 抓取次数不足：exporter_observed=%d < expected=%d"
            % (exporter_observed, expected)
        )
    if exporter_up_1 < expected:
        message = (
            "postgres exporter 失败：exporter_up_1=%d < expected=%d"
            % (exporter_up_1, expected)
        )
        if exporter_up_1 == 0:
            message += "（窗口内 up=0）"
        missing.append(message)
    if exporter_up_1 < exporter_observed:
        missing.append(
            "postgres exporter 存在 up=0 的抓取："
            "exporter_up_1=%d < exporter_observed=%d"
            % (exporter_up_1, exporter_observed)
        )

    if pg_observed < expected:
        missing.append(
            "postgres 抓取次数不足：pg_up 样本=%d < expected=%d"
            % (pg_observed, expected)
        )
    if pg_up_1 < expected:
        message = "pg_up 抓取失败：pg_up_1=%d < expected=%d" % (
            pg_up_1,
            expected,
        )
        if pg_up_1 == 0:
            message += "（窗口内 pg_up=0）"
        missing.append(message)
    elif pg_up_1 < pg_observed:
        missing.append(
            "pg_up 存在为 0 的抓取：pg_up_1=%d < pg_observed=%d"
            % (pg_up_1, pg_observed)
        )

    for name, state in required_series.items():
        if state == "absent":
            missing.append("关键序列缺失：%s" % name)

    for expression, label in (
        ('up{job="payment-hub"}', "payment-hub"),
        ('up{job="postgres"}', "postgres exporter"),
        ("pg_up", "pg_up"),
    ):
        series = client.range_series(expression, start, end)
        missing.extend(detect_gaps(label, series, start, end))

    status = "complete" if not missing else "incomplete"
    return {
        "round": window["round"],
        "round_id": window["round_id"],
        "window": {
            "start_utc": utc_iso(start),
            "end_utc": utc_iso(end),
        },
        "targets": {
            "payment-hub": target_state(observed, up_1),
            "postgres": target_state(exporter_observed, exporter_up_1),
            "pg_up": target_state(pg_observed, pg_up_1),
        },
        "scrapes": {
            "expected": expected,
            "observed": observed,
            "up_1": up_1,
            "exporter_observed": exporter_observed,
            "exporter_up_1": exporter_up_1,
            "pg_up_1": pg_up_1,
        },
        "required_series": required_series,
        "status": status,
        "missing": missing,
    }


def compose_evidence(
    run_id,
    run_dir_name,
    prometheus_url,
    stage_results,
    global_missing,
    archive_error,
    archive_error_present,
    prometheus_error,
):
    """组装顶层证据；整体 complete 要求所有阶段 complete 且无缺失。"""
    missing = list(global_missing)
    for stage in stage_results:
        for reason in stage["missing"]:
            missing.append("第 %d 轮：%s" % (stage["round"], reason))
    if prometheus_error:
        missing.insert(0, prometheus_error)
    if archive_error_present:
        message = "已有 metrics-evidence.json 含 archive_error，指标证据强制为 incomplete"
        if message not in missing:
            missing.append(message)

    complete = bool(stage_results) and not missing
    if any(stage["status"] != "complete" for stage in stage_results):
        complete = False
    if prometheus_error or archive_error_present:
        complete = False

    evidence = {
        "schema": SCHEMA,
        "run_id": run_id,
        "run_dir": run_dir_name,
        "prometheus": prometheus_url,
        "stages": stage_results,
        "status": "complete" if complete else "incomplete",
        "missing": missing,
        "checked_at_utc": utc_iso(time.time()),
    }
    if prometheus_error:
        evidence["error"] = prometheus_error
    if archive_error_present:
        evidence["archive_error"] = archive_error
    return evidence


def write_json_atomic(path, document):
    """先写同目录临时文件并 fsync，再用 os.replace 原子替换目标文件。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, temp_path = tempfile.mkstemp(
        prefix=".metrics-evidence-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def main(argv=None):
    args = parse_args(argv)
    run_dir = os.path.abspath(args.run_dir)
    try:
        if not os.path.isdir(run_dir):
            raise InputError("运行目录不存在或不是目录：%s" % run_dir)
        summary = load_summary(run_dir)
        run_id = extract_run_id(summary)
        existing = load_existing_evidence(run_dir)
        archive_error_present = (
            existing is not None and "archive_error" in existing
        )
        archive_error = (
            existing.get("archive_error") if archive_error_present else None
        )

        windows, global_missing = collect_stage_windows(summary, run_id)
        client = PrometheusClient(args.prometheus, args.timeout)
        stage_results = []
        prometheus_error = None
        for window in windows:
            try:
                stage_results.append(evaluate_stage(window, client))
            except PrometheusError as exc:
                prometheus_error = "Prometheus 查询失败：%s" % exc
                break

        evidence = compose_evidence(
            run_id=run_id,
            run_dir_name=os.path.basename(os.path.normpath(run_dir)),
            prometheus_url=args.prometheus,
            stage_results=stage_results,
            global_missing=global_missing,
            archive_error=archive_error,
            archive_error_present=archive_error_present,
            prometheus_error=prometheus_error,
        )
        evidence_path = os.path.join(run_dir, "metrics-evidence.json")
        write_json_atomic(evidence_path, evidence)
    except InputError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 2
    except (OSError, TypeError, ValueError) as exc:
        print("错误：文件操作失败：%s" % exc, file=sys.stderr)
        return 2

    print("指标证据：%s" % evidence_path)
    print("整体状态：%s" % evidence["status"])
    if prometheus_error:
        print("错误：%s" % prometheus_error, file=sys.stderr)
    for reason in evidence["missing"]:
        print("缺失：%s" % reason)

    if prometheus_error:
        return 4
    if evidence["status"] == "complete":
        return 0
    return 3


if __name__ == "__main__":
    sys.exit(main())
