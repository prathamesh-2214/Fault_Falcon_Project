# Postmortem INC-072: Elevated 5xx errors on compute-svc

Published: 2026-07-01T15:16:25.333398+00:00

Date: 2026-06-28T18:49 UTC. Severity: SEV-1. Alert: compute-svc: 5XX error rate > 2 % for 5 min on compute-svc.

Impact: A share of requests failed with 5xx on compute-svc.

Root cause: Deploy v5.14.0 of node-svc leaks one program-image buffer per failed load; the smaller instance type from the earlier launch-template change ran out of memory first.

Breaking change: node/ciod/ciod_loader.c:170 (ciod_load_program). node/ciod/ciod_loader.c:170 (ciod_load_program) drops free(image_buf) on the error path.

How it was fixed: Roll back node-svc v5.14.0 -> v5.13.0; restore free(image_buf) at node/ciod/ciod_loader.c:170; add a leak check (valgrind) to the ppd suite. Mitigated at 2026-06-28T21:46 UTC.

Action items: Add a regression test covering ciod_load_program (node/ciod/ciod_loader.c).; Alert on the error signature `Call to <*> failed: HTTP 503 Service Unavailable`..

Services affected: node-svc, compute-svc, storage-svc, cloud-api, api-gateway.
