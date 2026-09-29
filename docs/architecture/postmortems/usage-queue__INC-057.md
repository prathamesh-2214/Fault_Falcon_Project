# Postmortem INC-057: Processing errors on billing-svc

Published: 2026-06-07T11:33:42.365164+00:00

Date: 2026-06-04T08:13 UTC. Severity: SEV-1. Alert: billing-svc: error-log rate > 20/min (CloudWatch Logs metric filter) on billing-svc.

Impact: Background processing failed; customer-visible data was delayed or wrong on billing-svc.

Root cause: Terraform change set maxReceiveCount 5 -> 1; every transient failure sent usage events straight to the DLQ and invoices were missing usage.

Breaking change: infra/usage-queue.tf:24 (aws_sqs_queue.usage_queue). infra/usage-queue.tf:24 sets maxReceiveCount 5 -> 1 on aws_sqs_queue.usage_queue.

How it was fixed: Restore maxReceiveCount = 5; redrive the DLQ. Mitigated at 2026-06-04T11:04 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `c.f.b.invoice.InvoiceService - invoice <*> total does not match usage records`..

Services affected: usage-queue, billing-svc, api-gateway.
