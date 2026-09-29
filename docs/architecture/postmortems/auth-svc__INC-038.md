# Postmortem INC-038: Elevated 5xx errors on auth-svc

Published: 2026-05-08T00:36:31.567564+00:00

Date: 2026-05-04T08:25 UTC. Severity: SEV-2. Alert: auth-svc: 5XX error rate > 2 % for 5 min on auth-svc.

Impact: A share of requests failed with 5xx on auth-svc.

Root cause: Terraform change halved auth-svc task memory 2048 -> 1024 MB; tasks were OOM-killed under load and the service restarted in a loop.

Breaking change: infra/auth-svc.tf:96 (aws_ecs_task_definition.auth_svc). infra/auth-svc.tf:96 sets memory 2048 -> 1024 on aws_ecs_task_definition.auth_svc.

How it was fixed: Restore memory = 2048 on aws_ecs_task_definition.auth_svc; alarm on MemoryUtilization. Mitigated at 2026-05-04T12:45 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `ECS service started 1 task (replacing a stopped task)`..

Services affected: auth-svc, api-gateway.
