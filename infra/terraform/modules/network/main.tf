variable "project_id" { type = string }
variable "region" { type = string }
variable "name" { type = string }
variable "enable_nat" { type = bool }

variable "cidrs" {
  type = object({
    subnet_cidr   = string
    pods_cidr     = string
    services_cidr = string
    master_cidr   = string
  })
}

variable "aws_vpn" {
  type = object({
    peer_ips         = list(string)
    shared_secrets   = list(string)
    peer_bgp_ips     = list(string)
    inside_cidrs     = list(string)
    aws_asn          = number
    aws_vpc_cidr     = string
    cloud_router_asn = optional(number, 65010)
  })
  default   = null
  sensitive = true
}

locals {
  vpn     = var.aws_vpn != null
  tunnels = local.vpn ? 4 : 0
}

resource "google_compute_network" "vpc" {
  project                 = var.project_id
  name                    = "${var.name}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "GLOBAL"
}

resource "google_compute_subnetwork" "workloads" {
  project                  = var.project_id
  name                     = "${var.name}-workloads"
  region                   = var.region
  network                  = google_compute_network.vpc.id
  ip_cidr_range            = var.cidrs.subnet_cidr
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.cidrs.pods_cidr
  }

  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = var.cidrs.services_cidr
  }

  log_config {
    aggregation_interval = "INTERVAL_5_SEC"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

# Private Service Access: Cloud SQL and Memorystore get private IPs only.
resource "google_compute_global_address" "psa" {
  project       = var.project_id
  name          = "${var.name}-psa"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 20
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "psa" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.psa.name]
}

resource "google_compute_router" "router" {
  count   = var.enable_nat || local.vpn ? 1 : 0
  project = var.project_id
  name    = "${var.name}-router"
  region  = var.region
  network = google_compute_network.vpc.id

  bgp {
    asn = local.vpn ? var.aws_vpn.cloud_router_asn : 64514
  }
}

resource "google_compute_router_nat" "nat" {
  count                              = var.enable_nat ? 1 : 0
  project                            = var.project_id
  name                               = "${var.name}-nat"
  router                             = google_compute_router.router[0].name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# --- HA VPN to the source AWS VPC (99.99% SLA: 2 interfaces x 2 AWS connections) ---
resource "google_compute_ha_vpn_gateway" "aws" {
  count   = local.vpn ? 1 : 0
  project = var.project_id
  name    = "${var.name}-to-aws"
  region  = var.region
  network = google_compute_network.vpc.id
}

resource "google_compute_external_vpn_gateway" "aws" {
  count           = local.vpn ? 1 : 0
  project         = var.project_id
  name            = "${var.name}-aws-peer"
  redundancy_type = "FOUR_IPS_REDUNDANCY"

  dynamic "interface" {
    for_each = local.vpn ? var.aws_vpn.peer_ips : []
    content {
      id         = interface.key
      ip_address = interface.value
    }
  }
}

resource "google_compute_vpn_tunnel" "aws" {
  count                           = local.tunnels
  project                         = var.project_id
  name                            = "${var.name}-aws-${count.index}"
  region                          = var.region
  vpn_gateway                     = google_compute_ha_vpn_gateway.aws[0].id
  vpn_gateway_interface           = floor(count.index / 2)
  peer_external_gateway           = google_compute_external_vpn_gateway.aws[0].id
  peer_external_gateway_interface = count.index
  shared_secret                   = var.aws_vpn.shared_secrets[count.index]
  router                          = google_compute_router.router[0].id
  ike_version                     = 2
}

resource "google_compute_router_interface" "aws" {
  count      = local.tunnels
  project    = var.project_id
  name       = "${var.name}-aws-if-${count.index}"
  router     = google_compute_router.router[0].name
  region     = var.region
  ip_range   = var.aws_vpn.inside_cidrs[count.index]
  vpn_tunnel = google_compute_vpn_tunnel.aws[count.index].name
}

resource "google_compute_router_peer" "aws" {
  count           = local.tunnels
  project         = var.project_id
  name            = "${var.name}-aws-bgp-${count.index}"
  router          = google_compute_router.router[0].name
  region          = var.region
  peer_ip_address = var.aws_vpn.peer_bgp_ips[count.index]
  peer_asn        = var.aws_vpn.aws_asn
  interface       = google_compute_router_interface.aws[count.index].name
}

# Only the migration ports are open from AWS: HTTPS (shadow traffic) and Postgres (CDC/backfill).
resource "google_compute_firewall" "from_aws" {
  count     = local.vpn ? 1 : 0
  project   = var.project_id
  name      = "${var.name}-allow-aws-migration"
  network   = google_compute_network.vpc.id
  direction = "INGRESS"
  priority  = 1000

  source_ranges = [var.aws_vpn.aws_vpc_cidr]

  allow {
    protocol = "tcp"
    ports    = ["443", "5432"]
  }

  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}

output "network_id" {
  value = google_compute_network.vpc.id
  # Cloud SQL / Memorystore need the peering in place before they can use private IP.
  depends_on = [google_service_networking_connection.psa]
}

output "subnetwork_id" { value = google_compute_subnetwork.workloads.id }
output "pods_range_name" { value = "pods" }
output "services_range_name" { value = "services" }
