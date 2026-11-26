"""export_metrics.py 导出行为测试。

使用本地 ``threading`` + ``http.server`` 假 Prometheus，不依赖外网或 Docker。
覆盖：完整导出（CSV 内容、queries.json 字段）、空结果、不可达（4）、
查询错误（4 且不落地）、输出目录非空（2）、summary 缺阶段（2）以及原子落地。
"""

import csv
import importlib.util
import io
import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCRIPT = Path(__file__).resolve().parent / "export_metrics.py"

EXPECTED_QUERY_NAMES = [
    "request_rate",
    "http_4xx_rate",
    "http_5xx_rate",
    "http_5xx_ratio",
    "request_rate_by_route",
    "rolling_p95",
    "full_window_p95",
    "gc_rate",
    "gc_average_pause",
    "db_pool_utilization",
    "db_pool_empty_acquire_rate",
    "db_pool_average_acquire_wait",
    "pg_xact_commit_rate",
    "pg_waiting_locks",
    "pg_cache_hit_ratio",
    "pg_temp_files_rate",
    "pg_checkpoint_write_time_rate",
]

EXPECTED_PROMQL = {
    "request_rate": (
        "sum(rate(payment_hub_http_requests_total[1m])) or vector(0)"
    ),
    "http_4xx_rate": (
        'sum(rate(payment_hub_http_requests_total{status=~"4.."}[1m]))'
        " or vector(0)"
    ),
    "http_5xx_rate": (
        'sum(rate(payment_hub_http_requests_total{status=~"5.."}[1m]))'
        " or vector(0)"
    ),
    "http_5xx_ratio": (
        "(sum(rate(payment_hub_http_requests_total"
        '{status=~"5.."}[1m])) or vector(0))'
        " / (sum(rate(payment_hub_http_requests_total[1m])) > 0)"
    ),
    "request_rate_by_route": (
        "sum by (route, status)"
        " (rate(payment_hub_http_requests_total[1m]))"
    ),
    "rolling_p95": (
        "histogram_quantile(0.95, sum by (le, route)"
        " (rate(payment_hub_http_request_duration_seconds_bucket[1m])))"
    ),
    "full_window_p95": (
        "histogram_quantile(0.95, sum by (le, route)"
        " (increase(payment_hub_http_request_duration_seconds_bucket"
        "[330s])))"
    ),
    "gc_rate": "rate(go_gc_duration_seconds_count[1m])",
    "gc_average_pause": (
        "rate(go_gc_duration_seconds_sum[1m])"
        " / rate(go_gc_duration_seconds_count[1m])"
    ),
    "db_pool_utilization": (
        "payment_hub_db_pool_connections_in_use"
        " / payment_hub_db_pool_connections_max"
    ),
    "db_pool_empty_acquire_rate": (
        "rate(payment_hub_db_pool_empty_acquires_total[1m])"
    ),
    "db_pool_average_acquire_wait": (
        "rate(payment_hub_db_pool_acquire_duration_seconds_total[1m])"
        " / (rate(payment_hub_db_pool_acquires_total[1m]) > 0)"
    ),
    "pg_xact_commit_rate": (
        "sum(rate(pg_stat_database_xact_commit[1m]))"
    ),
    "pg_waiting_locks": "pg_benchmark_waiting_locks",
    "pg_cache_hit_ratio": (
        "sum(rate(pg_stat_database_blks_hit[1m]))"
        " / ((sum(rate(pg_stat_database_blks_hit[1m]))"
        " + sum(rate(pg_stat_database_blks_read[1m]))) > 0)"
    ),
    "pg_temp_files_rate": (
        "sum(rate(pg_stat_database_temp_files[1m]))"
    ),
    "pg_checkpoint_write_time_rate": (
        "sum(rate(pg_stat_bgwriter_checkpoint_write_time_total[1m]))"
    ),
}

WINDOW_START = 1700000000  # 2023-11-14T22:13:20Z
WINDOW_END = 1700000330  # 2023-11-14T22:18:50Z


