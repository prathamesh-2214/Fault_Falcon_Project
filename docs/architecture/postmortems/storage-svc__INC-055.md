# Postmortem INC-055: Latency and timeouts on api-gateway

Published: 2026-06-05T20:26:26.543422+00:00

Date: 2026-06-01T01:11 UTC. Severity: SEV-1. Alert: api-gateway: p99 latency > 2 s for 5 min on api-gateway.

Impact: Requests slowed down and a share of them timed out on api-gateway.

Root cause: Deploy v4.7.0 of storage-svc introduced a regression that only the skipped ppd load suite would have caught.

Breaking change: storage/datanode/DataXceiver.java:47 (DataXceiver.readBlock). storage/datanode/DataXceiver.java:47 (DataXceiver.readBlock) changes the condition / constant shown in the diff.

How it was fixed: Roll back storage-svc v4.7.0 -> v4.6.0; revert storage/datanode/DataXceiver.java:47. Make the ppd load suite mandatory. Mitigated at 2026-06-01T03:51 UTC.

Action items: Make the ppd load test suite mandatory before prod.; Add a regression test covering DataXceiver.readBlock (storage/datanode/DataXceiver.java).; Alert on the error signature `GET <*> <*> <*> target=cloud-api`..

Services affected: storage-svc, compute-svc, cloud-api, api-gateway.
