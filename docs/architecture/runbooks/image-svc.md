# Runbook: image-svc

Published: 2026-03-02T00:00:00+00:00

Image registry: base images stored in S3, served to cloud-api and hosts. Runs on ECS Fargate (Go); owned by team-api.

## Current configuration (config/image-svc.yaml)

- http.timeout_ms: 4500
- retry.max: 3
- retry.backoff_ms: 600
- log.level: DEBUG
- metrics.interval_s: 30
- cache.image_mb: 4096
- feature.lazy_pull: off

## Infrastructure (infra/image-svc.tf)

- ecs_desired_count: 11
- ecs_task_memory_mb: 2048
- iam_s3_actions: s3:GetObject,s3:ListBucket

## What to check on errors

On image read failures: check S3 errors (403 AccessDenied -> IAM policy, 404 NoSuchKey -> lifecycle rules, 503 SlowDown -> AWS health), then ECS task memory and the in-memory cache size.

Dependencies: object-store. Host: none. Called by: cloud-api.

## Normal behaviour (from logs)

- error ratio band: 0.00 - 0.20 (median 0.00)
