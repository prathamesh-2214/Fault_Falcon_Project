# Postmortem INC-003: Connection failures on billing-svc

Published: 2026-03-13T04:54:18.844735+00:00

Date: 2026-03-08T05:14 UTC. Severity: SEV-2. Alert: billing-svc: 5XX error rate > 2 % for 5 min on billing-svc.

Impact: Requests failed while connections were refused on billing-svc.

Root cause: Terraform change to the billing-db parameter group cut max_connections 500 -> 100; after the next reboot the services' pools exceeded it and new connections were refused.

Breaking change: infra/billing-db.tf:102 (aws_db_parameter_group.billing_db). infra/billing-db.tf:102 sets max_connections 500 -> 100 on aws_db_parameter_group.billing_db.

How it was fixed: Restore max_connections = 500 on aws_db_parameter_group.billing_db and reboot in the maintenance window. Mitigated at 2026-03-08T09:33 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `c.f.b.repo.InvoiceRepository - call to billing-db failed: <*> <*> <*> <*> <*> <*`..

Services affected: billing-db, billing-svc, api-gateway.
