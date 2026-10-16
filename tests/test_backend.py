import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from app import Store, analysis, inspect_repository, run_branch


class BackendTests(unittest.TestCase):
    def test_store_persists_sample_entry_and_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            first = Store(path)
            self.assertTrue(first.data["repositories"][0]["sample"])
            first.save("investigations", {"id": "i1", "status": "created"})
            second = Store(path)
            self.assertEqual(second.find("investigations", "i1")["status"], "created")
            self.assertEqual(second.data["repositories"][0]["name"], "checkout-service (sample)")

    def test_real_python_repository_can_be_inspected_and_run_in_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-qm", "initial"], check=True)
            current = subprocess.check_output(["git", "-C", str(repo), "branch", "--show-current"], text=True).strip()
            subprocess.run(["git", "-C", str(repo), "branch", "change-a"], check=True)
            metadata = {"id": "r1", "path": str(repo)}
            inspected = inspect_repository(metadata, current, "change-a")
            self.assertEqual(inspected["branch_a"], current)
            result = run_branch(repo, "change-a", "python -m unittest discover -v")
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["exit_code"], 0)

    def test_sample_workflow_keeps_confirmed_semantics(self):
        result = analysis()
        self.assertEqual(result["summary"]["confirmed"], 1)
        self.assertEqual(result["tests"]["change_a"]["exit_code"], 0)
        self.assertNotEqual(result["tests"]["combined"]["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
