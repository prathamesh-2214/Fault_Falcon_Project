"""``make ingest``: loghub -> dataset (plan, telemetry, log_events, simulator) -> stores -> doc_indexer.

Usage::

    python -m ff.ingest.pipeline                     # template narratives, ONNX embeddings
    python -m ff.ingest.pipeline --narrator llm      # TinyLlama narratives (CPU, cached)
    python -m ff.ingest.pipeline --offline --embedding hash   # CI / no network
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from typing import Any

from ff.config import Paths, get_paths
from ff.store.sqlite_store import SqliteStore
from ff.store.vector_store import VectorStore, approx_tokens


def open_stores(paths: Paths | None = None) -> tuple[SqliteStore, VectorStore]:
    """The persistent SQLite + chroma stores under ``var/``."""
    paths = (paths or get_paths()).ensure()
    return SqliteStore(paths.sqlite), VectorStore(paths.chroma)


def load_into_stores(world: Any, store: SqliteStore, vectors: VectorStore, llm: Callable[[str, str], str] | None = None,
                     reset: bool = True) -> list[dict[str, Any]]:
    """Render narratives + significance, then fill SQLite and chroma. Returns the final events."""
    from ff.ingest.deploy_summarizer import summarize_all

    events = summarize_all(world.events, world.change_details, llm, store)
    for e in events:
        e.pop("_sim", None)
    from ff.ingest.simulator import public_change_detail

    cds = [public_change_detail(c) for c in world.change_details]
    if reset:
        store.reset_events()
        vectors.reset_events()
    store.add_events(events)
    store.add_change_details(cds)
    store.add_error_signatures(world.signatures)
    by_anomaly: dict[str, list[str]] = {}
    for s in world.signatures:
        by_anomaly.setdefault(s["anomaly_id"], []).append(s["template"])
    vectors.upsert_events(events, by_anomaly)
    return events


def run(paths: Paths | None = None, narrator: str = "template", offline: bool = False) -> dict[str, Any]:
    """Run the whole ingestion; returns count summaries."""
    from ff.ingest import dataset, doc_indexer, loghub, simulator

    paths = (paths or get_paths()).ensure()
    if not offline:
        loghub.download(paths=paths)
    loghub.audit(paths)
    llm, count = None, approx_tokens
    if narrator != "template":
        from ff.llm.model import count_tokens, get_model, make_chat_fn

        model, tok = get_model(tiny=narrator == "tiny")
        llm = make_chat_fn(model, tok, max_new_tokens=160)
        count = lambda text: count_tokens(tok, text)  # noqa: E731
    store, vectors = open_stores(paths)
    world = dataset.build(paths, store=store, llm=llm)["world"]
    load_into_stores(world, store, vectors, llm)
    doc_indexer.write_docs(paths, simulator.final_params(world), store, world)
    docs = doc_indexer.index(paths, store, vectors, count)
    from ff.eval import detector_check

    det = detector_check.run(paths)
    return {"by_type": store.count_events("event_type"), "by_source": store.count_events("source"),
            "by_service": store.count_events("service"), "vectors": vectors.count(), "docs": docs,
            "incidents": len(world.incidents), "detector_bgl": det}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--narrator", choices=["template", "llm", "tiny"], default="template")
    ap.add_argument("--offline", action="store_true", help="do not download LogHub (files must be present)")
    ap.add_argument("--embedding", choices=["onnx", "hash"], default=None,
                    help="hash = offline test embedding (sets FF_EMBEDDING=hash)")
    args = ap.parse_args(argv)
    if args.embedding == "hash":
        os.environ["FF_EMBEDDING"] = "hash"
    out = run(narrator=args.narrator, offline=args.offline)
    for k in ("by_type", "by_source", "by_service"):
        print(f"{k}:")
        for name, n in out[k].items():
            print(f"  {name:16s} {n}")
    print(f"vectors: {out['vectors']}  docs: {out['docs']}  incidents: {out['incidents']}")
    d = out["detector_bgl"]
    print(f"BGL detector: precision {d['precision']:.2f} recall {d['recall']:.2f} F1 {d['f1']:.2f}")


if __name__ == "__main__":
    main()
