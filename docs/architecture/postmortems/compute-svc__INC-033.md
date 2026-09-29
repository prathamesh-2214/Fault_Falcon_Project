# Postmortem INC-033: Latency and timeouts on compute-svc

Published: 2026-04-30T11:43:43.936106+00:00

Date: 2026-04-25T19:53 UTC. Severity: SEV-3. Alert: compute-svc: p99 latency > 2 s for 5 min on compute-svc.

Impact: Requests slowed down and a share of them timed out on compute-svc.

Root cause: Deploy v2.10.0 of compute-svc removed exponential backoff from ResourceManager calls; combined with retry.max raised earlier it created a retry storm.

Breaking change: compute/rpc/RMClient.java:308 (RMClient.allocate). compute/rpc/RMClient.java:308 (RMClient.allocate) replaces exponential backoff with a constant BASE_BACKOFF_MS.

How it was fixed: Roll back compute-svc v2.10.0 -> v2.9.0; restore `backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS)` at compute/rpc/RMClient.java:308; revert retry.max to its previous value. Mitigated at 2026-04-25T23:46 UTC.

Action items: Add a regression test covering RMClient.allocate (compute/rpc/RMClient.java).; Alert on the error signature `Retrying connect to server: resourcemanager:8030. Already tried <*> time(s)`..

Services affected: compute-svc, cloud-api, api-gateway, storage-svc, coord-svc.
