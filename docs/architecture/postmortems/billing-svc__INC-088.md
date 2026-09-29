# Postmortem INC-088: Elevated 5xx errors on api-gateway

Published: 2026-07-28T05:46:20.009338+00:00

Date: 2026-07-24T15:59 UTC. Severity: SEV-3. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: A share of requests failed with 5xx on api-gateway.

Root cause: Deploy v4.38.0 of billing-svc added an unbounded in-memory structure; memory grew for hours until tasks were OOM-killed and restarted in a loop.

Breaking change: billing/consumer/UsageEventConsumer.java:72 (UsageEventConsumer.onMessage). billing/consumer/UsageEventConsumer.java:72 (UsageEventConsumer.onMessage) adds entries that are never evicted.

How it was fixed: Roll back billing-svc v4.38.0 -> v4.37.0; bound or remove the structure at billing/consumer/UsageEventConsumer.java:72; alert on memory growth per task. Mitigated at 2026-07-24T20:12 UTC.

Action items: Add a regression test covering UsageEventConsumer.onMessage (billing/consumer/UsageEventConsumer.java).; Alert on the error signature `GET <*> <*> <*> target=billing-svc`..

Services affected: billing-svc, api-gateway.
