# Postmortem INC-076: Latency and timeouts on api-gateway

Published: 2026-07-07T13:31:07.844379+00:00

Date: 2026-07-05T06:21 UTC. Severity: SEV-3. Alert: api-gateway: p99 latency > 2 s for 5 min on api-gateway.

Impact: Requests slowed down and a share of them timed out on api-gateway.

Root cause: Deploy v2.25.0 of compute-svc ran a blocking migration on the meta-db jobs table (RDS); writes queued behind the table lock and callers timed out. The hotfix patch applied afterwards did not touch the DB.

Breaking change: compute/db/migrations/V49__jobs_feat104.sql:2 (migration). compute/db/migrations/V49__jobs_feat104.sql:2 runs DDL that rewrites jobs under an ACCESS EXCLUSIVE lock.

How it was fixed: Cancel the migration / roll back compute-svc v2.25.0 -> v2.23.0; re-run it online (CONCURRENTLY / nullable column, batched backfill); review meta-db sizing. Mitigated at 2026-07-05T11:20 UTC.

Action items: Add a regression test covering migration (compute/db/migrations/V49__jobs_feat104.sql).; Alert on the error signature `GET <*> <*> <*> target=cloud-api`..

Services affected: meta-db, compute-svc, cloud-api, api-gateway.
