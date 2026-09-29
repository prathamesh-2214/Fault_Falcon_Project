"""Central configuration for FaultFalcon.

Every tunable number lives here so experiments, tests and the app agree. Paths are
resolved from ``FF_HOME`` (default: the repository root) so tests and CI can point the
whole pipeline at a temporary directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --------------------------------------------------------------------------- basics
SEED: int = 1337
REPO_ROOT: Path = Path(__file__).resolve().parents[1]



@dataclass(frozen=True)
class ModelSpec:
    """A supported generative model. All are Llama-architecture (required by the pos-shift patch)."""

    hf_id: str
    chat_format: str  # 'zephyr' | 'chatml'
    train_len: int
    note: str


MODELS: dict[str, ModelSpec] = {
    "smollm2-360m": ModelSpec("HuggingFaceTB/SmolLM2-360M-Instruct", "chatml", 8192,
                              "default: ~0.72 GB download, ~1.5 GB RAM in fp32"),
    "smollm-360m": ModelSpec("HuggingFaceTB/SmolLM-360M-Instruct", "chatml", 2048,
                             "same size, 2,048-token training length like TinyLlama"),
    "tinyllama-1.1b": ModelSpec("TinyLlama/TinyLlama-1.1B-Chat-v1.0", "zephyr", 2048,
                                "the original spec model: ~2.2 GB download, ~4.4 GB RAM in fp32"),
}
MODEL_KEY: str = os.environ.get("FF_MODEL", "smollm2-360m")
if MODEL_KEY not in MODELS:
    raise ValueError(f"FF_MODEL={MODEL_KEY!r}; choose one of {sorted(MODELS)}")
MODEL_SPEC: ModelSpec = MODELS[MODEL_KEY]


def _model_name() -> str:
    """Which weights to load: ``FF_MODEL_PATH`` if set; else a fine-tune found under ``models/``
    (``<FF_MODEL>-ff-chat`` from the Colab notebook, then ``<FF_MODEL>-ff`` from ``make finetune``);
    else the base model from the Hugging Face hub. ``FF_BASE_MODEL=1`` forces the base model."""
    if os.environ.get("FF_MODEL_PATH"):
        return os.environ["FF_MODEL_PATH"]
    if os.environ.get("FF_BASE_MODEL", "").lower() not in ("1", "true", "yes"):
        for name in (f"{MODEL_KEY}-ff-chat", f"{MODEL_KEY}-ff"):
            p = REPO_ROOT / "models" / name
            if (p / "config.json").exists() and (any(p.glob("*.safetensors")) or any(p.glob("*.bin"))):
                return str(p)
    return MODEL_SPEC.hf_id


MODEL_NAME: str = _model_name()
FINE_TUNED: bool = MODEL_NAME != MODEL_SPEC.hf_id
MODEL_LABEL: str = Path(MODEL_NAME).name + (" (fine-tuned)" if FINE_TUNED else "")  # short name for the UI
MODEL_TRAIN_LEN: int = MODEL_SPEC.train_len


@dataclass(frozen=True)
class Paths:
    """Filesystem layout. Build with :func:`get_paths` so ``FF_HOME`` is honoured."""

    home: Path

    @property
    def data(self) -> Path:
        return self.home / "data"

    @property
    def raw_loghub(self) -> Path:
        return self.data / "raw" / "loghub"

    @property
    def derived(self) -> Path:
        return self.data / "derived"

    @property
    def sim(self) -> Path:
        return self.data / "sim"

    @property
    def incidents(self) -> Path:
        return self.data / "incidents"

    @property
    def var(self) -> Path:
        return self.home / "var"

    @property
    def sqlite(self) -> Path:
        return self.var / "faultfalcon.sqlite"

    @property
    def chroma(self) -> Path:
        return self.var / "chroma"

    @property
    def graph_state(self) -> Path:
        return self.var / "graph_state.sqlite"

    @property
    def results(self) -> Path:
        return self.home / "results"

    @property
    def docs_arch(self) -> Path:
        return self.home / "docs" / "architecture"

    def ensure(self) -> Paths:
        """Create every output directory and return self."""
        for p in (self.raw_loghub, self.derived, self.sim, self.incidents, self.var,
                  self.results, self.docs_arch / "runbooks"):
            p.mkdir(parents=True, exist_ok=True)
        return self


def get_paths(home: str | Path | None = None) -> Paths:
    """Return the path layout rooted at ``home``, ``$FF_HOME`` or the repo root."""
    root = home or os.environ.get("FF_HOME") or REPO_ROOT
    return Paths(Path(root).resolve())


# --------------------------------------------------------------------------- timeline
# Six months of platform history ending on 2026-08-31; the real LogHub logs cover its last 72 hours.
# FF_SIM_DAYS shortens the history (tests / CI) without moving the real-log window.
SIM_END: datetime = datetime(2026, 8, 31, tzinfo=timezone.utc)
SIM_DAYS: int = int(os.environ.get("FF_SIM_DAYS", "182"))
DAY0: datetime = SIM_END - timedelta(days=SIM_DAYS)
LOG_WINDOW_HOURS: int = 72
LOG_START: datetime = SIM_END - timedelta(hours=LOG_WINDOW_HOURS)

# --------------------------------------------------------------------------- LogHub
LOGHUB_BASE_URL: str = "https://raw.githubusercontent.com/logpai/loghub/master"
LOGHUB_SYSTEMS: tuple[str, ...] = ("OpenStack", "Hadoop", "HDFS", "Zookeeper", "BGL", "Thunderbird")
LOGHUB_SUFFIXES: tuple[str, ...] = ("_2k.log", "_2k.log_structured.csv", "_2k.log_templates.csv")
LABELLED_SYSTEMS: frozenset[str] = frozenset({"BGL", "Thunderbird"})
INFO_LEVELS: frozenset[str] = frozenset({"INFO", "DEBUG", "TRACE", "NA"})

# --------------------------------------------------------------------------- platform
# The simulated company runs a small managed-compute platform on AWS (think "AWS Batch for
# customers"): an edge (API Gateway + ALB), a control-plane API, a job scheduler on an EC2 fleet,
# a block store, a coordination quorum, an image registry on S3, auth with a Redis session cache,
# and usage-based billing fed by an SQS queue. Five components are backed by REAL LogHub logs; the
# others (and the rest of the timeline) get synthetic logs in their native formats
# (ff/ingest/telemetry.py). Edges follow the usual AWS patterns: synchronous calls only where a
# request needs an answer, a queue between metering and billing, one database per bounded
# context, a read-through cache in front of the session store, hosts under the stateful services.


@dataclass(frozen=True)
class Component:
    """One deployable unit or managed AWS resource of the platform."""

    name: str
    kind: str  # edge | service | host | coordination | datastore | cache | queue | objectstore
    aws: str  # how it runs on AWS
    team: str
    lang: str | None  # code language (None: managed resource, no code)
    logs: str  # "loghub:<System>" (real logs in the last 72 h) or "synthetic"
    level: int  # deploy order inside a release train (callees first)
    purpose: str


COMPONENTS: dict[str, Component] = {c.name: c for c in (
    Component("api-gateway", "edge", "Amazon API Gateway (REST) + Application Load Balancer, Lambda authorizer",
              "team-edge", "python", "synthetic", 4, "Public entry point: routing, throttling, TLS, request auth"),
    Component("auth-svc", "service", "ECS Fargate (Go), 3 tasks behind an internal ALB", "team-identity", "go",
              "synthetic", 2, "Issues and validates access tokens; sessions in Redis, accounts in the meta DB"),
    Component("cloud-api", "service", "EC2 (OpenStack Nova API, Python)", "team-api", "python", "loghub:OpenStack", 3,
              "Control-plane API: servers, jobs, images; publishes usage events"),
    Component("image-svc", "service", "ECS Fargate (Go)", "team-api", "go", "synthetic", 1,
              "Image registry: base images stored in S3, served to cloud-api and hosts"),
    Component("compute-svc", "service", "EC2 Auto Scaling group (Hadoop YARN, Java)", "team-batch", "java",
              "loghub:Hadoop", 2, "Job scheduler and MapReduce runtime"),
    Component("storage-svc", "service", "EC2 + EBS gp3 (HDFS, Java)", "team-storage", "java", "loghub:HDFS", 1,
              "Block store for job data"),
    Component("coord-svc", "coordination", "EC2, 3-node ZooKeeper quorum", "team-platform", "java",
              "loghub:Zookeeper", 1, "Coordination: leader election, sessions, locks"),
    Component("node-svc", "host", "EC2 compute fleet (HPC nodes, C)", "team-hpc", "c", "loghub:BGL", 0,
              "Host layer: kernel, program loader, RAS, Lustre mounts"),
    Component("billing-svc", "service", "ECS Fargate (Java, Spring Boot)", "team-billing", "java", "synthetic", 2,
              "Usage-based billing: consumes usage events, writes invoices, customer billing API"),
    Component("notification-svc", "service", "AWS Lambda (Python) + Amazon SES", "team-billing", "python", "synthetic",
              1, "Invoice e-mails and customer webhooks"),
    Component("meta-db", "datastore", "Amazon RDS for PostgreSQL 15, Multi-AZ", "team-platform", None, "synthetic", 0,
              "Shared metadata DB: accounts, servers, jobs"),
    Component("billing-db", "datastore", "Amazon RDS for PostgreSQL 15", "team-billing", None, "synthetic", 0,
              "Billing DB: usage records, invoices"),
    Component("session-cache", "cache", "Amazon ElastiCache for Redis 7 (cluster mode off)", "team-identity", None,
              "synthetic", 0, "Session and token cache (read-through)"),
    Component("usage-queue", "queue", "Amazon SQS standard queue + DLQ", "team-billing", None, "synthetic", 0,
              "Usage events from cloud-api to billing-svc"),
    Component("object-store", "objectstore", "Amazon S3 bucket (images, job artifacts)", "team-platform", None,
              "synthetic", 0, "Base images and job artifacts"),
)}
SERVICES: tuple[str, ...] = tuple(COMPONENTS)
CODE_SERVICES: tuple[str, ...] = tuple(c.name for c in COMPONENTS.values() if c.lang)
MANAGED: tuple[str, ...] = tuple(c.name for c in COMPONENTS.values() if not c.lang)
# component -> LogHub system (the components whose last 72 h are REAL logs). Audit: data/audit.md.
SERVICE_MAP: dict[str, str] = {c.name: c.logs.split(":", 1)[1] for c in COMPONENTS.values()
                               if c.logs.startswith("loghub:")}
LOGHUB_SERVICES: tuple[str, ...] = tuple(SERVICE_MAP)
# components whose logs carry request latency (OpenStack API lines, ALB access logs)
LATENCY_SERVICES: tuple[str, ...] = ("cloud-api", "api-gateway")
LATENCY_SERVICE: str = "cloud-api"

# caller -> callees (synchronous calls, DB/cache connections, queue publish/consume)
TOPOLOGY_CALLS: dict[str, tuple[str, ...]] = {
    "api-gateway": ("auth-svc", "cloud-api", "billing-svc"),
    "auth-svc": ("session-cache", "meta-db"),
    "cloud-api": ("compute-svc", "image-svc", "meta-db", "usage-queue"),
    "image-svc": ("object-store",),
    "compute-svc": ("storage-svc", "coord-svc", "meta-db"),
    "billing-svc": ("billing-db", "usage-queue", "notification-svc"),
    "notification-svc": (),
    "storage-svc": (), "coord-svc": (), "node-svc": (),
    "meta-db": (), "billing-db": (), "session-cache": (), "usage-queue": (), "object-store": (),
}
# service -> host service it runs on
TOPOLOGY_HOSTS: dict[str, str] = {"compute-svc": "node-svc", "storage-svc": "node-svc"}
# queue -> producers (a consumer's input depends on what the producers publish)
QUEUE_PRODUCERS: dict[str, tuple[str, ...]] = {"usage-queue": ("cloud-api",)}
QUEUE_CONSUMERS: dict[str, tuple[str, ...]] = {"usage-queue": ("billing-svc",)}


def dependencies(service: str) -> tuple[str, ...]:
    """Direct callees of ``service``."""
    return TOPOLOGY_CALLS.get(service, ())


def callers(service: str) -> tuple[str, ...]:
    """Components that call ``service`` directly (for a queue: its consumers, which read from it)."""
    return tuple(s for s, deps in TOPOLOGY_CALLS.items() if service in deps
                 and not (service in QUEUE_PRODUCERS and s in QUEUE_PRODUCERS[service]))


def host_of(service: str) -> str | None:
    """Host service ``service`` runs on, if any."""
    return TOPOLOGY_HOSTS.get(service)


def upstream_of(service: str) -> tuple[str, ...]:
    """Where a fault seen in ``service`` can come from: its callees, its host and, for a queue, the
    producers that publish into it."""
    out = list(dependencies(service))
    if host_of(service):
        out.append(host_of(service))  # type: ignore[arg-type]
    out += QUEUE_PRODUCERS.get(service, ())
    return tuple(dict.fromkeys(out))


def dependency_path(src: str, dst: str) -> list[str]:
    """Shortest path src -> dst following 'calls', 'runs on' and 'queue fed by' edges ([] if unreachable)."""
    prev: dict[str, str | None] = {src: None}
    todo = [src]
    while todo:
        cur = todo.pop(0)
        if cur == dst:
            path = [cur]
            while prev[path[-1]] is not None:
                path.append(prev[path[-1]])  # type: ignore[arg-type]
            return path[::-1]
        for n in upstream_of(cur):
            if n not in prev:
                prev[n] = cur
                todo.append(n)
    return []


def neighbourhood(service: str) -> tuple[str, ...]:
    """The lens service, its direct dependencies, its host and (for a queue) its producers:
    the retrieval scope."""
    return tuple(dict.fromkeys([service, *upstream_of(service)]))


# --------------------------------------------------------------------------- log events
DRAIN_SIM_TH: float = 0.4
DRAIN_DEPTH: int = 4
DRAIN_MAX_CHILDREN: int = 100
WINDOW_MIN_MINUTES: float = 5.0
WINDOW_DIVISOR: int = 150
NOVELTY_WARMUP_WINDOWS: int = 2  # no "first-seen template" flags in the first N windows
ERROR_RATIO_MULT: float = 3.0
MIN_ERROR_LINES: int = 3
KNOWN_NOISE_SHARE: float = 0.30
LATENCY_MARGIN: float = 1.25  # p95 must exceed the band's upper edge by 25%
MIN_LATENCY_SAMPLES: int = 5
SEVERITY_RATIO_CAP: float = 20.0
NARRATIVE_MAX_WORDS: int = 100
NARRATIVE_SYSTEM: str = "Describe what the numbers show. Do not guess causes. Use only the numbers given."

# --------------------------------------------------------------------------- simulator
# 120 incidents: up to MAX_REAL_INCIDENTS anchored on anomalies of the REAL LogHub logs (last 72 h),
# the rest planned over the six months with synthetic telemetry. 80 % 'dev' (training / practice),
# 20 % 'heldout' (evaluation), stratified by scenario family. FF_INCIDENTS shrinks it for tests.
N_INCIDENTS: int = int(os.environ.get("FF_INCIDENTS", "120"))
DEV_SHARE: float = 0.8
MAX_REAL_INCIDENTS: int = 12
MAX_INCIDENTS: int = N_INCIDENTS
DEV_INCIDENTS: int = round(N_INCIDENTS * DEV_SHARE)
INCIDENT_MIN_GAP_HOURS: float = 12.0  # between any two planned incidents
# Scenario library: ff/ingest/scenarios.py; synthetic logs: ff/ingest/telemetry.py.

# --------------------------------------------------------------------------- ranker
RANKER_WEIGHTS: dict[str, float] = {"sim": 0.45, "recency": 0.25, "significance": 0.20, "keyword": 0.10}
RECENCY_TAU_DAYS: dict[str, float] = {
    "ANOMALY": 2.0, "ERROR_SIGNATURE": 2.0,
    "DEPLOY": 14.0, "PATCH": 14.0, "ROLLBACK": 14.0, "CONFIG_CHANGE": 14.0,
    "TEST_RUN": 14.0, "SECURITY_SCAN": 14.0,
    "INFRA_CHANGE": 60.0,
}
WINDOW_DAYS: int = 21
AFTER_INCIDENT_HOURS: float = 2.0
RETRIEVAL_BUDGET: int = 600
TOP_K_SIM: int = 25
SLOT_SHARES: dict[str, float] = {"guaranteed": 0.35, "scored": 0.45, "architecture": 0.20}
GUARANTEE_LENS: int = 3
GUARANTEE_NEIGHBOUR: int = 1
GUARANTEE_MIN_SIG: int = 4
COMPRESS_MAX_TOKENS: int = 60
CHECKPOINT_CANDIDATES: int = 10
CHANGE_SCORE_THRESHOLD: float = 0.45

CHANGE_TYPES: tuple[str, ...] = ("DEPLOY", "PATCH", "ROLLBACK", "CONFIG_CHANGE", "INFRA_CHANGE")
LOG_TYPES: tuple[str, ...] = ("ANOMALY", "ERROR_SIGNATURE")
# RUNTIME stage also sees BASELINE_SNAPSHOT events (only created by the replay ablation that
# stores hourly baselines as vectors instead of pinning one baseline record)
RUNTIME_TYPES: tuple[str, ...] = LOG_TYPES + ("BASELINE_SNAPSHOT",)

# --------------------------------------------------------------------------- KV cache
TOTAL_CACHE: int = 1536
SINK_TOKENS: int = 4
MIN_RECENT: int = 600
MAX_CHECKPOINTS: int = 5
SEGMENT_ORDER: tuple[str, ...] = ("sinks", "system", "initial_query", "checkpoints", "lens_baseline", "ledger")
SEGMENT_BUDGETS: dict[str, int] = {
    "sinks": SINK_TOKENS,
    "system": 120,
    "initial_query": 100,
    "checkpoints": 300,
    "lens_baseline": 120,
    "ledger": 200,
}
START_REGION_MAX: int = TOTAL_CACHE - MIN_RECENT
LEDGER_MAX_TOKENS: int = 250
MAX_NEW_TOKENS: int = 128

SYSTEM_PROMPT: str = (
    "You are FaultFalcon, a DevOps root-cause assistant. Check changes before symptoms. "
    "Give one hypothesis. Cite ids like [evt_12]. Use only the context. "
    "If no change explains it, say so."
)

CACHE_MODES: tuple[str, ...] = ("full", "trim", "streaming", "faultfalcon", "ff_no_anchor")

# --------------------------------------------------------------------------- app
USE_LANGGRAPH: bool = os.environ.get("USE_LANGGRAPH", "false").lower() in {"1", "true", "yes"}
TINY_MODEL: bool = os.environ.get("FF_TINY_MODEL", "0").lower() in {"1", "true", "yes"}
