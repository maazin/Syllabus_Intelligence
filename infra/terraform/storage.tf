# Object storage and the database (PRD section 18.1).

# Cloudflare R2 holds uploaded syllabi and raw extraction output. Chosen over S3
# because egress is always free and the free tier is 10 GB, and because it
# implements the S3 API, so the same boto3 code and the same R2_* variable names
# work against MinIO locally.
resource "cloudflare_r2_bucket" "syllabi" {
  account_id = var.cloudflare_account_id
  name       = "syllint-${var.environment}-syllabi"
  location   = "ENAM"
}

# Section 18.4 calls for a lifecycle rule moving originals to Infrequent Access
# after 30 days: the 10 GB free tier runs out around 10,000 documents (term 2),
# and an original is read once at parse time and then almost never, which is
# exactly what IA is priced for.
#
# The v4 Cloudflare provider has no resource for R2 lifecycle rules, so this is
# applied out of band until the provider grows one. Left as a documented gap
# rather than silently dropped, because it is the difference between $0.015 and
# $0.01 per GB-month on the fastest-growing thing we store:
#
#   wrangler r2 bucket lifecycle add syllint-production-syllabi \
#     --prefix documents/ --transition-days 30 --storage-class InfrequentAccess
#
# tflint and the reviewer both see it here rather than discovering the bill.

# Neon is the only free Postgres that survives a seasonal workload (18.1).
# Supabase Free pauses a project after a week idle, which is fatal for a product
# used heavily in week 1 and then quietly until registration week.
resource "neon_project" "main" {
  name      = "syllint-${var.environment}"
  region_id = "aws-us-east-2"

  # Auto-suspend after five minutes idle is what keeps the 100 compute-hour
  # free tier viable between the product's two busy windows.
  history_retention_seconds = 86400

  branch {
    name          = "main"
    database_name = "syllint"
    role_name     = "syllint_app"
  }
}

resource "neon_database" "app" {
  project_id = neon_project.main.id
  branch_id  = neon_project.main.default_branch_id
  name       = "syllint"
  owner_name = neon_project.main.database_user
}
