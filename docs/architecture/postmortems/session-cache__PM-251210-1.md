# Postmortem PM-251210-1: session-cache

Published: 2025-12-13T22:40:47.786170+00:00

Date: 2025-12-10.

Symptom: latency on every login.

Root cause: cluster was running on burstable nodes out of CPU credits.

How it was fixed: moved to cache.r6g.large.

Lesson: check recent changes of session-cache and its dependencies first.
