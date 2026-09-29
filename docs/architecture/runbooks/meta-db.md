# Runbook: meta-db

Published: 2026-03-02T00:00:00+00:00

Shared metadata DB: accounts, servers, jobs. Runs on Amazon RDS for PostgreSQL 15, Multi-AZ; owned by team-platform.

## Infrastructure (infra/meta-db.tf)

- rds_instance_class: db.r5.large
- rds_max_connections: 500
- rds_engine_version: 15.21
- rds_apply_immediately: false
- rds_storage_gb: 900

## What to check on errors

On connection errors: max_connections in the parameter group vs the services' pools, recent maintenance (engine upgrades with apply_immediately). On slow queries: instance class, missing indexes (recent migrations), N+1 query patterns in recent deploys, lock waits during migrations.

Dependencies: none. Host: none. Called by: auth-svc, cloud-api, compute-svc.

## Normal behaviour (from logs)

- error ratio band: 0.00 - 0.17 (median 0.00)
- known noise: LOG: duration: <*> ms statement: SELECT * FROM <*> WHERE <*> = $1
