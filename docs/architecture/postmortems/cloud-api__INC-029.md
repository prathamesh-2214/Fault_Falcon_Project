# Postmortem INC-029: Elevated 5xx errors on api-gateway

Published: 2026-04-21T21:41:46.560779+00:00

Date: 2026-04-19T07:53 UTC. Severity: SEV-2. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: A share of requests failed with 5xx on api-gateway.

Root cause: Deploy v3.17.0 of cloud-api introduced a regression.

Breaking change: cloud-api/nova/compute/client.py:26 (ComputeClient.call). cloud-api/nova/compute/client.py:26 (ComputeClient.call) changes the condition / constant shown in the diff.

How it was fixed: Roll back cloud-api v3.17.0 -> v3.16.0; revert cloud-api/nova/compute/client.py:26. Mitigated at 2026-04-19T11:14 UTC.

Action items: Add a regression test covering ComputeClient.call (cloud-api/nova/compute/client.py).; Alert on the error signature `GET <*> <*> <*> target=cloud-api`..

Services affected: cloud-api, api-gateway.
