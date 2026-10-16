from __future__ import annotations

import json
import os
import re
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
BIND_HOST = os.environ.get("MERGEPROOF_BIND_HOST", "127.0.0.1")
DEFAULT_COMMAND = "python -m unittest discover -v"
SAMPLE_COMMAND = "Built-in sample checks (fixed)"
API_VERSION = "2026.10.1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path = DATA_FILE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.data = self._load()
        # A process cannot resume a subprocess after restart.  Make abandoned
        # work explicit while retaining all runs/logs already persisted.
        for investigation in self.data["investigations"]:
            if investigation.get("status") == "running":
                investigation.update({"status": "interrupted", "error": "backend restarted while investigation was running", "finished_at": now()})
        self._write()

    def _load(self):
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                collections = ("repositories", "investigations", "test_runs", "reports")
                if isinstance(value, dict) and all(isinstance(value.get(key, []), list) for key in collections):
                    return {key: value.get(key, []) for key in collections}
                raise ValueError("top-level value must be an object containing array collections")
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                backup = self.path.with_name(self.path.name + ".corrupt-backup")
                try:
                    shutil.copy2(self.path, backup)
                except OSError as backup_exc:
                    raise RuntimeError(f"storage file is corrupted ({exc}); original could not be backed up: {backup_exc}") from exc
                raise RuntimeError(f"storage file is corrupted ({exc}); original preserved at {self.path}. Back up or remove it, then restore from {backup} if needed") from exc
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
    # Include branches that exist only on a remote-tracking ref.  They are
    # valid investigation inputs even when the working copy has no local head.
    branches = git(location, "for-each-ref", "refs/heads", "refs/remotes", "--format=%(refname)").stdout.splitlines()
    normalized = set()
    for ref in branches:
        if ref == "refs/remotes/origin/HEAD" or ref.endswith("/HEAD"):
            continue
        name = ref.removeprefix("refs/heads/").removeprefix("refs/remotes/origin/")
        if name and name != "HEAD":
            normalized.add(name)
    branches = sorted(normalized)
    head = git(location, "branch", "--show-current", check=False).stdout.strip() or "HEAD"
    return {"path": str(location), "valid": True, "head": head, "head_sha": git(location, "rev-parse", "HEAD").stdout.strip(), "branches": branches,
            "name": location.name, "checked_at": now()}


def inspect_repository(repo: dict, branch_a=None, branch_b=None) -> dict:
    info = validate_repository(repo["path"])
    info["repository_id"] = repo["id"]
    if (branch_a is not None and (not isinstance(branch_a, str) or not branch_a.strip())) or (branch_b is not None and (not isinstance(branch_b, str) or not branch_b.strip())):
        raise ValueError("branch_a and branch_b must be non-empty strings")
    if branch_a and branch_b:
        if branch_a == branch_b:
            raise ValueError("branch_a and branch_b must be different branches")
        for branch in (branch_a, branch_b):
            if branch not in info["branches"]:
                raise ValueError(f"unknown branch: {branch}")
        if repo.get("sample") or Path(info["path"]).resolve() == (ROOT / "demo_repo").resolve():
            info.update({"branch_a": branch_a, "branch_b": branch_b, "common_base": "sample-base", "common_base_short": "sample-base"})
            return info
        source = Path(info["path"])
        base = git(source, "merge-base", _git_ref(source, branch_a), _git_ref(source, branch_b)).stdout.strip()
        info.update({"branch_a": branch_a, "branch_b": branch_b, "branch_a_sha": _sha(source, branch_a), "branch_b_sha": _sha(source, branch_b), "common_base": base, "common_base_short": base[:12]})
    return info


