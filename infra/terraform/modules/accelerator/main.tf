variable "project_id" { type = string }
variable "region" { type = string }
variable "name" { type = string }
variable "source_dir" { type = string }
variable "gcloud_bin" { type = string }
variable "network_id" { type = string }
variable "subnetwork_id" { type = string }
variable "reports_kms_key" { type = string }
variable "images_kms_key" { type = string }
variable "labels" { type = map(string) }

variable "db" {
  type = object({
    host        = string
    database    = string
    user        = string
    secret_id   = string
    secret_name = string
  })
}

locals {
  # Image tag follows the source: any change to the code or Dockerfile rebuilds and redeploys.
  source_files = sort(concat(tolist(fileset(var.source_dir, "cutover/**/*.py")), ["Dockerfile"]))
  source_hash  = sha256(join("", [for f in local.source_files : filesha256("${var.source_dir}/${f}")]))
  image        = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/cutover:${substr(local.source_hash, 0, 12)}"
}

resource "google_artifact_registry_repository" "images" {
  #checkov:skip=CKV_GCP_84:CMEK is set via var.images_kms_key (module input Checkov cannot resolve statically)
  project       = var.project_id
  location      = var.region
  repository_id = var.name
  format        = "DOCKER"
  kms_key_name  = var.images_kms_key
  labels        = var.labels

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }
}

# --- Build: dedicated least-privilege Cloud Build service account --------------
resource "google_service_account" "builder" {
  project      = var.project_id
  account_id   = "${var.name}-builder"
  display_name = "Cutover image builder"
}

resource "google_project_iam_member" "builder" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/storage.objectUser", # source staging bucket
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.builder.email}"
}

resource "google_artifact_registry_repository_iam_member" "builder" {
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.builder.email}"
}

resource "terraform_data" "image" {
  triggers_replace = [local.image]

  provisioner "local-exec" {
    working_dir = var.source_dir
    command     = "\"${var.gcloud_bin}\" builds submit --project ${var.project_id} --region ${var.region} --tag ${local.image} --service-account ${google_service_account.builder.id} --default-buckets-behavior REGIONAL_USER_OWNED_BUCKET --quiet ."
  }

  depends_on = [
    google_project_iam_member.builder,
    google_artifact_registry_repository_iam_member.builder,
  ]
}

# --- Reports bucket (CMEK, versioned, public access blocked) --------------------
resource "google_storage_bucket" "reports" {
  #checkov:skip=CKV_GCP_62:object access is captured by Cloud Audit Logs (Data Access) instead of legacy bucket access logs
  project                     = var.project_id
  name                        = "${var.project_id}-${var.name}-reports"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true
  labels                      = var.labels

  versioning {
    enabled = true
  }

  encryption {
    default_kms_key_name = var.reports_kms_key
  }

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }
}

# --- Cloud Run job that executes the cutover drill against Cloud SQL ----------
resource "google_service_account" "job" {
  project      = var.project_id
  account_id   = "${var.name}-job"
  display_name = "Cutover accelerator job"
}

resource "google_secret_manager_secret_iam_member" "job_db" {
  project   = var.project_id
  secret_id = var.db.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.job.email}"
}

resource "google_storage_bucket_iam_member" "job_reports" {
  bucket = google_storage_bucket.reports.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.job.email}"
}

resource "google_cloud_run_v2_job" "cutover" {
  project             = var.project_id
  name                = "${var.name}-drill"
  location            = var.region
  deletion_protection = false
  labels              = var.labels

  template {
    task_count = 1

    template {
      service_account       = google_service_account.job.email
      timeout               = "1800s"
      max_retries           = 0
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

      containers {
        image = local.image
        args  = ["--target", "postgres", "--out", "/reports/latest"]

        env {
          name  = "TARGET_DB_HOST"
          value = var.db.host
        }
        env {
          name  = "TARGET_DB_NAME"
          value = var.db.database
        }
        env {
          name  = "TARGET_DB_USER"
          value = var.db.user
        }
        env {
          name = "TARGET_DB_PASSWORD"
          value_source {
            secret_key_ref {
              secret  = var.db.secret_name
              version = "latest"
            }
          }
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        volume_mounts {
          name       = "reports"
          mount_path = "/reports"
        }
      }

      volumes {
        name = "reports"
        gcs {
          bucket    = google_storage_bucket.reports.name
          read_only = false
        }
      }

      # Direct VPC egress: reach Cloud SQL's private IP without a connector.
      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = var.network_id
          subnetwork = var.subnetwork_id
        }
      }
    }
  }

  depends_on = [
    terraform_data.image,
    google_secret_manager_secret_iam_member.job_db,
    google_storage_bucket_iam_member.job_reports,
  ]
}

output "image" { value = local.image }
output "job_name" { value = google_cloud_run_v2_job.cutover.name }
output "reports_bucket" { value = google_storage_bucket.reports.url }
