import json
import io
import http.client
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import Handler, Store, analysis, inspect_repository, run_branch, run_command, run_combined, _confirmed, collection_view, overview, report_findings, evidence_bundle


class BackendTests(unittest.TestCase):
    def _init_repo(self, repo):
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        # Test repositories must never depend on a user's global Git config.
        subprocess.run(["git", "-C", str(repo), "config", "--local", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "--local", "user.name", "Test"], check=True)

    def _http_server(self, store):
        class TestHandler(Handler):
            pass
        TestHandler.store = store
        TestHandler.worker = None
        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, TestHandler

    def _request(self, server, path, method="GET"):
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request(method, path, body=b"{}" if method == "POST" else None, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, body

    def _real_repo_with_investigation(self, root, command):
        repo = root / "repo"
        repo.mkdir()
        self._init_repo(repo)
        (repo / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
        subprocess.run(["git", "-C", str(repo), "branch", "change_a"], check=True)
        subprocess.run(["git", "-C", str(repo), "branch", "change_b"], check=True)
        repo_info = {"id": "repo-async", **inspect_repository({"id": "repo-async", "path": str(repo)})}
        investigation = {"id": "investigation-async", "repository_id": repo_info["id"], "branch_a": "change_a", "branch_b": "change_b", "test_command": command, "status": "created", "created_at": "2026-01-01"}
        return repo_info, investigation

    def _stop_http_server(self, server, handler_type):
        server.shutdown()
        server.server_close()
        if handler_type.worker is not None:
            handler_type.worker.shutdown()

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
            self._init_repo(repo)
            (repo / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
            current = subprocess.check_output(["git", "-C", str(repo), "branch", "--show-current"], text=True).strip()
            subprocess.run(["git", "-C", str(repo), "branch", "change-a"], check=True)
            sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/remote-only", sha], check=True)
            metadata = {"id": "r1", "path": str(repo)}
            inspected = inspect_repository(metadata, current, "change-a")
            self.assertEqual(inspected["branch_a"], current)
            self.assertIn("remote-only", inspected["branches"])
            result = run_branch(repo, "change-a", "python -m unittest discover -v")
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["exit_code"], 0)
            remote_result = run_branch(repo, "remote-only", "python -m unittest discover -v")
            self.assertEqual(remote_result["status"], "passed")
            self.assertEqual(remote_result["exit_code"], 0)

    def test_sample_workflow_keeps_confirmed_semantics(self):
        result = analysis()
        self.assertEqual(result["summary"]["confirmed"], 1)
        self.assertEqual(result["tests"]["change_a"]["exit_code"], 0)
        self.assertNotEqual(result["tests"]["combined"]["exit_code"], 0)

    def test_command_results_distinguish_failure_missing_command_and_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failed = run_command(root, "python -c \"import sys; sys.exit(3)\"")
            missing = run_command(root, "mergeproof-command-that-does-not-exist")
            timeout = run_command(root, "python -c \"import time; time.sleep(2)\"", timeout=0.05)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["result_type"], "execution_error")
        self.assertEqual(missing["status"], "missing_command")
        self.assertEqual(timeout["status"], "timeout")
        for result in (failed, missing, timeout):
            self.assertLessEqual(result["started_at"], result["finished_at"])

    def test_command_results_classify_syntax_import_and_execution_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            syntax = run_command(root, "python -c \"raise SyntaxError('bad')\"")
            dependency = run_command(root, "python -c \"raise ModuleNotFoundError('x')\"")
            execution = run_command(root, "python -c \"raise RuntimeError('boom')\"")
        self.assertEqual(syntax["result_type"], "syntax_error")
        self.assertEqual(dependency["result_type"], "dependency_error")
        self.assertEqual(execution["result_type"], "execution_error")

    def test_assertion_evidence_is_required_for_test_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assertion = run_command(root, "python -c \"raise AssertionError('bad')\"")
            runtime = run_command(root, "python -c \"raise RuntimeError('bad')\"")
            runtime_with_assertion_word = run_command(root, "python -c \"raise RuntimeError('AssertionError mentioned as data')\"")
        self.assertEqual(assertion["result_type"], "test_failure")
        self.assertEqual(runtime["result_type"], "execution_error")
        self.assertEqual(runtime_with_assertion_word["result_type"], "execution_error")

    def test_all_passing_findings_do_not_claim_generic_risk(self):
        runs = [{"status": "passed"}, {"status": "passed"}, {"status": "passed"}]
        findings = report_findings(False, runs=runs)
        self.assertEqual(findings[0]["title"], "No conflict detected by these checks")
        self.assertNotIn("Likely Risk", {finding["classification"] for finding in findings})

    def test_evidence_bundle_contains_report_logs_and_reproduction(self):
        bundle = evidence_bundle({"id": "r1", "investigation_id": "i1", "execution_id": "e1", "runs": [{"role": "combined", "command": "python -m unittest", "status": "failed", "full_log": "AssertionError"}], "findings": [{"source_refs": [{"kind": "Change A", "text": "diff --git"}]}]})
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            names = set(archive.namelist())
            self.assertIn("report.json", names)
            self.assertIn("REPRODUCE.md", names)
            self.assertIn("logs/1-combined.log", names)
            self.assertIn("sources/finding-1-1.diff", names)

    def test_store_marks_running_investigations_interrupted_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            store = Store(path)
            store.save("investigations", {"id": "running", "status": "running"})
            store.save("investigations", {"id": "queued", "status": "queued"})
            restarted = Store(path)
        self.assertEqual(restarted.find("investigations", "running")["status"], "interrupted")
        self.assertEqual(restarted.find("investigations", "queued")["status"], "interrupted")

    def test_background_start_returns_promptly_and_persists_progress_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "data.json"
            store = Store(path)
            repo, investigation = self._real_repo_with_investigation(root, 'python -c "import time; time.sleep(0.3)"')
            store.save("repositories", repo)
            store.save("investigations", investigation)
            server, _, handler_type = self._http_server(store)
            try:
                started = time.monotonic()
                status, response = self._request(server, "/api/investigations/investigation-async/start", "POST")
                elapsed = time.monotonic() - started
                self.assertEqual(status, 202)
                self.assertEqual(response["status"], "queued")
                self.assertLess(elapsed, 0.5)
                saved = store.find("investigations", "investigation-async")
                self.assertEqual(saved["progress"]["total"], 3)
                self.assertEqual(saved["progress_percent"], 0)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    _, progress = self._request(server, "/api/investigations/investigation-async/progress")
                    if progress["status"] == "completed":
                        break
                    time.sleep(0.05)
                self.assertEqual(progress["status"], "completed")
                self.assertEqual(progress["progress"], {"completed": 3, "total": 3, "percent": 100, "label": "3 of 3 complete", "current_role": None})
                persisted = Store(path)
                self.assertEqual(persisted.find("investigations", "investigation-async")["progress_percent"], 100)
                history_status, history = self._request(server, "/api/investigations/investigation-async/history")
                self.assertEqual(history_status, 200)
                self.assertEqual(len(history["executions"]), 1)
                self.assertEqual(len(history["executions"][0]["runs"]), 3)
                _, runs = self._request(server, "/api/investigations/investigation-async/runs?history=1")
                self.assertEqual(len(runs["runs"]), 3)
            finally:
                self._stop_http_server(server, handler_type)

    def test_background_investigation_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "data.json")
            repo, investigation = self._real_repo_with_investigation(root, 'python -c "import time; time.sleep(5)"')
            store.save("repositories", repo)
            store.save("investigations", investigation)
            server, _, handler_type = self._http_server(store)
            try:
                self.assertEqual(self._request(server, "/api/investigations/investigation-async/start", "POST")[0], 202)
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    _, progress = self._request(server, "/api/investigations/investigation-async/progress")
                    if progress["status"] == "running":
                        break
                    time.sleep(0.05)
                self.assertEqual(progress["status"], "running")
                cancel_status, cancelled = self._request(server, "/api/investigations/investigation-async/cancel", "POST")
                self.assertEqual(cancel_status, 200)
                self.assertEqual(cancelled["status"], "cancelled")
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    _, progress = self._request(server, "/api/investigations/investigation-async/progress")
                    if progress["status"] == "cancelled" and progress.get("finished_at"):
                        break
                    time.sleep(0.05)
                self.assertEqual(progress["status"], "cancelled")
                _, runs = self._request(server, "/api/investigations/investigation-async/runs?history=1")
                self.assertTrue(all(run["status"] == "cancelled" for run in runs["runs"]))
            finally:
                self._stop_http_server(server, handler_type)

    def test_interrupted_investigation_can_be_restarted_after_store_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "data.json"
            store = Store(path)
            repo, investigation = self._real_repo_with_investigation(root, 'python -c "pass"')
            store.save("repositories", repo)
            store.save("investigations", investigation)
            store.update("investigations", investigation["id"], status="running")
            restarted = Store(path)
            self.assertEqual(restarted.find("investigations", investigation["id"])["status"], "interrupted")
            server, _, handler_type = self._http_server(restarted)
            try:
                status, response = self._request(server, "/api/investigations/investigation-async/start", "POST")
                self.assertEqual(status, 202)
                self.assertIn(response["status"], ("queued", "running"))
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    _, progress = self._request(server, "/api/investigations/investigation-async/progress")
                    if progress["status"] == "completed":
                        break
                    time.sleep(0.05)
                self.assertEqual(progress["status"], "completed")
            finally:
                self._stop_http_server(server, handler_type)

    def test_corrupt_storage_is_preserved_and_recovery_is_actionable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            original = b'{"repositories": [broken'
            path.write_bytes(original)
            with self.assertRaisesRegex(RuntimeError, "corrupted.*backup") as raised:
                Store(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(path.with_name("data.json.corrupt-backup").read_bytes(), original)
            self.assertIn("restore", str(raised.exception))

    def test_collection_status_filter_and_zero_test_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "data.json")
            store.save("investigations", {"id": "done", "status": "completed"})
            store.save("investigations", {"id": "new", "status": "created"})
            result = collection_view(store, "investigations", {"status": ["completed"]})
            zero = run_command(Path(directory), "python -c \"print('Ran 0 tests')\"")
        self.assertEqual([item["id"] for item in result["investigations"]], ["done"])
        self.assertEqual(zero["test_count"], 0)
        self.assertTrue(zero["zero_tests"])

    def test_confirmation_requires_tests_to_have_run(self):
        runs = [{"role": "independent_a", "status": "passed", "test_count": 0},
                {"role": "independent_b", "status": "passed", "test_count": 1},
                {"role": "combined", "status": "failed", "result_type": "test_failure", "test_count": 1, "log": "assert"}]
        self.assertFalse(_confirmed(runs))

    def test_overview_and_collection_view_support_counts_search_and_pagination(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "data.json")
            store.save("investigations", {"id": "one", "status": "completed", "created_at": "2026-01-01"})
            store.save("investigations", {"id": "two", "status": "created", "created_at": "2026-01-02"})
            finding = {"classification": "Confirmed Conflict", "title": "same", "source_refs": [{"file": "service.py", "sha": "abc"}]}
            store.save("reports", {"id": "r1", "summary": {"confirmed": 1}, "findings": [finding]})
            store.save("reports", {"id": "r2", "summary": {"confirmed": 1}, "findings": [finding]})
            result = collection_view(store, "investigations", {"search": ["one"], "page": ["1"], "page_size": ["1"]})
            dashboard = overview(store)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["investigations"][0]["id"], "one")
        self.assertEqual(dashboard["counts"]["investigations"], 2)
        self.assertEqual(dashboard["counts"]["completed"], 1)
        self.assertEqual(dashboard["unique_confirmed_findings"], 1)

    def test_same_branch_is_rejected_and_merge_conflict_is_not_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            self._init_repo(repo)
            (repo / "value.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
            subprocess.run(["git", "-C", str(repo), "branch", "change-a"], check=True)
            subprocess.run(["git", "-C", str(repo), "branch", "change-b"], check=True)
            subprocess.run(["git", "-C", str(repo), "branch", "mergeproof-analysis"], check=True)
            subprocess.run(["git", "-C", str(repo), "checkout", "-q", "change-a"], check=True)
            (repo / "value.txt").write_text("a\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "a"], check=True)
            subprocess.run(["git", "-C", str(repo), "checkout", "-q", "change-b"], check=True)
            (repo / "value.txt").write_text("b\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "b"], check=True)
            base = subprocess.check_output(["git", "-C", str(repo), "merge-base", "change-a", "change-b"], text=True).strip()
            conflict = run_combined(repo, "change-a", "change-b", base, "python -m unittest")
            with self.assertRaisesRegex(ValueError, "different"):
                inspect_repository({"id": "r", "path": str(repo)}, "change-a", "change-a")
        self.assertEqual(conflict["status"], "merge_conflict")
        conflict["role"] = "combined"
        self.assertFalse(_confirmed([{"role": "independent_a", "status": "passed"}, {"role": "independent_b", "status": "passed"}, conflict]))

    def test_combined_success_uses_clean_snapshot_and_existing_helper_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            self._init_repo(repo)
            (repo / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
            current = subprocess.check_output(["git", "-C", str(repo), "branch", "--show-current"], text=True).strip()
            subprocess.run(["git", "-C", str(repo), "branch", "change-a"], check=True)
            subprocess.run(["git", "-C", str(repo), "branch", "change-b"], check=True)
            subprocess.run(["git", "-C", str(repo), "branch", "mergeproof-analysis"], check=True)
            base = subprocess.check_output(["git", "-C", str(repo), "merge-base", "change-a", "change-b"], text=True).strip()
            (repo / "untracked.txt").write_text("must not enter snapshot", encoding="utf-8")
            result = run_combined(repo, "change-a", "change-b", base, "python -m unittest discover -v")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["result_type"], "passed")

    def test_different_commits_merge_without_global_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            self._init_repo(repo)
            (repo / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
            subprocess.run(["git", "-C", str(repo), "branch", "change-a"], check=True)
            subprocess.run(["git", "-C", str(repo), "branch", "change-b"], check=True)
            subprocess.run(["git", "-C", str(repo), "checkout", "-q", "change-a"], check=True)
            (repo / "a.txt").write_text("A\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "change A"], check=True)
            subprocess.run(["git", "-C", str(repo), "checkout", "-q", "change-b"], check=True)
            (repo / "b.txt").write_text("B\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "change B"], check=True)
            base = subprocess.check_output(["git", "-C", str(repo), "merge-base", "change-a", "change-b"], text=True).strip()
            result = run_combined(repo, "change-a", "change-b", base, "python -m unittest discover -v")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["result_type"], "passed")


if __name__ == "__main__":
    unittest.main()
