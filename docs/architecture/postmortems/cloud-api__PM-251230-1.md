# Postmortem PM-251230-1: cloud-api

Published: 2026-01-02T07:32:28.500383+00:00

Date: 2025-12-30.

Symptom: 5xx on /servers after a deploy.

Root cause: N+1 queries in ServersController.detail exhausted the DB pool.

How it was fixed: rolled back, added eager loading, raised db.pool.max_size temporarily.

Lesson: check config and pools of cloud-api and its dependencies first.
