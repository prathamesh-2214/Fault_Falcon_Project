# Postmortem INC-108: Latency and timeouts on api-gateway

Published: 2026-08-29T14:49:37.034966+00:00

Date: 2026-08-26T08:35 UTC. Severity: SEV-1. Alert: api-gateway: p99 latency > 2 s for 5 min on api-gateway.

Impact: Requests slowed down and a share of them timed out on api-gateway.

Root cause: Deploy v2.37.0 of compute-svc removed exponential backoff from ResourceManager calls; combined with retry.max raised earlier it created a retry storm.

Breaking change: compute/rpc/RMClient.java:62 (RMClient.allocate). compute/rpc/RMClient.java:62 (RMClient.allocate) replaces exponential backoff with a constant BASE_BACKOFF_MS.

How it was fixed: Roll back compute-svc v2.37.0 -> v2.36.0; restore `backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS)` at compute/rpc/RMClient.java:62; revert retry.max to its previous value. Mitigated at 2026-08-26T12:08 UTC.

Action items: Add a regression test covering RMClient.allocate (compute/rpc/RMClient.java).; Alert on the error signature `GET <*> <*> <*> target=cloud-api`..

Services affected: compute-svc, cloud-api, api-gateway, storage-svc, coord-svc.
