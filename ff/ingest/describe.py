"""Change requests: a structured record and a natural-language description for every change.

Real teams do not ship bare diffs: every deploy has a pull-request / change-request text
(summary, motivation, what changed per file, testing, rollout, rollback plan), every hotfix
says what bug it fixes, and every Terraform apply has a plan summary and a reason. This module
writes both forms for each simulated change after the simulator's replay (versions and
parameter values are known then):

* ``payload["change_request"]``: structured (id, author, approver, risk, rollout, rollback plan,
  tickets, affected resources);
* ``payload["description"]``: unstructured, a few paragraphs in plain English.

Nothing here knows which change caused an incident: the text of a harmful change reads like the
text of a harmless one (teams rarely write "this will break prod"). Descriptions never contain
event ids (those are renumbered later).
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any

from ff import config

PEOPLE: dict[str, tuple[str, ...]] = {
    "team-edge": ("a.okafor", "m.lindqvist", "r.iyer"),
    "team-identity": ("s.moreau", "k.tanaka", "d.silva"),
    "team-api": ("p.deshmukh", "l.becker", "j.nakamura"),
    "team-batch": ("h.kowalski", "e.rossi", "t.mensah"),
    "team-storage": ("y.chen", "o.adeyemi", "f.novak"),
    "team-platform": ("b.haddad", "c.oconnor", "v.petrov"),
    "team-hpc": ("n.gupta", "i.larsen", "g.duarte"),
    "team-billing": ("z.alvarez", "w.fischer", "q.huang"),
}
ROLLOUT = {
    "ecs": "ECS rolling update (minimum healthy 100 %, maximum 200 %), health checks gate each batch",
    "lambda": "Lambda alias shifted 10 % -> 100 % over 10 minutes (CodeDeploy canary)",
    "ec2": "rolling replacement of the Auto Scaling group, 25 % of instances at a time",
    "gateway": "API Gateway stage deployment with a canary at 10 % for 15 minutes",
}
KEY_EFFECT: dict[str, str] = {
    "http.timeout_ms": "how long outbound HTTP calls wait before failing",
    "db.pool.max_size": "the maximum number of open database connections per instance",
    "db.statement_timeout_ms": "how long a single SQL statement may run",
    "retry.max": "how many times a failed call is retried",
    "retry.backoff_ms": "the pause between retries",
    "jvm.heap_mb": "the JVM heap size",
    "gc.algorithm": "the garbage collector",
    "log.level": "log verbosity",
    "metrics.interval_s": "how often metrics are flushed",
    "session.timeout_ms": "how quickly idle ZooKeeper sessions expire",
    "dfs.replication": "how many copies of each block are written",
    "cache.session_ttl_s": "how long sessions stay in the Redis cache",
    "cache.image_mb": "the in-memory image manifest cache size",
    "consumer.concurrency": "how many SQS messages are processed in parallel per task",
    "db.credentials_cache_s": "how long database credentials from Secrets Manager are cached",
    "webhook.max_retries": "how often a failed customer webhook is retried",
    "breaker.failure_pct": "the failure rate that opens the circuit breaker",
    "disk_gb": "the EBS data volume size", "ebs_throughput": "EBS gp3 throughput (MB/s)",
    "ebs_iops": "EBS gp3 IOPS", "instance_type": "the EC2 instance type",
    "node_pool_size": "the Auto Scaling group's desired capacity", "asg_max_size": "the Auto Scaling group's maximum size",
    "ecs_desired_count": "the number of ECS tasks", "ecs_task_memory_mb": "memory per ECS task",
    "lb_healthcheck_timeout_s": "the target-group health-check timeout", "alb_idle_timeout_s": "the ALB idle timeout",
    "apigw_burst_limit": "API Gateway burst throttling", "apigw_rate_limit": "API Gateway steady-state rate limit",
    "sg_election_port": "the quorum election port allowed by the security group",
    "iam_s3_actions": "which S3 actions the task role may perform",
    "lambda_concurrency": "reserved Lambda concurrency", "lambda_timeout_s": "the Lambda timeout",
    "rds_instance_class": "the RDS instance class", "rds_max_connections": "PostgreSQL max_connections",
    "rds_engine_version": "the PostgreSQL engine version", "rds_apply_immediately": "whether modifications apply "
    "immediately instead of in the maintenance window", "rds_storage_gb": "allocated RDS storage",
    "secret_rotation_days": "automatic rotation of the database credentials in Secrets Manager",
    "redis_node_type": "the ElastiCache node type", "redis_maxmemory_policy": "Redis eviction policy",
    "redis_sg_port": "the Redis port allowed by the security group",
    "sqs_visibility_timeout_s": "how long a received message stays invisible to other consumers",
    "sqs_max_receive_count": "how many receives before a message goes to the DLQ",
    "sqs_retention_s": "how long messages are retained", "s3_expiration_days": "when objects under images/ expire",
    "s3_request_metrics": "S3 request metrics",
}
BUG_FIXES: tuple[tuple[str, str], ...] = (  # (bug, fix) pairs for routine hotfixes
    ("intermittent 500 when the request carries no tenant header", "default the tenant from the token in {sym}"),
    ("NullPointerException when an optional field is missing", "guard null in {sym}"),
    ("stale cache entry served after an update", "invalidate the cache entry after writes in {sym}"),
    ("log line leaks the request body at DEBUG", "redact the request body in {sym} logging"),
    ("wrong HTTP status (500 instead of 404) for missing resources", "map not-found errors to 404 in {sym}"),
    ("retry loop does not honour the request deadline", "stop retrying after the deadline in {sym}"),
    ("metrics counter double-counts retried calls", "count each call once in {sym}"),
    ("pagination cursor skips the last item", "fix the cursor bound in {sym}"),
)
BUGS: tuple[str, ...] = tuple(b for b, _ in BUG_FIXES)


def _runtime(svc: str) -> str:
    aws = config.COMPONENTS[svc].aws.lower()
    if "lambda" in aws and svc != "api-gateway":
        return "lambda"
    if svc == "api-gateway":
        return "gateway"
    return "ecs" if "fargate" in aws else "ec2"


def _risk(ev: dict[str, Any], cds: list[dict[str, Any]]) -> str:
    if ev["event_type"] == "INFRA_CHANGE" or any((c.get("file") or "").endswith(".sql") for c in cds):
        return "medium"
    if ev["event_type"] in ("PATCH", "ROLLBACK") or len(cds) > 3:
        return "medium"
    return "low"


def _hunk_line(c: dict[str, Any]) -> str:
    """'`file` (symbol): + first added line' for a code hunk."""
    added = [ln[1:].strip() for ln in (c.get("diff") or "").splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    removed = [ln[1:].strip() for ln in (c.get("diff") or "").splitlines() if ln.startswith("-") and not ln.startswith("---")]
    if added and removed:
        what = f"`{removed[0][:70]}` -> `{added[0][:70]}`"
    elif added:
        what = f"adds `{added[0][:80]}`"
    elif removed:
        what = f"removes `{removed[0][:80]}`"
    else:
        what = "edits"
    return f"- `{c['file']}` ({c.get('symbol')}): {what}"


def describe(ev: dict[str, Any], cds: list[dict[str, Any]], rng: random.Random, tests: dict[str, Any] | None = None
             ) -> tuple[dict[str, Any], str]:
    """(change_request, description) for one change event."""
    svc, et, p = ev["service"], ev["event_type"], ev["payload"]
    team = config.COMPONENTS[svc].team
    people = PEOPLE.get(team, ("ops.bot",))
    author = rng.choice(people)
    approver = rng.choice([x for x in people if x != author] or ["platform.lead"])
    cr = {"id": f"CHG-{rng.randint(10000, 99999)}", "author": author, "approver": approver, "team": team,
          "risk": _risk(ev, cds), "tickets": list(p.get("tickets", []))}
    when = datetime.fromisoformat(ev["occurred_at"]) if isinstance(ev["occurred_at"], str) else ev["occurred_at"]
    params = [c for c in cds if c.get("param_key")]
    code = [c for c in cds if not c.get("param_key")]
    if et in ("DEPLOY", "PATCH"):
        rt = _runtime(svc)
        cr["rollout"] = ROLLOUT[rt] if et == "DEPLOY" else "direct to prod after the prod e2e suite (hotfix path)"
        mig = [c for c in code if (c.get("file") or "").endswith(".sql")]
        cr["rollback_plan"] = (f"redeploy {ev.get('version_from')} through the pipeline"
                               + ("; the migration is not reversible, restore needs a forward fix" if mig else ""))
        feats = p.get("features") or []
        paras = []
        if et == "DEPLOY" and feats:
            paras.append(f"Release {ev.get('version_to')} of {svc} ({ev['environment']}, train {p.get('cycle', '')}). "
                         + " ".join(f"Ships {f['ticket']} {f['title']}: {f.get('summary') or 'see ticket'}"
                                    + (f" (behind flag {f['flag']})." if f.get("flag") else "") for f in feats))
        elif et == "DEPLOY":
            paras.append(f"Release {ev.get('version_to')} of {svc} ({ev['environment']}): "
                         f"{'; '.join(p.get('commit_messages', [])) or 'maintenance'}.")
        else:
            bug = p.get("bug") or rng.choice(BUGS)
            p.setdefault("bug", bug)
            fixes = f" Follow-up to {p['fixes']}." if p.get("fixes") else ""
            paras.append(f"Hotfix {', '.join(p.get('tickets', []))} for {svc} {ev.get('version_from')} -> "
                         f"{ev.get('version_to')}. Problem: {bug}. Fix: {'; '.join(p.get('commit_messages', []))}.{fixes}")
        if code:
            paras.append("Changes:\n" + "\n".join(_hunk_line(c) for c in code[:5]))
        if mig:
            paras.append(f"Database: runs migration {mig[0]['file'].rsplit('/', 1)[-1]} on "
                         f"{'billing-db' if svc == 'billing-svc' else 'meta-db'} during the deploy.")
        t = tests or p.get("tests")
        if t:
            skipped = f"; skipped suites: {', '.join(t['skipped_suites'])}" if t.get("skipped_suites") else ""
            paras.append(f"Testing: {t['passed']}/{t['total']} tests passed in {ev['environment']}{skipped}.")
        paras.append(f"Rollout: {cr['rollout']}. Rollback: {cr['rollback_plan']}. Change request {cr['id']} by "
                     f"{author}, approved by {approver}, risk {cr['risk']}.")
        return cr, "\n\n".join(paras)
    if et in ("CONFIG_CHANGE", "INFRA_CHANGE"):
        role = ev.get("_sim", {}).get("role")
        reason = p.get("reason") or "not stated"
        lines = []
        for c in params:
            eff = KEY_EFFECT.get(c["param_key"], "the setting")
            if c["param_key"].startswith("feature."):
                eff = f"the {c['param_key']} feature flag"
            lines.append(f"`{c['param_key']}`: {c['param_old']} -> {c['param_new']} ({eff})")
        if et == "CONFIG_CHANGE":
            cr["rollout"] = "config pushed through AWS AppConfig, picked up by running tasks within 60 s"
            cr["rollback_plan"] = "revert the commit in the config repository and push again"
            head = f"Config change to config/{svc}.yaml ({ev['environment']})."
        else:
            res = sorted({c.get("infra_resource") or "" for c in params} - {""})
            cr["resources"] = res
            cr["rollout"] = "terraform apply from the pipeline (plan reviewed in the pull request)"
            cr["rollback_plan"] = "revert the Terraform commit and apply again"
            head = (f"Terraform apply on {', '.join(res)}. Plan: 0 to add, {len(res)} to change, 0 to destroy.")
            if any(c["param_key"] in ("rds_engine_version", "rds_instance_class", "redis_node_type") for c in params):
                head += " The resource restarts or fails over while the change is applied."
        if role == "remediation":
            head = f"Mitigation. {head}"
        body = f"{head} Changes: {'; '.join(lines) or 'see diff'}. Reason: {reason}."
        return cr, (f"{body}\n\nRollout: {cr['rollout']}. Rollback: {cr['rollback_plan']}. Change request {cr['id']} "
                    f"by {author}, approved by {approver}, applied {when:%Y-%m-%d %H:%M} UTC.")
    if et == "ROLLBACK":
        cr["rollout"] = "previous image redeployed" + (" on part of the fleet" if p.get("partial") else "")
        cr["rollback_plan"] = "redeploy the rolled-back version once fixed"
        return cr, (f"Rolled back {svc} from {ev.get('version_from')} to {ev.get('version_to')}"
                    f"{' on part of the fleet' if p.get('partial') else ''}. Reason: {p.get('reason') or 'not stated'}. "
                    f"Change request {cr['id']} by {author}.")
    return cr, ""


def describe_world(world: Any, seed: int = config.SEED) -> None:
    """Attach ``change_request`` and ``description`` to every simulated change (after replay)."""
    rng = random.Random(seed + 101)
    cds: dict[str, list[dict[str, Any]]] = {}
    for c in world.change_details:
        cds.setdefault(c["event_id"], []).append(c)
    for e in sorted(world.sim_events(), key=lambda e: (str(e["occurred_at"]), e["id"])):
        if e["event_type"] in config.CHANGE_TYPES:
            cr, text = describe(e, cds.get(e["id"], []), rng)
            e["payload"]["change_request"] = cr
            e["payload"]["description"] = text
