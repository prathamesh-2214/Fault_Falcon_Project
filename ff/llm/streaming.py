"""KV-cache policies built on the original mit-han-lab/streaming-llm code.

Modes
-----
* :class:`FullCache` — never evicts (the "normal LLM"); used on the UNPATCHED model.
* :class:`StreamingCache` — ``StartRecentKVCache(start_size, recent_size)``: attention
  sinks + a rolling window, on the pos-shift-patched model.
* :class:`WindowCache` — the same with ``start_size=0`` (the "no sinks" control).
* :class:`AnchoredStreamingCache` — FaultFalcon's mode: the never-evicted start region is
  an ordered list of named segments ``sinks | system | initial_query | checkpoints |
  lens_baseline | ledger`` followed by the rolling window. Segments are ordered from
  never-changing to most-changing, so changing segment *k* only re-encodes *k* onward:
  attention is causal, so the cached K/V of segments before *k* stay exactly valid.

Pos-shift attention (``enable_llama_pos_shift_attention``) caches keys *before* RoPE and
rotates them by their position inside the cache at every step; without it, evicting
middle tokens leaves position gaps. The patch sets an instance attribute ``forward`` on
each ``LlamaAttention``; :func:`pos_shift` toggles it so one set of weights serves both
the patched and the unpatched model.
"""

from __future__ import annotations

import contextlib
import io
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import torch
from streaming_llm.kv_cache import StartRecentKVCache
from streaming_llm.pos_shift.modify_llama import enable_llama_pos_shift_attention
from transformers.models.llama.modeling_llama import LlamaAttention

from ff import config

PastKV = Any  # legacy tuple/list format: [[k, v], ...] with k/v shaped [b, kv_heads, seq, dim]


class BudgetError(ValueError):
    """The start region would leave less than ``MIN_RECENT`` tokens for the rolling window."""


class CheckpointLimitError(BudgetError):
    """More checkpoints (anchor events) than allowed."""


# --------------------------------------------------------------------------- helpers
def cache_len(past: PastKV | None) -> int:
    """Number of tokens held in ``past`` (0 for an empty cache)."""
    return 0 if past is None else int(past[0][0].shape[2])


def make_kv(start_size: int, recent_size: int) -> StartRecentKVCache:
    """Build a ``StartRecentKVCache`` without its constructor's print."""
    with contextlib.redirect_stdout(io.StringIO()):
        return StartRecentKVCache(start_size=start_size, recent_size=recent_size, k_seq_dim=2, v_seq_dim=2)


def is_pos_shift_enabled(model: torch.nn.Module) -> bool:
    """True if any ``LlamaAttention`` in ``model`` has the pos-shift forward installed."""
    return any(isinstance(m, LlamaAttention) and "forward" in m.__dict__ for m in model.modules())


def _disable_pos_shift(model: torch.nn.Module) -> None:
    for m in model.modules():
        if isinstance(m, LlamaAttention) and "forward" in m.__dict__:
            del m.forward


@contextmanager
def pos_shift(model: torch.nn.Module, enabled: bool = True) -> Iterator[torch.nn.Module]:
    """Temporarily set the pos-shift patch state of ``model``; restores it on exit."""
    was = is_pos_shift_enabled(model)
    if enabled and not was:
        enable_llama_pos_shift_attention(model)
    elif not enabled and was:
        _disable_pos_shift(model)
    try:
        yield model
    finally:
        now = is_pos_shift_enabled(model)
        if was and not now:
            enable_llama_pos_shift_attention(model)
        elif not was and now:
            _disable_pos_shift(model)


@torch.no_grad()
def forward(model: torch.nn.Module, ids: Sequence[int], past: PastKV | None) -> tuple[torch.Tensor, PastKV]:
    """One forward pass, batch size 1, ``attention_mask=None``. Returns (logits, past)."""
    device = next(model.parameters()).device
    x = torch.tensor([list(ids)], dtype=torch.long, device=device)
    out = model(input_ids=x, past_key_values=past, use_cache=True, attention_mask=None)
    return out.logits, out.past_key_values


def prefill(model: torch.nn.Module, ids: Sequence[int], past: PastKV | None,
            policy: CachePolicy) -> tuple[torch.Tensor | None, PastKV | None]:
    """Feed ``ids`` in chunks of at most ``policy.chunk_size``, making room before each chunk.

    Returns the logits of the last chunk (None if ``ids`` is empty) and the new cache.
    """
    logits = None
    step = policy.chunk_size
    for i in range(0, len(ids), step):
        chunk = ids[i:i + step]
        past = policy.make_room(past, len(chunk))
        logits, past = forward(model, chunk, past)
    return logits, past


