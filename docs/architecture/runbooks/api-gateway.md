# Runbook: api-gateway

Published: 2026-03-02T00:00:00+00:00

Public entry point: routing, throttling, TLS, request auth. Runs on Amazon API Gateway (REST) + Application Load Balancer, Lambda authorizer; owned by team-edge.

## Current configuration (config/api-gateway.yaml)

- http.timeout_ms: 3500
- retry.max: 3
- retry.backoff_ms: 1400
- log.level: INFO
- metrics.interval_s: 60
- breaker.failure_pct: 50
- feature.edge_cache: off

## Infrastructure (infra/api-gateway.tf)

- lb_healthcheck_timeout_s: 5
- alb_idle_timeout_s: 60
- apigw_burst_limit: 5000
- apigw_rate_limit: 6500
- lambda_concurrency: 50
- lambda_timeout_s: 300

## What to check on errors

On 5xx: find the target in the access log (target=...) and follow that service. On 429: check the usage-plan throttle_settings in infra/api-gateway.tf and per-customer traffic. On 504: compare the ALB idle_timeout with the slowest routes (report exports). On 401 bursts: check auth-svc and the authorizer's JWKS cache.

Dependencies: auth-svc, cloud-api, billing-svc. Host: none. Called by: none.

## Normal behaviour (from logs)

- error ratio band: 0.00 - 0.00 (median 0.00)
- p95 latency band: 0.109 - 0.245 s
- known noise: GET <*> <*> <*> target=billing-svc; GET <*> <*> <*> target=cloud-api; POST <*> <*> <*> target=auth-svc; POST <*> <*> <*> target=cloud-api
