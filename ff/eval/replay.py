"""Replay evaluation: every held-out incident in 'faultfalcon' mode with a scripted engineer.

Macro metrics: top-3 root-cause accuracy (final hypothesis), turns to root cause, file hit rate
(change_detail files of cited events vs expected_files), citation groundedness (verified /
all cited ids), and the detector precision/recall from Prompt 3.

Micro metrics (after the root cause is confirmed): culprit_hit (the diff-analysis answer names
the breaking line), suspect_top1 (the deterministic suspect-line ranking puts it first),
blast_hit (share of affected services named), remediation_hit (rollback / revert / restore of
the right thing) and rca_complete (the RCA report has both macro and micro findings).

Ablations (``--ablations``): guarantees off, stage order off, ledger off, anchor block off,
checkpoints re-retrieved every turn, architecture retrieval off, hourly baseline snapshots
stored as vectors instead of one pinned baseline record.

The scripted engineer: asks the initial question (change timeline), focuses the first
change the model cites (suspects), asks about runtime evidence, then asks for a hypothesis.
While turns remain and the top cited change is not the cause, it marks that change ruled
out (as an engineer would after checking it) and asks again.

    python -m ff.eval.replay                       # all ablations, real model
    python -m ff.eval.replay --tiny --max-turns 4  # CI plumbing check
"""

from __future__ import annotations

import argparse
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
from ff.engine.session import Session, SessionFlags
from ff.eval import common
from ff.llm.model import get_model
from ff.retrieve.ranker import Ranker, RankerFlags


@dataclass
class Ablation:
    name: str
    ranker: RankerFlags = field(default_factory=RankerFlags)
    session: SessionFlags = field(default_factory=SessionFlags)
    mode: str = "faultfalcon"
    hourly_baselines: bool = False


ABLATIONS: dict[str, Ablation] = {a.name: a for a in (
    Ablation("faultfalcon"),
    Ablation("no_guarantees", ranker=RankerFlags(guarantees=False)),
    Ablation("no_stage_order", ranker=RankerFlags(stage_filter=False)),
    Ablation("no_ledger", session=SessionFlags(ledger=False)),
    Ablation("no_anchor", mode="ff_no_anchor"),
    Ablation("reretrieve_checkpoints", session=SessionFlags(reretrieve_checkpoints=True)),
    Ablation("no_architecture", ranker=RankerFlags(architecture=False)),
    Ablation("hourly_baseline_vectors", session=SessionFlags(baseline_pinned=False), hourly_baselines=True),
)}


def hourly_snapshots(paths: Any) -> list[dict[str, Any]]:
    """BASELINE_SNAPSHOT pseudo-events: one per service per hour from the real log windows."""
    w = pd.read_parquet(paths.derived / "windows.parquet")
    w["hour"] = pd.to_datetime(w["start"]).dt.floor("h")
    out = []
    for (svc, hour), g in w.groupby(["service", "hour"]):
        ts = hour.to_pydatetime().isoformat()
        text = (f"[BASELINE_SNAPSHOT] {svc} {hour:%m-%d %H}:00: error ratio {g['error_ratio'].mean():.2f}, "
                f"{g['n_lines'].sum()} lines")
        out.append({"id": f"evt_{900000 + len(out)}", "event_type": "BASELINE_SNAPSHOT", "service": svc,
                    "environment": "prod", "occurred_at": ts, "significance": 1, "source": "logs", "narrative": text,
                    "payload": {}})
    return out


