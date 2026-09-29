# Codebase: notification-svc

Published: 2026-03-02T00:00:00+00:00

Owner: team-billing. Runs on: AWS Lambda (Python) + Amazon SES. Calls: none. Host: its own instances. Called by: billing-svc.

- `notify/handlers/invoice_email.py`: Sends invoice e-mails through SES. Connections: SES. Reads config: feature.batch_send. Calls: -.

- `notify/handlers/webhook.py`: Delivers customer webhooks (with retries). Connections: none. Reads config: webhook.max_retries, http.timeout_ms. Calls: customer endpoints.

- `notify/ses_client.py`: Thin SES client (send rate, retries). Connections: SES. Reads config: http.timeout_ms, retry.max. Calls: SES.

- `notify/templates/render.py`: Renders e-mail templates. Connections: none. Reads config: -. Calls: -.

- `notify/requirements.txt`: Dependency manifest (library versions). Connections: -. Reads config: -. Calls: -.

- `config/notification-svc.yaml`: Service configuration (pushed through AppConfig). Connections: -. Reads config: -. Calls: -.

- `infra/notification-svc.tf`: AWS resources (Terraform). Connections: -. Reads config: -. Calls: -.
