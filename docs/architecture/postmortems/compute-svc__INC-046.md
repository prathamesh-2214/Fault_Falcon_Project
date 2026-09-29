# Postmortem INC-046: Elevated 5xx errors on compute-svc

Published: 2026-05-20T05:06:15.138015+00:00

Date: 2026-05-17T09:19 UTC. Severity: SEV-1. Alert: compute-svc: 5XX error rate > 2 % for 5 min on compute-svc.

Impact: A share of requests failed with 5xx on compute-svc.

Root cause: Deploy v2.14.0 of compute-svc introduced a regression.

Breaking change: compute/dfs/LeaseRenewer.java:195 (LeaseRenewer.renew). compute/dfs/LeaseRenewer.java:195 (LeaseRenewer.renew) changes the condition / constant shown in the diff.

How it was fixed: Complete the rollback of compute-svc to v2.13.0 on all nodes; revert compute/dfs/LeaseRenewer.java:195. Mitigated at 2026-05-17T13:01 UTC.

Action items: Add a regression test covering LeaseRenewer.renew (compute/dfs/LeaseRenewer.java).; Alert on the error signature `unhandled error in request handler: <*> failures in 60 s`..

Services affected: compute-svc, cloud-api, api-gateway.
