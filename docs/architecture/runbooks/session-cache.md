# Runbook: session-cache

Published: 2026-03-02T00:00:00+00:00

Session and token cache (read-through). Runs on Amazon ElastiCache for Redis 7 (cluster mode off); owned by team-identity.

## Infrastructure (infra/session-cache.tf)

- redis_node_type: cache.r6g.xlarge
- redis_maxmemory_policy: volatile-lru
- redis_sg_port: 6379

## What to check on errors

On evictions or OOM: node type and maxmemory-policy; on connection timeouts: the security group and the port.

Dependencies: none. Host: none. Called by: auth-svc.

## Normal behaviour (from logs)

- error ratio band: 0.00 - 0.14 (median 0.00)
