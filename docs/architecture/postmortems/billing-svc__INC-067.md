# Postmortem INC-067: Latency and timeouts on billing-svc

Published: 2026-06-23T13:57:42.498317+00:00

Date: 2026-06-20T10:58 UTC. Severity: SEV-1. Alert: billing-svc: p99 latency > 2 s for 5 min on billing-svc.

Impact: Requests slowed down and a share of them timed out on billing-svc.

Root cause: Deploy v4.28.0 of billing-svc replaced a join with one query per row (N+1); billing-db CPU saturated under peak traffic and requests timed out.

Breaking change: billing/invoice/InvoiceService.java:298 (InvoiceService.generate). billing/invoice/InvoiceService.java:298 (InvoiceService.generate) queries the database inside the loop.

How it was fixed: Roll back billing-svc v4.28.0 -> v4.27.0; restore the eager join at billing/invoice/InvoiceService.java:298; add a query-count assertion to the integration tests. Mitigated at 2026-06-20T15:29 UTC.

Action items: Add a regression test covering InvoiceService.generate (billing/invoice/InvoiceService.java).; Alert on the error signature `slow request: <*> ms, <*> SQL queries`..

Services affected: billing-db, billing-svc, api-gateway.
