# Postmortem INC-036: Throttling (429) on notification-svc

Published: 2026-05-05T16:16:49.487637+00:00

Date: 2026-04-30T19:26 UTC. Severity: SEV-2. Alert: notification-svc: 429 rate > 1 % for 5 min on notification-svc.

Impact: Customers were throttled on notification-svc.

Root cause: Config change raised webhook.max_retries 3 -> 24; one failing customer endpoint multiplied invocations until notification-svc hit its Lambda concurrency limit and invoice e-mails were throttled.

Breaking change: config/notification-svc.yaml:33 (webhook.max_retries). config/notification-svc.yaml:33 sets webhook.max_retries 3 -> 24.

How it was fixed: Revert webhook.max_retries to 3; add exponential backoff with jitter and a per-endpoint circuit breaker. Mitigated at 2026-05-01T00:13 UTC.

Action items: Add validation bounds and a canary for risky config keys (pool / timeout / retry / flags).; Alert on the error signature `[WARNING] webhook delivery failed endpoint=hooks.acme.example.com <*> status=502`..

Services affected: notification-svc, billing-svc, api-gateway.
