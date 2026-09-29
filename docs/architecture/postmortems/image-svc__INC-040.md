# Postmortem INC-040: Elevated 5xx errors on cloud-api

Published: 2026-05-11T05:27:07.849345+00:00

Date: 2026-05-07T10:41 UTC. Severity: SEV-2. Alert: cloud-api: 5XX error rate > 2 % for 5 min on cloud-api.

Impact: A share of requests failed with 5xx on cloud-api.

Root cause: Terraform change halved image-svc task memory 2048 -> 1024 MB; tasks were OOM-killed under load and the service restarted in a loop.

Breaking change: infra/image-svc.tf:21 (aws_ecs_task_definition.image_svc). infra/image-svc.tf:21 sets memory 2048 -> 1024 on aws_ecs_task_definition.image_svc.

How it was fixed: Restore memory = 2048 on aws_ecs_task_definition.image_svc; alarm on MemoryUtilization. Mitigated at 2026-05-07T15:33 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `<*> <*> <*> HTTP/1.1" status: <*> len: <*> time: <*>`..

Services affected: image-svc, cloud-api, api-gateway.
