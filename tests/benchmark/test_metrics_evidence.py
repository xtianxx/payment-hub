"""check_metrics_evidence.py 命令行骨架测试。"""

import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check_metrics_evidence.py"


class CheckMetricsEvidenceCliTest(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_help_exits_zero_and_shows_usage(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage", (result.stdout + result.stderr).lower())

    def test_missing_required_run_dir_exits_two(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 2, result.stderr)


if __name__ == "__main__":
    unittest.main()
