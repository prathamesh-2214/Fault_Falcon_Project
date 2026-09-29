# Postmortem INC-068: Elevated 5xx errors on api-gateway

Published: 2026-06-26T17:50:19.578112+00:00

Date: 2026-06-22T07:54 UTC. Severity: SEV-3. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: A share of requests failed with 5xx on api-gateway.

Root cause: Deploy v1.16.0 of coord-svc removed a null/nil guard; requests for records without the optional field now crash the handler.

Breaking change: coord/session/SessionTracker.java:172 (SessionTrackerImpl.run). coord/session/SessionTracker.java:172 (SessionTrackerImpl.run) drops the null check before dereferencing the field.

How it was fixed: Roll back coord-svc v1.16.0 -> v1.15.0; restore the guard at coord/session/SessionTracker.java:172; add a test with the optional field missing. Mitigated at 2026-06-22T12:44 UTC.

Action items: Add a regression test covering SessionTrackerImpl.run (coord/session/SessionTracker.java).; Alert on the error signature `GET <*> <*> <*> target=cloud-api`..

Services affected: coord-svc, compute-svc, cloud-api, api-gateway.
