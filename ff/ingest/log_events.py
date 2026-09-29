"""Log-derived records: Drain templates, windows, baseline, anomalies and error signatures.

Everything here is computed from log lines: the real LogHub logs (five components, last 72 h)
and the synthetic telemetry of the other components / periods (``ff/ingest/telemetry.py``).
The two streams are analysed separately with the same code (their baselines differ). The LLM
only turns computed numbers into a short narrative, and that narrative is discarded (template
sentence instead) if it contains any number that is not in its input.

Pipeline per service and stream:
1. Drain3 template mining in time order (the source's template ids kept as a cross-check).
2. Windows with rate, error ratio, template counts, first-seen templates and (components whose
   logs carry latency: cloud-api, api-gateway) p50/p95 latency and 5xx share. Real stream: fixed
   windows of ``max(5 min, span / 150)``; synthetic stream: 10-minute windows inside each
   contiguous segment of lines.
3. Two-pass baseline: flag with a baseline from all windows, recompute the baseline from
   the unflagged windows, then flag again (final).
4. Significance rules; adjacent significant windows merge into anomaly spans.
5. ANOMALY and ERROR_SIGNATURE records (provisional ids ``L...``; the simulator assigns
   the final ``evt_N`` ids chronologically across logs and simulated events).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import numpy as np
import pandas as pd

from ff import config
from ff.config import Paths, get_paths
from ff.store.sqlite_store import SqliteStore

LLMFn = Callable[[str, str], str]  # (system, user) -> text

# --------------------------------------------------------------------------- redaction
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<EMAIL>"),
    (re.compile(r"(?i)\b(token|password|passwd|secret|api[_-]?key|auth)\s*[=:]\s*\S+"), r"\1=<REDACTED>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "<HEX>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<IP>"),
)


def redact(text: str) -> str:
    """Remove emails, secrets (key=value), UUIDs, 32+ char hex strings and IPv4 addresses."""
    for pat, repl in _REDACTIONS:
        text = pat.sub(repl, text)
    return text


# --------------------------------------------------------------------------- drain
def mine_templates(df: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """Run Drain3 in time order; add ``drain_id`` and ``drain_template`` columns.

    Returns the frame and the agreement with LogHub's EventId: the share of lines whose
    EventId equals the majority EventId of their Drain cluster.
    """
    from drain3 import TemplateMiner
    from drain3.template_miner_config import TemplateMinerConfig

    logging.getLogger("drain3").setLevel(logging.ERROR)
    cfg = TemplateMinerConfig()
    cfg.drain_sim_th = config.DRAIN_SIM_TH
    cfg.drain_depth = config.DRAIN_DEPTH
    cfg.drain_max_children = config.DRAIN_MAX_CHILDREN
    cfg.profiling_enabled = False
    miner = TemplateMiner(persistence_handler=None, config=cfg)
    out = df.sort_values("ts" if "ts" in df.columns else "original_ts", kind="stable").reset_index(drop=True).copy()
    ids = [miner.add_log_message(str(c))["cluster_id"] for c in out["content"]]
    out["drain_id"] = ids
    templates = {c.cluster_id: c.get_template() for c in miner.drain.clusters}
    out["drain_template"] = out["drain_id"].map(templates)
    majority = out.groupby("drain_id")["event_id"].agg(lambda s: s.value_counts().index[0])
    agreement = float((out["event_id"] == out["drain_id"].map(majority)).mean()) if len(out) else 1.0
    return out, agreement


# --------------------------------------------------------------------------- windows
def window_minutes(df: pd.DataFrame) -> float:
    """``max(5 minutes, span / 150)`` in minutes."""
    span = (df["ts"].max() - df["ts"].min()).total_seconds() / 60 if len(df) else 0.0
    return max(config.WINDOW_MIN_MINUTES, span / config.WINDOW_DIVISOR)


def build_windows(df: pd.DataFrame, service: str, wmin: float | None = None, seen: set[int] | None = None,
                  offset: int = 0) -> pd.DataFrame:
    """Per-window statistics (empty windows included, so rates stay honest).

    ``wmin`` fixes the window length (default ``max(5 min, span / 150)``); ``seen`` carries the
    templates seen in earlier segments; ``offset`` shifts the window index."""
    wmin = wmin or window_minutes(df)
    t0 = df["ts"].min()
    idx = ((df["ts"] - t0).dt.total_seconds() // (wmin * 60)).astype(int)
    n_win = int(idx.max()) + 1 if len(df) else 0
    df = df.assign(win=idx)
    seen = set() if seen is None else seen
    rows = []
    for w in range(n_win):
        g = df[df["win"] == w]
        n = len(g)
        n_err = int(g["is_error"].sum())
        counts = Counter(int(x) for x in g["drain_id"])
        err_counts = Counter(int(x) for x in g.loc[g["is_error"], "drain_id"])
        new = sorted(set(counts) - seen)
        new_err = sorted(set(err_counts) - seen)
        seen |= set(counts)
        row: dict[str, Any] = {
            "service": service, "win": w + offset,
            "start": t0 + timedelta(minutes=w * wmin), "end": t0 + timedelta(minutes=(w + 1) * wmin),
            "n_lines": n, "rate_per_min": n / wmin, "n_err": n_err,
            "error_ratio": n_err / n if n else 0.0,
            "alert_share": float((g["label"] != "-").mean()) if n and g["label"].notna().any() else np.nan,
            "n_alert": int((g["label"] != "-").sum()) if g["label"].notna().any() else 0,
            "templates": json.dumps(dict(counts)), "new_templates": json.dumps(new),
            "new_error_templates": json.dumps(new_err),
            "p50_latency": np.nan, "p95_latency": np.nan, "share_5xx": np.nan, "n_latency": 0,
        }
        if service in config.LATENCY_SERVICES:
            lat = g["latency_s"].dropna()
            row["n_latency"] = int(len(lat))
            if len(lat):
                row["p50_latency"] = float(lat.quantile(0.5))
                row["p95_latency"] = float(lat.quantile(0.95))
            st = g["http_status"].dropna()
            if len(st):
                row["share_5xx"] = float((st >= 500).mean())
        rows.append(row)
    return pd.DataFrame(rows)


def segments(df: pd.DataFrame, gap: timedelta = timedelta(minutes=30)) -> list[pd.DataFrame]:
    """Contiguous runs of lines (a gap longer than ``gap`` starts a new segment)."""
    if df.empty:
        return []
    cut = (df["ts"].diff() > gap).cumsum()
    return [g for _, g in df.groupby(cut, sort=True)]


def build_segment_windows(df: pd.DataFrame, service: str, wmin: float) -> pd.DataFrame:
    """Windows of every segment (indices keep a gap between segments, so spans never join them)."""
    seen: set[int] = set()
    out, offset = [], 0
    for seg in segments(df):
        w = build_windows(seg, service, wmin, seen, offset)
        out.append(w)
        offset = int(w["win"].max()) + 2
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


# --------------------------------------------------------------------------- baseline
def compute_baseline(windows: pd.DataFrame, lines: pd.DataFrame, flagged: pd.Series | None = None,
                     wmin: float | None = None) -> dict[str, Any]:
    """Baseline stats from non-empty, unflagged windows (bands are [p10, p90])."""
    ok = windows["n_lines"] > 0
    if flagged is not None:
        ok &= ~flagged
    w = windows[ok]
    if w.empty:  # every window flagged: fall back to all non-empty windows, say so
        w = windows[windows["n_lines"] > 0]
    templates = {int(k): v for k, v in lines.groupby("drain_id")["drain_template"].first().items()}
    err_ids = set(int(x) for x in lines.loc[lines["is_error"], "drain_id"])
    tpl_counts: Counter[int] = Counter()
    presence: Counter[int] = Counter()
    for t in w["templates"]:
        d = {int(k): v for k, v in json.loads(t).items()}
        tpl_counts.update(d)
        presence.update(d.keys())
    n = max(1, len(w))
    known_noise = [templates[k] for k, c in presence.items() if k in err_ids and c / n > config.KNOWN_NOISE_SHARE]
    base: dict[str, Any] = {
        "windows_used": int(len(w)),
        "window_minutes": float(wmin or window_minutes(lines)),
        "rate_band": [float(w["rate_per_min"].quantile(0.1)), float(w["rate_per_min"].quantile(0.9))],
        "error_ratio_band": [float(w["error_ratio"].quantile(0.1)), float(w["error_ratio"].quantile(0.9))],
        "error_ratio_median": float(w["error_ratio"].median()),
        "top_templates": [templates[k] for k, _ in tpl_counts.most_common(10) if k not in err_ids],
        "known_noise": sorted(known_noise),
        "p95_latency_band": None,
    }
    lat = w.loc[w["n_latency"] >= config.MIN_LATENCY_SAMPLES, "p95_latency"].dropna()
    if len(lat):
        base["p95_latency_band"] = [float(lat.quantile(0.1)), float(lat.quantile(0.9))]
    return base


def flag_windows(windows: pd.DataFrame, baseline: dict[str, Any]) -> pd.DataFrame:
    """Apply the significance rules; adds ``significant`` and ``reasons`` columns."""
    out = windows.copy()
    nonempty_rank = (out["n_lines"] > 0).cumsum()
    med = baseline["error_ratio_median"]
    lat_band = baseline.get("p95_latency_band")
    sig, reasons = [], []
    for i, r in out.iterrows():
        why = []
        if json.loads(r["new_error_templates"]) and nonempty_rank[i] > config.NOVELTY_WARMUP_WINDOWS:
            why.append("new_error_template")
        if r["error_ratio"] > config.ERROR_RATIO_MULT * med and r["n_err"] >= config.MIN_ERROR_LINES:
            why.append("error_ratio")
        if (lat_band and not math.isnan(r["p95_latency"]) and r["n_latency"] >= config.MIN_LATENCY_SAMPLES
                and r["p95_latency"] > lat_band[1] * config.LATENCY_MARGIN):
            why.append("p95_latency")
        sig.append(bool(why))
        reasons.append(",".join(why))
    out["significant"] = sig
    out["reasons"] = reasons
    return out


def merge_spans(windows: pd.DataFrame) -> list[list[int]]:
    """Adjacent significant windows -> lists of window indices."""
    spans: list[list[int]] = []
    for w in windows.loc[windows["significant"], "win"]:
        if spans and w == spans[-1][-1] + 1:
            spans[-1].append(int(w))
        else:
            spans.append([int(w)])
    return spans


# --------------------------------------------------------------------------- narratives
_NUM = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")


def _numbers(text: str) -> set[float]:
    out = set()
    for m in _NUM.findall(text):
        try:
            out.add(round(float(m), 6))
        except ValueError:
            pass
    return out


def invented_numbers(output: str, source: str) -> set[float]:
    """Numbers in ``output`` that do not appear in ``source``."""
    return _numbers(output) - _numbers(source)


def template_narrative(facts: dict[str, Any]) -> str:
    """Deterministic fallback sentence built only from the computed numbers."""
    s = (f"{facts['service']}: {facts['error_lines']} of {facts['lines']} log lines were non-INFO between "
         f"{facts['start']} and {facts['end']} UTC (peak error ratio {facts['peak_error_ratio']} vs baseline median "
         f"{facts['baseline_median']}). {facts['n_templates']} error templates, {facts['n_new']} first seen in this span.")
    if facts.get("p95_latency") is not None:
        s += f" Peak p95 latency {facts['p95_latency']} s vs baseline band up to {facts['p95_band_high']} s."
    if facts.get("alert_lines"):
        s += f" {facts['alert_lines']} lines carry an alert label."
    return s


def narrative_prompt(facts: dict[str, Any], exemplars: list[str]) -> str:
    """User message for the narrator: the numbers plus at most 3 redacted exemplar lines."""
    lines = [f"{k}: {v}" for k, v in facts.items() if v is not None]
    lines.append("Example log lines:")
    lines += [f"- {e}" for e in exemplars[:3]]
    lines.append(f"Write at most {config.NARRATIVE_MAX_WORDS} words.")
    return "\n".join(lines)


def narrate(facts: dict[str, Any], exemplars: list[str], llm: LLMFn | None, store: SqliteStore | None) -> tuple[str, str]:
    """Narrative for an anomaly and its origin (``llm`` | ``template`` | ``fallback``).

    Cached in SQLite by a hash of the exact prompt, so re-runs cost nothing.
    """
    user = narrative_prompt(facts, exemplars)
    if llm is None:
        return template_narrative(facts), "template"
    key = "narr:" + hashlib.sha256((config.NARRATIVE_SYSTEM + "\n" + user).encode()).hexdigest()
    if store is not None and (hit := store.cache_get(key)) is not None:
        d = json.loads(hit)
        return d["text"], d["origin"]
    text = " ".join(llm(config.NARRATIVE_SYSTEM, user).split()[:config.NARRATIVE_MAX_WORDS]).strip()
    if not text or invented_numbers(text, user):
        text, origin = template_narrative(facts), "fallback"
    else:
        origin = "llm"
    if store is not None:
        store.cache_put(key, json.dumps({"text": text, "origin": origin}))
    return text, origin


# --------------------------------------------------------------------------- records
@dataclass
class ServiceLogResult:
    """Everything derived from one service's logs."""

    service: str
    lines: pd.DataFrame
    windows: pd.DataFrame
    baseline: dict[str, Any]
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    signatures: list[dict[str, Any]] = field(default_factory=list)
    drain_agreement: float = 1.0


