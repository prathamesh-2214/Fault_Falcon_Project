# Postmortem INC-083: Latency and timeouts on storage-svc

Published: 2026-07-19T15:27:39.704414+00:00

Date: 2026-07-16T18:41 UTC. Severity: SEV-2. Alert: storage-svc: p99 latency > 2 s for 5 min on storage-svc.

Impact: Requests slowed down and a share of them timed out on storage-svc.

Root cause: Terraform change to the storage-svc datanode EBS volume cut gp3 throughput and IOPS; the extra replica writes after dfs.replication was raised then saturated the volume.

Breaking change: infra/storage-svc.tf:49 (aws_ebs_volume.storage_svc_data). infra/storage-svc.tf:49 sets ebs_throughput 250 -> 125 on aws_ebs_volume.storage_svc_data.

How it was fixed: Restore throughput/IOPS on aws_ebs_volume.storage_svc_data (terraform tf-r10); alarm on EBS VolumeQueueLength and BurstBalance. Mitigated at 2026-07-16T22:22 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `<*> exception while serving <*> to <*>`..

Services affected: storage-svc, compute-svc, cloud-api, api-gateway.
