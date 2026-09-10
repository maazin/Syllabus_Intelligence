#!/usr/bin/env bash
# Boot script for the always-on VM (PRD section 18.2).
#
# This box holds the four things that genuinely cannot be serverless: the Redis
# broker, the Celery workers, MLflow, and Airflow. Section 18.2 argues the trade
# and this is what it costs to make good on it.
#
# Rendered by Terraform and handed to the instance as metadata, so it runs on
# every boot, not only the first. That matters: everything below is written to
# be idempotent so a restart converges rather than duplicating state, and a
# reboot after an image bump is a legitimate way to deploy the worker.
set -euo pipefail

exec > >(tee /var/log/syllint-startup.log | logger -t syllint -s 2>/dev/console) 2>&1

echo "syllint: startup begins"

# ------------------------------------------------------------------ docker

if ! command -v docker >/dev/null 2>&1; then
  echo "syllint: installing docker"
  apt-get update
  apt-get install -y --no-install-recommends ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg |
    gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi

systemctl enable --now docker

# The VM's own service account has artifactregistry.reader, so no key material
# is involved in pulling images.
gcloud auth configure-docker "${region}-docker.pkg.dev" --quiet

# ----------------------------------------------------------------- secrets

# Read from Secret Manager on every boot rather than baked into metadata.
# Instance metadata is readable by anything on the box that can reach the
# metadata server, which includes a compromised dependency inside a container.
mkdir -p /opt/syllint
umask 077

fetch_secret() {
  gcloud secrets versions access latest --secret="syllint-${environment}-$1"
}

DATABASE_URL_VALUE="$(fetch_secret database-url)"

# dbt takes host, port, user, password and database as five separate settings
# rather than a URL, so they are derived here from the one value Terraform
# stores. Deriving beats storing both: two spellings of the same credential
# drift the moment one is rotated, and the failure is a nightly job that
# authenticates against yesterday's password.
eval "$(
  DATABASE_URL_VALUE="$DATABASE_URL_VALUE" python3 - <<'PARSE'
import os
import shlex
from urllib.parse import urlsplit, unquote

url = urlsplit(os.environ["DATABASE_URL_VALUE"])
parts = {
    "PGHOST_": url.hostname or "",
    "PGPORT_": str(url.port or 5432),
    "PGUSER_": unquote(url.username or ""),
    "PGPASS_": unquote(url.password or ""),
    "PGNAME_": (url.path or "/").lstrip("/").split("?")[0],
}
for key, value in parts.items():
    print(f"{key}={shlex.quote(value)}")
PARSE
)"

cat > /opt/syllint/.env <<ENVFILE
ENVIRONMENT=${environment}
DATABASE_URL=$DATABASE_URL_VALUE
DATABASE_URL_PSYCOPG=$DATABASE_URL_VALUE
POSTGRES_HOST=$PGHOST_
POSTGRES_PORT=$PGPORT_
POSTGRES_USER=$PGUSER_
POSTGRES_PASSWORD=$PGPASS_
POSTGRES_DB=$PGNAME_
DBT_TARGET=prod
LLM_API_KEY=$(fetch_secret llm-api-key)
JWT_SECRET=$(fetch_secret jwt-secret)
R2_ACCESS_KEY_ID=$(fetch_secret r2-access-key-id)
R2_SECRET_ACCESS_KEY=$(fetch_secret r2-secret-access-key)
R2_ENDPOINT=${r2_endpoint}
R2_BUCKET=${r2_bucket}
LLM_MODEL_SYNC=${llm_model_sync}
LLM_MODEL_STRONG=${llm_model_strong}
LLM_MODEL_BATCH=${llm_model_batch}
OCR_MODE=docling
REDIS_URL=redis://redis:6379/0
MLFLOW_TRACKING_URI=http://mlflow:5000
SYLLINT_REPO_ROOT=/opt/syllabus-intelligence
ENVFILE
chmod 600 /opt/syllint/.env

# ----------------------------------------------------------- local metadata

