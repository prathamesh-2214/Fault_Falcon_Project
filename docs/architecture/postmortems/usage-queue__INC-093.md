# Postmortem INC-093: Processing errors on billing-svc

Published: 2026-08-04T13:20:35.525887+00:00

Date: 2026-08-01T22:29 UTC. Severity: SEV-2. Alert: billing-svc: error-log rate > 20/min (CloudWatch Logs metric filter) on billing-svc.

Impact: Background processing failed; customer-visible data was delayed or wrong on billing-svc.

Root cause: Terraform change cut sqs_visibility_timeout_s 300 -> 30 s, shorter than billing-svc's processing time; messages became visible again mid-processing and were billed twice (duplicate-key errors).

Breaking change: infra/usage-queue.tf:69 (aws_sqs_queue.usage_queue). infra/usage-queue.tf:69 sets visibility_timeout_seconds 300 -> 30 on aws_sqs_queue.usage_queue.

How it was fixed: Restore visibility_timeout_seconds = 300 on aws_sqs_queue.usage_queue; make the consumer idempotent. Mitigated at 2026-08-02T01:45 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `c.f.b.consumer.UsageEventConsumer - duplicate key value violates unique constrai`..

Services affected: usage-queue, billing-svc, api-gateway.
