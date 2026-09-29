# Postmortem INC-009: Connection failures on auth-svc

Published: 2026-03-22T21:30:23.357265+00:00

Date: 2026-03-17T19:48 UTC. Severity: SEV-2. Alert: auth-svc: 5XX error rate > 2 % for 5 min on auth-svc.

Impact: Requests failed while connections were refused on auth-svc.

Root cause: Terraform change cut the auth-svc target-group health-check timeout 5 -> 2 s; targets under load flapped unhealthy and the ALB returned 502.

Breaking change: infra/auth-svc.tf:69 (aws_lb_target_group.auth_svc). infra/auth-svc.tf:69 sets health_check timeout 5 -> 2 on aws_lb_target_group.auth_svc.

How it was fixed: Restore the health-check timeout = 5 on aws_lb_target_group.auth_svc. Mitigated at 2026-03-18T00:21 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `received SIGTERM, draining connections (target deregistered)`..

Services affected: auth-svc, api-gateway.
