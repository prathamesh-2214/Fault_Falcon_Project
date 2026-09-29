# Postmortem INC-015: Missing resources on api-gateway

Published: 2026-03-29T22:14:03.953786+00:00

Date: 2026-03-27T14:52 UTC. Severity: SEV-2. Alert: api-gateway: 4XX rate > 5 % for 10 min on api-gateway.

Impact: Operations on older resources failed on api-gateway.

Root cause: Terraform change narrowed the image-svc task role to s3:ListBucket; reads of base images from S3 failed with AccessDenied and cloud-api could not find base files.

Breaking change: infra/image-svc.tf:118 (aws_iam_policy_document.image_svc). infra/image-svc.tf:118 sets the policy actions s3:GetObject,s3:ListBucket -> s3:ListBucket.

How it was fixed: Restore actions = [s3:GetObject,s3:ListBucket] on aws_iam_policy_document.image_svc; test IAM changes with the IAM policy simulator. Mitigated at 2026-03-27T19:20 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `GET <*> <*> <*> target=cloud-api`..

Services affected: image-svc, cloud-api, api-gateway.
