variable "project_id" { type = string }
variable "region" { type = string }
variable "name" { type = string }
variable "full" { type = bool }
variable "network_id" { type = string }
variable "sql_kms_key" { type = string }
variable "cache_kms_key" { type = string }
variable "labels" { type = map(string) }

resource "random_id" "instance" {
  byte_length = 2 # Cloud SQL names are reserved for a week after deletion
}

resource "google_sql_database_instance" "target" {
  #checkov:skip=CKV_GCP_6:ssl_mode=ENCRYPTED_ONLY enforces TLS on every connection; client certificates are impractical for serverless clients (Cloud Run)
  project             = var.project_id
  name                = "${var.name}-target-${random_id.instance.hex}"
  region              = var.region
  database_version    = "POSTGRES_18"
  encryption_key_name = var.sql_kms_key
  deletion_protection = var.full

  settings {
    edition           = "ENTERPRISE"
    tier              = var.full ? "db-custom-2-7680" : "db-g1-small"
    availability_type = var.full ? "REGIONAL" : "ZONAL"
    disk_type         = "PD_SSD"
    disk_size         = 10
    disk_autoresize   = true
    user_labels       = var.labels

    ip_configuration {
      ipv4_enabled    = false
      private_network = var.network_id
      ssl_mode        = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "20:30" # 02:00 IST
      transaction_log_retention_days = 7
    }

    # Logical decoding enables CDC (Datastream / Debezium) for the reverse sync
    # that keeps AWS a hot rollback target after the write cutover.
    database_flags {
      name  = "cloudsql.logical_decoding"
      value = "on"
    }

    # CIS GCP benchmark logging + pgAudit for DDL/role changes.
    database_flags {
      name  = "log_checkpoints"
      value = "on"
    }

    database_flags {
      name  = "log_connections"
      value = "on"
    }

    database_flags {
      name  = "log_disconnections"
      value = "on"
    }

    database_flags {
      name  = "log_duration"
      value = "on"
    }

    database_flags {
      name  = "log_hostname"
      value = "on"
    }

    database_flags {
      name  = "log_lock_waits"
      value = "on"
    }

    database_flags {
      name  = "log_min_duration_statement"
      value = "-1"
    }

    database_flags {
      name  = "log_min_error_statement"
      value = "error"
    }

    database_flags {
      name  = "log_min_messages"
      value = "error"
    }

    database_flags {
      name  = "log_statement"
      value = "ddl"
    }

    database_flags {
      name  = "cloudsql.enable_pgaudit"
      value = "on"
    }

    database_flags {
      name  = "pgaudit.log"
      value = "ddl,role"
    }

    insights_config {
      query_insights_enabled  = true
      record_application_tags = true
    }

    maintenance_window {
      day          = 7
      hour         = 21
      update_track = "stable"
    }
  }
}

resource "google_sql_database" "orders" {
  project  = var.project_id
  name     = "orders"
  instance = google_sql_database_instance.target.name
}

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "google_sql_user" "cutover" {
  project  = var.project_id
  name     = "cutover"
  instance = google_sql_database_instance.target.name
  password = random_password.db.result
}

resource "google_secret_manager_secret" "db_password" {
  project   = var.project_id
  secret_id = "${var.name}-db-password"
  labels    = var.labels

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.db_password.id
  secret_data = random_password.db.result
}

# Memorystore (full profile): session/cache tier for the migrated service.
resource "google_redis_instance" "cache" {
  count                   = var.full ? 1 : 0
  project                 = var.project_id
  name                    = "${var.name}-cache"
  region                  = var.region
  tier                    = "STANDARD_HA"
  memory_size_gb          = 1
  redis_version           = "REDIS_7_2"
  authorized_network      = var.network_id
  connect_mode            = "PRIVATE_SERVICE_ACCESS"
  transit_encryption_mode = "SERVER_AUTHENTICATION"
  auth_enabled            = true
  customer_managed_key    = var.cache_kms_key
  labels                  = var.labels
}

output "instance_name" {
  value = google_sql_database_instance.target.name
}

output "connection" {
  value = {
    host        = google_sql_database_instance.target.private_ip_address
    database    = google_sql_database.orders.name
    user        = google_sql_user.cutover.name
    secret_id   = google_secret_manager_secret.db_password.id
    secret_name = google_secret_manager_secret.db_password.secret_id
  }
  depends_on = [google_secret_manager_secret_version.db_password]
}

output "redis_host" {
  value = var.full ? google_redis_instance.cache[0].host : null
}
