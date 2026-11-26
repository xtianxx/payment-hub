"""校验 Grafana 预置总览看板的经典 JSON 结构（T038）。

断言范围：
- 固定 UID、标题、schemaVersion、时间范围和刷新间隔；
- 面板数量、四组行标题、每个面板的 id/type/title/gridPos/targets；
- 所有 target.expr 中的契约指标名与 $__rate_interval / $__range 口径；
- 不出现 usename、application_name 或订单号类标签；
- 不引用合同之外的模板变量。
"""

import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DASHBOARD = (
    REPO
    / "deploy"
    / "observability"
    / "grafana"
    / "dashboards"
    / "overview.json"
)

EXPECTED_UID = "payment-hub-overview"
EXPECTED_TITLE = "Payment Hub 压测总览"
EXPECTED_SCHEMA_VERSION = 39
EXPECTED_TIME = {"from": "now-6h", "to": "now"}
EXPECTED_REFRESH = "30s"
EXPECTED_PANEL_COUNT = 20
EXPECTED_GROUP_TITLES = ("HTTP 请求", "数据库连接池", "PostgreSQL", "抓取状态")

REQUIRED_METRICS = (
    "payment_hub_http_requests_total",
    "payment_hub_http_request_duration_seconds_bucket",
    "payment_hub_db_pool_connections_in_use",
    "payment_hub_db_pool_empty_acquires_total",
    "pg_stat_database_xact_commit",
    "pg_benchmark_waiting_locks",
    "pg_stat_database_blks_hit",
    "pg_stat_database_blks_read",
    "pg_stat_database_temp_files",
    "pg_stat_bgwriter_checkpoint_write_time_total",
    "pg_up",
)
REQUIRED_UP_SELECTORS = ('up{job="payment-hub"}', 'up{job="postgres"}')
FORBIDDEN_LABEL_TOKENS = (
    "usename",
    "application_name",
    "order_id",
    "order_no",
    "order_number",
    "payment_id",
    "request_id",
    "trace_id",
    "user_id",
)
# 只允许 Grafana 内置的 $__rate_interval / $__range 等变量，不允许看板自定义变量。
TEMPLATE_VARIABLE = re.compile(r"\$(?!__)[A-Za-z_][A-Za-z0-9_]*")


def load_dashboard():
    with DASHBOARD.open(encoding="utf-8") as handle:
        return json.load(handle)


def iter_targets(panel):
    targets = panel.get("targets")
    if not isinstance(targets, list):
        return []
    return [target for target in targets if isinstance(target, dict)]


def all_expressions(dashboard):
    expressions = []
    for panel in dashboard["panels"]:
        for target in iter_targets(panel):
            expression = target.get("expr")
            if isinstance(expression, str):
                expressions.append(expression)
    return expressions


def datasource_name(reference):
    if isinstance(reference, str):
        return reference
    if isinstance(reference, dict):
        return reference.get("name")
    return None


