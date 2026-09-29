# Postmortem INC-037: Processing errors on billing-svc

Published: 2026-05-06T00:19:49.500846+00:00

Date: 2026-05-02T07:52 UTC. Severity: SEV-1. Alert: billing-svc: error-log rate > 20/min (CloudWatch Logs metric filter) on billing-svc.

Impact: Background processing failed; customer-visible data was delayed or wrong on billing-svc.

Root cause: Deploy v3.21.0 of cloud-api started publishing the v2 usage-event schema (FEAT-124) before billing-svc could read it; billing-svc rejected every event and the usage queue backed up into the DLQ.

Breaking change: cloud-api/nova/usage/publisher.py:240 (UsagePublisher.publish). cloud-api/nova/usage/publisher.py:240 (UsagePublisher.publish) replaces the instance_type field with a nested flavor object.

How it was fixed: Roll back cloud-api v3.21.0 -> v3.20.1; redrive the DLQ after billing-svc accepts both schemas; deploy consumers before producers. Mitigated at 2026-05-02T10:44 UTC.

Action items: Add a regression test covering UsagePublisher.publish (cloud-api/nova/usage/publisher.py).; Alert on the error signature `c.f.b.consumer.UsageEventConsumer - message <*> returned to queue (receive <*>`..

Services affected: cloud-api, usage-queue, billing-svc, notification-svc.
