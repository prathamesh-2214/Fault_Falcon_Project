# Codebase: usage-queue

Published: 2026-03-02T00:00:00+00:00

Owner: team-billing. Runs on: Amazon SQS standard queue + DLQ. Calls: none. Host: its own instances. Called by: billing-svc.

- `infra/usage-queue.tf`: AWS resources (Terraform). Connections: -. Reads config: -. Calls: -.

Managed resource: no application code. Terraform keys: sqs_visibility_timeout_s, sqs_max_receive_count, sqs_retention_s.
