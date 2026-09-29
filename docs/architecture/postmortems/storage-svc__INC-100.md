# Postmortem INC-100: Elevated 5xx errors on api-gateway

Published: 2026-08-17T22:20:58.836470+00:00

Date: 2026-08-13T14:29 UTC. Severity: SEV-2. Alert: api-gateway: 5XX error rate > 2 % for 5 min on api-gateway.

Impact: A share of requests failed with 5xx on api-gateway.

Root cause: Config change turned feature.fast_ack on, activating a code path shipped days earlier.

Breaking change: storage/datanode/PacketResponder.java:374 (PacketResponder.run). storage/datanode/PacketResponder.java:374 (PacketResponder.run), deployed in evt_4220, runs only when the flag is on and skips the safety check of the old path.

How it was fixed: Turn feature.fast_ack off again in config/storage-svc.yaml; fix the flag-guarded branch at storage/datanode/PacketResponder.java:374 before re-enabling. Mitigated at 2026-08-13T17:17 UTC.

Action items: Add validation bounds and a canary for risky config keys (pool / timeout / retry / flags).; Add a regression test covering PacketResponder.run (storage/datanode/PacketResponder.java).; Alert on the error signature `GET <*> <*> <*> target=cloud-api`..

Services affected: storage-svc, compute-svc, cloud-api, api-gateway.
