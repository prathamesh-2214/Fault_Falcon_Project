"""ChromaDB vector store: 'events' (narratives) and 'architecture' (runbook/topology chunks).

The default embedding is chromadb's ``DefaultEmbeddingFunction`` (ONNX all-MiniLM-L6-v2,
downloaded once into ~/.cache/chroma). :class:`HashEmbedding` is a deterministic,
dependency-free stand-in used by tests and CI (``FF_EMBEDDING=hash``) so nothing is
downloaded there.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from chromadb.utils.embedding_functions import register_embedding_function

_WORD = re.compile(r"[a-z0-9_.]+")


@register_embedding_function
class HashEmbedding(EmbeddingFunction[Documents]):
    """Hashed bag of words + bigrams, L2-normalised. Deterministic and offline."""

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002 (chroma's signature)
        out = []
        for text in input:
            v = np.zeros(self.dim, dtype=np.float32)
            words = _WORD.findall(text.lower())
            for tok in words + [f"{a} {b}" for a, b in zip(words, words[1:])]:
                h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "little")
                v[h % self.dim] += 1.0 if (h >> 40) & 1 else -1.0
            n = float(np.linalg.norm(v))
            out.append(v / n if n else v)
        return out

    @staticmethod
    def name() -> str:
        return "ff-hash"

    def get_config(self) -> dict[str, Any]:
        return {"dim": self.dim}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> HashEmbedding:
        return HashEmbedding(config.get("dim", 384))

    def default_space(self) -> str:
        return "cosine"


def default_embedding() -> EmbeddingFunction:
    """``FF_EMBEDDING=hash`` -> :class:`HashEmbedding`, otherwise chroma's ONNX MiniLM."""
    if os.environ.get("FF_EMBEDDING", "").lower() == "hash":
        return HashEmbedding()
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

    return DefaultEmbeddingFunction()


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity (0 for a zero vector)."""
    a, b = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    return float(a @ b / (na * nb)) if na and nb else 0.0


class VectorStore:
    """Two chroma collections. ``path=None`` gives an in-memory store (unique namespace)."""

    def __init__(self, path: str | Path | None = None, embedding_fn: EmbeddingFunction | None = None,
                 namespace: str | None = None) -> None:
        import chromadb
        from chromadb.config import Settings

        self.ef = embedding_fn or default_embedding()
        settings = Settings(anonymized_telemetry=False, allow_reset=True)
        if path is None:
            self.client = chromadb.EphemeralClient(settings=settings)
            namespace = namespace or f"ff{uuid.uuid4().hex[:8]}"
        else:
            Path(path).mkdir(parents=True, exist_ok=True)
            self.client = chromadb.PersistentClient(path=str(path), settings=settings)
        self.ns = f"{namespace}_" if namespace else ""
        self._meta = {"hnsw:space": "cosine"}
        self.events = self.client.get_or_create_collection(f"{self.ns}events", embedding_function=self.ef,
                                                           metadata=self._meta)
        self.architecture = self.client.get_or_create_collection(f"{self.ns}architecture", embedding_function=self.ef,
                                                                 metadata=self._meta)
        self._emb_cache: dict[str, np.ndarray] = {}

    def reset_events(self) -> None:
        """Drop and recreate the events collection (full re-ingest)."""
        self.client.delete_collection(f"{self.ns}events")
        self.events = self.client.get_or_create_collection(f"{self.ns}events", embedding_function=self.ef,
                                                           metadata=self._meta)
        self._emb_cache.clear()

    # ------------------------------------------------------------------ writes
    def upsert_events(self, events: Iterable[dict[str, Any]], signatures_by_anomaly: dict[str, list[str]] | None = None,
                      batch: int = 1000) -> int:
        """Index event narratives with filterable metadata."""
        from ff.store.sqlite_store import to_epoch

        rows = list(events)
        sigs = signatures_by_anomaly or {}
        for i in range(0, len(rows), batch):
            part = rows[i:i + batch]
            metas = []
            for e in part:
                p = e.get("payload") or {}
                tpls = sigs.get(e["id"]) or p.get("templates") or ([p["template"]] if p.get("template") else [])
                metas.append({
                    "service": e["service"], "environment": e.get("environment") or "prod",
                    "event_type": e["event_type"], "occurred_at": to_epoch(e["occurred_at"]),
                    "significance": int(e.get("significance") or 1), "source": e["source"],
                    "error_signatures": " | ".join(tpls)[:2000],
                })
            self.events.upsert(ids=[e["id"] for e in part], documents=[e.get("narrative") or e["id"] for e in part],
                               metadatas=metas)
        self._emb_cache.clear()
        return len(rows)

    def upsert_chunks(self, chunks: Sequence[dict[str, Any]]) -> int:
        """Chunks: {id, text, service, path, kind}; kind: runbook / topology / release / codebase / postmortem."""
        if chunks:
            for i in range(0, len(chunks), 1000):
                part = chunks[i:i + 1000]
                self.architecture.upsert(ids=[c["id"] for c in part], documents=[c["text"] for c in part],
                                         metadatas=[{"service": c["service"], "path": c["path"],
                                                     "kind": c.get("kind", "runbook"),
                                                     "published": int(c.get("published", 0))} for c in part])
        return len(chunks)

    def delete_chunks(self, path: str) -> None:
        self.architecture.delete(where={"path": path})

    # ------------------------------------------------------------------ reads
    def count(self) -> dict[str, int]:
        return {"events": self.events.count(), "architecture": self.architecture.count()}

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        return [np.asarray(v, dtype=np.float32) for v in self.ef(list(texts))]

    def event_embeddings(self, ids: Sequence[str]) -> dict[str, np.ndarray]:
        """Stored embeddings for ``ids`` (cached per process)."""
        missing = [i for i in ids if i not in self._emb_cache]
        if missing:
            try:
                got = self.events.get(ids=missing, include=["embeddings"])
                pairs = list(zip(got["ids"], got["embeddings"]))
            except Exception:  # noqa: BLE001 - chroma's in-memory store occasionally fails a bulk get: go one by one
                pairs = []
                for i in missing:
                    try:
                        one = self.events.get(ids=[i], include=["documents"])
                    except Exception:  # noqa: BLE001
                        continue
                    if one["ids"]:
                        pairs.append((i, self.embed(one["documents"])[0]))
            for i, v in pairs:
                self._emb_cache[i] = np.asarray(v, dtype=np.float32)
        return {i: self._emb_cache[i] for i in ids if i in self._emb_cache}

    def similarity(self, query: str, ids: Sequence[str]) -> dict[str, float]:
        """Cosine similarity between ``query`` and each stored event in ``ids`` (exact, no ANN)."""
        if not ids:
            return {}
        q = self.embed([query])[0]
        return {i: max(0.0, cosine(q, v)) for i, v in self.event_embeddings(ids).items()}

    def query_events(self, text: str, n: int = 25, where: dict[str, Any] | None = None) -> list[tuple[str, float]]:
        """Top-n events by similarity: [(id, sim)], sim = 1 - cosine distance."""
        total = self.events.count()
        if not total:
            return []
        res = self.events.query(query_texts=[text], n_results=min(n, total), where=where)
        return [(i, 1.0 - float(d)) for i, d in zip(res["ids"][0], res["distances"][0])]

    def query_architecture(self, text: str, services: Sequence[str], n: int = 4,
                           kinds: Sequence[str] | None = None, before: int | None = None) -> list[dict[str, Any]]:
        """Top-n knowledge chunks tagged with one of ``services`` (or 'all'), optionally of given kinds and
        published at or before the epoch ``before`` (so a document never reveals a later event)."""
        total = self.architecture.count()
        if not total:
            return []
        conds: list[dict[str, Any]] = [{"service": {"$in": list(services) + ["all"]}}]
        if kinds:
            conds.append({"kind": {"$in": list(kinds)}})
        if before is not None:
            conds.append({"published": {"$lte": int(before)}})
        where: dict[str, Any] = conds[0] if len(conds) == 1 else {"$and": conds}
        try:
            res = self.architecture.query(query_texts=[text], n_results=min(n, total), where=where)
        except Exception:  # noqa: BLE001 - chroma raises when fewer chunks match than requested
            return []
        return [{"id": i, "text": d, "service": m["service"], "path": m["path"], "kind": m.get("kind", "runbook"),
                 "sim": 1.0 - float(dist), "published": m.get("published")}
                for i, d, m, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0])]


def approx_tokens(text: str) -> int:
    """Rough token count when no tokenizer is at hand.

    Log/deploy text is id- and number-heavy, so words undercount; ~3.2 characters per
    token is close to (and slightly above) TinyLlama's real count on this data.
    """
    return max(math.ceil(len(text) / 3.2), len(text.split()))