def iso(ts: Any) -> str:
    """ISO-8601 with microsecond precision (Python 3.10 cannot parse pandas' nanoseconds)."""
    return pd.Timestamp(ts).floor("us").to_pydatetime().isoformat()


def _fmt(ts: pd.Timestamp) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M")


def analyse_service(service: str, lines: pd.DataFrame, llm: LLMFn | None = None,
                    store: SqliteStore | None = None, segmented: bool = False, prefix: str = "L",
                    wmin: float = 10.0) -> ServiceLogResult:
    """Run steps 1-5 for one service's log lines (``segmented``: the synthetic stream)."""
    from ff.ingest.deploy_summarizer import significance

    mined, agreement = mine_templates(lines)
    if segmented:
        windows = build_segment_windows(mined, service, wmin)
    else:
        windows, wmin = build_windows(mined, service), window_minutes(mined)
    base0 = compute_baseline(windows, mined, wmin=wmin)
    pass1 = flag_windows(windows, base0)
    baseline = compute_baseline(windows, mined, pass1["significant"], wmin=wmin)
    windows = flag_windows(windows, baseline)
    res = ServiceLogResult(service, mined, windows, baseline, drain_agreement=agreement)
    med = baseline["error_ratio_median"]
    for k, span in enumerate(merge_spans(windows)):
        ws = windows[windows["win"].isin(span)]
        start, end = ws["start"].min(), ws["end"].max()
        sl = mined[(mined["ts"] >= start) & (mined["ts"] < end)]
        alert = sl["label"].notna() & (sl["label"] != "-")
        err = sl[sl["is_error"] | alert]
        aid = f"{prefix}-{service}-{k:03d}"
        sig_ids = []
        groups = sorted(err.groupby("drain_id", sort=True), key=lambda x: (-len(x[1]), x[0]))  # most frequent first
        for j, (_tid, g) in enumerate(groups):
            sid = f"{aid}-S{j:02d}"
            sig_ids.append(sid)
            ex = [redact(c) for c in g["content"].head(3)]
            tpl = redact(str(g["drain_template"].iloc[0]))
            labels = sorted(set(g["label"].dropna()) - {"-"})
            res.signatures.append({
                "id": sid, "anomaly_id": aid, "service": service, "template": tpl, "count": int(len(g)),
                "first_seen": iso(g["ts"].min()), "last_seen": iso(g["ts"].max()), "exemplars": ex,
                "levels": sorted(set(g["level"])), "labels": labels,
            })
        peak = float(ws["error_ratio"].max())
        ratio_x = peak / med if med > 0 else (float("inf") if peak > 0 else 0.0)
        novel = sorted({int(t) for v in ws["new_error_templates"] for t in json.loads(v)})
        facts: dict[str, Any] = {
            "service": service, "start": _fmt(start), "end": _fmt(end),
            "lines": int(len(sl)), "error_lines": int(sl["is_error"].sum()),
            "peak_error_ratio": round(peak, 2), "baseline_median": round(med, 2),
            "n_templates": int(err["drain_id"].nunique()), "n_new": len(novel),
            "p95_latency": None, "p95_band_high": None, "alert_lines": int(alert.sum()) or None,
        }
        lat_band = baseline.get("p95_latency_band")
        if service in config.LATENCY_SERVICES and ws["p95_latency"].notna().any() and lat_band:
            facts["p95_latency"] = round(float(ws["p95_latency"].max()), 3)
            facts["p95_band_high"] = round(lat_band[1], 3)
        exemplars = [redact(c) for c in err["content"].head(3)]
        narrative, origin = narrate(facts, exemplars, llm, store)
        cap = config.SEVERITY_RATIO_CAP
        severity = (min(ratio_x, cap) if math.isfinite(ratio_x) else cap) + math.log1p(facts["error_lines"]) \
            + 2 * len(novel) + (5.0 if "p95_latency" in ",".join(ws["reasons"]) else 0.0)
        payload = {
            "peak_error_ratio": peak, "baseline_median_error_ratio": med,
            "ratio_vs_baseline": ratio_x if math.isfinite(ratio_x) else None,
            "error_lines": facts["error_lines"], "lines": facts["lines"], "novel_templates": len(novel),
            "reasons": sorted({r for v in ws["reasons"] for r in v.split(",") if r}),
            "windows": span, "error_signatures": sig_ids, "severity": severity,
            "templates": [s["template"] for s in res.signatures if s["anomaly_id"] == aid],
            "p95_latency": facts["p95_latency"], "p95_band_high": facts["p95_band_high"],
            "alert_lines": int(alert.sum()), "narrative_origin": origin,
        }
        ev = {"id": aid, "event_type": "ANOMALY", "service": service, "environment": "prod",
              "occurred_at": iso(start), "end_at": iso(end), "source": "logs",
              "log_source": "synthetic" if segmented else "loghub",
              "narrative": f"[ANOMALY] {service} {_fmt(start)}-{end.strftime('%H:%M')}: {narrative}",
              "payload": payload}
        ev["significance"] = significance(ev)
        res.anomalies.append(ev)
    return res


