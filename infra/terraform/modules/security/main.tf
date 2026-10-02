variable "project_id" { type = string }
variable "project_number" { type = string }
variable "region" { type = string }
variable "name" { type = string }
variable "full" { type = bool }
variable "access_policy_id" { type = string }
variable "vpc_sc_enforce" { type = bool }
variable "labels" { type = map(string) }

# KMS key rings can never be deleted, so a suffix keeps destroy/re-apply cycles working.
resource "random_id" "keyring" {
  byte_length = 3
}

resource "google_kms_key_ring" "this" {
  project  = var.project_id
  name     = "${var.name}-${random_id.keyring.hex}"
  location = var.region
}

resource "google_kms_crypto_key" "keys" {
  #checkov:skip=CKV_GCP_82:destroyable demo stack; production overlay adds lifecycle.prevent_destroy (cannot be set conditionally)
  for_each        = toset(["sql", "gke", "cache", "reports", "images"])
  name            = each.key
  key_ring        = google_kms_key_ring.this.id
  rotation_period = "7776000s" # 90 days
  labels          = var.labels
}

# --- Service agents that must be able to use the CMEK keys -------------------
resource "google_project_service_identity" "sql" {
  provider = google-beta
  project  = var.project_id
  service  = "sqladmin.googleapis.com"
}

resource "google_project_service_identity" "artifactregistry" {
  provider = google-beta
  project  = var.project_id
  service  = "artifactregistry.googleapis.com"
}

data "google_storage_project_service_account" "gcs" {
  project = var.project_id
}

locals {
  key_users = merge(
    {
      sql     = "serviceAccount:${google_project_service_identity.sql.email}"
      reports = "serviceAccount:${data.google_storage_project_service_account.gcs.email_address}"
      images  = "serviceAccount:${google_project_service_identity.artifactregistry.email}"
    },
    var.full ? {
      gke   = "serviceAccount:service-${var.project_number}@container-engine-robot.iam.gserviceaccount.com"
      cache = "serviceAccount:service-${var.project_number}@cloud-redis.iam.gserviceaccount.com"
    } : {}
  )
}

resource "google_kms_crypto_key_iam_member" "agents" {
  for_each      = local.key_users
  crypto_key_id = google_kms_crypto_key.keys[each.key].id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = each.value
}

# --- Cloud Armor edge policy (full profile): OWASP rules + per-IP rate limit -----
resource "google_compute_security_policy" "edge" {
  count       = var.full ? 1 : 0
  project     = var.project_id
  name        = "${var.name}-edge"
  description = "WAF and rate limiting in front of the migrated service"

  rule {
    action      = "deny(403)"
    priority    = 1000
    description = "OWASP SQL injection"
    match {
      expr {
        expression = "evaluatePreconfiguredWaf('sqli-v33-stable')"
      }
    }
  }

  rule {
    action      = "deny(403)"
    priority    = 1001
    description = "OWASP cross-site scripting"
    match {
      expr {
        expression = "evaluatePreconfiguredWaf('xss-v33-stable')"
      }
    }
  }

  rule {
    action      = "deny(403)"
    priority    = 1002
    description = "Log4Shell (CVE-2021-44228) lookups"
    match {
      expr {
        expression = "evaluatePreconfiguredExpr('cve-canary')"
      }
    }
  }

  rule {
    action      = "rate_based_ban"
    priority    = 2000
    description = "Per-IP rate limit"
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    rate_limit_options {
      conform_action   = "allow"
      exceed_action    = "deny(429)"
      enforce_on_key   = "IP"
      ban_duration_sec = 300
      rate_limit_threshold {
        count        = 600
        interval_sec = 60
      }
    }
  }

  rule {
    action      = "allow"
    priority    = 2147483647
    description = "Default allow"
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }
}

# --- VPC Service Controls perimeter (full profile, needs an org access policy) -----
locals {
  perimeter_config = {
    resources = ["projects/${var.project_number}"]
    restricted_services = [
      "sqladmin.googleapis.com",
      "storage.googleapis.com",
      "secretmanager.googleapis.com",
      "cloudkms.googleapis.com",
      "artifactregistry.googleapis.com",
    ]
  }
}

resource "google_access_context_manager_service_perimeter" "this" {
  count  = var.access_policy_id == null ? 0 : 1
  parent = "accessPolicies/${var.access_policy_id}"
  name   = "accessPolicies/${var.access_policy_id}/servicePerimeters/${replace(var.name, "-", "_")}_perimeter"
  title  = "${var.name}-perimeter"

  # Roll out in dry-run first: violations are logged, nothing is blocked.
  use_explicit_dry_run_spec = !var.vpc_sc_enforce

  dynamic "status" {
    for_each = var.vpc_sc_enforce ? [1] : []
    content {
      resources           = local.perimeter_config.resources
      restricted_services = local.perimeter_config.restricted_services
    }
  }

  dynamic "spec" {
    for_each = var.vpc_sc_enforce ? [] : [1]
    content {
      resources           = local.perimeter_config.resources
      restricted_services = local.perimeter_config.restricted_services
    }
  }
}

output "key_ids" {
  value = { for k, v in google_kms_crypto_key.keys : k => v.id }
  # Keys are only usable once the service agents hold encrypt/decrypt on them.
  depends_on = [google_kms_crypto_key_iam_member.agents]
}

output "edge_policy" {
  value = var.full ? google_compute_security_policy.edge[0].name : null
}
