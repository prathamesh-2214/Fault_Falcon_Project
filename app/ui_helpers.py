"""Pure helpers for the Streamlit app (figures, tables, loading). No Streamlit calls here,
so they can be unit-tested."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go

from ff import config

SEGMENT_COLORS = {
    "sinks": "#1f2937", "system": "#4b5563", "initial_query": "#7c3aed", "checkpoints": "#a855f7",
    "lens_baseline": "#2563eb", "ledger": "#0891b2", "rolling": "#16a34a", "free": "#e5e7eb",
}
SEGMENT_LABELS = {
    "sinks": "sinks", "system": "system", "initial_query": "initial query", "checkpoints": "checkpoints",
    "lens_baseline": "lens + baseline", "ledger": "ledger", "rolling": "rolling window", "free": "free",
}
TYPE_COLORS = {
    "DEPLOY": "#2563eb", "PATCH": "#0891b2", "ROLLBACK": "#dc2626", "CONFIG_CHANGE": "#d97706",
    "INFRA_CHANGE": "#7c3aed", "TEST_RUN": "#6b7280", "SECURITY_SCAN": "#9ca3af",
}


def load_incidents(paths: Any) -> list[dict[str, Any]]:
    """All incident files, sorted by id."""
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(paths.incidents).glob("INC-*.json"))]


def default_query(inc: dict[str, Any]) -> str:
    """A natural first message for an incident."""
    t = datetime.fromisoformat(inc["incident_start"]).strftime("%H:%M")
    return f"{inc['service']} error burst since {t}, what changed?"


def kv_segments(stats: dict[str, Any]) -> list[tuple[str, int]]:
    """(segment, tokens) in cache order, ending with rolling and free."""
    segs = stats.get("segments") or {}
    out = [(k, int(segs.get(k, 0))) for k in config.SEGMENT_ORDER if segs.get(k)]
    if not segs and stats.get("sink_tokens"):
        out = [("sinks", int(stats["sink_tokens"]))]
    cap = int(stats.get("capacity") or max(stats.get("total_tokens", 0), 1))
    used = sum(n for _, n in out)
    rolling = max(0, int(stats.get("total_tokens", 0)) - used)
    out.append(("rolling", rolling))
    out.append(("free", max(0, cap - used - rolling)))
    return out


def kv_bar(stats: dict[str, Any]) -> go.Figure:
    """Stacked horizontal bar: sinks | system | initial query | checkpoints | lens+baseline | ledger | rolling | free."""
    fig = go.Figure()
    for name, n in kv_segments(stats):
        fig.add_trace(go.Bar(x=[n], y=["KV cache"], orientation="h", name=SEGMENT_LABELS[name],
                             marker_color=SEGMENT_COLORS[name], hovertemplate=f"{SEGMENT_LABELS[name]}: %{{x}} tokens<extra></extra>"))
    cap = stats.get("capacity") or "∞"
    fig.update_layout(barmode="stack", height=150, margin=dict(l=10, r=10, t=30, b=10), showlegend=True,
                      legend=dict(orientation="h", y=-0.4, font=dict(size=10)),
                      title=dict(text=f"{stats.get('total_tokens', 0)} / {cap} tokens ({stats.get('mode', '')})",
                                 font=dict(size=12)))
    fig.update_yaxes(showticklabels=False)
    return fig


def latency_spark(turns: list[dict[str, Any]]) -> go.Figure:
    """Per-turn prefill ms and decode ms/token."""
    fig = go.Figure()
    xs = [t["turn"] for t in turns]
    fig.add_trace(go.Scatter(x=xs, y=[t["gen_stats"].get("decode_ms_per_token", 0) for t in turns],
                             name="decode ms/token", mode="lines+markers"))
    fig.add_trace(go.Scatter(x=xs, y=[t["gen_stats"].get("prefill_ms", 0) / 100 for t in turns],
                             name="prefill ms / 100", mode="lines+markers"))
    fig.update_layout(height=170, margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h", y=-0.3))
    return fig


def timeline_figure(events: list[dict[str, Any]], incident: dict[str, Any], services: list[str],
                    reveal: bool = False, days_before: float = 21.0) -> go.Figure:
    """Changes as markers (colour = type, size = significance); detected anomaly spans as shaded
    bands; a vertical line at the incident start."""
    start = datetime.fromisoformat(incident["incident_start"])
    lo, hi = start - timedelta(days=days_before), start + timedelta(hours=config.AFTER_INCIDENT_HOURS)
    fig = go.Figure()
    changes = [e for e in events if e["event_type"] in TYPE_COLORS and e["service"] in services
               and lo <= datetime.fromisoformat(e["occurred_at"]) <= hi]
    for etype, color in TYPE_COLORS.items():
        sub = [e for e in changes if e["event_type"] == etype]
        if not sub:
            continue
        fig.add_trace(go.Scatter(
            x=[e["occurred_at"] for e in sub], y=[e["service"] for e in sub], mode="markers", name=etype,
            marker=dict(color=color, size=[6 + 3 * e.get("significance", 2) for e in sub], opacity=0.8,
                        line=dict(width=[4 if reveal and e["id"] == incident.get("cause_event_id") else 0 for e in sub],
                                  color="#ffffff")),
            text=[f"{e['id']} {e['event_type']} {e.get('environment')} {e.get('version_from') or ''}->{e.get('version_to') or ''}"
                  for e in sub], hovertemplate="%{text}<br>%{x}<extra></extra>"))
    for a in (e for e in events if e["event_type"] == "ANOMALY" and e["service"] in services):
        a0, a1 = a["occurred_at"], a.get("end_at") or a["occurred_at"]
        if datetime.fromisoformat(a1) < lo or datetime.fromisoformat(a0) > hi:
            continue
        fig.add_shape(type="rect", x0=a0, x1=a1, y0=services.index(a["service"]) - 0.4,
                      y1=services.index(a["service"]) + 0.4, fillcolor="#fca5a5", opacity=0.45, line_width=0, layer="below")
        if a["id"] == incident.get("anomaly_id"):  # label only the incident's own span (others would pile up)
            fig.add_annotation(x=a0, y=a["service"], text="anomaly", showarrow=False, yshift=-26,
                               xanchor="right", font=dict(size=10, color="#dc2626"))
    fig.add_shape(type="line", x0=incident["incident_start"], x1=incident["incident_start"], y0=-0.5,
                  y1=len(services) - 0.5, line=dict(color="#dc2626", dash="dash"))
    fig.add_annotation(x=incident["incident_start"], y=len(services) - 0.5, text="incident start", showarrow=False,
                       yshift=10, xanchor="right", font=dict(color="#dc2626"))
    fig.update_yaxes(categoryorder="array", categoryarray=services, type="category")
    fig.update_xaxes(range=[(start - timedelta(days=3)).isoformat(), (start + timedelta(hours=8)).isoformat()],
                     rangeslider=dict(visible=True))
    fig.update_layout(height=430, margin=dict(l=10, r=10, t=30, b=10), legend=dict(orientation="h", y=1.12))
    return fig


def error_ratio_figure(windows: pd.DataFrame, service: str) -> go.Figure:
    w = windows[windows["service"] == service]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=w["start"], y=w["error_ratio"], mode="lines", name="error ratio"))
    sig = w[w["significant"]]
    fig.add_trace(go.Scatter(x=sig["start"], y=sig["error_ratio"], mode="markers", name="significant window",
                             marker=dict(color="#dc2626", size=9), text=sig["reasons"],
                             hovertemplate="%{text}<br>%{x}<extra></extra>"))
    fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="non-INFO share")
    return fig


def template_table(lines: pd.DataFrame) -> pd.DataFrame:
    """Drain templates with counts and the share of non-INFO lines."""
    g = lines.groupby("drain_template").agg(count=("content", "size"), non_info=("is_error", "mean"))
    return g.sort_values("count", ascending=False).reset_index()


def files_to_inspect(change_details: list[dict[str, Any]]) -> pd.DataFrame:
    rows = [{"event": c["event_id"], "file": c.get("file"), "lines": f"{c.get('line_start') or ''}-{c.get('line_end') or ''}",
             "symbol": c.get("symbol"), "param": (f"{c['param_key']}: {c['param_old']} -> {c['param_new']}"
                                                  if c.get("param_key") else "")}
            for c in change_details]
    return pd.DataFrame(rows)
