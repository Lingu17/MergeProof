from __future__ import annotations

import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent


def run_case(folder: str, test_file: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "-v", test_file],
        cwd=ROOT / "demo_repo" / folder,
        capture_output=True,
        text=True,
    )
    output = (result.stdout + result.stderr).strip()
    return {
        "status": "passed" if result.returncode == 0 else "failed",
        "exit_code": result.returncode,
        "output": output,
    }


def analysis() -> dict:
    independent_a = run_case("change_a", "test_change_a.py")
    independent_b = run_case("change_b", "test_change_b.py")
    combined = run_case("combined", "test_combined.py")
    return {
        "repo": {"name": "checkout-service", "branch": "mergeproof/demo", "language": "Python 3"},
        "summary": {
            "confirmed": 1 if combined["status"] == "failed" and independent_a["status"] == "passed" and independent_b["status"] == "passed" else 0,
            "risks": 2,
            "suggestions": 2,
            "confidence": 98,
        },
        "changes": [
            {"id": "change-a", "label": "Change A", "title": "Normalize checkout currency", "author": "Maya Chen", "sha": "a17c9e2", "files": 2, "description": "Moves currency normalization into the order boundary so downstream totals always use USD.", "color": "violet"},
            {"id": "change-b", "label": "Change B", "title": "Add loyalty discount", "author": "Jon Bell", "sha": "b82fd11", "files": 2, "description": "Applies a member discount using the checkout subtotal before tax is calculated.", "color": "cyan"},
        ],
        "tests": {"change_a": independent_a, "change_b": independent_b, "combined": combined},
        "finding": {
            "title": "Discount is calculated against the pre-normalized subtotal",
            "severity": "High confidence",
            "why": "Change A replaces the order total with a normalized USD value in `total_usd`. Change B still reads the removed `subtotal` field, so the combined checkout path raises AttributeError.",
            "evidence": [
                {"kind": "Change A", "file": "demo_repo/combined/service.py", "line": "12", "code": "order['total_usd'] = round(order['subtotal'] * rate, 2)"},
                {"kind": "Change B", "file": "demo_repo/combined/service.py", "line": "18", "code": "discount = order['subtotal'] * 0.10"},
                {"kind": "Test evidence", "file": "demo_repo/combined/test_combined.py", "line": "9", "code": "self.assertEqual(checkout({'subtotal': 100, 'currency': 'EUR'}), 99.0)"},
            ],
            "trace": ["Order subtotal", "Normalize to total_usd", "Apply loyalty discount", "Checkout assertion"],
        },
        "generated_at": "Live run · local Python subprocess",
    }


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/analysis":
            self.send_json(analysis())
            return
        if path in ("/", "/index.html"):
            body = (ROOT / "web" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_json({"error": "Not found"}, 404)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8000), Handler)
    print("MergeProof running at http://localhost:8000")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