def _output(exc_or_out) -> str:
    value = exc_or_out or ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _test_evidence(log: str) -> dict:
    names = sorted(set(re.findall(r"(?:^|\s)(test_[A-Za-z0-9_]+)(?:\s|$)", log or "")))
    match = re.search(r"Ran\s+(\d+)\s+tests?", log or "", re.I)
    if match:
        count = int(match.group(1))
    else:
        passed = re.search(r"(\d+)\s+passed", log or "", re.I)
        failed = re.search(r"(\d+)\s+failed", log or "", re.I)
        count = (int(passed.group(1)) if passed else 0) + (int(failed.group(1)) if failed else 0)
    return {"test_names": names, "test_count": count, "zero_tests": count == 0}


def run_command(cwd: Path, command: str, timeout: int = 300) -> dict:
    started = now()
    try:
        args = [arg.strip('"') if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in "\"'" else arg
                for arg in shlex.split(command, posix=False)]
        if not args:
            return {"command": command, "status": "missing_command", "exit_code": -1, "log": "No command was supplied.", "full_log": "No command was supplied.", **_test_evidence(""), "started_at": started, "finished_at": now()}
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        log = (result.stdout + result.stderr).strip()
        status = "passed" if result.returncode == 0 else "failed"
        result_type = "passed" if result.returncode == 0 else classify_failure(log, command)
        return {"command": command, "status": status, "result_type": result_type, "exit_code": result.returncode, "output": log, "log": log, "full_log": log, **_test_evidence(log), "started_at": started, "finished_at": now()}
    except subprocess.TimeoutExpired as exc:
        log = (_output(exc.stdout) + _output(exc.stderr)).strip() or f"Command timed out after {timeout} seconds."
        return {"command": command, "status": "timeout", "result_type": "execution_error", "exit_code": -1, "log": log, "full_log": log, **_test_evidence(log), "started_at": started, "finished_at": now()}
    except FileNotFoundError as exc:
        log = f"Command not found: {args[0] if 'args' in locals() and args else command} ({exc})"
        return {"command": command, "status": "missing_command", "result_type": "dependency_error", "exit_code": -1, "log": log, "full_log": log, **_test_evidence(log), "started_at": started, "finished_at": now()}
    except (OSError, ValueError) as exc:
        log = str(exc)
        return {"command": command, "status": "setup_error", "result_type": "execution_error", "exit_code": -1, "log": log, "full_log": log, **_test_evidence(log), "started_at": started, "finished_at": now()}


def classify_failure(log: str, command: str = "") -> str:
    text = (log or "").lower()
    if "syntaxerror" in text or "indentationerror" in text:
        return "syntax_error"
    if any(token in text for token in ("modulenotfounderror", "importerror", "no module named", "command not found")):
        return "dependency_error"
    # A non-zero process exit is not, by itself, evidence that a test
    # assertion failed.  In particular, commands that deliberately call
    # sys.exit() and uncaught RuntimeError/other exceptions are execution
    # errors.  Only assertion evidence (or unittest's failure summary) can
    # establish a test failure.
    assertion_traceback = re.search(r"(?:^|\n)(?:e\s+)?assertionerror(?:\s*:|\s*$)", text)
    pytest_assertion = re.search(r"(?:^|\n)failed\s+.+\s-\s+assert\b", text)
    if assertion_traceback or pytest_assertion or "failed (failures=" in text:
        return "test_failure"
    return "execution_error"


def _git_ref(source: Path, branch: str) -> str:
    local = git(source, "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
    return branch if local.returncode == 0 else f"refs/remotes/origin/{branch}"


def _sha(source: Path, branch: str) -> str:
    return git(source, "rev-parse", _git_ref(source, branch)).stdout.strip()


def _clone(source: Path, destination: Path, branches=()):
    result = subprocess.run(["git", "clone", "--no-local", "--no-hardlinks", "--quiet", str(source), str(destination)], capture_output=True, text=True)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip() or "unable to create clean repository snapshot")
    # Materialize selected remote-only commits under private refs. Never fetch
    # into refs/remotes/origin/*: that can reject when a stale remote ref is
    # older than a newer local branch. Execution uses exact commit SHAs.
    for branch in branches:
        remote_ref = f"refs/remotes/origin/{branch}"
        if git(source, "rev-parse", "--verify", remote_ref, check=False).returncode == 0:
            sha = git(source, "rev-parse", remote_ref).stdout.strip()
            private_ref = f"refs/mergeproof/{branch.replace('..', '_')}"
            fetched = git(destination, "fetch", "--no-tags", str(source), f"+{sha}:{private_ref}", check=False)
            if fetched.returncode:
                raise ValueError((fetched.stderr or fetched.stdout).strip() or f"unable to fetch {remote_ref}")


