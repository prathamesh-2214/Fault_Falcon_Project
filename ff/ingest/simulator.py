"""Seeded platform simulator: six months of change history + the incidents behind the anomalies.

Nothing here touches log lines. The simulator reads the incident plan (``ff/ingest/plan.py``),
whose incidents are each linked to an ANOMALY detected in the logs (real LogHub logs or the
synthetic telemetry), and for each incident runs a *scenario* (``ff/ingest/scenarios.py``) that
places the changes behind it: a macro cause (deploy / hotfix / config / AWS infra change), the
micro culprit (the exact diff hunk that broke it), contributing changes, 1-3 harmless decoys
closer in time than the cause, and the team's mitigation afterwards (rollback or revert). The
ground truth is known by construction.

Every change is made of diff hunks (``ff/ingest/repo.py``) over a realistic repository: service
code, build manifests, DB migrations, ``config/<svc>.yaml`` and ``infra/<component>.tf`` (AWS:
EBS, launch templates, Auto Scaling, ECS, ALB / API Gateway, security groups, IAM, RDS parameter
groups, ElastiCache, SQS, S3 lifecycle, Lambda, Secrets Manager).

Stages
------
1. Background history per component: routine config changes of services, Terraform changes of
   every component (managed resources included), weekly security scans of services.
2. Weekly release trains (``ff/ingest/cycles.py``): features deploy ppd -> test -> prod, callee
   first; hotfix patches and occasional rollbacks.
3. Incident anchoring: each planned incident runs its scenario and gets decoys and a mitigation.
4. :func:`replay` walks the world chronologically: versions, parameter old/new values (from each
   change's *intent*), parameter diff text, linked tests/scans, and the post-deploy error rate
   computed from the log lines around each deploy ("no log coverage" when there are none).
5. :func:`ff.ingest.describe.describe_world` writes a change request and a natural-language
   description for every change; :func:`finalize` renumbers everything chronologically
   (``evt_N`` / ``chg_N`` / ``INC-NNN``) so ids carry no hint of the cause, and fills each
   incident's RCA ground truth.

:func:`make_variant` re-draws one incident's scenario (lags, decoys, values) and returns a
complete new :class:`World` (used for extra held-out sessions).
"""

from __future__ import annotations

import argparse
import copy
import json
import random
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from ff import config
from ff.config import Paths, get_paths
from ff.ingest import repo, scenarios
from ff.ingest.repo import Hunk

H = timedelta(hours=1)
M = timedelta(minutes=1)
D = timedelta(days=1)

MAJOR = {"cloud-api": 3, "compute-svc": 2, "storage-svc": 4, "coord-svc": 1, "node-svc": 5, "api-gateway": 2,
         "auth-svc": 3, "image-svc": 1, "billing-svc": 4, "notification-svc": 2}
INSTANCE_LADDER = ("t3.medium", "t3.large", "m5.large", "m5.xlarge", "m5.2xlarge", "m5.4xlarge")
RDS_LADDER = ("db.t3.small", "db.t3.medium", "db.t3.large", "db.r5.large", "db.r5.xlarge", "db.r5.2xlarge")
REDIS_LADDER = ("cache.t4g.small", "cache.t4g.medium", "cache.r6g.large", "cache.r6g.xlarge")
FEATURE_FLAG = repo.FEATURE_FLAG


def _ladder(lad: tuple[str, ...], step: int):
    return lambda o: lad[max(0, min(len(lad) - 1, lad.index(o) + step))]


def _minor_bump(v: str) -> str:
    major, minor = str(v).split(".")[:2]
    return f"{major}.{int(minor) + 2}"


