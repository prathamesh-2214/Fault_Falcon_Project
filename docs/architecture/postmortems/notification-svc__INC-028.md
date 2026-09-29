# Postmortem INC-028: Throttling (429) on billing-svc

Published: 2026-04-21T23:40:55.095465+00:00

Date: 2026-04-17T19:20 UTC. Severity: SEV-2. Alert: billing-svc: 429 rate > 1 % for 5 min on billing-svc.

Impact: Customers were throttled on billing-svc.

Root cause: No change explains the anomaly: Amazon SES throttled the account after a bounce-rate review by AWS.

How it was fixed: No rollback needed; AWS support restored the sending quota; bounce handling was improved. Mitigated at 2026-04-17T22:03 UTC.

Action items: Alert on the error signature `c.f.b.client.NotificationClient - call to notification-svc failed: HTTP 429 Too `..

Services affected: notification-svc, billing-svc, api-gateway.
