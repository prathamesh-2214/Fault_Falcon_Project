# Postmortem INC-002: Elevated 5xx errors on api-gateway

Published: 2026-03-11T02:11:08.908402+00:00

Date: 2026-03-06T15:47 UTC. Severity: SEV-3. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: A share of requests failed with 5xx on api-gateway.

Root cause: Deploy v3.2.0 of auth-svc was a routine dependency bump; go-redis 9.5 enables client-side caching handshakes (CLIENT TRACKING) the cluster rejects.

Breaking change: auth/go.mod:116 (go.mod). auth/go.mod:116 upgrades the library: go-redis 9.5 enables client-side caching handshakes (CLIENT TRACKING) the cluster rejects.

How it was fixed: Roll back auth-svc v3.2.0 -> v3.1.0; pin the previous version in auth/go.mod until the upgrade is tested against the real dependency. Mitigated at 2026-03-06T20:04 UTC.

Action items: Add a regression test covering go.mod (auth/go.mod).; Alert on the error signature `authorizer: ImmatureSignatureError: The token is not yet valid (iat)`..

Services affected: auth-svc, api-gateway.
