# Postmortem INC-092: Throttling (429) on api-gateway

Published: 2026-08-04T05:06:29.128506+00:00

Date: 2026-07-31T12:40 UTC. Severity: SEV-1. Alert: api-gateway: 429 rate > 1 % for 5 min on api-gateway.

Impact: Customers were throttled on api-gateway.

Root cause: Terraform change cut the API Gateway usage-plan apigw_burst_limit 5000 -> 500; traffic at the morning peak got 429 Too Many Requests.

Breaking change: infra/api-gateway.tf:6 (aws_api_gateway_usage_plan.api_gateway). infra/api-gateway.tf:6 sets burst_limit 5000 -> 500 on aws_api_gateway_usage_plan.api_gateway.

How it was fixed: Restore burst_limit = 5000 on aws_api_gateway_usage_plan.api_gateway. Mitigated at 2026-07-31T17:26 UTC.

Action items: Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.; Alert on the error signature `GET /v1/servers <*> <*> <*>`..

Services affected: api-gateway.
