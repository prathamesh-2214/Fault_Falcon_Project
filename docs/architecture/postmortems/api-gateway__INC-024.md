# Postmortem INC-024: Throttling (429) on api-gateway

Published: 2026-04-16T03:14:30.277717+00:00

Date: 2026-04-11T05:54 UTC. Severity: SEV-1. Alert: api-gateway: 429 rate > 1 % for 5 min on api-gateway.

Impact: Customers were throttled on api-gateway.

Root cause: No change explains the anomaly: a customer's batch integration sent 12x its usual request rate for 40 minutes.

How it was fixed: No rollback needed; per-customer throttling for that account; no change was rolled back. Mitigated at 2026-04-11T10:31 UTC.

Action items: Alert on the error signature `POST /v1/servers <*> <*> <*>`..

Services affected: api-gateway.