def run_branch(source: Path, branch: str, command: str, commit_sha: str = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="mergeproof-") as temporary:
        destination = Path(temporary) / "repo"
        try:
            _clone(source, destination, (branch,))
            ref = commit_sha or _sha(source, branch)
            git(destination, "config", "user.name", "MergeProof Temporary")
            git(destination, "config", "user.email", "mergeproof@localhost")
            git(destination, "checkout", "--quiet", "--detach", ref)
            git(destination, "reset", "--hard", ref)
            git(destination, "clean", "-fdx")
        except (ValueError, OSError) as exc:
            return {"command": command, "status": "setup_error", "result_type": "execution_error", "exit_code": -1, "log": str(exc), "full_log": str(exc), "started_at": now(), "finished_at": now()}
        return run_command(destination, command)


def run_combined(source: Path, branch_a: str, branch_b: str, base: str, command: str, sha_a: str = None, sha_b: str = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="mergeproof-") as temporary:
        destination = Path(temporary) / "repo"
        try:
            _clone(source, destination, (branch_a, branch_b))
            # Use a detached clean base so an existing source branch named
            # mergeproof-analysis (or any other helper branch) cannot break setup.
            git(destination, "checkout", "--quiet", "--detach", base)
            git(destination, "config", "user.name", "MergeProof Temporary")
            git(destination, "config", "user.email", "mergeproof@localhost")
            merge = git(destination, "merge", "--no-commit", "--no-ff", sha_a or _sha(source, branch_a), sha_b or _sha(source, branch_b), check=False)
            if merge.returncode:
                log = (merge.stdout + merge.stderr).strip()
                git(destination, "merge", "--abort", check=False)
                conflict = "CONFLICT" in log.upper() or "Automatic merge failed" in log
                status = "merge_conflict" if conflict else "setup_error"
                return {"command": command, "status": status, "result_type": "merge_conflict" if conflict else "execution_error", "exit_code": merge.returncode, "log": ("Merge conflict before tests\n" if conflict else "Merge setup failed before tests\n") + log, "full_log": log, **_test_evidence(log), "started_at": now(), "finished_at": now()}
        except (ValueError, OSError) as exc:
            return {"command": command, "status": "setup_error", "result_type": "execution_error", "exit_code": -1, "log": str(exc), "full_log": str(exc), "started_at": now(), "finished_at": now()}
        return run_command(destination, command)


def run_case(folder: str, test_file: str) -> dict:
    return run_command(ROOT / "demo_repo" / folder, f'{sys.executable} -m unittest -v {test_file}')


def report_findings(confirmed: bool, source_refs=None, runs=None):
    if confirmed:
        return [{"classification": "Confirmed Conflict", "title": "Combined test failure reproduced", "explanation": "Independent relevant checks passed and the combined relevant check failed; persisted logs and changed-file evidence show the interaction.", "source_refs": source_refs or []}]
    if runs and all(run.get("status") == "passed" for run in runs):
        return [{"classification": "No Conflict", "title": "No conflict detected by these checks", "explanation": "All selected checks passed; these checks did not detect a conflict.", "source_refs": source_refs or []}]
    return [{"classification": "Suggested Investigation", "title": "No confirmed interaction reproduced", "explanation": "The selected checks did not meet the pass/pass/test-failed rule. Merge conflicts, setup errors, missing commands, timeouts, and test failures are reported separately.", "source_refs": source_refs or []}]


