# Postmortem INC-103: Authentication failures on billing-svc

Published: 2026-08-20T13:17:08.934676+00:00

Date: 2026-08-18T07:43 UTC. Severity: SEV-3. Alert: billing-svc: authentication-failure rate > 5 % for 5 min on billing-svc.

Impact: Users were logged out or rejected on billing-svc.

Root cause: Terraform change enabled automatic rotation of the billing-db credentials; rotation ran at once and billing-svc kept using its cached password (db.credentials_cache_s), so logins failed.

Breaking change: infra/billing-db.tf:21 (aws_secretsmanager_secret_rotation.billing_db). infra/billing-db.tf:21 sets secret_rotation_days 0 -> 30 on aws_secretsmanager_secret_rotation.billing_db.

How it was fixed: Restart billing-svc tasks to pick up the new secret; lower db.credentials_cache_s and refresh on authentication failure before re-enabling rotation. Mitigated at 2026-08-18T11:20 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `c.f.b.repo.InvoiceRepository - org.postgresql.util.PSQLException: FATAL: passwor`..

Services affected: billing-db, billing-svc, api-gateway.