class DashboardJsonTest(unittest.TestCase):
    def setUp(self):
        self.dashboard = load_dashboard()
        self.panels = self.dashboard["panels"]

    def test_identity_and_time_window(self):
        self.assertEqual(self.dashboard["uid"], EXPECTED_UID)
        self.assertEqual(self.dashboard["title"], EXPECTED_TITLE)
        self.assertEqual(self.dashboard["schemaVersion"], EXPECTED_SCHEMA_VERSION)
        self.assertEqual(self.dashboard["time"], EXPECTED_TIME)
        self.assertEqual(self.dashboard["refresh"], EXPECTED_REFRESH)

    def test_panel_count_and_four_group_titles(self):
        self.assertIsInstance(self.panels, list)
        self.assertEqual(len(self.panels), EXPECTED_PANEL_COUNT)
        row_titles = [
            panel["title"] for panel in self.panels if panel.get("type") == "row"
        ]
        self.assertEqual(tuple(row_titles), EXPECTED_GROUP_TITLES)
        for title in EXPECTED_GROUP_TITLES:
            with self.subTest(title=title):
                self.assertIn(title, row_titles)

    def test_every_panel_has_required_structure(self):
        for panel in self.panels:
            with self.subTest(panel=panel.get("title")):
                for key in ("id", "type", "title", "gridPos", "targets"):
                    self.assertIn(key, panel)
                self.assertIsInstance(panel["id"], int)
                self.assertGreater(panel["id"], 0)
                self.assertIsInstance(panel["type"], str)
                self.assertTrue(panel["type"])
                self.assertIsInstance(panel["title"], str)
                self.assertTrue(panel["title"])
                self.assertIsInstance(panel["gridPos"], dict)
                self.assertIsInstance(panel["targets"], list)
                if panel["type"] != "row":
                    self.assertTrue(
                        panel["targets"], f"{panel['title']} 缺少查询 target"
                    )
                    for target in iter_targets(panel):
                        self.assertIsInstance(target.get("expr"), str)
                        self.assertTrue(target["expr"].strip())

    def test_panel_ids_are_unique_and_grid_positions_non_zero(self):
        ids = [panel["id"] for panel in self.panels]
        self.assertEqual(len(ids), len(set(ids)), f"面板 id 重复：{ids}")
        for panel in self.panels:
            with self.subTest(panel=panel["title"]):
                grid = panel["gridPos"]
                for key in ("h", "w", "x", "y"):
                    self.assertIn(key, grid)
                    self.assertIsInstance(grid[key], int)
                self.assertGreater(grid["h"], 0)
                self.assertGreater(grid["w"], 0)
                self.assertGreaterEqual(grid["x"], 0)
                self.assertGreaterEqual(grid["y"], 0)
                self.assertLessEqual(grid["x"] + grid["w"], 24)

    def test_queries_cover_contract_metrics(self):
        expressions = all_expressions(self.dashboard)
        self.assertTrue(expressions, "看板中没有任何 target.expr")
        text = "\n".join(expressions)
        for metric in REQUIRED_METRICS:
            with self.subTest(metric=metric):
                self.assertIn(metric, text)
        for selector in REQUIRED_UP_SELECTORS:
            with self.subTest(selector=selector):
                self.assertIn(selector, text)
        self.assertRegex(text, r"(?<![A-Za-z0-9_])up\{")
        self.assertRegex(text, r"(?<![A-Za-z0-9_])pg_up\b")

    def test_queries_use_grafana_time_variables(self):
        expressions = all_expressions(self.dashboard)
        text = "\n".join(expressions)
        self.assertIn("$__rate_interval", text)
        self.assertNotIn("[1m]", text)
        self.assertIn("$__range", text)

        round_p95 = [
            panel
            for panel in self.panels
            if panel.get("title") == "整轮 P95 延迟（按路由）"
        ]
        self.assertEqual(len(round_p95), 1)
        round_p95_expr = round_p95[0]["targets"][0]["expr"]
        self.assertIn("histogram_quantile", round_p95_expr)
        self.assertIn("increase(", round_p95_expr)
        self.assertIn("[$__range]", round_p95_expr)

    def test_no_forbidden_labels_or_template_variables(self):
        expressions = all_expressions(self.dashboard)
        text = "\n".join(expressions)
        for token in FORBIDDEN_LABEL_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, text)

        self.assertEqual(
            self.dashboard.get("templating", {}).get("list", []),
            [],
            "看板不应定义模板变量",
        )
        serialized = json.dumps(self.dashboard, ensure_ascii=False)
        unexpected = sorted(set(TEMPLATE_VARIABLE.findall(serialized)))
        self.assertEqual(unexpected, [], f"引用了未知模板变量：{unexpected}")

    def test_datasource_references_prometheus(self):
        for panel in self.panels:
            if panel.get("type") == "row":
                continue
            with self.subTest(panel=panel["title"]):
                self.assertEqual(
                    datasource_name(panel.get("datasource")), "Prometheus"
                )
                for target in iter_targets(panel):
                    self.assertEqual(
                        datasource_name(target.get("datasource")), "Prometheus"
                    )


if __name__ == "__main__":
    unittest.main()
