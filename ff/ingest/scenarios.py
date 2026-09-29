"""Incident scenarios: macro cause, micro culprit line, contributing factors, blast radius, fix.

Each scenario *builds* the changes behind one incident into the simulated world:

* **macro cause**: the change (deploy / hotfix patch / config / AWS infra event) the RCA must
  identify; ``None`` for external causes (traffic, certificates, AWS degradations);
* **micro culprit**: the one diff hunk (``change_detail`` row) whose line broke production;
  for ``FLAG_DORMANT`` it lives in an *earlier* deploy than the macro trigger;
* **contributing** changes that made it worse (e.g. a shrunk RDS instance under a pool cut);
* **blast radius** (components affected, via the topology) and a **remediation** template.

Scenarios cover the failure classes a platform team meets: code bugs shipped in releases or
hotfixes (null dereference, N+1 queries, leaks, unit bugs, contract breaks between producer
and consumer, library upgrades, blocking migrations), config errors (pools, TTLs, retries,
circuit breakers, feature flags) and AWS infrastructure changes (RDS parameter groups and
maintenance, Secrets Manager rotation, ElastiCache sizing / eviction / security groups, SQS
visibility and redrive, IAM policies, S3 lifecycle rules, API Gateway throttling, ALB timeouts,
ECS memory, Auto Scaling limits, Lambda concurrency, EBS throughput).

Two ways an incident gets a scenario:

* **planned** incidents (synthetic telemetry, ``ff/ingest/plan.py``) name their scenario and
  cause component up front (``inc["planned_cause"]``); the telemetry shows the fault's error
  signature along the dependency path from the cause to the alerting service;
* incidents anchored on **real LogHub anomalies** get a scenario by service and error-template
  keywords (:func:`match`).

Templates use ``{svc} {version_from} {version_to} {file} {line} {key} {old} {new} {resource}
{symbol} {cycle} {alert_svc} {culprit_event}``, filled after the simulator's replay/renumbering.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from ff import config
from ff.ingest import repo
from ff.ingest.repo import Hunk

H = timedelta(hours=1)
M = timedelta(minutes=1)
D = timedelta(days=1)

CODE = tuple(config.CODE_SERVICES)
PAGING = CODE  # components with SLO alerts that page on-call


@dataclass
class Outcome:
    """What a scenario built (provisional ids; renumbered by the simulator)."""

    cause: dict[str, Any] | None
    culprit: dict[str, Any] | None  # change_detail row
    contributing: list[dict[str, Any]] = field(default_factory=list)
    macro: str = ""
    micro: str = ""
    remediation: str = ""
    blast: list[str] = field(default_factory=list)
    cause_service: str | None = None
    decoy_window: timedelta | None = None  # latent causes: decoys go in this window before the incident
    fix: str = "rollback"  # rollback | revert | scale | none: how the team mitigates it
    external: str | None = None  # external factor for causes that are not a change


@dataclass(frozen=True)
class Scenario:
    name: str
    services: tuple[str, ...]  # alerting services it can explain (real-anomaly matching)
    keywords: tuple[str, ...]
    build: Callable[[Any, dict[str, Any]], Outcome]
    family: str = "complex"  # complex | simple | special (real-anomaly matching)
    title: str = ""
    category: str = "code"  # code | config | infra | data | external
    causes: tuple[str, ...] = ()  # components that can be the cause (planned incidents)
    alerts: tuple[str, ...] = ()  # fixed alerting components (default: paging components in the blast radius)
    fault: str = ""  # telemetry fault signature (ff/ingest/telemetry.py), default: the scenario name
    planned: bool = True  # usable for planned (synthetic-telemetry) incidents


def _start(inc: dict[str, Any]):
    from ff.ingest.simulator import _dt

    return _dt(inc["incident_start"])


def _cause(inc: dict[str, Any], default: str | None = None) -> str:
    return inc.get("planned_cause") or default or inc["service"]


def blast_radius(svc: str) -> list[str]:
    """The component, everything that calls it (transitively) and everything hosted on it."""
    out, todo = [svc], [svc]
    while todo:
        cur = todo.pop()
        for c in config.callers(cur):
            if c not in out:
                out.append(c)
                todo.append(c)
        for s, h in config.TOPOLOGY_HOSTS.items():
            if h == cur and s not in out:
                out.append(s)
                todo.append(s)
    return out


def _benign(sim: Any, svc: str, k: int, exclude: str = "") -> list[Hunk]:
    files = [f for f, _, _ in repo.CODE_FILES[svc] if f != exclude]
    return [repo.benign_hunk(sim.rng, f) for f in sim.rng.sample(files, min(k, len(files)))]


# --------------------------------------------------------------------------- complex scenarios (original platform)
def retry_storm(sim: Any, inc: dict[str, Any]) -> Outcome:
    svc, start, rng = "compute-svc", _start(inc), sim.rng
    contrib = sim.change(svc, start - rng.uniform(3, 8) * H, "CONFIG_CHANGE", ["retry_storm"], role="contributing",
                         reason="more resilient RM calls")
    culprit = Hunk("compute/rpc/RMClient.java", "RMClient.allocate",
                   ("        backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);",),
                   ("        backoffMs = BASE_BACKOFF_MS;  // simpler: constant retry interval",),
                   ("      } catch (IOException e) {", "        attempts++;"),
                   ("        Thread.sleep(backoffMs);", "      }"))
    rel = sim.release(svc, start - rng.uniform(10, 45) * M, [culprit] + _benign(sim, svc, 2, culprit.file),
                      role="cause", messages=["Simplify retry handling in RMClient", "Improve task logging"])
    return Outcome(rel["prod"], rel["cds"][0], [contrib],
                   macro="Deploy {version_to} of compute-svc removed exponential backoff from ResourceManager calls; "
                         "combined with retry.max raised earlier it created a retry storm.",
                   micro="{file}:{line} ({symbol}) replaces exponential backoff with a constant BASE_BACKOFF_MS.",
                   remediation="Roll back compute-svc {version_to} -> {version_from}; restore "
                               "`backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS)` at {file}:{line}; revert "
                               "retry.max to its previous value.",
                   blast=blast_radius(svc) + ["storage-svc", "coord-svc"])


def election_port(sim: Any, inc: dict[str, Any]) -> Outcome:
    svc, start, rng = "coord-svc", _start(inc), sim.rng
    contrib = sim.change(svc, start - rng.uniform(1, 3) * D, "CONFIG_CHANGE", ["session_cut"], role="contributing",
                         reason="faster failure detection")
    cause = sim.change(svc, start - rng.uniform(15, 90) * M, "INFRA_CHANGE", ["sg_port_change"], role="cause",
                       reason="consolidate quorum ports")
    return Outcome(cause, sim.last_cds[0], [contrib], fix="revert",
                   macro="Terraform change to aws_security_group for coord-svc closed the leader-election port, so "
                         "quorum peers could not open election channels.",
                   micro="{file}:{line} changes the election ingress rule {key} {old} -> {new}.",
                   remediation="Restore the ingress rule for port {old} on {resource} (terraform apply of "
                               "{version_from}); add a connectivity check for 2181/2888/3888 to the pipeline.",
                   blast=blast_radius(svc))


def image_cache_sync(sim: Any, inc: dict[str, Any]) -> Outcome:
    svc, start, rng = "cloud-api", _start(inc), sim.rng
    contrib = sim.change(svc, start - rng.uniform(1, 3) * D, "INFRA_CHANGE", ["lb_hc_cut"], role="contributing",
                         reason="detect dead targets faster")
    culprit = Hunk("cloud-api/nova/image/cache.py", "ImageCacheManager.verify_base_images",
                   ("        self._executor.submit(self._verify_async, context, images)",),
                   ("        self._verify_sync(context, images)  # avoid stale cache entries",),
                   ("    def verify_base_images(self, context, images):",
                    '        """Check cached base files against the image service."""'),
                   ("        return len(images)",))
    rel = sim.release(svc, start - rng.uniform(10, 50) * M, [culprit] + _benign(sim, svc, 2, culprit.file),
                      role="cause", messages=["Verify base images before serving", "Type hints for servers API"])
    return Outcome(rel["prod"], rel["cds"][0], [contrib],
                   macro="Deploy {version_to} of cloud-api made base-image verification synchronous in the request "
                         "path; the shorter ALB health-check timeout then marked slow targets unhealthy.",
                   micro="{file}:{line} ({symbol}) calls _verify_sync inline instead of submitting it to the executor.",
                   remediation="Roll back cloud-api {version_to} -> {version_from}; restore the async executor call at "
                               "{file}:{line}; revert the aws_lb_target_group health_check timeout.",
                   blast=blast_radius(svc))


def ebs_throughput(sim: Any, inc: dict[str, Any]) -> Outcome:
    svc, start, rng = "storage-svc", _start(inc), sim.rng
    contrib = sim.change(svc, start - rng.uniform(4, 12) * H, "CONFIG_CHANGE", ["replication_up"],
                         role="contributing", reason="durability review")
    cause = sim.change(svc, start - rng.uniform(1, 3) * D, "INFRA_CHANGE", ["ebs_cut", "ebs_iops_cut"],
                       role="cause", reason="cost reduction")
    return Outcome(cause, sim.last_cds[0], [contrib], fix="revert",
                   macro="Terraform change to the storage-svc datanode EBS volume cut gp3 throughput and IOPS; the "
                         "extra replica writes after dfs.replication was raised then saturated the volume.",
                   micro="{file}:{line} sets {key} {old} -> {new} on {resource}.",
                   remediation="Restore throughput/IOPS on {resource} (terraform {version_from}); alarm on EBS "
                               "VolumeQueueLength and BurstBalance.",
                   blast=blast_radius(svc), decoy_window=12 * H)


def db_pool(sim: Any, inc: dict[str, Any]) -> Outcome:
    svc, start, rng = _cause(inc), _start(inc), sim.rng
    db = repo.DB_OF.get(svc, "meta-db")
    contrib = sim.change(db, start - rng.uniform(2, 6) * D, "INFRA_CHANGE", ["rds_downsize"], role="contributing",
                         reason="cost reduction")
    cause = sim.change(svc, start - rng.uniform(20, 120) * M, "CONFIG_CHANGE", ["pool_cut"], role="cause",
                       reason="reduce idle connections")
    return Outcome(cause, sim.last_cds[0], [contrib], fix="revert", cause_service=svc,
                   macro="Config change shrank the {svc} database connection pool; requests queued for connections and "
                         "timed out (the " + db + " downsize days earlier left no headroom).",
                   micro="{file}:{line} sets {key} {old} -> {new}.",
                   remediation="Revert {key} to {old} in {file}; right-size aws_db_instance." + db.replace("-", "_")
                               + " before lowering pools again.",
                   blast=blast_radius(svc))


def ciod_leak(sim: Any, inc: dict[str, Any]) -> Outcome:
    svc, start, rng = "node-svc", _start(inc), sim.rng
    contrib = sim.change(svc, start - rng.uniform(2, 6) * D, "INFRA_CHANGE", ["instance_down"], role="contributing",
                         reason="cost reduction")
    culprit = Hunk("node/ciod/ciod_loader.c", "ciod_load_program",
                   ("        free(image_buf);", "        return -EIO;"),
                   ("        return -EIO;  /* image_buf is reused by the next load */",),
                   ("    rc = ciod_copy_image(job, image_buf, len);", "    if (rc != 0) {"), ("    }",))
    rel = sim.release(svc, start - rng.uniform(30, 180) * M, [culprit] + _benign(sim, svc, 2, culprit.file),
                      role="cause", messages=["Reuse image buffers between loads", "Quieter RAS logging"])
    return Outcome(rel["prod"], rel["cds"][0], [contrib],
                   macro="Deploy {version_to} of node-svc leaks one program-image buffer per failed load; the smaller "
                         "instance type from the earlier launch-template change ran out of memory first.",
                   micro="{file}:{line} ({symbol}) drops free(image_buf) on the error path.",
                   remediation="Roll back node-svc {version_to} -> {version_from}; restore free(image_buf) at "
                               "{file}:{line}; add a leak check (valgrind) to the ppd suite.",
                   blast=blast_radius(svc))


def timeout_unit(sim: Any, inc: dict[str, Any]) -> Outcome:
    svc, start, rng = _cause(inc), _start(inc), sim.rng
    f = repo.CLIENT_FILE[svc]
    lang = repo.lang_of(f)
    before, after = {
        "java": ('    return conf.getLong("http.timeout_ms", 2000);',
                 '    return conf.getLong("http.timeout_ms", 2000) / 1000;  // seconds'),
        "python": ("        timeout = CONF.http.timeout_ms / 1000.0",
                   "        timeout = CONF.http.timeout_ms / 1000.0 / 1000.0  # seconds"),
        "c": ("    timeout_ms = cfg->timeout_ms;", "    timeout_ms = cfg->timeout_ms / 1000; /* seconds */"),
        "go": ("\ttimeout := time.Duration(cfg.HTTP.TimeoutMs) * time.Millisecond",
               "\ttimeout := time.Duration(cfg.HTTP.TimeoutMs) * time.Microsecond // was ms, now us"),
    }[lang]
    culprit = Hunk(f, repo.symbol_of(f), (before,), (after,), ("  // outbound call timeout",), ())
    rel = sim.release(svc, start - rng.uniform(5, 60) * M, [culprit] + _benign(sim, svc, 2, f), role="cause",
                      messages=["Normalise timeout units", "Refactor request logging"])
    return Outcome(rel["prod"], rel["cds"][0], [], cause_service=svc,
                   macro="Deploy {version_to} of {svc} shrinks its outbound timeout by a factor of 1000, so calls "
                         "time out almost immediately.",
                   micro="{file}:{line} ({symbol}) converts http.timeout_ms with the wrong unit.",
                   remediation="Roll back {svc} {version_to} -> {version_from}; fix the unit conversion at "
                               "{file}:{line}; add a unit test for the timeout conversion.",
                   blast=blast_radius(svc))


FLAG_HUNKS: dict[str, Hunk] = {
    "compute-svc": Hunk("compute/scheduler/TaskRunner.java", "TaskRunner.assignSlots", (),
                        ('    if (flags.isOn("new_scheduler")) {', "      slots = requested;  // trust the request", "    }"),
                        ("    int slots = Math.min(requested, node.freeSlots());",), ("    node.reserve(slots);",)),
    "storage-svc": Hunk("storage/datanode/PacketResponder.java", "PacketResponder.run", (),
                        ('    if (flags.isOn("fast_ack")) {', "      ackBeforeFlush = true;  // lower latency", "    }"),
                        ("    boolean ackBeforeFlush = false;",), ("    sendAck(seqno, ackBeforeFlush);",)),
    "billing-svc": Hunk("billing/invoice/InvoiceService.java", "InvoiceService.generate", (),
                        ('    if (flags.isOn("invoice_v2")) {', "      lines = repo.findAllLines(accountId);  // all history",
                         "    }"),
                        ("    List<InvoiceLine> lines = repo.findLinesSince(accountId, periodStart);",),
                        ("    return render(lines);",)),
    "auth-svc": Hunk("auth/internal/token/refresh.go", "Refresher.Refresh", (),
                     ('\tif flags.On("token_v2") {', "\t\ts.store.Delete(ctx, old.SessionID) // rotate: drop the old session",
                      "\t}"),
                     ("\tnewTok, err := s.issuer.Issue(ctx, old.AccountID)",), ("\treturn newTok, nil",)),
}


def flag_dormant(sim: Any, inc: dict[str, Any]) -> Outcome:
    svc, start, rng = _cause(inc), _start(inc), sim.rng
    if svc not in FLAG_HUNKS:
        svc = rng.choice(("compute-svc", "storage-svc")) if inc["service"] not in FLAG_HUNKS else inc["service"]
    flag = repo.FEATURE_FLAG[svc].split(".", 1)[1]
    culprit = FLAG_HUNKS[svc]
    rel = sim.release(svc, start - rng.uniform(1, 4) * D, [culprit] + _benign(sim, svc, 1, culprit.file),
                      role="contributing", messages=[f"Add {flag} code path behind a flag"])
    cause = sim.change(svc, start - rng.uniform(10, 60) * M, "CONFIG_CHANGE", ["flag_on"], role="cause",
                       reason=f"enable {flag} in prod")
    return Outcome(cause, rel["cds"][0], [rel["prod"]], fix="revert", cause_service=svc,
                   macro="Config change turned {key} on, activating a code path shipped days earlier.",
                   micro="{file}:{line} ({symbol}), deployed in {culprit_event}, runs only when the flag is on and "
                         "skips the safety check of the old path.",
                   remediation=f"Turn feature.{flag} off again in config/{svc}.yaml; fix the flag-guarded branch at "
                               "{file}:{line} before re-enabling.",
                   blast=blast_radius(svc))


# the break a multi-service feature introduces in each service (release-train cascades)
TRAIN_BREAKS: dict[str, tuple[str, Hunk, str]] = {
    "storage-svc": ("FEAT-115", Hunk("storage/datanode/BlockWriter.java", "BlockWriter.write",
                                     ("    if (pending >= FLUSH_BATCH) {",),
                                     ("    if (pending >= 1) {  // audit trail: flush every record",),
                                     ("    pending++;",), ("      flushToVolume();", "    }")),
                    "{file}:{line} ({symbol}) flushes the EBS volume on every record instead of every FLUSH_BATCH."),
    "coord-svc": ("FEAT-112", Hunk("coord/session/SessionTracker.java", "SessionTrackerImpl.run",
                                   ("      if (now - s.lastSeen > s.timeoutMs) {",),
                                   ("      if (now - s.lastSeen > s.timeoutMs / 10) {  // align with client ticks",),
                                   ("    for (Session s : sessions.values()) {",), ("        expire(s);", "      }")),
                  "{file}:{line} ({symbol}) expires sessions after a tenth of their timeout."),
    "image-svc": ("FEAT-121", Hunk("images/internal/registry/s3.go", "S3Registry.Fetch",
                                   ("\tobj, err := r.client.GetObject(ctx, &s3.GetObjectInput{Bucket: b, Key: k})",),
                                   ("\tobj, err := r.client.GetObject(ctx, &s3.GetObjectInput{Bucket: b, Key: k, "
                                    "Range: aws.String(layerRange(k))}) // lazy pull",),
                                   ("\tk := r.key(image, layer)",), ("\tif err != nil {", "\t\treturn nil, err")),
                  "{file}:{line} ({symbol}) requests a byte range computed for the new layer format, which old base "
                  "images do not have (S3 returns InvalidRange)."),
    "notification-svc": ("FEAT-125", Hunk("notify/templates/render.py", "render_template",
                                          ("    html = TEMPLATES[name].render(**ctx)",),
                                          ('    html = TEMPLATES[f"{name}_v2"].render(**ctx)  # invoice v2 layout',),
                                          ("def render_template(name, ctx):",), ("    return html",)),
                         "{file}:{line} ({symbol}) always loads the *_v2 template, which only exists for invoices."),
    "billing-svc": ("FEAT-132", Hunk("billing/repo/InvoiceRepository.java", "InvoiceRepository.findOpenByAccount",
                                     ('  @Query("select i from Invoice i where i.accountId = :acct and i.state = OPEN")',),
                                     ('  @Query("select i from Invoice i join fetch i.lines where i.accountId = :acct")',),
                                     ("  // used by /v1/usage and the invoice job",), ("  List<Invoice> find(Long acct);",)),
                    "{file}:{line} ({symbol}) fetches every invoice with all its lines for the usage API."),
    "auth-svc": ("FEAT-116", Hunk("auth/internal/token/issuer.go", "Issuer.Issue",
                                  ("\tclaims.ExpiresAt = now.Add(i.ttl)",), ("\tclaims.ExpiresAt = now.Add(i.ttl / 60)",),
                                  ("\tclaims := newClaims(acct)",), ("\treturn i.sign(claims)",)),
                 "{file}:{line} ({symbol}) divides the token lifetime by 60 (minutes vs seconds)."),
}


def cross_service(sim: Any, inc: dict[str, Any]) -> Outcome:
    """Release-train cascade: a multi-service feature rolls out callee-first; the break is in a
    service at least one hop below the alerting one, so the investigation has to follow the
    dependency chain. The later (caller) parts of the same feature deploy closer to the incident
    and are harmless."""
    from ff.ingest import cycles

    svc, start, rng = inc["service"], _start(inc), sim.rng
    target = inc.get("planned_cause") or rng.choice(("storage-svc", "coord-svc"))
    ticket, culprit, micro = TRAIN_BREAKS[target]
    feat = cycles.feature_ref(cycles.FEATURE_BY_TICKET[ticket])
    t0 = start - rng.uniform(6, 10) * H
    rel = sim.release(target, t0, [culprit] + _benign(sim, target, 1, culprit.file), role="cause", features=[feat],
                      messages=[f"{feat['title']}: {target} part"])
    path = config.dependency_path(svc, target)[:-1][::-1]  # callers of the target, nearest first
    later = []
    for k, s in enumerate(x for x in path if x in config.CODE_SERVICES):
        t = t0 + (k + 1) * rng.uniform(1.5, 2.5) * H
        later.append(sim.release(s, t, _benign(sim, s, 1), role="train", features=[feat],
                                 messages=[f"{feat['title']}: {s} part"])["prod"])
    inc.setdefault("_decoys", []).extend(e["id"] for e in later)
    return Outcome(rel["prod"], rel["cds"][0], [], cause_service=target, blast=blast_radius(target),
                   macro="Release train {cycle} shipped " + feat["ticket"] + " (" + feat["title"] + ") across services; "
                         "the {svc} part ({version_to}) broke, and the failure surfaced in {alert_svc} through the "
                         "dependency chain. The later parts of the same feature were harmless.",
                   micro=micro,
                   remediation="Roll back {svc} {version_to} -> {version_from} (the other services' parts of "
                               + feat["ticket"] + " can stay); fix {file}:{line}; add a cross-service load test to "
                               "the train.")


MIGRATIONS: dict[str, tuple[str, str, str]] = {
    # service -> (table, blocking DDL, code line that uses it)
    "compute-svc": ("jobs", "ALTER TABLE jobs ADD COLUMN audit_id BIGINT NOT NULL DEFAULT 0;",
                    "    job.setAuditId(auditId);"),
    "billing-svc": ("invoice_lines", "ALTER TABLE invoice_lines ALTER COLUMN amount TYPE NUMERIC(20,6);",
                    "    line.setAmount(amount.setScale(6));"),
    "cloud-api": ("servers", "CREATE INDEX ix_servers_tags ON servers USING gin (tags);",
                  "        query = query.filter(Server.tags.contains(tags))"),
}


def db_migration(sim: Any, inc: dict[str, Any]) -> Outcome:
    """A blocking schema migration on a shared RDS database, shipped with a feature; a hotfix patch
    applied afterwards (to the wrong thing) sits between the cause and the incident."""
    from ff.ingest import cycles

    start, rng = _start(inc), sim.rng
    target = inc.get("planned_cause") or "compute-svc"
    db = repo.DB_OF[target]
    table, ddl, use = MIGRATIONS[target]
    ticket = {"compute-svc": rng.choice(("FEAT-104", "FEAT-115")), "billing-svc": "FEAT-126",
              "cloud-api": "FEAT-133"}[target]
    feat = cycles.feature_ref(cycles.FEATURE_BY_TICKET[ticket])
    contrib = sim.change(db, start - rng.uniform(2, 6) * D, "INFRA_CHANGE", ["rds_downsize"],
                         role="contributing", reason="cost reduction")
    ver = rng.randint(40, 79)
    culprit = Hunk(f"{repo.MIGRATION_DIR[target]}/V{ver}__{table}_{feat['ticket'].lower().replace('-', '')}.sql",
                   "migration", (), (ddl,), (f"-- {feat['ticket']} {feat['title']}",), ("COMMIT;",), "sql")
    code_file = repo.CODE_FILES[target][0]
    code = Hunk(code_file[0], code_file[1], (), (use,), ("    // " + feat["title"],), ())
    t = start - rng.uniform(15, 45) * M
    rel = sim.release(target, t, [culprit, code] + _benign(sim, target, 1, code.file), role="cause", features=[feat],
                      messages=[f"{feat['title']}: schema change on {table}"])
    patch = sim.patch(target, t + (start - t) * rng.uniform(0.4, 0.8), role="decoy",
                      message="Raise client log level while investigating slow requests")
    inc.setdefault("_decoys", []).append(patch["id"])
    return Outcome(rel["prod"], rel["cds"][0], [contrib], cause_service=target,
                   blast=list(dict.fromkeys([db] + blast_radius(target))),
                   macro="Deploy {version_to} of {svc} ran a blocking migration on the " + db + " " + table + " table "
                         "(RDS); writes queued behind the table lock and callers timed out. The hotfix patch applied "
                         "afterwards did not touch the DB.",
                   micro="{file}:{line} runs DDL that rewrites " + table + " under an ACCESS EXCLUSIVE lock.",
                   remediation="Cancel the migration / roll back {svc} {version_to} -> {version_from}; re-run it "
                               "online (CONCURRENTLY / nullable column, batched backfill); review " + db + " sizing.")


# --------------------------------------------------------------------------- code bugs (any service)
NULL_HUNKS: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "java": (("    if (event.getAccount() == null) {", "      return Result.skipped();", "    }"), (),
             ("  public Result handle(Event event) {",), ("    Account acct = event.getAccount().resolve();",)),
    "python": (("    if item is None:", "        return None"), (), ("def handle(self, item):",),
               ("    return item.owner.account_id",)),
    "go": (("\tif sess == nil {", "\t\treturn nil, ErrNotFound", "\t}"), (), ("\tsess, _ := s.lookup(ctx, id)",),
           ("\treturn sess.Account, nil",)),
    "c": (("    if (!job->image)", "        return -ENOENT;"), (), ("    struct job *job = lookup(id);",),
          ("    return load(job->image->path);",)),
}


def null_deref(sim: Any, inc: dict[str, Any]) -> Outcome:
    svc, start, rng = _cause(inc), _start(inc), sim.rng
    f, sym, lang = rng.choice(repo.CODE_FILES[svc])
    before, after, cb, ca = NULL_HUNKS.get(lang, NULL_HUNKS["java"])
    culprit = Hunk(f, sym, before, after, cb, ca)
    rel = sim.release(svc, start - rng.uniform(5, 90) * M, [culprit] + _benign(sim, svc, 1, f), role="cause",
                      messages=[f"Simplify {sym.split('.')[-1]}: remove redundant check", "Tidy imports"])
    return Outcome(rel["prod"], rel["cds"][0], cause_service=svc, blast=blast_radius(svc),
                   macro="Deploy {version_to} of {svc} removed a null/nil guard; requests for records without the "
                         "optional field now crash the handler.",
                   micro="{file}:{line} ({symbol}) drops the null check before dereferencing the field.",
                   remediation="Roll back {svc} {version_to} -> {version_from}; restore the guard at {file}:{line}; "
                               "add a test with the optional field missing.")


N1_HUNKS: dict[str, Hunk] = {
    "cloud-api": Hunk("cloud-api/nova/api/servers.py", "ServersController.detail",
                      ("        servers = q.options(joinedload(Server.flavor)).all()",),
                      ("        servers = q.all()", "        for s in servers:",
                       "            s.flavor = self.db.query(Flavor).get(s.flavor_id)  # per row"),
                      ("        q = self.db.query(Server).filter_by(tenant=ctx.tenant)",), ("        return servers",)),
    "billing-svc": Hunk("billing/invoice/InvoiceService.java", "InvoiceService.generate",
                        ("    List<InvoiceLine> lines = repo.findLinesWithPrices(accountId);",),
                        ("    List<InvoiceLine> lines = repo.findLines(accountId);",
                         "    lines.forEach(l -> l.setPrice(prices.findById(l.getSku())));  // one query per line"),
                        ("  public Invoice generate(Long accountId) {",), ("    return render(lines);",)),
    "auth-svc": Hunk("auth/internal/account/repo.go", "AccountRepo.FindByID",
                     ('\trows, err := r.db.Query(ctx, "SELECT a.*, r.name FROM accounts a JOIN roles r ON ...", id)',),
                     ('\tacct, err := r.one(ctx, "SELECT * FROM accounts WHERE id=$1", id)',
                      "\tfor _, rid := range acct.RoleIDs {",
                      '\t\tacct.Roles = append(acct.Roles, r.role(ctx, rid)) // one query per role', "\t}"),
                     ("func (r *AccountRepo) FindByID(ctx context.Context, id string) (*Account, error) {",),
                     ("\treturn acct, err",)),
}


def n_plus_one(sim: Any, inc: dict[str, Any]) -> Outcome:
    svc, start, rng = _cause(inc, "cloud-api"), _start(inc), sim.rng
    culprit = N1_HUNKS[svc]
    db = repo.DB_OF[svc]
    rel = sim.release(svc, start - rng.uniform(20, 120) * M, [culprit] + _benign(sim, svc, 1, culprit.file),
                      role="cause", messages=["Simplify the query, drop the eager join", "Add request metrics"])
    return Outcome(rel["prod"], rel["cds"][0], cause_service=svc,
                   blast=list(dict.fromkeys([db] + blast_radius(svc))),
                   macro="Deploy {version_to} of {svc} replaced a join with one query per row (N+1); " + db
                         + " CPU saturated under peak traffic and requests timed out.",
                   micro="{file}:{line} ({symbol}) queries the database inside the loop.",
                   remediation="Roll back {svc} {version_to} -> {version_from}; restore the eager join at {file}:{line}; "
                               "add a query-count assertion to the integration tests.")


LEAK_HUNKS: dict[str, Hunk] = {
    "image-svc": Hunk("images/internal/cache/lru.go", "LRU.Get",
                      ("\t\tc.evictOldest()",), ("\t\t// eviction moved to the background janitor (FEAT-122)",),
                      ("\tif c.size > c.max {",), ("\t}",)),
    "billing-svc": Hunk("billing/consumer/UsageEventConsumer.java", "UsageEventConsumer.onMessage", (),
                        ("    SEEN.put(msg.getMessageId(), msg);  // de-duplicate redeliveries",),
                        ("  public void onMessage(Message msg) {",), ("    process(parse(msg));",)),
    "cloud-api": Hunk("cloud-api/nova/api/servers.py", "ServersController.detail", (),
                      ("        _AUDIT.append((ctx.request_id, servers))  # keep for audit",),
                      ("        servers = self._list(req)",), ("        return self._view(servers)",)),
    "auth-svc": Hunk("auth/internal/session/store.go", "RedisStore.Get", (),
                     ("\ts.local[id] = sess // local copy to skip Redis",), ("\tsess, err := s.fetch(ctx, id)",),
                     ("\treturn sess, err",)),
}


def memory_leak(sim: Any, inc: dict[str, Any]) -> Outcome:
    svc, start, rng = _cause(inc, "image-svc"), _start(inc), sim.rng
    culprit = LEAK_HUNKS[svc]
    rel = sim.release(svc, start - rng.uniform(6, 30) * H, [culprit] + _benign(sim, svc, 1, culprit.file),
                      role="cause", messages=["Cache hot entries in memory", "Refactor handlers"])
    return Outcome(rel["prod"], rel["cds"][0], cause_service=svc, blast=blast_radius(svc), decoy_window=8 * H,
                   macro="Deploy {version_to} of {svc} added an unbounded in-memory structure; memory grew for hours "
                         "until tasks were OOM-killed and restarted in a loop.",
                   micro="{file}:{line} ({symbol}) adds entries that are never evicted.",
                   remediation="Roll back {svc} {version_to} -> {version_from}; bound or remove the structure at "
                               "{file}:{line}; alert on memory growth per task.")


def contract_break(sim: Any, inc: dict[str, Any]) -> Outcome:
    """The producer (cloud-api) ships the v2 usage-event schema before the consumer (billing-svc):
    the consumer rejects every message, the queue backs up and messages land in the DLQ."""
    from ff.ingest import cycles

    start, rng = _start(inc), sim.rng
    feat = cycles.feature_ref(cycles.FEATURE_BY_TICKET["FEAT-124"])
    culprit = Hunk("cloud-api/nova/usage/publisher.py", "UsagePublisher.publish",
                   ('            "instance_type": server.flavor.name,',),
                   ('            "flavor": {"name": server.flavor.name, "vcpus": server.flavor.vcpus},',),
                   ("        event = {", '            "account_id": server.tenant,'),
                   ('            "hours": hours,', "        }"))
    rel = sim.release("cloud-api", start - rng.uniform(10, 60) * M, [culprit] + _benign(sim, "cloud-api", 1,
                                                                                         culprit.file),
                      role="cause", features=[feat], messages=[f"{feat['title']}: publish v2 events"])
    return Outcome(rel["prod"], rel["cds"][0], cause_service="cloud-api",
                   blast=["cloud-api", "usage-queue", "billing-svc", "notification-svc"],
                   macro="Deploy {version_to} of cloud-api started publishing the v2 usage-event schema ("
                         + feat["ticket"] + ") before billing-svc could read it; billing-svc rejected every event and "
                         "the usage queue backed up into the DLQ.",
                   micro="{file}:{line} ({symbol}) replaces the instance_type field with a nested flavor object.",
                   remediation="Roll back cloud-api {version_to} -> {version_from}; redrive the DLQ after billing-svc "
                               "accepts both schemas; deploy consumers before producers.")


UPGRADES: dict[str, tuple[str, str, str, str]] = {
    # service -> (before, after, context, what broke)
    "billing-svc": ("      <version>5.0.1</version>", "      <version>5.1.0</version>",
                    "      <artifactId>HikariCP</artifactId>",
                    "HikariCP 5.1 validates connections with a new keepalive that the RDS proxy closes"),
    "notification-svc": ("urllib3==1.26.18", "urllib3==2.2.1", "requests==2.31.0",
                         "urllib3 2 requires OpenSSL 1.1.1+, the Lambda base image ships 1.0.2 -> TLS handshakes fail"),
    "auth-svc": ("\tgithub.com/redis/go-redis/v9 v9.0.5", "\tgithub.com/redis/go-redis/v9 v9.5.1", "require (",
                 "go-redis 9.5 enables client-side caching handshakes (CLIENT TRACKING) the cluster rejects"),
    "image-svc": ("\tgithub.com/aws/aws-sdk-go-v2/service/s3 v1.40.0", "\tgithub.com/aws/aws-sdk-go-v2/service/s3 v1.58.0",
                  "require (", "the new S3 client sends checksum headers the bucket policy denies"),
    "api-gateway": ("PyJWT==2.7.0", "PyJWT==2.8.0", "cryptography==41.0.7",
                    "PyJWT 2.8 rejects tokens whose 'iat' is in the future (clock skew with auth-svc)"),
}


def dependency_upgrade(sim: Any, inc: dict[str, Any]) -> Outcome:
    svc, start, rng = _cause(inc, "billing-svc"), _start(inc), sim.rng
    before, after, ctx, why = UPGRADES[svc]
    f = repo.BUILD_FILES[svc]
    culprit = Hunk(f, f.rsplit("/", 1)[-1], (before,), (after,), (ctx,), ())
    rel = sim.release(svc, start - rng.uniform(10, 90) * M, [culprit] + _benign(sim, svc, 1), role="cause",
                      messages=["Bump dependencies (security scan findings)", "Minor logging tweaks"])
    return Outcome(rel["prod"], rel["cds"][0], cause_service=svc, blast=blast_radius(svc),
                   macro="Deploy {version_to} of {svc} was a routine dependency bump; " + why + ".",
                   micro="{file}:{line} upgrades the library: " + why + ".",
                   remediation="Roll back {svc} {version_to} -> {version_from}; pin the previous version in {file} "
                               "until the upgrade is tested against the real dependency.")


LOCK_HUNKS: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "java": (("  public Result handle(Request r) {",), ("  public synchronized Result handle(Request r) {  // HOTFIX race",),
             ("    return process(r);",)),
    "python": (("        return self._process(req)",), ("        with _GLOBAL_LOCK:  # HOTFIX race on shared state",
                                                          "            return self._process(req)"), ()),
    "go": ((), ("\tglobalMu.Lock() // HOTFIX race", "\tdefer globalMu.Unlock()"), ("\treturn s.process(ctx, req)",)),
    "c": ((), ("    pthread_mutex_lock(&global_lock);  /* HOTFIX race */",), ("    rc = handle(req);",)),
}


def hotfix_regression(sim: Any, inc: dict[str, Any]) -> Outcome:
    """A hotfix PATCH applied straight to prod fixes a race with a global lock and serialises every
    request: the patch, not the earlier release, is the root cause."""
    svc, start, rng = _cause(inc), _start(inc), sim.rng
    f, sym, lang = rng.choice(repo.CODE_FILES[svc])
    before, after, ca = LOCK_HUNKS.get(lang, LOCK_HUNKS["java"])
    earlier = sim.release(svc, start - rng.uniform(1, 3) * D, _benign(sim, svc, 2), role="background")
    inc.setdefault("_decoys", [])
    t = start - rng.uniform(20, 90) * M
    patch = sim.patch(svc, t, role="cause", message=f"Serialise {sym.split('.')[-1]} to fix a data race",
                      hunks=[Hunk(f, sym, before, after, (), ca)],
                      bug="concurrent requests corrupt the shared state (seen twice after the last release)")
    return Outcome(patch, sim.last_cds[0], [earlier["prod"]], cause_service=svc, blast=blast_radius(svc),
                   macro="Hotfix patch {version_to} of {svc} fixed a race by adding a global lock; every request then "
                         "waited on that lock and latency exploded under load.",
                   micro="{file}:{line} ({symbol}) wraps the whole request in one global lock.",
                   remediation="Roll back {svc} {version_to} -> {version_from}; fix the race with a per-key lock at "
                               "{file}:{line}; load-test hotfixes before prod.")


def missing_index(sim: Any, inc: dict[str, Any]) -> Outcome:
    svc, start, rng = _cause(inc, "billing-svc"), _start(inc), sim.rng
    db = repo.DB_OF[svc]
    table, index = {"billing-svc": ("invoice_lines", "ix_invoice_lines_account"), "cloud-api": ("servers",
                                                                                               "ix_servers_tenant"),
                    "compute-svc": ("jobs", "ix_jobs_state"), "auth-svc": ("sessions", "ix_sessions_account")}[svc]
    ver = rng.randint(80, 99)
    culprit = Hunk(f"{repo.MIGRATION_DIR[svc]}/V{ver}__cleanup_indexes.sql", "migration", (),
                   (f"DROP INDEX IF EXISTS {index};  -- unused according to pg_stat_user_indexes (replica only)",),
                   ("-- index cleanup (storage cost)",), ("COMMIT;",), "sql")
    rel = sim.release(svc, start - rng.uniform(1, 6) * H, [culprit] + _benign(sim, svc, 1), role="cause",
                      messages=["Drop unused indexes", "Refactor repository layer"])
    return Outcome(rel["prod"], rel["cds"][0], cause_service=svc, blast=list(dict.fromkeys([db] + blast_radius(svc))),
                   macro="Deploy {version_to} of {svc} dropped index " + index + " on " + db + "." + table + " (it looked "
                         "unused on the replica); the hot query fell back to sequential scans and timed out at peak.",
                   micro="{file}:{line} drops " + index + ".",
                   remediation=f"Recreate {index} CONCURRENTLY on {db}; roll back {{svc}} if needed; check index usage "
                               "on the primary before dropping.", decoy_window=6 * H)


# --------------------------------------------------------------------------- parameter scenarios (config / infra)
def _param(etype: str, intents: list[str], lag: tuple[float, float], unit: timedelta, macro: str, micro: str,
           remediation: str, default_cause: str | None = None, reason: str | None = None,
           contributing: tuple[str, str, str, tuple[float, float], timedelta] | None = None,
           decoy_window: timedelta | None = None, blast_extra: tuple[str, ...] = ()
           ) -> Callable[[Any, dict[str, Any]], Outcome]:
    """A scenario whose cause is a config / Terraform change on the cause component.

    ``contributing``: (component or 'cause', etype, intent, lag range, unit) of an earlier change."""
    def build(sim: Any, inc: dict[str, Any]) -> Outcome:
        svc, start, rng = _cause(inc, default_cause), _start(inc), sim.rng
        contrib = []
        if contributing:
            comp, cet, cint, clag, cunit = contributing
            comp = svc if comp == "cause" else comp
            contrib.append(sim.change(comp, start - rng.uniform(*clag) * cunit, cet, [cint], role="contributing"))
        cause = sim.change(svc, start - rng.uniform(*lag) * unit, etype, intents, reason=reason, role="cause")
        return Outcome(cause, sim.last_cds[0], contrib, macro=macro, micro=micro, remediation=remediation,
                       cause_service=svc, fix="revert", decoy_window=decoy_window,
                       blast=list(dict.fromkeys(blast_radius(svc) + list(blast_extra))))
    return build


def config_simple(sim: Any, inc: dict[str, Any]) -> Outcome:
    svc = _cause(inc)
    from ff.ingest.simulator import intent_key

    ok = [i for i in ("timeout_cut", "pool_cut", "retry_storm") if repo.param_spec(intent_key(i, svc)).applies(svc)]
    intent = sim.rng.choice(ok)
    return _param("CONFIG_CHANGE", [intent], (20, 360), M, "Config change to {key} in {svc} broke calls under load.",
                  "{file}:{line} sets {key} {old} -> {new}.", "Revert {key} to {old} in {file}.")(sim, inc)


def _deploy_scn(kind: dict[str, Hunk], skip_load: bool = False, dependency: bool = False,
                partial: bool = False) -> Callable[[Any, dict[str, Any]], Outcome]:
    def build(sim: Any, inc: dict[str, Any]) -> Outcome:
        svc, start, rng = inc["service"], _start(inc), sim.rng
        deps = [d for d in config.dependencies(svc) if d in config.CODE_SERVICES]
        tsvc = inc.get("planned_cause") or (rng.choice(deps) if dependency and deps else svc)
        f = sim.relevant_file(tsvc, inc.get("error_templates", []))
        culprit = repo.breaking_hunk(kind, f)
        lag = (60, 180) if partial else ((15, 90) if skip_load else (5, 60))
        rel = sim.release(tsvc, start - rng.uniform(*lag) * M, [culprit] + _benign(sim, tsvc, rng.randint(1, 2), f),
                          role="cause", skip_load=skip_load)
        out = Outcome(rel["prod"], rel["cds"][0], cause_service=tsvc, blast=blast_radius(tsvc))
        out.macro = "Deploy {version_to} of {svc} introduced a regression" + (
            " that only the skipped ppd load suite would have caught." if skip_load else ".")
        if dependency:
            out.macro = "Deploy {version_to} of {svc}, a dependency of the alerting service, introduced a regression."
        out.micro = "{file}:{line} ({symbol}) changes the condition / constant shown in the diff."
        out.remediation = "Roll back {svc} {version_to} -> {version_from}; revert {file}:{line}."
        if skip_load:
            out.remediation += " Make the ppd load suite mandatory."
        if partial:
            rb = sim.rollback(tsvc, start - rng.uniform(15, 40) * M, partial=True, role="decoy",
                              reason="error budget alert; rollout halted after 2 of 5 nodes")
            inc.setdefault("_decoys", []).append(rb["id"])
            out.remediation = "Complete the rollback of {svc} to {version_from} on all nodes; revert {file}:{line}."
        return out
    return build


EXTERNAL: dict[str, tuple[str, str]] = {
    # scenario -> (what happened, how it was handled)
    "TRAFFIC_SPIKE": ("a customer's batch integration sent 12x its usual request rate for 40 minutes",
                      "per-customer throttling for that account; no change was rolled back"),
    "CERT_EXPIRY": ("the TLS certificate of a large customer's webhook endpoint expired",
                    "the customer renewed the certificate; webhook failures for that endpoint are now alerted "
                    "separately"),
    "AWS_S3_DEGRADED": ("Amazon S3 in us-east-1 returned elevated 503 SlowDown errors (AWS Health event)",
                        "waited for AWS to recover; added jittered retries and a regional fallback read"),
    "SES_THROTTLED": ("Amazon SES throttled the account after a bounce-rate review by AWS",
                      "AWS support restored the sending quota; bounce handling was improved"),
}


def no_change(sim: Any, inc: dict[str, Any]) -> Outcome:
    ext = EXTERNAL.get(inc.get("scenario_type", ""))
    what, how = ext if ext else ("the logs show the anomaly without a preceding change", "tuned the detector")
    return Outcome(None, None, fix="none", external=what,
                   macro=f"No change explains the anomaly: {what}.",
                   remediation=f"No rollback needed; {how}.",
                   blast=blast_radius(inc.get("planned_cause") or inc["service"]))


def _scn(name: str, title: str, category: str, build: Callable[[Any, dict[str, Any]], Outcome],
         causes: tuple[str, ...], alerts: tuple[str, ...] = (), fault: str = "") -> Scenario:
    """A scenario for planned incidents only."""
    return Scenario(name, (), (), build, "planned", title, category, causes, alerts, fault or name)


SCENARIOS: dict[str, Scenario] = {s.name: s for s in (
    # ---- original platform (also matched to real LogHub anomalies by keywords)
    Scenario("RETRY_STORM", ("compute-svc",), ("contacting rm", "renew lease", "retry", "address change"),
             retry_storm, title="Retry storm after backoff removal", category="code", causes=("compute-svc",)),
    Scenario("ELECTION_PORT", ("coord-svc",), ("election", "open channel", "connection broken", "sendworker",
                                               "session", "connection"), election_port,
             title="Security group closed the election port", category="infra", causes=("coord-svc",)),
    Scenario("CROSS_SERVICE", ("cloud-api",), ("base file", "instances", "synchroniz", "hypervisor", "server"),
             cross_service, title="Release-train cascade down the dependency chain", category="code",
             causes=tuple(TRAIN_BREAKS)),
    Scenario("IMAGE_CACHE_SYNC", ("cloud-api",), ("base file", "instances", "image"), image_cache_sync,
             title="Synchronous image verification in the request path", category="code", causes=("cloud-api",)),
    Scenario("DB_MIGRATION", ("cloud-api", "compute-svc"), ("instances", "power states", "database", "timeout",
                                                            "contacting", "lease"),
             db_migration, title="Blocking DB migration on a shared RDS database", category="data",
             causes=tuple(MIGRATIONS)),
    Scenario("EBS_THROUGHPUT", ("storage-svc",), ("serving blk", "exception while serving", "blk_", "write"),
             ebs_throughput, title="EBS gp3 throughput cut", category="infra", causes=("storage-svc",)),
    Scenario("DB_POOL", ("compute-svc", "cloud-api"), ("timeout", "time out", "connection", "no route"), db_pool,
             title="DB pool cut on a downsized RDS", category="config",
             causes=("cloud-api", "compute-svc", "auth-svc", "billing-svc")),
    Scenario("CIOD_LEAK", ("node-svc",), ("ciod", "program image", "error loading"), ciod_leak,
             title="Memory leak in the program loader", category="code", causes=("node-svc",)),
    Scenario("TIMEOUT_UNIT", tuple(config.LOGHUB_SERVICES), ("timed out", "timeout", "time out", "socket"),
             timeout_unit, title="Timeout unit conversion bug", category="code", causes=CODE),
    Scenario("FLAG_DORMANT", ("compute-svc", "storage-svc"), ("task", "container", "attempt", "cleanup", "responder"),
             flag_dormant, title="Feature flag enabled a dormant code path", category="config",
             causes=tuple(FLAG_HUNKS)),
    # simple (single change)
    Scenario("CONFIG_CHANGE", tuple(config.LOGHUB_SERVICES), ("connection", "timeout", "time out", "refused",
                                                               "session"),
             config_simple, "simple", "Bad config value", "config", causes=CODE),
    Scenario("LATENT_DISK", tuple(config.LOGHUB_SERVICES), ("disk", "storage", "kernstor", "space"),
             _param("INFRA_CHANGE", ["disk_cut"], (10, 20), D,
                    "Terraform change shrank {resource} days earlier; the disk filled up.",
                    "{file}:{line} sets {key} {old} -> {new} on {resource}.",
                    "Restore {key} = {old} on {resource} (terraform {version_from}).", decoy_window=10 * H),
             "simple", "Latent disk shrink", "infra", causes=("storage-svc", "node-svc", "compute-svc")),
    Scenario("MEMORY_INFRA", tuple(config.LOGHUB_SERVICES), ("memory", "tlb", "kerndtlb", "outofmemory"),
             _param("INFRA_CHANGE", ["instance_down"], (1, 5), D,
                    "Launch-template change moved {svc} to a smaller instance type.",
                    "{file}:{line} sets {key} {old} -> {new} on {resource}.",
                    "Restore {key} = {old} on {resource} (terraform {version_from}).", decoy_window=10 * H),
             "simple", "Instance downsize", "infra", causes=("node-svc", "compute-svc", "cloud-api")),
    Scenario("MEMORY_HEAP", ("compute-svc", "storage-svc", "coord-svc"), ("heap", "memory", "gc"),
             _param("CONFIG_CHANGE", ["heap_cut"], (1, 12), H, "Config change cut the {svc} JVM heap.",
                    "{file}:{line} sets {key} {old} -> {new}.", "Revert {key} to {old} in {file}."),
             "simple", "Heap cut", "config", causes=("compute-svc", "storage-svc", "billing-svc")),
    Scenario("BAD_DEPLOY", tuple(config.LOGHUB_SERVICES), (), _deploy_scn(repo.BREAKING), "simple", "Bad deploy",
             "code", causes=CODE),
    Scenario("DEPENDENCY_DEPLOY", ("cloud-api", "compute-svc"), (), _deploy_scn(repo.BREAKING, dependency=True),
             "special", "Deploy in a dependency", "code",
             causes=("compute-svc", "image-svc", "auth-svc", "billing-svc", "notification-svc", "storage-svc"),
             planned=False),
    Scenario("SKIPPED_TEST", tuple(config.LOGHUB_SERVICES), (), _deploy_scn(repo.PERF, skip_load=True), "special",
             "Regression behind a skipped load suite", "code", causes=CODE),
    Scenario("PARTIAL_ROLLBACK", tuple(config.LOGHUB_SERVICES), (), _deploy_scn(repo.BREAKING, partial=True),
             "special", "Partial rollback of a bad deploy", "code", causes=CODE),
    Scenario("NO_CHANGE", tuple(config.LOGHUB_SERVICES), (), no_change, "special", "No change explains it",
             "external", causes=CODE),
    # ---- code bugs (planned)
    _scn("NULL_DEREF", "Null dereference after a removed guard", "code", null_deref, CODE),
    _scn("N_PLUS_ONE", "N+1 queries saturate the database", "code", n_plus_one, tuple(N1_HUNKS)),
    _scn("MEMORY_LEAK", "Unbounded in-memory cache, OOM-killed tasks", "code", memory_leak, tuple(LEAK_HUNKS)),
    _scn("CONTRACT_BREAK", "Producer shipped a new event schema before the consumer", "code", contract_break,
         ("cloud-api",), alerts=("billing-svc",)),
    _scn("DEPENDENCY_UPGRADE", "Library upgrade changed runtime behaviour", "code", dependency_upgrade,
         tuple(UPGRADES)),
    _scn("HOTFIX_REGRESSION", "Hotfix patch serialised requests with a global lock", "code", hotfix_regression,
         tuple(s for s in CODE if s != "node-svc")),
    _scn("MISSING_INDEX", "Dropped index, sequential scans at peak", "data", missing_index,
         ("billing-svc", "cloud-api", "compute-svc", "auth-svc")),
    # ---- config errors (planned)
    _scn("CACHE_TTL", "Session TTL typo, cache miss storm on the DB", "config",
         _param("CONFIG_CHANGE", ["ttl_typo"], (15, 120), M,
                "Config change set {key} to {new} instead of {old}; almost every token check missed the Redis cache "
                "and hit meta-db, which saturated.", "{file}:{line} sets {key} {old} -> {new}.",
                "Revert {key} to {old} in {file}; add bounds validation for TTLs.", "auth-svc",
                reason="shorter sessions for the security review", blast_extra=("meta-db",)), ("auth-svc",)),
    _scn("CONSUMER_CONCURRENCY", "Consumer concurrency raised past the DB connection limit", "config",
         _param("CONFIG_CHANGE", ["concurrency_up"], (20, 180), M,
                "Config change raised {key} {old} -> {new} to drain the usage backlog faster; every consumer thread "
                "held a billing-db connection and the database refused new ones.",
                "{file}:{line} sets {key} {old} -> {new}.",
                "Revert {key} to {old}; size consumer concurrency against db.pool.max_size and RDS max_connections.",
                "billing-svc", reason="drain the usage backlog faster", blast_extra=("billing-db",)), ("billing-svc",)),
    _scn("WEBHOOK_RETRY", "Webhook retries without jitter exhausted Lambda concurrency", "config",
         _param("CONFIG_CHANGE", ["webhook_retries_up"], (30, 240), M,
                "Config change raised {key} {old} -> {new}; one failing customer endpoint multiplied invocations until "
                "notification-svc hit its Lambda concurrency limit and invoice e-mails were throttled.",
                "{file}:{line} sets {key} {old} -> {new}.",
                "Revert {key} to {old}; add exponential backoff with jitter and a per-endpoint circuit breaker.",
                "notification-svc", reason="fewer lost webhooks"), ("notification-svc",),
         alerts=("notification-svc", "billing-svc")),
    _scn("BREAKER_SENSITIVE", "Circuit breaker threshold too low, breaker stuck open", "config",
         _param("CONFIG_CHANGE", ["breaker_sensitive"], (20, 240), M,
                "Config change set {key} {old} -> {new}; ordinary error blips opened the circuit breaker and {svc} "
                "returned 503 for healthy downstreams.", "{file}:{line} sets {key} {old} -> {new}.",
                "Revert {key} to {old} in {file}.", reason="fail fast on downstream errors"),
         ("api-gateway", "cloud-api", "billing-svc")),
    # ---- infrastructure (planned)
    _scn("RDS_MAX_CONNECTIONS", "RDS parameter group cut max_connections", "infra",
         _param("INFRA_CHANGE", ["rds_max_conn_cut"], (2, 20), H,
                "Terraform change to the {svc} parameter group cut max_connections {old} -> {new}; after the next "
                "reboot the services' pools exceeded it and new connections were refused.",
                "{file}:{line} sets max_connections {old} -> {new} on {resource}.",
                "Restore max_connections = {old} on {resource} and reboot in the maintenance window.",
                reason="align with the RDS Proxy rollout"), ("meta-db", "billing-db")),
    _scn("RDS_DOWNSIZE", "RDS instance class downsized", "infra",
         _param("INFRA_CHANGE", ["rds_downsize"], (1, 4), D,
                "Terraform change downsized {resource} {old} -> {new}; at the next traffic peak CPU hit 100 % and "
                "queries timed out.", "{file}:{line} sets {key} {old} -> {new} on {resource}.",
                "Restore {key} = {old} on {resource}; alarm on CPU and DBLoad before downsizing.",
                reason="cost reduction", decoy_window=10 * H), ("meta-db", "billing-db")),
    _scn("RDS_MAINTENANCE", "Engine upgrade applied immediately at peak", "infra",
         _param("INFRA_CHANGE", ["rds_apply_now", "rds_engine_upgrade"], (5, 30), M,
                "Terraform change upgraded {svc} with apply_immediately = true; the Multi-AZ failover happened at "
                "peak and JVM clients kept stale connections and DNS for minutes.",
                "{file}:{line} sets {key} {old} -> {new} on {resource}.",
                "Set apply_immediately back to false (maintenance window); cap JVM DNS TTL at 30 s; enable pool "
                "validation.", reason="security patch for the engine"), ("meta-db", "billing-db")),
    _scn("SECRET_ROTATION", "Secrets Manager rotation with cached credentials", "infra",
         _param("INFRA_CHANGE", ["rotation_on"], (10, 90), M,
                "Terraform change enabled automatic rotation of the billing-db credentials; rotation ran at once and "
                "billing-svc kept using its cached password (db.credentials_cache_s), so logins failed.",
                "{file}:{line} sets {key} {old} -> {new} on {resource}.",
                "Restart billing-svc tasks to pick up the new secret; lower db.credentials_cache_s and refresh on "
                "authentication failure before re-enabling rotation.", "billing-db",
                reason="security baseline: rotate DB credentials",
                contributing=("billing-svc", "CONFIG_CHANGE", "cred_cache_up", (2, 8), D),
                blast_extra=("billing-svc",)), ("billing-db",), alerts=("billing-svc",)),
    _scn("REDIS_DOWNSIZE", "ElastiCache node downsized, evictions", "infra",
         _param("INFRA_CHANGE", ["redis_downsize"], (1, 48), H,
                "Terraform change moved {svc} to {new}; memory ran out, Redis evicted sessions and users were logged "
                "out in waves.", "{file}:{line} sets {key} {old} -> {new} on {resource}.",
                "Restore {key} = {old} on {resource}; alarm on Evictions and DatabaseMemoryUsagePercentage.",
                reason="cost reduction", decoy_window=8 * H), ("session-cache",)),
    _scn("REDIS_NOEVICTION", "Redis eviction policy set to noeviction", "infra",
         _param("INFRA_CHANGE", ["redis_noeviction"], (1, 30), H,
                "Terraform change set maxmemory-policy {old} -> {new}; when memory filled, Redis rejected writes "
                "(OOM command not allowed) and new sessions could not be created.",
                "{file}:{line} sets maxmemory-policy {old} -> {new} on {resource}.",
                "Restore maxmemory-policy = {old} on {resource}.", reason="never lose sessions"), ("session-cache",)),
    _scn("REDIS_SG_PORT", "Security group typo blocked the Redis port", "infra",
         _param("INFRA_CHANGE", ["redis_port_typo"], (5, 60), M,
                "Terraform change to the {svc} security group opened {new} instead of {old}; auth-svc could not reach "
                "Redis and every token check failed.", "{file}:{line} sets {key} {old} -> {new} on {resource}.",
                "Restore the ingress rule for port {old} on {resource}; add a connectivity check to the pipeline.",
                reason="tighten security group rules"), ("session-cache",)),
    _scn("SQS_VISIBILITY", "SQS visibility timeout shorter than processing time", "infra",
         _param("INFRA_CHANGE", ["sqs_visibility_cut"], (20, 240), M,
                "Terraform change cut {key} {old} -> {new} s, shorter than billing-svc's processing time; messages "
                "became visible again mid-processing and were billed twice (duplicate-key errors).",
                "{file}:{line} sets visibility_timeout_seconds {old} -> {new} on {resource}.",
                "Restore visibility_timeout_seconds = {old} on {resource}; make the consumer idempotent.",
                reason="faster retries of failed messages"), ("usage-queue",), alerts=("billing-svc",)),
    _scn("SQS_REDRIVE", "Redrive maxReceiveCount 1 sends retries to the DLQ", "infra",
         _param("INFRA_CHANGE", ["sqs_redrive_cut"], (20, 300), M,
                "Terraform change set maxReceiveCount {old} -> {new}; every transient failure sent usage events "
                "straight to the DLQ and invoices were missing usage.",
                "{file}:{line} sets maxReceiveCount {old} -> {new} on {resource}.",
                "Restore maxReceiveCount = {old}; redrive the DLQ.", reason="stop poison messages sooner"),
         ("usage-queue",), alerts=("billing-svc",)),
    _scn("IAM_POLICY", "IAM policy lost s3:GetObject", "infra",
         _param("INFRA_CHANGE", ["iam_drop_get"], (10, 120), M,
                "Terraform change narrowed the image-svc task role to {new}; reads of base images from S3 failed "
                "with AccessDenied and cloud-api could not find base files.",
                "{file}:{line} sets the policy actions {old} -> {new}.",
                "Restore actions = [{old}] on {resource}; test IAM changes with the IAM policy simulator.",
                "image-svc", reason="least privilege review"), ("image-svc",)),
    _scn("S3_LIFECYCLE", "S3 lifecycle rule expired old base images", "infra",
         _param("INFRA_CHANGE", ["s3_expire_short"], (2, 5), D,
                "Terraform change shortened the images/ expiration {old} -> {new} days; S3 deleted older base images "
                "and servers booting from them failed.",
                "{file}:{line} sets expiration days {old} -> {new} on {resource}.",
                "Restore expiration = {old} days; restore the images from the replica bucket / versioning.",
                "object-store", reason="storage cost reduction", decoy_window=10 * H), ("object-store",)),
    _scn("APIGW_THROTTLE", "API Gateway burst limit cut", "infra",
         _param("INFRA_CHANGE", ["apigw_burst_cut"], (15, 240), M,
                "Terraform change cut the API Gateway usage-plan {key} {old} -> {new}; traffic at the morning peak got "
                "429 Too Many Requests.", "{file}:{line} sets burst_limit {old} -> {new} on {resource}.",
                "Restore burst_limit = {old} on {resource}.", "api-gateway", reason="protect backends from bursts"),
         ("api-gateway",)),
    _scn("ALB_IDLE_TIMEOUT", "ALB idle timeout shorter than long requests", "infra",
         _param("INFRA_CHANGE", ["alb_idle_cut"], (15, 240), M,
                "Terraform change set the ALB idle_timeout {old} -> {new} s; long-running requests (report exports) "
                "were cut with 504 and clients retried.", "{file}:{line} sets idle_timeout {old} -> {new} on {resource}.",
                "Restore idle_timeout = {old} on {resource}.", "api-gateway", reason="free idle connections"),
         ("api-gateway",)),
    _scn("LB_HEALTHCHECK", "Target-group health check too aggressive", "infra",
         _param("INFRA_CHANGE", ["lb_hc_cut"], (15, 240), M,
                "Terraform change cut the {svc} target-group health-check timeout {old} -> {new} s; targets under load "
                "flapped unhealthy and the ALB returned 502.",
                "{file}:{line} sets health_check timeout {old} -> {new} on {resource}.",
                "Restore the health-check timeout = {old} on {resource}.", reason="detect dead targets faster"),
         ("auth-svc", "cloud-api", "api-gateway")),
    _scn("ECS_MEMORY", "ECS task memory halved", "infra",
         _param("INFRA_CHANGE", ["ecs_memory_cut"], (30, 360), M,
                "Terraform change halved {svc} task memory {old} -> {new} MB; tasks were OOM-killed under load and the "
                "service restarted in a loop.", "{file}:{line} sets memory {old} -> {new} on {resource}.",
                "Restore memory = {old} on {resource}; alarm on MemoryUtilization.", reason="right-sizing (cost)"),
         ("auth-svc", "image-svc", "billing-svc")),
    _scn("ASG_CAPACITY", "Auto Scaling group max size cut", "infra",
         _param("INFRA_CHANGE", ["asg_max_cut"], (6, 48), H,
                "Terraform change cut the {svc} Auto Scaling group max_size {old} -> {new}; at the next batch peak the "
                "fleet could not scale out and jobs queued.", "{file}:{line} sets max_size {old} -> {new} on {resource}.",
                "Restore max_size = {old} on {resource}.", reason="budget cap", decoy_window=8 * H),
         ("node-svc", "compute-svc")),
    _scn("LAMBDA_CONCURRENCY", "Lambda reserved concurrency cut", "infra",
         _param("INFRA_CHANGE", ["lambda_conc_cut"], (30, 300), M,
                "Terraform change cut reserved concurrency of {svc} {old} -> {new}; invocations were throttled "
                "(Rate Exceeded) and billing-svc's calls failed.",
                "{file}:{line} sets reserved_concurrent_executions {old} -> {new} on {resource}.",
                "Restore reserved concurrency = {old} on {resource}.", "notification-svc",
                reason="protect SES sending quota"), ("notification-svc",), alerts=("notification-svc", "billing-svc")),
    # ---- external causes: no change explains it (planned)
    _scn("TRAFFIC_SPIKE", "Traffic spike from one customer", "external", no_change, ("api-gateway",)),
    _scn("CERT_EXPIRY", "Customer webhook certificate expired", "external", no_change, ("notification-svc",),
         alerts=("notification-svc",)),
    _scn("AWS_S3_DEGRADED", "Amazon S3 regional degradation", "external", no_change, ("object-store",),
         alerts=("image-svc", "cloud-api")),
    _scn("SES_THROTTLED", "Amazon SES sending quota throttled", "external", no_change, ("notification-svc",),
         alerts=("notification-svc", "billing-svc")),
)}
COMPLEX = [s for s in SCENARIOS.values() if s.family == "complex"]
SIMPLE = [s for s in SCENARIOS.values() if s.family == "simple"]
PLANNED = [s for s in SCENARIOS.values() if s.planned and s.causes]
MAX_PER_COMPLEX = 2
GENERIC_WORDS = ("exception", "failed", "error")


def match(inc: dict[str, Any], used: dict[Any, int]) -> str:
    """Scenario name for an incident on a REAL anomaly: a complex scenario whose service and keywords fit
    (each used at most ``MAX_PER_COMPLEX`` times and once per service, so two incidents never stack the
    same change on one service), else a simple keyword scenario, else BAD_DEPLOY."""
    text = " ".join(inc.get("error_templates", [])).lower()
    svc = inc["service"]

    def free(s: Scenario, limit: int = MAX_PER_COMPLEX) -> bool:
        return used.get(s.name, 0) < limit and used.get((s.name, svc), 0) < 1

    for s in COMPLEX:
        if svc in s.services and any(k in text for k in s.keywords) and free(s):
            return s.name
    for s in COMPLEX:  # service fits even without a keyword: still realistic, keeps variety
        if svc in s.services and s.name != "TIMEOUT_UNIT" and free(s, 1):
            return s.name
    for s in SIMPLE:
        if svc in s.services and any(k in text for k in s.keywords):
            return s.name
    # generic failures: a code regression; prefer the richer unit-bug scenario while it is free
    if any(k in text for k in GENERIC_WORDS) and free(SCENARIOS["TIMEOUT_UNIT"]):
        return "TIMEOUT_UNIT"
    return "BAD_DEPLOY"


def alert_candidates(s: Scenario, cause: str) -> list[str]:
    """Where a fault in ``cause`` pages on-call: the scenario's fixed alerts, else the paging
    components in its blast radius (the cause itself included when it pages)."""
    if s.alerts:
        return list(s.alerts)
    return [c for c in blast_radius(cause) if c in PAGING]
