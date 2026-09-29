# Postmortem INC-060: Latency and timeouts on coord-svc

Published: 2026-06-14T03:12:35.645768+00:00

Date: 2026-06-09T05:33 UTC. Severity: SEV-1. Alert: coord-svc: p99 latency > 2 s for 5 min on coord-svc.

Impact: Requests slowed down and a share of them timed out on coord-svc.

Root cause: Hotfix patch v1.10.1 of coord-svc fixed a race by adding a global lock; every request then waited on that lock and latency exploded under load.

Breaking change: coord/server/NIOServerCnxn.java:91 (NIOServerCnxn.doIO). coord/server/NIOServerCnxn.java:91 (NIOServerCnxn.doIO) wraps the whole request in one global lock.

How it was fixed: Roll back coord-svc v1.10.1 -> v1.10.0; fix the race with a per-key lock at coord/server/NIOServerCnxn.java:91; load-test hotfixes before prod. Mitigated at 2026-06-09T08:31 UTC.

Action items: Add a regression test covering NIOServerCnxn.doIO (coord/server/NIOServerCnxn.java).; Alert on the error signature `request timed out after 30000 ms (lock contention)`..

Services affected: coord-svc, compute-svc, cloud-api, api-gateway.
