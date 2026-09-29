# Postmortem INC-077: Throttling (429) on api-gateway

Published: 2026-07-11T01:47:45.917076+00:00

Date: 2026-07-06T15:26 UTC. Severity: SEV-2. Alert: api-gateway: 429 rate > 1 % for 5 min on api-gateway.

Impact: Customers were throttled on api-gateway.

Root cause: Terraform change cut the API Gateway usage-plan apigw_burst_limit 5000 -> 500; traffic at the morning peak got 429 Too Many Requests.

Breaking change: infra/api-gateway.tf:6 (aws_api_gateway_usage_plan.api_gateway). infra/api-gateway.tf:6 sets burst_limit 5000 -> 500 on aws_api_gateway_usage_plan.api_gateway.

How it was fixed: Restore burst_limit = 5000 on aws_api_gateway_usage_plan.api_gateway. Mitigated at 2026-07-06T19:36 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `GET /v1/servers <*> <*> <*>`..

Services affected: api-gateway.
