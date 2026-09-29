# Postmortem INC-066: Throttling (429) on billing-svc

Published: 2026-06-22T13:23:59.839322+00:00

Date: 2026-06-18T18:22 UTC. Severity: SEV-2. Alert: billing-svc: 429 rate > 1 % for 5 min on billing-svc.

Impact: Customers were throttled on billing-svc.

Root cause: Terraform change cut reserved concurrency of notification-svc 50 -> 5; invocations were throttled (Rate Exceeded) and billing-svc's calls failed.

Breaking change: infra/notification-svc.tf:61 (aws_lambda_function.notification_svc). infra/notification-svc.tf:61 sets reserved_concurrent_executions 50 -> 5 on aws_lambda_function.notification_svc.

How it was fixed: Restore reserved concurrency = 50 on aws_lambda_function.notification_svc. Mitigated at 2026-06-18T22:11 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `c.f.b.client.NotificationClient - call to notification-svc failed: HTTP 429 Too `..

Services affected: notification-svc, billing-svc, api-gateway.
