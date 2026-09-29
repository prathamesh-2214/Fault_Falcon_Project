"""The simulated repository: files, diff hunks, config keys and AWS Terraform resources.

Every simulated change is a set of *hunks* (file, line, before/after lines with context),
rendered as unified-diff text and stored on ``change_detail.diff``. That is the "micro"
context: once the macro root cause (a change) is known, the hunks show the exact file and
line that broke it.

* Code hunks come from per-language pools of benign edits (logging, metrics, guards,
  renames) and from the breaking hunks that scenarios use as culprits.
* Parameter hunks are generated from :data:`PARAMS`: each key lives in either
  ``config/<svc>.yaml`` or ``infra/<svc>.tf`` (an AWS resource), always at the same line, and
  the old/new values are filled in by the simulator's chronological replay.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------- files
# service -> [(path, symbol, language)]
CODE_FILES: dict[str, list[tuple[str, str, str]]] = {
    "cloud-api": [("cloud-api/nova/api/servers.py", "ServersController.detail", "python"),
                  ("cloud-api/nova/api/auth.py", "AuthMiddleware.__call__", "python"),
                  ("cloud-api/nova/compute/client.py", "ComputeClient.call", "python"),
                  ("cloud-api/nova/image/cache.py", "ImageCacheManager.verify_base_images", "python"),
                  ("cloud-api/nova/usage/publisher.py", "UsagePublisher.publish", "python")],
    "compute-svc": [("compute/scheduler/TaskRunner.java", "TaskRunner.assignSlots", "java"),
                    ("compute/rpc/RMClient.java", "RMClient.allocate", "java"),
                    ("compute/mapreduce/ShuffleHandler.java", "ShuffleHandler.sendMapOutput", "java"),
                    ("compute/dfs/LeaseRenewer.java", "LeaseRenewer.renew", "java")],
    "storage-svc": [("storage/datanode/BlockWriter.java", "BlockWriter.write", "java"),
                    ("storage/datanode/DataXceiver.java", "DataXceiver.readBlock", "java"),
                    ("storage/namenode/BlockManager.java", "BlockManager.addBlock", "java"),
                    ("storage/datanode/PacketResponder.java", "PacketResponder.run", "java")],
    "coord-svc": [("coord/quorum/QuorumCnxManager.java", "QuorumCnxManager.connectOne", "java"),
                  ("coord/quorum/FastLeaderElection.java", "FastLeaderElection.lookForLeader", "java"),
                  ("coord/server/NIOServerCnxn.java", "NIOServerCnxn.doIO", "java"),
                  ("coord/session/SessionTracker.java", "SessionTrackerImpl.run", "java")],
    "node-svc": [("node/kernel/ras_handler.c", "ras_handle_event", "c"),
                 ("node/ciod/ciod_loader.c", "ciod_load_program", "c"),
                 ("node/mem/tlb_miss.c", "dtlb_miss_handler", "c"),
                 ("node/io/lustre_mount.c", "lustre_mount", "c")],
    "api-gateway": [("gateway/authorizer/handler.py", "authorize", "python"),
                    ("gateway/authorizer/jwks.py", "JwksCache.get_key", "python"),
                    ("gateway/transforms/request_mapper.py", "map_request", "python"),
                    ("gateway/throttle/usage_plans.py", "UsagePlans.limit_for", "python")],
    "auth-svc": [("auth/internal/token/issuer.go", "Issuer.Issue", "go"),
                 ("auth/internal/session/store.go", "RedisStore.Get", "go"),
                 ("auth/internal/account/repo.go", "AccountRepo.FindByID", "go"),
                 ("auth/internal/token/refresh.go", "Refresher.Refresh", "go")],
    "image-svc": [("images/internal/registry/s3.go", "S3Registry.Fetch", "go"),
                  ("images/internal/cache/lru.go", "LRU.Get", "go"),
                  ("images/internal/api/handlers.go", "Handlers.GetImage", "go"),
                  ("images/internal/verify/checksum.go", "VerifyChecksum", "go")],
    "billing-svc": [("billing/consumer/UsageEventConsumer.java", "UsageEventConsumer.onMessage", "java"),
                    ("billing/invoice/InvoiceService.java", "InvoiceService.generate", "java"),
                    ("billing/repo/InvoiceRepository.java", "InvoiceRepository.findOpenByAccount", "java"),
                    ("billing/client/NotificationClient.java", "NotificationClient.send", "java")],
    "notification-svc": [("notify/handlers/invoice_email.py", "send_invoice_email", "python"),
                         ("notify/handlers/webhook.py", "deliver_webhook", "python"),
                         ("notify/ses_client.py", "SesClient.send", "python"),
                         ("notify/templates/render.py", "render_template", "python")],
}
# where each service keeps its DB migrations (and which database they run on)
MIGRATION_DIR: dict[str, str] = {"cloud-api": "cloud-api/db/migrations", "compute-svc": "compute/db/migrations",
                                 "auth-svc": "auth/db/migrations", "billing-svc": "billing/db/migrations"}
DB_OF: dict[str, str] = {"cloud-api": "meta-db", "compute-svc": "meta-db", "auth-svc": "meta-db",
                         "billing-svc": "billing-db"}
# dependency manifests (library upgrades)
BUILD_FILES: dict[str, str] = {"cloud-api": "cloud-api/requirements.txt", "compute-svc": "compute/pom.xml",
                               "storage-svc": "storage/pom.xml", "coord-svc": "coord/pom.xml",
                               "node-svc": "node/Makefile.deps", "api-gateway": "gateway/requirements.txt",
                               "auth-svc": "auth/go.mod", "image-svc": "images/go.mod", "billing-svc": "billing/pom.xml",
                               "notification-svc": "notify/requirements.txt"}
# the file holding each service's outbound client timeout (used by the unit-bug scenario)
CLIENT_FILE: dict[str, str] = {"cloud-api": "cloud-api/nova/compute/client.py", "compute-svc": "compute/rpc/RMClient.java",
                               "storage-svc": "storage/datanode/DataXceiver.java",
                               "coord-svc": "coord/quorum/QuorumCnxManager.java", "node-svc": "node/io/lustre_mount.c",
                               "api-gateway": "gateway/authorizer/handler.py", "auth-svc": "auth/internal/session/store.go",
                               "image-svc": "images/internal/registry/s3.go",
                               "billing-svc": "billing/client/NotificationClient.java",
                               "notification-svc": "notify/ses_client.py"}
DOC_FILES = ("docs/CHANGELOG.md", "metrics/dashboards.json", "README.md")


def lang_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if name in ("pom.xml", "go.mod", "requirements.txt", "Makefile.deps"):
        return "build"
    return {"py": "python", "java": "java", "c": "c", "go": "go", "md": "docs", "json": "docs", "sql": "sql",
            "yaml": "config", "tf": "infra"}.get(path.rsplit(".", 1)[-1], "docs")


def symbol_of(path: str) -> str:
    for files in CODE_FILES.values():
        for f, sym, _ in files:
            if f == path:
                return sym
    return path.rsplit("/", 1)[-1]


# --------------------------------------------------------------------------- hunks
@dataclass(frozen=True)
class Hunk:
    """One diff hunk. ``before`` / ``after`` may hold ``{old}`` / ``{new}`` placeholders (params)."""

    file: str
    symbol: str
    before: tuple[str, ...]
    after: tuple[str, ...]
    ctx_before: tuple[str, ...] = ()
    ctx_after: tuple[str, ...] = ()
    kind: str = "code"  # code | config | infra | docs | sql

    def with_file(self, file: str, symbol: str | None = None) -> Hunk:
        return Hunk(file, symbol or symbol_of(file), self.before, self.after, self.ctx_before, self.ctx_after,
                    lang_kind(file))


def lang_kind(path: str) -> str:
    lang = lang_of(path)
    return lang if lang in ("config", "infra", "docs", "sql") else "code"


def _fmt_value(v: Any, quote: bool) -> str:
    s = str(v)
    if quote and not s.replace(".", "", 1).isdigit():
        return f'"{s}"'
    return s


def render_diff(h: Hunk, line: int, old: Any = None, new: Any = None) -> str:
    """Unified-diff text; ``line`` is the first line of the hunk (context included)."""
    quote = h.kind == "infra"

    def val(v: Any) -> str:
        if h.kind == "infra-raw":  # the template already carries HCL quoting; lists render as lists
            s = str(v)
            return "[" + ", ".join(f'"{a}"' for a in s.split(",") if a) + "]" if ":" in s and not s[0].isdigit() else s
        return _fmt_value(v, quote)

    def fill(s: str) -> str:
        return s.replace("{old}", val(old)).replace("{new}", val(new))

    n_old = len(h.ctx_before) + len(h.before) + len(h.ctx_after)
    n_new = len(h.ctx_before) + len(h.after) + len(h.ctx_after)
    out = [f"@@ -{line},{n_old} +{line},{n_new} @@ {h.symbol}"]
    out += [" " + fill(x) for x in h.ctx_before]
    out += ["-" + fill(x) for x in h.before]
    out += ["+" + fill(x) for x in h.after]
    out += [" " + fill(x) for x in h.ctx_after]
    return "\n".join(out)


def changed_line(h: Hunk, line: int) -> int:
    """Line number of the first changed line."""
    return line + len(h.ctx_before)


def stable_line(*parts: str, lo: int = 12, hi: int = 380) -> int:
    """Deterministic line number for a (file, key) so the same setting always sits on the same line."""
    h = int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)
    return lo + h % (hi - lo)


# benign edit templates per language: (before, after, ctx_before, ctx_after)
BENIGN: dict[str, list[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]]] = {
    "java": [
        (('      LOG.debug("start");',), ('      LOG.info("start attempt={}", attempt);',), ("    try {",), ("      process();",)),
        ((), ('    metrics.counter("calls").inc();',), ("  public void run() {",), ("    long t0 = clock.now();",)),
        (("    return result;",), ("    if (result == null) {", "      return Collections.emptyList();", "    }",
                                   "    return result;"), ("    List<Item> result = fetch();",), ("  }",)),
        (("    int n = items.size();",), ("    int itemCount = items.size();",), ("  void flush(List<Item> items) {",), ()),
        (("    // TODO: tune",), ("    // tuned in load test, see PERF-212",), ("  private static final int BATCH = 128;",), ()),
    ],
    "python": [
        (('    LOG.debug("listing")',), ('    LOG.info("listing", extra={"tenant": ctx.tenant})',), ("def detail(self, req):",),
         ("    items = self._list(req)",)),
        (("def index(self, req):",), ("def index(self, req: Request) -> Response:",), (), ('    """List servers."""',)),
        (("    return self._client.get(url)",), ("    resp = self._client.get(url)", "    resp.raise_for_status()",
                                               "    return resp"), ("    url = self._url(path)",), ()),
        ((), ("    self._metrics.incr(\"cache.lookups\")",), ("    key = self._key(image_id)",), ("    return key in self._c",)),
    ],
    "c": [
        (('    printk(KERN_DEBUG "event %d\\n", ev);',), ('    pr_debug("event %d code %x\\n", ev, code);',), ("    ev = read_event(dev);",), ()),
        (("    int i;",), ("    size_t i;",), ("static int scan(struct node *n) {",), ("    for (i = 0; i < n->count; i++) {",)),
        (("    /* FIXME */",), ("    /* retries handled by caller since v5.2 */",), ("    rc = submit(req);",), ()),
    ],
    "docs": [
        ((), ("- Add dashboard panel for p95 latency",), ("## Unreleased",), ()),
        (('    "refresh": "1m",',), ('    "refresh": "30s",',), ('  "title": "Service overview",',), ()),
        (("See the runbook.",), ("See the runbook in docs/architecture/runbooks.",), ("## Operations",), ()),
    ],
    "go": [
        (('\tlog.Debug("fetch", "key", key)',), ('\tlog.Info("fetch", "key", key, "request_id", reqID(ctx))',),
         ("\tstart := time.Now()",), ("\tv, err := s.client.Get(ctx, key).Result()",)),
        ((), ('\tmetrics.Counter("calls_total").Inc()',), ("func (s *Store) handle(ctx context.Context) error {",),
         ("\tdefer span.End()",)),
        (("\tif err != nil {", "\t\treturn err"),
         ("\tif err != nil {", '\t\treturn fmt.Errorf("lookup %s: %w", key, err)'), ("\tv, err := s.lookup(ctx, key)",),
         ("\t}",)),
        (("\t// TODO: tune",), ("\t// tuned in load test, see PERF-318",), ("\tconst batchSize = 128",), ()),
    ],
    "build": [
        (("    <version>2.15.2</version>",), ("    <version>2.15.3</version>",),
         ("    <artifactId>jackson-databind</artifactId>",), ("  </dependency>",)),
        (("requests==2.31.0",), ("requests==2.32.3",), ("boto3==1.34.10",), ()),
        (("\tgithub.com/prometheus/client_golang v1.18.0",), ("\tgithub.com/prometheus/client_golang v1.19.1",),
         ("require (",), (")",)),
    ],
    "sql": [((), ("CREATE INDEX IF NOT EXISTS ix_jobs_state ON jobs(state);",), ("-- migration",), ())],
}


def benign_hunk(rng: Any, file: str, symbol: str | None = None) -> Hunk:
    """A harmless edit in ``file`` (language inferred from the extension)."""
    lang = lang_of(file)
    pool = BENIGN.get(lang, BENIGN["docs"])
    before, after, cb, ca = pool[rng.randrange(len(pool))]
    return Hunk(file, symbol or symbol_of(file), before, after, cb, ca, lang_kind(file))


# --------------------------------------------------------------------------- breaking hunks
# generic breaking edits used by the simple deploy scenarios (one per language)
BREAKING: dict[str, Hunk] = {
    "java": Hunk("", "", ("    if (attempt < maxAttempts) {",), ("    if (attempt > maxAttempts) {",),
                 ("    attempt++;",), ("      return retry(request);", "    }")),
    "python": Hunk("", "", ("        if not cached:",), ("        if cached:",),
                   ("        cached = self._cache.get(key)",), ("            cached = self._fetch(key)",)),
    "c": Hunk("", "", ("    if (rc < 0) {",), ("    if (rc <= 0) {",), ("    rc = submit(req);",),
              ("        return handle_error(rc);", "    }")),
    "go": Hunk("", "", ("\tif err != nil {",), ("\tif err == nil {",), ("\tv, err := s.lookup(ctx, key)",),
               ("\t\treturn nil, err", "\t}")),
}
# a load-path regression (only caught by the load suite): used by SKIPPED_TEST
PERF: dict[str, Hunk] = {
    "java": Hunk("", "", ("  private static final int BATCH = 512;",), ("  private static final int BATCH = 1;",),
                 ("  // records per flush",), ()),
    "python": Hunk("", "", ("PAGE_SIZE = 500",), ("PAGE_SIZE = 5",), ("# servers per page",), ()),
    "c": Hunk("", "", ("#define BATCH 512",), ("#define BATCH 1",), ("/* frames per DMA batch */",), ()),
    "go": Hunk("", "", ("\tconst batchSize = 512",), ("\tconst batchSize = 1",), ("\t// items per pipeline flush",), ()),
}


def breaking_hunk(kind: dict[str, Hunk], file: str) -> Hunk:
    return kind.get(lang_of(file), kind["java"]).with_file(file)


# --------------------------------------------------------------------------- parameters
@dataclass(frozen=True)
class Param:
    """Where a parameter lives. ``kind`` 'config' -> config/<svc>.yaml, 'infra' -> infra/<svc>.tf.

    ``on`` lists the components that have it (empty: every component with code)."""

    kind: str
    default: Any
    section: str  # yaml section line, or terraform resource type
    attr: str  # yaml leaf / terraform attribute
    resource_suffix: str = ""  # terraform resource name suffix
    on: tuple[str, ...] = ()

    def file(self, svc: str) -> str:
        return f"config/{svc}.yaml" if self.kind == "config" else f"infra/{svc}.tf"

    def resource(self, svc: str) -> str | None:
        if self.kind != "infra":
            return None
        return f"{self.section}.{svc.replace('-', '_')}{self.resource_suffix}"

    def applies(self, svc: str) -> bool:
        from ff import config

        return svc in self.on if self.on else svc in config.CODE_SERVICES


_JVM = ("compute-svc", "storage-svc", "coord-svc", "billing-svc")
_EC2 = ("cloud-api", "compute-svc", "storage-svc", "coord-svc", "node-svc")
_DBS = ("meta-db", "billing-db")
PARAMS: dict[str, Param] = {
    # config/<svc>.yaml (services with code)
    "http.timeout_ms": Param("config", 2000, "http:", "timeout_ms"),
    "db.pool.max_size": Param("config", 50, "db:", "pool_max_size",
                              on=("cloud-api", "compute-svc", "auth-svc", "billing-svc")),
    "db.statement_timeout_ms": Param("config", 30000, "db:", "statement_timeout_ms",
                                     on=("cloud-api", "compute-svc", "auth-svc", "billing-svc")),
    "retry.max": Param("config", 3, "retry:", "max_attempts"),
    "retry.backoff_ms": Param("config", 200, "retry:", "backoff_ms"),
    "jvm.heap_mb": Param("config", 4096, "jvm:", "heap_mb", on=_JVM),
    "gc.algorithm": Param("config", "G1", "jvm:", "gc", on=_JVM),
    "log.level": Param("config", "INFO", "logging:", "level"),
    "metrics.interval_s": Param("config", 60, "metrics:", "interval_s"),
    "session.timeout_ms": Param("config", 4000, "session:", "timeout_ms", on=("coord-svc", "compute-svc")),
    "dfs.replication": Param("config", 2, "dfs:", "replication", on=("storage-svc",)),
    "cache.session_ttl_s": Param("config", 3600, "cache:", "session_ttl_s", on=("auth-svc",)),
    "cache.image_mb": Param("config", 2048, "cache:", "image_mb", on=("image-svc",)),
    "consumer.concurrency": Param("config", 8, "consumer:", "concurrency", on=("billing-svc",)),
    "db.credentials_cache_s": Param("config", 900, "db:", "credentials_cache_s", on=("billing-svc",)),
    "webhook.max_retries": Param("config", 3, "webhook:", "max_retries", on=("notification-svc",)),
    "breaker.failure_pct": Param("config", 50, "circuit_breaker:", "failure_rate_threshold_pct",
                                 on=("api-gateway", "cloud-api", "billing-svc")),
    "feature": Param("config", "off", "features:", "{flag}"),
    # infra/<component>.tf (AWS)
    "disk_gb": Param("infra", 500, "aws_ebs_volume", "size", "_data", on=_EC2),
    "ebs_throughput": Param("infra", 250, "aws_ebs_volume", "throughput", "_data", on=("storage-svc",)),
    "ebs_iops": Param("infra", 6000, "aws_ebs_volume", "iops", "_data", on=("storage-svc",)),
    "instance_type": Param("infra", "m5.2xlarge", "aws_launch_template", "instance_type", on=_EC2),
    "node_pool_size": Param("infra", 6, "aws_autoscaling_group", "desired_capacity", on=_EC2),
    "asg_max_size": Param("infra", 12, "aws_autoscaling_group", "max_size", on=("node-svc", "compute-svc")),
    "ecs_desired_count": Param("infra", 3, "aws_ecs_service", "desired_count",
                               on=("auth-svc", "image-svc", "billing-svc")),
    "ecs_task_memory_mb": Param("infra", 2048, "aws_ecs_task_definition", "memory",
                                on=("auth-svc", "image-svc", "billing-svc")),
    "lb_healthcheck_timeout_s": Param("infra", 5, "aws_lb_target_group", "health_check.timeout",
                                      on=("api-gateway", "cloud-api", "auth-svc")),
    "alb_idle_timeout_s": Param("infra", 60, "aws_lb", "idle_timeout", on=("api-gateway",)),
    "apigw_burst_limit": Param("infra", 5000, "aws_api_gateway_usage_plan", "throttle_settings.burst_limit",
                               on=("api-gateway",)),
    "apigw_rate_limit": Param("infra", 2000, "aws_api_gateway_usage_plan", "throttle_settings.rate_limit",
                              on=("api-gateway",)),
    "sg_election_port": Param("infra", 3888, "aws_security_group", "ingress.to_port", on=("coord-svc",)),
    "iam_s3_actions": Param("infra", "s3:GetObject,s3:ListBucket", "aws_iam_policy_document", "statement.actions",
                            on=("image-svc",)),
    "lambda_concurrency": Param("infra", 50, "aws_lambda_function", "reserved_concurrent_executions",
                                on=("notification-svc", "api-gateway")),
    "lambda_timeout_s": Param("infra", 30, "aws_lambda_function", "timeout", on=("notification-svc", "api-gateway")),
    # managed data stores
    "rds_instance_class": Param("infra", "db.r5.large", "aws_db_instance", "instance_class", on=_DBS),
    "rds_max_connections": Param("infra", 500, "aws_db_parameter_group", "parameter.max_connections", on=_DBS),
    "rds_engine_version": Param("infra", "15.5", "aws_db_instance", "engine_version", on=_DBS),
    "rds_apply_immediately": Param("infra", "false", "aws_db_instance", "apply_immediately", on=_DBS),
    "rds_storage_gb": Param("infra", 400, "aws_db_instance", "allocated_storage", on=_DBS),
    "secret_rotation_days": Param("infra", 0, "aws_secretsmanager_secret_rotation",
                                  "rotation_rules.automatically_after_days", on=("billing-db",)),
    "redis_node_type": Param("infra", "cache.r6g.large", "aws_elasticache_replication_group", "node_type",
                             on=("session-cache",)),
    "redis_maxmemory_policy": Param("infra", "volatile-lru", "aws_elasticache_parameter_group",
                                    "parameter.maxmemory-policy", on=("session-cache",)),
    "redis_sg_port": Param("infra", 6379, "aws_security_group", "ingress.to_port", on=("session-cache",)),
    "sqs_visibility_timeout_s": Param("infra", 300, "aws_sqs_queue", "visibility_timeout_seconds", on=("usage-queue",)),
    "sqs_max_receive_count": Param("infra", 5, "aws_sqs_queue", "redrive_policy.maxReceiveCount", on=("usage-queue",)),
    "sqs_retention_s": Param("infra", 345600, "aws_sqs_queue", "message_retention_seconds", on=("usage-queue",)),
    "s3_expiration_days": Param("infra", 365, "aws_s3_bucket_lifecycle_configuration", "rule.expiration.days",
                                on=("object-store",)),
    "s3_request_metrics": Param("infra", "false", "aws_s3_bucket_metric", "enabled", on=("object-store",)),
}
FEATURE_FLAG = {"cloud-api": "feature.async_boot", "compute-svc": "feature.new_scheduler",
                "storage-svc": "feature.fast_ack", "coord-svc": "feature.fast_election", "node-svc": "feature.ecc_scrub",
                "api-gateway": "feature.edge_cache", "auth-svc": "feature.token_v2", "image-svc": "feature.lazy_pull",
                "billing-svc": "feature.invoice_v2", "notification-svc": "feature.batch_send"}


def param_spec(key: str) -> Param:
    return PARAMS["feature"] if key.startswith("feature.") else PARAMS[key]


def params_of(svc: str) -> list[str]:
    """The parameter keys a component has (its feature flag included)."""
    keys = [k for k, p in PARAMS.items() if k != "feature" and p.applies(svc)]
    if svc in FEATURE_FLAG:
        keys.append(FEATURE_FLAG[svc])
    return keys


# Terraform attributes whose real syntax is not a plain nested block: (context before, changed line, context after)
_TF_CUSTOM: dict[str, tuple[tuple[str, ...], str, tuple[str, ...]]] = {
    "rds_max_connections": (('resource "aws_db_parameter_group" "{name}" {', '  family = "postgres15"', "  parameter {",
                             '    name  = "max_connections"'), '    value = "{v}"', ('    apply_method = "pending-reboot"',
                                                                                     "  }")),
    "redis_maxmemory_policy": (('resource "aws_elasticache_parameter_group" "{name}" {', '  family = "redis7"',
                                "  parameter {", '    name  = "maxmemory-policy"'), '    value = "{v}"', ("  }",)),
    "sqs_max_receive_count": (('resource "aws_sqs_queue" "{name}" {', "  redrive_policy = jsonencode({",
                               "    deadLetterTargetArn = aws_sqs_queue.{name}_dlq.arn"), "    maxReceiveCount     = {v}",
                              ("  })",)),
    "iam_s3_actions": (('data "aws_iam_policy_document" "{name}" {', "  statement {", '    effect    = "Allow"'),
                       "    actions   = {v}", ('    resources = ["${aws_s3_bucket.object_store.arn}/images/*"]', "  }")),
    "s3_expiration_days": (('resource "aws_s3_bucket_lifecycle_configuration" "{name}" {', "  rule {",
                            '    id     = "expire-old-images"', '    status = "Enabled"', '    filter { prefix = "images/" }',
                            "    expiration {"), "      days = {v}", ("    }", "  }")),
}


def param_hunk(svc: str, key: str) -> tuple[Hunk, int]:
    """(hunk template with {old}/{new}, stable first line) for a parameter of ``svc``."""
    p = param_spec(key)
    if key in _TF_CUSTOM:
        cb, line, ca = _TF_CUSTOM[key]
        name = svc.replace("-", "_")
        cb = tuple(x.replace("{name}", name) for x in cb)
        ca = tuple(x.replace("{name}", name) for x in ca)
        h = Hunk(p.file(svc), p.resource(svc) or key, (line.replace("{v}", "{old}"),), (line.replace("{v}", "{new}"),),
                 cb, ca, "infra-raw")
        return h, stable_line(svc, key, lo=4, hi=140)
    if p.kind == "config":
        leaf = key.split(".", 1)[1] if key.startswith("feature.") else p.attr
        h = Hunk(p.file(svc), key, (f"  {leaf}: {{old}}",), (f"  {leaf}: {{new}}",), (p.section,),
                 ("  # managed by the platform team",), "config")
    else:
        attr = p.attr.split(".")[-1]
        blocks = p.attr.split(".")[:-1]
        name = f"{svc.replace('-', '_')}{p.resource_suffix}"
        cb = [f'resource "{p.section}" "{name}" {{']
        pad = "  "
        for b in blocks:
            cb.append(f"{pad}{b} {{")
            pad += "  "
        ca = tuple("  " * (k + 1) + "}" for k in reversed(range(len(blocks)))) if blocks else ("  tags = local.tags",)
        h = Hunk(p.file(svc), p.resource(svc) or key, (f"{pad}{attr} = {{old}}",), (f"{pad}{attr} = {{new}}",),
                 tuple(cb), ca, "infra")
    return h, stable_line(svc, key, lo=4, hi=140)
