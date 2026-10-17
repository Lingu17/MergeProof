FROM python:3.11.17-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock ./
RUN python -m pip install --no-index --require-hashes -r requirements.lock
COPY app.py README.md LICENSE .python-version ./
COPY web ./web
COPY demo_repo ./demo_repo
COPY tests ./tests
ENV MERGEPROOF_BIND_HOST=0.0.0.0 \
    MERGEPROOF_DATA_FILE=/data/mergeproof-data.json \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "app.py"]
