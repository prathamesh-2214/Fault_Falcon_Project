# Postmortem INC-048: Elevated 5xx errors on api-gateway

Published: 2026-05-24T14:26:10.835226+00:00

Date: 2026-05-20T14:06 UTC. Severity: SEV-3. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: A share of requests failed with 5xx on api-gateway.

Root cause: Deploy v2.17.0 of compute-svc removed a null/nil guard; requests for records without the optional field now crash the handler.

Breaking change: compute/rpc/RMClient.java:71 (RMClient.allocate). compute/rpc/RMClient.java:71 (RMClient.allocate) drops the null check before dereferencing the field.

How it was fixed: Roll back compute-svc v2.17.0 -> v2.16.1; restore the guard at compute/rpc/RMClient.java:71; add a test with the optional field missing. Mitigated at 2026-05-20T18:56 UTC.

Action items: Add a regression test covering RMClient.allocate (compute/rpc/RMClient.java).; Alert on the error signature `GET <*> <*> <*> target=cloud-api`..

Services affected: compute-svc, cloud-api, api-gateway.
