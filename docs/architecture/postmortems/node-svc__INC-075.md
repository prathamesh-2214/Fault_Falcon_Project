# Postmortem INC-075: Latency and timeouts on cloud-api

Published: 2026-07-07T15:09:00.727450+00:00

Date: 2026-07-03T10:50 UTC. Severity: SEV-1. Alert: cloud-api: p99 latency > 2 s for 5 min on cloud-api.

Impact: Requests slowed down and a share of them timed out on cloud-api.

Root cause: Terraform change cut the node-svc Auto Scaling group max_size 12 -> 6; at the next batch peak the fleet could not scale out and jobs queued.

Breaking change: infra/node-svc.tf:113 (aws_autoscaling_group.node_svc). infra/node-svc.tf:113 sets max_size 12 -> 6 on aws_autoscaling_group.node_svc.

How it was fixed: Restore max_size = 12 on aws_autoscaling_group.node_svc. Mitigated at 2026-07-03T15:23 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `<*> <*> <*> HTTP/1.1" status: <*> len: <*> time: <*>`..

Services affected: node-svc, compute-svc, storage-svc, cloud-api, api-gateway.
