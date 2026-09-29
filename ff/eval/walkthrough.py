"""A full multi-turn RCA of one incident, scripted, with the real model: see (and verify) it yourself.

The scripted engineer runs the whole cycle a DevOps team would:
change timeline -> suspects -> runtime evidence -> (follow the dependency chain, one service per
turn, re-reading each service's changes) -> root-cause hypothesis -> confirm the change and drill
into its diff -> confirm the breaking line -> blast radius -> remediation -> RCA report.

Engineer decisions (which hop to take, which change / line to confirm):
* default ``--guided``: the engineer "has checked" and acts on the ground truth, so the full
  macro -> micro flow is always shown. The MODEL's answers are shown exactly as generated.
* ``--unguided``: the engineer follows the model (its first cited change, else the ranker's top
  change) and the top suspect line, so wrong turns stay wrong.

Writes ``results/walkthrough_<INC>.md`` (transcript + scorecard + RCA report).

    python -m ff.eval.walkthrough --incident INC-109            # real model (CPU: ~5 min)
    python -m ff.eval.walkthrough --incident INC-109 --tiny     # plumbing check in seconds
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from typing import Any

from ff import config
from ff.config import get_paths
from ff.engine import stages as st
from ff.engine.lens import Lens
from ff.engine.session import Session, cited_ids
from ff.eval import replay
from ff.ingest.pipeline import open_stores
from ff.ingest.simulator import load_world
from ff.llm.model import count_tokens, get_model
from ff.retrieve.ranker import Ranker


def run(incident_id: str, tiny: bool = False, guided: bool = True, max_new_tokens: int = config.MAX_NEW_TOKENS,
        echo: bool = True) -> dict[str, Any]:
    paths = get_paths()
    world = load_world(paths)
    inc = next(i for i in world.incidents if i["id"] == incident_id)
    store, vectors = open_stores(paths)
    model, tok = get_model(tiny=tiny)
    ranker = Ranker(store, vectors, count=lambda s: count_tokens(tok, s))
    start = datetime.fromisoformat(inc["incident_start"])
    query = f"{inc['service']} error burst since {start:%H:%M}, what changed?"
    lens = Lens(inc["service"], start, stage=st.CHANGE_TIMELINE)
    anchors = [c["id"] for c in ranker.checkpoint_candidates(lens, query) if c["preselect"]][:3]
    s = Session(model, tok, ranker, mode="faultfalcon", store=store, session_id=f"walkthrough-{incident_id}",
                max_new_tokens=max_new_tokens, token_scale=4 if tiny else 1)
    s.start(query, lens, anchors)
    log: list[dict[str, Any]] = []

    def say(msg: str) -> None:
        if echo:
            print(msg, flush=True)

    def turn(question: str, stage: str | None = None, note: str = "") -> Any:
        if stage:
            s.set_stage(stage)
        t0 = time.perf_counter()
        res = s.turn(question)
        entry = {"turn": res.turn, "stage": res.stage, "lens": s.lens.service, "question": question, "note": note,
                 "context_ids": res.context_ids, "answer": res.answer, "cache": res.cache_stats.get("total_tokens"),
                 "ms_per_token": res.gen_stats.get("decode_ms_per_token"), "seconds": time.perf_counter() - t0}
        log.append(entry)
        say(f"\n--- turn {res.turn} · {st.LABELS[res.stage]} · lens {s.lens.service}" + (f" · {note}" if note else ""))
        say(f"Q: {question}\ncontext: {', '.join(res.context_ids[:10])}{' ...' if len(res.context_ids) > 10 else ''}")
        say(f"A: {res.answer[:600]}")
        return res

    say(f"=== {incident_id} · {inc['service']} · {inc['incident_start']} · anchors {anchors}")
    turn(query, st.CHANGE_TIMELINE)
    turn(st.DEFAULT_QUESTIONS[st.SUSPECTS], st.SUSPECTS)
    turn(st.DEFAULT_QUESTIONS[st.RUNTIME], st.RUNTIME)
    # follow the dependency chain (guided: towards the culprit's service; unguided: stay)
    target_svc = inc.get("cause_service") or inc["service"]
    if guided:
        current = inc["service"]
        for hop in config.dependency_path(inc["service"], target_svc)[1:]:
            if target_svc in config.neighbourhood(current):
                break
            turn(f"lens {hop}", st.CHANGE_TIMELINE, note=f"engineer follows the dependency to {hop}")
            current = hop
    hyp = turn("What is the most likely root cause? Cite the change id, or say no change explains it.", st.HYPOTHESIS)
    model_cause = next((i for i in cited_ids(hyp.answer) if (store.get_event(i) or {}).get("event_type")
                        in config.CHANGE_TYPES), None)
    cause = inc.get("cause_event_id") if guided else (model_cause or (ranker.ranked_changes(s.lens, query)[:1] or
                                                                      [{"id": None}])[0]["id"])
    score = {"model_cited_cause_in_hypothesis": replay.common.root_cause_top3(hyp.answer, inc.get("cause_event_id")),
             "model_named_breaking_file": None, "model_named_breaking_line": None, "suspect_ranker_top1": None, "rca_root_cause_correct": None,
             "rca_breaking_line_correct": None, "lens_path": " -> ".join(dict.fromkeys(e["lens"] for e in log))}
    if cause:
        s.drill(cause)
        diff = turn(st.DEFAULT_QUESTIONS[st.DIFF_ANALYSIS], note=f"engineer confirmed [{cause}] as the root cause")
        score["model_named_breaking_line"] = replay.culprit_mentioned(diff.answer, inc.get("culprit") or {})
        score["model_named_breaking_file"] = replay.culprit_file_mentioned(diff.answer, inc.get("culprit") or {})
        hunks = ranker.suspect_hunks(s.lens, ranker.target_changes(s.lens, s.ledger.render(s.count)))
        line = inc.get("culprit_change_id") if guided and inc.get("culprit_change_id") else (
            hunks[0]["change_id"] if hunks else None)
        score["suspect_ranker_top1"] = bool(hunks) and hunks[0]["change_id"] == inc.get("culprit_change_id")
        if line:
            s.confirm(line)
        for d in inc.get("decoy_ids", [])[:1] if guided else []:
            s.rule_out(d)
        for c in inc.get("contributing_ids", [])[:1] if guided else []:
            s.contributing(c)
        turn(st.DEFAULT_QUESTIONS[st.BLAST_RADIUS], st.BLAST_RADIUS)
        turn(st.DEFAULT_QUESTIONS[st.REMEDIATION], st.REMEDIATION)
    s.set_stage(st.CLOSE)
    rep = s.rca()
    score["rca_root_cause_correct"] = rep.root_cause_id == inc.get("cause_event_id")
    score["rca_breaking_line_correct"] = rep.breaking_change_id == inc.get("culprit_change_id")
    md = [f"# Walkthrough {incident_id} ({'guided' if guided else 'unguided'}, "
          f"{'tiny model' if tiny else config.MODEL_NAME})", "",
          f"Incident: {inc['service']} at {inc['incident_start']} · scenario {inc['scenario_type']} "
          f"({inc.get('scenario_title', '')}) · lens path {score['lens_path']}", "", "## Scorecard", "",
          "| check | result |", "| --- | --- |"]
    md += [f"| {k} | {v} |" for k, v in score.items()]
    md += ["", "## Transcript", ""]
    for e in log:
        md += [f"### Turn {e['turn']} · {st.LABELS[e['stage']]} · lens {e['lens']}" + (f" · {e['note']}" if e["note"]
                                                                                       else ""),
               "", f"**Q:** {e['question']}", "", f"Context ids: {', '.join(e['context_ids'])}", "",
               f"**A:** {e['answer']}", "",
               f"_cache {e['cache']} tokens · {e['ms_per_token'] or 0:.0f} ms/token · {e['seconds']:.1f} s_", ""]
    md += ["## RCA report (generated from the ledger)", "", rep.markdown]
    out = paths.results / f"walkthrough_{incident_id}.md"
    out.write_text("\n".join(md), encoding="utf-8")
    say("\n=== scorecard\n" + "\n".join(f"{k}: {v}" for k, v in score.items()) + f"\n-> {out}")
    return {"score": score, "turns": log, "report": rep, "path": out}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--incident", default="INC-109")
    ap.add_argument("--tiny", action="store_true")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--guided", dest="guided", action="store_true", default=True)
    g.add_argument("--unguided", dest="guided", action="store_false")
    ap.add_argument("--max-new-tokens", type=int, default=config.MAX_NEW_TOKENS)
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    run(args.incident, args.tiny, args.guided, args.max_new_tokens)


if __name__ == "__main__":
    main()
