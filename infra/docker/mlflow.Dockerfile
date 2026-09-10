# MLflow tracking server with a Postgres backend.
#
# The official image ships without a Postgres driver, so a backend-store-uri
# pointing at Postgres fails at startup with ModuleNotFoundError. Adding the
# driver is the whole delta.
#
# Postgres rather than the SQLite default because the backend store holds the
# prompt registry (18.1) and every eval run (16). Those are the record of which
# prompt produced which result, and losing them to a corrupted single-file
# database would take the audit trail section 16 depends on with it.
FROM ghcr.io/mlflow/mlflow:v2.21.3

RUN pip install --no-cache-dir psycopg2-binary==2.9.9

EXPOSE 5000
