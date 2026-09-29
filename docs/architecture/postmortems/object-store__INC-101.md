# Postmortem INC-101: Missing resources on cloud-api

Published: 2026-08-17T08:55:30.710617+00:00

Date: 2026-08-14T18:43 UTC. Severity: SEV-1. Alert: cloud-api: 4XX rate > 5 % for 10 min on cloud-api.

Impact: Operations on older resources failed on cloud-api.

Root cause: Terraform change shortened the images/ expiration 365 -> 30 days; S3 deleted older base images and servers booting from them failed.

Breaking change: infra/object-store.tf:56 (aws_s3_bucket_lifecycle_configuration.object_store). infra/object-store.tf:56 sets expiration days 365 -> 30 on aws_s3_bucket_lifecycle_configuration.object_store.

How it was fixed: Restore expiration = 365 days; restore the images from the replica bucket / versioning. Mitigated at 2026-08-14T21:56 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `Unknown base file: /var/lib/nova/instances/_base/<HEX>`..

Services affected: object-store, image-svc, cloud-api, api-gateway.
