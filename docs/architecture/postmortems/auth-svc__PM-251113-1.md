# Postmortem PM-251113-1: auth-svc

Published: 2025-11-16T18:02:43.844861+00:00

Date: 2025-11-13.

Symptom: 401 on token refresh.

Root cause: Redis evicted session keys after a memory spike.

How it was fixed: moved sessions to a dedicated node group, alert on evicted_keys.

Lesson: check recent changes of auth-svc and its dependencies first.
