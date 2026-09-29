# Postmortem PM-251213-1: billing-svc

Published: 2025-12-16T21:46:32.352326+00:00

Date: 2025-12-13.

Symptom: usage events piling up in the DLQ.

Root cause: consumer threw on an unknown field after a producer deploy.

How it was fixed: made the consumer tolerant to unknown fields, deploy consumers before producers.

Lesson: check recent changes of billing-svc and its dependencies first.
