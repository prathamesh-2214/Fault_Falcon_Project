"""Build the whole dataset: plan -> telemetry -> log analysis -> change history -> files.

1. Parse the REAL LogHub logs (five components, rescaled onto the last 72 h) and detect their
   anomalies (``log_events.analyse_service``).
2. Plan the incidents (``plan.plan``): up to 10 on real anomalies, the rest over six months.
3. Generate synthetic telemetry for the planned incidents, quiet baseline windows and a few
   harmless blips (``telemetry.generate``).
4. Run the detector on both streams and write ``data/derived`` (``log_events.run``).
5. Link every planned incident to the ANOMALY the detector found on its alerting component at
   the planned time; the incident starts when that anomaly starts.
6. Simulate the six-month change history and place each incident's causes
   (``simulator.build_world``), then write ``data/sim``, ``data/incidents`` and log samples.

    python -m ff.ingest.dataset            # (make ingest runs it through ff.ingest.pipeline)
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from ff import config
from ff.config import Paths, get_paths
from ff.ingest import log_events, loghub, plan, simulator, telemetry


def link(incs: list[dict[str, Any]], events: list[dict[str, Any]], window: timedelta = timedelta(minutes=25)) -> int:
    """Attach each incident to its detected anomaly (start time, symptom, numbers); returns misses."""
    anomalies = [e for e in events if e["event_type"] == "ANOMALY"]
    by_id = {a["id"]: a for a in anomalies}
    misses = 0
    for inc in incs:
        a = by_id.get(inc.get("anomaly_id") or "")
        if a is None:
            t = datetime.fromisoformat(inc["planned_start"])
            cands = [x for x in anomalies if x["service"] == inc["service"] and x.get("log_source") == "synthetic"
                     and abs(datetime.fromisoformat(x["occurred_at"]) - t) <= window]
            a = min(cands, key=lambda x: abs(datetime.fromisoformat(x["occurred_at"]) - t), default=None)
        if a is None:
            misses += 1
            inc["detected"] = False
            continue
        p = a["payload"]
        inc["detected"] = True
        inc.update({
            "anomaly_id": a["id"], "incident_start": a["occurred_at"], "symptom": a["narrative"],
            "error_templates": p.get("templates", [])[:5],
            "anomaly_numbers": {"peak_error_ratio": round(p["peak_error_ratio"], 3),
                                "baseline_median_error_ratio": round(p["baseline_median_error_ratio"], 3),
                                "error_lines": p["error_lines"], "lines": p["lines"],
                                "p95_latency": p.get("p95_latency"), "reasons": p.get("reasons", [])},
        })
        if inc.get("mitigated_at") and inc["mitigated_at"] <= inc["incident_start"]:
            inc["mitigated_at"] = (datetime.fromisoformat(inc["incident_start"]) + timedelta(hours=2.5)).isoformat()
    return misses


def number(incs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detected incidents only, chronological, ids INC-001 ..."""
    keep = sorted((i for i in incs if i.get("detected")), key=lambda i: i["incident_start"])
    for k, inc in enumerate(keep):
        inc["plan_id"] = inc["id"]
        inc["id"] = f"INC-{k + 1:03d}"
    return keep


def write_samples(paths: Paths, lines: pd.DataFrame, per_component: int = 400) -> None:
    """Human-readable excerpts of the telemetry: data/telemetry/<component>.log (around its busiest hour)."""
    out = paths.data / "telemetry"
    out.mkdir(parents=True, exist_ok=True)
    for svc, g in lines.groupby("service"):
        g = g.sort_values("ts")
        err = g[g["is_error"]]
        center = err["ts"].iloc[len(err) // 2] if len(err) else g["ts"].iloc[len(g) // 2]
        win = g[(g["ts"] >= center - pd.Timedelta(hours=2)) & (g["ts"] <= center + pd.Timedelta(hours=1))]
        text = "\n".join(f"{t:%Y-%m-%dT%H:%M:%S}Z {lv:5s} [{src}] {c}" for t, lv, src, c in
                         win[["ts", "level", "source", "content"]].head(per_component).itertuples(index=False))
        (out / f"{svc}.log").write_text(text + "\n", encoding="utf-8")


def build(paths: Paths | None = None, seed: int = config.SEED, n_incidents: int = config.N_INCIDENTS,
          store: Any | None = None, llm: Any | None = None) -> dict[str, Any]:
    """Everything above; returns {"world", "plan", "misses", "results"}."""
    paths = (paths or get_paths()).ensure()
    logs = loghub.load_service_logs(paths)
    real_anoms = [a for svc in config.LOGHUB_SERVICES if svc in logs
                  for a in log_events.analyse_service(svc, logs[svc]).anomalies]
    incs = plan.plan(real_anoms, n_incidents, seed)
    syn = telemetry.generate(plan.views(incs), seed, real=logs)
    results = log_events.run(paths, store, llm, logs=logs, synthetic=syn)
    log_data = log_events.load_log_events(paths)
    misses = link(incs, log_data["events"])
    incs = number(incs)
    world = simulator.build_world(log_data, simulator.load_logs_for_replay(paths), incs, seed)
    simulator.save_world(world, paths)
    lines = pd.concat([pd.read_parquet(f, columns=["ts", "service", "level", "source", "content", "is_error"])
                       for f in sorted(paths.derived.glob("lines_*.parquet"))], ignore_index=True)
    write_samples(paths, lines[lines["source"] == "synthetic"])
    return {"world": world, "plan": incs, "misses": misses, "results": results}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=config.SEED)
    args = ap.parse_args(argv)
    out = build(seed=args.seed)
    w = out["world"]
    print(simulator.incident_table(w))
    print(f"{len(w.incidents)} incidents ({sum(i['split'] == 'dev' for i in w.incidents)} dev / "
          f"{sum(i['split'] == 'heldout' for i in w.incidents)} held out); {out['misses']} planned incidents were not "
          f"detected; {len(w.events)} events, {len(w.change_details)} diff hunks")


if __name__ == "__main__":
    main()
