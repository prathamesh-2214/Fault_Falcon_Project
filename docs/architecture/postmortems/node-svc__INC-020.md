# Postmortem INC-020: Elevated 5xx errors on node-svc

Published: 2026-04-08T14:23:32.072323+00:00

Date: 2026-04-05T01:05 UTC. Severity: SEV-2. Alert: node-svc: 5XX error rate > 2 % for 5 min on node-svc.

Impact: A share of requests failed with 5xx on node-svc.

Root cause: Terraform change shrank aws_ebs_volume.node_svc_data days earlier; the disk filled up.

Breaking change: infra/node-svc.tf:7 (aws_ebs_volume.node_svc_data). infra/node-svc.tf:7 sets disk_gb 750 -> 150 on aws_ebs_volume.node_svc_data.

How it was fixed: Restore disk_gb = 750 on aws_ebs_volume.node_svc_data (terraform tf-r3). Mitigated at 2026-04-05T05:53 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `write failed: No space left on device`..

Services affected: node-svc, compute-svc, storage-svc, cloud-api, api-gateway.
