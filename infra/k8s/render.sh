#!/usr/bin/env bash
# Substitute deploy-time values into the manifests and print them.
#
# The manifests carry PLACEHOLDER_ tokens rather than being templated by Helm
# or Kustomize overlays. Two reasons: every value here comes from a
# `terraform output`, so the deploy already has to shell out to read them, and
# a plain manifest stays readable and `kubectl diff`-able without rendering it
# first. The trade is that this script is the only place that knows the list,
# which is why it fails loudly on an unsubstituted token rather than applying
# a manifest with the word PLACEHOLDER in it.
set -euo pipefail

: "${PROJECT_ID:?}" "${CLUSTER_NAME:?}" "${CLUSTER_LOCATION:?}"
: "${WORKLOAD_SA:?}" "${REDIS_IP:?}" "${R2_BUCKET:?}" "${R2_ENDPOINT:?}"
: "${WORKER_IMAGE:?}" "${MLFLOW_IMAGE:?}"

# Numbered files only. airflow-values.yaml is Helm input, not a manifest,
# and deliberately carries no numeric prefix so this glob cannot reach it.
#
# A `---` is emitted between files. Plain `cat` runs the last document of one
# file into the first of the next, and YAML does not complain: it merges them
# into a single document where the later keys win. The symptom is an object
# that silently vanishes from the apply, which is how the ServiceAccount, the
# ConfigMap and the KEDA ScaledObject disappeared the first time this ran.
manifests=""
for file in "$(dirname "$0")"/[0-9][0-9]-*.yaml; do
  manifests="${manifests}"$'\n---\n'"$(cat "$file")"
done

rendered=$(printf '%s' "$manifests" \
  | sed "s|PLACEHOLDER_PROJECT_ID|${PROJECT_ID}|g" \
  | sed "s|PLACEHOLDER_CLUSTER_NAME|${CLUSTER_NAME}|g" \
  | sed "s|PLACEHOLDER_CLUSTER_LOCATION|${CLUSTER_LOCATION}|g" \
  | sed "s|PLACEHOLDER_WORKLOAD_SA|${WORKLOAD_SA}|g" \
  | sed "s|PLACEHOLDER_REDIS_IP|${REDIS_IP}|g" \
  | sed "s|PLACEHOLDER_R2_BUCKET|${R2_BUCKET}|g" \
  | sed "s|PLACEHOLDER_R2_ENDPOINT|${R2_ENDPOINT}|g" \
  | sed "s|PLACEHOLDER_WORKER_IMAGE|${WORKER_IMAGE}|g" \
  | sed "s|PLACEHOLDER_MLFLOW_IMAGE|${MLFLOW_IMAGE}|g")

if grep -q PLACEHOLDER_ <<<"$rendered"; then
  echo "unsubstituted placeholders remain:" >&2
  grep -o 'PLACEHOLDER_[A-Z_]*' <<<"$rendered" | sort -u >&2
  exit 1
fi

printf '%s\n' "$rendered"
