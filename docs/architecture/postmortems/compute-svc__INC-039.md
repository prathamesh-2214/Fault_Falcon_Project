# Postmortem INC-039: Elevated 5xx errors on compute-svc

Published: 2026-05-09T06:29:34.724930+00:00

Date: 2026-05-05T18:34 UTC. Severity: SEV-3. Alert: compute-svc: 5XX error rate > 2 % for 5 min on compute-svc.

Impact: A share of requests failed with 5xx on compute-svc.

Root cause: Config change cut the compute-svc JVM heap.

Breaking change: config/compute-svc.yaml:112 (jvm.heap_mb). config/compute-svc.yaml:112 sets jvm.heap_mb 6144 -> 1536.

How it was fixed: Revert jvm.heap_mb to 6144 in config/compute-svc.yaml. Mitigated at 2026-05-05T22:25 UTC.

Action items: Add validation bounds and a canary for risky config keys (pool / timeout / retry / flags).; Alert on the error signature `java.lang.OutOfMemoryError: Java heap space`..

Services affected: compute-svc, cloud-api, api-gateway.
