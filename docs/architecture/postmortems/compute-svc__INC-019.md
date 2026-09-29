# Postmortem INC-019: Elevated 5xx errors on cloud-api

Published: 2026-04-06T09:47:36.878059+00:00

Date: 2026-04-03T10:25 UTC. Severity: SEV-3. Alert: cloud-api: 5XX error rate > 2 % for 5 min on cloud-api.

Impact: A share of requests failed with 5xx on cloud-api.

Root cause: Config change turned feature.new_scheduler on, activating a code path shipped days earlier.

Breaking change: compute/scheduler/TaskRunner.java:39 (TaskRunner.assignSlots). compute/scheduler/TaskRunner.java:39 (TaskRunner.assignSlots), deployed in evt_908, runs only when the flag is on and skips the safety check of the old path.

How it was fixed: Turn feature.new_scheduler off again in config/compute-svc.yaml; fix the flag-guarded branch at compute/scheduler/TaskRunner.java:39 before re-enabling. Mitigated at 2026-04-03T14:01 UTC.

Action items: Add validation bounds and a canary for risky config keys (pool / timeout / retry / flags).; Add a regression test covering TaskRunner.assignSlots (compute/scheduler/TaskRunner.java).; Alert on the error signature `<*> <*> <*> HTTP/1.1" status: <*> len: <*> time: <*>`..

Services affected: compute-svc, cloud-api, api-gateway.
