"""check_metrics_evidence.py 的单元测试。

测试使用标准库 threading + http.server 启动本地假 Prometheus，不依赖网络、
Docker 或真实 Prometheus。覆盖完整证据、缺阶段、应用与 exporter 的 up=0、
pg_up=0、抓取次数不足、窗口内采样缺口、Prometheus 不可达、archive_error
保留、原子写入和退出码 0/2/3/4。
"""

import json
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


def sample_timestamps(start, end, gap_after=None):
    """生成假抓取样本时间；gap_after 指定 55 秒后制造 8 秒缺口。"""
    timestamps = []
    current = float(start)
    shifted = False
    while current <= end + 1e-9:
        if (
            gap_after is not None
            and not shifted
            and current > start + gap_after + 1e-9
        ):
            current += 3.0
            shifted = True
            if current > end + 1e-9:
                break
        timestamps.append(round(current, 3))
        current += 5.0
    return timestamps


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
        up_gap_after=None,
        exporter_gap_after=None,
        pg_gap_after=None,
    ):
        self.up_observed = int(up_observed)
        self.up_1 = int(up_1)
        self.exporter_observed = int(exporter_observed)
        self.exporter_up_1 = int(exporter_up_1)
        self.pg_observed = int(pg_observed)
        self.pg_up_1 = int(pg_up_1)
        self.present_series = set(REQUIRED_SERIES) - set(missing_series)
        self.up_gap_after = up_gap_after
        self.exporter_gap_after = exporter_gap_after
        self.pg_gap_after = pg_gap_after
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
        if text.startswith('count_over_time(up{job="payment-hub"}'):
            return self._vector(self.up_observed, at_epoch)
        if text.startswith('sum_over_time((up{job="payment-hub"} == 1)'):
            return self._vector(self.up_1, at_epoch)
        if text.startswith('count_over_time(up{job="postgres"}'):
            return self._vector(self.exporter_observed, at_epoch)
        if text.startswith('sum_over_time((up{job="postgres"} == 1)'):
            return self._vector(self.exporter_up_1, at_epoch)
        if text.startswith("count_over_time(pg_up"):
            return self._vector(self.pg_observed, at_epoch)
        if text.startswith("sum_over_time((pg_up == 1)"):
            return self._vector(self.pg_up_1, at_epoch)
        if text in self.present_series:
            return self._vector(1, at_epoch)
        return self._vector(None, at_epoch)

    def range(self, query, start, end, step):
        self.requests.append(("range", query, start, end, step))
        if query == 'up{job="payment-hub"}':
            gap_after = self.up_gap_after
        elif query == 'up{job="postgres"}':
            gap_after = self.exporter_gap_after
        elif query == "pg_up":
            gap_after = self.pg_gap_after
        else:
            return {"resultType": "matrix", "result": []}
        timestamps = sample_timestamps(start, end, gap_after)
        return {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"__name__": query},
                    "values": [[timestamp, "1"] for timestamp in timestamps],
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
                step = params.get("step", ["5s"])[0]
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


def unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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
        path = self.run_dir / "summary.json"
        path.write_text(
            json.dumps(make_summary(**kwargs), indent=2),
            encoding="utf-8",
        )
        return path

    def write_evidence(self, document):
        path = self.run_dir / "metrics-evidence.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

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
            "http://127.0.0.1:%d" % unused_port(), timeout="1"
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

    def test_eight_second_gap_marks_stage_incomplete(self):
        self.write_summary(rounds=1, requested=1)
        with fake_prometheus(up_gap_after=55) as (base_url, _state):
            result = self.run_cli(base_url)
        self.assertEqual(result.returncode, 3, result.stderr)

        evidence = self.read_evidence()
        stage = evidence["stages"][0]
        self.assertEqual(stage["status"], "incomplete")
        self.assertTrue(
            any(
                "8.0" in reason and "7.5" in reason
                for reason in stage["missing"]
            ),
            stage["missing"],
        )

    def test_postgres_exporter_gap_marks_stage_incomplete(self):
        self.write_summary(rounds=1, requested=1)
        with fake_prometheus(exporter_gap_after=55) as (
            base_url,
            _state,
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
                and "8.0" in reason
                and "7.5" in reason
                for reason in stage["missing"]
            ),
            stage["missing"],
        )

    def test_prometheus_unreachable_still_writes_evidence(self):
        self.write_summary(rounds=1, requested=1)
        result = self.run_cli(
            "http://127.0.0.1:%d" % unused_port(), timeout="1"
        )
        self.assertEqual(result.returncode, 4, result.stderr)

        evidence = self.read_evidence()
        self.assertEqual(evidence["status"], "incomplete")
        self.assertTrue(evidence.get("error"), evidence)
        self.assertTrue(evidence["missing"], evidence)
        self.assertEqual(evidence["stages"], [])
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

    def test_invalid_timeout_exits_two(self):
        self.write_summary(rounds=1, requested=1)
        for timeout in ("0", "-2", "nan", "inf"):
            result = self.run_cli(
                "http://127.0.0.1:%d" % unused_port(), timeout=timeout
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
            "http://127.0.0.1:%d" % unused_port(), timeout="1"
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.run_dir / "metrics-evidence.json").exists())

    def test_invalid_summary_json_exits_two(self):
        (self.run_dir / "summary.json").write_text(
            "{not json", encoding="utf-8"
        )
        result = self.run_cli(
            "http://127.0.0.1:%d" % unused_port(), timeout="1"
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.run_dir / "metrics-evidence.json").exists())


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
