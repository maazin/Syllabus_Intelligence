#!/usr/bin/env bash
# Wire this GitHub repository for the Deploy workflow, from `terraform output`.
#
# Run once, after `terraform apply` succeeds (DEPLOY.md step 6):
#
#   export CLOUDFLARE_API_TOKEN=...     # the token you put in terraform.tfvars
#   scripts/configure_github.sh --dry-run
#   scripts/configure_github.sh
#
# Why a script rather than a table of values to copy: the Deploy workflow's
# `configured` gate refuses to start unless all eight settings exist, because a
# partial set once meant a deploy that migrated the production database and
# then failed. Eight hand-copied values is exactly how a partial set happens,
# and a mistyped region or domain fails later with an error about something
# else. Every value here comes from the state that provisioned the resources.
#
# Secrets are piped to `gh secret set` on stdin, never passed as arguments, so
# they do not appear in the process list or in shell history.
set -euo pipefail

DRY_RUN=false
[ "${1:-}" = "--dry-run" ] && DRY_RUN=true

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="$ROOT/infra/terraform"

command -v gh >/dev/null || { echo "gh (GitHub CLI) is required" >&2; exit 1; }
command -v terraform >/dev/null || { echo "terraform is required" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Run 'gh auth login' first" >&2; exit 1; }

# The one input that is not in Terraform state as an output. It is a
# credential, and outputting it would put it in every `terraform output`.
: "${CLOUDFLARE_API_TOKEN:?export CLOUDFLARE_API_TOKEN (the token from terraform.tfvars)}"

REPO="${GITHUB_REPOSITORY:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"

out() {
  local value
  if ! value=$(terraform -chdir="$TF_DIR" output -raw "$1" 2>/dev/null) || [ -z "$value" ]; then
    echo "terraform output '$1' is empty. Has 'terraform apply' completed in infra/terraform?" >&2
    exit 1
  fi
  printf '%s' "$value"
}

# Read everything before writing anything, so a missing output cannot leave the
# repository half-configured, which is the state this script exists to avoid.
#
# Parallel indexed arrays rather than `declare -A`: macOS ships bash 3.2, which
# has no associative arrays, and this runs on the laptop that ran Terraform.
VAR_NAMES=(GCP_PROJECT_ID GCP_REGION ARTIFACT_REPOSITORY APP_DOMAIN)
VAR_VALUES=(
  "$(out gcp_project_id)"
  "$(out gcp_region)"
  "$(out artifact_repository)"
  "$(out app_domain)"
)
SECRET_NAMES=(GCP_WORKLOAD_IDENTITY_PROVIDER GCP_SERVICE_ACCOUNT CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_API_TOKEN)
SECRET_VALUES=(
  "$(out workload_identity_provider)"
  "$(out deploy_service_account)"
  "$(out cloudflare_account_id)"
  "$CLOUDFLARE_API_TOKEN"
)

echo "Repository: $REPO"
echo
echo "Variables (visible in the repo settings, and in the built app anyway):"
for i in "${!VAR_NAMES[@]}"; do
  printf '  %-32s %s\n' "${VAR_NAMES[$i]}" "${VAR_VALUES[$i]}"
done
echo
echo "Secrets (values never printed):"
for i in "${!SECRET_NAMES[@]}"; do
  printf '  %-32s %d characters\n' "${SECRET_NAMES[$i]}" "${#SECRET_VALUES[$i]}"
done
echo

if $DRY_RUN; then
  echo "Dry run. Nothing was written. Re-run without --dry-run to apply."
  exit 0
fi

for i in "${!VAR_NAMES[@]}"; do
  gh variable set "${VAR_NAMES[$i]}" --repo "$REPO" --body "${VAR_VALUES[$i]}"
done
for i in "${!SECRET_NAMES[@]}"; do
  printf '%s' "${SECRET_VALUES[$i]}" | gh secret set "${SECRET_NAMES[$i]}" --repo "$REPO"
done

echo
echo "All 8 set. Start the first deploy with:"
echo "  gh workflow run Deploy --repo $REPO"
echo "  gh run watch --repo $REPO"
