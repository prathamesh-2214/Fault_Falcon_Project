# Codebase: node-svc

Published: 2026-03-02T00:00:00+00:00

Owner: team-hpc. Runs on: EC2 compute fleet (HPC nodes, C). Calls: none. Host: its own instances. Called by: none.

- `node/kernel/ras_handler.c`: RAS event handling (hardware errors). Connections: none. Reads config: -. Calls: -.

- `node/ciod/ciod_loader.c`: Loads job program images. Connections: instance memory. Reads config: -. Calls: -.

- `node/mem/tlb_miss.c`: TLB miss handler. Connections: instance memory. Reads config: -. Calls: -.

- `node/io/lustre_mount.c`: Mounts Lustre / EBS scratch. Connections: EBS scratch volume. Reads config: http.timeout_ms. Calls: storage-svc.

- `node/Makefile.deps`: Dependency manifest (library versions). Connections: -. Reads config: -. Calls: -.

- `config/node-svc.yaml`: Service configuration (pushed through AppConfig). Connections: -. Reads config: -. Calls: -.

- `infra/node-svc.tf`: AWS resources (Terraform). Connections: -. Reads config: -. Calls: -.
