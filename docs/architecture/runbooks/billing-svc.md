# Runbook: billing-svc

Published: 2026-03-02T00:00:00+00:00

Usage-based billing: consumes usage events, writes invoices, customer billing API. Runs on ECS Fargate (Java, Spring Boot); owned by team-billing.

## Current configuration (config/billing-svc.yaml)

- http.timeout_ms: 2500
- db.pool.max_size: 90
- db.statement_timeout_ms: 45000
- retry.max: 3
- retry.backoff_ms: 1100
- jvm.heap_mb: 7168
- gc.algorithm: ZGC
- log.level: DEBUG
- metrics.interval_s: 60
- consumer.concurrency: 8
- db.credentials_cache_s: 900
- breaker.failure_pct: 50
- feature.invoice_v2: off

## Infrastructure (infra/billing-svc.tf)

- ecs_desired_count: 14
- ecs_task_memory_mb: 2048

## What to check on errors

On consumer errors: look at the usage-queue (age of oldest message, DLQ depth) and the first failing message: a parse error means a producer (cloud-api) changed the event schema; duplicate keys mean the SQS visibility timeout is shorter than processing. On DB errors: billing-db connections (consumer.concurrency x pool size vs max_connections) and Secrets Manager rotation.

Dependencies: billing-db, usage-queue, notification-svc. Host: none. Called by: api-gateway.

## Normal behaviour (from logs)

- error ratio band: 0.00 - 0.17 (median 0.00)
