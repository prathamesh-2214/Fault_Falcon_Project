# Postmortem INC-034: Throttling (429) on api-gateway

Published: 2026-04-30T18:50:01.171468+00:00

Date: 2026-04-27T17:24 UTC. Severity: SEV-2. Alert: api-gateway: 429 rate > 1 % for 5 min on api-gateway.

Impact: Customers were throttled on api-gateway.

Root cause: No change explains the anomaly: a customer's batch integration sent 12x its usual request rate for 40 minutes.

How it was fixed: No rollback needed; per-customer throttling for that account; no change was rolled back. Mitigated at 2026-04-27T22:01 UTC.

Action items: Alert on the error signature `POST /v1/servers <*> <*> <*>`..

Services affected: api-gateway.
