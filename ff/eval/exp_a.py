"""Experiment A: cache mechanics (perplexity / memory / speed) on one long token stream.

Configs (all on the same stream; follows streaming-llm's examples/eval_long_ppl.py):
1. dense      unpatched model, full cache (goes past the model's training length)
2. window     pos-shift model, StartRecentKVCache(start_size=0, recent_size=1024)
3. streaming  pos-shift model, StartRecentKVCache(start_size=4, recent_size=1020)
4. recompute  for each position t, re-encode tokens [t-1024, t) from scratch (quality
              reference; slow, so limited to the first ``--recompute-limit`` tokens)
window and streaming differ ONLY by the 4 sink tokens.

Per 500-token chunk: perplexity, cache length, peak GPU memory, ms/token.
Outputs results/exp_a.csv, results/exp_a_stream.txt and PNG charts.

    python -m ff.eval.exp_a --device cuda              # Colab T4 (float16)
    python -m ff.eval.exp_a --tiny --tokens 1500       # CI plumbing check
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from typing import Any

import pandas as pd
import torch

from ff import config
from ff.config import get_paths
from ff.eval.stream import build_stream_ids
from ff.llm.model import get_model
from ff.llm.streaming import CachePolicy, FullCache, StreamingCache, WindowCache, cache_len, forward, pos_shift

CONFIGS = ("dense", "window", "streaming", "recompute")


def kv_bytes_per_token(model: torch.nn.Module, bytes_per_value: int = 2) -> int:
    """2 (K and V) * layers * kv_heads * head_dim * bytes."""
    c = model.config
    kv_heads = getattr(c, "num_key_value_heads", None) or c.num_attention_heads
    head_dim = c.hidden_size // c.num_attention_heads
    return 2 * c.num_hidden_layers * kv_heads * head_dim * bytes_per_value


def _reset_mem(device: str) -> None:
    if device.startswith("cuda"):
        torch.cuda.reset_peak_memory_stats()


def _peak_mb(device: str) -> float:
    return torch.cuda.max_memory_allocated() / 2**20 if device.startswith("cuda") else float("nan")


@torch.no_grad()
def run_cached(model: torch.nn.Module, ids: list[int], policy: CachePolicy, name: str, chunk: int,
               device: str) -> list[dict[str, Any]]:
    """Token-by-token NLL with a KV cache, aggregated per chunk. OOM is recorded, not fatal."""
    rows: list[dict[str, Any]] = []
    past = None
    nll_sum, n, ms = 0.0, 0, 0.0
    _reset_mem(device)
    with pos_shift(model, policy.needs_pos_shift):
        for i in range(len(ids) - 1):
            try:
                t0 = time.perf_counter()
                past = policy.make_room(past, 1)
                logits, past = forward(model, [ids[i]], past)
                nll = -torch.log_softmax(logits[0, -1].float(), -1)[ids[i + 1]].item()
                if device.startswith("cuda"):
                    torch.cuda.synchronize()
                ms += (time.perf_counter() - t0) * 1000
            except torch.cuda.OutOfMemoryError:
                rows.append({"config": name, "tokens_seen": i, "chunk_ppl": float("nan"), "cache_len": cache_len(past),
                             "peak_mem_mb": _peak_mb(device), "ms_per_token": float("nan"), "note": "OOM"})
                print(f"\n{name}: out of memory at token {i}", flush=True)
                return rows
            nll_sum += nll
            n += 1
            if n == chunk or i == len(ids) - 2:
                rows.append({"config": name, "tokens_seen": i + 1, "chunk_ppl": math.exp(nll_sum / n),
                             "cache_len": cache_len(past), "peak_mem_mb": _peak_mb(device), "ms_per_token": ms / n,
                             "note": ""})
                print(f"\r{name}: {i + 1}/{len(ids) - 1} ppl {rows[-1]['chunk_ppl']:.2f}", end="", flush=True)
                nll_sum, n, ms = 0.0, 0, 0.0
                _reset_mem(device)
    print()
    return rows


@torch.no_grad()
def run_recompute(model: torch.nn.Module, ids: list[int], window: int, chunk: int, device: str,
                  limit: int) -> list[dict[str, Any]]:
    """Sliding window with re-computation: the quality reference (no cache reuse)."""
    rows: list[dict[str, Any]] = []
    nll_sum, n, ms = 0.0, 0, 0.0
    end = min(len(ids) - 1, limit)
    _reset_mem(device)
    with pos_shift(model, False):
        for t in range(1, end + 1):
            t0 = time.perf_counter()
            ctx = ids[max(0, t - window):t]
            logits, _ = forward(model, ctx, None)
            nll = -torch.log_softmax(logits[0, -1].float(), -1)[ids[t]].item()
            if device.startswith("cuda"):
                torch.cuda.synchronize()
            ms += (time.perf_counter() - t0) * 1000
            nll_sum += nll
            n += 1
            if n == chunk or t == end:
                rows.append({"config": "recompute", "tokens_seen": t, "chunk_ppl": math.exp(nll_sum / n),
                             "cache_len": len(ctx), "peak_mem_mb": _peak_mb(device), "ms_per_token": ms / n,
                             "note": f"limited to first {end} tokens" if end < len(ids) - 1 else ""})
                print(f"\rrecompute: {t}/{end} ppl {rows[-1]['chunk_ppl']:.2f}", end="", flush=True)
                nll_sum, n, ms = 0.0, 0, 0.0
                _reset_mem(device)
    print()
    return rows


def run(tokens: int = 16000, device: str = "cpu", tiny: bool = False, configs: tuple[str, ...] = CONFIGS,
        recompute_limit: int = 6000, chunk: int = 500, window: int = 1024) -> pd.DataFrame:
    paths = get_paths().ensure()
    model, tok = get_model(tiny=tiny, device=device)
    ids = build_stream_ids(tok, tokens, paths, save_to=paths.results / "exp_a_stream.txt")
    print(f"model {config.MODEL_NAME if not tiny else 'tiny'}; stream {len(ids)} tokens; "
          f"KV bytes/token (fp16) = {kv_bytes_per_token(model)}")
    rows: list[dict[str, Any]] = []
    policies = {"dense": FullCache(), "window": WindowCache(window), "streaming": StreamingCache(4, window - 4)}
    for name in configs:
        if name == "recompute":
            rows += run_recompute(model, ids, window, chunk, device, recompute_limit)
        else:
            rows += run_cached(model, ids, policies[name], name, chunk, device)
    df = pd.DataFrame(rows)
    df["kv_bytes_per_token"] = kv_bytes_per_token(model)
    df["model"] = "tiny" if tiny else config.MODEL_NAME
    df["train_len"] = config.MODEL_TRAIN_LEN
    df.to_csv(paths.results / "exp_a.csv", index=False)
    from ff.eval import report

    report.save_exp_a_pngs(df, paths.results)
    return df


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tokens", type=int, default=16000)
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    ap.add_argument("--tiny", action="store_true")
    ap.add_argument("--configs", default=",".join(CONFIGS))
    ap.add_argument("--recompute-limit", type=int, default=6000)
    ap.add_argument("--chunk", type=int, default=500)
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    df = run(args.tokens, args.device, args.tiny, tuple(args.configs.split(",")), args.recompute_limit, args.chunk)
    from ff.eval import report

    print("\n".join(f"- {s}" for s in report.takeaways_a(df)))


if __name__ == "__main__":
    main()
