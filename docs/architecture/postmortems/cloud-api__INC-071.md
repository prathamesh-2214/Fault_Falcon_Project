# Postmortem INC-071: Processing errors on billing-svc

Published: 2026-07-01T10:37:53.023878+00:00

Date: 2026-06-26T19:59 UTC. Severity: SEV-3. Alert: billing-svc: error-log rate > 20/min (CloudWatch Logs metric filter) on billing-svc.

Impact: Background processing failed; customer-visible data was delayed or wrong on billing-svc.

Root cause: Deploy v3.31.0 of cloud-api started publishing the v2 usage-event schema (FEAT-124) before billing-svc could read it; billing-svc rejected every event and the usage queue backed up into the DLQ.

Breaking change: cloud-api/nova/usage/publisher.py:128 (UsagePublisher.publish). cloud-api/nova/usage/publisher.py:128 (UsagePublisher.publish) replaces the instance_type field with a nested flavor object.

How it was fixed: Roll back cloud-api v3.31.0 -> v3.30.0; redrive the DLQ after billing-svc accepts both schemas; deploy consumers before producers. Mitigated at 2026-06-27T00:38 UTC.

Action items: Add a regression test covering UsagePublisher.publish (cloud-api/nova/usage/publisher.py).; Alert on the error signature `c.f.b.consumer.UsageEventConsumer - message <*> returned to queue (receive <*>`..

Services affected: cloud-api, usage-queue, billing-svc, notification-svc.
