# Postmortem INC-091: Latency and timeouts on image-svc

Published: 2026-07-31T22:41:06.446218+00:00

Date: 2026-07-29T14:06 UTC. Severity: SEV-1. Alert: image-svc: p99 latency > 2 s for 5 min on image-svc.

Impact: Requests slowed down and a share of them timed out on image-svc.

Root cause: Deploy v1.19.0 of image-svc introduced a regression that only the skipped ppd load suite would have caught.

Breaking change: images/internal/verify/checksum.go:63 (VerifyChecksum). images/internal/verify/checksum.go:63 (VerifyChecksum) changes the condition / constant shown in the diff.

How it was fixed: Roll back image-svc v1.19.0 -> v1.18.0; revert images/internal/verify/checksum.go:63. Make the ppd load suite mandatory. Mitigated at 2026-07-29T17:47 UTC.

Action items: Make the ppd load test suite mandatory before prod.; Add a regression test covering VerifyChecksum (images/internal/verify/checksum.go).; Alert on the error signature `request took <*> ms (batch size 1)`..

Services affected: image-svc, cloud-api, api-gateway.
