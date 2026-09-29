# Postmortem INC-061: Latency and timeouts on api-gateway

Published: 2026-06-14T08:59:59.944073+00:00

Date: 2026-06-10T19:05 UTC. Severity: SEV-2. Alert: api-gateway: p99 latency > 2 s for 5 min on api-gateway.

Impact: Requests slowed down and a share of them timed out on api-gateway.

Root cause: Config change shrank the cloud-api database connection pool; requests queued for connections and timed out (the meta-db downsize days earlier left no headroom).

Breaking change: config/cloud-api.yaml:52 (db.pool.max_size). config/cloud-api.yaml:52 sets db.pool.max_size 60 -> 6.

How it was fixed: Revert db.pool.max_size to 60 in config/cloud-api.yaml; right-size aws_db_instance.meta_db before lowering pools again. Mitigated at 2026-06-10T23:06 UTC.

Action items: Add validation bounds and a canary for risky config keys (pool / timeout / retry / flags).; Alert on the error signature `GET <*> <*> <*> target=cloud-api`..

Services affected: cloud-api, api-gateway.
