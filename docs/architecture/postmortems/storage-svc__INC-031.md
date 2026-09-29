# Postmortem INC-031: Latency and timeouts on compute-svc

Published: 2026-04-26T23:08:37.178427+00:00

Date: 2026-04-22T13:45 UTC. Severity: SEV-2. Alert: compute-svc: p99 latency > 2 s for 5 min on compute-svc.

Impact: Requests slowed down and a share of them timed out on compute-svc.

Root cause: Terraform change to the storage-svc datanode EBS volume cut gp3 throughput and IOPS; the extra replica writes after dfs.replication was raised then saturated the volume.

Breaking change: infra/storage-svc.tf:49 (aws_ebs_volume.storage_svc_data). infra/storage-svc.tf:49 sets ebs_throughput 250 -> 125 on aws_ebs_volume.storage_svc_data.

How it was fixed: Restore throughput/IOPS on aws_ebs_volume.storage_svc_data (terraform tf-r2); alarm on EBS VolumeQueueLength and BurstBalance. Mitigated at 2026-04-22T18:26 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `Call to <*> failed: java.net.SocketTimeoutException: Read timed out`..

Services affected: storage-svc, compute-svc, cloud-api, api-gateway.