def source_references(source: Path, base: str, branch_a: str, branch_b: str, sha_a: str = None, sha_b: str = None):
    references = []
    for label, branch, selected_sha in (("Change A", branch_a, sha_a), ("Change B", branch_b, sha_b)):
        ref = selected_sha or _sha(source, branch)
        diff = git(source, "diff", "--unified=3", f"{base}..{ref}", "--", check=False)
        text = (diff.stdout or diff.stderr).strip()
        snippets = []
        names = git(source, "diff", "--name-only", f"{base}..{ref}", "--", check=False).stdout.splitlines()
        for name in names[:20]:
            content = git(source, "show", f"{ref}:{name}", check=False).stdout
            if content:
                snippets.append({"file": name, "text": content[:6000]})
        references.append({"kind": label, "branch": branch, "sha": ref, "command": f"git diff --unified=3 {base[:12]}..{ref}", "text": text[:12000] or "No textual diff available.", "snippets": snippets})
    return references


def analysis() -> dict:
    independent_a, independent_b, combined = run_case("change_a", "test_change_a.py"), run_case("change_b", "test_change_b.py"), run_case("combined", "test_combined.py")
    confirmed = _confirmed([{"role": "independent_a", **independent_a}, {"role": "independent_b", **independent_b}, {"role": "combined", **combined}])
    evidence = [{"kind": "Change A", "file": "demo_repo/combined/service.py", "line": "3", "code": "order['total_usd'] = round(order['subtotal'] * rates[order['currency']], 2)"}, {"kind": "Change B", "file": "demo_repo/combined/service.py", "line": "4", "code": "discount = order['subtotal'] * 0.10 if order.get('loyalty') else 0"}, {"kind": "Test evidence", "file": "demo_repo/combined/test_combined.py", "line": "7", "code": "self.assertEqual(checkout({'subtotal': 100, 'currency': 'EUR', 'loyalty': True}), 97.2)"}]
    return {"repo": {"name": "checkout-service", "branch": "mergeproof/demo", "language": "Python 3"}, "summary": {"confirmed": int(confirmed), "risks": 1, "suggestions": 1, "confidence": 98}, "progress": {"completed": 5, "total": 5, "label": "5 of 5 complete"}, "graph": {"nodes": 5, "edges": 5}, "changes": [], "tests": {"change_a": independent_a, "change_b": independent_b, "combined": combined}, "finding": {"title": "Discount is calculated against the pre-normalized subtotal", "severity": "High confidence", "why": "Change A creates the normalized USD value in `total_usd`. Change B still calculates its 10% discount from the pre-normalized `subtotal`, so the combined test fails while each independent test passes.", "evidence": evidence, "trace": ["Order subtotal", "Normalize to total_usd", "Apply loyalty discount", "Checkout assertion"]}, "generated_at": "Live run · local Python subprocess"}


def _confirmed(runs):
    by_role = {run["role"]: run for run in runs}
    # A merge conflict is not a test result and can never confirm an interaction.
    comparable = all(by_role.get(role, {}).get("test_count", 0) > 0 for role in ("independent_a", "independent_b", "combined"))
    return (comparable and by_role.get("independent_a", {}).get("status") == "passed" and
            by_role.get("independent_b", {}).get("status") == "passed" and
            by_role.get("combined", {}).get("status") == "failed" and
            by_role.get("combined", {}).get("result_type") == "test_failure" and
            bool(by_role.get("combined", {}).get("log")))


def collection_view(store, collection, query):
    items = list(store.data[collection])
    search = (query.get("search", [""])[0] or "").lower()
    if search:
        items = [item for item in items if search in json.dumps(item).lower()]
    statuses = [status.strip() for value in query.get("status", []) for status in value.split(",") if status.strip()]
    if statuses:
        items = [item for item in items if item.get("status") in statuses]
    try:
        page = max(1, int(query.get("page", ["1"])[0]))
        size = min(100, max(1, int(query.get("page_size", ["50"])[0])))
    except ValueError:
        page, size = 1, 50
    start = (page - 1) * size
    return {collection: items[start:start + size], "page": page, "page_size": size, "total": len(items), "has_more": start + size < len(items)}


