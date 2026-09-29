# Postmortem INC-104: Elevated 5xx errors on compute-svc

Published: 2026-08-22T08:27:41.416991+00:00

Date: 2026-08-19T19:31 UTC. Severity: SEV-2. Alert: compute-svc: 5XX error rate > 2 % for 5 min on compute-svc.

Impact: A share of requests failed with 5xx on compute-svc.

Root cause: Deploy v2.33.0 of compute-svc introduced a regression.

Breaking change: compute/rpc/RMClient.java:152 (RMClient.allocate). compute/rpc/RMClient.java:152 (RMClient.allocate) changes the condition / constant shown in the diff.

How it was fixed: Complete the rollback of compute-svc to v2.32.0 on all nodes; revert compute/rpc/RMClient.java:152. Mitigated at 2026-08-19T22:44 UTC.

Action items: Add a regression test covering RMClient.allocate (compute/rpc/RMClient.java).; Alert on the error signature `unhandled error in request handler: <*> failures in 60 s`..

Services affected: compute-svc, cloud-api, api-gateway.
