# Codebase: api-gateway

Published: 2026-03-02T00:00:00+00:00

Owner: team-edge. Runs on: Amazon API Gateway (REST) + Application Load Balancer, Lambda authorizer. Calls: auth-svc, cloud-api, billing-svc. Host: its own instances. Called by: none.

- `gateway/authorizer/handler.py`: Lambda authorizer: validates bearer tokens with auth-svc. Connections: none. Reads config: http.timeout_ms, retry.max. Calls: auth-svc.

- `gateway/authorizer/jwks.py`: Caches auth-svc signing keys (JWKS). Connections: none. Reads config: http.timeout_ms. Calls: auth-svc.

- `gateway/transforms/request_mapper.py`: Maps public routes to internal targets. Connections: none. Reads config: breaker.failure_pct. Calls: cloud-api, billing-svc.

- `gateway/throttle/usage_plans.py`: Per-customer usage plans and throttling. Connections: none. Reads config: -. Calls: -.

- `gateway/requirements.txt`: Dependency manifest (library versions). Connections: -. Reads config: -. Calls: -.

- `config/api-gateway.yaml`: Service configuration (pushed through AppConfig). Connections: -. Reads config: -. Calls: -.

- `infra/api-gateway.tf`: AWS resources (Terraform). Connections: -. Reads config: -. Calls: -.
