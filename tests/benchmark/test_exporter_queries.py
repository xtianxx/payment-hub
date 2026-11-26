import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
QUERIES = REPO / "deploy" / "observability" / "postgres-exporter-queries.yaml"
USER_FILE = REPO / "deploy" / "observability" / "postgres-exporter-user"


class ExporterQueriesTest(unittest.TestCase):
    def setUp(self):
        self.data = yaml.safe_load(QUERIES.read_text(encoding="utf-8"))

    def test_benchmark_namespace_has_lock_columns(self):
        self.assertIn("pg_benchmark", self.data)
        metrics = self.data["pg_benchmark"]["metrics"]
        columns = set()
        for entry in metrics:
            columns.update(entry)
        self.assertEqual(columns, {"waiting_locks", "granted_locks"})
        for entry in metrics:
            for name, definition in entry.items():
                with self.subTest(metric=name):
                    self.assertEqual(definition["usage"], "GAUGE")
                    self.assertTrue(definition["description"])

    def test_query_uses_pg_locks_without_user_labels(self):
        query = self.data["pg_benchmark"]["query"]
        self.assertIn("pg_locks", query)
        self.assertIn("granted", query)
        lowered = query.lower()
        self.assertNotIn("usename", lowered)
        self.assertNotIn("application_name", lowered)
        self.assertNotIn("pg_stat_activity", lowered)

    def test_exporter_user_file_is_single_role(self):
        text = USER_FILE.read_text(encoding="utf-8").strip()
        self.assertEqual(text, "postgres_exporter")
        self.assertNotIn("\n", text)


if __name__ == "__main__":
    unittest.main()
