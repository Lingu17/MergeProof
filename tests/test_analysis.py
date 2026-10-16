import unittest
from app import analysis


class AnalysisTests(unittest.TestCase):
    def test_conflict_requires_two_green_independent_runs_and_red_combined_run(self):
        result = analysis()
        self.assertEqual(result["tests"]["change_a"]["status"], "passed")
        self.assertEqual(result["tests"]["change_b"]["status"], "passed")
        self.assertEqual(result["tests"]["combined"]["status"], "failed")
        self.assertEqual(result["summary"]["confirmed"], 1)
        self.assertIn("total_usd", result["finding"]["why"])


if __name__ == "__main__":
    unittest.main()
