"""Does the fine-tune help? Base vs fine-tuned model on the HELD-OUT incidents, guided mode.

Both models walk the same guided path (``ff.train.lora_data.walk``, same seed, the engineer's
actions follow the ground truth, as in ``make walkthrough``); the answers are generated for real
(brief mode, anchored StreamingLLM cache) and scored against what the answer should contain:

==================  ================================================================================
cause_cited         CHANGE_TIMELINE / SUSPECTS / HYPOTHESIS: cites the root-cause id when it is in view
hop_named           CHANGE_TIMELINE with the cause on a dependency: names the next service to check
verdict_ok          SUSPECTS: says yes to the cause and no to a decoy; NO_CHANGE: says no change fits
culprit_hit         DIFF_ANALYSIS: names the breaking line (chg id, or file + line number)
culprit_file_hit    DIFF_ANALYSIS: names the breaking line's file
blast_hit           BLAST_RADIUS: share of the affected services named
remediation_hit     REMEDIATION: says roll back / revert / restore and names what
grounded            no invented ids (every cited id was in the context, anchor or ledger)
brief               at most 3 sentences and the answer ended by itself (not cut at the token limit)
coherent            distinct-2 > 0.3 and no 20-token repetition loop
==================  ================================================================================

Keep rule: keep the fine-tune if the mean of these scores improves and ``grounded`` does not drop.

    python -m ff.train.lora_eval                                   # base model
    python -m ff.train.lora_eval --model models/smollm2-360m-ff    # fine-tuned
    python -m ff.train.lora_eval --compare                         # the two CSVs side by side
"""

from __future__ import annotations

import argparse
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

from ff import config
from ff.config import get_paths
from ff.engine import stages as st
from ff.engine.session import Session
from ff.train.lora_data import Facts, walk

METRICS = ("cause_cited", "hop_named", "verdict_ok", "culprit_hit", "culprit_file_hit", "blast_hit",
           "remediation_hit", "grounded", "brief", "coherent")
_NEG = re.compile(r"\b(no|not|none|unlikely|unrelated|doesn't|does not|cannot|can't)\b")


def sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\[`])", text.strip()) if s.strip()])


def score(answer: str, facts: Facts, inc: dict[str, Any], store: Any, res: Any, max_new_tokens: int) -> dict[str, Any]:
    """One turn's metrics (None = not applicable to this turn)."""
    from ff.eval import common, replay

    low = answer.lower()
    cited = common.cited(answer)
    row: dict[str, Any] = {m: None for m in METRICS}
    if facts.stage in (st.CHANGE_TIMELINE, st.SUSPECTS, st.HYPOTHESIS) and facts.must_cite and inc.get("cause_event_id") \
            in facts.must_cite:
        row["cause_cited"] = inc["cause_event_id"] in cited
    if facts.stage == st.CHANGE_TIMELINE and facts.must_name:
        row["hop_named"] = all(n.lower() in low for n in facts.must_name)
    if facts.verdict == "yes":
        row["verdict_ok"] = not low.lstrip().startswith("no") and facts.must_cite[0] in cited
    elif facts.verdict == "no":
        row["verdict_ok"] = bool(_NEG.search(low))
    elif facts.verdict == "none":
        row["verdict_ok"] = bool(_NEG.search(low))
    culprit = inc.get("culprit") or {}
    if facts.stage == st.DIFF_ANALYSIS:
        row["culprit_hit"] = replay.culprit_mentioned(answer, culprit)
        row["culprit_file_hit"] = replay.culprit_file_mentioned(answer, culprit)
    if facts.stage == st.BLAST_RADIUS and facts.must_name:
        row["blast_hit"] = sum(n in answer for n in facts.must_name) / len(facts.must_name)
    if facts.stage == st.REMEDIATION:
        row["remediation_hit"] = replay.remediation_mentioned(answer, store.get_event(inc["cause_event_id"]), culprit)
    stopped = (res.gen_stats or {}).get("new_tokens", 0) < max_new_tokens
    row["grounded"] = not res.unverified
    row["brief"] = sentences(answer) <= 3 and bool(stopped)
    row["coherent"] = common.distinct_n(answer, 2) > 0.3 if answer.strip() else False
    return row


def run(model_path: str | None = None, only: list[str] | None = None, variants: int = 0, max_new_tokens: int = 96,
        tiny: bool = False, echo: bool = True, device: str = "cpu") -> pd.DataFrame:
    """Walk every held-out incident with the model (the BASE model, or ``model_path``); one row per turn.

    The base run forces the base model even when a fine-tune sits in ``models/`` (auto-detected otherwise)."""
    import importlib

    import ff.config
    import ff.llm.model

    if model_path:
        os.environ["FF_MODEL_PATH"] = str(Path(model_path).resolve())
    elif not tiny:
        os.environ.pop("FF_MODEL_PATH", None)
        os.environ["FF_BASE_MODEL"] = "1"
    importlib.reload(ff.config)
    importlib.reload(ff.llm.model)
    from ff.eval import common
    from ff.llm.model import count_tokens, get_model
    from ff.retrieve.ranker import Ranker
    from ff.store.vector_store import HashEmbedding

    model, tok = get_model(tiny=tiny, device=device)
    from ff import config as cfg

    print(f"model: {cfg.MODEL_LABEL if not tiny else 'tiny'} on {device}", flush=True)
    paths = get_paths()
    baselines = common.load_baselines(paths)
    rows: list[dict[str, Any]] = []
    for inc, world in common.heldout_worlds(paths, variants=variants, only=only):
        store, vectors = common.build_stores(world, baselines, HashEmbedding())
        ranker = Ranker(store, vectors, count=lambda s: count_tokens(tok, s))
        s = Session(model, tok, ranker, mode="faultfalcon", max_new_tokens=max_new_tokens,
                    token_scale=4 if tiny else 1, session_id=f"lora-eval-{inc['id']}")
        s.brief = True

        def ask(sess: Session, question: str, gold: str, facts: Facts, inc: dict[str, Any] = inc,
                store: Any = store) -> None:
            t0 = time.time()
            res = sess.turn(question)
            row = {"incident": inc["id"], "scenario": inc["scenario_type"], "stage": facts.stage,
                   "question": question, "answer": res.answer, "gold": gold, "s": round(time.time() - t0, 1),
                   **score(res.answer, facts, inc, store, res, max_new_tokens)}
            rows.append(row)
            if echo:
                print(f"{inc['id']:<10} {facts.stage:<16} {res.answer[:150]!r}", flush=True)

        walk(s, inc, ranker, ask, random.Random(config.SEED))
    return pd.DataFrame(rows)


def summary(df: pd.DataFrame) -> pd.Series:
    """Mean of each metric over the turns where it applies, plus their overall mean."""
    out = {m: pd.to_numeric(df[m], errors="coerce").dropna().astype(float).mean() for m in METRICS if m in df}
    s = pd.Series(out)
    s["overall"] = s.dropna().mean()
    return s


def compare(base: pd.DataFrame, tuned: pd.DataFrame) -> tuple[pd.DataFrame, bool, str]:
    b, t = summary(base), summary(tuned)
    table = pd.DataFrame({"base": b, "fine_tuned": t, "delta": t - b})
    keep = bool(t["overall"] > b["overall"] and not (t["grounded"] < b["grounded"]))
    msg = (f"overall {b['overall']:.2f} -> {t['overall']:.2f}, grounded {b['grounded']:.2f} -> {t['grounded']:.2f}: "
           f"{'KEEP' if keep else 'DISCARD'} the fine-tune")
    return table, keep, msg


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=None, help="merged fine-tune directory (default: the base model)")
    ap.add_argument("--incidents", default="", help="comma-separated held-out incident ids (default: all)")
    ap.add_argument("--variants", type=int, default=0)
    ap.add_argument("--max-new-tokens", type=int, default=96)
    ap.add_argument("--tiny", action="store_true")
    ap.add_argument("--device", default="cpu", help="cpu or cuda")
    ap.add_argument("--compare", action="store_true", help="compare results/lora_eval_base.csv and _tuned.csv")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    res = get_paths().results
    res.mkdir(parents=True, exist_ok=True)
    if args.compare:
        table, _, msg = compare(pd.read_csv(res / "lora_eval_base.csv"), pd.read_csv(res / "lora_eval_tuned.csv"))
        print(table.round(3).to_string())
        print(msg)
        (res / "lora_eval_compare.md").write_text(table.round(3).to_markdown() + f"\n\n{msg}\n", encoding="utf-8")
        return
    df = run(args.model, [i for i in args.incidents.split(",") if i] or None, args.variants, args.max_new_tokens,
             args.tiny, device=args.device)
    out = res / f"lora_eval_{'tuned' if args.model else 'base'}.csv"
    df.to_csv(out, index=False)
    print(summary(df).round(3).to_string())
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
