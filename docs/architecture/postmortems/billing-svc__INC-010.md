# Postmortem INC-010: Elevated 5xx errors on api-gateway

Published: 2026-03-22T17:32:02.867070+00:00

Date: 2026-03-19T16:39 UTC. Severity: SEV-2. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: A share of requests failed with 5xx on api-gateway.

Root cause: Release train RT-26.12 shipped FEAT-132 (Usage dashboards API) across services; the billing-svc part (v4.5.0) broke, and the failure surfaced in api-gateway through the dependency chain. The later parts of the same feature were harmless.

Breaking change: billing/repo/InvoiceRepository.java:25 (InvoiceRepository.findOpenByAccount). billing/repo/InvoiceRepository.java:25 (InvoiceRepository.findOpenByAccount) fetches every invoice with all its lines for the usage API.

How it was fixed: Roll back billing-svc v4.5.0 -> v4.4.1 (the other services' parts of FEAT-132 can stay); fix billing/repo/InvoiceRepository.java:25; add a cross-service load test to the train. Mitigated at 2026-03-19T20:15 UTC.

Action items: Add a regression test covering InvoiceRepository.findOpenByAccount (billing/repo/InvoiceRepository.java).; Alert on the error signature `GET <*> <*> <*> target=billing-svc`..

Services affected: billing-svc, api-gateway.
