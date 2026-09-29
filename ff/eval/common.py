"""Shared evaluation helpers: per-session stores, scoring without an LLM judge, timing."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from ff.config import Paths, get_paths
from ff.ingest import doc_indexer
from ff.ingest.pipeline import load_into_stores
from ff.ingest.simulator import World, final_params, load_logs_for_replay, load_world, make_variant
from ff.llm.model import distinct_n
from ff.store.sqlite_store import SqliteStore
from ff.store.vector_store import VectorStore

_CITE = re.compile(r"\[((?:evt|chg)_\d+)\]")


def build_stores(world: World, baselines: dict[str, dict[str, Any]], embedding_fn: Any | None = None,
                 extra_events: Sequence[dict[str, Any]] = ()) -> tuple[SqliteStore, VectorStore]:
    """In-memory SQLite + chroma for one world (base or variant), with runbooks indexed."""
    store = SqliteStore()
    vectors = VectorStore(embedding_fn=embedding_fn)
    load_into_stores(world, store, vectors)
    if extra_events:
        store.add_events(extra_events)
        vectors.upsert_events(extra_events)
    for svc, b in baselines.items():
        store.upsert_baseline(svc, b)
    chunks = []
    for rel, text in doc_indexer.knowledge_docs(final_params(world), baselines, world).items():
        chunks += doc_indexer.doc_chunks(rel, text)
    vectors.upsert_chunks(chunks)
    return store, vectors


def load_baselines(paths: Paths | None = None) -> dict[str, dict[str, Any]]:
    from ff.ingest.log_events import load_log_events

    return load_log_events(paths or get_paths())["baselines"]


def heldout_worlds(paths: Paths | None = None, variants: int = 2, only: Sequence[str] | None = None
                   ) -> list[tuple[dict[str, Any], World]]:
    """(incident, world) for each held-out incident and ``variants`` make_variant seeds of each."""
    paths = paths or get_paths()
    world = load_world(paths)
    logs = load_logs_for_replay(paths)
    out = []
    for inc in world.incidents:
        if inc["split"] != "heldout" or (only and inc["id"] not in only):
            continue
        out.append((inc, world))
        for seed in range(1, variants + 1):
            v = make_variant(world, inc["id"], seed=1000 + seed, logs=logs)
            out.append((v.incidents[0], v))
    return out


# --------------------------------------------------------------------------- scoring
def cited(answer: str) -> list[str]:
    """Verified citation ids in order (``[unverified evt_x]`` does not match)."""
    return list(dict.fromkeys(_CITE.findall(answer)))


def has_repetition_loop(token_ids: Sequence[int], n: int = 20) -> bool:
    """True if any n-token sequence occurs twice."""
    seen = set()
    for i in range(len(token_ids) - n + 1):
        g = tuple(token_ids[i:i + n])
        if g in seen:
            return True
        seen.add(g)
    return False


def coherent(answer: str, token_ids: Sequence[int]) -> bool:
    """distinct-2 > 0.3 and no 20-token repetition loop."""
    return distinct_n(answer, 2) > 0.3 and not has_repetition_loop(token_ids)


def format_ok(answer: str) -> bool:
    return bool(cited(answer))


def root_cause_top3(answer: str, cause_id: str | None) -> bool:
    """Cause among the first 3 cited ids; for NO_CHANGE the answer must say 'no change'."""
    if cause_id is None:
        return "no change" in answer.lower()
    return cause_id in cited(answer)[:3]
