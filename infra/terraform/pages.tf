# Where the browser app is served from (PRD section 18.1).
#
# Cloudflare Pages rather than a bucket behind a CDN: it is free at this scale,
# it is already in the zone that does the rate limiting, and static hosting for
# an Angular build is a solved problem not worth spending a VM on.

resource "cloudflare_pages_project" "web" {
  account_id        = var.cloudflare_account_id
  name              = "syllint-${var.environment}"
  production_branch = "main"

  deployment_configs {
    production {
      # Deploys are pushed from CI with wrangler against the built `dist`
      # directory, so there is no build command here. Building on Cloudflare
      # would mean the artifact that ships is not the artifact CI tested.
      environment_variables = {
        ENVIRONMENT = var.environment
      }
    }
    preview {
      environment_variables = {
        ENVIRONMENT = "staging"
      }
    }
  }
}

# The app answers on the apex; the API answers on api. (see dns.tf).
resource "cloudflare_pages_domain" "web" {
  account_id   = var.cloudflare_account_id
  project_name = cloudflare_pages_project.web.name
  domain       = var.app_domain
}

# CNAME at the apex, which Cloudflare flattens to A records automatically.
resource "cloudflare_record" "web" {
  zone_id = data.cloudflare_zone.app.id
  name    = "@"
  type    = "CNAME"
  content = cloudflare_pages_project.web.subdomain
  proxied = true
  comment = "Managed by Terraform. Points the apex at Cloudflare Pages."
}

resource "cloudflare_record" "web_www" {
  zone_id = data.cloudflare_zone.app.id
  name    = "www"
  type    = "CNAME"
  content = cloudflare_pages_project.web.subdomain
  proxied = true
  comment = "Managed by Terraform."
}
