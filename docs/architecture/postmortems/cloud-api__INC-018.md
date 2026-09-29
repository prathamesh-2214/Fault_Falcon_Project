# Postmortem INC-018: Latency and timeouts on api-gateway

Published: 2026-04-06T09:39:45.491683+00:00

Date: 2026-04-01T10:13 UTC. Severity: SEV-1. Alert: api-gateway: p99 latency > 2 s for 5 min on api-gateway.

Impact: Requests slowed down and a share of them timed out on api-gateway.

Root cause: Deploy v3.12.0 of cloud-api ran a blocking migration on the meta-db servers table (RDS); writes queued behind the table lock and callers timed out. The hotfix patch applied afterwards did not touch the DB.

Breaking change: cloud-api/db/migrations/V40__servers_feat133.sql:2 (migration). cloud-api/db/migrations/V40__servers_feat133.sql:2 runs DDL that rewrites servers under an ACCESS EXCLUSIVE lock.

How it was fixed: Cancel the migration / roll back cloud-api v3.12.0 -> v3.11.0; re-run it online (CONCURRENTLY / nullable column, batched backfill); review meta-db sizing. Mitigated at 2026-04-01T13:49 UTC.

Action items: Add a regression test covering migration (cloud-api/db/migrations/V40__servers_feat133.sql).; Alert on the error signature `GET <*> <*> <*> target=cloud-api`..

Services affected: meta-db, cloud-api, api-gateway.
