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
        self.assertIn("98.0 != 97.2", result["tests"]["combined"]["output"])
        self.assertEqual(result["summary"]["risks"], 1)
        self.assertEqual(result["summary"]["suggestions"], 1)
        self.assertEqual(result["progress"]["label"], "5 of 5 complete")
        self.assertEqual(result["graph"], {"nodes": 5, "edges": 5})
        self.assertEqual(result["finding"]["evidence"][2]["line"], "7")
        self.assertIn("97.2", result["finding"]["evidence"][2]["code"])


if __name__ == "__main__":
    unittest.main()
