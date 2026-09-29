"""Micro-level check: does the diff ranker point at the true breaking line?

For every incident with a culprit, the macro cause is given (oracle), the suspect hunks are
ranked (``Ranker.suspect_hunks``) and the rank of the true culprit hunk is recorded. This
measures the deterministic "suspect line" context the model and the UI get, independently of
the language model. Writes ``results/micro_check.csv``.

    python -m ff.eval.micro_check
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from ff.config import Paths, get_paths
from ff.engine.lens import Lens
from ff.retrieve.ranker import Ranker


def check(ranker: Ranker, incidents: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for inc in incidents:
        if not inc.get("cause_event_id") or not inc.get("culprit_change_id"):
            continue
        lens = Lens(inc["service"], datetime.fromisoformat(inc["incident_start"]))
        hunks = ranker.suspect_hunks(lens, ranker.store.get_events(ids=[inc["cause_event_id"]]))
        ids = [h["change_id"] for h in hunks]
        rank = ids.index(inc["culprit_change_id"]) + 1 if inc["culprit_change_id"] in ids else None
        top = hunks[0] if hunks else {}
        rows.append({"incident": inc["id"], "scenario": inc["scenario_type"], "culprit": inc["culprit_change_id"],
                     "culprit_file": (inc.get("culprit") or {}).get("file"), "candidates": len(ids), "rank": rank,
                     "top1": rank == 1, "top3": rank is not None and rank <= 3, "top_change": top.get("change_id"),
                     "top_score": top.get("score"), "top_reasons": "; ".join(top.get("reasons", []))})
    return pd.DataFrame(rows)


def run(paths: Paths | None = None) -> pd.DataFrame:
    from ff.ingest.pipeline import open_stores
    from ff.ingest.simulator import load_world

    paths = (paths or get_paths()).ensure()
    store, vectors = open_stores(paths)
    df = check(Ranker(store, vectors), load_world(paths).incidents)
    df.to_csv(paths.results / "micro_check.csv", index=False)
    return df


def main() -> None:
    df = run()
    print(df[["incident", "scenario", "culprit", "candidates", "rank", "top_score"]].to_string(index=False))
    if len(df):
        print(f"\nbreaking line ranked first: {df['top1'].mean():.0%}; in the top 3: {df['top3'].mean():.0%} "
              f"({len(df)} incidents with a culprit)")


if __name__ == "__main__":
    main()
