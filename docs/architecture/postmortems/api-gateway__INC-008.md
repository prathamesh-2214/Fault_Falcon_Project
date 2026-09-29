# Postmortem INC-008: Connection failures on api-gateway

Published: 2026-03-21T11:19:44.822192+00:00

Date: 2026-03-16T08:38 UTC. Severity: SEV-1. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: Requests failed while connections were refused on api-gateway.

Root cause: Terraform change cut the api-gateway target-group health-check timeout 5 -> 2 s; targets under load flapped unhealthy and the ALB returned 502.

Breaking change: infra/api-gateway.tf:101 (aws_lb_target_group.api_gateway). infra/api-gateway.tf:101 sets health_check timeout 5 -> 2 on aws_lb_target_group.api_gateway.

How it was fixed: Restore the health-check timeout = 5 on aws_lb_target_group.api_gateway. Mitigated at 2026-03-16T11:57 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `received SIGTERM, draining connections (target deregistered)`..

Services affected: api-gateway.
