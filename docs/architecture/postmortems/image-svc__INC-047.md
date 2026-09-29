# Postmortem INC-047: Missing resources on cloud-api

Published: 2026-05-21T19:19:53.055405+00:00

Date: 2026-05-18T19:15 UTC. Severity: SEV-1. Alert: cloud-api: 4XX rate > 5 % for 10 min on cloud-api.

Impact: Operations on older resources failed on cloud-api.

Root cause: Terraform change narrowed the image-svc task role to s3:ListBucket; reads of base images from S3 failed with AccessDenied and cloud-api could not find base files.

Breaking change: infra/image-svc.tf:118 (aws_iam_policy_document.image_svc). infra/image-svc.tf:118 sets the policy actions s3:GetObject,s3:ListBucket -> s3:ListBucket.

How it was fixed: Restore actions = [s3:GetObject,s3:ListBucket] on aws_iam_policy_document.image_svc; test IAM changes with the IAM policy simulator. Mitigated at 2026-05-18T23:45 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `Unknown base file: /var/lib/nova/instances/_base/<HEX>`..

Services affected: image-svc, cloud-api, api-gateway.
