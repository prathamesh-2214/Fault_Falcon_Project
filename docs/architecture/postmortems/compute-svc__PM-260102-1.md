# Postmortem PM-260102-1: compute-svc

Published: 2026-01-05T13:48:25.369137+00:00

Date: 2026-01-02.

Symptom: ERROR IN CONTACTING RM storm.

Root cause: retry.max raised without backoff.

How it was fixed: reverted retry.max, restored exponential backoff in RMClient.

Lesson: check config and pools of compute-svc and its dependencies first.
