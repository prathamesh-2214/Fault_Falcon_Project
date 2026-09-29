# Postmortem INC-086: Elevated 5xx errors on billing-svc

Published: 2026-07-26T00:28:16.293236+00:00

Date: 2026-07-21T10:12 UTC. Severity: SEV-1. Alert: billing-svc: 5XX error rate > 2 % for 5 min on billing-svc.

Impact: A share of requests failed with 5xx on billing-svc.

Root cause: Config change set breaker.failure_pct 50 -> 5; ordinary error blips opened the circuit breaker and billing-svc returned 503 for healthy downstreams.

Breaking change: config/billing-svc.yaml:105 (breaker.failure_pct). config/billing-svc.yaml:105 sets breaker.failure_pct 50 -> 5.

How it was fixed: Revert breaker.failure_pct to 50 in config/billing-svc.yaml. Mitigated at 2026-07-21T13:13 UTC.

Action items: Add validation bounds and a canary for risky config keys (pool / timeout / retry / flags).; Alert on the error signature `circuit breaker OPEN: failure rate <*> above threshold 5%`..

Services affected: billing-svc, api-gateway.
