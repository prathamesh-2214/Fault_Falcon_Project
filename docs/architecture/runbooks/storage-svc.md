# Runbook: storage-svc

Published: 2026-03-02T00:00:00+00:00

Block store for job data. Runs on EC2 + EBS gp3 (HDFS, Java); owned by team-storage.

## Current configuration (config/storage-svc.yaml)

- http.timeout_ms: 3500
- retry.max: 3
- retry.backoff_ms: 800
- jvm.heap_mb: 5120
- gc.algorithm: G1
- log.level: DEBUG
- metrics.interval_s: 60
- dfs.replication: 2
- feature.fast_ack: off

## Infrastructure (infra/storage-svc.tf)

- disk_gb: 1000
- ebs_throughput: 250
- ebs_iops: 6000
- instance_type: m5.4xlarge
- node_pool_size: 7

## What to check on errors

On 'exception while serving blk' or write failures: check EBS throughput / IOPS and disk_gb headroom first (latent changes show up days later), recent datanode deploys (BlockWriter, DataXceiver), then node-svc.

Dependencies: none. Host: node-svc. Called by: compute-svc.

## Normal behaviour (from logs)

- error ratio band: 0.00 - 0.14 (median 0.00)
