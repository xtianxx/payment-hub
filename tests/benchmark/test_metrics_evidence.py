"""check_metrics_evidence.py 的单元测试。

测试使用标准库 threading + http.server 启动本地假 Prometheus，不依赖网络、
Docker 或真实 Prometheus。覆盖完整证据、缺阶段、应用与 exporter 的 up=0、
pg_up=0、抓取次数不足、基于原始抓取的窗口缺口、窗口前旧样本、单阶段查询
失败后继续、Prometheus 不可达、archive_error 并发保留、URL 脱敏、超范围
epoch、原子写入和退出码 0/2/3/4。
"""

import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent / "check_metrics_evidence.py"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_metrics_evidence  # noqa: E402

RUN_ID = "a1b2c3d4e5f6"
REQUIRED_SERIES = check_metrics_evidence.REQUIRED_SERIES


def make_summary(
    rounds=1,
    requested=None,
    duration=120.0,
    missing_load_rounds=(),
    run_id=RUN_ID,
    start=1700000000.0,
):
    """构造最小 summary.json，只保留与指标证据相关的字段。"""
    if requested is None:
        requested = rounds
    round_records = []
    for index in range(1, rounds + 1):
        stages = [
            {
                "stage": "smoke",
                "k6_started_epoch": start + index * 1000,
                "k6_finished_epoch": start + index * 1000 + 10,
            },
            {
                "stage": "warmup",
                "k6_started_epoch": start + index * 1000 + 20,
                "k6_finished_epoch": start + index * 1000 + 50,
            },
        ]
        if index in missing_load_rounds:
            stages.append(
                {
                    "stage": "load200",
                    "not_run": True,
                    "reason": "测试中中断",
                }
            )
        else:
            stage_start = start + (index - 1) * 200
            stages.append(
                {
                    "stage": "load200",
                    "k6_started_epoch": stage_start,
                    "k6_finished_epoch": stage_start + duration,
                }
            )
        round_records.append(
            {
                "round": index,
                "round_id": "%sr%d" % (run_id, index),
                "stages": stages,
            }
        )
    return {
        "schema_version": 3,
        "kind": "payment_create_benchmark",
        "run_id": run_id,
        "rounds_requested": requested,
        "rounds": round_records,
    }


def load_stage(round_record):
    for stage in round_record["stages"]:
        if stage.get("stage") == "load200":
            return stage
    raise AssertionError("round has no load200 stage")


def sample_timestamps(start, end, gap_after=None):
    """生成假原始抓取时间；gap_after 后跳过一次抓取，形成 10 秒间隔。"""
    timestamps = []
    current = float(start)
    skipped = False
    while current <= end + 1e-9:
        if (
            gap_after is not None
            and not skipped
            and current > start + gap_after + 1e-9
        ):
            skipped = True
            current += 5.0
            continue
        timestamps.append(round(current, 3))
        current += 5.0
    return timestamps


def step_to_seconds(step):
    text = str(step)
    if text.endswith("s"):
        return float(text[:-1])
    return float(text)


