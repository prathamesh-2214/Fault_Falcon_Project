# Codebase: storage-svc

Published: 2026-03-02T00:00:00+00:00

Owner: team-storage. Runs on: EC2 + EBS gp3 (HDFS, Java). Calls: none. Host: node-svc. Called by: compute-svc.

- `storage/datanode/BlockWriter.java`: Writes blocks to the EBS data volume. Connections: EBS gp3 data volume. Reads config: dfs.replication. Calls: -.

- `storage/datanode/DataXceiver.java`: Streams blocks to clients. Connections: EBS data volume. Reads config: http.timeout_ms. Calls: -.

- `storage/namenode/BlockManager.java`: Block placement and replication. Connections: none. Reads config: dfs.replication. Calls: -.

- `storage/datanode/PacketResponder.java`: Acks write pipeline packets. Connections: none. Reads config: feature.fast_ack. Calls: -.

- `storage/pom.xml`: Dependency manifest (library versions). Connections: -. Reads config: -. Calls: -.

- `config/storage-svc.yaml`: Service configuration (pushed through AppConfig). Connections: -. Reads config: -. Calls: -.

- `infra/storage-svc.tf`: AWS resources (Terraform). Connections: -. Reads config: -. Calls: -.
