# Runbook: usage-queue

Published: 2026-03-02T00:00:00+00:00

Usage events from cloud-api to billing-svc. Runs on Amazon SQS standard queue + DLQ; owned by team-billing.

## Infrastructure (infra/usage-queue.tf)

- sqs_visibility_timeout_s: 300
- sqs_max_receive_count: 5
- sqs_retention_s: 1209600

## What to check on errors

On a growing backlog: consumer errors in billing-svc. On DLQ growth: maxReceiveCount and consumer failures. On duplicates: visibility_timeout_seconds vs processing time.

Dependencies: none. Host: none. Called by: billing-svc.

## Normal behaviour (from logs)

- error ratio band: 0.00 - 0.20 (median 0.00)
- known noise: metric usage-queue <*> <*> <*>
