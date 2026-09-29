# Postmortem INC-109: Elevated 5xx errors on cloud-api

Published: 2026-08-30T08:03:34.262002+00:00

Date: 2026-08-28T01:26 UTC. Severity: SEV-3. Alert: cloud-api: 5XX error rate > 2 % for 5 min on cloud-api.

Impact: A share of requests failed with 5xx on cloud-api.

Root cause: Release train RT-26.35 shipped FEAT-115 (Job-level audit trail) across services; the storage-svc part (v4.22.0) broke, and the failure surfaced in cloud-api through the dependency chain. The later parts of the same feature were harmless.

Breaking change: storage/datanode/BlockWriter.java:70 (BlockWriter.write). storage/datanode/BlockWriter.java:70 (BlockWriter.write) flushes the EBS volume on every record instead of every FLUSH_BATCH.

How it was fixed: Roll back storage-svc v4.22.0 -> v4.21.0 (the other services' parts of FEAT-115 can stay); fix storage/datanode/BlockWriter.java:70; add a cross-service load test to the train. Mitigated at 2026-08-28T04:28 UTC.

Action items: Add a regression test covering BlockWriter.write (storage/datanode/BlockWriter.java).; Alert on the error signature `Unknown base file: /var/lib/nova/instances/_base/<HEX>`..

Services affected: storage-svc, compute-svc, cloud-api, api-gateway.
