# Architecture patterns

Published: 2026-03-02T00:00:00+00:00

How the platform is wired, and what each pattern means when something fails:

- Edge: Amazon API Gateway + ALB in front of every public route; a Lambda authorizer validates tokens with auth-svc.
  Throttling happens here (usage plans). A 5xx at the edge names its target service in the access log.
- Synchronous calls only where a request needs an answer (edge -> services, cloud-api -> compute-svc / image-svc).
  Every call has a timeout, bounded retries with exponential backoff and jitter, and a circuit breaker; a failing
  callee shows up as timeouts / 5xx in its callers, so follow the dependency chain downwards.
- Asynchronous metering: cloud-api publishes usage events to SQS (usage-queue); billing-svc consumes them. Producer and
  consumer are decoupled, so a schema change must deploy the consumer first; failures surface in the consumer and the
  DLQ, not in the producer.
- One database per bounded context: meta-db (accounts, servers, jobs) and billing-db (usage, invoices) on RDS
  PostgreSQL. Services keep connection pools; pool sizes x task counts must stay below max_connections.
- Read-through cache: auth-svc keeps sessions in ElastiCache Redis in front of meta-db; a cold or shrunken cache moves
  the load to the database.
- Stateful services (compute-svc, storage-svc) run on the node-svc EC2 fleet; hardware and capacity problems of the
  fleet surface in them.
- Changes: code ships in weekly release trains, callees first; hotfixes go straight to prod; config through
  AppConfig; infrastructure through Terraform. Every change has a change request with a rollback plan.
