# Postmortem INC-007: Elevated 5xx errors on node-svc

Published: 2026-03-17T06:55:18.147571+00:00

Date: 2026-03-14T15:35 UTC. Severity: SEV-1. Alert: node-svc: 5XX error rate > 2 % for 5 min on node-svc.

Impact: A share of requests failed with 5xx on node-svc.

Root cause: Deploy v5.2.0 of node-svc leaks one program-image buffer per failed load; the smaller instance type from the earlier launch-template change ran out of memory first.

Breaking change: node/ciod/ciod_loader.c:31 (ciod_load_program). node/ciod/ciod_loader.c:31 (ciod_load_program) drops free(image_buf) on the error path.

How it was fixed: Roll back node-svc v5.2.0 -> v5.1.1; restore free(image_buf) at node/ciod/ciod_loader.c:31; add a leak check (valgrind) to the ppd suite. Mitigated at 2026-03-14T19:28 UTC.

Action items: Add a regression test covering ciod_load_program (node/ciod/ciod_loader.c).; Alert on the error signature `ciod: Error loading <*> invalid or missing program image, No such file or direct`..

Services affected: node-svc, compute-svc, storage-svc, cloud-api, api-gateway.
