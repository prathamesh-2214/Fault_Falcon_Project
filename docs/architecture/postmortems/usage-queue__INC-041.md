# Postmortem INC-041: Processing errors on billing-svc

Published: 2026-05-11T23:41:03.050750+00:00

Date: 2026-05-09T03:59 UTC. Severity: SEV-3. Alert: billing-svc: error-log rate > 20/min (CloudWatch Logs metric filter) on billing-svc.

Impact: Background processing failed; customer-visible data was delayed or wrong on billing-svc.

Root cause: Terraform change set maxReceiveCount 5 -> 1; every transient failure sent usage events straight to the DLQ and invoices were missing usage.

Breaking change: infra/usage-queue.tf:24 (aws_sqs_queue.usage_queue). infra/usage-queue.tf:24 sets maxReceiveCount 5 -> 1 on aws_sqs_queue.usage_queue.

How it was fixed: Restore maxReceiveCount = 5; redrive the DLQ. Mitigated at 2026-05-09T08:42 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `c.f.b.consumer.UsageEventConsumer - transient error on usage event <*> SocketTim`..

Services affected: usage-queue, billing-svc, api-gateway.
