# Postmortem INC-012: Elevated 5xx errors on api-gateway

Published: 2026-03-27T03:30:09.930324+00:00

Date: 2026-03-22T14:31 UTC. Severity: SEV-1. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: A share of requests failed with 5xx on api-gateway.

Root cause: Config change raised consumer.concurrency 8 -> 64 to drain the usage backlog faster; every consumer thread held a billing-db connection and the database refused new ones.

Breaking change: config/billing-svc.yaml:100 (consumer.concurrency). config/billing-svc.yaml:100 sets consumer.concurrency 8 -> 64.

How it was fixed: Revert consumer.concurrency to 8; size consumer concurrency against db.pool.max_size and RDS max_connections. Mitigated at 2026-03-22T18:26 UTC.

Action items: Add validation bounds and a canary for risky config keys (pool / timeout / retry / flags).; Alert on the error signature `GET <*> <*> <*> target=billing-svc`..

Services affected: billing-svc, api-gateway, billing-db.
