# Postmortem INC-073: Latency and timeouts on billing-svc

Published: 2026-07-04T02:11:09.909462+00:00

Date: 2026-06-30T09:34 UTC. Severity: SEV-1. Alert: billing-svc: p99 latency > 2 s for 5 min on billing-svc.

Impact: Requests slowed down and a share of them timed out on billing-svc.

Root cause: Config change shrank the billing-svc database connection pool; requests queued for connections and timed out (the billing-db downsize days earlier left no headroom).

Breaking change: config/billing-svc.yaml:29 (db.pool.max_size). config/billing-svc.yaml:29 sets db.pool.max_size 80 -> 8.

How it was fixed: Revert db.pool.max_size to 80 in config/billing-svc.yaml; right-size aws_db_instance.billing_db before lowering pools again. Mitigated at 2026-06-30T13:45 UTC.

Action items: Add validation bounds and a canary for risky config keys (pool / timeout / retry / flags).; Alert on the error signature `connection pool exhausted: timed out after 30000 ms waiting for a connection`..

Services affected: billing-svc, api-gateway.
