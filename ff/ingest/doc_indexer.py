"""The team's knowledge base: topology, patterns, runbooks, release plans, daily change logs,
codebase guides and postmortems, chunked and indexed incrementally.

The runbooks quote the simulator's LATEST config/infra state (``final_params``) and the log
baselines, so what the model reads matches the timeline. Every document carries a
``Published:`` time; each chunk stores it and retrieval only returns chunks published before
the incident under investigation. Files are chunked at about 250 tokens (runbooks, topology)
or 110 tokens (one entry); each file's content hash is stored in SQLite and only changed files
are re-indexed.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ff import config
from ff.config import Paths
from ff.store.sqlite_store import SqliteStore
from ff.store.vector_store import VectorStore, approx_tokens

CHUNK_TOKENS = 250

RUNBOOK_CHECKS: dict[str, str] = {
    "api-gateway": ("On 5xx: find the target in the access log (target=...) and follow that service. On 429: check the "
                    "usage-plan throttle_settings in infra/api-gateway.tf and per-customer traffic. On 504: compare the "
                    "ALB idle_timeout with the slowest routes (report exports). On 401 bursts: check auth-svc and the "
                    "authorizer's JWKS cache."),
    "auth-svc": ("On token failures: check session-cache (Redis) health first: evictions, maxmemory-policy, node type "
                 "and the security group on 6379; then cache.session_ttl_s and meta-db load. On 502 from the ALB: "
                 "check the target-group health check and ECS task memory."),
    "cloud-api": ("On latency spikes or 5xx: check compute-svc and image-svc health and their recent deploys, then "
                  "http.timeout_ms and db.pool.max_size in config/cloud-api.yaml and meta-db load. 'Unknown base file' "
                  "warnings come from the image cache (nova/image/cache.py) and image-svc / S3."),
    "image-svc": ("On image read failures: check S3 errors (403 AccessDenied -> IAM policy, 404 NoSuchKey -> lifecycle "
                  "rules, 503 SlowDown -> AWS health), then ECS task memory and the in-memory cache size."),
    "compute-svc": ("On task failures, 'ERROR IN CONTACTING RM' or lease-renewal failures: check RM connectivity "
                    "(rpc/RMClient.java), retry.max and http.timeout_ms, then storage-svc, coord-svc, meta-db and the "
                    "node-svc fleet (Auto Scaling max_size). Check jvm.heap_mb on memory errors."),
    "storage-svc": ("On 'exception while serving blk' or write failures: check EBS throughput / IOPS and disk_gb headroom "
                    "first (latent changes show up days later), recent datanode deploys (BlockWriter, DataXceiver), "
                    "then node-svc."),
    "coord-svc": ("On 'Connection broken' or election timeouts: check the security group (2888/3888), "
                  "session.timeout_ms, recent config changes and quorum membership (QuorumCnxManager)."),
    "node-svc": ("On KERNDTLB (TLB errors): check instance_type and memory. On KERNSTOR (data storage interrupt): "
                 "check disk_gb. On ciod load failures or Lustre mount failures: check recent kernel/ciod deploys."),
    "billing-svc": ("On consumer errors: look at the usage-queue (age of oldest message, DLQ depth) and the first "
                    "failing message: a parse error means a producer (cloud-api) changed the event schema; duplicate "
                    "keys mean the SQS visibility timeout is shorter than processing. On DB errors: billing-db "
                    "connections (consumer.concurrency x pool size vs max_connections) and Secrets Manager rotation."),
    "notification-svc": ("On throttling: Lambda reserved concurrency and SES sending quota. On webhook failures: "
                         "webhook.max_retries (retry storms) and the customer's endpoint (TLS certificate)."),
    "meta-db": ("On connection errors: max_connections in the parameter group vs the services' pools, recent "
                "maintenance (engine upgrades with apply_immediately). On slow queries: instance class, missing "
                "indexes (recent migrations), N+1 query patterns in recent deploys, lock waits during migrations."),
    "billing-db": ("Same checks as meta-db; also Secrets Manager rotation of the billing_app credentials."),
    "session-cache": ("On evictions or OOM: node type and maxmemory-policy; on connection timeouts: the security group "
                      "and the port."),
    "usage-queue": ("On a growing backlog: consumer errors in billing-svc. On DLQ growth: maxReceiveCount and consumer "
                    "failures. On duplicates: visibility_timeout_seconds vs processing time."),
    "object-store": ("On 403: IAM policies of the reading role. On 404: lifecycle expiration rules. On 503 SlowDown: "
                     "request rate and AWS health events."),
}

PATTERNS_MD = """# Architecture patterns

