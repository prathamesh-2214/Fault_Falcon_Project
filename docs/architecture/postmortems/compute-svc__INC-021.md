# Postmortem INC-021: Elevated 5xx errors on compute-svc

Published: 2026-04-11T18:33:47.287002+00:00

Date: 2026-04-06T15:03 UTC. Severity: SEV-2. Alert: compute-svc: 5XX error rate > 2 % for 5 min on compute-svc.

Impact: A share of requests failed with 5xx on compute-svc.

Root cause: Launch-template change moved compute-svc to a smaller instance type.

Breaking change: infra/compute-svc.tf:10 (aws_launch_template.compute_svc). infra/compute-svc.tf:10 sets instance_type m5.2xlarge -> m5.large on aws_launch_template.compute_svc.

How it was fixed: Restore instance_type = m5.2xlarge on aws_launch_template.compute_svc (terraform tf-r1). Mitigated at 2026-04-06T17:36 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `out of memory: killed process <*>`..

Services affected: compute-svc, cloud-api, api-gateway.
