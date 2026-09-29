# Postmortem INC-030: Elevated 5xx errors on cloud-api

Published: 2026-04-24T20:38:59.258380+00:00

Date: 2026-04-21T08:30 UTC. Severity: SEV-2. Alert: cloud-api: 5XX error rate > 2 % for 5 min on cloud-api.

Impact: A share of requests failed with 5xx on cloud-api.

Root cause: No change explains the anomaly: Amazon S3 in us-east-1 returned elevated 503 SlowDown errors (AWS Health event).

How it was fixed: No rollback needed; waited for AWS to recover; added jittered retries and a regional fallback read. Mitigated at 2026-04-21T12:52 UTC.

Action items: Alert on the error signature `<*> <*> <*> HTTP/1.1" status: <*> len: <*> time: <*>`..

Services affected: object-store, image-svc, cloud-api, api-gateway.
