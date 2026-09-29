# Codebase: meta-db

Published: 2026-03-02T00:00:00+00:00

Owner: team-platform. Runs on: Amazon RDS for PostgreSQL 15, Multi-AZ. Calls: none. Host: its own instances. Called by: auth-svc, cloud-api, compute-svc.

- `infra/meta-db.tf`: AWS resources (Terraform). Connections: -. Reads config: -. Calls: -.

Managed resource: no application code. Terraform keys: rds_instance_class, rds_max_connections, rds_engine_version, rds_apply_immediately, rds_storage_gb.
