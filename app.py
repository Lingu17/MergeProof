from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
DATA_FILE = Path(os.environ.get("MERGEPROOF_DATA_FILE", ROOT / "mergeproof-data.json"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path = DATA_FILE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self):
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    return {key: value.get(key, []) for key in ("repositories", "investigations", "test_runs", "reports")}
            except (OSError, json.JSONDecodeError):
                pass
        sample = {"id": "sample", "name": "checkout-service (sample)", "path": str(ROOT / "demo_repo"),
                  "sample": True, "valid": True, "created_at": now()}
        result = {"repositories": [sample], "investigations": [], "test_runs": [], "reports": []}
        self._write(result)
        return result

    def _write(self, data=None):
        if data is not None:
            self.data = data
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def save(self, collection, value):
        self.data[collection].append(value)
        self._write()
        return value

    def find(self, collection, item_id):
        return next((item for item in self.data[collection] if item["id"] == item_id), None)

    def update(self, collection, item_id, **changes):
        item = self.find(collection, item_id)
        if item:
            item.update(changes)
            self._write()
        return item


def git(path, *args, check=True):
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)
    if check and result.returncode:
        raise ValueError((result.stderr or result.stdout).strip() or "git command failed")
    return result


def validate_repository(path: str) -> dict:
    location = Path(path).expanduser().resolve()
    if not location.is_dir():
        raise ValueError("repository path is not a directory")
    git(location, "rev-parse", "--is-inside-work-tree")
    branches = git(location, "for-each-ref", "refs/heads", "--format=%(refname:short)").stdout.splitlines()
    head = git(location, "branch", "--show-current", check=False).stdout.strip() or "HEAD"
    return {"path": str(location), "valid": True, "head": head, "branches": branches,
            "name": location.name, "checked_at": now()}


def inspect_repository(repo: dict, branch_a=None, branch_b=None) -> dict:
    info = validate_repository(repo["path"])
    info["repository_id"] = repo["id"]
    if branch_a and branch_b:
        for branch in (branch_a, branch_b):
            if branch not in info["branches"]:
                raise ValueError(f"unknown branch: {branch}")
        base = git(Path(info["path"]), "merge-base", branch_a, branch_b).stdout.strip()
        info.update({"branch_a": branch_a, "branch_b": branch_b, "common_base": base,
                     "common_base_short": base[:12]})
    return info


def run_command(cwd: Path, command: str) -> dict:
    try:
        args = shlex.split(command)
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=300)
        log = (result.stdout + result.stderr).strip()
        return {"command": command, "status": "passed" if result.returncode == 0 else "failed",
                "exit_code": result.returncode, "log": log, "full_log": log}
    except (OSError, subprocess.TimeoutExpired) as exc:
        log = str(exc)
        return {"command": command, "status": "failed", "exit_code": -1, "log": log, "full_log": log}


def run_branch(source: Path, branch: str, command: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="mergeproof-") as temporary:
        destination = Path(temporary) / "repo"
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        try:
            git(destination, "checkout", "--quiet", branch)
        except ValueError as exc:
            return {"command": command, "status": "failed", "exit_code": -1, "log": str(exc), "full_log": str(exc)}
        return run_command(destination, command)


def run_combined(source: Path, branch_a: str, branch_b: str, base: str, command: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="mergeproof-") as temporary:
        destination = Path(temporary) / "repo"
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        try:
            git(destination, "checkout", "--quiet", "-b", "mergeproof-analysis", base)
            merge = git(destination, "merge", "--no-commit", "--no-ff", branch_a, branch_b, check=False)
            if merge.returncode:
                git(destination, "merge", "--abort", check=False)
                log = (merge.stdout + merge.stderr).strip()
                return {"command": command, "status": "failed", "exit_code": merge.returncode,
                        "log": "Merge conflict before tests\n" + log, "full_log": "Merge conflict before tests\n" + log}
        except ValueError as exc:
            return {"command": command, "status": "failed", "exit_code": -1, "log": str(exc), "full_log": str(exc)}
        return run_command(destination, command)


