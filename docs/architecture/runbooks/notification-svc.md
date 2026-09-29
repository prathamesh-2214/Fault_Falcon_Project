# Runbook: notification-svc

Published: 2026-03-02T00:00:00+00:00

Invoice e-mails and customer webhooks. Runs on AWS Lambda (Python) + Amazon SES; owned by team-billing.

## Current configuration (config/notification-svc.yaml)

- http.timeout_ms: 3500
- retry.max: 3
- retry.backoff_ms: 900
- log.level: DEBUG
- metrics.interval_s: 60
- webhook.max_retries: 3
- feature.batch_send: on

## Infrastructure (infra/notification-svc.tf)

- lambda_concurrency: 50
- lambda_timeout_s: 360

## What to check on errors

On throttling: Lambda reserved concurrency and SES sending quota. On webhook failures: webhook.max_retries (retry storms) and the customer's endpoint (TLS certificate).

Dependencies: none. Host: none. Called by: billing-svc.

## Normal behaviour (from logs)

- error ratio band: 0.00 - 0.19 (median 0.00)
