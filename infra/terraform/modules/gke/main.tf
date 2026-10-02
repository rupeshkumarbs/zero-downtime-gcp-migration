variable "project_id" { type = string }
variable "region" { type = string }
variable "name" { type = string }
variable "network_id" { type = string }
variable "subnetwork_id" { type = string }
variable "pods_range_name" { type = string }
variable "services_range_name" { type = string }
variable "master_cidr" { type = string }
variable "kms_key_id" { type = string }
variable "labels" { type = map(string) }

variable "authorized_networks" {
  type = list(object({
    name = string
    cidr = string
  }))
}

# Private Autopilot cluster: Google-managed nodes, Workload Identity on by default,
# Kubernetes secrets envelope-encrypted with our CMEK key.
resource "google_container_cluster" "this" {
  #checkov:skip=CKV_GCP_12:Autopilot always runs GKE Dataplane V2, which enforces NetworkPolicy
  #checkov:skip=CKV_GCP_13:Autopilot disables client certificate issuance; not configurable
  #checkov:skip=CKV_GCP_69:Autopilot always enables the GKE metadata server (Workload Identity)
  #checkov:skip=CKV_GCP_61:subnet VPC flow logs are enabled in the network module; intranode visibility is not configurable on Autopilot
  #checkov:skip=CKV_GCP_65:Google Groups for RBAC needs a Workspace/Cloud Identity domain; set authenticator_groups_config in org deployments
  project             = var.project_id
  name                = "${var.name}-gke"
  location            = var.region
  enable_autopilot    = true
  network             = var.network_id
  subnetwork          = var.subnetwork_id
  deletion_protection = false
  resource_labels     = var.labels

  ip_allocation_policy {
    cluster_secondary_range_name  = var.pods_range_name
    services_secondary_range_name = var.services_range_name
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = var.master_cidr
  }

  master_authorized_networks_config {
    dynamic "cidr_blocks" {
      for_each = var.authorized_networks
      content {
        cidr_block   = cidr_blocks.value.cidr
        display_name = cidr_blocks.value.name
      }
    }
  }

  database_encryption {
    state    = "ENCRYPTED"
    key_name = var.kms_key_id
  }

  release_channel {
    channel = "REGULAR"
  }

  binary_authorization {
    evaluation_mode = "PROJECT_SINGLETON_POLICY_ENFORCE"
  }

  # Service mesh (Istio) for mirroring / weighted routing is enabled through the
  # fleet: see deploy/README.md. Kept out of Terraform so mesh upgrades follow
  # Google's release channel rather than our apply cadence.
}

output "cluster_name" { value = google_container_cluster.this.name }
