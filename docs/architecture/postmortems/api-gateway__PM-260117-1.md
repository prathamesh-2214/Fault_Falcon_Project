# Postmortem PM-260117-1: api-gateway

Published: 2026-01-20T09:37:04.115455+00:00

Date: 2026-01-17.

Symptom: 429 Too Many Requests for every customer.

Root cause: a usage plan was attached to the wrong stage.

How it was fixed: re-attached the usage plan, added a synthetic canary per stage.

Lesson: check recent changes of api-gateway and its dependencies first.
