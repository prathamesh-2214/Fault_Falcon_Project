"""Charts and takeaways for Experiments A / B and replay.

Every takeaway sentence is computed from the numbers in the CSVs (nothing hard-coded), so
the Experiment tab and the poster say exactly what the data says, including results that
contradict the hypothesis.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go

from ff.config import Paths, get_paths

MODE_ORDER = ["B1", "B2", "B3", "B4", "B5"]
MODE_NAMES = {"B1": "B1 full", "B2": "B2 trim", "B3": "B3 streaming", "B4": "B4 faultfalcon", "B5": "B5 ff_no_anchor"}


def _read(p: Path) -> pd.DataFrame | None:
    return pd.read_csv(p) if p.exists() and p.stat().st_size > 0 else None


def load_exp_a(paths: Paths) -> pd.DataFrame | None:
    return _read(paths.results / "exp_a.csv")


def load_exp_b(paths: Paths) -> pd.DataFrame | None:
    s = _read(paths.results / "exp_b.csv")
    if s is None:
        return None
    t = _read(paths.results / "exp_b_turns.csv")
    s.attrs["turns"] = t
    return s


def load_replay(paths: Paths) -> pd.DataFrame | None:
    return _read(paths.results / "replay.csv")


def load_micro(paths: Paths) -> pd.DataFrame | None:
    return _read(paths.results / "micro_check.csv")


def takeaways_micro(df: pd.DataFrame) -> list[str]:
    if not len(df):
        return []
    out = [f"Given the true root-cause change, the suspect-line ranking puts the breaking line first in "
           f"{df['top1'].mean():.0%} of {len(df)} incidents and in the top 3 in {df['top3'].mean():.0%}."]
    miss = df[~df["top1"].astype(bool)]
    if len(miss):
        out.append("Not first: " + ", ".join(f"{r.incident} ({r.scenario}, rank {r['rank']})" for _, r in miss.iterrows()) + ".")
    return out


def figs_micro(df: pd.DataFrame) -> list[go.Figure]:
    g = df.groupby("scenario")["top1"].mean().sort_values()
    f = go.Figure(go.Bar(x=g.values, y=g.index, orientation="h"))
    f.update_layout(title="Breaking line ranked first, per scenario", xaxis_range=[0, 1.05], height=360)
    return [f]


def _fmt(x: float, nd: int = 2) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{nd}f}"


# --------------------------------------------------------------------------- experiment A
def takeaways_a(df: pd.DataFrame) -> list[str]:
    out: list[str] = []
    train_len = int(df["train_len"].iloc[0]) if "train_len" in df else 2048
    dense = df[df["config"] == "dense"].dropna(subset=["chunk_ppl"])
    if len(dense):
        early = dense[dense["tokens_seen"] <= train_len]["chunk_ppl"]
        base = float(early.median()) if len(early) else float(dense["chunk_ppl"].iloc[0])
        rise = dense[(dense["tokens_seen"] > train_len) & (dense["chunk_ppl"] > 2 * base)]
        if len(rise):
            out.append(f"Dense perplexity first exceeds 2x its early level ({_fmt(base)}) at {int(rise['tokens_seen'].iloc[0])} "
                       f"tokens (training length {train_len}).")
        else:
            out.append(f"Dense perplexity never exceeded 2x its early level ({_fmt(base)}) within "
                       f"{int(dense['tokens_seen'].max())} tokens (training length {train_len}).")
    if (df["note"].fillna("") == "OOM").any():
        r = df[df["note"] == "OOM"].iloc[0]
        out.append(f"{r['config']} ran out of memory at {int(r['tokens_seen'])} tokens.")
    win, stream = df[df["config"] == "window"], df[df["config"] == "streaming"]
    if len(win) and len(stream):
        first_evict = int(win["cache_len"].max())
        w, s = win[win["tokens_seen"] > first_evict]["chunk_ppl"].mean(), stream[stream["tokens_seen"] > first_evict]["chunk_ppl"].mean()
        verdict = ("streaming (4 sinks) is better, as the StreamingLLM paper predicts" if s < w
                   else "streaming is NOT better than window here, which contradicts the StreamingLLM paper")
        out.append(f"After the first eviction ({first_evict} tokens): window perplexity {_fmt(w)} vs streaming "
                   f"{_fmt(s)}; {verdict}.")
    rec = df[df["config"] == "recompute"]
    if len(rec) and len(stream):
        rs, ss = rec["ms_per_token"].mean(), stream["ms_per_token"].mean()
        out.append(f"Recompute costs {_fmt(rs, 1)} ms/token vs streaming {_fmt(ss, 1)} ms/token "
                   f"({_fmt(rs / ss, 1)}x); recompute perplexity {_fmt(rec['chunk_ppl'].mean())} "
                   f"(over its first {int(rec['tokens_seen'].max())} tokens).")
    if "kv_bytes_per_token" in df:
        out.append(f"KV cache: {int(df['kv_bytes_per_token'].iloc[0])} bytes per token in fp16.")
    return out


def figs_a(df: pd.DataFrame) -> list[go.Figure]:
    train_len = int(df["train_len"].iloc[0]) if "train_len" in df else 2048
    f1, f2 = go.Figure(), go.Figure()
    for cfg, g in df.groupby("config", sort=False):
        f1.add_trace(go.Scatter(x=g["tokens_seen"], y=g["chunk_ppl"], name=cfg, mode="lines+markers"))
        f2.add_trace(go.Scatter(x=g["tokens_seen"], y=g["cache_len"], name=cfg, mode="lines"))
    f1.add_vline(x=train_len, line_dash="dash", annotation_text=f"training length {train_len}")
    f1.update_layout(title="Perplexity per 500-token chunk", yaxis_type="log", xaxis_title="tokens seen", height=360)
    f2.update_layout(title="Cache length", xaxis_title="tokens seen", height=300)
    speed = df.groupby("config", sort=False)["ms_per_token"].mean()
    f3 = go.Figure(go.Bar(x=speed.index, y=speed.values))
    f3.update_layout(title="ms per token", height=300)
    return [f1, f2, f3]


def save_exp_a_pngs(df: pd.DataFrame, out: Path, dpi: int = 150) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    train_len = int(df["train_len"].iloc[0]) if "train_len" in df else 2048
    fig, ax = plt.subplots(figsize=(7, 4))
    for cfg, g in df.groupby("config", sort=False):
        ax.plot(g["tokens_seen"], g["chunk_ppl"], marker=".", label=cfg)
    ax.axvline(train_len, ls="--", c="gray")
    ax.set_yscale("log")
    ax.set(xlabel="tokens seen", ylabel="perplexity (500-token chunks)", title="Experiment A: perplexity")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "exp_a_ppl.png", dpi=dpi)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(7, 3))
    for cfg, g in df.groupby("config", sort=False):
        ax.plot(g["tokens_seen"], g["cache_len"], label=cfg)
    ax.set(xlabel="tokens seen", ylabel="cache length", title="Experiment A: cache length")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "exp_a_cache.png", dpi=dpi)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(5, 3))
    speed = df.groupby("config", sort=False)["ms_per_token"].mean()
    ax.bar(speed.index, speed.values)
    ax.set(ylabel="ms / token", title="Experiment A: speed")
    fig.tight_layout()
    fig.savefig(out / "exp_a_speed.png", dpi=dpi)
    plt.close(fig)


# --------------------------------------------------------------------------- experiment B
def _by_cfg(s: pd.DataFrame, col: str) -> pd.Series:
    return s.groupby("config")[col].mean().reindex([c for c in MODE_ORDER if c in set(s["config"])])


def takeaways_b(s: pd.DataFrame) -> list[str]:
    out: list[str] = []
    rec, anc = _by_cfg(s, "recall_acc"), _by_cfg(s, "anchor_acc")
    coh, top = _by_cfg(s, "coherent_rate"), _by_cfg(s, "root_cause_top3")
    for c in rec.index:
        out.append(f"{MODE_NAMES[c]}: recall probes {_fmt(rec[c])}, anchor probes {_fmt(anc[c])}, "
                   f"coherent turns {_fmt(coh[c])}, root cause in top 3 {_fmt(float(top[c]))}.")
    if "B4" in anc and "B5" in anc:
        d = anc["B4"] - anc["B5"]
        if math.isnan(d):
            out.append("Value of the anchor block (B4 - B5 anchor-probe accuracy): n/a (no anchor probes scored).")
        else:
            out.append(f"Value of the anchor block (B4 - B5 anchor-probe accuracy): {d:+.2f}"
                       + (" (no measurable difference)." if abs(d) < 1e-9 else "."))
    if "B4" in anc and "B3" in anc:
        out.append(f"B4 vs plain StreamingLLM (B3) on anchor probes: {_fmt(anc['B4'])} vs {_fmt(anc['B3'])}.")
    if "ledger_click_rebuild_tokens" in s and s["ledger_click_rebuild_tokens"].notna().any():
        b4 = s[s["config"] == "B4"]
        out.append(f"A ledger click re-encoded {_fmt(b4['ledger_click_rebuild_tokens'].mean(), 0)} tokens on average, vs "
                   f"{_fmt(b4['full_start_region_tokens'].mean(), 0)} for a full start-region rebuild.")
    return out


def figs_b(s: pd.DataFrame) -> list[go.Figure]:
    t = s.attrs.get("turns")
    figs = []
    f = go.Figure()
    for col, name in (("recall_acc", "recall probes"), ("anchor_acc", "anchor probes"), ("root_cause_top3", "root cause top-3")):
        g = s.groupby("config")[col].agg(["mean", "min", "max"]).reindex([c for c in MODE_ORDER if c in set(s["config"])])
        g = g.astype(float)
        f.add_trace(go.Bar(x=[MODE_NAMES[c] for c in g.index], y=g["mean"], name=name,
                           error_y=dict(type="data", symmetric=False, array=g["max"] - g["mean"],
                                        arrayminus=g["mean"] - g["min"])))
    f.update_layout(title="Probe accuracy per config (mean, min-max across sessions)", barmode="group", height=380,
                    yaxis_range=[0, 1.05])
    figs.append(f)
    if t is not None and len(t):
        for col, title in (("coherent", "Coherence rate per turn"), ("decode_ms_per_token", "Decode ms/token per turn"),
                           ("cache_len", "Cache length per turn")):
            f = go.Figure()
            for cfg, g in t.groupby("config"):
                y = g.groupby("turn")[col].mean()
                f.add_trace(go.Scatter(x=y.index, y=y.values, name=MODE_NAMES.get(cfg, cfg), mode="lines"))
            f.update_layout(title=title, xaxis_title="turn", height=300)
            figs.append(f)
    return figs


def save_exp_b_pngs(s: pd.DataFrame, t: pd.DataFrame, out: Path, dpi: int = 150) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    cfgs = [c for c in MODE_ORDER if c in set(s["config"])]
    fig, ax = plt.subplots(figsize=(8, 4))
    width = 0.25
    for k, col in enumerate(("recall_acc", "anchor_acc", "root_cause_top3")):
        g = s.groupby("config")[col].agg(["mean", "min", "max"]).reindex(cfgs).astype(float)
        xs = [i + (k - 1) * width for i in range(len(cfgs))]
        ax.bar(xs, g["mean"], width, yerr=[g["mean"] - g["min"], g["max"] - g["mean"]], capsize=3, label=col)
    ax.set_xticks(range(len(cfgs)), [MODE_NAMES[c] for c in cfgs], rotation=15)
    ax.set(ylim=(0, 1.05), title="Experiment B: probe accuracy")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "exp_b_probes.png", dpi=dpi)
    plt.close(fig)
    for col, name in (("coherent", "coherence"), ("decode_ms_per_token", "speed"), ("cache_len", "cache")):
        fig, ax = plt.subplots(figsize=(7, 3))
        for cfg, g in t.groupby("config"):
            y = g.groupby("turn")[col].mean()
            ax.plot(y.index, y.values, label=MODE_NAMES.get(cfg, cfg))
        ax.set(xlabel="turn", ylabel=col, title=f"Experiment B: {col} per turn")
        ax.legend(fontsize=7)
        fig.tight_layout()
        fig.savefig(out / f"exp_b_{name}.png", dpi=dpi)
        plt.close(fig)
    sub = s[s["config"].isin(["B3", "B4", "B5"])]
    if len(sub):
        fig, ax = plt.subplots(figsize=(5, 3))
        g = sub.groupby("config")["anchor_acc"].mean()
        ax.bar([MODE_NAMES[c] for c in g.index], g.values)
        ax.set(ylim=(0, 1.05), title="Anchor-probe accuracy")
        fig.tight_layout()
        fig.savefig(out / "exp_b_anchor.png", dpi=dpi)
        plt.close(fig)
    b4 = s[(s["config"] == "B4") & s["ledger_click_rebuild_tokens"].notna()]
    if len(b4):
        fig, ax = plt.subplots(figsize=(5, 3))
        ax.bar(["ledger click (partial)", "full start region"],
               [b4["ledger_click_rebuild_tokens"].mean(), b4["full_start_region_tokens"].mean()])
        ax.set(ylabel="tokens re-encoded", title="Rebuild cost per ledger click (B4)")
        fig.tight_layout()
        fig.savefig(out / "exp_b_rebuild.png", dpi=dpi)
        plt.close(fig)


# --------------------------------------------------------------------------- replay
def takeaways_replay(df: pd.DataFrame) -> list[str]:
    out: list[str] = []
    base = df[df["ablation"] == "faultfalcon"]
    if len(base):
        b = base.iloc[0]
        out.append(f"FaultFalcon: top-3 root-cause accuracy {_fmt(b['top3_acc'])} over {int(b['incidents'])} held-out "
                   f"sessions, file hit rate {_fmt(b['file_hit_rate'])}, citation groundedness {_fmt(b['groundedness'])}.")
        if "culprit_hit" in b:
            out.append(f"Micro: the model named the breaking line in {_fmt(b['culprit_hit'])} of sessions (its file in "
                       f"{_fmt(b.get('culprit_file_hit', float('nan')))}); the "
                       f"suspect-line ranking put it first in {_fmt(b['suspect_top1'])}; blast radius coverage "
                       f"{_fmt(b['blast_hit'])}; remediation named in {_fmt(b['remediation_hit'])}; complete RCA "
                       f"reports {_fmt(b['rca_complete'])}.")
        for _, r in df[df["ablation"] != "faultfalcon"].iterrows():
            d = r["top3_acc"] - b["top3_acc"]
            out.append(f"{r['ablation']}: top-3 {_fmt(r['top3_acc'])} ({d:+.2f} vs FaultFalcon), groundedness "
                       f"{_fmt(r['groundedness'])}.")
    if "detector_precision" in df and len(df):
        out.append(f"Log detector on BGL labels: precision {_fmt(df['detector_precision'].iloc[0])}, "
                   f"recall {_fmt(df['detector_recall'].iloc[0])}.")
    return out


def figs_replay(df: pd.DataFrame) -> list[go.Figure]:
    f = go.Figure()
    for col in ("top3_acc", "file_hit_rate", "groundedness", "culprit_hit", "suspect_top1", "remediation_hit"):
        if col not in df:
            continue
        f.add_trace(go.Bar(x=df["ablation"], y=df[col], name=col))
    f.update_layout(title="Replay: held-out incidents per ablation", barmode="group", height=380, yaxis_range=[0, 1.05])
    return [f]


SECTIONS: list[tuple[str, Any, Any, Any]] = [
    ("Micro check: does the suspect-line ranking find the breaking line?", load_micro, figs_micro, takeaways_micro),
    ("Experiment A: cache mechanics", load_exp_a, figs_a, takeaways_a),
    ("Experiment B: the debugging conversation", load_exp_b, figs_b, takeaways_b),
    ("Replay: held-out incidents and ablations", load_replay, figs_replay, takeaways_replay),
]


# --------------------------------------------------------------------------- poster
# where each record comes from (for the poster / report, not shown in the app)
PROVENANCE = pd.DataFrame([
    {"record": "ANOMALY events", "source": "Real LogHub logs (last 72 h) + synthetic telemetry",
     "how": "Drain templates + window statistics + significance rules (same detector for both)"},
    {"record": "ERROR_SIGNATURE", "source": "Real LogHub logs + synthetic telemetry",
     "how": "non-INFO templates (+ BGL alert labels)"},
    {"record": "Baseline (one per component)", "source": "Logs", "how": "statistics of unflagged windows"},
    {"record": "Latency", "source": "Real logs (OpenStack only)", "how": "status / time fields; never invented elsewhere"},
    {"record": "DEPLOY / PATCH / ROLLBACK / CONFIG / INFRA", "source": "Simulated (seeded)", "how": "pipeline simulator"},
    {"record": "TEST_RUN / SECURITY_SCAN", "source": "Simulated (seeded)", "how": "pipeline simulator"},
    {"record": "change_detail: diff hunks (file, line, before/after)", "source": "Simulated",
     "how": "service code, config/<svc>.yaml and infra/<svc>.tf (AWS EBS, launch template, ASG, ALB, SG, RDS)"},
    {"record": "Incident ground truth", "source": "Simulated, anchored",
     "how": "macro cause + breaking line + contributing changes + decoys placed before each real anomaly"},
    {"record": "Topology / runbooks", "source": "Generated", "how": "match the simulator's latest config"},
])


def build_poster(paths: Paths | None = None, dpi: int = 300) -> Path:
    """results/poster/: charts at 300 dpi, provenance table, one-page results table, graph mermaid."""
    paths = (paths or get_paths()).ensure()
    out = paths.results / "poster"
    out.mkdir(parents=True, exist_ok=True)
    a, b, r = load_exp_a(paths), load_exp_b(paths), load_replay(paths)
    if a is not None:
        save_exp_a_pngs(a, out, dpi)
    if b is not None and b.attrs.get("turns") is not None:
        save_exp_b_pngs(b, b.attrs["turns"], out, dpi)
    (out / "provenance.md").write_text(PROVENANCE.to_markdown(index=False), encoding="utf-8")
    lines = ["# FaultFalcon results", "", "Logs and anomalies are real; deployments are simulated so the answer is known.", ""]
    for name, df, fn in (("Experiment A", a, takeaways_a), ("Experiment B", b, takeaways_b), ("Replay", r, takeaways_replay)):
        lines.append(f"## {name}")
        lines += [f"- {x}" for x in fn(df)] if df is not None else ["- not run yet"]
        lines.append("")
    if r is not None:
        lines += ["## Replay table", "", r.to_markdown(index=False), ""]
    (out / "results.md").write_text("\n".join(lines), encoding="utf-8")
    from ff.engine.graph import GraphDeps, build_graph, mermaid

    (out / "langgraph_flow.mmd").write_text(mermaid(build_graph(GraphDeps(ranker=None, session_factory=None))),
                                            encoding="utf-8")
    import shutil

    svg = Path(__file__).resolve().parents[2] / "app" / "assets" / "architecture.svg"
    if svg.exists():
        shutil.copy(svg, out / "architecture.svg")
    return out


if __name__ == "__main__":
    print(build_poster())
