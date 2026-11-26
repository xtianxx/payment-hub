import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "deploy" / "compose.observability.yaml"
OBSERVABILITY = REPO / "deploy" / "observability"


def load_compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def command_text(service):
    command = service.get("command", [])
    return "\n".join(str(part) for part in command)


class ObservabilityComposeTest(unittest.TestCase):
    def setUp(self):
        self.compose = load_compose()
        self.services = self.compose["services"]

    def test_all_services_use_host_network_without_published_ports(self):
        self.assertEqual(set(self.services), {"prometheus", "postgres-exporter", "grafana"})
        for name, service in self.services.items():
            with self.subTest(service=name):
                self.assertEqual(service.get("network_mode"), "host")
                self.assertNotIn("ports", service)
                self.assertNotIn("networks", service)
        text = COMPOSE.read_text(encoding="utf-8")
        self.assertNotIn("host.docker.internal", text)

    def test_services_bind_only_loopback(self):
        prometheus = command_text(self.services["prometheus"])
        self.assertIn("--web.listen-address=127.0.0.1:9090", prometheus)
        exporter = command_text(self.services["postgres-exporter"])
        self.assertIn("--web.listen-address=127.0.0.1:9187", exporter)
        grafana_env = self.services["grafana"]["environment"]
        self.assertEqual(grafana_env["GF_SERVER_HTTP_ADDR"], "127.0.0.1")
        self.assertEqual(str(grafana_env["GF_SERVER_HTTP_PORT"]), "3000")

    def test_images_are_fixed_versions(self):
        self.assertEqual(self.services["prometheus"]["image"], "prom/prometheus:v3.16.0")
        self.assertEqual(
            self.services["postgres-exporter"]["image"],
            "prometheuscommunity/postgres-exporter:v0.21.1",
        )
        self.assertEqual(self.services["grafana"]["image"], "grafana/grafana:13.2.7")

    def test_prometheus_and_exporter_run_as_the_host_user(self):
        for name in ("prometheus", "postgres-exporter"):
            with self.subTest(service=name):
                user = self.services[name]["user"]
                self.assertIn("HOST_UID", user)
                self.assertIn("HOST_GID", user)

    def test_exporter_uses_read_only_monitor_credentials(self):
        exporter = self.services["postgres-exporter"]
        mounts = {volume["target"]: volume for volume in exporter["volumes"]}
        for target in (
            "/etc/postgres-exporter/queries.yaml",
            "/run/payment-hub-monitor-user",
            "/run/payment-hub-monitor-password",
        ):
            with self.subTest(target=target):
                self.assertTrue(mounts[target].get("read_only"))
        self.assertIn("PAYMENT_HUB_MONITOR_PASSWORD_FILE", str(mounts["/run/payment-hub-monitor-password"]))

    def test_exporter_datasource_and_collector_contract(self):
        exporter = self.services["postgres-exporter"]
        command = command_text(exporter)
        self.assertIn("--datasource.uri=127.0.0.1:55432/postgres?sslmode=disable", command)
        self.assertIn("--datasource.user-file=/run/payment-hub-monitor-user", command)
        self.assertIn("--datasource.pass-file=/run/payment-hub-monitor-password", command)
        self.assertIn("--collection-timeout=3s", command)
        self.assertIn("--extend.query-path=/etc/postgres-exporter/queries.yaml", command)
        for collector in (
            "settings",
            "stat_activity",
            "stat_activity_autovacuum",
            "stat_user_tables",
            "statio_user_tables",
            "statio_user_indexes",
            "stat_progress_vacuum",
            "locks",
            "database",
            "replication",
            "replication_slots",
            "database_wraparound",
            "long_running_transactions",
            "process_idle",
        ):
            with self.subTest(collector=collector):
                self.assertIn(f"--no-collector.{collector}", command)
        self.assertNotIn("--no-collector.stat_database", command)
        self.assertNotIn("--no-collector.stat_bgwriter", command)

    def test_prometheus_retention_keeps_archive_usable(self):
        command = command_text(self.services["prometheus"])
        self.assertIn("--storage.tsdb.retention.time=${PROMETHEUS_RETENTION:-100y}", command)


if __name__ == "__main__":
    unittest.main()
