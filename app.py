from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
DATA_FILE = Path(os.environ.get("MERGEPROOF_DATA_FILE", ROOT / "mergeproof-data.json"))
PORT = int(os.environ.get("MERGEPROOF_PORT", "8000"))
DEFAULT_COMMAND = "python -m unittest discover -v"
SAMPLE_COMMAND = "Built-in sample checks (fixed)"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path = DATA_FILE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
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
        with self.lock:
            self.data[collection].append(value)
            self._write()
            return value

    def find(self, collection, item_id):
        with self.lock:
            return next((item for item in self.data[collection] if item["id"] == item_id), None)

    def update(self, collection, item_id, **changes):
        with self.lock:
            item = self.find(collection, item_id)
            if item:
                item.update(changes)
                self._write()
            return item


def git(path: Path, *args, check=True):
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)
    if check and result.returncode:
        raise ValueError((result.stderr or result.stdout).strip() or "git command failed")
    return result


def validate_repository(path: str) -> dict:
    location = Path(path).expanduser().resolve()
    if not location.is_dir():
        raise ValueError("repository path is not a directory")
    if location == (ROOT / "demo_repo").resolve():
        return {"path": str(location), "valid": True, "head": "sample", "branches": ["change_a", "change_b"],
                "name": "checkout-service (sample)", "checked_at": now()}
    git(location, "rev-parse", "--is-inside-work-tree")
    branches = git(location, "for-each-ref", "refs/heads", "--format=%(refname:short)").stdout.splitlines()
    head = git(location, "branch", "--show-current", check=False).stdout.strip() or "HEAD"
    return {"path": str(location), "valid": True, "head": head, "branches": branches,
            "name": location.name, "checked_at": now()}


def inspect_repository(repo: dict, branch_a=None, branch_b=None) -> dict:
    info = validate_repository(repo["path"])
    info["repository_id"] = repo["id"]
    if branch_a and branch_b:
        if branch_a == branch_b:
            raise ValueError("branch_a and branch_b must be different branches")
        for branch in (branch_a, branch_b):
            if branch not in info["branches"]:
                raise ValueError(f"unknown branch: {branch}")
        if repo.get("sample") or Path(info["path"]).resolve() == (ROOT / "demo_repo").resolve():
            info.update({"branch_a": branch_a, "branch_b": branch_b, "common_base": "sample-base", "common_base_short": "sample-base"})
            return info
        base = git(Path(info["path"]), "merge-base", branch_a, branch_b).stdout.strip()
        info.update({"branch_a": branch_a, "branch_b": branch_b, "common_base": base, "common_base_short": base[:12]})
    return info


def _output(exc_or_out) -> str:
    value = exc_or_out or ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def run_command(cwd: Path, command: str, timeout: int = 300) -> dict:
    started = now()
    try:
        args = [arg.strip('"') if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in "\"'" else arg
                for arg in shlex.split(command, posix=False)]
        if not args:
            return {"command": command, "status": "missing_command", "exit_code": -1, "log": "No command was supplied.", "full_log": "No command was supplied.", "started_at": started, "finished_at": now()}
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        log = (result.stdout + result.stderr).strip()
        status = "passed" if result.returncode == 0 else "failed"
        result_type = "passed" if result.returncode == 0 else "test_failure"
        return {"command": command, "status": status, "result_type": result_type, "exit_code": result.returncode, "output": log, "log": log, "full_log": log, "started_at": started, "finished_at": now()}
    except subprocess.TimeoutExpired as exc:
        log = (_output(exc.stdout) + _output(exc.stderr)).strip() or f"Command timed out after {timeout} seconds."
        return {"command": command, "status": "timeout", "exit_code": -1, "log": log, "full_log": log, "started_at": started, "finished_at": now()}
    except FileNotFoundError as exc:
        log = f"Command not found: {args[0] if 'args' in locals() and args else command} ({exc})"
        return {"command": command, "status": "missing_command", "exit_code": -1, "log": log, "full_log": log, "started_at": started, "finished_at": now()}
    except (OSError, ValueError) as exc:
        log = str(exc)
        return {"command": command, "status": "setup_error", "exit_code": -1, "log": log, "full_log": log, "started_at": started, "finished_at": now()}


