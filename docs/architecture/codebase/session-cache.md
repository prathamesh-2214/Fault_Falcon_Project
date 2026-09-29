# Codebase: session-cache

Published: 2026-03-02T00:00:00+00:00

Owner: team-identity. Runs on: Amazon ElastiCache for Redis 7 (cluster mode off). Calls: none. Host: its own instances. Called by: auth-svc.

- `infra/session-cache.tf`: AWS resources (Terraform). Connections: -. Reads config: -. Calls: -.

Managed resource: no application code. Terraform keys: redis_node_type, redis_maxmemory_policy, redis_sg_port.
