"""Experiment B: the debugging conversation, five cache configs, scored without an LLM judge.

Sessions: every held-out incident + 2 make_variant() seeds each; 40 scripted turns:
* stages scope/change timeline -> suspects -> runtime -> hypothesis, with engineer
  observations built from the incident file and the REAL anomaly numbers;
* filler turns (dashboards, unrelated questions) so the session passes 2,048 tokens early;
* recall probes at turns 10/20/30/40 (ruled-out decoy id, lens service, suspect parameter);
* anchor probes at turns 15/25/35 (original question, starting checkpoints, one checkpoint);
* turn 0 picks 3 checkpoints: the true cause among them in half of the sessions, only
  decoys / other changes in the other half; turn 5 clicks "Mark ruled out" on a decoy.

Configs: B1 full, B2 trim, B3 streaming, B4 faultfalcon, B5 ff_no_anchor (same prompts,
greedy decoding, max_new_tokens=128). Outputs results/exp_b.csv (session x config) and
results/exp_b_turns.csv (per turn).

    python -m ff.eval.exp_b --device cuda            # prints a runtime estimate first
    python -m ff.eval.exp_b --tiny --turns 8 --sessions 1 --yes   # CI plumbing check
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd
import torch

from ff import config
from ff.config import get_paths
from ff.engine import stages as st
from ff.engine.lens import Lens
from ff.engine.session import Session
from ff.eval import common
from ff.llm.model import get_model
from ff.retrieve.ranker import Ranker

CONFIGS: dict[str, str] = {"B1": "full", "B2": "trim", "B3": "streaming", "B4": "faultfalcon", "B5": "ff_no_anchor"}
RECALL_TURNS = (10, 20, 30, 40)
ANCHOR_TURNS = (15, 25, 35)
RULE_OUT_TURN = 5
FILLERS = (
    "Dashboard check: CPU 41%, memory 63%, disk IO 22 MB/s, GC pauses p99 180 ms, queue depth 12, thread pool "
    "usage 71%, open file handles 2,300, network retransmits 0.2%. The on-call channel has 14 unread messages "
    "about an unrelated billing report. Is anything on this dashboard relevant?",
    "Unrelated question while we wait: what is a good naming convention for Kubernetes namespaces across dev, "
    "staging and production clusters, and should team names be part of it?",
    "The status page currently says 'investigating elevated errors'. Product asked for an ETA and a one-line "
    "customer summary; legal wants to know whether any data was lost. Draft nothing yet, just note it.",
    "Another dashboard: request rate is flat at about 1,200 per minute, p50 latency 85 ms, p90 140 ms, "
    "saturation of the load balancer 35%, cache hit ratio 92%, replication lag under 1 second.",
)
RECALL_QUESTIONS = ("Which change did we rule out earlier? Answer with its id.",
                    "Which service is the lens on?",
                    "Which parameter or file changed in the suspect change?")
ANCHOR_QUESTIONS = ("What was the original question of this session?",
                    "Which checkpoints did we start from? List their ids.",
                    "Is the change in [{ck}] still a suspect? Why?")


@dataclass
class ScriptTurn:
    turn: int
    kind: str  # question | filler | probe_recall | probe_anchor | hypothesis
    text: str
    stage: str
    probe: str | None = None
    gold: Any = None
    action: dict[str, Any] | None = None


@dataclass
class Script:
    session_id: str
    incident: dict[str, Any]
    initial_query: str
    anchor_ids: list[str]
    includes_cause: bool
    ruled_out: str | None
    turns: list[ScriptTurn] = field(default_factory=list)


def _scaled(turns: int, positions: tuple[int, ...]) -> list[int]:
    return sorted({max(2, round(p * turns / 40)) for p in positions})


def stage_for(t: int, total: int) -> str:
    f = t / total
    if f <= 0.15:
        return st.CHANGE_TIMELINE
    if f <= 0.35:
        return st.SUSPECTS
    if f <= 0.75:
        return st.RUNTIME
    return st.HYPOTHESIS


def pick_anchors(inc: dict[str, Any], ranker: Ranker, lens: Lens, query: str, include_cause: bool) -> list[str]:
    """3 checkpoints: the cause + 2 others, or 3 non-cause changes."""
    cause = inc.get("cause_event_id")
    others = [d for d in inc["decoy_ids"] if d != cause]
    others += [c["id"] for c in ranker.checkpoint_candidates(lens, query) if c["id"] not in others and c["id"] != cause]
    picked = ([cause] if include_cause and cause else []) + others
    return list(dict.fromkeys(picked))[:3]


def build_script(inc: dict[str, Any], ranker: Ranker, store: Any, turns: int, include_cause: bool,
                 session_id: str) -> Script:
    """The scripted engineer for one session."""
    start = datetime.fromisoformat(inc["incident_start"])
    query = f"{inc['service']} error burst since {start:%H:%M}, what changed?"
    lens = Lens(inc["service"], start)
    anchors = pick_anchors(inc, ranker, lens, query, include_cause)
    ruled = next((d for d in inc["decoy_ids"]), None)
    nums = inc.get("anomaly_numbers", {})
    tpl = (inc.get("error_templates") or ["(no template)"])[0][:100]
    cause = store.get_event(inc["cause_event_id"]) if inc.get("cause_event_id") else None
    param_gold = None
    if cause is not None:
        cds = store.get_change_details([cause["id"]])
        keys = [c["param_key"] for c in cds if c.get("param_key")]
        files = [c["file"].split("/")[-1] for c in cds if c.get("file")]
        param_gold = (keys or files or [None])[0]
    observations = [
        f"Observation from the logs: {inc['service']} had {nums.get('error_lines')} non-INFO lines out of "
        f"{nums.get('lines')}, peak error ratio {nums.get('peak_error_ratio')} vs baseline median "
        f"{nums.get('baseline_median_error_ratio')}. Top error template: {tpl}. What changed before this?",
        f"The first errors appeared at {start:%H:%M} UTC in {inc['service']}. Which recent change fits that timing?",
        f"Detector reasons for this span: {', '.join(nums.get('reasons', [])) or 'n/a'}. Does that point to a change?",
    ]
    recall_t, anchor_t = _scaled(turns, RECALL_TURNS), _scaled(turns, ANCHOR_TURNS)
    ruled_t = max(2, round(RULE_OUT_TURN * turns / 40))
    hyp_t = turns - 1  # the final hypothesis turn wins over any probe scheduled there
    recall_t = [t for t in recall_t if t != hyp_t]
    anchor_t = [t for t in anchor_t if t != hyp_t]
    script = Script(session_id, inc, query, anchors, bool(include_cause and inc.get("cause_event_id")), ruled)
    obs_i = 0
    for t in range(1, turns + 1):
        stage = stage_for(t, turns)
        action = {"type": "rule_out", "id": ruled} if t == ruled_t and ruled else None
        if t == 1:
            item = ScriptTurn(t, "question", query, st.CHANGE_TIMELINE)
        elif t in recall_t:
            k = recall_t.index(t) % 3
            gold = [ruled, inc["service"], param_gold][k]
            item = ScriptTurn(t, "probe_recall", RECALL_QUESTIONS[k], stage, ["ruled_out", "lens", "param"][k], gold)
        elif t in anchor_t:
            k = anchor_t.index(t) % 3
            gold = [[inc["service"], "burst"], anchors, anchors[0] if anchors else None][k]
            text = ANCHOR_QUESTIONS[k].format(ck=anchors[0] if anchors else "none")
            item = ScriptTurn(t, "probe_anchor", text, stage, ["original_query", "checkpoints", "checkpoint_cited"][k],
                              gold)
        elif t == hyp_t:
            item = ScriptTurn(t, "hypothesis", "Give your final root-cause hypothesis with the change id, or say "
                                               "no change explains it.", st.HYPOTHESIS)
        elif t % 2 == 0:
            item = ScriptTurn(t, "filler", FILLERS[(t // 2) % len(FILLERS)], stage)
        else:
            item = ScriptTurn(t, "question", observations[obs_i % len(observations)], stage)
            obs_i += 1
        item.action = action
        script.turns.append(item)
    return script


def score_probe(turn: ScriptTurn, answer: str) -> bool | None:
    """Regex scoring of a probe answer; None if the probe has no gold answer."""
    if turn.gold is None:
        return None
    low = answer.lower()
    if turn.probe in ("ruled_out", "checkpoint_cited"):
        return re.search(rf"\b{re.escape(turn.gold)}\b", answer) is not None
    if turn.probe == "lens":
        return turn.gold.lower() in low
    if turn.probe == "param":
        return turn.gold.lower() in low
    if turn.probe == "original_query":
        return all(g.lower() in low for g in turn.gold)
    if turn.probe == "checkpoints":
        hits = sum(re.search(rf"\b{re.escape(g)}\b", answer) is not None for g in turn.gold)
        return hits >= min(2, len(turn.gold))
    return None


def run_session(script: Script, cfg: str, model: torch.nn.Module, tok: Any, ranker: Ranker,
                max_new_tokens: int, total_cache: int, token_scale: int = 1
                ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Play one script under one config; returns (summary row, per-turn rows)."""
    mode = CONFIGS[cfg]
    inc = script.incident
    s = Session(model, tok, ranker, mode=mode, session_id=f"{script.session_id}-{cfg}", max_new_tokens=max_new_tokens,
                total_cache=total_cache, token_scale=token_scale)
    s.start(script.initial_query, Lens(inc["service"], datetime.fromisoformat(inc["incident_start"]),
                                       stage=st.CHANGE_TIMELINE), script.anchor_ids)
    rows, seen = [], 0
    click_rebuild = None
    for t in script.turns:
        rebuild_ms = rebuild_tokens = None
        if t.action:
            tail = len(s.last_turn_ids)
            getattr(s, t.action["type"])(t.action["id"])
            if s.is_ff and s.flags.ledger and s.last_rebuild:
                # a full rebuild would re-encode the whole start region + the same tail
                click_rebuild = {**s.last_rebuild, "full_equivalent": s.cache.start_size + tail}
                rebuild_ms, rebuild_tokens = click_rebuild["ms"], click_rebuild["tokens_reencoded"]
        if s.is_ff:
            s.set_stage(t.stage)
        res = s.generate_turn(t.text, s.retrieve(t.text))
        g_ids = tok.encode(res.raw_answer, add_special_tokens=False)
        seen += res.prompt_tokens + res.gen_stats["new_tokens"]
        rows.append({
            "session": script.session_id, "incident": inc["id"], "config": cfg, "mode": mode, "turn": t.turn,
            "kind": t.kind, "probe": t.probe, "probe_correct": score_probe(t, res.answer) if t.probe else None,
            "format_ok": common.format_ok(res.answer), "coherent": common.coherent(res.answer, g_ids),
            "root_cause_top3": common.root_cause_top3(res.answer, inc.get("cause_event_id"))
            if t.kind == "hypothesis" else None,
            "cache_len": res.prompt_tokens if mode == "trim" else s.cache_tokens(), "tokens_seen": seen,
            "prefill_ms": res.gen_stats["prefill_ms"], "decode_ms_per_token": res.gen_stats["decode_ms_per_token"],
            "rebuild_ms": rebuild_ms, "rebuild_tokens": rebuild_tokens, "answer": res.answer[:400],
        })
    df = pd.DataFrame(rows)

    def mean(mask: pd.Series) -> float:
        vals = df.loc[mask, "probe_correct"].dropna()
        return float(vals.astype(float).mean()) if len(vals) else float("nan")

    hyp = df[df["kind"] == "hypothesis"]
    summary = {
        "session": script.session_id, "incident": inc["id"], "scenario": inc["scenario_type"], "config": cfg,
        "mode": mode, "includes_cause": script.includes_cause, "turns": len(df),
        "recall_acc": mean(df["kind"] == "probe_recall"), "anchor_acc": mean(df["kind"] == "probe_anchor"),
        "root_cause_top3": bool(hyp["root_cause_top3"].iloc[0]) if len(hyp) else None,
        "format_ok_rate": float(df["format_ok"].mean()), "coherent_rate": float(df["coherent"].mean()),
        "mean_decode_ms_per_token": float(df["decode_ms_per_token"].mean()),
        "max_cache_len": int(df["cache_len"].max()), "tokens_seen": int(seen),
        "ledger_click_rebuild_tokens": click_rebuild["tokens_reencoded"] if click_rebuild else None,
        "full_start_region_tokens": click_rebuild["full_equivalent"] if click_rebuild else None,
    }
    return summary, rows


