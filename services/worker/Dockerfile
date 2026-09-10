# Celery worker: OCR, extraction, date resolution (section 21).
# Runs on the always-on VM from 18.2, not on serverless — a Celery worker
# long-polls its broker and starves under request-scoped CPU throttling.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/app/packages/core

WORKDIR /app

# pdfplumber and Docling need these for image handling and PDF rasterization.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY services/worker/requirements.txt ./requirements.txt
RUN uv pip install --system --no-cache -r requirements.txt

COPY packages/core ./packages/core
COPY services/__init__.py ./services/__init__.py
COPY services/worker ./services/worker

CMD ["celery", "-A", "services.worker.celery_app", "worker", "--loglevel=info"]
