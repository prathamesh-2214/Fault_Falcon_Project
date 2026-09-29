# Postmortem INC-025: Elevated 5xx errors on api-gateway

Published: 2026-04-15T16:12:16.079685+00:00

Date: 2026-04-13T05:28 UTC. Severity: SEV-3. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: A share of requests failed with 5xx on api-gateway.

Root cause: Deploy v2.16.0 of api-gateway introduced a regression.

Breaking change: gateway/transforms/request_mapper.py:223 (map_request). gateway/transforms/request_mapper.py:223 (map_request) changes the condition / constant shown in the diff.

How it was fixed: Roll back api-gateway v2.16.0 -> v2.15.0; revert gateway/transforms/request_mapper.py:223. Mitigated at 2026-04-13T08:13 UTC.

Action items: Add a regression test covering map_request (gateway/transforms/request_mapper.py).; Alert on the error signature `unhandled error in request handler: <*> failures in 60 s`..

Services affected: api-gateway.
