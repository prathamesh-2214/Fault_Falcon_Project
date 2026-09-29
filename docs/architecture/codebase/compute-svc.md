# Codebase: compute-svc

Published: 2026-03-02T00:00:00+00:00

Owner: team-batch. Runs on: EC2 Auto Scaling group (Hadoop YARN, Java). Calls: storage-svc, coord-svc, meta-db. Host: node-svc. Called by: cloud-api.

- `compute/scheduler/TaskRunner.java`: Slot assignment and task launch. Connections: meta-db: jobs table. Reads config: feature.new_scheduler, db.pool.max_size. Calls: node-svc.

- `compute/rpc/RMClient.java`: ResourceManager RPC client (allocate, retries, backoff). Connections: none. Reads config: retry.max, retry.backoff_ms, http.timeout_ms. Calls: ResourceManager.

- `compute/mapreduce/ShuffleHandler.java`: Serves map outputs to reducers. Connections: none. Reads config: http.timeout_ms. Calls: -.

- `compute/dfs/LeaseRenewer.java`: Renews HDFS write leases. Connections: none. Reads config: retry.max. Calls: storage-svc.

- `compute/pom.xml`: Dependency manifest (library versions). Connections: -. Reads config: -. Calls: -.

- `config/compute-svc.yaml`: Service configuration (pushed through AppConfig). Connections: -. Reads config: -. Calls: -.

- `infra/compute-svc.tf`: AWS resources (Terraform). Connections: -. Reads config: -. Calls: -.
