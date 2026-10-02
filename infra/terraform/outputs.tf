output "profile" {
  value = var.profile
}

output "image" {
  description = "Accelerator image built by Cloud Build."
  value       = module.accelerator.image
}

output "job_name" {
  value = module.accelerator.job_name
}

output "run_cutover" {
  description = "Execute the cutover drill (add --args to inject a fault)."
  value       = "gcloud run jobs execute ${module.accelerator.job_name} --region ${var.region} --project ${var.project_id} --wait"
}

output "run_rollback_drill" {
  value = "gcloud run jobs execute ${module.accelerator.job_name} --region ${var.region} --project ${var.project_id} --wait --args=--target,postgres,--inject-fault,canary-25,--out,/reports/rollback-drill"
}

output "reports_bucket" {
  value = module.accelerator.reports_bucket
}

output "cloud_sql_instance" {
  value = module.database.instance_name
}

output "gke_cluster" {
  value = local.full ? module.gke[0].cluster_name : null
}

output "get_gke_credentials" {
  value = local.full ? "gcloud container clusters get-credentials ${module.gke[0].cluster_name} --region ${var.region} --project ${var.project_id}" : null
}
