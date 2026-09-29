# Codebase: image-svc

Published: 2026-03-02T00:00:00+00:00

Owner: team-api. Runs on: ECS Fargate (Go). Calls: object-store. Host: its own instances. Called by: cloud-api.

- `images/internal/registry/s3.go`: Reads base images from S3. Connections: object-store (S3) via the AWS SDK. Reads config: http.timeout_ms, retry.max. Calls: object-store.

- `images/internal/cache/lru.go`: In-memory image manifest cache. Connections: task memory. Reads config: cache.image_mb. Calls: -.

- `images/internal/api/handlers.go`: HTTP handlers for /images. Connections: none. Reads config: -. Calls: -.

- `images/internal/verify/checksum.go`: SHA-256 verification of image layers. Connections: none. Reads config: -. Calls: -.

- `images/go.mod`: Dependency manifest (library versions). Connections: -. Reads config: -. Calls: -.

- `config/image-svc.yaml`: Service configuration (pushed through AppConfig). Connections: -. Reads config: -. Calls: -.

- `infra/image-svc.tf`: AWS resources (Terraform). Connections: -. Reads config: -. Calls: -.
