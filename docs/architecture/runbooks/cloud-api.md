# Runbook: cloud-api

Published: 2026-03-02T00:00:00+00:00

Control-plane API: servers, jobs, images; publishes usage events. Runs on EC2 (OpenStack Nova API, Python); owned by team-api.

## Current configuration (config/cloud-api.yaml)

- http.timeout_ms: 3000
- db.pool.max_size: 70
- db.statement_timeout_ms: 120000
- retry.max: 3
- retry.backoff_ms: 700
- log.level: DEBUG
- metrics.interval_s: 30
- breaker.failure_pct: 50
- feature.async_boot: off

## Infrastructure (infra/cloud-api.tf)

- disk_gb: 1000
- instance_type: m5.4xlarge
- node_pool_size: 10
- lb_healthcheck_timeout_s: 5

## What to check on errors

On latency spikes or 5xx: check compute-svc and image-svc health and their recent deploys, then http.timeout_ms and db.pool.max_size in config/cloud-api.yaml and meta-db load. 'Unknown base file' warnings come from the image cache (nova/image/cache.py) and image-svc / S3.

Dependencies: compute-svc, image-svc, meta-db, usage-queue. Host: none. Called by: api-gateway.

## Normal behaviour (from logs)

- error ratio band: 0.00 - 0.00 (median 0.00)
- p95 latency band: 0.264 - 0.453 s
- known noise: <*> <*> <*> HTTP/1.1" status: <*> len: <*> time: <*>
