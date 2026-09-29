# Postmortem INC-004: Latency and timeouts on api-gateway

Published: 2026-03-14T04:29:10.981767+00:00

Date: 2026-03-09T14:20 UTC. Severity: SEV-2. Alert: api-gateway: p99 latency > 2 s for 5 min on api-gateway.

Impact: Requests slowed down and a share of them timed out on api-gateway.

Root cause: Deploy v3.3.0 of cloud-api dropped index ix_servers_tenant on meta-db.servers (it looked unused on the replica); the hot query fell back to sequential scans and timed out at peak.

Breaking change: cloud-api/db/migrations/V84__cleanup_indexes.sql:2 (migration). cloud-api/db/migrations/V84__cleanup_indexes.sql:2 drops ix_servers_tenant.

How it was fixed: Recreate ix_servers_tenant CONCURRENTLY on meta-db; roll back cloud-api if needed; check index usage on the primary before dropping. Mitigated at 2026-03-09T18:40 UTC.

Action items: Add a regression test covering migration (cloud-api/db/migrations/V84__cleanup_indexes.sql).; Alert on the error signature `GET <*> <*> <*> target=cloud-api`..

Services affected: meta-db, cloud-api, api-gateway.
