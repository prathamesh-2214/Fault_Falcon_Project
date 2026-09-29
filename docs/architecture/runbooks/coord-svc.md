# Runbook: coord-svc

Published: 2026-03-02T00:00:00+00:00

Coordination: leader election, sessions, locks. Runs on EC2, 3-node ZooKeeper quorum; owned by team-platform.

## Current configuration (config/coord-svc.yaml)

- http.timeout_ms: 5000
- retry.max: 3
- retry.backoff_ms: 400
- jvm.heap_mb: 6144
- gc.algorithm: G1
- log.level: DEBUG
- metrics.interval_s: 30
- session.timeout_ms: 4000
- feature.fast_election: on

## Infrastructure (infra/coord-svc.tf)

- disk_gb: 1750
- instance_type: m5.4xlarge
- node_pool_size: 7
- sg_election_port: 3888

## What to check on errors

On 'Connection broken' or election timeouts: check the security group (2888/3888), session.timeout_ms, recent config changes and quorum membership (QuorumCnxManager).

Dependencies: none. Host: none. Called by: compute-svc.

## Normal behaviour (from logs)

- error ratio band: 0.00 - 0.16 (median 0.00)
