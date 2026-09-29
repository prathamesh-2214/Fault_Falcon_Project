# Runbook: object-store

Published: 2026-03-02T00:00:00+00:00

Base images and job artifacts. Runs on Amazon S3 bucket (images, job artifacts); owned by team-platform.

## Infrastructure (infra/object-store.tf)

- s3_expiration_days: 365
- s3_request_metrics: true

## What to check on errors

On 403: IAM policies of the reading role. On 404: lifecycle expiration rules. On 503 SlowDown: request rate and AWS health events.

Dependencies: none. Host: none. Called by: image-svc.

## Normal behaviour (from logs)

- error ratio band: 0.00 - 0.20 (median 0.00)
