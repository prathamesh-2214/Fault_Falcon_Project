# Postmortem INC-035: Elevated 5xx errors on api-gateway

Published: 2026-05-03T20:18:40.897454+00:00

Date: 2026-04-29T07:10 UTC. Severity: SEV-2. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: A share of requests failed with 5xx on api-gateway.

Root cause: Terraform change to the session-cache security group opened 6380 instead of 6379; auth-svc could not reach Redis and every token check failed.

Breaking change: infra/session-cache.tf:46 (aws_security_group.session_cache). infra/session-cache.tf:46 sets redis_sg_port 6379 -> 6380 on aws_security_group.session_cache.

How it was fixed: Restore the ingress rule for port 6379 on aws_security_group.session_cache; add a connectivity check to the pipeline. Mitigated at 2026-04-29T10:35 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `POST <*> <*> <*> target=auth-svc`..

Services affected: session-cache, auth-svc, api-gateway.
