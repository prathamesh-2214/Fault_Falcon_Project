# Postmortem INC-017: Elevated 5xx errors on api-gateway

Published: 2026-04-04T03:51:28.553788+00:00

Date: 2026-03-30T17:10 UTC. Severity: SEV-1. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: A share of requests failed with 5xx on api-gateway.

Root cause: Deploy v2.11.0 of api-gateway introduced a regression.

Breaking change: gateway/transforms/request_mapper.py:196 (map_request). gateway/transforms/request_mapper.py:196 (map_request) changes the condition / constant shown in the diff.

How it was fixed: Roll back api-gateway v2.11.0 -> v2.10.0; revert gateway/transforms/request_mapper.py:196. Mitigated at 2026-03-30T21:20 UTC.

Action items: Add a regression test covering map_request (gateway/transforms/request_mapper.py).; Alert on the error signature `unhandled error in request handler: <*> failures in 60 s`..

Services affected: api-gateway.
