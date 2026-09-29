"""Window-level precision / recall / F1 of the significance detector against BGL labels.

A window is truly anomalous if it holds any line whose BGL ``Label`` is not '-'. The
detector never sees labels, so this is an honest (naive) evaluation. Only non-empty
windows are scored. Writes ``results/detector_bgl.csv``.
"""

from __future__ import annotations

import pandas as pd

from ff.config import Paths, get_paths


def score(windows: pd.DataFrame) -> dict[str, float]:
    """Precision / recall / F1 for windows with ``significant`` and ``n_alert`` columns."""
    w = windows[windows["n_lines"] > 0]
    truth = w["n_alert"] > 0
    pred = w["significant"].astype(bool)
    tp = int((truth & pred).sum())
    fp = int((~truth & pred).sum())
    fn = int((truth & ~pred).sum())
    tn = int((~truth & ~pred).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"windows": len(w), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall, "f1": f1}


def run(paths: Paths | None = None, service: str = "node-svc") -> dict[str, float]:
    """Score the saved windows of ``service`` (BGL) and write the CSV."""
    paths = (paths or get_paths()).ensure()
    windows = pd.read_parquet(paths.derived / "windows.parquet")
    w = windows[windows["service"] == service]
    if "source" in w.columns:  # the BGL labels exist only in the real LogHub stream
        w = w[w["source"] == "loghub"]
    res = {"service": service, **score(w)}
    pd.DataFrame([res]).to_csv(paths.results / "detector_bgl.csv", index=False)
    return res


def main() -> None:
    r = run()
    print(f"BGL detector over {r['windows']} non-empty windows: precision {r['precision']:.2f}, "
          f"recall {r['recall']:.2f}, F1 {r['f1']:.2f} (tp={r['tp']} fp={r['fp']} fn={r['fn']} tn={r['tn']})")


if __name__ == "__main__":
    main()
