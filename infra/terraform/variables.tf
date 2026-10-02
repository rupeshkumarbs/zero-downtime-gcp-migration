variable "project_id" {
  description = "GCP project to deploy into (billing must be enabled)."
  type        = string
}

variable "region" {
  description = "Primary region. asia-south1 = Mumbai."
  type        = string
  default     = "asia-south1"
}

variable "profile" {
  description = <<-EOT
    lean = accelerator on Cloud Run + zonal Cloud SQL + KMS + private VPC (~USD 1-3/day).
    full = lean + GKE Autopilot, regional HA Cloud SQL, Memorystore, Cloud Armor,
           Cloud NAT, optional HA VPN to AWS and VPC Service Controls (~USD 15-30/day).
  EOT
  type        = string
  default     = "lean"

  validation {
    condition     = contains(["lean", "full"], var.profile)
    error_message = "profile must be \"lean\" or \"full\"."
  }
}

variable "name" {
  description = "Prefix for resource names."
  type        = string
  default     = "cutover"
}

variable "gcloud_bin" {
  description = "gcloud executable used by the image build step (full path if gcloud is not first on PATH)."
  type        = string
  default     = "gcloud"
}

variable "network" {
  description = "Address plan. Must not overlap the AWS VPC when HA VPN is enabled."
  type = object({
    subnet_cidr   = string
    pods_cidr     = string
    services_cidr = string
    master_cidr   = string
  })
  default = {
    subnet_cidr   = "10.40.0.0/20"
    pods_cidr     = "10.44.0.0/14"
    services_cidr = "10.48.0.0/20"
    master_cidr   = "172.16.0.32/28"
  }
}

variable "gke_authorized_networks" {
  description = "CIDRs allowed to reach the GKE control plane (full profile)."
  type = list(object({
    name = string
    cidr = string
  }))
  default = []
}

variable "aws_vpn" {
  description = "HA VPN to the source AWS VPC (full profile). Leave null to skip."
  type = object({
    peer_ips         = list(string) # 4 AWS tunnel outside IPs (2 per AWS VPN connection)
    shared_secrets   = list(string) # 4 pre-shared keys
    peer_bgp_ips     = list(string) # 4 AWS-side BGP inside IPs
    inside_cidrs     = list(string) # 4 GCP-side /30 inside ranges, e.g. 169.254.10.2/30
    aws_asn          = number
    aws_vpc_cidr     = string
    cloud_router_asn = optional(number, 65010)
  })
  default   = null
  sensitive = true
}

variable "access_policy_id" {
  description = "Org Access Context Manager policy ID for VPC Service Controls (full profile). Null skips the perimeter."
  type        = string
  default     = null
}

variable "vpc_sc_enforce" {
  description = "false = dry-run perimeter (log only), true = enforced."
  type        = bool
  default     = false
}

variable "labels" {
  description = "Labels applied to every resource that supports them."
  type        = map(string)
  default = {
    app        = "cutover-accelerator"
    managed-by = "terraform"
  }
}
