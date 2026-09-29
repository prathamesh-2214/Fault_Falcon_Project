# Runbook: compute-svc

Published: 2026-03-02T00:00:00+00:00

Job scheduler and MapReduce runtime. Runs on EC2 Auto Scaling group (Hadoop YARN, Java); owned by team-batch.

## Current configuration (config/compute-svc.yaml)

- http.timeout_ms: 3000
- db.pool.max_size: 60
- db.statement_timeout_ms: 30000
- retry.max: 3
- retry.backoff_ms: 700
- jvm.heap_mb: 8192
- gc.algorithm: G1
- log.level: DEBUG
- metrics.interval_s: 30
- session.timeout_ms: 4000
- feature.new_scheduler: on

## Infrastructure (infra/compute-svc.tf)

- disk_gb: 1750
- instance_type: m5.4xlarge
- node_pool_size: 11
- asg_max_size: 12

## What to check on errors

On task failures, 'ERROR IN CONTACTING RM' or lease-renewal failures: check RM connectivity (rpc/RMClient.java), retry.max and http.timeout_ms, then storage-svc, coord-svc, meta-db and the node-svc fleet (Auto Scaling max_size). Check jvm.heap_mb on memory errors.

Dependencies: storage-svc, coord-svc, meta-db. Host: node-svc. Called by: cloud-api.

## Normal behaviour (from logs)

- error ratio band: 0.00 - 0.14 (median 0.00)
