# Postmortem INC-069: Connection failures on cloud-api

Published: 2026-06-26T09:37:49.737383+00:00

Date: 2026-06-23T18:16 UTC. Severity: SEV-2. Alert: cloud-api: 5XX error rate > 2 % for 5 min on cloud-api.

Impact: Requests failed while connections were refused on cloud-api.

Root cause: Terraform change to aws_security_group for coord-svc closed the leader-election port, so quorum peers could not open election channels.

Breaking change: infra/coord-svc.tf:13 (aws_security_group.coord_svc). infra/coord-svc.tf:13 changes the election ingress rule sg_election_port 3888 -> 2888.

How it was fixed: Restore the ingress rule for port 3888 on aws_security_group.coord_svc (terraform apply of tf-r6); add a connectivity check for 2181/2888/3888 to the pipeline. Mitigated at 2026-06-23T21:36 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `<*> <*> <*> HTTP/1.1" status: <*> len: <*> time: <*>`..

Services affected: coord-svc, compute-svc, cloud-api, api-gateway.
