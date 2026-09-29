# Topology

Published: 2026-03-02T00:00:00+00:00

| component | kind | runs on AWS | owner | calls | host |
| --- | --- | --- | --- | --- | --- |
| api-gateway | edge | Amazon API Gateway (REST) + Application Load Balancer, Lambda authorizer | team-edge | auth-svc, cloud-api, billing-svc | - |
| auth-svc | service | ECS Fargate (Go), 3 tasks behind an internal ALB | team-identity | session-cache, meta-db | - |
| cloud-api | service | EC2 (OpenStack Nova API, Python) | team-api | compute-svc, image-svc, meta-db, usage-queue | - |
| image-svc | service | ECS Fargate (Go) | team-api | object-store | - |
| compute-svc | service | EC2 Auto Scaling group (Hadoop YARN, Java) | team-batch | storage-svc, coord-svc, meta-db | node-svc |
| storage-svc | service | EC2 + EBS gp3 (HDFS, Java) | team-storage | - | node-svc |
| coord-svc | coordination | EC2, 3-node ZooKeeper quorum | team-platform | - | - |
| node-svc | host | EC2 compute fleet (HPC nodes, C) | team-hpc | - | - |
| billing-svc | service | ECS Fargate (Java, Spring Boot) | team-billing | billing-db, usage-queue, notification-svc | - |
| notification-svc | service | AWS Lambda (Python) + Amazon SES | team-billing | - | - |
| meta-db | datastore | Amazon RDS for PostgreSQL 15, Multi-AZ | team-platform | - | - |
| billing-db | datastore | Amazon RDS for PostgreSQL 15 | team-billing | - | - |
| session-cache | cache | Amazon ElastiCache for Redis 7 (cluster mode off) | team-identity | - | - |
| usage-queue | queue | Amazon SQS standard queue + DLQ | team-billing | - | - |
| object-store | objectstore | Amazon S3 bucket (images, job artifacts) | team-platform | - | - |

## Call graph

- api-gateway calls auth-svc: an anomaly in api-gateway can be caused by a change in auth-svc.
- api-gateway calls cloud-api: an anomaly in api-gateway can be caused by a change in cloud-api.
- api-gateway calls billing-svc: an anomaly in api-gateway can be caused by a change in billing-svc.
- auth-svc calls session-cache: an anomaly in auth-svc can be caused by a change in session-cache.
- auth-svc calls meta-db: an anomaly in auth-svc can be caused by a change in meta-db.
- cloud-api calls compute-svc: an anomaly in cloud-api can be caused by a change in compute-svc.
- cloud-api calls image-svc: an anomaly in cloud-api can be caused by a change in image-svc.
- cloud-api calls meta-db: an anomaly in cloud-api can be caused by a change in meta-db.
- cloud-api calls usage-queue: an anomaly in cloud-api can be caused by a change in usage-queue.
- image-svc calls object-store: an anomaly in image-svc can be caused by a change in object-store.
- compute-svc calls storage-svc: an anomaly in compute-svc can be caused by a change in storage-svc.
- compute-svc calls coord-svc: an anomaly in compute-svc can be caused by a change in coord-svc.
- compute-svc calls meta-db: an anomaly in compute-svc can be caused by a change in meta-db.
- billing-svc calls billing-db: an anomaly in billing-svc can be caused by a change in billing-db.
- billing-svc calls usage-queue: an anomaly in billing-svc can be caused by a change in usage-queue.
- billing-svc calls notification-svc: an anomaly in billing-svc can be caused by a change in notification-svc.
- compute-svc runs on node-svc: hardware, kernel or capacity problems of node-svc surface in compute-svc.
- storage-svc runs on node-svc: hardware, kernel or capacity problems of node-svc surface in storage-svc.
- cloud-api publishes to usage-queue, consumed by billing-svc: a change in cloud-api's events can break the consumers.