def estimate_minutes(model: torch.nn.Module, tok: Any, n_sessions: int, turns: int, max_new: int) -> float:
    """Calibrate decode speed with a short generation and extrapolate."""
    from ff.llm.model import generate
    from ff.llm.streaming import FullCache

    ids = tok.encode("Calibration prompt for timing the model.", add_special_tokens=True)
    t0 = time.perf_counter()
    g = generate(model, tok, ids, None, FullCache(), max_new_tokens=16)
    per_tok = (time.perf_counter() - t0) / max(1, g.stats["new_tokens"] + 1)
    per_turn = per_tok * max_new + per_tok * 0.15 * 700  # decode + a ~700-token prefill
    return n_sessions * len(CONFIGS) * turns * per_turn / 60


def run(device: str = "cpu", tiny: bool = False, turns: int = 40, variants: int = 2, sessions: int | None = None,
        configs: tuple[str, ...] = tuple(CONFIGS), max_new_tokens: int = config.MAX_NEW_TOKENS,
        total_cache: int = config.TOTAL_CACHE, yes: bool = False, max_minutes: float = 90.0,
        embedding: Any | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    paths = get_paths().ensure()
    model, tok = get_model(tiny=tiny, device=device)
    worlds = common.heldout_worlds(paths, variants)
    if sessions:
        worlds = worlds[:sessions]
    est = estimate_minutes(model, tok, len(worlds), turns, max_new_tokens) * len(configs) / len(CONFIGS)
    print(f"{len(worlds)} sessions x {len(configs)} configs x {turns} turns x {max_new_tokens} tokens: "
          f"estimated {est:.0f} minutes on {device}")
    if est > max_minutes and not yes:
        print(f"That is longer than {max_minutes:.0f} minutes. Re-run with --yes to proceed, or reduce "
              f"--sessions / --turns / --variants.")
        sys.exit(2)
    baselines = common.load_baselines(paths)
    summaries, turn_rows = [], []
    for i, (inc, world) in enumerate(worlds):
        store, vectors = common.build_stores(world, baselines, embedding)
        ranker = Ranker(store, vectors, count=lambda s: len(tok.encode(s, add_special_tokens=False)))
        script = build_script(inc, ranker, store, turns, include_cause=i % 2 == 0, session_id=f"S{i:02d}-{inc['id']}")
        for cfg in configs:
            t0 = time.perf_counter()
            summary, rows = run_session(script, cfg, model, tok, ranker, max_new_tokens, total_cache,
                                        token_scale=4 if tiny else 1)
            summaries.append(summary)
            turn_rows += rows
            print(f"{script.session_id} {cfg} ({CONFIGS[cfg]}): recall {summary['recall_acc']:.2f} anchor "
                  f"{summary['anchor_acc']:.2f} top3 {summary['root_cause_top3']} coherent "
                  f"{summary['coherent_rate']:.2f} [{time.perf_counter() - t0:.0f}s]", flush=True)
    s_df, t_df = pd.DataFrame(summaries), pd.DataFrame(turn_rows)
    s_df["model"] = t_df["model"] = "tiny" if tiny else config.MODEL_NAME
    s_df.to_csv(paths.results / "exp_b.csv", index=False)
    t_df.to_csv(paths.results / "exp_b_turns.csv", index=False)
    from ff.eval import report

    report.save_exp_b_pngs(s_df, t_df, paths.results)
    return s_df, t_df


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    ap.add_argument("--tiny", action="store_true")
    ap.add_argument("--turns", type=int, default=40)
    ap.add_argument("--variants", type=int, default=2)
    ap.add_argument("--sessions", type=int, default=None, help="limit the number of sessions")
    ap.add_argument("--configs", default=",".join(CONFIGS))
    ap.add_argument("--max-new-tokens", type=int, default=config.MAX_NEW_TOKENS)
    ap.add_argument("--yes", action="store_true", help="run even if the estimate exceeds --max-minutes")
    ap.add_argument("--max-minutes", type=float, default=90.0)
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    s_df, t_df = run(args.device, args.tiny, args.turns, args.variants, args.sessions, tuple(args.configs.split(",")),
                     args.max_new_tokens, yes=args.yes, max_minutes=args.max_minutes)
    from ff.eval import report

    print("\n".join(f"- {x}" for x in report.takeaways_b(s_df)))


if __name__ == "__main__":
    main()