def run_case(folder: str, test_file: str) -> dict:
    result = subprocess.run([sys.executable, "-m", "unittest", "-v", test_file], cwd=ROOT / "demo_repo" / folder,
                            capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    return {"status": "passed" if result.returncode == 0 else "failed", "exit_code": result.returncode, "output": output,
            "log": output, "full_log": output, "command": f"{sys.executable} -m unittest -v {test_file}"}


def analysis() -> dict:
    independent_a, independent_b, combined = run_case("change_a", "test_change_a.py"), run_case("change_b", "test_change_b.py"), run_case("combined", "test_combined.py")
    return {"repo": {"name": "checkout-service", "branch": "mergeproof/demo", "language": "Python 3"},
        "summary": {"confirmed": int(combined["status"] == "failed" and independent_a["status"] == "passed" and independent_b["status"] == "passed"), "risks": 1, "suggestions": 1, "confidence": 98},
        "progress": {"completed": 5, "total": 5, "label": "5 of 5 complete"}, "graph": {"nodes": 5, "edges": 5},
        "changes": [{"id": "change-a", "label": "Change A", "title": "Normalize checkout currency", "author": "Maya Chen", "sha": "a17c9e2", "files": 2, "description": "Moves currency normalization into the order boundary so downstream totals always use USD.", "color": "violet"}, {"id": "change-b", "label": "Change B", "title": "Add loyalty discount", "author": "Jon Bell", "sha": "b82fd11", "files": 2, "description": "Applies a member discount using the checkout subtotal before tax is calculated.", "color": "cyan"}],
        "tests": {"change_a": independent_a, "change_b": independent_b, "combined": combined}, "finding": {"title": "Discount is calculated against the pre-normalized subtotal", "severity": "High confidence", "why": "Change A creates the normalized USD value in `total_usd`. Change B still calculates its 10% discount from the pre-normalized `subtotal`, so the combined test returns 98.0 instead of the expected 97.2.", "evidence": [{"kind": "Change A", "file": "demo_repo/combined/service.py", "line": "3", "code": "order['total_usd'] = round(order['subtotal'] * rates[order['currency']], 2)"}, {"kind": "Change B", "file": "demo_repo/combined/service.py", "line": "4", "code": "discount = order['subtotal'] * 0.10 if order.get('loyalty') else 0"}, {"kind": "Test evidence", "file": "demo_repo/combined/test_combined.py", "line": "7", "code": "self.assertEqual(checkout({'subtotal': 100, 'currency': 'EUR', 'loyalty': True}), 97.2)"}], "trace": ["Order subtotal", "Normalize to total_usd", "Apply loyalty discount", "Checkout assertion"]}, "generated_at": "Live run · local Python subprocess"}


class Handler(BaseHTTPRequestHandler):
    store = Store()

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode(); self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def body(self):
        length = int(self.headers.get("Content-Length", 0)); return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path); parts = [p for p in parsed.path.split("/") if p]; query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/analysis": self.send_json(analysis()); return
            if parsed.path == "/api/repositories": self.send_json({"repositories": self.store.data["repositories"]}); return
            if len(parts) == 4 and parts[:2] == ["api", "repositories"] and parts[3] == "inspect":
                repo = self.store.find("repositories", parts[2]);
                if not repo: raise KeyError("repository not found")
                self.send_json(inspect_repository(repo, query.get("branch_a", [None])[0], query.get("branch_b", [None])[0])); return
            if len(parts) == 4 and parts[:2] == ["api", "repositories"] and parts[3] == "validate":
                repo = self.store.find("repositories", parts[2])
                if not repo: raise KeyError("repository not found")
                self.send_json({**repo, **validate_repository(repo["path"])}); return
            if parsed.path == "/api/investigations": self.send_json({"investigations": self.store.data["investigations"]}); return
            if len(parts) == 3 and parts[:2] == ["api", "investigations"]:
                item = self.store.find("investigations", parts[2]);
                if not item: raise KeyError("investigation not found")
                self.send_json(item); return
            if len(parts) == 4 and parts[:2] == ["api", "investigations"] and parts[3] == "runs":
                if not self.store.find("investigations", parts[2]): raise KeyError("investigation not found")
                self.send_json({"runs": [x for x in self.store.data["test_runs"] if x["investigation_id"] == parts[2]]}); return
            if len(parts) == 3 and parts[:2] == ["api", "test-runs"]:
                run = self.store.find("test_runs", parts[2])
                if not run: raise KeyError("test run not found")
                self.send_json(run); return
            if len(parts) == 4 and parts[:2] == ["api", "investigations"] and parts[3] == "report":
                report = next((x for x in self.store.data["reports"] if x["investigation_id"] == parts[2]), None)
                if not report: raise KeyError("report not found")
                if query.get("download") == ["1"]:
                    body = json.dumps(report, indent=2).encode(); self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Disposition", 'attachment; filename="mergeproof-report.json"'); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
                self.send_json(report); return
            if len(parts) == 4 and parts[:2] == ["api", "reports"] and parts[3] == "download":
                report = self.store.find("reports", parts[2])
                if not report: raise KeyError("report not found")
                body = json.dumps(report, indent=2).encode(); self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Disposition", 'attachment; filename="mergeproof-report.json"'); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            if parsed.path in ("/", "/index.html"):
                body = (ROOT / "web" / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            self.send_json({"error": "Not found"}, 404); return
        except (ValueError, KeyError) as exc: self.send_json({"error": str(exc)}, 400 if isinstance(exc, ValueError) else 404)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path); parts = [p for p in parsed.path.split("/") if [] or p]
        try:
            payload = self.body()
            if parsed.path == "/api/repositories/validate":
                self.send_json(validate_repository(payload["path"])); return
            if parsed.path == "/api/repositories":
                info = validate_repository(payload["path"]); item = {"id": str(uuid.uuid4()), **info, "sample": False, "created_at": now()}; self.store.save("repositories", item); self.send_json(item, 201); return
            if parsed.path == "/api/investigations":
                repo = self.store.find("repositories", payload["repository_id"]); 
                if not repo: raise KeyError("repository not found")
                inspect_repository(repo, payload["branch_a"], payload["branch_b"])
                item = {"id": str(uuid.uuid4()), "repository_id": repo["id"], "branch_a": payload["branch_a"], "branch_b": payload["branch_b"], "test_command": payload.get("test_command", "python -m unittest discover -v"), "status": "created", "created_at": now()}; self.store.save("investigations", item); self.send_json(item, 201); return
            if len(parts) == 4 and parts[:2] == ["api", "investigations"] and parts[3] == "start":
                investigation = self.store.find("investigations", parts[2]);
                if not investigation: raise KeyError("investigation not found")
                repo = self.store.find("repositories", investigation["repository_id"]); details = inspect_repository(repo, investigation["branch_a"], investigation["branch_b"]); command = investigation["test_command"]
                runs = [("independent_a", run_branch(Path(repo["path"]), investigation["branch_a"], command)), ("independent_b", run_branch(Path(repo["path"]), investigation["branch_b"], command)), ("combined", run_combined(Path(repo["path"]), investigation["branch_a"], investigation["branch_b"], details["common_base"], command))]
                run_ids = []
                for role, run in runs: run_ids.append(self.store.save("test_runs", {"id": str(uuid.uuid4()), "investigation_id": investigation["id"], "role": role, "started_at": now(), **run})["id"])
                report = {"id": str(uuid.uuid4()), "investigation_id": investigation["id"], "runs": [self.store.find("test_runs", x) for x in run_ids], "summary": {"confirmed": int(runs[0][1]["status"] == "passed" and runs[1][1]["status"] == "passed" and runs[2][1]["status"] == "failed")}, "generated_at": now()}; self.store.save("reports", report); self.store.update("investigations", investigation["id"], status="completed", run_ids=run_ids, report_id=report["id"], completed_at=now()); self.send_json(report); return
            if len(parts) == 4 and parts[:2] == ["api", "investigations"] and parts[3] == "report":
                if not self.store.find("investigations", parts[2]): raise KeyError("investigation not found")
                report = {"id": str(uuid.uuid4()), "investigation_id": parts[2], "data": payload, "generated_at": now()}; self.store.save("reports", report); self.send_json(report, 201); return
            self.send_json({"error": "Not found"}, 404)
        except (ValueError, KeyError, json.JSONDecodeError) as exc: self.send_json({"error": str(exc)}, 400)

    def log_message(self, *_args): pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8000), Handler); print("MergeProof running at http://localhost:8000")
    try: server.serve_forever()
    except KeyboardInterrupt: server.server_close()
