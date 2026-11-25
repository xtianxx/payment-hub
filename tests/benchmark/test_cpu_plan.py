import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cpu_plan import (  # noqa: E402
    CpuPlanError,
    format_cpu_set,
    parse_cpu_set,
    parse_lscpu_topology,
    validate_groups,
)


class CpuSetTest(unittest.TestCase):
    def test_round_trip(self):
        cpus = parse_cpu_set("0-1,4,6-9")
        self.assertEqual(cpus, {0, 1, 4, 6, 7, 8, 9})
        self.assertEqual(format_cpu_set(cpus), "0-1,4,6-9")

    def test_rejects_bad_sets(self):
        for value in ("", "1-0", "0,,1", "a-b", "0-1-2"):
            with self.assertRaises(CpuPlanError, msg=value):
                parse_cpu_set(value)

    def test_sibling_pair_split_rejected(self):
        topology = parse_lscpu_topology(
            "0,0,0\n1,0,0\n2,1,0\n3,1,0\n"
        )
        with self.assertRaises(CpuPlanError) as caught:
            validate_groups(
                {"k6": "0", "postgres": "2-3", "app": "1"},
                topology,
                {0, 1, 2, 3},
            )
        self.assertIn("splits sibling pair", str(caught.exception))

    def test_disjoint_and_inside_guest_required(self):
        topology = parse_lscpu_topology(
            "0,0,0\n1,0,0\n2,1,0\n3,1,0\n4,2,0\n5,2,0\n"
        )
        with self.assertRaises(CpuPlanError):
            validate_groups(
                {"k6": "0-1", "postgres": "0-1", "app": "2-3"},
                topology,
                {0, 1, 2, 3},
            )
        with self.assertRaises(CpuPlanError) as caught:
            validate_groups(
                {"k6": "0-1", "postgres": "2-3", "app": "4-5"},
                topology,
                {0, 1, 2, 3},
            )
        self.assertIn("outside the guest-allowed set", str(caught.exception))

    def test_single_thread_core_rejected(self):
        topology = parse_lscpu_topology(
            "0,0,0\n1,1,0\n2,2,0\n3,3,0\n"
        )
        with self.assertRaises(CpuPlanError) as caught:
            validate_groups(
                {"k6": "0-1", "postgres": "2-3"},
                topology,
                {0, 1, 2, 3},
            )
        self.assertIn("not a two-thread core", str(caught.exception))

    def test_valid_plan(self):
        topology = parse_lscpu_topology(
            "0,0,0\n1,0,0\n2,1,0\n3,1,0\n4,2,0\n5,2,0\n"
        )
        plan = validate_groups(
            {"k6": "0-1", "postgres": "2-3", "app": "4-5"},
            topology,
            {0, 1, 2, 3, 4, 5},
        )
        self.assertEqual(plan["groups"]["k6"]["mask"], "0-1")
        self.assertEqual(len(plan["sibling_pairs"]), 3)


if __name__ == "__main__":
    unittest.main()