def play(inc: dict[str, Any], ranker: Ranker, store: Any, model: torch.nn.Module, tok: Any, ab: Ablation,
         max_turns: int, max_new_tokens: int, token_scale: int = 1, micro_phase: bool = True) -> dict[str, Any]:
    """Run the scripted engineer on one incident (macro, then micro); returns the metric row."""
    start = datetime.fromisoformat(inc["incident_start"])
    query = f"{inc['service']} error burst since {start:%H:%M}, what changed?"
    lens = Lens(inc["service"], start, stage=st.CHANGE_TIMELINE)
    anchors = [c["id"] for c in ranker.checkpoint_candidates(lens, query) if c["preselect"]][:3]
    s = Session(model, tok, ranker, mode=ab.mode, flags=ab.session, max_new_tokens=max_new_tokens,
                session_id=f"replay-{ab.name}-{inc['id']}", token_scale=token_scale)
    s.start(query, lens, anchors)
    cause = inc.get("cause_event_id")
    all_cited: list[str] = []
    visited = [inc["service"]]  # services the lens visited (cross-service incidents need hops)
    unverified = 0
    turns_to_rc = None
    final = ""
    plan = [(st.CHANGE_TIMELINE, query), (st.SUSPECTS, st.DEFAULT_QUESTIONS[st.SUSPECTS]),
            (st.RUNTIME, st.DEFAULT_QUESTIONS[st.RUNTIME])]
    # cross-service incidents: an SRE follows the dependency chain one hop per turn, re-reading
    # the change timeline of each service, until the culprit's service is in view
    target_svc = inc.get("cause_service") or inc["service"]
    current = inc["service"]
    for hop in config.dependency_path(inc["service"], target_svc)[1:]:
        if target_svc in config.neighbourhood(current):
            break
        plan.append((st.CHANGE_TIMELINE, f"lens {hop}"))
        current = hop
    for t in range(1, max_turns + 1):
        stage, q = plan[t - 1] if t <= len(plan) else (st.HYPOTHESIS, "What is the most likely root cause? Cite the "
                                                                       "change id, or say no change explains it.")
        s.set_stage(stage)
        if stage == st.SUSPECTS:
            changes = [i for i in all_cited if (store.get_event(i) or {}).get("event_type") in config.CHANGE_TYPES]
            if changes:
                s.set_lens(Lens(**{**s.lens.__dict__, "focus": changes[0]}))
        res = s.turn(q)
        if s.lens.service != visited[-1]:
            visited.append(s.lens.service)
        ids = common.cited(res.answer)
        all_cited += [i for i in ids if i not in all_cited]
        unverified += len(res.unverified)
        # NO_CHANGE is only "found" when a hypothesis turn says so
        counts = cause is not None or stage == st.HYPOTHESIS
        if turns_to_rc is None and counts and common.root_cause_top3(res.answer, cause):
            turns_to_rc = t
        if stage == st.HYPOTHESIS:
            final = res.answer
            if common.root_cause_top3(res.answer, cause):
                break
            wrong = [i for i in ids[:1] if i != cause]
            if wrong:
                s.rule_out(wrong[0])
    macro_ok = common.root_cause_top3(final, cause)
    # ---- micro phase. To score it independently of the macro result, the scripted engineer
    # confirms the TRUE cause if the model missed it (an engineer would get there eventually);
    # `macro_top3` above is the model's own macro result.
    micro = {"culprit_hit": None, "culprit_file_hit": None, "suspect_top1": None, "blast_hit": None, "remediation_hit": None,
             "rca_complete": None}
    culprit = inc.get("culprit") or {}
    if cause is not None and micro_phase:
        s.drill(cause)
        res = s.turn(st.DEFAULT_QUESTIONS[st.DIFF_ANALYSIS])
        unverified += len(res.unverified)
        all_cited += [i for i in common.cited(res.answer) if i not in all_cited]
        micro["culprit_hit"] = culprit_mentioned(res.answer, culprit)
        micro["culprit_file_hit"] = culprit_file_mentioned(res.answer, culprit)
        hunks = ranker.suspect_hunks(s.lens, ranker.target_changes(s.lens, s.ledger.render(s.count)))
        micro["suspect_top1"] = bool(hunks) and hunks[0]["change_id"] == inc.get("culprit_change_id")
        if hunks:  # the engineer confirms the top suspect line (deterministic evidence, not ground truth)
            s.confirm(hunks[0]["change_id"])
        s.set_stage(st.BLAST_RADIUS)
        res = s.turn(st.DEFAULT_QUESTIONS[st.BLAST_RADIUS])
        expected_blast = inc.get("blast_radius") or [inc["service"]]
        micro["blast_hit"] = sum(b in res.answer for b in expected_blast) / len(expected_blast)
        s.set_stage(st.REMEDIATION)
        res = s.turn(st.DEFAULT_QUESTIONS[st.REMEDIATION])
        micro["remediation_hit"] = remediation_mentioned(res.answer, store.get_event(cause), culprit)
        micro["rca_complete"] = s.rca().complete
    cds = store.get_change_details(all_cited)
    files = {c["file"] for c in cds if c.get("file")}
    expected = set(inc.get("expected_files", []))
    n_cited = len(all_cited) + unverified
    return {"ablation": ab.name, "incident": inc["id"], "scenario": inc["scenario_type"],
            "top3": macro_ok, "turns_to_root_cause": turns_to_rc, "turns": s.turn_no,
            "file_hit_rate": len(files & expected) / len(expected) if expected else float("nan"),
            "groundedness": len(all_cited) / n_cited if n_cited else float("nan"), **micro,
            "lens_path": " -> ".join(visited),
            "final_answer": final[:400]}


