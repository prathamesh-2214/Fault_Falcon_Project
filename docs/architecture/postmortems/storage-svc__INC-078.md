# Postmortem INC-078: Elevated 5xx errors on compute-svc

Published: 2026-07-12T19:12:45.212410+00:00

Date: 2026-07-08T15:40 UTC. Severity: SEV-1. Alert: compute-svc: 5XX error rate > 2 % for 5 min on compute-svc.

Impact: A share of requests failed with 5xx on compute-svc.

Root cause: Terraform change shrank aws_ebs_volume.storage_svc_data days earlier; the disk filled up.

Breaking change: infra/storage-svc.tf:55 (aws_ebs_volume.storage_svc_data). infra/storage-svc.tf:55 sets disk_gb 750 -> 150 on aws_ebs_volume.storage_svc_data.

How it was fixed: Restore disk_gb = 750 on aws_ebs_volume.storage_svc_data (terraform tf-r6). Mitigated at 2026-07-08T18:13 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `Call to <*> failed: HTTP 503 Service Unavailable`..

Services affected: storage-svc, compute-svc, cloud-api, api-gateway.
