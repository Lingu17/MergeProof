# MergeProof

MergeProof is an evidence-first demo for finding bugs that only appear when two individually passing code changes are combined.

## Run it

```bash
python app.py
```

Open http://localhost:8000. The dashboard runs a real Python fixture analysis: Change A and Change B each pass their own tests, while the combined snapshot reproduces a failing integration test.

## Checks

```bash
python -m unittest discover -s tests -v
```
