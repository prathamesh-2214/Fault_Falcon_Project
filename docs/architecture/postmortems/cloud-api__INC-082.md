# Postmortem INC-082: Latency and timeouts on api-gateway

Published: 2026-07-19T06:47:43.896721+00:00

Date: 2026-07-14T19:37 UTC. Severity: SEV-3. Alert: api-gateway: p99 latency > 2 s for 5 min on api-gateway.

Impact: Requests slowed down and a share of them timed out on api-gateway.

Root cause: Deploy v3.35.0 of cloud-api introduced a regression that only the skipped ppd load suite would have caught.

Breaking change: cloud-api/nova/image/cache.py:52 (ImageCacheManager.verify_base_images). cloud-api/nova/image/cache.py:52 (ImageCacheManager.verify_base_images) changes the condition / constant shown in the diff.

How it was fixed: Roll back cloud-api v3.35.0 -> v3.34.0; revert cloud-api/nova/image/cache.py:52. Make the ppd load suite mandatory. Mitigated at 2026-07-14T23:53 UTC.

Action items: Make the ppd load test suite mandatory before prod.; Add a regression test covering ImageCacheManager.verify_base_images (cloud-api/nova/image/cache.py).; Alert on the error signature `GET <*> <*> <*> target=cloud-api`..

Services affected: cloud-api, api-gateway.