def overview(store):
    investigations = store.data["investigations"]
    unique = set()
    for report in store.data["reports"]:
        if not report.get("summary", {}).get("confirmed"):
            continue
        for finding in report.get("findings", []):
            if finding.get("classification") == "Confirmed Conflict":
                refs = finding.get("source_refs", [])
                unique.add((finding.get("title"), tuple((ref.get("file"), ref.get("sha"), ref.get("line")) for ref in refs)))
    return {"api_version": API_VERSION, "counts": {"repositories": len(store.data["repositories"]), "investigations": len(investigations), "test_runs": len(store.data["test_runs"]), "reports": len(store.data["reports"]), "completed": sum(i.get("status") == "completed" for i in investigations), "running": sum(i.get("status") == "running" for i in investigations)}, "confirmed_findings": len(unique), "unique_confirmed_findings": len(unique), "recent_activity": sorted(investigations, key=lambda x: x.get("created_at", ""), reverse=True)[:10]}


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
        results = [("independent_a", lambda: run_branch(source, investigation["branch_a"], command, details["branch_a_sha"])),
                   ("independent_b", lambda: run_branch(source, investigation["branch_b"], command, details["branch_b_sha"])),
                   ("combined", lambda: run_combined(source, investigation["branch_a"], investigation["branch_b"], details["common_base"], command, details["branch_a_sha"], details["branch_b_sha"]))]
        refs = source_references(source, details["common_base"], investigation["branch_a"], investigation["branch_b"], details["branch_a_sha"], details["branch_b_sha"])
    execution_id = str(uuid.uuid4())
    run_ids = []
    for role, result in results:
        result = result() if callable(result) else result
        result = {**result, "id": str(uuid.uuid4()), "investigation_id": investigation["id"], "execution_id": execution_id, "role": role}
        store.save("test_runs", result)
        run_ids.append(result["id"])
        store.update("investigations", investigation["id"], run_ids=run_ids, latest_execution_id=execution_id,
                     completed_roles=[item["role"] for item in [store.find("test_runs", run_id) for run_id in run_ids]])
    runs = [store.find("test_runs", item_id) for item_id in run_ids]
    confirmed = _confirmed(runs)
    all_passed = bool(runs) and all(run.get("status") == "passed" for run in runs)
    report = {"id": str(uuid.uuid4()), "investigation_id": investigation["id"], "execution_id": execution_id, "run_ids": run_ids, "runs": runs,
              "repository": {"repository_id": repo["id"], "branch_a": details.get("branch_a"), "branch_b": details.get("branch_b"), "branch_a_sha": details.get("branch_a_sha"), "branch_b_sha": details.get("branch_b_sha"), "common_base": details.get("common_base")},
              # A non-confirmed run is not automatically a risk: setup and
              # execution errors, and an all-green run, provide no interaction
              # evidence.  Keep the risk count evidence-based.
               "summary": {"confirmed": int(confirmed), "likely_risks": 0, "suggestions": 0 if confirmed or all_passed else 1},
              "findings": report_findings(confirmed, refs, runs), "generated_at": now()}
    store.save("reports", report)
    finished = now()
    store.update("investigations", investigation["id"], status="completed", run_ids=run_ids, report_id=report["id"], latest_execution_id=execution_id, completed_at=finished, finished_at=finished, common_base=details.get("common_base"), branch_a_sha=details.get("branch_a_sha"), branch_b_sha=details.get("branch_b_sha"))
    return report


