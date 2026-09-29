# Codebase: coord-svc

Published: 2026-03-02T00:00:00+00:00

Owner: team-platform. Runs on: EC2, 3-node ZooKeeper quorum. Calls: none. Host: its own instances. Called by: compute-svc.

- `coord/quorum/QuorumCnxManager.java`: Peer connections on 2888/3888. Connections: security group ingress rules. Reads config: session.timeout_ms. Calls: coord peers.

- `coord/quorum/FastLeaderElection.java`: Leader election. Connections: election port 3888. Reads config: feature.fast_election. Calls: -.

- `coord/server/NIOServerCnxn.java`: Client connections on 2181. Connections: none. Reads config: session.timeout_ms. Calls: -.

- `coord/session/SessionTracker.java`: Session expiry. Connections: none. Reads config: session.timeout_ms. Calls: -.

- `coord/pom.xml`: Dependency manifest (library versions). Connections: -. Reads config: -. Calls: -.

- `config/coord-svc.yaml`: Service configuration (pushed through AppConfig). Connections: -. Reads config: -. Calls: -.

- `infra/coord-svc.tf`: AWS resources (Terraform). Connections: -. Reads config: -. Calls: -.
