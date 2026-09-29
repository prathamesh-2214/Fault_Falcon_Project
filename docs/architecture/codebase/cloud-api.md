# Codebase: cloud-api

Published: 2026-03-02T00:00:00+00:00

Owner: team-api. Runs on: EC2 (OpenStack Nova API, Python). Calls: compute-svc, image-svc, meta-db, usage-queue. Host: its own instances. Called by: api-gateway.

- `cloud-api/nova/api/servers.py`: REST handlers for /servers (list, detail, boot). Connections: meta-db (RDS) via the SQLAlchemy pool. Reads config: db.pool.max_size, http.timeout_ms. Calls: compute-svc.

- `cloud-api/nova/api/auth.py`: Token validation middleware. Connections: none. Reads config: http.timeout_ms. Calls: -.

- `cloud-api/nova/compute/client.py`: HTTP client to compute-svc (timeouts, retries). Connections: none. Reads config: http.timeout_ms, retry.max, retry.backoff_ms. Calls: compute-svc.

- `cloud-api/nova/image/cache.py`: Base-image cache: verification and eviction. Connections: local disk; image-svc for misses. Reads config: -. Calls: image-svc.

- `cloud-api/nova/usage/publisher.py`: Publishes usage events (server hours, job runs) to SQS. Connections: usage-queue (SQS). Reads config: retry.max. Calls: usage-queue.

- `cloud-api/requirements.txt`: Dependency manifest (library versions). Connections: -. Reads config: -. Calls: -.

- `config/cloud-api.yaml`: Service configuration (pushed through AppConfig). Connections: -. Reads config: -. Calls: -.

- `infra/cloud-api.tf`: AWS resources (Terraform). Connections: -. Reads config: -. Calls: -.
