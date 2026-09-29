# Postmortem INC-005: Elevated 5xx errors on api-gateway

Published: 2026-03-14T10:14:17.450234+00:00

Date: 2026-03-11T09:56 UTC. Severity: SEV-3. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: A share of requests failed with 5xx on api-gateway.

Root cause: Release train RT-26.11 shipped FEAT-116 (Token refresh v2) across services; the auth-svc part (v3.4.0) broke, and the failure surfaced in api-gateway through the dependency chain. The later parts of the same feature were harmless.

Breaking change: auth/internal/token/issuer.go:238 (Issuer.Issue). auth/internal/token/issuer.go:238 (Issuer.Issue) divides the token lifetime by 60 (minutes vs seconds).

How it was fixed: Roll back auth-svc v3.4.0 -> v3.3.0 (the other services' parts of FEAT-116 can stay); fix auth/internal/token/issuer.go:238; add a cross-service load test to the train. Mitigated at 2026-03-11T14:21 UTC.

Action items: Add a regression test covering Issuer.Issue (auth/internal/token/issuer.go).; Alert on the error signature `POST <*> <*> <*> target=auth-svc`..

Services affected: auth-svc, api-gateway.
