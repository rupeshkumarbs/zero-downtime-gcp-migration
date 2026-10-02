# ADR-0001: GCP target landing zone and decision analysis

- **Status:** Accepted
- **Context:** A latency-sensitive transactional service (orders) runs on AWS and has to move to GCP, with Azure considered as an alternative target. Constraints: no downtime, no data loss, rollback possible at every step, and India data residency.

## Decision analysis (DAR)

Weighted scores, 1 (poor) to 5 (strong):

| Criterion | Weight | GCP | AWS (stay) | Azure |
|---|---|---|---|---|
| Fit with the target data/AI platform (BigQuery, Vertex AI) | 25% | 5 | 3 | 3 |
| Security perimeter controls (VPC-SC, CMEK everywhere, BinAuthz) | 20% | 5 | 4 | 4 |
| Managed Kubernetes operating model (Autopilot) | 15% | 5 | 3 | 3 |
| Migration risk and effort | 20% | 3 | 5 | 3 |
| Cost at steady state (committed-use discounts) | 10% | 4 | 3 | 4 |
| Team skills and hiring market | 10% | 3 | 5 | 4 |
| **Weighted total** | | **4.30** | 3.80 | 3.40 |

Staying on AWS wins on migration risk, but loses on the platform criteria that motivated the move. The risk gap is closed by the cutover design (ADR-0002, ADR-0003) rather than by staying put.

## Decision

- **Network:** custom-mode VPC with global routing; Private Service Access for Cloud SQL and Memorystore (private IP only); Cloud NAT for egress; HA VPN to AWS (4 tunnels, BGP, 99.99% SLA) during the migration. Only TCP 443 and 5432 are allowed in from AWS.
- **Compute:** private GKE Autopilot cluster (`full` profile) and Cloud Run with Direct VPC egress for jobs (`lean` profile).
- **Data:** Cloud SQL for PostgreSQL with private IP, `ENCRYPTED_ONLY` TLS, PITR, `cloudsql.logical_decoding` for CDC, and pgAudit. Memorystore with AUTH and in-transit TLS.
- **Security:** CMEK (Cloud KMS, 90-day rotation) for Cloud SQL, GKE secrets, Memorystore, GCS and Artifact Registry. Cloud Armor with OWASP rules, the Log4Shell rule and per-IP rate limits. VPC Service Controls rolled out in **dry-run first**, then enforced. Binary Authorization on GKE.
- **Delivery:** everything in Terraform; Argo CD for in-cluster state; images built by Cloud Build with a least-privilege service account.

## Consequences

- A single `profile` variable switches between a lean, low-cost footprint and the full landing zone, so the same code serves demos and production designs.
- KMS key rings cannot be deleted, so names carry a random suffix to keep destroy/re-apply cycles working. Production adds `prevent_destroy`.
