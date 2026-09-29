# Postmortem INC-081: Latency and timeouts on storage-svc

Published: 2026-07-16T06:02:15.735377+00:00

Date: 2026-07-13T11:46 UTC. Severity: SEV-1. Alert: storage-svc: p99 latency > 2 s for 5 min on storage-svc.

Impact: Requests slowed down and a share of them timed out on storage-svc.

Root cause: Config change to retry.max in storage-svc broke calls under load.

Breaking change: config/storage-svc.yaml:105 (retry.max). config/storage-svc.yaml:105 sets retry.max 3 -> 15.

How it was fixed: Revert retry.max to 3 in config/storage-svc.yaml. Mitigated at 2026-07-13T16:34 UTC.

Action items: Add validation bounds and a canary for risky config keys (pool / timeout / retry / flags).; Alert on the error signature `outbound call failed after <*> attempts`..

Services affected: storage-svc, compute-svc, cloud-api, api-gateway.
