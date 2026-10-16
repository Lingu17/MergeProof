FROM python:3.11-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
ENV MERGEPROOF_BIND_HOST=0.0.0.0
EXPOSE 8000
CMD ["python", "app.py"]
