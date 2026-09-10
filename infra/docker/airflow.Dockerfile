# Airflow with dbt, for the nightly corpus aggregation.
#
# Airflow runs in its own image on purpose. Airflow 2.x caps SQLAlchemy below
# 2.0 while the application requires 2.x, and its constraint file pins
# typing_extensions older than pydantic needs. Installing it alongside the app
# silently downgrades both and breaks the API.
#
# That conflict is the concrete form of the trade section 18.2 describes: the
# most recognized orchestrator is also the heaviest, and isolating it in a
# container is the price of using it. The application never imports Airflow,
# and Airflow never imports the application: the DAG shells out to dbt.
FROM apache/airflow:2.10.5-python3.11

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

USER airflow

# dbt-postgres and psycopg are the only extras the DAG needs. Everything else
# it does is a shell out.
RUN pip install --no-cache-dir \
      "dbt-core>=1.8,<2.0" \
      "dbt-postgres>=1.8,<2.0" \
      "psycopg[binary]>=3.1"

# The DAG and the dbt project have to be *in* the image.
#
# Airflow only runs what is in its DAGs folder, and a DAG that is absent is not
# an error anywhere: the scheduler simply has nothing to schedule, and the
# corpus quietly stops rebuilding. Nothing alerts on a job that was never
# registered, so this is the failure most likely to go unnoticed for a term.
COPY --chown=airflow:root services/pipeline/dags /opt/airflow/dags
COPY --chown=airflow:root services/pipeline/dbt /opt/syllabus-intelligence/services/pipeline/dbt

ENV SYLLINT_REPO_ROOT=/opt/syllabus-intelligence \
    AIRFLOW__CORE__DAGS_FOLDER=/opt/airflow/dags \
    AIRFLOW__CORE__LOAD_EXAMPLES=False \
    DBT_PROFILES_DIR=/opt/syllabus-intelligence/services/pipeline/dbt
