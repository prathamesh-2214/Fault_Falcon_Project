# Postmortem INC-026: Elevated 5xx errors on api-gateway

Published: 2026-04-17T13:50:55.506795+00:00

Date: 2026-04-14T16:35 UTC. Severity: SEV-2. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: A share of requests failed with 5xx on api-gateway.

Root cause: Terraform change set maxmemory-policy volatile-lru -> noeviction; when memory filled, Redis rejected writes (OOM command not allowed) and new sessions could not be created.

Breaking change: infra/session-cache.tf:113 (aws_elasticache_parameter_group.session_cache). infra/session-cache.tf:113 sets maxmemory-policy volatile-lru -> noeviction on aws_elasticache_parameter_group.session_cache.

How it was fixed: Restore maxmemory-policy = volatile-lru on aws_elasticache_parameter_group.session_cache. Mitigated at 2026-04-14T19:12 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `POST <*> <*> <*> target=auth-svc`..

Services affected: session-cache, auth-svc, api-gateway.
