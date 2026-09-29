# Postmortem INC-016: Latency and timeouts on api-gateway

Published: 2026-04-01T22:55:33.018445+00:00

Date: 2026-03-29T04:57 UTC. Severity: SEV-3. Alert: api-gateway: p99 latency > 2 s for 5 min on api-gateway.

Impact: Requests slowed down and a share of them timed out on api-gateway.

Root cause: Deploy v3.9.0 of cloud-api made base-image verification synchronous in the request path; the shorter ALB health-check timeout then marked slow targets unhealthy.

Breaking change: cloud-api/nova/image/cache.py:294 (ImageCacheManager.verify_base_images). cloud-api/nova/image/cache.py:294 (ImageCacheManager.verify_base_images) calls _verify_sync inline instead of submitting it to the executor.

How it was fixed: Roll back cloud-api v3.9.0 -> v3.8.0; restore the async executor call at cloud-api/nova/image/cache.py:294; revert the aws_lb_target_group health_check timeout. Mitigated at 2026-03-29T09:44 UTC.

Action items: Add a regression test covering ImageCacheManager.verify_base_images (cloud-api/nova/image/cache.py).; Alert on the error signature `GET <*> <*> <*> target=cloud-api`..

Services affected: cloud-api, api-gateway.
