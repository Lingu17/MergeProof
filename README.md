# MergeProof

MergeProof is an evidence-first demo for finding bugs that only appear when two individually passing code changes are combined.

## Run it

```bash
cp .env.example .env  # optional; edit values, then export them in your shell
set -a; . ./.env; set +a
python app.py
```

Open http://localhost:8000. The dashboard runs a real Python fixture analysis: Change A and Change B each pass their own tests, while the combined snapshot reproduces a failing integration test.

The local server binds to `127.0.0.1` by default. Set `MERGEPROOF_BIND_HOST` when a different interface is required. The image installs Git because real repository investigations clone clean Git snapshots.

The backend stores repositories, investigations, test runs, and reports in `mergeproof-data.json` beside `app.py`. Set `MERGEPROOF_DATA_FILE` to use another local JSON file. The built-in `checkout-service (sample)` repository is retained as a clearly labeled sample record whenever a new store is created.

## API

All request and response bodies are JSON.

* `POST /api/repositories` with `{ "path": "/absolute/path/to/git/repo" }` validates and stores a local Git repository. `POST /api/repositories/validate` validates without storing; `GET /api/repositories` lists stored repositories.
* `GET /api/repositories/:id/inspect?branch_a=...&branch_b=...` returns local branches and their `merge-base`. `POST /api/investigations` accepts `repository_id`, `branch_a`, `branch_b`, and optional `test_command` (default: `python -m unittest discover -v`).
* `GET /api/investigations` lists investigations and `GET /api/investigations/:id` retrieves one. `POST /api/investigations/:id/start` copies the repository into isolated temporary directories, runs both branches independently and then runs the combined merge, and persists every command, status, exit code, and full log.
* `GET /api/investigations/:id/runs`, `/history`, and `/progress` expose the persisted run evidence and background state. `POST /api/investigations/:id/start` queues work and `POST /api/investigations/:id/cancel` requests cooperative cancellation. `GET /api/investigations/:id/report` returns the saved report; use `/api/reports/:id/download` for JSON or `/api/reports/:id/bundle` for a ZIP containing the report, logs, diffs, and `REPRODUCE.md`.

The report marks `summary.confirmed` only when independent A passes, independent B passes, and the combined run fails. The existing `GET /api/analysis` sample endpoint remains available and continues to execute the fixture locally.

## Checks

```bash
python -m unittest discover -s tests -v
```

## Docker

Build and run with an explicit container exposure setting:

```bash
docker build -t mergeproof .
docker run --rm -p 8000:8000 -e MERGEPROOF_BIND_HOST=0.0.0.0 mergeproof
```

The Docker image sets `MERGEPROOF_BIND_HOST=0.0.0.0` because published ports need an in-container listener; the host-only default remains in effect for direct local runs. Use a volume if investigation data must persist, for example `-v "$PWD/data:/app/data" -e MERGEPROOF_DATA_FILE=/app/data/mergeproof-data.json`.

Run the offline regression suite inside the image with:

```bash
docker run --rm mergeproof python -m unittest discover -s tests -v
```

To analyze a host repository, mount it read-only into the container and use the mounted path in the Repositories screen, for example:

```bash
docker run --rm -p 8000:8000 \
  -e MERGEPROOF_BIND_HOST=0.0.0.0 \
  -v "$PWD/my-repository:/repos/my-repository:ro" \
  -v "$PWD/data:/app/data" \
  -e MERGEPROOF_DATA_FILE=/app/data/mergeproof-data.json \
  mergeproof
```

The project is released under the MIT License (`LICENSE`).

The UI is a local-only application: it does not provide hosted accounts, billing, teams, or remote provider imports. Repository import currently means a path to a local Git checkout. `/api/health` exposes the backend version used by the UI; `/api/overview` provides live dashboard counts and recent activity.

## Demo outline

1. Open the clearly labelled `checkout-service (sample)` repository and create the fixed `change_a` + `change_b` comparison.
2. Review the base/change/combined table, then start the background analysis.
3. Show two passing independent checks and the combined assertion failure; open the saved source diff and logs.
4. Download the evidence bundle and open `REPRODUCE.md`.
5. Add a real local Git repository, select two branches, provide its test command, and repeat the same workflow. The same engine reports a neutral all-green result, a confirmed assertion interaction, or a setup/merge/runtime result without conflating them.

MergeProof does not currently include runtime AI reasoning. This prototype keeps deterministic Git/test evidence separate; an AI layer can later propose hypotheses or targeted regression tests whose claims remain unverified until execution.
