# Postmortem INC-099: Elevated 5xx errors on image-svc

Published: 2026-08-16T11:11:13.918675+00:00

Date: 2026-08-11T14:26 UTC. Severity: SEV-1. Alert: image-svc: 5XX error rate > 2 % for 5 min on image-svc.

Impact: A share of requests failed with 5xx on image-svc.

Root cause: No change explains the anomaly: Amazon S3 in us-east-1 returned elevated 503 SlowDown errors (AWS Health event).

How it was fixed: No rollback needed; waited for AWS to recover; added jittered retries and a regional fallback read. Mitigated at 2026-08-11T18:12 UTC.

Action items: Alert on the error signature `{"level":"error","msg":"s3 GetObject failed","err":"api error SlowDown: Please r`..

Services affected: object-store, image-svc, cloud-api, api-gateway.
