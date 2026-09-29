# Codebase: billing-db

Published: 2026-03-02T00:00:00+00:00

Owner: team-billing. Runs on: Amazon RDS for PostgreSQL 15. Calls: none. Host: its own instances. Called by: billing-svc.

- `infra/billing-db.tf`: AWS resources (Terraform). Connections: -. Reads config: -. Calls: -.

Managed resource: no application code. Terraform keys: rds_instance_class, rds_max_connections, rds_engine_version, rds_apply_immediately, rds_storage_gb, secret_rotation_days.
