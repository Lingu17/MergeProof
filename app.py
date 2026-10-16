from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import io
import zipfile
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
INVESTIGATION_ROLES = ("independent_a", "independent_b", "combined")
INVESTIGATION_STATUSES = ("created", "queued", "running", "completed", "failed", "cancelled", "interrupted")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _progress_fields(completed=0, total=len(INVESTIGATION_ROLES), current_role=None):
    completed = max(0, min(int(completed), int(total) if total else 0))
    total = max(0, int(total))
    percent = int((completed * 100) / total) if total else 0
    progress = {"completed": completed, "total": total, "percent": percent,
                "label": f"{completed} of {total} complete", "current_role": current_role}
    return {"progress": progress, "completed_steps": completed, "total_steps": total,
            "progress_percent": percent, "progress_label": progress["label"],
            "current_role": current_role}


def _copy_json(value):
    """Return a detached JSON-compatible snapshot for HTTP responses."""
    return json.loads(json.dumps(value))


def evidence_bundle(report: dict) -> bytes:
    """Build a portable, deterministic evidence bundle on demand."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("report.json", json.dumps(report, indent=2))
        reproduction = [
            "# MergeProof reproduction",
            "",
            f"Investigation: {report.get('investigation_id', 'unknown')}",
            f"Execution: {report.get('execution_id', 'unknown')}",
            "",
            "The runs below were executed from clean snapshots. Review the exact SHAs before reproducing.",
            "",
        ]
        repository = report.get("repository", {})
        for key in ("common_base", "branch_a", "branch_a_sha", "branch_b", "branch_b_sha"):
            if repository.get(key):
                reproduction.append(f"- {key}: `{repository[key]}`")
        reproduction.extend(["", "## Commands and outcomes", ""])
        for run in report.get("runs", []):
            reproduction.extend([
                f"### {run.get('role', 'run')}",
                f"- command: `{run.get('command', '')}`",
                f"- status: `{run.get('status', '')}`",
                f"- result_type: `{run.get('result_type', '')}`",
                f"- exit_code: `{run.get('exit_code', '')}`",
                f"- test_count: `{run.get('test_count', 0)}`",
                "",
                "```text",
                run.get("full_log", ""),
                "```",
                "",
            ])
        archive.writestr("REPRODUCE.md", "\n".join(reproduction))
        for index, run in enumerate(report.get("runs", []), 1):
            archive.writestr(f"logs/{index}-{run.get('role', 'run')}.log", run.get("full_log", ""))
        for index, finding in enumerate(report.get("findings", []), 1):
            for source_index, source in enumerate(finding.get("source_refs", []), 1):
                archive.writestr(f"sources/finding-{index}-{source_index}.diff", source.get("text") or source.get("code") or "")
    return buffer.getvalue()


class Store:
    def __init__(self, path: Path = DATA_FILE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.data = self._load()
        # A process cannot resume a subprocess after restart.  Make abandoned
        # work explicit while retaining all runs/logs already persisted.  A
        # queued job is also interrupted: it may have been waiting in an
        # in-memory queue which cannot survive a process restart.
        changed = False
        for investigation in self.data["investigations"]:
            if investigation.get("status") in ("queued", "running"):
                investigation.update({"status": "interrupted", "error": "backend restarted while investigation was running or queued", "interrupted_at": now(), "interrupted_execution_id": investigation.get("latest_execution_id"), "finished_at": now(), **_progress_fields(len(investigation.get("completed_roles", [])), int(investigation.get("total_steps", len(INVESTIGATION_ROLES))), None)})
                changed = True
            if "progress" not in investigation:
                completed = len(investigation.get("completed_roles", []))
                total = int(investigation.get("total_steps", len(INVESTIGATION_ROLES)))
                investigation.update(_progress_fields(completed, total, investigation.get("current_role")))
                changed = True
        if changed:
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


def run_command(cwd: Path, command: str, timeout: int = 300, cancel_event=None) -> dict:
    started = now()
    try:
        args = [arg.strip('"') if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in "\"'" else arg
                for arg in shlex.split(command, posix=False)]
        if not args:
            return {"command": command, "status": "missing_command", "exit_code": -1, "log": "No command was supplied.", "full_log": "No command was supplied.", **_test_evidence(""), "started_at": started, "finished_at": now()}
        process = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.01, min(0.1, deadline - time.monotonic()))
            try:
                stdout, stderr = process.communicate(timeout=remaining)
                break
            except subprocess.TimeoutExpired as exc:
                if cancel_event is not None and cancel_event.is_set():
                    process.kill()
                    stdout, stderr = process.communicate()
                    log = (_output(stdout) + _output(stderr)).strip() or "Command cancelled."
                    return {"command": command, "status": "cancelled", "result_type": "cancelled", "exit_code": -1,
                            "output": log, "log": log, "full_log": log, **_test_evidence(log), "started_at": started, "finished_at": now()}
                if time.monotonic() >= deadline:
                    process.kill()
                    stdout, stderr = process.communicate()
                    log = (_output(stdout) + _output(stderr)).strip() or f"Command timed out after {timeout} seconds."
                    return {"command": command, "status": "timeout", "result_type": "execution_error", "exit_code": -1,
                            "log": log, "full_log": log, **_test_evidence(log), "started_at": started, "finished_at": now()}
        log = (_output(stdout) + _output(stderr)).strip()
        status = "passed" if process.returncode == 0 else "failed"
        result_type = "passed" if process.returncode == 0 else classify_failure(log, command)
        return {"command": command, "status": status, "result_type": result_type, "exit_code": process.returncode, "output": log, "log": log, "full_log": log, **_test_evidence(log), "started_at": started, "finished_at": now()}
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


class InvestigationCancelled(Exception):
    """Internal signal used to stop a queued execution without marking it failed."""


def run_branch(source: Path, branch: str, command: str, commit_sha: str = None, cancel_event=None) -> dict:
    with tempfile.TemporaryDirectory(prefix="mergeproof-") as temporary:
        destination = Path(temporary) / "repo"
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise InvestigationCancelled()
            _clone(source, destination, (branch,))
            ref = commit_sha or _sha(source, branch)
            git(destination, "config", "user.name", "MergeProof Temporary")
            git(destination, "config", "user.email", "mergeproof@localhost")
            git(destination, "checkout", "--quiet", "--detach", ref)
            git(destination, "reset", "--hard", ref)
            git(destination, "clean", "-fdx")
        except (ValueError, OSError) as exc:
            return {"command": command, "status": "setup_error", "result_type": "execution_error", "exit_code": -1, "log": str(exc), "full_log": str(exc), "started_at": now(), "finished_at": now()}
        return run_command(destination, command, cancel_event=cancel_event)


def run_combined(source: Path, branch_a: str, branch_b: str, base: str, command: str, sha_a: str = None, sha_b: str = None, cancel_event=None) -> dict:
    with tempfile.TemporaryDirectory(prefix="mergeproof-") as temporary:
        destination = Path(temporary) / "repo"
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise InvestigationCancelled()
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
        return run_command(destination, command, cancel_event=cancel_event)


def run_case(folder: str, test_file: str, cancel_event=None) -> dict:
    return run_command(ROOT / "demo_repo" / folder, f'{sys.executable} -m unittest -v {test_file}', cancel_event=cancel_event)


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
    return {"repo": {"name": "checkout-service", "branch": "mergeproof/demo", "language": "Python 3"}, "summary": {"confirmed": int(confirmed), "risks": 1, "suggestions": 1}, "progress": {"completed": 5, "total": 5, "label": "5 of 5 complete"}, "graph": {"nodes": 5, "edges": 5}, "changes": [], "tests": {"change_a": independent_a, "change_b": independent_b, "combined": combined}, "finding": {"title": "Discount is calculated against the pre-normalized subtotal", "severity": "Reproduced", "why": "Change A creates the normalized USD value in `total_usd`. Change B still calculates its 10% discount from the pre-normalized `subtotal`, so the combined test fails while each independent test passes.", "evidence": evidence, "trace": ["Order subtotal", "Normalize to total_usd", "Apply loyalty discount", "Checkout assertion"]}, "generated_at": "Live run · local Python subprocess"}


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
    with store.lock:
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
    counts = {"repositories": len(store.data["repositories"]), "investigations": len(investigations), "test_runs": len(store.data["test_runs"]), "reports": len(store.data["reports"])}
    for status in ("queued", "running", "completed", "failed", "cancelled", "interrupted"):
        counts[status] = sum(i.get("status") == status for i in investigations)
    return {"api_version": API_VERSION, "counts": counts, "confirmed_findings": len(unique), "unique_confirmed_findings": len(unique), "recent_activity": sorted(investigations, key=lambda x: x.get("created_at", ""), reverse=True)[:10]}


def _sample_evidence():
    return [{"kind": "Change A", "file": "demo_repo/combined/service.py", "line": "3", "code": "order['total_usd'] = round(order['subtotal'] * rates[order['currency']], 2)"},
            {"kind": "Change B", "file": "demo_repo/combined/service.py", "line": "4", "code": "discount = order['subtotal'] * 0.10 if order.get('loyalty') else 0"},
            {"kind": "Test evidence", "file": "demo_repo/combined/test_combined.py", "line": "7", "code": "self.assertEqual(checkout({'subtotal': 100, 'currency': 'EUR', 'loyalty': True}), 97.2)"}]


def _cancel_requested(store, investigation_id, cancel_event=None):
    return (cancel_event is not None and cancel_event.is_set()) or bool((store.find("investigations", investigation_id) or {}).get("status") == "cancelled")


def _save_progress(store, investigation_id, completed, current_role=None):
    return store.update("investigations", investigation_id, **_progress_fields(completed, len(INVESTIGATION_ROLES), current_role))


def execute_investigation(handler, investigation, cancel_event=None):
    """Execute one investigation, persisting each role as soon as it ends.

    This remains callable by synchronous helpers, but the HTTP API invokes it
    only from InvestigationWorker so a request never owns the execution.
    """
    store = handler.store
    repo = store.find("repositories", investigation["repository_id"])
    if not repo:
        raise KeyError("repository not found")
    if _cancel_requested(store, investigation["id"], cancel_event):
        raise InvestigationCancelled()
    details = inspect_repository(repo, investigation["branch_a"], investigation["branch_b"])
    command = investigation["test_command"]
    sample_repository = False
    if repo.get("sample"):
        if (investigation["branch_a"], investigation["branch_b"]) != ("change_a", "change_b"):
            raise ValueError("sample repository only supports change_a and change_b")
        if command != SAMPLE_COMMAND:
            raise ValueError("sample repository only supports the default test command")
        # Keep the sample's three real subprocess checks separate so their
        # results and progress are durable just like real repository runs.
        sample_repository = True
        results = [("independent_a", lambda: run_case("change_a", "test_change_a.py", cancel_event)),
                   ("independent_b", lambda: run_case("change_b", "test_change_b.py", cancel_event)),
                   ("combined", lambda: run_case("combined", "test_combined.py", cancel_event))]
        refs = _sample_evidence()
    else:
        source = Path(repo["path"])
        results = [("independent_a", lambda: run_branch(source, investigation["branch_a"], command, details["branch_a_sha"], cancel_event)),
                   ("independent_b", lambda: run_branch(source, investigation["branch_b"], command, details["branch_b_sha"], cancel_event)),
                   ("combined", lambda: run_combined(source, investigation["branch_a"], investigation["branch_b"], details["common_base"], command, details["branch_a_sha"], details["branch_b_sha"], cancel_event))]
        refs = source_references(source, details["common_base"], investigation["branch_a"], investigation["branch_b"], details["branch_a_sha"], details["branch_b_sha"])
    execution_id = str(uuid.uuid4())
    store.update("investigations", investigation["id"], latest_execution_id=execution_id, run_ids=[], completed_roles=[], **_progress_fields(0, len(results), None))
    run_ids = []
    for role, action in results:
        if _cancel_requested(store, investigation["id"], cancel_event):
            raise InvestigationCancelled()
        _save_progress(store, investigation["id"], len(run_ids), role)
        result = action()
        if sample_repository:
            result = {**result, "command": SAMPLE_COMMAND}
        result = {**result, "id": str(uuid.uuid4()), "investigation_id": investigation["id"], "execution_id": execution_id, "role": role}
        # A cancelled subprocess is still a completed, persisted test-run
        # record.  The investigation itself becomes cancelled below.
        store.save("test_runs", result)
        run_ids.append(result["id"])
        completed_roles = [store.find("test_runs", run_id)["role"] for run_id in run_ids]
        _save_progress(store, investigation["id"], len(run_ids), None)
        store.update("investigations", investigation["id"], run_ids=run_ids, latest_execution_id=execution_id, completed_roles=completed_roles)
        if result.get("status") == "cancelled" or _cancel_requested(store, investigation["id"], cancel_event):
            raise InvestigationCancelled()
    runs = [store.find("test_runs", item_id) for item_id in run_ids]
    confirmed = _confirmed(runs)
    all_passed = bool(runs) and all(run.get("status") == "passed" for run in runs)
    report = {"id": str(uuid.uuid4()), "investigation_id": investigation["id"], "execution_id": execution_id, "run_ids": run_ids, "runs": runs,
              "repository": {"repository_id": repo["id"], "branch_a": details.get("branch_a"), "branch_b": details.get("branch_b"), "branch_a_sha": details.get("branch_a_sha"), "branch_b_sha": details.get("branch_b_sha"), "common_base": details.get("common_base")},
              "environment": {"python": sys.version.split()[0], "platform": os.name, "execution": "clean detached snapshot; working tree cleaned", "global_git_config": "not modified"},
              # A non-confirmed run is not automatically a risk: setup and
              # execution errors, and an all-green run, provide no interaction
              # evidence.  Keep the risk count evidence-based.
               "summary": {"confirmed": int(confirmed), "likely_risks": 0, "suggestions": 0 if confirmed or all_passed else 1},
              "findings": report_findings(confirmed, refs, runs), "generated_at": now()}
    if _cancel_requested(store, investigation["id"], cancel_event):
        raise InvestigationCancelled()
    store.save("reports", report)
    finished = now()
    store.update("investigations", investigation["id"], status="completed", run_ids=run_ids, report_id=report["id"], latest_execution_id=execution_id, completed_at=finished, finished_at=finished, common_base=details.get("common_base"), branch_a_sha=details.get("branch_a_sha"), branch_b_sha=details.get("branch_b_sha"), **_progress_fields(len(run_ids), len(results), None))
    return report


class InvestigationWorker:
    """One durable-facing queue backed by a daemon worker thread."""

    def __init__(self, store):
        self.store = store
        self.jobs = queue.Queue()
        self.cancel_events = {}
        self.job_tokens = {}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="mergeproof-investigation-worker", daemon=True)
        self.thread.start()

    def submit(self, investigation_id):
        with self.lock:
            investigation = self.store.find("investigations", investigation_id)
            if not investigation:
                raise KeyError("investigation not found")
            if investigation.get("status") in ("queued", "running"):
                raise RuntimeError("investigation is already running")
            queued_at = now()
            queued = self.store.update("investigations", investigation_id, status="queued", queued_at=queued_at,
                                       started_at=None, finished_at=None, completed_at=None, report_id=None,
                                       latest_execution_id=None, run_ids=[], completed_roles=[], error=None,
                                       cancellation_requested_at=None, cancelled_at=None,
                                       **_progress_fields(0, len(INVESTIGATION_ROLES), None))
            token = str(uuid.uuid4())
            self.job_tokens[investigation_id] = token
            self.cancel_events[investigation_id] = threading.Event()
            snapshot = _copy_json(queued)
            self.jobs.put((investigation_id, token))
            return snapshot

    def cancel(self, investigation_id):
        with self.lock:
            investigation = self.store.find("investigations", investigation_id)
            if not investigation:
                raise KeyError("investigation not found")
            if investigation.get("status") not in ("queued", "running"):
                raise RuntimeError(f"investigation cannot be cancelled from status {investigation.get('status')}")
            event = self.cancel_events.setdefault(investigation_id, threading.Event())
            event.set()
            completed = len(investigation.get("completed_roles", []))
            changes = {"status": "cancelled", "cancelled_at": now(), "cancellation_requested_at": now(),
                       **_progress_fields(completed, len(INVESTIGATION_ROLES), None)}
            if investigation.get("status") == "queued":
                changes["finished_at"] = now()
            cancelled = self.store.update("investigations", investigation_id, **changes)
            return _copy_json(cancelled)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                investigation_id, token = self.jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                with self.lock:
                    investigation = self.store.find("investigations", investigation_id)
                    event = self.cancel_events.get(investigation_id, threading.Event())
                    if self.job_tokens.get(investigation_id) != token or not investigation or investigation.get("status") == "cancelled" or event.is_set():
                        continue
                    self.store.update("investigations", investigation_id, status="running", started_at=now(), error=None, **_progress_fields(len(investigation.get("completed_roles", [])), len(INVESTIGATION_ROLES), None))
                    investigation = self.store.find("investigations", investigation_id)
                execute_investigation(self, investigation, event)
            except InvestigationCancelled:
                self.store.update("investigations", investigation_id, status="cancelled", error="investigation cancelled", finished_at=now(), **_progress_fields(len((self.store.find("investigations", investigation_id) or {}).get("completed_roles", [])), len(INVESTIGATION_ROLES), None))
            except Exception as exc:
                investigation = self.store.find("investigations", investigation_id) or {}
                self.store.update("investigations", investigation_id, status="failed", error=str(exc), finished_at=now(), **_progress_fields(len(investigation.get("completed_roles", [])), len(INVESTIGATION_ROLES), None))
            finally:
                with self.lock:
                    if self.job_tokens.get(investigation_id) == token:
                        self.cancel_events.pop(investigation_id, None)
                        self.job_tokens.pop(investigation_id, None)
                self.jobs.task_done()

    def shutdown(self):
        self.stop_event.set()
        self.thread.join(timeout=1)


def investigation_history(store, investigation_id):
    with store.lock:
        investigation = store.find("investigations", investigation_id)
        if not investigation:
            raise KeyError("investigation not found")
        investigation = _copy_json(investigation)
        runs = _copy_json([run for run in store.data["test_runs"] if run.get("investigation_id") == investigation_id])
        reports = _copy_json([report for report in store.data["reports"] if report.get("investigation_id") == investigation_id])
    executions = []
    for execution_id in dict.fromkeys(run.get("execution_id") for run in runs if run.get("execution_id")):
        execution_runs = [run for run in runs if run.get("execution_id") == execution_id]
        report = next((item for item in reports if item.get("execution_id") == execution_id), None)
        if execution_id == investigation.get("interrupted_execution_id"):
            status = "interrupted"
        elif execution_id == investigation.get("latest_execution_id") and investigation.get("status") in ("queued", "running", "cancelled", "failed", "interrupted"):
            status = investigation["status"]
        else:
            status = "completed" if report else ("cancelled" if any(run.get("status") == "cancelled" for run in execution_runs) else "failed")
        executions.append({"execution_id": execution_id, "status": status, "run_ids": [run["id"] for run in execution_runs], "runs": execution_runs, "report_id": report.get("id") if report else None})
    return {"investigation_id": investigation_id, "latest_execution_id": investigation.get("latest_execution_id"), "executions": executions, "runs": runs}


class Handler(BaseHTTPRequestHandler):
    store = Store()
    worker = None
    worker_lock = threading.RLock()

    @classmethod
    def get_worker(cls):
        with cls.worker_lock:
            if cls.worker is None or cls.worker.store is not cls.store:
                if cls.worker is not None:
                    cls.worker.shutdown()
                cls.worker = InvestigationWorker(cls.store)
            return cls.worker

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
            if len(parts) == 4 and parts[:2] == ["api", "investigations"] and parts[3] == "progress":
                item = self.store.find("investigations", parts[2]);
                if not item: raise KeyError("investigation not found")
                runs = [run for run in self.store.data["test_runs"] if run.get("investigation_id") == parts[2] and run.get("execution_id") == item.get("latest_execution_id")]
                self.send_json({**item, "runs": runs}); return
            if len(parts) == 4 and parts[:2] == ["api", "investigations"] and parts[3] == "history":
                self.send_json(investigation_history(self.store, parts[2])); return
            if len(parts) == 4 and parts[:2] == ["api", "investigations"] and parts[3] == "runs":
                if not self.store.find("investigations", parts[2]): raise KeyError("investigation not found")
                investigation = self.store.find("investigations", parts[2])
                latest = investigation.get("latest_execution_id")
                history_requested = query.get("history", [""])[0].lower() in ("1", "true", "yes", "all")
                runs = [x for x in self.store.data["test_runs"] if x["investigation_id"] == parts[2] and (history_requested or not latest or x.get("execution_id") == latest)]
                if history_requested:
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
            if len(parts) == 4 and parts[:2] == ["api", "reports"] and parts[3] == "bundle":
                report = self.store.find("reports", parts[2])
                if not report: raise KeyError("report not found")
                body = evidence_bundle(report); self.send_response(200); self.send_header("Content-Type", "application/zip"); self.send_header("Content-Disposition", 'attachment; filename="mergeproof-evidence.zip"'); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
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
                item = {"id": str(uuid.uuid4()), "repository_id": repo["id"], "branch_a": payload["branch_a"], "branch_b": payload["branch_b"], "test_command": requested_command, "status": "created", "created_at": now(), **_progress_fields(0, len(INVESTIGATION_ROLES), None)}; self.store.save("investigations", item); self.send_json(item, 201); return
            if len(parts) == 4 and parts[:2] == ["api", "investigations"] and parts[3] == "start":
                investigation = self.store.find("investigations", parts[2])
                if not investigation: raise KeyError("investigation not found")
                try:
                    queued = self.get_worker().submit(investigation["id"])
                except RuntimeError as exc:
                    self.send_json({"error": str(exc)}, 409); return
                self.send_json(queued, 202); return
            if len(parts) == 4 and parts[:2] == ["api", "investigations"] and parts[3] == "cancel":
                if not self.store.find("investigations", parts[2]): raise KeyError("investigation not found")
                try:
                    cancelled = self.get_worker().cancel(parts[2])
                except RuntimeError as exc:
                    self.send_json({"error": str(exc)}, 409); return
                self.send_json(cancelled); return
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
    except KeyboardInterrupt: pass
    finally:
        server.server_close()
        if Handler.worker is not None:
            Handler.worker.shutdown()
