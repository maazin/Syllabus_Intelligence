# DNS and edge (PRD section 18.1).
#
# Cloudflare sits in front for the free CDN, DNS, and rate limiting. The PRD's
# reason is concrete: "Someone will upload 500 PDFs at once."

data "cloudflare_zone" "app" {
  name = var.app_domain
}

resource "cloudflare_record" "api" {
  zone_id = data.cloudflare_zone.app.id
  name    = "api"
  type    = "CNAME"
  content = trimprefix(google_cloud_run_v2_service.api.uri, "https://")
  proxied = true
  comment = "Managed by Terraform. Proxied so rate limiting applies."
}

# The upload endpoint is the one that costs real money per request: each new
# document is two model calls. Everything else is cheap by comparison.
resource "cloudflare_ruleset" "upload_rate_limit" {
  zone_id = data.cloudflare_zone.app.id
  name    = "Upload rate limiting"
  kind    = "zone"
  phase   = "http_ratelimit"

  rules {
    action      = "block"
    description = "Cap document uploads per client"
    expression  = "(http.request.uri.path contains \"/api/v1/documents\")"
    enabled     = true

    ratelimit {
      characteristics     = ["ip.src", "cf.colo.id"]
      period              = 60
      requests_per_period = 20
      mitigation_timeout  = 600
    }
  }
}
