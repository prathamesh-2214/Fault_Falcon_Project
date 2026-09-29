# Postmortem PM-260121-1: meta-db

Published: 2026-01-24T06:44:21.958580+00:00

Date: 2026-01-21.

Symptom: connection spikes and 'too many clients'.

Root cause: a new service fleet opened 40 connections per task.

How it was fixed: introduced RDS Proxy for the batch fleet, capped pools per task.

Lesson: check recent changes of meta-db and its dependencies first.
