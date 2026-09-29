"""Base model vs fine-tuned model, side by side, from two results folders.

The Colab notebooks run each experiment twice, once per model, and keep the outputs apart:
``results_base/`` (``FF_BASE_MODEL=1``) and ``results_finetuned/`` (the model in ``models/``). This prints
and writes ``results/model_comparison.md`` with one table per experiment that ran:

* Experiment A (``exp_a.csv``): mean perplexity per cache config (lower is better);
* Experiment B (``exp_b.csv``): recall / anchor / root-cause / coherence per cache config;
* Replay (``replay.csv``): RCA metrics on the held-out incidents per ablation;
* Guided Q&A (``lora_eval_base.csv`` / ``lora_eval_tuned.csv``): the guided-mode scores.

    python -m ff.eval.compare_models --base results_base --tuned results_finetuned [--charts]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from ff.config import get_paths


def _read(folder: Path, name: str) -> pd.DataFrame | None:
    """``folder/name`` as a DataFrame, or None when that experiment has not run."""
    f = folder / name
    return pd.read_csv(f) if f.exists() else None


def side_by_side(base: pd.DataFrame, tuned: pd.DataFrame, key: str, cols: list[str]) -> pd.DataFrame:
    """One row per ``key`` value, ``<col> base`` / ``<col> tuned`` columns."""
    cols = [c for c in cols if c in base.columns and c in tuned.columns]
    b = base.groupby(key)[cols].mean(numeric_only=True)
    t = tuned.groupby(key)[cols].mean(numeric_only=True)
    out = pd.concat({"base": b, "tuned": t}, axis=1).swaplevel(axis=1).sort_index(axis=1, level=0)
    out.columns = [f"{c} {m}" for c, m in out.columns]
    return out.round(3)


def compare(base_dir: Path, tuned_dir: Path) -> dict[str, pd.DataFrame]:
    """Every comparison table for which both folders hold results, by title."""
    out: dict[str, pd.DataFrame] = {}
    a_b, a_t = _read(base_dir, "exp_a.csv"), _read(tuned_dir, "exp_a.csv")
    if a_b is not None and a_t is not None:
        out["Experiment A: perplexity by cache config (lower is better)"] = side_by_side(
            a_b, a_t, "config", ["chunk_ppl", "ms_per_token"])
    b_b, b_t = _read(base_dir, "exp_b.csv"), _read(tuned_dir, "exp_b.csv")
    if b_b is not None and b_t is not None:
        for df in (b_b, b_t):
            df["root_cause_top3"] = pd.to_numeric(df["root_cause_top3"], errors="coerce")
        out["Experiment B: long debugging sessions by cache config (higher is better)"] = side_by_side(
            b_b, b_t, "config", ["recall_acc", "anchor_acc", "root_cause_top3", "coherent_rate", "format_ok_rate"])
    r_b, r_t = _read(base_dir, "replay.csv"), _read(tuned_dir, "replay.csv")
    if r_b is not None and r_t is not None:
        out["Replay: RCA on the held-out incidents (higher is better)"] = side_by_side(
            r_b, r_t, "ablation", ["top3_acc", "groundedness", "culprit_hit", "culprit_file_hit", "blast_hit",
                                   "remediation_hit"])
    g_b = _read(base_dir, "lora_eval_base.csv")
    g_t = _read(tuned_dir, "lora_eval_tuned.csv")
    if g_b is not None and g_t is not None:
        from ff.train.lora_eval import compare as guided_compare

        table, _, msg = guided_compare(g_b, g_t)
        table.attrs["verdict"] = msg
        out["Guided Q&A on the held-out incidents (higher is better)"] = table.round(3)
    return out


def charts(folder: Path, dpi: int = 110) -> list[Path]:
    """Draw the Experiment A / B charts (``ff.eval.report``) from the CSVs in ``folder``, into ``folder``."""
    from ff.eval.report import save_exp_a_pngs, save_exp_b_pngs

    a, s, t = _read(folder, "exp_a.csv"), _read(folder, "exp_b.csv"), _read(folder, "exp_b_turns.csv")
    if a is not None:
        save_exp_a_pngs(a, folder, dpi)
    if s is not None and t is not None:
        save_exp_b_pngs(s, t, folder, dpi)
    return sorted(folder.glob("exp_*.png"))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    home = get_paths().home
    ap.add_argument("--base", default=str(home / "results_base"))
    ap.add_argument("--tuned", default=str(home / "results_finetuned"))
    ap.add_argument("--charts", action="store_true", help="also draw the Experiment A / B charts in both folders")
    args = ap.parse_args(argv)
    if args.charts:
        for folder in (Path(args.base), Path(args.tuned)):
            if folder.is_dir():
                print(folder, [p.name for p in charts(folder)])
    tables = compare(Path(args.base), Path(args.tuned))
    if not tables:
        print("Nothing to compare: run the experiments for both models first.")
        return
    lines = ["# Base vs fine-tuned model", ""]
    for name, t in tables.items():
        print(f"\n== {name}\n{t.to_string()}")
        lines += [f"## {name}", "", t.to_markdown(), ""]
        if t.attrs.get("verdict"):
            print(t.attrs["verdict"])
            lines += [t.attrs["verdict"], ""]
    out = get_paths().results / "model_comparison.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
