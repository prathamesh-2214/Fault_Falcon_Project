# Postmortem INC-084: Elevated 5xx errors on api-gateway

Published: 2026-07-21T05:49:17.408190+00:00

Date: 2026-07-18T08:14 UTC. Severity: SEV-2. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: A share of requests failed with 5xx on api-gateway.

Root cause: Deploy v4.36.0 of billing-svc added an unbounded in-memory structure; memory grew for hours until tasks were OOM-killed and restarted in a loop.

Breaking change: billing/consumer/UsageEventConsumer.java:333 (UsageEventConsumer.onMessage). billing/consumer/UsageEventConsumer.java:333 (UsageEventConsumer.onMessage) adds entries that are never evicted.

How it was fixed: Roll back billing-svc v4.36.0 -> v4.35.1; bound or remove the structure at billing/consumer/UsageEventConsumer.java:333; alert on memory growth per task. Mitigated at 2026-07-18T11:54 UTC.

Action items: Add a regression test covering UsageEventConsumer.onMessage (billing/consumer/UsageEventConsumer.java).; Alert on the error signature `GET <*> <*> <*> target=billing-svc`..

Services affected: billing-svc, api-gateway.
