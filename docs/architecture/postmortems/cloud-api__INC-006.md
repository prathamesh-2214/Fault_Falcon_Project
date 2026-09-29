# Postmortem INC-006: Latency and timeouts on api-gateway

Published: 2026-03-15T08:26:32.831737+00:00

Date: 2026-03-12T17:57 UTC. Severity: SEV-3. Alert: api-gateway: p99 latency > 2 s for 5 min on api-gateway.

Impact: Requests slowed down and a share of them timed out on api-gateway.

Root cause: Deploy v3.5.0 of cloud-api shrinks its outbound timeout by a factor of 1000, so calls time out almost immediately.

Breaking change: cloud-api/nova/compute/client.py:87 (ComputeClient.call). cloud-api/nova/compute/client.py:87 (ComputeClient.call) converts http.timeout_ms with the wrong unit.

How it was fixed: Roll back cloud-api v3.5.0 -> v3.4.1; fix the unit conversion at cloud-api/nova/compute/client.py:87; add a unit test for the timeout conversion. Mitigated at 2026-03-12T21:50 UTC.

Action items: Add a regression test covering ComputeClient.call (cloud-api/nova/compute/client.py).; Alert on the error signature `GET <*> <*> <*> target=cloud-api`..

Services affected: cloud-api, api-gateway.
