# Postmortem INC-049: Connection failures on compute-svc

Published: 2026-05-26T08:03:16.062547+00:00

Date: 2026-05-22T02:05 UTC. Severity: SEV-1. Alert: compute-svc: 5XX error rate > 2 % for 5 min on compute-svc.

Impact: Requests failed while connections were refused on compute-svc.

Root cause: Terraform change to aws_security_group for coord-svc closed the leader-election port, so quorum peers could not open election channels.

Breaking change: infra/coord-svc.tf:13 (aws_security_group.coord_svc). infra/coord-svc.tf:13 changes the election ingress rule sg_election_port 3888 -> 2888.

How it was fixed: Restore the ingress rule for port 3888 on aws_security_group.coord_svc (terraform apply of tf-r3); add a connectivity check for 2181/2888/3888 to the pipeline. Mitigated at 2026-05-22T05:59 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `Call to coord-svc failed: java.net.ConnectException: Connection refused`..

Services affected: coord-svc, compute-svc, cloud-api, api-gateway.