{published}

How the platform is wired, and what each pattern means when something fails:

- Edge: Amazon API Gateway + ALB in front of every public route; a Lambda authorizer validates tokens with auth-svc.
  Throttling happens here (usage plans). A 5xx at the edge names its target service in the access log.
- Synchronous calls only where a request needs an answer (edge -> services, cloud-api -> compute-svc / image-svc).
  Every call has a timeout, bounded retries with exponential backoff and jitter, and a circuit breaker; a failing
  callee shows up as timeouts / 5xx in its callers, so follow the dependency chain downwards.
- Asynchronous metering: cloud-api publishes usage events to SQS (usage-queue); billing-svc consumes them. Producer and
  consumer are decoupled, so a schema change must deploy the consumer first; failures surface in the consumer and the
  DLQ, not in the producer.
- One database per bounded context: meta-db (accounts, servers, jobs) and billing-db (usage, invoices) on RDS
  PostgreSQL. Services keep connection pools; pool sizes x task counts must stay below max_connections.
- Read-through cache: auth-svc keeps sessions in ElastiCache Redis in front of meta-db; a cold or shrunken cache moves
  the load to the database.
- Stateful services (compute-svc, storage-svc) run on the node-svc EC2 fleet; hardware and capacity problems of the
  fleet surface in them.
- Changes: code ships in weekly release trains, callees first; hotfixes go straight to prod; config through
  AppConfig; infrastructure through Terraform. Every change has a change request with a rollback plan.
