# The cluster that replaces the always-on VM (PRD section 18.2).
#
# Section 18.2's reasoning does not change: a Celery worker long-polls its
# broker, Cloud Run throttles CPU outside requests, so the workers cannot be
# serverless and need somewhere always-on to live. What changes is what that
# somewhere is.
#
# The VM ran a compose file written by a 175-line bash startup script, and
# deploying the worker meant resetting the instance. That is a hard restart of
# every process on the box, with no health gate and no rollback. The same four
# workloads on Kubernetes get rolling updates, readiness gates, declarative
# resource limits, and an Airflow deployment that is the project's own
# supported one rather than `airflow standalone`.
#
# Deliberately a *zonal* Standard cluster on one small node pool, not Autopilot:
#
#   - One zonal cluster per billing account carries a control-plane credit, so
#     the control plane is free where a second cluster or a regional one is not.
#   - Autopilot bills the pod resource requests, and this workload's requests
#     are dominated by the worker image's Docling and torch footprint. Standard
#     on one e2-standard-2 node is the same machine the VM was, at the same
#     price, which keeps section 18.5's cost envelope intact.
#
# The honest cost of that choice: a single-node zonal cluster is not highly
# available. A node upgrade or a zone incident is downtime. For a product whose
# workers process a queue that survives in Redis, that is the right trade at
# this scale, and it is a node-pool size change rather than a redesign when it
# stops being.

locals {
  cluster_name = "syllint-${var.environment}-cluster"
  # Pods run as this Kubernetes service account, bound to the Google service
  # account below through Workload Identity.
  k8s_namespace       = "syllint"
  k8s_service_account = "syllint-workload"
}

resource "google_container_cluster" "main" {
  name     = local.cluster_name
  location = "${var.gcp_region}-a"

  # The default pool cannot be configured after creation, so it is created and
  # immediately replaced by the managed pool below. This is the documented
  # pattern, not a workaround.
  remove_default_node_pool = true
  initial_node_count       = 1

  release_channel {
    # Regular rather than Rapid: security patches land promptly without this
    # cluster being the place a new Kubernetes minor is first exercised.
    channel = "REGULAR"
  }

  # The whole point of the identity setup below. Without this, pods reach
  # Google APIs as the node's service account, which means every pod on the
  # node shares one identity.
  workload_identity_config {
    workload_pool = "${var.gcp_project_id}.svc.id.goog"
  }

  # Nodes keep public egress, deliberately, and this is the one place the cost
  # envelope wins over the stricter default.
  #
  # Private nodes have no route to the internet without a Cloud NAT gateway,
  # and this cluster pulls from public registries on every cold start: KEDA and
  # External Secrets from ghcr.io, redis and postgres from Docker Hub. A NAT
  # gateway is billed hourly at roughly three to five times the node pool it
  # would serve, which would make the security posture the most expensive line
  # in section 18.5's budget.
  #
  # What actually protects the workloads is that nothing here listens publicly:
  # the broker is behind an internal load balancer, Airflow's webserver is
  # ClusterIP and reached by port-forward, and the firewall below admits only
  # VPC ranges. Node egress is outbound-only, which is the same posture the VM
  # this replaces had.
  #
  # With a budget, the upgrade is `enable_private_nodes = true` plus a
  # google_compute_router_nat, and nothing else in this file changes.
  ip_allocation_policy {}

  addons_config {
    http_load_balancing {
      # Nothing here is served over HTTP from outside; the API is on Cloud Run
      # and Redis is reached over an internal address.
      disabled = true
    }
  }

  # Redis's append-only file and MLflow's artifacts live on persistent volumes
  # in this cluster. The VM carried prevent_destroy for the same reason, and
  # losing them silently on a refactor would take the queue and the extraction
  # audit trail with it.
  deletion_protection = true

  lifecycle {
    ignore_changes = [node_config]
  }
}

resource "google_container_node_pool" "main" {
  name     = "workers"
  cluster  = google_container_cluster.main.id
  location = google_container_cluster.main.location

  initial_node_count = 1

  autoscaling {
    # One node holds the steady state. The product has two busy weeks a year
    # (section 4), and KEDA scaling the worker Deployment past what one node
    # holds is what pulls the second and third in.
    min_node_count = 1
    max_node_count = 3
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type = var.worker_node_machine_type
    disk_size_gb = 50
    disk_type    = "pd-standard"

    # Not the default compute service account, which carries project editor.
    service_account = google_service_account.node.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    workload_metadata_config {
      # Pods cannot read the node's identity from the metadata server; they get
      # their own through Workload Identity or nothing.
      mode = "GKE_METADATA"
    }

    labels = { workload = "syllint" }
  }

  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
  }
}

# ----------------------------------------------------------------- identity

# What the nodes themselves run as. Enough to pull images and emit telemetry,
# and nothing else: application credentials belong to the pods, not the node.
resource "google_service_account" "node" {
  account_id   = "syllint-${var.environment}-node"
  display_name = "GKE node pool"
}

resource "google_project_iam_member" "node" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/artifactregistry.reader",
  ])
  project = var.gcp_project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.node.email}"
}

# What the *pods* run as. This is the identity that reads Secret Manager, and
# it is reachable only from the one Kubernetes service account named below.
resource "google_service_account" "workload" {
  account_id   = "syllint-${var.environment}-workload"
  display_name = "Syllabus Intelligence workloads"
}

resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.workload.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.gcp_project_id}.svc.id.goog[${local.k8s_namespace}/${local.k8s_service_account}]"
}

resource "google_secret_manager_secret_iam_member" "workload_access" {
  for_each  = google_secret_manager_secret.app
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.workload.email}"
}

# CI applies manifests and waits on rollouts.
resource "google_project_iam_member" "deployer_cluster" {
  project = var.gcp_project_id
  role    = "roles/container.developer"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

# ------------------------------------------------------------------- broker

# A reserved internal address for the Redis Service.
#
# Cloud Run enqueues parse tasks, so the API has to reach the broker, and the
# broker now lives in the cluster. An internal load balancer gives it a VPC
# address that Cloud Run's Direct VPC egress can route to. Reserving the
# address rather than letting GKE allocate one keeps REDIS_URL on the Cloud Run
# service stable: otherwise every recreation of the Service silently repoints
# the API at nothing, and uploads queue into a void.
resource "google_compute_address" "redis" {
  name         = "syllint-${var.environment}-redis"
  subnetwork   = "default"
  address_type = "INTERNAL"
  region       = var.gcp_region
}

# Only the cluster's own nodes and Cloud Run's egress range reach the broker.
resource "google_compute_firewall" "redis_internal" {
  name    = "syllint-${var.environment}-redis-internal"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["6379"]
  }

  source_ranges = ["10.128.0.0/9"]
  target_tags   = ["gke-${local.cluster_name}"]
}
