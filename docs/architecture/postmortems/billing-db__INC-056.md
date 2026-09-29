# Postmortem INC-056: Connection failures on billing-svc

Published: 2026-06-06T17:26:28.739422+00:00

Date: 2026-06-02T18:35 UTC. Severity: SEV-2. Alert: billing-svc: 5XX error rate > 2 % for 5 min on billing-svc.

Impact: Requests failed while connections were refused on billing-svc.

Root cause: Terraform change upgraded billing-db with apply_immediately = true; the Multi-AZ failover happened at peak and JVM clients kept stale connections and DNS for minutes.

Breaking change: infra/billing-db.tf:15 (aws_db_instance.billing_db). infra/billing-db.tf:15 sets rds_apply_immediately false -> true on aws_db_instance.billing_db.

How it was fixed: Set apply_immediately back to false (maintenance window); cap JVM DNS TTL at 30 s; enable pool validation. Mitigated at 2026-06-02T21:37 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `c.f.b.repo.InvoiceRepository - call to billing-db failed: <*> <*> <*> <*> <*> <*`..

Services affected: billing-db, billing-svc, api-gateway.
