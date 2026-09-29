# Postmortem PM-251213-1: coord-svc

Published: 2025-12-16T07:38:10.714111+00:00

Date: 2025-12-13.

Symptom: Connection broken between quorum peers.

Root cause: security group change dropped port 3888.

How it was fixed: restored the ingress rule, added a port connectivity check to CI.

Lesson: check recent changes of coord-svc and its dependencies first.