def culprit_mentioned(answer: str, culprit: dict[str, Any]) -> bool:
    """The DIFF_ANALYSIS answer names the breaking line: its chg id, or file name + line number."""
    if not culprit:
        return False
    base = (culprit.get("file") or "").rsplit("/", 1)[-1]
    return f"[{culprit['change_id']}]" in answer or (bool(base) and base in answer and str(culprit["line"]) in answer)


def culprit_file_mentioned(answer: str, culprit: dict[str, Any]) -> bool:
    """The answer names the breaking line's file (a weaker, file-level hit)."""
    base = (culprit or {}).get("file", "").rsplit("/", 1)[-1]
    return bool(base) and base in answer


def remediation_mentioned(answer: str, cause: dict[str, Any] | None, culprit: dict[str, Any]) -> bool:
    """The REMEDIATION answer says roll back / revert / restore AND names what to restore."""
    low = answer.lower()
    verb = any(w in low for w in ("roll back", "rollback", "revert", "restore", "undo"))
    what = [culprit.get("file", "").rsplit("/", 1)[-1], culprit.get("param_key") or ""]
    if cause:
        what += [cause.get("version_from") or "", cause["service"]]
    return verb and any(w and w.lower() in low for w in what)


def run(tiny: bool = False, device: str = "cpu", ablations: tuple[str, ...] = tuple(ABLATIONS), max_turns: int = 8,
        variants: int = 0, max_new_tokens: int = config.MAX_NEW_TOKENS, embedding: Any | None = None) -> pd.DataFrame:
    paths = get_paths().ensure()
    model, tok = get_model(tiny=tiny, device=device)
    baselines = common.load_baselines(paths)
    worlds = common.heldout_worlds(paths, variants)
    snaps = hourly_snapshots(paths) if any(ABLATIONS[a].hourly_baselines for a in ablations) else []
    rows = []
    for inc, world in worlds:
        base = common.build_stores(world, baselines, embedding)
        snap = common.build_stores(world, baselines, embedding, snaps) if snaps else None
        for name in ablations:
            ab = ABLATIONS[name]
            store, vectors = snap if ab.hourly_baselines and snap else base
            ranker = Ranker(store, vectors, count=lambda s: len(tok.encode(s, add_special_tokens=False)), flags=ab.ranker)
            t0 = time.perf_counter()
            row = play(inc, ranker, store, model, tok, ab, max_turns, max_new_tokens, 4 if tiny else 1)
            rows.append(row)
            print(f"{inc['id']:10s} {name:24s} top3={row['top3']} turns_to_rc={row['turns_to_root_cause']} "
                  f"files={row['file_hit_rate']:.2f} grounded={row['groundedness']:.2f} "
                  f"[{time.perf_counter() - t0:.0f}s]", flush=True)
    detail = pd.DataFrame(rows)
    detail["model"] = "tiny" if tiny else config.MODEL_NAME
    detail.to_csv(paths.results / "replay_incidents.csv", index=False)
    det = paths.results / "detector_bgl.csv"
    d = pd.read_csv(det).iloc[0] if det.exists() else {"precision": float("nan"), "recall": float("nan")}
    for col in ("culprit_hit", "culprit_file_hit", "suspect_top1", "blast_hit", "remediation_hit", "rca_complete"):
        detail[col] = pd.to_numeric(detail[col], errors="coerce")
    agg = detail.groupby("ablation", sort=False).agg(
        incidents=("incident", "count"), top3_acc=("top3", "mean"),
        turns_to_root_cause=("turns_to_root_cause", "mean"), file_hit_rate=("file_hit_rate", "mean"),
        groundedness=("groundedness", "mean"), culprit_hit=("culprit_hit", "mean"),
        culprit_file_hit=("culprit_file_hit", "mean"),
        suspect_top1=("suspect_top1", "mean"), blast_hit=("blast_hit", "mean"),
        remediation_hit=("remediation_hit", "mean"), rca_complete=("rca_complete", "mean")).reset_index()
    agg["detector_precision"], agg["detector_recall"] = d["precision"], d["recall"]
    agg["model"] = detail["model"].iloc[0] if len(detail) else ""
    agg.to_csv(paths.results / "replay.csv", index=False)
    return agg


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tiny", action="store_true")
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    ap.add_argument("--ablations", default=",".join(ABLATIONS))
    ap.add_argument("--max-turns", type=int, default=8)
    ap.add_argument("--variants", type=int, default=0)
    ap.add_argument("--max-new-tokens", type=int, default=config.MAX_NEW_TOKENS)
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    agg = run(args.tiny, args.device, tuple(args.ablations.split(",")), args.max_turns, args.variants,
              args.max_new_tokens)
    print(agg.to_string(index=False))
    from ff.eval import report

    print("\n".join(f"- {x}" for x in report.takeaways_replay(agg)))


if __name__ == "__main__":
    main()
