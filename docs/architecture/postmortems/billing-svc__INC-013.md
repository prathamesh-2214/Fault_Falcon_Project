# Postmortem INC-013: Latency and timeouts on api-gateway

Published: 2026-03-29T02:54:43.823229+00:00

Date: 2026-03-24T11:49 UTC. Severity: SEV-2. Alert: api-gateway: p99 latency > 2 s for 5 min on api-gateway.

Impact: Requests slowed down and a share of them timed out on api-gateway.

Root cause: Deploy v4.7.0 of billing-svc shrinks its outbound timeout by a factor of 1000, so calls time out almost immediately.

Breaking change: billing/client/NotificationClient.java:270 (NotificationClient.send). billing/client/NotificationClient.java:270 (NotificationClient.send) converts http.timeout_ms with the wrong unit.

How it was fixed: Roll back billing-svc v4.7.0 -> v4.6.0; fix the unit conversion at billing/client/NotificationClient.java:270; add a unit test for the timeout conversion. Mitigated at 2026-03-24T14:51 UTC.

Action items: Add a regression test covering NotificationClient.send (billing/client/NotificationClient.java).; Alert on the error signature `GET <*> <*> <*> target=billing-svc`..

Services affected: billing-svc, api-gateway.
