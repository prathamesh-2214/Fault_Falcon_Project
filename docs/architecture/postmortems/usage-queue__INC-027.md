# Postmortem INC-027: Processing errors on billing-svc

Published: 2026-04-18T20:41:19.964612+00:00

Date: 2026-04-16T07:30 UTC. Severity: SEV-3. Alert: billing-svc: error-log rate > 20/min (CloudWatch Logs metric filter) on billing-svc.

Impact: Background processing failed; customer-visible data was delayed or wrong on billing-svc.

Root cause: Terraform change cut sqs_visibility_timeout_s 300 -> 30 s, shorter than billing-svc's processing time; messages became visible again mid-processing and were billed twice (duplicate-key errors).

Breaking change: infra/usage-queue.tf:69 (aws_sqs_queue.usage_queue). infra/usage-queue.tf:69 sets visibility_timeout_seconds 300 -> 30 on aws_sqs_queue.usage_queue.

How it was fixed: Restore visibility_timeout_seconds = 300 on aws_sqs_queue.usage_queue; make the consumer idempotent. Mitigated at 2026-04-16T11:15 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `c.f.b.consumer.UsageEventConsumer - message <*> received <*> times`..

Services affected: usage-queue, billing-svc, api-gateway.
