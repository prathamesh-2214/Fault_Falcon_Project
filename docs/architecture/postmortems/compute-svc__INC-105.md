# Postmortem INC-105: Latency and timeouts on compute-svc

Published: 2026-08-24T00:42:26.390720+00:00

Date: 2026-08-21T07:58 UTC. Severity: SEV-3. Alert: compute-svc: p99 latency > 2 s for 5 min on compute-svc.

Impact: Requests slowed down and a share of them timed out on compute-svc.

Root cause: Terraform change cut the compute-svc Auto Scaling group max_size 12 -> 6; at the next batch peak the fleet could not scale out and jobs queued.

Breaking change: infra/compute-svc.tf:85 (aws_autoscaling_group.compute_svc). infra/compute-svc.tf:85 sets max_size 12 -> 6 on aws_autoscaling_group.compute_svc.

How it was fixed: Restore max_size = 12 on aws_autoscaling_group.compute_svc. Mitigated at 2026-08-21T12:30 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `Container request pending for <*> s: insufficient cluster capacity`..

Services affected: compute-svc, cloud-api, api-gateway.
