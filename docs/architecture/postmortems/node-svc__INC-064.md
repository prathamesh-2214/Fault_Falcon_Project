# Postmortem INC-064: Elevated 5xx errors on storage-svc

Published: 2026-06-20T02:19:25.159598+00:00

Date: 2026-06-15T18:33 UTC. Severity: SEV-2. Alert: storage-svc: 5XX error rate > 2 % for 5 min on storage-svc.

Impact: A share of requests failed with 5xx on storage-svc.

Root cause: Launch-template change moved node-svc to a smaller instance type.

Breaking change: infra/node-svc.tf:127 (aws_launch_template.node_svc). infra/node-svc.tf:127 sets instance_type m5.4xlarge -> m5.xlarge on aws_launch_template.node_svc.

How it was fixed: Restore instance_type = m5.4xlarge on aws_launch_template.node_svc (terraform tf-r11). Mitigated at 2026-06-15T23:21 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `Call to node-svc failed: HTTP 503 Service Unavailable`..

Services affected: node-svc, compute-svc, storage-svc, cloud-api, api-gateway.
