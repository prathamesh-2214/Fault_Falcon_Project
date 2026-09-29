# Postmortem INC-090: Latency and timeouts on storage-svc

Published: 2026-08-01T03:59:28.053462+00:00

Date: 2026-07-28T06:50 UTC. Severity: SEV-3. Alert: storage-svc: p99 latency > 2 s for 5 min on storage-svc.

Impact: Requests slowed down and a share of them timed out on storage-svc.

Root cause: Terraform change to the storage-svc datanode EBS volume cut gp3 throughput and IOPS; the extra replica writes after dfs.replication was raised then saturated the volume.

Breaking change: infra/storage-svc.tf:49 (aws_ebs_volume.storage_svc_data). infra/storage-svc.tf:49 sets ebs_throughput 250 -> 125 on aws_ebs_volume.storage_svc_data.

How it was fixed: Restore throughput/IOPS on aws_ebs_volume.storage_svc_data (terraform tf-r12); alarm on EBS VolumeQueueLength and BurstBalance. Mitigated at 2026-07-28T09:47 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `<*> exception while serving <*> to <*>`..

Services affected: storage-svc, compute-svc, cloud-api, api-gateway.
