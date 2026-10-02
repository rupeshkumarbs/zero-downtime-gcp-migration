locals {
  full = var.profile == "full"

  base_apis = [
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudkms.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
  ]
  full_apis = [
    "container.googleapis.com",
    "redis.googleapis.com",
  ]
  apis = toset(concat(local.base_apis, local.full ? local.full_apis : []))
}

data "google_project" "this" {
  project_id = var.project_id
}

resource "google_project_service" "apis" {
  for_each           = local.apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

module "network" {
  source = "./modules/network"

  project_id = var.project_id
  region     = var.region
  name       = var.name
  cidrs      = var.network
  enable_nat = local.full
  aws_vpn    = local.full ? var.aws_vpn : null

  depends_on = [google_project_service.apis]
}

module "security" {
  source = "./modules/security"

  project_id       = var.project_id
  project_number   = data.google_project.this.number
  region           = var.region
  name             = var.name
  full             = local.full
  access_policy_id = local.full ? var.access_policy_id : null
  vpc_sc_enforce   = var.vpc_sc_enforce
  labels           = var.labels

  depends_on = [google_project_service.apis]
}

module "database" {
  source = "./modules/database"

  project_id    = var.project_id
  region        = var.region
  name          = var.name
  full          = local.full
  network_id    = module.network.network_id
  sql_kms_key   = module.security.key_ids["sql"]
  cache_kms_key = module.security.key_ids["cache"]
  labels        = var.labels

  depends_on = [module.network, module.security]
}

module "accelerator" {
  source = "./modules/accelerator"

  project_id      = var.project_id
  region          = var.region
  name            = var.name
  source_dir      = abspath("${path.root}/../..")
  gcloud_bin      = var.gcloud_bin
  network_id      = module.network.network_id
  subnetwork_id   = module.network.subnetwork_id
  reports_kms_key = module.security.key_ids["reports"]
  images_kms_key  = module.security.key_ids["images"]
  db              = module.database.connection
  labels          = var.labels

  depends_on = [google_project_service.apis, module.security]
}

module "gke" {
  source = "./modules/gke"
  count  = local.full ? 1 : 0

  project_id          = var.project_id
  region              = var.region
  name                = var.name
  network_id          = module.network.network_id
  subnetwork_id       = module.network.subnetwork_id
  pods_range_name     = module.network.pods_range_name
  services_range_name = module.network.services_range_name
  master_cidr         = var.network.master_cidr
  authorized_networks = var.gke_authorized_networks
  kms_key_id          = module.security.key_ids["gke"]
  labels              = var.labels

  depends_on = [module.network, module.security]
}
