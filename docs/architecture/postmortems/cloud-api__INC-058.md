# Postmortem INC-058: Latency and timeouts on api-gateway

Published: 2026-06-10T12:23:13.837416+00:00

Date: 2026-06-05T14:26 UTC. Severity: SEV-1. Alert: api-gateway: p99 latency > 2 s for 5 min on api-gateway.

Impact: Requests slowed down and a share of them timed out on api-gateway.

Root cause: Deploy v3.26.0 of cloud-api made base-image verification synchronous in the request path; the shorter ALB health-check timeout then marked slow targets unhealthy.

Breaking change: cloud-api/nova/image/cache.py:99 (ImageCacheManager.verify_base_images). cloud-api/nova/image/cache.py:99 (ImageCacheManager.verify_base_images) calls _verify_sync inline instead of submitting it to the executor.

How it was fixed: Roll back cloud-api v3.26.0 -> v3.25.0; restore the async executor call at cloud-api/nova/image/cache.py:99; revert the aws_lb_target_group health_check timeout. Mitigated at 2026-06-05T18:56 UTC.

Action items: Add a regression test covering ImageCacheManager.verify_base_images (cloud-api/nova/image/cache.py).; Alert on the error signature `GET <*> <*> <*> target=cloud-api`..

Services affected: cloud-api, api-gateway.
