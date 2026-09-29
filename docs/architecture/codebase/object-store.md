# Codebase: object-store

Published: 2026-03-02T00:00:00+00:00

Owner: team-platform. Runs on: Amazon S3 bucket (images, job artifacts). Calls: none. Host: its own instances. Called by: image-svc.

- `infra/object-store.tf`: AWS resources (Terraform). Connections: -. Reads config: -. Calls: -.

Managed resource: no application code. Terraform keys: s3_expiration_days, s3_request_metrics.
