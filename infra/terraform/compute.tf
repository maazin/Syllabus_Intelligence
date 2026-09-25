# Compute (PRD sections 18.1 and 18.2).
#
# The split here is the whole argument of 18.2. The API is stateless and bursty,
# so it scales to zero on Cloud Run and stays inside the free grant. The Celery
# workers cannot: a worker long-polls its broker, and Cloud Run throttles CPU
# outside of requests, so a worker there starves unless you pin CPU-always-on
# and a minimum instance, which is the opposite of scaling to zero.
#
# The always-on half lives on the GKE cluster in gke.tf: Redis, the Celery
# workers, MLflow and Airflow. This file owns only the serverless half.

resource "google_cloud_run_v2_service" "api" {
  name     = "syllint-${var.environment}-api"
  location = var.gcp_region

  template {
    service_account = google_service_account.api.email

    # Direct VPC egress rather than a Serverless VPC Access connector. The
    # connector is a pair of billed e2-micro instances that run whether or not
    # anything is calling; direct egress costs nothing and is what makes the
    # private Redis address above reachable at all. Private ranges only, so
    # ordinary outbound calls to the model API still take the internet path
    # instead of being hairpinned through the VPC.
    vpc_access {
      network_interfaces {
        network    = "default"
        subnetwork = "default"
      }
      egress = "PRIVATE_RANGES_ONLY"
    }

    scaling {
      # Zero when idle. The product has two busy weeks a year (section 4), so
      # paying for a warm instance the rest of the time buys nothing.
      min_instance_count = 0
      max_instance_count = 10
    }

    containers {
      image = var.container_image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        # CPU only during requests, which is what keeps this inside the
        # 180k vCPU-second free grant.
        cpu_idle = true
      }

      # Every non-secret variable the code reads, in one map.
      #
      # Written as a loop rather than as fifteen `env` blocks because the
      # failure mode of the long form is a variable that was simply forgotten,
      # and a missing variable does not fail a deploy: it reads as an empty
      # string and breaks one feature quietly. CORS_ORIGINS is the clearest
      # example. Its default is localhost:4200, so leaving it out ships an API
      # that refuses every request the real web app makes, while passing every
      # health check.
      dynamic "env" {
        for_each = {
          ENVIRONMENT = var.environment

          R2_BUCKET   = cloudflare_r2_bucket.syllabi.name
          R2_ENDPOINT = "https://${var.cloudflare_account_id}.r2.cloudflarestorage.com"

          # The browser app is served from the apex; the API answers on api.
          # Different origins, so this has to be explicit.
          CORS_ORIGINS = "https://${var.app_domain}"

          # Where a magic link points. Wrong here means every sign-in email in
          # production sends the student to localhost.
          APP_BASE_URL             = "https://${var.app_domain}"
          INSTITUTION_EMAIL_DOMAIN = var.institution_email_domain

          EMAIL_PROVIDER = "smtp"
          EMAIL_FROM     = var.email_from
          SMTP_HOST      = var.smtp_host
          SMTP_PORT      = tostring(var.smtp_port)
          SMTP_USER      = var.smtp_user

          LLM_MODEL_SYNC   = var.llm_model_sync
          LLM_MODEL_STRONG = var.llm_model_strong
          LLM_MODEL_BATCH  = var.llm_model_batch

          # The broker is an internal load balancer in front of the cluster's
          # Redis, on a reserved address so this value survives the Service
          # being recreated. Reachable only through the VPC egress below.
          REDIS_URL = "redis://${google_compute_address.redis.address}:6379/0"

          GOOGLE_CALENDAR_CLIENT_ID = var.google_calendar_client_id
        }
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = google_secret_manager_secret.app
        content {
          name = upper(replace(env.key, "-", "_"))
          value_source {
            secret_key_ref {
              secret  = env.value.secret_id
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        http_get {
          path = "/health"
        }
        initial_delay_seconds = 5
        timeout_seconds       = 3
        failure_threshold     = 6
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }
}

# Public: the API is the backend for a browser app and does its own bearer-token
# auth. Authentication at the Cloud Run layer would block the browser entirely.
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.api.name
  location = google_cloud_run_v2_service.api.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---------------------------------------------------------------- migrations

# Schema changes run as their own job, not on API startup.
#
# Running alembic in the container's entrypoint looks simpler and is wrong here
# for two reasons. Cloud Run can start several instances of a new revision at
# once, so the migration would race itself, and a migration that fails would
# fail the health check and be reported as a bad image rather than as a bad
# migration. A job runs exactly once, to completion, with its own exit code,
# and the deploy pipeline can refuse to route traffic when it fails.
resource "google_cloud_run_v2_job" "migrate" {
  name     = "syllint-${var.environment}-migrate"
  location = var.gcp_region

  template {
    template {
      service_account = google_service_account.api.email
      max_retries     = 1
      # Neon takes a few seconds to wake from suspend, and a long migration on
      # a large table should not be killed halfway.
      timeout = "900s"

      vpc_access {
        network_interfaces {
          network    = "default"
          subnetwork = "default"
        }
        egress = "PRIVATE_RANGES_ONLY"
      }

      containers {
        # The same image the API runs, so the migration that ships is by
        # construction the one that revision expects. A separate migration
        # image can drift from the code that reads the schema.
        image       = var.container_image
        command     = ["alembic"]
        args        = ["upgrade", "head"]
        working_dir = "/app/packages/core"

        env {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.app["database-url"].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    # The deploy pipeline sets the image on every run. Without this, Terraform
    # proposes reverting the job to whatever tag was last applied, every time.
    ignore_changes = [template[0].template[0].containers[0].image]
  }
}
