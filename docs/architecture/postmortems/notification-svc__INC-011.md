# Postmortem INC-011: Processing errors on notification-svc

Published: 2026-03-25T06:33:21.118210+00:00

Date: 2026-03-21T01:35 UTC. Severity: SEV-2. Alert: notification-svc: error-log rate > 20/min (CloudWatch Logs metric filter) on notification-svc.

Impact: Background processing failed; customer-visible data was delayed or wrong on notification-svc.

Root cause: No change explains the anomaly: the TLS certificate of a large customer's webhook endpoint expired.

How it was fixed: No rollback needed; the customer renewed the certificate; webhook failures for that endpoint are now alerted separately. Mitigated at 2026-03-21T04:17 UTC.

Action items: Alert on the error signature `[ERROR] webhook delivery failed endpoint=hooks.acme.example.com: SSLError certif`..

Services affected: notification-svc, billing-svc, api-gateway.
