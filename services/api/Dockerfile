# FastAPI service. Deployed to Cloud Run, which scales to zero (18.1).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/app/packages/core

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY services/api/requirements.txt ./requirements.txt
RUN uv pip install --system --no-cache -r requirements.txt

COPY packages/core ./packages/core
COPY services/__init__.py ./services/__init__.py
COPY services/api ./services/api

EXPOSE 8000

# No parsing dependencies here on purpose: the API is the HTTP surface only
# (section 21), and keeping Docling/torch out of this image keeps cold starts
# fast enough for Cloud Run's scale-to-zero to be worth having.
CMD ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
