# Postmortem INC-074: Throttling (429) on billing-svc

Published: 2026-07-05T12:54:55.597845+00:00

Date: 2026-07-01T19:16 UTC. Severity: SEV-1. Alert: billing-svc: 429 rate > 1 % for 5 min on billing-svc.

Impact: Customers were throttled on billing-svc.

Root cause: Config change raised webhook.max_retries 3 -> 24; one failing customer endpoint multiplied invocations until notification-svc hit its Lambda concurrency limit and invoice e-mails were throttled.

Breaking change: config/notification-svc.yaml:33 (webhook.max_retries). config/notification-svc.yaml:33 sets webhook.max_retries 3 -> 24.

How it was fixed: Revert webhook.max_retries to 3; add exponential backoff with jitter and a per-endpoint circuit breaker. Mitigated at 2026-07-01T23:21 UTC.

Action items: Add validation bounds and a canary for risky config keys (pool / timeout / retry / flags).; Alert on the error signature `c.f.b.client.NotificationClient - call to notification-svc failed: HTTP 429 Too `..

Services affected: notification-svc, billing-svc, api-gateway.