class Handler(BaseHTTPRequestHandler):
    store = Store()

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def body(self):
        length = int(self.headers.get("Content-Length", 0))
        value = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("JSON request body must be an object")
        return value

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path); parts = [p for p in parsed.path.split("/") if p]; query = parse_qs(parsed.query)
        try:
            if parsed.path in ("/api/health", "/api/status"):
                self.send_json({"status": "ok", "api_version": API_VERSION, "service": "mergeproof", "time": now()}); return
            if parsed.path in ("/api/overview", "/api/dashboard"):
                self.send_json(overview(self.store)); return
            if parsed.path == "/api/analysis": self.send_json(analysis()); return
            if parsed.path == "/api/repositories": self.send_json(collection_view(self.store, "repositories", query)); return
            if len(parts) == 4 and parts[:2] == ["api", "repositories"] and parts[3] == "inspect":
                repo = self.store.find("repositories", parts[2]);
                if not repo: raise KeyError("repository not found")
                self.send_json(inspect_repository(repo, query.get("branch_a", [None])[0], query.get("branch_b", [None])[0])); return
            if len(parts) == 4 and parts[:2] == ["api", "repositories"] and parts[3] == "validate":
                repo = self.store.find("repositories", parts[2]);
                if not repo: raise KeyError("repository not found")
                self.send_json({**repo, **validate_repository(repo["path"])}); return
            if parsed.path == "/api/investigations": self.send_json(collection_view(self.store, "investigations", query)); return
            if len(parts) == 3 and parts[:2] == ["api", "investigations"]:
                item = self.store.find("investigations", parts[2]);
                if not item: raise KeyError("investigation not found")
                self.send_json(item); return
            if len(parts) == 4 and parts[:2] == ["api", "investigations"] and parts[3] == "runs":
                if not self.store.find("investigations", parts[2]): raise KeyError("investigation not found")
                investigation = self.store.find("investigations", parts[2])
                latest = investigation.get("latest_execution_id")
                runs = [x for x in self.store.data["test_runs"] if x["investigation_id"] == parts[2] and (query.get("history") == ["1"] or not latest or x.get("execution_id") == latest)]
                if query.get("history") == ["1"]:
                    try:
                        page = max(1, int(query.get("page", ["1"])[0])); size = min(100, max(1, int(query.get("page_size", ["50"])[0])))
                    except ValueError:
                        page, size = 1, 50
                    start = (page - 1) * size
                    runs = runs[start:start + size]
                    self.send_json({"runs": runs, "execution_id": latest, "page": page, "page_size": size, "total": len([x for x in self.store.data["test_runs"] if x["investigation_id"] == parts[2]])}); return
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
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            if parsed.path == "/api/repositories/validate":
                if not isinstance(payload.get("path"), str) or not payload["path"].strip(): raise ValueError("path must be a non-empty string")
                self.send_json(validate_repository(payload["path"])); return
            if parsed.path == "/api/repositories":
                if not isinstance(payload.get("path"), str) or not payload["path"].strip(): raise ValueError("path must be a non-empty string")
                info = validate_repository(payload["path"]); item = {"id": str(uuid.uuid4()), **info, "sample": False, "created_at": now()}; self.store.save("repositories", item); self.send_json(item, 201); return
            if parsed.path == "/api/investigations":
                if not isinstance(payload.get("repository_id"), str) or not isinstance(payload.get("branch_a"), str) or not isinstance(payload.get("branch_b"), str):
                    raise ValueError("repository_id, branch_a, and branch_b must be strings")
                repo = self.store.find("repositories", payload["repository_id"])
                if not repo: raise KeyError("repository not found")
                for key in ("branch_a", "branch_b"):
                    if not isinstance(payload.get(key), str) or not payload[key].strip(): raise ValueError(f"{key} must be a non-empty string")
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
    server = ThreadingHTTPServer((BIND_HOST, PORT), Handler); print(f"MergeProof running at http://{BIND_HOST}:{PORT}")
    try: server.serve_forever()
    except KeyboardInterrupt: server.server_close()
