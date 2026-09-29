# Postmortem INC-089: Elevated 5xx errors on cloud-api

Published: 2026-07-29T18:47:11.015399+00:00

Date: 2026-07-26T16:09 UTC. Severity: SEV-1. Alert: cloud-api: 5XX error rate > 2 % for 5 min on cloud-api.

Impact: A share of requests failed with 5xx on cloud-api.

Root cause: Deploy v1.17.0 of image-svc was a routine dependency bump; the new S3 client sends checksum headers the bucket policy denies.

Breaking change: images/go.mod:310 (go.mod). images/go.mod:310 upgrades the library: the new S3 client sends checksum headers the bucket policy denies.

How it was fixed: Roll back image-svc v1.17.0 -> v1.16.0; pin the previous version in images/go.mod until the upgrade is tested against the real dependency. Mitigated at 2026-07-26T21:02 UTC.

Action items: Add a regression test covering go.mod (images/go.mod).; Alert on the error signature `<*> <*> <*> HTTP/1.1" status: <*> len: <*> time: <*>`..

Services affected: image-svc, cloud-api, api-gateway.
