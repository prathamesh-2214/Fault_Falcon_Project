# Runbook: auth-svc

Published: 2026-03-02T00:00:00+00:00

Issues and validates access tokens; sessions in Redis, accounts in the meta DB. Runs on ECS Fargate (Go), 3 tasks behind an internal ALB; owned by team-identity.

## Current configuration (config/auth-svc.yaml)

- http.timeout_ms: 4500
- db.pool.max_size: 70
- db.statement_timeout_ms: 45000
- retry.max: 3
- retry.backoff_ms: 600
- log.level: DEBUG
- metrics.interval_s: 30
- cache.session_ttl_s: 3600
- feature.token_v2: off

## Infrastructure (infra/auth-svc.tf)

- ecs_desired_count: 17
- ecs_task_memory_mb: 2048
- lb_healthcheck_timeout_s: 5

## What to check on errors

On token failures: check session-cache (Redis) health first: evictions, maxmemory-policy, node type and the security group on 6379; then cache.session_ttl_s and meta-db load. On 502 from the ALB: check the target-group health check and ECS task memory.

Dependencies: session-cache, meta-db. Host: none. Called by: api-gateway.

## Normal behaviour (from logs)

- error ratio band: 0.00 - 0.20 (median 0.00)
