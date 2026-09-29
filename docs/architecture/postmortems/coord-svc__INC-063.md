# Postmortem INC-063: Latency and timeouts on coord-svc

Published: 2026-06-16T10:02:04.612500+00:00

Date: 2026-06-13T19:20 UTC. Severity: SEV-1. Alert: coord-svc: p99 latency > 2 s for 5 min on coord-svc.

Impact: Requests slowed down and a share of them timed out on coord-svc.

Root cause: Hotfix patch v1.13.1 of coord-svc fixed a race by adding a global lock; every request then waited on that lock and latency exploded under load.

Breaking change: coord/quorum/FastLeaderElection.java:73 (FastLeaderElection.lookForLeader). coord/quorum/FastLeaderElection.java:73 (FastLeaderElection.lookForLeader) wraps the whole request in one global lock.

How it was fixed: Roll back coord-svc v1.13.1 -> v1.13.0; fix the race with a per-key lock at coord/quorum/FastLeaderElection.java:73; load-test hotfixes before prod. Mitigated at 2026-06-13T22:02 UTC.

Action items: Add a regression test covering FastLeaderElection.lookForLeader (coord/quorum/FastLeaderElection.java).; Alert on the error signature `request waited <*> ms for the global lock`..

Services affected: coord-svc, compute-svc, cloud-api, api-gateway.
