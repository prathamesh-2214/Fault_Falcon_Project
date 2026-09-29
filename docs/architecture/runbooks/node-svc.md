# Runbook: node-svc

Published: 2026-03-02T00:00:00+00:00

Host layer: kernel, program loader, RAS, Lustre mounts. Runs on EC2 compute fleet (HPC nodes, C); owned by team-hpc.

## Current configuration (config/node-svc.yaml)

- http.timeout_ms: 5500
- retry.max: 3
- retry.backoff_ms: 600
- log.level: DEBUG
- metrics.interval_s: 60
- feature.ecc_scrub: on

## Infrastructure (infra/node-svc.tf)

- disk_gb: 2250
- instance_type: m5.4xlarge
- node_pool_size: 7
- asg_max_size: 12

## What to check on errors

On KERNDTLB (TLB errors): check instance_type and memory. On KERNSTOR (data storage interrupt): check disk_gb. On ciod load failures or Lustre mount failures: check recent kernel/ciod deploys.

Dependencies: none. Host: none. Called by: none.

## Normal behaviour (from logs)

- error ratio band: 0.00 - 0.15 (median 0.00)
