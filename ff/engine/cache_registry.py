"""Process-local registry of live sessions (and their KV caches), keyed by LangGraph thread_id.

Tensors cannot be serialised into LangGraph state, so the KV cache lives here. After an
app restart (or a Streamlit rerun in a new process) :func:`get_or_rebuild` rebuilds the
whole start region — sinks, system, initial query, anchor events, lens/baseline, ledger —
from the JSON state. Only the rolling window is lost, and that is logged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ff.engine.session import Session

log = logging.getLogger(__name__)

CACHES: dict[str, Session] = {}
REBUILDS: list[str] = []  # thread ids rebuilt from state (inspected by tests and the UI)


def create(thread_id: str, factory: Callable[[str, str], Session], mode: str) -> Session:
    """Create (or replace) the live session for ``thread_id``."""
    s = factory(mode, thread_id)
    CACHES[thread_id] = s
    return s


def get(thread_id: str) -> Session | None:
    return CACHES.get(thread_id)


def get_or_rebuild(state: dict[str, Any], factory: Callable[[str, str], Session]) -> Session:
    """The live session for ``state['thread_id']``, rebuilt from state if it is missing."""
    tid = state["thread_id"]
    s = CACHES.get(tid)
    if s is not None:
        return s
    log.warning("KV cache for thread %s missing (restart?): rebuilding the start region from state", tid)
    s = factory(state.get("cache_mode") or "faultfalcon", tid)
    s.restore({"initial_query": state["initial_query"], "anchor_ids": state.get("anchor_ids", []),
               "lens": state["lens"], "ledger": state.get("ledger", []), "turn": state.get("turn", 0)})
    CACHES[tid] = s
    REBUILDS.append(tid)
    return s


def drop(thread_id: str) -> None:
    CACHES.pop(thread_id, None)
