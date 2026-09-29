# Codebase: billing-svc

Published: 2026-03-02T00:00:00+00:00

Owner: team-billing. Runs on: ECS Fargate (Java, Spring Boot). Calls: billing-db, usage-queue, notification-svc. Host: its own instances. Called by: api-gateway.

- `billing/consumer/UsageEventConsumer.java`: Consumes usage events from SQS. Connections: usage-queue (SQS), billing-db. Reads config: consumer.concurrency, db.pool.max_size. Calls: usage-queue.

- `billing/invoice/InvoiceService.java`: Builds invoices and PDFs. Connections: billing-db. Reads config: feature.invoice_v2. Calls: notification-svc.

- `billing/repo/InvoiceRepository.java`: JPA repository for invoices. Connections: billing-db via HikariCP. Reads config: db.pool.max_size, db.statement_timeout_ms, db.credentials_cache_s. Calls: billing-db.

- `billing/client/NotificationClient.java`: HTTP client to notification-svc. Connections: none. Reads config: http.timeout_ms, retry.max. Calls: notification-svc.

- `billing/pom.xml`: Dependency manifest (library versions). Connections: -. Reads config: -. Calls: -.

- `config/billing-svc.yaml`: Service configuration (pushed through AppConfig). Connections: -. Reads config: -. Calls: -.

- `infra/billing-svc.tf`: AWS resources (Terraform). Connections: -. Reads config: -. Calls: -.
