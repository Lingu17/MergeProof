FROM python:3.11-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
EXPOSE 8000
CMD ["python", "app.py"]
