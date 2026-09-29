# Postmortem INC-014: Connection failures on cloud-api

Published: 2026-03-28T16:15:08.035449+00:00

Date: 2026-03-25T19:23 UTC. Severity: SEV-2. Alert: cloud-api: 5XX error rate > 2 % for 5 min on cloud-api.

Impact: Requests failed while connections were refused on cloud-api.

Root cause: Terraform change upgraded meta-db with apply_immediately = true; the Multi-AZ failover happened at peak and JVM clients kept stale connections and DNS for minutes.

Breaking change: infra/meta-db.tf:44 (aws_db_instance.meta_db). infra/meta-db.tf:44 sets rds_apply_immediately false -> true on aws_db_instance.meta_db.

How it was fixed: Set apply_immediately back to false (maintenance window); cap JVM DNS TTL at 30 s; enable pool validation. Mitigated at 2026-03-25T22:34 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `<*> <*> <*> HTTP/1.1" status: <*> len: <*> time: <*>`..

Services affected: meta-db, auth-svc, cloud-api, compute-svc, api-gateway.