# --------------------------------------------------------------------------- policies
class CachePolicy:
    """Common interface: ``make_room`` before feeding tokens, ``stats`` for the UI."""

    name: str = "base"
    needs_pos_shift: bool = False
    capacity: int | None = None
    recent_size: int = 1024

    @property
    def chunk_size(self) -> int:
        """Prefill chunk: at most half the rolling window, so a chunk never overflows it."""
        return max(1, self.recent_size // 2)

    def make_room(self, past: PastKV | None, num_coming: int) -> PastKV | None:
        """Evict so that ``num_coming`` more tokens fit. Default: never evict."""
        return past

    def stats(self, past: PastKV | None) -> dict[str, Any]:
        """Token accounting for the cache bar in the UI."""
        n = cache_len(past)
        return {"mode": self.name, "sink_tokens": 0, "segments": {}, "rolling_tokens": n,
                "total_tokens": n, "capacity": self.capacity, "last_rebuild": None}


class FullCache(CachePolicy):
    """Never evicts. Use on the unpatched model."""

    name = "full"

    def __init__(self, chunk: int = 1024) -> None:
        self.recent_size = chunk


class StreamingCache(CachePolicy):
    """Attention sinks + rolling window (``StartRecentKVCache``) on the pos-shift model."""

    name = "streaming"
    needs_pos_shift = True

    def __init__(self, start_size: int = config.SINK_TOKENS, recent_size: int = config.TOTAL_CACHE - config.SINK_TOKENS) -> None:
        self.start_size = start_size
        self.recent_size = recent_size
        self.capacity = start_size + recent_size
        self.kv = make_kv(start_size, recent_size)

    def make_room(self, past: PastKV | None, num_coming: int) -> PastKV | None:
        # evict_for_space misbehaves if num_coming > recent_size; callers chunk anyway.
        return self.kv.evict_for_space(past, min(num_coming, self.recent_size))

    def stats(self, past: PastKV | None) -> dict[str, Any]:
        n = cache_len(past)
        sinks = min(n, self.start_size)
        return {"mode": self.name, "sink_tokens": sinks, "segments": {"sinks": sinks},
                "rolling_tokens": n - sinks, "total_tokens": n, "capacity": self.capacity,
                "last_rebuild": None}


class WindowCache(StreamingCache):
    """``StartRecentKVCache(start_size=0)`` on the same patched model: the no-sink control."""

    name = "window"

    def __init__(self, recent_size: int = 1024) -> None:
        super().__init__(start_size=0, recent_size=recent_size)


class AnchoredStreamingCache(CachePolicy):
    """FaultFalcon's cache: a segmented, never-evicted start region + a rolling window.

    The start region is ``SEGMENT_ORDER`` = sinks, system, initial_query, checkpoints,
    lens_baseline, ledger. ``start_size`` is the sum of segment lengths and the rolling
    window gets ``total_size - start_size`` tokens (never fewer than ``min_recent``).
    """

    name = "faultfalcon"
    needs_pos_shift = True

    def __init__(self, model: torch.nn.Module, total_size: int = config.TOTAL_CACHE,
                 min_recent: int = config.MIN_RECENT, sink_size: int = config.SINK_TOKENS,
                 budgets: dict[str, int] | None = None,
                 max_checkpoints: int = config.MAX_CHECKPOINTS) -> None:
        self.model = model
        self.total_size = total_size
        self.capacity = total_size
        self.min_recent = min_recent
        self.sink_size = sink_size
        self.budgets = dict(config.SEGMENT_BUDGETS if budgets is None else budgets)
        self.max_checkpoints = max_checkpoints
        self.order: tuple[str, ...] = config.SEGMENT_ORDER
        self.segments: dict[str, list[int]] = {n: [] for n in self.order}
        self.n_checkpoints = 0
        self.past: PastKV | None = None
        self.last_rebuild: dict[str, Any] | None = None

    # ------------------------------------------------------------------ layout
    @property
    def start_size(self) -> int:
        return sum(len(v) for v in self.segments.values())

    @property
    def recent_size(self) -> int:  # type: ignore[override]
        return self.total_size - self.start_size

    def offsets(self) -> dict[str, tuple[int, int]]:
        """Segment table: name -> (start_offset, length)."""
        out, pos = {}, 0
        for n in self.order:
            out[n] = (pos, len(self.segments[n]))
            pos += len(self.segments[n])
        return out

    def segment_table(self) -> dict[str, tuple[int, int, list[int]]]:
        """name -> (start_offset, length, token_ids)."""
        return {n: (o, ln, list(self.segments[n])) for n, (o, ln) in self.offsets().items()}

    def _flatten_checkpoints(self, value: Sequence[int] | Sequence[Sequence[int]]) -> tuple[list[int], int]:
        if value and isinstance(value[0], (list, tuple)):
            items = [list(v) for v in value]  # type: ignore[arg-type]
            return [t for it in items for t in it], len(items)
        flat = list(value)  # type: ignore[arg-type]
        return flat, (1 if flat else 0)

    def _validate(self, segments: dict[str, list[int]], n_checkpoints: int) -> None:
        if n_checkpoints > self.max_checkpoints:
            raise CheckpointLimitError(
                f"At most {self.max_checkpoints} checkpoints fit in the anchor block; "
                f"unpin (drop) one before adding another.")
        for name, ids in segments.items():
            if len(ids) > self.budgets.get(name, 10**9):
                raise BudgetError(
                    f"Segment '{name}' has {len(ids)} tokens, over its budget of {self.budgets[name]}; "
                    f"shorten it{' or drop a checkpoint' if name == 'checkpoints' else ''}.")
        start = sum(len(v) for v in segments.values())
        if self.total_size - start < self.min_recent:
            raise BudgetError(
                f"Start region of {start} tokens leaves {self.total_size - start} < {self.min_recent} "
                f"rolling tokens; drop a checkpoint or shorten the ledger.")

    # ------------------------------------------------------------------ API
    def init_session(self, segments: dict[str, Any]) -> PastKV:
        """Prefill all segments in order (fresh cache) and record their offsets.

        ``segments`` maps segment names to token ids. If ``sinks`` is absent, the first
        ``sink_size`` tokens of ``system`` (which must start with BOS) become the sinks.
        ``checkpoints`` may be a list of per-checkpoint id lists (counted for the limit).
        """
        unknown = set(segments) - set(self.order)
        if unknown:
            raise KeyError(f"unknown segments: {sorted(unknown)}")
        segs = {n: [] for n in self.order}
        n_ck = 0
        for name, value in segments.items():
            if name == "checkpoints":
                segs[name], n_ck = self._flatten_checkpoints(value)
            else:
                segs[name] = list(value)
        if "sinks" not in segments:
            system = segs["system"]
            segs["sinks"], segs["system"] = system[:self.sink_size], system[self.sink_size:]
        self._validate(segs, n_ck)
        self.segments, self.n_checkpoints = segs, n_ck
        t0 = time.perf_counter()
        past = None
        with pos_shift(self.model, True):
            for name in self.order:
                _, past = prefill(self.model, self.segments[name], past, self)
        self.past = past
        self.last_rebuild = {"segment": "all", "tokens_reencoded": self.start_size,
                             "ms": (time.perf_counter() - t0) * 1000}
        return past

    def update_segment(self, name: str, new_ids: Sequence[int] | Sequence[Sequence[int]],
                       tail_ids: Sequence[int] = (), past: PastKV | None = None) -> PastKV:
        """Replace segment ``name`` and re-encode it, the later segments and ``tail_ids``.

        Segments before ``name`` are NOT recomputed (exact: attention is causal). The
        rolling window is dropped; ``tail_ids`` (the last turn) is re-fed so the
        conversation continues. Validation happens before anything is mutated.
        """
        if name not in self.order or name == "sinks":
            raise KeyError(f"cannot update segment {name!r}")
        past = self.past if past is None else past
        segs = {n: list(v) for n, v in self.segments.items()}
        n_ck = self.n_checkpoints
        if name == "checkpoints":
            segs[name], n_ck = self._flatten_checkpoints(new_ids)
        else:
            segs[name] = list(new_ids)  # type: ignore[arg-type]
        self._validate(segs, n_ck)
        offset_k = self.offsets()[name][0]
        t0 = time.perf_counter()
        kv = make_kv(offset_k, 0)
        past = kv.evict_range(past, offset_k, cache_len(past)) if past is not None else None
        self.segments, self.n_checkpoints = segs, n_ck
        k = self.order.index(name)
        reencoded = 0
        with pos_shift(self.model, True):
            for seg in self.order[k:]:
                _, past = prefill(self.model, self.segments[seg], past, self)
                reencoded += len(self.segments[seg])
            tail = list(tail_ids)[-(self.recent_size - 1):] if tail_ids else []
            _, past = prefill(self.model, tail, past, self)
            reencoded += len(tail)
        self.past = past
        self.last_rebuild = {"segment": name, "tokens_reencoded": reencoded,
                             "ms": (time.perf_counter() - t0) * 1000}
        return past

    def set_checkpoints(self, items: Sequence[Sequence[int]], tail_ids: Sequence[int] = ()) -> PastKV:
        """Replace the checkpoint list (one id list per checkpoint); enforces the count limit."""
        return self.update_segment("checkpoints", [list(i) for i in items], tail_ids)

    def make_room(self, past: PastKV | None, num_coming: int) -> PastKV | None:
        """``evict_for_space`` with the current start size: the start region is never evicted."""
        kv = make_kv(self.start_size, self.recent_size)
        return kv.evict_for_space(past, min(num_coming, self.recent_size))

    def stats(self, past: PastKV | None = None) -> dict[str, Any]:
        past = self.past if past is None else past
        n = cache_len(past)
        segs = {k: len(v) for k, v in self.segments.items()}
        return {"mode": self.name, "sink_tokens": segs["sinks"], "segments": segs,
                "rolling_tokens": max(0, n - self.start_size), "total_tokens": n,
                "capacity": self.total_size, "last_rebuild": self.last_rebuild}
