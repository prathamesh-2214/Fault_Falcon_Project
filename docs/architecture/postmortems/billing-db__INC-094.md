# Postmortem INC-094: Latency and timeouts on api-gateway

Published: 2026-08-08T15:26:12.519413+00:00

Date: 2026-08-03T12:44 UTC. Severity: SEV-1. Alert: api-gateway: p99 latency > 2 s for 5 min on api-gateway.

Impact: Requests slowed down and a share of them timed out on api-gateway.

Root cause: Terraform change downsized aws_db_instance.billing_db db.r5.large -> db.t3.medium; at the next traffic peak CPU hit 100 % and queries timed out.

Breaking change: infra/billing-db.tf:106 (aws_db_instance.billing_db). infra/billing-db.tf:106 sets rds_instance_class db.r5.large -> db.t3.medium on aws_db_instance.billing_db.

How it was fixed: Restore rds_instance_class = db.r5.large on aws_db_instance.billing_db; alarm on CPU and DBLoad before downsizing. Mitigated at 2026-08-03T16:42 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `GET <*> <*> <*> target=billing-svc`..

Services affected: billing-db, billing-svc, api-gateway.
