variable "gcp_project_id" {
  description = "GCP project hosting Cloud Run and the Terraform state bucket."
  type        = string
}

variable "gcp_region" {
  description = <<-EOT
    Cloud Run region. Defaults to us-central1 because section 18.1 notes the
    free grant (2M requests, 180k vCPU-seconds, 360k GiB-seconds per month) is
    rated there. Deploying elsewhere silently forfeits it.
  EOT
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "local, staging, or production (section 24)."
  type        = string
  default     = "production"

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "Terraform manages staging and production only; local runs on docker compose."
  }
}

variable "cloudflare_api_token" {
  description = "Cloudflare token with R2 and DNS edit scope."
  type        = string
  sensitive   = true
}

variable "cloudflare_account_id" {
  type      = string
  sensitive = true
}

variable "neon_api_key" {
  description = "Neon API key. The free tier is 0.5 GB and 100 compute-hours (18.1)."
  type        = string
  sensitive   = true
}

variable "app_domain" {
  description = "Domain serving the web app, e.g. syllabus.example.edu."
  type        = string
}

variable "llm_api_key" {
  description = <<-EOT
    Model API key. Passed to Secret Manager rather than to the service
    environment, so it is never readable from a Cloud Run revision's config.
  EOT
  type        = string
  sensitive   = true
}

variable "container_image" {
  description = "Fully qualified API image, tagged by commit rather than latest."
  type        = string
}

variable "worker_vm_machine_type" {
  description = <<-EOT
    Section 18.2: Celery cannot scale to zero on Cloud Run, because a worker
    long-polls its broker and CPU is throttled outside requests. The always-on
    VM hosts Redis, the Celery workers, MLflow, and Airflow. 8 GB once Airflow
    is included, per 18.2.
  EOT
  type        = string
  default     = "e2-standard-2"
}

variable "artifact_repository" {
  description = <<-EOT
    Artifact Registry repository holding the API and worker images. Kept as a
    variable so a second environment in the same project can share one
    repository rather than paying storage twice for identical base layers.
  EOT
  type        = string
  default     = "syllint"
}

variable "worker_image" {
  description = <<-EOT
    Celery worker image, tagged by commit like the API. The VM pulls this on
    boot, so a deploy that changes it is a VM restart rather than a rebuild.
  EOT
  type        = string
}

variable "github_repository" {
  description = <<-EOT
    owner/name of the repository allowed to deploy, e.g. "someone/syllabus-intelligence".
    Workload Identity Federation trusts this exact repository and nothing else,
    which is what makes a long-lived service-account JSON key unnecessary.
  EOT
  type        = string
}

variable "email_from" {
  description = <<-EOT
    Envelope sender for magic links. Section 18.4 is blunt about why this is a
    launch blocker rather than a detail: if sign-in mail lands in spam during
    syllabus week, signups fail silently and nobody reports it.
  EOT
  type        = string
}

variable "smtp_host" {
  description = "Transactional SMTP host (Brevo to start, SES once 300/day bites)."
  type        = string
  default     = "smtp-relay.brevo.com"
}

variable "smtp_port" {
  type    = number
  default = 587
}

variable "smtp_user" {
  description = "Relay login. Brevo uses the account email; SES uses a generated SMTP username."
  type        = string
}

variable "institution_email_domain" {
  description = "Signup is restricted to this domain (section 15.2)."
  type        = string
}

variable "llm_model_sync" {
  description = "Interactive uploads. A student is waiting, so this is the fast tier."
  type        = string
  default     = "claude-sonnet-5"
}

variable "llm_model_strong" {
  description = "Escalation tier for documents the fast model is not confident on."
  type        = string
  default     = "claude-opus-5"
}

variable "llm_model_batch" {
  description = "Corpus seeding, which runs on the batch endpoint at half price (18.1)."
  type        = string
  default     = "claude-sonnet-5"
}

variable "smtp_password" {
  description = <<-EOT
    Transactional SMTP password or API key. Sign-in is a magic link, so this
    credential being wrong is a full authentication outage rather than a
    degraded feature.
  EOT
  type        = string
  sensitive   = true
}

variable "google_calendar_client_id" {
  description = "OAuth client for calendar sync (section 13). Empty disables the feature."
  type        = string
  default     = ""
}

variable "google_calendar_client_secret" {
  type      = string
  sensitive = true
  default   = ""
}

variable "mlflow_image" {
  description = <<-EOT
    MLflow server image. Built from infra/docker/mlflow.Dockerfile rather than
    pulled from upstream, because the published image has no Postgres driver
    and MLflow's backend store here is Postgres.
  EOT
  type        = string
}

variable "airflow_image" {
  description = <<-EOT
    Airflow image with dbt installed. Built from infra/docker/airflow.Dockerfile:
    Airflow's own constraints file caps SQLAlchemy below 2.0, so it cannot share
    an environment with the application and has to be its own container.
  EOT
  type        = string
}
