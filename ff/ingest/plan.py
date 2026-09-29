"""Incident plan: which 100 incidents happen, where, when, why, and how they get split.

* Up to ``MAX_REAL_INCIDENTS`` incidents sit on anomalies of the REAL LogHub logs (last 72 h); they
  get a scenario from their service and error templates (``scenarios.match``).
* The rest are *planned* over the six months: every planned scenario is used about equally, the
  cause component is drawn from the scenario's candidates and the alerting component from the
  paging components in its blast radius (60 % of the time a different component than the cause,
  so the investigation has to follow the dependency chain). Incidents are at least
  ``INCIDENT_MIN_GAP_HOURS`` apart, mostly in business hours, and are mitigated 2.5-5 h later.
* Split: 80 % ``dev`` / 20 % ``heldout``, stratified by scenario so every failure class appears in
  both halves when it occurs at least twice.

The telemetry of planned incidents is generated from this plan (``ff/ingest/telemetry.py``); the
simulator then places the changes behind each one (``ff/ingest/scenarios.py``).
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from ff import config
from ff.ingest import scenarios, telemetry

H = timedelta(hours=1)
D = timedelta(days=1)

# scenarios whose cause lands days before the incident need room at the start of the history
LATENT_DAYS = {"LATENT_DISK": 21, "S3_LIFECYCLE": 6, "RDS_DOWNSIZE": 5, "MEMORY_INFRA": 6, "FLAG_DORMANT": 5,
               "EBS_THROUGHPUT": 4, "DB_POOL": 7, "SECRET_ROTATION": 9, "REDIS_DOWNSIZE": 3, "ASG_CAPACITY": 3,
               "DB_MIGRATION": 7, "HOTFIX_REGRESSION": 4, "CIOD_LEAK": 7, "IMAGE_CACHE_SYNC": 4, "ELECTION_PORT": 4,
               "RETRY_STORM": 2, "MEMORY_LEAK": 3}
FLAVOR_TITLE = {"timeout": "Latency and timeouts on {alert}", "5xx": "Elevated 5xx errors on {alert}",
                "refused": "Connection failures on {alert}", "auth": "Authentication failures on {alert}",
                "throttle": "Throttling (429) on {alert}", "notfound": "Missing resources on {alert}",
                "data": "Processing errors on {alert}"}
ALARM = {"timeout": "p99 latency > 2 s for 5 min", "5xx": "5XX error rate > 2 % for 5 min",
         "refused": "5XX error rate > 2 % for 5 min", "auth": "authentication-failure rate > 5 % for 5 min",
         "throttle": "429 rate > 1 % for 5 min", "notfound": "4XX rate > 5 % for 10 min",
         "data": "error-log rate > 20/min (CloudWatch Logs metric filter)"}
IMPACT = {"timeout": "requests slowed down and a share of them timed out", "5xx": "a share of requests failed with 5xx",
          "refused": "requests failed while connections were refused", "auth": "users were logged out or rejected",
          "throttle": "customers were throttled", "notfound": "operations on older resources failed",
          "data": "background processing failed; customer-visible data was delayed or wrong"}


def planned_scenarios() -> list[scenarios.Scenario]:
    return [s for s in scenarios.PLANNED if s.name != "NO_CHANGE"]


def _business_time(rng: random.Random, lo: datetime, hi: datetime) -> datetime:
    """A time in [lo, hi], preferably 07:00-20:00 UTC on a weekday."""
    best = lo + (hi - lo) * rng.random()
    for _ in range(12):
        t = lo + (hi - lo) * rng.random()
        if 7 <= t.hour < 20 and t.weekday() < 5 and rng.random() < 0.85:
            return t
        best = t
    return best


def choose_alert(s: scenarios.Scenario, cause: str, rng: random.Random) -> str:
    cands = scenarios.alert_candidates(s, cause)
    if s.name in ("CROSS_SERVICE", "CONTRACT_BREAK"):
        far = [a for a in cands if a != cause and cause not in config.neighbourhood(a)]
        cands = far or [a for a in cands if a != cause] or cands
    others = [a for a in cands if a != cause]
    if others and (cause not in cands or rng.random() < 0.6):
        return rng.choice(others)
    return cause if cause in cands else rng.choice(cands)


def enrich(inc: dict[str, Any], rng: random.Random) -> None:
    """Title, severity, detection and impact of an incident (what the on-call engineer sees first)."""
    flavor = telemetry.FAULTS.get(inc["scenario_type"], ("5xx", {}))[0]
    alert = inc["service"]
    inc["title"] = FLAVOR_TITLE[flavor].format(alert=alert)
    edge = alert == "api-gateway" or "api-gateway" in scenarios.blast_radius(inc.get("planned_cause") or alert)
    inc["severity"] = "SEV-1" if edge and rng.random() < 0.35 else ("SEV-2" if rng.random() < 0.6 else "SEV-3")
    inc["detection"] = {"alert": f"{alert}: {ALARM[flavor]}", "source": "CloudWatch alarm -> PagerDuty",
                        "flavor": flavor}
    inc["impact"] = f"{IMPACT[flavor].capitalize()} on {alert}"


def plan(real_anomalies: list[dict[str, Any]], n_total: int = config.N_INCIDENTS, seed: int = config.SEED,
         max_real: int = config.MAX_REAL_INCIDENTS) -> list[dict[str, Any]]:
    """The incident plan (provisional ids ``P-###``), chronological."""
    from ff.ingest.simulator import select_incidents

    rng = random.Random(seed + 11)
    incs: list[dict[str, Any]] = []
    # 1) incidents on real LogHub anomalies
    real = select_incidents(real_anomalies, min(max_real, max(1, n_total // 10))) if real_anomalies else []
    used: dict[Any, int] = {}
    for a in real:
        p = a["payload"]
        inc = {"service": a["service"], "incident_start": a["occurred_at"], "planned_start": a["occurred_at"],
               "anomaly_id": a["id"], "source": "loghub", "planned_cause": None,
               "error_templates": p.get("templates", [])[:5]}
        inc["scenario_type"] = scenarios.match(inc, used)
        used[inc["scenario_type"]] = used.get(inc["scenario_type"], 0) + 1
        used[(inc["scenario_type"], inc["service"])] = used.get((inc["scenario_type"], inc["service"]), 0) + 1
        start = datetime.fromisoformat(a["occurred_at"])
        inc["mitigated_at"] = (start + rng.uniform(2.5, 5) * H).isoformat()
        incs.append(inc)
    # 2) planned incidents over the history
    n_syn = max(0, n_total - len(incs))
    pool = planned_scenarios()
    names: list[scenarios.Scenario] = []
    while len(names) < n_syn:
        batch = pool[:]
        rng.shuffle(batch)
        names += batch
    names = names[:n_syn]
    lo = config.DAY0 + 2 * D
    hi = config.LOG_START - 18 * H
    span = hi - lo
    bins = [(lo + span * i / max(1, n_syn), lo + span * (i + 1) / max(1, n_syn)) for i in range(n_syn)]
    # scenarios whose cause lands days earlier only take bins with enough history before them
    free = list(range(n_syn))
    assign: dict[int, int] = {}
    for k in sorted(range(n_syn), key=lambda k: -LATENT_DAYS.get(names[k].name, 1)):
        need = config.DAY0 + (LATENT_DAYS.get(names[k].name, 1) + 1) * D
        ok = [b for b in free if bins[b][0] >= need] or free
        slot = rng.choice(ok)
        free.remove(slot)
        assign[k] = slot
    gap = max(0.0, (span / max(1, n_syn) - config.INCIDENT_MIN_GAP_HOURS * H) / 2 / H)
    for k, s in enumerate(names):
        b0, b1 = bins[assign[k]]
        t = _business_time(rng, b0 + gap * H, b1 - gap * H) if gap > 0 else b0 + (b1 - b0) / 2
        cause = rng.choice(s.causes)
        alert = choose_alert(s, cause, rng)
        inc = {"service": alert, "planned_start": t.isoformat(), "incident_start": t.isoformat(), "source": "synthetic",
               "scenario_type": s.name, "planned_cause": cause, "anomaly_id": None,
               "mitigated_at": (t + rng.uniform(2.5, 5) * H).isoformat()}
        incs.append(inc)
    incs.sort(key=lambda i: i["planned_start"])
    for k, inc in enumerate(incs):
        inc["id"] = f"P-{k + 1:03d}"
        s = scenarios.SCENARIOS[inc["scenario_type"]]
        inc["scenario_family"], inc["scenario_title"], inc["category"] = s.family, s.title, s.category
        enrich(inc, rng)
    split(incs, rng)
    return incs


def split(incs: list[dict[str, Any]], rng: random.Random, dev_share: float = config.DEV_SHARE) -> None:
    """``dev`` / ``heldout``, stratified by scenario (about 1 in 5 of each scenario is held out)."""
    n_held = len(incs) - round(len(incs) * dev_share)
    groups: dict[str, list[dict[str, Any]]] = {}
    for inc in incs:
        groups.setdefault(inc["scenario_type"], []).append(inc)
    ranked = []
    for g in groups.values():
        rng.shuffle(g)
        for k, inc in enumerate(g):
            # the last item of groups with >= 2 incidents ranks first, so most scenarios reach both halves
            ranked.append(((len(g) - k - 0.5) / len(g) if len(g) > 1 else 0.9 + rng.random() * 0.1, rng.random(), inc))
    ranked.sort(key=lambda x: (x[0], x[1]))
    held = {id(x[2]) for x in ranked[:n_held]}
    for inc in incs:
        inc["split"] = "heldout" if id(inc) in held else "dev"


def views(incs: list[dict[str, Any]]) -> list[telemetry.IncidentPlanView]:
    """What the telemetry generator needs from the planned (synthetic) incidents."""
    out = []
    for inc in incs:
        if inc["source"] != "synthetic":
            continue
        extra = tuple(scenarios.SCENARIOS[inc["scenario_type"]].alerts)
        out.append(telemetry.IncidentPlanView(inc["id"], inc["scenario_type"], inc["planned_cause"], inc["service"],
                                              datetime.fromisoformat(inc["planned_start"]),
                                              datetime.fromisoformat(inc["mitigated_at"]), extra))
    return out
