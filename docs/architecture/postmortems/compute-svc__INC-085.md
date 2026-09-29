# Postmortem INC-085: Latency and timeouts on api-gateway

Published: 2026-07-22T12:54:45.276804+00:00

Date: 2026-07-19T22:41 UTC. Severity: SEV-2. Alert: api-gateway: p99 latency > 2 s for 5 min on api-gateway.

Impact: Requests slowed down and a share of them timed out on api-gateway.

Root cause: Deploy v2.28.0 of compute-svc dropped index ix_jobs_state on meta-db.jobs (it looked unused on the replica); the hot query fell back to sequential scans and timed out at peak.

Breaking change: compute/db/migrations/V90__cleanup_indexes.sql:2 (migration). compute/db/migrations/V90__cleanup_indexes.sql:2 drops ix_jobs_state.

How it was fixed: Recreate ix_jobs_state CONCURRENTLY on meta-db; roll back compute-svc if needed; check index usage on the primary before dropping. Mitigated at 2026-07-20T01:26 UTC.

Action items: Add a regression test covering migration (compute/db/migrations/V90__cleanup_indexes.sql).; Alert on the error signature `GET <*> <*> <*> target=cloud-api`..

Services affected: meta-db, compute-svc, cloud-api, api-gateway.
