# Postmortem INC-062: Elevated 5xx errors on api-gateway

Published: 2026-06-15T15:13:08.655341+00:00

Date: 2026-06-12T12:01 UTC. Severity: SEV-1. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: A share of requests failed with 5xx on api-gateway.

Root cause: Config change cut the billing-svc JVM heap.

Breaking change: config/billing-svc.yaml:113 (jvm.heap_mb). config/billing-svc.yaml:113 sets jvm.heap_mb 7168 -> 1792.

How it was fixed: Revert jvm.heap_mb to 7168 in config/billing-svc.yaml. Mitigated at 2026-06-12T14:51 UTC.

Action items: Add validation bounds and a canary for risky config keys (pool / timeout / retry / flags).; Alert on the error signature `GET <*> <*> <*> target=billing-svc`..

Services affected: billing-svc, api-gateway.
