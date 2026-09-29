# Postmortem PM-260119-1: storage-svc

Published: 2026-01-22T02:32:32.251677+00:00

Date: 2026-01-19.

Symptom: exception while serving blocks.

Root cause: EBS burst balance exhausted on a gp2 volume.

How it was fixed: migrated to gp3 with 250 MB/s throughput, alarm on BurstBalance.

Lesson: check recent changes of storage-svc and its dependencies first.
