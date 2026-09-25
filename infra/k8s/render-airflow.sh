#!/usr/bin/env bash
# Render the Airflow Helm values. Separate from render.sh because this is input
# to `helm upgrade`, not something kubectl ever sees.
set -euo pipefail
: "${AIRFLOW_REPO:?}" "${AIRFLOW_TAG:?}"
sed -e "s|PLACEHOLDER_AIRFLOW_REPO|${AIRFLOW_REPO}|g" \
    -e "s|PLACEHOLDER_AIRFLOW_TAG|${AIRFLOW_TAG}|g" \
    "$(dirname "$0")/airflow-values.yaml"