# intent -> (parameter key, function old -> new). 'feature' means the service's feature flag.
INTENTS: dict[str, tuple[str, Any]] = {
    # benign
    "timeout_up": ("http.timeout_ms", lambda o: o + 500), "pool_up": ("db.pool.max_size", lambda o: o + 10),
    "heap_up": ("jvm.heap_mb", lambda o: o + 1024), "gc_switch": ("gc.algorithm", lambda o: "ZGC" if o == "G1" else "G1"),
    "flag_toggle": ("feature", lambda o: "on" if o == "off" else "off"),
    "log_level": ("log.level", lambda o: "DEBUG" if o == "INFO" else "INFO"),
    "metrics_interval": ("metrics.interval_s", lambda o: 30 if o == 60 else 60),
    "backoff_up": ("retry.backoff_ms", lambda o: o + 100),
    "stmt_timeout_up": ("db.statement_timeout_ms", lambda o: o + 15000),
    "image_cache_up": ("cache.image_mb", lambda o: o + 1024),
    "concurrency_tune": ("consumer.concurrency", lambda o: o + 2 if o < 12 else o - 2),
    "cred_cache_down": ("db.credentials_cache_s", lambda o: max(60, o // 3)),
    "disk_up": ("disk_gb", lambda o: o + 250), "instance_up": ("instance_type", _ladder(INSTANCE_LADDER, 1)),
    "pool_size_up": ("node_pool_size", lambda o: o + 1),
    "ecs_count_up": ("ecs_desired_count", lambda o: o + 1),
    "apigw_rate_up": ("apigw_rate_limit", lambda o: o + 500),
    "lambda_timeout_up": ("lambda_timeout_s", lambda o: o + 30),
    "rds_storage_up": ("rds_storage_gb", lambda o: o + 100),
    "rds_minor_upgrade": ("rds_engine_version", _minor_bump),
    "sqs_retention_up": ("sqs_retention_s", lambda o: min(1209600, o * 2)),
    "s3_metrics_toggle": ("s3_request_metrics", lambda o: "true" if o == "false" else "false"),
    "redis_node_up": ("redis_node_type", _ladder(REDIS_LADDER, 1)),
    "replication_up": ("dfs.replication", lambda o: o + 1),
    # harmful (incident causes / contributing factors)
    "timeout_cut": ("http.timeout_ms", lambda o: max(200, o // 4)),
    "pool_cut": ("db.pool.max_size", lambda o: max(2, o // 10)),
    "retry_storm": ("retry.max", lambda o: o * 5), "heap_cut": ("jvm.heap_mb", lambda o: max(512, o // 4)),
    "disk_cut": ("disk_gb", lambda o: max(50, o // 5)), "instance_down": ("instance_type", _ladder(INSTANCE_LADDER, -2)),
    "session_cut": ("session.timeout_ms", lambda o: max(500, o // 2)),
    "sg_port_change": ("sg_election_port", lambda o: 2888 if o == 3888 else 3888),
    "lb_hc_cut": ("lb_healthcheck_timeout_s", lambda o: max(2, o - 3)),
    "ebs_cut": ("ebs_throughput", lambda o: max(62, o // 2)), "ebs_iops_cut": ("ebs_iops", lambda o: max(1500, o // 2)),
    "rds_downsize": ("rds_instance_class", _ladder(RDS_LADDER, -2)),
    "flag_on": ("feature", lambda o: "on"),
    "ttl_typo": ("cache.session_ttl_s", lambda o: max(1, o // 100)),
    "concurrency_up": ("consumer.concurrency", lambda o: o * 8),
    "cred_cache_up": ("db.credentials_cache_s", lambda o: 86400),
    "webhook_retries_up": ("webhook.max_retries", lambda o: o * 8),
    "breaker_sensitive": ("breaker.failure_pct", lambda o: 5 if o != 5 else 3),
    "rds_max_conn_cut": ("rds_max_connections", lambda o: max(40, o // 5)),
    "rds_engine_upgrade": ("rds_engine_version", _minor_bump),
    "rds_apply_now": ("rds_apply_immediately", lambda o: "true"),
    "rotation_on": ("secret_rotation_days", lambda o: 30),
    "redis_downsize": ("redis_node_type", _ladder(REDIS_LADDER, -2)),
    "redis_noeviction": ("redis_maxmemory_policy", lambda o: "noeviction"),
    "redis_port_typo": ("redis_sg_port", lambda o: 6380 if o == 6379 else 6379),
    "sqs_visibility_cut": ("sqs_visibility_timeout_s", lambda o: max(15, o // 10)),
    "sqs_redrive_cut": ("sqs_max_receive_count", lambda o: 1),
    "iam_drop_get": ("iam_s3_actions", lambda o: "s3:ListBucket"),
    "s3_expire_short": ("s3_expiration_days", lambda o: max(7, o // 12)),
    "apigw_burst_cut": ("apigw_burst_limit", lambda o: max(100, o // 10)),
    "alb_idle_cut": ("alb_idle_timeout_s", lambda o: max(5, o // 6)),
    "ecs_memory_cut": ("ecs_task_memory_mb", lambda o: max(512, o // 2)),
    "asg_max_cut": ("asg_max_size", lambda o: max(2, o // 2)),
    "lambda_conc_cut": ("lambda_concurrency", lambda o: max(2, o // 10)),
}
BENIGN_CONFIG = ("timeout_up", "pool_up", "heap_up", "gc_switch", "flag_toggle", "log_level", "metrics_interval",
                 "backoff_up", "stmt_timeout_up", "image_cache_up", "concurrency_tune")
BENIGN_INFRA = ("disk_up", "instance_up", "pool_size_up", "ecs_count_up", "apigw_rate_up", "lambda_timeout_up",
                "rds_storage_up", "rds_minor_upgrade", "sqs_retention_up", "s3_metrics_toggle", "redis_node_up")
REASONS = ("reduce tail latency", "cost reduction", "align with platform defaults", "capacity review",
           "requested by on-call", "")
MESSAGES = ("Refactor {sym}", "Add metrics around {sym}", "Bump dependency versions", "Improve logging in {sym}",
            "Fix flaky test for {sym}", "Tune batching in {sym}", "Clean up unused imports",
            "Handle edge case in {sym}")
LONG_MESSAGE = ("Rework {sym} so that the request path reuses pooled clients, moves retry bookkeeping into a helper, "
                "adds structured log fields for the request id and target host, removes the legacy fallback branch "
                "that was only used by the old scheduler, and updates the unit tests accordingly")
SUITES = {"unit": (200, 400), "integration": (30, 60), "load": (5, 10), "e2e": (10, 20)}
STAGE_SUITES = {"ppd": ("unit", "integration", "load", "e2e"), "test": ("integration", "e2e"), "prod": ("e2e",)}


def _iso(t: datetime | str) -> str:
    return t if isinstance(t, str) else t.isoformat()


def _dt(t: datetime | str) -> datetime:
    return datetime.fromisoformat(t) if isinstance(t, str) else t


def intent_key(intent: str, svc: str) -> str:
    key = INTENTS[intent][0]
    return FEATURE_FLAG.get(svc, "feature.none") if key == "feature" else key


def intents_for(svc: str, pool: tuple[str, ...]) -> list[str]:
    """The intents of ``pool`` whose parameter ``svc`` has."""
    out = []
    for i in pool:
        key = INTENTS[i][0]
        if key == "feature":
            if svc in FEATURE_FLAG:
                out.append(i)
        elif repo.PARAMS[key].applies(svc):
            out.append(i)
    return out


def default_params(svc: str) -> dict[str, Any]:
    out = {k: repo.param_spec(k).default for k in repo.params_of(svc)}
    for k in list(out):
        if k.startswith("feature."):
            out[k] = "off"
    return out


# --------------------------------------------------------------------------- world
@dataclass
class World:
    """All events (log-derived + simulated), change details, error signatures and incidents."""

    events: list[dict[str, Any]] = field(default_factory=list)
    change_details: list[dict[str, Any]] = field(default_factory=list)
    signatures: list[dict[str, Any]] = field(default_factory=list)
    incidents: list[dict[str, Any]] = field(default_factory=list)

    def by_id(self) -> dict[str, dict[str, Any]]:
        return {e["id"]: e for e in self.events}

    def sim_events(self) -> list[dict[str, Any]]:
        return [e for e in self.events if e["source"] == "simulated"]

    def log_events(self) -> list[dict[str, Any]]:
        return [e for e in self.events if e["source"] == "logs"]

    def details_of(self, event_id: str) -> list[dict[str, Any]]:
        return [c for c in self.change_details if c["event_id"] == event_id]


class _Ids:
    def __init__(self, prefix: str = "S") -> None:
        self.prefix, self.n_ev, self.n_cd, self.n_rel = prefix, 0, 0, 0

    def ev(self) -> str:
        self.n_ev += 1
        return f"{self.prefix}{self.n_ev:05d}"

    def cd(self) -> str:
        self.n_cd += 1
        return f"{self.prefix}C{self.n_cd:05d}"

    def rel(self, svc: str) -> str:
        self.n_rel += 1
        return f"{svc}#{self.prefix}r{self.n_rel:04d}"


class Simulator:
    """Generates events into a :class:`World` with a seeded RNG."""

    def __init__(self, seed: int = config.SEED, world: World | None = None, prefix: str = "S") -> None:
        self.rng = random.Random(seed)
        self.world = world or World()
        self.ids = _Ids(prefix)
        self.last_cds: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ primitives
    def event(self, etype: str, svc: str, env: str, t: datetime, payload: dict | None = None,
              sim: dict | None = None, related: str | None = None) -> dict[str, Any]:
        ev = {"id": self.ids.ev(), "event_type": etype, "service": svc, "environment": env, "occurred_at": t,
              "end_at": None, "version_from": None, "version_to": None, "source": "simulated",
              "payload": payload or {}, "_sim": sim or {}, "related_event_id": related}
        self.world.events.append(ev)
        return ev

    def add_hunk(self, ev: dict[str, Any], h: Hunk, line: int | None = None, **extra: Any) -> dict[str, Any]:
        """A change_detail row for one hunk (diff rendered now, or in replay for parameters)."""
        line = line if line is not None else self.rng.randint(20, 380)
        start = repo.changed_line(h, line)
        n = max(len(h.before), len(h.after), 1)
        cd = {"id": self.ids.cd(), "event_id": ev["id"], "file": h.file, "line_start": start, "line_end": start + n - 1,
              "lines_changed": len(h.before) + len(h.after), "symbol": h.symbol, "param_key": None, "param_old": None,
              "param_new": None, "param_reason": None, "infra_resource": None,
              "diff": None if extra.get("_intent") else repo.render_diff(h, line),
              "_intent": None, "_hunk": asdict(h), "_line": line}
        cd.update(extra)
        self.world.change_details.append(cd)
        return cd

    def test_run(self, svc: str, env: str, t: datetime, release: str, skip_load: bool = False,
                 fail: str | None = None) -> dict[str, Any]:
        suites = {}
        for name in STAGE_SUITES[env]:
            lo, hi = SUITES[name]
            n = self.rng.randint(lo, hi)
            if name == "load" and skip_load:
                suites[name] = {"passed": 0, "failed": 0, "skipped": n, "skipped_suite": True}
                continue
            failed = 1 if (fail == name or self.rng.random() < 0.03) else 0
            suites[name] = {"passed": n - failed, "failed": failed, "skipped": 0, "skipped_suite": False}
        payload = {
            "suites": suites, "stage": env,
            "passed": sum(s["passed"] for s in suites.values()),
            "failed": sum(s["failed"] for s in suites.values()),
            "skipped": sum(s["skipped"] for s in suites.values()),
            "coverage_delta": round(self.rng.uniform(-1.5, 1.5), 1),
        }
        payload["total"] = payload["passed"] + payload["failed"] + payload["skipped"]
        payload["skipped_suites"] = [k for k, s in suites.items() if s["skipped_suite"]]
        payload["failed_suites"] = [k for k, s in suites.items() if s["failed"]]
        return self.event("TEST_RUN", svc, env, t, payload, {"release": release})

    def _messages(self, symbols: list[str], long: bool = False) -> list[str]:
        if long:
            return [LONG_MESSAGE.format(sym=symbols[0])]
        return [self.rng.choice(MESSAGES).format(sym=s.split(".")[0]) for s in symbols]

    def release(self, svc: str, t_prod: datetime, hunks: list[Hunk], *, messages: list[str] | None = None,
                schema: bool = False, skip_load: bool = False, role: str = "background",
                envs: tuple[str, ...] = ("ppd", "test", "prod"),
                features: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """A release deployed ppd -> test -> prod (2-8 h apart) with a TEST_RUN before each deploy.

        ``features`` are the product features (tickets) the release carries; incident releases
        without an explicit feature get one named after their first commit message.
        Returns {"ppd"/"test"/"prod": deploy event, "cds": the prod change_detail rows in hunk order}.
        """
        key = self.ids.rel(svc)
        t_test = t_prod - self.rng.uniform(2, 8) * H
        t_ppd = t_test - self.rng.uniform(2, 8) * H
        times = {"ppd": t_ppd, "test": t_test, "prod": t_prod}
        if schema:
            hunks = hunks + [repo.benign_hunk(self.rng, f"{repo.MIGRATION_DIR.get(svc, 'db/migrations')}/"
                                                        f"V{self.rng.randint(10, 99)}__add_index.sql", "migration")]
        msgs = messages or self._messages([h.symbol for h in hunks if h.kind == "code"] or ["docs"],
                                          long=self.rng.random() < 0.1)
        if features is None and role in ("cause", "contributing"):
            from ff.ingest import cycles

            if messages:  # an explicitly described change becomes its own feature ticket
                self._feat_seq = getattr(self, "_feat_seq", 200) + 1
                features = [{"ticket": f"FEAT-{self._feat_seq}", "title": messages[0], "services": [svc], "flag": None,
                             "summary": messages[0] + "."}]
            else:  # a generic regression ships inside one of the service's catalog features
                pool = [f for f in cycles.FEATURES if svc in f.services]
                features = [cycles.feature_ref(self.rng.choice(pool))] if pool else []
        features = features or []
        msgs = [f"{f['ticket']}: {f['title']}" for f in features] + list(msgs)
        drawn = [self.rng.randint(20, 380) for _ in hunks]  # (drawn for every hunk: keeps the seeded sequence stable)
        lines = [1 if h.file.endswith(".sql") else ln for h, ln in zip(hunks, drawn)]  # migrations are new files
        out: dict[str, Any] = {"cds": []}
        for env in envs:
            t = times[env]
            tr = self.test_run(svc, env, t - self.rng.uniform(10, 40) * M, key, skip_load=skip_load and env == "ppd")
            dep = self.event("DEPLOY", svc, env, t, {"commit_messages": msgs, "reason": "scheduled release",
                                                     "schema_change": schema or any(h.file.endswith(".sql") for h in hunks),
                                                     "test_run_id": tr["id"], "features": features,
                                                     "tickets": [f["ticket"] for f in features]},
                             {"release": key, "role": role}, related=tr["id"])
            cds = [self.add_hunk(dep, h, ln) for h, ln in zip(hunks, lines)]
            out[env] = dep
            if env == "prod":
                out["cds"] = cds
        return out

    def patch(self, svc: str, t: datetime, fixes: str | None = None, role: str = "background",
              message: str | None = None, hunks: list[Hunk] | None = None, bug: str | None = None) -> dict[str, Any]:
        """A hotfix applied directly to prod (ticket HOTFIX-n, optionally fixing a feature ticket)."""
        from ff.ingest.describe import BUG_FIXES

        f, s, _ = self.rng.choice(repo.CODE_FILES[svc])
        ticket = f"HOTFIX-{self.rng.randint(200, 999)}"
        tr = self.test_run(svc, "prod", t - 15 * M, f"{svc}#patch")
        pair = self.rng.choice(BUG_FIXES)
        if message is None:
            bug = bug or pair[0]
            message = "Hotfix: " + pair[1].format(sym=s) + (f" (follow-up to {fixes})" if fixes else "")
        msg = message
        payload = {"commit_messages": [f"{ticket}: {msg}"], "reason": "hotfix", "test_run_id": tr["id"],
                   "tickets": [ticket], "fixes": fixes}
        if bug:
            payload["bug"] = bug
        ev = self.event("PATCH", svc, "prod", t, payload, {"role": role}, related=tr["id"])
        self.last_cds = [self.add_hunk(ev, h) for h in (hunks or [repo.benign_hunk(self.rng, f, s)])]
        return ev

    def release_trains(self) -> None:
        """Weekly release trains: each cycle ships 2-4 features; the services they touch deploy to
        prod in dependency order (callees first), with occasional hotfix patches and rollbacks.
        Services without a feature get a maintenance release half of the time."""
        from ff.ingest import cycles

        rng = self.rng
        for n in range(cycles.N_CYCLES):
            c0 = cycles.cycle_start(n) + rng.uniform(0.5, 1.5) * D
            feats = cycles.plan_cycle(rng, n)
            per_svc: dict[str, list[Any]] = {}
            for f in feats:
                for s in f.services:
                    per_svc.setdefault(s, []).append(f)
            for s in config.CODE_SERVICES:
                if s not in per_svc and rng.random() < 0.5:
                    per_svc[s] = []
            for svc in sorted(per_svc, key=lambda s: (cycles.LEVEL[s], s)):
                t = c0 + cycles.LEVEL[svc] * 6 * H + rng.uniform(0, 5) * H  # one 6-hour slot per level
                if t >= config.SIM_END:
                    continue
                files = rng.sample(repo.CODE_FILES[svc], rng.randint(1, 3))
                hunks = [repo.benign_hunk(rng, f, s) for f, s, _ in files]
                if rng.random() < 0.3:
                    hunks.append(repo.benign_hunk(rng, f"{svc}/{rng.choice(repo.DOC_FILES)}", "docs"))
                if rng.random() < 0.15 and svc in repo.BUILD_FILES:
                    hunks.append(repo.benign_hunk(rng, repo.BUILD_FILES[svc], "dependencies"))
                fl = [cycles.feature_ref(f) for f in per_svc[svc]]
                msgs = None if fl else ["Maintenance: dependency bumps"]
                self.release(svc, t, hunks, messages=msgs, features=fl,
                             schema=svc in repo.MIGRATION_DIR and rng.random() < 0.25)
                if rng.random() < 0.3:
                    self.patch(svc, t + rng.uniform(0.5, 2.5) * D, fixes=fl[0]["ticket"] if fl else None)
                if rng.random() < 0.05:
                    self.rollback(svc, t + rng.uniform(1, 6) * H)

    def rollback(self, svc: str, t: datetime, partial: bool = False, role: str = "background",
                 reason: str = "error budget alert") -> dict[str, Any]:
        payload = {"change_summary": "Roll back to the previous prod version" + (
            " on 2 of 5 nodes (partial)" if partial else ""), "reason": reason, "partial": partial}
        ev = self.event("ROLLBACK", svc, "prod", t, payload, {"role": role})
        h = Hunk(f"deploy/{svc}/release.yaml", "image.tag", ("  tag: current",), ("  tag: previous",), ("image:",),
                 (("  rollout: {maxUnavailable: 2}",) if partial else ()), "config")
        self.last_cds = [self.add_hunk(ev, h, 3)]
        return ev

    def change(self, svc: str, t: datetime, etype: str, intents: list[str], reason: str | None = None,
               role: str = "background", env: str = "prod") -> dict[str, Any]:
        """A CONFIG_CHANGE or INFRA_CHANGE touching one or more parameters (one hunk each)."""
        reason = self.rng.choice(REASONS) if reason is None else reason
        keys = [intent_key(i, svc) for i in intents]
        specs = [repo.param_spec(k) for k in keys]
        if etype == "CONFIG_CHANGE":
            summary = f"Update {', '.join(keys)} in config/{svc}.yaml"
        else:
            res = sorted({s.resource(svc) or k for s, k in zip(specs, keys)})
            summary = f"Terraform apply: {', '.join(res)} ({', '.join(keys)})"
        ev = self.event(etype, svc, env, t, {"change_summary": summary, "reason": reason}, {"role": role})
        self.last_cds = []
        for intent, key, spec in zip(intents, keys, specs):
            h, line = repo.param_hunk(svc, key)
            self.last_cds.append(self.add_hunk(ev, h, line, param_key=key, param_reason=reason or None,
                                               infra_resource=spec.resource(svc), _intent=intent))
        return ev

    def revert(self, change: dict[str, Any], t: datetime, reason: str = "incident remediation") -> dict[str, Any]:
        """Restore every parameter of ``change`` to its value before that change (post-incident fix)."""
        svc = change["service"]
        cds = [c for c in self.world.change_details if c["event_id"] == change["id"] and c.get("param_key")]
        keys = [c["param_key"] for c in cds]
        ev = self.event(change["event_type"], svc, change.get("environment", "prod"), t,
                        {"change_summary": f"Revert {', '.join(keys)} (incident remediation)", "reason": reason},
                        {"role": "remediation"})
        for c in cds:
            h, line = repo.param_hunk(svc, c["param_key"])
            spec = repo.param_spec(c["param_key"])
            self.add_hunk(ev, h, line, param_key=c["param_key"], param_reason=reason, infra_resource=spec.resource(svc),
                          _intent="revert", _revert_of=c["id"])
        return ev

    def config_change(self, svc: str, t: datetime, intent: str, reason: str | None = None,
                      role: str = "background", env: str = "prod") -> dict[str, Any]:
        return self.change(svc, t, "CONFIG_CHANGE", [intent], reason, role, env)

    def infra_change(self, svc: str, t: datetime, intent: str, reason: str | None = None,
                     role: str = "background") -> dict[str, Any]:
        return self.change(svc, t, "INFRA_CHANGE", [intent], reason, role)

    def security_scan(self, svc: str, t: datetime) -> dict[str, Any]:
        counts = {"critical": int(self.rng.random() < 0.1), "high": self.rng.randint(0, 3),
                  "medium": self.rng.randint(2, 10), "low": self.rng.randint(5, 20)}
        new = self.rng.randint(0, 3)
        new_hc = bool(new and self.rng.random() < 0.25 and (counts["critical"] or counts["high"]))
        return self.event("SECURITY_SCAN", svc, "prod", t, {"counts": counts, "new_since_last_deploy": new,
                                                            "new_high_critical": new_hc})

    # ------------------------------------------------------------------ background
    def background(self, svc: str) -> None:
        """Routine config changes (services), Terraform changes (every component) and weekly scans."""
        rng = self.rng
        cfg = intents_for(svc, BENIGN_CONFIG) if svc in config.CODE_SERVICES else []
        infra = intents_for(svc, BENIGN_INFRA)
        if cfg:
            t = config.DAY0 + rng.uniform(1, 6) * D
            while t < config.SIM_END:
                self.config_change(svc, t, rng.choice(cfg))
                t += rng.uniform(5, 12) * D
        if infra:
            t = config.DAY0 + rng.uniform(2, 20) * D
            while t < config.SIM_END:
                self.infra_change(svc, t, rng.choice(infra), reason=rng.choice(("capacity review", "cost review",
                                                                                "engine maintenance", "")))
                t += rng.uniform(18, 40) * D
        if svc in config.CODE_SERVICES:
            t = config.DAY0 + rng.uniform(0, 2) * D
            while t < config.SIM_END:
                self.security_scan(svc, t)
                t += 7 * D

    # ------------------------------------------------------------------ incidents
    def relevant_file(self, svc: str, templates: list[str]) -> str:
        """The code file whose path / symbol best overlaps the error templates."""
        words = {w.lower() for t in templates for w in t.replace("/", " ").replace(".", " ").split() if len(w) > 3}

        def overlap(fs: tuple[str, str, str]) -> int:
            name = (fs[0] + " " + fs[1]).lower()
            return sum(w in name for w in words)

        return sorted(repo.CODE_FILES[svc], key=lambda fs: (-overlap(fs), self.rng.random()))[0][0]

    def apply_scenario(self, inc: dict[str, Any]) -> scenarios.Outcome:
        """Run the incident's scenario; records every event it created for make_variant."""
        mark = len(self.world.events)
        out = scenarios.SCENARIOS[inc["scenario_type"]].build(self, inc)
        inc["scenario_event_ids"] = [e["id"] for e in self.world.events[mark:]]
        inc["cause_event_id"] = out.cause["id"] if out.cause else None
        inc["culprit_change_id"] = out.culprit["id"] if out.culprit else None
        inc["contributing_ids"] = [e["id"] for e in out.contributing]
        inc["cause_service"] = out.cause_service or (out.cause["service"] if out.cause else
                                                     inc.get("planned_cause") or inc["service"])
        inc["blast_radius"] = list(dict.fromkeys(out.blast))
        inc["_templates"] = {"macro": out.macro, "micro": out.micro, "remediation": out.remediation}
        inc["_decoy_window_h"] = out.decoy_window / H if out.decoy_window else None
        inc["_fix"] = out.fix
        if out.external:
            inc["external_factor"] = out.external
        return out

    def place_decoys(self, inc: dict[str, Any], cause: dict[str, Any] | None) -> list[dict[str, Any]]:
        """1-3 harmless changes strictly between the cause and the incident start."""
        rng, svc, start = self.rng, inc["service"], _dt(inc["incident_start"])
        lo = _dt(cause["occurred_at"]) if cause else start - rng.uniform(3, 8) * H
        if cause and inc.get("_decoy_window_h"):  # latent cause: decoys near the incident, never before the cause
            lo = max(lo, start - rng.uniform(0.4, 1.0) * inc["_decoy_window_h"] * H)
        gap = start - lo
        hood = list(dict.fromkeys(list(config.neighbourhood(svc)) + [inc.get("cause_service") or svc]))
        out = []
        deployed: set[str] = set()
        for i in range(rng.randint(1, 3)):
            t = lo + gap * rng.uniform(0.15, 0.9)
            dsvc = svc if i == 0 else rng.choice(hood)
            code = dsvc in config.CODE_SERVICES
            kind = ("deploy" if i == 0 and rng.random() < 0.7 else rng.choice(("config", "infra", "deploy"))) if code \
                else "infra"
            if kind == "deploy" and dsvc in deployed:
                kind = "config"
            if kind == "deploy":
                deployed.add(dsvc)
                hunks = [repo.benign_hunk(rng, f"{dsvc}/{rng.choice(repo.DOC_FILES)}", "docs")]
                ev = self.release(dsvc, t, hunks, messages=[rng.choice(("Update changelog", "Add dashboard panel",
                                                                        "Docs: clarify runbook"))], role="decoy")["prod"]
            elif kind == "config":
                ok = intents_for(dsvc, ("log_level", "metrics_interval", "timeout_up", "backoff_up"))
                ev = self.config_change(dsvc, t, rng.choice(ok), role="decoy")
            else:
                ok = intents_for(dsvc, BENIGN_INFRA) or ["pool_size_up"]
                if not intents_for(dsvc, tuple(ok)):
                    continue
                ev = self.infra_change(dsvc, t, rng.choice(ok), reason="capacity review", role="decoy")
            out.append(ev)
        inc["_decoys"] = inc.get("_decoys", []) + [e["id"] for e in out]
        return out


# --------------------------------------------------------------------------- scenario choice
def select_incidents(anomalies: list[dict[str, Any]], k: int = config.MAX_REAL_INCIDENTS,
                     min_gap: timedelta = 6 * H) -> list[dict[str, Any]]:
    """Severity-ranked, round-robin across services, same-service anomalies >= 6 h apart."""
    by_svc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in sorted(anomalies, key=lambda a: -a["payload"]["severity"]):
        by_svc[a["service"]].append(a)
    order = sorted(by_svc, key=lambda s: -by_svc[s][0]["payload"]["severity"])
    chosen: list[dict[str, Any]] = []
    while len(chosen) < k and any(by_svc[s] for s in order):
        for s in order:
            while by_svc[s]:
                a = by_svc[s].pop(0)
                t = _dt(a["occurred_at"])
                if all(c["service"] != s or abs(_dt(c["occurred_at"]) - t) >= min_gap for c in chosen):
                    chosen.append(a)
                    break
            if len(chosen) >= k:
                break
    return chosen


# --------------------------------------------------------------------------- replay
def _apply_intent(intent: str, old: Any) -> Any:
    return INTENTS[intent][1](old)


def post_deploy_line(ev: dict[str, Any], logs: dict[str, pd.DataFrame] | None) -> str:
    """Error ratio 30 min before vs after the deploy, from the log lines of that component
    ("no log coverage" when there are no lines on either side)."""
    t = pd.Timestamp(_dt(ev["occurred_at"]))
    if ev["environment"] != "prod":
        return "no log coverage (non-prod environment)"
    if logs is None or ev["service"] not in logs:
        return "no log coverage"
    df = logs[ev["service"]]
    before = df[(df["ts"] >= t - 30 * M) & (df["ts"] < t)]
    after = df[(df["ts"] >= t) & (df["ts"] < t + 30 * M)]
    if len(before) < 3 or len(after) < 3:
        return "no log coverage"

    def ratio(x: pd.DataFrame) -> str:
        return f"{x['is_error'].mean():.2f} ({int(x['is_error'].sum())}/{len(x)} lines)"

    return f"error ratio {ratio(before)} before -> {ratio(after)} after"


def replay(world: World, logs: dict[str, pd.DataFrame] | None) -> None:
    """Chronological pass: versions, parameter values + diffs, linked tests/scans, post-deploy lines."""
    sims = sorted(world.sim_events(), key=lambda e: (_dt(e["occurred_at"]), e["id"]))
    by_id = world.by_id()
    # releases are numbered in the order they reach prod, so prod versions only go up
    first_seen: dict[str, datetime] = {}
    for e in sims:
        rel = e["_sim"].get("release")
        if e["event_type"] == "DEPLOY" and rel and e["environment"] == "prod":
            first_seen.setdefault(rel, _dt(e["occurred_at"]))
    for e in sims:
        rel = e["_sim"].get("release")
        if e["event_type"] == "DEPLOY" and rel:
            first_seen.setdefault(rel, _dt(e["occurred_at"]))
    rel_version: dict[str, str] = {}
    per_svc: Counter[str] = Counter()
    for rel in sorted(first_seen, key=lambda r: first_seen[r]):
        svc = rel.split("#")[0]
        per_svc[svc] += 1
        rel_version[rel] = f"v{MAJOR[svc]}.{per_svc[svc]}.0"
    current: dict[tuple[str, str], str] = {}
    history: dict[tuple[str, str], list[str]] = defaultdict(list)
    params: dict[str, dict[str, Any]] = {s: default_params(s) for s in config.SERVICES}
    cds_by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cd in world.change_details:
        cds_by_event[cd["event_id"]].append(cd)
    last_scan: dict[str, dict[str, Any]] = {}
    revs: Counter[tuple[str, str]] = Counter()
    before_value: dict[str, Any] = {}  # change_detail id -> parameter value before that change
    from ff.ingest.cycles import cycle_id

    for e in sims:
        e["payload"]["cycle"] = cycle_id(_dt(e["occurred_at"]))
    for e in sims:
        svc, env, t = e["service"], e["environment"], e["event_type"]
        key = (svc, env)
        base = f"v{MAJOR.get(svc, 1)}.0.0"
        if t == "SECURITY_SCAN":
            last_scan[svc] = e
        elif t in ("CONFIG_CHANGE", "INFRA_CHANGE"):
            prefix = "cfg" if t == "CONFIG_CHANGE" else "tf"
            e["version_from"] = f"{prefix}-r{revs[(svc, prefix)]}"
            revs[(svc, prefix)] += 1
            e["version_to"] = f"{prefix}-r{revs[(svc, prefix)]}"
        elif t == "DEPLOY":
            e["version_from"] = current.get(key, base)
            e["version_to"] = rel_version[e["_sim"]["release"]]
        elif t == "PATCH":
            cur = current.get(key, base)
            major, minor, p = cur.lstrip("v").split(".")
            e["version_from"], e["version_to"] = cur, f"v{major}.{minor}.{int(p) + 1}"
        elif t == "ROLLBACK":
            hist = history[key]
            e["version_from"] = current.get(key, base)
            target = e["_sim"].get("rollback_to")
            e["version_to"] = (by_id[target]["version_from"] if target in by_id and by_id[target].get("version_from")
                               else (hist[-2] if len(hist) >= 2 else base))
        if t in ("DEPLOY", "PATCH", "ROLLBACK"):
            current[key] = e["version_to"]
            history[key].append(e["version_to"])
            e["payload"]["post_deploy"] = post_deploy_line(e, logs)
            scan = last_scan.get(svc)
            e["payload"]["security"] = ({"counts": scan["payload"]["counts"],
                                         "new_since_last_deploy": scan["payload"]["new_since_last_deploy"]}
                                        if scan else None)
            e["payload"]["scan_id"] = scan["id"] if scan else None
            tr = by_id.get(e["payload"].get("test_run_id") or "")
            if tr is not None:
                tp = tr["payload"]
                e["payload"]["tests"] = {k: tp[k] for k in ("passed", "total", "failed", "skipped",
                                                            "skipped_suites", "failed_suites")}
                tr["version_to"] = e["version_to"]
                tr["related_event_id"] = e["id"]
        for cd in cds_by_event.get(e["id"], []):
            intent = cd.get("_intent")
            if intent:
                k = cd["param_key"]
                old = params[svc][k]
                if intent == "revert":
                    new = before_value.get(cd.get("_revert_of"), repo.param_spec(k).default)
                else:
                    new = _apply_intent(intent, old)
                before_value[cd["id"]] = old
                params[svc][k] = new
                cd["param_old"], cd["param_new"] = str(old), str(new)
                cd["diff"] = repo.render_diff(Hunk(**{**cd["_hunk"], "before": tuple(cd["_hunk"]["before"]),
                                                     "after": tuple(cd["_hunk"]["after"]),
                                                     "ctx_before": tuple(cd["_hunk"]["ctx_before"]),
                                                     "ctx_after": tuple(cd["_hunk"]["ctx_after"])}),
                                              cd["_line"], old, new)
    rel_tests: dict[str, dict[str, dict[str, list[str]]]] = defaultdict(dict)
    for e in sims:
        if e["event_type"] == "DEPLOY" and e["payload"].get("tests"):
            t = e["payload"]["tests"]
            rel_tests[e["_sim"]["release"]][e["environment"]] = {
                "skipped_suites": t["skipped_suites"], "failed_suites": t["failed_suites"]}
    for e in sims:
        if e["event_type"] == "DEPLOY":
            e["payload"]["release_tests"] = rel_tests.get(e["_sim"]["release"], {})
    world.__dict__["final_params"] = params


def final_params(world: World) -> dict[str, dict[str, Any]]:
    """Latest config / infra state per component after :func:`replay` (used by the runbooks)."""
    return world.__dict__.get("final_params", {})


# --------------------------------------------------------------------------- finalize
def finalize(world: World) -> World:
    """Renumber events chronologically to evt_N and change details to chg_N; remap every reference;
    fill each incident's RCA ground truth."""
    events = sorted(world.events, key=lambda e: (_dt(e["occurred_at"]), e["source"] != "logs", e["id"]))
    emap = {e["id"]: f"evt_{i + 1}" for i, e in enumerate(events)}
    num = {v: i for i, v in enumerate(emap.values())}
    cds = sorted(world.change_details, key=lambda c: (num[emap[c["event_id"]]], c["id"]))
    cmap = {c["id"]: f"chg_{i + 1}" for i, c in enumerate(cds)}

    def m(x: str | None) -> str | None:
        return emap.get(x, x) if x else x

    for e in events:
        e["id"] = emap[e["id"]]
        e["occurred_at"] = _iso(e["occurred_at"])
        e["end_at"] = _iso(e["end_at"]) if e.get("end_at") else None
        e["related_event_id"] = m(e.get("related_event_id"))
        p = e["payload"]
        for k in ("test_run_id", "scan_id"):
            if p.get(k):
                p[k] = m(p[k])
        if p.get("error_signatures"):
            p["error_signatures"] = [m(x) for x in p["error_signatures"]]
        if e.get("_sim", {}).get("rollback_to"):
            e["_sim"]["rollback_to"] = m(e["_sim"]["rollback_to"])
    for c in cds:
        c["id"], c["event_id"] = cmap[c["id"]], emap[c["event_id"]]
        if c.get("_revert_of"):
            c["_revert_of"] = cmap.get(c["_revert_of"], c["_revert_of"])
    for s in world.signatures:
        s["id"], s["anomaly_id"] = m(s["id"]), m(s["anomaly_id"])
    for inc in world.incidents:
        inc["anomaly_id"] = m(inc["anomaly_id"])
        inc["cause_event_id"] = m(inc.get("cause_event_id"))
        inc["decoy_ids"] = [m(x) for x in inc.get("decoy_ids", [])]
        inc["contributing_ids"] = [m(x) for x in inc.get("contributing_ids", [])]
        inc["remediation_ids"] = [m(x) for x in inc.get("remediation_ids", [])]
        inc["scenario_event_ids"] = [emap[x] for x in inc.get("scenario_event_ids", []) if x in emap]
        if inc.get("culprit_change_id"):
            inc["culprit_change_id"] = cmap.get(inc["culprit_change_id"], inc["culprit_change_id"])
    world.events, world.change_details = events, cds
    for inc in world.incidents:
        fill_ground_truth(world, inc)
    return world


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def fill_ground_truth(world: World, inc: dict[str, Any]) -> None:
    """expected_files / expected_params (macro), culprit (micro), RCA text fields and action items."""
    from ff.engine.rca import action_items

    by_id = world.by_id()
    cause_id = inc.get("cause_event_id")
    cds = world.details_of(cause_id) if cause_id else []
    inc["expected_files"] = sorted({c["file"] for c in cds if c.get("file")})
    inc["expected_params"] = sorted({c["param_key"] for c in cds if c.get("param_key")})
    culprit = next((c for c in world.change_details if c["id"] == inc.get("culprit_change_id")), None)
    inc["culprit"] = None
    if culprit:
        inc["culprit"] = {"change_id": culprit["id"], "event_id": culprit["event_id"], "file": culprit["file"],
                          "line": culprit["line_start"], "symbol": culprit["symbol"], "diff": culprit["diff"],
                          "param_key": culprit.get("param_key")}
    tpl = inc.get("_templates") or {}
    cause = by_id.get(cause_id) if cause_id else None
    ver_src = cause or (by_id.get(culprit["event_id"]) if culprit else None) or {}
    param = next((c for c in cds if c.get("param_key")), None) or (culprit if culprit and culprit.get("param_key") else None)
    values = _SafeDict(svc=(cause or {}).get("service", inc.get("cause_service") or inc["service"]),
                       version_from=ver_src.get("version_from", ""),
                       version_to=ver_src.get("version_to", ""), file=culprit["file"] if culprit else "",
                       line=culprit["line_start"] if culprit else "", symbol=culprit["symbol"] if culprit else "",
                       key=param["param_key"] if param else "", old=param["param_old"] if param else "",
                       new=param["param_new"] if param else "", resource=(param or {}).get("infra_resource") or "",
                       culprit_event=culprit["event_id"] if culprit else "", alert_svc=inc["service"],
                       cycle=(ver_src.get("payload") or {}).get("cycle", ""))
    inc["rca"] = {k: v.format_map(values) for k, v in tpl.items()}
    breaking = {**culprit, "line_start": culprit["line_start"]} if culprit else None
    tmpl = (inc.get("error_templates") or [None])[0]
    inc["action_items"] = action_items(cause, breaking, tmpl)


# --------------------------------------------------------------------------- build
def anchor(sim: Simulator, incs: list[dict[str, Any]]) -> None:
    """Run each incident's scenario, place its decoys and the team's mitigation (provisional ids)."""
    for inc in incs:
        out = sim.apply_scenario(inc)
        sim.place_decoys(inc, out.cause)
        inc["decoy_ids"] = inc.pop("_decoys")
        start = _dt(inc["incident_start"])
        mitigated = _dt(inc["mitigated_at"]) if inc.get("mitigated_at") else start + sim.rng.uniform(2.5, 5) * H
        inc["mitigated_at"] = _iso(mitigated)
        rem = []
        if mitigated < config.SIM_END and out.cause is not None:
            if out.cause["event_type"] in ("CONFIG_CHANGE", "INFRA_CHANGE"):
                rem.append(sim.revert(out.cause, mitigated))
            elif out.cause["event_type"] in ("DEPLOY", "PATCH"):
                rb = sim.rollback(out.cause["service"], mitigated, role="remediation",
                                  reason=f"mitigation of the {inc['service']} incident")
                rb["_sim"]["rollback_to"] = out.cause["id"]
                rem.append(rb)
            for ch in out.contributing:  # harmful parameter changes that contributed are reverted later
                if ch["event_type"] in ("CONFIG_CHANGE", "INFRA_CHANGE"):
                    t = mitigated + sim.rng.uniform(1, 4) * H
                    if t < config.SIM_END:
                        rem.append(sim.revert(ch, t, reason="post-incident clean-up"))
        inc["remediation_ids"] = [e["id"] for e in rem]
        inc["scenario_event_ids"] = inc.get("scenario_event_ids", []) + [e["id"] for e in rem]


def build_world(log_data: dict[str, Any], logs: dict[str, pd.DataFrame] | None, incs: list[dict[str, Any]],
                seed: int = config.SEED) -> World:
    """Background history + release trains + anchored incidents + replay + descriptions + finalize."""
    from ff.ingest.describe import describe_world

    world = World(events=copy.deepcopy(log_data["events"]), signatures=copy.deepcopy(log_data["signatures"]))
    for e in world.events:
        e.setdefault("_sim", {})
        e.setdefault("related_event_id", None)
    sim = Simulator(seed, world)
    for svc in config.SERVICES:
        sim.background(svc)
    sim.release_trains()
    incs = copy.deepcopy(incs)
    anchor(sim, incs)
    world.incidents = incs
    world.events = [e for e in world.events if _dt(e["occurred_at"]) <= config.SIM_END]
    keep = {e["id"] for e in world.events}
    world.change_details = [c for c in world.change_details if c["event_id"] in keep]
    replay(world, logs)
    describe_world(world, seed)
    return finalize(world)


def make_variant(world: World, incident_id: str, seed: int, logs: dict[str, pd.DataFrame] | None = None) -> World:
    """A new world where one incident's scenario (lags, decoys, values) is re-drawn.

    Every event the scenario created (and its releases) plus the decoys are removed; the
    scenario runs again with ``seed``; then the world is replayed and renumbered. The
    incident id gets a ``-v<seed>`` suffix and ``split='heldout'``.
    """
    from ff.ingest.describe import describe_world

    w = copy.deepcopy(world)
    inc = next(i for i in w.incidents if i["id"] == incident_id)
    by_id = w.by_id()
    drop_ids = [x for x in [inc.get("cause_event_id"), *inc.get("decoy_ids", []), *inc.get("contributing_ids", []),
                            *inc.get("scenario_event_ids", []), *inc.get("remediation_ids", [])] if x and x in by_id]
    releases = {by_id[x]["_sim"].get("release") for x in drop_ids if by_id[x]["_sim"].get("release")}
    drop = set(drop_ids) | {e["id"] for e in w.events if e["_sim"].get("release") in releases}
    w.events = [e for e in w.events if e["id"] not in drop]
    w.change_details = [c for c in w.change_details if c["event_id"] not in drop]
    sim = Simulator(seed, w, prefix=f"V{seed}_")
    inc.update({"id": f"{incident_id}-v{seed}", "split": "heldout", "decoy_ids": [],
                "planned_cause": inc.get("planned_cause") or inc.get("cause_service")})
    anchor(sim, [inc])
    w.incidents = [inc]
    replay(w, logs)
    describe_world(w, seed)
    return finalize(w)


# --------------------------------------------------------------------------- io
PRIVATE_CD_KEYS = ("_intent", "_hunk", "_line", "_revert_of")


def public_change_detail(c: dict[str, Any]) -> dict[str, Any]:
    """A change_detail row without the simulator's private fields."""
    return {k: v for k, v in c.items() if k not in PRIVATE_CD_KEYS}


def save_world(world: World, paths: Paths) -> None:
    """Write data/sim/*.jsonl, data/incidents/<id>.json and data/incidents/index.csv."""
    paths.ensure()

    def dump(name: str, rows: list[dict[str, Any]]) -> None:
        with open(paths.sim / name, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")

    dump("events.jsonl", world.sim_events())
    dump("log_events.jsonl", world.log_events())
    dump("change_details.jsonl", world.change_details)
    dump("error_signatures.jsonl", world.signatures)
    (paths.sim / "final_params.json").write_text(json.dumps(final_params(world), indent=1), encoding="utf-8")
    for old in paths.incidents.glob("INC-*.json"):
        old.unlink()
    for inc in world.incidents:
        (paths.incidents / f"{inc['id']}.json").write_text(json.dumps(inc, indent=1, default=str), encoding="utf-8")
    rows = [{"id": i["id"], "split": i["split"], "start": i["incident_start"][:16], "alert": i["service"],
             "severity": i.get("severity"), "title": i.get("title"), "scenario": i["scenario_type"],
             "category": i.get("category"), "cause_component": i.get("cause_service"), "cause": i.get("cause_event_id"),
             "breaking_line": i.get("culprit_change_id"), "file": (i.get("culprit") or {}).get("file"),
             "logs": i.get("source")} for i in world.incidents]
    pd.DataFrame(rows).to_csv(paths.incidents / "index.csv", index=False)


def load_world(paths: Paths | None = None) -> World:
    """Read a saved world back."""
    paths = paths or get_paths()

    def read(name: str) -> list[dict[str, Any]]:
        with open(paths.sim / name, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    w = World(events=read("log_events.jsonl") + read("events.jsonl"), change_details=read("change_details.jsonl"),
              signatures=read("error_signatures.jsonl"),
              incidents=[json.loads(p.read_text(encoding="utf-8")) for p in sorted(paths.incidents.glob("INC-*.json"))])
    w.events.sort(key=lambda e: (e["occurred_at"], int(e["id"].split("_")[1])))
    fp = paths.sim / "final_params.json"
    if fp.exists():
        w.__dict__["final_params"] = json.loads(fp.read_text(encoding="utf-8"))
    return w


def load_logs_for_replay(paths: Paths | None = None) -> dict[str, pd.DataFrame]:
    """Log lines (real + synthetic) per component, for post-deploy lines in variants."""
    paths = paths or get_paths()
    out = {}
    for svc in config.SERVICES:
        f = paths.derived / f"lines_{svc}.parquet"
        if f.exists():
            out[svc] = pd.read_parquet(f, columns=["ts", "is_error"])
    return out


def incident_table(world: World) -> str:
    """Console table: incident x scenario x alert x cause x culprit line."""
    rows = [f"{'id':8s} {'split':8s} {'alert':16s} {'scenario':20s} {'cause comp.':16s} {'cause':9s} {'culprit':10s} "
            f"file:line"]
    for i in world.incidents:
        c = i.get("culprit") or {}
        rows.append(f"{i['id']:8s} {i['split']:8s} {i['service']:16s} {i['scenario_type']:20s} "
                    f"{str(i.get('cause_service')):16s} {str(i['cause_event_id']):9s} "
                    f"{str(i.get('culprit_change_id')):10s} {c.get('file', '-')}:{c.get('line', '')}")
    return "\n".join(rows)


def run(paths: Paths | None = None, seed: int = config.SEED) -> World:
    """Build the whole dataset (plan, telemetry, log analysis, history) and save it."""
    from ff.ingest import dataset

    return dataset.build(paths, seed=seed)["world"]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=config.SEED)
    ap.add_argument("--list", action="store_true", help="print the saved incidents without rebuilding")
    args = ap.parse_args(argv)
    if args.list:
        print(incident_table(load_world()))
        return
    world = run(seed=args.seed)
    print(incident_table(world))
    print(Counter(e["event_type"] for e in world.events))


if __name__ == "__main__":
    main()
