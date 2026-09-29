# Postmortem INC-098: Authentication failures on auth-svc

Published: 2026-08-14T19:18:23.098617+00:00

Date: 2026-08-10T08:18 UTC. Severity: SEV-1. Alert: auth-svc: authentication-failure rate > 5 % for 5 min on auth-svc.

Impact: Users were logged out or rejected on auth-svc.

Root cause: Terraform change moved session-cache to cache.t4g.medium; memory ran out, Redis evicted sessions and users were logged out in waves.

Breaking change: infra/session-cache.tf:69 (aws_elasticache_replication_group.session_cache). infra/session-cache.tf:69 sets redis_node_type cache.r6g.xlarge -> cache.t4g.medium on aws_elasticache_replication_group.session_cache.

How it was fixed: Restore redis_node_type = cache.r6g.xlarge on aws_elasticache_replication_group.session_cache; alarm on Evictions and DatabaseMemoryUsagePercentage. Mitigated at 2026-08-10T12:43 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `{"level":"warn","msg":"session not found, forcing <*>`..

Services affected: session-cache, auth-svc, api-gateway.
