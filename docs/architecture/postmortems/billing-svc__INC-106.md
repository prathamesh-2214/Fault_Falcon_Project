# Postmortem INC-106: Elevated 5xx errors on api-gateway

Published: 2026-08-27T15:01:35.058787+00:00

Date: 2026-08-23T05:39 UTC. Severity: SEV-1. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: A share of requests failed with 5xx on api-gateway.

Root cause: Config change set breaker.failure_pct 50 -> 5; ordinary error blips opened the circuit breaker and billing-svc returned 503 for healthy downstreams.

Breaking change: config/billing-svc.yaml:105 (breaker.failure_pct). config/billing-svc.yaml:105 sets breaker.failure_pct 50 -> 5.

How it was fixed: Revert breaker.failure_pct to 50 in config/billing-svc.yaml. Mitigated at 2026-08-23T10:33 UTC.

Action items: Add validation bounds and a canary for risky config keys (pool / timeout / retry / flags).; Alert on the error signature `GET <*> <*> <*> target=billing-svc`..

Services affected: billing-svc, api-gateway.
