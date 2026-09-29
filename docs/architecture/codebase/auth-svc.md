# Codebase: auth-svc

Published: 2026-03-02T00:00:00+00:00

Owner: team-identity. Runs on: ECS Fargate (Go), 3 tasks behind an internal ALB. Calls: session-cache, meta-db. Host: its own instances. Called by: api-gateway.

- `auth/internal/token/issuer.go`: Issues signed access tokens. Connections: meta-db: accounts. Reads config: feature.token_v2. Calls: -.

- `auth/internal/session/store.go`: Session store (read-through Redis cache). Connections: session-cache (Redis). Reads config: cache.session_ttl_s, http.timeout_ms. Calls: session-cache.

- `auth/internal/account/repo.go`: Account lookups. Connections: meta-db (RDS) via pgx pool. Reads config: db.pool.max_size, db.statement_timeout_ms. Calls: meta-db.

- `auth/internal/token/refresh.go`: Refresh-token rotation. Connections: session-cache. Reads config: feature.token_v2. Calls: -.

- `auth/go.mod`: Dependency manifest (library versions). Connections: -. Reads config: -. Calls: -.

- `config/auth-svc.yaml`: Service configuration (pushed through AppConfig). Connections: -. Reads config: -. Calls: -.

- `infra/auth-svc.tf`: AWS resources (Terraform). Connections: -. Reads config: -. Calls: -.
