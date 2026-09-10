# Provider versions (PRD sections 18.1 and 21).
#
# Every provider is pinned to a minor version. An unpinned provider is how a
# `terraform apply` that worked last week proposes to replace a database this
# week, and the blast radius here includes the object store holding every
# uploaded syllabus.

terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.12"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.48"
    }
    neon = {
      source  = "kislerdm/neon"
      version = "~> 0.6"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State lives in a bucket rather than on a laptop. Values come from
  # `-backend-config` at init time so the bucket name is not committed.
  backend "gcs" {
    prefix = "syllabus-intelligence"
  }
}

provider "google" {
  project = var.gcp_project_id
  region  = var.gcp_region
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

provider "neon" {
  api_key = var.neon_api_key
}
