# Postmortem INC-096: Connection failures on api-gateway

Published: 2026-08-11T03:51:56.135353+00:00

Date: 2026-08-06T17:29 UTC. Severity: SEV-1. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: Requests failed while connections were refused on api-gateway.

Root cause: Terraform change to aws_security_group for coord-svc closed the leader-election port, so quorum peers could not open election channels.

Breaking change: infra/coord-svc.tf:13 (aws_security_group.coord_svc). infra/coord-svc.tf:13 changes the election ingress rule sg_election_port 3888 -> 2888.

How it was fixed: Restore the ingress rule for port 3888 on aws_security_group.coord_svc (terraform apply of tf-r9); add a connectivity check for 2181/2888/3888 to the pipeline. Mitigated at 2026-08-06T20:45 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `GET <*> <*> <*> target=cloud-api`..

Services affected: coord-svc, compute-svc, cloud-api, api-gateway.