class FakePrometheusState(object):
    """可配置的假 Prometheus 查询结果。"""

    def __init__(
        self,
        up_observed=23,
        up_1=23,
        exporter_observed=23,
        exporter_up_1=23,
        pg_observed=23,
        pg_up_1=23,
        missing_series=(),
        stale_series=(),
        up_gap_after=None,
        exporter_gap_after=None,
        pg_gap_after=None,
        fail_once_query=None,
    ):
        self.up_observed = int(up_observed)
        self.up_1 = int(up_1)
        self.exporter_observed = int(exporter_observed)
        self.exporter_up_1 = int(exporter_up_1)
        self.pg_observed = int(pg_observed)
        self.pg_up_1 = int(pg_up_1)
        self.stale_series = set(stale_series)
        self.present_series = (
            set(REQUIRED_SERIES)
            - set(missing_series)
            - set(stale_series)
        )
        self.up_gap_after = up_gap_after
        self.exporter_gap_after = exporter_gap_after
        self.pg_gap_after = pg_gap_after
        self.fail_once_query = fail_once_query
        self.failed_once = False
        self.requests = []

    @staticmethod
    def _vector(value, at_epoch):
        if value is None:
            return {"resultType": "vector", "result": []}
        return {
            "resultType": "vector",
            "result": [
                {
                    "metric": {},
                    "value": [float(at_epoch), str(value)],
                }
            ],
        }

    def instant(self, query, at_epoch):
        self.requests.append(("query", query))
        text = query.strip()
        if (
            self.fail_once_query is not None
            and not self.failed_once
            and text == self.fail_once_query
        ):
            self.failed_once = True
            raise RuntimeError("模拟查询失败")

        prefix = "count_over_time("
        suffix = ") > 0"
        if text.startswith(prefix) and text.endswith(suffix):
            inner = text[len(prefix):-len(suffix)]
            name = inner.split("[", 1)[0]
            if name in self.present_series:
                return self._vector(1, at_epoch)
            return self._vector(None, at_epoch)

        if text.startswith('count_over_time(up{job="payment-hub"}'):
            return self._vector(self.up_observed, at_epoch)
        if text.startswith('sum_over_time(up{job="payment-hub"}'):
            return self._vector(self.up_1, at_epoch)
        if text.startswith('count_over_time(up{job="postgres"}'):
            return self._vector(self.exporter_observed, at_epoch)
        if text.startswith('sum_over_time(up{job="postgres"}'):
            return self._vector(self.exporter_up_1, at_epoch)
        if text.startswith("count_over_time(pg_up"):
            return self._vector(self.pg_observed, at_epoch)
        if text.startswith("sum_over_time(pg_up"):
            return self._vector(self.pg_up_1, at_epoch)
        if text in self.present_series or text in self.stale_series:
            return self._vector(1, at_epoch)
        return self._vector(None, at_epoch)

    def range(self, query, start, end, step):
        self.requests.append(("range", query, start, end, step))
        gaps = {
            'absent_over_time(up{job="payment-hub"}[7500ms])':
                self.up_gap_after,
            'absent_over_time(up{job="postgres"}[7500ms])':
                self.exporter_gap_after,
            "absent_over_time(pg_up[7500ms])": self.pg_gap_after,
        }
        if query not in gaps:
            return {"resultType": "matrix", "result": []}

        raw_start = start - check_metrics_evidence.MAX_SAMPLE_GAP_SECONDS
        if (
            (query.startswith("absent_over_time(up{job=\"payment-hub\"}")
             and self.up_observed == 0)
            or (query.startswith("absent_over_time(up{job=\"postgres\"}")
                and self.exporter_observed == 0)
            or (query.startswith("absent_over_time(pg_up")
                and self.pg_observed == 0)
        ):
            raw_times = []
        else:
            raw_times = sample_timestamps(raw_start, end, gaps[query])
        step_seconds = step_to_seconds(step)
        count = int(math.floor((end - start) / step_seconds + 1e-9)) + 1
        values = []
        for index in range(max(0, count)):
            timestamp = round(start + index * step_seconds, 3)
            has_sample = any(
                timestamp - 7.5 + 1e-9 < raw <= timestamp + 1e-9
                for raw in raw_times
            )
            if not has_sample:
                # 真实 absent_over_time 只在“无样本”的评估点返回值 1，
                # 有样本的点直接省略，不返回 0。
                values.append([timestamp, "1"])
        if not values:
            return {"resultType": "matrix", "result": []}
        return {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"__name__": query},
                    "values": values,
                }
            ],
        }


class FakePrometheusHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        state = self.server.state
        try:
            if parsed.path == "/api/v1/query":
                query = params.get("query", [""])[0]
                at_epoch = float(params.get("time", ["0"])[0])
                data = state.instant(query, at_epoch)
            elif parsed.path == "/api/v1/query_range":
                query = params.get("query", [""])[0]
                start = float(params.get("start", ["0"])[0])
                end = float(params.get("end", ["0"])[0])
                step = params.get("step", ["1s"])[0]
                data = state.range(query, start, end, step)
            else:
                self.send_error(404)
                return
        except Exception as exc:  # 测试辅助服务器不隐藏内部错误
            self._send_json(
                500,
                {
                    "status": "error",
                    "errorType": "internal",
                    "error": str(exc),
                },
            )
            return
        self._send_json(200, {"status": "success", "data": data})


@contextmanager
def fake_prometheus(**kwargs):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakePrometheusHandler)
    server.state = FakePrometheusState(**kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], server.state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class MetricsEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="metrics-evidence-test-"
        )
        self.addCleanup(self.temp_dir.cleanup)
        self.run_dir = (
            Path(self.temp_dir.name) / ("20261126T090000Z-" + RUN_ID)
        )
        self.run_dir.mkdir()

    def write_summary(self, **kwargs):
        return self.write_summary_document(make_summary(**kwargs))

    def write_summary_document(self, document):
        path = self.run_dir / "summary.json"
        path.write_text(
            json.dumps(document, indent=2),
            encoding="utf-8",
        )
        return path

    def write_evidence(self, document):
        path = self.run_dir / "metrics-evidence.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def unreachable_url(self):
        """绑定但不监听的本机端口；连接会被拒绝，且端口在用例结束前不会被抢占。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self.addCleanup(sock.close)
        return "http://127.0.0.1:%d" % sock.getsockname()[1]

    def read_evidence(self):
        return json.loads(
            (self.run_dir / "metrics-evidence.json").read_text(
                encoding="utf-8"
            )
        )

    def run_cli(self, prometheus, timeout="2"):
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPT),
                "--run-dir",
                str(self.run_dir),
                "--prometheus",
                prometheus,
                "--timeout",
                str(timeout),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def test_help_exits_zero_and_shows_usage(self):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage", (result.stdout + result.stderr).lower())

    def test_missing_required_run_dir_exits_two(self):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_complete_run_writes_complete_evidence(self):
        self.write_summary(rounds=5, requested=5)
        with fake_prometheus() as (base_url, state):
            result = self.run_cli(base_url)
        self.assertEqual(result.returncode, 0, result.stderr)

        evidence = self.read_evidence()
        self.assertEqual(
            evidence["schema"], "payment-hub.metrics-evidence.v2"
        )
        self.assertEqual(evidence["run_id"], RUN_ID)
        self.assertEqual(evidence["run_dir"], self.run_dir.name)
        self.assertEqual(evidence["prometheus"], base_url)
        self.assertEqual(evidence["status"], "complete")
        self.assertEqual(evidence["missing"], [])
        self.assertTrue(evidence["checked_at_utc"].endswith("Z"))
        self.assertEqual(
            [stage["round"] for stage in evidence["stages"]],
            [1, 2, 3, 4, 5],
        )
        self.assertEqual(
            [stage["round_id"] for stage in evidence["stages"]],
            ["%sr%d" % (RUN_ID, index) for index in range(1, 6)],
        )
        for stage in evidence["stages"]:
            self.assertEqual(stage["status"], "complete")
            self.assertEqual(stage["missing"], [])
            self.assertEqual(
                stage["window"]["start_utc"],
                check_metrics_evidence.utc_iso(
                    1700000000.0 + (stage["round"] - 1) * 200
                ),
            )
            self.assertEqual(
                stage["window"]["end_utc"],
                check_metrics_evidence.utc_iso(
                    1700000000.0 + (stage["round"] - 1) * 200 + 120
                ),
            )
            self.assertEqual(
                stage["targets"],
                {"payment-hub": "up", "postgres": "up", "pg_up": "up"},
            )
            self.assertEqual(
                stage["scrapes"],
                {
                    "expected": 23,
                    "observed": 23,
                    "up_1": 23,
                    "exporter_observed": 23,
                    "exporter_up_1": 23,
                    "pg_up_1": 23,
                },
            )
            self.assertEqual(len(stage["required_series"]), 15)
            self.assertEqual(
                set(stage["required_series"]), set(REQUIRED_SERIES)
            )
            self.assertTrue(
                all(
                    state_value == "present"
                    for state_value in stage["required_series"].values()
                )
            )
        self.assertTrue(any(kind == "range" for kind, *_ in state.requests))

    def test_success_counts_use_raw_samples_not_subquery(self):
        self.write_summary(rounds=1, requested=1)
        with fake_prometheus() as (base_url, state):
            result = self.run_cli(base_url)
        self.assertEqual(result.returncode, 0, result.stderr)

        stage = self.read_evidence()["stages"][0]
        self.assertEqual(stage["scrapes"]["up_1"], stage["scrapes"]["observed"])
        self.assertEqual(
            stage["scrapes"]["exporter_up_1"],
            stage["scrapes"]["exporter_observed"],
        )
        queries = [
            request[1]
            for request in state.requests
            if request[0] == "query"
        ]
        self.assertIn('sum_over_time(up{job="payment-hub"}[120s])', queries)
        self.assertIn('sum_over_time(up{job="postgres"}[120s])', queries)
        self.assertIn("sum_over_time(pg_up[120s])", queries)
        self.assertFalse(
            any(
                ":5s" in query
                for query in queries
                if query.startswith("sum_over_time")
            )
        )

    def test_missing_stage_marks_overall_incomplete(self):
        self.write_summary(
            rounds=5, requested=5, missing_load_rounds=(3,)
        )
        with fake_prometheus() as (base_url, _state):
            result = self.run_cli(base_url)
        self.assertEqual(result.returncode, 3, result.stderr)

        evidence = self.read_evidence()
        self.assertEqual(evidence["status"], "incomplete")
        self.assertEqual(
            [stage["round"] for stage in evidence["stages"]],
            [1, 2, 4, 5],
        )
        self.assertTrue(
            any("第 3 轮" in reason for reason in evidence["missing"])
        )
        self.assertTrue(
            all(
                stage["status"] == "complete"
                for stage in evidence["stages"]
            )
        )

    def test_no_valid_stage_writes_incomplete_evidence(self):
        self.write_summary(
            rounds=5, requested=5, missing_load_rounds=(1, 2, 3, 4, 5)
        )
        result = self.run_cli(
            self.unreachable_url(), timeout="1"
        )
        self.assertEqual(result.returncode, 3, result.stderr)

        evidence = self.read_evidence()
        self.assertEqual(evidence["status"], "incomplete")
        self.assertEqual(evidence["stages"], [])
        self.assertTrue(
            any(
                "没有有效的 load200" in reason
                for reason in evidence["missing"]
            )
        )

    def test_up_zero_marks_stage_incomplete(self):
        self.write_summary(rounds=1, requested=1)
        with fake_prometheus(up_1=0) as (base_url, _state):
            result = self.run_cli(base_url)
        self.assertEqual(result.returncode, 3, result.stderr)

        evidence = self.read_evidence()
        stage = evidence["stages"][0]
        self.assertEqual(stage["status"], "incomplete")
        self.assertEqual(stage["scrapes"]["observed"], 23)
        self.assertEqual(stage["scrapes"]["up_1"], 0)
        self.assertEqual(stage["targets"]["payment-hub"], "down")
        self.assertTrue(
            any("up=0" in reason for reason in stage["missing"]),
            stage["missing"],
        )
        self.assertEqual(evidence["status"], "incomplete")

    def test_pg_up_zero_marks_stage_incomplete(self):
        self.write_summary(rounds=1, requested=1)
        with fake_prometheus(pg_up_1=0) as (base_url, _state):
            result = self.run_cli(base_url)
        self.assertEqual(result.returncode, 3, result.stderr)

        evidence = self.read_evidence()
        stage = evidence["stages"][0]
        self.assertEqual(stage["status"], "incomplete")
        self.assertEqual(stage["scrapes"]["pg_up_1"], 0)
        self.assertEqual(stage["targets"]["postgres"], "up")
        self.assertEqual(stage["targets"]["pg_up"], "down")
        self.assertTrue(
            any("pg_up=0" in reason for reason in stage["missing"]),
            stage["missing"],
        )

    def test_postgres_exporter_down_marks_stage_incomplete(self):
        self.write_summary(rounds=1, requested=1)
        with fake_prometheus(exporter_observed=23, exporter_up_1=0) as (
            base_url,
            _state,
        ):
            result = self.run_cli(base_url)
        self.assertEqual(result.returncode, 3, result.stderr)

        evidence = self.read_evidence()
        stage = evidence["stages"][0]
        self.assertEqual(stage["status"], "incomplete")
        self.assertEqual(stage["scrapes"]["exporter_observed"], 23)
        self.assertEqual(stage["scrapes"]["exporter_up_1"], 0)
        self.assertEqual(stage["targets"]["postgres"], "down")
        self.assertEqual(stage["targets"]["pg_up"], "up")
        self.assertTrue(
            any(
                "exporter 失败" in reason
                for reason in stage["missing"]
            ),
            stage["missing"],
        )

    def test_postgres_exporter_absent_marks_stage_incomplete(self):
        self.write_summary(rounds=1, requested=1)
        with fake_prometheus(exporter_observed=0, exporter_up_1=0) as (
            base_url,
            _state,
        ):
            result = self.run_cli(base_url)
        self.assertEqual(result.returncode, 3, result.stderr)

        evidence = self.read_evidence()
        stage = evidence["stages"][0]
        self.assertEqual(stage["status"], "incomplete")
        self.assertEqual(stage["scrapes"]["exporter_observed"], 0)
        self.assertEqual(stage["scrapes"]["exporter_up_1"], 0)
        self.assertEqual(stage["targets"]["postgres"], "absent")
        self.assertEqual(stage["targets"]["pg_up"], "up")
        self.assertTrue(
            any("exporter" in reason for reason in stage["missing"]),
            stage["missing"],
        )
        self.assertTrue(
            any(
                "absent_over_time 为 1" in reason
                for reason in stage["missing"]
            ),
            stage["missing"],
        )

    def test_target_absent_is_incomplete_even_when_expected_is_zero(self):
        self.write_summary(rounds=1, requested=1, duration=1.0)
        with fake_prometheus(up_observed=0, up_1=0) as (
            base_url,
            _state,
        ):
            result = self.run_cli(base_url)
        self.assertEqual(result.returncode, 3, result.stderr)

        evidence = self.read_evidence()
        stage = evidence["stages"][0]
        self.assertEqual(stage["scrapes"]["expected"], 0)
        self.assertEqual(stage["targets"]["payment-hub"], "absent")
        self.assertEqual(stage["status"], "incomplete")
        self.assertTrue(
            any(
                "payment-hub 窗口内没有抓取样本" in reason
                for reason in stage["missing"]
            ),
            stage["missing"],
        )

    def test_stale_series_before_window_is_not_present(self):
        self.write_summary(rounds=1, requested=1)
        with fake_prometheus(stale_series=("go_goroutines",)) as (
            base_url,
            state,
        ):
            result = self.run_cli(base_url)
        self.assertEqual(result.returncode, 3, result.stderr)

        evidence = self.read_evidence()
        stage = evidence["stages"][0]
        self.assertEqual(
            stage["required_series"]["go_goroutines"], "absent"
        )
        self.assertTrue(
            any(
                "go_goroutines" in reason for reason in stage["missing"]
            ),
            stage["missing"],
        )
        queries = [
            request[1]
            for request in state.requests
            if request[0] == "query"
        ]
        self.assertNotIn("go_goroutines", queries)
        self.assertIn(
            "count_over_time(go_goroutines[120s]) > 0", queries
        )

    def test_insufficient_scrapes_marks_stage_incomplete(self):
        self.write_summary(rounds=1, requested=1)
        with fake_prometheus(up_observed=20, up_1=20) as (
            base_url,
            _state,
        ):
            result = self.run_cli(base_url)
        self.assertEqual(result.returncode, 3, result.stderr)

        evidence = self.read_evidence()
        stage = evidence["stages"][0]
        self.assertEqual(stage["scrapes"]["expected"], 23)
        self.assertEqual(stage["scrapes"]["observed"], 20)
        self.assertTrue(
            any("抓取次数不足" in reason for reason in stage["missing"]),
            stage["missing"],
        )

    def test_missing_one_scrape_incomplete_even_if_observed_meets_expected(
        self,
    ):
        self.write_summary(rounds=1, requested=1)
        with fake_prometheus(up_gap_after=55) as (base_url, state):
            result = self.run_cli(base_url)
        self.assertEqual(result.returncode, 3, result.stderr)

        evidence = self.read_evidence()
        stage = evidence["stages"][0]
        self.assertGreaterEqual(
            stage["scrapes"]["observed"], stage["scrapes"]["expected"]
        )
        self.assertEqual(stage["status"], "incomplete")
        self.assertTrue(
            any(
                "7.5" in reason and "absent_over_time 为 1" in reason
                for reason in stage["missing"]
            ),
            stage["missing"],
        )
        queries = [str(request[1]) for request in state.requests]
        self.assertTrue(
            any("[7500ms]" in query for query in queries), queries
        )
        self.assertTrue(
            any(
                query.startswith("absent_over_time(")
                for query in queries
            ),
            queries,
        )
        self.assertFalse(
            any(
                query.startswith("count_over_time(")
                and "[7500ms]" in query
                for query in queries
            ),
            queries,
        )
        self.assertFalse(
            any("7.5s" in query for query in queries), queries
        )
        decimal_seconds = re.compile(r"\[\d+\.\d+s\]")
        self.assertFalse(
            any(decimal_seconds.search(query) for query in queries),
            queries,
        )

    def test_postgres_exporter_gap_marks_stage_incomplete(self):
        self.write_summary(rounds=1, requested=1)
        with fake_prometheus(exporter_gap_after=55) as (
            base_url,
            state,
        ):
            result = self.run_cli(base_url)
        self.assertEqual(result.returncode, 3, result.stderr)

        evidence = self.read_evidence()
        stage = evidence["stages"][0]
        self.assertEqual(stage["status"], "incomplete")
        self.assertEqual(stage["targets"]["postgres"], "up")
        self.assertTrue(
            any(
                "postgres exporter" in reason
                and "7.5" in reason
                and "absent_over_time 为 1" in reason
                for reason in stage["missing"]
            ),
            stage["missing"],
        )
        self.assertTrue(
            any(
                str(request[1]).startswith(
                    'absent_over_time(up{job="postgres"}'
                )
                for request in state.requests
            )
        )

    def test_single_stage_failure_keeps_later_stages(self):
        self.write_summary(rounds=2, requested=2)
        with fake_prometheus(
            fail_once_query='count_over_time(up{job="payment-hub"}[120s])'
        ) as (base_url, _state):
            result = self.run_cli(base_url)
        self.assertEqual(result.returncode, 4, result.stderr)

        evidence = self.read_evidence()
        self.assertEqual(evidence["status"], "incomplete")
        self.assertTrue(evidence.get("error"), evidence)
        self.assertEqual(
            [stage["round"] for stage in evidence["stages"]],
            [1, 2],
        )
        first, second = evidence["stages"]
        self.assertEqual(first["status"], "incomplete")
        self.assertTrue(first["error"])
        self.assertTrue(first["missing"])
        self.assertEqual(second["status"], "complete")
        self.assertNotIn("error", second)

    def test_prometheus_unreachable_still_writes_evidence(self):
        self.write_summary(rounds=1, requested=1)
        result = self.run_cli(
            self.unreachable_url(), timeout="1"
        )
        self.assertEqual(result.returncode, 4, result.stderr)

        evidence = self.read_evidence()
        self.assertEqual(evidence["status"], "incomplete")
        self.assertTrue(evidence.get("error"), evidence)
        self.assertTrue(evidence["missing"], evidence)
        self.assertEqual(len(evidence["stages"]), 1)
        stage = evidence["stages"][0]
        self.assertEqual(stage["status"], "incomplete")
        self.assertTrue(stage["error"])
        self.assertTrue(stage["missing"])
        self.assertTrue(evidence["checked_at_utc"].endswith("Z"))

    def test_archive_error_is_preserved_and_forces_incomplete(self):
        self.write_summary(rounds=1, requested=1)
        self.write_evidence(
            {
                "schema": "payment-hub.metrics-evidence.v2",
                "archive_error": "归档复制失败",
            }
        )
        with fake_prometheus() as (base_url, _state):
            result = self.run_cli(base_url)
        self.assertEqual(result.returncode, 3, result.stderr)

        evidence = self.read_evidence()
        self.assertEqual(evidence["archive_error"], "归档复制失败")
        self.assertEqual(evidence["status"], "incomplete")
        self.assertEqual(evidence["stages"][0]["status"], "complete")
        self.assertTrue(
            any(
                "archive_error" in reason
                for reason in evidence["missing"]
            )
        )

    def test_prometheus_url_userinfo_is_redacted(self):
        cases = [
            (
                "http://user:secret@127.0.0.1:9090",
                "http://127.0.0.1:9090",
            ),
            (
                "https://user@example.com/prom",
                "https://example.com/prom",
            ),
            (
                "http://user:p%40ss@[::1]:9090/path",
                "http://[::1]:9090/path",
            ),
            (
                "http://127.0.0.1:9090",
                "http://127.0.0.1:9090",
            ),
        ]
        for raw, expected in cases:
            self.assertEqual(
                check_metrics_evidence.redact_prometheus_url(raw),
                expected,
            )

    def test_invalid_prometheus_url_is_replaced_with_placeholder(self):
        redacted = check_metrics_evidence.redact_prometheus_url(
            "http://user:pw@[::1"
        )
        self.assertEqual(redacted, "<invalid-url>")
        self.assertNotIn("user", redacted)
        self.assertNotIn("pw", redacted)

    def test_out_of_range_epoch_is_skipped_without_crash(self):
        summary = make_summary(rounds=3, requested=3)
        too_large = 10 ** 400
        first = load_stage(summary["rounds"][0])
        first["k6_started_epoch"] = too_large
        first["k6_finished_epoch"] = too_large + 120
        second = load_stage(summary["rounds"][1])
        second["k6_started_epoch"] = 1e300
        second["k6_finished_epoch"] = 2e300
        self.write_summary_document(summary)

        with fake_prometheus() as (base_url, _state):
            result = self.run_cli(base_url)
        self.assertEqual(result.returncode, 3, result.stderr)

        evidence = self.read_evidence()
        self.assertEqual(evidence["status"], "incomplete")
        self.assertEqual(
            [stage["round"] for stage in evidence["stages"]],
            [3],
        )
        self.assertEqual(evidence["stages"][0]["status"], "complete")
        self.assertTrue(
            any("第 1 轮" in reason for reason in evidence["missing"])
        )
        self.assertTrue(
            any("第 2 轮" in reason for reason in evidence["missing"])
        )

    def test_invalid_timeout_exits_two(self):
        self.write_summary(rounds=1, requested=1)
        for timeout in ("0", "-2", "nan", "inf"):
            result = self.run_cli(
                self.unreachable_url(), timeout=timeout
            )
            self.assertEqual(
                result.returncode,
                2,
                "timeout=%s stderr=%s" % (timeout, result.stderr),
            )
            self.assertFalse(
                (self.run_dir / "metrics-evidence.json").exists()
            )

    def test_invalid_run_id_exits_two(self):
        self.write_summary(rounds=1, requested=1, run_id="not-a-run-id")
        result = self.run_cli(
            self.unreachable_url(), timeout="1"
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.run_dir / "metrics-evidence.json").exists())

    def test_invalid_summary_json_exits_two(self):
        (self.run_dir / "summary.json").write_text(
            "{not json", encoding="utf-8"
        )
        result = self.run_cli(
            self.unreachable_url(), timeout="1"
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.run_dir / "metrics-evidence.json").exists())


class EvidenceLockTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="metrics-evidence-lock-"
        )
        self.addCleanup(self.temp_dir.cleanup)
        self.run_dir = Path(self.temp_dir.name) / "run"
        self.run_dir.mkdir()

    def test_concurrent_writes_preserve_archive_error_and_lock_mode(self):
        evidence_path = self.run_dir / "metrics-evidence.json"
        evidence_path.write_text(
            json.dumps(
                {
                    "schema": "payment-hub.metrics-evidence.v2",
                    "archive_error": "归档复制失败",
                }
            ),
            encoding="utf-8",
        )
        barrier = threading.Barrier(2)
        errors = []

        def build_document():
            return {
                "schema": "payment-hub.metrics-evidence.v2",
                "run_id": RUN_ID,
                "run_dir": self.run_dir.name,
                "prometheus": "http://127.0.0.1:9090",
                "stages": [],
                "status": "complete",
                "missing": [],
                "checked_at_utc": "2026-11-26T09:22:00Z",
            }

        def worker():
            try:
                barrier.wait(timeout=5)
                check_metrics_evidence.write_evidence_locked(
                    str(self.run_dir), build_document
                )
            except BaseException as exc:  # 线程中的失败要回传给主线程
                errors.append(exc)

        threads = [
            threading.Thread(target=worker),
            threading.Thread(target=worker),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])

        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        self.assertEqual(evidence["archive_error"], "归档复制失败")
        self.assertEqual(evidence["status"], "incomplete")
        self.assertTrue(
            any(
                "archive_error" in reason
                for reason in evidence["missing"]
            )
        )
        lock_path = self.run_dir / ".metrics-evidence.lock"
        self.assertTrue(lock_path.exists())
        self.assertEqual(os.stat(str(lock_path)).st_mode & 0o777, 0o600)


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="metrics-evidence-atomic-"
        )
        self.addCleanup(self.temp_dir.cleanup)
        self.directory = Path(self.temp_dir.name)

    def leftover_temp_files(self):
        return [
            path.name
            for path in self.directory.iterdir()
            if path.name.startswith(".metrics-evidence-")
        ]

    def test_replace_failure_keeps_old_target(self):
        target = self.directory / "metrics-evidence.json"
        target.write_text('{"old": true}\n', encoding="utf-8")
        with mock.patch(
            "check_metrics_evidence.os.replace",
            side_effect=OSError("replace failed"),
        ):
            with self.assertRaises(OSError):
                check_metrics_evidence.write_json_atomic(
                    str(target), {"new": True}
                )
        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8")),
            {"old": True},
        )
        self.assertEqual(self.leftover_temp_files(), [])

    def test_replace_failure_does_not_create_target(self):
        target = self.directory / "new-evidence.json"
        with mock.patch(
            "check_metrics_evidence.os.replace",
            side_effect=OSError("replace failed"),
        ):
            with self.assertRaises(OSError):
                check_metrics_evidence.write_json_atomic(
                    str(target), {"new": True}
                )
        self.assertFalse(target.exists())
        self.assertEqual(self.leftover_temp_files(), [])


if __name__ == "__main__":
    unittest.main()
