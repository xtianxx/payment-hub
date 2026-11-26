import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "deploy" / "observability" / "prometheus.yml"
VIEW = REPO / "deploy" / "observability" / "prometheus.view.yml"


class PrometheusConfigTest(unittest.TestCase):
    def test_scrape_interval_and_timeout(self):
        for path in (CONFIG, VIEW):
            with self.subTest(path=path.name):
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertEqual(data["global"]["scrape_interval"], "5s")
                self.assertEqual(data["global"]["scrape_timeout"], "4s")

    def test_measurement_config_scrapes_application_and_exporter(self):
        data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        jobs = {job["job_name"]: job for job in data["scrape_configs"]}
        self.assertEqual(set(jobs), {"payment-hub", "postgres"})
        self.assertEqual(
            jobs["payment-hub"]["static_configs"][0]["targets"], ["127.0.0.1:9091"]
        )
        self.assertEqual(
            jobs["postgres"]["static_configs"][0]["targets"], ["127.0.0.1:9187"]
        )

    def test_view_config_has_no_scrape_targets(self):
        data = yaml.safe_load(VIEW.read_text(encoding="utf-8"))
        self.assertEqual(data["scrape_configs"], [])


if __name__ == "__main__":
    unittest.main()
