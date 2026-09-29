# Postmortem INC-053: Elevated 5xx errors on billing-svc

Published: 2026-06-01T04:43:49.778858+00:00

Date: 2026-05-28T12:43 UTC. Severity: SEV-3. Alert: billing-svc: 5XX error rate > 2 % for 5 min on billing-svc.

Impact: A share of requests failed with 5xx on billing-svc.

Root cause: Config change raised consumer.concurrency 8 -> 64 to drain the usage backlog faster; every consumer thread held a billing-db connection and the database refused new ones.

Breaking change: config/billing-svc.yaml:100 (consumer.concurrency). config/billing-svc.yaml:100 sets consumer.concurrency 8 -> 64.

How it was fixed: Revert consumer.concurrency to 8; size consumer concurrency against db.pool.max_size and RDS max_connections. Mitigated at 2026-05-28T17:31 UTC.

Action items: Add validation bounds and a canary for risky config keys (pool / timeout / retry / flags).; Alert on the error signature `c.f.b.consumer.UsageEventConsumer - HikariPool-1 - Connection is not available, `..

Services affected: billing-svc, api-gateway, billing-db.
