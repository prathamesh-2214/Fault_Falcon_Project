# Postmortem INC-054: Processing errors on notification-svc

Published: 2026-06-03T23:21:32.888375+00:00

Date: 2026-05-30T01:43 UTC. Severity: SEV-1. Alert: notification-svc: error-log rate > 20/min (CloudWatch Logs metric filter) on notification-svc.

Impact: Background processing failed; customer-visible data was delayed or wrong on notification-svc.

Root cause: No change explains the anomaly: the TLS certificate of a large customer's webhook endpoint expired.

How it was fixed: No rollback needed; the customer renewed the certificate; webhook failures for that endpoint are now alerted separately. Mitigated at 2026-05-30T06:29 UTC.

Action items: Alert on the error signature `[ERROR] webhook delivery failed endpoint=hooks.acme.example.com: SSLError certif`..

Services affected: notification-svc, billing-svc, api-gateway.
