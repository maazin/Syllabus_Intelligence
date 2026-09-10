# Secrets (PRD section 24).
#
# Values live in Secret Manager and are referenced by Cloud Run, never set as
# plain environment variables on the service. A secret in a revision's config is
# readable by anyone with view access to the project, and the model API key and
# the database password both grant more than that audience should have.

locals {
  # The map key becomes the environment variable name on Cloud Run, upcased with
  # dashes turned into underscores. It has to match what the code actually reads
  # or the variable is simply absent at runtime, which shows up as an empty
  # credential rather than as a startup error.
  secrets = {
    database-url = neon_project.main.connection_uri
    llm-api-key  = var.llm_api_key
    jwt-secret   = random_password.jwt.result

    # R2 speaks the S3 API, and Cloudflare derives the S3 credential pair from
    # an API token rather than issuing one: the Access Key ID is the token's id,
    # and the Secret Access Key is the SHA-256 of the token value. Passing the
    # token value itself as the access key, which is the obvious thing to do,
    # produces a SignatureDoesNotMatch on every upload.
    r2-access-key-id     = cloudflare_api_token.r2.id
    r2-secret-access-key = sha256(cloudflare_api_token.r2.value)

    # Sign-in is a magic link, so a broken SMTP credential is a total outage of
    # authentication, not a degraded feature.
    smtp-password = var.smtp_password

    google-calendar-client-secret = var.google_calendar_client_secret
  }
}

# Generated rather than supplied: nobody needs to know the signing key, and one
# that never passes through a human is one that cannot be pasted into a chat.
resource "random_password" "jwt" {
  length  = 64
  special = true
}

resource "cloudflare_api_token" "r2" {
  name = "syllint-${var.environment}-r2"

  policy {
    permission_groups = [
      # Object read and write on the one bucket. Not account-wide.
      "2efd5506f9c8494dacb1fa10a3e7d5b6",
    ]
    resources = {
      "com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${cloudflare_r2_bucket.syllabi.name}" = "*"
    }
  }
}

resource "google_secret_manager_secret" "app" {
  for_each  = local.secrets
  secret_id = "syllint-${var.environment}-${each.key}"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "app" {
  for_each    = local.secrets
  secret      = google_secret_manager_secret.app[each.key].id
  secret_data = each.value
}

# The API service account reads secrets and nothing else. A default service
# account would carry project-editor, which is far more than an HTTP surface
# needs.
resource "google_service_account" "api" {
  account_id   = "syllint-${var.environment}-api"
  display_name = "Syllabus Intelligence API"
}

resource "google_secret_manager_secret_iam_member" "api_access" {
  for_each  = google_secret_manager_secret.app
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}
