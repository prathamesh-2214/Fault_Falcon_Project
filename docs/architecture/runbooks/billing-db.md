# Runbook: billing-db

Published: 2026-03-02T00:00:00+00:00

Billing DB: usage records, invoices. Runs on Amazon RDS for PostgreSQL 15; owned by team-billing.

## Infrastructure (infra/billing-db.tf)

- rds_instance_class: db.r5.large
- rds_max_connections: 500
- rds_engine_version: 15.23
- rds_apply_immediately: false
- rds_storage_gb: 1100
- secret_rotation_days: 0

## What to check on errors

Same checks as meta-db; also Secrets Manager rotation of the billing_app credentials.

Dependencies: none. Host: none. Called by: billing-svc.

## Normal behaviour (from logs)

- error ratio band: 0.00 - 0.17 (median 0.00)
