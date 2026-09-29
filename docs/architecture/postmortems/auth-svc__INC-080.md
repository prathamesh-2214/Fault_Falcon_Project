# Postmortem INC-080: Latency and timeouts on auth-svc

Published: 2026-07-16T08:12:36.454010+00:00

Date: 2026-07-11T21:32 UTC. Severity: SEV-3. Alert: auth-svc: p99 latency > 2 s for 5 min on auth-svc.

Impact: Requests slowed down and a share of them timed out on auth-svc.

Root cause: Config change set cache.session_ttl_s to 36 instead of 3600; almost every token check missed the Redis cache and hit meta-db, which saturated.

Breaking change: config/auth-svc.yaml:81 (cache.session_ttl_s). config/auth-svc.yaml:81 sets cache.session_ttl_s 3600 -> 36.

How it was fixed: Revert cache.session_ttl_s to 3600 in config/auth-svc.yaml; add bounds validation for TTLs. Mitigated at 2026-07-12T02:29 UTC.

Action items: Add validation bounds and a canary for risky config keys (pool / timeout / retry / flags).; Alert on the error signature `{"level":"warn","msg":"session cache <*>`..

Services affected: auth-svc, api-gateway, meta-db.
