# Where images live, and who is allowed to push them.
#
# Two things are set up here that a first deploy usually discovers the hard way:
# a repository has to exist before the first `docker push`, and CI needs a way
# to authenticate that is not a downloadable JSON key.

resource "google_artifact_registry_repository" "images" {
  location      = var.gcp_region
  repository_id = var.artifact_repository
  format        = "DOCKER"
  description   = "API and Celery worker images for Syllabus Intelligence."

  # Images accumulate one tag per commit forever otherwise. Artifact Registry
  # charges for storage above 0.5 GB, and a Python image with torch in it is
  # not small, so a term of daily deploys is the difference between free and
  # not.
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }

  cleanup_policies {
    id     = "delete-old-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s" # 7 days
    }
  }

  # Without this, tagged images are kept forever and the untagged rule above
  # collects almost nothing, since every push produces a tagged image. KEEP
  # policies take precedence over DELETE, so the ten most recent survive this
  # regardless of age; the trade is that rolling back to something older than a
  # month means rebuilding it from the tag in git.
  cleanup_policies {
    id     = "delete-stale"
    action = "DELETE"
    condition {
      older_than = "2592000s" # 30 days
    }
  }
}

# ------------------------------------------------------------ deploy identity

# Workload Identity Federation instead of a service-account key. A JSON key is
# a permanent credential that lives in a GitHub secret, and its worst property
# is that nothing expires: a leaked key from a fork or a log dump keeps working
# until someone notices. Federation trades that for short-lived tokens minted
# only for this repository.
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "syllint-${var.environment}-github"
  display_name              = "GitHub Actions"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }

  # Without this, *any* GitHub repository in the world can mint a token for
  # this pool. It is the single most important line in this file.
  attribute_condition = "assertion.repository == '${var.github_repository}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "deployer" {
  account_id   = "syllint-${var.environment}-deploy"
  display_name = "GitHub Actions deployer"
}

resource "google_service_account_iam_member" "github_impersonation" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}

# Push images, roll a revision, run the migration job, and act as the runtime
# service account. Deliberately not project editor: a deploy credential that
# can delete the database is a bad trade for saving four lines.
resource "google_project_iam_member" "deployer" {
  for_each = toset([
    "roles/artifactregistry.writer",
    "roles/run.developer",
  ])
  project = var.gcp_project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

# Cloud Run refuses a deploy whose revision runs as a service account the
# deployer cannot impersonate, which is the most common first-deploy failure
# after the registry not existing.
resource "google_service_account_iam_member" "deployer_acts_as_runtime" {
  service_account_id = google_service_account.api.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

# The worker VM pulls its own images on boot, so it needs read access too.
resource "google_artifact_registry_repository_iam_member" "runtime_pull" {
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.api.email}"
}
