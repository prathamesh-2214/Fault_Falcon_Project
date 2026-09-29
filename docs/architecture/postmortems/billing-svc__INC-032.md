# Postmortem INC-032: Elevated 5xx errors on api-gateway

Published: 2026-04-29T14:59:45.275875+00:00

Date: 2026-04-24T12:57 UTC. Severity: SEV-2. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: A share of requests failed with 5xx on api-gateway.

Root cause: Deploy v4.15.0 of billing-svc was a routine dependency bump; HikariCP 5.1 validates connections with a new keepalive that the RDS proxy closes.

Breaking change: billing/pom.xml:116 (pom.xml). billing/pom.xml:116 upgrades the library: HikariCP 5.1 validates connections with a new keepalive that the RDS proxy closes.

How it was fixed: Roll back billing-svc v4.15.0 -> v4.14.0; pin the previous version in billing/pom.xml until the upgrade is tested against the real dependency. Mitigated at 2026-04-24T17:08 UTC.

Action items: Add a regression test covering pom.xml (billing/pom.xml).; Alert on the error signature `authorizer: ImmatureSignatureError: The token is not yet valid (iat)`..

Services affected: billing-svc, api-gateway.
