# Postmortem INC-050: Latency and timeouts on api-gateway

Published: 2026-05-27T17:35:26.931598+00:00

Date: 2026-05-23T14:46 UTC. Severity: SEV-1. Alert: api-gateway: p99 latency > 2 s for 5 min on api-gateway.

Impact: Requests slowed down and a share of them timed out on api-gateway.

Root cause: Terraform change set the ALB idle_timeout 60 -> 10 s; long-running requests (report exports) were cut with 504 and clients retried.

Breaking change: infra/api-gateway.tf:32 (aws_lb.api_gateway). infra/api-gateway.tf:32 sets idle_timeout 60 -> 10 on aws_lb.api_gateway.

How it was fixed: Restore idle_timeout = 60 on aws_lb.api_gateway. Mitigated at 2026-05-23T18:20 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `GET /v1/servers <*> <*> <*>`..

Services affected: api-gateway.