"""


def topology_md() -> str:
    """docs/architecture/topology.md generated from config."""
    from ff.ingest.cycles import published

    lines = ["# Topology", "", published(config.DAY0), "",
             "| component | kind | runs on AWS | owner | calls | host |", "| --- | --- | --- | --- | --- | --- |"]
    for c in config.COMPONENTS.values():
        lines.append(f"| {c.name} | {c.kind} | {c.aws} | {c.team} | {', '.join(config.dependencies(c.name)) or '-'} "
                     f"| {config.host_of(c.name) or '-'} |")
    lines += ["", "## Call graph", ""]
    for svc, deps in config.TOPOLOGY_CALLS.items():
        for d in deps:
            lines.append(f"- {svc} calls {d}: an anomaly in {svc} can be caused by a change in {d}.")
    for svc, host in config.TOPOLOGY_HOSTS.items():
        lines.append(f"- {svc} runs on {host}: hardware, kernel or capacity problems of {host} surface in {svc}.")
    for q, prods in config.QUEUE_PRODUCERS.items():
        for p in prods:
            lines.append(f"- {p} publishes to {q}, consumed by {', '.join(config.QUEUE_CONSUMERS.get(q, ()))}: a "
                         f"change in {p}'s events can break the consumers.")
    lines.append("")
    return "\n".join(lines)


def runbook_md(svc: str, params: dict[str, Any], baseline: dict[str, Any] | None) -> str:
    """docs/architecture/runbooks/<svc>.md with the latest config values."""
    from ff.ingest import repo
    from ff.ingest.cycles import published

    comp = config.COMPONENTS[svc]
    lines = [f"# Runbook: {svc}", "", published(config.DAY0), "", f"{comp.purpose}. Runs on {comp.aws}; owned by "
             f"{comp.team}.", ""]
    cfg = [k for k in repo.params_of(svc) if repo.param_spec(k).kind == "config" and k in params]
    tf = [k for k in repo.params_of(svc) if repo.param_spec(k).kind == "infra" and k in params]
    if cfg:
        lines += [f"## Current configuration (config/{svc}.yaml)", ""] + [f"- {k}: {params[k]}" for k in cfg] + [""]
    if tf:
        lines += [f"## Infrastructure (infra/{svc}.tf)", ""] + [f"- {k}: {params[k]}" for k in tf] + [""]
    lines += ["## What to check on errors", "", RUNBOOK_CHECKS.get(svc, ""), ""]
    deps = config.dependencies(svc)
    host = config.host_of(svc)
    lines.append(f"Dependencies: {', '.join(deps) or 'none'}. Host: {host or 'none'}. Called by: "
                 f"{', '.join(config.callers(svc)) or 'none'}.")
    if baseline:
        band = baseline.get("error_ratio_band", [0, 0])
        lines += ["", "## Normal behaviour (from logs)", "",
                  f"- error ratio band: {band[0]:.2f} - {band[1]:.2f} (median {baseline.get('error_ratio_median', 0):.2f})"]
        if baseline.get("p95_latency_band"):
            lb = baseline["p95_latency_band"]
            lines.append(f"- p95 latency band: {lb[0]:.3f} - {lb[1]:.3f} s")
        if baseline.get("known_noise"):
            lines.append("- known noise: " + "; ".join(t[:80] for t in baseline["known_noise"][:5]))
    lines.append("")
    return "\n".join(lines)


KNOWLEDGE_DIRS = ("runbooks", "releases", "changelog", "codebase", "postmortems")


def knowledge_docs(params: dict[str, dict[str, Any]], baselines: dict[str, Any] | None = None,
                   world: Any | None = None, seed: int = config.SEED) -> dict[str, str]:
    """relative path -> markdown for every knowledge document.

    Always: topology, architecture patterns and one runbook per component. With a ``world``:
    release-train plans, daily change logs, a codebase guide per component, postmortems of past
    and simulated incidents (see ``ff/ingest/cycles.py``). Every document has a ``Published:`` line.
    """
    import random

    from ff.ingest import cycles

    docs = {"topology.md": topology_md(), "patterns.md": PATTERNS_MD.format(published=cycles.published(config.DAY0))}
    for svc in config.SERVICES:
        docs[f"runbooks/{svc}.md"] = runbook_md(svc, params.get(svc, {}), (baselines or {}).get(svc))
    if world is not None:
        for cid, text in cycles.release_plans(world).items():
            docs[f"releases/{cid}.md"] = text
        for day, text in cycles.daily_changelog(world).items():
            docs[f"changelog/{day}.md"] = text
        for svc in config.SERVICES:
            docs[f"codebase/{svc}.md"] = cycles.codebase_guide(world, svc)
        rng = random.Random(seed + 7)
        for name, text in {**cycles.past_postmortems(rng), **cycles.incident_postmortems(world, rng)}.items():
            docs[f"postmortems/{name}"] = text
    return docs


def write_docs(paths: Paths, params: dict[str, dict[str, Any]], store: SqliteStore | None = None,
               world: Any | None = None) -> list[Path]:
    """Write the knowledge documents under docs/architecture (stale generated files are removed)."""
    root = paths.docs_arch
    baselines = {svc: store.get_baseline(svc) for svc in config.SERVICES} if store else None
    docs = knowledge_docs(params, baselines, world)
    for d in KNOWLEDGE_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
        if world is None and d != "runbooks":
            continue
        for old in (root / d).glob("*.md"):
            if f"{d}/{old.name}" not in docs:
                old.unlink()
    out = []
    for rel, text in docs.items():
        p = root / rel
        p.write_text(text, encoding="utf-8")
        out.append(p)
    return out


_PUB = re.compile(r"^Published: (\S+)$", re.M)


def doc_published(text: str) -> datetime:
    """The document's ``Published:`` time (the start of the history when absent)."""
    m = _PUB.search(text)
    return datetime.fromisoformat(m.group(1)) if m else config.DAY0


