# MergeProof

MergeProof is an evidence-first demo for finding bugs that only appear when two individually passing code changes are combined.

## Run it

```bash
python app.py
```

Open http://localhost:8000. The dashboard runs a real Python fixture analysis: Change A and Change B each pass their own tests, while the combined snapshot reproduces a failing integration test.

The backend stores repositories, investigations, test runs, and reports in `mergeproof-data.json` beside `app.py`. Set `MERGEPROOF_DATA_FILE` to use another local JSON file. The built-in `checkout-service (sample)` repository is retained as a clearly labeled sample record whenever a new store is created.

## API

All request and response bodies are JSON.

* `POST /api/repositories` with `{ "path": "/absolute/path/to/git/repo" }` validates and stores a local Git repository. `POST /api/repositories/validate` validates without storing; `GET /api/repositories` lists stored repositories.
* `GET /api/repositories/:id/inspect?branch_a=...&branch_b=...` returns local branches and their `merge-base`. `POST /api/investigations` accepts `repository_id`, `branch_a`, `branch_b`, and optional `test_command` (default: `python -m unittest discover -v`).
* `GET /api/investigations` lists investigations and `GET /api/investigations/:id` retrieves one. `POST /api/investigations/:id/start` copies the repository into isolated temporary directories, runs both branches independently and then runs the combined merge, and persists every command, status, exit code, and full log.
* `GET /api/investigations/:id/runs` and `GET /api/test-runs/:id` expose the persisted run evidence. `GET /api/investigations/:id/report` returns the saved report; add `?download=1`, or use `GET /api/reports/:id/download`, for a downloadable JSON report. `POST /api/investigations/:id/report` saves caller-supplied report data.

The report marks `summary.confirmed` only when independent A passes, independent B passes, and the combined run fails. The existing `GET /api/analysis` sample endpoint remains available and continues to execute the fixture locally.

## Checks

```bash
python -m unittest discover -s tests -v
```
