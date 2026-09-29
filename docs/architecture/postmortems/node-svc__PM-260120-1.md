# Postmortem PM-260120-1: node-svc

Published: 2026-01-23T16:35:58.336513+00:00

Date: 2026-01-20.

Symptom: ciod: Error loading program image.

Root cause: Lustre mount failed after a kernel patch.

How it was fixed: rolled back the kernel patch, added mount retries (FEAT-114).

Lesson: check recent changes of node-svc and its dependencies first.
