# Postmortem INC-044: Latency and timeouts on node-svc

Published: 2026-05-16T06:16:54.831295+00:00

Date: 2026-05-13T19:30 UTC. Severity: SEV-1. Alert: node-svc: p99 latency > 2 s for 5 min on node-svc.

Impact: Requests slowed down and a share of them timed out on node-svc.

Root cause: Config change to retry.max in node-svc broke calls under load.

Breaking change: config/node-svc.yaml:22 (retry.max). config/node-svc.yaml:22 sets retry.max 3 -> 15.

How it was fixed: Revert retry.max to 3 in config/node-svc.yaml. Mitigated at 2026-05-14T00:11 UTC.

Action items: Add validation bounds and a canary for risky config keys (pool / timeout / retry / flags).; Alert on the error signature `outbound call failed after <*> attempts`..

Services affected: node-svc, compute-svc, storage-svc, cloud-api, api-gateway.