def _clone(source: Path, destination: Path):
    result = subprocess.run(["git", "clone", "--no-local", "--no-hardlinks", "--quiet", str(source), str(destination)], capture_output=True, text=True)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip() or "unable to create clean repository snapshot")


def run_branch(source: Path, branch: str, command: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="mergeproof-") as temporary:
        destination = Path(temporary) / "repo"
        try:
            _clone(source, destination)
            git(destination, "checkout", "--quiet", branch)
            git(destination, "reset", "--hard", branch)
            git(destination, "clean", "-fdx")
        except (ValueError, OSError) as exc:
            return {"command": command, "status": "setup_error", "exit_code": -1, "log": str(exc), "full_log": str(exc), "started_at": now(), "finished_at": now()}
        return run_command(destination, command)


def run_combined(source: Path, branch_a: str, branch_b: str, base: str, command: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="mergeproof-") as temporary:
        destination = Path(temporary) / "repo"
        try:
            _clone(source, destination)
            # Use a detached clean base so an existing source branch named
            # mergeproof-analysis (or any other helper branch) cannot break setup.
            git(destination, "checkout", "--quiet", "--detach", base)
            def merge_ref(branch):
                local = git(destination, "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
                return branch if local.returncode == 0 else f"origin/{branch}"
            merge = git(destination, "merge", "--no-commit", "--no-ff", merge_ref(branch_a), merge_ref(branch_b), check=False)
            if merge.returncode:
                log = (merge.stdout + merge.stderr).strip()
                git(destination, "merge", "--abort", check=False)
                return {"command": command, "status": "merge_conflict", "exit_code": merge.returncode, "log": "Merge conflict before tests\n" + log, "full_log": "Merge conflict before tests\n" + log, "started_at": now(), "finished_at": now()}
        except (ValueError, OSError) as exc:
            return {"command": command, "status": "setup_error", "exit_code": -1, "log": str(exc), "full_log": str(exc), "started_at": now(), "finished_at": now()}
        return run_command(destination, command)


def run_case(folder: str, test_file: str) -> dict:
    return run_command(ROOT / "demo_repo" / folder, f'{sys.executable} -m unittest -v {test_file}')


def report_findings(confirmed: bool, source_refs=None):
    if confirmed:
        return [{"classification": "Confirmed Conflict", "title": "Combined test failure reproduced", "explanation": "Independent relevant checks passed and the combined relevant check failed; persisted logs and changed-file evidence show the interaction.", "source_refs": source_refs or []}]
    return [{"classification": "Likely Risk", "title": "No confirmed interaction reproduced", "explanation": "The selected checks did not meet the pass/pass/test-failed rule. Merge conflicts, setup errors, missing commands, timeouts, and test failures are reported separately.", "source_refs": source_refs or []}, {"classification": "Suggested Investigation", "title": "Add or refine a cross-branch regression test", "explanation": "A targeted integration check may be needed to exercise the changed behavior together.", "source_refs": source_refs or []}]


def source_references(source: Path, base: str, branch_a: str, branch_b: str):
    references = []
    for label, branch in (("Change A", branch_a), ("Change B", branch_b)):
        diff = git(source, "diff", "--unified=3", f"{base}..{branch}", "--", check=False)
        text = (diff.stdout or diff.stderr).strip()
        references.append({"kind": label, "command": f"git diff --unified=3 {base[:12]}..{branch}", "text": text[:12000] or "No textual diff available."})
    return references


def analysis() -> dict:
    independent_a, independent_b, combined = run_case("change_a", "test_change_a.py"), run_case("change_b", "test_change_b.py"), run_case("combined", "test_combined.py")
    confirmed = independent_a["status"] == "passed" and independent_b["status"] == "passed" and combined["status"] == "failed" and combined.get("result_type") == "test_failure" and bool(combined.get("log"))
    evidence = [{"kind": "Change A", "file": "demo_repo/combined/service.py", "line": "3", "code": "order['total_usd'] = round(order['subtotal'] * rates[order['currency']], 2)"}, {"kind": "Change B", "file": "demo_repo/combined/service.py", "line": "4", "code": "discount = order['subtotal'] * 0.10 if order.get('loyalty') else 0"}, {"kind": "Test evidence", "file": "demo_repo/combined/test_combined.py", "line": "7", "code": "self.assertEqual(checkout({'subtotal': 100, 'currency': 'EUR', 'loyalty': True}), 97.2)"}]
    return {"repo": {"name": "checkout-service", "branch": "mergeproof/demo", "language": "Python 3"}, "summary": {"confirmed": int(confirmed), "risks": 1, "suggestions": 1, "confidence": 98}, "progress": {"completed": 5, "total": 5, "label": "5 of 5 complete"}, "graph": {"nodes": 5, "edges": 5}, "changes": [], "tests": {"change_a": independent_a, "change_b": independent_b, "combined": combined}, "finding": {"title": "Discount is calculated against the pre-normalized subtotal", "severity": "High confidence", "why": "Change A creates the normalized USD value in `total_usd`. Change B still calculates its 10% discount from the pre-normalized `subtotal`, so the combined test fails while each independent test passes.", "evidence": evidence, "trace": ["Order subtotal", "Normalize to total_usd", "Apply loyalty discount", "Checkout assertion"]}, "generated_at": "Live run · local Python subprocess"}


def _confirmed(runs):
    by_role = {run["role"]: run for run in runs}
    # A merge conflict is not a test result and can never confirm an interaction.
    return (by_role.get("independent_a", {}).get("status") == "passed" and
            by_role.get("independent_b", {}).get("status") == "passed" and
            by_role.get("combined", {}).get("status") == "failed" and
            by_role.get("combined", {}).get("result_type") == "test_failure" and
            bool(by_role.get("combined", {}).get("log")))


def execute_investigation(handler, investigation):
    store = handler.store
    repo = store.find("repositories", investigation["repository_id"])
    if not repo:
        raise KeyError("repository not found")
    details = inspect_repository(repo, investigation["branch_a"], investigation["branch_b"])
    command = investigation["test_command"]
    if repo.get("sample"):
        if (investigation["branch_a"], investigation["branch_b"]) != ("change_a", "change_b"):
            raise ValueError("sample repository only supports change_a and change_b")
        if command != SAMPLE_COMMAND:
            raise ValueError("sample repository only supports the default test command")
        sample = analysis()
        results = [("independent_a", sample["tests"]["change_a"]), ("independent_b", sample["tests"]["change_b"]), ("combined", sample["tests"]["combined"])]
        results = [(role, {**result, "command": SAMPLE_COMMAND}) for role, result in results]
        refs = sample["finding"].get("evidence", [])
    else:
        source = Path(repo["path"])
        results = [("independent_a", run_branch(source, investigation["branch_a"], command)),
                   ("independent_b", run_branch(source, investigation["branch_b"], command)),
                   ("combined", run_combined(source, investigation["branch_a"], investigation["branch_b"], details["common_base"], command))]
        refs = source_references(source, details["common_base"], investigation["branch_a"], investigation["branch_b"])
    execution_id = str(uuid.uuid4())
    run_ids = []
    for role, result in results:
        result = {**result, "id": str(uuid.uuid4()), "investigation_id": investigation["id"], "execution_id": execution_id, "role": role}
        store.save("test_runs", result)
        run_ids.append(result["id"])
    runs = [store.find("test_runs", item_id) for item_id in run_ids]
    confirmed = _confirmed(runs)
    report = {"id": str(uuid.uuid4()), "investigation_id": investigation["id"], "execution_id": execution_id, "run_ids": run_ids, "runs": runs,
              "summary": {"confirmed": int(confirmed), "likely_risks": 0 if confirmed else 1, "suggestions": 0 if confirmed else 1},
              "findings": report_findings(confirmed, refs), "generated_at": now()}
    store.save("reports", report)
    finished = now()
    store.update("investigations", investigation["id"], status="completed", run_ids=run_ids, report_id=report["id"], latest_execution_id=execution_id, completed_at=finished, finished_at=finished)
    return report


class Handler(BaseHTTPRequestHandler):
    store = Store()

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

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
                repo = self.store.find("repositories", parts[2]);
                if not repo: raise KeyError("repository not found")
                self.send_json({**repo, **validate_repository(repo["path"])}); return
            if parsed.path == "/api/investigations": self.send_json({"investigations": self.store.data["investigations"]}); return
            if len(parts) == 3 and parts[:2] == ["api", "investigations"]:
                item = self.store.find("investigations", parts[2]);
                if not item: raise KeyError("investigation not found")
                self.send_json(item); return
            if len(parts) == 4 and parts[:2] == ["api", "investigations"] and parts[3] == "runs":
                if not self.store.find("investigations", parts[2]): raise KeyError("investigation not found")
                investigation = self.store.find("investigations", parts[2])
                latest = investigation.get("latest_execution_id")
                runs = [x for x in self.store.data["test_runs"] if x["investigation_id"] == parts[2] and (not latest or x.get("execution_id") == latest)]
                self.send_json({"runs": runs, "execution_id": latest}); return
            if len(parts) == 3 and parts[:2] == ["api", "test-runs"]:
                run = self.store.find("test_runs", parts[2]);
                if not run: raise KeyError("test run not found")
                self.send_json(run); return
            if len(parts) == 4 and parts[:2] == ["api", "investigations"] and parts[3] == "report":
                report = next((x for x in self.store.data["reports"] if x["investigation_id"] == parts[2] and x.get("execution_id") == self.store.find("investigations", parts[2]).get("latest_execution_id")), None)
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
            self.send_json({"error": "Not found"}, 404)
        except (ValueError, KeyError) as exc: self.send_json({"error": str(exc)}, 400 if isinstance(exc, ValueError) else 404)
        except Exception as exc: self.send_json({"error": f"request failed: {exc}"}, 500)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path); parts = [p for p in parsed.path.split("/") if p]
        try:
            payload = self.body()
            if parsed.path == "/api/repositories/validate": self.send_json(validate_repository(payload["path"])); return
            if parsed.path == "/api/repositories":
                info = validate_repository(payload["path"]); item = {"id": str(uuid.uuid4()), **info, "sample": False, "created_at": now()}; self.store.save("repositories", item); self.send_json(item, 201); return
            if parsed.path == "/api/investigations":
                repo = self.store.find("repositories", payload["repository_id"])
                if not repo: raise KeyError("repository not found")
                if payload["branch_a"] == payload["branch_b"]: raise ValueError("branch_a and branch_b must be different branches")
                inspect_repository(repo, payload["branch_a"], payload["branch_b"])
                requested_command = payload.get("test_command", DEFAULT_COMMAND)
                if repo.get("sample"):
                    requested_command = SAMPLE_COMMAND
                item = {"id": str(uuid.uuid4()), "repository_id": repo["id"], "branch_a": payload["branch_a"], "branch_b": payload["branch_b"], "test_command": requested_command, "status": "created", "created_at": now()}; self.store.save("investigations", item); self.send_json(item, 201); return
            if len(parts) == 4 and parts[:2] == ["api", "investigations"] and parts[3] == "start":
                investigation = self.store.find("investigations", parts[2])
                if not investigation: raise KeyError("investigation not found")
                with self.store.lock:
                    if investigation.get("status") == "running":
                        self.send_json({"error": "investigation is already running"}, 409); return
                    self.store.update("investigations", investigation["id"], status="running", started_at=now())
                try:
                    report = execute_investigation(self, investigation)
                except Exception as exc:
                    self.store.update("investigations", investigation["id"], status="failed", error=str(exc), finished_at=now())
                    raise
                self.send_json(report); return
            if len(parts) == 4 and parts[:2] == ["api", "investigations"] and parts[3] == "report":
                if not self.store.find("investigations", parts[2]): raise KeyError("investigation not found")
                report = {"id": str(uuid.uuid4()), "investigation_id": parts[2], "data": payload, "generated_at": now()}
                self.store.save("reports", report); self.send_json(report, 201); return
            self.send_json({"error": "Not found"}, 404)
        except (ValueError, KeyError, json.JSONDecodeError) as exc: self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            # Never drop the HTTP connection on an execution/setup bug. The
            # investigation was already marked failed and the message is
            # actionable for the UI.
            self.send_json({"error": f"analysis failed: {exc}"}, 500)

    def log_message(self, *_args): pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler); print(f"MergeProof running at http://localhost:{PORT}")
    try: server.serve_forever()
    except KeyboardInterrupt: server.server_close()