# One small Postgres on this box backs both Airflow and MLflow. Neither belongs
# in the application database:
#
#   Airflow's scheduler polls its metadata store every few seconds forever.
#   Pointed at Neon, the compute never suspends and the 100 compute-hour free
#   tier section 18.1 relies on is gone in about four days.
#
#   MLflow creates roughly fifteen tables of its own. In the application schema
#   those become tables `alembic revision --autogenerate` does not know about
#   and proposes to drop, which is a migration nobody would read carefully
#   enough to catch.
mkdir -p /opt/syllint/initdb
cat > /opt/syllint/initdb/01-databases.sql <<'INITDB'
-- Runs once, on first initialisation of an empty data directory.
CREATE DATABASE mlflow OWNER airflow;
INITDB

# ----------------------------------------------------------------- compose

# Redis persists to a named volume on the boot disk, which is why the instance
# carries prevent_destroy: the append-only file is the queue of work in flight,
# and MLflow's artifact store is the extraction audit trail.
cat > /opt/syllint/docker-compose.yml <<'COMPOSE'
name: syllint

services:
  redis:
    image: redis:7-alpine
    restart: unless-stopped
    # appendonly, so a reboot does not silently drop queued extractions.
    command: ["redis-server", "--appendonly", "yes"]
    volumes:
      - redis-data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5

  worker:
    image: WORKER_IMAGE
    restart: unless-stopped
    env_file: /opt/syllint/.env
    depends_on:
      redis:
        condition: service_healthy
    # Two workers on a 2-vCPU box. Extraction is I/O bound on the model call,
    # but Docling's layout pass is not, and oversubscribing turns one slow PDF
    # into a stalled queue.
    command: ["celery", "-A", "services.worker.celery_app", "worker", "--loglevel=info", "--concurrency=2"]

  mlflow:
    image: MLFLOW_IMAGE
    restart: unless-stopped
    env_file: /opt/syllint/.env
    depends_on:
      metadata-db:
        condition: service_healthy
    volumes:
      - mlflow-artifacts:/mlruns
    command: >
      mlflow server --host 0.0.0.0 --port 5000
      --backend-store-uri postgresql://airflow:airflow@metadata-db/mlflow
      --default-artifact-root /mlruns

  # Bookkeeping for the two tools that keep one, on the box that runs them.
  # Reachable only from inside this compose network, which is why a fixed local
  # password is not a secret worth putting in Secret Manager.
  metadata-db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: airflow
      POSTGRES_PASSWORD: airflow
      POSTGRES_DB: airflow
    volumes:
      - metadata-db:/var/lib/postgresql/data
      - /opt/syllint/initdb:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U airflow"]
      interval: 10s
      timeout: 3s
      retries: 5

  airflow:
    image: AIRFLOW_IMAGE
    restart: unless-stopped
    env_file: /opt/syllint/.env
    environment:
      AIRFLOW__CORE__EXECUTOR: LocalExecutor
      AIRFLOW__CORE__LOAD_EXAMPLES: "False"
      AIRFLOW__DATABASE__SQL_ALCHEMY_CONN: postgresql+psycopg2://airflow:airflow@metadata-db/airflow
    depends_on:
      metadata-db:
        condition: service_healthy
    volumes:
      - airflow-logs:/opt/airflow/logs
    command: ["standalone"]

volumes:
  redis-data:
  mlflow-artifacts:
  airflow-logs:
  metadata-db:
COMPOSE

sed -i "s|WORKER_IMAGE|${worker_image}|; s|MLFLOW_IMAGE|${mlflow_image}|; s|AIRFLOW_IMAGE|${airflow_image}|" \
  /opt/syllint/docker-compose.yml

# ------------------------------------------------------------------- start

cd /opt/syllint
docker compose pull --quiet
docker compose up -d --remove-orphans

# Images accumulate on a 30 GB disk otherwise, and a full boot disk is a very
# annoying way to find out that deploys have been working.
docker image prune -af --filter "until=168h" || true

echo "syllint: startup complete"