def signature_event(sig: dict[str, Any], anomaly: dict[str, Any]) -> dict[str, Any]:
    """ERROR_SIGNATURE record as an event (for the vector store and the ranker)."""
    from ff.ingest.deploy_summarizer import significance

    ex = sig["exemplars"][0] if sig["exemplars"] else ""
    ev = {
        "id": sig["id"], "event_type": "ERROR_SIGNATURE", "service": sig["service"], "environment": "prod",
        "occurred_at": sig["first_seen"], "end_at": sig["last_seen"], "source": "logs",
        "log_source": anomaly.get("log_source", "loghub"),
        "related_event_id": sig["anomaly_id"],
        "narrative": (f"[ERROR_SIGNATURE] {sig['service']} `{sig['template']}` x{sig['count']} "
                      f"{sig['first_seen'][:16]}..{sig['last_seen'][11:16]}; e.g. {ex}"),
        "payload": {k: sig[k] for k in ("template", "count", "exemplars", "levels", "labels")}
                   | {"anomaly_ratio_vs_baseline": anomaly["payload"].get("ratio_vs_baseline")},
    }
    ev["significance"] = significance(ev)
    return ev


# --------------------------------------------------------------------------- runner
def run(paths: Paths | None = None, store: SqliteStore | None = None, llm: LLMFn | None = None,
        logs: dict[str, pd.DataFrame] | None = None, synthetic: pd.DataFrame | None = None
        ) -> dict[str, ServiceLogResult]:
    """Analyse every component (real LogHub stream and synthetic stream separately), upsert one
    baseline per component (the synthetic stream's, which covers the whole history; the real
    stream's for components without synthetic lines), and write ``data/derived``.

    Results are keyed ``<service>`` (real stream) and ``<service>#synthetic``."""
    from ff.ingest import loghub

    paths = (paths or get_paths()).ensure()
    logs = logs if logs is not None else loghub.load_service_logs(paths)
    results: dict[str, ServiceLogResult] = {}
    for svc in config.LOGHUB_SERVICES:
        if svc in logs:
            results[svc] = analyse_service(svc, logs[svc], llm=llm, store=store)
    if synthetic is not None and len(synthetic):
        for svc, g in synthetic.groupby("service", sort=False):
            results[f"{svc}#synthetic"] = analyse_service(str(svc), g.reset_index(drop=True), llm=llm, store=store,
                                                          segmented=True, prefix="LS")
    baselines: dict[str, dict[str, Any]] = {}
    for key, r in results.items():
        if key.endswith("#synthetic") or r.service not in baselines:
            baselines[r.service] = r.baseline
    if store is not None:
        for svc, b in baselines.items():
            store.upsert_baseline(svc, b)
    for svc in config.SERVICES:
        parts = []
        if svc in results:
            parts.append(results[svc].lines.assign(source="loghub"))
        if f"{svc}#synthetic" in results:
            parts.append(results[f"{svc}#synthetic"].lines.assign(source="synthetic"))
        f = paths.derived / f"lines_{svc}.parquet"
        if parts:
            pd.concat(parts, ignore_index=True).sort_values("ts", kind="stable").reset_index(drop=True).to_parquet(
                f, index=False)
        elif f.exists():
            f.unlink()
    pd.concat([r.windows.assign(source="synthetic" if k.endswith("#synthetic") else "loghub")
               for k, r in results.items() if len(r.windows)], ignore_index=True).to_parquet(
        paths.derived / "windows.parquet", index=False)
    events = []
    for r in results.values():
        by_id = {a["id"]: a for a in r.anomalies}
        events += r.anomalies + [signature_event(s, by_id[s["anomaly_id"]]) for s in r.signatures]
    out = {
        "events": events,
        "signatures": [s for r in results.values() for s in r.signatures],
        "baselines": baselines,
        "baselines_loghub": {r.service: r.baseline for k, r in results.items() if not k.endswith("#synthetic")},
        "drain_agreement": {k: r.drain_agreement for k, r in results.items()},
    }
    (paths.derived / "log_events.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    return results


def load_log_events(paths: Paths | None = None) -> dict[str, Any]:
    """Read ``data/derived/log_events.json``."""
    paths = paths or get_paths()
    return json.loads((paths.derived / "log_events.json").read_text(encoding="utf-8"))


def summary(results: dict[str, ServiceLogResult]) -> str:
    """One line per service and stream for the console."""
    rows = []
    for s, r in results.items():
        w = r.windows
        rows.append(f"{s:28s} windows={len(w):5d} significant={int(w['significant'].sum()):4d} "
                    f"anomalies={len(r.anomalies):2d} signatures={len(r.signatures):3d} "
                    f"err_median={r.baseline['error_ratio_median']:.2f} drain_agreement={r.drain_agreement:.2f}")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--narrator", choices=["template", "llm", "tiny"], default="template")
    args = ap.parse_args(argv)
    paths = get_paths().ensure()
    store = SqliteStore(paths.sqlite)
    llm = None
    if args.narrator != "template":
        from ff.llm.model import get_model, make_chat_fn

        llm = make_chat_fn(*get_model(tiny=args.narrator == "tiny"), max_new_tokens=160)
    print(summary(run(paths, store, llm)))


if __name__ == "__main__":
    main()
