# Postmortem INC-022: Elevated 5xx errors on billing-svc

Published: 2026-04-13T06:22:39.779517+00:00

Date: 2026-04-08T04:19 UTC. Severity: SEV-3. Alert: billing-svc: 5XX error rate > 2 % for 5 min on billing-svc.

Impact: A share of requests failed with 5xx on billing-svc.

Root cause: Deploy v2.9.0 of notification-svc introduced a regression.

Breaking change: notify/ses_client.py:148 (SesClient.send). notify/ses_client.py:148 (SesClient.send) changes the condition / constant shown in the diff.

How it was fixed: Complete the rollback of notification-svc to v2.8.0 on all nodes; revert notify/ses_client.py:148. Mitigated at 2026-04-08T08:17 UTC.

Action items: Add a regression test covering SesClient.send (notify/ses_client.py).; Alert on the error signature `c.f.b.client.NotificationClient - call to notification-svc failed: HTTP 503 Serv`..

Services affected: notification-svc, billing-svc, api-gateway.
