# Postmortem INC-102: Connection failures on cloud-api

Published: 2026-08-19T14:47:17.674227+00:00

Date: 2026-08-16T16:10 UTC. Severity: SEV-1. Alert: cloud-api: 5XX error rate > 2 % for 5 min on cloud-api.

Impact: Requests failed while connections were refused on cloud-api.

Root cause: Terraform change to the meta-db parameter group cut max_connections 500 -> 100; after the next reboot the services' pools exceeded it and new connections were refused.

Breaking change: infra/meta-db.tf:70 (aws_db_parameter_group.meta_db). infra/meta-db.tf:70 sets max_connections 500 -> 100 on aws_db_parameter_group.meta_db.

How it was fixed: Restore max_connections = 500 on aws_db_parameter_group.meta_db and reboot in the maintenance window. Mitigated at 2026-08-16T20:25 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `<*> <*> <*> HTTP/1.1" status: <*> len: <*> time: <*>`..

Services affected: meta-db, auth-svc, cloud-api, compute-svc, api-gateway.
