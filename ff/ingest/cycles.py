"""Deployment cycles (weekly release trains) and the team's knowledge base.

A DevOps team resolving an incident knows more than the change log: which features each
release train shipped (now and in past cycles), in which order dependent services were
deployed, which hotfix patches and DB migrations went out, what the error rates did after
each deploy, what every file in the codebase does (owner, DB connections, config keys it
reads, what it calls), and how past incidents were fixed. This module models that:

* :data:`FEATURES`: the product feature catalog. A feature can span several services (e.g.
  "Usage events v2" touches cloud-api and billing-svc); each has a ticket id and sometimes a
  feature flag.
* :func:`cycle_id`: weekly release trains ``RT-26.10`` .. ``RT-26.35`` (ISO week of the start).
* :data:`LEVEL`: deploy order inside a train, callees first (hosts and data stores, then leaf
  services, then their callers, the edge last), as a platform team rolls out dependent services.
* :data:`FILE_INFO`: codebase knowledge per file (purpose, DB / connections, config keys read,
  calls to other services).
* Generators for the knowledge documents (``docs/architecture/``). Every document carries a
  ``Published:`` line; retrieval only sees documents published before the incident it is
  investigating (no document can reveal the answer to the incident it describes):

  - ``releases/<cycle>.md``: the train plan (features, services, deploy order), published when
    the train is cut;
  - ``changelog/<day>.md``: what actually shipped that day (deploys with versions and files,
    patches, migrations, config / infra changes, rollbacks, error rates), published at the end
    of the day;
  - ``codebase/<svc>.md``: what each file does (published at the start of the history);
  - ``postmortems/<INC-id>.md``: how each incident was fixed, published a few days after it;
    plus a handful of older postmortems from before the simulated history.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ff import config

D = timedelta(days=1)
H = timedelta(hours=1)

# callees first: hosts / data stores, then leaf services, then their callers, the edge last
LEVEL: dict[str, int] = {c.name: c.level for c in config.COMPONENTS.values()}
N_CYCLES = max(1, config.SIM_DAYS // 7)


@dataclass(frozen=True)
class Feature:
    ticket: str
    title: str
    services: tuple[str, ...]
    flag: str | None = None
    summary: str = ""


FEATURES: tuple[Feature, ...] = (
    Feature("FEAT-101", "Async server boot", ("cloud-api",), "feature.async_boot",
            "Boot servers asynchronously and return 202 immediately."),
    Feature("FEAT-102", "Paginated server listing", ("cloud-api",), None, "Cursor-based pagination for /servers."),
    Feature("FEAT-103", "Image cache warm-up", ("cloud-api", "image-svc"), None,
            "Pre-verify base images on host start; image-svc serves a manifest endpoint."),
    Feature("FEAT-104", "Bulk job submission API", ("cloud-api", "compute-svc"), None,
            "Submit up to 500 jobs per request; compute-svc accepts batches."),
    Feature("FEAT-105", "New task scheduler", ("compute-svc",), "feature.new_scheduler",
            "Capacity-aware slot assignment behind a flag."),
    Feature("FEAT-106", "Shuffle compression", ("compute-svc",), None, "LZ4 compression of map outputs."),
    Feature("FEAT-107", "Lease renewal batching", ("compute-svc", "storage-svc"), None,
            "Renew HDFS leases in batches instead of per file."),
    Feature("FEAT-108", "Faster block acks", ("storage-svc",), "feature.fast_ack", "Ack blocks before flush."),
    Feature("FEAT-109", "Block report throttling", ("storage-svc",), None, "Throttle full block reports."),
    Feature("FEAT-110", "Quorum metrics", ("coord-svc",), None, "Export quorum latency metrics."),
    Feature("FEAT-111", "Fast leader election", ("coord-svc",), "feature.fast_election", "Shorter election rounds."),
    Feature("FEAT-112", "Session expiry tuning", ("coord-svc", "compute-svc"), None,
            "Align client session timeouts with the quorum."),
    Feature("FEAT-113", "ECC scrubbing", ("node-svc",), "feature.ecc_scrub", "Background ECC memory scrubbing."),
    Feature("FEAT-114", "Lustre mount retries", ("node-svc", "storage-svc"), None, "Retry Lustre mounts on boot."),
    Feature("FEAT-115", "Job-level audit trail", ("storage-svc", "compute-svc", "cloud-api"), None,
            "Every job action is recorded end to end: API -> scheduler -> block store."),
    Feature("FEAT-116", "Token refresh v2", ("auth-svc", "api-gateway"), "feature.token_v2",
            "Sliding refresh tokens; the gateway authorizer accepts both token versions."),
    Feature("FEAT-117", "Per-customer rate limits", ("api-gateway",), None,
            "Usage plans per customer tier instead of one global limit."),
    Feature("FEAT-118", "Edge response caching", ("api-gateway",), "feature.edge_cache",
            "Cache GET /v1/images responses at the edge for 60 s."),
    Feature("FEAT-119", "SSO login (SAML)", ("auth-svc",), None, "SAML 2.0 login for enterprise accounts."),
    Feature("FEAT-120", "Session store sharding", ("auth-svc",), None, "Key sessions by account shard in Redis."),
    Feature("FEAT-121", "Lazy image pull", ("image-svc", "cloud-api"), "feature.lazy_pull",
            "Stream image layers on demand instead of downloading whole images."),
    Feature("FEAT-122", "Image checksum verification", ("image-svc",), None,
            "Verify SHA-256 of every image read from S3."),
    Feature("FEAT-123", "Cross-region image replication", ("image-svc",), None, "Replicate base images to eu-west-1."),
    Feature("FEAT-124", "Usage events v2 schema", ("billing-svc", "cloud-api"), None,
            "Richer usage events (flavor, region, tags); the consumer must deploy before the producer."),
    Feature("FEAT-125", "Invoice PDF v2", ("notification-svc", "billing-svc"), "feature.invoice_v2",
            "New invoice layout; PDFs rendered by billing-svc, e-mailed by notification-svc."),
    Feature("FEAT-126", "Prorated billing", ("billing-svc",), None, "Prorate plan changes to the second."),
    Feature("FEAT-127", "Signed webhooks", ("notification-svc",), None, "HMAC-SHA256 signatures on customer webhooks."),
    Feature("FEAT-128", "Batch e-mail sending", ("notification-svc",), "feature.batch_send",
            "Send invoice e-mails in batches of 50 through SES."),
    Feature("FEAT-129", "Spot capacity for batch jobs", ("node-svc", "compute-svc"), None,
            "Run low-priority jobs on spot instances with graceful draining."),
    Feature("FEAT-130", "Job priorities", ("compute-svc", "cloud-api"), None,
            "Three job priority classes from the API down to the scheduler."),
    Feature("FEAT-131", "Account suspension flow", ("notification-svc", "billing-svc", "auth-svc"), None,
            "Suspend accounts with unpaid invoices: billing decides, auth enforces, notification informs."),
    Feature("FEAT-132", "Usage dashboards API", ("billing-svc", "api-gateway"), None,
            "Customers query daily usage through /v1/usage."),
    Feature("FEAT-133", "Server tags", ("cloud-api",), None, "Key/value tags on servers."),
    Feature("FEAT-134", "Block checksum scrubbing", ("storage-svc",), None, "Background block checksum verification."),
    Feature("FEAT-135", "Quorum TLS", ("coord-svc",), None, "TLS between quorum peers."),
)
FEATURE_BY_TICKET = {f.ticket: f for f in FEATURES}

OWNERS = {c.name: c.team for c in config.COMPONENTS.values()}

# path -> (purpose, DB / connections, config keys read, calls)
FILE_INFO: dict[str, tuple[str, str, tuple[str, ...], str]] = {
    "cloud-api/nova/api/servers.py": ("REST handlers for /servers (list, detail, boot)", "meta-db (RDS) via the "
                                      "SQLAlchemy pool", ("db.pool.max_size", "http.timeout_ms"), "compute-svc"),
    "cloud-api/nova/api/auth.py": ("Token validation middleware", "none", ("http.timeout_ms",), "-"),
    "cloud-api/nova/compute/client.py": ("HTTP client to compute-svc (timeouts, retries)", "none",
                                         ("http.timeout_ms", "retry.max", "retry.backoff_ms"), "compute-svc"),
    "cloud-api/nova/image/cache.py": ("Base-image cache: verification and eviction", "local disk; image-svc for "
                                      "misses", (), "image-svc"),
    "cloud-api/nova/usage/publisher.py": ("Publishes usage events (server hours, job runs) to SQS", "usage-queue (SQS)",
                                          ("retry.max",), "usage-queue"),
    "compute/scheduler/TaskRunner.java": ("Slot assignment and task launch", "meta-db: jobs table",
                                          ("feature.new_scheduler", "db.pool.max_size"), "node-svc"),
    "compute/rpc/RMClient.java": ("ResourceManager RPC client (allocate, retries, backoff)", "none",
                                  ("retry.max", "retry.backoff_ms", "http.timeout_ms"), "ResourceManager"),
    "compute/mapreduce/ShuffleHandler.java": ("Serves map outputs to reducers", "none", ("http.timeout_ms",), "-"),
    "compute/dfs/LeaseRenewer.java": ("Renews HDFS write leases", "none", ("retry.max",), "storage-svc"),
    "storage/datanode/BlockWriter.java": ("Writes blocks to the EBS data volume", "EBS gp3 data volume",
                                          ("dfs.replication",), "-"),
    "storage/datanode/DataXceiver.java": ("Streams blocks to clients", "EBS data volume", ("http.timeout_ms",), "-"),
    "storage/namenode/BlockManager.java": ("Block placement and replication", "none", ("dfs.replication",), "-"),
    "storage/datanode/PacketResponder.java": ("Acks write pipeline packets", "none", ("feature.fast_ack",), "-"),
    "coord/quorum/QuorumCnxManager.java": ("Peer connections on 2888/3888", "security group ingress rules",
                                           ("session.timeout_ms",), "coord peers"),
    "coord/quorum/FastLeaderElection.java": ("Leader election", "election port 3888", ("feature.fast_election",), "-"),
    "coord/server/NIOServerCnxn.java": ("Client connections on 2181", "none", ("session.timeout_ms",), "-"),
    "coord/session/SessionTracker.java": ("Session expiry", "none", ("session.timeout_ms",), "-"),
    "node/kernel/ras_handler.c": ("RAS event handling (hardware errors)", "none", (), "-"),
    "node/ciod/ciod_loader.c": ("Loads job program images", "instance memory", (), "-"),
    "node/mem/tlb_miss.c": ("TLB miss handler", "instance memory", (), "-"),
    "node/io/lustre_mount.c": ("Mounts Lustre / EBS scratch", "EBS scratch volume", ("http.timeout_ms",), "storage-svc"),
    "gateway/authorizer/handler.py": ("Lambda authorizer: validates bearer tokens with auth-svc", "none",
                                      ("http.timeout_ms", "retry.max"), "auth-svc"),
    "gateway/authorizer/jwks.py": ("Caches auth-svc signing keys (JWKS)", "none", ("http.timeout_ms",), "auth-svc"),
    "gateway/transforms/request_mapper.py": ("Maps public routes to internal targets", "none",
                                             ("breaker.failure_pct",), "cloud-api, billing-svc"),
    "gateway/throttle/usage_plans.py": ("Per-customer usage plans and throttling", "none", (), "-"),
    "auth/internal/token/issuer.go": ("Issues signed access tokens", "meta-db: accounts", ("feature.token_v2",), "-"),
    "auth/internal/session/store.go": ("Session store (read-through Redis cache)", "session-cache (Redis)",
                                       ("cache.session_ttl_s", "http.timeout_ms"), "session-cache"),
    "auth/internal/account/repo.go": ("Account lookups", "meta-db (RDS) via pgx pool",
                                      ("db.pool.max_size", "db.statement_timeout_ms"), "meta-db"),
    "auth/internal/token/refresh.go": ("Refresh-token rotation", "session-cache", ("feature.token_v2",), "-"),
    "images/internal/registry/s3.go": ("Reads base images from S3", "object-store (S3) via the AWS SDK",
                                       ("http.timeout_ms", "retry.max"), "object-store"),
    "images/internal/cache/lru.go": ("In-memory image manifest cache", "task memory", ("cache.image_mb",), "-"),
    "images/internal/api/handlers.go": ("HTTP handlers for /images", "none", (), "-"),
    "images/internal/verify/checksum.go": ("SHA-256 verification of image layers", "none", (), "-"),
    "billing/consumer/UsageEventConsumer.java": ("Consumes usage events from SQS", "usage-queue (SQS), billing-db",
                                                 ("consumer.concurrency", "db.pool.max_size"), "usage-queue"),
    "billing/invoice/InvoiceService.java": ("Builds invoices and PDFs", "billing-db", ("feature.invoice_v2",),
                                            "notification-svc"),
    "billing/repo/InvoiceRepository.java": ("JPA repository for invoices", "billing-db via HikariCP",
                                            ("db.pool.max_size", "db.statement_timeout_ms", "db.credentials_cache_s"),
                                            "billing-db"),
    "billing/client/NotificationClient.java": ("HTTP client to notification-svc", "none",
                                               ("http.timeout_ms", "retry.max"), "notification-svc"),
    "notify/handlers/invoice_email.py": ("Sends invoice e-mails through SES", "SES", ("feature.batch_send",), "-"),
    "notify/handlers/webhook.py": ("Delivers customer webhooks (with retries)", "none",
                                   ("webhook.max_retries", "http.timeout_ms"), "customer endpoints"),
    "notify/ses_client.py": ("Thin SES client (send rate, retries)", "SES", ("http.timeout_ms", "retry.max"), "SES"),
    "notify/templates/render.py": ("Renders e-mail templates", "none", (), "-"),
}


def cycle_start(n: int) -> datetime:
    return config.DAY0 + n * 7 * D


def cycle_id_for_start(start: datetime) -> str:
    iso = start.isocalendar()
    return f"RT-{iso[0] % 100}.{iso[1]:02d}"


def cycle_index(t: datetime) -> int:
    return max(0, min(N_CYCLES - 1, int((t - config.DAY0) / (7 * D))))


def cycle_id(t: datetime) -> str:
    """Release train an event belongs to (by time)."""
    return cycle_id_for_start(cycle_start(cycle_index(t)))


def plan_cycle(rng: random.Random, n: int) -> list[Feature]:
    """2-4 features for cycle ``n`` (deterministic with ``rng``)."""
    return rng.sample(FEATURES, rng.randint(2, 4))


def feature_ref(f: Feature) -> dict[str, Any]:
    return {"ticket": f.ticket, "title": f.title, "services": list(f.services), "flag": f.flag,
            "summary": f.summary}


# --------------------------------------------------------------------------- knowledge documents
def _t(x: str) -> datetime:
    return datetime.fromisoformat(x)


def published(t: datetime) -> str:
    return f"Published: {t.isoformat()}"


def _cds_by_event(world: Any) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for c in world.change_details:
        out.setdefault(c["event_id"], []).append(c)
    return out


def release_plans(world: Any) -> dict[str, str]:
    """cycle id -> the train plan (features, services, deploy order), published when the train is cut."""
    by_cycle: dict[str, list[dict[str, Any]]] = {}
    for e in world.sim_events():
        if e["event_type"] == "DEPLOY" and e["environment"] == "prod" and e["_sim"].get("role") == "background":
            by_cycle.setdefault(e["payload"].get("cycle") or cycle_id(_t(e["occurred_at"])), []).append(e)
    out = {}
    for n in range(N_CYCLES):
        cid = cycle_id(cycle_start(n))
        evs = sorted(by_cycle.get(cid, []), key=lambda e: e["occurred_at"])
        feats: dict[str, dict[str, Any]] = {}
        for e in evs:
            for f in e["payload"].get("features", []):
                feats.setdefault(f["ticket"], f)
        order = list(dict.fromkeys(e["service"] for e in sorted(evs, key=lambda e: (LEVEL[e["service"]], e["service"]))))
        lines = [f"# Release train {cid}", "", published(cycle_start(n)), "",
                 f"Train window: {cycle_start(n):%Y-%m-%d} to {cycle_start(n) + 6 * D:%Y-%m-%d}. Deploy order "
                 f"(callees first): {' -> '.join(order) or 'no service releases planned'}.", "",
                 "## Features in this train", ""]
        for t, f in sorted(feats.items()):
            flag = f" Behind flag {f['flag']}." if f.get("flag") else ""
            lines += [f"- {t} {f['title']} ({', '.join(f['services'])}): {f.get('summary', '')}{flag}", ""]
        if not feats:
            lines += ["- maintenance releases only (dependency bumps, logging)", ""]
        out[cid] = "\n".join(lines) + "\n"
    return out


def daily_changelog(world: Any) -> dict[str, str]:
    """'YYYY-MM-DD' -> what shipped to prod that day, published at the end of the day."""
    cds = _cds_by_event(world)
    by_day: dict[str, list[dict[str, Any]]] = {}
    for e in world.sim_events():
        if e["environment"] == "prod" and e["event_type"] in config.CHANGE_TYPES:
            by_day.setdefault(e["occurred_at"][:10], []).append(e)
    out = {}
    for day, evs in sorted(by_day.items()):
        evs.sort(key=lambda e: e["occurred_at"])
        end = datetime.fromisoformat(day + "T23:59:00+00:00")
        lines = [f"# Change log {day} ({cycle_id(end)})", "", published(end), ""]
        for e in evs:
            ch = cds.get(e["id"], [])
            p = e["payload"]
            head = f"- [{e['id']}] {e['occurred_at'][11:16]} {e['event_type']} {e['service']}"
            if e["event_type"] in ("DEPLOY", "PATCH", "ROLLBACK"):
                head += f" {e.get('version_from')} -> {e.get('version_to')}"
            tickets = ", ".join(p.get("tickets", [])) or p.get("change_request", {}).get("id", "")
            what = "; ".join(p.get("commit_messages", [])) or p.get("change_summary", "")
            params = "; ".join(f"{c['param_key']} {c['param_old']} -> {c['param_new']}" for c in ch if c.get("param_key"))
            files = ", ".join(sorted({c["file"] for c in ch if c.get("file") and not c.get("param_key")}))
            parts = [f"{head} ({tickets})" if tickets else head, what[:160]]
            if params:
                parts.append(f"parameters: {params}")
            if files:
                parts.append(f"files: {files}")
            if e["event_type"] in ("DEPLOY", "PATCH"):
                parts.append(f"error rate after deploy: {p.get('post_deploy') or 'no log coverage'}")
            if p.get("reason"):
                parts.append(f"reason: {p['reason']}")
            lines += [". ".join(x for x in parts if x) + ".", ""]
        out[day] = "\n".join(lines)
    return out


def codebase_guide(world: Any, svc: str) -> str:
    """docs/architecture/codebase/<svc>.md: what each file does, who owns it, what it connects to."""
    from ff.ingest import repo

    comp = config.COMPONENTS[svc]
    lines = [f"# Codebase: {svc}", "", published(config.DAY0), "",
             f"Owner: {OWNERS[svc]}. Runs on: {comp.aws}. Calls: {', '.join(config.dependencies(svc)) or 'none'}. "
             f"Host: {config.host_of(svc) or 'its own instances'}. Called by: {', '.join(config.callers(svc)) or 'none'}.",
             ""]
    files = [f for f, _, _ in repo.CODE_FILES.get(svc, [])]
    if svc in repo.BUILD_FILES:
        files.append(repo.BUILD_FILES[svc])
    files += ([f"config/{svc}.yaml"] if comp.lang else []) + [f"infra/{svc}.tf"]
    for f in files:
        purpose, db, keys, calls = FILE_INFO.get(f, (
            "Service configuration (pushed through AppConfig)" if f.endswith(".yaml") else
            "AWS resources (Terraform)" if f.endswith(".tf") else "Dependency manifest (library versions)",
            "-", (), "-"))
        lines += [f"- `{f}`: {purpose}. Connections: {db}. Reads config: {', '.join(keys) or '-'}. Calls: {calls}.", ""]
    if not comp.lang:
        from ff.ingest.repo import params_of

        lines += [f"Managed resource: no application code. Terraform keys: {', '.join(params_of(svc))}.", ""]
    return "\n".join(lines)


PAST_FAILURES: dict[str, list[tuple[str, str, str]]] = {
    # component -> (symptom, root cause, fix): incidents from BEFORE the simulated history
    "cloud-api": [("5xx on /servers after a deploy", "N+1 queries in ServersController.detail exhausted the DB pool",
                   "rolled back, added eager loading, raised db.pool.max_size temporarily")],
    "compute-svc": [("ERROR IN CONTACTING RM storm", "retry.max raised without backoff",
                     "reverted retry.max, restored exponential backoff in RMClient")],
    "storage-svc": [("exception while serving blocks", "EBS burst balance exhausted on a gp2 volume",
                     "migrated to gp3 with 250 MB/s throughput, alarm on BurstBalance")],
    "coord-svc": [("Connection broken between quorum peers", "security group change dropped port 3888",
                   "restored the ingress rule, added a port connectivity check to CI")],
    "node-svc": [("ciod: Error loading program image", "Lustre mount failed after a kernel patch",
                  "rolled back the kernel patch, added mount retries (FEAT-114)")],
    "api-gateway": [("429 Too Many Requests for every customer", "a usage plan was attached to the wrong stage",
                     "re-attached the usage plan, added a synthetic canary per stage")],
    "auth-svc": [("401 on token refresh", "Redis evicted session keys after a memory spike",
                  "moved sessions to a dedicated node group, alert on evicted_keys")],
    "billing-svc": [("usage events piling up in the DLQ", "consumer threw on an unknown field after a producer "
                     "deploy", "made the consumer tolerant to unknown fields, deploy consumers before producers")],
    "meta-db": [("connection spikes and 'too many clients'", "a new service fleet opened 40 connections per task",
                 "introduced RDS Proxy for the batch fleet, capped pools per task")],
    "session-cache": [("latency on every login", "cluster was running on burstable nodes out of CPU credits",
                       "moved to cache.r6g.large")],
}


def past_postmortems(rng: random.Random) -> dict[str, str]:
    """{filename: markdown} of incidents from before the simulated history."""
    out = {}
    for svc, items in PAST_FAILURES.items():
        for k, (symptom, cause, fix) in enumerate(items):
            day = config.DAY0 - rng.uniform(20, 120) * D
            pid = f"PM-{day:%y%m%d}-{k + 1}"
            out[f"{svc}__{pid}.md"] = (
                f"# Postmortem {pid}: {svc}\n\n{published(day + 3 * D)}\n\nDate: {day:%Y-%m-%d}.\n\n"
                f"Symptom: {symptom}.\n\nRoot cause: {cause}.\n\nHow it was fixed: {fix}.\n\n"
                f"Lesson: check {('config and pools' if 'pool' in cause or 'retry' in cause else 'recent changes')} "
                f"of {svc} and its dependencies first.\n")
    return out


def incident_postmortems(world: Any, rng: random.Random) -> dict[str, str]:
    """{filename: markdown}: one postmortem per incident of the simulated history, published 2-5 days
    after it was mitigated. It states what happened and how it was fixed (retrieval never shows a
    postmortem to an investigation that starts before it was published)."""
    out = {}
    for inc in world.incidents:
        mitigated = _t(inc.get("mitigated_at") or inc["incident_start"]) + H
        pub = mitigated + rng.uniform(2, 5) * D
        if pub > config.SIM_END:
            continue
        rca = inc.get("rca") or {}
        culprit = inc.get("culprit") or {}
        lines = [f"# Postmortem {inc['id']}: {inc.get('title') or inc['service']}", "", published(pub), "",
                 f"Date: {inc['incident_start'][:16]} UTC. Severity: {inc.get('severity', 'SEV-3')}. Alert: "
                 f"{(inc.get('detection') or {}).get('alert', inc['service'])} on {inc['service']}.", "",
                 f"Impact: {inc.get('impact') or 'degraded requests'}.", "",
                 f"Root cause: {rca.get('macro') or 'not determined'}", ""]
        if culprit:
            lines += [f"Breaking change: {culprit.get('file')}:{culprit.get('line')} ({culprit.get('symbol')}). "
                      f"{rca.get('micro', '')}", ""]
        lines += [f"How it was fixed: {rca.get('remediation') or 'see timeline'} Mitigated at "
                  f"{(inc.get('mitigated_at') or '')[:16]} UTC.", ""]
        if inc.get("action_items"):
            lines += ["Action items: " + "; ".join(inc["action_items"]) + ".", ""]
        lines += [f"Services affected: {', '.join(inc.get('blast_radius') or [inc['service']])}.", ""]
        svc = inc.get("cause_service") or inc["service"]
        out[f"{svc}__{inc['id']}.md"] = "\n".join(lines)
    return out