def _load_export_module():
    spec = importlib.util.spec_from_file_location(
        "export_metrics_under_test", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXPORT = _load_export_module()


def _vector(samples, timestamp=1700000330.5):
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": dict(metric),
                    "value": [timestamp, value],
                }
                for metric, value in samples
            ],
        },
    }


def _empty_vector():
    return {
        "status": "success",
        "data": {"resultType": "vector", "result": []},
    }


class _QueryHandler(BaseHTTPRequestHandler):
    """只处理 /api/v1/query 的最小 Prometheus HTTP 假实现。"""

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 接口
        parsed = urlparse(self.path)
        if parsed.path != "/api/v1/query":
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        query = params.get("query", [""])[0]
        requested_at = params.get("time", [""])[0]
        self.server.requests.append((query, requested_at))
        try:
            status, payload = self.server.responder(query)
        except Exception as exc:  # pragma: no cover - 测试辅助防御
            status = 500
            payload = {"status": "error", "error": str(exc)}
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: A003 - 静默测试日志
        pass


class FakePrometheus:
    """在 127.0.0.1 随机端口启动的假 Prometheus。"""

    def __init__(self, responder):
        self.requests = []
        self._server = HTTPServer(("127.0.0.1", 0), _QueryHandler)
        self._server.responder = responder
        self._server.requests = self.requests
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    @property
    def url(self):
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def _closed_port():
    """绑定但不监听的本机端口；返回 (socket, port)，调用方负责关闭。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    return sock, sock.getsockname()[1]


class ExportMetricsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def write_summary(self, summary):
        (self.run_dir / "summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )

    def write_default_summary(self):
        self.write_summary(
            {
                "schema_version": 3,
                "run_id": "a1b2c3d4e5f6",
                "stages": [
                    {
                        "stage": "load200",
                        "k6_started_epoch": WINDOW_START,
                        "k6_finished_epoch": WINDOW_END,
                    }
                ],
            }
        )

    def run_main(self, prometheus, out):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = EXPORT.main(
                [
                    "--run-dir",
                    str(self.run_dir),
                    "--prometheus",
                    prometheus,
                    "--out",
                    str(out),
                ]
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_no_temp_leftovers(self):
        leftovers = [
            path.name
            for path in self.tmp.iterdir()
            if path.name.startswith(".") and ".tmp-" in path.name
        ]
        self.assertEqual(leftovers, [])

    def test_full_export_writes_csv_and_queries_json(self):
        self.write_summary(
            {
                "schema_version": 3,
                "run_id": "a1b2c3d4e5f6",
                "stages": [
                    {
                        "stage": "smoke",
                        "k6_started_epoch": WINDOW_START - 100,
                        "k6_finished_epoch": WINDOW_START - 90,
                    },
                    {
                        "stage": "load200",
                        "k6_started_epoch": WINDOW_START,
                        "k6_finished_epoch": WINDOW_START + 120,
                    },
                    {
                        "stage": "load200",
                        "k6_started_epoch": WINDOW_START + 200,
                        "k6_finished_epoch": WINDOW_END,
                    },
                ],
            }
        )
        out = self.tmp / "metrics-export"
        out.mkdir()  # 已存在的空目录允许被原子替换
        samples = [
            (
                {
                    "__name__": "demo_metric",
                    "route": "/api/v1/payments",
                    "status": "200",
                },
                "1.5",
            ),
            (
                {"__name__": "demo_metric", "beta": "2", "alpha": "1"},
                "0",
            ),
        ]
        with FakePrometheus(
            lambda query: (200, _vector(samples))
        ) as prometheus:
            prometheus_url = (
                prometheus.url.replace(
                    "http://", "http://loaduser:secretpass@", 1
                )
                + "?token=supersecret"
            )
            code, stdout, stderr = self.run_main(
                prometheus_url, out
            )
            requests = list(prometheus.requests)

        self.assertEqual(code, 0, stderr)
        self.assertIn("已导出", stdout)
        self.assertTrue((out / "queries.json").is_file())

        manifest_text = (out / "queries.json").read_text(encoding="utf-8")
        manifest = json.loads(manifest_text)
        self.assertEqual(manifest["schema"], "payment-hub.metrics-export.v1")
        self.assertEqual(manifest["run_id"], "a1b2c3d4e5f6")
        self.assertEqual(
            manifest["window"],
            {
                "start_utc": "2023-11-14T22:13:20Z",
                "end_utc": "2023-11-14T22:18:50Z",
                "start_epoch": WINDOW_START,
                "end_epoch": WINDOW_END,
            },
        )
        self.assertEqual(manifest["prometheus"], prometheus.url)
        self.assertNotIn("loaduser", manifest_text)
        self.assertNotIn("secretpass", manifest_text)
        self.assertNotIn("supersecret", manifest_text)
        self.assertEqual(
            [entry["name"] for entry in manifest["queries"]],
            EXPECTED_QUERY_NAMES,
        )
        for entry in manifest["queries"]:
            self.assertEqual(entry["status"], "success")
            self.assertEqual(entry["samples"], 2)
            self.assertEqual(entry["csv"], entry["name"] + ".csv")
            self.assertEqual(entry["promql"], EXPECTED_PROMQL[entry["name"]])
            self.assertNotIn("$__range", entry["promql"])
            self.assertNotIn("/", entry["csv"])
            self.assertTrue((out / entry["csv"]).is_file())

        full_window = next(
            entry
            for entry in manifest["queries"]
            if entry["name"] == "full_window_p95"
        )
        self.assertEqual(
            full_window["promql"],
            "histogram_quantile(0.95, sum by (le, route)"
            " (increase(payment_hub_http_request_duration_seconds_bucket"
            "[330s])))",
        )
        self.assertNotIn("$__range", full_window["promql"])

        request_rate = next(
            entry
            for entry in manifest["queries"]
            if entry["name"] == "request_rate"
        )
        self.assertEqual(
            request_rate["promql"],
            "sum(rate(payment_hub_http_requests_total[1m])) or vector(0)",
        )

        with (out / "request_rate.csv").open(
            "r", encoding="utf-8", newline=""
        ) as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(list(rows[0]), ["metric", "labels", "timestamp", "value"])
        self.assertEqual(
            rows[1],
            ["demo_metric", "alpha=1;beta=2", "1700000330.5", "0"],
        )
        self.assertEqual(
            rows[2],
            [
                "demo_metric",
                "route=/api/v1/payments;status=200",
                "1700000330.5",
                "1.5",
            ],
        )
        self.assertEqual(len(rows), 3)

        self.assertEqual(len(requests), len(EXPECTED_QUERY_NAMES))
        for (sent_promql, requested_at), entry in zip(
            requests, manifest["queries"]
        ):
            self.assertEqual(sent_promql, entry["promql"])
            self.assertAlmostEqual(
                float(requested_at), float(WINDOW_END), places=3
            )
        self.assert_no_temp_leftovers()

    def test_empty_result_exports_header_only_csv(self):
        self.write_default_summary()
        out = self.tmp / "metrics-export"
        with FakePrometheus(
            lambda query: (200, _empty_vector())
        ) as prometheus:
            code, stdout, stderr = self.run_main(prometheus.url, out)

        self.assertEqual(code, 0, stderr)
        manifest = json.loads(
            (out / "queries.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [entry["name"] for entry in manifest["queries"]],
            EXPECTED_QUERY_NAMES,
        )
        for entry in manifest["queries"]:
            self.assertEqual(entry["status"], "success")
            self.assertEqual(entry["samples"], 0)
            csv_bytes = (out / entry["csv"]).read_bytes()
            self.assertEqual(
                csv_bytes, b"metric,labels,timestamp,value\r\n"
            )
        self.assert_no_temp_leftovers()

    def test_non_numeric_sample_exits_four_without_out(self):
        self.write_default_summary()
        cases = [
            (
                "value",
                _vector([({"__name__": "demo_metric"}, "=1+1")]),
            ),
            (
                "timestamp",
                _vector(
                    [({"__name__": "demo_metric"}, "1")],
                    timestamp="=1+1",
                ),
            ),
        ]
        for label, payload in cases:
            with self.subTest(field=label):
                out = self.tmp / f"non-numeric-{label}"
                with FakePrometheus(
                    lambda query, payload=payload: (200, payload)
                ) as prometheus:
                    code, stdout, stderr = self.run_main(
                        prometheus.url, out
                    )
                self.assertEqual(code, 4, stderr)
                self.assertIn("不是数字", stderr)
                self.assertFalse(out.exists())
        self.assert_no_temp_leftovers()

    def test_malformed_empty_result_exits_four(self):
        self.write_default_summary()
        payloads = [
            {"status": "success", "data": {"resultType": "vector"}},
            {
                "status": "success",
                "data": {"resultType": "matrix", "result": []},
            },
            {"status": "success", "data": {"result": []}},
        ]
        for index, payload in enumerate(payloads):
            with self.subTest(index=index):
                out = self.tmp / f"malformed-{index}"
                with FakePrometheus(
                    lambda query, payload=payload: (200, payload)
                ) as prometheus:
                    code, stdout, stderr = self.run_main(
                        prometheus.url, out
                    )
                self.assertEqual(code, 4, stderr)
                self.assertFalse(out.exists())
        self.assert_no_temp_leftovers()

    def test_metric_column_falls_back_to_query_name(self):
        self.write_default_summary()
        out = self.tmp / "metrics-export"
        with FakePrometheus(
            lambda query: (
                200,
                _vector(
                    [({"route": "/api/v1/payments"}, "0.02")]
                ),
            )
        ) as prometheus:
            code, stdout, stderr = self.run_main(prometheus.url, out)

        self.assertEqual(code, 0, stderr)
        for name in EXPECTED_QUERY_NAMES:
            with (out / f"{name}.csv").open(
                "r", encoding="utf-8", newline=""
            ) as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1][0], name)
            self.assertEqual(rows[1][1], "route=/api/v1/payments")
        self.assert_no_temp_leftovers()

    def test_cr_in_label_is_quoted_and_round_trips(self):
        self.write_default_summary()
        out = self.tmp / "metrics-export"
        with FakePrometheus(
            lambda query: (
                200,
                _vector(
                    [
                        (
                            {
                                "__name__": "demo_metric",
                                "note": "first\rsecond",
                            },
                            "1",
                        )
                    ]
                ),
            )
        ) as prometheus:
            code, stdout, stderr = self.run_main(prometheus.url, out)

        self.assertEqual(code, 0, stderr)
        csv_path = out / "request_rate.csv"
        csv_bytes = csv_path.read_bytes()
        self.assertEqual(csv_bytes.count(b"\r\n"), 2)
        with csv_path.open(
            "r", encoding="utf-8", newline=""
        ) as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[1]), 4)
        self.assertEqual(rows[1][0], "demo_metric")
        self.assertEqual(rows[1][1], "note=first\rsecond")
        self.assertEqual(rows[1][2], "1700000330.5")
        self.assertEqual(rows[1][3], "1")
        self.assert_no_temp_leftovers()

    def test_invalid_epoch_stage_is_skipped(self):
        self.write_summary(
            {
                "schema_version": 3,
                "run_id": "a1b2c3d4e5f6",
                "stages": [
                    {
                        "stage": "load200",
                        "k6_started_epoch": 10 ** 400,
                        "k6_finished_epoch": 10 ** 400,
                    },
                    {
                        "stage": "load200",
                        "k6_started_epoch": 1e30,
                        "k6_finished_epoch": 1.1e30,
                    },
                    {
                        "stage": "load200",
                        "k6_started_epoch": WINDOW_START,
                        "k6_finished_epoch": WINDOW_END,
                    },
                ],
            }
        )
        out = self.tmp / "metrics-export"
        with FakePrometheus(
            lambda query: (200, _empty_vector())
        ) as prometheus:
            code, stdout, stderr = self.run_main(prometheus.url, out)

        self.assertEqual(code, 0, stderr)
        manifest = json.loads(
            (out / "queries.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["window"]["start_epoch"], WINDOW_START
        )
        self.assertEqual(manifest["window"]["end_epoch"], WINDOW_END)
        self.assert_no_temp_leftovers()

    def test_all_invalid_epochs_exit_two(self):
        self.write_summary(
            {
                "schema_version": 3,
                "run_id": "a1b2c3d4e5f6",
                "stages": [
                    {
                        "stage": "load200",
                        "k6_started_epoch": 10 ** 400,
                        "k6_finished_epoch": 10 ** 400,
                    },
                    {
                        "stage": "load200",
                        "k6_started_epoch": 1e30,
                        "k6_finished_epoch": 1.1e30,
                    },
                ],
            }
        )
        out = self.tmp / "metrics-export"
        with FakePrometheus(
            lambda query: (200, _empty_vector())
        ) as prometheus:
            code, stdout, stderr = self.run_main(prometheus.url, out)
            requests = list(prometheus.requests)

        self.assertEqual(code, 2, stderr)
        self.assertIn("load200", stderr)
        self.assertEqual(requests, [])
        self.assertFalse(out.exists())
        self.assert_no_temp_leftovers()

    def test_unreachable_prometheus_exits_four_without_out(self):
        self.write_default_summary()
        out = self.tmp / "metrics-export"
        holder, port = _closed_port()
        self.addCleanup(holder.close)
        url = f"http://127.0.0.1:{port}"

        code, stdout, stderr = self.run_main(url, out)

        self.assertEqual(code, 4, stderr)
        self.assertIn("Prometheus", stderr)
        self.assertFalse(out.exists())
        self.assert_no_temp_leftovers()

    def test_query_error_exits_four_and_keeps_empty_out(self):
        self.write_default_summary()
        out = self.tmp / "metrics-export"
        out.mkdir()
        calls = {"count": 0}

        def responder(query):
            calls["count"] += 1
            if calls["count"] == 5:
                return 503, {"status": "error", "error": "boom"}
            return 200, _vector(
                [({"__name__": "demo_metric", "route": "/api/v1/payments"}, "1")]
            )

        with FakePrometheus(responder) as prometheus:
            code, stdout, stderr = self.run_main(prometheus.url, out)
            request_count = len(prometheus.requests)

        self.assertEqual(code, 4, stderr)
        self.assertEqual(request_count, 5)
        self.assertTrue(out.is_dir())
        self.assertEqual(list(out.iterdir()), [])
        self.assert_no_temp_leftovers()

    def test_non_empty_out_exits_two_without_querying(self):
        self.write_default_summary()
        out = self.tmp / "metrics-export"
        out.mkdir()
        keep = out / "keep.txt"
        keep.write_text("keep", encoding="utf-8")

        with FakePrometheus(
            lambda query: (200, _empty_vector())
        ) as prometheus:
            code, stdout, stderr = self.run_main(prometheus.url, out)
            requests = list(prometheus.requests)

        self.assertEqual(code, 2, stderr)
        self.assertIn("非空", stderr)
        self.assertEqual(requests, [])
        self.assertEqual(keep.read_text(encoding="utf-8"), "keep")
        self.assert_no_temp_leftovers()

    def test_summary_without_load200_exits_two(self):
        self.write_summary(
            {
                "schema_version": 3,
                "run_id": "a1b2c3d4e5f6",
                "stages": [
                    {
                        "stage": "smoke",
                        "k6_started_epoch": WINDOW_START,
                        "k6_finished_epoch": WINDOW_END,
                    }
                ],
            }
        )
        out = self.tmp / "metrics-export"
        with FakePrometheus(
            lambda query: (200, _empty_vector())
        ) as prometheus:
            code, stdout, stderr = self.run_main(prometheus.url, out)
            requests = list(prometheus.requests)

        self.assertEqual(code, 2, stderr)
        self.assertIn("load200", stderr)
        self.assertEqual(requests, [])
        self.assertFalse(out.exists())
        self.assert_no_temp_leftovers()

    def test_missing_summary_exits_two(self):
        out = self.tmp / "metrics-export"

        code, stdout, stderr = self.run_main(
            "http://127.0.0.1:9090", out
        )

        self.assertEqual(code, 2, stderr)
        self.assertIn("summary.json", stderr)
        self.assertFalse(out.exists())
        self.assert_no_temp_leftovers()

    def test_help_exits_zero(self):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage", (result.stdout + result.stderr).lower())


if __name__ == "__main__":
    unittest.main()
