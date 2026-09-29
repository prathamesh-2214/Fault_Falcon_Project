"""Start FaultFalcon with one command: ``run.bat`` (Windows), ``./run.sh`` (macOS / Linux) or ``make run``.

The dataset, the stores and the knowledge base are built ONCE and kept in ``data/``, ``var/`` and
``docs/architecture/`` (they ship with the repository). This launcher only builds them when they are
missing, then starts the Streamlit app. A fine-tuned model in ``models/`` is used automatically
(see ``config.MODEL_NAME``).

    python -m ff.launch                 # open http://localhost:8501
    python -m ff.launch --port 8600
    python -m ff.launch --rebuild       # rebuild the data (about 25 minutes on a laptop CPU)
    python -m ff.launch --warmup        # download / load the models once and exit (Docker build step)
    python -m ff.launch --check         # print what the app would use (model, data, stores) and exit
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from ff import config
from ff.config import Paths, get_paths


def data_ready(paths: Paths) -> bool:
    """The app can start: incidents, the SQLite store and the Chroma index are present."""
    return (paths.sqlite.exists() and (paths.chroma / "chroma.sqlite3").exists()
            and any(paths.incidents.glob("INC-*.json")))


def build(paths: Paths) -> None:
    from ff.ingest import pipeline

    print("Building the dataset, stores and knowledge base (one time, about 25 minutes) ...", flush=True)
    out = pipeline.run(paths)
    print(f"Done: {out['incidents']} incidents, {out['vectors']['events']} indexed events.", flush=True)


def warmup() -> None:
    """Load the generative model and the embedding model once, so both are cached (no downloads later)."""
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

    from ff.llm.model import load_model

    DefaultEmbeddingFunction()(["warm up the ONNX all-MiniLM-L6-v2 embedding model"])
    model, _ = load_model()
    print(f"Warm: {config.MODEL_LABEL} ({sum(p.numel() for p in model.parameters()) / 1e6:.0f} M parameters) and "
          f"the ONNX embedding model are cached.", flush=True)


def check(paths: Paths) -> dict[str, object]:
    """What the app will use: model, data, stores (printed by ``--check``)."""
    from ff.store.sqlite_store import SqliteStore
    from ff.store.vector_store import VectorStore

    out: dict[str, object] = {"model": config.MODEL_NAME, "fine_tuned": config.FINE_TUNED,
                              "incidents": len(list(paths.incidents.glob("INC-*.json"))), "data_ready": data_ready(paths)}
    if out["data_ready"]:
        out["sqlite_events"] = len(SqliteStore(paths.sqlite).get_events())
        out["chroma"] = VectorStore(paths.chroma).count()
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8501)
    ap.add_argument("--rebuild", action="store_true", help="rebuild the data even if it exists")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    ap.add_argument("--host", default="localhost", help="0.0.0.0 inside a container")
    ap.add_argument("--warmup", action="store_true", help="cache the models and exit")
    ap.add_argument("--check", action="store_true", help="print model / data / stores and exit")
    args = ap.parse_args(argv)
    paths = get_paths()
    if args.warmup:
        warmup()
        return
    if args.check:
        res = check(paths)
        for k, v in res.items():
            print(f"{k}: {v}")
        raise SystemExit(0 if res["data_ready"] else 1)
    if args.rebuild or not data_ready(paths):
        build(paths)
    print(f"Model: {config.MODEL_LABEL}. Data: {len(list(paths.incidents.glob('INC-*.json')))} incidents.")
    print(f"Opening FaultFalcon on http://localhost:{args.port} (Ctrl+C to stop)", flush=True)
    cmd = [sys.executable, "-m", "streamlit", "run", str(config.REPO_ROOT / "app" / "streamlit_app.py"),
           f"--server.port={args.port}", f"--server.address={args.host}", "--browser.gatherUsageStats=false"]
    if args.no_browser:
        cmd.append("--server.headless=true")
    raise SystemExit(subprocess.call(cmd, cwd=config.REPO_ROOT))


if __name__ == "__main__":
    main()