def doc_meta(rel: str) -> tuple[str, str]:
    """(kind, service tag) of a knowledge document from its relative path."""
    parts = rel.split("/")
    if len(parts) == 1:
        return ("patterns" if rel.startswith("patterns") else "topology"), "all"
    kind = {"runbooks": "runbook", "releases": "release", "changelog": "changelog", "codebase": "codebase",
            "postmortems": "postmortem"}.get(parts[0], "doc")
    stem = parts[-1].rsplit(".", 1)[0].split("__")[0]
    return kind, stem if stem in config.SERVICES else "all"


def doc_chunks(rel: str, text: str, count: Callable[[str], int] = approx_tokens) -> list[dict[str, Any]]:
    """Chunks for one document: runbooks / topology ~250 tokens, the rest ~110 (about one entry each)."""
    from ff.store.sqlite_store import to_epoch

    kind, svc = doc_meta(rel)
    size = CHUNK_TOKENS if kind in ("runbook", "topology", "patterns") else 110
    title = text.splitlines()[0].lstrip("# ").strip() if text else rel
    pub = to_epoch(doc_published(text).isoformat())
    body_text = _PUB.sub("", text)
    out = []
    for i, c in enumerate(chunk(body_text, size, count)):
        body = c if i == 0 or c.startswith("#") else f"({title}) {c}"
        out.append({"id": f"doc:{rel}:{i}", "text": body, "service": svc, "path": rel, "kind": kind, "published": pub})
    return out


def chunk(text: str, max_tokens: int = CHUNK_TOKENS, count: Callable[[str], int] = approx_tokens) -> list[str]:
    """Split on blank lines, packing paragraphs up to ``max_tokens``; long paragraphs split by words."""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    pieces: list[str] = []
    for p in paras:
        if count(p) <= max_tokens:
            pieces.append(p)
            continue
        words, cur = p.split(), []
        for w in words:
            if cur and count(" ".join(cur + [w])) > max_tokens:
                pieces.append(" ".join(cur))
                cur = []
            cur.append(w)
        if cur:
            pieces.append(" ".join(cur))
    chunks, cur_text = [], ""
    for p in pieces:
        cand = f"{cur_text}\n\n{p}" if cur_text else p
        if cur_text and count(cand) > max_tokens:
            chunks.append(cur_text)
            cur_text = p
        else:
            cur_text = cand
    if cur_text:
        chunks.append(cur_text)
    return chunks


def index(paths: Paths, store: SqliteStore, vectors: VectorStore,
          count: Callable[[str], int] = approx_tokens) -> dict[str, int]:
    """Index every .md under docs/architecture; skip unchanged files; drop deleted ones."""
    stats = {"indexed": 0, "skipped": 0, "chunks": 0}
    present, changed, chunks = set(), {}, []
    for f in sorted(paths.docs_arch.rglob("*.md")):
        rel = f.relative_to(paths.docs_arch).as_posix()
        present.add(rel)
        text = f.read_text(encoding="utf-8")
        sha = hashlib.sha256(text.encode()).hexdigest()
        if store.doc_sha(rel) == sha:
            stats["skipped"] += 1
            continue
        changed[rel] = sha
        chunks += doc_chunks(rel, text, count)
    gone = [rel for rel in store.doc_paths() if rel not in present]
    # one delete and one (batched) upsert for all changed documents: per-file writes are slow in Chroma
    if changed or gone:
        vectors.architecture.delete(where={"path": {"$in": list(changed) + gone}})
    stats["chunks"] = vectors.upsert_chunks(chunks)
    for rel, sha in changed.items():
        store.set_doc_sha(rel, sha)
    for rel in gone:
        store.forget_doc(rel)
    stats["indexed"] = len(changed)
    return stats
